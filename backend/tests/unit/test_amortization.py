# SPDX-License-Identifier: AGPL-3.0-or-later
"""Amortisation engine (``app.domain.loans.amortization``): exact figures and properties.

Property tests (hypothesis): for any terms, the principal parts of a
schedule add up to the principal exactly, every row is rounded to cents,
the balance never goes negative and falls monotonically, zero-rate loans
pay no interest, and a variable loan re-amortises at a mid-term rate change.
Hand-checked figures: a 30-year 6.125 % mortgage and a 7-month zero-rate loan.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.loans.amortization import (
    Extras,
    OneOffExtra,
    RateChange,
    RecurringExtra,
    ScheduleError,
    Start,
    Terms,
    add_months,
    first_due_after,
    level_payment,
    nearest_due_number,
    payment_at,
    project,
    schedule,
    simulate,
)

D = Decimal
CENT = D("0.01")


def _mortgage(**changes: object) -> Terms:
    fields: dict[str, object] = {
        "principal": D("250000.00"),
        "annual_rate": D("0.06125"),
        "term_months": 360,
        "origination_date": date(2024, 3, 1),
        "payment_day": 1,
        "currency": "USD",
    }
    fields.update(changes)
    return Terms(**fields)  # type: ignore[arg-type]


def _cents(value: Decimal) -> bool:
    return value == value.quantize(CENT)


# ---------------------------------------------------------------------------
# Exact figures
# ---------------------------------------------------------------------------


def test_level_payment_of_a_30_year_mortgage() -> None:
    assert level_payment(D("250000"), D("0.06125"), 360, "USD") == D("1519.03")
    plan = schedule(_mortgage())
    first, last = plan.rows[0], plan.rows[-1]
    assert (first.due_date, first.interest, first.principal) == (
        date(2024, 4, 1),
        D("1276.04"),
        D("242.99"),
    )
    assert first.balance == D("249757.01")
    assert len(plan.rows) == 360
    assert last.due_date == date(2054, 3, 1)
    # Final-payment adjustment: the last payment takes what rounding left.
    assert last.payment != D("1519.03")
    assert last.balance == 0
    assert plan.total_principal == D("250000.00")
    assert plan.total_interest == sum(r.interest for r in plan.rows)


def test_zero_rate_loan_spreads_the_rounding_into_the_final_payment() -> None:
    terms = Terms(
        principal=D("1000"),
        annual_rate=D("0"),
        term_months=7,
        origination_date=date(2024, 1, 31),
        payment_day=31,
        currency="USD",
    )
    plan = schedule(terms)
    assert [r.payment for r in plan.rows] == [D("142.86")] * 6 + [D("142.84")]
    assert [r.due_date for r in plan.rows][:2] == [date(2024, 2, 29), date(2024, 3, 31)]
    assert plan.total_interest == 0
    assert plan.total_principal == D("1000")


def test_escrow_is_added_to_each_total_but_not_to_the_balance() -> None:
    plan = schedule(_mortgage(escrow=D("350.00")))
    assert plan.rows[0].total == D("1869.03")
    assert plan.total_escrow == D("350.00") * 360
    assert plan.total_principal == D("250000.00")


def test_rate_change_mid_term_reamortises_a_variable_loan() -> None:
    terms = Terms(
        principal=D("100000"),
        annual_rate=D("0.05"),
        term_months=120,
        origination_date=date(2024, 1, 15),
        payment_day=15,
        currency="USD",
        variable=True,
        rate_changes=(RateChange(date(2026, 1, 15), D("0.07")),),
    )
    plan = schedule(terms)
    by_number = {r.number: r for r in plan.rows}
    assert by_number[24].rate == D("0.05")
    assert by_number[25].rate == D("0.07")
    assert by_number[24].payment == D("1060.66")
    # The new payment repays the balance after payment 24 over the 96 months left.
    assert by_number[25].payment == level_payment(by_number[24].balance, D("0.07"), 96, "USD")
    assert len(plan.rows) == 120
    assert plan.total_principal == D("100000")
    assert payment_at(terms, 30) == by_number[25].payment


def test_rate_change_on_a_fixed_loan_keeps_the_payment() -> None:
    terms = _mortgage(rate_changes=(RateChange(date(2030, 3, 1), D("0.05")),))
    plan = schedule(terms)
    assert {r.payment for r in plan.rows[:-1]} == {D("1519.03")}
    assert len(plan.rows) < 360  # less interest: paid off early
    assert plan.total_principal == D("250000.00")


def test_payment_that_does_not_cover_interest_is_refused() -> None:
    with pytest.raises(ScheduleError) as caught:
        schedule(_mortgage(payment=D("1000.00")))
    assert caught.value.code == "loan-payment-too-low"


def test_extra_payments_save_interest_and_shorten_the_loan() -> None:
    terms = _mortgage()
    start = Start(1, D("250000.00"))
    extras = Extras(
        one_off=(OneOffExtra(date(2024, 6, 10), D("5000")),),
        recurring=(RecurringExtra(D("200"), date(2024, 4, 1)),),
    )
    result = simulate(terms, start, extras)
    assert result.baseline.remaining_payments == 360
    assert result.scenario.remaining_payments < 300
    assert result.interest_saved > D("80000")
    assert result.payments_saved == 360 - result.scenario.remaining_payments
    rows = result.scenario.schedule.rows
    # Due 2024-06-01 is before the one-off's date: it goes with the July payment.
    assert (rows[2].due_date, rows[2].extra) == (date(2024, 6, 1), D("200"))
    assert (rows[3].due_date, rows[3].extra) == (date(2024, 7, 1), D("5200"))
    total = sum(r.principal + r.extra for r in rows)
    assert total == D("250000.00")


def test_recurring_extra_window_and_one_off_on_the_due_date() -> None:
    terms = _mortgage()
    extras = Extras(
        one_off=(OneOffExtra(date(2024, 5, 1), D("100")),),
        recurring=(RecurringExtra(D("50"), date(2024, 6, 1), date(2024, 7, 1)),),
    )
    rows = schedule(terms, extras=extras).rows
    assert [r.extra for r in rows[:5]] == [D(0), D("100"), D("50"), D("50"), D(0)]


def test_projection_from_a_balance_mid_term() -> None:
    terms = _mortgage()
    original = schedule(terms)
    after_12 = original.rows[11]
    projected = project(terms, Start(13, after_12.balance))
    assert projected.next_due_date == date(2025, 4, 1)
    assert projected.payoff_date == date(2054, 3, 1)
    assert projected.remaining_payments == 348
    assert projected.schedule.rows == original.rows[12:]


def test_due_date_helpers() -> None:
    assert add_months(date(2024, 1, 31), 1, 31) == date(2024, 2, 29)
    assert add_months(date(2024, 11, 15), 3, 15) == date(2025, 2, 15)
    terms = _mortgage()
    assert first_due_after(terms, date(2024, 3, 1)) == 1
    assert first_due_after(terms, date(2024, 4, 1)) == 2
    assert first_due_after(terms, date(2030, 12, 31)) == 82
    assert nearest_due_number(terms, date(2024, 3, 28)) == 1
    assert nearest_due_number(terms, date(2024, 4, 10)) == 1
    assert nearest_due_number(terms, date(2024, 4, 20)) == 2


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

amounts = st.decimals(min_value=D("100"), max_value=D("2000000"), places=2)
rates = st.decimals(min_value=D("0"), max_value=D("0.25"), places=5)
terms_months = st.integers(min_value=1, max_value=480)
days = st.dates(min_value=date(1990, 1, 1), max_value=date(2040, 12, 31))


@settings(max_examples=150, deadline=None)
@given(principal=amounts, rate=rates, months=terms_months, start=days, day=st.integers(1, 31))
def test_schedule_principal_sums_to_principal(
    principal: Decimal, rate: Decimal, months: int, start: date, day: int
) -> None:
    terms = Terms(
        principal=principal,
        annual_rate=rate,
        term_months=months,
        origination_date=start,
        payment_day=day,
        currency="USD",
    )
    plan = schedule(terms)
    assert sum(r.principal for r in plan.rows) == principal
    assert len(plan.rows) <= months
    assert plan.rows[-1].balance == 0
    previous = principal
    for row in plan.rows:
        assert row.principal >= 0
        assert row.interest >= 0
        assert all(_cents(v) for v in (row.payment, row.principal, row.interest, row.balance))
        assert row.balance < previous or (row.principal == 0 and row.balance == previous)
        assert row.payment == row.principal + row.interest
        previous = row.balance
    if rate == 0:
        assert plan.total_interest == 0
        payments = {r.payment for r in plan.rows[:-1]}
        assert len(payments) <= 1


@settings(max_examples=100, deadline=None)
@given(
    principal=amounts,
    rate=rates,
    new_rate=rates,
    months=st.integers(min_value=2, max_value=360),
    data=st.data(),
)
def test_rate_change_mid_term_still_repays_exactly(
    principal: Decimal, rate: Decimal, new_rate: Decimal, months: int, data: st.DataObject
) -> None:
    change_after = data.draw(st.integers(min_value=1, max_value=months - 1))
    origination = date(2020, 5, 10)
    terms = Terms(
        principal=principal,
        annual_rate=rate,
        term_months=months,
        origination_date=origination,
        payment_day=10,
        currency="USD",
        variable=True,
        rate_changes=(RateChange(add_months(origination, change_after, 10), new_rate),),
    )
    plan = schedule(terms)
    assert sum(r.principal for r in plan.rows) == principal
    assert len(plan.rows) == months or plan.rows[-1].balance == 0
    for row in plan.rows:
        expected = new_rate if row.number > change_after else rate
        assert row.rate == expected


@settings(max_examples=100, deadline=None)
@given(
    principal=amounts,
    rate=rates,
    months=st.integers(min_value=12, max_value=360),
    extra=st.decimals(min_value=D("1"), max_value=D("5000"), places=2),
)
def test_extras_never_cost_more_and_still_repay_exactly(
    principal: Decimal, rate: Decimal, months: int, extra: Decimal
) -> None:
    terms = Terms(
        principal=principal,
        annual_rate=rate,
        term_months=months,
        origination_date=date(2022, 2, 1),
        payment_day=1,
        currency="USD",
    )
    result = simulate(
        terms, Start(1, principal), Extras(recurring=(RecurringExtra(extra, date(2022, 1, 1)),))
    )
    rows = result.scenario.schedule.rows
    assert sum(r.principal + r.extra for r in rows) == principal
    assert result.interest_saved >= 0
    assert result.payments_saved >= 0
    assert all(r.balance >= 0 for r in rows)

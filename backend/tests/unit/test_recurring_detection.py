# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recurring series detection on ledgers built in the test (no database).

Every row is invented. A few merchant keys are public brand names, as the
dictionary's are; local businesses, people, amounts and dates are made up.
Each ledger is built from a pattern (cadence, amount, jitter) so the expected
answer is known.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

import pytest

from app.domain.recurring import detection
from app.domain.recurring.detection import Candidate, Observed, Pattern
from app.domain.reports.dates import add_months
from app.domain.transactions.models import Cadence

D = Decimal
ACCOUNT = uuid.UUID("00000000-0000-7000-8000-000000000001")
OTHER_ACCOUNT = uuid.UUID("00000000-0000-7000-8000-000000000002")
#: The latest row of the account in most ledgers below.
LATEST = date(2026, 6, 28)


def rows(
    days: Iterable[date],
    amounts: Iterable[Decimal | str] | Decimal | str,
    *,
    key: str | None = "northlight stream",
    text: str | None = None,
    kind: str = "purchase",
    category: str | None = None,
    category_kind: str | None = None,
    hint: Literal["subscription", "bill"] | None = None,
    payee_id: uuid.UUID | None = None,
    account: uuid.UUID = ACCOUNT,
    cadence_hints: frozenset[str] = frozenset(),
    habit_hint: bool = False,
) -> list[Observed]:
    day_list = list(days)
    if isinstance(amounts, (Decimal, str)):
        values = [D(str(amounts))] * len(day_list)
    else:
        values = [D(str(a)) for a in amounts]
    if category is not None and category_kind is None:
        category_kind = "income" if category.startswith("income.") else "expense"
    return [
        Observed(
            id=uuid.uuid4(),
            account_id=account,
            posted_date=d,
            amount=v,
            merchant_key=key,
            text_key=text if text is not None else key,
            kind=kind,
            category_key=category,
            category_kind=category_kind,
            series_hint=hint,
            payee_id=payee_id,
            cadence_hints=cadence_hints,
            habit_hint=habit_hint,
        )
        for d, v in zip(day_list, values, strict=True)
    ]


def monthly(first: date, count: int, step: int = 1) -> list[date]:
    return [add_months(first, step * i) for i in range(count)]


def every(first: date, step: int, count: int) -> list[date]:
    return [first + timedelta(days=step * i) for i in range(count)]


def detect(ledger: list[Observed], on: date = LATEST) -> list[Candidate]:
    """Detection with every account's latest row on ``on``."""
    return detection.detect(ledger, {row.account_id: on for row in ledger})


def only(found: list[Candidate]) -> Candidate:
    assert len(found) == 1, found
    return found[0]


# ---------------------------------------------------------------------------
# What is found, and how sure
# ---------------------------------------------------------------------------


def test_monthly_fixed_at_a_known_merchant_is_an_active_subscription() -> None:
    ledger = rows(
        monthly(date(2025, 7, 5), 12),
        "-15.49",
        key="netflix",
        category="leisure.subscriptions",
        hint="subscription",
    )
    found = only(detect(ledger))
    assert found.merchant_key == "netflix"
    assert (found.kind, found.status) == ("subscription", "active")
    assert found.fixed
    assert found.known
    assert found.pattern is Pattern.MONTHLY
    assert (found.cadence, found.interval_count) == (Cadence.MONTHLY, 1)
    assert found.amount_min == found.amount_max == found.last_amount == D("-15.49")
    assert (found.first_seen, found.last_seen) == (date(2025, 7, 5), date(2026, 6, 5))
    assert found.next_due == date(2026, 7, 5)
    assert found.occurrences == 12
    assert found.regularity == D("1.00")
    assert found.confidence == D("1.00")
    assert not found.inflow
    assert set(found.transaction_ids) == {r.id for r in ledger}


def test_two_charges_of_a_known_merchant_are_enough_and_three_of_an_unknown_one() -> None:
    days = monthly(date(2026, 4, 9), 3)
    known = only(detect(rows(days[:2], "-9.99", key="spotify", hint="subscription"), days[1]))
    # Found on the reduced count, but thin evidence is a suggestion:
    # 0.4 * 1 + 0.3 * 1 + 0.2 * 2 / 6 + 0.1 = 0.8667.
    assert (known.status, known.occurrences, known.confidence) == ("suggested", 2, D("0.87"))

    assert detect(rows(days[:2], "-9.99", key="ferncliff dojo"), days[1]) == []

    unknown = only(detect(rows(days, "-9.99", key="ferncliff dojo", category="health.fitness")))
    # 0.4 * 1 + 0.3 * 1 + 0.2 * 3 / 6 + 0 = 0.80: just enough to count.
    assert (unknown.status, unknown.confidence, unknown.known) == ("active", D("0.80"), False)

    three_known = only(detect(rows(days, "-9.99", key="spotify", hint="subscription")))
    assert (three_known.status, three_known.confidence) == ("active", D("0.90"))


def test_one_annual_charge_of_a_known_merchant_is_suggested() -> None:
    charged = date(2026, 2, 11)
    once = rows([charged], "-119.88", key="dropbox", hint="subscription")
    found = only(detect(once))
    assert found.pattern is Pattern.ANNUAL
    assert (found.cadence, found.interval_count) == (Cadence.YEARLY, 1)
    assert (found.status, found.kind, found.occurrences) == ("suggested", "subscription", 1)
    assert found.next_due == date(2027, 2, 11)
    assert found.confidence < detection.ACTIVE_MIN

    # Not for a merchant nobody knows, nor one the dictionary says bills monthly only,
    # nor a charge so old that a renewal would have shown by now.
    assert detect(rows([charged], "-119.88", key="ferncliff dojo")) == []
    monthly_only = rows(
        [charged],
        "-119.88",
        key="dropbox",
        hint="subscription",
        cadence_hints=frozenset({"monthly"}),
    )
    assert detect(monthly_only) == []
    assert detect(once, charged + timedelta(days=420)) == []
    # A known merchant charged at two unrelated prices was not "seen once".
    twice = once + rows([charged + timedelta(days=9)], "-31.00", key="dropbox", hint="subscription")
    assert detect(twice) == []


def test_two_plans_at_one_merchant_are_two_series() -> None:
    days = monthly(date(2026, 1, 10), 6)
    ledger = rows(days, "-2.99", key="apple com bill", hint="subscription") + rows(
        [d + timedelta(days=3) for d in days], "-10.99", key="apple com bill", hint="subscription"
    )
    found = detect(ledger)
    assert sorted(c.amount_min for c in found) == [D("-10.99"), D("-2.99")]
    assert all(c.pattern is Pattern.MONTHLY and c.occurrences == 6 for c in found)
    assert all((c.kind, c.fixed) == ("subscription", True) for c in found)


def test_a_price_rise_keeps_one_series_at_the_new_price() -> None:
    days = monthly(date(2025, 7, 12), 12)
    ledger = rows(days, ["-15.49"] * 8 + ["-17.99"] * 4, key="netflix", hint="subscription")
    found = only(detect(ledger))
    assert found.occurrences == 12
    assert found.first_seen == date(2025, 7, 12)
    assert (found.amount_min, found.amount_max, found.last_amount) == (
        D("-17.99"),
        D("-17.99"),
        D("-17.99"),
    )
    assert (found.kind, found.fixed, found.status) == ("subscription", True, "active")

    # The first charge at a new price already belongs to the series.
    first = rows(days, ["-15.49"] * 11 + ["-17.99"], key="netflix", hint="subscription")
    assert only(detect(first)).last_amount == D("-17.99")

    # Amounts that come and go are not a price list: an unknown merchant charging
    # anything between 10 and 50 on a monthly rhythm is not a series.
    swinging = [f"-{a}.00" for a in (10, 40, 22, 31, 14, 36, 18, 45, 26, 12, 50, 20)]
    assert detect(rows(days, swinging, key="ferncliff dojo")) == []


def test_two_monthly_and_half_yearly_are_found() -> None:
    water = rows(
        monthly(date(2025, 7, 20), 6, step=2),
        "-104.10",
        key="ashgrove water dist",
        category="utilities.water",
    )
    radio = rows(
        monthly(date(2024, 12, 3), 4, step=6),
        "-68.94",
        key="siriusxm",
        category="leisure.subscriptions",
        hint="subscription",
    )
    found = {c.pattern: c for c in detect(water + radio)}
    assert set(found) == {Pattern.TWO_MONTHLY, Pattern.HALF_YEARLY}
    two = found[Pattern.TWO_MONTHLY]
    assert (two.cadence, two.interval_count, two.kind) == (Cadence.MONTHLY, 2, "bill")
    assert two.next_due == date(2026, 7, 20)
    half = found[Pattern.HALF_YEARLY]
    assert (half.cadence, half.interval_count, half.kind) == (Cadence.MONTHLY, 6, "subscription")
    assert half.next_due == date(2026, 12, 3)
    assert half.status == "active"


def test_weekly_quarterly_and_annual() -> None:
    weekly = rows(
        every(date(2026, 4, 3), 7, 13), "-32.00", key="riverbend swim", category="leisure.hobbies"
    )
    quarterly = rows(
        monthly(date(2025, 1, 15), 6, step=3),
        "-96.45",
        key="maple ridge sewer",
        category="utilities.water",
    )
    annual = rows(
        [date(2024, 9, 2), date(2025, 9, 1)], "-83.99", key="duolingo", hint="subscription"
    )
    found = {c.pattern: c for c in detect(weekly + quarterly + annual)}
    assert set(found) == {Pattern.WEEKLY, Pattern.QUARTERLY, Pattern.ANNUAL}
    assert found[Pattern.WEEKLY].next_due == date(2026, 7, 3)
    assert found[Pattern.QUARTERLY].cadence is Cadence.QUARTERLY
    assert found[Pattern.QUARTERLY].next_due == date(2026, 7, 15)
    assert found[Pattern.ANNUAL].cadence is Cadence.YEARLY
    assert found[Pattern.ANNUAL].next_due == date(2026, 9, 1)
    assert found[Pattern.ANNUAL].status == "active"


def test_irregular_gaps_below_80_percent_are_dropped() -> None:
    def ledger(*late: int) -> list[Observed]:
        # Eleven monthly charges (ten gaps). A charge eleven days late puts the gap
        # before it and the gap after it outside 26 to 35 days.
        days = monthly(date(2025, 8, 4), 11)
        moved = [d + timedelta(days=11 if i in late else 0) for i, d in enumerate(days)]
        return rows(moved, "-20.00", key="ferncliff dojo", category="health.fitness")

    assert only(detect(ledger())).regularity == D("1.00")
    # One late charge: eight of ten gaps are regular, which is just enough.
    kept = only(detect(ledger(4)))
    assert kept.regularity == D("0.80")
    assert kept.confidence == D("0.82")  # 0.4 * 0.8 + 0.3 + 0.2
    # Two late charges: six of ten.
    assert detect(ledger(2, 6)) == []


def test_a_series_is_not_ended_by_the_wall_clock() -> None:
    days = monthly(date(2025, 2, 1), 12)  # last charge 2026-01-01, five months before "today"
    ledger = rows(days, "-11.99", key="disney plus", hint="subscription")
    # The account's statements stop a week after the last charge: still running.
    found = only(detect(ledger, date(2026, 1, 8)))
    assert found.status == "active"
    assert found.next_due == date(2026, 2, 1)
    # Just inside one and a half cycles plus the grace (45 + 5 days), and just outside.
    assert len(detect(ledger, date(2026, 2, 20))) == 1
    assert detect(ledger, date(2026, 2, 21)) == []
    # With rows up to the end of June and no charge since January it did stop.
    assert detect(ledger, LATEST) == []


def test_rows_after_the_account_s_clock_are_not_read() -> None:
    days = monthly(date(2026, 1, 5), 6)
    ledger = rows(days, "-11.99", key="disney plus", hint="subscription")
    found = only(detect(ledger, date(2026, 4, 6)))
    assert (found.occurrences, found.last_seen) == (4, date(2026, 4, 5))
    # An account the clock does not name is read up to its own latest row.
    assert only(detection.detect(ledger, {})).occurrences == 6


# ---------------------------------------------------------------------------
# Habits, extra charges and money that is not spending
# ---------------------------------------------------------------------------


def test_weekly_groceries_are_not_a_series() -> None:
    amounts = ["-120.05", "-87.40", "-141.12", "-96.30", "-133.75", "-78.64", "-110.20", "-99.91"]
    ledger = rows(
        every(date(2026, 5, 2), 7, 8), amounts, key="kroger", category="food.groceries"
    ) + rows(
        # The same shop at the same price every week, but only five times.
        every(date(2026, 5, 20), 7, 5),
        "-54.00",
        key="trader joes",
        category="food.groceries",
    )
    assert detect(ledger) == []


def test_a_coffee_habit_is_not_a_series() -> None:
    days = [date(2026, 3, 2) + timedelta(days=i) for i in range(110) if i % 7 < 5]
    ledger = rows(days, "-4.50", key="starbucks", category="food.coffee")
    assert detect(ledger) == []


def test_a_fixed_weekly_car_wash_in_a_habit_category_is_only_suggested() -> None:
    days = every(date(2026, 3, 7), 7, 16)
    wash = rows(days, "-12.00", key="glide car wash", category="transport.fuel")
    found = only(detect(wash))
    assert found.pattern is Pattern.WEEKLY
    assert found.confidence == D("0.90")  # sure enough to count, and still a habit category
    assert (found.status, found.kind, found.fixed) == ("suggested", "subscription", True)

    # One wash a day late, or a price that moves, and it is an ordinary habit.
    late = [d + timedelta(days=3 if i == 9 else 0) for i, d in enumerate(days)]
    assert detect(rows(late, "-12.00", key="glide car wash", category="transport.fuel")) == []
    prices = [f"-{p}.00" for p in (12, 14, 12, 12, 16, 14, 12, 12, 18, 12, 14, 16, 12, 12, 14, 12)]
    assert detect(rows(days, prices, key="glide car wash", category="transport.fuel")) == []


def test_a_store_that_sells_everything_is_a_habit_while_nobody_filed_it() -> None:
    # About monthly and about the same amount, by chance: enough for a series anywhere else.
    days = monthly(date(2026, 1, 9), 6)
    amounts = ["-41.20", "-42.05", "-41.10", "-42.60", "-41.95", "-42.40"]
    assert len(detect(rows(days, amounts, key="everymart"))) == 1
    assert detect(rows(days, amounts, key="everymart", habit_hint=True)) == []
    # The same amount on the same day six times is worth a look, and no more.
    fixed = rows(days, "-14.99", key="everymart", habit_hint=True)
    assert only(detect(fixed)).status == "suggested"
    # A category a person or the engine gave says what it is instead of the hint.
    filed = rows(days, "-14.99", key="everymart", habit_hint=True, category="leisure.subscriptions")
    assert (only(detect(filed)).status, only(detect(filed)).kind) == ("active", "subscription")


@pytest.mark.parametrize("count", [3, 5, 6, 12])
def test_one_price_every_month_at_a_store_that_sells_everything_is_suggested_at_once(
    count: int,
) -> None:
    # A plan billed through the store (the review's case: a reading plan at 11.99
    # under the store's digital arm) is worth showing as soon as any other
    # series would be, and is a person's to confirm.
    days = monthly(add_months(date(2026, 6, 9), 1 - count), count)
    plan = only(detect(rows(days, "-11.99", key="everymart digital", habit_hint=True)))
    assert (plan.status, plan.kind, plan.fixed) == ("suggested", "other", True)
    assert plan.occurrences >= 3
    # Without the hint it was active from the third charge, as before.
    assert only(detect(rows(days, "-11.99", key="everymart digital"))).status == "active"


def test_about_the_same_price_at_a_store_that_sells_everything_is_still_a_habit() -> None:
    # Three orders a month apart that differ by cents are orders, whatever "fixed" allows.
    days = monthly(date(2026, 4, 9), 3)
    near = rows(days, ["-11.99", "-12.19", "-11.89"], key="everymart", habit_hint=True)
    assert detect(near) == []
    assert len(detect(rows(days, ["-11.99", "-12.19", "-11.89"], key="everymart"))) == 1
    # One charge a few days off, and it is no plan either.
    late = [days[0], days[1] + timedelta(days=6), days[2]]
    assert detect(rows(late, "-11.99", key="everymart", habit_hint=True)) == []
    # In a habit category the rule is the old one: six charges.
    filed = rows(days, "-11.99", key="everymart", habit_hint=True, category="shopping.general")
    assert detect(filed) == []


def test_two_charges_per_cycle_are_dropped() -> None:
    firsts = monthly(date(2025, 11, 3), 8)
    twice = rows(firsts, "-25.00", key="ferncliff dojo", category="health.fitness") + rows(
        [d + timedelta(days=7) for d in firsts],
        "-25.00",
        key="ferncliff dojo",
        category="health.fitness",
    )
    assert detect(twice) == []
    same_day = rows(firsts + firsts, "-25.00", key="ferncliff dojo", category="health.fitness")
    assert detect(same_day) == []
    # One doubled charge in a long history does not undo the series.
    doubled = rows([*firsts, firsts[3]], "-25.00", key="ferncliff dojo", category="health.fitness")
    assert only(detect(doubled)).pattern is Pattern.MONTHLY


def test_two_plans_at_one_price_are_told_apart_by_their_day_of_the_month() -> None:
    firsts = monthly(date(2026, 1, 3), 6)
    ninth = [d + timedelta(days=9) for d in firsts]
    # One known merchant bills two plans at one price, on the 3rd and on the 12th.
    ledger = rows(firsts + ninth, "-15.49", key="apple com bill", hint="subscription")
    found = detect(ledger)
    assert len(found) == 2
    assert all(c.pattern is Pattern.MONTHLY and c.occurrences == 6 for c in found)
    assert all((c.kind, c.status, c.fixed) == ("subscription", "active", True) for c in found)
    assert sorted(c.next_due for c in found) == [date(2026, 7, 3), date(2026, 7, 12)]
    assert not set(found[0].transaction_ids) & set(found[1].transaction_ids)

    # One of the two was cancelled in March: the other is still found.
    kept = only(
        detect(rows(firsts + ninth[:3], "-15.49", key="apple com bill", hint="subscription"))
    )
    assert (kept.pattern, kept.occurrences, kept.next_due) == (Pattern.MONTHLY, 6, date(2026, 7, 3))

    # At a merchant the dictionary does not know, two charges per cycle are a habit
    # (test_two_charges_per_cycle_are_dropped), and so are one price on scattered days
    # at a known one.
    assert detect(rows(firsts + ninth, "-15.49", key="ferncliff dojo")) == []
    scattered = [
        d + timedelta(days=step) for d, step in zip(firsts, (9, 13, 6, 17, 11, 8), strict=True)
    ]
    assert (
        detect(rows(firsts + scattered, "-15.49", key="apple com bill", hint="subscription")) == []
    )


def test_a_fortnightly_reading_of_two_days_of_the_month_is_two_monthly_series() -> None:
    firsts = monthly(date(2026, 1, 3), 6)
    later = [d + timedelta(days=15) for d in firsts]
    # The 3rd and the 18th are 13 to 16 days apart: a fortnightly rhythm on paper,
    # 26 charges a year instead of 24.
    found = detect(rows(firsts + later, "-7.00", key="ferncliff dojo", category="health.fitness"))
    assert len(found) == 2
    assert all(c.pattern is Pattern.MONTHLY and c.occurrences == 6 for c in found)
    assert sorted(c.next_due for c in found) == [date(2026, 7, 3), date(2026, 7, 18)]

    # A charge every fourteen days moves through the month: one fortnightly series.
    fortnight = every(date(2026, 1, 2), 14, 13)
    one = only(detect(rows(fortnight, "-7.00", key="ferncliff dojo", category="health.fitness")))
    assert (one.pattern, one.occurrences) == (Pattern.BIWEEKLY, 13)
    # Too few charges to tell: a February fortnight lands on the same two days twice.
    short = [date(2026, 2, 3), date(2026, 2, 17), date(2026, 3, 3), date(2026, 3, 17)]
    brief = only(detect(rows(short, "-7.00", key="ferncliff dojo"), date(2026, 3, 20)))
    assert brief.pattern is Pattern.BIWEEKLY
    # Pay on the 15th and the 30th stays one income series.
    paydays = [date(2026, month, 15) for month in range(1, 7)] + [
        date(2026, month, 28 if month == 2 else 30) for month in range(1, 6)
    ]
    pay = only(detect(rows(paydays, "2500.00", key="brightwater staffing", kind="payroll")))
    assert (pay.pattern, pay.kind, pay.occurrences) == (Pattern.BIWEEKLY, "income", 11)


def test_a_top_level_category_counts_like_its_children() -> None:
    # Filed under "Food" itself (system key ``food``, no dot): as much a habit as coffee.
    weekly = every(date(2026, 5, 4), 7, 8)
    for category in ("food", "food.coffee", "shopping"):
        habit = only(detect(rows(weekly, "-5.25", key="beanbarn", category=category)))
        assert (habit.status, habit.kind) == ("suggested", "subscription"), category
    # Under "Housing" or "Utilities" themselves a fixed charge is a bill.
    days = monthly(date(2026, 1, 6), 6)
    for category in ("housing", "utilities"):
        bill = only(detect(rows(days, "-61.00", key="hill water", category=category)))
        assert bill.kind == "bill", category
    # A key that only begins with the same letters is neither.
    other = only(detect(rows(weekly, "-5.25", key="beanbarn", category="foodbank")))
    assert (other.status, other.kind) == ("active", "subscription")
    assert only(detect(rows(days, "-61.00", key="hill water", category="housingfair"))).kind == (
        "subscription"
    )


def test_a_variable_utility_is_a_bill() -> None:
    days = monthly(date(2025, 7, 20), 12)
    amounts = [
        "-212.00", "-225.92", "-171.30", "-120.44", "-96.10", "-88.75",
        "-101.36", "-93.02", "-118.90", "-140.57", "-181.15", "-215.48",
    ]  # fmt: skip
    power = rows(days, amounts, key="ashby power", category="utilities.electricity")
    found = only(detect(power))
    assert (found.kind, found.fixed, found.occurrences) == ("bill", False, 12)
    assert (found.amount_min, found.amount_max) == (D("-225.92"), D("-88.75"))
    assert found.last_amount == D("-215.48")
    # A bill's amount is expected to move, so the swing does not count against it.
    assert (found.status, found.confidence) == ("active", D("0.90"))

    # A phone bill that drifts a few per cent is a bill by its amount alone.
    drift = ["-74.91", "-78.40", "-76.02", "-80.15", "-77.30", "-79.66"]
    phone = only(detect(rows(days[6:], drift, key="larkspur wireless")))
    assert (phone.kind, phone.fixed) == ("bill", False)

    # The same swing at a merchant nothing says is a bill is not a series.
    assert detect(rows(days, amounts, key="ashby supply")) == []

    # One bill paid twelve days late, so the next comes close behind it: still the bill.
    late = [d + timedelta(days=12 if i == 5 else 0) for i, d in enumerate(days)]
    paid_late = only(
        detect(rows(late, amounts, key="ashby power", category="utilities.electricity"))
    )
    assert (paid_late.kind, paid_late.occurrences) == ("bill", 12)
    assert paid_late.regularity == D("0.82")
    assert detect(rows(late, amounts, key="ashby supply")) == []


def test_rent_and_a_loan_are_bills_not_subscriptions() -> None:
    days = monthly(date(2025, 9, 1), 10)
    rent = rows(days, "-1850.00", key="hollowbrook apts", kind="ach_debit", category="housing.rent")
    loan = rows(
        [d + timedelta(days=11) for d in days],
        "-412.37",
        key="toyota financial",
        kind="loan_payment",
        category="transfers.loan_payment",
        category_kind="transfer",
    )
    uncategorised_loan = rows(
        [d + timedelta(days=14) for d in days],
        "-203.10",
        key="lakemont lending",
        kind="loan_payment",
    )
    insurer = rows(
        [d + timedelta(days=17) for d in days], "-46.20", key="northwestern mutual", hint="bill"
    )
    found = {c.merchant_key: c for c in detect(rent + loan + uncategorised_loan + insurer)}
    assert set(found) == {
        "hollowbrook apts",
        "toyota financial",
        "lakemont lending",
        "northwestern mutual",
    }
    assert all((c.kind, c.fixed, c.status) == ("bill", True, "active") for c in found.values())


def test_transfers_card_payments_and_cash_are_never_series() -> None:
    days = monthly(date(2025, 9, 2), 10)
    ledger: list[Observed] = []
    for index, kind in enumerate(sorted(detection.EXCLUDED_KINDS)):
        ledger += rows(
            [d + timedelta(days=index) for d in days], "-250.00", key=f"moved {index}", kind=kind
        )
    assert len(detection.EXCLUDED_KINDS) == 7
    # A category that is not spending keeps a row out whatever its descriptor says.
    ledger += rows(
        days,
        "-500.00",
        key="harbor savings",
        category="transfers.savings",
        category_kind="transfer",
    )
    ledger += rows(
        days, "-40.00", key="reimbursed", category="excluded.reimbursable", category_kind="ignore"
    )
    # A fee and interest filed as such, on descriptors nothing recognised.
    ledger += rows(days, "-12.00", key="monthly maintenance", category="financial.bank_fees")
    ledger += rows(days, "-16.91", key="interest charge", category="financial.interest_paid")
    ledger += rows(days, "0", key="zero")
    ledger += rows(days, "-10.00", key=None, text=None)
    assert detect(ledger) == []


def test_a_monthly_p2p_payment_is_a_suggested_bill() -> None:
    days = monthly(date(2025, 7, 1), 12)
    housemate = rows(days, "-600.00", key="zelle", text="zelle payment to dorian vale", kind="p2p")
    # A second person paid through the same service is a second series, not a second
    # charge per cycle of the first.
    cleaner = rows(
        [d + timedelta(days=9) for d in days],
        "-90.00",
        key="zelle",
        text="zelle payment to imre castellan",
        kind="p2p",
    )
    found = detect(housemate + cleaner)
    assert sorted(c.amount_min for c in found) == [D("-600.00"), D("-90.00")]
    for candidate in found:
        assert candidate.confidence == D("0.90")
        assert (candidate.kind, candidate.status) == ("bill", "suggested")
        # The service is not who was paid: the series carries no merchant key.
        assert candidate.merchant_key is None
        assert (candidate.text_key or "").startswith("zelle payment to")
    # Without any text there is no telling the people apart.
    assert detect(rows(days, "-600.00", key="zelle", text="", kind="p2p")) == []


def test_salary_is_income() -> None:
    days = every(date(2026, 1, 2), 14, 13)
    pay = rows(
        days, "2500.00", key="brightwater staffing", kind="payroll", category="income.salary"
    )
    found = only(detect(pay))
    assert found.pattern is Pattern.BIWEEKLY
    assert (found.cadence, found.interval_count) == (Cadence.WEEKLY, 2)
    assert found.inflow
    assert (found.kind, found.status) == ("income", "active")
    assert (found.last_seen, found.next_due) == (date(2026, 6, 19), date(2026, 7, 3))

    # Pay that changes with the hours is still one income series.
    hours = [f"{1800 + 137 * (i % 5)}.00" for i in range(13)]
    varying = only(detect(rows(days, hours, key="brightwater staffing", kind="payroll")))
    assert (varying.kind, varying.fixed, varying.occurrences) == ("income", False, 13)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def test_history_split_between_rows_with_and_without_a_payee_is_one_series() -> None:
    payee = uuid.uuid4()
    days = monthly(date(2025, 7, 9), 12)
    ledger = rows(days[:5], "-22.99", key="adobe", hint="subscription") + rows(
        days[5:], "-22.99", key="adobe", hint="subscription", payee_id=payee
    )
    found = only(detect(ledger))
    assert found.occurrences == 12
    assert found.payee_id == payee  # the most common payee of its rows
    assert only(detect(ledger[:5], days[4])).payee_id is None


def test_rows_without_a_merchant_key_meet_on_their_text() -> None:
    days = monthly(date(2026, 1, 6), 6)
    ledger = rows(days, "-30.00", key=None, text="quarry lane studio")
    found = only(detect(ledger))
    assert (found.merchant_key, found.text_key) == (None, "quarry lane studio")
    assert found.kind == "other"  # fixed, but nothing says what it is


def test_groups_split_by_account_and_direction() -> None:
    days = monthly(date(2026, 1, 3), 6)
    ledger = (
        rows(days, "-10.00", key="same name")
        + rows(days, "-10.00", key="same name", account=OTHER_ACCOUNT)
        + rows(days, "10.00", key="same name")
    )
    found = detect(ledger)
    assert sorted((c.account_id == ACCOUNT, c.inflow) for c in found) == [
        (False, False),
        (True, False),
        (True, True),
    ]


def test_category_and_label_come_from_the_rows() -> None:
    food, other = uuid.uuid4(), uuid.uuid4()
    days = monthly(date(2026, 1, 3), 6)
    ledger = [
        Observed(
            id=uuid.uuid4(),
            account_id=ACCOUNT,
            posted_date=day,
            amount=D("-10.00"),
            merchant_key="meal kit",
            text_key="meal kit",
            kind="purchase",
            category_key="leisure.hobbies",
            category_kind="expense",
            category_id=other if index == 4 else food,
            label=f"MEAL KIT #{index}",
        )
        for index, day in enumerate(days)
    ]
    found = only(detect(ledger))
    assert found.category_id == food
    assert found.label == "MEAL KIT #5"


def test_normalize_payee() -> None:
    assert detection.normalize_payee("  ACME*Store #123 ") == "acme store"
    assert detection.normalize_payee(None) == ""
    assert detection.normalize_payee("1234") == ""


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "cadence", "interval", "per_month", "per_year"),
    [
        ("10.00", Cadence.MONTHLY, 1, "10.00", "120.00"),
        ("120.00", Cadence.YEARLY, 1, "10.00", "120.00"),
        ("5.00", Cadence.WEEKLY, 1, "21.67", "260.00"),
        ("2500.00", Cadence.WEEKLY, 2, "5416.67", "65000.00"),
        ("96.45", Cadence.QUARTERLY, 1, "32.15", "385.80"),
        ("104.10", Cadence.MONTHLY, 2, "52.05", "624.60"),
        ("68.94", Cadence.MONTHLY, 6, "11.49", "137.88"),
        ("1.00", Cadence.DAILY, 1, "30.42", "365.00"),
        ("-15.49", Cadence.MONTHLY, 1, "-15.49", "-185.88"),
    ],
)
def test_costs(amount: str, cadence: Cadence, interval: int, per_month: str, per_year: str) -> None:
    cent = D("0.01")
    assert detection.monthly_cost(D(amount), cadence, interval).quantize(cent) == D(per_month)
    assert detection.yearly_cost(D(amount), cadence, interval).quantize(cent) == D(per_year)
    assert isinstance(detection.monthly_cost(D(amount), cadence, interval), Decimal)

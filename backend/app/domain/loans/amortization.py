# SPDX-License-Identifier: AGPL-3.0-or-later
"""Amortisation schedules, payoff projections and extra-payment simulation (pure).

No database, no floats: every figure is a ``Decimal`` computed in the money
context (``app.core.money.calc_context``, 34 digits, half-even) and rounded
once per period to the currency's minor unit.

Conventions (DATA_MODEL.md "Loans", API.md ``/loans``):

* **Due dates**: payment ``k`` (1-based) is due ``k`` months after the
  origination date, on ``payment_day`` (29-31 fall back to the month's last
  day). Interest accrues monthly at the nominal rate / 12; odd first periods
  are not prorated.
* **Rate**: the rate of period ``k`` is the rate in force on the period's
  first day (the previous due date, or the origination date for ``k = 1``):
  the latest rate change on or before it, else the loan's rate.
* **Payment** (principal and interest): the loan's ``payment_amount``; when
  none is given, the level payment ``P * r / (1 - (1 + r) ** -n)`` (``P / n``
  at a zero rate), rounded to the minor unit. A **variable** loan is
  re-amortised whenever its rate changes: the new payment is the level
  payment of the balance over the months left. A **fixed** loan keeps its
  payment.
* **Rounding**: interest is rounded to the minor unit each period; the
  **final payment** takes whatever balance is left (it can differ from the
  others by the rounding), so the principal parts always add up to the
  starting balance exactly. The last period of the term pays the rest even
  when the payment was too small to get there (a balloon).
* **Extras**: a one-off extra is added to the principal of the first
  payment due on or after its date; a recurring extra to every payment due
  in its window. An extra never takes the balance below zero.
* **Escrow** is a constant amount added to each payment's total; it does not
  touch the balance.

A payment that does not cover a period's interest (negative amortisation)
is refused (:class:`ScheduleError`): the schedule would grow the debt.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext
from typing import Final

from app.core.money import calc_context, round_to_minor, sum_amounts

#: Longest schedule computed (100 years of monthly payments).
MAX_PERIODS: Final = 1200
ZERO: Final = Decimal(0)


class ScheduleError(ValueError):
    """The terms do not give a schedule (``code`` names why)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RateChange:
    effective_date: date
    rate: Decimal


@dataclass(frozen=True)
class Terms:
    """What a schedule is computed from (one loan, in its currency)."""

    principal: Decimal
    annual_rate: Decimal
    term_months: int
    origination_date: date
    payment_day: int
    currency: str
    #: Principal and interest per period; ``None``: the level payment.
    payment: Decimal | None = None
    variable: bool = False
    escrow: Decimal = ZERO
    rate_changes: tuple[RateChange, ...] = ()


@dataclass(frozen=True)
class OneOffExtra:
    on: date
    amount: Decimal


@dataclass(frozen=True)
class RecurringExtra:
    amount: Decimal
    start: date
    end: date | None = None


@dataclass(frozen=True)
class Extras:
    one_off: tuple[OneOffExtra, ...] = ()
    recurring: tuple[RecurringExtra, ...] = ()

    def on(self, period_start: date | None, due: date) -> Decimal:
        """Extra principal paid with the payment due ``due`` (after ``period_start``)."""
        total = sum_amounts(
            e.amount
            for e in self.one_off
            if e.on <= due and (period_start is None or e.on > period_start)
        )
        total += sum_amounts(
            r.amount for r in self.recurring if r.start <= due and (r.end is None or due <= r.end)
        )
        return total


NO_EXTRAS: Final = Extras()


@dataclass(frozen=True)
class Start:
    """Where a projection starts: ``balance`` owed before payment ``number``."""

    number: int
    balance: Decimal


@dataclass(frozen=True)
class Row:
    number: int
    due_date: date
    rate: Decimal
    #: Scheduled principal and interest (``principal + interest``).
    payment: Decimal
    principal: Decimal
    interest: Decimal
    extra: Decimal
    escrow: Decimal
    #: Everything paid: ``payment + extra + escrow``.
    total: Decimal
    balance: Decimal


@dataclass(frozen=True)
class Schedule:
    rows: tuple[Row, ...]
    currency: str
    start_balance: Decimal
    total_principal: Decimal = field(default=ZERO)
    total_interest: Decimal = field(default=ZERO)
    total_extra: Decimal = field(default=ZERO)
    total_escrow: Decimal = field(default=ZERO)
    total_paid: Decimal = field(default=ZERO)

    @property
    def payoff_date(self) -> date | None:
        return self.rows[-1].due_date if self.rows else None


# ---------------------------------------------------------------------------
# Dates and rates
# ---------------------------------------------------------------------------


def add_months(start: date, months: int, day: int) -> date:
    """``start`` moved ``months`` months on, on ``day`` (clamped to the month's end)."""
    index = start.year * 12 + start.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def due_date(terms: Terms, number: int) -> date:
    """Due date of payment ``number`` (``0`` is the origination date)."""
    if number <= 0:
        return terms.origination_date
    return add_months(terms.origination_date, number, terms.payment_day)


def rate_in_force(terms: Terms, on: date) -> Decimal:
    rate = terms.annual_rate
    for change in sorted(terms.rate_changes, key=lambda c: c.effective_date):
        if change.effective_date <= on:
            rate = change.rate
    return rate


def level_payment(balance: Decimal, annual_rate: Decimal, months: int, currency: str) -> Decimal:
    """The constant payment that repays ``balance`` in ``months`` (rounded to the minor unit)."""
    if months <= 0:
        raise ScheduleError("invalid-term", "The term must be at least one month.")
    with localcontext(calc_context()):
        if annual_rate == 0:
            value = balance / months
        else:
            r = annual_rate / 12
            value = balance * r / (1 - (1 + r) ** -months)
    return round_to_minor(value, currency)


def period_interest(balance: Decimal, annual_rate: Decimal, currency: str) -> Decimal:
    with localcontext(calc_context()):
        value = balance * annual_rate / 12
    return round_to_minor(value, currency)


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


def scheduled_payment(terms: Terms) -> Decimal:
    """The first period's principal and interest (given, or the level payment)."""
    if terms.payment is not None and terms.payment > 0:
        return terms.payment
    return level_payment(terms.principal, terms.annual_rate, terms.term_months, terms.currency)


def schedule(
    terms: Terms,
    *,
    extras: Extras = NO_EXTRAS,
    start: Start | None = None,
    payment: Decimal | None = None,
) -> Schedule:
    """The amortisation schedule of ``terms`` (module doc).

    Without ``start`` it runs from origination over the original principal.
    With it, it projects ``start.balance`` from payment ``start.number`` on,
    paying ``payment`` (default: the payment the original schedule has for
    that period, see :func:`payment_at`).
    """
    if terms.term_months <= 0 or terms.term_months > MAX_PERIODS:
        raise ScheduleError("invalid-term", "The term must be 1 to 1200 months.")
    first = 1 if start is None else max(1, start.number)
    balance = terms.principal if start is None else start.balance
    if balance < 0:
        raise ScheduleError("invalid-balance", "The balance owed cannot be negative.")
    if start is None:
        current = scheduled_payment(terms)
    elif payment is not None:
        current = payment
    else:
        current = payment_at(terms, first)
    last = max(terms.term_months, first)
    # A variable loan re-amortises when its rate changes, including at the
    # first projected period (the rate of the period before it differs).
    previous_rate: Decimal | None = (
        rate_in_force(terms, due_date(terms, first - 2)) if first > 1 else None
    )
    escrow = terms.escrow
    rows: list[Row] = []
    number = first
    while balance > 0 and number <= last and len(rows) < MAX_PERIODS:
        period_start = due_date(terms, number - 1)
        due = due_date(terms, number)
        rate = rate_in_force(terms, period_start)
        if terms.variable and previous_rate is not None and rate != previous_rate:
            current = level_payment(balance, rate, last - number + 1, terms.currency)
        previous_rate = rate
        interest = period_interest(balance, rate, terms.currency)
        if number == last or balance + interest <= current:
            principal = balance
            extra = ZERO
        else:
            principal = current - interest
            if principal < 0:
                raise ScheduleError(
                    "loan-payment-too-low",
                    f"A payment of {current} does not cover the interest of {interest} "
                    f"due {due.isoformat()}.",
                )
            # The first payment also takes one-off extras dated before it.
            wanted = extras.on(period_start if number > first else None, due)
            extra = min(wanted, balance - principal) if wanted > 0 else ZERO
        balance = balance - principal - extra
        pi = principal + interest
        rows.append(
            Row(
                number=number,
                due_date=due,
                rate=rate,
                payment=pi,
                principal=principal,
                interest=interest,
                extra=extra,
                escrow=escrow,
                total=pi + extra + escrow,
                balance=balance,
            )
        )
        number += 1
    return Schedule(
        rows=tuple(rows),
        currency=terms.currency,
        start_balance=terms.principal if start is None else start.balance,
        total_principal=sum_amounts(r.principal for r in rows),
        total_interest=sum_amounts(r.interest for r in rows),
        total_extra=sum_amounts(r.extra for r in rows),
        total_escrow=sum_amounts(r.escrow for r in rows),
        total_paid=sum_amounts(r.total for r in rows),
    )


def payment_at(terms: Terms, number: int) -> Decimal:
    """The scheduled principal-and-interest payment of period ``number``.

    For a fixed loan the loan's payment; for a variable one the payment of
    the last re-amortisation on or before that period in the original
    schedule (the level payment before the first rate change).
    """
    base = scheduled_payment(terms)
    if not terms.variable or number <= 1:
        return base
    original = schedule(terms)
    # The final row pays the rest, so it says nothing about the level payment.
    regular = [r for r in original.rows[:-1] if r.number <= number]
    return regular[-1].payment if regular else base


@dataclass(frozen=True)
class Projection:
    """A projection from today: what is left and when it ends."""

    schedule: Schedule
    next_due_date: date | None
    payoff_date: date | None
    remaining_payments: int
    remaining_interest: Decimal


def project(terms: Terms, start: Start, *, extras: Extras = NO_EXTRAS) -> Projection:
    plan = schedule(terms, extras=extras, start=start)
    return Projection(
        schedule=plan,
        next_due_date=plan.rows[0].due_date if plan.rows else None,
        payoff_date=plan.payoff_date,
        remaining_payments=len(plan.rows),
        remaining_interest=plan.total_interest,
    )


@dataclass(frozen=True)
class Comparison:
    baseline: Projection
    scenario: Projection
    interest_saved: Decimal
    payments_saved: int


def simulate(terms: Terms, start: Start, extras: Extras) -> Comparison:
    """The projection without and with ``extras`` (POST /loans/{id}/simulate)."""
    baseline = project(terms, start)
    scenario = project(terms, start, extras=extras)
    return Comparison(
        baseline=baseline,
        scenario=scenario,
        interest_saved=baseline.remaining_interest - scenario.remaining_interest,
        payments_saved=baseline.remaining_payments - scenario.remaining_payments,
    )


def first_due_after(terms: Terms, day: date) -> int:
    """The number of the first payment due strictly after ``day`` (at least 1)."""
    if day < terms.origination_date:
        return 1
    months = (day.year - terms.origination_date.year) * 12 + (
        day.month - terms.origination_date.month
    )
    number = max(1, months)
    while due_date(terms, number) <= day:
        number += 1
    while number > 1 and due_date(terms, number - 1) > day:
        number -= 1
    return number


def nearest_due_number(terms: Terms, day: date) -> int:
    """The payment whose due date is nearest ``day`` (a payment made early or late)."""
    after = first_due_after(terms, day)
    if after > 1 and (day - due_date(terms, after - 1)) <= (due_date(terms, after) - day):
        return after - 1
    return after

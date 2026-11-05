# SPDX-License-Identifier: AGPL-3.0-or-later
"""Budget periods, rollover, forecast and the budget report (``GET /budgets/{id}/report``).

**Periods.** A ``monthly`` budget starts on the first day of a month and
its periods are calendar months; an ``annual`` budget starts on the first
day of a month and its periods are twelve months from there (a fiscal year
may start in April); a ``custom`` budget is one period ``start_date ..
end_date``. ``?period=`` names any day (``YYYY-MM-DD``) or month
(``YYYY-MM``, its first day) and selects the period containing it; before
the budget starts (or after a custom budget ends) is ``422
period-out-of-range``.

**Actual.** The ledger lines of the period (``app.domain.reports.ledger``:
visible accounts only, splits instead of split transactions, no transfers,
no ``pending`` review rows unless asked) converted into the budget's
currency with the rate of each posting day. A line on category ``C``
counts ``C`` and its subcategories, except a subcategory that has its own
line (the nearest line wins). Expense lines show money out as positive (a
refund reduces it); income lines money in. Spending in expense categories
without a line is listed as ``unbudgeted``.

**Rollover** (``rollover = true``, monthly and annual, expense lines
only): what is left of a period (``available - actual``, negative when
overspent) is carried into the next one; ``available = budgeted +
carried_over``. The carry starts with the budget's first period, looking
back at most :data:`MAX_CARRY_MONTHS` months, and uses today's line
amounts for earlier periods too (lines are not versioned).

**Forecast** (the expected ``actual`` at the end of the period):

* a past period: its actual;
* the current period: ``actual + variable * remaining_days / elapsed_days
  + scheduled``, where ``variable`` is the actual without lines that match
  an active scheduled transaction (same account, payee and category; those
  are expected again through ``scheduled`` instead of being extrapolated),
  ``elapsed_days`` counts the period's days up to and including today and
  ``remaining_days`` the days after today;
* a future period: ``actual + scheduled``;

``scheduled`` adds the occurrences of the active scheduled transactions
(visible accounts, a category under one of the lines) due after today (or
from the period start for a future period) up to the period end, each at
the middle of its ``amount_min .. amount_max`` range, converted with the
latest rate on or before its due date. All arithmetic is ``Decimal``; the
linear projection is rounded once, half-even, to 6 decimals.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.principal import Principal
from app.core.errors import ProblemError
from app.core.money import Money, calc_context, sum_amounts, to_storage
from app.domain.accounts.visibility import visible_account_filter
from app.domain.budgets.models import Budget, BudgetLine, BudgetPeriod
from app.domain.budgets.schemas import (
    BudgetReportLine,
    BudgetReportOut,
    BudgetTotals,
    PeriodOut,
    UnbudgetedOut,
)
from app.domain.fx.service import FxConverter
from app.domain.reports.dates import add_months, days_inclusive, month_index
from app.domain.reports.ledger import (
    EXPENSE,
    ZERO,
    Accumulator,
    CategoryTree,
    LedgerScope,
    aggregate,
    ledger_lines,
)
from app.domain.transactions.models import Cadence, CategoryKind, ScheduledTransaction

#: How far rollover looks back (ten years of monthly periods, ten annual ones).
MAX_CARRY_MONTHS: Final = 120
#: Most occurrences of one schedule counted in one period (a daily one: 366).
MAX_OCCURRENCES: Final = 400
#: Latest year a period may fall in (keeps date arithmetic in range).
MAX_YEAR: Final = 9000
_TWO: Final = Decimal(2)

_STEP_DAYS: Final[Mapping[Cadence, int]] = {Cadence.DAILY: 1, Cadence.WEEKLY: 7}
_STEP_MONTHS: Final[Mapping[Cadence, int]] = {
    Cadence.MONTHLY: 1,
    Cadence.QUARTERLY: 3,
    Cadence.YEARLY: 12,
}


@dataclass(frozen=True)
class Period:
    index: int
    start: date
    end: date

    @property
    def days(self) -> int:
        return days_inclusive(self.start, self.end)


def period_out_of_range(detail: str) -> ProblemError:
    return ProblemError(422, "period-out-of-range", "Period out of range", detail)


def period_months(period: BudgetPeriod) -> int:
    return 12 if period is BudgetPeriod.ANNUAL else 1


def period_at(period: BudgetPeriod, start_date: date, end_date: date | None, index: int) -> Period:
    """The ``index``-th period (0 = the first) of a budget."""
    if period is BudgetPeriod.CUSTOM:
        if index != 0 or end_date is None:
            raise ValueError("a custom budget has exactly one period")
        return Period(0, start_date, end_date)
    months = period_months(period)
    start = add_months(start_date, index * months)
    return Period(index, start, add_months(start_date, (index + 1) * months) - timedelta(days=1))


def period_containing(
    period: BudgetPeriod, start_date: date, end_date: date | None, day: date
) -> Period:
    """The period that contains ``day`` (``422 period-out-of-range`` if none)."""
    if day < start_date:
        raise period_out_of_range("The budget starts after this date.")
    if day.year > MAX_YEAR:
        raise period_out_of_range(f"Periods after the year {MAX_YEAR} are not supported.")
    if period is BudgetPeriod.CUSTOM:
        if end_date is None or day > end_date:
            raise period_out_of_range("The budget ends before this date.")
        return period_at(period, start_date, end_date, 0)
    index = (month_index(day) - month_index(start_date)) // period_months(period)
    return period_at(period, start_date, end_date, index)


def bucket_of(budget: Budget, day: date) -> int:
    """Index of the period a posting month belongs to (custom budgets: always 0)."""
    if budget.period is BudgetPeriod.CUSTOM:
        return 0
    return (month_index(day) - month_index(budget.start_date)) // period_months(budget.period)


def occurrences(
    next_due: date, cadence: Cadence, interval: int, start: date, end: date
) -> list[date]:
    """Due dates of a schedule in ``start .. end`` (at most :data:`MAX_OCCURRENCES`).

    Steps of ``interval`` days, weeks, months, quarters or years from
    ``next_due``; month steps are anchored at ``next_due`` (a schedule due
    on the 31st falls on the last day of shorter months).
    """
    if end < start or end < next_due:
        return []
    found: list[date] = []
    if cadence in _STEP_DAYS:
        step = _STEP_DAYS[cadence] * interval
        k = max(0, -(-(start - next_due).days // step))
        current = next_due + timedelta(days=k * step)
        while current <= end and len(found) < MAX_OCCURRENCES:
            found.append(current)
            current += timedelta(days=step)
        return found
    step_months = _STEP_MONTHS[cadence] * interval
    k = max(0, (month_index(start) - month_index(next_due)) // step_months - 1)
    while len(found) < MAX_OCCURRENCES:
        current = add_months(next_due, k * step_months)
        if current > end:
            break
        if current >= start:
            found.append(current)
        k += 1
    return found


def linear_projection(variable: Decimal, elapsed_days: int, remaining_days: int) -> Decimal:
    """``variable * remaining / elapsed``, rounded half-even to 6 decimals."""
    if elapsed_days <= 0 or remaining_days <= 0:
        return ZERO
    with localcontext(calc_context()):
        value = variable * remaining_days / elapsed_days
    return to_storage(value)


def rollover_carry(budgeted: Decimal, actuals: Sequence[Decimal]) -> Decimal:
    """What reaches the period after ``actuals`` (one actual per earlier period)."""
    carry = ZERO
    for actual in actuals:
        carry = budgeted + carry - actual
    return carry


def midpoint(low: Decimal, high: Decimal) -> Decimal:
    with localcontext(calc_context()):
        value = (low + high) / _TWO
    return to_storage(value)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Line:
    line: BudgetLine
    kind: CategoryKind | None

    @property
    def sign(self) -> int:
        """Income lines count money in; every other line money out."""
        return 1 if self.kind is CategoryKind.INCOME else -1

    @property
    def carries(self) -> bool:
        return self.kind is not CategoryKind.INCOME


def _period_state(period: Period, today: date) -> str:
    if period.end < today:
        return "past"
    if period.start > today:
        return "future"
    return "current"


def _schedules(
    db: Session, principal: Principal, household_id: uuid.UUID
) -> list[ScheduledTransaction]:
    return list(
        db.execute(
            select(ScheduledTransaction)
            .where(
                ScheduledTransaction.household_id == household_id,
                ScheduledTransaction.active.is_(True),
                ScheduledTransaction.category_id.is_not(None),
                visible_account_filter(principal, ScheduledTransaction.account_id),
            )
            .order_by(ScheduledTransaction.id)
        ).scalars()
    )


def build_report(
    db: Session,
    principal: Principal,
    budget: Budget,
    lines: Iterable[BudgetLine],
    *,
    day: date,
    today: date,
    include_pending: bool,
) -> BudgetReportOut:
    """The report of the period containing ``day`` (module doc)."""
    period = period_containing(budget.period, budget.start_date, budget.end_date, day)
    state = _period_state(period, today)
    tree = CategoryTree.load(db, budget.household_id)
    by_category: dict[uuid.UUID, _Line] = {}
    for line in lines:
        node = tree.nodes.get(line.category_id)
        by_category[line.category_id] = _Line(line, node.kind if node else None)

    carry_periods = 0
    if budget.rollover and budget.period is not BudgetPeriod.CUSTOM:
        carry_periods = min(period.index, MAX_CARRY_MONTHS // period_months(budget.period))
    first = period_at(
        budget.period, budget.start_date, budget.end_date, period.index - carry_periods
    )
    scope = LedgerScope(
        household_id=budget.household_id,
        date_from=first.start,
        date_to=period.end,
        currency=budget.currency,
        include_pending=include_pending,
    )
    recurring_from = period.start if state == "current" else None
    sums = aggregate(
        db,
        ledger_lines(principal, scope, recurring_from=recurring_from),
        ["month", "category_id", "flow"],
    )
    acc = Accumulator(FxConverter(db, budget.household_id, budget.currency, until=period.end))
    actual = Accumulator(acc.converter)  # (period index, line category) -> actual
    recurring = Accumulator(acc.converter)  # line category -> recurring actual (current)
    unbudgeted = Accumulator(acc.converter)  # category -> spending without a line
    for row in sums:
        month, category_id, flow = row.key
        bucket = bucket_of(budget, month)
        owner = tree.nearest(category_id, by_category)
        parts = acc.convert_parts(
            (row.total, row.recurring), row.currency, row.fx_day, missing=row.total
        )
        if parts is None:
            continue
        total, matched = parts
        if owner is not None:
            sign = by_category[owner].sign
            actual.put((bucket, owner), total * sign)
            if bucket == period.index:
                recurring.put(owner, matched * sign)
        elif flow == EXPENSE and bucket == period.index:
            unbudgeted.put(category_id, -total)

    scheduled = _scheduled(
        db, principal, budget, tree, by_category, acc, period=period, state=state, today=today
    )
    elapsed = (today - period.start).days + 1 if state == "current" else 0
    remaining_days = (period.end - today).days if state == "current" else 0
    currency = budget.currency

    report_lines: list[BudgetReportLine] = []
    totals: dict[bool, list[tuple[Decimal, ...]]] = {True: [], False: []}
    for category_id, entry in by_category.items():
        budgeted = entry.line.amount
        carried = ZERO
        if carry_periods and entry.carries:
            history = [
                actual.get((i, category_id))
                for i in range(period.index - carry_periods, period.index)
            ]
            carried = rollover_carry(budgeted, history)
        available = budgeted + carried
        spent = actual.get((period.index, category_id))
        forecast = spent
        if state == "current":
            variable = spent - recurring.get(category_id)
            forecast = spent + linear_projection(variable, elapsed, remaining_days)
        if state != "past":
            forecast += scheduled.get(category_id)
        values = (budgeted, carried, available, spent, available - spent, forecast)
        totals[entry.kind is CategoryKind.INCOME].append(values)
        report_lines.append(
            BudgetReportLine(
                category_id=category_id,
                name=tree.name(category_id),
                kind=entry.kind,
                budgeted=Money.of(budgeted, currency),
                carried_over=Money.of(carried, currency),
                available=Money.of(available, currency),
                actual=Money.of(spent, currency),
                remaining=Money.of(available - spent, currency),
                forecast=Money.of(forecast, currency),
            )
        )
    report_lines.sort(key=lambda r: (r.kind is CategoryKind.INCOME, r.name or "", r.category_id))

    unbudgeted_items = sorted(
        (
            UnbudgetedOut(
                category_id=_uuid(key), name=tree.name(_uuid(key)), actual=Money.of(value, currency)
            )
            for key, value in unbudgeted.values.items()
        ),
        key=lambda u: (-u.actual.as_decimal(), u.name or "", str(u.category_id or "")),
    )
    # Every accumulator shares the converter, so missing rates are gathered once.
    for extra in (actual, recurring, unbudgeted, scheduled):
        for code, value in extra.unconverted.items():
            acc.unconverted[code] = sum_amounts((acc.unconverted.get(code, ZERO), value))
    return BudgetReportOut(
        budget_id=budget.id,
        name=budget.name,
        currency=currency,
        as_of=today,
        period=PeriodOut(start=period.start, end=period.end, state=state),
        include_pending=include_pending,
        lines=report_lines,
        unbudgeted=unbudgeted_items,
        unbudgeted_total=Money.of(sum_amounts(unbudgeted.values.values()), currency),
        expense_totals=_totals(totals[False], currency),
        income_totals=_totals(totals[True], currency),
        unconverted=acc.unconverted_out(),
    )


def _uuid(value: object) -> uuid.UUID | None:
    return value if isinstance(value, uuid.UUID) else None


def _totals(rows: Sequence[tuple[Decimal, ...]], currency: str) -> BudgetTotals:
    sums = [sum_amounts(row[i] for row in rows) for i in range(6)]
    return BudgetTotals(
        budgeted=Money.of(sums[0], currency),
        carried_over=Money.of(sums[1], currency),
        available=Money.of(sums[2], currency),
        actual=Money.of(sums[3], currency),
        remaining=Money.of(sums[4], currency),
        forecast=Money.of(sums[5], currency),
    )


def _scheduled(
    db: Session,
    principal: Principal,
    budget: Budget,
    tree: CategoryTree,
    by_category: Mapping[uuid.UUID, _Line],
    acc: Accumulator,
    *,
    period: Period,
    state: str,
    today: date,
) -> Accumulator:
    """Expected scheduled amounts per line category for the rest of the period."""
    result = Accumulator(acc.converter)
    if state == "past" or not by_category:
        return result
    window_start = period.start if state == "future" else today + timedelta(days=1)
    if window_start > period.end:
        return result
    for schedule in _schedules(db, principal, budget.household_id):
        owner = tree.nearest(schedule.category_id, by_category)
        if owner is None:
            continue
        amount = midpoint(schedule.amount_min, schedule.amount_max) * by_category[owner].sign
        for due in occurrences(
            schedule.next_due, schedule.cadence, schedule.interval_count, window_start, period.end
        ):
            result.add(owner, amount, schedule.currency, due)
    return result

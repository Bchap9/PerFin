# SPDX-License-Identifier: AGPL-3.0-or-later
"""Spending, income, trend and cash-flow reports (API.md ``/reports``).

All reports read the ledger lines of ``app.domain.reports.ledger`` (splits
instead of split transactions; visible accounts only; no transfers; no
``pending`` review rows unless asked) and answer in the household's base
currency, converting other currencies with the rate of each posting day.

* **Spending** (``flow=spending``): lines of expense categories plus
  uncategorised money out, shown as positive money out (a refund in an
  expense category reduces it). **Income**: income categories plus
  uncategorised money in.
* ``group_by=category`` rolls categories up to ``depth`` (1 = top level, at
  most 3); ``payee`` and ``account`` group by those ids (``null`` key: no
  payee); ``month`` lists every month of the range, empty ones as zero.
* **Trends**: a month's amount per category against the month before
  (month-on-month) and the same month a year earlier (year-on-year), plus
  the monthly series of the last ``months`` months.
* **Cash flow**: per month the sum of the positive lines (in), of the
  negative lines (out, shown positive) and their difference (net); net over
  a range equals income minus spending.

Bounds (SECURITY.md F8): a range is at most 3660 days, a trend at most 60
months, at most 50 account filters; aggregation happens in SQL with bound
parameters, so a response holds one row per group, never per transaction.

Who may read (router: any household role, token scope
``transactions:read``): everyone, restricted to the accounts they may see
(``app.domain.accounts.visibility``); naming an account they cannot see is
``404 account-not-found``.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Hashable, Sequence
from datetime import date
from decimal import Decimal, localcontext
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.money import Money, calc_context, quantize, sum_amounts
from app.domain.accounts.models import Account
from app.domain.accounts.visibility import require_visible_accounts
from app.domain.fx.service import FxConverter, base_currency
from app.domain.reports.dates import (
    add_months,
    check_range,
    iter_months,
    month_end,
    month_key,
    month_start,
)
from app.domain.reports.ledger import (
    EXPENSE,
    INCOME,
    ZERO,
    Accumulator,
    CategoryTree,
    LedgerScope,
    Sum,
    aggregate,
    ledger_lines,
)
from app.domain.reports.schemas import (
    DEFAULT_CASHFLOW_MONTHS,
    CashflowMonth,
    CashflowReport,
    Flow,
    FlowReport,
    GroupBy,
    ReportItem,
    TrendPoint,
    TrendReport,
    TrendRow,
)
from app.domain.transactions.models import Payee
from app.domain.transactions.service import include_pending_in_reports

_FLOW_LINES: Final = {Flow.SPENDING: EXPENSE, Flow.INCOME: INCOME}
_HUNDRED: Final = Decimal(100)


def today() -> date:
    """The date reports are relative to (UTC, as balances)."""
    return clock.utcnow().date()


def household_for_reports(principal: Principal) -> uuid.UUID:
    """The active household; ``403`` without a household role."""
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, HouseholdRole.VIEWER):  # pragma: no cover - any role
        raise forbidden()
    return principal.household_id


def resolve_include_pending(db: Session, household_id: uuid.UUID, requested: bool | None) -> bool:
    """An explicit request wins; otherwise the household setting (default ``False``)."""
    if requested is not None:
        return requested
    return include_pending_in_reports(db, household_id)


def make_scope(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    *,
    date_from: date,
    date_to: date,
    currency: str,
    account_ids: Collection[uuid.UUID] | None,
    include_pending: bool,
) -> LedgerScope:
    """A checked :class:`LedgerScope` (range bounds, visible account filters)."""
    check_range(date_from, date_to)
    accounts: tuple[uuid.UUID, ...] | None = None
    if account_ids:
        require_visible_accounts(db, principal, account_ids)
        accounts = tuple(sorted(set(account_ids)))
    return LedgerScope(
        household_id=household_id,
        date_from=date_from,
        date_to=date_to,
        currency=currency,
        include_pending=include_pending,
        account_ids=accounts,
    )


def percent(change: Decimal, base: Decimal) -> str | None:
    """``change / base`` in percent with two decimals (half-even); ``None`` when ``base`` is 0."""
    if base == 0:
        return None
    with localcontext(calc_context()):
        value = change * _HUNDRED / abs(base)
    return f"{quantize(value, 2):f}"


# ---------------------------------------------------------------------------
# Spending and income
# ---------------------------------------------------------------------------


def _names(
    db: Session, household_id: uuid.UUID, group_by: GroupBy, ids: Collection[Hashable]
) -> dict[Hashable, str]:
    wanted = [i for i in ids if isinstance(i, uuid.UUID)]
    if not wanted:
        return {}
    if group_by is GroupBy.PAYEE:
        rows = db.execute(
            select(Payee.id, Payee.name).where(
                Payee.household_id == household_id, Payee.id.in_(wanted)
            )
        )
    elif group_by is GroupBy.ACCOUNT:
        rows = db.execute(
            select(Account.id, Account.name).where(
                Account.household_id == household_id, Account.id.in_(wanted)
            )
        )
    else:  # pragma: no cover - categories and months are named elsewhere
        return {}
    return {row[0]: row[1] for row in rows}


def flow_report(
    db: Session,
    principal: Principal,
    *,
    flow: Flow,
    date_from: date | None = None,
    date_to: date | None = None,
    group_by: GroupBy = GroupBy.CATEGORY,
    depth: int = 1,
    account_ids: Collection[uuid.UUID] | None = None,
    include_pending: bool | None = None,
) -> FlowReport:
    """``GET /reports/spending`` / ``/reports/income`` (module doc).

    Default range: the current month (``date_to`` defaults to the end of
    this month, ``date_from`` to the first day of ``date_to``'s month).
    """
    household_id = household_for_reports(principal)
    end = date_to if date_to is not None else month_end(today())
    start = date_from if date_from is not None else month_start(end)
    currency = base_currency(db, household_id)
    pending = resolve_include_pending(db, household_id, include_pending)
    scope = make_scope(
        db,
        principal,
        household_id,
        date_from=start,
        date_to=end,
        currency=currency,
        account_ids=account_ids,
        include_pending=pending,
    )
    column = {
        GroupBy.CATEGORY: "category_id",
        GroupBy.PAYEE: "payee_id",
        GroupBy.ACCOUNT: "account_id",
        GroupBy.MONTH: "month",
    }[group_by]
    sums = aggregate(db, ledger_lines(principal, scope), [column], flow=_FLOW_LINES[flow])
    tree = CategoryTree.load(db, household_id) if group_by is GroupBy.CATEGORY else None
    acc = Accumulator(FxConverter(db, household_id, currency, until=end))
    for row in sums:
        key: Hashable = row.key[0]
        if tree is not None:
            key = tree.at_depth(row.key[0], depth)
        acc.add(key, _flow_value(flow, row.total), row.currency, row.fx_day)

    items: list[ReportItem]
    if group_by is GroupBy.MONTH:
        items = [
            ReportItem(key=month_key(m), name=None, amount=Money.of(acc.get(m), currency))
            for m in iter_months(start, end)
        ]
    else:
        if tree is not None:
            names: dict[Hashable, str | None] = {k: tree.name(_as_uuid(k)) for k in acc.values}
        else:
            names = dict(_names(db, household_id, group_by, acc.values.keys()))
        ordered = sorted(
            acc.values.items(),
            key=lambda kv: (-kv[1], names.get(kv[0]) or "", str(kv[0]) if kv[0] else ""),
        )
        items = [
            ReportItem(
                key=str(k) if k is not None else None,
                name=names.get(k),
                amount=Money.of(v, currency),
            )
            for k, v in ordered
        ]
    total = sum_amounts(acc.values.values())
    return FlowReport(
        flow=flow,
        currency=currency,
        date_from=start,
        date_to=end,
        group_by=group_by,
        depth=depth,
        include_pending=pending,
        items=items,
        total=Money.of(total, currency),
        unconverted=acc.unconverted_out(),
    )


def _as_uuid(value: Hashable) -> uuid.UUID | None:
    return value if isinstance(value, uuid.UUID) else None


def _flow_value(flow: Flow, total: Decimal) -> Decimal:
    """Spending is shown as positive money out; income as it is."""
    return -total if flow is Flow.SPENDING else total


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------


def trends(
    db: Session,
    principal: Principal,
    *,
    flow: Flow = Flow.SPENDING,
    month: date | None = None,
    months: int = 12,
    depth: int = 1,
    account_ids: Collection[uuid.UUID] | None = None,
    include_pending: bool | None = None,
) -> TrendReport:
    """``GET /reports/trends`` (module doc); ``month`` defaults to the current month."""
    household_id = household_for_reports(principal)
    current = month_start(month if month is not None else today())
    previous = add_months(current, -1)
    year_ago = add_months(current, -12)
    series_start = add_months(current, -(months - 1))
    start = min(series_start, year_ago)
    end = month_end(current)
    currency = base_currency(db, household_id)
    pending = resolve_include_pending(db, household_id, include_pending)
    scope = make_scope(
        db,
        principal,
        household_id,
        date_from=start,
        date_to=end,
        currency=currency,
        account_ids=account_ids,
        include_pending=pending,
    )
    sums = aggregate(
        db, ledger_lines(principal, scope), ["month", "category_id"], flow=_FLOW_LINES[flow]
    )
    tree = CategoryTree.load(db, household_id)
    acc = Accumulator(FxConverter(db, household_id, currency, until=end))
    for row in sums:
        key = (row.key[0], tree.at_depth(row.key[1], depth))
        acc.add(key, _flow_value(flow, row.total), row.currency, row.fx_day)

    by_month: dict[date, Decimal] = {}
    categories: set[uuid.UUID | None] = set()
    for (m, category), value in _items(acc):
        by_month[m] = sum_amounts((by_month.get(m, ZERO), value))
        if m in (current, previous, year_ago):
            categories.add(category)

    def row_for(key: uuid.UUID | None, name: str | None, values: dict[date, Decimal]) -> TrendRow:
        now, prev, ago = (values.get(m, ZERO) for m in (current, previous, year_ago))
        return TrendRow(
            key=str(key) if key is not None else None,
            name=name,
            amount=Money.of(now, currency),
            previous=Money.of(prev, currency),
            mom_change=Money.of(now - prev, currency),
            mom_percent=percent(now - prev, prev),
            year_ago=Money.of(ago, currency),
            yoy_change=Money.of(now - ago, currency),
            yoy_percent=percent(now - ago, ago),
        )

    rows = []
    for category in categories:
        values = {m: acc.get((m, category)) for m in (current, previous, year_ago)}
        rows.append(row_for(category, tree.name(category), values))
    rows.sort(key=lambda r: (-r.amount.as_decimal(), r.name or "", r.key or ""))
    return TrendReport(
        flow=flow,
        currency=currency,
        month=month_key(current),
        months=months,
        depth=depth,
        include_pending=pending,
        series=[
            TrendPoint(month=month_key(m), amount=Money.of(by_month.get(m, ZERO), currency))
            for m in iter_months(series_start, current)
        ],
        total=row_for(None, None, by_month),
        categories=rows,
        unconverted=acc.unconverted_out(),
    )


def _items(acc: Accumulator) -> list[tuple[tuple[date, uuid.UUID | None], Decimal]]:
    result: list[tuple[tuple[date, uuid.UUID | None], Decimal]] = []
    for key, value in acc.values.items():
        if not isinstance(key, tuple):  # pragma: no cover - keys are built above
            raise TypeError("trend keys are (month, category) pairs")
        result.append(((key[0], key[1]), value))
    return result


# ---------------------------------------------------------------------------
# Cash flow
# ---------------------------------------------------------------------------


def cashflow(
    db: Session,
    principal: Principal,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    account_ids: Collection[uuid.UUID] | None = None,
    include_pending: bool | None = None,
) -> CashflowReport:
    """``GET /reports/cashflow`` (module doc); default: this month and the 11 before."""
    household_id = household_for_reports(principal)
    end = date_to if date_to is not None else month_end(today())
    start = (
        date_from
        if date_from is not None
        else add_months(month_start(end), -(DEFAULT_CASHFLOW_MONTHS - 1))
    )
    currency = base_currency(db, household_id)
    pending = resolve_include_pending(db, household_id, include_pending)
    scope = make_scope(
        db,
        principal,
        household_id,
        date_from=start,
        date_to=end,
        currency=currency,
        account_ids=account_ids,
        include_pending=pending,
    )
    sums = aggregate(db, ledger_lines(principal, scope), ["month"])
    acc = Accumulator(FxConverter(db, household_id, currency, until=end))
    inflows, outflows = _split_flows(sums, acc)
    months = [
        CashflowMonth(
            month=month_key(m),
            inflow=Money.of(inflows.get(m, ZERO), currency),
            outflow=Money.of(-outflows.get(m, ZERO), currency),
            net=Money.of(inflows.get(m, ZERO) + outflows.get(m, ZERO), currency),
        )
        for m in iter_months(start, end)
    ]
    total_in = sum_amounts(inflows.values())
    total_out = sum_amounts(outflows.values())
    return CashflowReport(
        currency=currency,
        date_from=start,
        date_to=end,
        include_pending=pending,
        months=months,
        inflow=Money.of(total_in, currency),
        outflow=Money.of(-total_out, currency),
        net=Money.of(total_in + total_out, currency),
        unconverted=acc.unconverted_out(),
    )


def _split_flows(
    sums: Sequence[Sum], acc: Accumulator
) -> tuple[dict[date, Decimal], dict[date, Decimal]]:
    inflows: dict[date, Decimal] = {}
    outflows: dict[date, Decimal] = {}
    for row in sums:
        parts = acc.convert_parts(
            (row.inflow, row.outflow), row.currency, row.fx_day, missing=row.total
        )
        if parts is None:
            continue
        month: date = row.key[0]
        inflows[month] = sum_amounts((inflows.get(month, ZERO), parts[0]))
        outflows[month] = sum_amounts((outflows.get(month, ZERO), parts[1]))
    return inflows, outflows

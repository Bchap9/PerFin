# SPDX-License-Identifier: AGPL-3.0-or-later
"""Net worth: snapshots, history, latest figures and projections (API.md ``/networth``).

* :func:`sees_household_totals`: whether household-wide figures (a stored
  ``net_worth_snapshot`` with its per-account breakdown) may be shown to a
  principal: owners and admins, or a member/viewer who can see **every**
  account of the household. Anyone else gets figures recomputed over their
  visible accounts only (``scope = visible_accounts``), never a stored
  snapshot.
* :func:`latest_snapshot`: the most recent stored snapshot on or before a day.
* :func:`snapshot_household` (the daily ``networth.snapshot`` job): computes
  and upserts today's snapshot (``app.domain.networth.compute``) and fills
  every missing day since the last stored one (or since the household's
  first data, at most :data:`MAX_BACKFILL_DAYS` back). One row per household
  and day (unique key, ``INSERT ... ON CONFLICT DO UPDATE``), so running it
  twice on a day changes nothing but the figures.
* :func:`history` (``GET /networth?range=&granularity=``): one point per
  bucket end (every day, every 7 days back from today, or every month end),
  oldest first, the last point always today. With household scope a stored
  snapshot of that exact day (in today's base currency) is used, otherwise
  the point is computed; today is always computed live. ``range`` as
  investments performance (``1m`` ... ``5y``, ``ytd``, ``max`` = from the
  first data). Without ``granularity``: ``day`` up to 400 days, ``week`` up
  to 7 years, else ``month``; more than :data:`MAX_POINTS` points is ``422
  too-many-points``, except that ``max`` without ``granularity`` starts at
  most :data:`MAX_POINTS` months back (SECURITY.md F8).
* :func:`latest` (``GET /networth/latest``): today's figures, computed live,
  with the breakdown by class, by account type and per account.
* :func:`projection` (``GET /networth/projection``): ``app.domain.networth.projection``.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.auth.authz import forbidden
from app.auth.principal import Principal
from app.core import clock
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.money import Money
from app.domain.accounts.models import Account
from app.domain.accounts.visibility import sees_all_accounts, visible_account_ids
from app.domain.fx.service import base_currency
from app.domain.investments.performance import range_start
from app.domain.investments.schemas import Range
from app.domain.networth import projection as model
from app.domain.networth.compute import NetWorthBook, NetWorthPoint
from app.domain.networth.models import NetWorthSnapshot
from app.domain.networth.schemas import (
    MAX_POINTS,
    AccountValueOut,
    ClassTotals,
    Granularity,
    NetWorthHistory,
    NetWorthLatest,
    NetWorthPointOut,
    NetWorthProjection,
    PointSource,
    ProjectionPoint,
    SavingsModel,
    Scope,
    TrendModel,
)
from app.domain.reports import service as reports
from app.domain.reports.dates import add_months, iter_months, month_end, month_start

#: Most days one job run fills (ten years).
MAX_BACKFILL_DAYS: Final = 3660
_AUTO_DAY_LIMIT: Final = 400
_AUTO_WEEK_LIMIT: Final = 7 * 366


def today() -> date:
    return clock.utcnow().date()


def sees_household_totals(db: Session, principal: Principal) -> bool:
    if principal.household_id is None:
        return False
    if sees_all_accounts(principal):
        return True
    total = db.execute(
        select(func.count())
        .select_from(Account)
        .where(Account.household_id == principal.household_id)
    ).scalar_one()
    return len(visible_account_ids(db, principal)) == int(total)


def latest_snapshot(db: Session, household_id: uuid.UUID, on: date) -> NetWorthSnapshot | None:
    return db.execute(
        select(NetWorthSnapshot)
        .where(NetWorthSnapshot.household_id == household_id, NetWorthSnapshot.as_of_date <= on)
        .order_by(NetWorthSnapshot.as_of_date.desc())
        .limit(1)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Snapshots (worker)
# ---------------------------------------------------------------------------


def upsert_snapshot(db: Session, household_id: uuid.UUID, point: NetWorthPoint) -> None:
    values = {
        "assets": point.assets,
        "liabilities": point.liabilities,
        "net": point.net,
        "currency": point.currency,
        "breakdown": point.breakdown(),
        "updated_at": func.now(),
    }
    stmt = insert(NetWorthSnapshot).values(household_id=household_id, as_of_date=point.on, **values)
    db.execute(stmt.on_conflict_do_update(constraint="uq_net_worth_snapshot_date", set_=values))


def snapshot_household(
    db: Session, household_id: uuid.UUID, on: date, *, max_days: int = MAX_BACKFILL_DAYS
) -> int:
    """Upsert the snapshot of ``on`` and fill the missing days before it (module doc).

    Runs in the household's RLS context. Returns the number of days written.
    """
    base = base_currency(db, household_id)
    book = NetWorthBook.load(db, household_id, None, target=base, until=on)
    last = db.execute(
        select(func.max(NetWorthSnapshot.as_of_date)).where(
            NetWorthSnapshot.household_id == household_id, NetWorthSnapshot.as_of_date < on
        )
    ).scalar_one_or_none()
    start = last + timedelta(days=1) if last is not None else (book.first_day() or on)
    start = min(max(start, on - timedelta(days=max_days - 1)), on)
    day = start
    written = 0
    while day <= on:
        upsert_snapshot(db, household_id, book.on(day))
        written += 1
        day += timedelta(days=1)
    # No payload: the event names no account, so only owners and admins hear it.
    emit(db, household_id, "networth.snapshot")
    return written


# ---------------------------------------------------------------------------
# Reads (API)
# ---------------------------------------------------------------------------


def _household(principal: Principal) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    return principal.household_id


def _scope(db: Session, principal: Principal) -> tuple[Scope, list[uuid.UUID] | None]:
    """``household`` (every account) or the accounts the principal may see."""
    if sees_household_totals(db, principal):
        return "household", None
    return "visible_accounts", visible_account_ids(db, principal)


def _money(value: Decimal, currency: str) -> Money:
    return Money.of(value, currency)


def _point_out(point: NetWorthPoint, source: PointSource) -> NetWorthPointOut:
    return NetWorthPointOut(
        as_of=point.on,
        assets=_money(point.assets, point.currency),
        liabilities=_money(point.liabilities, point.currency),
        net=_money(point.net, point.currency),
        source=source,
    )


def _snapshot_out(row: NetWorthSnapshot) -> NetWorthPointOut:
    return NetWorthPointOut(
        as_of=row.as_of_date,
        assets=_money(row.assets, row.currency),
        liabilities=_money(row.liabilities, row.currency),
        net=_money(row.net, row.currency),
        source="snapshot",
    )


def bucket_ends(start: date, end: date, granularity: Granularity) -> list[date]:
    """The days a history samples in ``start..end`` (module doc), oldest first."""
    if end < start:
        return []
    if granularity is Granularity.DAY:
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]
    if granularity is Granularity.WEEK:
        days = [end - timedelta(days=7 * i) for i in range((end - start).days // 7 + 1)]
        return sorted(days)
    days = [month_end(m) for m in iter_months(start, end)]
    return [d for d in days if d < end] + [end]


def auto_granularity(start: date, end: date) -> Granularity:
    span = (end - start).days + 1
    if span <= _AUTO_DAY_LIMIT:
        return Granularity.DAY
    if span <= _AUTO_WEEK_LIMIT:
        return Granularity.WEEK
    return Granularity.MONTH


class _Series:
    """Points of one scope: stored snapshots where allowed, else computed."""

    def __init__(self, db: Session, principal: Principal, end: date) -> None:
        self.db = db
        self.household_id = _household(principal)
        self.end = end
        self.base = base_currency(db, self.household_id)
        self.scope, self.account_ids = _scope(db, principal)
        self._book: NetWorthBook | None = None

    @property
    def book(self) -> NetWorthBook:
        if self._book is None:
            self._book = NetWorthBook.load(
                self.db, self.household_id, self.account_ids, target=self.base, until=self.end
            )
        return self._book

    def first_day(self) -> date | None:
        days = [d for d in [self.book.first_day()] if d is not None]
        if self.scope == "household":
            stored = self.db.execute(
                select(func.min(NetWorthSnapshot.as_of_date)).where(
                    NetWorthSnapshot.household_id == self.household_id
                )
            ).scalar_one_or_none()
            if stored is not None:
                days.append(stored)
        return min(days) if days else None

    def points(self, days: list[date]) -> list[NetWorthPointOut]:
        stored: dict[date, NetWorthSnapshot] = {}
        if self.scope == "household" and days:
            for row in self.db.execute(
                select(NetWorthSnapshot).where(
                    NetWorthSnapshot.household_id == self.household_id,
                    NetWorthSnapshot.as_of_date.in_(days),
                    NetWorthSnapshot.currency == self.base,
                )
            ).scalars():
                stored[row.as_of_date] = row
        out: list[NetWorthPointOut] = []
        for day in days:
            found = stored.get(day)
            if found is not None and day != self.end:
                out.append(_snapshot_out(found))
            else:
                out.append(_point_out(self.book.on(day), "computed"))
        return out


def history(
    db: Session,
    principal: Principal,
    *,
    range_: Range = "1y",
    granularity: Granularity | None = None,
) -> NetWorthHistory:
    end = today()
    series = _Series(db, principal, end)
    start = range_start(range_, end)
    if start is None:
        start = series.first_day() or end
        if granularity is None:
            # ``max`` with the automatic granularity is monthly beyond seven
            # years: keep it to the newest MAX_POINTS months instead of a 422.
            start = max(start, month_start(add_months(end, -(MAX_POINTS - 1))))
    start = min(start, end)
    chosen = granularity or auto_granularity(start, end)
    days = bucket_ends(start, end, chosen)
    if len(days) > MAX_POINTS:
        raise ProblemError(
            422,
            "too-many-points",
            "Too many points",
            f"This range has {len(days)} {chosen.value} points; at most {MAX_POINTS}. "
            "Choose a coarser granularity.",
        )
    points = series.points(days)
    change = (
        Money.of(Decimal(points[-1].net.amount) - Decimal(points[0].net.amount), series.base)
        if len(points) >= 2
        else None
    )
    return NetWorthHistory(
        currency=series.base,
        range=range_,
        granularity=chosen,
        scope=series.scope,
        date_from=start,
        date_to=end,
        points=points,
        change=change,
    )


def latest(db: Session, principal: Principal) -> NetWorthLatest:
    end = today()
    series = _Series(db, principal, end)
    point = series.book.on(end)
    base = series.base
    names = (
        {
            a.id: a.name
            for a in db.execute(
                select(Account).where(
                    Account.household_id == series.household_id,
                    Account.id.in_([v.account_id for v in point.accounts]),
                )
            ).scalars()
        }
        if point.accounts
        else {}
    )
    classes = point.by_class()
    stored = latest_snapshot(db, series.household_id, end) if series.scope == "household" else None
    return NetWorthLatest(
        as_of=end,
        currency=base,
        scope=series.scope,
        assets=_money(point.assets, base),
        liabilities=_money(point.liabilities, base),
        net=_money(point.net, base),
        by_class=ClassTotals(
            assets={k: _money(v, base) for k, v in classes["asset"].items()},
            liabilities={k: _money(v, base) for k, v in classes["liability"].items()},
        ),
        by_type={k: _money(v, base) for k, v in point.by_type().items()},
        accounts=[
            AccountValueOut(
                account_id=v.account_id,
                name=names.get(v.account_id, ""),
                type=v.type.value,
                account_class=v.klass,
                side=v.side,
                amount=_money(v.amount, v.currency),
                amount_base=_money(v.amount_base, base) if v.amount_base is not None else None,
            )
            for v in point.accounts
        ],
        unconverted=[v.account_id for v in point.unconverted],
        unpriced_security_ids=list(point.unpriced_security_ids),
        last_snapshot_date=stored.as_of_date if stored is not None else None,
    )


def projection(
    db: Session, principal: Principal, *, months: int, basis_months: int
) -> NetWorthProjection:
    end = today()
    series = _Series(db, principal, end)
    base = series.base
    first_month = add_months(month_start(end), -basis_months)
    last_whole = month_start(end) - timedelta(days=1)
    days = [month_end(m) for m in iter_months(first_month, last_whole)] + [end]
    history_points = series.points(days)
    current = Decimal(history_points[-1].net.amount)
    targets = [add_months(end, k) for k in range(1, months + 1)]

    slope = model.slope_per_day([(p.as_of, Decimal(p.net.amount)) for p in history_points])
    trend = (
        TrendModel(
            slope_per_month=_money(model.per_month(slope), base),
            points=[
                ProjectionPoint(on=day, net=_money(model.along(current, slope, end, day), base))
                for day in targets
            ],
        )
        if slope is not None
        else None
    )

    flows = reports.cashflow(db, principal, date_from=first_month, date_to=last_whole)
    net_flow = Decimal(flows.net.amount)
    monthly = model.average(net_flow, basis_months)
    rate = model.savings_rate(net_flow, Decimal(flows.inflow.amount))
    savings = SavingsModel(
        average_monthly_savings=_money(monthly, base),
        savings_rate=f"{rate:f}" if rate is not None else None,
        points=[
            ProjectionPoint(on=day, net=_money(model.accumulate(current, monthly, k), base))
            for k, day in enumerate(targets, start=1)
        ],
    )
    return NetWorthProjection(
        as_of=end,
        currency=base,
        scope=series.scope,
        months=months,
        basis_months=basis_months,
        current=_money(current, base),
        trend=trend,
        savings=savings,
        unconverted=flows.unconverted,
    )

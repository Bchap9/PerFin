# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``/fx-rates`` API rules (API.md "FX rates").

* ``GET /fx-rates?base=&quote=&from=&to=&cursor=&limit=``: any role; the
  rate history of the household, newest day first (then pair). ``base`` and
  ``quote`` filter the stored direction; pass both to read one pair.
* ``POST /fx-rates``: owner or admin. Sets the **manual** rate of a pair and
  day (``1 base = rate quote``), replacing whatever rate that pair had that
  day (a fetched one included; ``source`` becomes ``manual``). Not in the
  future (``422 fx-rate-date-in-future``). Rates are household-wide and
  change every converted total, including totals over accounts a member
  cannot see, so members and viewers only read them.
* ``DELETE /fx-rates/{id}``: owner or admin (``404 fx-rate-not-found``).

Writes are audited (``fx_rate.set``, ``fx_rate.delete``) and emit
``fx.rate_set`` / ``fx.rate_deleted`` with the rate id only. Fetched rates
(``app.integrations.fx``) never replace a manual one.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.domain.fx import repo
from app.domain.fx.models import MANUAL_SOURCE, FxRate
from app.domain.fx.schemas import FxRateOut, FxRatePage, FxRateSet

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return principal.household_id


def format_rate(value: Any) -> str:
    """Plain decimal string without trailing zeros (``"1.0812"``)."""
    text = f"{value:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def rate_out(row: FxRate) -> FxRateOut:
    return FxRateOut(
        id=row.id,
        base=row.base,
        quote=row.quote,
        rate_date=row.rate_date,
        rate=format_rate(row.rate),
        source=row.source,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def list_rates(
    db: Session,
    principal: Principal,
    *,
    base: str | None,
    quote: str | None,
    date_from: date | None,
    date_to: date | None,
    cursor: uuid.UUID | None,
    limit: int,
) -> FxRatePage:
    """Newest day first, then base, quote; ``cursor`` is the last id of a page."""
    household_id = _household(principal, HouseholdRole.VIEWER)
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ProblemError(422, "invalid-range", "Invalid range", "'from' is after 'to'.")
    stmt = select(FxRate).where(FxRate.household_id == household_id)
    if base is not None:
        stmt = stmt.where(FxRate.base == base)
    if quote is not None:
        stmt = stmt.where(FxRate.quote == quote)
    if date_from is not None:
        stmt = stmt.where(FxRate.rate_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(FxRate.rate_date <= date_to)
    if cursor is not None:
        after = db.execute(
            select(FxRate).where(FxRate.household_id == household_id, FxRate.id == cursor)
        ).scalar_one_or_none()
        if after is None:
            raise ProblemError(422, "invalid-cursor", "Invalid cursor")
        # Order: rate_date desc, base asc, quote asc (unique per household).
        stmt = stmt.where(
            (FxRate.rate_date < after.rate_date)
            | (
                (FxRate.rate_date == after.rate_date)
                & (tuple_(FxRate.base, FxRate.quote) > tuple_(after.base, after.quote))
            )
        )
    order = (FxRate.rate_date.desc(), FxRate.base, FxRate.quote)
    rows = list(db.execute(stmt.order_by(*order).limit(limit + 1)).scalars())
    page = rows[:limit]
    return FxRatePage(
        items=[rate_out(r) for r in page],
        next_cursor=page[-1].id if len(rows) > limit and page else None,
    )


def _record(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    row_id: uuid.UUID,
    action: str,
    event: str,
    *,
    before: Any = None,
    after: Any = None,
    request: HTTPConnection | None,
) -> None:
    audit(
        db,
        action=f"fx_rate.{action}",
        target_type="fx_rate",
        target_id=row_id,
        before=before,
        after=after,
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(db, household_id, f"fx.{event}", {"rate_id": str(row_id)})


def _state(row: FxRate) -> dict[str, str]:
    return {
        "base": row.base,
        "quote": row.quote,
        "rate_date": row.rate_date.isoformat(),
        "rate": format_rate(row.rate),
        "source": row.source,
    }


def set_rate(
    db: Session, principal: Principal, data: FxRateSet, *, request: HTTPConnection | None
) -> FxRateOut:
    household_id = _household(principal, HouseholdRole.ADMIN)
    if data.rate_date > clock.utcnow().date():
        raise ProblemError(
            422,
            "fx-rate-date-in-future",
            "Rate date in the future",
            "Exchange rates are recorded for days that have begun.",
        )
    existing = db.execute(
        select(FxRate).where(
            FxRate.household_id == household_id,
            FxRate.base == data.base,
            FxRate.quote == data.quote,
            FxRate.rate_date == data.rate_date,
        )
    ).scalar_one_or_none()
    before = _state(existing) if existing is not None else None
    row = repo.upsert_rate(
        db, household_id, data.base, data.quote, data.rate_date, data.rate, source=MANUAL_SOURCE
    )
    _record(
        db,
        principal,
        household_id,
        row.id,
        "set",
        "rate_set",
        before=before,
        after=_state(row),
        request=request,
    )
    db.refresh(row)
    return rate_out(row)


def delete_rate(
    db: Session, principal: Principal, rate_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    household_id = _household(principal, HouseholdRole.ADMIN)
    row = db.execute(
        select(FxRate).where(FxRate.household_id == household_id, FxRate.id == rate_id)
    ).scalar_one_or_none()
    if row is None:
        raise ProblemError(404, "fx-rate-not-found", "Exchange rate not found")
    _record(
        db,
        principal,
        household_id,
        row.id,
        "delete",
        "rate_deleted",
        before=_state(row),
        request=request,
    )
    db.delete(row)
    db.flush()

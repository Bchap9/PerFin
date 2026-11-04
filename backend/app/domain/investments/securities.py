# SPDX-License-Identifier: AGPL-3.0-or-later
"""The security catalogue and manual prices (API.md ``/securities``).

* **Read** (any role, ``investments:read``): the securities the caller may
  see (``service.visible_security_filter``: owners and admins all; members
  and viewers those with activity on an account they can see, or with no
  activity at all). Anything else is ``404 security-not-found``.
* **Write** (member or above, ``investments:write``): create; change and
  delete visible securities. ``(symbol, exchange)`` is unique in the
  household (``409 security-exists``). The currency changes only while the
  security has no prices and no transactions (``409 security-currency-locked``);
  a security with transactions or lots cannot be deleted (``409
  security-in-use``), its prices and holding rows go with it.
* **Prices**: ``POST /securities/{id}/prices`` sets the manual close of a day
  (replacing whatever price that day had, ``source`` becomes ``manual``);
  closes are in the security's currency and above zero. External price feeds
  are opt-in and live elsewhere (docs/security/integrations.md).

Writes are audited (``security.create|update|delete``,
``security.price_set|price_delete``) and emit ``security.*`` events with
ids only; ``security.`` events are account-scoped in the SSE filter, so only
callers who see every account hear about them.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, or_, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.domain.investments import service
from app.domain.investments.lots import format_units
from app.domain.investments.models import (
    MANUAL_SOURCE,
    AssetClass,
    InvestmentTxn,
    Lot,
    Price,
    Security,
)
from app.domain.investments.schemas import (
    PriceOut,
    PricePage,
    PriceSet,
    SecurityCreate,
    SecurityOut,
    SecurityPage,
    SecurityUpdate,
)

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

_FIELDS = ("symbol", "name", "asset_class", "currency", "isin", "cusip", "sector", "exchange")


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return principal.household_id


def _exists_problem() -> ProblemError:
    return ProblemError(
        409,
        "security-exists",
        "Security exists",
        "The household already has a security with this symbol and exchange.",
    )


def security_out(row: Security) -> SecurityOut:
    return SecurityOut(
        id=row.id,
        symbol=row.symbol,
        name=row.name,
        asset_class=row.asset_class,
        currency=row.currency,
        isin=row.isin,
        cusip=row.cusip,
        sector=row.sector,
        exchange=row.exchange,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def price_out(row: Price) -> PriceOut:
    return PriceOut(
        id=row.id,
        security_id=row.security_id,
        price_date=row.price_date,
        close=format_units(row.close),
        currency=row.currency,
        source=row.source,
    )


def _state(row: Security) -> dict[str, Any]:
    return {
        name: (value.value if isinstance(value, AssetClass) else value)
        for name in _FIELDS
        for value in (getattr(row, name),)
    }


def _record(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    security_id: uuid.UUID,
    action: str,
    event: str,
    *,
    before: Any = None,
    after: Any = None,
    request: HTTPConnection | None,
) -> None:
    audit(
        db,
        action=f"security.{action}",
        target_type="security",
        target_id=security_id,
        before=before,
        after=after,
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(db, household_id, f"security.{event}", {"security_id": str(security_id)})


def _clash(
    db: Session,
    household_id: uuid.UUID,
    symbol: str,
    exchange: str | None,
    exclude: uuid.UUID | None = None,
) -> bool:
    stmt = select(Security.id).where(
        Security.household_id == household_id,
        Security.symbol == symbol,
        Security.exchange.is_(None) if exchange is None else Security.exchange == exchange,
    )
    if exclude is not None:
        stmt = stmt.where(Security.id != exclude)
    return db.execute(stmt.limit(1)).first() is not None


def _flush_unique(db: Session) -> None:
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as exc:
        raise _exists_problem() from exc


# ---------------------------------------------------------------------------
# Securities
# ---------------------------------------------------------------------------


def list_securities(
    db: Session,
    principal: Principal,
    *,
    q: str | None,
    asset_class: AssetClass | None,
    cursor: uuid.UUID | None,
    limit: int,
) -> SecurityPage:
    """By symbol, then id; ``q`` matches symbol, name, ISIN or CUSIP (case-insensitive)."""
    household_id = _household(principal, HouseholdRole.VIEWER)
    stmt = select(Security).where(
        Security.household_id == household_id, service.visible_security_filter(principal)
    )
    if q:
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(
            or_(
                Security.symbol.ilike(pattern, escape="\\"),
                Security.name.ilike(pattern, escape="\\"),
                Security.isin.ilike(pattern, escape="\\"),
                Security.cusip.ilike(pattern, escape="\\"),
            )
        )
    if asset_class is not None:
        stmt = stmt.where(Security.asset_class == asset_class)
    if cursor is not None:
        after = db.execute(
            select(Security.symbol).where(
                Security.household_id == household_id, Security.id == cursor
            )
        ).scalar_one_or_none()
        if after is None:
            raise ProblemError(422, "invalid-cursor", "Invalid cursor")
        stmt = stmt.where(tuple_(Security.symbol, Security.id) > tuple_(after, cursor))
    rows = list(db.execute(stmt.order_by(Security.symbol, Security.id).limit(limit + 1)).scalars())
    page = rows[:limit]
    return SecurityPage(
        items=[security_out(r) for r in page],
        next_cursor=page[-1].id if len(rows) > limit and page else None,
    )


def get_security(db: Session, principal: Principal, security_id: uuid.UUID) -> SecurityOut:
    _household(principal, HouseholdRole.VIEWER)
    return security_out(service.get_visible_security(db, principal, security_id))


def create_security(
    db: Session, principal: Principal, data: SecurityCreate, *, request: HTTPConnection | None
) -> SecurityOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    if _clash(db, household_id, data.symbol, data.exchange):
        raise _exists_problem()
    row = Security(household_id=household_id, **{name: getattr(data, name) for name in _FIELDS})
    db.add(row)
    _flush_unique(db)
    _record(
        db, principal, household_id, row.id, "create", "created", after=_state(row), request=request
    )
    return security_out(row)


def _in_use(db: Session, household_id: uuid.UUID, security_id: uuid.UUID) -> bool:
    used = select(InvestmentTxn.id).where(
        InvestmentTxn.household_id == household_id, InvestmentTxn.security_id == security_id
    )
    lots = select(Lot.id).where(Lot.household_id == household_id, Lot.security_id == security_id)
    return bool(db.execute(select(exists(used) | exists(lots))).scalar())


def update_security(
    db: Session,
    principal: Principal,
    security_id: uuid.UUID,
    data: SecurityUpdate,
    *,
    request: HTTPConnection | None,
) -> SecurityOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    row = service.get_visible_security(db, principal, security_id, lock=True)
    before = _state(row)
    given = data.model_fields_set
    if "currency" in given and data.currency != row.currency:
        prices = select(Price.id).where(
            Price.household_id == household_id, Price.security_id == row.id
        )
        if _in_use(db, household_id, row.id) or db.execute(select(exists(prices))).scalar():
            raise ProblemError(
                409,
                "security-currency-locked",
                "Currency locked",
                "The currency of a security with prices or transactions cannot change.",
            )
    symbol = data.symbol if "symbol" in given and data.symbol is not None else row.symbol
    exchange = data.exchange if "exchange" in given else row.exchange
    if _clash(db, household_id, symbol, exchange, exclude=row.id):
        raise _exists_problem()
    for name in _FIELDS:
        if name in given:
            setattr(row, name, getattr(data, name))
    _flush_unique(db)
    after = _state(row)
    if after != before:
        _record(
            db,
            principal,
            household_id,
            row.id,
            "update",
            "updated",
            before=before,
            after=after,
            request=request,
        )
    return security_out(row)


def delete_security(
    db: Session, principal: Principal, security_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    household_id = _household(principal, HouseholdRole.MEMBER)
    row = service.get_visible_security(db, principal, security_id, lock=True)
    if _in_use(db, household_id, row.id):
        raise ProblemError(
            409,
            "security-in-use",
            "Security in use",
            "A security with transactions or lots cannot be deleted.",
        )
    _record(
        db,
        principal,
        household_id,
        row.id,
        "delete",
        "deleted",
        before=_state(row),
        request=request,
    )
    db.delete(row)
    db.flush()


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


def list_prices(
    db: Session,
    principal: Principal,
    security_id: uuid.UUID,
    *,
    date_from: date | None,
    date_to: date | None,
    cursor: uuid.UUID | None,
    limit: int,
) -> PricePage:
    """Newest first; ``cursor`` is the last id of a page."""
    household_id = _household(principal, HouseholdRole.VIEWER)
    security = service.get_visible_security(db, principal, security_id)
    stmt = select(Price).where(Price.household_id == household_id, Price.security_id == security.id)
    if date_from is not None:
        stmt = stmt.where(Price.price_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Price.price_date <= date_to)
    if cursor is not None:
        after = db.execute(
            select(Price.price_date).where(
                Price.household_id == household_id,
                Price.security_id == security.id,
                Price.id == cursor,
            )
        ).scalar_one_or_none()
        if after is None:
            raise ProblemError(422, "invalid-cursor", "Invalid cursor")
        stmt = stmt.where(Price.price_date < after)
    rows = list(db.execute(stmt.order_by(Price.price_date.desc()).limit(limit + 1)).scalars())
    page = rows[:limit]
    return PricePage(
        items=[price_out(r) for r in page],
        next_cursor=page[-1].id if len(rows) > limit and page else None,
    )


def set_price(
    db: Session,
    principal: Principal,
    security_id: uuid.UUID,
    data: PriceSet,
    *,
    request: HTTPConnection | None,
) -> PriceOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    security = service.get_visible_security(db, principal, security_id, lock=True)
    if data.price_date > service.today():
        raise ProblemError(
            422, "price-date-in-future", "Price date in the future", "Prices are closing prices."
        )
    row = db.execute(
        select(Price).where(
            Price.household_id == household_id,
            Price.security_id == security.id,
            Price.price_date == data.price_date,
        )
    ).scalar_one_or_none()
    before = {"close": format_units(row.close), "source": row.source} if row is not None else None
    if row is None:
        row = Price(
            household_id=household_id,
            security_id=security.id,
            price_date=data.price_date,
            currency=security.currency,
        )
        db.add(row)
    row.close = data.close
    row.source = MANUAL_SOURCE
    db.flush()
    _record(
        db,
        principal,
        household_id,
        security.id,
        "price_set",
        "price_set",
        before=before,
        after={"price_date": data.price_date.isoformat(), "close": format_units(data.close)},
        request=request,
    )
    return price_out(row)


def delete_price(
    db: Session,
    principal: Principal,
    security_id: uuid.UUID,
    price_id: uuid.UUID,
    *,
    request: HTTPConnection | None,
) -> None:
    household_id = _household(principal, HouseholdRole.MEMBER)
    security = service.get_visible_security(db, principal, security_id, lock=True)
    row = db.execute(
        select(Price).where(
            Price.household_id == household_id,
            Price.security_id == security.id,
            Price.id == price_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise ProblemError(404, "price-not-found", "Price not found")
    _record(
        db,
        principal,
        household_id,
        security.id,
        "price_delete",
        "price_deleted",
        before={"price_date": row.price_date.isoformat(), "close": format_units(row.close)},
        request=request,
    )
    db.delete(row)
    db.flush()

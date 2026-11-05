# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manual assets and their valuations (API.md ``/assets``).

Shared building blocks (net worth, AI views):

* :func:`latest_valuation`: the value of an account on a day (the latest
  valuation on or before it; on one date ``manual`` beats ``provider``).
* :func:`visible_manual_assets`: manual assets of accounts the principal sees.

Who may do what (routers check role and token scope first; every function
here checks again):

* **Read** (any role, ``accounts:read``): assets of visible accounts only;
  any other is ``404 asset-not-found``.
* **Write** (member or above, ``accounts:write``): the asset's account must
  be editable (``404`` hidden, ``403`` view-only).

Rules: an asset details an account of type ``manual_asset`` (``422
invalid-asset-account``), at most one per account (``409 asset-exists``).
Its kind (property, vehicle, other) is the account's ``asset_kind``. A
valuation is ``>= 0`` in the account's currency (``422 currency-mismatch``,
``422 invalid-valuation``), dated today or earlier (``422
valuation-in-future``); a second manual valuation on the same day replaces
the first. Each manual valuation is also the account's ``manual`` balance
snapshot of that day, so the account's balance follows its valuations;
deleting the valuation removes that snapshot.

``description`` may hold an address: it never goes to the audit trail,
events or logs (only whether it is set). Writes are audited
(``asset.create|update|delete``, ``asset.valuation_add|valuation_delete``)
and emit ``asset.created|updated|deleted`` with ids only (account-scoped).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, case, delete, select
from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.money import Money
from app.domain.accounts import repo as accounts_repo
from app.domain.accounts.models import Account, AccountType, BalanceSnapshot, BalanceSource
from app.domain.accounts.visibility import (
    account_not_found,
    can_edit_account,
    get_visible_account,
    visible_account_filter,
)
from app.domain.assets.models import AssetValuation, ManualAsset, ValuationSource
from app.domain.assets.schemas import (
    AssetCreate,
    AssetOut,
    AssetPage,
    AssetUpdate,
    ValuationCreate,
    ValuationList,
    ValuationOut,
)

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------


def latest_valuation(
    db: Session, household_id: uuid.UUID, account_id: uuid.UUID, on: date
) -> AssetValuation | None:
    manual_first = case((AssetValuation.source == ValuationSource.MANUAL, 0), else_=1)
    return db.execute(
        select(AssetValuation)
        .where(
            AssetValuation.household_id == household_id,
            AssetValuation.account_id == account_id,
            AssetValuation.valued_on <= on,
        )
        .order_by(AssetValuation.valued_on.desc(), manual_first)
        .limit(1)
    ).scalar_one_or_none()


def visible_manual_assets(principal: Principal) -> Select[ManualAsset]:
    if principal.household_id is None:
        raise ValueError("principal has no active household")
    return select(ManualAsset).where(
        ManualAsset.household_id == principal.household_id,
        visible_account_filter(principal, ManualAsset.account_id),
    )


# ---------------------------------------------------------------------------
# Problems and helpers
# ---------------------------------------------------------------------------


def asset_not_found() -> ProblemError:
    return ProblemError(404, "asset-not-found", "Asset not found")


def valuation_not_found() -> ProblemError:
    return ProblemError(404, "valuation-not-found", "Valuation not found")


def today() -> date:
    return clock.utcnow().date()


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return principal.household_id


def _account(db: Session, asset: ManualAsset, *, lock: bool = False) -> Account:
    account = accounts_repo.get_account(db, asset.household_id, asset.account_id, lock=lock)
    if account is None:  # pragma: no cover - the foreign key cascades
        raise account_not_found()
    return account


def _get(
    db: Session, principal: Principal, asset_id: uuid.UUID, *, lock: bool = False
) -> ManualAsset:
    _household(principal, HouseholdRole.VIEWER)
    stmt = visible_manual_assets(principal).where(ManualAsset.id == asset_id)
    if lock:
        stmt = stmt.with_for_update()
    asset = db.execute(stmt).scalar_one_or_none()
    if asset is None:
        raise asset_not_found()
    return asset


def get_asset_row(db: Session, principal: Principal, asset_id: uuid.UUID) -> ManualAsset:
    return _get(db, principal, asset_id)


def require_editable(db: Session, principal: Principal, asset_id: uuid.UUID) -> ManualAsset:
    """An asset the caller may change: hidden ``404``, view-only ``403`` (locked)."""
    _household(principal, HouseholdRole.MEMBER)
    asset = _get(db, principal, asset_id)
    if not can_edit_account(db, principal, asset.account_id):
        raise forbidden("You can view this asset but not change it.")
    return _get(db, principal, asset_id, lock=True)


def _state(asset: ManualAsset, account: Account) -> dict[str, Any]:
    # Never the description itself: it may be an address (AI_ACCESS.md).
    return {
        "account_id": str(asset.account_id),
        "kind": account.asset_kind.value if account.asset_kind else None,
        "valuation_method": asset.valuation_method.value,
        "has_description": asset.description is not None,
    }


def _record(
    db: Session,
    principal: Principal,
    asset: ManualAsset,
    action: str,
    *,
    event: str = "updated",
    target_type: str = "manual_asset",
    target_id: uuid.UUID | None = None,
    before: Any = None,
    after: Any = None,
    request: HTTPConnection | None,
) -> None:
    audit(
        db,
        action=f"asset.{action}",
        target_type=target_type,
        target_id=target_id or asset.id,
        before=before,
        after=after,
        household_id=asset.household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(
        db,
        asset.household_id,
        f"asset.{event}",
        {"asset_id": str(asset.id), "account_id": str(asset.account_id)},
    )


def _valuation_out(valuation: AssetValuation) -> ValuationOut:
    return ValuationOut(
        id=valuation.id,
        account_id=valuation.account_id,
        valued_on=valuation.valued_on,
        value=Money.of(valuation.value, valuation.currency),
        source=valuation.source,
        created_at=valuation.created_at,
    )


def asset_out(db: Session, asset: ManualAsset, account: Account | None = None) -> AssetOut:
    account = account or _account(db, asset)
    current = latest_valuation(db, asset.household_id, asset.account_id, today())
    if account.asset_kind is None:  # pragma: no cover - checked constraint on manual_asset
        raise asset_not_found()
    return AssetOut(
        id=asset.id,
        account_id=asset.account_id,
        name=account.name,
        kind=account.asset_kind,
        description=asset.description,
        valuation_method=asset.valuation_method,
        current_value=_valuation_out(current) if current is not None else None,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def _currency_mismatch(currency: str) -> ProblemError:
    return ProblemError(
        422,
        "currency-mismatch",
        "Currency mismatch",
        f"Values of this asset are in {currency}.",
        extra={"currency": currency},
    )


def _upsert_valuation(db: Session, account: Account, valued_on: date, value: Any) -> AssetValuation:
    if value.currency != account.currency:
        raise _currency_mismatch(account.currency)
    if value.amount < 0:
        raise ProblemError(
            422, "invalid-valuation", "Invalid valuation", "A value is zero or more."
        )
    if valued_on > today():
        raise ProblemError(
            422, "valuation-in-future", "Valuation in the future", "valued_on is after today."
        )
    valuation = db.execute(
        select(AssetValuation).where(
            AssetValuation.household_id == account.household_id,
            AssetValuation.account_id == account.id,
            AssetValuation.valued_on == valued_on,
            AssetValuation.source == ValuationSource.MANUAL,
        )
    ).scalar_one_or_none()
    if valuation is None:
        valuation = AssetValuation(
            household_id=account.household_id,
            account_id=account.id,
            valued_on=valued_on,
            value=value.amount,
            currency=account.currency,
            source=ValuationSource.MANUAL,
        )
        db.add(valuation)
    else:
        valuation.value = value.amount
    db.flush()
    sign = 1 if account.is_asset else -1
    accounts_repo.upsert_snapshot(db, account, valued_on, sign * value.amount, BalanceSource.MANUAL)
    return valuation


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


def list_assets(
    db: Session, principal: Principal, *, cursor: uuid.UUID | None, limit: int
) -> AssetPage:
    _household(principal, HouseholdRole.VIEWER)
    stmt = visible_manual_assets(principal).order_by(ManualAsset.id).limit(limit + 1)
    if cursor is not None:
        stmt = stmt.where(ManualAsset.id > cursor)
    rows = list(db.execute(stmt).scalars())
    more = len(rows) > limit
    rows = rows[:limit]
    return AssetPage(
        items=[asset_out(db, a) for a in rows], next_cursor=rows[-1].id if more and rows else None
    )


def get_asset(db: Session, principal: Principal, asset_id: uuid.UUID) -> AssetOut:
    return asset_out(db, _get(db, principal, asset_id))


def create_asset(
    db: Session, principal: Principal, data: AssetCreate, *, request: HTTPConnection | None
) -> AssetOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    get_visible_account(db, principal, data.account_id)
    if not can_edit_account(db, principal, data.account_id):
        raise forbidden("You can view this account but not change it.")
    account = get_visible_account(db, principal, data.account_id, need="edit", lock=True)
    if account.type is not AccountType.MANUAL_ASSET:
        raise ProblemError(
            422,
            "invalid-asset-account",
            "Not a manual asset account",
            "An asset describes an account of type manual_asset.",
        )
    exists = db.execute(
        select(ManualAsset.id).where(
            ManualAsset.household_id == household_id, ManualAsset.account_id == account.id
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise ProblemError(
            409, "asset-exists", "Asset exists", "This account already has asset details."
        )
    asset = ManualAsset(
        household_id=household_id,
        account_id=account.id,
        description=data.description,
        valuation_method=data.valuation_method,
    )
    db.add(asset)
    db.flush()
    if data.value is not None:
        _upsert_valuation(db, account, data.valued_on or today(), data.value)
    _record(
        db,
        principal,
        asset,
        "create",
        event="created",
        after=_state(asset, account),
        request=request,
    )
    return asset_out(db, asset, account)


def update_asset(
    db: Session,
    principal: Principal,
    asset_id: uuid.UUID,
    data: AssetUpdate,
    *,
    request: HTTPConnection | None,
) -> AssetOut:
    asset = require_editable(db, principal, asset_id)
    account = _account(db, asset, lock=True)
    before = _state(asset, account)
    if data.kind is not None:
        account.asset_kind = data.kind
    if "description" in data.model_fields_set:
        asset.description = data.description
    if data.valuation_method is not None:
        asset.valuation_method = data.valuation_method
    db.flush()
    _record(
        db, principal, asset, "update", before=before, after=_state(asset, account), request=request
    )
    return asset_out(db, asset, account)


def delete_asset(
    db: Session, principal: Principal, asset_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    """Delete the asset's details; the account and its valuations stay."""
    asset = require_editable(db, principal, asset_id)
    account = _account(db, asset)
    _record(
        db,
        principal,
        asset,
        "delete",
        event="deleted",
        before=_state(asset, account),
        request=request,
    )
    db.delete(asset)
    db.flush()


# ---------------------------------------------------------------------------
# Valuations
# ---------------------------------------------------------------------------


def list_valuations(
    db: Session,
    principal: Principal,
    asset_id: uuid.UUID,
    *,
    start: date | None,
    end: date | None,
    limit: int,
) -> ValuationList:
    asset = _get(db, principal, asset_id)
    stmt = select(AssetValuation).where(
        AssetValuation.household_id == asset.household_id,
        AssetValuation.account_id == asset.account_id,
    )
    if start is not None:
        stmt = stmt.where(AssetValuation.valued_on >= start)
    if end is not None:
        stmt = stmt.where(AssetValuation.valued_on <= end)
    rows = db.execute(
        stmt.order_by(AssetValuation.valued_on.desc(), AssetValuation.id.desc()).limit(limit)
    ).scalars()
    return ValuationList(items=[_valuation_out(v) for v in rows])


def add_valuation(
    db: Session,
    principal: Principal,
    asset_id: uuid.UUID,
    data: ValuationCreate,
    *,
    request: HTTPConnection | None,
) -> ValuationOut:
    asset = require_editable(db, principal, asset_id)
    account = _account(db, asset, lock=True)
    valuation = _upsert_valuation(db, account, data.valued_on, data.value)
    _record(
        db,
        principal,
        asset,
        "valuation_add",
        target_type="asset_valuation",
        target_id=valuation.id,
        after={"valued_on": data.valued_on.isoformat(), "value": str(data.value.amount)},
        request=request,
    )
    return _valuation_out(valuation)


def delete_valuation(
    db: Session,
    principal: Principal,
    asset_id: uuid.UUID,
    valuation_id: uuid.UUID,
    *,
    request: HTTPConnection | None,
) -> None:
    asset = require_editable(db, principal, asset_id)
    account = _account(db, asset, lock=True)
    valuation = db.execute(
        select(AssetValuation).where(
            AssetValuation.household_id == asset.household_id,
            AssetValuation.account_id == asset.account_id,
            AssetValuation.id == valuation_id,
        )
    ).scalar_one_or_none()
    if valuation is None:
        raise valuation_not_found()
    before = {
        "valued_on": valuation.valued_on.isoformat(),
        "value": str(valuation.value),
        "source": valuation.source.value,
    }
    if valuation.source is ValuationSource.MANUAL:
        sign = 1 if account.is_asset else -1
        db.execute(
            delete(BalanceSnapshot).where(
                BalanceSnapshot.household_id == account.household_id,
                BalanceSnapshot.account_id == account.id,
                BalanceSnapshot.as_of_date == valuation.valued_on,
                BalanceSnapshot.source == BalanceSource.MANUAL,
                BalanceSnapshot.balance == sign * valuation.value,
            )
        )
    db.delete(valuation)
    db.flush()
    _record(
        db,
        principal,
        asset,
        "valuation_delete",
        target_type="asset_valuation",
        target_id=valuation_id,
        before=before,
        request=request,
    )

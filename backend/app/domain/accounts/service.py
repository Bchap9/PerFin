# SPDX-License-Identifier: AGPL-3.0-or-later
"""Accounts, balances and account grants (API.md ``/accounts``, AUTH.md).

Who may do what (the router guards check role and token scope first; every
function here checks again, so a service call can never widen access):

* **Read** (``GET /accounts``, ``/accounts/{id}``, ``/accounts/{id}/balances``):
  any role, but only the accounts the caller can see
  (``app.domain.accounts.visibility``: owners and admins see all, members and
  viewers what ``account_grant`` gives them). A hidden account is ``404
  account-not-found``, exactly like another household's or a missing one.
* **Create** (``POST /accounts``): member or above. A member who creates an
  account gets an ``edit`` grant on it (otherwise they could not see what
  they just created); owners and admins see every account anyway.
* **Change** (``PATCH``, close, reopen, manual balance): member or above with
  ``edit`` on the account (owner/admin always); an account the caller can
  only view is ``403 forbidden``.
* **Delete**: owner or admin. Transactions, snapshots, schedules and grants of
  the account go with it (foreign keys cascade).
* **Grants**: owner or admin, on a browser session with step-up (router);
  members and viewers read their own grants.

Every write is audited (``app.core.audit``, without ``number_last4``) and
emits a household event (``account.created``, ``account.updated``,
``account.closed``, ``account.reopened``, ``account.deleted``,
``account.balance_recorded``, ``account_grant.changed``) whose payload holds
ids only, so the SPA refetches over the API (which applies visibility).
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import AccountGrant, GrantLevel, HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.money import Money
from app.domain.accounts import balances, repo
from app.domain.accounts.models import (
    Account,
    AccountType,
    BalanceSnapshot,
    BalanceSource,
    Institution,
    default_is_asset,
    default_manual_balance,
)
from app.domain.accounts.schemas import (
    AccountClose,
    AccountCreate,
    AccountOut,
    AccountPage,
    AccountUpdate,
    BalanceHistoryOut,
    BalancePoint,
    ManualBalanceIn,
    SnapshotOut,
)
from app.domain.accounts.visibility import (
    account_not_found,
    can_edit_account,
    get_visible_account,
    sees_all_accounts,
    visible_account_filter,
    visible_account_ids,
)
from app.domain.households import repo as households_repo
from app.domain.households import service as households_service
from app.domain.transactions.import_hash import normalize_payee

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

#: Default and longest range of ``GET /accounts/{id}/balances`` (days, inclusive).
DEFAULT_HISTORY_DAYS: Final = 90
MAX_HISTORY_DAYS: Final = 3660


# ---------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------


def institution_not_found() -> ProblemError:
    return ProblemError(404, "institution-not-found", "Institution not found")


def _currency_mismatch(currency: str) -> ProblemError:
    return ProblemError(
        422,
        "currency-mismatch",
        "Currency mismatch",
        f"Balances of this account are in {currency}.",
        extra={"currency": currency},
    )


def _currency_locked() -> ProblemError:
    return ProblemError(
        409,
        "account-currency-locked",
        "Currency cannot change",
        "The account has transactions, balances or schedules in its current currency.",
    )


def _invalid_dates(detail: str) -> ProblemError:
    return ProblemError(422, "invalid-account-dates", "Invalid account dates", detail)


def _asset_kind_problem() -> ProblemError:
    return ProblemError(
        422,
        "invalid-asset-kind",
        "Invalid asset kind",
        "asset_kind is required for manual_asset accounts and only for them.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _household(principal: Principal) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    return principal.household_id


def _require_role(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    household_id = _household(principal)
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return household_id


def today() -> date:
    """The date balances are reported for (UTC)."""
    return clock.utcnow().date()


def _audit_state(account: Account) -> dict[str, Any]:
    """What the audit trail keeps of an account (never ``number_last4``, H8)."""
    return {
        "type": account.type.value,
        "asset_kind": account.asset_kind.value if account.asset_kind else None,
        "name": account.name,
        "nickname": account.nickname,
        "currency": account.currency,
        "institution_id": str(account.institution_id) if account.institution_id else None,
        "has_number_last4": account.number_last4 is not None,
        "is_asset": account.is_asset,
        "manual_balance": account.manual_balance,
        "include_in_net_worth": account.include_in_net_worth,
        "opened_on": account.opened_on.isoformat() if account.opened_on else None,
        "closed_on": account.closed_on.isoformat() if account.closed_on else None,
    }


def _record(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    *,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    event: str,
    payload: dict[str, Any],
    before: Any = None,
    after: Any = None,
    request: HTTPConnection | None,
) -> None:
    audit(
        db,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(db, household_id, event, payload)


def account_out(account: Account, *, balance: Decimal, on: date, can_edit: bool) -> AccountOut:
    return AccountOut(
        id=account.id,
        type=account.type,
        asset_kind=account.asset_kind,
        name=account.name,
        nickname=account.nickname,
        currency=account.currency,
        institution_id=account.institution_id,
        number_last4=account.number_last4,
        is_asset=account.is_asset,
        manual_balance=account.manual_balance,
        include_in_net_worth=account.include_in_net_worth,
        opened_on=account.opened_on,
        closed_on=account.closed_on,
        is_closed=account.closed_on is not None,
        balance=Money.of(balance, account.currency),
        balance_date=on,
        can_edit=can_edit,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def snapshot_out(snapshot: BalanceSnapshot) -> SnapshotOut:
    return SnapshotOut(
        id=snapshot.id,
        as_of_date=snapshot.as_of_date,
        balance=Money.of(snapshot.balance, snapshot.currency),
        source=snapshot.source,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
    )


def _view(db: Session, principal: Principal, account: Account, *, can_edit: bool) -> AccountOut:
    on = today()
    return account_out(
        account, balance=balances.balance_as_of(db, account, on), on=on, can_edit=can_edit
    )


def _check_institution(db: Session, household_id: uuid.UUID, institution_id: uuid.UUID) -> None:
    if not repo.institution_exists(db, household_id, institution_id):
        raise institution_not_found()


#: Longest institution name kept (the column's limit).
INSTITUTION_NAME_MAX: Final = 200


def find_or_create_institution(
    db: Session, principal: Principal, name: str, *, request: HTTPConnection | None
) -> uuid.UUID:
    """The household's institution called ``name`` (compared normalised), else a new one.

    Member or above (the caller is creating an account at it). A new
    institution is audited (``institution.create``) and emits
    ``institution.created``.
    """
    household_id = _require_role(principal, HouseholdRole.MEMBER)
    clean = " ".join(name.split())[:INSTITUTION_NAME_MAX]
    wanted = normalize_payee(clean)
    if not wanted:
        raise ProblemError(
            422, "invalid-institution", "Invalid institution", "Name the institution."
        )
    rows = db.execute(
        select(Institution.id, Institution.name).where(Institution.household_id == household_id)
    ).all()
    for institution_id, existing in rows:
        if normalize_payee(existing) == wanted:
            return institution_id
    institution = Institution(household_id=household_id, name=clean)
    db.add(institution)
    db.flush()
    _record(
        db,
        principal,
        household_id,
        action="institution.create",
        target_type="institution",
        target_id=institution.id,
        event="institution.created",
        payload={"institution_id": str(institution.id)},
        after={"name": clean},
        request=request,
    )
    return institution.id


def _editable(db: Session, principal: Principal, account_id: uuid.UUID) -> Account:
    """An account the caller may change (member+ with ``edit``), locked.

    Hidden is ``404 account-not-found``; visible but read-only (a ``view``
    grant) is ``403 forbidden``.
    """
    _require_role(principal, HouseholdRole.MEMBER)
    get_visible_account(db, principal, account_id)
    if not can_edit_account(db, principal, account_id):
        raise forbidden("You can view this account but not change it.")
    return get_visible_account(db, principal, account_id, need="edit", lock=True)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_accounts(
    db: Session,
    principal: Principal,
    *,
    cursor: uuid.UUID | None,
    limit: int,
    closed: bool | None = None,
    account_type: AccountType | None = None,
) -> AccountPage:
    """A page of the accounts the caller can see, in creation order."""
    household_id = _household(principal)
    rows = repo.list_accounts(
        db,
        household_id,
        visible=visible_account_filter(principal, Account.id),
        after=cursor,
        limit=limit + 1,
        closed=closed,
        account_type=account_type,
    )
    more = len(rows) > limit
    page = rows[:limit]
    on = today()
    amounts = balances.balances_as_of(db, page, on)
    editable = (
        {a.id for a in page}
        if sees_all_accounts(principal)
        else set(visible_account_ids(db, principal, need="edit"))
    )
    if not role_at_least(principal.role, HouseholdRole.MEMBER):
        editable = set()
    items = [account_out(a, balance=amounts[a.id], on=on, can_edit=a.id in editable) for a in page]
    return AccountPage(items=items, next_cursor=page[-1].id if more and page else None)


def get_account(db: Session, principal: Principal, account_id: uuid.UUID) -> AccountOut:
    _household(principal)
    account = get_visible_account(db, principal, account_id)
    return _view(db, principal, account, can_edit=_can_edit(db, principal, account_id))


def require_visible(db: Session, principal: Principal, account_id: uuid.UUID) -> None:
    """``404 account-not-found`` unless the caller can see the account."""
    _household(principal)
    get_visible_account(db, principal, account_id)


def _can_edit(db: Session, principal: Principal, account_id: uuid.UUID) -> bool:
    if not role_at_least(principal.role, HouseholdRole.MEMBER):
        return False
    return can_edit_account(db, principal, account_id)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_account(
    db: Session, principal: Principal, data: AccountCreate, *, request: HTTPConnection | None
) -> AccountOut:
    household_id = _require_role(principal, HouseholdRole.MEMBER)
    currency = data.currency
    if currency is None:
        household = households_repo.get_household(db, household_id)
        currency = household.base_currency if household is not None else "USD"
    if data.institution_id is not None:
        _check_institution(db, household_id, data.institution_id)
    if data.opening_balance is not None and data.opening_balance.currency != currency:
        raise _currency_mismatch(currency)
    account = Account(
        household_id=household_id,
        type=data.type,
        asset_kind=data.asset_kind,
        name=data.name,
        nickname=data.nickname,
        currency=currency,
        institution_id=data.institution_id,
        number_last4=data.number_last4,
        is_asset=default_is_asset(data.type) if data.is_asset is None else data.is_asset,
        manual_balance=(
            default_manual_balance(data.type)
            if data.manual_balance is None
            else data.manual_balance
        ),
        include_in_net_worth=data.include_in_net_worth,
        opened_on=data.opened_on,
    )
    db.add(account)
    db.flush()
    if not sees_all_accounts(principal):
        # The creating member keeps access to what they created.
        households_repo.upsert_grant(
            db, household_id, principal.user_id, account.id, GrantLevel.EDIT, now=clock.utcnow()
        )
    if data.opening_balance is not None:
        as_of = data.opening_balance_date or data.opened_on or today()
        repo.upsert_snapshot(db, account, as_of, data.opening_balance.amount, BalanceSource.MANUAL)
    _record(
        db,
        principal,
        household_id,
        action="account.create",
        target_type="account",
        target_id=account.id,
        after={
            **_audit_state(account),
            "opening_balance": (
                None if data.opening_balance is None else str(data.opening_balance.amount)
            ),
        },
        event="account.created",
        payload={"account_id": str(account.id)},
        request=request,
    )
    return _view(db, principal, account, can_edit=True)


def update_account(
    db: Session,
    principal: Principal,
    account_id: uuid.UUID,
    data: AccountUpdate,
    *,
    request: HTTPConnection | None,
) -> AccountOut:
    account = _editable(db, principal, account_id)
    household_id = account.household_id
    before = _audit_state(account)
    given = data.model_fields_set
    new_type = data.type if "type" in given and data.type is not None else account.type
    if "type" in given and new_type is not account.type:
        account.type = new_type
        if "is_asset" not in given:
            account.is_asset = default_is_asset(new_type)
        if "manual_balance" not in given:
            account.manual_balance = default_manual_balance(new_type)
        if new_type is not AccountType.MANUAL_ASSET and "asset_kind" not in given:
            account.asset_kind = None
    if "asset_kind" in given:
        account.asset_kind = data.asset_kind
    if (account.type is AccountType.MANUAL_ASSET) != (account.asset_kind is not None):
        raise _asset_kind_problem()
    if "currency" in given and data.currency is not None and data.currency != account.currency:
        if repo.has_ledger_rows(db, account):
            raise _currency_locked()
        account.currency = data.currency
    if "institution_id" in given:
        if data.institution_id is not None:
            _check_institution(db, household_id, data.institution_id)
        account.institution_id = data.institution_id
    if "opened_on" in given:
        if (
            data.opened_on is not None
            and account.closed_on is not None
            and data.opened_on > account.closed_on
        ):
            raise _invalid_dates("opened_on cannot be after closed_on.")
        account.opened_on = data.opened_on
    for name in ("name", "nickname", "number_last4"):
        if name in given:
            setattr(account, name, getattr(data, name))
    for name in ("is_asset", "manual_balance", "include_in_net_worth"):
        if name in given and getattr(data, name) is not None:
            setattr(account, name, getattr(data, name))
    after = _audit_state(account)
    changed = {k: v for k, v in after.items() if before[k] != v}
    # number_last4 changes are recorded only as "changed" (never the digits).
    last4_changed = "number_last4" in given
    if changed or last4_changed:
        account.updated_at = clock.utcnow()
        db.flush()
        _record(
            db,
            principal,
            household_id,
            action="account.update",
            target_type="account",
            target_id=account.id,
            before={k: before[k] for k in changed},
            after={**changed, **({"number_last4": "changed"} if last4_changed else {})},
            event="account.updated",
            payload={"account_id": str(account.id)},
            request=request,
        )
    return _view(db, principal, account, can_edit=True)


def close_account(
    db: Session,
    principal: Principal,
    account_id: uuid.UUID,
    data: AccountClose,
    *,
    request: HTTPConnection | None,
) -> AccountOut:
    account = _editable(db, principal, account_id)
    if account.closed_on is not None:
        raise ProblemError(409, "account-closed", "Account already closed")
    closed_on = data.closed_on or today()
    if closed_on > today():
        raise _invalid_dates("closed_on cannot be in the future.")
    if account.opened_on is not None and closed_on < account.opened_on:
        raise _invalid_dates("closed_on cannot be before opened_on.")
    account.closed_on = closed_on
    account.updated_at = clock.utcnow()
    db.flush()
    _record(
        db,
        principal,
        account.household_id,
        action="account.close",
        target_type="account",
        target_id=account.id,
        after={"closed_on": closed_on.isoformat()},
        event="account.closed",
        payload={"account_id": str(account.id)},
        request=request,
    )
    return _view(db, principal, account, can_edit=True)


def reopen_account(
    db: Session, principal: Principal, account_id: uuid.UUID, *, request: HTTPConnection | None
) -> AccountOut:
    account = _editable(db, principal, account_id)
    if account.closed_on is None:
        raise ProblemError(409, "account-not-closed", "Account is not closed")
    previous = account.closed_on
    account.closed_on = None
    account.updated_at = clock.utcnow()
    db.flush()
    _record(
        db,
        principal,
        account.household_id,
        action="account.reopen",
        target_type="account",
        target_id=account.id,
        before={"closed_on": previous.isoformat()},
        event="account.reopened",
        payload={"account_id": str(account.id)},
        request=request,
    )
    return _view(db, principal, account, can_edit=True)


def delete_account(
    db: Session, principal: Principal, account_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    """Delete an account and everything recorded on it (owner/admin)."""
    _require_role(principal, HouseholdRole.ADMIN)
    account = get_visible_account(db, principal, account_id, need="edit", lock=True)
    state = _audit_state(account)
    repo.delete_account(db, account)
    _record(
        db,
        principal,
        account.household_id,
        action="account.delete",
        target_type="account",
        target_id=account.id,
        before=state,
        event="account.deleted",
        payload={"account_id": str(account.id)},
        request=request,
    )


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------


def balance_history(
    db: Session,
    principal: Principal,
    account_id: uuid.UUID,
    *,
    start: date | None,
    end: date | None,
) -> BalanceHistoryOut:
    """End-of-day balances for ``start..end`` (default: the last 90 days to today)."""
    _household(principal)
    account = get_visible_account(db, principal, account_id)
    last = end or today()
    first = start or last - timedelta(days=DEFAULT_HISTORY_DAYS - 1)
    if first > last:
        raise ProblemError(422, "invalid-range", "Invalid range", "start must not be after end.")
    if (last - first).days + 1 > MAX_HISTORY_DAYS:
        raise ProblemError(
            422,
            "range-too-long",
            "Range too long",
            f"Ask for at most {MAX_HISTORY_DAYS} days at a time.",
            extra={"max_days": MAX_HISTORY_DAYS},
        )
    series = balances.balance_history(db, account, first, last)
    return BalanceHistoryOut(
        account_id=account.id,
        currency=account.currency,
        start=first,
        end=last,
        items=[
            BalancePoint(date=day.on, balance=Money.of(day.balance, account.currency))
            for day in series
        ],
        snapshots=[snapshot_out(s) for s in repo.list_snapshots(db, account, first, last)],
    )


def record_manual_balance(
    db: Session,
    principal: Principal,
    account_id: uuid.UUID,
    data: ManualBalanceIn,
    *,
    request: HTTPConnection | None,
) -> SnapshotOut:
    """Create or replace the manual balance of ``data.as_of_date``."""
    account = _editable(db, principal, account_id)
    if data.balance.currency != account.currency:
        raise _currency_mismatch(account.currency)
    if data.as_of_date > today():
        raise ProblemError(
            422, "balance-in-future", "Balance date in the future", "as_of_date is after today."
        )
    snapshot = repo.upsert_snapshot(
        db, account, data.as_of_date, data.balance.amount, BalanceSource.MANUAL
    )
    _record(
        db,
        principal,
        account.household_id,
        action="account.balance_record",
        target_type="balance_snapshot",
        target_id=snapshot.id,
        after={
            "account_id": str(account.id),
            "as_of_date": data.as_of_date.isoformat(),
            "balance": str(data.balance.amount),
        },
        event="account.balance_recorded",
        payload={"account_id": str(account.id), "snapshot_id": str(snapshot.id)},
        request=request,
    )
    return snapshot_out(snapshot)


# ---------------------------------------------------------------------------
# Account grants
# ---------------------------------------------------------------------------


def _member_or_404(db: Session, household_id: uuid.UUID, user_id: uuid.UUID) -> None:
    if households_repo.get_member(db, household_id, user_id) is None:
        raise households_service.member_not_found()


def member_grants(db: Session, principal: Principal, user_id: uuid.UUID) -> list[AccountGrant]:
    """A member's grants: any for owner/admin, one's own for everybody else."""
    household_id = _household(principal)
    if user_id != principal.user_id and not role_at_least(principal.role, HouseholdRole.ADMIN):
        raise forbidden()
    _member_or_404(db, household_id, user_id)
    return households_service.list_account_grants(db, principal, user_id=user_id)


def account_grants(db: Session, principal: Principal, account_id: uuid.UUID) -> list[AccountGrant]:
    """Every grant on one account (owner/admin)."""
    household_id = _require_role(principal, HouseholdRole.ADMIN)
    account = get_visible_account(db, principal, account_id)
    return repo.grants_on_account(db, household_id, account.id)


def set_grant(
    db: Session,
    principal: Principal,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    level: GrantLevel,
    *,
    request: HTTPConnection | None,
) -> AccountGrant:
    """Create or change a grant (owner/admin; step-up checked by the router)."""
    household_id = _require_role(principal, HouseholdRole.ADMIN)
    before = households_repo.get_grant(db, household_id, user_id, account_id)
    previous = None if before is None else before.level
    grant = households_service.set_account_grant(
        db, principal, user_id, account_id, level, request=request
    )
    if previous is not level:
        emit(
            db,
            household_id,
            "account_grant.changed",
            {"user_id": str(user_id), "account_id": str(account_id)},
        )
    return grant


def revoke_grant(
    db: Session,
    principal: Principal,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    request: HTTPConnection | None,
) -> None:
    """Remove a grant (owner/admin); ``404`` for an unknown member or account."""
    household_id = _require_role(principal, HouseholdRole.ADMIN)
    _member_or_404(db, household_id, user_id)
    if repo.get_account(db, household_id, account_id) is None:
        raise account_not_found()
    if households_service.revoke_account_grant(db, principal, user_id, account_id, request=request):
        emit(
            db,
            household_id,
            "account_grant.changed",
            {"user_id": str(user_id), "account_id": str(account_id)},
        )

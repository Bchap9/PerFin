# SPDX-License-Identifier: AGPL-3.0-or-later
"""Account-level visibility (AUTH.md "Authorisation model", SECURITY.md F3/I3).

Who may see or change which account of the active household:

* **owner, admin**: every account (view and edit);
* **member**: the accounts with an ``account_grant`` of level ``view``
  (read) or ``edit`` (read and write);
* **viewer**: the accounts granted ``view`` or ``edit``, read-only (a viewer
  never edits, whatever the grant says; ``set_account_grant`` refuses
  ``edit`` for viewers anyway);
* a grant of level ``none``, or no grant, hides the account.

A personal access token acts with its user's role and grants in the token's
household (its scopes are checked separately by ``require_scope``).

Everything derived from accounts (transactions, splits, balances, budgets
spent, goals' progress, reports) must be restricted the same way: filter SQL
with :func:`visible_account_filter` or use :func:`visible_account_ids`, and
load single objects with :func:`get_visible_account`. A hidden account is
``404 account-not-found``, exactly like an account of another household, so
its existence is not revealed.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Literal

from sqlalchemy import ColumnElement, false, select, true
from sqlalchemy.orm import QueryableAttribute, Session

from app.auth.authz import role_at_least
from app.auth.models import AccountGrant, GrantLevel, HouseholdRole
from app.auth.principal import Principal
from app.core.errors import ProblemError
from app.domain.accounts import repo
from app.domain.accounts.models import Account

Need = Literal["view", "edit"]

_LEVELS: dict[Need, tuple[GrantLevel, ...]] = {
    "view": (GrantLevel.VIEW, GrantLevel.EDIT),
    "edit": (GrantLevel.EDIT,),
}


def account_not_found() -> ProblemError:
    return ProblemError(404, "account-not-found", "Account not found")


def sees_all_accounts(principal: Principal) -> bool:
    """Owners and admins see (and may edit) every account of their household."""
    return principal.household_id is not None and role_at_least(principal.role, HouseholdRole.ADMIN)


def _may_use_grants(principal: Principal, need: Need) -> bool:
    if principal.household_id is None or principal.role is None:
        return False
    return not (need == "edit" and principal.role is HouseholdRole.VIEWER)


def visible_account_filter(
    principal: Principal,
    account_id_column: ColumnElement[uuid.UUID] | QueryableAttribute[uuid.UUID],
    *,
    need: Need = "view",
) -> ColumnElement[bool]:
    """SQL predicate on an ``account_id`` column: the principal may ``need`` that account.

    Combine with the query's own ``household_id`` filter, e.g.::

        select(Transaction).where(
            Transaction.household_id == principal.household_id,
            visible_account_filter(principal, Transaction.account_id),
        )
    """
    if sees_all_accounts(principal):
        return true()
    if not _may_use_grants(principal, need):
        return false()
    granted = select(AccountGrant.account_id).where(
        AccountGrant.household_id == principal.household_id,
        AccountGrant.user_id == principal.user_id,
        AccountGrant.level.in_(_LEVELS[need]),
    )
    return account_id_column.in_(granted)


def visible_account_ids(
    db: Session, principal: Principal, *, need: Need = "view"
) -> list[uuid.UUID]:
    """Ids of the accounts of the active household the principal may ``need``."""
    if principal.household_id is None:
        return []
    return list(
        db.execute(
            select(Account.id)
            .where(
                Account.household_id == principal.household_id,
                visible_account_filter(principal, Account.id, need=need),
            )
            .order_by(Account.id)
        ).scalars()
    )


def _allowed(db: Session, principal: Principal, account_id: uuid.UUID, need: Need) -> bool:
    if principal.household_id is None:
        return False
    found = db.execute(
        select(Account.id).where(
            Account.household_id == principal.household_id,
            Account.id == account_id,
            visible_account_filter(principal, Account.id, need=need),
        )
    ).scalar_one_or_none()
    return found is not None


def can_view_account(db: Session, principal: Principal, account_id: uuid.UUID) -> bool:
    """The account exists in the active household and the principal may read it."""
    return _allowed(db, principal, account_id, "view")


def can_edit_account(db: Session, principal: Principal, account_id: uuid.UUID) -> bool:
    """The account exists in the active household and the principal may change it."""
    return _allowed(db, principal, account_id, "edit")


def get_visible_account(
    db: Session,
    principal: Principal,
    account_id: uuid.UUID,
    *,
    need: Need = "view",
    lock: bool = False,
) -> Account:
    """The account, or ``404 account-not-found`` (hidden, foreign or missing alike)."""
    if principal.household_id is None or not _allowed(db, principal, account_id, need):
        raise account_not_found()
    account = repo.get_account(db, principal.household_id, account_id, lock=lock)
    if account is None:  # deleted in between
        raise account_not_found()
    return account


def require_visible_accounts(
    db: Session, principal: Principal, account_ids: Collection[uuid.UUID], *, need: Need = "view"
) -> None:
    """``404 account-not-found`` unless the principal may ``need`` every one of ``account_ids``."""
    wanted = set(account_ids)
    if not wanted:
        return
    if principal.household_id is None:
        raise account_not_found()
    allowed = set(
        db.execute(
            select(Account.id).where(
                Account.household_id == principal.household_id,
                Account.id.in_(wanted),
                visible_account_filter(principal, Account.id, need=need),
            )
        ).scalars()
    )
    if allowed != wanted:
        raise account_not_found()

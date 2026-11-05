# SPDX-License-Identifier: AGPL-3.0-or-later
"""Account grants: which accounts a member or viewer may see or change (AUTH.md).

* ``GET /households/{household_id}/members/{user_id}/grants``: a member's
  grants. Owners and admins read anyone's; members and viewers only their
  own (``403`` for someone else's). Tokens need ``households:read``.
* ``PUT /households/{household_id}/members/{user_id}/grants/{account_id}``
  (``{"level": "none" | "view" | "edit"}``) and ``DELETE`` of the same path:
  owner or admin on a browser session with step-up MFA (SECURITY.md I2: a
  grant widens what a member sees). Owners and admins themselves get no
  grants (``422 grant-not-applicable``), viewers at most ``view`` (``422
  grant-level-not-allowed``).
* ``GET /accounts/{account_id}/grants``: every grant on one account (owner
  or admin; tokens need ``accounts:read``).

The household in the path must be the caller's active one (``404
household-not-found``); an unknown member is ``404 member-not-found``, an
account of another household ``404 account-not-found``. Changes are audited
(``account_grant.set`` / ``account_grant.revoke``) and emit
``account_grant.changed``; visibility applies from the next request.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.routers.accounts import PROBLEMS, scoped_role
from app.auth.authz import (
    Authenticated,
    forbidden,
    require_scope,
    require_step_up,
    role_at_least,
)
from app.auth.deps import DbSession
from app.auth.models import AccountGrant, HouseholdRole
from app.auth.principal import Principal
from app.domain.accounts import service
from app.domain.accounts.schemas import AccountGrantLevelSet
from app.domain.households.schemas import AccountGrantOut
from app.domain.households.service import active_household_id

router = APIRouter(tags=["account grants"])

_households_read = require_scope("households:read")


def require_grant_reader(household_id: uuid.UUID, principal: Authenticated) -> Principal:
    """The active household (else ``404``), any role, ``households:read`` for tokens."""
    active_household_id(principal, household_id)
    if principal.role is None:
        raise forbidden("You are not a member of a household.")
    return _households_read(principal)


def require_grant_manager(household_id: uuid.UUID, principal: Authenticated) -> Principal:
    """The active household, owner or admin, browser session with fresh step-up (I2)."""
    active_household_id(principal, household_id)
    if not role_at_least(principal.role, HouseholdRole.ADMIN):
        raise forbidden()
    return require_step_up(principal)


GrantReader = Annotated[Principal, Depends(require_grant_reader)]
GrantManager = Annotated[Principal, Depends(require_grant_manager)]
AccountAdmin = Annotated[Principal, Depends(scoped_role("accounts:read", HouseholdRole.ADMIN))]


def _out(grant: AccountGrant) -> AccountGrantOut:
    return AccountGrantOut(
        id=grant.id,
        user_id=grant.user_id,
        account_id=grant.account_id,
        level=grant.level,
        updated_at=grant.updated_at,
    )


@router.get(
    "/households/{household_id}/members/{user_id}/grants",
    response_model=list[AccountGrantOut],
    responses=PROBLEMS,
    summary="A member's account grants (owner/admin, or one's own)",
)
def get_member_grants(
    household_id: uuid.UUID, user_id: uuid.UUID, db: DbSession, principal: GrantReader
) -> list[AccountGrantOut]:
    return [_out(g) for g in service.member_grants(db, principal, user_id)]


@router.put(
    "/households/{household_id}/members/{user_id}/grants/{account_id}",
    response_model=AccountGrantOut,
    responses=PROBLEMS,
    summary="Set a member's access to an account (owner/admin, step-up)",
)
def put_member_grant(
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    body: AccountGrantLevelSet,
    request: Request,
    db: DbSession,
    principal: GrantManager,
) -> AccountGrantOut:
    grant = service.set_grant(db, principal, user_id, account_id, body.level, request=request)
    return _out(grant)


@router.delete(
    "/households/{household_id}/members/{user_id}/grants/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Remove a member's grant on an account (owner/admin, step-up)",
)
def delete_member_grant(
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    request: Request,
    db: DbSession,
    principal: GrantManager,
) -> Response:
    service.revoke_grant(db, principal, user_id, account_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/accounts/{account_id}/grants",
    response_model=list[AccountGrantOut],
    responses=PROBLEMS,
    summary="Who has a grant on this account (owner/admin)",
)
def get_account_grants(
    account_id: uuid.UUID, db: DbSession, principal: AccountAdmin
) -> list[AccountGrantOut]:
    return [_out(g) for g in service.account_grants(db, principal, account_id)]

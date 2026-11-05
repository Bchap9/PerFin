# SPDX-License-Identifier: AGPL-3.0-or-later
"""Households, members and household switching (API.md "Endpoint families").

* ``GET /households``: the caller's households with their role; for an
  instance admin (browser session) every household (``role`` null where they
  are not a member).
* ``POST /households`` (instance admin): a new household with one owner.
* ``GET/PATCH/DELETE /households/{id}``: the caller's **active** household
  only; any other id is ``404 household-not-found`` (F3). ``PATCH`` (name,
  base currency) needs owner or admin, ``DELETE`` an owner with step-up.
* ``GET /households/{id}/members``: any role. ``POST``, ``PATCH
  /members/{user_id}`` and ``DELETE /members/{user_id}``: owner or admin on a
  browser session with step-up MFA (I2); the rules for owners are in
  ``app.domain.households.service``.
* ``POST /auth/session/household``: switch the session to another household
  the caller belongs to; rotates the session (new cookie and CSRF token).

Personal access tokens need the ``households:read`` scope to read and
``households:write`` to change the household; member management and
deletion need step-up, which tokens never have. Input schemas forbid unknown
fields (F4). Business rules live in the
service; this module only resolves the caller and maps to responses.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status

from app.auth.authz import (
    Authenticated,
    forbidden,
    require_instance_admin,
    require_scope,
    require_step_up,
)
from app.auth.deps import DbSession, get_app_settings
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.auth.sessions import SessionPolicy, set_session_cookie
from app.core.csrf import get_csrf
from app.domain.households import service
from app.domain.households.schemas import (
    ActiveHouseholdOut,
    HouseholdCreate,
    HouseholdOut,
    HouseholdSwitch,
    HouseholdSwitchOut,
    HouseholdUpdate,
    MemberAdd,
    MemberOut,
    MemberUpdate,
)

router = APIRouter(tags=["households"])

_PROBLEMS: dict[int | str, dict[str, Any]] = {
    code: {"description": "Problem details", "content": {"application/problem+json": {}}}
    for code in (403, 404, 409, 422)
}


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def require_household_access(household_id: uuid.UUID, principal: Authenticated) -> Principal:
    """The path's household must be the caller's active one (else ``404``, F3)."""
    service.active_household_id(principal, household_id)
    return principal


InHousehold = Annotated[Principal, Depends(require_household_access)]

#: Token scopes for the household endpoints (sessions hold every scope).
SCOPE_READ = "households:read"
SCOPE_WRITE = "households:write"
CanRead = Annotated[Principal, Depends(require_scope(SCOPE_READ))]
CanWrite = Annotated[Principal, Depends(require_scope(SCOPE_WRITE))]


def require_household_read(household_id: uuid.UUID, principal: CanRead) -> Principal:
    service.active_household_id(principal, household_id)
    return principal


def require_household_write(household_id: uuid.UUID, principal: CanWrite) -> Principal:
    service.active_household_id(principal, household_id)
    return principal


HouseholdReader = Annotated[Principal, Depends(require_household_read)]
HouseholdWriter = Annotated[Principal, Depends(require_household_write)]


def require_member_manager(principal: InHousehold) -> Principal:
    """Owner or admin of the active household, browser session, fresh step-up (I2)."""
    if not service.can_manage_members(principal.role):
        raise forbidden()
    return require_step_up(principal)


MemberManager = Annotated[Principal, Depends(require_member_manager)]


def require_owner_step_up(principal: InHousehold) -> Principal:
    if principal.role is not HouseholdRole.OWNER:
        raise forbidden()
    return require_step_up(principal)


OwnerSteppedUp = Annotated[Principal, Depends(require_owner_step_up)]
InstanceAdmin = Annotated[Principal, Depends(require_instance_admin)]


def _household_out(view: service.HouseholdView) -> HouseholdOut:
    h = view.household
    return HouseholdOut(
        id=h.id, name=h.name, base_currency=h.base_currency, created_at=h.created_at, role=view.role
    )


def _member_out(view: service.MemberView) -> MemberOut:
    return MemberOut(
        user_id=view.user.id,
        username=view.user.username,
        display_name=view.user.display_name,
        role=view.member.role,
        is_active=view.user.is_active,
        joined_at=view.member.created_at,
    )


# ---------------------------------------------------------------------------
# Households
# ---------------------------------------------------------------------------


@router.get("/households", response_model=list[HouseholdOut], summary="My households")
def get_households(db: DbSession, principal: CanRead) -> list[HouseholdOut]:
    return [_household_out(v) for v in service.list_households(db, principal)]


@router.post(
    "/households",
    response_model=HouseholdOut,
    status_code=status.HTTP_201_CREATED,
    responses=_PROBLEMS,
    summary="Create a household (instance admin)",
)
def post_household(
    body: HouseholdCreate, request: Request, db: DbSession, principal: InstanceAdmin
) -> HouseholdOut:
    return _household_out(service.create_household(db, principal, body, request=request))


@router.get(
    "/households/{household_id}",
    response_model=HouseholdOut,
    responses=_PROBLEMS,
    summary="My active household",
)
def get_household(
    household_id: uuid.UUID, db: DbSession, principal: HouseholdReader
) -> HouseholdOut:
    return _household_out(service.get_household(db, principal, household_id))


@router.patch(
    "/households/{household_id}",
    response_model=HouseholdOut,
    responses=_PROBLEMS,
    summary="Rename or change the base currency (owner/admin)",
)
def patch_household(
    household_id: uuid.UUID,
    body: HouseholdUpdate,
    request: Request,
    db: DbSession,
    principal: HouseholdWriter,
) -> HouseholdOut:
    view = service.update_household(db, principal, household_id, body, request=request)
    return _household_out(view)


@router.delete(
    "/households/{household_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_PROBLEMS,
    summary="Delete the household and all its data (owner, step-up)",
)
def delete_household(
    household_id: uuid.UUID, request: Request, db: DbSession, principal: OwnerSteppedUp
) -> Response:
    service.delete_household(db, principal, household_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@router.get(
    "/households/{household_id}/members",
    response_model=list[MemberOut],
    responses=_PROBLEMS,
    summary="Members of my active household",
)
def get_members(
    household_id: uuid.UUID, db: DbSession, principal: HouseholdReader
) -> list[MemberOut]:
    return [_member_out(v) for v in service.list_members(db, principal, household_id)]


@router.post(
    "/households/{household_id}/members",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
    responses=_PROBLEMS,
    summary="Add an existing user or create a local user as member (owner/admin, step-up)",
)
def post_member(
    household_id: uuid.UUID,
    body: MemberAdd,
    request: Request,
    db: DbSession,
    principal: MemberManager,
) -> MemberOut:
    settings = get_app_settings(request)
    view = service.add_member(db, settings, principal, household_id, body, request=request)
    return _member_out(view)


@router.patch(
    "/households/{household_id}/members/{user_id}",
    response_model=MemberOut,
    responses=_PROBLEMS,
    summary="Change a member's role (owner/admin, step-up)",
)
def patch_member(
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberUpdate,
    request: Request,
    db: DbSession,
    principal: MemberManager,
) -> MemberOut:
    view = service.change_member_role(
        db, principal, household_id, user_id, body.role, request=request
    )
    return _member_out(view)


@router.delete(
    "/households/{household_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_PROBLEMS,
    summary="Remove a member (owner/admin, step-up)",
)
def delete_member(
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    db: DbSession,
    principal: MemberManager,
) -> Response:
    service.remove_member(db, principal, household_id, user_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Household switching
# ---------------------------------------------------------------------------


@router.post(
    "/auth/session/household",
    response_model=HouseholdSwitchOut,
    responses=_PROBLEMS,
    summary="Act in another of my households (rotates the session)",
)
def post_session_household(
    body: HouseholdSwitch,
    request: Request,
    response: Response,
    db: DbSession,
    principal: Authenticated,
) -> HouseholdSwitchOut:
    settings = get_app_settings(request)
    result = service.switch_household(
        db,
        principal,
        body.household_id,
        policy=SessionPolicy.from_settings(settings),
        request=request,
    )
    set_session_cookie(response, settings, result.issued)
    return HouseholdSwitchOut(
        household=ActiveHouseholdOut(
            id=result.household.id, name=result.household.name, role=result.role
        ),
        csrf_token=get_csrf(request).token_for_session(result.issued.raw_id),
    )

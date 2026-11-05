# SPDX-License-Identifier: AGPL-3.0-or-later
"""Instance administration of users (AUTH.md: "instance admin -> manages users").

All endpoints need the local ``is_instance_admin`` flag on a browser session
(``app.auth.authz.require_instance_admin``; tokens never administer the
instance). Every change additionally needs step-up MFA within the last ten
minutes (A6/I2), and nobody changes their own account here.

* ``GET /admin/users?cursor=&limit=``: users in creation order,
  ``{"items": [...], "next_cursor": ...}``; no secrets, only whether a local
  password exists and which factor types are enrolled.
* ``GET /admin/users/{id}``.
* ``POST /admin/users``: a local account (password policy incl. breached
  list); never an instance admin or break-glass account.
* ``PATCH /admin/users/{id}``: ``is_active`` (deactivation revokes every
  session and token), ``is_breakglass`` (needs a local password; ends every
  session and API token of the account, B7) and ``email_verified`` (the
  admin vouches for the address, which OIDC link-by-verified-email needs;
  ``409 no-email`` without one). The instance-admin flag cannot be set
  through the API.
* ``POST /admin/users/{id}/mfa-reset``: removes every factor and ends the
  user's sessions.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict

from app.auth.authz import require_instance_admin, require_step_up
from app.auth.deps import DbSession, get_app_settings
from app.auth.principal import Principal
from app.domain.households import users
from app.domain.households.schemas import (
    AdminUserCreate,
    AdminUserOut,
    AdminUserPage,
    AdminUserUpdate,
)

router = APIRouter(tags=["admin"])

_PROBLEMS: dict[int | str, dict[str, Any]] = {
    code: {"description": "Problem details", "content": {"application/problem+json": {}}}
    for code in (403, 404, 409, 422)
}

InstanceAdmin = Annotated[Principal, Depends(require_instance_admin)]


def require_admin_step_up(principal: InstanceAdmin) -> Principal:
    return require_step_up(principal)


SteppedUpAdmin = Annotated[Principal, Depends(require_admin_step_up)]


class MfaResetOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    factors_removed: int


@router.get("/admin/users", response_model=AdminUserPage, summary="List users (instance admin)")
def get_users(
    db: DbSession,
    principal: InstanceAdmin,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=users.MAX_PAGE)] = 100,
) -> AdminUserPage:
    return users.list_users_page(db, cursor=cursor, limit=limit)


@router.get(
    "/admin/users/{user_id}",
    response_model=AdminUserOut,
    responses=_PROBLEMS,
    summary="One user (instance admin)",
)
def get_user(user_id: uuid.UUID, db: DbSession, principal: InstanceAdmin) -> AdminUserOut:
    return users.get_user_view(db, user_id)


@router.post(
    "/admin/users",
    response_model=AdminUserOut,
    status_code=status.HTTP_201_CREATED,
    responses=_PROBLEMS,
    summary="Create a local user (instance admin, step-up)",
)
def post_user(
    body: AdminUserCreate, request: Request, db: DbSession, principal: SteppedUpAdmin
) -> AdminUserOut:
    user = users.create_local_user(
        db, get_app_settings(request), body, actor_id=principal.user_id, request=request
    )
    return users.admin_view(db, [user])[0]


@router.patch(
    "/admin/users/{user_id}",
    response_model=AdminUserOut,
    responses=_PROBLEMS,
    summary="Activate/deactivate a user or set the break-glass flag (instance admin, step-up)",
)
def patch_user(
    user_id: uuid.UUID,
    body: AdminUserUpdate,
    request: Request,
    db: DbSession,
    principal: SteppedUpAdmin,
) -> AdminUserOut:
    return users.update_user(db, principal, user_id, body, request=request)


@router.post(
    "/admin/users/{user_id}/mfa-reset",
    response_model=MfaResetOut,
    responses=_PROBLEMS,
    summary="Remove every second factor of a user (instance admin, step-up)",
)
def post_mfa_reset(
    user_id: uuid.UUID, request: Request, db: DbSession, principal: SteppedUpAdmin
) -> MfaResetOut:
    return MfaResetOut(factors_removed=users.reset_mfa(db, principal, user_id, request=request))

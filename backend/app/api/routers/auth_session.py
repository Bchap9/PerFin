# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /auth/session`` (who am I + CSRF token) and session management.

``GET /auth/session`` is what the SPA calls first (``frontend/src/lib/csrf.ts``):

* **Logged in** (session cookie or bearer token): ``200`` with the user, the
  active household and role, the MFA state, the login method and a CSRF token
  bound to the session (``null`` for bearer callers, who need none), and
  ``ai_clients_active`` (the household has a live AI client: the persistent
  banner of AI_ACCESS.md M5).
* **Not logged in**: ``401`` problem (``unauthenticated``) whose body also
  carries ``authenticated: false`` and a fresh *pre-auth* CSRF token; the
  response sets the ``__Host-perfin-pre`` cookie it is paired with, so the
  login form can ``POST``. A session cookie that no longer maps to a live
  session is cleared at the same time.
* **Password accepted, second factor outstanding**: ``401`` problem
  ``mfa-required`` with ``mfa.pending: true``, the enrolled factor types and a
  CSRF token bound to the pending session (for ``POST /auth/mfa/verify``).

Session management (browser sessions only, tokens are refused):
``GET /auth/sessions`` lists the caller's live sessions, ``DELETE
/auth/sessions/{id}`` revokes one (clearing the cookie if it is the current
one) and ``DELETE /auth/sessions`` revokes every other session.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.authz import require_authenticated
from app.auth.deps import DbSession, OptionalPrincipal, get_app_settings, session_acceptor
from app.auth.models import MfaMethod, SessionStage
from app.auth.principal import PRIMARY_MFA_TYPES, Principal
from app.auth.sessions import (
    SessionPolicy,
    clear_session_cookie,
    cookie_name,
    list_sessions,
    load_session,
    raw_session_id,
    revoke_other_sessions,
    revoke_session,
)
from app.core.audit import audit
from app.core.csrf import get_csrf
from app.core.errors import ProblemError, problem_response
from app.db.models import Household
from app.db.rls import get_context, restore_context, set_context
from app.domain.ai.switches import active_client_count

router = APIRouter(tags=["auth"])

PROBLEM_RESPONSE: dict[int | str, dict[str, Any]] = {
    401: {
        "description": "Not logged in (body carries a pre-auth `csrf_token`)",
        "content": {"application/problem+json": {}},
    }
}


class SessionUserOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    username: str
    display_name: str
    is_instance_admin: bool


class SessionHouseholdOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str
    role: Literal["owner", "admin", "member", "viewer"]


class SessionMfaOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    enrolled: bool
    verified: bool
    verified_at: datetime | None
    required: bool
    enrollment_required: bool


class SessionOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    authenticated: Literal[True] = True
    user: SessionUserOut
    household: SessionHouseholdOut | None
    csrf_token: str | None
    mfa: SessionMfaOut
    auth_method: Literal["local", "oidc", "breakglass", "bootstrap", "token"]
    ai_clients_active: bool = Field(
        default=False,
        description="The active household has a live AI client (not revoked, not "
        "expired): the app shows a persistent banner (AI_ACCESS.md M5).",
    )


class SessionInfoOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    current: bool
    login_method: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    remember_device: bool
    user_agent: str | None
    ip: str | None


class RevokedOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    revoked: int


def _session_out(db: Session, request: Request, principal: Principal) -> SessionOut:
    settings = get_app_settings(request)
    household: SessionHouseholdOut | None = None
    ai_active = False
    if principal.household_id is not None and principal.role is not None:
        row = db.get(Household, principal.household_id)
        if row is not None:
            household = SessionHouseholdOut(id=row.id, name=row.name, role=principal.role.value)
            ai_active = active_client_count(db, row.id) > 0
    csrf_token: str | None = None
    if principal.auth_method == "session":
        raw = raw_session_id(request, settings)
        if raw is not None:
            csrf_token = get_csrf(request).token_for_session(raw)
    method: Any = (
        "token"
        if principal.auth_method == "token" or principal.login_method is None
        else principal.login_method.value
    )
    return SessionOut(
        user=SessionUserOut(
            id=principal.user.id,
            username=principal.user.username,
            display_name=principal.user.display_name,
            is_instance_admin=principal.is_instance_admin,
        ),
        household=household,
        csrf_token=csrf_token,
        mfa=SessionMfaOut(
            enrolled=principal.mfa_enrolled,
            verified=principal.mfa_verified_at is not None,
            verified_at=principal.mfa_verified_at,
            required=principal.mfa_required,
            enrollment_required=principal.restricted,
        ),
        auth_method=method,
        ai_clients_active=ai_active,
    )


def _pending_factor_types(db: Session, user_id: uuid.UUID) -> list[str]:
    types = db.execute(
        select(MfaMethod.type)
        .where(MfaMethod.user_id == user_id, MfaMethod.type.in_(PRIMARY_MFA_TYPES))
        .distinct()
    ).scalars()
    return sorted(t.value for t in types)


@router.get(
    "/auth/session",
    response_model=SessionOut,
    responses=PROBLEM_RESPONSE,
    summary="Current session, household, MFA state and CSRF token",
)
def get_auth_session(
    request: Request, db: DbSession, principal: OptionalPrincipal
) -> SessionOut | JSONResponse:
    if principal is not None:
        return _session_out(db, request, principal)

    settings = get_app_settings(request)
    csrf = get_csrf(request)
    raw = raw_session_id(request, settings)
    if raw is not None:
        row = load_session(
            db,
            raw,
            policy=SessionPolicy.from_settings(settings),
            touch=False,
            accept=session_acceptor(request),
        )
        if row is not None and row.stage is SessionStage.MFA_PENDING:
            return problem_response(
                401,
                "mfa-required",
                "Second factor required",
                "Complete the login with your second factor.",
                {
                    "authenticated": False,
                    "csrf_token": csrf.token_for_session(raw),
                    "mfa": {"pending": True, "methods": _pending_factor_types(db, row.user_id)},
                },
            )
    # Issue the pre-auth nonce cookie on a carrier, then copy it onto the problem.
    carrier = Response()
    token = csrf.issue_pre_auth(carrier)
    if request.cookies.get(cookie_name(settings)) is not None:
        clear_session_cookie(carrier, settings)
    response = problem_response(
        401,
        "unauthenticated",
        "Not authenticated",
        "Log in to continue.",
        {"authenticated": False, "csrf_token": token},
    )
    for value in carrier.headers.getlist("set-cookie"):
        response.headers.append("set-cookie", value)
    return response


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


def require_browser_session(
    principal: Annotated[Principal, Depends(require_authenticated)],
) -> Principal:
    if principal.auth_method != "session" or principal.session_id is None:
        raise ProblemError(
            403, "session-required", "Browser session required", "Use the web app for this."
        )
    return principal


BrowserPrincipal = Annotated[Principal, Depends(require_browser_session)]


def _audit_user_event(
    db: Session, request: Request, principal: Principal, action: str, target: uuid.UUID | None
) -> None:
    """An audit row for a user-level event (instance-level when no household)."""
    if principal.household_id is not None:
        audit(
            db,
            action=action,
            target_type="user_session",
            target_id=target,
            household_id=principal.household_id,
            user_id=principal.user_id,
            request=request,
        )
        return
    saved = get_context(db)
    set_context(db, None, principal.user_id, system_actor=True)
    try:
        audit(
            db,
            action=action,
            target_type="user_session",
            target_id=target,
            user_id=principal.user_id,
            request=request,
        )
    finally:
        restore_context(db, saved)


@router.get("/auth/sessions", response_model=list[SessionInfoOut], summary="List my sessions")
def get_auth_sessions(db: DbSession, principal: BrowserPrincipal) -> list[SessionInfoOut]:
    return [
        SessionInfoOut(
            id=row.id,
            current=row.id == principal.session_id,
            login_method=row.login_method.value,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            expires_at=min(row.idle_expires_at, row.absolute_expires_at),
            remember_device=row.remember_device,
            user_agent=row.user_agent,
            ip=None if row.ip is None else str(row.ip),
        )
        for row in list_sessions(db, principal.user_id)
    ]


@router.delete(
    "/auth/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke one of my sessions",
)
def delete_auth_session(
    session_id: uuid.UUID, request: Request, db: DbSession, principal: BrowserPrincipal
) -> Response:
    if not revoke_session(db, session_id, user_id=principal.user_id, reason="user_revoked"):
        raise ProblemError(404, "session-not-found", "Session not found")
    _audit_user_event(db, request, principal, "session.revoke", session_id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    if session_id == principal.session_id:
        clear_session_cookie(response, get_app_settings(request))
    return response


@router.delete(
    "/auth/sessions",
    response_model=RevokedOut,
    summary="Revoke all my other sessions",
)
def delete_other_auth_sessions(
    request: Request, db: DbSession, principal: BrowserPrincipal
) -> RevokedOut:
    count = revoke_other_sessions(
        db, principal.user_id, keep_session_id=principal.session_id, reason="user_revoked_others"
    )
    _audit_user_event(db, request, principal, "session.revoke_others", None)
    return RevokedOut(revoked=count)

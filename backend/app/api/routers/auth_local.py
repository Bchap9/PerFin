# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local login, logout, password change and password reset (``app.auth.local``).

* ``GET /auth/login/options``: whether the local password form and password
  reset are offered to this client (OIDC-only mode hides the form except from
  ``PERFIN_BREAKGLASS_CIDRS``).
* ``POST /auth/login``: step 1. ``200`` with ``status: "authenticated"`` (a
  full session) or ``status: "mfa_required"`` (an ``mfa_pending`` session
  that only ``POST /auth/mfa/verify`` accepts), the session cookie and the
  CSRF token bound to the new session. Every refusal is the same ``401
  invalid-credentials`` (A2); too many attempts ``429 rate-limited``;
  OIDC-only mode outside the break-glass networks ``403
  local-login-disabled``. The login form posts with the pre-auth CSRF token
  from ``GET /auth/session``.
* ``POST /auth/logout``: revokes the presented session (full or pending) and
  clears the cookie; ``204`` even without a session.
* ``POST /auth/password``: change the password (browser session, current
  password, and a fresh step-up when the user has a second factor). The
  session id rotates, every other session is revoked, and the response
  carries the new CSRF token.
* ``POST /auth/password-reset/request``: always ``202`` (A2); the mail is
  prepared after the response is sent. ``404 password-reset-unavailable``
  when SMTP is not configured or in OIDC-only mode (the same for everyone).
* ``POST /auth/password-reset/confirm``: token (from the link's fragment) and
  new password in the body (A11); ``204``, or ``400 invalid-reset-token``.

Listing and revoking sessions lives in ``auth_session``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.auth.authz import require_authenticated, step_up_is_fresh
from app.auth.deps import DbSession, get_app_settings
from app.auth.local import (
    PASSWORD_INPUT_MAX,
    USERNAME_INPUT_MAX,
    LocalLoginDisabled,
    LoginFailed,
    audit_auth,
    audit_auth_committed,
    get_auth_limits,
    get_sessionmaker,
    invalid_credentials,
    local_login_available,
    login,
    request_ip,
    set_password,
    validate_new_password,
    weak_password,
)
from app.auth.models import Credential
from app.auth.passwords import PasswordPolicyError, verify_password
from app.auth.principal import Principal
from app.auth.ratelimit import RateLimiter, account_key, ip_key
from app.auth.reset import (
    InvalidResetToken,
    confirm_reset,
    process_reset_request,
    reset_available,
)
from app.auth.sessions import (
    SessionPolicy,
    clear_session_cookie,
    load_session,
    raw_session_id,
    revoke,
    revoke_other_sessions,
    rotate_session,
    set_session_cookie,
)
from app.core.audit import RequestMeta
from app.core.csrf import get_csrf
from app.core.errors import ProblemError
from app.core.settings import Settings
from app.integrations.notifications.smtp import Mailer, mailer_from_settings

router = APIRouter(tags=["auth"])


def _problems(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {
        code: {"description": "Problem details", "content": {"application/problem+json": {}}}
        for code in statuses
    }


def local_login_disabled() -> ProblemError:
    return ProblemError(
        403,
        "local-login-disabled",
        "Local login is disabled",
        "Sign in with the identity provider.",
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LoginOptionsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    local_login: bool
    password_reset: bool


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, Field(min_length=1, max_length=USERNAME_INPUT_MAX)]
    password: Annotated[str, Field(min_length=1, max_length=PASSWORD_INPUT_MAX)]
    remember_device: bool = False


class LoginMfaOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    required: bool
    methods: list[str]


class LoginOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["authenticated", "mfa_required"]
    csrf_token: str
    mfa: LoginMfaOut


class PasswordChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: Annotated[str, Field(min_length=1, max_length=PASSWORD_INPUT_MAX)]
    new_password: Annotated[str, Field(min_length=1, max_length=PASSWORD_INPUT_MAX)]


class PasswordChangedOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    csrf_token: str
    sessions_revoked: int


class ResetRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: Annotated[str, Field(min_length=1, max_length=USERNAME_INPUT_MAX)]


class AcceptedOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["accepted"] = "accepted"


class ResetConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, Field(min_length=1, max_length=256)]
    new_password: Annotated[str, Field(min_length=1, max_length=PASSWORD_INPUT_MAX)]


# ---------------------------------------------------------------------------
# Login and logout
# ---------------------------------------------------------------------------


@router.get(
    "/auth/login/options",
    response_model=LoginOptionsOut,
    summary="Which local login features this client is offered",
)
def get_login_options(request: Request) -> LoginOptionsOut:
    settings = get_app_settings(request)
    return LoginOptionsOut(
        local_login=local_login_available(settings, request_ip(request)),
        password_reset=reset_available(settings),
    )


@router.post(
    "/auth/login",
    response_model=LoginOut,
    responses=_problems(401, 403, 429),
    summary="Local login, step 1 (username and password)",
)
def post_login(body: LoginIn, request: Request, response: Response) -> LoginOut:
    settings = get_app_settings(request)
    try:
        result = login(
            request,
            settings,
            username=body.username,
            password=body.password,
            remember_device=body.remember_device,
            previous_session_id=raw_session_id(request, settings),
        )
    except LocalLoginDisabled:
        raise local_login_disabled() from None
    except LoginFailed:
        raise invalid_credentials() from None
    set_session_cookie(response, settings, result.issued)
    csrf = get_csrf(request)
    csrf.clear_pre_auth(response)
    return LoginOut(
        status="mfa_required" if result.mfa_pending else "authenticated",
        csrf_token=csrf.token_for_session(result.issued.raw_id),
        mfa=LoginMfaOut(required=result.mfa_pending, methods=list(result.mfa_methods)),
    )


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_problems(400),
    summary="Log out (revoke this session)",
)
def post_logout(request: Request, db: DbSession) -> Response:
    settings = get_app_settings(request)
    if request.headers.get("authorization") is not None:
        raise ProblemError(
            400,
            "session-required",
            "Browser session required",
            "Personal access tokens are revoked under /auth/tokens.",
        )
    row = load_session(
        db,
        raw_session_id(request, settings),
        policy=SessionPolicy.from_settings(settings),
        touch=False,
        for_update=True,
    )
    if row is not None:
        revoke(row, reason="logout")
        db.flush()
        audit_auth(
            db,
            action="user.logout",
            user_id=row.user_id,
            meta=RequestMeta.from_request(request),
            target_type="user_session",
            target_id=row.id,
        )
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, settings)
    return response


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------


def require_password_change_caller(
    principal: Annotated[Principal, Depends(require_authenticated)],
) -> Principal:
    """A browser session; with a second factor enrolled, a fresh step-up (A6)."""
    if principal.auth_method != "session" or principal.session is None:
        raise ProblemError(
            403, "session-required", "Browser session required", "Use the web app for this."
        )
    if principal.mfa_enrolled and not step_up_is_fresh(principal.mfa_verified_at):
        raise ProblemError(
            403,
            "step-up-required",
            "Recent verification required",
            "Verify your second factor again to continue.",
        )
    return principal


PasswordChanger = Annotated[Principal, Depends(require_password_change_caller)]


@router.post(
    "/auth/password",
    response_model=PasswordChangedOut,
    responses=_problems(403, 409, 422, 429),
    summary="Change my password (revokes my other sessions)",
)
def post_password(
    body: PasswordChangeIn,
    request: Request,
    response: Response,
    db: DbSession,
    principal: PasswordChanger,
) -> PasswordChangedOut:
    settings = get_app_settings(request)
    user = principal.user
    if not settings.local_login_enabled and not user.is_breakglass:
        raise local_login_disabled()
    credential = db.execute(
        select(Credential).where(Credential.user_id == user.id)
    ).scalar_one_or_none()
    if credential is None:
        raise ProblemError(
            409,
            "no-local-password",
            "No local password",
            "This account signs in through the identity provider.",
        )
    factory = get_sessionmaker(request)
    meta = RequestMeta.from_request(request)
    RateLimiter(factory).hit(
        get_auth_limits(request).password_change_per_account, account_key(user.id)
    ).raise_if_denied()
    if not verify_password(credential.password_hash, body.current_password):
        audit_auth_committed(
            factory,
            action="user.password_change_failed",
            user_id=user.id,
            meta=meta,
            after={"reason": "bad_password"},
        )
        raise ProblemError(
            403, "invalid-credentials", "Password not changed", "The current password is wrong."
        )
    if body.new_password == body.current_password:
        raise weak_password(
            PasswordPolicyError("same-as-current", "Choose a password you have not used here.")
        )
    try:
        validate_new_password(body.new_password, settings, username=user.username)
    except PasswordPolicyError as exc:
        raise weak_password(exc) from None
    set_password(db, user.id, body.new_password)
    if principal.session is None:  # pragma: no cover - guarded by the dependency
        raise ProblemError(403, "session-required", "Browser session required")
    issued = rotate_session(
        db,
        principal.session,
        policy=SessionPolicy.from_settings(settings),
        reason="password_change",
    )
    revoked = revoke_other_sessions(
        db, user.id, keep_session_id=issued.row.id, reason="password_change"
    )
    audit_auth(
        db,
        action="user.password_change",
        user_id=user.id,
        meta=meta,
        after={"sessions_revoked": revoked},
    )
    set_session_cookie(response, settings, issued)
    return PasswordChangedOut(
        csrf_token=get_csrf(request).token_for_session(issued.raw_id),
        sessions_revoked=revoked,
    )


# ---------------------------------------------------------------------------
# Password reset (A11)
# ---------------------------------------------------------------------------


def get_mailer(request: Request) -> Mailer | None:
    """``app.state.mailer`` when set (tests), else SMTP from the settings."""
    mailer = getattr(request.app.state, "mailer", None)
    if mailer is not None:
        return mailer  # type: ignore[no-any-return]
    return mailer_from_settings(get_app_settings(request))


def _require_reset(settings: Settings) -> None:
    if not reset_available(settings):
        raise ProblemError(
            404,
            "password-reset-unavailable",
            "Password reset is not available",
            "Ask the instance administrator to reset your password.",
        )


@router.post(
    "/auth/password-reset/request",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedOut,
    responses=_problems(404, 429),
    summary="Ask for a password reset email (same answer for every account)",
)
def post_reset_request(
    body: ResetRequestIn, request: Request, background: BackgroundTasks
) -> AcceptedOut:
    settings = get_app_settings(request)
    _require_reset(settings)
    limits = get_auth_limits(request)
    limiter = RateLimiter(get_sessionmaker(request))
    limiter.hit(limits.reset_request_per_ip, ip_key(request)).raise_if_denied()
    limiter.hit(
        limits.reset_request_per_account, account_key(None, body.identifier)
    ).raise_if_denied()
    # Lookup, token and mail happen after the response is sent (A2).
    background.add_task(
        process_reset_request,
        get_sessionmaker(request),
        settings,
        get_mailer(request),
        identifier=body.identifier,
        ip=request_ip(request),
        meta=RequestMeta.from_request(request),
    )
    return AcceptedOut()


@router.post(
    "/auth/password-reset/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_problems(400, 404, 422, 429),
    summary="Set a new password with a reset token",
)
def post_reset_confirm(body: ResetConfirmIn, request: Request, db: DbSession) -> Response:
    settings = get_app_settings(request)
    _require_reset(settings)
    RateLimiter(get_sessionmaker(request)).hit(
        get_auth_limits(request).reset_confirm_per_ip, ip_key(request)
    ).raise_if_denied()
    try:
        confirm_reset(
            db,
            settings,
            token=body.token,
            new_password=body.new_password,
            meta=RequestMeta.from_request(request),
        )
    except InvalidResetToken:
        raise ProblemError(
            400,
            "invalid-reset-token",
            "Reset link not valid",
            "The reset link is invalid, expired or already used. Ask for a new one.",
        ) from None
    except PasswordPolicyError as exc:
        raise weak_password(exc) from None
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, settings)
    return response

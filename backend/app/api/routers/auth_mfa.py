# SPDX-License-Identifier: AGPL-3.0-or-later
"""Second factors: the login's second step, step-up and factor management.

Login and step-up (any factor; ``app.auth.mfa.stepup``):

* ``POST /auth/mfa/verify``: second step of a login. Needs the ``mfa_pending``
  session of a password (or OIDC) login; on success the session id rotates
  into a full session with ``mfa_verified_at`` (A3) and the response carries
  the new session's CSRF token.
* ``POST /auth/step-up``: re-verify on a full browser session; rotates the
  id and stamps ``mfa_verified_at`` for ``require_step_up`` (10 minutes, A6;
  remembered devices included).
* ``POST /auth/mfa/webauthn/authentication-options``: a WebAuthn challenge
  for either of the two (pending or full session).

Proof bodies: ``{"method": "totp", "code": "123456"}``, ``{"method":
"recovery", "code": "XXXX-XXXX-XXXX-XXXX"}`` or ``{"method": "webauthn",
"credential": <PublicKeyCredential JSON>}``. A rejected proof is ``400
mfa-invalid``; too many attempts or a locked account ``429``.

Management (browser sessions only; a ``PERFIN_REQUIRE_MFA``-restricted
session may use these to enrol its first factor):

* ``GET /auth/mfa``: the user's factors and unused recovery codes.
* ``POST /auth/mfa/totp`` starts a TOTP enrolment (secret and ``otpauth://``
  URI shown once), ``POST /auth/mfa/totp/confirm`` finishes it with a first
  code, ``DELETE /auth/mfa/totp`` removes it (step-up).
* ``GET /auth/mfa/webauthn`` lists security keys/passkeys, ``POST
  /auth/mfa/webauthn/registration-options`` + ``POST /auth/mfa/webauthn``
  register one, ``DELETE /auth/mfa/webauthn/{id}`` removes it (step-up).
* ``POST /auth/mfa/recovery-codes``: ten new codes, all earlier ones void
  (step-up).

Adding a factor needs step-up once the user has one. Confirming a new factor
proves possession of it, so the session rotates and counts as freshly
verified; the first factor also returns the initial ten recovery codes.
Removing the last factor is refused while MFA is required instance-wide.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.authz import require_authenticated, require_step_up
from app.auth.deps import (
    DbSession,
    get_app_settings,
    get_keyring,
    get_pending_mfa_session,
    require_principal,
    session_acceptor,
    unauthenticated,
)
from app.auth.mfa import recovery, totp
from app.auth.mfa import webauthn as webauthn_mfa
from app.auth.mfa.stepup import (
    MfaRuntime,
    Proof,
    RecoveryProof,
    TotpProof,
    WebAuthnProof,
    audit_user_event,
    require_fresh_mfa_if_enrolled,
    verify_second_factor,
)
from app.auth.mfa.webauthn import NoCredentialsError, RelyingParty, WebAuthnError
from app.auth.models import MfaMethod, MfaType, User, UserSession
from app.auth.principal import PRIMARY_MFA_TYPES, Principal
from app.auth.ratelimit import (
    MFA_PER_ACCOUNT,
    MFA_PER_SESSION,
    account_key,
    get_rate_limiter,
    register_login_success,
)
from app.auth.sessions import (
    IssuedSession,
    SessionPolicy,
    complete_mfa,
    load_session,
    raw_session_id,
    record_step_up,
    set_session_cookie,
)
from app.core import clock
from app.core.csrf import get_csrf
from app.core.errors import ProblemError
from app.core.logging import get_logger

router = APIRouter(tags=["auth"])
log = get_logger(__name__)

NAME_MAX = 100


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TotpProofIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["totp"]
    code: str = Field(min_length=6, max_length=16)


class RecoveryProofIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["recovery"]
    code: str = Field(min_length=16, max_length=64)


class WebAuthnProofIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["webauthn"]
    credential: dict[str, Any]


ProofIn = Annotated[TotpProofIn | RecoveryProofIn | WebAuthnProofIn, Field(discriminator="method")]


def _clean_name(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    if not cleaned:
        raise ValueError("name must not be blank")
    return cleaned


class TotpConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=6, max_length=16)
    name: str = Field(default="Authenticator app", min_length=1, max_length=NAME_MAX)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean_name(value)


class WebAuthnRegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential: dict[str, Any]
    name: str = Field(default="Security key", min_length=1, max_length=NAME_MAX)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean_name(value)


class MfaVerifiedOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    method: Literal["totp", "webauthn", "recovery"]
    mfa_verified_at: datetime
    csrf_token: str
    recovery_codes_remaining: int | None = None


class MfaMethodOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    type: Literal["totp", "webauthn"]
    name: str
    created_at: datetime
    last_used_at: datetime | None


class MfaStatusOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    methods: list[MfaMethodOut]
    recovery_codes_remaining: int
    required: bool


class TotpEnrollmentOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    secret: str
    otpauth_uri: str
    digits: int = totp.DIGITS
    period: int = totp.PERIOD
    algorithm: Literal["SHA1"] = "SHA1"
    expires_at: datetime


class FactorAddedOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    method: MfaMethodOut
    mfa_verified_at: datetime
    csrf_token: str
    recovery_codes: list[str] | None = None


class RecoveryCodesOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    codes: list[str]


# ---------------------------------------------------------------------------
# Dependencies and helpers
# ---------------------------------------------------------------------------


def get_mfa_runtime(request: Request) -> MfaRuntime:
    settings = get_app_settings(request)
    return MfaRuntime(
        settings=settings,
        keyring=get_keyring(request),
        session_factory=request.app.state.sessionmaker,
        limiter=get_rate_limiter(request),
        rp=RelyingParty.from_settings(settings),
    )


Runtime = Annotated[MfaRuntime, Depends(get_mfa_runtime)]
PendingSession = Annotated[UserSession, Depends(get_pending_mfa_session)]


def _browser(principal: Principal) -> Principal:
    if principal.auth_method != "session" or principal.session is None:
        raise ProblemError(
            403, "session-required", "Browser session required", "Use the web app for this."
        )
    return principal


def _session_of(principal: Principal) -> UserSession:
    """The browser session row of a principal that passed ``_browser``."""
    if principal.session is None:
        raise unauthenticated()
    return principal.session


def require_enrolling_session(
    principal: Annotated[Principal, Depends(require_principal)],
) -> Principal:
    """A browser session, MFA-restricted or not (enrolment works for both)."""
    return _browser(principal)


def require_full_browser_session(
    principal: Annotated[Principal, Depends(require_authenticated)],
) -> Principal:
    return _browser(principal)


def require_step_up_session(
    principal: Annotated[Principal, Depends(require_step_up)],
) -> Principal:
    return _browser(principal)


Enrolling = Annotated[Principal, Depends(require_enrolling_session)]
FullSession = Annotated[Principal, Depends(require_full_browser_session)]
SteppedUp = Annotated[Principal, Depends(require_step_up_session)]


def get_ceremony_session(request: Request, db: DbSession) -> UserSession:
    """The caller's live browser session, pending or full, row-locked.

    For ``authentication-options``: the second step of a login and step-up
    both need a challenge. Tokens never take part in MFA ceremonies.
    """
    if request.headers.get("authorization") is not None:
        raise unauthenticated()
    settings = get_app_settings(request)
    row = load_session(
        db,
        raw_session_id(request, settings),
        policy=SessionPolicy.from_settings(settings),
        for_update=True,
        accept=session_acceptor(request),
    )
    if row is None:
        raise unauthenticated()
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise unauthenticated()
    return row


CeremonySession = Annotated[UserSession, Depends(get_ceremony_session)]


def _to_proof(body: TotpProofIn | RecoveryProofIn | WebAuthnProofIn) -> Proof:
    if isinstance(body, TotpProofIn):
        return TotpProof(body.code)
    if isinstance(body, RecoveryProofIn):
        return RecoveryProof(body.code)
    return WebAuthnProof(body.credential)


def _active_user(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthenticated()
    return user


def _issue(request: Request, response: Response, issued: IssuedSession) -> str:
    """Set the rotated session's cookie; return its CSRF token."""
    set_session_cookie(response, get_app_settings(request), issued)
    return get_csrf(request).token_for_session(issued.raw_id)


def _lock_live(db: Session, row: UserSession) -> UserSession:
    """Re-read ``row`` under a lock; refuse if it was rotated or revoked meanwhile."""
    db.refresh(row, with_for_update=True)
    if row.revoked_at is not None:
        raise unauthenticated()
    return row


def _method_out(method: MfaMethod) -> MfaMethodOut:
    kind: Literal["totp", "webauthn"] = "totp" if method.type is MfaType.TOTP else "webauthn"
    return MfaMethodOut(
        id=method.id,
        type=kind,
        name=method.name,
        created_at=method.created_at,
        last_used_at=method.last_used_at,
    )


def _primary_methods(db: Session, user_id: uuid.UUID) -> list[MfaMethod]:
    return list(
        db.execute(
            select(MfaMethod)
            .where(MfaMethod.user_id == user_id, MfaMethod.type.in_(PRIMARY_MFA_TYPES))
            .order_by(MfaMethod.created_at, MfaMethod.id)
        ).scalars()
    )


def _factor_added(
    db: Session,
    request: Request,
    response: Response,
    principal: Principal,
    method: MfaMethod,
) -> FactorAddedOut:
    """Audit, first recovery codes, rotate (possession just proven)."""
    audit_user_event(
        db,
        user_id=principal.user_id,
        action="auth.mfa.enrolled",
        target_type="mfa_method",
        target_id=method.id,
        after={"type": method.type.value, "name": method.name},
        request=request,
    )
    codes: list[str] | None = None
    if recovery.remaining(db, principal.user_id) == 0:
        codes = recovery.regenerate(db, principal.user_id)
        audit_user_event(
            db,
            user_id=principal.user_id,
            action="auth.mfa.recovery_generated",
            after={"count": len(codes)},
            request=request,
        )
    settings = get_app_settings(request)
    current = _lock_live(db, _session_of(principal))
    issued = record_step_up(db, current, policy=SessionPolicy.from_settings(settings))
    csrf_token = _issue(request, response, issued)
    verified_at = issued.row.mfa_verified_at or clock.utcnow()
    return FactorAddedOut(
        method=_method_out(method),
        mfa_verified_at=verified_at,
        csrf_token=csrf_token,
        recovery_codes=codes,
    )


def _ensure_can_remove(db: Session, request: Request, user_id: uuid.UUID, count: int) -> None:
    """Refuse removing the last factor while MFA is required instance-wide."""
    remaining = len(_primary_methods(db, user_id)) - count
    if remaining <= 0 and get_app_settings(request).require_mfa:
        raise ProblemError(
            409,
            "last-mfa-factor",
            "Cannot remove the last second factor",
            "This instance requires a second factor. Add another one first.",
        )


def _after_removal(db: Session, request: Request, user_id: uuid.UUID, method_id: uuid.UUID) -> None:
    audit_user_event(
        db,
        user_id=user_id,
        action="auth.mfa.removed",
        target_type="mfa_method",
        target_id=method_id,
        request=request,
    )
    if not _primary_methods(db, user_id):
        # Recovery codes only stand in for a factor; none left, none needed.
        recovery.delete_all(db, user_id)


# ---------------------------------------------------------------------------
# Login second step and step-up
# ---------------------------------------------------------------------------


@router.post(
    "/auth/mfa/verify",
    response_model=MfaVerifiedOut,
    summary="Second step of a login: verify a factor (rotates the session)",
)
def post_mfa_verify(
    body: Annotated[ProofIn, Body()],
    request: Request,
    response: Response,
    db: DbSession,
    pending: PendingSession,
    rt: Runtime,
) -> MfaVerifiedOut:
    user = _active_user(db, pending.user_id)
    factor = verify_second_factor(
        db,
        rt,
        user=user,
        session_row=pending,
        proof=_to_proof(body),
        purpose="login",
        request=request,
    )
    register_login_success(db, user.id)
    issued = complete_mfa(db, pending, policy=SessionPolicy.from_settings(rt.settings))
    csrf_token = _issue(request, response, issued)
    verified_at = issued.row.mfa_verified_at or clock.utcnow()
    return MfaVerifiedOut(
        method=factor.type.value,
        mfa_verified_at=verified_at,
        csrf_token=csrf_token,
        recovery_codes_remaining=factor.recovery_codes_remaining,
    )


@router.post(
    "/auth/step-up",
    response_model=MfaVerifiedOut,
    summary="Re-verify a factor for sensitive actions (rotates the session)",
)
def post_step_up(
    body: Annotated[ProofIn, Body()],
    request: Request,
    response: Response,
    db: DbSession,
    principal: FullSession,
    rt: Runtime,
) -> MfaVerifiedOut:
    current = _lock_live(db, _session_of(principal))
    factor = verify_second_factor(
        db,
        rt,
        user=principal.user,
        session_row=current,
        proof=_to_proof(body),
        purpose="step_up",
        request=request,
    )
    issued = record_step_up(db, current, policy=SessionPolicy.from_settings(rt.settings))
    csrf_token = _issue(request, response, issued)
    verified_at = issued.row.mfa_verified_at or clock.utcnow()
    return MfaVerifiedOut(
        method=factor.type.value,
        mfa_verified_at=verified_at,
        csrf_token=csrf_token,
        recovery_codes_remaining=factor.recovery_codes_remaining,
    )


@router.post(
    "/auth/mfa/webauthn/authentication-options",
    response_model=dict[str, Any],
    summary="WebAuthn challenge for the login's second step or step-up",
)
def post_webauthn_authentication_options(
    db: DbSession, session_row: CeremonySession, rt: Runtime
) -> dict[str, Any]:
    try:
        return webauthn_mfa.authentication_options(
            db, rt.rp, session_id=session_row.id, user_id=session_row.user_id
        )
    except NoCredentialsError:
        raise ProblemError(
            409,
            "no-webauthn-credentials",
            "No security key registered",
            "Use another second factor.",
        ) from None


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


@router.get("/auth/mfa", response_model=MfaStatusOut, summary="My second factors")
def get_mfa_status(request: Request, db: DbSession, principal: Enrolling) -> MfaStatusOut:
    return MfaStatusOut(
        methods=[_method_out(m) for m in _primary_methods(db, principal.user_id)],
        recovery_codes_remaining=recovery.remaining(db, principal.user_id),
        required=get_app_settings(request).require_mfa,
    )


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------


@router.post(
    "/auth/mfa/totp",
    response_model=TotpEnrollmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start a TOTP enrolment (secret shown once)",
)
def post_totp_enrollment(db: DbSession, principal: Enrolling, rt: Runtime) -> TotpEnrollmentOut:
    require_fresh_mfa_if_enrolled(principal)
    if totp.user_totp_methods(db, principal.user_id):
        raise ProblemError(
            409,
            "totp-already-enrolled",
            "Authenticator app already set up",
            "Remove the existing authenticator app first.",
        )
    session_id = _session_of(principal).id
    enrollment = totp.start_enrollment(
        db,
        rt.keyring,
        session_id=session_id,
        user_id=principal.user_id,
        account_name=principal.user.username,
    )
    return TotpEnrollmentOut(
        secret=enrollment.secret,
        otpauth_uri=enrollment.otpauth_uri,
        expires_at=enrollment.expires_at,
    )


@router.post(
    "/auth/mfa/totp/confirm",
    response_model=FactorAddedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Finish a TOTP enrolment with the first code",
)
def post_totp_confirm(
    body: TotpConfirmIn,
    request: Request,
    response: Response,
    db: DbSession,
    principal: Enrolling,
    rt: Runtime,
) -> FactorAddedOut:
    require_fresh_mfa_if_enrolled(principal)
    session_id = _session_of(principal).id
    # Enrolment codes are guessable too: same windows as verification.
    rt.limiter.hit(MFA_PER_SESSION, str(session_id)).raise_if_denied()
    rt.limiter.hit(MFA_PER_ACCOUNT, account_key(principal.user_id)).raise_if_denied()
    if totp.user_totp_methods(db, principal.user_id, lock=True):
        raise ProblemError(
            409,
            "totp-already-enrolled",
            "Authenticator app already set up",
            "Remove the existing authenticator app first.",
        )
    try:
        method = totp.confirm_enrollment(
            db,
            rt.keyring,
            session_id=session_id,
            user_id=principal.user_id,
            code=body.code,
            name=body.name,
        )
    except totp.EnrollmentNotFoundError:
        raise ProblemError(
            409,
            "no-totp-enrollment",
            "No enrolment in progress",
            "Start the authenticator app setup again.",
        ) from None
    if method is None:
        raise ProblemError(
            400,
            "mfa-invalid",
            "Verification failed",
            "The code was not accepted. Check the time on your device and try again.",
        )
    return _factor_added(db, request, response, principal, method)


@router.delete(
    "/auth/mfa/totp",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove the authenticator app (step-up)",
)
def delete_totp(request: Request, db: DbSession, principal: SteppedUp) -> Response:
    methods = totp.user_totp_methods(db, principal.user_id, lock=True)
    if not methods:
        raise ProblemError(404, "mfa-method-not-found", "Authenticator app not set up")
    _ensure_can_remove(db, request, principal.user_id, len(methods))
    for method in methods:
        totp.remove(db, principal.user_id, method.id)
        _after_removal(db, request, principal.user_id, method.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# WebAuthn
# ---------------------------------------------------------------------------


@router.get(
    "/auth/mfa/webauthn",
    response_model=list[MfaMethodOut],
    summary="My security keys and passkeys",
)
def get_webauthn_credentials(db: DbSession, principal: Enrolling) -> list[MfaMethodOut]:
    return [_method_out(m) for m in webauthn_mfa.user_credentials(db, principal.user_id)]


@router.post(
    "/auth/mfa/webauthn/registration-options",
    response_model=dict[str, Any],
    summary="Start registering a security key or passkey",
)
def post_webauthn_registration_options(
    db: DbSession, principal: Enrolling, rt: Runtime
) -> dict[str, Any]:
    require_fresh_mfa_if_enrolled(principal)
    session_id = _session_of(principal).id
    try:
        return webauthn_mfa.registration_options(
            db, rt.rp, session_id=session_id, user=principal.user
        )
    except WebAuthnError:
        raise ProblemError(
            409,
            "too-many-webauthn-credentials",
            "Too many security keys",
            f"Remove one first (at most {webauthn_mfa.MAX_CREDENTIALS_PER_USER}).",
        ) from None


@router.post(
    "/auth/mfa/webauthn",
    response_model=FactorAddedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Finish registering a security key or passkey",
)
def post_webauthn_register(
    body: WebAuthnRegisterIn,
    request: Request,
    response: Response,
    db: DbSession,
    principal: Enrolling,
    rt: Runtime,
) -> FactorAddedOut:
    require_fresh_mfa_if_enrolled(principal)
    session_id = _session_of(principal).id
    try:
        method = webauthn_mfa.verify_registration(
            db,
            rt.session_factory,
            rt.rp,
            session_id=session_id,
            user_id=principal.user_id,
            credential=body.credential,
            name=body.name,
        )
    except WebAuthnError as exc:
        log.info("webauthn registration refused", extra={"reason": exc.reason})
        raise ProblemError(
            400,
            "webauthn-registration-failed",
            "Registration failed",
            "The security key response was not accepted. Start again.",
        ) from None
    return _factor_added(db, request, response, principal, method)


@router.delete(
    "/auth/mfa/webauthn/{method_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a security key or passkey (step-up)",
)
def delete_webauthn_credential(
    method_id: uuid.UUID, request: Request, db: DbSession, principal: SteppedUp
) -> Response:
    owned = [m for m in webauthn_mfa.user_credentials(db, principal.user_id) if m.id == method_id]
    if not owned:
        raise ProblemError(404, "mfa-method-not-found", "Security key not found")
    _ensure_can_remove(db, request, principal.user_id, 1)
    webauthn_mfa.remove(db, principal.user_id, method_id)
    _after_removal(db, request, principal.user_id, method_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------


@router.post(
    "/auth/mfa/recovery-codes",
    response_model=RecoveryCodesOut,
    summary="Ten new recovery codes; all earlier ones stop working (step-up)",
)
def post_recovery_codes(request: Request, db: DbSession, principal: SteppedUp) -> RecoveryCodesOut:
    if not _primary_methods(db, principal.user_id):
        raise ProblemError(
            409,
            "no-mfa-factor",
            "No second factor",
            "Set up an authenticator app or security key first.",
        )
    codes = recovery.regenerate(db, principal.user_id)
    audit_user_event(
        db,
        user_id=principal.user_id,
        action="auth.mfa.recovery_generated",
        after={"count": len(codes)},
        request=request,
    )
    return RecoveryCodesOut(codes=codes)

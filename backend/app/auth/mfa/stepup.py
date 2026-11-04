# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verifying a second factor: the login's second step and step-up (A1/A3/A6/A7/A8).

:func:`verify_second_factor` checks one proof (TOTP code, WebAuthn assertion
or recovery code) for the user of a session. Both ``POST /auth/mfa/verify``
(a ``mfa_pending`` session) and ``POST /auth/step-up`` (a full session) use
it; the caller then rotates the session with
``app.auth.sessions.complete_mfa`` or ``record_step_up``, which stamp
``mfa_verified_at`` (A3: the id changes on MFA completion).

Before any proof is looked at:

* an account locked by ``app.auth.ratelimit`` (failed passwords or factors)
  is answered ``429`` with ``Retry-After``;
* each attempt counts against the sliding windows ``mfa:session`` (5 per 5
  minutes per session) and ``mfa:account`` (10 per 15 minutes per user), and
  recovery codes additionally against :data:`RECOVERY_PER_ACCOUNT` (5 per
  hour per user). Counting happens in its own transaction, so it survives the
  rolled-back failed request.

A failed proof feeds the progressive lockout of the account
(``register_login_failure``: growing delay, then a 15-minute lock) and is
audited, both in their own transactions, before ``400 mfa-invalid`` is
returned. A successful proof is audited in the request transaction; a used
recovery code also emits a ``security.recovery_code_used`` event into each
of the user's households (the notification hook; out-of-band delivery
through notification channels arrives in Phase 7).

:func:`require_fresh_mfa_if_enrolled` is the step-up rule for *adding* a
factor: free while the user has none (first enrolment, including the
restricted session of ``PERFIN_REQUIRE_MFA``), otherwise the session must
have verified MFA within ``app.auth.authz.STEP_UP_WINDOW``.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import HTTPConnection

from app.auth.authz import step_up_is_fresh
from app.auth.mfa import recovery, totp
from app.auth.mfa import webauthn as webauthn_mfa
from app.auth.mfa.webauthn import RelyingParty, WebAuthnError
from app.auth.models import MfaType, User, UserSession
from app.auth.principal import Principal, memberships
from app.auth.ratelimit import (
    MFA_PER_ACCOUNT,
    MFA_PER_SESSION,
    Limit,
    RateLimiter,
    account_key,
    is_locked,
    register_login_failure,
    too_many_requests,
)
from app.core import clock
from app.core.audit import audit
from app.core.crypto import Keyring
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.logging import get_logger
from app.core.settings import Settings
from app.db.rls import get_context, restore_context, set_context
from app.domain.notifications.routing import notify

log = get_logger(__name__)

RECOVERY_PER_ACCOUNT: Final = Limit("mfa:recovery", 5, timedelta(hours=1))
RECOVERY_USED_EVENT: Final = "security.recovery_code_used"

Purpose = Literal["login", "step_up"]


# ---------------------------------------------------------------------------
# Proofs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TotpProof:
    code: str = field(repr=False)


@dataclass(frozen=True)
class RecoveryProof:
    code: str = field(repr=False)


@dataclass(frozen=True)
class WebAuthnProof:
    credential: Mapping[str, Any] = field(repr=False)


Proof = TotpProof | RecoveryProof | WebAuthnProof


def proof_type(proof: Proof) -> MfaType:
    if isinstance(proof, TotpProof):
        return MfaType.TOTP
    if isinstance(proof, RecoveryProof):
        return MfaType.RECOVERY
    return MfaType.WEBAUTHN


@dataclass(frozen=True)
class VerifiedFactor:
    type: MfaType
    method_id: uuid.UUID
    recovery_codes_remaining: int | None = None


@dataclass(frozen=True)
class MfaRuntime:
    """What verification needs from the app (built per request by the router)."""

    settings: Settings
    keyring: Keyring
    session_factory: sessionmaker[Session]
    limiter: RateLimiter
    rp: RelyingParty


def mfa_invalid() -> ProblemError:
    return ProblemError(
        400,
        "mfa-invalid",
        "Verification failed",
        "The code or security key response was not accepted.",
    )


# ---------------------------------------------------------------------------
# Audit and notification helpers
# ---------------------------------------------------------------------------


def audit_user_event(
    db: Session,
    *,
    user_id: uuid.UUID,
    action: str,
    target_type: str = "user",
    target_id: uuid.UUID | None = None,
    after: Mapping[str, Any] | None = None,
    request: HTTPConnection | None = None,
) -> None:
    """An instance-level audit row about the user's own authenticators.

    MFA events belong to the person, not to a household, so they are written
    with ``household_id = NULL`` in a system-actor context (the caller's RLS
    context is restored afterwards).
    """
    saved = get_context(db)
    set_context(db, None, user_id, system_actor=True)
    try:
        audit(
            db,
            action=action,
            target_type=target_type,
            target_id=target_id if target_id is not None else user_id,
            user_id=user_id,
            after=dict(after) if after is not None else None,
            request=request,
        )
    finally:
        restore_context(db, saved)


def notify_households(
    db: Session, user_id: uuid.UUID, event_type: str, payload: Mapping[str, Any]
) -> int:
    """Emit ``event_type`` into every household the user belongs to."""
    saved = get_context(db)
    try:
        households = [m.household_id for m in memberships(db, user_id)]
        for household_id in households:
            set_context(db, household_id, user_id)
            event_id = emit(db, household_id, event_type, dict(payload))
            notify(db, household_id, event_type, source_id=event_id)
    finally:
        restore_context(db, saved)
    return len(households)


def _record_failure(
    rt: MfaRuntime,
    *,
    user_id: uuid.UUID,
    proof: Proof,
    purpose: Purpose,
    request: HTTPConnection | None,
    now: datetime,
) -> None:
    """Lockout counter and audit row, each committed on its own."""
    outcome = register_login_failure(rt.session_factory, user_id, now=now)
    with rt.session_factory() as db, db.begin():
        audit_user_event(
            db,
            user_id=user_id,
            action="auth.mfa.failed",
            after={
                "method": proof_type(proof).value,
                "purpose": purpose,
                "failures": outcome.failed_login_count,
                "locked": outcome.newly_locked,
            },
            request=request,
        )


def _locked_response(user: User, now: datetime) -> ProblemError:
    until = user.locked_until or now
    return too_many_requests(max(math.ceil((until - now).total_seconds()), 1))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_second_factor(
    db: Session,
    rt: MfaRuntime,
    *,
    user: User,
    session_row: UserSession,
    proof: Proof,
    purpose: Purpose,
    request: HTTPConnection | None = None,
    now: datetime | None = None,
) -> VerifiedFactor:
    """Check ``proof`` for ``user`` (see module doc); raise ``ProblemError`` on failure."""
    now = now or clock.utcnow()
    if is_locked(user, now):
        raise _locked_response(user, now)
    rt.limiter.hit(MFA_PER_SESSION, str(session_row.id), now=now).raise_if_denied()
    rt.limiter.hit(MFA_PER_ACCOUNT, account_key(user.id), now=now).raise_if_denied()
    if isinstance(proof, RecoveryProof):
        rt.limiter.hit(RECOVERY_PER_ACCOUNT, account_key(user.id), now=now).raise_if_denied()

    method_id: uuid.UUID | None = None
    if isinstance(proof, TotpProof):
        found = totp.verify(db, rt.keyring, user_id=user.id, code=proof.code, now=now)
        method_id = found.id if found else None
    elif isinstance(proof, RecoveryProof):
        spent = recovery.consume(db, user.id, proof.code, now=now)
        method_id = spent.id if spent else None
    else:
        try:
            used = webauthn_mfa.verify_assertion(
                db,
                rt.session_factory,
                rt.rp,
                session_id=session_row.id,
                user_id=user.id,
                credential=proof.credential,
                now=now,
            )
            method_id = used.id
        except WebAuthnError as exc:
            log.info("webauthn assertion refused", extra={"reason": exc.reason})

    if method_id is None:
        _record_failure(rt, user_id=user.id, proof=proof, purpose=purpose, request=request, now=now)
        raise mfa_invalid()

    kind = proof_type(proof)
    left: int | None = None
    if kind is MfaType.RECOVERY:
        left = recovery.remaining(db, user.id)
        audit_user_event(
            db,
            user_id=user.id,
            action="auth.mfa.recovery_used",
            target_type="mfa_method",
            target_id=method_id,
            after={"remaining": left, "purpose": purpose},
            request=request,
        )
        notify_households(
            db, user.id, RECOVERY_USED_EVENT, {"user_id": str(user.id), "remaining": left}
        )
    audit_user_event(
        db,
        user_id=user.id,
        action="auth.mfa.verified" if purpose == "login" else "auth.step_up",
        target_type="mfa_method",
        target_id=method_id,
        after={"method": kind.value},
        request=request,
    )
    reset_failures(db, user.id)
    return VerifiedFactor(kind, method_id, left)


def reset_failures(db: Session, user_id: uuid.UUID) -> None:
    """A verified factor clears the failure counter and any delay or lock."""
    db.execute(
        update(User)
        .where(User.id == user_id)
        .values(failed_login_count=0, locked_until=None)
        .execution_options(synchronize_session="fetch")
    )


def require_fresh_mfa_if_enrolled(principal: Principal, now: datetime | None = None) -> None:
    """Adding a factor needs step-up once the user has one (see module doc)."""
    if principal.mfa_enrolled and not step_up_is_fresh(principal.mfa_verified_at, now):
        raise ProblemError(
            403,
            "step-up-required",
            "Recent verification required",
            "Verify your second factor again to continue.",
        )

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server-side browser sessions (ADR-0005, AUTH.md "Sessions", SECURITY.md A3/A4/A6).

* The cookie carries only a random 256-bit id (43 base64url characters); the
  database stores its SHA-256 (``user_session.token_hash``). A database leak
  does not yield usable cookies.
* Cookie: ``__Host-perfin-session``; ``HttpOnly; Secure; SameSite=Lax;
  Path=/``, no ``Domain`` (the ``__Host-`` prefix makes browsers refuse it
  otherwise, so a sibling subdomain cannot plant or overwrite it). Only in
  dev mode with ``PERFIN_SECURE_COOKIES=false`` (plain http) the cookie is
  ``perfin-session-dev`` without ``Secure`` (the prefix requires ``Secure``).
  A normal session cookie has no ``Max-Age`` (dies with the browser); a
  "remember this device" cookie lives until the absolute expiry.
* Lifetimes: idle timeout ``PERFIN_SESSION_IDLE_MINUTES`` (30), absolute
  ``PERFIN_SESSION_ABSOLUTE_HOURS`` (12); "remember this device"
  ``PERFIN_SESSION_REMEMBER_DAYS`` (30) for both. Step-up actions still need
  a fresh MFA verification (``app.auth.authz.require_step_up``, A6). A
  half-logged-in session (``stage = mfa_pending``: password accepted, second
  factor outstanding) lives at most :data:`PENDING_TTL`.
* Rotation (A3): :func:`rotate_session` issues a new id and revokes the old
  row; the login flows create a fresh session (never reuse a presented one),
  and :func:`complete_mfa`, :func:`record_step_up` and
  :func:`switch_household` rotate. Rotation never extends the absolute
  lifetime, except when a pending session becomes a full one.
* Users list and revoke their sessions (:func:`list_sessions`,
  :func:`revoke_session`); a password change calls
  :func:`revoke_other_sessions`.
* :func:`session_id_from_cookie` is the CSRF session-id resolver
  (``app.core.csrf``): cheap, no database access, it only returns a
  well-formed cookie value. ``create_app`` registers it.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection
from starlette.responses import Response

from app.auth.models import USER_AGENT_MAX, LoginMethod, SessionStage, UserSession
from app.core import clock

if TYPE_CHECKING:
    from app.core.settings import Settings

SESSION_COOKIE: Final = "__Host-perfin-session"
DEV_SESSION_COOKIE: Final = "perfin-session-dev"
SESSION_ID_BYTES: Final = 32
_SESSION_ID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{43}$")

#: How long a password-accepted, MFA-pending session may wait for the factor.
PENDING_TTL: Final = timedelta(minutes=10)
#: ``last_seen_at``/idle expiry are written at most this often per session.
TOUCH_INTERVAL: Final = timedelta(seconds=60)

#: ``hook(db, old_row, new_row)`` runs inside :func:`rotate_session` after the
#: new row is flushed, so data keyed by a session can follow it to its new id
#: (``app.auth.oidc.logout.follow_rotation``). Register with
#: :func:`register_rotation_hook`.
RotationHook = Callable[[Session, UserSession, UserSession], None]
ROTATION_HOOKS: list[RotationHook] = []


def register_rotation_hook(hook: RotationHook) -> RotationHook:
    """Call ``hook`` on every rotation (idempotent; usable as a decorator)."""
    if hook not in ROTATION_HOOKS:
        ROTATION_HOOKS.append(hook)
    return hook


@dataclass(frozen=True)
class SessionPolicy:
    """Lifetimes derived from settings."""

    idle: timedelta = timedelta(minutes=30)
    absolute: timedelta = timedelta(hours=12)
    remember: timedelta = timedelta(days=30)

    @classmethod
    def from_settings(cls, settings: Settings) -> SessionPolicy:
        return cls(
            idle=timedelta(minutes=settings.session_idle_minutes),
            absolute=timedelta(hours=settings.session_absolute_hours),
            remember=timedelta(days=settings.session_remember_days),
        )


@dataclass(frozen=True)
class IssuedSession:
    """A newly created session: the cookie value (only here) and the row."""

    raw_id: str = field(repr=False)
    row: UserSession


def new_session_id() -> str:
    """A random 256-bit session id, base64url without padding (43 chars)."""
    return secrets.token_urlsafe(SESSION_ID_BYTES)


def is_well_formed(raw_id: str | None) -> bool:
    return raw_id is not None and _SESSION_ID_RE.fullmatch(raw_id) is not None


def hash_session_id(raw_id: str) -> str:
    """SHA-256 hex of the cookie value (what the database stores)."""
    return hashlib.sha256(raw_id.encode("ascii")).hexdigest()


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------


def cookie_name(settings: Settings) -> str:
    """``__Host-perfin-session``, or ``perfin-session-dev`` with insecure dev cookies."""
    return SESSION_COOKIE if settings.secure_cookies else DEV_SESSION_COOKIE


def raw_session_id(conn: HTTPConnection, settings: Settings) -> str | None:
    """The well-formed session cookie value of a request, if any."""
    value = conn.cookies.get(cookie_name(settings))
    return value if is_well_formed(value) else None


def session_id_from_cookie(conn: HTTPConnection) -> str | None:
    """CSRF session-id resolver (``CSRFProtect.set_session_id_resolver``).

    Returns the cookie value without touching the database, so a CSRF token
    is bound to whatever session the browser presents. ``GET /auth/session``
    clears a cookie that no longer maps to a live session, after which the
    pre-auth token applies again.
    """
    settings = getattr(conn.app.state, "settings", None)
    if settings is None:
        return None
    return raw_session_id(conn, settings)


def set_session_cookie(response: Response, settings: Settings, issued: IssuedSession) -> None:
    """Set the session cookie for ``issued`` on ``response``."""
    max_age: int | None = None
    if issued.row.remember_device:
        remaining = issued.row.absolute_expires_at - clock.utcnow()
        max_age = max(int(remaining.total_seconds()), 0)
    response.set_cookie(
        cookie_name(settings),
        issued.raw_id,
        max_age=max_age,
        path="/",
        secure=settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        cookie_name(settings),
        path="/",
        secure=settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )


# ---------------------------------------------------------------------------
# Life cycle
# ---------------------------------------------------------------------------


def _truncate_agent(user_agent: str | None) -> str | None:
    if not user_agent:
        return None
    return user_agent[:USER_AGENT_MAX]


def _expiries(
    policy: SessionPolicy, now: datetime, *, stage: SessionStage, remember: bool
) -> tuple[datetime, datetime]:
    """``(idle_expires_at, absolute_expires_at)`` for a session starting ``now``."""
    if stage is SessionStage.MFA_PENDING:
        absolute = now + PENDING_TTL
        return absolute, absolute
    if remember:
        # A remembered device is not logged out for being idle; it still has
        # an absolute lifetime and step-up still needs fresh MFA.
        absolute = now + policy.remember
        return absolute, absolute
    absolute = now + policy.absolute
    return min(now + policy.idle, absolute), absolute


def create_session(
    db: Session,
    *,
    user_id: uuid.UUID,
    policy: SessionPolicy,
    login_method: LoginMethod,
    stage: SessionStage = SessionStage.AUTHENTICATED,
    household_id: uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    mfa_verified: bool = False,
    remember_device: bool = False,
    now: datetime | None = None,
) -> IssuedSession:
    """Create a session with a fresh random id (never one the client chose)."""
    now = now or clock.utcnow()
    raw = new_session_id()
    idle_expires, absolute_expires = _expiries(policy, now, stage=stage, remember=remember_device)
    row = UserSession(
        token_hash=hash_session_id(raw),
        user_id=user_id,
        household_id=household_id,
        stage=stage,
        login_method=login_method,
        user_agent=_truncate_agent(user_agent),
        ip=ip,
        created_at=now,
        updated_at=now,
        last_seen_at=now,
        idle_expires_at=idle_expires,
        absolute_expires_at=absolute_expires,
        mfa_verified_at=now if mfa_verified else None,
        remember_device=remember_device,
    )
    db.add(row)
    db.flush()
    return IssuedSession(raw, row)


def is_live(row: UserSession, now: datetime) -> bool:
    """Not revoked and neither idle nor absolute expiry has passed."""
    return row.revoked_at is None and now < row.idle_expires_at and now < row.absolute_expires_at


def load_session(
    db: Session,
    raw_id: str | None,
    *,
    policy: SessionPolicy,
    now: datetime | None = None,
    touch: bool = True,
    for_update: bool = False,
    accept: Callable[[UserSession], bool] | None = None,
) -> UserSession | None:
    """The live session for a cookie value, or ``None``.

    With ``touch`` the idle expiry slides forward (at most once per
    :data:`TOUCH_INTERVAL`, never past the absolute expiry). ``accept`` is a
    per-request condition on the row (the break-glass network check in
    ``app.auth.deps``): a row it refuses is treated as absent and is not
    touched, so a refused request never keeps the session alive.
    """
    if raw_id is None or not is_well_formed(raw_id):
        return None
    now = now or clock.utcnow()
    stmt = select(UserSession).where(UserSession.token_hash == hash_session_id(raw_id))
    if for_update:
        stmt = stmt.with_for_update()
    row = db.execute(stmt).scalar_one_or_none()
    if row is None or not is_live(row, now):
        return None
    if accept is not None and not accept(row):
        return None
    if touch and now - row.last_seen_at >= TOUCH_INTERVAL:
        row.last_seen_at = now
        if row.stage is SessionStage.AUTHENTICATED and not row.remember_device:
            row.idle_expires_at = min(now + policy.idle, row.absolute_expires_at)
        db.flush()
    return row


def revoke(row: UserSession, *, reason: str, now: datetime | None = None) -> None:
    if row.revoked_at is None:
        row.revoked_at = now or clock.utcnow()
        row.revoked_reason = reason[:64]


def rotate_session(
    db: Session,
    current: UserSession,
    *,
    policy: SessionPolicy,
    reason: str,
    stage: SessionStage | None = None,
    mfa_verified_at: datetime | None = None,
    household_id: uuid.UUID | None = None,
    change_household: bool = False,
    now: datetime | None = None,
) -> IssuedSession:
    """Replace ``current`` with a new id carrying the same (or updated) state.

    The old row is revoked with ``reason``. The absolute expiry is kept, so
    rotating cannot extend a session, except when an ``mfa_pending`` session
    becomes ``authenticated`` (its lifetime starts then).
    """
    now = now or clock.utcnow()
    new_stage = stage or current.stage
    if current.stage is SessionStage.MFA_PENDING and new_stage is SessionStage.AUTHENTICATED:
        idle_expires, absolute_expires = _expiries(
            policy, now, stage=new_stage, remember=current.remember_device
        )
    else:
        absolute_expires = current.absolute_expires_at
        if new_stage is SessionStage.AUTHENTICATED and not current.remember_device:
            idle_expires = min(now + policy.idle, absolute_expires)
        else:
            idle_expires = current.idle_expires_at
    raw = new_session_id()
    row = UserSession(
        token_hash=hash_session_id(raw),
        user_id=current.user_id,
        household_id=household_id if change_household else current.household_id,
        stage=new_stage,
        login_method=current.login_method,
        user_agent=current.user_agent,
        ip=current.ip,
        created_at=now,
        updated_at=now,
        last_seen_at=now,
        idle_expires_at=idle_expires,
        absolute_expires_at=absolute_expires,
        mfa_verified_at=mfa_verified_at or current.mfa_verified_at,
        remember_device=current.remember_device,
        rotated_from_id=current.id,
    )
    revoke(current, reason=reason, now=now)
    db.add(row)
    db.flush()
    for hook in ROTATION_HOOKS:
        hook(db, current, row)
    return IssuedSession(raw, row)


def complete_mfa(
    db: Session, current: UserSession, *, policy: SessionPolicy, now: datetime | None = None
) -> IssuedSession:
    """Second factor verified: rotate into a full session with ``mfa_verified_at``."""
    now = now or clock.utcnow()
    return rotate_session(
        db,
        current,
        policy=policy,
        reason="mfa_completed",
        stage=SessionStage.AUTHENTICATED,
        mfa_verified_at=now,
        now=now,
    )


def record_step_up(
    db: Session, current: UserSession, *, policy: SessionPolicy, now: datetime | None = None
) -> IssuedSession:
    """A fresh MFA verification on a full session (step-up): rotate and stamp."""
    now = now or clock.utcnow()
    return rotate_session(
        db, current, policy=policy, reason="step_up", mfa_verified_at=now, now=now
    )


def switch_household(
    db: Session,
    current: UserSession,
    household_id: uuid.UUID | None,
    *,
    policy: SessionPolicy,
    now: datetime | None = None,
) -> IssuedSession:
    """Act in another household (a privilege change): rotate.

    The caller must have checked the membership; the principal resolver
    re-checks it on every request anyway.
    """
    return rotate_session(
        db,
        current,
        policy=policy,
        reason="privilege_change",
        household_id=household_id,
        change_household=True,
        now=now,
    )


def list_sessions(
    db: Session, user_id: uuid.UUID, *, now: datetime | None = None
) -> list[UserSession]:
    """The user's live sessions, most recently used first."""
    now = now or clock.utcnow()
    rows = db.execute(
        select(UserSession)
        .where(
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
            UserSession.idle_expires_at > now,
            UserSession.absolute_expires_at > now,
        )
        .order_by(UserSession.last_seen_at.desc(), UserSession.id.desc())
    ).scalars()
    return list(rows)


def revoke_session(
    db: Session,
    session_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    reason: str = "revoked",
    now: datetime | None = None,
) -> bool:
    """Revoke one of ``user_id``'s sessions; ``False`` if no such live session."""
    now = now or clock.utcnow()
    result = db.execute(
        update(UserSession)
        .where(
            UserSession.id == session_id,
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_reason=reason[:64], updated_at=now)
        .execution_options(synchronize_session="fetch")
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


def revoke_other_sessions(
    db: Session,
    user_id: uuid.UUID,
    *,
    keep_session_id: uuid.UUID | None,
    reason: str = "revoked_others",
    now: datetime | None = None,
) -> int:
    """Revoke every live session of the user except ``keep_session_id``.

    Called on password change (AUTH.md) and by "log out other devices".
    ``keep_session_id=None`` revokes all of them (account disabled, admin
    reset).
    """
    now = now or clock.utcnow()
    stmt = (
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason[:64], updated_at=now)
        .execution_options(synchronize_session="fetch")
    )
    if keep_session_id is not None:
        stmt = stmt.where(UserSession.id != keep_session_id)
    result = db.execute(stmt)
    return int(result.rowcount)  # type: ignore[attr-defined]


def revoke_all_sessions(
    db: Session, user_id: uuid.UUID, *, reason: str = "revoked_all", now: datetime | None = None
) -> int:
    return revoke_other_sessions(db, user_id, keep_session_id=None, reason=reason, now=now)


def prune_sessions(db: Session, *, older_than: timedelta, now: datetime | None = None) -> int:
    """Delete sessions that ended (expired or revoked) more than ``older_than`` ago."""
    now = now or clock.utcnow()
    cutoff = now - older_than
    result = db.execute(
        delete(UserSession).where(
            or_(
                UserSession.absolute_expires_at < cutoff,
                UserSession.idle_expires_at < cutoff,
                UserSession.revoked_at < cutoff,
            )
        )
    )
    return int(result.rowcount)  # type: ignore[attr-defined]

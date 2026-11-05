# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rate limits and progressive lockout (SECURITY.md A1, AUTH.md "Brute force").

**Sliding windows.** :class:`RateLimiter` keeps one ``rate_limit_event`` row
per allowed attempt, keyed by ``(scope, sha256(scope, key))``; an attempt is
allowed while fewer than ``limit.max_events`` rows of that key fall inside
``limit.window``. Keys are per IP (``ip_key``: the client address from
``app.core.request_context``, which honours forwarded headers only from
``PERFIN_TRUSTED_PROXIES``) and per account (``account_key``). Each check
runs in **its own transaction** (with a per-key advisory lock, so concurrent
attempts cannot overshoot): a login failure answered with an error rolls the
request transaction back, but the counted attempt must stay counted.

**Progressive lockout** on ``app_user``: :func:`register_login_failure`
(also its own transaction) bumps ``failed_login_count``; from
``LockoutPolicy.delay_after`` failures on, ``locked_until`` is pushed out by
a delay that doubles per failure (no sleeping in a request thread), and from
``lock_after`` failures the account is locked for ``lock_duration``.
:func:`register_login_success` resets both. The login flow must answer a
locked account exactly like a wrong password (A2) and notify the user (Phase
7 channels).

:func:`prune` deletes events older than the longest window in use.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import HTTPConnection

from app.auth.models import RateLimitEvent, User
from app.core import clock
from app.core.errors import ProblemError
from app.core.request_context import client_ip

_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 7305))")


@dataclass(frozen=True)
class Limit:
    """At most ``max_events`` attempts per ``window`` for one key of ``scope``."""

    scope: str
    max_events: int
    window: timedelta

    def __post_init__(self) -> None:
        if not self.scope or len(self.scope) > 64:
            raise ValueError("scope must be 1..64 characters")
        if self.max_events < 1 or self.window <= timedelta(0):
            raise ValueError("limit and window must be positive")


# Defaults for the Phase 1 flows (login, MFA, reset, OIDC callback).
LOGIN_PER_IP: Final = Limit("login:ip", 30, timedelta(minutes=5))
LOGIN_PER_ACCOUNT: Final = Limit("login:account", 10, timedelta(minutes=15))
MFA_PER_SESSION: Final = Limit("mfa:session", 5, timedelta(minutes=5))
MFA_PER_ACCOUNT: Final = Limit("mfa:account", 10, timedelta(minutes=15))
RESET_PER_IP: Final = Limit("reset:ip", 5, timedelta(minutes=15))
OIDC_CALLBACK_PER_IP: Final = Limit("oidc:ip", 30, timedelta(minutes=5))
DEFAULT_LIMITS: Final = (
    LOGIN_PER_IP,
    LOGIN_PER_ACCOUNT,
    MFA_PER_SESSION,
    MFA_PER_ACCOUNT,
    RESET_PER_IP,
    OIDC_CALLBACK_PER_IP,
)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    count: int
    limit: int
    retry_after_seconds: int

    def raise_if_denied(self) -> None:
        """``429 rate-limited`` with ``Retry-After`` when not allowed."""
        if not self.allowed:
            raise too_many_requests(self.retry_after_seconds)


def too_many_requests(retry_after_seconds: int) -> ProblemError:
    return ProblemError(
        429,
        "rate-limited",
        "Too many requests",
        "Too many attempts. Try again later.",
        headers={"Retry-After": str(max(retry_after_seconds, 1))},
    )


def key_hash(scope: str, key: str) -> str:
    """What is stored instead of the raw key (an IP address or account id)."""
    return hashlib.sha256(f"{scope}\x00{key}".encode()).hexdigest()


def ip_key(conn: HTTPConnection) -> str:
    """The client address (trusted-proxy aware) as a rate-limit key."""
    return client_ip(conn) or "unknown"


def account_key(user_id: uuid.UUID | None, username: str | None = None) -> str:
    """Per-account key: the user id, or the normalised username when unknown.

    Keying unknown usernames too keeps "no such user" and "wrong password"
    limited identically (A2).
    """
    if user_id is not None:
        return f"user:{user_id}"
    return f"name:{(username or '').strip().lower()}"


class RateLimiter:
    """Sliding-window limiter over ``rate_limit_event``; one transaction per call."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory

    def _decide(self, db: Session, limit: Limit, digest: str, now: datetime) -> Decision:
        since = now - limit.window
        count, oldest = db.execute(
            select(func.count(), func.min(RateLimitEvent.created_at)).where(
                RateLimitEvent.scope == limit.scope,
                RateLimitEvent.key_hash == digest,
                RateLimitEvent.created_at > since,
            )
        ).one()
        count = int(count)
        if count < limit.max_events:
            return Decision(True, count, limit.max_events, 0)
        retry = (oldest + limit.window - now).total_seconds() if oldest else 1.0
        return Decision(False, count, limit.max_events, max(math.ceil(retry), 1))

    def hit(self, limit: Limit, key: str, *, now: datetime | None = None) -> Decision:
        """Count one attempt if allowed; return the decision."""
        now = now or clock.utcnow()
        digest = key_hash(limit.scope, key)
        with self._factory() as db, db.begin():
            db.execute(_LOCK_SQL, {"k": digest})  # the digest covers scope and key
            # Keep each key's history bounded to its window.
            db.execute(
                delete(RateLimitEvent).where(
                    RateLimitEvent.scope == limit.scope,
                    RateLimitEvent.key_hash == digest,
                    RateLimitEvent.created_at <= now - limit.window,
                )
            )
            decision = self._decide(db, limit, digest, now)
            if decision.allowed:
                db.add(RateLimitEvent(scope=limit.scope, key_hash=digest, created_at=now))
                decision = Decision(True, decision.count + 1, limit.max_events, 0)
        return decision

    def peek(self, limit: Limit, key: str, *, now: datetime | None = None) -> Decision:
        """The decision :meth:`hit` would make, without counting."""
        now = now or clock.utcnow()
        with self._factory() as db, db.begin():
            return self._decide(db, limit, key_hash(limit.scope, key), now)

    def reset(self, limit: Limit, key: str) -> None:
        """Forget a key's attempts (e.g. per-account after a successful login)."""
        with self._factory() as db, db.begin():
            db.execute(
                delete(RateLimitEvent).where(
                    RateLimitEvent.scope == limit.scope,
                    RateLimitEvent.key_hash == key_hash(limit.scope, key),
                )
            )

    def prune(self, older_than: timedelta | None = None, *, now: datetime | None = None) -> int:
        """Delete events older than ``older_than`` (default: longest default window)."""
        now = now or clock.utcnow()
        horizon = older_than or max(limit.window for limit in DEFAULT_LIMITS)
        with self._factory() as db, db.begin():
            result = db.execute(
                delete(RateLimitEvent).where(RateLimitEvent.created_at < now - horizon)
            )
            return int(result.rowcount)  # type: ignore[attr-defined]


def get_rate_limiter(conn: HTTPConnection) -> RateLimiter:
    """A :class:`RateLimiter` on the app's session factory."""
    return RateLimiter(conn.app.state.sessionmaker)


# ---------------------------------------------------------------------------
# Progressive lockout on app_user
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LockoutPolicy:
    delay_after: int = 3
    base_delay: timedelta = timedelta(seconds=1)
    max_delay: timedelta = timedelta(minutes=5)
    lock_after: int = 10
    lock_duration: timedelta = timedelta(minutes=15)

    def locked_until(self, failures: int, now: datetime) -> datetime | None:
        """When the next attempt may be made after ``failures`` consecutive failures."""
        if failures >= self.lock_after:
            return now + self.lock_duration
        if failures >= self.delay_after:
            exponent = min(failures - self.delay_after, 30)
            delay: timedelta = min(self.base_delay * (2**exponent), self.max_delay)
            return now + delay
        return None


DEFAULT_LOCKOUT: Final = LockoutPolicy()


def is_locked(user: User, now: datetime | None = None) -> bool:
    now = now or clock.utcnow()
    return user.locked_until is not None and user.locked_until > now


@dataclass(frozen=True)
class FailureOutcome:
    failed_login_count: int
    locked_until: datetime | None
    newly_locked: bool


def register_login_failure(
    session_factory: sessionmaker[Session],
    user_id: uuid.UUID,
    *,
    policy: LockoutPolicy = DEFAULT_LOCKOUT,
    now: datetime | None = None,
) -> FailureOutcome:
    """Count a failed login for ``user_id`` in its own transaction."""
    now = now or clock.utcnow()
    with session_factory() as db, db.begin():
        user = db.execute(select(User).where(User.id == user_id).with_for_update()).scalar_one()
        failures = user.failed_login_count + 1
        until = policy.locked_until(failures, now)
        user.failed_login_count = failures
        user.last_failed_login_at = now
        if until is not None and (user.locked_until is None or until > user.locked_until):
            user.locked_until = until
        # The long lock starts exactly at ``lock_after`` (notify the user then).
        newly_locked = failures == policy.lock_after
        return FailureOutcome(failures, user.locked_until, newly_locked)


def register_login_success(db: Session, user_id: uuid.UUID, *, now: datetime | None = None) -> None:
    """Reset the failure counter and lock (in the caller's transaction)."""
    now = now or clock.utcnow()
    db.execute(
        update(User)
        .where(User.id == user_id)
        .values(failed_login_count=0, locked_until=None, last_login_at=now, updated_at=now)
        .execution_options(synchronize_session="fetch")
    )

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local username/password login and password management (AUTH.md "Local accounts").

**Login** (:func:`login`, step 1 of ``POST /auth/login``):

1. OIDC-only mode (``PERFIN_LOCAL_LOGIN_ENABLED=false``): requests from
   outside ``PERFIN_BREAKGLASS_CIDRS`` are refused with
   :class:`LocalLoginDisabled` before any account is looked at (the answer is
   the same for every username). From inside, only ``is_breakglass`` users
   can succeed (SECURITY.md B7).
2. Rate limits (A1): per client IP and per account
   (``app.auth.ratelimit``); unknown usernames are keyed and limited exactly
   like real accounts. Each hit is committed in its own transaction.
3. The password is always verified once at full cost: against the stored
   Argon2id hash, or with ``passwords.dummy_verify`` when there is no such
   user or no local password (A2).
4. Every refusal (unknown user, wrong password, locked or delayed account,
   inactive account, break-glass account outside its CIDR, non-break-glass
   account in OIDC-only mode) raises the same :class:`LoginFailed`, which the
   router turns into one identical ``401 invalid-credentials`` problem. Only
   the audit row records the reason.
5. A wrong password counts towards the progressive lockout
   (``ratelimit.register_login_failure``: growing delay, then a 15 minute
   lock); when the lock starts, :func:`notify_account_locked` writes an audit
   row, emits a ``user.locked`` event in each of the user's households and
   calls the registered lock notifiers (notification channels: Phase 7).
6. Success: the hash is upgraded if its parameters are outdated, the failure
   counter is reset (only once the second factor succeeds when the account
   has one: MFA failures feed the same counter, and a correct password alone
   must not clear it) and a **fresh** session is created through
   ``sessions.create_session`` (never a client-supplied id, A3). A user with a
   TOTP or WebAuthn factor gets an ``mfa_pending`` session that only
   ``POST /auth/mfa/verify`` accepts (it finishes with
   ``sessions.complete_mfa``, which rotates the id); otherwise a full
   session (restricted to MFA enrolment when ``PERFIN_REQUIRE_MFA`` is on);
   the stage comes from ``app.auth.mfa.policy.login_stage``.

**New passwords** (:func:`validate_new_password`): 12..128 characters, no
composition rules, not the username, not on the bundled breached-password
list, and not known to Have I Been Pwned when ``PERFIN_HIBP_CHECK_ENABLED``.

Audit rows for these events are instance-level (``household_id`` NULL),
written in a system-actor RLS context, and never contain a password, hash or
token; failures are written in their own transaction so the rolled-back
request does not take them along.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import HTTPConnection

from app.auth import breached
from app.auth.mfa.policy import login_stage
from app.auth.models import (
    Credential,
    LoginMethod,
    MfaMethod,
    SessionStage,
    User,
    normalize_username,
)
from app.auth.passwords import (
    DEFAULT_PARAMS,
    PasswordPolicyError,
    check_password_policy,
    dummy_verify,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.auth.principal import PRIMARY_MFA_TYPES, memberships
from app.auth.ratelimit import (
    DEFAULT_LOCKOUT,
    LOGIN_PER_ACCOUNT,
    LOGIN_PER_IP,
    RESET_PER_IP,
    Limit,
    LockoutPolicy,
    RateLimiter,
    account_key,
    ip_key,
    is_locked,
    register_login_failure,
    register_login_success,
)
from app.auth.sessions import IssuedSession, SessionPolicy, create_session, load_session, revoke
from app.core import clock
from app.core.audit import RequestMeta, audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.logging import get_logger
from app.core.request_context import client_ip
from app.core.settings import IPNetwork, Settings
from app.db.rls import get_context, restore_context, set_context
from app.domain.notifications.devices import note_sign_in
from app.domain.notifications.routing import notify

log = get_logger(__name__)

#: Upper bounds on what the login form accepts (longer input is a 422 for
#: everyone, so it reveals nothing about accounts).
USERNAME_INPUT_MAX: Final = 320
PASSWORD_INPUT_MAX: Final = 1024


# ---------------------------------------------------------------------------
# Limits (overridable per app for tests: ``app.state.auth_limits``)
# ---------------------------------------------------------------------------


PASSWORD_CHANGE_PER_ACCOUNT: Final = Limit("password:account", 5, timedelta(minutes=15))
SETUP_PER_IP: Final = Limit("setup:ip", 10, timedelta(minutes=15))
# Windows stay within the longest default window: auth_prune trims older events.
RESET_REQUEST_PER_ACCOUNT: Final = Limit("reset:account", 3, timedelta(minutes=15))
RESET_CONFIRM_PER_IP: Final = Limit("reset_confirm:ip", 10, timedelta(minutes=15))


@dataclass(frozen=True)
class AuthLimits:
    """Rate limits and lockout used by the local-login endpoints."""

    login_per_ip: Limit = LOGIN_PER_IP
    login_per_account: Limit = LOGIN_PER_ACCOUNT
    password_change_per_account: Limit = PASSWORD_CHANGE_PER_ACCOUNT
    setup_per_ip: Limit = SETUP_PER_IP
    reset_request_per_ip: Limit = RESET_PER_IP
    reset_request_per_account: Limit = RESET_REQUEST_PER_ACCOUNT
    reset_confirm_per_ip: Limit = RESET_CONFIRM_PER_IP
    lockout: LockoutPolicy = DEFAULT_LOCKOUT


DEFAULT_AUTH_LIMITS: Final = AuthLimits()


def get_auth_limits(conn: HTTPConnection) -> AuthLimits:
    limits = getattr(conn.app.state, "auth_limits", None)
    return limits if isinstance(limits, AuthLimits) else DEFAULT_AUTH_LIMITS


def get_sessionmaker(conn: HTTPConnection) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = conn.app.state.sessionmaker
    return factory


# ---------------------------------------------------------------------------
# Client address and break-glass CIDRs (B7)
# ---------------------------------------------------------------------------


def request_ip(conn: HTTPConnection) -> str | None:
    """The client IP (trusted-proxy aware) if it is a valid address, else ``None``."""
    value = client_ip(conn)
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def ip_in_networks(ip: str | None, networks: Iterable[IPNetwork]) -> bool:
    """True when ``ip`` lies in one of ``networks`` (IPv4-mapped IPv6 unwrapped)."""
    if ip is None:
        return False
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address.version == net.version and address in net for net in networks)


def from_breakglass_network(settings: Settings, ip: str | None) -> bool:
    return ip_in_networks(ip, settings.breakglass_cidrs)


def local_login_available(settings: Settings, ip: str | None) -> bool:
    """Whether the local password form is offered to a client at ``ip``."""
    return settings.local_login_enabled or from_breakglass_network(settings, ip)


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


@contextmanager
def system_actor(db: Session, user_id: uuid.UUID | None = None) -> Iterator[None]:
    """Run in the system-actor RLS context (instance-level audit rows)."""
    saved = get_context(db)
    set_context(db, None, user_id, system_actor=True)
    try:
        yield
    finally:
        restore_context(db, saved)


def audit_auth(
    db: Session,
    *,
    action: str,
    user_id: uuid.UUID | None,
    meta: RequestMeta,
    after: Mapping[str, Any] | None = None,
    target_type: str = "user",
    target_id: uuid.UUID | None = None,
) -> None:
    """An instance-level audit row for an authentication event (caller's transaction)."""
    with system_actor(db, user_id):
        audit(
            db,
            action=action,
            target_type=target_type,
            target_id=target_id if target_id is not None else user_id,
            user_id=user_id,
            after=dict(after) if after is not None else None,
            meta=meta,
        )


def audit_auth_committed(
    factory: sessionmaker[Session],
    *,
    action: str,
    user_id: uuid.UUID | None,
    meta: RequestMeta,
    after: Mapping[str, Any] | None = None,
) -> None:
    """:func:`audit_auth` in its own transaction (survives a failed request)."""
    with factory() as db, db.begin():
        audit_auth(db, action=action, user_id=user_id, meta=meta, after=after)


# ---------------------------------------------------------------------------
# Lock notification hook (A1; channels arrive in Phase 7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LockNotice:
    """An account entered the long lock after repeated failed logins."""

    user_id: uuid.UUID
    locked_until: datetime | None
    ip: str | None


LockNotifier = Callable[[LockNotice], None]
LOCK_NOTIFIERS: list[LockNotifier] = []


def register_lock_notifier(notifier: LockNotifier) -> LockNotifier:
    """Register a callback for account locks (usable as a decorator)."""
    LOCK_NOTIFIERS.append(notifier)
    return notifier


def notify_account_locked(
    factory: sessionmaker[Session], notice: LockNotice, meta: RequestMeta
) -> None:
    """Audit the lock, emit ``user.locked`` per household, call the notifiers."""
    with factory() as db, db.begin():
        audit_auth(
            db,
            action="user.locked",
            user_id=notice.user_id,
            meta=meta,
            after={"locked_until": notice.locked_until},
        )
        payload = {
            "user_id": str(notice.user_id),
            "locked_until": notice.locked_until.isoformat() if notice.locked_until else None,
        }
        for member in memberships(db, notice.user_id):
            set_context(db, member.household_id)
            event_id = emit(db, member.household_id, "user.locked", payload)
            notify(db, member.household_id, "user.locked", source_id=event_id)
    for notifier in list(LOCK_NOTIFIERS):
        try:
            notifier(notice)
        except Exception as exc:  # noqa: BLE001 - a notifier must never break the login path
            log.warning("lock notifier failed", extra={"error": type(exc).__name__})


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def validate_new_password(
    password: str, settings: Settings, *, username: str | None = None
) -> None:
    """Raise :class:`PasswordPolicyError` unless ``password`` may be set."""
    check_password_policy(password)
    if username and password.strip().lower() == username.strip().lower():
        raise PasswordPolicyError("same-as-username", "The password must not be the username.")
    if breached.is_breached(password, hibp=settings.hibp_check_enabled):
        raise PasswordPolicyError(
            "breached",
            "This password appears in a list of breached passwords. Choose another one.",
        )


def weak_password(exc: PasswordPolicyError) -> ProblemError:
    """``422 weak-password`` with the stable ``reason`` code."""
    return ProblemError(
        422, "weak-password", "Password not accepted", str(exc), extra={"reason": exc.code}
    )


def set_password(
    db: Session, user_id: uuid.UUID, password: str, *, now: datetime | None = None
) -> Credential:
    """Store a new Argon2id hash (baseline parameters) for ``user_id``."""
    now = now or clock.utcnow()
    hashed = hash_password(password, DEFAULT_PARAMS)
    credential = db.execute(
        select(Credential).where(Credential.user_id == user_id).with_for_update()
    ).scalar_one_or_none()
    if credential is None:
        credential = Credential(
            user_id=user_id,
            password_hash=hashed.encoded,
            hash_params=hashed.params_dict(),
            rotated_at=now,
        )
        db.add(credential)
    else:
        credential.password_hash = hashed.encoded
        credential.hash_params = hashed.params_dict()
        credential.rotated_at = now
        credential.updated_at = now
    db.flush()
    return credential


def factor_types(db: Session, user_id: uuid.UUID) -> list[str]:
    """The user's enrolled primary MFA types (``totp``, ``webauthn``), sorted."""
    types = db.execute(
        select(MfaMethod.type)
        .where(MfaMethod.user_id == user_id, MfaMethod.type.in_(PRIMARY_MFA_TYPES))
        .distinct()
    ).scalars()
    return sorted(t.value for t in types)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class LoginFailed(Exception):  # noqa: N818 - an outcome, not a programming error
    """Any refused login. The reason is for the audit trail only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LocalLoginDisabled(Exception):  # noqa: N818 - an outcome
    """OIDC-only mode and the client is outside the break-glass CIDRs."""


def invalid_credentials() -> ProblemError:
    """The one response for every refused login (SECURITY.md A2)."""
    return ProblemError(
        401,
        "invalid-credentials",
        "Login failed",
        "The username or password is incorrect.",
    )


@dataclass(frozen=True)
class LoginResult:
    issued: IssuedSession = field(repr=False)
    user_id: uuid.UUID
    mfa_methods: tuple[str, ...]

    @property
    def mfa_pending(self) -> bool:
        return self.issued.row.stage is SessionStage.MFA_PENDING


@dataclass(frozen=True)
class _Account:
    user: User | None
    credential: Credential | None


def _load_account(factory: sessionmaker[Session], username: str) -> _Account:
    with factory() as db, db.begin():
        user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
        credential = (
            db.execute(select(Credential).where(Credential.user_id == user.id)).scalar_one_or_none()
            if user is not None
            else None
        )
    return _Account(user, credential)


def _refusal_reason(
    account: _Account,
    *,
    password_ok: bool,
    settings: Settings,
    from_breakglass: bool,
    now: datetime,
) -> str | None:
    user = account.user
    if user is None:
        return "unknown_user"
    if not user.is_active:
        return "inactive"
    if account.credential is None:
        return "no_password"
    if user.is_breakglass and not from_breakglass:
        return "breakglass_outside_cidr"
    if not settings.local_login_enabled and not user.is_breakglass:
        return "local_login_disabled"
    if is_locked(user, now):
        return "locked"
    if not password_ok:
        return "bad_password"
    return None


def login(
    request: HTTPConnection,
    settings: Settings,
    *,
    username: str,
    password: str,
    remember_device: bool = False,
    previous_session_id: str | None = None,
    limits: AuthLimits | None = None,
) -> LoginResult:
    """Step 1 of the local login; see the module doc for every rule.

    ``previous_session_id`` is the session cookie the browser presented, if
    any: on success that session is revoked (replaced by the new one).
    """
    limits = limits or get_auth_limits(request)
    factory = get_sessionmaker(request)
    meta = RequestMeta.from_request(request)
    ip = request_ip(request)
    from_breakglass = from_breakglass_network(settings, ip)
    if not settings.local_login_enabled and not from_breakglass:
        raise LocalLoginDisabled

    limiter = RateLimiter(factory)
    limiter.hit(limits.login_per_ip, ip_key(request)).raise_if_denied()
    name = normalize_username(username)
    account = _load_account(factory, name)
    key = account_key(account.user.id if account.user else None, name)
    limiter.hit(limits.login_per_account, key).raise_if_denied()

    # Exactly one full-cost verification on every path (A2).
    if account.credential is not None:
        password_ok = verify_password(account.credential.password_hash, password)
    else:
        password_ok = dummy_verify(password)
    now = clock.utcnow()
    reason = _refusal_reason(
        account,
        password_ok=password_ok,
        settings=settings,
        from_breakglass=from_breakglass,
        now=now,
    )
    user = account.user
    if reason is not None:
        user_id = user.id if user is not None else None
        if reason == "bad_password" and user is not None:
            outcome = register_login_failure(factory, user.id, policy=limits.lockout, now=now)
            if outcome.newly_locked:
                notify_account_locked(factory, LockNotice(user.id, outcome.locked_until, ip), meta)
        audit_auth_committed(
            factory,
            action="user.login_failed",
            user_id=user_id,
            meta=meta,
            after={"method": "local", "reason": reason},
        )
        raise LoginFailed(reason)

    if user is None or account.credential is None:  # pragma: no cover - _refusal_reason
        raise LoginFailed("unknown_user")
    method = LoginMethod.BREAKGLASS if user.is_breakglass else LoginMethod.LOCAL
    policy = SessionPolicy.from_settings(settings)
    with factory() as db, db.begin():
        previous = load_session(db, previous_session_id, policy=policy, now=now, touch=False)
        if previous is not None:
            revoke(previous, reason="replaced_by_login", now=now)
        if needs_rehash(account.credential.password_hash):
            # Parameters were raised since this hash was made (AUTH.md).
            set_password(db, user.id, password, now=now)
        stage = login_stage(db, user.id, idp_mfa=False)
        if stage is not SessionStage.MFA_PENDING:
            register_login_success(db, user.id, now=now)
        # else: the counter (fed by password *and* MFA failures) is reset only
        # once the second factor succeeds (``POST /auth/mfa/verify``), so a
        # correct password cannot wipe the MFA lockout (review P1-SR-02).
        methods = tuple(factor_types(db, user.id))
        found = memberships(db, user.id)
        household_id = found[0].household_id if found else None
        issued = create_session(
            db,
            user_id=user.id,
            policy=policy,
            login_method=method,
            stage=stage,
            household_id=household_id,
            ip=ip,
            user_agent=request.headers.get("user-agent"),
            mfa_verified=False,
            remember_device=remember_device,
            now=now,
        )
        note_sign_in(db, issued.row)
        audit_auth(
            db,
            action="user.login",
            user_id=user.id,
            meta=meta,
            after={
                "method": method.value,
                "stage": issued.row.stage.value,
                "remember_device": remember_device,
            },
        )
    limiter.reset(limits.login_per_account, key)
    return LoginResult(issued=issued, user_id=user.id, mfa_methods=methods)

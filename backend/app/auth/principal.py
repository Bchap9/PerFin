# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who is calling: session or personal-access-token authentication.

:class:`Principal` is what every authorised endpoint receives (through
``app.auth.deps`` / ``app.auth.authz``). It is built by
:func:`authenticate_session` (browser cookie) or :func:`authenticate_token`
(``Authorization: Bearer pfn_...``), and :func:`apply_rls_context` then sets
the transaction's RLS context (``app.db.rls.set_context``) to the active
household, the user and the instance-admin flag.

Rules enforced here:

* An inactive user authenticates nothing.
* The household comes from a current ``household_member`` row: the session's
  household if the user is still a member there, otherwise the user's first
  membership (oldest). A token is bound to its household and fails if the
  membership is gone. The role is always read from the membership, never
  from the session or token, so a demotion applies on the next request.
* Tokens: ``pfn_<prefix>_<secret>``; looked up by ``prefix`` (RLS policy
  ``token_prefix_lookup``), compared by SHA-256 in constant time, must not be
  revoked or expired; scopes narrow, never widen, the user's role: the
  principal's scopes are the token's scopes intersected with the ceiling of
  the user's *current* role (``app.auth.scopes.effective_scopes``).
* A break-glass account (``is_breakglass``, SECURITY.md B7) never
  authenticates with a token: its access must stay on the allowlisted
  network, which only the session check (``app.auth.deps.session_acceptor``)
  enforces, so its tokens are refused here (and ``app.auth.tokens`` refuses
  to mint them, ``set_breakglass`` revokes existing ones).
* ``restricted``: when ``PERFIN_REQUIRE_MFA`` is on and the user has no MFA
  method (and the session was not MFA-verified by the IdP), the principal may
  only enrol MFA (``app.auth.authz.require_mfa_if_enforced``).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.models import (
    ApiToken,
    HouseholdMember,
    HouseholdRole,
    LoginMethod,
    MfaMethod,
    MfaType,
    SessionStage,
    User,
    UserSession,
)
from app.auth.scopes import effective_scopes
from app.auth.sessions import SessionPolicy, load_session
from app.core import clock
from app.db.rls import set_context

AuthMethod = Literal["session", "token"]

TOKEN_PREFIX: Final = "pfn"  # noqa: S105 - the public token type marker, not a secret
_TOKEN_RE: Final = re.compile(r"^pfn_([0-9a-f]{12})_([A-Za-z0-9_-]{43})$")
TOKEN_SECRET_BYTES: Final = 32
#: ``last_used_at`` on a token is written at most this often.
TOKEN_TOUCH_SECONDS: Final = 60

#: MFA types that satisfy "has MFA" (recovery codes are a fallback, not a factor).
PRIMARY_MFA_TYPES: Final = (MfaType.TOTP, MfaType.WEBAUTHN)


@dataclass(frozen=True)
class Principal:
    """An authenticated caller.

    ``scopes`` is ``None`` for sessions (every user-level permission) and the
    token's effective scopes (granted scopes within the current role's
    ceiling, ``app.auth.scopes``) for personal access tokens.
    """

    user: User
    household_id: uuid.UUID | None
    role: HouseholdRole | None
    auth_method: AuthMethod
    scopes: frozenset[str] | None = None
    session_id: uuid.UUID | None = None
    token_id: uuid.UUID | None = None
    mfa_verified_at: datetime | None = None
    login_method: LoginMethod | None = None
    mfa_enrolled: bool = False
    mfa_required: bool = False
    session: UserSession | None = field(default=None, repr=False, compare=False)

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def is_instance_admin(self) -> bool:
        return bool(self.user.is_instance_admin)

    @property
    def mfa_satisfied(self) -> bool:
        """The user has a second factor, or this session was MFA-verified (OIDC ``amr``)."""
        return self.mfa_enrolled or self.mfa_verified_at is not None

    @property
    def restricted(self) -> bool:
        """MFA is required but not set up: only MFA enrolment is allowed."""
        return self.mfa_required and not self.mfa_satisfied

    def has_scope(self, scope: str) -> bool:
        return self.scopes is None or scope in self.scopes


# ---------------------------------------------------------------------------
# Membership and RLS context
# ---------------------------------------------------------------------------


def memberships(db: Session, user_id: uuid.UUID) -> list[HouseholdMember]:
    """The user's memberships, oldest first.

    Runs in a user-only context (``perfin.user_id``; the ``member_self_read``
    policy) and leaves that context set; callers set the final context next.
    """
    set_context(db, None, user_id)
    rows = db.execute(
        select(HouseholdMember)
        .where(HouseholdMember.user_id == user_id)
        .order_by(HouseholdMember.created_at, HouseholdMember.id)
    ).scalars()
    return list(rows)


def membership_in(
    db: Session, user_id: uuid.UUID, household_id: uuid.UUID
) -> HouseholdMember | None:
    for member in memberships(db, user_id):
        if member.household_id == household_id:
            return member
    return None


def has_primary_mfa(db: Session, user_id: uuid.UUID) -> bool:
    found = db.execute(
        select(MfaMethod.id)
        .where(MfaMethod.user_id == user_id, MfaMethod.type.in_(PRIMARY_MFA_TYPES))
        .limit(1)
    ).first()
    return found is not None


def apply_rls_context(db: Session, principal: Principal) -> None:
    """Set the transaction's RLS context for ``principal``."""
    set_context(
        db,
        principal.household_id,
        principal.user_id,
        instance_admin=principal.is_instance_admin,
    )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def authenticate_session(
    db: Session,
    raw_id: str | None,
    *,
    policy: SessionPolicy,
    require_mfa: bool,
    now: datetime | None = None,
    accept: Callable[[UserSession], bool] | None = None,
) -> Principal | None:
    """A principal for a full (``authenticated``) session cookie, else ``None``.

    MFA-pending sessions are not principals; the MFA verification endpoint
    loads them with :func:`app.auth.sessions.load_session`. ``accept`` is
    passed to ``load_session`` (per-request conditions on the session row).
    """
    now = now or clock.utcnow()
    row = load_session(db, raw_id, policy=policy, now=now, accept=accept)
    if row is None or row.stage is not SessionStage.AUTHENTICATED:
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    member: HouseholdMember | None = None
    if row.household_id is not None:
        member = membership_in(db, user.id, row.household_id)
    if member is None:
        found = memberships(db, user.id)
        member = found[0] if found else None
        new_household = member.household_id if member else None
        if row.household_id != new_household:
            row.household_id = new_household
            db.flush()
    principal = Principal(
        user=user,
        household_id=member.household_id if member else None,
        role=member.role if member else None,
        auth_method="session",
        scopes=None,
        session_id=row.id,
        mfa_verified_at=row.mfa_verified_at,
        login_method=row.login_method,
        mfa_enrolled=has_primary_mfa(db, user.id),
        mfa_required=require_mfa,
        session=row,
    )
    apply_rls_context(db, principal)
    return principal


# ---------------------------------------------------------------------------
# Personal access tokens
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenMaterial:
    """A freshly generated token. ``raw`` is shown to the user once."""

    prefix: str
    raw: str = field(repr=False)
    token_hash: str


def hash_api_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def generate_api_token() -> TokenMaterial:
    """``pfn_<12 hex prefix>_<43 base64url secret>`` and its hash."""
    prefix = secrets.token_hex(6)
    raw = f"{TOKEN_PREFIX}_{prefix}_{secrets.token_urlsafe(TOKEN_SECRET_BYTES)}"
    return TokenMaterial(prefix=prefix, raw=raw, token_hash=hash_api_token(raw))


def parse_api_token(raw: str) -> str | None:
    """The prefix of a well-formed token, else ``None``."""
    match = _TOKEN_RE.fullmatch(raw)
    return match.group(1) if match else None


_SET_PREFIX = text("SELECT set_config('perfin.token_prefix', :prefix, true)")


@contextmanager
def token_lookup_scope(db: Session, prefix: str) -> Iterator[None]:
    """Make the ``api_token`` row with ``prefix`` (only that one) readable."""
    db.execute(_SET_PREFIX, {"prefix": prefix})
    try:
        yield
    finally:
        db.execute(_SET_PREFIX, {"prefix": ""})


def authenticate_token(
    db: Session,
    raw: str,
    *,
    require_mfa: bool,
    now: datetime | None = None,
) -> Principal | None:
    """A principal for a bearer personal access token, else ``None``."""
    now = now or clock.utcnow()
    prefix = parse_api_token(raw)
    if prefix is None:
        return None
    set_context(db, None)
    with token_lookup_scope(db, prefix):
        token = db.execute(select(ApiToken).where(ApiToken.prefix == prefix)).scalar_one_or_none()
    if token is None:
        return None
    if not hmac.compare_digest(token.token_hash.encode(), hash_api_token(raw).encode()):
        return None
    if token.revoked_at is not None or token.expires_at <= now:
        return None
    user = db.get(User, token.user_id)
    if user is None or not user.is_active:
        return None
    if user.is_breakglass:
        # B7: a token carries no client-address check, so it would take a
        # break-glass account off the allowlisted network (review P1-SR-01).
        return None
    member = membership_in(db, user.id, token.household_id)
    if member is None:
        return None
    principal = Principal(
        user=user,
        household_id=token.household_id,
        role=member.role,
        auth_method="token",
        scopes=effective_scopes(token.scopes, member.role),
        token_id=token.id,
        mfa_enrolled=has_primary_mfa(db, user.id),
        mfa_required=require_mfa,
    )
    apply_rls_context(db, principal)
    # The row is now visible through household_isolation; record its use.
    if token.last_used_at is None or (now - token.last_used_at).total_seconds() >= (
        TOKEN_TOUCH_SECONDS
    ):
        token.last_used_at = now
        db.flush()
    return principal

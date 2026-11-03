# SPDX-License-Identifier: AGPL-3.0-or-later
"""Identity and access tables (DATA_MODEL.md "Identity and access").

Not household-scoped (no RLS; reachable only through ``app.auth`` service
functions): ``app_user``, ``credential``, ``mfa_method``, ``identity``,
``user_session``, ``password_reset_token`` and ``rate_limit_event``.
``user`` and ``session`` are SQL keywords (``SELECT user`` returns the
database role), so the tables are named ``app_user`` and ``user_session``.

Household-scoped (forced RLS, ``household_isolation``): ``household_member``,
``account_grant`` and ``api_token``. Each also gets an extra *SELECT-only*
policy (registered below in ``app.db.rls.EXTRA_POLICIES``) so a user can read
their own rows before any household context exists:

* ``user_id = perfin.user_id``: list a user's memberships, grants and tokens
  (the principal resolver needs the memberships to pick a household);
* ``api_token`` additionally ``prefix = perfin.token_prefix``: the bearer
  authenticator looks a token up by its public prefix before it knows the
  user or household (``app.auth.principal.token_lookup_scope``).

Writes still need the household context, so nobody can add themselves to
another household.

``app_user.is_instance_admin`` is only ever set from the local database
(bootstrap, instance-admin UI), never from an OIDC claim (SECURITY.md B6).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.rls import CTX_USER, EXTRA_POLICIES, Policy
from app.db.types import household_fk

USERNAME_MAX = 64
USER_AGENT_MAX = 512

# The bearer-token lookup context (see app.auth.principal.token_lookup_scope).
CTX_LOOKUP_PREFIX = "nullif(current_setting('perfin.token_prefix', true), '')"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_cls]


def _pg_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    return Enum(enum_cls, name=name, values_callable=_enum_values, validate_strings=True)


class HouseholdRole(enum.StrEnum):
    """Household roles, strongest first (``app.auth.authz.ROLE_RANK``)."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class GrantLevel(enum.StrEnum):
    NONE = "none"
    VIEW = "view"
    EDIT = "edit"


class MfaType(enum.StrEnum):
    WEBAUTHN = "webauthn"
    TOTP = "totp"
    RECOVERY = "recovery"


class IdentityProvider(enum.StrEnum):
    LOCAL = "local"
    OIDC = "oidc"


class SessionStage(enum.StrEnum):
    """``mfa_pending``: password accepted, second factor outstanding (only the
    MFA verification endpoints accept it). ``authenticated``: full session."""

    MFA_PENDING = "mfa_pending"
    AUTHENTICATED = "authenticated"


class LoginMethod(enum.StrEnum):
    LOCAL = "local"
    OIDC = "oidc"
    BREAKGLASS = "breakglass"
    BOOTSTRAP = "bootstrap"


def normalize_username(value: str) -> str:
    """Canonical username: stripped and lower-cased (case-insensitive unique)."""
    return value.strip().lower()


# ---------------------------------------------------------------------------
# Users and their authenticators
# ---------------------------------------------------------------------------


class User(UUIDPk, Timestamps, Base):
    """A person who can log in. Not household-scoped."""

    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint("username = lower(username)", name="username_lowercase"),
        CheckConstraint("length(username) BETWEEN 1 AND 64", name="username_length"),
        CheckConstraint("failed_login_count >= 0", name="failed_login_count_nonnegative"),
    )

    username: Mapped[str] = mapped_column(String(USERNAME_MAX), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Only ever set from the local database, never from a claim (B6).
    is_instance_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # Break-glass local admin, usable only from PERFIN_BREAKGLASS_CIDRS (B7).
    is_breakglass: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_failed_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Credential(UUIDPk, Timestamps, Base):
    """A local password (Argon2id PHC string plus its parameters)."""

    __tablename__ = "credential"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    hash_params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rotated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MfaMethod(UUIDPk, Timestamps, Base):
    """A second factor.

    * ``totp``: ``secret_encrypted`` is the RFC 6238 secret sealed with
      ``app.core.crypto`` (AD = ``column_ad("mfa_method", "secret_encrypted",
      id)``); ``totp_last_counter`` is the last accepted time step (A7).
    * ``webauthn``: ``credential_id`` (unique), ``public_key`` (COSE, not a
      secret), ``sign_count``, ``transports``.
    * ``recovery``: one row per code, ``code_hash`` (never the code),
      ``consumed_at`` once used (A8).
    """

    __tablename__ = "mfa_method"
    __table_args__ = (
        CheckConstraint("(type <> 'totp') OR secret_encrypted IS NOT NULL", name="totp_has_secret"),
        CheckConstraint(
            "(type <> 'webauthn') OR (credential_id IS NOT NULL AND public_key IS NOT NULL)",
            name="webauthn_has_key",
        ),
        CheckConstraint("(type <> 'recovery') OR code_hash IS NOT NULL", name="recovery_has_hash"),
        Index("ix_mfa_method_user_type", "user_id", "type"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[MfaType] = mapped_column(_pg_enum(MfaType, "mfa_type"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default="")
    secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    public_key: Mapped[bytes | None] = mapped_column(LargeBinary)
    credential_id: Mapped[bytes | None] = mapped_column(LargeBinary, unique=True)
    sign_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    transports: Mapped[list[str] | None] = mapped_column(ARRAY(String(32)))
    code_hash: Mapped[str | None] = mapped_column(Text)
    totp_last_counter: Mapped[int | None] = mapped_column(BigInteger)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Identity(UUIDPk, Timestamps, Base):
    """A login identity of a user, matched by ``(issuer, subject)`` only (B3).

    Local identities use ``issuer = 'local'`` and ``subject = str(user.id)``.
    """

    __tablename__ = "identity"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_identity_issuer_subject"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[IdentityProvider] = mapped_column(
        _pg_enum(IdentityProvider, "identity_provider"), nullable=False
    )
    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email_at_link: Mapped[str | None] = mapped_column(String(320))
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserSession(UUIDPk, Timestamps, Base):
    """A server-side browser session (ADR-0005).

    The cookie carries a random 256-bit id; only its SHA-256 (``token_hash``)
    is stored. ``household_id`` is the household the session currently acts
    in (validated against ``household_member`` on every request).
    """

    __tablename__ = "user_session"
    __table_args__ = (
        Index("ix_user_session_user_active", "user_id", "revoked_at"),
        Index("ix_user_session_absolute_expires", "absolute_expires_at"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    household_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("household.id", ondelete="SET NULL")
    )
    stage: Mapped[SessionStage] = mapped_column(
        _pg_enum(SessionStage, "session_stage"), nullable=False
    )
    login_method: Mapped[LoginMethod] = mapped_column(
        _pg_enum(LoginMethod, "login_method"), nullable=False
    )
    user_agent: Mapped[str | None] = mapped_column(String(USER_AGENT_MAX))
    ip: Mapped[str | None] = mapped_column(INET)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mfa_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remember_device: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(64))
    # The session this one replaced on rotation (audit trail of fixation defence).
    rotated_from_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))


class PasswordResetToken(UUIDPk, Timestamps, Base):
    """A pending password reset (``app.auth.reset``, SECURITY.md A11).

    Only the SHA-256 of the emailed token is stored. A token is valid for 15
    minutes and once (``used_at``); a new request invalidates the user's
    earlier unused tokens. Not household-scoped (no RLS), like
    ``user_session``.
    """

    __tablename__ = "password_reset_token"
    __table_args__ = (Index("ix_password_reset_token_user", "user_id", "used_at"),)

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip: Mapped[str | None] = mapped_column(INET)


# ---------------------------------------------------------------------------
# Household membership, grants and tokens (household-scoped, RLS)
# ---------------------------------------------------------------------------


class HouseholdMember(UUIDPk, Timestamps, HouseholdScoped, Base):
    """A user's role in a household."""

    __tablename__ = "household_member"
    __table_args__ = (
        UniqueConstraint("household_id", "user_id", name="uq_household_member_household_user"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[HouseholdRole] = mapped_column(
        _pg_enum(HouseholdRole, "household_role"), nullable=False
    )


class AccountGrant(UUIDPk, Timestamps, HouseholdScoped, Base):
    """Which accounts a member or viewer may see or edit.

    ``(household_id, account_id)`` references ``account(household_id, id)``
    (``ON DELETE CASCADE``; ``app.db.types.household_fk``), so a grant can
    only name an account of its own household and goes with the account.
    """

    __tablename__ = "account_grant"
    __table_args__ = (
        UniqueConstraint(
            "household_id", "user_id", "account_id", name="uq_account_grant_member_account"
        ),
        household_fk("account_grant", "account_id", "account"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    level: Mapped[GrantLevel] = mapped_column(_pg_enum(GrantLevel, "grant_level"), nullable=False)


class ApiToken(UUIDPk, Timestamps, HouseholdScoped, Base):
    """A personal access token: ``pfn_<prefix>_<secret>``.

    ``prefix`` is the public lookup id; ``token_hash`` is SHA-256 of the whole
    token. Tokens always expire and act only within ``household_id`` with at
    most the user's role there, narrowed by ``scopes``.
    """

    __tablename__ = "api_token"
    __table_args__ = (CheckConstraint("cardinality(scopes) <= 64", name="scopes_bounded"),)

    prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("app_user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default=text("'{}'")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Rate limiting (A1)
# ---------------------------------------------------------------------------


class RateLimitEvent(Base):
    """One counted attempt in a sliding window (``app.auth.ratelimit``).

    ``key_hash`` is SHA-256 of ``scope`` and the key (an IP or user id), so
    raw addresses and usernames are not kept here. Rows older than the longest
    window are pruned.
    """

    __tablename__ = "rate_limit_event"
    __table_args__ = (Index("ix_rate_limit_event_lookup", "scope", "key_hash", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Extra RLS policies (OR-ed with household_isolation; SELECT only)
# ---------------------------------------------------------------------------

_OWN_ROWS = f"user_id = {CTX_USER}"

EXTRA_POLICIES["household_member"] = [Policy("member_self_read", "SELECT", using=_OWN_ROWS)]
EXTRA_POLICIES["account_grant"] = [Policy("grant_self_read", "SELECT", using=_OWN_ROWS)]
EXTRA_POLICIES["api_token"] = [
    Policy("token_self_read", "SELECT", using=_OWN_ROWS),
    Policy("token_prefix_lookup", "SELECT", using=f"prefix = {CTX_LOOKUP_PREFIX}"),
]


# ``account_grant`` references ``account``: make sure that table is in the
# metadata wherever these models are used (flush ordering resolves every FK).
import app.domain.accounts.models  # noqa: E402, F401

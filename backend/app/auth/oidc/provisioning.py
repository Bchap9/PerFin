# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who an OIDC login is, and what it may do (AUTH.md "OIDC" steps 4-5; B3, B6).

:func:`resolve_user` takes a validated ID token:

1. **Lookup by ``(iss, sub)`` only** (``identity`` with provider ``oidc``).
   The email claim never selects an account (B3).
2. **Group mapping** (:func:`map_groups`): ``groups`` containing
   ``PERFIN_OIDC_GROUP_ADMIN`` / ``_MEMBER`` / ``_VIEWER`` maps to the
   household role admin / member / viewer (the strongest wins; ``owner`` is
   never granted from a claim). With ``PERFIN_OIDC_REQUIRE_GROUP`` a user in
   no mapped group is refused (``not-in-group``), linked or not.
   ``app_user.is_instance_admin`` is **never** read from or written by
   anything here: no claim, group or role name grants instance admin (B6).
3. No identity yet:

   * a local account with the same email exists: if
     ``PERFIN_OIDC_LINK_BY_VERIFIED_EMAIL`` is on, the IdP says
     ``email_verified: true``, and exactly one active, non-break-glass local
     account has that email marked verified, the result is
     :class:`LinkRequired`: nothing is logged in; the user must log in to that
     account locally (password, plus MFA if enrolled) and then confirm
     (``POST /auth/oidc/link``). Otherwise ``account-exists``: an IdP that lets
     users pick any email cannot take an account over, and no second account
     with the same email is created.
   * else with ``PERFIN_OIDC_JIT_PROVISIONING`` a new user is created
     (username from ``preferred_username``, made unique; no password, no
     instance admin), else ``no-account``.

4. Inactive and break-glass accounts never log in through OIDC.

:func:`sync_membership` applies the mapped role to the OIDC household
(``PERFIN_OIDC_HOUSEHOLD_ID``, or the instance's only household): a missing
membership is created with the mapped role, a different role is updated, an
``owner`` membership is left alone, and without a mapped role (only possible
with ``PERFIN_OIDC_REQUIRE_GROUP=false``) nothing changes. With several
households and no configured one, no membership is touched.
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.auth.models import (
    HouseholdMember,
    HouseholdRole,
    Identity,
    IdentityProvider,
    User,
)
from app.auth.oidc.tokens import ValidatedToken
from app.core import clock
from app.core.logging import get_logger
from app.core.settings import Settings
from app.db.rls import set_context

log = get_logger(__name__)

_ROLE_ORDER: Final = (HouseholdRole.ADMIN, HouseholdRole.MEMBER, HouseholdRole.VIEWER)
_USERNAME_CLEAN: Final = re.compile(r"[^a-z0-9._-]+")
USERNAME_BASE_MAX: Final = 48


class OidcRejectedError(Exception):
    """The login is refused. ``reason`` is a stable code shown to the SPA."""

    def __init__(self, reason: str, *, user_id: uuid.UUID | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.user_id = user_id


@dataclass(frozen=True)
class ResolvedUser:
    user: User
    role: HouseholdRole | None
    provisioned: bool


@dataclass(frozen=True)
class LinkRequired:
    """A local account may be linked after local re-authentication."""

    user_id: uuid.UUID
    email: str


@dataclass(frozen=True)
class MembershipChange:
    household_id: uuid.UUID
    before: HouseholdRole | None
    after: HouseholdRole


# ---------------------------------------------------------------------------
# Groups -> role (B6)
# ---------------------------------------------------------------------------


def map_groups(groups: list[str], settings: Settings) -> HouseholdRole | None:
    """The strongest household role the ``groups`` claim maps to, if any."""
    configured = {
        HouseholdRole.ADMIN: settings.oidc_group_admin.strip(),
        HouseholdRole.MEMBER: settings.oidc_group_member.strip(),
        HouseholdRole.VIEWER: settings.oidc_group_viewer.strip(),
    }
    present = set(groups)
    for role in _ROLE_ORDER:
        name = configured[role]
        if name and name in present:
            return role
    return None


# ---------------------------------------------------------------------------
# Household
# ---------------------------------------------------------------------------


def household_ids(db: Session) -> list[uuid.UUID]:
    """Every household id (``perfin_household_ids()``; ``household`` is under RLS)."""
    rows = db.execute(text("SELECT * FROM perfin_household_ids()")).scalars()
    return [r if isinstance(r, uuid.UUID) else uuid.UUID(str(r)) for r in rows]


def target_household(db: Session, settings: Settings) -> uuid.UUID | None:
    """The household group-mapped roles apply to (module doc), or ``None``."""
    ids = household_ids(db)
    if settings.oidc_household_id is not None:
        if settings.oidc_household_id in ids:
            return settings.oidc_household_id
        log.warning("PERFIN_OIDC_HOUSEHOLD_ID names no existing household")
        return None
    return ids[0] if len(ids) == 1 else None


def sync_membership(
    db: Session, user_id: uuid.UUID, role: HouseholdRole | None, settings: Settings
) -> MembershipChange | None:
    """Apply the mapped role in the OIDC household (module doc)."""
    if role is None:
        return None
    household_id = target_household(db, settings)
    if household_id is None:
        log.warning("OIDC group role not applied: no single OIDC household is configured")
        return None
    set_context(db, household_id, user_id)
    member = db.execute(
        select(HouseholdMember)
        .where(HouseholdMember.household_id == household_id, HouseholdMember.user_id == user_id)
        .with_for_update()
    ).scalar_one_or_none()
    if member is None:
        db.add(HouseholdMember(household_id=household_id, user_id=user_id, role=role))
        db.flush()
        return MembershipChange(household_id, None, role)
    if member.role is HouseholdRole.OWNER or member.role is role:
        return None
    before = member.role
    member.role = role
    member.updated_at = clock.utcnow()
    db.flush()
    return MembershipChange(household_id, before, role)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def find_identity(db: Session, issuer: str, subject: str) -> Identity | None:
    return db.execute(
        select(Identity).where(
            Identity.provider == IdentityProvider.OIDC,
            Identity.issuer == issuer,
            Identity.subject == subject,
        )
    ).scalar_one_or_none()


def _users_with_email(db: Session, email: str) -> list[User]:
    return list(
        db.execute(select(User).where(func.lower(User.email) == email.strip().lower())).scalars()
    )


def _username_base(token: ValidatedToken) -> str:
    candidate = token.string("preferred_username", 200) or ""
    if not candidate:
        email = token.string("email") or ""
        candidate = email.split("@", 1)[0]
    cleaned = _USERNAME_CLEAN.sub("-", candidate.lower()).strip("-._")
    return cleaned[:USERNAME_BASE_MAX].strip("-._") or "user"


def _unique_username(db: Session, base: str) -> str:
    name = base
    for _ in range(8):
        taken = db.execute(select(User.id).where(User.username == name)).first()
        if taken is None:
            return name
        name = f"{base}-{secrets.token_hex(3)}"
    raise OidcRejectedError("username-unavailable")


def provision_user(db: Session, token: ValidatedToken, *, now: datetime) -> User:
    """Create a user for an IdP identity (no password, never an instance admin)."""
    if token.subject is None:  # pragma: no cover - ID tokens always have sub
        raise OidcRejectedError("invalid-token")
    username = _unique_username(db, _username_base(token))
    email = token.string("email")
    display = token.string("name", 200) or token.string("preferred_username", 200) or username
    user = User(
        username=username,
        display_name=display,
        email=email,
        email_verified=bool(email) and token.email_verified,
        is_instance_admin=False,
        is_breakglass=False,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    db.flush()
    db.add(
        Identity(
            user_id=user.id,
            provider=IdentityProvider.OIDC,
            issuer=token.issuer,
            subject=token.subject,
            email_at_link=email,
            linked_at=now,
        )
    )
    db.flush()
    return user


def _check_usable(user: User) -> None:
    if not user.is_active:
        raise OidcRejectedError("account-disabled", user_id=user.id)
    if user.is_breakglass:
        raise OidcRejectedError("breakglass-account", user_id=user.id)


def resolve_user(
    db: Session, token: ValidatedToken, settings: Settings, *, now: datetime | None = None
) -> ResolvedUser | LinkRequired:
    """Steps 1-4 of the module doc. Raises :class:`OidcRejectedError`."""
    now = now or clock.utcnow()
    if token.subject is None:  # pragma: no cover - ID tokens always have sub
        raise OidcRejectedError("invalid-token")
    role = map_groups(token.groups, settings)
    identity = find_identity(db, token.issuer, token.subject)
    if identity is not None:
        user = db.get(User, identity.user_id)
        if user is None:  # pragma: no cover - FK
            raise OidcRejectedError("no-account")
        _check_usable(user)
        if role is None and settings.oidc_require_group:
            raise OidcRejectedError("not-in-group", user_id=user.id)
        return ResolvedUser(user=user, role=role, provisioned=False)

    if role is None and settings.oidc_require_group:
        raise OidcRejectedError("not-in-group")
    email = token.string("email")
    if email:
        existing = _users_with_email(db, email)
        if existing:
            linkable = [
                u for u in existing if u.email_verified and u.is_active and not u.is_breakglass
            ]
            if (
                settings.oidc_link_by_verified_email
                and token.email_verified
                and len(existing) == 1
                and len(linkable) == 1
            ):
                return LinkRequired(user_id=linkable[0].id, email=email)
            raise OidcRejectedError("account-exists")
    if not settings.oidc_jit_provisioning:
        raise OidcRejectedError("no-account")
    user = provision_user(db, token, now=now)
    return ResolvedUser(user=user, role=role, provisioned=True)


def link_identity(
    db: Session, *, user: User, issuer: str, subject: str, email: str, now: datetime | None = None
) -> Identity:
    """Attach ``(issuer, subject)`` to ``user`` after local re-authentication."""
    now = now or clock.utcnow()
    _check_usable(user)
    if find_identity(db, issuer, subject) is not None:
        raise OidcRejectedError("already-linked", user_id=user.id)
    identity = Identity(
        user_id=user.id,
        provider=IdentityProvider.OIDC,
        issuer=issuer,
        subject=subject,
        email_at_link=email,
        linked_at=now,
    )
    db.add(identity)
    db.flush()
    return identity

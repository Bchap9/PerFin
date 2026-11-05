# SPDX-License-Identifier: AGPL-3.0-or-later
"""Queries for households, members, grants, preferences and users.

Every function runs in the caller's RLS context and adds the household filter
explicitly as well (defence in depth, SECURITY.md F3): a missing context
returns nothing, and a context for another household returns nothing either.
Bound parameters only.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.auth.models import (
    AccountGrant,
    ApiToken,
    Credential,
    GrantLevel,
    HouseholdMember,
    HouseholdRole,
    MfaMethod,
    User,
    UserSession,
)
from app.auth.principal import PRIMARY_MFA_TYPES
from app.db.models import Household, Setting, SettingScope

# ---------------------------------------------------------------------------
# Households
# ---------------------------------------------------------------------------


def get_household(db: Session, household_id: uuid.UUID, *, lock: bool = False) -> Household | None:
    """The household row if the context lets us see it (``FOR UPDATE`` with ``lock``)."""
    stmt = select(Household).where(Household.id == household_id)
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def list_households(db: Session) -> list[Household]:
    """Every household visible in the context (all of them for an instance admin)."""
    return list(
        db.execute(select(Household).order_by(Household.created_at, Household.id)).scalars()
    )


def delete_household(db: Session, household_id: uuid.UUID) -> int:
    result = db.execute(delete(Household).where(Household.id == household_id))
    return int(result.rowcount)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


def list_members(db: Session, household_id: uuid.UUID) -> list[tuple[HouseholdMember, User]]:
    rows = db.execute(
        select(HouseholdMember, User)
        .join(User, User.id == HouseholdMember.user_id)
        .where(HouseholdMember.household_id == household_id)
        .order_by(HouseholdMember.created_at, HouseholdMember.id)
    ).all()
    return [(member, user) for member, user in rows]


def get_member(
    db: Session, household_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False
) -> HouseholdMember | None:
    stmt = select(HouseholdMember).where(
        HouseholdMember.household_id == household_id, HouseholdMember.user_id == user_id
    )
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def count_owners(db: Session, household_id: uuid.UUID) -> int:
    count = db.execute(
        select(func.count())
        .select_from(HouseholdMember)
        .where(
            HouseholdMember.household_id == household_id,
            HouseholdMember.role == HouseholdRole.OWNER,
        )
    ).scalar_one()
    return int(count)


def add_member(
    db: Session, household_id: uuid.UUID, user_id: uuid.UUID, role: HouseholdRole
) -> HouseholdMember:
    member = HouseholdMember(household_id=household_id, user_id=user_id, role=role)
    db.add(member)
    db.flush()
    return member


def delete_member(db: Session, household_id: uuid.UUID, user_id: uuid.UUID) -> int:
    result = db.execute(
        delete(HouseholdMember).where(
            HouseholdMember.household_id == household_id, HouseholdMember.user_id == user_id
        )
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Sessions and tokens of an affected member (privilege change, A3/I2)
# ---------------------------------------------------------------------------


def revoke_household_sessions(
    db: Session, user_id: uuid.UUID, household_id: uuid.UUID, *, reason: str, now: datetime
) -> int:
    """Revoke the user's live sessions acting in ``household_id`` (or in none yet).

    ``user_session`` is not household-scoped; the filter is explicit.
    """
    result = db.execute(
        update(UserSession)
        .where(
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
            or_(UserSession.household_id == household_id, UserSession.household_id.is_(None)),
        )
        .values(revoked_at=now, revoked_reason=reason[:64], updated_at=now)
        .execution_options(synchronize_session="fetch")
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


def revoke_household_tokens(
    db: Session, user_id: uuid.UUID, household_id: uuid.UUID, *, now: datetime
) -> int:
    """Revoke the user's personal access tokens for ``household_id`` (needs its context)."""
    result = db.execute(
        update(ApiToken)
        .where(
            ApiToken.household_id == household_id,
            ApiToken.user_id == user_id,
            ApiToken.revoked_at.is_(None),
        )
        .values(revoked_at=now, updated_at=now)
        .execution_options(synchronize_session="fetch")
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Account grants
# ---------------------------------------------------------------------------


def list_grants(
    db: Session, household_id: uuid.UUID, *, user_id: uuid.UUID | None = None
) -> list[AccountGrant]:
    stmt = select(AccountGrant).where(AccountGrant.household_id == household_id)
    if user_id is not None:
        stmt = stmt.where(AccountGrant.user_id == user_id)
    stmt = stmt.order_by(AccountGrant.user_id, AccountGrant.account_id)
    return list(db.execute(stmt).scalars())


def get_grant(
    db: Session, household_id: uuid.UUID, user_id: uuid.UUID, account_id: uuid.UUID
) -> AccountGrant | None:
    return db.execute(
        select(AccountGrant).where(
            AccountGrant.household_id == household_id,
            AccountGrant.user_id == user_id,
            AccountGrant.account_id == account_id,
        )
    ).scalar_one_or_none()


def upsert_grant(
    db: Session,
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    level: GrantLevel,
    *,
    now: datetime,
) -> AccountGrant:
    grant = get_grant(db, household_id, user_id, account_id)
    if grant is None:
        grant = AccountGrant(
            household_id=household_id, user_id=user_id, account_id=account_id, level=level
        )
        db.add(grant)
    else:
        grant.level = level
        grant.updated_at = now
    db.flush()
    return grant


def delete_grants(
    db: Session,
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    account_id: uuid.UUID | None = None,
) -> int:
    stmt = delete(AccountGrant).where(
        AccountGrant.household_id == household_id, AccountGrant.user_id == user_id
    )
    if account_id is not None:
        stmt = stmt.where(AccountGrant.account_id == account_id)
    result = db.execute(stmt)
    return int(result.rowcount)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def get_user(db: Session, user_id: uuid.UUID, *, lock: bool = False) -> User | None:
    stmt = select(User).where(User.id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.execute(select(User).where(User.username == username)).scalar_one_or_none()


def list_users(db: Session, *, after: uuid.UUID | None, limit: int) -> list[User]:
    stmt = select(User).order_by(User.id).limit(limit)
    if after is not None:
        stmt = stmt.where(User.id > after)
    return list(db.execute(stmt).scalars())


def users_with_password(db: Session, user_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    if not user_ids:
        return set()
    rows = db.execute(select(Credential.user_id).where(Credential.user_id.in_(user_ids))).scalars()
    return set(rows)


def mfa_types_by_user(db: Session, user_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    """Primary factor types (``totp``, ``webauthn``) per user, sorted."""
    if not user_ids:
        return {}
    rows = db.execute(
        select(MfaMethod.user_id, MfaMethod.type)
        .where(MfaMethod.user_id.in_(user_ids), MfaMethod.type.in_(PRIMARY_MFA_TYPES))
        .distinct()
    ).all()
    out: dict[uuid.UUID, list[str]] = {}
    for user_id, kind in rows:
        out.setdefault(user_id, []).append(kind.value)
    return {k: sorted(v) for k, v in out.items()}


def delete_mfa_methods(db: Session, user_id: uuid.UUID) -> int:
    """Every factor of the user: TOTP, WebAuthn and recovery codes."""
    result = db.execute(delete(MfaMethod).where(MfaMethod.user_id == user_id))
    return int(result.rowcount)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# User-scoped settings (``setting`` scope ``user``; RLS: perfin.user_id)
# ---------------------------------------------------------------------------


def get_user_setting(db: Session, user_id: uuid.UUID, key: str) -> Any:
    return db.execute(
        select(Setting.value).where(
            Setting.scope == SettingScope.USER, Setting.scope_id == user_id, Setting.key == key
        )
    ).scalar_one_or_none()


def put_user_setting(
    db: Session, user_id: uuid.UUID, key: str, value: Any, *, now: datetime
) -> None:
    """Insert or replace one user setting (race-free upsert)."""
    stmt = pg_insert(Setting).values(
        scope=SettingScope.USER,
        scope_id=user_id,
        key=key,
        value=value,
        created_at=now,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Setting.scope, Setting.scope_id, Setting.key],
        set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
    )
    db.execute(stmt)

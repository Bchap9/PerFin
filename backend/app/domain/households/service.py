# SPDX-License-Identifier: AGPL-3.0-or-later
"""Households, membership, household switching and account grants.

Authorisation model (AUTH.md "Authorisation model", SECURITY.md I2/F3):

* Household endpoints act on the caller's **active** household only (the
  session's household, or the token's). Any other id is ``404
  household-not-found``, whether it exists or not and whatever the caller's
  role elsewhere (cross-household access is never a ``403``). Users in
  several households switch first (:func:`switch_household`).
* Reading the household and its member list: any role. Changing the
  household's name or base currency: owner or admin. Deleting it: owner.
* Managing members (add, change role, remove): owner or admin, on a browser
  session with step-up MFA (``app.auth.authz.require_step_up``; enforced by
  the router). Then :func:`check_add`, :func:`check_change` and
  :func:`check_remove`:

  - only an owner may make someone an owner, or change or remove an owner
    (an admin may not demote or remove the owner);
  - nobody changes or removes their own membership here (no accidental
    self-lockout; another owner or admin does it);
  - a household always keeps at least one owner.

* Every membership change is audited (before/after role), emits a household
  event (``member.added``, ``member.role_changed``, ``member.removed``) for
  live clients and notifications, and revokes the affected user's sessions
  acting in the household (A3: privilege change). Removal also deletes the
  user's account grants and revokes their personal access tokens there.
  Member management for a household is serialised with a row lock on the
  household, so two owners cannot demote each other at the same time.
* Account grants (:func:`set_account_grant` and friends) name an account
  of the household (checked here and by the composite foreign key); what a
  grant lets a member see is ``app.domain.accounts.visibility``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.deps import unauthenticated
from app.auth.local import audit_auth
from app.auth.models import AccountGrant, GrantLevel, HouseholdMember, HouseholdRole, User
from app.auth.principal import Principal, apply_rls_context, membership_in, memberships
from app.auth.sessions import IssuedSession, SessionPolicy
from app.auth.sessions import switch_household as rotate_into_household
from app.core import clock
from app.core.audit import RequestMeta, audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.ids import uuid7
from app.core.settings import Settings
from app.db.models import Household
from app.db.rls import get_context, restore_context, set_context
from app.domain.accounts.repo import get_account
from app.domain.accounts.visibility import account_not_found
from app.domain.households import repo
from app.domain.households.schemas import HouseholdCreate, HouseholdUpdate, MemberAdd
from app.domain.households.users import create_local_user, user_not_found
from app.domain.transactions.categories import seed_default_categories

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

#: ``user_session.revoked_reason`` for sessions ended by a membership change.
PRIVILEGE_CHANGE_REASON = "privilege_change"


# ---------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------


def household_not_found() -> ProblemError:
    return ProblemError(404, "household-not-found", "Household not found")


def member_not_found() -> ProblemError:
    return ProblemError(404, "member-not-found", "Member not found")


def _owner_protected() -> ProblemError:
    return ProblemError(
        403,
        "owner-protected",
        "Owner protected",
        "Only an owner can change, remove or appoint an owner.",
    )


def _own_membership() -> ProblemError:
    return ProblemError(
        409,
        "own-membership",
        "Cannot change your own membership",
        "Ask another owner or admin to change or remove your membership.",
    )


def _last_owner() -> ProblemError:
    return ProblemError(
        409, "last-owner", "Last owner", "A household must keep at least one owner."
    )


# ---------------------------------------------------------------------------
# Role rules (pure; unit-tested over every combination)
# ---------------------------------------------------------------------------


def can_manage_members(actor: HouseholdRole | None) -> bool:
    return role_at_least(actor, HouseholdRole.ADMIN)


def check_add(actor: HouseholdRole | None, role: HouseholdRole) -> None:
    """May ``actor`` add a member with ``role``?"""
    if not can_manage_members(actor):
        raise forbidden()
    if role is HouseholdRole.OWNER and actor is not HouseholdRole.OWNER:
        raise _owner_protected()


def check_change(
    actor: HouseholdRole | None,
    current: HouseholdRole,
    new: HouseholdRole,
    *,
    is_self: bool,
    owners: int,
) -> None:
    """May ``actor`` change a member's role from ``current`` to ``new``?"""
    if not can_manage_members(actor):
        raise forbidden()
    if is_self:
        raise _own_membership()
    if HouseholdRole.OWNER in (current, new) and actor is not HouseholdRole.OWNER:
        raise _owner_protected()
    if current is HouseholdRole.OWNER and new is not HouseholdRole.OWNER and owners <= 1:
        raise _last_owner()


def check_remove(
    actor: HouseholdRole | None, current: HouseholdRole, *, is_self: bool, owners: int
) -> None:
    """May ``actor`` remove a member whose role is ``current``?"""
    if not can_manage_members(actor):
        raise forbidden()
    if is_self:
        raise _own_membership()
    if current is HouseholdRole.OWNER and actor is not HouseholdRole.OWNER:
        raise _owner_protected()
    if current is HouseholdRole.OWNER and owners <= 1:
        raise _last_owner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HouseholdView:
    household: Household
    #: The caller's role there (``None`` for an instance admin who is not a member).
    role: HouseholdRole | None


@dataclass(frozen=True)
class MemberView:
    member: HouseholdMember
    user: User


@contextmanager
def _context(
    db: Session,
    household_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    *,
    instance_admin: bool = False,
) -> Iterator[None]:
    """Run in another RLS context, then put the caller's back."""
    saved = get_context(db)
    set_context(db, household_id, user_id, instance_admin=instance_admin)
    try:
        yield
    finally:
        restore_context(db, saved)


def active_household_id(principal: Principal, household_id: uuid.UUID) -> uuid.UUID:
    """``household_id`` if it is the caller's active household, else ``404``."""
    if principal.household_id is None or principal.household_id != household_id:
        raise household_not_found()
    return household_id


def _require_role(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise household_not_found()
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return principal.household_id


def _record(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    *,
    action: str,
    target_type: str,
    target_id: uuid.UUID | None,
    before: object = None,
    after: object = None,
    event: str | None = None,
    payload: dict[str, object] | None = None,
    request: HTTPConnection | None = None,
) -> None:
    """Audit row and (optionally) event in the household's context."""
    audit(
        db,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    if event is not None:
        emit(db, household_id, event, payload or {})


# ---------------------------------------------------------------------------
# Households
# ---------------------------------------------------------------------------


def list_households(db: Session, principal: Principal) -> list[HouseholdView]:
    """The caller's households; every household for an instance admin (browser session).

    A personal access token sees only the household it is bound to.
    """
    saved = get_context(db)
    try:
        roles = {m.household_id: m.role for m in memberships(db, principal.user_id)}
        if principal.auth_method == "token":
            # A personal access token acts only in its own household (I3).
            roles = {k: v for k, v in roles.items() if k == principal.household_id}
        if principal.is_instance_admin and principal.auth_method == "session":
            set_context(db, None, principal.user_id, instance_admin=True)
            return [HouseholdView(h, roles.get(h.id)) for h in repo.list_households(db)]
        views: list[HouseholdView] = []
        for household_id, role in roles.items():
            set_context(db, household_id, principal.user_id)
            household = repo.get_household(db, household_id)
            if household is not None:
                views.append(HouseholdView(household, role))
        return views
    finally:
        restore_context(db, saved)


def create_household(
    db: Session, principal: Principal, data: HouseholdCreate, *, request: HTTPConnection | None
) -> HouseholdView:
    """A new household with one owner (the caller unless ``owner_user_id``). Instance admin."""
    owner_id = data.owner_user_id or principal.user_id
    owner = repo.get_user(db, owner_id)
    if owner is None:
        raise user_not_found()
    if not owner.is_active:
        raise ProblemError(409, "user-inactive", "User inactive", "The owner must be active.")
    household_id = uuid7()
    # A household is inserted in the context of its own new id.
    with _context(db, household_id, principal.user_id, instance_admin=True):
        household = Household(id=household_id, name=data.name, base_currency=data.base_currency)
        db.add(household)
        db.flush()
        repo.add_member(db, household_id, owner.id, HouseholdRole.OWNER)
        seeded = seed_default_categories(db, household_id)
        _record(
            db,
            principal,
            household_id,
            action="household.create",
            target_type="household",
            target_id=household_id,
            after={
                "name": household.name,
                "base_currency": household.base_currency,
                "owner_user_id": str(owner.id),
                "default_categories": seeded,
            },
            request=request,
        )
        if owner.id != principal.user_id:
            repo.revoke_household_sessions(
                db, owner.id, household_id, reason=PRIVILEGE_CHANGE_REASON, now=clock.utcnow()
            )
    return HouseholdView(household, HouseholdRole.OWNER if owner.id == principal.user_id else None)


def get_household(db: Session, principal: Principal, household_id: uuid.UUID) -> HouseholdView:
    active_household_id(principal, household_id)
    household = repo.get_household(db, household_id)
    if household is None:
        raise household_not_found()
    return HouseholdView(household, principal.role)


def update_household(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    data: HouseholdUpdate,
    *,
    request: HTTPConnection | None,
) -> HouseholdView:
    """Rename or change the base currency (owner or admin)."""
    active_household_id(principal, household_id)
    _require_role(principal, HouseholdRole.ADMIN)
    household = repo.get_household(db, household_id, lock=True)
    if household is None:
        raise household_not_found()
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    if data.name is not None and data.name != household.name:
        before["name"], after["name"] = household.name, data.name
        household.name = data.name
    if data.base_currency is not None and data.base_currency != household.base_currency:
        before["base_currency"], after["base_currency"] = (
            household.base_currency,
            data.base_currency,
        )
        household.base_currency = data.base_currency
    if after:
        household.updated_at = clock.utcnow()
        db.flush()
        _record(
            db,
            principal,
            household_id,
            action="household.update",
            target_type="household",
            target_id=household_id,
            before=before,
            after=after,
            event="household.updated",
            payload={"fields": sorted(after)},
            request=request,
        )
    return HouseholdView(household, principal.role)


def delete_household(
    db: Session, principal: Principal, household_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    """Delete the household and everything in it (owner; the router adds step-up).

    The audit row is instance-level: the household's own rows stay in the
    chain but are only readable in a context that can no longer exist.
    """
    active_household_id(principal, household_id)
    _require_role(principal, HouseholdRole.OWNER)
    household = repo.get_household(db, household_id, lock=True)
    if household is None:
        raise household_not_found()
    audit_auth(
        db,
        action="household.delete",
        user_id=principal.user_id,
        meta=RequestMeta.from_request(request) if request is not None else RequestMeta(),
        after={"name": household.name, "household_id": str(household_id)},
        target_type="household",
        target_id=household_id,
    )
    if repo.delete_household(db, household_id) != 1:
        raise household_not_found()


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


def list_members(db: Session, principal: Principal, household_id: uuid.UUID) -> list[MemberView]:
    active_household_id(principal, household_id)
    return [MemberView(m, u) for m, u in repo.list_members(db, household_id)]


def _lock_household(db: Session, household_id: uuid.UUID) -> Household:
    household = repo.get_household(db, household_id, lock=True)
    if household is None:
        raise household_not_found()
    return household


def _resolve_new_member(
    db: Session,
    settings: Settings,
    principal: Principal,
    data: MemberAdd,
    request: HTTPConnection | None,
) -> tuple[User, bool]:
    if data.new_user is not None:
        user = create_local_user(
            db, settings, data.new_user, actor_id=principal.user_id, request=request
        )
        return user, True
    if data.user_id is not None:
        found = repo.get_user(db, data.user_id)
    else:
        found = repo.get_user_by_username(db, data.username or "")
    if found is None:
        raise user_not_found()
    return found, False


def add_member(
    db: Session,
    settings: Settings,
    principal: Principal,
    household_id: uuid.UUID,
    data: MemberAdd,
    *,
    request: HTTPConnection | None,
) -> MemberView:
    """Add an existing user, or create a local user and add them."""
    active_household_id(principal, household_id)
    check_add(principal.role, data.role)
    _lock_household(db, household_id)
    user, created = _resolve_new_member(db, settings, principal, data, request)
    if not user.is_active:
        raise ProblemError(409, "user-inactive", "User inactive", "Inactive users cannot be added.")
    if repo.get_member(db, household_id, user.id) is not None:
        raise ProblemError(409, "already-member", "Already a member")
    member = repo.add_member(db, household_id, user.id, data.role)
    repo.revoke_household_sessions(
        db, user.id, household_id, reason=PRIVILEGE_CHANGE_REASON, now=clock.utcnow()
    )
    _record(
        db,
        principal,
        household_id,
        action="member.add",
        target_type="household_member",
        target_id=member.id,
        after={"user_id": str(user.id), "role": data.role.value, "created_user": created},
        event="member.added",
        payload={"user_id": str(user.id), "role": data.role.value, "by": str(principal.user_id)},
        request=request,
    )
    return MemberView(member, user)


def change_member_role(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    role: HouseholdRole,
    *,
    request: HTTPConnection | None,
) -> MemberView:
    """Change a member's role (owner/admin + step-up; see the module doc)."""
    active_household_id(principal, household_id)
    if not can_manage_members(principal.role):
        raise forbidden()
    _lock_household(db, household_id)
    member = repo.get_member(db, household_id, user_id, lock=True)
    user = repo.get_user(db, user_id)
    if member is None or user is None:
        raise member_not_found()
    check_change(
        principal.role,
        member.role,
        role,
        is_self=user_id == principal.user_id,
        owners=repo.count_owners(db, household_id),
    )
    if member.role is role:
        return MemberView(member, user)
    previous = member.role
    now = clock.utcnow()
    member.role = role
    member.updated_at = now
    db.flush()
    repo.revoke_household_sessions(
        db, user_id, household_id, reason=PRIVILEGE_CHANGE_REASON, now=now
    )
    _record(
        db,
        principal,
        household_id,
        action="member.role_change",
        target_type="household_member",
        target_id=member.id,
        before={"user_id": str(user_id), "role": previous.value},
        after={"user_id": str(user_id), "role": role.value},
        event="member.role_changed",
        payload={
            "user_id": str(user_id),
            "role": role.value,
            "previous_role": previous.value,
            "by": str(principal.user_id),
        },
        request=request,
    )
    return MemberView(member, user)


def remove_member(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    request: HTTPConnection | None,
) -> None:
    """Remove a member, their grants, tokens and sessions in this household."""
    active_household_id(principal, household_id)
    if not can_manage_members(principal.role):
        raise forbidden()
    _lock_household(db, household_id)
    member = repo.get_member(db, household_id, user_id, lock=True)
    if member is None:
        raise member_not_found()
    check_remove(
        principal.role,
        member.role,
        is_self=user_id == principal.user_id,
        owners=repo.count_owners(db, household_id),
    )
    now = clock.utcnow()
    member_id, previous = member.id, member.role
    grants = repo.delete_grants(db, household_id, user_id)
    tokens = repo.revoke_household_tokens(db, user_id, household_id, now=now)
    repo.delete_member(db, household_id, user_id)
    repo.revoke_household_sessions(
        db, user_id, household_id, reason=PRIVILEGE_CHANGE_REASON, now=now
    )
    _record(
        db,
        principal,
        household_id,
        action="member.remove",
        target_type="household_member",
        target_id=member_id,
        before={"user_id": str(user_id), "role": previous.value},
        after={"grants_deleted": grants, "pats_revoked": tokens},
        event="member.removed",
        payload={
            "user_id": str(user_id),
            "previous_role": previous.value,
            "by": str(principal.user_id),
        },
        request=request,
    )


# ---------------------------------------------------------------------------
# Household switching
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SwitchResult:
    issued: IssuedSession
    household: Household
    role: HouseholdRole


def switch_household(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    *,
    policy: SessionPolicy,
    request: HTTPConnection | None,
) -> SwitchResult:
    """Make ``household_id`` the session's household; rotates the session (A3).

    ``404 household-not-found`` unless the caller is a member there.
    """
    current = principal.session
    if principal.auth_method != "session" or current is None:
        raise ProblemError(
            403, "session-required", "Browser session required", "Use the web app for this."
        )
    member = membership_in(db, principal.user_id, household_id)
    if member is None:
        apply_rls_context(db, principal)
        raise household_not_found()
    db.refresh(current, with_for_update=True)
    if current.revoked_at is not None:
        raise unauthenticated()
    previous = current.household_id
    issued = rotate_into_household(db, current, household_id, policy=policy)
    set_context(db, household_id, principal.user_id, instance_admin=principal.is_instance_admin)
    household = repo.get_household(db, household_id)
    if household is None:
        raise household_not_found()
    audit(
        db,
        action="session.switch_household",
        target_type="user_session",
        target_id=issued.row.id,
        before={"household_id": None if previous is None else str(previous)},
        after={"household_id": str(household_id)},
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    return SwitchResult(issued, household, member.role)


# ---------------------------------------------------------------------------
# Account grants (endpoints arrive with accounts in Phase 2)
# ---------------------------------------------------------------------------


def _grantable_member(db: Session, household_id: uuid.UUID, user_id: uuid.UUID) -> HouseholdMember:
    member = repo.get_member(db, household_id, user_id)
    if member is None:
        raise member_not_found()
    if role_at_least(member.role, HouseholdRole.ADMIN):
        raise ProblemError(
            422,
            "grant-not-applicable",
            "Grant not applicable",
            "Owners and admins can use every account of the household.",
        )
    return member


def list_account_grants(
    db: Session, principal: Principal, *, user_id: uuid.UUID | None = None
) -> list[AccountGrant]:
    """Grants in the active household: all of them for owner/admin, else the caller's own."""
    household_id = _require_role(principal, HouseholdRole.VIEWER)
    if not role_at_least(principal.role, HouseholdRole.ADMIN):
        if user_id is not None and user_id != principal.user_id:
            raise forbidden()
        user_id = principal.user_id
    return repo.list_grants(db, household_id, user_id=user_id)


def set_account_grant(
    db: Session,
    principal: Principal,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    level: GrantLevel,
    *,
    request: HTTPConnection | None = None,
) -> AccountGrant:
    """Create or change a member's or viewer's grant on an account (owner/admin).

    A viewer can be granted ``view`` at most. ``account_id`` must be an
    account of this household (``404 account-not-found`` otherwise; the
    composite foreign key on ``account_grant`` enforces it in the database).
    """
    household_id = _require_role(principal, HouseholdRole.ADMIN)
    member = _grantable_member(db, household_id, user_id)
    if member.role is HouseholdRole.VIEWER and level is GrantLevel.EDIT:
        raise ProblemError(
            422, "grant-level-not-allowed", "Grant level not allowed", "Viewers are read-only."
        )
    if get_account(db, household_id, account_id) is None:
        raise account_not_found()
    existing = repo.get_grant(db, household_id, user_id, account_id)
    previous = None if existing is None else existing.level.value
    if previous == level.value and existing is not None:
        return existing
    grant = repo.upsert_grant(db, household_id, user_id, account_id, level, now=clock.utcnow())
    _record(
        db,
        principal,
        household_id,
        action="account_grant.set",
        target_type="account_grant",
        target_id=grant.id,
        before=None if previous is None else {"level": previous},
        after={"user_id": str(user_id), "account_id": str(account_id), "level": level.value},
        request=request,
    )
    return grant


def revoke_account_grant(
    db: Session,
    principal: Principal,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    request: HTTPConnection | None = None,
) -> bool:
    """Delete a grant (owner/admin); ``False`` if there was none."""
    household_id = _require_role(principal, HouseholdRole.ADMIN)
    existing = repo.get_grant(db, household_id, user_id, account_id)
    if existing is None:
        return False
    grant_id, previous = existing.id, existing.level.value
    repo.delete_grants(db, household_id, user_id, account_id=account_id)
    _record(
        db,
        principal,
        household_id,
        action="account_grant.revoke",
        target_type="account_grant",
        target_id=grant_id,
        before={"user_id": str(user_id), "account_id": str(account_id), "level": previous},
        request=request,
    )
    return True

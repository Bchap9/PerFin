# SPDX-License-Identifier: AGPL-3.0-or-later
"""Account grants service.

Runs the service functions in the rolled-back ``db`` fixture with a real RLS
context, then checks ``app.auth.authz.account_access`` sees what was granted.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.authz import account_access
from app.auth.models import GrantLevel, HouseholdRole
from app.auth.principal import Principal, apply_rls_context
from app.core.errors import ProblemError
from app.db.models import AuditLog
from app.domain.households.service import (
    list_account_grants,
    revoke_account_grant,
    set_account_grant,
)
from tests.fixtures.factories import UserRecord, add_member, make_household, make_user
from tests.fixtures.ledger import make_account

pytestmark = pytest.mark.integration


def _principal(db: Session, record: UserRecord) -> Principal:
    principal = Principal(
        user=record.user,
        household_id=record.household_id,
        role=record.role,
        auth_method="session",
    )
    apply_rls_context(db, principal)
    return principal


def _slug(exc: pytest.ExceptionInfo[ProblemError]) -> tuple[int, str]:
    return exc.value.status, exc.value.slug


def test_owner_grants_and_revokes(db: Session) -> None:
    owner = make_user(db, role="owner")
    member = make_user(db, role="member", household=owner.household)
    assert owner.household_id is not None
    account = make_account(db, owner.household_id).id
    p = _principal(db, owner)
    grant = set_account_grant(db, p, member.id, account, GrantLevel.VIEW)
    assert (grant.user_id, grant.account_id, grant.level) == (member.id, account, GrantLevel.VIEW)
    as_member = _principal(db, member)
    assert account_access(db, as_member, account, "view")
    assert not account_access(db, as_member, account, "edit")

    p = _principal(db, owner)
    upgraded = set_account_grant(db, p, member.id, account, GrantLevel.EDIT)
    assert upgraded.id == grant.id
    assert upgraded.level is GrantLevel.EDIT
    assert [g.level for g in list_account_grants(db, p)] == [GrantLevel.EDIT]
    assert account_access(db, _principal(db, member), account, "edit")

    p = _principal(db, owner)
    assert revoke_account_grant(db, p, member.id, account) is True
    assert revoke_account_grant(db, p, member.id, account) is False
    assert not account_access(db, _principal(db, member), account, "view")

    _principal(db, owner)
    actions = db.execute(
        select(AuditLog.action, AuditLog.after)
        .where(AuditLog.household_id == owner.household_id, AuditLog.action.like("account_grant.%"))
        .order_by(AuditLog.seq)
    ).all()
    assert [a for a, _ in actions] == [
        "account_grant.set",
        "account_grant.set",
        "account_grant.revoke",
    ]
    assert actions[0][1]["level"] == "view"


def test_setting_the_same_level_again_is_not_audited(db: Session) -> None:
    admin = make_user(db, role="admin")
    member = make_user(db, role="member", household=admin.household)
    assert admin.household_id is not None
    account = make_account(db, admin.household_id).id
    p = _principal(db, admin)
    set_account_grant(db, p, member.id, account, GrantLevel.VIEW)
    set_account_grant(db, p, member.id, account, GrantLevel.VIEW)
    count = db.execute(
        select(AuditLog.id).where(
            AuditLog.household_id == admin.household_id, AuditLog.action == "account_grant.set"
        )
    ).all()
    assert len(count) == 1


def test_viewers_get_view_at_most(db: Session) -> None:
    owner = make_user(db, role="owner")
    viewer = make_user(db, role="viewer", household=owner.household)
    p = _principal(db, owner)
    with pytest.raises(ProblemError) as caught:
        set_account_grant(db, p, viewer.id, uuid.uuid4(), GrantLevel.EDIT)
    assert _slug(caught) == (422, "grant-level-not-allowed")
    assert owner.household_id is not None
    account = make_account(db, owner.household_id).id
    set_account_grant(db, _principal(db, owner), viewer.id, account, GrantLevel.VIEW)


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owners_and_admins_need_no_grants(db: Session, role: str) -> None:
    owner = make_user(db, role="owner")
    target = make_user(db, role=role, household=owner.household)
    with pytest.raises(ProblemError) as caught:
        set_account_grant(db, _principal(db, owner), target.id, uuid.uuid4(), GrantLevel.VIEW)
    assert _slug(caught) == (422, "grant-not-applicable")


@pytest.mark.parametrize("role", ["member", "viewer"])
def test_members_and_viewers_cannot_grant(db: Session, role: str) -> None:
    actor = make_user(db, role=role)
    other = make_user(db, role="member", household=actor.household)
    p = _principal(db, actor)
    with pytest.raises(ProblemError) as caught:
        set_account_grant(db, p, other.id, uuid.uuid4(), GrantLevel.VIEW)
    assert _slug(caught) == (403, "forbidden")
    with pytest.raises(ProblemError):
        revoke_account_grant(db, p, other.id, uuid.uuid4())


def test_members_list_only_their_own_grants(db: Session) -> None:
    owner = make_user(db, role="owner")
    alice = make_user(db, role="member", household=owner.household)
    bob = make_user(db, role="member", household=owner.household)
    assert owner.household_id is not None
    first, second = (make_account(db, owner.household_id).id for _ in range(2))
    p = _principal(db, owner)
    set_account_grant(db, p, alice.id, first, GrantLevel.VIEW)
    set_account_grant(db, p, bob.id, second, GrantLevel.EDIT)
    assert len(list_account_grants(db, _principal(db, owner))) == 2
    as_alice = _principal(db, alice)
    assert [g.user_id for g in list_account_grants(db, as_alice)] == [alice.id]
    with pytest.raises(ProblemError) as caught:
        list_account_grants(db, as_alice, user_id=bob.id)
    assert _slug(caught) == (403, "forbidden")


def test_users_of_other_households_cannot_be_granted(db: Session) -> None:
    owner = make_user(db, role="owner")
    stranger = make_user(db, role="member")
    with pytest.raises(ProblemError) as caught:
        set_account_grant(db, _principal(db, owner), stranger.id, uuid.uuid4(), GrantLevel.VIEW)
    assert _slug(caught) == (404, "member-not-found")


def test_grants_stay_in_their_household(db: Session) -> None:
    owner = make_user(db, role="owner")
    member = make_user(db, role="member", household=owner.household)
    other_household = make_household(db)
    other_owner = make_user(db, role="owner", household=other_household)
    assert owner.household_id is not None
    account = make_account(db, owner.household_id).id
    set_account_grant(db, _principal(db, owner), member.id, account, GrantLevel.EDIT)
    assert list_account_grants(db, _principal(db, other_owner)) == []
    # The member's grant does not count when acting in another household.
    add_member(db, other_household.id, member.id, HouseholdRole.MEMBER)
    elsewhere = Principal(
        user=member.user,
        household_id=other_household.id,
        role=HouseholdRole.MEMBER,
        auth_method="session",
    )
    apply_rls_context(db, elsewhere)
    assert not account_access(db, elsewhere, account, "view")


def test_grant_needs_an_account_of_the_household(db: Session) -> None:
    """A missing account and another household's account are both 404 (F3)."""
    owner = make_user(db, role="owner")
    member = make_user(db, role="member", household=owner.household)
    other = make_household(db)
    foreign = make_account(db, other.id).id
    for account_id in (uuid.uuid4(), foreign):
        with pytest.raises(ProblemError) as caught:
            set_account_grant(db, _principal(db, owner), member.id, account_id, GrantLevel.VIEW)
        assert _slug(caught) == (404, "account-not-found")
    assert list_account_grants(db, _principal(db, owner)) == []

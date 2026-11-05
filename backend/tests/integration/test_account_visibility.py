# SPDX-License-Identifier: AGPL-3.0-or-later
"""Account-level visibility (``app.domain.accounts.visibility``; SECURITY.md F3, I3).

Owners and admins see every account; members see the accounts granted
``view``/``edit`` (and edit only with ``edit``); viewers see granted accounts
read-only; ``none`` or no grant hides an account. A hidden account is
indistinguishable from another household's or a missing one: ``404
account-not-found``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.models import GrantLevel, HouseholdRole
from app.auth.principal import AuthMethod, Principal, apply_rls_context
from app.core.errors import ProblemError
from app.domain.accounts.models import Account
from app.domain.accounts.visibility import (
    Need,
    can_edit_account,
    can_view_account,
    get_visible_account,
    require_visible_accounts,
    sees_all_accounts,
    visible_account_filter,
    visible_account_ids,
)
from app.domain.transactions.models import Transaction
from tests.fixtures.factories import (
    UserRecord,
    add_member,
    grant_account,
    make_household,
    make_user,
)
from tests.fixtures.ledger import make_account, make_transaction

pytestmark = pytest.mark.integration


@dataclass
class World:
    owner: UserRecord
    accounts: dict[str, Account]
    foreign: Account


def _principal(
    db: Session, record: UserRecord, *, auth_method: AuthMethod = "session"
) -> Principal:
    principal = Principal(
        user=record.user,
        household_id=record.household_id,
        role=record.role,
        auth_method=auth_method,
    )
    apply_rls_context(db, principal)
    return principal


def _world(db: Session) -> World:
    owner = make_user(db, role="owner")
    assert owner.household_id is not None
    accounts = {
        name: make_account(db, owner.household_id, name=f"Synthetic {name}")
        for name in ("viewed", "edited", "none", "hidden")
    }
    for name, account in accounts.items():
        make_transaction(db, account, amount="-1.00", raw_payee=f"Synthetic {name}")
    other = make_household(db)
    foreign = make_account(db, other.id)
    make_transaction(db, foreign)
    return World(owner, accounts, foreign)


def _granted(db: Session, world: World, role: str) -> UserRecord:
    record = make_user(db, role=role, household=world.owner.household)
    household_id = world.owner.household_id
    assert household_id is not None
    grant_account(db, household_id, record.id, world.accounts["viewed"].id, GrantLevel.VIEW)
    # A viewer's edit grant cannot be made through the service; a stray row
    # (written directly) still never lets a viewer edit.
    grant_account(db, household_id, record.id, world.accounts["edited"].id, GrantLevel.EDIT)
    grant_account(db, household_id, record.id, world.accounts["none"].id, GrantLevel.NONE)
    return record


def _ids(world: World, *names: str) -> list[uuid.UUID]:
    return sorted(world.accounts[n].id for n in names)


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owners_and_admins_see_everything(db: Session, role: str) -> None:
    world = _world(db)
    record = world.owner if role == "owner" else _granted(db, world, "admin")
    p = _principal(db, record)
    assert sees_all_accounts(p)
    everything = _ids(world, "viewed", "edited", "none", "hidden")
    assert visible_account_ids(db, p) == everything
    assert visible_account_ids(db, p, need="edit") == everything
    assert all(can_edit_account(db, p, a.id) for a in world.accounts.values())
    assert not can_view_account(db, p, world.foreign.id)


@pytest.mark.parametrize("auth_method", ["session", "token"])
def test_members_see_granted_accounts(db: Session, auth_method: AuthMethod) -> None:
    world = _world(db)
    member = _granted(db, world, "member")
    p = _principal(db, member, auth_method=auth_method)
    assert not sees_all_accounts(p)
    assert visible_account_ids(db, p) == _ids(world, "viewed", "edited")
    assert visible_account_ids(db, p, need="edit") == _ids(world, "edited")
    assert can_view_account(db, p, world.accounts["viewed"].id)
    assert not can_edit_account(db, p, world.accounts["viewed"].id)
    assert can_edit_account(db, p, world.accounts["edited"].id)
    for hidden in ("none", "hidden"):
        assert not can_view_account(db, p, world.accounts[hidden].id)


def test_viewers_never_edit(db: Session) -> None:
    world = _world(db)
    viewer = _granted(db, world, "viewer")
    p = _principal(db, viewer)
    assert visible_account_ids(db, p) == _ids(world, "viewed", "edited")
    assert visible_account_ids(db, p, need="edit") == []
    assert not can_edit_account(db, p, world.accounts["edited"].id)
    with pytest.raises(ProblemError) as caught:
        get_visible_account(db, p, world.accounts["edited"].id, need="edit")
    assert (caught.value.status, caught.value.slug) == (404, "account-not-found")


def test_hidden_foreign_and_missing_accounts_look_the_same(db: Session) -> None:
    world = _world(db)
    member = _granted(db, world, "member")
    p = _principal(db, member)
    assert get_visible_account(db, p, world.accounts["viewed"].id).id == (
        world.accounts["viewed"].id
    )
    for account_id in (world.accounts["hidden"].id, world.foreign.id, uuid.uuid4()):
        with pytest.raises(ProblemError) as caught:
            get_visible_account(db, p, account_id)
        assert (caught.value.status, caught.value.slug) == (404, "account-not-found")
    # Even an owner cannot reach another household's account.
    owner = _principal(db, world.owner)
    with pytest.raises(ProblemError):
        get_visible_account(db, owner, world.foreign.id)


def test_filter_restricts_derived_rows(db: Session) -> None:
    world = _world(db)
    member = _granted(db, world, "member")

    def payees(p: Principal) -> list[str]:
        return sorted(
            payee or ""
            for payee in db.execute(
                select(Transaction.raw_payee).where(
                    Transaction.household_id == p.household_id,
                    visible_account_filter(p, Transaction.account_id),
                )
            ).scalars()
        )

    assert payees(_principal(db, member)) == ["Synthetic edited", "Synthetic viewed"]
    assert payees(_principal(db, world.owner)) == [
        "Synthetic edited",
        "Synthetic hidden",
        "Synthetic none",
        "Synthetic viewed",
    ]


def test_require_visible_accounts_is_all_or_nothing(db: Session) -> None:
    world = _world(db)
    member = _granted(db, world, "member")
    p = _principal(db, member)
    require_visible_accounts(db, p, _ids(world, "viewed", "edited"))
    require_visible_accounts(db, p, [])
    cases: list[tuple[list[uuid.UUID], Need]] = [
        (_ids(world, "viewed", "hidden"), "view"),
        ([world.foreign.id], "view"),
        (_ids(world, "viewed"), "edit"),
    ]
    for ids, need in cases:
        with pytest.raises(ProblemError) as caught:
            require_visible_accounts(db, p, ids, need=need)
        assert caught.value.slug == "account-not-found"


def test_grants_elsewhere_and_no_household_give_nothing(db: Session) -> None:
    world = _world(db)
    member = _granted(db, world, "member")
    # Acting with no household (no membership selected) sees nothing.
    nobody = Principal(user=member.user, household_id=None, role=None, auth_method="session")
    apply_rls_context(db, nobody)
    assert visible_account_ids(db, nobody) == []
    assert not can_view_account(db, nobody, world.accounts["viewed"].id)
    # The same user as a member of another household: the first household's
    # grants do not apply there.
    other = make_household(db)
    add_member(db, other.id, member.id, HouseholdRole.MEMBER)
    elsewhere = Principal(
        user=member.user, household_id=other.id, role=HouseholdRole.MEMBER, auth_method="session"
    )
    apply_rls_context(db, elsewhere)
    assert visible_account_ids(db, elsewhere) == []
    assert not can_view_account(db, elsewhere, world.accounts["viewed"].id)

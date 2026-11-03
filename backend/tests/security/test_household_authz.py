# SPDX-License-Identifier: AGPL-3.0-or-later
"""Household authorisation: role matrix, owner protection, cross-household 404, F4.

* Every role (owner, admin, member, viewer) tries every member operation
  (add with each role, change each kind of member, remove each kind of
  member, change/remove themselves) against an expected table written out
  here independently of the service code (SECURITY.md I2).
* Cross-household access is ``404`` whatever the caller's role, for every
  household and member endpoint (F3).
* Unknown or protected fields in any input are ``422`` and change nothing
  (F4).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.models import HouseholdMember, HouseholdRole
from app.core.settings import Settings
from app.db.models import Household
from app.db.rls import set_context
from tests.fixtures.factories import AuthedClient, Factories, UserRecord
from tests.fixtures.households import API, Actors, problem_type

pytestmark = [pytest.mark.integration, pytest.mark.security]

ROLES = ("owner", "admin", "member", "viewer")


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


@dataclass
class Household4:
    """A household with an actor of the given role and one member of each role."""

    actor: AuthedClient
    household_id: uuid.UUID
    others: dict[str, UserRecord]
    outsider: UserRecord


def _setup(actors: Actors, role: str) -> Household4:
    actor = actors.new(role=role)
    household = actor.record.household
    assert household is not None
    others = {r: actors.user(role=r, household=household) for r in ROLES}
    return Household4(actor, household.id, others, actors.user(role=None))


def _roles(factories: Factories, household_id: uuid.UUID) -> dict[uuid.UUID, str]:
    with factories.transaction() as db:
        set_context(db, household_id)
        rows = db.execute(
            select(HouseholdMember.user_id, HouseholdMember.role).where(
                HouseholdMember.household_id == household_id
            )
        ).all()
        return {user_id: role.value for user_id, role in rows}


# ---- role x operation matrix ------------------------------------------------------

OK_ADD, OK_CHANGE, OK_REMOVE = 201, 200, 204
F = (403, "forbidden")
OWNER_ONLY = (403, "owner-protected")
SELF = (409, "own-membership")

# operation -> {actor role -> expected (status) or (status, problem slug)}
MATRIX: dict[str, dict[str, Any]] = {
    "add-viewer": {"owner": OK_ADD, "admin": OK_ADD, "member": F, "viewer": F},
    "add-member": {"owner": OK_ADD, "admin": OK_ADD, "member": F, "viewer": F},
    "add-admin": {"owner": OK_ADD, "admin": OK_ADD, "member": F, "viewer": F},
    "add-owner": {"owner": OK_ADD, "admin": OWNER_ONLY, "member": F, "viewer": F},
    "viewer-to-member": {"owner": OK_CHANGE, "admin": OK_CHANGE, "member": F, "viewer": F},
    "member-to-viewer": {"owner": OK_CHANGE, "admin": OK_CHANGE, "member": F, "viewer": F},
    "member-to-admin": {"owner": OK_CHANGE, "admin": OK_CHANGE, "member": F, "viewer": F},
    "admin-to-member": {"owner": OK_CHANGE, "admin": OK_CHANGE, "member": F, "viewer": F},
    "member-to-owner": {"owner": OK_CHANGE, "admin": OWNER_ONLY, "member": F, "viewer": F},
    "owner-to-admin": {"owner": OK_CHANGE, "admin": OWNER_ONLY, "member": F, "viewer": F},
    "owner-to-viewer": {"owner": OK_CHANGE, "admin": OWNER_ONLY, "member": F, "viewer": F},
    "remove-viewer": {"owner": OK_REMOVE, "admin": OK_REMOVE, "member": F, "viewer": F},
    "remove-member": {"owner": OK_REMOVE, "admin": OK_REMOVE, "member": F, "viewer": F},
    "remove-admin": {"owner": OK_REMOVE, "admin": OK_REMOVE, "member": F, "viewer": F},
    "remove-owner": {"owner": OK_REMOVE, "admin": OWNER_ONLY, "member": F, "viewer": F},
    "change-self": {"owner": SELF, "admin": SELF, "member": F, "viewer": F},
    "remove-self": {"owner": SELF, "admin": SELF, "member": F, "viewer": F},
}

CASES = [(op, role) for op in MATRIX for role in ROLES]


def _run(h: Household4, op: str) -> tuple[Any, uuid.UUID | None, str | None]:
    """Perform ``op``; return the response, the affected user and the role it should end with."""
    base = f"{API}/households/{h.household_id}/members"
    if op.startswith("add-"):
        role = op.removeprefix("add-")
        body = {"user_id": str(h.outsider.id), "role": role}
        return h.actor.post(base, json=body), h.outsider.id, role
    if op.startswith("remove-"):
        who = op.removeprefix("remove-")
        target = h.actor.user.id if who == "self" else h.others[who].id
        return h.actor.delete(f"{base}/{target}"), target, None
    if op == "change-self":
        target = h.actor.user.id
        return h.actor.patch(f"{base}/{target}", json={"role": "viewer"}), target, "viewer"
    current, new = op.split("-to-")
    target = h.others[current].id
    return h.actor.patch(f"{base}/{target}", json={"role": new}), target, new


@pytest.mark.parametrize(("op", "role"), CASES, ids=[f"{r}:{o}" for o, r in CASES])
def test_member_operation_matrix(actors: Actors, factories: Factories, op: str, role: str) -> None:
    h = _setup(actors, role)
    before = _roles(factories, h.household_id)
    response, target, end_role = _run(h, op)
    expected = MATRIX[op][role]
    status, slug = (expected, None) if isinstance(expected, int) else expected
    assert response.status_code == status, response.text
    after = _roles(factories, h.household_id)
    if slug is not None:
        assert problem_type(response) == slug
        assert after == before  # a refused operation changes nothing
        return
    assert target is not None
    if end_role is None:
        assert target not in after
    else:
        assert after[target] == end_role
    assert sum(1 for r in after.values() if r == "owner") >= 1


def test_admin_cannot_demote_or_remove_the_only_owner(actors: Actors, factories: Factories) -> None:
    owner = actors.user(role="owner")
    household = owner.household
    assert household is not None
    admin = actors.new(role="admin", household=household)
    base = f"{API}/households/{household.id}/members/{owner.id}"
    for response in (admin.patch(base, json={"role": "member"}), admin.delete(base)):
        assert (response.status_code, problem_type(response)) == (403, "owner-protected")
    assert _roles(factories, household.id)[owner.id] == "owner"


def test_admin_cannot_promote_self_to_owner(actors: Actors, factories: Factories) -> None:
    admin = actors.new(role="admin")
    household_id = admin.record.household_id
    assert household_id is not None
    response = admin.patch(
        f"{API}/households/{household_id}/members/{admin.user.id}", json={"role": "owner"}
    )
    assert response.status_code in (403, 409)
    assert _roles(factories, household_id)[admin.user.id] == "admin"


@pytest.mark.parametrize("role", ["member", "viewer"])
def test_members_and_viewers_cannot_change_the_household(actors: Actors, role: str) -> None:
    actor = actors.new(role=role)
    response = actor.patch(f"{API}/households/{actor.record.household_id}", json={"name": "Mine"})
    assert (response.status_code, problem_type(response)) == (403, "forbidden")


@pytest.mark.parametrize("role", ["admin", "member", "viewer"])
def test_only_the_owner_deletes_the_household(
    actors: Actors, factories: Factories, role: str
) -> None:
    actor = actors.new(role=role)
    household_id = actor.record.household_id
    assert household_id is not None
    response = actor.delete(f"{API}/households/{household_id}")
    assert (response.status_code, problem_type(response)) == (403, "forbidden")
    with factories.transaction() as db:
        set_context(db, household_id)
        assert db.get(Household, household_id) is not None


@pytest.mark.parametrize("role", ROLES)
def test_only_instance_admins_create_households(actors: Actors, role: str) -> None:
    actor = actors.new(role=role)
    response = actor.post(f"{API}/households", json={"name": "Mine"})
    assert (response.status_code, problem_type(response)) == (403, "forbidden")


def test_instance_admin_token_cannot_create_households(
    actors: Actors, factories: Factories
) -> None:
    admin = actors.user(role="owner", is_instance_admin=True)
    raw = factories.token(admin)
    response = actors.anonymous().post(
        f"{API}/households", json={"name": "Mine"}, headers={"Authorization": f"Bearer {raw}"}
    )
    assert response.status_code == 403


# ---- cross-household access is 404 (F3) ------------------------------------------


def _foreign_requests(foreign_id: uuid.UUID, foreign_user: uuid.UUID) -> list[tuple[str, str, Any]]:
    base = f"{API}/households/{foreign_id}"
    return [
        ("GET", base, None),
        ("PATCH", base, {"name": "Taken over"}),
        ("DELETE", base, None),
        ("GET", f"{base}/members", None),
        ("POST", f"{base}/members", {"user_id": str(uuid.uuid4()), "role": "owner"}),
        ("PATCH", f"{base}/members/{foreign_user}", {"role": "viewer"}),
        ("DELETE", f"{base}/members/{foreign_user}", None),
    ]


@pytest.mark.parametrize("role", ROLES)
def test_other_households_are_404_for_every_role(
    actors: Actors, factories: Factories, role: str
) -> None:
    actor = actors.new(role=role)
    victim = actors.user(role="owner")
    assert victim.household is not None
    for method, url, body in _foreign_requests(victim.household.id, victim.id):
        response = actor.request(method, url, json=body)
        assert response.status_code == 404, (method, url, response.text)
        assert problem_type(response) == "household-not-found"
    with factories.transaction() as db:
        set_context(db, victim.household.id)
        household = db.get(Household, victim.household.id)
        assert household is not None
        assert household.name == victim.household.name
    assert _roles(factories, victim.household.id) == {victim.id: "owner"}


def test_unknown_household_is_404(actors: Actors) -> None:
    actor = actors.new(role="owner")
    for method, url, body in _foreign_requests(uuid.uuid4(), uuid.uuid4()):
        response = actor.request(method, url, json=body)
        assert (response.status_code, problem_type(response)) == (404, "household-not-found")


def test_instance_admin_gets_404_for_households_they_are_not_in(
    actors: Actors, factories: Factories
) -> None:
    admin = actors.new(role="owner", is_instance_admin=True)
    victim = actors.user(role="owner")
    assert victim.household is not None
    for method, url, body in _foreign_requests(victim.household.id, victim.id):
        response = admin.request(method, url, json=body)
        assert response.status_code == 404, (method, url)
    assert _roles(factories, victim.household.id) == {victim.id: "owner"}


def test_user_without_household_gets_404(actors: Actors) -> None:
    loner = actors.new(role=None)
    victim = actors.user(role="owner")
    assert victim.household is not None
    response = loner.get(f"{API}/households/{victim.household.id}")
    assert (response.status_code, problem_type(response)) == (404, "household-not-found")


def test_member_of_another_household_is_404_through_my_household(
    actors: Actors, factories: Factories
) -> None:
    owner = actors.new(role="owner")
    household_id = owner.record.household_id
    stranger = actors.user(role="member")
    assert stranger.household_id is not None
    base = f"{API}/households/{household_id}/members/{stranger.id}"
    for response in (owner.patch(base, json={"role": "viewer"}), owner.delete(base)):
        assert (response.status_code, problem_type(response)) == (404, "member-not-found")
    assert _roles(factories, stranger.household_id) == {stranger.id: "member"}


def test_membership_elsewhere_is_not_access_without_switching(
    actors: Actors, factories: Factories
) -> None:
    actor = actors.new(role="owner")
    second = factories.household()
    factories.member(second.id, actor.user.id, HouseholdRole.OWNER)
    response = actor.get(f"{API}/households/{second.id}")
    assert response.status_code == 404  # switch first (POST /auth/session/household)


# ---- mass assignment (F4) ----------------------------------------------------------


def test_household_inputs_reject_unknown_fields(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner", is_instance_admin=True)
    household_id = owner.record.household_id
    assert household_id is not None
    for body in (
        {"name": "X", "id": str(uuid.uuid4())},
        {"name": "X", "created_at": "2020-01-01T00:00:00Z"},
        {"name": "X", "owner": str(owner.user.id)},
    ):
        response = owner.patch(f"{API}/households/{household_id}", json=body)
        assert response.status_code == 422, body
        response = owner.post(f"{API}/households", json=body)
        assert response.status_code == 422, body
    with factories.transaction() as db:
        set_context(db, household_id)
        household = db.get(Household, household_id)
        assert household is not None
        assert household.name != "X"


def test_member_inputs_reject_unknown_fields(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    household_id = owner.record.household_id
    assert household_id is not None
    target = actors.user(role="member", household=owner.record.household)
    other = actors.user(role="owner")
    base = f"{API}/households/{household_id}/members"
    bad_patches = (
        {"role": "viewer", "user_id": str(owner.user.id)},
        {"role": "viewer", "household_id": str(other.household_id)},
        {"role": "viewer", "is_instance_admin": True},
    )
    for body in bad_patches:
        response = owner.patch(f"{base}/{target.id}", json=body)
        assert response.status_code == 422, body
    bad_adds: tuple[dict[str, Any], ...] = (
        {"user_id": str(actors.user(role=None).id), "role": "member", "household_id": "x"},
        {
            "role": "member",
            "new_user": {
                "username": f"mass-{uuid.uuid4().hex[:8]}",
                "display_name": "Mass",
                "password": "a long synthetic passphrase 7",
                "is_instance_admin": True,
            },
        },
        {
            "role": "member",
            "new_user": {
                "username": f"mass-{uuid.uuid4().hex[:8]}",
                "display_name": "Mass",
                "password": "a long synthetic passphrase 7",
                "is_breakglass": True,
            },
        },
    )
    for body in bad_adds:
        response = owner.post(base, json=body)
        assert response.status_code == 422, body
    response = owner.patch(f"{base}/{target.id}", json={"role": "superuser"})
    assert response.status_code == 422
    assert _roles(factories, household_id) == {owner.user.id: "owner", target.id: "member"}


def test_switch_input_rejects_unknown_fields(actors: Actors) -> None:
    actor = actors.new(role="owner")
    response = actor.post(
        f"{API}/auth/session/household",
        json={"household_id": str(actor.record.household_id), "role": "owner"},
    )
    assert response.status_code == 422

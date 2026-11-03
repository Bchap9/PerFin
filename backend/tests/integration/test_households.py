# SPDX-License-Identifier: AGPL-3.0-or-later
"""Households, members and household switching over HTTP.

The role-by-operation matrix, cross-household ``404`` and mass-assignment
checks are in ``tests/security/test_household_authz.py``; this module covers
behaviour: what is created, changed, audited, emitted and revoked.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select

from app.auth.models import (
    AccountGrant,
    ApiToken,
    GrantLevel,
    HouseholdMember,
    HouseholdRole,
    User,
    UserSession,
)
from app.auth.sessions import cookie_name, hash_session_id
from app.core.settings import Settings
from app.db.models import Household
from app.db.rls import set_context
from app.domain.households.service import PRIVILEGE_CHANGE_REASON
from tests.fixtures.factories import Factories
from tests.fixtures.households import (
    API,
    Actors,
    household_audit,
    household_events,
    instance_audit,
    problem_type,
)
from tests.fixtures.ledger import LedgerFactories

pytestmark = pytest.mark.integration


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


def _session_row(factories: Factories, session_id: uuid.UUID) -> UserSession:
    with factories.transaction() as db:
        row = db.get(UserSession, session_id)
        assert row is not None
        db.expunge(row)
        return row


def _member_role(factories: Factories, household_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
    with factories.transaction() as db:
        set_context(db, household_id)
        role = db.execute(
            select(HouseholdMember.role).where(
                HouseholdMember.household_id == household_id, HouseholdMember.user_id == user_id
            )
        ).scalar_one_or_none()
        return None if role is None else role.value


# ---- households --------------------------------------------------------------------


def test_list_households_shows_mine_with_role(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    other = factories.household()
    factories.member(other.id, owner.user.id, HouseholdRole.VIEWER)
    factories.household()  # not mine
    response = owner.get(f"{API}/households")
    assert response.status_code == 200
    got = {h["id"]: h["role"] for h in response.json()}
    assert got == {str(owner.record.household_id): "owner", str(other.id): "viewer"}


def test_instance_admin_lists_every_household(actors: Actors, factories: Factories) -> None:
    admin = actors.new(role="owner", is_instance_admin=True)
    foreign = factories.household()
    got = {h["id"]: h["role"] for h in admin.get(f"{API}/households").json()}
    assert got[str(admin.record.household_id)] == "owner"
    assert got[str(foreign.id)] is None


def test_instance_admin_token_lists_only_own_households(
    actors: Actors, factories: Factories
) -> None:
    admin = actors.user(role="owner", is_instance_admin=True)
    factories.household()
    client = actors.anonymous()
    raw = factories.token(admin, scopes=["households:read"])
    response = client.get(f"{API}/households", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 200
    assert [h["id"] for h in response.json()] == [str(admin.household_id)]


def test_instance_admin_creates_household_owned_by_self(
    actors: Actors, factories: Factories
) -> None:
    admin = actors.new(role=None, is_instance_admin=True)
    response = admin.post(
        f"{API}/households", json={"name": "  Synthetic Home ", "base_currency": "eur"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Synthetic Home"
    assert body["base_currency"] == "EUR"
    assert body["role"] == "owner"
    household_id = uuid.UUID(body["id"])
    assert _member_role(factories, household_id, admin.user.id) == "owner"
    rows = household_audit(factories, household_id, "household.create")
    assert len(rows) == 1
    assert rows[0].user_id == admin.user.id
    assert rows[0].after is not None
    assert rows[0].after["base_currency"] == "EUR"


def test_instance_admin_creates_household_for_another_owner(
    actors: Actors, factories: Factories
) -> None:
    admin = actors.new(role=None, is_instance_admin=True)
    future_owner = actors.user(role=None)
    response = admin.post(
        f"{API}/households", json={"name": "For someone", "owner_user_id": str(future_owner.id)}
    )
    assert response.status_code == 201, response.text
    assert response.json()["role"] is None
    assert response.json()["base_currency"] == "USD"
    household_id = uuid.UUID(response.json()["id"])
    assert _member_role(factories, household_id, future_owner.id) == "owner"
    assert _member_role(factories, household_id, admin.user.id) is None


def test_create_household_unknown_or_inactive_owner(actors: Actors) -> None:
    admin = actors.new(role=None, is_instance_admin=True)
    response = admin.post(
        f"{API}/households", json={"name": "X", "owner_user_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404
    assert problem_type(response) == "user-not-found"
    inactive = actors.user(role=None, is_active=False)
    response = admin.post(
        f"{API}/households", json={"name": "X", "owner_user_id": str(inactive.id)}
    )
    assert response.status_code == 409
    assert problem_type(response) == "user-inactive"


@pytest.mark.parametrize("currency", ["US", "USDD", "XXX", "XAU", "ABC", "12$"])
def test_create_household_rejects_bad_currency(actors: Actors, currency: str) -> None:
    admin = actors.new(role=None, is_instance_admin=True)
    response = admin.post(f"{API}/households", json={"name": "X", "base_currency": currency})
    assert response.status_code == 422


def test_tokens_need_household_scopes(actors: Actors, factories: Factories) -> None:
    owner = actors.user(role="owner")
    url = f"{API}/households/{owner.household_id}"
    client = actors.anonymous()

    def auth(*scopes: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {factories.token(owner, scopes=list(scopes))}"}

    none, read, write = auth("transactions:read"), auth("households:read"), auth("households:write")
    for path in (f"{API}/households", url, f"{url}/members"):
        response = client.get(path, headers=none)
        assert (response.status_code, problem_type(response)) == (403, "insufficient-scope")
        assert client.get(path, headers=read).status_code == 200
    response = client.patch(url, json={"name": "By token"}, headers=read)
    assert (response.status_code, problem_type(response)) == (403, "insufficient-scope")
    response = client.patch(url, json={"name": "By token"}, headers=write)
    assert response.status_code == 200
    assert response.json()["name"] == "By token"


def test_get_active_household(actors: Actors) -> None:
    viewer = actors.new(role="viewer")
    response = viewer.get(f"{API}/households/{viewer.record.household_id}")
    assert response.status_code == 200
    assert response.json()["role"] == "viewer"
    assert response.json()["base_currency"] == "USD"


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owner_and_admin_rename_and_change_currency(
    actors: Actors, factories: Factories, role: str
) -> None:
    actor = actors.new(role=role)
    household_id = actor.record.household_id
    assert household_id is not None
    response = actor.patch(
        f"{API}/households/{household_id}", json={"name": "Renamed", "base_currency": "GBP"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed"
    assert response.json()["base_currency"] == "GBP"
    rows = household_audit(factories, household_id, "household.update")
    assert len(rows) == 1
    assert rows[0].after == {"name": "Renamed", "base_currency": "GBP"}
    assert rows[0].before is not None
    assert rows[0].before["base_currency"] == "USD"
    events = household_events(factories, household_id, "household.updated")
    assert [e.payload for e in events] == [{"fields": ["base_currency", "name"]}]


def test_patch_household_needs_a_field(actors: Actors) -> None:
    owner = actors.new(role="owner")
    response = owner.patch(f"{API}/households/{owner.record.household_id}", json={})
    assert response.status_code == 422


def test_owner_deletes_household_with_step_up(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    member = actors.new(role="member", household=owner.record.household)
    household_id = owner.record.household_id
    assert household_id is not None
    stale = actors.login(owner.record, mfa_verified=False)
    response = stale.delete(f"{API}/households/{household_id}")
    assert response.status_code == 403
    assert problem_type(response) == "step-up-required"
    response = owner.delete(f"{API}/households/{household_id}")
    assert response.status_code == 204, response.text
    with factories.transaction() as db:
        set_context(db, household_id)
        assert db.get(Household, household_id) is None
        assert db.execute(select(HouseholdMember)).scalars().all() == []
    assert len(instance_audit(factories, "household.delete", household_id)) == 1
    # The member's session lost its household; it carries on without one.
    session = member.get(f"{API}/auth/session")
    assert session.status_code == 200
    assert session.json()["household"] is None


# ---- members -----------------------------------------------------------------------


def test_list_members(actors: Actors) -> None:
    owner = actors.new(role="owner", display_name="Owner Person")
    viewer = actors.new(role="viewer", household=owner.record.household)
    response = viewer.get(f"{API}/households/{owner.record.household_id}/members")
    assert response.status_code == 200
    got = {m["user_id"]: m["role"] for m in response.json()}
    assert got == {str(owner.user.id): "owner", str(viewer.user.id): "viewer"}
    first = response.json()[0]
    assert set(first) == {"user_id", "username", "display_name", "role", "is_active", "joined_at"}


def test_add_existing_user_by_id_and_username(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    household_id = owner.record.household_id
    assert household_id is not None
    by_id = actors.user(role=None)
    by_name = actors.user(role=None)
    url = f"{API}/households/{household_id}/members"
    response = owner.post(url, json={"user_id": str(by_id.id), "role": "member"})
    assert response.status_code == 201, response.text
    assert response.json()["role"] == "member"
    response = owner.post(url, json={"username": by_name.user.username.upper(), "role": "viewer"})
    assert response.status_code == 201, response.text
    assert _member_role(factories, household_id, by_name.id) == "viewer"
    rows = household_audit(factories, household_id, "member.add")
    assert [r.after["role"] for r in rows if r.after] == ["member", "viewer"]
    events = household_events(factories, household_id, "member.added")
    assert {e.payload["user_id"] for e in events} == {str(by_id.id), str(by_name.id)}


def test_add_member_errors(actors: Actors) -> None:
    owner = actors.new(role="owner")
    url = f"{API}/households/{owner.record.household_id}/members"
    response = owner.post(url, json={"user_id": str(uuid.uuid4()), "role": "member"})
    assert (response.status_code, problem_type(response)) == (404, "user-not-found")
    response = owner.post(url, json={"username": "no-such-user-here", "role": "member"})
    assert (response.status_code, problem_type(response)) == (404, "user-not-found")
    response = owner.post(url, json={"user_id": str(owner.user.id), "role": "member"})
    assert (response.status_code, problem_type(response)) == (409, "already-member")
    inactive = actors.user(role=None, is_active=False)
    response = owner.post(url, json={"user_id": str(inactive.id), "role": "member"})
    assert (response.status_code, problem_type(response)) == (409, "user-inactive")
    response = owner.post(url, json={"role": "member"})
    assert response.status_code == 422
    other = actors.user(role=None)
    response = owner.post(
        url, json={"user_id": str(other.id), "username": other.user.username, "role": "member"}
    )
    assert response.status_code == 422


def test_invite_creates_a_local_user(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    household_id = owner.record.household_id
    assert household_id is not None
    url = f"{API}/households/{household_id}/members"
    username = f"invited-{uuid.uuid4().hex[:8]}"
    payload = {
        "role": "member",
        "new_user": {
            "username": username,
            "display_name": "Invited Person",
            "password": "correct horse battery staple 42",
            "email": "invited@example.test",
        },
    }
    response = owner.post(url, json=payload)
    assert response.status_code == 201, response.text
    user_id = uuid.UUID(response.json()["user_id"])
    with factories.transaction() as db:
        user = db.get(User, user_id)
        assert user is not None
        assert (user.username, user.is_instance_admin, user.is_breakglass) == (
            username,
            False,
            False,
        )
    assert _member_role(factories, household_id, user_id) == "member"
    assert len(instance_audit(factories, "user.create", user_id)) == 1
    # Same username again: taken.
    response = owner.post(url, json=payload)
    assert (response.status_code, problem_type(response)) == (409, "username-taken")


def test_invite_applies_the_password_policy(actors: Actors) -> None:
    owner = actors.new(role="owner")
    url = f"{API}/households/{owner.record.household_id}/members"
    for password, reason in (("short", "too-short"), ("password1234", "breached")):
        response = owner.post(
            url,
            json={
                "role": "viewer",
                "new_user": {
                    "username": f"weak-{uuid.uuid4().hex[:8]}",
                    "display_name": "Weak",
                    "password": password,
                },
            },
        )
        assert response.status_code == 422, response.text
        assert problem_type(response) == "weak-password"
        assert response.json()["reason"] == reason


def test_role_change_is_audited_notified_and_rotates_sessions(
    actors: Actors, factories: Factories
) -> None:
    owner = actors.new(role="owner")
    household_id = owner.record.household_id
    assert household_id is not None
    target = actors.new(role="member", household=owner.record.household)
    assert target.get(f"{API}/auth/session").json()["household"]["role"] == "member"
    response = owner.patch(
        f"{API}/households/{household_id}/members/{target.user.id}", json={"role": "admin"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "admin"
    # The affected user's session acting in the household is revoked (A3).
    row = _session_row(factories, target.login.session_id)
    assert row.revoked_at is not None
    assert row.revoked_reason == PRIVILEGE_CHANGE_REASON
    assert target.get(f"{API}/households/{household_id}").status_code == 401
    # The actor's own session is untouched.
    assert owner.get(f"{API}/households/{household_id}").status_code == 200
    rows = household_audit(factories, household_id, "member.role_change")
    assert len(rows) == 1
    assert rows[0].user_id == owner.user.id
    assert rows[0].before == {"user_id": str(target.user.id), "role": "member"}
    assert rows[0].after == {"user_id": str(target.user.id), "role": "admin"}
    events = household_events(factories, household_id, "member.role_changed")
    assert [e.payload for e in events] == [
        {
            "user_id": str(target.user.id),
            "role": "admin",
            "previous_role": "member",
            "by": str(owner.user.id),
        }
    ]
    # A new login sees the new role.
    again = actors.login(target.record)
    assert again.get(f"{API}/auth/session").json()["household"]["role"] == "admin"


def test_role_change_leaves_sessions_in_other_households(
    actors: Actors, factories: Factories
) -> None:
    owner = actors.new(role="owner")
    household_id = owner.record.household_id
    target = actors.new(role="member")  # active in their own household
    assert household_id is not None
    assert target.record.household_id is not None
    factories.member(household_id, target.user.id, HouseholdRole.MEMBER)
    response = owner.patch(
        f"{API}/households/{household_id}/members/{target.user.id}", json={"role": "viewer"}
    )
    assert response.status_code == 200
    assert _session_row(factories, target.login.session_id).revoked_at is None


def test_same_role_is_a_no_op(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    household_id = owner.record.household_id
    assert household_id is not None
    target = actors.new(role="member", household=owner.record.household)
    response = owner.patch(
        f"{API}/households/{household_id}/members/{target.user.id}", json={"role": "member"}
    )
    assert response.status_code == 200
    assert household_audit(factories, household_id, "member.role_change") == []
    assert _session_row(factories, target.login.session_id).revoked_at is None


def test_remove_member_cleans_up(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    household_id = owner.record.household_id
    assert household_id is not None
    target = actors.new(role="member", household=owner.record.household)
    account_id = LedgerFactories(factories.session_factory).account(household_id).id
    factories.grant(household_id, target.user.id, account_id, GrantLevel.EDIT)
    raw = factories.token(target.record)
    response = owner.delete(f"{API}/households/{household_id}/members/{target.user.id}")
    assert response.status_code == 204, response.text
    assert _member_role(factories, household_id, target.user.id) is None
    with factories.transaction() as db:
        set_context(db, household_id)
        assert (
            db.execute(select(AccountGrant).where(AccountGrant.user_id == target.user.id)).first()
            is None
        )
        token = db.execute(select(ApiToken).where(ApiToken.user_id == target.user.id)).scalar_one()
        assert token.revoked_at is not None
    assert _session_row(factories, target.login.session_id).revoked_at is not None
    client = actors.anonymous()
    assert (
        client.get(f"{API}/auth/session", headers={"Authorization": f"Bearer {raw}"}).status_code
        == 401
    )
    rows = household_audit(factories, household_id, "member.remove")
    assert len(rows) == 1
    assert rows[0].before == {"user_id": str(target.user.id), "role": "member"}
    assert rows[0].after == {"grants_deleted": 1, "pats_revoked": 1}
    events = household_events(factories, household_id, "member.removed")
    assert [e.payload["user_id"] for e in events] == [str(target.user.id)]
    # Removing again: no such member.
    response = owner.delete(f"{API}/households/{household_id}/members/{target.user.id}")
    assert (response.status_code, problem_type(response)) == (404, "member-not-found")


def test_co_owner_can_be_demoted_by_an_owner(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    household_id = owner.record.household_id
    assert household_id is not None
    other = actors.new(role="owner", household=owner.record.household)
    url = f"{API}/households/{household_id}/members/{other.user.id}"
    response = owner.patch(url, json={"role": "admin"})
    assert response.status_code == 200
    # Now the actor is the last owner; nobody else can demote them, and they
    # cannot demote themselves.
    response = owner.patch(
        f"{API}/households/{household_id}/members/{owner.user.id}", json={"role": "admin"}
    )
    assert (response.status_code, problem_type(response)) == (409, "own-membership")
    assert _member_role(factories, household_id, owner.user.id) == "owner"


def test_member_management_needs_fresh_step_up(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    household_id = owner.record.household_id
    assert household_id is not None
    target = actors.user(role="member", household=owner.record.household)
    stale = actors.login(owner.record, mfa_verified=False)
    url = f"{API}/households/{household_id}/members"
    for method, path, body in (
        ("POST", url, {"user_id": str(actors.user(role=None).id), "role": "member"}),
        ("PATCH", f"{url}/{target.id}", {"role": "viewer"}),
        ("DELETE", f"{url}/{target.id}", None),
    ):
        response = stale.request(method, path, json=body)
        assert response.status_code == 403, (method, response.text)
        assert problem_type(response) == "step-up-required"
    assert _member_role(factories, household_id, target.id) == "member"


def test_tokens_cannot_manage_members(actors: Actors, factories: Factories) -> None:
    owner = actors.user(role="owner")
    target = actors.user(role="member", household=owner.household)
    raw = factories.token(owner)
    client = actors.anonymous()
    response = client.patch(
        f"{API}/households/{owner.household_id}/members/{target.id}",
        json={"role": "viewer"},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert response.status_code == 403
    assert problem_type(response) == "step-up-required"


# ---- switching households ----------------------------------------------------------


def test_switch_household_rotates_session(
    actors: Actors, factories: Factories, settings: Settings
) -> None:
    actor = actors.new(role="owner")
    first = actor.record.household_id
    second = factories.household()
    factories.member(second.id, actor.user.id, HouseholdRole.VIEWER)
    old_raw = actor.login.raw_id
    response = actor.post(f"{API}/auth/session/household", json={"household_id": str(second.id)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["household"] == {"id": str(second.id), "name": second.name, "role": "viewer"}
    new_raw = response.cookies.get(cookie_name(settings))
    assert new_raw
    assert new_raw != old_raw
    # The old id is dead; the new one acts in the second household.
    old = _session_row(factories, actor.login.session_id)
    assert old.revoked_at is not None
    assert old.revoked_reason == "privilege_change"
    with factories.transaction() as db:
        new = db.execute(
            select(UserSession).where(UserSession.token_hash == hash_session_id(new_raw))
        ).scalar_one()
        assert new.household_id == second.id
        assert new.rotated_from_id == actor.login.session_id
    actor.client.cookies.clear()
    actor.client.cookies.set(cookie_name(settings), new_raw)
    session = actor.get(f"{API}/auth/session").json()
    assert session["household"]["id"] == str(second.id)
    assert session["csrf_token"] == body["csrf_token"]
    # The first household is now "another" household for this session.
    assert actor.get(f"{API}/households/{first}").status_code == 404
    rows = household_audit(factories, second.id, "session.switch_household")
    assert len(rows) == 1
    assert rows[0].before == {"household_id": str(first)}


def test_switch_to_a_household_i_am_not_in_is_404(actors: Actors, factories: Factories) -> None:
    actor = actors.new(role="owner")
    foreign = factories.household()
    for target in (foreign.id, uuid.uuid4()):
        response = actor.post(f"{API}/auth/session/household", json={"household_id": str(target)})
        assert (response.status_code, problem_type(response)) == (404, "household-not-found")
    assert _session_row(factories, actor.login.session_id).revoked_at is None


def test_switch_household_needs_a_browser_session(actors: Actors, factories: Factories) -> None:
    record = actors.user(role="owner")
    raw = factories.token(record)
    client = actors.anonymous()
    response = client.post(
        f"{API}/auth/session/household",
        json={"household_id": str(record.household_id)},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert (response.status_code, problem_type(response)) == (403, "session-required")

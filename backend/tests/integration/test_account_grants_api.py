# SPDX-License-Identifier: AGPL-3.0-or-later
"""Account grants over HTTP: who may see or change which account (AUTH.md, I2, F3)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from app.core.settings import Settings
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import (
    API,
    Actors,
    household_audit,
    household_events,
    problem_type,
)
from tests.fixtures.ledger import LedgerFactories

pytestmark = pytest.mark.integration


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


def _grants_url(
    client: AuthedClient, user_id: uuid.UUID, account_id: uuid.UUID | None = None
) -> str:
    household = client.record.household
    assert household is not None
    base = f"{API}/households/{household.id}/members/{user_id}/grants"
    return base if account_id is None else f"{base}/{account_id}"


def test_owner_grants_and_revokes_access(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    member = actors.new(role="member", household=household)
    account = ledger.account(household.id)
    account_url = f"{API}/accounts/{account.id}"
    assert member.get(account_url).status_code == 404

    granted = owner.put(_grants_url(owner, member.user.id, account.id), json={"level": "view"})
    assert granted.status_code == 200, granted.text
    assert (granted.json()["level"], granted.json()["account_id"]) == ("view", str(account.id))
    assert member.get(account_url).status_code == 200
    assert member.patch(account_url, json={"name": "X"}).status_code == 403

    upgraded = owner.put(_grants_url(owner, member.user.id, account.id), json={"level": "edit"})
    assert upgraded.json()["level"] == "edit"
    assert member.patch(account_url, json={"name": "X"}).status_code == 200

    listed = owner.get(_grants_url(owner, member.user.id))
    assert [(g["account_id"], g["level"]) for g in listed.json()] == [(str(account.id), "edit")]
    on_account = owner.get(f"{account_url}/grants")
    assert [(g["user_id"], g["level"]) for g in on_account.json()] == [
        (str(member.user.id), "edit")
    ]
    # The member reads their own grants, not other people's.
    assert member.get(_grants_url(member, member.user.id)).status_code == 200
    other = actors.new(role="member", household=household)
    refused = member.get(_grants_url(member, other.user.id))
    assert (refused.status_code, problem_type(refused)) == (403, "forbidden")
    assert member.get(f"{account_url}/grants").status_code == 403

    revoked = owner.delete(_grants_url(owner, member.user.id, account.id))
    assert revoked.status_code == 204
    assert member.get(account_url).status_code == 404
    assert owner.delete(_grants_url(owner, member.user.id, account.id)).status_code == 204

    assert len(household_audit(factories, household.id, "account_grant.set")) == 2
    assert len(household_audit(factories, household.id, "account_grant.revoke")) == 1
    events = household_events(factories, household.id, "account_grant.changed")
    assert len(events) == 3
    assert events[0].payload == {"user_id": str(member.user.id), "account_id": str(account.id)}


def test_level_none_hides_the_account(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    viewer = actors.new(role="viewer", household=household)
    account = ledger.account(household.id)
    url = _grants_url(owner, viewer.user.id, account.id)
    assert owner.put(url, json={"level": "view"}).status_code == 200
    assert viewer.get(f"{API}/accounts/{account.id}").status_code == 200
    assert owner.put(url, json={"level": "none"}).json()["level"] == "none"
    assert viewer.get(f"{API}/accounts/{account.id}").status_code == 404


def test_grant_rules(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    admin = actors.new(role="admin", household=household)
    viewer = actors.new(role="viewer", household=household)
    account = ledger.account(household.id)

    viewer_edit = owner.put(_grants_url(owner, viewer.user.id, account.id), json={"level": "edit"})
    assert (viewer_edit.status_code, problem_type(viewer_edit)) == (
        422,
        "grant-level-not-allowed",
    )
    for_admin = owner.put(_grants_url(owner, admin.user.id, account.id), json={"level": "view"})
    assert (for_admin.status_code, problem_type(for_admin)) == (422, "grant-not-applicable")
    stranger = owner.put(_grants_url(owner, uuid.uuid4(), account.id), json={"level": "view"})
    assert (stranger.status_code, problem_type(stranger)) == (404, "member-not-found")
    nowhere = owner.put(_grants_url(owner, viewer.user.id, uuid.uuid4()), json={"level": "view"})
    assert (nowhere.status_code, problem_type(nowhere)) == (404, "account-not-found")
    gone = owner.delete(_grants_url(owner, uuid.uuid4(), account.id))
    assert (gone.status_code, problem_type(gone)) == (404, "member-not-found")
    for body in ({}, {"level": "owner"}, {"level": "view", "user_id": str(viewer.user.id)}):
        response = owner.put(_grants_url(owner, viewer.user.id, account.id), json=body)
        assert response.status_code == 422, body
    # The admin manages grants too.
    assert (
        admin.put(
            _grants_url(admin, viewer.user.id, account.id), json={"level": "view"}
        ).status_code
        == 200
    )


def test_changes_need_step_up_and_a_browser_session(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner", mfa_verified=False)
    household = owner.record.household
    assert household is not None
    member = actors.new(role="member", household=household)
    account = ledger.account(household.id)
    url = _grants_url(owner, member.user.id, account.id)
    stale = owner.put(url, json={"level": "view"})
    assert (stale.status_code, problem_type(stale)) == (403, "step-up-required")
    raw = factories.token(owner.record, scopes=("households:read", "households:write"))
    token = actors.anonymous().put(
        url, json={"level": "view"}, headers={"Authorization": f"Bearer {raw}"}
    )
    assert token.status_code == 403
    # Reading works with the households:read scope.
    listed = actors.anonymous().get(
        _grants_url(owner, member.user.id), headers={"Authorization": f"Bearer {raw}"}
    )
    assert listed.status_code == 200


def test_another_households_account_cannot_be_granted(
    actors: Actors, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    member = actors.new(role="member", household=household)
    foreign_owner = actors.new(role="owner")
    foreign_household = foreign_owner.record.household
    assert foreign_household is not None
    foreign = ledger.account(foreign_household.id)
    response = owner.put(_grants_url(owner, member.user.id, foreign.id), json={"level": "view"})
    assert (response.status_code, problem_type(response)) == (404, "account-not-found")
    assert owner.get(f"{API}/accounts/{foreign.id}/grants").status_code == 404
    # And the other household's path is 404 for us.
    elsewhere = owner.get(
        f"{API}/households/{foreign_household.id}/members/{foreign_owner.user.id}/grants"
    )
    assert (elsewhere.status_code, problem_type(elsewhere)) == (404, "household-not-found")

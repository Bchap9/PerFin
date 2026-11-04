# SPDX-License-Identifier: AGPL-3.0-or-later
"""Savings goals over HTTP: accounts, contributions, progress and visibility.

Progress figures are exact (``Decimal`` strings); the rules are in
``app.domain.goals.progress`` (unit-tested in ``tests/unit/test_budget_math.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.domain.accounts.models import Account, AccountType
from app.domain.goals import service as goals_service
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

GOALS = f"{API}/goals"
TODAY = date(2026, 6, 30)


@dataclass(frozen=True)
class World:
    owner: AuthedClient
    member: AuthedClient
    viewer: AuthedClient
    household_id: uuid.UUID
    savings: Account
    euro: Account
    hidden: Account
    ledger: LedgerFactories


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


@pytest.fixture
def world(actors: Actors, ledger: LedgerFactories, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setattr(goals_service, "today", lambda: TODAY)
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    member = actors.new(role="member", household=household)
    viewer = actors.new(role="viewer", household=household)
    hid = household.id
    savings = ledger.account(hid, type=AccountType.SAVINGS, name="Savings")
    euro = ledger.account(hid, type=AccountType.SAVINGS, name="Euro savings", currency="EUR")
    hidden = ledger.account(hid, type=AccountType.SAVINGS, name="Hidden")
    ledger.snapshot(savings, as_of_date=date(2026, 1, 1), balance="1000.00")
    ledger.snapshot(euro, as_of_date=date(2026, 1, 1), balance="100.00")
    ledger.fx_rate(hid, base="EUR", quote="USD", rate_date=date(2026, 6, 1), rate="1.1")
    for account in (savings, euro):
        ledger.grant(hid, member.record.id, account, GrantLevel.EDIT)
        ledger.grant(hid, viewer.record.id, account, GrantLevel.VIEW)
    return World(owner, member, viewer, hid, savings, euro, hidden, ledger)


def _usd(amount: str) -> dict[str, str]:
    return {"amount": amount, "currency": "USD"}


def _post(client: AuthedClient, url: str, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(url, json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def _goal(client: AuthedClient, accounts: list[Account], **extra: Any) -> dict[str, Any]:
    body = {
        "name": "Emergency fund",
        "target": _usd("5000"),
        "account_ids": [str(a.id) for a in accounts],
        **extra,
    }
    return _post(client, GOALS, body)


def test_goal_progress_and_projection(world: World, factories: Factories) -> None:
    goal = _goal(world.member, [world.savings], target_date="2027-01-01")
    progress = goal["progress"]
    assert progress == {
        "as_of": "2026-06-30",
        "contributed": _usd("0.00"),
        "remaining": _usd("5000.00"),
        "percent": "0.00",
        "accounts_balance": _usd("1000.00"),
        "monthly_rate": None,
        "projected_completion": None,
        "on_track": None,
    }
    url = f"{GOALS}/{goal['id']}/contributions"
    _post(world.member, url, {"amount": _usd("500"), "contributed_on": "2026-01-15"})
    _post(world.member, url, {"amount": _usd("250.50"), "contributed_on": "2026-03-01"})
    _post(world.member, url, {"amount": _usd("300"), "contributed_on": "2026-06-01", "memo": "x"})
    deposit = world.ledger.transaction(world.savings, posted_date=date(2026, 6, 15), amount="200")
    linked = _post(world.member, url, {"transaction_id": str(deposit.id)})
    assert (linked["amount"], linked["contributed_on"], linked["transaction_id"]) == (
        _usd("200.00"),
        "2026-06-15",
        str(deposit.id),
    )
    again = world.member.post(url, json={"transaction_id": str(deposit.id)})
    assert (again.status_code, problem_type(again)) == (409, "contribution-exists")

    body = world.viewer.get(f"{GOALS}/{goal['id']}").json()
    # 1250.50 over the 167 days since the first contribution: 7.488024/day,
    # 227.916729 a month; 3749.50 to go takes 501 more days.
    assert body["progress"] == {
        "as_of": "2026-06-30",
        "contributed": _usd("1250.50"),
        "remaining": _usd("3749.50"),
        "percent": "25.01",
        "accounts_balance": _usd("1200.00"),  # snapshot 1000 + the 200 deposit
        "monthly_rate": _usd("227.916729"),
        "projected_completion": "2027-11-13",
        "on_track": False,
    }
    assert body["achieved_at"] is None
    listed = world.viewer.get(url).json()
    assert [c["amount"]["amount"] for c in listed["items"]] == [
        "500.00",
        "250.50",
        "300.00",
        "200.00",
    ]
    page = world.viewer.get(url, params={"limit": 3}).json()
    assert len(page["items"]) == 3
    assert page["next_cursor"] == listed["items"][2]["id"]

    [added, *_] = household_audit(factories, world.household_id, "goal.contribution_add")
    assert added.after is not None
    assert "memo" not in added.after
    assert len(household_audit(factories, world.household_id, "goal.create")) == 1
    # Ids only; the goal's accounts let the event stream filter it (P2-SR-03).
    assert household_events(factories, world.household_id, "goal.created")[0].payload == {
        "goal_id": goal["id"],
        "all_account_ids": sorted(goal["account_ids"]),
    }


def test_foreign_currency_link_and_balance(world: World) -> None:
    goal = _goal(world.owner, [world.savings, world.euro])
    # 100 EUR at 1.1 (rate of 2026-06-01): 110.00 USD; 1000 + 110 = 1110.
    assert goal["progress"]["accounts_balance"] == _usd("1110.00")
    url = f"{GOALS}/{goal['id']}/contributions"
    euro_in = world.ledger.transaction(world.euro, posted_date=date(2026, 6, 20), amount="100")
    created = _post(world.owner, url, {"transaction_id": str(euro_in.id)})
    assert created["amount"] == _usd("110.00")
    early = world.ledger.transaction(world.euro, posted_date=date(2026, 5, 20), amount="5")
    missing = world.owner.post(url, json={"transaction_id": str(early.id)})
    assert (missing.status_code, problem_type(missing)) == (422, "fx-rate-missing")
    explicit = _post(world.owner, url, {"transaction_id": str(early.id), "amount": _usd("5.40")})
    assert (explicit["amount"], explicit["contributed_on"]) == (_usd("5.40"), "2026-05-20")


def test_contribution_rules(world: World) -> None:
    goal = _goal(world.member, [world.savings])
    url = f"{GOALS}/{goal['id']}/contributions"

    def refused(body: dict[str, Any]) -> tuple[int, str]:
        response = world.member.post(url, json=body)
        return response.status_code, problem_type(response)

    other = world.ledger.transaction(world.euro, posted_date=date(2026, 6, 1), amount="10")
    hidden = world.ledger.transaction(world.hidden, posted_date=date(2026, 6, 1), amount="10")
    assert refused({"transaction_id": str(other.id)}) == (422, "contribution-transaction-invalid")
    assert refused({"transaction_id": str(hidden.id)}) == (404, "transaction-not-found")
    assert refused({"transaction_id": str(uuid.uuid4())}) == (404, "transaction-not-found")
    assert refused({"amount": _usd("1"), "contributed_on": "2026-07-01"}) == (
        422,
        "contribution-in-future",
    )
    assert refused({"amount": _usd("0")}) == (422, "invalid-contribution")
    assert refused({}) == (422, "invalid-contribution")
    eur = {"amount": {"amount": "1", "currency": "EUR"}}
    assert refused(eur) == (422, "currency-mismatch")
    assert (
        world.member.post(url, json={"amount": _usd("1"), "goal_id": goal["id"]}).status_code == 422
    )
    withdrawal = _post(world.member, url, {"amount": _usd("-25")})
    assert (withdrawal["amount"], withdrawal["contributed_on"]) == (_usd("-25.00"), "2026-06-30")
    viewer = world.viewer.post(url, json={"amount": _usd("1")})
    assert (viewer.status_code, problem_type(viewer)) == (403, "forbidden")


def test_achieved_follows_the_contributions(world: World) -> None:
    goal = _goal(world.owner, [], target=_usd("100"))
    url = f"{GOALS}/{goal['id']}/contributions"
    _post(world.owner, url, {"amount": _usd("60"), "contributed_on": "2026-06-10"})
    last = _post(world.owner, url, {"amount": _usd("40"), "contributed_on": "2026-06-20"})
    body = world.owner.get(f"{GOALS}/{goal['id']}").json()
    assert body["achieved_at"] is not None
    assert body["progress"]["percent"] == "100.00"
    assert body["progress"]["remaining"] == _usd("0.00")
    assert body["progress"]["projected_completion"] == "2026-06-20"  # the day it was reached
    assert body["progress"]["accounts_balance"] is None  # no accounts
    assert world.owner.delete(f"{url}/{last['id']}").status_code == 204
    assert world.owner.get(f"{GOALS}/{goal['id']}").json()["achieved_at"] is None
    gone = world.owner.delete(f"{url}/{last['id']}")
    assert (gone.status_code, problem_type(gone)) == (404, "contribution-not-found")
    raised = world.owner.patch(f"{GOALS}/{goal['id']}", json={"target": _usd("50")})
    assert raised.json()["achieved_at"] is not None


def test_goal_visibility(world: World, actors: Actors) -> None:
    shared = _goal(world.owner, [world.savings])
    secret = _goal(world.owner, [world.savings, world.hidden], name="Secret")
    ids = {g["id"] for g in world.member.get(GOALS).json()["items"]}
    assert ids == {shared["id"]}
    assert {g["id"] for g in world.owner.get(GOALS).json()["items"]} == {
        shared["id"],
        secret["id"],
    }
    for response in (
        world.member.get(f"{GOALS}/{secret['id']}"),
        world.member.get(f"{GOALS}/{secret['id']}/contributions"),
        world.member.patch(f"{GOALS}/{secret['id']}", json={"name": "Mine"}),
        world.member.post(f"{GOALS}/{secret['id']}/contributions", json={"amount": _usd("1")}),
        world.member.delete(f"{GOALS}/{secret['id']}"),
    ):
        assert (response.status_code, problem_type(response)) == (404, "goal-not-found")
    hidden_create = world.member.post(
        GOALS, json={"name": "X", "target": _usd("1"), "account_ids": [str(world.hidden.id)]}
    )
    assert (hidden_create.status_code, problem_type(hidden_create)) == (404, "account-not-found")
    moved = world.member.patch(
        f"{GOALS}/{shared['id']}", json={"account_ids": [str(world.hidden.id)]}
    )
    assert (moved.status_code, problem_type(moved)) == (404, "account-not-found")

    # A goal whose contribution is linked to a transaction on an account the
    # member cannot see is hidden too, even after the account leaves the goal.
    linked = _goal(world.owner, [world.hidden], name="Linked")
    deposit = world.ledger.transaction(world.hidden, posted_date=date(2026, 6, 1), amount="10")
    _post(world.owner, f"{GOALS}/{linked['id']}/contributions", {"transaction_id": str(deposit.id)})
    world.owner.patch(f"{GOALS}/{linked['id']}", json={"account_ids": [str(world.savings.id)]})
    assert world.member.get(f"{GOALS}/{linked['id']}").status_code == 404

    outsider = actors.new(role="owner")
    for response in (
        outsider.get(f"{GOALS}/{shared['id']}"),
        outsider.patch(f"{GOALS}/{shared['id']}", json={"name": "X"}),
        outsider.delete(f"{GOALS}/{shared['id']}"),
    ):
        assert (response.status_code, problem_type(response)) == (404, "goal-not-found")
    assert outsider.get(GOALS).json()["items"] == []
    alien = outsider.post(
        GOALS, json={"name": "X", "target": _usd("1"), "account_ids": [str(world.savings.id)]}
    )
    assert (alien.status_code, problem_type(alien)) == (404, "account-not-found")


def test_update_archive_and_delete(world: World, factories: Factories) -> None:
    goal = _goal(world.member, [world.savings], target_date="2027-01-01")
    url = f"{GOALS}/{goal['id']}"
    patched = world.member.patch(
        url,
        json={
            "name": "House",
            "target": {"amount": "20000", "currency": "EUR"},
            "target_date": None,
            "account_ids": [str(world.euro.id), str(world.savings.id)],
        },
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert (body["name"], body["target"], body["target_date"]) == (
        "House",
        {"amount": "20000.00", "currency": "EUR"},
        None,
    )
    assert body["account_ids"] == [str(world.savings.id), str(world.euro.id)]
    # 100 EUR + 1000 USD / 1.1 (inverse of the EUR->USD rate) = 1009.090909 EUR.
    assert body["progress"]["accounts_balance"] == {"amount": "1009.090909", "currency": "EUR"}
    _post(world.member, f"{url}/contributions", {"amount": {"amount": "1", "currency": "EUR"}})
    locked = world.member.patch(url, json={"target": _usd("1")})
    assert (locked.status_code, problem_type(locked)) == (409, "goal-currency-locked")
    zero = world.member.patch(url, json={"target": {"amount": "0", "currency": "EUR"}})
    assert (zero.status_code, problem_type(zero)) == (422, "invalid-goal-target")
    assert world.member.patch(url, json={"archived": None}).status_code == 422
    archived = world.member.patch(url, json={"archived": True})
    assert archived.json()["archived"] is True
    assert world.member.get(GOALS, params={"archived": "false"}).json()["items"] == []
    assert len(world.member.get(GOALS, params={"archived": "true"}).json()["items"]) == 1
    assert world.member.delete(url).status_code == 204
    assert problem_type(world.member.get(url)) == "goal-not-found"
    assert len(household_audit(factories, world.household_id, "goal.delete")) == 1
    assert len(household_events(factories, world.household_id, "goal.deleted")) == 1


def test_deleting_a_linked_transaction_keeps_the_contribution(world: World) -> None:
    goal = _goal(world.owner, [world.savings])
    url = f"{GOALS}/{goal['id']}/contributions"
    deposit = world.ledger.transaction(world.savings, posted_date=date(2026, 6, 1), amount="75")
    _post(world.owner, url, {"transaction_id": str(deposit.id)})
    assert world.owner.delete(f"{API}/transactions/{deposit.id}").status_code == 204
    [row] = world.owner.get(url).json()["items"]
    assert (row["transaction_id"], row["amount"]) == (None, _usd("75.00"))
    assert world.owner.get(f"{GOALS}/{goal['id']}").json()["progress"]["contributed"] == _usd(
        "75.00"
    )


def test_idempotent_create(world: World) -> None:
    body = {"name": "Once", "target": _usd("10")}
    headers = {"Idempotency-Key": "goal-once"}
    first = world.owner.post(GOALS, json=body, headers=headers)
    again = world.owner.post(GOALS, json=body, headers=headers)
    assert first.json()["id"] == again.json()["id"]
    assert again.headers.get("Idempotent-Replayed") == "true"
    duplicate = world.owner.post(
        GOALS,
        json={**body, "account_ids": [str(world.savings.id), str(world.savings.id)]},
    )
    assert duplicate.status_code == 422

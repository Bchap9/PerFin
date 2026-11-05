# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manual assets over HTTP: details, kind, valuation history, balance sync, visibility.

The description may hold an address: it is returned to callers who see the
account, but never written to the audit trail or events.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.accounts import service as accounts_service
from app.domain.accounts.models import Account, AccountType
from app.domain.assets import service as assets_service
from app.domain.assets.models import ManualAsset
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

ASSETS = f"{API}/assets"
TODAY = date(2026, 6, 30)
ADDRESS = "12 Placeholder Lane, Exampleville"


@dataclass(frozen=True)
class World:
    owner: AuthedClient
    member: AuthedClient
    viewer: AuthedClient
    household_id: uuid.UUID
    house: Account
    car: Account
    hidden: Account
    checking: Account
    ledger: LedgerFactories


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


@pytest.fixture
def world(actors: Actors, ledger: LedgerFactories, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setattr(assets_service, "today", lambda: TODAY)
    monkeypatch.setattr(accounts_service, "today", lambda: TODAY)
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    member = actors.new(role="member", household=household)
    viewer = actors.new(role="viewer", household=household)
    hid = household.id

    def manual(name: str, kind: str) -> Account:
        return ledger.account(hid, type=AccountType.MANUAL_ASSET, asset_kind=kind, name=name)

    house, car, hidden = manual("House", "property"), manual("Car", "vehicle"), manual("X", "other")
    checking = ledger.account(hid, type=AccountType.CHECKING, name="Checking")
    for account in (house, car, checking):
        ledger.grant(hid, member.record.id, account, GrantLevel.EDIT)
        ledger.grant(hid, viewer.record.id, account, GrantLevel.VIEW)
    return World(owner, member, viewer, hid, house, car, hidden, checking, ledger)


def _usd(amount: str) -> dict[str, str]:
    return {"amount": amount, "currency": "USD"}


def _post(client: AuthedClient, url: str, body: dict[str, Any], status: int = 201) -> Any:
    response = client.post(url, json=body)
    assert response.status_code == status, response.text
    return response.json()


def _balance(client: AuthedClient, account: Account) -> str:
    return str(client.get(f"{API}/accounts/{account.id}").json()["balance"]["amount"])


def test_asset_with_valuation_history(world: World, factories: Factories) -> None:
    asset = _post(
        world.member,
        ASSETS,
        {
            "account_id": str(world.house.id),
            "description": ADDRESS,
            "valuation_method": "appraisal",
            "value": _usd("350000.00"),
            "valued_on": "2025-01-10",
        },
    )
    assert asset["kind"] == "property"
    assert asset["name"] == "House"
    assert asset["description"] == ADDRESS
    assert asset["current_value"]["value"] == _usd("350000.00")
    url = f"{ASSETS}/{asset['id']}/valuations"
    _post(world.member, url, {"valued_on": "2026-01-10", "value": _usd("365000.00")})
    replaced = _post(world.member, url, {"valued_on": "2026-01-10", "value": _usd("366000.00")})
    history = world.viewer.get(url).json()["items"]
    assert [(v["valued_on"], v["value"]["amount"]) for v in history] == [
        ("2026-01-10", "366000.00"),
        ("2025-01-10", "350000.00"),
    ]
    assert (
        world.viewer.get(f"{ASSETS}/{asset['id']}").json()["current_value"]["id"]
        == (replaced["id"])
    )
    # The account's balance follows the valuations.
    assert _balance(world.viewer, world.house) == "366000.00"
    assert world.member.delete(f"{url}/{replaced['id']}").status_code == 204
    assert _balance(world.viewer, world.house) == "350000.00"
    ranged = world.viewer.get(f"{url}?from=2025-06-01").json()["items"]
    assert ranged == []

    # The address never reaches the audit trail or events.
    rows = [
        *household_audit(factories, world.household_id, "asset.create"),
        *household_audit(factories, world.household_id, "asset.valuation_add"),
    ]
    events = household_events(factories, world.household_id, "asset.created")
    assert rows
    assert events
    dumped = json.dumps([[r.before, r.after] for r in rows] + [e.payload for e in events])
    assert "Placeholder Lane" not in dumped
    assert rows[0].after is not None
    assert rows[0].after["has_description"] is True


def test_asset_rules(world: World) -> None:
    wrong = {"account_id": str(world.checking.id)}
    assert problem_type(world.member.post(ASSETS, json=wrong)) == "invalid-asset-account"
    hidden = {"account_id": str(world.hidden.id)}
    assert problem_type(world.member.post(ASSETS, json=hidden)) == "account-not-found"
    assert world.viewer.post(ASSETS, json={"account_id": str(world.car.id)}).status_code == 403
    euro = {"account_id": str(world.car.id), "value": {"amount": "1", "currency": "EUR"}}
    assert problem_type(world.member.post(ASSETS, json=euro)) == "currency-mismatch"
    future = {"account_id": str(world.car.id), "value": _usd("1"), "valued_on": "2026-07-01"}
    assert problem_type(world.member.post(ASSETS, json=future)) == "valuation-in-future"
    asset = _post(world.member, ASSETS, {"account_id": str(world.car.id)})
    again = world.member.post(ASSETS, json={"account_id": str(world.car.id)})
    assert problem_type(again) == "asset-exists"
    url = f"{ASSETS}/{asset['id']}/valuations"
    negative = {"valued_on": "2026-01-01", "value": _usd("-5")}
    assert problem_type(world.member.post(url, json=negative)) == "invalid-valuation"
    assert (
        world.viewer.post(url, json={"valued_on": "2026-01-01", "value": _usd("5")}).status_code
        == 403
    )


def test_update_kind_description_and_delete(world: World, factories: Factories) -> None:
    asset = _post(world.member, ASSETS, {"account_id": str(world.car.id), "description": "Hatch"})
    url = f"{ASSETS}/{asset['id']}"
    changed = world.member.patch(url, json={"kind": "other", "description": None})
    assert changed.status_code == 200, changed.text
    assert (changed.json()["kind"], changed.json()["description"]) == ("other", None)
    account = world.member.get(f"{API}/accounts/{world.car.id}").json()
    assert account["asset_kind"] == "other"
    assert world.member.patch(url, json={"kind": None}).status_code == 422
    assert world.member.patch(url, json={}).status_code == 422
    assert world.member.delete(url).status_code == 204
    assert problem_type(world.member.get(url)) == "asset-not-found"
    with factories.transaction() as db:
        set_context(db, world.household_id)
        assert db.get(ManualAsset, uuid.UUID(asset["id"])) is None
        # The account and its valuations stay.
        assert db.get(Account, world.car.id) is not None


def test_visibility(world: World) -> None:
    hidden = _post(world.owner, ASSETS, {"account_id": str(world.hidden.id), "description": "x"})
    shown = _post(world.owner, ASSETS, {"account_id": str(world.house.id)})
    assert [a["id"] for a in world.member.get(ASSETS).json()["items"]] == [shown["id"]]
    assert [a["id"] for a in world.owner.get(ASSETS).json()["items"]] == [
        hidden["id"],
        shown["id"],
    ]
    for path in ("", "/valuations"):
        assert problem_type(world.viewer.get(f"{ASSETS}/{hidden['id']}{path}")) == (
            "asset-not-found"
        )
    view_only = world.ledger.account(
        world.household_id, type=AccountType.MANUAL_ASSET, asset_kind="other", name="Shared"
    )
    world.ledger.grant(world.household_id, world.member.record.id, view_only, GrantLevel.VIEW)
    shared = _post(world.owner, ASSETS, {"account_id": str(view_only.id)})
    assert world.member.get(f"{ASSETS}/{shared['id']}").status_code == 200
    assert (
        world.member.patch(f"{ASSETS}/{shared['id']}", json={"kind": "vehicle"}).status_code == 403
    )

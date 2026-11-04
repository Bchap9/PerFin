# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/fx-rates``: manual exchange rates and their history over HTTP (synthetic rates)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pytest

from app.core import clock
from app.core.settings import Settings
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, household_audit, household_events, problem_type
from tests.fixtures.ledger import LedgerFactories

pytestmark = pytest.mark.integration

RATES = f"{API}/fx-rates"


@dataclass(frozen=True)
class World:
    owner: AuthedClient
    admin: AuthedClient
    member: AuthedClient
    viewer: AuthedClient
    household_id: uuid.UUID


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


@pytest.fixture
def world(actors: Actors) -> World:
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    return World(
        owner,
        actors.new(role="admin", household=household),
        actors.new(role="member", household=household),
        actors.new(role="viewer", household=household),
        household.id,
    )


def _body(**extra: Any) -> dict[str, Any]:
    return {"base": "EUR", "quote": "USD", "rate_date": "2026-10-01", "rate": "1.0812", **extra}


def test_set_list_and_delete(world: World, factories: Factories) -> None:
    created = world.admin.post(RATES, json=_body())
    assert created.status_code == 201, created.text
    row = created.json()
    assert {k: row[k] for k in ("base", "quote", "rate_date", "rate", "source")} == {
        "base": "EUR",
        "quote": "USD",
        "rate_date": "2026-10-01",
        "rate": "1.0812",
        "source": "manual",
    }
    # Setting the same pair and day replaces the rate (same row).
    again = world.owner.post(RATES, json=_body(rate="1.090000"))
    assert again.status_code == 201
    assert again.json()["id"] == row["id"]
    assert again.json()["rate"] == "1.09"

    listed = world.viewer.get(RATES)
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()["items"]] == [row["id"]]

    audits = household_audit(factories, world.household_id, "fx_rate.set")
    assert len(audits) == 2
    assert audits[1].before is not None
    assert audits[1].after is not None
    assert audits[1].before["rate"] == "1.0812"
    assert audits[1].after["rate"] == "1.09"
    events = household_events(factories, world.household_id, "fx.rate_set")
    assert [e.payload for e in events] == [{"rate_id": row["id"]}] * 2

    gone = world.admin.delete(f"{RATES}/{row['id']}")
    assert gone.status_code == 204
    assert world.viewer.get(RATES).json()["items"] == []
    missing = world.admin.delete(f"{RATES}/{row['id']}")
    assert (missing.status_code, problem_type(missing)) == (404, "fx-rate-not-found")
    assert household_audit(factories, world.household_id, "fx_rate.delete")


def test_members_and_viewers_only_read(world: World) -> None:
    for caller in (world.member, world.viewer):
        response = caller.post(RATES, json=_body())
        assert (response.status_code, problem_type(response)) == (403, "forbidden")
        assert caller.get(RATES).status_code == 200


@pytest.mark.parametrize(
    "update",
    [
        pytest.param({"rate": 1.08}, id="json-number"),
        pytest.param({"rate": "0"}, id="zero"),
        pytest.param({"rate": "-1"}, id="negative"),
        pytest.param({"rate": "1e3"}, id="exponent"),
        pytest.param({"rate": "1.1234567890123"}, id="too-many-decimals"),
        pytest.param({"rate": "1234567890123"}, id="too-large"),
        pytest.param({"quote": "EUR"}, id="same-currency"),
        pytest.param({"base": "XYZ"}, id="unknown-currency"),
        pytest.param({"source": "ecb"}, id="unknown-field"),
    ],
)
def test_invalid_rates_are_refused(world: World, update: dict[str, Any]) -> None:
    response = world.owner.post(RATES, json=_body(**update))
    assert response.status_code == 422, response.text


def test_rate_date_not_in_the_future(world: World) -> None:
    tomorrow = (clock.utcnow().date() + timedelta(days=1)).isoformat()
    response = world.owner.post(RATES, json=_body(rate_date=tomorrow))
    assert (response.status_code, problem_type(response)) == (422, "fx-rate-date-in-future")


def test_manual_entry_replaces_a_fetched_rate(world: World, ledger: LedgerFactories) -> None:
    fetched = ledger.fx_rate(
        world.household_id, base="EUR", quote="USD", rate_date=date(2026, 10, 1), source="ecb"
    )
    response = world.owner.post(RATES, json=_body())
    assert response.json()["id"] == str(fetched.id)
    assert response.json()["source"] == "manual"


def test_history_filters_and_pages(world: World, ledger: LedgerFactories) -> None:
    hid = world.household_id
    start = date(2026, 1, 1)
    for offset in range(5):
        day = start + timedelta(days=offset)
        ledger.fx_rate(hid, base="EUR", quote="USD", rate_date=day, rate=f"1.{offset}")
        ledger.fx_rate(hid, base="GBP", quote="USD", rate_date=day, rate=f"1.2{offset}")

    page = world.viewer.get(RATES, params={"limit": 3}).json()
    keys = [(r["rate_date"], r["base"]) for r in page["items"]]
    assert keys == [("2026-01-05", "EUR"), ("2026-01-05", "GBP"), ("2026-01-04", "EUR")]
    rest = world.viewer.get(RATES, params={"limit": 100, "cursor": page["next_cursor"]}).json()
    assert len(rest["items"]) == 7
    assert rest["next_cursor"] is None
    assert not {r["id"] for r in page["items"]} & {r["id"] for r in rest["items"]}

    gbp = world.viewer.get(
        RATES, params={"base": "gbp", "quote": "USD", "from": "2026-01-02", "to": "2026-01-03"}
    ).json()
    assert [(r["rate_date"], r["rate"]) for r in gbp["items"]] == [
        ("2026-01-03", "1.22"),
        ("2026-01-02", "1.21"),
    ]
    bad = world.viewer.get(RATES, params={"from": "2026-02-01", "to": "2026-01-01"})
    assert (bad.status_code, problem_type(bad)) == (422, "invalid-range")
    cursor = world.viewer.get(RATES, params={"cursor": str(uuid.uuid4())})
    assert (cursor.status_code, problem_type(cursor)) == (422, "invalid-cursor")


def test_other_households_rates_are_invisible(
    world: World, actors: Actors, ledger: LedgerFactories
) -> None:
    other = actors.new(role="owner")
    assert other.record.household is not None
    theirs = ledger.fx_rate(other.record.household.id, rate_date=date(2026, 3, 3))
    assert world.owner.get(RATES).json()["items"] == []
    response = world.owner.delete(f"{RATES}/{theirs.id}")
    assert (response.status_code, problem_type(response)) == (404, "fx-rate-not-found")

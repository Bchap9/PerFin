# SPDX-License-Identifier: AGPL-3.0-or-later
"""Accounts and balances over HTTP (API.md ``/accounts``).

Covers behaviour (create, change, close/reopen, delete, balances, paging,
audit and events), account-level visibility for every role (grants), the
cross-household ``404`` and mass assignment. Route-level guards for every
principal are in ``tests/security/test_authz_matrix.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.models import GrantLevel
from app.core import clock
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.accounts.models import Account, BalanceSnapshot
from app.domain.transactions.models import Transaction
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

ACCOUNTS = f"{API}/accounts"


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


def _household_id(client: AuthedClient) -> uuid.UUID:
    household = client.record.household
    assert household is not None
    return household.id


def _create(client: AuthedClient, **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"type": "checking", "name": "Everyday", **fields}
    response = client.post(ACCOUNTS, json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def _account_row(factories: Factories, household_id: uuid.UUID, account_id: str) -> Account | None:
    with factories.transaction() as db:
        set_context(db, household_id)
        row = db.get(Account, uuid.UUID(account_id))
        if row is not None:
            db.expunge(row)
        return row


# ---- create / read / change ----------------------------------------------------------


def test_create_account_defaults_and_opening_balance(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    household_id = _household_id(owner)
    created = _create(
        owner,
        type="credit_card",
        name="  Card  ",
        number_last4="4321",
        opening_balance={"amount": "-120.50", "currency": "USD"},
        opening_balance_date="2026-01-10",
    )
    assert created["name"] == "Card"
    assert created["currency"] == "USD"  # household base currency
    assert created["is_asset"] is False  # a card is a liability by default
    assert created["manual_balance"] is False
    assert created["number_last4"] == "4321"
    assert created["can_edit"] is True
    assert created["balance"] == {"amount": "-120.50", "currency": "USD"}
    assert created["is_closed"] is False

    got = owner.get(f"{ACCOUNTS}/{created['id']}")
    assert got.status_code == 200
    assert got.json() == created

    audit = household_audit(factories, household_id, "account.create")
    assert len(audit) == 1
    serialized = json.dumps(audit[0].after)
    assert "4321" not in serialized  # identity data never reaches the audit trail (H8)
    assert audit[0].after is not None
    assert audit[0].after["has_number_last4"] is True
    events = household_events(factories, household_id, "account.created")
    assert [e.payload for e in events] == [{"account_id": created["id"]}]
    assert "4321" not in json.dumps([e.payload for e in events])


def test_create_uses_household_base_currency(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household = factories.household(currency="EUR")
    other_owner = actors.new(role="owner", household=household)
    created = _create(other_owner)
    assert created["currency"] == "EUR"
    assert _create(owner, currency="jpy")["currency"] == "JPY"


def test_create_validation(actors: Actors) -> None:
    owner = actors.new(role="owner")
    cases: list[dict[str, Any]] = [
        {"type": "manual_asset", "name": "House"},  # asset_kind missing
        {"type": "checking", "name": "X", "asset_kind": "vehicle"},
        {"type": "checking", "name": "X", "number_last4": "12a4"},
        {"type": "checking", "name": "X", "currency": "XXX"},
        {"type": "checking", "name": "  "},
        {"type": "checking", "name": "X", "opening_balance": {"amount": 12.5, "currency": "USD"}},
        {"type": "checking", "name": "X", "opening_balance_date": "2026-01-01"},
        {"type": "savings_bond", "name": "X"},
    ]
    for body in cases:
        response = owner.post(ACCOUNTS, json=body)
        assert response.status_code == 422, (body, response.text)
    mismatch = owner.post(
        ACCOUNTS,
        json={
            "type": "checking",
            "name": "X",
            "currency": "USD",
            "opening_balance": {"amount": "1", "currency": "EUR"},
        },
    )
    assert (mismatch.status_code, problem_type(mismatch)) == (422, "currency-mismatch")
    unknown = owner.post(
        ACCOUNTS, json={"type": "checking", "name": "X", "institution_id": str(uuid.uuid4())}
    )
    assert (unknown.status_code, problem_type(unknown)) == (404, "institution-not-found")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("household_id", str(uuid.uuid4())),
        ("id", str(uuid.uuid4())),
        ("closed_on", "2026-01-01"),
        ("created_at", "2026-01-01T00:00:00Z"),
        ("balance", {"amount": "1", "currency": "USD"}),
        ("can_edit", True),
    ],
)
def test_mass_assignment_is_refused(actors: Actors, field: str, value: Any) -> None:
    owner = actors.new(role="owner")
    response = owner.post(ACCOUNTS, json={"type": "checking", "name": "X", field: value})
    assert response.status_code == 422, response.text
    created = _create(owner)
    patched = owner.patch(f"{ACCOUNTS}/{created['id']}", json={field: value})
    assert patched.status_code == 422, patched.text


def test_update_account(actors: Actors, factories: Factories, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    household_id = _household_id(owner)
    institution = ledger.institution(household_id)
    created = _create(owner, nickname="Main", number_last4="1111")
    url = f"{ACCOUNTS}/{created['id']}"
    response = owner.patch(
        url,
        json={
            "name": "Joint",
            "nickname": None,
            "number_last4": "2222",
            "institution_id": str(institution.id),
            "include_in_net_worth": False,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["name"], body["nickname"], body["number_last4"]) == ("Joint", None, "2222")
    assert body["institution_id"] == str(institution.id)
    assert body["include_in_net_worth"] is False
    audit = household_audit(factories, household_id, "account.update")
    assert len(audit) == 1
    assert audit[0].after is not None
    assert audit[0].after["number_last4"] == "changed"
    assert "2222" not in json.dumps(audit[0].after)
    assert "1111" not in json.dumps(audit[0].before)

    # Type change resets the type defaults; manual_asset needs a kind.
    refused = owner.patch(url, json={"type": "manual_asset"})
    assert (refused.status_code, problem_type(refused)) == (422, "invalid-asset-kind")
    house = owner.patch(url, json={"type": "manual_asset", "asset_kind": "property"})
    assert house.status_code == 200
    assert (house.json()["is_asset"], house.json()["manual_balance"]) == (True, True)
    loan = owner.patch(url, json={"type": "mortgage"})
    assert loan.json()["asset_kind"] is None
    assert loan.json()["is_asset"] is False

    for body_ in ({}, {"name": None}, {"type": None}, {"currency": None}):
        assert owner.patch(url, json=body_).status_code == 422, body_


def test_currency_is_locked_once_used(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    created = _create(owner)
    url = f"{ACCOUNTS}/{created['id']}"
    assert owner.patch(url, json={"currency": "EUR"}).json()["currency"] == "EUR"
    account = _account_row(factories, _household_id(owner), created["id"])
    assert account is not None
    assert account.currency == "EUR"
    ledger.transaction(account, amount="-1.00")
    locked = owner.patch(url, json={"currency": "USD"})
    assert (locked.status_code, problem_type(locked)) == (409, "account-currency-locked")


def test_close_and_reopen(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    household_id = _household_id(owner)
    created = _create(owner, opened_on="2025-06-01")
    url = f"{ACCOUNTS}/{created['id']}"
    future = (clock.utcnow().date() + timedelta(days=2)).isoformat()
    assert problem_type(owner.post(f"{url}/close", json={"closed_on": future})) == (
        "invalid-account-dates"
    )
    early = owner.post(f"{url}/close", json={"closed_on": "2025-01-01"})
    assert problem_type(early) == "invalid-account-dates"
    closed = owner.post(f"{url}/close", json={"closed_on": "2026-01-31"})
    assert closed.status_code == 200
    assert (closed.json()["closed_on"], closed.json()["is_closed"]) == ("2026-01-31", True)
    again = owner.post(f"{url}/close", json={})
    assert (again.status_code, problem_type(again)) == (409, "account-closed")
    listing = owner.get(ACCOUNTS, params={"closed": "false"}).json()["items"]
    assert created["id"] not in [a["id"] for a in listing]
    assert [a["id"] for a in owner.get(ACCOUNTS, params={"closed": "true"}).json()["items"]] == [
        created["id"]
    ]
    reopened = owner.post(f"{url}/reopen")
    assert reopened.status_code == 200
    assert reopened.json()["closed_on"] is None
    not_closed = owner.post(f"{url}/reopen")
    assert (not_closed.status_code, problem_type(not_closed)) == (409, "account-not-closed")
    # Default closing date is today.
    today_close = owner.post(f"{url}/close")
    assert today_close.json()["closed_on"] == clock.utcnow().date().isoformat()
    assert len(household_audit(factories, household_id, "account.close")) == 2
    assert len(household_events(factories, household_id, "account.reopened")) == 1


def test_delete_account_cascades(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _household_id(owner)
    account = ledger.account(household_id)
    ledger.transaction(account)
    ledger.snapshot(account)
    response = owner.delete(f"{ACCOUNTS}/{account.id}")
    assert response.status_code == 204
    assert _account_row(factories, household_id, str(account.id)) is None
    with factories.transaction() as db:
        set_context(db, household_id)
        assert (
            db.execute(select(Transaction).where(Transaction.account_id == account.id)).all() == []
        )
    assert owner.get(f"{ACCOUNTS}/{account.id}").status_code == 404
    assert len(household_audit(factories, household_id, "account.delete")) == 1
    assert len(household_events(factories, household_id, "account.deleted")) == 1


def test_listing_pages_with_cursor(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    household_id = _household_id(owner)
    ids = [str(ledger.account(household_id).id) for _ in range(5)]
    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = owner.get(ACCOUNTS, params=params).json()
        seen.extend(a["id"] for a in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == ids
    assert owner.get(ACCOUNTS, params={"limit": 501}).status_code == 422
    assert owner.get(ACCOUNTS, params={"limit": 0}).status_code == 422
    savings = ledger.account(household_id, type="savings")
    only = owner.get(ACCOUNTS, params={"type": "savings"}).json()["items"]
    assert [a["id"] for a in only] == [str(savings.id)]


# ---- balances ----------------------------------------------------------------------


def test_balance_history_and_manual_balance(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    household_id = _household_id(owner)
    account = ledger.account(household_id)
    ledger.snapshot(account, as_of_date=date(2026, 1, 1), balance="100.00")
    ledger.transaction(account, amount="-10.00", posted_date=date(2026, 1, 2))
    url = f"{ACCOUNTS}/{account.id}/balances"
    history = owner.get(url, params={"start": "2026-01-01", "end": "2026-01-03"})
    assert history.status_code == 200, history.text
    body = history.json()
    assert [(p["date"], p["balance"]["amount"]) for p in body["items"]] == [
        ("2026-01-01", "100.00"),
        ("2026-01-02", "90.00"),
        ("2026-01-03", "90.00"),
    ]
    assert [s["source"] for s in body["snapshots"]] == ["manual"]

    recorded = owner.post(
        url, json={"as_of_date": "2026-01-03", "balance": {"amount": "95.5", "currency": "USD"}}
    )
    assert recorded.status_code == 201, recorded.text
    assert recorded.json()["balance"] == {"amount": "95.50", "currency": "USD"}
    after = owner.get(url, params={"start": "2026-01-03", "end": "2026-01-04"}).json()
    assert [p["balance"]["amount"] for p in after["items"]] == ["95.50", "95.50"]
    # Same date again replaces the manual balance.
    owner.post(
        url, json={"as_of_date": "2026-01-03", "balance": {"amount": "96", "currency": "USD"}}
    )
    with ledger.session_factory() as db, db.begin():
        set_context(db, household_id)
        rows = db.execute(
            select(BalanceSnapshot.balance).where(
                BalanceSnapshot.account_id == account.id,
                BalanceSnapshot.as_of_date == date(2026, 1, 3),
            )
        ).all()
    assert len(rows) == 1


def test_balance_validation(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    account = ledger.account(_household_id(owner))
    url = f"{ACCOUNTS}/{account.id}/balances"
    wrong = owner.post(
        url, json={"as_of_date": "2026-01-01", "balance": {"amount": "1", "currency": "EUR"}}
    )
    assert (wrong.status_code, problem_type(wrong)) == (422, "currency-mismatch")
    future = (clock.utcnow().date() + timedelta(days=3)).isoformat()
    ahead = owner.post(
        url, json={"as_of_date": future, "balance": {"amount": "1", "currency": "USD"}}
    )
    assert problem_type(ahead) == "balance-in-future"
    too_long = owner.get(url, params={"start": "2000-01-01", "end": "2026-01-01"})
    assert (too_long.status_code, problem_type(too_long)) == (422, "range-too-long")
    backwards = owner.get(url, params={"start": "2026-02-01", "end": "2026-01-01"})
    assert problem_type(backwards) == "invalid-range"
    default = owner.get(url).json()
    assert len(default["items"]) == 90
    assert default["end"] == clock.utcnow().date().isoformat()


# ---- visibility (grants) -----------------------------------------------------------


def test_visibility_follows_role_and_grants(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    admin = actors.new(role="admin", household=household)
    member = actors.new(role="member", household=household)
    viewer = actors.new(role="viewer", household=household)
    shared = ledger.account(household.id, name="Shared")
    viewed = ledger.account(household.id, name="Viewed")
    hidden = ledger.account(household.id, name="Hidden")
    ledger.grant(household.id, member.user.id, shared, GrantLevel.EDIT)
    ledger.grant(household.id, member.user.id, viewed, GrantLevel.VIEW)
    ledger.grant(household.id, member.user.id, hidden, GrantLevel.NONE)
    ledger.grant(household.id, viewer.user.id, shared, GrantLevel.VIEW)

    def listed(client: AuthedClient) -> dict[str, bool]:
        items = client.get(ACCOUNTS).json()["items"]
        return {a["name"]: a["can_edit"] for a in items}

    everything = {"Shared": True, "Viewed": True, "Hidden": True}
    assert listed(owner) == everything
    assert listed(admin) == everything
    assert listed(member) == {"Shared": True, "Viewed": False}
    assert listed(viewer) == {"Shared": False}

    # Hidden accounts are 404 on every account route, like another household's.
    # (The member's hidden account has a grant of level "none".)
    for client, account in ((member, hidden), (viewer, viewed), (viewer, hidden)):
        url = f"{ACCOUNTS}/{account.id}"
        for method, path, body in (
            ("GET", url, None),
            ("GET", f"{url}/balances", None),
            ("PATCH", url, {"name": "Mine"}),
            ("POST", f"{url}/close", {}),
            ("POST", f"{url}/reopen", None),
            (
                "POST",
                f"{url}/balances",
                {"as_of_date": "2026-01-01", "balance": {"amount": "1", "currency": "USD"}},
            ),
        ):
            response = client.request(method, path, json=body)
            # A viewer's writes stop at the role check (403 for any id, so
            # nothing is revealed); everything else is the hidden 404.
            wanted = (
                (403, "forbidden")
                if client is viewer and method != "GET"
                else (404, "account-not-found")
            )
            assert (response.status_code, problem_type(response)) == wanted, (
                client.record.role,
                method,
                path,
            )

    # A view grant reads but does not change; edit changes.
    assert member.get(f"{ACCOUNTS}/{viewed.id}").status_code == 200
    read_only = member.patch(f"{ACCOUNTS}/{viewed.id}", json={"name": "Mine"})
    assert (read_only.status_code, problem_type(read_only)) == (403, "forbidden")
    assert member.patch(f"{ACCOUNTS}/{shared.id}", json={"name": "Ours"}).status_code == 200
    # Viewers never write, whatever the grant.
    viewer_patch = viewer.patch(f"{ACCOUNTS}/{shared.id}", json={"name": "X"})
    assert (viewer_patch.status_code, problem_type(viewer_patch)) == (403, "forbidden")
    assert viewer.get(f"{ACCOUNTS}/{shared.id}/balances").status_code == 200
    # Members do not delete accounts.
    assert member.delete(f"{ACCOUNTS}/{shared.id}").status_code == 403


def test_member_keeps_access_to_created_account(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    member = actors.new(role="member", household=household)
    viewer = actors.new(role="viewer", household=household)
    created = _create(member, name="Pocket money")
    assert created["can_edit"] is True
    assert member.get(f"{ACCOUNTS}/{created['id']}").status_code == 200
    assert [a["id"] for a in member.get(ACCOUNTS).json()["items"]] == [created["id"]]
    # Other members and viewers still need a grant.
    other = actors.new(role="member", household=household)
    assert other.get(f"{ACCOUNTS}/{created['id']}").status_code == 404
    assert viewer.post(ACCOUNTS, json={"type": "cash", "name": "X"}).status_code == 403


def test_tokens_respect_scopes_and_grants(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    member = actors.new(role="member", household=household)
    granted = ledger.account(household.id)
    hidden = ledger.account(household.id)
    ledger.grant(household.id, member.user.id, granted, GrantLevel.VIEW)
    raw = factories.token(member.record, scopes=("accounts:read",))
    client = actors.anonymous()
    bearer = {"Authorization": f"Bearer {raw}"}
    items = client.get(ACCOUNTS, headers=bearer).json()["items"]
    assert [a["id"] for a in items] == [str(granted.id)]
    assert client.get(f"{ACCOUNTS}/{hidden.id}", headers=bearer).status_code == 404
    refused = client.post(ACCOUNTS, headers=bearer, json={"type": "cash", "name": "X"})
    assert (refused.status_code, problem_type(refused)) == (403, "insufficient-scope")


def test_other_household_gets_404_and_changes_nothing(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    victim = actors.new(role="owner")
    household_id = _household_id(victim)
    account = ledger.account(household_id, name="Victim")
    intruder = actors.new(role="owner")
    url = f"{ACCOUNTS}/{account.id}"
    for method, path, body in (
        ("GET", url, None),
        ("PATCH", url, {"name": "Stolen"}),
        ("DELETE", url, None),
        ("POST", f"{url}/close", {}),
        ("GET", f"{url}/balances", None),
        (
            "POST",
            f"{url}/balances",
            {"as_of_date": "2026-01-01", "balance": {"amount": "1", "currency": "USD"}},
        ),
    ):
        response = intruder.request(method, path, json=body)
        assert (response.status_code, problem_type(response)) == (404, "account-not-found"), (
            method,
            path,
        )
    assert intruder.get(ACCOUNTS).json()["items"] == []
    row = _account_row(factories, household_id, str(account.id))
    assert row is not None
    assert row.name == "Victim"
    assert row.closed_on is None


# ---- Idempotency-Key ------------------------------------------------------------------


def test_idempotency_key_replays_the_first_answer(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    household_id = _household_id(owner)
    body = {"type": "savings", "name": "Rainy day"}
    key = {"Idempotency-Key": str(uuid.uuid4())}
    first = owner.post(ACCOUNTS, json=body, headers=key)
    second = owner.post(ACCOUNTS, json=body, headers=key)
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert [a["id"] for a in owner.get(ACCOUNTS).json()["items"]] == [first.json()["id"]]
    assert len(household_audit(factories, household_id, "account.create")) == 1

    reused = owner.post(ACCOUNTS, json={**body, "name": "Other"}, headers=key)
    assert (reused.status_code, problem_type(reused)) == (422, "idempotency-key-reused")
    other_route = owner.post(f"{API}/tags", json={"name": "x"}, headers=key)
    assert problem_type(other_route) == "idempotency-key-reused"

    # Another user of the same household has their own key space.
    admin = actors.new(role="admin", household=owner.record.household)
    theirs = admin.post(ACCOUNTS, json=body, headers=key)
    assert theirs.status_code == 201
    assert theirs.json()["id"] != first.json()["id"]
    assert "Idempotent-Replayed" not in theirs.headers


def test_failed_request_does_not_burn_the_key(actors: Actors) -> None:
    owner = actors.new(role="owner")
    key = {"Idempotency-Key": "retry-me-1"}
    bad = {"type": "checking", "name": "X", "institution_id": str(uuid.uuid4())}
    assert owner.post(ACCOUNTS, json=bad, headers=key).status_code == 404
    # Nothing was stored for the key: a corrected request with it runs.
    good = owner.post(ACCOUNTS, json={"type": "checking", "name": "X"}, headers=key)
    assert good.status_code == 201
    assert "Idempotent-Replayed" not in good.headers


@pytest.mark.parametrize("key", [b"", b"a" * 256, b"has space", "café".encode()])
def test_invalid_idempotency_keys_are_refused(actors: Actors, key: bytes) -> None:
    owner = actors.new(role="owner")
    response = owner.post(
        ACCOUNTS,
        json={"type": "checking", "name": "X"},
        headers={"Idempotency-Key": key},
    )
    assert (response.status_code, problem_type(response)) == (422, "invalid-idempotency-key")
    assert owner.get(ACCOUNTS).json()["items"] == []


def test_idempotent_manual_balance(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    household_id = _household_id(owner)
    first_account = ledger.account(household_id)
    second_account = ledger.account(household_id)
    body = {"as_of_date": "2026-01-01", "balance": {"amount": "5", "currency": "USD"}}
    key = {"Idempotency-Key": "balance-1"}
    one = owner.post(f"{ACCOUNTS}/{first_account.id}/balances", json=body, headers=key)
    two = owner.post(f"{ACCOUNTS}/{first_account.id}/balances", json=body, headers=key)
    assert two.json() == one.json()
    assert two.headers.get("Idempotent-Replayed") == "true"
    # The same key and body on another account is another request.
    other = owner.post(f"{ACCOUNTS}/{second_account.id}/balances", json=body, headers=key)
    assert problem_type(other) == "idempotency-key-reused"

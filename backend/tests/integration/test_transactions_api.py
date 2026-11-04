# SPDX-License-Identifier: AGPL-3.0-or-later
"""The transactions API end to end (API.md "Transactions", SECURITY.md F3/F4/F8).

Each test builds its own household through the committing factories:

* ``owner`` (sees every account), ``member`` and ``viewer``;
* account ``edit_acct`` (member ``edit``, viewer ``view``), ``view_acct``
  (member ``view``) and ``hidden_acct`` (no grants);
* an ``outsider``: the owner of another household with an account of their own.

All data is synthetic.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.db.models import Setting, SettingScope
from app.db.rls import set_context
from app.domain.accounts.models import Account
from app.domain.transactions import service
from app.domain.transactions.models import (
    ReviewState,
    Transaction,
    TransactionSplit,
    TransactionStatus,
    TransactionTag,
)
from tests.fixtures.factories import AuthedClient, Factories, UserRecord
from tests.fixtures.households import (
    API,
    Actors,
    household_audit,
    household_events,
    problem_type,
)
from tests.fixtures.ledger import LedgerFactories

pytestmark = pytest.mark.integration

TXN = f"{API}/transactions"


@dataclass
class World:
    actors: Actors
    owner: AuthedClient
    member: AuthedClient
    viewer: AuthedClient
    outsider: AuthedClient
    household_id: uuid.UUID
    edit_acct: Account
    view_acct: Account
    hidden_acct: Account
    foreign_acct: Account
    ledger: LedgerFactories
    factories: Factories


@pytest.fixture
def world(settings: Settings, factories: Factories, ledger: LedgerFactories) -> Iterator[World]:
    with Actors.open(settings, factories) as actors:
        owner_rec = actors.user(role="owner")
        household = owner_rec.household
        assert household is not None
        member_rec = actors.user(role="member", household=household)
        viewer_rec = actors.user(role="viewer", household=household)
        outsider_rec = actors.user(role="owner")
        assert outsider_rec.household_id is not None
        edit_acct = ledger.account(household.id, name="Edit account")
        view_acct = ledger.account(household.id, name="View account")
        hidden_acct = ledger.account(household.id, name="Hidden account")
        foreign_acct = ledger.account(outsider_rec.household_id, name="Foreign account")
        ledger.grant(household.id, member_rec.id, edit_acct, GrantLevel.EDIT)
        ledger.grant(household.id, member_rec.id, view_acct, GrantLevel.VIEW)
        ledger.grant(household.id, viewer_rec.id, edit_acct, GrantLevel.VIEW)
        yield World(
            actors=actors,
            owner=actors.login(owner_rec),
            member=actors.login(member_rec),
            viewer=actors.login(viewer_rec),
            outsider=actors.login(outsider_rec),
            household_id=household.id,
            edit_acct=edit_acct,
            view_acct=view_acct,
            hidden_acct=hidden_acct,
            foreign_acct=foreign_acct,
            ledger=ledger,
            factories=factories,
        )


def _ids(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def _create_body(account: Account, amount: str = "-12.50", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "account_id": str(account.id),
        "posted_date": "2026-02-03",
        "amount": {"amount": amount, "currency": account.currency},
        "raw_payee": "Synthetic Grocer",
    }
    body.update(extra)
    return body


def _count(factories: Factories, household_id: uuid.UUID, model: Any) -> int:
    with factories.transaction() as db:
        set_context(db, household_id)
        return int(
            db.execute(
                select(func.count()).select_from(model).where(model.household_id == household_id)
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# Listing, filters, pagination
# ---------------------------------------------------------------------------


def test_list_is_newest_first_and_paginates_stably(world: World) -> None:
    ledger, acct = world.ledger, world.edit_acct
    rows = [
        ledger.transaction(acct, amount=f"-{i}.00", posted_date=date(2026, 1, 1 + i % 5))
        for i in range(1, 13)
    ]
    expected = sorted(rows, key=lambda r: (r.posted_date, r.id), reverse=True)
    first = world.owner.get(TXN, params={"limit": 5})
    assert _ids(first) == [str(r.id) for r in expected[:5]]
    cursor = first.json()["next_cursor"]
    assert cursor

    # Rows inserted (newer, older and on the boundary date) and one deleted
    # while paging do not shift the remaining pages.
    ledger.transaction(acct, amount="-99.00", posted_date=date(2026, 3, 1))
    ledger.transaction(acct, amount="-98.00", posted_date=expected[4].posted_date)
    late_old = ledger.transaction(acct, amount="-97.00", posted_date=date(2025, 12, 1))
    seen = _ids(first)
    while cursor:
        page = world.owner.get(TXN, params={"limit": 5, "cursor": cursor})
        seen += _ids(page)
        cursor = page.json()["next_cursor"]
    originals = [str(r.id) for r in expected]
    assert [i for i in seen if i in originals] == originals
    assert len(seen) == len(set(seen))
    assert str(late_old.id) in seen  # older than every page boundary


def test_limit_and_cursor_are_validated(world: World) -> None:
    assert world.owner.get(TXN, params={"limit": 501}).status_code == 422
    assert world.owner.get(TXN, params={"limit": 0}).status_code == 422
    for bad in ("not-a-cursor!", "AAAA", "x" * 200):
        response = world.owner.get(TXN, params={"cursor": bad})
        assert response.status_code == 422
    response = world.owner.get(TXN, params={"cursor": "AAAA"})
    assert problem_type(response) == "invalid-cursor"
    assert world.owner.get(TXN, params={"limit": 500}).status_code == 200


def test_filters(world: World) -> None:
    ledger, hh = world.ledger, world.household_id
    acct, other = world.edit_acct, world.view_acct
    groceries = ledger.category(hh, name="Groceries")
    fuel = ledger.category(hh, name="Fuel")
    payee = ledger.payee(hh, name="Corner Market")
    tag = ledger.tag(hh, name="trip")
    a = ledger.transaction(
        acct, amount="-10.00", posted_date=date(2026, 1, 5), category=groceries, payee=payee
    )
    b = ledger.transaction(
        acct,
        amount="-20.00",
        posted_date=date(2026, 1, 10),
        raw_payee="Fuel 100% Station_7",
        memo="road trip",
        review_state=ReviewState.PENDING,
        status=TransactionStatus.PENDING,
    )
    c = ledger.transaction(other, amount="50.00", posted_date=date(2026, 1, 20), raw_payee="Salary")
    d = ledger.transaction(acct, amount="-30.00", posted_date=date(2026, 1, 25))
    ledger.split(d, amount="-10.00", category=fuel)
    ledger.split(d, amount="-20.00", category=groceries)
    ledger.run(_tag, b, tag)

    def ids(**params: Any) -> set[str]:
        return set(_ids(world.owner.get(TXN, params=params)))

    assert ids(account=str(other.id)) == {str(c.id)}
    assert ids(account=[str(acct.id), str(other.id)]) == {str(x.id) for x in (a, b, c, d)}
    assert ids(date_from="2026-01-06", date_to="2026-01-20") == {str(b.id), str(c.id)}
    assert ids(category=str(groceries.id)) == {str(a.id), str(d.id)}  # splits count
    assert ids(category=str(fuel.id)) == {str(d.id)}
    assert ids(uncategorized="true") == {str(b.id), str(c.id)}
    assert ids(uncategorized="false") == {str(a.id), str(d.id)}
    assert ids(payee=str(payee.id)) == {str(a.id)}
    assert ids(tag=str(tag.id)) == {str(b.id)}
    assert ids(review_state="pending") == {str(b.id)}
    assert ids(review_state=["none", "confirmed"]) == {str(a.id), str(c.id), str(d.id)}
    assert ids(status="pending") == {str(b.id)}
    # q: case-insensitive substring over payee text, payee name, memo, notes.
    assert ids(q="corner") == {str(a.id)}  # the payee's name
    assert ids(q="ROAD") == {str(b.id)}
    assert ids(q="salary") == {str(c.id)}
    # Wildcards are literal characters, never patterns.
    assert ids(q="100%") == {str(b.id)}
    assert ids(q="%") == {str(b.id)}
    assert ids(q="_") == {str(b.id)}
    assert ids(q="n_7") == {str(b.id)}
    assert ids(q="F%l") == set()
    assert ids(q="\\") == set()
    assert world.owner.get(TXN, params={"q": "x" * 101}).status_code == 422
    assert world.owner.get(TXN, params={"q": ""}).status_code == 422
    assert world.owner.get(TXN, params={"review_state": "bogus"}).status_code == 422
    too_many = [str(uuid.uuid4()) for _ in range(51)]
    assert world.owner.get(TXN, params={"account": too_many}).status_code == 422


def _tag(db: Any, transaction: Transaction, tag: Any) -> None:
    from tests.fixtures.ledger import tag_transaction

    tag_transaction(db, transaction, tag)


def test_list_respects_account_grants(world: World) -> None:
    ledger = world.ledger
    on_edit = ledger.transaction(world.edit_acct)
    on_view = ledger.transaction(world.view_acct)
    on_hidden = ledger.transaction(world.hidden_acct)
    ledger.transaction(world.foreign_acct)
    assert set(_ids(world.owner.get(TXN))) == {str(on_edit.id), str(on_view.id), str(on_hidden.id)}
    assert set(_ids(world.member.get(TXN))) == {str(on_edit.id), str(on_view.id)}
    assert set(_ids(world.viewer.get(TXN))) == {str(on_edit.id)}
    # Filtering by an account the caller cannot see, or another household's, is 404.
    for acct in (world.hidden_acct, world.foreign_acct):
        response = world.member.get(TXN, params={"account": str(acct.id)})
        assert (response.status_code, problem_type(response)) == (404, "account-not-found")
    response = world.outsider.get(TXN, params={"account": str(world.edit_acct.id)})
    assert (response.status_code, problem_type(response)) == (404, "account-not-found")
    assert str(on_edit.id) not in _ids(world.outsider.get(TXN))


# ---------------------------------------------------------------------------
# One transaction: visibility and cross-household 404
# ---------------------------------------------------------------------------


def test_get_and_cross_household_404(world: World) -> None:
    ledger = world.ledger
    row = ledger.transaction(world.edit_acct, amount="-7.25", raw_payee="Bakery")
    hidden = ledger.transaction(world.hidden_acct)
    body = world.member.get(f"{TXN}/{row.id}").json()
    assert body["amount"] == {"amount": "-7.25", "currency": "USD"}
    assert body["raw_payee"] == "Bakery"
    assert body["transfer"] is None
    assert body["splits"] == []
    assert body["tag_ids"] == []
    assert "import_hash" not in body

    for client, target in (
        (world.member, hidden),
        (world.viewer, hidden),
        (world.outsider, row),
        (world.outsider, hidden),
    ):
        url = f"{TXN}/{target.id}"
        requests = [
            client.get(url),
            client.patch(url, json={"memo": "x"}),
            client.delete(url),
            client.post(f"{url}/split", json={"splits": []}),
            client.post(f"{url}/transfer", json={"counterpart_id": str(row.id)}),
            client.delete(f"{url}/transfer"),
        ]
        if client is not world.viewer:
            requests.append(client.get(f"{url}/transfer-candidates"))
        for response in requests:
            if client is world.viewer and response.request.method != "GET":
                assert response.status_code == 403, response.text  # viewers never write
                continue
            assert (response.status_code, problem_type(response)) == (
                404,
                "transaction-not-found",
            ), response.text
    # Nothing changed.
    assert world.owner.get(f"{TXN}/{hidden.id}").json()["memo"] is None
    assert world.owner.get(f"{TXN}/{row.id}").status_code == 200
    assert world.owner.get(f"{TXN}/{uuid.uuid4()}").status_code == 404


def test_view_grant_reads_but_does_not_write(world: World) -> None:
    row = world.ledger.transaction(world.view_acct)
    url = f"{TXN}/{row.id}"
    assert world.member.get(url).status_code == 200
    for response in (
        world.member.patch(url, json={"memo": "nope"}),
        world.member.delete(url),
        world.member.post(f"{url}/split", json={"splits": []}),
    ):
        assert (response.status_code, problem_type(response)) == (404, "transaction-not-found")
    response = world.member.post(TXN, json=_create_body(world.view_acct))
    assert (response.status_code, problem_type(response)) == (404, "account-not-found")
    response = world.viewer.post(TXN, json=_create_body(world.edit_acct))
    assert (response.status_code, problem_type(response)) == (403, "forbidden")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_manual_transaction(world: World) -> None:
    hh = world.household_id
    category = world.ledger.category(hh)
    tag = world.ledger.tag(hh)
    response = world.member.post(
        TXN,
        json=_create_body(
            world.edit_acct,
            amount="-12.5",
            category_id=str(category.id),
            tag_ids=[str(tag.id)],
            memo="weekly shop",
        ),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["amount"] == {"amount": "-12.50", "currency": "USD"}
    assert body["category_id"] == str(category.id)
    assert body["tag_ids"] == [str(tag.id)]
    assert body["review_state"] == "none"
    assert body["status"] == "cleared"
    rows = household_audit(world.factories, hh, "transaction.create")
    assert [r.target_id for r in rows] == [body["id"]]
    assert rows[0].after is not None
    assert rows[0].after["amount"] == "-12.50"
    assert rows[0].after["free_fields_set"] == ["memo", "raw_payee"]
    assert "weekly shop" not in str(rows[0].after)
    assert "Synthetic Grocer" not in str(rows[0].after)
    events = household_events(world.factories, hh, "transaction.created")
    assert [e.payload["id"] for e in events] == [body["id"]]


@pytest.mark.parametrize(
    ("change", "status", "ptype"),
    [
        ({"amount": {"amount": "-1.00", "currency": "EUR"}}, 422, "currency-mismatch"),
        ({"amount": {"amount": -1.0, "currency": "USD"}}, 422, "validation-failed"),
        ({"amount": {"amount": "-1.0000001", "currency": "USD"}}, 422, "validation-failed"),
        ({"category_id": "FOREIGN_CATEGORY"}, 404, "category-not-found"),
        ({"payee_id": "FOREIGN_PAYEE"}, 404, "payee-not-found"),
        ({"tag_ids": ["FOREIGN_TAG"]}, 404, "tag-not-found"),
        ({"account_id": "FOREIGN_ACCOUNT"}, 404, "account-not-found"),
        ({"account_id": "HIDDEN_ACCOUNT"}, 404, "account-not-found"),
        ({"review_state": "confirmed"}, 422, "validation-failed"),
        ({"household_id": "anything"}, 422, "validation-failed"),
        ({"memo": "bad\x00byte"}, 422, "validation-failed"),
        ({"raw_payee": "two\nlines"}, 422, "validation-failed"),
        ({"memo": "x" * 1001}, 422, "validation-failed"),
    ],
)
def test_create_refusals(world: World, change: dict[str, Any], status: int, ptype: str) -> None:
    outsider_hh = world.outsider.record.household_id
    assert outsider_hh is not None
    replacements = {
        "FOREIGN_CATEGORY": str(world.ledger.category(outsider_hh).id),
        "FOREIGN_PAYEE": str(world.ledger.payee(outsider_hh).id),
        "FOREIGN_TAG": str(world.ledger.tag(outsider_hh).id),
        "FOREIGN_ACCOUNT": str(world.foreign_acct.id),
        "HIDDEN_ACCOUNT": str(world.hidden_acct.id),
    }

    def fill(value: Any) -> Any:
        if isinstance(value, list):
            return [fill(v) for v in value]
        return replacements.get(value, value) if isinstance(value, str) else value

    body = _create_body(world.edit_acct)
    body.update({k: fill(v) for k, v in change.items()})
    before = _count(world.factories, world.household_id, Transaction)
    response = world.member.post(TXN, json=body)
    assert response.status_code == status, response.text
    assert problem_type(response) == ptype
    assert _count(world.factories, world.household_id, Transaction) == before


def test_create_honours_idempotency_key(world: World) -> None:
    body = _create_body(world.edit_acct)
    headers = {"Idempotency-Key": f"txn-{uuid.uuid4()}"}
    first = world.member.post(TXN, json=body, headers=headers)
    second = world.member.post(TXN, json=body, headers=headers)
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert _count(world.factories, world.household_id, Transaction) == 1
    reused = world.member.post(TXN, json=_create_body(world.edit_acct, "-3.00"), headers=headers)
    assert (reused.status_code, problem_type(reused)) == (422, "idempotency-key-reused")
    # Another user's key space is separate.
    other = world.owner.post(TXN, json=body, headers=headers)
    assert other.status_code == 201
    assert other.json()["id"] != first.json()["id"]


def test_replay_after_a_revoked_grant_runs_again_under_current_visibility(
    world: World,
) -> None:
    # Review finding P2-SR-05: a stored answer must not outlive the grant
    # that allowed it.
    from app.auth.principal import Principal, apply_rls_context
    from app.domain.households import service as households_service

    body = _create_body(world.edit_acct)
    headers = {"Idempotency-Key": f"txn-{uuid.uuid4()}"}
    first = world.member.post(TXN, json=body, headers=headers)
    assert first.status_code == 201
    owner = world.owner.record
    with world.factories.transaction() as db:
        principal = Principal(
            user=owner.user, household_id=world.household_id, role=owner.role, auth_method="session"
        )
        apply_rls_context(db, principal)
        assert households_service.revoke_account_grant(
            db, principal, world.member.record.id, world.edit_acct.id
        )
    replay = world.member.post(TXN, json=body, headers=headers)
    assert "Idempotent-Replayed" not in replay.headers
    assert (replay.status_code, problem_type(replay)) == (404, "account-not-found")
    assert _count(world.factories, world.household_id, Transaction) == 1


# ---------------------------------------------------------------------------
# Update and delete
# ---------------------------------------------------------------------------


def test_patch_fields_and_clearing(world: World) -> None:
    hh = world.household_id
    cat = world.ledger.category(hh)
    t1, t2 = world.ledger.tag(hh), world.ledger.tag(hh)
    row = world.ledger.transaction(world.edit_acct, amount="-5.00", memo="old memo")
    url = f"{TXN}/{row.id}"
    response = world.member.patch(
        url,
        json={
            "category_id": str(cat.id),
            "amount": {"amount": "-6.00", "currency": "USD"},
            "posted_date": "2026-02-01",
            "memo": None,
            "notes": "secret note",
            "status": "reconciled",
            "review_state": "confirmed",
            "tag_ids": [str(t1.id), str(t2.id)],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["amount"]["amount"] == "-6.00"
    assert body["memo"] is None
    assert body["notes"] == "secret note"
    assert body["status"] == "reconciled"
    assert body["review_state"] == "confirmed"
    assert sorted(body["tag_ids"]) == sorted([str(t1.id), str(t2.id)])
    assert body["posted_date"] == "2026-02-01"
    # Replace the tag set, clear the category.
    body = world.member.patch(url, json={"tag_ids": [str(t2.id)], "category_id": None}).json()
    assert body["tag_ids"] == [str(t2.id)]
    assert body["category_id"] is None
    for bad in ({"amount": None}, {"status": None}, {"account_id": str(world.view_acct.id)}):
        assert world.member.patch(url, json=bad).status_code == 422
    audits = household_audit(world.factories, hh, "transaction.update")
    assert len(audits) == 2
    assert audits[0].before is not None
    assert audits[0].after is not None
    assert audits[0].before["amount"] == "-5.00"
    assert audits[0].after["amount"] == "-6.00"
    assert audits[0].after["memo"] == "[changed]"
    assert audits[0].after["notes"] == "[changed]"
    assert "secret note" not in str(audits[0].after)
    assert "old memo" not in str(audits[0].before)
    # A no-op PATCH writes nothing.
    world.member.patch(url, json={})
    assert len(household_audit(world.factories, hh, "transaction.update")) == 2


def test_delete_keeps_audit_row(world: World) -> None:
    hh = world.household_id
    row = world.ledger.transaction(world.edit_acct, amount="-42.00")
    world.ledger.split(row, amount="-40.00")
    world.ledger.split(row, amount="-2.00")
    response = world.member.delete(f"{TXN}/{row.id}")
    assert response.status_code == 204
    assert world.owner.get(f"{TXN}/{row.id}").status_code == 404
    assert _count(world.factories, hh, TransactionSplit) == 0
    rows = household_audit(world.factories, hh, "transaction.delete")
    assert [r.target_id for r in rows] == [str(row.id)]
    assert rows[0].before is not None
    assert rows[0].before["amount"] == "-42.00"
    assert rows[0].before["splits"] == 2
    assert rows[0].before["account_id"] == str(world.edit_acct.id)
    assert [e.payload["id"] for e in household_events(world.factories, hh, "transaction.deleted")]
    assert world.member.delete(f"{TXN}/{row.id}").status_code == 404


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------


def _split(amount: str, category: Any = None, currency: str = "USD", **extra: Any) -> Any:
    line: dict[str, Any] = {"amount": {"amount": amount, "currency": currency}}
    if category is not None:
        line["category_id"] = str(category.id)
    line.update(extra)
    return line


def test_split_validation_and_replacement(world: World) -> None:
    hh = world.household_id
    food, home = world.ledger.category(hh), world.ledger.category(hh)
    row = world.ledger.transaction(world.edit_acct, amount="-100.00", category=food)
    url = f"{TXN}/{row.id}/split"

    def refused(body: Any, status: int, ptype: str) -> None:
        response = world.member.post(url, json=body)
        assert (response.status_code, problem_type(response)) == (status, ptype), response.text

    refused({"splits": [_split("-60.00", food), _split("-30.00", home)]}, 422, "split-sum-mismatch")
    # Off by the smallest stored unit: exact Decimal comparison, no tolerance.
    refused({"splits": [_split("-100.00", food), _split("0.000001")]}, 422, "split-sum-mismatch")
    refused(
        {"splits": [_split("-50.00", currency="EUR"), _split("-50.00")]}, 422, "currency-mismatch"
    )
    refused({"splits": [_split("-100.00", food)]}, 422, "validation-failed")
    refused(
        {"splits": [_split("-50.00", food), _split("-50.00")], "category_id": str(food.id)},
        422,
        "validation-failed",
    )
    outsider_hh = world.outsider.record.household_id
    assert outsider_hh is not None
    foreign = world.ledger.category(outsider_hh)
    refused({"splits": [_split("-50.00", foreign), _split("-50.00")]}, 404, "category-not-found")
    assert _count(world.factories, hh, TransactionSplit) == 0

    ok = world.member.post(
        url,
        json={
            "splits": [
                _split("-60.00", food, memo="groceries"),
                _split("-45.50", home, tax_flag=True),
                _split("5.50", food),  # a refund inside the purchase
            ]
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["category_id"] is None
    assert [s["amount"]["amount"] for s in body["splits"]] == ["-60.00", "-45.50", "5.50"]
    assert [s["tax_flag"] for s in body["splits"]] == [False, True, False]

    # While split: the amount and the category cannot change directly.
    patch = world.member.patch(f"{TXN}/{row.id}", json={"category_id": str(food.id)})
    assert (patch.status_code, problem_type(patch)) == (409, "transaction-split")
    patch = world.member.patch(
        f"{TXN}/{row.id}", json={"amount": {"amount": "-1.00", "currency": "USD"}}
    )
    assert (patch.status_code, problem_type(patch)) == (409, "transaction-split")
    # The category filter finds it through its splits.
    found = _ids(world.member.get(TXN, params={"category": str(home.id)}))
    assert found == [str(row.id)]

    # Replace with two lines, then remove the splits and categorise again.
    body = world.member.post(url, json={"splits": [_split("-99", food), _split("-1", home)]}).json()
    assert len(body["splits"]) == 2
    body = world.member.post(url, json={"splits": [], "category_id": str(home.id)}).json()
    assert body["splits"] == []
    assert body["category_id"] == str(home.id)
    assert _count(world.factories, hh, TransactionSplit) == 0
    assert len(household_audit(world.factories, hh, "transaction.split")) == 3


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------


def test_bulk_authorises_each_item(world: World) -> None:
    hh, ledger = world.household_id, world.ledger
    cat = ledger.category(hh)
    mine = [ledger.transaction(world.edit_acct, amount=f"-{i}.00") for i in (1, 2)]
    split_row = ledger.transaction(world.edit_acct, amount="-3.00")
    ledger.split(split_row, amount="-1.00")
    ledger.split(split_row, amount="-2.00")
    view_only = ledger.transaction(world.view_acct)
    hidden = ledger.transaction(world.hidden_acct)
    foreign = ledger.transaction(world.foreign_acct)
    missing = uuid.uuid4()
    ids = [*(r.id for r in mine), split_row.id, view_only.id, hidden.id, foreign.id, missing]
    response = world.member.post(
        f"{TXN}/bulk",
        json={"action": "categorise", "ids": [str(i) for i in ids], "category_id": str(cat.id)},
    )
    assert response.status_code == 200, response.text
    results = {r["id"]: r["result"] for r in response.json()["results"]}
    assert [r["id"] for r in response.json()["results"]] == [str(i) for i in ids]
    assert results == {
        str(mine[0].id): "updated",
        str(mine[1].id): "updated",
        str(split_row.id): "skipped",
        str(view_only.id): "not_found",
        str(hidden.id): "not_found",
        str(foreign.id): "not_found",
        str(missing): "not_found",
    }
    assert response.json()["changed"] == 2
    for row in (view_only, hidden):
        assert world.owner.get(f"{TXN}/{row.id}").json()["category_id"] is None
    assert len(household_audit(world.factories, hh, "transaction.bulk_categorise")) == 2
    events = household_events(world.factories, hh, "transaction.bulk_updated")
    assert [e.payload["count"] for e in events] == [2]


def test_bulk_tag_untag_confirm_delete(world: World) -> None:
    hh, ledger = world.household_id, world.ledger
    tag = ledger.tag(hh)
    a = ledger.transaction(world.edit_acct, review_state=ReviewState.PENDING)
    b = ledger.transaction(world.edit_acct)
    hidden = ledger.transaction(world.hidden_acct)
    ids = [str(a.id), str(b.id), str(hidden.id)]

    def run(**body: Any) -> dict[str, str]:
        response = world.member.post(f"{TXN}/bulk", json={"ids": ids, **body})
        assert response.status_code == 200, response.text
        return {r["id"]: r["result"] for r in response.json()["results"]}

    assert run(action="tag", tag_ids=[str(tag.id)]) == {
        str(a.id): "updated",
        str(b.id): "updated",
        str(hidden.id): "not_found",
    }
    assert run(action="tag", tag_ids=[str(tag.id)])[str(a.id)] == "unchanged"
    assert _count(world.factories, hh, TransactionTag) == 2
    assert run(action="untag", tag_ids=[str(tag.id)])[str(b.id)] == "updated"
    assert _count(world.factories, hh, TransactionTag) == 0
    confirmed = run(action="confirm")
    assert confirmed[str(a.id)] == "updated"
    assert confirmed[str(b.id)] == "updated"
    assert world.owner.get(f"{TXN}/{a.id}").json()["review_state"] == "confirmed"
    deleted = run(action="delete")
    assert deleted == {str(a.id): "deleted", str(b.id): "deleted", str(hidden.id): "not_found"}
    assert world.owner.get(f"{TXN}/{hidden.id}").status_code == 200
    assert len(household_audit(world.factories, hh, "transaction.bulk_delete")) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"action": "categorise", "ids": []},
        {"action": "categorise", "ids": ["ID"]},  # category_id missing
        {"action": "tag", "ids": ["ID"]},
        {"action": "tag", "ids": ["ID"], "tag_ids": []},
        {"action": "delete", "ids": ["ID"], "category_id": "ID"},
        {"action": "delete", "ids": ["ID", "ID"]},
        {"action": "explode", "ids": ["ID"]},
        {"action": "delete", "ids": ["ID"], "extra": 1},
    ],
)
def test_bulk_validation(world: World, body: dict[str, Any]) -> None:
    row = world.ledger.transaction(world.edit_acct)
    filled = {
        k: ([str(row.id) for _ in v] if isinstance(v, list) and v and v[0] == "ID" else v)
        for k, v in body.items()
    }
    if filled.get("category_id") == "ID":
        filled["category_id"] = str(uuid.uuid4())
    response = world.member.post(f"{TXN}/bulk", json=filled)
    assert response.status_code == 422, response.text
    assert world.owner.get(f"{TXN}/{row.id}").status_code == 200


def test_bulk_caps_ids(world: World) -> None:
    ids = [str(uuid.uuid4()) for _ in range(501)]
    response = world.member.post(f"{TXN}/bulk", json={"action": "delete", "ids": ids})
    assert response.status_code == 422
    response = world.member.post(f"{TXN}/bulk", json={"action": "delete", "ids": ids[:500]})
    assert response.status_code == 200
    assert response.json()["changed"] == 0


def test_bulk_refuses_foreign_category_before_touching_anything(world: World) -> None:
    outsider_hh = world.outsider.record.household_id
    assert outsider_hh is not None
    foreign_cat = world.ledger.category(outsider_hh)
    row = world.ledger.transaction(world.edit_acct)
    response = world.member.post(
        f"{TXN}/bulk",
        json={"action": "categorise", "ids": [str(row.id)], "category_id": str(foreign_cat.id)},
    )
    assert (response.status_code, problem_type(response)) == (404, "category-not-found")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def test_token_scopes(world: World) -> None:
    owner: UserRecord = world.owner.record
    row = world.ledger.transaction(world.edit_acct)
    client = world.actors.anonymous()
    read = world.factories.token(owner, scopes=("transactions:read",))
    write = world.factories.token(owner, scopes=("transactions:write",))
    as_read = {"Authorization": f"Bearer {read}"}
    as_write = {"Authorization": f"Bearer {write}"}
    assert client.get(TXN, headers=as_read).status_code == 200
    assert client.get(f"{TXN}/{row.id}", headers=as_write).status_code == 403
    assert client.patch(f"{TXN}/{row.id}", json={"memo": "x"}, headers=as_read).status_code == 403
    response = client.patch(f"{TXN}/{row.id}", json={"memo": "via token"}, headers=as_write)
    assert response.status_code == 200
    assert response.json()["memo"] == "via token"


# ---------------------------------------------------------------------------
# Reports: pending review excluded by default
# ---------------------------------------------------------------------------


def test_reportable_clause_excludes_pending_review(world: World) -> None:
    hh, ledger = world.household_id, world.ledger
    keep = ledger.transaction(world.edit_acct, amount="-1.00")
    ledger.transaction(world.edit_acct, amount="-2.00", review_state=ReviewState.PENDING)
    ledger.transaction(world.edit_acct, amount="-4.00", review_state=ReviewState.CONFIRMED)

    def total(include: bool | None) -> Decimal:
        with world.factories.transaction() as db:
            set_context(db, hh)
            flag = service.include_pending_in_reports(db, hh) if include is None else include
            return Decimal(
                db.execute(
                    select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                        Transaction.household_id == hh, service.reportable_clause(flag)
                    )
                ).scalar_one()
            )

    assert total(None) == Decimal("-5.00")
    assert total(True) == Decimal("-7.00")
    with world.factories.transaction() as db:
        set_context(db, hh)
        db.add(
            Setting(
                scope=SettingScope.HOUSEHOLD,
                scope_id=hh,
                key=service.INCLUDE_PENDING_SETTING,
                value=True,
            )
        )
    assert total(None) == Decimal("-7.00")
    assert keep.review_state is ReviewState.NONE


def test_dates_far_apart_paginate(world: World) -> None:
    """Keyset ordering across years, with many rows on one date (id tiebreak)."""
    acct = world.edit_acct
    rows = [world.ledger.transaction(acct, posted_date=date(2020, 1, 1)) for _ in range(4)]
    rows += [
        world.ledger.transaction(acct, posted_date=date(2026, 1, 1) - timedelta(days=i))
        for i in range(3)
    ]
    expected = [str(r.id) for r in sorted(rows, key=lambda r: (r.posted_date, r.id), reverse=True)]
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = world.member.get(TXN, params=params)
        seen += _ids(page)
        cursor = page.json()["next_cursor"]
        if not cursor:
            break
    assert seen == expected

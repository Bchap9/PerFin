# SPDX-License-Identifier: AGPL-3.0-or-later
"""Securities, investment transactions, lots, holdings and performance over HTTP.

Every figure is exact (``Decimal`` strings). The ledger is synthetic: invented
symbols, accounts and prices, built through the API (lots and holdings come
only from the lot engine) plus fixture prices and exchange rates.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType
from app.domain.investments import service as investments_service
from app.domain.investments.models import Holding, InvestmentTxn, Lot
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import (
    API,
    Actors,
    household_audit,
    household_events,
    problem_type,
)
from tests.fixtures.ledger import LedgerFactories
from tests.fixtures.wealth import make_lot, make_price

pytestmark = pytest.mark.integration

SECURITIES = f"{API}/securities"
TXNS = f"{API}/investments/transactions"
HOLDINGS = f"{API}/investments/holdings"
PERFORMANCE = f"{API}/investments/performance"
TODAY = date(2026, 6, 30)


@dataclass(frozen=True)
class World:
    owner: AuthedClient
    member: AuthedClient
    viewer: AuthedClient
    household_id: uuid.UUID
    brokerage: Account
    euro: Account
    hidden: Account
    readonly: Account
    checking: Account
    ledger: LedgerFactories


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


@pytest.fixture
def world(actors: Actors, ledger: LedgerFactories, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setattr(investments_service, "today", lambda: TODAY)
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    member = actors.new(role="member", household=household)
    viewer = actors.new(role="viewer", household=household)
    hid = household.id
    brokerage = ledger.account(hid, type=AccountType.BROKERAGE, name="Brokerage")
    euro = ledger.account(hid, type=AccountType.RETIREMENT, name="Euro pension", currency="EUR")
    hidden = ledger.account(hid, type=AccountType.BROKERAGE, name="Hidden")
    readonly = ledger.account(hid, type=AccountType.BROKERAGE, name="Read only")
    checking = ledger.account(hid, type=AccountType.CHECKING, name="Checking")
    for account in (brokerage, euro, checking):
        ledger.grant(hid, member.record.id, account, GrantLevel.EDIT)
        ledger.grant(hid, viewer.record.id, account, GrantLevel.VIEW)
    ledger.grant(hid, member.record.id, readonly, GrantLevel.VIEW)
    ledger.fx_rate(hid, base="EUR", quote="USD", rate_date=date(2026, 6, 1), rate="1.1")
    return World(owner, member, viewer, hid, brokerage, euro, hidden, readonly, checking, ledger)


def _created(response: Any) -> dict[str, Any]:
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _security(client: AuthedClient, symbol: str, **extra: Any) -> dict[str, Any]:
    body = {"symbol": symbol, "name": f"Synthetic {symbol}", "asset_class": "equity"}
    body.setdefault("currency", "USD")
    return _created(client.post(SECURITIES, json={"currency": "USD", **body, **extra}))


def _money(amount: str, currency: str = "USD") -> dict[str, str]:
    return {"amount": amount, "currency": currency}


def _txn_body(
    account: Account,
    kind: str,
    day: str,
    *,
    security: dict[str, Any] | None = None,
    amount: str = "0",
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "account_id": str(account.id),
        "type": kind,
        "trade_date": day,
        "amount": _money(amount, account.currency),
        **extra,
    }
    if security is not None:
        body["security_id"] = security["id"]
    return body


def _txn(client: AuthedClient, account: Account, kind: str, day: str, **kw: Any) -> dict[str, Any]:
    return _created(client.post(TXNS, json=_txn_body(account, kind, day, **kw)))


def _lots(world: World, factories: Factories, account: Account) -> list[Lot]:
    with factories.transaction() as db:
        set_context(db, world.household_id)
        rows = list(
            db.execute(
                select(Lot).where(Lot.account_id == account.id).order_by(Lot.open_date, Lot.id)
            ).scalars()
        )
        for row in rows:
            db.expunge(row)
        return rows


def _by_symbol(holdings: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(h["account_id"], h["security"]["symbol"]): h for h in holdings["holdings"]}


# ---------------------------------------------------------------------------
# Securities
# ---------------------------------------------------------------------------


def test_security_catalogue(world: World, factories: Factories) -> None:
    created = _security(world.member, "SYNA", isin="us0000000001", sector="Synthetic tech")
    assert created["isin"] == "US0000000001"
    duplicate = world.owner.post(
        SECURITIES,
        json={"symbol": "SYNA", "name": "Again", "asset_class": "etf", "currency": "USD"},
    )
    assert (duplicate.status_code, problem_type(duplicate)) == (409, "security-exists")
    bad = world.member.post(
        SECURITIES,
        json={"symbol": "BAD SYMBOL", "name": "x", "asset_class": "equity", "currency": "USD"},
    )
    assert bad.status_code == 422
    other = _security(world.member, "SYNB", exchange="XSYN")
    listed = world.viewer.get(SECURITIES, params={"q": "syn"}).json()
    assert [s["symbol"] for s in listed["items"]] == ["SYNA", "SYNB"]
    page = world.viewer.get(SECURITIES, params={"limit": 1}).json()
    assert len(page["items"]) == 1
    rest = world.viewer.get(SECURITIES, params={"cursor": page["next_cursor"]}).json()
    assert [s["symbol"] for s in rest["items"]] == ["SYNB"]
    patched = world.member.patch(f"{SECURITIES}/{other['id']}", json={"currency": "EUR"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["currency"] == "EUR"
    assert world.viewer.patch(f"{SECURITIES}/{other['id']}", json={"name": "x"}).status_code == 403
    assert world.member.delete(f"{SECURITIES}/{other['id']}").status_code == 204
    assert world.member.get(f"{SECURITIES}/{other['id']}").status_code == 404
    assert household_audit(factories, world.household_id, "security.create")


def test_security_used_only_on_hidden_account_is_not_found(world: World) -> None:
    secret = _security(world.owner, "SYNH")
    shared = _security(world.owner, "SYNS")
    _txn(
        world.owner, world.hidden, "buy", "2026-01-05", security=secret, quantity="1", amount="-10"
    )
    _txn(
        world.owner, world.hidden, "buy", "2026-01-05", security=shared, quantity="1", amount="-10"
    )
    _txn(
        world.owner,
        world.brokerage,
        "buy",
        "2026-01-06",
        security=shared,
        quantity="1",
        amount="-9",
    )
    member_view = world.member.get(SECURITIES).json()
    assert [s["symbol"] for s in member_view["items"]] == ["SYNS"]
    for client in (world.member, world.viewer):
        response = client.get(f"{SECURITIES}/{secret['id']}")
        assert (response.status_code, problem_type(response)) == (404, "security-not-found")
        prices = client.get(f"{SECURITIES}/{secret['id']}/prices")
        assert prices.status_code == 404
    hidden_use = world.member.post(
        TXNS, json=_txn_body(world.brokerage, "buy", "2026-01-07", security=secret, quantity="1")
    )
    assert (hidden_use.status_code, problem_type(hidden_use)) == (404, "security-not-found")
    assert world.owner.get(f"{SECURITIES}/{secret['id']}").status_code == 200
    in_use = world.owner.delete(f"{SECURITIES}/{secret['id']}")
    assert (in_use.status_code, problem_type(in_use)) == (409, "security-in-use")
    locked = world.owner.patch(f"{SECURITIES}/{shared['id']}", json={"currency": "EUR"})
    assert (locked.status_code, problem_type(locked)) == (409, "security-currency-locked")


def test_manual_prices(world: World) -> None:
    security = _security(world.member, "SYNP")
    url = f"{SECURITIES}/{security['id']}/prices"
    first = _created(world.member.post(url, json={"price_date": "2026-06-01", "close": "10.5"}))
    assert (first["close"], first["currency"], first["source"]) == (
        "10.5",
        "USD",
        "manual",
    )
    again = _created(world.member.post(url, json={"price_date": "2026-06-01", "close": "11"}))
    assert again["id"] == first["id"]
    _created(world.member.post(url, json={"price_date": "2026-06-02", "close": "12"}))
    listed = world.viewer.get(url).json()["items"]
    assert [(p["price_date"], p["close"]) for p in listed] == [
        ("2026-06-02", "12"),
        ("2026-06-01", "11"),
    ]
    float_close = world.member.post(url, json={"price_date": "2026-06-03", "close": 1.5})
    assert float_close.status_code == 422
    future = world.member.post(url, json={"price_date": "2026-07-01", "close": "1"})
    assert (future.status_code, problem_type(future)) == (422, "price-date-in-future")
    assert (
        world.viewer.post(url, json={"price_date": "2026-06-03", "close": "1"}).status_code == 403
    )
    assert world.member.delete(f"{url}/{first['id']}").status_code == 204
    assert len(world.viewer.get(url).json()["items"]) == 1


# ---------------------------------------------------------------------------
# Transactions and lots
# ---------------------------------------------------------------------------


def test_fifo_lots_holdings_and_realised_gains(world: World, factories: Factories) -> None:
    sec = _security(world.member, "SYNF", sector="Synthetic tech")
    first = _txn(
        world.member,
        world.brokerage,
        "buy",
        "2025-01-10",
        security=sec,
        quantity="10",
        price="100",
        amount="-1005",
        fees=_money("5"),
        memo="synthetic memo text",
    )
    _txn(
        world.member,
        world.brokerage,
        "buy",
        "2026-03-01",
        security=sec,
        quantity="10",
        price="120",
        amount="-1200",
    )
    sale = _txn(
        world.member,
        world.brokerage,
        "sell",
        "2026-04-01",
        security=sec,
        quantity="12",
        price="150",
        amount="1795",
        fees=_money("5"),
    )
    lots = _lots(world, factories, world.brokerage)
    assert [(lot.remaining_quantity, lot.cost_per_unit, lot.closed_date) for lot in lots] == [
        (Decimal(0), Decimal("100.5"), date(2026, 4, 1)),
        (Decimal(8), Decimal(120), None),
    ]

    def picks(row: dict[str, Any]) -> list[tuple[str, Decimal]]:
        return [(p["lot_id"], Decimal(p["quantity"])) for p in row["lots"]]

    assert picks(sale) == [(str(lots[0].id), Decimal(10)), (str(lots[1].id), Decimal(2))]
    assert picks(first) == [(str(lots[0].id), Decimal(10))]
    assert sale["lots_specified"] is False

    with factories.transaction() as db:
        set_context(db, world.household_id)
        holding = db.execute(
            select(Holding).where(Holding.account_id == world.brokerage.id)
        ).scalar_one()
        assert (holding.quantity, holding.cost_basis) == (Decimal(8), Decimal(960))
    from app.domain.investments.models import Security

    with factories.transaction() as db:
        set_context(db, world.household_id)
        security_row = db.get(Security, uuid.UUID(sec["id"]))
        assert security_row is not None
        db.expunge(security_row)
    world.ledger.run(make_price, security_row, close="130", price_date=date(2026, 6, 29))

    holdings = world.member.get(HOLDINGS, params={"account": str(world.brokerage.id)}).json()
    entry = _by_symbol(holdings)[(str(world.brokerage.id), "SYNF")]
    assert entry["quantity"] == "8"
    assert entry["cost_basis"] == _money("960.00")
    assert entry["price"] == {
        "close": "130",
        "currency": "USD",
        "price_date": "2026-06-29",
        "basis": "price",
    }
    assert entry["market_value"] == _money("1040.00")
    assert entry["unrealized"] == {
        "short_term": _money("80.00"),
        "long_term": _money("0.00"),
        "total": _money("80.00"),
    }
    assert entry["weight"] == "1.0000000000"
    assert [lot["term"] for lot in entry["lots"]] == ["short"]

    performance = world.member.get(
        PERFORMANCE, params={"range": "max", "account": str(world.brokerage.id)}
    ).json()
    assert performance["realized"] == {
        "short_term": _money("59.166667"),
        "long_term": _money("490.833333"),
        "total": _money("550.00"),
    }
    assert performance["fees"] == _money("10.00")
    assert performance["start"] == "2025-01-09"

    audit_rows = household_audit(factories, world.household_id, "investment_txn.create")
    assert len(audit_rows) == 3
    assert all("synthetic memo text" not in str(row.after) for row in audit_rows)
    events = household_events(factories, world.household_id, "investment.txn_created")
    assert {e.payload["account_id"] for e in events} == {str(world.brokerage.id)}


def test_specific_lots_and_average_cost(world: World, factories: Factories) -> None:
    sec = _security(world.member, "SYNL")
    for day, price in (("2026-01-02", "100"), ("2026-02-02", "200")):
        _txn(
            world.member,
            world.brokerage,
            "buy",
            day,
            security=sec,
            quantity="10",
            price=price,
            amount=f"-{Decimal(price) * 10}",
        )
    lots = _lots(world, factories, world.brokerage)
    newest = str(lots[1].id)
    sale = _txn(
        world.member,
        world.brokerage,
        "sell",
        "2026-03-02",
        security=sec,
        quantity="4",
        amount="1000",
        lots=[{"lot_id": newest, "quantity": "4"}],
    )
    assert sale["lots_specified"] is True
    assert [(p["lot_id"], Decimal(p["quantity"])) for p in sale["lots"]] == [(newest, Decimal(4))]
    remaining = {lot.id: lot.remaining_quantity for lot in _lots(world, factories, world.brokerage)}
    assert remaining == {lots[0].id: Decimal(10), lots[1].id: Decimal(6)}
    mismatch = world.member.post(
        TXNS,
        json=_txn_body(
            world.brokerage,
            "sell",
            "2026-03-03",
            security=sec,
            quantity="2",
            amount="1",
            lots=[{"lot_id": newest, "quantity": "1"}],
        ),
    )
    assert (mismatch.status_code, problem_type(mismatch)) == (422, "lot-links-mismatch")
    url = f"{API}/investments/accounts/{world.brokerage.id}/cost-method"
    refused = world.member.put(url, json={"method": "average"})
    assert (refused.status_code, problem_type(refused)) == (422, "lot-selection-not-allowed")
    # Without the named lots the account can switch; the sale then takes the average.
    patched = world.member.patch(f"{TXNS}/{sale['id']}", json={"lots": None})
    assert patched.status_code == 200, patched.text
    assert patched.json()["lots_specified"] is False
    switched = world.member.put(url, json={"method": "average"})
    assert switched.status_code == 200, switched.text
    assert world.viewer.get(url).json() == {
        "account_id": str(world.brokerage.id),
        "method": "average",
    }
    lots_now = _lots(world, factories, world.brokerage)
    assert {lot.cost_per_unit for lot in lots_now if lot.remaining_quantity} == {Decimal(150)}
    holdings = world.owner.get(HOLDINGS, params={"account": str(world.brokerage.id)}).json()
    assert holdings["accounts"][0]["cost_method"] == "average"
    assert _by_symbol(holdings)[(str(world.brokerage.id), "SYNL")]["cost_basis"] == _money(
        "2400.00"
    )
    named = world.member.post(
        TXNS,
        json=_txn_body(
            world.brokerage,
            "sell",
            "2026-03-03",
            security=sec,
            quantity="1",
            amount="1",
            lots=[{"lot_id": str(lots_now[0].id), "quantity": "1"}],
        ),
    )
    assert (named.status_code, problem_type(named)) == (422, "lot-selection-not-allowed")
    assert household_events(factories, world.household_id, "investment.cost_method_changed")


def test_split_dividend_reinvest_and_transfers(world: World, factories: Factories) -> None:
    sec = _security(world.member, "SYNX")
    _txn(
        world.member,
        world.brokerage,
        "buy",
        "2026-01-02",
        security=sec,
        quantity="10",
        amount="-1000",
    )
    split = _txn(
        world.member,
        world.brokerage,
        "split",
        "2026-02-02",
        security=sec,
        ratio={"new": "3", "old": "1"},
    )
    assert (split["ratio"], split["quantity"], split["price"]) == (
        {"new": "3", "old": "1"},
        None,
        None,
    )
    _txn(world.member, world.brokerage, "dividend", "2026-02-10", security=sec, amount="12.5")
    _txn(
        world.member,
        world.brokerage,
        "reinvest",
        "2026-02-10",
        security=sec,
        quantity="0.5",
        amount="-12.5",
    )
    _txn(
        world.member,
        world.brokerage,
        "transfer_in",
        "2026-02-11",
        security=sec,
        quantity="2",
        price="30",
    )
    _txn(world.member, world.brokerage, "transfer_out", "2026-02-12", security=sec, quantity="20")
    lots = _lots(world, factories, world.brokerage)
    assert [(lot.quantity, lot.remaining_quantity) for lot in lots] == [
        (Decimal(30), Decimal(10)),
        (Decimal("0.5"), Decimal("0.5")),
        (Decimal(2), Decimal(2)),
    ]
    assert lots[0].cost_per_unit == Decimal("33.3333333333")
    holdings = world.member.get(HOLDINGS, params={"account": str(world.brokerage.id)}).json()
    entry = _by_symbol(holdings)[(str(world.brokerage.id), "SYNX")]
    assert entry["quantity"] == "12.5"
    # 10 x 33.3333333333 + 12.5 + 60
    assert entry["cost_basis"] == _money("405.833333")
    split_needed = world.member.delete(f"{TXNS}/{split['id']}")
    assert (split_needed.status_code, problem_type(split_needed)) == (409, "insufficient-quantity")
    bad_split = world.member.post(
        TXNS, json=_txn_body(world.brokerage, "split", "2026-03-01", security=sec, quantity="2")
    )
    assert (bad_split.status_code, problem_type(bad_split)) == (
        422,
        "invalid-investment-transaction",
    )


def test_write_rules_and_problems(world: World) -> None:
    sec = _security(world.member, "SYNR")
    short = world.member.post(
        TXNS, json=_txn_body(world.brokerage, "sell", "2026-01-02", security=sec, quantity="1")
    )
    assert (short.status_code, problem_type(short)) == (422, "insufficient-quantity")
    wrong_currency = world.member.post(
        TXNS,
        json={
            **_txn_body(world.brokerage, "buy", "2026-01-02", security=sec, quantity="1"),
            "amount": _money("-10", "EUR"),
        },
    )
    assert (wrong_currency.status_code, problem_type(wrong_currency)) == (422, "currency-mismatch")
    checking = world.member.post(
        TXNS, json=_txn_body(world.checking, "buy", "2026-01-02", security=sec, quantity="1")
    )
    assert (checking.status_code, problem_type(checking)) == (422, "not-an-investment-account")
    future = world.member.post(
        TXNS, json=_txn_body(world.brokerage, "buy", "2026-07-01", security=sec, quantity="1")
    )
    assert (future.status_code, problem_type(future)) == (422, "trade-date-in-future")
    positive_buy = world.member.post(
        TXNS,
        json=_txn_body(
            world.brokerage, "buy", "2026-01-02", security=sec, quantity="1", amount="10"
        ),
    )
    assert problem_type(positive_buy) == "invalid-investment-transaction"
    float_quantity = world.member.post(
        TXNS, json=_txn_body(world.brokerage, "buy", "2026-01-02", security=sec, quantity=1.5)
    )
    assert float_quantity.status_code == 422
    extra_field = world.member.post(
        TXNS,
        json={
            **_txn_body(world.brokerage, "dividend", "2026-01-02"),
            "household_id": str(uuid.uuid4()),
        },
    )
    assert extra_field.status_code == 422
    # A member with a view grant cannot write; a hidden account does not exist for them.
    for account in (world.readonly, world.hidden):
        response = world.member.post(
            TXNS, json=_txn_body(account, "buy", "2026-01-02", security=sec, quantity="1")
        )
        assert (response.status_code, problem_type(response)) == (404, "account-not-found")
    viewer = world.viewer.post(
        TXNS, json=_txn_body(world.brokerage, "dividend", "2026-01-02", amount="1")
    )
    assert viewer.status_code == 403


def test_update_and_delete_replay(world: World, factories: Factories) -> None:
    sec = _security(world.member, "SYNU")
    buy = _txn(
        world.member,
        world.brokerage,
        "buy",
        "2026-01-02",
        security=sec,
        quantity="10",
        amount="-100",
    )
    sale = _txn(
        world.member, world.brokerage, "sell", "2026-01-05", security=sec, quantity="4", amount="60"
    )
    too_few = world.member.patch(f"{TXNS}/{buy['id']}", json={"quantity": "3"})
    assert (too_few.status_code, problem_type(too_few)) == (422, "insufficient-quantity")
    changed = world.member.patch(
        f"{TXNS}/{buy['id']}", json={"quantity": "8", "amount": _money("-80")}
    )
    assert changed.status_code == 200, changed.text
    lots = _lots(world, factories, world.brokerage)
    assert [(lot.quantity, lot.remaining_quantity, lot.cost_per_unit) for lot in lots] == [
        (Decimal(8), Decimal(4), Decimal(10))
    ]
    assert lots[0].opening_txn_id == uuid.UUID(buy["id"])
    needed = world.member.delete(f"{TXNS}/{buy['id']}")
    assert (needed.status_code, problem_type(needed)) == (409, "insufficient-quantity")
    assert world.member.delete(f"{TXNS}/{sale['id']}").status_code == 204
    assert world.member.delete(f"{TXNS}/{buy['id']}").status_code == 204
    assert _lots(world, factories, world.brokerage) == []
    with factories.transaction() as db:
        set_context(db, world.household_id)
        assert (
            db.execute(select(Holding).where(Holding.account_id == world.brokerage.id)).first()
            is None
        )
        assert (
            db.execute(
                select(InvestmentTxn).where(InvestmentTxn.account_id == world.brokerage.id)
            ).first()
            is None
        )


def test_listing_and_visibility(world: World) -> None:
    sec = _security(world.owner, "SYNV")
    _txn(
        world.owner,
        world.brokerage,
        "buy",
        "2026-01-02",
        security=sec,
        quantity="1",
        price="10",
        amount="-10",
    )
    hidden = _txn(
        world.owner,
        world.hidden,
        "buy",
        "2026-01-03",
        security=sec,
        quantity="1",
        price="10",
        amount="-10",
    )
    _txn(world.owner, world.brokerage, "dividend", "2026-01-04", security=sec, amount="0.5")
    owner_list = world.owner.get(TXNS).json()["items"]
    assert [t["trade_date"] for t in owner_list] == ["2026-01-04", "2026-01-03", "2026-01-02"]
    member_list = world.member.get(TXNS).json()["items"]
    assert [t["trade_date"] for t in member_list] == ["2026-01-04", "2026-01-02"]
    assert world.member.get(TXNS, params={"type": "dividend"}).json()["items"][0][
        "amount"
    ] == _money("0.50")
    page = world.owner.get(TXNS, params={"limit": 2}).json()
    rest = world.owner.get(TXNS, params={"limit": 2, "cursor": page["next_cursor"]}).json()
    assert [t["trade_date"] for t in rest["items"]] == ["2026-01-02"]
    for client in (world.member, world.viewer):
        response = client.get(f"{TXNS}/{hidden['id']}")
        assert (response.status_code, problem_type(response)) == (
            404,
            "investment-transaction-not-found",
        )
        filtered = client.get(TXNS, params={"account": str(world.hidden.id)})
        assert (filtered.status_code, problem_type(filtered)) == (404, "account-not-found")
        hidden_holdings = client.get(HOLDINGS, params={"account": str(world.hidden.id)})
        assert hidden_holdings.status_code == 404
    patch = world.member.patch(f"{TXNS}/{hidden['id']}", json={"memo": "x"})
    assert (patch.status_code, problem_type(patch)) == (404, "investment-transaction-not-found")
    owner_holdings = world.owner.get(HOLDINGS).json()
    member_holdings = world.member.get(HOLDINGS).json()
    assert len(owner_holdings["holdings"]) == 2
    assert [h["account_id"] for h in member_holdings["holdings"]] == [str(world.brokerage.id)]
    assert member_holdings["market_value"] == _money("10.00")
    assert owner_holdings["market_value"] == _money("20.00")


def test_multi_currency_holdings_and_allocation(world: World, factories: Factories) -> None:
    usd = _security(world.member, "SYNU1", sector="Synthetic tech")
    eur = _security(world.member, "SYNE1", asset_class="etf", currency="EUR")
    _txn(
        world.member,
        world.brokerage,
        "buy",
        "2026-03-01",
        security=usd,
        quantity="10",
        price="100",
        amount="-1000",
    )
    _txn(
        world.member,
        world.euro,
        "buy",
        "2026-05-01",
        security=eur,
        quantity="5",
        price="20",
        amount="-100",
    )
    _created(
        world.member.post(
            f"{SECURITIES}/{eur['id']}/prices", json={"price_date": "2026-06-30", "close": "22"}
        )
    )
    holdings = world.member.get(HOLDINGS).json()
    assert holdings["base_currency"] == "USD"
    euro_entry = _by_symbol(holdings)[(str(world.euro.id), "SYNE1")]
    assert euro_entry["market_value"] == _money("110.00", "EUR")
    assert euro_entry["market_value_base"] == _money("121.00")
    assert euro_entry["unrealized"]["short_term"] == _money("10.00", "EUR")
    assert holdings["market_value"] == _money("1121.00")
    assert holdings["cost_basis"] == _money("1110.00")
    assert holdings["unrealized"]["total"] == _money("11.00")
    allocation = holdings["allocation"]
    assert [(a["key"], a["value"]) for a in allocation["asset_class"]] == [
        ("equity", _money("1000.00")),
        ("etf", _money("121.00")),
    ]
    assert [a["key"] for a in allocation["sector"]] == ["Synthetic tech", "unclassified"]
    assert sum(Decimal(a["weight"]) for a in allocation["account"]) == pytest.approx(Decimal(1))
    assert holdings["missing_rates"] == []


def test_performance_twr_equals_mwr_without_flows(world: World, factories: Factories) -> None:
    sec = _security(world.owner, "SYNT")
    _txn(
        world.owner,
        world.brokerage,
        "buy",
        "2026-01-02",
        security=sec,
        quantity="10",
        price="100",
        amount="-1000",
    )
    url = f"{SECURITIES}/{sec['id']}/prices"
    for day, close in (("2026-03-31", "110"), ("2026-06-30", "121")):
        _created(world.owner.post(url, json={"price_date": day, "close": close}))
    params = {"range": "3m", "account": str(world.brokerage.id)}
    quiet = world.owner.get(PERFORMANCE, params=params).json()
    assert (quiet["start"], quiet["end"], quiet["days"]) == ("2026-03-30", "2026-06-30", 92)
    assert quiet["start_value"] == _money("1000.00")
    assert quiet["end_value"] == _money("1210.00")
    assert quiet["twr"] == "0.2100000000"
    assert quiet["mwr"] == "0.2100000000"
    assert quiet["twr_annualized"] is None
    assert quiet["mwr_annualized"] is None
    assert quiet["inflows"] == _money("0.00")
    assert quiet["series"][-1] == {
        "date": "2026-06-30",
        "value": _money("1210.00"),
        "twr": "0.2100000000",
    }
    _txn(world.owner, world.brokerage, "dividend", "2026-05-15", security=sec, amount="50")
    paid = world.owner.get(PERFORMANCE, params=params).json()
    # 1.1 x (1100 + 50) / 1100 x 1.1 - 1
    assert paid["twr"] == "0.2650000000"
    assert paid["income"] == _money("50.00")
    assert paid["outflows"] == _money("50.00")
    assert Decimal("0.21") < Decimal(paid["mwr"]) < Decimal("0.27")
    year = world.owner.get(
        PERFORMANCE, params={"range": "1y", "account": str(world.brokerage.id)}
    ).json()
    assert year["start"] == "2026-01-01"
    assert year["start_value"] == _money("0.00")
    assert year["inflows"] == _money("1000.00")
    assert year["unrealized"]["short_term"] == _money("210.00")
    assert world.viewer.get(PERFORMANCE, params={"range": "ytd"}).status_code == 200
    assert world.viewer.get(PERFORMANCE, params={"range": "2w"}).status_code == 422
    hidden = world.member.get(PERFORMANCE, params={"account": str(world.hidden.id)})
    assert hidden.status_code == 404


def test_statement_seed_lots_survive_replay(world: World, factories: Factories) -> None:
    """Lots without an opening transaction (statement positions) are kept by a replay."""
    sec = _security(world.owner, "SYNQ")
    from app.domain.investments.models import Security

    with factories.transaction() as db:
        set_context(db, world.household_id)
        security_row = db.get(Security, uuid.UUID(sec["id"]))
        assert security_row is not None
        db.expunge(security_row)
    seed = world.ledger.run(
        make_lot,
        world.brokerage,
        security_row,
        quantity="5",
        cost_per_unit="10",
        open_date=date(2026, 1, 2),
    )
    _txn(
        world.owner, world.brokerage, "sell", "2026-02-02", security=sec, quantity="2", amount="40"
    )
    lots = _lots(world, factories, world.brokerage)
    assert [(lot.id, lot.remaining_quantity, lot.opening_txn_id) for lot in lots] == [
        (seed.id, Decimal(3), None)
    ]
    performance = world.owner.get(
        PERFORMANCE, params={"range": "max", "account": str(world.brokerage.id)}
    ).json()
    assert performance["realized"]["total"] == _money("20.00")

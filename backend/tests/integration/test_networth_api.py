# SPDX-License-Identifier: AGPL-3.0-or-later
"""Net worth over HTTP and the daily snapshot job, on a synthetic household.

"Today" is frozen at 2026-06-30. The household (base USD) holds, generated
by code with invented names and amounts:

* checking (USD): manual balance 1000.00 on 2026-06-01, then -200.00 and
  +3000.00 -> 3800.00
* savings (EUR): +1000.00 EUR, EUR->USD 1.10 -> 1100.00
* credit card (USD): -500.00 -> liability 500.00
* brokerage (USD): +1000.00 cash deposit, buy 10 units at 100.00, close
  120.00 on 2026-06-29 -> 0.00 cash + 1200.00 holdings
* mortgage: 200000.00 from 2024-03-01, 1000.00 principal paid 2026-06-01
  (balance synced by the loans service) -> liability 199000.00
* property (manual asset): valued 350000.00 on 2026-01-15
* hidden checking (USD): +5000.00, not granted to the member

Owner: assets 361100.00, liabilities 199500.00, net 161600.00. The member
sees every account but the hidden one (net 156600.00); the viewer is
granted every account and so gets household figures.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.db.models import Job
from app.db.queue import enqueue
from app.db.rls import queue_scope, set_context
from app.domain.accounts import service as accounts_service
from app.domain.accounts.models import Account, AccountType, BalanceSource
from app.domain.documents.models import Document
from app.domain.imports import service as imports_service
from app.domain.investments.models import InvestmentTxnType, opening_import_hash
from app.domain.loans import service as loans_service
from app.domain.networth import recompute as networth_recompute
from app.domain.networth import service as networth_service
from app.domain.networth.compute import NetWorthBook
from app.domain.networth.models import NetWorthSnapshot
from app.domain.reports import service as reports_service
from app.workers.handlers import networth as networth_handler
from app.workers.registry import JobContext
from app.workers.scheduler import SCHEDULE
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, problem_type
from tests.fixtures.imports import make_extracted_document, make_import_mapping, table_result
from tests.fixtures.ledger import (
    LedgerFactories,
    household_context,
    make_fx_rate,
    make_snapshot,
    make_transaction,
)
from tests.fixtures.wealth import (
    make_asset_valuation,
    make_investment_txn,
    make_loan,
    make_loan_payment,
    make_net_worth_snapshot,
    make_price,
    make_security,
)

pytestmark = pytest.mark.integration

NW = f"{API}/networth"
TODAY = date(2026, 6, 30)
D = Decimal


@dataclass(frozen=True)
class World:
    owner: AuthedClient
    member: AuthedClient
    viewer: AuthedClient
    household_id: uuid.UUID
    accounts: dict[str, Account]
    hidden: Account
    ledger: LedgerFactories
    factories: Factories


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


def freeze(monkeypatch: pytest.MonkeyPatch, day: date = TODAY) -> None:
    monkeypatch.setattr(networth_service, "today", lambda: day)
    monkeypatch.setattr(networth_recompute, "today", lambda: day)
    monkeypatch.setattr(reports_service, "today", lambda: day)


def _build(db: Session, household_id: uuid.UUID, accounts: dict[str, Account]) -> None:
    make_snapshot(db, accounts["checking"], as_of_date=date(2026, 6, 1), balance="1000.00")
    make_transaction(db, accounts["checking"], amount="-200.00", posted_date=date(2026, 6, 10))
    make_transaction(db, accounts["checking"], amount="3000.00", posted_date=date(2026, 6, 20))
    make_transaction(db, accounts["savings"], amount="1000.00", posted_date=date(2026, 5, 1))
    make_fx_rate(db, household_id, base="EUR", quote="USD", rate="1.10", rate_date=date(2026, 5, 1))
    make_transaction(db, accounts["card"], amount="-500.00", posted_date=date(2026, 6, 15))
    brokerage = accounts["brokerage"]
    make_transaction(db, brokerage, amount="1000.00", posted_date=date(2026, 3, 1))
    security = make_security(db, household_id)
    make_investment_txn(
        db, brokerage, security=security, trade_date=date(2026, 3, 2), quantity="10", price="100"
    )
    make_price(db, security, close="120.00", price_date=date(2026, 6, 29))
    loan = make_loan(
        db,
        household_id,
        account=accounts["mortgage"],
        principal_original="200000.00",
        origination_date=date(2024, 3, 1),
    )
    make_loan_payment(db, loan, paid_on=date(2026, 6, 1), principal="1000.00")
    with household_context(db, household_id):
        loans_service.sync_balance(db, loan)
    make_asset_valuation(db, accounts["property"], value="350000.00", valued_on=date(2026, 1, 15))


@pytest.fixture
def world(
    actors: Actors,
    ledger: LedgerFactories,
    factories: Factories,
    monkeypatch: pytest.MonkeyPatch,
) -> World:
    freeze(monkeypatch)
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    member = actors.new(role="member", household=household)
    viewer = actors.new(role="viewer", household=household)
    hid = household.id
    accounts = {
        "checking": ledger.account(hid, type=AccountType.CHECKING, name="Checking"),
        "savings": ledger.account(hid, type=AccountType.SAVINGS, currency="EUR", name="Savings"),
        "card": ledger.account(hid, type=AccountType.CREDIT_CARD, name="Card"),
        "brokerage": ledger.account(hid, type=AccountType.BROKERAGE, name="Brokerage"),
        "mortgage": ledger.account(hid, type=AccountType.MORTGAGE, name="Mortgage"),
        "property": ledger.account(
            hid, type=AccountType.MANUAL_ASSET, asset_kind="property", name="House"
        ),
    }
    hidden = ledger.account(hid, type=AccountType.CHECKING, name="Hidden")
    ledger.transaction(hidden, amount="5000.00", posted_date=date(2026, 6, 1))
    ledger.run(_build, hid, accounts)
    for account in accounts.values():
        ledger.grant(hid, member.record.id, account, GrantLevel.VIEW)
        ledger.grant(hid, viewer.record.id, account, GrantLevel.VIEW)
    ledger.grant(hid, viewer.record.id, hidden, GrantLevel.VIEW)
    return World(owner, member, viewer, hid, accounts, hidden, ledger, factories)


def _get(client: AuthedClient, url: str, status: int = 200, **params: Any) -> Any:
    response = client.get(url, params=params)
    assert response.status_code == status, response.text
    return response.json()


def _amount(money: dict[str, str]) -> Decimal:
    return D(money["amount"])


def _snapshot(world: World, day: date = TODAY) -> int:
    with world.factories.transaction() as db:
        set_context(db, world.household_id)
        return networth_service.snapshot_household(db, world.household_id, day)


def _snapshots(world: World) -> list[NetWorthSnapshot]:
    with world.factories.transaction() as db:
        set_context(db, world.household_id)
        return list(
            db.execute(select(NetWorthSnapshot).order_by(NetWorthSnapshot.as_of_date)).scalars()
        )


def test_latest_owner_gets_household_figures_and_breakdown(world: World) -> None:
    body = _get(world.owner, f"{NW}/latest")
    assert body["scope"] == "household"
    assert body["currency"] == "USD"
    assert body["as_of"] == TODAY.isoformat()
    assert _amount(body["assets"]) == D("361100.00")
    assert _amount(body["liabilities"]) == D("199500.00")
    assert _amount(body["net"]) == D("161600.00")
    by_class = body["by_class"]
    assert {k: _amount(v) for k, v in by_class["assets"].items()} == {
        "cash": D("9900.00"),
        "investments": D("1200.00"),
        "real_estate": D("350000.00"),
    }
    assert {k: _amount(v) for k, v in by_class["liabilities"].items()} == {
        "credit_cards": D("500.00"),
        "mortgages": D("199000.00"),
    }
    assert _amount(body["by_type"]["checking"]) == D("8800.00")
    accounts = {a["name"]: a for a in body["accounts"]}
    savings = accounts["Savings"]
    assert savings["amount"] == {"amount": "1000.00", "currency": "EUR"}
    assert savings["amount_base"] == {"amount": "1100.00", "currency": "USD"}
    assert accounts["Mortgage"]["side"] == "liability"
    assert accounts["House"]["account_class"] == "real_estate"
    assert body["unconverted"] == []
    assert body["unpriced_security_ids"] == []
    assert body["last_snapshot_date"] is None


def test_member_gets_only_visible_accounts_viewer_with_every_grant_gets_household(
    world: World,
) -> None:
    member = _get(world.member, f"{NW}/latest")
    assert member["scope"] == "visible_accounts"
    assert _amount(member["net"]) == D("156600.00")
    assert str(world.hidden.id) not in {a["account_id"] for a in member["accounts"]}
    assert member["last_snapshot_date"] is None
    viewer = _get(world.viewer, f"{NW}/latest")
    assert viewer["scope"] == "household"
    assert _amount(viewer["net"]) == D("161600.00")


def test_stored_snapshots_are_never_shown_to_a_partial_member(world: World) -> None:
    # A stored snapshot names every account; a planted one shows who reads it.
    world.ledger.run(
        make_net_worth_snapshot,
        world.household_id,
        as_of_date=date(2026, 6, 29),
        assets="1.00",
        liabilities="0",
    )
    owner = _get(world.owner, NW, range="1m")
    by_day = {p["as_of"]: p for p in owner["points"]}
    assert by_day["2026-06-29"]["source"] == "snapshot"
    assert _amount(by_day["2026-06-29"]["net"]) == D("1.00")
    member = _get(world.member, NW, range="1m")
    assert member["scope"] == "visible_accounts"
    point = {p["as_of"]: p for p in member["points"]}["2026-06-29"]
    assert point["source"] == "computed"
    assert _amount(point["net"]) == D("156600.00")
    assert _get(world.member, f"{NW}/latest")["last_snapshot_date"] is None
    assert _get(world.owner, f"{NW}/latest")["last_snapshot_date"] == "2026-06-29"


def test_snapshot_job_backfills_once_and_is_idempotent(world: World) -> None:
    first_day = date(2024, 3, 1)  # the mortgage's origination: the first data
    written = _snapshot(world)
    assert written == (TODAY - first_day).days + 1
    rows = _snapshots(world)
    assert len(rows) == written
    assert rows[0].as_of_date == first_day
    assert (rows[0].assets, rows[0].liabilities, rows[0].net) == (
        D("0"),
        D("200000.00"),
        D("-200000.00"),
    )
    last = rows[-1]
    assert (last.as_of_date, last.net, last.currency) == (TODAY, D("161600.00"), "USD")
    breakdown = last.breakdown
    assert breakdown["version"] == 1
    assert breakdown["by_class"]["liabilities"]["mortgages"] == "199000.000000"
    assert {a["account_id"] for a in breakdown["accounts"]} >= {str(world.hidden.id)}
    assert breakdown["unconverted"] == []
    # Again the same day: the day before and the day rewritten, nothing added.
    assert _snapshot(world) == 2
    assert len(_snapshots(world)) == written
    # The next day fills that day and rewrites the last stored one.
    assert _snapshot(world, TODAY + timedelta(days=1)) == 2


def test_history_uses_snapshots_for_household_scope(world: World) -> None:
    _snapshot(world)
    body = _get(world.owner, NW, range="1m")
    assert body["granularity"] == "day"
    assert body["date_from"] == "2026-05-30"
    points = body["points"]
    assert len(points) == 32
    assert {p["source"] for p in points[:-1]} == {"snapshot"}
    assert points[-1]["source"] == "computed"
    assert points[-1]["as_of"] == TODAY.isoformat()
    # 2026-05-30: checking had no anchor yet (0), savings 1100, brokerage 1000
    # cash - 1000 buy + 10 * 100 (latest trade price), house 350000,
    # mortgage 200000.
    assert _amount(points[0]["net"]) == D("152100.00")
    assert _amount(body["change"]) == D("161600.00") - D("152100.00")


def test_history_granularity_and_limits(world: World) -> None:
    body = _get(world.member, NW, range="max", granularity="month")
    assert body["date_from"] == "2024-03-01"
    assert body["points"][0]["as_of"] == "2024-03-31"
    assert body["points"][-1]["as_of"] == TODAY.isoformat()
    assert len(body["points"]) == 28
    weekly = _get(world.member, NW, range="3m", granularity="week")
    assert all(p["source"] == "computed" for p in weekly["points"])
    auto = _get(world.member, NW, range="5y")
    assert auto["granularity"] == "week"
    refused = world.member.get(NW, params={"range": "5y", "granularity": "day"})
    assert refused.status_code == 422
    assert problem_type(refused) == "too-many-points"
    assert world.member.get(NW, params={"range": "2w"}).status_code == 422


def test_unconverted_currency_is_listed_not_counted(world: World) -> None:
    gbp = world.ledger.account(world.household_id, type=AccountType.SAVINGS, currency="GBP")
    world.ledger.transaction(gbp, amount="700.00", posted_date=date(2026, 6, 2))
    body = _get(world.owner, f"{NW}/latest")
    assert body["unconverted"] == [str(gbp.id)]
    assert _amount(body["net"]) == D("161600.00")
    entry = next(a for a in body["accounts"] if a["account_id"] == str(gbp.id))
    assert entry["amount_base"] is None


def test_closed_and_excluded_accounts_are_left_out(world: World) -> None:
    with world.factories.transaction() as db:
        set_context(db, world.household_id)
        card = db.get(Account, world.accounts["card"].id)
        assert card is not None
        card.include_in_net_worth = False
        hidden = db.get(Account, world.hidden.id)
        assert hidden is not None
        hidden.closed_on = date(2026, 6, 15)
    body = _get(world.owner, f"{NW}/latest")
    assert _amount(body["net"]) == D("161600.00") + D("500.00") - D("5000.00")


def test_brokerage_statement_anchor_carries_the_market_move(world: World) -> None:
    # A statement says the whole account was worth 1500.00 on 2026-06-01
    # (when the close was the 100.00 trade price): +200.00 since then.
    world.ledger.snapshot(
        world.accounts["brokerage"], as_of_date=date(2026, 6, 1), balance="1500.00"
    )
    body = _get(world.owner, f"{NW}/latest")
    entry = next(a for a in body["accounts"] if a["name"] == "Brokerage")
    assert entry["amount"]["amount"] == "1700.00"


def _first_statement(
    db: Session, household_id: uuid.UUID, account: Account, *, opening: str, hashed: str
) -> None:
    """An account's first statement for June, as the statement importer records it.

    It opened with 1500.00 (10 units, held before June), recorded the day
    before the period; the 10 units are a zero-amount transfer in dated on
    the period's first day, costed at 80.00 a unit (the cost basis, not the
    market price). On 06-10 750.00 is paid in and 5 more units are bought at
    the market price of 150.00. ``opening`` is the stored close on 05-31
    ("" for none); ``hashed``: ``opening`` (the importer's opening hash),
    ``none`` (a transfer in a person added) or ``other`` (an imported row).
    """
    make_snapshot(
        db,
        account,
        as_of_date=date(2026, 5, 31),
        balance="1500.00",
        source=BalanceSource.STATEMENT,
    )
    security = make_security(db, household_id)
    if opening:
        make_price(db, security, close=opening, price_date=date(2026, 5, 31))
    digest = {
        "opening": opening_import_hash(account.id, security.id, date(2026, 6, 1)),
        "none": None,
        "other": "e" * 64,
    }[hashed]
    make_investment_txn(
        db,
        account,
        security=security,
        type=InvestmentTxnType.TRANSFER_IN,
        trade_date=date(2026, 6, 1),
        quantity="10",
        price="80",
        amount="0",
        memo="Opening position from statement",
        import_hash=digest,
    )
    make_transaction(db, account, amount="750.00", posted_date=date(2026, 6, 10))
    make_investment_txn(
        db, account, security=security, trade_date=date(2026, 6, 10), quantity="5", price="150"
    )


def _values(world: World, account: Account, days: list[date]) -> list[Decimal]:
    with world.factories.transaction() as db:
        set_context(db, world.household_id)
        book = NetWorthBook.load(db, world.household_id, [account.id], target="USD", until=TODAY)
        return [book.values_on(day)[account.id] for day in days]


DAYS = [date(2026, 5, 31), date(2026, 6, 1), date(2026, 6, 5), date(2026, 6, 20)]


def test_a_first_statement_s_opening_positions_are_held_on_its_opening_day(world: World) -> None:
    # The opening value already holds the 10 units: they are not added to it
    # on the period's first day. 06-20: 1500.00 + the 750.00 paid in.
    account = world.ledger.account(world.household_id, type=AccountType.BROKERAGE)
    world.ledger.run(_first_statement, world.household_id, account, opening="", hashed="opening")
    assert _values(world, account, DAYS) == [D("1500"), D("1500"), D("1500"), D("2250")]
    # With a close known on the opening day, the units' market move counts:
    # 10 x (150.00 - 140.00) more by 06-20.
    other = world.ledger.account(world.household_id, type=AccountType.BROKERAGE)
    world.ledger.run(_first_statement, world.household_id, other, opening="140", hashed="opening")
    assert _values(world, other, DAYS) == [D("1500"), D("1500"), D("1500"), D("2350")]


@pytest.mark.parametrize("hashed", ["none", "other"])
def test_a_transfer_in_after_an_anchor_still_adds_its_units(world: World, hashed: str) -> None:
    # Units that really arrived on 06-01 (a person's row, or an imported
    # transfer) are on top of what the account held: 10 x 80.00, then
    # 15 x 150.00 + 750.00 - 750.00.
    account = world.ledger.account(world.household_id, type=AccountType.BROKERAGE)
    world.ledger.run(_first_statement, world.household_id, account, opening="", hashed=hashed)
    assert _values(world, account, DAYS) == [D("1500"), D("2300"), D("2300"), D("3750")]


def test_accounts_value_an_investment_account_as_net_worth_does(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ledger alone says 1000.00 (the deposit); the account is worth its
    # cash (0.00 after the buy) plus its holdings (10 x 120.00).
    monkeypatch.setattr(accounts_service, "today", lambda: TODAY)
    brokerage = world.accounts["brokerage"]
    listed = {a["name"]: a for a in _get(world.owner, f"{API}/accounts")["items"]}
    assert listed["Brokerage"]["balance"] == {"amount": "1200.00", "currency": "USD"}
    assert listed["Checking"]["balance"]["amount"] == "3800.00"
    single = _get(world.owner, f"{API}/accounts/{brokerage.id}")
    assert single["balance"]["amount"] == "1200.00"
    # Left out of net worth, it is still worth what it holds.
    with world.factories.transaction() as db:
        set_context(db, world.household_id)
        row = db.get(Account, brokerage.id)
        assert row is not None
        row.include_in_net_worth = False
    single = _get(world.owner, f"{API}/accounts/{brokerage.id}")
    assert single["balance"]["amount"] == "1200.00"


def test_an_investment_account_s_history_is_valued_as_net_worth_does(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ledger alone says 1000.00 every day from the deposit on; the
    # account is worth its cash plus its holdings on each day, priced at the
    # latest close (or trade price) on or before it.
    monkeypatch.setattr(accounts_service, "today", lambda: TODAY)
    brokerage = world.accounts["brokerage"]
    body = _get(
        world.owner,
        f"{API}/accounts/{brokerage.id}/balances",
        start="2026-02-28",
        end="2026-06-30",
    )
    by_day = {p["date"]: p["balance"]["amount"] for p in body["items"]}
    assert len(by_day) == (TODAY - date(2026, 2, 28)).days + 1
    assert by_day["2026-02-28"] == "0.00"
    assert by_day["2026-03-01"] == "1000.00"  # the deposit, nothing bought yet
    assert by_day["2026-03-02"] == "1000.00"  # 0.00 cash + 10 x 100.00
    assert by_day["2026-06-28"] == "1000.00"
    assert by_day["2026-06-29"] == "1200.00"  # the close of 120.00
    assert by_day["2026-06-30"] == "1200.00"
    # The last point is the balance the account shows.
    single = _get(world.owner, f"{API}/accounts/{brokerage.id}")
    assert by_day["2026-06-30"] == single["balance"]["amount"]
    # A bank account's history is its ledger, as before.
    checking = world.accounts["checking"]
    bank = _get(
        world.owner,
        f"{API}/accounts/{checking.id}/balances",
        start="2026-06-01",
        end="2026-06-30",
    )
    assert bank["items"][-1]["balance"]["amount"] == "3800.00"


def test_projection(world: World) -> None:
    owner = world.owner
    body = _get(owner, f"{NW}/projection", months=12, basis_months=3)
    assert body["months"] == 12
    assert body["basis_months"] == 3
    assert _amount(body["current"]) == D("161600.00")
    # Whole months March-May: the +1000.00 brokerage deposit (2026-03-01) and
    # the +1000.00 EUR savings deposit (2026-05-01, 1100.00 USD); no outflows
    # (the buy is an investment transaction, not a ledger row).
    savings = body["savings"]
    monthly = _amount(savings["average_monthly_savings"])
    assert monthly == D("700.00")  # (1000 + 1100) / 3
    assert savings["savings_rate"] == "1.0000"
    assert len(savings["points"]) == 12
    assert savings["points"][0]["on"] == "2026-07-30"
    assert _amount(savings["points"][11]["net"]) == D("161600.00") + 12 * monthly
    trend = body["trend"]
    assert trend is not None
    assert len(trend["points"]) == 12
    assert _amount(trend["slope_per_month"]) > 0
    member = _get(world.member, f"{NW}/projection")
    assert member["scope"] == "visible_accounts"
    assert _amount(member["current"]) == D("156600.00")
    assert world.owner.get(f"{NW}/projection", params={"months": 0}).status_code == 422


def test_daily_dispatcher_enqueues_one_snapshot_per_household_and_day(world: World) -> None:
    assert any(j.job_type == "snapshot_net_worth" for j in SCHEDULE.jobs())
    other = world.factories.user(role="owner")
    for _ in range(2):
        with world.factories.transaction() as db:
            set_context(db, None)
            ctx = JobContext(
                job_id=uuid.uuid4(),
                job_type=networth_handler.DISPATCH_JOB_TYPE,
                attempt=1,
                max_attempts=1,
                household_id=None,
                session=db,
                deadline=datetime.max.replace(tzinfo=UTC),
            )
            networth_handler.snapshot_net_worth(ctx, networth_handler.SnapshotNetWorthPayload())
    for household_id in (world.household_id, other.household_id):
        assert household_id is not None
        with world.factories.transaction() as db:
            set_context(db, household_id)
            jobs = list(
                db.execute(
                    select(Job).where(Job.type == networth_handler.SNAPSHOT_JOB_TYPE)
                ).scalars()
            )
        assert len(jobs) == 1
        assert jobs[0].household_id == household_id
        assert jobs[0].dedupe_key is not None
        assert jobs[0].dedupe_key.startswith(f"networth:{household_id}:")


def test_household_job_writes_the_snapshot(world: World) -> None:
    with world.factories.transaction() as db:
        set_context(db, world.household_id)
        ctx = JobContext(
            job_id=uuid.uuid4(),
            job_type=networth_handler.SNAPSHOT_JOB_TYPE,
            attempt=1,
            max_attempts=1,
            household_id=world.household_id,
            session=db,
            deadline=datetime.max.replace(tzinfo=UTC),
        )
        networth_handler.snapshot_household(
            ctx, networth_handler.HouseholdSnapshotPayload(as_of=TODAY)
        )
        count = db.execute(select(func.count()).select_from(NetWorthSnapshot)).scalar_one()
    assert count == (TODAY - date(2024, 3, 1)).days + 1


def _run_job(world: World, household_id: uuid.UUID) -> None:
    """Run every queued ``networth.snapshot`` job of ``household_id``, as the worker would."""
    with world.factories.transaction() as db:
        set_context(db, household_id)
        jobs = list(
            db.execute(
                select(Job).where(
                    Job.type == networth_handler.SNAPSHOT_JOB_TYPE, Job.state == "queued"
                )
            ).scalars()
        )
        for job in jobs:
            ctx = JobContext(
                job_id=job.id,
                job_type=job.type,
                attempt=1,
                max_attempts=1,
                household_id=job.household_id,
                session=db,
                deadline=datetime.max.replace(tzinfo=UTC),
            )
            networth_handler.snapshot_household(
                ctx, networth_handler.HouseholdSnapshotPayload.model_validate(job.payload)
            )
            db.delete(job)


def _stored(world: World) -> dict[date, Decimal]:
    return {row.as_of_date: row.net for row in _snapshots(world)}


def test_a_backdated_import_rewrites_the_stored_days_from_its_first_day(
    world: World, actors: Actors
) -> None:
    _snapshot(world)
    before = _stored(world)
    # A statement of June, imported today: 400.00 paid on 06-12.
    world.ledger.transaction(
        world.accounts["checking"], amount="-400.00", posted_date=date(2026, 6, 12)
    )
    # The daily job alone rewrites today and the day before, and keeps what
    # was known on each earlier day.
    _snapshot(world)
    yesterday = TODAY - timedelta(days=1)
    assert {d: n for d, n in _stored(world).items() if d < yesterday} == {
        d: n for d, n in before.items() if d < yesterday
    }
    # Another household with stored days of its own, and a job queued for each.
    stranger = actors.new(role="owner")
    other = stranger.record.household
    assert other is not None
    ledger = world.ledger
    theirs = ledger.account(other.id, type=AccountType.CHECKING)
    ledger.transaction(theirs, amount="70.00", posted_date=date(2026, 6, 1))
    with world.factories.transaction() as db:
        set_context(db, other.id)
        networth_service.snapshot_household(db, other.id, TODAY)
    for household_id in (world.household_id, other.id):
        with world.factories.transaction() as db:
            set_context(db, household_id)
            networth_service.request_recompute(db, household_id, date(2026, 6, 12))
    _run_job(world, world.household_id)
    stored = _stored(world)
    assert stored == {
        day: net - (D("400.00") if day >= date(2026, 6, 12) else 0) for day, net in before.items()
    }
    assert stored[TODAY] == D("161200.00")
    # The other household's job has not run: its rows are its own and as they were.
    with world.factories.transaction() as db:
        set_context(db, other.id)
        rows = list(db.execute(select(NetWorthSnapshot)).scalars())
        queued = db.execute(
            select(func.count()).select_from(Job).where(Job.household_id == other.id)
        ).scalar_one()
    assert {r.household_id for r in rows} == {other.id}
    assert {r.net for r in rows} == {D("70.00")}
    assert queued == 1


def test_data_older_than_the_backfill_window_is_not_rewritten_every_day(world: World) -> None:
    # The mortgage reaches back before a 30-day window: the first run fills
    # the window, the next one only its own day and the one before.
    with world.factories.transaction() as db:
        set_context(db, world.household_id)
        assert networth_service.snapshot_household(db, world.household_id, TODAY, max_days=30) == 30
        assert networth_service.snapshot_household(db, world.household_id, TODAY, max_days=30) == 2
        # A row from older rules before the window stays as it is.
        make_net_worth_snapshot(db, world.household_id, as_of_date=date(2026, 1, 2))
        assert networth_service.snapshot_household(db, world.household_id, TODAY, max_days=30) == 2


def test_a_day_stored_in_a_former_base_currency_stays_as_recorded(world: World) -> None:
    _snapshot(world)
    with world.factories.transaction() as db:
        set_context(db, world.household_id)
        old = db.execute(
            select(NetWorthSnapshot).where(NetWorthSnapshot.as_of_date == date(2026, 5, 10))
        ).scalar_one()
        old.currency, old.assets, old.liabilities, old.net = "EUR", D("10"), D("0"), D("10")
        old.breakdown = {"version": 1, "accounts": []}
    with world.factories.transaction() as db:
        set_context(db, world.household_id)
        # Rewritten from before it: the row stays; and, though it carries no
        # valuation version, it does not make the next run start again.
        networth_service.snapshot_household(db, world.household_id, TODAY, since=date(2026, 5, 1))
        assert networth_service.snapshot_household(db, world.household_id, TODAY) == 2
        row = db.execute(
            select(NetWorthSnapshot).where(NetWorthSnapshot.as_of_date == date(2026, 5, 10))
        ).scalar_one()
    assert (row.currency, row.net) == ("EUR", D("10.00"))


def test_the_snapshot_job_payload_names_since_only_when_set() -> None:
    # A daily job queued before going back to rc.11 must still be readable there.
    payload = networth_handler.HouseholdSnapshotPayload(as_of=TODAY)
    assert payload.model_dump(mode="json") == {"as_of": "2026-06-30"}
    since = networth_handler.HouseholdSnapshotPayload(as_of=TODAY, since=date(2026, 6, 1))
    assert since.model_dump(mode="json") == {"as_of": "2026-06-30", "since": "2026-06-01"}


def test_stored_days_valued_by_older_rules_are_rewritten(world: World) -> None:
    # A row from before this version (no "valuation" in its breakdown) and
    # one from it: the job rewrites from the older one on.
    _snapshot(world)
    before = _stored(world)
    with world.factories.transaction() as db:
        set_context(db, world.household_id)
        for row in db.execute(select(NetWorthSnapshot)).scalars():
            if row.as_of_date >= date(2026, 6, 1):
                row.net = D("1.00")
                row.assets = D("1.00")
                row.liabilities = D("0")
                if row.as_of_date == date(2026, 6, 20):
                    row.breakdown = {"version": 1, "accounts": []}
    written = _snapshot(world)
    assert written == (TODAY - date(2026, 6, 20)).days + 1
    stored = _stored(world)
    assert stored[date(2026, 6, 19)] == D("1.00")
    assert all(stored[day] == before[day] for day in before if day >= date(2026, 6, 20))
    # Rewritten with this version: nothing left to rewrite but the last days.
    assert _snapshot(world) == 2


def _live(world: World) -> dict[date, Decimal]:
    """What every stored day is worth by the data held now."""
    with world.factories.transaction() as db:
        set_context(db, world.household_id)
        book = NetWorthBook.load(db, world.household_id, None, target="USD", until=TODAY)
        return {day: book.on(day).net for day in _stored(world)}


def _named(row: NetWorthSnapshot) -> list[str]:
    return [a["account_id"] for a in row.breakdown["accounts"]]


def test_deleting_an_account_takes_it_out_of_the_stored_history(world: World) -> None:
    _snapshot(world)
    before = _stored(world)
    hidden = world.hidden
    response = world.owner.delete(f"{API}/accounts/{hidden.id}")
    assert response.status_code == 204, response.text
    _run_job(world, world.household_id)
    # Deleting removes everything recorded on it, its place in the history
    # too (closing an account is how it stays): 5000.00 from 06-01 is gone.
    stored = _stored(world)
    assert stored == {
        day: net - (D("5000.00") if day >= date(2026, 6, 1) else 0) for day, net in before.items()
    }
    assert stored == _live(world)
    assert all(str(hidden.id) not in _named(row) for row in _snapshots(world))


_STATEMENT_ROWS = [
    ["Date", "Description", "Amount"],
    ["06/05/2026", "Northfield Payroll", "10000.00"],
]
_STATEMENT_SPEC = {
    "date_column": 0,
    "date_format": "MM/DD/YYYY",
    "payee_column": 1,
    "amount_column": 2,
}


def _import_statement(world: World, account: Account) -> None:
    """Import a June statement (10000.00 on 06-05) into ``account``, as the importer would."""
    ledger, hid = world.ledger, world.household_id
    mapping = ledger.run(
        make_import_mapping, hid, spec=_STATEMENT_SPEC, rows=_STATEMENT_ROWS, account=account
    )
    result = table_result(_STATEMENT_ROWS)
    document = ledger.run(make_extracted_document, hid, account=account, result=result)
    with world.factories.transaction() as db:
        set_context(db, hid)
        found = db.get(Document, document.id)
        assert found is not None
        imports_service.import_extraction(
            db, found, result, mapping_id=mapping.id, account_id=account.id
        )


def test_a_statement_moved_to_another_account_by_deleting_the_first_counts_once(
    world: World,
) -> None:
    # Imported into the wrong account, which is then deleted, and imported
    # again into the right one: every stored day says what the ledger does.
    _snapshot(world)
    before = _stored(world)
    wrong = world.ledger.account(world.household_id, type=AccountType.CHECKING, name="Wrong")
    _import_statement(world, wrong)
    _run_job(world, world.household_id)
    assert _stored(world)[date(2026, 6, 5)] == before[date(2026, 6, 5)] + D("10000.00")
    response = world.owner.delete(f"{API}/accounts/{wrong.id}")
    assert response.status_code == 204, response.text
    _run_job(world, world.household_id)
    right = world.ledger.account(world.household_id, type=AccountType.CHECKING, name="Right")
    _import_statement(world, right)
    _run_job(world, world.household_id)
    stored = _stored(world)
    assert stored == {
        day: net + (D("10000.00") if day >= date(2026, 6, 5) else 0) for day, net in before.items()
    }
    assert stored == _live(world)
    assert all(str(wrong.id) not in _named(row) for row in _snapshots(world))


def test_other_household_sees_none_of_it(world: World, actors: Actors) -> None:
    _snapshot(world)
    stranger = actors.new(role="owner")
    body = _get(stranger, f"{NW}/latest")
    assert body["accounts"] == []
    assert _amount(body["net"]) == 0
    history = _get(stranger, NW, range="1m")
    assert {_amount(p["net"]) for p in history["points"]} == {D(0)}


def _queued(world: World) -> list[Job]:
    with world.factories.transaction() as db:
        set_context(db, world.household_id)
        jobs = list(
            db.execute(
                select(Job)
                .where(Job.type == networth_handler.SNAPSHOT_JOB_TYPE, Job.state == "queued")
                .order_by(Job.created_at)
            ).scalars()
        )
        db.expunge_all()
        return jobs


def _request(world: World, since: date) -> None:
    with world.factories.transaction() as db:
        set_context(db, world.household_id)
        networth_service.request_recompute(db, world.household_id, since)


def test_rewrites_asked_for_while_one_waits_become_one_from_the_earliest_day(
    world: World,
) -> None:
    # The day's own job (keyed) is left alone; the rewrites asked for while
    # one is waiting fold into it, from the earliest day any of them names.
    with world.factories.transaction() as db:
        set_context(db, world.household_id)
        enqueue(
            db,
            networth_handler.SNAPSHOT_JOB_TYPE,
            networth_handler.HouseholdSnapshotPayload(as_of=TODAY),
            household_id=world.household_id,
            dedupe_key=networth_handler.dedupe_key(world.household_id, TODAY),
        )
    _request(world, date(2026, 6, 12))
    _request(world, date(2026, 6, 20))
    _request(world, date(2026, 5, 3))
    _request(world, date(2026, 6, 1))
    jobs = _queued(world)
    assert [j.dedupe_key is not None for j in jobs] == [True, False]
    assert jobs[1].payload == {"as_of": TODAY.isoformat(), "since": "2026-05-03"}
    # A day not before today needs nothing: the next daily run rewrites the
    # last stored day.
    _request(world, TODAY)
    assert len(_queued(world)) == 2


def test_a_running_rewrite_does_not_swallow_a_later_one(world: World) -> None:
    _request(world, date(2026, 6, 12))
    [job] = _queued(world)
    with world.factories.transaction() as db:
        set_context(db, None)
        with queue_scope(db):
            db.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(
                    state="running",
                    locked_by="w",
                    locked_at=func.now(),
                    lease_expires_at=func.now(),
                )
            )
    _request(world, date(2026, 6, 20))
    [queued] = _queued(world)
    assert queued.id != job.id
    assert queued.payload["since"] == "2026-06-20"


def test_a_row_dated_today_reaches_the_stored_day_on_the_next_run(world: World) -> None:
    _snapshot(world)
    world.ledger.transaction(world.accounts["checking"], amount="-40.00", posted_date=TODAY)
    _snapshot(world, TODAY + timedelta(days=1))
    assert _stored(world)[TODAY] == D("161560.00")


def test_the_snapshot_job_holds_the_household_s_lock(world: World) -> None:
    # Two runs of one household one after the other, never side by side: the
    # later one reads the book only once the earlier one has committed.
    with world.factories.transaction() as db:
        set_context(db, world.household_id)
        networth_service.snapshot_household(db, world.household_id, TODAY)
        with world.factories.transaction() as other:
            taken = other.execute(
                text("SELECT pg_try_advisory_xact_lock(:ns, hashtext(:h))"),
                {"ns": networth_service.SNAPSHOT_LOCK_NAMESPACE, "h": str(world.household_id)},
            ).scalar_one()
    assert taken is False

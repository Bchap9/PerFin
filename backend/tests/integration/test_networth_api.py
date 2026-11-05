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
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.db.models import Job
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType
from app.domain.loans import service as loans_service
from app.domain.networth import service as networth_service
from app.domain.networth.models import NetWorthSnapshot
from app.domain.reports import service as reports_service
from app.workers.handlers import networth as networth_handler
from app.workers.registry import JobContext
from app.workers.scheduler import SCHEDULE
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, problem_type
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
    # Again the same day: one row rewritten, nothing added.
    assert _snapshot(world) == 1
    assert len(_snapshots(world)) == written
    # The next day fills only that day.
    assert _snapshot(world, TODAY + timedelta(days=1)) == 1


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


def test_other_household_sees_none_of_it(world: World, actors: Actors) -> None:
    _snapshot(world)
    stranger = actors.new(role="owner")
    body = _get(stranger, f"{NW}/latest")
    assert body["accounts"] == []
    assert _amount(body["net"]) == 0
    history = _get(stranger, NW, range="1m")
    assert {_amount(p["net"]) for p in history["points"]} == {D(0)}

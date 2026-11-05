# SPDX-License-Identifier: AGPL-3.0-or-later
"""Business dates are bounded below and day-walking reads are capped (SECURITY.md F8).

Security review P56-SR-R1-01: a member could record a buy dated 0001-01-02;
``GET /investments/performance?range=max`` then valued 739 796 days (17 s per
request) and ``GET /networth?range=max`` was a permanent ``422``. Every
layer is checked here: the API refuses such dates, the database refuses them,
statement imports and price/FX feeds hold or drop them, and the
performance period and the ``max`` net worth history stay bounded even over
a history that legitimately reaches back to 1900. All data is synthetic.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import GrantLevel
from app.core.dates import MIN_BUSINESS_DATE
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType
from app.domain.documents.models import ReviewKind
from app.domain.fx.models import FxRate
from app.domain.investments import service as investments_service
from app.domain.investments.models import InvestmentTxn, Price, Security
from app.domain.investments.performance import MAX_PERFORMANCE_DAYS
from app.domain.investments.statement_import import import_investment_statements
from app.domain.networth import service as networth_service
from app.domain.networth.schemas import MAX_POINTS
from app.domain.reports import service as reports_service
from app.ingestion.sandbox.protocol import (
    InvestmentActivity,
    InvestmentStatement,
    SecurityRef,
)
from app.integrations.fx.base import FxQuote
from app.integrations.fx.store import store_rates
from app.integrations.prices.base import PriceQuote
from app.integrations.prices.store import store_quotes
from tests.fixtures.factories import AuthedClient, Factories, make_household
from tests.fixtures.households import API, Actors
from tests.fixtures.imports import make_extracted_document
from tests.fixtures.ledger import LedgerFactories, household_context, make_account, make_fx_rate
from tests.fixtures.wealth import (
    make_asset_valuation,
    make_brokerage,
    make_investment_txn,
    make_loan,
    make_loan_payment,
    make_loan_rate_change,
    make_lot,
    make_manual_asset,
    make_price,
    make_security,
)

pytestmark = pytest.mark.integration

TXNS = f"{API}/investments/transactions"
SECURITIES = f"{API}/securities"
PERFORMANCE = f"{API}/investments/performance"
RATES = f"{API}/fx-rates"
NETWORTH = f"{API}/networth"
TODAY = date(2026, 6, 30)
TOO_OLD = MIN_BUSINESS_DATE - timedelta(days=1)


@dataclass(frozen=True)
class World:
    owner: AuthedClient
    member: AuthedClient
    household_id: uuid.UUID
    brokerage: Account


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


@pytest.fixture
def world(actors: Actors, ledger: LedgerFactories, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setattr(investments_service, "today", lambda: TODAY)
    monkeypatch.setattr(networth_service, "today", lambda: TODAY)
    monkeypatch.setattr(reports_service, "today", lambda: TODAY)
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    member = actors.new(role="member", household=household)
    brokerage = ledger.account(household.id, type=AccountType.BROKERAGE, name="Brokerage")
    ledger.grant(household.id, member.record.id, brokerage, GrantLevel.EDIT)
    return World(owner, member, household.id, brokerage)


def _security(client: AuthedClient, symbol: str) -> dict[str, Any]:
    body = {
        "symbol": symbol,
        "name": f"Synthetic {symbol}",
        "asset_class": "equity",
        "currency": "USD",
    }
    response = client.post(SECURITIES, json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def _buy(world: World, security: dict[str, Any], day: str) -> dict[str, Any]:
    return {
        "account_id": str(world.brokerage.id),
        "security_id": security["id"],
        "type": "buy",
        "trade_date": day,
        "quantity": "10",
        "price": "100",
        "amount": {"amount": "-1000", "currency": "USD"},
    }


def _txn_count(factories: Factories, household_id: uuid.UUID) -> int:
    with factories.transaction() as db:
        set_context(db, household_id)
        return int(db.execute(select(func.count()).select_from(InvestmentTxn)).scalar_one())


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_api_refuses_dates_before_1900(world: World, factories: Factories) -> None:
    security = _security(world.member, "SYNOLD")
    for ancient in ("0001-01-02", "1000-01-02", TOO_OLD.isoformat()):
        planted = world.member.post(TXNS, json=_buy(world, security, ancient))
        assert planted.status_code == 422, planted.text
        settled = world.member.post(
            TXNS, json={**_buy(world, security, "2026-01-02"), "settle_date": ancient}
        )
        assert settled.status_code == 422, settled.text
        price = world.owner.post(
            f"{SECURITIES}/{security['id']}/prices", json={"price_date": ancient, "close": "1"}
        )
        assert price.status_code == 422, price.text
        rate = world.owner.post(
            RATES, json={"base": "EUR", "quote": "USD", "rate_date": ancient, "rate": "1.1"}
        )
        assert rate.status_code == 422, rate.text
    assert _txn_count(factories, world.household_id) == 0

    created = world.member.post(TXNS, json=_buy(world, security, MIN_BUSINESS_DATE.isoformat()))
    assert created.status_code == 201, created.text
    moved = world.member.patch(f"{TXNS}/{created.json()['id']}", json={"trade_date": "0001-01-02"})
    assert moved.status_code == 422, moved.text


# ---------------------------------------------------------------------------
# Day-walking reads stay bounded
# ---------------------------------------------------------------------------


def _plant_1900_history(world: World, ledger: LedgerFactories) -> None:
    """A legitimate but very old history: a buy on 1900-01-02 still held today."""

    def build(db: Session) -> None:
        security = make_security(db, world.household_id)
        make_investment_txn(
            db,
            world.brokerage,
            security=security,
            trade_date=MIN_BUSINESS_DATE + timedelta(days=1),
            quantity="10",
            price="100",
        )
        make_price(db, security, close="100.00", price_date=MIN_BUSINESS_DATE + timedelta(days=1))

    ledger.run(build)


def test_performance_max_is_capped(world: World, ledger: LedgerFactories) -> None:
    _plant_1900_history(world, ledger)
    body = world.member.get(PERFORMANCE, params={"range": "max"}).json()
    assert body["days"] == MAX_PERFORMANCE_DAYS
    assert body["start"] == (TODAY - timedelta(days=MAX_PERFORMANCE_DAYS)).isoformat()
    assert body["end"] == TODAY.isoformat()
    # The position held at the clamped start is the starting value.
    assert body["start_value"] == {"amount": "1000.00", "currency": "USD"}
    assert body["end_value"] == {"amount": "1000.00", "currency": "USD"}
    assert body["twr"] == "0.0000000000"
    assert len(body["series"]) <= 400


def test_networth_max_stays_within_the_point_cap(world: World, ledger: LedgerFactories) -> None:
    _plant_1900_history(world, ledger)
    response = world.owner.get(NETWORTH, params={"range": "max"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["granularity"] == "month"
    assert len(body["points"]) == MAX_POINTS
    assert body["points"][-1]["as_of"] == TODAY.isoformat()
    # The newest MAX_POINTS (1000) months: March 1943 to June 2026.
    assert body["date_from"] == "1943-03-01"
    # An explicit fine granularity over a long range is still refused.
    too_fine = world.owner.get(NETWORTH, params={"range": "max", "granularity": "week"})
    assert too_fine.status_code == 422


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def _refused(db: Session, household_id: uuid.UUID, write: Callable[[], object]) -> None:
    def attempt() -> None:
        with db.begin_nested():
            set_context(db, household_id)
            write()
            db.flush()

    with pytest.raises((IntegrityError, DBAPIError)) as caught:
        attempt()
    assert "_min" in str(caught.value)


def test_database_refuses_dates_before_1900(db: Session) -> None:
    hh = make_household(db)
    account = make_brokerage(db, hh.id)
    security = make_security(db, hh.id)
    loan = make_loan(db, hh.id)
    asset = make_manual_asset(db, hh.id)
    with household_context(db, hh.id):
        asset_account = db.get_one(Account, asset.account_id)
    _refused(db, hh.id, lambda: make_price(db, security, price_date=TOO_OLD))
    _refused(
        db, hh.id, lambda: make_investment_txn(db, account, security=security, trade_date=TOO_OLD)
    )
    _refused(db, hh.id, lambda: make_lot(db, account, security, open_date=TOO_OLD))
    _refused(db, hh.id, lambda: make_loan(db, hh.id, origination_date=TOO_OLD))
    _refused(db, hh.id, lambda: make_loan_payment(db, loan, paid_on=TOO_OLD))
    _refused(
        db,
        hh.id,
        lambda: make_loan_rate_change(db, loan, effective_date=TOO_OLD, new_rate="0.05"),
    )
    _refused(db, hh.id, lambda: make_asset_valuation(db, asset_account, valued_on=TOO_OLD))
    _refused(db, hh.id, lambda: make_fx_rate(db, hh.id, rate_date=TOO_OLD))
    # The bound itself is accepted.
    make_investment_txn(db, account, security=security, trade_date=MIN_BUSINESS_DATE)
    make_fx_rate(db, hh.id, rate_date=MIN_BUSINESS_DATE)


def test_check_constraints_exist_on_every_bounded_column(db: Session) -> None:
    names = set(
        db.execute(
            text("SELECT conname FROM pg_constraint WHERE contype = 'c' AND conname LIKE '%_min'")
        ).scalars()
    )
    assert {
        "ck_price_price_date_min",
        "ck_investment_txn_trade_date_min",
        "ck_lot_open_date_min",
        "ck_loan_origination_date_min",
        "ck_loan_payment_paid_on_min",
        "ck_loan_rate_change_effective_date_min",
        "ck_asset_valuation_valued_on_min",
        "ck_fx_rate_rate_date_min",
    } <= names


# ---------------------------------------------------------------------------
# Importers and feeds
# ---------------------------------------------------------------------------


def test_statement_with_an_ancient_date_is_held(db: Session) -> None:
    hh = make_household(db)
    account = make_brokerage(db, hh.id)
    document = make_extracted_document(db, hh.id, account=account)
    statement = InvestmentStatement(
        source_format="ofx",
        activity=[
            InvestmentActivity(
                type="buy",
                trade_date=date(1, 1, 2),
                security=SecurityRef(symbol="SYNANC"),
                quantity=Decimal(10),
                unit_price=Decimal(100),
                amount=Decimal(-1000),
            )
        ],
    )
    reviews: list[tuple[ReviewKind, dict[str, Any]]] = []
    with household_context(db, hh.id):
        result = import_investment_statements(
            db,
            document,
            [statement],
            allowed=None,
            review=lambda kind, detail: reviews.append((kind, detail)),
            commit_cash=lambda _statement, _account: None,
        )
        db.flush()
        written = db.execute(select(func.count()).select_from(InvestmentTxn)).scalar_one()
        created = db.execute(select(func.count()).select_from(Security)).scalar_one()
    assert (result.statements, result.held) == (0, 1)
    assert written == 0
    assert created == 0
    assert [(kind, detail["reason"]) for kind, detail in reviews] == [
        (ReviewKind.LOW_CONFIDENCE_FIELD, "investment_date_out_of_range")
    ]


def test_feeds_drop_quotes_dated_before_1900(db: Session) -> None:
    hh = make_household(db)
    make_account(db, hh.id, currency="EUR")
    security = make_security(db, hh.id, symbol="SYNFEED")
    with household_context(db, hh.id):
        prices = store_quotes(
            db,
            hh.id,
            [PriceQuote(symbol="SYNFEED", price_date=TOO_OLD, close=Decimal(5), currency="USD")],
            source="synthetic-feed",
            today=TODAY,
        )
        rates = store_rates(
            db,
            hh.id,
            [FxQuote(base="EUR", quote="USD", rate_date=TOO_OLD, rate=Decimal("1.1"))],
            source="synthetic-feed",
            today=TODAY,
        )
        stored_prices = db.execute(
            select(func.count()).select_from(Price).where(Price.security_id == security.id)
        ).scalar_one()
        stored_rates = db.execute(select(func.count()).select_from(FxRate)).scalar_one()
    assert (prices, rates) == (0, 0)
    assert (stored_prices, stored_rates) == (0, 0)

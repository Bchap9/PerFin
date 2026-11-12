# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``refresh_prices`` job against a real database (no network: respx).

Two synthetic households share a symbol: the provider is asked once for it,
both households get the price, manual prices and rates are never replaced,
and nothing is contacted while the feeds are off (the default).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.settings import Settings
from app.db.models import Event
from app.db.rls import set_context
from app.domain.fx.models import FxRate
from app.domain.investments.models import Price
from app.integrations.fx.ecb import EcbFxProvider
from app.integrations.prices.http_json import HttpJsonConfig, HttpJsonPriceProvider
from app.integrations.prices.registry import external_price_provider
from app.workers.handlers import market_data
from app.workers.handlers.market_data import (
    REFRESH_INTERVAL,
    REFRESH_JOB_TYPE,
    RefreshPricesPayload,
    refresh_interval,
    refresh_market_data,
    refresh_prices,
)
from app.workers.registry import JobContext
from app.workers.scheduler import SCHEDULE
from tests.fixtures.ledger import LedgerFactories
from tests.fixtures.market_data import (
    PUBLIC_IP,
    QUOTE_URL_AT_IP,
    ecb_xml,
    price_settings,
    quote_body,
    resolver,
)
from tests.fixtures.wealth import make_price, make_security, synthetic_symbol

pytestmark = pytest.mark.integration

TODAY = date(2026, 11, 3)
ECB_AT_IP = f"https://{PUBLIC_IP}/stats/eurofxref/eurofxref-daily.xml"


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


def _household(ledger: LedgerFactories, factories: Any) -> uuid.UUID:
    return uuid.UUID(str(factories.household().id))


def _prices(db: Session, household_id: uuid.UUID) -> dict[uuid.UUID, Price]:
    set_context(db, household_id)
    rows = db.execute(select(Price).where(Price.household_id == household_id)).scalars()
    out = {r.security_id: r for r in rows if r.price_date == TODAY}
    set_context(db, None)
    return out


def _price_events(db: Session, household_id: uuid.UUID) -> list[dict[str, Any]]:
    set_context(db, household_id)
    rows = db.execute(
        select(Event)
        .where(Event.household_id == household_id, Event.type == "price.updated")
        .order_by(Event.id)
    ).scalars()
    out = [dict(r.payload or {}) for r in rows]
    set_context(db, None)
    return out


def _price_provider(**update: Any) -> HttpJsonPriceProvider:
    config = HttpJsonConfig.from_settings(price_settings(Settings(), **update))
    return HttpJsonPriceProvider(config, resolver=resolver(), proxy=None)


def _ctx(db: Session) -> JobContext:
    return JobContext(
        job_id=uuid.uuid4(),
        job_type=REFRESH_JOB_TYPE,
        attempt=1,
        max_attempts=1,
        household_id=None,
        session=db,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


def test_refresh_is_scheduled_and_does_nothing_by_default(
    db: Session, mock: respx.MockRouter, ledger: LedgerFactories, factories: Any
) -> None:
    (job,) = [j for j in SCHEDULE.jobs() if j.job_type == REFRESH_JOB_TYPE]
    assert job.interval == REFRESH_INTERVAL
    # PERFIN_PRICE_REFRESH_MINUTES sets the interval (six hours by default).
    assert refresh_interval(Settings()) == REFRESH_INTERVAL
    assert refresh_interval(Settings(price_refresh_minutes=15)) == timedelta(minutes=15)
    hid = _household(ledger, factories)
    ledger.run(make_security, hid, symbol=synthetic_symbol())
    refresh_prices(_ctx(db), RefreshPricesPayload())
    assert mock.calls.call_count == 0
    assert _prices(db, hid) == {}
    assert refresh_market_data(db, prices=None, fx=None).households == 0


def test_one_request_per_symbol_for_the_whole_instance(
    db: Session, mock: respx.MockRouter, ledger: LedgerFactories, factories: Any
) -> None:
    shared, own, euro = synthetic_symbol(), synthetic_symbol(), synthetic_symbol()
    closes = {shared: "250.5", own: "12.125", euro: "40"}

    def answer(request: httpx.Request) -> httpx.Response:
        symbol = request.url.path.rsplit("/", 1)[-1]
        if symbol not in closes:
            return httpx.Response(404)
        return httpx.Response(200, text=quote_body(closes[symbol], TODAY))

    route = mock.get(url__startswith=QUOTE_URL_AT_IP).mock(side_effect=answer)
    first, second = _household(ledger, factories), _household(ledger, factories)
    a_shared = ledger.run(make_security, first, symbol=shared)
    a_own = ledger.run(make_security, first, symbol=own)
    b_shared = ledger.run(make_security, second, symbol=shared)
    # Quoted in USD by the provider: a EUR security of that symbol is skipped.
    b_euro = ledger.run(make_security, second, symbol=euro, currency="EUR")
    # A manual close of the day is never replaced.
    ledger.run(make_price, a_own, close="11", price_date=TODAY, source="manual")

    stats = refresh_market_data(db, prices=_price_provider(), fx=None, today=TODAY)

    asked = [call.request.url.path.rsplit("/", 1)[-1] for call in route.calls]
    for symbol in (shared, own, euro):
        assert asked.count(symbol) == 1
    assert stats.households >= 2

    first_prices = _prices(db, first)
    assert first_prices[a_shared.id].close == Decimal("250.5")
    assert first_prices[a_shared.id].source == "http_json"
    assert first_prices[a_own.id].close == Decimal("11")
    assert first_prices[a_own.id].source == "manual"
    second_prices = _prices(db, second)
    assert second_prices[b_shared.id].close == Decimal("250.5")
    assert b_euro.id not in second_prices

    # Each household whose prices changed is told so (a count, never a symbol).
    assert _price_events(db, first) == [{"count": 1}]
    assert _price_events(db, second) == [{"count": 1}]

    # A second run replaces the fetched close, still not the manual one.
    closes[shared] = "251"
    closes[own] = "99"
    refresh_market_data(db, prices=_price_provider(), fx=None, today=TODAY)
    assert _prices(db, first)[a_shared.id].close == Decimal("251")
    assert _prices(db, first)[a_own.id].close == Decimal("11")
    assert len(_price_events(db, first)) == 2


def test_ecb_rates_are_crossed_to_each_base_currency(
    db: Session, mock: respx.MockRouter, ledger: LedgerFactories, factories: Any
) -> None:
    mock.get(ECB_AT_IP).mock(
        return_value=httpx.Response(
            200, content=ecb_xml({TODAY: {"USD": "1.10", "GBP": "0.88", "JPY": "165"}})
        )
    )
    hid = _household(ledger, factories)
    ledger.account(hid, name="Euro savings", currency="EUR")
    ledger.account(hid, name="Sterling", currency="GBP")
    ledger.run(make_security, hid, symbol=synthetic_symbol(), currency="JPY")
    # A manual GBP rate of the day (stored the other way round) wins.
    ledger.fx_rate(hid, base="USD", quote="GBP", rate_date=TODAY, rate="0.79")
    # A fetched rate of the day is replaced.
    ledger.fx_rate(hid, base="EUR", quote="USD", rate_date=TODAY, rate="1.05", source="ecb")

    fx = EcbFxProvider(resolver=resolver(), proxy=None)
    stats = refresh_market_data(db, prices=None, fx=fx, today=TODAY)
    assert stats.fx_quotes == 3

    set_context(db, hid)
    rows = {
        (r.base, r.quote): (r.rate, r.source)
        for r in db.execute(
            select(FxRate).where(FxRate.household_id == hid, FxRate.rate_date == TODAY)
        ).scalars()
    }
    set_context(db, None)
    assert rows == {
        ("EUR", "USD"): (Decimal("1.100000000000"), "ecb"),
        ("JPY", "USD"): (Decimal("0.006666666667"), "ecb"),
        ("USD", "GBP"): (Decimal("0.790000000000"), "manual"),
    }


def test_a_failing_fx_feed_does_not_stop_prices(
    db: Session, mock: respx.MockRouter, ledger: LedgerFactories, factories: Any
) -> None:
    symbol = synthetic_symbol()
    mock.get(ECB_AT_IP).mock(return_value=httpx.Response(503))
    mock.get(url__startswith=QUOTE_URL_AT_IP).mock(
        side_effect=lambda request: httpx.Response(
            200 if request.url.path.endswith(symbol) else 404, text=quote_body("5", TODAY)
        )
    )
    hid = _household(ledger, factories)
    security = ledger.run(make_security, hid, symbol=symbol)
    stats = refresh_market_data(
        db,
        prices=_price_provider(),
        fx=EcbFxProvider(resolver=resolver(), proxy=None),
        today=TODAY,
    )
    assert stats.fx_failed
    assert _prices(db, hid)[security.id].close == Decimal("5")


def test_handler_runs_the_configured_feeds(
    db: Session,
    mock: respx.MockRouter,
    ledger: LedgerFactories,
    factories: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbol = synthetic_symbol()
    route = mock.get(url__startswith=QUOTE_URL_AT_IP).mock(
        side_effect=lambda request: httpx.Response(
            200 if request.url.path.endswith(symbol) else 404,
            text=quote_body("3.5", clock.utcnow().date()),
        )
    )
    hid = _household(ledger, factories)
    security = ledger.run(make_security, hid, symbol=symbol)
    configured = price_settings(Settings())
    monkeypatch.setattr(market_data, "get_settings", lambda: configured)
    monkeypatch.setattr(
        market_data,
        "external_price_provider",
        lambda settings: external_price_provider(settings, resolver=resolver(), proxy=None),
    )
    refresh_prices(_ctx(db), RefreshPricesPayload())
    assert route.call_count >= 1
    set_context(db, hid)
    (row,) = db.execute(select(Price).where(Price.security_id == security.id)).scalars()
    set_context(db, None)
    assert (row.close, row.source) == (Decimal("3.5"), "http_json")

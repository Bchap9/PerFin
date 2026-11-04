# SPDX-License-Identifier: AGPL-3.0-or-later
"""Price and FX feed parsing, cross rates and config checks (no DB, no network)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.core.config_check import Finding, check_config
from app.core.settings import Settings, get_settings
from app.integrations.fx.base import FxProviderError, FxQuote
from app.integrations.fx.ecb import parse_daily, parse_rate
from app.integrations.fx.store import cross_rates
from app.integrations.prices.base import PriceQuote
from app.integrations.prices.http_json import (
    HttpJsonConfig,
    HttpJsonPriceProvider,
    parse_close,
    parse_date,
)
from tests.fixtures.market_data import (
    QUOTE_URL_AT_IP,
    ecb_xml,
    price_settings,
    resolver,
)

TODAY = date(2026, 11, 3)
YESTERDAY = date(2026, 11, 2)


# ---------------------------------------------------------------------------
# JSON price answers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(Decimal("101.25"), Decimal("101.25"), id="number"),
        pytest.param("101.25", Decimal("101.25"), id="string"),
        pytest.param(Decimal("0.12345678905"), Decimal("0.1234567890"), id="half-even-down"),
        pytest.param(Decimal("0.12345678915"), Decimal("0.1234567892"), id="half-even-up"),
        pytest.param(Decimal("0"), None, id="zero"),
        pytest.param(Decimal("-1"), None, id="negative"),
        pytest.param(Decimal("1E+18"), None, id="too-large"),
        pytest.param(Decimal("0.00000000001"), None, id="rounds-to-zero"),
        pytest.param("NaN", None, id="nan"),
        pytest.param("Infinity", None, id="infinity"),
        pytest.param("abc", None, id="text"),
        pytest.param(True, None, id="bool"),
        pytest.param(None, None, id="null"),
        pytest.param(101.25, None, id="float-never-accepted"),
    ],
)
def test_parse_close(value: object, expected: Decimal | None) -> None:
    assert parse_close(value) == expected


def test_parse_date() -> None:
    assert parse_date("2026-11-02") == YESTERDAY
    assert parse_date("2026-11-02T21:00:00Z") == YESTERDAY
    assert parse_date("02/11/2026") is None
    assert parse_date(20261102) is None


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


def _fetch(mock: respx.MockRouter, body: str, **update: object) -> list[PriceQuote]:
    mock.get(url__startswith=QUOTE_URL_AT_IP).mock(return_value=httpx.Response(200, text=body))
    config = HttpJsonConfig.from_settings(price_settings(Settings(), **update))
    provider = HttpJsonPriceProvider(config, resolver=resolver(), proxy=None)
    return provider.fetch(["SYNAAA"], today=TODAY)


def test_json_numbers_are_read_as_decimals(mock: respx.MockRouter) -> None:
    # 18 significant digits: a float would come back as 12345678.123456789.
    body = '{"data": {"close": 12345678.1234567891, "date": "2026-11-02", "currency": "usd"}}'
    (quote,) = _fetch(mock, body)
    assert quote.close == Decimal("12345678.1234567891")
    assert quote.currency == "USD"
    assert quote.price_date == YESTERDAY


def test_list_paths_and_defaults(mock: respx.MockRouter) -> None:
    body = '{"results": [{"c": "12.5"}]}'
    (quote,) = _fetch(
        mock,
        body,
        price_provider_price_field="results.0.c",
        price_provider_date_field="",
        price_provider_currency_field="",
        price_provider_currency="EUR",
    )
    assert (quote.close, quote.price_date, quote.currency) == (
        Decimal("12.5"),
        TODAY,
        "EUR",
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            '{"data": {"close": 1, "date": "2026-11-04", "currency": "USD"}}', id="future"
        ),
        pytest.param('{"data": {"close": 1, "date": "2026-11-02", "currency": "XYZ"}}', id="cur"),
        pytest.param('{"data": {"close": 1, "date": "2026-11-02", "currency": 5}}', id="cur-type"),
        pytest.param('{"data": {"date": "2026-11-02", "currency": "USD"}}', id="missing"),
        pytest.param('{"data": [1]}', id="wrong-shape"),
        pytest.param("not json", id="not-json"),
        pytest.param("[" * 100_000, id="deep-nesting"),
    ],
)
def test_unusable_answers_are_skipped(mock: respx.MockRouter, body: str) -> None:
    assert _fetch(mock, body) == []


def test_non_200_is_skipped(mock: respx.MockRouter) -> None:
    mock.get(url__startswith=QUOTE_URL_AT_IP).mock(return_value=httpx.Response(500, text="{}"))
    config = HttpJsonConfig.from_settings(price_settings(Settings()))
    provider = HttpJsonPriceProvider(config, resolver=resolver(), proxy=None)
    assert provider.fetch(["SYNAAA"], today=TODAY) == []
    assert provider.failures == 1


def test_symbols_per_run_are_capped(mock: respx.MockRouter) -> None:
    route = mock.get(url__startswith=QUOTE_URL_AT_IP).mock(return_value=httpx.Response(404))
    config = HttpJsonConfig.from_settings(price_settings(Settings(), price_provider_max_symbols=3))
    provider = HttpJsonPriceProvider(config, resolver=resolver(), proxy=None)
    provider.fetch([f"SYN{i}" for i in range(10)], today=TODAY)
    assert route.call_count == 3


# ---------------------------------------------------------------------------
# ECB reference rates
# ---------------------------------------------------------------------------


def test_ecb_newest_day_only_and_bad_entries_dropped() -> None:
    body = ecb_xml(
        {
            YESTERDAY: {"USD": "1.0", "GBP": "0.8"},
            TODAY: {
                "USD": "1.1234",
                "JPY": "160.5",
                "XYZ": "1.0",
                "GBP": "-1",
                "CHF": "abc",
                "EUR": "1",
            },
        }
    )
    assert parse_daily(body) == [
        FxQuote("EUR", "JPY", TODAY, Decimal("160.5")),
        FxQuote("EUR", "USD", TODAY, Decimal("1.1234")),
    ]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
            b"<r><Cube time='2026-11-03'><Cube currency='USD' rate='&x;'/></Cube></r>",
            id="xxe",
        ),
        pytest.param(
            b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa">'
            b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><r>&b;</r>',
            id="billion-laughs",
        ),
        pytest.param(b"<r><Cube time='2026-11-03'></Cube></r>", id="no-rates"),
        pytest.param(b"<r>", id="truncated"),
        pytest.param(b"", id="empty"),
    ],
)
def test_ecb_refuses_hostile_or_empty_documents(body: bytes) -> None:
    with pytest.raises(FxProviderError):
        parse_daily(body)


def test_parse_rate_bounds() -> None:
    assert parse_rate("1.0812") == Decimal("1.0812")
    assert parse_rate("0.0000000000004") is None  # rounds to zero at 12 places
    assert parse_rate("1000000000000") is None
    assert parse_rate(None) is None


# ---------------------------------------------------------------------------
# Cross rates against the household's base currency
# ---------------------------------------------------------------------------


def test_cross_rates_to_the_base_currency() -> None:
    quotes = [
        FxQuote("EUR", "USD", TODAY, Decimal("1.1")),
        FxQuote("EUR", "GBP", TODAY, Decimal("0.88")),
        FxQuote("EUR", "JPY", TODAY, Decimal("165")),
    ]
    out = cross_rates(quotes, "USD", {"USD", "EUR", "GBP", "CAD"})
    assert out == [
        FxQuote("EUR", "USD", TODAY, Decimal("1.100000000000")),
        # 1 GBP = 1.1 / 0.88 USD = 1.25 exactly.
        FxQuote("GBP", "USD", TODAY, Decimal("1.250000000000")),
    ]
    # Base EUR: the pivot itself, rates are 1 / r.
    (gbp,) = cross_rates(quotes, "EUR", {"GBP"})
    assert gbp.rate == Decimal("1.136363636364")  # half-even at 12 places
    # A base the provider does not publish yields nothing.
    assert cross_rates(quotes, "CAD", {"USD"}) == []


# ---------------------------------------------------------------------------
# config check
# ---------------------------------------------------------------------------

_SECURE_ENV = {
    "PERFIN_PUBLIC_URL": "https://perfin.example.test",
    "PERFIN_MASTER_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
    "PERFIN_SECRET_KEY": "synthetic-" + "Qm9vVnB4c2Y3Ukx1aFdOZ0pEelR5S2hBcFZkUWJ3RXo",
    "PERFIN_DATABASE_URL": "postgresql+psycopg://perfin:synthetic-db-pw-Zq9xW2@db:5432/perfin",
    "PERFIN_TRUSTED_PROXIES": "172.16.0.0/12",
    "PERFIN_LOG_LEVEL": "INFO",
    "PERFIN_MASTER_KEY_BACKUP_ACKNOWLEDGED": "true",
}


@pytest.fixture
def secure(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    for name in list(os.environ):
        if name.startswith("PERFIN_"):
            monkeypatch.delenv(name)
    for name, value in _SECURE_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield Settings()
    get_settings.cache_clear()


def _market(findings: list[Finding]) -> dict[str, str]:
    return {f.code: f.level for f in findings if f.code.startswith(("prices.", "fx."))}


def test_config_check_defaults_have_no_feed_findings(secure: Settings) -> None:
    assert _market(check_config(secure)) == {}


def test_config_check_warns_what_leaves_the_host(secure: Settings) -> None:
    settings = price_settings(secure, fx_provider="ecb")
    findings = check_config(settings)
    assert _market(findings) == {
        "prices.provider_enabled": "warning",
        "fx.provider_enabled": "warning",
    }
    text = " ".join(f.message for f in findings)
    assert "quotes.example.test:443" in text
    assert "www.ecb.europa.eu" in text


def test_config_check_refuses_a_broken_provider(secure: Settings) -> None:
    settings = price_settings(secure, price_provider_url="http://quotes.example.test/{symbol}")
    assert _market(check_config(settings)) == {"prices.provider_invalid": "error"}
    dev = settings.model_copy(update={"dev_mode": True})
    assert _market(check_config(dev)) == {"prices.provider_invalid": "warning"}


def test_config_check_never_prints_the_api_key(secure: Settings) -> None:
    key = "synthetic-key-" + "Zx81"
    settings = price_settings(secure, role="worker", price_provider_api_key=SecretStr(key))
    findings = check_config(settings)
    assert _market(findings) == {"prices.provider_invalid": "error"}  # header missing
    assert all(key not in f.message for f in findings)


def test_only_the_worker_needs_the_credential(secure: Settings) -> None:
    # docker-compose.yml blanks the key outside the worker; the api must still start.
    settings = price_settings(secure, price_provider_auth_header="X-Api-Key")
    assert settings.role == "api"
    assert _market(check_config(settings)) == {"prices.provider_enabled": "warning"}
    worker = settings.model_copy(update={"role": "worker"})
    assert _market(check_config(worker)) == {"prices.provider_invalid": "error"}


def test_config_check_flags_an_unused_url(secure: Settings) -> None:
    settings = secure.model_copy(update={"price_provider_url": "https://q.example.test/{symbol}"})
    assert _market(check_config(settings)) == {"prices.url_unused": "warning"}

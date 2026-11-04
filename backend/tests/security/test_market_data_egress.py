# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the price and FX feeds send, and where they may connect (docs/security/integrations.md).

No network: ``respx`` answers :data:`PUBLIC_IP`; any request without a route
raises, so a request that should never be made fails the test. The resolver
is injected, so SSRF cases are real resolutions to forbidden addresses.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.core.settings import Settings
from app.integrations.fx.ecb import ECB_DAILY_URL, EcbFxProvider
from app.integrations.fx.registry import external_fx_provider, fx_providers
from app.integrations.prices.base import PriceProviderConfigError
from app.integrations.prices.http_json import HttpJsonConfig, HttpJsonPriceProvider
from app.integrations.prices.registry import external_price_provider, price_providers
from tests.fixtures.market_data import (
    ECB_HOST,
    PUBLIC_IP,
    QUOTE_HOST,
    QUOTE_URL_AT_IP,
    ecb_xml,
    price_settings,
    quote_body,
    resolver,
)

pytestmark = pytest.mark.security

TODAY = date(2026, 11, 3)
SECRET_KEY = "synthetic-price-api-key-" + "R4nd0m"


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


def _provider(*addresses: str, **update: Any) -> HttpJsonPriceProvider:
    settings = price_settings(Settings(), **update)
    return HttpJsonPriceProvider(
        HttpJsonConfig.from_settings(settings), resolver=resolver(*addresses), proxy=None
    )


def _answer(request: httpx.Request) -> httpx.Response:
    symbol = request.url.path.rsplit("/", 1)[-1]
    close = {"SYNAAA": "101.25", "SYN.B": "7.5"}.get(symbol)
    if close is None:
        return httpx.Response(404)
    return httpx.Response(200, text=quote_body(close, TODAY))


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


def test_feeds_are_off_by_default(mock: respx.MockRouter) -> None:
    settings = Settings()
    assert settings.price_provider == "none"
    assert settings.fx_provider == "none"
    assert external_price_provider(settings) is None
    assert external_fx_provider(settings) is None
    assert [p.name for p in price_providers(settings)] == ["manual"]
    assert [p.name for p in fx_providers(settings)] == ["manual"]
    assert all(not p.external for p in price_providers(settings))
    assert price_providers(settings)[0].fetch(["SYNAAA"], today=TODAY) == []
    assert fx_providers(settings)[0].fetch() == []
    assert mock.calls.call_count == 0


# ---------------------------------------------------------------------------
# Only symbols leave the host
# ---------------------------------------------------------------------------


def test_price_request_carries_only_the_symbol(mock: respx.MockRouter) -> None:
    route = mock.get(url__startswith=QUOTE_URL_AT_IP).mock(side_effect=_answer)
    quotes = _provider().fetch(["SYNAAA", "SYN.B", "SYNAAA"], today=TODAY)

    assert {q.symbol: q.close for q in quotes} == {
        "SYNAAA": Decimal("101.25"),
        "SYN.B": Decimal("7.5"),
    }
    # One request per distinct symbol, in sorted order (no household ordering).
    assert route.call_count == 2
    sent = [call.request for call in route.calls]
    assert [r.url.path for r in sent] == ["/v1/last/SYN.B", "/v1/last/SYNAAA"]
    for request in sent:
        assert request.method == "GET"
        assert request.content == b""
        assert str(request.url.query, "ascii") == "fields=close"  # the template's own query
        assert request.headers["host"] == QUOTE_HOST
        assert set(request.headers) == {
            "host",
            "user-agent",
            "accept",
            "accept-encoding",
            "connection",
        }
        assert request.headers["user-agent"] == "PerFin"
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers


def test_symbols_are_percent_encoded_and_invalid_ones_never_sent(
    mock: respx.MockRouter,
) -> None:
    route = mock.get(url__startswith=QUOTE_URL_AT_IP).mock(side_effect=_answer)
    provider = _provider()
    provider.fetch(
        ["SYN/X", "../../admin", "..", "...", "A B", "x" * 33, "SYN?q=1", "SYN%2F"], today=TODAY
    )
    paths = [call.request.url.raw_path for call in route.calls]
    # Each symbol is one percent-encoded path segment; dot-only symbols,
    # spaces, query characters and percent signs are never sent.
    assert paths == [b"/v1/last/..%2F..%2Fadmin?fields=close", b"/v1/last/SYN%2FX?fields=close"]


def test_api_key_goes_in_its_header_only(mock: respx.MockRouter) -> None:
    route = mock.get(url__startswith=QUOTE_URL_AT_IP).mock(side_effect=_answer)
    provider = _provider(
        price_provider_auth_header="X-Api-Key",
        price_provider_api_key=SecretStr(SECRET_KEY),
    )
    provider.fetch(["SYNAAA"], today=TODAY)
    (call,) = route.calls
    assert call.request.headers["x-api-key"] == SECRET_KEY
    assert SECRET_KEY not in str(call.request.url)
    assert SECRET_KEY not in repr(provider.config)


def test_ecb_request_is_the_fixed_url_and_nothing_else(mock: respx.MockRouter) -> None:
    body = ecb_xml({TODAY: {"USD": "1.1000", "GBP": "0.8500"}})
    route = mock.get(f"https://{PUBLIC_IP}/stats/eurofxref/eurofxref-daily.xml").mock(
        return_value=httpx.Response(200, content=body)
    )
    quotes = EcbFxProvider(resolver=resolver(), proxy=None).fetch()
    assert {(q.base, q.quote, q.rate) for q in quotes} == {
        ("EUR", "USD", Decimal("1.1")),
        ("EUR", "GBP", Decimal("0.85")),
    }
    (call,) = route.calls
    assert call.request.method == "GET"
    assert call.request.content == b""
    assert call.request.url.query == b""
    assert call.request.headers["host"] == ECB_HOST
    assert set(call.request.headers) == {
        "host",
        "user-agent",
        "accept",
        "accept-encoding",
        "connection",
    }
    assert ECB_DAILY_URL.startswith(f"https://{ECB_HOST}/")


# ---------------------------------------------------------------------------
# SSRF: forbidden addresses, other hosts, redirects
# ---------------------------------------------------------------------------

FORBIDDEN = [
    pytest.param("169.254.169.254", id="metadata"),
    pytest.param("127.0.0.1", id="loopback"),
    pytest.param("10.0.0.5", id="rfc1918"),
    pytest.param("::ffff:169.254.169.254", id="mapped-metadata"),
    pytest.param("fc00::1", id="ula"),
]


@pytest.mark.parametrize("address", FORBIDDEN)
def test_price_host_resolving_to_internal_addresses_is_never_contacted(
    mock: respx.MockRouter, address: str
) -> None:
    provider = _provider(address)
    assert provider.fetch(["SYNAAA"], today=TODAY) == []
    assert provider.failures == 1
    assert mock.calls.call_count == 0


@pytest.mark.parametrize("address", FORBIDDEN)
def test_ecb_host_resolving_to_internal_addresses_is_never_contacted(
    mock: respx.MockRouter, address: str
) -> None:
    from app.integrations.fx.base import FxProviderError

    with pytest.raises(FxProviderError, match="forbidden address"):
        EcbFxProvider(resolver=resolver(address), proxy=None).fetch()
    assert mock.calls.call_count == 0


def test_mixed_answer_with_one_private_address_is_refused(mock: respx.MockRouter) -> None:
    provider = _provider(PUBLIC_IP, "192.168.1.10")
    assert provider.fetch(["SYNAAA"], today=TODAY) == []
    assert mock.calls.call_count == 0


def test_redirect_to_another_host_is_refused(mock: respx.MockRouter) -> None:
    route = mock.get(url__startswith=QUOTE_URL_AT_IP).mock(
        return_value=httpx.Response(302, headers={"location": "https://169.254.169.254/latest"})
    )
    provider = _provider()
    assert provider.fetch(["SYNAAA"], today=TODAY) == []
    assert route.call_count == 1  # the redirect was not followed


def test_allowlist_is_the_template_host_only() -> None:
    config = HttpJsonConfig.from_settings(price_settings(Settings()))
    assert config.policy().allowed_hosts == frozenset({f"{QUOTE_HOST}:443"})
    assert not config.policy().allow_http
    assert not config.policy().allow_loopback
    assert config.policy().private_hosts == frozenset()
    assert EcbFxProvider.policy().allowed_hosts == frozenset({f"{ECB_HOST}:443"})


BAD_TEMPLATES = [
    pytest.param("", id="empty"),
    pytest.param("https://quotes.example.test/v1/last", id="no-placeholder"),
    pytest.param("https://quotes.example.test/{symbol}/{symbol}", id="twice"),
    pytest.param("http://quotes.example.test/{symbol}", id="plain-http"),
    pytest.param("ftp://quotes.example.test/{symbol}", id="ftp"),
    pytest.param("https://{symbol}.example.test/q", id="symbol-in-host"),
    pytest.param("https://{symbol}/q", id="symbol-is-host"),
    pytest.param("https://user:pw@quotes.example.test/{symbol}", id="userinfo"),
    pytest.param("https://quotes.example.test/{symbol}?k={key}", id="other-placeholder"),
    pytest.param("https://quotes.example.test/{symbol}#frag", id="fragment"),
    pytest.param("https:///{symbol}", id="no-host"),
]


@pytest.mark.parametrize("template", BAD_TEMPLATES)
def test_bad_url_templates_are_refused(template: str) -> None:
    with pytest.raises(PriceProviderConfigError):
        HttpJsonConfig.from_settings(price_settings(Settings(), price_provider_url=template))


@pytest.mark.parametrize(
    "update",
    [
        pytest.param({"price_provider_auth_header": "X-Api-Key"}, id="header-without-key"),
        pytest.param({"price_provider_api_key": SecretStr("k")}, id="key-without-header"),
        pytest.param(
            {"price_provider_auth_header": "Host", "price_provider_api_key": SecretStr("k")},
            id="reserved-header",
        ),
        pytest.param(
            {"price_provider_auth_header": "X: y", "price_provider_api_key": SecretStr("k")},
            id="header-injection",
        ),
        pytest.param(
            {"price_provider_auth_header": "X-Key", "price_provider_api_key": SecretStr("a\r\nb")},
            id="key-line-break",
        ),
        pytest.param({"price_provider_currency": "XYZ"}, id="currency"),
        pytest.param({"price_provider_price_field": ""}, id="no-price-field"),
        pytest.param({"price_provider_price_field": "a..b"}, id="bad-path"),
    ],
)
def test_bad_provider_settings_are_refused(update: dict[str, Any]) -> None:
    with pytest.raises(PriceProviderConfigError):
        HttpJsonConfig.from_settings(price_settings(Settings(), **update))

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generic JSON-over-HTTPS price provider (``PERFIN_PRICE_PROVIDER=http_json``).

No vendor is baked in: the operator names an endpoint with a URL template
and says where the close (and optionally its date and currency) sits in the
JSON answer::

    PERFIN_PRICE_PROVIDER_URL=https://quotes.example.com/v1/last/{symbol}
    PERFIN_PRICE_PROVIDER_PRICE_FIELD=data.close
    PERFIN_PRICE_PROVIDER_DATE_FIELD=data.date          # optional (default: today)
    PERFIN_PRICE_PROVIDER_CURRENCY_FIELD=data.currency  # optional (default below)
    PERFIN_PRICE_PROVIDER_CURRENCY=USD

What leaves the host (docs/security/integrations.md):

* one ``GET`` per distinct symbol; the only variable part of the request is
  the symbol, percent-encoded into the template's single ``{symbol}``
  placeholder. Symbols must match :data:`~app.integrations.prices.base.SYMBOL_RE`;
  anything else is never sent.
* headers: ``User-Agent: PerFin``, ``Accept: application/json``,
  ``Accept-Encoding: identity`` and, only when configured, one credential
  header (``PERFIN_PRICE_PROVIDER_AUTH_HEADER`` /
  ``PERFIN_PRICE_PROVIDER_API_KEY``, an instance credential: never in the
  URL, never logged). No cookies, no household, user, account, quantity or
  holding data.

Transport: :class:`app.core.http.SafeHttpClient` with an allowlist of the
template's ``host:port`` only, https only, every resolved address vetted
(no private, loopback, link-local/metadata addresses), same-origin redirects
only, a 256 KiB body cap and a 10 s timeout; with ``HTTPS_PROXY`` the request
is tunnelled through the egress allowlist proxy (SECURITY.md B5, H2).

Parsing: JSON numbers are read as ``Decimal`` (never ``float``); the close
must be a positive decimal that fits ``NUMERIC(28, 10)`` (rounded half-even
to 10 places); the date an ISO date (a datetime's first 10 characters) not
after today; the currency an ISO 4217 code. A failed or unusable answer
skips that symbol (counted, logged without the symbol).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Final
from urllib.parse import quote, urlsplit

from pydantic import SecretStr

from app.core.http import (
    EgressError,
    EgressPolicy,
    ProxySetting,
    Resolver,
    SafeHttpClient,
    system_resolver,
    url_host_key,
)
from app.core.logging import get_logger
from app.core.money import is_currency
from app.core.settings import Settings
from app.integrations.prices.base import SYMBOL_RE, PriceProviderConfigError, PriceQuote

log = get_logger(__name__)

NAME: Final = "http_json"
PLACEHOLDER: Final = "{symbol}"
MAX_RESPONSE_BYTES: Final = 256 * 1024
TIMEOUT_SECONDS: Final = 10.0
#: ``NUMERIC(28, 10)``: 18 integer digits, 10 decimals.
_PLACES: Final = Decimal(1).scaleb(-10)
_MAX_CLOSE: Final = Decimal(10) ** 18
_FIELD_RE: Final = re.compile(r"^[A-Za-z0-9_$-]{1,64}(?:\.[A-Za-z0-9_$-]{1,64}){0,7}$")
_HEADER_RE: Final = re.compile(r"^[A-Za-z0-9-]{1,64}$")
#: Headers a credential may never be put in (they would change the request's meaning).
_RESERVED_HEADERS: Final = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "cookie", "accept-encoding"}
)


def check_template(template: str) -> str:
    """Validate a URL template; return its ``host:port`` key for the allowlist."""
    value = template.strip()
    if value.count(PLACEHOLDER) != 1:
        raise PriceProviderConfigError(
            "PERFIN_PRICE_PROVIDER_URL must contain {symbol} exactly once"
        )
    if "{" in value.replace(PLACEHOLDER, "") or "}" in value.replace(PLACEHOLDER, ""):
        raise PriceProviderConfigError("PERFIN_PRICE_PROVIDER_URL has an unknown placeholder")
    if any(ch.isspace() for ch in value):
        raise PriceProviderConfigError("PERFIN_PRICE_PROVIDER_URL contains whitespace")
    # The symbol must land in the path or query, never in the host.
    hosts = set()
    for probe in ("AAA", "ZZZ"):
        try:
            parts = urlsplit(value.replace(PLACEHOLDER, probe))
            port = parts.port
        except ValueError as exc:
            raise PriceProviderConfigError("PERFIN_PRICE_PROVIDER_URL is not a URL") from exc
        if parts.scheme != "https":
            raise PriceProviderConfigError("PERFIN_PRICE_PROVIDER_URL must be an https:// URL")
        if parts.username is not None or parts.password is not None:
            raise PriceProviderConfigError(
                "PERFIN_PRICE_PROVIDER_URL must not carry credentials (use the auth header)"
            )
        if not parts.hostname:
            raise PriceProviderConfigError("PERFIN_PRICE_PROVIDER_URL has no host")
        if parts.fragment:
            raise PriceProviderConfigError("PERFIN_PRICE_PROVIDER_URL must not have a fragment")
        hosts.add((parts.hostname.lower(), port))
    if len(hosts) != 1:
        raise PriceProviderConfigError(
            "the {symbol} placeholder must be in the path or query of PERFIN_PRICE_PROVIDER_URL"
        )
    return url_host_key(value.replace(PLACEHOLDER, "AAA"))


def check_field(name: str, value: str, *, required: bool) -> tuple[str, ...]:
    """A dotted JSON path (``data.0.close``) as its parts; empty when optional and unset."""
    text = value.strip()
    if not text:
        if required:
            raise PriceProviderConfigError(f"{name} must name a JSON field")
        return ()
    if not _FIELD_RE.fullmatch(text):
        raise PriceProviderConfigError(f"{name} must be a dotted JSON path such as data.close")
    return tuple(text.split("."))


@dataclass(frozen=True)
class HttpJsonConfig:
    url_template: str
    host_key: str
    price_field: tuple[str, ...]
    date_field: tuple[str, ...] = ()
    currency_field: tuple[str, ...] = ()
    default_currency: str = "USD"
    auth_header: str | None = None
    api_key: SecretStr | None = field(default=None, repr=False)
    max_symbols: int = 500

    @classmethod
    def from_settings(cls, settings: Settings) -> HttpJsonConfig:
        """The provider configuration; :class:`PriceProviderConfigError` when unusable."""
        template = settings.price_provider_url.strip()
        if not template:
            raise PriceProviderConfigError(
                "PERFIN_PRICE_PROVIDER=http_json needs PERFIN_PRICE_PROVIDER_URL"
            )
        host = check_template(template)
        currency = settings.price_provider_currency.strip().upper()
        if not is_currency(currency):
            raise PriceProviderConfigError(
                "PERFIN_PRICE_PROVIDER_CURRENCY must be an ISO 4217 code"
            )
        header = settings.price_provider_auth_header.strip()
        key = settings.price_provider_api_key
        has_key = key is not None and bool(key.get_secret_value())
        if bool(header) != has_key:
            raise PriceProviderConfigError(
                "set both PERFIN_PRICE_PROVIDER_AUTH_HEADER and PERFIN_PRICE_PROVIDER_API_KEY, "
                "or neither"
            )
        if header and (not _HEADER_RE.fullmatch(header) or header.lower() in _RESERVED_HEADERS):
            raise PriceProviderConfigError(
                "PERFIN_PRICE_PROVIDER_AUTH_HEADER must be a plain header name"
            )
        if has_key and key is not None and any(ch in key.get_secret_value() for ch in "\r\n\0"):
            raise PriceProviderConfigError("PERFIN_PRICE_PROVIDER_API_KEY contains a line break")
        return cls(
            url_template=template,
            host_key=host,
            price_field=check_field(
                "PERFIN_PRICE_PROVIDER_PRICE_FIELD",
                settings.price_provider_price_field,
                required=True,
            ),
            date_field=check_field(
                "PERFIN_PRICE_PROVIDER_DATE_FIELD",
                settings.price_provider_date_field,
                required=False,
            ),
            currency_field=check_field(
                "PERFIN_PRICE_PROVIDER_CURRENCY_FIELD",
                settings.price_provider_currency_field,
                required=False,
            ),
            default_currency=currency,
            auth_header=header or None,
            api_key=key if has_key else None,
            max_symbols=settings.price_provider_max_symbols,
        )

    def url_for(self, symbol: str) -> str:
        """The request URL for ``symbol`` (percent-encoded, nothing else varies)."""
        if not SYMBOL_RE.fullmatch(symbol):
            raise ValueError("not a ticker symbol")
        return self.url_template.replace(PLACEHOLDER, quote(symbol, safe=""))

    def policy(self) -> EgressPolicy:
        return EgressPolicy(
            allowed_hosts=frozenset({self.host_key}),
            max_response_bytes=MAX_RESPONSE_BYTES,
            timeout_seconds=TIMEOUT_SECONDS,
        )


class _MissingError(Exception):
    pass


def _dig(document: object, path: tuple[str, ...]) -> object:
    node = document
    for part in path:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise _MissingError(part)
    return node


def parse_close(value: object) -> Decimal | None:
    """A positive close that fits ``NUMERIC(28, 10)``, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return None
    else:
        return None
    if not number.is_finite() or number <= 0 or number >= _MAX_CLOSE:
        return None
    rounded = number.quantize(_PLACES, rounding=ROUND_HALF_EVEN)
    return rounded if rounded > 0 else None


def parse_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _loads(content: bytes) -> object:
    # Decimal for every number: a close never passes through a float.
    return json.loads(content, parse_float=Decimal, parse_int=Decimal)


class HttpJsonPriceProvider:
    """The generic JSON adapter (module doc)."""

    name: str = NAME
    external: bool = True

    def __init__(
        self,
        config: HttpJsonConfig,
        *,
        resolver: Resolver = system_resolver,
        proxy: ProxySetting = "env",
    ) -> None:
        self.config = config
        self._resolver = resolver
        self._proxy = proxy
        self.failures = 0

    def _client(self) -> SafeHttpClient:
        return SafeHttpClient(
            self.config.policy(),
            resolver=self._resolver,
            proxy=self._proxy,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.auth_header and self.config.api_key is not None:
            headers[self.config.auth_header] = self.config.api_key.get_secret_value()
        return headers

    def _quote(self, symbol: str, document: object, today: date) -> PriceQuote | None:
        try:
            close = parse_close(_dig(document, self.config.price_field))
            on = (
                parse_date(_dig(document, self.config.date_field))
                if self.config.date_field
                else today
            )
            currency: object = (
                _dig(document, self.config.currency_field)
                if self.config.currency_field
                else self.config.default_currency
            )
        except _MissingError:
            return None
        if not isinstance(currency, str):
            return None
        currency = currency.strip().upper()
        if close is None or on is None or on > today or not is_currency(currency):
            return None
        return PriceQuote(symbol=symbol, price_date=on, close=close, currency=currency)

    def fetch(self, symbols: Sequence[str], *, today: date) -> list[PriceQuote]:
        """One ``GET`` per distinct valid symbol, at most ``max_symbols`` (module doc)."""
        wanted = sorted({s for s in symbols if SYMBOL_RE.fullmatch(s)})[: self.config.max_symbols]
        quotes: list[PriceQuote] = []
        self.failures = 0
        headers = self._headers()
        with self._client() as client:
            for symbol in wanted:
                try:
                    result = client.get(self.config.url_for(symbol), headers=headers)
                    if result.status_code != 200:
                        raise EgressError(f"status {result.status_code}")
                    document = _loads(result.content)
                except (EgressError, ValueError, RecursionError) as exc:
                    self.failures += 1
                    log.warning(
                        "price lookup failed",
                        extra={"provider": NAME, "error": type(exc).__name__},
                    )
                    continue
                found = self._quote(symbol, document, today)
                if found is None:
                    self.failures += 1
                    log.warning(
                        "price lookup returned an unusable answer", extra={"provider": NAME}
                    )
                    continue
                quotes.append(found)
        return quotes

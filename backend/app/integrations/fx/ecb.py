# SPDX-License-Identifier: AGPL-3.0-or-later
"""ECB daily euro reference rates (``PERFIN_FX_PROVIDER=ecb``).

What leaves the host (docs/security/integrations.md): one ``GET`` of the
fixed URL :data:`ECB_DAILY_URL` per refresh run, with ``User-Agent: PerFin``,
``Accept: application/xml`` and ``Accept-Encoding: identity``. No query, no
currency list, no household, user or amount: every installation asks the
same question. The URL is a constant, never taken from settings or input.

Transport: :class:`app.core.http.SafeHttpClient` with an allowlist of
``www.ecb.europa.eu:443`` only (https, vetted addresses, same-origin
redirects, 64 KiB cap, 10 s timeout; tunnelled through the egress proxy with
``HTTPS_PROXY``).

Parsing: ``defusedxml`` (DTDs, entities and external references refused,
SECURITY.md C6). Every ``<Cube time="YYYY-MM-DD">`` holds ``<Cube
currency="USD" rate="1.0812"/>`` children: ``1 EUR = rate currency``. Rates
must be positive decimals that fit ``NUMERIC(24, 12)`` and currencies ISO
4217 codes; anything else is dropped. Only the newest day is returned.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Final

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

from app.core.http import (
    EgressError,
    EgressPolicy,
    ProxySetting,
    Resolver,
    SafeHttpClient,
    system_resolver,
    url_host_key,
)
from app.core.money import is_currency
from app.integrations.fx.base import FxProviderError, FxQuote

NAME: Final = "ecb"
ECB_DAILY_URL: Final = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ECB_BASE: Final = "EUR"
MAX_RESPONSE_BYTES: Final = 64 * 1024
TIMEOUT_SECONDS: Final = 10.0
#: ``NUMERIC(24, 12)``: 12 integer digits, 12 decimals.
RATE_PLACES: Final = Decimal(1).scaleb(-12)
MAX_RATE: Final = Decimal(10) ** 12


def parse_rate(text: str | None) -> Decimal | None:
    """A positive rate that fits ``NUMERIC(24, 12)`` (half-even), or ``None``."""
    if text is None:
        return None
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0 or value >= MAX_RATE:
        return None
    rounded = value.quantize(RATE_PLACES, rounding=ROUND_HALF_EVEN)
    return rounded if rounded > 0 else None


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_daily(content: bytes) -> list[FxQuote]:
    """``EUR -> currency`` quotes of the newest day in an ECB reference-rate file."""
    try:
        root = fromstring(content, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise FxProviderError(f"ECB answer is not acceptable XML ({type(exc).__name__})") from exc
    days: dict[date, dict[str, Decimal]] = {}
    for cube in root.iter():
        if _local(cube.tag) != "Cube" or "time" not in cube.attrib:
            continue
        try:
            on = date.fromisoformat(cube.attrib["time"].strip())
        except ValueError:
            continue
        rates = days.setdefault(on, {})
        for child in cube:
            if _local(child.tag) != "Cube":
                continue
            currency = (child.attrib.get("currency") or "").strip().upper()
            rate = parse_rate(child.attrib.get("rate"))
            if currency != ECB_BASE and is_currency(currency) and rate is not None:
                rates[currency] = rate
    days = {d: r for d, r in days.items() if r}
    if not days:
        raise FxProviderError("ECB answer holds no rates")
    newest = max(days)
    return [
        FxQuote(base=ECB_BASE, quote=currency, rate_date=newest, rate=rate)
        for currency, rate in sorted(days[newest].items())
    ]


class EcbFxProvider:
    """Fetches and parses :data:`ECB_DAILY_URL` (module doc)."""

    name: str = NAME
    external: bool = True

    def __init__(
        self, *, resolver: Resolver = system_resolver, proxy: ProxySetting = "env"
    ) -> None:
        self._resolver = resolver
        self._proxy = proxy

    @staticmethod
    def policy() -> EgressPolicy:
        return EgressPolicy(
            allowed_hosts=frozenset({url_host_key(ECB_DAILY_URL)}),
            max_response_bytes=MAX_RESPONSE_BYTES,
            timeout_seconds=TIMEOUT_SECONDS,
        )

    def fetch(self) -> list[FxQuote]:
        with SafeHttpClient(self.policy(), resolver=self._resolver, proxy=self._proxy) as client:
            try:
                result = client.get(ECB_DAILY_URL, headers={"Accept": "application/xml"})
            except EgressError as exc:
                raise FxProviderError(f"ECB request failed ({exc})") from exc
        if result.status_code != 200:
            raise FxProviderError(f"ECB answered {result.status_code}")
        return parse_daily(result.content)

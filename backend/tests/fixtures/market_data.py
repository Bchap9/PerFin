# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic market-data feeds for tests (no network, invented values).

* :func:`ecb_xml` builds an ECB-shaped daily reference-rate document from
  invented rates (the element layout of the public file, nothing copied).
* :data:`QUOTE_HOST` / :data:`ECB_HOST` resolve, through :func:`resolver`,
  to :data:`PUBLIC_IP`, a global unicast address that ``respx`` answers:
  requests arrive addressed to the IP with the original ``Host`` header, so
  nothing ever leaves the test process.
* :func:`price_settings` turns the conftest settings into an
  ``http_json`` price-provider configuration pointed at :data:`QUOTE_HOST`.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from app.core.http import IPAddress
from app.core.settings import Settings

QUOTE_HOST = "quotes.example.test"
QUOTE_TEMPLATE = f"https://{QUOTE_HOST}/v1/last/{{symbol}}?fields=close"
ECB_HOST = "www.ecb.europa.eu"
#: A global unicast address; respx answers it.
PUBLIC_IP = "151.101.10.10"
QUOTE_URL_AT_IP = f"https://{PUBLIC_IP}/v1/last/"

_ECB_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" '
    'xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">\n'
    "<gesmes:subject>Reference rates</gesmes:subject>\n"
    "<gesmes:Sender><gesmes:name>Synthetic Central Bank</gesmes:name></gesmes:Sender>\n"
)


def ecb_xml(days: Mapping[date, Mapping[str, str]]) -> bytes:
    """An ECB-shaped document: one ``<Cube time=...>`` per day, rates as given."""
    cubes = []
    for day, rates in days.items():
        inner = "".join(f"<Cube currency='{c}' rate='{r}'/>" for c, r in rates.items())
        cubes.append(f"<Cube time='{day.isoformat()}'>{inner}</Cube>")
    return (_ECB_HEAD + "<Cube>" + "".join(cubes) + "</Cube>\n</gesmes:Envelope>\n").encode()


def resolver(*addresses: str) -> Callable[[str, int], list[IPAddress]]:
    """A resolver answering every name with ``addresses`` (default :data:`PUBLIC_IP`)."""
    answer = [ipaddress.ip_address(a) for a in (addresses or (PUBLIC_IP,))]

    def resolve(_host: str, _port: int) -> list[IPAddress]:
        return list(answer)

    return resolve


def price_settings(base: Settings, **update: Any) -> Settings:
    """``base`` with the generic JSON price provider pointed at :data:`QUOTE_HOST`."""
    values: dict[str, Any] = {
        "price_provider": "http_json",
        "price_provider_url": QUOTE_TEMPLATE,
        "price_provider_price_field": "data.close",
        "price_provider_date_field": "data.date",
        "price_provider_currency_field": "data.currency",
        **update,
    }
    return base.model_copy(update=values)


def quote_body(close: str, day: date, currency: str = "USD") -> str:
    """A provider answer with the close as a JSON **number** (read as Decimal)."""
    return (
        f'{{"data": {{"close": {close}, "date": "{day.isoformat()}", "currency": "{currency}"}}}}'
    )

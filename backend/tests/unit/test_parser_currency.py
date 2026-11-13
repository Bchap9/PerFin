# SPDX-License-Identifier: AGPL-3.0-or-later
"""A document's currency is an ISO code PerFin knows (``app.ingestion.parsers.currency``).

The word after "currency" is a code only when it is printed as three capital
letters and is an active ISO 4217 code; a heading such as "Currency Forward
Contracts" or a sentence ("currency for the period") names none.
"""

from __future__ import annotations

import pytest

from app.ingestion.parsers.currency import known_currency, stated_currency


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Currency: USD", "USD"),
        ("Currency USD", "USD"),
        ("CURRENCY : EUR", "EUR"),
        ("Account currency: CAD Page 1 of 3", "CAD"),
        ("Base currency:GBP", "GBP"),
        # A heading and a real statement of the currency on one line.
        ("Currency Forward Contracts. Currency: CHF", "CHF"),
        # Words, not codes.
        ("Currency Forward Contracts", None),
        ("currency for the period", None),
        ("Currency FORWARD contracts", None),
        ("CURRENCY FOR THE PERIOD", None),
        # Three capital letters that are no currency PerFin knows.
        ("Currency: XQZ", None),
        ("Currency: XAU", None),
        # Not printed as a code.
        ("Currency: usd", None),
        ("Currency: Usd", None),
        ("Currency: USDX", None),
        ("Currency: US", None),
        # The word must be "currency" itself.
        ("Cryptocurrency USD", None),
        ("USD", None),
        ("", None),
    ],
)
def test_stated_currency(text: str, code: str | None) -> None:
    assert stated_currency(text) == code


@pytest.mark.parametrize(
    ("cell", "code"),
    [
        ("eur", "EUR"),
        ("USD", "USD"),
        (" cad ", "CAD"),
        ("ABC", None),
        ("US$", None),
        ("USDX", None),
        ("XXX", None),
        ("", None),
    ],
)
def test_known_currency(cell: str, code: str | None) -> None:
    assert known_currency(cell) == code

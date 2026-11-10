# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recognising a payee inside a bank's description (``app.domain.imports.payees``)."""

from __future__ import annotations

import uuid

import pytest

from app.domain.imports.payees import PayeeMatcher
from app.domain.transactions.import_hash import normalize_payee

GROCER, DELI, RENT, COFFEE, FUEL, SHORT, DIGITS = (uuid.uuid4() for _ in range(7))
LOOKUP = {
    normalize_payee(name): payee
    for name, payee in (
        ("Corner Grocer", GROCER),
        ("Corner Grocer Deli", DELI),
        ("Oakfield Rentals", RENT),
        ("Quiet Bean Coffee", COFFEE),
        ("QB COFFEE", COFFEE),  # an alias of the same payee
        ("Lantern Fuel", FUEL),
        ("BP", SHORT),
        ("7731", DIGITS),
    )
}


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Corner Grocer", GROCER),  # the bare name, as before
        ("Card Purchase 01/14 Corner Grocer #204 Springfield IL Card 7731", GROCER),
        ("CHECKCARD 0304 CORNER GROCER DELI SPRINGFIELD", DELI),  # the longer name wins
        ("OAKFIELD RENTALS DES:RENT ID:88123 INDN:JORDAN SAMPLE", RENT),
        ("Oakfield-Rentals, Web Pmts", RENT),  # punctuation and case do not matter
        ("POS QB COFFEE 0042", COFFEE),  # by an alias
        ("Quiet Bean Coffee and QB Coffee", COFFEE),  # two names of one payee
        ("BP", SHORT),  # a short name matches exactly only
        ("BP FUEL 5521", None),
        ("ATM Withdrawal Card 7731", None),  # digits are never searched for
        ("Corner Grocery", None),  # whole words only
        ("Oakfield", None),
        ("Lantern Fuel at Oakfield Rentals", None),  # two payees side by side
        ("", None),
        (None, None),
    ],
)
def test_match(description: str | None, expected: uuid.UUID | None) -> None:
    assert PayeeMatcher(LOOKUP).match(description) == expected


def test_no_payees_no_match() -> None:
    assert PayeeMatcher({}).match("Corner Grocer") is None

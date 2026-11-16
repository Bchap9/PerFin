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


# ---------------------------------------------------------------------------
# The core: what clean-up left of the description (the merchant)
# ---------------------------------------------------------------------------

PAYPAL, STREAMBOX = uuid.uuid4(), uuid.uuid4()
BEHIND = {**LOOKUP, normalize_payee("PayPal"): PAYPAL, normalize_payee("Streambox"): STREAMBOX}


MARTA, VENMO, APPLE = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
PEOPLE = {
    **BEHIND,
    normalize_payee("Marta Lindqvist"): MARTA,
    normalize_payee("Venmo"): VENMO,
    normalize_payee("Apple"): APPLE,
}


def test_a_payee_is_found_behind_a_processor() -> None:
    matcher = PayeeMatcher(BEHIND)
    # The whole text names two payees, the processor and the merchant: no match.
    assert matcher.match("PAYPAL *STREAMBOX") is None
    assert matcher.match("PAYPAL *STREAMBOX", core="streambox", via="paypal") == STREAMBOX
    # A core that names nobody leaves it there.
    assert matcher.match("PAYPAL *STREAMBOX", core="", via="paypal") is None
    assert matcher.match("PAYPAL *STREAMBOX", core=None, via="paypal") is None
    # Through real clean-up, with a store's tail behind the merchant.
    assert matcher.match_descriptor("PAYPAL *STREAMBOX") == STREAMBOX
    assert matcher.match_descriptor("PAYPAL *STREAMBOX 4029357733 CA") == STREAMBOX
    assert matcher.match_descriptor("SQ *QUIET BEAN COFFEE") == COFFEE


def test_a_match_on_the_whole_text_is_not_second_guessed_by_the_core() -> None:
    matcher = PayeeMatcher(BEHIND)
    assert matcher.match("POS QB COFFEE 0042", core="streambox", via="paypal") == COFFEE


def test_a_short_name_matches_a_core_it_equals() -> None:
    matcher = PayeeMatcher(LOOKUP)
    assert matcher.match("BP#5521044 SPRINGFIELD IL", core="bp") == SHORT
    assert matcher.match_descriptor("BP#5521044 SPRINGFIELD IL") == SHORT


def test_a_short_name_inside_a_longer_core_is_still_no_match() -> None:
    matcher = PayeeMatcher(LOOKUP)
    assert matcher.match("BP FUEL 5521", core="bp fuel") is None
    assert matcher.match("SQ *BP FUEL 5521", core="bp fuel", via="square") is None
    assert matcher.match_descriptor("SQ *BP FUEL 5521") is None


def test_two_payees_in_the_core_are_still_no_match() -> None:
    core = "lantern fuel at oakfield rentals"
    matcher = PayeeMatcher(LOOKUP)
    assert matcher.match("Lantern Fuel at Oakfield Rentals", core=core) is None
    assert matcher.match("SQ *LANTERN FUEL AT OAKFIELD RENTALS", core=core, via="square") is None
    assert matcher.match_descriptor("Lantern Fuel at Oakfield Rentals") is None


def test_no_processor_was_stripped_so_a_name_inside_the_core_is_no_match() -> None:
    matcher = PayeeMatcher(LOOKUP)
    # Only a core that *is* a payee's name answers when nothing was stripped.
    assert matcher.match("unreadable", core="corner grocer west") is None
    assert matcher.match("unreadable", core="corner grocer") == GROCER
    assert matcher.match("unreadable", core="corner grocer west", via="square") == GROCER


@pytest.mark.parametrize(
    "description",
    [
        # The core is cut to six words: the second payee stands behind the cut.
        "Lantern Fuel weekly delivery charge for Oakfield Rentals",
        "LANTERN FUEL 0042 DELIVERY ORDER REF 7781 OAKFIELD RENTALS",
        # Also behind a processor: the other payee is not the processor.
        "PAYPAL *LANTERN FUEL WEEKLY DELIVERY CHARGE FOR OAKFIELD RENTALS",
        "SQ *LANTERN FUEL WEEKLY DELIVERY CHARGE FOR OAKFIELD RENTALS",
    ],
)
def test_a_second_payee_past_the_end_of_the_core_is_still_no_match(description: str) -> None:
    assert PayeeMatcher(BEHIND).match_descriptor(description) is None


@pytest.mark.parametrize(
    "description",
    [
        # Between people the core is the app's name: never the payee of the line.
        "VENMO PAYMENT 1027 MARTA LINDQVIST",
        # Not read as a line between people, but it names two payees all the same.
        "APPLE CASH SENT MONEY TO MARTA LINDQVIST",
    ],
)
def test_a_line_naming_an_app_and_a_person_is_no_match(description: str) -> None:
    assert PayeeMatcher(PEOPLE).match_descriptor(description) is None


def test_a_line_between_people_never_takes_the_app_from_its_core() -> None:
    # "SQC*" is how Square prints Cash App; the whole text does not say so.
    cash_app = uuid.uuid4()
    matcher = PayeeMatcher({**LOOKUP, normalize_payee("Cash App"): cash_app})
    assert matcher.match_descriptor("SQC*ELSPETH 4410") is None
    assert matcher.match("SQC*ELSPETH 4410", core="cash app") == cash_app  # why the kind is read

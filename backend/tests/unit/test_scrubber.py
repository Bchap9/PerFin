# SPDX-License-Identifier: AGPL-3.0-or-later
"""Properties of the free-text scrubber (app.core.scrubber, AI_ACCESS.md M2).

The leak and false-positive corpora live in ``tests/security/ai``; here:
idempotence and normalisation on arbitrary text (hypothesis), no digit run of
seven or more digits ever survives, and the merchant-location split.
"""

from __future__ import annotations

import re

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.scrubber import Scrubber, classify_digit_run, luhn_ok, member_terms, normalise
from app.domain.ai.policy import account_labels, split_merchant_location

SCRUBBER = Scrubber(member_terms(["Jane Q. Doe"], ["jdoe42"], ["jane.doe@example.org"]), ["Acme"])

_ALPHABET = st.sampled_from(
    [
        *list("0123456789 .-()#*xX@[]ACTDEPSOBjanedoMIN,:/+_"),
        "\u200d",
        "\xad",
        "\uff14",
        "\u0663",
        "\xa0",
        "\u2026",
        "\u00e9",
        "Jane",
        "acct",
        "St",
    ]
)
TEXT = st.lists(_ALPHABET, max_size=80).map("".join)


@settings(max_examples=400, deadline=None)
@given(TEXT)
def test_idempotent(text: str) -> None:
    once = SCRUBBER.scrub_text(text)
    assert SCRUBBER.scrub_text(once) == once


@settings(max_examples=400, deadline=None)
@given(TEXT)
def test_no_long_digit_run_survives(text: str) -> None:
    out = SCRUBBER.scrub_text(text)
    for run in re.findall(r"\d(?:[ .\-()/_,]{0,3}\d)*", out):
        digits = re.sub(r"\D", "", run)
        assert len(digits) < 7 or classify_digit_run(run) is None, (text, out)


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=60))
def test_normalise_is_idempotent(text: str) -> None:
    assert normalise(normalise(text)) == normalise(text)


def test_luhn() -> None:
    assert luhn_ok("4111111111111111")
    assert not luhn_ok("4111111111111112")


def test_digit_runs() -> None:
    assert classify_digit_run("2026-01-15") is None
    assert classify_digit_run("1234.56") is None
    assert classify_digit_run("1234") is None
    assert classify_digit_run("2026-01-15 12.34") is None
    assert classify_digit_run("12.34 5551234567") is not None
    assert classify_digit_run("123-45-6789") == "[GOVID]"
    assert classify_digit_run("(555) 123-4567") == "[PHONE]"
    assert classify_digit_run("4111 1111 1111 1111") == "[ACCT]"


def test_split_merchant_location() -> None:
    loc = split_merchant_location("STARBUCKS #1234 SEATTLE WA")
    assert (loc.text, loc.city, loc.state) == ("STARBUCKS #1234", "SEATTLE", "WA")
    loc = split_merchant_location("BLUE BOTTLE SAN FRANCISCO CA 94107")
    assert (loc.text, loc.city, loc.state) == ("BLUE BOTTLE", "SAN FRANCISCO", "CA")
    assert split_merchant_location("NETFLIX.COM").city is None
    assert split_merchant_location("SEATTLE WA").city is None  # nothing would be left
    assert split_merchant_location("PAYMENT XX ZZ").state is None  # not a state


def test_account_labels() -> None:
    labels = account_labels([(1, "checking"), (2, "checking"), (3, "credit_card")])
    assert labels == {1: "Checking A", 2: "Checking B", 3: "Credit card A"}
    many = account_labels([(i, "savings") for i in range(28)])
    assert many[26] == "Savings AA"


def test_digit_runs_with_slash_underscore_comma() -> None:
    """P6B-SR-R1-02: "/", "_" and "," join groups; amounts and dates survive."""
    assert classify_digit_run("1234/5678/9012") == "[ACCT]"
    assert classify_digit_run("1234_5678_9012") == "[ACCT]"
    assert classify_digit_run("1234,5678,9012") == "[ACCT]"
    assert classify_digit_run("123,456,789") == "[GOVID]"
    assert classify_digit_run("1,234,567") == "[ACCT]"
    assert classify_digit_run("12,345.67") is None
    assert classify_digit_run("1,234,567.89") is None
    assert classify_digit_run("12,345") is None
    assert classify_digit_run("01/15/2026") is None
    assert classify_digit_run("2026/01/15") is None
    assert classify_digit_run("2026-03-14, 12.34") is None


def test_member_terms_short_parts_and_household_name() -> None:
    """P6B-SR-R1-04: two-letter surnames next to a name, the family name."""
    terms = member_terms(["Jane Li", "Bo Wolcott"], household_names=["The Wolcott Family"])
    scrubber = Scrubber(terms)
    assert scrubber.scrub_text("ZELLE TO LI JANE") == "ZELLE TO [MEMBER]"
    assert scrubber.scrub_text("Gift from Bo Wolcott") == "Gift from [MEMBER]"
    assert scrubber.scrub_text("WOLCOTT LI") == "[MEMBER]"
    assert scrubber.scrub_text("SMITH FAMILY TRUST") == "SMITH FAMILY TRUST"
    assert scrubber.scrub_text("WOLCOTT FAMILY TRUST") == "[MEMBER] FAMILY TRUST"
    # A short part alone, and the household stopwords, are not identity.
    assert scrubber.scrub_text("LI NING STORE") == "LI NING STORE"
    assert scrubber.scrub_text("The Home Depot") == "The Home Depot"
    assert scrubber.scrub_text("BO DIDDLEY RECORDS") == "BO DIDDLEY RECORDS"


def test_fingerprint_follows_the_terms() -> None:
    """P6B-SR-R1-05: the router's cache key changes when the terms do."""
    a = Scrubber(member_terms(["Jane Doe"]), ["Acme"])
    assert a.fingerprint == Scrubber(member_terms(["JANE DOE"]), ["acme"]).fingerprint
    assert a.fingerprint != Scrubber(member_terms(["Jane Doe", "Max Roe"]), ["Acme"]).fingerprint
    assert a.fingerprint != Scrubber(member_terms(["Jane Doe"]), ["Acme", "Oak"]).fingerprint
    assert "Jane" not in a.fingerprint


def test_known_account_numbers() -> None:
    """P6B-SR-R2-01: a stored last four is an account number wherever it stands
    alone; amounts and longer numbers around it keep their own rules."""
    s = Scrubber(account_numbers=["6611", None, "not-digits"])
    assert s.scrub_text("Sapphire 6611") == "Sapphire [ACCT]"
    assert s.scrub_text("from 6611 to savings") == "from [ACCT] to savings"
    assert s.scrub_text("Paid 6611.00 rent") == "Paid 6611.00 rent"
    assert s.scrub_text("Total 1,6611") == "Total 1,6611"
    assert s.scrub_text("Store 66110") == "Store 66110"
    assert Scrubber().scrub_text("Sapphire 6611") == "Sapphire 6611"
    assert s.fingerprint != Scrubber().fingerprint
    assert s.fingerprint != Scrubber(account_numbers=["6612"]).fingerprint
    assert "6611" not in s.fingerprint
    # A replacement that opens a word boundary is followed to a fixed point.
    once = s.scrub_text("6611visa00")
    assert once == "[ACCT][ACCT]"
    assert s.scrub_text(once) == once

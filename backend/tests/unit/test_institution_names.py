# SPDX-License-Identifier: AGPL-3.0-or-later
"""Institution names that mean the same institution, and last four digits in more wordings."""

from __future__ import annotations

import pytest

from app.ingestion.classify.accounts import detect_last4
from app.ingestion.classify.text import DocumentView
from app.ingestion.match.institutions import institution_key, pick, same_institution


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Chase", "JPMorgan Chase Bank, N.A.", True),
        ("Fidelity", "Fidelity Investments", True),
        ("CHASE BANK", "chase", True),
        ("Navy Federal Credit Union", "Navy Federal", True),
        ("Synthetic Savings Bank", "Synthetic Savings", True),
        ("First National Bank", "First Republic", False),
        ("Capital One", "Capital Bank", False),
        ("American Express", "Bank of America", False),
        ("Ally Bank", "Discover Bank", False),
        ("Bank", "Bank", True),
        ("", "Chase", False),
    ],
)
def test_same_institution(a: str, b: str, same: bool) -> None:
    assert same_institution(a, b) is same
    assert same_institution(b, a) is same


def test_generic_words_are_not_distinctive() -> None:
    assert institution_key("The Bank of Synthetic, N.A.") == frozenset({"synthetic"})
    assert institution_key("Investments Inc") == frozenset()


def test_pick_prefers_an_exact_name_and_refuses_ties() -> None:
    named = [(1, "Fidelity Investments"), (2, "Fidelity"), (3, "Chase")]
    assert pick("fidelity", named) == 2  # exact first
    assert pick("Fidelity Investments Inc", named) is None  # two fuzzy matches
    assert pick("JPMorgan Chase Bank", named) == 3
    assert pick("Wells Fargo", named) is None
    assert pick(None, named) is None


@pytest.mark.parametrize(
    "line",
    [
        "Checking x1234",
        "Account: x-1234",
        "Card ...1234",
        "Card …1234",
        "Card ends in 1234",
        "Account ending with 1234",
        "Last 4 digits of account number: 1234",
        "last four 1234",
    ],
)
def test_more_ways_to_print_the_last_four_digits(line: str) -> None:
    found = detect_last4(DocumentView(lines=(line,), header=()))
    assert (found.value, found.source) == ("1234", "mask")


def test_digits_inside_longer_numbers_are_not_a_mask() -> None:
    assert detect_last4(DocumentView(lines=("Invoice x12345 total 3.00",), header=())).value is None
    assert detect_last4(DocumentView(lines=("Version 1.2.1234",), header=())).value is None

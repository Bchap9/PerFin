# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lines that name no merchant but say what the money was: a fee, interest,
rent, power, pay, cash.

The bank's own wording (a fee, interest) is a kind clean-up reads, and its
category is stated. Wording a household or a biller writes ("RENT PAYMENT",
"ELECTRIC BILL", "EMPLOYEE WAGES") is a keyword, and only ever suggested.
A word for pay answers money coming in only. A word inside another word, or
the start of a shop's name, answers nothing. A cheque and a deposit stay
unguessed (``engine.NEVER_GUESSED``).

Real clean-up, real dictionary, a new household's default categories; every
line is invented.
"""

from __future__ import annotations

import pytest

from app.domain.categorisation import engine
from app.domain.categorisation.memory import Direction
from tests.fixtures.quality import DEFAULT_CATEGORIES, NOW

_KEY_OF = {category_id: key for key, category_id in DEFAULT_CATEGORIES.items()}


def _answer(raw: str, direction: Direction) -> tuple[str | None, bool]:
    """The category key given to a new row of ``raw``, and whether as a fact."""
    decision = engine.decide(
        engine.identify(raw),
        direction,
        learned=None,
        payee_default=None,
        categories=DEFAULT_CATEGORIES,
        now=NOW,
    )
    if decision.category_id is not None:
        return _KEY_OF[decision.category_id], True
    if decision.suggested_category_id is not None:
        return _KEY_OF[decision.suggested_category_id], False
    return None, False


@pytest.mark.parametrize(
    ("raw", "direction", "category"),
    [
        ("BANK FEE", "out", "financial.bank_fees"),
        ("OVERLIMIT FEE", "out", "financial.bank_fees"),
        ("OVER LIMIT FEE", "out", "financial.bank_fees"),
        ("RETURNED CHECK FEE", "out", "financial.bank_fees"),
        ("INTEREST FEE", "out", "financial.interest_paid"),
        ("SAVINGS INTEREST CREDIT", "in", "income.interest"),
    ],
)
def test_a_bank_s_own_line_is_stated(raw: str, direction: Direction, category: str) -> None:
    assert _answer(raw, direction) == (category, True)
    assert engine.identify(raw).key is None


@pytest.mark.parametrize(
    ("raw", "direction", "category"),
    [
        ("DIVIDEND EARNED", "in", "income.dividends"),
        ("RENT PAYMENT", "out", "housing.rent"),
        ("APARTMENT RENT PAYMENT", "out", "housing.rent"),
        ("MONTHLY RENT 0412", "out", "housing.rent"),
        ("LANTERN ELECTRICITY 0412", "out", "utilities.electricity"),
        ("ELECTRIC BILL", "out", "utilities.electricity"),
        ("AUTO WITHDRAWAL LANTERN ELECTRIC BILL 0412", "out", "utilities.electricity"),
        ("UTILITY", "out", "utilities.other"),
        ("LANTERN CITY UTILITY BILLING", "out", "utilities.other"),
        ("EMPLOYER", "in", "income.salary"),
        ("EMPLOYEE WAGES", "in", "income.salary"),
        ("CASH DEPOSIT", "in", "transfers.cash"),
        ("WITHDRAWAL", "out", "transfers.cash"),
        ("LANTERN GRAND THEATERS", "out", "leisure.events"),
        ("MAPLE CINEMAS 12", "out", "leisure.events"),
    ],
)
def test_wording_that_says_what_it_was_is_suggested(
    raw: str, direction: Direction, category: str
) -> None:
    assert _answer(raw, direction) == (category, False)


@pytest.mark.parametrize(
    ("raw", "direction"),
    [
        # "rent" inside a word, or opening a shop's name.
        ("PARENT TEACHER ASSOC", "out"),
        ("RENT-A-CENTER #0412", "out"),
        ("LANTERN RENTALS", "out"),
        # "electric" alone is a shop as often as a bill.
        ("ELECTRIC GUITAR CENTER", "out"),
        # "interest" alone, and the bank's words in a shop's name.
        ("INTEREST BOOKS AND MAPS", "out"),
        ("FOOD BANK FEE DONATION", "out"),
        # Pay goes out to whoever is paid; it is nobody's salary.
        ("EMPLOYEE WAGES", "out"),
        ("EMPLOYER", "out"),
        ("DIVIDEND EARNED", "out"),
        # Who an automatic withdrawal paid is not cash.
        ("AUTOMATIC WITHDRAWAL LANTERN ATHLETIC CLUB", "out"),
        # Cheques and deposits are never guessed.
        ("CHECK 1042", "out"),
        ("DEPOSIT", "in"),
        ("ATM CHECK DEPOSIT 04/12", "in"),
    ],
)
def test_ambiguous_wording_answers_nothing_wrong(raw: str, direction: Direction) -> None:
    category, _ = _answer(raw, direction)
    assert category not in {
        "housing.rent",
        "utilities.electricity",
        "financial.bank_fees",
        "financial.interest_paid",
        "income.salary",
        "income.dividends",
        "income.interest",
        "transfers.cash",
    }, (raw, category)

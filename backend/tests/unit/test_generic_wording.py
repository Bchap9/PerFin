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
        # A bill of a business nobody's dictionary knows.
        ("QUARRYVALE APTS DES:RENT ID:U0000001 CO ID:0000000001 WEB", "out", "housing.rent"),
        ("BRISTLECONE WATER DIST BILLPAY", "out", "utilities.water"),
        ("ELKTON SEWER DIST PPD ID: 0000000001", "out", "utilities.water"),
        ("LANTERN CITY SEWER", "out", "utilities.water"),
        ("PINEGATE SEWER DISTRICT", "out", "utilities.water"),
        ("PINEGATE SEWER SERVICE 0412", "out", "utilities.water"),
        ("WILLOWMERE TUITION 0412", "out", "family.education"),
        # The entry description of a labelled ACH line says what the company
        # was paid for when its name does not.
        (
            "WILLOWMERE ACADEMY DES:TUITION ID:%1 CO ID:0000000002 PPD",
            "out",
            "family.education",
        ),
        ("ORIG CO NAME:QUARRYVALE MGMT CO ENTRY DESCR:RENT PMT", "out", "housing.rent"),
        # Pay coming in by ACH says so in its entry description too.
        ("QUARRYVALE HOLDINGS DES:WAGES ID:%1 CO ID:0000000003 PPD", "in", "income.salary"),
        # Lessons in a sport are a pastime, not school.
        ("PINEGATE SWIM SCHOOL", "out", "leisure.hobbies"),
        ("ELKTON SWIMMING SCHOOL 0412", "out", "leisure.hobbies"),
        ("WILLOWMERE DANCE SCHOOL", "out", "leisure.hobbies"),
        ("BRISTLECONE MUSIC SCHOOL 0412", "out", "leisure.hobbies"),
        ("QUARRYVALE KARATE SCHOOL", "out", "leisure.hobbies"),
        ("LANTERN KARATE 0412", "out", "leisure.hobbies"),
        ("PINEGATE MARTIAL ARTS", "out", "leisure.hobbies"),
        # A driving school, and a school of no other word, are school.
        ("ELKTON DRIVING SCHOOL", "out", "family.education"),
        ("WILLOWMERE SCHOOL 0412", "out", "family.education"),
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
        ("QUARRYVALE HOLDINGS DES:WAGES ID:%1 CO ID:0000000003 PPD", "out"),
        # Who an automatic withdrawal paid is not cash.
        ("AUTOMATIC WITHDRAWAL LANTERN ATHLETIC CLUB", "out"),
        # Nor what a credit union's withdrawal paid by ACH, card or bill pay,
        # moved to another account, or paid by share draft.
        ("Withdrawal ACH QUARRYVALE MUTUAL", "out"),
        ("Withdrawal Debit Card LANTERN ATHLETIC CLUB", "out"),
        ("Withdrawal POS #0042 LANTERN ATHLETIC CLUB", "out"),
        ("Withdrawal Bill Payment #0042 QUARRYVALE MUTUAL", "out"),
        ("Withdrawal Home Banking Transfer To Share 0001", "out"),
        ("Withdrawal by Check 0042", "out"),
        ("Share Draft Withdrawal", "out"),
        # Money coming in is no withdrawal, money going out no deposit.
        ("WITHDRAWAL", "in"),
        ("CASH DEPOSIT", "out"),
        # Cheques and deposits are never guessed.
        ("CHECK 1042", "out"),
        ("CHECK WITHDRAWAL", "out"),
        ("CHK WITHDRAWAL 1042", "out"),
        ("DEPOSIT", "in"),
        ("ATM CHECK DEPOSIT 04/12", "in"),
        # An entry description of no telling word answers nothing.
        ("QUARRYVALE MGMT DES:PAYMENT ID:%1 CO ID:0000000003 PPD", "out"),
        ("QUARRYVALE INS DES:INS PREM ID:%1 CO ID:0000000004 PPD", "out"),
        # A company that pulls money by ACH is paid; it is no cash.
        ("QUARRYVALE HOLDINGS DES:WITHDRAWAL ID:%1 CO ID:0000000009 PPD", "out"),
        ("QUARRYVALE HOLDINGS DES:CASH DEPOSIT ID:%1 CO ID:0000000009 PPD", "in"),
        ("ORIG CO NAME:QUARRYVALE HOLDINGS CO ENTRY DESCR:WITHDRAWAL", "out"),
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


@pytest.mark.parametrize(
    "raw", ["Withdrawal Debit Card NETFLIX.COM", "WITHDRAWAL POS #0042 NETFLIX.COM"]
)
def test_the_merchant_behind_a_credit_union_withdrawal_is_read(raw: str) -> None:
    assert engine.identify(raw).key == "netflix"
    assert _answer(raw, "out") == ("leisure.subscriptions", True)


@pytest.mark.parametrize(
    "raw",
    [
        "ROTO SEWER DRAIN CLEANING",
        "SEWER PIPE SUPPLY",
        "QUARRYVALE SEWER AND DRAIN 0412",
    ],
)
def test_a_trade_that_works_on_sewers_is_no_water_bill(raw: str) -> None:
    # A plumber or a pipe supplier is paid for work, not for the household's
    # water: only a sewer district's or a city's wording says that.
    category, _ = _answer(raw, "out")
    assert category != "utilities.water", raw


@pytest.mark.parametrize(
    ("raw", "direction"),
    [
        ("QUARRYVALE HOLDINGS DES:PHARMACY ID:%1 CO ID:0000000006 PPD", "out"),
        ("QUARRYVALE HOLDINGS DES:GYM ID:%1 CO ID:0000000007 PPD", "out"),
        ("ORIG CO NAME:QUARRYVALE HOLDINGS CO ENTRY DESCR:PARKING", "out"),
    ],
)
def test_an_entry_description_suggests_only_a_bill(raw: str, direction: Direction) -> None:
    # The entry description is read to find a bill (rent, tuition) at a
    # company whose name says nothing; any other word there answers nothing.
    assert _answer(raw, direction) == (None, False)


@pytest.mark.parametrize(
    "raw",
    [
        "QUARRYVALE INS DES:INS PREM ID:%1 CO ID:0000000004 PPD",
        "QUARRYVALE MUTUAL DES:INSURANCE PREMIUM ID:%1 CO ID:0000000005 PPD",
        "ORIG CO NAME:QUARRYVALE LIFE CO ENTRY DESCR:INSURANCE",
    ],
)
def test_an_insurance_premium_stays_unguessed_by_its_entry_description(raw: str) -> None:
    # The owner's decision: "insurance premium" says nothing about which
    # insurance, so the entry description suggests nothing either.
    assert _answer(raw, "out") == (None, False)

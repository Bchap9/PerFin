# SPDX-License-Identifier: AGPL-3.0-or-later
"""QIF balance records are not transactions (synthetic files).

A record whose payee is a balance (``Opening Balance``, ``Balance brought
forward``...) with nothing else that makes it a transaction (no split, no
category that is not an account transfer, no memo of its own) is the
statement's opening balance when it is the section's first record and of an
opening kind; otherwise it is left out with a ``balance_record_skipped:<n>``
warning, so the document still says what it held.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.ingestion.extract.qif import extract_qif
from app.ingestion.parsers.structured import from_statement
from app.ingestion.sandbox.protocol import Balance, ExtractOptions

OPTIONS = ExtractOptions()

BODY = (
    b"D03/14/2026\nT-23.80\nPOrchard Row Market\nLGroceries\n^\n"
    b"D03/27/2026\nT410.00\nPStipend\nLIncome\n^\n"
)


def _qif(*records: bytes) -> bytes:
    return b"!Account\nNEveryday\nTBank\n^\n!Type:Bank\n" + b"".join(records)


def _payees(data: bytes) -> list[str | None]:
    return [t.payee for t in extract_qif(data, OPTIONS, "qif").statements[0].transactions]


# ---------------------------------------------------------------------------
# QIF balance records
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payee",
    [
        "Opening Balance",
        "OPENING BALANCE",
        "  opening   balance ",
        "Beginning Balance",
        "Previous Balance",
        "Balance Forward",
        "Balance Brought Forward",
    ],
)
def test_qif_first_record_that_is_an_opening_balance_is_the_opening_balance(payee: str) -> None:
    opening = f"D03/01/2026\nT1250.00\nCX\nP{payee}\nL[Everyday]\n^\n".encode()
    result = extract_qif(_qif(opening, BODY), OPTIONS, "qif")
    (statement,) = result.statements
    assert [t.payee for t in statement.transactions] == ["Orchard Row Market", "Stipend"]
    assert statement.opening_balance == Balance(amount=Decimal("1250.00"), as_of=date(2026, 3, 1))
    # The register starts on the opening balance's day.
    assert statement.period_start == date(2026, 3, 1)
    assert statement.period_end == date(2026, 3, 27)
    assert not [w for w in result.warnings if "skipped" in w]


def test_qif_opening_balance_of_zero_is_still_no_transaction() -> None:
    opening = b"D03/01/2026\nT0.00\nPOpening Balance\nL[Everyday]\n^\n"
    result = extract_qif(_qif(opening, BODY), OPTIONS, "qif")
    (statement,) = result.statements
    assert len(statement.transactions) == 2
    assert statement.opening_balance == Balance(amount=Decimal("0.00"), as_of=date(2026, 3, 1))


def test_qif_opening_balance_without_a_category_is_the_opening_balance() -> None:
    opening = b"D03/01/2026\nT80.00\nPOpening Balance\n^\n"
    statement = extract_qif(_qif(opening, BODY), OPTIONS, "qif").statements[0]
    assert len(statement.transactions) == 2
    assert statement.opening_balance is not None


@pytest.mark.parametrize("payee", ["Closing Balance", "Ending Balance", "Balance Carried Forward"])
def test_qif_closing_balance_record_is_left_out_and_counted(payee: str) -> None:
    closing = f"D03/31/2026\nT1636.20\nP{payee}\n^\n".encode()
    result = extract_qif(_qif(BODY, closing), OPTIONS, "qif")
    (statement,) = result.statements
    assert [t.payee for t in statement.transactions] == ["Orchard Row Market", "Stipend"]
    assert statement.opening_balance is None
    assert statement.ledger_balance is None
    assert "balance_record_skipped:1" in result.warnings
    assert not [w for w in result.warnings if w.startswith("transaction_skipped")]


def test_qif_opening_balance_that_is_not_the_first_record_is_left_out_and_counted() -> None:
    late = b"D03/20/2026\nT900.00\nPOpening Balance\nL[Everyday]\n^\n"
    first, second = BODY.split(b"^\n")[:2]
    result = extract_qif(_qif(first + b"^\n", late, second + b"^\n"), OPTIONS, "qif")
    (statement,) = result.statements
    assert [t.payee for t in statement.transactions] == ["Orchard Row Market", "Stipend"]
    assert statement.opening_balance is None
    assert "balance_record_skipped:1" in result.warnings


def test_each_section_has_its_own_opening_balance() -> None:
    card = b"!Account\nNCard\nTCCard\n^\n!Type:CCard\n"
    data = (
        _qif(b"D03/01/2026\nT10.00\nPOpening Balance\nL[Everyday]\n^\n", BODY)
        + card
        + b"D03/02/2026\nT-55.00\nPOpening Balance\nL[Card]\n^\n"
        + BODY
    )
    result = extract_qif(data, OPTIONS, "qif")
    assert [s.opening_balance for s in result.statements] == [
        Balance(amount=Decimal("10.00"), as_of=date(2026, 3, 1)),
        Balance(amount=Decimal("-55.00"), as_of=date(2026, 3, 2)),
    ]
    assert [len(s.transactions) for s in result.statements] == [2, 2]


@pytest.mark.parametrize(
    "record",
    [
        # A category that is no account transfer: money that moved, named oddly.
        b"D03/01/2026\nT12.00\nPOpening Balance\nLInterest Income\n^\n",
        # A split.
        b"D03/01/2026\nT12.00\nPOpening Balance\nSFees\n$12.00\n^\n",
        # A memo of its own.
        b"D03/01/2026\nT12.00\nPOpening Balance\nMcorrection for March\n^\n",
        # More than a balance phrase.
        b"D03/01/2026\nT12.00\nPOpening Balance Fee\n^\n",
    ],
)
def test_qif_balance_wording_with_another_meaning_stays_a_transaction(record: bytes) -> None:
    result = extract_qif(_qif(record, BODY), OPTIONS, "qif")
    (statement,) = result.statements
    assert len(statement.transactions) == 3
    assert statement.opening_balance is None
    assert not [w for w in result.warnings if "skipped" in w]


def test_qif_opening_balance_reaches_the_parsed_statement() -> None:
    opening = b"D03/01/2026\nT1250.00\nPOpening Balance\nL[Everyday]\n^\n"
    parsed = from_statement(extract_qif(_qif(opening, BODY), OPTIONS, "qif").statements[0])
    assert parsed.opening_balance == Decimal("1250.00")
    assert parsed.closing_balance is None
    assert len(parsed.transactions) == 2

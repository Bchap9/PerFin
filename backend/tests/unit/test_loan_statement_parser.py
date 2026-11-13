# SPDX-License-Identifier: AGPL-3.0-or-later
"""The loan statement parser on synthetic PDFs (``tests/fixtures/synth_loans.py``).

PDFs are extracted in process (``extract_pdf``) and parsed by the registry
as the worker does: the loan parser must win over the bank statement
parsers, read the split and balances exactly, and hold back a statement
whose figures do not reconcile (SECURITY.md C12).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers import confidence
from app.ingestion.parsers.base import Cell, DocumentView, ViewLine
from app.ingestion.parsers.loan_statement import (
    MONEY_FIELDS,
    LoanStatementParser,
    reconcile_loan,
)
from app.ingestion.parsers.registry import parse_extraction, reparse
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, LoanStatement
from tests.fixtures import synth_loans as synth
from tests.fixtures.synth_pdf_statements import sample_national_pdf, sample_national_statement

OPTIONS = ExtractOptions(previews=False, ocr=False)


def _parse(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return parse_extraction(result, threshold=confidence.DEFAULT_THRESHOLD)


def _loan(result: ExtractResult) -> LoanStatement:
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == "loan_statement_generic"
    assert result.parse.loan is not None
    assert result.statements == []
    return result.parse.loan


@pytest.mark.parametrize("layout", ["classic", "compact"])
def test_statement_is_read_exactly(layout: synth.Layout) -> None:
    for stmt in synth.loan_series(3, extra={2: Decimal("1000.00")}, late_fee_month=3):
        loan = _loan(_parse(synth.loan_statement_pdf(stmt, layout=layout)))
        assert loan.lender == synth.LENDER
        assert loan.last4 == synth.LAST4
        assert loan.currency == "USD"
        assert (loan.statement_date, loan.paid_on) == (stmt.statement_date, stmt.paid_on)
        assert (loan.principal, loan.interest, loan.escrow, loan.fees) == (
            stmt.principal,
            stmt.interest,
            stmt.escrow,
            stmt.fees,
        )
        assert loan.total == stmt.total
        assert (loan.previous_balance, loan.balance) == (stmt.previous_balance, stmt.balance)
        assert loan.rate == Decimal("0.06125")
        assert (loan.next_due_date, loan.next_amount_due) == (stmt.next_due, stmt.next_amount)
        assert loan.reconciliation is not None
        assert loan.reconciliation.status == "ok"
        assert loan.reconciliation.checks == ["parts", "balance"]
        assert loan.low_confidence == []
        assert loan.confidence["principal"] == 1.0


@pytest.mark.parametrize("tamper", ["parts", "balance"])
def test_tampered_statement_does_not_reconcile(tamper: synth.Tamper) -> None:
    stmt = synth.loan_series(1)[0]
    loan = _loan(_parse(synth.loan_statement_pdf(stmt, tamper=tamper)))
    assert loan.reconciliation is not None
    assert loan.reconciliation.status == "mismatch"
    assert loan.reconciliation.failed == [tamper]
    assert {"principal", "interest", "balance"} <= set(loan.low_confidence)


def test_reparse_keeps_the_loan_parser() -> None:
    result = _parse(synth.loan_statement_pdf(synth.loan_series(1)[0]))
    again = reparse(result)
    assert _loan(again) == _loan(result)


def test_bank_statements_are_not_loan_statements() -> None:
    data = sample_national_pdf(sample_national_statement())
    result = extract_pdf(data, OPTIONS, "pdf")
    assert LoanStatementParser().detect(DocumentView.from_result(result)) == 0.0


def test_reconcile_without_figures_is_unavailable() -> None:
    assert reconcile_loan({"principal": Decimal("1")}).status == "unavailable"
    checked = reconcile_loan(
        {
            "principal": Decimal("10.00"),
            "interest": Decimal("5.00"),
            "total": Decimal("15.00"),
            "previous": Decimal("100.00"),
            "balance": Decimal("90.00"),
        }
    )
    assert (checked.status, checked.checks) == ("ok", ["parts", "balance"])


def test_series_dates_follow_the_schedule() -> None:
    first, second = synth.loan_series(2)
    assert (first.paid_on, second.paid_on) == (date(2024, 4, 1), date(2024, 5, 1))
    assert second.previous_balance == first.balance


def _as_scan(data: bytes, *, clean: float, footer: float) -> DocumentView:
    """The PDF's lines as an OCR view: every cell at ``clean``, the last line at ``footer``."""
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    view = DocumentView.from_result(result)
    last = len(view.lines) - 1
    lines = tuple(
        replace(
            line,
            cells=tuple(replace(c, conf=footer if at == last else clean) for c in line.cells),
        )
        for at, line in enumerate(view.lines)
    )
    return replace(view, lines=lines, source_format="image")


def test_loan_fields_scale_by_their_own_cells() -> None:
    stmt = synth.loan_series(1)[0]
    view = _as_scan(synth.loan_statement_pdf(stmt, layout="classic"), clean=0.96, footer=0.02)
    assert view.lines[-1].norm.startswith("synthetic test document")
    outcome = LoanStatementParser().parse(view, threshold=confidence.DEFAULT_THRESHOLD)
    loan = outcome.loan
    assert loan is not None
    assert loan.reconciliation is not None
    assert loan.reconciliation.status == "ok"
    assert {name: loan.confidence[name] for name in MONEY_FIELDS} == dict.fromkeys(
        MONEY_FIELDS, 1.0
    )
    for name in ("paid_on", "statement_date", "rate", "lender", "last4"):
        assert loan.confidence[name] == pytest.approx(confidence.GENERIC * 0.96), name
    assert loan.low_confidence == []


def test_tampered_scan_scales_its_figures_by_their_cells() -> None:
    stmt = synth.loan_series(1)[0]
    data = synth.loan_statement_pdf(stmt, layout="classic", tamper="parts")
    view = _as_scan(data, clean=0.96, footer=0.02)
    loan = LoanStatementParser().parse(view, threshold=confidence.DEFAULT_THRESHOLD).loan
    assert loan is not None
    assert loan.confidence["principal"] == pytest.approx(confidence.UNRECONCILED * 0.96)


def _text_view(*texts: str) -> DocumentView:
    """A one-page text view of whole lines (one cell each)."""
    lines = tuple(
        ViewLine(1, at, (Cell(text, 72.0, 40.0 + 16 * at, 400.0, 50.0 + 16 * at),), text, 10.0)
        for at, text in enumerate(texts)
    )
    return DocumentView(lines=lines, source_format="pdf", truncated=False, page_count=1)


_RECEIPT_LINES = (
    "Placeholder Home Lending",
    "Mortgage Statement",
    "Payment received 04/01/2024",
    "Principal 243.00",
    "Interest 1,276.03",
    "Total payment received 1,519.03",
)


@pytest.mark.parametrize(
    ("printed", "rate"),
    [
        ("Interest rate 4, 75%", None),
        ("Interest rate 4,75%", None),
        ("Interest rate 4. 75%", None),
        ("Interest rate 4.75%", Decimal("0.0475")),
        ("Interest rate 6.125 %", Decimal("0.06125")),
        ("Interest rate: 7%", Decimal("0.07")),
    ],
)
def test_split_percentage_is_not_a_rate(printed: str, rate: Decimal | None) -> None:
    view = _text_view(*_RECEIPT_LINES, printed)
    loan = LoanStatementParser().parse(view, threshold=confidence.DEFAULT_THRESHOLD).loan
    assert loan is not None
    assert loan.principal == Decimal("243.00")
    assert loan.rate == rate


@pytest.mark.parametrize(
    ("printed", "lender"),
    [
        ("Lender: Example Lending Co", "Example Lending Co"),
        ("Lender paid expenses 0.00", "Placeholder Home Lending"),
        ("Lender Example Lending Co", "Placeholder Home Lending"),
    ],
)
def test_a_lender_line_needs_its_colon(printed: str, lender: str) -> None:
    view = _text_view(*_RECEIPT_LINES, printed)
    loan = LoanStatementParser().parse(view, threshold=confidence.DEFAULT_THRESHOLD).loan
    assert loan is not None
    assert loan.lender == lender


@pytest.mark.parametrize(
    ("printed", "last4"),
    [
        ("Loan number XXXXXX4821", "4821"),
        ("Loan number 0004417755", "7755"),
        ("Loan No. 00-0441-7755", "7755"),
        ("Account number: 000 441 7755", "7755"),
        ("Account no 0004417755 Statement date 04/15/2024", "7755"),
        # Too short to be a loan number, or run into the figure after it: not read.
        ("Loan number 7755", None),
        ("Loan number 0004417755 03/20/2026", None),
        ("Reference 0004417755", None),
    ],
)
def test_last_four_of_a_number_printed_in_full(printed: str, last4: str | None) -> None:
    view = _text_view(*_RECEIPT_LINES, printed)
    loan = LoanStatementParser().parse(view, threshold=confidence.DEFAULT_THRESHOLD).loan
    assert loan is not None
    assert loan.last4 == last4
    assert "0004417755" not in loan.model_dump_json()

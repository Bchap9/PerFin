# SPDX-License-Identifier: AGPL-3.0-or-later
"""The loan statement parser on synthetic PDFs (``tests/fixtures/synth_loans.py``).

PDFs are extracted in process (``extract_pdf``) and parsed by the registry
as the worker does: the loan parser must win over the bank statement
parsers, read the split and balances exactly, and hold back a statement
whose figures do not reconcile (SECURITY.md C12).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers import confidence
from app.ingestion.parsers.loan_statement import LoanStatementParser, reconcile_loan
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
    from app.ingestion.parsers.base import DocumentView

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

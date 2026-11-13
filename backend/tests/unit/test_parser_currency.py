# SPDX-License-Identifier: AGPL-3.0-or-later
"""A document's currency is an ISO code PerFin knows (``app.ingestion.parsers.currency``).

The word after "currency" is a code only when it is printed as three capital
letters and is an active ISO 4217 code; a heading such as "Currency Forward
Contracts" or a sentence ("currency for the period") names none.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.base import Cell, DocumentView, ViewLine
from app.ingestion.parsers.currency import known_currency, stated_currency
from app.ingestion.parsers.loan_statement import LoanStatementParser
from app.ingestion.parsers.payslip import PayslipParser
from app.ingestion.parsers.table_statement import TableStatementParser
from app.ingestion.sandbox.protocol import ExtractOptions
from tests.fixtures import synth_loans as loans
from tests.fixtures import synth_payslips as payslips
from tests.fixtures.synth_pdf_statements import generic_pdf
from tests.fixtures.synth_statements import sample_statement


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


# The parsers that read "Currency: ..." off a line of the page: a word after
# "currency" that is no code leaves each with the currency it reads without it.
NOT_A_CODE = ["Currency Forward Contracts", "currency for the period", "Currency: XQZ"]


def _view(data: bytes) -> DocumentView:
    result = extract_pdf(data, ExtractOptions(previews=False, ocr=False), "pdf")
    assert result.ok, result.error
    return DocumentView.from_result(result)


def _with_first_line(view: DocumentView, text: str) -> DocumentView:
    """``view`` with one more line above the rest of page 1."""
    top = min(line.bbox[1] for line in view.lines if line.page == 1)
    extra = ViewLine(
        page=1,
        index=0,
        cells=(Cell(text, 40.0, top - 20.0, 40.0 + 6.0 * len(text), top - 10.0),),
        text=text,
        height=10.0,
    )
    return replace(view, lines=(extra, *(replace(ln, index=ln.index + 1) for ln in view.lines)))


@pytest.mark.parametrize("added", NOT_A_CODE)
def test_a_word_after_currency_is_not_a_payslips_currency(added: str) -> None:
    view = _view(payslips.this_period_pdf())
    parser = PayslipParser()
    alone = parser.parse(view, threshold=0.85).payslip
    found = parser.parse(_with_first_line(view, added), threshold=0.85).payslip
    assert alone is not None
    assert found is not None
    assert found.currency == alone.currency
    assert found.net == alone.net


@pytest.mark.parametrize("added", NOT_A_CODE)
def test_a_word_after_currency_is_not_a_loans_currency(added: str) -> None:
    view = _view(loans.loan_statement_pdf(loans.loan_series(1)[0]))
    parser = LoanStatementParser()
    alone = parser.parse(view, threshold=0.85).loan
    found = parser.parse(_with_first_line(view, added), threshold=0.85).loan
    assert alone is not None
    assert found is not None
    assert found.currency == alone.currency == "USD"
    assert found.principal == alone.principal


@pytest.mark.parametrize("added", NOT_A_CODE)
def test_a_word_after_currency_is_not_a_bank_statements_currency(added: str) -> None:
    view = _view(generic_pdf(sample_statement(seed=7)))
    parser = TableStatementParser()
    (alone,) = parser.parse(view, threshold=0.85).statements
    (found,) = parser.parse(_with_first_line(view, added), threshold=0.85).statements
    assert found.account.currency == alone.account.currency
    assert found.account.currency not in {"FOR", "XQZ"}
    assert len(found.transactions) == len(alone.transactions)

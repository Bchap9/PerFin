# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bank statement rows as real statements print them (``synth_bank_layouts``).

The generic reader on rows whose cells sit a little off their columns:
figures right-aligned away from a left-aligned heading, a date printed so
close to the description that the two are one cell, a row that is one cell
altogether, and a zero printed beside the amount in the other money column.
Each rule has the line it must leave alone beside it: nothing here may turn
a line that is no transaction into one.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.base import Cell, DocumentView, ParseOutcome, ViewLine
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.table_statement import TableStatementParser
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, Statement
from tests.fixtures import synth_bank_layouts as synth

OPTIONS = ExtractOptions(previews=False, ocr=False)


def _read(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return parse_extraction(result, kind="statement")


def _only(result: ExtractResult) -> Statement:
    assert len(result.statements) == 1, result.statements
    return result.statements[0]


def _rows(statement: Statement) -> list[tuple[date, Decimal, str | None]]:
    return [(t.posted, t.amount, t.payee) for t in statement.transactions]


def _skipped(result: ExtractResult) -> int:
    assert result.parse is not None
    return result.parse.skipped_lines


def _status(statement: Statement) -> str:
    assert statement.reconciliation is not None
    return statement.reconciliation.status


# -- whole documents ------------------------------------------------------------


def test_right_aligned_figures_outside_their_heading_are_read() -> None:
    result = _read(synth.left_heading_right_figures())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["left_heading_right_figures"]
    assert _status(statement) == "ok"
    assert _skipped(result) == 0
    # The balance figures were as far from their heading: the last one closes.
    assert statement.ledger_balance is not None
    assert statement.ledger_balance.amount == Decimal("2193.26")
    assert statement.account.last4 == "2046"


def test_a_date_fused_with_the_description_is_split() -> None:
    result = _read(synth.tight_date_checking())
    statement = _only(result)
    expected = synth.EXPECTED_ROWS["tight_date_checking"]
    assert len(statement.transactions) == 7
    assert [t.posted for t in statement.transactions] == [posted for posted, _, _ in expected]
    assert not any((t.payee or "0")[0].isdigit() for t in statement.transactions)
    assert _rows(statement) == expected
    assert _status(statement) == "ok"
    assert _skipped(result) == 0


def test_fused_date_and_trailing_amount() -> None:
    result = _read(synth.tight_date_one_money_column())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["tight_date_one_money_column"]
    assert _status(statement) == "ok"
    assert _skipped(result) == 0


def test_zero_beside_the_amount_is_absent() -> None:
    result = _read(synth.zero_placeholder_columns())
    statement = _only(result)
    expected = synth.EXPECTED_ROWS["zero_placeholder_columns"]
    assert len(statement.transactions) == 6
    # Signs by column: a figure under ``Credits`` is money in, under ``Debits`` out.
    assert _rows(statement) == expected
    assert [t.amount > 0 for t in statement.transactions] == [
        True,
        False,
        True,
        False,
        True,
        False,
    ]
    assert _status(statement) == "ok"
    assert _skipped(result) == 0


# -- hand-built lines -----------------------------------------------------------


def _view(rows: list[list[tuple[str, float]]]) -> DocumentView:
    """One page of lines, each a list of ``(text, x0)`` cells, 14 points apart."""
    lines = []
    for index, cells in enumerate(rows):
        y = 100.0 + 14 * index
        built = tuple(Cell(text, x0, y, x0 + 5.0 * len(text), y + 9.0) for text, x0 in cells)
        lines.append(
            ViewLine(
                page=1, index=index, cells=built, text=" ".join(t for t, _ in cells), height=9.0
            )
        )
    return DocumentView(lines=tuple(lines), source_format="pdf", truncated=False, page_count=1)


def _parse(rows: list[list[tuple[str, float]]]) -> ParseOutcome:
    return TableStatementParser().parse(_view(rows), threshold=0.85)


HEADER = [("Date", 62.0), ("Description", 150.0), ("Amount", 500.0)]
TWO_SIDED = [
    ("Date", 62.0),
    ("Description", 150.0),
    ("Debit", 380.0),
    ("Credit", 450.0),
    ("Balance", 520.0),
]


def test_two_figures_in_debit_and_credit_are_still_skipped() -> None:
    outcome = _parse(
        [
            TWO_SIDED,
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 380.0), ("0.00", 450.0)],
            # Money out and money in on one line: nothing says which it is.
            [("01/07/2026", 62.0), ("Adjustment", 150.0), ("4.50", 380.0), ("3.00", 450.0)],
            [("01/09/2026", 62.0), ("Refund", 150.0), ("0.00", 380.0), ("9.25", 450.0)],
        ]
    )
    (statement,) = outcome.statements
    assert [(t.payee, t.amount) for t in statement.transactions] == [
        ("Coffee", Decimal("-4.50")),
        ("Refund", Decimal("9.25")),
    ]
    assert outcome.skipped_lines == 1
    assert len(outcome.skipped_regions) == 1


def test_two_zeros_in_debit_and_credit_are_still_skipped() -> None:
    outcome = _parse(
        [
            TWO_SIDED,
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 380.0), ("0.00", 450.0)],
            [("01/07/2026", 62.0), ("Courtesy notice", 150.0), ("0.00", 380.0), ("0.00", 450.0)],
        ]
    )
    (statement,) = outcome.statements
    assert [t.payee for t in statement.transactions] == ["Coffee"]
    assert outcome.skipped_lines == 1


def test_an_amount_right_of_its_heading_goes_to_the_column_that_starts_left_of_it() -> None:
    outcome = _parse(
        [
            TWO_SIDED,
            # 4.50 lies 40 points right of ``Debit`` and short of ``Credit``;
            # the balance as far right of ``Balance``.
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 420.0), ("95.50", 585.0)],
        ]
    )
    (statement,) = outcome.statements
    assert [(t.payee, t.amount) for t in statement.transactions] == [("Coffee", Decimal("-4.50"))]
    assert statement.ledger_balance is not None
    assert statement.ledger_balance.amount == Decimal("95.50")
    assert outcome.skipped_lines == 0


def test_an_amount_under_no_numeric_column_is_not_moved() -> None:
    outcome = _parse(
        [
            HEADER,
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 500.0)],
            # 12.00 is printed over the description column, far from every
            # heading: it is a figure in the text (a quantity, a rate), no amount.
            [("01/07/2026", 62.0), ("Tea", 150.0), ("12.00", 290.0)],
        ]
    )
    (statement,) = outcome.statements
    assert [(t.payee, t.amount) for t in statement.transactions] == [("Coffee", Decimal("4.50"))]
    assert outcome.skipped_lines == 1


def test_a_stray_figure_never_joins_a_column_that_has_its_amount() -> None:
    outcome = _parse(
        [
            HEADER,
            # 35.00 at the far right is no part of the row: the amount under
            # its heading is read as it always was.
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 500.0), ("35.00", 590.0)],
        ]
    )
    (statement,) = outcome.statements
    assert [(t.payee, t.amount) for t in statement.transactions] == [("Coffee", Decimal("4.50"))]
    assert outcome.skipped_lines == 0


def test_an_impossible_day_and_month_is_no_date() -> None:
    outcome = _parse(
        [
            [("Statement Period: 01/01/2026 - 31/01/2026", 62.0)],
            HEADER,
            [("28/01", 62.0), ("Tea", 150.0), ("3.00", 500.0)],
            # A rate table printed under the rows: 2.80 is no day and month.
            [("2.80", 62.0), ("Annual percentage yield", 150.0)],
        ]
    )
    (statement,) = outcome.statements
    assert [(t.posted, t.payee) for t in statement.transactions] == [(date(2026, 1, 28), "Tea")]
    assert outcome.skipped_lines == 0


def test_a_wrapped_line_that_starts_with_a_date_is_no_row() -> None:
    outcome = _parse(
        [
            HEADER,
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 500.0)],
            # The row's second line, printed from the date column: no figure
            # in a money column, so it stays the row's memo.
            [("01/04 purchase reference 5521", 62.0)],
        ]
    )
    (statement,) = outcome.statements
    assert len(statement.transactions) == 1
    assert statement.transactions[0].payee == "Coffee"
    assert outcome.skipped_lines == 0


def test_a_date_led_cell_away_from_the_date_column_is_no_row() -> None:
    outcome = _parse(
        [
            HEADER,
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 500.0)],
            # Starts under the description, 150 points right of the dates.
            [("01/31/2026 average daily balance", 215.0), ("812.44", 500.0)],
        ]
    )
    (statement,) = outcome.statements
    assert [t.payee for t in statement.transactions] == ["Coffee"]
    assert outcome.skipped_lines == 0

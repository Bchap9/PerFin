# SPDX-License-Identifier: AGPL-3.0-or-later
"""A line that prints two dates, or two amounts, where its table prints one.

Text laid over text (a page printed twice into one layer) gives lines like
``05/10/13 07/10/12 Overdraft fee -35.00``. The table reader used to take
the first date and drop the second as a posting date, and the row was as
sure as any other. A second date is a posting date only where the table
says so: a heading of its own, or every dated row of the section printing
one within a few days of its first. Anything else is a doubt on the row's
date (``confidence.INFERRED``), which holds its statement for a person.

Two amounts under one money heading read as no amount: the line is counted
as skipped, never a row. An amount beside a running balance, each under its
own heading, is read as before. A figure of a row line that no column of
the table takes, on a row that has its amount, is a second amount all the
same: the row's amount is a doubt.

A balance printed with a sign nothing settles (``Ending Balance - $40.00``
over rows without a running balance) is withheld, and the parse says so
(``balance_sign_unsettled``): the importer never takes such a statement for
one that printed no balance.

Hand-built pages; the one end-to-end case is in
``tests/integration/test_trust_policy.py``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.base import Cell, DocumentView, ParseOutcome, ViewLine
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.table_statement import TableStatementParser
from app.ingestion.sandbox.protocol import ExtractedTransaction, ExtractOptions
from tests.fixtures import synth_bank_layouts as layouts

HEADER = [("Date", 62.0), ("Description", 200.0), ("Amount", 500.0)]
TWO_DATES = [("Date", 62.0), ("Post Date", 130.0), ("Description", 200.0), ("Amount", 500.0)]
RUNNING = [("Date", 62.0), ("Description", 200.0), ("Amount", 430.0), ("Balance", 500.0)]


def _view(rows: list[list[tuple[str, float]]]) -> DocumentView:
    lines = []
    for index, cells in enumerate(rows):
        y = 100.0 + 14.0 * index
        built = tuple(Cell(text, x0, y, x0 + 5.0 * len(text), y + 9.0) for text, x0 in cells)
        lines.append(
            ViewLine(
                page=1, index=index, cells=built, text=" ".join(t for t, _ in cells), height=9.0
            )
        )
    return DocumentView(lines=tuple(lines), source_format="pdf", truncated=False, page_count=1)


def _parse(rows: list[list[tuple[str, float]]]) -> ParseOutcome:
    return TableStatementParser().parse(_view(rows), threshold=conf.DEFAULT_THRESHOLD)


def _rows(outcome: ParseOutcome) -> list[ExtractedTransaction]:
    (statement,) = outcome.statements
    return list(statement.transactions)


def _doubted(outcome: ParseOutcome) -> list[str | None]:
    """The payees of the rows whose date is a doubt."""
    return [t.payee for t in _rows(outcome) if "posted_date" in t.low_confidence]


def test_a_second_date_on_one_row_only_is_a_doubt() -> None:
    outcome = _parse(
        [
            HEADER,
            [("07/30/12", 62.0), ("Monthly fee", 200.0), ("0.00", 500.0)],
            [("05/10/13 07/10/12", 62.0), ("Overdraft fee", 200.0), ("-35.00", 500.0)],
        ]
    )
    assert [(t.posted, t.amount, t.payee) for t in _rows(outcome)] == [
        (date(2012, 7, 30), Decimal("0.00"), "Monthly fee"),
        (date(2013, 5, 10), Decimal("-35.00"), "Overdraft fee"),
    ]
    assert _doubted(outcome) == ["Overdraft fee"]
    doubled = _rows(outcome)[1]
    assert doubled.confidence is not None
    assert doubled.confidence <= conf.INFERRED


def test_the_second_date_in_a_cell_of_its_own_is_a_doubt_too() -> None:
    outcome = _parse(
        [
            HEADER,
            [("07/30/12", 62.0), ("Monthly fee", 200.0), ("0.00", 500.0)],
            [("05/10/13", 62.0), ("07/10/12", 85.0), ("Overdraft fee", 200.0), ("-35.00", 500.0)],
        ]
    )
    assert _doubted(outcome) == ["Overdraft fee"]


def test_a_posting_date_on_every_row_is_none() -> None:
    # One heading over two dates on every row, a day or two apart: the
    # layout prints a posting date, and the first date is the row's.
    outcome = _parse(
        [
            HEADER,
            [("03/03/2026 03/04/2026", 62.0), ("Pinewick Grocers", 200.0), ("-48.20", 500.0)],
            [("03/09/2026 03/09/2026", 62.0), ("Lantern Street Books", 200.0), ("-17.35", 500.0)],
            [("03/27/2026 03/30/2026", 62.0), ("Juniper Corner Cafe", 200.0), ("-29.99", 500.0)],
        ]
    )
    assert [t.posted for t in _rows(outcome)] == [
        date(2026, 3, 3),
        date(2026, 3, 9),
        date(2026, 3, 27),
    ]
    assert _doubted(outcome) == []


def test_a_second_date_months_from_the_first_is_no_posting_date() -> None:
    # Every row prints two dates, but one pair is ten months apart.
    outcome = _parse(
        [
            HEADER,
            [("03/03/2026 03/04/2026", 62.0), ("Pinewick Grocers", 200.0), ("-48.20", 500.0)],
            [("03/09/2026 05/09/2025", 62.0), ("Lantern Street Books", 200.0), ("-17.35", 500.0)],
            [("03/27/2026 03/30/2026", 62.0), ("Juniper Corner Cafe", 200.0), ("-29.99", 500.0)],
        ]
    )
    assert _doubted(outcome) == ["Lantern Street Books"]


def test_a_posting_date_column_the_header_names_is_none() -> None:
    outcome = _parse(
        [
            TWO_DATES,
            [("03/03/2026", 62.0), ("03/04/2026", 130.0), ("Pinewick", 200.0), ("-48.20", 500.0)],
            # A posting date long after: the header says what the column is.
            [("03/09/2026", 62.0), ("06/20/2026", 130.0), ("Lantern", 200.0), ("-17.35", 500.0)],
            # And a row without one.
            [("03/27/2026", 62.0), ("Juniper", 200.0), ("-29.99", 500.0)],
        ]
    )
    assert [t.posted for t in _rows(outcome)] == [
        date(2026, 3, 3),
        date(2026, 3, 9),
        date(2026, 3, 27),
    ]
    assert _doubted(outcome) == []


def test_two_amounts_under_one_heading_are_no_row() -> None:
    outcome = _parse(
        [
            HEADER,
            [("07/30/12", 62.0), ("Monthly fee", 200.0), ("0.00", 500.0)],
            [("05/10/13", 62.0), ("Overdraft fee", 200.0), ("-35.00 -12.00", 500.0)],
        ]
    )
    assert [t.payee for t in _rows(outcome)] == ["Monthly fee"]
    assert outcome.skipped_lines == 1
    # In two cells under the one heading as well.
    outcome = _parse(
        [
            HEADER,
            [("07/30/12", 62.0), ("Monthly fee", 200.0), ("0.00", 500.0)],
            [("05/10/13", 62.0), ("Overdraft fee", 200.0), ("-35.00", 480.0), ("-12.00", 515.0)],
        ]
    )
    assert [t.payee for t in _rows(outcome)] == ["Monthly fee"]
    assert outcome.skipped_lines == 1


def test_a_figure_no_column_takes_is_a_doubt_on_the_amount() -> None:
    # ``-35.00`` stands under no heading and near none; ``-12.00`` is under
    # ``Amount``. The row has an amount, and the line printed two.
    outcome = _parse(
        [
            HEADER,
            [("07/30/12", 62.0), ("Monthly fee", 200.0), ("0.00", 500.0)],
            [("05/10/13", 62.0), ("Overdraft fee", 200.0), ("-35.00", 440.0), ("-12.00", 505.0)],
        ]
    )
    rows = _rows(outcome)
    assert [(t.payee, "amount" in t.low_confidence) for t in rows] == [
        ("Monthly fee", False),
        ("Overdraft fee", True),
    ]
    assert rows[1].confidence is not None
    assert rows[1].confidence <= conf.INFERRED


def test_a_balance_whose_sign_nothing_settles_is_named() -> None:
    rows = [
        HEADER,
        [("03/14/2026", 62.0), ("Pinewick", 200.0), ("-48.20", 500.0)],
        [("03/18/2026", 62.0), ("Lantern", 200.0), ("-17.35", 500.0)],
    ]
    outcome = _parse([[("Ending Balance - $40.00", 62.0)], *rows])
    (statement,) = outcome.statements
    assert statement.ledger_balance is None
    assert statement.reconciliation is not None
    assert (statement.reconciliation.status, statement.reconciliation.closing) == (
        "unavailable",
        None,
    )
    assert "balance_sign_unsettled" in outcome.warnings
    # A sign that is printed as one is used, and nothing is said.
    outcome = _parse([[("Ending Balance -$40.00", 62.0)], *rows])
    (statement,) = outcome.statements
    assert statement.reconciliation is not None
    assert statement.reconciliation.closing == Decimal("-40.00")
    assert "balance_sign_unsettled" not in outcome.warnings


def test_an_amount_beside_its_running_balance_is_read_as_before() -> None:
    outcome = _parse(
        [
            RUNNING,
            [("03/14/2026", 62.0), ("Pinewick", 200.0), ("-48.20", 430.0), ("951.80", 500.0)],
            [("03/18/2026", 62.0), ("Lantern", 200.0), ("-17.35", 430.0), ("934.45", 500.0)],
        ]
    )
    assert [(t.amount, t.low_confidence) for t in _rows(outcome)] == [
        (Decimal("-48.20"), []),
        (Decimal("-17.35"), []),
    ]
    assert outcome.skipped_lines == 0


def test_the_dates_of_a_table_printed_across_the_page_are_their_own_rows() -> None:
    # Three copies of ``Check No. | Date | Amount`` side by side: the dates
    # right of the first copy are no second date of its rows.
    extracted = extract_pdf(
        layouts.checks_three_across(), ExtractOptions(previews=False, ocr=False), "pdf"
    )
    result = parse_extraction(extracted, kind="statement")
    (statement,) = result.statements
    assert [t.payee for t in statement.transactions if t.low_confidence] == []
    assert result.parse is not None
    assert not [w for w in result.parse.warnings if w.startswith("second_date_unexplained")]

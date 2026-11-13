# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bank statement rows, round two (``synth_bank_layouts2``).

A credit or debit marker printed in a column of its own (``Amount | DC``,
the cells ``CR``, ``DR`` or ``DB``) signs the amount it stands beside, as
the same marker printed after the figure does. Beside it the cells that
must sign nothing: a category that starts with ``Dr.``, a marker printed
left of the amount, and a marker with another figure between it and the
amount (the marker is that figure's).

Lines between two balance lines: a summary block printed under a header
(``Beginning Balance``, two count lines, ``Ending Balance``) gives no row,
while the undated rows under a dated ``Balance brought forward`` line still
take its date once a dated row follows them. A table that reads no row at
all counts its lines with a figure.

Cells that sit in two columns, or in the wrong one: an amount printed in one
cell with the description under ``Amount Description`` is split off it; of
two dates in the date column the first is the row's; a category word that
reaches under ``Amount`` is no part of the amount. Beside them what must
stay as it was: ``CR`` alone under an amount, a fused cell with no amount
word, a date with a word after it.

The amount printed alone on a line under its row (a card's purchase abroad:
the dated line, the foreign amount, the rate, then the amount) is the row's.
Beside it what must stay unread: a total under such a row, a figure four
lines down, a figure under a table with two money columns, and a figure
that follows another dated line.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.base import Cell, DocumentView, ParseOutcome, ViewLine
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.statement_header import Role
from app.ingestion.parsers.table_statement import TableStatementParser
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, Statement
from tests.fixtures import synth_bank_layouts2 as synth

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


def _view(rows: list[list[tuple[str, float]]], *, step: float = 14.0) -> DocumentView:
    """One page of lines, each a list of ``(text, x0)`` cells, ``step`` points apart."""
    lines = []
    for index, cells in enumerate(rows):
        y = 100.0 + step * index
        built = tuple(Cell(text, x0, y, x0 + 5.0 * len(text), y + 9.0) for text, x0 in cells)
        lines.append(
            ViewLine(
                page=1, index=index, cells=built, text=" ".join(t for t, _ in cells), height=9.0
            )
        )
    return DocumentView(lines=tuple(lines), source_format="pdf", truncated=False, page_count=1)


def _cell(text: str, x0: float) -> Cell:
    return Cell(text, x0, 100.0, x0 + 5.0 * len(text), 109.0)


def _parse(rows: list[list[tuple[str, float]]]) -> ParseOutcome:
    return TableStatementParser().parse(_view(rows), threshold=0.85)


def _amounts(outcome: ParseOutcome) -> list[tuple[str | None, Decimal]]:
    (statement,) = outcome.statements
    return [(t.payee, t.amount) for t in statement.transactions]


# -- a credit or debit marker in a column of its own ----------------------------


def test_marker_in_its_own_column_signs_the_amount() -> None:
    result = _read(synth.marker_column_checking())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["marker_column_checking"]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert statement.reconciliation.running_mismatches == []
    assert result.parse is not None
    assert result.parse.skipped_lines == 0


#: ``Amount`` and, right of it, a column no heading word names.
MARKED = [("Date", 62.0), ("Description", 150.0), ("Amount", 400.0), ("DC", 470.0)]


def test_a_marker_cell_signs_a_hand_built_row() -> None:
    outcome = _parse(
        [
            MARKED,
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 400.0), ("DB", 470.0)],
            [("01/06/2026", 62.0), ("Refund", 150.0), ("9.25", 400.0), ("CR", 470.0)],
            [("01/07/2026", 62.0), ("Bakery", 150.0), ("3.00", 400.0), ("Dr", 470.0)],
            # No marker: the amount is what it was.
            [("01/08/2026", 62.0), ("Tea", 150.0), ("2.00", 400.0)],
        ]
    )
    assert _amounts(outcome) == [
        ("Coffee", Decimal("-4.50")),
        ("Refund", Decimal("9.25")),
        ("Bakery", Decimal("-3.00")),
        ("Tea", Decimal("2.00")),
    ]
    assert outcome.skipped_lines == 0


def test_a_category_cell_is_no_marker() -> None:
    outcome = _parse(
        [
            [("Date", 62.0), ("Description", 150.0), ("Amount", 400.0), ("Category", 470.0)],
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 400.0), ("Dr. Office", 470.0)],
            [("01/06/2026", 62.0), ("Stamps", 150.0), ("-9.25", 400.0), ("Dr. Office", 470.0)],
            [("01/07/2026", 62.0), ("Dues", 150.0), ("3.00", 400.0), ("Credit union fee", 470.0)],
            [("01/08/2026", 62.0), ("Toner", 150.0), ("7.00", 400.0), ("DB Supplies", 470.0)],
        ]
    )
    # Each amount keeps the sign it was printed with.
    assert _amounts(outcome) == [
        ("Coffee", Decimal("4.50")),
        ("Stamps", Decimal("-9.25")),
        ("Dues", Decimal("3.00")),
        ("Toner", Decimal("7.00")),
    ]
    assert outcome.skipped_lines == 0


def test_a_marker_left_of_the_amount_is_not_taken() -> None:
    outcome = _parse(
        [
            [
                ("Date", 62.0),
                ("Description", 150.0),
                ("Balance", 330.0),
                ("DC", 400.0),
                ("Amount", 470.0),
            ],
            # ``DB`` stands left of the amount: whatever it marks, it is not
            # the figure printed after it.
            [
                ("01/05/2026", 62.0),
                ("Coffee", 150.0),
                ("95.50", 330.0),
                ("DB", 400.0),
                ("4.50", 470.0),
            ],
        ]
    )
    assert _amounts(outcome) == [("Coffee", Decimal("4.50"))]
    # And asked directly, whatever column the header reader made of ``DC``.
    parser = TableStatementParser()
    amount, left, right = _cell("4.50", 470.0), _cell("DB", 400.0), _cell("DB", 540.0)
    assert parser._own_column_marker({"amount": [amount], "other": [left]}) is None
    assert parser._own_column_marker({"amount": [amount], "other": [left, right]}) is False
    # A row with a debit or a credit column has its direction already.
    assert parser._own_column_marker({"debit": [amount], "other": [right]}) is None
    assert (
        parser._own_column_marker(
            {"amount": [amount], "credit": [_cell("0.00", 505.0)], "other": [right]}
        )
        is None
    )
    # So has an amount that prints its own marker.
    marked = _cell("4.50 CR", 470.0)
    assert parser._own_column_marker({"amount": [marked], "other": [right]}) is None


def test_a_marker_behind_another_figure_is_not_taken() -> None:
    outcome = _parse(
        [
            [("Date", 62.0), ("Description", 150.0), ("Amount", 400.0), ("DC", 520.0)],
            # No balance heading: 9,590.00 is in no column, and the marker
            # behind it is its marker, not the amount's.
            [
                ("01/05/2026", 62.0),
                ("Card purchase", 150.0),
                ("410.00", 400.0),
                ("9,590.00", 455.0),
                ("DB", 520.0),
            ],
        ]
    )
    assert _amounts(outcome) == [("Card purchase", Decimal("410.00"))]
    # The same with the figure in a column: a balance printed before the marker.
    parser = TableStatementParser()
    cells: dict[Role, list[Cell]] = {
        "amount": [_cell("410.00", 400.0)],
        "balance": [_cell("9,590.00", 455.0)],
        "other": [_cell("DB", 520.0)],
    }
    assert parser._own_column_marker(cells) is None
    del cells["balance"]
    assert parser._own_column_marker(cells) is False


# -- lines between two balance lines; a table that reads no row -----------------

HEADER = [("Date", 62.0), ("Description", 150.0), ("Amount", 500.0)]


def _warnings(result: ExtractResult) -> list[str]:
    assert result.parse is not None
    return list(result.parse.warnings)


def test_lines_between_two_balance_lines_are_not_rows() -> None:
    result = _read(synth.summary_grid_with_counts())
    statement = _only(result)
    assert len(statement.transactions) == 15
    assert not [
        t.payee for t in statement.transactions if synth.GRID_COUNT_WORDS in (t.payee or "")
    ]
    assert _rows(statement) == synth.EXPECTED_ROWS["summary_grid_with_counts"]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert "dates_carried" not in _warnings(result)
    assert result.parse is not None
    assert result.parse.skipped_lines == 0


def test_undated_rows_after_a_brought_forward_line_are_still_carried() -> None:
    result = _read(synth.brought_forward_dated_once())
    statement = _only(result)
    assert len(statement.transactions) == 9
    assert _rows(statement) == synth.EXPECTED_ROWS["brought_forward_dated_once"]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert _warnings(result).count("dates_carried") == 1
    assert result.parse is not None
    assert result.parse.skipped_lines == 0


def test_a_table_that_reads_no_row_counts_its_lines() -> None:
    outcome = _parse(
        [
            HEADER,
            [("Service charges", 150.0), ("12.00", 500.0)],
            [("Interest earned", 150.0), ("0.42", 500.0)],
            [("Overdraft items", 150.0), ("35.00", 500.0)],
        ]
    )
    assert [t for s in outcome.statements for t in s.transactions] == []
    assert outcome.skipped_lines == 3
    assert outcome.skipped_regions == [
        (1, (150.0, 114.0, 525.0, 123.0)),
        (1, (150.0, 128.0, 520.0, 137.0)),
        (1, (150.0, 142.0, 525.0, 151.0)),
    ]


def test_a_table_that_reads_no_row_counts_its_lines_at_the_next_header() -> None:
    outcome = _parse(
        [
            HEADER,
            [("Service charges", 150.0), ("12.00", 500.0)],
            [("Interest earned", 150.0), ("0.42", 500.0)],
            HEADER,
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("-4.50", 500.0)],
            # Under a table with a row, a line nothing dated follows is what
            # it was: a line of the summary, not counted.
            [("Average balance", 150.0), ("95.50", 500.0)],
        ]
    )
    assert _amounts(outcome) == [("Coffee", Decimal("-4.50"))]
    assert outcome.skipped_lines == 2


def test_candidates_under_a_balance_line_wait_for_a_dated_row() -> None:
    carried = [
        [("01/05/2026", 62.0), ("Coffee", 150.0), ("-4.50", 500.0)],
        [("Bakery", 150.0), ("-3.00", 500.0)],
        [("Tea", 150.0), ("-2.00", 500.0)],
        [("01/09/2026", 62.0), ("Refund", 150.0), ("9.25", 500.0)],
    ]
    block = [
        [("01/31/2026", 62.0), ("Beginning Balance", 150.0), ("100.00", 500.0)],
        [("2 Credits This Period", 150.0), ("9.25", 500.0)],
        [("3 Debits This Period", 150.0), ("9.50", 500.0)],
        [("01/31/2026", 62.0), ("Ending Balance", 150.0), ("99.75", 500.0)],
    ]
    # Two rows of the statement took the date above them already: the lines
    # between the two balance lines are still no rows.
    outcome = _parse([HEADER, *carried, *block])
    assert [payee for payee, _ in _amounts(outcome)] == ["Coffee", "Bakery", "Tea", "Refund"]
    assert outcome.skipped_lines == 0
    # Candidates that start at a transaction row are rows when a balance
    # line follows them, as when a dated row does.
    outcome = _parse(
        [
            HEADER,
            [("01/02/2026", 62.0), ("Balance brought forward", 150.0), ("100.00", 500.0)],
            [("Stamps", 150.0), ("-1.00", 500.0)],
            *carried,
            [("Toner", 150.0), ("-7.00", 500.0)],
            [("01/31/2026", 62.0), ("Balance carried forward", 150.0), ("91.75", 500.0)],
        ]
    )
    assert _amounts(outcome) == [
        ("Stamps", Decimal("-1.00")),
        ("Coffee", Decimal("-4.50")),
        ("Bakery", Decimal("-3.00")),
        ("Tea", Decimal("-2.00")),
        ("Refund", Decimal("9.25")),
        ("Toner", Decimal("-7.00")),
    ]
    assert outcome.skipped_lines == 0


# -- cells that sit in two columns, or in the wrong one -------------------------


def test_an_amount_fused_with_the_description_is_split() -> None:
    result = _read(synth.amount_before_description_fused())
    statement = _only(result)
    assert len(statement.transactions) == 7
    assert _rows(statement) == synth.EXPECTED_ROWS["amount_before_description_fused"]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert result.parse is not None
    assert result.parse.skipped_lines == 0


def test_two_dates_under_one_heading_cell() -> None:
    result = _read(synth.two_dates_left_aligned())
    statement = _only(result)
    assert len(statement.transactions) == 7
    # Each row is dated with the first of its two dates.
    assert _rows(statement) == synth.EXPECTED_ROWS["two_dates_left_aligned"]
    for transaction in statement.transactions:
        text = f"{transaction.payee} {transaction.memo or ''}"
        assert not [word for word in synth.CATEGORY_WORDS if word in text]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert result.parse is not None
    assert result.parse.skipped_lines == 0


#: ``Transaction Amount`` spans 380 to 470, ``Category`` 480 to 520.
CATEGORISED = [
    ("Date", 62.0),
    ("Description", 150.0),
    ("Transaction Amount", 380.0),
    ("Category", 480.0),
]


def test_a_worded_cell_is_not_put_in_a_money_column() -> None:
    outcome = _parse(
        [
            CATEGORISED,
            # ``Restaurants`` spans 440 to 495: 30 points under the amount
            # heading, 15 under its own.
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 380.0), ("Restaurants", 440.0)],
            [("01/06/2026", 62.0), ("Refund", 150.0), ("-9.25", 380.0), ("Restaurants", 440.0)],
            # Under its own heading, as before.
            [("01/07/2026", 62.0), ("Bakery", 150.0), ("3.00", 380.0), ("Groceries", 480.0)],
        ]
    )
    assert _amounts(outcome) == [
        ("Coffee", Decimal("4.50")),
        ("Refund", Decimal("-9.25")),
        ("Bakery", Decimal("3.00")),
    ]
    assert outcome.skipped_lines == 0
    parser = TableStatementParser()
    view = _view(
        [
            CATEGORISED,
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 380.0), ("Restaurants", 440.0)],
            # Words under the amount heading and under no other: they stay
            # where they are printed, and the line reads no amount.
            [("01/08/2026", 62.0), ("Tea", 150.0), ("2.00", 380.0), ("see note", 420.0)],
            # A figure is no worded cell, wherever it reaches.
            [("01/09/2026", 62.0), ("Toner", 150.0), ("7.00", 440.0)],
        ]
    )
    columns = parser.columns(view.lines[0])
    assert columns is not None
    texts = [
        {role: [c.text for c in found] for role, found in parser._assign(line, columns).items()}
        for line in view.lines[1:]
    ]
    assert texts == [
        {
            "date": ["01/05/2026"],
            "description": ["Coffee"],
            "amount": ["4.50"],
            "other": ["Restaurants"],
        },
        {"date": ["01/08/2026"], "description": ["Tea"], "amount": ["2.00", "see note"]},
        {"date": ["01/09/2026"], "description": ["Toner"], "amount": ["7.00"]},
    ]


def test_a_marker_cell_still_reaches_the_amount() -> None:
    outcome = _parse(
        [
            HEADER,
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 500.0)],
            # ``CR`` alone under the amount marks the row above it.
            [("CR", 500.0)],
            [("01/06/2026", 62.0), ("Bakery", 150.0), ("3.00", 500.0)],
            [("DR", 500.0)],
        ]
    )
    assert _amounts(outcome) == [("Coffee", Decimal("4.50")), ("Bakery", Decimal("-3.00"))]
    assert outcome.skipped_lines == 0
    # A marker that reaches under another heading as well stays the amount's.
    parser = TableStatementParser()
    view = _view(
        [
            CATEGORISED,
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 380.0), ("CR", 465.0)],
            [("01/06/2026", 62.0), ("Bakery", 150.0), ("3.00", 380.0), ("DB", 465.0)],
        ]
    )
    columns = parser.columns(view.lines[0])
    assert columns is not None
    for line, marker in zip(view.lines[1:], ("CR", "DB"), strict=True):
        assert [c.text for c in parser._assign(line, columns)["amount"]] == [
            line.cells[2].text,
            marker,
        ]
    outcome = parser.parse(view, threshold=0.85)
    assert _amounts(outcome) == [("Coffee", Decimal("4.50")), ("Bakery", Decimal("-3.00"))]


#: ``Amount`` spans 120 to 150 and ``Description`` 155 to 210: one cell of
#: two headings, as :func:`synth.amount_before_description_fused` prints them.
AMOUNT_FIRST = [("Date", 62.0), ("Amount Description", 120.0)]
#: The same two columns the other way round (150 to 205 and 210 to 240).
AMOUNT_LAST = [("Date", 62.0), ("Description Amount", 150.0)]


def test_a_fused_cell_is_split_at_its_amount_word() -> None:
    outcome = _parse(
        [
            AMOUNT_FIRST,
            [("01/05/2026", 62.0), ("$4.50 Coffee house", 120.0)],
            [("01/06/2026", 62.0), ("-$9.25 Stamps", 120.0)],
        ]
    )
    assert _amounts(outcome) == [
        ("Coffee house", Decimal("4.50")),
        ("Stamps", Decimal("-9.25")),
    ]
    assert outcome.skipped_lines == 0
    # The money column right of the description: the last word.
    outcome = _parse([AMOUNT_LAST, [("01/05/2026", 62.0), ("Coffee house 4.50", 150.0)]])
    assert _amounts(outcome) == [("Coffee house", Decimal("4.50"))]


def test_a_fused_cell_without_an_amount_word_is_left_alone() -> None:
    outcome = _parse(
        [
            AMOUNT_FIRST,
            [("01/05/2026", 62.0), ("$4.50 Coffee house", 120.0)],
            # No amount where the amount is printed: the word at the other
            # end is none either, whatever it reads as.
            [("01/06/2026", 62.0), ("Stamps 9.25", 120.0)],
            [("01/07/2026", 62.0), ("Bakery order 7719", 120.0)],
            # An undated line is a row's second line, never a row.
            [("12.00 off next order", 120.0)],
        ]
    )
    assert _amounts(outcome) == [("Coffee house", Decimal("4.50"))]
    assert outcome.skipped_lines == 2
    (statement,) = outcome.statements
    assert "12.00" not in {str(abs(t.amount)) for t in statement.transactions}
    # A table with two money columns says nothing of which one a word is for.
    outcome = _parse(
        [
            [("Date", 62.0), ("Description Debit", 150.0), ("Credit", 300.0)],
            [("01/05/2026", 62.0), ("Coffee house 4.50", 150.0)],
        ]
    )
    assert [t for s in outcome.statements for t in s.transactions] == []
    assert outcome.skipped_lines == 1
    # And a line whose amount is read where it stands keeps its words.
    outcome = _parse(
        [
            [("Date", 62.0), ("Description", 150.0), ("Amount", 215.0)],
            [("01/05/2026", 62.0), ("Platform 9.75 cafe", 150.0), ("4.50", 260.0)],
        ]
    )
    assert _amounts(outcome) == [("Platform 9.75 cafe", Decimal("4.50"))]


def test_two_dates_in_the_date_column_are_the_date_and_a_posting_date() -> None:
    wide = [("Transaction Date", 62.0), ("Description", 200.0), ("Amount", 500.0)]
    outcome = _parse(
        [
            wide,
            # Two cells under the one heading.
            [("01/05/2026", 62.0), ("01/06/2026", 120.0), ("Coffee", 200.0), ("-4.50", 500.0)],
            # One cell of two halves.
            [("01/07/2026 01/09/2026", 62.0), ("Bakery", 200.0), ("-3.00", 500.0)],
        ]
    )
    (statement,) = outcome.statements
    assert [(t.posted, t.payee, t.amount) for t in statement.transactions] == [
        (date(2026, 1, 5), "Coffee", Decimal("-4.50")),
        (date(2026, 1, 7), "Bakery", Decimal("-3.00")),
    ]
    assert outcome.skipped_lines == 0


def test_a_date_and_a_word_in_the_date_column_are_no_two_dates() -> None:
    wide = [("Transaction Date", 62.0), ("Description", 200.0), ("Amount", 500.0)]
    outcome = _parse(
        [
            wide,
            [("01/05/2026", 62.0), ("Coffee", 200.0), ("-4.50", 500.0)],
            # A date and a word, a word and a date, three dates: no row is
            # dated by guessing which of them is its date.
            [("01/06/2026", 62.0), ("Hold", 120.0), ("Bakery", 200.0), ("-3.00", 500.0)],
            [("Est.", 62.0), ("01/07/2026", 90.0), ("Tea", 200.0), ("-2.00", 500.0)],
            [
                ("01/08/2026 01/09/2026", 62.0),
                ("01/10", 175.0),
                ("Toner", 200.0),
                ("-7.00", 500.0),
            ],
            [("01/12/2026", 62.0), ("Refund", 200.0), ("9.25", 500.0)],
        ]
    )
    (statement,) = outcome.statements
    assert [(t.posted, t.payee, t.amount) for t in statement.transactions] == [
        (date(2026, 1, 5), "Coffee", Decimal("-4.50")),
        (date(2026, 1, 12), "Refund", Decimal("9.25")),
    ]
    # Found and not imported: each of the three is counted.
    assert outcome.skipped_lines == 3


# -- cents printed without a zero in a money column -----------------------------


def test_cents_without_a_zero_are_read_in_a_money_column() -> None:
    outcome = _parse(
        [
            [("Date", 62.0), ("Description", 150.0), ("Amount", 400.0), ("Balance", 500.0)],
            [("01/05/2026", 62.0), ("Interest", 150.0), (".15", 400.0), ("100.15", 500.0)],
            [("01/06/2026", 62.0), ("Service fee", 150.0), ("-.30", 400.0), ("99.85", 500.0)],
            [("01/07/2026", 62.0), ("Rebate", 150.0), ("$.45", 400.0), ("100.30", 500.0)],
        ]
    )
    assert _amounts(outcome) == [
        ("Interest", Decimal("0.15")),
        ("Service fee", Decimal("-0.30")),
        ("Rebate", Decimal("0.45")),
    ]
    assert outcome.skipped_lines == 0
    (statement,) = outcome.statements
    assert statement.reconciliation is not None
    assert statement.reconciliation.running_mismatches == []


def test_a_figure_that_is_no_cents_is_still_no_amount() -> None:
    outcome = _parse(
        [
            [("Date", 62.0), ("Description", 150.0), ("Amount", 400.0)],
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 400.0)],
            # One digit, three digits, a dot: none is cents, and the line is
            # counted as it was.
            [("01/06/2026", 62.0), ("Rate", 150.0), (".5", 400.0)],
            [("01/07/2026", 62.0), ("Ratio", 150.0), (".125", 400.0)],
            [("01/08/2026", 62.0), ("Dots", 150.0), (".", 400.0)],
            # Under the description heading ``.15`` is text, not money.
            [("01/09/2026", 62.0), ("Build .15", 150.0)],
        ]
    )
    assert _amounts(outcome) == [("Coffee", Decimal("4.50"))]
    assert outcome.skipped_lines == 4


# -- the amount printed on a line of its own under its row ----------------------


def _transactions(outcome: ParseOutcome) -> list[tuple[str | None, Decimal]]:
    return [(t.payee, t.amount) for s in outcome.statements for t in s.transactions]


def test_amount_on_a_line_under_its_row() -> None:
    result = _read(synth.amount_under_row_card())
    statement = _only(result)
    assert len(statement.transactions) == 6
    assert _rows(statement) == synth.EXPECTED_ROWS["amount_under_row_card"]
    by_payee = {t.payee: t for t in statement.transactions}
    assert len(synth.AMOUNT_UNDER_ROW) == 2
    for payee, amount, under in synth.AMOUNT_UNDER_ROW:
        assert by_payee[payee].amount == amount
        memo = by_payee[payee].memo or ""
        assert "Exchange Rate" in memo
        for text in under:
            assert text in memo
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert result.parse is not None
    assert result.parse.skipped_lines == 0


def test_an_amount_alone_under_a_hand_built_row_is_its_amount() -> None:
    outcome = _parse(
        [
            HEADER,
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("-4.50", 500.0)],
            [("01/06/2026", 62.0), ("Bakery abroad", 150.0)],
            [("63.00 CAD", 150.0)],
            [("1.28 Exchange Rate", 150.0)],
            [("-49.22", 500.0)],
            # The amount right under the dated line.
            [("01/07/2026", 62.0), ("Tea", 150.0)],
            [("-2.00", 500.0)],
            [("01/08/2026", 62.0), ("Refund", 150.0), ("9.25", 500.0)],
        ]
    )
    assert _amounts(outcome) == [
        ("Coffee", Decimal("-4.50")),
        ("Bakery abroad", Decimal("-49.22")),
        ("Tea", Decimal("-2.00")),
        ("Refund", Decimal("9.25")),
    ]
    assert outcome.skipped_lines == 0
    (statement,) = outcome.statements
    assert [t.memo for t in statement.transactions] == [
        None,
        "63.00 CAD 1.28 Exchange Rate",
        None,
        None,
    ]


def test_a_total_under_a_row_without_amount_is_not_its_amount() -> None:
    outcome = _parse(
        [
            HEADER,
            [("01/05/2026", 62.0), ("Coffee", 150.0)],
            [("Total", 150.0), ("55.00", 500.0)],
        ]
    )
    assert _transactions(outcome) == []
    assert outcome.skipped_lines == 1
    # Nor is a figure alone after a total, a balance label or a title.
    for between in (
        [("Total purchases", 150.0)],
        [("Ending Balance", 150.0)],
        [("Fees", 62.0)],
        [("Pending", 150.0)],
    ):
        outcome = _parse(
            [
                HEADER,
                [("01/04/2026", 62.0), ("Stamps", 150.0), ("-1.00", 500.0)],
                [("01/05/2026", 62.0), ("Coffee", 150.0)],
                between,
                [("55.00", 500.0)],
            ]
        )
        assert _transactions(outcome) == [("Stamps", Decimal("-1.00"))], between
        assert outcome.skipped_lines == 1, between
    # A figure alone under ``Balance`` is a balance, not the row's amount.
    outcome = _parse(
        [
            [("Date", 62.0), ("Description", 150.0), ("Amount", 400.0), ("Balance", 500.0)],
            [("01/04/2026", 62.0), ("Stamps", 150.0), ("-1.00", 400.0), ("99.00", 500.0)],
            [("01/05/2026", 62.0), ("Coffee", 150.0)],
            [("55.00", 500.0)],
        ]
    )
    assert _transactions(outcome) == [("Stamps", Decimal("-1.00"))]
    assert outcome.skipped_lines == 1
    # A figure alone over a balance label printed alone is that balance.
    outcome = _parse(
        [
            HEADER,
            [("01/04/2026", 62.0), ("Stamps", 150.0), ("-1.00", 500.0)],
            [("01/05/2026", 62.0), ("Coffee", 150.0)],
            [("55.00", 500.0)],
            [("Ending Balance", 150.0)],
        ]
    )
    assert _transactions(outcome) == [("Stamps", Decimal("-1.00"))]
    assert outcome.skipped_lines == 1
    # With the label's own figure under it, the one above is the row's.
    outcome = _parse(
        [
            HEADER,
            [("01/04/2026", 62.0), ("Stamps", 150.0), ("-1.00", 500.0)],
            [("01/05/2026", 62.0), ("Coffee", 150.0)],
            [("-5.00", 500.0)],
            [("Ending Balance", 150.0)],
            [("94.00", 500.0)],
        ]
    )
    assert _transactions(outcome) == [("Stamps", Decimal("-1.00")), ("Coffee", Decimal("-5.00"))]
    assert outcome.skipped_lines == 0
    # A line with words and a figure of its own under ``Amount`` is no
    # second line of the row above: nothing is taken from under it.
    outcome = _parse(
        [
            HEADER,
            [("01/04/2026", 62.0), ("Stamps", 150.0), ("-1.00", 500.0)],
            [("01/05/2026", 62.0), ("Coffee", 150.0)],
            [("Service charge", 150.0), ("12.00", 500.0)],
            [("55.00", 500.0)],
        ]
    )
    assert ("Coffee", Decimal("55.00")) not in _transactions(outcome)
    assert Decimal("55.00") not in {amount for _, amount in _transactions(outcome)}


def test_a_figure_four_lines_down_is_not_taken() -> None:
    def table(notes: int) -> list[list[tuple[str, float]]]:
        return [
            HEADER,
            [("01/04/2026", 62.0), ("Stamps", 150.0), ("-1.00", 500.0)],
            [("01/05/2026", 62.0), ("Coffee", 150.0)],
            *([("see the note", 150.0)] for _ in range(notes)),
            [("55.00", 500.0)],
        ]

    outcome = _parse(table(3))
    assert _transactions(outcome) == [("Stamps", Decimal("-1.00"))]
    assert outcome.skipped_lines == 1
    # On the third line it is the row's.
    outcome = _parse(table(2))
    assert _transactions(outcome) == [("Stamps", Decimal("-1.00")), ("Coffee", Decimal("55.00"))]
    assert outcome.skipped_lines == 0
    # Not from far below the row, however few lines stand between them.
    view = _view([*table(0)[:3], [("55.00", 500.0)]], step=40.0)
    outcome = TableStatementParser().parse(view, threshold=0.85)
    assert _transactions(outcome) == [("Stamps", Decimal("-1.00"))]
    assert outcome.skipped_lines == 1


def test_two_money_columns_take_no_amount_from_below() -> None:
    outcome = _parse(
        [
            [("Date", 62.0), ("Description", 150.0), ("Debit", 400.0), ("Credit", 500.0)],
            [("01/04/2026", 62.0), ("Stamps", 150.0), ("1.00", 400.0)],
            [("01/05/2026", 62.0), ("Coffee", 150.0)],
            [("4.50", 400.0)],
        ]
    )
    assert _transactions(outcome) == [("Stamps", Decimal("-1.00"))]
    assert outcome.skipped_lines == 1


def test_a_dated_line_ends_the_wait() -> None:
    outcome = _parse(
        [
            HEADER,
            [("01/05/2026", 62.0), ("Coffee", 150.0)],
            [("house blend", 150.0)],
            [("01/06/2026", 62.0), ("Bakery", 150.0), ("-3.00", 500.0)],
            [("-4.50", 500.0)],
        ]
    )
    # The figure under the second row is neither row's amount.
    assert _transactions(outcome) == [("Bakery", Decimal("-3.00"))]
    assert outcome.skipped_lines == 1
    # Two rows without a figure: the amount is the second one's only.
    outcome = _parse(
        [
            HEADER,
            [("01/05/2026", 62.0), ("Coffee", 150.0)],
            [("01/06/2026", 62.0), ("Bakery", 150.0)],
            [("-3.00", 500.0)],
        ]
    )
    assert _transactions(outcome) == [("Bakery", Decimal("-3.00"))]
    assert outcome.skipped_lines == 1

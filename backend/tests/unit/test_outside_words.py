# SPDX-License-Identifier: AGPL-3.0-or-later
"""Words printed beside or over a row that are no part of it (``synth_outside_words``).

A notice printed up the margin of the page puts a word left of some rows; a
line as tall as such a word takes it for a cell of the date column, and the
row's date then reads as no date. The line is read once more without the
words of the margin, and is a row when its date column then holds a date.
The same for a watermark: a word whose box lies over the other words of its
cell is taken out and those words go to their own columns.

What must stay unread, and counted: a word printed left of rows only (it
may say something about them: ``Hold``), a word close to the date, a word
inside the date column, and a cell in which no word lies over the others.
And no row is ever given an amount it does not print: a dated line without
a figure in a money column is counted, not imported as ``0.00``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.base import Cell, DocumentView, ParseOutcome, ViewLine
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.table_statement import MARGIN_GAP, TableStatementParser
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, Statement
from tests.fixtures import synth_outside_words as synth

OPTIONS = ExtractOptions(previews=False, ocr=False)

#: A word and its box: ``(text, x0, x1)``.
Word = tuple[str, float, float]
#: A cell: ``(text, x0)``, five points to the letter, or its words with their boxes.
Spec = tuple[str, float] | list[Word]
Page = list[list[Spec]]

#: A word of the margin: its box, the same for every word printed up the page.
EDGE = (8.0, 15.0)


def _read(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return parse_extraction(result, kind="statement")


def _only(result: ExtractResult) -> Statement:
    assert len(result.statements) == 1, result.statements
    return result.statements[0]


def _rows(statement: Statement) -> list[tuple[date, Decimal, str | None]]:
    return [(t.posted, t.amount, t.payee) for t in statement.transactions]


def _cell(spec: Spec, y: float) -> Cell:
    if isinstance(spec, tuple):
        text, x0 = spec
        return Cell(text, x0, y, x0 + 5.0 * len(text), y + 9.0)
    return Cell(
        " ".join(text for text, _, _ in spec),
        min(x0 for _, x0, _ in spec),
        y,
        max(x1 for _, _, x1 in spec),
        y + 9.0,
        None,
        tuple(spec),
    )


def _parse(*pages: Page) -> ParseOutcome:
    """Lines 14 points apart, one list of them per page, each a list of cells.

    A line beside a word printed up the margin is as tall as that word."""
    lines: list[ViewLine] = []
    for number, rows in enumerate(pages, start=1):
        for row, specs in enumerate(rows):
            cells = tuple(_cell(spec, 100.0 + 14.0 * row) for spec in specs)
            tall = any(cell.x1 <= EDGE[1] for cell in cells) and len(cells) > 1
            lines.append(
                ViewLine(
                    page=number,
                    index=len(lines),
                    cells=cells,
                    text=" ".join(c.text for c in cells),
                    height=24.0 if tall else 9.0,
                )
            )
    view = DocumentView(
        lines=tuple(lines), source_format="pdf", truncated=False, page_count=len(pages)
    )
    return TableStatementParser().parse(view, threshold=0.85)


def _read_rows(outcome: ParseOutcome) -> list[tuple[date, str | None, Decimal]]:
    (statement,) = outcome.statements
    return [(t.posted, t.payee, t.amount) for t in statement.transactions]


HEADER: list[Spec] = [("Date", 62.0), ("Description", 200.0), ("Amount", 500.0)]


def _edge(text: str) -> Spec:
    return [(text, *EDGE)]


# -- a notice printed up the margin ---------------------------------------------------


def test_a_word_of_the_margin_beside_a_row_does_not_lose_it() -> None:
    result = _read(synth.notice_up_the_margin())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["notice_up_the_margin"]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert statement.reconciliation.running_mismatches == []
    assert result.parse is not None
    assert result.parse.skipped_lines == 0
    # Each row has the date printed on its own line: none is carried.
    assert "dates_carried" not in result.parse.warnings
    # No word of the notice is part of a payee or a memo.
    words = " ".join(f"{t.payee} {t.memo or ''}" for t in statement.transactions).split()
    assert "licensed" not in words
    assert "Limited" not in words


def test_a_word_left_of_rows_only_keeps_them_unread() -> None:
    result = _read(synth.flag_left_of_rows())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["flag_left_of_rows"]
    assert result.parse is not None
    # Found and not imported: counted.
    assert result.parse.skipped_lines == len(synth.FLAGGED_ROWS)
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "mismatch"


def test_margin_words_hand_built() -> None:
    outcome = _parse(
        [
            HEADER,
            [("01/05/2026", 62.0), ("Coffee", 200.0), ("-4.50", 500.0)],
            [_edge("licensed"), ("01/06/2026", 62.0), ("Bakery", 200.0), ("-3.00", 500.0)],
            [_edge("by")],
            [("01/08/2026", 62.0), ("Tea", 200.0), ("-2.00", 500.0)],
        ]
    )
    assert _read_rows(outcome) == [
        (date(2026, 1, 5), "Coffee", Decimal("-4.50")),
        (date(2026, 1, 6), "Bakery", Decimal("-3.00")),
        (date(2026, 1, 8), "Tea", Decimal("-2.00")),
    ]
    assert outcome.skipped_lines == 0


def test_a_word_beside_rows_only_is_no_margin_word() -> None:
    outcome = _parse(
        [
            HEADER,
            [("01/05/2026", 62.0), ("Coffee", 200.0), ("-4.50", 500.0)],
            [_edge("Hold"), ("01/06/2026", 62.0), ("Bakery", 200.0), ("-3.00", 500.0)],
            [_edge("Hold"), ("01/07/2026", 62.0), ("Toner", 200.0), ("-7.00", 500.0)],
            [("01/08/2026", 62.0), ("Tea", 200.0), ("-2.00", 500.0)],
        ]
    )
    assert _read_rows(outcome) == [
        (date(2026, 1, 5), "Coffee", Decimal("-4.50")),
        (date(2026, 1, 8), "Tea", Decimal("-2.00")),
    ]
    assert outcome.skipped_lines == 2


def test_a_word_close_to_the_date_is_no_margin_word() -> None:
    # It ends less than MARGIN_GAP left of the table, and stands beside a
    # line without a date as well: still no word of the margin.
    end = 62.0 - MARGIN_GAP + 7.0
    close: Spec = [("Est.", end - 20.0, end)]
    outcome = _parse(
        [
            HEADER,
            [("01/05/2026", 62.0), ("Coffee", 200.0), ("-4.50", 500.0)],
            [close, ("01/06/2026", 62.0), ("Bakery", 200.0), ("-3.00", 500.0)],
            [close],
            [("01/08/2026", 62.0), ("Tea", 200.0), ("-2.00", 500.0)],
        ]
    )
    assert _read_rows(outcome) == [
        (date(2026, 1, 5), "Coffee", Decimal("-4.50")),
        (date(2026, 1, 8), "Tea", Decimal("-2.00")),
    ]
    assert outcome.skipped_lines == 1


# -- a watermark over the dates ---------------------------------------------------------


def test_a_watermark_over_the_dates_does_not_lose_the_row() -> None:
    result = _read(synth.watermark_over_dates())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["watermark_over_dates"]
    # The row under the watermark has its own date, not the one above it.
    assert synth.WATERMARKED_ROW in _rows(statement)
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert result.parse is not None
    assert result.parse.skipped_lines == 0
    assert "dates_carried" not in result.parse.warnings
    assert all(synth.WATERMARK_WORD not in (t.payee or "") for t in statement.transactions)


def test_a_word_printed_once_over_the_dates_keeps_the_row_unread() -> None:
    result = _read(synth.stamp_over_dates())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["stamp_over_dates"]
    assert result.parse is not None
    assert result.parse.skipped_lines == 1


#: A word printed over the two words after it, and the page of a note it is printed on again.
OVER: Word = ("SPECIMEN", 55.0, 195.0)
NOTE_PAGE: Page = [[("Notes", 62.0)], [[OVER]]]


def test_watermark_hand_built() -> None:
    page: Page = [
        HEADER,
        [("01/05/2026", 62.0), ("Coffee", 200.0), ("-4.50", 500.0)],
        [[OVER, ("01/06/2026", 62.0, 112.0)], ("Bakery", 200.0), ("-3.00", 500.0)],
        [("01/08/2026", 62.0), ("Tea", 200.0), ("-2.00", 500.0)],
    ]
    read = [
        (date(2026, 1, 5), "Coffee", Decimal("-4.50")),
        (date(2026, 1, 6), "Bakery", Decimal("-3.00")),
        (date(2026, 1, 8), "Tea", Decimal("-2.00")),
    ]
    outcome = _parse(page, NOTE_PAGE)
    assert _read_rows(outcome) == read
    assert outcome.skipped_lines == 0
    # Printed on this page only: a stamp on the row, for all we know.
    outcome = _parse(page)
    assert _read_rows(outcome) == [read[0], read[2]]
    assert outcome.skipped_lines == 1


def test_a_cell_without_a_word_over_the_others_is_left_alone() -> None:
    outcome = _parse(
        [
            HEADER,
            [("01/05/2026", 62.0), ("Coffee", 200.0), ("-4.50", 500.0)],
            # Three words side by side: none lies over the other two, though
            # the first is printed at the same place on the next page.
            [
                [("Est.", 62.0, 80.0), ("01/06/2026", 84.0, 134.0), ("01/07/2026", 138.0, 188.0)],
                ("Bakery", 200.0),
                ("-3.00", 500.0),
            ],
            # A word over the date and not over the word after it.
            [
                [OVER, ("01/07/2026", 62.0, 112.0), ("Hold", 180.0, 200.0)],
                ("Toner", 240.0),
                ("-7.00", 500.0),
            ],
            [("01/08/2026", 62.0), ("Tea", 200.0), ("-2.00", 500.0)],
        ],
        [*NOTE_PAGE, [[("Est.", 62.0, 80.0)]]],
    )
    assert _read_rows(outcome) == [
        (date(2026, 1, 5), "Coffee", Decimal("-4.50")),
        (date(2026, 1, 8), "Tea", Decimal("-2.00")),
    ]
    assert outcome.skipped_lines == 2


# -- no amount, no row ------------------------------------------------------------------


def test_a_row_without_its_amount_is_counted_never_zero() -> None:
    outcome = _parse(
        [
            [("Date", 62.0), ("Description", 150.0), ("Amount", 400.0), ("Balance", 500.0)],
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("-4.50", 400.0), ("95.50", 500.0)],
            # The amount cell is missing: dated, and with a balance only.
            [("01/06/2026", 62.0), ("Bakery", 150.0), ("92.50", 500.0)],
            # Undated, with words and no figure under ``Amount``.
            [("Card ending 0000", 150.0), ("92.50", 500.0)],
            [("01/08/2026", 62.0), ("Tea", 150.0), ("-2.00", 400.0), ("90.50", 500.0)],
        ]
    )
    (statement,) = outcome.statements
    assert [(t.payee, t.amount) for t in statement.transactions] == [
        ("Coffee", Decimal("-4.50")),
        ("Tea", Decimal("-2.00")),
    ]
    assert all(t.amount != 0 for t in statement.transactions)
    assert outcome.skipped_lines >= 1

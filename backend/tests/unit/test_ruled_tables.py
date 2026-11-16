# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tables printed with a vertical rule as text (``synth_ruled_tables``).

A statement set line-printer style prints ``|`` between the two halves of
a table printed across the page: on the header line, on a line of its own
and after each row's amount. The bar is no heading, no description and no
figure. With it in the line the header was no header (a cell that is no
heading), the row beside it ended in a cell that is no amount, and the row
was neither read nor counted. The reader drops such cells first
(:func:`~app.ingestion.parsers.statement_lines.without_rules`).

And where a header is not one we read, met outside any table, the lines
under it that start with a date, hold words and end with an amount are
counted, as they are after a header that ended a table of ours.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.base import Cell, ViewLine
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.statement_header import across_groups, header_like, read_header
from app.ingestion.parsers.statement_lines import without_rules
from app.ingestion.parsers.table_statement import TableStatementParser
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, Statement
from tests.fixtures import synth_ruled_tables as synth

OPTIONS = ExtractOptions(previews=False, ocr=False)


def _read(data: bytes) -> ExtractResult:
    """Read by the bank statement parser. Which parser a document goes to is
    not this module's subject: the brokerage activity reader takes these
    pages for cash activity (a second ``DESCRIPTION`` heading is a column
    of its own to it, the titles its sections) and scores above the bank
    parsers, so the tests name the parser as a classifier's hint does."""
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    found = parse_extraction(result, kind="statement", parser_name="statement_generic")
    assert found.parse is not None
    assert found.parse.parser is not None
    assert found.parse.parser.name == "statement_generic"
    return found


def _only(result: ExtractResult) -> Statement:
    assert len(result.statements) == 1, result.statements
    return result.statements[0]


def _rows(statement: Statement) -> list[tuple[date, Decimal, str | None]]:
    return [(t.posted, t.amount, t.payee) for t in statement.transactions]


def _line(index: int, cells: list[tuple[str, float]], *, page: int = 1) -> ViewLine:
    y = 100.0 + 14.0 * index
    built = tuple(Cell(text, x0, y, x0 + 5.0 * len(text), y + 9.0) for text, x0 in cells)
    return ViewLine(
        page=page, index=index, cells=built, text=" ".join(t for t, _ in cells), height=9.0
    )


def _lines(rows: list[list[tuple[str, float]]]) -> list[ViewLine]:
    return [_line(index, cells) for index, cells in enumerate(rows)]


TWICE = [
    ("DATE", 20.0),
    ("DESCRIPTION", 68.0),
    ("AMOUNT", 241.0),
    ("|", 294.0),
    ("DATE", 308.0),
    ("DESCRIPTION", 356.0),
    ("AMOUNT", 529.0),
]


# -- the bar is no cell -----------------------------------------------------------------


def test_rule_cells_are_dropped_and_nothing_else() -> None:
    lines = _lines(
        [
            TWICE,
            [("|", 294.0)],
            [("03/02", 20.0), ("Lodgement", 68.0), ("700.00", 241.0), ("|", 294.0)],
            [("| |", 150.0), ("│", 294.0)],
            # A dash may be a sign, a separator or an empty column: kept.
            [("03/03", 20.0), ("Payment", 68.0), ("-", 200.0), ("45.10", 241.0)],
            # A bar among the words of a cell is that cell's.
            [("Totals | Page 1", 20.0)],
        ]
    )
    kept = without_rules(lines)
    assert [line.text for line in kept] == [
        "DATE DESCRIPTION AMOUNT DATE DESCRIPTION AMOUNT",
        "03/02 Lodgement 700.00",
        "03/03 Payment - 45.10",
        "Totals | Page 1",
    ]
    assert [len(line.cells) for line in kept] == [6, 3, 4, 1]
    # A line without a bar is the line it was.
    assert kept[2] is lines[4]
    assert kept[3] is lines[5]
    # Page and index stay the line's own.
    assert (kept[1].page, kept[1].index) == (1, 2)


def test_a_header_printed_twice_with_a_bar_between_is_a_table_across() -> None:
    (raw,) = _lines([TWICE])
    # As printed, the bar is a cell that is no heading: no header, and not
    # even the header of a table we do not read.
    assert read_header(raw) is None
    assert not header_like(raw)
    (line,) = TableStatementParser().prepare([raw])
    columns = read_header(line)
    assert columns is not None
    assert [column.role for column in columns] == ["date", "description", "amount", "across"]
    groups = across_groups(line)
    assert groups is not None
    assert [[column.role for column in group] for group in groups] == [
        ["date", "description", "amount"]
    ] * 2


def test_a_table_two_across_with_a_bar_between_is_read() -> None:
    result = _read(synth.two_across_with_rules())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["two_across_with_rules"]
    assert synth.ALONE_BESIDE_A_RULE in _rows(statement)
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert result.parse is not None
    assert result.parse.skipped_lines == 0
    assert all("|" not in f"{t.payee} {t.memo or ''}" for t in statement.transactions)


def test_entries_alone_beside_a_bar_are_read() -> None:
    result = _read(synth.two_across_left_half_only())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["two_across_left_half_only"]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert result.parse is not None
    assert result.parse.skipped_lines == 0


# -- under a header we do not read ------------------------------------------------------

#: Heading words only, and no header we read: the second half names no amount.
UNREAD = [
    ("Date", 62.0),
    ("Description", 150.0),
    ("Amount", 300.0),
    ("Date", 350.0),
    ("Description", 420.0),
]


def test_lines_under_a_header_we_do_not_read_are_counted() -> None:
    parser = TableStatementParser()
    lines = _lines(
        [
            UNREAD,
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 300.0)],
            [("01/06/2026", 62.0), ("Bakery", 150.0), ("3.00", 300.0)],
            # A balance line is the summary's, figures by day have no words,
            # a line without a date is no row: none of them is counted.
            [("01/31/2026", 62.0), ("Ending Balance", 150.0), ("1,200.00", 300.0)],
            # The same with more of the summary beside it (a grid of labels
            # and values): the label has its figure right after it.
            [
                ("01/01/2026", 62.0),
                ("Beginning Balance", 150.0),
                ("1,150.00", 300.0),
                ("Minimum Balance", 350.0),
                ("94.70", 440.0),
            ],
            [("01/07/2026", 62.0), ("1,192.50", 300.0)],
            [("Thank you for banking with us", 62.0), ("0.00", 300.0)],
        ]
    )
    assert parser.columns(lines[0]) is None
    assert header_like(lines[0])
    rows, summary, _, skipped, found_header = parser._collect(lines)
    assert rows == []
    assert not found_header
    assert skipped == 2
    assert [box for _, box in summary.skipped_boxes] == [lines[1].bbox, lines[2].bbox]


def test_the_count_under_an_unread_header_ends_with_its_page() -> None:
    parser = TableStatementParser()
    lines = [
        _line(0, UNREAD),
        _line(1, [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 300.0)]),
        # The next page starts under no header: as outside a table.
        _line(2, [("01/06/2026", 62.0), ("Bakery", 150.0), ("3.00", 300.0)], page=2),
    ]
    _, _, _, skipped, _ = parser._collect(lines)
    assert skipped == 1

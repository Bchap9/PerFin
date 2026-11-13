# SPDX-License-Identifier: AGPL-3.0-or-later
"""Header recognition of the generic statement reader, on hand-built lines.

:mod:`app.ingestion.parsers.statement_header` holds pure functions on a
:class:`ViewLine`, so a header is tested here without a PDF: every line is
built from ``(text, x0)`` cells and nothing but column headings is printed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers import statement_header, table_statement
from app.ingestion.parsers.base import Cell, ViewLine
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.statement_header import (
    Column,
    Role,
    header_like,
    read_header,
    split_heading_cell,
    stack_headers,
)
from app.ingestion.sandbox.protocol import ExtractOptions, ParseInfo, Statement
from tests.fixtures import synth_statement_headers as synth


def _line(cells: list[tuple[str, float]], index: int = 0) -> ViewLine:
    """One line of ``(text, x0)`` cells, each five points wide per character."""
    y = 100.0 + 14 * index
    built = tuple(Cell(text, x0, y, x0 + 5.0 * len(text), y + 9.0) for text, x0 in cells)
    return ViewLine(
        page=1, index=index, cells=built, text=" ".join(t for t, _ in cells), height=9.0
    )


def test_one_line_header_is_read() -> None:
    line = _line([("Date", 62.0), ("Description", 150.0), ("Amount", 420.0), ("Balance", 500.0)])
    assert read_header(line) == [
        Column("date", 62.0, 82.0),
        Column("description", 150.0, 205.0),
        Column("amount", 420.0, 450.0),
        Column("balance", 500.0, 535.0),
    ]
    assert not header_like(_line([("Date", 62.0)]))


def test_headless_heading_gives_a_lead_column() -> None:
    line = _line(
        [("New Transactions for the card", 62.0), ("Foreign Spending", 330.0), ("Amount", 500.0)]
    )
    assert read_header(line) == [
        Column("lead", 0.0, 330.0),
        Column("foreign", 330.0, 410.0),
        Column("amount", 500.0, 530.0),
    ]
    # No heading that names a table: money columns alone are no header.
    assert read_header(_line([("Fees for the card", 62.0), ("Amount", 500.0)])) is None
    # A figure on the line: a row or a total, never a header.
    assert read_header(_line([("New Transactions", 62.0), ("Amount 12.00", 500.0)])) is None


def test_a_line_of_headings_we_do_not_read_is_header_like() -> None:
    line = _line([("Date", 62.0), ("Balance", 150.0), ("Date", 300.0), ("Balance", 390.0)])
    assert header_like(line)
    assert read_header(line) is None
    # A word that is no heading, or a figure, and the line is not header-like.
    assert not header_like(_line([("Date", 62.0), ("Notes", 150.0)]))
    assert not header_like(_line([("Date", 62.0), ("Balance 10.00", 150.0)]))


def test_headers_can_be_replaced() -> None:
    line = _line([("When", 62.0), ("What", 150.0), ("Sum", 420.0)])
    assert read_header(line) is None
    own: Mapping[str, Role] = {"when": "date", "what": "description", "sum": "amount"}
    assert [c.role for c in read_header(line, own) or []] == ["date", "description", "amount"]
    assert header_like(line, own)
    assert not header_like(line)


def test_stack_headers_returns_the_lines_unchanged() -> None:
    lines = (
        _line([("Date", 62.0), ("Description", 150.0)], 0),
        _line([("Posted", 62.0), ("Amount", 420.0)], 1),
    )
    stacked = stack_headers(lines)
    assert stacked == list(lines)
    assert all(a is b for a, b in zip(stacked, lines, strict=True))


def test_moved_names_still_import_from_table_statement() -> None:
    for name in ("HEADERS", "Column", "MONEY_ROLES", "NUMERIC_ROLES", "TABLE_HEADING_WORDS"):
        assert getattr(table_statement, name) is getattr(statement_header, name), name
    assert table_statement.Role == statement_header.Role


def test_the_parser_delegates_with_its_own_headers() -> None:
    class Own(table_statement.TableStatementParser):
        headers: ClassVar[Mapping[str, Role]] = {
            "when": "date",
            "what": "description",
            "sum": "amount",
        }

    line = _line([("When", 62.0), ("What", 150.0), ("Sum", 420.0)])
    assert table_statement.TableStatementParser().columns(line) is None
    assert [c.role for c in Own().columns(line) or []] == ["date", "description", "amount"]
    assert Own()._header_like(line)
    assert Own().prepare([line]) == [line]


# -- two headings in one cell ---------------------------------------------------


def _cell(text: str, x0: float = 100.0) -> Cell:
    return _line([(text, x0)]).cells[0]


def test_two_headings_in_one_cell_are_two_columns() -> None:
    parts = split_heading_cell(_cell("WITHDRAWAL BALANCE", 400.0), statement_header.HEADERS)
    assert parts is not None
    assert [(role, c.text, c.x0, c.x1) for role, c in parts] == [
        ("debit", "WITHDRAWAL", 400.0, 450.0),
        ("balance", "BALANCE", 455.0, 490.0),
    ]
    line = _line(
        [("DATE", 62.0), ("DESCRIPTION", 150.0), ("DEPOSIT", 330.0), ("WITHDRAWAL BALANCE", 400.0)]
    )
    assert read_header(line) == [
        Column("date", 62.0, 82.0),
        Column("description", 150.0, 205.0),
        Column("credit", 330.0, 365.0),
        Column("debit", 400.0, 450.0),
        Column("balance", 455.0, 490.0),
    ]
    # A heading as a whole is not split, and neither is one word.
    assert split_heading_cell(_cell("Transaction Details"), statement_header.HEADERS) is None
    assert split_heading_cell(_cell("Balance"), statement_header.HEADERS) is None


def test_date_then_two_word_heading_in_one_cell() -> None:
    line = _line(
        [
            ("Date Transaction Description", 62.0),
            ("Withdrawal", 330.0),
            ("Deposit", 400.0),
            ("Balance", 470.0),
        ]
    )
    # "Transaction Description" is one heading: the fewest phrases win.
    assert read_header(line) == [
        Column("date", 62.0, 82.0),
        Column("description", 87.0, 202.0),
        Column("debit", 330.0, 380.0),
        Column("credit", 400.0, 435.0),
        Column("balance", 470.0, 505.0),
    ]


def test_a_cell_with_a_leftover_word_is_not_split() -> None:
    for text in ("Amount Enclosed", "Date of Birth", "Total Amount Due", "Date Paid in Full"):
        assert split_heading_cell(_cell(text), statement_header.HEADERS) is None, text
    assert (
        read_header(_line([("Date of Birth", 62.0), ("Description", 150.0), ("Amount", 420.0)]))
        is None
    )
    assert read_header(_line([("Date", 62.0), ("Description Amount Enclosed", 150.0)])) is None
    assert not header_like(_line([("Date", 62.0), ("Amount Enclosed", 150.0)]))


def test_second_date_heading_is_the_post_date() -> None:
    line = _line(
        [
            ("Transaction Date", 40.0),
            ("Posting Date", 140.0),
            ("Description", 230.0),
            ("Amount", 420.0),
        ]
    )
    assert read_header(line) == [
        Column("date", 40.0, 120.0),
        Column("post_date", 140.0, 200.0),
        Column("description", 230.0, 285.0),
        Column("amount", 420.0, 450.0),
    ]
    # Both dates in one cell.
    merged = _line([("Trans Date Post Date", 40.0), ("Description", 230.0), ("Amount", 420.0)])
    assert [(c.role, c.x0, c.x1) for c in read_header(merged) or []] == [
        ("date", 40.0, 90.0),
        ("post_date", 95.0, 140.0),
        ("description", 230.0, 285.0),
        ("amount", 420.0, 450.0),
    ]
    # A posting date the line names keeps its place; a third date names nothing.
    named = _line(
        [("Post Date", 40.0), ("Date", 110.0), ("Date", 150.0), ("Payee", 230.0), ("Amount", 420.0)]
    )
    assert [(c.role, c.x0) for c in read_header(named) or []] == [
        ("post_date", 40.0),
        ("date", 110.0),
        ("description", 230.0),
        ("amount", 420.0),
    ]


def test_unknown_heading_right_of_the_money_is_an_other_column() -> None:
    line = _line([("Date", 62.0), ("Description", 150.0), ("Amount", 420.0), ("Category", 470.0)])
    assert read_header(line) == [
        Column("date", 62.0, 82.0),
        Column("description", 150.0, 205.0),
        Column("amount", 420.0, 450.0),
        Column("other", 470.0, 510.0),
    ]
    # Left of the money an unknown heading is ignored, as before.
    left = _line([("Date", 62.0), ("Ref Code", 100.0), ("Description", 150.0), ("Amount", 420.0)])
    assert [c.role for c in read_header(left) or []] == ["date", "description", "amount"]
    # Between two money columns it is right of the leftmost one.
    between = _line(
        [("Date", 62.0), ("Payee", 150.0), ("Amount", 330.0), ("Tag", 380.0), ("Balance", 470.0)]
    )
    assert [c.role for c in read_header(between) or []] == [
        "date",
        "description",
        "amount",
        "other",
        "balance",
    ]
    # An unknown cell makes no header of a line that is none.
    assert read_header(_line([("Date", 62.0), ("Amount", 420.0), ("Category", 470.0)])) is None


def test_prose_is_no_header() -> None:
    sentence = "The date and description of each amount are shown on the pages that follow"
    assert len(sentence.split()) == 14
    assert read_header(_line([(sentence, 40.0)])) is None
    assert not header_like(_line([(sentence, 40.0)]))
    # Shorter, and still a sentence: words are left over.
    assert read_header(_line([("Date and description of each amount", 40.0)])) is None
    # Thirteen heading words in one cell are more than any header prints.
    run = " ".join(["Date", "Description", "Amount"] * 4 + ["Balance"])
    assert read_header(_line([(run, 40.0)])) is None
    # A whole header in one cell is read.
    assert read_header(_line([("Date Description Amount Balance", 40.0)])) is not None


def test_a_line_with_an_amount_is_not_read_by_words() -> None:
    line = _line(
        [
            ("Trans Date Post Date", 40.0),
            ("Description", 230.0),
            ("Amount", 420.0),
            ("12.00", 470.0),
        ]
    )
    assert read_header(line) is None
    whole = _line([("Date", 40.0), ("Description", 230.0), ("Amount", 420.0), ("Fee 12.00", 470.0)])
    # Whole cells are read as before, and nothing is added to them.
    assert [c.role for c in read_header(whole) or []] == ["date", "description", "amount"]


def test_a_line_of_more_than_twelve_cells_is_read_by_whole_cells_only() -> None:
    filler = [(f"n{i}", 500.0 + 20 * i) for i in range(10)]
    line = _line([("Trans Date Post Date", 40.0), ("Payee", 230.0), ("Amount", 420.0), *filler])
    assert len(line.cells) == 13
    assert read_header(line) is None


def test_header_like_reads_words() -> None:
    line = _line(
        [
            ("Date", 40.0),
            ("Amount", 135.0),
            ("Date", 220.0),
            ("Amount Date", 300.0),
            ("Amount", 400.0),
        ]
    )
    assert header_like(line)
    assert read_header(line) is None
    # One cell holding the whole header of a table we do not read.
    assert header_like(_line([("Date Balance Date Balance", 40.0)]))
    # A row's wrapped text made of heading words is no header.
    assert not header_like(_line([("Check Deposit", 150.0)]))
    assert not header_like(_line([("Transaction Details", 150.0)]))
    assert not header_like(_line([("Date Balance 10.00", 40.0)]))


# -- whole documents ------------------------------------------------------------


def _read(data: bytes) -> tuple[Statement, ParseInfo]:
    result = extract_pdf(data, ExtractOptions(previews=False, ocr=False), "pdf")
    assert result.ok, result.error
    parsed = parse_extraction(result, kind="statement")
    assert parsed.parse is not None
    assert len(parsed.statements) == 1, parsed.parse.warnings
    return parsed.statements[0], parsed.parse


def _rows(statement: Statement) -> list[tuple[str | None, object]]:
    return [(t.payee, t.amount) for t in statement.transactions]


def test_merged_heading_statement_reads_both_columns() -> None:
    statement, info = _read(synth.merged_heading_checking())
    assert _rows(statement) == list(synth.MERGED_ROWS)
    assert sum(1 for t in statement.transactions if t.amount < 0) == 4  # the withdrawals
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert statement.ledger_balance is not None
    assert str(statement.ledger_balance.amount) == "1828.15"
    assert statement.account.last4 == "5521"
    assert info.skipped_lines == 0


def test_amount_before_description_is_read() -> None:
    statement, info = _read(synth.amount_before_description())
    assert _rows(statement) == list(synth.AMOUNT_FIRST_ROWS)
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert info.skipped_lines == 0


def test_category_is_not_joined_to_the_amount() -> None:
    statement, info = _read(synth.two_dates_and_category())
    assert _rows(statement) == list(synth.TWO_DATES_ROWS)
    assert info.skipped_lines == 0
    for row in statement.transactions:
        words = f"{row.payee} {row.memo or ''}".split()
        assert not set(words) & set(synth.CATEGORIES), words
        assert (row.posted.year, row.posted.month) == (2026, 5)
    # The row's date is the transaction date, not the posting date.
    assert [t.posted.day for t in statement.transactions] == [2, 6, 11, 15, 22, 29]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


def test_daily_balances_are_not_transactions() -> None:
    statement, info = _read(synth.daily_balance_after_rows())
    assert _rows(statement) == list(synth.DAILY_ROWS)
    assert not {t.amount for t in statement.transactions} & set(synth.DAILY_BALANCES)
    assert info.skipped_lines == 0
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"

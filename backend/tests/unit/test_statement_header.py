# SPDX-License-Identifier: AGPL-3.0-or-later
"""Header recognition of the generic statement reader, on hand-built lines.

:mod:`app.ingestion.parsers.statement_header` holds pure functions on a
:class:`ViewLine`, so a header is tested here without a PDF: every line is
built from ``(text, x0)`` cells and nothing but column headings is printed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import ClassVar

import pytest

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers import statement_header, table_statement
from app.ingestion.parsers.base import Cell, DocumentView, ViewLine
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.statement_header import (
    Column,
    Role,
    across_start,
    header_like,
    heading_role,
    read_header,
    repair_heading,
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


def test_a_one_cell_title_or_wrap_ending_in_a_date_word_is_not_header_like() -> None:
    # ``Posted`` is a date heading, and these are titles inside a table or a
    # row's wrapped text: they must not end the table.
    for printed in (
        "Deposits Posted",
        "Withdrawals Posted",
        "Transaction Posted",
        "Other Debits Posted",
        "Credits Date Posted",
        "Deposit Date",
        "Description Date",
        "Date Posted Transaction",
    ):
        assert not header_like(_line([(printed, 40.0)])), printed
    # One cell is a header of a table we do not read when it starts with a
    # date heading and names a column of figures.
    for printed in ("Date Balance", "Date Amount Date Amount", "Posted Balance", "DATE AMOUNT USD"):
        assert header_like(_line([(printed, 40.0)])), printed


def test_cells_that_all_name_one_column_are_not_header_like() -> None:
    # A row's wrapped text in two cells, both words for money in.
    assert not header_like(_line([("Deposit", 110.0), ("Additions", 220.0)]))
    assert not header_like(_line([("Withdrawal", 110.0), ("Other Debits", 220.0)]))
    # Two columns of different kinds are the header of a table.
    assert header_like(_line([("Deposits", 110.0), ("Withdrawals", 220.0)]))
    assert header_like(_line([("Date", 40.0), ("Balance", 150.0)]))


def test_banking_abbreviations_are_no_currency() -> None:
    for printed in (
        "DEBIT POS",
        "DEBIT ATM",
        "CREDIT ACH",
        "BALANCE FWD",
        "CREDIT ADJ",
        "CREDIT INT",
        "CREDIT DIV",
    ):
        assert heading_role(printed, statement_header.HEADERS) is None, printed
    assert not header_like(_line([("DEBIT POS", 110.0), ("CREDIT ACH", 220.0)]))


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


def test_titles_of_heading_words_inside_a_table_do_not_end_it() -> None:
    statement, info = _read(synth.titles_inside_the_table())
    assert _rows(statement) == list(synth.INSIDE_ROWS)
    assert info.skipped_lines == 0
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


def test_wrapped_heading_words_do_not_end_the_table() -> None:
    statement, info = _read(synth.wrapped_heading_words())
    assert _rows(statement) == list(synth.WRAPPED_ROWS)
    assert info.skipped_lines == 0
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


# -- reworded headings, units, the one unknown cell, a damaged word --------------

#: Printed heading -> role, one per phrase the vocabulary gained.
_NEW_WORDS: dict[str, Role] = {
    "Date of Transaction": "date",
    "Date Posted": "date",
    "Effective Date": "date",
    "Txn Date": "date",
    "Tran Date": "date",
    "Information": "description",
    "Narration": "description",
    "Merchant Name": "description",
    "Merchant Name or Transaction Description": "description",
    "Description of Transaction or Credit": "description",
    "Transaction Detail": "description",
    "Activity Description": "description",
    "Deposits/Credits": "credit",
    "Deposits / Additions": "credit",
    "Deposits & Other Credits": "credit",
    "Deposits and Other Credits": "credit",
    "Deposits and Credits": "credit",
    "Deposits and Additions": "credit",
    "Additions": "credit",
    "Deposit Amt.": "credit",
    "Deposit Amount": "credit",
    "Credit Amount": "credit",
    "Withdrawals/Debits": "debit",
    "Withdrawals / Subtractions": "debit",
    "Withdrawals & Other Debits": "debit",
    "Withdrawals and Other Debits": "debit",
    "Withdrawals and Debits": "debit",
    "Withdrawals and Subtractions": "debit",
    "Subtractions": "debit",
    "Other Debits": "debit",
    "Withdrawal Amt.": "debit",
    "Withdrawal Amount": "debit",
    "Debit Amount": "debit",
    "Ending Daily Balance": "balance",
    "Daily Balance": "balance",
    "Closing Balance": "balance",
    "Transaction Amount": "amount",
    "Check Nbr": "check",
    "Chk": "check",
}


@pytest.mark.parametrize(("printed", "role"), sorted(_NEW_WORDS.items()))
def test_new_heading_words(printed: str, role: Role) -> None:
    assert heading_role(printed, statement_header.HEADERS) == role


def test_words_whose_direction_differs_are_no_headings() -> None:
    for printed in ("Payments", "Charges", "Receipts", "Daily Ending Balance"):
        assert heading_role(printed, statement_header.HEADERS) is None, printed


def test_unit_after_a_money_heading() -> None:
    for printed, role in (
        ("Amount (SGD)", "amount"),
        ("Balance (USD)", "balance"),
        ("Withdrawals [CAD]", "debit"),
        ("AMOUNT EUR", "amount"),
        ("DEPOSITS GBP", "credit"),
    ):
        assert heading_role(printed, statement_header.HEADERS) == role, printed
    line = _line(
        [("Date", 40.0), ("Details", 110.0), ("Amount (CAD)", 330.0), ("Balance (CAD)", 440.0)]
    )
    assert read_header(line) == [
        Column("date", 40.0, 60.0),
        Column("description", 110.0, 145.0),
        Column("amount", 330.0, 390.0),
        Column("balance", 440.0, 505.0),
    ]
    # Both money headings in one cell, each with its unit.
    parts = split_heading_cell(_cell("Amount (CAD) Balance (CAD)"), statement_header.HEADERS)
    assert [(role, c.text) for role, c in parts or ()] == [
        ("amount", "Amount (CAD)"),
        ("balance", "Balance (CAD)"),
    ]
    # No unit: a long bracket, a word in small letters, a word that is no currency.
    for printed in (
        "Amount (see the note overleaf)",
        "Amount eur",
        "AMOUNT DUE",
        "BALANCE NEW",
        "Amount (CAD) (USD)",
        "(CAD)",
    ):
        assert heading_role(printed, statement_header.HEADERS) is None, printed


def test_unit_is_not_dropped_from_a_text_heading() -> None:
    for printed in ("Description (USD)", "DATE EUR", "CHECK USD", "Post Date (UTC)"):
        assert heading_role(printed, statement_header.HEADERS) is None, printed
    assert not header_like(_line([("Date", 40.0), ("Description (USD)", 110.0)]))


def test_single_unknown_cell_between_date_and_money_is_the_description() -> None:
    line = _line([("Date", 40.0), ("Memo Text", 110.0), ("Amount", 420.0)])
    assert read_header(line) == [
        Column("date", 40.0, 60.0),
        Column("description", 110.0, 155.0),
        Column("amount", 420.0, 450.0),
    ]
    wide = _line(
        [
            ("Date", 40.0),
            ("What it was for", 110.0),
            ("Withdrawals", 300.0),
            ("Deposits", 380.0),
            ("Balance", 460.0),
            ("Category", 520.0),
        ]
    )
    assert [c.role for c in read_header(wide) or []] == [
        "date",
        "description",
        "debit",
        "credit",
        "balance",
        "other",
    ]
    # The line stays a list of headings we do not all know: it ends no table.
    assert not header_like(line)


def test_two_unknown_cells_are_no_header() -> None:
    assert (
        read_header(_line([("Date", 40.0), ("Memo", 110.0), ("Ref", 200.0), ("Amount", 420.0)]))
        is None
    )
    # One of them left of the date: still two.
    assert (
        read_header(_line([("Ref", 10.0), ("Date", 40.0), ("Memo", 110.0), ("Amount", 420.0)]))
        is None
    )


def test_an_unknown_cell_that_is_no_description_heading_is_no_header() -> None:
    for cells in (
        # Left of the date, or reaching under the amount heading.
        [("Memo", 10.0), ("Date", 40.0), ("Amount", 420.0)],
        [("Date", 40.0), ("A heading far too wide to stand here", 300.0), ("Amount", 420.0)],
        # Five words; a digit; a word that names a figure or a date.
        [("Date", 40.0), ("what you paid us for", 110.0), ("Amount", 420.0)],
        [("Date", 40.0), ("Page 2", 110.0), ("Amount", 420.0)],
        [("Date", 40.0), ("Interest Rate", 110.0), ("Amount", 420.0)],
        [("Date", 40.0), ("Account Summary", 110.0), ("Amount", 420.0)],
        [("Date", 40.0), ("Total", 110.0), ("Amount", 420.0)],
        [("Date", 40.0), ("Due Date", 110.0), ("Amount", 420.0)],
        # No money heading, or no date heading.
        [("Date", 40.0), ("Memo Text", 110.0), ("Balance", 420.0)],
        [("Memo Text", 110.0), ("Amount", 420.0)],
    ):
        assert read_header(_line(cells)) is None, cells
    # A figure on the line: a row, never a header.
    assert (
        read_header(_line([("Date", 40.0), ("Memo Text", 110.0), ("Amount 12.00", 420.0)])) is None
    )
    # A known description or check heading: the unknown cell is ignored as before.
    known = _line([("Date", 40.0), ("Memo", 110.0), ("Payee", 200.0), ("Amount", 420.0)])
    assert [(c.role, c.x0) for c in read_header(known) or []] == [
        ("date", 40.0),
        ("description", 200.0),
        ("amount", 420.0),
    ]


def test_unknown_cell_naming_a_balance_is_not_a_description() -> None:
    line = _line([("Date", 40.0), ("DAILY ENDING BALANCE", 150.0), ("Amount", 400.0)])
    assert read_header(line) is None
    assert not header_like(line)


def test_cut_heading_is_the_description() -> None:
    line = _line([("Date", 40.0), ("Descri", 110.0), ("Amount", 420.0)])
    assert read_header(line) == [
        Column("date", 40.0, 60.0),
        Column("description", 110.0, 140.0),
        Column("amount", 420.0, 450.0),
    ]


def _ocr(text: str, x0: float = 100.0, conf: float | None = 0.9) -> Cell:
    return Cell(text, x0, 100.0, x0 + 5.0 * len(text), 109.0, conf)


def _ocr_line(cells: list[tuple[str, float]]) -> ViewLine:
    return ViewLine(
        page=1,
        index=0,
        cells=tuple(_ocr(text, x0) for text, x0 in cells),
        text=" ".join(t for t, _ in cells),
        height=9.0,
    )


def test_ocr_damaged_heading_is_repaired() -> None:
    headers = statement_header.HEADERS
    assert repair_heading(_ocr("ithdrawal"), headers) == "debit"  # a letter lost
    assert repair_heading(_ocr("ithdrawal", conf=None), headers) is None  # a text layer
    assert repair_heading(_ocr("Deposit5"), headers) == "credit"  # a letter changed
    assert repair_heading(_ocr("Descriiption"), headers) == "description"  # a letter added
    assert repair_heading(_ocr("Balance:"), headers) is None  # a heading as printed needs no repair
    assert repair_heading(_ocr("Wxthdrawxl"), headers) is None  # two letters off
    # Two headings as close that name different columns: neither.
    own: Mapping[str, Role] = {"posted": "date", "costed": "amount"}
    assert repair_heading(_ocr("rosted"), own) is None
    assert repair_heading(_ocr("postel"), own) == "date"
    line = _ocr_line(
        [
            ("Date", 40.0),
            ("Descripton", 110.0),
            ("ithdrawal", 300.0),
            ("Deposit5", 380.0),
            ("Balance", 460.0),
        ]
    )
    assert [c.role for c in read_header(line) or []] == [
        "date",
        "description",
        "debit",
        "credit",
        "balance",
    ]
    # The same words in a text layer are not repaired.
    typed = _line(
        [("Date", 40.0), ("Description", 110.0), ("ithdrawal", 300.0), ("Balance", 460.0)]
    )
    assert read_header(typed) is None


def test_short_words_are_not_repaired() -> None:
    headers = statement_header.HEADERS
    for text in ("Dat", "Debi", "Debit5", "Payes", "Amoun", "Check5"):
        assert repair_heading(_ocr(text), headers) is None, text
    # Only a cell of one word.
    assert repair_heading(_ocr("ithdrawal alance"), headers) is None
    assert repair_heading(_ocr("Transacton Date"), headers) is None
    # A line with a figure is a row: its words are not repaired into headings.
    row = _ocr_line(
        [("Date", 40.0), ("Description", 110.0), ("ithdrawal", 300.0), ("12.00", 460.0)]
    )
    assert read_header(row) is None


def test_reworded_statement_reads_and_reconciles() -> None:
    statement, info = _read(synth.reworded_headings_checking())
    assert _rows(statement) == list(synth.REWORDED_ROWS)
    assert sum(1 for t in statement.transactions if t.amount < 0) == 4  # the withdrawals
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert statement.ledger_balance is not None
    assert str(statement.ledger_balance.amount) == "1469.25"
    assert info.skipped_lines == 0


def test_amounts_with_a_unit_are_read() -> None:
    statement, info = _read(synth.amount_with_unit())
    assert _rows(statement) == list(synth.UNIT_ROWS)
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert info.skipped_lines == 0


def test_unknown_description_heading_statement_is_read() -> None:
    statement, info = _read(synth.unknown_description_heading())
    assert _rows(statement) == list(synth.MEMO_ROWS)
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert info.skipped_lines == 0


def test_title_between_date_and_amount_makes_no_table() -> None:
    result = extract_pdf(
        synth.date_balance_pairs_with_title(), ExtractOptions(previews=False, ocr=False), "pdf"
    )
    assert result.ok, result.error
    parsed = parse_extraction(result, kind="statement")
    assert parsed.parse is not None
    assert all(not s.transactions for s in parsed.statements)
    assert parsed.parse.skipped_lines == 0


# -- headers printed over two or three lines --------------------------------------

_STACK_TOP: list[tuple[str, float]] = [
    ("Check", 95.0),
    ("Deposits/", 330.0),
    ("Withdrawals/", 410.0),
    ("Ending daily", 490.0),
]
_STACK_BOTTOM: list[tuple[str, float]] = [
    ("Date", 40.0),
    ("Number Description", 95.0),
    ("Credits", 330.0),
    ("Debits", 410.0),
    ("balance", 490.0),
]
_PLAIN_HEADER: list[tuple[str, float]] = [
    ("Date", 62.0),
    ("Description", 150.0),
    ("Amount", 420.0),
]


def _at(cells: list[tuple[str, float]], top: float, index: int = 0) -> ViewLine:
    """As :func:`_line`, with the line's top at ``top``."""
    built = tuple(Cell(text, x0, top, x0 + 5.0 * len(text), top + 9.0) for text, x0 in cells)
    return ViewLine(
        page=1, index=index, cells=built, text=" ".join(t for t, _ in cells), height=9.0
    )


def _unchanged(lines: list[ViewLine]) -> bool:
    stacked = stack_headers(lines)
    return len(stacked) == len(lines) and all(a is b for a, b in zip(stacked, lines, strict=True))


def _roles(line: ViewLine) -> list[Role]:
    return [c.role for c in read_header(line) or []]


def test_two_line_header_is_one_header() -> None:
    before = _line([("Account activity", 40.0)], 0)
    top, bottom = _line(_STACK_TOP, 1), _line(_STACK_BOTTOM, 2)
    after = _line([("1/2", 40.0), ("Payroll", 130.0), ("1,500.00", 330.0)], 3)
    stacked = stack_headers([before, top, bottom, after])
    assert len(stacked) == 3
    assert stacked[0] is before
    assert stacked[2] is after
    header = stacked[1]
    assert (header.page, header.index, header.height) == (1, 2, 9.0)
    assert read_header(header) == [
        Column("date", 40.0, 60.0),
        Column("check", 95.0, 125.0),  # ``Check`` over ``Number``
        Column("description", 130.0, 185.0),
        Column("credit", 330.0, 375.0),  # as wide as ``Deposits/``, the longer of the two
        Column("debit", 410.0, 470.0),
        Column("balance", 490.0, 550.0),
    ]
    # A cell of both lines keeps the box of all its words.
    credit = next(c for c in header.cells if c.text == "Deposits/ Credits")
    assert (credit.y0, credit.y1) == (top.cells[0].y0, bottom.cells[0].y1)
    # Alone, the second line reads ``Number Description`` as its description.
    assert _roles(bottom) == ["date", "description", "credit", "debit", "balance"]


def test_three_line_header_with_a_single_cell_line() -> None:
    lines = [
        _line(
            [
                ("POSTING", 40.0),
                ("DEPOSITS & OTHER", 300.0),
                ("WITHDRAWALS &", 400.0),
                ("DAILY", 500.0),
            ],
            0,
        ),
        _line([("TRANSACTION DESCRIPTION", 110.0)], 1),
        _line(
            [
                ("DATE", 40.0),
                ("CREDITS (+)", 300.0),
                ("OTHER DEBITS (-)", 400.0),
                ("BALANCE", 500.0),
            ],
            2,
        ),
    ]
    assert all(read_header(line) is None for line in lines)
    stacked = stack_headers(lines)
    assert len(stacked) == 1
    assert stacked[0].index == 2
    assert _roles(stacked[0]) == ["date", "description", "credit", "debit", "balance"]
    assert [c.text for c in stacked[0].cells] == [
        "POSTING DATE",
        "TRANSACTION DESCRIPTION",
        "DEPOSITS & OTHER CREDITS (+)",
        "WITHDRAWALS & OTHER DEBITS (-)",
        "DAILY BALANCE",
    ]


def _view_lines(data: bytes) -> list[ViewLine]:
    result = extract_pdf(data, ExtractOptions(previews=False, ocr=False), "pdf")
    assert result.ok, result.error
    return list(DocumentView.from_result(result).lines)


def test_a_title_over_a_header_is_not_stacked() -> None:
    lines = _view_lines(synth.title_over_header())
    stacked = stack_headers(lines)
    assert len(stacked) == len(lines)
    assert all(a is b for a, b in zip(stacked, lines, strict=True))
    texts = [line.text for line in stacked]
    assert texts.count(synth.TITLED_HEADER) == 2
    for title in synth.TITLES:
        # The title is still a line of its own, right over a header.
        at = texts.index(title)
        assert texts[at + 1] == synth.TITLED_HEADER
        assert _roles(stacked[at + 1]) == ["date", "description", "amount"]
        gap = stacked[at + 1].bbox[1] - stacked[at].bbox[1]
        assert gap <= 2.2 * stacked[at].height, "the fixture must print the title close by"
    # Hand-built, the same two: a left-hand title, and a right-hand one over no heading.
    assert _unchanged([_line([("Deposits and other credits", 62.0)], 0), _line(_PLAIN_HEADER, 1)])
    assert _unchanged([_line([("Balance", 500.0)], 0), _line(_PLAIN_HEADER, 1)])
    assert _unchanged(
        [
            _line([("Deposits and other credits", 62.0), ("Balance", 500.0)], 0),
            _line(_PLAIN_HEADER, 1),
        ]
    )


def test_a_title_over_header_statement_reads_both_tables() -> None:
    statement, info = _read(synth.title_over_header())
    assert sorted(_rows(statement), key=str) == sorted(synth.TITLED_ROWS, key=str)
    assert info.skipped_lines == 0
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


def test_a_title_over_the_amount_heading_is_no_second_column() -> None:
    # Right-aligned over ``Amount``: headings one above the other are one
    # phrase or nothing, never a debit column beside the amount.
    assert _unchanged([_line([("Withdrawals", 395.0)], 0), _line(_PLAIN_HEADER, 1)])
    assert _unchanged([_line([("Balance", 415.0)], 0), _line(_PLAIN_HEADER, 1)])
    # Nor may the title be split off a heading it shares a cell with.
    assert _unchanged(
        [
            _line([("Posting", 62.0), ("Withdrawals", 395.0)], 0),
            _line([("Date", 62.0), ("Details", 150.0), ("Amount", 420.0)], 1),
        ]
    )


def test_a_header_that_reads_alone_wins_a_tie() -> None:
    # ``Posting Date`` and ``Withdrawal Amount`` are headings, and the line
    # under them names as many columns alone: it stays as it reads today.
    assert _unchanged([_line([("Posting", 62.0)], 0), _line(_PLAIN_HEADER, 1)])
    assert _unchanged([_line([("Withdrawal", 400.0)], 0), _line(_PLAIN_HEADER, 1)])
    assert _unchanged(
        [_line([("Posting", 62.0), ("Withdrawal", 400.0)], 0), _line(_PLAIN_HEADER, 1)]
    )


def test_lines_far_apart_are_not_stacked() -> None:
    near = [_at(_STACK_TOP, 100.0, 0), _at(_STACK_BOTTOM, 119.0, 1)]
    assert len(stack_headers(near)) == 1
    far = [_at(_STACK_TOP, 100.0, 0), _at(_STACK_BOTTOM, 127.0, 1)]  # three line heights
    assert _unchanged(far)
    # Nor across a page break.
    other_page = _line(_STACK_BOTTOM, 1)
    other_page = ViewLine(2, 1, other_page.cells, other_page.text, other_page.height)
    assert _unchanged([_line(_STACK_TOP, 0), other_page])


def test_a_line_with_an_amount_is_never_part_of_a_header() -> None:
    top = [*_STACK_TOP[:3], ("Ending daily 1,250.00", 490.0)]
    assert _unchanged([_line(top, 0), _line(_STACK_BOTTOM, 1)])
    bottom = [*_STACK_BOTTOM[:4], ("balance 1,250.00", 490.0)]
    assert _unchanged([_line(_STACK_TOP, 0), _line(bottom, 1)])
    # A line too long to be headings is none either.
    long = [("Check", 95.0), ("Deposits/ " + "and more words " * 9, 330.0)]
    assert _unchanged([_line(long, 0), _line(_STACK_BOTTOM, 1)])


def test_a_line_longer_than_the_limit_is_never_part_of_a_header() -> None:
    # A heading nobody knows right of the money is an ``other`` column, so
    # the two lines stack whatever it says: only its length differs here.
    def bottom(filler: str) -> ViewLine:
        return _line([*_STACK_BOTTOM, (filler, 600.0)], 1)

    lead = len(_line(_STACK_BOTTOM).text) + 1
    limit = statement_header.MAX_STACK_CHARS
    assert limit == 130
    fits = ("notes " * 40)[: limit - lead].strip().ljust(limit - lead, "s")
    assert len(bottom(fits).text) == limit
    stacked = stack_headers([_line(_STACK_TOP, 0), bottom(fits)])
    assert len(stacked) == 1
    assert _roles(stacked[0]) == [
        "date",
        "check",
        "description",
        "credit",
        "debit",
        "balance",
        "other",
    ]
    over = bottom(fits + "s")
    assert len(over.text) == limit + 1
    assert _unchanged([_line(_STACK_TOP, 0), over])
    # The top line, too.
    top = _line([*_STACK_TOP, (fits + "s" * 40, 600.0)], 0)
    assert len(top.text) > limit
    assert _unchanged([top, bottom("notes")])
    assert len(stack_headers([_line([*_STACK_TOP, ("Our", 600.0)], 0), bottom("notes")])) == 1


def test_two_lines_that_make_a_headless_heading_are_not_stacked() -> None:
    # Merged, the lines read ``New Transactions for the card | Foreign
    # Spending | Amount``: a heading with no date column is never stacked.
    top = _line([("New Transactions", 62.0), ("Foreign", 330.0)], 0)
    bottom = _line([("for the card", 62.0), ("Spending", 330.0), ("Amount", 500.0)], 1)
    assert read_header(top) is None
    assert read_header(bottom) is None
    assert _unchanged([top, bottom])


def test_lines_in_reverse_vertical_order_are_not_stacked() -> None:
    assert len(stack_headers([_at(_STACK_TOP, 100.0, 0), _at(_STACK_BOTTOM, 109.0, 1)])) == 1
    # The second line printed above the first, or at its height: no header.
    assert _unchanged([_at(_STACK_TOP, 109.0, 0), _at(_STACK_BOTTOM, 100.0, 1)])
    assert _unchanged([_at(_STACK_TOP, 100.0, 0), _at(_STACK_BOTTOM, 100.0, 1)])


def test_stacking_stops_at_three_lines() -> None:
    last = [("Date", 62.0), ("Credit", 150.0), ("Amount", 420.0)]
    three = [
        _line([("Description of", 150.0)], 0),
        _line([("Transaction or", 150.0)], 1),
        _line(last, 2),
    ]
    stacked = stack_headers(three)
    assert len(stacked) == 1
    assert _roles(stacked[0]) == ["date", "description", "amount"]
    four = [
        _line([("Description", 150.0)], 0),
        _line([("of Transaction", 150.0)], 1),
        _line([("or", 150.0)], 2),
        _line(last, 3),
    ]
    assert _unchanged(four)
    assert all(read_header(line) is None for line in four)


def test_a_section_title_under_a_stacked_header_is_not_part_of_it() -> None:
    # ``Deposits`` is a heading word, printed close under the two header
    # lines and under none of their cells: three lines name no more columns
    # than two, so the title stays a line and the credit column where it is.
    title = _line([("Deposits", 250.0)], 2)
    stacked = stack_headers([_line(_STACK_TOP, 0), _line(_STACK_BOTTOM, 1), title])
    assert len(stacked) == 2
    assert stacked[1] is title
    assert Column("credit", 330.0, 375.0) in (read_header(stacked[0]) or [])
    # Under ``Date`` it would be a second heading over another: no part either.
    under = _line([("Deposits", 40.0)], 2)
    stacked = stack_headers([_line(_STACK_TOP, 0), _line(_STACK_BOTTOM, 1), under])
    assert len(stacked) == 2
    assert stacked[1] is under


def test_an_unknown_heading_on_a_line_of_its_own_is_no_stacked_description() -> None:
    # ``Memo`` over ``text`` beside known headings of both lines: the description.
    both = [
        _line([("Posting", 62.0), ("Memo", 150.0), ("Paid", 420.0)], 0),
        _line([("Date", 62.0), ("text", 150.0), ("out", 420.0)], 1),
    ]
    stacked = stack_headers(both)
    assert len(stacked) == 1
    assert _roles(stacked[0]) == ["date", "description", "debit"]
    # The same words on a line that holds no heading: a title, not a header part.
    alone = [
        _line([("Posting", 62.0), ("Paid", 420.0)], 0),
        _line([("Memo text", 150.0)], 1),
        _line([("Date", 62.0), ("out", 420.0)], 2),
    ]
    assert _unchanged(alone)


def test_stacked_header_statement_reads_and_reconciles() -> None:
    statement, info = _read(synth.stacked_header_checking())
    assert _rows(statement) == list(synth.STACKED_ROWS)
    assert len(statement.transactions) == 10
    # By column: the two figures under ``Deposits/ Credits`` are money in.
    assert [t.amount > 0 for t in statement.transactions] == [
        amount > 0 for _, amount in synth.STACKED_ROWS
    ]
    assert sum(1 for t in statement.transactions if t.amount > 0) == 2
    checks = [t.payee for t in statement.transactions if (t.payee or "").startswith("CHECK")]
    assert checks == ["CHECK 1041", "CHECK 1042"]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert statement.ledger_balance is not None
    assert str(statement.ledger_balance.amount) == "2287.15"
    assert {(t.posted.year, t.posted.month) for t in statement.transactions} == {(2026, 1)}
    assert info.skipped_lines == 0


def test_description_on_its_own_line_statement() -> None:
    statement, info = _read(synth.description_on_its_own_line())
    assert _rows(statement) == list(synth.OWN_LINE_ROWS)
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert statement.ledger_balance is not None
    assert str(statement.ledger_balance.amount) == "1204.55"
    assert info.skipped_lines == 0


def test_card_long_heading_is_read_as_a_card() -> None:
    statement, info = _read(synth.card_long_heading())
    assert info.parser is not None
    assert info.parser.name == "card_statement_generic"
    assert _rows(statement) == list(synth.CARD_ROWS)
    purchases = [t.amount for t in statement.transactions[1:]]
    assert len(purchases) == 4
    assert all(amount < 0 for amount in purchases)
    assert statement.transactions[0].amount > 0  # the payment, printed with its minus
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert info.skipped_lines == 0


# -- tables printed two or three across -------------------------------------------

_CHECKS_ACROSS = [
    ("Number", 40.0),
    ("Date Paid", 100.0),
    ("Amount", 185.0),
    ("Number", 240.0),
    ("Date Paid", 300.0),
    ("Amount", 385.0),
    ("Number", 440.0),
    ("Date Paid", 500.0),
    ("Amount", 585.0),
]


def test_repeated_column_group_is_found() -> None:
    line = _line(_CHECKS_ACROSS)
    assert across_start(line) == 240.0
    # The left group is the header; all from the second group on is one column.
    assert read_header(line) == [
        Column("check", 40.0, 70.0),
        Column("date", 100.0, 145.0),
        Column("amount", 185.0, 215.0),
        Column("across", 240.0, math.inf),
    ]
    # Twice across, and with a heading the vocabulary knows as it is.
    twice = _line(
        [
            ("Check No", 40.0),
            ("Date", 100.0),
            ("Amount", 185.0),
            ("Check No", 300.0),
            ("Date", 360.0),
            ("Amount", 445.0),
        ]
    )
    assert across_start(twice) == 300.0
    assert [c.role for c in read_header(twice) or []] == ["check", "date", "amount", "across"]


def test_a_plain_header_has_no_repeat() -> None:
    plain = _line([("Date", 62.0), ("Description", 150.0), ("Amount", 420.0), ("Balance", 500.0)])
    assert across_start(plain) is None
    assert [c.role for c in read_header(plain) or []] == [
        "date",
        "description",
        "amount",
        "balance",
    ]
    # One group of a checks table: ``Number`` is no heading of its own there.
    assert across_start(_line(_CHECKS_ACROSS[:3])) is None
    # Two dates and one amount: a posting date, not a second table.
    two_dates = _line(
        [("Trans Date", 40.0), ("Post Date", 110.0), ("Description", 180.0), ("Amount", 420.0)]
    )
    assert across_start(two_dates) is None
    assert [c.role for c in read_header(two_dates) or []] == [
        "date",
        "post_date",
        "description",
        "amount",
    ]
    # The groups differ: no repeat.
    uneven = _line(
        [
            ("Date", 40.0),
            ("Description", 100.0),
            ("Amount", 250.0),
            ("Date", 320.0),
            ("Amount", 400.0),
        ]
    )
    assert across_start(uneven) is None
    # A figure on the line: rows, never a header.
    figures = _line([*_CHECKS_ACROSS[:8], ("Amount 12.00", 585.0)])
    assert across_start(figures) is None
    # A word that is no heading, and ``Number`` away from a date heading.
    assert across_start(_line([*_CHECKS_ACROSS, ("Notes", 640.0)])) is None
    apart = _line(
        [
            ("Number", 40.0),
            ("Amount", 100.0),
            ("Date", 185.0),
            ("Number", 240.0),
            ("Amount", 300.0),
            ("Date", 385.0),
        ]
    )
    assert across_start(apart) is None
    assert read_header(apart) is None


def test_date_balance_pairs_are_not_an_across_table() -> None:
    balances = _line([("Date", 62.0), ("Balance", 150.0), ("Date", 300.0), ("Balance", 390.0)])
    assert across_start(balances) is None
    assert read_header(balances) is None
    assert header_like(balances)
    # ``Date | Amount`` three times repeats, and its first group is no header
    # (no description, no check number): a table of daily balances.
    amounts = _line(
        [
            ("Date", 40.0),
            ("Amount", 140.0),
            ("Date", 220.0),
            ("Amount", 300.0),
            ("Date", 336.0),
            ("Amount", 400.0),
        ]
    )
    assert across_start(amounts) == 220.0
    assert read_header(amounts) is None
    assert header_like(amounts)

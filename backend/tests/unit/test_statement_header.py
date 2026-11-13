# SPDX-License-Identifier: AGPL-3.0-or-later
"""Header recognition of the generic statement reader, on hand-built lines.

:mod:`app.ingestion.parsers.statement_header` holds pure functions on a
:class:`ViewLine`, so a header is tested here without a PDF: every line is
built from ``(text, x0)`` cells and nothing but column headings is printed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

import pytest

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers import statement_header, table_statement
from app.ingestion.parsers.base import Cell, ViewLine
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.statement_header import (
    Column,
    Role,
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

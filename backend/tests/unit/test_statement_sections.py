# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sections printed with a title and no header row (``synth_sections``).

A known section title outside any table opens a section whose lines are
read by their own shape (a date, words, an amount at the end). The rows are
believed when the section's printed total is theirs, or, without a total,
as far as the statement's balances check them; a section whose total
disagrees is counted, not read. A statement with no header anywhere is a
statement all the same.

Beside each rule the lines it must leave alone: sentences that start with a
date and end with a figure, balances by day, one dated line under a title,
a dated balance line, lines far below the title, and a printed minus under
a title that says money in.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.base import Cell, DocumentView, ParseOutcome, ViewLine
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.statement_lines import MAX_LED_WORDS
from app.ingestion.parsers.table_statement import (
    HEADERLESS_GAP,
    CardStatementParser,
    TableStatementParser,
)
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, Statement
from tests.fixtures import synth_sections as synth

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


def _warnings(result: ExtractResult) -> list[str]:
    assert result.parse is not None
    return list(result.parse.warnings)


def _status(statement: Statement) -> str:
    assert statement.reconciliation is not None
    return statement.reconciliation.status


# -- whole documents ------------------------------------------------------------


def test_headerless_sections_are_read_and_checked() -> None:
    result = _read(synth.headerless_sections_checking())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["headerless_sections_checking"]
    amounts = [t.amount for t in statement.transactions]
    assert len(amounts) == 11
    assert sum(1 for amount in amounts if amount > 0) == 4
    assert sum(1 for amount in amounts if amount < 0) == 7
    assert _status(statement) == "ok"
    assert _skipped(result) == 0
    assert "sections_without_header:2" in _warnings(result)
    assert not any(w.startswith("rows_without_header") for w in _warnings(result))
    # Three of the withdrawals are on page 2, under the title printed again.
    assert [t.page for t in statement.transactions].count(2) == 3
    # Checked against their totals and by the balances: nothing waits.
    assert all(not t.low_confidence for t in statement.transactions)


def test_a_section_whose_total_disagrees_is_counted_not_read() -> None:
    result = _read(synth.headerless_total_off())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["headerless_total_off"]
    assert all(t.amount < 0 for t in statement.transactions)
    assert _skipped(result) == 3
    assert "rows_without_header:3" in _warnings(result)
    assert "sections_without_header:1" in _warnings(result)
    assert result.parse is not None
    assert len(result.parse.skipped_regions) == 3
    assert _status(statement) != "ok"


def test_a_statement_with_no_header_is_read() -> None:
    result = _read(synth.headerless_card())
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == "card_statement_generic"
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["headerless_card"]
    assert len(statement.transactions) == 6
    assert _status(statement) == "ok"
    assert _skipped(result) == 0
    assert "sections_without_header:2" in _warnings(result)
    # The payment's title is the last line of page 1; its line heads page 2.
    assert statement.transactions[0].page == 2


def test_dated_sentences_under_a_title_are_not_rows() -> None:
    assert all(len(text.split()) > MAX_LED_WORDS for text in synth.NOTICE_SENTENCES)
    result = _read(synth.dated_notices_under_a_title())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["dated_notices_under_a_title"]
    assert not any("charge" in (t.payee or "") for t in statement.transactions)
    assert _skipped(result) == 0
    assert _status(statement) == "ok"
    assert not any("without_header" in w for w in _warnings(result))


def test_balances_by_day_under_a_title_are_not_rows() -> None:
    result = _read(synth.dated_notices_under_a_title())
    statement = _only(result)
    # Five lines of a date and a balance stand under ``Deposits``.
    assert len(statement.transactions) == 5
    assert _rows(statement) == synth.EXPECTED_ROWS["dated_notices_under_a_title"]
    assert _skipped(result) == 0


# -- hand-built lines -----------------------------------------------------------


def _pages(pages: list[list[list[tuple[str, float]]]]) -> DocumentView:
    """One list of lines per page, each a list of ``(text, x0)`` cells, 14 points apart."""
    lines = []
    for number, rows in enumerate(pages, start=1):
        for index, cells in enumerate(rows):
            y = 100.0 + 14.0 * index
            built = tuple(Cell(text, x0, y, x0 + 5.0 * len(text), y + 9.0) for text, x0 in cells)
            lines.append(
                ViewLine(
                    page=number,
                    index=index,
                    cells=built,
                    text=" ".join(t for t, _ in cells),
                    height=9.0,
                )
            )
    return DocumentView(
        lines=tuple(lines), source_format="pdf", truncated=False, page_count=len(pages)
    )


def _parse(*pages: list[list[tuple[str, float]]]) -> ParseOutcome:
    return TableStatementParser().parse(_pages(list(pages)), threshold=0.85)


def _one(outcome: ParseOutcome) -> Statement:
    (statement,) = outcome.statements
    return statement


def _led(day: int, text: str, amount: str) -> list[tuple[str, float]]:
    return [(f"01/{day:02d}/2026", 62.0), (text, 150.0), (amount, 500.0)]


def _amounts(outcome: ParseOutcome) -> list[Decimal]:
    return [t.amount for t in _one(outcome).transactions]


HEADER = [("Date", 62.0), ("Description", 150.0), ("Amount", 500.0)]
ROW = [("01/05/2026", 62.0), ("Coffee", 150.0), ("-4.50", 500.0)]
DEPOSITS = [("Deposits", 62.0)]
WITHDRAWALS = [("Withdrawals", 62.0)]
NOTE = [("Thank you for banking with us", 62.0)]


def test_a_printed_sign_beats_the_title() -> None:
    outcome = _parse(
        [DEPOSITS, _led(2, "Pay", "50.00"), _led(3, "Pay returned", "-12.00"), _led(4, "Gift", "5")]
    )
    # ``5`` is no amount: the line is not a row, and nothing else is made of it.
    assert _amounts(outcome) == [Decimal("50.00"), Decimal("-12.00")]
    outcome = _parse([WITHDRAWALS, _led(2, "Rent", "50.00"), _led(3, "Rent back", "+12.00")])
    assert _amounts(outcome) == [Decimal("-50.00"), Decimal("12.00")]


def test_one_led_line_is_no_section() -> None:
    outcome = _parse([DEPOSITS, _led(2, "Pay", "50.00"), HEADER, ROW])
    assert [t.payee for t in _one(outcome).transactions] == ["Coffee"]
    assert outcome.skipped_lines == 1
    assert outcome.skipped_regions == [(1, (62.0, 114.0, 525.0, 123.0))]
    assert "rows_without_header:1" in outcome.warnings
    assert not any(w.startswith("sections_without_header") for w in outcome.warnings)
    # With no header either, there is no statement at all.
    alone = _parse([DEPOSITS, _led(2, "Pay", "50.00")])
    assert alone.statements == []
    assert alone.skipped_lines == 1
    assert "no_table_header" in alone.warnings


def test_one_led_line_with_its_own_total_is_a_section() -> None:
    lines = [DEPOSITS, _led(2, "Pay", "50.00")]
    outcome = _parse([*lines, [("Total deposits", 62.0), ("50.00", 500.0)]])
    assert _amounts(outcome) == [Decimal("50.00")]
    assert outcome.skipped_lines == 0
    # Another figure is not its total: counted, wherever the line is printed.
    outcome = _parse([DEPOSITS], [_led(2, "Pay", "50.00"), [("Total deposits 75.00", 62.0)]])
    assert outcome.statements == []
    assert outcome.skipped_lines == 1
    assert "rows_without_header:1" in outcome.warnings


def test_rows_of_an_unchecked_section_wait() -> None:
    section = [DEPOSITS, _led(2, "Pay", "50.00"), _led(3, "Gift", "25.00")]
    opening = [("Beginning Balance 100.00", 62.0)]
    # No total under the section, and the balances do not add up.
    outcome = _parse([opening, [("Ending Balance 500.00", 62.0)], *section])
    statement = _one(outcome)
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "mismatch"
    assert len(statement.transactions) == 2
    assert all("amount" in t.low_confidence for t in statement.transactions)
    # Nor when nothing can be checked at all.
    statement = _one(_parse(section))
    assert all("amount" in t.low_confidence for t in statement.transactions)
    # The balances vouch for them when they add up.
    statement = _one(_parse([opening, [("Ending Balance 175.00", 62.0)], *section]))
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert all("amount" not in t.low_confidence for t in statement.transactions)
    # A total that is theirs does not vouch for a statement that does not add up...
    total = [("Total deposits", 62.0), ("75.00", 500.0)]
    statement = _one(_parse([opening, [("Ending Balance 500.00", 62.0)], *section, total]))
    assert all("reconciliation" in t.low_confidence for t in statement.transactions)
    # ...but the amounts were checked against it.
    assert all("amount" not in t.low_confidence for t in statement.transactions)


def test_a_dated_balance_or_total_line_under_a_title_is_no_row() -> None:
    outcome = _parse(
        [
            DEPOSITS,
            _led(2, "Pay", "50.00"),
            _led(3, "Gift", "25.00"),
            _led(31, "Ending Balance", "175.00"),
            _led(31, "Total fees this period", "3.00"),
        ]
    )
    assert _amounts(outcome) == [Decimal("50.00"), Decimal("25.00")]
    assert outcome.skipped_lines == 0
    statement = _one(outcome)
    assert statement.ledger_balance is not None
    assert statement.ledger_balance.amount == Decimal("175.00")


def test_a_line_with_a_balance_beside_its_amount_is_counted() -> None:
    both = [("01/04/2026", 62.0), ("Wire in", 150.0), ("30.00", 430.0), ("205.00", 500.0)]
    outcome = _parse([DEPOSITS, _led(2, "Pay", "50.00"), _led(3, "Gift", "25.00"), both])
    assert _amounts(outcome) == [Decimal("50.00"), Decimal("25.00")]
    assert outcome.skipped_lines == 1
    assert "rows_without_header:1" in outcome.warnings


def test_a_section_ends_after_lines_that_are_no_rows() -> None:
    notes = [NOTE] * HEADERLESS_GAP
    late = [_led(8, "Rate set to", "1.25"), _led(9, "Rate set to", "1.50")]
    outcome = _parse([DEPOSITS, _led(2, "Pay", "50.00"), _led(3, "Gift", "25.00"), *notes, *late])
    assert _amounts(outcome) == [Decimal("50.00"), Decimal("25.00")]
    assert outcome.skipped_lines == 0
    # One line fewer between them, and the two are lines of the section.
    outcome = _parse(
        [DEPOSITS, _led(2, "Pay", "50.00"), _led(3, "Gift", "25.00"), *notes[1:], *late]
    )
    assert len(_one(outcome).transactions) == 4


def test_a_section_runs_over_a_page_break_and_under_its_title_again() -> None:
    first = [DEPOSITS, _led(2, "Pay", "50.00"), [("Page 1 of 2", 62.0)]]
    total = [("Total deposits", 62.0), ("90.00", 500.0)]
    for again in ([], [[("Deposits (continued)", 62.0)]], [DEPOSITS]):
        outcome = _parse(
            first, [NOTE, *again, _led(3, "Gift", "25.00"), _led(4, "Tip", "15.00"), total]
        )
        assert _amounts(outcome) == [Decimal("50.00"), Decimal("25.00"), Decimal("15.00")]
        assert "sections_without_header:1" in outcome.warnings
        assert outcome.skipped_lines == 0
    # Another title ends it: two sections, each with its own direction.
    outcome = _parse(first, [WITHDRAWALS, _led(3, "Rent", "25.00"), _led(4, "Gas", "15.00")])
    assert _one(outcome).transactions[0].amount == Decimal("-25.00")
    assert outcome.skipped_lines == 1


def test_a_line_of_words_under_a_row_is_its_memo() -> None:
    outcome = _parse(
        [
            DEPOSITS,
            _led(2, "Pay", "50.00"),
            [("Ref 7741 branch counter", 150.0)],
            _led(3, "Gift", "25.00"),
            [("Total deposits", 62.0), ("75.00", 500.0)],
        ]
    )
    first, second = _one(outcome).transactions
    assert (first.payee, first.memo) == ("Pay", "Ref 7741 branch counter")
    assert first.bbox is not None
    assert first.bbox[3] == pytest.approx(137.0)
    assert second.memo is None


def test_a_card_section_takes_the_card_s_direction() -> None:
    view = _pages(
        [
            [
                [("Previous Balance 100.00", 62.0)],
                [("New Balance 125.00", 62.0)],
                [("Payments and Other Credits", 62.0)],
                _led(2, "Payment", "-20.00"),
                [("Total payments and other credits for this period -20.00", 62.0)],
                [("Purchases and Adjustments", 62.0)],
                _led(3, "Books", "30.00"),
                _led(4, "Tea", "15.00"),
            ]
        ]
    )
    outcome = CardStatementParser().parse(view, threshold=0.85)
    statement = _one(outcome)
    assert [t.amount for t in statement.transactions] == [
        Decimal("20.00"),
        Decimal("-30.00"),
        Decimal("-15.00"),
    ]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


def test_a_title_with_three_led_rows_is_a_table() -> None:
    parser = TableStatementParser()
    rows = [_led(2, "Pay", "50.00"), _led(3, "Gift", "25.00"), _led(4, "Tip", "15.00")]
    assert parser.detect(_pages([[DEPOSITS, *rows]])) == 0.4
    assert parser.detect(_pages([[DEPOSITS, *rows[:2]]])) == 0.0
    # No title, a title of something else, or figures with no words: no table.
    assert parser.detect(_pages([rows])) == 0.0
    assert parser.detect(_pages([[[("Rate changes", 62.0)], *rows]])) == 0.0
    assert parser.detect(_pages([[[("Account Summary", 62.0)], *rows]])) == 0.0
    by_day = [[(f"01/0{day}/2026", 62.0), ("405.50", 500.0)] for day in (2, 3, 4)]
    assert parser.detect(_pages([[DEPOSITS, *by_day]])) == 0.0
    # The rows may head the next page, or follow the title printed again there.
    assert parser.detect(_pages([[DEPOSITS], rows])) == 0.4
    assert parser.detect(_pages([[DEPOSITS, rows[0]], [DEPOSITS, *rows[1:]]])) == 0.4
    assert parser.detect(_pages([[DEPOSITS], [NOTE], rows])) == 0.0


def test_a_dated_balance_line_under_an_unread_header_is_not_counted() -> None:
    # A summary grid printed under the table: its header ends the table, and
    # its dated balance lines are lines of the summary, not rows left unread.
    grid = [("Date", 62.0), ("Balance", 500.0)]
    outcome = _parse(
        [
            HEADER,
            ROW,
            grid,
            [("01/01/2026", 62.0), ("Beginning Balance", 150.0), ("410.00", 500.0)],
            [("01/31/2026", 62.0), ("Ending Balance", 150.0), ("405.50", 500.0)],
        ]
    )
    assert [t.payee for t in _one(outcome).transactions] == ["Coffee"]
    assert outcome.skipped_lines == 0
    assert not any(w.startswith("rows_without_header") for w in outcome.warnings)
    # A payee that starts like a label is still a line found and not read.
    outcome = _parse(
        [HEADER, ROW, grid, [("01/06/2026", 62.0), ("New Balance Outlet", 150.0), ("9.00", 500.0)]]
    )
    assert outcome.skipped_lines == 1

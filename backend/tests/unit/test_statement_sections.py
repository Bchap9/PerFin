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

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.base import Cell, DocumentView, ParseOutcome, ViewLine
from app.ingestion.parsers.registry import PARSERS, parse_extraction
from app.ingestion.parsers.statement_lines import MAX_LED_WORDS
from app.ingestion.parsers.table_statement import (
    HEADERLESS_GAP,
    CardStatementParser,
    TableStatementParser,
)
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, Statement
from tests.fixtures import synth_bank_layouts
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


# -- tables printed across the page; a second listing ----------------------------


def test_a_second_listing_of_checks_is_not_read_twice() -> None:
    result = _read(synth.checks_listed_twice())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["checks_listed_twice"]
    assert len(statement.transactions) == 12
    assert _status(statement) == "ok"
    assert "checks_listed_twice" in _warnings(result)
    assert _skipped(result) == 0
    assert not any(w.startswith("table_across_unread") for w in _warnings(result))
    assert not any((t.payee or "").startswith("CHECK ") for t in statement.transactions)


def test_checks_only_in_their_table_are_rows() -> None:
    result = _read(synth.checks_only_across())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["checks_only_across"]
    assert len(statement.transactions) == 12
    assert [t.payee for t in statement.transactions[8:]] == [
        payee for _, _, payee in synth.LISTED_CHECKS
    ]
    assert all(t.amount < 0 for t in statement.transactions[8:])
    assert _status(statement) == "ok"
    assert _skipped(result) == 0
    assert "checks_listed_twice" not in _warnings(result)
    assert not any(w.startswith("table_across_unread") for w in _warnings(result))


CHECKS_HEADER = [
    ("Number", 62.0),
    ("Date Paid", 110.0),
    ("Amount", 200.0),
    ("Number", 260.0),
    ("Date Paid", 310.0),
    ("Amount", 400.0),
]
SUMMARY_OF_CHECKS = [("Summary of checks", 62.0)]


def _check_pair(
    first: tuple[str, int, str], second: tuple[str, int, str] | None = None
) -> list[tuple[str, float]]:
    """One line of a checks table two across: ``(number, day, amount)`` per group."""
    cells: list[tuple[str, float]] = []
    for check, (number_x, date_x, amount_x) in zip(
        (first, second), ((62.0, 110.0, 200.0), (260.0, 310.0, 400.0)), strict=True
    ):
        if check is None:
            continue
        number, day, amount = check
        if number:
            cells.append((number, number_x))
        cells += [(f"01/{day:02d}/2026", date_x), (amount, amount_x)]
    return cells


def _listing(closing: str, *main: list[tuple[str, float]]) -> ParseOutcome:
    """A signed table of ``main`` rows, then four checks listed two across."""
    return _parse(
        [
            [("Beginning Balance 1,000.00", 62.0)],
            [(f"Ending Balance {closing}", 62.0)],
            HEADER,
            *main,
            SUMMARY_OF_CHECKS,
            CHECKS_HEADER,
            _check_pair(("3101", 5, "20.00"), ("3102", 6, "31.00")),
            _check_pair(("3103", 7, "7.50"), ("3104", 8, "12.25")),
        ]
    )


_MAIN_CHECKS = [
    _led(5, "Check 3101", "-20.00"),
    _led(6, "Check 3102", "-31.00"),
    _led(7, "Check 3103", "-7.50"),
    _led(8, "Check 3104", "-12.25"),
]


def test_one_unmatched_check_keeps_the_whole_table() -> None:
    coffee = _led(4, "Coffee", "-4.50")
    # Three of the four listed checks are rows of the first table: the
    # fourth is nowhere else, so the listing is no second listing.
    outcome = _listing("924.75", coffee, *_MAIN_CHECKS[:3])
    statement = _one(outcome)
    assert len(statement.transactions) == 4 + 4
    assert [t.payee for t in statement.transactions[4:]] == [
        "CHECK 3101",
        "CHECK 3102",
        "CHECK 3103",
        "CHECK 3104",
    ]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "mismatch"
    assert "checks_listed_twice" not in outcome.warnings
    assert outcome.skipped_lines == 0
    # All four are there: listed twice, read once, nothing counted.
    outcome = _listing("924.75", coffee, *_MAIN_CHECKS)
    statement = _one(outcome)
    assert [t.payee for t in statement.transactions] == [
        "Coffee",
        "Check 3101",
        "Check 3102",
        "Check 3103",
        "Check 3104",
    ]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert "checks_listed_twice" in outcome.warnings
    assert outcome.skipped_lines == 0
    assert not any(w.startswith("table_across_unread") for w in outcome.warnings)


def test_a_partner_vouches_for_one_listed_check_only() -> None:
    # Two listed checks of one day and amount, one such row in the first
    # table: the second of them has no partner, and the listing stays.
    outcome = _parse(
        [
            HEADER,
            _led(5, "Check 3101", "-20.00"),
            SUMMARY_OF_CHECKS,
            CHECKS_HEADER,
            _check_pair(("3101", 5, "20.00"), ("3102", 5, "20.00")),
        ]
    )
    assert len(_one(outcome).transactions) == 3
    assert "checks_listed_twice" not in outcome.warnings
    # Another day is no partner either.
    outcome = _parse(
        [
            HEADER,
            _led(5, "Check 3101", "-20.00"),
            SUMMARY_OF_CHECKS,
            CHECKS_HEADER,
            _check_pair(("3101", 6, "20.00")),
        ]
    )
    assert len(_one(outcome).transactions) == 2
    assert "checks_listed_twice" not in outcome.warnings


def test_a_group_that_reads_no_row_is_counted() -> None:
    outcome = _parse(
        [
            CHECKS_HEADER,
            _check_pair(("3101", 5, "20.00"), ("3102", 6, "31.00")),
            # The second group prints a date and no figure: found, not read.
            [("3103", 62.0), ("01/07/2026", 110.0), ("7.50", 200.0), ("01/08/2026", 310.0)],
        ]
    )
    assert [(t.payee, t.amount) for t in _one(outcome).transactions] == [
        ("CHECK 3101", Decimal("-20.00")),
        ("CHECK 3102", Decimal("-31.00")),
        ("CHECK 3103", Decimal("-7.50")),
    ]
    assert outcome.skipped_lines == 1
    assert outcome.warnings.count("table_across_unread:1") == 1


# -- titles printed with their totals ---------------------------------------------


def test_title_with_its_total_gives_direction_and_a_check() -> None:
    result = _read(synth.titles_with_totals())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["titles_with_totals"]
    amounts = [t.amount for t in statement.transactions]
    assert sum(1 for amount in amounts if amount > 0) == 3
    assert sum(1 for amount in amounts if amount < 0) == 9
    assert _status(statement) == "ok"
    assert _skipped(result) == 0
    assert "section_total_mismatch" not in _warnings(result)
    assert not any(w.startswith("table_across_unread") for w in _warnings(result))
    assert "checks_listed_twice" not in _warnings(result)
    # With one withdrawal off the page the title's total is not its rows'.
    result = _read(synth.titles_with_totals_one_missing())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["titles_with_totals_one_missing"]
    assert "section_total_mismatch" in _warnings(result)
    assert _status(statement) == "mismatch"


def _titled(title: str, total: str) -> list[tuple[str, float]]:
    return [(title, 62.0), (total, 300.0)]


def test_a_printed_sign_beats_a_title_with_a_total() -> None:
    outcome = _parse(
        [
            _titled("Deposits / Credits", "2 items totaling $38.00"),
            HEADER,
            _led(2, "Pay", "50.00"),
            _led(3, "Pay returned", "-12.00"),
        ]
    )
    assert _amounts(outcome) == [Decimal("50.00"), Decimal("-12.00")]
    assert "section_total_mismatch" not in outcome.warnings
    outcome = _parse(
        [
            _titled("Withdrawals / Debits", "2 items totaling $38.00"),
            HEADER,
            _led(2, "Rent", "50.00"),
            _led(3, "Rent back", "+12.00"),
        ]
    )
    assert _amounts(outcome) == [Decimal("-50.00"), Decimal("12.00")]
    assert "section_total_mismatch" not in outcome.warnings
    # The title's total is checked: another figure is not these rows' total.
    outcome = _parse(
        [
            _titled("Withdrawals / Debits", "2 items totaling $99.00"),
            HEADER,
            _led(2, "Rent", "50.00"),
            _led(3, "Rent back", "+12.00"),
        ]
    )
    assert "section_total_mismatch" in outcome.warnings


def test_a_title_s_total_is_kept_when_a_total_line_follows() -> None:
    outcome = _parse(
        [
            _titled("Deposits / Credits", "2 items totaling $75.00"),
            HEADER,
            _led(2, "Pay", "50.00"),
            _led(3, "Gift", "25.00"),
            # Not the section's rows: the title's figure is the one checked.
            [("Total deposits year to now", 62.0), ("900.00", 500.0)],
        ]
    )
    assert _amounts(outcome) == [Decimal("50.00"), Decimal("25.00")]
    assert "section_total_mismatch" not in outcome.warnings
    assert outcome.skipped_lines == 0


def test_a_check_without_its_number_is_a_check() -> None:
    checks = [("Check No.", 62.0), ("Date", 150.0), ("Amount", 500.0)]
    outcome = _parse(
        [
            checks,
            [("3101", 62.0), ("01/05/2026", 150.0), ("20.00", 500.0)],
            [("01/06/2026", 150.0), ("7.50", 500.0)],
        ]
    )
    assert [(t.payee, t.amount) for t in _one(outcome).transactions] == [
        ("CHECK 3101", Decimal("-20.00")),
        ("CHECK", Decimal("-7.50")),
    ]
    assert outcome.skipped_lines == 0
    # Across the page too, in every group.
    outcome = _parse(
        [
            CHECKS_HEADER,
            _check_pair(("", 5, "20.00"), ("", 6, "31.00")),
        ]
    )
    assert [(t.payee, t.amount) for t in _one(outcome).transactions] == [
        ("CHECK", Decimal("-20.00")),
        ("CHECK", Decimal("-31.00")),
    ]
    # A table that names more than checks: a row with no number is as printed.
    wide = [("Date", 62.0), ("Check No.", 130.0), ("Description", 220.0), ("Amount", 500.0)]
    outcome = _parse(
        [
            wide,
            [("01/05/2026", 62.0), ("Pay", 220.0), ("20.00", 500.0)],
            [("01/06/2026", 62.0), ("20.00", 500.0)],
        ]
    )
    assert [(t.payee, t.amount) for t in _one(outcome).transactions] == [
        ("Pay", Decimal("20.00")),
        (None, Decimal("20.00")),
    ]


def test_a_summary_line_with_a_figure_is_no_title() -> None:
    summary = [("Deposits and additions", 62.0), ("900.00", 500.0)]
    # In a summary box three lines over the header: not this table's title.
    outcome = _parse(
        [[("Withdrawals", 62.0), ("75.00", 500.0)], NOTE, NOTE, HEADER, _led(2, "Pay", "50.00")]
    )
    assert _amounts(outcome) == [Decimal("50.00")]
    assert "section_total_mismatch" not in outcome.warnings
    # Printed as one cell it is a sentence with a figure, wherever it stands.
    outcome = _parse([[("Withdrawals 75.00", 62.0)], HEADER, _led(2, "Pay", "50.00")])
    assert _amounts(outcome) == [Decimal("50.00")]
    assert "section_total_mismatch" not in outcome.warnings
    # Nor a title with a bare figure right over the header: nothing beside
    # the figure says it is the total of the rows under it.
    outcome = _parse([[("Withdrawals", 62.0), ("75.00", 500.0)], HEADER, _led(2, "Pay", "50.00")])
    assert _amounts(outcome) == [Decimal("50.00")]
    assert "section_total_mismatch" not in outcome.warnings
    # A label that is no section title has no say, with a figure or without.
    outcome = _parse([[("Ending balance", 62.0), ("75.00", 500.0)], HEADER, ROW])
    assert _amounts(outcome) == [Decimal("-4.50")]
    assert "section_total_mismatch" not in outcome.warnings
    # Under a table, a title with a figure over the next header starts no
    # row of the table above, whatever column its figure stands under.
    outcome = _parse(
        [
            HEADER,
            _led(2, "Pay", "50.00"),
            _led(3, "Gift", "25.00"),
            summary,
            NOTE,
            NOTE,
            HEADER,
            _led(4, "Tip", "15.00"),
        ]
    )
    assert _amounts(outcome) == [Decimal("50.00"), Decimal("25.00"), Decimal("15.00")]


def test_items_totaling_is_the_sections_total() -> None:
    rows = [_led(2, "Pay", "50.00"), _led(3, "Gift", "25.00")]
    for cells in (
        [("2 items totaling $75.00", 150.0)],
        [("2 item(s) totaling", 150.0), ("$75.00", 500.0)],
    ):
        outcome = _parse([DEPOSITS, HEADER, *rows, cells])
        assert _amounts(outcome) == [Decimal("50.00"), Decimal("25.00")]
        assert outcome.skipped_lines == 0
        assert "section_total_mismatch" not in outcome.warnings
    outcome = _parse([DEPOSITS, HEADER, *rows, [("2 items totaling", 150.0), ("$80.00", 500.0)]])
    assert _amounts(outcome) == [Decimal("50.00"), Decimal("25.00")]
    assert "section_total_mismatch" in outcome.warnings
    # A sentence that says as much is no total.
    outcome = _parse(
        [DEPOSITS, HEADER, *rows, [("We counted 2 items totaling $80.00 for you", 150.0)]]
    )
    assert "section_total_mismatch" not in outcome.warnings


def test_a_title_with_its_total_is_no_row_of_the_table_above() -> None:
    # The dates are printed once per day, so an undated line with words and
    # one figure under ``Amount`` is a row of the day above it. The title
    # of the next table, its total under ``Amount``, is none.
    outcome = _parse(
        [
            HEADER,
            _led(2, "Pay", "50.00"),
            [("Gift", 150.0), ("25.00", 500.0)],
            [("Tip", 150.0), ("15.00", 500.0)],
            _led(3, "Refund", "10.00"),
            [("Withdrawals / Debits", 150.0), ("1 items totaling", 300.0), ("$30.00", 500.0)],
            HEADER,
            _led(4, "Rent", "30.00"),
        ]
    )
    assert _amounts(outcome) == [
        Decimal("50.00"),
        Decimal("25.00"),
        Decimal("15.00"),
        Decimal("10.00"),
        Decimal("-30.00"),
    ]
    assert outcome.skipped_lines == 0
    assert "section_total_mismatch" not in outcome.warnings


# -- two accounts told apart by their summaries ----------------------------------


def _balances(statement: Statement) -> tuple[Decimal | None, Decimal | None]:
    assert statement.reconciliation is not None
    assert statement.ledger_balance is not None
    return statement.reconciliation.opening, statement.ledger_balance.amount


def test_two_accounts_by_their_summaries() -> None:
    result = _read(synth.two_accounts_one_number())
    assert len(result.statements) == 2, [len(s.transactions) for s in result.statements]
    assert [len(s.transactions) for s in result.statements] == [9, 3]
    for statement, (opening, closing, rows) in zip(
        result.statements, synth.EXPECTED_ACCOUNTS, strict=True
    ):
        assert _rows(statement) == rows
        assert _balances(statement) == (opening, closing)
        assert _status(statement) == "ok"
    warnings = _warnings(result)
    assert "accounts_by_summary" in warnings
    assert "accounts_by_balance" not in warnings
    assert "rows_outside_account" not in warnings
    assert _skipped(result) == 0
    # Named by their summaries; the one masked number tells neither apart.
    assert [(s.account.name, s.account.last4) for s in result.statements] == [
        ("CHECKING", None),
        ("SAVINGS", None),
    ]
    assert [s.account.account_type for s in result.statements] == ["checking", "savings"]
    assert [(s.first_page, s.last_page) for s in result.statements] == [(1, 2), (2, 2)]


def test_a_summary_repeated_on_page_two_is_one_account() -> None:
    result = _read(synth.summary_printed_twice())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["summary_printed_twice"]
    assert _status(statement) == "ok"
    assert _skipped(result) == 0
    assert "accounts_by_summary" not in _warnings(result)


BALANCED = [("Date", 62.0), ("Description", 150.0), ("Amount", 400.0), ("Balance", 500.0)]


def _summary(title: str | None, opening: str, closing: str) -> list[list[tuple[str, float]]]:
    """A summary block: its title (when given) and its two balance lines."""
    head = [[(title, 62.0)]] if title is not None else []
    return [
        *head,
        [("Beginning Balance", 62.0), (opening, 300.0)],
        [("Ending Balance", 62.0), (closing, 300.0)],
    ]


def _dated(day: int, text: str, amount: str) -> list[tuple[str, float]]:
    return [(f"01/{day:02d}/2026", 62.0), (text, 150.0), (amount, 500.0)]


CHECKING_PAGE = [
    *_summary("CHECKING SUMMARY", "100.00", "130.00"),
    HEADER,
    _dated(5, "Deposit", "50.00"),
    _dated(9, "Coffee", "-20.00"),
]
SAVINGS_PAGE = [
    *_summary("SAVINGS SUMMARY", "900.00", "950.00"),
    HEADER,
    _dated(12, "Deposit", "60.00"),
    _dated(20, "Fee", "-10.00"),
]


def _statuses(outcome: ParseOutcome) -> list[str]:
    return [_status(statement) for statement in outcome.statements]


def test_hand_built_summaries_are_two_accounts() -> None:
    outcome = _parse(CHECKING_PAGE, SAVINGS_PAGE)
    assert [len(s.transactions) for s in outcome.statements] == [2, 2]
    assert _statuses(outcome) == ["ok", "ok"]
    assert "accounts_by_summary" in outcome.warnings
    assert [s.account.name for s in outcome.statements] == ["CHECKING", "SAVINGS"]


def test_the_first_summary_printed_a_third_time_is_no_third_account() -> None:
    # Checking, savings, then the checking summary once more with no table.
    again = _summary("CHECKING SUMMARY", "100.00", "130.00")
    outcome = _parse(CHECKING_PAGE, SAVINGS_PAGE, again)
    assert [len(s.transactions) for s in outcome.statements] == [2, 2]
    assert _statuses(outcome) == ["ok", "ok"]
    assert "accounts_by_summary" in outcome.warnings
    assert "no_table_header" not in outcome.warnings
    assert "account_without_tables" not in outcome.warnings


def test_a_summary_with_no_table_is_no_statement() -> None:
    # A third account prints its balances and no transactions.
    third = _summary("MONEY MARKET SUMMARY", "5,000.00", "5,004.00")
    outcome = _parse(CHECKING_PAGE, SAVINGS_PAGE, third)
    assert [len(s.transactions) for s in outcome.statements] == [2, 2]
    assert _statuses(outcome) == ["ok", "ok"]
    assert "account_without_tables" in outcome.warnings


def test_one_summary_with_a_table_is_one_statement() -> None:
    # Two summaries, one table: nothing says the rows are the first's only.
    outcome = _parse(CHECKING_PAGE, _summary("SAVINGS SUMMARY", "900.00", "950.00"))
    assert len(outcome.statements) == 1
    assert "accounts_by_summary" not in outcome.warnings


def test_a_balance_carried_over_the_page_is_one_account() -> None:
    # The figure carried forward at the foot of page 1 is brought forward
    # above the header of page 2: the same account goes on.
    first = [
        *_summary("CHECKING SUMMARY", "100.00", "190.00"),
        HEADER,
        _dated(5, "Deposit", "50.00"),
        _dated(9, "Coffee", "-20.00"),
        [("Balance Carried Forward", 62.0), ("130.00", 500.0)],
    ]
    second = [
        [("Balance Brought Forward", 62.0), ("130.00", 500.0)],
        HEADER,
        _dated(12, "Deposit", "60.00"),
    ]
    outcome = _parse(first, second)
    assert len(outcome.statements) == 1
    assert len(_one(outcome).transactions) == 3
    assert "accounts_by_summary" not in outcome.warnings


def test_balance_lines_of_a_table_start_no_account() -> None:
    # Undated balance lines inside a table with a running balance, the
    # table's header printed again on page 2: one account.
    first = [
        *_summary("CHECKING SUMMARY", "100.00", "190.00"),
        BALANCED,
        [("01/05/2026", 62.0), ("Deposit", 150.0), ("50.00", 400.0), ("150.00", 500.0)],
        [("Ending Balance", 150.0), ("150.00", 500.0)],
    ]
    second = [
        BALANCED,
        [("Beginning Balance", 150.0), ("150.00", 500.0)],
        [("01/12/2026", 62.0), ("Deposit", 150.0), ("40.00", 400.0), ("190.00", 500.0)],
    ]
    outcome = _parse(first, second)
    assert len(outcome.statements) == 1
    assert "accounts_by_summary" not in outcome.warnings


def test_a_sentence_over_a_summary_names_no_account() -> None:
    # The line above the second summary is a footer sentence, not a title.
    second = [NOTE, *SAVINGS_PAGE[1:]]
    outcome = _parse(CHECKING_PAGE, second)
    assert _statuses(outcome) == ["ok", "ok"]
    assert [s.account.name for s in outcome.statements] == ["CHECKING", None]
    assert [s.account.account_type for s in outcome.statements] == ["checking", None]


# -- what the first page's summary names: a card, a loan bill ---------------------


def _parser(result: ExtractResult) -> str | None:
    assert result.parse is not None
    return result.parse.parser.name if result.parse.parser is not None else None


def _scores(result: ExtractResult) -> dict[str, float]:
    assert result.parse is not None
    return {score.name: score.score for score in result.parse.scores}


def test_card_without_a_card_word_is_read_as_a_card() -> None:
    result = _read(synth.card_named_by_its_summary())
    assert _parser(result) == "card_statement_generic"
    statement = _only(result)
    assert statement.account.account_type == "creditcard"
    assert _rows(statement) == synth.EXPECTED_ROWS["card_named_by_its_summary"]
    amounts = [t.amount for t in statement.transactions]
    # Five purchases are money out; the payment, printed with a minus, is money in.
    assert sum(1 for amount in amounts if amount < 0) == 5
    assert [amount for amount in amounts if amount > 0] == [Decimal("300.00")]
    assert _status(statement) == "ok"
    assert statement.ledger_balance is not None
    assert abs(statement.ledger_balance.amount) == synth.EQUATION_NEW_BALANCE
    assert _skipped(result) == 0


@pytest.mark.parametrize(
    "builder",
    [
        synth_bank_layouts.sectioned_unsigned_checking,
        synth_bank_layouts.checking_with_card_note,
        synth.headerless_sections_checking,
    ],
)
def test_checking_summary_is_no_card(builder: Callable[[], bytes]) -> None:
    result = _read(builder())
    assert _parser(result) == "statement_generic"
    assert _scores(result)["card_statement_generic"] == 0.0
    assert _only(result).account.account_type != "creditcard"


def test_loan_bill_is_declined_by_the_statement_parsers() -> None:
    result = _read(synth.loan_bill_with_activity())
    scores = _scores(result)
    assert scores["statement_generic"] == 0.0
    assert scores["card_statement_generic"] == 0.0
    assert result.statements == []
    assert _parser(result) not in {"statement_generic", "card_statement_generic"}


def test_member_statement_with_a_loan_share_is_still_read() -> None:
    result = _read(synth.member_statement_with_a_loan_share())
    assert _parser(result) == "statement_generic"
    assert [(s.account.name, s.account.last4) for s in result.statements] == [
        ("PRIMARY SHARE", "0000"),
        ("SHARE DRAFT", "0040"),
        ("HOLIDAY CLUB", "0050"),
    ]
    assert [len(s.transactions) for s in result.statements] == [2, 6, 1]
    for statement, (_, _, _, closing, rows) in zip(
        result.statements, synth_bank_layouts.EXPECTED_MEMBER, strict=True
    ):
        assert _rows(statement) == rows
        assert _status(statement) == "ok"
        assert statement.ledger_balance is not None
        assert statement.ledger_balance.amount == closing
    assert _skipped(result) == 0


LOAN_SHARE = [[("Principal Balance 8,412.60", 62.0)], [("Amount Due 265.00", 62.0)]]


def _generic_scores(view: DocumentView) -> tuple[float, float]:
    return TableStatementParser().detect(view), CardStatementParser().detect(view)


def test_a_loan_bill_s_summary_stops_both_generic_parsers() -> None:
    view = _pages([[*LOAN_SHARE, HEADER, ROW]])
    assert _generic_scores(view) == (0.0, 0.0)
    # The same table without the bill's labels is a statement's.
    assert TableStatementParser().detect(_pages([[HEADER, ROW]])) > 0
    # Either label alone names no bill.
    for line in LOAN_SHARE:
        assert TableStatementParser().detect(_pages([[line, HEADER, ROW]])) > 0
    # A parser written for one institution keeps its own fingerprint's word.
    named = next(p for p in PARSERS if isinstance(p, TableStatementParser) and p.fingerprints)
    lines = [[(phrase, 62.0)] for phrase in named.fingerprints]
    assert named.detect(_pages([[*lines, *LOAN_SHARE, HEADER, ROW]])) == 1.0


@pytest.mark.parametrize(
    "line",
    [
        "Beginning Balance 1,250.00",
        "Share Savings Ending Balance 1,450.52",
        "Total Deposits 200.52",
        "2 Deposits 200.52",
    ],
)
def test_a_deposit_summary_beside_a_loan_is_no_loan_bill(line: str) -> None:
    view = _pages([[[(line, 62.0)], *LOAN_SHARE, HEADER, ROW]])
    assert TableStatementParser().detect(view) > 0


def test_a_dated_balance_line_of_a_table_beside_a_loan_is_no_loan_bill() -> None:
    table = [
        HEADER,
        [("01/01/2026", 62.0), ("Beginning Balance", 150.0), ("100.00", 500.0)],
        ROW,
        [("Jan 31", 62.0), ("Ending Balance", 150.0), ("95.50", 500.0)],
    ]
    assert TableStatementParser().detect(_pages([[*LOAN_SHARE, *table]])) > 0
    # The label has to lead the line's words: a payee that holds it does not.
    payee = [("01/09/2026", 62.0), ("Refund of ending balance fee", 150.0), ("2.00", 500.0)]
    assert TableStatementParser().detect(_pages([[*LOAN_SHARE, HEADER, ROW, payee]])) == 0.0


def test_a_loan_s_labels_after_page_one_stop_nothing() -> None:
    view = _pages([[HEADER, ROW], LOAN_SHARE])
    assert TableStatementParser().detect(view) > 0

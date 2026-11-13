# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bank statement rows as real statements print them (``synth_bank_layouts``).

The generic reader on rows whose cells sit a little off their columns:
figures right-aligned away from a left-aligned heading, a date printed so
close to the description that the two are one cell, a row that is one cell
altogether, and a zero printed beside the amount in the other money column.
Each rule has the line it must leave alone beside it: nothing here may turn
a line that is no transaction into one.

And balances: a dated ``Beginning Balance`` or ``Ending Balance`` line is a
balance whatever column its figure is in, never a transaction; a label takes
the figure printed alone on the line next to it; ``Previous Statement
Balance`` opens the statement, it does not close it.

And which reader reads a statement: card words in a sentence do not make a
checking statement a card statement, and a parser written for one
institution claims nothing on one of its two phrases.

And the direction of amounts printed without a sign: the title of their
section gives it (``Deposits and additions``, ``Electronic withdrawals``,
``Payments and credits`` on a card), across a page break under the header
printed again; a printed sign, ``CR`` or a debit or credit column always
says more than a title, and a table without a title has no direction.

And lines that used to vanish: a row under a date printed once per day takes
the date above it; a line with a figure that cannot be read between two
rows, a row above the first share and the date-led lines of a titled section
without a header are counted. Summary lines under an ordinary table stay
what they were: no rows, and nothing counted.

And totals and order: the total a section prints is compared with the rows
read under it (a section that does not add up holds the statement and names
its rows; it never vouches for one), totals of the year are no section's
total, and a statement printed newest first is read oldest first, so its
running balances agree.

And the period, and rows that print no year: more labels (``Statement
For``), the classifier's period, a range printed once on page 1, and, when
nothing says when the statement is from, a year assumed from the day the
file was made or arrived (:func:`assume_years`): the rows are kept, each
waiting on its date, never split around a date printed in a notice.

And tables printed two or three across the page: the left group of a checks
table is read and every check beside it is counted; a table of daily
balances printed across is neither read nor counted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.base import Cell, DocumentView, ParseOutcome, ViewLine
from app.ingestion.parsers.institutions import MockSavingsParser
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.table_statement import (
    SECTION_TITLES,
    CardStatementParser,
    SectionRule,
    TableStatementParser,
    assume_years,
)
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, Statement
from tests.fixtures import synth_bank_layouts as synth
from tests.fixtures import synth_card_statement, synth_pdf_statements, synth_unseen_layouts

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


# -- balances -------------------------------------------------------------------


def _balances(statement: Statement) -> tuple[Decimal | None, Decimal | None]:
    opening, closing = statement.opening_balance, statement.ledger_balance
    return (opening.amount if opening else None, closing.amount if closing else None)


def test_balance_lines_in_a_money_column_are_not_rows() -> None:
    result = _read(synth.summary_table_balances())
    statement = _only(result)
    assert len(statement.transactions) == 6
    assert not any("Balance" in (t.payee or "") for t in statement.transactions)
    assert _rows(statement) == synth.EXPECTED_ROWS["summary_table_balances"]
    assert _balances(statement) == synth.EXPECTED_BALANCES["summary_table_balances"]
    assert _status(statement) == "ok"
    assert _skipped(result) == 0


def test_label_and_figure_on_neighbouring_lines() -> None:
    result = _read(synth.summary_label_wrapped())
    statement = _only(result)
    assert _balances(statement) == synth.EXPECTED_BALANCES["summary_label_wrapped"]
    assert _rows(statement) == synth.EXPECTED_ROWS["summary_label_wrapped"]
    assert _status(statement) == "ok"
    # Read beside its label, not after it: believed a little less.
    assert statement.confidence["opening_balance"] == 0.9
    assert statement.confidence["closing_balance"] == 0.95


def test_previous_statement_balance_is_the_opening_balance() -> None:
    result = _read(synth.previous_statement_balance_first())
    statement = _only(result)
    assert statement.parser is not None
    assert statement.parser.name == "statement_generic"
    assert _balances(statement) == synth.EXPECTED_BALANCES["previous_statement_balance_first"]
    assert _rows(statement) == synth.EXPECTED_ROWS["previous_statement_balance_first"]
    assert _status(statement) == "ok"


def test_shares_with_opening_rows_in_the_amount_column_are_two_accounts() -> None:
    result = _read(synth.shares_opening_under_deposit())
    assert result.parse is not None
    assert "accounts_by_balance" in result.parse.warnings
    assert len(result.statements) == 2
    for statement, (last4, opening, closing, rows) in zip(
        result.statements, synth.EXPECTED_SHARES, strict=True
    ):
        assert statement.account.last4 == last4
        assert _balances(statement) == (opening, closing)
        assert _rows(statement) == rows
        assert _status(statement) == "ok"
    assert _skipped(result) == 0


# -- the right reader -----------------------------------------------------------


def _unhinted(data: bytes) -> ExtractResult:
    """Read with no word from the classifier: the parsers' own scores decide."""
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return parse_extraction(result)


def _parser(statement: Statement) -> str:
    assert statement.parser is not None
    return statement.parser.name


def _score(result: ExtractResult, parser: str) -> float:
    assert result.parse is not None
    (score,) = [s.score for s in result.parse.scores if s.name == parser]
    return score


def test_a_card_mentioned_in_a_note_does_not_make_a_card_statement() -> None:
    # The note is on the page, one line of 22 words, outside the rows.
    assert len(synth.CARD_NOTE.split()) == 22
    assert "business credit card" in synth.CARD_NOTE
    data = synth.checking_with_card_note()
    for result in (_unhinted(data), _read(data)):
        statement = _only(result)
        assert _score(result, "card_statement_generic") == 0.0
        assert _parser(statement) == "statement_generic"
        assert statement.account.account_type != "creditcard"
        # Signs as printed: a figure with a minus is money out.
        assert _rows(statement) == synth.EXPECTED_ROWS["checking_with_card_note"]
        assert _balances(statement) == (Decimal("4820.10"), Decimal("6474.84"))
        assert _status(statement) == "ok"
        assert _skipped(result) == 0


@pytest.mark.parametrize(
    ("claimant", "builder", "reader"),
    [
        ("mock_savings_and_loan", "other_current_account", "statement_generic"),
        ("demo_card_services", "other_card_ending_in", "card_statement_generic"),
    ],
    ids=["mock_savings_and_loan", "demo_card_services"],
)
def test_one_fingerprint_phrase_is_not_the_institution(
    claimant: str, builder: str, reader: str
) -> None:
    result = _unhinted(getattr(synth, builder)())
    statement = _only(result)
    assert _score(result, claimant) == 0.0
    assert _parser(statement) == reader
    # Nobody's name is put on an account its institution did not print.
    assert statement.account.institution is None
    assert _rows(statement) == synth.EXPECTED_ROWS[builder]
    assert _status(statement) == "ok"
    assert _skipped(result) == 0


def test_a_statement_of_another_bank_keeps_its_own_currency_and_type() -> None:
    statement = _only(_unhinted(synth.other_current_account()))
    assert statement.account.currency == "GBP"
    assert statement.account.last4 == "5527"
    card = _only(_unhinted(synth.other_card_ending_in()))
    assert card.account.account_type == "creditcard"
    assert card.account.last4 == "4410"


def test_a_card_paid_from_a_checking_account_is_still_a_card() -> None:
    # Two short lines of page 1 hold a deposit-account word, and neither
    # names the account the statement is of.
    assert "Checking" in synth.CARD_AUTOPAY_LINE
    assert "savings" in synth.CARD_REWARDS_LINE
    data = synth.card_paid_from_checking()
    for result in (_unhinted(data), _read(data)):
        statement = _only(result)
        assert _parser(statement) == "card_statement_generic"
        assert statement.account.account_type == "creditcard"
        assert statement.account.last4 == "4410"
        # Charges are money out and the payment money in, as without the two lines.
        assert _rows(statement) == synth.EXPECTED_ROWS["card_paid_from_checking"]
        assert _rows(statement) == _rows(_only(_unhinted(synth.other_card_ending_in())))
        assert _status(statement) == "ok"
        assert _skipped(result) == 0


def _statement_of_account() -> bytes:
    return synth_card_statement.card_pdf(synth_card_statement.card_statement())


def _demo_card() -> bytes:
    return synth_pdf_statements.demo_card_pdf(synth_pdf_statements.demo_card_statement())


@pytest.mark.parametrize(
    "fixture",
    ["statement_of_account", "unseen_layout", "demo_card_services"],
)
def test_a_card_statement_is_still_a_card(fixture: str) -> None:
    builders: dict[str, Callable[[], bytes]] = {
        "statement_of_account": _statement_of_account,
        "unseen_layout": synth_unseen_layouts.card_statement,
        "demo_card_services": _demo_card,
    }
    data = builders[fixture]()
    result = _unhinted(data)
    statement = _only(result)
    assert _parser(statement) == (
        "demo_card_services" if fixture == "demo_card_services" else "card_statement_generic"
    )
    assert statement.account.account_type == "creditcard"
    assert _status(statement) == "ok"
    # Every phrase of its fingerprint, and a table: the institution's parser, in full.
    assert _score(result, "demo_card_services") == (1.0 if fixture == "demo_card_services" else 0.0)


def test_every_fingerprint_phrase_without_a_table_scores_low() -> None:
    top = [[("Mock Savings and Loan", 62.0)], [("Current Account Statement", 62.0)]]
    table = [
        [("Date", 62.0), ("Details", 150.0), ("Paid out", 380.0), ("Paid in", 450.0)],
        [("05/01/2026", 62.0), ("Coffee", 150.0), ("4.50", 380.0)],
    ]
    parser = MockSavingsParser()
    assert parser.detect(_view([*top, *table])) == 1.0
    assert parser.detect(_view(top)) == 0.54
    # One phrase, table or not: nothing.
    assert parser.detect(_view([top[1], *table])) == 0.0
    assert parser.detect(_view([top[0], *table])) == 0.0
    assert parser.detect(_view([top[0]])) == 0.0


# -- hand-built lines -----------------------------------------------------------


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


def _parse(rows: list[list[tuple[str, float]]], *, step: float = 14.0) -> ParseOutcome:
    return TableStatementParser().parse(_view(rows, step=step), threshold=0.85)


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
            # The same on a row that reads, and on one that prints no payee:
            # the figure joins no column, the description's least of all.
            [("01/08/2026", 62.0), ("Bakery", 150.0), ("12.00", 290.0), ("3.00", 500.0)],
            [("01/09/2026", 62.0), ("12.00", 290.0), ("7.00", 500.0)],
        ]
    )
    (statement,) = outcome.statements
    assert [(t.payee, t.amount) for t in statement.transactions] == [
        ("Coffee", Decimal("4.50")),
        ("Bakery", Decimal("3.00")),
        (None, Decimal("7.00")),
    ]
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
            # Under ``Description``, 88 points right of the dates: the cell is
            # the description's, and its first word is no date of a row.
            [("01/31/2026 average daily balance", 150.0), ("812.44", 500.0)],
        ]
    )
    (statement,) = outcome.statements
    assert [(t.posted, t.payee) for t in statement.transactions] == [(date(2026, 1, 5), "Coffee")]
    assert outcome.skipped_lines == 0
    assert "dates_carried" not in outcome.warnings


@pytest.mark.parametrize(
    "line",
    [
        "01/01/2026 to 01/31/2026 average balance",
        "01/01 through 01/31 interest earned",
        "01/01 thru 01/31/2026 interest earned",
        "Jan 1 - Jan 31 interest earned",
        "01/01/2026 \u2013 01/31/2026 average balance",
    ],
)
@pytest.mark.parametrize("more_rows", [False, True], ids=["last-line", "rows-follow"])
def test_a_line_that_starts_with_a_range_of_dates_is_no_row(line: str, more_rows: bool) -> None:
    # A summary line printed from the date column, its figure under
    # ``Amount``: the first date of a range dates no transaction.
    after = [_row(20, "Tea", "-9.00")] if more_rows else []
    outcome = _parse(
        [
            [("Statement period 01/01/2026 - 01/31/2026", 62.0)],
            HEADER,
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("-4.50", 500.0)],
            [(line, 62.0), ("97.00", 500.0)],
            *after,
        ]
    )
    (statement,) = outcome.statements
    assert [t.payee for t in statement.transactions] == ["Coffee", *(["Tea"] if more_rows else [])]
    # No row, no row with the date above it, and nothing to count: a summary line.
    assert outcome.skipped_lines == 0
    assert "dates_carried" not in outcome.warnings


def test_a_fused_date_before_a_payee_that_starts_with_a_range_word_is_split() -> None:
    # ``To`` starts payees too: only a second date after it makes a range.
    outcome = _parse(
        [
            HEADER,
            [("01/05/2026 To Savings 0041", 62.0), ("-50.00", 500.0)],
            [("01/07/2026 Through Road Cafe", 62.0), ("-4.50", 500.0)],
        ]
    )
    (statement,) = outcome.statements
    assert [(t.posted, t.payee, t.amount) for t in statement.transactions] == [
        (date(2026, 1, 5), "To Savings 0041", Decimal("-50.00")),
        (date(2026, 1, 7), "Through Road Cafe", Decimal("-4.50")),
    ]


# -- balances, hand-built -------------------------------------------------------

ROW = [("01/05/2026", 62.0), ("Coffee", 150.0), ("-4.50", 500.0)]


def _one(outcome: ParseOutcome) -> Statement:
    (statement,) = outcome.statements
    return statement


@pytest.mark.parametrize("label", ["Balance last statement", "Last statement balance"])
def test_longest_label_wins(label: str) -> None:
    # ``statement balance`` (closing) is inside ``last statement balance``:
    # the longer label is the one printed.
    statement = _one(_parse([[(f"{label} 410.00", 62.0)], HEADER, ROW]))
    assert _balances(statement) == (Decimal("410.00"), None)
    statement = _one(
        _parse([[(f"{label} 410.00", 62.0)], [("Statement balance 405.50", 62.0)], HEADER, ROW])
    )
    assert _balances(statement) == (Decimal("410.00"), Decimal("405.50"))


def test_a_statement_balance_alone_is_the_closing_balance() -> None:
    statement = _one(_parse([[("Statement balance 55.00", 62.0)], HEADER, ROW]))
    assert _balances(statement) == (None, Decimal("55.00"))


@pytest.mark.parametrize("word", ["Previous", "Last", "Prior"])
def test_a_closing_label_after_previous_is_no_closing_balance(word: str) -> None:
    # No opening label reads ``<word> ending balance``; it is still not this
    # statement's closing balance.
    statement = _one(_parse([[(f"{word} ending balance 77.00", 62.0)], HEADER, ROW]))
    assert _balances(statement)[1] is None


def test_a_figure_alone_on_the_next_line_is_the_labels() -> None:
    outcome = _parse(
        [[("Beginning Balance on", 62.0)], [("410.00", 300.0)], HEADER, ROW],
        step=10.0,
    )
    statement = _one(outcome)
    assert _balances(statement) == (Decimal("410.00"), None)
    assert statement.confidence["opening_balance"] == 0.9


def test_a_figure_alone_on_the_line_before_is_the_labels() -> None:
    outcome = _parse(
        [
            [("405.50", 300.0)],
            [("Ending Balance", 62.0)],
            [("as of January 31", 62.0)],
            HEADER,
            ROW,
        ],
        step=10.0,
    )
    statement = _one(outcome)
    assert _balances(statement) == (None, Decimal("405.50"))
    assert statement.confidence["closing_balance"] == 0.9


def test_an_amount_two_lines_away_is_not_taken() -> None:
    # 12 points under the label, but another line lies between the two.
    outcome = _parse(
        [
            [("Beginning Balance on", 62.0)],
            [("Thank you for banking with us", 62.0)],
            [("410.00", 300.0)],
            HEADER,
            ROW,
        ],
        step=6.0,
    )
    assert _balances(_one(outcome)) == (None, None)


def test_an_amount_far_below_its_label_is_not_taken() -> None:
    # The next line, 14 points down: more than a line and a half of 9 points.
    outcome = _parse([[("Beginning Balance on", 62.0)], [("410.00", 300.0)], HEADER, ROW])
    assert _balances(_one(outcome)) == (None, None)


def test_a_figure_beside_other_text_is_not_taken() -> None:
    outcome = _parse(
        [
            [("Beginning Balance on", 62.0)],
            [("Reward points", 62.0), ("410.00", 300.0)],
            HEADER,
            ROW,
        ],
        step=10.0,
    )
    assert _balances(_one(outcome)) == (None, None)


def test_a_sentence_that_starts_with_a_label_takes_no_figure() -> None:
    outcome = _parse(
        [
            [("Ending balance figures are shown after pending items clear", 62.0)],
            # Right of the sentence's own box (labels over their values are another rule).
            [("410.00", 420.0)],
            HEADER,
            ROW,
        ],
        step=10.0,
    )
    assert _balances(_one(outcome)) == (None, None)


def test_a_dated_balance_line_is_never_a_row() -> None:
    outcome = _parse(
        [
            HEADER,
            [("01/01/2026", 62.0), ("Opening Balance", 150.0), ("100.00", 500.0)],
            ROW,
            [("01/31/2026", 62.0), ("Closing Balance", 150.0), ("95.50", 500.0)],
        ]
    )
    statement = _one(outcome)
    assert [(t.payee, t.amount) for t in statement.transactions] == [("Coffee", Decimal("-4.50"))]
    assert _balances(statement) == (Decimal("100.00"), Decimal("95.50"))
    assert _status(statement) == "ok"
    assert outcome.skipped_lines == 0


def test_the_balance_column_beats_a_money_column_on_a_balance_line() -> None:
    outcome = _parse(
        [
            TWO_SIDED,
            # A zero under ``Credit`` beside the balance: the balance is the figure.
            [("01/01/2026", 62.0), ("Balance Forward", 150.0), ("0.00", 450.0), ("100.00", 520.0)],
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 380.0), ("95.50", 520.0)],
        ]
    )
    statement = _one(outcome)
    assert [(t.payee, t.amount) for t in statement.transactions] == [("Coffee", Decimal("-4.50"))]
    assert _balances(statement) == (Decimal("100.00"), Decimal("95.50"))
    assert _status(statement) == "ok"


def test_a_payee_that_mentions_a_balance_is_still_a_row() -> None:
    outcome = _parse(
        [
            HEADER,
            # The label is not where the description starts.
            [("01/05/2026", 62.0), ("Low Balance Alert Charge", 150.0), ("-5.00", 500.0)],
            [("01/07/2026", 62.0), ("Transfer Ending Balance Sweep", 150.0), ("-20.00", 500.0)],
        ]
    )
    statement = _one(outcome)
    assert [t.amount for t in statement.transactions] == [Decimal("-5.00"), Decimal("-20.00")]


@pytest.mark.parametrize("reader", [TableStatementParser, CardStatementParser])
def test_a_payee_that_starts_with_a_balance_label_is_still_a_row(
    reader: type[TableStatementParser],
) -> None:
    card = reader is CardStatementParser
    sign = Decimal(-1) if card else Decimal(1)
    view = _view(
        [
            [("Previous balance 100.00", 62.0)],
            [("Ending balance 284.99", 62.0)],
            HEADER,
            [("01/05/2026", 62.0), ("Grocer Fennel", 150.0), ("45.00", 500.0)],
            # A shop and a brewery named like a balance: purchases, with an amount.
            [("01/07/2026", 62.0), ("New Balance Outlet 114", 150.0), ("119.99", 500.0)],
            [("01/09/2026", 62.0), ("Opening Balance Brewing Co", 150.0), ("20.00", 500.0)],
        ]
    )
    outcome = reader().parse(view, threshold=0.85)
    statement = _one(outcome)
    assert [(t.payee, t.amount) for t in statement.transactions] == [
        ("Grocer Fennel", sign * Decimal("45.00")),
        ("New Balance Outlet 114", sign * Decimal("119.99")),
        ("Opening Balance Brewing Co", sign * Decimal("20.00")),
    ]
    # The balances are the summary's, untouched by the rows.
    assert _balances(statement) == (sign * Decimal("100.00"), sign * Decimal("284.99"))
    assert _status(statement) == "ok"
    assert outcome.skipped_lines == 0


@pytest.mark.parametrize(
    "label",
    [
        "Ending Balance",
        "Ending Balance on 01/31/2026",
        "Ending Balance as of January 31",
        "Ending balance this period",
    ],
)
def test_a_dated_label_with_a_date_or_a_few_small_words_is_a_balance_line(label: str) -> None:
    outcome = _parse([[("Beginning balance 100.00", 62.0)], HEADER, ROW, _row(31, label, "95.50")])
    statement = _one(outcome)
    assert [t.payee for t in statement.transactions] == ["Coffee"]
    assert _balances(statement) == (Decimal("100.00"), Decimal("95.50"))
    assert _status(statement) == "ok"


def test_a_dated_line_with_only_a_balance_figure_is_a_balance_whatever_follows_the_label() -> None:
    # No figure in a money column: nothing here could be a transaction.
    outcome = _parse(
        [
            TWO_SIDED,
            [("01/01/2026", 62.0), ("Balance Forward Harbour Branch", 150.0), ("100.00", 520.0)],
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 380.0), ("95.50", 520.0)],
        ]
    )
    statement = _one(outcome)
    assert [t.payee for t in statement.transactions] == ["Coffee"]
    assert _balances(statement) == (Decimal("100.00"), Decimal("95.50"))
    assert outcome.skipped_lines == 0


def test_an_undated_row_before_a_dated_balance_line_takes_the_date_above_it() -> None:
    outcome = _parse(
        [
            [("Beginning balance 100.00", 62.0)],
            HEADER,
            ROW,
            [("Tea", 150.0), ("-9.00", 500.0)],
            [("01/31/2026", 62.0), ("Ending Balance", 150.0), ("86.50", 500.0)],
        ]
    )
    statement = _one(outcome)
    assert [(t.posted, t.payee, t.amount) for t in statement.transactions] == [
        (date(2026, 1, 5), "Coffee", Decimal("-4.50")),
        (date(2026, 1, 5), "Tea", Decimal("-9.00")),
    ]
    assert _balances(statement) == (Decimal("100.00"), Decimal("86.50"))
    assert _status(statement) == "ok"
    assert outcome.skipped_lines == 0


class _LabelledParser(TableStatementParser):
    """An opening label that holds a closing one after a word of its own."""

    opening_labels = (*TableStatementParser.opening_labels, "carried statement balance")


def test_a_closing_label_inside_an_opening_label_closes_nothing() -> None:
    # ``statement balance`` lies inside the longer label, and no ``previous``,
    # ``last`` or ``prior`` stands before it.
    view = _view([[("Carried statement balance 410.00", 62.0)], HEADER, ROW])
    statement = _one(_LabelledParser().parse(view, threshold=0.85))
    assert _balances(statement) == (Decimal("410.00"), None)


def test_two_figures_beside_a_label_are_not_one_balance() -> None:
    # ``1`` and ``234.00`` are two cells (a count and a figure), not 1,234.00.
    outcome = _parse(
        [[("Beginning Balance on", 62.0)], [("1", 300.0), ("234.00", 330.0)], HEADER, ROW],
        step=10.0,
    )
    assert _balances(_one(outcome)) == (None, None)
    # A currency sign in a cell of its own is still one amount alone on its line.
    outcome = _parse(
        [[("Beginning Balance on", 62.0)], [("$", 300.0), ("410.00", 310.0)], HEADER, ROW],
        step=10.0,
    )
    assert _balances(_one(outcome)) == (Decimal("410.00"), None)


# -- section titles and the direction of unsigned amounts -----------------------

IN = SectionRule("in", None, None)
OUT = SectionRule("out", None, None)
PLAIN = SectionRule(None, None, None)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # One of each group of the table.
        ("Deposits and additions", IN),
        ("DEPOSITS AND OTHER CREDITS", IN),
        ("Electronic Deposits", IN),
        ("Other credits", SectionRule("in", "in", None)),
        ("Credits", SectionRule("in", "in", None)),
        ("Electronic withdrawals", OUT),
        ("Withdrawals / Debits", OUT),
        ("ATM & Debit Card Withdrawals", OUT),
        ("Checks paid", OUT),
        ("Other debits", SectionRule("out", "out", None)),
        ("Purchases and adjustments", SectionRule("out", "out", None)),
        ("Payments and other credits", SectionRule(None, "in", None)),
        ("Payments", SectionRule(None, "in", None)),
        ("New charges", SectionRule(None, "out", None)),
        ("Cash advances", SectionRule(None, "out", None)),
        ("Fees charged", SectionRule("out", "out", "fee")),
        ("Service fees", SectionRule("out", "out", "fee")),
        ("Interest charged", SectionRule("out", "out", "int")),
        ("Interest earned", SectionRule("in", None, "int")),
        ("Interest", SectionRule(None, "out", "int")),
        ("Transactions", PLAIN),
        # The longest key, then anything after it.
        ("Deposits and other additions this period", IN),
        ("Checks paid in number order", OUT),
        # ``continued`` after the title.
        ("ELECTRONIC WITHDRAWALS (continued)", OUT),
        ("Deposits and Additions - continued", IN),
        ("Checks paid (cont.)", OUT),
        # Both sides named: a title, and no direction.
        ("Deposits and withdrawals", PLAIN),
        ("Withdrawals and deposits", PLAIN),
        ("Credits and debits", PLAIN),
        ("Payments and purchases", PLAIN),
        ("Deposits and fees", PLAIN),
        ("Fees and credits", SectionRule(None, None, "fee")),
    ],
)
def test_section_title_matching(title: str, expected: SectionRule) -> None:
    line = _view([[(title, 62.0)]]).lines[0]
    assert TableStatementParser()._section(line.norm) == (False, expected)


@pytest.mark.parametrize(
    "text",
    [
        "Interest summary",
        "Fees summary",
        "Interest rate",
        "Interest calculation",
        "Deposits information",
        "Deposit",
        "Deposited items",
        "Feesable",
        "Account activity",
        # Nine words: a sentence.
        "Deposits made after the daily cutoff post next day",
    ],
)
def test_lines_that_are_no_section_title(text: str) -> None:
    line = _view([[(text, 62.0)]]).lines[0]
    assert TableStatementParser()._section(line.norm) is None


def test_a_pending_heading_is_still_a_pending_section() -> None:
    assert TableStatementParser()._section("pending transactions") == (True, None)


def test_every_section_key_is_normalised_and_one_sided() -> None:
    for key, rule in SECTION_TITLES.items():
        assert _view([[(key, 62.0)]]).lines[0].norm == key
        assert {rule.bank, rule.card} - {None} in (set(), {"in"}, {"out"})


def test_unsigned_amounts_take_the_direction_of_their_section() -> None:
    result = _read(synth.sectioned_unsigned_checking())
    statement = _only(result)
    expected = synth.EXPECTED_ROWS["sectioned_unsigned_checking"]
    assert [t.amount > 0 for t in statement.transactions] == [True] * 3 + [False] * 6
    assert _rows(statement) == expected
    assert [t.type for t in statement.transactions] == [None] * 8 + ["fee"]
    assert _balances(statement) == synth.EXPECTED_BALANCES["sectioned_unsigned_checking"]
    assert _status(statement) == "ok"
    assert _skipped(result) == 0
    assert _parser(statement) == "statement_generic"


@pytest.mark.parametrize(
    "builder",
    ["sectioned_unsigned_checking", "sectioned_repeat_without_title"],
    ids=["with-title", "header-only"],
)
def test_direction_survives_a_repeated_page_header(builder: str) -> None:
    result = _read(getattr(synth, builder)())
    statement = _only(result)
    second = [t for t in statement.transactions if t.page == 2 and t.type is None]
    assert [(t.payee, t.amount) for t in second] == [
        ("ATM Cash Mill Lane", Decimal("-45.00")),
        ("Card Purchase Hazelbank Tools", Decimal("-203.77")),
    ]
    assert _rows(statement) == synth.EXPECTED_ROWS[builder]
    assert _status(statement) == "ok"
    assert _skipped(result) == 0


@pytest.mark.parametrize(
    "builder", ["sectioned_unsigned_checking", "sectioned_repeat_without_title"]
)
def test_a_section_continued_on_the_next_page_is_one_section(builder: str) -> None:
    result = extract_pdf(getattr(synth, builder)(), OPTIONS, "pdf")
    parser = TableStatementParser()
    view = DocumentView.from_result(result)
    rows = parser._collect(parser.prepare(view.lines))[0]
    assert [row.direction for row in rows] == ["in"] * 3 + ["out"] * 6
    numbers = [row.section for row in rows]
    # Three sections, in order: the withdrawals of both pages are one.
    assert len(set(numbers)) == 3
    assert numbers == sorted(numbers)
    assert len(set(numbers[:3])) == len(set(numbers[3:8])) == 1


def test_card_payment_without_a_sign_is_a_credit() -> None:
    for result in (
        _unhinted(synth.card_unsigned_payments()),
        _read(synth.card_unsigned_payments()),
    ):
        statement = _only(result)
        assert _parser(statement) == "card_statement_generic"
        assert _rows(statement) == synth.EXPECTED_ROWS["card_unsigned_payments"]
        # The payment and the refund are money in, the purchases money out.
        assert [t.amount > 0 for t in statement.transactions] == [True, False, False, True, False]
        assert _balances(statement) == (Decimal("-640.25"), Decimal("-120.06"))
        assert _status(statement) == "ok"
        assert _skipped(result) == 0


def test_signed_sectioned_statement_is_unchanged() -> None:
    statement = _only(_read(synth_unseen_layouts.sectioned_checking()))
    assert [t.amount for t in statement.transactions] == [
        Decimal("1240.00"),
        Decimal("1240.00"),
        Decimal("-950.00"),
        Decimal("-88.12"),
        Decimal("-43.28"),
        Decimal("-130.00"),
        Decimal("-100.00"),
        Decimal("-250.00"),
    ]
    assert _status(statement) == "ok"


def _pages(pages: list[list[list[tuple[str, float]]]]) -> DocumentView:
    """Like :func:`_view`, one list of lines per page."""
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


def _amounts(outcome: ParseOutcome) -> list[Decimal]:
    return [t.amount for t in _one(outcome).transactions]


def _row(day: int, payee: str, amount: str) -> list[tuple[str, float]]:
    return [(f"01/{day:02d}/2026", 62.0), (payee, 150.0), (amount, 500.0)]


def test_a_title_above_the_header_gives_the_direction() -> None:
    outcome = _parse(
        [
            [("Withdrawals", 62.0)],
            HEADER,
            _row(5, "Rent", "700.00"),
            [("Deposits", 62.0)],
            HEADER,
            _row(9, "Salary", "900.00"),
        ]
    )
    assert _amounts(outcome) == [Decimal("-700.00"), Decimal("900.00")]


def test_a_title_two_lines_above_the_header_counts_when_a_figure_line_is_between() -> None:
    outcome = _parse(
        [[("Withdrawals", 62.0)], [("3 items 745.00", 62.0)], HEADER, _row(5, "Rent", "700.00")]
    )
    assert _amounts(outcome) == [Decimal("-700.00")]


def test_only_the_nearest_line_of_words_above_a_header_may_be_its_title() -> None:
    # ``Withdrawals`` is two lines up, and a line of words lies between.
    outcome = _parse(
        [
            [("Withdrawals", 62.0)],
            [("Account activity", 62.0)],
            HEADER,
            _row(5, "Salary", "900.00"),
        ]
    )
    assert _amounts(outcome) == [Decimal("900.00")]
    # Three lines up is too far.
    outcome = _parse(
        [
            [("Withdrawals", 62.0)],
            [("745.00", 300.0)],
            [("12.00", 300.0)],
            HEADER,
            _row(5, "Salary", "900.00"),
        ]
    )
    assert _amounts(outcome) == [Decimal("900.00")]


def test_a_title_on_the_page_before_is_not_the_headers() -> None:
    view = _pages([[[("Withdrawals", 62.0)]], [HEADER, _row(5, "Salary", "900.00")]])
    outcome = TableStatementParser().parse(view, threshold=0.85)
    assert _amounts(outcome) == [Decimal("900.00")]


def test_a_summary_line_with_a_figure_is_no_title() -> None:
    # ``Withdrawals 700.00`` is the summary's total, not a title.
    outcome = _parse([[("Withdrawals 700.00", 62.0)], HEADER, _row(5, "Salary", "900.00")])
    assert _amounts(outcome) == [Decimal("900.00")]
    # Between rows too.
    outcome = _parse(
        [
            HEADER,
            _row(5, "Salary", "900.00"),
            [("Withdrawals 700.00", 62.0)],
            _row(9, "Bonus", "50.00"),
        ]
    )
    assert _amounts(outcome) == [Decimal("900.00"), Decimal("50.00")]


def test_a_title_between_rows_gives_the_direction() -> None:
    outcome = _parse(
        [
            HEADER,
            [("Deposits", 62.0)],
            _row(5, "Salary", "900.00"),
            [("Checks paid", 62.0)],
            _row(9, "Rent", "700.00"),
            [("Deposits and withdrawals", 62.0)],
            _row(12, "Bonus", "50.00"),
        ]
    )
    assert _amounts(outcome) == [Decimal("900.00"), Decimal("-700.00"), Decimal("50.00")]


def test_a_new_table_without_a_title_has_no_direction() -> None:
    outcome = _parse(
        [
            [("Withdrawals", 62.0)],
            HEADER,
            _row(5, "Rent", "700.00"),
            # The same page, the same header, no title: another table.
            HEADER,
            _row(9, "Salary", "900.00"),
        ]
    )
    assert _amounts(outcome) == [Decimal("-700.00"), Decimal("900.00")]


def test_a_repeated_header_on_the_next_page_keeps_the_direction_and_the_type() -> None:
    view = _pages(
        [
            [[("Fees", 62.0)], HEADER, _row(5, "Paper statements", "3.00")],
            [[("Page 2", 62.0)], HEADER, _row(9, "Wire, outgoing", "25.00")],
        ]
    )
    statement = _one(TableStatementParser().parse(view, threshold=0.85))
    assert [(t.amount, t.type) for t in statement.transactions] == [
        (Decimal("-3.00"), "fee"),
        (Decimal("-25.00"), "fee"),
    ]


def test_a_different_header_on_the_next_page_is_a_new_table() -> None:
    other = [("Date", 62.0), ("Description", 150.0), ("Amount", 440.0), ("Balance", 520.0)]
    view = _pages(
        [
            [[("Withdrawals", 62.0)], HEADER, _row(5, "Rent", "700.00")],
            [
                other,
                [("01/09/2026", 62.0), ("Salary", 150.0), ("900.00", 440.0), ("1,100.00", 520.0)],
            ],
        ]
    )
    outcome = TableStatementParser().parse(view, threshold=0.85)
    assert _amounts(outcome) == [Decimal("-700.00"), Decimal("900.00")]


def test_a_header_after_an_unread_table_is_no_repeat() -> None:
    # A ``Date | Balance`` table ended ours: the header on page 2 starts anew.
    view = _pages(
        [
            [
                [("Withdrawals", 62.0)],
                HEADER,
                _row(5, "Rent", "700.00"),
                [("Date", 62.0), ("Balance", 500.0)],
            ],
            [HEADER, _row(9, "Salary", "900.00")],
        ]
    )
    outcome = TableStatementParser().parse(view, threshold=0.85)
    assert _amounts(outcome) == [Decimal("-700.00"), Decimal("900.00")]


@pytest.mark.parametrize(
    "wrap",
    [
        "Checks 2 items branch 14",
        "Fees waived for this deposit",
        "Transactions batch 0088",
        "Payments desk 3",
    ],
)
def test_a_wrapped_line_that_starts_with_a_title_word_is_no_title(wrap: str) -> None:
    outcome = _parse(
        [
            [("Deposits", 62.0)],
            HEADER,
            _row(5, "Mobile deposit", "900.00"),
            # Right under the row, in the description column: the row's second line.
            [(wrap, 150.0)],
            _row(9, "Salary Fennel Works", "1,180.00"),
        ]
    )
    statement = _one(outcome)
    assert [(t.amount, t.type) for t in statement.transactions] == [
        (Decimal("900.00"), statement.transactions[0].type),
        (Decimal("1180.00"), statement.transactions[0].type),
    ]
    assert statement.transactions[0].type != "fee"
    assert statement.transactions[0].memo == wrap


def test_a_title_under_a_row_is_one_at_the_margin_or_when_it_is_the_whole_key() -> None:
    outcome = _parse(
        [
            [("Deposits", 62.0)],
            HEADER,
            _row(5, "Salary", "900.00"),
            # Left of the description column: a title, whatever follows its key.
            [("Checks paid this period", 62.0)],
            _row(9, "Rent", "700.00"),
            # In the description column, the key and nothing else: a sub-heading.
            [("Fees", 150.0)],
            _row(12, "Paper statements", "3.00"),
            [("Deposits continued", 150.0)],
            _row(14, "Bonus", "50.00"),
        ]
    )
    statement = _one(outcome)
    assert [(t.amount, t.type, t.memo) for t in statement.transactions] == [
        (Decimal("900.00"), statement.transactions[0].type, None),
        (Decimal("-700.00"), statement.transactions[1].type, None),
        (Decimal("-3.00"), "fee", None),
        (Decimal("50.00"), statement.transactions[3].type, None),
    ]


def test_a_pending_heading_under_a_row_is_no_wrapped_line() -> None:
    outcome = _parse(
        [
            HEADER,
            _row(5, "Coffee", "-4.50"),
            [("Pending transactions", 150.0)],
            _row(9, "Tea", "-2.00"),
        ]
    )
    statement = _one(outcome)
    assert [(t.payee, t.memo) for t in statement.transactions] == [("Coffee", None)]
    assert outcome.pending_items == 1


def test_the_last_line_of_the_page_before_says_nothing_of_a_repeated_header() -> None:
    view = _pages(
        [
            [
                [("Electronic Withdrawals", 62.0)],
                HEADER,
                _row(5, "Rent", "700.00"),
                [("Withdrawals carried to the next page", 62.0)],
            ],
            [HEADER, _row(9, "Gas", "40.00")],
        ]
    )
    outcome = TableStatementParser().parse(view, threshold=0.85)
    assert _amounts(outcome) == [Decimal("-700.00"), Decimal("-40.00")]


@pytest.mark.parametrize(
    "above",
    [
        # A title the table does not know.
        [("Direct Deposits", 62.0)],
        # A known one with its total on the line: no title, and no page furniture.
        [("Deposits and additions", 62.0), ("900.00", 500.0)],
        [("Other Additions 900.00", 62.0)],
    ],
    ids=["unknown-title", "title-with-total", "unknown-with-total"],
)
def test_a_header_under_another_sections_line_is_no_repeat(above: list[tuple[str, float]]) -> None:
    view = _pages(
        [
            [[("Electronic Withdrawals", 62.0)], HEADER, _row(5, "Rent", "700.00")],
            [above, HEADER, _row(9, "Salary", "900.00")],
        ]
    )
    outcome = TableStatementParser().parse(view, threshold=0.85)
    # The withdrawals' direction ends with their section: the figure is as printed.
    assert _amounts(outcome) == [Decimal("-700.00"), Decimal("900.00")]


@pytest.mark.parametrize(
    "above",
    [
        [[("Harbour Lantern Bank", 62.0)], [("Account number ending 4410", 62.0)]],
        [[("Statement period 01/01/2026 - 01/31/2026", 62.0)], [("Page 2 of 3", 400.0)]],
        [],
    ],
    ids=["bank-and-account", "period-and-page", "nothing"],
)
def test_page_furniture_over_a_repeated_header_keeps_the_direction(
    above: list[list[tuple[str, float]]],
) -> None:
    view = _pages(
        [
            [[("Electronic Withdrawals", 62.0)], HEADER, _row(5, "Rent", "700.00")],
            [*above, HEADER, _row(9, "Gas", "40.00")],
        ]
    )
    outcome = TableStatementParser().parse(view, threshold=0.85)
    assert _amounts(outcome) == [Decimal("-700.00"), Decimal("-40.00")]


def test_a_printed_sign_beats_the_section() -> None:
    outcome = _parse(
        [
            [("Deposits", 62.0)],
            HEADER,
            _row(5, "Reversal", "-25.00"),
            _row(6, "Returned item", "(40.00)"),
            _row(7, "Salary", "900.00"),
            [("Withdrawals", 62.0)],
            HEADER,
            _row(9, "Refund", "30.00 CR"),
            _row(10, "Correction", "+15.00"),
            _row(11, "Rent", "700.00"),
            _row(12, "Card", "8.00 DR"),
        ]
    )
    assert _amounts(outcome) == [
        Decimal("-25.00"),
        Decimal("-40.00"),
        Decimal("900.00"),
        Decimal("30.00"),
        Decimal("15.00"),
        Decimal("-700.00"),
        Decimal("-8.00"),
    ]


def test_a_debit_or_credit_column_beats_the_section() -> None:
    outcome = _parse(
        [
            [("Deposits", 62.0)],
            TWO_SIDED,
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 380.0), ("95.50", 520.0)],
            [("01/06/2026", 62.0), ("Salary", 150.0), ("900.00", 450.0), ("995.50", 520.0)],
        ]
    )
    assert _amounts(outcome) == [Decimal("-4.50"), Decimal("900.00")]


def _card(rows: list[list[tuple[str, float]]]) -> list[Decimal]:
    outcome = CardStatementParser().parse(_view(rows), threshold=0.85)
    return _amounts(outcome)


def test_card_sections_follow_the_card_column() -> None:
    assert _card(
        [
            [("Payments", 62.0)],
            HEADER,
            _row(5, "Payment received", "200.00"),
            [("Purchases", 62.0)],
            HEADER,
            _row(9, "Bookshop", "18.00"),
            _row(10, "Bookshop return", "-18.00"),
            [("Interest", 62.0)],
            HEADER,
            _row(28, "On purchases", "2.10"),
        ]
    ) == [Decimal("200.00"), Decimal("-18.00"), Decimal("18.00"), Decimal("-2.10")]


def test_a_bank_title_gives_a_card_no_direction() -> None:
    # ``Deposits`` says nothing on a card: an unsigned figure is a charge, as always.
    assert _card([[("Deposits", 62.0)], HEADER, _row(5, "Bookshop", "18.00")]) == [
        Decimal("-18.00")
    ]


def test_a_card_title_gives_a_bank_statement_no_direction() -> None:
    # ``Payments`` is money in on a card and says nothing on a bank account.
    outcome = _parse([[("Payments", 62.0)], HEADER, _row(5, "Loan payment", "120.00")])
    assert _amounts(outcome) == [Decimal("120.00")]


# -- dates printed once per day; lines found and not read ------------------------


def _warnings(result: ExtractResult) -> list[str]:
    assert result.parse is not None
    return list(result.parse.warnings)


def test_rows_take_the_date_printed_above_them() -> None:
    result = _read(synth.date_once_per_day())
    statement = _only(result)
    expected = synth.EXPECTED_ROWS["date_once_per_day"]
    assert len(statement.transactions) == 14
    assert _rows(statement) == expected
    assert _status(statement) == "ok"
    assert _skipped(result) == 0
    assert _warnings(result).count("dates_carried") == 1
    # A date read one line up is believed enough to import: nothing waits on it.
    assert not any("posted_date" in t.low_confidence for t in statement.transactions)


def test_a_carried_date_is_believed_less_than_a_printed_one() -> None:
    statement = _only(_read(synth.date_once_per_day()))
    days = [posted for posted, _, _ in synth.EXPECTED_ROWS["date_once_per_day"]]
    carried = [index for index, day in enumerate(days) if index and day == days[index - 1]]
    assert len(carried) == 9
    for index, txn in enumerate(statement.transactions):
        # The date is the least sure field of a carried row (0.9), and of no other.
        assert (txn.confidence == 0.9) is (index in carried), (index, txn.confidence)
        assert txn.confidence is not None
        assert txn.confidence >= 0.9


def test_a_figure_without_a_payee_between_rows_is_counted_not_read() -> None:
    outcome = _parse(
        [HEADER, _row(5, "Coffee", "-4.50"), [("-3.00", 500.0)], _row(9, "Refund", "9.25")]
    )
    assert [payee for _, payee in _payees(outcome)] == ["Coffee", "Refund"]
    assert outcome.skipped_lines == 1
    assert outcome.skipped_regions == [(1, (500.0, 128.0, 525.0, 137.0))]
    assert "dates_carried" not in outcome.warnings


def test_an_undated_line_at_the_top_of_a_page_takes_no_date_from_the_page_before() -> None:
    # The table runs on without a header; the day of the line is not known.
    view = _pages(
        [
            [HEADER, _row(5, "Coffee", "-4.50")],
            [_undated("Bakery", "-3.00"), _row(9, "Refund", "9.25")],
        ]
    )
    outcome = TableStatementParser().parse(view, threshold=0.85)
    assert [payee for _, payee in _payees(outcome)] == ["Coffee", "Refund"]
    assert outcome.skipped_lines == 1
    assert outcome.skipped_regions == [(2, (150.0, 100.0, 525.0, 109.0))]
    assert "dates_carried" not in outcome.warnings


def test_lines_under_an_ordinary_table_are_not_rows() -> None:
    result = _read(synth.summary_lines_under_table())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["summary_lines_under_table"]
    assert _status(statement) == "ok"
    assert _skipped(result) == 0
    assert "dates_carried" not in _warnings(result)


def test_an_unreadable_line_between_rows_is_counted() -> None:
    odd = [("Adjustment", 150.0), ("4.50", 490.0), ("3.00", 515.0)]
    outcome = _parse([HEADER, _row(5, "Coffee", "-4.50"), odd, _row(9, "Refund", "9.25")])
    statement = _one(outcome)
    assert [t.payee for t in statement.transactions] == ["Coffee", "Refund"]
    assert outcome.skipped_lines == 1
    # The line itself: third on the page, from its text to its last figure.
    assert outcome.skipped_regions == [(1, (150.0, 128.0, 535.0, 137.0))]
    assert "dates_carried" not in outcome.warnings


_SHARE_HEADER = TWO_SIDED


def _two_shares(above: list[list[tuple[str, float]]]) -> list[list[tuple[str, float]]]:
    """Two shares told apart by their opening rows, ``above`` printed before the first."""
    return [
        *above,
        [("REGULAR SAVINGS - ID 0001", 62.0)],
        _SHARE_HEADER,
        [("03/01/2026", 62.0), ("Beginning Balance", 150.0), ("100.00", 520.0)],
        [("03/05/2026", 62.0), ("Deposit", 150.0), ("20.00", 450.0), ("120.00", 520.0)],
        [("PLAIN CHECKING - ID 0009", 62.0)],
        _SHARE_HEADER,
        [("03/01/2026", 62.0), ("Beginning Balance", 150.0), ("300.00", 520.0)],
        [("03/07/2026", 62.0), ("Coffee", 150.0), ("4.50", 380.0), ("295.50", 520.0)],
    ]


def test_rows_before_the_first_share_are_counted() -> None:
    stray = [("03/02/2026", 62.0), ("Courtesy credit", 150.0), ("5.00", 450.0)]
    outcome = _parse(_two_shares([_SHARE_HEADER, stray]))
    assert "accounts_by_balance" in outcome.warnings
    assert [[t.payee for t in s.transactions] for s in outcome.statements] == [
        ["Deposit"],
        ["Coffee"],
    ]
    assert outcome.skipped_lines == 1
    assert outcome.skipped_regions == [(1, (62.0, 114.0, 470.0, 123.0))]
    assert "rows_outside_account" in outcome.warnings


def test_shares_with_nothing_above_them_count_nothing() -> None:
    outcome = _parse(_two_shares([]))
    assert len(outcome.statements) == 2
    assert outcome.skipped_lines == 0
    assert "rows_outside_account" not in outcome.warnings


def test_lines_of_a_headerless_section_are_counted_not_read() -> None:
    result = _read(synth.titled_sections_without_headers())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["titled_sections_without_headers"]
    assert len(statement.transactions) == 4
    assert all(t.amount < 0 for t in statement.transactions)
    assert _skipped(result) == 3
    assert "rows_without_header:3" in _warnings(result)
    assert result.parse is not None
    assert len(result.parse.skipped_regions) == 3
    # The deposits were not read, so the statement cannot add up: it is held.
    assert _status(statement) == "mismatch"


def _undated(payee: str, amount: str) -> list[tuple[str, float]]:
    return [(payee, 150.0), (amount, 500.0)]


def _payees(outcome: ParseOutcome) -> list[tuple[date, str | None]]:
    return [(t.posted, t.payee) for t in _one(outcome).transactions]


def test_an_undated_line_between_rows_takes_the_date_above_it() -> None:
    outcome = _parse(
        [
            HEADER,
            _row(15, "Coffee", "-4.50"),
            _undated("Bakery", "-3.00"),
            [("Counter 4", 150.0)],
            _row(19, "Refund", "9.25"),
        ]
    )
    assert _payees(outcome) == [
        (date(2026, 1, 15), "Coffee"),
        (date(2026, 1, 15), "Bakery"),
        (date(2026, 1, 19), "Refund"),
    ]
    carried = _one(outcome).transactions[1]
    assert carried.amount == Decimal("-3.00")
    # Its wrapped line is its own, and its date is believed enough to import.
    assert carried.memo == "Counter 4"
    assert "posted_date" not in carried.low_confidence
    assert outcome.skipped_lines == 0
    assert outcome.warnings.count("dates_carried") == 1


def test_an_undated_line_far_below_the_row_above_is_counted_not_read() -> None:
    lines = [
        HEADER,
        _row(5, "Coffee", "-4.50"),
        _undated("Bakery", "-3.00"),
        _row(9, "Tea", "-2.00"),
    ]
    outcome = _parse(lines, step=40.0)
    assert [payee for _, payee in _payees(outcome)] == ["Coffee", "Tea"]
    assert outcome.skipped_lines == 1
    assert "dates_carried" not in outcome.warnings


def test_undated_lines_at_the_end_of_a_table_need_a_proven_layout() -> None:
    tail = [_undated("Service charge", "-2.00"), _undated("Paper statement", "-1.00")]
    once = [
        HEADER,
        _row(5, "Coffee", "-4.50"),
        _undated("Bakery", "-3.00"),
        _row(9, "Tea", "-2.00"),
    ]
    outcome = _parse([*once, *tail])
    # One carried row proves nothing: the lines under the table stay summary lines.
    assert [payee for _, payee in _payees(outcome)] == ["Coffee", "Bakery", "Tea"]
    assert outcome.skipped_lines == 0
    twice = [*once, _undated("Stamps", "-1.10"), _row(12, "Bus", "-2.40")]
    outcome = _parse([*twice, *tail])
    assert _payees(outcome)[-3:] == [
        (date(2026, 1, 12), "Bus"),
        (date(2026, 1, 12), "Service charge"),
        (date(2026, 1, 12), "Paper statement"),
    ]
    assert outcome.skipped_lines == 0


def test_a_line_under_one_table_is_no_row_of_the_next() -> None:
    outcome = _parse(
        [
            HEADER,
            _row(5, "Coffee", "-4.50"),
            _undated("Interest earned this period", "0.38"),
            HEADER,
            _row(9, "Refund", "9.25"),
        ]
    )
    assert [payee for _, payee in _payees(outcome)] == ["Coffee", "Refund"]
    assert outcome.skipped_lines == 0
    assert "dates_carried" not in outcome.warnings


def test_a_line_before_a_new_section_title_is_no_row() -> None:
    outcome = _parse(
        [
            HEADER,
            _row(5, "Pay", "50.00"),
            _undated("Deposits this period", "50.00"),
            [("Withdrawals", 62.0)],
            _row(9, "Rent", "30.00"),
        ]
    )
    assert _amounts(outcome) == [Decimal("50.00"), Decimal("-30.00")]
    assert outcome.skipped_lines == 0


def test_totals_and_balance_lines_between_rows_are_neither_rows_nor_counted() -> None:
    outcome = _parse(
        [
            HEADER,
            _row(5, "Coffee", "-4.50"),
            _undated("Total for the day", "-4.50"),
            _undated("Closing balance", "95.50"),
            _row(9, "Refund", "9.25"),
        ]
    )
    assert [payee for _, payee in _payees(outcome)] == ["Coffee", "Refund"]
    assert outcome.skipped_lines == 0
    assert _one(outcome).ledger_balance is not None


def test_a_figure_in_both_money_columns_of_an_undated_line_is_counted() -> None:
    outcome = _parse(
        [
            TWO_SIDED,
            [("01/05/2026", 62.0), ("Coffee", 150.0), ("4.50", 380.0)],
            [("Adjustment", 150.0), ("4.50", 380.0), ("3.00", 450.0)],
            [("01/09/2026", 62.0), ("Refund", 150.0), ("9.25", 450.0)],
        ]
    )
    assert [payee for _, payee in _payees(outcome)] == ["Coffee", "Refund"]
    assert outcome.skipped_lines == 1


def test_a_printed_sign_beats_the_section_on_a_carried_row() -> None:
    outcome = _parse(
        [
            [("Withdrawals", 62.0)],
            HEADER,
            _row(5, "Rent", "30.00"),
            _undated("Gas", "12.00"),
            _undated("Rent rebate", "+5.00"),
            _row(9, "Water", "8.00"),
        ]
    )
    assert _amounts(outcome) == [
        Decimal("-30.00"),
        Decimal("-12.00"),
        Decimal("5.00"),
        Decimal("-8.00"),
    ]


def _led(day: int, text: str, amount: str) -> list[tuple[str, float]]:
    return [(f"01/{day:02d}/2026", 62.0), (text, 150.0), (amount, 500.0)]


def test_date_led_lines_without_a_title_or_a_header_are_not_counted() -> None:
    outcome = _parse(
        [
            [("Rate changes", 62.0)],
            _led(2, "Rate set to", "1.25"),
            [("Fees information", 62.0)],
            _led(3, "Fee schedule revised", "3.00"),
            HEADER,
            _row(9, "Refund", "9.25"),
        ]
    )
    assert [payee for _, payee in _payees(outcome)] == ["Refund"]
    assert outcome.skipped_lines == 0
    assert not any(w.startswith("rows_without_header") for w in outcome.warnings)


def test_a_headerless_section_ends_with_its_page_and_at_a_block_title() -> None:
    view = _pages(
        [
            [
                [("Deposits", 62.0)],
                _led(2, "Pay", "50.00"),
                [("Account Summary", 62.0)],
                _led(3, "Rate set to", "1.25"),
                [("Deposits", 62.0)],
            ],
            [_led(4, "Notice mailed", "1.00"), HEADER, _row(9, "Refund", "9.25")],
        ]
    )
    outcome = TableStatementParser().parse(view, threshold=0.85)
    assert [t.payee for t in _one(outcome).transactions] == ["Refund"]
    assert outcome.skipped_lines == 1
    assert outcome.skipped_regions == [(1, (62.0, 114.0, 525.0, 123.0))]
    assert "rows_without_header:1" in outcome.warnings


# -- share titles ---------------------------------------------------------------

_FIRST_TITLE = [("PRIMARY SHARE ID 0000", 62.0), ("Dividends Paid YTD:", 330.0), ("4.12", 520.0)]
_SECOND_TITLE = [("ID 0040 - SHARE DRAFT", 62.0), ("Dividends Paid YTD:", 330.0), ("0.00", 520.0)]


def _titled_shares(
    *,
    first: list[tuple[str, float]] = _FIRST_TITLE,
    second: list[tuple[str, float]] = _SECOND_TITLE,
    between: tuple[list[tuple[str, float]], ...] = (),
    under_title: tuple[list[tuple[str, float]], ...] = (),
) -> list[list[tuple[str, float]]]:
    """Two shares told apart by their opening rows: ``between`` is printed after
    the first share's rows, ``under_title`` between the second title and its header."""
    return [
        first,
        _SHARE_HEADER,
        [("03/01/2026", 62.0), ("Beginning Balance", 150.0), ("100.00", 520.0)],
        [("03/05/2026", 62.0), ("Deposit", 150.0), ("20.00", 450.0), ("120.00", 520.0)],
        *between,
        second,
        *under_title,
        _SHARE_HEADER,
        [("03/01/2026", 62.0), ("Beginning Balance", 150.0), ("300.00", 520.0)],
        [("03/07/2026", 62.0), ("Coffee", 150.0), ("4.50", 380.0), ("295.50", 520.0)],
    ]


def _shares(outcome: ParseOutcome) -> list[tuple[str | None, str | None]]:
    return [(s.account.name, s.account.last4) for s in outcome.statements]


_NAMED = [("PRIMARY SHARE", "0000"), ("SHARE DRAFT", "0040")]


def test_a_title_with_a_figure_on_its_line_names_the_share() -> None:
    outcome = _parse(_titled_shares())
    assert "accounts_by_balance" in outcome.warnings
    assert _shares(outcome) == _NAMED
    assert [[t.payee for t in s.transactions] for s in outcome.statements] == [
        ["Deposit"],
        ["Coffee"],
    ]
    # The figure beside a title is no row and no skipped line.
    assert outcome.skipped_lines == 0


@pytest.mark.parametrize(
    "total",
    [
        [("Total dividends 4.12", 62.0)],
        [("Total dividends", 62.0), ("4.12", 520.0)],
        [("Subtotal", 62.0), ("4.12", 520.0)],
    ],
    ids=["one-cell", "two-cells", "subtotal"],
)
@pytest.mark.parametrize("where", ["between", "under_title"])
def test_a_total_line_is_never_a_share_title(total: list[tuple[str, float]], where: str) -> None:
    outcome = _parse(_titled_shares(**{where: (total,)}))  # type: ignore[arg-type]
    assert _shares(outcome) == _NAMED


@pytest.mark.parametrize(
    "line",
    [
        # Beside its label, not under the balance heading: a figure there is
        # read as the running balance and the share below no longer starts over.
        [("Beginning Balance", 62.0), ("300.00", 250.0)],
        [("Ending Balance", 62.0), ("295.50", 520.0)],
        [("Statement Period", 62.0), ("03/01/2026 - 03/31/2026", 330.0)],
        [("Statement Date", 62.0), ("03/31/2026", 330.0)],
        [("Page 2 of 3", 62.0)],
        [("03/31/2026", 62.0), ("Summary of the share", 150.0)],
        [("12.50", 62.0), ("Dividends", 150.0)],
        # A label and its figure, as before: the label is no name.
        [("Dividends Paid YTD:", 62.0), ("0.00", 520.0)],
        [("Annual Percentage Yield Earned:", 62.0), ("0.05%", 520.0)],
    ],
    ids=[
        "opening",
        "closing",
        "period",
        "statement-date",
        "page",
        "date-first",
        "amount-first",
        "label-and-figure",
        "label-and-rate",
    ],
)
def test_a_labelled_line_under_a_title_is_not_the_title(line: list[tuple[str, float]]) -> None:
    outcome = _parse(_titled_shares(under_title=(line,)))
    assert _shares(outcome) == _NAMED


def test_a_title_without_a_figure_is_named_as_before() -> None:
    outcome = _parse(
        _titled_shares(
            first=[("REGULAR SAVINGS - ID 0001", 62.0)],
            second=[("Share 02: HOLIDAY CLUB", 62.0)],
        )
    )
    assert _shares(outcome) == [("REGULAR SAVINGS", "0001"), ("HOLIDAY CLUB", None)]


def test_a_row_of_the_share_above_is_never_the_title() -> None:
    # No title over the second share: the last row above it names nothing.
    rows = _titled_shares()
    rows.remove(_SECOND_TITLE)
    outcome = _parse(rows)
    assert _shares(outcome) == [("PRIMARY SHARE", "0000"), (None, None)]


# -- section totals, and rows printed newest first ------------------------------


def _doubtful(statement: Statement) -> list[int]:
    assert statement.reconciliation is not None
    return list(statement.reconciliation.doubtful or [])


def test_section_totals_that_agree_change_nothing() -> None:
    result = _read(synth.sectioned_unsigned_checking())
    statement = _only(result)
    assert _status(statement) == "ok"
    assert "section_total_mismatch" not in _warnings(result)
    assert "rows_newest_first" not in _warnings(result)
    assert not any("reconciliation" in t.low_confidence for t in statement.transactions)
    # The three totals were read, each for the section of its rows.
    parser = TableStatementParser()
    view = DocumentView.from_result(
        extract_pdf(synth.sectioned_unsigned_checking(), OPTIONS, "pdf")
    )
    rows, summary = parser._collect(parser.prepare(view.lines))[:2]
    assert {number: total.value for number, total in summary.section_totals.items()} == {
        rows[0].section: Decimal("2159.50"),
        rows[3].section: Decimal("1418.15"),
        rows[8].section: Decimal("12.00"),
    }


def test_a_section_that_does_not_add_up_is_named() -> None:
    result = _read(synth.section_total_off())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["section_total_off"]
    assert _status(statement) == "mismatch"
    assert _warnings(result).count("section_total_mismatch") == 1
    # Three deposits, the four withdrawals printed, the fee.
    assert _doubtful(statement) == [3, 4, 5, 6]
    in_doubt = ["reconciliation" in t.low_confidence for t in statement.transactions]
    assert in_doubt == [False] * 3 + [True] * 4 + [False]
    assert _skipped(result) == 0


def test_year_to_date_totals_are_not_section_checks() -> None:
    result = _read(synth.year_to_date_totals())
    statement = _only(result)
    assert _parser(statement) == "card_statement_generic"
    assert _rows(statement) == synth.EXPECTED_ROWS["year_to_date_totals"]
    assert [t.type for t in statement.transactions] == [None] * 4 + ["fee"]
    assert _status(statement) == "ok"
    assert "section_total_mismatch" not in _warnings(result)
    assert _skipped(result) == 0


def test_newest_first_statement_reconciles() -> None:
    result = _read(synth.newest_first_checking())
    statement = _only(result)
    expected = synth.EXPECTED_ROWS["newest_first_checking"]
    assert _status(statement) == "ok"
    assert statement.reconciliation is not None
    assert statement.reconciliation.running_mismatches == []
    assert _warnings(result).count("rows_newest_first") == 1
    assert statement.transactions[0].posted == min(row[0] for row in expected)
    # Oldest first, the two rows of the first day in the order they happened.
    assert _rows(statement) == expected
    assert _balances(statement) == synth.EXPECTED_BALANCES["newest_first_checking"]
    assert _skipped(result) == 0


def test_oldest_first_statement_is_not_reversed() -> None:
    stmt = synth_pdf_statements.generic_statement(days=28)
    result = _read(synth_pdf_statements.generic_pdf(stmt))
    statement = _only(result)
    assert "rows_newest_first" not in _warnings(result)
    posted = [t.posted for t in statement.transactions]
    assert len(posted) >= 3
    assert posted == sorted(posted)
    assert _status(statement) == "ok"


def _total(label: str, amount: str) -> list[tuple[str, float]]:
    return [(label, 62.0), (amount, 500.0)]


def _reconciliation(outcome: ParseOutcome) -> tuple[str, list[int]]:
    result = _one(outcome).reconciliation
    assert result is not None
    return result.status, list(result.doubtful or [])


_BALANCES = [[("Beginning Balance 1,000.00", 62.0)], [("Ending Balance 1,150.00", 62.0)]]
_TWO_SECTIONS = [
    [("Deposits", 62.0)],
    HEADER,
    _row(5, "Salary", "900.00"),
    _row(6, "Refund", "50.00"),
    _total("Total deposits", "950.00"),
    [("Withdrawals", 62.0)],
    HEADER,
    _row(7, "Rent", "700.00"),
    _row(8, "Fuel", "100.00"),
]


def test_a_total_that_disagrees_holds_a_statement_that_balances() -> None:
    # The rows add up to the closing balance; the withdrawals do not add up to
    # their total (a row read under the wrong title, or a figure misread).
    outcome = _parse([*_BALANCES, *_TWO_SECTIONS, _total("Total withdrawals", "810.00")])
    assert _amounts(outcome) == [Decimal("900.00"), Decimal("50.00"), -700, -100]
    assert outcome.warnings.count("section_total_mismatch") == 1
    assert _reconciliation(outcome) == ("mismatch", [2, 3])


def test_totals_that_agree_vouch_for_no_row() -> None:
    # Every total agrees and the balances do not: a total says how much, never
    # which way, so every row stays in doubt.
    lines = [
        [("Beginning Balance 1,000.00", 62.0)],
        [("Ending Balance 1,000.00", 62.0)],
        *_TWO_SECTIONS,
        _total("Total withdrawals", "800.00"),
    ]
    outcome = _parse(lines)
    assert "section_total_mismatch" not in outcome.warnings
    assert _reconciliation(outcome) == ("mismatch", [0, 1, 2, 3])


def test_a_gap_that_is_not_the_whole_difference_leaves_every_row_in_doubt() -> None:
    # The withdrawals are 40.00 short of their total; the statement is off by
    # 300.00. Something else is wrong too: no section is cleared.
    lines = [
        [("Beginning Balance 1,000.00", 62.0)],
        [("Ending Balance 850.00", 62.0)],
        *_TWO_SECTIONS,
        _total("Total withdrawals", "840.00"),
    ]
    outcome = _parse(lines)
    assert outcome.warnings.count("section_total_mismatch") == 1
    assert _reconciliation(outcome) == ("mismatch", [0, 1, 2, 3])


def test_the_one_row_that_is_the_whole_difference_still_wins() -> None:
    # A deposit of 100.00 too many under a total of 950.00: the row whose
    # amount is the difference is in doubt alone, as without totals.
    lines = [
        *_BALANCES,
        [("Deposits", 62.0)],
        HEADER,
        _row(5, "Salary", "900.00"),
        _row(6, "Refund", "50.00"),
        _row(6, "Stub", "100.00"),
        _total("Total deposits", "950.00"),
        [("Withdrawals", 62.0)],
        HEADER,
        _row(7, "Rent", "700.00"),
        _row(8, "Fuel", "100.00"),
        _total("Total withdrawals", "800.00"),
    ]
    outcome = _parse(lines)
    assert outcome.warnings.count("section_total_mismatch") == 1
    assert _reconciliation(outcome) == ("mismatch", [2])


@pytest.mark.parametrize(
    "label",
    [
        "Total fees year to date",
        "Total fees year-to-date",
        "Total fees YTD",
        "Total fees this year",
        "Total fees last year",
        "Total fees previous year",
        "Total fees charged in 2026",
        "Total fees 2025",
    ],
)
def test_a_total_of_another_span_of_time_is_no_section_total(label: str) -> None:
    outcome = _parse(
        [*_BALANCES, *_TWO_SECTIONS, _total(label, "36.00"), _total("Total withdrawals", "800.00")]
    )
    assert "section_total_mismatch" not in outcome.warnings
    assert _reconciliation(outcome)[0] == "ok"


@pytest.mark.parametrize(
    "line",
    [
        # Two figures, a count and no figure: none is one total.
        [("Total", 62.0), ("36.00", 430.0), ("810.00", 500.0)],
        [("Total items 4", 62.0)],
        [("Subtotal", 62.0), ("36.00", 500.0)],
        [("Totals are shown in US dollars", 62.0)],
    ],
)
def test_lines_that_are_no_total_line(line: list[tuple[str, float]]) -> None:
    outcome = _parse([*_BALANCES, *_TWO_SECTIONS, line])
    assert "section_total_mismatch" not in outcome.warnings
    assert _reconciliation(outcome)[0] == "ok"
    assert len(_one(outcome).transactions) == 4


def test_only_the_first_total_of_a_section_counts() -> None:
    outcome = _parse(
        [
            *_BALANCES,
            *_TWO_SECTIONS,
            _total("Total withdrawals", "800.00"),
            _total("Total service charges", "36.00"),
        ]
    )
    assert "section_total_mismatch" not in outcome.warnings
    assert _reconciliation(outcome)[0] == "ok"


def test_a_total_of_one_side_of_a_mixed_table_is_no_mismatch() -> None:
    # One table of money in and out, then the total of each side: the first
    # is the money out, not what the rows add up to.
    lines = [
        *_BALANCES,
        HEADER,
        _row(5, "Salary", "900.00"),
        _row(6, "Refund", "50.00"),
        _row(7, "Rent", "-700.00"),
        _row(8, "Fuel", "-100.00"),
        _total("Total debits", "800.00"),
        _total("Total credits", "950.00"),
    ]
    outcome = _parse(lines)
    assert "section_total_mismatch" not in outcome.warnings
    assert _reconciliation(outcome)[0] == "ok"
    # Neither side's sum, nor the net: the table does not add up to it.
    outcome = _parse([*lines[:-2], _total("Total debits", "815.00")])
    assert outcome.warnings.count("section_total_mismatch") == 1
    assert _reconciliation(outcome) == ("mismatch", [0, 1, 2, 3])


def test_a_total_under_pending_rows_is_ignored() -> None:
    outcome = _parse(
        [
            *_BALANCES,
            *_TWO_SECTIONS,
            _total("Total withdrawals", "800.00"),
            [("Pending transactions", 62.0)],
            _row(9, "Hold", "25.00"),
            _total("Total pending", "99.00"),
        ]
    )
    assert outcome.pending_items == 1
    assert "section_total_mismatch" not in outcome.warnings
    assert _reconciliation(outcome)[0] == "ok"


def _balance_row(day: int, payee: str, amount: str, balance: str) -> list[tuple[str, float]]:
    return [(f"01/{day:02d}/2026", 62.0), (payee, 150.0), (amount, 430.0), (balance, 520.0)]


_BALANCE_HEADER = [("Date", 62.0), ("Description", 150.0), ("Amount", 430.0), ("Balance", 520.0)]


def test_newest_first_rows_without_summary_balances_give_the_right_ones() -> None:
    outcome = _parse(
        [
            _BALANCE_HEADER,
            _balance_row(9, "Fuel", "-100.00", "1,150.00"),
            _balance_row(7, "Rent", "-700.00", "1,250.00"),
            _balance_row(5, "Salary", "900.00", "1,950.00"),
        ]
    )
    statement = _one(outcome)
    assert _amounts(outcome) == [Decimal("900.00"), Decimal("-700.00"), Decimal("-100.00")]
    assert _balances(statement) == (Decimal("1050.00"), Decimal("1150.00"))
    assert _reconciliation(outcome)[0] == "ok"
    assert outcome.warnings.count("rows_newest_first") == 1


def test_sections_in_descending_order_are_not_a_newest_first_statement() -> None:
    # One row per table, the tables from the latest day to the earliest: the
    # order of the sections, not of a statement printed backwards.
    outcome = _parse(
        [
            *_BALANCES,
            [("Fees", 62.0)],
            HEADER,
            _row(28, "Service charge", "10.00"),
            [("Withdrawals", 62.0)],
            HEADER,
            _row(15, "Rent", "740.00"),
            [("Deposits", 62.0)],
            HEADER,
            _row(5, "Salary", "900.00"),
        ]
    )
    assert "rows_newest_first" not in outcome.warnings
    assert _amounts(outcome) == [Decimal("-10.00"), Decimal("-740.00"), Decimal("900.00")]
    assert _reconciliation(outcome)[0] == "ok"


def test_two_rows_or_one_day_show_no_order() -> None:
    outcome = _parse(
        [
            _BALANCE_HEADER,
            _balance_row(9, "Fuel", "-100.00", "1,150.00"),
            _balance_row(7, "Rent", "-700.00", "1,250.00"),
        ]
    )
    assert "rows_newest_first" not in outcome.warnings


# -- the period, and a year for rows that print none ----------------------------

MARCH_END = date(2026, 3, 31)


def _pairs(days: list[date]) -> list[tuple[int, int] | None]:
    return [(day.month, day.day) for day in days]


def test_assume_years_one_month() -> None:
    days = [date(2026, 3, d) for d in (2, 5, 9, 12, 17, 20, 24, 27)]
    assert assume_years(_pairs(days), MARCH_END) == days
    assert assume_years(_pairs(days), date(2026, 3, 10)) == [d.replace(year=2025) for d in days]


def test_assume_years_over_new_year() -> None:
    days = [date(2025, 12, 27), date(2025, 12, 31), date(2026, 1, 2), date(2026, 1, 6)]
    assert assume_years(_pairs(days), date(2026, 1, 20)) == days


def test_assume_years_refuses_a_long_span() -> None:
    assert assume_years([(1, 10), (5, 10), (9, 10)], date(2026, 12, 31)) is None


def test_assume_years_leap_day() -> None:
    assert assume_years([(2, 28), (2, 29), (3, 1)], date(2026, 3, 15)) == [
        date(2026, 2, 28),
        None,
        date(2026, 3, 1),
    ]
    # In a leap year the day is real.
    assert assume_years([(2, 28), (2, 29), (3, 1)], date(2024, 3, 15)) == [
        date(2024, 2, 28),
        date(2024, 2, 29),
        date(2024, 3, 1),
    ]


def test_assume_years_ignores_printed_order() -> None:
    days = [date(2025, 12, 27), date(2025, 12, 31), date(2026, 1, 2), date(2026, 1, 6)]
    assert assume_years(_pairs(days[::-1]), date(2026, 1, 20)) == days[::-1]


def test_assume_years_keeps_the_place_of_rows_without_a_pair() -> None:
    assert assume_years([None, (3, 2), None, (3, 2)], MARCH_END) == [
        None,
        date(2026, 3, 2),
        None,
        date(2026, 3, 2),
    ]
    assert assume_years([None, None], MARCH_END) is None
    assert assume_years([], MARCH_END) is None


def test_assume_years_refuses_two_days_half_a_year_apart() -> None:
    # Nothing says which of the two comes first.
    assert assume_years([(1, 1), (7, 2)], date(2026, 12, 31)) is None
    # A day nearer, the shorter way round is the statement.
    assert assume_years([(1, 1), (7, 1)], date(2026, 12, 31)) == [
        date(2026, 1, 1),
        date(2026, 7, 1),
    ]


def _read_with(
    data: bytes,
    *,
    not_after: date | None = None,
    period: tuple[date, date, float] | None = None,
) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return parse_extraction(result, kind="statement", period=period, not_after=not_after)


def _low(statement: Statement, field: str) -> list[bool]:
    return [field in t.low_confidence for t in statement.transactions]


def test_yearless_rows_without_a_period_keep_their_rows() -> None:
    result = _read_with(synth.yearless_no_period(), not_after=MARCH_END)
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["yearless_no_period"]
    # One run of the calendar: none in the notice's 2031, none a year apart.
    assert {(t.posted.year, t.posted.month) for t in statement.transactions} == {(2026, 3)}
    assert all(_low(statement, "posted_date"))
    assert _warnings(result).count("year_assumed:2026") == 1
    assert _skipped(result) == 0
    assert _status(statement) == "ok"
    # The rows' span, and no stated period.
    assert (statement.period_start, statement.period_end) == (date(2026, 3, 2), date(2026, 3, 27))
    assert statement.confidence["period"] < 0.85


def test_rows_over_new_year_take_two_years() -> None:
    result = _read_with(synth.yearless_over_new_year(), not_after=date(2026, 1, 20))
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["yearless_over_new_year"]
    assert all(_low(statement, "posted_date"))
    assert "year_assumed:2026" in _warnings(result)
    assert _skipped(result) == 0
    assert _status(statement) == "ok"


def test_unlabelled_range_on_page_one_is_the_period() -> None:
    result = _read_with(synth.unlabelled_period_once(), not_after=MARCH_END)
    statement = _only(result)
    assert (statement.period_start, statement.period_end) == synth.ONCE_PERIOD
    assert statement.confidence["period"] == 0.75
    assert _rows(statement) == synth.EXPECTED_ROWS["unlabelled_period_once"]
    assert not any(_low(statement, "posted_date"))
    warnings = _warnings(result)
    assert "period_unlabelled" in warnings
    assert not any(w.startswith("year_assumed") for w in warnings)
    assert _status(statement) == "ok"


def test_two_different_ranges_are_no_period() -> None:
    result = _read_with(synth.two_ranges_on_page_one(), not_after=MARCH_END)
    statement = _only(result)
    warnings = _warnings(result)
    assert "period_unlabelled" not in warnings
    assert "year_assumed:2025" in warnings
    assert _rows(statement) == synth.EXPECTED_ROWS["two_ranges_on_page_one"]
    assert all(_low(statement, "posted_date"))
    assert _skipped(result) == 0


def test_statement_for_is_a_period_label() -> None:
    result = _read_with(synth.statement_for_period(), not_after=date(2026, 7, 4))
    statement = _only(result)
    assert (statement.period_start, statement.period_end) == synth.FOR_PERIOD
    assert statement.confidence["period"] == 0.95
    assert _rows(statement) == synth.EXPECTED_ROWS["statement_for_period"]
    assert not any(_low(statement, "posted_date"))
    assert not any(w.startswith("year_assumed") for w in _warnings(result))


def test_the_classifiers_period_dates_the_rows() -> None:
    hint = (date(2026, 3, 1), date(2026, 3, 31), 0.9)
    result = _read_with(synth.yearless_no_period(), not_after=date(2027, 5, 1), period=hint)
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["yearless_no_period"]
    assert (statement.period_start, statement.period_end) == hint[:2]
    assert statement.confidence["period"] == 0.9
    warnings = _warnings(result)
    assert "period_from_classifier" in warnings
    assert not any(w.startswith("year_assumed") for w in warnings)
    assert not any(_low(statement, "posted_date"))


def test_a_labelled_period_beats_the_classifiers() -> None:
    hint = (date(2025, 6, 1), date(2025, 6, 30), 0.9)
    result = _read_with(synth.statement_for_period(), period=hint)
    statement = _only(result)
    assert (statement.period_start, statement.period_end) == synth.FOR_PERIOD
    assert statement.confidence["period"] == 0.95
    assert "period_from_classifier" not in _warnings(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["statement_for_period"]


def test_without_a_ceiling_the_year_is_not_assumed() -> None:
    result = extract_pdf(synth.yearless_no_period(), OPTIONS, "pdf")
    assert result.ok, result.error
    view = DocumentView.from_result(result)
    bare = DocumentView(
        lines=view.lines,
        source_format=view.source_format,
        truncated=view.truncated,
        page_count=view.page_count,
    )
    assert bare.ceiling is None
    outcome = TableStatementParser().parse(bare, threshold=0.85)
    assert not outcome.statements or not outcome.statements[0].transactions
    assert outcome.skipped_lines == len(synth.EXPECTED_ROWS["yearless_no_period"])
    assert not any(w.startswith("year_assumed") for w in outcome.warnings)


def test_rows_with_and_without_a_year_are_not_given_one() -> None:
    view = replace(
        _view(
            [
                HEADER,
                [("03/02/2026", 62.0), ("Wren Lane Grocer", 150.0), ("-12.00", 500.0)],
                [("03/05", 62.0), ("Oxbow Hardware", 150.0), ("-8.00", 500.0)],
            ]
        ),
        created=MARCH_END,
    )
    outcome = TableStatementParser().parse(view, threshold=0.85)
    (statement,) = outcome.statements
    assert [t.posted for t in statement.transactions] == [date(2026, 3, 2)]
    assert outcome.skipped_lines == 1
    assert not any(w.startswith("year_assumed") for w in outcome.warnings)


def test_month_named_rows_take_an_assumed_year_too() -> None:
    view = replace(
        _view(
            [
                HEADER,
                [("December 30", 62.0), ("Wren Lane Grocer", 150.0), ("-12.00", 500.0)],
                [("January 4", 62.0), ("Oxbow Hardware", 150.0), ("-8.00", 500.0)],
            ]
        ),
        created=date(2026, 1, 9),
    )
    outcome = TableStatementParser().parse(view, threshold=0.85)
    (statement,) = outcome.statements
    assert [t.posted for t in statement.transactions] == [date(2025, 12, 30), date(2026, 1, 4)]
    assert all("posted_date" in t.low_confidence for t in statement.transactions)
    assert "year_assumed:2026" in outcome.warnings
    assert outcome.skipped_lines == 0


def test_rows_spread_over_the_year_are_counted_not_dated() -> None:
    view = replace(
        _view(
            [
                HEADER,
                [("01/10", 62.0), ("Wren Lane Grocer", 150.0), ("-12.00", 500.0)],
                [("05/10", 62.0), ("Oxbow Hardware", 150.0), ("-8.00", 500.0)],
                [("09/10", 62.0), ("Dovecote Cafe", 150.0), ("-3.00", 500.0)],
            ]
        ),
        created=date(2026, 12, 1),
    )
    outcome = TableStatementParser().parse(view, threshold=0.85)
    assert not outcome.statements or not outcome.statements[0].transactions
    assert outcome.skipped_lines == 3
    assert not any(w.startswith("year_assumed") for w in outcome.warnings)


def test_a_statement_date_still_dates_yearless_rows() -> None:
    view = replace(
        _view(
            [
                [("Statement Ending 03/31/2024", 62.0)],
                HEADER,
                [("03/14", 62.0), ("Wren Lane Grocer", 150.0), ("-12.00", 500.0)],
                [("03/25", 62.0), ("Oxbow Hardware", 150.0), ("-8.00", 500.0)],
            ]
        ),
        created=date(2026, 5, 1),
    )
    outcome = TableStatementParser().parse(view, threshold=0.85)
    (statement,) = outcome.statements
    assert [t.posted for t in statement.transactions] == [date(2024, 3, 14), date(2024, 3, 25)]
    assert statement.period_end == date(2024, 3, 31)
    assert not any("posted_date" in t.low_confidence for t in statement.transactions)
    assert not any(w.startswith("year_assumed") for w in outcome.warnings)


@pytest.mark.parametrize(
    "line",
    [
        "Billing Cycle: 03/01/2026 - 03/31/2026",
        "Period Covered 03/01/2026 to 03/31/2026",
        "This statement covers 03/01/2026 through 03/31/2026",
        "Activity from 03/01/2026 to 03/31/2026",
        "Account activity for March 1, 2026 - March 31, 2026",
    ],
)
def test_more_period_labels(line: str) -> None:
    outcome = _parse(
        [
            [(line, 62.0)],
            HEADER,
            [("03/02", 62.0), ("Wren Lane Grocer", 150.0), ("-12.00", 500.0)],
        ]
    )
    (statement,) = outcome.statements
    assert (statement.period_start, statement.period_end) == (date(2026, 3, 1), date(2026, 3, 31))
    assert statement.confidence["period"] == 0.95
    assert [t.posted for t in statement.transactions] == [date(2026, 3, 2)]


@pytest.mark.parametrize("label", ["Statement Issued", "Statement Ending", "Statement End Date"])
def test_more_statement_date_labels(label: str) -> None:
    outcome = _parse(
        [
            [(f"{label}: 03/31/2026", 62.0)],
            HEADER,
            [("03/02", 62.0), ("Wren Lane Grocer", 150.0), ("-12.00", 500.0)],
        ]
    )
    (statement,) = outcome.statements
    assert statement.period_end == date(2026, 3, 31)
    assert [t.posted for t in statement.transactions] == [date(2026, 3, 2)]


@pytest.mark.parametrize("label", ["Created on", "Generated"])
def test_a_creation_date_is_no_statement_date(label: str) -> None:
    outcome = _parse(
        [
            [(f"{label} 04/02/2026", 62.0)],
            HEADER,
            [("03/02", 62.0), ("Wren Lane Grocer", 150.0), ("-12.00", 500.0)],
        ]
    )
    # No ceiling on a hand-built view: counted, not dated.
    assert not outcome.statements or not outcome.statements[0].transactions
    assert outcome.skipped_lines == 1


def test_a_range_inside_a_sentence_on_page_one_is_no_period() -> None:
    view = replace(
        _view(
            [
                # Under the header band (the top eight lines), in a sentence.
                *([[("Hazelgrove Community Bank", 62.0)]] * 8),
                [("Rates were lower from January 5, 2021 through February 14, 2021.", 62.0)],
                HEADER,
                [("03/02", 62.0), ("Wren Lane Grocer", 150.0), ("-12.00", 500.0)],
            ]
        ),
        created=MARCH_END,
    )
    outcome = TableStatementParser().parse(view, threshold=0.85)
    (statement,) = outcome.statements
    assert "period_unlabelled" not in outcome.warnings
    assert [t.posted for t in statement.transactions] == [date(2026, 3, 2)]
    assert "year_assumed:2026" in outcome.warnings


# -- tables printed two or three across -------------------------------------------


def test_checks_across_the_page_are_counted() -> None:
    result = _read(synth.checks_three_across())
    statement = _only(result)
    expected = synth.EXPECTED_ROWS["checks_three_across"]
    assert _rows(statement) == expected
    assert len(statement.transactions) == 9
    checks = statement.transactions[5:]
    assert [t.payee for t in checks] == ["CHECK 2301", "CHECK 2304", "CHECK 2308", "CHECK 2311"]
    assert all(t.amount < 0 for t in checks)
    # Seven checks stand right of the left group: found, not read, counted.
    assert _skipped(result) == 7
    assert "table_across_unread:7" in _warnings(result)
    assert result.parse is not None
    # One box per line of the checks table, whatever it holds.
    assert len(result.parse.skipped_regions) == 4
    # They were not read, so the statement cannot add up: it is held.
    assert _status(statement) == "mismatch"


def test_daily_balances_across_the_page_are_neither_read_nor_counted() -> None:
    result = _read(synth.daily_balances_three_across())
    statement = _only(result)
    assert _rows(statement) == synth.EXPECTED_ROWS["daily_balances_three_across"]
    assert len(statement.transactions) == 5
    assert not {t.amount for t in statement.transactions} & set(synth.ACROSS_DAILY_BALANCES)
    assert _skipped(result) == 0
    assert not any(w.startswith("table_across_unread") for w in _warnings(result))
    assert _status(statement) == "ok"


ACROSS_HEADER = [
    ("Number", 62.0),
    ("Date Paid", 110.0),
    ("Amount", 200.0),
    ("Number", 260.0),
    ("Date Paid", 310.0),
    ("Amount", 400.0),
]


def test_only_dates_right_of_the_left_group_are_entries() -> None:
    outcome = _parse(
        [
            ACROSS_HEADER,
            # Two checks; the second one's amount could be a day and a month.
            [
                ("3101", 62.0),
                ("01/05/2026", 110.0),
                ("20.00", 200.0),
                ("3102", 260.0),
                ("01/06/2026", 310.0),
                ("4.11", 400.0),
            ],
            # One check, alone in the left group: nothing beside it.
            [("3103", 62.0), ("01/07/2026", 110.0), ("7.50", 200.0)],
            # No check at the left: a date at the right belongs to no entry.
            [("Checks paid through", 62.0), ("01/31/2026", 310.0)],
        ]
    )
    statement = _one(outcome)
    assert [(t.payee, t.amount) for t in statement.transactions] == [
        ("CHECK 3101", Decimal("-20.00")),
        ("CHECK 3103", Decimal("-7.50")),
    ]
    assert outcome.skipped_lines == 1
    assert outcome.skipped_regions == [(1, (62.0, 114.0, 420.0, 123.0))]
    assert outcome.warnings.count("table_across_unread:1") == 1


# -- a whole credit-union member statement ----------------------------------------


def test_member_statement_is_three_accounts_read_whole() -> None:
    result = _read(synth.credit_union_member_statement())
    assert [(s.account.name, s.account.last4) for s in result.statements] == [
        ("PRIMARY SHARE", "0000"),
        ("SHARE DRAFT", "0040"),
        ("HOLIDAY CLUB", "0050"),
    ]
    assert [len(s.transactions) for s in result.statements] == [2, 6, 1]
    for statement, (_, _, opening, closing, rows) in zip(
        result.statements, synth.EXPECTED_MEMBER, strict=True
    ):
        assert _rows(statement) == rows
        assert _status(statement) == "ok"
        assert (statement.period_start, statement.period_end) == synth.MEMBER_PERIOD
        assert statement.reconciliation is not None
        assert statement.reconciliation.opening == opening
        assert statement.ledger_balance is not None
        assert statement.ledger_balance.amount == closing
        # The year is the period's: no row waits on its date.
        assert not any(_low(statement, "posted_date"))
    assert sum(1 for t in result.statements[1].transactions if t.amount < 0) == 4
    warnings = _warnings(result)
    assert "accounts_by_balance" in warnings
    assert not any(w.startswith("year_assumed") for w in warnings)
    assert _skipped(result) == 0


def _scanned(rows: list[list[tuple[str, float]]]) -> DocumentView:
    """:func:`_view` as an OCR page gives it: every cell with a confidence of 0.9.

    A negative ``x`` is where the cell ends (a figure printed right-aligned).
    """
    lines = []
    for index, cells in enumerate(rows):
        y = 100.0 + 14.0 * index
        built = tuple(
            Cell(text, -x - width if x < 0 else x, y, -x if x < 0 else x + width, y + 9.0, 0.9)
            for text, x, width in ((text, x, 5.0 * len(text)) for text, x in cells)
        )
        lines.append(
            ViewLine(
                page=1, index=index, cells=built, text=" ".join(t for t, _ in cells), height=9.0
            )
        )
    return DocumentView(lines=tuple(lines), source_format="pdf", truncated=False, page_count=1)


def test_member_statement_with_a_damaged_heading_reads_its_withdrawals() -> None:
    title, opening, rows = synth.MEMBER_SHARE_DRAFT
    balances = [opening]
    for _, amount, _ in rows:
        balances.append(balances[-1] + amount)
    view = _scanned(
        [
            [("Statement For: 04/01/2026 - 04/30/2026", 300.0)],
            [(title[0], 62.0), (title[1], 300.0), (title[2], -560.0)],
            # ``Withdrawal`` as a scan gave it, its first letter lost.
            [
                ("Date Transaction Description", 62.0),
                ("ithdrawal", -400.0),
                ("Deposit", -480.0),
                ("Balance", -560.0),
            ],
            [("04/01", 62.0), ("Beginning Balance", 100.0), (f"{opening:,.2f}", -560.0)],
            *(
                [
                    (posted.strftime("%m/%d"), 62.0),
                    (payee, 100.0),
                    (f"{abs(amount):,.2f}", -400.0 if amount < 0 else -480.0),
                    (f"{balance:,.2f}", -560.0),
                ]
                for (posted, amount, payee), balance in zip(rows, balances[1:], strict=True)
            ),
            [("04/30", 62.0), ("Ending Balance", 100.0), (f"{balances[-1]:,.2f}", -560.0)],
        ]
    )
    outcome = TableStatementParser().parse(view, threshold=0.85)
    statement = _one(outcome)
    assert [(t.posted, t.amount, t.payee) for t in statement.transactions] == list(rows)
    assert [t.amount for t in statement.transactions if t.amount < 0] == [
        amount for _, amount, _ in rows if amount < 0
    ]
    assert len([t for t in statement.transactions if t.amount < 0]) == 4
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert outcome.skipped_lines == 0

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single lines of a statement read by themselves, on hand-built lines.

:mod:`app.ingestion.parsers.statement_lines` holds pure functions on a
:class:`ViewLine`: a row that starts with its date and ends with its
amount, a section title printed with its total, a line that is a count and
a total, a line that is one amount; and one function on a run of lines,
the line at which each account's summary starts. Every line here is built
from ``(text, x0)`` cells; no PDF is drawn and every figure is invented.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest

from app.ingestion.parsers.base import Cell, ViewLine
from app.ingestion.parsers.statement_lines import (
    MAX_LED_WORDS,
    TOTAL_WORDS,
    LedRow,
    amount_only,
    items_total,
    led_row,
    summary_starts,
    titled_total,
)
from app.ingestion.parsers.text import Amount, find_amounts, month_day, numeric_parts


def _line(cells: list[tuple[str, float]], index: int = 0, page: int = 1) -> ViewLine:
    """One line of ``(text, x0)`` cells, each five points wide per character."""
    y = 100.0 + 14 * index
    built = tuple(Cell(text, x0, y, x0 + 5.0 * len(text), y + 9.0) for text, x0 in cells)
    return ViewLine(
        page=page, index=index, cells=built, text=" ".join(t for t, _ in cells), height=9.0
    )


def _date_shaped(text: str) -> bool:
    """``MM/DD`` with or without a year, ``Apr 02`` or ``02 Apr``."""
    parts = numeric_parts(text)
    if parts is not None:
        first, second, year = parts
        return year is not None or (1 <= first <= 12 and 1 <= second <= 31)
    return month_day(text, "mon_d") is not None or month_day(text, "d_mon") is not None


# --- led_row -----------------------------------------------------------------


def test_led_row_by_cells() -> None:
    row = led_row(_line([("04/02", 62.0), ("Dn", 150.0), ("107.40", 500.0)]), _date_shaped)
    assert row is not None
    assert row == LedRow("04/02", ("Dn",), row.amount)
    assert row.amount.value == Decimal("107.40")
    assert not row.amount.negative_marked


def test_led_row_in_one_cell() -> None:
    row = led_row(_line([("04/02 04/03 CARD PAYMENT 7711 3302 -45.10", 62.0)]), _date_shaped)
    assert row is not None
    assert row.date == "04/02"
    assert row.description == ("CARD PAYMENT 7711 3302",)
    assert row.amount.value == Decimal("-45.10")
    assert row.amount.negative_marked


def test_led_row_with_a_named_date_and_cells_of_text() -> None:
    row = led_row(
        _line([("Apr 02", 62.0), ("Apr 03", 110.0), ("GROCER 114", 170.0), ("$18.25", 500.0)]),
        _date_shaped,
    )
    assert row is not None
    assert row.date == "Apr 02"
    assert row.description == ("GROCER 114",)
    assert row.amount.value == Decimal("18.25")


def test_led_row_keeps_the_text_of_each_cell() -> None:
    row = led_row(
        _line([("04/05 TRANSFER", 62.0), ("REF 20441", 220.0), ("250.00 CR", 500.0)]),
        _date_shaped,
    )
    assert row is not None
    assert row.date == "04/05"
    assert row.description == ("TRANSFER", "REF 20441")
    assert row.amount.value == Decimal("250.00")
    assert row.amount.credit is True


def test_led_row_whose_last_cell_ends_with_the_amount() -> None:
    # The dates are a cell of their own and the text runs into the figure.
    row = led_row(_line([("04/02 04/03", 62.0), ("CARD PAYMENT 7711 -45.10", 150.0)]), _date_shaped)
    assert row is not None
    assert row.date == "04/02"
    assert row.description == ("CARD PAYMENT 7711",)
    assert row.amount.value == Decimal("-45.10")


def _between(word: str) -> list[tuple[str, float]]:
    """A row with ``word`` in a cell of its own between an amount and a balance."""
    return [
        ("04/02", 62.0),
        ("PAYMENT", 150.0),
        ("45.10", 400.0),
        (word, 460.0),
        ("1,307.40", 500.0),
    ]


@pytest.mark.parametrize(
    "cells",
    [
        [("04/02", 62.0), ("Dn", 150.0), ("107.40", 420.0), ("1,307.40", 500.0)],
        [("04/02 CARD PAYMENT 45.10 1,307.40", 62.0)],
        [("04/02", 62.0), ("CARD PAYMENT $ 45.10", 150.0), ("1,307.40", 500.0)],
        [("04/02", 62.0), ("CARD PAYMENT", 150.0), ("45.10 CR", 420.0), ("1,307.40", 500.0)],
        # A symbol, a sign or a currency code in a cell of its own between the two.
        [("04/02", 62.0), ("PAYMENT", 150.0), ("45.10", 400.0), ("$", 460.0), ("1,307.40", 500.0)],
        [
            ("04/02", 62.0),
            ("PAYMENT", 150.0),
            ("$", 380.0),
            ("45.10", 400.0),
            ("$", 460.0),
            ("1,307.40", 500.0),
        ],
        [("04/02", 62.0), ("PAYMENT", 150.0), ("45.10", 400.0), ("-", 460.0), ("1,307.40", 500.0)],
        [("04/02", 62.0), ("PAYMENT", 150.0), ("45.10", 400.0), ("+", 460.0), ("1,307.40", 500.0)],
        [
            ("04/02", 62.0),
            ("PAYMENT", 150.0),
            ("45.10", 400.0),
            ("USD", 460.0),
            ("1,307.40", 500.0),
        ],
        [("04/02", 62.0), ("PAYMENT 45.10 $", 150.0), ("1,307.40", 500.0)],
        [("04/02 PAYMENT 45.10 USD 1,307.40", 62.0)],
        [("04/02 PAYMENT 45.10 - $ 1,307.40", 62.0)],
        [("04/02", 62.0), ("PURCHASE 12.00 EUR", 150.0), ("13.05", 500.0)],
        # Cents printed without their zero are a figure too.
        [("04/02", 62.0), ("FEE", 150.0), (".50", 420.0), ("1,307.40", 500.0)],
        [("04/02 FEE .50 1,307.40", 62.0)],
        [("04/02", 62.0), ("FEE", 150.0), ("$.50", 420.0), ("1,307.40", 500.0)],
        # Whatever one word stands between the two figures.
        [("04/02", 62.0), ("PAYMENT", 150.0), ("45.10 OD", 400.0), ("1,307.40", 500.0)],
        _between("Balance:"),
        _between("bal"),
        _between("US$"),
        _between("C$"),
        _between("usd"),
        _between("\u20b9"),
        _between("\u00a5"),
        _between("/"),
        [("04/02", 62.0), ("PAYMENT 45.10", 150.0), ("Balance 1,307.40", 460.0)],
        [("04/02", 62.0), ("PAYMENT", 150.0), ("$ 45.10", 400.0), ("bal", 460.0), ("9.00", 500.0)],
        [("04/02", 62.0), ("PAYMENT", 150.0), ("45.10 USD $", 400.0), ("1,307.40", 500.0)],
    ],
    ids=[
        "cells",
        "one-cell",
        "symbol-apart",
        "marker",
        "symbol-cell",
        "two-symbol-cells",
        "minus-cell",
        "plus-cell",
        "code-cell",
        "symbol-in-text",
        "code-in-text",
        "sign-and-symbol",
        "figure-and-code",
        "bare-cents",
        "bare-cents-one-cell",
        "bare-cents-symbol",
        "overdrawn",
        "label",
        "word",
        "letters-symbol",
        "letter-symbol",
        "code-lower",
        "rupee",
        "yen",
        "slash",
        "label-in-cell",
        "symbol-figure-word",
        "code-and-symbol",
    ],
)
def test_two_trailing_figures_are_no_led_row(cells: list[tuple[str, float]]) -> None:
    assert led_row(_line(cells), _date_shaped) is None


def test_a_currency_code_after_words_is_still_a_led_row() -> None:
    # Only a figure before the code makes two figures of the line.
    row = led_row(_line([("04/02", 62.0), ("WIRE OUT USD", 150.0), ("45.10", 500.0)]), _date_shaped)
    assert row is not None
    assert row.description == ("WIRE OUT USD",)
    assert row.amount.value == Decimal("45.10")


def test_a_figure_deep_in_the_text_is_still_a_led_row() -> None:
    # Two words or more after it, and no date: a figure of the description.
    row = led_row(
        _line([("04/02", 62.0), ("REFUND 12.00 OF ORDER", 150.0), ("4.50", 500.0)]), _date_shaped
    )
    assert row is not None
    assert row.description == ("REFUND 12.00 OF ORDER",)
    assert row.amount.value == Decimal("4.50")


def test_cents_without_a_zero_are_not_the_amount_of_a_led_row() -> None:
    # Only a figure before another is read this way: alone, the line is counted.
    assert led_row(_line([("04/02 FEE .50", 62.0)]), _date_shaped) is None
    assert led_row(_line([("04/02", 62.0), ("FEE", 150.0), (".50", 500.0)]), _date_shaped) is None


@pytest.mark.parametrize(
    "cells",
    [
        [("04/02 CHECK 1042 250.00 04/03 CHECK 1043 75.00", 62.0)],
        [
            ("04/02", 62.0),
            ("Deposit", 110.0),
            ("250.00", 200.0),
            ("04/03", 300.0),
            ("Deposit", 350.0),
            ("75.00", 500.0),
        ],
        [("04/02 FEE .50 04/03 FEE 2.00", 62.0)],
        # Balances by day printed across, with named months.
        [
            ("Apr 02", 62.0),
            ("1,200.00", 110.0),
            ("Apr 09", 200.0),
            ("1,300.00", 250.0),
            ("Apr 16", 350.0),
            ("900.00", 500.0),
        ],
        [("Apr 02 1,200.00 Apr 09 1,300.00", 62.0)],
        # The letters of a date are no description.
        [("Apr 02", 62.0), ("Apr 09", 200.0), ("Apr 16", 350.0), ("900.00", 500.0)],
        [("04/02 MAY 12 4.50", 62.0)],
    ],
    ids=[
        "checks-one-cell",
        "cells",
        "bare-cents",
        "balances-named",
        "balances-one-cell",
        "dates-only",
        "date-written-another-way",
    ],
)
def test_two_entries_side_by_side_are_no_led_row(cells: list[tuple[str, float]]) -> None:
    assert led_row(_line(cells), _date_shaped) is None


@pytest.mark.parametrize(
    "cells",
    [
        [("04/02", 62.0), ("PAYMENT", 150.0), ("CR", 460.0), ("45.10", 500.0)],
        [("04/02", 62.0), ("PAYMENT", 150.0), ("DR", 460.0), ("45.10", 500.0)],
        [("04/02", 62.0), ("PAYMENT", 150.0), ("db", 460.0), ("45.10", 500.0)],
        [("04/02", 62.0), ("PAYMENT", 150.0), ("CR 45.10", 460.0)],
        # After the figure in a cell of its own: no amount ends the line.
        [("04/02", 62.0), ("PAYMENT", 150.0), ("45.10", 460.0), ("CR", 500.0)],
    ],
    ids=["credit", "debit", "lower", "leads-the-cell", "after"],
)
def test_a_marker_apart_from_its_figure_is_no_led_row(cells: list[tuple[str, float]]) -> None:
    # A printed marker beats any direction a caller infers: the row is not
    # made without it.
    assert led_row(_line(cells), _date_shaped) is None


def test_a_marker_s_letters_inside_the_text_are_words() -> None:
    row = led_row(_line([("04/02", 62.0), ("12 MILL DR", 150.0), ("45.10", 500.0)]), _date_shaped)
    assert row is not None
    assert row.description == ("12 MILL DR",)
    assert row.amount.credit is None


def test_led_row_with_the_table_reader_s_own_dates() -> None:
    from app.ingestion.parsers.table_statement import TableStatementParser

    reader = TableStatementParser.__new__(TableStatementParser)
    shaped = reader._date_shaped
    row = led_row(_line([("04/02", 62.0), ("Dn", 150.0), ("107.40", 500.0)]), shaped)
    assert row is not None
    assert (row.date, row.description, row.amount.value) == ("04/02", ("Dn",), Decimal("107.40"))
    assert (
        led_row(_line([("04/02 CHECK 1042 250.00 04/03 CHECK 1043 75.00", 62.0)]), shaped) is None
    )
    assert (
        led_row(_line([("04/02", 62.0), ("FEE", 150.0), (".50", 420.0), ("9.40", 500.0)]), shaped)
        is None
    )
    # A limit: a word of the text shaped like a date, right after the date
    # and written the same way, is taken for the posting date. The date and
    # the amount are right; the description is shorter by that word.
    row = led_row(_line([("04/02", 62.0), ("7/11 STORE", 150.0), ("4.50", 500.0)]), shaped)
    assert row is not None
    assert (row.date, row.description, row.amount.value) == ("04/02", ("STORE",), Decimal("4.50"))


@pytest.mark.parametrize(
    ("cells", "value"),
    [
        ([("04/02", 62.0), ("PAYMENT", 150.0), ("-$", 480.0), ("45.10", 500.0)], "-45.10"),
        ([("04/02", 62.0), ("PAYMENT", 150.0), ("$", 480.0), ("45.10", 500.0)], "45.10"),
        ([("04/02", 62.0), ("PAYMENT -$", 150.0), ("45.10", 500.0)], "-45.10"),
        ([("04/02 PAYMENT -$ 45.10", 62.0)], "-45.10"),
        ([("04/02 PAYMENT $ 45.10", 62.0)], "45.10"),
        ([("04/02", 62.0), ("PAYMENT", 150.0), ("- 45.10", 500.0)], "-45.10"),
    ],
    ids=["minus-symbol-cell", "symbol-cell", "symbol-ends-text", "one-cell", "plain", "sign-cell"],
)
def test_a_symbol_apart_from_its_figure_is_the_figure_s(
    cells: list[tuple[str, float]], value: str
) -> None:
    row = led_row(_line(cells), _date_shaped)
    assert row is not None
    assert row.description == ("PAYMENT",)
    assert row.amount.value == Decimal(value)
    assert row.amount.negative_marked is (value[0] == "-")
    assert row.amount.symbol in ("$", None)


@pytest.mark.parametrize(
    "cells",
    [
        # A dash alone before the figure: a minus or a separator, no telling.
        [("04/02", 62.0), ("PAYMENT", 150.0), ("-", 480.0), ("45.10", 500.0)],
        [("04/02", 62.0), ("PAYMENT", 150.0), ("\u2212", 480.0), ("45.10", 500.0)],
        [("04/02", 62.0), ("PAYMENT -", 150.0), ("45.10", 500.0)],
        [("04/02 PAYMENT - 45.10", 62.0)],
        [("04/02 PAYMENT - 45.10 CR", 62.0)],
        # Two signs, two symbols: not one figure.
        [("04/02", 62.0), ("PAYMENT", 150.0), ("-$", 480.0), ("-45.10", 500.0)],
        [("04/02", 62.0), ("PAYMENT", 150.0), ("$", 480.0), ("$45.10", 500.0)],
        # Nothing but the symbol between the date and the figure.
        [("04/02", 62.0), ("$", 480.0), ("45.10", 500.0)],
    ],
    ids=["minus", "minus-sign", "ends-text", "one-cell", "marker", "signs", "symbols", "no-text"],
)
def test_a_sign_that_cannot_be_told_is_no_led_row(cells: list[tuple[str, float]]) -> None:
    assert led_row(_line(cells), _date_shaped) is None


def test_a_second_date_written_another_way_stays_in_the_text() -> None:
    row = led_row(_line([("04/02 MAY 12 STORE 4.50", 62.0)]), _date_shaped)
    assert row is not None
    assert row.date == "04/02"
    assert row.description == ("MAY 12 STORE",)
    assert row.amount.value == Decimal("4.50")


def test_led_row_in_one_cell_with_a_marker() -> None:
    row = led_row(_line([("04/02 PAYMENT 45.10 CR", 62.0)]), _date_shaped)
    assert row is not None
    assert row.description == ("PAYMENT",)
    assert row.amount.value == Decimal("45.10")
    assert row.amount.credit is True


def test_a_range_word_without_a_date_after_it_is_text() -> None:
    row = led_row(_line([("04/02 TO GO PIZZA 12.00", 62.0)]), _date_shaped)
    assert row is not None
    assert row.description == ("TO GO PIZZA",)
    assert row.amount.value == Decimal("12.00")


@pytest.mark.parametrize(
    "cells",
    [
        [("04/01 through 04/30 interest 1.20", 62.0)],
        [("04/01", 62.0), ("to", 100.0), ("04/30", 120.0), ("interest", 200.0), ("1.20", 500.0)],
        [("04/01 - 04/30", 62.0), ("interest", 200.0), ("1.20", 500.0)],
        [("04/01-04/30", 62.0), ("interest", 200.0), ("1.20", 500.0)],
        [("Apr 01 thru Apr 30", 62.0), ("interest", 200.0), ("1.20", 500.0)],
    ],
    ids=["through", "to", "dash", "joined", "named"],
)
def test_a_range_is_no_led_row(cells: list[tuple[str, float]]) -> None:
    assert led_row(_line(cells), _date_shaped) is None


@pytest.mark.parametrize(
    "cells",
    [
        [("04/02", 62.0), ("1,200.00", 500.0)],
        [("04/02 1,200.00", 62.0)],
        [("04/02", 62.0), ("04/03", 110.0), ("1,200.00", 500.0)],
        [("04/02", 62.0), ("7711 3302", 150.0), ("1,200.00", 500.0)],
    ],
    ids=["cells", "one-cell", "two-dates", "digits"],
)
def test_figures_without_words_are_no_led_row(cells: list[tuple[str, float]]) -> None:
    assert led_row(_line(cells), _date_shaped) is None


@pytest.mark.parametrize(
    "cells",
    [
        [("CARD PAYMENT", 62.0), ("45.10", 500.0)],  # no date
        [("Total for 04/02", 62.0), ("45.10", 500.0)],  # the date does not lead
        [("04/02", 62.0), ("CARD PAYMENT", 150.0)],  # no amount
        [("04/02", 62.0), ("CARD PAYMENT", 150.0), ("7711", 500.0)],  # a number, no amount
        [("04/02", 62.0)],
        [("45.10", 62.0)],
        # The date is in the first cell, whole.
        [("Apr", 62.0), ("02", 90.0), ("STORE", 150.0), ("4.50", 500.0)],
    ],
    ids=[
        "no-date",
        "date-inside",
        "no-amount",
        "integer",
        "date-alone",
        "amount-alone",
        "date-in-two-cells",
    ],
)
def test_other_lines_are_no_led_row(cells: list[tuple[str, float]]) -> None:
    assert led_row(_line(cells), _date_shaped) is None


def test_a_long_sentence_is_no_led_row() -> None:
    words = " ".join(["notice"] * (MAX_LED_WORDS - 2))
    at_limit = _line([(f"04/02 {words} 12.00", 62.0)])
    assert len(at_limit.text.split()) == MAX_LED_WORDS
    assert led_row(at_limit, _date_shaped) is not None
    over = _line([(f"04/02 {words} more 12.00", 62.0)])
    assert led_row(over, _date_shaped) is None


# --- titled_total ------------------------------------------------------------


def test_titled_total() -> None:
    found = titled_total(
        _line([("Withdrawals / Debits", 62.0), ("5 items totaling $912.40", 300.0)])
    )
    assert found is not None
    title, total = found
    assert title == "Withdrawals / Debits"
    assert total.value == Decimal("912.40")


@pytest.mark.parametrize(
    "cells",
    [
        [("Checks", 62.0), ("8 item(s) totaling $3,500.00", 300.0)],
        [("Checks", 62.0), ("8 item(s)", 300.0), ("totaling", 380.0), ("$3,500.00", 500.0)],
        [("Checks", 62.0), ("(8 checks) total $ 3,500.00", 300.0)],
        [("Checks", 62.0), ("3,500.00", 500.0)],
        [("Checks", 62.0), ("8 items totaling", 300.0), ("3,500.00 DR", 500.0)],
        [("Checks", 62.0), ("8 items totaling", 300.0), ("3,500.00", 500.0), ("DR", 560.0)],
    ],
    ids=["item(s)", "cells", "brackets", "figure-only", "marker", "marker-cell"],
)
def test_titled_total_shapes(cells: list[tuple[str, float]]) -> None:
    found = titled_total(_line(cells))
    assert found is not None
    assert found[0] == "Checks"
    assert found[1].value == Decimal("3500.00")


@pytest.mark.parametrize(
    "cells",
    [
        [("Ending balance", 62.0), ("1,200.00", 420.0), ("3,400.00", 500.0)],
        [("Deposits", 62.0), ("5 items totaling 912.40 1,100.00", 300.0)],
    ],
    ids=["cells", "one-cell"],
)
def test_a_label_with_two_figures_is_no_titled_total(cells: list[tuple[str, float]]) -> None:
    assert titled_total(_line(cells)) is None


@pytest.mark.parametrize(
    "cells",
    [
        [("Fees", 62.0), ("charged this period were 12.00 in all", 300.0)],
        [("Deposits", 62.0), ("5 items posted 912.40", 300.0)],
        [("Deposits", 62.0), ("5 of 7 items totaling 912.40", 300.0)],
        [("Deposits", 62.0), ("5 7 items totaling 912.40", 300.0)],
        [("Deposits", 62.0), ("5 items DR totaling 912.40", 300.0)],
    ],
    ids=["sentence", "other-word", "of", "two-counts", "marker-before-figure"],
)
def test_a_sentence_with_a_figure_is_no_titled_total(cells: list[tuple[str, float]]) -> None:
    assert titled_total(_line(cells)) is None


@pytest.mark.parametrize(
    "cells",
    [
        [("Withdrawals / Debits 5 items totaling $912.40", 62.0)],  # one cell: no title cell
        [("Withdrawals / Debits", 62.0)],  # no figure
        [("Withdrawals / Debits", 62.0), ("5 items", 300.0)],  # a count is no total
        [("Fees 12.00", 62.0), ("5 items totaling $912.40", 300.0)],  # a figure in the title
        [("04/02", 62.0), ("5 items totaling $912.40", 300.0)],  # no letters in the title
    ],
    ids=["one-cell", "no-figure", "count-only", "figure-in-title", "no-letters"],
)
def test_other_lines_are_no_titled_total(cells: list[tuple[str, float]]) -> None:
    assert titled_total(_line(cells)) is None


def test_total_words_are_the_plan_s() -> None:
    assert (
        frozenset(
            {
                "item",
                "items",
                "totaling",
                "total",
                "checks",
                "credits",
                "debits",
                "deposits",
                "withdrawals",
            }
        )
        == TOTAL_WORDS
    )


# --- items_total -------------------------------------------------------------


@pytest.mark.parametrize(
    ("cells", "value"),
    [
        ([("5 items totaling $912.40", 62.0)], "912.40"),
        ([("8 item(s) totaling $3,500.00", 62.0)], "3500.00"),
        ([("1 item totaling 12.00", 62.0)], "12.00"),
        ([("12 Items Totaling", 62.0), ("$ 40.05", 500.0)], "40.05"),
        ([("3", 62.0), ("items", 80.0), ("totaling", 120.0), ("-7.50", 500.0)], "-7.50"),
    ],
    ids=["items", "item(s)", "item", "cells", "word-cells"],
)
def test_items_total(cells: list[tuple[str, float]], value: str) -> None:
    found = items_total(_line(cells))
    assert found is not None
    assert found.value == Decimal(value)


@pytest.mark.parametrize(
    "text",
    [
        "5 items totaling",
        "items totaling 912.40",
        "Checks 5 items totaling 912.40",
        "5 items totaling 912.40 and more",
        "5 items totaling 912.40 1,100.00",
        "5 items 912.40",
        "5 items totaling 912",
        "04/02 items totaling 912.40",
    ],
)
def test_other_lines_are_no_items_total(text: str) -> None:
    assert items_total(_line([(text, 62.0)])) is None


# --- amount_only -------------------------------------------------------------


@pytest.mark.parametrize(
    ("cells", "value"),
    [
        ([("107.40", 500.0)], "107.40"),
        ([("$1,200.00", 500.0)], "1200.00"),
        ([("-45.10", 500.0)], "-45.10"),
        ([("(12.00)", 500.0)], "-12.00"),
        ([("45.10 CR", 500.0)], "45.10"),
        ([("$", 480.0), ("45.10", 500.0)], "45.10"),
    ],
    ids=["plain", "symbol", "minus", "brackets", "marker", "symbol-cell"],
)
def test_amount_only(cells: list[tuple[str, float]], value: str) -> None:
    found = amount_only(_line(cells))
    assert found is not None
    assert found.value == Decimal(value)


@pytest.mark.parametrize(
    "text",
    ["04/02", "12", "Total 12.00", "12.00 13.00", "12.00 paid", "7711 12.00"],
)
def test_other_lines_are_no_amount_only(text: str) -> None:
    assert amount_only(_line([(text, 62.0)])) is None


# --- summary_starts ----------------------------------------------------------


def _labelled(label: str) -> Callable[[ViewLine], Amount | None]:
    """The one figure of a line that starts with ``label``, as a caller's reader would."""

    def read(line: ViewLine) -> Amount | None:
        if not line.text.lower().startswith(label):
            return None
        found = find_amounts(line.text)
        return found[0] if len(found) == 1 else None

    return read


_OPENING = _labelled("beginning balance")
_CLOSING = _labelled("ending balance")


def _page(texts: list[str], page: int = 1) -> list[ViewLine]:
    """One page of one-cell lines, indexed from 0 on the page."""
    return [_line([(text, 62.0)], index, page) for index, text in enumerate(texts)]


def _starts(lines: list[ViewLine], table: frozenset[int] = frozenset()) -> list[int]:
    return summary_starts(lines, _OPENING, _CLOSING, lambda index: index in table)


_FIRST = [
    "Harbour Lantern Savings Bank",
    "Account Number: 0000",
    "CHEQUING SUMMARY",
    "Beginning Balance 2,410.00",
    "Deposits and Additions 615.25",
    "Withdrawals 402.10",
    "Ending Balance 2,623.15",
    "Date Description Amount",
    "04/02 PAYROLL 615.25",
    "04/09 RENT 402.10",
]
_SECOND = [
    "RESERVE SUMMARY",
    "Beginning Balance 18,300.00",
    "Interest 4.75",
    "Ending Balance 18,304.75",
]
#: The lines of ``_FIRST`` that are its table (header and rows).
_FIRST_TABLE = frozenset({7, 8, 9})


def test_two_summaries_are_two_starts() -> None:
    lines = _page(_FIRST) + _page(_SECOND, page=2)
    assert _starts(lines, _FIRST_TABLE) == [2, 10]
    assert [lines[at].text for at in _starts(lines, _FIRST_TABLE)] == [
        "CHEQUING SUMMARY",
        "RESERVE SUMMARY",
    ]


def test_two_summaries_on_one_page_are_two_starts() -> None:
    lines = _page(_FIRST + _SECOND)
    assert _starts(lines, _FIRST_TABLE) == [2, 10]


def test_three_summaries_are_three_starts() -> None:
    third = ["HOLIDAY SUMMARY", "Beginning Balance 90.00", "Ending Balance 140.00"]
    lines = _page(_FIRST) + _page(_SECOND, page=2) + _page(third, page=3)
    assert _starts(lines, _FIRST_TABLE) == [2, 10, 14]


def test_the_same_summary_printed_again_is_no_start() -> None:
    again = ["CHEQUING SUMMARY", "Beginning Balance 2,410.00", "Ending Balance 2,623.15"]
    assert _starts(_page(_FIRST) + _page(again, page=2), _FIRST_TABLE) == []
    # The account after it still starts: its opening figure is another one.
    lines = _page(_FIRST) + _page(again, page=2) + _page(_SECOND, page=3)
    assert _starts(lines, _FIRST_TABLE) == [2, 13]


def test_a_repeat_of_the_opening_line_alone_keeps_what_was_closed() -> None:
    # The head of the next page prints the opening line again and no closing
    # one: the account met after it still starts.
    again = ["CHEQUING SUMMARY", "Beginning Balance 2,410.00"]
    lines = _page(_FIRST) + _page(again, page=2) + _page(_SECOND, page=3)
    assert _starts(lines, _FIRST_TABLE) == [2, 12]


def test_an_opening_line_is_not_a_closing_line_too() -> None:
    # A caller's closing reader that also knows the opening line: the line
    # opens, it closes nothing.
    texts = [
        "CHEQUING SUMMARY",
        "Beginning Balance 2,410.00",
        "RESERVE SUMMARY",
        "Beginning Balance 18,300.00",
    ]
    either = _labelled("beginning balance")
    assert summary_starts(_page(texts), _OPENING, either, lambda index: False) == []


def test_an_opening_without_a_closing_before_it_is_no_start() -> None:
    # A second opening figure under the first, with no closing between them
    # (an overview of balances, a restated figure): one account.
    texts = [
        "CHEQUING SUMMARY",
        "Beginning Balance 2,410.00",
        "Deposits and Additions 615.25",
        "Beginning Balance 18,300.00",
        "Ending Balance 2,623.15",
    ]
    assert _starts(_page(texts)) == []
    # After the one closing, the next differing opening starts an account;
    # the one after that, with no closing between, does not.
    more = [*texts, "RESERVE SUMMARY", "Beginning Balance 18,300.00", "Beginning Balance 77.00"]
    assert _starts(_page(more)) == [0, 5]


def test_opening_lines_inside_a_table_are_ignored() -> None:
    # The second account's balances are rows of its table: no summary of its own.
    table = [
        "Date Description Amount Balance",
        "Beginning Balance 18,300.00",
        "04/30 INTEREST 4.75 18,304.75",
        "Ending Balance 18,304.75",
    ]
    lines = _page(_FIRST) + _page(table, page=2)
    inside = _FIRST_TABLE | frozenset(range(10, 14))
    assert _starts(lines, inside) == []
    # A closing line inside a table closes nothing either.
    texts = [
        "CHEQUING SUMMARY",
        "Beginning Balance 2,410.00",
        "Date Description Amount Balance",
        "Ending Balance 2,623.15",
        "RESERVE SUMMARY",
        "Beginning Balance 18,300.00",
    ]
    assert _starts(_page(texts), frozenset({2, 3})) == []
    assert _starts(_page(texts)) == [0, 4]


def test_one_summary_gives_nothing() -> None:
    assert _starts(_page(_FIRST), _FIRST_TABLE) == []
    assert _starts([]) == []
    assert _starts(_page(["Harbour Lantern Savings Bank", "Page 1 of 2"])) == []


def test_the_start_is_the_nearest_title_within_three_lines() -> None:
    def second(above: list[str]) -> list[int]:
        return _starts(_page([*_FIRST, *above, "Beginning Balance 18,300.00"]), _FIRST_TABLE)

    # No line above it: the opening line itself (index 10) is the start.
    assert second([]) == [2, 10]
    # Lines with a digit are passed over; the nearest line of letters wins.
    assert second(["RESERVE SUMMARY", "Account Number: 0000", "Period 04/01/2026"]) == [2, 10]
    assert second(["Reserve accounts", "RESERVE SUMMARY", "Account Number: 0000"]) == [2, 11]
    # Four lines above is too far: the start stays on the opening line.
    far = ["RESERVE SUMMARY", "Account Number: 0000", "Period 04/01/2026", "Rate 1.10%"]
    assert second(far) == [2, 14]
    # Nothing at or above the closing line met before it is its title.
    closed = [*_FIRST[:6], "Totals", "Ending Balance 2,623.15", "Beginning Balance 18,300.00"]
    assert _starts(_page(closed)) == [2, 8]
    # A line of a table is no title, and nothing above a table is one.
    rows = [*_FIRST[:9], "CONTINUED", "Beginning Balance 18,300.00"]
    assert _starts(_page(rows), frozenset({7, 8, 9})) == [2, 10]


def test_the_title_is_looked_for_on_the_opening_line_s_page() -> None:
    # Page 2 begins with the opening line: the last line of page 1 is not its title.
    first = [*_FIRST, "Thank you for banking with us"]
    lines = _page(first) + _page(["Beginning Balance 18,300.00", "Ending Balance 18,304.75"], 2)
    assert _starts(lines, _FIRST_TABLE) == [2, 11]


def test_a_summary_printed_again_after_another_account_s_is_a_start() -> None:
    # A limit, pinned: an opening figure is compared with the last start's
    # only (two accounts may open with the same figure, 0.00 often), so the
    # first summary printed again after the second one's is a third start.
    # The caller tells it by its equal opening and closing figures.
    again = ["CHEQUING SUMMARY", "Beginning Balance 2,410.00", "Ending Balance 2,623.15"]
    lines = _page(_FIRST) + _page(_SECOND, page=2) + _page(again, page=3)
    assert _starts(lines, _FIRST_TABLE) == [2, 10, 14]


def test_any_line_of_letters_above_the_opening_line_is_taken_for_its_title() -> None:
    # A limit, pinned: the function knows no titles. A sentence printed
    # under the closing line of the account above is the start; whether the
    # start line is a title is the caller's to ask.
    lines = _page([*_FIRST, "Thank you for banking with us", "Beginning Balance 18,300.00"])
    assert _starts(lines, _FIRST_TABLE) == [2, 10]
    assert lines[10].text == "Thank you for banking with us"

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single lines of a statement read by themselves, on hand-built lines.

:mod:`app.ingestion.parsers.statement_lines` holds pure functions on a
:class:`ViewLine`: a row that starts with its date and ends with its
amount, a section title printed with its total, a line that is a count and
a total, a line that is one amount. Every line here is built from
``(text, x0)`` cells; no PDF is drawn and every figure is invented.
"""

from __future__ import annotations

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
    titled_total,
)
from app.ingestion.parsers.text import month_day, numeric_parts


def _line(cells: list[tuple[str, float]], index: int = 0) -> ViewLine:
    """One line of ``(text, x0)`` cells, each five points wide per character."""
    y = 100.0 + 14 * index
    built = tuple(Cell(text, x0, y, x0 + 5.0 * len(text), y + 9.0) for text, x0 in cells)
    return ViewLine(
        page=1, index=index, cells=built, text=" ".join(t for t, _ in cells), height=9.0
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


@pytest.mark.parametrize(
    "cells",
    [
        [("04/02", 62.0), ("Dn", 150.0), ("107.40", 420.0), ("1,307.40", 500.0)],
        [("04/02 CARD PAYMENT 45.10 1,307.40", 62.0)],
        [("04/02", 62.0), ("CARD PAYMENT $ 45.10", 150.0), ("1,307.40", 500.0)],
        [("04/02", 62.0), ("CARD PAYMENT", 150.0), ("45.10 CR", 420.0), ("1,307.40", 500.0)],
    ],
    ids=["cells", "one-cell", "symbol-apart", "marker"],
)
def test_two_trailing_figures_are_no_led_row(cells: list[tuple[str, float]]) -> None:
    assert led_row(_line(cells), _date_shaped) is None


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
    ],
    ids=["no-date", "date-inside", "no-amount", "integer", "date-alone", "amount-alone"],
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
    ],
    ids=["item(s)", "cells", "brackets", "figure-only"],
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
    ],
    ids=["sentence", "other-word", "two-counts"],
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

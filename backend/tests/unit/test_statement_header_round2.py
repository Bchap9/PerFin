# SPDX-License-Identifier: AGPL-3.0-or-later
"""Header recognition, round two: a grid of labels and values, a rule glyph.

Hand-built lines only, as in ``test_statement_header.py``: every line is
made of ``(text, x0)`` cells and nothing but headings is printed.
"""

from __future__ import annotations

import math

import pytest

from app.ingestion.parsers.base import Cell, ViewLine
from app.ingestion.parsers.statement_header import (
    Column,
    Role,
    across_start,
    header_like,
    heading_role,
    read_header,
    split_heading_cell,
)


def _line(cells: list[tuple[str, float]], index: int = 0) -> ViewLine:
    """One line of ``(text, x0)`` cells, each five points wide per character."""
    y = 100.0 + 14 * index
    built = tuple(Cell(text, x0, y, x0 + 5.0 * len(text), y + 9.0) for text, x0 in cells)
    return ViewLine(
        page=1, index=index, cells=built, text=" ".join(t for t, _ in cells), height=9.0
    )


# -- a grid of labels and values ----------------------------------------------------


def test_description_and_amount_pairs_are_no_header() -> None:
    """``Date | Description | Amount | Description | Amount`` heads a summary
    grid (a label and its figure, twice across): no transaction table, and
    still a line of headings that ends the table above it."""
    grid = _line(
        [
            ("Date", 40.0),
            ("Description", 100.0),
            ("Amount", 250.0),
            ("Description", 330.0),
            ("Amount", 500.0),
        ]
    )
    assert across_start(grid) is None
    assert read_header(grid) is None
    assert header_like(grid)
    # The same when the extraction made one cell of each pair, or of all four.
    paired = _line([("Date", 40.0), ("Description Amount", 100.0), ("Description Amount", 330.0)])
    assert read_header(paired) is None
    assert header_like(paired)
    one_cell = _line([("Date", 40.0), ("Description Amount Description Amount", 100.0)])
    assert read_header(one_cell) is None
    assert header_like(one_cell)
    # Other words for the text column are the same column named twice.
    worded = _line([("Date", 40.0), ("Details", 100.0), ("Amount", 250.0), ("Particulars", 330.0)])
    assert read_header(worded) is None
    assert header_like(worded)


def test_one_description_heading_is_still_a_header() -> None:
    """The negative of the rule: the text column named once, whatever else
    is named twice (a guard: these read today)."""
    line = _line([("Date", 40.0), ("Description", 100.0), ("Amount", 250.0), ("Amount", 400.0)])
    assert read_header(line) == [
        Column("date", 40.0, 60.0),
        Column("description", 100.0, 155.0),
        Column("amount", 250.0, 280.0),
    ]
    # ``Transaction Description`` in one cell is one heading, not two.
    phrase = _line([("Date", 40.0), ("Transaction Description", 100.0), ("Amount", 330.0)])
    assert read_header(phrase) == [
        Column("date", 40.0, 60.0),
        Column("description", 100.0, 215.0),
        Column("amount", 330.0, 360.0),
    ]


def test_two_date_headings_are_still_a_header() -> None:
    line = _line(
        [
            ("Transaction Date", 40.0),
            ("Posting Date", 150.0),
            ("Description", 250.0),
            ("Amount", 480.0),
        ]
    )
    assert read_header(line) == [
        Column("date", 40.0, 120.0),
        Column("post_date", 150.0, 210.0),
        Column("description", 250.0, 305.0),
        Column("amount", 480.0, 510.0),
    ]


def test_a_table_across_is_still_read() -> None:
    line = _line(
        [
            ("Number", 40.0),
            ("Date Paid", 100.0),
            ("Amount", 180.0),
            ("Number", 300.0),
            ("Date Paid", 360.0),
            ("Amount", 440.0),
        ]
    )
    assert across_start(line) == 300.0
    assert read_header(line) == [
        Column("check", 40.0, 70.0),
        Column("date", 100.0, 145.0),
        Column("amount", 180.0, 210.0),
        Column("across", 300.0, math.inf),
    ]


def test_a_description_table_across_is_still_read() -> None:
    """``Date | Description | Amount`` printed twice is a table across the
    page, not a grid: the description is named once per group."""
    line = _line(
        [
            ("Date", 40.0),
            ("Description", 100.0),
            ("Amount", 220.0),
            ("Date", 300.0),
            ("Description", 360.0),
            ("Amount", 480.0),
        ]
    )
    assert read_header(line) == [
        Column("date", 40.0, 60.0),
        Column("description", 100.0, 155.0),
        Column("amount", 220.0, 250.0),
        Column("across", 300.0, math.inf),
    ]


# -- a stray glyph after a unit -----------------------------------------------------


@pytest.mark.parametrize(
    ("printed", "role"),
    [
        ("AMOUNT (EUR)I", "amount"),
        ("Balance (USD)|", "balance"),
        ("Withdrawals [CAD]l", "debit"),
        ("Deposits (GBP)! ", "credit"),
    ],
)
def test_rule_glyph_after_a_unit(printed: str, role: Role) -> None:
    assert heading_role(printed) == role


def test_rule_glyph_header_is_read() -> None:
    line = _line(
        [("Date", 40.0), ("Details", 110.0), ("AMOUNT (EUR)I", 330.0), ("Balance (EUR)|", 440.0)]
    )
    assert read_header(line) == [
        Column("date", 40.0, 60.0),
        Column("description", 110.0, 145.0),
        Column("amount", 330.0, 395.0),
        Column("balance", 440.0, 510.0),
    ]
    # Both in one cell, each with its unit and its glyph.
    cell = _line([("Amount (EUR)I Balance (EUR)I", 330.0)]).cells[0]
    assert [(r, c.text) for r, c in split_heading_cell(cell) or ()] == [
        ("amount", "Amount (EUR)I"),
        ("balance", "Balance (EUR)I"),
    ]


def test_a_letter_after_a_heading_is_no_unit() -> None:
    for printed in (
        "Amount I",
        "Balance (EUR) due",
        "Amount (EUR) I",  # not directly behind the bracket
        "Amount (EUR)II",  # one glyph, not two
        "Amount (EUR)x",
        "Amount (EUR)1",
        "AmountI",
        "Description (EUR)I",  # a text heading keeps its words
        "(EUR)I",
    ):
        assert heading_role(printed) is None, printed

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Header recognition, round two: a grid of labels and values, a rule glyph,
the column groups of a table printed across the page.

Hand-built lines only, as in ``test_statement_header.py``: every line is
made of ``(text, x0)`` cells and nothing but headings is printed.
"""

from __future__ import annotations

import itertools
import math

import pytest

from app.ingestion.parsers.base import Cell, ViewLine
from app.ingestion.parsers.statement_header import (
    Column,
    Role,
    across_groups,
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
    # Other words for the text column, and no figure after the second label:
    # the money heading still stands between the two.
    worded = _line([("Date", 40.0), ("Details", 100.0), ("Amount", 250.0), ("Particulars", 330.0)])
    assert read_header(worded) is None
    assert header_like(worded)
    # Any money heading between them, and more figures beside each label.
    wide = _line(
        [
            ("Date", 40.0),
            ("Description", 100.0),
            ("Withdrawals", 200.0),
            ("Balance", 270.0),
            ("Description", 330.0),
            ("Amount", 430.0),
            ("Balance", 500.0),
        ]
    )
    assert read_header(wide) is None
    assert header_like(wide)


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


@pytest.mark.parametrize(
    ("cells", "roles", "text_x0"),
    [
        pytest.param(
            [("Date", 40.0), ("Payee", 100.0), ("Description", 220.0), ("Amount", 480.0)],
            ["date", "description", "amount"],
            100.0,
            id="payee-description",
        ),
        pytest.param(
            [
                ("Date", 40.0),
                ("Description", 100.0),
                ("Details", 220.0),
                ("Fee", 400.0),
                ("Amount", 480.0),
            ],
            ["date", "description", "amount"],
            100.0,
            id="description-details-fee",
        ),
        pytest.param(
            [
                ("Date", 40.0),
                ("Type", 90.0),
                ("Details", 140.0),
                ("Particulars", 220.0),
                ("Code", 300.0),
                ("Reference", 350.0),
                ("Amount", 440.0),
                ("Balance", 520.0),
            ],
            ["date", "description", "amount", "balance"],
            140.0,
            id="details-particulars-code-reference",
        ),
        pytest.param(
            [("Trans Date", 40.0), ("Transaction", 120.0), ("Merchant", 240.0), ("Amount", 480.0)],
            ["date", "description", "amount"],
            120.0,
            id="transaction-merchant",
        ),
        pytest.param(
            [("Transaction", 40.0), ("Date", 120.0), ("Description", 200.0), ("Amount", 480.0)],
            ["description", "date", "amount"],
            40.0,
            id="text-date-text",
        ),
        pytest.param(
            [
                ("Date", 40.0),
                ("Transaction", 100.0),
                ("Description", 200.0),
                ("Debit", 400.0),
                ("Credit", 460.0),
                ("Balance", 520.0),
            ],
            ["date", "description", "debit", "credit", "balance"],
            100.0,
            id="two-texts-debit-credit",
        ),
        pytest.param(
            [("Date", 40.0), ("Payee Description", 100.0), ("Amount", 480.0)],
            ["date", "description", "amount"],
            100.0,
            id="two-texts-in-one-cell",
        ),
    ],
)
def test_text_headings_side_by_side_are_still_a_header(
    cells: list[tuple[str, float]], roles: list[Role], text_x0: float
) -> None:
    """The negative of the grid rule: two text headings with no money heading
    between them are the text columns of one table (a payee and what it was
    for), and the header reads as it always did, the first one named being
    the description column."""
    header = read_header(_line(cells))
    assert header is not None
    assert [c.role for c in header if c.role != "other"] == roles
    assert next(c.x0 for c in header if c.role == "description") == text_x0


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


# -- the groups of a table printed across the page ----------------------------------

_THREE_ACROSS = [
    ("Number", 40.0),
    ("Date Paid", 100.0),
    ("Amount", 180.0),
    ("Number", 240.0),
    ("Date Paid", 300.0),
    ("Amount", 380.0),
    ("Number", 440.0),
    ("Date Paid", 500.0),
    ("Amount", 580.0),
]


def test_groups_of_a_table_across() -> None:
    """``Number | Date Paid | Amount`` three times: one list of columns per
    printed group, left to right, each column with its own heading's span."""
    line = _line(_THREE_ACROSS)
    groups = across_groups(line)
    assert groups is not None
    assert [[c.role for c in group] for group in groups] == [["check", "date", "amount"]] * 3
    assert groups[1] == [
        Column("check", 240.0, 270.0),
        Column("date", 300.0, 345.0),  # the second ``Date Paid`` cell's span
        Column("amount", 380.0, 410.0),
    ]
    assert groups[2][0] == Column("check", 440.0, 470.0)
    assert groups[2][2] == Column("amount", 580.0, 610.0)
    # ``read_header`` reads as before: the first group, then everything beside it.
    header = read_header(line)
    assert header == [*groups[0], Column("across", 240.0, math.inf)]
    # Twice across is two groups.
    twice = across_groups(_line(_THREE_ACROSS[:6]))
    assert twice is not None
    assert [[c.role for c in group] for group in twice] == [["check", "date", "amount"]] * 2
    # No column of one group reaches into the next.
    for left, right in itertools.pairwise(groups):
        assert max(c.x1 for c in left) <= min(c.x0 for c in right)


def test_a_role_named_twice_in_a_group_keeps_its_first_heading() -> None:
    """``Date | Payee | Description | Amount`` printed twice: each group's
    description column is its own first text heading, as in a plain header
    and in :func:`read_header`'s first group."""
    line = _line(
        [
            ("Date", 40.0),
            ("Payee", 90.0),
            ("Description", 140.0),
            ("Amount", 220.0),
            ("Date", 300.0),
            ("Payee", 350.0),
            ("Description", 400.0),
            ("Amount", 480.0),
        ]
    )
    groups = across_groups(line)
    assert groups == [
        [
            Column("date", 40.0, 60.0),
            Column("description", 90.0, 115.0),
            Column("amount", 220.0, 250.0),
        ],
        [
            Column("date", 300.0, 320.0),
            Column("description", 350.0, 375.0),
            Column("amount", 480.0, 510.0),
        ],
    ]
    assert read_header(line) == [*groups[0], Column("across", 300.0, math.inf)]


def test_groups_of_one_cell_headings() -> None:
    """The last heading of a group printed so close to the first of the next
    that they are one cell (``Amount Date``, twice): each heading has the
    span of its own words, and each goes to its own group."""
    line = _line(
        [
            ("Date", 40.0),
            ("Description", 90.0),
            ("Amount Date", 200.0),
            ("Description", 290.0),
            ("Amount Date", 400.0),
            ("Description", 490.0),
            ("Amount", 600.0),
        ]
    )
    assert across_groups(line) == [
        [
            Column("date", 40.0, 60.0),
            Column("description", 90.0, 145.0),
            Column("amount", 200.0, 230.0),
        ],
        [
            Column("date", 235.0, 255.0),
            Column("description", 290.0, 345.0),
            Column("amount", 400.0, 430.0),
        ],
        [
            Column("date", 435.0, 455.0),
            Column("description", 490.0, 545.0),
            Column("amount", 600.0, 630.0),
        ],
    ]
    # A whole group printed as one cell.
    packed = _line(
        [
            ("Number", 40.0),
            ("Date Paid", 100.0),
            ("Amount", 180.0),
            ("Number Date Paid Amount", 240.0),
        ]
    )
    assert across_groups(packed) == [
        [Column("check", 40.0, 70.0), Column("date", 100.0, 145.0), Column("amount", 180.0, 210.0)],
        [
            Column("check", 240.0, 270.0),
            Column("date", 275.0, 320.0),
            Column("amount", 325.0, 355.0),
        ],
    ]


def test_a_plain_header_has_no_groups() -> None:
    plain = _line([("Date", 40.0), ("Description", 100.0), ("Amount", 300.0), ("Balance", 400.0)])
    assert read_header(plain) is not None
    assert across_groups(plain) is None
    # One group and the start of a second: not printed twice.
    uneven = _line([*_THREE_ACROSS[:3], ("Number", 240.0), ("Date Paid", 300.0)])
    assert across_groups(uneven) is None
    # A cell that is no heading beside the groups.
    assert across_groups(_line([*_THREE_ACROSS[:6], ("Notes", 640.0)])) is None
    # A line of entries, not of headings.
    entries = _line([("3051", 40.0), ("03/04", 100.0), ("66.00", 180.0), ("3052", 240.0)])
    assert across_groups(entries) is None
    # The grid of labels and values is no table across.
    grid = _line(
        [
            ("Date", 40.0),
            ("Description", 100.0),
            ("Amount", 250.0),
            ("Description", 330.0),
            ("Amount", 500.0),
        ]
    )
    assert across_groups(grid) is None


def test_date_balance_pairs_have_no_groups() -> None:
    """Balances by day, printed across: no table of transactions, so no
    groups to read entries by, whichever word heads the figures."""
    balances = _line([("Date", 40.0), ("Balance", 100.0), ("Date", 220.0), ("Balance", 280.0)])
    assert across_groups(balances) is None
    amounts = _line([("Date", 40.0), ("Amount", 100.0), ("Date", 220.0), ("Amount", 280.0)])
    assert across_start(amounts) == 220.0
    assert read_header(amounts) is None
    assert across_groups(amounts) is None
    one_cell = _line([("Date Balance Date Balance", 40.0)])
    assert across_groups(one_cell) is None

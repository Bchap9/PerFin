# SPDX-License-Identifier: AGPL-3.0-or-later
"""Amounts and dates in the shapes statements print (values invented).

A debit marker ``DB``, a sign or a currency symbol standing one space away
from its figure, a gap before the decimal point, a footnote mark after the
figure, and a month-named date with a two-digit year. Each shape has its
negative beside it: what must still not be read as money or as a date.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.ingestion.parsers.text import find_amounts, read_amount, read_date


@pytest.mark.parametrize(("text", "value"), [("4,120.00 DB", "4120.00"), ("75.10Db", "75.10")])
def test_debit_marker_db(text: str, value: str) -> None:
    amount = read_amount(text)
    assert amount is not None
    assert amount.value == Decimal(value)
    assert amount.credit is False


def test_detached_symbol() -> None:
    amount = read_amount("$ 1,480.25")
    assert amount is not None
    assert amount.value == Decimal("1480.25")
    assert amount.symbol == "$"
    assert not amount.negative_marked


@pytest.mark.parametrize("text", ["- $310.00", "-$ 310.00", "- 310.00", "\u2212 310.00"])
def test_detached_minus_sign(text: str) -> None:
    amount = read_amount(text)
    assert amount is not None
    assert amount.value == Decimal("-310.00")
    assert amount.negative_marked


@pytest.mark.parametrize(
    ("text", "detached"),
    [
        ("- $310.00", True),
        ("- 310.00", True),
        ("\u2212 310.00", True),
        ("+ 25.00", True),
        ("- $ 310.00", True),
        # The sign touches the symbol or the figure: it is the figure's own.
        ("-$310.00", False),
        ("-$ 310.00", False),
        ("-310.00", False),
        ("$-310.00", False),
        ("310.00-", False),
        ("(310.00)", False),
        ("$ 310.00", False),
        ("310.00", False),
    ],
)
def test_a_sign_standing_apart_is_told_from_a_sign_on_the_figure(text: str, detached: bool) -> None:
    amount = read_amount(text)
    assert amount is not None
    assert amount.sign_detached is detached


def test_detached_plus_sign() -> None:
    amount = read_amount("+ 25.00")
    assert amount is not None
    assert amount.value == Decimal("25.00")
    assert amount.positive_marked
    assert not amount.negative_marked


@pytest.mark.parametrize(
    ("text", "value"), [("7,310 .00", "7310.00"), ("$12 ,040 .50", "12040.50")]
)
def test_gap_before_the_decimal_point(text: str, value: str) -> None:
    amount = read_amount(text)
    assert amount is not None
    assert amount.value == Decimal(value)


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("642.18*", "642.18"),
        ("642.18 *", "642.18"),
        ("642.18†", "642.18"),
        ("642.18¹", "642.18"),
        ("642.18\u2021", "642.18"),
        ("642.18\u00a7", "642.18"),
        ("642.18\u00b2", "642.18"),
        ("642.18 \u00b3", "642.18"),
        ("18.00-*", "-18.00"),
    ],
)
def test_footnote_mark_after_the_figure(text: str, value: str) -> None:
    amount = read_amount(text)
    assert amount is not None
    assert amount.value == Decimal(value)


@pytest.mark.parametrize(
    "text",
    [
        "*",
        "- -5.00",
        "5 .0",
        "12 34.00",
        "1,23.00",
        "(5.00",
        # One mark, one space: no more of either.
        "642.18**",
        "642.18  *",
        "-  310.00",
        "$  310.00",
        # A gap is closed before a separator, never after one or between digits.
        "7,310. 00",
        "7, 310.00",
        # Parentheses already say negative.
        "(- 5.00)",
        # A marker letter pair that is neither credit nor debit.
        "75.10 DC",
    ],
)
def test_still_not_amounts(text: str) -> None:
    assert read_amount(text) is None


def test_find_amounts_joins_a_lone_symbol() -> None:
    found = find_amounts("Ending balance $ 2,010.40")
    assert [(a.value, a.symbol) for a in found] == [(Decimal("2010.40"), "$")]
    assert [a.value for a in find_amounts("Fee - $12.00")] == [Decimal("-12.00")]
    # A sign, a lone symbol and the figure: three tokens, one amount.
    assert [a.value for a in find_amounts("Fee - $ 12.00 charged")] == [Decimal("-12.00")]


def test_find_amounts_says_when_the_sign_stood_apart() -> None:
    """``Label - $x`` may print a separator, ``Label -$x`` prints a sign: the
    reader of a balance line is told which (values invented)."""
    for line in ("Closing figure - $992.50", "Closing figure - $ 992.50"):
        (apart,) = find_amounts(line)
        assert apart.value == Decimal("-992.50")
        assert apart.sign_detached
    for line in ("Closing figure -$992.50", "Closing figure -$ 992.50", "Closing figure -992.50"):
        (joined,) = find_amounts(line)
        assert joined.value == Decimal("-992.50")
        assert not joined.sign_detached
    found = find_amounts("$900.00 - $150.00 + $40.00 = $790.00")
    assert [a.sign_detached for a in found] == [False, True, True, False]
    assert not any(a.sign_detached for a in find_amounts("900.00 - 150.00 = 750.00"))


def test_a_symbol_or_sign_that_stands_alone_is_no_amount() -> None:
    assert find_amounts("Amounts in $ unless noted") == []
    assert find_amounts("Paid in $") == []
    assert find_amounts("Totals - $") == []
    assert find_amounts("Totals -$") == []
    assert [a.value for a in find_amounts("-$ twice 8.00")] == [Decimal("8.00")]
    # The word after a lone symbol is still read on its own.
    assert [a.value for a in find_amounts("$ 40.00 then $ twice 8.00")] == [
        Decimal("40.00"),
        Decimal("8.00"),
    ]


def test_an_equation_line_keeps_its_operators() -> None:
    found = find_amounts("900.00 - 150.00 + 40.00 = 790.00")
    assert [a.value for a in found] == [
        Decimal("900.00"),
        Decimal("150.00"),
        Decimal("40.00"),
        Decimal("790.00"),
    ]
    assert not any(a.negative_marked or a.positive_marked for a in found)


def test_find_amounts_reads_db() -> None:
    found = find_amounts("310.00 DB 55.00 CR")
    assert [a.value for a in found] == [Decimal("310.00"), Decimal("55.00")]
    assert [a.credit for a in found] == [False, True]
    # A lone symbol, its figure and the marker: three tokens, one amount.
    joined = find_amounts("Charge $ 5.00 DB then 9.00")
    assert [(a.value, a.credit, a.symbol) for a in joined] == [
        (Decimal("5.00"), False, "$"),
        (Decimal("9.00"), None, None),
    ]


def test_find_amounts_stays_bounded() -> None:
    assert find_amounts("$ " * 50_000) == []
    assert find_amounts("- $ " * 50_000) == []
    assert find_amounts("-$ " * 50_000) == []
    assert read_amount("7" + " " * 100_000 + ".00") is None


def test_month_name_date_with_two_digit_year() -> None:
    assert read_date("03-Nov-25", "d_mon_y") == date(2025, 11, 3)
    assert read_date("03/Nov/25", "d_mon_y") == date(2025, 11, 3)
    assert read_date("03-November-25", "d_mon_y") == date(2025, 11, 3)
    # ``16 Dec 20`` may be a day, a month and a count.
    assert read_date("03 Nov 25", "d_mon_y") is None
    assert read_date("03-Nov 25", "d_mon_y") is None
    assert read_date("03 Nov-25", "d_mon_y") is None
    # Joined means one separator printed twice: a mix is not a date.
    assert read_date("03-Nov/25", "d_mon_y") is None
    assert read_date("03/Nov-25", "d_mon_y") is None
    assert read_date("03-Nov/2025", "d_mon_y") is None


def test_month_name_date_with_four_digit_year_reads_as_before() -> None:
    assert read_date("03 Nov 2025", "d_mon_y") == date(2025, 11, 3)
    assert read_date("03-Nov-2025", "d_mon_y") == date(2025, 11, 3)
    assert read_date("03/Nov/2025", "d_mon_y") == date(2025, 11, 3)
    assert read_date("03/November/2025", "d_mon_y") == date(2025, 11, 3)
    assert read_date("03 Nov. 2025", "d_mon_y") == date(2025, 11, 3)
    assert read_date("03-Nov-025", "d_mon_y") is None
    assert read_date("03-Xyz-25", "d_mon_y") is None
    assert read_date("31-Feb-25", "d_mon_y") is None

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Likely causes of a statement's difference, on hand-built rows.

:func:`app.domain.review.causes.rank_causes` is pure: it is given the
difference (``opening + total - closing``, so a row too many has the
difference's sign), the ledger rows and what else is known, and names what
may explain it, most likely first. Every date, amount and line here is
invented; no document is read.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from app.domain.review.causes import (
    CAUSES_MAX,
    FEE_LINES_MAX,
    PER_KIND_MAX,
    Cause,
    LedgerRow,
    PageAmount,
    fee_lines,
    rank_causes,
)
from app.ingestion.parsers.base import Cell, DocumentView, ViewLine

PERIOD = (date(2031, 3, 1), date(2031, 3, 31))
BOX = (40.0, 300.0, 520.0, 309.0)


def _row(amount: str, day: int = 10, *, month: int = 3, earlier: bool = False) -> LedgerRow:
    return LedgerRow(uuid.uuid4(), date(2031, month, day), Decimal(amount), earlier)


def _rank(
    difference: str | None,
    rows: list[LedgerRow],
    *,
    opening: str | None = None,
    previous_closing: str | None = None,
    fees: tuple[PageAmount, ...] = (),
    grace_days: int = 0,
) -> list[Cause]:
    return rank_causes(
        None if difference is None else Decimal(difference),
        rows,
        period=PERIOD,
        opening=None if opening is None else Decimal(opening),
        previous_closing=None if previous_closing is None else Decimal(previous_closing),
        fee_lines=fees,
        grace_days=grace_days,
    )


def _kinds(causes: list[Cause]) -> list[str]:
    return [c.kind for c in causes]


# --- one test per cause --------------------------------------------------------


def test_a_row_equal_to_the_difference_is_named() -> None:
    extra = _row("-41.37")
    rows = [_row("-12.00", 4), extra, _row("250.00", 15)]
    causes = _rank("-41.37", rows)
    assert causes[0] == Cause("extra_row", (extra.id,), Decimal("-41.37"))
    # A row of the same size and the other sign is not a row too many.
    assert _kinds(_rank("41.37", rows)) == ["missing_row"]


def test_half_the_difference_is_a_sign() -> None:
    flipped = _row("63.20")  # printed as money out, read as money in
    causes = _rank("126.40", [_row("-9.10", 3), flipped])
    assert causes == [Cause("sign_flipped", (flipped.id,), Decimal("63.20"))]
    # Half the size with the other sign: flipping it would double the gap.
    assert "sign_flipped" not in _kinds(_rank("-126.40", [flipped]))


def test_multiple_of_nine_cents_is_a_transposition() -> None:
    # 81.45 read as 18.45: the difference is 63.00, 6300 cents, 9 * 700.
    causes = _rank("63.00", [_row("-18.45"), _row("-7.77", 12)])
    # The amount that is missing is still named: a transposition is only a guess.
    assert causes == [Cause("transposed_digits"), Cause("missing_row", (), Decimal("-63.00"))]
    assert _kinds(_rank("-0.27", [_row("-18.45")])) == ["transposed_digits", "missing_row"]
    # Not a multiple of nine cents.
    assert _kinds(_rank("63.01", [_row("-18.45")])) == ["missing_row"]


def test_transposition_is_not_named_when_a_row_explains_it() -> None:
    extra = _row("-27.00")
    assert _kinds(_rank("-27.00", [extra, _row("-3.10", 2)])) == ["extra_row"]
    half = _row("13.50")
    assert _kinds(_rank("27.00", [half])) == ["sign_flipped"]
    fee = PageAmount(Decimal("27.00"), 2, BOX)
    assert _kinds(_rank("-27.00", [_row("-3.10")], fees=(fee,))) == ["fee_or_interest"]


def test_fee_line_of_the_same_size() -> None:
    other = PageAmount(Decimal("3.00"), 1, (40.0, 100.0, 520.0, 109.0))
    fee = PageAmount(Decimal("14.95"), 3, BOX)
    # The fee was printed and not read as a row: the rows' total is 14.95 too high.
    causes = _rank("14.95", [_row("-8.00")], fees=(other, fee))
    assert causes == [Cause("fee_or_interest", (), Decimal("-14.95"), 3, BOX)]
    # Printed with a minus, or the difference the other way round: the size decides.
    minus = PageAmount(Decimal("-14.95"), 3, BOX)
    assert _rank("-14.95", [_row("-8.00")], fees=(minus,)) == [
        Cause("fee_or_interest", (), Decimal("14.95"), 3, BOX)
    ]
    assert _kinds(_rank("14.96", [_row("-8.00")], fees=(fee,))) == ["missing_row"]


def test_opening_against_the_previous_closing() -> None:
    causes = _rank("-20.00", [_row("-5.25")], opening="480.00", previous_closing="500.00")
    assert causes[0] == Cause("opening_differs_from_previous", (), Decimal("500.00"))
    # Equal within rounding, or one of the two unknown: not named.
    assert "opening_differs_from_previous" not in _kinds(
        _rank("-20.00", [], opening="500.00", previous_closing="500.004")
    )
    assert "opening_differs_from_previous" not in _kinds(
        _rank("-20.00", [], opening=None, previous_closing="500.00")
    )
    assert "opening_differs_from_previous" not in _kinds(
        _rank("-20.00", [], opening="480.00", previous_closing=None)
    )


def test_same_day_same_amount_pair() -> None:
    first, second = _row("-19.99", 7), _row("-19.99", 7)
    another_day = _row("-19.99", 8)
    causes = _rank("-19.99", [first, second, another_day, _row("-2.00", 7)])
    pairs = [c for c in causes if c.kind == "duplicated_row"]
    assert pairs == [Cause("duplicated_row", (first.id, second.id), Decimal("-19.99"))]
    # Each of the three is also a row equal to the difference, named before the pair.
    assert _kinds(causes) == ["extra_row"] * 3 + ["duplicated_row"]
    # Two equal rows on one day that are not the difference are two purchases.
    assert "duplicated_row" not in _kinds(_rank("-5.00", [first, second]))


def test_rows_outside_the_period_with_grace() -> None:
    before = _row("-30.00", 27, month=2)
    edge = _row("-4.00", 1)
    after = _row("-11.00", 2, month=4)
    late = _row("-6.00", 9, month=4)
    rows = [before, edge, after, late]
    causes = _rank("-17.00", rows)
    assert causes[-1].kind == "missing_row"
    assert causes[0] == Cause("out_of_period", (before.id, after.id, late.id))
    # Three days of grace on both ends take in all but the last; ten take in all.
    assert _rank("0", rows, grace_days=3) == [Cause("out_of_period", (late.id,))]
    assert _rank("0", rows, grace_days=10) == []
    # Without a period no row is outside it.
    assert rank_causes(Decimal("0"), rows, period=None, opening=None, previous_closing=None) == []


def test_a_removed_row_of_the_missing_amount_is_named_first() -> None:
    # Two purchases of 4.50 on one day, one removed: 450 cents divide by nine.
    twin = _row("-4.50", 3)
    rows = [twin, _row("-20.00", 9)]
    causes = rank_causes(
        Decimal("4.50"),
        rows,
        period=PERIOD,
        opening=None,
        previous_closing=None,
        removed=[Decimal("-4.50")],
    )
    assert causes == [Cause("missing_row", (), Decimal("-4.50"))]
    # A removed row of another amount explains nothing.
    other = rank_causes(
        Decimal("4.50"),
        rows,
        period=PERIOD,
        opening=None,
        previous_closing=None,
        removed=[Decimal("-20.00")],
    )
    assert _kinds(other) == ["transposed_digits", "missing_row"]
    # It comes before a row that happens to be as large as the difference.
    extra = _row("-4.50", 5)
    both = rank_causes(
        Decimal("-4.50"),
        [extra],
        period=PERIOD,
        opening=None,
        previous_closing=None,
        removed=[Decimal("4.50")],
    )
    assert _kinds(both) == ["missing_row", "extra_row"]


def test_nothing_explains_it_names_the_missing_amount() -> None:
    causes = _rank("55.31", [_row("-8.00"), _row("120.00", 20)])
    assert causes == [Cause("missing_row", (), Decimal("-55.31"))]


def test_earlier_reading_comes_first() -> None:
    old_a, old_b = _row("-41.37", earlier=True), _row("-3.00", 12, earlier=True)
    causes = _rank(
        "-41.37", [_row("-1.00", 2), old_a, old_b], opening="10.00", previous_closing="11.00"
    )
    assert _kinds(causes) == ["earlier_reading", "opening_differs_from_previous", "extra_row"]
    assert causes[0] == Cause("earlier_reading", (old_a.id, old_b.id))


# --- no difference, order, cap -------------------------------------------------


def test_zero_difference_names_no_row_cause() -> None:
    old = _row("0.00", earlier=True)
    outside = _row("-9.00", 3, month=5)
    rows = [old, outside, _row("-9.00", 3), _row("-9.00", 3)]
    fee = PageAmount(Decimal("0.00"), 1, BOX)
    for difference in ("0", "0.00", "0.004", "-0.004", None):
        causes = _rank(difference, rows, opening="1.00", previous_closing="2.00", fees=(fee,))
        assert _kinds(causes) == [
            "earlier_reading",
            "opening_differs_from_previous",
            "out_of_period",
        ], difference
    assert _rank("0", [_row("-9.00"), _row("0.00", 4)]) == []


def test_order_and_cap() -> None:
    old = _row("-1.00", 2, earlier=True)
    extras = [_row("-36.00", 5), _row("-36.00", 5), _row("-36.00", 6), _row("-36.00", 8)]
    halves = [_row("-18.00", 9), _row("-18.00", 11), _row("-18.00", 13), _row("-18.00", 14)]
    outside = _row("-2.50", 3, month=6)
    fee = PageAmount(Decimal("36.00"), 2, BOX)
    causes = _rank(
        "-36.00",
        [old, *extras, *halves, outside],
        opening="90.00",
        previous_closing="95.00",
        fees=(fee,),
    )
    # Thirteen candidates: 1 + 1 + 4 + 4 + 1 pair + 1 fee + 1 outside the period.
    assert len(causes) == CAUSES_MAX == 8
    assert PER_KIND_MAX == 3
    assert _kinds(causes) == [
        "earlier_reading",
        "opening_differs_from_previous",
        "extra_row",
        "extra_row",
        "extra_row",
        "sign_flipped",
        "sign_flipped",
        "sign_flipped",
    ]
    assert [c.rows for c in causes[2:5]] == [(r.id,) for r in extras[:3]]
    assert [c.rows for c in causes[5:8]] == [(r.id,) for r in halves[:3]]
    # With fewer rows in front, the later kinds follow in their order.
    causes = _rank(
        "-36.00",
        [old, extras[0], extras[1], halves[0], outside],
        opening="90.00",
        previous_closing="95.00",
        fees=(fee,),
    )
    assert _kinds(causes) == [
        "earlier_reading",
        "opening_differs_from_previous",
        "extra_row",
        "extra_row",
        "sign_flipped",
        "duplicated_row",
        "fee_or_interest",
        "out_of_period",
    ]
    assert len({(c.kind, c.rows, c.page) for c in causes}) == len(causes)


# --- fee_lines -----------------------------------------------------------------


def _line(text: str, index: int, page: int = 1) -> ViewLine:
    y = 100.0 + 14 * index
    cell = Cell(text, 40.0, y, 40.0 + 5.0 * len(text), y + 9.0)
    return ViewLine(page=page, index=index, cells=(cell,), text=text, height=9.0)


def _view(lines: list[ViewLine]) -> DocumentView:
    return DocumentView(lines=tuple(lines), source_format="pdf", truncated=False, page_count=3)


def test_fee_lines_reads_lines_that_name_a_fee_and_one_amount() -> None:
    lines = [
        _line("Monthly Service Charge 12.00", 0),
        _line("INTEREST PAID THIS PERIOD $0.42", 1),
        _line("Late fee -25.00", 2, page=2),
        _line("Finance Charge: 3.18", 3, page=3),
    ]
    found = fee_lines(_view(lines))
    assert [(f.amount, f.page) for f in found] == [
        (Decimal("12.00"), 1),
        (Decimal("0.42"), 1),
        (Decimal("-25.00"), 2),
        (Decimal("3.18"), 3),
    ]
    assert found[0].bbox == lines[0].bbox
    assert [f.page for f in fee_lines(_view(lines), pages={2, 3})] == [2, 3]
    assert fee_lines(_view(lines), pages=()) == []


def test_fee_lines_leaves_out_other_lines() -> None:
    lines = [
        _line("Corner Coffee House 4.50", 0),  # "fee" inside a word
        _line("Disinterested Books 18.00", 1),  # "interest" inside a word
        _line("Fees 10.00 Interest 2.00", 2),  # two amounts: a summary grid
        _line("Interest rate on purchases", 3),  # no amount
        _line("Service desk 7.25", 4),  # "service" without "charge"
        _line("03/04 Grocer 61.20", 5),  # an ordinary row
    ]
    assert fee_lines(_view(lines)) == []


def test_fee_lines_is_bounded() -> None:
    lines = [_line(f"Service charge {n}.00", n) for n in range(1, FEE_LINES_MAX + 20)]
    assert len(fee_lines(_view(lines))) == FEE_LINES_MAX == 50

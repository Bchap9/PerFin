# SPDX-License-Identifier: AGPL-3.0-or-later
"""Order and section-total checks for statement reconciliation (pure functions).

A statement printed newest first reconciles once its rows are put in the
order they happened; a section that prints its own total ("Total deposits")
is checked against the rows read under it. Every figure here is invented.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.ingestion.reconcile import (
    Row,
    newest_first,
    running_mismatches,
    section_mismatches,
    settle_signs,
)


def _d(text: str) -> Decimal:
    return Decimal(text)


def test_descending_dates_are_newest_first() -> None:
    dates = [date(2031, 3, 28), date(2031, 3, 28), date(2031, 3, 14), date(2031, 3, 2)]
    assert newest_first(dates) is True


def test_ascending_equal_and_two_row_lists_are_not() -> None:
    ascending = [date(2031, 3, 2), date(2031, 3, 14), date(2031, 3, 28)]
    equal = [date(2031, 3, 14)] * 4
    two_rows = [date(2031, 3, 28), date(2031, 3, 2)]
    down_then_up = [date(2031, 3, 28), date(2031, 3, 14), date(2031, 3, 2), date(2031, 3, 9)]
    assert newest_first(ascending) is False
    assert newest_first(equal) is False
    assert newest_first(two_rows) is False
    assert newest_first(down_then_up) is False
    assert newest_first([]) is False


def test_none_dates_are_ignored() -> None:
    descending = [None, date(2031, 3, 28), None, date(2031, 3, 14), date(2031, 3, 2), None]
    assert newest_first(descending) is True
    # Only two dates are present: too few to tell an order from an accident.
    assert newest_first([date(2031, 3, 28), None, None, date(2031, 3, 2)]) is False
    assert newest_first([None, None, None]) is False
    # A None between two dates hides nothing: the dates around it still go up.
    assert newest_first([date(2031, 3, 28), None, date(2031, 3, 2), date(2031, 3, 9)]) is False


def test_reversed_rows_reconcile() -> None:
    opening = _d("1500.00")
    amounts = [
        _d("-12.40") if n % 3 else _d("230.15") + n  # money out mostly, some money in
        for n in range(20)
    ]
    oldest_first: list[Row] = []
    balance = opening
    for amount in amounts:
        balance += amount
        oldest_first.append(Row(amount=amount, balance=balance))
    days = [date(2031, 3, 1) + timedelta(days=n) for n in range(20)]

    printed = oldest_first[::-1]  # the statement prints the newest row first
    printed_days = days[::-1]

    assert newest_first(printed_days) is True
    assert len(running_mismatches(opening, printed)) >= 18
    assert running_mismatches(opening, printed[::-1]) == []


def test_section_total_that_agrees() -> None:
    rows = [Row(_d("100.00")), Row(_d("200.00")), Row(_d("-40.00")), Row(_d("-59.00"))]
    sections = [1, 1, 2, 2]
    totals = {1: _d("300.00"), 2: _d("-99.00")}
    assert section_mismatches(rows, sections, totals) == []
    # Less than a cent apart is rounding, never a missing row.
    assert section_mismatches(rows, sections, {1: _d("300.004"), 2: _d("-99.00")}) == []


def test_section_total_that_disagrees_names_its_section() -> None:
    rows = [
        Row(_d("100.00")),
        Row(_d("200.00")),
        Row(_d("-40.00")),
        Row(_d("-59.00")),
        Row(_d("-21.00")),
    ]
    sections = [1, 1, 2, 2, 2]
    totals = {1: _d("300.00"), 2: _d("99.00")}
    assert section_mismatches(rows, sections, totals) == [2]
    # Both wrong: the ids come back sorted, whatever order the totals were read in.
    assert section_mismatches(rows, sections, {2: _d("99.00"), 1: _d("310.00")}) == [1, 2]


def test_sign_of_the_printed_total_does_not_matter() -> None:
    rows = [Row(_d("-40.00")), Row(_d("-59.00")), Row(_d("75.00"))]
    sections = [4, 4, 7]
    # Withdrawals printed as a positive total; a deposit total printed negative.
    assert section_mismatches(rows, sections, {4: _d("99.00"), 7: _d("-75.00")}) == []
    assert section_mismatches(rows, sections, {4: _d("-99.00"), 7: _d("75.00")}) == []


def test_section_total_is_the_net_of_its_rows_not_their_magnitudes() -> None:
    # A reversal inside a section of withdrawals takes away from its total.
    rows = [Row(_d("-40.00")), Row(_d("-59.00")), Row(_d("10.00")), Row(_d("75.00"))]
    sections = [4, 4, 4, 7]
    assert section_mismatches(rows, sections, {4: _d("89.00"), 7: _d("75.00")}) == []
    assert section_mismatches(rows, sections, {4: _d("-89.00")}) == []
    # 109.00 is what the three amounts come to with their signs dropped.
    assert section_mismatches(rows, sections, {4: _d("109.00"), 7: _d("75.00")}) == [4]


def test_sections_must_name_one_section_per_row() -> None:
    rows = [Row(_d("100.00")), Row(_d("-40.00"))]
    # A list built from other rows is a caller's mistake, never a shorter check.
    with pytest.raises(ValueError, match="one section per row"):
        section_mismatches(rows, [1], {1: _d("100.00")})
    with pytest.raises(ValueError, match="one section per row"):
        section_mismatches(rows, [1, 2, 2], {})


def test_section_without_a_total_is_not_checked() -> None:
    rows = [Row(_d("100.00")), Row(_d("-40.00")), Row(_d("-59.00"))]
    sections = [1, 2, 2]
    assert section_mismatches(rows, sections, {2: _d("99.00")}) == []
    assert section_mismatches(rows, sections, {}) == []
    # A total printed for a section no row was read under compares against zero.
    assert section_mismatches(rows, sections, {2: _d("99.00"), 3: _d("18.50")}) == [3]
    assert section_mismatches(rows, sections, {3: _d("0.00")}) == []


# -- a balance whose minus may be a separator -----------------------------------


def _settle(
    opening: str | None,
    closing: str | None,
    rows: list[Row],
    *,
    opening_open: bool = False,
    closing_open: bool = False,
) -> tuple[Decimal | None, Decimal | None]:
    return settle_signs(
        _d(opening) if opening is not None else None,
        _d(closing) if closing is not None else None,
        rows,
        opening_open=opening_open,
        closing_open=closing_open,
    )


def test_balances_with_a_sure_sign_are_left_alone() -> None:
    rows = [Row(_d("-4.50"))]
    # Not even a balance that disagrees with the rows is touched.
    assert _settle("410.00", "-300.00", rows) == (_d("410.00"), _d("-300.00"))
    assert _settle(None, None, rows, opening_open=True, closing_open=True) == (None, None)


def test_an_open_sign_takes_the_reading_the_rows_agree_with() -> None:
    rows = [Row(_d("-4.50"))]
    # ``Ending Balance - $405.50``: the dash was a separator.
    assert _settle("410.00", "-405.50", rows, closing_open=True) == (_d("410.00"), _d("405.50"))
    # An overdraft: the dash was the sign.
    assert _settle("-15.50", "-20.00", rows, closing_open=True) == (_d("-15.50"), _d("-20.00"))
    assert _settle("-410.00", "405.50", rows, opening_open=True) == (_d("410.00"), _d("405.50"))


def test_a_running_balance_settles_an_open_sign() -> None:
    rows = [Row(_d("-4.50"), _d("405.50")), Row(_d("-5.50"), _d("400.00"))]
    assert _settle(None, "-400.00", rows, closing_open=True) == (None, _d("400.00"))
    assert _settle("-410.00", None, rows, opening_open=True) == (_d("410.00"), None)
    overdrawn = [Row(_d("-4.50"), _d("-14.50")), Row(_d("-5.50"), _d("-20.00"))]
    assert _settle(None, "-20.00", overdrawn, closing_open=True) == (None, _d("-20.00"))


def test_both_signs_open_are_settled_together() -> None:
    rows = [Row(_d("-4.50"))]
    both = _settle("-410.00", "-405.50", rows, opening_open=True, closing_open=True)
    assert both == (_d("410.00"), _d("405.50"))
    overdrawn = _settle("-15.50", "-20.00", rows, opening_open=True, closing_open=True)
    assert overdrawn == (_d("-15.50"), _d("-20.00"))


def test_an_open_sign_nothing_settles_is_no_balance() -> None:
    rows = [Row(_d("-4.50"))]
    # Nothing to check it against: neither reading is given.
    assert _settle(None, "-405.50", rows, closing_open=True) == (None, None)
    assert _settle("-410.00", None, rows, opening_open=True) == (None, None)
    # Neither reading agrees with the rows: the sure balance stays.
    assert _settle("410.00", "-300.00", rows, closing_open=True) == (_d("410.00"), None)
    # No rows between two equal figures of opposite sign: both readings of
    # the pair agree, so neither is given.
    assert _settle("-50.00", "-50.00", [], opening_open=True, closing_open=True) == (None, None)
    # The running balance contradicts the one reading the totals allow.
    odd = [Row(_d("-4.50"), _d("77.00"))]
    assert _settle("410.00", "-405.50", odd, closing_open=True) == (_d("410.00"), None)

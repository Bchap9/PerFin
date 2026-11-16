# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bank statements, round two: layouts the first round read wrongly.

Each builder draws one PDF as :mod:`tests.fixtures.synth_bank_layouts` does;
every institution, payee, number and figure is invented. Each prints a
masked account number, a labelled period, an opening and a closing balance
and rows that reconcile:

* :func:`marker_column_checking`: ``Date | Description | Reference | Amount |
  DC | Balance``: every amount is printed without a sign and the column
  ``DC`` says which way it went (``DB`` on five rows, ``CR`` on three); a
  running balance on every row.
* :func:`summary_grid_with_counts`: a block ``Account Summary`` over the
  header ``Date | Description | Amount`` holding a dated ``Beginning
  Balance`` line, two undated count lines (``6 Credits This Period``, ``9
  Debits This Period``) and a dated ``Ending Balance`` line: no line of it
  is a transaction. Two titled tables follow with the six and the nine rows.
* :func:`brought_forward_dated_once`: ``Date | Description | Paid out | Paid
  in | Balance``; a dated ``Balance brought forward`` line, then nine rows
  over four days with the date printed once per day (the first day's date
  is the one on the brought-forward line), and a dated ``Balance carried
  forward`` line under the last of them.
* :func:`amount_before_description_fused`: ``Date | Amount Description``, the
  two headings a word space apart, and under them rows whose amount and
  description are one cell (``$47.30 Card Purchase Mossgiel Dairy``): the
  cell sits in both columns. Money out is printed with a minus.
* :func:`two_dates_left_aligned`: a card. ``Trans. Date Post Date |
  Description | Amount | Category``: the two dates of a row stand 22 points
  apart under the one heading cell, so the second reaches under ``Trans.
  Date`` more than under ``Post Date``; the category is printed left of its
  heading and, on the rows with a short one, overlaps ``Amount`` more than
  ``Category``. Charges are printed without a sign, the one payment with a
  minus.
* :func:`amount_under_row_card`: a card. ``Date | Description | Amount``; on
  two of its six rows the dated line prints the description only, one or
  two lines under it give the purchase in its own currency and the rate
  (``63.00 CAD``, ``1.28 Exchange Rate``), and the amount stands alone on
  the line after those, under ``Amount``.

:data:`EXPECTED_ROWS` gives, per builder, the rows a correct reading returns
(``(posted, amount, payee)``, money out negative).
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from typing import Final

from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from tests.fixtures.synth_bank_layouts import (
    _FOOTER,
    ExpectedRow,
    _d,
    _head,
    _money,
    _running,
    _top,
    _total,
    _unsigned_table,
)
from tests.fixtures.synth_unseen_layouts import _line, _rows

# -- a credit or debit marker in a column of its own ----------------------------

_MARKER_OPENING: Final = _d("3,264.80")
#: ``(row, reference)``: the reference is printed between description and amount.
_MARKER_ROWS: Final[tuple[tuple[ExpectedRow, str], ...]] = (
    ((date(2026, 8, 3), _d("-412.60"), "Card Purchase Thistledown Garage"), "7719"),
    ((date(2026, 8, 5), _d("1,930.00"), "Payroll Deposit Brackenhurst Mills"), "4402"),
    ((date(2026, 8, 7), _d("-58.25"), "Card Purchase Oatfield Bakery"), "7720"),
    ((date(2026, 8, 12), _d("-1,180.00"), "Rent Ploverfield Mews"), "3051"),
    ((date(2026, 8, 14), _d("75.00"), "Mobile Deposit"), "4403"),
    ((date(2026, 8, 19), _d("-96.40"), "Wrenlow Water Bill Pay"), "3052"),
    ((date(2026, 8, 25), _d("-240.00"), "ATM Withdrawal Saltmarsh Row"), "7721"),
    ((date(2026, 8, 28), _d("0.52"), "Interest Paid"), "4404"),
)


def marker_column_checking() -> bytes:
    """Eight unsigned amounts; ``DB`` or ``CR`` in the column beside them."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    rows = [row for row, _ in _MARKER_ROWS]
    balances = _running(_MARKER_OPENING, rows)
    _head(
        c,
        "Kestrelmoor Savings Bank",
        "6183",
        "08/01/2026 - 08/31/2026",
        _MARKER_OPENING,
        balances[-1],
    )
    _line(
        c,
        650,
        [
            (40, "Date"),
            (105, "Description"),
            (310, "Reference"),
            (-430, "Amount"),
            (445, "DC"),
            (-550, "Balance"),
        ],
        size=8,
    )
    lines: list[list[tuple[float, str]]] = []
    for ((posted, amount, payee), reference), balance in zip(_MARKER_ROWS, balances, strict=True):
        lines.append(
            [
                (40, posted.strftime("%m/%d/%Y")),
                (105, payee),
                (310, reference),
                (-430, _money(amount)),
                (445, "DB" if amount < 0 else "CR"),
                (-550, _money(balance)),
            ]
        )
    _rows(c, 636, lines)
    c.save()
    return buf.getvalue()


# -- a summary block that looks like a table: two balance lines, counts between --

_GRID_OPENING: Final = _d("2,104.35")
_GRID_CREDITS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 9, 2), _d("1,200.00"), "Payroll Deposit Marrowby Looms"),
    (date(2026, 9, 9), _d("310.00"), "Transfer From Savings"),
    (date(2026, 9, 14), _d("145.50"), "Mobile Deposit"),
    (date(2026, 9, 18), _d("96.25"), "Refund Hobbleton Outfitters"),
    (date(2026, 9, 23), _d("88.00"), "Cash Deposit Branch 12"),
    (date(2026, 9, 29), _d("30.25"), "Rebate Tansyfield Co-op"),
)
_GRID_DEBITS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 9, 1), _d("-640.00"), "Rent Curlewmere Court"),
    (date(2026, 9, 4), _d("-212.40"), "Card Purchase Stonechat Grocers"),
    (date(2026, 9, 8), _d("-118.75"), "Bill Pay Eddlestone Power"),
    (date(2026, 9, 11), _d("-96.10"), "Card Purchase Furzeholt Hardware"),
    (date(2026, 9, 15), _d("-84.00"), "ATM Cash Wicket Lane"),
    (date(2026, 9, 19), _d("-73.25"), "Card Purchase Pipitwood Cafe"),
    (date(2026, 9, 22), _d("-66.00"), "Bill Pay Skerrowick Water"),
    (date(2026, 9, 25), _d("-55.00"), "Card Purchase Gorsedown Books"),
    (date(2026, 9, 28), _d("-75.00"), "Transfer To Savings"),
)
_GRID_ROWS: Final = (*_GRID_CREDITS, *_GRID_DEBITS)
#: The words the two count lines of the summary block end with.
GRID_COUNT_WORDS: Final = "This Period"


def summary_grid_with_counts() -> bytes:
    """A summary block under a table header, then the two tables it counts."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_GRID_OPENING, _GRID_ROWS)[-1]
    _top(c, "Dunnockvale Community Bank", "2946", "09/01/2026 - 09/30/2026")
    _line(c, 706, [(40, "Account Summary")], size=10, bold=True)
    _line(c, 690, [(40, "Date"), (110, "Description"), (-560, "Amount")], size=8)
    y = _rows(
        c,
        677,
        [
            [(40, "09/01/2026"), (110, "Beginning Balance"), (-560, _money(_GRID_OPENING))],
            [
                (110, f"{len(_GRID_CREDITS)} Credits {GRID_COUNT_WORDS}"),
                (-560, _money(_total(_GRID_CREDITS))),
            ],
            [
                (110, f"{len(_GRID_DEBITS)} Debits {GRID_COUNT_WORDS}"),
                (-560, _money(_total(_GRID_DEBITS))),
            ],
            [(40, "09/30/2026"), (110, "Ending Balance"), (-560, _money(closing))],
        ],
    )
    y = _unsigned_table(c, y - 20, "DEPOSITS AND ADDITIONS", _GRID_CREDITS)
    _unsigned_table(c, y - 13, "ELECTRONIC WITHDRAWALS", _GRID_DEBITS)
    c.save()
    return buf.getvalue()


# -- a dated brought-forward line over rows that print their date once per day --

_FORWARD_OPENING: Final = _d("1,618.92")
_FORWARD_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 10, 1), _d("-52.30"), "Card Purchase Linnetshaw Bakery"),
    (date(2026, 10, 1), _d("-420.00"), "Standing Order Ravelstoke Lettings"),
    (date(2026, 10, 9), _d("1,705.00"), "Salary Coldharrow Potteries"),
    (date(2026, 10, 9), _d("-36.48"), "Direct Debit Whinfell Mobile"),
    (date(2026, 10, 9), _d("-20.00"), "Cash Machine Ropewalk"),
    (date(2026, 10, 16), _d("-114.07"), "Card Purchase Sallowby Grocers"),
    (date(2026, 10, 16), _d("60.00"), "Transfer From Saver"),
    (date(2026, 10, 27), _d("-81.15"), "Direct Debit Thornaby Energy"),
    (date(2026, 10, 27), _d("-150.00"), "Transfer To Saver"),
)
#: The day printed on the ``Balance brought forward`` line.
_FORWARD_DAY: Final = date(2026, 10, 1)


def brought_forward_dated_once() -> bytes:
    """Nine rows over four days; the first day's date is on the line above them."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    balances = _running(_FORWARD_OPENING, _FORWARD_ROWS)
    _head(
        c,
        "Merlewood Building Society",
        "8057",
        "10/01/2026 - 10/31/2026",
        _FORWARD_OPENING,
        balances[-1],
    )
    _line(
        c,
        650,
        [
            (40, "Date"),
            (110, "Description"),
            (-400, "Paid out"),
            (-470, "Paid in"),
            (-550, "Balance"),
        ],
        size=8,
    )
    lines: list[list[tuple[float, str]]] = [
        [
            (40, _FORWARD_DAY.strftime("%m/%d/%Y")),
            (110, "Balance brought forward"),
            (-550, _money(_FORWARD_OPENING)),
        ]
    ]
    printed: date | None = _FORWARD_DAY
    for (posted, amount, payee), balance in zip(_FORWARD_ROWS, balances, strict=True):
        cells: list[tuple[float, str]] = []
        if posted != printed:
            cells.append((40, posted.strftime("%m/%d/%Y")))
            printed = posted
        cells += [
            (110, payee),
            (-400 if amount < 0 else -470, _money(amount)),
            (-550, _money(balance)),
        ]
        lines.append(cells)
    lines.append(
        [(40, "10/31/2026"), (110, "Balance carried forward"), (-550, _money(balances[-1]))]
    )
    _rows(c, 636, lines)
    c.save()
    return buf.getvalue()


# -- the amount and the description of a row printed as one cell ----------------

_AMOUNT_FIRST_OPENING: Final = _d("1,912.40")
_AMOUNT_FIRST_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 6, 2), _d("1,640.00"), "Payroll Deposit Lapwing Foundry"),
    (date(2026, 6, 4), _d("-925.00"), "Rent Tolliver Yard"),
    (date(2026, 6, 8), _d("-47.30"), "Card Purchase Mossgiel Dairy"),
    (date(2026, 6, 12), _d("210.00"), "Mobile Deposit"),
    (date(2026, 6, 17), _d("-132.85"), "Bill Pay Cinderford Gas"),
    (date(2026, 6, 23), _d("-80.00"), "ATM Withdrawal Quarry Road"),
    (date(2026, 6, 29), _d("0.38"), "Interest Paid"),
)


def amount_before_description_fused() -> bytes:
    """Seven rows ``MM/DD | $NNN.NN Words``: amount and description one cell."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_AMOUNT_FIRST_OPENING, _AMOUNT_FIRST_ROWS)[-1]
    _head(
        c,
        "Pennywhistle Mutual Bank",
        "5527",
        "06/01/2026 - 06/30/2026",
        _AMOUNT_FIRST_OPENING,
        closing,
    )
    _line(c, 650, [(40, "Date"), (110, "Amount Description")], size=8)
    _rows(
        c,
        636,
        [
            [
                (40, posted.strftime("%m/%d")),
                (110, f"{'-' if amount < 0 else ''}${_money(amount)} {payee}"),
            ]
            for posted, amount, payee in _AMOUNT_FIRST_ROWS
        ],
    )
    c.save()
    return buf.getvalue()


# -- a card: two dates under one heading cell, a category left of its heading ---

_TWO_DATES_PREVIOUS: Final = _d("95.00")
#: ``(row, category)``; the posting date printed is the day after the row's.
_TWO_DATES_ROWS: Final[tuple[tuple[ExpectedRow, str], ...]] = (
    ((date(2026, 7, 3), _d("-15.49"), "FERNHILL STREAMING"), "Services"),
    ((date(2026, 7, 6), _d("-62.10"), "OSPREY LANE GROCER"), "Groceries"),
    ((date(2026, 7, 9), _d("95.00"), "PAYMENT RECEIVED - THANK YOU"), "Payments"),
    ((date(2026, 7, 13), _d("-38.75"), "COBBLEGATE DINER"), "Restaurants"),
    ((date(2026, 7, 18), _d("-71.20"), "WINDLESHAM RAIL"), "Transport"),
    ((date(2026, 7, 22), _d("-27.90"), "MARLPIT PHARMACY"), "Healthcare"),
    ((date(2026, 7, 27), _d("-84.16"), "DUNLIN HARDWARE"), "Merchandise"),
)
#: The words printed under ``Category``: none of them is part of a payee.
CATEGORY_WORDS: Final = tuple(category for _, category in _TWO_DATES_ROWS)
#: Points between the end of a row's first date and the start of its second.
_DATE_GAP: Final = 22.0
_ROW_SIZE: Final = 8
_HEADING_SIZE: Final = 12
_AMOUNT_X: Final = 390.0
#: The categories start here, under the last letters of ``Amount``; their
#: heading starts 30 points on.
_CATEGORY_X: Final = 428.0
_CATEGORY_HEADING_X: Final = 458.0


def two_dates_left_aligned() -> bytes:
    """Seven card rows, each with its transaction and its posting date."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    rows = [row for row, _ in _TWO_DATES_ROWS]
    charges = -sum((a for _, a, _ in rows if a < 0), Decimal(0))
    credits = sum((a for _, a, _ in rows if a > 0), Decimal(0))
    owed = _TWO_DATES_PREVIOUS - credits + charges
    _line(c, 750, [(40, "Tarnbeck Rewards Mastercard")], size=14, bold=True)
    _line(c, 734, [(40, "Card ending in 3306")])
    _line(c, 750, [(380, "Billing Period: 07/01/2026 - 07/31/2026")])
    _line(c, 60, [(40, _FOOTER)], size=7)
    _rows(
        c,
        700,
        [
            [(40, "Previous Balance"), (-300, f"${_money(_TWO_DATES_PREVIOUS)}")],
            [(40, "Payments and Credits"), (-300, f"-${_money(credits)}")],
            [(40, "New Charges"), (-300, f"+${_money(charges)}")],
            [(40, "New Balance"), (-300, f"${_money(owed)}")],
            [(40, "Minimum Payment Due"), (-300, "$25.00")],
        ],
    )
    _line(
        c,
        618,
        [
            (40, "Trans. Date Post Date"),
            (175, "Description"),
            (_AMOUNT_X, "Amount"),
            (_CATEGORY_HEADING_X, "Category"),
        ],
        size=_HEADING_SIZE,
        bold=True,
    )
    y = 602.0
    for (posted, amount, payee), category in _TWO_DATES_ROWS:
        first = posted.strftime("%m/%d")
        second = posted.replace(day=posted.day + 1).strftime("%m/%d")
        _line(
            c,
            y,
            [
                (40, first),
                (40 + stringWidth(first, "Helvetica", _ROW_SIZE) + _DATE_GAP, second),
                (175, payee),
                (_AMOUNT_X, f"${_money(amount)}" if amount < 0 else f"-${_money(amount)}"),
                (_CATEGORY_X, category),
            ],
            size=_ROW_SIZE,
        )
        y -= 13
    c.save()
    return buf.getvalue()


# -- a card: the amount alone on a line under its row ---------------------------

_UNDER_PREVIOUS: Final = _d("120.00")
#: ``(row, lines printed between the dated line and the amount)``: with
#: lines, the dated line prints no amount and the amount follows them alone.
_UNDER_ROWS: Final[tuple[tuple[ExpectedRow, tuple[str, ...]], ...]] = (
    ((date(2026, 11, 3), _d("-54.20"), "TEALBROOK MARKET"), ()),
    (
        (date(2026, 11, 6), _d("-49.22"), "CAFE HERONSGATE MONTREAL"),
        ("63.00 CAD", "1.28 Exchange Rate"),
    ),
    ((date(2026, 11, 10), _d("120.00"), "PAYMENT RECEIVED - THANK YOU"), ()),
    (
        (date(2026, 11, 14), _d("-35.00"), "LIBRAIRIE ALDERWICK QUEBEC"),
        ("44.80 CAD 1.28 Exchange Rate",),
    ),
    ((date(2026, 11, 19), _d("-41.75"), "SEDGEMOOR FUEL"), ()),
    ((date(2026, 11, 25), _d("-18.30"), "WHINBURN PHARMACY"), ()),
)
#: Per row printed that way: its payee, its amount and the lines under it.
AMOUNT_UNDER_ROW: Final = tuple(
    (payee, amount, under) for (_, amount, payee), under in _UNDER_ROWS if under
)


def amount_under_row_card() -> bytes:
    """Six card rows; two print their amount alone, under the rate lines."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    rows = [row for row, _ in _UNDER_ROWS]
    charges = -sum((a for _, a, _ in rows if a < 0), Decimal(0))
    credits = sum((a for _, a, _ in rows if a > 0), Decimal(0))
    owed = _UNDER_PREVIOUS - credits + charges
    _line(c, 750, [(40, "Yarrowdale Travel Visa")], size=14, bold=True)
    _line(c, 734, [(40, "Card ending in 9164")])
    _line(c, 750, [(380, "Billing Period: 11/01/2026 - 11/30/2026")])
    _line(c, 60, [(40, _FOOTER)], size=7)
    _rows(
        c,
        700,
        [
            [(40, "Previous Balance"), (-300, f"${_money(_UNDER_PREVIOUS)}")],
            [(40, "Payments and Credits"), (-300, f"-${_money(credits)}")],
            [(40, "New Charges"), (-300, f"+${_money(charges)}")],
            [(40, "New Balance"), (-300, f"${_money(owed)}")],
            [(40, "Minimum Payment Due"), (-300, "$25.00")],
        ],
    )
    _line(c, 618, [(40, "Date"), (110, "Description"), (-540, "Amount")], size=8)
    lines: list[list[tuple[float, str]]] = []
    for (posted, amount, payee), under in _UNDER_ROWS:
        figure = f"${_money(amount)}" if amount < 0 else f"-${_money(amount)}"
        dated: list[tuple[float, str]] = [(40, posted.strftime("%m/%d")), (110, payee)]
        if not under:
            lines.append([*dated, (-540, figure)])
            continue
        lines.append(dated)
        lines.extend([(110, text)] for text in under)
        lines.append([(-540, figure)])
    _rows(c, 604, lines)
    c.save()
    return buf.getvalue()


EXPECTED_ROWS: Final[dict[str, list[ExpectedRow]]] = {
    "marker_column_checking": [row for row, _ in _MARKER_ROWS],
    "summary_grid_with_counts": list(_GRID_ROWS),
    "brought_forward_dated_once": list(_FORWARD_ROWS),
    "amount_before_description_fused": list(_AMOUNT_FIRST_ROWS),
    "two_dates_left_aligned": [row for row, _ in _TWO_DATES_ROWS],
    "amount_under_row_card": [row for row, _ in _UNDER_ROWS],
}

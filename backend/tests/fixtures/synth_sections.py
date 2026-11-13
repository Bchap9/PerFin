# SPDX-License-Identifier: AGPL-3.0-or-later
"""Statements whose sections are printed with a title and no header row.

Each builder draws one PDF as :mod:`tests.fixtures.synth_bank_layouts` does;
every institution, payee, number and figure is invented. Each prints a
masked account number, a labelled period, an opening and a closing balance
and, unless it says otherwise, rows that reconcile:

* :func:`headerless_sections_checking`: two pages and no header anywhere. A
  summary, then ``DEPOSITS & CREDITS`` over four lines of a date, text and a
  figure and ``Total Deposits & Credits``; ``WITHDRAWALS`` over seven such
  lines, the last three on page 2 under ``WITHDRAWALS (CONTINUED)``, and
  ``Total Withdrawals``. Every figure is printed without a sign.
* :func:`headerless_total_off`: the same with one deposit line left off the
  page; the totals and the balances still count it.
* :func:`headerless_card`: a card with no header anywhere. ``Payments and
  Other Credits`` is the last line of page 1; its one line (two dates, the
  payment printed with a minus) and its total head page 2, then
  ``Purchases and Adjustments`` over five lines and its total.
* :func:`dated_notices_under_a_title`: ``Fees`` over three sentences that
  each start with a date and end with a figure (more than thirty words
  each), ``Deposits`` over five lines of a date and a balance with no
  words, then a headed table that reconciles. The blocks stand above the
  table: under it they would be lines of the table, read by its columns.

:data:`EXPECTED_ROWS` gives, per builder, the rows a correct reading returns
(``(posted, amount, payee)``, money out negative).
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import date
from typing import Final

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from tests.fixtures.synth_bank_layouts import (
    ExpectedRow,
    _d,
    _head,
    _money,
    _running,
    _signed_rows,
    _top,
    _total,
)
from tests.fixtures.synth_unseen_layouts import _line, _rows

# -- a checking statement with no header anywhere --------------------------------

_CHECKING_OPENING: Final = _d("2,640.15")
_CHECKING_DEPOSITS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 10, 2), _d("1,815.00"), "Payroll Hazelrigg Cooperage"),
    (date(2026, 10, 9), _d("120.00"), "Mobile Deposit"),
    (date(2026, 10, 16), _d("1,815.00"), "Payroll Hazelrigg Cooperage"),
    (date(2026, 10, 27), _d("42.75"), "Refund Sedgewick Lane Books"),
)
_CHECKING_WITHDRAWALS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 10, 1), _d("-1,340.00"), "Rent Pennywell Terrace"),
    (date(2026, 10, 5), _d("-64.18"), "Card Purchase Barleymow Grocer"),
    (date(2026, 10, 8), _d("-118.40"), "Bill Pay Tarnside Electric"),
    (date(2026, 10, 13), _d("-60.00"), "ATM Cash Ropewalk Square"),
    (date(2026, 10, 19), _d("-37.25"), "Card Purchase Fernhollow Cafe"),
    (date(2026, 10, 23), _d("-210.90"), "Bill Pay Ousebridge Insurance"),
    (date(2026, 10, 29), _d("-15.00"), "Monthly Service Charge"),
)
#: The withdrawals printed on page 1; the rest are under the title printed again.
_CHECKING_FIRST_PAGE: Final = 4
_CHECKING_BANK: Final = "Dunnockshaw Savings Bank"
_CHECKING_PERIOD: Final = "10/01/2026 - 10/31/2026"


def _led_lines(rows: Sequence[ExpectedRow]) -> list[list[tuple[float, str]]]:
    """``MM/DD``, the text and the figure with no sign, one line per row."""
    return [
        [(40, posted.strftime("%m/%d")), (110, payee), (-560, _money(amount))]
        for posted, amount, payee in rows
    ]


def _total_line(c: canvas.Canvas, y: float, label: str, rows: Sequence[ExpectedRow]) -> None:
    _line(c, y, [(40, label), (-560, _money(_total(rows)))], bold=True)


def _headerless_checking(*, omit: ExpectedRow | None = None) -> bytes:
    """``omit`` is a deposit left off the page; the totals and balances count it."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    everything = (*_CHECKING_DEPOSITS, *_CHECKING_WITHDRAWALS)
    closing = _running(_CHECKING_OPENING, everything)[-1]
    _top(c, _CHECKING_BANK, "2946", _CHECKING_PERIOD)
    _rows(
        c,
        700,
        [
            [(40, "Beginning Balance"), (-300, f"${_money(_CHECKING_OPENING)}")],
            [(40, "Deposits & Credits"), (-300, _money(_total(_CHECKING_DEPOSITS)))],
            [(40, "Withdrawals"), (-300, _money(_total(_CHECKING_WITHDRAWALS)))],
            [(40, "Ending Balance"), (-300, f"${_money(closing)}")],
        ],
    )
    _line(c, 630, [(40, "DEPOSITS & CREDITS")], size=10, bold=True)
    y = _rows(c, 614, _led_lines([row for row in _CHECKING_DEPOSITS if row != omit]))
    _total_line(c, y, "Total Deposits & Credits", _CHECKING_DEPOSITS)
    y -= 30
    _line(c, y, [(40, "WITHDRAWALS")], size=10, bold=True)
    _rows(c, y - 16, _led_lines(_CHECKING_WITHDRAWALS[:_CHECKING_FIRST_PAGE]))
    c.showPage()
    _top(c, _CHECKING_BANK, "2946", _CHECKING_PERIOD)
    _line(c, 700, [(40, "WITHDRAWALS (CONTINUED)")], size=10, bold=True)
    y = _rows(c, 684, _led_lines(_CHECKING_WITHDRAWALS[_CHECKING_FIRST_PAGE:]))
    _total_line(c, y, "Total Withdrawals", _CHECKING_WITHDRAWALS)
    c.save()
    return buf.getvalue()


def headerless_sections_checking() -> bytes:
    """Four deposits and seven withdrawals under their titles, no header row."""
    return _headerless_checking()


#: The deposit :func:`headerless_total_off` leaves off the page.
TOTAL_OFF_OMITTED: Final = _CHECKING_DEPOSITS[1]


def headerless_total_off() -> bytes:
    """One deposit is missing: the deposits are not their printed total."""
    return _headerless_checking(omit=TOTAL_OFF_OMITTED)


# -- a card with no header anywhere ----------------------------------------------

_CARD_PREVIOUS: Final = _d("812.40")
#: The payment: its two reference numbers are printed after the text.
_CARD_PAYMENT: Final[ExpectedRow] = (
    date(2026, 8, 14),
    _d("240.00"),
    "PAYMENT RECEIVED - THANK YOU 5521 0098",
)
_CARD_PURCHASES: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 8, 3), _d("-58.12"), "MARLPIT HARDWARE 4410 7702"),
    (date(2026, 8, 9), _d("-23.80"), "COWSLIP LANE BAKERY 4410 7719"),
    (date(2026, 8, 17), _d("-142.65"), "STANEGATE OUTFITTERS 4410 7733"),
    (date(2026, 8, 22), _d("-9.99"), "TALLOWBY STREAMING 4410 7748"),
    (date(2026, 8, 28), _d("-76.30"), "WHINFELL FUEL STOP 4410 7760"),
)
_CARD_ISSUER: Final = "Gorsebank Card Services"
_CARD_PERIOD: Final = "08/01/2026 - 08/31/2026"


def _card_line(row: ExpectedRow, *, minus: bool) -> list[tuple[float, str]]:
    """The transaction date, the posting date a day later, the text, the figure."""
    posted, amount, payee = row
    after = posted.replace(day=posted.day + 1)
    figure = f"-{_money(amount)}" if minus else _money(amount)
    return [
        (40, posted.strftime("%m/%d")),
        (80, after.strftime("%m/%d")),
        (125, payee),
        (-560, figure),
    ]


def headerless_card() -> bytes:
    """One payment (title on page 1, line on page 2) and five purchases."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    purchases = _total(_CARD_PURCHASES)
    new_balance = _CARD_PREVIOUS - _CARD_PAYMENT[1] + purchases
    _top(c, _CARD_ISSUER, "4417", _CARD_PERIOD)
    _rows(
        c,
        700,
        [
            [(40, "Credit Limit"), (-300, "$6,000.00")],
            [(40, "Previous Balance"), (-300, f"${_money(_CARD_PREVIOUS)}")],
            [(40, "Payments and Other Credits"), (-300, f"-${_money(_CARD_PAYMENT[1])}")],
            [(40, "Purchases and Adjustments"), (-300, f"${_money(purchases)}")],
            [(40, "New Balance"), (-300, f"${_money(new_balance)}")],
        ],
    )
    _line(c, 90, [(40, "Payments and Other Credits")], size=10, bold=True)
    c.showPage()
    _top(c, _CARD_ISSUER, "4417", _CARD_PERIOD)
    _rows(c, 700, [_card_line(_CARD_PAYMENT, minus=True)])
    _line(
        c,
        687,
        [
            (40, "TOTAL PAYMENTS AND OTHER CREDITS FOR THIS PERIOD"),
            (-560, f"-${_money(_CARD_PAYMENT[1])}"),
        ],
        bold=True,
    )
    _line(c, 657, [(40, "Purchases and Adjustments")], size=10, bold=True)
    y = _rows(c, 641, [_card_line(row, minus=False) for row in _CARD_PURCHASES])
    _line(
        c,
        y,
        [
            (40, "TOTAL PURCHASES AND ADJUSTMENTS FOR THIS PERIOD"),
            (-560, f"${_money(purchases)}"),
        ],
        bold=True,
    )
    c.save()
    return buf.getvalue()


# -- dated lines under a title that are no rows ----------------------------------

_NOTICE_OPENING: Final = _d("1,906.32")
_NOTICE_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 11, 3), _d("1,480.00"), "Payroll Deposit Lintmill Weavers"),
    (date(2026, 11, 6), _d("-915.00"), "Rent Coldwell Row"),
    (date(2026, 11, 12), _d("-48.66"), "POS Purchase Ravensgill Market"),
    (date(2026, 11, 20), _d("-72.10"), "Skerryvore Mobile Online Pmt"),
    (date(2026, 11, 27), _d("0.41"), "Interest Credit"),
)
#: Sentences that start with a date and end with a figure: notices, not rows.
NOTICE_SENTENCES: Final[tuple[str, ...]] = (
    "11/01 a monthly charge of the kind set out in the schedule we sent to you in the spring"
    " may be taken from an account that is kept below the floor we agreed with you of 12.00",
    "11/15 a charge for paper copies of a statement that was first sent to you more than two"
    " years ago is taken on the day we post it to you and is now for each copy 5.00",
    "11/30 a charge for a payment that we send abroad for you on a working day before noon and"
    " that is paid in the currency of the account it leaves is from now on 18.50",
)
#: ``(day, balance)``: balances by day, a date and a figure with no words.
_NOTICE_BALANCES: Final[tuple[tuple[str, str], ...]] = (
    ("11/03", "3,386.32"),
    ("11/06", "2,471.32"),
    ("11/12", "2,422.66"),
    ("11/20", "2,350.56"),
    ("11/27", "2,350.97"),
)


def _notice_blocks(c: canvas.Canvas, y: float) -> float:
    """``Fees`` over the sentences, ``Deposits`` over the balances by day; the next y."""
    _line(c, y, [(40, "Fees")], size=10, bold=True)
    y -= 16
    for sentence in NOTICE_SENTENCES:
        _line(c, y, [(40, sentence)], size=6)
        y -= 13
    y -= 13
    _line(c, y, [(40, "Deposits")], size=10, bold=True)
    return _rows(c, y - 16, [[(40, day), (-560, balance)] for day, balance in _NOTICE_BALANCES])


def dated_notices_under_a_title() -> bytes:
    """Two titled blocks of dated lines that are no rows, and a headed table."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_NOTICE_OPENING, _NOTICE_ROWS)[-1]
    _head(c, "Brackenthwaite Bank", "5068", "11/01/2026 - 11/30/2026", _NOTICE_OPENING, closing)
    y = _notice_blocks(c, 650)
    _signed_rows(c, y - 26, _NOTICE_ROWS)
    c.save()
    return buf.getvalue()


EXPECTED_ROWS: Final[dict[str, list[ExpectedRow]]] = {
    "headerless_sections_checking": [*_CHECKING_DEPOSITS, *_CHECKING_WITHDRAWALS],
    "headerless_total_off": list(_CHECKING_WITHDRAWALS),
    "headerless_card": [_CARD_PAYMENT, *_CARD_PURCHASES],
    "dated_notices_under_a_title": list(_NOTICE_ROWS),
}

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checking statements whose table header is printed in ways a plain
``Date | Description | Amount`` reader does not expect.

Each generator draws one page: a masked account number, a labelled period
with years, an opening and a closing balance, and six rows that reconcile.
Every institution, payee and figure is invented. The rows are printed as the
row reader wants them (a date cell of its own, every figure under its
heading); only the **header** is unusual:

* :func:`merged_heading_checking`: ``DATE | DESCRIPTION | DEPOSIT |
  WITHDRAWAL BALANCE``, the last two headings five points apart and so one
  cell.
* :func:`amount_before_description`: ``Date | Amount Description``, the
  amount printed before the description.
* :func:`two_dates_and_category`: ``Trans Date Post Date | Description |
  Amount | Category``; every row prints both dates and a one-word category
  right of the amount.
* :func:`daily_balance_after_rows`: a plain table followed by a block
  titled ``DAILY ENDING BALANCE`` whose header is ``Date | Amount | Date |
  Amount Date | Amount`` over lines of date and balance pairs. Those lines
  are balances: none of them is a transaction.
* :func:`reworded_headings_checking`: ``Txn Date | Narration | Withdrawals
  / Debits | Deposits / Credits | Ending Daily Balance``, every heading in
  words a plainer statement does not use.
* :func:`amount_with_unit`: ``Date | Details | Amount (CAD) | Balance
  (CAD)``, the currency printed after each money heading.
* :func:`unknown_description_heading`: ``Date | Memo Text | Amount``; the
  one heading nobody knows stands where the description is.
* :func:`date_balance_pairs_with_title`: no transactions at all, and a line
  ``Date | DAILY ENDING BALANCE | Amount`` over lines of a date and a
  balance. The words between the date and the amount are a title, not a
  description heading: nothing on the page is a transaction.

``*_ROWS`` give what a correct reading returns: ``(payee, signed amount)``.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from tests.fixtures.synth_unseen_layouts import Line, _line, _rows

#: Headings printed in one cell are this far apart (under the cell gap).
_TIGHT: Final = 5.0
_HEAD_SIZE: Final = 9.0

Expected = tuple[tuple[str, Decimal], ...]


def _after(x: float, text: str, gap: float = _TIGHT) -> float:
    """Where a heading starts that is printed ``gap`` points after ``text`` at ``x``."""
    return x + float(stringWidth(text, "Helvetica-Bold", _HEAD_SIZE)) + gap


def _page(
    bank: str, product: str, last4: str, period: str, opening: str, closing: str
) -> tuple[io.BytesIO, canvas.Canvas]:
    """A page with everything a statement says about itself; the table goes at y=590."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _line(c, 750, [(40, bank)], size=14, bold=True)
    _line(c, 730, [(40, product), (360, f"Account number: XXXX XXXX {last4}")])
    _line(c, 716, [(40, f"Statement period: {period}")])
    _line(c, 680, [(40, "Account summary")], size=11, bold=True)
    _rows(
        c,
        664,
        [
            [(40, "Beginning balance"), (-330, f"${opening}")],
            [(40, "Ending balance"), (-330, f"${closing}")],
        ],
    )
    _line(c, 60, [(40, "Synthetic test document from the PerFin test suite.")], size=7)
    return buf, c


def _done(buf: io.BytesIO, c: canvas.Canvas) -> bytes:
    c.save()
    return buf.getvalue()


def _table(c: canvas.Canvas, header: Line, rows: Sequence[Line], y: float = 590) -> float:
    _line(c, y, header, size=_HEAD_SIZE, bold=True)
    return _rows(c, y - 14, rows)


#: ``(date, description, deposit, withdrawal, balance)``.
_MERGED: Final[tuple[tuple[str, str, str, str, str], ...]] = (
    ("03/02/2026", "Fernhill Mills Payroll", "1,500.00", "", "2,700.00"),
    ("03/05/2026", "Tallow Street Market", "", "82.40", "2,617.60"),
    ("03/09/2026", "Brindle Water Utility", "", "61.15", "2,556.45"),
    ("03/14/2026", "Mobile Deposit", "240.00", "", "2,796.45"),
    ("03/20/2026", "Orchard Lane Rent", "", "950.00", "1,846.45"),
    ("03/27/2026", "Kettle and Crumb Bakery", "", "18.30", "1,828.15"),
)
MERGED_ROWS: Final[Expected] = tuple(
    (text, Decimal(paid_in.replace(",", "")) if paid_in else -Decimal(out.replace(",", "")))
    for _, text, paid_in, out, _ in _MERGED
)


def merged_heading_checking() -> bytes:
    """``WITHDRAWAL BALANCE`` is one cell; the figures stand under each word."""
    buf, c = _page(
        "Larkspur Savings Bank",
        "Everyday Checking",
        "5521",
        "March 1, 2026 to March 31, 2026",
        "1,200.00",
        "1,828.15",
    )
    balance_x = _after(410, "WITHDRAWAL")
    _table(
        c,
        [
            (40, "DATE"),
            (110, "DESCRIPTION"),
            (330, "DEPOSIT"),
            (410, "WITHDRAWAL"),
            (balance_x, "BALANCE"),
        ],
        [
            [
                (40, day),
                (110, text),
                *([(330, paid_in)] if paid_in else [(410, out)]),
                (balance_x, balance),
            ]
            for day, text, paid_in, out, balance in _MERGED
        ],
    )
    return _done(buf, c)


#: ``(date, amount, description)``.
_AMOUNT_FIRST: Final[tuple[tuple[str, str, str], ...]] = (
    ("04/01/2026", "825.00", "Fernhill Mills Payroll"),
    ("04/03/2026", "-64.18", "Tallow Street Market"),
    ("04/08/2026", "-41.07", "Lantern Fuel Stop"),
    ("04/15/2026", "-450.00", "Orchard Lane Rent"),
    ("04/21/2026", "-27.21", "Quiet Bean Coffee"),
    ("04/28/2026", "120.00", "Mobile Deposit"),
)
AMOUNT_FIRST_ROWS: Final[Expected] = tuple(
    (text, Decimal(amount)) for _, amount, text in _AMOUNT_FIRST
)


def amount_before_description() -> bytes:
    """``Amount Description`` is one cell; the amount is printed first."""
    buf, c = _page(
        "Pinecrest Mutual Bank",
        "Standard Checking",
        "3306",
        "April 1, 2026 to April 30, 2026",
        "640.25",
        "1,002.79",
    )
    # Nine points: still one cell, and room for the widest amount before the text.
    description_x = _after(120, "Amount", 9.0)
    _table(
        c,
        [(40, "Date"), (120, "Amount"), (description_x, "Description")],
        [[(40, day), (120, amount), (description_x, text)] for day, amount, text in _AMOUNT_FIRST],
    )
    return _done(buf, c)


#: ``(transaction date, posting date, description, amount, category)``.
_TWO_DATES: Final[tuple[tuple[str, str, str, str, str], ...]] = (
    ("05/02", "05/03", "Juniper Hollow Grocer", "-58.20", "Food"),
    ("05/06", "05/07", "Slate River Transit", "-24.00", "Travel"),
    ("05/11", "05/11", "Fernhill Mills Payroll", "1,640.00", "Income"),
    ("05/15", "05/18", "Cobalt Corner Pharmacy", "-17.45", "Health"),
    ("05/22", "05/23", "Marrow and Pine Hardware", "-96.10", "Home"),
    ("05/29", "05/30", "Wren Street Cinema", "-31.00", "Fun"),
)
TWO_DATES_ROWS: Final[Expected] = tuple(
    (text, Decimal(amount.replace(",", ""))) for _, _, text, amount, _ in _TWO_DATES
)
#: The categories printed right of the amount: no payee may contain one.
CATEGORIES: Final = tuple(category for *_, category in _TWO_DATES)
#: Where the category is printed: 12 points right of the amount's right edge.
_CATEGORY_X: Final = 442.0


def two_dates_and_category() -> bytes:
    """Both date headings are one cell; a category stands right of the amount."""
    buf, c = _page(
        "Meridian Row Bank",
        "Rewards Checking",
        "9084",
        "May 1, 2026 to May 31, 2026",
        "2,310.00",
        "3,723.25",
    )
    post_x = _after(40, "Trans Date")
    _table(
        c,
        [
            (40, "Trans Date"),
            (post_x, "Post Date"),
            (170, "Description"),
            (-430, "Amount"),
            (_CATEGORY_X, "Category"),
        ],
        [
            [(40, day), (post_x, posted), (170, text), (-430, amount), (_CATEGORY_X, category)]
            for day, posted, text, amount, category in _TWO_DATES
        ],
    )
    return _done(buf, c)


#: ``(date, description, amount)``.
_PLAIN: Final[tuple[tuple[str, str, str], ...]] = (
    ("06/03/2026", "Fernhill Mills Payroll", "1,480.00"),
    ("06/05/2026", "Orchard Lane Rent", "-950.00"),
    ("06/10/2026", "Tallow Street Market", "-73.55"),
    ("06/16/2026", "Brindle Water Utility", "-58.90"),
    ("06/22/2026", "Mobile Deposit", "200.00"),
    ("06/26/2026", "Lantern Fuel Stop", "-44.25"),
)
DAILY_ROWS: Final[Expected] = tuple(
    (text, Decimal(amount.replace(",", ""))) for _, text, amount in _PLAIN
)
#: ``(day, balance)`` of the daily-balance block: the opening day, then
#: every day with a row. Three to a line.
_DAILY: Final[tuple[tuple[str, str], ...]] = (
    ("06/01", "1,912.40"),
    ("06/03", "3,392.40"),
    ("06/05", "2,442.40"),
    ("06/10", "2,368.85"),
    ("06/16", "2,309.95"),
    ("06/22", "2,509.95"),
    ("06/26", "2,465.70"),
)
DAILY_BALANCES: Final = tuple(Decimal(balance.replace(",", "")) for _, balance in _DAILY)
#: Per pair of the block: where the date starts and where the balance ends.
#: The second balance ends six points before the third date (one cell); the
#: third balance ends where the transaction table's amounts end.
_PAIRS: Final = ((40.0, 170.0), (220.0, 330.0), (336.0, 430.0))


def daily_balance_after_rows() -> bytes:
    """A table of balances by day under the transactions: not transactions."""
    buf, c = _page(
        "Copperline Trust Bank",
        "Simple Checking",
        "7412",
        "June 1, 2026 to June 30, 2026",
        "1,912.40",
        "2,465.70",
    )
    y = _table(
        c,
        [(40, "Date"), (110, "Description"), (-430, "Amount")],
        [[(40, day), (110, text), (-430, amount)] for day, text, amount in _PLAIN],
    )
    y -= 24
    _line(c, y, [(40, "DAILY ENDING BALANCE")], size=11, bold=True)
    header: list[tuple[float, str]] = []
    for date_x, amount_x in _PAIRS:
        header += [(date_x, "Date"), (-amount_x, "Amount")]
    _line(c, y - 16, header, size=_HEAD_SIZE, bold=True)
    lines: list[list[tuple[float, str]]] = []
    for start in range(0, len(_DAILY), len(_PAIRS)):
        cells: list[tuple[float, str]] = []
        for (day, balance), (date_x, amount_x) in zip(
            _DAILY[start : start + len(_PAIRS)], _PAIRS, strict=False
        ):
            cells += [(date_x, day), (-amount_x, balance)]
        lines.append(cells)
    _rows(c, y - 30, lines)
    return _done(buf, c)


def _signed(amount: str) -> Decimal:
    return Decimal(amount.replace(",", ""))


#: ``(date, description, withdrawal, deposit, balance)``.
_REWORDED: Final[tuple[tuple[str, str, str, str, str], ...]] = (
    ("07/01/2026", "Fernhill Mills Payroll", "", "1,350.00", "2,250.00"),
    ("07/03/2026", "Orchard Lane Rent", "800.00", "", "1,450.00"),
    ("07/08/2026", "Tallow Street Market", "67.35", "", "1,382.65"),
    ("07/14/2026", "Brindle Water Utility", "49.80", "", "1,332.85"),
    ("07/20/2026", "Mobile Deposit", "", "175.00", "1,507.85"),
    ("07/28/2026", "Lantern Fuel Stop", "38.60", "", "1,469.25"),
)
REWORDED_ROWS: Final[Expected] = tuple(
    (text, _signed(paid_in) if paid_in else -_signed(out)) for _, text, out, paid_in, _ in _REWORDED
)


def reworded_headings_checking() -> bytes:
    """Every heading is a longer wording; the figures stand under their own."""
    buf, c = _page(
        "Harrowgate Community Bank",
        "Neighbour Checking",
        "6630",
        "July 1, 2026 to July 31, 2026",
        "900.00",
        "1,469.25",
    )
    _table(
        c,
        [
            (40, "Txn Date"),
            (110, "Narration"),
            (280, "Withdrawals / Debits"),
            (385, "Deposits / Credits"),
            (485, "Ending Daily Balance"),
        ],
        [
            [
                (40, day),
                (110, text),
                *([(280, out)] if out else [(385, paid_in)]),
                (485, balance),
            ]
            for day, text, out, paid_in, balance in _REWORDED
        ],
    )
    return _done(buf, c)


#: ``(date, description, amount, balance)``.
_UNIT: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("08/03/2026", "Fernhill Mills Payroll", "1,200.00", "1,700.00"),
    ("08/05/2026", "Juniper Hollow Grocer", "-72.15", "1,627.85"),
    ("08/11/2026", "Slate River Transit", "-30.00", "1,597.85"),
    ("08/17/2026", "Cobalt Corner Pharmacy", "-22.40", "1,575.45"),
    ("08/24/2026", "Mobile Deposit", "60.00", "1,635.45"),
    ("08/29/2026", "Wren Street Cinema", "-28.50", "1,606.95"),
)
UNIT_ROWS: Final[Expected] = tuple((text, _signed(amount)) for _, text, amount, _ in _UNIT)


def amount_with_unit() -> bytes:
    """The currency stands in brackets after each money heading."""
    buf, c = _page(
        "Tamarack Shore Bank",
        "Chequing Account",
        "2957",
        "August 1, 2026 to August 31, 2026",
        "500.00",
        "1,606.95",
    )
    _table(
        c,
        [(40, "Date"), (110, "Details"), (330, "Amount (CAD)"), (440, "Balance (CAD)")],
        [
            [(40, day), (110, text), (330, amount), (440, balance)]
            for day, text, amount, balance in _UNIT
        ],
    )
    return _done(buf, c)


#: ``(date, description, amount)``.
_MEMO: Final[tuple[tuple[str, str, str], ...]] = (
    ("09/02/2026", "Fernhill Mills Payroll", "1,410.00"),
    ("09/04/2026", "Orchard Lane Rent", "-925.00"),
    ("09/09/2026", "Kettle and Crumb Bakery", "-14.75"),
    ("09/15/2026", "Marrow and Pine Hardware", "-88.20"),
    ("09/21/2026", "Mobile Deposit", "95.00"),
    ("09/27/2026", "Lantern Fuel Stop", "-9.60"),
)
MEMO_ROWS: Final[Expected] = tuple((text, _signed(amount)) for _, text, amount in _MEMO)


def unknown_description_heading() -> bytes:
    """``Memo Text`` is no heading anyone knows; it stands where the description is."""
    buf, c = _page(
        "Sorrel Bridge Bank",
        "Basic Checking",
        "8145",
        "September 1, 2026 to September 30, 2026",
        "1,050.00",
        "1,517.45",
    )
    _table(
        c,
        [(40, "Date"), (110, "Memo Text"), (-430, "Amount")],
        [[(40, day), (110, text), (-430, amount)] for day, text, amount in _MEMO],
    )
    return _done(buf, c)


#: ``(day, balance)`` of the only block the page prints.
_PAIRS_ONLY: Final[tuple[tuple[str, str], ...]] = (
    ("10/01/2026", "1,880.00"),
    ("10/09/2026", "1,955.60"),
    ("10/20/2026", "2,040.10"),
)
PAIR_BALANCES: Final = tuple(_signed(balance) for _, balance in _PAIRS_ONLY)


def date_balance_pairs_with_title() -> bytes:
    """A title printed between ``Date`` and ``Amount`` heads balances, not a table of rows."""
    buf, c = _page(
        "Elmwater Savings Bank",
        "Reserve Savings",
        "4478",
        "October 1, 2026 to October 31, 2026",
        "1,880.00",
        "2,040.10",
    )
    _table(
        c,
        [(40, "Date"), (150, "DAILY ENDING BALANCE"), (-430, "Amount")],
        [[(40, day), (-430, balance)] for day, balance in _PAIRS_ONLY],
    )
    return _done(buf, c)

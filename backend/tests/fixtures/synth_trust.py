# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic statements for the trust policy (import 1d): clean reads and doubts.

Everything is invented: the bank ("Tallowmere Test Bank") does not exist,
the account number is masked and the rows are made up. Each builder returns
the bytes of a one-page PDF with a text layer and a "synthetic test
document" footer. All four print a labelled period with years, a masked
account number and the same six dated rows with signed amounts.

* :func:`no_balances_statement`: no opening balance, no closing balance and
  no running balance column. Nothing on it can be checked and nothing on it
  is in doubt: a *clean read* (``app.domain.imports.service.clean_read``).
* :func:`no_balances_with_a_stray_row`: the same plus one row dated 40 days
  before the period starts (:data:`STRAY`).
* :func:`no_balances_with_an_unread_line`: the first plus a dated line whose
  amount cell is not an amount; the parser counts it as skipped.
* :func:`balanced_with_an_unread_line`: an opening balance, a closing
  balance and a running balance column that all agree with the rows, plus a
  dated notice line whose amount cell is not an amount, which the parser
  counts as skipped. (An undated line is a continuation of the description
  above it and is never counted, so the notice carries a date.)
* :func:`closing_that_disagrees`: an opening and a closing balance and no
  running balance column; the closing balance is :data:`OFF` more than the
  rows make it. The statement does not add up and nothing says which row is
  wrong: every row is in doubt for that reason only.
* :func:`stray_row_against_the_balances`: an opening and a closing balance
  that agree with the six rows, no running balance column, plus the row
  dated before the period (:data:`STRAY`). It is off by exactly that row,
  which alone is in doubt, and the row's date is a doubt of its own.

:data:`ROWS` are the six rows; :func:`expected` is what the ledger holds for
a builder's statement.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

FONT: Final = "Helvetica"
BOLD: Final = "Helvetica-Bold"

INSTITUTION: Final = "Tallowmere Test Bank"
LAST4: Final = "4417"
CURRENCY: Final = "USD"
PERIOD_START: Final = date(2026, 3, 1)
PERIOD_END: Final = date(2026, 3, 31)
OPENING: Final = Decimal("2140.55")

Row = tuple[date, Decimal, str]

#: The six rows every builder prints.
ROWS: Final[tuple[Row, ...]] = (
    (date(2026, 3, 3), Decimal("-48.20"), "Pinewick Grocers"),
    (date(2026, 3, 9), Decimal("-17.35"), "Lantern Street Books"),
    (date(2026, 3, 14), Decimal("1850.00"), "Payroll Deposit Brindle Works"),
    (date(2026, 3, 18), Decimal("-212.40"), "Harbor Mill Hardware"),
    (date(2026, 3, 23), Decimal("-64.09"), "Quartz Lane Pharmacy"),
    (date(2026, 3, 27), Decimal("-29.99"), "Juniper Corner Cafe"),
)
#: The row of :func:`no_balances_with_a_stray_row`: 40 days before the period.
STRAY: Final[Row] = (PERIOD_START - timedelta(days=40), Decimal("-81.15"), "Maple Yard Fuel")
#: The payee and the amount cell of the dated line no reader can read.
UNREAD_PAYEE: Final = "Adjustment under enquiry"
UNREAD_AMOUNT: Final = "see branch"
CLOSING: Final = OPENING + sum((amount for _, amount, _ in ROWS), Decimal(0))
#: How far the printed closing balance of :func:`closing_that_disagrees` is off.
OFF: Final = Decimal("100.00")

_DATE_X: Final = 72
_DESC_X: Final = 150
_AMOUNT_X: Final = 440
_BALANCE_X: Final = 540


def _us(day: date) -> str:
    return day.strftime("%m/%d/%Y")


def _money(value: Decimal) -> str:
    return f"{'-' if value < 0 else ''}{abs(value):,.2f}"


def expected(*, stray: bool = False) -> list[Row]:
    """``(date, amount, payee)`` of every row a builder's statement holds, sorted."""
    return sorted([*ROWS, STRAY] if stray else ROWS)


def _pdf(
    rows: Sequence[Row],
    *,
    balances: bool = False,
    unread_after: int | None = None,
    running: bool = True,
    closing: Decimal = CLOSING,
) -> bytes:
    """One page: heading, period, account, a table of ``rows``.

    ``balances`` prints the opening and closing balances (``closing`` is
    the closing balance printed) and, with ``running``, a running balance
    column; ``unread_after`` prints the unreadable dated line after that
    many rows.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont(BOLD, 14)
    c.drawString(72, 750, INSTITUTION)
    c.setFont(FONT, 10)
    c.drawString(72, 732, "Account statement")
    c.drawString(72, 716, f"Statement period: {_us(PERIOD_START)} - {_us(PERIOD_END)}")
    c.drawString(72, 700, f"Account number: ****{LAST4}")
    if balances:
        c.drawString(72, 684, f"Opening balance: {_money(OPENING)}")
        c.drawString(300, 684, f"Closing balance: {_money(closing)}")
    top = 652
    c.setFont(BOLD, 9)
    c.drawString(_DATE_X, top, "Date")
    c.drawString(_DESC_X, top, "Description")
    c.drawRightString(_AMOUNT_X, top, "Amount")
    if balances and running:
        c.drawRightString(_BALANCE_X, top, "Balance")
    c.setFont(FONT, 9)
    y = top - 16
    balance = OPENING
    for index, (posted, amount, payee) in enumerate(rows):
        if unread_after == index:
            c.drawString(_DATE_X, y, _us(posted))
            c.drawString(_DESC_X, y, UNREAD_PAYEE)
            c.drawRightString(_AMOUNT_X, y, UNREAD_AMOUNT)
            y -= 14
        balance += amount
        c.drawString(_DATE_X, y, _us(posted))
        c.drawString(_DESC_X, y, payee)
        c.drawRightString(_AMOUNT_X, y, _money(amount))
        if balances and running:
            c.drawRightString(_BALANCE_X, y, _money(balance))
        y -= 14
    c.setFont(FONT, 7)
    c.drawString(72, 60, "Synthetic test document. Not a real statement.")
    c.showPage()
    c.save()
    return buf.getvalue()


def no_balances_statement() -> bytes:
    """Six rows, a period and an account number; no balance of any kind."""
    return _pdf(ROWS)


def no_balances_with_a_stray_row() -> bytes:
    """The same plus one row dated 40 days before the period (printed first)."""
    return _pdf([STRAY, *ROWS])


def no_balances_with_an_unread_line() -> bytes:
    """The same as the first plus a dated line whose amount is not an amount."""
    return _pdf(ROWS, unread_after=3)


def balanced_with_an_unread_line() -> bytes:
    """Balances that agree with the six rows, plus one dated line nobody can read."""
    return _pdf(ROWS, balances=True, unread_after=3)


def closing_that_disagrees() -> bytes:
    """Opening and closing balances, no running balance; the closing is off."""
    return _pdf(ROWS, balances=True, running=False, closing=CLOSING + OFF)


def stray_row_against_the_balances() -> bytes:
    """Balances of the six rows, no running balance, plus the row before the period."""
    return _pdf([STRAY, *ROWS], balances=True, running=False)

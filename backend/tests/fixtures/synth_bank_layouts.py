# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bank statements whose rows sit a little off their columns.

Each builder draws one PDF the generic statement reader has to read from the
geometry people's statements really have; every institution, payee, number
and figure is invented. Each prints a masked account number, a labelled
period, an opening and a closing balance and rows that reconcile:

* :func:`left_heading_right_figures`: ``Debit | Credit | Balance`` headings
  about 20 points wide at the left of 70-point columns whose figures are
  right-aligned, so no figure overlaps its heading or lies near its centre.
* :func:`tight_date_checking`: the date 2 points before the description, so
  the two are one cell and the date column gets none.
* :func:`tight_date_one_money_column`: the same with ``Date | Description |
  Amount`` and the amount 3 points after the description: the whole row is
  one cell.
* :func:`zero_placeholder_columns`: ``Credits | Debits | Balance`` with a
  figure in both money columns of every row, one of them a zero
  (``$250.00 | -$0.00``).

:data:`EXPECTED_ROWS` gives, per builder, the rows a correct reading returns
(``(posted, amount, payee)``, money out negative).
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Final

from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from tests.fixtures.synth_unseen_layouts import _line, _rows

_FOOTER: Final = "Synthetic test document from the PerFin test suite."
_FONT: Final = "Helvetica"
_SIZE: Final = 9

#: ``(posted, amount, payee)``: what a correct reading returns.
ExpectedRow = tuple[date, Decimal, str]


def _d(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _money(value: Decimal) -> str:
    return f"{abs(value):,.2f}"


def _head(
    c: canvas.Canvas, bank: str, last4: str, period: str, opening: Decimal, closing: Decimal
) -> None:
    """The top of every statement here: who, which account, when, the two balances."""
    _line(c, 750, [(40, bank)], size=14, bold=True)
    _line(c, 750, [(380, f"Account Number: ******{last4}")])
    _line(c, 736, [(380, f"Statement Period: {period}")])
    _rows(
        c,
        700,
        [
            [(40, "Beginning Balance"), (-300, f"${_money(opening)}")],
            [(40, "Ending Balance"), (-300, f"${_money(closing)}")],
        ],
    )
    _line(c, 60, [(40, _FOOTER)], size=7)


def _fused(c: canvas.Canvas, y: float, x: float, parts: Sequence[tuple[str, float]]) -> None:
    """``parts`` (``(text, gap after it)``) one after the other from ``x``."""
    c.setFont(_FONT, _SIZE)
    for text, gap in parts:
        c.drawString(x, y, text)
        x += stringWidth(text, _FONT, _SIZE) + gap


def _running(opening: Decimal, rows: Sequence[ExpectedRow]) -> list[Decimal]:
    out: list[Decimal] = []
    balance = opening
    for _, amount, _ in rows:
        balance += amount
        out.append(balance)
    return out


# -- headings at the left of their columns, figures at the right ----------------

_LEFT_OPENING: Final = _d("1,480.25")
_LEFT_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 3, 2), _d("2,150.00"), "Payroll Deposit Fennel Works"),
    (date(2026, 3, 4), _d("-1,275.00"), "Rent Alder Court Apartments"),
    (date(2026, 3, 6), _d("-86.42"), "POS Purchase Pine Street Market"),
    (date(2026, 3, 11), _d("300.00"), "Online Transfer From Savings"),
    (date(2026, 3, 13), _d("-142.17"), "Tidewater Electric Bill Pay"),
    (date(2026, 3, 18), _d("-60.00"), "ATM Withdrawal Canal Street"),
    (date(2026, 3, 24), _d("-218.90"), "POS Purchase Ridgeline Outfitters"),
    (date(2026, 3, 30), _d("45.50"), "Mobile Deposit"),
)
#: Left edge of each 70-point money column (its heading starts there; its
#: figures end 70 points on).
_DEBIT_X: Final = 285.0
_CREDIT_X: Final = 390.0
_BALANCE_X: Final = 495.0
_COLUMN: Final = 70.0


def left_heading_right_figures() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_LEFT_OPENING, _LEFT_ROWS)[-1]
    _head(c, "Tamarack Savings Bank", "2046", "03/01/2026 - 03/31/2026", _LEFT_OPENING, closing)
    _line(
        c,
        650,
        [
            (40, "Date"),
            (100, "Description"),
            (_DEBIT_X, "Debit"),
            (_CREDIT_X, "Credit"),
            (_BALANCE_X, "Balance"),
        ],
        size=8,
    )
    lines = []
    for (posted, amount, payee), balance in zip(
        _LEFT_ROWS, _running(_LEFT_OPENING, _LEFT_ROWS), strict=True
    ):
        column = _DEBIT_X if amount < 0 else _CREDIT_X
        lines.append(
            [
                (40, posted.strftime("%m/%d/%Y")),
                (100, payee),
                (-(column + _COLUMN), _money(amount)),
                (-(_BALANCE_X + _COLUMN), _money(balance)),
            ]
        )
    _rows(c, 636, lines)
    c.save()
    return buf.getvalue()


# -- the date printed close to the description ----------------------------------

_TIGHT_OPENING: Final = _d("905.60")
_TIGHT_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 5, 1), _d("-720.00"), "Rent Juniper Row Housing"),
    (date(2026, 5, 4), _d("1,640.00"), "Direct Deposit Calloway Freight"),
    (date(2026, 5, 7), _d("-52.38"), "POS Purchase Marigold Grocers"),
    (date(2026, 5, 12), _d("-18.00"), "Transit Pass Reload"),
    (date(2026, 5, 15), _d("-96.14"), "Brightwater Utilities Online Pmt"),
    (date(2026, 5, 21), _d("-40.00"), "ATM Withdrawal Depot Square"),
    (date(2026, 5, 28), _d("125.00"), "Transfer From Savings"),
)


def tight_date_checking() -> bytes:
    """``MM/DD`` two points before the description; amount and balance in their columns."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    balances = _running(_TIGHT_OPENING, _TIGHT_ROWS)
    _head(
        c, "Copperfield Trust Bank", "5583", "05/01/2026 - 05/31/2026", _TIGHT_OPENING, balances[-1]
    )
    _line(
        c,
        650,
        [(40, "Date"), (75, "Description"), (-430, "Amount"), (-520, "Balance")],
        size=8,
    )
    y = 636.0
    for (posted, amount, payee), balance in zip(_TIGHT_ROWS, balances, strict=True):
        _fused(c, y, 40, [(posted.strftime("%m/%d"), 2.0), (payee, 0.0)])
        _line(c, y, [(-430, f"{amount:,.2f}"), (-520, _money(balance))])
        y -= 13
    c.save()
    return buf.getvalue()


_ONE_OPENING: Final = _d("2,310.40")
_ONE_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 7, 2), _d("-64.18"), "POS Purchase Saltmarsh Bakery"),
    (date(2026, 7, 6), _d("1,905.00"), "Direct Deposit Orchard Lane Studio"),
    (date(2026, 7, 9), _d("-1,100.00"), "Rent Wren Hollow Lettings"),
    (date(2026, 7, 14), _d("-37.25"), "Kestrel Mobile Autopay"),
    (date(2026, 7, 20), _d("-12.00"), "Monthly Maintenance Charge"),
    (date(2026, 7, 27), _d("-208.63"), "POS Purchase Foxglove Hardware"),
)


def tight_date_one_money_column() -> bytes:
    """Date, description and a signed amount a few points apart: one cell a row."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_ONE_OPENING, _ONE_ROWS)[-1]
    _head(c, "Larkspur Federal Bank", "9172", "07/01/2026 - 07/31/2026", _ONE_OPENING, closing)
    _line(c, 650, [(40, "Date"), (95, "Description"), (250, "Amount")], size=8)
    y = 636.0
    for posted, amount, payee in _ONE_ROWS:
        _fused(
            c,
            y,
            40,
            [(posted.strftime("%m/%d/%Y"), 2.0), (payee, 3.0), (f"{amount:,.2f}", 0.0)],
        )
        y -= 13
    c.save()
    return buf.getvalue()


# -- a figure in both money columns, one of them a zero -------------------------

_ZERO_OPENING: Final = _d("1,480.25")
_ZERO_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 9, 3), _d("250.00"), "Transfer From External Account"),
    (date(2026, 9, 8), _d("-64.18"), "Withdrawal To Linked Checking"),
    (date(2026, 9, 15), _d("1,200.00"), "Direct Deposit Hawthorn Press"),
    (date(2026, 9, 19), _d("-300.00"), "Online Transfer To Money Market"),
    (date(2026, 9, 25), _d("3.07"), "Savings Yield Paid"),
    (date(2026, 9, 29), _d("-45.00"), "Outgoing Wire Charge"),
)


def zero_placeholder_columns() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    balances = _running(_ZERO_OPENING, _ZERO_ROWS)
    _head(
        c, "Bellwether Online Bank", "3305", "09/01/2026 - 09/30/2026", _ZERO_OPENING, balances[-1]
    )
    _line(
        c,
        650,
        [
            (40, "Date"),
            (110, "Description"),
            (-380, "Credits"),
            (-460, "Debits"),
            (-550, "Balance"),
        ],
        size=8,
    )
    lines = []
    for (posted, amount, payee), balance in zip(_ZERO_ROWS, balances, strict=True):
        credit = amount if amount > 0 else Decimal(0)
        debit = -amount if amount < 0 else Decimal(0)
        lines.append(
            [
                (40, posted.strftime("%m/%d/%Y")),
                (110, payee),
                (-380, f"${_money(credit)}"),
                (-460, f"-${_money(debit)}"),
                (-550, f"${_money(balance)}"),
            ]
        )
    _rows(c, 636, lines)
    c.save()
    return buf.getvalue()


EXPECTED_ROWS: Final[dict[str, list[ExpectedRow]]] = {
    "left_heading_right_figures": list(_LEFT_ROWS),
    "tight_date_checking": list(_TIGHT_ROWS),
    "tight_date_one_money_column": list(_ONE_ROWS),
    "zero_placeholder_columns": list(_ZERO_ROWS),
}

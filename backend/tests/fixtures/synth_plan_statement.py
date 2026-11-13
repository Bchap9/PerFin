# SPDX-License-Identifier: AGPL-3.0-or-later
"""A synthetic 401(k) plan statement in the roll-forward layout (reportlab).

Everything here is invented: the record keeper (``Example Plan Services``),
the funds, the dates and every figure. The statement prints what a plan's
quarterly statement prints and a brokerage statement does not: no units and
no prices, but one row of dollars per fund under
``Beginning Balance | Earnings/Fees | Contributions | Withdrawals |
Transfers | Ending Balance``, a ``Total`` row, and the same money again by
contribution source.

Every row adds up (beginning plus the columns between is ending) and the
rows add up to the total, in both tables. :func:`plan_statement_pdf`:

* ``tamper``: the beginning balance of the first table's ``Total`` row is
  printed 100.00 too high, so that row no longer adds up (every fund row
  still does, and the ending total is unchanged).
* ``by_source``: the second table, by contribution source, same totals.
* ``second_plan``: a second page with another plan's table and another
  ending total.
* ``subtracted``: the ``Withdrawals`` column prints what left the plan as a
  positive figure (the reader has to subtract it).
* ``period``: the dates printed after ``for period:``.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from typing import Final

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

FONT: Final = "Helvetica"
BOLD: Final = "Helvetica-Bold"
INSTITUTION: Final = "Example Plan Services"
PERIOD: Final = (date(2026, 1, 1), date(2026, 3, 31))
HEADERS: Final = (
    "Beginning Balance",
    "Earnings/Fees",
    "Contributions",
    "Withdrawals",
    "Transfers",
    "Ending Balance",
)
#: Right edge of each amount column.
COLUMNS: Final = (240, 305, 375, 440, 500, 580)

Row = tuple[str, str, str, str, str, str, str]

FUNDS: Final[tuple[Row, ...]] = (
    (
        "Example Target 2050 Fund",
        "20,000.00",
        "500.00",
        "1,200.00",
        "0.00",
        "(300.00)",
        "21,400.00",
    ),
    ("Example Bond Index Fund", "8,000.00", "(120.00)", "600.00", "0.00", "300.00", "8,780.00"),
    ("Example Stable Value Fund", "2,000.00", "10.00", "0.00", "0.00", "0.00", "2,010.00"),
)
SOURCES: Final[tuple[Row, ...]] = (
    ("Employee Deferral", "18,000.00", "234.00", "1,200.00", "0.00", "0.00", "19,434.00"),
    ("Employer Match", "12,000.00", "156.00", "600.00", "0.00", "0.00", "12,756.00"),
)
TOTAL: Final[Row] = ("Total", "$30,000.00", "$390.00", "$1,800.00", "$0.00", "$0.00", "$32,190.00")
OPENING: Final = Decimal("30000.00")
CLOSING: Final = Decimal("32190.00")
#: The movement columns' totals, in the order printed.
MOVEMENTS: Final = (Decimal("390.00"), Decimal("1800.00"), Decimal("0.00"), Decimal("0.00"))

#: ``subtracted``: 450.00 left the bond fund and is printed without a sign.
SUBTRACTED_FUNDS: Final[tuple[Row, ...]] = (
    FUNDS[0],
    ("Example Bond Index Fund", "8,000.00", "(120.00)", "600.00", "450.00", "300.00", "8,330.00"),
    FUNDS[2],
)
SUBTRACTED_TOTAL: Final[Row] = (
    "Total",
    "$30,000.00",
    "$390.00",
    "$1,800.00",
    "$450.00",
    "$0.00",
    "$31,740.00",
)
SUBTRACTED_CLOSING: Final = Decimal("31740.00")

#: ``second_plan``: another plan on a second page.
OTHER_FUNDS: Final[tuple[Row, ...]] = (
    ("Example Money Market Fund", "5,000.00", "50.00", "100.00", "0.00", "0.00", "5,150.00"),
)
OTHER_TOTAL: Final[Row] = ("Total", "$5,000.00", "$50.00", "$100.00", "$0.00", "$0.00", "$5,150.00")


def _table(c: canvas.Canvas, y: float, title: str, rows: tuple[Row, ...], total: Row) -> float:
    c.setFont(BOLD, 8)
    c.drawString(40, y, title)
    for header, x in zip(HEADERS, COLUMNS, strict=True):
        c.drawRightString(x, y, header)
    c.setFont(FONT, 8)
    for row in (*rows, total):
        y -= 14
        c.drawString(40, y, row[0])
        for text, x in zip(row[1:], COLUMNS, strict=True):
            c.drawRightString(x, y, text)
    return y - 14


def plan_statement_pdf(
    *,
    tamper: bool = False,
    by_source: bool = True,
    second_plan: bool = False,
    subtracted: bool = False,
    period: str = "01/01/2026 - 03/31/2026",
) -> bytes:
    """The plan statement (module doc)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont(BOLD, 12)
    c.drawString(40, 750, INSTITUTION)
    c.setFont(FONT, 9)
    c.drawString(40, 734, "Example Works Retirement Savings Plan")
    c.setFont(BOLD, 10)
    c.drawString(40, 706, "Account Summary")
    c.setFont(FONT, 9)
    c.drawString(40, 690, f"for period: {period}")

    funds = SUBTRACTED_FUNDS if subtracted else FUNDS
    total = SUBTRACTED_TOTAL if subtracted else TOTAL
    first: Row = (total[0], "$30,100.00", total[2], total[3], total[4], total[5], total[6])
    y = _table(c, 656, "By Investment", funds, first if tamper else total)
    c.setFont(FONT, 9)
    c.drawString(40, y, "Vested Balance")
    c.drawRightString(COLUMNS[-1], y, "$31,000.00")
    y -= 30
    if by_source and not subtracted:
        _table(c, y, "By Source", SOURCES, total)
    if second_plan:
        c.showPage()
        c.setFont(BOLD, 12)
        c.drawString(40, 750, INSTITUTION)
        c.setFont(FONT, 9)
        c.drawString(40, 734, "Example Works Profit Sharing Plan")
        _table(c, 700, "By Investment", OTHER_FUNDS, OTHER_TOTAL)
    c.showPage()
    c.save()
    return buf.getvalue()

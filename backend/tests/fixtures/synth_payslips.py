# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic payslips in the layouts payroll services print (reportlab).

Everything here is invented: the employer (``Initech Example Ltd``), the
person, the dates and every figure. Each layout is internally consistent
(gross less the deductions is the net pay, in the current column and in the
year-to-date column alike), so a reader that took the wrong column would
still add up: the tests compare the figures themselves.

* :func:`summary_columns_pdf`: a heading row ``EMPLOYER | PAY PERIOD`` with
  the name under the first heading, ``Period Beginning`` / ``Period Ending``
  / ``Pay Date`` labels, pay and deductions side by side under one header
  line (``PAY | Hours | Rate | Current | YTD | DEDUCTIONS | Current | YTD``)
  and a summary of ``Total Pay``, ``Deductions`` and ``Net Pay``, each with
  a current and a year-to-date figure. ``ytd_first`` prints the
  year-to-date column before the current one everywhere.
* :func:`this_period_pdf`: an ``Earnings Statement`` with a table headed
  ``Taxes / Deductions | Type | This Period | YTD`` and no total line;
  ``sentence`` adds the line some services print in place of a total
  (``Your taxes and deductions for this period are $324.25``).
* :func:`labels_over_values_lines`: the lines of a scanned stub whose
  labels stand over their values, one unrelated line read badly by OCR.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Final

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.ingestion.parsers.base import Cell, ViewLine

FONT: Final = "Helvetica"
BOLD: Final = "Helvetica-Bold"
EMPLOYER: Final = "Initech Example Ltd"

#: ``summary_columns_pdf``: (label, current, year to date).
SUMMARY_DEDUCTIONS: Final = (
    ("Federal tax", Decimal("288.00"), Decimal("1152.00")),
    ("Social insurance", Decimal("183.60"), Decimal("734.40")),
    ("Retirement plan", Decimal("120.00"), Decimal("480.00")),
)
SUMMARY_GROSS: Final = (Decimal("2400.00"), Decimal("9600.00"))
SUMMARY_TOTAL: Final = (Decimal("591.60"), Decimal("2366.40"))
SUMMARY_NET: Final = (Decimal("1808.40"), Decimal("7233.60"))

#: ``this_period_pdf``: (label, type, this period, year to date).
PERIOD_DEDUCTIONS: Final = (
    ("Federal income tax", "Tax", Decimal("175.00"), Decimal("700.00")),
    ("State tax", "Tax", Decimal("61.25"), Decimal("245.00")),
    ("Health plan", "Deduction", Decimal("88.00"), Decimal("352.00")),
)
PERIOD_GROSS: Final = Decimal("1750.00")
PERIOD_TOTAL: Final = Decimal("324.25")
PERIOD_NET: Final = Decimal("1425.75")

#: OCR confidence of the cells of :func:`labels_over_values_lines`.
STUB_CONF: Final = 0.95


def money(value: Decimal, *, symbol: str = "") -> str:
    return f"{symbol}{value:,.2f}"


def summary_columns_pdf(*, ytd_first: bool = False) -> bytes:
    """Pay and deductions side by side, a current and a year-to-date column each."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)

    def pair(current: Decimal, ytd: Decimal, *, symbol: str = "") -> tuple[str, str]:
        texts = (money(current, symbol=symbol), money(ytd, symbol=symbol))
        return (texts[1], texts[0]) if ytd_first else texts

    heads = ("YTD", "Current") if ytd_first else ("Current", "YTD")

    c.setFont(BOLD, 10)
    c.drawString(72, 750, "EMPLOYER")
    c.drawString(330, 750, "PAY PERIOD")
    c.setFont(FONT, 10)
    c.drawString(72, 734, EMPLOYER)
    c.drawString(330, 734, "Period Beginning:")
    c.drawString(450, 734, "02/01/2026")
    c.drawString(72, 718, "40 Sample Works Road")
    c.drawString(330, 718, "Period Ending:")
    c.drawString(450, 718, "02/15/2026")
    c.drawString(330, 702, "Pay Date:")
    c.drawString(450, 702, "02/20/2026")
    c.setFont(BOLD, 10)
    c.drawString(72, 686, "EMPLOYEE")
    c.setFont(FONT, 10)
    c.drawString(72, 670, "Jordan Example")

    y = 630
    c.setFont(BOLD, 9)
    c.drawString(72, y, "PAY")
    c.drawRightString(170, y, "Hours")
    c.drawRightString(215, y, "Rate")
    c.drawRightString(270, y, heads[0])
    c.drawRightString(325, y, heads[1])
    c.drawString(345, y, "DEDUCTIONS")
    c.drawRightString(500, y, heads[0])
    c.drawRightString(560, y, heads[1])
    c.setFont(FONT, 9)
    y -= 16
    first, second = pair(*SUMMARY_GROSS)
    c.drawString(72, y, "Regular")
    c.drawRightString(170, y, "80.00")
    c.drawRightString(215, y, "30.00")
    c.drawRightString(270, y, first)
    c.drawRightString(325, y, second)
    for label, current, ytd in SUMMARY_DEDUCTIONS:
        first, second = pair(current, ytd)
        c.drawString(345, y, label)
        c.drawRightString(500, y, first)
        c.drawRightString(560, y, second)
        y -= 16

    y -= 14
    c.setFont(BOLD, 10)
    c.drawString(72, y, "SUMMARY")
    c.setFont(FONT, 10)
    y -= 16
    for label, (current, ytd) in (
        ("Total Pay", SUMMARY_GROSS),
        ("Deductions", SUMMARY_TOTAL),
        ("Net Pay", SUMMARY_NET),
    ):
        first, second = pair(current, ytd, symbol="$")
        c.drawString(72, y, label)
        c.drawRightString(270, y, first)
        c.drawRightString(325, y, second)
        y -= 16
    c.showPage()
    c.save()
    return buf.getvalue()


def this_period_pdf(*, sentence: bool = False) -> bytes:
    """A deductions table with ``This Period`` and ``YTD`` columns and no total line."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont(BOLD, 14)
    c.drawString(72, 750, EMPLOYER)
    c.setFont(BOLD, 12)
    c.drawString(72, 730, "Earnings Statement")
    c.setFont(FONT, 10)
    c.drawString(72, 712, "Pay period: 03/01/2026 to 03/15/2026")
    c.drawString(330, 712, "Pay date: 03/20/2026")
    c.drawString(72, 688, "Gross Pay")
    c.drawRightString(320, 688, money(PERIOD_GROSS))
    y = 660
    c.setFont(BOLD, 10)
    c.drawString(72, y, "Taxes / Deductions")
    c.drawString(250, y, "Type")
    c.drawRightString(440, y, "This Period")
    c.drawRightString(520, y, "YTD")
    c.setFont(FONT, 10)
    y -= 16
    for label, kind, current, ytd in PERIOD_DEDUCTIONS:
        c.drawString(72, y, label)
        c.drawString(250, y, kind)
        c.drawRightString(440, y, money(current))
        c.drawRightString(520, y, money(ytd))
        y -= 16
    if sentence:
        y -= 4
        c.drawString(
            72,
            y,
            f"Your taxes and deductions for this period are {money(PERIOD_TOTAL, symbol='$')}",
        )
        y -= 16
    y -= 8
    c.drawString(72, y, "Net Pay")
    c.drawRightString(320, y, money(PERIOD_NET))
    c.showPage()
    c.save()
    return buf.getvalue()


def _line(index: int, *cells: tuple[str, float], conf: float = STUB_CONF) -> ViewLine:
    y = 60.0 + 24.0 * index
    built = tuple(Cell(text, x0, y, x0 + 6.0 * len(text), y + 11.0, conf) for text, x0 in cells)
    return ViewLine(
        page=1, index=index, cells=built, text=" ".join(text for text, _ in cells), height=11.0
    )


def labels_over_values_lines() -> list[ViewLine]:
    """A scanned stub: three labels over their values, one line OCR read badly."""
    return [
        _line(0, ("Pay Stub", 40.0)),
        _line(1, ("Gross pay", 40.0), ("Total deductions", 200.0), ("Net pay", 400.0)),
        _line(2, ("$3,100.00", 40.0), ("- $930.00", 200.0), ("$2,170.00", 400.0)),
        _line(3, ("Keep this stub with your records", 40.0), conf=0.02),
    ]

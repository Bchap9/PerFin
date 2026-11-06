# SPDX-License-Identifier: AGPL-3.0-or-later
"""A synthetic statement layout no parser knows, for "Teach PerFin" (INGESTION.md).

An invented institution (*Larkfeather Mutual Test Society*) with labels and
column headers chosen to be in none of the parsers' vocabularies, so the
first statement of it reads nothing (``unparsed``/``no_records``) until a
person labels it; the second statement of the same layout (other dates,
amounts and rows) must then import on its own.

* ``Member key: QF 7342-1180-<last4>`` (the account number, right of its label).
* ``Window`` above ``06 Apr 2026 to 05 May 2026`` (the period, below its label).
* ``Carried in`` / ``Carried out`` with the balances right of them.
* A ledger headed ``Booked | Narrative | Movement | Kept``: ``DD Mon YYYY``
  dates, signed movements, the balance kept after each row; a wrapped
  narrative continues on the next line; ``Subtotal so far`` lines inside the
  ledger (to be ignored); ``End of ledger`` closes it. The ledger continues
  on page 2 under a repeated header.

Every page says it is a synthetic test document; every name and figure is
invented.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

INSTITUTION: Final = "Larkfeather Mutual Test Society"
FONT: Final = "Helvetica"
BOLD: Final = "Helvetica-Bold"
DISCLAIMER: Final = "SYNTHETIC TEST DOCUMENT - INVENTED DATA - NOT A REAL STATEMENT"
#: Column x positions (points).
COLUMNS: Final = (60.0, 150.0, 400.0, 490.0)


@dataclass(frozen=True)
class Row:
    day: date
    narrative: str
    amount: Decimal
    wrapped: str | None = None


@dataclass(frozen=True)
class TaughtStatement:
    last4: str
    start: date
    end: date
    opening: Decimal
    rows: tuple[Row, ...]

    @property
    def closing(self) -> Decimal:
        return self.opening + sum((r.amount for r in self.rows), Decimal(0))


def _rows(start: date, seed: int, count: int) -> tuple[Row, ...]:
    names = (
        "Lantern Grocers",
        "Pebblebrook Utilities",
        "Salary Orchid Works",
        "Copperleaf Pharmacy",
        "Thistle Rail Pass",
        "Moonwater Cinema",
        "Driftwood Bakery",
        "Juniper Insurance",
    )
    out: list[Row] = []
    for index in range(count):
        cents = (seed * 7919 + index * 104729) % 9000 + 500
        amount = Decimal(cents) / 100
        name = names[(seed + index) % len(names)]
        income = name.startswith("Salary")
        out.append(
            Row(
                start + timedelta(days=1 + (index * 29) // count),
                name,
                amount * 9 if income else -amount,
                "Reference memo continued" if index % 5 == 2 else None,
            )
        )
    return tuple(out)


def statement_one() -> TaughtStatement:
    start = date(2026, 4, 6)
    return TaughtStatement("2468", start, date(2026, 5, 5), Decimal("1520.40"), _rows(start, 3, 26))


def statement_two() -> TaughtStatement:
    start = date(2026, 5, 6)
    return TaughtStatement(
        "2468", start, date(2026, 6, 5), statement_one().closing, _rows(start, 5, 23)
    )


def _day(value: date) -> str:
    return value.strftime("%d %b %Y")


def money(value: Decimal) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}{abs(value):,.2f}"


def pdf(stmt: TaughtStatement, *, rows_per_page: int = 16) -> bytes:
    """The statement as a two-page PDF with a text layer (reportlab)."""
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=letter)
    page.setTitle(f"{INSTITUTION} ledger summary")
    page.setAuthor(INSTITUTION)
    height = letter[1]
    chunks = [stmt.rows[i : i + rows_per_page] for i in range(0, len(stmt.rows), rows_per_page)]
    kept = stmt.opening
    for number, chunk in enumerate(chunks, start=1):
        y = height - 50
        page.setFont(BOLD, 14)
        page.drawString(60, y, INSTITUTION)
        page.setFont(FONT, 8)
        page.drawString(330, y, DISCLAIMER[:44])
        y -= 24
        page.setFont(FONT, 10)
        if number == 1:
            page.drawString(60, y, f"Member key: QF 7342-1180-{stmt.last4}")
            y -= 22
            page.drawString(60, y, "Window")
            y -= 14
            page.drawString(60, y, f"{_day(stmt.start)} to {_day(stmt.end)}")
            y -= 22
            page.drawString(60, y, f"Carried in: {money(stmt.opening)}")
            page.drawString(300, y, f"Carried out: {money(stmt.closing)}")
            y -= 30
        page.setFont(BOLD, 10)
        for x, text in zip(COLUMNS, ("Booked", "Narrative", "Movement", "Kept"), strict=True):
            page.drawString(x, y, text)
        y -= 16
        page.setFont(FONT, 10)
        for index, row in enumerate(chunk):
            kept += row.amount
            page.drawString(COLUMNS[0], y, _day(row.day))
            page.drawString(COLUMNS[1], y, row.narrative)
            page.drawRightString(COLUMNS[2] + 55, y, money(row.amount))
            page.drawRightString(COLUMNS[3] + 55, y, money(kept))
            y -= 14
            if row.wrapped:
                page.drawString(COLUMNS[1], y, row.wrapped)
                y -= 14
            if index % 8 == 7:
                page.drawString(COLUMNS[1], y, "Subtotal so far")
                page.drawRightString(COLUMNS[3] + 55, y, money(kept))
                y -= 14
        if number == len(chunks):
            y -= 6
            page.drawString(COLUMNS[0], y, "End of ledger")
        page.setFont(FONT, 8)
        page.drawString(60, 40, f"Sheet {number} of {len(chunks)}  {DISCLAIMER}")
        page.showPage()
    page.save()
    return buffer.getvalue()


def expected(stmt: TaughtStatement) -> list[tuple[date, Decimal, str]]:
    return [
        (r.day, r.amount, f"{r.narrative} {r.wrapped}" if r.wrapped else r.narrative)
        for r in stmt.rows
    ]

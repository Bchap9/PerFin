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

Four more statements of the same invented society, for what a taught layout
can be told since import 1d (each builder returns the PDF's bytes; the
figures a test expects are in the spec beside it):

* :func:`two_tables_statement` (:func:`split_statement`): ``Money received``
  and ``Money sent``, two tables under the same headers ``Booked |
  Narrative | Movement``, every figure printed without a sign. Carried in
  and carried out agree only when the second table is read as money out.
* :func:`continued_table_statement` (:func:`continued_statement`): one
  ledger of 30 rows over two sheets; only the first prints the header. The
  second sheet has a print date beside the society's name and, under ``End
  of ledger``, a dated notice with a figure that is no row.
* :func:`month_name_rows_statement` (:func:`month_name_statement`): rows
  dated ``Dec 12`` without a year, in a window that crosses the new year.
* :func:`card_positive_charges_statement` (:func:`card_statement`): a card's
  sheet, ``Owed before`` / ``Owed now`` and a table ``Booked | Narrative |
  Charged`` whose charges are printed positive, a payment with a minus and
  a refund with ``CR``.

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


# ---------------------------------------------------------------------------
# Import 1d: several tables, a continued table, month-name dates, a card
# ---------------------------------------------------------------------------

_PAYERS: Final = ("Orchid Works Payroll", "Heronsgate Rent Share", "Tidewell Refund Desk")
_PAYEES: Final = (
    "Lantern Grocers",
    "Pebblebrook Utilities",
    "Copperleaf Pharmacy",
    "Thistle Rail Pass",
    "Moonwater Cinema",
    "Driftwood Bakery",
)


@dataclass(frozen=True)
class SplitStatement:
    """Money received and money sent in two tables; ``sent`` amounts are negative."""

    last4: str
    start: date
    end: date
    opening: Decimal
    received: tuple[Row, ...]
    sent: tuple[Row, ...]

    @property
    def rows(self) -> tuple[Row, ...]:
        """The rows in the order they are printed."""
        return (*self.received, *self.sent)

    @property
    def closing(self) -> Decimal:
        return self.opening + sum((r.amount for r in self.rows), Decimal(0))


def _plain(rows: tuple[Row, ...]) -> tuple[Row, ...]:
    """``rows`` without their wrapped second lines."""
    return tuple(Row(r.day, r.narrative, r.amount) for r in rows)


def split_statement() -> SplitStatement:
    start = date(2026, 7, 6)
    received = tuple(
        Row(start + timedelta(days=3 + 9 * i), _PAYERS[i], Decimal(61250 + 4175 * i) / 100)
        for i in range(3)
    )
    sent = tuple(
        Row(
            start + timedelta(days=1 + 4 * i),
            _PAYEES[i % len(_PAYEES)],
            -Decimal(1830 + 977 * i) / 100,
        )
        for i in range(7)
    )
    return SplitStatement("3157", start, date(2026, 8, 5), Decimal("842.15"), received, sent)


def continued_statement() -> TaughtStatement:
    start = date(2026, 9, 6)
    return TaughtStatement(
        "6094", start, date(2026, 10, 5), Decimal("2310.75"), _rows(start, 11, 30)
    )


def month_name_statement() -> TaughtStatement:
    """A window over the new year: ``Dec`` rows are last year's, ``Jan`` rows this year's."""
    start = date(2025, 12, 10)
    return TaughtStatement(
        "8821",
        start,
        date(2026, 1, 9),
        Decimal("975.00"),
        _plain(_rows(start, 7, 12)),
    )


@dataclass(frozen=True)
class CardRow:
    day: date
    narrative: str
    #: The figure as the sheet prints it (a charge has no sign).
    printed: str
    #: What the ledger keeps: a charge is negative, a payment or refund positive.
    amount: Decimal


@dataclass(frozen=True)
class CardStatement:
    last4: str
    start: date
    end: date
    #: Owed at the start, printed positive.
    owed_before: Decimal
    rows: tuple[CardRow, ...]

    @property
    def owed_now(self) -> Decimal:
        return self.owed_before - sum((r.amount for r in self.rows), Decimal(0))


def card_statement() -> CardStatement:
    start = date(2026, 2, 11)
    rows: list[CardRow] = []
    for index in range(8):
        value = Decimal(2240 + 1315 * index) / 100
        rows.append(
            CardRow(
                start + timedelta(days=2 + 3 * index),
                _PAYEES[(index + 2) % len(_PAYEES)],
                money(value),
                -value,
            )
        )
    rows.insert(
        3,
        CardRow(
            start + timedelta(days=9), "Payment received with thanks", "-200.00", Decimal("200.00")
        ),
    )
    rows.insert(
        6,
        CardRow(
            start + timedelta(days=16),
            "Returned item credit",
            "18.40 CR",
            Decimal("18.40"),
        ),
    )
    return CardStatement("4410", start, date(2026, 3, 10), Decimal("412.30"), tuple(rows))


def _open(title: str) -> tuple[io.BytesIO, canvas.Canvas]:
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=letter)
    page.setTitle(f"{INSTITUTION} {title}")
    page.setAuthor(INSTITUTION)
    return buffer, page


def _masthead(page: canvas.Canvas, *, beside: str | None = None) -> float:
    """The society's name and the disclaimer; the y of the next line."""
    y = float(letter[1]) - 50
    page.setFont(BOLD, 14)
    page.drawString(60, y, INSTITUTION)
    page.setFont(FONT, 8)
    page.drawString(330, y, beside if beside is not None else DISCLAIMER[:44])
    page.setFont(FONT, 10)
    return y - 24


def _summary(
    page: canvas.Canvas,
    y: float,
    *,
    last4: str,
    start: date,
    end: date,
    balances: tuple[tuple[str, Decimal], tuple[str, Decimal]],
) -> float:
    page.drawString(60, y, f"Member key: QF 7342-1180-{last4}")
    y -= 22
    page.drawString(60, y, "Window")
    y -= 14
    page.drawString(60, y, f"{_day(start)} to {_day(end)}")
    y -= 22
    page.drawString(60, y, f"{balances[0][0]}: {money(balances[0][1])}")
    page.drawString(300, y, f"{balances[1][0]}: {money(balances[1][1])}")
    return y - 30


def _headers(page: canvas.Canvas, y: float, headers: tuple[str, ...]) -> float:
    page.setFont(BOLD, 10)
    for x, text in zip(COLUMNS, headers, strict=False):
        page.drawString(x, y, text)
    page.setFont(FONT, 10)
    return y - 16


def _foot(page: canvas.Canvas, number: int, of: int) -> None:
    page.setFont(FONT, 8)
    page.drawString(60, 40, f"Sheet {number} of {of}  {DISCLAIMER}")
    page.showPage()


def two_tables_statement() -> bytes:
    """:func:`split_statement` as a one-page PDF: two tables, no figure signed."""
    stmt = split_statement()
    buffer, page = _open("money received and sent")
    y = _masthead(page)
    y = _summary(
        page,
        y,
        last4=stmt.last4,
        start=stmt.start,
        end=stmt.end,
        balances=(("Carried in", stmt.opening), ("Carried out", stmt.closing)),
    )
    for title, rows, end in (
        ("Money received", stmt.received, "Received in all"),
        ("Money sent", stmt.sent, "Sent in all"),
    ):
        page.setFont(BOLD, 11)
        page.drawString(60, y, title)
        y -= 18
        y = _headers(page, y, ("Booked", "Narrative", "Movement"))
        for row in rows:
            page.drawString(COLUMNS[0], y, _day(row.day))
            page.drawString(COLUMNS[1], y, row.narrative)
            page.drawRightString(COLUMNS[2] + 55, y, money(abs(row.amount)))
            y -= 14
        y -= 4
        page.drawString(COLUMNS[0], y, end)
        total = sum((abs(r.amount) for r in rows), Decimal(0))
        page.drawRightString(COLUMNS[2] + 55, y, money(total))
        y -= 30
    _foot(page, 1, 1)
    page.save()
    return buffer.getvalue()


#: Rows of :func:`continued_table_statement` on its first sheet.
CONTINUED_FIRST_SHEET: Final = 18
#: The notice printed under ``End of ledger`` (dated, with a figure; no row).
CONTINUED_NOTICE: Final = "Yearly sheet charge notice"


def continued_table_statement() -> bytes:
    """:func:`continued_statement` over two sheets; the header is on the first only."""
    stmt = continued_statement()
    buffer, page = _open("ledger summary continued")
    kept = stmt.opening
    y = _masthead(page)
    y = _summary(
        page,
        y,
        last4=stmt.last4,
        start=stmt.start,
        end=stmt.end,
        balances=(("Carried in", stmt.opening), ("Carried out", stmt.closing)),
    )
    y = _headers(page, y, ("Booked", "Narrative", "Movement", "Kept"))
    for index, row in enumerate(stmt.rows):
        if index == CONTINUED_FIRST_SHEET:
            _foot(page, 1, 2)
            # A date beside the name: it is not in the ledger's date column.
            y = _masthead(page, beside=f"Sheet printed {_day(stmt.end + timedelta(days=1))}")
        kept += row.amount
        page.drawString(COLUMNS[0], y, _day(row.day))
        page.drawString(COLUMNS[1], y, row.narrative)
        page.drawRightString(COLUMNS[2] + 55, y, money(row.amount))
        page.drawRightString(COLUMNS[3] + 55, y, money(kept))
        y -= 14
        if row.wrapped:
            page.drawString(COLUMNS[1], y, row.wrapped)
            y -= 14
    y -= 6
    page.drawString(COLUMNS[0], y, "End of ledger")
    y -= 28
    page.drawString(COLUMNS[0], y, _day(stmt.end + timedelta(days=30)))
    page.drawString(COLUMNS[1], y, CONTINUED_NOTICE)
    page.drawRightString(COLUMNS[2] + 55, y, money(Decimal("-15.00")))
    _foot(page, 2, 2)
    page.save()
    return buffer.getvalue()


def month_name_rows_statement(*, period: bool = True) -> bytes:
    """:func:`month_name_statement`, rows dated ``Dec 12``; without ``period`` no window."""
    stmt = month_name_statement()
    buffer, page = _open("ledger summary by month name")
    y = _masthead(page)
    if period:
        y = _summary(
            page,
            y,
            last4=stmt.last4,
            start=stmt.start,
            end=stmt.end,
            balances=(("Carried in", stmt.opening), ("Carried out", stmt.closing)),
        )
    else:
        page.drawString(60, y, f"Carried in: {money(stmt.opening)}")
        page.drawString(300, y, f"Carried out: {money(stmt.closing)}")
        y -= 30
    y = _headers(page, y, ("Booked", "Narrative", "Movement", "Kept"))
    kept = stmt.opening
    for row in stmt.rows:
        kept += row.amount
        page.drawString(COLUMNS[0], y, f"{row.day:%b} {row.day.day:02d}")
        page.drawString(COLUMNS[1], y, row.narrative)
        page.drawRightString(COLUMNS[2] + 55, y, money(row.amount))
        page.drawRightString(COLUMNS[3] + 55, y, money(kept))
        y -= 14
    y -= 6
    page.drawString(COLUMNS[0], y, "End of ledger")
    _foot(page, 1, 1)
    page.save()
    return buffer.getvalue()


def card_positive_charges_statement() -> bytes:
    """:func:`card_statement`: charges printed positive, the balances are amounts owed."""
    stmt = card_statement()
    buffer, page = _open("card sheet")
    y = _masthead(page)
    y = _summary(
        page,
        y,
        last4=stmt.last4,
        start=stmt.start,
        end=stmt.end,
        balances=(("Owed before", stmt.owed_before), ("Owed now", stmt.owed_now)),
    )
    y = _headers(page, y, ("Booked", "Narrative", "Charged"))
    for row in stmt.rows:
        page.drawString(COLUMNS[0], y, _day(row.day))
        page.drawString(COLUMNS[1], y, row.narrative)
        page.drawRightString(COLUMNS[2] + 55, y, row.printed)
        y -= 14
    y -= 6
    page.drawString(COLUMNS[0], y, "End of charges")
    _foot(page, 1, 1)
    page.save()
    return buffer.getvalue()

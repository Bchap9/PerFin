# SPDX-License-Identifier: AGPL-3.0-or-later
"""A synthetic card "Statement of Account": a statement date, no period printed.

The layout traits of a charge/corporate card statement, redrawn with an
invented issuer (*Borealis Express*), cardholder, card number, merchants
and amounts; every page says it is a synthetic test document. Nothing in
it is taken from a real statement.

Layout traits:

* **Header** on every page: the issuer's name over ``Statement of
  Account``, a remittance address at the right, then a row of labels
  (``Prepared For | Account Number | Date``) over a row of values (the
  cardholder, the masked number ``XXXX-XXXXX4-62019`` and the **statement
  date** ``January 14, 2025``). No period range is printed anywhere.
  ``Page n of N`` under it.
* **Summary as an equation** on page 1: labels (``Previous Balance``,
  ``Payments and Credits``, ``New Charges`` stacked over three lines,
  ``New Balance $``) over boxed values on one line, with ``-``, ``+`` and
  ``=`` between the boxes: previous - credits + charges = new.
* **Transactions** under ``New Transactions for <NAME>`` with only two
  column headings, ``Foreign Spending`` and ``Amount $`` (no *Date* or
  *Description* heading), and ``Card XXXX-XXXXX4-62019`` under it:

  - dates with a month name and **no year** (``December 13``,
    ``January 9``), the cycle crossing a year boundary (December rows are
    the year before the statement date), a ``*`` after some dates (a
    processing date);
  - a **two-line description**: the merchant line (name, city and region
    in separate cells) and a category line (``Goods or Services``,
    ``INTERNET - ELEC DELIVERY``);
  - an informational **foreign amount** (``360.00`` over ``US Dollar``)
    beside the card-currency amount;
  - **credits** marked ``CR`` after the amount on the same line or alone
    under it on the category line; charges are printed positive.

* The table runs over **continuation pages** (the header block and the
  column headings repeated, ``New Transactions for <NAME> continued``),
  ends with a total line, and a last page holds notes only.
* A **payment stub** at the bottom of page 1, under ``Please detach here``:
  the issuer's name, payment options, the full account number with digits
  (``Account Number 9999-123454-62019``), a ``Payment Due Date | Amount
  Due $ | Amount Paid $`` box whose value row starts with a date and ends
  with an amount (it would read as a row), the cardholder's address, a
  remittance address and an OCR scan line of digits.

:func:`card_statement` builds the default statement, :func:`card_pdf`
draws it and :func:`expected` gives the rows a parser must return in the
ledger convention (a charge negative, a credit positive; the balances owed
negative).
"""

from __future__ import annotations

import io
from calendar import monthrange
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

FONT: Final = "Helvetica"
BOLD: Final = "Helvetica-Bold"
DISCLAIMER: Final = "Synthetic test document - Borealis Express is fictional; no real account."

ISSUER: Final = "Borealis Express"
HOLDER: Final = "JORDAN Q SAMPLE"
MASKED: Final = "XXXX-XXXXX4-62019"
STUB_NUMBER: Final = "9999-123454-62019"
#: The line over the payment stub.
STUB_MARKER: Final = "Please detach here"
LAST4: Final = "2019"


@dataclass(frozen=True)
class CardRow:
    """One printed transaction (the amount as printed: positive)."""

    day: date
    #: The merchant line's cells (name, city, region), printed apart.
    merchant: tuple[str, ...]
    category: str
    amount: Decimal
    credit: bool = False
    #: ``CR`` printed after the amount (else alone on the category line).
    cr_same_line: bool = False
    #: ``(amount, currency name)`` in the informational foreign column.
    foreign: tuple[str, str] | None = None
    #: A ``*`` after the date (a processing date).
    processing: bool = False

    @property
    def payee(self) -> str:
        return " ".join(self.merchant)

    @property
    def ledger(self) -> Decimal:
        """The ledger amount: a charge takes money out of the card account."""
        return self.amount if self.credit else -self.amount


@dataclass(frozen=True)
class CardStatement:
    statement_date: date
    previous_balance: Decimal
    rows: tuple[CardRow, ...]
    holder: str = HOLDER
    masked: str = MASKED
    stub_number: str = STUB_NUMBER
    last4: str = LAST4
    extra_notes: tuple[str, ...] = field(default_factory=tuple)
    #: Printed as ``Previous Statement Date: ...`` under the summary when set.
    previous_statement: date | None = None
    #: Added to both printed totals (credits and charges): the equation still
    #: holds but the totals no longer match the rows.
    summary_shift: Decimal = Decimal(0)

    @property
    def credits(self) -> Decimal:
        return sum((r.amount for r in self.rows if r.credit), Decimal(0))

    @property
    def charges(self) -> Decimal:
        return sum((r.amount for r in self.rows if not r.credit), Decimal(0))

    @property
    def new_balance(self) -> Decimal:
        return self.previous_balance - self.credits + self.charges

    @property
    def opening_balance(self) -> Decimal:
        """The previous balance in the ledger convention (owed: negative)."""
        return -self.previous_balance

    @property
    def closing_balance(self) -> Decimal:
        return -self.new_balance

    @property
    def cycle_start(self) -> date:
        """The day after the same date a month before the statement date."""
        end = self.statement_date
        year, month = (end.year, end.month - 1) if end.month > 1 else (end.year - 1, 12)
        return date(year, month, min(end.day, monthrange(year, month)[1])) + timedelta(days=1)

    @property
    def period_start(self) -> date:
        """After the previous statement; else the cycle's first day or an older row."""
        if self.previous_statement is not None:
            return self.previous_statement + timedelta(days=1)
        return min([self.cycle_start, *(r.day for r in self.rows)])


def _d(year: int, month: int, day: int) -> date:
    return date(year, month, day)


def default_rows() -> tuple[CardRow, ...]:
    """Invented rows of a cycle ending January 14, 2025 (December rows: 2024)."""
    y0, y1 = 2024, 2025
    m = Decimal
    return (
        CardRow(
            _d(y0, 12, 13),
            ("NORTHWIND BOOKS ONLINE", "NWBOOKS.EXAMPLE"),
            "INTERNET - ELEC DELIVERY",
            m("18.45"),
        ),
        CardRow(_d(y0, 12, 16), ("CEDARLINE TELECOM", "877-555-0142"), "Mail Order", m("152.30")),
        CardRow(
            _d(y0, 12, 19),
            ("PIXELFORGE SOFTWARE", "VANCOUVER", "BC"),
            "DIRECT MARKETING-INTERNET",
            m("64.00"),
        ),
        CardRow(
            _d(y0, 12, 23),
            ("HARBOURVIEW CATERING", "HALIFAX", "NS"),
            "Restaurant",
            m("1906.75"),
        ),
        CardRow(
            _d(y0, 12, 27),
            ("INKSTONE STUDIO, LLC", "PORTLAND", "OR"),
            "Goods or Services",
            m("498.12"),
            foreign=("362.00", "US Dollar"),
        ),
        CardRow(
            _d(y0, 12, 30),
            ("NORTHWIND BOOKS ONLINE", "NWBOOKS.EXAMPLE"),
            "INTERNET - ELEC DELIVERY",
            m("18.45"),
            credit=True,
            cr_same_line=True,
        ),
        CardRow(
            _d(y1, 1, 2),
            ("BLUEFERN HOSTING", "BLUEFERN.EXAMPLE"),
            "DIRECT MARKETING-INTERNET",
            m("131.20"),
        ),
        CardRow(
            _d(y1, 1, 2),
            ("SUMMIT PARCEL DEPOT", "OTTAWA", "ON"),
            "Courier Services",
            m("27.88"),
        ),
        CardRow(
            _d(y1, 1, 3),
            ("ORCHARD LANE OFFICE SUPPLY", "KINGSTON", "ON"),
            "Retail",
            m("244.61"),
        ),
        CardRow(
            _d(y1, 1, 4),
            ("ALPENGLOW RAIL", "ZURICH"),
            "Travel - Rail",
            m("212.40"),
            foreign=("131.50", "Swiss Franc"),
        ),
        CardRow(
            _d(y1, 1, 6),
            ("PIXELFORGE SOFTWARE", "VANCOUVER", "BC"),
            "DIRECT MARKETING-INTERNET",
            m("64.00"),
            processing=True,
        ),
        CardRow(
            _d(y1, 1, 6),
            ("TIDEWATER CONFERENCE CTR", "SEATTLE", "WA"),
            "Goods or Services",
            m("289.47"),
            foreign=("201.00", "US Dollar"),
        ),
        CardRow(
            _d(y1, 1, 7),
            ("CEDARLINE TELECOM", "877-555-0142"),
            "Telephone Equipment",
            m("75.00"),
            credit=True,
        ),
        CardRow(
            _d(y1, 1, 8),
            ("MAPLEHURST COFFEE ROASTERS", "GUELPH", "ON"),
            "Restaurant",
            m("42.16"),
        ),
        CardRow(
            _d(y1, 1, 8),
            ("INKSTONE STUDIO, LLC", "PORTLAND", "OR"),
            "Goods or Services",
            m("71.94"),
            foreign=("50.00", "US Dollar"),
        ),
        CardRow(
            _d(y1, 1, 9),
            ("BLUEFERN HOSTING", "BLUEFERN.EXAMPLE"),
            "DIRECT MARKETING-INTERNET",
            m("19.99"),
            processing=True,
        ),
        CardRow(
            _d(y1, 1, 10),
            ("GRANITE PEAK PRINTING", "CALGARY", "AB"),
            "Printing Services",
            m("318.05"),
        ),
        CardRow(
            _d(y1, 1, 10),
            ("SUMMIT PARCEL DEPOT", "OTTAWA", "ON"),
            "Courier Services",
            m("16.40"),
        ),
        CardRow(
            _d(y1, 1, 11),
            ("LANTERN BAY HOTEL", "VICTORIA", "BC"),
            "Lodging",
            m("426.90"),
        ),
        CardRow(
            _d(y1, 1, 11),
            ("LANTERN BAY HOTEL", "VICTORIA", "BC"),
            "Lodging",
            m("60.00"),
            credit=True,
            cr_same_line=True,
        ),
        CardRow(
            _d(y1, 1, 12),
            ("ORCHARD LANE OFFICE SUPPLY", "KINGSTON", "ON"),
            "Retail",
            m("88.13"),
        ),
        CardRow(
            _d(y1, 1, 13),
            ("NORTHWIND BOOKS ONLINE", "NWBOOKS.EXAMPLE"),
            "INTERNET - ELEC DELIVERY",
            m("33.60"),
        ),
        CardRow(
            _d(y1, 1, 13),
            ("FERNWOOD AV RENTALS", "TORONTO", "ON"),
            "Equipment Rental",
            m("540.00"),
        ),
        CardRow(
            _d(y1, 1, 14),
            ("MAPLEHURST COFFEE ROASTERS", "GUELPH", "ON"),
            "Restaurant",
            m("23.75"),
        ),
    )


def card_statement(*, rows: Sequence[CardRow] | None = None) -> CardStatement:
    """The default statement: dated January 14, 2025, the cycle from December."""
    return CardStatement(
        statement_date=date(2025, 1, 14),
        previous_balance=Decimal("7412.06"),
        rows=tuple(rows) if rows is not None else default_rows(),
    )


def later_rows(stmt: CardStatement) -> CardStatement:
    """``stmt`` without the rows dated before its cycle (the period is the cycle)."""
    return replace(stmt, rows=tuple(r for r in stmt.rows if r.day >= stmt.cycle_start))


def expected(stmt: CardStatement) -> list[tuple[date, Decimal, str]]:
    """``(date, ledger amount, payee)`` of every row, sorted."""
    return sorted((r.day, r.ledger, r.payee) for r in stmt.rows)


def money(value: Decimal) -> str:
    return f"{value:,.2f}"


def long_date(day: date) -> str:
    return f"{day.strftime('%B')} {day.day}, {day.year}"


def row_date(day: date) -> str:
    return f"{day.strftime('%B')} {day.day}"


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

LEFT: Final = 62.0
DESC: Final = 150.0
FOREIGN_RIGHT: Final = 500.0
AMOUNT_RIGHT: Final = 560.0


def _header(c: canvas.Canvas, stmt: CardStatement, page: int, pages: int) -> float:
    """The block every page starts with; returns the y under it."""
    c.setFont(BOLD, 13)
    c.drawString(LEFT, 752, f"{ISSUER} Corporate Card")
    c.drawString(LEFT, 736, "Statement of Account")
    c.setFont(FONT, 7)
    for i, text in enumerate(
        (
            "www.borealis-express.example",
            "Borealis Bank (Test)",
            "Corporate Service Centre",
            "PO Box 4100 Station Q",
            "Northbury (Ontario) A1A 1A1",
        )
    ):
        c.drawRightString(AMOUNT_RIGHT, 756 - i * 9, text)
    c.setFont(FONT, 7)
    c.drawString(LEFT, 704, "Prepared For")
    c.drawRightString(330, 704, "Account Number")
    c.drawRightString(470, 704, "Date")
    c.setFont(FONT, 11)
    c.drawString(LEFT, 690, stmt.holder)
    c.drawString(240, 690, stmt.masked)
    c.drawString(400, 690, long_date(stmt.statement_date))
    c.setFont(FONT, 8)
    c.drawRightString(AMOUNT_RIGHT, 668, f"Page {page} of {pages}")
    return 660


def _summary(c: canvas.Canvas, stmt: CardStatement, top: float) -> float:
    """The boxed equation previous - credits + charges = new; returns the y under it."""
    c.setFont(FONT, 6.5)
    c.drawCentredString(285, top, "New Charges")
    c.drawCentredString(285, top - 8, "Including Delinquency")
    labels_y = top - 16
    c.drawCentredString(105, labels_y, "Previous Balance")
    c.drawCentredString(190, labels_y, "Payments and Credits")
    c.drawCentredString(285, labels_y, "Assessment, if any")
    c.setFont(BOLD, 6.5)
    c.drawCentredString(380, labels_y, "New Balance $")
    boxes = (
        (70, 140, stmt.previous_balance),
        (155, 225, stmt.credits + stmt.summary_shift),
        (245, 325, stmt.charges + stmt.summary_shift),
        (340, 420, stmt.new_balance),
    )
    y = labels_y - 18
    for x0, x1, value in boxes:
        c.rect(x0, y - 4, x1 - x0, 15)
        c.setFont(BOLD if x0 == 340 else FONT, 9)
        c.drawRightString(x1 - 4, y, money(value))
    c.setFont(FONT, 9)
    for x, sign in ((147.5, "-"), (235, "+"), (332.5, "=")):
        c.drawCentredString(x, y, sign)
    c.setFont(FONT, 6.5)
    c.drawString(
        LEFT,
        y - 20,
        f"Statement includes payments and charges received by {long_date(stmt.statement_date)}",
    )
    c.drawString(LEFT, y - 32, 'Please see "About Your Statement" for important information.')
    c.drawString(LEFT, y - 44, "* indicates processing date where purchase date not available")
    if stmt.previous_statement is not None:
        c.drawRightString(
            AMOUNT_RIGHT, y - 44, f"Previous Statement Date: {long_date(stmt.previous_statement)}"
        )
    return y - 64


def _table_header(c: canvas.Canvas, stmt: CardStatement, y: float, continued: bool) -> float:
    c.setFont(BOLD, 10)
    heading = f"New Transactions for {stmt.holder}" + (" continued" if continued else "")
    c.drawString(LEFT, y, heading)
    c.setFont(FONT, 7)
    c.drawRightString(FOREIGN_RIGHT, y, "Foreign Spending")
    c.drawRightString(AMOUNT_RIGHT, y, "Amount $")
    c.setFont(FONT, 6)
    c.drawString(LEFT, y - 9, f"Card {stmt.masked}")
    c.line(LEFT, y - 13, AMOUNT_RIGHT, y - 13)
    return y - 28


def _row(c: canvas.Canvas, row: CardRow, y: float) -> float:
    c.setFont(BOLD, 9)
    c.drawString(LEFT, y, row_date(row.day) + (" *" if row.processing else ""))
    x = DESC
    for i, cell in enumerate(row.merchant):
        c.drawString(x, y, cell)
        x += c.stringWidth(cell, BOLD, 9) + (22 if i == 0 else 16)
    printed = money(row.amount) + (" CR" if row.credit and row.cr_same_line else "")
    c.drawRightString(AMOUNT_RIGHT, y, printed)
    c.setFont(FONT, 8)
    if row.foreign is not None:
        c.drawRightString(FOREIGN_RIGHT, y, row.foreign[0])
    c.setFont(FONT, 7)
    c.drawString(DESC, y - 9, row.category)
    if row.foreign is not None:
        c.setFont(FONT, 6)
        c.drawRightString(FOREIGN_RIGHT, y - 9, row.foreign[1])
    if row.credit and not row.cr_same_line:
        c.setFont(FONT, 8)
        c.drawRightString(AMOUNT_RIGHT, y - 9, "CR")
    c.line(LEFT, y - 13, AMOUNT_RIGHT, y - 13)
    return y - 25


def _stub(c: canvas.Canvas, stmt: CardStatement, marker: str = STUB_MARKER) -> None:
    """The payment stub at the bottom of page 1 (never transactions)."""
    c.setFont(FONT, 7)
    c.drawCentredString(306, 262, marker)
    c.setFont(BOLD, 15)
    c.drawString(LEFT, 238, ISSUER.upper())
    c.setFont(FONT, 6.5)
    for i, text in enumerate(
        (
            "Payment Options",
            "PLEASE ALLOW 3 TO 5 BUSINESS DAYS FOR YOUR PAYMENT TO BE PROCESSED.",
            "Pay online at www.borealis-express.example/pay or by mail to the address below.",
        )
    ):
        c.drawString(LEFT, 226 - i * 8, text)
    c.rect(330, 160, 230, 80)
    c.setFont(FONT, 8)
    c.drawString(336, 228, f"Account Number {stmt.stub_number}")
    c.setFont(FONT, 7)
    c.drawString(LEFT, 196, "Payment Due Date")
    c.drawString(440, 196, "Amount Due $")
    c.drawRightString(AMOUNT_RIGHT - 4, 196, "Amount Paid $")
    c.setFont(FONT, 9)
    due = stmt.statement_date + timedelta(days=25)
    c.drawString(LEFT, 182, long_date(due))
    c.drawRightString(AMOUNT_RIGHT, 182, money(stmt.new_balance))
    c.setFont(FONT, 9)
    for i, text in enumerate((stmt.holder, "200 EXAMPLE CREEK WAY", "NORTHBURY ON", "A1A 1A1")):
        c.drawString(90, 140 - i * 10, text)
    for i, text in enumerate(("Borealis Bank (Test)", "PO BOX 4200", "Eastfield ON B2B 2B2")):
        c.drawString(400, 130 - i * 10, text)
    c.setFont("Courier", 11)
    c.drawString(
        LEFT, 70, "00009999123454620190000" + f"{stmt.new_balance:.2f}".replace(".", "") + " 7"
    )


def _notes(c: canvas.Canvas, stmt: CardStatement, y: float) -> None:
    c.setFont(BOLD, 10)
    c.drawString(LEFT, y, "About Your Statement")
    c.setFont(FONT, 8)
    lines = (
        "Your account is payable in full each month. Charges in a foreign currency are",
        "converted to your billing currency on the date they are processed.",
        "The foreign spending column shows the amount charged by the merchant.",
        "Questions about this statement? Call the number on the back of your card.",
        *stmt.extra_notes,
    )
    for i, text in enumerate(lines):
        c.drawString(LEFT, y - 16 - i * 11, text)


def card_pdf(
    stmt: CardStatement,
    *,
    first_page_rows: int = 8,
    rows_per_page: int = 12,
    stub: bool = True,
    stub_marker: str = STUB_MARKER,
) -> bytes:
    """``stmt`` drawn over continuation pages, the stub on page 1, notes last.

    ``stub_marker`` is the line over the stub (other wordings, a perforation).
    """
    rows = list(stmt.rows)
    chunks = [rows[:first_page_rows]]
    rest = rows[first_page_rows:]
    chunks.extend(rest[i : i + rows_per_page] for i in range(0, len(rest), rows_per_page))
    pages = len(chunks) + 1
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle("Statement of Account")
    c.setAuthor(ISSUER)
    for number, chunk in enumerate(chunks, start=1):
        y = _header(c, stmt, number, pages)
        if number == 1:
            y = _summary(c, stmt, y - 4)
        y = _table_header(c, stmt, y, continued=number > 1)
        for row in chunk:
            y = _row(c, row, y)
        if number == len(chunks):
            c.setFont(BOLD, 9)
            c.drawString(DESC, y, f"Total of New Transactions for {stmt.holder}")
            c.drawRightString(AMOUNT_RIGHT, y, money(stmt.charges - stmt.credits))
        if number == 1 and stub:
            _stub(c, stmt, stub_marker)
        c.setFont(FONT, 6)
        c.drawString(LEFT, 40, DISCLAIMER)
        c.showPage()
    y = _header(c, stmt, pages, pages)
    _notes(c, stmt, y - 10)
    c.setFont(FONT, 6)
    c.drawString(LEFT, 40, DISCLAIMER)
    c.showPage()
    c.save()
    return buf.getvalue()

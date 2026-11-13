# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic loan and mortgage statements (reportlab PDFs) for the loan statement parser.

The fictional lender *Placeholder Home Lending* (invented; not a real
company) sends a monthly statement for each payment of a schedule computed
by the real amortisation engine (``app.domain.loans.amortization``): the
payment's date and split (principal, interest, escrow, fees), the total
received, the previous and outstanding principal balance, the interest rate
and the next payment due. Loan numbers are invented and printed masked
(``XXXXXX4821``). Nothing here is real data.

* :func:`loan_series` builds consecutive :class:`SynthLoanStatement` objects
  (``extra`` principal on chosen months, a late fee on another);
* :func:`loan_statement_pdf` draws one (``layout="classic"`` labels the
  payment ``Payment received``; ``"compact"`` uses ``Payment date`` and
  ``New principal balance``, the lender named on a ``Lender:`` line);
* ``tamper="parts"`` prints a total that is not the sum of the parts,
  ``tamper="balance"`` a new balance the principal does not explain: neither
  reconciles (SECURITY.md C12).
* :func:`loan_guide_pdf` draws a guide of many pages about mortgage
  statements with one such statement inside it as a sample: a document a
  loan statement parser must not take for the reader's own statement.
* :func:`periodic_statement_pdf` draws a :class:`PeriodicStatement`: the
  periodic statement a mortgage servicer sends each month. It prints three
  sets of figures that must not be confused: the split of the **next**
  payment (*Explanation of Amount Due*), what was paid **last month** and
  what was paid in the **year to date** (*Past Payments Breakdown*, two
  columns), with the payment itself as a row of *Transaction Activity* and
  a coupon to detach at the foot. Its loan number is printed in full
  (invented; only its last four digits may leave a parser).
* :func:`auto_loan_bill_pdf` draws a two-page bill of the fictional
  *Placeholder Auto Finance*: no words ``loan statement`` anywhere, the
  principal balance printed **above** its label, the payment as a bare row
  (date, ``Payment Received. Thank you!``, amount) and its split on page 2
  under ``PAYMENT ALLOCATION``.
* :func:`student_loan_bill_pdf` draws a one-page bill that lists two loans
  in one table (``Loan Seq | Program | Int Rate | Balance | ...``): a bill
  of several loans, which the parser must not read as one.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, Literal

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.domain.loans.amortization import Terms, add_months, schedule

LENDER: Final = "Placeholder Home Lending"
LAST4: Final = "4821"
FONT: Final = "Helvetica"
BOLD: Final = "Helvetica-Bold"

Layout = Literal["classic", "compact"]
Tamper = Literal["parts", "balance"] | None

TERMS: Final = Terms(
    principal=Decimal("250000.00"),
    annual_rate=Decimal("0.06125"),
    term_months=360,
    origination_date=date(2024, 3, 1),
    payment_day=1,
    currency="USD",
    escrow=Decimal("350.00"),
)


@dataclass(frozen=True)
class SynthLoanStatement:
    number: int
    statement_date: date
    paid_on: date
    principal: Decimal
    interest: Decimal
    escrow: Decimal
    fees: Decimal
    previous_balance: Decimal
    balance: Decimal
    rate: Decimal
    next_due: date
    next_amount: Decimal
    last4: str = LAST4

    @property
    def total(self) -> Decimal:
        return self.principal + self.interest + self.escrow + self.fees


def loan_series(
    months: int = 3,
    *,
    terms: Terms = TERMS,
    extra: dict[int, Decimal] | None = None,
    late_fee_month: int | None = None,
) -> list[SynthLoanStatement]:
    """The first ``months`` statements of ``terms`` (payments made on the due date).

    ``extra`` maps a payment number to extra principal paid with it (the
    following interest is computed on the lower balance); a late fee of
    35.00 is added to payment ``late_fee_month`` (paid five days late).
    """
    extra = extra or {}
    plan = schedule(terms)
    balance = terms.principal
    out: list[SynthLoanStatement] = []
    payment = plan.rows[0].payment
    for number in range(1, months + 1):
        due = add_months(terms.origination_date, number, terms.payment_day)
        interest = (balance * terms.annual_rate / 12).quantize(Decimal("0.01"))
        principal = payment - interest + extra.get(number, Decimal(0))
        late = number == late_fee_month
        paid_on = due + timedelta(days=5 if late else 0)
        new_balance = balance - principal
        out.append(
            SynthLoanStatement(
                number=number,
                statement_date=due + timedelta(days=14),
                paid_on=paid_on,
                principal=principal,
                interest=interest,
                escrow=terms.escrow,
                fees=Decimal("35.00") if late else Decimal(0),
                previous_balance=balance,
                balance=new_balance,
                rate=terms.annual_rate,
                next_due=add_months(terms.origination_date, number + 1, terms.payment_day),
                next_amount=payment + terms.escrow,
            )
        )
        balance = new_balance
    return out


def _money(value: Decimal, symbol: str = "") -> str:
    return f"{symbol}{value:,.2f}"


def _us(day: date) -> str:
    return f"{day.month:02d}/{day.day:02d}/{day.year}"


def loan_statement_pdf(
    stmt: SynthLoanStatement, *, layout: Layout = "classic", tamper: Tamper = None
) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _draw_statement(c, stmt, layout, tamper)
    c.save()
    return buf.getvalue()


def _draw_statement(
    c: canvas.Canvas, stmt: SynthLoanStatement, layout: Layout, tamper: Tamper
) -> None:
    """One statement on a page of its own."""
    total = stmt.total + (Decimal("100.00") if tamper == "parts" else 0)
    balance = stmt.balance - (Decimal("500.00") if tamper == "balance" else 0)
    left, right = 72, 520
    y = 750

    def row(label: str, value: str, *, bold: bool = False) -> None:
        nonlocal y
        c.setFont(BOLD if bold else FONT, 10)
        c.drawString(left, y, label)
        c.drawRightString(right, y, value)
        y -= 18

    c.setFont(BOLD, 14)
    if layout == "classic":
        c.drawString(left, y, LENDER)
        y -= 20
        c.setFont(BOLD, 12)
        c.drawString(left, y, "Mortgage Statement")
    else:
        c.drawString(left, y, "Home Loan Statement")
        y -= 20
        c.setFont(FONT, 10)
        c.drawString(left, y, f"Lender: {LENDER}")
    y -= 24
    c.setFont(FONT, 10)
    c.drawString(left, y, f"Loan number XXXXXX{stmt.last4}")
    y -= 18
    c.drawString(left, y, f"Statement Date {_us(stmt.statement_date)}")
    y -= 30
    c.setFont(BOLD, 11)
    c.drawString(left, y, "Payment Summary")
    y -= 20
    label = "Payment received" if layout == "classic" else "Payment date"
    row(label, _us(stmt.paid_on))
    row("Principal", _money(stmt.principal))
    row("Interest", _money(stmt.interest))
    row("Escrow (taxes and insurance)", _money(stmt.escrow))
    row("Fees and charges", _money(stmt.fees))
    row("Total payment received", _money(total, "$"), bold=True)
    y -= 12
    c.setFont(BOLD, 11)
    c.drawString(left, y, "Loan Summary")
    y -= 20
    row("Interest rate", f"{stmt.rate * 100:.3f}%")
    row("Previous principal balance", _money(stmt.previous_balance))
    new_label = "Outstanding principal balance" if layout == "classic" else "New principal balance"
    row(new_label, _money(balance))
    row("Escrow balance", _money(Decimal("1234.56")))
    row("Principal paid year to date", _money(stmt.principal * stmt.number))
    y -= 12
    row("Next payment due", _us(stmt.next_due))
    row("Amount due", _money(stmt.next_amount, "$"))
    c.setFont(FONT, 8)
    c.drawString(left, 60, "Synthetic test document. Placeholder Home Lending is fictional.")
    c.showPage()


#: The prose of a guide page (:func:`loan_guide_pdf`); none of it is a labelled figure.
_GUIDE_LINES: Final = (
    "This guide explains each part of the statement a servicer sends every month.",
    "The first part says what was paid and how the payment was divided.",
    "The second part says what is still owed and at which rate.",
    "A sample is printed on one of the pages that follow. Its figures are examples.",
    "Compare the sample with your own statement, line by line.",
)


def loan_guide_pdf(stmt: SynthLoanStatement, *, pages: int = 12, sample_page: int = 5) -> bytes:
    """A guide of ``pages`` pages about mortgage statements, one of them a sample.

    Page 1 is headed ``Your Mortgage Statement Explained``; page
    ``sample_page`` is ``stmt`` exactly as :func:`loan_statement_pdf` draws
    it (it reconciles); every other page is prose. A document about
    statements, not a statement of the reader's loan.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for number in range(1, pages + 1):
        if number == sample_page:
            _draw_statement(c, stmt, "classic", None)
            continue
        c.setFont(BOLD, 14)
        heading = "Your Mortgage Statement Explained" if number == 1 else f"Chapter {number}"
        c.drawString(72, 750, heading)
        c.setFont(FONT, 10)
        y = 720
        for text in _GUIDE_LINES:
            c.drawString(72, y, text)
            y -= 16
        c.setFont(FONT, 8)
        c.drawString(72, 60, f"Synthetic test document. Page {number} of {pages}.")
        c.showPage()
    c.save()
    return buf.getvalue()


def with_last4(stmt: SynthLoanStatement, last4: str) -> SynthLoanStatement:
    return replace(stmt, last4=last4)


PeriodicVariant = Literal["blocks", "labels_over_values", "payment_date_is_due_date", "no_payment"]
PeriodicTamper = Literal["paid", "due"] | None

#: Printed in full on the periodic statement (invented).
PERIODIC_LOAN_NUMBER: Final = "0004417755"
PERIODIC_LAST4: Final = "7755"


@dataclass(frozen=True)
class ActivityRow:
    """One row of *Transaction Activity*: a payment received or a charge."""

    day: date
    description: str
    amount: Decimal
    kind: Literal["payment", "charge"] = "payment"


@dataclass(frozen=True)
class PeriodicStatement:
    """A periodic mortgage statement (module doc); every figure is invented.

    The next payment adds up (412.08 + 760.17 + 301.50 = 1,473.75, plus the
    25.00 late charge = 1,498.75) and so does last month's (410.37 + 761.88
    + 301.50 = 1,473.75, received on 2026-03-02).
    """

    statement_date: date = date(2026, 3, 20)
    due_date: date = date(2026, 4, 1)
    since: date = date(2026, 2, 20)
    rate: Decimal = Decimal("0.05")
    balance: Decimal = Decimal("182440.17")
    due_principal: Decimal = Decimal("412.08")
    due_interest: Decimal = Decimal("760.17")
    due_escrow: Decimal = Decimal("301.50")
    due_regular: Decimal = Decimal("1473.75")
    due_fees: Decimal = Decimal("25.00")
    due_total: Decimal = Decimal("1498.75")
    paid_principal: Decimal = Decimal("410.37")
    paid_interest: Decimal = Decimal("761.88")
    paid_escrow: Decimal = Decimal("301.50")
    paid_fees: Decimal = Decimal("0.00")
    paid_total: Decimal = Decimal("1473.75")
    ytd_principal: Decimal = Decimal("2444.91")
    ytd_interest: Decimal = Decimal("4588.59")
    ytd_escrow: Decimal = Decimal("1809.00")
    ytd_fees: Decimal = Decimal("25.00")
    ytd_total: Decimal = Decimal("8867.50")
    activity: tuple[ActivityRow, ...] = (
        ActivityRow(date(2026, 3, 2), "Payment received, thank you", Decimal("1473.75")),
        ActivityRow(date(2026, 3, 17), "Late charge", Decimal("25.00"), "charge"),
    )
    loan_number: str = PERIODIC_LOAN_NUMBER
    #: The amount the coupon repeats (None: the total amount due).
    coupon_amount: Decimal | None = None


#: The statement :func:`periodic_statement_pdf` draws when given none.
DEFAULT_PERIODIC: Final = PeriodicStatement()


def periodic_statement_pdf(
    stmt: PeriodicStatement = DEFAULT_PERIODIC,
    *,
    variant: PeriodicVariant = "blocks",
    tamper: PeriodicTamper = None,
) -> bytes:
    """One periodic statement (module doc).

    ``blocks``: *Account Information* and *Explanation of Amount Due* side
    by side, then *Past Payments Breakdown* (``Paid Last Month | Paid Year
    to Date``) and *Transaction Activity* (``Date | Description | Charges |
    Payments``). ``labels_over_values``: the dates, the loan number, the
    balance and the rate under their labels, the paid columns headed ``PAID
    SINCE <date> | PAID YEAR TO DATE`` with no heading, and a ``LENDER PAID
    EXPENSES`` line. ``payment_date_is_due_date``: the due date labelled
    ``Payment Date``. ``no_payment``: nothing was paid last month.
    ``tamper``: 100.00 added to last month's total (``paid``) or to the
    total amount due (``due``).
    """
    zero = Decimal("0.00")
    paid = (
        (zero, zero, zero, zero, zero)
        if variant == "no_payment"
        else (
            stmt.paid_principal,
            stmt.paid_interest,
            stmt.paid_escrow,
            stmt.paid_fees,
            stmt.paid_total,
        )
    )
    paid_total = paid[4] + (Decimal("100.00") if tamper == "paid" else 0)
    due_total = stmt.due_total + (Decimal("100.00") if tamper == "due" else 0)
    year = (stmt.ytd_principal, stmt.ytd_interest, stmt.ytd_escrow, stmt.ytd_fees, stmt.ytd_total)
    rows = tuple(
        row for row in stmt.activity if not (variant == "no_payment" and row.kind == "payment")
    )
    upper = variant == "labels_over_values"
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    left = 72
    y = 750.0

    def text(x: float, label: str, *, bold: bool = False, size: int = 10) -> None:
        c.setFont(BOLD if bold else FONT, size)
        c.drawString(x, y, label.upper() if upper else label)

    def right(x: float, label: str, *, bold: bool = False) -> None:
        c.setFont(BOLD if bold else FONT, 10)
        c.drawRightString(x, y, label.upper() if upper else label)

    c.setFont(BOLD, 14)
    c.drawString(left, y, LENDER)
    y -= 20
    c.setFont(BOLD, 12)
    c.drawString(left, y, "Mortgage Statement")
    y -= 26
    due_parts = (
        ("Principal", stmt.due_principal),
        ("Interest", stmt.due_interest),
        ("Escrow (Taxes and Insurance)", stmt.due_escrow),
        ("Regular Monthly Payment", stmt.due_regular),
        ("Fees & Charges" if upper else "Total Fees Charged", stmt.due_fees),
        ("Total Amount Due", due_total),
    )
    if upper:
        for x, label in ((left, "Loan Number"), (220, "Statement Date"), (360, "Payment Due Date")):
            text(x, label, bold=True, size=9)
        y -= 14
        for x, value in (
            (left, stmt.loan_number),
            (220, _us(stmt.statement_date)),
            (360, _us(stmt.due_date)),
        ):
            text(x, value)
        y -= 26
        text(left, "Principal Balance", bold=True, size=9)
        text(220, "Interest Rate", bold=True, size=9)
        y -= 14
        text(left, _money(stmt.balance, "$"))
        text(220, f"{stmt.rate * 100:.3f}%")
        y -= 30
        text(left, "Explanation of Amount Due", bold=True, size=11)
        y -= 18
        for part, figure in due_parts:
            text(left, part)
            right(300, _money(figure, "$"))
            y -= 16
        y -= 14
        c.setFont(BOLD, 9)
        c.drawRightString(400, y, f"PAID SINCE {_us(stmt.since)}")
        c.drawRightString(540, y, "PAID YEAR TO DATE")
        y -= 16
        table = (
            ("Principal", paid[0], year[0]),
            ("Interest", paid[1], year[1]),
            ("Escrow (Taxes and Insurance)", paid[2], year[2]),
            ("Fees & Charges", paid[3], year[3]),
            ("Lender Paid Expenses", zero, zero),
            ("Total", paid_total, year[4]),
        )
        for label, last, to_date in table:
            text(left, label)
            right(400, _money(last, "$"))
            right(540, _money(to_date, "$"))
            y -= 16
    else:
        text(left, f"Loan Number {stmt.loan_number}")
        y -= 16
        text(left, f"Statement Date {_us(stmt.statement_date)}")
        y -= 30
        text(left, "Account Information", bold=True, size=11)
        text(320, "Explanation of Amount Due", bold=True, size=11)
        y -= 18
        due_label = "Payment Date" if variant == "payment_date_is_due_date" else "Payment Due Date"
        account = (
            ("Outstanding Principal", _money(stmt.balance, "$")),
            ("Interest Rate", f"{stmt.rate * 100:.3f}%"),
            (due_label, _us(stmt.due_date)),
        )
        for at, (part, figure) in enumerate(due_parts):
            if at < len(account):
                text(left, account[at][0])
                right(290, account[at][1])
            text(320, part)
            right(540, _money(figure, "$"))
            y -= 16
        y -= 16
        text(left, "Past Payments Breakdown", bold=True, size=11)
        y -= 18
        right(340, "Paid Last Month", bold=True)
        right(480, "Paid Year to Date", bold=True)
        y -= 16
        for label, last, to_date in (
            ("Principal", paid[0], year[0]),
            ("Interest", paid[1], year[1]),
            ("Escrow (Taxes and Insurance)", paid[2], year[2]),
            ("Fees", paid[3], year[3]),
            ("Total", paid_total, year[4]),
        ):
            text(left, label)
            right(340, _money(last, "$"))
            right(480, _money(to_date, "$"))
            y -= 16
    y -= 16
    text(left, "Transaction Activity", bold=True, size=11)
    y -= 18
    text(left, "Date", bold=True)
    text(150, "Description", bold=True)
    right(430, "Charges", bold=True)
    right(530, "Payments", bold=True)
    y -= 16
    for row in rows:
        c.setFont(FONT, 10)
        c.drawString(left, y, _us(row.day))
        c.drawString(150, y, row.description)
        c.drawRightString(430 if row.kind == "charge" else 530, y, _money(row.amount))
        y -= 16
    y = 190
    c.setFont(FONT, 9)
    c.drawString(left, y, "Please detach and return this portion with your payment")
    y -= 18
    c.setFont(FONT, 10)
    c.drawString(left, y, f"Loan Number {stmt.loan_number}")
    c.drawString(320, y, "Amount Due")
    coupon = due_total if stmt.coupon_amount is None else stmt.coupon_amount
    c.drawRightString(540, y, _money(coupon, "$"))
    y -= 16
    c.drawString(320, y, f"Due Date {_us(stmt.due_date)}")
    c.setFont(FONT, 8)
    c.drawString(left, 60, "Synthetic test document. Placeholder Home Lending is fictional.")
    c.showPage()
    c.save()
    return buf.getvalue()


AUTO_LENDER: Final = "Placeholder Auto Finance"
#: Printed in full on the auto-loan bill (invented).
AUTO_ACCOUNT_NUMBER: Final = "300044171234"
AUTO_LAST4: Final = "1234"
STUDENT_LENDER: Final = "Placeholder Education Lending"


def _footer(c: canvas.Canvas, lender: str) -> None:
    c.setFont(FONT, 8)
    c.drawString(72, 60, f"Synthetic test document. {lender} is fictional.")


def auto_loan_bill_pdf() -> bytes:
    """A two-page auto-loan bill (module doc); every figure is invented.

    The payment adds up (371.85 + 83.35 = 455.20) and is the row of
    2026-03-14; the amount due next is the same 455.20, on 2026-04-15.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    left = 72
    c.setFont(BOLD, 14)
    c.drawString(left, 750, AUTO_LENDER)
    c.setFont(BOLD, 12)
    c.drawString(left, 730, "Account Statement")
    c.setFont(FONT, 10)
    c.drawString(left, 704, f"Account number {AUTO_ACCOUNT_NUMBER}")
    c.setFont(BOLD, 12)
    c.drawString(left, 664, "$18,250.40")
    c.setFont(FONT, 9)
    c.drawString(left, 650, "CURRENT PRINCIPAL BALANCE")
    c.setFont(FONT, 10)
    c.drawString(left, 610, "Payment Due Date")
    c.drawString(260, 610, "04/15/2026")
    c.drawString(left, 594, "Total Amount Due")
    c.drawString(260, 594, "$455.20")
    c.drawString(left, 550, "03/14/2026")
    c.drawString(160, 550, "Payment Received. Thank you!")
    c.drawRightString(520, 550, "$455.20")
    _footer(c, AUTO_LENDER)
    c.showPage()
    c.setFont(BOLD, 11)
    c.drawString(left, 750, "PAYMENT ALLOCATION")
    c.setFont(FONT, 10)
    y = 730
    for label, value in (("Principal", "$371.85"), ("Interest", "$83.35"), ("Total", "$455.20")):
        c.drawString(left, y, label)
        c.drawRightString(300, y, value)
        y -= 16
    _footer(c, AUTO_LENDER)
    c.showPage()
    c.save()
    return buf.getvalue()


def student_loan_bill_pdf() -> bytes:
    """A one-page student-loan bill listing two loans (module doc); invented figures."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    left = 72
    c.setFont(BOLD, 14)
    c.drawString(left, 750, STUDENT_LENDER)
    c.setFont(BOLD, 12)
    c.drawString(left, 730, "Installment Bill")
    columns = (left, 232, 392)
    c.setFont(BOLD, 8)
    for x, label in zip(
        columns,
        (
            "Date Last Payment Received",
            "Principal Paid Since Last Statement",
            "Interest Paid Since Last Statement",
        ),
        strict=True,
    ):
        c.drawString(x, 690, label)
    c.setFont(FONT, 10)
    for x, value in zip(columns, ("02/27/2026", "$61.75", "$40.25"), strict=True):
        c.drawString(x, 676, value)
    table = (72, 130, 250, 320, 400, 480)
    c.setFont(BOLD, 8)
    for x, label in zip(
        table,
        ("Loan Seq", "Program", "Int Rate", "Balance", "Amount Past Due", "Current Amount Due"),
        strict=True,
    ):
        c.drawString(x, 630, label)
    c.setFont(FONT, 10)
    y = 614
    for row in (
        ("1", "Subsidized", "4.250%", "$6,200.00", "$0.00", "$64.10"),
        ("2", "Unsubsidized", "5.500%", "$9,150.00", "$0.00", "$98.40"),
    ):
        for x, value in zip(table, row, strict=True):
            c.drawString(x, y, value)
        y -= 16
    _footer(c, STUDENT_LENDER)
    c.showPage()
    c.save()
    return buf.getvalue()

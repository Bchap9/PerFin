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
    total = stmt.total + (Decimal("100.00") if tamper == "parts" else 0)
    balance = stmt.balance - (Decimal("500.00") if tamper == "balance" else 0)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
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
    c.save()
    return buf.getvalue()


def with_last4(stmt: SynthLoanStatement, last4: str) -> SynthLoanStatement:
    return replace(stmt, last4=last4)

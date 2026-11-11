# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic PDF statements and payslips for the document parsers (reportlab).

The four fictional statement institutions of the shipped classifier
fingerprints (``app/ingestion/classify/fingerprints/*.yaml``, text layouts in
``tests/fixtures/synth_classify.py``) each get a PDF layout drawn from a
:class:`SynthStatement` (invented merchants, amounts and account numbers;
nothing real). Every layout satisfies its fingerprint, so a PDF is
classified and then read by that institution's parser
(``app/ingestion/parsers/institutions.py``):

* Sample National Bank checking (``sample-national-bank-checking``):
  ``MM/DD/YYYY``, *Posting Date / Description / Amount / Balance* (one signed
  amount and a running balance), "Beginning/Ending balance on <date>", the
  table continued over several pages (header repeated, page footers),
  wrapped description lines, a *pending* section outside the balances,
  service fee and interest rows.
* Example Federal Credit Union (``example-fcu-share-statement``): a ruled
  table, *Withdrawals*/*Deposits* columns, previous/new balance, a dividend.
* Demo Card Services (``demo-card-services-card``): ``MM/DD`` rows without a
  year (the billing period gives it), transaction and posting dates,
  sections (payments and credits, purchases, fees, interest, pending
  authorisations), charges printed positive and credits with ``CR``,
  previous/new balance as amounts owed.
* Mock Savings and Loan (``mock-savings-and-loan-statement``, GBP):
  ``DD/MM/YYYY``, *Paid out*/*Paid in*, balance brought/carried forward.
* :func:`generic_pdf`: a bank no fingerprint knows (Riverbend Test Bank),
  *Debit*/*Credit* columns, ``DD/MM/YYYY`` dates; a 12-day statement has
  every day <= 12, so the date order cannot be told from the values.
* :func:`payslip_pdf`: an invented employer's payslip.

``tamper="extra_row"`` inserts a transaction the balances do not account for
(the data-poisoning case of SECURITY.md C12) and ``tamper="closing"`` prints
a wrong closing balance: neither statement reconciles. :func:`expected`
gives the rows a parser must return.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, Literal

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from tests.fixtures.synth_statements import (
    FakeInstitution,
    SynthStatement,
    SynthTxn,
    statement,
    synth_ledger,
)

SAMPLE_NATIONAL: Final = FakeInstitution(
    "sample-national-bank-checking", "Sample National Bank", "9101"
)
EXAMPLE_FCU: Final = FakeInstitution(
    "example-fcu-share-statement", "Example Federal Credit Union", "9102"
)
DEMO_CARD: Final = FakeInstitution("demo-card-services-card", "Demo Card Services", "9103")
MOCK_SAVINGS: Final = FakeInstitution(
    "mock-savings-and-loan-statement", "Mock Savings and Loan", "9104"
)
GENERIC: Final = FakeInstitution("riverbend", "Riverbend Test Bank", "9199")

Tamper = Literal["extra_row", "closing"] | None
#: The row a ``tamper="extra_row"`` statement slips in.
FORGED: Final = SynthTxn(date(2026, 1, 18), Decimal("-777.77"), "Forged Payee", "", "FORGED")

FONT = "Helvetica"
BOLD = "Helvetica-Bold"
START: Final = date(2026, 1, 1)


@dataclass(frozen=True)
class PendingItem:
    posted: date
    amount: Decimal
    payee: str


def money(value: Decimal, *, symbol: str = "", plus: bool = False) -> str:
    """``-1,234.56`` (or ``$1,234.56``); never scientific notation."""
    sign = "-" if value < 0 else ("+" if plus and value > 0 else "")
    return f"{sign}{symbol}{abs(value):,.2f}"


def with_extras(
    stmt: SynthStatement, extras: Sequence[tuple[int, Decimal, str, str]]
) -> SynthStatement:
    """``stmt`` plus rows ``(day offset, amount, payee, memo)``, date-ordered."""
    added = [
        SynthTxn(stmt.start + timedelta(days=d), a, p, m, f"X{i:04d}")
        for i, (d, a, p, m) in enumerate(extras)
    ]
    rows = sorted([*stmt.transactions, *added], key=lambda t: t.posted)
    return replace(stmt, transactions=tuple(rows))


def _statement(
    seed: int,
    institution: FakeInstitution,
    *,
    days: int,
    account_type: str = "checking",
    currency: str = "USD",
    base: Decimal = Decimal("5000.00"),
    credit_card: bool = False,
) -> SynthStatement:
    ledger = synth_ledger(seed, start=START, days=days, credit_card=credit_card)
    return statement(
        ledger,
        START,
        START + timedelta(days=days - 1),
        seed=seed,
        institution=institution,
        account_type=account_type,
        currency=currency,
        base_balance=base,
    )


def sample_national_statement(seed: int = 31, *, days: int = 31) -> SynthStatement:
    """A checking month with a service fee and interest."""
    stmt = _statement(seed, SAMPLE_NATIONAL, days=days)
    return with_extras(
        stmt,
        [
            (days - 1, Decimal("-12.00"), "Monthly service fee", "fee"),
            (days - 1, Decimal("0.42"), "Interest paid", "interest"),
        ],
    )


def example_fcu_statement(seed: int = 32, *, days: int = 31) -> SynthStatement:
    stmt = _statement(seed, EXAMPLE_FCU, days=days, account_type="savings", base=Decimal("8000.00"))
    return with_extras(stmt, [(days - 1, Decimal("3.17"), "Dividend credited", "interest")])


def demo_card_statement(seed: int = 33, *, days: int = 31) -> SynthStatement:
    stmt = _statement(
        seed, DEMO_CARD, days=days, account_type="creditcard", base=Decimal(0), credit_card=True
    )
    return with_extras(
        stmt,
        [
            (14, Decimal("-25.00"), "Late payment fee", "fee"),
            (days - 1, Decimal("-8.31"), "Interest charge on purchases", "interest"),
        ],
    )


def mock_savings_statement(seed: int = 34, *, days: int = 31) -> SynthStatement:
    return _statement(seed, MOCK_SAVINGS, days=days, currency="GBP", base=Decimal("2500.00"))


def generic_statement(seed: int = 35, *, days: int = 12) -> SynthStatement:
    return _statement(seed, GENERIC, days=days, base=Decimal("1500.00"))


def _tampered(stmt: SynthStatement, tamper: Tamper) -> tuple[list[SynthTxn], Decimal]:
    """The rows to print and the closing balance to print."""
    rows = list(stmt.transactions)
    closing = stmt.closing_balance
    if tamper == "extra_row":
        rows = sorted([*rows, FORGED], key=lambda t: t.posted)
    elif tamper == "closing":
        closing += Decimal("100.00")
    return rows, closing


def expected(stmt: SynthStatement, *, tamper: Tamper = None) -> list[tuple[date, Decimal, str]]:
    """``(date, amount, payee)`` of every posted row the PDF prints, sorted."""
    rows, _ = _tampered(stmt, tamper)
    return sorted((t.posted, t.amount, t.payee) for t in rows)


def _running(stmt: SynthStatement, rows: Sequence[SynthTxn]) -> list[Decimal]:
    """Balances after each printed row; a forged row does not move the balance."""
    balance = stmt.opening_balance
    out: list[Decimal] = []
    for t in rows:
        if t.fitid != FORGED.fitid:
            balance += t.amount
        out.append(balance)
    return out


def _us(day: date) -> str:
    return day.strftime("%m/%d/%Y")


def _uk(day: date) -> str:
    return day.strftime("%d/%m/%Y")


# ---------------------------------------------------------------------------
# Sample National Bank checking
# ---------------------------------------------------------------------------


def sample_national_pdf(
    stmt: SynthStatement,
    *,
    rows_per_page: int = 40,
    pending: Sequence[PendingItem] = (),
    tamper: Tamper = None,
    wrap_memos: bool = True,
) -> bytes:
    rows, closing = _tampered(stmt, tamper)
    paired = list(zip(rows, _running(stmt, rows), strict=True))
    pages = [paired[i : i + rows_per_page] for i in range(0, len(paired), rows_per_page)] or [[]]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle("Statement")
    cols = {"date": 72, "desc": 150, "amount": 440, "balance": 540}
    for number, chunk in enumerate(pages, start=1):
        c.setFont(BOLD, 16)
        c.drawString(72, 750, "Sample National Bank, N.A.")
        c.setFont(FONT, 10)
        if number == 1:
            c.drawString(72, 732, "Everyday Checking Account Statement")
            c.drawString(72, 714, f"Account Number: XXXX-XXXX-{stmt.last4}")
            c.drawString(330, 714, f"For the period {_us(stmt.start)} to {_us(stmt.end)}")
            c.drawString(
                72,
                696,
                f"Beginning balance on {_us(stmt.start)} {money(stmt.opening_balance, symbol='$')}",
            )
            c.drawString(
                330, 696, f"Ending balance on {_us(stmt.end)} {money(closing, symbol='$')}"
            )
            top = 660
        else:
            c.drawString(72, 732, "Everyday Checking Account Statement (continued)")
            top = 700
        c.setFont(BOLD, 10)
        c.drawString(cols["date"], top, "Posting Date")
        c.drawString(cols["desc"], top, "Description")
        c.drawRightString(cols["amount"], top, "Amount")
        c.drawRightString(cols["balance"], top, "Balance")
        c.setFont(FONT, 9)
        y = top - 16
        for txn, balance in chunk:
            c.drawString(cols["date"], y, _us(txn.posted))
            c.drawString(cols["desc"], y, txn.payee)
            c.drawRightString(cols["amount"], y, money(txn.amount))
            c.drawRightString(cols["balance"], y, money(balance))
            y -= 14
            if wrap_memos and txn.memo == "Internal transfer":
                c.drawString(cols["desc"], y, f"Ref {txn.fitid}")
                y -= 14
        if number == len(pages) and pending:
            y -= 12
            c.setFont(BOLD, 10)
            c.drawString(72, y, "Pending transactions (not yet posted, not included in balances)")
            c.setFont(FONT, 9)
            y -= 16
            for item in pending:
                c.drawString(cols["date"], y, _us(item.posted))
                c.drawString(cols["desc"], y, item.payee)
                c.drawRightString(cols["amount"], y, money(item.amount))
                y -= 14
        c.setFont(FONT, 8)
        c.drawString(72, 40, f"Page {number} of {len(pages)}")
        c.drawRightString(540, 40, "Sample National Bank is a fictional bank used for testing.")
        c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Example Federal Credit Union (ruled table)
# ---------------------------------------------------------------------------


def example_fcu_pdf(stmt: SynthStatement, *, tamper: Tamper = None) -> bytes:
    rows, closing = _tampered(stmt, tamper)
    balances = _running(stmt, rows)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont(BOLD, 15)
    c.drawString(72, 750, "Example Federal Credit Union")
    c.setFont(FONT, 10)
    c.drawString(72, 732, "Member Share Statement")
    c.drawString(72, 716, f"Member number XXXXXX{stmt.last4}")
    c.drawString(300, 716, f"Statement Period: {_us(stmt.start)} through {_us(stmt.end)}")
    c.drawString(72, 700, f"Previous balance {money(stmt.opening_balance)}")
    c.drawString(300, 700, f"New balance {money(closing)}")
    edges = (66, 140, 330, 410, 480, 546)
    body: list[tuple[str, str, str, str, str]] = [
        ("Date", "Description", "Withdrawals", "Deposits", "Balance")
    ]
    for txn, balance in zip(rows, balances, strict=True):
        out = money(-txn.amount) if txn.amount < 0 else ""
        into = money(txn.amount) if txn.amount > 0 else ""
        body.append((_us(txn.posted), txn.payee, out, into, money(balance)))
    top, height = 676, 16
    for i, cells in enumerate(body):
        y = top - (i + 1) * height + 5
        c.setFont(BOLD if i == 0 else FONT, 9)
        c.drawString(edges[0] + 4, y, cells[0])
        c.drawString(edges[1] + 4, y, cells[1])
        for j in (2, 3, 4):
            c.drawRightString(edges[j + 1] - 4, y, cells[j])
    for i in range(len(body) + 1):
        c.line(edges[0], top - i * height, edges[-1], top - i * height)
    for x in edges:
        c.line(x, top, x, top - len(body) * height)
    c.setFont(FONT, 9)
    dividends = sum((t.amount for t in rows if t.memo == "interest"), Decimal(0))
    c.drawString(72, top - (len(body) + 2) * height, f"Dividends earned this period {dividends}")
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Demo Card Services (credit card)
# ---------------------------------------------------------------------------


def _short(day: date) -> str:
    return day.strftime("%m/%d")


def _abbr(day: date) -> str:
    return f"{day.strftime('%b')} {day.day}, {day.year}"


def demo_card_pdf(
    stmt: SynthStatement,
    *,
    pending: Sequence[PendingItem] = (),
    tamper: Tamper = None,
    rows_per_page: int = 45,
) -> bytes:
    rows, closing = _tampered(stmt, tamper)
    credits = [t for t in rows if t.amount > 0]
    fees = [t for t in rows if t.memo == "fee"]
    interest = [t for t in rows if t.memo == "interest"]
    purchases = [t for t in rows if t.amount < 0 and t not in fees and t not in interest]

    def total(items: Sequence[SynthTxn]) -> Decimal:
        return sum((-t.amount for t in items), Decimal(0))

    lines: list[tuple[str, SynthTxn | PendingItem | str]] = []
    for title, items in (
        ("PAYMENTS AND OTHER CREDITS", credits),
        ("PURCHASES", purchases),
        ("FEES CHARGED", fees),
        ("INTEREST CHARGED", interest),
    ):
        lines.append(("section", title))
        lines.extend(("txn", t) for t in items)
        lines.append(("total", f"Total {title.lower()} {money(total(items), symbol='$')}"))
    if pending:
        lines.append(("section", "PENDING AUTHORIZATIONS"))
        lines.extend(("txn", p) for p in pending)
    pages = [lines[i : i + rows_per_page] for i in range(0, len(lines), rows_per_page)]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    cols = {"trans": 72, "post": 140, "desc": 210, "amount": 540}
    for number, chunk in enumerate(pages, start=1):
        c.setFont(BOLD, 15)
        c.drawString(72, 750, "Demo Card Services")
        c.setFont(FONT, 10)
        top = 700
        if number == 1:
            c.drawString(72, 732, "Rewards Card Monthly Statement")
            c.drawString(72, 716, f"Card ending in {stmt.last4}")
            c.drawString(300, 716, f"Billing Period {_abbr(stmt.start)} - {_abbr(stmt.end)}")
            summary = (
                ("Previous balance", money(-stmt.opening_balance, symbol="$")),
                ("Payments and credits", money(total(credits), symbol="$")),
                ("Purchases", money(total(purchases), symbol="$", plus=True)),
                ("Fees charged", money(total(fees), symbol="$", plus=True)),
                ("Interest charged", money(total(interest), symbol="$", plus=True)),
                ("New Balance", money(-closing, symbol="$")),
                ("Minimum Payment Due", "$25.00"),
            )
            y = 694
            for label, value in summary:
                c.drawString(72, y, label)
                c.drawRightString(300, y, value)
                y -= 14
            top = y - 14
        c.setFont(BOLD, 9)
        c.drawString(cols["trans"], top, "Trans Date")
        c.drawString(cols["post"], top, "Post Date")
        c.drawString(cols["desc"], top, "Description")
        c.drawRightString(cols["amount"], top, "Amount")
        y = top - 16
        for kind, item in chunk:
            if isinstance(item, str):
                c.setFont(BOLD if kind == "section" else FONT, 9)
                c.drawString(72 if kind == "section" else cols["desc"], y, item)
            else:
                c.setFont(FONT, 9)
                c.drawString(cols["trans"], y, _short(item.posted))
                c.drawString(cols["post"], y, _short(item.posted + timedelta(days=1)))
                c.drawString(cols["desc"], y, item.payee)
                amount = item.amount
                printed = money(-amount) if amount < 0 else f"{money(amount)} CR"
                c.drawRightString(cols["amount"], y, printed)
            y -= 14
        c.setFont(FONT, 8)
        c.drawString(72, 40, f"Page {number} of {len(pages)}")
        c.drawString(300, 40, "Annual percentage rate (APR) 19.99%")
        c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Mock Savings and Loan (GBP, DD/MM/YYYY)
# ---------------------------------------------------------------------------


def mock_savings_pdf(stmt: SynthStatement, *, tamper: Tamper = None) -> bytes:
    rows, closing = _tampered(stmt, tamper)
    balances = _running(stmt, rows)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont(BOLD, 15)
    c.drawString(72, 750, "Mock Savings and Loan")
    c.setFont(FONT, 10)
    c.drawString(72, 732, "Current Account Statement")
    c.drawString(72, 716, f"Account no. ****{stmt.last4}")
    c.drawString(300, 716, f"Statement from {_uk(stmt.start)} to {_uk(stmt.end)}")
    c.drawString(72, 700, f"Currency: {stmt.currency}")
    c.drawString(72, 684, f"Balance brought forward {money(stmt.opening_balance)}")
    cols = {"date": 72, "desc": 150, "out": 400, "in": 470, "balance": 545}
    top = 656
    c.setFont(BOLD, 9)
    c.drawString(cols["date"], top, "Date")
    c.drawString(cols["desc"], top, "Details")
    c.drawRightString(cols["out"], top, "Paid out")
    c.drawRightString(cols["in"], top, "Paid in")
    c.drawRightString(cols["balance"], top, "Balance")
    c.setFont(FONT, 9)
    y = top - 16
    for txn, balance in zip(rows, balances, strict=True):
        c.drawString(cols["date"], y, _uk(txn.posted))
        c.drawString(cols["desc"], y, txn.payee)
        column = "out" if txn.amount < 0 else "in"
        c.drawRightString(cols[column], y, money(abs(txn.amount)))
        c.drawRightString(cols["balance"], y, money(balance))
        y -= 14
    c.drawString(72, y - 10, f"Balance carried forward {money(closing)}")
    c.setFont(FONT, 8)
    c.drawString(72, 40, "Mock Savings and Loan is an invented lender.")
    c.showPage()
    c.save()
    return buf.getvalue()


#: How far the right box's rows sit above the left box's (points).
RAISED: Final = 3.0


def side_by_side_pdf(stmt: SynthStatement, *, table: Literal["below", "beside"]) -> bytes:
    """The savings statement with boxes printed side by side.

    ``below``: an account summary beside an interest summary, the
    transaction table across the page under them. ``beside``: the account
    summary in a narrow box left of the transaction table, row for row.
    """
    rows = list(stmt.transactions)
    balances = _running(stmt, rows)
    summary = [
        ("Balance brought forward", stmt.opening_balance),
        ("Balance carried forward", balances[-1]),
    ]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont(BOLD, 15)
    c.drawString(40, 750, "Mock Savings and Loan")
    c.setFont(FONT, 10)
    c.drawString(40, 732, "Current Account Statement")
    c.drawString(40, 716, f"Account no. ****{stmt.last4}")
    c.drawString(300, 716, f"Statement from {_uk(stmt.start)} to {_uk(stmt.end)}")
    c.drawString(40, 700, f"Currency: {stmt.currency}")
    beside = table == "beside"
    left = 40 if beside else 72
    edge = 250 if beside else 72  # the table's left edge
    c.setFont(BOLD, 10)
    c.drawString(left, 676, "Account Summary")
    c.drawString(
        edge if beside else 330, 676, "Transaction Detail" if beside else "Interest Summary"
    )
    c.setFont(FONT, 8 if beside else 9)
    y = 660
    interest = [
        ("Interest paid this period", Decimal("0.42")),
        ("Interest paid this year", Decimal("3.10")),
    ]
    for index, (label, value) in enumerate(summary):
        c.drawString(left, y, label)
        if beside:
            c.drawRightString(left + 180, y, money(value))
        else:
            # The other box's row sits a little higher and the amount between
            # the two: a line-by-line reading tears it from its label.
            c.drawRightString(left + 180, y + RAISED, money(value))
            c.drawString(330, y + 2 * RAISED, interest[index][0])
            c.drawRightString(545, y + 2 * RAISED, money(interest[index][1]))
        y -= 14
    cols = {"date": edge, "desc": edge + 60, "out": 440, "in": 500, "balance": 570}
    top = 660 if beside else 610
    c.setFont(BOLD, 9)
    c.drawString(cols["date"], top, "Date")
    c.drawString(cols["desc"], top, "Details")
    c.drawRightString(cols["out"], top, "Paid out")
    c.drawRightString(cols["in"], top, "Paid in")
    c.drawRightString(cols["balance"], top, "Balance")
    c.setFont(FONT, 9)
    y = top - 14
    for txn, balance in zip(rows, balances, strict=True):
        c.drawString(cols["date"], y, _uk(txn.posted))
        c.drawString(cols["desc"], y, txn.payee)
        column = "out" if txn.amount < 0 else "in"
        c.drawRightString(cols[column], y, money(abs(txn.amount)))
        c.drawRightString(cols["balance"], y, money(balance))
        y -= 14
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# A bank no fingerprint knows
# ---------------------------------------------------------------------------


def generic_pdf(stmt: SynthStatement, *, tamper: Tamper = None) -> bytes:
    rows, _ = _tampered(stmt, tamper)
    balances = _running(stmt, rows)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont(BOLD, 14)
    c.drawString(72, 750, stmt.institution.name)
    c.setFont(FONT, 10)
    c.drawString(72, 732, "Account activity")
    c.drawString(72, 716, f"Account ****{stmt.last4}")
    cols = {"date": 72, "desc": 160, "debit": 400, "credit": 470, "balance": 545}
    top = 680
    c.setFont(BOLD, 9)
    c.drawString(cols["date"], top, "Value Date")
    c.drawString(cols["desc"], top, "Transaction")
    c.drawRightString(cols["debit"], top, "Debit")
    c.drawRightString(cols["credit"], top, "Credit")
    c.drawRightString(cols["balance"], top, "Balance")
    c.setFont(FONT, 9)
    y = top - 16
    for txn, balance in zip(rows, balances, strict=True):
        c.drawString(cols["date"], y, _uk(txn.posted))
        c.drawString(cols["desc"], y, txn.payee)
        column = "debit" if txn.amount < 0 else "credit"
        c.drawRightString(cols[column], y, money(abs(txn.amount)))
        c.drawRightString(cols["balance"], y, money(balance))
        y -= 14
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Payslip
# ---------------------------------------------------------------------------

EARNINGS: Final = (("Basic salary", Decimal("4000.00")), ("Overtime", Decimal("250.00")))
DEDUCTIONS: Final = (
    ("Income tax", Decimal("850.00")),
    ("Social insurance", Decimal("340.00")),
    ("Pension", Decimal("212.50")),
)


def payslip_pdf(*, net_override: Decimal | None = None) -> bytes:
    gross = sum((a for _, a in EARNINGS), Decimal(0))
    deductions = sum((a for _, a in DEDUCTIONS), Decimal(0))
    net = net_override if net_override is not None else gross - deductions
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont(BOLD, 15)
    c.drawString(72, 750, "Initech Example Ltd")
    c.setFont(BOLD, 12)
    c.drawString(72, 730, "PAYSLIP")
    c.setFont(FONT, 10)
    c.drawString(72, 712, "Employee: Jordan Example")
    c.drawString(330, 712, "Employee no: E-0042")
    c.drawString(72, 696, "Pay period: 01/01/2026 to 01/31/2026")
    c.drawString(330, 696, "Pay date: 01/30/2026")
    c.drawString(72, 680, "Currency: USD")
    y = 650

    def row(label: str, value: Decimal, *, bold: bool = False) -> None:
        nonlocal y
        c.setFont(BOLD if bold else FONT, 10)
        c.drawString(72, y, label)
        c.drawRightString(320, y, money(value))
        y -= 16

    c.setFont(BOLD, 11)
    c.drawString(72, y, "Earnings")
    y -= 18
    for label, value in EARNINGS:
        row(label, value)
    row("Gross pay", gross, bold=True)
    y -= 8
    c.setFont(BOLD, 11)
    c.drawString(72, y, "Deductions")
    y -= 18
    for label, value in DEDUCTIONS:
        row(label, value)
    row("Total deductions", deductions, bold=True)
    y -= 8
    row("Net pay", net, bold=True)
    c.showPage()
    c.save()
    return buf.getvalue()

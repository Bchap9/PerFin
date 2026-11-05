# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic documents of every kind for the classifier (training, held-out tests).

Everything is invented and generated from a seed: the institutions (the six
fictional ones of the shipped fingerprints plus generic invented ones), the
merchants, employers, insurers, people's roles, numbers and dates. No real
institution, person, account or document appears and nothing is read from
outside this module (standard library only).

* :func:`institution_document` renders one document in the layout of a
  shipped fingerprint (``FAKE_INSTITUTIONS``, ``SHIPPED_LAYOUTS``), with the facts the classifier
  should find (kind, institution, last four, period or printed date, tax year).
* :func:`generic_document` renders a document of a kind without a known
  layout (other banks, shops, employers, insurers, letters).
* :func:`corpus` mixes both, optionally with OCR-like noise (character
  swaps, dropped and merged lines), for training (``scripts/train_classifier.py``)
  and for the held-out accuracy test (a different seed).
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Final

KINDS: Final = ("statement", "receipt", "invoice", "tax_form", "payslip", "insurance", "other")
TRAINING_SEED: Final = 20261104
HELD_OUT_SEED: Final = 77
TRAINING_PER_KIND: Final = 220


@dataclass(frozen=True)
class FakeInstitution:
    key: str  # the fingerprint key
    name: str
    kind: str
    date_order: str = "mdy"


FAKE_INSTITUTIONS: Final = (
    FakeInstitution("example-fcu-share-statement", "Example Federal Credit Union", "statement"),
    FakeInstitution("sample-national-bank-checking", "Sample National Bank", "statement"),
    FakeInstitution("sample-national-bank-1099-int", "Sample National Bank", "tax_form"),
    FakeInstitution("demo-card-services-card", "Demo Card Services", "statement"),
    FakeInstitution("placeholder-brokerage-account", "Placeholder Brokerage", "statement"),
    FakeInstitution("fictional-mutual-auto-policy", "Fictional Mutual Insurance", "insurance"),
    FakeInstitution(
        "mock-savings-and-loan-statement", "Mock Savings and Loan", "statement", date_order="dmy"
    ),
)

#: Shipped fingerprints of real institutions' layouts. Their documents here are
#: drawn from invented facts like every other; they are left out of
#: :func:`corpus` (the shipped classifier artefact is trained without them).
SHIPPED_LAYOUTS: Final = (
    FakeInstitution("fidelity-investments-statement", "Fidelity Investments", "statement"),
)

_GENERIC_BANKS: Final = (
    "Riverbend Test Bank",
    "Lakeshore Imaginary Trust",
    "Northwind Pretend Bank",
    "Copperleaf Fake Credit Union",
    "Hilltop Invented Savings",
)
_MERCHANTS: Final = (
    "Blue Owl Grocers",
    "Corner Fuel",
    "Maple Bakery",
    "Quartz Books",
    "Lantern Pharmacy",
    "Harbor Hardware",
    "Juniper Cafe",
    "Pebble Market",
)
_ITEMS: Final = (
    "Milk 1L",
    "Bread",
    "Apples",
    "Coffee beans",
    "Batteries AA",
    "Notebook",
    "Paint brush",
    "Shampoo",
    "Rice 2kg",
    "Tomatoes",
    "Light bulb",
    "Unleaded",
    "Croissant",
    "Paperback",
)
_VENDORS: Final = (
    "Gizmo Example Consulting",
    "Sprocket Pretend Plumbing",
    "Widget Fictional Design",
    "Tinker Sample Electric",
    "Cobalt Imaginary Hosting",
)
_EMPLOYERS: Final = (
    "Northwind Example Ltd",
    "Contoso Pretend Works",
    "Fabrikam Sample Labs",
    "Tailspin Fictional Toys",
)
_INSURERS: Final = (
    "Steady Pretend Assurance",
    "Harbor Example Insurance",
    "Oakline Fictional Health Plan",
)
_SERVICES: Final = (
    "Monthly retainer",
    "Website hosting",
    "Repair labour",
    "Design work",
    "Parts and materials",
    "Support hours",
    "Call-out fee",
)
_PAYEES: Final = (
    "GROCERY OUTLET",
    "ONLINE TRANSFER",
    "PAYROLL DEPOSIT",
    "COFFEE SHOP",
    "UTILITY PAYMENT",
    "ATM WITHDRAWAL",
    "CARD PURCHASE",
    "RENT PAYMENT",
    "INTEREST",
    "SERVICE FEE",
    "MOBILE DEPOSIT",
    "PHONE BILL",
)
_LETTER_SENTENCES: Final = (
    "We are writing to let you know about upcoming changes to our privacy notice.",
    "Our annual member meeting will be held at the community hall.",
    "Please keep this letter for your records.",
    "Thank you for being a valued member of our community.",
    "You can update your contact details online or by phone.",
    "Our branch opening hours will change next month.",
    "This newsletter includes tips on saving for the holidays.",
    "If you have questions please contact our support team.",
    "We have updated our terms and conditions effective next quarter.",
    "Join our webinar about planning for retirement.",
    "Your new card will arrive in a plain envelope.",
    "Remember to never share your password with anyone.",
    "The library book you reserved is ready for collection.",
    "The neighbourhood association invites you to the summer picnic.",
    "Your subscription preferences have been saved.",
    "We moved our office to a new address downtown.",
    "Please find enclosed the brochure you requested.",
)


@dataclass(frozen=True)
class SynthDoc:
    kind: str
    lines: tuple[str, ...]
    institution: str | None = None
    fingerprint: str | None = None
    last4: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    printed_date: date | None = None
    tax_year: int | None = None
    table_headers: tuple[tuple[str, ...], ...] = ()
    date_order: str = "mdy"
    explicit_period: bool = True
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _money(rng: random.Random, low: float = 1.0, high: float = 900.0) -> str:
    return f"{rng.uniform(low, high):,.2f}"


def _digits(rng: random.Random, n: int) -> str:
    return "".join(str(rng.randrange(10)) for _ in range(n))


def _month_window(rng: random.Random) -> tuple[date, date]:
    year = rng.choice((2024, 2025, 2026))
    month = rng.randrange(1, 13)
    start = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return start, nxt - timedelta(days=1)


_MONTH_NAMES: Final = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def fmt_date(d: date, style: str) -> str:
    """``d`` in one of the printed styles the period detector must read."""
    if style == "iso":
        return d.isoformat()
    if style == "mdy":
        return f"{d.month:02d}/{d.day:02d}/{d.year}"
    if style == "mdy_short":
        return f"{d.month}/{d.day}/{d.year % 100:02d}"
    if style == "dmy_dot":
        return f"{d.day:02d}.{d.month:02d}.{d.year}"
    if style == "dmy_slash":
        return f"{d.day:02d}/{d.month:02d}/{d.year}"
    if style == "long":
        return f"{_MONTH_NAMES[d.month - 1]} {d.day}, {d.year}"
    if style == "abbr":
        return f"{_MONTH_NAMES[d.month - 1][:3]} {d.day}, {d.year}"
    if style == "dmy_name":
        return f"{d.day} {_MONTH_NAMES[d.month - 1][:3]} {d.year}"
    if style == "dmy_dash":
        return f"{d.day:02d}-{_MONTH_NAMES[d.month - 1][:3]}-{d.year}"
    raise ValueError(style)


US_STYLES: Final = ("iso", "mdy", "mdy_short", "long", "abbr", "dmy_name", "dmy_dash")
DMY_STYLES: Final = ("iso", "dmy_dot", "dmy_slash", "dmy_name", "dmy_dash", "long")


def _txn_lines(
    rng: random.Random, start: date, end: date, style: str, n: int
) -> tuple[list[str], list[date]]:
    span = (end - start).days
    days = sorted(start + timedelta(days=rng.randrange(span + 1)) for _ in range(n))
    balance = rng.uniform(500, 9000)
    out = []
    for d in days:
        amount = rng.uniform(-400, 400)
        balance += amount
        out.append(f"{fmt_date(d, style)} {rng.choice(_PAYEES)} {amount:,.2f} {balance:,.2f}")
    return out, days


# ---------------------------------------------------------------------------
# Documents in the shipped fingerprints' layouts
# ---------------------------------------------------------------------------


def _example_fcu(rng: random.Random) -> SynthDoc:
    start, end = _month_window(rng)
    last4 = _digits(rng, 4)
    style = rng.choice(US_STYLES)
    txns, _ = _txn_lines(rng, start, end, style, rng.randrange(4, 12))
    header = ("Date", "Description", "Withdrawals", "Deposits", "Balance")
    lines = [
        "Example Federal Credit Union",
        "Member Share Statement",
        "Federally insured by a pretend agency",
        f"Member number XXXXXX{last4}",
        f"Statement Period: {fmt_date(start, style)} through {fmt_date(end, style)}",
        "Share Savings Regular Shares",
        f"Previous balance {_money(rng, 100, 5000)}",
        " ".join(header),
        *txns,
        f"New balance {_money(rng, 100, 5000)}",
        "Dividends earned this period 0.42",
    ]
    return SynthDoc(
        "statement",
        tuple(lines),
        "Example Federal Credit Union",
        "example-fcu-share-statement",
        last4,
        start,
        end,
        table_headers=(header,),
    )


def _sample_national_checking(rng: random.Random) -> SynthDoc:
    start, end = _month_window(rng)
    last4 = _digits(rng, 4)
    style = rng.choice(US_STYLES)
    txns, _ = _txn_lines(rng, start, end, style, rng.randrange(4, 12))
    header = ("Posting Date", "Description", "Amount", "Balance")
    lines = [
        "Sample National Bank, N.A.",
        "Everyday Checking Account Statement",
        f"Account Number: XXXX-XXXX-{last4}",
        f"For the period {fmt_date(start, style)} to {fmt_date(end, style)}",
        f"Beginning balance on {fmt_date(start, style)} {_money(rng)}",
        "Deposits and other additions",
        "Withdrawals and other subtractions",
        " ".join(header),
        *txns,
        f"Ending balance on {fmt_date(end, style)} {_money(rng)}",
        "Sample National Bank is a fictional bank used for testing.",
    ]
    return SynthDoc(
        "statement",
        tuple(lines),
        "Sample National Bank",
        "sample-national-bank-checking",
        last4,
        start,
        end,
        table_headers=(header,),
    )


def _sample_national_1099(rng: random.Random) -> SynthDoc:
    year = rng.choice((2023, 2024, 2025))
    last4 = _digits(rng, 4)
    lines = [
        "Sample National Bank",
        f"Form 1099-INT Interest Income {year}",
        "Copy B For Recipient",
        f"Tax year {year}",
        "PAYER'S name, street address, city or town",
        "PAYER'S TIN XX-XXX" + _digits(rng, 4),
        "RECIPIENT'S TIN XXX-XX-" + _digits(rng, 4),
        f"Account number XXXXXX{last4}",
        f"1 Interest income {_money(rng, 1, 500)}",
        "2 Early withdrawal penalty 0.00",
        "4 Federal income tax withheld 0.00",
        "This is important tax information and is being furnished to the tax authority.",
    ]
    return SynthDoc(
        "tax_form",
        tuple(lines),
        "Sample National Bank",
        "sample-national-bank-1099-int",
        last4,
        tax_year=year,
    )


def _demo_card(rng: random.Random) -> SynthDoc:
    start, end = _month_window(rng)
    start = start + timedelta(days=rng.randrange(0, 10))
    end = start + timedelta(days=rng.randrange(27, 31))
    last4 = _digits(rng, 4)
    style = rng.choice(("abbr", "long", "mdy"))
    txns, _ = _txn_lines(rng, start, end, style, rng.randrange(4, 12))
    header = ("Trans Date", "Post Date", "Description", "Amount")
    lines = [
        "Demo Card Services",
        "Rewards Card Monthly Statement",
        f"Card ending in {last4}",
        f"Billing Period {fmt_date(start, style)} - {fmt_date(end, style)}",
        f"New Balance {_money(rng)}",
        f"Minimum Payment Due {_money(rng, 25, 80)}",
        f"Payment Due Date {fmt_date(end + timedelta(days=25), style)}",
        "Credit limit 5,000.00 Available credit 3,210.00",
        " ".join(header),
        *txns,
        "Interest charge calculation",
        "Annual percentage rate (APR) 19.99%",
    ]
    return SynthDoc(
        "statement",
        tuple(lines),
        "Demo Card Services",
        "demo-card-services-card",
        last4,
        start,
        end,
        table_headers=(header,),
    )


def _placeholder_brokerage(rng: random.Random) -> SynthDoc:
    start, end = _month_window(rng)
    last4 = _digits(rng, 4)
    style = rng.choice(US_STYLES)
    header = ("Date", "Activity", "Description", "Quantity", "Price", "Amount")
    lines = [
        "Placeholder Brokerage",
        "Investment Account Statement",
        f"Account XXXX-{last4}",
        f"Statement period: {fmt_date(start, style)} - {fmt_date(end, style)}",
        "Portfolio Summary",
        f"Total account value {_money(rng, 1000, 90000)}",
        "Holdings Symbol Quantity Price Market value",
        f"EXMPL Example Index Fund {rng.randrange(1, 300)} {_money(rng, 10, 400)}",
        " ".join(header),
        f"{fmt_date(start + timedelta(days=3), style)} Buy EXMPL 10 41.20 412.00",
        f"{fmt_date(start + timedelta(days=9), style)} Dividend SMPLX 0 0 12.34",
        "Realized gain loss summary",
    ]
    return SynthDoc(
        "statement",
        tuple(lines),
        "Placeholder Brokerage",
        "placeholder-brokerage-account",
        last4,
        start,
        end,
        table_headers=(header,),
    )


def _fidelity(rng: random.Random) -> SynthDoc:
    """One account of the Fidelity "Investment Report" layout (invented facts)."""
    start, end = _month_window(rng)
    last4 = _digits(rng, 4)
    number = f"{_digits(rng, 3)}-{_digits(rng, 2)}{last4}"
    title = rng.choice(("ROTH IRA", "TRADITIONAL IRA", "HEALTH SAVINGS ACCOUNT"))
    lines = [
        "Fidelity Investments",
        f"INVESTMENT REPORT {fmt_date(start, 'long')} - {fmt_date(end, 'long')}",
        f"Your Portfolio Value {_money(rng, 1000, 90000)}",
        "Accounts Included in This Report",
        f"{title} Account Number: {number}",
        "Account Summary",
        f"Beginning Account Value {_money(rng, 1000, 90000)}",
        f"Ending Account Value {_money(rng, 1000, 90000)}",
        "Holdings",
        "Mutual Funds",
        f"SAMPLE TOTAL MARKET INDEX FUND (ZZFTX) {rng.randrange(1, 300)} {_money(rng, 10, 400)}",
        f"Total Holdings {_money(rng, 1000, 90000)}",
        "Synthetic test document; not a real statement.",
    ]
    return SynthDoc(
        "statement",
        tuple(lines),
        "Fidelity Investments",
        "fidelity-investments-statement",
        last4,
        # The period is unlabelled: the brokerage parser reads it, not the classifier.
    )


def _fictional_mutual(rng: random.Random) -> SynthDoc:
    start = date(rng.choice((2025, 2026)), rng.randrange(1, 13), 1)
    end = date(start.year + 1, start.month, 1) - timedelta(days=1)
    if (end - start).days > 366:
        end = start + timedelta(days=364)
    last4 = _digits(rng, 4)
    style = rng.choice(US_STYLES)
    lines = [
        "Fictional Mutual Insurance",
        "Auto Policy Declarations Page",
        f"Policy Number FMI-XXXX-{last4}",
        f"Policy Period: {fmt_date(start, style)} to {fmt_date(end, style)}",
        "Named insured: Policyholder",
        "Vehicle 1 Pretend Motors Sedan",
        f"Bodily injury liability {_money(rng, 100, 400)}",
        f"Collision deductible {rng.choice(('250', '500', '1,000'))}",
        f"Total policy premium {_money(rng, 400, 2400)}",
        "This declarations page is part of your policy.",
    ]
    return SynthDoc(
        "insurance",
        tuple(lines),
        "Fictional Mutual Insurance",
        "fictional-mutual-auto-policy",
        last4,
        start,
        end,
    )


def _mock_savings(rng: random.Random) -> SynthDoc:
    start, end = _month_window(rng)
    last4 = _digits(rng, 4)
    style = rng.choice(("dmy_dot", "dmy_slash"))
    txns, _ = _txn_lines(rng, start, end, style, rng.randrange(4, 12))
    header = ("Date", "Details", "Paid out", "Paid in", "Balance")
    lines = [
        "Mock Savings and Loan",
        "Current Account Statement",
        f"Account no. ****{last4}",
        f"Statement from {fmt_date(start, style)} to {fmt_date(end, style)}",
        f"Balance brought forward {_money(rng)}",
        " ".join(header),
        *txns,
        f"Balance carried forward {_money(rng)}",
        "Mock Savings and Loan is an invented lender.",
    ]
    return SynthDoc(
        "statement",
        tuple(lines),
        "Mock Savings and Loan",
        "mock-savings-and-loan-statement",
        last4,
        start,
        end,
        table_headers=(header,),
        date_order="dmy",
    )


_LAYOUTS: Final[dict[str, Callable[[random.Random], SynthDoc]]] = {
    "example-fcu-share-statement": _example_fcu,
    "sample-national-bank-checking": _sample_national_checking,
    "sample-national-bank-1099-int": _sample_national_1099,
    "demo-card-services-card": _demo_card,
    "placeholder-brokerage-account": _placeholder_brokerage,
    "fictional-mutual-auto-policy": _fictional_mutual,
    "mock-savings-and-loan-statement": _mock_savings,
    "fidelity-investments-statement": _fidelity,
}


def institution_document(key: str, seed: int = 0) -> SynthDoc:
    return _LAYOUTS[key](random.Random(f"{key}:{seed}"))


# ---------------------------------------------------------------------------
# Documents of each kind without a shipped layout
# ---------------------------------------------------------------------------


def _generic_statement(rng: random.Random) -> SynthDoc:
    bank = rng.choice(_GENERIC_BANKS)
    start, end = _month_window(rng)
    style = rng.choice(US_STYLES)
    last4 = _digits(rng, 4)
    txns, _ = _txn_lines(rng, start, end, style, rng.randrange(3, 14))
    explicit = rng.random() < 0.7
    label = rng.choice(
        (
            "Statement period",
            "Statement Period:",
            "Period covered",
            "For the period",
            "Activity from",
        )
    )
    joiner = rng.choice(("to", "-", "through"))
    head = [bank, rng.choice(("Account statement", "Monthly statement", "Savings statement"))]
    head.append(rng.choice((f"Account ending {last4}", f"Account ****{last4}")))
    if explicit:
        head.append(f"{label} {fmt_date(start, style)} {joiner} {fmt_date(end, style)}")
    body = [
        rng.choice(("Opening balance", "Beginning balance", "Previous balance"))
        + f" {_money(rng)}",
        rng.choice(("Date Description Amount Balance", "Date Transaction Debit Credit Balance")),
        *txns,
        rng.choice(("Closing balance", "Ending balance", "New balance")) + f" {_money(rng)}",
        rng.choice(("Total deposits", "Total credits")) + f" {_money(rng)}",
        rng.choice(("Total withdrawals", "Total debits")) + f" {_money(rng)}",
    ]
    if rng.random() < 0.5:
        body.append(f"Interest paid {_money(rng, 0, 9)}")
    return SynthDoc(
        "statement",
        tuple(head + body),
        bank,
        None,
        last4,
        start,
        end,
        explicit_period=explicit,
    )


def _receipt(rng: random.Random) -> SynthDoc:
    merchant = rng.choice(_MERCHANTS)
    day = date(rng.choice((2025, 2026)), rng.randrange(1, 13), rng.randrange(1, 29))
    style = rng.choice(US_STYLES)
    items = [f"{rng.choice(_ITEMS)} {_money(rng, 1, 40)}" for _ in range(rng.randrange(1, 8))]
    lines = [
        merchant.upper(),
        f"{rng.randrange(10, 999)} Example Street",
        rng.choice(("Store #", "Store no. ", "Branch ")) + str(rng.randrange(1, 900)),
        f"Date: {fmt_date(day, style)} {rng.randrange(7, 22):02d}:{rng.randrange(60):02d}",
        rng.choice(("Cashier: 4", "Register 2", "Till 3 Operator 17")),
        *items,
        f"Subtotal {_money(rng, 5, 120)}",
        rng.choice(("Sales tax", "Tax", "VAT 20%")) + f" {_money(rng, 0, 9)}",
        f"TOTAL {_money(rng, 5, 130)}",
        rng.choice(
            (f"VISA ****{_digits(rng, 4)}", "CASH 50.00", f"DEBIT CARD XXXX{_digits(rng, 4)}")
        ),
        rng.choice(("CHANGE 0.00", "Approved", "Auth code 0" + _digits(rng, 5))),
        rng.choice(
            ("Thank you for shopping with us", "Thank you, come again", "Keep your receipt")
        ),
    ]
    if rng.random() < 0.3:
        lines.append("Returns accepted within 30 days with receipt")
    return SynthDoc("receipt", tuple(lines), merchant, printed_date=day)


def _invoice(rng: random.Random) -> SynthDoc:
    vendor = rng.choice(_VENDORS)
    day = date(rng.choice((2025, 2026)), rng.randrange(1, 13), rng.randrange(1, 29))
    style = rng.choice(US_STYLES)
    rows = [
        " ".join(
            (
                rng.choice(_SERVICES),
                str(rng.randrange(1, 9)),
                _money(rng, 20, 300),
                _money(rng, 20, 900),
            )
        )
        for _ in range(rng.randrange(1, 6))
    ]
    lines = [
        vendor,
        rng.choice(("INVOICE", "Tax Invoice", "Invoice")),
        f"Invoice number INV-{_digits(rng, 5)}",
        f"Invoice date {fmt_date(day, style)}",
        f"Due date {fmt_date(day + timedelta(days=30), style)}",
        "Bill to: Customer",
        rng.choice(("Terms: Net 30", "Payment terms 14 days", "Due on receipt")),
        "Description Qty Unit price Amount",
        *rows,
        f"Subtotal {_money(rng, 50, 2000)}",
        f"Amount due {_money(rng, 50, 2000)}",
        rng.choice(("Please remit payment to the account below", "Remit to: Accounts receivable")),
    ]
    if rng.random() < 0.4:
        lines.append(f"Purchase order PO-{_digits(rng, 4)}")
    return SynthDoc("invoice", tuple(lines), vendor, printed_date=day)


def _tax_form(rng: random.Random) -> SynthDoc:
    year = rng.choice((2022, 2023, 2024, 2025))
    variant = rng.randrange(4)
    issuer = rng.choice((*_EMPLOYERS, *_GENERIC_BANKS))
    if variant == 0:
        lines = [
            f"Form W-2 Wage and Tax Statement {year}",
            "Copy B To Be Filed With Employee's Federal Tax Return",
            f"a Employee's social security number XXX-XX-{_digits(rng, 4)}",
            f"c Employer's name, address, and ZIP code {issuer}",
            f"1 Wages, tips, other compensation {_money(rng, 20000, 90000)}",
            f"2 Federal income tax withheld {_money(rng, 1000, 9000)}",
            f"3 Social security wages {_money(rng, 20000, 90000)}",
            f"5 Medicare wages and tips {_money(rng, 20000, 90000)}",
            "OMB No. 1545-0008",
        ]
    elif variant == 1:
        lines = [
            issuer,
            f"Form 1099-DIV Dividends and Distributions {year}",
            f"For calendar year {year}",
            "PAYER'S TIN RECIPIENT'S TIN",
            f"1a Total ordinary dividends {_money(rng, 1, 900)}",
            f"1b Qualified dividends {_money(rng, 1, 900)}",
            "Copy B For Recipient",
        ]
    elif variant == 2:
        lines = [
            issuer,
            f"{year} Form 1098 Mortgage Interest Statement",
            "RECIPIENT'S/LENDER'S name PAYER'S/BORROWER'S TIN",
            f"1 Mortgage interest received from payer(s)/borrower(s) {_money(rng, 900, 9000)}",
            "2 Outstanding mortgage principal",
            "This information is being furnished to the tax authority.",
        ]
    else:
        lines = [
            issuer,
            f"Annual tax summary - Tax year {year}",
            "Consolidated Form 1099",
            f"Interest income {_money(rng, 1, 300)}",
            f"Federal income tax withheld {_money(rng, 0, 50)}",
            "Keep for your tax records",
        ]
    return SynthDoc("tax_form", tuple(lines), issuer, tax_year=year)


def _payslip(rng: random.Random) -> SynthDoc:
    employer = rng.choice(_EMPLOYERS)
    start = date(rng.choice((2025, 2026)), rng.randrange(1, 13), rng.choice((1, 16)))
    end = start + timedelta(days=rng.choice((13, 14)))
    style = rng.choice(US_STYLES)
    lines = [
        employer,
        rng.choice(("Earnings Statement", "Pay Slip", "Payslip", "Statement of earnings")),
        f"Employee ID {_digits(rng, 6)}",
        f"Pay period {fmt_date(start, style)} - {fmt_date(end, style)}",
        f"Pay date {fmt_date(end + timedelta(days=3), style)}",
        "Earnings Rate Hours Current YTD",
        f"Regular {_money(rng, 15, 80)} 80.00 {_money(rng, 1000, 5000)} {_money(rng, 9000, 60000)}",
        rng.choice(("Overtime", "Bonus", "Holiday pay")) + f" {_money(rng, 0, 400)}",
        f"Gross pay {_money(rng, 1000, 6000)}",
        "Deductions Current YTD",
        f"Federal withholding {_money(rng, 100, 900)}",
        f"Social Security {_money(rng, 60, 400)}",
        f"Medicare {_money(rng, 15, 90)}",
        rng.choice(("401k", "Pension contribution", "Health insurance"))
        + f" {_money(rng, 20, 300)}",
        f"Net pay {_money(rng, 800, 5000)}",
        f"Direct deposit XXXX{_digits(rng, 4)}",
    ]
    return SynthDoc("payslip", tuple(lines), employer, period_start=start, period_end=end)


def _insurance(rng: random.Random) -> SynthDoc:
    insurer = rng.choice(_INSURERS)
    variant = rng.randrange(3)
    start = date(rng.choice((2025, 2026)), rng.randrange(1, 13), 1)
    end = start + timedelta(days=rng.choice((182, 364)))
    style = rng.choice(US_STYLES)
    if variant == 0:
        lines = [
            insurer,
            "Homeowners Policy Declarations",
            f"Policy number HP-{_digits(rng, 7)}",
            f"Policy period {fmt_date(start, style)} to {fmt_date(end, style)}",
            "Coverage A Dwelling Limit of liability",
            f"Deductible {rng.choice(('500', '1,000', '2,500'))}",
            f"Annual premium {_money(rng, 400, 3000)}",
            "Named insured and mailing address",
        ]
    elif variant == 1:
        lines = [
            insurer,
            "Explanation of Benefits",
            "This is not a bill",
            f"Claim number {_digits(rng, 10)}",
            f"Member ID XXXX{_digits(rng, 4)}",
            f"Date of service {fmt_date(start, style)}",
            f"Amount billed {_money(rng, 50, 900)} Plan paid {_money(rng, 10, 700)}",
            f"Your responsibility {_money(rng, 0, 200)}",
            "Coinsurance Copay Deductible",
        ]
    else:
        lines = [
            insurer,
            "Renewal notice - Auto insurance",
            f"Policy {_digits(rng, 8)}",
            f"Coverage period {fmt_date(start, style)} through {fmt_date(end, style)}",
            "Liability Comprehensive Collision Uninsured motorist",
            f"Premium due {_money(rng, 200, 1500)}",
            "Proof of insurance card enclosed",
        ]
    return SynthDoc(
        "insurance",
        tuple(lines),
        insurer,
        period_start=start if variant != 1 else None,
        period_end=end if variant != 1 else None,
    )


def _other(rng: random.Random) -> SynthDoc:
    sender = rng.choice((*_GENERIC_BANKS, *_MERCHANTS, "Town Library", "Residents Association"))
    lines = [
        sender,
        rng.choice(("Dear member,", "Dear customer,", "Hello neighbour,", "Important notice")),
        *rng.sample(_LETTER_SENTENCES, rng.randrange(3, 7)),
        rng.choice(("Sincerely,", "Kind regards,", "Best wishes,")),
        rng.choice(("The member services team", "Customer care", "The organising committee")),
    ]
    return SynthDoc("other", tuple(lines), sender)


_GENERIC: Final[dict[str, Callable[[random.Random], SynthDoc]]] = {
    "statement": _generic_statement,
    "receipt": _receipt,
    "invoice": _invoice,
    "tax_form": _tax_form,
    "payslip": _payslip,
    "insurance": _insurance,
    "other": _other,
}


def generic_document(kind: str, seed: int = 0) -> SynthDoc:
    return _GENERIC[kind](random.Random(f"{kind}:{seed}"))


# ---------------------------------------------------------------------------
# Noise and corpora
# ---------------------------------------------------------------------------

_OCR_SWAPS: Final = {"o": "0", "l": "1", "e": "c", "s": "5", "i": "l", "a": "o", "t": "f"}


def ocr_noise(lines: tuple[str, ...], rng: random.Random, rate: float = 0.03) -> tuple[str, ...]:
    """OCR-like damage: character swaps, a dropped line, two lines merged."""
    out = []
    for line in lines:
        chars = [
            _OCR_SWAPS.get(c, c) if c in _OCR_SWAPS and rng.random() < rate else c for c in line
        ]
        out.append("".join(chars))
    if len(out) > 4 and rng.random() < 0.5:
        del out[rng.randrange(1, len(out))]
    if len(out) > 4 and rng.random() < 0.3:
        i = rng.randrange(1, len(out) - 1)
        out[i : i + 2] = [out[i] + " " + out[i + 1]]
    return tuple(out)


def corpus(seed: int, per_kind: int, *, noisy_share: float = 0.4) -> Iterator[SynthDoc]:
    """``per_kind`` documents of every kind; about a fifth in the shipped layouts."""
    rng = random.Random(seed)
    by_kind: dict[str, list[str]] = {}
    for inst in FAKE_INSTITUTIONS:
        by_kind.setdefault(inst.kind, []).append(inst.key)
    for kind in KINDS:
        for i in range(per_kind):
            sub = rng.randrange(1 << 30)
            layouts = by_kind.get(kind, [])
            if layouts and rng.random() < 0.2:
                doc = _LAYOUTS[rng.choice(layouts)](random.Random(sub))
            else:
                doc = _GENERIC[kind](random.Random(sub))
            if rng.random() < noisy_share:
                doc = SynthDoc(
                    doc.kind,
                    ocr_noise(doc.lines, random.Random(sub ^ i)),
                    doc.institution,
                    doc.fingerprint,
                    doc.last4,
                    doc.period_start,
                    doc.period_end,
                    doc.printed_date,
                    doc.tax_year,
                    doc.table_headers,
                    doc.date_order,
                    doc.explicit_period,
                    {"noisy": "1"},
                )
            yield doc


def training_samples() -> list[tuple[str, str]]:
    """The fixed training set of the shipped artefact."""
    return [(d.text, d.kind) for d in corpus(TRAINING_SEED, TRAINING_PER_KIND)]


#: Version of the shipped artefact; bump with any change to the generator or features.
MODEL_VERSION: Final = "1.0.0"


def build_artefact() -> str:
    """Train on :func:`training_samples` and return the artefact's canonical JSON."""
    from app.ingestion.classify import model

    trained = model.train(
        training_samples(),
        model_version=MODEL_VERSION,
        training={
            "generator": "tests/fixtures/synth_classify.py",
            "seed": TRAINING_SEED,
            "per_kind": TRAINING_PER_KIND,
            "samples": TRAINING_PER_KIND * len(KINDS),
        },
    )
    return model.dumps(trained)

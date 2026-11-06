# SPDX-License-Identifier: AGPL-3.0-or-later
"""A synthetic provider-neutral combined statement: checking, savings and brokerage.

The second multi-account layout, with none of the vocabulary of
``synth_fidelity_report.py``, so the multi-account work is shown to
generalise rather than to fit one institution. The bank, the customer, the
merchants, the account numbers and the instruments (symbols starting
``ZZ``) are all invented and every page says it is a synthetic test
document.

Layout traits:

* **Labelled period** in every page header: ``Statement Period: 03/01/2024 -
  03/31/2024`` (numeric dates, years on both ends), the bank's name printed
  as text (``HARBORVIEW TEST FINANCIAL``) and in the PDF metadata.
* **Account index** on page 1, *Your Accounts at a Glance* (``Account Name |
  Account Number | Opening Balance | Closing Balance``), with masked
  numbers (``XXXX-4821``).
* **Account headings on their own lines**: the account name, then
  ``Account Number: XXXX-4821`` alone on the next line. A page that continues
  an account repeats no heading (page 3 continues the checking account,
  page 5 the brokerage account), and page 4 holds the end of one account
  (savings) and the start of the next (brokerage).
* **Checking** (bank): *Account Summary* (``Beginning Balance on
  03/01/2024``, credits, debits, checks, ``Ending Balance on 03/31/2024``),
  *Transaction Detail* (``Date | Description | Amount``, signed amounts,
  year-less ``MM/DD`` dates) continued over a page break with the header
  repeated, *Checks Paid* (``Check No. | Date Paid | Amount``) and a
  *Daily Ending Balance* table that is not activity.
* **Savings** (bank): the same summary words, a short *Transaction Detail*.
* **Brokerage** (investment): *Account Value Summary*, *Portfolio Holdings*
  (``Symbol | Description | Quantity | Price | Market Value``, single-line
  header, the sweep as a ``Cash & Sweep Balance`` line, not a position),
  *Trade Activity* (``Trade Date | Activity | Symbol | Description | Units |
  Unit Price | Commission | Net Amount``, full ``MM/DD/YYYY`` dates) and
  *Cash Activity* (``Date | Description | Amount``).
* Transfers between the accounts name the other account's masked number
  in their descriptions (``TRANSFER TO BROKERAGE XXXX-6619``): a mention,
  not a heading.

Everything reconciles: each bank account's opening balance plus its rows is
its closing balance; the brokerage's positions are quantity x price, the
sweep's opening cash plus the activity is its closing cash, positions +
cash = the account value. :func:`expected` and the shared helpers give what
a correct parser must return per account.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from reportlab.pdfgen import canvas

from tests.fixtures.synth_multi_account import (
    BOLD,
    DISCLAIMER,
    FONT,
    ZERO,
    Account,
    Activity,
    ActivityType,
    Cursor,
    Page,
    Position,
    Report,
    check_account,
    expected_activity,
    expected_bank_rows,
    expected_positions,
    money,
    number,
    render,
)

__all__ = [
    "EXPECTED_HEADER_LINES",
    "INSTITUTION",
    "Expected",
    "expected",
    "expected_activity",
    "expected_bank_rows",
    "expected_positions",
    "report",
    "report_pdf",
]

INSTITUTION: Final = "Harborview Test Financial"
START: Final = date(2024, 3, 1)
END: Final = date(2024, 3, 31)
#: Rows of the checking account's *Transaction Detail* on its first page.
FIRST_PAGE_ROWS: Final = 14


def _period_text(start: date, end: date) -> str:
    return f"Statement Period: {start:%m/%d/%Y} - {end:%m/%d/%Y}"


EXPECTED_HEADER_LINES: Final = (INSTITUTION.upper(), _period_text(START, END))


def report() -> Report:
    """The three accounts of March 2024 (module doc)."""

    def d(day: int) -> date:
        return START.replace(day=day)

    def row(
        day: int, kind: ActivityType, text: str, amount: str, section: str = "detail"
    ) -> Activity:
        return Activity(d(day), kind, section, text, Decimal(amount))

    checking = Account(
        number="XXXX-4821",
        title="Everyday Checking",
        kind="bank",
        subtype="checking",
        cash_opening=Decimal("2450.17"),
        activity=(
            row(1, "deposit", "DIRECT DEPOSIT EXAMPLE EMPLOYER PAYROLL", "2150.00"),
            row(2, "withdrawal", "CARD PURCHASE SAMPLE GROCERY 0412", "-84.31"),
            row(4, "withdrawal", "CARD PURCHASE DEMO FUEL STATION", "-42.10"),
            row(5, "withdrawal", "ONLINE TRANSFER TO SAVINGS XXXX-5530", "-300.00"),
            row(6, "withdrawal", "ACH DEBIT EXAMPLE POWER CO UTILITY", "-126.45"),
            row(7, "withdrawal", "CARD PURCHASE PLACEHOLDER COFFEE", "-6.75"),
            row(8, "withdrawal", "ATM WITHDRAWAL 100 MAIN ST ANYTOWN", "-60.00"),
            row(11, "withdrawal", "CARD PURCHASE SAMPLE HARDWARE", "-58.20"),
            row(12, "withdrawal", "ACH DEBIT MOCK MOBILE WIRELESS", "-71.99"),
            row(13, "withdrawal", "CARD PURCHASE SAMPLE GROCERY 0412", "-102.66"),
            row(14, "withdrawal", "TRANSFER TO BROKERAGE XXXX-6619", "-1000.00"),
            row(15, "deposit", "DIRECT DEPOSIT EXAMPLE EMPLOYER PAYROLL", "2150.00"),
            row(15, "withdrawal", "ACH DEBIT FICTIONAL MORTGAGE SERVICING", "-1450.00"),
            row(18, "withdrawal", "CARD PURCHASE DEMO PHARMACY", "-23.40"),
            row(19, "deposit", "CARD REFUND SAMPLE OUTFITTERS", "35.00"),
            row(20, "withdrawal", "CARD PURCHASE PLACEHOLDER BOOKS", "-18.99"),
            row(21, "withdrawal", "ACH DEBIT EXAMPLE INSURANCE CO", "-142.00"),
            row(22, "withdrawal", "CARD PURCHASE DEMO FUEL STATION", "-39.85"),
            row(25, "deposit", "MOBILE DEPOSIT", "250.00"),
            row(26, "withdrawal", "CARD PURCHASE SAMPLE GROCERY 0412", "-91.37"),
            row(27, "withdrawal", "ACH DEBIT EXAMPLE WATER DISTRICT", "-48.12"),
            row(28, "withdrawal", "CARD PURCHASE PLACEHOLDER COFFEE", "-5.25"),
            row(29, "withdrawal", "ONLINE BILL PAY DEMO CARD SERVICES", "-400.00"),
            row(29, "fee", "MONTHLY SERVICE FEE", "-12.00"),
            row(8, "withdrawal", "CHECK 1201", "-125.00", "checks"),
            row(20, "withdrawal", "CHECK 1202", "-310.00", "checks"),
        ),
    )
    savings = Account(
        number="XXXX-5530",
        title="Statement Savings",
        kind="bank",
        subtype="savings",
        cash_opening=Decimal("10000.00"),
        activity=(
            row(5, "deposit", "ONLINE TRANSFER FROM CHECKING XXXX-4821", "300.00"),
            row(31, "interest", "INTEREST PAYMENT", "8.27"),
        ),
    )
    brokerage = Account(
        number="XXXX-6619",
        title="Self-Directed Brokerage",
        kind="investment",
        subtype="brokerage",
        cash_opening=Decimal("1250.00"),
        core_name="Cash & Sweep Balance",
        positions=(
            Position(
                "SAMPLE QUARTZ RESOURCES INC",
                "Stocks",
                Decimal("120.000"),
                Decimal("45.1200"),
                Decimal("4400.00"),
                symbol="ZZQRS",
                price_places=4,
            ),
            Position(
                "EXAMPLE LUMEN NETWORKS CORP",
                "Stocks",
                Decimal("40.000"),
                Decimal("21.3500"),
                Decimal("1025.00"),
                symbol="ZZLMN",
                price_places=4,
            ),
            Position(
                "PLACEHOLDER TOTAL INDEX FUND",
                "Funds",
                Decimal("80.582"),
                Decimal("31.7000"),
                Decimal("2500.00"),
                symbol="ZZIDX",
                price_places=4,
            ),
        ),
        activity=(
            Activity(
                d(5),
                "buy",
                "trades",
                "Bought",
                Decimal("-902.40"),
                name="SAMPLE QUARTZ RESOURCES INC",
                symbol="ZZQRS",
                quantity=Decimal(20),
                price=Decimal("45.1200"),
            ),
            Activity(
                d(12),
                "sell",
                "trades",
                "Sold",
                Decimal("205.05"),
                name="EXAMPLE LUMEN NETWORKS CORP",
                symbol="ZZLMN",
                quantity=Decimal(10),
                price=Decimal("21.0000"),
                cost=Decimal("4.95"),
            ),
            Activity(
                d(15),
                "dividend",
                "trades",
                "Dividend",
                Decimal("18.40"),
                name="PLACEHOLDER TOTAL INDEX FUND",
                symbol="ZZIDX",
            ),
            Activity(
                d(15),
                "reinvest",
                "trades",
                "Reinvest",
                Decimal("-18.40"),
                name="PLACEHOLDER TOTAL INDEX FUND",
                symbol="ZZIDX",
                quantity=Decimal("0.582"),
                price=Decimal("31.6200"),
            ),
            Activity(d(14), "deposit", "cash", "Transfer from Checking XXXX-4821", Decimal(1000)),
            Activity(d(29), "fee", "cash", "Advisory Fee", Decimal("-15.00")),
            Activity(d(31), "interest", "cash", "Interest on Cash Balance", Decimal("1.12")),
        ),
    )
    accounts = (checking, savings, brokerage)
    for account in accounts:
        check_account(account)
    return Report(
        INSTITUTION,
        START,
        END,
        accounts,
        owner="ALEX R. PLACEHOLDER",
        address=("42 Sample Street Apt 3", "Exampleville, ZZ 99999"),
    )


@dataclass(frozen=True)
class Expected:
    """What a correct parser returns for one account (bank or investment)."""

    last4: str
    kind: str
    subtype: str
    period_start: date
    period_end: date
    opening: Decimal | None
    closing: Decimal | None
    #: Bank accounts: ``(date, amount, description)`` in print order.
    bank_rows: list[tuple[date, Decimal, str]]
    #: Investment accounts (empty for a bank account).
    positions: list[tuple[str, Decimal, Decimal, Decimal]]
    activity: list[tuple[date, str, str | None, Decimal | None, Decimal]]
    positions_value: Decimal
    total_value: Decimal


def expected(stmt: Report) -> list[Expected]:
    """Per account, in print order (module doc)."""
    out: list[Expected] = []
    for a in stmt.accounts:
        bank = a.kind == "bank"
        out.append(
            Expected(
                last4=a.last4,
                kind=a.kind,
                subtype=a.subtype,
                period_start=stmt.start,
                period_end=stmt.end,
                opening=a.cash_opening,
                closing=a.cash_closing,
                bank_rows=_bank_print_order(a) if bank else [],
                positions=[] if bank else expected_positions(a),
                activity=[] if bank else expected_activity(a),
                positions_value=a.holdings_value,
                total_value=a.total_value,
            )
        )
    return out


def _bank_print_order(account: Account) -> list[tuple[date, Decimal, str]]:
    """Transaction Detail rows first (as printed), then Checks Paid."""
    rows = expected_bank_rows(account)
    detail = [r for r, a in zip(rows, account.activity, strict=True) if a.section != "checks"]
    checks = [r for r, a in zip(rows, account.activity, strict=True) if a.section == "checks"]
    return detail + checks


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def _total(rows: list[Activity]) -> Decimal:
    return sum((a.amount for a in rows), ZERO)


class _Layout:
    def __init__(self, stmt: Report) -> None:
        self.stmt = stmt

    def chrome(self, c: canvas.Canvas, page: int, total: int) -> float:
        c.setFont(BOLD, 13)
        c.drawString(40, 762, INSTITUTION.upper())
        c.setFont(FONT, 9)
        c.drawString(40, 748, "Combined Statement")
        c.drawRightString(572, 762, _period_text(self.stmt.start, self.stmt.end))
        c.setFont(FONT, 7)
        c.drawString(40, 36, DISCLAIMER)
        c.drawRightString(572, 36, f"Page {page} of {total}")
        c.setLineWidth(0.6)
        c.line(40, 740, 572, 740)
        return 722.0

    def build(self) -> list[Page]:
        checking, savings, brokerage = self.stmt.accounts
        return [
            self.overview,
            lambda cur: self.checking_first(cur, checking),
            lambda cur: self.checking_rest(cur, checking),
            lambda cur: self.savings_and_brokerage(cur, savings, brokerage),
            lambda cur: self.brokerage_detail(cur, brokerage),
            self.disclosures,
        ]

    @staticmethod
    def heading(cur: Cursor, account: Account) -> None:
        cur.line({40: account.title}, font=BOLD, size=12, gap=15)
        cur.line({40: f"Account Number: {account.number}"}, size=9, gap=16)

    def overview(self, cur: Cursor) -> None:
        stmt = self.stmt
        cur.line({40: stmt.owner}, size=9)
        for line in stmt.address:
            cur.line({40: line}, size=9)
        cur.skip(20)
        cur.heading("Your Accounts at a Glance", size=11)
        cur.line(
            {40: "Account Name", 220: "Account Number"},
            {470: "Opening Balance", 572: "Closing Balance"},
            font=BOLD,
            size=8,
        )
        opening = closing = ZERO
        for account in stmt.accounts:
            first = account.begin_value
            last = account.total_value
            opening += first
            closing += last
            cur.line(
                {40: account.title, 220: account.number},
                {470: money(first, dollar=True), 572: money(last, dollar=True)},
                size=8,
            )
        cur.line(
            {40: "Total"},
            {470: money(opening, dollar=True), 572: money(closing, dollar=True)},
            font=BOLD,
            size=8,
        )
        cur.skip(24)
        cur.line({40: "Thank you for banking with us. This page is filler text of a synthetic"})
        cur.line({40: "test statement; no figure on it belongs to a real person or account."})

    def _bank_summary(self, cur: Cursor, account: Account) -> None:
        assert account.cash_opening is not None
        assert account.cash_closing is not None
        detail = account.section("detail")
        credits = sum((a.amount for a in detail if a.amount > 0), ZERO)
        debits = sum((a.amount for a in detail if a.amount < 0), ZERO)
        checks = _total(account.section("checks"))
        start, end = self.stmt.start, self.stmt.end
        cur.heading("Account Summary", size=9)
        rows = [
            (f"Beginning Balance on {start:%m/%d/%Y}", money(account.cash_opening, dollar=True)),
            ("Deposits and Other Credits", money(credits, dollar=True, plus=True)),
            ("Withdrawals and Other Debits", money(debits, dollar=True)),
        ]
        if account.section("checks"):
            rows.append(("Checks Paid", money(checks, dollar=True)))
        rows.append((f"Ending Balance on {end:%m/%d/%Y}", money(account.cash_closing, dollar=True)))
        for label, value in rows:
            cur.line({40: label}, {300: value}, size=8)
        cur.skip()

    @staticmethod
    def _detail_header(cur: Cursor, title: str) -> None:
        cur.heading(title, size=9)
        cur.line({40: "Date", 90: "Description"}, {572: "Amount"}, font=BOLD, size=8)

    @staticmethod
    def _detail_rows(cur: Cursor, rows: list[Activity]) -> None:
        for a in rows:
            cur.line({40: f"{a.day:%m/%d}", 90: a.description}, {572: money(a.amount)}, size=8)

    def checking_first(self, cur: Cursor, account: Account) -> None:
        self.heading(cur, account)
        self._bank_summary(cur, account)
        self._detail_header(cur, "Transaction Detail")
        self._detail_rows(cur, account.section("detail")[:FIRST_PAGE_ROWS])

    def checking_rest(self, cur: Cursor, account: Account) -> None:
        detail = account.section("detail")
        self._detail_header(cur, "Transaction Detail (continued)")
        self._detail_rows(cur, detail[FIRST_PAGE_ROWS:])
        cur.line({90: "Total Transaction Detail"}, {572: money(_total(detail))}, font=BOLD, size=8)
        cur.skip()
        checks = account.section("checks")
        cur.heading("Checks Paid", size=9)
        cur.line({40: "Check No.", 110: "Date Paid"}, {300: "Amount"}, font=BOLD, size=8)
        for a in checks:
            cur.line(
                {40: a.description.split()[-1], 110: f"{a.day:%m/%d}"},
                {300: money(-a.amount)},
                size=8,
            )
        cur.line({40: "Total Checks Paid"}, {300: money(-_total(checks))}, font=BOLD, size=8)
        cur.skip()
        self._daily_balances(cur, account)

    def _daily_balances(self, cur: Cursor, account: Account) -> None:
        assert account.cash_opening is not None
        balance = account.cash_opening
        by_day: dict[date, Decimal] = {}
        for a in sorted(account.activity, key=lambda a: a.day):
            balance += a.amount
            by_day[a.day] = balance
        cur.heading("Daily Ending Balance", size=9)
        cur.line(
            {40: "Date", 220: "Date", 400: "Date"},
            {170: "Balance", 350: "Balance", 530: "Balance"},
            font=BOLD,
            size=8,
        )
        days = sorted(by_day)
        third = (len(days) + 2) // 3
        for index in range(third):
            left: dict[float, str] = {}
            right: dict[float, str] = {}
            for column, x in enumerate((40.0, 220.0, 400.0)):
                position = index + column * third
                if position < len(days):
                    day = days[position]
                    left[x] = f"{day:%m/%d}"
                    right[x + 130] = money(by_day[day])
            cur.line(left, right, size=8)

    def savings_and_brokerage(self, cur: Cursor, savings: Account, brokerage: Account) -> None:
        self.heading(cur, savings)
        self._bank_summary(cur, savings)
        self._detail_header(cur, "Transaction Detail")
        self._detail_rows(cur, savings.section("detail"))
        cur.skip(24)
        self.heading(cur, brokerage)
        assert brokerage.cash_opening is not None
        assert brokerage.cash_closing is not None
        cur.heading("Account Value Summary", size=9)
        cur.line({}, {380: "This Period", 480: "Last Period"}, font=BOLD, size=8)
        for label, now, before in (
            (
                "Securities",
                brokerage.holdings_value,
                brokerage.begin_value - brokerage.cash_opening,
            ),
            ("Cash & Sweep Balance", brokerage.cash_closing, brokerage.cash_opening),
            ("Total Account Value", brokerage.total_value, brokerage.begin_value),
        ):
            cur.line({40: label}, {380: money(now, dollar=True), 480: money(before, dollar=True)})

    def brokerage_detail(self, cur: Cursor, account: Account) -> None:
        assert account.cash_closing is not None
        cur.heading("Portfolio Holdings", size=9)
        cur.line(
            {40: "Symbol", 100: "Description"},
            {400: "Quantity", 470: "Price", 572: "Market Value"},
            font=BOLD,
            size=8,
        )
        for p in account.positions:
            cur.line(
                {40: p.key, 100: p.name},
                {
                    400: number(p.quantity, 3),
                    470: number(p.price, p.price_places),
                    572: money(p.market_value),
                },
                size=8,
            )
        cur.line(
            {40: "Total Securities"}, {572: money(account.holdings_value, dollar=True)}, size=8
        )
        cur.line(
            {40: "Cash & Sweep Balance"}, {572: money(account.cash_closing, dollar=True)}, size=8
        )
        cur.line(
            {40: "Total Account Value"},
            {572: money(account.total_value, dollar=True)},
            font=BOLD,
            size=8,
        )
        cur.skip()
        cur.heading("Trade Activity", size=9)
        cur.line(
            {40: "Trade Date", 96: "Activity", 146: "Symbol", 190: "Description"},
            {390: "Units", 446: "Unit Price", 504: "Commission", 572: "Net Amount"},
            font=BOLD,
            size=7,
        )
        for a in account.section("trades"):
            right = {572: money(a.amount)}
            if a.quantity is not None:
                right[390] = number(a.quantity, 3)
            if a.price is not None:
                right[446] = number(a.price, 4)
            right[504] = money(a.cost) if a.cost else ""
            cur.line(
                {40: f"{a.day:%m/%d/%Y}", 96: a.description, 146: a.ident or "", 190: a.name},
                right,
                size=7,
            )
        cur.skip()
        cash = account.section("cash")
        cur.heading("Cash Activity", size=9)
        cur.line({40: "Date", 100: "Description"}, {572: "Amount"}, font=BOLD, size=8)
        for a in cash:
            cur.line({40: f"{a.day:%m/%d/%Y}", 100: a.description}, {572: money(a.amount)}, size=8)
        cur.line({40: "Total Cash Activity"}, {572: money(_total(cash))}, font=BOLD, size=8)

    def disclosures(self, cur: Cursor) -> None:
        cur.heading("Important Information", size=10)
        for text in (
            "In case of errors or questions about your electronic transfers, contact us as",
            "soon as you can. This disclosure is filler text of a synthetic test statement.",
            "Securities are not deposits and may lose value. Balances shown are as of the",
            "end of the statement period.",
        ):
            cur.line({40: text}, size=8)


def report_pdf(stmt: Report | None = None) -> bytes:
    """The combined statement as PDF bytes (module doc)."""
    stmt = report() if stmt is None else stmt
    layout = _Layout(stmt)
    return render(
        layout.build(),
        layout.chrome,
        title="Combined Statement",
        author=INSTITUTION,
        subject="Synthetic test statement",
    )

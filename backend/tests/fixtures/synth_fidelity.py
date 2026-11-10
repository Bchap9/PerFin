# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic multi-account statements in the Fidelity Investments "Investment Report" layout.

Nothing here comes from a real statement. The page layout is redrawn by
code (reportlab, like ``synth_brokerage.py``) from the headings and column
names the layout is known to use; the accounts, account numbers,
instruments (symbols starting ``ZZ``, names made of placeholder words),
quantities, prices and amounts are invented from a seed, and every page
says it is a synthetic test document. The institution's name is printed
only because the layout's fingerprint is that name.

* :func:`fidelity_statement` builds one month for three accounts
  (``INDIVIDUAL - TOD``, ``ROTH IRA``, ``HEALTH SAVINGS ACCOUNT``) whose
  figures reconcile: positions at month-end prices, the core money market
  position as each account's cash (opening + the month's activity =
  closing), holdings + core = the ending account value.
* :func:`fidelity_pdf` draws it: page 1 an overview of the accounts (rows
  with amounts, so no account heading), then per account pages with the
  account title and ``Account # ...`` at the top of every page, an
  *Account Summary*, *Holdings* by asset class (*Core Account*, *Mutual
  Funds*, *Exchange Traded Products*, *Stocks*) with ``Total <group>``
  subtotals and ``Total Holdings``, the holdings header over two lines with
  the dates under the market values, wrapped security names, and *Activity*
  tables (*Securities Bought & Sold*, *Dividends, Interest & Other Income*,
  *Deposits*, *Withdrawals*, *Fees and Charges*, *Other Activity In*,
  *Core Fund Activity*) with the layout's action words ("You Bought",
  "Dividend Received", "Reinvestment", "Long-Term Cap Gain", "Electronic
  Funds Transfer Received", "Transferred From", ...). Pages break inside
  tables with the header repeated (``page_bottom`` moves the break up to
  force more of them); ``ruled=True`` rules every table line (a grid
  PyMuPDF's table finder sees). ``generic=True`` draws the same
  accounts under an invented broker's name with full ``MM/DD/YYYY`` dates
  and a ``Statement period`` label and without the core fund table (for
  the generic parser).
* Tampering (SECURITY.md C12) of one account (``tamper_account``):
  ``"total"`` misprints its ending account value, ``"market_value"`` one
  position's market value, ``"extra_row"`` adds a buy its cash does not
  reflect, ``"misprinted_row"`` prints its deposit :data:`MISPRINT` too
  high while the *Deposits* total and the cash stay what they were (one
  line of one section that does not add up).

:func:`expected_activity` / :func:`expected_positions` give what the
parser must return for an account.
"""

from __future__ import annotations

import io
import random
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final, Literal

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

INSTITUTION: Final = "Fidelity Investments"
INSTITUTION_KEY: Final = "fidelity-investments-statement"
GENERIC_INSTITUTION: Final = "Riverbend Test Investments"
CENT: Final = Decimal("0.01")
FONT = "Helvetica"
BOLD = "Helvetica-Bold"
DISCLAIMER: Final = "Synthetic test document from the PerFin test suite. Not a real statement."

Tamper = Literal["total", "market_value", "extra_row", "misprinted_row"] | None
Table = Literal["trades", "income", "deposits", "withdrawals", "fees", "other_in"]
Type = Literal[
    "buy",
    "sell",
    "dividend",
    "interest",
    "fee",
    "reinvest",
    "deposit",
    "withdrawal",
    "transfer_in",
]


@dataclass(frozen=True)
class FidSecurity:
    symbol: str
    name: str
    group: str


#: Invented instruments (symbols and names are made up).
SECURITIES: Final = (
    FidSecurity("ZZFTX", "SAMPLE TOTAL MARKET INDEX FUND", "Mutual Funds"),
    FidSecurity("ZZBDX", "EXAMPLE US BOND INDEX FUND", "Mutual Funds"),
    FidSecurity("ZZGRX", "PLACEHOLDER GROWTH COMPANY FUND", "Mutual Funds"),
    FidSecurity("ZZETF", "MOCK BROAD MARKET ETF", "Exchange Traded Products"),
    FidSecurity("ZZIN", "DEMO INDUSTRIALS INC", "Stocks"),
    FidSecurity("ZZTEC", "FICTIONAL TECHNOLOGY CORP", "Stocks"),
)
CORE: Final = FidSecurity("ZZCOR", "FICTIONAL GOVERNMENT MONEY MARKET", "Core Account")
BY_SYMBOL: Final = {s.symbol: s for s in (*SECURITIES, CORE)}
GROUPS: Final = ("Mutual Funds", "Exchange Traded Products", "Stocks")


@dataclass(frozen=True)
class FidPosition:
    symbol: str
    quantity: Decimal
    price: Decimal
    begin_value: Decimal
    cost: Decimal

    @property
    def market_value(self) -> Decimal:
        return (self.quantity * self.price).quantize(CENT, rounding=ROUND_HALF_EVEN)

    @property
    def security(self) -> FidSecurity:
        return BY_SYMBOL[self.symbol]


@dataclass(frozen=True)
class FidActivity:
    day: date
    type: Type
    action: str
    amount: Decimal
    table: Table
    symbol: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    reference: str = ""


@dataclass(frozen=True)
class FidAccount:
    title: str
    number: str
    positions: tuple[FidPosition, ...]
    activity: tuple[FidActivity, ...]
    core_opening: Decimal

    @property
    def last4(self) -> str:
        return "".join(c for c in self.number if c.isdigit())[-4:]

    @property
    def core_closing(self) -> Decimal:
        return self.core_opening + sum((a.amount for a in self.activity), Decimal(0))

    @property
    def holdings_value(self) -> Decimal:
        return sum((p.market_value for p in self.positions), Decimal(0))

    @property
    def begin_value(self) -> Decimal:
        return sum((p.begin_value for p in self.positions), Decimal(0)) + self.core_opening

    @property
    def total_value(self) -> Decimal:
        return self.holdings_value + self.core_closing


@dataclass(frozen=True)
class FidStatement:
    start: date
    end: date
    accounts: tuple[FidAccount, ...]


def _price(rng: random.Random, low: int, high: int) -> Decimal:
    return Decimal(rng.randrange(low * 10_000, high * 10_000)) / 10_000


def _money(rng: random.Random, low: int, high: int) -> Decimal:
    return Decimal(rng.randrange(low * 100, high * 100)) / 100


def _value(quantity: Decimal, price: Decimal) -> Decimal:
    return (quantity * price).quantize(CENT, rounding=ROUND_HALF_EVEN)


class _Builder:
    """One account's month: starting holdings, activity, ending positions."""

    def __init__(self, rng: random.Random, start: date, symbols: tuple[str, ...]) -> None:
        self.rng = rng
        self.start = start
        self.begin_price = {s: _price(rng, 15, 220) for s in symbols}
        self.end_price = {
            s: (p * Decimal(rng.randrange(94, 107)) / 100).quantize(Decimal("0.0001"))
            for s, p in self.begin_price.items()
        }
        self.start_qty = {s: Decimal(rng.randrange(20_000, 600_000)) / 1000 for s in symbols}
        self.qty = dict(self.start_qty)
        self.cost = {
            s: (q * self.begin_price[s] * Decimal("0.85")).quantize(CENT)
            for s, q in self.start_qty.items()
        }
        self.rows: list[FidActivity] = []

    def day(self, offset: int) -> date:
        return self.start + timedelta(days=offset)

    def trade(self, offset: int, symbol: str, kind: Literal["buy", "sell"], qty: Decimal) -> None:
        price = (self.begin_price[symbol] * Decimal(self.rng.randrange(97, 104)) / 100).quantize(
            Decimal("0.0001")
        )
        value = _value(qty, price)
        if kind == "buy":
            self.rows.append(
                FidActivity(
                    self.day(offset), "buy", "You Bought", -value, "trades", symbol, qty, price
                )
            )
            self.qty[symbol] += qty
            self.cost[symbol] += value
        else:
            self.rows.append(
                FidActivity(
                    self.day(offset), "sell", "You Sold", value, "trades", symbol, qty, price
                )
            )
            self.cost[symbol] = (
                self.cost[symbol] * (self.qty[symbol] - qty) / self.qty[symbol]
            ).quantize(CENT)
            self.qty[symbol] -= qty

    def income(self, offset: int, symbol: str, action: str, kind: Type, amount: Decimal) -> None:
        self.rows.append(FidActivity(self.day(offset), kind, action, amount, "income", symbol))

    def reinvest(self, offset: int, symbol: str, amount: Decimal) -> None:
        price = self.end_price[symbol]
        qty = (amount / price).quantize(Decimal("0.001"))
        # The reinvestment buys whole thousandths; its amount is what they cost.
        cost = _value(qty, price)
        self.rows.append(
            FidActivity(
                self.day(offset), "reinvest", "Reinvestment", -cost, "income", symbol, qty, price
            )
        )
        self.qty[symbol] += qty
        self.cost[symbol] += cost

    def cash(self, offset: int, kind: Type, action: str, amount: Decimal, table: Table) -> None:
        ref = f"REF{self.rng.randrange(10_000, 99_999)}"
        self.rows.append(FidActivity(self.day(offset), kind, action, amount, table, reference=ref))

    def transfer_in(self, offset: int, symbol: str, qty: Decimal) -> None:
        self.rows.append(
            FidActivity(
                self.day(offset),
                "transfer_in",
                "Transferred From",
                Decimal(0),
                "other_in",
                symbol,
                qty,
            )
        )
        self.cost[symbol] += _value(qty, self.begin_price[symbol])
        self.qty[symbol] += qty

    def account(self, title: str, number: str, core_opening: Decimal) -> FidAccount:
        positions = tuple(
            FidPosition(
                s,
                q,
                self.end_price[s],
                _value(self.start_qty[s], self.begin_price[s]),
                self.cost[s],
            )
            for s, q in self.qty.items()
        )
        return FidAccount(
            title,
            number,
            positions,
            tuple(sorted(self.rows, key=lambda a: a.day)),
            core_opening,
        )


def _month_end(start: date) -> date:
    following = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return following - timedelta(days=1)


def fidelity_statement(seed: int = 81, *, start: date = date(2026, 10, 1)) -> FidStatement:
    """Three accounts for one month (module doc); deterministic for a seed."""
    rng = random.Random(seed)
    end = _month_end(start)
    accounts: list[FidAccount] = []

    brokerage = _Builder(rng, start, ("ZZFTX", "ZZBDX", "ZZETF", "ZZIN"))
    brokerage.trade(2, "ZZFTX", "buy", Decimal(rng.randrange(5, 30)))
    brokerage.trade(8, "ZZIN", "sell", Decimal(rng.randrange(2, 9)))
    brokerage.cash(
        5, "deposit", "Electronic Funds Transfer Received", _money(rng, 200, 900), "deposits"
    )
    brokerage.income(14, "ZZIN", "Dividend Received", "dividend", _money(rng, 5, 60))
    bond_dividend = _money(rng, 10, 80)
    brokerage.income(14, "ZZBDX", "Dividend Received", "dividend", bond_dividend)
    brokerage.reinvest(14, "ZZBDX", bond_dividend)
    brokerage.income(19, "ZZETF", "Foreign Tax Paid", "fee", -_money(rng, 1, 4))
    brokerage.cash(
        21, "withdrawal", "Electronic Funds Transfer Paid", -_money(rng, 50, 300), "withdrawals"
    )
    brokerage.income(30, CORE.symbol, "Dividend Received", "dividend", _money(rng, 1, 9))
    accounts.append(brokerage.account("INDIVIDUAL - TOD", "Z29-104821", _money(rng, 3000, 9000)))

    roth = _Builder(rng, start, ("ZZGRX", "ZZETF", "ZZTEC"))
    roth.cash(1, "deposit", "Transferred From", _money(rng, 500, 1500), "deposits")
    roth.trade(4, "ZZETF", "buy", Decimal(rng.randrange(3, 12)))
    long_term = _money(rng, 20, 120)
    short_term = _money(rng, 5, 40)
    roth.income(16, "ZZGRX", "Long-Term Cap Gain", "dividend", long_term)
    roth.income(16, "ZZGRX", "Short-Term Cap Gain", "dividend", short_term)
    roth.reinvest(16, "ZZGRX", long_term + short_term)
    roth.cash(30, "fee", "Fee Charged", Decimal("-25.00"), "fees")
    accounts.append(roth.account("ROTH IRA", "245-718293", _money(rng, 1500, 5000)))

    hsa = _Builder(rng, start, ("ZZFTX", "ZZBDX"))
    hsa.cash(0, "deposit", "Direct Deposit", _money(rng, 100, 400), "deposits")
    hsa.trade(11, "ZZFTX", "buy", Decimal(rng.randrange(2, 8)))
    hsa.cash(19, "withdrawal", "Transferred To", -_money(rng, 40, 120), "withdrawals")
    hsa.transfer_in(23, "ZZBDX", Decimal("3.000"))
    hsa.income(30, CORE.symbol, "Interest Earned", "interest", _money(rng, 0, 2) + CENT)
    accounts.append(hsa.account("HEALTH SAVINGS ACCOUNT", "246-556074", _money(rng, 800, 2500)))
    return FidStatement(start, end, tuple(accounts))


#: The buy a ``tamper="extra_row"`` account prints but its cash does not reflect.
FORGED: Final = FidActivity(
    date(2026, 10, 18),
    "buy",
    "You Bought",
    Decimal("-777.77"),
    "trades",
    "ZZETF",
    Decimal(7),
    Decimal("111.1100"),
)


#: What a ``tamper="misprinted_row"`` account's deposit prints too much.
MISPRINT: Final = Decimal("30.00")


def _printed_activity(account: FidAccount, tamper: Tamper) -> list[FidActivity]:
    rows = list(account.activity)
    if tamper == "extra_row":
        rows = sorted([*rows, FORGED], key=lambda a: a.day)
    if tamper == "misprinted_row":
        index = next(i for i, a in enumerate(rows) if a.table == "deposits")
        rows[index] = replace(rows[index], amount=rows[index].amount + MISPRINT)
    return rows


def expected_activity(
    account: FidAccount, *, tamper: Tamper = None
) -> list[tuple[date, str, str | None, Decimal | None, Decimal]]:
    """``(trade date, type, symbol, quantity, amount)`` a parser must return, sorted."""
    rows = [
        (a.day, a.type, a.symbol, a.quantity, a.amount) for a in _printed_activity(account, tamper)
    ]
    return sorted(rows, key=lambda r: (r[0], r[1], r[2] or "", r[4]))


def expected_positions(account: FidAccount) -> list[tuple[str, Decimal, Decimal, Decimal]]:
    """``(symbol, quantity, price, market value)`` of each position (not the core), sorted."""
    return sorted((p.symbol, p.quantity, p.price, p.market_value) for p in account.positions)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _money_text(value: Decimal, *, symbol: str = "$") -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(value):,.2f}"


def _qty_text(value: Decimal) -> str:
    return f"{value:,.3f}"


def _long(day: date) -> str:
    return f"{day:%B} {day.day}, {day.year}"


def _short(day: date) -> str:
    return f"{day:%b} {day.day}, {day.year}"


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


# Column anchors (x of the left edge for text columns, of the right edge for numbers).
H_DESC, H_BEGIN, H_QTY, H_PRICE, H_END, H_COST, H_GAIN, H_EAI = (
    40,
    232,
    284,
    336,
    400,
    458,
    516,
    572,
)
A_DATE, A_NAME, A_SYMBOL, A_DESC, A_QTY, A_PRICE, A_COST, A_AMOUNT = (
    40,
    86,
    218,
    268,
    398,
    450,
    506,
    572,
)
D_DATE, D_REF, D_DESC, D_AMOUNT = 40, 90, 200, 572
ROW = 10.0
BOTTOM = 70.0


class _Writer:
    """Draws flowing lines over pages; a table header is repeated after a page break."""

    def __init__(
        self, stmt: FidStatement, *, generic: bool, bottom: float = BOTTOM, ruled: bool = False
    ) -> None:
        self.stmt = stmt
        self.generic = generic
        self.bottom = bottom
        self.ruled = ruled
        self.buffer = io.BytesIO()
        self.c = canvas.Canvas(self.buffer, pagesize=letter)
        self.c.setTitle("Statement")
        self.page = 0
        self.y = 0.0
        self.account: FidAccount | None = None
        self.header: Callable[[], None] | None = None

    # -- pages --------------------------------------------------------------

    def date_text(self, day: date) -> str:
        return day.strftime("%m/%d/%Y") if self.generic else day.strftime("%m/%d")

    def new_page(self, account: FidAccount | None) -> None:
        if self.page:
            self._footer()
            self.c.showPage()
        self.page += 1
        self.account = account
        c = self.c
        c.setFont(BOLD, 14)
        c.drawString(40, 760, GENERIC_INSTITUTION if self.generic else INSTITUTION)
        c.setFont(FONT, 9)
        if self.generic:
            c.drawString(40, 744, "Investment Statement")
            period = f"{self.stmt.start:%m/%d/%Y} - {self.stmt.end:%m/%d/%Y}"
            c.drawString(300, 744, f"Statement period: {period}")
        else:
            c.drawString(40, 744, "INVESTMENT REPORT")
            c.drawString(300, 744, f"{_long(self.stmt.start)} - {_long(self.stmt.end)}")
        self.y = 724.0
        if account is not None:
            c.setFont(BOLD, 10)
            c.drawString(40, self.y, account.title)
            c.setFont(FONT, 9)
            c.drawString(400, self.y, f"Account # {account.number}")
            self.y -= 20

    def _footer(self) -> None:
        self.c.setFont(FONT, 7)
        self.c.drawString(40, 40, f"Page {self.page}")
        self.c.drawString(40, 30, DISCLAIMER)

    def need(self, lines: int = 1) -> None:
        if self.y - lines * ROW < self.bottom:
            self.new_page(self.account)
            if self.header is not None:
                self.header()

    def bold(self, text: str, size: float = 9) -> None:
        self.need(2)
        self.y -= 4
        self.c.setFont(BOLD, size)
        self.c.drawString(40, self.y, text)
        self.c.setFont(FONT, 7)
        self.y -= ROW + 2

    def finish(self) -> bytes:
        self._footer()
        self.c.save()
        return self.buffer.getvalue()

    # -- drawing helpers ------------------------------------------------------

    def cells(self, left: dict[int, str], right: dict[int, str], *, font: str = FONT) -> None:
        self.c.setFont(font, 7)
        for x, text in left.items():
            if text:
                self.c.drawString(x, self.y, text)
        for x, text in right.items():
            if text:
                self.c.drawRightString(x, self.y, text)
        if self.ruled:
            # Every printed line a row of a grid: rules above and below, five column rules.
            top, bottom = self.y + ROW - 2.5, self.y - 2.5
            self.c.setLineWidth(0.4)
            self.c.line(36, top, 576, top)
            self.c.line(36, bottom, 576, bottom)
            for x in (36, 200, 300, 420, 576):
                self.c.line(x, top, x, bottom)
        self.y -= ROW


def _overview(w: _Writer, accounts: tuple[FidAccount, ...]) -> None:
    w.new_page(None)
    c = w.c
    total = sum((a.total_value for a in accounts), Decimal(0))
    c.setFont(BOLD, 11)
    c.drawString(40, w.y, "Your Portfolio Value")
    c.drawRightString(300, w.y, _money_text(total))
    w.y -= 24
    w.bold("Accounts Included in This Report")
    w.cells(
        {40: "Account Type", 220: "Account Number"},
        {420: "Beginning Value", 520: "Ending Value"},
        font=BOLD,
    )
    for account in accounts:
        w.cells(
            {40: account.title, 220: account.number},
            {420: _money_text(account.begin_value), 520: _money_text(account.total_value)},
        )
    w.cells({40: "Total Portfolio Value"}, {520: _money_text(total)})


def _summary(w: _Writer, account: FidAccount, total: Decimal) -> None:
    additions = sum((a.amount for a in account.activity if a.type == "deposit"), Decimal(0))
    subtractions = sum(
        (
            a.amount
            for a in account.activity
            if a.type in ("withdrawal", "fee") and a.table != "income"
        ),
        Decimal(0),
    )
    change = total - account.begin_value - additions - subtractions
    w.bold("Account Summary")
    for label, value in (
        ("Beginning Account Value", account.begin_value),
        ("Additions", additions),
        ("Subtractions", subtractions),
        ("Change in Investment Value", change),
        ("Ending Account Value", total),
    ):
        w.cells({40: label}, {300: _money_text(value)})
    w.y -= 6


def _holdings_header(w: _Writer) -> None:
    stmt = w.stmt
    w.cells(
        {H_DESC: "Description"},
        {
            H_BEGIN: "Beginning",
            H_QTY: "Quantity",
            H_PRICE: "Price",
            H_END: "Ending",
            H_COST: "Total",
            H_GAIN: "Unrealized",
            H_EAI: "EAI ($) /",
        },
        font=BOLD,
    )
    w.cells(
        {},
        {
            H_BEGIN: "Market Value",
            H_PRICE: "Per Unit",
            H_END: "Market Value",
            H_COST: "Cost Basis",
            H_GAIN: "Gain/Loss",
            H_EAI: "EY (%)",
        },
        font=BOLD,
    )
    w.cells({}, {H_BEGIN: _short(stmt.start), H_END: _short(stmt.end)}, font=BOLD)


def _holding_row(
    w: _Writer,
    security: FidSecurity,
    begin: Decimal,
    quantity: Decimal,
    price: Decimal,
    value: Decimal,
    cost: Decimal | None,
) -> None:
    name = _wrap(f"{security.name} ({security.symbol})", 24)
    w.need(len(name))
    gain = "not applicable" if cost is None else _money_text(value - cost)
    w.cells(
        {H_DESC: name[0]},
        {
            H_BEGIN: _money_text(begin),
            H_QTY: _qty_text(quantity),
            H_PRICE: f"${price:,.4f}",
            H_END: _money_text(value),
            H_COST: "not applicable" if cost is None else _money_text(cost),
            H_GAIN: gain,
            H_EAI: "-",
        },
    )
    for extra in name[1:]:
        w.cells({H_DESC: extra}, {})


def _holdings(w: _Writer, account: FidAccount, values: list[Decimal]) -> Decimal:
    w.bold("Holdings")
    w.header = lambda: _holdings_header(w)
    _holdings_header(w)
    core_qty = account.core_closing
    w.bold("Core Account", 8)
    _holding_row(w, CORE, account.core_opening, core_qty, Decimal("1.0000"), core_qty, None)
    w.cells(
        {H_DESC: "Total Core Account"},
        {H_BEGIN: _money_text(account.core_opening), H_END: _money_text(core_qty)},
    )
    total_begin = account.core_opening
    total_end = core_qty
    for group in GROUPS:
        rows = [
            (p, v)
            for p, v in zip(account.positions, values, strict=True)
            if p.security.group == group
        ]
        if not rows:
            continue
        w.bold(group, 8)
        for position, value in rows:
            _holding_row(
                w,
                position.security,
                position.begin_value,
                position.quantity,
                position.price,
                value,
                position.cost,
            )
        begin = sum((p.begin_value for p, _ in rows), Decimal(0))
        end = sum((v for _, v in rows), Decimal(0))
        cost = sum((p.cost for p, _ in rows), Decimal(0))
        w.cells(
            {H_DESC: f"Total {group}"},
            {H_BEGIN: _money_text(begin), H_END: _money_text(end), H_COST: _money_text(cost)},
        )
        total_begin += begin
        total_end += end
    w.cells(
        {H_DESC: "Total Holdings"},
        {H_BEGIN: _money_text(total_begin), H_END: _money_text(total_end)},
        font=BOLD,
    )
    w.header = None
    w.y -= 6
    return total_end


def _security_header(w: _Writer, *, cost: bool) -> None:
    right = {A_QTY: "Quantity", A_PRICE: "Price", A_AMOUNT: "Amount"}
    if cost:
        right[A_COST] = "Transaction"
    w.cells(
        {A_DATE: "Settlement", A_NAME: "Security Name", A_SYMBOL: "Symbol/", A_DESC: "Description"},
        right,
        font=BOLD,
    )
    w.cells({A_DATE: "Date", A_SYMBOL: "CUSIP"}, {A_COST: "Cost"} if cost else {}, font=BOLD)


def _security_rows(w: _Writer, rows: list[FidActivity], *, cost: bool) -> None:
    for a in rows:
        w.need()
        name = BY_SYMBOL[a.symbol].name[:26] if a.symbol else ""
        right = {
            A_QTY: (
                ""
                if a.quantity is None
                else _qty_text(-a.quantity if a.type == "sell" else a.quantity)
            ),
            A_PRICE: "-" if a.price is None else f"${a.price:,.4f}",
            A_AMOUNT: "-"
            if a.amount == 0 and a.type == "transfer_in"
            else _money_text(a.amount, symbol=""),
        }
        if cost:
            right[A_COST] = "-"
        w.cells(
            {A_DATE: w.date_text(a.day), A_NAME: name, A_SYMBOL: a.symbol or "", A_DESC: a.action},
            right,
        )


def _cash_header(w: _Writer, *, reference: bool) -> None:
    left = {D_DATE: "Date", D_DESC: "Description"}
    if reference:
        left[D_REF] = "Reference"
    w.cells(left, {D_AMOUNT: "Amount"}, font=BOLD)


def _cash_rows(w: _Writer, rows: list[FidActivity], *, reference: bool) -> None:
    for a in rows:
        w.need()
        left = {D_DATE: w.date_text(a.day), D_DESC: a.action}
        if reference:
            left[D_REF] = a.reference
        w.cells(left, {D_AMOUNT: _money_text(a.amount)})


def _activity(
    w: _Writer,
    account: FidAccount,
    rows: list[FidActivity],
    *,
    true_totals: bool = False,
) -> None:
    """The activity tables; ``true_totals``: each total is the account's own
    rows' (not the printed rows': a misprinted row is not in its total)."""
    w.bold("Activity")
    sections: list[tuple[str, Table]] = [
        ("Securities Bought & Sold", "trades"),
        ("Dividends, Interest & Other Income", "income"),
        ("Deposits", "deposits"),
        ("Withdrawals", "withdrawals"),
        ("Fees and Charges", "fees"),
        ("Other Activity In", "other_in"),
    ]
    for title, table in sections:
        chosen = [a for a in rows if a.table == table]
        if not chosen:
            continue
        w.bold(title, 8)
        if table in ("trades", "income", "other_in"):
            cost = table == "trades"
            w.header = lambda cost=cost: _security_header(w, cost=cost)
            _security_header(w, cost=cost)
            _security_rows(w, chosen, cost=cost)
        else:
            reference = table != "fees"
            w.header = lambda reference=reference: _cash_header(w, reference=reference)
            _cash_header(w, reference=reference)
            _cash_rows(w, chosen, reference=reference)
        summed = [a for a in account.activity if a.table == table] if true_totals else chosen
        w.cells(
            {A_DATE: f"Total {title}"},
            {A_AMOUNT: _money_text(sum((a.amount for a in summed), Decimal(0)))},
        )
        w.header = None
        w.y -= 4
    if not w.generic:
        _core_fund_activity(w, account, rows)


def _core_fund_activity(w: _Writer, account: FidAccount, rows: list[FidActivity]) -> None:
    """The core's own buys and sells settling each trade (not read by the parser)."""
    w.bold("Core Fund Activity", 8)

    def header() -> None:
        w.cells(
            {
                A_DATE: "Settlement",
                A_NAME: "Account",
                A_SYMBOL: "Transaction",
                A_DESC: "Description",
            },
            {A_QTY: "Quantity", A_PRICE: "Price", A_COST: "Amount", A_AMOUNT: "Balance"},
            font=BOLD,
        )
        w.cells({A_DATE: "Date", A_NAME: "Type"}, {}, font=BOLD)

    w.header = header
    header()
    balance = account.core_opening
    for a in rows:
        if a.amount == 0:
            continue
        balance += a.amount
        w.need()
        action = "You Sold" if a.amount < 0 else "You Bought"
        w.cells(
            {
                A_DATE: w.date_text(a.day),
                A_NAME: "CASH",
                A_SYMBOL: action,
                A_DESC: "FICTIONAL GOVT MMKT",
            },
            {
                A_QTY: _qty_text(a.amount),
                A_PRICE: "$1.0000",
                A_COST: _money_text(-a.amount, symbol=""),
                A_AMOUNT: _money_text(balance),
            },
        )
    w.header = None


def fidelity_pdf(
    stmt: FidStatement,
    *,
    tamper: Tamper = None,
    tamper_account: int = 1,
    generic: bool = False,
    page_bottom: float = BOTTOM,
    ruled: bool = False,
) -> bytes:
    """The statement as a PDF (module doc)."""
    w = _Writer(stmt, generic=generic, bottom=page_bottom, ruled=ruled)
    _overview(w, stmt.accounts)
    for index, account in enumerate(stmt.accounts):
        mark = tamper if index == tamper_account else None
        values = [p.market_value for p in account.positions]
        if mark == "market_value":
            values[0] += Decimal("100.00")
        total = account.holdings_value + account.core_closing
        if mark == "total":
            total += Decimal("250.00")
        w.new_page(account)
        _summary(w, account, total)
        _holdings(w, account, values)
        _activity(
            w, account, _printed_activity(account, mark), true_totals=mark == "misprinted_row"
        )
    return w.finish()

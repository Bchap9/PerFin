# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic brokerage statements: PDF (reportlab) and OFX ``INVSTMTRS`` (SGML/XML).

Everything is invented: the fictional **Placeholder Brokerage** of the
shipped classifier fingerprint (``placeholder-brokerage-account``), made-up
instruments (symbols like ``PBXA``, CUSIP-shaped ids starting ``ZZ``), random
quantities and prices from a seed. Nothing is a real instrument, account or
person.

* :func:`brokerage_series` builds consecutive monthly
  :class:`SynthBrokerage` statements: holdings at the start, the month's
  activity (buy, sell, dividend, a dividend reinvestment, interest, a fee, a
  cash deposit and withdrawal), holdings at the end valued at month-end
  prices, opening/closing cash. Month *n+1* starts where month *n* ends.
* :func:`placeholder_pdf` draws the Placeholder Brokerage layout: portfolio
  summary (cash, holdings total, account value), *Holdings as of* table
  (symbol, description, quantity, 4-decimal price, market value, cost
  basis) and *Account Activity* table (*Date / Activity / Description /
  Quantity / Price / Amount*, signed amounts, continued over pages with
  the header repeated). A reinvested dividend prints as two rows (the
  dividend and the reinvestment), as brokers do.
* :func:`ofx_investment` writes the same statement as OFX 1.x SGML or 2.x
  XML: ``SECLIST``, ``INVPOSLIST``, ``INVTRANLIST`` (``BUYMF``,
  ``BUYSTOCK``, ``SELLSTOCK``, ``INCOME``, ``REINVEST``, ``INVEXPENSE``,
  ``INVBANKTRAN``) and ``INVBAL``.
* Tampering (SECURITY.md C12): ``"extra_row"`` prints a forged buy the cash
  balance does not account for, ``"market_value"`` misprints one position's
  market value, ``"total"`` misprints the account value.

:func:`expected_activity` / :func:`expected_positions` give what a parser
must return.
"""

from __future__ import annotations

import io
import random
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final, Literal

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

INSTITUTION: Final = "Placeholder Brokerage"
INSTITUTION_KEY: Final = "placeholder-brokerage-account"
CENT: Final = Decimal("0.01")
FONT = "Helvetica"
BOLD = "Helvetica-Bold"

Tamper = Literal["extra_row", "market_value", "total"] | None
ActivityType = Literal[
    "buy", "sell", "dividend", "interest", "fee", "reinvest", "deposit", "withdrawal"
]


@dataclass(frozen=True)
class SynthSecurity:
    symbol: str
    name: str
    cusip: str
    kind: Literal["stock", "mf"]


#: Invented instruments (symbols, names and CUSIP-shaped ids are made up).
SECURITIES: Final = (
    SynthSecurity("PBXA", "Placeholder Index Fund", "ZZ0000A17", "mf"),
    SynthSecurity("PBXB", "Example Growth Holdings", "ZZ0000B25", "stock"),
    SynthSecurity("PBXC", "Sample Bond Fund", "ZZ0000C33", "mf"),
    SynthSecurity("PBXD", "Fictional Industrials", "ZZ0000D41", "stock"),
)
BY_SYMBOL: Final = {s.symbol: s for s in SECURITIES}


@dataclass(frozen=True)
class SynthPosition:
    symbol: str
    quantity: Decimal
    price: Decimal
    cost: Decimal

    @property
    def market_value(self) -> Decimal:
        return (self.quantity * self.price).quantize(CENT, rounding=ROUND_HALF_EVEN)

    @property
    def security(self) -> SynthSecurity:
        return BY_SYMBOL[self.symbol]


@dataclass(frozen=True)
class SynthActivity:
    day: date
    type: ActivityType
    amount: Decimal
    symbol: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    description: str = ""
    fitid: str = ""


@dataclass(frozen=True)
class SynthBrokerage:
    last4: str
    start: date
    end: date
    positions: tuple[SynthPosition, ...]
    activity: tuple[SynthActivity, ...]
    cash_opening: Decimal
    currency: str = "USD"

    @property
    def cash_closing(self) -> Decimal:
        # A reinvested dividend never touches the cash (dividend in, reinvestment out).
        return self.cash_opening + sum(
            (a.amount for a in self.activity if a.type != "reinvest"), Decimal(0)
        )

    @property
    def holdings_value(self) -> Decimal:
        return sum((p.market_value for p in self.positions), Decimal(0))

    @property
    def total_value(self) -> Decimal:
        return self.holdings_value + self.cash_closing

    @property
    def account_number(self) -> str:
        return f"5550{self.last4[::-1]}{self.last4}"


def _money(rng: random.Random, low: int, high: int) -> Decimal:
    return Decimal(rng.randrange(low * 100, high * 100)) / 100


def _price(rng: random.Random, low: int, high: int) -> Decimal:
    return Decimal(rng.randrange(low * 10_000, high * 10_000)) / 10_000


def _quantity(rng: random.Random) -> Decimal:
    return Decimal(rng.randrange(10_000, 400_000)) / 1000


def _month_end(start: date) -> date:
    following = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return following - timedelta(days=1)


def brokerage_series(
    seed: int = 51, *, months: int = 2, start: date = date(2026, 1, 1), last4: str = "4821"
) -> list[SynthBrokerage]:
    """Consecutive monthly statements (module doc); deterministic for a seed."""
    rng = random.Random(seed)
    prices = {s.symbol: _price(rng, 12, 240) for s in SECURITIES}
    held: dict[str, Decimal] = {s.symbol: _quantity(rng) for s in SECURITIES[:3]}
    cost: dict[str, Decimal] = {
        sym: (qty * prices[sym] * Decimal("0.9")).quantize(CENT) for sym, qty in held.items()
    }
    cash = _money(rng, 8000, 20000)
    out: list[SynthBrokerage] = []
    month_start = start
    for month in range(months):
        end = _month_end(month_start)
        opening_cash = cash
        rows: list[SynthActivity] = []

        def add(row: SynthActivity, rows: list[SynthActivity] = rows, month: int = month) -> None:
            rows.append(replace(row, fitid=f"PB{seed:03d}{month:02d}{len(rows):04d}"))

        # A buy of a fund already held, and one of a new stock in the first month.
        for symbol, offset in (("PBXA", 3), ("PBXD", 7) if month == 0 else ("PBXB", 7)):
            qty = Decimal(rng.randrange(2, 15))
            price = _price(rng, 12, 240)
            amount = -(qty * price).quantize(CENT, rounding=ROUND_HALF_EVEN)
            sec = BY_SYMBOL[symbol]
            add(
                SynthActivity(
                    month_start + timedelta(days=offset),
                    "buy",
                    amount,
                    symbol,
                    qty,
                    price,
                    f"{symbol} {sec.name}",
                )
            )
            held[symbol] = held.get(symbol, Decimal(0)) + qty
            cost[symbol] = cost.get(symbol, Decimal(0)) - amount
        # A partial sale.
        sell_qty = (held["PBXB"] / 4).quantize(Decimal("0.001"))
        sell_price = _price(rng, 12, 240)
        proceeds = (sell_qty * sell_price).quantize(CENT, rounding=ROUND_HALF_EVEN)
        add(
            SynthActivity(
                month_start + timedelta(days=11),
                "sell",
                proceeds,
                "PBXB",
                sell_qty,
                sell_price,
                f"PBXB {BY_SYMBOL['PBXB'].name}",
            )
        )
        cost["PBXB"] = (cost["PBXB"] * (held["PBXB"] - sell_qty) / held["PBXB"]).quantize(CENT)
        held["PBXB"] -= sell_qty
        add(
            SynthActivity(
                month_start + timedelta(days=14),
                "dividend",
                _money(rng, 5, 90),
                "PBXB",
                description=f"PBXB {BY_SYMBOL['PBXB'].name}",
            )
        )
        # A reinvested dividend (printed as a dividend row and a reinvestment row).
        re_qty = Decimal(rng.randrange(1000, 5000)) / 1000
        re_price = _price(rng, 12, 240)
        re_amount = (re_qty * re_price).quantize(CENT, rounding=ROUND_HALF_EVEN)
        add(
            SynthActivity(
                month_start + timedelta(days=15),
                "reinvest",
                -re_amount,
                "PBXC",
                re_qty,
                re_price,
                f"PBXC {BY_SYMBOL['PBXC'].name}",
            )
        )
        held["PBXC"] += re_qty
        cost["PBXC"] += re_amount
        add(
            SynthActivity(
                month_start + timedelta(days=17),
                "deposit",
                _money(rng, 200, 2000),
                description="Electronic funds transfer received",
            )
        )
        add(
            SynthActivity(
                month_start + timedelta(days=20),
                "withdrawal",
                -_money(rng, 50, 300),
                description="Electronic funds transfer sent",
            )
        )
        add(
            SynthActivity(
                end, "interest", _money(rng, 0, 5) + CENT, description="Cash sweep interest"
            )
        )
        add(SynthActivity(end, "fee", Decimal("-5.00"), description="Account maintenance fee"))
        for symbol in prices:
            prices[symbol] = (prices[symbol] * Decimal(rng.randrange(95, 106)) / 100).quantize(
                Decimal("0.0001")
            )
        positions = tuple(
            SynthPosition(sym, qty, prices[sym], cost[sym])
            for sym, qty in sorted(held.items())
            if qty > 0
        )
        stmt = SynthBrokerage(last4, month_start, end, positions, tuple(rows), opening_cash)
        cash = stmt.cash_closing
        out.append(stmt)
        month_start = end + timedelta(days=1)
    return out


def brokerage_statement(seed: int = 51) -> SynthBrokerage:
    return brokerage_series(seed, months=1)[0]


#: The buy a ``tamper="extra_row"`` statement prints but its cash does not reflect.
FORGED: Final = SynthActivity(
    date(2026, 1, 18),
    "buy",
    Decimal("-777.77"),
    "PBXA",
    Decimal(7),
    Decimal("111.1100"),
    "PBXA Placeholder Index Fund",
    "FORGED",
)


def _printed_activity(stmt: SynthBrokerage, tamper: Tamper) -> list[SynthActivity]:
    rows = list(stmt.activity)
    if tamper == "extra_row":
        forged = replace(FORGED, day=stmt.start + timedelta(days=17))
        rows = sorted([*rows, forged], key=lambda a: a.day)
    return rows


def expected_activity(
    stmt: SynthBrokerage, *, tamper: Tamper = None
) -> list[tuple[date, str, str | None, Decimal | None, Decimal]]:
    """``(trade date, type, symbol, quantity, amount)`` a parser must return, sorted.

    A reinvestment is a dividend of the amount and a ``reinvest`` costing it.
    """
    out: list[tuple[date, str, str | None, Decimal | None, Decimal]] = []
    for a in _printed_activity(stmt, tamper):
        if a.type == "reinvest":
            out.append((a.day, "dividend", a.symbol, None, -a.amount))
        out.append((a.day, a.type, a.symbol, a.quantity, a.amount))
    return sorted(out, key=lambda r: (r[0], r[1], r[2] or "", r[4]))


def expected_positions(stmt: SynthBrokerage) -> list[tuple[str, Decimal, Decimal, Decimal]]:
    """``(symbol, quantity, price, market value)`` of each position, sorted."""
    return sorted((p.symbol, p.quantity, p.price, p.market_value) for p in stmt.positions)


# ---------------------------------------------------------------------------
# PDF (Placeholder Brokerage layout)
# ---------------------------------------------------------------------------


def _money_text(value: Decimal, *, symbol: str = "") -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(value):,.2f}"


def _qty_text(value: Decimal) -> str:
    text = f"{value:,.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _us(day: date) -> str:
    return day.strftime("%m/%d/%Y")


def _activity_lines(
    rows: Sequence[SynthActivity],
) -> list[tuple[date, str, str, Decimal | None, Decimal | None, Decimal]]:
    out: list[tuple[date, str, str, Decimal | None, Decimal | None, Decimal]] = []
    labels = {
        "buy": "Buy",
        "sell": "Sell",
        "dividend": "Dividend",
        "interest": "Interest",
        "fee": "Fee",
        "deposit": "Deposit",
        "withdrawal": "Withdrawal",
    }
    for a in rows:
        if a.type == "reinvest":
            out.append((a.day, "Dividend", a.description, None, None, -a.amount))
            out.append((a.day, "Reinvest", a.description, a.quantity, a.price, a.amount))
        else:
            out.append((a.day, labels[a.type], a.description, a.quantity, a.price, a.amount))
    return out


def placeholder_pdf(
    stmt: SynthBrokerage,
    *,
    tamper: Tamper = None,
    rows_per_page: int = 9,
    institution: str = INSTITUTION,
) -> bytes:
    """The Placeholder Brokerage statement as a PDF (module doc)."""
    activity = _activity_lines(_printed_activity(stmt, tamper))
    positions = list(stmt.positions)
    values = [p.market_value for p in positions]
    if tamper == "market_value":
        values[0] += Decimal("100.00")
    total_value = stmt.total_value + (Decimal("250.00") if tamper == "total" else Decimal(0))
    pages = [activity[i : i + rows_per_page] for i in range(0, len(activity), rows_per_page)]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle("Statement")
    h = {"symbol": 72, "desc": 125, "qty": 330, "price": 400, "value": 480, "cost": 555}
    a = {"date": 72, "activity": 135, "desc": 195, "qty": 400, "price": 470, "amount": 545}
    for number, chunk in enumerate(pages, start=1):
        c.setFont(BOLD, 16)
        c.drawString(72, 750, institution)
        c.setFont(FONT, 10)
        if number == 1:
            c.drawString(72, 732, "Investment Account Statement")
            c.drawString(72, 716, f"Account XXXX-{stmt.last4}")
            c.drawString(300, 716, f"Statement period: {_us(stmt.start)} - {_us(stmt.end)}")
            c.setFont(BOLD, 11)
            c.drawString(72, 694, "Portfolio Summary")
            c.setFont(FONT, 10)
            summary = (
                ("Beginning cash balance", stmt.cash_opening),
                ("Ending cash balance", stmt.cash_closing),
                ("Total holdings value", sum(values, Decimal(0))),
                ("Total account value", total_value),
            )
            y = 678
            for label, value in summary:
                c.drawString(72, y, label)
                c.drawRightString(330, y, _money_text(value, symbol="$"))
                y -= 14
            y -= 10
            c.setFont(BOLD, 11)
            c.drawString(72, y, f"Holdings as of {_us(stmt.end)}")
            y -= 18
            c.setFont(BOLD, 9)
            c.drawString(h["symbol"], y, "Symbol")
            c.drawString(h["desc"], y, "Description")
            c.drawRightString(h["qty"], y, "Quantity")
            c.drawRightString(h["price"], y, "Price")
            c.drawRightString(h["value"], y, "Market Value")
            c.drawRightString(h["cost"], y, "Cost Basis")
            c.setFont(FONT, 9)
            y -= 14
            for position, value in zip(positions, values, strict=True):
                c.drawString(h["symbol"], y, position.symbol)
                c.drawString(h["desc"], y, position.security.name)
                c.drawRightString(h["qty"], y, _qty_text(position.quantity))
                c.drawRightString(h["price"], y, f"{position.price:,.4f}")
                c.drawRightString(h["value"], y, _money_text(value))
                c.drawRightString(h["cost"], y, _money_text(position.cost))
                y -= 14
            c.drawString(h["symbol"], y, "Total holdings")
            c.drawRightString(h["value"], y, _money_text(sum(values, Decimal(0))))
            y -= 26
            c.setFont(BOLD, 11)
            c.drawString(72, y, "Account Activity")
            top = y - 18
        else:
            c.drawString(72, 732, "Account Activity (continued)")
            top = 706
        c.setFont(BOLD, 9)
        c.drawString(a["date"], top, "Date")
        c.drawString(a["activity"], top, "Activity")
        c.drawString(a["desc"], top, "Description")
        c.drawRightString(a["qty"], top, "Quantity")
        c.drawRightString(a["price"], top, "Price")
        c.drawRightString(a["amount"], top, "Amount")
        c.setFont(FONT, 9)
        y = top - 14
        for day, label, description, qty, price, amount in chunk:
            c.drawString(a["date"], y, _us(day))
            c.drawString(a["activity"], y, label)
            c.drawString(a["desc"], y, description)
            if qty is not None:
                c.drawRightString(a["qty"], y, _qty_text(qty))
            if price is not None:
                c.drawRightString(a["price"], y, f"{price:,.4f}")
            c.drawRightString(a["amount"], y, _money_text(amount))
            y -= 14
        c.setFont(FONT, 8)
        c.drawString(72, 40, f"Page {number} of {len(pages)}")
        c.drawRightString(540, 40, f"{institution} is a fictional broker for testing.")
        c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# OFX INVSTMTRS (SGML 1.x and XML 2.x)
# ---------------------------------------------------------------------------


def _ofx_date(day: date) -> str:
    return day.strftime("%Y%m%d") + "120000"


def _secid(symbol: str) -> str:
    return (
        f"<SECID><UNIQUEID>{BY_SYMBOL[symbol].cusip}</UNIQUEID>"
        "<UNIQUEIDTYPE>CUSIP</UNIQUEIDTYPE></SECID>"
    )


def _invtran(a: SynthActivity) -> str:
    return (
        f"<INVTRAN><FITID>{a.fitid}</FITID><DTTRADE>{_ofx_date(a.day)}</DTTRADE>"
        f"<DTSETTLE>{_ofx_date(a.day + timedelta(days=2))}</DTSETTLE>"
        f"<MEMO>{a.description}</MEMO></INVTRAN>"
    )


def _txn(a: SynthActivity) -> str:
    if a.type in ("buy", "sell"):
        assert a.symbol is not None
        kind = BY_SYMBOL[a.symbol].kind
        verb = "BUY" if a.type == "buy" else "SELL"
        aggregate = f"{verb}{'MF' if kind == 'mf' else 'STOCK'}"
        inner = "INVBUY" if a.type == "buy" else "INVSELL"
        extra = f"<{verb}TYPE>{verb}</{verb}TYPE>"
        units = a.quantity if a.type == "buy" else -(a.quantity or Decimal(0))
        return (
            f"<{aggregate}><{inner}>{_invtran(a)}{_secid(a.symbol)}"
            f"<UNITS>{units}</UNITS><UNITPRICE>{a.price}</UNITPRICE>"
            f"<COMMISSION>0.00</COMMISSION><TOTAL>{a.amount}</TOTAL>"
            f"<SUBACCTSEC>CASH</SUBACCTSEC><SUBACCTFUND>CASH</SUBACCTFUND></{inner}>"
            f"{extra}</{aggregate}>"
        )
    if a.type in ("dividend",):
        assert a.symbol is not None
        return (
            f"<INCOME>{_invtran(a)}{_secid(a.symbol)}<INCOMETYPE>DIV</INCOMETYPE>"
            f"<TOTAL>{a.amount}</TOTAL><SUBACCTSEC>CASH</SUBACCTSEC>"
            f"<SUBACCTFUND>CASH</SUBACCTFUND></INCOME>"
        )
    if a.type == "reinvest":
        assert a.symbol is not None
        return (
            f"<REINVEST>{_invtran(a)}{_secid(a.symbol)}<INCOMETYPE>DIV</INCOMETYPE>"
            f"<TOTAL>{a.amount}</TOTAL><SUBACCTSEC>CASH</SUBACCTSEC>"
            f"<UNITS>{a.quantity}</UNITS><UNITPRICE>{a.price}</UNITPRICE></REINVEST>"
        )
    if a.type == "fee":
        return (
            f"<INVEXPENSE>{_invtran(a)}<TOTAL>{a.amount}</TOTAL>"
            f"<SUBACCTSEC>CASH</SUBACCTSEC><SUBACCTFUND>CASH</SUBACCTFUND></INVEXPENSE>"
        )
    trntype = {"interest": "INT", "deposit": "CREDIT", "withdrawal": "DEBIT"}[a.type]
    if a.type == "interest":
        # Interest on the cash sweep is an INCOME without a security in some
        # files; this one sends it as a bank transaction of type INT.
        return (
            f"<INCOME>{_invtran(a)}<INCOMETYPE>INTEREST</INCOMETYPE>"
            f"<TOTAL>{a.amount}</TOTAL><SUBACCTSEC>CASH</SUBACCTSEC>"
            f"<SUBACCTFUND>CASH</SUBACCTFUND></INCOME>"
        )
    return (
        f"<INVBANKTRAN><STMTTRN><TRNTYPE>{trntype}</TRNTYPE>"
        f"<DTPOSTED>{_ofx_date(a.day)}</DTPOSTED><TRNAMT>{a.amount}</TRNAMT>"
        f"<FITID>{a.fitid}</FITID><NAME>{a.description}</NAME></STMTTRN>"
        f"<SUBACCTFUND>CASH</SUBACCTFUND></INVBANKTRAN>"
    )


def _position(p: SynthPosition, on: date) -> str:
    aggregate = "POSMF" if p.security.kind == "mf" else "POSSTOCK"
    return (
        f"<{aggregate}><INVPOS>{_secid(p.symbol)}<HELDINACCT>CASH</HELDINACCT>"
        f"<POSTYPE>LONG</POSTYPE><UNITS>{p.quantity}</UNITS><UNITPRICE>{p.price}</UNITPRICE>"
        f"<MKTVAL>{p.market_value}</MKTVAL><DTPRICEASOF>{_ofx_date(on)}</DTPRICEASOF>"
        f"</INVPOS></{aggregate}>"
    )


def _seclist(stmt: SynthBrokerage) -> str:
    symbols = sorted(
        {p.symbol for p in stmt.positions} | {a.symbol for a in stmt.activity if a.symbol}
    )
    infos = []
    for symbol in symbols:
        sec = BY_SYMBOL[symbol]
        aggregate = "MFINFO" if sec.kind == "mf" else "STOCKINFO"
        infos.append(
            f"<{aggregate}><SECINFO>{_secid(symbol)}<SECNAME>{sec.name}</SECNAME>"
            f"<TICKER>{symbol}</TICKER></SECINFO></{aggregate}>"
        )
    return f"<SECLISTMSGSRSV1><SECLIST>{''.join(infos)}</SECLIST></SECLISTMSGSRSV1>"


def ofx_investment(stmt: SynthBrokerage, *, xml: bool = False, tamper: Tamper = None) -> bytes:
    """The statement as an OFX investment download (module doc)."""
    rows = _printed_activity(stmt, tamper)
    positions = "".join(_position(p, stmt.end) for p in stmt.positions)
    if tamper == "market_value":
        first = stmt.positions[0]
        positions = positions.replace(
            f"<MKTVAL>{first.market_value}</MKTVAL>",
            f"<MKTVAL>{first.market_value + 100}</MKTVAL>",
            1,
        )
    body = (
        "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>"
        f"<DTSERVER>{_ofx_date(stmt.end)}</DTSERVER><LANGUAGE>ENG</LANGUAGE>"
        f"<FI><ORG>{INSTITUTION}</ORG><FID>9105</FID></FI></SONRS></SIGNONMSGSRSV1>"
        "<INVSTMTMSGSRSV1><INVSTMTTRNRS><TRNUID>1</TRNUID>"
        "<STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>"
        f"<INVSTMTRS><DTASOF>{_ofx_date(stmt.end)}</DTASOF><CURDEF>{stmt.currency}</CURDEF>"
        f"<INVACCTFROM><BROKERID>placeholder.example</BROKERID>"
        f"<ACCTID>{stmt.account_number}</ACCTID></INVACCTFROM>"
        f"<INVTRANLIST><DTSTART>{_ofx_date(stmt.start)}</DTSTART>"
        f"<DTEND>{_ofx_date(stmt.end)}</DTEND>{''.join(_txn(a) for a in rows)}</INVTRANLIST>"
        f"<INVPOSLIST>{positions}</INVPOSLIST>"
        f"<INVBAL><AVAILCASH>{stmt.cash_closing}</AVAILCASH><MARGINBALANCE>0</MARGINBALANCE>"
        "<SHORTBALANCE>0</SHORTBALANCE></INVBAL>"
        "</INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1>"
        f"{_seclist(stmt)}"
    )
    if xml:
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
            '<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" '
            'NEWFILEUID="NONE"?>\n'
            f"<OFX>{body}</OFX>\n"
        ).encode()
    return (
        "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\n"
        "CHARSET:1252\nCOMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n"
        f"<OFX>{body}</OFX>\n"
    ).encode("ascii")

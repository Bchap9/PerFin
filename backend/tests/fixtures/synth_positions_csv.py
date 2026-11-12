# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic "Portfolio Positions" CSV exports, shaped like a brokerage's.

The layout follows the column header of a common US brokerage positions
export (``Account Number``, ``Account Name``, ``Symbol``, ``Description``,
``Quantity``, ``Last Price``, ``Last Price Change``, ``Current Value``, the
day's and total gain columns, ``Percent Of Account``, ``Cost Basis Total``,
``Average Cost Basis``, ``Type``) and its file shape: one row per holding of
every account, a core cash row per account (a symbol ending in ``**``, no
quantity), amounts written ``$1,234.56`` with a sign in front of the gains,
quoted disclaimer paragraphs and a ``Date downloaded`` line after the rows.
Everything else is invented: the account numbers, the fund names, symbols,
quantities, prices and cost bases. No real export, account, security or
person appears, and nothing is read from outside this module.

* :class:`Holding` / :class:`PositionsAccount` - what an account holds.
* :func:`positions_csv_bytes` - the file itself (CRLF, UTF-8).
* :func:`two_accounts` - a taxable account and a Roth IRA with a few funds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

HEADER: Final = (
    "Account Number",
    "Account Name",
    "Symbol",
    "Description",
    "Quantity",
    "Last Price",
    "Last Price Change",
    "Current Value",
    "Today's Gain/Loss Dollar",
    "Today's Gain/Loss Percent",
    "Total Gain/Loss Dollar",
    "Total Gain/Loss Percent",
    "Percent Of Account",
    "Cost Basis Total",
    "Average Cost Basis",
    "Type",
)
DISCLAIMERS: Final = (
    "The data and information in this synthetic spreadsheet are invented for tests. "
    "They describe no real account, security or person.",
    "Synthetic Brokerage is not a real company; nothing here is investment, tax or legal advice.",
)
CENT: Final = Decimal("0.01")
CORE_SYMBOL: Final = "SCASH**"
CORE_NAME: Final = "HELD IN MONEY MARKET"


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True)
class Holding:
    symbol: str
    name: str
    quantity: Decimal
    price: Decimal
    #: The position's total cost basis (None: the export prints none).
    cost: Decimal | None

    @property
    def value(self) -> Decimal:
        return cents(self.quantity * self.price)


@dataclass(frozen=True)
class PositionsAccount:
    number: str
    name: str
    holdings: tuple[Holding, ...] = ()
    #: The core position's value (None: the account prints no cash row).
    cash: Decimal | None = Decimal("0.00")
    #: Extra rows written as given (cells), after the account's own.
    extra: tuple[tuple[str, ...], ...] = field(default=())

    @property
    def last4(self) -> str:
        return self.number[-4:]

    @property
    def total(self) -> Decimal:
        return sum((h.value for h in self.holdings), self.cash or Decimal(0))


def money(value: Decimal, *, sign: bool = False) -> str:
    text = f"${abs(value):,.2f}"
    if sign:
        return ("-" if value < 0 else "+") + text
    return "-" + text if value < 0 else text


def _percent(part: Decimal, whole: Decimal, *, sign: bool = False) -> str:
    if whole == 0:
        return ""
    value = cents(part * 100 / whole)
    return f"{value:+.2f}%" if sign else f"{value:.2f}%"


def account_rows(account: PositionsAccount) -> list[list[str]]:
    """The rows the export writes for one account: its cash, then its holdings."""
    rows: list[list[str]] = []
    total = account.total
    if account.cash is not None:
        rows.append(
            [
                account.number,
                account.name,
                CORE_SYMBOL,
                CORE_NAME,
                "",
                "",
                "",
                money(account.cash),
                "",
                "",
                "",
                "",
                _percent(account.cash, total),
                "",
                "",
                "Cash",
            ]
        )
    for holding in account.holdings:
        gain = holding.value - holding.cost if holding.cost is not None else None
        rows.append(
            [
                account.number,
                account.name,
                holding.symbol,
                holding.name,
                f"{holding.quantity:f}",
                money(holding.price),
                "+$0.11",
                money(holding.value),
                "+$1.10",
                "+0.24%",
                money(gain, sign=True) if gain is not None else "n/a",
                (
                    _percent(gain, holding.cost, sign=True)
                    if gain is not None and holding.cost
                    else "n/a"
                ),
                _percent(holding.value, total),
                money(holding.cost) if holding.cost is not None else "n/a",
                (
                    money(cents(holding.cost / holding.quantity))
                    if holding.cost is not None
                    else "n/a"
                ),
                "Cash",
            ]
        )
    rows.extend([list(cells) for cells in account.extra])
    return rows


def _quote(value: str) -> str:
    if any(ch in value for ch in ',"\r\n'):
        return '"' + value.replace('"', '""') + '"'
    return value


def positions_csv_bytes(
    accounts: Sequence[PositionsAccount],
    *,
    downloaded: date | None = date(2026, 11, 11),
    header: Sequence[str] = HEADER,
) -> bytes:
    """The export: header, every account's rows, disclaimers, the download line."""
    lines = [",".join(header)]
    for account in accounts:
        lines += [",".join(_quote(c) for c in row) for row in account_rows(account)]
    lines += ["", *(_quote(text) for text in DISCLAIMERS), ""]
    if downloaded is not None:
        lines.append(_quote(f"Date downloaded {downloaded.strftime('%b-%d-%Y')} 7:35 p.m ET"))
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


GROWTH: Final = Holding(
    "SGRX", "SYNTHETIC GROWTH INDEX FUND", Decimal("52.069"), Decimal("45.78"), Decimal("2237.68")
)
BOND: Final = Holding(
    "SBND", "SYNTHETIC TOTAL BOND FUND", Decimal("40"), Decimal("10.25"), Decimal("400.00")
)
VALUE: Final = Holding(
    "SVAL", "SYNTHETIC VALUE INDEX ETF", Decimal("12.5"), Decimal("80.00"), Decimal("900.50")
)


def two_accounts() -> tuple[PositionsAccount, PositionsAccount]:
    """A taxable account (two funds and cash) and a Roth IRA (one fund and cash)."""
    return (
        PositionsAccount("Z40001111", "Individual", (GROWTH, BOND), Decimal("1000.75")),
        PositionsAccount("240002222", "ROTH IRA", (VALUE,), Decimal("25.00")),
    )

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
* :func:`broker_positions_csv` / :func:`broker_all_accounts_positions_csv` -
  another broker's shape: a title line naming the account and the moment of
  the download, no account column, a ``Cash & Cash Investments`` row, an
  ``Account Total`` row and a trailing comma on every line; the second prints
  one such block per account, each under a line naming it (``one_header``:
  the header row once, above the first block only).
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


# ---------------------------------------------------------------------------
# Another broker's shape: a title, no account column, cash and total rows
# ---------------------------------------------------------------------------

BROKER_HEADER: Final = ("Symbol", "Description", "Quantity", "Price", "Market Value", "Cost Basis")
BROKER_CASH: Final = "Cash & Cash Investments"
BROKER_TOTAL: Final = "Account Total"
BROKER_DAY: Final = date(2026, 11, 6)
_BROKER_MOMENT: Final = "09:30 AM ET, 11/06/2026"


@dataclass(frozen=True)
class BrokerBlock:
    """One account of the export: the line naming it, its holdings, cash and printed total."""

    name: str
    last4: str
    holdings: tuple[Holding, ...]
    cash: Decimal
    #: The ``Account Total`` the export prints (None: holdings plus cash).
    printed_total: Decimal | None = None

    @property
    def title(self) -> str:
        return f"{self.name} ...{self.last4}"

    @property
    def total(self) -> Decimal:
        if self.printed_total is not None:
            return self.printed_total
        return sum((h.value for h in self.holdings), self.cash)


BROKER_INDIVIDUAL: Final = BrokerBlock(
    "Individual",
    "4417",
    (
        Holding(
            "SGRX",
            "SYNTHETIC GROWTH INDEX FUND",
            Decimal("10"),
            Decimal("52.10"),
            Decimal("480.00"),
        ),
        Holding(
            "SBND", "SYNTHETIC TOTAL BOND FUND", Decimal("20"), Decimal("24.05"), Decimal("470.00")
        ),
    ),
    Decimal("298.00"),
)
BROKER_ROTH: Final = BrokerBlock(
    "Roth IRA",
    "8820",
    (
        Holding(
            "SVAL", "SYNTHETIC VALUE INDEX ETF", Decimal("5"), Decimal("80.00"), Decimal("360.00")
        ),
        Holding(
            "SGRX", "SYNTHETIC GROWTH INDEX FUND", Decimal("4"), Decimal("52.10"), Decimal("200.00")
        ),
    ),
    Decimal("41.60"),
)


def _broker_rows(block: BrokerBlock, *, header: bool = True) -> list[list[str]]:
    """The header, the holdings, the cash row and the total row of one block."""
    rows = [list(BROKER_HEADER)] if header else []
    rows += [
        [
            h.symbol,
            h.name,
            f"{h.quantity:f}",
            money(h.price),
            money(h.value),
            money(h.cost) if h.cost is not None else "--",
        ]
        for h in block.holdings
    ]
    rows.append([BROKER_CASH, "--", "--", "--", money(block.cash), "--"])
    rows.append([BROKER_TOTAL, "--", "--", "--", money(block.total), "--"])
    return rows


def _broker_bytes(lines: Sequence[Sequence[str]]) -> bytes:
    """CRLF lines, every one ending in a comma (as the export writes them)."""
    text = "".join(",".join(_quote(c) for c in line) + ",\r\n" for line in lines)
    return text.encode("utf-8")


def broker_positions_csv(*, total: Decimal | None = None) -> bytes:
    """One account: its title line, then its block (``total``: the printed total)."""
    block = BROKER_INDIVIDUAL
    if total is not None:
        block = BrokerBlock(block.name, block.last4, block.holdings, block.cash, total)
    title = f"Positions for account {block.title} as of {_BROKER_MOMENT}"
    return _broker_bytes([[title], [], *_broker_rows(block)])


def broker_all_accounts_positions_csv(
    blocks: Sequence[BrokerBlock] = (BROKER_INDIVIDUAL, BROKER_ROTH),
    *,
    one_header: bool = False,
) -> bytes:
    """Every account: a title, then per account a line naming it and its block.

    ``one_header``: the header row is printed under the first account's line
    only, so nothing but the lines naming the accounts keeps them apart.
    """
    lines: list[Sequence[str]] = [[f"Positions for All-Accounts as of {_BROKER_MOMENT}"], []]
    for index, block in enumerate(blocks):
        rows = _broker_rows(block, header=index == 0 or not one_header)
        lines += [[block.title], *rows, []]
    return _broker_bytes(lines)

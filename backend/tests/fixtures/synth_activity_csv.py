# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic multi-account "Activity" CSV exports, shaped like a brokerage's.

The layout follows the column header of a common US brokerage activity
export (``Run Date``, ``Account``, ``Account Number``, ``Action``,
``Symbol``, ``Description``, ``Type``, the exchange columns, ``Currency``,
``Price``, ``Quantity``, ``Commission``, ``Fees``, ``Accrued Interest``,
``Amount``, ``Settlement Date``) and its file shape: blank lines before the
header, several accounts in one file told apart by ``Account`` and
``Account Number``, an ``Exchange Currency`` column that is always empty,
and quoted disclaimer paragraphs plus a ``Date downloaded`` line after the
rows. Everything else is invented and generated from a seed: the account
numbers (a letter and eight random digits), the fund names (``Synthetic
Growth Index ETF``...), symbols, prices, quantities and amounts. No real
statement, account, security or person appears, and nothing is read from
outside this module.

``Amount`` is the cash effect (negative = cash out) and ``Quantity`` the
signed number of shares, as in the real exports; this module only cares
about the cash-ledger columns (the generic CSV path), not investment
semantics.

* :func:`activity_accounts` - the synthetic accounts of a seed.
* :func:`activity_rows` - the rows (dataclasses) of a file.
* :func:`activity_csv_bytes` - the file itself (CRLF, UTF-8).
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

HEADER: Final = (
    "Run Date",
    "Account",
    "Account Number",
    "Action",
    "Symbol",
    "Description",
    "Type",
    "Exchange Quantity",
    "Exchange Currency",
    "Currency",
    "Price",
    "Quantity",
    "Exchange Rate",
    "Commission",
    "Fees",
    "Accrued Interest",
    "Amount",
    "Settlement Date",
)
#: Zero-based columns of :data:`HEADER` the cash-ledger mapping reads.
COLUMNS: Final = {
    "date": 0,
    "account_name": 1,
    "account": 2,
    "action": 3,
    "description": 5,
    "type": 6,
    "exchange_currency": 8,
    "currency": 9,
    "amount": 16,
    "settlement": 17,
}
#: The mapping a person would save for this layout (``MappingSpec`` JSON).
SPEC: Final = {
    "skip_rows": 0,
    "date_column": 0,
    "date_format": "MM/DD/YYYY",
    "amount_column": 16,
    "payee_column": 5,
    "memo_column": 3,
    "currency_column": 9,
    "account_column": 2,
}
DISCLAIMERS: Final = (
    "The data and information in this synthetic spreadsheet are invented for tests. "
    "They describe no real account, security or person.",
    "Synthetic Brokerage is not a real company; nothing here is investment, tax or legal advice.",
)
_FUNDS: Final = (
    ("SGRX", "SYNTHETIC GROWTH INDEX ETF"),
    ("SBND", "SYNTHETIC TOTAL BOND FUND"),
    ("SVAL", "SYNTHETIC VALUE INDEX ETF"),
)
_NAMES: Final = ("Individual", "Roth IRA", "Health Savings Account", "Cash Management")


@dataclass(frozen=True)
class ActivityAccount:
    name: str
    number: str

    @property
    def last4(self) -> str:
        return self.number[-4:]


@dataclass(frozen=True)
class ActivityRow:
    run_date: date
    account: ActivityAccount
    action: str
    symbol: str
    description: str
    price: Decimal | None
    quantity: Decimal
    amount: Decimal | None
    settlement: date | None
    currency: str = "USD"


def activity_accounts(seed: int, count: int = 2) -> tuple[ActivityAccount, ...]:
    """``count`` invented accounts with distinct last four digits."""
    rng = random.Random(f"activity-accounts-{seed}")  # nosec B311 - test data
    accounts: list[ActivityAccount] = []
    seen: set[str] = set()
    while len(accounts) < count:
        number = rng.choice("XYZ") + "".join(str(rng.randint(0, 9)) for _ in range(8))
        if number[-4:] in seen:
            continue
        seen.add(number[-4:])
        accounts.append(ActivityAccount(_NAMES[len(accounts) % len(_NAMES)], number))
    return tuple(accounts)


def _money(rng: random.Random, low: int, high: int) -> Decimal:
    return Decimal(rng.randint(low * 100, high * 100)) / 100


def activity_rows(
    seed: int,
    accounts: Sequence[ActivityAccount],
    *,
    start: date = date(2026, 10, 13),
    days: int = 18,
) -> list[ActivityRow]:
    """A newest-first activity list for ``accounts`` (days of the month above 12)."""
    rng = random.Random(f"activity-rows-{seed}")  # nosec B311 - test data
    rows: list[ActivityRow] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        for account in accounts:
            if rng.random() < 0.45:
                continue
            symbol, fund = rng.choice(_FUNDS)
            kind = rng.choice(("buy", "dividend", "reinvest", "interest", "deposit", "fee"))
            price = _money(rng, 40, 400)
            settle = day + timedelta(days=1)
            if kind == "buy":
                quantity = Decimal(rng.randint(1, 20))
                rows.append(
                    ActivityRow(
                        day,
                        account,
                        f"YOU BOUGHT {fund} ({symbol}) (Cash)",
                        symbol,
                        fund,
                        price,
                        quantity,
                        -(price * quantity),
                        settle,
                    )
                )
            elif kind == "dividend":
                amount = _money(rng, 1, 60)
                rows.append(
                    ActivityRow(
                        day,
                        account,
                        f"DIVIDEND RECEIVED {fund} ({symbol}) (Cash)",
                        symbol,
                        fund,
                        None,
                        Decimal(0),
                        amount,
                        None,
                    )
                )
                quantity = (amount / price).quantize(Decimal("0.001"))
                rows.append(
                    ActivityRow(
                        day,
                        account,
                        f"REINVESTMENT {fund} ({symbol}) (Cash)",
                        symbol,
                        fund,
                        price,
                        quantity,
                        -amount,
                        None,
                    )
                )
            elif kind == "reinvest":
                amount = _money(rng, 1, 5)
                rows.append(
                    ActivityRow(
                        day,
                        account,
                        f"REINVESTMENT {fund} ({symbol}) (Cash)",
                        symbol,
                        fund,
                        price,
                        (amount / price).quantize(Decimal("0.001")),
                        -amount,
                        None,
                    )
                )
            elif kind == "interest":
                rows.append(
                    ActivityRow(
                        day,
                        account,
                        "INTEREST EARNED SYNTHETIC MONEY MARKET (Cash)",
                        "SMMXX",
                        "SYNTHETIC MONEY MARKET",
                        None,
                        Decimal(0),
                        _money(rng, 0, 3),
                        None,
                    )
                )
            elif kind == "deposit":
                rows.append(
                    ActivityRow(
                        day,
                        account,
                        "Electronic Funds Transfer Received (Cash)",
                        "",
                        "No Description",
                        None,
                        Decimal(0),
                        _money(rng, 50, 900),
                        None,
                    )
                )
            else:
                rows.append(
                    ActivityRow(
                        day,
                        account,
                        "FEE CHARGED SYNTHETIC ADVISORY (Cash)",
                        "",
                        "SYNTHETIC ADVISORY FEE",
                        None,
                        Decimal(0),
                        -_money(rng, 1, 20),
                        None,
                    )
                )
    rows.sort(key=lambda r: r.run_date, reverse=True)
    return rows


def _quote(cell: str) -> str:
    if any(ch in cell for ch in (",", '"', "\n", "\r")):
        return '"' + cell.replace('"', '""') + '"'
    return cell


def _cells(row: ActivityRow) -> tuple[str, ...]:
    return (
        row.run_date.strftime("%m/%d/%Y"),
        row.account.name,
        row.account.number,
        row.action,
        row.symbol,
        row.description,
        "Cash",
        "0",
        "",  # Exchange Currency: always empty
        row.currency,
        f"{row.price:.2f}" if row.price is not None else "",
        f"{row.quantity}",
        "0",
        "",
        "",
        "",
        f"{row.amount:.2f}" if row.amount is not None else "",
        row.settlement.strftime("%m/%d/%Y") if row.settlement else "",
    )


def activity_csv_bytes(
    rows: Sequence[ActivityRow], *, downloaded: date = date(2026, 11, 2)
) -> bytes:
    """The export: blank lines, header, rows, disclaimers, the download line."""
    lines = ["", "", ",".join(HEADER)]
    lines += [",".join(_quote(c) for c in _cells(row)) for row in rows]
    lines += ["", *(_quote(text) for text in DISCLAIMERS), ""]
    lines.append(_quote(f"Date downloaded {downloaded.strftime('%m/%d/%Y')} 10:04 am"))
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brokerage statements: holdings and activity sections, and their reconciliation.

Two halves (INGESTION.md "Brokerage statements", SECURITY.md C12):

* :func:`reconcile_investment` checks an :class:`InvestmentStatement` from
  any source (an OFX ``INVSTMTRS`` from the sandbox, a PDF read here):

  - **positions**: quantity x unit price = market value for every position
    that prints all three, within half a unit of the price's last printed
    decimal times the quantity, plus :data:`~app.ingestion.reconcile.TOLERANCE`;
  - **holdings_total**: the positions' market values add up to the
    holdings total the statement prints;
  - **cash**: opening cash + the activity's cash effects = closing cash;
  - **total_value**: holdings + closing cash = the account value printed.

  Status ``mismatch`` when any check fails, ``unavailable`` when none could
  run, else ``ok``. The importer (``app.domain.investments.statement_import``)
  writes nothing of a statement that does not reconcile.

* :class:`BrokerageStatementParser` reads the stored pages of a PDF or
  scan (the words and boxes the sandbox extracted; never file bytes,
  ADR-0006) in the worker:

  - **accounts**: one statement per account when the pages name several
    (account headings such as ``Account # Z12-345678`` or ``INDIVIDUAL -
    TOD Z12-345678`` repeated on each account's pages), each with its own
    last four digits, tables, totals and reconciliation; a document naming
    one account is read as one statement (the parser's class doc);
  - a **holdings** table: a header naming a quantity, a market value and a
    symbol or security name (optional price, cost basis, beginning market
    value; columns such as unrealized gain/loss or estimated income are
    recognised and not read), repeated per page. A header may run over up
    to three lines (``Price Per`` / ``Unit``, a date under ``Ending Market
    Value``) and a header cell may end in a date (:func:`header_key`). Rows
    may be grouped under asset-class headings (:data:`ASSET_GROUPS`:
    *Mutual Funds*, *Stocks*, ...) that give the positions their class;
    ``Total <group>`` lines are subtotals, ``Total Holdings`` (or another
    :data:`GRAND_TOTALS` label, else the first unlabelled ``Total``) is the
    printed holdings total. A security name wrapped onto the next line is
    joined (its ``(SYMBOL)`` may be on that line);
  - an **activity** table: a header naming a date, an activity/action and
    an amount (optional settle date, description, security name,
    symbol or CUSIP, quantity, price, fees). Only a settlement date: it
    dates the row; no action column: the description is the action when
    the table also names a security, a symbol or a quantity (always for a
    layout with ``description_types``). The action text types the row
    (:func:`activity_type`, :data:`ACTIVITY_TYPES`); an unsigned amount
    takes the type's direction (a buy takes cash out); a transfer of units
    printed without an amount (``-``) moves no cash; a split's ratio comes
    from its description (``2-for-1``, ``3:1``). A header line of known
    column names that is no usable table ends the table above it;
  - outside the tables: the period, the account's last four digits, the
    currency, opening/closing cash, the holdings total and the account
    value by label (``Beginning Account Value`` is consumed, not read as
    the account value; ``Ending Account Value``/``Ending Net Value`` are),
    and the holdings' "as of" date.

  Each value is scored like the bank parsers
  (:mod:`app.ingestion.parsers.confidence`): the declared format of an
  institution parser 1.0, the generic parser :data:`~confidence.GENERIC`,
  OCR scaled by the cell's word confidence, a position whose market value
  disagrees :data:`~confidence.CONTRADICTED`, and every row of a statement
  that does not reconcile capped at :data:`~confidence.UNRECONCILED`
  (:data:`~confidence.UNCHECKED` when nothing can be checked).

Institution parsers pin a layout: :class:`PlaceholderBrokerageParser` the
fictional Placeholder Brokerage (fingerprint ``placeholder-brokerage-account``;
synthetic PDFs from ``tests/fixtures/synth_brokerage.py``) and
:class:`FidelityStatementParser` the Fidelity Investments "Investment
Report" of one or several accounts (fingerprint
``fidelity-investments-statement``; synthetic PDFs from
``tests/fixtures/synth_fidelity.py``), whose core money market position is
the account's cash (``cash_groups``) and whose *Core Fund Activity* table
is not read (``ignored_tables``). Every parser here keeps only the last four
digits of an account number (an account heading is read for them and not
kept).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Final, Literal

from pydantic import ValidationError

from app.core.money import sum_amounts
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.base import Cell, DocumentParser, DocumentView, ParseOutcome, ViewLine
from app.ingestion.parsers.table_statement import (
    PeriodCandidates,
    full_style,
    label_end,
    read_period,
)
from app.ingestion.parsers.text import (
    FULL_STYLES,
    MAX_TOKEN,
    NUMERIC_DM,
    Amount,
    DateStyle,
    find_amounts,
    find_dates,
    guess_numeric_style,
    normalise,
    numeric_parts,
    read_amount,
    read_date,
)
from app.ingestion.reconcile import TOLERANCE
from app.ingestion.sandbox.protocol import (
    MAX_STATEMENTS,
    AssetClassCode,
    Balance,
    InvestmentActivity,
    InvestmentActivityType,
    InvestmentPosition,
    InvestmentReconciliation,
    InvestmentStatement,
    ParserRef,
    SecurityRef,
    StatementAccount,
)

# ---------------------------------------------------------------------------
# Reconciliation (any source)
# ---------------------------------------------------------------------------


def _price_unit(price: Decimal) -> Decimal:
    """Half a unit of the last decimal the price was given with."""
    exponent = price.as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    return Decimal(5).scaleb(-(places + 1))


def position_value(position: InvestmentPosition) -> Decimal | None:
    """The position's market value: printed, else quantity x price."""
    if position.market_value is not None:
        return position.market_value
    if position.unit_price is not None:
        return position.quantity * position.unit_price
    return None


def reconcile_investment(statement: InvestmentStatement) -> InvestmentReconciliation:
    """The four brokerage checks (module doc)."""
    checks: list[str] = []
    failed: list[str] = []
    mismatches: list[int] = []
    for index, position in enumerate(statement.positions):
        if position.unit_price is None or position.market_value is None:
            continue
        expected = position.quantity * position.unit_price
        allowed = TOLERANCE + abs(position.quantity) * _price_unit(position.unit_price)
        if abs(expected - position.market_value) > allowed:
            mismatches.append(index)
    if any(p.unit_price is not None and p.market_value is not None for p in statement.positions):
        checks.append("positions")
        if mismatches:
            failed.append("positions")

    values = [position_value(p) for p in statement.positions]
    positions_total = (
        sum_amounts(v for v in values if v is not None)
        if all(v is not None for v in values)
        else None
    )
    reported_positions = statement.positions_value.amount if statement.positions_value else None
    if reported_positions is not None and positions_total is not None:
        checks.append("holdings_total")
        if abs(positions_total - reported_positions) > TOLERANCE:
            failed.append("holdings_total")

    opening = statement.cash_opening.amount if statement.cash_opening else None
    closing = statement.cash_closing.amount if statement.cash_closing else None
    activity = sum_amounts(a.amount for a in statement.activity)
    cash_difference: Decimal | None = None
    if opening is not None and closing is not None:
        checks.append("cash")
        cash_difference = opening + activity - closing
        if abs(cash_difference) > TOLERANCE:
            failed.append("cash")

    reported_total = statement.total_value.amount if statement.total_value else None
    holdings = positions_total if positions_total is not None else reported_positions
    total_value = holdings + closing if holdings is not None and closing is not None else None
    if reported_total is not None and total_value is not None:
        checks.append("total_value")
        if abs(total_value - reported_total) > TOLERANCE:
            failed.append("total_value")

    status: Literal["ok", "mismatch", "unavailable"] = (
        "mismatch" if failed else ("ok" if checks else "unavailable")
    )
    return InvestmentReconciliation(
        status=status,
        checks=checks,
        failed=failed,
        position_mismatches=mismatches,
        positions_total=positions_total,
        reported_positions_total=reported_positions,
        cash_opening=opening,
        cash_closing=closing,
        cash_activity=activity,
        cash_difference=cash_difference,
        total_value=total_value,
        reported_total_value=reported_total,
        tolerance=TOLERANCE,
    )


# ---------------------------------------------------------------------------
# Reading numbers, symbols, activity words
# ---------------------------------------------------------------------------

_NUMBER: Final = re.compile(
    r"(?P<open>\()?(?P<sign>[-+\u2212])?(?P<cur>[$\u00a3\u20ac])?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d{1,18})(?:\.(?P<dec>\d{1,10}))?(?P<close>\))?"
)
_SYMBOL: Final = re.compile(r"[A-Z][A-Z0-9.]{0,9}")
_PAREN_SYMBOL: Final = re.compile(r"\(([A-Z][A-Z0-9.]{0,9})\)")
_CUSIP: Final = re.compile(r"[0-9A-Z]{8}[0-9]")
_RATIO: Final = re.compile(r"\b(\d{1,6})(?:\s?-?\s?for\s?-?\s?|\s?:\s?)(\d{1,6})\b", re.IGNORECASE)
_LAST4: Final = re.compile(
    r"(?<![A-Za-z0-9])[*xX\u2022]{2}[*xX\u2022\s-]{0,30}?(\d{4})\b"
    r"|\bending(?:\s+in)?\s*:?\s*(\d{4})\b"
    r"|\baccount(?:\s+(?:number|no\.?))?\s*:?\s*\d{0,26}?(\d{4})\b",
    re.IGNORECASE,
)
_CURRENCY: Final = re.compile(r"\bcurrency\s*:?\s*([A-Z]{3})\b", re.IGNORECASE)
#: ``Account # Z12-345678``, ``Account Number: 123-456789``, ``Account XXXX-4821``.
_ACCOUNT_LABELLED: Final = re.compile(
    r"\baccount\s{0,3}(?:#|number|no\.?)?\s{0,3}:?\s{0,3}"
    r"(?P<num>(?:[A-Z]{1,3}|[*xX\u2022]{2,12})?[ -]?\d(?:[\d-]| (?=\d)){2,24}\d)(?![\w-])",
    re.IGNORECASE,
)
#: The number ending an all-capitals account title (``INDIVIDUAL - TOD Z12-345678``).
_TITLE_NUMBER: Final = re.compile(r"[A-Z]{0,3}\d{2,4}-\d{4,8}")
_TITLE_WORD: Final = re.compile(r"[A-Z][A-Z&'./]{0,30}")
#: A date at the end of a (normalised) header cell: ``ending market value oct 31 2026``.
_TRAILING_DATE: Final = re.compile(
    r"(?:\sas\sof)?\s(?:[a-z]{3,9}\s\d{1,2}\s\d{4}|\d{1,2}\s\d{1,2}\s\d{2,4}|\d{4}\s\d{1,2}\s\d{1,2})$"
)
#: Characters of a line an account heading is looked for in.
_HEADING_CHARS: Final = 200
_DASHES: Final = frozenset({"-", "\u2013"})
#: Account headings are looked for in a page's first and last lines (where
#: layouts print and repeat them), never mid-page.
HEADING_TOP_LINES: Final = 6
HEADING_BOTTOM_LINES: Final = 3


def read_number(text: str) -> Decimal | None:
    """A quantity or unit price (any number of decimals, ``$``, thousands commas)."""
    match = _NUMBER.fullmatch(text.strip()[:MAX_TOKEN])
    if match is None or bool(match["open"]) != bool(match["close"]):
        return None
    digits = match["int"].replace(",", "")
    if match["dec"]:
        digits = f"{digits}.{match['dec']}"
    try:
        value = Decimal(digits)
    except InvalidOperation:  # pragma: no cover - the pattern only admits digits
        return None
    negative = bool(match["open"]) or match["sign"] in ("-", "\u2212")
    return -value if negative else value


def is_cusip(text: str) -> bool:
    """A CUSIP: 8 characters with at least one digit, then a valid check digit."""
    if _CUSIP.fullmatch(text) is None or not any(c.isdigit() for c in text[:8]):
        return False
    total = 0
    for index, char in enumerate(text[:8]):
        value = int(char) if char.isdigit() else ord(char) - ord("A") + 10
        if index % 2:
            value *= 2
        total += value // 10 + value % 10
    return (10 - total % 10) % 10 == int(text[8])


def account_heading(text: str) -> str | None:
    """The last four digits of the account a heading (a line or a cell) names, or None.

    A heading starts with ``Account # Z12-345678`` / ``Account Number:
    123-456789`` / ``Account XXXX-4821``, after at most an all-capitals
    account title (``INDIVIDUAL - TOD Account # Z12-345678``), or is an
    all-capitals title ending in a number (``INDIVIDUAL - TOD Z12-345678``).
    A text that merely mentions an account (``Linked bank account
    XXXX-9999``) or prints an amount (a row of an accounts overview) is no
    heading. Only the last four digits are returned; the number is not kept.
    """
    text = text[:_HEADING_CHARS]
    if find_amounts(text):
        return None
    tokens = text.split()
    title = 0
    while (
        title < len(tokens)
        and normalise(tokens[title]) != "account"
        and (tokens[title] in _DASHES or _TITLE_WORD.fullmatch(tokens[title]))
    ):
        title += 1
    match = _ACCOUNT_LABELLED.match(" ".join(tokens[title:]))
    number: str | None = match["num"] if match is not None else None
    if (
        number is None
        and 2 <= len(tokens) <= 12
        and _TITLE_NUMBER.fullmatch(tokens[-1])
        and any(_TITLE_WORD.fullmatch(t) for t in tokens[:-1])
        and all(t in _DASHES or _TITLE_WORD.fullmatch(t) for t in tokens[:-1])
    ):
        number = tokens[-1]
    if number is None:
        return None
    digits = "".join(c for c in number if c.isdigit())
    return digits[-4:] if len(digits) >= 4 else None


#: Normalised activity word -> type (the first word(s) of the activity cell).
ACTIVITY_TYPES: Final[Mapping[str, InvestmentActivityType]] = {
    "buy": "buy",
    "bought": "buy",
    "you bought": "buy",
    "purchase": "buy",
    "sell": "sell",
    "sold": "sell",
    "you sold": "sell",
    "sale": "sell",
    "dividend": "dividend",
    "cash dividend": "dividend",
    "dividend received": "dividend",
    "qualified dividend": "dividend",
    "capital gain": "dividend",
    "long term cap gain": "dividend",
    "short term cap gain": "dividend",
    "long term capital gain": "dividend",
    "short term capital gain": "dividend",
    "interest": "interest",
    "interest earned": "interest",
    "fee": "fee",
    "fee charged": "fee",
    "account fee": "fee",
    "commission": "fee",
    "foreign tax paid": "fee",
    "reinvest": "reinvest",
    "reinvestment": "reinvest",
    "dividend reinvestment": "reinvest",
    "split": "split",
    "stock split": "split",
    "transfer in": "transfer_in",
    "shares in": "transfer_in",
    "transfer out": "transfer_out",
    "shares out": "transfer_out",
    "deposit": "deposit",
    "direct deposit": "deposit",
    "electronic funds transfer received": "deposit",
    "contribution": "deposit",
    "withdrawal": "withdrawal",
    "electronic funds transfer paid": "withdrawal",
    "distribution": "withdrawal",
    "normal distribution": "withdrawal",
    "corporate action": "corporate_action",
    "merger": "corporate_action",
    "spinoff": "corporate_action",
    # Fidelity's cash-management and retirement wording (activity exports).
    "redemption": "sell",
    "redeemed": "sell",
    "redemption from core account": "sell",
    "purchase into core account": "buy",
    "check paid": "withdrawal",
    "check received": "deposit",
    "debit card purchase": "withdrawal",
    "bill payment": "withdrawal",
    "bill pay": "withdrawal",
    "direct debit": "withdrawal",
    "cash in lieu": "deposit",
    "return of capital": "deposit",
    "partic contr": "deposit",
    "participant contribution": "deposit",
    "employer contr": "deposit",
    "employer contribution": "deposit",
    "credit interest": "interest",
    "margin interest": "fee",
    "margin interest paid": "fee",
    "advisory fee": "fee",
    "adr fee": "fee",
    "tax withheld": "fee",
    "fed tax withheld": "fee",
    "federal tax withheld": "fee",
    "state tax withheld": "fee",
    "nonresident tax": "fee",
    "non resident tax": "fee",
    "foreign tax": "fee",
}
#: Phrases naming a transfer whose type depends on what moved: ``(type when a
#: security and a quantity are present, type for cash)``.
TRANSFER_TYPES: Final[Mapping[str, tuple[InvestmentActivityType, InvestmentActivityType]]] = {
    "transferred from": ("transfer_in", "deposit"),
    "transferred to": ("transfer_out", "withdrawal"),
}
#: Phrases whose direction only the row's signs tell: ``(units in, units out)``
#: when the row moves a security (by the quantity's sign), else cash in or
#: out by the amount's sign (``deposit``/``withdrawal``). A journal entry,
#: a rollover or a conversion moves units or cash either way; an exchange,
#: an option assignment or expiry is a buy or a sale of units.
SIGNED_TYPES: Final[Mapping[str, tuple[InvestmentActivityType, InvestmentActivityType]]] = {
    "journaled": ("transfer_in", "transfer_out"),
    "journal": ("transfer_in", "transfer_out"),
    "transfer": ("transfer_in", "transfer_out"),
    "transfer of assets": ("transfer_in", "transfer_out"),
    "wire transfer": ("transfer_in", "transfer_out"),
    "electronic funds transfer": ("transfer_in", "transfer_out"),
    "rollover": ("transfer_in", "transfer_out"),
    "roth conversion": ("transfer_in", "transfer_out"),
    "conversion": ("transfer_in", "transfer_out"),
    "exchanged": ("buy", "sell"),
    "exchange": ("buy", "sell"),
    "you exchanged": ("buy", "sell"),
    "assigned": ("buy", "sell"),
    "expired": ("buy", "sell"),
}
#: Types whose cash effect is an outflow when the amount is printed unsigned.
OUTFLOW_TYPES: Final = frozenset({"buy", "fee", "withdrawal", "reinvest"})
#: Types that must name a security / carry a quantity (``investment_txn`` checks).
SECURITY_TYPES: Final = frozenset(
    {"buy", "sell", "split", "transfer_in", "transfer_out", "reinvest"}
)
QUANTITY_TYPES: Final = frozenset({"buy", "sell", "transfer_in", "transfer_out", "reinvest"})
#: Cash rows whose action text is their memo when nothing else describes them.
CASH_MEMO_TYPES: Final = frozenset({"deposit", "withdrawal", "fee", "interest"})


def split_ratio(text: str) -> tuple[Decimal, Decimal] | None:
    """``(new, old)`` from ``2 for 1`` / ``2-for-1`` / ``2:1`` in a split's description."""
    match = _RATIO.search(text[:MAX_TOKEN])
    if match is None:
        return None
    new, old = Decimal(match[1]), Decimal(match[2])
    if new <= 0 or old <= 0 or new == old:
        return None
    return new, old


def activity_type(
    text: str,
    *,
    moves_security: bool = False,
    units_sign: int = 0,
    cash_sign: int = 0,
) -> InvestmentActivityType | None:
    """The type an activity text names (the longest known phrase at its start).

    Case and punctuation do not matter and anything after the phrase is
    ignored, so a broker's action text with the security and a trailing
    ``(Cash)``/``(Margin)`` is typed by its leading words
    (``REINVESTMENT EXAMPLE FUND (EXMPL) (Cash)`` -> ``reinvest``). It reads
    a PDF's activity cell or a CSV's action column alike.

    ``Transferred from``/``Transferred to`` (:data:`TRANSFER_TYPES`) is a
    ``transfer_in``/``transfer_out`` of units when ``moves_security`` (the
    row names a security and a non-zero quantity), else cash: ``deposit``/
    ``withdrawal``.

    A phrase of :data:`SIGNED_TYPES` (``JOURNALED``, ``ROLLOVER``,
    ``EXCHANGED``, ...) takes its direction from the row: with
    ``moves_security`` the sign of the quantity (``units_sign``, negative =
    units leave), else the sign of the amount (``cash_sign``, negative =
    cash leaves: ``withdrawal``, else ``deposit``). Without a sign it is
    None: the direction is never guessed.
    """
    norm = normalise(text)
    best: InvestmentActivityType | None = None
    best_len = 0

    def starts(phrase: str) -> bool:
        return norm == phrase or norm.startswith(f"{phrase} ")

    for phrase, kind in ACTIVITY_TYPES.items():
        if len(phrase) > best_len and starts(phrase):
            best, best_len = kind, len(phrase)
    for phrase, (units, cash) in TRANSFER_TYPES.items():
        if len(phrase) > best_len and starts(phrase):
            best, best_len = (units if moves_security else cash), len(phrase)
    for phrase, (inward, outward) in SIGNED_TYPES.items():
        if len(phrase) > best_len and starts(phrase):
            sign = units_sign if moves_security else cash_sign
            if moves_security:
                best = outward if sign < 0 else inward
            else:
                best = "withdrawal" if sign < 0 else "deposit"
            if sign == 0:
                best = None
            best_len = len(phrase)
    return best


def guess_asset_class(name: str | None) -> AssetClassCode | None:
    """A rough class from the security's name (an institution's own words)."""
    words = set(normalise(name or "").split())
    if {"etf", "exchange", "traded"} & words:
        return "etf"
    if {"money", "market", "cash", "sweep"} & words:
        return "cash_equivalent"
    if {"fund", "funds", "portfolio", "trust"} & words:
        return "mutual_fund"  # a bond fund is a fund
    if {"bond", "bonds", "treasury", "note", "notes", "municipal"} & words:
        return "bond"
    return None


# ---------------------------------------------------------------------------
# Table vocabulary
# ---------------------------------------------------------------------------

#: ``begin`` is a beginning-of-period market value (read for cash groups only);
#: ``other`` a column that is recognised (so its cells are not mistaken for
#: another column's) but not read.
HoldingRole = Literal["symbol", "name", "quantity", "price", "value", "cost", "begin", "other"]
#: ``name`` is a security-name column next to a description column.
ActivityRole = Literal[
    "date",
    "settle",
    "type",
    "description",
    "name",
    "symbol",
    "quantity",
    "price",
    "amount",
    "fees",
    "other",
]

HOLDING_HEADERS: Final[Mapping[str, HoldingRole]] = {
    "symbol": "symbol",
    "ticker": "symbol",
    "symbol cusip": "symbol",
    "cusip": "symbol",
    "description": "name",
    "security": "name",
    "security description": "name",
    "security name": "name",
    "investment": "name",
    "name": "name",
    "quantity": "quantity",
    "shares": "quantity",
    "units": "quantity",
    "qty": "quantity",
    "price": "price",
    "unit price": "price",
    "closing price": "price",
    "price per share": "price",
    "price per unit": "price",
    "market price": "price",
    "market value": "value",
    "current value": "value",
    "ending market value": "value",
    "ending value": "value",
    "value": "value",
    "cost basis": "cost",
    "total cost basis": "cost",
    "total cost": "cost",
    "cost": "cost",
    "book cost": "cost",
    "beginning market value": "begin",
    "beginning value": "begin",
    "unrealized gain loss": "other",
    "unrealized gain": "other",
    "gain loss": "other",
    "eai ey": "other",
    "eai": "other",
    "estimated annual income": "other",
    "accrued interest": "other",
}
ACTIVITY_HEADERS: Final[Mapping[str, ActivityRole]] = {
    "date": "date",
    "trade date": "date",
    "transaction date": "date",
    "settle date": "settle",
    "settlement date": "settle",
    "activity": "type",
    "action": "type",
    "transaction": "type",
    "type": "type",
    "transaction type": "type",
    "description": "description",
    "security": "description",
    "details": "description",
    "security name": "name",
    "symbol": "symbol",
    "symbol cusip": "symbol",
    "cusip": "symbol",
    "quantity": "quantity",
    "shares": "quantity",
    "units": "quantity",
    "price": "price",
    "unit price": "price",
    "amount": "amount",
    "net amount": "amount",
    "fees": "fees",
    "commission": "fees",
    "transaction cost": "fees",
    "reference": "other",
    "account type": "other",
    "balance": "other",
}
#: Asset-class group headings of a holdings table (normalised) -> the class
#: of the rows under them. ``Total <group>`` lines are subtotals.
ASSET_GROUPS: Final[Mapping[str, AssetClassCode | None]] = {
    "mutual funds": "mutual_fund",
    "exchange traded products": "etf",
    "exchange traded funds": "etf",
    "etfs": "etf",
    "stocks": "equity",
    "equities": "equity",
    "common stock": "equity",
    "bonds": "bond",
    "fixed income": "bond",
    "options": "option",
    "money market funds": "cash_equivalent",
    "core account": "cash_equivalent",
    "cash and cash equivalents": "cash_equivalent",
    "other": None,
}
#: ``Total <these>`` in a holdings table is the grand total (beats an unlabelled ``Total``).
GRAND_TOTALS: Final = ("holdings", "market value", "securities", "investments", "portfolio")


@dataclass(frozen=True)
class _Column[R]:
    role: R
    x0: float
    x1: float


@dataclass(frozen=True)
class _HeaderCell:
    """A header cell, possibly over several lines (``Price Per`` / ``Unit``)."""

    parts: tuple[str, ...]
    x0: float
    x1: float


def _header_cells(lines: Sequence[ViewLine]) -> list[_HeaderCell]:
    """The first line's cells, with the words of the lines below each one appended."""
    cells = [_HeaderCell((c.text,), c.x0, c.x1) for c in lines[0].cells]
    for line in lines[1:]:
        for cell in line.cells:
            overlaps = [cell.overlap(h.x0, h.x1) for h in cells]
            best = max(range(len(cells)), key=lambda i: overlaps[i]) if cells else -1
            if best >= 0 and overlaps[best] > 0:
                h = cells[best]
                cells[best] = _HeaderCell(
                    (*h.parts, cell.text), min(h.x0, cell.x0), max(h.x1, cell.x1)
                )
            else:
                cells.append(_HeaderCell((cell.text,), cell.x0, cell.x1))
    return sorted(cells, key=lambda h: h.x0)


def header_key[R](parts: Sequence[str], headers: Mapping[str, R]) -> R | None:
    """The role a header cell names: the most of its lines that are a known header.

    ``Ending Market Value`` over ``Oct 31, 2026`` and ``Price Per`` over
    ``Unit`` are read whole; a trailing date (``... as of 10/31/2026``) is
    dropped; a line below that is no part of the header (a group heading)
    is left off.
    """
    for count in range(len(parts), 0, -1):
        text = normalise(" ".join(parts[:count]))
        for candidate in (text, _TRAILING_DATE.sub("", text)):
            if candidate in headers:
                return headers[candidate]
    return None


def _columns[R](cells: Sequence[_HeaderCell], headers: Mapping[str, R]) -> list[_Column[R]]:
    found: list[_Column[R]] = []
    roles: set[R] = set()
    for cell in cells:
        role = header_key(cell.parts, headers)
        if role is None or (role in roles and role != "other"):
            continue
        roles.add(role)
        found.append(_Column(role, cell.x0, cell.x1))
    return sorted(found, key=lambda c: c.x0)


def _known_header(text: str) -> bool:
    parts = (text,)
    return header_key(parts, HOLDING_HEADERS) is not None or (
        header_key(parts, ACTIVITY_HEADERS) is not None
    )


def _header_like(line: ViewLine) -> bool:
    """Three or more cells, all header words, no amount: a table header we cannot use."""
    return (
        len(line.cells) >= 3
        and not find_amounts(line.text)
        and all(_known_header(c.text) for c in line.cells)
    )


def _assign[R](line: ViewLine, columns: Sequence[_Column[R]]) -> dict[R, list[Cell]]:
    out: dict[R, list[Cell]] = {}
    for cell in line.cells:
        best: _Column[R] | None = None
        best_overlap = 0.0
        for column in columns:
            overlap = cell.overlap(column.x0, column.x1)
            if overlap > best_overlap:
                best, best_overlap = column, overlap
        if best is None and columns:
            nearest = min(columns, key=lambda c: abs((c.x0 + c.x1) / 2 - cell.centre))
            if abs((nearest.x0 + nearest.x1) / 2 - cell.centre) < max(3 * line.height, 30.0):
                best = nearest
        if best is not None:
            out.setdefault(best.role, []).append(cell)
    return out


def _text(cells: list[Cell] | None) -> str:
    return " ".join(c.text for c in cells or ()).strip()


def _ocr[R](cells: Mapping[R, list[Cell]]) -> float | None:
    values = [c.conf for group in cells.values() for c in group if c.conf is not None]
    return min(values) if values else None


def _starts(norm: str, phrase: str) -> bool:
    return norm == phrase or norm.startswith(f"{phrase} ")


# ---------------------------------------------------------------------------
# Collected rows
# ---------------------------------------------------------------------------


@dataclass
class _HoldingRow:
    page: int
    bbox: tuple[float, float, float, float]
    symbol: str | None
    name: str | None
    quantity: Decimal
    price: Decimal | None
    value: Amount | None
    cost: Amount | None
    ocr: float | None
    begin: Amount | None = None
    cusip: str | None = None
    #: The asset-class group heading the row is under (normalised).
    group: str | None = None
    #: The name cell as printed (continuation lines appended).
    raw_name: str | None = None
    #: The symbol came from a symbol column (not from the name).
    symbol_column: bool = False


@dataclass
class _ActivityRow:
    page: int
    bbox: tuple[float, float, float, float]
    date_text: str
    settle_text: str
    type: InvestmentActivityType
    description: str
    symbol: str | None
    quantity: Decimal | None
    price: Decimal | None
    amount: Amount
    fees: Amount | None
    ocr: float | None
    cusip: str | None = None


@dataclass
class _Summary:
    period: PeriodCandidates = field(default_factory=PeriodCandidates)
    as_of_text: str | None = None
    last4: str | None = None
    currency: str | None = None
    cash_opening: Amount | None = None
    cash_closing: Amount | None = None
    positions_value: Amount | None = None
    total_value: Amount | None = None


@dataclass
class _Collected:
    """What one account's lines yield (the whole document for one account)."""

    holdings: list[_HoldingRow] = field(default_factory=list)
    activity: list[_ActivityRow] = field(default_factory=list)
    #: Rows under a cash group heading (``cash_groups``): the account's cash.
    cash: list[_HoldingRow] = field(default_factory=list)
    summary: _Summary = field(default_factory=_Summary)
    skipped: int = 0
    unknown_activity: int = 0
    holdings_header: bool = False
    activity_header: bool = False
    # -- reading state ------------------------------------------------------------
    holdings_columns: list[_Column[HoldingRole]] | None = None
    activity_columns: list[_Column[ActivityRole]] | None = None
    group: str | None = None
    last_holding: _HoldingRow | None = None
    #: Inside a table the layout says not to read (``ignored_tables``).
    ignoring: bool = False
    #: Where the holdings total came from: an unlabelled or a ``Total Holdings`` line.
    table_total: Literal["plain", "grand"] | None = None

    @property
    def found_table(self) -> bool:
        return self.holdings_header or self.activity_header


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


class BrokerageStatementParser(DocumentParser):
    """Generic brokerage statement: holdings and activity tables, one or more accounts.

    **Accounts.** A heading naming an account (:func:`account_heading`, on
    the line or in one of its cells) among a page's first
    :data:`HEADING_TOP_LINES` or last :data:`HEADING_BOTTOM_LINES` lines
    marks where that account's pages are; a mention mid-page is no heading.
    When the headings name one account (or
    none) the document is one statement, read exactly as a single-account
    statement always was (its last four digits from the first account
    number found, else from the heading). When they name two or more, each
    account is its own :class:`InvestmentStatement` (own last four digits,
    positions, activity, cash, totals and reconciliation):

    * a page whose headings all name one account belongs to it entirely (a
      heading repeated at the top or the bottom of each page);
    * on a page naming several, the lines before the first heading continue
      the account of the page before, and each heading starts its account;
    * a page with no heading continues the account before it; the pages
      before the first heading (an overview of all accounts) give only the
      period, currency and "as of" date every account falls back on, and a
      holdings or activity row there belongs to no account (skipped,
      ``rows_outside_account``);
    * an account without a holdings or activity table gives no statement
      (``account_without_tables``); at most :data:`MAX_STATEMENTS` accounts.

    Misplaced lines cannot pass silently: they break that account's
    reconciliation, and the importer holds a statement that does not
    reconcile.
    """

    name: ClassVar[str] = "brokerage_statement_generic"
    version: ClassVar[str] = "2"
    kind: ClassVar[Literal["statement", "payslip", "receipt"]] = "statement"
    display_name: ClassVar[str | None] = None
    #: Phrases on the first page that identify the institution (its name first).
    fingerprints: ClassVar[tuple[str, ...]] = ()
    #: Every fingerprint phrase must be on page 1 (not only the name): for an
    #: institution whose name other documents print (a payee, a transfer).
    all_fingerprints: ClassVar[bool] = False
    account_type: ClassVar[str] = "brokerage"
    default_currency: ClassVar[str | None] = None
    date_styles: ClassVar[tuple[DateStyle, ...]] = ()
    period_styles: ClassVar[tuple[DateStyle, ...]] = FULL_STYLES
    base: ClassVar[float] = conf.GENERIC
    period_labels: ClassVar[tuple[str, ...]] = (
        "statement period",
        "for the period",
        "statement from",
        "period",
    )
    #: Summary labels in the order they are tried on a line (longest first);
    #: ``ignore`` consumes a label no slot takes (a beginning account value
    #: is not the account value).
    summary_labels: ClassVar[tuple[tuple[str, str], ...]] = (
        ("beginning account value", "ignore"),
        ("beginning net value", "ignore"),
        ("beginning portfolio value", "ignore"),
        ("beginning cash balance", "cash_opening"),
        ("opening cash balance", "cash_opening"),
        ("starting cash balance", "cash_opening"),
        ("cash balance beginning", "cash_opening"),
        ("ending cash balance", "cash_closing"),
        ("closing cash balance", "cash_closing"),
        ("cash balance ending", "cash_closing"),
        ("total holdings value", "positions_value"),
        ("total market value", "positions_value"),
        ("total securities value", "positions_value"),
        ("total holdings", "positions_value"),
        ("holdings value", "positions_value"),
        ("ending account value", "total_value"),
        ("ending net value", "total_value"),
        ("total account value", "total_value"),
        ("total portfolio value", "total_value"),
        ("account value", "total_value"),
    )
    skip_prefixes: ClassVar[tuple[str, ...]] = ("page ", "continued", "subtotal")
    #: Read an activity table's description as the action when it has no
    #: action column (always; the generic parser only when the table also
    #: names a security, a symbol or a quantity, so a bank table stays a bank's).
    description_types: ClassVar[bool] = False
    #: Holdings groups whose rows are the account's cash (a core/sweep money
    #: market position): beginning and ending market values are the opening
    #: and closing cash, they are no position, and the printed holdings totals
    #: include them (subtracted for the holdings check).
    cash_groups: ClassVar[tuple[str, ...]] = ()
    #: Headings (normalised prefixes) of tables not to read (their rows would
    #: count the same money twice), until one of ``section_headings``.
    ignored_tables: ClassVar[tuple[str, ...]] = ()
    section_headings: ClassVar[tuple[str, ...]] = ()

    # -- detection ----------------------------------------------------------------

    @staticmethod
    def holdings_columns(line: ViewLine) -> list[_Column[HoldingRole]] | None:
        return BrokerageStatementParser._holdings_from(_header_cells([line]))

    @staticmethod
    def _holdings_from(cells: Sequence[_HeaderCell]) -> list[_Column[HoldingRole]] | None:
        found = _columns(cells, HOLDING_HEADERS)
        roles = {c.role for c in found}
        if {"quantity", "value"} <= roles and roles & {"symbol", "name"}:
            return found
        return None

    @classmethod
    def activity_columns(cls, line: ViewLine) -> list[_Column[ActivityRole]] | None:
        return cls._activity_from(_header_cells([line]))

    @classmethod
    def _activity_from(cls, cells: Sequence[_HeaderCell]) -> list[_Column[ActivityRole]] | None:
        found = _columns(cells, ACTIVITY_HEADERS)
        roles = {c.role for c in found}
        if "date" not in roles and "settle" in roles:
            # Only a settlement date: it dates the row.
            found = [_Column("date", c.x0, c.x1) if c.role == "settle" else c for c in found]
            roles = {c.role for c in found}
        if (
            "type" not in roles
            and "description" in roles
            and (cls.description_types or roles & {"symbol", "name", "quantity"})
        ):
            # "You Bought", "Dividend Received": the description is the action.
            found = [_Column("type", c.x0, c.x1) if c.role == "description" else c for c in found]
            roles = {c.role for c in found}
        if {"date", "type", "amount"} <= roles:
            return found
        return None

    def _header_at(
        self, lines: Sequence[ViewLine], index: int
    ) -> tuple[list[_Column[HoldingRole]] | None, list[_Column[ActivityRole]] | None, int] | None:
        """A table header starting at ``lines[index]`` over up to three lines.

        Returns ``(holdings, activity, lines used)``. A header over several
        lines (``Price Per`` / ``Unit``, a date under ``Ending Market
        Value``) is used when it names more columns than its first line
        alone; a later line with an amount is a row, never header.
        """
        first = lines[index]
        best: tuple[list[_Column[Any]], bool, int] | None = None
        for count in (1, 2, 3):
            group = lines[index : index + count]
            if len(group) < count or group[-1].page != first.page:
                break
            if count > 1:
                if find_amounts(group[-1].text):
                    break
                if count == 2 and (
                    find_amounts(first.text)
                    or sum(1 for c in first.cells if _known_header(c.text)) < 2
                ):
                    break
            cells = _header_cells(group)
            holdings = self._holdings_from(cells)
            columns: list[_Column[Any]] | None = holdings
            if columns is None:
                columns = self._activity_from(cells)
            if columns is not None and (best is None or len(columns) > len(best[0])):
                best = (columns, holdings is not None, count)
        if best is None:
            return None
        columns, is_holdings, count = best
        if is_holdings:
            return columns, None, count
        return None, columns, count

    def fingerprint_score(self, view: DocumentView) -> float:
        if not self.fingerprints:
            return 0.0
        head = f" {view.head_text()} "
        hits = sum(1 for phrase in self.fingerprints if f" {normalise(phrase)} " in head)
        return hits / len(self.fingerprints)

    def _tables(self, view: DocumentView) -> tuple[bool, bool]:
        holdings = activity = False
        for index in range(len(view.lines)):
            found = self._header_at(view.lines, index)
            if found is not None:
                holdings = holdings or found[0] is not None
                activity = activity or found[1] is not None
                if holdings and activity:
                    break
        return holdings, activity

    def detect(self, view: DocumentView) -> float:
        if not self.fingerprints:
            holdings, activity = self._tables(view)
            # Above the bank-statement parsers (0.4/0.45) for the same activity table.
            if holdings:
                return 0.7 if activity else 0.6
            return 0.5 if activity else 0.0
        share = self.fingerprint_score(view)
        if f" {normalise(self.fingerprints[0])} " not in f" {view.head_text()} ":
            return 0.0  # the institution's own name (the first phrase) must be there
        if self.all_fingerprints and share < 1:
            return 0.0
        holdings, activity = self._tables(view)
        score = 0.5 + 0.4 * share
        return round(score + 0.1 if holdings or activity else score * 0.6, 4)

    # -- accounts -----------------------------------------------------------------

    def _sections(self, view: DocumentView) -> tuple[list[_Collected], _Collected | None, int]:
        """``(accounts, preamble, accounts dropped)`` (class doc); one account: no preamble."""
        pages: dict[int, list[ViewLine]] = {}
        for line in view.lines:
            pages.setdefault(line.page, []).append(line)
        headings: dict[int, str | None] = {}
        for page_lines in pages.values():
            last = len(page_lines) - HEADING_BOTTOM_LINES
            for position, line in enumerate(page_lines):
                zone = position < HEADING_TOP_LINES or position >= last
                headings[line.index] = self._heading(line) if zone else None
        named = list(dict.fromkeys(v for v in headings.values() if v is not None))
        if len(named) < 2:
            single = _Collected()
            self._read(view.lines, single)
            if single.summary.last4 is None and named:
                single.summary.last4 = named[0]
            return [single], None, 0
        routed: dict[str | None, list[ViewLine]] = {None: []}
        current: str | None = None
        for page_lines in pages.values():
            marks = [headings[line.index] for line in page_lines if headings[line.index]]
            if marks and len(set(marks)) == 1:
                current = marks[0]
                routed.setdefault(current, []).extend(page_lines)
                continue
            for line in page_lines:
                mark = headings[line.index]
                if mark is not None:
                    current = mark
                routed.setdefault(current, []).append(line)
        preamble = _Collected()
        self._read(routed.pop(None), preamble)
        accounts: list[_Collected] = []
        for last4, lines in routed.items():
            collected = _Collected()
            self._read(lines, collected)
            collected.summary.last4 = last4
            accounts.append(collected)
        kept = accounts[:MAX_STATEMENTS]
        return kept, preamble, len(accounts) - len(kept)

    @staticmethod
    def _heading(line: ViewLine) -> str | None:
        """The account a line names (the line, else one of its cells; no amounts)."""
        if find_amounts(line.text):
            return None
        found = account_heading(line.text)
        if found is None and len(line.cells) > 1:
            found = next(
                (h for c in line.cells if (h := account_heading(c.text)) is not None), None
            )
        return found

    # -- collecting -----------------------------------------------------------------

    def _scan_summary(self, line: ViewLine, summary: _Summary) -> None:
        for label, slot in self.summary_labels:
            end = label_end(line.text, label)
            if end is None:
                continue
            amounts = find_amounts(line.text[end:])
            if slot != "ignore" and amounts and getattr(summary, slot) is None:
                setattr(summary, slot, amounts[0])
            break
        summary.period.scan(line.text, self.period_labels)
        if summary.as_of_text is None:
            end = label_end(line.text, "as of")
            if end is not None:
                summary.as_of_text = line.text[end:]
        if summary.last4 is None:
            match = _LAST4.search(line.text)
            if match is not None:
                summary.last4 = next(g for g in match.groups() if g)
        if summary.currency is None:
            match = _CURRENCY.search(line.text)
            if match is not None:
                summary.currency = match[1].upper()

    @staticmethod
    def _name_symbol(raw: str | None) -> tuple[str | None, str | None]:
        """``(symbol, name)`` from a name cell (``... (EXMPL)`` or a leading ticker)."""
        if raw is None:
            return None, None
        symbol, name = _split_symbol(raw)
        if symbol is not None and not _SYMBOL.fullmatch(symbol):
            symbol = None
        return symbol, name

    def _holding(
        self, line: ViewLine, cells: dict[HoldingRole, list[Cell]], group: str | None
    ) -> _HoldingRow | None:
        quantity = read_number(_text(cells.get("quantity")))
        if quantity is None:
            return None
        raw_name = _text(cells.get("name")) or None
        name = raw_name
        symbol, cusip = _identifier(_text(cells.get("symbol")))
        symbol_column = symbol is not None or cusip is not None
        if not symbol_column:
            symbol, name = self._name_symbol(raw_name)
        if symbol is None and cusip is None and name is None:
            return None
        price_text = _text(cells.get("price"))
        return _HoldingRow(
            page=line.page,
            bbox=line.bbox,
            symbol=symbol,
            name=name,
            quantity=quantity,
            price=read_number(price_text) if price_text else None,
            value=read_amount(_text(cells.get("value"))),
            cost=read_amount(_text(cells.get("cost"))),
            ocr=_ocr(cells),
            begin=read_amount(_text(cells.get("begin"))),
            cusip=cusip,
            group=group,
            raw_name=raw_name,
            symbol_column=symbol_column,
        )

    def _continue_holding(
        self, line: ViewLine, cells: dict[HoldingRole, list[Cell]], row: _HoldingRow
    ) -> bool:
        """A wrapped security name (``EXAMPLE TOTAL MARKET`` / ``INDEX FUND (EXMPL)``)."""
        if not cells or not set(cells) <= {"name", "symbol"} or find_amounts(line.text):
            return False
        extra = _text(cells.get("name"))
        if not extra:
            return False
        row.raw_name = f"{row.raw_name or ''} {extra}".strip()[:200]
        if row.symbol_column:
            row.name = row.raw_name
        else:
            row.symbol, row.name = self._name_symbol(row.raw_name)
        return True

    def _group(self, norm: str, *, prefix: bool = False) -> str | None:
        """The asset-class group a heading names (``prefix``: a ``Total <group> ...`` rest)."""
        if norm.endswith(" continued"):
            norm = norm[: -len(" continued")]
        for group in (*self.cash_groups, *ASSET_GROUPS):
            if norm == group or (prefix and norm.startswith(f"{group} ")):
                return group
        return None

    def _holdings_total(self, line: ViewLine, collected: _Collected) -> None:
        rest = line.norm[len("total") :].strip()
        if self._group(rest, prefix=True) is not None:
            return  # "Total Mutual Funds": a group's subtotal
        columns = collected.holdings_columns or []
        value = read_amount(_text(_assign(line, columns).get("value")))
        if value is None:
            amounts = find_amounts(line.text)
            value = amounts[-1] if amounts else None
        if value is None:
            return
        summary = collected.summary
        if any(_starts(rest, g) for g in GRAND_TOTALS):
            if collected.table_total != "grand" and (
                summary.positions_value is None or collected.table_total == "plain"
            ):
                summary.positions_value = value
            collected.table_total = "grand"
        elif summary.positions_value is None:
            summary.positions_value = value
            collected.table_total = "plain"

    def _activity(
        self, line: ViewLine, cells: dict[ActivityRole, list[Cell]], collected: _Collected
    ) -> None:
        date_text = _text(cells.get("date"))
        if not date_text or not _date_shaped(date_text):
            return
        type_text = _text(cells.get("type"))
        name = _text(cells.get("name"))
        symbol, cusip = _identifier(_text(cells.get("symbol")))
        quantity_text = _text(cells.get("quantity"))
        quantity = read_number(quantity_text) if quantity_text else None
        # A transfer moves units when the row names a security and a quantity.
        named = symbol or cusip or name or _text(cells.get("description"))
        moves = bool(named) and bool(quantity)
        kind = activity_type(type_text, moves_security=moves)
        amount_text = _text(cells.get("amount"))
        amount = read_amount(amount_text)
        if kind is None:
            collected.unknown_activity += 1
            collected.skipped += 1
            return
        if (
            amount is None
            and kind in ("transfer_in", "transfer_out")
            and quantity
            and amount_text.strip("-\u2013 ") == ""
        ):
            amount = Amount(value=Decimal(0))  # units moved, no cash ("-")
        if amount is None:
            collected.skipped += 1
            return
        description = name or _text(cells.get("description"))
        if not description and kind in CASH_MEMO_TYPES:
            # A deposit table's action ("Electronic Funds Transfer Received") is its memo.
            description = type_text
        price_text = _text(cells.get("price"))
        fees_text = _text(cells.get("fees"))
        collected.activity.append(
            _ActivityRow(
                page=line.page,
                bbox=line.bbox,
                date_text=date_text,
                settle_text=_text(cells.get("settle")),
                type=kind,
                description=description,
                symbol=symbol,
                quantity=quantity,
                price=read_number(price_text) if price_text else None,
                amount=amount,
                fees=read_amount(fees_text) if fees_text else None,
                ocr=_ocr(cells),
                cusip=cusip,
            )
        )

    def _is_heading(self, line: ViewLine, phrases: tuple[str, ...]) -> bool:
        if not phrases or len(line.cells) > 2 or find_amounts(line.text):
            return False
        return any(_starts(line.norm, p) for p in phrases)

    def _read(self, lines: Sequence[ViewLine], collected: _Collected) -> None:
        """Read one account's lines (or the whole document) into ``collected``."""
        index = 0
        while index < len(lines):
            line = lines[index]
            found = self._header_at(lines, index)
            if found is not None:
                holdings, activity, used = found
                index += used
                if collected.ignoring:
                    collected.holdings_columns = collected.activity_columns = None
                elif holdings is not None:
                    collected.holdings_columns, collected.activity_columns = holdings, None
                    collected.holdings_header = True
                    collected.group = None
                    collected.last_holding = None
                else:
                    collected.holdings_columns, collected.activity_columns = None, activity
                    collected.activity_header = True
                continue
            index += 1
            if self._is_heading(line, self.ignored_tables):
                collected.ignoring = True
                collected.holdings_columns = collected.activity_columns = None
                continue
            if collected.ignoring:
                if not self._is_heading(line, self.section_headings):
                    continue
                collected.ignoring = False
            self._line(line, collected)

    def _line(self, line: ViewLine, collected: _Collected) -> None:
        norm = line.norm
        if any(norm.startswith(prefix.strip()) for prefix in self.skip_prefixes):
            return
        holdings, activity = collected.holdings_columns, collected.activity_columns
        if (holdings is not None or activity is not None) and _header_like(line):
            # A table header we cannot read: the table above ends here.
            collected.holdings_columns = collected.activity_columns = None
            return
        if holdings is not None:
            group = self._group(norm)
            if group is not None and not find_amounts(line.text):
                collected.group = group
                collected.last_holding = None
                return
            if norm.startswith("total"):
                self._holdings_total(line, collected)
                collected.last_holding = None
                return
            cells = _assign(line, holdings)
            row = self._holding(line, cells, collected.group)
            if row is not None:
                if collected.group is not None and collected.group in self.cash_groups:
                    collected.cash.append(row)
                else:
                    collected.holdings.append(row)
                collected.last_holding = row
                return
            last = collected.last_holding
            if last is not None and self._continue_holding(line, cells, last):
                return
            collected.last_holding = None
        elif activity is not None:
            if norm.startswith("total"):
                return
            before = len(collected.activity) + collected.skipped
            self._activity(line, _assign(line, activity), collected)
            if len(collected.activity) + collected.skipped != before:
                return
        self._scan_summary(line, collected.summary)

    # -- dates ----------------------------------------------------------------------

    def _styles(
        self, rows: Sequence[_ActivityRow], hint: str | None
    ) -> tuple[tuple[DateStyle, ...], bool]:
        if self.date_styles:
            return self.date_styles, True
        numeric = [r.date_text for r in rows if numeric_parts(r.date_text) is not None]
        if not numeric:
            return ("d_mon_y", "mon_d_y", "ymd", "mdy"), True
        style, certain = guess_numeric_style(numeric)
        if hint in NUMERIC_DM and (not certain or style == hint):
            style, certain = hint, True
        chosen: DateStyle = style if style is not None else "mdy"
        # ``MM/DD`` rows take their year from the period (read_date's window).
        yearless: DateStyle = "md" if chosen == "mdy" else "dm"
        return (chosen, yearless, "ymd", "d_mon_y", "mon_d_y"), certain

    def _period(
        self, summary: _Summary, styles: tuple[DateStyle, ...], view: DocumentView
    ) -> tuple[date, date] | None:
        ordered: list[DateStyle] = [s for s in self.period_styles if s in FULL_STYLES]
        for style in styles:
            numeric = full_style(style)
            if numeric in NUMERIC_DM and numeric in ordered:
                ordered.remove(numeric)
                ordered.insert(0, numeric)
        return read_period(summary.period.texts, ordered, view)

    @staticmethod
    def _read_date(
        text: str, styles: tuple[DateStyle, ...], window: tuple[date, date] | None
    ) -> date | None:
        for style in styles:
            found = read_date(text, style, window=window)
            if found is not None:
                return found
        return None

    # -- the statement --------------------------------------------------------------

    def _security(
        self,
        symbol: str | None,
        name: str | None,
        known: Mapping[str, str | None],
        *,
        cusip: str | None = None,
        asset_class: AssetClassCode | None = None,
    ) -> SecurityRef | None:
        if symbol is None and cusip is None:
            return None
        clean_name = (name or (known.get(symbol) if symbol else None) or "").strip()[:200] or None
        try:
            return SecurityRef(
                symbol=symbol,
                cusip=cusip,
                name=clean_name,
                asset_class=asset_class or guess_asset_class(clean_name),
            )
        except ValidationError:
            return None

    def _positions(
        self, collected: _Collected, as_of: date | None
    ) -> tuple[list[InvestmentPosition], list[dict[str, float]]]:
        positions: list[InvestmentPosition] = []
        scores: list[dict[str, float]] = []
        for row in collected.holdings:
            security = self._security(
                row.symbol,
                row.name,
                {},
                cusip=row.cusip,
                asset_class=ASSET_GROUPS.get(row.group) if row.group else None,
            )
            if security is None:
                collected.skipped += 1
                continue
            score = {
                "security": conf.scaled(self.base, row.ocr),
                "quantity": conf.scaled(self.base, row.ocr),
                "market_value": conf.scaled(
                    self.base if row.value is not None else conf.MISSING, row.ocr
                ),
            }
            if row.price is not None:
                score["unit_price"] = conf.scaled(self.base, row.ocr)
            positions.append(
                InvestmentPosition(
                    security=security,
                    quantity=row.quantity,
                    unit_price=row.price,
                    market_value=row.value.value if row.value is not None else None,
                    cost_basis=abs(row.cost.value) if row.cost is not None else None,
                    price_as_of=as_of,
                    page=row.page,
                    bbox=row.bbox,
                )
            )
            scores.append(score)
        return positions, scores

    def _activities(
        self,
        collected: _Collected,
        styles: tuple[DateStyle, ...],
        certain: bool,
        period: tuple[date, date] | None,
        known: Mapping[str, str | None],
    ) -> tuple[list[InvestmentActivity], list[dict[str, float]]]:
        rows: list[InvestmentActivity] = []
        scores: list[dict[str, float]] = []
        for row in collected.activity:
            trade = self._read_date(row.date_text, styles, period)
            if trade is None:
                collected.skipped += 1
                continue
            settle = self._read_date(row.settle_text, styles, period) if row.settle_text else None
            symbol, name = row.symbol, row.description or None
            if symbol is None and row.cusip is None and name is not None:
                lead, rest = _split_symbol(name)
                if lead is not None and (lead in known or row.type in SECURITY_TYPES):
                    symbol, name = lead, rest
            security = self._security(symbol, name, known, cusip=row.cusip)
            amount = row.amount.value
            if not row.amount.negative_marked and row.type in OUTFLOW_TYPES:
                amount = -abs(amount)
            quantity = abs(row.quantity) if row.quantity is not None else None
            ratio = split_ratio(row.description) if row.type == "split" else None
            if row.type == "split":
                quantity = None
            date_conf = self.base if certain else conf.GUESSED
            if period is not None and not (period[0] <= trade <= period[1]):
                date_conf = min(date_conf, conf.INFERRED)
            score = {
                "trade_date": conf.scaled(date_conf, row.ocr),
                "type": conf.scaled(self.base, row.ocr),
                "amount": conf.scaled(self.base, row.ocr),
            }
            if row.type in SECURITY_TYPES:
                score["security"] = conf.scaled(
                    self.base if security is not None else conf.MISSING, row.ocr
                )
            if row.type == "split":
                score["ratio"] = conf.scaled(
                    self.base if ratio is not None else conf.MISSING, row.ocr
                )
            if row.type in QUANTITY_TYPES:
                score["quantity"] = conf.scaled(
                    self.base if quantity is not None and quantity > 0 else conf.MISSING, row.ocr
                )
            fees = abs(row.fees.value) if row.fees is not None else Decimal(0)
            rows.append(
                InvestmentActivity(
                    type=row.type,
                    trade_date=trade,
                    settle_date=settle if settle is not None and settle >= trade else None,
                    security=security,
                    quantity=quantity if quantity is None or quantity != 0 else None,
                    unit_price=abs(row.price) if row.price is not None else None,
                    split_ratio=ratio,
                    amount=amount,
                    fees=fees,
                    memo=(row.description or None),
                    page=row.page,
                    bbox=row.bbox,
                )
            )
            scores.append(score)
        return rows, scores

    @staticmethod
    def _cash_from_groups(collected: _Collected) -> tuple[Amount | None, Amount | None, Decimal]:
        """``(opening, closing, closing total)`` from the cash group rows."""
        if not collected.cash:
            return None, None, Decimal(0)
        ends = [r.value.value for r in collected.cash if r.value is not None]
        begins = [r.begin.value for r in collected.cash if r.begin is not None]
        closing = sum_amounts(ends) if len(ends) == len(collected.cash) else None
        opening = sum_amounts(begins) if len(begins) == len(collected.cash) else None
        return (
            Amount(value=opening) if opening is not None else None,
            Amount(value=closing) if closing is not None else None,
            closing if closing is not None else Decimal(0),
        )

    def _statement(
        self,
        view: DocumentView,
        collected: _Collected,
        fallback: _Summary | None,
        threshold: float,
        date_order: str | None,
    ) -> tuple[InvestmentStatement, bool]:
        """One account's statement and whether its dates were read with certainty."""
        summary = collected.summary
        if fallback is not None:
            for slot in ("as_of_text", "currency"):
                if getattr(summary, slot) is None:
                    setattr(summary, slot, getattr(fallback, slot))
            # The account's own labelled lines first, then the document's.
            summary.period = PeriodCandidates([*summary.period.texts, *fallback.period.texts])
        styles, certain = self._styles(collected.activity, date_order)
        period = self._period(summary, styles, view)
        as_of: date | None = None
        if summary.as_of_text is not None:
            found = find_dates(summary.as_of_text, styles)
            as_of = found[0] if found else None
        statement_conf: dict[str, float] = {}
        if as_of is None and period is not None:
            as_of = period[1]
        positions, position_scores = self._positions(collected, as_of)
        known = {p.security.symbol: p.security.name for p in positions if p.security.symbol}
        activity, activity_scores = self._activities(collected, styles, certain, period, known)
        if period is not None:
            statement_conf["period"] = self.base
        elif activity:
            period = (min(a.trade_date for a in activity), max(a.trade_date for a in activity))
            statement_conf["period"] = min(
                [conf.INFERRED, *(s["trade_date"] for s in activity_scores)]
            )
        currency = summary.currency or self.default_currency
        cash_opening, cash_closing = summary.cash_opening, summary.cash_closing
        positions_value = summary.positions_value
        group_open, group_close, group_total = self._cash_from_groups(collected)
        if collected.cash:
            if cash_opening is None:
                cash_opening = group_open
            if cash_closing is None:
                cash_closing = group_close
            if positions_value is not None:
                # The printed holdings total includes the cash group rows.
                positions_value = replace(
                    positions_value, value=positions_value.value - group_total
                )
        statement_conf["account_number"] = self.base if summary.last4 else conf.MISSING
        statement_conf["currency"] = self.base if currency else conf.MISSING
        values = {
            "cash_opening": cash_opening,
            "cash_closing": cash_closing,
            "positions_value": positions_value,
            "total_value": summary.total_value,
        }
        for slot, value in values.items():
            statement_conf[slot] = self.base if value is not None else conf.MISSING

        def balance(value: Amount | None, on: date | None) -> Balance | None:
            return Balance(amount=value.value, as_of=on) if value is not None else None

        statement = InvestmentStatement(
            source_format=view.source_format,
            account=StatementAccount(
                account_type=self.account_type,
                last4=summary.last4,
                currency=currency,
                institution=self.display_name,
            ),
            period_start=period[0] if period else None,
            period_end=period[1] if period else None,
            as_of=as_of,
            positions=positions,
            activity=activity,
            cash_opening=balance(cash_opening, period[0] if period else None),
            cash_closing=balance(cash_closing, as_of),
            positions_value=balance(positions_value, as_of),
            total_value=balance(summary.total_value, as_of),
            truncated=view.truncated,
        )
        check = reconcile_investment(statement)
        for index in check.position_mismatches:
            position_scores[index]["market_value"] = conf.CONTRADICTED
        cap = {"mismatch": conf.UNRECONCILED, "unavailable": conf.UNCHECKED}.get(check.status)
        if cap is not None:
            for score in (*position_scores, *activity_scores):
                score["reconciliation"] = cap
        statement_conf["reconciliation"] = cap if cap is not None else 1.0
        statement = statement.model_copy(
            update={
                "positions": [
                    p.model_copy(
                        update={
                            "confidence": min(s.values()),
                            "low_confidence": conf.low_fields(s, threshold),
                        }
                    )
                    for p, s in zip(positions, position_scores, strict=True)
                ],
                "activity": [
                    a.model_copy(
                        update={
                            "confidence": min(s.values()),
                            "low_confidence": conf.low_fields(s, threshold),
                        }
                    )
                    for a, s in zip(activity, activity_scores, strict=True)
                ],
                "parser": ParserRef(
                    name=self.name,
                    version=self.version,
                    institution=self.institution,
                    kind="statement",
                ),
                "confidence": {k: round(v, 4) for k, v in statement_conf.items()},
                "reconciliation": check,
            }
        )
        return statement, certain

    def parse(
        self, view: DocumentView, *, threshold: float, date_order: str | None = None
    ) -> ParseOutcome:
        accounts, preamble, dropped = self._sections(view)
        outcome = ParseOutcome()
        if not any(c.found_table for c in accounts):
            outcome.warnings.append("no_table_header")
            return outcome
        warnings: list[str] = []
        skipped = 0
        unknown = 0
        guessed = False
        for collected in accounts:
            if not collected.found_table:
                warnings.append("account_without_tables")
                continue
            statement, certain = self._statement(
                view,
                collected,
                preamble.summary if preamble is not None else None,
                threshold,
                date_order,
            )
            outcome.investments.append(statement)
            skipped += collected.skipped
            unknown += collected.unknown_activity
            guessed = guessed or not certain
        if preamble is not None:
            outside = len(preamble.holdings) + len(preamble.activity) + len(preamble.cash)
            skipped += outside + preamble.skipped
            unknown += preamble.unknown_activity
            if outside:
                warnings.append("rows_outside_account")
        if dropped:
            warnings.append("too_many_accounts")
        outcome.skipped_lines = skipped
        if unknown:
            outcome.warnings.append("activity_type_unknown")
        if view.truncated:
            outcome.warnings.append("truncated")
        if guessed:
            outcome.warnings.append("date_order_guessed")
        outcome.warnings.extend(dict.fromkeys(warnings))
        return outcome


def _date_shaped(text: str) -> bool:
    if numeric_parts(text) is not None:
        return True
    return any(read_date(text, style) is not None for style in ("d_mon_y", "mon_d_y", "ymd"))


def _identifier(text: str) -> tuple[str | None, str | None]:
    """``(symbol, cusip)`` of a symbol cell (``Symbol/CUSIP`` holds either)."""
    text = text.strip()
    if not text:
        return None, None
    if is_cusip(text):
        return None, text
    return (text, None) if _SYMBOL.fullmatch(text) else (None, None)


def _split_symbol(text: str) -> tuple[str | None, str | None]:
    """``("EXMPL", "Example Index Fund")`` from ``EXMPL Example Index Fund`` or ``... (EXMPL)``."""
    paren = _PAREN_SYMBOL.search(text)
    if paren is not None:
        rest = (text[: paren.start()] + text[paren.end() :]).strip()
        return paren[1], rest or None
    head, _, rest = text.strip().partition(" ")
    if _SYMBOL.fullmatch(head) and len(head) >= 2:
        return head, rest.strip() or None
    return None, text.strip() or None


class PlaceholderBrokerageParser(BrokerageStatementParser):
    """Placeholder Brokerage (fictional): ``MM/DD/YYYY``, USD, signed amounts."""

    name: ClassVar[str] = "placeholder_brokerage"
    version: ClassVar[str] = "1"
    institution: ClassVar[str | None] = "placeholder-brokerage-account"
    display_name: ClassVar[str | None] = "Placeholder Brokerage"
    fingerprints: ClassVar[tuple[str, ...]] = (
        "placeholder brokerage",
        "investment account statement",
    )
    default_currency: ClassVar[str | None] = "USD"
    date_styles: ClassVar[tuple[DateStyle, ...]] = ("mdy",)
    period_styles: ClassVar[tuple[DateStyle, ...]] = ("mdy",)
    base: ClassVar[float] = 1.0


class FidelityStatementParser(BrokerageStatementParser):
    """Fidelity Investments "Investment Report": one or several accounts per PDF.

    The layout as this parser expects it (drawn by
    ``tests/fixtures/synth_fidelity.py`` from invented accounts and
    instruments; no real statement is in the repository): page 1 names
    "Fidelity Investments" and "Investment Report" with the period
    (``October 1, 2026 - October 31, 2026``) and an overview of the accounts;
    each account's pages carry its title and number (``INDIVIDUAL - TOD``
    ... ``Account # Z12-345678``), an account summary (``Beginning Account
    Value`` ... ``Ending Account Value``), *Holdings* grouped by asset class
    (Core Account, Mutual Funds, Exchange Traded Products, Stocks) with
    ``Total <group>`` subtotals and ``Total Holdings``, its columns
    *Description, Beginning Market Value, Quantity, Price Per Unit, Ending
    Market Value, Total Cost Basis, Unrealized Gain/Loss, EAI ($) / EY (%)*
    over two lines with dates under them; and *Activity* tables (*Settlement
    Date, Security Name, Symbol/CUSIP, Description, Quantity, Price,
    Transaction Cost, Amount*; *Date, Reference, Description, Amount* for
    deposits and withdrawals) whose description is the action ("You
    Bought", "Dividend Received", "Reinvestment", "Electronic Funds Transfer
    Received", ...; :data:`ACTIVITY_TYPES`). Dates are ``MM/DD`` (the year
    from the period), ``MM/DD/YYYY`` or ``Mon D, YYYY``.

    Cash is the **core account** (a money market position): its rows are
    the account's opening and closing cash, not a position, so every trade,
    income and transfer is checked against the change in cash; the *Core
    Fund Activity* table (the core's own buys and sells settling those
    trades) is not read, it would count each of them twice.
    """

    name: ClassVar[str] = "fidelity_investments_statement"
    version: ClassVar[str] = "1"
    institution: ClassVar[str | None] = "fidelity-investments-statement"
    display_name: ClassVar[str | None] = "Fidelity Investments"
    fingerprints: ClassVar[tuple[str, ...]] = ("fidelity investments", "investment report")
    all_fingerprints: ClassVar[bool] = True
    default_currency: ClassVar[str | None] = "USD"
    date_styles: ClassVar[tuple[DateStyle, ...]] = ("mdy", "md", "mon_d_y")
    period_styles: ClassVar[tuple[DateStyle, ...]] = ("mon_d_y", "mdy")
    base: ClassVar[float] = 1.0
    period_labels: ClassVar[tuple[str, ...]] = (
        "investment report",
        *BrokerageStatementParser.period_labels,
    )
    description_types: ClassVar[bool] = True
    cash_groups: ClassVar[tuple[str, ...]] = ("core account",)
    ignored_tables: ClassVar[tuple[str, ...]] = ("core fund activity",)
    section_headings: ClassVar[tuple[str, ...]] = (
        "holdings",
        "activity",
        "account summary",
        "securities bought",
        "dividends interest",
        "deposits",
        "withdrawals",
        "fees and charges",
        "other activity",
        "trades pending",
        "income summary",
    )


BROKERAGE_PARSERS: Final[tuple[type[BrokerageStatementParser], ...]] = (
    FidelityStatementParser,
    PlaceholderBrokerageParser,
    BrokerageStatementParser,
)

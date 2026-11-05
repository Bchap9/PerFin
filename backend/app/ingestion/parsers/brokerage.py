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

  - a **holdings** table: a header naming a quantity, a market value and a
    symbol or security name (optional price, cost basis), repeated per page;
    ``Total ...`` lines in it give the printed holdings total;
  - an **activity** table: a header naming a date, an activity/action and
    an amount (optional settle date, description, symbol, quantity, price,
    fees). The activity word types the row (:data:`ACTIVITY_TYPES`); an
    unsigned amount takes the type's direction (a buy takes cash out); a
    split's ratio comes from its description (``2-for-1``, ``3:1``);
  - outside the tables: the period, the account's last four digits, the
    currency, opening/closing cash, the holdings total and the account
    value by label, and the holdings' "as of" date.

  Each value is scored like the bank parsers
  (:mod:`app.ingestion.parsers.confidence`): the declared format of an
  institution parser 1.0, the generic parser :data:`~confidence.GENERIC`,
  OCR scaled by the cell's word confidence, a position whose market value
  disagrees :data:`~confidence.CONTRADICTED`, and every row of a statement
  that does not reconcile capped at :data:`~confidence.UNRECONCILED`
  (:data:`~confidence.UNCHECKED` when nothing can be checked).

:class:`PlaceholderBrokerageParser` pins the layout of the fictional
Placeholder Brokerage (fingerprint ``placeholder-brokerage-account``;
synthetic PDFs from ``tests/fixtures/synth_brokerage.py``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import ClassVar, Final, Literal

from pydantic import ValidationError

from app.core.money import sum_amounts
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.base import Cell, DocumentParser, DocumentView, ParseOutcome, ViewLine
from app.ingestion.parsers.table_statement import label_end
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
_RATIO: Final = re.compile(r"\b(\d{1,6})(?:\s?-?\s?for\s?-?\s?|\s?:\s?)(\d{1,6})\b", re.IGNORECASE)
_LAST4: Final = re.compile(
    r"(?<![A-Za-z0-9])[*xX\u2022]{2}[*xX\u2022\s-]{0,30}?(\d{4})\b"
    r"|\bending(?:\s+in)?\s*:?\s*(\d{4})\b"
    r"|\baccount(?:\s+(?:number|no\.?))?\s*:?\s*\d{0,26}?(\d{4})\b",
    re.IGNORECASE,
)
_CURRENCY: Final = re.compile(r"\bcurrency\s*:?\s*([A-Z]{3})\b", re.IGNORECASE)
_RANGE: Final = re.compile(r"\s(?:to|through|until|-|\u2013)\s", re.IGNORECASE)


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


#: Normalised activity word -> type (the first word(s) of the activity cell).
ACTIVITY_TYPES: Final[Mapping[str, InvestmentActivityType]] = {
    "buy": "buy",
    "bought": "buy",
    "purchase": "buy",
    "sell": "sell",
    "sold": "sell",
    "sale": "sell",
    "dividend": "dividend",
    "cash dividend": "dividend",
    "qualified dividend": "dividend",
    "capital gain": "dividend",
    "interest": "interest",
    "fee": "fee",
    "account fee": "fee",
    "commission": "fee",
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
    "contribution": "deposit",
    "withdrawal": "withdrawal",
    "distribution": "withdrawal",
    "corporate action": "corporate_action",
    "merger": "corporate_action",
    "spinoff": "corporate_action",
}
#: Types whose cash effect is an outflow when the amount is printed unsigned.
OUTFLOW_TYPES: Final = frozenset({"buy", "fee", "withdrawal", "reinvest"})
#: Types that must name a security / carry a quantity (``investment_txn`` checks).
SECURITY_TYPES: Final = frozenset(
    {"buy", "sell", "split", "transfer_in", "transfer_out", "reinvest"}
)
QUANTITY_TYPES: Final = frozenset({"buy", "sell", "transfer_in", "transfer_out", "reinvest"})


def split_ratio(text: str) -> tuple[Decimal, Decimal] | None:
    """``(new, old)`` from ``2 for 1`` / ``2-for-1`` / ``2:1`` in a split's description."""
    match = _RATIO.search(text[:MAX_TOKEN])
    if match is None:
        return None
    new, old = Decimal(match[1]), Decimal(match[2])
    if new <= 0 or old <= 0 or new == old:
        return None
    return new, old


def activity_type(text: str) -> InvestmentActivityType | None:
    """The type an activity cell names (longest known phrase at its start)."""
    norm = normalise(text)
    best: InvestmentActivityType | None = None
    best_len = 0
    for phrase, kind in ACTIVITY_TYPES.items():
        if (norm == phrase or norm.startswith(f"{phrase} ")) and len(phrase) > best_len:
            best, best_len = kind, len(phrase)
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
# The parser
# ---------------------------------------------------------------------------

HoldingRole = Literal["symbol", "name", "quantity", "price", "value", "cost"]
ActivityRole = Literal[
    "date", "settle", "type", "description", "symbol", "quantity", "price", "amount", "fees"
]

HOLDING_HEADERS: Final[Mapping[str, HoldingRole]] = {
    "symbol": "symbol",
    "ticker": "symbol",
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
    "market price": "price",
    "market value": "value",
    "current value": "value",
    "value": "value",
    "cost basis": "cost",
    "total cost": "cost",
    "cost": "cost",
    "book cost": "cost",
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
    "symbol": "symbol",
    "quantity": "quantity",
    "shares": "quantity",
    "units": "quantity",
    "price": "price",
    "unit price": "price",
    "amount": "amount",
    "net amount": "amount",
    "fees": "fees",
    "commission": "fees",
}


@dataclass(frozen=True)
class _Column[R]:
    role: R
    x0: float
    x1: float


def _columns[R](line: ViewLine, headers: Mapping[str, R]) -> list[_Column[R]]:
    found: list[_Column[R]] = []
    roles: set[R] = set()
    for cell in line.cells:
        role = headers.get(normalise(cell.text))
        if role is None or role in roles:
            continue
        roles.add(role)
        found.append(_Column(role, cell.x0, cell.x1))
    return sorted(found, key=lambda c: c.x0)


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


@dataclass
class _Summary:
    period_text: str | None = None
    as_of_text: str | None = None
    last4: str | None = None
    currency: str | None = None
    cash_opening: Amount | None = None
    cash_closing: Amount | None = None
    positions_value: Amount | None = None
    total_value: Amount | None = None


@dataclass
class _Collected:
    holdings: list[_HoldingRow] = field(default_factory=list)
    activity: list[_ActivityRow] = field(default_factory=list)
    summary: _Summary = field(default_factory=_Summary)
    skipped: int = 0
    unknown_activity: int = 0
    holdings_header: bool = False
    activity_header: bool = False


class BrokerageStatementParser(DocumentParser):
    """Generic brokerage statement: holdings and activity tables (module doc)."""

    name: ClassVar[str] = "brokerage_statement_generic"
    version: ClassVar[str] = "1"
    kind: ClassVar[Literal["statement", "payslip", "receipt"]] = "statement"
    display_name: ClassVar[str | None] = None
    #: Phrases on the first page that identify the institution (its name first).
    fingerprints: ClassVar[tuple[str, ...]] = ()
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
    #: Summary labels in the order they are tried on a line (longest first).
    summary_labels: ClassVar[tuple[tuple[str, str], ...]] = (
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
        ("total account value", "total_value"),
        ("total portfolio value", "total_value"),
        ("account value", "total_value"),
    )
    skip_prefixes: ClassVar[tuple[str, ...]] = ("page ", "continued", "subtotal")

    # -- detection ----------------------------------------------------------------

    @staticmethod
    def holdings_columns(line: ViewLine) -> list[_Column[HoldingRole]] | None:
        found = _columns(line, HOLDING_HEADERS)
        roles = {c.role for c in found}
        if {"quantity", "value"} <= roles and roles & {"symbol", "name"}:
            return found
        return None

    @staticmethod
    def activity_columns(line: ViewLine) -> list[_Column[ActivityRole]] | None:
        found = _columns(line, ACTIVITY_HEADERS)
        roles = {c.role for c in found}
        if {"date", "type", "amount"} <= roles:
            return found
        return None

    def fingerprint_score(self, view: DocumentView) -> float:
        if not self.fingerprints:
            return 0.0
        head = f" {view.head_text()} "
        hits = sum(1 for phrase in self.fingerprints if f" {normalise(phrase)} " in head)
        return hits / len(self.fingerprints)

    def detect(self, view: DocumentView) -> float:
        holdings = any(self.holdings_columns(line) is not None for line in view.lines)
        activity = any(self.activity_columns(line) is not None for line in view.lines)
        if not self.fingerprints:
            # Above the bank-statement parsers (0.4/0.45) for the same activity table.
            if holdings:
                return 0.7 if activity else 0.6
            return 0.5 if activity else 0.0
        share = self.fingerprint_score(view)
        if f" {normalise(self.fingerprints[0])} " not in f" {view.head_text()} ":
            return 0.0  # the institution's own name (the first phrase) must be there
        score = 0.5 + 0.4 * share
        return round(score + 0.1 if holdings or activity else score * 0.6, 4)

    # -- collecting -----------------------------------------------------------------

    def _scan_summary(self, line: ViewLine, summary: _Summary) -> None:
        for label, slot in self.summary_labels:
            end = label_end(line.text, label)
            if end is None:
                continue
            amounts = find_amounts(line.text[end:])
            if amounts and getattr(summary, slot) is None:
                setattr(summary, slot, amounts[0])
            break
        if summary.period_text is None:
            for label in self.period_labels:
                end = label_end(line.text, label)
                if end is not None:
                    summary.period_text = line.text[end:]
                    break
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

    def _holding(self, line: ViewLine, cells: dict[HoldingRole, list[Cell]]) -> _HoldingRow | None:
        quantity = read_number(_text(cells.get("quantity")))
        if quantity is None:
            return None
        name = _text(cells.get("name")) or None
        symbol = _text(cells.get("symbol")) or None
        if symbol is None and name is not None:
            symbol, name = _split_symbol(name)
        if symbol is not None and not _SYMBOL.fullmatch(symbol):
            symbol = None
        if symbol is None and name is None:
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
        )

    def _activity(
        self, line: ViewLine, cells: dict[ActivityRole, list[Cell]], collected: _Collected
    ) -> None:
        date_text = _text(cells.get("date"))
        if not date_text or not _date_shaped(date_text):
            return
        kind = activity_type(_text(cells.get("type")))
        amount = read_amount(_text(cells.get("amount")))
        if kind is None:
            collected.unknown_activity += 1
            collected.skipped += 1
            return
        if amount is None:
            collected.skipped += 1
            return
        description = _text(cells.get("description"))
        symbol = _text(cells.get("symbol")) or None
        quantity_text = _text(cells.get("quantity"))
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
                symbol=symbol if symbol and _SYMBOL.fullmatch(symbol) else None,
                quantity=read_number(quantity_text) if quantity_text else None,
                price=read_number(price_text) if price_text else None,
                amount=amount,
                fees=read_amount(fees_text) if fees_text else None,
                ocr=_ocr(cells),
            )
        )

    def _collect(self, view: DocumentView) -> _Collected:
        collected = _Collected()
        holdings: list[_Column[HoldingRole]] | None = None
        activity: list[_Column[ActivityRole]] | None = None
        for line in view.lines:
            header_h = self.holdings_columns(line)
            header_a = self.activity_columns(line) if header_h is None else None
            if header_h is not None:
                holdings, activity = header_h, None
                collected.holdings_header = True
                continue
            if header_a is not None:
                holdings, activity = None, header_a
                collected.activity_header = True
                continue
            norm = line.norm
            if any(norm.startswith(prefix.strip()) for prefix in self.skip_prefixes):
                continue
            if holdings is not None:
                if norm.startswith("total"):
                    amounts = find_amounts(line.text)
                    if amounts and collected.summary.positions_value is None:
                        collected.summary.positions_value = amounts[-1]
                    continue
                row = self._holding(line, _assign(line, holdings))
                if row is not None:
                    collected.holdings.append(row)
                    continue
            elif activity is not None:
                if norm.startswith("total"):
                    continue
                before = len(collected.activity) + collected.skipped
                self._activity(line, _assign(line, activity), collected)
                if len(collected.activity) + collected.skipped != before:
                    continue
            self._scan_summary(line, collected.summary)
        return collected

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
        return (chosen, "ymd", "d_mon_y", "mon_d_y"), certain

    def _period(self, summary: _Summary, styles: tuple[DateStyle, ...]) -> tuple[date, date] | None:
        if summary.period_text is None:
            return None
        ordered: list[DateStyle] = [s for s in self.period_styles if s in FULL_STYLES]
        for style in styles:
            if style in NUMERIC_DM and style in ordered:
                ordered.remove(style)
                ordered.insert(0, style)
        parts = _RANGE.split(summary.period_text, maxsplit=1)
        if len(parts) == 2:
            start = find_dates(parts[0], ordered)
            end = find_dates(parts[1], ordered)
            if start and end and start[-1] <= end[0]:
                return start[-1], end[0]
        found = find_dates(summary.period_text, ordered)
        if len(found) >= 2 and found[0] <= found[1]:
            return found[0], found[1]
        return None

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
        self, symbol: str | None, name: str | None, known: Mapping[str, str | None]
    ) -> SecurityRef | None:
        if symbol is None:
            return None
        clean_name = (name or known.get(symbol) or "").strip()[:200] or None
        try:
            return SecurityRef(
                symbol=symbol, name=clean_name, asset_class=guess_asset_class(clean_name)
            )
        except ValidationError:
            return None

    def _positions(
        self, collected: _Collected, as_of: date | None
    ) -> tuple[list[InvestmentPosition], list[dict[str, float]]]:
        positions: list[InvestmentPosition] = []
        scores: list[dict[str, float]] = []
        for row in collected.holdings:
            security = self._security(row.symbol, row.name, {})
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
            if symbol is None and name is not None:
                lead, rest = _split_symbol(name)
                if lead is not None and (lead in known or row.type in SECURITY_TYPES):
                    symbol, name = lead, rest
            security = self._security(symbol, name, known)
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

    def parse(
        self, view: DocumentView, *, threshold: float, date_order: str | None = None
    ) -> ParseOutcome:
        collected = self._collect(view)
        outcome = ParseOutcome()
        if not (collected.holdings_header or collected.activity_header):
            outcome.warnings.append("no_table_header")
            return outcome
        summary = collected.summary
        styles, certain = self._styles(collected.activity, date_order)
        period = self._period(summary, styles)
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
        statement_conf["account_number"] = self.base if summary.last4 else conf.MISSING
        statement_conf["currency"] = self.base if currency else conf.MISSING
        for slot in ("cash_opening", "cash_closing", "positions_value", "total_value"):
            statement_conf[slot] = self.base if getattr(summary, slot) is not None else conf.MISSING

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
            cash_opening=balance(summary.cash_opening, period[0] if period else None),
            cash_closing=balance(summary.cash_closing, as_of),
            positions_value=balance(summary.positions_value, as_of),
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
        outcome.investments.append(statement)
        outcome.skipped_lines = collected.skipped
        if collected.unknown_activity:
            outcome.warnings.append("activity_type_unknown")
        if view.truncated:
            outcome.warnings.append("truncated")
        if not certain:
            outcome.warnings.append("date_order_guessed")
        return outcome


def _date_shaped(text: str) -> bool:
    if numeric_parts(text) is not None:
        return True
    return any(read_date(text, style) is not None for style in ("d_mon_y", "mon_d_y", "ymd"))


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


BROKERAGE_PARSERS: Final[tuple[type[BrokerageStatementParser], ...]] = (
    PlaceholderBrokerageParser,
    BrokerageStatementParser,
)

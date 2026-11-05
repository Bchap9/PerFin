# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brokerage activity tables (CSV, TSV, XLSX) -> investment statements.

A brokerage's "Activity" export (Fidelity's ``Run Date, Account, Account
Number, Action, Symbol, Description, Type, ..., Price, Quantity, ...,
Commission, Fees, Accrued Interest, Amount, Settlement Date`` and layouts like
it) lists buys, sells, reinvestments, income and cash movements, often of
several accounts in one file. Read through a cash-ledger column mapping it
would become a list of cash amounts on one account; read here it becomes one
:class:`~app.ingestion.sandbox.protocol.InvestmentStatement` per account, which
the importer commits through ``app.domain.investments.statement_import`` like
an OFX ``INVSTMTRS`` (INGESTION.md "Brokerage activity exports").

It runs in the **worker** (the importer, ``app.domain.imports.service``), on
the stored, schema-checked table the sandbox extracted (``document.extraction``),
never on file bytes (ADR-0006). The statements it builds are not stored: a
re-run reads the stored table again, so a fixed or newer version of this
parser applies to documents already uploaded.

**Recognising a table** (:func:`find_header`): a header row among the first
:data:`~app.ingestion.parsers.structured.HEADER_SEARCH_ROWS` rows naming

* a date (``Run Date``, ``Trade Date``, ``Date``, ...; a settlement date is
  read separately as the settle date, and alone is not enough),
* an action (``Action``, ``Activity``, ``Transaction Type``; Fidelity's
  ``Type`` column, ``Cash``/``Margin``, is recognised and not read),
* an amount (``Amount``, ``Amount ($)``, ``Net Amount``),
* and a symbol or a quantity column, so a bank export with a ``Type`` of
  ``Deposit``/``Withdrawal`` never reads as brokerage activity.

Optional columns: settlement date, symbol (a CUSIP with a valid check digit
is kept as the CUSIP), description (the security's name), quantity, price,
commission, fees, currency and an account number (``Account Number``; else an
``Account`` column whose cells carry four or more digits). Other columns of
the export (exchange quantity/currency/rate, accrued interest, the ``Type``)
are recognised and ignored. The table is recognised only when at least half
of its data rows have an action :func:`~app.ingestion.parsers.brokerage.
activity_type` knows, so the header alone never routes a file.

**Rows**: a row with at most one filled cell (Fidelity's quoted disclaimer
paragraphs, the ``Date downloaded ...`` line, blank lines) is a *note*: not
read, counted (``notes``). Every other row must give

* a date (one format for the whole column, settlement dates included;
  :func:`~app.ingestion.parsers.structured.best_date_format`),
* an action :func:`activity_type` types (``YOU BOUGHT``, ``REINVESTMENT ...
  (Cash)``, ``DIVIDEND RECEIVED``, ``Electronic Funds Transfer Paid``, ...;
  ``TRANSFERRED FROM/TO`` is a transfer of units when the row names a security
  and a non-zero quantity, else cash). An action it does not know is
  **skipped and counted** (``unknown_activity``), never guessed;
* an amount: the signed cash effect, kept as printed (negative = cash out).
  A blank amount is only accepted on a transfer of units (no cash moves);
* a security (``Symbol``, with ``Description`` as its name) for a buy, sell,
  reinvestment or transfer of units, and a non-zero quantity for those; a
  split needs its ratio in the description (``2-for-1``).

  A row missing any of these is skipped and counted (``skipped``). Quantities
  are printed signed (a sale is negative); the statement carries them positive
  like the PDF parser does, the type giving the direction. ``fees`` is
  commission + fees. A settle date before the trade date is dropped. The
  action text is the row's memo (a cash deposit's or withdrawal's payee on
  the ledger: ``DIRECT DEPOSIT ...``, ``Electronic Funds Transfer Paid``).
  The extractors' formula guard (a leading ``'`` on a cell that would be a
  formula, SECURITY.md C7) is set aside only to recognise headers and
  actions and to parse dates and numbers; the description (the security's
  name) and the memo are kept guarded, and a symbol that would be a formula
  is no symbol.

**Accounts**: rows are grouped by the last four digits of the account-number
cell (:func:`~app.ingestion.parsers.structured.cell_last4`), one statement per
account in order of first appearance; without an account column the table is
one statement. Only the last four digits are kept; the cell is never stored or
logged. A group's currency is its rows' currency column when they agree; a row
in another currency than the rest of its group is skipped (``currency_mixed``),
an empty cell is the account's currency.

**Scoring and holding (the choice made here).** An activity export prints no
positions, no cash balances and no totals, so there is nothing to reconcile:
:func:`~app.ingestion.parsers.brokerage.reconcile_investment` says
``unavailable``. The statements are a *structured source* like OFX
(``source_format = "csv"``): every value comes from a named column of a
delimited file, with no OCR or layout guess to score, so they are imported the
way ``statement_import`` imports an OFX statement with nothing to check, and
carry no per-row confidence. The one guess this parser can make is the
day/month order of an all-numeric date column whose days are all 12 or less;
then every row carries ``confidence`` :data:`~app.ingestion.parsers.confidence.
GUESSED` and ``low_confidence = ["trade_date"]``, and ``statement_import``
holds the statement (``investment_low_confidence``) instead of writing dates
that may be wrong. ``csv`` is a worker-only ``source_format``
(``WORKER_STATEMENT_FORMATS`` in the sandbox protocol): a sandbox child that
answers with one is refused (``bad_output``), so a crafted file cannot make
the child skip the hold rules of document statements by claiming to be a
table read here.

The out-of-order and lot rules of ``statement_import`` apply as for any
statement: import exports oldest first; a sale of units bought before the
export's first row (with no earlier statement giving the account's
positions) is refused by the lot engine (``investment_lot_conflict``) and
nothing of that account's statement is written.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final, Literal

from pydantic import ValidationError

from app.core.csv_safety import is_dangerous, neutralise_formula
from app.domain.imports.schemas import DateFormat
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.brokerage import (
    QUANTITY_TYPES,
    SECURITY_TYPES,
    activity_type,
    guess_asset_class,
    is_cusip,
    reconcile_investment,
    split_ratio,
)
from app.ingestion.parsers.structured import (
    HEADER_SEARCH_ROWS,
    best_date_format,
    cell_last4,
    guess_decimal_separator,
    parse_cell_amount,
    parse_cell_date,
)
from app.ingestion.parsers.text import normalise
from app.ingestion.sandbox.protocol import (
    MAX_STATEMENTS,
    TEXT_MAX,
    InvestmentActivity,
    InvestmentActivityType,
    InvestmentStatement,
    ParserRef,
    SecurityRef,
    StatementAccount,
    Table,
)

PARSER_NAME: Final = "brokerage_activity_table"
PARSER_VERSION: Final = "1"
#: Extraction kinds whose tables are read here (never a PDF's page table).
TABLE_KINDS: Final = frozenset({"csv", "tsv", "xlsx"})
#: Share of the data rows whose action must be typed for the table to be recognised.
TYPED_SHARE_MIN: Final = 0.5
#: The account type the statements carry (the importer accepts brokerage or retirement).
ACCOUNT_TYPE: Final = "brokerage"

Role = Literal[
    "date",
    "settle",
    "action",
    "symbol",
    "description",
    "quantity",
    "price",
    "commission",
    "fees",
    "amount",
    "currency",
    "account",
    "account_name",
    "other",
]

#: Normalised header cell (:func:`~app.ingestion.parsers.text.normalise`:
#: ``Amount ($)`` -> ``amount``) -> its role. ``other`` columns are known and
#: not read; ``account_name`` is an ``Account`` column, read for the account
#: only when no ``Account Number`` column exists and its cells carry digits.
HEADERS: Final[Mapping[str, Role]] = {
    "run date": "date",
    "trade date": "date",
    "transaction date": "date",
    "activity date": "date",
    "date": "date",
    "settlement date": "settle",
    "settle date": "settle",
    "action": "action",
    "activity": "action",
    "activity type": "action",
    "transaction type": "action",
    "transaction": "action",
    "symbol": "symbol",
    "ticker": "symbol",
    "symbol cusip": "symbol",
    "cusip": "symbol",
    "description": "description",
    "security description": "description",
    "security name": "description",
    "security": "description",
    "quantity": "quantity",
    "shares": "quantity",
    "units": "quantity",
    "qty": "quantity",
    "price": "price",
    "unit price": "price",
    "commission": "commission",
    "commissions": "commission",
    "fees": "fees",
    "fee": "fees",
    "amount": "amount",
    "net amount": "amount",
    "currency": "currency",
    "account number": "account",
    "account no": "account",
    "account": "account_name",
    "account name": "other",
    "type": "other",
    "exchange quantity": "other",
    "exchange currency": "other",
    "exchange rate": "other",
    "accrued interest": "other",
}
#: Roles a header must name (plus one of :data:`SECURITY_ROLES`).
REQUIRED_ROLES: Final = ("date", "action", "amount")
SECURITY_ROLES: Final = ("symbol", "quantity")
_SYMBOL: Final = re.compile(r"[A-Za-z0-9.^/:_-]{1,32}")
_CURRENCY: Final = re.compile(r"[A-Z]{3}")


@dataclass(frozen=True)
class Header:
    """The header row of a recognised table and the column of each role."""

    row: int
    columns: Mapping[Role, int]


@dataclass
class ActivityTable:
    """What :func:`read_activity_table` read (counts only, no cell text)."""

    header: Header
    statements: list[InvestmentStatement] = field(default_factory=list)
    #: Rows read into a statement.
    rows: int = 0
    #: Rows with at most one filled cell (disclaimers, ``Date downloaded``).
    notes: int = 0
    #: Rows whose action no known phrase types (skipped, never guessed).
    unknown_activity: int = 0
    #: Rows that could not be read (date, amount, security, quantity, ratio, currency).
    skipped: int = 0
    #: Accounts beyond :data:`~app.ingestion.sandbox.protocol.MAX_STATEMENTS` (dropped).
    dropped_accounts: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def parser(self) -> ParserRef:
        return ParserRef(name=PARSER_NAME, version=PARSER_VERSION, kind="statement")

    def summary(self) -> dict[str, object]:
        """The run summary's ``brokerage_table`` entry (counts and codes only)."""
        return {
            "parser": f"{PARSER_NAME}/{PARSER_VERSION}",
            "statements": len(self.statements),
            "rows": self.rows,
            "notes": self.notes,
            "unknown_activity": self.unknown_activity,
            "skipped": self.skipped,
            "warnings": list(self.warnings),
        }


def _filled(row: Sequence[str]) -> int:
    return sum(1 for cell in row if cell.strip())


def _roles(row: Sequence[str]) -> dict[Role, int]:
    """The role of each header cell (the first column naming a role takes it)."""
    columns: dict[Role, int] = {}
    for index, cell in enumerate(row):
        role = HEADERS.get(normalise(cell.removeprefix("'")))
        if role is not None and role != "other" and role not in columns:
            columns[role] = index
    return columns


def _cell(row: Sequence[str], column: int | None) -> str:
    """The cell for matching and parsing: the extractor's formula guard removed."""
    if column is None or column >= len(row):
        return ""
    return " ".join(row[column].removeprefix("'").split())


def _text(row: Sequence[str], column: int | None) -> str:
    """The cell as text to keep (a name, a memo): still formula-guarded (SECURITY.md C7)."""
    if column is None or column >= len(row):
        return ""
    return neutralise_formula(" ".join(row[column].split()))


def _account_column(table: Table, header: int, columns: Mapping[Role, int]) -> int | None:
    if "account" in columns:
        return columns["account"]
    name = columns.get("account_name")
    if name is None:
        return None
    body = table.rows[header + 1 :]
    return name if any(cell_last4(_cell(r, name)) for r in body) else None


def find_header(table: Table) -> Header | None:
    """The header of a brokerage activity table, or None (module doc, "Recognising")."""
    for index, row in enumerate(table.rows[:HEADER_SEARCH_ROWS]):
        columns = _roles(row)
        if all(r in columns for r in REQUIRED_ROLES) and any(r in columns for r in SECURITY_ROLES):
            account = _account_column(table, index, columns)
            columns.pop("account_name", None)
            if account is not None:
                columns["account"] = account
            return Header(index, columns)
    return None


def _security(symbol: str, name: str) -> SecurityRef | None:
    # A trailing marker (``SPAXX**``: a core money market) is not part of the symbol.
    text = symbol.strip().rstrip("*")
    text = text.upper() if is_cusip(text.upper()) else text
    if not text or _SYMBOL.fullmatch(text) is None or is_dangerous(text):
        return None
    clean_name = name.strip()[:200] or None
    try:
        if is_cusip(text):
            return SecurityRef(
                symbol=None, cusip=text, name=clean_name, asset_class=guess_asset_class(clean_name)
            )
        return SecurityRef(symbol=text, name=clean_name, asset_class=guess_asset_class(clean_name))
    except ValidationError:  # pragma: no cover - the pattern matches the model's
        return None


@dataclass
class _Row:
    account: str | None
    currency: str | None
    activity: InvestmentActivity


@dataclass
class _Reader:
    table: Table
    header: Header
    date_format: DateFormat
    separator: str
    guessed: bool
    out: ActivityTable

    def col(self, role: Role) -> int | None:
        return self.header.columns.get(role)

    def number(self, row: Sequence[str], role: Role) -> Decimal | None:
        text = _cell(row, self.col(role))
        return parse_cell_amount(text, self.separator) if text else None

    def read(self, row: Sequence[str]) -> _Row | None:
        """One data row, or None (counted as unknown or skipped)."""
        trade = parse_cell_date(_cell(row, self.col("date")), self.date_format)
        if trade is None:
            self.out.skipped += 1
            return None
        symbol = _cell(row, self.col("symbol"))
        name = _cell(row, self.col("description"))
        security = _security(symbol, _text(row, self.col("description")))
        signed = self.number(row, "quantity")
        moves = security is not None and signed is not None and signed != 0
        kind: InvestmentActivityType | None = activity_type(
            _cell(row, self.col("action")), moves_security=moves
        )
        if kind is None:
            self.out.unknown_activity += 1
            return None
        amount_text = _cell(row, self.col("amount"))
        amount = parse_cell_amount(amount_text, self.separator) if amount_text else None
        if amount is None and not amount_text and kind in ("transfer_in", "transfer_out"):
            amount = Decimal(0)  # a transfer of units moves no cash
        quantity = abs(signed) if signed is not None and signed != 0 else None
        ratio = split_ratio(name or _cell(row, self.col("action"))) if kind == "split" else None
        if (
            amount is None
            or (kind in SECURITY_TYPES and security is None)
            or (kind in QUANTITY_TYPES and quantity is None)
            or (kind == "split" and ratio is None)
        ):
            self.out.skipped += 1
            return None
        currency = _cell(row, self.col("currency")).upper() or None
        if currency is not None and _CURRENCY.fullmatch(currency) is None:
            self.out.skipped += 1
            return None
        settle_text = _cell(row, self.col("settle"))
        settle = parse_cell_date(settle_text, self.date_format) if settle_text else None
        price = self.number(row, "price")
        fees = abs(self.number(row, "commission") or Decimal(0)) + abs(
            self.number(row, "fees") or Decimal(0)
        )
        try:
            activity = InvestmentActivity(
                type=kind,
                trade_date=trade,
                settle_date=settle if settle is not None and settle >= trade else None,
                security=security,
                quantity=None if kind == "split" else quantity,
                unit_price=abs(price) if price is not None and price != 0 else None,
                split_ratio=ratio,
                amount=amount,
                fees=fees,
                memo=_text(row, self.col("action"))[:TEXT_MAX] or None,
                confidence=conf.GUESSED if self.guessed else None,
                low_confidence=["trade_date"] if self.guessed else [],
            )
        except ValidationError:
            self.out.skipped += 1
            return None
        account = cell_last4(_cell(row, self.col("account"))) if self.col("account") else None
        return _Row(account, currency, activity)


def _date_format(header: Header, body: Sequence[Sequence[str]]) -> tuple[DateFormat, bool]:
    """One format for the date and settlement columns; whether the day/month order is a guess."""
    roles: tuple[Role, ...] = ("date", "settle")
    values = [_cell(r, header.columns.get(role)) for r in body for role in roles]
    found, _, tied = best_date_format(v for v in values if v)
    return (found or "MM/DD/YYYY"), tied


def _typed_share(header: Header, body: Sequence[Sequence[str]]) -> float:
    column = header.columns["action"]
    if not body:
        return 0.0
    typed = sum(1 for r in body if activity_type(_cell(r, column)) is not None)
    return typed / len(body)


def read_activity_table(table: Table) -> ActivityTable | None:
    """The investment statements of a brokerage activity table (None: not one; module doc)."""
    header = find_header(table)
    if header is None:
        return None
    rows = table.rows[header.row + 1 :]
    body = [r for r in rows if _filled(r) >= 2]
    if not body or _typed_share(header, body) < TYPED_SHARE_MIN:
        return None
    date_format, guessed = _date_format(header, body)
    amounts = [_cell(r, header.columns["amount"]) for r in body]
    separator = guess_decimal_separator(a for a in amounts if a)
    out = ActivityTable(header=header)
    reader = _Reader(table, header, date_format, separator, guessed, out)
    groups: dict[str | None, list[_Row]] = {}
    for row in rows:
        if _filled(row) < 2:
            out.notes += 1
            continue
        read = reader.read(row)
        if read is None:
            continue
        groups.setdefault(read.account, []).append(read)
    mixed = False
    for key, group in groups.items():
        if len(out.statements) >= MAX_STATEMENTS:
            out.dropped_accounts += 1
            out.skipped += len(group)
            continue
        currencies = [r.currency for r in group if r.currency is not None]
        currency = max(set(currencies), key=currencies.count) if currencies else None
        kept = [r for r in group if r.currency is None or r.currency == currency]
        if len(kept) != len(group):
            mixed = True
            out.skipped += len(group) - len(kept)
        activity = sorted((r.activity for r in kept), key=lambda a: a.trade_date)
        out.rows += len(activity)
        statement = InvestmentStatement(
            source_format="csv",
            account=StatementAccount(account_type=ACCOUNT_TYPE, last4=key, currency=currency),
            period_start=activity[0].trade_date if activity else None,
            period_end=activity[-1].trade_date if activity else None,
            activity=activity,
            parser=out.parser,
        )
        out.statements.append(
            statement.model_copy(update={"reconciliation": reconcile_investment(statement)})
        )
    if out.unknown_activity:
        out.warnings.append("activity_type_unknown")
    if out.skipped:
        out.warnings.append("rows_unreadable")
    if mixed:
        out.warnings.append("currency_mixed")
    if guessed:
        out.warnings.append("date_order_guessed")
    if out.dropped_accounts:
        out.warnings.append("too_many_accounts")
    if table.truncated:
        out.warnings.append("truncated")
    return out


def activity_dates(statements: Sequence[InvestmentStatement]) -> tuple[date, date] | None:
    """The first and last trade dates of ``statements`` (None: no rows)."""
    days = [a.trade_date for s in statements for a in s.activity]
    return (min(days), max(days)) if days else None

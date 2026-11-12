# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brokerage positions exports (CSV, TSV, XLSX) -> investment statements.

A brokerage's "Positions" export (Fidelity's ``Account Number, Account Name,
Symbol, Description, Quantity, Last Price, Last Price Change, Current Value,
Today's Gain/Loss Dollar, ..., Percent Of Account, Cost Basis Total, Average
Cost Basis, Type`` and layouts like it) lists what every account holds at the
moment of the download: one row per holding, with its units, price, value and
cost basis. It is not activity: no dates, no actions. Read here it becomes
one :class:`~app.ingestion.sandbox.protocol.InvestmentStatement` per account
whose ``positions`` are the account's holdings on the day of the download,
which the importer commits through ``app.domain.investments.statement_import``
as a **positions snapshot** (INGESTION.md "Brokerage positions exports").

Like the activity table parser it runs in the **worker**, on the stored,
schema-checked table the sandbox extracted (``document.extraction``), never on
file bytes (ADR-0006); the statements are read again on every run and never
stored.

**Recognising a table** (:func:`find_header`): a header row among the first
:data:`~app.ingestion.parsers.structured.HEADER_SEARCH_ROWS` rows naming a
symbol, a quantity and a market value column (``Current Value``, ``Market
Value``, ``Value``) and **no** date or action column (that is an activity
export). Optional columns: account number, account name, description (the
security's name), price, cost basis (the position's total), percent of
account and currency. The other columns of the export (price change, the
day's and total gain, the average cost, ``Type``) restate these and are not
read. The table is recognised only when at least half of its data rows read
(:data:`READ_SHARE_MIN`), so the header alone never routes a file.

**Rows**: a row with at most one filled cell is a *note* (the quoted
disclaimer paragraphs, blank lines); the ``Date downloaded ...`` note gives
the day the positions are valued (:func:`downloaded_on`). Every other row is

* **cash**: the account's core position (a symbol ending in ``**``:
  ``SPAXX**``, ``FCASH**``), ``Pending Activity``, or a row with no quantity
  whose description reads like cash (``HELD IN MONEY MARKET``). Its value is
  part of the account's closing cash, never a holding, as the PDF statement
  parser treats a core account;
* a **position**: a security (the symbol, a CUSIP with a valid check digit
  kept as the CUSIP, else the description alone) with a non-zero quantity.
  Price, value and cost basis are read when printed (``n/a`` and ``--`` are
  blanks). A price quoted per 100 of face value (a bond: quantity x price is
  a hundred times the value) is marked ``price_per_100``; a price quoted per
  share of a contract of 100 (an option: the value is a hundred times
  quantity x price) is stored per contract, so quantity x price is the
  value either way. A negative quantity is a short position (the importer
  keeps it out of the lot engine);
* anything else is **skipped and counted** (``skipped``), never guessed.

The extractors' formula guard (a leading ``'``, SECURITY.md C7) is set aside
only to recognise headers and to parse numbers; names are kept guarded and a
symbol that would be a formula is no symbol.

**Accounts**: rows are grouped by their account-number cell together with
the account's name (so two accounts never fold into one, even when an export
masks every number alike), one statement per account in order of first
appearance; without an account column the table is one statement. Only the
last four digits of the number are kept. The name becomes the statement
account's name and makes it a ``retirement`` account when it reads like one.
A known broker layout (:data:`LAYOUT_INSTITUTIONS`) names the institution.

**What the statement says about itself** (the checks of
:func:`~app.ingestion.parsers.brokerage.reconcile_investment`): each
position's quantity x price against its printed value (``positions``), and,
when the export prints each row's percent of its account, whether the rows
are the whole account (``total_value``): the percentages of an account's
rows add up to 100. Then the statement's ``total_value`` is the sum of its
rows; when they do not (rows were removed from the file), it is the total
the percentages imply, so the check fails and says by how much. A positions
export has no activity, so a failed ``positions`` check is the fault of the
positions that fail it and of nothing else: they alone carry the
``reconciliation`` doubt (``doubtful_positions``), and the importer holds
those securities while the rest of the account is imported. An account with
a row that could not be read prints no cash and no total: the importer then
records no balance for it.

The statements are a structured source like OFX (``source_format = "csv"``,
a worker-only format): every value comes from a named column. Their period
is the one day of the download.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Final, Literal

from pydantic import ValidationError

from app.core.csv_safety import is_dangerous, neutralise_formula
from app.core.money import calc_context
from app.ingestion.parsers.brokerage import (
    _position_agrees,
    guess_asset_class,
    is_cusip,
    reconcile_investment,
)
from app.ingestion.parsers.brokerage_table import account_type_for
from app.ingestion.parsers.structured import HEADER_SEARCH_ROWS, cell_last4
from app.ingestion.parsers.text import normalise
from app.ingestion.sandbox.protocol import (
    MAX_POSITIONS,
    MAX_STATEMENTS,
    Balance,
    InvestmentPosition,
    InvestmentStatement,
    ParserRef,
    SecurityRef,
    StatementAccount,
    Table,
)

PARSER_NAME: Final = "brokerage_positions_table"
PARSER_VERSION: Final = "1"
#: Extraction kinds whose tables are read here (never a PDF's page table).
TABLE_KINDS: Final = frozenset({"csv", "tsv", "xlsx"})
#: Share of the data rows that must read for the table to be recognised.
READ_SHARE_MIN: Final = 0.5
#: Longest account name kept.
NAME_MAX: Final = 80
#: The doubt a position whose quantity x price is not its value carries.
RECONCILIATION_DOUBT: Final = "reconciliation"
#: Rounding of one printed percentage (two decimals), in percent points.
PERCENT_ROUNDING: Final = Decimal("0.005")
#: Slack on a sum of percentages beyond its rows' rounding.
PERCENT_SLACK: Final = Decimal("0.02")
CENT: Final = Decimal("0.01")

Role = Literal[
    "account",
    "account_name",
    "symbol",
    "description",
    "quantity",
    "price",
    "value",
    "cost",
    "percent",
    "currency",
    "other",
]

#: Normalised header cell (``Today's Gain/Loss Dollar`` -> ``today s gain loss
#: dollar``) -> its role. ``other`` columns are known and not read.
HEADERS: Final[Mapping[str, Role]] = {
    "account number": "account",
    "account no": "account",
    "account name number": "account",
    "account name": "account_name",
    "account": "account_name",
    "symbol": "symbol",
    "ticker": "symbol",
    "symbol cusip": "symbol",
    "description": "description",
    "security description": "description",
    "security name": "description",
    "investment name": "description",
    "name": "description",
    "quantity": "quantity",
    "shares": "quantity",
    "units": "quantity",
    "qty": "quantity",
    "last price": "price",
    "price": "price",
    "share price": "price",
    "current price": "price",
    "unit price": "price",
    "current value": "value",
    "market value": "value",
    "total value": "value",
    "value": "value",
    "cost basis total": "cost",
    "total cost basis": "cost",
    "cost basis": "cost",
    "total cost": "cost",
    "percent of account": "percent",
    "of account": "percent",
    "currency": "currency",
    "last price change": "other",
    "today s gain loss dollar": "other",
    "today s gain loss percent": "other",
    "total gain loss dollar": "other",
    "total gain loss percent": "other",
    "average cost basis": "other",
    "cost basis per share": "other",
    "type": "other",
}
#: Header cells of an activity export: a table naming one of each is not positions.
_ACTIVITY_DATES: Final = frozenset(
    {"run date", "trade date", "transaction date", "activity date", "date", "settlement date"}
)
_ACTIVITY_ACTIONS: Final = frozenset(
    {"action", "activity", "activity type", "transaction type", "transaction"}
)
REQUIRED_ROLES: Final = ("symbol", "quantity", "value")
#: Header rows (their first normalised cells) that name the broker who wrote them.
LAYOUT_INSTITUTIONS: Final[Mapping[tuple[str, ...], str]] = {
    ("account number", "account name", "symbol", "description", "quantity"): (
        "Fidelity Investments"
    ),
    ("account name number", "symbol", "description", "quantity", "last price"): (
        "Fidelity Investments"
    ),
}
#: Symbol cells (normalised) that are the account's cash, not a security.
CASH_SYMBOLS: Final = frozenset({"pending activity", "cash", "core"})
_SYMBOL: Final = re.compile(r"[A-Za-z0-9.^/:_-]{1,32}")
_CURRENCY: Final = re.compile(r"[A-Z]{3}")
_BLANKS: Final = frozenset({"", "n/a", "na", "--", "-"})
_DOWNLOADED: Final = re.compile(r"date\s+downloaded\s*:?\s*(?P<day>\S+)", re.IGNORECASE)
_MONTHS: Final = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_DAY_FORMATS: Final = (
    # Nov-11-2026 (the export's own form)
    re.compile(r"(?P<mon>[A-Za-z]{3})[A-Za-z]*[-/ ](?P<d>\d{1,2})[-/, ]+(?P<y>\d{4})"),
    # 11/11/2026
    re.compile(r"(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})"),
    # 2026-11-11
    re.compile(r"(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})"),
)


@dataclass(frozen=True)
class Header:
    """The header row of a recognised table and the column of each role."""

    row: int
    columns: Mapping[Role, int]


@dataclass
class PositionsTable:
    """What :func:`read_positions_table` read (counts only, no cell text)."""

    header: Header
    statements: list[InvestmentStatement] = field(default_factory=list)
    #: The day the positions are valued.
    as_of: date | None = None
    #: ``as_of`` is the day the file was received (it prints no download date).
    as_of_assumed: bool = False
    #: Rows read as holdings and as cash.
    positions: int = 0
    cash_rows: int = 0
    #: Rows with at most one filled cell (disclaimers, ``Date downloaded``).
    notes: int = 0
    #: Rows that could not be read (no security, no quantity, no value).
    skipped: int = 0
    #: Accounts beyond :data:`~app.ingestion.sandbox.protocol.MAX_STATEMENTS` (dropped).
    dropped_accounts: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def parser(self) -> ParserRef:
        return ParserRef(name=PARSER_NAME, version=PARSER_VERSION, kind="statement")

    def summary(self) -> dict[str, object]:
        """The run summary's ``positions_table`` entry (counts and codes only)."""
        return {
            "parser": f"{PARSER_NAME}/{PARSER_VERSION}",
            "statements": len(self.statements),
            "as_of": self.as_of.isoformat() if self.as_of is not None else None,
            "as_of_assumed": self.as_of_assumed,
            "positions": self.positions,
            "cash_rows": self.cash_rows,
            "notes": self.notes,
            "skipped": self.skipped,
            "accounts": [
                # Counts and types only: an account's name may be a person's.
                {"account_type": s.account.account_type, "positions": len(s.positions)}
                for s in self.statements
            ],
            "warnings": list(self.warnings),
        }


def is_positions_snapshot(statement: InvestmentStatement) -> bool:
    """``statement`` was read from a positions export: its positions are the whole account.

    The importer then brings the account's holdings to them (units no
    longer listed leave), instead of only opening what its history does not
    explain. ``csv`` is a worker-only source format, so a sandbox child
    cannot claim it.
    """
    parser = statement.parser
    return (
        statement.source_format == "csv"
        and parser is not None
        and parser.name == PARSER_NAME
        and statement.as_of is not None
    )


def _filled(row: Sequence[str]) -> int:
    return sum(1 for cell in row if cell.strip())


def _roles(row: Sequence[str]) -> dict[Role, int] | None:
    """The role of each header cell (None: the row names activity columns)."""
    columns: dict[Role, int] = {}
    names = [normalise(cell.removeprefix("'")) for cell in row]
    if _ACTIVITY_DATES & set(names) and _ACTIVITY_ACTIONS & set(names):
        return None
    for index, name in enumerate(names):
        role = HEADERS.get(name)
        if role is not None and role != "other" and role not in columns:
            columns[role] = index
    return columns


def find_header(table: Table) -> Header | None:
    """The header of a positions table, or None (module doc, "Recognising")."""
    for index, row in enumerate(table.rows[:HEADER_SEARCH_ROWS]):
        columns = _roles(row)
        if columns is not None and all(role in columns for role in REQUIRED_ROLES):
            return Header(index, columns)
    return None


def layout_institution(table: Table, row: int) -> str | None:
    """The broker whose export layout the header row ``row`` is."""
    if row >= len(table.rows):
        return None
    cells = tuple(normalise(c.removeprefix("'")) for c in table.rows[row])
    for prefix, institution in LAYOUT_INSTITUTIONS.items():
        if cells[: len(prefix)] == prefix:
            return institution
    return None


def downloaded_on(rows: Sequence[Sequence[str]]) -> date | None:
    """The day of the export's ``Date downloaded ...`` note (None: it prints none)."""
    for row in rows:
        if _filled(row) != 1:
            continue
        text = next(cell for cell in row if cell.strip()).removeprefix("'")
        found = _DOWNLOADED.search(text)
        if found is None:
            continue
        rest = text[found.start("day") :]
        for pattern in _DAY_FORMATS:
            match = pattern.match(rest)
            if match is None:
                continue
            parts = match.groupdict()
            month = _MONTHS.get(parts["mon"].lower()) if "mon" in parts else int(parts["m"])
            if month is None:
                continue
            try:
                return date(int(parts["y"]), month, int(parts["d"]))
            except ValueError:
                continue
    return None


def _cell(row: Sequence[str], column: int | None) -> str:
    """The cell for matching and parsing: the extractor's formula guard removed."""
    if column is None or column >= len(row):
        return ""
    text = " ".join(row[column].removeprefix("'").split())
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].strip()
    return text


def _text(row: Sequence[str], column: int | None) -> str:
    """The cell as text to keep (a name): still formula-guarded (SECURITY.md C7)."""
    if column is None or column >= len(row):
        return ""
    return neutralise_formula(" ".join(row[column].split()))


def read_number(text: str) -> Decimal | None:
    """A quantity, price, amount or percentage as printed; None for a blank or ``n/a``.

    ``$1,000.71``, ``+$53.11``, ``-$250.00``, ``$-250.00``, ``(12.50)``,
    ``2.27%``. Any number of decimals (a quantity may have more than money).
    """
    value = text.strip().lower()
    if value in _BLANKS:
        return None
    negative = False
    if value.startswith("(") and value.endswith(")"):
        negative, value = True, value[1:-1]
    value = value.replace("$", "").replace(",", "").replace(" ", "").removesuffix("%")
    if value.startswith(("+", "-")):
        negative = negative != (value[0] == "-")
        value = value[1:]
    if not re.fullmatch(r"\d{1,18}(?:\.\d{1,12})?|\.\d{1,12}", value):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:  # pragma: no cover - the pattern admits only numbers
        return None
    return -number if negative else number


def _security(symbol: str, name: str) -> SecurityRef | None:
    """How the row names its security: a symbol or CUSIP, else its name alone."""
    clean_name = name.strip()[:200] or None
    text = symbol.strip().upper() if is_cusip(symbol.strip().upper()) else symbol.strip()
    asset_class = guess_asset_class(clean_name)
    try:
        if text and _SYMBOL.fullmatch(text) is not None and not is_dangerous(text):
            if is_cusip(text):
                return SecurityRef(
                    symbol=None, cusip=text, name=clean_name, asset_class=asset_class
                )
            return SecurityRef(symbol=text, name=clean_name, asset_class=asset_class)
        if clean_name is not None:
            return SecurityRef(name=clean_name, asset_class=asset_class)
    except ValidationError:
        return None
    return None


def _is_cash(symbol: str, description: str, quantity: Decimal | None) -> bool:
    """The row is the account's cash (module doc, "Rows")."""
    if symbol.endswith("**") or normalise(symbol) in CASH_SYMBOLS:
        return True
    return quantity is None and guess_asset_class(description) == "cash_equivalent"


@dataclass
class _Row:
    #: Groups the rows of one account (never stored).
    key: str
    last4: str | None
    name: str | None
    currency: str | None
    #: Percent of the account, when the row prints one.
    percent: Decimal | None
    value: Decimal | None
    position: InvestmentPosition | None = None
    cash: Decimal | None = None


@dataclass
class _Group:
    rows: list[_Row] = field(default_factory=list)
    #: A row of the account could not be read: its figures are not the whole account.
    incomplete: bool = False


def _position(
    security: SecurityRef,
    quantity: Decimal,
    price: Decimal | None,
    value: Decimal | None,
    cost: Decimal | None,
    on: date | None,
) -> InvestmentPosition | None:
    per_100 = False
    with localcontext(calc_context()):
        if price is not None and price <= 0:
            price = None
        if price is not None and value is not None:
            if _position_agrees(quantity, price, value, 1):
                pass
            elif _position_agrees(quantity, price, value, 100):
                per_100 = True  # a bond: quoted per 100 of face value
            elif _position_agrees(quantity, price * 100, value, 1):
                price = price * 100  # an option: one contract is 100 shares
    try:
        return InvestmentPosition(
            security=security,
            quantity=quantity,
            unit_price=price,
            price_per_100=per_100,
            market_value=value,
            cost_basis=abs(cost) if cost is not None else None,
            price_as_of=on,
        )
    except ValidationError:
        return None


def _account_key(number: str, name: str | None) -> str:
    return f"{normalise(number)}\x1f{normalise(name or '')}"


@dataclass
class _Reader:
    header: Header
    on: date | None

    def col(self, role: Role) -> int | None:
        return self.header.columns.get(role)

    def number(self, row: Sequence[str], role: Role) -> Decimal | None:
        return read_number(_cell(row, self.col(role)))

    def read(self, row: Sequence[str]) -> _Row | None:
        """One data row: a position, cash, or None (it could not be read)."""
        number = _cell(row, self.col("account"))
        name = _text(row, self.col("account_name"))[:NAME_MAX] or None
        currency = _cell(row, self.col("currency")).upper() or None
        if currency is not None and _CURRENCY.fullmatch(currency) is None:
            return None
        base = _Row(
            key=_account_key(number, name),
            last4=cell_last4(number) if number else None,
            name=name,
            currency=currency,
            percent=self.number(row, "percent"),
            value=self.number(row, "value"),
        )
        symbol = _cell(row, self.col("symbol"))
        quantity = self.number(row, "quantity")
        if _is_cash(symbol, _cell(row, self.col("description")), quantity):
            if base.value is None:
                return None
            base.cash = base.value
            return base
        security = _security(symbol, _text(row, self.col("description")))
        if security is None or quantity is None or quantity == 0:
            return None
        price = self.number(row, "price")
        if base.value is None and price is None:
            return None  # nothing says what it is worth
        base.position = _position(
            security, quantity, price, base.value, self.number(row, "cost"), self.on
        )
        return base if base.position is not None else None


def _percent_total(rows: Sequence[_Row], total: Decimal) -> Decimal | None:
    """The account value the rows' percentages vouch for (module doc).

    ``total`` (the sum of the rows) when every valued row prints its share
    and the shares add up to 100; the total the shares imply when they do
    not; None when the export prints no shares (nothing to check).
    """
    valued = [r for r in rows if r.value is not None and r.value != 0]
    if not valued or any(r.percent is None for r in valued):
        return None
    with localcontext(calc_context()):
        share = sum((r.percent for r in valued if r.percent is not None), Decimal(0))
        allowed = PERCENT_ROUNDING * len(valued) + PERCENT_SLACK
        if abs(share - 100) <= allowed:
            return total
        if share <= 0:
            return None
        return (total * 100 / share).quantize(CENT)


def _statement(
    group: _Group,
    *,
    on: date | None,
    institution: str | None,
    parser: ParserRef,
    truncated: bool,
) -> InvestmentStatement:
    rows = group.rows
    currencies = [r.currency for r in rows if r.currency is not None]
    currency = max(set(currencies), key=currencies.count) if currencies else None
    positions = [r.position for r in rows if r.position is not None][:MAX_POSITIONS]
    cash_rows = [r.cash for r in rows if r.cash is not None]
    complete = not group.incomplete and not truncated
    cash: Decimal | None = None
    total: Decimal | None = None
    if complete:
        with localcontext(calc_context()):
            cash = sum(cash_rows, Decimal(0)) if cash_rows else None
            values = [r.value for r in rows]
            if all(v is not None for v in values):
                rows_total = sum((v for v in values if v is not None), Decimal(0))
                total = _percent_total(rows, rows_total)
    name = next((r.name for r in rows if r.name), None)
    statement = InvestmentStatement(
        source_format="csv",
        account=StatementAccount(
            account_type=account_type_for(name),
            last4=rows[0].last4,
            currency=currency,
            name=name,
            institution=institution,
        ),
        period_start=on,
        period_end=on,
        as_of=on,
        positions=positions,
        cash_closing=Balance(amount=cash, as_of=on) if cash is not None else None,
        total_value=Balance(amount=total, as_of=on) if total is not None else None,
        truncated=truncated,
        parser=parser,
    )
    check = reconcile_investment(statement)
    # No activity: a position whose value is not quantity x price is the
    # whole of what the ``positions`` check doubts (module doc).
    doubted = set(check.position_mismatches)
    return statement.model_copy(
        update={
            "positions": [
                p.model_copy(update={"low_confidence": [RECONCILIATION_DOUBT]})
                if index in doubted
                else p
                for index, p in enumerate(statement.positions)
            ],
            "reconciliation": check.model_copy(
                update={"doubtful_activity": [], "doubtful_positions": sorted(doubted)}
            ),
        }
    )


def read_positions_table(
    table: Table, *, institution: str | None = None, received_on: date | None = None
) -> PositionsTable | None:
    """The investment statements of a positions export (None: not one; module doc).

    ``institution`` names the statements' institution; without it a known
    broker layout (:data:`LAYOUT_INSTITUTIONS`) does. ``received_on`` is
    the day the file was received, used as the day the positions are
    valued only when the export prints no ``Date downloaded`` note
    (``as_of_assumed``); with neither, the table is not read.
    """
    header = find_header(table)
    if header is None or header.row >= len(table.rows):
        return None
    rows = table.rows[header.row + 1 :]
    body = [r for r in rows if _filled(r) >= 2]
    if not body:
        return None
    printed = downloaded_on(table.rows)
    on = printed or received_on
    if on is None:
        return None
    reader = _Reader(header, on)
    out = PositionsTable(header=header, as_of=on, as_of_assumed=printed is None)
    groups: dict[str, _Group] = {}
    for row in rows:
        if _filled(row) < 2:
            out.notes += 1
            continue
        read = reader.read(row)
        if read is None:
            out.skipped += 1
            # The account it belongs to is no longer whole.
            key = _account_key(
                _cell(row, reader.col("account")),
                _text(row, reader.col("account_name"))[:NAME_MAX] or None,
            )
            groups.setdefault(key, _Group()).incomplete = True
            continue
        groups.setdefault(read.key, _Group()).rows.append(read)
    read_rows = sum(len(g.rows) for g in groups.values())
    if read_rows < READ_SHARE_MIN * len(body):
        return None
    institution = institution or layout_institution(table, header.row)
    mixed = False
    for group in groups.values():
        if not group.rows:
            continue
        if len(out.statements) >= MAX_STATEMENTS:
            out.dropped_accounts += 1
            out.skipped += len(group.rows)
            continue
        currencies = {r.currency for r in group.rows if r.currency is not None}
        if len(currencies) > 1:
            # One account, one currency: the rows in another are left out.
            mixed = True
            common = max(currencies, key=[r.currency for r in group.rows].count)
            kept = [r for r in group.rows if r.currency in (None, common)]
            out.skipped += len(group.rows) - len(kept)
            group.rows, group.incomplete = kept, True
        statement = _statement(
            group, on=on, institution=institution, parser=out.parser, truncated=table.truncated
        )
        out.positions += len(statement.positions)
        out.cash_rows += sum(1 for r in group.rows if r.cash is not None)
        out.statements.append(statement)
    if out.skipped:
        out.warnings.append("rows_unreadable")
    if mixed:
        out.warnings.append("currency_mixed")
    if out.as_of_assumed:
        out.warnings.append("as_of_assumed")
    if out.dropped_accounts:
        out.warnings.append("too_many_accounts")
    if table.truncated:
        out.warnings.append("truncated")
    return out

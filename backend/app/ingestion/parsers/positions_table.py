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
disclaimer paragraphs, blank lines, a title); the ``Date downloaded ...``
note gives the day the positions are valued (:func:`downloaded_on`), else a
title above the header that says ``as of`` a date does (:func:`as_of_title`).
A row that repeats the header row is a note too. Every other row is

* a **total**: a row with no quantity whose symbol cell is one of
  :data:`TOTAL_SYMBOLS` (``Account Total``). It is not data: it is neither a
  holding nor skipped, and it does not count towards
  :data:`READ_SHARE_MIN`. Its value is the account's printed total (below);
* **cash**: the account's core position (a symbol ending in ``**``:
  ``SPAXX**``, ``FCASH**``), ``Pending Activity``, a symbol cell that starts
  with the word cash (``Cash & Cash Investments``), or a row with no
  quantity whose description reads like cash (``HELD IN MONEY MARKET``). Its
  value is part of the account's closing cash, never a holding, as the PDF
  statement parser treats a core account;
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
appearance. Without an account-number column the table is read in *blocks*:
the header row starts the first, every repetition of it the next, and rows
of different blocks never share a statement. The nearest one-cell line above
a block's header names its account (:func:`block_account`: ``Roth IRA
...8820``), when it prints a number or a mask; a table with one header and
no such line is one statement, as before. Only the last four digits of the
number are kept. The name becomes the statement account's name and makes it
a ``retirement`` account when it reads like one. A known broker layout
(:data:`LAYOUT_INSTITUTIONS`) names the institution.

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
those securities while the rest of the account is imported. An account
whose rows end in a total row is checked against that instead: the printed
total is the statement's ``total_value``, so the check compares the
holdings plus the cash rows with it. Total rows of one account that
disagree (a subtotal and a total) are no total, and a ``Grand Total`` is an
account's only when the file has one account. An account with a row that
could not be read prints no cash and no total: the importer then records no
balance for it.

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
#: Symbol cells (normalised) of a row that prints an account's total, not a holding.
TOTAL_SYMBOLS: Final = frozenset(
    {"account total", "total", "totals", "positions total", "grand total"}
)
#: The total row that sums every account of the file.
_GRAND_TOTAL: Final = "grand total"
_SYMBOL: Final = re.compile(r"[A-Za-z0-9.^/:_-]{1,32}")
_CURRENCY: Final = re.compile(r"[A-Z]{3}")
_BLANKS: Final = frozenset({"", "n/a", "na", "--", "-"})
_DOWNLOADED: Final = re.compile(r"date\s+downloaded\s*:?\s*(?P<day>\S+)", re.IGNORECASE)
_AS_OF: Final = re.compile(r"\bas\s+of\b", re.IGNORECASE)
#: The words a title puts before the account it names.
_TITLE_LEAD: Final = re.compile(r"^\s*positions\s+for(?:\s+account)?\b[\s:]*", re.IGNORECASE)
_CLOCK: Final = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")
#: A token of a title that is (part of) an account number: digits, or a mask.
_NUMBER_TOKEN: Final = re.compile(r".*\d.*|[*#xX.\u2022-]*[*#xX.\u2022][*#xX.\u2022-]*")
#: Names (normalised) that say no account.
_NO_NAMES: Final = frozenset({"all accounts", "account", "accounts", "account number", "acct"})
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
    #: Rows with at most one filled cell (disclaimers, ``Date downloaded``)
    #: and repetitions of the header row.
    notes: int = 0
    #: Rows that print a total (:data:`TOTAL_SYMBOLS`): not data, not skipped.
    total_rows: int = 0
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
            "total_rows": self.total_rows,
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


def _day(match: re.Match[str]) -> date | None:
    """The day one of the :data:`_DAY_FORMATS` matched (None: no such day)."""
    parts = match.groupdict()
    month = _MONTHS.get(parts["mon"].lower()) if "mon" in parts else int(parts["m"])
    if month is None:
        return None
    try:
        return date(int(parts["y"]), month, int(parts["d"]))
    except ValueError:
        return None


def _only_cell(row: Sequence[str]) -> str | None:
    """The text of a row with exactly one filled cell (its formula guard removed)."""
    if _filled(row) != 1:
        return None
    return next(cell for cell in row if cell.strip()).strip().removeprefix("'")


def downloaded_on(rows: Sequence[Sequence[str]]) -> date | None:
    """The day of the export's ``Date downloaded ...`` note (None: it prints none)."""
    for row in rows:
        text = _only_cell(row)
        if text is None:
            continue
        found = _DOWNLOADED.search(text)
        if found is None:
            continue
        rest = text[found.start("day") :]
        for pattern in _DAY_FORMATS:
            match = pattern.match(rest)
            day = _day(match) if match is not None else None
            if day is not None:
                return day
    return None


def as_of_title(rows: Sequence[Sequence[str]]) -> date | None:
    """The day a title says the positions are ``as of`` (None: no title says).

    ``rows`` are the rows above the header. The first date after the words
    ``as of`` on a one-cell row (``Positions for account ... as of 09:30 AM
    ET, 11/06/2026``: the date, whatever time stands before it).
    """
    for row in rows:
        text = _only_cell(row)
        found = _AS_OF.search(text) if text is not None else None
        if text is None or found is None:
            continue
        rest = text[found.end() :]
        matches = sorted(
            (m for m in (pattern.search(rest) for pattern in _DAY_FORMATS) if m is not None),
            key=lambda m: m.start(),
        )
        for match in matches:
            day = _day(match)
            if day is not None:
                return day
    return None


def block_account(text: str) -> tuple[str | None, str | None]:
    """``(last four digits, name)`` of the account a one-cell line names.

    ``Roth IRA ...8820`` -> ``("8820", "Roth IRA")``. Read from what is left
    of the line without a leading ``Positions for`` / ``Positions for
    account``, without everything from `` as of `` on and without any date
    or time of day, so the moment of the download is never the account's
    digits. The name is what stands before the first token that holds a
    digit or is a mask (``...``, ``XXXX``); the digits are the last four
    from that token on. A line with no such token names no account (it is a
    note), and neither does ``All-Accounts``.
    """
    rest = " ".join(text.removeprefix("'").split())
    cut = _AS_OF.search(rest)
    if cut is not None:
        rest = rest[: cut.start()]
    rest = _TITLE_LEAD.sub("", rest)
    for pattern in (*_DAY_FORMATS, _CLOCK):
        rest = pattern.sub(" ", rest)
    tokens = rest.split()
    first = next((i for i, t in enumerate(tokens) if _NUMBER_TOKEN.fullmatch(t)), None)
    if first is None:
        return None, None
    name = " ".join(tokens[:first]).strip(" :,-")
    if not normalise(name) or normalise(name) in _NO_NAMES:
        return cell_last4(" ".join(tokens[first:])), None
    return cell_last4(" ".join(tokens[first:])), neutralise_formula(name)[:NAME_MAX]


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
    label = normalise(symbol)
    if symbol.endswith("**") or label in CASH_SYMBOLS or label.startswith("cash "):
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
    #: The values of the account's total rows.
    totals: list[Decimal] = field(default_factory=list)

    @property
    def printed_total(self) -> Decimal | None:
        """The account total the export prints (None: none, or total rows that disagree)."""
        return self.totals[0] if len(set(self.totals)) == 1 else None


@dataclass(frozen=True)
class _Block:
    """The rows under one header row of a table with no account-number column."""

    index: int
    last4: str | None = None
    name: str | None = None


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

    def account(
        self, row: Sequence[str], block: _Block | None
    ) -> tuple[str, str | None, str | None]:
        """``(key, last four digits, name)`` of the account ``row`` belongs to.

        ``block`` (a table with no account-number column) keeps the rows
        under each header row apart and names their account.
        """
        number = _cell(row, self.col("account"))
        name = _text(row, self.col("account_name"))[:NAME_MAX] or None
        if block is None:
            return _account_key(number, name), cell_last4(number) if number else None, name
        name = name or block.name
        return f"{block.index}\x1f{_account_key('', name)}", block.last4, name

    def is_total(self, row: Sequence[str]) -> str | None:
        """The normalised label of a total row (None: ``row`` is not one)."""
        label = normalise(_cell(row, self.col("symbol")))
        if label in TOTAL_SYMBOLS and self.number(row, "quantity") is None:
            return label
        return None

    def read(self, row: Sequence[str], block: _Block | None = None) -> _Row | None:
        """One data row: a position, cash, or None (it could not be read)."""
        key, last4, name = self.account(row, block)
        currency = _cell(row, self.col("currency")).upper() or None
        if currency is not None and _CURRENCY.fullmatch(currency) is None:
            return None
        base = _Row(
            key=key,
            last4=last4,
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
            if group.printed_total is not None:
                # The account's own total row: holdings plus cash must be it.
                total = group.printed_total
            elif all(v is not None for v in values):
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


def _heading(row: Sequence[str]) -> list[str]:
    """A row's cells as header names (trailing blanks dropped), to spot the header repeated."""
    names = [normalise(cell.removeprefix("'")) for cell in row]
    while names and not names[-1]:
        names.pop()
    return names


def _block(rows: Sequence[Sequence[str]], header_row: int, index: int) -> _Block:
    """The block under the header at ``header_row``, named by the one-cell line above it.

    The nearest row above that is not blank; a row of several cells there
    (the last row of the block before) names nothing.
    """
    for row in reversed(rows[:header_row]):
        if _filled(row) == 0:
            continue
        text = _only_cell(row)
        if text is None:
            break
        last4, name = block_account(text)
        return _Block(index, last4, name)
    return _Block(index)


def read_positions_table(
    table: Table, *, institution: str | None = None, received_on: date | None = None
) -> PositionsTable | None:
    """The investment statements of a positions export (None: not one; module doc).

    ``institution`` names the statements' institution; without it a known
    broker layout (:data:`LAYOUT_INSTITUTIONS`) does. ``received_on`` is
    the day the file was received, used as the day the positions are
    valued only when the export prints neither a ``Date downloaded`` note
    nor a title ``as of`` a date (``as_of_assumed``); with none of the
    three, the table is not read.
    """
    header = find_header(table)
    if header is None or header.row >= len(table.rows):
        return None
    above = table.rows[: header.row]
    printed = downloaded_on(table.rows) or as_of_title(above)
    on = printed or received_on
    if on is None:
        return None
    reader = _Reader(header, on)
    out = PositionsTable(header=header, as_of=on, as_of_assumed=printed is None)
    heading = _heading(table.rows[header.row])
    # Blocks only where no column says whose a row is (module doc, "Accounts").
    block = _block(table.rows, header.row, 0) if reader.col("account") is None else None
    groups: dict[str, _Group] = {}
    grand: list[Decimal] = []
    body = 0
    for index, row in enumerate(table.rows[header.row + 1 :], header.row + 1):
        if _filled(row) < 2:
            out.notes += 1
            continue
        if _heading(row) == heading:
            out.notes += 1
            if block is not None:
                block = _block(table.rows, index, block.index + 1)
            continue
        total = reader.is_total(row)
        if total is not None:
            out.total_rows += 1
            value = reader.number(row, "value")
            if value is None:
                continue
            if total == _GRAND_TOTAL:
                grand.append(value)
            else:
                groups.setdefault(reader.account(row, block)[0], _Group()).totals.append(value)
            continue
        body += 1
        read = reader.read(row, block)
        if read is None:
            out.skipped += 1
            # The account it belongs to is no longer whole.
            groups.setdefault(reader.account(row, block)[0], _Group()).incomplete = True
            continue
        groups.setdefault(read.key, _Group()).rows.append(read)
    read_rows = sum(len(g.rows) for g in groups.values())
    if not body or read_rows < READ_SHARE_MIN * body:
        return None
    held = [g for g in groups.values() if g.rows]
    if len(held) == 1:
        # The file's one account: its grand total is the account's.
        held[0].totals.extend(grand)
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

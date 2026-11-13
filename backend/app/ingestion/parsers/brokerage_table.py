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
commission, fees (also one ``Fees & Comm`` column), currency and an account
number (``Account Number``; else an ``Account`` column whose cells carry four
or more digits). Other columns of
the export (exchange quantity/currency/rate, accrued interest, the ``Type``)
are recognised and ignored. The table is recognised only when at least half
of its data rows have an action :func:`~app.ingestion.parsers.brokerage.
activity_type` knows, so the header alone never routes a file.

**Rows**: a row with at most one filled cell (Fidelity's quoted disclaimer
paragraphs, the ``Date downloaded ...`` line, blank lines) is a *note*: not
read, counted (``notes``). So is the export's total line (``Transactions
Total`` and figures only; :func:`_is_total_row`): it is no activity, is not
counted as unread and takes no part in the typed share or the date format.
Every other row must give

* a date (one format for the whole column, settlement dates included;
  :func:`~app.ingestion.parsers.structured.best_date_format`). A cell
  printed ``10/14/2026 as of 10/10/2026`` is read as its first date, the
  trade date (:func:`_trade_date_text`); the second is dropped and decides
  nothing, the day/month order included. When no day is above 12 the order
  is a guess (``date_order_guessed``), unless the header's filled cells are
  exactly a known US broker header (:func:`whole_layout`; a layout known by
  its first cells only, or a known header with one more column, is not) and
  the amounts are written ``$`` first: then the dates are month first
  (:func:`_date_format`),
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

  A broker that prints a reinvested dividend as two rows gives the income
  first (``Reinvest Dividend``: no quantity, cash in) and the purchase second
  (``Reinvest Shares``: units, cash out). The row typed ``reinvest`` with an
  empty quantity cell and an amount above zero is that income leg and is
  read as a ``dividend`` of its security. With no quantity and no cash
  coming in it proves neither leg and is skipped like any reinvestment
  without units; so is one whose quantity cell is filled but gives no units
  (``n/a``, ``0``), which may be the purchase on an export that prints
  amounts unsigned. The other way round, a row typed ``dividend`` whose
  action says reinvest in its first words (``Div Reinvest``, ``Qual Div
  Reinvest``) and that carries units is the purchase of a broker printing
  the reinvestment as one row: it is read as a ``reinvest``.

  **Options.** A symbol cell that names an option (``ZZOP 03/20/2026 45.00
  C``, or the OCC form; :func:`~app.ingestion.parsers.brokerage.
  option_symbol`) is the security ``ZZOP260320C00045000`` with the asset
  class ``option``; its name is the description, else the cell as printed.
  ``Buy to Open``, ``Sell to Close``, ``Sell to Open`` and ``Buy to Close``
  are typed by their first word. One contract is 100 shares: when the
  amount before fees is nearer to quantity x price x 100 than to quantity x
  price the price is stored per contract (price x 100), else as printed
  (:func:`_contract_price`). An ``Expired`` or ``Assigned`` row of an option
  says that a position closed, not which way: once an account's rows are
  read (:func:`_close_options`) it becomes a ``sell`` when the account's
  other rows leave the option held and a ``buy`` when they leave it written,
  whatever sign its quantity is printed with, and a blank amount is zero.
  A closing with nothing open, or larger than what is open, is skipped and
  counted: the position was opened before the export's first row and
  nothing here says which way. So is a ``Buy to Close``/``Buy to Cover``
  of an option the other rows do not leave written: it closes a short this
  export never opened and is never read as a purchase.

  Only rows of the export prove what is open. A sale that closes by its
  own words (``Sell to Close``, ``You Sold Closing Transaction``) takes
  what the rows before it bought and never leaves the option written: the
  rest was bought before the export's first row. A plain ``Sell`` with
  nothing held may write the option or sell contracts bought earlier, so
  the short it leaves is not proven; only a sale that opens by its own
  words (``Sell to Open``, ``You Sold Opening Transaction``, ``Sell
  Short``) proves one. An expiry is a ``buy`` only against a proven short;
  against an unproven one it is skipped and counted.

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
logged. A masked number with fewer than four digits (``Z2xxxxxxxx``) groups
by its cell text, else by the ``Account`` nickname (:func:`_account_key`), so
two accounts never fold into one; such a statement has no last four digits.
The nickname names the statement's account (``StatementAccount.name``, in
memory only: never in the summary, which may be stored) and makes it a
``retirement`` account when it reads like one (:func:`account_type_for`). A
known broker layout (:data:`LAYOUT_INSTITUTIONS`) names the institution.

**Column mappings**: an activity-mode ``MappingSpec`` (``mode =
"activity"``) gives the columns instead of :func:`find_header`
(:func:`header_from_spec`); the table is then read whatever share of its
actions is known. :func:`suggest_activity_mapping` is the mapping editor's
suggestion for a recognised header.

A group's currency is its rows' currency column when they agree; a row in
another currency than the rest of its group is skipped (``currency_mixed``),
an empty cell is the account's currency. A filled cell that is not an ISO
code PerFin knows (``currency.known_currency``) makes its row unreadable.

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
statement: import exports oldest first. A sale of units bought before the
export's first row (with no earlier statement giving the account's
positions) opens the shortfall at an unknown cost basis
(``investment_opening_basis_unknown``) rather than being refused.
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
from app.domain.accounts.subtypes import guess_subtype
from app.domain.imports.schemas import DateFormat, MappingSpec
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.brokerage import (
    QUANTITY_TYPES,
    SECURITY_TYPES,
    activity_type,
    expand_action,
    guess_asset_class,
    is_cusip,
    option_symbol,
    reconcile_investment,
    split_ratio,
)
from app.ingestion.parsers.currency import known_currency
from app.ingestion.parsers.structured import (
    HEADER_SEARCH_ROWS,
    MappingSuggestion,
    RoleReason,
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
#: Unknown actions kept as examples (their first words) for the review queue.
UNKNOWN_EXAMPLES: Final = 8
UNKNOWN_WORDS: Final = 3
#: Longest account nickname kept.
NAME_MAX: Final = 80

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
    # One column for both (``Fees & Comm``): read as fees, summed with a
    # commission column like any other.
    "fees comm": "fees",
    "fees and comm": "fees",
    "fees commissions": "fees",
    "fees and commissions": "fees",
    "commission fees": "fees",
    "commissions fees": "fees",
    "commissions and fees": "fees",
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
#: Header rows (their first normalised cells) that name the broker who wrote them.
#: A header whose filled cells are exactly one of these is a whole known
#: header (:func:`whole_layout`).
LAYOUT_INSTITUTIONS: Final[Mapping[tuple[str, ...], str]] = {
    ("run date", "account", "account number", "action", "symbol"): "Fidelity Investments",
    ("run date", "action", "symbol", "description", "type"): "Fidelity Investments",
    # A whole header, in the two column orders the export has been written in.
    (
        "date",
        "action",
        "symbol",
        "description",
        "quantity",
        "price",
        "fees comm",
        "amount",
    ): "Charles Schwab",
    (
        "date",
        "action",
        "symbol",
        "description",
        "price",
        "quantity",
        "fees comm",
        "amount",
    ): "Charles Schwab",
}
#: The month-first formats a known US broker layout settles when the dates
#: alone read day first just as well (:func:`_date_format`).
US_TIE_FORMATS: Final[frozenset[str]] = frozenset({"MM/DD/YYYY", "MM/DD/YY"})
#: Filled amount cells a table needs before the way it writes money says anything.
DOLLAR_FIRST_MIN_CELLS: Final = 4
#: Share of the filled amount cells that must start with the dollar sign.
DOLLAR_FIRST_SHARE: Final = 0.9
_DOLLAR_FIRST: Final = re.compile(r"[-+(]?\$")
#: Roles a header must name (plus one of :data:`SECURITY_ROLES`).
REQUIRED_ROLES: Final = ("date", "action", "amount")
SECURITY_ROLES: Final = ("symbol", "quantity")
_SYMBOL: Final = re.compile(r"[A-Za-z0-9.^/:_-]{1,32}")


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
    #: Rows with at most one filled cell (disclaimers, ``Date downloaded``) and total lines.
    notes: int = 0
    #: Rows whose action no known phrase types (skipped, never guessed).
    unknown_activity: int = 0
    #: Rows that could not be read (date, amount, security, quantity, ratio, currency).
    skipped: int = 0
    #: Accounts beyond :data:`~app.ingestion.sandbox.protocol.MAX_STATEMENTS` (dropped).
    dropped_accounts: int = 0
    warnings: list[str] = field(default_factory=list)
    #: The leading words of actions no phrase types (normalised, a few, for the review queue).
    unknown_actions: dict[str, int] = field(default_factory=dict)

    def note_unknown(self, action: str) -> None:
        # Digits (a check number, a card's last digits) are not kept.
        words = " ".join(
            "#" if any(c.isdigit() for c in w) else w
            for w in normalise(action).split()[:UNKNOWN_WORDS]
        )
        if not words or (
            words not in self.unknown_actions and len(self.unknown_actions) >= UNKNOWN_EXAMPLES
        ):
            return
        self.unknown_actions[words] = self.unknown_actions.get(words, 0) + 1

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
            "unknown_actions": [
                {"action": action, "rows": rows} for action, rows in self.unknown_actions.items()
            ],
            "accounts": [
                # Counts and types only: an account's nickname may be a
                # person's name and is never written to a summary.
                {"account_type": s.account.account_type, "rows": len(s.activity)}
                for s in self.statements
            ],
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
    """The cell for matching and parsing: the extractor's formula guard removed.

    Quotes left around a value (an export that writes ``, "VUG"`` with a
    space before the quote, which CSV readers do not treat as quoting) are
    removed too.
    """
    if column is None or column >= len(row):
        return ""
    text = " ".join(row[column].removeprefix("'").split())
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].strip()
    return text


def _text(row: Sequence[str], column: int | None) -> str:
    """The cell as text to keep (a name, a memo): still formula-guarded (SECURITY.md C7)."""
    if column is None or column >= len(row):
        return ""
    return neutralise_formula(" ".join(row[column].split()))


#: ``<trade date> as of <effective date>``: what follows the first date.
_AS_OF: Final = re.compile(r"\s+as\s+of\s+.*", re.IGNORECASE | re.DOTALL)


def _trade_date_text(cell: str) -> str:
    """A date cell cut at its first `` as of ``: the first date is the trade date.

    A broker prints ``10/14/2026 as of 10/10/2026`` for an entry booked on
    one day and effective on another. The day it was booked is the row's
    date; the second date is dropped.
    """
    return _AS_OF.sub("", cell, count=1).strip()


def _is_total_label(cell: str) -> bool:
    text = normalise(cell)
    return text == "total" or text.startswith("total ") or text.endswith(" total")


def _is_total_row(row: Sequence[str], header: Header) -> bool:
    """Whether ``row`` is the export's total line (``Transactions Total``, then figures).

    Its first filled cell, normalised, is ``total``, starts with ``total ``
    or ends with `` total``, and every other filled cell is an amount (in
    either decimal convention: this runs before the separator is known). A
    row with any other text (an action, a symbol, a date) is a row; so is
    one with something in the symbol column beside the label, since a
    total line names no security and an all-digit identifier would
    otherwise pass for a figure.
    """
    filled = [(index, _cell(row, index)) for index in range(len(row))]
    filled = [(index, text) for index, text in filled if text]
    if len(filled) < 2 or not _is_total_label(filled[0][1]):
        return False
    symbol = header.columns.get("symbol")
    for index, text in filled[1:]:
        if index == symbol:
            return False
        if parse_cell_amount(text, ".") is None and parse_cell_amount(text, ",") is None:
            return False
    return True


def _is_note(row: Sequence[str], header: Header) -> bool:
    """A row that is not activity: at most one filled cell, or the total line."""
    return _filled(row) < 2 or _is_total_row(row, header)


def _body(table: Table, header: Header) -> list[list[str]]:
    """The rows under the header that are read (no notes)."""
    return [r for r in table.rows[header.row + 1 :] if not _is_note(r, header)]


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
            if account is not None:
                columns["account"] = account
            if columns.get("account_name") == account:
                columns.pop("account_name", None)
            return Header(index, columns)
    return None


#: The mapping spec's activity columns -> the role they name.
SPEC_ROLES: Final[Mapping[str, Role]] = {
    "date_column": "date",
    "settle_column": "settle",
    "action_column": "action",
    "symbol_column": "symbol",
    "security_column": "description",
    "quantity_column": "quantity",
    "price_column": "price",
    "commission_column": "commission",
    "fees_column": "fees",
    "amount_column": "amount",
    "currency_column": "currency",
    "account_column": "account",
    "account_name_column": "account_name",
}


def layout_institution(table: Table, row: int) -> str | None:
    """The broker whose export layout the header row ``row`` is (``Run Date, Account, ...``)."""
    if row >= len(table.rows):
        return None
    cells = tuple(normalise(c.removeprefix("'")) for c in table.rows[row])
    for prefix, institution in LAYOUT_INSTITUTIONS.items():
        if cells[: len(prefix)] == prefix:
            return institution
    return None


def whole_layout(table: Table, row: int) -> bool:
    """Whether the filled cells of the header row ``row`` are exactly a known broker header.

    Stricter than :func:`layout_institution`, which names the broker from the
    first cells: a header with one more column is another export.
    """
    if row >= len(table.rows):
        return False
    cells = tuple(normalise(c.removeprefix("'")) for c in table.rows[row])
    return tuple(c for c in cells if c) in LAYOUT_INSTITUTIONS


def header_from_spec(spec: MappingSpec) -> Header:
    """The header an activity-mode mapping names (its ``skip_rows`` row, its columns)."""
    columns: dict[Role, int] = {}
    for name, role in SPEC_ROLES.items():
        column = getattr(spec, name)
        if column is not None:
            columns[role] = column
    return Header(spec.skip_rows, columns)


def account_type_for(name: str | None) -> str:
    """``retirement`` for an account nicknamed like one (``ROTH IRA``, ``401(k)``, ``HSA``).

    Anything else is ``brokerage`` (``app.domain.accounts.subtypes.guess_subtype``).
    """
    kind, _ = guess_subtype(name)
    return kind.value if kind is not None else ACCOUNT_TYPE


def _security(symbol: str, name: str) -> SecurityRef | None:
    # A trailing marker (``SPAXX**``: a core money market) is not part of the symbol.
    text = symbol.strip().rstrip("*")
    option = option_symbol(text)
    if option is not None:
        # ``text`` is letters, digits, ``/``, ``.`` and spaces: nothing a formula starts with.
        try:
            return SecurityRef(
                symbol=option, name=name.strip()[:200] or text[:200], asset_class="option"
            )
        except ValidationError:  # pragma: no cover - the OCC form fits the model's pattern
            return None
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
    #: Groups the rows of one account (never stored; see :func:`_account_key`).
    key: str | None
    last4: str | None
    name: str | None
    currency: str | None
    activity: InvestmentActivity
    #: An option row that closes a position: ``expiry`` (``Expired``,
    #: ``Assigned``: its direction is settled by :func:`_close_options`),
    #: ``cover`` (``Buy to Close``: kept only when a short is open) or ``sale``
    #: (``Sell to Close``: never leaves the option written).
    closes: Literal["expiry", "cover", "sale"] | None = None
    #: An option sale that writes the option by its own words (``Sell to Open``).
    opens: bool = False


#: Actions that close an option whichever way it was open (the row's own sign says nothing).
EXPIRY_WORDS: Final = ("expired", "assigned")
#: Actions that buy back a written option, by their own words.
COVER_WORDS: Final = ("buy to close", "buy to cover")
#: Actions that sell a held option, by their own words.
CLOSING_SALE_WORDS: Final = (
    "sell to close",
    "sold to close",
    "you sold closing transaction",
    "sold closing transaction",
)
#: Actions that write an option, by their own words.
OPENING_SALE_WORDS: Final = (
    "sell to open",
    "sold to open",
    "you sold opening transaction",
    "sold opening transaction",
    "sell short",
    "sold short",
)
#: How far into a dividend's action the word ``reinvest`` makes it a reinvestment.
REINVEST_WITHIN: Final = 5
#: Shares one option contract stands for.
CONTRACT_SIZE: Final = 100


def _starts(norm: str, phrases: Sequence[str]) -> bool:
    return any(norm == phrase or norm.startswith(f"{phrase} ") for phrase in phrases)


def _is_option(security: SecurityRef | None) -> bool:
    return security is not None and security.asset_class == "option"


def _contract_price(
    quantity: Decimal, price: Decimal, amount: Decimal, fees: Decimal, *, sale: bool
) -> Decimal:
    """An option's price per contract: ``price`` x 100 when the amount says it was per share.

    The amount before fees (a sale's proceeds plus its fees, a purchase's
    cost less its fees) is compared with quantity x price and with quantity
    x price x 100; the nearer one tells how the price was printed. A tie
    keeps the printed price.
    """
    value = quantity * price
    gross = amount + fees if sale else abs(amount) - fees
    if abs(gross - value * CONTRACT_SIZE) < abs(gross - value):
        return price * CONTRACT_SIZE
    return price


def _says_reinvest(action: str) -> bool:
    """Whether one of the action's first words is ``reinvest``/``reinvestment``/``reinvested``."""
    words = expand_action(normalise(action)).split()[:REINVEST_WITHIN]
    return any(word.startswith("reinvest") for word in words)


#: Units a row adds to (or takes from) what the account holds of its security.
_UNITS_IN: Final = frozenset({"buy", "reinvest", "transfer_in"})
_UNITS_OUT: Final = frozenset({"sell", "transfer_out"})
#: On one day: openings and plain trades first, then a closing trade, then an expiry.
_CLOSE_ORDER: Final[Mapping[str | None, int]] = {None: 0, "cover": 1, "sale": 1, "expiry": 2}


def _close_options(rows: list[_Row], out: ActivityTable) -> list[_Row]:
    """``rows`` (one account's) with every option closing given its direction, or dropped.

    Each option is walked in date order with the units the rows before
    leave open (above zero: held; below zero: written):

    * an ``expiry`` row (``Expired``, ``Assigned``) of a held option is a
      ``sell``, of a written one a ``buy``, moving the open units toward
      zero. With nothing open, or more contracts than are open, it is
      dropped and counted in ``skipped``; so is the expiry of a short no
      row proves (below);
    * a ``cover`` row (``Buy to Close``, ``Buy to Cover``) stays a ``buy``
      when the option is written, else it is dropped and counted: the short
      it closes was opened before the export's first row. It never leaves
      the option held;
    * a ``sale`` row (``Sell to Close``) takes from what is held and never
      leaves the option written: what it sells beyond the rows before it
      was bought before the export's first row;
    * any other sale that takes the units below zero leaves a short that is
      proven only when the row opens by its own words (``Sell to Open``):
      a plain ``Sell`` may as well have sold contracts bought earlier.

    Rows keep their order; other securities are not looked at.
    """
    options: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        security = row.activity.security
        if security is not None and _is_option(security) and security.symbol:
            options.setdefault(security.symbol, []).append(index)
    dropped: set[int] = set()
    for indices in options.values():
        if not any(rows[i].closes for i in indices):
            continue
        indices.sort(key=lambda i: (rows[i].activity.trade_date, _CLOSE_ORDER[rows[i].closes]))
        net = Decimal(0)
        #: Whether the short that is open (``net`` below zero) was written by a row's own words.
        proven = True
        for index in indices:
            row = rows[index]
            activity = row.activity
            units = activity.quantity or Decimal(0)
            if row.closes == "expiry":
                if units <= 0 or net == 0 or units > abs(net) or (net < 0 and not proven):
                    dropped.add(index)
                    continue
                kind: InvestmentActivityType = "sell" if net > 0 else "buy"
                row.activity = activity.model_copy(update={"type": kind})
                net += -units if net > 0 else units
            elif row.closes == "cover":
                if net >= 0:
                    dropped.add(index)
                    continue
                net = min(net + units, Decimal(0))
            elif row.closes == "sale":
                if net < 0:
                    proven = False  # the rows disagree: nothing here says which way
                else:
                    net = max(net - units, Decimal(0))
            elif activity.type in _UNITS_IN:
                net += units
            elif activity.type in _UNITS_OUT:
                if net >= 0 and units > net:
                    proven = row.opens
                net -= units
            if net >= 0:
                proven = True
    out.skipped += len(dropped)
    return [row for index, row in enumerate(rows) if index not in dropped]


def _sign(value: Decimal | None) -> int:
    if value is None or value == 0:
        return 0
    return -1 if value < 0 else 1


def _account_key(number: str, name: str | None) -> str | None:
    """The account a row belongs to: its number's last four digits, else the whole cell.

    A masked number (``Z2xxxxxxxx``) has fewer than four digits; its cell
    text (else the account's nickname) still tells two accounts apart, so
    they never fold into one statement. The key lives only while the table
    is read.
    """
    last4 = cell_last4(number) if number else None
    if last4 is not None:
        return f"#{last4}"
    folded = normalise(number) if number else ""
    if folded:
        return "=" + folded
    return f"@{normalise(name)}" if name else None


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
        trade = parse_cell_date(_trade_date_text(_cell(row, self.col("date"))), self.date_format)
        if trade is None:
            self.out.skipped += 1
            return None
        symbol = _cell(row, self.col("symbol"))
        name = _cell(row, self.col("description"))
        security = _security(symbol, _text(row, self.col("description")))
        signed = self.number(row, "quantity")
        moves = security is not None and signed is not None and signed != 0
        amount_text = _cell(row, self.col("amount"))
        amount = parse_cell_amount(amount_text, self.separator) if amount_text else None
        action = _cell(row, self.col("action"))
        option = _is_option(security)
        words = normalise(action)
        closes: Literal["expiry", "cover", "sale"] | None = None
        if option and _starts(words, EXPIRY_WORDS):
            closes = "expiry"
        elif option and _starts(words, COVER_WORDS):
            closes = "cover"
        elif option and _starts(words, CLOSING_SALE_WORDS):
            closes = "sale"
        # An expiry's direction is not its own sign's to give: ``sell`` stands
        # in until the account's rows are read (:func:`_close_options`).
        kind: InvestmentActivityType | None = (
            "sell"
            if closes == "expiry"
            else activity_type(
                action,
                moves_security=moves,
                units_sign=_sign(signed),
                cash_sign=_sign(amount),
            )
        )
        if kind is None:
            self.out.unknown_activity += 1
            self.out.note_unknown(action)
            return None
        if (
            amount is None
            and not amount_text
            and (kind in ("transfer_in", "transfer_out") or closes == "expiry")
        ):
            amount = Decimal(0)  # a transfer of units, or an expiry, moves no cash
        quantity = abs(signed) if signed is not None and signed != 0 else None
        if kind == "dividend" and quantity is not None and _says_reinvest(action):
            # The purchase of a reinvestment printed as one row (module doc, "Rows").
            kind = "reinvest"
        elif (
            kind == "reinvest"
            and not _cell(row, self.col("quantity"))
            and amount is not None
            and amount > 0
        ):
            # The income leg of a reinvestment printed as two rows (module doc, "Rows").
            kind = "dividend"
        ratio = split_ratio(name or _cell(row, self.col("action"))) if kind == "split" else None
        if (
            amount is None
            or (kind in SECURITY_TYPES and security is None)
            or (kind in QUANTITY_TYPES and quantity is None)
            or (kind == "split" and ratio is None)
        ):
            self.out.skipped += 1
            return None
        printed = _cell(row, self.col("currency"))
        currency = known_currency(printed)
        if printed and currency is None:
            self.out.skipped += 1
            return None
        settle_text = _trade_date_text(_cell(row, self.col("settle")))
        settle = parse_cell_date(settle_text, self.date_format) if settle_text else None
        price = self.number(row, "price")
        unit_price = abs(price) if price is not None and price != 0 else None
        fees = abs(self.number(row, "commission") or Decimal(0)) + abs(
            self.number(row, "fees") or Decimal(0)
        )
        if option and unit_price is not None and quantity is not None:
            unit_price = _contract_price(quantity, unit_price, amount, fees, sale=kind == "sell")
        try:
            activity = InvestmentActivity(
                type=kind,
                trade_date=trade,
                settle_date=settle if settle is not None and settle >= trade else None,
                security=security,
                quantity=None if kind == "split" else quantity,
                unit_price=unit_price,
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
        number = _cell(row, self.col("account"))
        nickname = _text(row, self.col("account_name"))[:NAME_MAX] or None
        return _Row(
            _account_key(number, nickname),
            cell_last4(number) if number else None,
            nickname,
            currency,
            activity,
            closes,
            opens=option and kind == "sell" and _starts(words, OPENING_SALE_WORDS),
        )


def _dollar_first(amounts: Sequence[str]) -> bool:
    """Whether the amount cells are written the US way: ``$`` first (after a sign or bracket).

    At least :data:`DOLLAR_FIRST_MIN_CELLS` filled cells, and
    :data:`DOLLAR_FIRST_SHARE` of them: a handful of cells, or a column
    that only sometimes prints the symbol, says nothing.
    """
    filled = [a.strip() for a in amounts if a.strip()]
    if len(filled) < DOLLAR_FIRST_MIN_CELLS:
        return False
    marked = sum(1 for a in filled if _DOLLAR_FIRST.match(a))
    return marked >= DOLLAR_FIRST_SHARE * len(filled)


def _date_format(
    table: Table, header: Header, body: Sequence[Sequence[str]]
) -> tuple[DateFormat, bool]:
    """One format for the date and settlement columns; whether the day/month order is a guess.

    A tie between month first and day first (no day above 12) is no guess
    when two independent signs say the file is a US broker's: its whole
    header is a known one (:func:`whole_layout`) and its amounts are
    written ``$`` first (:func:`_dollar_first`). Either alone leaves the
    order a guess, and so does any other tie.
    """
    roles: tuple[Role, ...] = ("date", "settle")
    values = [_trade_date_text(_cell(r, header.columns.get(role))) for r in body for role in roles]
    found, _, tied = best_date_format(v for v in values if v)
    if (
        tied
        and found in US_TIE_FORMATS
        and whole_layout(table, header.row)
        and _dollar_first([_cell(r, header.columns.get("amount")) for r in body])
    ):
        tied = False
    return (found or "MM/DD/YYYY"), tied


def _typed_share(header: Header, body: Sequence[Sequence[str]]) -> float:
    column = header.columns["action"]
    if not body:
        return 0.0
    symbol = header.columns.get("symbol")

    def known(row: Sequence[str]) -> bool:
        action = _cell(row, column)
        if activity_type(action) is not None:
            return True
        # ``Expired``/``Assigned`` on an option row is typed by the rows around it.
        return (
            _starts(normalise(action), EXPIRY_WORDS)
            and option_symbol(_cell(row, symbol).rstrip("*")) is not None
        )

    typed = sum(1 for r in body if known(r))
    return typed / len(body)


def read_activity_table(
    table: Table, spec: MappingSpec | None = None, *, institution: str | None = None
) -> ActivityTable | None:
    """The investment statements of a brokerage activity table (None: not one; module doc).

    ``spec`` is an activity-mode column mapping a person saved for this
    layout: its columns, date format and decimal separator are used as
    given and the table is read whatever share of its actions is known
    (each unknown one is still skipped and counted). ``institution`` names
    the statements' institution; without it a known broker layout
    (:data:`LAYOUT_INSTITUTIONS`) names it.
    """
    if spec is not None:
        if spec.mode != "activity":
            return None
        header: Header | None = header_from_spec(spec)
    else:
        header = find_header(table)
    if header is None or header.row >= len(table.rows):
        return None
    institution = institution or layout_institution(table, header.row)
    rows = table.rows[header.row + 1 :]
    body = _body(table, header)
    if not body:
        return None
    if spec is None and _typed_share(header, body) < TYPED_SHARE_MIN:
        return None
    if spec is not None:
        date_format: DateFormat = spec.date_format
        guessed = False
        separator: str = spec.decimal_separator
    else:
        date_format, guessed = _date_format(table, header, body)
        amounts = [_cell(r, header.columns["amount"]) for r in body]
        separator = guess_decimal_separator(a for a in amounts if a)
    out = ActivityTable(header=header)
    reader = _Reader(table, header, date_format, separator, guessed, out)
    groups: dict[str | None, list[_Row]] = {}
    for row in rows:
        if _is_note(row, header):
            out.notes += 1
            continue
        read = reader.read(row)
        if read is None:
            continue
        groups.setdefault(read.key, []).append(read)
    mixed = False
    for group in groups.values():
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
        kept = _close_options(kept, out)
        if not kept:
            continue  # every row was a closing with nothing open: no statement
        activity = sorted((r.activity for r in kept), key=lambda a: a.trade_date)
        out.rows += len(activity)
        name = next((r.name for r in group if r.name), None)
        statement = InvestmentStatement(
            source_format="csv",
            account=StatementAccount(
                account_type=account_type_for(name),
                last4=group[0].last4,
                currency=currency,
                name=name,
                institution=institution,
            ),
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


#: The role each activity spec field reads (the inverse of :data:`SPEC_ROLES`).
_ROLE_FIELDS: Final[Mapping[Role, str]] = {role: name for name, role in SPEC_ROLES.items()}


def activity_spec(table: Table) -> MappingSpec | None:
    """The activity-mode mapping a recognised header implies (None: no brokerage header).

    The header (:func:`find_header`) need not pass the typed-actions share:
    a person shown this suggestion in the mapping editor decides.
    """
    header = find_header(table)
    if header is None:
        return None
    body = _body(table, header)
    date_format, _ = _date_format(table, header, body)
    amounts = [_cell(r, header.columns["amount"]) for r in body]
    separator = guess_decimal_separator(a for a in amounts if a)
    fields: dict[str, object] = {
        _ROLE_FIELDS[role]: column
        for role, column in header.columns.items()
        if role in _ROLE_FIELDS
    }
    try:
        return MappingSpec.model_validate(
            {
                **fields,
                "mode": "activity",
                "skip_rows": header.row,
                "date_format": date_format,
                "decimal_separator": separator,
            }
        )
    except ValidationError:
        return None


def suggest_activity_mapping(table: Table) -> MappingSuggestion | None:
    """The mapping editor's suggestion for a brokerage activity table (None: not one).

    Confident when every data row reads and the day/month order is not a
    guess; ``doubts`` otherwise (``activity_type_unknown``,
    ``rows_unreadable``, ``date_order_guessed``, ``no_rows``).
    """
    spec = activity_spec(table)
    if spec is None:
        return None
    read = read_activity_table(table, spec)
    header = header_from_spec(spec)
    body = _body(table, header)
    _, guessed = _date_format(table, header, body)
    rows = read.rows if read is not None else 0
    unknown = read.unknown_activity if read is not None else 0
    skipped = read.skipped if read is not None else 0
    doubts: list[str] = []
    if not rows:
        doubts.append("no_rows")
    if unknown:
        doubts.append("activity_type_unknown")
    if skipped:
        doubts.append("rows_unreadable")
    if guessed:
        doubts.append("date_order_guessed")
    total = rows + unknown + skipped
    return MappingSuggestion(
        spec=spec,
        score=round(rows / total, 3) if total else 0.0,
        confident=not doubts,
        reasons=tuple(
            RoleReason(role, column, "exact", 1.0)
            for role, column in sorted(header.columns.items(), key=lambda item: item[1])
        ),
        parsed_rows=rows,
        failed_rows=(),
        footer_rows=read.notes if read is not None else 0,
        accounts=len(read.statements) if read is not None else 0,
        sign_evidence="activity",
        doubts=tuple(doubts),
    )

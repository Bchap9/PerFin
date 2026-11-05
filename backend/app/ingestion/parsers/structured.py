# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured statements -> normalised transactions (INGESTION.md "Parse").

The sandbox child hands back validated records (``app.ingestion.sandbox.
protocol``): OFX/QFX/QIF *statements* with dated, signed amounts, and CSV/TSV
*tables* of text cells. This module turns both into :class:`ParsedStatement`
objects the importer (``app.domain.imports.service``) can match and commit.
It never sees file bytes: it works on the stored, schema-checked sandbox
output (``document.extraction``), so re-running an import never re-parses the
original file.

* :func:`from_statement`: OFX/QFX/QIF. Amounts are already signed from the
  account's point of view (OFX ``TRNAMT``, QIF ``T``), so they are kept as
  they are. The ledger balance (OFX ``LEDGERBAL``) becomes the statement's
  closing balance.
* :func:`from_table`: a CSV/TSV table read with a :class:`MappingSpec`
  (columns, date format, sign convention, decimal separator). Rows that do not
  parse (footers, totals, blank amounts) are skipped and reported by row
  number; the running-balance column, when mapped, gives the closing balance
  and a reconciliation check (each balance must equal the previous one plus
  the row's amount). An empty currency cell is the account's currency; any
  other cell that is not a three-letter code makes the row unreadable. A row
  with at most one filled cell (the disclaimer paragraphs and "Date
  downloaded" line brokerages append) can never be a transaction: it is a
  note row, neither imported nor reported.
* :func:`from_table_grouped`: a multi-account table (``account_column``)
  as one statement per account, each carrying only the **last four digits**
  of its account cell (:func:`cell_last4`), so the importer matches it like
  an OFX statement. The cell text is used for grouping in memory and never
  kept.
* :func:`header_fingerprint`: the identity of a table layout, so a saved
  mapping is found again for the next file with the same header.
* :func:`suggest_mapping`: a deterministic guess at the mapping (no model of
  any kind, ADR-0007): the header row searched among the first
  :data:`HEADER_SEARCH_ROWS` rows, every (column, role) pair scored by how
  the header names it (exact, whole word, substring) and whether the
  column's cells fit the role, pairs assigned globally best first; with a
  confidence (``confident``, ``score``, per-role reasons, the rows that read
  and the doubts) the importer uses to import a never-seen layout on its
  own (``app.domain.imports.service``). :func:`suggest_spec` is its spec
  alone, for the mapping editor.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from app.core.money import AMOUNT_PLACES, MAX_ABS_AMOUNT
from app.domain.imports.schemas import (
    DATE_FORMATS,
    OUTFLOW_VALUES_MAX,
    DateFormat,
    MappingSpec,
    Sign,
)
from app.ingestion.sandbox.protocol import Reconciliation, Statement, Table

FINGERPRINT_VERSION: Final = "perfin-header-v1"
#: How many leading rows are tried as the header (saved mappings and suggestions).
HEADER_SEARCH_ROWS: Final = 11
_US: Final = "\x1f"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedSplit:
    amount: Decimal
    category: str | None
    memo: str | None


@dataclass(frozen=True)
class ParsedTransaction:
    posted_date: date
    amount: Decimal
    raw_payee: str | None = None
    memo: str | None = None
    currency: str | None = None
    category: str | None = None
    check_number: str | None = None
    splits: tuple[ParsedSplit, ...] = ()
    #: Index of the source row in the table (CSV/TSV), for problem reports.
    row: int | None = None
    #: Document parsers (PDF, scans): the row's lowest field confidence, the
    #: fields below the threshold (imported as ``review_state = pending``)
    #: and the page it was read from.
    confidence: float | None = None
    low_confidence: tuple[str, ...] = ()
    page: int | None = None


@dataclass(frozen=True)
class ClosingBalance:
    amount: Decimal
    as_of: date


@dataclass
class ParsedStatement:
    """One account's worth of transactions from a document."""

    source_format: str
    transactions: list[ParsedTransaction] = field(default_factory=list)
    last4: str | None = None
    institution: str | None = None
    account_type: str | None = None
    currency: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    closing_balance: ClosingBalance | None = None
    #: Table rows (0-based) that could not be read with the mapping.
    skipped_rows: list[int] = field(default_factory=list)
    #: Table rows with at most one filled cell (a disclaimer paragraph, a
    #: "Date downloaded" line): never a transaction, not reported as skipped.
    note_rows: list[int] = field(default_factory=list)
    #: Rows whose running balance disagrees with the previous row plus the amount.
    balance_mismatches: list[int] = field(default_factory=list)
    #: The date order was guessed (QIF dates that fit both M/D and D/M).
    date_order_assumed: bool = False
    truncated: bool = False
    #: Document parsers: the printed opening balance and the reconciliation
    #: of opening + transactions = closing (``app.ingestion.reconcile``).
    opening_balance: Decimal | None = None
    reconciliation: Reconciliation | None = None
    #: Which parser read it (``name/version``), for review items.
    parser: str | None = None

    @property
    def reconciled(self) -> bool:
        """False for a parsed document statement whose figures do not agree."""
        return self.reconciliation is None or self.reconciliation.status == "ok"


# ---------------------------------------------------------------------------
# OFX / QFX / QIF
# ---------------------------------------------------------------------------


def from_statement(statement: Statement) -> ParsedStatement:
    """A statement extracted from OFX, QFX or QIF, or read by a document parser, as is."""
    closing: ClosingBalance | None = None
    ledger = statement.ledger_balance
    if ledger is not None:
        as_of = ledger.as_of or statement.period_end
        if as_of is not None:
            closing = ClosingBalance(ledger.amount, as_of)
    transactions = [
        ParsedTransaction(
            posted_date=t.posted,
            amount=t.amount,
            raw_payee=t.payee,
            memo=t.memo,
            currency=t.currency,
            category=t.category,
            check_number=t.check_number,
            splits=tuple(ParsedSplit(s.amount, s.category, s.memo) for s in t.splits),
            confidence=t.confidence,
            low_confidence=tuple(t.low_confidence),
            page=t.page,
        )
        for t in statement.transactions
    ]
    parser = statement.parser
    return ParsedStatement(
        source_format=statement.source_format,
        transactions=transactions,
        last4=statement.account.last4,
        institution=statement.account.institution,
        account_type=statement.account.account_type,
        currency=statement.account.currency,
        period_start=statement.period_start,
        period_end=statement.period_end,
        closing_balance=closing,
        date_order_assumed=statement.date_order == "mdy_assumed",
        truncated=statement.truncated,
        opening_balance=statement.opening_balance.amount if statement.opening_balance else None,
        reconciliation=statement.reconciliation,
        parser=f"{parser.name}/{parser.version}" if parser is not None else None,
    )


# ---------------------------------------------------------------------------
# Header fingerprints
# ---------------------------------------------------------------------------

_SPACE: Final = re.compile(r"\s+")


def _header_cell(cell: str) -> str:
    text = cell[1:] if cell.startswith("'") else cell
    return _SPACE.sub(" ", text).strip().casefold()


def _shape(cell: str) -> str:
    text = cell.strip()
    if not text:
        return "e"
    if any(_parse_date(text, fmt) is not None for fmt in DATE_FORMATS):
        return "d"
    if parse_cell_amount(text, ".") is not None or parse_cell_amount(text, ",") is not None:
        return "n"
    return "t"


def header_fingerprint(row: Sequence[str], *, has_header: bool = True) -> str:
    """The layout identity of a table whose header (or first data row) is ``row``.

    With a header: SHA-256 over the version, the column count and every
    header cell (whitespace collapsed, case folded, a formula-guard quote
    removed). Without one: over the column count and the *shape* of each cell
    of the first data row (date, number, text, empty).
    """
    if has_header:
        cells = [_header_cell(c) for c in row]
        marker = "header"
    else:
        cells = [_shape(c) for c in row]
        marker = "shape"
    text = _US.join((FINGERPRINT_VERSION, marker, str(len(row)), *cells))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def spec_fingerprint(table: Table, spec: MappingSpec) -> str | None:
    """The fingerprint of ``table`` read with ``spec`` (None: too few rows)."""
    if len(table.rows) <= spec.skip_rows:
        return None
    return header_fingerprint(table.rows[spec.skip_rows], has_header=spec.has_header)


def candidate_fingerprints(table: Table) -> dict[tuple[int, bool], str]:
    """Fingerprints of the first rows, keyed by ``(skip_rows, has_header)``."""
    out: dict[tuple[int, bool], str] = {}
    for index, row in enumerate(table.rows[:HEADER_SEARCH_ROWS]):
        out[(index, True)] = header_fingerprint(row, has_header=True)
        out[(index, False)] = header_fingerprint(row, has_header=False)
    return out


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------

_MONTHS: Final = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_TIME_SUFFIX: Final = (
    r"(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s?[AaPp][Mm])?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
_D: Final = r"(?P<d>\d{1,2})"
_M: Final = r"(?P<m>\d{1,2})"
_Y4: Final = r"(?P<y>\d{4})"
_Y2: Final = r"(?P<yy>\d{2})"
_B: Final = r"(?P<b>[A-Za-z]{3})[A-Za-z]*\.?"
_DATE_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    fmt: re.compile(rf"^{pattern}{_TIME_SUFFIX}$")
    for fmt, pattern in {
        "YYYY-MM-DD": rf"{_Y4}-{_M}-{_D}",
        "YYYY/MM/DD": rf"{_Y4}/{_M}/{_D}",
        "YYYY.MM.DD": rf"{_Y4}\.{_M}\.{_D}",
        "YYYYMMDD": r"(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})",
        "MM/DD/YYYY": rf"{_M}/{_D}/{_Y4}",
        "MM/DD/YY": rf"{_M}/{_D}/{_Y2}",
        "MM-DD-YYYY": rf"{_M}-{_D}-{_Y4}",
        "DD/MM/YYYY": rf"{_D}/{_M}/{_Y4}",
        "DD/MM/YY": rf"{_D}/{_M}/{_Y2}",
        "DD-MM-YYYY": rf"{_D}-{_M}-{_Y4}",
        "DD.MM.YYYY": rf"{_D}\.{_M}\.{_Y4}",
        "DD.MM.YY": rf"{_D}\.{_M}\.{_Y2}",
        "DD MMM YYYY": rf"{_D} {_B} {_Y4}",
        "DD-MMM-YYYY": rf"{_D}-{_B}-{_Y4}",
        "MMM DD, YYYY": rf"{_B} {_D},? {_Y4}",
    }.items()
}


def _parse_date(text: str, fmt: str) -> date | None:
    match = _DATE_PATTERNS[fmt].match(text.strip())
    if match is None:
        return None
    groups = match.groupdict()
    if groups.get("y") is not None:
        year = int(groups["y"])
    else:
        short = int(groups["yy"])
        year = 2000 + short if short < 70 else 1900 + short
    if groups.get("b") is not None:
        month = _MONTHS.get(groups["b"].lower())
        if month is None:
            return None
    else:
        month = int(groups["m"])
    try:
        return date(year, month, int(groups["d"]))
    except ValueError:
        return None


def parse_cell_date(text: str, fmt: DateFormat) -> date | None:
    """A date from a table cell in ``fmt`` (:data:`DATE_FORMATS`), or None."""
    return _parse_date(text, fmt)


_STRIP: Final = re.compile("[\\s\u00a0\u202f'\u2019$\u20ac\u00a3\u00a5\u20b9]|[A-Z]{3}$|^[A-Z]{3}")
_PLAIN_DECIMAL: Final = re.compile(r"^\d+(?:\.\d+)?$")


def parse_cell_amount(text: str, decimal_separator: str) -> Decimal | None:
    """A signed amount from a table cell, or None when it is not one.

    Accepts ``1,234.56`` / ``1.234,56`` (by ``decimal_separator``), a leading
    ``+``/``-``, ``(12.34)`` and ``12.34-`` as negatives, currency symbols,
    a leading or trailing ISO code, spaces and apostrophes. At most
    :data:`~app.core.money.AMOUNT_PLACES` decimals.
    """
    value = text.strip()
    if value.startswith("'"):  # the import-side formula guard (C7)
        value = value[1:]
    value = _STRIP.sub("", value.upper())
    negative = False
    if value.startswith("(") and value.endswith(")"):
        negative, value = True, value[1:-1]
    if value.endswith("-"):
        negative, value = not negative, value[:-1]
    if value.startswith(("+", "-")):
        if value[0] == "-":
            negative = not negative
        value = value[1:]
    value = _STRIP.sub("", value)
    if not value:
        return None
    group = "," if decimal_separator == "." else "."
    if decimal_separator == ".":
        value = value.replace(group, "")
    else:
        value = value.replace(group, "").replace(",", ".")
    if not _PLAIN_DECIMAL.fullmatch(value):
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation:  # pragma: no cover - the pattern admits only numbers
        return None
    exponent = amount.as_tuple().exponent
    if not isinstance(exponent, int) or -exponent > AMOUNT_PLACES:
        return None
    if amount > MAX_ABS_AMOUNT:
        return None
    return -amount if negative else amount


def _cell(row: Sequence[str], column: int | None) -> str | None:
    if column is None or column >= len(row):
        return None
    return row[column]


def _text(row: Sequence[str], column: int | None) -> str | None:
    value = _cell(row, column)
    if value is None:
        return None
    cleaned = _SPACE.sub(" ", value).strip()
    return cleaned or None


def _row_amount(row: Sequence[str], spec: MappingSpec) -> Decimal | None:
    sep = spec.decimal_separator
    if spec.amount_column is not None:
        raw = _cell(row, spec.amount_column)
        amount = parse_cell_amount(raw, sep) if raw is not None else None
        if amount is None:
            return None
        if spec.indicator_column is not None:
            indicator = (_text(row, spec.indicator_column) or "").casefold()
            outflow = indicator in {v.casefold() for v in spec.outflow_values}
            return -abs(amount) if outflow else abs(amount)
        return -amount if spec.sign is Sign.OUTFLOW_POSITIVE else amount
    debit_raw = _text(row, spec.debit_column)
    credit_raw = _text(row, spec.credit_column)
    debit = parse_cell_amount(debit_raw, sep) if debit_raw else None
    credit = parse_cell_amount(credit_raw, sep) if credit_raw else None
    if (debit_raw and debit is None) or (credit_raw and credit is None):
        return None
    if debit is None and credit is None:
        return None
    return abs(credit or Decimal(0)) - abs(debit or Decimal(0))


def _currency(row: Sequence[str], spec: MappingSpec) -> str | bool | None:
    """The row's currency; ``False`` when the column holds something else.

    An empty cell is no currency (the account's), like a file without a
    currency column: exports leave it blank on rows in the account currency.
    """
    if spec.currency is not None:
        return spec.currency
    if spec.currency_column is None:
        return None
    value = (_text(row, spec.currency_column) or "").upper()
    if not value:
        return None
    if re.fullmatch(r"[A-Z]{3}", value):
        return value
    return False


def cell_last4(text: str | None) -> str | None:
    """The last four digits of an account-number cell (None: fewer than four digits)."""
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    return digits[-4:] if len(digits) >= 4 else None


def _account_key(row: Sequence[str], column: int) -> str:
    """How rows are grouped by account: the last four digits, else the folded text."""
    value = _cell(row, column) or ""
    found = cell_last4(value)
    return f"#{found}" if found is not None else _header_cell(value)


def _body_rows(table: Table, spec: MappingSpec) -> range:
    return range(spec.skip_rows + (1 if spec.has_header else 0), len(table.rows))


def _filled(row: Sequence[str]) -> int:
    """How many cells of ``row`` hold something: 0, 1 or 2 (two or more)."""
    seen = 0
    for cell in row:
        if cell.strip():
            seen += 1
            if seen == 2:
                break
    return seen


def _read_rows(table: Table, spec: MappingSpec, indexes: Iterable[int]) -> ParsedStatement:
    """``indexes`` rows of ``table`` read with ``spec``, as one statement."""
    parsed = ParsedStatement(source_format="csv", truncated=table.truncated)
    balances: list[tuple[int, date, Decimal, Decimal]] = []
    for index in indexes:
        row = table.rows[index]
        filled = _filled(row)
        if not filled:
            continue
        if filled == 1:
            parsed.note_rows.append(index)
            continue
        raw_date = _cell(row, spec.date_column)
        posted = parse_cell_date(raw_date, spec.date_format) if raw_date else None
        amount = _row_amount(row, spec)
        currency = _currency(row, spec)
        if posted is None or amount is None or currency is False:
            parsed.skipped_rows.append(index)
            continue
        parsed.transactions.append(
            ParsedTransaction(
                posted_date=posted,
                amount=amount,
                raw_payee=_text(row, spec.payee_column),
                memo=_text(row, spec.memo_column),
                currency=currency if isinstance(currency, str) else None,
                row=index,
            )
        )
        if spec.balance_column is not None:
            raw_balance = _text(row, spec.balance_column)
            balance = (
                parse_cell_amount(raw_balance, spec.decimal_separator) if raw_balance else None
            )
            if balance is not None:
                balances.append((index, posted, amount, balance))
    if parsed.transactions:
        dates = [t.posted_date for t in parsed.transactions]
        parsed.period_start, parsed.period_end = min(dates), max(dates)
        currencies = {t.currency for t in parsed.transactions if t.currency}
        if len(currencies) == 1:
            parsed.currency = next(iter(currencies))
    if balances:
        chronological = _chronological(balances)
        _, last_date, _, last_balance = chronological[-1]
        parsed.closing_balance = ClosingBalance(last_balance, last_date)
        parsed.balance_mismatches = _reconcile(chronological)
    return parsed


def from_table(table: Table, spec: MappingSpec) -> ParsedStatement:
    """Read every body row of ``table`` with ``spec`` as one statement (module doc).

    With an ``account_column`` the rows of all accounts are read together
    (the preview, the suggestion's checks); ``last4`` is set only when every
    readable row names the same account. The importer reads such a table
    with :func:`from_table_grouped`.
    """
    parsed = _read_rows(table, spec, _body_rows(table, spec))
    if spec.account_column is not None and parsed.transactions:
        column = spec.account_column
        found = {
            cell_last4(_cell(table.rows[t.row], column))
            for t in parsed.transactions
            if t.row is not None
        }
        if len(found) == 1:
            parsed.last4 = next(iter(found))
    return parsed


def from_table_grouped(table: Table, spec: MappingSpec) -> list[ParsedStatement]:
    """One statement per account of a multi-account table (``spec.account_column``).

    Rows are grouped by the last four digits of the account cell (cells with
    fewer digits by their folded text, so two unnamed accounts stay apart),
    in the order the accounts first appear among the readable rows; each
    group is read like :func:`from_table` and carries only ``last4`` (the
    cell text is never kept). Unreadable rows are reported in their
    account's group; those of no group with a readable row (footers, totals)
    in the first group, like the note rows. Without an ``account_column``: the one
    statement of :func:`from_table`. A table without any readable row gives
    one empty statement listing the skipped rows.
    """
    if spec.account_column is None:
        return [from_table(table, spec)]
    column = spec.account_column
    rows: dict[str, list[int]] = {}
    for index in _body_rows(table, spec):
        row = table.rows[index]
        if any(cell.strip() for cell in row):
            rows.setdefault(_account_key(row, column), []).append(index)
    groups: list[tuple[int, ParsedStatement]] = []
    orphans: list[int] = []
    notes: list[int] = []
    for key, indexes in rows.items():
        parsed = _read_rows(table, spec, indexes)
        if not parsed.transactions:
            orphans.extend(parsed.skipped_rows)
            notes.extend(parsed.note_rows)
            continue
        parsed.last4 = key[1:] if key.startswith("#") else None
        first = parsed.transactions[0].row
        groups.append((first if first is not None else 0, parsed))
    groups.sort(key=lambda pair: pair[0])
    statements = [parsed for _, parsed in groups]
    if not statements:
        return [
            ParsedStatement(
                source_format="csv",
                skipped_rows=sorted(orphans),
                note_rows=sorted(notes),
                truncated=table.truncated,
            )
        ]
    if orphans:
        statements[0].skipped_rows = sorted([*statements[0].skipped_rows, *orphans])
    if notes:
        statements[0].note_rows = sorted([*statements[0].note_rows, *notes])
    return statements


def group_last4(table: Table, spec: MappingSpec, group: int) -> str | None:
    """The last four digits of account group ``group`` of :func:`from_table_grouped`."""
    statements = from_table_grouped(table, spec)
    if spec.account_column is None or not 0 <= group < len(statements):
        return None
    return statements[group].last4


def _chronological(
    rows: list[tuple[int, date, Decimal, Decimal]],
) -> list[tuple[int, date, Decimal, Decimal]]:
    """Rows in time order: files list newest-first or oldest-first; keep their order."""
    if len(rows) > 1 and rows[0][1] > rows[-1][1]:
        return list(reversed(rows))
    return rows


def _reconcile(rows: Iterable[tuple[int, date, Decimal, Decimal]]) -> list[int]:
    """Rows whose balance is not the previous balance plus their amount."""
    mismatches: list[int] = []
    previous: Decimal | None = None
    for index, _, amount, balance in rows:
        if previous is not None and previous + amount != balance:
            mismatches.append(index)
        previous = balance
    return mismatches


# ---------------------------------------------------------------------------
# Suggestions for the mapping editor (and the importer's auto-mapping)
# ---------------------------------------------------------------------------

#: Body rows looked at per candidate header row when scoring a suggestion.
SUGGEST_SAMPLE_ROWS: Final = 200
#: Share of a column's non-empty sample cells that must fit a role.
ROLE_FIT_MIN: Final = 0.6

#: How a header cell matched one of a role's words.
EXACT: Final = 3
WORD: Final = 2
SUBSTRING: Final = 1
MATCH_NAMES: Final[dict[int, str]] = {EXACT: "exact", WORD: "word", SUBSTRING: "substring"}

#: Roles in tie-break order, with the header words that name them.
ROLE_WORDS: Final[dict[str, tuple[str, ...]]] = {
    "date": (
        "date",
        "posting date",
        "posted date",
        "post date",
        "booking date",
        "transaction date",
        "trans date",
        "trans. date",
        "trade date",
        "run date",
        "value date",
        "settlement date",
        "settle date",
        "posted",
    ),
    "amount": (
        "amount",
        "value",
        "sum",
        "transaction amount",
        "net amount",
        "amount ($)",
        "debit/credit",
        "credit/debit",
    ),
    "debit": ("debit", "debits", "withdrawal", "withdrawals", "money out", "paid out", "outflow"),
    "credit": ("credit", "credits", "deposit", "deposits", "money in", "paid in", "inflow"),
    "indicator": (
        "dr/cr",
        "cr/dr",
        "debit/credit",
        "credit/debit",
        "type",
        "direction",
        "transaction type",
    ),
    "balance": ("balance", "running balance", "available balance", "cash balance"),
    "currency": ("currency", "ccy", "curr"),
    "account": (
        "account number",
        "account no",
        "account no.",
        "account #",
        "account nr",
        "acct",
        "acct no",
        "acct #",
        "account",
    ),
    "payee": ("payee", "description", "merchant", "name", "counterparty", "beneficiary"),
    "memo": (
        "memo",
        "details",
        "reference",
        "notes",
        "narrative",
        "purpose",
        "category",
        "remarks",
        "action",
    ),
}
_ROLE_ORDER: Final = {role: position for position, role in enumerate(ROLE_WORDS)}
_MONEY_ROLES: Final = frozenset({"amount", "debit", "credit", "balance"})
#: Indicator cells that mean money out / money in (case-insensitive).
OUTFLOW_WORDS: Final = frozenset({"dr", "d", "debit", "out", "withdrawal", "outflow", "db"})
INFLOW_WORDS: Final = frozenset({"cr", "c", "credit", "in", "deposit", "inflow"})
_ACCOUNT_CELL: Final = re.compile(r"[A-Za-z0-9*#.\- ]{4,34}")
_SETTLEMENT_DATE: Final = re.compile(r"settle|value|effective|due")
_POSTING_DATE: Final = re.compile(r"post|book")
_TRANSACTION_DATE: Final = re.compile(r"trans|trade|run")


@dataclass(frozen=True)
class RoleReason:
    """Why a column got a role: how its header matched and how many cells fit."""

    role: str
    column: int
    #: ``exact`` / ``word`` / ``substring`` (:data:`MATCH_NAMES`).
    match: str
    #: Share of the column's non-empty sample cells that fit the role (0-1).
    fit: float


@dataclass(frozen=True)
class MappingSuggestion:
    """A guessed mapping and how far it can be trusted (:func:`suggest_mapping`).

    ``confident`` is True when ``doubts`` is empty: every required role
    (date, and the amount or debit/credit columns) matched its header
    exactly, every body row before the footer reads and, with an indicator
    column, every read row's indicator is a direction word beside an
    unsigned amount (else ``indicator_values_unknown``). ``sign_evidence``
    says what proves the sign convention (``debit_credit``, ``indicator``,
    a reconciling running ``balance``) or is None when a single signed
    amount column is read as "positive is money in" on trust. Reasons carry
    roles, column indexes, match kinds and shares only, never cell text.
    """

    spec: MappingSpec
    score: float
    confident: bool
    reasons: tuple[RoleReason, ...]
    #: Body rows that read, that did not (before the last readable row) and
    #: trailing rows after it (footers, disclaimers, "Date downloaded").
    parsed_rows: int
    failed_rows: tuple[int, ...]
    footer_rows: int
    #: Distinct accounts when ``spec.account_column`` is set (else 1).
    accounts: int
    sign_evidence: str | None
    doubts: tuple[str, ...]

    def summary(self) -> dict[str, object]:
        """JSON facts for review items, run summaries and the preview (ids and counts)."""
        return {
            "score": self.score,
            "confident": self.confident,
            "skip_rows": self.spec.skip_rows,
            "parsed_rows": self.parsed_rows,
            "failed_rows": len(self.failed_rows),
            "footer_rows": self.footer_rows,
            "accounts": self.accounts,
            "sign_evidence": self.sign_evidence,
            "doubts": list(self.doubts),
            "roles": [
                {"role": r.role, "column": r.column, "match": r.match, "fit": r.fit}
                for r in self.reasons
            ],
        }


def _match_level(header: str, words: Iterable[str]) -> int:
    """How well ``header`` names a role: exact beats whole word beats substring."""
    best = 0
    for word in words:
        if header == word:
            return EXACT
        if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", header):
            best = max(best, WORD)
        elif len(word) > 3 and word in header:
            best = max(best, SUBSTRING)
    return best


def _preference(role: str, header: str) -> float:
    """Tie-breaks among columns of one role: booking dates over settlement dates."""
    if role != "date":
        return 0.0
    if _SETTLEMENT_DATE.search(header):
        return -1.0
    if _POSTING_DATE.search(header):
        return 0.3
    if _TRANSACTION_DATE.search(header):
        return 0.2
    return 0.0


def best_date_format(values: Iterable[str]) -> tuple[DateFormat | None, int, bool]:
    """The format reading most of ``values``, how many it reads, and whether another reads as many.

    Ties go to the earlier format of :data:`DATE_FORMATS` (month first
    before day first), so the third value says the day/month order is a
    guess (every day of the sample is 12 or less).
    """
    sample = [v.strip() for v in values if v.strip()][:SUGGEST_SAMPLE_ROWS]
    best: DateFormat | None = None
    best_count = 0
    tied = False
    for fmt in DATE_FORMATS:
        count = sum(1 for v in sample if _parse_date(v, fmt) is not None)
        if count > best_count:
            best, best_count, tied = fmt, count, False  # type: ignore[assignment]
        elif count and count == best_count:
            tied = True
    return best, best_count, tied


def _looks_like_account(text: str) -> bool:
    value = text.strip().removeprefix("'")
    if not _ACCOUNT_CELL.fullmatch(value):
        return False
    digits = sum(c.isdigit() for c in value)
    masks = sum(c in "*#xX" for c in value)
    letters = sum(c.isalpha() and c not in "xX" for c in value)
    return digits >= 1 and digits + masks >= 4 and letters <= 4


def _is_money(text: str) -> bool:
    if _shape(text) == "d":
        return False
    return parse_cell_amount(text, ".") is not None or parse_cell_amount(text, ",") is not None


def _role_fit(role: str, values: list[str]) -> float:
    """Share of the non-empty ``values`` that fit ``role`` (0: the role is out)."""
    cells = [v.strip() for v in values if v.strip()]
    if not cells:
        return 0.0
    if role == "date":
        _, count, _ = best_date_format(cells)
        return count / len(cells)
    if role in _MONEY_ROLES:
        return sum(1 for c in cells if _is_money(c)) / len(cells)
    if role == "currency":
        return sum(1 for c in cells if re.fullmatch(r"[A-Z]{3}", c.upper())) / len(cells)
    if role == "indicator":
        folded = [c.casefold() for c in cells]
        if not any(c in OUTFLOW_WORDS for c in folded):
            return 0.0
        return sum(1 for c in folded if c in OUTFLOW_WORDS | INFLOW_WORDS) / len(cells)
    if role == "account":
        if len({_header_cell(c) for c in cells}) < 2:
            return 0.0
        return sum(1 for c in cells if _looks_like_account(c)) / len(cells)
    # payee / memo: text, not a column of numbers or dates.
    return sum(1 for c in cells if _shape(c) == "t") / len(cells)


@dataclass(frozen=True)
class _Pick:
    column: int
    level: int
    fit: float


def _assign(header: Sequence[str], body: Sequence[Sequence[str]]) -> dict[str, _Pick]:
    """Roles for the columns of ``header``, best (column, role) pair first."""
    names = [_header_cell(c) for c in header]

    def column_values(column: int) -> list[str]:
        return [row[column] for row in body if column < len(row)]

    pairs: list[tuple[float, int, int, str, float]] = []
    memo_candidates: list[tuple[float, int, int, float]] = []
    for column, name in enumerate(names):
        if not name:
            continue
        values = column_values(column)
        for role, words in ROLE_WORDS.items():
            level = _match_level(name, words)
            if not level:
                continue
            fit = _role_fit(role, values)
            if fit < ROLE_FIT_MIN:
                continue
            score = level + _preference(role, name)
            pairs.append((score, column, level, role, fit))
            if role == "memo":
                memo_candidates.append((score, column, level, fit))
    pairs.sort(key=lambda p: (-p[0], _ROLE_ORDER[p[3]], p[1]))
    picks: dict[str, _Pick] = {}
    used: set[int] = set()
    for _, column, level, role, fit in pairs:
        if role in picks or column in used:
            continue
        picks[role] = _Pick(column, level, round(fit, 3))
        used.add(column)
    if "payee" not in picks and "memo" in picks:
        # The best text column is the payee; the next one, if any, the memo.
        picks["payee"] = picks.pop("memo")
        memo_candidates.sort(key=lambda p: (-p[0], p[1]))
        for _, column, level, fit in memo_candidates:
            if column not in used:
                picks["memo"] = _Pick(column, level, round(fit, 3))
                used.add(column)
                break
    return picks


def guess_date_format(values: Iterable[str]) -> DateFormat | None:
    """The first format that reads every non-empty value (day-first only if needed)."""
    sample = [v.strip() for v in values if v.strip()][:200]
    if not sample:
        return None
    for fmt in DATE_FORMATS:
        if all(_parse_date(v, fmt) is not None for v in sample):
            return fmt  # type: ignore[return-value]
    return None


def guess_decimal_separator(values: Iterable[str]) -> str:
    comma = point = 0
    for value in values:
        text = value.strip()
        if re.search(r",\d{1,2}\)?-?$", text):
            comma += 1
        elif re.search(r"\.\d{1,2}\)?-?$", text):
            point += 1
    return "," if comma > point else "."


def _data_rows(rows: Sequence[Sequence[str]]) -> list[Sequence[str]]:
    """Rows with at least two filled cells (a disclaimer paragraph is one cell)."""
    return [row for row in rows if _filled(row) == 2]


@dataclass(frozen=True)
class _Candidate:
    spec: MappingSpec
    picks: dict[str, _Pick]
    date_order_guessed: bool
    rank: float


def _candidate(table: Table, skip: int) -> _Candidate | None:
    """The mapping read with row ``skip`` as the header, or None when it does not fit."""
    header = table.rows[skip]
    if sum(1 for c in header if c.strip()) < 2:
        return None
    body = _data_rows(table.rows[skip + 1 : skip + 1 + SUGGEST_SAMPLE_ROWS])
    if not body:
        return None
    picks = _assign(header, body)
    if "date" not in picks or not ({"amount", "debit", "credit"} & set(picks)):
        return None

    def column_values(column: int) -> list[str]:
        return [row[column] for row in body if column < len(row)]

    date_format, _, tied = best_date_format(column_values(picks["date"].column))
    if date_format is None:
        return None
    money = [picks[r].column for r in ("amount", "debit", "credit", "balance") if r in picks]
    separator = guess_decimal_separator(v for c in money for v in column_values(c))
    fields: dict[str, object] = {
        "skip_rows": skip,
        "date_column": picks["date"].column,
        "date_format": date_format,
        "decimal_separator": separator,
        "payee_column": picks["payee"].column if "payee" in picks else None,
        "memo_column": picks["memo"].column if "memo" in picks else None,
        "balance_column": picks["balance"].column if "balance" in picks else None,
        "currency_column": picks["currency"].column if "currency" in picks else None,
        "account_column": picks["account"].column if "account" in picks else None,
    }
    if "amount" in picks:
        fields["amount_column"] = picks["amount"].column
        picks.pop("debit", None)
        picks.pop("credit", None)
        if "indicator" in picks and _signed(column_values(picks["amount"].column), separator):
            # The amounts carry their own sign: a "Type" column of direction
            # words beside them is a description, not the sign.
            picks.pop("indicator")
        if "indicator" in picks:
            values = {v.strip() for v in column_values(picks["indicator"].column) if v.strip()}
            outflow = sorted(v for v in values if v.casefold() in OUTFLOW_WORDS)
            fields["indicator_column"] = picks["indicator"].column
            fields["outflow_values"] = outflow[:OUTFLOW_VALUES_MAX]
    else:
        picks.pop("indicator", None)
        fields["debit_column"] = picks["debit"].column if "debit" in picks else None
        fields["credit_column"] = picks["credit"].column if "credit" in picks else None
    try:
        spec = MappingSpec.model_validate(fields)
    except ValueError:
        return None
    rank = sum(p.level + p.fit for p in picks.values())
    return _Candidate(spec, picks, tied, rank)


def _signed(values: Iterable[str], separator: str) -> bool:
    """Whether any of the amount cells ``values`` is negative."""
    for value in values:
        amount = parse_cell_amount(value, separator) if value.strip() else None
        if amount is not None and amount < 0:
            return True
    return False


def _indicator_doubtful(table: Table, spec: MappingSpec, rows: Iterable[int]) -> bool:
    """Whether a read row's indicator is no direction word, or its amount is signed.

    Either way the indicator does not decide the row's sign on its own
    (:func:`_row_amount` takes ``abs`` of the amount and treats every value
    outside ``outflow_values`` as money in), so the suggestion is not
    trusted. The whole table is checked, not only the sample the roles
    were assigned from.
    """
    if spec.indicator_column is None or spec.amount_column is None:
        return False
    for index in rows:
        row = table.rows[index]
        word = (_text(row, spec.indicator_column) or "").casefold()
        if word not in OUTFLOW_WORDS | INFLOW_WORDS:
            return True
        raw = _cell(row, spec.amount_column)
        amount = parse_cell_amount(raw, spec.decimal_separator) if raw is not None else None
        if amount is not None and amount < 0:
            return True
    return False


def _balances_agree(statements: list[ParsedStatement]) -> bool:
    checked = sum(1 for s in statements for _ in s.transactions)
    return checked >= 2 and not any(s.balance_mismatches for s in statements)


def _read_suggested(
    table: Table, spec: MappingSpec
) -> tuple[MappingSpec, list[ParsedStatement], bool]:
    """The whole table read with ``spec``, the sign its running balance agrees with.

    Returns the spec (``sign`` switched to ``outflow_positive`` when only
    that reconciles), its statements and whether a balance proved the sign.
    One pass over the table, a second only when "positive is money in" does
    not reconcile (both cannot, unless every amount is zero).
    """
    statements = from_table_grouped(table, spec)
    if (
        spec.balance_column is None
        or spec.amount_column is None
        or spec.indicator_column is not None
    ):
        return spec, statements, False
    if _balances_agree(statements):
        return spec, statements, True
    flipped = spec.model_copy(update={"sign": Sign.OUTFLOW_POSITIVE})
    flipped_statements = from_table_grouped(table, flipped)
    if _balances_agree(flipped_statements):
        return flipped, flipped_statements, True
    return spec, statements, False


def suggest_mapping(table: Table) -> MappingSuggestion | None:
    """A deterministic guess at the mapping of ``table`` and how confident it is.

    The header is looked for in the first :data:`HEADER_SEARCH_ROWS` rows
    (``skip_rows`` is the preamble above it). For each candidate header
    every (column, role) pair is scored, an exact header word beating a
    whole word inside the header beating a substring, posting and
    transaction dates beating settlement and value dates, and the pairs are
    assigned globally, best first, one role per column. A column only takes
    a role when its body cells fit (dates parse, amounts parse, currencies
    are three-letter codes, an indicator holds direction words such as
    ``DR``/``CR``, an account column several account-number-like values);
    an empty column never takes one, and an indicator is dropped when the
    sampled amounts already carry a minus sign (a "Type" column beside a
    signed amount describes the row, it does not sign it). The best candidate is then read in full
    with :func:`from_table_grouped` to count the rows that read (and, with a
    running balance, which sign reconciles); the trailing rows after the last
    readable one are footers. No model of any kind (ADR-0007).
    """
    if len(table.rows) < 2:
        return None
    best: _Candidate | None = None
    for skip in range(min(HEADER_SEARCH_ROWS, len(table.rows) - 1)):
        found = _candidate(table, skip)
        if found is not None and (best is None or found.rank > best.rank):
            best = found
    if best is None:
        return None
    spec, statements, balance_agrees = _read_suggested(table, best.spec)
    rows = [t.row for s in statements for t in s.transactions if t.row is not None]
    skipped = sorted(r for s in statements for r in s.skipped_rows)
    notes = [r for s in statements for r in s.note_rows]
    last = max(rows, default=-1)
    failed = tuple(r for r in skipped if r < last)
    footer = sum(1 for r in (*skipped, *notes) if r > last)
    accounts = sum(1 for s in statements if s.transactions) or 1
    if spec.debit_column is not None or spec.credit_column is not None:
        evidence: str | None = "debit_credit"
    elif spec.indicator_column is not None:
        evidence = "indicator"
    elif balance_agrees:
        evidence = "balance"
    else:
        evidence = None
    required = [best.picks["date"]] + [
        best.picks[r] for r in ("amount", "debit", "credit") if r in best.picks
    ]
    doubts: list[str] = []
    if any(p.level != EXACT for p in required):
        doubts.append("header_not_exact")
    if not rows:
        doubts.append("no_rows")
    if failed:
        doubts.append("rows_unreadable")
    if best.date_order_guessed:
        doubts.append("date_order_guessed")
    if _indicator_doubtful(table, spec, rows):
        doubts.append("indicator_values_unknown")
    readable = len(rows) / (len(rows) + len(failed)) if rows else 0.0
    quality = sum(p.level / EXACT for p in required) / len(required)
    score = round(quality * readable * (0.9 if best.date_order_guessed else 1.0), 3)
    reasons = tuple(
        RoleReason(role, pick.column, MATCH_NAMES[pick.level], pick.fit)
        for role, pick in sorted(best.picks.items(), key=lambda item: item[1].column)
    )
    return MappingSuggestion(
        spec=spec,
        score=score,
        confident=not doubts,
        reasons=reasons,
        parsed_rows=len(rows),
        failed_rows=failed,
        footer_rows=footer,
        accounts=accounts,
        sign_evidence=evidence,
        doubts=tuple(doubts),
    )


def suggest_spec(table: Table) -> MappingSpec | None:
    """A first guess for the mapping editor, or None when nothing fits (:func:`suggest_mapping`)."""
    suggestion = suggest_mapping(table)
    return suggestion.spec if suggestion is not None else None

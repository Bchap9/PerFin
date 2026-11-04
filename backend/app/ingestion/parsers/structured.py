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
  the row's amount).
* :func:`header_fingerprint`: the identity of a table layout, so a saved
  mapping is found again for the next file with the same header.
* :func:`suggest_spec`: a deterministic first guess for the mapping editor
  from header names and cell shapes (no model of any kind, ADR-0007).
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
from app.domain.imports.schemas import DATE_FORMATS, DateFormat, MappingSpec, Sign
from app.ingestion.sandbox.protocol import Reconciliation, Statement, Table

FINGERPRINT_VERSION: Final = "perfin-header-v1"
#: How many leading rows are tried as the header when looking for a mapping.
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
    """The row's currency; ``False`` when the column holds something else."""
    if spec.currency is not None:
        return spec.currency
    if spec.currency_column is None:
        return None
    value = (_text(row, spec.currency_column) or "").upper()
    if re.fullmatch(r"[A-Z]{3}", value):
        return value
    return False


def from_table(table: Table, spec: MappingSpec) -> ParsedStatement:
    """Read ``table`` with ``spec`` (module doc)."""
    first = spec.skip_rows + (1 if spec.has_header else 0)
    parsed = ParsedStatement(source_format="csv", truncated=table.truncated)
    balances: list[tuple[int, date, Decimal, Decimal]] = []
    for index in range(first, len(table.rows)):
        row = table.rows[index]
        if not any(cell.strip() for cell in row):
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
# Suggestions for the mapping editor
# ---------------------------------------------------------------------------

_ROLE_WORDS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("date", ("posting date", "posted date", "booking date", "transaction date", "date", "posted")),
    ("debit", ("debit", "withdrawal", "withdrawals", "money out", "paid out", "outflow")),
    ("credit", ("credit", "deposit", "deposits", "money in", "paid in", "inflow")),
    ("amount", ("amount", "value", "sum", "transaction amount")),
    ("indicator", ("dr/cr", "debit/credit", "cr/dr", "type", "direction")),
    ("balance", ("balance", "running balance", "available balance")),
    ("currency", ("currency", "ccy", "curr")),
    ("payee", ("payee", "description", "merchant", "name", "counterparty", "beneficiary")),
    (
        "memo",
        ("memo", "details", "reference", "notes", "narrative", "purpose", "category", "remarks"),
    ),
)


def _role_of(header: str, taken: set[str]) -> str | None:
    for role, words in _ROLE_WORDS:
        if role in taken:
            continue
        if header in words:
            return role
    for role, words in _ROLE_WORDS:
        if role in taken:
            continue
        if any(word in header for word in words if len(word) > 3):
            return role
    return None


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


def suggest_spec(table: Table) -> MappingSpec | None:
    """A first guess for the mapping editor, or None when nothing fits."""
    if len(table.rows) < 2:
        return None
    header = [_header_cell(c) for c in table.rows[0]]
    body = table.rows[1:51]
    roles: dict[str, int] = {}
    for column, name in enumerate(header):
        role = _role_of(name, set(roles))
        if role is not None:
            roles[role] = column
    if "date" not in roles or not ({"amount", "debit", "credit"} & set(roles)):
        return None

    def column_values(column: int) -> list[str]:
        return [row[column] for row in body if column < len(row)]

    date_format = guess_date_format(column_values(roles["date"]))
    if date_format is None:
        return None
    money_columns = [roles[r] for r in ("amount", "debit", "credit", "balance") if r in roles]
    separator = guess_decimal_separator(v for c in money_columns for v in column_values(c))
    if "payee" not in roles and "memo" in roles:
        roles["payee"] = roles.pop("memo")
    fields: dict[str, object] = {
        "date_column": roles["date"],
        "date_format": date_format,
        "decimal_separator": separator,
        "payee_column": roles.get("payee"),
        "memo_column": roles.get("memo"),
        "balance_column": roles.get("balance"),
        "currency_column": roles.get("currency"),
    }
    if "amount" in roles:
        fields["amount_column"] = roles["amount"]
        if "indicator" in roles:
            values = {v.strip() for v in column_values(roles["indicator"]) if v.strip()}
            outflow = sorted(v for v in values if v.casefold() in {"dr", "d", "debit", "out"})
            if outflow:
                fields["indicator_column"] = roles["indicator"]
                fields["outflow_values"] = outflow
    else:
        fields["debit_column"] = roles.get("debit")
        fields["credit_column"] = roles.get("credit")
    try:
        return MappingSpec.model_validate(fields)
    except ValueError:
        return None

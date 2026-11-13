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
  parse (blank amounts, a date in another form) are skipped and reported by
  row number; the unreadable rows after the table's last readable row whose
  date cell is no date are footers (totals, closing lines), listed apart and
  not reported. The running-balance column, when mapped, gives the closing
  balance and a reconciliation check (each printed balance must equal the
  one printed before plus the amounts of the rows since, so a balance
  printed once a day reconciles too). A debit or credit cell keeps its printed sign: a
  negative debit is money in and a negative credit money out (a reversal),
  unless that column prints every amount negative
  (:func:`_printed_negative`). An empty currency cell is the
  account's currency; any
  other cell that is not a three-letter code makes the row unreadable. A row
  with at most one filled cell (the disclaimer paragraphs and "Date
  downloaded" line brokerages append), or without a date and without an
  amount, can never be a transaction: it is a note row, neither imported
  nor reported. A one-cell row under the description of the transaction
  row directly above it continues that description and is appended to the
  row's memo; a row whose date cell says ``Pending`` is a pending row,
  counted and not imported (:func:`_read_rows`).
* :func:`from_table_grouped`: a multi-account table (``account_column``)
  as one statement per account, each carrying only the **last four digits**
  of its account cell (:func:`cell_last4`), so the importer matches it like
  an OFX statement. The text of a number cell is used for grouping in
  memory and never kept. A column of account *names* (``Everyday
  Checking``, ``Rewards Card``: an aggregator's export) groups the same
  way, by the folded name; such a statement carries its ``account_name``
  and no last four, so the importer asks which account each one is.
* :func:`header_fingerprint`: the identity of a table layout, so a saved
  mapping is found again for the next file with the same header.
* :func:`suggest_mapping`: a deterministic guess at the mapping (no model of
  any kind, ADR-0007): the header row searched among the first
  :data:`HEADER_SEARCH_ROWS` rows, every (column, role) pair scored by how
  the header names it (exact, whole word, substring) and whether the
  column's cells fit the role, pairs assigned globally best first; with a
  confidence (``confident``, ``score``, per-role reasons, the rows that read
  and the doubts) the importer uses to import a never-seen layout on its
  own (``app.domain.imports.service``). The sign convention is proven
  (debit/credit columns, an indicator, a reconciling running balance,
  direction words that go with the amounts' signs) or, taken on trust, it
  is a doubt for a table that looks like a card export or whose amounts say
  nothing (``sign_unverified``); a table of securities trades is never a
  confident cash ledger (``investment_columns``). :func:`suggest_spec` is
  its spec alone, for the mapping editor.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from app.core.money import AMOUNT_PLACES, MAX_ABS_AMOUNT, quantize
from app.domain.imports.schemas import (
    DATE_FORMATS,
    OUTFLOW_VALUES_MAX,
    DateFormat,
    MappingSpec,
    Sign,
)
from app.ingestion.parsers.accounts import summary_last4
from app.ingestion.parsers.text import MAX_LINE
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
    #: The account's name in a table grouped by a column of names
    #: (:func:`from_table_grouped`); never an account number.
    account_name: str | None = None
    institution: str | None = None
    account_type: str | None = None
    currency: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    closing_balance: ClosingBalance | None = None
    #: Table rows (0-based) that could not be read with the mapping.
    skipped_rows: list[int] = field(default_factory=list)
    #: Table rows that can never be a transaction (a disclaimer paragraph, a
    #: "Date downloaded" line, a row without a date and an amount): not
    #: reported as skipped.
    note_rows: list[int] = field(default_factory=list)
    #: Unreadable table rows after the table's last readable row whose date
    #: cell is no date (totals, closing lines): not reported as skipped.
    footer_rows: list[int] = field(default_factory=list)
    #: Table rows whose date cell says ``Pending``: not yet a transaction,
    #: neither imported nor reported, only counted.
    pending_rows: list[int] = field(default_factory=list)
    #: Rows whose running balance disagrees with the balance printed before
    #: plus the amounts since.
    balance_mismatches: list[int] = field(default_factory=list)
    #: How many printed balances were compared with the one printed before.
    balance_checks: int = 0
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
#: A time of day after the date, ignored: ``10:22``, ``10:22:05.123``,
#: ``10:22 PM``, with a zone (``Z``, ``+0100``, ``-05:00``) that may follow a blank.
_TIME_SUFFIX: Final = (
    r"(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s?[AaPp][Mm])?"
    r"(?:\s?(?:Z|[+-]\d{2}:?\d{2}))?)?"
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
        "DDMMMYYYY": rf"{_D}{_B}{_Y4}",
        "DD-MMM-YY": rf"{_D}-{_B}-{_Y2}",
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
#: A currency written in letters: one to three letters, each with or without
#: a dot after it (``zł``, ``Kč``, ``лв.``, the dotted abbreviations of the
#: Arabic script).
_LETTERS: Final = r"(?:[^\W\d_]\.?){1,3}"
_LETTERED_BEFORE: Final = re.compile(rf"^(?P<sign>[-+(]?)\s*(?P<letters>{_LETTERS})\s*(?=[-+]?\d)")
_LETTERED_AFTER: Final = re.compile(rf"(?<=\d)\s*(?P<letters>{_LETTERS})(?P<sign>[-)]*)$")
#: Signs of a hundredth or a thousandth of a currency (cent, mill, pfennig):
#: ``50¢`` is not 50 of anything, so the cell stays unreadable.
_SUBUNIT_SIGNS: Final = frozenset("\u00a2\uffe0\u20a5\u20b0")


def _without_lettered_currency(value: str) -> str:
    """``value`` without the currency signs, direction marks and currency letters around a number.

    Only called for a cell with a character outside ASCII. Every currency
    sign (Unicode ``Sc``, but :data:`_SUBUNIT_SIGNS`) and format character
    (``Cf``: the left-to-right and right-to-left marks) goes; so does a
    currency written in letters before or after the number
    (:data:`_LETTERS`) when one of its letters is not ASCII. Plain ASCII
    letters are left to :data:`_STRIP`, which knows three-letter codes
    only: a word before a number is not a currency.
    """
    kept = "".join(
        c for c in value if c in _SUBUNIT_SIGNS or unicodedata.category(c) not in ("Sc", "Cf")
    )
    for pattern in (_LETTERED_BEFORE, _LETTERED_AFTER):
        match = pattern.search(kept)
        if match is not None and not match["letters"].isascii():
            kept = kept[: match.start()] + match["sign"] + kept[match.end() :]
    return kept


def parse_cell_amount(text: str, decimal_separator: str) -> Decimal | None:
    """A signed amount from a table cell, or None when it is not one.

    Accepts ``1,234.56`` / ``1.234,56`` (by ``decimal_separator``), a leading
    ``+``/``-``, ``(12.34)`` and ``12.34-`` as negatives, currency symbols,
    a leading or trailing ISO code, a currency written in letters of another
    script with its direction marks (:func:`_without_lettered_currency`),
    spaces and apostrophes. More than
    :data:`~app.core.money.AMOUNT_PLACES` decimals are rounded half-even to
    that many, as the OFX reader does: one cell an export computed (an
    average, a conversion) does not make its row unreadable.
    """
    value = text.strip()
    if value.startswith("'"):  # the import-side formula guard (C7)
        value = value[1:]
    if not value.isascii():
        value = _without_lettered_currency(value)
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
    if amount > MAX_ABS_AMOUNT:
        return None
    if negative:
        amount = -amount
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > AMOUNT_PLACES:
        return quantize(amount)
    return amount


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


def _row_amount(
    row: Sequence[str], spec: MappingSpec, printed_negative: frozenset[str] = frozenset()
) -> Decimal | None:
    """The row's signed amount (None: it does not read).

    Debit and credit cells keep their printed sign (``credit - debit``): a
    negative debit is a credit and a negative credit a debit, which is also
    how a lone ``Inflow`` column of negative cells is money out. The
    columns named in ``printed_negative`` (:func:`_printed_negative`) print
    every amount negative and are read by magnitude.
    """
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
    money_out = debit or Decimal(0)
    money_in = credit or Decimal(0)
    if "debit" in printed_negative:
        money_out = abs(money_out)
    if "credit" in printed_negative:
        money_in = abs(money_in)
    return money_in - money_out


def _printed_negative(table: Table, spec: MappingSpec) -> frozenset[str]:
    """Which of a debit and a credit column print every amount as a negative number.

    Some exports write a withdrawal as ``-25.00`` under ``Amount Debit``:
    the minus is the column's notation, not a reversal. A column counts
    when its cells that read hold at least one negative amount and no
    positive one; its cells are then read by magnitude. That is never said
    of a lone credit column: negative cells under a lone ``Inflow`` header
    are money out, the minus sign being all that says which way.
    """
    columns: list[tuple[str, int]] = []
    if spec.debit_column is not None:
        columns.append(("debit", spec.debit_column))
        if spec.credit_column is not None:
            columns.append(("credit", spec.credit_column))
    found: set[str] = set()
    for name, column in columns:
        negative = positive = False
        for index in _body_rows(table, spec):
            raw = _text(table.rows[index], column)
            amount = parse_cell_amount(raw, spec.decimal_separator) if raw else None
            if amount is None:
                continue
            negative = negative or amount < 0
            positive = positive or amount > 0
            if positive:
                break
        if negative and not positive:
            found.add(name)
    return frozenset(found)


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


#: An account name is kept up to this many characters.
ACCOUNT_NAME_MAX: Final = 80


def _account_key(row: Sequence[str], column: int) -> tuple[str | None, str]:
    """How rows are grouped by account: (the last four digits, "") else (None, the folded text).

    The folded text is the cell case-folded with its blanks collapsed, so
    ``Everyday Checking`` and ``everyday  checking`` are one account.
    """
    value = _cell(row, column) or ""
    found = cell_last4(value)
    return (found, "") if found is not None else (None, _header_cell(value))


def _account_name(text: str | None) -> str | None:
    """An account cell as the account's name (None: a number, a masked number, nothing).

    A name has fewer than four digits (else the cell is a number and only
    its last four are kept), is not shaped like a masked number
    (``Z2xxxxxxxx``) and has letters. Blanks are collapsed and a leading
    formula guard dropped.
    """
    value = _SPACE.sub(" ", (text or "").strip().removeprefix("'")).strip()
    if not value or cell_last4(value) is not None or _looks_like_account(value):
        return None
    if sum(1 for c in value if c.isalpha() and c not in "xX") < 2:
        return None
    return value[:ACCOUNT_NAME_MAX]


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


#: What a date cell says on a row the bank has not booked yet.
PENDING_CELL: Final = "pending"


def _blank(row: Sequence[str], *columns: int | None) -> bool:
    """Whether none of the mapped ``columns`` of ``row`` holds anything."""
    return not any((_cell(row, column) or "").strip() for column in columns if column is not None)


def _continuation(row: Sequence[str], spec: MappingSpec) -> str | None:
    """The text of a row whose one filled cell stands under the payee or the memo header."""
    for column in (spec.payee_column, spec.memo_column):
        text = _text(row, column)
        if text is not None:
            return text
    return None


def _read_rows(
    table: Table,
    spec: MappingSpec,
    indexes: Iterable[int],
    printed_negative: frozenset[str] = frozenset(),
) -> ParsedStatement:
    """``indexes`` rows of ``table`` read with ``spec``, as one statement.

    Rows that are not transactions are told apart before anything is
    called unreadable:

    * a row with one filled cell is a note row, unless it continues the
      description of the transaction row directly above it (below);
    * a row whose date cell and amount cells are all empty is a note row
      wherever it stands (a holder's name beside an opening balance, a
      section title over two cells);
    * a row whose date cell says ``Pending`` is a pending row.

    A **continuation row** is a row with one filled cell, under the payee
    or the memo header, directly below a transaction row or below another
    continuation of it (exports that print the merchant on a second
    line). Its text is appended to that row's memo. After the last
    transaction row it only counts when an earlier row of the table was
    continued too: otherwise a single closing line is the note it always
    was.
    """
    parsed = ParsedStatement(source_format="csv", truncated=table.truncated)
    balances: list[tuple[int, date, Decimal, Decimal | None]] = []
    #: (position of the transaction, row, text) of each candidate continuation.
    continued: list[tuple[int, int, str]] = []
    #: The last transaction row, or the last continuation row below it.
    line = -1
    for index in indexes:
        row = table.rows[index]
        filled = _filled(row)
        if not filled:
            continue
        if filled == 1:
            text = _continuation(row, spec) if parsed.transactions and index == line + 1 else None
            if text is None:
                parsed.note_rows.append(index)
            else:
                continued.append((len(parsed.transactions) - 1, index, text))
                line = index
            continue
        if _blank(row, spec.date_column, spec.amount_column, spec.debit_column, spec.credit_column):
            parsed.note_rows.append(index)
            continue
        raw_date = _cell(row, spec.date_column)
        if raw_date is not None and _header_cell(raw_date) == PENDING_CELL:
            parsed.pending_rows.append(index)
            continue
        posted = parse_cell_date(raw_date, spec.date_format) if raw_date else None
        amount = _row_amount(row, spec, printed_negative)
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
        line = index
        if spec.balance_column is not None:
            raw_balance = _text(row, spec.balance_column)
            balance = (
                parse_cell_amount(raw_balance, spec.decimal_separator) if raw_balance else None
            )
            balances.append((index, posted, amount, balance))
    _append_continuations(parsed, continued)
    if parsed.transactions:
        dates = [t.posted_date for t in parsed.transactions]
        parsed.period_start, parsed.period_end = min(dates), max(dates)
        currencies = {t.currency for t in parsed.transactions if t.currency}
        if len(currencies) == 1:
            parsed.currency = next(iter(currencies))
    chronological = _chronological(balances)
    printed = [(posted, balance) for _, posted, _, balance in chronological if balance is not None]
    if printed:
        last_date, last_balance = printed[-1]
        parsed.closing_balance = ClosingBalance(last_balance, last_date)
        parsed.balance_mismatches = _reconcile(chronological)
        parsed.balance_checks = len(printed) - 1
    return parsed


def _append_continuations(parsed: ParsedStatement, continued: list[tuple[int, int, str]]) -> None:
    """Append the continuation rows to their transactions' memos (:func:`_read_rows`).

    Those below the last transaction are note rows unless a row before it
    was continued.
    """
    last = len(parsed.transactions) - 1
    established = any(position < last for position, _, _ in continued)
    for position, index, text in continued:
        if position == last and not established:
            parsed.note_rows.append(index)
            continue
        transaction = parsed.transactions[position]
        memo = f"{transaction.memo} {text}" if transaction.memo else text
        parsed.transactions[position] = replace(transaction, memo=memo)
    parsed.note_rows.sort()


def _split_footers(table: Table, spec: MappingSpec, statements: Sequence[ParsedStatement]) -> None:
    """Move the unreadable rows that trail the table from ``skipped_rows`` to ``footer_rows``.

    A footer is an unreadable row after the last readable row of the whole
    table (not of its own account's rows) whose date cell is no date: a
    total, a closing line. A trailing row that has a date is a transaction
    that did not read and stays reported. A table without any readable row
    has no footer: every row stays reported.
    """
    last = max((t.row for s in statements for t in s.transactions if t.row is not None), default=-1)
    if last < 0:
        return
    for parsed in statements:
        footers = [
            index
            for index in parsed.skipped_rows
            if index > last
            and parse_cell_date(_cell(table.rows[index], spec.date_column) or "", spec.date_format)
            is None
        ]
        if footers:
            parsed.footer_rows = footers
            parsed.skipped_rows = [index for index in parsed.skipped_rows if index not in footers]


def from_table(table: Table, spec: MappingSpec) -> ParsedStatement:
    """Read every body row of ``table`` with ``spec`` as one statement (module doc).

    With an ``account_column`` the rows of all accounts are read together
    (the preview, the suggestion's checks); ``last4`` is set only when every
    readable row names the same account. The importer reads such a table
    with :func:`from_table_grouped`.
    """
    parsed = _read_rows(table, spec, _body_rows(table, spec), _printed_negative(table, spec))
    _split_footers(table, spec, [parsed])
    if spec.account_column is not None and parsed.transactions:
        column = spec.account_column
        found = {
            cell_last4(_cell(table.rows[t.row], column))
            for t in parsed.transactions
            if t.row is not None
        }
        if len(found) == 1:
            parsed.last4 = next(iter(found))
    elif spec.account_column is None and parsed.transactions:
        parsed.last4 = preamble_last4(table, spec)
    return parsed


#: Preamble rows read for the account's number (the rest is not looked at).
PREAMBLE_ROWS: Final = 20


def preamble_last4(table: Table, spec: MappingSpec) -> str | None:
    """The last four digits the lines above the header name, when they name one account.

    An export without an account column may still say whose rows these are
    in the lines before its header (``Account Number : ****3377``,
    ``Account: 4410 0092 6654``). Each preamble row is read as one line
    (:func:`app.ingestion.parsers.accounts.summary_last4`); two different
    numbers are no answer. Nothing but the four digits is kept.
    """
    found = {
        last4
        for row in table.rows[: min(spec.skip_rows, PREAMBLE_ROWS)]
        if (last4 := summary_last4(" ".join(cell for cell in row if cell.strip())[:MAX_LINE]))
    }
    return next(iter(found)) if len(found) == 1 else None


def from_table_grouped(table: Table, spec: MappingSpec) -> list[ParsedStatement]:
    """One statement per account of a multi-account table (``spec.account_column``).

    Rows are grouped by the last four digits of the account cell (cells with
    fewer digits by their folded text, so two unnamed accounts stay apart),
    in the order the accounts first appear among the readable rows; each
    group is read like :func:`from_table` and carries only ``last4`` (the
    text of a number cell is never kept). A group of cells that are a name,
    not a number, carries ``account_name`` (the cell of its first readable
    row, :func:`_account_name`) and no ``last4``. Unreadable rows are
    reported in their account's group; those of no group with a readable row
    (footers, totals) in the first group, like the note rows and the
    pending rows; the ones that trail the whole table are footers
    (:func:`_split_footers`). A continuation row has no account cell, so in
    a grouped table it stays a note row. Without an
    ``account_column``: the one statement of :func:`from_table`. A table
    without any readable row gives one empty statement listing the skipped
    rows.
    """
    if spec.account_column is None:
        return [from_table(table, spec)]
    column = spec.account_column
    printed_negative = _printed_negative(table, spec)
    rows: dict[tuple[str | None, str], list[int]] = {}
    for index in _body_rows(table, spec):
        row = table.rows[index]
        if any(cell.strip() for cell in row):
            rows.setdefault(_account_key(row, column), []).append(index)
    groups: list[tuple[int, ParsedStatement]] = []
    orphans: list[int] = []
    notes: list[int] = []
    pending: list[int] = []
    for key, indexes in rows.items():
        parsed = _read_rows(table, spec, indexes, printed_negative)
        if not parsed.transactions:
            orphans.extend(parsed.skipped_rows)
            notes.extend(parsed.note_rows)
            pending.extend(parsed.pending_rows)
            continue
        parsed.last4 = key[0]
        first = parsed.transactions[0].row
        if parsed.last4 is None and first is not None:
            parsed.account_name = _account_name(_cell(table.rows[first], column))
        groups.append((first if first is not None else 0, parsed))
    groups.sort(key=lambda pair: pair[0])
    statements = [parsed for _, parsed in groups]
    if not statements:
        return [
            ParsedStatement(
                source_format="csv",
                skipped_rows=sorted(orphans),
                note_rows=sorted(notes),
                pending_rows=sorted(pending),
                truncated=table.truncated,
            )
        ]
    if orphans:
        statements[0].skipped_rows = sorted([*statements[0].skipped_rows, *orphans])
    if notes:
        statements[0].note_rows = sorted([*statements[0].note_rows, *notes])
    if pending:
        statements[0].pending_rows = sorted([*statements[0].pending_rows, *pending])
    _split_footers(table, spec, statements)
    return statements


def group_last4(table: Table, spec: MappingSpec, group: int) -> str | None:
    """The last four digits of account group ``group`` of :func:`from_table_grouped`."""
    statements = from_table_grouped(table, spec)
    if spec.account_column is None or not 0 <= group < len(statements):
        return None
    return statements[group].last4


def _chronological(
    rows: list[tuple[int, date, Decimal, Decimal | None]],
) -> list[tuple[int, date, Decimal, Decimal | None]]:
    """Rows in time order: files list newest-first or oldest-first; keep their order."""
    if len(rows) > 1 and rows[0][1] > rows[-1][1]:
        return list(reversed(rows))
    return rows


def _reconcile(rows: Iterable[tuple[int, date, Decimal, Decimal | None]]) -> list[int]:
    """Rows whose balance is not the balance printed before plus the amounts since.

    ``rows`` are every transaction in time order, the balance None where
    the row prints none: an export that prints the balance once a day (on
    the day's last row) reconciles over the day's rows together.
    """
    mismatches: list[int] = []
    previous: Decimal | None = None
    since = Decimal(0)
    for index, _, amount, balance in rows:
        since += amount
        if balance is None:
            continue
        if previous is not None and previous + since != balance:
            mismatches.append(index)
        previous, since = balance, Decimal(0)
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
#: No header word names the role: the column's cells alone say so (a column of
#: ``Debit``/``Credit`` under a header such as ``Credit Debit Indicator``).
CONTENT: Final = 0
MATCH_NAMES: Final[dict[int, str]] = {
    EXACT: "exact",
    WORD: "word",
    SUBSTRING: "substring",
    CONTENT: "content",
}

#: Roles in tie-break order, with the header words that name them.
#: Header words per role. English first; then the words German, French,
#: Spanish, Dutch, Italian and Portuguese exports use, so a bank's own
#: export maps without a person naming its columns. A word alone never
#: assigns a role: the column's values must fit it (:func:`_role_fit`), which
#: also settles words two languages use differently (``Valuta``: a value
#: date in German, the currency in Dutch and Italian).
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
        *("date processed", "created", "datetime", "timestamp"),
        *("datum", "buchungstag", "buchungsdatum", "valuta", "wertstellung", "valutadatum"),
        *("date opération", "date operation", "date de valeur", "date comptable"),
        *("fecha", "fecha valor", "fecha operación", "fecha operacion", "fecha contable"),
        *("boekdatum", "transactiedatum", "rentedatum"),
        *("data", "data operazione", "data valuta", "data contabile", "data movimento"),
        *("data lançamento", "data lancamento", "data valor"),
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
        *("betrag", "umsatz", "montant", "importe", "monto", "bedrag", "importo", "valor"),
        *("quantia", "montante"),
    ),
    "debit": (
        *("debit", "debits", "withdrawal", "withdrawals", "money out", "paid out", "outflow"),
        *("debit amount", "amount debit"),
        *("soll", "lastschrift", "ausgang", "ausgaben", "débit", "debe", "cargo", "cargos"),
        *("retiro", "retiros", "af", "uscite", "addebito", "addebiti", "débito", "debito"),
    ),
    "credit": (
        *("credit", "credits", "deposit", "deposits", "money in", "paid in", "inflow"),
        *("credit amount", "amount credit"),
        *("haben", "gutschrift", "eingang", "einnahmen", "crédit", "haber", "abono", "abonos"),
        *("depósito", "deposito", "bij", "entrate", "accredito", "accrediti", "crédito"),
        "credito",
    ),
    "indicator": (
        "dr/cr",
        "cr/dr",
        "debit/credit",
        "credit/debit",
        "type",
        "direction",
        "transaction type",
        *("soll/haben", "s/h", "af bij", "af/bij", "débit/crédit", "debe/haber", "dare/avere"),
    ),
    "balance": (
        *("balance", "running balance", "available balance", "cash balance"),
        *("saldo", "kontostand", "solde", "saldo contabile"),
    ),
    "currency": (
        *("currency", "ccy", "curr"),
        *("währung", "waehrung", "devise", "moneda", "divisa", "valuta", "munt", "moeda"),
    ),
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
        "account name",
        "source account",
        *("kontonummer", "konto", "iban", "numéro de compte", "numero de compte", "compte"),
        *("número de cuenta", "numero de cuenta", "cuenta", "rekeningnummer", "rekening"),
        *("numero conto", "conto", "número da conta", "numero da conta", "conta"),
    ),
    "payee": (
        *("payee", "description", "merchant", "name", "counterparty", "beneficiary"),
        *("empfänger", "empfaenger", "auftraggeber", "begünstigter", "beguenstigter"),
        *("zahlungsempfänger", "bezeichnung", "bénéficiaire", "beneficiaire", "libellé"),
        *("libelle", "tiers", "beneficiario", "concepto", "descripción", "descripcion"),
        *("naam", "omschrijving", "tegenpartij", "descrizione", "descrição", "descricao"),
    ),
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
        *("verwendungszweck", "buchungstext", "notiz", "kategorie", "référence", "détails"),
        *("catégorie", "categorie", "referencia", "detalle", "observaciones", "categoría"),
        *("categoria", "mededelingen", "mededeling", "causale", "dettagli", "observações"),
    ),
}
_ROLE_ORDER: Final = {role: position for position, role in enumerate(ROLE_WORDS)}
_MONEY_ROLES: Final = frozenset({"amount", "debit", "credit", "balance"})
#: Indicator cells that mean money out / money in (case-insensitive).
OUTFLOW_WORDS: Final = frozenset(
    {
        *("dr", "d", "debit", "out", "withdrawal", "outflow", "db"),
        *("s", "soll", "af", "débit", "debe", "cargo", "dare", "débito", "debito"),
    }
)
INFLOW_WORDS: Final = frozenset(
    {
        *("cr", "c", "credit", "in", "deposit", "inflow"),
        *("h", "haben", "bij", "crédit", "haber", "abono", "avere", "crédito", "credito"),
    }
)
#: Transaction-type cells that go with money out / money in. Beside a
#: *signed* amount they are evidence for the sign convention
#: (:func:`_correlate`); they never sign an unsigned amount, because
#: ``Payment`` is money in on a card and money out of a checking account.
TYPE_OUTFLOW_WORDS: Final = frozenset({"purchase", "sale", "fee", "payment sent"})
TYPE_INFLOW_WORDS: Final = frozenset({"payment", "refund", "return"})
_DIRECTION: Final = OUTFLOW_WORDS | INFLOW_WORDS
_ANY_OUTFLOW: Final = OUTFLOW_WORDS | TYPE_OUTFLOW_WORDS
_ANY_INFLOW: Final = INFLOW_WORDS | TYPE_INFLOW_WORDS
#: Share of a column's cells that must be direction or type words for the
#: column to be read as one when its header does not name it.
DIRECTION_FIT_MIN: Final = 0.9
#: Direction words and signs are compared on at least this many rows, and
#: prove a sign convention when this share of them agree.
CORRELATION_MIN_ROWS: Final = 4
CORRELATION_AGREE_MIN: Final = 0.95


def _bounded(words: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9])(?:{words})(?![a-z0-9])")


#: Header words only a card export uses.
_CARD_HEADER: Final = _bounded(
    r"card no|card number|card used|member name|merchant|statement balance"
)
_POST_DATE_HEADER: Final = _bounded(r"post(?:ed)? date")
_TRANSACTION_DATE_HEADER: Final = _bounded(r"trans(?:action|\.)? date")
#: Header words of a securities trade: what was traded, how many, at what price.
_SECURITY_HEADER: Final = _bounded(r"symbol|ticker|cusip|isin|fund|security")
_QUANTITY_HEADER: Final = _bounded(r"quantity|qty|shares|units")
_PRICE_HEADER: Final = _bounded(r"price|nav")
_ACCOUNT_CELL: Final = re.compile(r"[A-Za-z0-9*#.\- ]{4,34}")
#: The only headers under which a column of names (not numbers) is the
#: account column: any other text column (a payee, a category, an account
#: *type*) must never split a file.
ACCOUNT_NAME_HEADERS: Final = frozenset({"account", "account name", "source account"})
#: A column of names is an account column when it holds this many distinct
#: names, each on at least :data:`ACCOUNT_NAME_REPEATS` rows: one person's
#: accounts are few and each has several rows; payees are many or seen once.
ACCOUNT_NAMES_MIN: Final = 2
ACCOUNT_NAMES_MAX: Final = 20
ACCOUNT_NAME_REPEATS: Final = 2
_SETTLEMENT_DATE: Final = re.compile(
    r"settle|value|effective|due|valut|wertstell|valeur|valor|rente"
)
_POSTING_DATE: Final = re.compile(r"post|book|buchung|boek|compta|contab")
_TRANSACTION_DATE: Final = re.compile(r"trans|trade|run|op[eé]ra|moviment|lan[cç]amento")


#: A qualifier that names a part of a figure: ``Amount (fee)`` is not the
#: amount, whichever of ``Amount (fee)`` and ``Amount (total)`` comes first.
_PART: Final = r"(?!(?:fees?|tax|vat|tip)\))"
#: A unit after a money header (``Bedrag (EUR)``, ``Amount (USD)``, ``Betrag in EUR``):
#: the header still names its role exactly.
_UNIT_SUFFIX: Final = re.compile(
    rf"\s*(?:\({_PART}(?:[a-z]{{3}}|[$\u20ac\u00a3\u00a5])\)|\bin [a-z]{{3}})$"
)
#: Any other parenthesised qualifier (``Amount (total)``, ``Date (UTC)``) is
#: read the same way, when that leaves a name no other column has
#: (:func:`_role_names`).
_QUALIFIER: Final = re.compile(rf"\s*\({_PART}[^()]*\)$")
#: Beside a ``Fee`` and a ``Net`` column, ``Gross`` is the amount (a payment
#: service's export); on its own it is no amount word.
_GROSS_HEADERS: Final = ("gross", "fee", "net")


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
    a reconciling running ``balance``, ``indicator_correlation``: direction
    or transaction-type words that go with the signs of the amounts) or is
    None when signed amounts are read as "positive is money in" on trust.
    A sign on trust is the doubt ``sign_unverified`` when the header names
    a card export (:func:`names_card`), when every amount has the same
    sign, or when more rows are money in than money out (a card export
    lists purchases as positive); under a header that names a card a
    running balance is no proof either (a card's balance is what is owed,
    which adds up with purchases read as money in).
    ``indicator_contradicts_sign``: direction
    words and signs disagree row to row, or agree with each other against
    the running balance. ``investment_columns``: the header names a
    security, a quantity and a price (:func:`names_trades`), so the rows are
    trades, not a cash ledger. Reasons carry roles, column indexes, match
    kinds and shares only, never cell text.
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


def _names_accounts(cells: Sequence[str]) -> bool:
    """Whether the non-empty ``cells`` are a few account names, each on several rows.

    Between :data:`ACCOUNT_NAMES_MIN` and :data:`ACCOUNT_NAMES_MAX` distinct
    values (case-folded, blanks collapsed, as :func:`from_table_grouped`
    groups them), every one on at least :data:`ACCOUNT_NAME_REPEATS` rows.
    """
    counts = Counter(_header_cell(cell) for cell in cells)
    return ACCOUNT_NAMES_MIN <= len(counts) <= ACCOUNT_NAMES_MAX and all(
        count >= ACCOUNT_NAME_REPEATS for count in counts.values()
    )


def _role_fit(role: str, values: list[str], header: str = "") -> float:
    """Share of the non-empty ``values`` that fit ``role`` (0: the role is out).

    ``header`` is the column's folded header: the account role is also
    fitted by a column of names, but only under a header that is exactly an
    account's (:data:`ACCOUNT_NAME_HEADERS`, :func:`_names_accounts`).
    :func:`_assign` leaves it out when the table has a column of account
    numbers.
    """
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
        numbers = sum(1 for c in cells if _looks_like_account(c)) / len(cells)
        if numbers < ROLE_FIT_MIN and header in ACCOUNT_NAME_HEADERS and _names_accounts(cells):
            return 1.0
        return numbers
    # payee / memo: text, not a column of numbers or dates.
    return sum(1 for c in cells if _shape(c) == "t") / len(cells)


@dataclass(frozen=True)
class _Pick:
    column: int
    level: int
    fit: float


def _gross_level(names: Sequence[str], body: Sequence[Sequence[str]]) -> int:
    """How a ``Gross`` header names the amount (0: it does not).

    Only beside a ``Fee`` and a ``Net`` column (:data:`_GROSS_HEADERS`).
    Exactly, when gross and net are the same figure on every row: no fee
    was taken, so there is one amount. Else as a word only: the fee makes
    them differ, which of the two the account saw is for a person to say,
    and the suggestion is not confident (``header_not_exact``).
    """
    if any(names.count(name) != 1 for name in _GROSS_HEADERS):
        return 0
    gross, net = names.index("gross"), names.index("net")
    same = all(_text(row, gross) == _text(row, net) for row in body)
    return EXACT if same else WORD


def _role_names(header: Sequence[str]) -> list[str]:
    """The header cells as the role words are matched against them.

    Folded (:func:`_header_cell`), without a unit (:data:`_UNIT_SUFFIX`) and
    without another parenthesised qualifier (:data:`_QUALIFIER`). When two
    columns differ by their qualifiers alone (``Amount (original)``,
    ``Amount (converted)``) the qualifier is what tells them apart: both
    keep theirs, so neither names the role exactly and a person chooses.
    """
    names = [_UNIT_SUFFIX.sub("", _header_cell(cell)) for cell in header]
    bare = [_QUALIFIER.sub("", name) for name in names]
    return [
        short if bare.count(short) == 1 else name for name, short in zip(names, bare, strict=True)
    ]


def _assign(header: Sequence[str], body: Sequence[Sequence[str]]) -> dict[str, _Pick]:
    """Roles for the columns of ``header``, best (column, role) pair first."""
    names = _role_names(header)

    def column_values(column: int) -> list[str]:
        return [row[column] for row in body if column < len(row)]

    # A column of account numbers is the account column whenever there is
    # one: a column of names beside it is the accounts' nicknames, and only
    # digits match an account without a person.
    numbered = any(
        name
        and _match_level(name, ROLE_WORDS["account"])
        and _role_fit("account", column_values(column)) >= ROLE_FIT_MIN
        for column, name in enumerate(names)
    )
    gross = _gross_level(names, body)
    pairs: list[tuple[float, int, int, str, float]] = []
    memo_candidates: list[tuple[float, int, int, float]] = []
    for column, name in enumerate(names):
        if not name:
            continue
        values = column_values(column)
        for role, words in ROLE_WORDS.items():
            level = gross if (role, name) == ("amount", "gross") else _match_level(name, words)
            if not level:
                continue
            fit = _role_fit(role, values, "" if numbered else name)
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


def _data_rows(rows: Sequence[Sequence[str]], width: int = 0) -> list[Sequence[str]]:
    """Rows with at least two filled cells (a disclaimer paragraph is one cell).

    The rows at the end that have fewer cells than the header (``width``:
    the header up to its last named cell) are a footer block and left out,
    so a short table's totals do not decide what its columns hold. A
    footer block is no longer than the table above it: when the short rows
    outnumber the rows before them, the file just writes its rows without
    their empty trailing cells, and all are kept.
    """
    data = [row for row in rows if _filled(row) == 2]
    end = len(data)
    while end and len(data[end - 1]) < width:
        end -= 1
    return data[:end] if end >= len(data) - end else data


def names_card(header: Sequence[str]) -> bool:
    """Whether a header row has words only a card export uses.

    ``Card No.``, ``Card Number``, ``Card Used``, ``Member Name``,
    ``Merchant``, ``Statement Balance``, or a ``Post Date`` beside a
    ``Transaction Date``. Such a file may list purchases as positive
    amounts, so its sign is never taken on trust.
    """
    names = [_header_cell(cell) for cell in header]
    if any(_CARD_HEADER.search(name) for name in names):
        return True
    return any(_POST_DATE_HEADER.search(name) for name in names) and any(
        _TRANSACTION_DATE_HEADER.search(name) for name in names
    )


def names_trades(header: Sequence[str]) -> bool:
    """Whether a header row names a security, a quantity and a price (a list of trades)."""
    names = [_header_cell(cell) for cell in header]
    return all(
        any(pattern.search(name) for name in names)
        for pattern in (_SECURITY_HEADER, _QUANTITY_HEADER, _PRICE_HEADER)
    )


def _direction_column(body: Sequence[Sequence[str]], taken: set[int]) -> _Pick | None:
    """The column whose cells are direction or transaction-type words, found by content.

    At least :data:`DIRECTION_FIT_MIN` of its non-empty cells are such words
    (``Debit``/``Credit``, ``Purchase``/``Payment``; one-letter marks only
    count under a header that names an indicator) and both directions
    occur. ``taken`` columns already have a role that rules them out. The
    best fitting column wins, the leftmost on a tie.
    """
    best: _Pick | None = None
    for column in range(max((len(row) for row in body), default=0)):
        if column in taken:
            continue
        cells = [
            _header_cell(row[column]) for row in body if column < len(row) and row[column].strip()
        ]
        words = [c for c in cells if len(c) > 1 and c in _ANY_OUTFLOW | _ANY_INFLOW]
        if not any(w in _ANY_OUTFLOW for w in words) or not any(w in _ANY_INFLOW for w in words):
            continue
        fit = len(words) / len(cells)
        if fit >= DIRECTION_FIT_MIN and (best is None or fit > best.fit):
            best = _Pick(column, CONTENT, round(fit, 3))
    return best


@dataclass(frozen=True)
class _Candidate:
    spec: MappingSpec
    picks: dict[str, _Pick]
    date_order_guessed: bool
    rank: float
    #: A column of direction or type words beside *signed* amounts: it is not
    #: in the spec (the amounts sign themselves) but is compared with them.
    direction: int | None = None


def _candidate(table: Table, skip: int) -> _Candidate | None:
    """The mapping read with row ``skip`` as the header, or None when it does not fit."""
    header = table.rows[skip]
    if sum(1 for c in header if c.strip()) < 2:
        return None
    width = max(index for index, cell in enumerate(header) if cell.strip()) + 1
    body = _data_rows(table.rows[skip + 1 : skip + 1 + SUGGEST_SAMPLE_ROWS], width)
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
    }
    direction: int | None = None
    if "amount" in picks:
        fields["amount_column"] = picks["amount"].column
        picks.pop("debit", None)
        picks.pop("credit", None)
        # A column of direction words: named by its header, else found by
        # its cells (never a column that is the date, money or an account).
        named = picks.pop("indicator", None)
        text_roles = {p.column for role, p in picks.items() if role in ("payee", "memo")}
        taken = {p.column for p in picks.values()} - text_roles
        found = named or _direction_column(body, taken)
        if found is not None and _signed(column_values(picks["amount"].column), separator):
            # The amounts carry their own sign: the words beside them are
            # not the sign, they are compared with it (:func:`_correlate`).
            direction = found.column
        elif found is not None:
            values = {v.strip() for v in column_values(found.column) if v.strip()}
            outflow = sorted(v for v in values if v.casefold() in OUTFLOW_WORDS)
            if outflow:
                # Unsigned amounts: the direction word signs each row.
                for role in ("payee", "memo"):
                    if role in picks and picks[role].column == found.column:
                        del picks[role]
                picks["indicator"] = found
                fields["indicator_column"] = found.column
                fields["outflow_values"] = outflow[:OUTFLOW_VALUES_MAX]
    else:
        picks.pop("indicator", None)
        fields["debit_column"] = picks["debit"].column if "debit" in picks else None
        fields["credit_column"] = picks["credit"].column if "credit" in picks else None
    for role in ("payee", "memo", "balance", "currency", "account"):
        fields[f"{role}_column"] = picks[role].column if role in picks else None
    try:
        spec = MappingSpec.model_validate(fields)
    except ValueError:
        return None
    rank = sum(p.level + p.fit for p in picks.values())
    return _Candidate(spec, picks, tied, rank, direction)


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
        if word not in _DIRECTION:
            return True
        raw = _cell(row, spec.amount_column)
        amount = parse_cell_amount(raw, spec.decimal_separator) if raw is not None else None
        if amount is not None and amount < 0:
            return True
    return False


@dataclass(frozen=True)
class _Correlation:
    """What the direction words beside signed amounts say about the sign convention."""

    #: The convention the words prove (None: they prove nothing).
    sign: Sign | None = None
    #: Words and signs disagree row to row.
    contradicts: bool = False
    #: Plain direction words voted (``debit``/``credit``), not transaction types.
    strong: bool = False


def _correlate(table: Table, spec: MappingSpec, column: int, rows: Iterable[int]) -> _Correlation:
    """Compare the direction words of ``column`` with the signs of the amounts on ``rows``.

    A row votes for "positive is money out" when an outflow word stands
    beside a positive amount or an inflow word beside a negative one, else
    for "positive is money in"; rows with another word, or a zero amount,
    do not vote. When plain direction words of both directions occur
    (``debit``/``credit``, ``withdrawal``/``deposit``) only they vote;
    otherwise the transaction-type words do too (``purchase``, ``payment``,
    ``fee``, ``refund``). With at least :data:`CORRELATION_MIN_ROWS` votes
    and both directions present, :data:`CORRELATION_AGREE_MIN` of them
    agreeing proves that convention and anything less is a contradiction;
    fewer rows or one direction alone prove nothing.
    """
    if spec.amount_column is None:
        return _Correlation()
    pairs: list[tuple[str, bool]] = []
    for index in rows:
        row = table.rows[index]
        raw = _cell(row, spec.amount_column)
        amount = parse_cell_amount(raw, spec.decimal_separator) if raw is not None else None
        if amount:
            pairs.append((_header_cell(_cell(row, column) or ""), amount > 0))
    plain = [(word in OUTFLOW_WORDS, positive) for word, positive in pairs if word in _DIRECTION]
    strong = any(out for out, _ in plain) and any(not out for out, _ in plain)
    votes = (
        plain
        if strong
        else [
            (word in _ANY_OUTFLOW, positive)
            for word, positive in pairs
            if word in _ANY_OUTFLOW | _ANY_INFLOW
        ]
    )
    both = any(out for out, _ in votes) and any(not out for out, _ in votes)
    if len(votes) < CORRELATION_MIN_ROWS or not both:
        return _Correlation(strong=strong)
    outflow_positive = sum(1 for out, positive in votes if out == positive) / len(votes)
    if outflow_positive >= CORRELATION_AGREE_MIN:
        return _Correlation(Sign.OUTFLOW_POSITIVE, strong=strong)
    if 1 - outflow_positive >= CORRELATION_AGREE_MIN:
        return _Correlation(Sign.INFLOW_POSITIVE, strong=strong)
    return _Correlation(contradicts=True, strong=strong)


def _sign_says_nothing(amounts: Sequence[Decimal]) -> bool:
    """Whether amounts read as "positive is money in" look as likely the other way round.

    Every amount of one sign says nothing about which way the money went,
    and more rows of money in than of money out is the shape of a card
    export that lists its purchases as positive amounts.
    """
    positive = sum(1 for amount in amounts if amount > 0)
    negative = sum(1 for amount in amounts if amount < 0)
    return positive == 0 or positive > negative


def _balances_agree(statements: list[ParsedStatement]) -> bool:
    """Whether a printed balance was checked against the one before, and none disagreed.

    A balance column that is printed on one row only (an opening balance)
    checks nothing, so it proves nothing.
    """
    checked = sum(s.balance_checks for s in statements)
    return checked >= 1 and not any(s.balance_mismatches for s in statements)


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
    ``DR``/``CR``, an account column several account-number-like values
    or, under a header that is exactly ``Account``, ``Account Name`` or
    ``Source Account``, a few names that each repeat: :func:`_role_fit`);
    an empty column never takes one. A column of direction words is also
    found by its cells alone (:func:`_direction_column`). Beside unsigned
    amounts it is the indicator that signs each row; beside amounts that
    already carry a minus sign it is left out of the spec (a "Type" column
    beside a signed amount describes the row, it does not sign it) and
    compared with the signs instead (:func:`_correlate`): words that go
    with the signs prove the convention (``indicator_correlation``, the
    spec's ``sign`` set to what they prove), words that go against them, or
    against a running balance that reconciles, are a doubt
    (``indicator_contradicts_sign``; transaction-type words that merely
    disagree among themselves do not overrule a reconciling balance). The
    best candidate is then read in full with :func:`from_table_grouped` to
    count the rows that read (and, with a running balance, which sign
    reconciles); the trailing rows after the last readable one are footers.
    The doubts about the sign and the kind of table are described on
    :class:`MappingSuggestion`. No model of any kind (ADR-0007).
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
    header = table.rows[spec.skip_rows]
    rows = [t.row for s in statements for t in s.transactions if t.row is not None]
    skipped = sorted(r for s in statements for r in s.skipped_rows)
    trailing = [r for s in statements for r in (*s.note_rows, *s.footer_rows)]
    last = max(rows, default=-1)
    failed = tuple(r for r in skipped if r < last)
    footer = sum(1 for r in (*skipped, *trailing) if r > last)
    accounts = sum(1 for s in statements if s.transactions) or 1
    amounts = [t.amount for s in statements for t in s.transactions]
    correlation = _Correlation()
    if best.direction is not None and spec.indicator_column is None:
        correlation = _correlate(table, spec, best.direction, rows)
    against_balance = (
        balance_agrees and correlation.sign is not None and correlation.sign is not spec.sign
    )
    if spec.debit_column is not None and spec.credit_column is not None:
        evidence: str | None = "debit_credit"
    elif spec.debit_column is not None:
        # A lone column whose cells carry signs is a signed amount under
        # another name: its header proves nothing.
        evidence = None if any(a > 0 for a in amounts) else "debit_credit"
    elif spec.credit_column is not None:
        evidence = None if any(a < 0 for a in amounts) else "debit_credit"
    elif spec.indicator_column is not None:
        evidence = "indicator"
    elif balance_agrees:
        evidence = "balance"
    elif correlation.sign is not None:
        evidence = "indicator_correlation"
        spec = spec.model_copy(update={"sign": correlation.sign})
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
    if against_balance or (correlation.contradicts and (correlation.strong or not balance_agrees)):
        doubts.append("indicator_contradicts_sign")
    card = names_card(header)
    if rows and (
        (evidence is None and (card or _sign_says_nothing(amounts)))
        # A card's running balance is what is owed: it adds up with
        # purchases read as money in, so it proves nothing about a card.
        or (evidence == "balance" and card)
    ):
        doubts.append("sign_unverified")
    if names_trades(header):
        doubts.append("investment_columns")
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

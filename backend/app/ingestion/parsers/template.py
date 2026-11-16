# SPDX-License-Identifier: AGPL-3.0-or-later
"""Learned layouts: field anchors and labelled tables (INGESTION.md "Teaching PerFin a layout").

A person shows PerFin where the facts of a statement are ("Teach PerFin":
``POST /documents/{id}/teach``, ``app.domain.review.teach``). What can be
remembered for the next statement of the same layout is a
:class:`LayoutTemplate`, stored on the household's ``parser_fingerprint``
next to the rules that recognise the layout:

* **Field anchors** (:class:`FieldAnchor`): the *label* printed next to a
  value (``Holder reference``), where the value sits relative to it
  (``right`` on the same line, ``below`` it, or ``band``: an unlabelled date
  range in the top lines of the pages) and which field it is. Never the
  value: a label is refused when it holds any digit
  (:func:`label_text`), the same rule as the remembered header lines
  (``app.domain.review.layout.derive_match``), so no account number, date
  or amount of the document is ever stored.
* **Tables** (:class:`TableTemplate`): the header text of each column
  (again without digits) and its role (date, payee, amount, debit, credit,
  balance; or symbol, security, quantity, price, market value, cost basis
  for a holdings table), with the text of the line that ends the table.
  Up to :data:`MAX_TRANSACTION_TABLES` transaction tables and one holdings
  table. A transaction table also says what its single ``amount`` column
  means (``direction``, below) and whether it runs on over pages that do
  not repeat its header (``continues``).
* **Ignored lines** and **notes**: label text the person said to ignore
  (never read as a row or a value), and free-text notes ("Other — describe
  it") kept for the household to read, without any word that holds a digit
  (:func:`note_text`). Notes are data, never interpreted.

The same layout taught again adds to what was remembered
(:func:`merge_templates`: what was taught again wins, the rest is kept).

Reading (:func:`read_template`) is deterministic and bounded by the view
(``DocumentView``, at most ``MAX_LINES`` lines): an anchor's label is found
as consecutive words of a line, its value read to the right of it (the rest
of that cell, else the next cell) or on the next line under it, as a date,
an amount, an account number (only its last four digits are kept) or text.
A table starts at every line whose words hold the column headers in order
(a header repeated on later pages continues it); words go to the column
whose span (between the midpoints of neighbouring headers) holds their
centre; a row needs a date and an amount (a holdings row a quantity and a
symbol or name); a line of description text only continues the row above;
a dated line without an amount is skipped and its box recorded for the
review highlight.

**Several tables** are read in one pass (:func:`read_tables`): one table is
open at a time, and it is closed by its ``end`` text (a line that *starts*
with it: a row that merely mentions the words is a row), by the end of its
page, or by the header of another table. A header line that several
templates fit (``Money received`` and ``Money sent`` printed under the same
headers) belongs to the one with the most headers on it; between equals,
the first header of a new page is the table still open from the page
before, otherwise the next table of the template that has not started yet
(tables are taught in the order they are printed;
``app.domain.review.teach`` puts them in that order whatever order they
were drawn in). The rows of all transaction tables make **one** statement,
in page order; ``TemplateOutcome.row_tables`` says which table each row
came from. Printed running balances are checked row by row only when one
table gave the rows.

**A guessed table.** Between tables that share their headers and read an
amount differently (another direction, other column roles) nothing on the
page says which one a header starts; the order is all there is. Two things
make that order a guess (``TableRead.guessed``): a header on a new page
given to the table still open from the page before when that table has no
``end`` text (it may as well have ended at the foot of the page, and the
header be the next table's); and such tables started an unequal number of
times (a section the statement left out this month: which one is not
printed). The rows of a guessed table are read, but unless the statement
reconciles their ``amount`` is scored :data:`~app.ingestion.parsers.
confidence.GUESSED` and the outcome warns ``table_order_guessed``: a sign
that rests on a guess waits for a person, it is never a clean read.

**Continued tables.** A table with ``continues`` stays open over a page
break: on the next page its rows are read with the column spans of the last
header found, from the first line whose date column holds a date (a
holdings table: the first line that is a row), until its ``end`` text or
the header of another table. Teach an ``end`` text with it: without one it
runs to the end of the document, and a dated line further down is a row or
is counted as skipped.

**Direction** applies to an ``amount`` column only (``debit`` and
``credit`` columns say it themselves). ``as_printed``: the figure with its
printed sign. ``money_in``: the same (an unsigned figure is money in, a
printed minus or bracket takes it back). ``money_out``: an unsigned figure
is spending; a printed minus or bracket flips it back, so a refund in a
withdrawals table is money in. ``card``: the printed sign negated (charges
printed positive are spending). A printed ``CR`` or ``DR`` decides in every
direction but ``as_printed``. When a transaction table is a ``card`` table
the statement's balances are amounts owed and are negated too
(:func:`owed_balances`), the ledger convention the card parser follows:
only so can the printed figures reconcile with the negated rows. A card
that prints its payments in a table of their own is taught with that table
as ``money_in`` and its purchases as ``card``.

**Dates** are read as numbers in the order the rows settle, ISO, or with a
month name, with or without a year. A date without its year takes it from
the statement period the anchors (or the person) gave; without a period
such rows are counted as skipped, no statement is read from them, and the
outcome warns ``dates_without_year``.

The rows are reconciled like any parser's
(:mod:`app.ingestion.reconcile`, ``reconcile_investment``) and scored with
the generic confidences (:mod:`app.ingestion.parsers.confidence`), so a
template that misreads a statement is held back exactly as a parser's
reading would be (SECURITY.md C12).

``app.ingestion.parsers.registry.parse_extraction`` runs it after the
parsers when the classifier matched a household fingerprint that carries a
template: a labelled table replaces what the parsers read, the anchors fill
the period, the account's last four digits and the balances.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.core.money import sum_amounts
from app.ingestion.classify.dates import find_dates as classify_dates
from app.ingestion.classify.dates import find_ranges, header_band_range
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.base import DocumentView, ViewLine
from app.ingestion.parsers.brokerage import read_number, reconcile_investment
from app.ingestion.parsers.table_statement import page_heads
from app.ingestion.parsers.text import (
    DateStyle,
    guess_numeric_style,
    month_day,
    numeric_parts,
    read_amount,
    read_date,
)
from app.ingestion.reconcile import Row, reconcile
from app.ingestion.sandbox.protocol import (
    Balance,
    ExtractedTransaction,
    InvestmentPosition,
    InvestmentStatement,
    ParserRef,
    SecurityRef,
    Statement,
    StatementAccount,
    has_word,
)

SCHEMA_VERSION: Final = 1
PARSER_NAME: Final = "layout_template"
PARSER_VERSION: Final = "6"
TEMPLATE_REF: Final = ParserRef(name=PARSER_NAME, version=PARSER_VERSION, kind="statement")

MAX_LABEL: Final = 120
MAX_HEADER: Final = 64
MAX_NOTE: Final = 500
MAX_FIELDS: Final = 12
#: Transaction tables one layout may hold (deposits, withdrawals, cheques, fees).
MAX_TRANSACTION_TABLES: Final = 4
#: The transaction tables and one holdings table.
MAX_TABLES: Final = MAX_TRANSACTION_TABLES + 1
MAX_COLUMNS: Final = 20
MAX_IGNORED: Final = 20
MAX_NOTES: Final = 20
#: Words of a label taken from the left of a value (a label is a short phrase).
MAX_LABEL_WORDS: Final = 8
#: Boxes of skipped lines kept for the review highlight.
MAX_SKIPPED_REGIONS: Final = 50
#: Rows one labelled table yields at most (bounded like a parser's).
MAX_ROWS: Final = 100_000

AnchorField = Literal[
    "account_number",
    "account_name",
    "period_start",
    "period_end",
    "opening_balance",
    "closing_balance",
]
ANCHOR_FIELDS: Final[tuple[AnchorField, ...]] = (
    "account_number",
    "account_name",
    "period_start",
    "period_end",
    "opening_balance",
    "closing_balance",
)
Direction = Literal["right", "below", "band"]
TableKind = Literal["transactions", "holdings"]
#: What a single ``amount`` column of a transaction table means (module doc).
AmountDirection = Literal["as_printed", "money_in", "money_out", "card"]
TransactionRole = Literal["date", "payee", "memo", "amount", "debit", "credit", "balance"]
HoldingRole = Literal["symbol", "security", "quantity", "price", "market_value", "cost_basis"]
ColumnRole = Literal[
    "ignore",
    "date",
    "payee",
    "memo",
    "amount",
    "debit",
    "credit",
    "balance",
    "symbol",
    "security",
    "quantity",
    "price",
    "market_value",
    "cost_basis",
]
TRANSACTION_ROLES: Final = frozenset(
    {"date", "payee", "memo", "amount", "debit", "credit", "balance"}
)
HOLDING_ROLES: Final = frozenset(
    {"symbol", "security", "quantity", "price", "market_value", "cost_basis"}
)

_DIGIT: Final = re.compile(r"\d")
_LETTERS: Final = re.compile(r"[^\W\d_]{2,}")
_TOKEN: Final = re.compile(r"[^\w]+")
_EDGE: Final = re.compile("^[\\s:;,.\\-\u2013\u2014|#*]+|[\\s:;,.\\-\u2013\u2014|#*]+$")
_ACCOUNT: Final = re.compile("[0-9Xx*\u2022][0-9Xx*\u2022 -]{2,40}[0-9]")


def label_text(text: str, *, limit: int = MAX_LABEL) -> str | None:
    """``text`` as a storable label: trimmed, printable, letters, **no digit** (None: not one)."""
    clean = _EDGE.sub("", " ".join(text.split()))
    if not 2 <= len(clean) <= limit or not clean.isprintable():
        return None
    if _DIGIT.search(clean) or not _LETTERS.search(clean):
        return None
    return clean


def note_text(text: str | None, *, limit: int = MAX_NOTE) -> str | None:
    """A free-text note as it may be kept: one line, **words holding a digit left out**.

    Notes ("Other — describe it") are stored on the remembered layout and in
    the audit log, which outlive the document, so an account or sort number
    typed into one is not kept (the rule labels and headers follow).
    """
    words = [w for w in (text or "").split() if not _DIGIT.search(w)]
    clean = " ".join(words)[:limit].strip()
    return clean if clean and clean.isprintable() else None


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(t for t in _TOKEN.split(text.casefold()) if t)


def _label_value(value: str) -> str:
    found = label_text(value)
    if found is None:
        raise ValueError("a label is 2-120 printable characters with letters and no digits")
    return found


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Label = Annotated[str, Field(min_length=2, max_length=MAX_LABEL)]
Note = Annotated[str, Field(min_length=1, max_length=MAX_NOTE)]


class FieldAnchor(_Strict):
    """Where a field's value is: next to ``label`` (``right``/``below``) or the header band."""

    field: AnchorField
    direction: Direction
    label: Label | None = None

    @field_validator("label")
    @classmethod
    def _label(cls, value: str | None) -> str | None:
        return None if value is None else _label_value(value)

    @model_validator(mode="after")
    def _check(self) -> FieldAnchor:
        if self.direction == "band":
            if self.field not in ("period_start", "period_end") or self.label is not None:
                raise ValueError("only a period is read from the header band, without a label")
        elif self.label is None:
            raise ValueError("an anchor needs its label")
        return self


class TableColumn(_Strict):
    #: The column's header text (no digits); empty for a column without one.
    header: Annotated[str, Field(max_length=MAX_HEADER)] = ""
    role: ColumnRole

    @field_validator("header")
    @classmethod
    def _header(cls, value: str) -> str:
        if not value:
            return value
        found = label_text(value, limit=MAX_HEADER)
        if found is None:
            raise ValueError("a column header has letters and no digits")
        return found


class TableTemplate(_Strict):
    kind: TableKind
    columns: list[TableColumn] = Field(min_length=2, max_length=MAX_COLUMNS)
    #: Text of the line that ends the table (``Total``, ``Ending balance``).
    end: Label | None = None
    #: What the ``amount`` column means. Defaulted, like ``continues``: a
    #: template stored before the field existed reads as it did.
    direction: AmountDirection = "as_printed"
    #: The table runs on over later pages that do not repeat its header.
    continues: bool = False

    @field_validator("end")
    @classmethod
    def _end(cls, value: str | None) -> str | None:
        return None if value is None else _label_value(value)

    @model_validator(mode="after")
    def _check(self) -> TableTemplate:
        roles = [c.role for c in self.columns if c.role != "ignore"]
        if len(roles) != len(set(roles)):
            raise ValueError("a role is given to two columns")
        allowed = TRANSACTION_ROLES if self.kind == "transactions" else HOLDING_ROLES
        if any(role not in allowed for role in roles):
            raise ValueError(f"a {self.kind} table has no such column role")
        if self.kind == "transactions":
            if "date" not in roles or not {"amount", "debit", "credit"} & set(roles):
                raise ValueError("a transaction table needs a date and an amount column")
        elif "quantity" not in roles or not {"symbol", "security"} & set(roles):
            raise ValueError("a holdings table needs a quantity and a symbol or name column")
        if sum(1 for c in self.columns if c.header) < 2:
            raise ValueError("at least two columns need a header to find the table")
        return self


class LayoutTemplate(_Strict):
    """What a household taught PerFin about one layout (module doc)."""

    version: Literal[1] = SCHEMA_VERSION
    fields: list[FieldAnchor] = Field(default_factory=list, max_length=MAX_FIELDS)
    tables: list[TableTemplate] = Field(default_factory=list, max_length=MAX_TABLES)
    ignore: list[Label] = Field(default_factory=list, max_length=MAX_IGNORED)
    #: Free text from "Other — describe it": shown to the household, never read.
    notes: list[Note] = Field(default_factory=list, max_length=MAX_NOTES)

    @field_validator("ignore")
    @classmethod
    def _ignore(cls, values: list[str]) -> list[str]:
        return [_label_value(v) for v in values]

    @field_validator("notes")
    @classmethod
    def _notes(cls, values: list[str]) -> list[str]:
        return [n for n in (note_text(v) for v in values) if n is not None]

    @model_validator(mode="after")
    def _check(self) -> LayoutTemplate:
        kinds = [t.kind for t in self.tables]
        if kinds.count("transactions") > MAX_TRANSACTION_TABLES:
            raise ValueError(f"at most {MAX_TRANSACTION_TABLES} transaction tables")
        if kinds.count("holdings") > 1:
            raise ValueError("one holdings table")
        return self

    @property
    def empty(self) -> bool:
        return not self.fields and not self.tables


def parse_template(data: Any) -> LayoutTemplate | None:
    """A stored template (``parser_fingerprint.template``); None when absent or invalid."""
    if not isinstance(data, dict) or not data:
        return None
    try:
        return LayoutTemplate.model_validate(data)
    except ValidationError:
        return None


def merge_templates(
    earlier: LayoutTemplate, later: LayoutTemplate | None
) -> tuple[LayoutTemplate, list[str]]:
    """``later`` taught on top of ``earlier`` (the same layout taught again).

    What was taught again wins (an anchor per field, the tables of a kind:
    transaction tables taught again replace all the earlier ones); the
    rest of ``earlier`` is kept, ignored lines and notes are added to. The
    second value names what was kept from ``earlier`` (field names and
    ``table:<kind>``), so the person can be told.
    """
    if later is None:
        later = LayoutTemplate()
    taught = {a.field for a in later.fields}
    kinds = {t.kind for t in later.tables}
    fields = [*later.fields, *(a for a in earlier.fields if a.field not in taught)]
    tables = [*later.tables, *(t for t in earlier.tables if t.kind not in kinds)]
    kept: list[str] = [a.field for a in earlier.fields if a.field not in taught]
    kept += [f"table:{t.kind}" for t in earlier.tables if t.kind not in kinds]
    ignore = list(dict.fromkeys([*later.ignore, *earlier.ignore]))[:MAX_IGNORED]
    notes = list(dict.fromkeys([*later.notes, *earlier.notes]))[:MAX_NOTES]
    merged = LayoutTemplate(
        fields=fields[:MAX_FIELDS], tables=tables[:MAX_TABLES], ignore=ignore, notes=notes
    )
    return merged, kept


# ---------------------------------------------------------------------------
# Words and lines
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


def line_words(line: ViewLine) -> list[Word]:
    """The words of ``line`` left to right (their cell's height)."""
    return [
        Word(w.text, w.x0, cell.y0, w.x1, cell.y1) for cell in line.cells for w in cell.word_cells()
    ]


def _cells_of(line: ViewLine) -> list[list[Word]]:
    return [
        [Word(w.text, w.x0, cell.y0, w.x1, cell.y1) for w in cell.word_cells()]
        for cell in line.cells
    ]


BBox = tuple[float, float, float, float]


def inside(word: Word, box: Sequence[float]) -> bool:
    """The word's centre lies in ``box`` (the review UI's ``wordInside``)."""
    x0, y0, x1, y1 = box
    return x0 <= word.cx <= x1 and y0 <= word.cy <= y1


def page_lines(view: DocumentView, page: int) -> list[ViewLine]:
    return [line for line in view.lines if line.page == page]


def selected(
    view: DocumentView, page: int, box: Sequence[float]
) -> list[tuple[ViewLine, list[Word]]]:
    """The lines of ``page`` with words inside ``box`` and those words, top to bottom."""
    out: list[tuple[ViewLine, list[Word]]] = []
    for line in page_lines(view, page):
        words = [w for w in line_words(line) if inside(w, box)]
        if words:
            out.append((line, words))
    return out


def selection_text(view: DocumentView, page: int, box: Sequence[float]) -> str:
    return " ".join(" ".join(w.text for w in words) for _, words in selected(view, page, box))[
        :1000
    ]


def _find(words: Sequence[Word], label: str, start: int = 0) -> int | None:
    """Index after the label's last word when ``label`` is consecutive words of ``words``."""
    want = _tokens(label)
    if not want:
        return None
    flat: list[tuple[str, int]] = []
    for index, word in enumerate(words):
        flat.extend((token, index) for token in _tokens(word.text))
    for i in range(len(flat) - len(want) + 1):
        if flat[i][1] < start:
            continue
        if tuple(t for t, _ in flat[i : i + len(want)]) == want:
            last = flat[i + len(want) - 1][1]
            # A label word may carry the value (``Total:123``): not ours.
            return last + 1
    return None


def _has(line: ViewLine, label: str) -> bool:
    return _find(line_words(line), label) is not None


def _starts(line: ViewLine, label: str) -> bool:
    """``line`` begins with the words of ``label`` (how a table's ``end`` text is found)."""
    want = _tokens(label)
    have = [token for word in line_words(line) for token in _tokens(word.text)]
    return bool(want) and tuple(have[: len(want)]) == want


def _end_text(line: ViewLine) -> str | None:
    """The words ``line`` starts with, up to its first figure, as a label (None: none).

    ``Received in all 1,962.75`` gives ``Received in all``; a line that
    starts with a figure or a date (a row) gives nothing.
    """
    lead: list[str] = []
    for word in line_words(line):
        if _DIGIT.search(word.text):
            break
        lead.append(word.text)
    return label_text(" ".join(lead)) if lead else None


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

ValueKind = Literal["date", "amount", "account", "text"]
FIELD_KINDS: Final[dict[str, ValueKind]] = {
    "account_number": "account",
    "account_name": "text",
    "period_start": "date",
    "period_end": "date",
    "opening_balance": "amount",
    "closing_balance": "amount",
    "institution": "text",
}


def read_value(field_name: str, text: str) -> str | None:
    """The value ``text`` holds for ``field_name``, as a string (date ISO, amount, last4, text)."""
    kind = FIELD_KINDS.get(field_name, "text")
    text = " ".join(text.split())[:300]
    if not text:
        return None
    if kind == "date":
        ranges = find_ranges(text)
        if ranges:
            found = ranges[0].start if field_name == "period_start" else ranges[0].end
            return found.isoformat()
        hits = [h.value for h in classify_dates(text) if h.value is not None]
        if not hits:
            return None
        return (hits[0] if field_name == "period_start" else hits[-1]).isoformat()
    if kind == "amount":
        for token in text.split():
            amount = read_amount(token.rstrip(".,;:"))
            if amount is not None:
                return str(amount.value)
        return None
    if kind == "account":
        for match in _ACCOUNT.finditer(text):
            digits = "".join(ch for ch in match.group(0) if ch.isdigit())
            if len(digits) >= 4:
                return digits[-4:]
        return None
    clean = label_text(text, limit=200) if field_name == "account_name" else " ".join(text.split())
    return clean[:200] if clean else None


@dataclass
class FieldRead:
    value: str
    page: int
    bbox: BBox
    source: Literal["anchor", "band"]


def _box(words: Sequence[Word]) -> BBox:
    return (
        min(w.x0 for w in words),
        min(w.y0 for w in words),
        max(w.x1 for w in words),
        max(w.y1 for w in words),
    )


def _overlap(a: Sequence[float], b: Sequence[float]) -> bool:
    """Whether two boxes share some area (or touch)."""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _ignored(line: ViewLine, ignore: Sequence[str]) -> bool:
    return any(_has(line, label) for label in ignore)


def _right(line: ViewLine, label: str) -> list[list[Word]]:
    """Candidate value words right of ``label`` on ``line``: the rest of its cell, then cells."""
    cells = _cells_of(line)
    flat = [w for cell in cells for w in cell]
    end = _find(flat, label)
    if end is None:
        return []
    out: list[list[Word]] = []
    seen = 0
    for cell in cells:
        rest = [w for i, w in enumerate(cell, start=seen) if i >= end]
        seen += len(cell)
        if rest:
            out.append(rest)
    return out


def _below(lines: Sequence[ViewLine], index: int, label: str) -> list[list[Word]]:
    """Value words on the lines just under ``label`` (overlapping it horizontally)."""
    words = line_words(lines[index])
    end = _find(words, label)
    if end is None:
        return []
    want = len(_tokens(label))
    used: list[Word] = []
    count = 0
    for word in reversed(words[:end]):
        used.append(word)
        count += len(_tokens(word.text))
        if count >= want:
            break
    x0, x1 = min(w.x0 for w in used), max(w.x1 for w in used)
    pad = max(x1 - x0, 20.0) * 0.5
    out: list[list[Word]] = []
    for line in lines[index + 1 : index + 3]:
        if line.page != lines[index].page:
            break
        # Whole cells under the label (a value is often wider than its label)
        # and the cells that continue them closely to the right.
        under: list[Word] = []
        for cell in _cells_of(line):
            if not cell:
                continue
            overlaps = any(w.x1 >= x0 - pad and w.x0 <= x1 + pad for w in cell)
            gap = cell[0].x0 - under[-1].x1 if under else None
            height = max(cell[0].y1 - cell[0].y0, 1.0)
            if overlaps or (gap is not None and gap <= height * 2.5):
                under.extend(cell)
            elif under:
                break
        if under:
            out.append(under)
    return out


def read_anchor(
    view: DocumentView, anchor: FieldAnchor, ignore: Sequence[str] = ()
) -> FieldRead | None:
    """The value of ``anchor`` in ``view`` (the first place it reads), or None."""
    if anchor.direction == "band":
        band = header_band_range(page_heads(view))
        if band is None:
            return None
        found = band.start if anchor.field == "period_start" else band.end
        first = view.lines[0] if view.lines else None
        box: BBox = first.bbox if first is not None else (0.0, 0.0, 0.0, 0.0)
        return FieldRead(found.isoformat(), first.page if first else 1, box, "band")
    label = anchor.label or ""
    lines = view.lines
    for index, line in enumerate(lines):
        if _ignored(line, ignore) and not _has(line, label):
            continue
        if not _has(line, label):
            continue
        candidates = (
            _right(line, label) if anchor.direction == "right" else _below(lines, index, label)
        )
        for words in candidates:
            value = read_value(anchor.field, " ".join(w.text for w in words))
            if value is not None:
                return FieldRead(value, line.page, _box(words), "anchor")
    return None


def read_fields(view: DocumentView, template: LayoutTemplate) -> dict[str, FieldRead]:
    out: dict[str, FieldRead] = {}
    for anchor in template.fields:
        if anchor.field in out:
            continue
        found = read_anchor(view, anchor, template.ignore)
        if found is not None:
            out[anchor.field] = found
    return out


# ---------------------------------------------------------------------------
# Anchors from a selection
# ---------------------------------------------------------------------------


def _left_label(words: Sequence[Word], before: float) -> str | None:
    """The digit-free words just left of ``before`` on one line, as a label."""
    left = [w for w in words if w.x1 <= before + 0.5]
    left.sort(key=lambda w: w.x0)
    taken: list[Word] = []
    for word in reversed(left):
        if _DIGIT.search(word.text) or len(taken) >= MAX_LABEL_WORDS:
            break
        if taken and taken[-1].x0 - word.x1 > max(word.y1 - word.y0, 1.0) * 3:
            break  # a wide gap: another cell's text, not this value's label
        taken.append(word)
    return label_text(" ".join(w.text for w in reversed(taken))) if taken else None


def derive_anchor(
    view: DocumentView, page: int, box: Sequence[float], field_name: AnchorField
) -> FieldAnchor | None:
    """The anchor that finds the value selected in ``box`` again (None: no usable label).

    The label is the digit-free text just left of the value on its line
    (``right``), else the text right above it (``below``). A selection that
    includes its own label (``Account number: 1234``) is split at the first
    word that is not label text. An unlabelled date range in the header band
    is a ``band`` anchor. A ``right`` or ``below`` anchor is kept only when
    reading it on this document finds the selected value (the first line
    holding the label is the one read on the next statement): a label
    printed twice over different figures, such as ``Balance`` on two summary
    lines, would otherwise read the wrong figure next time. The value read
    may sit on another page: a label printed in every page header and
    selected on page 2 reads the same value on page 1.
    """
    chosen = selected(view, page, box)
    if not chosen:
        return None
    line, words = chosen[0]
    words.sort(key=lambda w: w.x0)
    # A selection starting with its own label: "Holder reference: 1234".
    own: list[Word] = []
    for word in words:
        if _DIGIT.search(word.text):
            break
        own.append(word)
        if word.text.endswith(":"):
            break

    def reads_back(anchor: FieldAnchor, value_words: Sequence[Word]) -> bool:
        # The label must read the selected value again, not another one
        # under the same label ("Balance" on two summary lines). The value
        # may be read elsewhere (a label printed in every page header is
        # read on the first page when the person selected it on page 2).
        found = read_anchor(view, anchor)
        if found is None:
            return False
        wanted = read_value(field_name, " ".join(w.text for w in value_words))
        if wanted is not None and found.value == wanted:
            return True
        return found.page == page and _overlap(found.bbox, _box(value_words))

    if own and len(own) < len(words):
        found = label_text(" ".join(w.text for w in own))
        if found is not None:
            anchor = FieldAnchor(field=field_name, direction="right", label=found)
            if reads_back(anchor, words[len(own) :]):
                return anchor
    found = _left_label(line_words(line), words[0].x0)
    if found is not None:
        anchor = FieldAnchor(field=field_name, direction="right", label=found)
        if reads_back(anchor, words):
            return anchor
    lines = page_lines(view, page)
    position = next((i for i, candidate in enumerate(lines) if candidate is line), None)
    if position is not None and position > 0:
        x0, x1 = min(w.x0 for w in words), max(w.x1 for w in words)
        pad = max(x1 - x0, 20.0) * 0.5
        above = [
            w for w in line_words(lines[position - 1]) if w.x1 >= x0 - pad and w.x0 <= x1 + pad
        ]
        text = label_text(" ".join(w.text for w in above)) if above else None
        if text is not None:
            anchor = FieldAnchor(field=field_name, direction="below", label=text)
            if reads_back(anchor, words):
                return anchor
    if field_name in ("period_start", "period_end"):
        band = header_band_range(page_heads(view))
        value = read_value(field_name, " ".join(w.text for w in words))
        if band is not None and value is not None:
            ends = (band.start.isoformat(), band.end.isoformat())
            if value in ends:
                return FieldAnchor(field=field_name, direction="band")
    return None


def table_from_selection(
    view: DocumentView,
    page: int,
    box: Sequence[float],
    kind: TableKind,
    columns: Sequence[tuple[float, float, str]],
    *,
    continues: bool = False,
) -> TableTemplate:
    """A table template from a selected box and its columns ``(x0, x1, role)``.

    The header text of each column is the selection's first line inside the
    column's span (digits dropped: a column headed only by a date has no
    header). The ``end`` text is what the first line under the box starts
    with, up to its first figure (:func:`_end_text`: a total line ``Sent in
    all 132.61`` ends the table at ``Sent in all``). It is left out when a
    line inside the selection starts with the same words (it would end the
    table on this very page), and, for a table that ``continues`` over
    pages, when the line holds a figure: under the first page of such a
    table stands the page's foot (``Sheet 1 of 2``), not the table's end.
    Raises ``ValueError`` when the columns do not make a table.
    """
    chosen = selected(view, page, box)
    if not chosen:
        raise ValueError("the selection holds no words")
    header_words = chosen[0][1]
    built: list[TableColumn] = []
    for x0, x1, role in columns:
        words = [w for w in header_words if x0 <= w.cx <= x1]
        text = " ".join(w.text for w in words)
        header = label_text(text, limit=MAX_HEADER) or ""
        built.append(TableColumn(header=header, role=role))  # type: ignore[arg-type]
    end: str | None = None
    lines = page_lines(view, page)
    last = chosen[-1][0]
    after = [line for line in lines if line.bbox[1] > max(box[3], last.bbox[3])]
    if after:
        figures = any(_DIGIT.search(w.text) for w in line_words(after[0]))
        end = None if continues and figures else _end_text(after[0])
        if end is not None and any(_starts(line, end) for line, _ in chosen):
            end = None
    try:
        return TableTemplate(kind=kind, columns=built, end=end, continues=continues)
    except ValidationError as exc:
        raise ValueError(_first_error(exc)) from None


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    return str(errors[0].get("msg", "invalid table"))[:200] if errors else "invalid table"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Span:
    role: str
    x0: float
    x1: float


def _header_spans(line: ViewLine, table: TableTemplate) -> list[_Span] | None:
    """Column spans when ``line`` holds the table's headers in order (None: not its header)."""
    words = line_words(line)
    found: list[tuple[int, float, float] | None] = []
    start = 0
    for column in table.columns:
        if not column.header:
            found.append(None)
            continue
        end = _find(words, column.header, start)
        if end is None:
            return None
        count = len(_tokens(column.header))
        used: list[Word] = []
        tokens = 0
        for word in reversed(words[:end]):
            used.append(word)
            tokens += len(_tokens(word.text))
            if tokens >= count:
                break
        found.append((end, min(w.x0 for w in used), max(w.x1 for w in used)))
        start = end
    known = [(i, f) for i, f in enumerate(found) if f is not None]
    if len(known) < 2:
        return None
    centres: list[float] = []
    for index in range(len(found)):
        hit = found[index]
        if hit is not None:
            centres.append((hit[1] + hit[2]) / 2)
            continue
        before = next((f for i, f in reversed(known) if i < index), None)
        after = next((f for i, f in known if i > index), None)
        if before is not None and after is not None:
            centres.append((before[2] + after[1]) / 2)
        elif before is not None:
            centres.append(before[2] + (before[2] - before[1]))
        elif after is not None:
            centres.append(after[1] - (after[2] - after[1]))
        else:  # pragma: no cover - two headers are required
            return None
    spans: list[_Span] = []
    for index, column in enumerate(table.columns):
        left = (centres[index - 1] + centres[index]) / 2 if index > 0 else float("-inf")
        right = (
            (centres[index] + centres[index + 1]) / 2 if index + 1 < len(centres) else float("inf")
        )
        hit = found[index]
        if hit is not None:
            # A header wider than the midpoint keeps its own words.
            left, right = min(left, hit[1]), max(right, hit[2])
        spans.append(_Span(column.role, left, right))
    return spans


def _assign(line: ViewLine, spans: Sequence[_Span]) -> dict[str, list[Word]]:
    out: dict[str, list[Word]] = {}
    for word in line_words(line):
        best: _Span | None = None
        for span in spans:
            if span.x0 <= word.cx <= span.x1:
                best = span
                break
        if best is not None:
            out.setdefault(best.role, []).append(word)
    return out


def _text(words: Sequence[Word] | None) -> str:
    return " ".join(w.text for w in words or ())


@dataclass
class _Row:
    page: int
    bbox: BBox
    cells: dict[str, list[Word]]
    extra: list[str] = field(default_factory=list)
    #: Index of the row's table in the template.
    table: int = 0
    #: Index of the row's line in the view (page order across tables).
    line: int = 0


@dataclass
class TableRead:
    rows: list[_Row] = field(default_factory=list)
    skipped: list[tuple[int, BBox]] = field(default_factory=list)
    skipped_count: int = 0
    found: bool = False
    #: Which of several tables under the same headers this is was a guess
    #: (module doc, "A guessed table").
    guessed: bool = False


def _dateish(text: str) -> bool:
    """A cell that reads as a date (with or without its year)."""
    text = text.strip()
    return bool(text) and (numeric_parts(text) is not None or bool(classify_dates(text)))


def _row_kind(cells: dict[str, list[Word]], kind: TableKind) -> Literal["row", "more", "other"]:
    if kind == "transactions":
        dated = _dateish(_text(cells.get("date")))
        money = any(
            read_amount(_text(cells.get(role)).replace(" ", "")) is not None
            for role in ("amount", "debit", "credit")
        )
        described = bool(_text(cells.get("payee")) or _text(cells.get("memo")))
        if dated:
            return "row" if money else "other"
        others = set(cells) - {"payee", "memo", "ignore"}
        if described and not money and not others:
            return "more"
        return "other"
    quantity = read_number(_text(cells.get("quantity")).replace(" ", ""))
    named = bool(_text(cells.get("symbol")) or _text(cells.get("security")))
    if quantity is not None and named:
        return "row"
    if named and set(cells) <= {"security", "symbol", "ignore"}:
        return "more"
    return "other"


def _close_below(row: _Row, line: ViewLine) -> bool:
    """``line`` sits right under ``row`` (wrapped text, not a footer further down)."""
    height = max(row.bbox[3] - row.bbox[1], line.height, 1.0)
    return line.page == row.page and line.bbox[1] - row.bbox[3] <= height * 1.5


def _headed(table: TableTemplate) -> int:
    return sum(1 for column in table.columns if column.header)


def _meaning(table: TableTemplate) -> tuple[Any, ...]:
    """What tells two tables under the same headers apart when a row is read."""
    roles = tuple(column.role for column in table.columns)
    return (table.kind, roles, table.direction if "amount" in roles else None)


@dataclass(frozen=True)
class _Hit:
    table: int
    spans: list[_Span]
    #: The tables the header fits as well and that read a row differently
    #: (with ``table`` itself); empty when the header says which table it is.
    rivals: tuple[int, ...] = ()
    #: Chosen because it was still open from the page before.
    carried: bool = False


def _header_hit(
    line: ViewLine,
    tables: Sequence[TableTemplate],
    reads: Sequence[TableRead],
    carried: int | None,
    recent: int | None,
) -> _Hit | None:
    """The table ``line`` is the header of, with its column spans (module doc).

    ``carried``: the table still open from the page before, while no header
    was seen on this page; ``recent``: the table whose header was seen last.
    """
    hits: dict[int, list[_Span]] = {}
    for index, table in enumerate(tables):
        spans = _header_spans(line, table)
        if spans is not None:
            hits[index] = spans
    if not hits:
        return None
    most = max(_headed(tables[index]) for index in hits)
    equal = [index for index in hits if _headed(tables[index]) == most]
    by_carry = False
    if carried is not None and carried in equal:
        chosen, by_carry = carried, True
    elif len(equal) == 1:
        chosen = equal[0]
    else:
        fresh = [index for index in equal if not reads[index].found]
        later = [index for index in equal if recent is not None and index > recent]
        chosen = fresh[0] if fresh else (later[0] if later else equal[0])
    rivals = tuple(equal) if len({_meaning(tables[index]) for index in equal}) > 1 else ()
    return _Hit(chosen, hits[chosen], rivals, by_carry)


def read_tables(
    view: DocumentView, tables: Sequence[TableTemplate], ignore: Sequence[str] = ()
) -> list[TableRead]:
    """The rows of every one of ``tables`` in ``view``, read in one pass (module doc)."""
    reads = [TableRead() for _ in tables]
    current: int | None = None
    spans: list[_Span] | None = None
    last: _Row | None = None
    #: The open table came over a page break without a header; no row of it yet.
    waiting = False
    carried: int | None = None
    recent: int | None = None
    #: Tables that share their headers and read a row differently, the times
    #: each was started by its place in the order, and those that were guessed.
    groups: set[tuple[int, ...]] = set()
    started = [0 for _ in tables]
    guessed: set[int] = set()
    page = 0
    for number, line in enumerate(view.lines):
        if line.page != page:
            page, last, carried = line.page, None, current
            if current is not None and tables[current].continues:
                waiting = True
            else:
                current, spans = None, None
        hit = _header_hit(line, tables, reads, carried, recent)
        if hit is not None:
            current, spans = hit.table, hit.spans
            last, waiting, carried, recent = None, False, None, current
            reads[current].found = True
            if hit.rivals:
                groups.add(hit.rivals)
                if not hit.carried:
                    started[current] += 1
                elif tables[current].end is None:
                    # Nothing says the open table did not end with its page.
                    guessed.update(hit.rivals)
            continue
        if current is None or spans is None:
            continue
        table, out = tables[current], reads[current]
        if table.end is not None and _starts(line, table.end):
            current, spans, last, waiting = None, None, None, False
            continue
        if _ignored(line, ignore):
            continue
        cells = _assign(line, spans)
        kind = _row_kind(cells, table.kind)
        if waiting:
            # The page's own heading stands above the continued rows.
            starts = (
                _dateish(_text(cells.get("date")))
                if table.kind == "transactions"
                else kind == "row"
            )
            if not starts:
                continue
            waiting = False
        if kind == "row":
            if len(out.rows) >= MAX_ROWS:
                break
            last = _Row(line.page, line.bbox, cells, table=current, line=number)
            out.rows.append(last)
        elif kind == "more" and last is not None and _close_below(last, line):
            last.extra.append(
                _text(cells.get("payee") or cells.get("memo") or cells.get("security"))
            )
            box = line.bbox
            last.bbox = (
                min(last.bbox[0], box[0]),
                last.bbox[1],
                max(last.bbox[2], box[2]),
                max(last.bbox[3], box[3]),
            )
        else:
            if table.kind == "transactions" and _dateish(_text(cells.get("date"))):
                out.skipped_count += 1
                if len(out.skipped) < MAX_SKIPPED_REGIONS:
                    out.skipped.append((line.page, line.bbox))
            last = None
    for group in groups:
        # A section the statement did not print: which one is not said.
        if len({started[index] for index in group}) > 1:
            guessed.update(group)
    for index in guessed:
        reads[index].guessed = True
    return reads


def read_table(view: DocumentView, table: TableTemplate, ignore: Sequence[str] = ()) -> TableRead:
    """The rows of one ``table`` in ``view`` (module doc)."""
    return read_tables(view, [table], ignore)[0]


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------


@dataclass
class TemplateOutcome:
    statements: list[Statement] = field(default_factory=list)
    investments: list[InvestmentStatement] = field(default_factory=list)
    fields: dict[str, FieldRead] = field(default_factory=dict)
    skipped_lines: int = 0
    skipped: list[tuple[int, BBox]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Tables whose header was found (even without rows).
    tables_found: list[str] = field(default_factory=list)
    #: What each table of the template read, in the template's order.
    tables: list[TableRead] = field(default_factory=list)
    #: For each transaction of the taught statement, the index of its table.
    row_tables: list[int] = field(default_factory=list)

    @property
    def read(self) -> bool:
        return any(s.transactions for s in self.statements) or any(
            i.positions for i in self.investments
        )


def _date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except ArithmeticError:
        return None


def _amount(words: Sequence[Word] | None) -> Decimal | None:
    text = _text(words).replace(" ", "")
    found = read_amount(text) if text else None
    if found is None:
        return None
    if found.credit is False:
        return -abs(found.value)
    return found.value


def _directed(words: Sequence[Word] | None, direction: AmountDirection) -> Decimal | None:
    """An ``amount`` cell under its table's ``direction`` (module doc)."""
    if direction == "as_printed":
        return _amount(words)
    text = _text(words).replace(" ", "")
    found = read_amount(text) if text else None
    if found is None:
        return None
    if found.credit is not None:
        return abs(found.value) if found.credit else -abs(found.value)
    return found.value if direction == "money_in" else -found.value


def owed_balances(template: LayoutTemplate) -> bool:
    """Whether the layout's balances are amounts owed (a transaction table is a card's).

    Such balances are negated when the statement is built, like the rows
    (module doc); :func:`fill_statement` is told with ``owed``. One ``card``
    table is enough: a card that prints its payments apart has them taught
    as ``money_in`` beside the ``card`` table of its purchases.
    """
    return any(t.kind == "transactions" and t.direction == "card" for t in template.tables)


#: Month-name styles without a year, as the generic reader has them.
NAMED_YEARLESS: Final[tuple[DateStyle, ...]] = ("mon_d", "d_mon")


def _yearless(text: str) -> bool:
    """A date cell printed without its year (``03/14``, ``Mar 14``, ``14 Mar``)."""
    parts = numeric_parts(text)
    if parts is not None:
        return parts[2] is None
    return any(month_day(text, style) is not None for style in NAMED_YEARLESS)


def _styles(texts: Iterable[str]) -> tuple[list[DateStyle], bool]:
    texts = [t for t in texts if t]
    numeric = [t for t in texts if numeric_parts(t) is not None]
    style, certain = guess_numeric_style(numeric)
    yearless = any((numeric_parts(t) or (0, 0, 0))[2] is None for t in numeric)
    styles: list[DateStyle] = []
    if style is not None:
        styles.append(style)
        if yearless:
            styles.append("md" if style == "mdy" else "dm")
    styles.extend(s for s in ("ymd", "d_mon_y", "mon_d_y") if s not in styles)
    # Last: a cell with its year is read with it before a window is asked.
    styles.extend(NAMED_YEARLESS)
    return styles, certain or not numeric


def _period(fields: dict[str, FieldRead]) -> tuple[date, date] | None:
    start = _date(fields["period_start"].value) if "period_start" in fields else None
    end = _date(fields["period_end"].value) if "period_end" in fields else None
    if start is not None and end is not None and start <= end:
        return start, end
    return None


def _period_confidence(fields: dict[str, FieldRead]) -> float:
    sources = {fields[k].source for k in ("period_start", "period_end") if k in fields}
    return 0.9 if "band" in sources else conf.GENERIC


def _account(fields: dict[str, FieldRead]) -> StatementAccount:
    last4 = fields["account_number"].value if "account_number" in fields else None
    name = fields["account_name"].value if "account_name" in fields else None
    return StatementAccount(
        last4=last4 if last4 is not None and re.fullmatch(r"\d{4}", last4) else None,
        name=name[:200] if name else None,
    )


def _transactions(
    view: DocumentView,
    template: LayoutTemplate,
    read: Sequence[_Row],
    fields: dict[str, FieldRead],
    threshold: float,
    outcome: TemplateOutcome,
) -> Statement | None:
    """One statement from the rows of the transaction tables, in page order."""
    period = _period(fields)
    styles, certain = _styles(_text(r.cells.get("date")) for r in read)
    rows: list[ExtractedTransaction] = []
    sources: list[int] = []
    scores: list[dict[str, float]] = []
    ledger: list[Row] = []
    # Balances owed (a card's) are negative in the ledger, like its rows.
    owed = owed_balances(template)
    # A running balance is checked row by row inside one table only.
    several = len({row.table for row in read}) > 1
    undated = False
    for row in read:
        text = _text(row.cells.get("date"))
        posted: date | None = None
        for style in styles:
            posted = read_date(text, style, window=period)
            if posted is not None:
                break
        if posted is None and period is None and _yearless(text):
            undated = True
        amount = _directed(row.cells.get("amount"), template.tables[row.table].direction)
        if amount is None:
            debit, credit = _amount(row.cells.get("debit")), _amount(row.cells.get("credit"))
            if debit is not None or credit is not None:
                amount = (abs(credit) if credit is not None else Decimal(0)) - (
                    abs(debit) if debit is not None else Decimal(0)
                )
        if posted is None or amount is None:
            outcome.skipped_lines += 1
            if len(outcome.skipped) < MAX_SKIPPED_REGIONS:
                outcome.skipped.append((row.page, row.bbox))
            continue
        payee = " ".join([_text(row.cells.get("payee")), *row.extra]).strip() or None
        memo = _text(row.cells.get("memo")).strip() or None
        score = {
            "posted_date": conf.GENERIC if certain else conf.GUESSED,
            "amount": conf.GENERIC,
            "payee": conf.GENERIC if payee else conf.MISSING,
        }
        # A line that names a balance or a statement gets no days of grace.
        if conf.outside_period_for(posted, period, payee):
            score["posted_date"] = min(score["posted_date"], conf.INFERRED)
        scores.append(score)
        kept = None if several else _amount(row.cells.get("balance"))
        ledger.append(Row(amount, -kept if owed and kept is not None else kept))
        sources.append(row.table)
        rows.append(
            ExtractedTransaction(
                posted=posted,
                amount=amount,
                payee=payee[:4096] if payee else None,
                memo=memo[:4096] if memo else None,
                page=row.page,
                bbox=row.bbox,
            )
        )
    if undated and "dates_without_year" not in outcome.warnings:
        outcome.warnings.append("dates_without_year")
    if not rows:
        return None
    outcome.row_tables = sources
    opening = _decimal(fields["opening_balance"].value) if "opening_balance" in fields else None
    closing = _decimal(fields["closing_balance"].value) if "closing_balance" in fields else None
    if owed:
        opening = -opening if opening is not None else None
        closing = -closing if closing is not None else None
    statement_conf: dict[str, float] = {
        "opening_balance": conf.GENERIC if opening is not None else conf.MISSING,
        "closing_balance": conf.GENERIC if closing is not None else conf.MISSING,
    }
    if opening is None and ledger[0].balance is not None:
        opening = ledger[0].balance - ledger[0].amount
        statement_conf["opening_balance"] = conf.DERIVED
    if closing is None and ledger[-1].balance is not None:
        closing = ledger[-1].balance
        statement_conf["closing_balance"] = conf.DERIVED
    check = reconcile(opening, closing, ledger)
    for index in check.running_mismatches:
        scores[index]["amount"] = min(scores[index]["amount"], conf.CONTRADICTED)
    cap = {"mismatch": conf.UNRECONCILED, "unavailable": conf.UNCHECKED}.get(check.status)
    if cap is not None:
        for score in scores:
            score["reconciliation"] = cap
    guessed = {index for index, table in enumerate(outcome.tables) if table.guessed}
    if check.status != "ok" and guessed.intersection(sources):
        # Which table these rows stand in, and so their sign, was a guess
        # that no balance bears out (module doc, "A guessed table").
        for score, source in zip(scores, sources, strict=True):
            if source in guessed:
                score["amount"] = min(score["amount"], conf.GUESSED)
        if "table_order_guessed" not in outcome.warnings:
            outcome.warnings.append("table_order_guessed")
    if period is None:
        period = (min(r.posted for r in rows), max(r.posted for r in rows))
        statement_conf["period"] = conf.INFERRED
    else:
        statement_conf["period"] = _period_confidence(fields)
    statement_conf["account_number"] = conf.GENERIC if "account_number" in fields else conf.MISSING
    statement_conf["reconciliation"] = 1.0 if check.status == "ok" else (cap or 1.0)
    if not certain:
        outcome.warnings.append("date_order_guessed")
    return Statement(
        source_format=view.source_format,
        account=_account(fields),
        period_start=period[0],
        period_end=period[1],
        opening_balance=Balance(amount=opening, as_of=period[0]) if opening is not None else None,
        ledger_balance=Balance(amount=closing, as_of=period[1]) if closing is not None else None,
        transactions=[
            row.model_copy(
                update={
                    "confidence": min(score.values()),
                    "low_confidence": conf.low_fields(score, threshold),
                }
            )
            for row, score in zip(rows, scores, strict=True)
        ],
        date_order="ymd"
        if styles[0] == "ymd"
        else ("dmy" if styles[0] in ("dmy", "dm") else "mdy"),
        truncated=view.truncated,
        parser=TEMPLATE_REF,
        confidence={k: round(v, 4) for k, v in statement_conf.items()},
        reconciliation=check,
    )


def _holdings(
    view: DocumentView, read: TableRead, fields: dict[str, FieldRead], threshold: float
) -> InvestmentStatement | None:
    period = _period(fields)
    positions: list[InvestmentPosition] = []
    for row in read.rows:
        quantity = read_number(_text(row.cells.get("quantity")).replace(" ", ""))
        if quantity is None:
            continue
        symbol_text = _text(row.cells.get("symbol")).strip().upper() or None
        symbol = (
            symbol_text
            if symbol_text is not None and re.fullmatch(r"[A-Z0-9.^/:_-]{1,32}", symbol_text)
            else None
        )
        name = " ".join([_text(row.cells.get("security")), *row.extra]).strip() or None
        if symbol is None and not has_word(name):
            continue  # a holding needs a symbol or a name (it may be named by it alone)
        price = read_number(_text(row.cells.get("price")).replace(" ", "").lstrip("$"))
        score = {"quantity": conf.GENERIC, "security": conf.GENERIC}
        positions.append(
            InvestmentPosition(
                security=SecurityRef(symbol=symbol, name=(name or symbol_text or "")[:200] or None),
                quantity=quantity,
                unit_price=abs(price) if price is not None else None,
                market_value=_amount(row.cells.get("market_value")),
                cost_basis=_amount(row.cells.get("cost_basis")),
                price_as_of=period[1] if period else None,
                confidence=min(score.values()),
                low_confidence=conf.low_fields(score, threshold),
                page=row.page,
                bbox=row.bbox,
            )
        )
    if not positions:
        return None
    values = [p.market_value for p in positions if p.market_value is not None]
    statement = InvestmentStatement(
        source_format=view.source_format,
        account=_account(fields),
        period_start=period[0] if period else None,
        period_end=period[1] if period else None,
        as_of=period[1] if period else None,
        positions=positions,
        positions_value=(
            Balance(amount=sum_amounts(values), as_of=period[1] if period else None)
            if values and len(values) == len(positions) and "closing_balance" in fields
            else None
        ),
        total_value=(
            Balance(amount=Decimal(fields["closing_balance"].value), as_of=period[1])
            if "closing_balance" in fields and period is not None
            else None
        ),
        truncated=view.truncated,
        parser=TEMPLATE_REF,
        confidence={
            "period": _period_confidence(fields) if period else conf.MISSING,
            "account_number": conf.GENERIC if "account_number" in fields else conf.MISSING,
        },
    )
    return statement.model_copy(update={"reconciliation": reconcile_investment(statement)})


def read_template(
    view: DocumentView,
    template: LayoutTemplate,
    *,
    threshold: float,
    given: dict[str, FieldRead] | None = None,
) -> TemplateOutcome:
    """Read ``view`` with ``template``: its anchors, then its labelled tables (module doc).

    ``given`` are values a person selected or typed on this very document;
    they win over what the anchors read. The rows of all transaction tables
    make one statement, in page order (``row_tables``: each row's table).
    """
    outcome = TemplateOutcome(fields={**read_fields(view, template), **(given or {})})
    outcome.tables = read_tables(view, template.tables, template.ignore)
    ledger: list[_Row] = []
    for table, read in zip(template.tables, outcome.tables, strict=True):
        if read.found:
            outcome.tables_found.append(table.kind)
        outcome.skipped_lines += read.skipped_count
        outcome.skipped.extend(read.skipped[: MAX_SKIPPED_REGIONS - len(outcome.skipped)])
        if table.kind == "transactions":
            ledger.extend(read.rows)
        else:
            holdings = _holdings(view, read, outcome.fields, threshold)
            if holdings is not None:
                outcome.investments.append(holdings)
    if ledger:
        ledger.sort(key=lambda row: row.line)
        statement = _transactions(view, template, ledger, outcome.fields, threshold, outcome)
        if statement is not None:
            outcome.statements.append(statement)
    for name in (
        "period_start",
        "period_end",
        "account_number",
        "opening_balance",
        "closing_balance",
    ):
        if any(a.field == name for a in template.fields) and name not in outcome.fields:
            outcome.warnings.append(f"template_field_missing:{name}")
    return outcome


def fill_statement(
    statement: Statement, fields: dict[str, FieldRead], *, single: bool, owed: bool = False
) -> Statement:
    """A parser's ``statement`` with the anchors' values.

    The period always; the account and balances only for a document's
    ``single`` statement (they belong to one account). ``owed``: the
    balances are amounts owed and are negated (:func:`owed_balances`; pass
    it when ``statement`` was read by a card layout's own tables, whose
    rows and balances are already in the ledger convention).
    """
    update: dict[str, Any] = {}
    confidence = dict(statement.confidence)
    period = _period(fields)
    if period is not None:
        update["period_start"], update["period_end"] = period
        confidence["period"] = _period_confidence(fields)
    if single:
        account = _account(fields)
        if account.last4 is not None or account.name is not None:
            update["account"] = statement.account.model_copy(
                update={k: v for k, v in (("last4", account.last4), ("name", account.name)) if v}
            )
            if account.last4 is not None:
                confidence["account_number"] = conf.GENERIC
        start = period[0] if period else statement.period_start
        end = period[1] if period else statement.period_end
        opening = _decimal(fields["opening_balance"].value) if "opening_balance" in fields else None
        closing = _decimal(fields["closing_balance"].value) if "closing_balance" in fields else None
        if owed:
            opening = -opening if opening is not None else None
            closing = -closing if closing is not None else None
        if opening is not None:
            update["opening_balance"] = Balance(amount=opening, as_of=start)
            confidence["opening_balance"] = conf.GENERIC
        if closing is not None:
            update["ledger_balance"] = Balance(amount=closing, as_of=end)
            confidence["closing_balance"] = conf.GENERIC
        if opening is not None or closing is not None:
            filled = statement.model_copy(update=update)
            return rebalance(filled.model_copy(update={"confidence": confidence}))
    update["confidence"] = confidence
    return statement.model_copy(update=update)


def rebalance(statement: Statement) -> Statement:
    """``statement`` reconciled again with its (corrected) balances.

    The rows' printed running balances are not kept with the statement, so
    only the totals are checked: opening + rows = closing. Rows that were
    only doubted because the statement did not add up are trusted again
    when it now does.
    """
    opening = statement.opening_balance.amount if statement.opening_balance else None
    closing = statement.ledger_balance.amount if statement.ledger_balance else None
    check = reconcile(opening, closing, [Row(t.amount) for t in statement.transactions])
    confidence = dict(statement.confidence)
    rows = statement.transactions
    if check.status == "ok":
        confidence["reconciliation"] = 1.0
        rows = [
            t.model_copy(
                update={
                    "low_confidence": [f for f in t.low_confidence if f != "reconciliation"],
                    "confidence": (
                        conf.GENERIC if t.low_confidence == ["reconciliation"] else t.confidence
                    ),
                }
            )
            for t in rows
        ]
    else:
        cap = conf.UNRECONCILED if check.status == "mismatch" else conf.UNCHECKED
        confidence["reconciliation"] = cap
    return statement.model_copy(
        update={"reconciliation": check, "confidence": confidence, "transactions": rows}
    )

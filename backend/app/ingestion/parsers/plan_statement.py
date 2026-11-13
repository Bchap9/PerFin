# SPDX-License-Identifier: AGPL-3.0-or-later
"""401(k) plan statements in the roll-forward layout (INGESTION.md "Retirement plan statements").

A plan's quarterly statement prints no units and no prices. It prints one
row of dollars per fund (and often the same money again per contribution
source) under a header such as::

    By Investment  Beginning Balance  Earnings/Fees  Contributions  Transfers  Ending Balance

followed by a ``Total`` row. Such a statement becomes **the balance of the
plan and nothing else**: one :class:`InvestmentStatement` with no positions
and no activity, whose ``total_value`` is the ending total on the period's
last day. Dollars per fund are not holdings (a holding needs units and a
cost per unit), so none is written; the importer records the balance
(``app.domain.investments.statement_import``, a parser of
:data:`~app.ingestion.parsers.base.BALANCE_ONLY_PARSERS`).

**The header** (:func:`roll_forward_header`). A line that names
``beginning balance`` and, after it, ``ending balance``, with at least one
movement column between them and nothing between them but movement columns
(:data:`MOVEMENTS`: contributions, earnings, earnings/fees, gain/loss,
withdrawals, distributions, transfers, forfeitures, fees), and no figure on
the line. A bank summary of ``Beginning Balance | Deposits | Withdrawals |
Ending Balance`` is not one (``deposits`` is no plan column), nor is a
sentence naming both balances. Columns it does not know are a layout this
parser does not read: it scores 0 rather than guess what a column means.

**Rows.** A row is a name followed by figures only (``1,200.00``,
``(300.00)``, ``$0.00``; a lone dash is zero), each with two decimals: a
year in a fund's name is no figure. As many figures as columns are taken in
order; fewer are placed under the header whose centre is nearest (a blank
cell is zero), each column at most once and in the printed order. A line
with figures that cannot be read that way is counted, never guessed.
``Sub Total`` rows are skipped. The row named exactly ``Total`` (or
``Totals``, ``Grand Total``, ``Account Total``, ``Plan Total``) is the
printed total: a fund called "Total Market ..." is a fund. A table goes on
over a page break when its header is printed again.

**Checks** (``InvestmentReconciliation.checks``; tolerance
:data:`~app.ingestion.reconcile.TOLERANCE` per comparison):

* ``roll_forward``: on the total row, beginning plus the movement columns
  is ending. The columns are added as printed (parentheses are negative);
  when that does not hold, the columns headed withdrawals, distributions,
  fees and forfeitures are subtracted instead (:data:`SUBTRACTED`), and the
  convention under which the total row adds up is used for every row.
* ``roll_forward_rows``: every row adds up the same way, and the rows'
  ending balances add up to the total's (so a total with no row read
  under it fails, unless it is zero). A line
  with figures that was not read as a row (counted in ``skipped_lines``,
  so a person is told) therefore fails the check whenever it held money.

``reported_total_value`` is the printed ending total and ``total_value``
what the total row's own figures come to.

**Several tables.** A second roll-forward table with the same ending total
is the same money counted another way (by source) and is ignored. One with
another ending total means several plans or participants in one document:
nothing is read (warning ``plan_statement_several_accounts``). No total row
at all: a statement with no ``total_value``, reconciliation ``unavailable``
and the warning ``plan_statement_no_total`` (the importer says that nothing
was recorded).

**What is kept.** The institution (the document's first line), the period
from a ``for period:`` / ``statement period:`` label (``as_of`` is its last
day), ``account_type`` ``retirement``, and the total row as figures of the
group ``account_value``: the beginning total (``opening``), each movement
column's total as it counts (``item``, labelled by its header) and the
ending total (``closing``), which is what lets the importer record the
opening balance under its existing rule. No account number is read (plan
statements print participant ids), no vested balance, no per-fund figure.
A numeric period that reads two ways (``01/02/2026 - 03/04/2026``) is kept
with a confidence below any threshold, and the importer then records
nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import ClassVar, Final, Literal

from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.base import Cell, DocumentParser, DocumentView, ParseOutcome, ViewLine
from app.ingestion.parsers.table_statement import PeriodCandidates, read_period
from app.ingestion.parsers.text import DateStyle, normalise, read_amount, symbol_currency
from app.ingestion.reconcile import TOLERANCE
from app.ingestion.sandbox.protocol import (
    SECTION_LABEL_MAX,
    Balance,
    InvestmentReconciliation,
    InvestmentStatement,
    ParserRef,
    StatementAccount,
    StatementFigure,
)

PARSER_NAME: Final = "retirement_plan_statement"
DETECT_SCORE: Final = 0.65
SEVERAL_ACCOUNTS: Final = "plan_statement_several_accounts"
NO_TOTAL: Final = "plan_statement_no_total"

BEGINNING: Final = ("beginning", "balance")
ENDING: Final = ("ending", "balance")
#: Movement column headings, as normalised words; the longest is tried first.
MOVEMENTS: Final[tuple[tuple[str, ...], ...]] = (
    ("earnings", "fees"),
    ("gain", "loss"),
    ("contributions",),
    ("earnings",),
    ("withdrawals",),
    ("distributions",),
    ("transfers",),
    ("forfeitures",),
    ("fees",),
)
#: Columns of money leaving the plan: subtracted when adding them does not add up.
SUBTRACTED: Final = frozenset({"withdrawals", "distributions", "fees", "forfeitures"})
TOTAL_NAMES: Final = frozenset({"total", "totals", "grand total", "account total", "plan total"})
SUBTOTAL_PREFIXES: Final = ("sub total", "subtotal")
PERIOD_LABELS: Final = ("for the period", "for period", "statement period")
_NUMERIC_ORDERS: Final[tuple[tuple[DateStyle, ...], ...]] = (
    ("mdy", "dmy", "ymd", "d_mon_y", "mon_d_y"),
    ("dmy", "mdy", "ymd", "d_mon_y", "mon_d_y"),
)
_DASHES: Final = frozenset({"-", "\u2013", "\u2014", "\u2212"})
_SYMBOLS: Final = frozenset({"$", "\u00a3", "\u20ac"})
INSTITUTION_MAX: Final = 200
#: Unread lines whose place on the page is kept for the review highlight.
MAX_REGIONS: Final = 50

Convention = Literal["printed", "subtracted"]
_CONVENTIONS: Final[tuple[Convention, ...]] = ("printed", "subtracted")


@dataclass(frozen=True)
class Column:
    """One column of a roll-forward header."""

    kind: Literal["beginning", "movement", "ending"]
    #: The heading's normalised words (``earnings fees``).
    key: str
    #: The heading as printed.
    label: str
    x0: float
    x1: float

    @property
    def centre(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass(frozen=True)
class _Token:
    word: str
    cell: int
    box: Cell


def _tokens(line: ViewLine) -> list[_Token]:
    """The line's normalised words, each with the cell and the word box it came from."""
    out: list[_Token] = []
    for index, cell in enumerate(line.cells):
        for box in cell.word_cells():
            out.extend(_Token(word, index, box) for word in normalise(box.text).split())
    return out


def _match(tokens: Sequence[_Token], at: int, words: tuple[str, ...]) -> bool:
    """``words`` stand at ``at``, all of them in one cell."""
    part = tokens[at : at + len(words)]
    return (
        len(part) == len(words)
        and all(token.word == word for token, word in zip(part, words, strict=True))
        and len({token.cell for token in part}) == 1
    )


def _column(kind: Literal["beginning", "movement", "ending"], part: Sequence[_Token]) -> Column:
    boxes: list[Cell] = []
    for token in part:
        if not boxes or boxes[-1] is not token.box:
            boxes.append(token.box)
    return Column(
        kind=kind,
        key=" ".join(token.word for token in part),
        label=" ".join(box.text for box in boxes)[:SECTION_LABEL_MAX],
        x0=min(box.x0 for box in boxes),
        x1=max(box.x1 for box in boxes),
    )


def roll_forward_header(line: ViewLine) -> tuple[Column, ...] | None:
    """The columns of a roll-forward header on ``line``, else None (module doc)."""
    tokens = _tokens(line)
    if any(ch.isdigit() for token in tokens for ch in token.word):
        return None
    start = next((i for i in range(len(tokens)) if _match(tokens, i, BEGINNING)), None)
    if start is None:
        return None
    columns = [_column("beginning", tokens[start : start + len(BEGINNING)])]
    at = start + len(BEGINNING)
    while at < len(tokens):
        if _match(tokens, at, ENDING):
            if len(columns) < 2:
                return None
            columns.append(_column("ending", tokens[at : at + len(ENDING)]))
            return tuple(columns)
        words = next((m for m in MOVEMENTS if _match(tokens, at, m)), None)
        if words is None:
            return None  # a column this parser has no reading for
        columns.append(_column("movement", tokens[at : at + len(words)]))
        at += len(words)
    return None


@dataclass(frozen=True)
class _Row:
    name: str
    #: One figure per column of the header.
    values: tuple[Decimal, ...]
    line: ViewLine


def _figure(text: str) -> Decimal | None:
    """The figure a word of a row is: an amount with cents, or a dash (zero)."""
    if text in _DASHES:
        return Decimal(0)
    amount = read_amount(text)
    return amount.value if amount is not None else None


def _has_figure(line: ViewLine) -> bool:
    return any(
        read_amount(box.text) is not None for cell in line.cells for box in cell.word_cells()
    )


def _place(
    cells: Sequence[tuple[Cell, Decimal]], columns: Sequence[Column]
) -> tuple[Decimal, ...] | None:
    """One figure per column: in order when all are printed, else under the nearest header."""
    if len(cells) == len(columns):
        return tuple(value for _, value in cells)
    if not cells or len(cells) > len(columns):
        return None
    values: list[Decimal] = [Decimal(0)] * len(columns)
    last = -1
    for cell, value in cells:
        nearest = min(range(len(columns)), key=lambda i: abs(columns[i].centre - cell.centre))
        if nearest <= last:
            return None  # two figures under one header, or out of order
        values[nearest] = value
        last = nearest
    return tuple(values)


def _row(line: ViewLine, columns: Sequence[Column]) -> _Row | None:
    """``line`` as a name and one figure per column, else None."""
    words = [box for cell in line.cells for box in cell.word_cells()]
    figures: list[tuple[Cell, Decimal]] = []
    while words:
        text = words[-1].text.strip()
        if text in _SYMBOLS:
            words.pop()
            continue
        value = _figure(text)
        if value is None:
            break
        figures.append((words.pop(), value))
    name = " ".join(word.text for word in words).strip()
    if not figures or not any(ch.isalpha() for ch in name):
        return None
    if any(read_amount(word.text) is not None for word in words):
        return None  # a figure inside the name: not a row of this table
    values = _place(figures[::-1], columns)
    return _Row(name, values, line) if values is not None else None


@dataclass
class _Table:
    header: ViewLine
    columns: tuple[Column, ...]
    rows: list[_Row] = field(default_factory=list)
    total: _Row | None = None
    #: Lines with figures between the header and the total that are no row.
    unread: list[ViewLine] = field(default_factory=list)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(column.key for column in self.columns)


def _tables(view: DocumentView) -> list[_Table]:
    tables: list[_Table] = []
    current: _Table | None = None
    for line in view.lines:
        columns = roll_forward_header(line)
        if columns is not None:
            if current is not None and tuple(c.key for c in columns) == current.keys:
                continue  # the header again after a page break
            current = _Table(line, columns)
            tables.append(current)
            continue
        if current is None:
            continue
        row = _row(line, current.columns)
        if row is None:
            if _has_figure(line):
                current.unread.append(line)
            continue
        name = normalise(row.name)
        if name.startswith(SUBTOTAL_PREFIXES):
            continue
        if name in TOTAL_NAMES:
            current.total = row
            current = None
            continue
        current.rows.append(row)
    for table in tables:
        if table.total is None:
            table.unread.clear()  # with no total row nothing says where the table ends
    return tables


def _signed(values: Sequence[Decimal], columns: Sequence[Column], how: Convention) -> list[Decimal]:
    """The movement figures of a row as they count under ``how``."""
    return [
        -value if how == "subtracted" and column.key in SUBTRACTED else value
        for value, column in zip(values[1:-1], columns[1:-1], strict=True)
    ]


def _computed(row: _Row, columns: Sequence[Column], how: Convention) -> Decimal:
    return row.values[0] + sum(_signed(row.values, columns, how), Decimal(0))


def _adds_up(row: _Row, columns: Sequence[Column], how: Convention) -> bool:
    return abs(_computed(row, columns, how) - row.values[-1]) <= TOLERANCE


def _rows_hold(table: _Table, total: _Row, how: Convention) -> bool:
    if not all(_adds_up(row, table.columns, how) for row in table.rows):
        return False
    endings = sum((row.values[-1] for row in table.rows), Decimal(0))
    return abs(endings - total.values[-1]) <= TOLERANCE


def _check(table: _Table, total: _Row) -> tuple[InvestmentReconciliation, Convention]:
    holding = [how for how in _CONVENTIONS if _adds_up(total, table.columns, how)]
    how: Convention = next(
        (h for h in holding if _rows_hold(table, total, h)), holding[0] if holding else "printed"
    )
    failed = [
        *([] if holding else ["roll_forward"]),
        *([] if _rows_hold(table, total, how) else ["roll_forward_rows"]),
    ]
    return (
        InvestmentReconciliation(
            status="mismatch" if failed else "ok",
            checks=["roll_forward", "roll_forward_rows"],
            failed=failed,
            total_value=_computed(total, table.columns, how),
            reported_total_value=total.values[-1],
            tolerance=TOLERANCE,
        ),
        how,
    )


def _figures(table: _Table, total: _Row, how: Convention) -> list[StatementFigure]:
    amounts = [total.values[0], *_signed(total.values, table.columns, how), total.values[-1]]
    roles: dict[str, Literal["opening", "item", "closing"]] = {
        "beginning": "opening",
        "movement": "item",
        "ending": "closing",
    }
    return [
        StatementFigure(
            group="account_value",
            label=column.label,
            role=roles[column.kind],
            amount=amount,
            page=total.line.page,
            bbox=total.line.bbox,
        )
        for column, amount in zip(table.columns, amounts, strict=True)
    ]


@dataclass(frozen=True)
class _Period:
    start: date
    end: date
    confidence: float
    #: OCR confidence of the lines the label was found on.
    ocr: tuple[float | None, ...]


def _period(view: DocumentView) -> _Period | None:
    """The labelled period; in doubt when its numeric dates read two ways."""
    found = PeriodCandidates()
    ocr: list[float | None] = []
    for line in view.lines:
        before = len(found.texts)
        found.scan(line.text, PERIOD_LABELS)
        if len(found.texts) > before:
            ocr.append(line.conf)
    if not found.texts:
        return None
    readings = [read_period(found.texts, order, view, certain=False) for order in _NUMERIC_ORDERS]
    first = readings[0]
    if first is None or first.source != "label":
        return None
    other = readings[1]
    agreed = other is not None and other.span == first.span
    base = first.confidence(conf.GENERIC) if agreed else conf.GUESSED
    return _Period(first.start, first.end, base, tuple(ocr))


def _institution(view: DocumentView) -> str | None:
    for line in view.lines[:1]:
        text = line.text.strip()
        if any(ch.isalpha() for ch in text) and not any(ch.isdigit() for ch in text):
            return text[:INSTITUTION_MAX]
    return None


def _same(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= TOLERANCE


class RetirementPlanParser(DocumentParser):
    """A plan statement's roll-forward table as the plan's balance (module doc)."""

    name: ClassVar[str] = PARSER_NAME
    version: ClassVar[str] = "1"
    kind: ClassVar[Literal["statement"]] = "statement"

    def detect(self, view: DocumentView) -> float:
        return (
            DETECT_SCORE
            if any(roll_forward_header(line) is not None for line in view.lines)
            else 0.0
        )

    def parse(
        self, view: DocumentView, *, threshold: float, date_order: str | None = None
    ) -> ParseOutcome:
        outcome = ParseOutcome()
        tables = _tables(view)
        if not tables:
            return outcome
        totalled = [(table, table.total) for table in tables if table.total is not None]
        endings: list[Decimal] = []
        for _, total in totalled:
            if not any(_same(total.values[-1], known) for known in endings):
                endings.append(total.values[-1])
        if len(endings) > 1:
            # Several plans or participants: which balance is whose is not guessed.
            outcome.warnings.append(SEVERAL_ACCOUNTS)
            return outcome
        period = _period(view)
        scores: dict[str, float] = {}
        ocr: dict[str, list[float | None]] = {}
        if period is not None:
            scores["period"] = period.confidence
            ocr["period"] = list(period.ocr)
        account = StatementAccount(account_type="retirement", institution=_institution(view))
        ref = ParserRef(name=self.name, version=self.version, institution=None, kind=self.kind)
        if not totalled:
            outcome.warnings.append(NO_TOTAL)
            outcome.investments.append(
                InvestmentStatement(
                    source_format=view.source_format,
                    account=account,
                    period_start=period.start if period else None,
                    period_end=period.end if period else None,
                    as_of=period.end if period else None,
                    truncated=view.truncated,
                    first_page=tables[0].header.page,
                    last_page=tables[0].header.page,
                    parser=ref,
                    confidence=conf.scaled_fields(scores, ocr),
                    reconciliation=InvestmentReconciliation(
                        status="unavailable", tolerance=TOLERANCE
                    ),
                )
            )
            return outcome
        table, total = totalled[0]
        check, how = _check(table, total)
        scores["total_value"] = conf.GENERIC if check.status == "ok" else conf.UNRECONCILED
        ocr["total_value"] = [table.header.conf, total.line.conf]
        currency = symbol_currency(
            [table.header.text, total.line.text, *(row.line.text for row in table.rows)]
        )
        outcome.skipped_lines = len(table.unread)
        outcome.skipped_regions = [(line.page, line.bbox) for line in table.unread[:MAX_REGIONS]]
        outcome.investments.append(
            InvestmentStatement(
                source_format=view.source_format,
                account=account.model_copy(update={"currency": currency}),
                period_start=period.start if period else None,
                period_end=period.end if period else None,
                as_of=period.end if period else None,
                total_value=Balance(amount=total.values[-1], as_of=period.end if period else None),
                truncated=view.truncated,
                figures=_figures(table, total, how),
                first_page=table.header.page,
                last_page=total.line.page,
                parser=ref,
                confidence=conf.scaled_fields(
                    scores, ocr, confirmed={"total_value"} if check.status == "ok" else ()
                ),
                reconciliation=check,
            )
        )
        return outcome

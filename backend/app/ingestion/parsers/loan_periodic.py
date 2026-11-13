# SPDX-License-Identifier: AGPL-3.0-or-later
"""The periodic mortgage statement: figures read by section and by column.

The statement a servicer sends each month prints the same labels three
times: ``Principal``, ``Interest`` and ``Escrow`` of the **next** payment
(*Explanation of Amount Due*), of what was paid **last month** and of what
was paid in the **year to date** (*Past Payments Breakdown*, two columns).
Only last month's column is a payment made. A line reader that takes the
last amount after a label stores the year-to-date figure as a payment, so
this reader works on cells (:mod:`app.ingestion.parsers.labelled_cells`):

1. **Stubs**: from a line saying to detach the coupon to the end of its
   page nothing is read (:data:`~app.ingestion.parsers.table_statement.STUB_MARKERS`).
2. **Sections**: a heading cell (:data:`HEADINGS`) starts a section that
   owns the labels below it from its left edge to the next heading on its
   line (or, with no other heading there, to the first other cell on its
   line: blocks printed side by side), down to the next heading in that
   range. A line naming a year-to-date column starts (or heads) the *paid*
   section; a ``Date | Description | Payments`` header the *activity* one.
   Labels outside every section are *account* labels. Sections do not carry
   over to the next page.
3. **Labels** (the whole cell, :func:`~app.ingestion.parsers.labelled_cells.label_pairs`):
   in *due* the parts, the regular payment and the total amount due go to
   :attr:`LoanRead.due`, never to the payment; in *paid* the parts and the
   total are the payment (:attr:`LoanRead.money`), each from the period's
   column. A year-to-date column is never read. A header naming it and
   exactly one other column makes that other the period's whatever its
   words; a paid heading with one period header under it (``Paid Last
   Month`` alone) has that one column. With more than one unnamed column,
   or with no header saying what the figures are, nothing is read and the
   warning is ``loan_columns_unknown``. In *account*: the outstanding principal
   (``balance``), the previous one and the interest rate.
4. **Labels over or under their value**: an account or due label with no
   amount on its line takes the cell under it, else the one over it.
5. **Dates**: the statement date, the next due date (``Payment date`` is
   the due date in this layout) and, when a label says it, the day the last
   payment was received; after the label or under it.
6. **The day paid**, when no label gave it, is a row of the activity table
   whose description says ``payment`` (and not late, fee, due, reversal,
   returned) and whose amount stands in the payments column. With the
   payment's total known: the latest row of that amount, else, when all
   payment rows add up to it, the latest of them, else nothing and the
   warning ``loan_payment_row_mismatch``. With no total: the latest row.
7. **Nothing paid**: principal, interest and total of the period all zero.
   No day paid is looked for, a labelled one is dropped (it belongs to an
   earlier period) and the warning is ``loan_no_payment_in_period``.

Nothing is guessed: a label is read only as the whole of its cell, the
first reading of a field stands, and a figure whose column cannot be told
stays unread. :mod:`app.ingestion.parsers.loan_statement` checks what is
read here and scores it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final, Literal

from app.core.money import sum_amounts
from app.ingestion.parsers.base import Cell, DocumentView, ViewLine
from app.ingestion.parsers.labelled_cells import (
    CURRENT_HEADERS,
    CURRENT_PREFIXES,
    PeriodColumn,
    amount_of,
    current_cells,
    has_letters,
    label_pairs,
    period_columns,
    value_over,
    value_under,
)
from app.ingestion.parsers.table_statement import STUB_MARKERS, label_end
from app.ingestion.parsers.text import DateStyle, find_amounts, find_dates, normalise
from app.ingestion.reconcile import TOLERANCE

Kind = Literal["account", "due", "paid", "activity"]

#: Section headings (normalised, the whole cell) and the section each starts.
HEADINGS: Final[dict[str, Kind]] = {
    "explanation of amount due": "due",
    "explanation of payment amount": "due",
    "past payments breakdown": "paid",
    "transaction activity": "activity",
    "account activity": "activity",
    "account information": "account",
    "loan information": "account",
}
#: The parts of a payment, in both the due and the paid section.
_PARTS: Final[dict[str, str]] = {
    "principal": "principal",
    "interest": "interest",
    "escrow": "escrow",
    "escrow taxes and insurance": "escrow",
    "escrow for taxes and insurance": "escrow",
    "escrow taxes insurance": "escrow",
    "fees": "fees",
    "fees charges": "fees",
    "fees and charges": "fees",
    "total fees charged": "fees",
    "late charges": "fees",
    "late charge": "fees",
}
_DUE_LABELS: Final[dict[str, str]] = {
    **_PARTS,
    "regular monthly payment": "regular",
    "total amount due": "total",
    "amount due": "total",
}
_PAID_LABELS: Final[dict[str, str]] = {**_PARTS, "total": "total", "total paid": "total"}
_ACCOUNT_LABELS: Final[dict[str, str]] = {
    "outstanding principal": "balance",
    "outstanding principal balance": "balance",
    "principal balance": "balance",
    "current principal balance": "balance",
    "unpaid principal": "balance",
    "unpaid principal balance": "balance",
    "new principal balance": "balance",
    "previous principal balance": "previous",
}
#: Date labels (a cell starts with one), the longest first.
_DATE_LABELS: Final[tuple[tuple[str, str], ...]] = (
    ("date last payment received", "paid_on"),
    ("last payment received", "paid_on"),
    ("payment received on", "paid_on"),
    ("statement date", "statement_date"),
    ("statement ending", "statement_date"),
    ("next payment due", "next_due"),
    ("payment due date", "next_due"),
    ("payment date", "next_due"),
    ("due date", "next_due"),
)
_NUMBER_LABELS: Final = ("loan number", "loan no", "account number", "account no")
#: Money columns an activity table may name; a payment stands in the first two.
_ACTIVITY_COLUMNS: Final[tuple[str, ...]] = ("payments", "amount", "charges")
#: Words in a row's description that say it is not a payment received.
_NOT_A_PAYMENT: Final = ("late", "fee", "due", "reversal", "reversed", "returned")
_MONTH_START: Final = re.compile(
    r"(?i)(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?$"
)

#: A percentage. Digits that follow a digit and ``,`` or ``.`` are the tail of
#: a number OCR split (``4, 75%``), never a rate of their own.
PERCENT: Final = re.compile(r"(?<!\d)(?<!\d[.,])(?<!\d[.,]\s)(\d{1,2}(?:\.\d{1,6})?)\s*%")
#: The last four digits of a masked number (``ending in 1234``, ``XXXX1234``).
MASKED_LAST4: Final = re.compile(
    r"(?:ending(?:\s+in)?\s*:?\s*|[xX*•]{2,}[\s-]?)([0-9]{4})\b(?![0-9])"
)
_RUN: Final = r"\d(?:[ -]?\d){5,}"
#: A loan or account number printed in full: six or more digits after its label.
FULL_NUMBER: Final = re.compile(
    rf"(?:loan|account)\s+(?:number|no)\b\.?\s*[:#]?\s*({_RUN})(?![\d/.,-]|\s\d)",
    re.IGNORECASE,
)
_WHOLE_RUN: Final = re.compile(_RUN)
LENDER_LINE: Final = re.compile(r"^\s*lender\s*:\s*(.*)$", re.IGNORECASE)
_EVERYWHERE: Final = float("inf")


@dataclass
class LoanRead:
    """What a reader found on a loan statement, before any check."""

    #: The payment made and the balances (``loan_statement.MONEY_FIELDS``).
    money: dict[str, Decimal] = field(default_factory=dict)
    #: The next payment: ``principal``, ``interest``, ``escrow``, ``fees``,
    #: ``regular``, ``total``. Used for a check; never stored as a payment.
    due: dict[str, Decimal] = field(default_factory=dict)
    #: ``statement_date``, ``paid_on``, ``next_due``.
    dates: dict[str, date] = field(default_factory=dict)
    rate: Decimal | None = None
    #: The lender a ``Lender:`` line names (the caller knows the first-line rule).
    lender: str | None = None
    last4: str | None = None
    next_due_amount: Decimal | None = None
    #: Currencies of the amounts' symbols ($ is USD).
    symbols: set[str] = field(default_factory=set)
    #: Per field, the cells its label and value were read from.
    cells: dict[str, list[Cell]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def read_rate(text: str) -> Decimal | None:
    """The percentage in ``text`` as a fraction (:data:`PERCENT`), or None."""
    match = PERCENT.search(text)
    return Decimal(match[1]) / 100 if match is not None else None


def read_last4(text: str) -> str | None:
    """The last four digits of the loan number ``text`` prints, never more.

    Masked (:data:`MASKED_LAST4`), or in full after its label
    (:data:`FULL_NUMBER`; a run that goes on into another figure is not read).
    """
    masked = MASKED_LAST4.search(text)
    if masked is not None:
        return masked[1]
    full = FULL_NUMBER.search(text)
    if full is None:
        return None
    return "".join(ch for ch in full[1] if ch.isdigit())[-4:]


def named_lender(text: str) -> str | None:
    """The name after ``Lender:`` on a line; the colon is what makes it a label."""
    match = LENDER_LINE.match(text)
    return (match[1].strip()[:200] or None) if match is not None else None


def _heading(cell: Cell) -> Kind | None:
    """The section ``cell`` heads: a heading phrase, alone or with figures after it."""
    norm = normalise(cell.text)
    for phrase, kind in HEADINGS.items():
        if norm == phrase or (
            norm.startswith(f"{phrase} ") and not has_letters(norm[len(phrase) :])
        ):
            return kind
    return None


def _year_to_date_header(line: ViewLine) -> list[PeriodColumn]:
    """The columns ``line`` heads when it names a year-to-date one beside another.

    A header prints no amount and at least two cells of words (``Principal
    paid year to date 2,444.91`` is a label and its figure; the same words
    alone on a line are a label whose figure stands elsewhere).
    """
    if find_amounts(line.text):
        return []
    columns = period_columns(line)
    if not any(column.kind == "ytd" for column in columns):
        return []
    words = [c for c in line.cells if has_letters(c.text) and _heading(c) is None]
    return columns if len(words) >= 2 or len(columns) >= 2 else []


def is_periodic(view: DocumentView) -> bool:
    """``view`` is a periodic statement (module doc), on any page.

    A line heads a year-to-date column beside another column, or a cell is
    one of the headings only this layout prints (those of the *due* and the
    *paid* section).
    """
    for line in view.lines:
        if _year_to_date_header(line):
            return True
        if any(_heading(cell) in ("due", "paid") for cell in line.cells):
            return True
    return False


@dataclass
class _Region:
    """A span of the page's width and the section its labels belong to."""

    x0: float
    x1: float
    kind: Kind
    #: Paid: the columns its header named (None: no header seen).
    columns: list[PeriodColumn] | None = None
    #: Paid: a header was seen and the period's column could not be told.
    unknown: bool = False
    #: Activity: the money columns its header named, by word.
    table: dict[str, tuple[float, float]] | None = None


class _Regions:
    """The sections of a page, side by side: a partition of its width."""

    def __init__(self) -> None:
        self.spans: list[_Region] = [_Region(-_EVERYWHERE, _EVERYWHERE, "account")]

    def at(self, x: float, slack: float) -> _Region:
        """The region a cell starting at ``x`` stands in (``slack``: a label
        may start a little left of its heading)."""
        for span in self.spans:
            if span.x0 - slack <= x < span.x1 - slack:
                return span
        return self.spans[-1]  # pragma: no cover - the spans cover the width

    def start(self, x0: float, x1: float, kind: Kind) -> _Region:
        """A new region over ``[x0, x1)``; what stood there ends, the rest stays."""
        new = _Region(x0, x1, kind)
        kept: list[_Region] = []
        for span in self.spans:
            if span.x0 < x0:
                kept.append(
                    _Region(
                        span.x0, min(span.x1, x0), span.kind, span.columns, span.unknown, span.table
                    )
                )
            if span.x1 > x1:
                kept.append(
                    _Region(
                        max(span.x0, x1), span.x1, span.kind, span.columns, span.unknown, span.table
                    )
                )
        self.spans = sorted([*kept, new], key=lambda span: span.x0)
        return new


@dataclass(frozen=True)
class _PaymentRow:
    day: date
    amount: Decimal
    cells: tuple[Cell, ...]


def _stub_marker(line: ViewLine) -> bool:
    """A line saying to detach the coupon: without an amount, or starting with the marker."""
    norm = f" {line.norm} "
    found = [marker for marker in STUB_MARKERS if f" {marker} " in norm]
    if not found:
        return False
    return not find_amounts(line.text) or any(norm.startswith(f" {m} ") for m in found)


def _without_stubs(lines: Sequence[ViewLine]) -> list[ViewLine]:
    out: list[ViewLine] = []
    stub_page: int | None = None
    for line in lines:
        if stub_page == line.page:
            continue
        if _stub_marker(line):
            stub_page = line.page
            continue
        out.append(line)
    return out


def _part(line: ViewLine, cells: Sequence[Cell]) -> ViewLine:
    return ViewLine(
        line.page, line.index, tuple(cells), " ".join(c.text for c in cells), line.height
    )


def _starts_with_date(cell: Cell, styles: tuple[DateStyle, ...]) -> date | None:
    """The date ``cell`` starts with (a table row's first cell), else None."""
    tokens = cell.text.split()
    if not tokens or not (tokens[0][0].isdigit() or _MONTH_START.fullmatch(tokens[0])):
        return None
    for size in (3, 2, 1):
        found = find_dates(" ".join(tokens[:size]), styles)
        if found:
            return found[0]
    return None


def _activity_header(line: ViewLine) -> tuple[float, dict[str, tuple[float, float]]] | None:
    """An activity table header (``Date | Description | Payments``): where its
    ``Date`` column starts and the money columns it names."""
    words = [word for cell in line.cells for word in cell.word_cells()]
    names = {normalise(word.text): (word.x0, word.x1) for word in words}
    if "date" not in names or "description" not in names or find_amounts(line.text):
        return None
    table = {name: names[name] for name in _ACTIVITY_COLUMNS if name in names}
    return (names["date"][0], table) if table else None


def _nearest(cell: Cell, table: dict[str, tuple[float, float]]) -> str:
    """The column of ``table`` that ``cell`` stands in: the smallest gap, then the centres."""

    def distance(name: str) -> tuple[float, float]:
        x0, x1 = table[name]
        return max(0.0, max(cell.x0, x0) - min(cell.x1, x1)), abs(cell.centre - (x0 + x1) / 2)

    return min(table, key=distance)


def _payment_row(
    cells: Sequence[Cell], line: ViewLine, region: _Region, styles: tuple[DateStyle, ...]
) -> _PaymentRow | None:
    """The payment received that a table row prints, else None.

    The description says ``payment`` and nothing that makes it a charge or
    a correction; the amount stands in the payments column (``amount`` when
    the table has no such column), not in the charges one.
    """
    table = region.table or {}
    day = _starts_with_date(cells[0], styles)
    wanted = "payments" if "payments" in table else "amount"
    if day is None or wanted not in table:
        return None
    pairs = label_pairs(_part(line, cells))
    words = normalise(" ".join(pair.label.text for pair in pairs)).split()
    if not any(word.startswith("payment") for word in words):
        return None
    if any(word.startswith(_NOT_A_PAYMENT) for word in words):
        return None
    for cell in (cell for pair in pairs for cell in pair.amounts):
        amount = amount_of(cell)
        if amount is not None and _nearest(cell, table) == wanted:
            return _PaymentRow(day, abs(amount.value), tuple(cells))
    return None


def _after(line: ViewLine, label: Cell) -> list[Cell]:
    """The cells of ``line`` printed after ``label``, left to right."""
    return sorted((c for c in line.cells if c.x0 >= label.x1), key=lambda c: c.x0)


def _is_date_cell(cell: Cell, styles: tuple[DateStyle, ...]) -> bool:
    """``cell`` is a value and no label: no letters, or a date in words (``March 20, 2026``)."""
    if not has_letters(cell.text):
        return True
    return len(cell.text.split()) <= 3 and bool(find_dates(cell.text, styles))


class _Reader:
    def __init__(self, view: DocumentView, styles: tuple[DateStyle, ...]) -> None:
        self.styles = styles
        self.lines = _without_stubs(view.lines)
        self.read = LoanRead()
        self.regions = _Regions()
        self.rows: list[_PaymentRow] = []

    def warn(self, code: str) -> None:
        if code not in self.read.warnings:
            self.read.warnings.append(code)

    # -- sections ---------------------------------------------------------------

    def _headings(self, line: ViewLine, columns: list[PeriodColumn]) -> set[int]:
        """Start the sections ``line`` heads; the ids of its heading cells."""
        found = sorted(
            ((cell, kind) for cell in line.cells if (kind := _heading(cell)) is not None),
            key=lambda item: item[0].x0,
        )
        heads = {id(cell) for cell, _ in found}
        for at, (cell, kind) in enumerate(found):
            x1 = _EVERYWHERE
            if at + 1 < len(found):
                x1 = found[at + 1][0].x0
            elif not columns:
                # Blocks side by side: a cell after the heading on its line that
                # stands in another section is that block's, which goes on there.
                beside = [
                    c.x0
                    for c in line.cells
                    if id(c) not in heads
                    and c.x0 > cell.x1
                    and self.regions.at(c.x0, line.height / 2).kind != "account"
                ]
                x1 = min(beside, default=_EVERYWHERE)
            self.regions.start(cell.x0, x1, kind)
        return heads

    def _paid_header(self, line: ViewLine, columns: list[PeriodColumn], heads: set[int]) -> None:
        """``line`` names a year-to-date column: the section under it is the paid one."""
        slack = line.height / 2
        region = self.regions.at(columns[0].x0, slack)
        region.kind, region.table = "paid", None
        inside = [c for c in columns if self.regions.at(c.x0, slack) is region]
        named = [c for c in inside if c.kind == "current"]
        others = [
            cell
            for cell in line.cells
            if id(cell) not in heads
            and has_letters(cell.text)
            and self.regions.at(cell.x0, slack) is region
            and not any(cell.overlap(c.x0, c.x1) > 0 for c in columns)
        ]
        region.unknown = False
        if named:
            region.columns = inside
        elif not named and len(others) == 1:
            region.columns = sorted(
                [*inside, PeriodColumn("current", others[0].x0, others[0].x1)],
                key=lambda column: column.x0,
            )
        else:
            region.columns, region.unknown = None, True
            self.warn("loan_columns_unknown")

    def _period_header(self, line: ViewLine, heads: set[int]) -> None:
        """A paid section with no columns yet takes the one period column
        ``line`` names (``Paid Last Month`` alone over a single column)."""
        if find_amounts(line.text):
            return
        for cell in line.cells:
            norm = normalise(cell.text)
            if id(cell) in heads or not (
                norm in CURRENT_HEADERS or norm.startswith(CURRENT_PREFIXES)
            ):
                continue
            region = self.regions.at(cell.x0, line.height / 2)
            if region.kind == "paid" and region.columns is None and not region.unknown:
                region.columns = [PeriodColumn("current", cell.x0, cell.x1)]

    # -- labels -----------------------------------------------------------------

    def _keep(self, name: str, cells: list[Cell]) -> None:
        self.read.cells[name] = cells

    def _value(self, at: int, label: Cell, amounts: Sequence[Cell]) -> Cell | None:
        """The one amount of a label: after it, else under it, else over it."""
        if len(amounts) == 1:
            return amounts[0]
        if amounts:
            return None
        for cell in (value_under(self.lines, at, label), value_over(self.lines, at, label)):
            if cell is not None and amount_of(cell) is not None:
                return cell
        return None

    def _money(self, target: dict[str, Decimal], name: str, cell: Cell) -> bool:
        amount = amount_of(cell)
        if amount is None or name in target:
            return False
        target[name] = abs(amount.value)
        if amount.currency:
            self.read.symbols.add(amount.currency)
        return True

    def _dates(self, at: int, line: ViewLine, label: Cell) -> None:
        norm = normalise(label.text)
        for phrase, key in _DATE_LABELS:
            if not (norm == phrase or norm.startswith(f"{phrase} ")):
                continue
            if key in self.read.dates:
                return
            end = label_end(label.text, phrase) or 0
            texts = [label.text[end:]]
            cells = [label]
            for cell in _after(line, label):
                if not _is_date_cell(cell, self.styles):
                    break
                texts.append(cell.text)
                cells.append(cell)
            found = find_dates(" ".join(texts), self.styles)
            if not found:
                under = value_under(self.lines, at, label)
                found = find_dates(under.text, self.styles) if under is not None else []
                cells = [label, under] if under is not None else cells
            if found:
                self.read.dates[key] = found[0]
                self._keep(key, cells)
            return

    def _rate(self, at: int, line: ViewLine, label: Cell) -> None:
        norm = normalise(label.text)
        if self.read.rate is not None or not (
            norm == "interest rate" or norm.startswith("interest rate ")
        ):
            return
        beside = [c for c in _after(line, label)[:1] if not has_letters(c.text)]
        cells = [label, *beside]
        rate = read_rate(" ".join(cell.text for cell in cells))
        if rate is None and not beside:
            under = value_under(self.lines, at, label)
            if under is not None:
                rate, cells = read_rate(under.text), [label, under]
        if rate is not None:
            self.read.rate = rate
            self._keep("rate", cells)

    def _number(self, at: int, line: ViewLine, label: Cell) -> None:
        norm = normalise(label.text)
        if self.read.last4 is not None or not any(
            norm == name or norm.startswith(f"{name} ") for name in _NUMBER_LABELS
        ):
            return
        beside = [
            c
            for c in _after(line, label)[:1]
            if not has_letters(c.text) or MASKED_LAST4.search(c.text) is not None
        ]
        cells = [label, *beside]
        last4 = read_last4(" ".join(cell.text for cell in cells))
        if last4 is None and not beside and norm in _NUMBER_LABELS:
            under = value_under(self.lines, at, label)
            text = under.text.strip() if under is not None else ""
            masked = MASKED_LAST4.search(text)
            if masked is not None:
                last4 = masked[1]
            elif _WHOLE_RUN.fullmatch(text):
                last4 = "".join(ch for ch in text if ch.isdigit())[-4:]
            if under is not None:
                cells = [label, under]
        if last4 is not None:
            self.read.last4 = last4
            self._keep("last4", cells)

    def _label(self, at: int, line: ViewLine, label: Cell, amounts: Sequence[Cell]) -> None:
        region = self.regions.at(label.x0, line.height / 2)
        if region.kind == "activity":
            return
        self._dates(at, line, label)
        self._rate(at, line, label)
        self._number(at, line, label)
        norm = normalise(label.text)
        read = self.read
        if region.kind == "due":
            name = _DUE_LABELS.get(norm)
            cell = self._value(at, label, amounts) if name is not None else None
            if name is not None and cell is not None and self._money(read.due, name, cell):
                self._keep(f"due_{name}", [label, cell])
        elif region.kind == "account":
            name = _ACCOUNT_LABELS.get(norm)
            cell = self._value(at, label, amounts) if name is not None else None
            if name is not None and cell is not None and self._money(read.money, name, cell):
                self._keep(name, [label, cell])
        else:
            name = _PAID_LABELS.get(norm)
            if name is None or region.unknown or not amounts:
                return
            if region.columns is None:
                # No header says what the figures are: one of them may be the year's.
                self.warn("loan_columns_unknown")
                return
            chosen = current_cells(amounts, region.columns)
            if chosen and self._money(read.money, name, chosen[0]):
                self._keep(name, [label, chosen[0]])

    # -- the page ---------------------------------------------------------------

    def run(self) -> LoanRead:
        page: int | None = None
        for at, line in enumerate(self.lines):
            if line.page != page:
                page, self.regions = line.page, _Regions()
            lender = named_lender(line.text)
            if lender is not None:
                self.read.lender = lender
                self._keep("lender", list(line.cells))
            columns = _year_to_date_header(line)
            heads = self._headings(line, columns)
            if columns:
                self._paid_header(line, columns, heads)
                continue
            slack = line.height / 2
            self._period_header(line, heads)
            header = _activity_header(line)
            if header is not None:
                region = self.regions.at(header[0], slack)
                region.kind, region.table = "activity", header[1]
                region.columns, region.unknown = None, False
                continue
            taken: set[int] = set()
            for region in self.regions.spans:
                if region.kind != "activity" or region.table is None:
                    continue
                inside = [c for c in line.cells if self.regions.at(c.x0, slack) is region]
                if not inside or _starts_with_date(inside[0], self.styles) is None:
                    continue
                taken.update(id(cell) for cell in inside)
                row = _payment_row(inside, line, region, self.styles)
                if row is not None:
                    self.rows.append(row)
            rest = [c for c in line.cells if id(c) not in taken and id(c) not in heads]
            if not rest:
                continue
            for pair in label_pairs(_part(line, rest)):
                self._label(at, line, pair.label, pair.amounts)
        self._finish()
        return self.read

    def _finish(self) -> None:
        read = self.read
        read.next_due_amount = read.due.get("total")
        money = read.money
        if (
            "principal" in money
            and "interest" in money
            and not any(money.get(name) for name in ("principal", "interest", "total"))
        ):
            read.dates.pop("paid_on", None)
            read.cells.pop("paid_on", None)
            self.warn("loan_no_payment_in_period")
            return
        if "paid_on" in read.dates or not self.rows:
            return
        rows = sorted(self.rows, key=lambda row: row.day)
        total = money.get("total")
        chosen: list[_PaymentRow]
        if total is None:
            chosen = rows[-1:]
        else:
            chosen = [row for row in rows if abs(row.amount - total) <= TOLERANCE][-1:]
            if not chosen and abs(sum_amounts(row.amount for row in rows) - total) <= TOLERANCE:
                chosen = rows
        if not chosen:
            self.warn("loan_payment_row_mismatch")
            return
        read.dates["paid_on"] = chosen[-1].day
        self._keep("paid_on", [cell for row in chosen for cell in row.cells])


def read_periodic(view: DocumentView, styles: tuple[DateStyle, ...]) -> LoanRead:
    """Read a periodic statement (module doc); numeric dates in ``styles``."""
    return _Reader(view, styles).run()

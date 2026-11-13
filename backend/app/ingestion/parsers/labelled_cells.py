# SPDX-License-Identifier: AGPL-3.0-or-later
"""Labels and the figures beside, under or over them (payslips, loan statements).

A labelled document prints a figure after its label (``Net Pay 1,808.40``),
often twice: once for the period and once for the year so far, under a
header line naming the two columns. The helpers here work on a view's cells
(their words and boxes), so a reader can say which column a figure stands
in, and take the period's:

* :func:`label_pairs`: a line as labels, each with the amounts that follow
  it. A label and its amount inside one cell are split by the cell's words.
* :func:`period_columns`: the header cells of a line that name a column of
  the period (``Current``, ``This Period``, ``Paid Last Month``) or of the
  year so far (``YTD``, ``Year to Date``). One lone ``Amount`` or
  ``Current`` is no header: a line counts only with a year-to-date cell or
  two header cells.
* :func:`current_cells`: the amounts standing in a period column. With no
  period column named, those not in the year-to-date column; with no
  columns at all, every amount (the caller reads the line as it did).
* :func:`value_under` / :func:`value_over`: the value printed under (over)
  a label on the neighbouring line of the same page.

Nothing here decides what a figure means: the caller matches the label
against its own vocabulary and checks the figures against each other. A
year-to-date figure is never offered as a period's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from app.ingestion.parsers.base import Cell, ViewLine, join_cells
from app.ingestion.parsers.text import Amount, find_amounts, normalise, read_amount

_SIGNS: Final = frozenset("-+\u2212")
_SYMBOLS: Final = frozenset("$\u00a3\u20ac")
#: Markers printed after a figure (credit, debit, debit).
_MARKERS: Final = ("CR", "DR", "DB")
_TRAILING: Final = ".,;:"

#: Header cells naming the period's column (normalised, the whole cell).
CURRENT_HEADERS: Final = (
    "current",
    "this period",
    "current period",
    "this pay period",
    "amount",
    "paid last month",
    "last month",
    "last payment",
    "paid this period",
)
#: A header cell starting with one of these names the period too (``Paid since 02/20/2026``).
CURRENT_PREFIXES: Final = ("paid since", "since last")
#: Words naming the year-to-date column, anywhere in a header cell.
YTD_PHRASES: Final = ("year to date", "ytd", "y t d")
#: ``Amount`` after another header is part of it (``YTD Amount`` is one column).
_SPLIT_CURRENT: Final = tuple(header for header in CURRENT_HEADERS if header != "amount")
#: The longest header phrase, in words (for splitting two headers in one cell).
_MAX_HEADER_WORDS: Final = 4


def has_letters(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def is_year_to_date(text: str) -> bool:
    """``text`` names the year so far (``YTD``, ``Year-to-date``, ``Y-T-D``)."""
    norm = f" {normalise(text)} "
    return any(f" {phrase} " in norm for phrase in YTD_PHRASES)


def amount_of(cell: Cell) -> Amount | None:
    """The amount ``cell`` is (the whole cell; punctuation after it ignored), or None."""
    return read_amount(cell.text.strip().rstrip(_TRAILING))


@dataclass(frozen=True)
class LabelPair:
    """A label and the amount cells printed after it, up to the next label."""

    label: Cell
    amounts: tuple[Cell, ...]


def _figure(words: Sequence[Cell], index: int) -> tuple[Cell, int] | None:
    """The amount starting at ``words[index]`` and the words it takes, or None.

    The joining rules are :func:`app.ingestion.parsers.text.find_amounts`'s:
    a lone currency symbol (with or without its sign) takes the word after
    it, a lone sign takes the word after it only when that starts with a
    symbol (``900.00 - 150.00`` is two positive figures), and ``CR``, ``DR``
    or ``DB`` after a figure belongs to it.
    """
    word = words[index].text
    following = words[index + 1].text if index + 1 < len(words) else ""
    size = 1
    if following and (
        word in _SYMBOLS or (len(word) == 2 and word[0] in _SIGNS and word[1] in _SYMBOLS)
    ):
        size = 2
    elif word in _SIGNS and following[:1] in _SYMBOLS:
        size = 2 if following not in _SYMBOLS else 3
    if index + size > len(words):
        return None
    marker = words[index + size].text if index + size < len(words) else ""
    if marker.upper() in _MARKERS:
        marked = join_cells(words[index : index + size + 1])
        if amount_of(marked) is not None:
            return marked, size + 1
    figure = join_cells(words[index : index + size])
    return (figure, size) if amount_of(figure) is not None else None


def label_pairs(line: ViewLine) -> list[LabelPair]:
    """The labels of ``line``, each with the amount cells that follow it.

    A label is a cell holding letters and no amount; the amount cells after
    it, up to the next label, are its figures. A cell holding both
    (``Net Pay 1,808.40``) is split by its words. Amounts before the first
    label belong to no pair; other cells (a date, a count) are skipped.
    """
    pairs: list[LabelPair] = []
    label: Cell | None = None
    amounts: list[Cell] = []

    def start(cell: Cell) -> None:
        nonlocal label, amounts
        if label is not None:
            pairs.append(LabelPair(label, tuple(amounts)))
        label, amounts = cell, []

    for cell in line.cells:
        if amount_of(cell) is not None:
            if label is not None:
                amounts.append(cell)
            continue
        if has_letters(cell.text) and not find_amounts(cell.text):
            start(cell)
            continue
        words = cell.word_cells()
        pending: list[Cell] = []
        index = 0
        while index < len(words):
            figure = _figure(words, index)
            if figure is None:
                pending.append(words[index])
                index += 1
                continue
            if any(has_letters(word.text) for word in pending):
                start(join_cells(pending))
            pending = []
            if label is not None:
                amounts.append(figure[0])
            index += figure[1]
        if any(has_letters(word.text) for word in pending):
            start(join_cells(pending))
    if label is not None:
        pairs.append(LabelPair(label, tuple(amounts)))
    return pairs


@dataclass(frozen=True)
class PeriodColumn:
    """A column a header cell names: the period's (``current``) or the year's so far."""

    kind: Literal["current", "ytd"]
    x0: float
    x1: float


def _header_kind(norm: str) -> Literal["current", "ytd"] | None:
    if is_year_to_date(norm):
        return "ytd"
    norm = norm.removesuffix(" amount")
    if norm in CURRENT_HEADERS or norm.startswith(CURRENT_PREFIXES):
        return "current"
    return None


def _split_headers(cell: Cell) -> list[PeriodColumn]:
    """Two or more headers printed in one cell (``Current YTD``), by their words.

    Every word of the cell must belong to a header phrase; else nothing.
    """
    words = [word for word in cell.word_cells() if normalise(word.text)]
    out: list[PeriodColumn] = []
    index = 0
    while index < len(words):
        for size in range(min(_MAX_HEADER_WORDS, len(words) - index), 0, -1):
            chunk = words[index : index + size]
            norm = normalise(" ".join(word.text for word in chunk))
            kind: Literal["current", "ytd"] | None = (
                "ytd" if norm in YTD_PHRASES else ("current" if norm in _SPLIT_CURRENT else None)
            )
            if kind is not None:
                out.append(PeriodColumn(kind, chunk[0].x0, chunk[-1].x1))
                index += size
                break
        else:
            return []
    return out if len(out) >= 2 else []


def period_columns(line: ViewLine) -> list[PeriodColumn]:
    """The period and year-to-date columns ``line`` heads, left to right.

    Empty unless the line holds a year-to-date cell or two header cells (a
    lone ``Amount`` heads any table).
    """
    columns: list[PeriodColumn] = []
    for cell in line.cells:
        split = _split_headers(cell)
        if split:
            columns.extend(split)
            continue
        kind = _header_kind(normalise(cell.text))
        if kind is not None:
            columns.append(PeriodColumn(kind, cell.x0, cell.x1))
    if len(columns) < 2 and not any(column.kind == "ytd" for column in columns):
        return []
    return sorted(columns, key=lambda column: column.x0)


def _distance(cell: Cell, column: PeriodColumn) -> tuple[float, float]:
    """How far ``cell`` stands from ``column``: the gap between them, then their centres."""
    gap = max(0.0, max(cell.x0, column.x0) - min(cell.x1, column.x1))
    return gap, abs(cell.centre - (column.x0 + column.x1) / 2)


def current_cells(amounts: Sequence[Cell], columns: Sequence[PeriodColumn]) -> tuple[Cell, ...]:
    """The cells of ``amounts`` that stand in a period column of ``columns``.

    With a ``current`` column: the amount nearest each one. With only
    year-to-date columns: the amounts that are not the nearest to one. With
    no columns: all of them. Each column takes the amount nearest it; when
    two columns would take the same amount, the amounts are matched to the
    columns in order if there are as many of each (figures printed off
    their columns), else each amount goes to the column nearest it (a line
    with one figure: under ``YTD`` it is no period figure).
    """
    amounts = tuple(amounts)
    if not columns or not amounts:
        return amounts
    ordered = sorted(columns, key=lambda column: column.x0)
    taken = [min(amounts, key=lambda cell: _distance(cell, column)) for column in ordered]
    if len({id(cell) for cell in taken}) != len(taken):
        if len(amounts) == len(ordered):
            taken = sorted(amounts, key=lambda cell: cell.x0)
        else:
            nearest = [min(ordered, key=lambda column: _distance(cell, column)) for cell in amounts]
            has_current = any(column.kind == "current" for column in ordered)
            return tuple(
                cell
                for cell, column in zip(amounts, nearest, strict=True)
                if (column.kind == "current" if has_current else column.kind != "ytd")
            )
    current = [
        id(cell) for cell, column in zip(taken, ordered, strict=True) if column.kind == "current"
    ]
    if current:
        return tuple(cell for cell in amounts if id(cell) in current)
    ytd = {id(cell) for cell in taken}
    return tuple(cell for cell in amounts if id(cell) not in ytd)


def _neighbour(
    lines: Sequence[ViewLine], index: int, label: Cell, step: int, text: bool
) -> Cell | None:
    at = index + step
    if not (0 <= index < len(lines) and 0 <= at < len(lines)):
        return None
    if lines[at].page != lines[index].page:
        return None
    best: Cell | None = None
    for cell in lines[at].cells:
        if cell.overlap(label.x0, label.x1) > (best.overlap(label.x0, label.x1) if best else 0.0):
            best = cell
    if best is None:
        return None
    if text:
        return best
    if amount_of(best) is not None:
        return best
    if has_letters(best.text) or not any(ch.isdigit() for ch in best.text):
        return None
    return best


def value_under(
    lines: Sequence[ViewLine], index: int, label: Cell, *, text: bool = False
) -> Cell | None:
    """The value printed under ``label``, a cell of ``lines[index]``.

    The cell of the next line (same page) with the widest overlap with the
    label, when it holds an amount or digits and no letters (an amount, a
    numeric date). ``text``: any cell there, words included (a name under a
    heading); the caller says what it accepts.
    """
    return _neighbour(lines, index, label, 1, text)


def value_over(
    lines: Sequence[ViewLine], index: int, label: Cell, *, text: bool = False
) -> Cell | None:
    """:func:`value_under` for the previous line (a balance printed above its label)."""
    return _neighbour(lines, index, label, -1, text)

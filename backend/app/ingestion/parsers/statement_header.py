# SPDX-License-Identifier: AGPL-3.0-or-later
"""The header of a statement's transaction table: which line, which columns.

Pure functions on a :class:`~app.ingestion.parsers.base.ViewLine`, used by
:mod:`app.ingestion.parsers.table_statement` (its module doc, "Header",
says what a header is to the reader) and testable on hand-built lines
without a PDF.

* :func:`read_header` gives the columns a line declares (a :class:`Column`
  per heading it knows, with the heading's horizontal span), or None when
  the line is no header of a table the reader reads.
* :func:`split_heading_cell` reads two or more headings printed so close
  that they are one cell (``WITHDRAWAL BALANCE``, ``Trans Date Post
  Date``): the fewest known phrases that use every word of the cell, each
  with the span of its own words. A cell with a word left over is not
  split (``Amount Enclosed``, ``Date of Birth``).
* :func:`header_like` says a line is made of heading words only: the header
  of a table the reader does **not** read (a daily-balance table), which
  ends the transaction table above it.
* :func:`stack_headers` is where a header printed over several lines becomes
  one line; today it returns the lines as they are.

How :func:`read_header` works on a line:

1. A cell whose whole text is a heading names its column, as printed.
2. Any other cell of two words or more goes through
   :func:`split_heading_cell`.
3. A second ``date`` heading is the ``post_date`` when the line names none
   (``Transaction Date | Posting Date``): the reader claims its cells and
   does not read them, so a second date never lands in another column.
4. A cell that is still no heading and starts right of the leftmost money
   column is a column of role ``other`` (``Category``, ``Reference``): its
   cells are claimed and not read, so they are never joined to an amount.
   One left of the money is ignored.
5. The line is a header when it names a date, a description or check
   number, and a money column; else it may be a headless header
   (:func:`_headless`).

Steps 2 to 4 are for a line of headings only: they are skipped on a line
that holds an amount or has more than :data:`MAX_HEADER_CELLS` cells, and a
cell of more than :data:`MAX_SPLIT_WORDS` words is never split, so a row,
a total or a sentence that happens to say "date", "description" and
"amount" is no header.

The vocabulary (:data:`HEADERS`) maps a normalised heading to its
:data:`Role`; a parser may pass its own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from app.ingestion.parsers.base import Cell, ViewLine, join_cells
from app.ingestion.parsers.text import find_amounts, normalise

#: What a column holds. ``other`` is a heading that is no column we read
#: (its cells are claimed and not read); ``across`` (a table repeated side
#: by side) is declared and not yet produced by any rule.
Role = Literal[
    "date",
    "post_date",
    "description",
    "amount",
    "debit",
    "credit",
    "balance",
    "check",
    "foreign",
    "lead",
    "other",
    "across",
]
NUMERIC_ROLES: Final = frozenset({"amount", "debit", "credit", "balance"})

#: Normalised header cell text -> column role.
HEADERS: Final[Mapping[str, Role]] = {
    "date": "date",
    "trans date": "date",
    "transaction date": "date",
    "posting date": "date",
    "posted": "date",
    "value date": "date",
    "post date": "post_date",
    "description": "description",
    "details": "description",
    "transaction": "description",
    "transaction details": "description",
    "transaction description": "description",
    "payee": "description",
    "particulars": "description",
    "narrative": "description",
    "merchant": "description",
    "amount": "amount",
    "amount usd": "amount",
    "debit": "debit",
    "debits": "debit",
    "withdrawals": "debit",
    "withdrawal": "debit",
    "money out": "debit",
    "paid out": "debit",
    "credit": "credit",
    "credits": "credit",
    "deposits": "credit",
    "deposit": "credit",
    "money in": "credit",
    "paid in": "credit",
    "balance": "balance",
    "running balance": "balance",
    "date paid": "date",
    "check no": "check",
    "check number": "check",
    "check num": "check",
    "check": "check",
    "chk no": "check",
    "foreign spending": "foreign",
    "foreign currency": "foreign",
    "foreign amount": "foreign",
    "foreign currency amount": "foreign",
    "original currency": "foreign",
    "original amount": "foreign",
}
#: Roles whose cells hold the transaction's money (a table needs one).
MONEY_ROLES: Final = frozenset({"amount", "debit", "credit"})
#: Words of a heading that names a transaction table without date or
#: description headings (``New Transactions for JORDAN SAMPLE``).
TABLE_HEADING_WORDS: Final = frozenset({"transactions", "activity"})


#: A cell of more words than this is never split into headings.
MAX_SPLIT_WORDS: Final = 12
#: A line of more cells than this is read by whole cells only.
MAX_HEADER_CELLS: Final = 12
_DATE_ROLES: Final = frozenset({"date", "post_date"})


@dataclass(frozen=True)
class Column:
    role: Role
    x0: float
    x1: float


def split_heading_cell(
    cell: Cell, headers: Mapping[str, Role] = HEADERS
) -> tuple[tuple[Role, Cell], ...] | None:
    """The headings printed together in ``cell``, each with its own words' span.

    For a cell of 2 to :data:`MAX_SPLIT_WORDS` words that is no heading as a
    whole: the fewest known phrases that use **every** word, in order (of
    two readings as short, the one whose earlier phrase is longer). None
    when a word is left over.
    """
    words = cell.word_cells()
    count = len(words)
    if not 2 <= count <= MAX_SPLIT_WORDS or normalise(cell.text) in headers:
        return None
    roles: dict[tuple[int, int], Role] = {}
    #: Per start word: (phrases needed for the words from there on, end of the first).
    best: list[tuple[int, int] | None] = [None] * count + [(0, count)]
    for start in range(count - 1, -1, -1):
        for end in range(count, start, -1):
            rest = best[end]
            if rest is None or end - start == count:
                continue
            role = headers.get(normalise(" ".join(w.text for w in words[start:end])))
            if role is None:
                continue
            roles[start, end] = role
            known = best[start]
            if known is None or rest[0] + 1 < known[0]:
                best[start] = (rest[0] + 1, end)
    out: list[tuple[Role, Cell]] = []
    start = 0
    while start < count:
        step = best[start]
        if step is None:
            return None
        out.append((roles[start, step[1]], join_cells(words[start : step[1]])))
        start = step[1]
    return tuple(out)


def _plain(line: ViewLine) -> bool:
    """A line that may be read by words: no amount on it and not too many cells."""
    return len(line.cells) <= MAX_HEADER_CELLS and not find_amounts(line.text)


def _headings(
    line: ViewLine, headers: Mapping[str, Role], split: bool
) -> tuple[list[tuple[Role, Cell]], list[Cell]]:
    """``(headings, unmatched cells)`` of ``line``: whole cells first, then
    the headings of the cells that ``split`` lets through
    :func:`split_heading_cell`."""
    whole: list[tuple[Role, Cell]] = []
    parts: list[tuple[Role, Cell]] = []
    unmatched: list[Cell] = []
    for cell in line.cells:
        role = headers.get(normalise(cell.text))
        if role is not None:
            whole.append((role, cell))
            continue
        pieces = split_heading_cell(cell, headers) if split else None
        if pieces is None:
            unmatched.append(cell)
        else:
            parts.extend(pieces)
    return [*whole, *parts], unmatched


def read_header(line: ViewLine, headers: Mapping[str, Role] = HEADERS) -> list[Column] | None:
    """The table columns ``line`` declares, or None when it is no header."""
    plain = _plain(line)
    headings, unmatched = _headings(line, headers, plain)
    found: list[Column] = []
    roles: set[Role] = set()
    dates: list[Cell] = []
    for role, cell in headings:
        if role == "date":
            dates.append(cell)
        if role in roles:
            continue
        roles.add(role)
        found.append(Column(role, cell.x0, cell.x1))
    if plain:
        if len(dates) > 1 and "post_date" not in roles:
            # Two date headings: the left one is the row's date, the next its posting date.
            first, second = sorted(dates, key=lambda c: c.x0)[:2]
            found = [c for c in found if c.role != "date"]
            found += [Column("date", first.x0, first.x1), Column("post_date", second.x0, second.x1)]
            roles.add("post_date")
        money = [c for c in found if c.role in MONEY_ROLES]
        if money:
            edge = min(money, key=lambda c: c.x0).x1
            found += [Column("other", c.x0, c.x1) for c in unmatched if c.x0 >= edge]
    if "date" in roles and roles & {"description", "check"} and roles & MONEY_ROLES:
        return sorted(found, key=lambda c: c.x0)
    return _headless(line, found, roles)


def _headless(line: ViewLine, found: Sequence[Column], roles: set[Role]) -> list[Column] | None:
    """A heading naming the table over money columns only.

    ``New Transactions for JORDAN SAMPLE | Foreign Spending | Amount $``:
    everything left of the first column heading is the **lead**, read
    row by row as the date then the description.
    """
    if roles & {"date", "post_date", "description", "check"} or not roles & MONEY_ROLES:
        return None
    if len(line.cells) > 5 or find_amounts(line.text):
        return None
    first = min(c.x0 for c in found)
    heading = [c for c in line.cells if c.x1 <= first]
    if not any(TABLE_HEADING_WORDS & set(normalise(c.text).split()) for c in heading):
        return None
    return [Column("lead", 0.0, first), *sorted(found, key=lambda c: c.x0)]


def header_like(line: ViewLine, headers: Mapping[str, Role] = HEADERS) -> bool:
    """Heading words only, two headings or more, no amount: a table we do not read.

    Headings printed in one cell count each (``Date | Amount | Date | Amount
    Date | Amount``). A line of **one** cell is header-like only when one of
    its headings is a date: ``Check Deposit`` under a row is the row's
    wrapped text and must not end the table.
    """
    if find_amounts(line.text):
        return False
    headings, unmatched = _headings(line, headers, len(line.cells) <= MAX_HEADER_CELLS)
    if unmatched or len(headings) < 2:
        return False
    return len(line.cells) >= 2 or any(role in _DATE_ROLES for role, _ in headings)


def stack_headers(
    lines: Sequence[ViewLine],
    headers: Mapping[str, Role] = HEADERS,
) -> list[ViewLine]:
    """``lines`` with a header printed over several lines made one line.

    No header is stacked yet: the lines are returned as they are.
    """
    return list(lines)

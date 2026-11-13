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
* :func:`heading_role` is the one lookup of a heading: the vocabulary, and
  a money heading with a unit printed after it (``Amount (SGD)``,
  ``AMOUNT EUR``).
* :func:`repair_heading` reads a heading that OCR got one letter wrong
  (``ithdrawal``): only a long single word of a scanned page.
* :func:`header_like` says a line is made of heading words only: the header
  of a table the reader does **not** read (a daily-balance table), which
  ends the transaction table above it. A title or a row's wrapped text
  made of heading words is none (its doc says how they are told apart).
* :func:`stack_headers` makes a header printed over two or three lines one
  line ("Stacked headers" below).

How :func:`read_header` works on a line:

1. A cell whose whole text is a heading names its column, as printed.
2. Any other cell of two words or more goes through
   :func:`split_heading_cell`, and a scanned cell of one word through
   :func:`repair_heading`.
3. A second ``date`` heading is the ``post_date`` when the line names none
   (``Transaction Date | Posting Date``): the reader claims its cells and
   does not read them, so a second date never lands in another column.
4. A cell that is still no heading and starts right of the leftmost money
   column is a column of role ``other`` (``Category``, ``Reference``): its
   cells are claimed and not read, so they are never joined to an amount.
   One left of the money is ignored.
5. The one unknown cell: when the line names a date and a money column
   and no description or check number, and exactly **one** cell that is
   not right of the leftmost money column is still no heading, that cell
   is the description heading in words we do not know (``Memo Text``, a
   heading cut to ``Descri``) when it stands between the date heading and
   the first money heading, has at most :data:`MAX_UNKNOWN_WORDS` words,
   no digit, and none of :data:`NOT_A_DESCRIPTION` (a title such as
   ``DAILY ENDING BALANCE`` between ``Date`` and ``Amount`` heads a list
   of balances, not rows). Two unknown cells, and the line is no header.
6. The line is a header when it names a date, a description or check
   number, and a money column; else it may be a headless header
   (:func:`_headless`).

Steps 2 to 5 are for a line of headings only: they are skipped on a line
that holds an amount or has more than :data:`MAX_HEADER_CELLS` cells, and a
cell of more than :data:`MAX_SPLIT_WORDS` words is never split, so a row,
a total or a sentence that happens to say "date", "description" and
"amount" is no header.

The vocabulary (:data:`HEADERS`) maps a normalised heading to its
:data:`Role`; a parser may pass its own.

Stacked headers
---------------

``Check | Deposits/ | Withdrawals/ | Ending daily`` printed over ``Date |
Number Description | Credits | Debits | balance`` is one header. For three
consecutive lines, then for two, :func:`stack_headers` puts one line in
their place when all of this holds (at most :data:`MAX_STACK_LINES` lines
are ever one header):

1. The lines are on one page, none holds an amount and none is longer than
   :data:`MAX_STACK_CHARS` characters.
2. Each line's top is under the one before it, at most
   :data:`STACK_PITCH` heights of the first line away (a height of at
   least :data:`MIN_STACK_HEIGHT`).
3. The **merged** line is a header by :func:`read_header` (with a date
   heading: never a headless one). Cells of the lines whose horizontal
   spans overlap (by :data:`MIN_STACK_OVERLAP` or more) are one cell: its
   text runs top to bottom, then left to right, and it keeps its words.
   When such a cell is no heading as a whole and
   :func:`split_heading_cell` reads several in it, they are its headings
   only when they stand **side by side** (``Check`` over ``Number
   Description`` is ``Check Number`` beside ``Description``). Headings one
   above the other are one phrase or the lines are no header: a title
   ``Withdrawals`` printed over ``Amount`` is no second column.
4. The merged header names **more** columns than any line of the group
   names as a header by itself, and three lines more than two of them
   that stack do. A tie keeps the shorter reading, so a header that reads
   today reads the same, and a section title printed close under a header
   (``Deposits``) is no part of it.
5. Every cell of the first line overlaps a cell of a later line (a title
   that stands over nothing is no header part), and every line gives a
   cell to a merged cell that holds a known heading. So the one unknown
   cell (step 5 above) is a stacked header's description only when each
   line it comes from also holds a known heading: words alone on a line
   between ``Date`` and ``Amount`` are a title.

The merged line has the last line's ``page``, ``index`` and ``height``; its
cells keep the boxes of their own words. It reads the same header through
:func:`read_header` afterwards, which is how the reader meets it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations, pairwise
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
    "date of transaction": "date",
    "date posted": "date",
    "effective date": "date",
    "txn date": "date",
    "tran date": "date",
    "information": "description",
    "narration": "description",
    "merchant name": "description",
    "merchant name or transaction description": "description",
    "description of transaction or credit": "description",
    "transaction detail": "description",
    "activity description": "description",
    "deposits credits": "credit",
    "deposits additions": "credit",
    "deposits other credits": "credit",
    "deposits and other credits": "credit",
    "deposits and credits": "credit",
    "deposits and additions": "credit",
    "additions": "credit",
    "deposit amt": "credit",
    "deposit amount": "credit",
    "credit amount": "credit",
    "withdrawals debits": "debit",
    "withdrawals subtractions": "debit",
    "withdrawals other debits": "debit",
    "withdrawals and other debits": "debit",
    "withdrawals and debits": "debit",
    "withdrawals and subtractions": "debit",
    "subtractions": "debit",
    "other debits": "debit",
    "withdrawal amt": "debit",
    "withdrawal amount": "debit",
    "debit amount": "debit",
    "ending daily balance": "balance",
    "daily balance": "balance",
    "closing balance": "balance",
    "transaction amount": "amount",
    "check nbr": "check",
    "chk": "check",
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
# ``payments``, ``charges`` and ``receipts`` are left out on purpose: which
# way the money goes under them differs from one layout to the next.
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
#: A unit in brackets after a money heading, brackets included, is at most
#: 12 characters (``(SGD)``, ``[in USD]``).
_UNIT_BRACKETS: Final = re.compile(r"\s*(?:\([^()\[\]]{1,10}\)|\[[^()\[\]]{1,10}\])\s*$")
#: A unit as a last word: three capital letters (``AMOUNT EUR``).
_UNIT_WORD: Final = re.compile(r"\s+([A-Z]{3})\s*$")
#: Three capital letters after a money heading that are a word, not a
#: currency: ``AMOUNT DUE``, ``BALANCE NEW`` and ``DEBIT POS`` are no column
#: headings.
NOT_A_UNIT: Final = frozenset(
    {"DUE", "NEW", "OLD", "END", "NET", "TAX", "FEE", "YTD", "MTD", "QTD", "APR", "APY"}
    | {"AVG", "MIN", "MAX", "OUT", "OWN", "ALL", "AND", "FOR", "THE", "PER", "NOW", "NOT"}
    | {"POS", "ATM", "ACH", "FWD", "ADJ", "INT", "DIV"}
)
#: The lone unknown cell of a header is a description heading of at most
#: this many words.
MAX_UNKNOWN_WORDS: Final = 4
#: Words no description heading holds: a cell with one of them between the
#: date and the money names figures (a title, a rate, another date).
NOT_A_DESCRIPTION: Final = frozenset({"balance", "total", "summary", "amount", "date", "rate"})
#: The lone unknown cell may reach this far over the date heading's right
#: edge and the first money heading's left edge.
UNKNOWN_CELL_SLACK: Final = 1.0
#: A scanned heading is repaired only when it, and the heading it is read
#: as, have this many letters: one wrong letter in a short word is another word.
MIN_REPAIR_LETTERS: Final = 6
#: A header is printed over at most this many lines.
MAX_STACK_LINES: Final = 3
#: A line of a stacked header is at most this long.
MAX_STACK_CHARS: Final = 130
#: The lines of a stacked header are at most this many heights of the
#: first line apart, top to top; a line is at least ``MIN_STACK_HEIGHT`` high.
STACK_PITCH: Final = 2.2
MIN_STACK_HEIGHT: Final = 6.0
#: Cells of two header lines are one heading when their horizontal spans
#: share this much; headings that share less stand side by side.
MIN_STACK_OVERLAP: Final = 1.0


@dataclass(frozen=True)
class Column:
    role: Role
    x0: float
    x1: float


def heading_role(text: str, headers: Mapping[str, Role] = HEADERS) -> Role | None:
    """The column ``text`` heads, or None when it is no heading.

    The role of the normalised text; else, for a **money** heading
    (:data:`NUMERIC_ROLES`), its role after dropping one unit printed after
    it: a bracketed group of at most 12 characters (``Amount (SGD)``) or one
    last word of three capital letters (``AMOUNT EUR``; not a word of
    :data:`NOT_A_UNIT`). A text heading keeps its words: ``Description
    (USD)`` is no heading.
    """
    role = headers.get(normalise(text))
    if role is not None:
        return role
    bare, dropped = _UNIT_BRACKETS.subn("", text, count=1)
    if not dropped:
        unit = _UNIT_WORD.search(text)
        if unit is None or unit.group(1) in NOT_A_UNIT:
            return None
        bare = text[: unit.start()]
    role = headers.get(normalise(bare))
    return role if role in NUMERIC_ROLES else None


def _one_edit(a: str, b: str) -> bool:
    """``a`` becomes ``b`` by one letter added, lost or changed."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    start = 0
    while start < len(a) and a[start] == b[start]:
        start += 1
    # Past the first difference the rest must agree: after it in both (a
    # letter changed) or after one more letter of the longer (a letter lost).
    return a[start + (len(a) == len(b)) :] == b[start + 1 :]


def repair_heading(cell: Cell, headers: Mapping[str, Role] = HEADERS) -> Role | None:
    """The heading a scanned ``cell`` printed before OCR got one letter wrong.

    Only for a cell read by OCR (``cell.conf`` is set) of one word with
    :data:`MIN_REPAIR_LETTERS` letters or more that is no heading as read:
    the role of the one-word headings of as many letters that are one edit
    away (a letter added, lost or changed). None when headings that close
    name different columns (``Deposit5`` is ``deposit`` or ``deposits``,
    both the credit column). Nothing is warned: the rows' fields already
    carry the OCR confidence.
    """
    if cell.conf is None:
        return None
    word = normalise(cell.text)
    if " " in word or sum(ch.isalpha() for ch in word) < MIN_REPAIR_LETTERS or word in headers:
        return None
    roles = {
        role
        for heading, role in headers.items()
        if " " not in heading and len(heading) >= MIN_REPAIR_LETTERS and _one_edit(word, heading)
    }
    return next(iter(roles)) if len(roles) == 1 else None


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
    if not 2 <= count <= MAX_SPLIT_WORDS or heading_role(cell.text, headers) is not None:
        return None
    roles: dict[tuple[int, int], Role] = {}
    #: Per start word: (phrases needed for the words from there on, end of the first).
    best: list[tuple[int, int] | None] = [None] * count + [(0, count)]
    for start in range(count - 1, -1, -1):
        for end in range(count, start, -1):
            rest = best[end]
            if rest is None or end - start == count:
                continue
            role = heading_role(" ".join(w.text for w in words[start:end]), headers)
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
    """``(headings, unmatched cells)`` of ``line``: whole cells first (as
    printed, or, when ``split``, as :func:`repair_heading` reads a scanned
    word), then the headings of the cells that ``split`` lets through
    :func:`split_heading_cell`."""
    whole: list[tuple[Role, Cell]] = []
    parts: list[tuple[Role, Cell]] = []
    unmatched: list[Cell] = []
    for cell in line.cells:
        role = heading_role(cell.text, headers)
        if role is None and split:
            role = repair_heading(cell, headers)
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
            if "date" in roles and not roles & {"description", "check"}:
                lone = _lone_description([c for c in unmatched if c.x0 < edge], found)
                if lone is not None:
                    found.append(Column("description", lone.x0, lone.x1))
                    roles.add("description")
    if "date" in roles and roles & {"description", "check"} and roles & MONEY_ROLES:
        return sorted(found, key=lambda c: c.x0)
    return _headless(line, found, roles)


def _lone_description(unknown: Sequence[Cell], found: Sequence[Column]) -> Cell | None:
    """The description heading among the cells that are no heading (module doc, 5).

    ``unknown`` are the unmatched cells that are not right of the leftmost
    money column; ``found`` names a date and a money column.
    """
    if len(unknown) != 1:
        return None
    cell = unknown[0]
    date = next(c for c in found if c.role == "date")
    money = min(c.x0 for c in found if c.role in MONEY_ROLES)
    if cell.x0 < date.x1 - UNKNOWN_CELL_SLACK or cell.x1 > money + UNKNOWN_CELL_SLACK:
        return None
    words = normalise(cell.text).split()
    if not 1 <= len(words) <= MAX_UNKNOWN_WORDS or any(ch.isdigit() for ch in cell.text):
        return None
    return None if NOT_A_DESCRIPTION & set(words) else cell


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
    Date | Amount``). What ends a table must be told from a title printed
    inside it and from a row's wrapped text, which may be heading words too:

    * A line of two cells or more names columns of two kinds or more:
      ``Deposit | Additions`` under a row are both money in, and no header.
    * A line of **one** cell starts with a date heading and names a column
      of figures (``Date Balance Date Balance``). ``Check Deposit`` and
      ``Transaction Posted`` under a row are its wrapped text, ``Deposits
      Posted`` over rows is a title.
    """
    if find_amounts(line.text):
        return False
    headings, unmatched = _headings(line, headers, len(line.cells) <= MAX_HEADER_CELLS)
    if unmatched or len(headings) < 2:
        return False
    roles = {role for role, _ in headings}
    if len(line.cells) >= 2:
        return len(roles) >= 2
    first = min(headings, key=lambda heading: heading[1].x0)[0]
    return first in _DATE_ROLES and bool(roles & NUMERIC_ROLES)


def _stackable(group: Sequence[ViewLine]) -> bool:
    """The lines may be one header by where and what they are (module doc, 1, 2 and 5)."""
    first = group[0]
    if any(not line.cells or line.page != first.page for line in group):
        return False
    reach = STACK_PITCH * max(first.height, MIN_STACK_HEIGHT)
    if any(not 0 < low.bbox[1] - high.bbox[1] <= reach for high, low in pairwise(group)):
        return False
    below = [cell for line in group[1:] for cell in line.cells]
    if any(
        all(cell.overlap(other.x0, other.x1) < MIN_STACK_OVERLAP for other in below)
        for cell in first.cells
    ):
        return False  # a title over nothing
    return all(len(line.text) <= MAX_STACK_CHARS and not find_amounts(line.text) for line in group)


def _merge(
    group: Sequence[ViewLine], headers: Mapping[str, Role]
) -> tuple[ViewLine, list[frozenset[int]]] | None:
    """``group`` as one line, and per cell of it the lines (by place in
    ``group``) its words come from; None when headings printed one above
    the other are no one phrase (module doc, 3)."""
    stacks: list[list[tuple[int, Cell]]] = []
    for place, line in enumerate(group):
        for cell in line.cells:
            joined = [(place, cell)]
            apart: list[list[tuple[int, Cell]]] = []
            for stack in stacks:
                if any(cell.overlap(c.x0, c.x1) >= MIN_STACK_OVERLAP for _, c in stack):
                    joined += stack
                else:
                    apart.append(stack)
            stacks = [*apart, joined]
    cells: list[tuple[Cell, frozenset[int]]] = []
    for stack in stacks:
        if len(stack) == 1:
            cells.append((stack[0][1], frozenset({stack[0][0]})))
            continue
        stack.sort(key=lambda item: (item[0], item[1].x0))
        words = [(place, word) for place, cell in stack for word in cell.word_cells()]
        whole = join_cells([word for _, word in words])
        parts = None if heading_role(whole.text, headers) else split_heading_cell(whole, headers)
        if parts is None:
            cells.append((whole, frozenset(place for place, _ in words)))
            continue
        pieces = [piece for _, piece in parts]
        if any(a.overlap(b.x0, b.x1) >= MIN_STACK_OVERLAP for a, b in combinations(pieces, 2)):
            return None  # one over the other and no phrase
        start = 0
        for piece in pieces:
            end = start + len(piece.words)
            cells.append((piece, frozenset(place for place, _ in words[start:end])))
            start = end
    cells.sort(key=lambda item: item[0].x0)
    last = group[-1]
    line = ViewLine(
        page=last.page,
        index=last.index,
        cells=tuple(cell for cell, _ in cells),
        text=" ".join(cell.text for cell, _ in cells),
        height=last.height,
    )
    return line, [origin for _, origin in cells]


def _stacked(
    lines: Sequence[ViewLine], at: int, size: int, headers: Mapping[str, Role]
) -> tuple[ViewLine, int] | None:
    """The ``size`` lines from ``at`` as one header line and how many columns
    it names, or None when they are no stacked header (module doc)."""
    group = lines[at : at + size]
    if len(group) < size or not _stackable(group):
        return None
    merged = _merge(group, headers)
    if merged is None:
        return None
    line, origins = merged
    columns = read_header(line, headers)
    if columns is None or all(column.role != "date" for column in columns):
        return None
    if any(len(read_header(single, headers) or ()) >= len(columns) for single in group):
        return None
    headings, _ = _headings(line, headers, _plain(line))
    named: set[int] = set()
    for cell, origin in zip(line.cells, origins, strict=True):
        if any(cell.x0 <= h.x0 and h.x1 <= cell.x1 for _, h in headings):
            named |= origin
    if len(named) < size:
        return None  # a line that gives the header no known heading
    for shorter in range(2, size):
        for start in range(at, at + size - shorter + 1):
            inner = _stacked(lines, start, shorter, headers)
            if inner is not None and inner[1] >= len(columns):
                return None
    return line, len(columns)


def stack_headers(
    lines: Sequence[ViewLine],
    headers: Mapping[str, Role] = HEADERS,
) -> list[ViewLine]:
    """``lines`` with a header printed over two or three lines made one line.

    Three lines are tried before two (module doc, "Stacked headers"); a
    line that is part of no stacked header is returned as it is.
    """
    out: list[ViewLine] = []
    at = 0
    while at < len(lines):
        for size in range(MAX_STACK_LINES, 1, -1):
            stacked = _stacked(lines, at, size, headers)
            if stacked is not None:
                out.append(stacked[0])
                at += size
                break
        else:
            out.append(lines[at])
            at += 1
    return out

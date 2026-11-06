# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which account each page of a statement belongs to (INGESTION.md "Several accounts").

One PDF may hold several accounts: a brokerage report of an individual
account, an IRA and a 529, or a bank's combined statement of checking,
savings and brokerage. The statement parsers
(:mod:`app.ingestion.parsers.brokerage`,
:mod:`app.ingestion.parsers.table_statement`) read each account on its own;
this module finds where each account's lines are, for any institution:

* **Account headings** (:func:`read_heading`): ``Account # Z12-345678``,
  ``Account Number: XXXX-4821``, ``Account 418-205817``, after at most an
  all-capitals title (``INDIVIDUAL - TOD Account # Z12-345678``), or an
  all-capitals title ending in a number (``INDIVIDUAL - TOD Z12-345678``).
  A text that merely mentions an account (``Trans. from account
  418-330642``, ``Linked bank account XXXX-9999``) is no heading.
  A heading is looked for

  - on a whole line without amounts, and in each of its **cells**;
  - in a page's **header band** (its first :data:`HEADING_TOP_LINES` lines
    above its first table row, a line led by a date, and its last
    :data:`HEADING_BOTTOM_LINES` lines) also in the cells of a line
    that prints amounts, so ``$103,351.18 | Account 111-111111`` and
    ``Account Value: $62,362.06 | Account 222-222222`` are headings (the
    cell holding the heading has no amount; a line starting with a date is
    a table row, never a heading). Such lines naming two or more accounts
    on one page are an overview of the accounts, not headings;
  - **mid-page** only on a line without amounts naming an account the
    document's account index lists (a combined statement starting its
    next account half-way down a page), so a body line never switches
    the account of a document that has no index.

* **The account index** (:func:`read_index`): a table whose header names
  an account number column (``Account Number``, ``Account #``, ``Account
  No``) and a name or type column (``Account Type/Name``, ``Account
  Name``), e.g. *Accounts Included in This Report* or *Your Accounts at a
  Glance*. Each row gives an account's last four digits and its printed
  name; all-capitals group lines between the rows (``PERSONAL
  RETIREMENT``, ``EDUCATION (529) ACCOUNTS``) qualify the rows under them.
  An owner's name printed before every account's name
  (``Jordan Sample - Traditional IRA``) is dropped. The index's own page
  numbers are not used: layouts print wrong ones, the headings decide.
  The name and group give the account's type and subtype
  (:func:`account_kind`, via
  :func:`app.domain.accounts.subtypes.guess_subtype`).

* **Segments** (:func:`segment`): a page whose headings all name one
  account belongs to it entirely; on a page naming several, the lines
  before the first heading continue the account of the page before and
  each heading starts its account; a page with no heading continues the
  account before it; pages before the first heading (an overview of all
  accounts) belong to no account (the **preamble**).

Only the last four digits of any account number are kept, in the results
and while reading; full numbers never leave the line they were read from.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from app.domain.accounts.models import AccountType
from app.domain.accounts.subtypes import guess_subtype
from app.ingestion.parsers.base import ViewLine
from app.ingestion.parsers.text import find_amounts, normalise, numeric_parts

#: ``Account # Z12-345678``, ``Account Number: 123-456789``, ``Account XXXX-4821``.
_ACCOUNT_LABELLED: Final = re.compile(
    r"\baccount\s{0,3}(?:#|number|no\.?)?\s{0,3}:?\s{0,3}"
    r"(?P<num>(?:[A-Z]{1,3}|[*xX\u2022]{2,12})?[ -]?\d(?:[\d-]| (?=\d)){2,24}\d)(?![\w-])",
    re.IGNORECASE,
)
#: The number ending an all-capitals account title (``INDIVIDUAL - TOD Z12-345678``).
_TITLE_NUMBER: Final = re.compile(r"[A-Z]{0,3}\d{2,4}-\d{4,8}")
_TITLE_WORD: Final = re.compile(r"[A-Z][A-Z&'./]{0,30}")
#: A cell holding nothing but an account number (an index row's number column).
_NUMBER_CELL: Final = re.compile(
    r"(?P<prefix>[A-Z]{1,3}|[*xX\u2022]{2,12})?[ -]?\d(?:[\d-]| (?=\d)){2,24}\d"
)
_ISO_DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
#: Characters of a line an account heading is looked for in.
_HEADING_CHARS: Final = 200
_DASHES: Final = frozenset({"-", "\u2013"})
#: A page's header band: account headings are looked for in its first and
#: last lines (where layouts print and repeat them).
HEADING_TOP_LINES: Final = 8
HEADING_BOTTOM_LINES: Final = 3
#: Rows of one account index read at most (bounded work).
INDEX_ROWS_MAX: Final = 60
#: Header cells (normalised; ``#`` is dropped) naming an index's account number column.
_NUMBER_HEADERS: Final = ("account number", "account no", "acct number", "acct no")
#: Words of a header cell naming an index's name or type column.
_NAME_WORDS: Final = frozenset({"name", "type", "title", "description", "nickname"})


@dataclass(frozen=True)
class Heading:
    """What an account heading says: the last four digits and a printed title."""

    last4: str
    #: The all-capitals title before the number (``INDIVIDUAL - TOD``), if any.
    title: str | None = None


def read_heading(text: str) -> Heading | None:
    """The account a heading (a line or a cell) names, or None (module doc).

    A text that prints an amount (a row of an accounts overview) is no
    heading. Only the last four digits are returned; the number is not kept.
    """
    text = text[:_HEADING_CHARS]
    if find_amounts(text):
        return None
    tokens = text.split()
    title = 0
    while (
        title < len(tokens)
        and normalise(tokens[title]) != "account"
        and (tokens[title] in _DASHES or _TITLE_WORD.fullmatch(tokens[title]))
    ):
        title += 1
    match = _ACCOUNT_LABELLED.match(" ".join(tokens[title:]))
    number: str | None = match["num"] if match is not None else None
    title_words = tokens[:title]
    if (
        number is None
        and 2 <= len(tokens) <= 12
        and _TITLE_NUMBER.fullmatch(tokens[-1])
        and any(_TITLE_WORD.fullmatch(t) for t in tokens[:-1])
        and all(t in _DASHES or _TITLE_WORD.fullmatch(t) for t in tokens[:-1])
    ):
        number = tokens[-1]
        title_words = tokens[:-1]
    if number is None:
        return None
    digits = "".join(c for c in number if c.isdigit())
    if len(digits) < 4:
        return None
    named = " ".join(t for t in title_words).strip(" -\u2013")
    return Heading(digits[-4:], named or None)


def account_heading(text: str) -> str | None:
    """The last four digits of the account a heading names (:func:`read_heading`)."""
    found = read_heading(text)
    return found.last4 if found is not None else None


# ---------------------------------------------------------------------------
# Account type from a printed name
# ---------------------------------------------------------------------------

_BANK_WORDS: Final[tuple[tuple[re.Pattern[str], AccountType], ...]] = (
    (re.compile(r"\b(checking|chequing|current account)\b"), AccountType.CHECKING),
    (re.compile(r"\b(credit card|card account)\b"), AccountType.CREDIT_CARD),
    (re.compile(r"\b(savings|money market|certificate of deposit)\b"), AccountType.SAVINGS),
)
#: Retirement subtypes whose name says "savings" (not a savings account).
_SAVINGS_RETIREMENT: Final = frozenset({"hsa", "tsp"})
#: Brokerage subtypes no bank account name would carry.
_SPECIFIC_BROKERAGE: Final = frozenset(
    {"education_529", "coverdell_esa", "able", "custodial", "crypto"}
)
_INVESTMENT_WORDS: Final = re.compile(
    r"\b(brokerage|investments?|investing|portfolio|trading|securities)\b"
)
_RETIREMENT_WORDS: Final = re.compile(r"\b(retirement|pension)\b")


def _kind_of(text: str) -> tuple[AccountType | None, str | None]:
    _, retirement_sub = guess_subtype(text, AccountType.RETIREMENT)
    _, brokerage_sub = guess_subtype(text, AccountType.BROKERAGE)
    bank = next((kind for pattern, kind in _BANK_WORDS if pattern.search(text)), None)
    if retirement_sub is not None and (bank is None or retirement_sub in _SAVINGS_RETIREMENT):
        return AccountType.RETIREMENT, retirement_sub
    if brokerage_sub in _SPECIFIC_BROKERAGE:
        return AccountType.BROKERAGE, brokerage_sub
    if bank is not None:
        return bank, guess_subtype(text, bank)[1]
    if brokerage_sub is not None:
        return AccountType.BROKERAGE, brokerage_sub
    if _RETIREMENT_WORDS.search(text):
        return AccountType.RETIREMENT, None
    if _INVESTMENT_WORDS.search(text):
        return AccountType.BROKERAGE, None
    return None, None


def account_kind(*texts: str | None) -> tuple[str | None, str | None]:
    """``(account type, subtype)`` an account's printed name and group say.

    The texts are tried in order (the account's name, then its index group):
    the first that names a type decides it, and a later one may add the
    subtype the earlier one did not (``Education Account`` under
    ``EDUCATION (529) ACCOUNTS``: brokerage, ``education_529``). Bank words
    (checking, savings, credit card) win over the brokerage subtypes a bank
    account's name also fits (``Joint Checking``); a retirement or
    education plan's own words win over a bank word (``Health Savings
    Account``, ``529 College Savings``). ``(None, None)``: nothing said.
    """
    kind: AccountType | None = None
    subtype: str | None = None
    for raw in texts:
        text = " ".join((raw or "").lower().replace("-", " ").split())
        if not text:
            continue
        found, found_sub = _kind_of(text)
        if found is None:
            continue
        if kind is None:
            kind, subtype = found, found_sub
        elif found is kind and subtype is None:
            subtype = found_sub
        if subtype is not None:
            break
    return (kind.value if kind is not None else None), subtype


# ---------------------------------------------------------------------------
# The account index
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountInfo:
    """What the document says about one account (never its full number)."""

    last4: str
    name: str | None = None
    #: An ``AccountType`` value the name says (``retirement``, ``checking``).
    account_type: str | None = None
    #: A subtype code of ``account_type`` (``traditional_ira``, ``education_529``).
    subtype: str | None = None


def number_last4(text: str) -> str | None:
    """The last four digits of a cell holding only an account number, else None.

    ``418-205817``, ``XXXX-4821``, ``Z29-104821``; not a bare integer under
    six digits (a page number, a year), not an amount, not a date.
    """
    text = text.strip()
    match = _NUMBER_CELL.fullmatch(text[:40])
    if match is None or _ISO_DATE.fullmatch(text) or numeric_parts(text) is not None:
        return None
    digits = "".join(c for c in text if c.isdigit())
    if len(digits) < 4:
        return None
    if match["prefix"] is None and "-" not in text and len(digits) < 6:
        return None
    return digits[-4:]


def _index_columns(line: ViewLine) -> tuple[float, float] | None:
    """The account number column's span when ``line`` is an index header."""
    number: tuple[float, float] | None = None
    named = False
    for cell in line.cells:
        norm = normalise(cell.text)
        numbered = norm in _NUMBER_HEADERS or (norm in ("account", "acct") and "#" in cell.text)
        if number is None and numbered:
            number = (cell.x0, cell.x1)
            continue
        if _NAME_WORDS & set(norm.replace("/", " ").split()):
            named = True
    if number is None or not named or len(line.cells) < 2:
        return None
    return number


def _group_line(line: ViewLine) -> bool:
    """An all-capitals group heading between index rows (``PERSONAL RETIREMENT``)."""
    if len(line.cells) != 1 or find_amounts(line.text):
        return False
    letters = [c for c in line.text if c.isalpha()]
    return len(letters) >= 3 and all(c.isupper() for c in letters)


def _common_owner(names: Sequence[str]) -> str | None:
    """A name printed before every account's own name (``Jordan Sample - ``)."""
    if len(names) < 2:
        return None
    heads = {re.split(r"\s[-\u2013]\s", name, maxsplit=1)[0] for name in names}
    if len(heads) != 1:
        return None
    head = heads.pop()
    if any(len(re.split(r"\s[-\u2013]\s", name, maxsplit=1)) < 2 for name in names):
        return None
    # ``INDIVIDUAL - TOD`` / ``INDIVIDUAL - JOINT``: the shared part is the account's kind.
    return head if account_kind(head) == (None, None) else None


def read_index(lines: Sequence[ViewLine]) -> tuple[dict[str, AccountInfo], frozenset[int]]:
    """``(account info by last four digits, indexes of the index's lines)`` (module doc)."""
    rows: list[tuple[str, str, str]] = []  # (last4, name, group)
    used: set[int] = set()
    position = 0
    while position < len(lines):
        header = lines[position]
        column = _index_columns(header)
        position += 1
        if column is None:
            continue
        used.add(header.index)
        group = ""
        found = 0
        while position < len(lines) and found < INDEX_ROWS_MAX:
            line = lines[position]
            if line.page != header.page or line.norm.startswith("total"):
                break
            row = _index_row(line, column)
            if row is not None:
                last4, name = row
                rows.append((last4, name, group))
                used.add(line.index)
                found += 1
            elif _group_line(line):
                group = line.text
                used.add(line.index)
            else:
                break
            position += 1
    owner = _common_owner([name for _, name, _ in rows if name])
    infos: dict[str, AccountInfo] = {}
    for last4, name, group in rows:
        if last4 in infos:
            continue
        if owner is not None and name.startswith(owner):
            name = name[len(owner) :].strip(" -\u2013")
        kind, subtype = account_kind(name, group)
        infos[last4] = AccountInfo(last4, name[:200] or None, kind, subtype)
    return infos, frozenset(used)


def _index_row(line: ViewLine, column: tuple[float, float]) -> tuple[str, str] | None:
    """``(last4, printed name)`` of an index row: a number under the number column."""
    for position, cell in enumerate(line.cells):
        if cell.overlap(*column) <= 0 and not (column[0] - 20 <= cell.x0 <= column[1] + 20):
            continue
        last4 = number_last4(cell.text)
        if last4 is None:
            continue
        before = [
            c.text
            for c in line.cells[:position]
            if not find_amounts(c.text) and not c.text.strip().isdigit()
        ]
        return last4, " ".join(before).strip()
    return None


# ---------------------------------------------------------------------------
# Headings on the pages and the segments
# ---------------------------------------------------------------------------


def _heading_of(line: ViewLine, *, band: bool) -> tuple[Heading | None, bool]:
    """``(heading, printed beside amounts)`` of one line (module doc)."""
    if not find_amounts(line.text):
        found = read_heading(line.text)
        if found is None and len(line.cells) > 1:
            found = next((h for c in line.cells if (h := read_heading(c.text)) is not None), None)
        return found, False
    if not band or numeric_parts(line.cells[0].text) is not None:
        return None, False
    for cell in line.cells:
        found = read_heading(cell.text)
        if found is not None:
            return found, True
    return None, False


@dataclass
class AccountSegment:
    """One account's lines, in print order."""

    info: AccountInfo
    lines: list[ViewLine] = field(default_factory=list)

    @property
    def last4(self) -> str:
        return self.info.last4

    @property
    def first_page(self) -> int | None:
        return self.lines[0].page if self.lines else None

    @property
    def last_page(self) -> int | None:
        return self.lines[-1].page if self.lines else None


@dataclass
class Segmentation:
    """The accounts a document's headings name and the lines before them."""

    accounts: list[AccountSegment]
    preamble: list[ViewLine]
    #: The account index's entries (empty without an index).
    index: Mapping[str, AccountInfo]

    def info(self, last4: str | None) -> AccountInfo | None:
        if last4 is None:
            return None
        found = next((a.info for a in self.accounts if a.last4 == last4), None)
        return found if found is not None else self.index.get(last4)


def _info(last4: str, index: Mapping[str, AccountInfo], titles: Mapping[str, str]) -> AccountInfo:
    known = index.get(last4)
    if known is not None:
        return known
    title = titles.get(last4)
    kind, subtype = account_kind(title)
    return AccountInfo(last4, title, kind, subtype)


def segment(lines: Sequence[ViewLine]) -> Segmentation:
    """Each account's lines (module doc); ``accounts`` in the order first named."""
    pages: dict[int, list[ViewLine]] = {}
    for line in lines:
        pages.setdefault(line.page, []).append(line)
    index, index_lines = read_index(lines)
    headings: dict[int, str] = {}
    titles: dict[str, str] = {}
    for page_lines in pages.values():
        last = len(page_lines) - HEADING_BOTTOM_LINES
        beside: dict[int, str] = {}
        rows = False
        for position, line in enumerate(page_lines):
            # The top band ends at the page's first table row (a line led by a date).
            rows = rows or numeric_parts(line.cells[0].text) is not None
            if line.index in index_lines:
                continue
            band = (position < HEADING_TOP_LINES and not rows) or position >= last
            found, with_amounts = _heading_of(line, band=band)
            if found is None or (not band and found.last4 not in index):
                continue
            if with_amounts:
                beside[line.index] = found.last4
            else:
                headings[line.index] = found.last4
            if found.title and found.last4 not in titles:
                titles[found.last4] = found.title
        if len(set(beside.values())) == 1:
            # Several accounts printed beside amounts: an overview, not headings.
            headings.update(beside)
    named = list(dict.fromkeys(headings[line.index] for line in lines if line.index in headings))
    routed: dict[str | None, list[ViewLine]] = {None: []}
    current: str | None = None
    for page_lines in pages.values():
        marks = [headings[line.index] for line in page_lines if line.index in headings]
        if marks and len(set(marks)) == 1:
            current = marks[0]
            routed.setdefault(current, []).extend(page_lines)
            continue
        for line in page_lines:
            mark = headings.get(line.index)
            if mark is not None:
                current = mark
            routed.setdefault(current, []).append(line)
    preamble = routed.pop(None)
    accounts = [AccountSegment(_info(last4, index, titles), routed[last4]) for last4 in named]
    return Segmentation(accounts, preamble, index)

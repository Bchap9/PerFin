# SPDX-License-Identifier: AGPL-3.0-or-later
"""The generic table statement parser for bank and card statements (PDF, scans).

Works on the words and boxes the extractor stored (``Page.words``/``lines``;
text layer or OCR), so ruled tables, aligned columns and multi-page tables
are read the same way:

1. **Header.** A line whose cells name a date, a description and an amount
   (one signed *Amount* column, or *Debit*/*Credit* style columns), with an
   optional running *Balance* and posting date, starts (or, on a later page,
   continues) the table. The header cells' horizontal spans are the columns.
   A *checks* table (``Check No. | Date Paid | Amount``) is one too: each
   row is ``CHECK <number>``, money out. A line of header words only that
   is no table we read (``Date | Balance | Date | Balance``) ends the table
   above it. A **headless** header names money columns only, beside a
   heading naming the table (``New Transactions for JORDAN SAMPLE |
   Foreign Spending | Amount $``, at most five cells, no figures): all left
   of the first column heading is the *lead*, read per row as the date (the
   lead's first cell, or the first words of it on a line with an amount)
   and then the description. A *Foreign Spending* (*Foreign Amount*,
   *Original Currency*...) column is informational: it goes to the memo
   (``Goods or Services; foreign amount 360.00 US Dollar``), never into the
   amount.
2. **Rows.** Every later line is split into cells; each cell goes to the
   column it overlaps most. A line with a date in the date column and an
   amount is a transaction; a dated line whose description is an opening or
   closing label is the table's own balance row; a following line with only
   description text (and foreign-column text) continues the previous row's
   memo (wrapped text, a category line); a ``CR``/``DR`` alone in the
   amount column of that line marks the row's amount, as one after the
   amount does. Dates may carry a mark after them (``January 9 *``: a
   processing date) and may be month names without a year
   (``December 16``, ``16 Dec``); numeric rows with and without a year
   in one table are both read.
   Section headings set the row type (fees, interest) or start a *pending*
   section whose rows are counted but not imported (they are not posted and
   not part of the balances). Totals, page footers and "continued" lines are
   skipped. A dated line without a readable amount is counted as skipped.
   A **payment stub** is left out before anything is read
   (:meth:`TableStatementParser.without_stubs`): from a line saying to
   detach it (``Please detach here``, ``Return this portion``..., without
   an amount or starting with the marker: a row starts with its date) to
   the end of its page, unless a table header follows on that page; so its
   dated ``Amount Due`` line is no row and its account number with all
   digits is not the statement's.
3. **Summary.** Outside the rows: the statement period (``Statement period``,
   ``Billing period``...), the account's last four digits (a masked number
   or "ending in"), the currency, and the opening/closing balances by label.
   Every labelled line is a period candidate (a label alone on its line
   offers the next line), tried in order until one reads, so a label that
   matches something else first ("Change from last period") blocks
   nothing; a range's start borrows the end's year (``Jan 1 - Jan 31,
   2026``). Without a labelled period, the **header band**
   (:func:`app.ingestion.classify.dates.header_band_range`: a range printed
   in the top lines of at least half the pages) is the period
   (:func:`read_period`), believed as far as the classifier believes it
   (:func:`~app.ingestion.classify.dates.band_confidence`: 0.9 on two or
   more pages, 0.75 on one, so a range atop a one-page document is no
   stated period). Yearless row dates (``MM/DD``) take their year from the
   period. The day/month order of a numeric period comes from its own
   values when one proves it (``01/07/2015 - 31/07/2015`` is day-first),
   whatever the rows' default; and when the rows fit both orders (or are
   not numeric) the order the period proves is the rows' order too.
   Labels printed **over their values** (a row of labels over a row of
   boxes: ``Previous Balance | Payments and Credits | New Charges | New
   Balance`` over ``9,803.59 - 11.39 + 4,330.71 = 14,122.91``; ``Prepared
   For | Account Number | Date`` over the values) give each value the label
   above it; label lines stacked over one value row add up.
   **Statement date.** With no period printed, a statement date
   (``Statement Date``, ``Closing Date``..., or a cell reading only
   ``Date`` on a line naming no column heading) ends the period
   (:func:`anchored_period`): it starts the day after a printed previous
   statement date, else the day after the same date a month before, or on
   the earliest row when one is older. Each label's value runs to the next
   label on its line (``Previous Statement Date: 12/13/24  Statement Date:
   01/14/25``); a label after ``Next`` (``Next Closing Date``) is another
   statement's and only ends the value before it. Yearless rows take the
   latest year that keeps them on or before the statement date (December
   rows of a January statement are the year before; none lands after it).
   When half the dated rows or more are printed with a year after the
   "statement date", it is none (the rows' span is the period, as without
   one); fewer are outliers (a stub's dated line read as a row) and are
   skipped. Its confidence is ``confidence.DERIVED``: a stated period for
   the duplicate guard.
4. **Signs.** ``bank``: a signed amount as printed, *Debit* negative and
   *Credit* positive, ``CR``/``DR`` markers honoured. ``card``: charges are
   printed positive and credits negative or with ``CR``; both the amounts and
   the printed balances (amounts owed) are turned into the ledger convention
   (a purchase is negative, the balance owed is negative).
5. **Checks and confidence.** The running balance and the totals are
   reconciled (:mod:`app.ingestion.reconcile`) and every field is scored
   (:mod:`app.ingestion.parsers.confidence`). A summary printed as a whole
   equation (opening + payments and credits - new charges = closing, in the
   ledger convention) is checked further (:func:`totals_disagree`): the
   rows' money in must be its credits and their money out its charges, or
   the statement does not reconcile (warning ``summary_totals_mismatch``).
6. **Several accounts.** When the pages name two or more accounts that each
   have a transaction table (:func:`app.ingestion.parsers.accounts.segment`:
   account headings, the account index), each account's lines are one
   statement, with the account's last four digits, printed name, type and
   pages; the lines before the first account (an overview) only offer their
   period, and a row there belongs to no account (skipped,
   ``rows_outside_account``). :meth:`TableStatementParser.read_statement`
   reads one account's lines for the brokerage parsers too (a combined
   statement's checking account).
7. **Accounts without a number.** A credit union prints a member's shares
   one after the other under titles (``PRIMARY SAVINGS - ID 0000``, ``FREE
   CHECKING - ID 0010``), each with its own opening and closing balance row
   and no account number. :meth:`TableStatementParser.balance_sections`
   finds them by the balance **starting over**: an opening-balance row in a
   table whose figure is not the balance the rows before it ended on. A
   balance brought forward at the top of a page (the same figure as the one
   carried forward) continues its account. Each section is one statement
   named by the title over its table; its last four digits are the title's
   own number when that has four digits or more, never the member number
   printed once for all of them (warning ``accounts_by_balance``).
   Otherwise the whole document is one statement.

Institution parsers (:mod:`app.ingestion.parsers.institutions`) subclass it
and pin what the generic one has to guess: date format, sign convention,
currency, fingerprint phrases and the institution's name.
"""

from __future__ import annotations

import re
from calendar import monthrange
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import ClassVar, Final, Literal

from app.ingestion.classify.dates import HEADER_BAND_LINES as BAND_LINES
from app.ingestion.classify.dates import (
    DateOrder,
    band_confidence,
    find_ranges,
    header_band_range,
)
from app.ingestion.classify.dates import find_dates as classify_dates
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.accounts import (
    AccountSegment,
    Segmentation,
    account_kind,
    segment,
    summary_last4,
)
from app.ingestion.parsers.base import (
    Cell,
    DocumentParser,
    DocumentView,
    ParseOutcome,
    ViewLine,
    join_cells,
)
from app.ingestion.parsers.text import (
    FULL_STYLES,
    MAX_LINE,
    NUMERIC_DM,
    Amount,
    DateStyle,
    find_amounts,
    find_dates,
    guess_numeric_style,
    month_day,
    normalise,
    numeric_parts,
    read_amount,
    read_date,
    symbol_currency,
)
from app.ingestion.reconcile import TOLERANCE, Row, reconcile
from app.ingestion.sandbox.protocol import (
    Balance,
    ExtractedTransaction,
    ParserRef,
    Statement,
    StatementAccount,
)

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
#: Phrases that start a payment stub (a coupon to detach and send with a
#: payment): the rest of the page is no part of the statement.
STUB_MARKERS: Final = (
    "detach here",
    "please detach",
    "detach and return",
    "tear off here",
    "return this portion",
    "return this coupon",
    "payment coupon",
    "remittance slip",
    "detach and mail",
    "tear and return",
    "return the lower portion",
    "return the bottom portion",
)
#: A word before a statement date label that makes it another statement's
#: date (``Next Closing Date``).
OTHER_STATEMENT_WORDS: Final = frozenset({"next", "upcoming", "following"})
#: A marker after a row's date (``January 9 *``: a processing date).
_DATE_MARKS: Final = " *\u2020"
#: Yearless styles a month-named row date may be printed in.
NAMED_YEARLESS: Final[tuple[DateStyle, ...]] = ("mon_d", "d_mon")

#: ``Currency: CAD``; the code in capitals (``currency are`` is no code).
_CURRENCY: Final = re.compile(r"\b(?i:currency)\s*:?\s*([A-Z]{3})\b")
#: Line separators between the two dates of a period.
_RANGE: Final = re.compile(r"\s(?:to|through|until|-|\u2013)\s", re.IGNORECASE)


@lru_cache(maxsize=256)
def _label_pattern(label: str) -> re.Pattern[str]:
    words = (re.escape(word) for word in label.split())
    return re.compile(r"\b" + r"[^\w]{1,3}".join(words) + r"\b", re.IGNORECASE)


def label_end(text: str, label: str) -> int | None:
    """Where ``label`` (words, any punctuation between them) ends in ``text``."""
    match = _label_pattern(label).search(text[:MAX_LINE])
    return match.end() if match else None


#: Period candidates kept per statement (labelled lines, in order).
MAX_PERIOD_CANDIDATES: Final = 20


@dataclass
class PeriodCandidates:
    """Lines a period label was found on (the text after the label), in order.

    A label with nothing after it on its line offers the next line instead
    (``Statement period`` above ``01/01/2026 - 01/31/2026``).
    """

    texts: list[str] = field(default_factory=list)
    next_line: bool = False

    def scan(self, text: str, labels: Sequence[str]) -> None:
        if self.next_line:
            self.next_line = False
            self.add(text)
        for label in labels:
            end = label_end(text, label)
            if end is None:
                continue
            rest = text[end:]
            if any(ch.isdigit() for ch in rest):
                self.add(rest)
            else:
                self.next_line = True
            break

    def add(self, text: str) -> None:
        if len(self.texts) < MAX_PERIOD_CANDIDATES:
            self.texts.append(text)


def page_heads(view: DocumentView, lines: int = BAND_LINES) -> list[list[str]]:
    """The top ``lines`` lines of each page (the header band)."""
    heads: dict[int, list[str]] = {}
    for line in view.lines:
        head = heads.setdefault(line.page, [])
        if len(head) < lines:
            head.append(line.text)
    return [heads[page] for page in sorted(heads)]


def full_style(style: DateStyle) -> DateStyle:
    """The numeric style with a year a yearless one (``md``/``dm``) stands for."""
    if style == "md":
        return "mdy"
    if style == "dm":
        return "dmy"
    return style


_SWAPPED: Final[dict[DateStyle, DateStyle]] = {"mdy": "dmy", "dmy": "mdy", "md": "dm", "dm": "md"}


def follow_order(styles: Sequence[DateStyle], order: DateOrder) -> tuple[DateStyle, ...]:
    """``styles`` with each numeric style turned to ``order`` (``md``/``dm`` likewise)."""
    out: list[DateStyle] = []
    for style in styles:
        numeric = full_style(style)
        if numeric in NUMERIC_DM and numeric != order:
            style = _SWAPPED[style]
        if style not in out:
            out.append(style)
    return tuple(out)


def period_order(styles: Iterable[DateStyle]) -> DateOrder | None:
    """The day/month order the first numeric style of ``styles`` stands for."""
    for style in styles:
        if style in ("mdy", "md"):
            return "mdy"
        if style in ("dmy", "dm"):
            return "dmy"
    return None


@dataclass(frozen=True)
class PeriodRead:
    """A statement period as :func:`read_period` read it."""

    start: date
    end: date
    #: ``label``: next to a period label; ``header``: the header band.
    source: Literal["label", "header"]
    #: Confidence cap of the source (a band on one page is believed less).
    cap: float = 1.0
    #: The day/month order the printed period proves (a part above 12).
    order: DateOrder | None = None

    @property
    def span(self) -> tuple[date, date]:
        return self.start, self.end

    def confidence(self, base: float) -> float:
        """The parser's ``base`` confidence, at most the source's cap."""
        return min(base, self.cap)


def _proven_order(text: str) -> DateOrder | None:
    """The day/month order a numeric date of ``text`` proves, if exactly one."""
    settled = {h.settles for h in classify_dates(text) if h.settles is not None}
    return settled.pop() if len(settled) == 1 else None


def _first(styles: Sequence[DateStyle], order: DateOrder | None) -> list[DateStyle]:
    """``styles`` with the numeric style of ``order`` first and the other one dropped."""
    if order is None:
        return list(styles)
    wanted: DateStyle = "mdy" if order == "mdy" else "dmy"
    other: DateStyle = "dmy" if order == "mdy" else "mdy"
    return [wanted, *(s for s in styles if s not in (wanted, other))]


def read_period(
    candidates: Sequence[str],
    ordered: Sequence[DateStyle],
    view: DocumentView,
    *,
    certain: bool = True,
) -> PeriodRead | None:
    """The first candidate that reads as a period, else the header band (module doc).

    ``ordered`` are the styles to read numeric dates in; ``certain`` says
    whether their day/month order is known (pinned by the parser, or proven
    by the rows). When it is not, it is only a default: a period whose own
    values prove the other order (``01/07/2015 - 31/07/2015``) is read in
    that order, and :attr:`PeriodRead.order` tells the caller so its rows
    can follow.
    """
    order = period_order(ordered) if certain else None
    for text in candidates:
        proven = _proven_order(text)
        styles = _first(ordered, proven) if proven is not None else list(ordered)
        parts = _RANGE.split(text, maxsplit=1)
        if len(parts) == 2:
            start = find_dates(parts[0], styles)
            end = find_dates(parts[1], styles)
            if start and end and start[-1] <= end[0]:
                return PeriodRead(start[-1], end[0], "label", order=proven)
        found = find_dates(text, styles)
        if len(found) >= 2 and found[0] <= found[1]:
            return PeriodRead(found[0], found[1], "label", order=proven)
        ranges = find_ranges(text, order=order)
        if ranges:
            return PeriodRead(ranges[0].start, ranges[0].end, "label", order=ranges[0].order)
    band = header_band_range(page_heads(view), order=order)
    if band is None:
        return None
    return PeriodRead(band.start, band.end, "header", band_confidence(band), band.order)


@dataclass(frozen=True)
class Column:
    role: Role
    x0: float
    x1: float


#: A box on a page in page units (``x0, y0, x1, y1``).
BBox = tuple[float, float, float, float]


@dataclass
class _Row:
    page: int
    date_text: str
    description: list[str]
    amount: Amount | None
    debit: Amount | None
    credit: Amount | None
    balance: Amount | None
    ocr: float | None
    bbox: tuple[float, float, float, float]
    section_type: str | None
    memo: list[str] = field(default_factory=list)
    #: A row of a checks table (``Check No. | Date Paid | Amount``): a check paid.
    check: bool = False
    #: An informational foreign amount and its currency (``360.00``, ``US Dollar``).
    foreign: list[str] = field(default_factory=list)


@dataclass
class _Summary:
    opening: Amount | None = None
    closing: Amount | None = None
    opening_in_table: bool = False
    closing_in_table: bool = False
    period: PeriodCandidates = field(default_factory=PeriodCandidates)
    last4: str | None = None
    currency: str | None = None
    #: Pages and boxes of the dated lines skipped (no readable amount).
    skipped_boxes: list[tuple[int, BBox]] = field(default_factory=list)
    #: The summary's own totals (``Payments and Credits``, ``New Charges``).
    credits_total: Amount | None = None
    charges_total: Amount | None = None
    #: Texts a statement date (``Date``, ``Statement Date``) or the previous
    #: statement's date was printed in, in order.
    statement_dates: list[str] = field(default_factory=list)
    previous_dates: list[str] = field(default_factory=list)
    #: Label cells of the lines just above (``Previous Balance`` over its box),
    #: as ``(field, x0, x1)``, and the page and index of the last such line.
    above: list[tuple[str, float, float]] = field(default_factory=list)
    above_line: tuple[int, int] | None = None


@dataclass
class StatementRead:
    """One statement as :meth:`TableStatementParser.read_statement` read it."""

    statement: Statement | None = None
    pending_items: int = 0
    skipped_lines: int = 0
    warnings: list[str] = field(default_factory=list)
    #: Where the skipped lines are (page, box), for the review highlight.
    skipped_regions: list[tuple[int, BBox]] = field(default_factory=list)


@dataclass
class BalanceSection:
    """One account's lines of a document that numbers none (module doc, 7)."""

    lines: list[ViewLine]
    #: The title printed over the account's table (``FREE CHECKING - ID 0010``).
    title: str | None = None


#: The number ending a section's title: ``- ID 0010``, ``Share 01``, ``#0070``.
_SECTION_ID: Final = re.compile(
    r"[\s\u2013-]*(?:\b(?:id|share|suffix|sub|account|acct|number|no\.?)|#)\s*[:#]?\s*(\d{1,12})\s*$",
    re.IGNORECASE,
)
#: Lines above a table's header searched for the account's title.
SECTION_TITLE_LINES: Final = 3
#: Digits a title's number needs to stand for the account (its last four).
_LAST4_DIGITS: Final = 4
_MIN_SECTIONS: Final = 2
TITLE_MAX: Final = 200


def section_name(title: str | None) -> tuple[str | None, str | None]:
    """``(printed name, last four digits)`` of a section's title.

    ``FREE CHECKING - ID 0010`` is ``("FREE CHECKING", "0010")``; a shorter
    number (``Share 01``) names no account by itself and is dropped.
    """
    if not title:
        return None, None
    match = _SECTION_ID.search(title)
    if match is None:
        return title.strip()[:TITLE_MAX] or None, None
    name = title[: match.start()].strip(" -\u2013:") or None
    digits = match[1]
    return (
        name[:TITLE_MAX] if name else None,
        digits[-_LAST4_DIGITS:] if len(digits) >= _LAST4_DIGITS else None,
    )


def _min(*values: float | None) -> float | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


class TableStatementParser(DocumentParser):
    """Generic: any statement with a recognisable transaction table (module doc)."""

    name: ClassVar[str] = "statement_generic"
    version: ClassVar[str] = "5"
    kind: ClassVar[Literal["statement", "payslip"]] = "statement"
    #: The institution's name as accounts know it (account matching).
    display_name: ClassVar[str | None] = None
    #: Normalised phrases on the first page that identify the institution.
    fingerprints: ClassVar[tuple[str, ...]] = ()
    account_type: ClassVar[str | None] = None
    default_currency: ClassVar[str | None] = None
    #: Date formats of the rows; empty: guessed from the values.
    date_styles: ClassVar[tuple[DateStyle, ...]] = ()
    period_styles: ClassVar[tuple[DateStyle, ...]] = FULL_STYLES
    sign: ClassVar[Literal["bank", "card"]] = "bank"
    #: Confidence of a value read in the parser's declared format.
    base: ClassVar[float] = conf.GENERIC
    headers: ClassVar[Mapping[str, Role]] = HEADERS
    opening_labels: ClassVar[tuple[str, ...]] = (
        "beginning balance",
        "opening balance",
        "previous balance",
        "starting balance",
        "balance brought forward",
        "balance forward",
    )
    closing_labels: ClassVar[tuple[str, ...]] = (
        "ending balance",
        "closing balance",
        "new balance",
        "statement balance",
        "balance carried forward",
    )
    period_labels: ClassVar[tuple[str, ...]] = (
        "statement period",
        "billing period",
        "statement dates",
        "for the period",
        "statement from",
        "opening closing date",
        "period",
    )
    #: Labels of the summary's own totals (an equation-style summary:
    #: previous - payments and credits + new charges = new balance).
    credit_total_labels: ClassVar[tuple[str, ...]] = (
        "payments and credits",
        "payments and other credits",
        "payments credits",
    )
    charge_total_labels: ClassVar[tuple[str, ...]] = (
        "new charges",
        "purchases and other charges",
    )
    #: Labels of a statement date (no period printed); a cell reading only
    #: ``Date`` is one too.
    statement_date_labels: ClassVar[tuple[str, ...]] = (
        "statement date",
        "statement closing date",
        "closing date",
        "date of statement",
        "billing date",
        "cycle date",
    )
    previous_date_labels: ClassVar[tuple[str, ...]] = (
        "previous statement date",
        "last statement date",
        "previous closing date",
        "previous statement",
    )
    stub_markers: ClassVar[tuple[str, ...]] = STUB_MARKERS
    pending_markers: ClassVar[tuple[str, ...]] = ("pending",)
    #: Section headings (normalised prefix) -> transaction type (None: plain).
    sections: ClassVar[Mapping[str, str | None]] = {
        "fees charged": "fee",
        "fees": "fee",
        "interest charged": "int",
        "interest": "int",
        "purchases": None,
        "payments and other credits": None,
        "payments and credits": None,
        "deposits and other credits": None,
        "withdrawals and other debits": None,
        "transactions": None,
    }
    skip_prefixes: ClassVar[tuple[str, ...]] = ("total", "subtotal", "page ", "continued")
    #: Words in a description that type a row when its section does not.
    type_keywords: ClassVar[tuple[tuple[str, str], ...]] = (
        ("fee", "fee"),
        ("interest", "int"),
        ("dividend", "div"),
    )

    # -- detection --------------------------------------------------------------

    def columns(self, line: ViewLine) -> list[Column] | None:
        """The table columns ``line`` declares, or None when it is no header."""
        found: list[Column] = []
        roles: set[Role] = set()
        for cell in line.cells:
            role = self.headers.get(normalise(cell.text))
            if role is None or role in roles:
                continue
            roles.add(role)
            found.append(Column(role, cell.x0, cell.x1))
        if "date" in roles and roles & {"description", "check"} and roles & MONEY_ROLES:
            return sorted(found, key=lambda c: c.x0)
        return self._headless(line, found, roles)

    def _headless(
        self, line: ViewLine, found: Sequence[Column], roles: set[Role]
    ) -> list[Column] | None:
        """A heading naming the table over money columns only (module doc, "Header").

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

    def _header_like(self, line: ViewLine) -> bool:
        """Two or more cells, all header words, no amount: a table we do not read."""
        return (
            len(line.cells) >= 2
            and not find_amounts(line.text)
            and all(normalise(c.text) in self.headers for c in line.cells)
        )

    def has_table(self, view: DocumentView) -> bool:
        return self.lines_have_table(view.lines)

    def lines_have_table(self, lines: Iterable[ViewLine]) -> bool:
        return any(self.columns(line) is not None for line in lines)

    def fingerprint_score(self, view: DocumentView) -> float:
        if not self.fingerprints:
            return 0.0
        head = f" {view.head_text()} "
        hits = sum(1 for phrase in self.fingerprints if f" {normalise(phrase)} " in head)
        return hits / len(self.fingerprints)

    def detect(self, view: DocumentView) -> float:
        table = self.has_table(view)
        if not self.fingerprints:
            return 0.4 if table else 0.0
        share = self.fingerprint_score(view)
        if share == 0:
            return 0.0
        score = 0.5 + 0.4 * share
        return round(score + 0.1 if table else score * 0.6, 4)

    # -- reading ----------------------------------------------------------------

    def _assign(self, line: ViewLine, columns: Sequence[Column]) -> dict[Role, list[Cell]]:
        out: dict[Role, list[Cell]] = {}
        for cell in line.cells:
            best: Column | None = None
            best_overlap = 0.0
            for column in columns:
                overlap = cell.overlap(column.x0, column.x1)
                if overlap > best_overlap:
                    best, best_overlap = column, overlap
            if best is None:
                nearest = min(
                    columns, key=lambda c: abs((c.x0 + c.x1) / 2 - cell.centre), default=None
                )
                if nearest is not None and abs((nearest.x0 + nearest.x1) / 2 - cell.centre) < max(
                    3 * line.height, 30.0
                ):
                    best = nearest
            if best is not None:
                out.setdefault(best.role, []).append(cell)
        return out

    def _date_shaped(self, text: str) -> bool:
        if numeric_parts(text) is not None:
            return True
        if any(month_day(text, style) is not None for style in NAMED_YEARLESS):
            return True
        return any(read_date(text, style) is not None for style in ("d_mon_y", "mon_d_y", "ymd"))

    def _split_lead(self, cells: dict[Role, list[Cell]]) -> None:
        """A headless table's lead cells: a date first, then the description.

        The date is the lead's first cell; when the line has an amount, it
        may also be the first words of that cell (a date printed close to
        the description). A lead without a date is description text (a
        wrapped line).
        """
        lead = sorted(cells.pop("lead", []), key=lambda c: c.x0)
        if not lead:
            return
        rest: list[Cell] = lead
        if self._date_shaped(lead[0].text.strip(_DATE_MARKS)):
            cells["date"], rest = [lead[0]], lead[1:]
        elif any(
            self._amount([c]) is not None
            for role, found in cells.items()
            if role in MONEY_ROLES
            for c in found
        ):
            words = lead[0].word_cells()
            for size in (3, 2, 1):
                head = " ".join(w.text for w in words[:size]).strip(_DATE_MARKS)
                if len(words) > size and self._date_shaped(head):
                    cells["date"] = [join_cells(words[:size])]
                    rest = [join_cells(words[size:]), *lead[1:]]
                    break
        if rest:
            cells["description"] = [*rest, *cells.get("description", [])]

    def without_stubs(self, lines: Sequence[ViewLine]) -> list[ViewLine]:
        """``lines`` without payment stubs: a stub marker line (``Please detach
        here``, no amount) and the rest of its page, unless a table header
        follows on that page (reading resumes there)."""
        out: list[ViewLine] = []
        stub_page: int | None = None
        for line in lines:
            if stub_page == line.page:
                if self.columns(line) is None:
                    continue
                stub_page = None
            elif self._stub_marker(line):
                stub_page = line.page
                continue
            out.append(line)
        return out

    def summary_lines(self, lines: Sequence[ViewLine]) -> list[ViewLine]:
        """``lines`` without table headers, transaction rows and the rows'
        wrapped lines: what the statement says about itself, not its payees
        (a dated line in a table is a row; an undated one with description
        text only is a wrap)."""
        out: list[ViewLine] = []
        columns: list[Column] | None = None
        for line in lines:
            header = self.columns(line)
            if header is not None:
                columns = header
                continue
            if columns is not None and self._header_like(line):
                columns = None
            if columns is None:
                out.append(line)
                continue
            cells = self._assign(line, columns)
            self._split_lead(cells)
            date_text = " ".join(c.text for c in cells.get("date", [])).strip(_DATE_MARKS)
            if date_text and self._date_shaped(date_text):
                continue
            if cells and set(cells) <= {"description", "foreign"}:
                continue
            out.append(line)
        return out

    def _stub_marker(self, line: ViewLine) -> bool:
        """A line saying to detach the stub: without an amount, or starting
        with the marker (``Please detach here  Amount Due $ 12.00``; a
        transaction row starts with its date)."""
        norm = f" {line.norm} "
        found = [m for m in self.stub_markers if f" {m} " in norm]
        if not found:
            return False
        return not find_amounts(line.text) or any(norm.startswith(f" {m} ") for m in found)

    def _label_amount(self, line: ViewLine, labels: Sequence[str]) -> Amount | None:
        for label in labels:
            end = label_end(line.text, label)
            if end is None:
                continue
            amounts = find_amounts(line.text[end:])
            if amounts:
                return amounts[0]
        return None

    def _scan_summary(self, line: ViewLine, summary: _Summary) -> None:
        if summary.opening is None:
            summary.opening = self._label_amount(line, self.opening_labels)
        closing = self._label_amount(line, self.closing_labels)
        if closing is not None and summary.closing is None:
            summary.closing = closing
        if summary.credits_total is None:
            summary.credits_total = self._label_amount(line, self.credit_total_labels)
        if summary.charges_total is None:
            summary.charges_total = self._label_amount(line, self.charge_total_labels)
        summary.period.scan(line.text, self.period_labels)
        self._scan_dates(line, summary)
        self._scan_above(line, summary)
        if summary.last4 is None:
            summary.last4 = summary_last4(line.text)
        if summary.currency is None:
            match = _CURRENCY.search(line.text)
            if match is not None:
                summary.currency = match[1].upper()

    def _scan_dates(self, line: ViewLine, summary: _Summary) -> None:
        """Statement date and previous statement date candidates on ``line``."""
        text = line.text[:MAX_LINE]
        for out, value in self._date_label_values(text):
            if out == "previous":
                found = summary.previous_dates
            elif out == "statement":
                found = summary.statement_dates
            else:
                continue
            if any(ch.isdigit() for ch in value) and len(found) < MAX_PERIOD_CANDIDATES:
                found.append(value)
        if not self._bare_date_labels(line):
            return
        for cell, after in zip(line.cells, line.cells[1:], strict=False):
            if normalise(cell.text) == "date" and len(summary.statement_dates) < (
                MAX_PERIOD_CANDIDATES
            ):
                summary.statement_dates.append(after.text)

    def _date_label_values(self, text: str) -> list[tuple[str | None, str]]:
        """Each date label on ``text`` with its value: the text up to the next label.

        ``("previous" | "statement" | None, value)`` in reading order. A label
        inside a longer one is no label of its own (``Statement Date`` in
        ``Previous Statement Date``); one after ``Next`` (``Next Closing
        Date``) names another statement's date: None, it only ends the value
        before it.
        """
        found: list[tuple[int, int, str | None]] = []
        for kind, labels in (
            ("previous", self.previous_date_labels),
            ("statement", self.statement_date_labels),
        ):
            for label in labels:
                for match in _label_pattern(label).finditer(text):
                    found.append((match.start(), match.end(), kind))
        kept: list[tuple[int, int, str | None]] = []
        for start, end, label_kind in sorted(found, key=lambda f: (f[0], -f[1])):
            if any(s <= start and end <= e for s, e, _ in kept):
                continue
            before = text[:start].split()
            other = bool(before) and normalise(before[-1]) in OTHER_STATEMENT_WORDS
            kept.append((start, end, None if other else label_kind))
        return [
            (label_kind, text[end : kept[i + 1][0] if i + 1 < len(kept) else len(text)])
            for i, (_, end, label_kind) in enumerate(kept)
        ]

    def _bare_date_labels(self, line: ViewLine) -> bool:
        """A cell reading only ``Date`` labels the statement date on ``line``:
        no other cell is a column heading (``Date | Balance`` heads a table)."""
        return not any(
            normalise(c.text) in self.headers and normalise(c.text) != "date" for c in line.cells
        )

    def _label_field(self, text: str) -> str | None:
        """The summary field a label cell names (``Previous Balance``: ``opening``)."""
        norm = normalise(text)
        if norm == "date":
            return "statement_date"
        for name, labels in (
            ("previous_date", self.previous_date_labels),
            ("statement_date", self.statement_date_labels),
            ("opening", self.opening_labels),
            ("closing", self.closing_labels),
            ("credits", self.credit_total_labels),
            ("charges", self.charge_total_labels),
        ):
            if any(norm == label or norm.startswith(f"{label} ") for label in labels):
                return name
        return None

    def _scan_above(self, line: ViewLine, summary: _Summary) -> None:
        """Values printed under their labels (a row of labels over a row of boxes).

        Lines without digits collect their label cells (stacked label lines
        add up); the next line gives each label the value cell under it.
        """
        here = (line.page, line.index)
        follows = summary.above_line is not None and here == (
            summary.above_line[0],
            summary.above_line[1] + 1,
        )
        if not any(ch.isdigit() for ch in line.text):
            if not follows:
                summary.above = []
            bare = self._bare_date_labels(line)
            labels = [
                (f, c.x0, c.x1)
                for c in line.cells
                if (f := self._label_field(c.text)) and (bare or normalise(c.text) != "date")
            ]
            summary.above = [*summary.above, *labels][:20]
            summary.above_line = here
            return
        if follows:
            for name, x0, x1 in summary.above:
                cell = max(line.cells, key=lambda c: c.overlap(x0, x1))
                if cell.overlap(x0, x1) > 0:
                    self._fill(summary, name, cell.text)
        summary.above, summary.above_line = [], None

    @staticmethod
    def _fill(summary: _Summary, name: str, text: str) -> None:
        if name in ("statement_date", "previous_date"):
            out = summary.statement_dates if name == "statement_date" else summary.previous_dates
            if len(out) < MAX_PERIOD_CANDIDATES:
                out.append(text)
            return
        amounts = find_amounts(text)
        if not amounts:
            return
        if name == "opening" and summary.opening is None:
            summary.opening = amounts[0]
        elif name == "closing" and summary.closing is None:
            summary.closing = amounts[0]
        elif name == "credits" and summary.credits_total is None:
            summary.credits_total = amounts[0]
        elif name == "charges" and summary.charges_total is None:
            summary.charges_total = amounts[0]

    def _section(self, norm: str) -> tuple[bool, str | None] | None:
        """``(pending, type)`` when ``norm`` is a section heading."""
        for marker in self.pending_markers:
            if norm.startswith(marker):
                return True, None
        for heading, kind in self.sections.items():
            if norm == heading or norm.startswith(f"{heading} "):
                return False, kind
        return None

    def _collect(self, lines: Sequence[ViewLine]) -> tuple[list[_Row], _Summary, int, int, bool]:
        rows: list[_Row] = []
        summary = _Summary()
        pending_items = skipped = 0
        columns: list[Column] | None = None
        pending = False
        section_type: str | None = None
        last: _Row | None = None
        found_header = False
        for line in lines:
            header = self.columns(line)
            if header is not None:
                columns, pending, section_type, last = header, False, None, None
                found_header = True
                continue
            norm = line.norm
            if columns is not None and self._header_like(line):
                # The header of a table we do not read ("Date | Balance"): ours ends.
                columns, last = None, None
            if columns is None:
                self._scan_summary(line, summary)
                continue
            cells = self._assign(line, columns)
            self._split_lead(cells)
            date_text = " ".join(c.text for c in cells.get("date", [])).strip(_DATE_MARKS)
            if not date_text or not self._date_shaped(date_text):
                last = self._not_a_row(line, norm, cells, summary, last)
                section = self._section(norm) if last is None else None
                if section is not None:
                    pending, section_type = section
                continue
            description = [c.text for c in cells.get("description", [])]
            number = " ".join(c.text for c in cells.get("check", [])).strip(" *")
            is_check = bool(number) and not description
            if is_check:
                description = ["CHECK", number]
            desc_norm = normalise(" ".join(description))
            balance = self._amount(cells.get("balance"))
            amount = self._amount(cells.get("amount"))
            debit = self._amount(cells.get("debit"))
            credit = self._amount(cells.get("credit"))
            if amount is None and debit is None and credit is None:
                if any(desc_norm.startswith(lbl) for lbl in self.opening_labels):
                    if balance is not None and summary.opening is None:
                        summary.opening, summary.opening_in_table = balance, True
                    continue
                if any(desc_norm.startswith(lbl) for lbl in self.closing_labels):
                    if balance is not None:
                        summary.closing, summary.closing_in_table = balance, True
                    continue
                if pending:
                    continue
                skipped += 1
                summary.skipped_boxes.append((line.page, line.bbox))
                last = None
                continue
            if pending:
                pending_items += 1
                last = None
                continue
            used = [c for role_cells in cells.values() for c in role_cells]
            last = _Row(
                page=line.page,
                date_text=date_text,
                description=description,
                amount=amount,
                debit=debit,
                credit=credit,
                balance=balance,
                ocr=_min(*(c.conf for c in used)),
                bbox=line.bbox,
                section_type=section_type,
                check=is_check,
                foreign=[c.text for c in cells.get("foreign", [])],
            )
            rows.append(last)
        return rows, summary, pending_items, skipped, found_header

    def _not_a_row(
        self,
        line: ViewLine,
        norm: str,
        cells: dict[Role, list[Cell]],
        summary: _Summary,
        last: _Row | None,
    ) -> _Row | None:
        """Handle an undated line in the table; return the row a wrap may extend."""
        if any(norm.startswith(prefix.strip()) for prefix in self.skip_prefixes):
            return None
        # A credit/debit marker alone in a money column (``CR`` under the amount).
        marker = {
            normalise(" ".join(c.text for c in cells[role]))
            for role in MONEY_ROLES
            if role in cells
        }
        marked = marker in ({"cr"}, {"dr"})
        text_roles = {r for r in cells if not (marked and r in MONEY_ROLES)}
        only_text = text_roles <= {"description", "foreign"} and bool(text_roles or marked)
        if (
            last is not None
            and only_text
            and line.page == last.page
            and line.bbox[1] - last.bbox[3] < 1.5 * line.height
            and self._section(norm) is None
            and not find_amounts(line.text)
        ):
            last.memo.extend(c.text for c in cells.get("description", []))
            last.foreign.extend(c.text for c in cells.get("foreign", []))
            if marked and last.amount is not None and last.amount.credit is None:
                last.amount = replace(last.amount, credit=marker == {"cr"})
            last.bbox = (
                min(last.bbox[0], line.bbox[0]),
                last.bbox[1],
                max(last.bbox[2], line.bbox[2]),
                line.bbox[3],
            )
            return last
        carried = self._amount(cells.get("balance"))
        if carried is not None and not any(role in cells for role in MONEY_ROLES):
            described = normalise(" ".join(c.text for c in cells.get("description", [])))
            if any(described.startswith(label) for label in self.closing_labels):
                # An undated closing row of the table (``BALANCE CARRIED
                # FORWARD`` at the foot of every page): the last one printed
                # is the statement's, as for a dated one.
                summary.closing, summary.closing_in_table = carried, True
                return None
        self._scan_summary(line, summary)
        return None

    @staticmethod
    def _amount(cells: list[Cell] | None) -> Amount | None:
        if not cells:
            return None
        return read_amount(" ".join(c.text for c in cells))

    def _signed(self, row: _Row) -> Decimal | None:
        if row.check and row.amount is not None and row.amount.credit is None:
            return -abs(row.amount.value)  # a check paid takes money out
        if row.amount is not None:
            value = row.amount
            if value.credit is True:
                return abs(value.value)
            if value.credit is False:
                return -abs(value.value)
            if self.sign == "card":
                return abs(value.value) if value.negative_marked else -abs(value.value)
            return value.value
        if row.debit is not None and row.credit is None:
            return -abs(row.debit.value)
        if row.credit is not None and row.debit is None:
            return abs(row.credit.value)
        return None

    def _ledger_balance(self, amount: Amount | None) -> Decimal | None:
        if amount is None:
            return None
        value = amount.value
        if amount.credit is not None:
            # "CR": the institution owes the holder (positive in the ledger);
            # "DR": the holder owes (an overdraft, a card balance).
            return abs(value) if amount.credit else -abs(value)
        return -value if self.sign == "card" else value

    # -- dates ------------------------------------------------------------------

    def _row_styles(
        self, rows: Sequence[_Row], hint: str | None = None
    ) -> tuple[tuple[DateStyle, ...], bool]:
        if self.date_styles:
            return self.date_styles, True
        texts = [r.date_text for r in rows]
        numeric = [t for t in texts if numeric_parts(t) is not None]
        if numeric:
            style, certain = guess_numeric_style(numeric)
            if hint in NUMERIC_DM and (not certain or style == hint):
                # The classifier's fingerprint knows the institution's order.
                style, certain = hint, True
            # A yearless style reads full dates too: rows mixing both keep both.
            yearless = any((numeric_parts(t) or (0, 0, 0))[2] is None for t in numeric)
            if style is None:  # pragma: no cover - numeric values always give a style
                style = "mdy"
            if yearless:
                style = "md" if style == "mdy" else "dm"
            return (style, "ymd", "d_mon_y", "mon_d_y", *NAMED_YEARLESS), certain
        return ("d_mon_y", "mon_d_y", "ymd", *NAMED_YEARLESS), True

    def _period(
        self,
        summary: _Summary,
        styles: tuple[DateStyle, ...],
        view: DocumentView,
        certain: bool,
    ) -> PeriodRead | None:
        ordered = self._period_styles(styles)
        return read_period(summary.period.texts, ordered, view, certain=certain)

    def _period_styles(self, styles: Sequence[DateStyle]) -> list[DateStyle]:
        ordered: list[DateStyle] = [s for s in self.period_styles if s in FULL_STYLES]
        for style in styles:
            numeric = full_style(style)
            if numeric in NUMERIC_DM and numeric in ordered:
                ordered.remove(numeric)
                ordered.insert(0, numeric)
        return ordered

    @staticmethod
    def _first_date(texts: Sequence[str], styles: Sequence[DateStyle]) -> date | None:
        for text in texts:
            found = find_dates(text, styles)
            if found:
                return found[0]
        return None

    def _statement_date(
        self, summary: _Summary, styles: Sequence[DateStyle]
    ) -> tuple[date, date | None] | None:
        """The statement date and the previous statement's date (module doc, "Summary")."""
        ordered = self._period_styles(styles)
        end = self._first_date(summary.statement_dates, ordered)
        if end is None:
            return None
        previous = self._first_date(summary.previous_dates, ordered)
        return end, previous if previous is not None and previous < end else None

    @staticmethod
    def _row_date(
        row: _Row,
        styles: Sequence[DateStyle],
        window: tuple[date, date] | None,
        strict: bool,
    ) -> date | None:
        for style in styles:
            posted = read_date(row.date_text, style, window=window, strict=strict)
            if posted is not None:
                return posted
        return None

    # -- the statement ----------------------------------------------------------

    def _type(self, row: _Row, description: str) -> str | None:
        if row.section_type:
            return row.section_type
        words = set(normalise(description).split())
        for keyword, kind in self.type_keywords:
            if keyword in words:
                return kind
        return None

    def parse(
        self, view: DocumentView, *, threshold: float, date_order: str | None = None
    ) -> ParseOutcome:
        lines = self.without_stubs(view.lines)
        split = self._accounts(lines)
        if split is not None:
            return self._parse_accounts(view, split, threshold=threshold, date_order=date_order)
        sections = self.balance_sections(lines)
        if sections is not None:
            return self._parse_sections(view, *sections, threshold=threshold, date_order=date_order)
        read = self.read_statement(view, lines, threshold=threshold, date_order=date_order)
        outcome = ParseOutcome(
            pending_items=read.pending_items,
            skipped_lines=read.skipped_lines,
            warnings=list(read.warnings),
            skipped_regions=list(read.skipped_regions),
        )
        if read.statement is not None:
            outcome.statements.append(read.statement)
        return outcome

    # -- several accounts -------------------------------------------------------

    def _accounts(self, lines: Sequence[ViewLine]) -> Segmentation | None:
        """The document's accounts when two or more have a transaction table each."""
        found = segment(lines)
        if len(found.accounts) < 2:
            return None
        if sum(1 for a in found.accounts if self.lines_have_table(a.lines)) < 2:
            return None
        return found

    def _parse_accounts(
        self,
        view: DocumentView,
        split: Segmentation,
        *,
        threshold: float,
        date_order: str | None,
    ) -> ParseOutcome:
        """One statement per account (INGESTION.md "Several accounts")."""
        outcome = ParseOutcome()
        rows, preamble, pending, skipped, _ = self._collect(split.preamble)
        outcome.pending_items += pending
        outcome.skipped_lines += skipped + len(rows)
        outcome.skipped_regions.extend(preamble.skipped_boxes)
        outcome.skipped_regions.extend((row.page, row.bbox) for row in rows)
        warnings: list[str] = ["rows_outside_account"] if rows else []
        for account in split.accounts:
            if not self.lines_have_table(account.lines):
                warnings.append("account_without_tables")
                continue
            read = self.read_statement(
                view,
                account.lines,
                threshold=threshold,
                date_order=date_order,
                period_texts=preamble.period.texts,
                account=account,
            )
            outcome.pending_items += read.pending_items
            outcome.skipped_lines += read.skipped_lines
            outcome.skipped_regions.extend(read.skipped_regions)
            warnings.extend(read.warnings)
            if read.statement is not None:
                outcome.statements.append(read.statement)
        outcome.warnings.extend(dict.fromkeys(warnings))
        return outcome

    # -- accounts without a number ----------------------------------------------

    def _opening_row(self, cells: Mapping[Role, list[Cell]]) -> Amount | None:
        """The balance of an opening-balance row (``04/01 Beginning Balance 812.44``)."""
        money: tuple[Role, ...] = ("amount", "debit", "credit")
        if any(self._amount(cells.get(role)) is not None for role in money):
            return None
        description = normalise(" ".join(c.text for c in cells.get("description", [])))
        if not any(description.startswith(label) for label in self.opening_labels):
            return None
        return self._amount(cells.get("balance"))

    def _section_title(self, lines: Sequence[ViewLine], below: int, floor: int) -> int | None:
        """The index of the title over line ``below``: the nearest line of words above it."""
        for index in range(below - 1, max(floor, below - 1 - SECTION_TITLE_LINES), -1):
            line = lines[index]
            if line.page != lines[below].page:
                break
            if (
                any(c.isalpha() for c in line.text)
                and not find_amounts(line.text)
                and numeric_parts(line.cells[0].text) is None
                and self.columns(line) is None
            ):
                return index
        return None

    def balance_sections(
        self, lines: Sequence[ViewLine]
    ) -> tuple[list[ViewLine], list[BalanceSection]] | None:
        """``(preamble, sections)`` when the balance starts over (module doc, 7), else None."""
        columns: list[Column] | None = None
        header: int | None = None
        balance: Decimal | None = None
        #: (opening row, the header it stands under) of each account found.
        starts: list[tuple[int, int]] = []
        for index, line in enumerate(lines):
            found = self.columns(line)
            if found is not None:
                columns, header = found, index
                continue
            if columns is None or header is None:
                continue
            if self._header_like(line):
                columns = None
                continue
            cells = self._assign(line, columns)
            self._split_lead(cells)
            opening = self._opening_row(cells)
            if opening is not None:
                if balance is None or opening.value != balance:
                    starts.append((index, header))
                balance = opening.value
                continue
            printed = self._amount(cells.get("balance"))
            if printed is not None:
                balance = printed.value
        if len(starts) < _MIN_SECTIONS:  # one account, as before
            return None
        bounds: list[int] = []
        titles: list[str | None] = []
        floor = -1
        for row, head in starts:
            # The table header is the account's own unless the account before used it.
            top = head if head > floor else row
            title = self._section_title(lines, top, floor)
            bounds.append(title if title is not None else top)
            titles.append(lines[title].text if title is not None else None)
            floor = row
        sections: list[BalanceSection] = []
        for position, (_, head) in enumerate(starts):
            start = bounds[position]
            end = bounds[position + 1] if position + 1 < len(bounds) else len(lines)
            body = list(lines[start:end])
            if not start <= head < end:
                # The header is printed once, over the first account only.
                body.insert(0, lines[head])
            sections.append(BalanceSection(body, titles[position]))
        return list(lines[: bounds[0]]), sections

    def _parse_sections(
        self,
        view: DocumentView,
        preamble_lines: Sequence[ViewLine],
        sections: Sequence[BalanceSection],
        *,
        threshold: float,
        date_order: str | None,
    ) -> ParseOutcome:
        """One statement per section (module doc, 7)."""
        outcome = ParseOutcome()
        _, preamble, pending, skipped, _ = self._collect(preamble_lines)
        outcome.pending_items += pending
        outcome.skipped_lines += skipped
        outcome.skipped_regions.extend(preamble.skipped_boxes)
        warnings: list[str] = ["accounts_by_balance"]
        for section in sections:
            read = self.read_statement(
                view,
                section.lines,
                threshold=threshold,
                date_order=date_order,
                period_texts=preamble.period.texts,
            )
            outcome.pending_items += read.pending_items
            outcome.skipped_lines += read.skipped_lines
            outcome.skipped_regions.extend(read.skipped_regions)
            warnings.extend(read.warnings)
            if read.statement is None:
                continue
            name, last4 = section_name(section.title)
            kind, subtype = account_kind(name)
            kind = self.account_type or kind
            pages = [line.page for line in section.lines]
            outcome.statements.append(
                read.statement.model_copy(
                    update={
                        "account": read.statement.account.model_copy(
                            update={
                                "name": name,
                                # Never the member number printed once for every share.
                                "last4": last4,
                                "account_type": kind,
                                "subtype": subtype if kind is not None else None,
                            }
                        ),
                        "confidence": {
                            **read.statement.confidence,
                            "account_number": self.base if last4 else conf.MISSING,
                        },
                        "first_page": min(pages),
                        "last_page": max(pages),
                    }
                )
            )
        outcome.warnings.extend(dict.fromkeys(warnings))
        return outcome

    # -- one statement ----------------------------------------------------------

    def statement_account(
        self, summary_last4: str | None, currency: str | None, account: AccountSegment | None
    ) -> StatementAccount:
        """The statement's account: the parser's type, else what the document names."""
        info = account.info if account is not None else None
        kind = self.account_type or (info.account_type if info is not None else None)
        subtype = info.subtype if info is not None and info.account_type == kind else None
        return StatementAccount(
            account_type=kind,
            last4=info.last4 if info is not None else summary_last4,
            currency=currency,
            name=info.name if info is not None else None,
            institution=self.display_name,
            subtype=subtype,
        )

    def read_statement(
        self,
        view: DocumentView,
        lines: Sequence[ViewLine],
        *,
        threshold: float,
        date_order: str | None = None,
        period_texts: Sequence[str] = (),
        account: AccountSegment | None = None,
    ) -> StatementRead:
        """The statement ``lines`` hold (all of ``view``'s, or one account's).

        ``period_texts`` are period candidates from outside the lines (the
        document's overview) tried after the lines' own; ``account`` is the
        account the lines belong to (:mod:`app.ingestion.parsers.accounts`).
        ``statement`` is None when no transaction table header is found.
        """
        rows, summary, pending_items, skipped, found_header = self._collect(
            self.without_stubs(lines)
        )
        outcome = StatementRead(
            pending_items=pending_items,
            skipped_lines=skipped,
            skipped_regions=list(summary.skipped_boxes),
        )
        if not found_header:
            outcome.warnings.append("no_table_header")
            return outcome
        summary.period = PeriodCandidates([*summary.period.texts, *period_texts])
        styles, certain = self._row_styles(rows, date_order)
        # The day/month order is known when pinned or proven by numeric rows.
        order_known = bool(self.date_styles) or (
            certain and any(numeric_parts(r.date_text) is not None for r in rows)
        )
        read = self._period(summary, styles, view, order_known)
        if read is not None and not order_known and read.order is not None:
            # The rows fit both orders (or have none); the printed period proves one.
            styles, certain = follow_order(styles, read.order), True
        period = read.span if read is not None else None
        anchor = self._statement_date(summary, styles) if read is None else None
        window, strict = period, False
        if anchor is not None:
            # Only a statement date: a yearless row is the latest date not after it.
            window, strict = (anchor[0] - timedelta(days=364), anchor[0]), True
        dates = [self._row_date(row, styles, window, strict) for row in rows]
        if anchor is not None:
            later = sum(1 for d in dates if d is not None and d > anchor[0])
            if later and 2 * later >= sum(1 for d in dates if d is not None):
                # Half the rows or more printed with a year after the
                # "statement date": it is none.
                anchor = None
                dates = [self._row_date(row, styles, None, False) for row in rows]
            elif later:
                # A few outliers (a stub's dated line read as a row): skipped.
                dates = [None if d is not None and d > anchor[0] else d for d in dates]
        if anchor is not None:
            period = anchored_period(*anchor, [d for d in dates if d is not None])
        transactions: list[ExtractedTransaction] = []
        fields: list[dict[str, float]] = []
        ledger_rows: list[Row] = []
        for row, posted in zip(rows, dates, strict=True):
            amount = self._signed(row)
            if posted is None or amount is None:
                outcome.skipped_lines += 1
                outcome.skipped_regions.append((row.page, row.bbox))
                continue
            date_conf = self.base if certain else conf.GUESSED
            if period is not None and not (period[0] <= posted <= period[1]):
                date_conf = min(date_conf, conf.INFERRED)
            payee = " ".join(row.description).strip() or None
            score = {
                "posted_date": conf.scaled(date_conf, row.ocr),
                "amount": conf.scaled(self.base, row.ocr),
                "payee": conf.scaled(self.base if payee else conf.MISSING, row.ocr),
            }
            memo = row_memo(row)
            fields.append(score)
            ledger_rows.append(Row(amount, self._ledger_balance(row.balance)))
            transactions.append(
                ExtractedTransaction(
                    posted=posted,
                    amount=amount,
                    payee=payee[:4096] if payee else None,
                    memo=memo[:4096] if memo else None,
                    type=self._type(row, " ".join(row.description)),
                    page=row.page,
                    bbox=row.bbox,
                )
            )
        statement_conf: dict[str, float] = {}
        opening = self._ledger_balance(summary.opening)
        closing = self._ledger_balance(summary.closing)
        statement_conf["opening_balance"] = self.base if opening is not None else conf.MISSING
        statement_conf["closing_balance"] = self.base if closing is not None else conf.MISSING
        if ledger_rows and opening is None and ledger_rows[0].balance is not None:
            opening = ledger_rows[0].balance - ledger_rows[0].amount
            statement_conf["opening_balance"] = conf.DERIVED
        if ledger_rows and closing is None and ledger_rows[-1].balance is not None:
            closing = ledger_rows[-1].balance
            statement_conf["closing_balance"] = conf.DERIVED
        result = reconcile(opening, closing, ledger_rows)
        if result.status == "ok" and totals_disagree(summary, opening, closing, ledger_rows):
            # The summary's own credits and charges do not match the rows'.
            result = result.model_copy(update={"status": "mismatch"})
            outcome.warnings.append("summary_totals_mismatch")
        for index in result.running_mismatches:
            fields[index]["amount"] = min(fields[index]["amount"], conf.CONTRADICTED)
        if result.status == "ok" and any(r.balance is not None for r in ledger_rows):
            # Each amount agrees with the printed running balance: confirmed.
            for index, checked in enumerate(ledger_rows):
                if checked.balance is not None and index not in result.running_mismatches:
                    fields[index]["amount"] = 1.0
        cap = {"mismatch": conf.UNRECONCILED, "unavailable": conf.UNCHECKED}.get(result.status)
        if cap is not None:
            for score in fields:
                score["reconciliation"] = cap
        if read is not None:
            statement_conf["period"] = read.confidence(self.base)
        elif anchor is not None and period is not None:
            # A printed statement date ends it; its start is the cycle's.
            statement_conf["period"] = min(self.base, conf.DERIVED)
        elif transactions:
            period = (min(t.posted for t in transactions), max(t.posted for t in transactions))
            # Inferred from the rows: no surer than their dates.
            statement_conf["period"] = min(
                [conf.INFERRED, *(score["posted_date"] for score in fields)]
            )
        statement_conf["account_number"] = self.base if summary.last4 else conf.MISSING
        # A printed code, else the amounts' own symbol, else the layout's currency.
        currency = (
            summary.currency
            or symbol_currency(line.text for line in lines)
            or self.default_currency
        )
        statement_conf["currency"] = self.base if currency else conf.MISSING
        statement_conf["reconciliation"] = (
            1.0 if result.status == "ok" else (cap if cap is not None else 1.0)
        )
        transactions = [
            txn.model_copy(
                update={
                    "confidence": min(score.values()),
                    "low_confidence": conf.low_fields(score, threshold),
                }
            )
            for txn, score in zip(transactions, fields, strict=True)
        ]
        if view.truncated:
            outcome.warnings.append("truncated")
        if not certain:
            outcome.warnings.append("date_order_guessed")
        date_order = styles[0] if styles[0] in ("mdy", "dmy", "ymd") else None
        if styles[0] == "md":
            date_order = "mdy"
        elif styles[0] == "dm":
            date_order = "dmy"
        outcome.statement = Statement(
            source_format=view.source_format,
            account=self.statement_account(summary.last4, currency, account),
            period_start=period[0] if period else None,
            period_end=period[1] if period else None,
            opening_balance=(
                Balance(amount=opening, as_of=period[0] if period else None)
                if opening is not None
                else None
            ),
            ledger_balance=(
                Balance(amount=closing, as_of=period[1] if period else None)
                if closing is not None
                else None
            ),
            transactions=transactions,
            date_order=date_order,  # type: ignore[arg-type]
            truncated=view.truncated,
            first_page=account.first_page if account is not None else None,
            last_page=account.last_page if account is not None else None,
            parser=ParserRef(
                name=self.name,
                version=self.version,
                institution=self.institution,
                kind="statement",
            ),
            confidence={k: round(v, 4) for k, v in statement_conf.items()},
            reconciliation=result,
        )
        return outcome


def month_before(day: date) -> date:
    """The same day a month earlier (the month's last day when it has none)."""
    year, month = (day.year, day.month - 1) if day.month > 1 else (day.year - 1, 12)
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def anchored_period(end: date, previous: date | None, rows: Sequence[date]) -> tuple[date, date]:
    """A period from a statement date (module doc, "Summary").

    It ends on the statement date and starts the day after the previous
    statement's date when one is printed; otherwise the day after the same
    date a month before (a monthly cycle), or the earliest row when one is
    older (a charge posted late in the cycle before).
    """
    if previous is not None:
        return previous + timedelta(days=1), end
    start = month_before(end) + timedelta(days=1)
    earliest = min((d for d in rows if d <= end), default=start)
    return min(start, earliest), end


def row_memo(row: _Row) -> str | None:
    """The row's wrapped lines, then its informational foreign amount."""
    memo = " ".join(row.memo).strip()
    foreign = " ".join(row.foreign).strip()
    if foreign:
        memo = f"{memo}; foreign amount {foreign}" if memo else f"foreign amount {foreign}"
    return memo or None


def totals_disagree(
    summary: _Summary, opening: Decimal | None, closing: Decimal | None, rows: Sequence[Row]
) -> bool:
    """The summary's credits and charges totals contradict the rows.

    Only when the summary is a whole equation (opening + credits - charges =
    closing, in the ledger convention): then the rows' money in must be the
    credits and their money out the charges. A summary with more terms
    (fees, interest printed apart) is not compared.
    """
    if opening is None or closing is None:
        return False
    if summary.credits_total is None or summary.charges_total is None:
        return False
    credits, charges = abs(summary.credits_total.value), abs(summary.charges_total.value)
    if abs(opening + credits - charges - closing) > TOLERANCE:
        return False
    money_in = sum((r.amount for r in rows if r.amount > 0), Decimal(0))
    money_out = -sum((r.amount for r in rows if r.amount < 0), Decimal(0))
    return abs(money_in - credits) > TOLERANCE or abs(money_out - charges) > TOLERANCE


#: Words on a card statement's first page (the generic card parser's cue),
#: read outside the transaction rows (:meth:`TableStatementParser.summary_lines`:
#: a checking account's ``CREDIT CARD PAYMENT`` row is no cue).
CARD_VOCABULARY: Final = (
    "card ending",
    "credit card",
    "card statement",
    "minimum payment",
    "credit limit",
    "corporate card",
    "charge card",
)
#: Card words that other statements print too (``CARDMEMBER SERV`` as a
#: payee, ``new charges`` on a fee notice): a cue only two together.
WEAK_CARD_VOCABULARY: Final = ("new charges", "cardmember", "card member")


class CardStatementParser(TableStatementParser):
    """Generic credit card statement: charges positive, credits ``CR`` or negative.

    Detected (0.45, above ``statement_generic``) when page 1 has a table and,
    outside its rows, a card word (:data:`CARD_VOCABULARY`) or two of the
    weak ones (:data:`WEAK_CARD_VOCABULARY`).
    """

    name: ClassVar[str] = "card_statement_generic"
    sign: ClassVar[Literal["bank", "card"]] = "card"
    account_type: ClassVar[str | None] = "creditcard"

    def detect(self, view: DocumentView) -> float:
        if self.fingerprints:
            return super().detect(view)
        page_one = [line for line in view.lines if line.page <= 1]
        head = f" {' '.join(line.norm for line in self.summary_lines(page_one))} "
        strong = any(f" {word} " in head for word in CARD_VOCABULARY)
        # Each weak word once (``Card Member`` and ``Cardmember`` are one).
        weak = len({w.replace(" ", "") for w in WEAK_CARD_VOCABULARY if f" {w} " in head})
        if not strong and weak < 2:
            return 0.0
        # Above the bank-statement generic parser for the same table.
        return 0.45 if self.has_table(view) else 0.0

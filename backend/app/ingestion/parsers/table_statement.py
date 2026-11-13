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
   amount is a transaction; a dated line whose description starts with an
   opening or closing label is a balance line whatever column its figure
   is in (:meth:`TableStatementParser._balance_row`: the balance column,
   else the rightmost money column), never a transaction (with a figure in
   a money column, only when the description is the label, a date and
   small words: ``New Balance Outlet 114`` is a payee and a row): the first
   opening and the last closing one are the statement's; a following line with only
   description text (and foreign-column text) continues the previous row's
   memo (wrapped text, a category line); a ``CR``/``DR`` alone in the
   amount column of that line marks the row's amount, as one after the
   amount does. Dates may carry a mark after them (``January 9 *``: a
   processing date) and may be month names without a year
   (``December 16``, ``16 Dec``); numeric rows with and without a year
   in one table are both read.
   **Cells off their columns.** A figure that overlaps no heading and lies
   near none (figures right-aligned in a wide column under a heading
   printed at its left) goes to the numeric column that starts left of it,
   when that column has no figure of its own on the line. A date printed
   so close to the description that the two are one cell is split off it
   (:meth:`TableStatementParser._split_fused_date`), on a line with a
   figure only: a wrapped line that starts with a date stays a wrap, and
   so does a line that starts with a range of dates (``01/01 through
   01/31 interest earned``). With
   a figure in both the debit and the credit column, a zero is no figure
   (``$250.00 | -$0.00``); two amounts, or two zeros, say nothing and the
   row is counted as skipped. A year-less numeric text is a date only when
   it can be a day and a month (``2.80`` is a figure).
   Section headings set the row type (fees, interest) or start a *pending*
   section whose rows are counted but not imported (they are not posted and
   not part of the balances). Totals, page footers and "continued" lines are
   skipped.
   **Direction from the title.** A section title (:data:`SECTION_TITLES`:
   ``Deposits and additions``, ``Electronic withdrawals``, ``Checks paid``,
   ``Payments and credits``, ``Purchases``, ``Fees``...) also says which way
   the money of its rows went, for a bank account, a card or both
   (:class:`SectionRule`). It is read between the rows of a table and
   **above a header**: the nearest of the two lines before it, on its page,
   that has letters and no amount. Only an amount of the ``Amount`` column
   printed with no sign at all takes that direction: a minus, a plus,
   brackets, ``CR``/``DR``/``DB`` and a debit or credit column always say
   more than a title. A title naming both sides (``Deposits and
   withdrawals``) gives none; a line with an amount, of more than
   :data:`SECTION_TITLE_WORDS` words, or with one of
   :data:`NOT_TITLE_WORDS` (``Interest summary``) is no title. A header
   without a title, on a later page than the last row and with the columns
   of the table being read, is that table's header **printed again**: the
   direction and the type carry on, unless the line over it speaks of a
   section (:data:`SECTION_WORDS`: a title the table does not know,
   ``Direct Deposits``, or one with its total on the line). Any other header
   without a title starts a table with neither. Right under a row, a line
   of description text that starts with a title's key is the row's wrapped
   text unless it is the whole key or starts left of the description column
   (:meth:`TableStatementParser._heading_under_row`). A dated line without
   a readable amount is counted as skipped.
   **A date printed once per day.** An undated line of a table with
   description text and exactly one figure in a money column, no total, no
   balance label and no section title, at most :data:`CARRY_LINES` line
   heights below the row (or such a line) above it on its page, is a
   *candidate* row with the date printed above it. Candidates become rows,
   in printed order, when a dated row follows in the same table (its header
   printed again on a later page does not end it); those left when the
   table ends are rows only when the statement has :data:`MIN_CARRIED`
   such rows already, and otherwise what they always were: summary lines
   under the table. Their date scores at most ``confidence.DERIVED``
   (warning ``dates_carried``).
   **Found and not read.** Any other undated line with a figure in a money
   column that a dated row follows (two figures in one column, a line too
   far below the row above) is counted as skipped, as is each date-led
   line ending in an amount printed **under a section title and no
   header** (outside any table, up to the next header, block title or page
   end; warning ``rows_without_header:<n>``): nothing is imported from
   those.
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
   The longest label on a line is the one printed: ``statement balance``
   inside ``Previous Statement Balance`` closes nothing, and a closing
   label after ``previous``, ``last`` or ``prior`` is another statement's.
   A label printed without its figure (``Beginning Balance on`` over its
   date) takes the figure printed alone on the line next to it, the next
   one first, at most a line and a half away (confidence
   ``confidence.DERIVED``), when no balance was read after its label.
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
   ``rows_outside_account``; the same for rows above the first section of
   7). :meth:`TableStatementParser.read_statement`
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
currency, fingerprint phrases and the institution's name. Such a parser
claims a document only when every one of its phrases is on page 1
(:meth:`TableStatementParser.detect`).
"""

from __future__ import annotations

import re
from calendar import monthrange
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import ClassVar, Final, Literal, NamedTuple

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
from app.ingestion.parsers.blocks import unfold_blocks
from app.ingestion.parsers.statement_header import HEADERS as HEADERS
from app.ingestion.parsers.statement_header import MONEY_ROLES as MONEY_ROLES
from app.ingestion.parsers.statement_header import NUMERIC_ROLES as NUMERIC_ROLES
from app.ingestion.parsers.statement_header import TABLE_HEADING_WORDS as TABLE_HEADING_WORDS
from app.ingestion.parsers.statement_header import Column as Column
from app.ingestion.parsers.statement_header import Role as Role
from app.ingestion.parsers.statement_header import header_like, read_header, stack_headers
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
from app.ingestion.reconcile import TOLERANCE, Row, doubtful_rows, reconcile
from app.ingestion.sandbox.protocol import (
    Balance,
    ExtractedTransaction,
    ParserRef,
    Statement,
    StatementAccount,
)

#: Titles of the blocks a bank or card statement prints side by side
#: (normalised; :func:`block_title`). Deliberately few and plain: a line of
#: two such titles, or one of two words or more alone at the right, splits
#: the page there, and a table's own column headings must never do that.
BLOCK_TITLES: Final = frozenset(
    {
        "account summary",
        "summary of account",
        "summary of your account",
        "statement summary",
        "balance summary",
        "daily balance summary",
        "daily ending balance",
        "daily ledger balance",
        "interest summary",
        "fee summary",
        "rewards summary",
        "payment information",
        "account information",
        "important information",
        "account activity",
        "transaction detail",
        "transaction history",
        "transactions",
        "deposits and other credits",
        "withdrawals and other debits",
        "checks paid",
        "electronic withdrawals",
        "atm and debit card withdrawals",
    }
)


def block_title(norm: str) -> bool:
    """``norm`` (a normalised cell) is the title of a block printed beside another."""
    text = norm[: -len(" continued")] if norm.endswith(" continued") else norm
    return text in BLOCK_TITLES


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
#: Words before a closing label that make it the statement before's
#: (``Previous Ending Balance``).
EARLIER_STATEMENT_WORDS: Final = frozenset({"previous", "last", "prior"})
#: Words a balance label may be followed by on a line that leaves its figure
#: to the line beside it (``Beginning Balance on``, ``Ending Balance as of``):
#: a longer line is a sentence.
LABEL_TAIL_WORDS: Final = 3
#: Words a balance line may print after its label beside a date (``Ending
#: Balance as of January 31``, ``Beginning balance this period``). Any other
#: word after the label of a line with a figure in a money column makes it a
#: payee (``New Balance Outlet 114``).
LABEL_SMALL_WORDS: Final = frozenset(
    {
        "on", "as", "of", "at", "for", "from", "to", "the", "this", "last", "previous",
        "prior", "period", "statement", "cycle", "month", "date", "page",
    }
)  # fmt: skip
#: Line heights between a label and the figure printed beside it.
ADJACENT_LINES: Final = 1.5
#: A marker after a row's date (``January 9 *``: a processing date).
_DATE_MARKS: Final = " *\u2020"
#: How far left of the date heading a cell may start and still begin with the
#: row's date (dates printed a little outside their column).
FUSED_DATE_SLACK: Final = 25.0
#: Line heights an undated row may start below the row above it and still
#: take that row's date (a date printed once per day).
CARRY_LINES: Final = 2.5
#: Rows with a carried date a statement needs before undated lines at the
#: end of a table are rows too (the layout is proven).
MIN_CARRIED: Final = 2
#: The most words the date of a line outside a table is looked for in.
LEAD_DATE_WORDS: Final = 3
#: Words between the two dates of a range (``01/01 through 01/31``).
RANGE_WORDS: Final = frozenset({"to", "through", "thru", "until", "-", "\u2013", "\u2014"})
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


#: A box on a page in page units (``x0, y0, x1, y1``).
BBox = tuple[float, float, float, float]


#: Which way the money of a row went: into the account or out of it.
Direction = Literal["in", "out"]


class SectionRule(NamedTuple):
    """What a section title says about the rows under it."""

    #: The direction of an unsigned amount on a bank account's statement...
    bank: Direction | None
    #: ...and on a card's (``Payments`` is money in there and says nothing
    #: on a bank account; ``Deposits`` the other way round).
    card: Direction | None
    #: The transaction type of the rows (``fee``, ``int``), if the title names one.
    kind: str | None


def _titles(
    keys: str, bank: Direction | None, card: Direction | None, kind: str | None = None
) -> dict[str, SectionRule]:
    return {key.strip(): SectionRule(bank, card, kind) for key in keys.split(",")}


#: Section titles (normalised) and what each says. A title matches a key
#: when, a trailing ``continued`` or ``cont`` dropped, it is the key or
#: starts with the key and a space; the longest key wins (:func:`section_title`).
SECTION_TITLES: Final[Mapping[str, SectionRule]] = {
    **_titles(
        "deposits and additions, deposits and other additions, deposits and other credits,"
        " deposits and credits, deposits credits, deposits, additions, electronic deposits,"
        " electronic credits",
        "in",
        None,
    ),
    **_titles("other credits, credits", "in", "in"),
    **_titles(
        "withdrawals and other debits, withdrawals and other subtractions,"
        " withdrawals and debits, withdrawals and subtractions, withdrawals debits,"
        " withdrawals, other withdrawals, electronic withdrawals, electronic debits,"
        " atm and debit card withdrawals, atm debit card withdrawals, subtractions,"
        " checks paid, checks cleared, checks",
        "out",
        None,
    ),
    **_titles("other debits, debits, purchases and adjustments, purchases", "out", "out"),
    **_titles(
        "payments and other credits, payments and credits, payments credits, payments",
        None,
        "in",
    ),
    **_titles("new charges, cash advances, balance transfers", None, "out"),
    **_titles("fees charged, service fees, service charges, fees", "out", "out", "fee"),
    **_titles("interest charged", "out", "out", "int"),
    **_titles("interest paid, interest earned", "in", None, "int"),
    **_titles("interest", None, "out", "int"),
    **_titles("transactions", None, None),
}
#: The most words a section title has: a longer line is a sentence.
SECTION_TITLE_WORDS: Final = 8
#: A word of these makes a line something else than the title of rows
#: (``Interest summary``, ``Interest rate``, ``Fees information``).
NOT_TITLE_WORDS: Final = frozenset({"summary", "calculation", "rate", "information"})
#: Printed after the title of a section that runs on from the page before.
_CONTINUED_WORDS: Final = frozenset({"continued", "cont"})
#: Words after a title's key that name the other side (``Deposits and
#: withdrawals``, ``Withdrawals and deposits``): the title has no direction.
_OUT_WORDS: Final = frozenset(
    {"withdrawals", "debits", "subtractions", "checks", "fees", "charges", "purchases"}
)
_IN_WORDS: Final = frozenset({"deposits", "credits", "additions", "payments"})
#: Words that make a line over a header the line of a section (its title,
#: known or not, or its total): a header under such a line is no page header
#: printed again.
SECTION_WORDS: Final = _IN_WORDS | _OUT_WORDS
#: Lines above a header searched for its section's title.
TITLE_LINES_ABOVE: Final = 2


class SectionTitle(NamedTuple):
    """A section title as :func:`section_title` read it."""

    #: The key of the titles' table it matched.
    key: str
    rule: SectionRule
    #: ``continued`` or ``cont`` was printed after it.
    continued: bool


def section_title(
    norm: str, titles: Mapping[str, SectionRule] = SECTION_TITLES
) -> SectionTitle | None:
    """The section title ``norm`` (a normalised line) is, or None.

    The caller checks that the line holds no amount (the normalised text
    cannot tell ``12.00`` from two numbers).
    """
    words = norm.split()
    if len(words) > SECTION_TITLE_WORDS or not NOT_TITLE_WORDS.isdisjoint(words):
        return None
    continued = len(words) > 1 and words[-1] in _CONTINUED_WORDS
    text = " ".join(words[:-1] if continued else words)
    key = max(
        (key for key in titles if text == key or text.startswith(f"{key} ")),
        key=len,
        default=None,
    )
    if key is None:
        return None
    rule = titles[key]
    side = rule.bank or rule.card
    other = _OUT_WORDS if side == "in" else _IN_WORDS if side == "out" else frozenset()
    if not other.isdisjoint(text[len(key) :].split()):
        rule = SectionRule(None, None, rule.kind)
    return SectionTitle(key, rule, continued)


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
    #: Which way the row's section says its money went, for this parser's
    #: kind of account (None: no title, or one that does not say).
    direction: Direction | None = None
    #: The number of the row's section: it goes up each time a section starts
    #: or ends at a header, and stays over a page break the section runs across.
    section: int = 0
    #: The line printed no date: ``date_text`` is the date of the row above.
    carried: bool = False


@dataclass
class _Summary:
    opening: Amount | None = None
    closing: Amount | None = None
    opening_in_table: bool = False
    closing_in_table: bool = False
    #: The balance is the figure alone on the line beside its label.
    opening_adjacent: bool = False
    closing_adjacent: bool = False
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
    #: Rows that took the date printed above them.
    carried: int = 0
    #: Date-led lines under a section title and no header (counted, not read).
    headerless: int = 0


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
#: The number leading a section's title: ``ID 0040 - SHARE DRAFT``, ``Share 02: HOLIDAY CLUB``.
_SECTION_ID_FIRST: Final = re.compile(
    r"\s*(?:(?:id|share|suffix|sub|account|acct|no\.?)(?![a-z])|#)\s*[:#]?\s*(\d{1,12})"
    r"\s*[-\u2013:]\s*(\S.*)",
    re.IGNORECASE | re.DOTALL,
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
    number (``Share 01``) names no account by itself and is dropped. The
    number may come first, before a dash or a colon: ``ID 0040 - SHARE
    DRAFT`` is ``("SHARE DRAFT", "0040")``.
    """
    if not title:
        return None, None
    title = title[: TITLE_MAX * 2]
    leading = _SECTION_ID_FIRST.fullmatch(title)
    match = _SECTION_ID.search(title) if leading is None else None
    if leading is not None:
        name = leading[2].strip(" -\u2013:") or None
        digits = leading[1]
    elif match is not None:
        name = title[: match.start()].strip(" -\u2013:") or None
        digits = match[1]
    else:
        return title.strip()[:TITLE_MAX] or None, None
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
    version: ClassVar[str] = "6"
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
        "previous statement balance",
        "last statement balance",
        "balance last statement",
        "prior balance",
    )
    closing_labels: ClassVar[tuple[str, ...]] = (
        "ending balance",
        "closing balance",
        "new balance",
        "statement balance",
        "balance carried forward",
        "balance this statement",
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
    #: Section titles and what each says of its rows (direction, type).
    section_titles: ClassVar[Mapping[str, SectionRule]] = SECTION_TITLES
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
        return read_header(line, self.headers)

    def _header_like(self, line: ViewLine) -> bool:
        """Two or more cells, all header words, no amount: a table we do not read."""
        return header_like(line, self.headers)

    def prepare(self, lines: Sequence[ViewLine]) -> list[ViewLine]:
        """``lines`` as the reader wants them: stacked headers made one line
        (:func:`~app.ingestion.parsers.statement_header.stack_headers`)."""
        return stack_headers(lines, self.headers)

    def has_table(self, view: DocumentView) -> bool:
        return self.lines_have_table(self.prepare(view.lines))

    def lines_have_table(self, lines: Iterable[ViewLine]) -> bool:
        return any(self.columns(line) is not None for line in lines)

    def fingerprint_score(self, view: DocumentView) -> float:
        if not self.fingerprints:
            return 0.0
        head = f" {view.head_text()} "
        hits = sum(1 for phrase in self.fingerprints if f" {normalise(phrase)} " in head)
        return hits / len(self.fingerprints)

    def detect(self, view: DocumentView) -> float:
        """Generic: 0.4 with a table. A parser written for one institution
        needs **every** phrase of its fingerprint on page 1 (1.0 with a
        table, 0.54 without): one of them (``current account statement``,
        ``card ending in``) is what any bank or issuer prints, and its
        statement is not this institution's."""
        table = self.has_table(view)
        if not self.fingerprints:
            return 0.4 if table else 0.0
        if self.fingerprint_score(view) < 1.0:
            return 0.0
        return 1.0 if table else 0.54

    # -- reading ----------------------------------------------------------------

    def _assign(self, line: ViewLine, columns: Sequence[Column]) -> dict[Role, list[Cell]]:
        out: dict[Role, list[Cell]] = {}
        unclaimed: list[Cell] = []
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
            elif read_amount(cell.text) is not None:
                unclaimed.append(cell)
        for cell in unclaimed:
            # A figure no heading claims (right-aligned in a wide column whose
            # heading is printed at its left): the numeric column that starts
            # left of it, unless that column has its figure already.
            left = [column for column in columns if column.x0 <= cell.centre]
            if not left:
                continue
            home = max(left, key=lambda column: column.x0)
            if home.role in NUMERIC_ROLES and home.role not in out:
                out[home.role] = [cell]
        return out

    def _date_shaped(self, text: str) -> bool:
        parts = numeric_parts(text)
        if parts is not None:
            first, second, year = parts
            if year is not None:
                return True
            # Without a year: a day and a month in one order (``2.80`` is a figure).
            return (1 <= first <= 12 and 1 <= second <= 31) or (
                1 <= second <= 12 and 1 <= first <= 31
            )
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

    def _split_fused_date(self, cells: dict[Role, list[Cell]], columns: Sequence[Column]) -> None:
        """A date printed so close to the description that the two are one cell.

        Only when the date column got no cell: the line's leftmost cell, if
        it starts at the date column (up to :data:`FUSED_DATE_SLACK` left of
        its heading), loses its first one to three words when they are
        date-shaped; they become the date cell and the rest goes in front of
        the description. In a table with one money column that got no cell,
        a last word reading as an amount becomes the money cell (the whole
        row printed as one cell).

        Nothing changes unless the line then has a figure in a numeric
        column: a wrapped line that starts with a date stays a wrap. And a
        date that starts a range (``01/01 through 01/31 interest earned``,
        a summary line with its figure under ``Amount``) is no row's date.
        """
        if cells.get("date"):
            return
        date_column = next((c for c in columns if c.role == "date"), None)
        placed = [cell for found in cells.values() for cell in found]
        if date_column is None or not placed:
            return
        first = min(placed, key=lambda cell: cell.x0)
        if not date_column.x0 - FUSED_DATE_SLACK <= first.x0 <= date_column.x1:
            return
        words = first.word_cells()
        for size in (3, 2, 1):
            head = " ".join(w.text for w in words[:size]).strip(_DATE_MARKS)
            if len(words) > size and self._date_shaped(head):
                break
        else:
            return
        rest = list(words[size:])
        if self._range_led(first.text):
            return
        split: dict[Role, list[Cell]] = {
            other: [c for c in found if c is not first] for other, found in cells.items()
        }
        split = {other: found for other, found in split.items() if found}
        money = [c.role for c in columns if c.role in MONEY_ROLES]
        if len(money) == 1 and money[0] not in split and read_amount(rest[-1].text) is not None:
            split[money[0]] = [rest.pop()]
        if not any(
            other in NUMERIC_ROLES and self._amount(found) is not None
            for other, found in split.items()
        ):
            return
        split["date"] = [join_cells(words[:size])]
        if rest:
            split["description"] = [join_cells(rest), *split.get("description", [])]
        cells.clear()
        cells.update(split)

    def _range_led(self, text: str) -> bool:
        """``text`` starts with a range of dates: a date (its first one to
        :data:`LEAD_DATE_WORDS` words), a range word (:data:`RANGE_WORDS`)
        and a second date. The word alone starts payees too (``01/05 To
        Savings 0041``)."""
        words = text.split()

        def dated(at: int) -> list[int]:
            """The sizes of the dates that start at ``words[at]``."""
            return [
                size
                for size in range(1, LEAD_DATE_WORDS + 1)
                if at + size <= len(words)
                and self._date_shaped(" ".join(words[at : at + size]).strip(_DATE_MARKS))
            ]

        return any(
            len(words) > size and words[size].lower() in RANGE_WORDS and bool(dated(size + 1))
            for size in dated(0)
        )

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
            self._split_fused_date(cells, columns)
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

    def _label_amount(
        self,
        line: ViewLine,
        labels: Sequence[str],
        *,
        inside: Sequence[str] = (),
        earlier: bool = False,
    ) -> Amount | None:
        """The first amount after one of ``labels`` on ``line``, the longest first.

        A match lying inside a match of one of ``inside`` is no match
        (``statement balance`` in ``Previous Statement Balance``); with
        ``earlier``, neither is one after ``previous``, ``last`` or ``prior``.
        """
        text = line.text[:MAX_LINE]
        outer = [match.span() for label in inside for match in _label_pattern(label).finditer(text)]
        for label in sorted(labels, key=len, reverse=True):
            for match in _label_pattern(label).finditer(text):
                start, end = match.span()
                if any(s <= start and end <= e for s, e in outer):
                    continue
                before = text[:start].split()
                if earlier and before and normalise(before[-1]) in EARLIER_STATEMENT_WORDS:
                    continue
                amounts = find_amounts(text[end:])
                if amounts:
                    return amounts[0]
        return None

    def _scan_summary(self, line: ViewLine, summary: _Summary) -> None:
        if summary.opening is None:
            summary.opening = self._label_amount(line, self.opening_labels)
        closing = self._label_amount(
            line, self.closing_labels, inside=self.opening_labels, earlier=True
        )
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

    def _section(self, norm: str) -> tuple[bool, SectionRule | None] | None:
        """``(pending, rule)`` when ``norm`` is a section heading
        (:func:`section_title`; a pending section has no rule)."""
        for marker in self.pending_markers:
            if norm.startswith(marker):
                return True, None
        title = section_title(norm, self.section_titles)
        return (False, title.rule) if title is not None else None

    def _title(self, line: ViewLine) -> SectionTitle | None:
        """The section title ``line`` is: no amount on it (``Fees 12.00`` is
        the summary's figure, not a title)."""
        if find_amounts(line.text):
            return None
        return section_title(line.norm, self.section_titles)

    def _title_above(self, lines: Sequence[ViewLine], index: int) -> SectionTitle | None:
        """The title of the header at ``lines[index]``: the nearest of the
        :data:`TITLE_LINES_ABOVE` lines before it on its page that has
        letters and no amount, when that line is a title."""
        header = lines[index]
        for above in reversed(lines[max(0, index - TITLE_LINES_ABOVE) : index]):
            if above.page != header.page:
                break
            if any(ch.isalpha() for ch in above.text) and not find_amounts(above.text):
                return self._title(above)
        return None

    def _section_line_above(self, lines: Sequence[ViewLine], index: int) -> bool:
        """The nearest line with letters among the :data:`TITLE_LINES_ABOVE`
        before the header at ``lines[index]``, on its page, speaks of a
        section (:data:`SECTION_WORDS`): a title the table does not know
        (``Direct Deposits``) or one printed with its total (``Deposits and
        additions 900.00``). Page furniture (the bank, the account, the
        period, the page number) does not."""
        header = lines[index]
        for above in reversed(lines[max(0, index - TITLE_LINES_ABOVE) : index]):
            if above.page != header.page:
                break
            if any(ch.isalpha() for ch in above.text):
                return not SECTION_WORDS.isdisjoint(above.norm.split())
        return False

    def _collect(self, lines: Sequence[ViewLine]) -> tuple[list[_Row], _Summary, int, int, bool]:
        rows: list[_Row] = []
        summary = _Summary()
        pending_items = skipped = 0
        columns: list[Column] | None = None
        pending = False
        # The section being read: its title (None: untitled), its number and
        # the page of the last row read.
        title: SectionTitle | None = None
        section = 0
        row_page: int | None = None
        last: _Row | None = None
        found_header = False
        # A date printed once per day (module doc): the dated line or
        # candidate above, the candidates held back, the other undated lines
        # with a figure, and the lines whose summary scan waits with them.
        anchor: _Row | None = None
        held: list[_Row] = []
        loose: list[tuple[int, BBox]] = []
        waiting: list[tuple[ViewLine, bool]] = []
        # The page a section title was printed on with no header after it yet.
        titled: int | None = None

        def runs_on(found: SectionTitle, page: int) -> bool:
            """``found`` names the section being read once more: marked
            ``continued``, or printed again on a later page."""
            return (
                title is not None
                and (found.key, found.rule) == (title.key, title.rule)
                and (found.continued or (row_page is not None and page > row_page))
            )

        def scan(line: ViewLine) -> None:
            """Read ``line`` as a summary line, after the candidates held."""
            if held:
                waiting.append((line, False))
            else:
                self._scan_summary(line, summary)

        def settle(*, dated: bool) -> None:
            """A dated row follows the lines held back (``dated``), or the table ends."""
            nonlocal skipped, row_page
            promote = bool(held) and (dated or summary.carried >= MIN_CARRIED)
            if promote:
                rows.extend(held)
                summary.carried += len(held)
                row_page = held[-1].page
            if dated:
                skipped += len(loose)
                summary.skipped_boxes.extend(loose)
            queued = list(waiting)
            held.clear()
            loose.clear()
            waiting.clear()
            for queued_line, of_candidate in queued:
                # Left alone, a candidate and its wrapped lines are what they
                # were before: lines of the summary.
                if not (promote and of_candidate):
                    self._scan_summary(queued_line, summary)

        for index, line in enumerate(lines):
            header = self.columns(line)
            if header is not None:
                before = section
                above = self._title_above(lines, index)
                if above is not None:
                    if not runs_on(above, line.page):
                        section += 1
                    title = above
                elif not (
                    columns is not None
                    and row_page is not None
                    and line.page > row_page
                    and [c.role for c in header] == [c.role for c in columns]
                    and not self._section_line_above(lines, index)
                ):
                    # No title, and not the header of the table being read
                    # printed again on a later page: a table of its own.
                    title = None
                    section += 1
                if section != before:
                    settle(dated=False)
                columns, pending, last, anchor, titled = header, False, None, None, None
                found_header = True
                continue
            norm = line.norm
            if columns is not None and self._header_like(line):
                # The header of a table we do not read ("Date | Balance"): ours ends.
                settle(dated=False)
                columns, last, anchor = None, None, None
            if columns is None:
                if self._title(line) is not None:
                    titled = line.page
                elif titled != line.page or block_title(norm) or self._header_like(line):
                    titled = None
                elif self._date_led_amount(line):
                    # A row of a section printed without a header: not read.
                    skipped += 1
                    summary.skipped_boxes.append((line.page, line.bbox))
                    summary.headerless += 1
                self._scan_summary(line, summary)
                continue
            cells = self._assign(line, columns)
            self._split_lead(cells)
            self._split_fused_date(cells, columns)
            date_text = " ".join(c.text for c in cells.get("date", [])).strip(_DATE_MARKS)
            if not date_text or not self._date_shaped(date_text):
                figures = 0 if pending else self._undated_figures(line, norm, cells)
                if figures == 1 and self._carries(line, cells, anchor) and anchor is not None:
                    last = anchor = self._row(
                        line, cells, anchor.date_text, title, section, carried=True
                    )
                    held.append(last)
                    waiting.append((line, True))
                    continue
                if figures:
                    loose.append((line.page, line.bbox))
                last = self._not_a_row(line, norm, cells, summary, last, scan, columns)
                if last is not None:
                    if held and last is held[-1]:
                        waiting.append((line, True))
                    continue
                if any(norm.startswith(marker) for marker in self.pending_markers):
                    settle(dated=False)
                    pending, title, anchor = True, None, None
                    section += 1
                elif (found := self._title(line)) is not None:
                    if not runs_on(found, line.page):
                        settle(dated=False)
                        section += 1
                    pending, title, anchor = False, found, None
                continue
            row = self._row(line, cells, date_text, title, section)
            balance_row = self._balance_row(normalise(" ".join(row.description)), cells)
            if balance_row is not None:
                # A balance line, whatever column its figure is in: never a row.
                kind, figure = balance_row
                if kind == "opening":
                    if figure is not None and summary.opening is None:
                        summary.opening, summary.opening_in_table = figure, True
                elif figure is not None:
                    summary.closing, summary.closing_in_table = figure, True
                if not pending:
                    # A dated line of the table: the undated rows above it
                    # are of the day printed over them, as before a dated row.
                    settle(dated=True)
                anchor = None if pending else row
                continue
            if row.amount is None and row.debit is None and row.credit is None:
                if pending:
                    continue
                settle(dated=True)
                skipped += 1
                summary.skipped_boxes.append((line.page, line.bbox))
                last, anchor = None, row
                continue
            settle(dated=True)
            row_page = line.page
            if pending:
                pending_items += 1
                last = anchor = None
                continue
            last = anchor = row
            rows.append(row)
        settle(dated=False)
        self._adjacent_balances(lines, summary)
        return rows, summary, pending_items, skipped, found_header

    def _row(
        self,
        line: ViewLine,
        cells: Mapping[Role, list[Cell]],
        date_text: str,
        title: SectionTitle | None,
        section: int,
        *,
        carried: bool = False,
    ) -> _Row:
        """The row ``line`` is, its cells in their columns, under ``title``."""
        description = [c.text for c in cells.get("description", [])]
        number = " ".join(c.text for c in cells.get("check", [])).strip(" *")
        is_check = bool(number) and not description
        if is_check:
            description = ["CHECK", number]
        rule = title.rule if title is not None else None
        used = [c for role_cells in cells.values() for c in role_cells]
        return _Row(
            page=line.page,
            date_text=date_text,
            description=description,
            amount=self._amount(cells.get("amount")),
            debit=self._amount(cells.get("debit")),
            credit=self._amount(cells.get("credit")),
            balance=self._amount(cells.get("balance")),
            ocr=_min(*(c.conf for c in used)),
            bbox=line.bbox,
            section_type=rule.kind if rule is not None else None,
            check=is_check,
            foreign=[c.text for c in cells.get("foreign", [])],
            direction=self._direction(rule),
            section=section,
            carried=carried,
        )

    def _undated_figures(self, line: ViewLine, norm: str, cells: Mapping[Role, list[Cell]]) -> int:
        """How many figures an undated line of a table has in its money
        columns; 0 for a total, a pending heading, a section title, a line
        that starts with a range of dates and one that starts with a balance
        label (none of them is a row)."""
        figures = sum(
            1
            for role, found in cells.items()
            if role in MONEY_ROLES
            for cell in found
            if read_amount(cell.text) is not None
        )
        if not figures:
            return 0
        if any(norm.startswith(prefix.strip()) for prefix in self.skip_prefixes):
            return 0
        if any(norm.startswith(marker) for marker in self.pending_markers):
            return 0
        if self._title(line) is not None:
            return 0
        if self._range_led(line.text):
            # ``01/01 through 01/31 interest earned``: a line of the summary.
            return 0
        described = normalise(" ".join(c.text for c in cells.get("description", [])))
        labels = (*self.opening_labels, *self.closing_labels)
        if any(norm.startswith(label) or described.startswith(label) for label in labels):
            return 0
        return figures

    def _carries(
        self, line: ViewLine, cells: Mapping[Role, list[Cell]], anchor: _Row | None
    ) -> bool:
        """An undated line with one figure is a candidate row (module doc):
        it has description text, its figure reads in one money column, and
        it starts at most :data:`CARRY_LINES` line heights below ``anchor``
        (the dated line or candidate above it) on the same page."""
        if anchor is None or line.page != anchor.page:
            return False
        if line.bbox[1] - anchor.bbox[3] > CARRY_LINES * line.height:
            return False
        if not any(ch.isalpha() for c in cells.get("description", []) for ch in c.text):
            return False
        read = [
            role
            for role, found in cells.items()
            if role in MONEY_ROLES and self._amount(found) is not None
        ]
        return len(read) == 1

    def _date_led_amount(self, line: ViewLine) -> bool:
        """``line`` starts with a date (its first one to
        :data:`LEAD_DATE_WORDS` words) and its last cell reads as an amount."""
        if read_amount(line.cells[-1].text) is None:
            return False
        words = line.text.split()
        return any(
            len(words) > size and self._date_shaped(" ".join(words[:size]).strip(_DATE_MARKS))
            for size in range(LEAD_DATE_WORDS, 0, -1)
        )

    def _balance_row(
        self, desc_norm: str, cells: Mapping[Role, list[Cell]]
    ) -> tuple[Literal["opening", "closing"], Amount | None] | None:
        """The kind and figure of a dated line whose description starts with
        an opening or closing label: the balance cell when it reads, else
        the rightmost money cell that reads (``Beginning Balance`` printed
        under ``Amount`` or ``Deposit``).

        With a figure in a money column the line could be a transaction, so
        the description has to be the label and no more than a balance line
        says after it (:meth:`_label_only`): a payee that starts with a
        label (``New Balance Outlet 114``, ``Opening Balance Brewing Co``)
        is a row, and its amount is no balance.
        """
        roles: tuple[Role, ...] = ("amount", "debit", "credit")
        money = sorted(
            (cell for role in roles for cell in cells.get(role, [])),
            key=lambda cell: cell.x0,
            reverse=True,
        )
        paid = next((a for c in money if (a := read_amount(c.text)) is not None), None)
        kind: Literal["opening", "closing"] | None = None
        for found, labels in (("opening", self.opening_labels), ("closing", self.closing_labels)):
            if any(
                desc_norm.startswith(label)
                and (paid is None or self._label_only(desc_norm[len(label) :]))
                for label in labels
            ):
                kind = "opening" if found == "opening" else "closing"
                break
        if kind is None:
            return None
        figure = self._amount(cells.get("balance"))
        return kind, figure if figure is not None else paid

    @staticmethod
    def _label_only(tail: str) -> bool:
        """``tail`` (normalised, what a line prints after a balance label)
        names no payee: nothing, or a date and words that are each one of
        :data:`LABEL_SMALL_WORDS` or a month."""
        words = [word for word in tail.split() if any(ch.isalpha() for ch in word)]
        return all(
            word in LABEL_SMALL_WORDS or month_day(f"{word} 1", "mon_d") is not None
            for word in words
        )

    def _label_alone(self, line: ViewLine) -> Literal["opening", "closing"] | None:
        """The balance a line names without printing its figure: it starts
        with the label, a few words at most follow, and it holds no amount."""
        norm = line.norm
        for kind, labels in (("opening", self.opening_labels), ("closing", self.closing_labels)):
            for label in labels:
                if norm != label and not norm.startswith(f"{label} "):
                    continue
                tail = len(norm.split()) - len(label.split())
                if tail <= LABEL_TAIL_WORDS and not find_amounts(line.text):
                    return "opening" if kind == "opening" else "closing"
        return None

    def _adjacent_balances(self, lines: Sequence[ViewLine], summary: _Summary) -> None:
        """Balances printed beside their label, for those no label gave.

        A label line without a figure takes the amount of an amount-only
        line (all of it reads as one amount) that is its neighbour in line order on
        its page, the tops at most :data:`ADJACENT_LINES` line heights
        apart: the next line first, else the one before.
        """
        if summary.opening is not None and summary.closing is not None:
            return
        for index, line in enumerate(lines):
            kind = self._label_alone(line)
            if kind is None or getattr(summary, kind) is not None:
                continue
            for other in (index + 1, index - 1):
                if not 0 <= other < len(lines):
                    continue
                beside = lines[other]
                if (
                    beside.page != line.page
                    or abs(beside.bbox[1] - line.bbox[1]) > ADJACENT_LINES * line.height
                ):
                    continue
                figure = read_amount(beside.text)
                if figure is None:
                    continue
                if kind == "opening":
                    summary.opening, summary.opening_adjacent = figure, True
                else:
                    summary.closing, summary.closing_adjacent = figure, True
                break

    def _not_a_row(
        self,
        line: ViewLine,
        norm: str,
        cells: dict[Role, list[Cell]],
        summary: _Summary,
        last: _Row | None,
        scan: Callable[[ViewLine], None],
        columns: Sequence[Column] = (),
    ) -> _Row | None:
        """Handle an undated line in the table; return the row a wrap may extend.

        ``scan`` reads a line of the summary (at once, or after the
        candidate rows held back before it: :meth:`_collect`). A line right
        under a row that starts with a section title's key is the row's
        wrapped text unless it is printed as a heading
        (:meth:`_heading_under_row`, by the table's ``columns``)."""
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
            and not self._heading_under_row(line, norm, columns)
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
        scan(line)
        return None

    def _heading_under_row(self, line: ViewLine, norm: str, columns: Sequence[Column]) -> bool:
        """A line of description text right under a row is a heading, not the
        row's second line: a pending marker, or a section title that is its
        whole key (``Fees``, ``Deposits continued``) or starts left of the
        description column, where no wrapped text is printed. ``Checks 2
        items branch 14`` under a payee is the row's memo: read as a title
        it would turn the unsigned rows after it into money out."""
        found = self._section(norm)
        if found is None:
            return False
        title = section_title(norm, self.section_titles)
        if found[0] or title is None:
            return True
        if len(norm.split()) - title.continued == len(title.key.split()):
            return True
        described = next((c for c in columns if c.role == "description"), None)
        return described is None or line.bbox[0] < described.x0 - line.height

    @staticmethod
    def _amount(cells: list[Cell] | None) -> Amount | None:
        if not cells:
            return None
        return read_amount(" ".join(c.text for c in cells))

    def _direction(self, rule: SectionRule | None) -> Direction | None:
        """What ``rule`` says for this parser's kind of account."""
        if rule is None:
            return None
        return rule.card if self.sign == "card" else rule.bank

    def _signed(self, row: _Row) -> Decimal | None:
        if row.check and row.amount is not None and row.amount.credit is None:
            return -abs(row.amount.value)  # a check paid takes money out
        if row.amount is not None:
            value = row.amount
            if value.credit is True:
                return abs(value.value)
            if value.credit is False:
                return -abs(value.value)
            if row.direction is not None and not (value.negative_marked or value.positive_marked):
                # Printed with no sign at all: its section's title says which way.
                return abs(value.value) if row.direction == "in" else -abs(value.value)
            if self.sign == "card":
                return abs(value.value) if value.negative_marked else -abs(value.value)
            return value.value
        debit, credit = row.debit, row.credit
        if debit is not None and credit is not None:
            # Both printed: a zero is no figure (``250.00 | 0.00``). Two
            # amounts, or two zeros, say nothing: unread, and counted.
            if (debit.value == 0) == (credit.value == 0):
                return None
            if debit.value == 0:
                debit = None
            else:
                credit = None
        if debit is not None:
            return -abs(debit.value)
        if credit is not None:
            return abs(credit.value)
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
        lines = self.without_stubs(self.prepare(view.lines))
        split = self._accounts(lines)
        if split is not None:
            return self._parse_accounts(
                view, split, threshold=threshold, date_order=date_order, prepared=True
            )
        sections = self.balance_sections(lines)
        if sections is not None:
            return self._parse_sections(
                view, *sections, threshold=threshold, date_order=date_order, prepared=True
            )
        read = self.read_statement(
            view, lines, threshold=threshold, date_order=date_order, prepared=True
        )
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
        prepared: bool = False,
    ) -> ParseOutcome:
        """One statement per account (INGESTION.md "Several accounts")."""
        outcome = ParseOutcome()
        rows, preamble, pending, skipped, _ = self._collect(split.preamble)
        outcome.pending_items += pending
        outcome.skipped_lines += skipped + len(rows)
        outcome.skipped_regions.extend(preamble.skipped_boxes)
        outcome.skipped_regions.extend((row.page, row.bbox) for row in rows)
        warnings: list[str] = ["rows_outside_account"] if rows else []
        if preamble.headerless:
            warnings.append(f"rows_without_header:{preamble.headerless}")
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
                prepared=prepared,
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
        """The balance of an opening-balance row (``04/01 Beginning Balance
        812.44``), in the balance column or a money column (:meth:`_balance_row`)."""
        description = normalise(" ".join(c.text for c in cells.get("description", [])))
        found = self._balance_row(description, cells)
        if found is None or found[0] != "opening":
            return None
        return found[1]

    def _title_cell(self, line: ViewLine) -> str | None:
        """The text of ``line``'s first cell when it can name an account.

        The first cell is judged, not the line: a title may have a figure
        beside it (``PRIMARY SHARE ID 0000   Dividends Paid YTD: 4.12``). It
        has a letter, holds no amount and starts with no date; the line is
        no header; and it is no total, page or balance, period or
        statement-date label (``Total dividends 4.12`` names nothing), nor a
        label with its figure (``Dividends Paid YTD:   0.00``).
        """
        if not line.cells or self.columns(line) is not None:
            return None
        text = line.cells[0].text.strip()
        if not any(c.isalpha() for c in text) or find_amounts(text):
            return None
        words = text.split()
        if any(
            len(words) >= size and self._date_shaped(" ".join(words[:size]).strip(_DATE_MARKS))
            for size in range(LEAD_DATE_WORDS, 0, -1)
        ):
            return None
        norm = normalise(text)
        labels = (
            *self.opening_labels,
            *self.closing_labels,
            *self.period_labels,
            *self.statement_date_labels,
        )
        if any(norm.startswith(prefix.strip()) for prefix in self.skip_prefixes):
            return None
        if any(norm.startswith(label) for label in labels):
            return None
        if text.endswith(":") and any(
            any(c.isdigit() for c in cell.text) and not any(c.isalpha() for c in cell.text)
            for cell in line.cells[1:]
        ):
            return None
        return text

    def _section_title(self, lines: Sequence[ViewLine], below: int, floor: int) -> int | None:
        """The index of the title over line ``below``: the nearest line above it
        whose first cell can name an account (:meth:`_title_cell`)."""
        for index in range(below - 1, max(floor, below - 1 - SECTION_TITLE_LINES), -1):
            line = lines[index]
            if line.page != lines[below].page:
                break
            if self._title_cell(line) is not None:
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
            self._split_fused_date(cells, columns)
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
            titles.append(self._title_cell(lines[title]) if title is not None else None)
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
        prepared: bool = False,
    ) -> ParseOutcome:
        """One statement per section (module doc, 7)."""
        outcome = ParseOutcome()
        rows, preamble, pending, skipped, _ = self._collect(preamble_lines)
        outcome.pending_items += pending
        # A row above the first section belongs to no account: counted.
        outcome.skipped_lines += skipped + len(rows)
        outcome.skipped_regions.extend(preamble.skipped_boxes)
        outcome.skipped_regions.extend((row.page, row.bbox) for row in rows)
        warnings: list[str] = ["accounts_by_balance"]
        if rows:
            warnings.append("rows_outside_account")
        if preamble.headerless:
            warnings.append(f"rows_without_header:{preamble.headerless}")
        for section in sections:
            read = self.read_statement(
                view,
                section.lines,
                threshold=threshold,
                date_order=date_order,
                period_texts=preamble.period.texts,
                prepared=prepared,
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
        prepared: bool = False,
    ) -> StatementRead:
        """The statement ``lines`` hold (all of ``view``'s, or one account's).

        ``period_texts`` are period candidates from outside the lines (the
        document's overview) tried after the lines' own; ``account`` is the
        account the lines belong to (:mod:`app.ingestion.parsers.accounts`).
        ``statement`` is None when no transaction table header is found.
        ``prepared`` says the lines went through :meth:`prepare` already
        (:meth:`parse` does it once for the whole document).
        """
        if not prepared:
            lines = self.prepare(lines)
        # A summary box printed beside the transaction table (or two boxes
        # side by side) is read block by block, so the table's header and
        # rows are whole lines again (:mod:`app.ingestion.parsers.blocks`).
        lines = unfold_blocks(lines, block_title, lambda line: self.columns(line) is not None)
        rows, summary, pending_items, skipped, found_header = self._collect(
            self.without_stubs(lines)
        )
        outcome = StatementRead(
            pending_items=pending_items,
            skipped_lines=skipped,
            skipped_regions=list(summary.skipped_boxes),
        )
        if summary.headerless:
            outcome.warnings.append(f"rows_without_header:{summary.headerless}")
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
            if row.carried:
                # Printed on the row above, once for the day.
                date_conf = min(date_conf, conf.DERIVED)
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
        # A figure read beside its label, not after it, is believed a little less.
        opening_conf = min(self.base, conf.DERIVED) if summary.opening_adjacent else self.base
        closing_conf = min(self.base, conf.DERIVED) if summary.closing_adjacent else self.base
        statement_conf["opening_balance"] = opening_conf if opening is not None else conf.MISSING
        statement_conf["closing_balance"] = closing_conf if closing is not None else conf.MISSING
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
            # Only the rows the statement leaves in doubt (a printed running
            # balance vouches for its row); all of them when nothing says which.
            doubted = list(range(len(fields)))
            if result.status == "mismatch":
                doubted = doubtful_rows(opening, ledger_rows, result.difference)
                result = result.model_copy(update={"doubtful": doubted})
            for index in doubted:
                fields[index]["reconciliation"] = cap
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
        if any(row.carried for row in rows):
            outcome.warnings.append("dates_carried")
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
#: a checking account's ``CREDIT CARD PAYMENT`` row is no cue) and on short
#: lines only (:data:`CARD_CUE_WORDS`).
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
#: The most words (as printed, split at spaces) a line may hold for its card
#: or deposit-account words to count: a title or a label with its value says
#: what the statement is, a sentence (a fee note that mentions the bank's
#: credit card) only talks about one.
CARD_CUE_WORDS: Final = 8
#: A whole word of these on a short page-1 line outside the rows names a
#: deposit account (``Business Checking``, ``Total Deposits``): the statement
#: is not a card's, whatever card words are there. Only on a line that
#: **names** the account (:func:`names_deposit_account`): a card statement
#: mentions them too (``AutoPay from Checking ending 7731``, ``Rewards
#: savings this year $12.40``).
DEPOSIT_ACCOUNT_WORDS: Final = ("checking", "savings", "deposits", "withdrawals")
#: Where a deposit-account word stands on a line that names the account: it
#: is one of the first two words...
DEPOSIT_WORD_LEAD: Final = 2
#: ...of at most three (``Business Checking Account``, ``Deposits and
#: Additions``); figures, account numbers and dates are not counted.
DEPOSIT_TITLE_WORDS: Final = 3


def names_deposit_account(text: str) -> bool:
    """``text`` (a line as printed) names a deposit account or its totals:
    a title or a label (``Checking Summary``, ``Premier Savings 1,204.00``,
    ``Total Deposits 2,000.00``), not a line that mentions one (``AutoPay
    from Checking ending 7731``)."""
    words = [
        normalise(word)
        for word in text.split()
        if any(ch.isalpha() for ch in word) and not any(ch.isdigit() for ch in word)
    ]
    return len(words) <= DEPOSIT_TITLE_WORDS and any(
        word in DEPOSIT_ACCOUNT_WORDS for word in words[:DEPOSIT_WORD_LEAD]
    )


class CardStatementParser(TableStatementParser):
    """Generic credit card statement: charges positive, credits ``CR`` or negative.

    Detected (0.45, above ``statement_generic``) when page 1 has a table and,
    outside its rows on lines of at most :data:`CARD_CUE_WORDS` words, a card
    word (:data:`CARD_VOCABULARY`) or two of the weak ones
    (:data:`WEAK_CARD_VOCABULARY`), and no such line names a deposit account
    (:data:`DEPOSIT_ACCOUNT_WORDS`, :func:`names_deposit_account`: a line
    that only mentions one, as a card paid from a checking account prints,
    vetoes nothing).
    """

    name: ClassVar[str] = "card_statement_generic"
    sign: ClassVar[Literal["bank", "card"]] = "card"
    account_type: ClassVar[str | None] = "creditcard"

    def detect(self, view: DocumentView) -> float:
        if self.fingerprints:
            return super().detect(view)
        page_one = self.prepare([line for line in view.lines if line.page <= 1])
        cues = [
            line
            for line in self.summary_lines(page_one)
            if len(line.text.split()) <= CARD_CUE_WORDS
        ]
        if any(names_deposit_account(line.text) for line in cues):
            return 0.0
        short = [f" {line.norm} " for line in cues]
        strong = any(f" {word} " in text for text in short for word in CARD_VOCABULARY)
        # Each weak word once (``Card Member`` and ``Cardmember`` are one).
        weak = len(
            {
                word.replace(" ", "")
                for word in WEAK_CARD_VOCABULARY
                if any(f" {word} " in text for text in short)
            }
        )
        if not strong and weak < 2:
            return 0.0
        # Above the bank-statement generic parser for the same table.
        return 0.45 if self.has_table(view) else 0.0

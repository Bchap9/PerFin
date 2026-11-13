# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single lines of a statement, read by themselves; where a summary starts.

Pure functions on a :class:`~app.ingestion.parsers.base.ViewLine` for the
lines a table reader meets where no header row says what the columns are.
Nothing here knows a table, a section or a page: each function says what
one line is by its own shape, and the caller decides what that makes it (a
row of a section it can check against a printed total, a line it counts).
Testable on hand-built lines without a PDF.

* :func:`led_row`: a line that starts with its date and ends with its
  amount, with words between (``04/02 | Dn | 107.40``; reflowed into one
  cell, ``04/02 04/03 CARD PAYMENT 7711 3302 -45.10``). The amount keeps
  the sign and marker it was printed with: direction is the caller's.
* :func:`titled_total`: a title with its count and total printed beside it
  (``Withdrawals / Debits | 5 items totaling $912.40``). The function does
  not know titles: any label with one figure beside it has this shape
  (``Ending balance | 1,200.00``, ``Check | 1042 | 250.00``), and the
  caller asks only about a first cell it knows as a title. The title is the
  first cell as the cells break: a count that fell into it
  (``Withdrawals / Debits 5 items | totaling $912.40``) stays in the text
  returned, which a caller's titles will not know, and the line is then
  what it was without this function.
* :func:`items_total`: a line that is a count and a total and nothing else
  (``8 item(s) totaling $3,500.00``), the total of the rows above it.
* :func:`amount_only`: a line that is one amount.

One function reads a run of lines: :func:`summary_starts` says at which
line each account of a statement starts when the accounts are told apart
only by their summaries (two accounts printed under one masked number).
What an opening or a closing balance line is, and which lines belong to a
table, the caller says.

What :func:`led_row` refuses, so that the caller counts the line and no
row is made of it:

* two figures at the end of the line (an amount with a balance beside it:
  which of the two is the row's cannot be told without a header), also
  when a currency symbol, a sign or a currency code stands between them
  (``45.10 | $ | 1,307.40``, ``45.10 USD 1,307.40``);
* a dash or a plus standing alone before the amount, in a cell of its own
  or as a word of the text (``PAYMENT | - | 45.10``): a minus, a separator
  or an empty column, no telling. A sign in the amount's own cell
  (``- 45.10``) or on its symbol (``-$ | 45.10``) is the amount's;
* a range of dates at the start (``04/01 through 04/30 interest 1.20``: a
  line about a period, not a transaction of a day);
* nothing but digits between the date and the amount (a date and a
  balance, a line of a table of balances by day);
* more than :data:`MAX_LED_WORDS` words (a sentence that starts with a
  date and ends with a figure).

What :func:`led_row` does not refuse, because it judges a shape and knows
no label: a dated line of a summary (``04/30 | Ending Balance | 1,200.00``,
``04/30 Total 1,200.00``, ``12/31 Total fees charged 45.10``). The caller
tests the labels of balance and total lines before it asks.

Patterns are anchored and free of nested quantifiers, and every text is
cut to the limits of :mod:`app.ingestion.parsers.text` before one runs.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from app.ingestion.parsers.base import ViewLine
from app.ingestion.parsers.currency import known_currency
from app.ingestion.parsers.text import MAX_LINE, Amount, find_amounts, read_amount

#: A line of more words is a sentence, whatever it starts and ends with.
MAX_LED_WORDS: Final = 30
#: A date is printed in at most this many words (``Apr 02 2026``).
MAX_DATE_WORDS: Final = 3
#: The words a title's total may be printed with, beside a count and the figure.
TOTAL_WORDS: Final = frozenset(
    {
        "item",
        "items",
        "totaling",
        "total",
        "checks",
        "credits",
        "debits",
        "deposits",
        "withdrawals",
    }
)

#: A summary's title is looked for in this many lines above its opening line.
TITLE_LINES_ABOVE: Final = 3

#: Between two dates these make a range of them.
_RANGE_WORDS: Final = frozenset({"through", "thru", "to", "until", "-", "\u2013", "\u2014"})
#: Markers printed after a figure as a word of their own.
_MARKERS: Final = frozenset({"CR", "DR", "DB"})
#: A currency symbol printed as a word of its own, with or without its sign.
_LONE_SYMBOL: Final = re.compile(r"[-+\u2212]?[$\u00a3\u20ac]")
#: A sign or a dash printed as a word of its own.
_LONE_SIGNS: Final = frozenset({"-", "+", "\u2212", "\u2013", "\u2014"})
#: Punctuation around a word of a title's total (``(8``, ``checks)``, ``total:``).
_WRAPPING: Final = "()[]:,.;"
_ITEMS_TOTAL: Final = re.compile(
    r"\d{1,6} item(?:s|\(s\))? total{1,2}ing (?P<amount>\S.{0,40})", re.IGNORECASE
)


@dataclass(frozen=True)
class LedRow:
    """A line that starts with its date and ends with its amount."""

    #: The date as printed (its year, when it has none, is the caller's to find).
    date: str
    #: The text between the date and the amount, one string per cell.
    description: tuple[str, ...]
    #: The figure as printed: its sign and marker, no direction inferred.
    amount: Amount


def _date_words(words: Sequence[str], date_shaped: Callable[[str], bool]) -> int:
    """How many of the first ``words`` are one date (0: none)."""
    for size in range(min(MAX_DATE_WORDS, len(words)), 0, -1):
        if date_shaped(" ".join(words[:size])):
            return size
    return 0


def _trailing_amount(words: Sequence[str]) -> tuple[Amount, int] | None:
    """The amount ``words`` end with and the words it takes.

    One word, or one word with a marker after it (``45.10 CR``).
    """
    end = len(words)
    marked = end >= 2 and words[-1].upper() in _MARKERS
    start = end - 2 if marked else end - 1
    if start < 0:
        return None
    amount = read_amount(" ".join(words[start:end]))
    return None if amount is None else (amount, end - start)


def _between_figures(word: str) -> bool:
    """A word that may stand between an amount and the balance beside it:
    a currency symbol, a sign, a currency code printed in capitals."""
    if _LONE_SYMBOL.fullmatch(word) or word in _LONE_SIGNS:
        return True
    return word.isupper() and known_currency(word) is not None


def _ends_with_amount(words: Sequence[str]) -> bool:
    """The last word, or the last two (``$ 45.10``, ``45.10 CR``), read as an
    amount, whatever symbols, signs and currency codes are printed after it."""
    end = len(words)
    while end and _between_figures(words[end - 1]):
        end -= 1
    words = words[:end]
    if not words:
        return False
    if read_amount(words[-1]) is not None:
        return True
    return len(words) >= 2 and read_amount(" ".join(words[-2:])) is not None


def led_row(line: ViewLine, date_shaped: Callable[[str], bool]) -> LedRow | None:
    """The date-led row ``line`` is, or None (the module doc says what is refused).

    The first one to three words of the first cell are a date
    (``date_shaped`` says what a date looks like to the caller). A second
    date right after it, written the same way (digits, or a month's name),
    is the posting date and is dropped. The amount is the last cell when
    that cell is an amount, else the last word of the last cell; a currency
    symbol standing alone before it, with or without its sign, in the cell
    before or as a word (``-$ | 45.10``, ``$ 45.10``), is part of it. What
    stands between holds a letter.
    """
    cells = [cell.text.split() for cell in line.cells]
    cells = [words for words in cells if words]
    count = sum(len(words) for words in cells)
    if not cells or count > MAX_LED_WORDS:
        return None
    dated = _date_words(cells[0], date_shaped)
    if not dated:
        return None

    last = cells[-1]
    whole = read_amount(" ".join(last)) if len(cells) > 1 else None
    if whole is not None:
        amount, taken = whole, len(last)
    else:
        tail = _trailing_amount(last)
        if tail is None:
            return None
        amount, taken = tail
    if dated + taken > count:
        return None

    # Every word of the line with its cell; then those between the date and
    # the amount.
    between = [(word, index) for index, words in enumerate(cells) for word in words]
    before = count - taken - 1
    if before >= dated and _LONE_SYMBOL.fullmatch(between[before][0]):
        taken += 1
        joined = read_amount(" ".join(word for word, _ in between[before:]))
        if joined is None:
            return None  # two signs, two symbols: not one figure
        amount = joined
    elif before >= dated and between[before][0] in _LONE_SIGNS:
        return None
    between = between[dated : count - taken]
    words = [word for word, _ in between]
    if not words:
        return None
    if words[0].lower() in _RANGE_WORDS and _date_words(words[1:], date_shaped):
        return None
    date_text = " ".join(cells[0][:dated])
    second = _date_words(words, date_shaped)
    if second and _named(" ".join(words[:second])) == _named(date_text):
        between, words = between[second:], words[second:]
    if _ends_with_amount(words):
        return None
    if not any(ch.isalpha() for word in words for ch in word):
        return None

    description: list[list[str]] = []
    at = -1
    for word, index in between:
        if index != at:
            description.append([])
            at = index
        description[-1].append(word)
    return LedRow(date_text, tuple(" ".join(part) for part in description), amount)


def _named(text: str) -> bool:
    """The date names its month (``Apr 02``) rather than numbering it."""
    return any(ch.isalpha() for ch in text)


def titled_total(line: ViewLine) -> tuple[str, Amount] | None:
    """The first cell's text and the total printed beside it, or None.

    The first cell has letters and no amount. The other cells hold exactly
    one amount, at most one count, and otherwise only words of
    :data:`TOTAL_WORDS` (brackets and the ``(s)`` of ``item(s)`` ignored).
    """
    if len(line.cells) < 2:
        return None
    title = " ".join(line.cells[0].text.split())[:MAX_LINE]
    if not any(ch.isalpha() for ch in title) or find_amounts(title):
        return None
    rest = " ".join(cell.text for cell in line.cells[1:])[:MAX_LINE]
    amounts = find_amounts(rest)
    if len(amounts) != 1:
        return None
    counts = 0
    after_figure = False
    for token in rest.split():
        if read_amount(token.rstrip(".,;:")) is not None:
            after_figure = True
            continue
        marker = after_figure and token.upper() in _MARKERS
        after_figure = False
        if marker or _LONE_SYMBOL.fullmatch(token):
            continue
        word = token.lower().replace("(s)", "").strip(_WRAPPING)
        if not word:
            continue
        digits = word.replace(",", "")
        if digits.isascii() and digits.isdigit():
            counts += 1
        elif word not in TOTAL_WORDS:
            return None
    return (title, amounts[0]) if counts <= 1 else None


def items_total(line: ViewLine) -> Amount | None:
    """The total of ``<count> item(s) totaling <amount>``, when the line is nothing else."""
    match = _ITEMS_TOTAL.fullmatch(" ".join(line.text[:MAX_LINE].split()))
    return read_amount(match["amount"]) if match else None


def amount_only(line: ViewLine) -> Amount | None:
    """The amount the whole line is, or None."""
    return read_amount(" ".join(line.text[:MAX_LINE].split()))


def _title_shaped(line: ViewLine) -> bool:
    """Letters and no digit: the title of a block (``SAVINGS SUMMARY``)."""
    text = line.text[:MAX_LINE]
    return any(ch.isalpha() for ch in text) and not any(ch.isdigit() for ch in text)


def summary_starts(
    lines: Sequence[ViewLine],
    opening: Callable[[ViewLine], Amount | None],
    closing: Callable[[ViewLine], Amount | None],
    in_table: Callable[[int], bool],
) -> list[int]:
    """The index in ``lines`` at which each account starts; ``[]`` for fewer than two.

    Only lines outside a table count (``in_table(index)`` false). An account
    starts at a line for which ``opening`` gives a figure when it is the
    first such line, or when a ``closing`` figure was met since the last
    start and the figure differs from that start's opening figure: the same
    summary printed again (the head of the next page) is the same account,
    and a second opening figure before any closing one is no new account.

    The start moves up to the nearest of the :data:`TITLE_LINES_ABOVE` lines
    above the opening line that has letters and no digit (the block's
    title), on the opening line's page. The search stops at a line of a
    table and at the closing line met before it: what stands there belongs
    to the account above.
    """
    starts: list[int] = []
    opened: Amount | None = None
    #: The last closing line met since the last start (None: none yet).
    closed_at: int | None = None
    for index, line in enumerate(lines):
        if in_table(index):
            continue
        figure = opening(line)
        if figure is None:
            if closing(line) is not None:
                closed_at = index
            continue
        if opened is not None and (closed_at is None or figure.value == opened.value):
            continue
        start = index
        for above in range(index - 1, max(index - TITLE_LINES_ABOVE, 0) - 1, -1):
            if above == closed_at or in_table(above) or lines[above].page != line.page:
                break
            if _title_shaped(lines[above]):
                start = above
                break
        starts.append(start)
        opened, closed_at = figure, None
    return starts if len(starts) >= 2 else []

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single lines of a statement, read by themselves.

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
  (``Ending balance | 1,200.00``), and the caller asks only about a first
  cell it knows as a title.
* :func:`items_total`: a line that is a count and a total and nothing else
  (``8 item(s) totaling $3,500.00``), the total of the rows above it.
* :func:`amount_only`: a line that is one amount.

What :func:`led_row` refuses, so that the caller counts the line and no
row is made of it:

* two figures at the end of the line (an amount with a balance beside it:
  which of the two is the row's cannot be told without a header);
* a range of dates at the start (``04/01 through 04/30 interest 1.20``: a
  line about a period, not a transaction of a day);
* nothing but digits between the date and the amount (a date and a
  balance, a line of a table of balances by day);
* more than :data:`MAX_LED_WORDS` words (a sentence that starts with a
  date and ends with a figure).

Patterns are anchored and free of nested quantifiers, and every text is
cut to the limits of :mod:`app.ingestion.parsers.text` before one runs.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from app.ingestion.parsers.base import ViewLine
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

#: Between two dates these make a range of them.
_RANGE_WORDS: Final = frozenset({"through", "thru", "to", "until", "-", "\u2013", "\u2014"})
#: Markers printed after a figure as a word of their own.
_MARKERS: Final = frozenset({"CR", "DR", "DB"})
#: A currency symbol printed as a word of its own, with or without its sign.
_LONE_SYMBOL: Final = re.compile(r"[-+\u2212]?[$\u00a3\u20ac]")
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

    One word, with a marker after it (``45.10 CR``) and a currency symbol
    standing alone before it (``$ 45.10``). A lone dash before the figure is
    not taken: after words it may be a separator.
    """
    end = len(words)
    marked = end >= 2 and words[-1].upper() in _MARKERS
    start = end - 2 if marked else end - 1
    if start < 0:
        return None
    if start >= 1 and _LONE_SYMBOL.fullmatch(words[start - 1]):
        start -= 1
    amount = read_amount(" ".join(words[start:end]))
    return None if amount is None else (amount, end - start)


def _ends_with_amount(words: Sequence[str]) -> bool:
    """The last word, or the last two (``$ 45.10``, ``45.10 CR``), read as an amount."""
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
    that cell is an amount, else the last word of the last cell. What stands
    between holds a letter.
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

    # The words between the date and the amount, each with its cell.
    between = [(word, index) for index, words in enumerate(cells) for word in words]
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

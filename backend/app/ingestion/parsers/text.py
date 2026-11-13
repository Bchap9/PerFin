# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading amounts and dates out of statement text (deterministic, no model).

Everything here works on short strings already validated by the sandbox
contract (a page's words). Inputs are cut to :data:`MAX_TOKEN` characters
before any pattern runs, and every pattern is anchored and free of nested
quantifiers, so a crafted page cannot make matching expensive.

* :func:`read_amount`: ``1,234.56``, ``-1,234.56``, ``$1,234.56``,
  ``-$1,234.56``, ``+$25.00``, ``(12.00)``, ``12.00-``, ``500.00 CR`` /
  ``DR`` / ``DB``. A sign or a currency symbol may stand one space from
  what follows it (``- $310.00``, ``$ 1,480.25``), a gap may precede a
  separator (``7,310 .00``) and one footnote mark may follow the figure
  (``642.18*``). Two decimals are required (statements print cents); a bare
  integer is not an amount, so dates and reference numbers are never read
  as money. :func:`find_amounts` joins a lone symbol to the figure after it
  and a lone sign to a symbol, never to bare digits (``900.00 - 150.00`` is
  two positive figures). :attr:`Amount.sign_detached` says the sign stood a
  space away: on a summary line it may be the separator after a label
  (``Ending Balance - $992.50``), which only the reader of that line can
  tell (:func:`separator_sign`; :func:`after_label` for a reader that
  cannot check the sign against anything).
* :func:`read_date` for one :data:`DateStyle`; :func:`guess_numeric_style`
  tells ``DD/MM`` from ``MM/DD`` from the values themselves (a day above 12)
  and says when it cannot. Yearless styles (``MM/DD``, ``DD/MM``,
  ``December 16``, ``16 Dec``) take their year from a window
  (:func:`infer_year`): the statement period with a little slack around it,
  or, ``strict``, only inside it (a statement date's year before it, so a
  December row on a January statement is last year's and no row lands
  after the statement date).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

MAX_TOKEN: Final = 200
MAX_LINE: Final = 2000

DateStyle = Literal["mdy", "dmy", "ymd", "d_mon_y", "mon_d_y", "md", "dm", "mon_d", "d_mon"]
#: Styles with a year printed.
FULL_STYLES: Final[tuple[DateStyle, ...]] = ("mdy", "dmy", "ymd", "d_mon_y", "mon_d_y")
#: Styles without a year (read through a window, :func:`infer_year`).
YEARLESS_STYLES: Final[tuple[DateStyle, ...]] = ("md", "dm", "mon_d", "d_mon")
#: Numeric styles whose day/month order is not visible in the text.
NUMERIC_DM: Final[tuple[DateStyle, ...]] = ("mdy", "dmy")

_MONTHS: Final = {
    m: i
    for i, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for m in names
}

_AMOUNT: Final = re.compile(
    r"(?P<open>\()?(?:(?P<sign>[-+\u2212])(?P<gap>\s)?)?"
    r"(?:(?P<cur>[$\u00a3\u20ac])(?P<gap2>\s)?)?(?P<sign2>[-+\u2212])?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)\.(?P<dec>\d{2})"
    r"(?P<close>\))?(?P<trail>-)?(?:\s?(?P<mark>CR|DR|DB|Cr|Dr|Db|cr|dr|db))?"
)
#: Whitespace between a digit and a separator that a digit follows (``7,310 .00``).
_SEPARATOR_GAP: Final = re.compile(r"(?<=\d)\s+(?=[.,]\d)")
#: One of these may follow a figure (a pointer to a note under the table).
_FOOTNOTE_MARKS: Final = frozenset("*\u2020\u2021\u00a7\u00b9\u00b2\u00b3")
_SIGNS: Final = frozenset("-+\u2212")
#: Markers printed after a figure: credit, debit, debit.
_MARKERS: Final = ("CR", "DR", "DB")
_NUMERIC_DATE: Final = re.compile(r"(\d{1,4})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?")
_D_MON_Y: Final = re.compile(r"(\d{1,2})[\s-]([A-Za-z]{3,9})\.?[\s-](\d{4})")
#: ``03-Nov-25`` / ``03/Nov/2025``: joined parts, so a two-digit year is a year
#: (``16 Dec 20`` may be a day, a month and a count).
#: The separator is printed twice (``03-Nov/25`` is not a date).
_D_MON_Y_JOINED: Final = re.compile(
    r"(?P<day>\d{1,2})(?P<sep>[-/])(?P<month>[A-Za-z]{3,9})(?P=sep)(?P<year>\d{4}|\d{2})"
)
_MON_D_Y: Final = re.compile(r"([A-Za-z]{3,9})\.?\s(\d{1,2}),?\s(\d{4})")
_MON_D: Final = re.compile(r"([A-Za-z]{3,9})\.?\s(\d{1,2})")
_D_MON: Final = re.compile(r"(\d{1,2})[\s-]([A-Za-z]{3,9})\.?")
_CURRENCY_SYMBOLS: Final = {"$": "USD", "£": "GBP", "€": "EUR"}


@dataclass(frozen=True)
class Amount:
    value: Decimal
    #: ``CR`` (True) or ``DR``/``DB`` (False) printed after the figure (None: no marker).
    credit: bool | None = None
    #: A minus sign, a trailing minus or parentheses were printed.
    negative_marked: bool = False
    #: A plus sign was printed.
    positive_marked: bool = False
    symbol: str | None = None
    #: The sign stood one space from the symbol or figure after it (``- $12.00``,
    #: ``- 12.00``). Printed as a sign it is one; after a label on a line of
    #: text it may be a separator (``Ending Balance - $992.50``).
    sign_detached: bool = False

    @property
    def currency(self) -> str | None:
        return _CURRENCY_SYMBOLS.get(self.symbol) if self.symbol else None


def read_amount(text: str) -> Amount | None:
    """The amount ``text`` is (the whole cell), or None."""
    text = text.strip()[:MAX_TOKEN]
    if text and text[-1] in _FOOTNOTE_MARKS:
        text = text[:-1]
        if text.endswith(" "):
            text = text[:-1]
    text = _SEPARATOR_GAP.sub("", text)
    match = _AMOUNT.fullmatch(text)
    if match is None:
        return None
    if match["sign"] and match["sign2"] and (match["gap"] or match["gap2"]):
        return None  # ``- -5.00``: two signs are one figure only when printed as one
    if bool(match["open"]) != bool(match["close"]):
        return None
    sign = match["sign"] or match["sign2"]
    negative = bool(match["open"]) or bool(match["trail"]) or sign in ("-", "\u2212")
    if negative and (match["open"] and (sign or match["trail"])):
        return None
    try:
        value = Decimal(f"{match['int'].replace(',', '')}.{match['dec']}")
    except InvalidOperation:  # pragma: no cover - the pattern guarantees digits
        return None
    mark = (match["mark"] or "").upper()
    return Amount(
        value=-value if negative else value,
        credit=True if mark == "CR" else (False if mark in ("DR", "DB") else None),
        negative_marked=negative,
        positive_marked=sign == "+",
        symbol=match["cur"],
        sign_detached=bool(match["sign"] and match["gap"]),
    )


#: Symbols that name one currency. ``$`` is shared by too many to say which.
_SURE_SYMBOLS: Final = {"\u00a3": "GBP", "\u20ac": "EUR"}
#: Amounts that must carry the symbol before it is believed.
MIN_SYMBOL_AMOUNTS: Final = 2


def symbol_currency(texts: Iterable[str]) -> str | None:
    """The currency the amounts of ``texts`` are printed in, when their symbol says so.

    Every amount printed with a symbol must carry the same one, at least
    :data:`MIN_SYMBOL_AMOUNTS` do, and it names a single currency (``\u00a3``,
    ``\u20ac``). A dollar sign is no evidence (US, Canadian, Australian...),
    nor is a document mixing symbols (a card statement listing a purchase
    abroad).
    """
    seen: dict[str, int] = {}
    for text in texts:
        if not any(symbol in text for symbol in _CURRENCY_SYMBOLS):
            continue
        for amount in find_amounts(text):
            if amount.symbol is not None:
                seen[amount.symbol] = seen.get(amount.symbol, 0) + 1
    if len(seen) != 1:
        return None
    symbol, count = next(iter(seen.items()))
    return _SURE_SYMBOLS.get(symbol) if count >= MIN_SYMBOL_AMOUNTS else None


def _figure(tokens: list[str], index: int) -> tuple[str, int]:
    """The text of the figure starting at ``tokens[index]`` and the tokens it takes.

    A lone currency symbol, with or without its sign (``$``, ``-$``), takes
    the token after it; a lone sign takes the
    token after it when that starts with a currency symbol (and the one
    after that when the symbol stands alone too). A sign before bare digits
    is an operator of the line (``900.00 - 150.00``) and takes nothing.
    """
    token = tokens[index]
    following = tokens[index + 1] if index + 1 < len(tokens) else ""
    if token in _CURRENCY_SYMBOLS and following:
        return f"{token} {following}", 2
    if len(token) == 2 and token[0] in _SIGNS and token[1] in _CURRENCY_SYMBOLS and following:
        return f"{token} {following}", 2
    if token in _SIGNS and following[:1] in _CURRENCY_SYMBOLS:
        if following not in _CURRENCY_SYMBOLS:
            return f"{token} {following}", 2
        if index + 2 < len(tokens):
            return f"{token} {following} {tokens[index + 2]}", 3
    return token, 1


def find_amounts(text: str) -> list[Amount]:
    """Every amount in a line, left to right (whitespace-separated tokens)."""
    out: list[Amount] = []
    tokens = text[:MAX_LINE].split()
    index = 0
    while index < len(tokens):
        figure, used = _figure(tokens, index)
        following = tokens[index + used] if index + used < len(tokens) else ""
        amount = read_amount(f"{figure} {following}") if following.upper() in _MARKERS else None
        if amount is not None:
            used += 1
        else:
            amount = read_amount(figure.rstrip(".,;:"))
        if amount is None:
            # A symbol or sign that led nowhere: the next token is read on its own.
            index += 1
            continue
        out.append(amount)
        index += used
    return out


#: A minus printed as a token of its own.
_LONE_MINUS: Final = frozenset({"-", "\u2212"})


def separator_sign(amount: Amount, cells: Iterable[str]) -> bool:
    """The minus of ``amount``, read after a label on a line of ``cells``
    (their texts), may be a separator and no sign: it stood apart from the
    figure (``Ending Balance - $992.50``) and not in the figure's own cell
    (a cell reading ``- $20.00`` prints a sign). ``CR``, ``DR`` or ``DB``
    after the figure says the direction whatever the dash is."""
    if not (amount.sign_detached and amount.negative_marked) or amount.credit is not None:
        return False
    for text in cells:
        alone = read_amount(text)
        if alone is not None and alone.sign_detached and alone.value == amount.value:
            return False
    return True


def after_label(text: str, cells: Iterable[str]) -> list[Amount]:
    """:func:`find_amounts` of ``text``, what a line of ``cells`` prints after
    a label, for a reader with nothing to check a sign against: a dash that
    starts ``text`` and stands apart from the first figure
    (:func:`separator_sign`) is the label's separator, and the figure is
    read without it (``Net Pay - $1,840.25`` is pay, not a debt). A reader
    that can check the sign asks :func:`separator_sign` and decides."""
    amounts = find_amounts(text)
    tokens = text[:MAX_LINE].split()
    if amounts and tokens and tokens[0] in _LONE_MINUS and separator_sign(amounts[0], cells):
        first = amounts[0]
        amounts[0] = replace(
            first, value=abs(first.value), negative_marked=False, sign_detached=False
        )
    return amounts


def _year(text: str) -> int | None:
    value = int(text)
    if len(text) == 2:
        value += 2000
    return value if 1900 <= value <= 2200 else None


def _make(year: int | None, month: int, day: int) -> date | None:
    if year is None:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def numeric_parts(text: str) -> tuple[int, int, int | None] | None:
    """``(first, second, year)`` of ``NN/NN[/YYYY]`` (None: not that shape)."""
    match = _NUMERIC_DATE.fullmatch(text.strip()[:MAX_TOKEN])
    if match is None or len(match[1]) > 2:
        return None
    year = _year(match[3]) if match[3] else None
    if match[3] and year is None:
        return None
    return int(match[1]), int(match[2]), year


def infer_year(
    month: int, day: int, window: tuple[date, date] | None, *, strict: bool = False
) -> date | None:
    """A yearless date placed in (or next to) the statement period, the latest that fits.

    ``strict``: only inside ``window`` (no slack of 45 days before it and 7
    after it), e.g. the year before a statement date ending the window.
    """
    if window is None:
        return None
    start, end = window
    low, high = (start, end) if strict else (start - timedelta(days=45), end + timedelta(days=7))
    for year in sorted({start.year, end.year, start.year - 1}, reverse=True):
        candidate = _make(year, month, day)
        if candidate is not None and low <= candidate <= high:
            return candidate
    return None


def month_day(text: str, style: DateStyle) -> tuple[int, int] | None:
    """``(month, day)`` of a yearless month-named date: ``mon_d`` (``December 16``)
    or ``d_mon`` (``16 Dec``); None for any other text or style."""
    text = text.strip()[:MAX_TOKEN]
    if style == "mon_d":
        found = _MON_D.fullmatch(text)
        name, number = (found[1], found[2]) if found else ("", "0")
    elif style == "d_mon":
        found = _D_MON.fullmatch(text)
        name, number = (found[2], found[1]) if found else ("", "0")
    else:
        return None
    month = _MONTHS.get(name.lower())
    day = int(number)
    if month is None or not 1 <= day <= 31:
        return None
    return month, day


def read_date(
    text: str,
    style: DateStyle,
    *,
    window: tuple[date, date] | None = None,
    strict: bool = False,
) -> date | None:
    """``text`` (a whole cell) as a date in ``style``; yearless styles need ``window``.

    ``strict`` is :func:`infer_year`'s (a yearless date only inside ``window``).
    """
    text = text.strip()[:MAX_TOKEN]
    if style in ("mdy", "dmy", "md", "dm"):
        parts = numeric_parts(text)
        if parts is None:
            return None
        first, second, year = parts
        month, day = (first, second) if style in ("mdy", "md") else (second, first)
        if style in ("md", "dm"):
            if year is not None:
                return _make(year, month, day)
            if not (1 <= month <= 12 and 1 <= day <= 31):
                return None
            return infer_year(month, day, window, strict=strict)
        return _make(year, month, day)
    if style in ("mon_d", "d_mon"):
        named = month_day(text, style)
        if named is None:
            return None
        return infer_year(named[0], named[1], window, strict=strict)
    if style == "ymd":
        match = re.fullmatch(r"(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})", text)
        return _make(int(match[1]), int(match[2]), int(match[3])) if match else None
    if style == "d_mon_y":
        match = _D_MON_Y.fullmatch(text)
        if match is not None:
            day_text, name, year_text = match[1], match[2], match[3]
        elif (match := _D_MON_Y_JOINED.fullmatch(text)) is not None:
            day_text, name, year_text = match["day"], match["month"], match["year"]
        else:
            return None
        if name.lower() not in _MONTHS:
            return None
        year = int(year_text) if len(year_text) == 4 else _year(year_text)
        return _make(year, _MONTHS[name.lower()], int(day_text))
    match = _MON_D_Y.fullmatch(text)
    if match is None or match[1].lower() not in _MONTHS:
        return None
    return _make(int(match[3]), _MONTHS[match[1].lower()], int(match[2]))


def guess_numeric_style(texts: Iterable[str]) -> tuple[DateStyle | None, bool]:
    """``(style, certain)`` for numeric dates: ``dmy``/``mdy`` from a part above 12.

    ``certain`` is False when every value fits both orders (the style returned
    is then ``mdy``, the guess) or the values contradict each other.
    """
    first_big = second_big = False
    seen = False
    for text in texts:
        parts = numeric_parts(text)
        if parts is None:
            continue
        seen = True
        first, second, _ = parts
        first_big |= first > 12
        second_big |= second > 12
    if not seen:
        return None, False
    if first_big and not second_big:
        return "dmy", True
    if second_big and not first_big:
        return "mdy", True
    return "mdy", False


def find_dates(
    text: str, styles: Iterable[DateStyle], *, window: tuple[date, date] | None = None
) -> list[date]:
    """Dates in a line: word windows of one to three tokens read in ``styles``."""
    tokens = text[:MAX_LINE].replace("\u2013", " - ").split()
    out: list[date] = []
    index = 0
    styles = tuple(styles)
    while index < len(tokens):
        found: date | None = None
        width = 0
        for size in (3, 2, 1):
            chunk = " ".join(tokens[index : index + size]).rstrip(".,;:")
            for style in styles:
                found = read_date(chunk, style, window=window)
                if found is not None:
                    width = size
                    break
            if found is not None:
                break
        if found is not None:
            out.append(found)
            index += width
        else:
            index += 1
    return out


def normalise(text: str) -> str:
    """Lower case, punctuation-trimmed, single-spaced (for label comparisons)."""
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text[:MAX_LINE])
    return " ".join(cleaned.lower().split())

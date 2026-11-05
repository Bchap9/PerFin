# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading amounts and dates out of statement text (deterministic, no model).

Everything here works on short strings already validated by the sandbox
contract (a page's words). Inputs are cut to :data:`MAX_TOKEN` characters
before any pattern runs, and every pattern is anchored and free of nested
quantifiers, so a crafted page cannot make matching expensive.

* :func:`read_amount`: ``1,234.56``, ``-1,234.56``, ``$1,234.56``,
  ``-$1,234.56``, ``+$25.00``, ``(12.00)``, ``12.00-``, ``500.00 CR`` /
  ``DR``. Two decimals are required (statements print cents); a bare
  integer is not an amount, so dates and reference numbers are never read
  as money.
* :func:`read_date` for one :data:`DateStyle`; :func:`guess_numeric_style`
  tells ``DD/MM`` from ``MM/DD`` from the values themselves (a day above 12)
  and says when it cannot.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

MAX_TOKEN: Final = 200
MAX_LINE: Final = 2000

DateStyle = Literal["mdy", "dmy", "ymd", "d_mon_y", "mon_d_y", "md", "dm"]
#: Styles with a year printed.
FULL_STYLES: Final[tuple[DateStyle, ...]] = ("mdy", "dmy", "ymd", "d_mon_y", "mon_d_y")
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
    r"(?P<open>\()?(?P<sign>[-+\u2212])?(?P<cur>[$\u00a3\u20ac])?(?P<sign2>[-+\u2212])?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)\.(?P<dec>\d{2})"
    r"(?P<close>\))?(?P<trail>-)?(?:\s?(?P<mark>CR|DR|Cr|Dr|cr|dr))?"
)
_NUMERIC_DATE: Final = re.compile(r"(\d{1,4})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?")
_D_MON_Y: Final = re.compile(r"(\d{1,2})[\s-]([A-Za-z]{3,9})\.?[\s-](\d{4})")
_MON_D_Y: Final = re.compile(r"([A-Za-z]{3,9})\.?\s(\d{1,2}),?\s(\d{4})")
_CURRENCY_SYMBOLS: Final = {"$": "USD", "£": "GBP", "€": "EUR"}


@dataclass(frozen=True)
class Amount:
    value: Decimal
    #: ``CR``/``DR`` printed after the figure (None: no marker).
    credit: bool | None = None
    #: A minus sign, a trailing minus or parentheses were printed.
    negative_marked: bool = False
    #: A plus sign was printed.
    positive_marked: bool = False
    symbol: str | None = None

    @property
    def currency(self) -> str | None:
        return _CURRENCY_SYMBOLS.get(self.symbol) if self.symbol else None


def read_amount(text: str) -> Amount | None:
    """The amount ``text`` is (the whole cell), or None."""
    text = text.strip()[:MAX_TOKEN]
    match = _AMOUNT.fullmatch(text)
    if match is None:
        return None
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
        credit=True if mark == "CR" else (False if mark == "DR" else None),
        negative_marked=negative,
        positive_marked=sign == "+",
        symbol=match["cur"],
    )


def find_amounts(text: str) -> list[Amount]:
    """Every amount in a line, left to right (whitespace-separated tokens)."""
    out: list[Amount] = []
    tokens = text[:MAX_LINE].split()
    skip = False
    for index, token in enumerate(tokens):
        if skip:
            skip = False
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if following.upper() in ("CR", "DR"):
            amount = read_amount(f"{token} {following}")
            if amount is not None:
                out.append(amount)
                skip = True
                continue
        amount = read_amount(token.rstrip(".,;:"))
        if amount is not None:
            out.append(amount)
    return out


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


def infer_year(month: int, day: int, window: tuple[date, date] | None) -> date | None:
    """A yearless date placed in (or next to) the statement period."""
    if window is None:
        return None
    start, end = window
    for year in sorted({start.year, end.year, start.year - 1}, reverse=True):
        candidate = _make(year, month, day)
        if candidate is not None and start - timedelta(days=45) <= candidate <= end + timedelta(
            days=7
        ):
            return candidate
    return None


def read_date(
    text: str, style: DateStyle, *, window: tuple[date, date] | None = None
) -> date | None:
    """``text`` (a whole cell) as a date in ``style``; yearless styles need ``window``."""
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
            return infer_year(month, day, window)
        return _make(year, month, day)
    if style == "ymd":
        match = re.fullmatch(r"(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})", text)
        return _make(int(match[1]), int(match[2]), int(match[3])) if match else None
    if style == "d_mon_y":
        match = _D_MON_Y.fullmatch(text)
        if match is None or match[2].lower() not in _MONTHS:
            return None
        return _make(int(match[3]), _MONTHS[match[2].lower()], int(match[1]))
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

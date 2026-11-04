# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dates as statements, receipts and forms print them.

:func:`find_dates` returns every date in one line, in order: ISO
(``2026-01-31``, ``2026/01/31``, ``2026.01.31``), numeric with a day/month
order (``01/31/2026``, ``31.01.2026``, ``1-31-26``), and month names in both
orders (``Jan 31, 2026``, ``January 31 2026``, ``31 Jan 2026``,
``31-Jan-2026``, ``Sept. 3rd, 2026``). A named date without a year
(``Jan 1 - Jan 31, 2026``) is kept as partial so a range can borrow the year
of its other end (:func:`complete_range`).

Numeric dates are ambiguous between month-first and day-first. A component
above 12 settles it; otherwise the caller's ``order`` hint (from a
fingerprint) decides, then the separator (``.`` is day-first, ``/`` and
``-`` are month-first) and the hit is marked ``ambiguous`` so its
confidence can be lowered. The patterns are fixed code, bounded and without
nested quantifiers, and only ever run over single capped lines.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

DateOrder = Literal["mdy", "dmy", "ymd"]

MIN_YEAR: Final = 1900
MAX_YEAR: Final = 2200

_MONTHS: Final = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MONTH: Final = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_ORD: Final = r"(?:st|nd|rd|th)?"

_ISO: Final = re.compile(r"(?<!\d)(\d{4})([-/.])(\d{1,2})\2(\d{1,2})(?!\d)")
_NUMERIC: Final = re.compile(r"(?<!\d)(\d{1,2})([-/.])(\d{1,2})\2(\d{4}|\d{2})(?!\d)")
_NAMED_MDY: Final = re.compile(
    rf"(?<![a-z]){_MONTH}\s?(\d{{1,2}}){_ORD}(?!\d)(?:,?\s?(\d{{4}}))?(?!\d)", re.IGNORECASE
)
_NAMED_DMY: Final = re.compile(
    rf"(?<!\d)(\d{{1,2}}){_ORD}[\s-]?{_MONTH}(?![a-z])(?:[,\s-]{{1,2}}(\d{{4}}))?(?!\d)",
    re.IGNORECASE,
)
_MAX_LINE: Final = 300


@dataclass(frozen=True)
class DateHit:
    """One date in a line. ``value`` is ``None`` for a named date without a year."""

    start: int
    end: int
    month: int
    day: int
    year: int | None
    ambiguous: bool = False

    @property
    def value(self) -> date | None:
        if self.year is None:
            return None
        return _make(self.year, self.month, self.day)


def _make(year: int, month: int, day: int) -> date | None:
    if not MIN_YEAR <= year <= MAX_YEAR:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _year(text: str) -> int:
    value = int(text)
    if len(text) == 2:
        return 2000 + value if value < 70 else 1900 + value
    return value


def _numeric(a: int, b: int, sep: str, order: DateOrder | None) -> tuple[int, int, bool] | None:
    """(month, day, ambiguous) for the numeric date ``a sep b sep year``."""
    if a > 12 and b > 12:
        return None
    if a > 12:
        return b, a, False
    if b > 12:
        return a, b, False
    if a == b:
        return a, b, False
    if order == "dmy":
        return b, a, False
    if order == "mdy":
        return a, b, False
    if sep == ".":
        return b, a, True
    return a, b, True


def find_dates(line: str, *, order: DateOrder | None = None) -> list[DateHit]:
    """Every date in ``line`` (cut to 300 characters), left to right, without overlaps."""
    text = line[:_MAX_LINE]
    hits: list[DateHit] = []
    for m in _ISO.finditer(text):
        value = _make(int(m.group(1)), int(m.group(3)), int(m.group(4)))
        if value is not None:
            hits.append(DateHit(m.start(), m.end(), value.month, value.day, value.year))
    for m in _NUMERIC.finditer(text):
        resolved = _numeric(int(m.group(1)), int(m.group(3)), m.group(2), order)
        if resolved is None:
            continue
        month, day, ambiguous = resolved
        year = _year(m.group(4))
        if _make(year, month, day) is not None:
            hits.append(DateHit(m.start(), m.end(), month, day, year, ambiguous))
    for m in _NAMED_MDY.finditer(text):
        month = _MONTHS[m.group(1)[:3].lower()]
        day = int(m.group(2))
        named_year = int(m.group(3)) if m.group(3) else None
        if _make(named_year or 2000, month, day) is not None:
            hits.append(DateHit(m.start(), m.end(), month, day, named_year))
    for m in _NAMED_DMY.finditer(text):
        month = _MONTHS[m.group(2)[:3].lower()]
        day = int(m.group(1))
        named_year = int(m.group(3)) if m.group(3) else None
        if _make(named_year or 2000, month, day) is not None:
            hits.append(DateHit(m.start(), m.end(), month, day, named_year))
    # Longest first at each position, then drop overlaps.
    hits.sort(key=lambda h: (h.start, -(h.end - h.start)))
    kept: list[DateHit] = []
    for hit in hits:
        if kept and hit.start < kept[-1].end:
            if (hit.end - hit.start) > (kept[-1].end - kept[-1].start) and hit.year is not None:
                kept[-1] = hit
            continue
        kept.append(hit)
    return kept


def complete_range(first: DateHit, second: DateHit) -> tuple[date, date] | None:
    """Both ends of ``first .. second``, the first borrowing the second's year if it has none."""
    end = second.value
    if end is None:
        return None
    start = first.value
    if start is None:
        start = _make(end.year, first.month, first.day)
        if start is not None and start > end:
            start = _make(end.year - 1, first.month, first.day)
    if start is None or start > end:
        return None
    return start, end

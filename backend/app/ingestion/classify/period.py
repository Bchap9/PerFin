# SPDX-License-Identifier: AGPL-3.0-or-later
"""Time window of a document (INGESTION.md "Time window detection").

* Statements, payslips and insurance documents: an explicit period printed
  next to a label ("Statement period", "Billing period", "For the period",
  "Pay period", "Policy period", ...) or an "Opening date"/"Closing date"
  pair; otherwise ``[min(transaction date), max(transaction date)]`` with
  ``inferred=True`` (dates the extractor parsed, else dates that start a
  line, which is how statement tables print them).
* Receipts and invoices: the printed date (a labelled one first: "Date",
  "Invoice date", "Transaction date", ...; else the first date).
* Tax forms: the tax year printed on the form ("Tax year 2025", "2025 Form
  W-2", "Form 1099-INT ... 2025", "For calendar year 2025").

Every answer carries a confidence; ambiguous day/month order lowers it.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from app.ingestion.classify.dates import DateHit, DateOrder, complete_range, find_dates
from app.ingestion.classify.text import DocumentView

#: A statement period longer than this is not believed.
MAX_PERIOD_DAYS: Final = 400

_PERIOD_LABELS: Final = (
    "statement period",
    "billing period",
    "billing cycle",
    "period covered",
    "for the period",
    "statement covers",
    "activity from",
    "activity for",
    "statement from",
    "statement dates",
    "pay period",
    "policy period",
    "coverage period",
    "period of coverage",
    "period:",
    "period from",
)
_OPENING_LABELS: Final = ("opening date", "beginning date", "period start", "from date")
_CLOSING_LABELS: Final = ("closing date", "ending date", "period end", "statement date", "to date")
_PRINTED_LABELS: Final = (
    "invoice date",
    "transaction date",
    "purchase date",
    "receipt date",
    "date of issue",
    "issued",
    "pay date",
    "date:",
    "date ",
)
_TAX_YEAR: Final = (
    re.compile(r"\btax\s?year:?\s?(\d{4})\b"),
    re.compile(r"\b(\d{4})\s?tax\s?year\b"),
    re.compile(r"\bfor\s?(?:the\s)?calendar\s?year\s?(\d{4})\b"),
    re.compile(r"\b(\d{4})\s?form\s?(?:w-?2|1099|1098|1095|5498|1040)"),
    re.compile(r"\bform\s?(?:w-?2|1099|1098|1095|5498|1040)[a-z0-9 -]{0,40}?\b((?:19|20)\d{2})\b"),
    re.compile(r"\bwage and tax statement\s?((?:19|20)\d{2})\b"),
)


@dataclass(frozen=True)
class Period:
    start: date | None = None
    end: date | None = None
    inferred: bool = False
    source: str = "none"
    confidence: float = 0.0


@dataclass(frozen=True)
class PrintedDate:
    value: date | None = None
    source: str = "none"
    confidence: float = 0.0


@dataclass(frozen=True)
class TaxYear:
    value: int | None = None
    source: str = "none"
    confidence: float = 0.0


def _plausible(start: date, end: date) -> bool:
    return start <= end and (end - start).days <= MAX_PERIOD_DAYS


def _range_after(line: str, at: int, order: DateOrder | None) -> tuple[date, date, bool] | None:
    hits = [h for h in find_dates(line, order=order) if h.end > at]
    for first, second in itertools.pairwise(hits):
        both = complete_range(first, second)
        if both is not None and _plausible(*both):
            return both[0], both[1], first.ambiguous or second.ambiguous
    return None


def _labelled_date(
    lines: Sequence[str], normalised: Sequence[str], labels: Sequence[str], order: DateOrder | None
) -> DateHit | None:
    for index, low in enumerate(normalised):
        for label in labels:
            at = low.find(label)
            if at < 0:
                continue
            for candidate in (lines[index], *lines[index + 1 : index + 2]):
                offset = at + len(label) if candidate is lines[index] else 0
                hits = [h for h in find_dates(candidate, order=order) if h.end > offset]
                full = [h for h in hits if h.value is not None]
                if full:
                    return full[0]
    return None


def detect_period(
    view: DocumentView,
    *,
    order: DateOrder | None = None,
    transaction_dates: Sequence[date] = (),
) -> Period:
    """The document's period: explicit if printed, else inferred from transaction dates."""
    for start, end in view.statement_periods:
        if start and end and _plausible(start, end):
            return Period(start, end, False, "statement", 0.98)
    for index, low in enumerate(view.normalised):
        for label in _PERIOD_LABELS:
            at = low.find(label)
            if at < 0:
                continue
            found = _range_after(view.lines[index], at + len(label), order)
            if found is None and index + 1 < len(view.lines):
                joined = view.lines[index] + " " + view.lines[index + 1]
                found = _range_after(joined, at + len(label), order)
            if found is not None:
                start, end, ambiguous = found
                return Period(start, end, False, "label", 0.8 if ambiguous else 0.95)
    opening = _labelled_date(view.lines, view.normalised, _OPENING_LABELS, order)
    closing = _labelled_date(view.lines, view.normalised, _CLOSING_LABELS, order)
    if opening is not None and closing is not None:
        start_value, end_value = opening.value, closing.value
        if start_value and end_value and _plausible(start_value, end_value):
            ambiguous = opening.ambiguous or closing.ambiguous
            return Period(start_value, end_value, False, "open_close", 0.8 if ambiguous else 0.9)
    dates = list(transaction_dates) or list(view.transaction_dates)
    source = "transactions"
    ambiguous = False
    if not dates:
        source = "line_dates"
        for line in view.lines:
            hits = find_dates(line, order=order)
            if hits and hits[0].start <= 2 and hits[0].value is not None:
                dates.append(hits[0].value)
                ambiguous |= hits[0].ambiguous
    if dates:
        start, end = min(dates), max(dates)
        if _plausible(start, end):
            confidence = 0.7 if source == "transactions" else 0.55
            return Period(start, end, True, source, confidence - (0.1 if ambiguous else 0.0))
    return Period()


def detect_printed_date(view: DocumentView, *, order: DateOrder | None = None) -> PrintedDate:
    """The date a receipt or invoice was issued."""
    hit = _labelled_date(view.lines, view.normalised, _PRINTED_LABELS, order)
    if hit is not None and hit.value is not None:
        return PrintedDate(hit.value, "label", 0.75 if hit.ambiguous else 0.9)
    for line in view.lines:
        for candidate in find_dates(line, order=order):
            if candidate.value is not None:
                confidence = 0.5 if candidate.ambiguous else 0.6
                return PrintedDate(candidate.value, "first_date", confidence)
    return PrintedDate()


def detect_tax_year(view: DocumentView) -> TaxYear:
    """The tax year printed on a tax form."""
    for low in view.normalised:
        for pattern in _TAX_YEAR:
            m = pattern.search(low)
            if m is not None:
                year = int(m.group(1))
                if 1900 <= year <= 2200:
                    return TaxYear(year, "label", 0.95)
    return TaxYear()

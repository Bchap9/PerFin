# SPDX-License-Identifier: AGPL-3.0-or-later
"""Likely causes of a statement that does not add up, most likely first.

Pure: nothing here reads the database or a file. The reconcile view
(``app.domain.review.reconciliation``) hands in the difference, the ledger
rows of the statement and what else it knows, and shows the answer beside
the rows.

**The difference** is ``opening + total - closing`` in ledger terms, so a
row too many has the difference's sign, a row read with the wrong sign is
half of it, and a row that is missing is minus the difference.

**Order** (:func:`rank_causes`), at most :data:`CAUSES_MAX` in all:

1. ``earlier_reading``: rows kept from an earlier reading of the file;
2. ``opening_differs_from_previous``: the opening balance is not the
   closing balance of the account's previous statement;
3. ``extra_row``: a row whose amount is the difference;
4. ``sign_flipped``: a row whose amount is half the difference;
5. ``duplicated_row``: rows with the same date and amount, when that
   amount is the difference;
6. ``fee_or_interest``: a fee or interest line of the document as large as
   the difference (:func:`fee_lines`);
7. ``transposed_digits``: the difference in cents divides by nine, and
   none of 3 to 6 was found;
8. ``out_of_period``: rows dated outside the statement's period;
9. ``missing_row``: the difference is not zero and none of 3 to 7 was found.

Kinds 3 to 6 name one candidate each (one row, one pair, one line): each
is something a person can act on by itself. A kind is listed at most
:data:`PER_KIND_MAX` times, so that six equal rows do not push every other
explanation off the list. Without a difference (none, or within
:data:`~app.ingestion.reconcile.TOLERANCE`) only 1, 2 and 8 are named: they
are worth knowing about a statement that balances.

A cause carries row ids, an amount and a place on a page: never a payee or
any text of the document.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from app.ingestion.parsers.base import DocumentView
from app.ingestion.parsers.text import find_amounts
from app.ingestion.reconcile import TOLERANCE

#: Causes returned for one statement.
CAUSES_MAX: Final = 8
#: Causes of one kind (module doc).
PER_KIND_MAX: Final = 3
#: Lines :func:`fee_lines` returns for one document.
FEE_LINES_MAX: Final = 50
#: Words that make a line a fee or interest line (whole words, normalised text).
FEE_WORDS: Final = ("fee", "interest", "finance charge", "service charge")

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class LedgerRow:
    id: uuid.UUID
    posted: date
    amount: Decimal
    #: Kept from an earlier reading of the file (the new reading has no such row).
    earlier: bool = False


@dataclass(frozen=True)
class PageAmount:
    """One amount printed on a line of a page (as printed, sign included)."""

    amount: Decimal
    page: int
    bbox: BBox


@dataclass(frozen=True)
class Cause:
    kind: str
    rows: tuple[uuid.UUID, ...] = ()
    amount: Decimal | None = None
    page: int | None = None
    bbox: BBox | None = None


def _same(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= TOLERANCE


def _divides_by_nine(difference: Decimal) -> bool:
    """``difference`` is a whole, non-zero number of cents that divides by nine."""
    cents = difference * 100
    return cents == cents.to_integral_value() and cents != 0 and cents % 9 == 0


def _pairs(rows: Sequence[LedgerRow], difference: Decimal) -> list[Cause]:
    groups: dict[tuple[date, Decimal], list[uuid.UUID]] = {}
    for row in rows:
        if _same(row.amount, difference):
            groups.setdefault((row.posted, row.amount), []).append(row.id)
    return [
        Cause("duplicated_row", tuple(ids), amount)
        for (_, amount), ids in groups.items()
        if len(ids) > 1
    ]


def _outside(
    rows: Sequence[LedgerRow], period: tuple[date, date] | None, grace_days: int
) -> list[Cause]:
    if period is None:
        return []
    grace = timedelta(days=max(grace_days, 0))
    start, end = period[0] - grace, period[1] + grace
    found = tuple(r.id for r in rows if r.posted < start or r.posted > end)
    return [Cause("out_of_period", found)] if found else []


def rank_causes(
    difference: Decimal | None,
    rows: Sequence[LedgerRow],
    *,
    period: tuple[date, date] | None,
    opening: Decimal | None,
    previous_closing: Decimal | None,
    fee_lines: Sequence[PageAmount] = (),
    grace_days: int = 0,
) -> list[Cause]:
    """What may explain ``difference``, most likely first (module doc)."""
    causes: list[Cause] = []
    earlier = tuple(r.id for r in rows if r.earlier)
    if earlier:
        causes.append(Cause("earlier_reading", earlier))
    if (
        opening is not None
        and previous_closing is not None
        and not _same(opening, previous_closing)
    ):
        causes.append(Cause("opening_differs_from_previous", (), previous_closing))
    outside = _outside(rows, period, grace_days)
    if difference is None or abs(difference) <= TOLERANCE:
        return (causes + outside)[:CAUSES_MAX]

    half = difference / 2
    explained: list[Cause] = []
    for found in (
        [Cause("extra_row", (r.id,), r.amount) for r in rows if _same(r.amount, difference)],
        [Cause("sign_flipped", (r.id,), r.amount) for r in rows if _same(r.amount, half)],
        _pairs(rows, difference),
        [
            Cause("fee_or_interest", (), -difference, line.page, line.bbox)
            for line in fee_lines
            if _same(abs(line.amount), abs(difference))
        ],
    ):
        explained.extend(found[:PER_KIND_MAX])
    if not explained and _divides_by_nine(difference):
        explained.append(Cause("transposed_digits"))
    causes.extend(explained)
    causes.extend(outside)
    if not explained:
        causes.append(Cause("missing_row", (), -difference))
    return causes[:CAUSES_MAX]


def fee_lines(view: DocumentView, pages: Collection[int] | None = None) -> list[PageAmount]:
    """The fee and interest lines of ``view`` (on ``pages`` when given).

    A line counts when its normalised text holds one of :data:`FEE_WORDS` as
    whole words and exactly one amount: a summary grid printing fees beside
    interest, or a line that only mentions a rate, says nothing about one
    missing row. At most :data:`FEE_LINES_MAX`.
    """
    out: list[PageAmount] = []
    for line in view.lines:
        if pages is not None and line.page not in pages:
            continue
        norm = f" {line.norm} "
        if not any(f" {word} " in norm for word in FEE_WORDS):
            continue
        amounts = find_amounts(line.text)
        if len(amounts) != 1:
            continue
        out.append(PageAmount(amounts[0].value, line.page, line.bbox))
        if len(out) >= FEE_LINES_MAX:
            break
    return out

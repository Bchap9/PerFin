# SPDX-License-Identifier: AGPL-3.0-or-later
"""Statement reconciliation: opening balance + transactions = closing balance.

SECURITY.md C12 (data poisoning: a crafted statement inserts fake
transactions). A statement read from a PDF or a scan is only trusted when
its own figures agree:

* **Totals**: ``opening + sum(amounts)`` must equal ``closing`` within
  :data:`TOLERANCE` (less than one cent: rounding, never a missing row).
* **Running balance**: where the statement prints a balance after each row,
  every balance must equal the previous one plus the row's amount; the rows
  that disagree are listed (:attr:`Reconciliation.running_mismatches`).

The outcome is a :class:`app.ingestion.sandbox.protocol.Reconciliation`
stored with the statement. The document parsers lower the confidence of the
rows a statement that does not reconcile leaves in doubt
(:func:`doubtful_rows`; each is imported with ``review_state = pending``)
and the importer raises a ``low_confidence_field`` review item describing
the discrepancy and writes no balance snapshot from it
(``app.domain.imports.service``). A statement that states neither balance
and has no running balance cannot be checked (``unavailable``): every row of
it is in doubt.

**Which rows are in doubt** (:func:`doubtful_rows`). A printed running
balance vouches for its row: the balance is the one before plus the row's
amount (rows printed without a balance between two balances are vouched for
together, when the balances agree over all of them). On a statement that
prints running balances, only the rows no balance vouches for are in
doubt. On one that prints none, the one row whose amount is the whole
difference (a row too many) is in doubt alone; otherwise every row is.

**Order and sections.** Two more checks serve the parsers and decide
nothing by themselves. :func:`newest_first` tells a statement printed with
its latest row on top, whose running balances only agree once the rows are
put back in the order they happened. :func:`section_mismatches` compares the
total a section prints ("Total deposits") with the rows read under it; the
sign of the printed total is not compared, since a section of withdrawals
usually prints its total without one.

**A sign that may be a separator.** ``Ending Balance - $992.50`` prints a
dash between a label and its figure: a minus, or only a separator. The
parser says which balances were read that way and :func:`settle_signs`
takes, for each, the reading the rows agree with (the other balance and the
rows' total, or the running balance printed beside the first or last row).
A balance nothing settles is not given at all: a statement held for want of
a balance is better than one recorded with the wrong sign.

Pure functions on :class:`~decimal.Decimal`; no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from typing import Final

from app.core.money import sum_amounts
from app.ingestion.sandbox.protocol import Reconciliation

#: Largest difference accepted as rounding (strictly less than one cent).
TOLERANCE: Final = Decimal("0.005")


@dataclass(frozen=True)
class Row:
    amount: Decimal
    #: The balance the statement prints after the row (None: not printed).
    balance: Decimal | None = None


def newest_first(dates: Sequence[date | None]) -> bool:
    """True when the rows are printed latest first.

    Rows without a date (None) are ignored. At least three dates must be
    present, none later than the one before it, and the first later than the
    last: two rows, or rows all of one day, show no order.
    """
    present = [day for day in dates if day is not None]
    if len(present) < 3 or present[0] <= present[-1]:
        return False
    return all(later <= earlier for earlier, later in pairwise(present))


def section_mismatches(
    rows: Sequence[Row], sections: Sequence[int], totals: Mapping[int, Decimal]
) -> list[int]:
    """Ids (sorted) of the sections whose printed total is not the sum of their rows.

    ``sections[i]`` is the section of ``rows[i]``: build both from one list,
    since a different length raises ``ValueError`` (a check on the wrong rows
    would be worse than none). The rows of a section are added with their
    signs, so a reversal takes away from the total; only the magnitudes of
    that sum and of the printed total are compared, so the sign of the printed
    total does not matter. A section without a printed total is not checked;
    a total printed for a section with no rows is compared against zero.

    A total says how much, never which way: rows all read with the wrong sign
    still add up to it, so a section total never vouches for a row.
    """
    if len(sections) != len(rows):
        raise ValueError("sections must name one section per row")
    out: list[int] = []
    for section in sorted(totals):
        read = sum_amounts(
            row.amount for row, owner in zip(rows, sections, strict=True) if owner == section
        )
        if abs(abs(read) - abs(totals[section])) > TOLERANCE:
            out.append(section)
    return out


def running_mismatches(opening: Decimal | None, rows: Sequence[Row]) -> list[int]:
    """Indices of rows whose printed balance is not the previous one plus the amount.

    The first printed balance is checked against ``opening`` when it is
    known; a row without a printed balance carries the expected balance on.
    """
    out: list[int] = []
    previous = opening
    for index, row in enumerate(rows):
        expected = previous + row.amount if previous is not None else None
        if row.balance is None:
            previous = expected
            continue
        if expected is not None and abs(expected - row.balance) > TOLERANCE:
            out.append(index)
        previous = row.balance
    return out


def doubtful_rows(
    opening: Decimal | None, rows: Sequence[Row], difference: Decimal | None
) -> list[int]:
    """Indices of the rows a statement that does not reconcile leaves in doubt (module doc)."""
    if any(row.balance is not None for row in rows):
        vouched: set[int] = set()
        waiting: list[int] = []
        previous = opening
        for index, row in enumerate(rows):
            expected = previous + row.amount if previous is not None else None
            if row.balance is None:
                waiting.append(index)
                previous = expected
                continue
            if expected is not None and abs(expected - row.balance) <= TOLERANCE:
                vouched.update((*waiting, index))
            waiting = []
            previous = row.balance
        return [index for index in range(len(rows)) if index not in vouched]
    if difference:
        surplus = [i for i, row in enumerate(rows) if abs(row.amount - difference) <= TOLERANCE]
        if len(surplus) == 1:
            return surplus
    return list(range(len(rows)))


def settle_signs(
    opening: Decimal | None,
    closing: Decimal | None,
    rows: Sequence[Row],
    *,
    opening_open: bool,
    closing_open: bool,
) -> tuple[Decimal | None, Decimal | None]:
    """``(opening, closing)`` with each open sign settled (module doc).

    A balance whose sign is open (``opening_open``, ``closing_open``) may be
    the figure given or its negation. The pair of readings that agrees with
    everything the rows can check is taken when there is exactly one: the
    opening balance against the balance before the first row, the closing
    one against the balance after the last, and the two against the rows'
    total. An open balance that nothing checks, or that no reading or more
    than one fits, becomes None. A balance whose sign is not open is never
    changed.
    """
    opening_open = opening_open and opening is not None
    closing_open = closing_open and closing is not None
    if not (opening_open or closing_open):
        return opening, closing
    total = sum_amounts(row.amount for row in rows)
    before = rows[0].balance - rows[0].amount if rows and rows[0].balance is not None else None
    after = rows[-1].balance if rows else None

    def fits(first: Decimal | None, last: Decimal | None) -> bool:
        if first is not None and before is not None and abs(first - before) > TOLERANCE:
            return False
        if last is not None and after is not None and abs(last - after) > TOLERANCE:
            return False
        if first is not None and last is not None:
            return abs(first + total - last) <= TOLERANCE
        return True

    def readings(value: Decimal | None, is_open: bool) -> list[Decimal | None]:
        return [value, -value] if is_open and value is not None else [value]

    withheld = (None if opening_open else opening, None if closing_open else closing)
    if opening_open and before is None and closing is None:
        return withheld
    if closing_open and after is None and opening is None:
        return withheld
    fitting = {
        (first, last)
        for first in readings(opening, opening_open)
        for last in readings(closing, closing_open)
        if fits(first, last)
    }
    return next(iter(fitting)) if len(fitting) == 1 else withheld


def reconcile(
    opening: Decimal | None, closing: Decimal | None, rows: Sequence[Row]
) -> Reconciliation:
    """Check a statement's figures (module doc)."""
    total = sum_amounts(row.amount for row in rows)
    mismatches = running_mismatches(opening, rows)
    if opening is None or closing is None:
        has_running = any(row.balance is not None for row in rows)
        # Without both totals only the running balance can be checked.
        status = "mismatch" if mismatches else ("ok" if has_running else "unavailable")
        return Reconciliation(
            status=status,  # type: ignore[arg-type]
            opening=opening,
            closing=closing,
            total=total,
            difference=None,
            tolerance=TOLERANCE,
            running_mismatches=mismatches,
        )
    difference = opening + total - closing
    ok = abs(difference) <= TOLERANCE and not mismatches
    return Reconciliation(
        status="ok" if ok else "mismatch",
        opening=opening,
        closing=closing,
        total=total,
        difference=difference,
        tolerance=TOLERANCE,
        running_mismatches=mismatches,
    )

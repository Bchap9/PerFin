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
stored with the statement. The document parsers lower the confidence of
every row of a statement that does not reconcile (so each is imported with
``review_state = pending``) and the importer raises a
``low_confidence_field`` review item describing the discrepancy and writes
no balance snapshot from it (``app.domain.imports.service``). A statement
that states neither balance and has no running balance cannot be checked
(``unavailable``) and is treated the same way.

Pure functions on :class:`~decimal.Decimal`; no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
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

# SPDX-License-Identifier: AGPL-3.0-or-later
"""``transaction.import_hash`` and payee normalisation (DATA_MODEL.md "Ledger").

The import hash recognises the same transaction when it arrives again: an
overlapping CSV export, the OFX file for a month already imported from a
statement PDF. It is::

    sha256("perfin-import-v1" US account_id US posted_date US amount US payee US index)

as 64 lower-case hex digits, where ``US`` is the ASCII unit separator
(``\\x1f``, which cannot occur in any field after normalisation) and:

* ``account_id``: the account's UUID in canonical lower-case form;
* ``posted_date``: ISO ``YYYY-MM-DD``;
* ``amount``: the signed amount rounded half-even to 6 decimals and written
  as a plain decimal (``-12.500000``; ``-0`` is ``0.000000``), so ``-12.5``
  and ``-12.50`` hash alike;
* ``payee``: :func:`normalize_payee` of the raw payee text (empty when
  there is none);
* ``index``: the **running index within the day**: ``0`` for the first row
  of a source with this account, date, amount and normalised payee, ``1``
  for the second identical row in the same source, and so on, counted in the
  source's order (:func:`assign_import_hashes`). Counting only identical
  rows (not every row of the day) keeps the hash stable when another source
  covers the same day with more or fewer other transactions, while two real
  identical purchases on one day still get different hashes.

:func:`normalize_payee` (also ``payee.normalized_name``):

1. Unicode NFKC, then case folding (``"CAFÉ"`` and ``"café"`` match);
2. decomposition (NFKD) with combining marks removed (``"cafe"`` too);
3. every run of characters that are not letters or digits becomes one space
   (punctuation, ``*``, ``#``, control and separator characters);
4. leading and trailing spaces removed; the result is cut to
   :data:`PAYEE_KEY_MAX` characters.

Digits are kept (store numbers and references often tell payees apart).
Changing any of this changes every hash: bump :data:`HASH_VERSION` and keep
the old function for existing rows.
"""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from app.core.money import quantize

HASH_VERSION: Final = "perfin-import-v1"
PAYEE_KEY_MAX: Final = 200
_SEPARATOR: Final = "\x1f"


def normalize_payee(raw: str | None) -> str:
    """Comparison key for a payee string (see the module doc for the steps)."""
    if not raw:
        return ""
    folded = unicodedata.normalize("NFKC", raw).casefold()
    decomposed = unicodedata.normalize("NFKD", folded)
    out: list[str] = []
    pending_space = False
    for char in decomposed:
        if unicodedata.combining(char):
            continue
        if char.isalnum():
            if pending_space and out:
                out.append(" ")
            pending_space = False
            out.append(char)
        else:
            pending_space = True
    return "".join(out)[:PAYEE_KEY_MAX].rstrip()


def canonical_amount(amount: Decimal) -> str:
    """The amount as hashed: half-even to 6 decimals, plain notation, no ``-0``."""
    return f"{quantize(amount):f}"


def import_hash(
    account_id: uuid.UUID,
    posted_date: date,
    amount: Decimal,
    raw_payee: str | None,
    index: int,
) -> str:
    """The stable deduplication hash of one transaction (module doc)."""
    if index < 0:
        raise ValueError("index must be >= 0")
    fields = (
        HASH_VERSION,
        str(account_id),
        posted_date.isoformat(),
        canonical_amount(amount),
        normalize_payee(raw_payee),
        str(index),
    )
    return hashlib.sha256(_SEPARATOR.join(fields).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HashInput:
    """One parsed row: enough to compute its import hash."""

    posted_date: date
    amount: Decimal
    raw_payee: str | None


def assign_import_hashes(account_id: uuid.UUID, rows: Iterable[HashInput]) -> list[str]:
    """Hashes for ``rows`` of one source, in order, with running indexes.

    The index of a row is how many earlier rows of ``rows`` have the same
    date, amount and normalised payee.
    """
    seen: dict[tuple[date, str, str], int] = {}
    hashes: list[str] = []
    for row in rows:
        key = (row.posted_date, canonical_amount(row.amount), normalize_payee(row.raw_payee))
        index = seen.get(key, 0)
        seen[key] = index + 1
        hashes.append(import_hash(account_id, row.posted_date, row.amount, row.raw_payee, index))
    return hashes


def duplicate_hashes(existing: Iterable[str | None], incoming: Sequence[str]) -> set[str]:
    """Incoming hashes already present in ``existing`` (candidates for review)."""
    known = {h for h in existing if h}
    return {h for h in incoming if h in known}

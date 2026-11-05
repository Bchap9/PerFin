# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deduplication of imported transactions (INGESTION.md "Match and deduplicate").

Every parsed transaction gets its ``import_hash``
(:func:`app.domain.transactions.import_hash.assign_import_hashes`: account,
date, amount, normalised payee and the running index of identical rows in the
same source). Against the account's existing transactions:

* **exact**: an existing row with the same hash is the same transaction (an
  overlapping export, the same statement imported again): the incoming row
  is skipped. Overlapping statement periods need nothing more than this.
* **near**: no hash match, but an existing row on the same date with the same
  amount that no exact match has claimed (payee text spelled differently by
  another source, or a row entered by hand): the incoming row is imported
  with ``review_state = pending`` and a ``possible_duplicate`` review item
  names the candidates. Each existing row backs at most one near match (in
  source order), so a second identical purchase that day is simply new.
* everything else is **new**.

:func:`plan` is pure (unit-tested without a database); :func:`load_existing`
reads the candidates: the account's transactions inside the incoming date
range (a hash includes the date, so nothing outside it can match), plus their
``import_hash_alias`` rows (``app.domain.review.repo.alias_rows``): the hash of
an imported row a person confirmed as a duplicate of a transaction that
already had a hash of its own, so the same statement line matches that kept
transaction exactly next time. An alias is one more :class:`Existing` entry
for the same transaction id; the near-match pass counts each id once.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.review.repo import alias_rows
from app.domain.transactions.import_hash import (
    HashInput,
    assign_import_hashes,
    canonical_amount,
)
from app.domain.transactions.models import Transaction

#: Most candidate ids named in one ``possible_duplicate`` review item.
MAX_CANDIDATES = 10


@dataclass(frozen=True)
class Existing:
    """What dedupe needs to know about an existing transaction."""

    id: uuid.UUID
    posted_date: date
    amount: Decimal
    import_hash: str | None


@dataclass
class DedupePlan:
    """The decision for each incoming row (indexes into the incoming list)."""

    hashes: list[str]
    new: list[int] = field(default_factory=list)
    exact: list[int] = field(default_factory=list)
    near: dict[int, list[uuid.UUID]] = field(default_factory=dict)

    @property
    def to_insert(self) -> list[int]:
        """New and near rows, in source order."""
        return sorted([*self.new, *self.near])


def plan(
    hashes: Sequence[str], rows: Sequence[HashInput], existing: Sequence[Existing]
) -> DedupePlan:
    """Classify ``rows`` (with their ``hashes``) against ``existing`` (module doc)."""
    if len(hashes) != len(rows):
        raise ValueError("one hash per row")
    by_hash: dict[str, list[Existing]] = defaultdict(list)
    by_day: dict[tuple[date, str], list[Existing]] = defaultdict(list)
    seen: set[uuid.UUID] = set()
    for row in existing:
        if row.import_hash:
            by_hash[row.import_hash].append(row)
        if row.id in seen:
            continue  # an alias hash of a transaction already listed
        seen.add(row.id)
        by_day[(row.posted_date, canonical_amount(row.amount))].append(row)
    result = DedupePlan(hashes=list(hashes))
    claimed: set[uuid.UUID] = set()
    for index, digest in enumerate(hashes):
        matches = by_hash.get(digest)
        if matches:
            result.exact.append(index)
            claimed.update(m.id for m in matches)
    exact = set(result.exact)
    for index, incoming in enumerate(rows):
        if index in exact:
            continue
        candidates = [
            e
            for e in by_day.get((incoming.posted_date, canonical_amount(incoming.amount)), ())
            if e.id not in claimed
        ]
        if candidates:
            result.near[index] = sorted(e.id for e in candidates)[:MAX_CANDIDATES]
            # One existing row backs one near match.
            claimed.add(min(e.id for e in candidates))
        else:
            result.new.append(index)
    return result


def load_existing(
    db: Session, household_id: uuid.UUID, account_id: uuid.UUID, rows: Sequence[HashInput]
) -> list[Existing]:
    """The account's transactions inside the date range of ``rows``, with their aliases."""
    if not rows:
        return []
    first = min(r.posted_date for r in rows)
    last = max(r.posted_date for r in rows)
    result = db.execute(
        select(Transaction.id, Transaction.posted_date, Transaction.amount, Transaction.import_hash)
        .where(
            Transaction.household_id == household_id,
            Transaction.account_id == account_id,
            Transaction.posted_date >= first,
            Transaction.posted_date <= last,
        )
        .order_by(Transaction.posted_date, Transaction.id)
    )
    own = [Existing(i, d, a, h) for i, d, a, h in result]
    aliases = [
        Existing(i, d, a, h) for i, d, a, h in alias_rows(db, household_id, account_id, first, last)
    ]
    return [*own, *aliases]


def dedupe(
    db: Session, household_id: uuid.UUID, account_id: uuid.UUID, rows: Sequence[HashInput]
) -> DedupePlan:
    """Hash ``rows`` for ``account_id`` and classify them against the ledger."""
    hashes = assign_import_hashes(account_id, rows)
    return plan(hashes, rows, load_existing(db, household_id, account_id, rows))

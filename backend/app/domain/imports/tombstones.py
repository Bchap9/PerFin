# SPDX-License-Identifier: AGPL-3.0-or-later
"""Import tombstones: a deleted imported row stays deleted.

``import_tombstone`` (``app.domain.imports.models``) keeps the
``import_hash`` of an imported ledger row a person deleted, per account.
The deduplicator leaves a tombstoned hash out of every later import into
that account (the same document read again, or the same statement arriving
as another file); the reconcile view lists the removed rows of a statement
and can take one back ("Put back").

The functions here only read and write the table. Who may delete a row, or
take a tombstone back, is decided by the callers
(``app.domain.transactions.service``, ``app.domain.review.reconciliation``),
which also write the audit rows. Every query names the household as well as
relying on row-level security.

A tombstone carries the row's date and amount (to list it) and never its
payee, memo or any other text. A row a person typed has no ``import_hash``:
nothing could bring it back, so it leaves no tombstone.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.domain.imports.models import ImportTombstone
from app.domain.transactions.models import Transaction


def add_tombstone(db: Session, row: Transaction, *, user_id: uuid.UUID | None) -> bool:
    """Remember that ``row`` was deleted; True when a tombstone was written.

    False (and nothing written) for a row without an ``import_hash``, and
    for a hash that already has a tombstone on the row's account (the first
    one is kept as it is).
    """
    if row.import_hash is None:
        return False
    written = db.execute(
        insert(ImportTombstone)
        .values(
            household_id=row.household_id,
            account_id=row.account_id,
            import_hash=row.import_hash,
            deleted_at=utcnow(),
            deleted_by=user_id,
            posted_date=row.posted_date,
            amount=row.amount,
        )
        .on_conflict_do_nothing(constraint="uq_import_tombstone_hash")
        .returning(ImportTombstone.id)
    ).first()
    return written is not None


def tombstoned_hashes(
    db: Session, household_id: uuid.UUID, account_id: uuid.UUID, hashes: Collection[str]
) -> set[str]:
    """Those of ``hashes`` that have a tombstone on ``account_id``."""
    if not hashes:
        return set()
    found = db.execute(
        select(ImportTombstone.import_hash).where(
            ImportTombstone.household_id == household_id,
            ImportTombstone.account_id == account_id,
            ImportTombstone.import_hash.in_(sorted(set(hashes))),
        )
    ).scalars()
    return {str(value) for value in found}


def list_tombstones(
    db: Session, household_id: uuid.UUID, account_id: uuid.UUID, hashes: Collection[str]
) -> list[ImportTombstone]:
    """The tombstones of ``account_id`` for ``hashes``, oldest deletion first."""
    if not hashes:
        return []
    return list(
        db.execute(
            select(ImportTombstone)
            .where(
                ImportTombstone.household_id == household_id,
                ImportTombstone.account_id == account_id,
                ImportTombstone.import_hash.in_(sorted(set(hashes))),
            )
            .order_by(ImportTombstone.deleted_at, ImportTombstone.id)
        ).scalars()
    )


def remove_tombstone(
    db: Session, household_id: uuid.UUID, tombstone_id: uuid.UUID
) -> ImportTombstone | None:
    """Take a tombstone back; the removed row (detached), or None when there is none."""
    tombstone = db.execute(
        select(ImportTombstone).where(
            ImportTombstone.household_id == household_id, ImportTombstone.id == tombstone_id
        )
    ).scalar_one_or_none()
    if tombstone is None:
        return None
    db.execute(
        delete(ImportTombstone).where(
            ImportTombstone.household_id == household_id, ImportTombstone.id == tombstone.id
        )
    )
    db.expunge(tombstone)
    return tombstone

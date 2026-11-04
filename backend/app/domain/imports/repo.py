# SPDX-License-Identifier: AGPL-3.0-or-later
"""Queries for import mappings and the import step (always filtered by household too)."""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import ColumnElement, select, true, update
from sqlalchemy.orm import Session

from app.core import clock
from app.domain.documents.models import IngestJob, IngestStage, ReviewItem, ReviewItemState
from app.domain.imports.models import ImportMapping
from app.domain.transactions.import_hash import normalize_payee
from app.domain.transactions.models import Category, Payee


def list_mappings(
    db: Session,
    household_id: uuid.UUID,
    visible: ColumnElement[bool],
    *,
    after: uuid.UUID | None,
    limit: int,
) -> list[ImportMapping]:
    query = select(ImportMapping).where(ImportMapping.household_id == household_id, visible)
    if after is not None:
        query = query.where(ImportMapping.id > after)
    return list(db.execute(query.order_by(ImportMapping.id).limit(limit)).scalars())


def get_mapping(
    db: Session,
    household_id: uuid.UUID,
    mapping_id: uuid.UUID,
    visible: ColumnElement[bool] | None = None,
    *,
    lock: bool = False,
) -> ImportMapping | None:
    query = select(ImportMapping).where(
        ImportMapping.household_id == household_id,
        ImportMapping.id == mapping_id,
        visible if visible is not None else true(),
    )
    if lock:
        query = query.with_for_update()
    return db.execute(query).scalar_one_or_none()


def mappings_by_fingerprint(
    db: Session, household_id: uuid.UUID, fingerprints: Collection[str]
) -> list[ImportMapping]:
    if not fingerprints:
        return []
    return list(
        db.execute(
            select(ImportMapping)
            .where(
                ImportMapping.household_id == household_id,
                ImportMapping.header_fingerprint.in_(list(fingerprints)),
            )
            .order_by(ImportMapping.last_used_at.desc().nulls_last(), ImportMapping.id)
        ).scalars()
    )


def name_taken(
    db: Session, household_id: uuid.UUID, name: str, *, exclude: uuid.UUID | None = None
) -> bool:
    query = select(ImportMapping.id).where(
        ImportMapping.household_id == household_id, ImportMapping.name == name
    )
    if exclude is not None:
        query = query.where(ImportMapping.id != exclude)
    return db.execute(query.limit(1)).scalar_one_or_none() is not None


def mark_used(db: Session, mapping: ImportMapping) -> None:
    db.execute(
        update(ImportMapping)
        .where(ImportMapping.household_id == mapping.household_id, ImportMapping.id == mapping.id)
        .values(use_count=ImportMapping.use_count + 1, last_used_at=clock.utcnow())
        .execution_options(synchronize_session=False)
    )


def payee_lookup(db: Session, household_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Normalised payee name or alias -> payee id (names win over aliases)."""
    rows = list(
        db.execute(
            select(Payee.id, Payee.normalized_name, Payee.aliases)
            .where(Payee.household_id == household_id)
            .order_by(Payee.id)
        )
    )
    lookup: dict[str, uuid.UUID] = {}
    for payee_id, _, aliases in rows:
        for alias in aliases or ():
            key = normalize_payee(alias)
            if key:
                lookup.setdefault(key, payee_id)
    for payee_id, normalized, _ in rows:
        lookup[normalized] = payee_id
    return lookup


def category_lookup(db: Session, household_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Case-folded category path (``Parent:Child``) and unique leaf name -> id."""
    rows = list(
        db.execute(
            select(Category.id, Category.parent_id, Category.name).where(
                Category.household_id == household_id
            )
        )
    )
    by_id = {cid: (parent, name) for cid, parent, name in rows}

    def path(cid: uuid.UUID) -> str:
        parts: list[str] = []
        seen: set[uuid.UUID] = set()
        current: uuid.UUID | None = cid
        while current is not None and current in by_id and current not in seen:
            seen.add(current)
            parent, name = by_id[current]
            parts.append(name)
            current = parent
        return ":".join(reversed(parts)).casefold()

    lookup: dict[str, uuid.UUID] = {}
    leaf_count: dict[str, int] = {}
    for _, _, name in rows:
        leaf_count[name.casefold()] = leaf_count.get(name.casefold(), 0) + 1
    for cid, _, name in rows:
        lookup[path(cid)] = cid
        if leaf_count[name.casefold()] == 1:
            lookup.setdefault(name.casefold(), cid)
    return lookup


def open_review_items(
    db: Session, household_id: uuid.UUID, document_id: uuid.UUID, kinds: Collection[str]
) -> list[ReviewItem]:
    return list(
        db.execute(
            select(ReviewItem)
            .where(
                ReviewItem.household_id == household_id,
                ReviewItem.document_id == document_id,
                ReviewItem.state == ReviewItemState.OPEN,
                ReviewItem.kind.in_(list(kinds)),
            )
            .with_for_update()
        ).scalars()
    )


def last_import_log(
    db: Session, household_id: uuid.UUID, document_id: uuid.UUID
) -> dict[str, object] | None:
    """The facts of the latest finished parse stage of the document."""
    row = db.execute(
        select(IngestJob.log)
        .where(
            IngestJob.household_id == household_id,
            IngestJob.document_id == document_id,
            IngestJob.stage == IngestStage.PARSE,
            IngestJob.finished_at.is_not(None),
        )
        .order_by(IngestJob.started_at.desc(), IngestJob.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return dict(row) if row is not None else None

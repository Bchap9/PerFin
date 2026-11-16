# SPDX-License-Identifier: AGPL-3.0-or-later
"""The household's counted corrections (``merchant_memory``): the "learned" source.

A person's category choice on a transaction with a merchant key is one
*decision* for ``(merchant key, direction, category)``. :func:`record` adds
one, :func:`withdraw` takes one back (the person changed their mind on that
row), so the memory counts rows, not clicks. Deleting a transaction leaves
the memory as it is: that is why it is a table.

:func:`lookup` gives the answer for a key: the category with the most
decisions, the most recently decided on a tie, with its ``share`` of all
decisions for the key (two places). Whether that answer is strong enough to
be written as a fact, and how old is too old (:data:`STALE_DAYS`), is the
engine's decision, not this module's.

The table is bounded at :data:`MAX_ROWS` per household: a new row beyond
that evicts the rows decided longest ago. Entries can be listed
(:func:`entries`) and forgotten (:func:`forget`), and are seeded once from
the categories a household set before provenance existed
(:func:`seed_from_legacy`).

Functions here only read and write the table, inside the caller's
transaction; who may call them and the audit rows are the callers' business.
Every query names the household as well as relying on row-level security. A
merchant key is derived from statement text: nothing here logs one.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal, cast

from sqlalchemy import case, delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.domain.categorisation.models import MerchantMemory
from app.domain.transactions.models import Transaction

Direction = Literal["out", "in"]

#: Rows kept per household; the least recently decided go first.
MAX_ROWS: Final = 5000
#: Decisions older than this (three years) are only a suggestion.
STALE_DAYS: Final = 1095

_SHARE_PLACES: Final = Decimal("0.01")
_CHUNK: Final = 500


def direction_of(amount: Decimal) -> Direction:
    """``in`` for money arriving in the account, ``out`` otherwise (zero included)."""
    return "in" if amount > 0 else "out"


@dataclass(frozen=True)
class Learned:
    """The household's answer for one merchant key and direction."""

    #: The winning row's id; it names the whole entry (:func:`forget`, paging).
    memory_id: uuid.UUID
    category_id: uuid.UUID
    #: Decisions for the winning category, and for every category of the key.
    decisions: int
    total: int
    #: ``decisions / total``, two places.
    share: Decimal
    last_decided_at: datetime
    merchant_key: str = ""
    direction: Direction = "out"


def _chunks[T](items: Sequence[T]) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), _CHUNK):
        yield items[start : start + _CHUNK]


def _evict(db: Session, household_id: uuid.UUID, keep: uuid.UUID) -> None:
    """Drop the least recently decided rows beyond :data:`MAX_ROWS` (never ``keep``)."""
    count = int(
        db.execute(
            select(func.count())
            .select_from(MerchantMemory)
            .where(MerchantMemory.household_id == household_id)
        ).scalar_one()
    )
    excess = count - MAX_ROWS
    if excess <= 0:
        return
    oldest = (
        select(MerchantMemory.id)
        .where(MerchantMemory.household_id == household_id, MerchantMemory.id != keep)
        .order_by(MerchantMemory.last_decided_at, MerchantMemory.id)
        .limit(excess)
    )
    db.execute(
        delete(MerchantMemory).where(
            MerchantMemory.household_id == household_id, MerchantMemory.id.in_(oldest)
        )
    )


def record(
    db: Session,
    household_id: uuid.UUID,
    key: str,
    direction: Direction,
    category_id: uuid.UUID,
    *,
    at: datetime,
) -> None:
    """One more decision for ``category_id`` on ``key`` (module doc)."""
    statement = insert(MerchantMemory).values(
        household_id=household_id,
        merchant_key=key,
        direction=direction,
        category_id=category_id,
        decisions=1,
        last_decided_at=at,
    )
    written = db.execute(
        statement.on_conflict_do_update(
            constraint="uq_merchant_memory_choice",
            set_={
                "decisions": MerchantMemory.decisions + 1,
                "last_decided_at": func.greatest(
                    MerchantMemory.last_decided_at, statement.excluded.last_decided_at
                ),
                "updated_at": utcnow(),
            },
        ).returning(MerchantMemory.id, MerchantMemory.decisions)
    ).one()
    if written.decisions == 1:
        # A new row: the only time the table grows.
        _evict(db, household_id, written.id)


def withdraw(
    db: Session, household_id: uuid.UUID, key: str, direction: Direction, category_id: uuid.UUID
) -> None:
    """Take one decision back; the row goes at zero. A missing row is no error."""
    match = (
        MerchantMemory.household_id == household_id,
        MerchantMemory.merchant_key == key,
        MerchantMemory.direction == direction,
        MerchantMemory.category_id == category_id,
    )
    gone = db.execute(
        delete(MerchantMemory)
        .where(*match, MerchantMemory.decisions <= 1)
        .returning(MerchantMemory.id)
    ).first()
    if gone is None:
        db.execute(
            update(MerchantMemory)
            .where(*match)
            .values(decisions=MerchantMemory.decisions - 1, updated_at=utcnow())
        )


def _winners(rows: Iterable[MerchantMemory]) -> dict[tuple[str, Direction], Learned]:
    grouped: dict[tuple[str, Direction], list[MerchantMemory]] = defaultdict(list)
    for row in rows:
        grouped[(row.merchant_key, cast("Direction", row.direction))].append(row)
    found: dict[tuple[str, Direction], Learned] = {}
    for (key, direction), choices in grouped.items():
        total = sum(choice.decisions for choice in choices)
        # Most decisions, then the latest; the id only makes a full tie stable.
        best = max(choices, key=lambda c: (c.decisions, c.last_decided_at, c.id.bytes))
        share = (Decimal(best.decisions) / Decimal(total)).quantize(
            _SHARE_PLACES, rounding=ROUND_HALF_UP
        )
        found[(key, direction)] = Learned(
            memory_id=best.id,
            category_id=best.category_id,
            decisions=best.decisions,
            total=total,
            share=share,
            last_decided_at=best.last_decided_at,
            merchant_key=key,
            direction=direction,
        )
    return found


def lookup(
    db: Session, household_id: uuid.UUID, keys: Collection[tuple[str, Direction]]
) -> dict[tuple[str, Direction], Learned]:
    """The household's answer for each of ``keys`` that has one (module doc)."""
    wanted = sorted(set(keys))
    rows: list[MerchantMemory] = []
    for chunk in _chunks(wanted):
        rows.extend(
            db.execute(
                select(MerchantMemory).where(
                    MerchantMemory.household_id == household_id,
                    tuple_(MerchantMemory.merchant_key, MerchantMemory.direction).in_(chunk),
                )
            ).scalars()
        )
    return _winners(rows)


def forget(db: Session, household_id: uuid.UUID, memory_id: uuid.UUID) -> int:
    """Forget the entry ``memory_id`` belongs to: every category of its key and direction.

    Returns the rows removed; 0 when the household has no such row.
    """
    entry = db.execute(
        select(MerchantMemory.merchant_key, MerchantMemory.direction).where(
            MerchantMemory.household_id == household_id, MerchantMemory.id == memory_id
        )
    ).first()
    if entry is None:
        return 0
    removed = db.execute(
        delete(MerchantMemory)
        .where(
            MerchantMemory.household_id == household_id,
            MerchantMemory.merchant_key == entry.merchant_key,
            MerchantMemory.direction == entry.direction,
        )
        .returning(MerchantMemory.id)
    ).all()
    return len(removed)


def entries(
    db: Session, household_id: uuid.UUID, *, after: uuid.UUID | None, limit: int
) -> list[Learned]:
    """Up to ``limit`` entries (one per key and direction), in key order.

    ``after`` is the ``memory_id`` of the last entry of the previous page. An
    id that no longer names a row of the household (the entry was forgotten
    meanwhile) ends the listing instead of starting it over.
    """
    position = tuple_(MerchantMemory.merchant_key, MerchantMemory.direction)
    statement = (
        select(MerchantMemory.merchant_key, MerchantMemory.direction)
        .where(MerchantMemory.household_id == household_id)
        .distinct()
        .order_by(MerchantMemory.merchant_key, MerchantMemory.direction)
        .limit(max(limit, 0))
    )
    if after is not None:
        cursor = db.execute(
            select(MerchantMemory.merchant_key, MerchantMemory.direction).where(
                MerchantMemory.household_id == household_id, MerchantMemory.id == after
            )
        ).first()
        if cursor is None:
            return []
        statement = statement.where(position > tuple_(cursor.merchant_key, cursor.direction))
    page = [(str(key), cast("Direction", direction)) for key, direction in db.execute(statement)]
    found = lookup(db, household_id, page)
    return [found[key] for key in page if key in found]


def seed_from_legacy(db: Session, household_id: uuid.UUID) -> int:
    """Count the categories set before provenance existed as decisions.

    One decision per ``legacy`` transaction with a merchant key and a
    category, grouped by key, direction and category, decided when the row
    was last changed. Only keys (with their direction) that have no entry
    yet are seeded, so what the household taught since is left as it is and
    a second run adds nothing. Never beyond :data:`MAX_ROWS` (the most
    decided first). Returns the rows added.
    """
    direction = case((Transaction.amount > 0, "in"), else_="out")
    decisions = func.count()
    counted = db.execute(
        select(
            Transaction.merchant_key,
            direction,
            Transaction.category_id,
            decisions,
            func.max(Transaction.updated_at),
        )
        .where(
            Transaction.household_id == household_id,
            Transaction.category_source == "legacy",
            Transaction.merchant_key.is_not(None),
            Transaction.category_id.is_not(None),
        )
        .group_by(Transaction.merchant_key, direction, Transaction.category_id)
        .order_by(decisions.desc(), Transaction.merchant_key, direction, Transaction.category_id)
    ).all()
    if not counted:
        return 0
    known = {
        (str(key), str(way))
        for key, way in db.execute(
            select(MerchantMemory.merchant_key, MerchantMemory.direction)
            .where(MerchantMemory.household_id == household_id)
            .distinct()
        )
    }
    held = int(
        db.execute(
            select(func.count())
            .select_from(MerchantMemory)
            .where(MerchantMemory.household_id == household_id)
        ).scalar_one()
    )
    room = max(MAX_ROWS - held, 0)
    now = utcnow()
    new_rows = [
        {
            "household_id": household_id,
            "merchant_key": key,
            "direction": way,
            "category_id": category_id,
            "decisions": int(count),
            "last_decided_at": decided_at,
            "created_at": now,
            "updated_at": now,
        }
        for key, way, category_id, count, decided_at in counted
        if (str(key), str(way)) not in known
    ][:room]
    for chunk in _chunks(new_rows):
        db.execute(insert(MerchantMemory), list(chunk))
    return len(new_rows)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pinned write: how the system changes a transaction without touching it.

The importer reads ``updated_at == created_at`` as "nobody touched this row
since it was imported", and reprocessing a document removes only untouched
rows. A categorisation pass must therefore never look like a person.
:func:`pinned_update` is the one way the system writes a merchant key, a
guess, a suggestion or the link to a recurring series: a Core ``UPDATE``
that sets ``updated_at = updated_at``, so the ORM's ``onupdate`` never
fires. A person's own edit goes through the transactions service and moves
``updated_at``, as before.

Only :data:`PINNED_COLUMNS` can be written this way; anything else is a
programming error (``ValueError``) and nothing is written.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Final, cast

from sqlalchemy import bindparam, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.db.base import Base
from app.domain.transactions.models import Transaction

#: The columns a system write may set.
PINNED_COLUMNS: Final = frozenset(
    {
        "merchant_key",
        "category_id",
        "category_source",
        "category_confidence",
        "category_rule_id",
        "suggested_category_id",
        "scheduled_transaction_id",
    }
)


def pinned_update(
    db: Session, household_id: uuid.UUID, changes: Mapping[uuid.UUID, Mapping[str, Any]]
) -> int:
    """Write ``{transaction id: {column: value}}`` leaving ``updated_at`` alone.

    One ``UPDATE`` per distinct set of columns (many rows each), every one
    limited to ``household_id``. Returns the number of rows changed; an id
    that is not a row of the household changes nothing. ORM instances of the
    changed rows already in the session are expired for the written columns,
    so the caller reads the new values.
    """
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row_id, values in changes.items():
        columns = tuple(sorted(values))
        unknown = sorted(set(columns) - PINNED_COLUMNS)
        if unknown:
            raise ValueError(f"pinned_update cannot write: {', '.join(unknown)}")
        if not columns:
            continue
        params = {"pinned_household": household_id, "pinned_id": row_id}
        params.update({f"pinned_{column}": values[column] for column in columns})
        groups.setdefault(columns, []).append(params)
    table = Base.metadata.tables[Transaction.__tablename__]
    changed = 0
    for columns, rows in groups.items():
        statement = (
            update(table)
            .where(
                table.c.household_id == bindparam("pinned_household"),
                table.c.id == bindparam("pinned_id"),
            )
            .values(
                {
                    **{column: bindparam(f"pinned_{column}") for column in columns},
                    # Explicit, so the column's ``onupdate`` does not apply.
                    "updated_at": table.c.updated_at,
                }
            )
        )
        result = cast("CursorResult[Any]", db.execute(statement, rows))
        changed += result.rowcount
        for row in rows:
            key = Transaction.__mapper__.identity_key_from_primary_key((row["pinned_id"],))
            instance = db.identity_map.get(key)
            if instance is not None:
                db.expire(instance, columns)
    return changed

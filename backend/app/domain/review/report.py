# SPDX-License-Identifier: AGPL-3.0-or-later
"""``perfin review-report``: what the review queue is made of (read-only).

Counts a household's review items by kind and reason (``payload.reason``) and
state, and the documents behind the open ones by file type, so it is clear
which kinds of misses are most common and whether a change helped. Counts
and codes only: no file names, no payload text (H8).
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.rls import set_context
from app.domain.documents.models import Document, ReviewItem, ReviewItemState


def review_report(db: Session, household_id: uuid.UUID) -> dict[str, Any]:
    """The counts (module doc) for one household, in its RLS context."""
    set_context(db, household_id)
    rows = db.execute(
        select(ReviewItem.kind, ReviewItem.state, ReviewItem.payload, Document.mime_detected)
        .outerjoin(
            Document,
            (Document.household_id == ReviewItem.household_id)
            & (Document.id == ReviewItem.document_id),
        )
        .where(ReviewItem.household_id == household_id)
    ).all()
    by_reason: Counter[tuple[str, str, str]] = Counter()
    open_types: Counter[str] = Counter()
    for kind, state, payload, mime in rows:
        reason = (payload or {}).get("reason")
        by_reason[(str(kind), reason if isinstance(reason, str) else "-", str(state))] += 1
        if state is ReviewItemState.OPEN:
            open_types[mime or "none"] += 1
    return {
        "household_id": str(household_id),
        "items": len(rows),
        "open": sum(1 for _, state, _, _ in rows if state is ReviewItemState.OPEN),
        "by_reason": [
            {"kind": kind, "reason": reason, "state": state, "count": count}
            for (kind, reason, state), count in sorted(
                by_reason.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "open_by_file_type": dict(open_types.most_common()),
    }

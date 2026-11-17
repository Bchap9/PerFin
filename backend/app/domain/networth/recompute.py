# SPDX-License-Identifier: AGPL-3.0-or-later
"""Asking for the stored net worth days to be rewritten (``networth.snapshot`` with ``since``).

Kept apart from ``app.domain.networth.service`` (which reads accounts,
reports and investments) so that every writer of a past day can import it
without an import cycle.

* :func:`request_recompute`: queue the household's snapshot job to rewrite
  the stored days from ``since``, in the caller's transaction and RLS
  context.
* :func:`first_stored_with`: the first stored day whose breakdown names an
  account (asked before deleting it, so the job takes it out of every day).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import clock
from app.db.queue import enqueue
from app.domain.networth.models import NetWorthSnapshot
from app.domain.networth.schemas import SNAPSHOT_JOB_TYPE, HouseholdSnapshotPayload


def today() -> date:
    return clock.utcnow().date()


def request_recompute(db: Session, household_id: uuid.UUID, since: date) -> None:
    """Enqueue the snapshot job to rewrite the stored days from ``since``.

    In the caller's transaction and RLS context; no dedupe key (a key is
    held for good, and a later write may need the same days again).
    """
    enqueue(
        db,
        SNAPSHOT_JOB_TYPE,
        HouseholdSnapshotPayload(as_of=today(), since=since),
        household_id=household_id,
    )


def first_stored_with(db: Session, household_id: uuid.UUID, account_id: uuid.UUID) -> date | None:
    """The first stored day whose breakdown lists ``account_id``."""
    named = NetWorthSnapshot.breakdown["accounts"].contains([{"account_id": str(account_id)}])
    return db.execute(
        select(func.min(NetWorthSnapshot.as_of_date)).where(
            NetWorthSnapshot.household_id == household_id, named
        )
    ).scalar_one_or_none()

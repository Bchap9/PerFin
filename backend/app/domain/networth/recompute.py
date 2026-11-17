# SPDX-License-Identifier: AGPL-3.0-or-later
"""Asking for the stored net worth days to be rewritten (``networth.snapshot`` with ``since``).

Kept apart from ``app.domain.networth.service`` (which reads accounts,
reports and investments) so that every writer of a past day can import it
without an import cycle.

* :func:`request_recompute`: queue the household's snapshot job to rewrite
  the stored days from ``since``, in the caller's transaction and RLS
  context; one waiting job per household, from the earliest day asked for.
* :func:`first_stored_with`: the first stored day whose breakdown names an
  account (asked before deleting it, so the job takes it out of every day).
"""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import clock
from app.db.models import Job, JobState
from app.db.queue import enqueue
from app.domain.networth.models import NetWorthSnapshot
from app.domain.networth.schemas import SNAPSHOT_JOB_TYPE, HouseholdSnapshotPayload


def today() -> date:
    return clock.utcnow().date()


def request_recompute(db: Session, household_id: uuid.UUID, since: date) -> None:
    """Have the household's snapshot job rewrite the stored days from ``since``.

    In the caller's transaction and RLS context. Nothing for a day not
    before today: today is always computed live, and the next daily run
    rewrites the last stored day. Rewrites asked for while one of the
    household's waits (``queued``, no dedupe key: the daily keyed job is
    left alone) become one, from the earliest day: a job's payload cannot
    change (``perfin_job_immutable``), so those waiting are deleted and one
    is queued in their place, unless one already starts on or before
    ``since``. The waiting ones are locked (``FOR UPDATE SKIP LOCKED``), so
    none is claimed by a worker before this transaction commits; one a
    worker is claiming right now is skipped and left to run. No dedupe key
    (a key is held for good, and a later write may need the same days again).
    """
    on = today()
    if since >= on:
        return
    waiting = list(
        db.execute(
            select(Job)
            .where(
                Job.household_id == household_id,
                Job.type == SNAPSHOT_JOB_TYPE,
                Job.state == JobState.QUEUED,
                Job.dedupe_key.is_(None),
            )
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    earliest = since
    replaced: list[Job] = []
    for job in waiting:
        try:
            payload = HouseholdSnapshotPayload.model_validate(job.payload)
        except ValidationError:
            continue  # the worker dead-letters it
        if payload.as_of > on:
            continue
        if payload.since is not None and payload.since <= since and payload.as_of == on:
            return
        if payload.since is not None:
            earliest = min(earliest, payload.since)
        replaced.append(job)
    for job in replaced:
        db.delete(job)
    db.flush()
    enqueue(
        db,
        SNAPSHOT_JOB_TYPE,
        HouseholdSnapshotPayload(as_of=on, since=earliest),
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

# SPDX-License-Identifier: AGPL-3.0-or-later
"""``snapshot_net_worth`` and ``networth.snapshot`` (ARCHITECTURE.md "Jobs").

``snapshot_net_worth`` is an instance-level recurring job (daily, one per
slot however many workers run). It lists every household
(``perfin_household_ids()``) and enqueues ``networth.snapshot`` for each one
with the day (UTC) in the payload and the dedupe key
``networth:<household>:<day>``, so a household gets at most one snapshot run
per day even if the dispatcher runs twice.

``networth.snapshot`` runs in the household's RLS context and calls
:func:`app.domain.networth.service.snapshot_household`: it upserts that day's
``net_worth_snapshot`` and fills the days missing since the last one (the
backfill; at most ten years on a household's first run). Re-running it for
the same day rewrites the same row.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import clock
from app.core.logging import get_logger
from app.db.queue import enqueue
from app.db.rls import set_context
from app.domain.networth import service
from app.workers.registry import JobContext, PermanentJobError, job_handler
from app.workers.scheduler import recurring

log = get_logger(__name__)

DISPATCH_JOB_TYPE: Final = "snapshot_net_worth"
SNAPSHOT_JOB_TYPE: Final = "networth.snapshot"
SNAPSHOT_INTERVAL: Final = timedelta(days=1)
SNAPSHOT_TIMEOUT_SECONDS: Final = 900.0


class SnapshotNetWorthPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HouseholdSnapshotPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: date


def household_ids(db: Session) -> list[uuid.UUID]:
    rows = db.execute(text("SELECT * FROM perfin_household_ids()")).scalars()
    return [r if isinstance(r, uuid.UUID) else uuid.UUID(str(r)) for r in rows]


def dedupe_key(household_id: uuid.UUID, day: date) -> str:
    return f"networth:{household_id}:{day.isoformat()}"


@job_handler(DISPATCH_JOB_TYPE, payload=SnapshotNetWorthPayload, timeout=300.0, max_attempts=3)
def snapshot_net_worth(ctx: JobContext, payload: SnapshotNetWorthPayload) -> None:
    if ctx.household_id is not None:
        raise PermanentJobError("snapshot_net_worth is an instance-level job")
    day = clock.utcnow().date()
    for household_id in household_ids(ctx.session):
        ctx.check_cancelled()
        set_context(ctx.session, household_id)
        enqueue(
            ctx.session,
            SNAPSHOT_JOB_TYPE,
            HouseholdSnapshotPayload(as_of=day),
            household_id=household_id,
            dedupe_key=dedupe_key(household_id, day),
        )
    set_context(ctx.session, None)


@job_handler(
    SNAPSHOT_JOB_TYPE,
    payload=HouseholdSnapshotPayload,
    timeout=SNAPSHOT_TIMEOUT_SECONDS,
    max_attempts=3,
)
def snapshot_household(ctx: JobContext, payload: HouseholdSnapshotPayload) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("networth.snapshot needs a household")
    written = service.snapshot_household(ctx.session, ctx.household_id, payload.as_of)
    log.info("net worth snapshot", extra={"days": written})


recurring(DISPATCH_JOB_TYPE, SNAPSHOT_INTERVAL, SnapshotNetWorthPayload())

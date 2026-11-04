# SPDX-License-Identifier: AGPL-3.0-or-later
"""``detect_recurring`` and ``recurring.detect`` (ARCHITECTURE.md "Jobs").

``detect_recurring`` is an instance-level recurring job (daily). It lists
every household (``perfin_household_ids()``) and enqueues ``recurring.detect``
for each one with the dedupe key ``recurring:<household>:<day>`` (at most one
run per household and day).

``recurring.detect`` runs in the household's RLS context and calls
:func:`app.domain.recurring.service.detect_household`: detection over every
account of the household, writing ``origin = detected`` schedules (manual
schedules are never touched; a detected one a person switched off stays
off). Running it twice on the same ledger changes nothing.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict

from app.core import clock
from app.core.logging import get_logger
from app.db.queue import enqueue
from app.db.rls import set_context
from app.domain.recurring import service
from app.workers.handlers.networth import household_ids
from app.workers.registry import JobContext, PermanentJobError, job_handler
from app.workers.scheduler import recurring

log = get_logger(__name__)

DISPATCH_JOB_TYPE: Final = "detect_recurring"
DETECT_JOB_TYPE: Final = "recurring.detect"
DETECT_INTERVAL: Final = timedelta(days=1)
DETECT_TIMEOUT_SECONDS: Final = 600.0


class DetectRecurringPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HouseholdDetectPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: date


@job_handler(DISPATCH_JOB_TYPE, payload=DetectRecurringPayload, timeout=300.0, max_attempts=3)
def detect_recurring(ctx: JobContext, payload: DetectRecurringPayload) -> None:
    if ctx.household_id is not None:
        raise PermanentJobError("detect_recurring is an instance-level job")
    day = clock.utcnow().date()
    for household_id in household_ids(ctx.session):
        ctx.check_cancelled()
        set_context(ctx.session, household_id)
        enqueue(
            ctx.session,
            DETECT_JOB_TYPE,
            HouseholdDetectPayload(as_of=day),
            household_id=household_id,
            dedupe_key=f"recurring:{household_id}:{day.isoformat()}",
        )
    set_context(ctx.session, None)


@job_handler(
    DETECT_JOB_TYPE, payload=HouseholdDetectPayload, timeout=DETECT_TIMEOUT_SECONDS, max_attempts=3
)
def detect_household(ctx: JobContext, payload: HouseholdDetectPayload) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("recurring.detect needs a household")
    counts = service.detect_household(ctx.session, ctx.household_id, payload.as_of)
    # Prefixed: a bare ``created`` collides with a LogRecord attribute (KeyError at INFO).
    log.info("recurring detection", extra={f"detect_{k}": v for k, v in counts.__dict__.items()})


recurring(DISPATCH_JOB_TYPE, DETECT_INTERVAL, DetectRecurringPayload())

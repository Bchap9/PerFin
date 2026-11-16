# SPDX-License-Identifier: AGPL-3.0-or-later
"""``categorise.dispatch`` and ``categorise.refresh`` (design note, section 2).

``categorise.dispatch`` is an instance-level recurring job (hourly). It lists
every household (``perfin_household_ids()``) and, for each one whose rows
were last evaluated with an older clean-up, dictionary or set of default
categories than this version ships (or whose memory was never seeded),
enqueues ``categorise.refresh`` with a dedupe key of the household and the
versions it is to be brought to: at most one job per household and release,
however many hours pass before it runs.

``categorise.refresh`` runs in the household's RLS context and calls
:func:`app.domain.categorisation.service.refresh`: only rows nobody decided
are looked at again, and no ``updated_at`` moves. It checks for cancellation
between batches; nothing is kept of a cancelled run (one transaction), and a
second run after a finished one finds the household up to date. A person
asking (``POST /categorisation/refresh``) or a reverted rule queues the same
job with ``force``.

Only counts are logged: never a descriptor, a merchant key or a name.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict

from app.core.logging import get_logger
from app.db.queue import enqueue
from app.db.rls import set_context
from app.domain.categorisation import service
from app.domain.categorisation.service import REFRESH_JOB_TYPE, RefreshJob
from app.workers.handlers.networth import household_ids
from app.workers.registry import JobContext, PermanentJobError, job_handler
from app.workers.scheduler import recurring

log = get_logger(__name__)

DISPATCH_JOB_TYPE: Final = "categorise.dispatch"
DISPATCH_INTERVAL: Final = timedelta(hours=1)
REFRESH_TIMEOUT_SECONDS: Final = 900.0

__all__ = [
    "DISPATCH_INTERVAL",
    "DISPATCH_JOB_TYPE",
    "REFRESH_JOB_TYPE",
    "REFRESH_TIMEOUT_SECONDS",
    "DispatchPayload",
    "dispatch",
    "refresh",
]


class DispatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


@job_handler(DISPATCH_JOB_TYPE, payload=DispatchPayload, timeout=300.0, max_attempts=3)
def dispatch(ctx: JobContext, payload: DispatchPayload) -> None:
    del payload
    if ctx.household_id is not None:
        raise PermanentJobError("categorise.dispatch is an instance-level job")
    versions = service.current_versions()
    target = ".".join(str(versions[name]) for name in ("cleaner", "dictionary", "taxonomy"))
    queued = 0
    for household_id in household_ids(ctx.session):
        ctx.check_cancelled()
        set_context(ctx.session, household_id)
        if not service.is_behind(ctx.session, household_id):
            continue
        enqueue(
            ctx.session,
            REFRESH_JOB_TYPE,
            RefreshJob(),
            household_id=household_id,
            dedupe_key=f"categorise:{household_id}:{target}",
        )
        queued += 1
    set_context(ctx.session, None)
    log.info("categorisation refresh dispatched", extra={"households_behind": queued})


@job_handler(REFRESH_JOB_TYPE, payload=RefreshJob, timeout=REFRESH_TIMEOUT_SECONDS, max_attempts=3)
def refresh(ctx: JobContext, payload: RefreshJob) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("categorise.refresh needs a household")
    result = service.refresh(
        ctx.session,
        ctx.household_id,
        force=payload.force,
        check_cancelled=ctx.check_cancelled,
        user_id=payload.requested_by,
    )
    log.info(
        "categorisation refreshed",
        extra={
            "refresh_ran": result.ran,
            "rows_scanned": result.summary.scanned,
            "rows_keyed": result.summary.keyed,
            "rows_changed": result.summary.changed,
            "categories_added": result.categories_added,
            "memory_seeded": result.seeded,
        },
    )


recurring(DISPATCH_JOB_TYPE, DISPATCH_INTERVAL, DispatchPayload())

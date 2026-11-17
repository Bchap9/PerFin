# SPDX-License-Identifier: AGPL-3.0-or-later
"""``portability.export`` / ``.import`` / ``.expire`` / ``.sweep`` (docs/PORTABILITY.md).

Queued by ``POST /export`` and ``POST /import``
(``app.domain.portability.service``); ``portability.expire`` is queued when
an export becomes ready, to run at its expiry. Each runs in the job's
household context and its writes commit with the job. Deterministic
failures (an archive that does not validate, a document original that
cannot be read back) are recorded on the archive row (``failed`` plus an
error slug) and the job succeeds; database or storage errors raise and are
retried. ``portability.sweep`` (every 15 minutes, instance level) fails the
archives whose job was dead-lettered or pruned while they were still
pending (``service.fail_orphaned``), so their upload and sealed passphrase
key are shredded and the household can start another export or import even
if nobody asks again.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from functools import lru_cache
from typing import Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.settings import get_settings
from app.db.rls import set_context
from app.domain.portability import service
from app.domain.portability.schemas import (
    EXPIRE_JOB_TYPE,
    EXPORT_JOB_TYPE,
    IMPORT_JOB_TYPE,
    ArchiveJob,
)
from app.workers.registry import JobContext, PermanentJobError, job_handler
from app.workers.scheduler import recurring

log = get_logger(__name__)

#: Large households with many documents: room for reading and encrypting them all.
TIMEOUT_SECONDS = 3600.0


@lru_cache(maxsize=1)
def default_deps() -> service.PortabilityDeps:
    return service.PortabilityDeps.from_settings(get_settings())


@job_handler(EXPORT_JOB_TYPE, payload=ArchiveJob, timeout=TIMEOUT_SECONDS, max_attempts=3)
def export_household(ctx: JobContext, payload: ArchiveJob) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("portability.export needs a household")
    ctx.check_cancelled()
    state = service.run_export_job(
        ctx.session,
        ctx.household_id,
        payload.archive_id,
        default_deps(),
        check_cancelled=ctx.check_cancelled,
    )
    log.info("export finished", extra={"archive_id": str(payload.archive_id), "state": str(state)})


@job_handler(IMPORT_JOB_TYPE, payload=ArchiveJob, timeout=TIMEOUT_SECONDS, max_attempts=3)
def import_household(ctx: JobContext, payload: ArchiveJob) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("portability.import needs a household")
    ctx.check_cancelled()
    state = service.run_import_job(
        ctx.session,
        ctx.household_id,
        payload.archive_id,
        default_deps(),
        check_cancelled=ctx.check_cancelled,
    )
    log.info("import finished", extra={"archive_id": str(payload.archive_id), "state": str(state)})


@job_handler(EXPIRE_JOB_TYPE, payload=ArchiveJob, timeout=300.0, max_attempts=5)
def expire_export(ctx: JobContext, payload: ArchiveJob) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("portability.expire needs a household")
    state = service.run_expire_job(
        ctx.session, ctx.household_id, payload.archive_id, default_deps()
    )
    log.info("export expiry", extra={"archive_id": str(payload.archive_id), "state": str(state)})


SWEEP_INTERVAL: Final = timedelta(minutes=15)


class SweepPayload(BaseModel):
    """No parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _household_ids(db: Session) -> list[uuid.UUID]:
    rows = db.execute(text("SELECT * FROM perfin_household_ids()")).scalars()
    return [r if isinstance(r, uuid.UUID) else uuid.UUID(str(r)) for r in rows]


def sweep_orphaned_archives(db: Session, deps: service.PortabilityDeps) -> int:
    """Fail every household's orphaned pending archives (module doc); the caller commits."""
    failed = 0
    try:
        for household_id in _household_ids(db):
            set_context(db, household_id)
            failed += service.fail_orphaned(db, household_id, deps.store)
    finally:
        set_context(db, None)
    return failed


@job_handler("portability.sweep", payload=SweepPayload, timeout=300.0, max_attempts=1)
def sweep_archives(ctx: JobContext, payload: SweepPayload) -> None:
    ctx.check_cancelled()
    failed = sweep_orphaned_archives(ctx.session, default_deps())
    if failed:
        log.warning("failed orphaned archives", extra={"count": failed})


recurring("portability.sweep", SWEEP_INTERVAL, SweepPayload(), name="portability.sweep")

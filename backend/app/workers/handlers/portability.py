# SPDX-License-Identifier: AGPL-3.0-or-later
"""``portability.export`` / ``portability.import`` / ``portability.expire`` (docs/PORTABILITY.md).

Queued by ``POST /export`` and ``POST /import``
(``app.domain.portability.service``); ``portability.expire`` is queued when
an export becomes ready, to run at its expiry. Each runs in the job's
household context and its writes commit with the job. Deterministic
failures (an archive that does not validate, a document original that
cannot be read back) are recorded on the archive row (``failed`` plus an
error slug) and the job succeeds; database or storage errors raise and are
retried.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.logging import get_logger
from app.core.settings import get_settings
from app.domain.portability import service
from app.domain.portability.schemas import (
    EXPIRE_JOB_TYPE,
    EXPORT_JOB_TYPE,
    IMPORT_JOB_TYPE,
    ArchiveJob,
)
from app.workers.registry import JobContext, PermanentJobError, job_handler

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

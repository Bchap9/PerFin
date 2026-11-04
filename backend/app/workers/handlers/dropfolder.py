# SPDX-License-Identifier: AGPL-3.0-or-later
"""``watch_dropfolder`` and ``dropfolder.finish`` (ARCHITECTURE.md "perfin-worker").

``watch_dropfolder`` is an instance-level recurring job (every minute, one
per slot however many workers run). Each run is long-running within bounds:
it watches the inbox (inotify where available, polling otherwise) for up to
``PERFIN_DROPFOLDER_WATCH_SECONDS`` while files settle, takes what is
complete and returns, so its writes commit promptly
(:func:`app.ingestion.intake.dropfolder.watch`). With
``PERFIN_DROPFOLDER_ENABLED=false`` (the default) it does nothing.

The run switches the RLS context to the drop folder's household
(:func:`app.domain.dropfolder.service.target_household`) for its writes.
Originals are moved or deleted only after the run has committed: the run
enqueues ``dropfolder.finish`` for the household in its own transaction, and
the next run also finishes whatever is still pending.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict

from app.core.crypto import Keyring
from app.core.logging import get_logger
from app.core.settings import Settings, get_settings
from app.db.queue import enqueue
from app.db.rls import set_context
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.dropfolder.service import target_household
from app.ingestion.intake import dropfolder
from app.workers.registry import JobContext, PermanentJobError, job_handler
from app.workers.scheduler import recurring

log = get_logger(__name__)

WATCH_JOB_TYPE: Final = "watch_dropfolder"
FINISH_JOB_TYPE: Final = "dropfolder.finish"
#: Watch window (<= 50 s) plus copying up to the per-run byte cap.
WATCH_TIMEOUT_SECONDS: Final = 900.0
WATCH_INTERVAL: Final = timedelta(seconds=60)


class WatchDropfolderPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FinishDropfolderPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


def intake_deps(settings: Settings) -> dropfolder.IntakeDeps:
    return dropfolder.IntakeDeps(
        store=LocalFSBlobStore.from_settings(settings), keyring=Keyring.from_settings(settings)
    )


@job_handler(
    WATCH_JOB_TYPE,
    payload=WatchDropfolderPayload,
    timeout=WATCH_TIMEOUT_SECONDS,
    max_attempts=1,
)
def watch_dropfolder(ctx: JobContext, payload: WatchDropfolderPayload) -> None:
    settings = get_settings()
    if not settings.dropfolder_enabled:
        return
    if ctx.household_id is not None:
        raise PermanentJobError("watch_dropfolder is an instance-level job")
    household_id = target_household(ctx.session, settings)
    if household_id is None:
        return
    set_context(ctx.session, household_id)
    stats = dropfolder.watch(
        ctx.session,
        household_id,
        dropfolder.DropFolderConfig.from_settings(settings),
        intake_deps(settings),
        cancelled=ctx.cancelled,
    )
    if stats.pending_after:
        enqueue(ctx.session, FINISH_JOB_TYPE, FinishDropfolderPayload(), household_id=household_id)


@job_handler(FINISH_JOB_TYPE, payload=FinishDropfolderPayload, timeout=300.0, max_attempts=3)
def finish_dropfolder(ctx: JobContext, payload: FinishDropfolderPayload) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("dropfolder.finish needs a household")
    settings = get_settings()
    if not dropfolder.try_lock(ctx.session):
        return  # a run is busy; it finishes pending entries itself
    results = dropfolder.finish_pending(
        ctx.session, ctx.household_id, dropfolder.DropFolderConfig.from_settings(settings)
    )
    log.info("drop folder after-actions", extra={"results": dict(results)})


recurring(WATCH_JOB_TYPE, WATCH_INTERVAL, WatchDropfolderPayload())

# SPDX-License-Identifier: AGPL-3.0-or-later
"""``poll_mailboxes`` and ``poll_mailbox`` (ARCHITECTURE.md "Email intake").

``poll_mailboxes`` is an instance-level recurring job (every
:data:`POLL_INTERVAL`, one per slot however many workers run). With
``PERFIN_EMAIL_INTAKE_ENABLED`` on it looks at every household
(``perfin_household_ids()``) and enqueues ``poll_mailbox`` for each one whose
mailbox is enabled (dedupe key per household and slot).

``poll_mailbox`` runs in the household's RLS context. It takes a
transaction-scoped advisory lock for the household (a second run at the
same time returns at once), decrypts the mailbox credentials for this run
only, connects over IMAPS and runs :func:`app.ingestion.intake.email.poll`.
The outcome (``ok`` or a short failure code) and the run's counts are stored
on the household's ``mail_intake`` row, so the settings page shows them. A
run that recorded messages enqueues one follow-up run, which moves them out
of the inbox once this run has committed (the two-phase move in
``app.ingestion.intake.email``). Connection failures are not retried by the
queue (``max_attempts=1``): the next slot tries again.
"""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.crypto import Keyring
from app.core.logging import get_logger
from app.core.settings import Settings, get_settings
from app.db.queue import enqueue
from app.db.rls import set_context
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.intake_settings import service
from app.ingestion.intake import email
from app.workers.registry import JobContext, PermanentJobError, job_handler
from app.workers.scheduler import recurring

log = get_logger(__name__)

DISPATCH_JOB_TYPE: Final = "poll_mailboxes"
POLL_JOB_TYPE: Final = "poll_mailbox"
POLL_INTERVAL: Final = timedelta(minutes=5)
POLL_TIMEOUT_SECONDS: Final = 600.0
_LOCK: Final = text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 9931))")


class PollMailboxesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PollMailboxPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


def intake_deps(settings: Settings) -> email.IntakeDeps:
    return email.IntakeDeps(
        store=LocalFSBlobStore.from_settings(settings), keyring=Keyring.from_settings(settings)
    )


def connect_options(settings: Settings) -> email.ConnectOptions:
    return email.ConnectOptions.from_settings(settings)


def household_ids(db: Session) -> list[uuid.UUID]:
    rows = db.execute(text("SELECT * FROM perfin_household_ids()")).scalars()
    return [r if isinstance(r, uuid.UUID) else uuid.UUID(str(r)) for r in rows]


def try_lock(db: Session, household_id: uuid.UUID) -> bool:
    return bool(db.execute(_LOCK, {"k": f"mailbox:{household_id}"}).scalar_one())


def poll_household(
    db: Session,
    household_id: uuid.UUID,
    *,
    deps: email.IntakeDeps,
    options: email.ConnectOptions,
    limits: email.RunLimits,
    cancelled: threading.Event | None = None,
) -> email.PollStats | None:
    """One poll of the household's mailbox (module doc); ``None`` when there is nothing to do."""
    if not try_lock(db, household_id):
        return None
    row = service.get_row(db, household_id, lock=True)
    if row is None or not row.enabled:
        return None
    config = service.mailbox_config(row, deps.keyring)
    if config is None:
        service.record_poll(row, "not_configured")
        return None
    policy = service.acceptance_policy(db, row, deps.keyring)
    try:
        with email.connect(config, options) as mailbox:
            stats = email.poll(
                db, household_id, mailbox, config, policy, limits, deps, cancelled=cancelled
            )
    except email.MailboxError as exc:
        log.warning(
            "mailbox poll failed",
            extra={"household_id": str(household_id), "outcome": exc.outcome},
        )
        service.record_poll(row, exc.outcome)
        return None
    service.record_poll(row, stats.error or "ok", stats.counts())
    return stats


@job_handler(DISPATCH_JOB_TYPE, payload=PollMailboxesPayload, timeout=120.0, max_attempts=1)
def poll_mailboxes(ctx: JobContext, payload: PollMailboxesPayload) -> None:
    settings = get_settings()
    if not settings.email_intake_enabled:
        return
    if ctx.household_id is not None:
        raise PermanentJobError("poll_mailboxes is an instance-level job")
    for household_id in household_ids(ctx.session):
        set_context(ctx.session, household_id)
        row = service.get_row(ctx.session, household_id)
        if row is not None and row.enabled:
            enqueue(
                ctx.session,
                POLL_JOB_TYPE,
                PollMailboxPayload(),
                household_id=household_id,
                dedupe_key=f"mailbox:{household_id}:{ctx.job_id}",
            )
    set_context(ctx.session, None)


@job_handler(
    POLL_JOB_TYPE, payload=PollMailboxPayload, timeout=POLL_TIMEOUT_SECONDS, max_attempts=1
)
def poll_mailbox(ctx: JobContext, payload: PollMailboxPayload) -> None:
    settings = get_settings()
    if not settings.email_intake_enabled:
        return
    if ctx.household_id is None:
        raise PermanentJobError("poll_mailbox needs a household")
    stats = poll_household(
        ctx.session,
        ctx.household_id,
        deps=intake_deps(settings),
        options=connect_options(settings),
        limits=email.RunLimits.from_settings(settings),
        cancelled=ctx.cancelled,
    )
    if stats is not None and stats.recorded:
        # Moves the recorded messages once this transaction has committed.
        enqueue(ctx.session, POLL_JOB_TYPE, PollMailboxPayload(), household_id=ctx.household_id)


recurring(DISPATCH_JOB_TYPE, POLL_INTERVAL, PollMailboxesPayload())

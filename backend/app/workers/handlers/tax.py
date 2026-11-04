# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tax vault jobs: accountant export and year-end reminders (``app.domain.tax``).

* ``tax.export`` builds a requested accountant export
  (:func:`app.domain.tax.export.run_export_job`); ``tax.export_expire``
  crypto-shreds it once ``PERFIN_EXPORT_TTL_MINUTES`` passed.
* ``tax_reminders`` (daily, instance-level) queues ``tax.reminders`` for
  every household with the day in the payload and the dedupe key
  ``tax-reminders:<household>:<day>``; that job emits the reminders due that
  day (:func:`app.domain.tax.reminders.send_reminders`).

Settings are read through this module's :func:`get_settings` (tests patch it).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from app.core import clock
from app.core.logging import get_logger
from app.core.settings import get_settings
from app.db.queue import enqueue
from app.db.rls import set_context
from app.domain.tax import export, reminders
from app.domain.tax.schemas import (
    EXPORT_EXPIRE_JOB_TYPE,
    EXPORT_JOB_TYPE,
    REMINDER_DISPATCH_JOB_TYPE,
    REMINDER_JOB_TYPE,
    TaxExportJob,
    TaxReminderDispatch,
    TaxReminderJob,
)
from app.workers.handlers.networth import household_ids
from app.workers.registry import JobContext, PermanentJobError, job_handler
from app.workers.scheduler import recurring

log = get_logger(__name__)

REMINDER_INTERVAL: Final = timedelta(days=1)


def _deps() -> export.TaxExportDeps:
    return export.TaxExportDeps.from_settings(get_settings())


@job_handler(EXPORT_JOB_TYPE, payload=TaxExportJob, timeout=1800.0, max_attempts=3)
def tax_export(ctx: JobContext, payload: TaxExportJob) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("tax.export needs a household")
    state = export.run_export_job(
        ctx.session,
        ctx.household_id,
        payload.export_id,
        _deps(),
        check_cancelled=ctx.check_cancelled,
    )
    log.info("tax export", extra={"state": str(state) if state else None})


@job_handler(EXPORT_EXPIRE_JOB_TYPE, payload=TaxExportJob, timeout=120.0, max_attempts=5)
def tax_export_expire(ctx: JobContext, payload: TaxExportJob) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("tax.export_expire needs a household")
    export.run_expire_job(ctx.session, ctx.household_id, payload.export_id, _deps())


@job_handler(REMINDER_DISPATCH_JOB_TYPE, payload=TaxReminderDispatch, timeout=300.0)
def tax_reminders(ctx: JobContext, payload: TaxReminderDispatch) -> None:
    if ctx.household_id is not None:
        raise PermanentJobError("tax_reminders is an instance-level job")
    day = clock.utcnow().date()
    for household_id in household_ids(ctx.session):
        ctx.check_cancelled()
        set_context(ctx.session, household_id)
        enqueue(
            ctx.session,
            REMINDER_JOB_TYPE,
            TaxReminderJob(day=day),
            household_id=household_id,
            dedupe_key=f"tax-reminders:{household_id}:{day.isoformat()}",
        )
    set_context(ctx.session, None)


@job_handler(REMINDER_JOB_TYPE, payload=TaxReminderJob, timeout=120.0, max_attempts=3)
def tax_household_reminders(ctx: JobContext, payload: TaxReminderJob) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("tax.reminders needs a household")
    sent = reminders.send_reminders(
        ctx.session, ctx.household_id, payload.day, get_settings().tax_reminder_dates
    )
    log.info("tax reminders", extra={"count": len(sent)})


recurring(REMINDER_DISPATCH_JOB_TYPE, REMINDER_INTERVAL, TaxReminderDispatch())

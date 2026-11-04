# SPDX-License-Identifier: AGPL-3.0-or-later
"""``transactions.auto_pair``: run the transfer matcher after an import.

Enqueued by :func:`app.domain.transactions.transfers.request_auto_pair` (the
ingest worker does so for every document whose import went through). Runs in
the job's household context as the system, over every account of the
household; seeds are the document's transactions (or the accounts / dates
named in the payload). Rules: ``app.domain.transactions.transfers``.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.domain.transactions import transfers
from app.domain.transactions.schemas import AUTO_PAIR_JOB_TYPE, AutoPairJob
from app.workers.registry import JobContext, PermanentJobError, job_handler

log = get_logger(__name__)


@job_handler(AUTO_PAIR_JOB_TYPE, payload=AutoPairJob, timeout=300.0, max_attempts=3)
def auto_pair_transfers(ctx: JobContext, payload: AutoPairJob) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("transactions.auto_pair needs a household")
    ctx.check_cancelled()
    outcome = transfers.auto_pair(
        ctx.session,
        ctx.household_id,
        account_ids=payload.account_ids,
        date_from=payload.date_from,
        date_to=payload.date_to,
        source_document_id=payload.source_document_id,
        window_days=payload.window_days,
    )
    log.info(
        "transfers auto-paired",
        extra={"paired": len(outcome.pairs), "ambiguous": outcome.ambiguous},
    )

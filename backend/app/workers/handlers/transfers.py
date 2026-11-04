# SPDX-License-Identifier: AGPL-3.0-or-later
"""``transactions.auto_pair``: run the transfer matcher after an import.

Enqueued by :func:`app.domain.transactions.transfers.request_auto_pair` (the
ingest worker does so for every document whose import went through). Runs in
the job's household context on behalf of the document's uploader: only the
accounts the uploader may import into take part (all of them for an owner's
or admin's document, or one without an uploader; review finding P2-SR-02).
Seeds are the document's transactions (or the accounts / dates named in the
payload). Rules: ``app.domain.transactions.transfers``.
"""

from __future__ import annotations

from sqlalchemy import select

from app.core.logging import get_logger
from app.domain.documents.models import Document
from app.domain.transactions import transfers
from app.domain.transactions.schemas import AUTO_PAIR_JOB_TYPE, AutoPairJob
from app.ingestion.match.accounts import importable_account_ids
from app.workers.registry import JobContext, PermanentJobError, job_handler

log = get_logger(__name__)


@job_handler(AUTO_PAIR_JOB_TYPE, payload=AutoPairJob, timeout=300.0, max_attempts=3)
def auto_pair_transfers(ctx: JobContext, payload: AutoPairJob) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("transactions.auto_pair needs a household")
    ctx.check_cancelled()
    allowed = None
    if payload.source_document_id is not None:
        found = ctx.session.execute(
            select(Document.uploaded_by).where(
                Document.household_id == ctx.household_id,
                Document.id == payload.source_document_id,
            )
        ).first()
        if found is None:
            log.info("transfers auto-pair skipped: the document is gone")
            return
        allowed = importable_account_ids(ctx.session, ctx.household_id, found[0])
    outcome = transfers.auto_pair(
        ctx.session,
        ctx.household_id,
        account_ids=payload.account_ids,
        date_from=payload.date_from,
        date_to=payload.date_to,
        source_document_id=payload.source_document_id,
        allowed_account_ids=allowed,
        window_days=payload.window_days,
    )
    log.info(
        "transfers auto-paired",
        extra={"paired": len(outcome.pairs), "ambiguous": outcome.ambiguous},
    )

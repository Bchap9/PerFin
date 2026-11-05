# SPDX-License-Identifier: AGPL-3.0-or-later
"""``imports.apply_mapping``: re-run a document's import from its stored extraction.

Queued by ``POST /imports/{document_id}/apply-mapping``
(:func:`app.domain.imports.service.request_apply`) after a person saved a
column mapping or chose the account. Runs in the job's household context;
reads ``document.extraction`` (nothing is re-parsed, no sandbox needed),
imports with the chosen mapping/account, resolves the review items it
answered and, when transactions were written, queues the transfer matcher
seeded with the document's transactions and links receipts waiting for them
(``app.ingestion.match.receipts.rematch_receipts``).
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.core.settings import get_settings
from app.domain.imports import service
from app.domain.imports.schemas import APPLY_MAPPING_JOB_TYPE, ApplyMappingJob
from app.domain.transactions.transfers import request_auto_pair
from app.ingestion.match.receipts import rematch_receipts
from app.workers.registry import JobContext, PermanentJobError, job_handler

log = get_logger(__name__)


@job_handler(APPLY_MAPPING_JOB_TYPE, payload=ApplyMappingJob, timeout=900.0, max_attempts=3)
def apply_mapping(ctx: JobContext, payload: ApplyMappingJob) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("imports.apply_mapping needs a household")
    ctx.check_cancelled()
    outcome = service.run_apply_job(
        ctx.session, ctx.household_id, payload, job_id=ctx.job_id, attempt=ctx.attempt
    )
    if outcome is None:
        log.info("import re-run skipped", extra={"document_id": str(payload.document_id)})
        return
    if outcome.summary.get("imported"):
        request_auto_pair(ctx.session, ctx.household_id, source_document_id=payload.document_id)
        rematch_receipts(
            ctx.session,
            ctx.household_id,
            payload.document_id,
            threshold=get_settings().ingest_confidence_threshold,
        )
    log.info(
        "import re-run",
        extra={
            "document_id": str(payload.document_id),
            "status": str(outcome.status),
            "imported": outcome.summary.get("imported"),
        },
    )

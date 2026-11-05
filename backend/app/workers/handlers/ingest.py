# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ingest_document``: run the ingestion pipeline for one document.

Enqueued by :func:`app.domain.documents.service.record_received` in the same
transaction as the document row. The handler runs in the job's household
context; the pipeline's writes and the job's completion commit together.
Deterministic failures (sandbox limits, malformed files) are recorded on the
document (``failed`` plus a review item) and the job succeeds; database or
storage errors raise and are retried. The handler timeout leaves room for
the sandbox's own wall-clock limit. The structured importer
(``pipeline.structured_import``: mappings, dedupe, rules, snapshots) runs in
the same transaction. A document whose import went through
(``processed`` or ``needs_review``) also enqueues the transfer matcher
(``transactions.auto_pair``, seeded with the document's transactions) and
links receipts waiting in review to the transactions it brought
(``app.ingestion.match.receipts.rematch_receipts``).
"""

from __future__ import annotations

from functools import lru_cache

from app.core.crypto import Keyring
from app.core.logging import get_logger
from app.core.settings import get_settings
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import DocumentStatus
from app.domain.documents.schemas import INGEST_JOB_TYPE, IngestDocumentPayload
from app.domain.transactions.transfers import request_auto_pair
from app.ingestion import pipeline
from app.ingestion.match.receipts import rematch_receipts
from app.ingestion.sandbox.runner import SandboxConfig
from app.integrations.antivirus import scanner_from_settings
from app.workers.registry import JobContext, PermanentJobError, job_handler

log = get_logger(__name__)

#: Above the longest sandbox timeout (PERFIN_EXTRACT_TIMEOUT_SECONDS <= 600) plus I/O.
TIMEOUT_SECONDS = 900.0


@lru_cache(maxsize=1)
def default_deps() -> pipeline.PipelineDeps:
    settings = get_settings()
    return pipeline.PipelineDeps(
        store=LocalFSBlobStore.from_settings(settings),
        keyring=Keyring.from_settings(settings),
        sandbox=SandboxConfig.from_settings(settings),
        antivirus=settings.antivirus,
        scanner=scanner_from_settings(settings),
        antivirus_fail_mode=settings.antivirus_fail_mode,
        importer=pipeline.structured_import,
        child_max_bytes=settings.upload_max_bytes,
        confidence_threshold=settings.ingest_confidence_threshold,
    )


@job_handler(
    INGEST_JOB_TYPE, payload=IngestDocumentPayload, timeout=TIMEOUT_SECONDS, max_attempts=3
)
def ingest_document(ctx: JobContext, payload: IngestDocumentPayload) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("ingest_document needs a household")
    ctx.check_cancelled()
    status = pipeline.ingest_document(
        ctx.session,
        ctx.household_id,
        payload.document_id,
        default_deps(),
        attempt=ctx.attempt,
        # A scan that cannot run is retried by the queue until the last attempt.
        final_attempt=ctx.attempt >= ctx.max_attempts,
    )
    if status in (DocumentStatus.PROCESSED, DocumentStatus.NEEDS_REVIEW):
        request_auto_pair(ctx.session, ctx.household_id, source_document_id=payload.document_id)
        # Receipts that arrived before this statement may now find their transaction.
        rematch_receipts(
            ctx.session,
            ctx.household_id,
            payload.document_id,
            threshold=get_settings().ingest_confidence_threshold,
        )
    log.info(
        "document ingested",
        extra={"document_id": str(payload.document_id), "status": str(status)},
    )

# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ingest_document``: run the ingestion pipeline for one document.

Enqueued by :func:`app.domain.documents.service.record_received` in the same
transaction as the document row. The handler runs in the job's household
context; the pipeline's writes and the job's completion commit together.
Deterministic failures (sandbox limits, malformed files) are recorded on the
document (``failed`` plus a review item) and the job succeeds; database or
storage errors raise and are retried. The handler timeout leaves room for
the sandbox's own wall-clock limit.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.crypto import Keyring
from app.core.logging import get_logger
from app.core.settings import get_settings
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.schemas import INGEST_JOB_TYPE, IngestDocumentPayload
from app.ingestion import pipeline
from app.ingestion.sandbox.runner import SandboxConfig
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
    )


@job_handler(
    INGEST_JOB_TYPE, payload=IngestDocumentPayload, timeout=TIMEOUT_SECONDS, max_attempts=3
)
def ingest_document(ctx: JobContext, payload: IngestDocumentPayload) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("ingest_document needs a household")
    ctx.check_cancelled()
    status = pipeline.ingest_document(
        ctx.session, ctx.household_id, payload.document_id, default_deps(), attempt=ctx.attempt
    )
    log.info(
        "document ingested",
        extra={"document_id": str(payload.document_id), "status": str(status)},
    )

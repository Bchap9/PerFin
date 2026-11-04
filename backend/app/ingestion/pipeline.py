# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ingestion pipeline (INGESTION.md "Pipeline stages", ARCHITECTURE.md).

``received -> scanning -> extracting -> classifying -> parsing -> processed``
with the exits ``rejected`` (scan), ``failed`` (extraction) and
``needs_review``. :func:`ingest_document` runs every stage for one document
inside the worker job's transaction (``app.workers.handlers.ingest``), so a
crash or a retryable error leaves the document ``received`` for the next
attempt; each stage run is recorded as an ``ingest_job`` row.

Phase 2 routes only **structured** formats (CSV, TSV, OFX, QFX, QIF):

* **scan**: antivirus is Phase 3 (``PERFIN_ANTIVIRUS=clamav`` is recorded as
  "not available" in the stage log; nothing is rejected).
* **extract**: the blob is decrypted as a stream and fed to the sandboxed
  child (``app.ingestion.sandbox.runner``); the validated result is kept in
  ``document.extraction``. A sandbox failure (limits, crash, bad output) or an
  ``ok=false`` result makes the document ``failed`` with an ``unparsed``
  review item carrying the error code; a document of any other type goes to
  ``needs_review`` with an ``unparsed`` item (``format_not_supported``) until
  its extractor lands.
* **classify**: kind ``statement``; period from the statements; the account
  from the statement's last four digits (and OFX institution) when exactly
  one account of the household matches among those the uploader may edit
  (``app.ingestion.match.accounts``; the document has none yet).
* **parse/match/commit**: delegated to the structured importer in
  ``PipelineDeps.importer`` or registered with
  :func:`register_structured_importer`. The worker installs
  :func:`structured_import` (``app.domain.imports.service``: column mappings,
  ``import_hash`` dedupe, rules, balance snapshots, review items). Without
  one, the document waits in ``needs_review`` (``unparsed``/``import_pending``).
* Accepted documents (``processed`` or ``needs_review`` after a successful
  extraction) move from quarantine to the permanent area
  (:func:`app.domain.documents.service.promote_blob`); failed ones stay in
  quarantine.

Every outcome is audited (``document.ingest``) and emits ``document.updated``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.core import clock
from app.core.audit import audit
from app.core.crypto import DecryptionError, Keyring
from app.core.events import emit
from app.core.logging import get_logger, scrub_text
from app.domain.documents import repo
from app.domain.documents.blobstore import BlobStore, BlobStoreError
from app.domain.documents.models import (
    TERMINAL_STATUSES,
    Document,
    DocumentKind,
    DocumentStatus,
    IngestJob,
    IngestStage,
    ReviewKind,
)
from app.domain.documents.service import open_plaintext, promote_blob
from app.domain.imports.service import import_extraction
from app.ingestion.intake.sniff import FileType
from app.ingestion.match.accounts import importable_account_ids, match_account
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult
from app.ingestion.sandbox.runner import SandboxConfig, SandboxError, run_extraction

log = get_logger(__name__)

STDERR_LOG_CHARS = 500


class Extract(Protocol):
    def __call__(
        self,
        kind: str,
        data: bytes | Iterable[bytes],
        *,
        config: SandboxConfig | None = None,
        options: ExtractOptions | None = None,
    ) -> ExtractResult: ...


@dataclass(frozen=True)
class ImportOutcome:
    """What the structured importer did with an extracted document."""

    status: DocumentStatus
    summary: dict[str, Any] = field(default_factory=dict)


StructuredImporter = Callable[[Session, Document, ExtractResult], ImportOutcome]
_IMPORTER: list[StructuredImporter] = []


def register_structured_importer(importer: StructuredImporter) -> None:
    """Install the parse/match/commit step for structured formats (one per process)."""
    _IMPORTER[:] = [importer]


def structured_importer() -> StructuredImporter | None:
    return _IMPORTER[0] if _IMPORTER else None


@dataclass(frozen=True)
class PipelineDeps:
    store: BlobStore
    keyring: Keyring
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    options: ExtractOptions = field(default_factory=ExtractOptions)
    antivirus: str = "none"
    extract: Extract = run_extraction
    importer: StructuredImporter | None = None


class _Stage:
    """Context for one stage run: an ``ingest_job`` row with timing and a log."""

    def __init__(self, db: Session, document: Document, stage: IngestStage, attempt: int) -> None:
        self.row = IngestJob(
            household_id=document.household_id,
            document_id=document.id,
            stage=stage,
            attempt=max(1, attempt),
            started_at=clock.utcnow(),
            log={},
        )
        self._db = db
        db.add(self.row)

    def finish(self, *, error: str | None = None, **facts: Any) -> None:
        self.row.finished_at = clock.utcnow()
        self.row.error = error[:500] if error else None
        self.row.log = {**self.row.log, **facts}
        self._db.flush()


def _set_status(document: Document, status: DocumentStatus) -> None:
    document.status = status


def _finish(
    db: Session,
    document: Document,
    status: DocumentStatus,
    *,
    error: str | None = None,
    facts: dict[str, Any] | None = None,
) -> DocumentStatus:
    document.status = status
    document.error = error[:500] if error else None
    db.flush()
    audit(
        db,
        action="document.ingest",
        target_type="document",
        target_id=document.id,
        after={"status": str(status), "error": error, **(facts or {})},
        household_id=document.household_id,
    )
    emit(
        db,
        document.household_id,
        "document.updated",
        {"id": str(document.id), "status": str(status)},
    )
    return status


def _review(db: Session, document: Document, kind: ReviewKind, payload: dict[str, Any]) -> None:
    repo.add_review_item(db, document.household_id, kind, payload, document_id=document.id)


def _classify(db: Session, document: Document, result: ExtractResult) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    document.kind = DocumentKind.STATEMENT
    starts = [s.period_start for s in result.statements if s.period_start]
    ends = [s.period_end for s in result.statements if s.period_end]
    if starts and ends and min(starts) <= max(ends):
        document.period_start, document.period_end = min(starts), max(ends)
    if document.account_id is None:
        last4s = {s.account.last4 for s in result.statements if s.account.last4}
        if len(last4s) == 1:
            institutions = {s.account.institution for s in result.statements}
            match = match_account(
                db,
                document.household_id,
                last4=next(iter(last4s)),
                institution=next(iter(institutions)) if len(institutions) == 1 else None,
                allowed=importable_account_ids(db, document.household_id, document.uploaded_by),
            )
            facts["account_candidates"] = max(len(match.candidates), 1 if match.account_id else 0)
            if match.account_id is not None:
                document.account_id = match.account_id
    return facts


def structured_import(db: Session, document: Document, result: ExtractResult) -> ImportOutcome:
    """The Phase 2 structured importer (:func:`app.domain.imports.service.import_extraction`)."""
    outcome = import_extraction(db, document, result)
    return ImportOutcome(outcome.status, outcome.summary)


def ingest_document(
    db: Session,
    household_id: uuid.UUID,
    document_id: uuid.UUID,
    deps: PipelineDeps,
    *,
    attempt: int = 1,
) -> DocumentStatus | None:
    """Run the pipeline for one document; return its final status (None: it is gone)."""
    document = repo.get_document(db, household_id, document_id, lock=True)
    if document is None:
        log.info("ingest: document no longer exists", extra={"document_id": str(document_id)})
        return None
    if document.status in TERMINAL_STATUSES:
        return document.status
    blob = repo.get_blob(db, household_id, document.blob_id)
    if blob is None:  # pragma: no cover - the foreign key forbids it
        return _finish(db, document, DocumentStatus.FAILED, error="blob missing")

    # ---- scan ---------------------------------------------------------------
    _set_status(document, DocumentStatus.SCANNING)
    stage = _Stage(db, document, IngestStage.SCAN, attempt)
    stage.finish(antivirus=deps.antivirus, scanned=False)

    try:
        file_type = FileType(document.mime_detected)
    except ValueError:
        file_type = None
    kind = file_type.extract_kind if file_type is not None else None
    if kind is None:
        _review(
            db,
            document,
            ReviewKind.UNPARSED,
            {"reason": "format_not_supported", "mime": document.mime_detected},
        )
        promote_blob(db, deps.store, blob)
        return _finish(db, document, DocumentStatus.NEEDS_REVIEW, facts={"routed": "unsupported"})

    # ---- extract ------------------------------------------------------------
    _set_status(document, DocumentStatus.EXTRACTING)
    stage = _Stage(db, document, IngestStage.EXTRACT, attempt)
    started: datetime = clock.utcnow()
    try:
        result = deps.extract(
            kind,
            open_plaintext(deps.store, deps.keyring, blob),
            config=deps.sandbox,
            options=deps.options,
        )
    except SandboxError as exc:
        log.warning(
            "extraction sandbox failed",
            extra={
                "document_id": str(document.id),
                "code": exc.code,
                "stderr": scrub_text(exc.stderr[:STDERR_LOG_CHARS]),
            },
        )
        stage.finish(error=exc.code, kind=kind)
        _review(
            db, document, ReviewKind.UNPARSED, {"reason": "extraction_failed", "code": exc.code}
        )
        return _finish(db, document, DocumentStatus.FAILED, error=f"extraction failed: {exc.code}")
    except (DecryptionError, BlobStoreError) as exc:
        stage.finish(error="blob_unreadable", kind=kind)
        return _finish(
            db, document, DocumentStatus.FAILED, error=f"blob unreadable ({type(exc).__name__})"
        )
    elapsed = (clock.utcnow() - started).total_seconds()
    if not result.ok:
        code = result.error.code if result.error else "unknown"
        stage.finish(error=code, kind=kind, seconds=round(elapsed, 3))
        _review(db, document, ReviewKind.UNPARSED, {"reason": "extraction_failed", "code": code})
        return _finish(db, document, DocumentStatus.FAILED, error=f"extraction failed: {code}")
    document.extraction = result.model_dump(mode="json")
    document.parser_version = result.extractor_version
    stage.finish(
        kind=kind,
        seconds=round(elapsed, 3),
        tables=len(result.tables),
        statements=len(result.statements),
        warnings=list(result.warnings),
    )

    # ---- classify -------------------------------------------------------------
    _set_status(document, DocumentStatus.CLASSIFYING)
    stage = _Stage(db, document, IngestStage.CLASSIFY, attempt)
    stage.finish(**_classify(db, document, result))

    # ---- parse / match / commit (structured importer) -------------------------
    _set_status(document, DocumentStatus.PARSING)
    stage = _Stage(db, document, IngestStage.PARSE, attempt)
    importer = deps.importer or structured_importer()
    if importer is None:
        stage.finish(importer=None)
        _review(db, document, ReviewKind.UNPARSED, {"reason": "import_pending", "kind": kind})
        outcome = ImportOutcome(DocumentStatus.NEEDS_REVIEW, {"routed": "import_pending"})
    else:
        outcome = importer(db, document, result)
        stage.finish(**outcome.summary)
    if outcome.status not in (DocumentStatus.PROCESSED, DocumentStatus.NEEDS_REVIEW):
        return _finish(db, document, outcome.status, facts=outcome.summary)
    stage = _Stage(db, document, IngestStage.COMMIT, attempt)
    promote_blob(db, deps.store, blob)
    stage.finish(area="permanent")
    return _finish(db, document, outcome.status, facts=outcome.summary)

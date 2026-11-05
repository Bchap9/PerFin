# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ingestion pipeline (INGESTION.md "Pipeline stages", ARCHITECTURE.md).

``received -> scanning -> extracting -> classifying -> parsing -> processed``
with the exits ``rejected`` (scan), ``failed`` (extraction) and
``needs_review``. :func:`ingest_document` runs every stage for one document
inside the worker job's transaction (``app.workers.handlers.ingest``), so a
crash or a retryable error leaves the document ``received`` for the next
attempt; each stage run is recorded as an ``ingest_job`` row.

Phase 2 routes only **structured** formats (CSV, TSV, OFX, QFX, QIF):

* **scan** (SECURITY.md C9): with ``PERFIN_ANTIVIRUS=clamav`` the
  quarantined plaintext is streamed to the ClamAV sidecar
  (``app.integrations.antivirus``) before anything parses it. A positive
  result makes the document ``rejected`` with the signature name recorded
  (``document.error``, stage log, audit); the blob stays in quarantine and
  is never served. When the scan cannot run (clamd down, timeout, file
  above ``PERFIN_CLAMAV_MAX_MB``), ``PERFIN_ANTIVIRUS_FAIL_MODE=closed`` (the
  default) retries a retryable failure through the job queue
  (:class:`ScanUnavailableError`) and on the last attempt, or for a
  non-retryable one, marks the document ``failed`` with an ``unparsed``
  item (``scan_failed``); ``open`` goes on unscanned and logs it.
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
* **parse (PDF, images)**: the document parsers (``app.ingestion.parsers``)
  read the stored pages in the worker, hinted by the classifier's
  fingerprint (institution key, parser name, date order): statements with
  per-field confidence and a reconciliation (``app.ingestion.reconcile``)
  go through the structured importer like an OFX statement (rows below
  ``confidence_threshold`` or of a statement that does not reconcile are
  imported pending, with ``low_confidence_field`` items; SECURITY.md C12);
  brokerage statements (``app.ingestion.parsers.brokerage``: holdings and
  activity, reconciled positions x prices and cash) go the same way into
  ``app.domain.investments.statement_import`` (held back when they do not
  reconcile); a PDF of several accounts gives one brokerage statement per
  account, each matched to its account by its own last four digits, and the
  document itself is linked to no account (an account the classifier
  matched is undone; one chosen at upload stays); a payslip is kept on the
  document (``kind = payslip``, period) and goes to review when its figures
  do not add up; a loan statement (``app.ingestion.parsers.loan_statement``)
  records its payment split and balances on the matching loan
  (``app.domain.loans.statement_import``; held back when it does not
  reconcile); tax forms (``app.ingestion.parsers.tax_forms``:
  form type, year, issuer name and amount boxes only) go to the tax vault
  (``app.domain.tax.ingest``: form data, tax year, checklist ticked off), and
  the stored pages and search index of a tax form keep no identity text
  (``redact_identity``). Parser output stays in
  ``document.extraction`` (``statements``, ``parse``).
* Accepted documents (``processed`` or ``needs_review`` after a successful
  extraction) move from quarantine to the permanent area
  (:func:`app.domain.documents.service.promote_blob`); failed ones stay in
  quarantine.

Every outcome is audited (``document.ingest``) and emits ``document.updated``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import func, select
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
    Blob,
    Document,
    DocumentKind,
    DocumentStatus,
    IngestJob,
    IngestStage,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.previews import discard_previews, store_previews
from app.domain.documents.search import clear_search_index, update_search_index
from app.domain.documents.service import open_plaintext, promote_blob
from app.domain.imports.service import import_extraction
from app.domain.loans.statement_import import import_loan_statement
from app.domain.tax.ingest import import_tax_forms
from app.ingestion.children import document_depth, receive_children
from app.ingestion.classify.fingerprints import builtin_fingerprints
from app.ingestion.classify.service import classify_document
from app.ingestion.intake.sniff import FileType
from app.ingestion.match.accounts import importable_account_ids, match_account
from app.ingestion.match.receipts import apply_receipt
from app.ingestion.parsers.confidence import DEFAULT_THRESHOLD
from app.ingestion.parsers.receipt import PARSER_NAME as RECEIPT_PARSER
from app.ingestion.parsers.receipt import PARSER_VERSION as RECEIPT_PARSER_VERSION
from app.ingestion.parsers.receipt import parse_email_receipt
from app.ingestion.parsers.registry import DOCUMENT_KINDS, parse_extraction
from app.ingestion.parsers.tax_forms import carries_tax_identity, redact_identity
from app.ingestion.sandbox.protocol import (
    CONTAINER_KINDS,
    REVIEW_ERROR_CODES,
    WARNINGS_MAX,
    ExtractOptions,
    ExtractResult,
    InvestmentStatement,
    LoanStatement,
    ParseInfo,
    ParserRef,
    Payslip,
    Receipt,
    Statement,
    TaxFormRead,
)
from app.ingestion.sandbox.runner import SandboxConfig, SandboxError, run_extraction
from app.integrations.antivirus import Scanner

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
    #: The antivirus scanner (None: scanning off) and what happens when it fails.
    scanner: Scanner | None = None
    antivirus_fail_mode: str = "closed"
    extract: Extract = run_extraction
    importer: StructuredImporter | None = None
    #: Size cap for documents found inside an EML or ZIP (the upload cap).
    child_max_bytes: int = 50 * 1024 * 1024
    #: Parsed fields below this confidence are imported pending (C12).
    confidence_threshold: float = DEFAULT_THRESHOLD


class ScanUnavailableError(RuntimeError):
    """The antivirus scan could not run and the pipeline fails closed: retry the job."""

    def __init__(self, code: str) -> None:
        super().__init__(f"antivirus scan unavailable: {code}")
        self.code = code


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
    structured: list[Statement | InvestmentStatement] = [
        *result.statements,
        *result.investments,
    ]
    if not (structured or result.tables) and result.kind not in CONTAINER_KINDS:
        # PDFs, images and text: fingerprints, then the heuristic classifier.
        return classify_document(db, document, result)
    facts: dict[str, Any] = {}
    if structured or result.tables:
        document.kind = DocumentKind.STATEMENT
    starts = [s.period_start for s in structured if s.period_start]
    ends = [s.period_end for s in structured if s.period_end]
    if starts and ends and min(starts) <= max(ends):
        document.period_start, document.period_end = min(starts), max(ends)
    if document.account_id is None:
        last4s = {s.account.last4 for s in structured if s.account.last4}
        if len(last4s) == 1:
            institutions = {s.account.institution for s in structured}
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


def _parse_hints(document: Document) -> dict[str, str | None]:
    """The classifier's fingerprint match (``document.confidence``) as parser hints."""
    facts = document.confidence or {}
    fingerprint = facts.get("fingerprint") if isinstance(facts, dict) else None
    hints: dict[str, str | None] = {
        "kind": str(document.kind) if document.kind is not DocumentKind.UNKNOWN else None
    }
    if isinstance(fingerprint, dict):
        key = fingerprint.get("key")
        hints["institution"] = key if isinstance(key, str) else None
        parser = fingerprint.get("parser")
        hints["parser_name"] = parser if isinstance(parser, str) else None
        if fingerprint.get("origin") == "builtin" and isinstance(key, str):
            spec = next((f.spec for f in builtin_fingerprints() if f.key == key), None)
            hints["date_order"] = spec.date_order if spec is not None else None
    return hints


def _unlink_classified_account(document: Document) -> None:
    """Undo the account the classifier matched (``fields.account``); an uploader's choice stays.

    The classifier links a document to an account when it finds one set of
    last four digits; on a statement of several accounts that is one
    account's number (the others printed in a form it does not read), so
    the link would be wrong for the rest.
    """
    facts = document.confidence if isinstance(document.confidence, dict) else {}
    fields = facts.get("fields")
    if document.account_id is None or not isinstance(fields, dict) or "account" not in fields:
        return
    document.account_id = None
    document.confidence = {
        **facts,
        "fields": {key: value for key, value in fields.items() if key != "account"},
    }


def _parse_document(
    db: Session, document: Document, result: ExtractResult, threshold: float
) -> tuple[ExtractResult, dict[str, Any]]:
    """Run the document parsers (``app.ingestion.parsers``) on a PDF's or image's pages.

    The parsed statements and :class:`ParseInfo` are kept in
    ``document.extraction`` next to the pages they were read from, so a
    re-import or a newer parser needs no new upload or extraction.
    """
    hints = _parse_hints(document)
    result = parse_extraction(
        result,
        threshold=threshold,
        parser_name=hints.get("parser_name"),
        institution=hints.get("institution"),
        kind=hints.get("kind"),
        date_order=hints.get("date_order"),
    )
    info = result.parse
    if info is None:  # pragma: no cover - parse_extraction always records it
        return result, {}
    # A tax document outside the registered set (a prior-year 1040, an
    # unrecognised form) can be classified as anything; its identity text is
    # masked too unless a statement, payslip or loan parser read the pages
    # (their review tools need the page text). P56-SR-R1-02.
    unregistered_tax = (
        not info.tax_forms
        and document.kind is not DocumentKind.TAX_FORM
        and info.payslip is None
        and info.loan is None
        and not any(s.parser is not None for s in result.statements)
        and not any(i.parser is not None for i in result.investments)
        and carries_tax_identity(result)
    )
    if info.tax_forms or document.kind is DocumentKind.TAX_FORM or unregistered_tax:
        # Identity boxes (SSN/TIN/EIN, names, addresses, account numbers) of
        # a tax form are masked in the stored pages and the search index
        # (app.ingestion.parsers.tax_forms.redact_identity).
        result = redact_identity(result, info.tax_forms)
        if unregistered_tax or info.receipt is not None:
            # Not a receipt: read from the unredacted pages, its "merchant"
            # would be the taxpayer's name.
            info = info.model_copy(
                update={
                    "receipt": None,
                    "warnings": [*info.warnings, "tax_identity_redacted"][-WARNINGS_MAX:],
                }
            )
            result = result.model_copy(update={"parse": info})
            if document.kind is DocumentKind.RECEIPT:
                document.kind = DocumentKind.UNKNOWN
        document.extraction = result.model_dump(mode="json")
        update_search_index(db, document)
    document.extraction = result.model_dump(mode="json")
    facts: dict[str, Any] = {
        "parser": info.parser.name if info.parser else None,
        "parser_version": info.parser.version if info.parser else None,
        "pending_items": info.pending_items,
        "skipped_lines": info.skipped_lines,
        "parse_warnings": list(info.warnings),
    }
    if info.parser is None:
        return result, facts
    document.parser_version = f"{info.parser.name}/{info.parser.version}"[:64]
    parsed = [s for s in result.statements if s.parser is not None]
    brokerage = [i for i in result.investments if i.parser is not None]
    if len({i.account.last4 for i in brokerage}) > 1:
        # Several accounts in one document: each statement finds its own
        # account by its last four digits; the document is on none of them.
        facts["investment_accounts"] = len(brokerage)
        _unlink_classified_account(document)
    read: list[Statement | InvestmentStatement] = [
        *(s for s in parsed if s.transactions),
        *(i for i in brokerage if i.positions or i.activity),
    ]
    if read:
        document.kind = DocumentKind.STATEMENT
        first = read[0]
        if (
            first.period_start is not None
            and first.period_end is not None
            and first.confidence.get("period", 0.0) >= threshold
        ):
            document.period_start, document.period_end = first.period_start, first.period_end
    document.confidence = {
        **(document.confidence or {}),
        "parse": {
            "parser": info.parser.name,
            "version": info.parser.version,
            "threshold": threshold,
            "statements": [s.confidence for s in parsed],
            "reconciliation": [s.reconciliation.status for s in parsed if s.reconciliation],
            "low_confidence_rows": sum(
                1 for s in parsed for t in s.transactions if t.low_confidence
            ),
            "payslip": info.payslip.confidence if info.payslip else None,
            "receipt": info.receipt.confidence if info.receipt else None,
            "investments": [i.confidence for i in brokerage],
            "investment_reconciliation": [
                i.reconciliation.status for i in brokerage if i.reconciliation
            ],
        },
    }
    if info.skipped_lines:
        _review(
            db,
            document,
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {"reason": "lines_skipped", "count": info.skipped_lines, "parser": info.parser.name},
        )
    return result, facts


def _loan_outcome(db: Session, document: Document, loan: LoanStatement) -> ImportOutcome:
    """A loan statement records its payment on the matching loan (``app.domain.loans``)."""
    done = import_loan_statement(db, document, loan)
    status = DocumentStatus.NEEDS_REVIEW if done.review else DocumentStatus.PROCESSED
    return ImportOutcome(status, done.summary)


def _tax_form_outcome(
    db: Session, document: Document, forms: Sequence[TaxFormRead]
) -> ImportOutcome:
    """Tax forms go to the tax vault (``app.domain.tax.ingest``: amounts, checklist)."""
    done = import_tax_forms(db, document, forms)
    status = DocumentStatus.NEEDS_REVIEW if done.review else DocumentStatus.PROCESSED
    return ImportOutcome(status, done.summary)


def _payslip_outcome(
    db: Session, document: Document, payslip: Payslip, threshold: float
) -> ImportOutcome:
    """A payslip is kept on the document (no ledger rows); doubtful figures go to review."""
    document.kind = DocumentKind.PAYSLIP
    if (
        payslip.period_start is not None
        and payslip.period_end is not None
        and payslip.confidence.get("period", 0.0) >= threshold
    ):
        document.period_start, document.period_end = payslip.period_start, payslip.period_end
    if payslip.low_confidence:
        _review(
            db,
            document,
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": (
                    "payslip_does_not_add_up" if payslip.adds_up is False else "low_confidence"
                ),
                "fields": list(payslip.low_confidence),
                "adds_up": payslip.adds_up,
            },
        )
    open_items = db.execute(
        select(func.count(ReviewItem.id)).where(
            ReviewItem.household_id == document.household_id,
            ReviewItem.document_id == document.id,
            ReviewItem.state == ReviewItemState.OPEN,
        )
    ).scalar_one()
    status = DocumentStatus.NEEDS_REVIEW if open_items else DocumentStatus.PROCESSED
    return ImportOutcome(status, {"payslip": True, "low_confidence": len(payslip.low_confidence)})


def _scan(
    db: Session,
    document: Document,
    blob: Blob,
    deps: PipelineDeps,
    stage: _Stage,
    *,
    final_attempt: bool,
) -> DocumentStatus | None:
    """The antivirus stage; a status ends the pipeline, None goes on (module doc)."""
    scanner = deps.scanner
    if scanner is None:
        stage.finish(antivirus=deps.antivirus, scanned=False)
        return None
    try:
        verdict = scanner.scan(open_plaintext(deps.store, deps.keyring, blob), size=blob.size)
    except (DecryptionError, BlobStoreError) as exc:
        stage.finish(error="blob_unreadable", antivirus=scanner.name)
        return _finish(
            db, document, DocumentStatus.FAILED, error=f"blob unreadable ({type(exc).__name__})"
        )
    if verdict.infected:
        signature = verdict.signature or "unknown"
        stage.finish(antivirus=scanner.name, scanned=True, verdict="infected", signature=signature)
        log.warning(
            "antivirus: document rejected",
            extra={"document_id": str(document.id), "signature": signature},
        )
        return _finish(
            db,
            document,
            DocumentStatus.REJECTED,
            error=f"malware detected: {signature}",
            facts={"signature": signature, "antivirus": scanner.name},
        )
    if verdict.status == "clean":
        stage.finish(antivirus=scanner.name, scanned=True, verdict="clean")
        return None
    code = verdict.error or "scanner_error"
    if deps.antivirus_fail_mode == "open":
        log.warning(
            "antivirus: scan failed, continuing unscanned (fail open)",
            extra={"document_id": str(document.id), "code": code},
        )
        stage.finish(antivirus=scanner.name, scanned=False, scan_error=code, fail_mode="open")
        return None
    if verdict.retryable and not final_attempt:
        raise ScanUnavailableError(code)
    stage.finish(error=f"scan_{code}", antivirus=scanner.name, scanned=False, fail_mode="closed")
    _review(db, document, ReviewKind.UNPARSED, {"reason": "scan_failed", "code": code})
    return _finish(db, document, DocumentStatus.FAILED, error=f"antivirus scan failed: {code}")


def _receipt_outcome(
    db: Session, document: Document, result: ExtractResult, receipt: Receipt, threshold: float
) -> tuple[ExtractResult, ImportOutcome]:
    """A receipt is linked to the transaction that paid it, or queued for a person.

    ``app.ingestion.match.receipts.apply_receipt`` matches it (amount, date
    window, merchant alias) among the transactions the uploader may edit;
    the receipt, its merchant resolved to a payee, stays in the extraction.
    """
    outcome = apply_receipt(db, document, receipt, threshold=threshold)
    info = result.parse or ParseInfo(threshold=threshold)
    if info.parser is None:
        info = info.model_copy(
            update={
                "parser": ParserRef(
                    name=RECEIPT_PARSER, version=RECEIPT_PARSER_VERSION, kind="receipt"
                )
            }
        )
    result = result.model_copy(
        update={"parse": info.model_copy(update={"receipt": outcome.receipt})}
    )
    document.extraction = result.model_dump(mode="json")
    if document.parser_version is None or not document.parser_version.startswith(RECEIPT_PARSER):
        document.parser_version = f"{RECEIPT_PARSER}/{RECEIPT_PARSER_VERSION}"
    return result, ImportOutcome(outcome.status, outcome.summary)


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
    final_attempt: bool = True,
) -> DocumentStatus | None:
    """Run the pipeline for one document; return its final status (None: it is gone).

    ``final_attempt`` is False while the job queue would still retry: a
    retryable scan failure then raises :class:`ScanUnavailableError` (the
    transaction rolls back) instead of failing the document.
    """
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
    scanned = _scan(db, document, blob, deps, stage, final_attempt=final_attempt)
    if scanned is not None:
        return scanned

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
    # A document that came out of an EML or ZIP is extracted at its depth (D4).
    options = deps.options.model_copy(update={"depth": document_depth(document)})
    clear_search_index(db, document)
    try:
        result = deps.extract(
            kind,
            open_plaintext(deps.store, deps.keyring, blob),
            config=deps.sandbox,
            options=options,
        )
    except SandboxError as exc:
        log.warning(
            "extraction sandbox failed",
            extra={
                "document_id": str(document.id),
                "code": exc.code,
                "detail": exc.detail,
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
        if code in REVIEW_ERROR_CODES:
            # Not broken, but a person must look (a password-protected PDF,
            # an archive nested too deep): kept and parked for review.
            _review(db, document, ReviewKind.UNPARSED, {"reason": code, "kind": kind})
            promote_blob(db, deps.store, blob)
            return _finish(db, document, DocumentStatus.NEEDS_REVIEW, facts={"routed": code})
        _review(db, document, ReviewKind.UNPARSED, {"reason": "extraction_failed", "code": code})
        return _finish(db, document, DocumentStatus.FAILED, error=f"extraction failed: {code}")
    # Previews become encrypted blobs and children new documents; the stored
    # extraction keeps references only, never their bytes.
    discard_previews(
        db, deps.store, household_id=document.household_id, extraction=document.extraction
    )
    previews = store_previews(
        db,
        deps.store,
        deps.keyring,
        household_id=document.household_id,
        previews=result.previews,
    )
    children = receive_children(
        db,
        store=deps.store,
        keyring=deps.keyring,
        parent=document,
        children=result.children,
        max_bytes=deps.child_max_bytes,
    )
    result = result.model_copy(
        update={
            "previews": [],
            "children": [],
            "stored_previews": previews,
            "stored_children": children,
        }
    )
    document.extraction = result.model_dump(mode="json")
    document.parser_version = result.extractor_version
    db.flush()
    indexed = update_search_index(db, document)
    stage.finish(
        kind=kind,
        seconds=round(elapsed, 3),
        tables=len(result.tables),
        statements=len(result.statements),
        pages=len(result.pages),
        previews=len(previews),
        children=sum(1 for c in children if c.document_id),
        children_rejected=sum(1 for c in children if c.rejected),
        indexed_chars=indexed,
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
    parse_facts: dict[str, Any] = {}
    if kind in DOCUMENT_KINDS:
        # PDF and image pages -> statements or a payslip (worker side, on the
        # stored pages; INGESTION.md "Parse").
        result, parse_facts = _parse_document(db, document, result, deps.confidence_threshold)
    if kind in CONTAINER_KINDS:
        # An archive or mail is done once its contents are documents of
        # their own; refused or left-out contents need a look.
        rejected = [c.rejected for c in result.stored_children if c.rejected]
        incomplete = bool(rejected) or result.truncated or "depth_limit" in result.warnings
        if incomplete:
            _review(
                db,
                document,
                ReviewKind.UNPARSED,
                {
                    "reason": "container_incomplete",
                    "rejected": len(rejected),
                    "truncated": result.truncated,
                    "depth_limit": "depth_limit" in result.warnings,
                },
            )
        summary: dict[str, Any] = {"children": len(result.stored_children) - len(rejected)}
        status = DocumentStatus.NEEDS_REVIEW if incomplete else DocumentStatus.PROCESSED
        # A receipt mail's body is a receipt itself (its attachments are
        # documents of their own and are matched on their own).
        mail_receipt = (
            parse_email_receipt(result, threshold=deps.confidence_threshold)
            if kind == "eml"
            else None
        )
        if mail_receipt is not None:
            result, matched = _receipt_outcome(
                db, document, result, mail_receipt, deps.confidence_threshold
            )
            summary.update(matched.summary)
            if matched.status is DocumentStatus.NEEDS_REVIEW:
                status = DocumentStatus.NEEDS_REVIEW
        stage.finish(**summary)
        outcome = ImportOutcome(status, summary)
    elif result.parse is not None and result.parse.receipt is not None:
        result, outcome = _receipt_outcome(
            db, document, result, result.parse.receipt, deps.confidence_threshold
        )
        stage.finish(**parse_facts, **outcome.summary)
    elif result.parse is not None and result.parse.payslip is not None:
        outcome = _payslip_outcome(db, document, result.parse.payslip, deps.confidence_threshold)
        stage.finish(**parse_facts, **outcome.summary)
    elif result.parse is not None and result.parse.loan is not None:
        outcome = _loan_outcome(db, document, result.parse.loan)
        stage.finish(**parse_facts, **outcome.summary)
    elif result.parse is not None and result.parse.tax_forms:
        outcome = _tax_form_outcome(db, document, result.parse.tax_forms)
        stage.finish(**parse_facts, **outcome.summary)
    elif importer is None:
        stage.finish(importer=None, **parse_facts)
        _review(db, document, ReviewKind.UNPARSED, {"reason": "import_pending", "kind": kind})
        outcome = ImportOutcome(DocumentStatus.NEEDS_REVIEW, {"routed": "import_pending"})
    else:
        outcome = importer(db, document, result)
        stage.finish(**parse_facts, **outcome.summary)
    if outcome.status not in (DocumentStatus.PROCESSED, DocumentStatus.NEEDS_REVIEW):
        return _finish(db, document, outcome.status, facts=outcome.summary)
    stage = _Stage(db, document, IngestStage.COMMIT, attempt)
    promote_blob(db, deps.store, blob)
    stage.finish(area="permanent")
    return _finish(db, document, outcome.status, facts=outcome.summary)

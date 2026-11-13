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
  brokerage activity tables read as investment statements
  (``app.ingestion.parsers.brokerage_table``), ``import_hash`` dedupe,
  rules, balance snapshots, review items). Without
  one, the document waits in ``needs_review`` (``unparsed``/``import_pending``).
* **parse (PDF, images)**: the document parsers (``app.ingestion.parsers``)
  read the stored pages in the worker, hinted by the classifier's
  fingerprint (institution key, parser name, date order; a household
  fingerprint's learned template, "Teach PerFin",
  ``app.ingestion.parsers.template``, then reads its labelled tables and
  fills the period, account digits and balances; a ``lines_skipped`` item
  carries the boxes of skipped lines): statements with
  per-field confidence and a reconciliation (``app.ingestion.reconcile``)
  go through the structured importer like an OFX statement (rows below
  ``confidence_threshold`` or of a statement that does not reconcile are
  imported pending, with ``low_confidence_field`` items; SECURITY.md C12);
  brokerage statements (``app.ingestion.parsers.brokerage``: holdings and
  activity, reconciled positions x prices and cash) go the same way into
  ``app.domain.investments.statement_import`` (held back when they do not
  reconcile); a PDF of several accounts gives one statement per account
  (``app.ingestion.parsers.accounts``: brokerage, and bank accounts of a
  combined statement), each matched to its account by its own last four
  digits and imported or held on its own, and the document itself is
  linked to no account (an account the classifier matched is undone; one
  chosen at upload stays); the document's period
  is the span of the periods its statements (bank and brokerage) read
  with confidence, replacing the classifier's; a payslip is kept on the
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
from datetime import date, datetime
from typing import Any, Final, Protocol

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
    ParserFingerprint,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.previews import discard_previews, store_previews
from app.domain.documents.search import clear_search_index, update_search_index
from app.domain.documents.service import open_plaintext, promote_blob
from app.domain.imports import wording
from app.domain.imports.service import import_extraction
from app.domain.loans.statement_import import import_loan_statement
from app.domain.tax.ingest import import_tax_forms
from app.ingestion.children import document_depth, receive_children
from app.ingestion.classify.fingerprints import builtin_fingerprints
from app.ingestion.classify.service import FIELD_THRESHOLD, classify_document
from app.ingestion.intake.sniff import SNIFF_BYTES, FileType, sniff
from app.ingestion.match.accounts import importable_account_ids, match_account
from app.ingestion.match.receipts import apply_receipt
from app.ingestion.parsers.confidence import DEFAULT_THRESHOLD
from app.ingestion.parsers.receipt import PARSER_NAME as RECEIPT_PARSER
from app.ingestion.parsers.receipt import PARSER_VERSION as RECEIPT_PARSER_VERSION
from app.ingestion.parsers.receipt import parse_email_receipt
from app.ingestion.parsers.registry import DOCUMENT_KINDS, parse_extraction
from app.ingestion.parsers.tax_forms import carries_tax_identity, redact_identity
from app.ingestion.parsers.template import LayoutTemplate, parse_template
from app.ingestion.parsers.vocabulary import Vocabulary, masked
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
from app.ml.client import MLHints

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
    #: The optional local-model adapter (ADR-0010): text -> checked hints.
    ml: Callable[[str], MLHints | None] | None = None
    #: The same adapter asked what kind of activity the words a brokerage
    #: parser could not type are: (action phrases, headings, the statement's
    #: text) -> checked suggestions (``app.ml.client.activity_hints``).
    ml_vocabulary: AskVocabulary | None = None
    #: The text one call to the model carries at most (``PERFIN_ML_MAX_CHARS``).
    ml_max_chars: int = 16_000  # ML_MAX_CHARS (defined below)


#: ``(action phrases, headings, statement text, column headings) -> checked suggestions``.
AskVocabulary = Callable[[Sequence[str], Sequence[str], str, Sequence[str]], Vocabulary | None]


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


#: Extraction warnings of a document read on a retry after the OCR engine
#: crashed its sandbox child (:func:`_extract`).
OCR_RETRIED_WARNING: Final = "ocr_retried:single_block"
OCR_DISABLED_WARNING: Final = "ocr_disabled_after_crash"


def _extract(
    deps: PipelineDeps, kind: str, blob: Blob, options: ExtractOptions, document: Document
) -> ExtractResult:
    """Extract ``blob`` in the sandbox; a PDF or image whose child crashed is tried again.

    Tesseract's page layout analysis crashes natively (SIGSEGV) on some
    scans, which kills the child (``crashed``) and used to fail the
    document. After such a crash the document is read again, each time in a
    **new** sandbox child with the same filter and limits:

    1. with ``ocr_psm="single_block"`` (no layout analysis): the result
       carries the warning ``ocr_retried:single_block``;
    2. if that child crashes too, with ``ocr=False`` (text layers and page
       previews only): the warning is ``ocr_disabled_after_crash`` and the
       caller opens a review item (``ocr_failed``).

    Any other sandbox error (``memory_limit``, ``timeout``, a denied system
    call) is raised at once, as is a crash that both retries do not avoid:
    the first crash is then the error reported. Tables, mails and archives
    never run OCR and are not retried.
    """

    def run(with_options: ExtractOptions) -> ExtractResult:
        return deps.extract(
            kind,
            open_plaintext(deps.store, deps.keyring, blob),
            config=deps.sandbox,
            options=with_options,
        )

    try:
        return run(options)
    except SandboxError as exc:
        uses_ocr = options.ocr and options.max_ocr_pages > 0
        if exc.code != "crashed" or kind not in DOCUMENT_KINDS or not uses_ocr:
            raise
        first = exc
    retries: list[tuple[ExtractOptions, str]] = []
    if options.ocr_psm != "single_block":
        retries.append(
            (options.model_copy(update={"ocr_psm": "single_block"}), OCR_RETRIED_WARNING)
        )
    retries.append(
        (options.model_copy(update={"ocr_psm": "single_block", "ocr": False}), OCR_DISABLED_WARNING)
    )
    for retry_options, warning in retries:
        log.warning(
            "extraction child crashed; reading the document again",
            extra={"document_id": str(document.id), "kind": kind, "retry": warning},
        )
        try:
            result = run(retry_options)
        except SandboxError as exc:
            if exc.code != "crashed":
                raise
            continue
        if not result.ok:
            return result
        # First, so the cap on the list never drops it.
        warnings = [warning, *(w for w in result.warnings if w != warning)][:WARNINGS_MAX]
        return result.model_copy(update={"warnings": warnings})
    raise first


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


#: Rows of a table sent to the local model as text.
ML_TABLE_ROWS: Final = 80
#: Below this the model's kind is kept as a hint only, not set on the document.
ML_KIND_CONFIDENCE: Final = 0.6


def _document_text(result: ExtractResult) -> str:
    if result.text.strip():
        return result.text
    if result.tables:
        return "\n".join(",".join(row) for row in result.tables[0].rows[:ML_TABLE_ROWS])
    return ""


def _ml_hints(
    db: Session,
    document: Document,
    result: ExtractResult,
    ask: Callable[[str], MLHints | None],
) -> None:
    """Ask the local model when PerFin's own readers are unsure (ADR-0010).

    Only for a document whose kind is unknown or whose account nothing
    matched. The checked hints are kept on the document
    (``confidence["ml"]``) for account proposals and the review queue; an
    unknown kind is set from a confident answer (``kind_source: ml``), so the
    parsers get the hint. Nothing is written to the ledger from them.
    """
    if document.kind is not DocumentKind.UNKNOWN and document.account_id is not None:
        return
    text = _document_text(result)
    hints = ask(text) if text.strip() else None
    if hints is None:
        return
    facts = dict(document.confidence or {})
    facts["ml"] = hints.stored()
    if (
        document.kind is DocumentKind.UNKNOWN
        and hints.kind is not None
        and hints.confidence >= ML_KIND_CONFIDENCE
    ):
        document.kind = DocumentKind(hints.kind)
        facts["kind_source"] = "ml"
    document.confidence = facts
    db.flush()


def _learned_template(db: Session, document: Document) -> LayoutTemplate | None:
    """The learned layout of the household fingerprint the classifier matched, if any."""
    facts = document.confidence or {}
    fingerprint = facts.get("fingerprint") if isinstance(facts, dict) else None
    if not isinstance(fingerprint, dict) or fingerprint.get("origin") != "household":
        return None
    try:
        fingerprint_id = uuid.UUID(str(fingerprint.get("id")))
    except ValueError:
        return None
    stored = db.execute(
        select(ParserFingerprint.template).where(
            ParserFingerprint.household_id == document.household_id,
            ParserFingerprint.id == fingerprint_id,
        )
    ).scalar_one_or_none()
    return parse_template(stored)


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


def _period_hint(document: Document) -> tuple[date, date, float] | None:
    """The period the classifier read from a printed label, with its confidence.

    None for a period inferred from row or line dates (the parsers read
    those themselves), for one below the classifier's field threshold, and
    for anything that is not two ISO days in order.
    """
    facts = document.confidence
    if not isinstance(facts, dict):
        return None
    period, fields = facts.get("period"), facts.get("fields")
    if not isinstance(period, dict) or not isinstance(fields, dict):
        return None
    if period.get("inferred") is not False:
        return None
    confidence = fields.get("period")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    if confidence < FIELD_THRESHOLD:
        return None
    start, end = period.get("start"), period.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return None
    if first > last:
        return None
    return first, last, float(confidence)


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


#: Where the local model's suggested words are kept on a document.
ML_VOCABULARY_KEY: Final = wording.SUGGESTIONS_KEY
#: The text one call to the model carries, unless the deployment says otherwise.
ML_MAX_CHARS: Final = 16_000
#: Calls to the local model about one document's unknown wording, at most.
ML_VOCABULARY_CALLS: Final = 6


def _vocabulary_batches(
    result: ExtractResult,
    actions: Sequence[str],
    headings: Sequence[str],
    max_chars: int,
    columns: Sequence[str] = (),
) -> list[tuple[list[str], list[str], str, list[str]]]:
    """``(actions, headings, text, columns)`` per call to the model (ADR-0010).

    The model reads a phrase in the statement it is from, and one call
    carries ``max_chars`` of text at most (``PERFIN_ML_MAX_CHARS``): a long
    statement does not fit in one. Each phrase is asked about together with
    the page that prints it: the pages printing a phrase still to ask about
    are taken in order and a new call starts when the next page would not
    fit, up to :data:`ML_VOCABULARY_CALLS` calls (what is left over is asked
    in the last one, without its pages). A statement that fits in one call
    is sent whole, the pages that print a phrase first. A phrase found on no
    page (a heading over two lines) is asked in the first call.
    """
    wanted_actions = [key for key in actions if key]
    wanted_headings = [key for key in headings if key]
    wanted_columns = [key for key in columns if key]
    wanted = [*wanted_actions, *wanted_headings, *wanted_columns]
    pages = [(text, masked(text)) for page in result.pages if (text := page.text.strip())]
    if not pages:
        return [(wanted_actions, wanted_headings, _document_text(result), wanted_columns)]
    asked: set[str] = set()
    parts: list[tuple[set[str], list[str]]] = []  # (phrases, page texts)
    size = 0
    for text, flat in pages:
        new = {key for key in wanted if key not in asked and key in flat}
        if not new:
            continue
        if not parts or (size + len(text) > max_chars and len(parts) < ML_VOCABULARY_CALLS):
            parts.append((set(), []))
            size = 0
        parts[-1][0].update(new)
        parts[-1][1].append(text)
        size += len(text) + 2
        asked |= new
    if not parts:
        parts.append((set(), []))
    parts[0][0].update(key for key in wanted if key not in asked)
    if len(parts) == 1:
        # Room left: the rest of the statement, for the model to read it whole.
        printed = set(parts[0][1])
        parts[0][1].extend(text for text, _ in pages if text not in printed)
    return [
        (
            [key for key in wanted_actions if key in phrases],
            [key for key in wanted_headings if key in phrases],
            "\n\n".join(texts),
            [key for key in wanted_columns if key in phrases],
        )
        for phrases, texts in parts
    ]


def _parse_document(
    db: Session,
    document: Document,
    result: ExtractResult,
    threshold: float,
    ask: AskVocabulary | None = None,
    max_chars: int = ML_MAX_CHARS,
) -> tuple[ExtractResult, dict[str, Any]]:
    """Run the document parsers (``app.ingestion.parsers``) on a PDF's or image's pages.

    The parsed statements and :class:`ParseInfo` are kept in
    ``document.extraction`` next to the pages they were read from, so a
    re-import or a newer parser needs no new upload or extraction.

    **Words no parser types.** When a brokerage parser left activity rows
    unread because their words are in no vocabulary of its own
    (``ParseInfo.unknown_actions`` / ``unknown_headings``), or a table
    unread because its header names no column it knows as the date or the
    amount (``unknown_columns``), the pages are read once more with what is
    known about those words:

    1. the wording the household confirmed on an earlier statement of this
       layout (``app.domain.imports.wording``): typed like the parser's own
       words, nobody is asked;
    2. for what is still untyped, the optional local model's suggestions
       (``ask``, ADR-0010): it is sent those phrases with the statement's
       text to read them in (a long statement in several calls, each
       phrase with the page that prints it: :func:`_vocabulary_batches`),
       answers a kind for each from a closed list, and the checked answer
       is kept on the document
       (``confidence["ml_vocabulary"]``; used again without asking). A row
       typed by a suggestion is marked ``suggested_type`` and is never
       written without a person's confirmation.

    With nothing learned and no answer the document is read as without a model.
    """
    hints = _parse_hints(document)
    period = _period_hint(document)
    received = document.created_at.date() if document.created_at is not None else None
    template = _learned_template(db, document)
    pages = result

    def parse_with(words: Vocabulary | None) -> ExtractResult:
        return parse_extraction(
            pages,
            threshold=threshold,
            parser_name=hints.get("parser_name"),
            institution=hints.get("institution"),
            kind=hints.get("kind"),
            date_order=hints.get("date_order"),
            template=template,
            vocabulary=words,
            period=period,
            not_after=received,
        )

    result = parse_with(None)
    untyped = result.parse
    if untyped is not None and (
        untyped.unknown_actions or untyped.unknown_headings or untyped.unknown_columns
    ):
        known = wording.household_vocabulary(
            db, document.household_id, wording.layout_key(untyped.parser)
        ).merged(Vocabulary.from_stored((document.confidence or {}).get(ML_VOCABULARY_KEY)))
        actions, headings = known.untyped(untyped.unknown_actions, untyped.unknown_headings)
        columns = known.untyped_columns(untyped.unknown_columns)
        if ask is not None and (actions or headings or columns):
            answered = False
            for some_actions, some_headings, text, some_columns in _vocabulary_batches(
                pages, actions, headings, max_chars, columns
            ):
                suggested = ask(some_actions, some_headings, text, some_columns)
                if suggested is not None and not suggested.empty:
                    known = known.merged(suggested)
                    answered = True
            if answered:
                document.confidence = {
                    **(document.confidence or {}),
                    ML_VOCABULARY_KEY: known.stored(),
                }
                db.flush()
        if not known.empty:
            result = parse_with(known)
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
    named = {s.account.last4 for s in parsed} | {i.account.last4 for i in brokerage}
    if len(named) > 1:
        # Several accounts in one document (a brokerage report of several, a
        # combined bank statement): each statement finds its own account by
        # its last four digits; the document is on none of them.
        if brokerage:
            facts["investment_accounts"] = len(brokerage)
        if len(parsed) > 1:
            facts["statement_accounts"] = len(parsed)
        _unlink_classified_account(document)
    read: list[Statement | InvestmentStatement] = [
        *(s for s in parsed if s.transactions),
        *(i for i in brokerage if i.positions or i.activity),
    ]
    if read:
        document.kind = DocumentKind.STATEMENT
        # The document's period: what its statements (bank and brokerage)
        # read with confidence, all of them together.
        periods = [
            (s.period_start, s.period_end)
            for s in read
            if s.period_start is not None
            and s.period_end is not None
            and s.confidence.get("period", 0.0) >= threshold
        ]
        if periods:
            document.period_start = min(start for start, _ in periods)
            document.period_end = max(end for _, end in periods)
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
            {
                "reason": "lines_skipped",
                "count": info.skipped_lines,
                "parser": info.parser.name,
                # Where they are (page and box), when the parser knows.
                "lines": [
                    {"page": r.page, "bbox": [round(v, 2) for v in r.bbox]}
                    for r in info.skipped_regions
                ],
            },
        )
    if info.unread_accounts:
        # The document lists accounts no statement was read for: said, so a
        # missing account is never mistaken for an imported one.
        _review(
            db,
            document,
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "accounts_not_read",
                "accounts": list(info.unread_accounts),
                "count": len(info.unread_accounts),
                "parser": info.parser.name,
            },
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


def _sniff_again(deps: PipelineDeps, blob: Blob) -> FileType | None:
    """CSV or TSV when the first :data:`SNIFF_BYTES` of a stored text file are a table now."""
    head = bytearray()
    try:
        for chunk in open_plaintext(deps.store, deps.keyring, blob):
            head.extend(chunk)
            if len(head) >= SNIFF_BYTES:
                break
    except (DecryptionError, BlobStoreError):
        return None
    found = sniff(bytes(head))
    return found if found in (FileType.CSV, FileType.TSV) else None


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
    if kind is None and file_type is FileType.TEXT:
        # Text no reader took when it arrived may be a table a later release
        # recognises (rows under a preamble): look at its first bytes again,
        # as the upload did, so "Process again" can read it.
        again = _sniff_again(deps, blob)
        if again is not None:
            document.mime_detected = again.value
            kind = again.extract_kind
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
        result = _extract(deps, kind, blob, options, document)
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
    ocr_gave_up = OCR_DISABLED_WARNING in result.warnings
    if ocr_gave_up:
        # Read without OCR after the engine crashed twice: scanned pages have
        # no text, and the person is told so (never a silent, partial read).
        _review(db, document, ReviewKind.UNPARSED, {"reason": "ocr_failed", "kind": kind})
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
    if deps.ml is not None and kind not in CONTAINER_KINDS:
        _ml_hints(db, document, result, deps.ml)

    # ---- parse / match / commit (structured importer) -------------------------
    _set_status(document, DocumentStatus.PARSING)
    stage = _Stage(db, document, IngestStage.PARSE, attempt)
    importer = deps.importer or structured_importer()
    parse_facts: dict[str, Any] = {}
    if kind in DOCUMENT_KINDS:
        # PDF and image pages -> statements or a payslip (worker side, on the
        # stored pages; INGESTION.md "Parse").
        result, parse_facts = _parse_document(
            db,
            document,
            result,
            deps.confidence_threshold,
            deps.ml_vocabulary,
            deps.ml_max_chars,
        )
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
    # A document whose scan OCR could not read has an open item: it waits.
    status = DocumentStatus.NEEDS_REVIEW if ocr_gave_up else outcome.status
    return _finish(db, document, status, facts=outcome.summary)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document operations behind the documents API (API.md ``/documents``).

:mod:`app.domain.documents.service` owns storage (record, open, promote,
crypto-shred); this module adds who may do what through the API:

* **See** (``GET /documents``, ``GET /documents/{id}``):
  :func:`~app.domain.documents.service.visible_document_filter`: owners and
  admins every document of the household; members and viewers the documents
  of accounts they may view and their own uploads that are not attached to an
  account. Anything else is ``404 document-not-found`` (F3).
* **Change** (reprocess, link, delete): member or above who can see the
  document **and** edit its account (owner/admin always; an unattached
  document: its uploader). A member with only a ``view`` grant is ``403``.
* **Download** (SECURITY.md I1): member or above who can see it; a viewer
  only when the household setting :data:`VIEWER_DOWNLOAD_SETTING` is
  ``true`` (the explicit grant; default off: viewers see metadata only). A
  document whose blob is still in the **quarantine** area (not yet accepted
  by the pipeline: received, failed, rejected, a possible duplicate) is
  never served (``409 document-quarantined``). The response is built by the
  router with :func:`app.core.headers.attachment_headers` (C4).

Every change is audited (``document.reprocess``, ``document.link``,
``document.download``, ``document.delete``) and emits an event with ids
only.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

from fastapi.responses import JSONResponse
from sqlalchemy import func, select, tuple_, update
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.crypto import DecryptionError, Keyring
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.idempotency import idempotent
from app.core.logging import get_logger
from app.core.settings import Settings
from app.db.models import Setting, SettingScope
from app.db.queue import enqueue
from app.domain.accounts.visibility import (
    can_edit_account,
    get_visible_account,
    sees_all_accounts,
)
from app.domain.documents import repo, search, service
from app.domain.documents.blobstore import BlobStore, BlobStoreError, LocalFSBlobStore
from app.domain.documents.models import (
    TERMINAL_STATUSES,
    BlobArea,
    Document,
    DocumentKind,
    DocumentLink,
    DocumentSource,
    DocumentStatus,
    LinkTarget,
    ReviewItem,
    ReviewItemState,
)
from app.domain.documents.schemas import (
    INGEST_JOB_TYPE,
    DocumentDetailOut,
    DocumentLinkIn,
    DocumentLinkOut,
    DocumentListItem,
    DocumentOut,
    DocumentPage,
    DocumentReceivedOut,
    DocumentReprocessOut,
    IngestDocumentPayload,
    SnippetPart,
)
from app.domain.transactions import service as transactions_service
from app.ingestion.intake.sniff import FileType
from app.ingestion.intake.upload import StoredUpload, record_stored, store_stream

log = get_logger(__name__)

#: Household setting (``setting`` row, scope ``household``): ``true`` lets
#: viewers download originals (SECURITY.md I1: off by default).
VIEWER_DOWNLOAD_SETTING: Final = "documents.viewer_download"

#: Link targets the Phase 2 API accepts (the others arrive with their phases).
SUPPORTED_LINK_TARGETS: Final = frozenset({LinkTarget.TRANSACTION, LinkTarget.ACCOUNT})

_EXTENSIONS: Final[dict[str, str]] = {
    FileType.PDF: ".pdf",
    FileType.PNG: ".png",
    FileType.JPEG: ".jpg",
    FileType.HEIC: ".heic",
    FileType.TIFF: ".tiff",
    FileType.ZIP: ".zip",
    FileType.XLSX: ".xlsx",
    FileType.OFX: ".ofx",
    FileType.QFX: ".qfx",
    FileType.QIF: ".qif",
    FileType.EML: ".eml",
    FileType.CSV: ".csv",
    FileType.TSV: ".tsv",
    FileType.TEXT: ".txt",
}


def blob_store(settings: Settings) -> BlobStore:
    """The configured blob store (the local filesystem store in Phase 2)."""
    return LocalFSBlobStore.from_settings(settings)


# ---------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------


def document_quarantined() -> ProblemError:
    return ProblemError(
        409,
        "document-quarantined",
        "Document not available",
        "This file has not been accepted by the import pipeline and cannot be downloaded.",
    )


def document_busy() -> ProblemError:
    return ProblemError(
        409,
        "document-busy",
        "Document is being processed",
        "The document is still being processed. Try again when it has finished.",
    )


def document_unreadable() -> ProblemError:
    return ProblemError(
        500, "document-unreadable", "Document unreadable", "The stored file could not be read."
    )


def link_target_unsupported(target: LinkTarget) -> ProblemError:
    return ProblemError(
        422,
        "link-target-unsupported",
        "Link target not supported yet",
        f"Documents cannot be linked to a {target.value} yet.",
    )


# ---------------------------------------------------------------------------
# Access rules
# ---------------------------------------------------------------------------


def _household(principal: Principal) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    return principal.household_id


def can_edit_document(db: Session, principal: Principal, document: Document) -> bool:
    """Member or above, and owner/admin, the uploader of an unattached file or an editor
    of its account (the caller can already see ``document``)."""
    if not role_at_least(principal.role, HouseholdRole.MEMBER):
        return False
    if sees_all_accounts(principal):
        return True
    if document.account_id is None:
        return document.uploaded_by == principal.user_id
    return can_edit_account(db, principal, document.account_id)


def viewer_download_allowed(db: Session, household_id: uuid.UUID) -> bool:
    value = db.execute(
        select(Setting.value).where(
            Setting.scope == SettingScope.HOUSEHOLD,
            Setting.scope_id == household_id,
            Setting.key == VIEWER_DOWNLOAD_SETTING,
        )
    ).scalar_one_or_none()
    return value is True


def may_download(db: Session, principal: Principal) -> bool:
    """I1: member or above; a viewer only where the household allows it."""
    if role_at_least(principal.role, HouseholdRole.MEMBER):
        return True
    household_id = principal.household_id
    return household_id is not None and viewer_download_allowed(db, household_id)


def get_editable_document(
    db: Session, principal: Principal, document_id: uuid.UUID, *, lock: bool = False
) -> Document:
    """A document the caller may change: ``404`` if hidden, ``403`` if read-only."""
    household_id = _household(principal)
    document = service.get_visible_document(db, principal, document_id)
    if not can_edit_document(db, principal, document):
        raise forbidden("You may not change this document.")
    if lock:
        locked = repo.get_document(db, household_id, document_id, lock=True)
        if locked is None:  # pragma: no cover - deleted between the statements
            raise service.document_not_found()
        return locked
    return document


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_documents(
    db: Session,
    principal: Principal,
    *,
    cursor: str | None,
    limit: int,
    kind: DocumentKind | None = None,
    status: DocumentStatus | None = None,
    account_id: uuid.UUID | None = None,
    tax_year: int | None = None,
    q: str | None = None,
) -> DocumentPage:
    """Visible documents, newest first (keyset on the UUIDv7 id).

    With ``q``: only documents whose extracted text matches
    (``websearch_to_tsquery``), best match first (keyset on score and id),
    each with its rank and a snippet. A caller who may not download (I1)
    searches the original filename only and gets no snippet.
    """
    household_id = _household(principal)
    query = select(Document).where(service.visible_document_filter(principal))
    if account_id is not None:
        get_visible_account(db, principal, account_id)
        query = query.where(Document.account_id == account_id)
    if kind is not None:
        query = query.where(Document.kind == kind)
    if status is not None:
        query = query.where(Document.status == status)
    if tax_year is not None:
        query = query.where(Document.tax_year == tax_year)
    q = q.strip() if q else None
    if not q:
        after = search.decode_cursor(cursor, ranked=False)[1] if cursor else None
        if after is not None:
            query = query.where(Document.id < after)
        rows = list(db.execute(query.order_by(Document.id.desc()).limit(limit + 1)).scalars())
        more = len(rows) > limit
        rows = rows[:limit]
        return DocumentPage(
            items=[DocumentListItem.model_validate(row) for row in rows],
            next_cursor=search.encode_cursor(None, rows[-1].id) if more and rows else None,
        )
    # I1: without the download right, q never touches the extracted text
    # (it would be a yes/no oracle over documents the caller may not open).
    full_text = may_download(db, principal)
    if full_text:
        score = search.score(q).label("score")
        ranked = query.add_columns(score).where(search.matches(q))
    else:
        score = search.metadata_score(q).label("score")
        ranked = query.add_columns(score).where(search.metadata_matches(q))
    if cursor:
        points, after_id = search.decode_cursor(cursor, ranked=True)
        ranked = ranked.where(tuple_(score, Document.id) < tuple_(points, after_id))
    found = list(db.execute(ranked.order_by(score.desc(), Document.id.desc()).limit(limit + 1)))
    more = len(found) > limit
    found = found[:limit]
    snippets: dict[uuid.UUID, list[SnippetPart]] | None = None
    if full_text:
        snippets = {
            row_id: [SnippetPart(**part) for part in parts]
            for row_id, parts in search.snippets(
                db, household_id, [row.id for row, _ in found], q
            ).items()
        }
    items = [
        DocumentListItem.model_validate(row).model_copy(
            update={
                "rank": points / 1_000_000,
                "snippet": snippets.get(row.id, []) if snippets is not None else None,
            }
        )
        for row, points in found
    ]
    last = found[-1] if found else None
    return DocumentPage(
        items=items,
        next_cursor=search.encode_cursor(last[1], last[0].id) if more and last else None,
    )


def get_document(db: Session, principal: Principal, document_id: uuid.UUID) -> DocumentDetailOut:
    """Metadata, links and open review items of one visible document."""
    household_id = _household(principal)
    document = service.get_visible_document(db, principal, document_id)
    links = db.execute(
        select(DocumentLink)
        .where(DocumentLink.household_id == household_id, DocumentLink.document_id == document.id)
        .order_by(DocumentLink.id)
    ).scalars()
    open_items = db.execute(
        select(func.count())
        .select_from(ReviewItem)
        .where(
            ReviewItem.household_id == household_id,
            ReviewItem.document_id == document.id,
            ReviewItem.state == ReviewItemState.OPEN,
        )
    ).scalar_one()
    blob = repo.get_blob(db, household_id, document.blob_id)
    quarantined = blob is None or blob.area is BlobArea.QUARANTINE
    base = DocumentOut.model_validate(document).model_dump()
    return DocumentDetailOut(
        **base,
        links=[DocumentLinkOut.model_validate(link) for link in links],
        open_review_items=int(open_items),
        quarantined=quarantined,
        downloadable=not quarantined and may_download(db, principal),
        can_edit=can_edit_document(db, principal, document),
    )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Download:
    """A decrypted document ready to stream (``chunks`` is already primed)."""

    filename: str
    media_type: str
    size: int
    chunks: Iterator[bytes]


def download_filename(document: Document) -> str:
    if document.original_filename:
        return document.original_filename
    return f"document-{document.id}{_EXTENSIONS.get(document.mime_detected, '')}"


def open_download(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    *,
    store: BlobStore,
    keyring: Keyring,
    request: HTTPConnection | None,
) -> Download:
    """Check access (module doc), audit the download and open the plaintext stream.

    The first chunk is decrypted here, so a missing file or a wrong key is a
    problem response instead of a broken stream; later chunks are
    authenticated as they are read (a tampered chunk aborts the transfer,
    which the client sees as a short body against ``Content-Length``).
    """
    household_id = _household(principal)
    document = service.get_visible_document(db, principal, document_id)
    if not may_download(db, principal):
        raise forbidden("Viewers cannot download original documents in this household.")
    blob = repo.get_blob(db, household_id, document.blob_id)
    if blob is None or blob.area is BlobArea.QUARANTINE:
        raise document_quarantined()
    chunks = service.open_plaintext(store, keyring, blob)
    try:
        first = next(chunks, b"")
    except (DecryptionError, BlobStoreError, OSError) as exc:
        log.error(
            "document download failed",
            extra={"document_id": str(document.id), "error_class": type(exc).__name__},
        )
        raise document_unreadable() from None
    audit(
        db,
        action="document.download",
        target_type="document",
        target_id=document.id,
        after={"size": blob.size, "mime_detected": document.mime_detected},
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    return Download(
        filename=download_filename(document),
        media_type=document.mime_detected,
        size=blob.size,
        chunks=itertools.chain((first,), chunks) if first else chunks,
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def check_upload_target(db: Session, principal: Principal, account_id: uuid.UUID | None) -> None:
    """Before any byte is stored: an ``account_id`` must be an account the caller edits."""
    _household(principal)
    if account_id is not None:
        get_visible_account(db, principal, account_id, need="edit")


def store_upload(
    *, store: BlobStore, keyring: Keyring, chunks: Iterator[bytes], max_bytes: int
) -> StoredUpload:
    """Sniff, hash and encrypt the upload into quarantine (no database work)."""
    return store_stream(store=store, keyring=keyring, chunks=chunks, max_bytes=max_bytes)


def record_upload(
    db: Session,
    principal: Principal,
    stored: StoredUpload,
    *,
    store: BlobStore,
    filename: str | None,
    account_id: uuid.UUID | None,
    idempotency_key: str | None,
    request: HTTPConnection | None,
) -> DocumentReceivedOut | JSONResponse:
    """Record a stored upload (``Idempotency-Key`` checked against its SHA-256).

    The content hash is part of the idempotency fingerprint, so the same key
    with another file is ``422 idempotency-key-reused``. A replay returns
    the first answer and drops the object just stored. On any error the
    object is deleted too (nothing is kept for a refused request).
    """
    household_id = _household(principal)
    check_upload_target(db, principal, account_id)
    payload: dict[str, Any] = {
        "sha256": stored.sha256,
        "size": stored.sealed.size,
        "account_id": str(account_id) if account_id else None,
        "filename": service.clean_filename(filename),
    }

    def run() -> DocumentReceivedOut:
        received = record_stored(
            db,
            stored,
            store=store,
            household_id=household_id,
            source=DocumentSource.API
            if principal.auth_method == "token"
            else DocumentSource.UPLOAD,
            source_meta={"auth": principal.auth_method},
            original_filename=filename,
            uploaded_by=principal.user_id,
            account_id=account_id,
            request=request,
        )
        return DocumentReceivedOut(
            id=received.document_id,
            status=received.status,
            duplicate_of=received.duplicate_of,
            job_id=received.job_id,
        )

    try:
        result = idempotent(
            db,
            principal,
            idempotency_key,
            scope="POST /documents",
            payload=payload,
            run=run,
            status_code=202,
        )
    except BaseException:
        store.delete(stored.sealed.area, stored.sealed.key)
        raise
    if isinstance(result, JSONResponse):
        store.delete(stored.sealed.area, stored.sealed.key)
    return result


# ---------------------------------------------------------------------------
# Reprocess, link, delete
# ---------------------------------------------------------------------------


def close_open_items(
    db: Session,
    household_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
    resolution: dict[str, Any],
) -> int:
    """Dismiss every open review item of a document (it is processed again)."""
    result = db.execute(
        update(ReviewItem)
        .where(
            ReviewItem.household_id == household_id,
            ReviewItem.document_id == document_id,
            ReviewItem.state == ReviewItemState.OPEN,
        )
        .values(
            state=ReviewItemState.DISMISSED,
            resolved_at=clock.utcnow(),
            resolved_by=user_id,
            resolution=resolution,
        )
        .execution_options(synchronize_session="fetch")
    )
    return int(getattr(result, "rowcount", 0) or 0)


def queue_ingestion(
    db: Session,
    document: Document,
    *,
    principal: Principal,
    action: str,
    request: HTTPConnection | None,
    facts: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Send a finished document through the pipeline again (status ``received``).

    Open review items of the document are dismissed (the pipeline raises
    fresh ones). Shared by ``POST /documents/{id}/reprocess`` and the review
    resolutions that need a new run.
    """
    if document.status not in TERMINAL_STATUSES:
        raise document_busy()
    household_id = document.household_id
    before = {"status": str(document.status), "error": document.error}
    closed = close_open_items(
        db,
        household_id,
        document.id,
        user_id=principal.user_id,
        resolution={"action": "superseded", "by": action},
    )
    document.status = DocumentStatus.RECEIVED
    document.error = None
    db.flush()
    job_id = enqueue(
        db,
        INGEST_JOB_TYPE,
        IngestDocumentPayload(document_id=document.id),
        household_id=household_id,
    )
    audit(
        db,
        action=action,
        target_type="document",
        target_id=document.id,
        before=before,
        after={
            "status": str(document.status),
            "job_id": str(job_id),
            "review_items_closed": closed,
            **(facts or {}),
        },
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(
        db,
        household_id,
        "document.updated",
        {"id": str(document.id), "status": str(document.status)},
    )
    emit(db, household_id, "job.queued", {"id": str(job_id), "type": INGEST_JOB_TYPE})
    return job_id


def reprocess(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    *,
    request: HTTPConnection | None,
) -> DocumentReprocessOut:
    """Run the pipeline again for a finished document (``409 document-busy`` otherwise)."""
    document = get_editable_document(db, principal, document_id, lock=True)
    job_id = queue_ingestion(
        db, document, principal=principal, action="document.reprocess", request=request
    )
    return DocumentReprocessOut(id=document.id, status=document.status, job_id=job_id)


def link(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    data: DocumentLinkIn,
    *,
    request: HTTPConnection | None,
) -> tuple[DocumentLinkOut, bool]:
    """Attach a document to a transaction or account; ``(link, created)``.

    The caller must be able to change the document and edit the target
    (a hidden or foreign target is its ``404``). Linking twice returns the
    existing link.
    """
    household_id = _household(principal)
    document = get_editable_document(db, principal, document_id)
    if data.target_type not in SUPPORTED_LINK_TARGETS:
        raise link_target_unsupported(data.target_type)
    if data.target_type is LinkTarget.TRANSACTION:
        target = transactions_service.get_visible_transaction(
            db, principal, data.target_id, need="edit"
        )
        account_id = target.account_id
    else:
        account_id = get_visible_account(db, principal, data.target_id, need="edit").id
    existing = db.execute(
        select(DocumentLink).where(
            DocumentLink.household_id == household_id,
            DocumentLink.document_id == document.id,
            DocumentLink.target_type == data.target_type,
            DocumentLink.target_id == data.target_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return DocumentLinkOut.model_validate(existing), False
    row = DocumentLink(
        household_id=household_id,
        document_id=document.id,
        target_type=data.target_type,
        target_id=data.target_id,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        action="document.link",
        target_type="document",
        target_id=document.id,
        after={"target_type": str(data.target_type), "target_id": str(data.target_id)},
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(
        db,
        household_id,
        "document.linked",
        {"id": str(document.id), "link_id": str(row.id), "account_id": str(account_id)},
    )
    return DocumentLinkOut.model_validate(row), True


def delete(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    *,
    store: BlobStore,
    request: HTTPConnection | None,
) -> None:
    """Crypto-shred and delete a document (``service.delete_document``, G5)."""
    household_id = _household(principal)
    document = get_editable_document(db, principal, document_id)
    service.delete_document(
        db,
        store,
        household_id=household_id,
        document_id=document.id,
        user_id=principal.user_id,
        request=request,
    )

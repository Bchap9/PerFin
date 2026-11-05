# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document store operations (DATA_MODEL.md "Documents and ingestion").

* :func:`record_received`: after the intake layer has streamed a file into
  the quarantine area (``app.ingestion.intake.upload``), insert the ``blob``
  and ``document`` rows and enqueue ``ingest_document`` **in the caller's
  transaction** (the job exists exactly when the document does). A file whose
  SHA-256 is already in the household does not go through the pipeline: it is
  ``needs_review`` with a ``possible_duplicate`` review item.
* :func:`open_plaintext`: the decrypted content as a stream (the worker feeds
  it to the sandbox; the download route will stream it as an attachment).
* :func:`promote_blob`: quarantine -> permanent without re-encryption; the
  quarantine copy is removed only after the database change commits (and the
  permanent copy is removed again if it rolls back).
* :func:`delete_document`: crypto-shredding (G5) -- the wrapped DEK is
  destroyed and the rows deleted in the transaction; the files are unlinked
  after commit. A file that survives anywhere is ciphertext without a key.
* :func:`get_visible_document`: owners and admins see every document of the
  household; members and viewers see documents of accounts they may view and
  their own uploads that are not attached to an account. Anything else is
  ``404 document-not-found``, like another household's document (F3).

Every write is audited (``document.*``) and emits an event so the SPA
refreshes. Audit rows and events carry ids, sizes, types and statuses, never
file names or content.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import ColumnElement, and_, delete, event, or_, select, true
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.principal import Principal
from app.core.audit import audit
from app.core.crypto import Keyring, WrappedKey
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.logging import get_logger
from app.db.queue import enqueue
from app.domain.accounts.visibility import sees_all_accounts, visible_account_filter
from app.domain.documents import repo
from app.domain.documents.blobstore import BlobStore, SealedBlob, decrypt_stream
from app.domain.documents.models import (
    Blob,
    BlobArea,
    Document,
    DocumentSource,
    DocumentStatus,
    ReviewKind,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE, IngestDocumentPayload

log = get_logger(__name__)

FILENAME_MAX = 255


def document_not_found() -> ProblemError:
    return ProblemError(404, "document-not-found", "Document not found")


@dataclass(frozen=True)
class ReceivedDocument:
    document_id: uuid.UUID
    status: DocumentStatus
    duplicate_of: uuid.UUID | None
    job_id: uuid.UUID | None


def clean_filename(name: str | None) -> str | None:
    """The last path component, without control characters, at most 255 chars.

    Metadata only (SECURITY.md C3): never used to build a path.
    """
    if not name:
        return None
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    return base[:FILENAME_MAX] or None


def _after_outcome(
    session: Session, *, on_commit: Callable[[], None], on_rollback: Callable[[], None]
) -> None:
    """Run exactly one of the callbacks when the session's transaction ends."""
    done = [False]

    def committed(_: Session) -> None:
        if not done[0]:
            done[0] = True
            _safely(on_commit)

    def rolled_back(_: Session) -> None:
        if not done[0]:
            done[0] = True
            _safely(on_rollback)

    event.listen(session, "after_commit", committed, once=True)
    event.listen(session, "after_rollback", rolled_back, once=True)


def _safely(callback: Callable[[], None]) -> None:
    try:
        callback()
    except OSError as exc:
        # A left-over file is ciphertext; its key is in the DB (or shredded).
        log.warning("blob file cleanup failed", extra={"error_class": type(exc).__name__})


def _event(db: Session, household_id: uuid.UUID, type_: str, document: Document) -> None:
    emit(db, household_id, type_, {"id": str(document.id), "status": str(document.status)})


def record_received(
    db: Session,
    *,
    household_id: uuid.UUID,
    sealed: SealedBlob,
    sha256: str,
    mime_detected: str,
    source: DocumentSource,
    source_meta: dict[str, Any] | None = None,
    original_filename: str | None = None,
    uploaded_by: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    request: HTTPConnection | None = None,
    hold: tuple[ReviewKind, dict[str, Any]] | None = None,
) -> ReceivedDocument:
    """Insert blob and document rows for a quarantined file and enqueue its ingestion.

    With ``hold`` (a review kind and payload) the document waits in
    ``needs_review`` behind that item instead of being enqueued; the item's
    resolution sends it through the pipeline (drop-folder ``unknown_owner``).
    """
    blob = Blob(
        id=sealed.blob_id,
        household_id=household_id,
        store=sealed.store,
        key=sealed.key,
        area=sealed.area,
        size=sealed.size,
        stored_size=sealed.stored_size,
        enc_alg=sealed.enc_alg,
        key_version=sealed.wrapped.key_version,
        wrap_nonce=sealed.wrapped.wrap_nonce,
        wrapped_dek=sealed.wrapped.wrapped_dek,
        nonce=sealed.header,
    )
    db.add(blob)
    db.flush()
    duplicate_of = repo.find_duplicate(db, household_id, sha256)
    document = Document(
        household_id=household_id,
        blob_id=blob.id,
        status=(
            DocumentStatus.NEEDS_REVIEW
            if duplicate_of is not None or hold is not None
            else DocumentStatus.RECEIVED
        ),
        source=source,
        source_meta=dict(source_meta or {}),
        sha256=sha256,
        size=sealed.size,
        mime_detected=mime_detected,
        original_filename=clean_filename(original_filename),
        uploaded_by=uploaded_by,
        account_id=account_id,
    )
    db.add(document)
    db.flush()
    job_id: uuid.UUID | None = None
    if hold is not None:
        repo.add_review_item(db, household_id, hold[0], hold[1], document_id=document.id)
    if duplicate_of is not None:
        repo.add_review_item(
            db,
            household_id,
            ReviewKind.POSSIBLE_DUPLICATE,
            {"reason": "same_content", "duplicate_of": str(duplicate_of)},
            document_id=document.id,
        )
    elif hold is None:
        job_id = enqueue(
            db,
            INGEST_JOB_TYPE,
            IngestDocumentPayload(document_id=document.id),
            household_id=household_id,
        )
    audit(
        db,
        action="document.receive",
        target_type="document",
        target_id=document.id,
        after={
            "source": str(source),
            "size": sealed.size,
            "mime_detected": mime_detected,
            "sha256": sha256,
            "status": str(document.status),
            "account_id": account_id,
            "duplicate_of": duplicate_of,
        },
        household_id=household_id,
        user_id=uploaded_by,
        request=request,
    )
    _event(db, household_id, "document.created", document)
    return ReceivedDocument(document.id, document.status, duplicate_of, job_id)


def wrapped_key(blob: Blob) -> WrappedKey | None:
    if blob.wrapped_dek is None or blob.wrap_nonce is None:
        return None
    return WrappedKey(blob.key_version, blob.wrap_nonce, blob.wrapped_dek)


def open_plaintext(store: BlobStore, keyring: Keyring, blob: Blob) -> Iterator[bytes]:
    """The decrypted, authenticated content of ``blob`` as a chunk iterator."""
    return decrypt_stream(
        store,
        keyring,
        blob_id=blob.id,
        area=blob.area,
        key=blob.key,
        header=blob.nonce,
        wrapped=wrapped_key(blob),
        size=blob.size,
    )


def promote_blob(db: Session, store: BlobStore, blob: Blob) -> None:
    """Move ``blob`` from quarantine to permanent (ciphertext unchanged)."""
    if blob.area is BlobArea.PERMANENT:
        return
    key = blob.key
    store.copy(key, BlobArea.QUARANTINE, BlobArea.PERMANENT)
    blob.area = BlobArea.PERMANENT
    db.flush()
    _after_outcome(
        db,
        on_commit=lambda: _discard(store, BlobArea.QUARANTINE, key),
        on_rollback=lambda: _discard(store, BlobArea.PERMANENT, key),
    )


def _discard(store: BlobStore, area: BlobArea, key: str) -> None:
    store.delete(area, key)


def delete_document(
    db: Session,
    store: BlobStore,
    *,
    household_id: uuid.UUID,
    document_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    request: HTTPConnection | None = None,
) -> None:
    """Delete a document, its dependent rows and its blob (crypto-shredding, G5)."""
    document = repo.get_document(db, household_id, document_id, lock=True)
    if document is None:
        raise document_not_found()
    blob = repo.get_blob(db, household_id, document.blob_id)
    blob_id = document.blob_id
    before = {"status": str(document.status), "size": document.size, "sha256": document.sha256}
    # Who may hear about the deletion once the row is gone (events.deliverable).
    deleted_payload = {
        "id": str(document_id),
        "account_id": str(document.account_id) if document.account_id else None,
        "uploaded_by": str(document.uploaded_by) if document.uploaded_by else None,
    }
    # 1. Destroy the key first: from here on the ciphertext is unreadable.
    repo.shred_blob_key(db, household_id, blob_id)
    # Page previews are blobs of their own (app.domain.documents.previews).
    from app.domain.documents.previews import discard_previews

    discard_previews(db, store, household_id=household_id, extraction=document.extraction)
    # 2. Rows: links, review items and ingest history cascade with the document.
    db.execute(
        delete(Document).where(Document.household_id == household_id, Document.id == document_id)
    )
    db.execute(delete(Blob).where(Blob.household_id == household_id, Blob.id == blob_id))
    audit(
        db,
        action="document.delete",
        target_type="document",
        target_id=document_id,
        before=before,
        household_id=household_id,
        user_id=user_id,
        request=request,
    )
    emit(db, household_id, "document.deleted", deleted_payload)
    # 3. Files go once the deletion is committed.
    if blob is not None:
        key = blob.key

        def unlink() -> None:
            for area in (BlobArea.QUARANTINE, BlobArea.PERMANENT):
                store.delete(area, key)

        _after_outcome(db, on_commit=unlink, on_rollback=lambda: None)


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


def visible_document_filter(principal: Principal) -> ColumnElement[bool]:
    """SQL predicate: documents of the active household the principal may see."""
    if principal.household_id is None:
        return and_(Document.id.is_(None), Document.id.is_not(None))
    in_household = Document.household_id == principal.household_id
    account_column: Any = Document.account_id
    if sees_all_accounts(principal):
        return and_(in_household, true())
    return and_(
        in_household,
        or_(
            and_(
                Document.account_id.is_not(None),
                visible_account_filter(principal, cast(ColumnElement[uuid.UUID], account_column)),
            ),
            and_(Document.account_id.is_(None), Document.uploaded_by == principal.user_id),
        ),
    )


def get_visible_document(db: Session, principal: Principal, document_id: uuid.UUID) -> Document:
    """The document, or ``404 document-not-found`` (hidden, foreign or missing alike)."""
    document = db.execute(
        select(Document).where(Document.id == document_id, visible_document_filter(principal))
    ).scalar_one_or_none()
    if document is None:
        raise document_not_found()
    return document

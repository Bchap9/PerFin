# SPDX-License-Identifier: AGPL-3.0-or-later
"""Receiving a file into the document store (INGESTION.md "Receive", ADR-0006).

This runs in the **API** process (and for the drop folder and mail intake in
the worker). It never parses content (SECURITY.md C1): it

1. reads the stream chunk by chunk, never holding the whole file, and stops
   at ``max_bytes`` (``413 upload-too-large``; the reverse proxy and
   ``app.core.limits`` cap the request body too);
2. sniffs the magic bytes of the first :data:`~app.ingestion.intake.sniff.SNIFF_BYTES`
   (``app.ingestion.intake.sniff``); a disallowed type, SVG/HTML and
   polyglots are ``415 unsupported-file-type`` before a byte is stored (C4,
   C5); for text types the rest of the stream is scanned for active markup
   too, and a match there is the same ``415`` (the partial object is
   discarded);
3. computes SHA-256 over the stream;
4. encrypts each chunk into a new object in the **quarantine** area under a
   random key (``app.domain.documents.blobstore``: per-object DEK, the blob id
   as associated data; C3, G1);
5. inserts ``blob`` + ``document(status=received)`` and enqueues
   ``ingest_document`` in the caller's transaction
   (:func:`app.domain.documents.service.record_received`).

If anything fails after the object was written, the object is deleted (and
an object orphaned by a later rollback holds a key that was never stored, so
it is unreadable).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO, Final

from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.authz import forbidden
from app.auth.principal import Principal
from app.core.crypto import Keyring
from app.core.errors import ProblemError
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.domain.accounts.visibility import get_visible_account
from app.domain.documents import service
from app.domain.documents.blobstore import BlobStore, EncryptingWriter, SealedBlob
from app.domain.documents.models import DocumentSource, ReviewKind
from app.ingestion.intake.sniff import (
    SNIFF_BYTES,
    TEXT_TYPES,
    FileType,
    MarkupScanner,
    Rejected,
    RejectReason,
    sniff,
)

log = get_logger(__name__)

READ_CHUNK: Final = 64 * 1024

_REJECT_DETAIL: Final = {
    RejectReason.EMPTY: "The file is empty.",
    RejectReason.MARKUP: "HTML, SVG and other markup files are not accepted.",
    RejectReason.POLYGLOT: "The file contains active markup and is not accepted.",
    RejectReason.BINARY_UNKNOWN: "This file type is not supported.",
    RejectReason.TEXT_ENCODING: "Text must be UTF-8 or a single-byte encoding.",
    RejectReason.XML_NOT_OFX: "XML files other than OFX are not accepted.",
}


class UploadRejectedError(ProblemError):
    def __init__(self, reason: RejectReason) -> None:
        super().__init__(
            415,
            "unsupported-file-type",
            "Unsupported file type",
            _REJECT_DETAIL[reason],
            extra={"reason": str(reason)},
        )
        self.reason = reason


class UploadTooLargeError(ProblemError):
    def __init__(self, max_bytes: int) -> None:
        super().__init__(
            413,
            "upload-too-large",
            "File too large",
            f"Files may be at most {max_bytes // (1024 * 1024) or 1} MB.",
        )


def iter_file(handle: BinaryIO, chunk_size: int = READ_CHUNK) -> Iterator[bytes]:
    """Chunks of a binary file object (an ``UploadFile.file``, a drop-folder file)."""
    while chunk := handle.read(chunk_size):
        yield chunk


def _read_head(chunks: Iterator[bytes], max_bytes: int) -> bytes:
    head = bytearray()
    for chunk in chunks:
        head += chunk
        if len(head) > max_bytes:
            raise UploadTooLargeError(max_bytes)
        if len(head) >= SNIFF_BYTES:
            break
    return bytes(head)


@dataclass(frozen=True)
class StoredUpload:
    """A file sniffed, hashed and encrypted into the quarantine area (not yet recorded)."""

    sealed: SealedBlob
    sha256: str
    file_type: FileType


def store_stream(
    *,
    store: BlobStore,
    keyring: Keyring,
    chunks: Iterable[bytes],
    max_bytes: int,
) -> StoredUpload:
    """Steps 1-4 of the module doc: sniff, hash and encrypt into quarantine.

    Nothing is written to the database. The caller records the object with
    :func:`record_stored` or deletes it (``store.delete(sealed.area,
    sealed.key)``); the upload route uses the digest to check an
    ``Idempotency-Key`` before recording.
    """
    stream = iter(chunks)
    head = _read_head(stream, max_bytes)
    verdict = sniff(head)
    if isinstance(verdict, Rejected):
        log.info("upload rejected", extra={"reason": str(verdict.reason)})
        raise UploadRejectedError(verdict.reason)
    file_type: FileType = verdict
    digest = hashlib.sha256()
    # Text types: active markup anywhere in the file makes it a polyglot (C4).
    scanner = MarkupScanner() if file_type in TEXT_TYPES else None
    # The head may be longer than the sniff window (the first chunk is kept
    # whole): markup past the window but inside it is a polyglot as well.
    if scanner is not None and scanner.feed(head):
        log.info("upload rejected", extra={"reason": str(RejectReason.POLYGLOT)})
        raise UploadRejectedError(RejectReason.POLYGLOT)
    with EncryptingWriter(store, keyring, uuid7()) as writer:
        digest.update(head)
        writer.write(head)
        for chunk in stream:
            if writer.size + len(chunk) > max_bytes:
                raise UploadTooLargeError(max_bytes)
            if scanner is not None and scanner.feed(chunk):
                log.info("upload rejected", extra={"reason": str(RejectReason.POLYGLOT)})
                raise UploadRejectedError(RejectReason.POLYGLOT)
            digest.update(chunk)
            writer.write(chunk)
        sealed = writer.finish()
    return StoredUpload(sealed, digest.hexdigest(), file_type)


def record_stored(
    db: Session,
    stored: StoredUpload,
    *,
    store: BlobStore,
    household_id: uuid.UUID,
    source: DocumentSource,
    source_meta: dict[str, Any] | None = None,
    original_filename: str | None = None,
    uploaded_by: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    request: HTTPConnection | None = None,
    hold: tuple[ReviewKind, dict[str, Any]] | None = None,
) -> service.ReceivedDocument:
    """Step 5: record a stored object (the object is deleted if recording fails).

    ``hold``: see :func:`app.domain.documents.service.record_received`.
    """
    try:
        return service.record_received(
            db,
            household_id=household_id,
            sealed=stored.sealed,
            sha256=stored.sha256,
            mime_detected=str(stored.file_type),
            source=source,
            source_meta=source_meta,
            original_filename=original_filename,
            uploaded_by=uploaded_by,
            account_id=account_id,
            request=request,
            hold=hold,
        )
    except BaseException:
        store.delete(stored.sealed.area, stored.sealed.key)
        raise


def receive_stream(
    db: Session,
    *,
    store: BlobStore,
    keyring: Keyring,
    household_id: uuid.UUID,
    chunks: Iterable[bytes],
    max_bytes: int,
    source: DocumentSource,
    source_meta: dict[str, Any] | None = None,
    original_filename: str | None = None,
    uploaded_by: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    request: HTTPConnection | None = None,
) -> service.ReceivedDocument:
    """Sniff, hash, encrypt into quarantine and record a received file (module doc)."""
    stored = store_stream(store=store, keyring=keyring, chunks=chunks, max_bytes=max_bytes)
    return record_stored(
        db,
        stored,
        store=store,
        household_id=household_id,
        source=source,
        source_meta=source_meta,
        original_filename=original_filename,
        uploaded_by=uploaded_by,
        account_id=account_id,
        request=request,
    )


def receive_upload(
    db: Session,
    principal: Principal,
    *,
    store: BlobStore,
    keyring: Keyring,
    chunks: Iterable[bytes],
    max_bytes: int,
    filename: str | None = None,
    account_id: uuid.UUID | None = None,
    request: HTTPConnection | None = None,
) -> service.ReceivedDocument:
    """An upload by ``principal`` (browser or token) into its active household.

    The route guard decides who may upload; this checks that an
    ``account_id``, if given, is an account the principal may edit
    (``404 account-not-found`` otherwise).
    """
    if principal.household_id is None:
        raise forbidden("You are not a member of a household.")
    if account_id is not None:
        get_visible_account(db, principal, account_id, need="edit")
    return receive_stream(
        db,
        store=store,
        keyring=keyring,
        household_id=principal.household_id,
        chunks=chunks,
        max_bytes=max_bytes,
        source=DocumentSource.API if principal.auth_method == "token" else DocumentSource.UPLOAD,
        source_meta={"auth": principal.auth_method},
        original_filename=filename,
        uploaded_by=principal.user_id,
        account_id=account_id,
        request=request,
    )

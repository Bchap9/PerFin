# SPDX-License-Identifier: AGPL-3.0-or-later
"""Page previews: rendered PNGs kept as encrypted blobs (INGESTION.md "Review queue", C5).

The sandbox child renders each page (bounded DPI and pixels, no metadata
chunks: :class:`app.ingestion.sandbox.protocol.Preview`); the worker stores
every preview as its own ``blob`` in the **permanent** area, encrypted like
any document (per-object data key wrapped by the master key, the blob id as
associated data; ADR-0003), and records where in the document's stored
extraction (``document.extraction.stored_previews``: page, blob id, size,
DPI and the page-unit-to-pixel ``scale`` the review UI needs to highlight a
word's box). The extraction itself never holds the image bytes.

* :func:`store_previews` writes them (files are removed again if the
  transaction rolls back);
* :func:`discard_previews` shreds their keys and deletes the rows, the files
  go after commit (re-extraction and document deletion, G5);
* :func:`read_preview` decrypts one page for the API (``GET
  /documents/{id}/preview?page=``), refusing anything larger than a preview
  may be.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.crypto import Keyring
from app.core.ids import uuid7
from app.domain.documents import repo
from app.domain.documents.blobstore import (
    BlobStore,
    EncryptingWriter,
    SealedBlob,
    decrypt_to_bytes,
)
from app.domain.documents.models import Blob, BlobArea, Document
from app.domain.documents.service import _after_outcome, open_plaintext
from app.ingestion.sandbox.protocol import MAX_PREVIEW_PNG_BYTES, Preview, StoredPreview


def _blob_row(household_id: uuid.UUID, sealed: SealedBlob) -> Blob:
    return Blob(
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


def store_previews(
    db: Session,
    store: BlobStore,
    keyring: Keyring,
    *,
    household_id: uuid.UUID,
    previews: Sequence[Preview],
) -> list[StoredPreview]:
    """Encrypt each preview into a new permanent blob; return the references."""
    stored: list[StoredPreview] = []
    keys: list[str] = []
    try:
        for preview in previews:
            with EncryptingWriter(store, keyring, uuid7(), area=BlobArea.PERMANENT) as writer:
                writer.write(preview.png)
                sealed = writer.finish()
            keys.append(sealed.key)
            db.add(_blob_row(household_id, sealed))
            stored.append(
                StoredPreview(
                    page=preview.page,
                    blob_id=sealed.blob_id,
                    width=preview.width,
                    height=preview.height,
                    dpi=preview.dpi,
                    scale=preview.scale,
                )
            )
        db.flush()
    except BaseException:
        for key in keys:
            store.delete(BlobArea.PERMANENT, key)
        raise

    def remove() -> None:
        for key in keys:
            store.delete(BlobArea.PERMANENT, key)

    _after_outcome(db, on_commit=lambda: None, on_rollback=remove)
    return stored


def stored_previews(extraction: dict[str, Any] | None) -> list[StoredPreview]:
    """The preview references of a stored extraction (none if it has none or is invalid)."""
    out: list[StoredPreview] = []
    for item in (extraction or {}).get("stored_previews") or []:
        try:
            out.append(StoredPreview.model_validate(item))
        except ValidationError:
            continue
    return out


def discard_previews(
    db: Session, store: BlobStore, *, household_id: uuid.UUID, extraction: dict[str, Any] | None
) -> int:
    """Shred and delete the preview blobs of ``extraction``; return how many."""
    refs = stored_previews(extraction)
    keys: list[str] = []
    for ref in refs:
        blob = repo.get_blob(db, household_id, ref.blob_id)
        if blob is None:
            continue
        keys.append(blob.key)
        repo.shred_blob_key(db, household_id, ref.blob_id)
        db.execute(delete(Blob).where(Blob.household_id == household_id, Blob.id == ref.blob_id))
    if keys:

        def unlink() -> None:
            for key in keys:
                store.delete(BlobArea.PERMANENT, key)

        _after_outcome(db, on_commit=unlink, on_rollback=lambda: None)
    return len(keys)


def read_preview(
    db: Session, store: BlobStore, keyring: Keyring, document: Document, page: int
) -> tuple[StoredPreview, bytes] | None:
    """The decrypted PNG of ``page`` of ``document`` and its reference, or None."""
    ref = next((r for r in stored_previews(document.extraction) if r.page == page), None)
    if ref is None:
        return None
    blob = repo.get_blob(db, document.household_id, ref.blob_id)
    if blob is None:
        return None
    data = decrypt_to_bytes(open_plaintext(store, keyring, blob), limit=MAX_PREVIEW_PNG_BYTES)
    return ref, data

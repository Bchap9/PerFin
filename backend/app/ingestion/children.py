# SPDX-License-Identifier: AGPL-3.0-or-later
"""Documents found inside an EML or ZIP go through the normal intake (SECURITY.md D4, C3).

The sandbox child hands back attachments, inline ``cid:`` images and ZIP
members as :class:`app.ingestion.sandbox.protocol.ChildDocument` bytes. The
worker receives each one exactly like an upload
(:func:`app.ingestion.intake.upload.receive_stream`): the magic-byte sniff
(SVG, HTML and polyglots refused), the upload size cap, SHA-256, encryption
into quarantine and its own ``ingest_document`` job. The name is metadata
only (``original_filename``, sanitised again by the intake; never a path).

``source_meta`` links the child to its container (``parent_document_id``,
``origin``, ``content_id``) and records its ``depth``; the pipeline extracts
a child with ``ExtractOptions.depth`` set from it, so a container inside a
container is not expanded (depth limit 1, D4). A child the intake refuses is
recorded in the container's stored extraction with the reason and does not
become a document.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from sqlalchemy.orm import Session

from app.core.crypto import Keyring
from app.core.logging import get_logger
from app.domain.documents.blobstore import BlobStore
from app.domain.documents.models import Document
from app.ingestion.intake.upload import UploadRejectedError, UploadTooLargeError, receive_stream
from app.ingestion.sandbox.protocol import ChildDocument, StoredChild

log = get_logger(__name__)

#: Deepest ``depth`` the pipeline accepts from ``source_meta``.
MAX_DEPTH: Final = 8


def document_depth(document: Document) -> int:
    """The nesting depth recorded for ``document`` (0 for an upload)."""
    raw = (document.source_meta or {}).get("depth", 0)
    try:
        depth = int(raw)
    except (TypeError, ValueError):
        return MAX_DEPTH
    return min(max(depth, 0), MAX_DEPTH)


def receive_children(
    db: Session,
    *,
    store: BlobStore,
    keyring: Keyring,
    parent: Document,
    children: Sequence[ChildDocument],
    max_bytes: int,
) -> list[StoredChild]:
    """Receive every child of ``parent`` as a new document (module doc)."""
    stored: list[StoredChild] = []
    for child in children:
        meta = {
            "parent_document_id": str(parent.id),
            "origin": child.origin,
            "depth": min(child.depth, MAX_DEPTH),
        }
        if child.content_id:
            meta["content_id"] = child.content_id[:200]
        try:
            received = receive_stream(
                db,
                store=store,
                keyring=keyring,
                household_id=parent.household_id,
                chunks=(child.data,),
                max_bytes=max_bytes,
                source=parent.source,
                source_meta=meta,
                original_filename=child.name,
                uploaded_by=parent.uploaded_by,
            )
        except UploadRejectedError as exc:
            stored.append(
                StoredChild(
                    origin=child.origin, name=child.name, size=child.size, rejected=str(exc.reason)
                )
            )
            continue
        except UploadTooLargeError:
            stored.append(
                StoredChild(
                    origin=child.origin, name=child.name, size=child.size, rejected="too_large"
                )
            )
            continue
        stored.append(
            StoredChild(
                origin=child.origin,
                name=child.name,
                size=child.size,
                document_id=received.document_id,
            )
        )
    log.info(
        "container expanded",
        extra={
            "document_id": str(parent.id),
            "children": sum(1 for s in stored if s.document_id),
            "rejected": sum(1 for s in stored if s.rejected),
        },
    )
    return stored

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document schemas: the ingest job payload and read models."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from app.domain.documents.models import DocumentKind, DocumentSource, DocumentStatus, LinkTarget

#: Job type of the ingestion pipeline (``app.workers.handlers.ingest``).
INGEST_JOB_TYPE = "ingest_document"


class IngestDocumentPayload(BaseModel):
    """Payload of ``ingest_document``: the document id only, never content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: uuid.UUID


class DocumentOut(BaseModel):
    """A document as the API shows it (no blob location or key material)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: DocumentKind
    status: DocumentStatus
    source: DocumentSource
    size: int
    mime_detected: str
    sha256: str
    original_filename: str | None
    uploaded_by: uuid.UUID | None
    account_id: uuid.UUID | None
    institution_id: uuid.UUID | None
    period_start: date | None
    period_end: date | None
    tax_year: int | None
    parser_version: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Documents API (``app.api.routers.documents``, ``app.domain.documents.manage``)
# ---------------------------------------------------------------------------

#: Page size of ``GET /documents`` (API.md: at most 500).
DOCUMENTS_MAX_PAGE = 500
DOCUMENTS_DEFAULT_PAGE = 100


class SnippetPart(BaseModel):
    """A piece of a search snippet: plain text, ``match`` for a highlighted term.

    Never markup: clients render ``text`` as text (``app.domain.documents.search``).
    """

    text: str
    match: bool


class DocumentListItem(DocumentOut):
    """A row of ``GET /documents``; with ``q``: the rank and, for callers who
    may download documents (I1), a highlighted snippet of the extracted text."""

    rank: float | None = None
    snippet: list[SnippetPart] | None = None


class DocumentPage(BaseModel):
    """``GET /documents``: newest first, or best match first with ``q``.

    ``next_cursor`` is opaque and ``null`` on the last page.
    """

    items: list[DocumentListItem]
    next_cursor: str | None


class DocumentLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    target_type: LinkTarget
    target_id: uuid.UUID
    created_at: datetime


class DocumentDetailOut(DocumentOut):
    """``GET /documents/{id}``: metadata only (never the file or extracted text).

    ``downloadable`` says whether ``GET /documents/{id}/download`` would
    serve the caller (not quarantined, and member or above, or a viewer in a
    household that allows viewer downloads); ``can_edit`` whether the caller
    may reprocess, link or delete it.
    """

    links: list[DocumentLinkOut]
    open_review_items: int
    quarantined: bool
    downloadable: bool
    can_edit: bool


class DocumentReceivedOut(BaseModel):
    """``POST /documents`` (``202``): the file is stored; ingestion runs in the worker.

    ``job_id`` is the ``ingest_document`` job (``GET /jobs/{id}``); it is
    ``null`` when the content is already in the household (``duplicate_of``)
    and the document waits in the review queue instead.
    """

    id: uuid.UUID
    status: DocumentStatus
    duplicate_of: uuid.UUID | None
    job_id: uuid.UUID | None


class DocumentReprocessOut(BaseModel):
    """``POST /documents/{id}/reprocess`` (``202``)."""

    id: uuid.UUID
    status: DocumentStatus
    job_id: uuid.UUID


class DocumentLinkIn(BaseModel):
    """``POST /documents/{id}/link``: attach the document to a transaction or account.

    Investment transactions, loan payments and tax years arrive with their
    phases (``422 link-target-unsupported`` until then).
    """

    model_config = ConfigDict(extra="forbid")

    target_type: LinkTarget
    target_id: uuid.UUID

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Documents (API.md ``/documents``; rules in ``app.domain.documents.manage``).

* ``POST /documents?account_id=``: ``multipart/form-data`` with one part
  named ``file``. The body is streamed (never spooled to disk): sniffed,
  hashed and encrypted into the quarantine area as it arrives
  (``app.ingestion.intake``), then recorded with an ``ingest_document`` job.
  ``202`` with the document id and job id. Member or above; an
  ``account_id`` must be an account the caller edits. ``Idempotency-Key``
  is honoured (the content hash is part of the request fingerprint). The
  body is capped at ``PERFIN_UPLOAD_MAX_MB`` for this path only
  (``app.core.limits``; ``413``), and so is the file (``413
  upload-too-large``); disallowed types are ``415``.
* ``GET /documents?cursor=&limit=&kind=&status=&account=&tax_year=``:
  visible documents, newest first. Full-text ``q`` arrives in Phase 4.
* ``GET /documents/{id}``: metadata, links and open review items; never the
  file or extracted text.
* ``GET /documents/{id}/download``: member or above, or a viewer where the
  household allows it (SECURITY.md I1); never a quarantined file (``409
  document-quarantined``). Decrypted as it streams, sent as an attachment
  with the sniffed type, ``nosniff`` and a ``sandbox`` CSP (C4).
* ``POST /documents/{id}/reprocess`` (``202``), ``POST
  /documents/{id}/link``, ``DELETE /documents/{id}`` (crypto-shredding, G5):
  member or above who can change the document.

Tokens need ``documents:read`` / ``documents:write``. ``GET
/documents/{id}/preview`` is Phase 3.

The upload and the download end the request's database transaction before
the (possibly slow) transfer, so no connection or row lock is held while
bytes move; the upload records the document in a fresh transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

import anyio.from_thread
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, sessionmaker

from app.api.routers.accounts import PROBLEMS, scoped_role
from app.auth.deps import DbSession, get_app_settings, get_keyring
from app.auth.models import HouseholdRole
from app.auth.principal import Principal, apply_rls_context
from app.core.headers import attachment_headers
from app.core.idempotency import IdempotencyKeyHeader, idempotent, payload_of
from app.domain.documents import manage
from app.domain.documents.models import DocumentKind, DocumentStatus
from app.domain.documents.schemas import (
    DOCUMENTS_DEFAULT_PAGE,
    DOCUMENTS_MAX_PAGE,
    DocumentDetailOut,
    DocumentLinkIn,
    DocumentLinkOut,
    DocumentPage,
    DocumentReceivedOut,
    DocumentReprocessOut,
)
from app.ingestion.intake.multipart import MultipartFile, boundary_of

router = APIRouter(tags=["documents"])

Reader = Annotated[Principal, Depends(scoped_role("documents:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("documents:write", HouseholdRole.MEMBER))]

UPLOAD_PROBLEMS: dict[int | str, dict[str, Any]] = {
    **PROBLEMS,
    **{
        code: {"description": "Problem details", "content": {"application/problem+json": {}}}
        for code in (413, 415)
    },
}

_UPLOAD_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {"file": {"type": "string", "format": "binary"}},
                    "additionalProperties": False,
                }
            }
        },
    }
}


def _request_body(request: Request) -> Iterator[bytes]:
    """The raw request body, pulled chunk by chunk from the event loop (sync handler)."""
    stream = request.stream()

    async def pull() -> bytes | None:
        try:
            return await anext(stream)
        except StopAsyncIteration:
            return None

    while (chunk := anyio.from_thread.run(pull)) is not None:
        yield chunk


@contextmanager
def _transaction(request: Request, principal: Principal) -> Iterator[Session]:
    """A new transaction in the caller's RLS context (after the request's was ended)."""
    factory: sessionmaker[Session] = request.app.state.sessionmaker
    with factory() as session, session.begin():
        apply_rls_context(session, principal)
        yield session


@router.post(
    "/documents",
    response_model=DocumentReceivedOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses=UPLOAD_PROBLEMS,
    openapi_extra=_UPLOAD_BODY,
    summary="Upload a document (multipart, streamed into encrypted quarantine)",
)
def post_document(
    request: Request,
    db: DbSession,
    principal: Writer,
    account_id: Annotated[uuid.UUID | None, Query()] = None,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    settings = get_app_settings(request)
    boundary_of(request.headers.get("content-type"))
    manage.check_upload_target(db, principal, account_id)
    # Nothing below needs the request's transaction: end it before the transfer.
    db.commit()
    store = manage.blob_store(settings)
    upload = MultipartFile(request.headers.get("content-type"), _request_body(request))
    stored = manage.store_upload(
        store=store,
        keyring=get_keyring(request),
        chunks=upload.chunks(),
        max_bytes=settings.upload_max_bytes,
    )
    try:
        with _transaction(request, principal) as session:
            return manage.record_upload(
                session,
                principal,
                stored,
                store=store,
                filename=upload.filename,
                account_id=account_id,
                idempotency_key=idempotency_key,
                request=request,
            )
    except BaseException:
        # Also when the commit itself fails: the object's key was never stored.
        store.delete(stored.sealed.area, stored.sealed.key)
        raise


@router.get("/documents", response_model=DocumentPage, responses=PROBLEMS, summary="Documents")
def get_documents(
    db: DbSession,
    principal: Reader,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=DOCUMENTS_MAX_PAGE)] = DOCUMENTS_DEFAULT_PAGE,
    kind: Annotated[DocumentKind | None, Query()] = None,
    document_status: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    account: Annotated[uuid.UUID | None, Query()] = None,
    tax_year: Annotated[int | None, Query(ge=1900, le=2200)] = None,
) -> DocumentPage:
    return manage.list_documents(
        db,
        principal,
        cursor=cursor,
        limit=limit,
        kind=kind,
        status=document_status,
        account_id=account,
        tax_year=tax_year,
    )


@router.get(
    "/documents/{document_id}",
    response_model=DocumentDetailOut,
    responses=PROBLEMS,
    summary="One document (metadata only)",
)
def get_document(document_id: uuid.UUID, db: DbSession, principal: Reader) -> DocumentDetailOut:
    return manage.get_document(db, principal, document_id)


@router.get(
    "/documents/{document_id}/download",
    response_class=StreamingResponse,
    responses={
        200: {"description": "The original file, as an attachment", "content": {}},
        **{k: v for k, v in PROBLEMS.items() if k != 422},
        500: {"description": "Problem details", "content": {"application/problem+json": {}}},
    },
    summary="Download the original (member or above; decrypted as it streams)",
)
def download_document(
    document_id: uuid.UUID, request: Request, db: DbSession, principal: Reader
) -> StreamingResponse:
    download = manage.open_download(
        db,
        principal,
        document_id,
        store=manage.blob_store(get_app_settings(request)),
        keyring=get_keyring(request),
        request=request,
    )
    # The audit row is committed before the first byte goes out.
    db.commit()
    # Content-Type stays exactly the sniffed type (no charset is added: the
    # bytes are the original file's).
    headers = attachment_headers(download.filename, download.media_type)
    headers["Content-Length"] = str(download.size)
    return StreamingResponse(download.chunks, headers=headers)


@router.post(
    "/documents/{document_id}/reprocess",
    response_model=DocumentReprocessOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses=PROBLEMS,
    summary="Run the import pipeline again",
)
def post_reprocess(
    document_id: uuid.UUID, request: Request, db: DbSession, principal: Writer
) -> DocumentReprocessOut:
    return manage.reprocess(db, principal, document_id, request=request)


def require_document_writer(document_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer` who may change the document (``404``/``403`` before the body)."""
    manage.get_editable_document(db, principal, document_id)
    return principal


DocumentWriter = Annotated[Principal, Depends(require_document_writer)]


@router.post(
    "/documents/{document_id}/link",
    response_model=DocumentLinkOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Attach a document to a transaction or account",
)
def post_link(
    document_id: uuid.UUID,
    body: DocumentLinkIn,
    request: Request,
    response: Response,
    db: DbSession,
    principal: DocumentWriter,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    def run() -> DocumentLinkOut:
        out, created = manage.link(db, principal, document_id, body, request=request)
        if not created:
            response.status_code = status.HTTP_200_OK
        return out

    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /documents/{document_id}/link",
        payload=payload_of(body, document_id=document_id),
        run=run,
    )


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete a document (its key is destroyed first)",
)
def delete_document(
    document_id: uuid.UUID, request: Request, db: DbSession, principal: Writer
) -> Response:
    manage.delete(
        db,
        principal,
        document_id,
        store=manage.blob_store(get_app_settings(request)),
        request=request,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)

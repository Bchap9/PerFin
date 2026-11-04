# SPDX-License-Identifier: AGPL-3.0-or-later
"""Full export and import (API.md ``/export``, ``/import``; docs/PORTABILITY.md).

Every route needs a **browser session** of an owner or admin; the creating
routes and the download also need a step-up verification within the last
10 minutes (AUTH.md: "export everything"). Tokens are refused (``403
session-required``), even with the ``export`` scope: a step-up exists only
for sessions. Rules in ``app.domain.portability.service``.

* ``POST /export`` ``{"passphrase"?}`` -> ``202 {"job_id", "state",
  "passphrase_protected"}``; ``Idempotency-Key`` honoured (the passphrase
  is not part of the stored request fingerprint). ``409
  export-in-progress`` while another export of the household is pending.
* ``GET /export/{job_id}``: state, size, SHA-256 of the file to download,
  expiry and row counts; only for the user who requested it.
* ``GET /export/{job_id}/download``: once, within ``PERFIN_EXPORT_TTL_MINUTES``
  (``410 export-already-downloaded`` / ``410 export-expired``, ``409
  export-not-ready``). Decrypted while it streams, as an attachment.
* ``POST /import``: ``multipart/form-data`` with one part named ``file``
  (streamed, never spooled: encrypted into the quarantine area as it
  arrives, at most ``PERFIN_IMPORT_MAX_MB``); the passphrase of an encrypted
  archive in the ``X-Archive-Passphrase`` header (checked while the upload
  streams). ``202 {"job_id", ...}``; ``409 import-in-progress``; ``415``
  for anything that is not a PerFin archive; ``422 passphrase-required`` /
  ``archive-wrong-passphrase``.
* ``GET /import/{job_id}``: state, counts or the reason it failed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Annotated, Any

import anyio.from_thread
from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, sessionmaker

from app.api.routers.accounts import PROBLEMS
from app.auth.authz import require_role, require_step_up
from app.auth.deps import DbSession, get_app_settings, get_keyring
from app.auth.models import HouseholdRole
from app.auth.principal import Principal, apply_rls_context
from app.core.headers import attachment_headers
from app.core.idempotency import IdempotencyKeyHeader, idempotent
from app.domain.documents.manage import blob_store
from app.domain.portability import service
from app.domain.portability.schemas import (
    PASSPHRASE_HEADER,
    PASSPHRASE_MAX,
    ExportOut,
    ExportQueuedOut,
    ExportRequest,
    ImportOut,
    ImportQueuedOut,
)
from app.ingestion.intake.multipart import MultipartFile, boundary_of

router = APIRouter(tags=["portability"])

Admin = Annotated[Principal, Depends(require_role(HouseholdRole.ADMIN))]


def require_admin_session(principal: Admin) -> Principal:
    """Owner or admin, browser session (no tokens)."""
    if principal.auth_method != "session":
        raise service.session_required()
    return principal


AdminSession = Annotated[Principal, Depends(require_admin_session)]


def require_admin_step_up(principal: AdminSession) -> Principal:
    """:data:`AdminSession` with a fresh second-factor verification (AUTH.md step-up)."""
    return require_step_up(principal)


SteppedUp = Annotated[Principal, Depends(require_admin_step_up)]

GONE_PROBLEMS: dict[int | str, dict[str, Any]] = {
    **{k: v for k, v in PROBLEMS.items() if k != 422},
    **{
        code: {"description": "Problem details", "content": {"application/problem+json": {}}}
        for code in (410, 500)
    },
}
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


@router.post(
    "/export",
    response_model=ExportQueuedOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses=PROBLEMS,
    summary="Request a full export (job; owner/admin, step-up)",
)
def post_export(
    body: ExportRequest,
    request: Request,
    db: DbSession,
    principal: SteppedUp,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    # The passphrase never reaches the idempotency fingerprint.
    fingerprint = {"body": {"passphrase_protected": body.passphrase is not None}}
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /export",
        payload=fingerprint,
        run=lambda: service.request_export(
            db, principal, body, keyring=get_keyring(request), request=request
        ),
        status_code=status.HTTP_202_ACCEPTED,
    )


@router.get(
    "/export/{job_id}",
    response_model=ExportOut,
    responses=PROBLEMS,
    summary="State of one of my exports",
)
def get_export(job_id: uuid.UUID, db: DbSession, principal: AdminSession) -> ExportOut:
    return service.get_export(db, principal, job_id)


@router.get(
    "/export/{job_id}/download",
    response_class=StreamingResponse,
    responses={
        200: {"description": "The archive, as an attachment", "content": {}},
        **GONE_PROBLEMS,
    },
    summary="Download an export once (owner/admin, step-up; decrypted as it streams)",
)
def download_export(
    job_id: uuid.UUID, request: Request, db: DbSession, principal: SteppedUp
) -> StreamingResponse:
    store = blob_store(get_app_settings(request))
    if service.expire_if_due(db, principal, job_id, store=store):
        db.commit()  # the expired archive is destroyed even though this answers 410
        raise service.export_expired()
    download = service.open_export_download(
        db,
        principal,
        job_id,
        store=store,
        keyring=get_keyring(request),
        request=request,
    )
    # The key is destroyed and the audit row written before the first byte.
    db.commit()
    headers = attachment_headers(download.filename, download.media_type)
    headers["Content-Length"] = str(download.size)
    return StreamingResponse(download.chunks, headers=headers)


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


@router.post(
    "/import",
    response_model=ImportQueuedOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses=UPLOAD_PROBLEMS,
    openapi_extra=_UPLOAD_BODY,
    summary="Import a full export into this household (job; owner/admin, step-up)",
)
def post_import(
    request: Request,
    db: DbSession,
    principal: SteppedUp,
    passphrase: Annotated[
        str | None,
        Header(alias=PASSPHRASE_HEADER, max_length=PASSPHRASE_MAX, include_in_schema=True),
    ] = None,
) -> ImportQueuedOut:
    settings = get_app_settings(request)
    boundary_of(request.headers.get("content-type"))
    service.check_import_allowed(db, principal)
    # Nothing below needs the request's transaction: end it before the transfer.
    db.commit()
    store = blob_store(settings)
    keyring = get_keyring(request)
    upload = MultipartFile(request.headers.get("content-type"), _request_body(request))
    stored = service.store_import(
        upload.chunks(),
        store=store,
        keyring=keyring,
        max_bytes=settings.import_max_bytes,
        passphrase=passphrase or None,
    )
    factory: sessionmaker[Session] = request.app.state.sessionmaker
    try:
        with factory() as session, session.begin():
            apply_rls_context(session, principal)
            return service.record_import(
                session, principal, stored, keyring=keyring, request=request
            )
    except BaseException:
        # Also when the commit fails: the object's key was never stored.
        store.delete(stored.sealed.area, stored.sealed.key)
        raise


@router.get(
    "/import/{job_id}",
    response_model=ImportOut,
    responses=PROBLEMS,
    summary="State and outcome of one of my imports",
)
def get_import(job_id: uuid.UUID, db: DbSession, principal: AdminSession) -> ImportOut:
    return service.get_import(db, principal, job_id)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured imports (API.md ``/imports``; rules in ``app.domain.imports.service``).

* ``GET /imports/mappings``, ``GET /imports/mappings/{id}``: any role
  (mappings tied to an account the caller cannot see are hidden).
* ``POST /imports/mappings`` (``Idempotency-Key`` honoured), ``PATCH`` and
  ``DELETE /imports/mappings/{id}``: member or above; a mapping's account
  must be editable by the caller.
* ``GET /imports/layouts``: any role; the layouts the household taught
  PerFin ("Remember this layout", "Teach PerFin"), as the classifier uses
  them. ``DELETE /imports/layouts/{id}``: member or above; forgets every
  version of that layout (``app.domain.imports.layouts``).
* ``GET /imports/{document_id}/preview``: any role that can see the
  document; the extracted tables/statements stored by the sandbox, never
  re-parsed, with the matching saved mapping and a suggested one.
* ``POST /imports/{document_id}/apply-mapping``: member or above; queues
  ``imports.apply_mapping`` and answers ``202`` with its id.
* ``GET /documents/{document_id}/accounts`` and ``POST
  /documents/{document_id}/accounts/apply``: member or above who may change
  the document; the accounts the document names, matched or proposed, and
  the choice of an existing or new account for each
  (``app.domain.imports.proposals``).

Tokens need ``documents:read`` / ``documents:write``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.routers.accounts import PROBLEMS, scoped_role
from app.auth.deps import DbSession
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.idempotency import IdempotencyKeyHeader, idempotent, payload_of
from app.domain.documents.service import get_visible_document
from app.domain.imports import layouts, proposals, service
from app.domain.imports.schemas import (
    DEFAULT_PAGE,
    MAX_PAGE,
    PREVIEW_DEFAULT,
    PREVIEW_MAX,
    ApplyMapping,
    DocumentAccounts,
    DocumentAccountsApplied,
    DocumentAccountsApply,
    ImportAccepted,
    ImportPreview,
    LearnedLayoutList,
    MappingCreate,
    MappingOut,
    MappingPage,
    MappingUpdate,
)

router = APIRouter(tags=["imports"])

Reader = Annotated[Principal, Depends(scoped_role("documents:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("documents:write", HouseholdRole.MEMBER))]


def require_mapping_writer(mapping_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the mapping must be changeable (``404`` before the body is read)."""
    service.require_mapping_writer(db, principal, mapping_id)
    return principal


def require_document_writer(document_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the document must be visible (``404`` before the body is read)."""
    get_visible_document(db, principal, document_id)
    return principal


MappingWriter = Annotated[Principal, Depends(require_mapping_writer)]
DocumentWriter = Annotated[Principal, Depends(require_document_writer)]


@router.get(
    "/imports/mappings", response_model=MappingPage, summary="Saved column mappings I can see"
)
def get_mappings(
    db: DbSession,
    principal: Reader,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> MappingPage:
    return service.list_mappings(db, principal, cursor=cursor, limit=limit)


@router.post(
    "/imports/mappings",
    response_model=MappingOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Save a column mapping (member or above)",
)
def post_mapping(
    body: MappingCreate,
    request: Request,
    db: DbSession,
    principal: Writer,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /imports/mappings",
        payload=payload_of(body),
        run=lambda: service.create_mapping(db, principal, body, request=request),
    )


@router.get(
    "/imports/mappings/{mapping_id}",
    response_model=MappingOut,
    responses=PROBLEMS,
    summary="One column mapping",
)
def get_mapping(mapping_id: uuid.UUID, db: DbSession, principal: Reader) -> MappingOut:
    return service.get_mapping(db, principal, mapping_id)


@router.patch(
    "/imports/mappings/{mapping_id}",
    response_model=MappingOut,
    responses=PROBLEMS,
    summary="Change a column mapping (member or above)",
)
def patch_mapping(
    mapping_id: uuid.UUID,
    body: MappingUpdate,
    request: Request,
    db: DbSession,
    principal: MappingWriter,
) -> MappingOut:
    return service.update_mapping(db, principal, mapping_id, body, request=request)


@router.delete(
    "/imports/mappings/{mapping_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete a column mapping (member or above)",
)
def delete_mapping(
    mapping_id: uuid.UUID, request: Request, db: DbSession, principal: Writer
) -> Response:
    service.delete_mapping(db, principal, mapping_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/imports/layouts",
    response_model=LearnedLayoutList,
    summary="Layouts PerFin was taught (remembered and labelled statements)",
)
def get_layouts(db: DbSession, principal: Reader) -> LearnedLayoutList:
    return layouts.list_layouts(db, principal)


@router.delete(
    "/imports/layouts/{layout_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Forget a learned layout, every version of it (member or above)",
)
def delete_layout(
    layout_id: uuid.UUID, request: Request, db: DbSession, principal: Writer
) -> Response:
    layouts.delete_layout(db, principal, layout_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/imports/{document_id}/preview",
    response_model=ImportPreview,
    responses=PROBLEMS,
    summary="Extracted rows of a document, for the mapping editor",
)
def get_preview(
    document_id: uuid.UUID,
    db: DbSession,
    principal: Reader,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=PREVIEW_MAX)] = PREVIEW_DEFAULT,
) -> ImportPreview:
    return service.preview(db, principal, document_id, offset=offset, limit=limit)


@router.post(
    "/imports/{document_id}/apply-mapping",
    response_model=ImportAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses=PROBLEMS,
    summary="Re-run a document's import with a mapping and/or account (job)",
)
def post_apply_mapping(
    document_id: uuid.UUID,
    body: ApplyMapping,
    request: Request,
    db: DbSession,
    principal: DocumentWriter,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /imports/{document_id}/apply-mapping",
        payload=payload_of(body, document_id=document_id),
        run=lambda: service.request_apply(db, principal, document_id, body, request=request),
        status_code=status.HTTP_202_ACCEPTED,
    )


@router.get(
    "/documents/{document_id}/accounts",
    response_model=DocumentAccounts,
    responses=PROBLEMS,
    summary="The accounts a document names, matched or proposed (member or above)",
)
def get_document_accounts(
    document_id: uuid.UUID, db: DbSession, principal: DocumentWriter
) -> DocumentAccounts:
    return proposals.detect_accounts(db, principal, document_id)


@router.post(
    "/documents/{document_id}/accounts/apply",
    response_model=DocumentAccountsApplied,
    status_code=status.HTTP_202_ACCEPTED,
    responses=PROBLEMS,
    summary="Use or create an account for each of a document's accounts and re-import (job)",
)
def post_document_accounts(
    document_id: uuid.UUID,
    body: DocumentAccountsApply,
    request: Request,
    db: DbSession,
    principal: DocumentWriter,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /documents/{document_id}/accounts/apply",
        payload=payload_of(body, document_id=document_id),
        run=lambda: proposals.apply_accounts(db, principal, document_id, body, request=request),
        status_code=status.HTTP_202_ACCEPTED,
    )

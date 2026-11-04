# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tax vault (API.md ``/tax-years``, ``/tax-category-map``; DATA_MODEL.md "Tax vault").

* ``GET /tax-years``, ``GET /tax-years/{year}``: any role; each year with
  its checklist counts.
* ``POST /tax-years`` (``Idempotency-Key`` honoured; learns the checklist
  from the year before), ``PATCH /tax-years/{year}`` (filing status, notes,
  ``state`` open/filed/closed; reopening a closed year needs admin):
  member or above. ``DELETE /tax-years/{year}``: admin or owner.
* ``GET /tax-years/{year}/checklist``: any role (a received item's
  ``document_id`` only when the caller may see the document);
  ``POST`` / ``PATCH`` / ``DELETE /tax-years/{year}/checklist[/{item_id}]``
  and ``POST /tax-years/{year}/checklist/learn``: member or above.
* ``GET /tax-years/{year}/forms``: amounts by box of the parsed forms of
  documents the caller may see (identity boxes are never stored).
* ``GET /tax-years/{year}/summary``: the yearly roll-up by category and
  schedule hint over the caller's visible accounts (``partial`` when that is
  not every account), form totals and checklist counts.
* ``GET /tax-category-map``: any role; ``PUT`` / ``DELETE
  /tax-category-map/{category_id}``: member or above.
* ``POST /tax-years/{year}/export`` (accountant export: originals, summary
  CSV and PDF): owner or admin, browser session, step-up; ``202 {"job_id"}``.
  ``GET /tax-years/{year}/export/{job_id}`` (state) and ``GET
  .../download`` (once, step-up; decrypted as it streams): the requester
  only.

Tokens need ``tax:read`` / ``tax:write``; the export refuses tokens.
Rules: ``app.domain.tax``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from app.api.routers.accounts import PROBLEMS, scoped_role
from app.api.routers.portability import GONE_PROBLEMS, AdminSession, SteppedUp
from app.auth.deps import DbSession, get_app_settings, get_keyring
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.headers import attachment_headers
from app.core.idempotency import IdempotencyKeyHeader, idempotent, payload_of
from app.domain.documents.manage import blob_store
from app.domain.tax import export, rollup, service
from app.domain.tax.models import YEAR_MAX, YEAR_MIN
from app.domain.tax.schemas import (
    CategoryMapIn,
    CategoryMapList,
    CategoryMapOut,
    ChecklistItemCreate,
    ChecklistItemOut,
    ChecklistItemUpdate,
    ChecklistOut,
    FormDataList,
    Jurisdiction,
    LearnOut,
    TaxExportOut,
    TaxExportQueuedOut,
    TaxSummaryOut,
    TaxYearCreate,
    TaxYearList,
    TaxYearOut,
    TaxYearUpdate,
)

router = APIRouter(tags=["tax"])

Reader = Annotated[Principal, Depends(scoped_role("tax:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("tax:write", HouseholdRole.MEMBER))]
Manager = Annotated[Principal, Depends(scoped_role("tax:write", HouseholdRole.ADMIN))]
YearPath = Annotated[int, Path(ge=YEAR_MIN, le=YEAR_MAX)]


def require_year_writer(year: YearPath, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the year must exist (``404`` before the body is read)."""
    service.require_year(db, principal, year)
    return principal


def require_item_writer(
    year: YearPath, item_id: uuid.UUID, db: DbSession, principal: Writer
) -> Principal:
    service.require_item(db, principal, year, item_id)
    return principal


def require_category_writer(category_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    service.require_category(db, principal, category_id)
    return principal


YearWriter = Annotated[Principal, Depends(require_year_writer)]
ItemWriter = Annotated[Principal, Depends(require_item_writer)]
CategoryWriter = Annotated[Principal, Depends(require_category_writer)]


# ---------------------------------------------------------------------------
# Tax years
# ---------------------------------------------------------------------------


@router.get("/tax-years", response_model=TaxYearList, summary="Tax years of the household")
def get_tax_years(db: DbSession, principal: Reader) -> TaxYearList:
    return service.list_years(db, principal)


@router.post(
    "/tax-years",
    response_model=TaxYearOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Open a tax year (its checklist is learned from the year before)",
)
def post_tax_year(
    body: TaxYearCreate,
    request: Request,
    db: DbSession,
    principal: Writer,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /tax-years",
        payload=payload_of(body),
        run=lambda: service.create_year(db, principal, body, request=request),
        status_code=status.HTTP_201_CREATED,
    )


@router.get(
    "/tax-years/{year}", response_model=TaxYearOut, responses=PROBLEMS, summary="One tax year"
)
def get_tax_year(year: YearPath, db: DbSession, principal: Reader) -> TaxYearOut:
    return service.get_year(db, principal, year)


@router.patch(
    "/tax-years/{year}",
    response_model=TaxYearOut,
    responses=PROBLEMS,
    summary="Change a tax year (filing status, notes, state)",
)
def patch_tax_year(
    year: YearPath, body: TaxYearUpdate, request: Request, db: DbSession, principal: YearWriter
) -> TaxYearOut:
    return service.update_year(db, principal, year, body, request=request)


@router.delete(
    "/tax-years/{year}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=PROBLEMS,
    summary="Delete a tax year and its checklist (owner/admin)",
)
def delete_tax_year(
    year: YearPath, request: Request, db: DbSession, principal: Manager
) -> Response:
    service.delete_year(db, principal, year, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------


@router.get(
    "/tax-years/{year}/checklist",
    response_model=ChecklistOut,
    responses=PROBLEMS,
    summary="Expected and received documents of a tax year",
)
def get_checklist(year: YearPath, db: DbSession, principal: Reader) -> ChecklistOut:
    return service.get_checklist(db, principal, year)


@router.post(
    "/tax-years/{year}/checklist",
    response_model=ChecklistItemOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Expect a form for a tax year",
)
def post_checklist_item(
    year: YearPath,
    body: ChecklistItemCreate,
    request: Request,
    db: DbSession,
    principal: YearWriter,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /tax-years/{year}/checklist",
        payload=payload_of(body, year=year),
        run=lambda: service.add_item(db, principal, year, body, request=request),
        status_code=status.HTTP_201_CREATED,
    )


@router.post(
    "/tax-years/{year}/checklist/learn",
    response_model=LearnOut,
    responses=PROBLEMS,
    summary="Expect again what was received the year before",
)
def post_checklist_learn(
    year: YearPath, request: Request, db: DbSession, principal: Writer
) -> LearnOut:
    return service.relearn(db, principal, year, request=request)


@router.patch(
    "/tax-years/{year}/checklist/{item_id}",
    response_model=ChecklistItemOut,
    responses=PROBLEMS,
    summary="Change a checklist item (status, issuer, document)",
)
def patch_checklist_item(
    year: YearPath,
    item_id: uuid.UUID,
    body: ChecklistItemUpdate,
    request: Request,
    db: DbSession,
    principal: ItemWriter,
) -> ChecklistItemOut:
    return service.update_item(db, principal, year, item_id, body, request=request)


@router.delete(
    "/tax-years/{year}/checklist/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=PROBLEMS,
    summary="Remove a checklist item",
)
def delete_checklist_item(
    year: YearPath, item_id: uuid.UUID, request: Request, db: DbSession, principal: Writer
) -> Response:
    service.delete_item(db, principal, year, item_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Forms and summary
# ---------------------------------------------------------------------------


@router.get(
    "/tax-years/{year}/forms",
    response_model=FormDataList,
    responses=PROBLEMS,
    summary="Amounts read from the year's tax forms (by box; no identity data)",
)
def get_forms(year: YearPath, db: DbSession, principal: Reader) -> FormDataList:
    return service.list_forms(db, principal, year)


@router.get(
    "/tax-years/{year}/summary",
    response_model=TaxSummaryOut,
    responses=PROBLEMS,
    summary="Yearly roll-up by category and schedule hint (visible accounts)",
)
def get_summary(year: YearPath, db: DbSession, principal: Reader) -> TaxSummaryOut:
    return rollup.summary(db, principal, year)


# ---------------------------------------------------------------------------
# Category -> schedule/line hints
# ---------------------------------------------------------------------------


@router.get(
    "/tax-category-map",
    response_model=CategoryMapList,
    responses=PROBLEMS,
    summary="Schedule/line hints per category",
)
def get_category_map(
    db: DbSession, principal: Reader, jurisdiction: Annotated[Jurisdiction | None, Query()] = None
) -> CategoryMapList:
    return service.list_category_map(db, principal, jurisdiction)


@router.put(
    "/tax-category-map/{category_id}",
    response_model=CategoryMapOut,
    responses=PROBLEMS,
    summary="Set a category's schedule/line hint",
)
def put_category_map(
    category_id: uuid.UUID,
    body: CategoryMapIn,
    request: Request,
    db: DbSession,
    principal: CategoryWriter,
) -> CategoryMapOut:
    return service.put_category_map(db, principal, category_id, body, request=request)


@router.delete(
    "/tax-category-map/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=PROBLEMS,
    summary="Remove a category's schedule/line hint",
)
def delete_category_map(
    category_id: uuid.UUID,
    request: Request,
    db: DbSession,
    principal: Writer,
    jurisdiction: Annotated[Jurisdiction, Query()] = "US",
) -> Response:
    service.delete_category_map(db, principal, category_id, jurisdiction, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Accountant export
# ---------------------------------------------------------------------------


@router.post(
    "/tax-years/{year}/export",
    response_model=TaxExportQueuedOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses=PROBLEMS,
    summary="Request the accountant export of a tax year (job; owner/admin, step-up)",
)
def post_tax_export(
    year: YearPath,
    request: Request,
    db: DbSession,
    principal: SteppedUp,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /tax-years/{year}/export",
        payload={"year": year},
        run=lambda: export.request_export(db, principal, year, request=request),
        status_code=status.HTTP_202_ACCEPTED,
    )


@router.get(
    "/tax-years/{year}/export/{job_id}",
    response_model=TaxExportOut,
    responses=PROBLEMS,
    summary="State of one of my accountant exports",
)
def get_tax_export(
    year: YearPath, job_id: uuid.UUID, db: DbSession, principal: AdminSession
) -> TaxExportOut:
    return export.get_export(db, principal, year, job_id)


@router.get(
    "/tax-years/{year}/export/{job_id}/download",
    response_class=StreamingResponse,
    responses={200: {"description": "The zip, as an attachment", "content": {}}, **GONE_PROBLEMS},
    summary="Download an accountant export once (owner/admin, step-up)",
)
def download_tax_export(
    year: YearPath, job_id: uuid.UUID, request: Request, db: DbSession, principal: SteppedUp
) -> StreamingResponse:
    store = blob_store(get_app_settings(request))
    if export.expire_if_due(db, principal, year, job_id, store=store):
        db.commit()  # the expired archive is destroyed even though this answers 410
        raise export.export_expired()
    download = export.open_download(
        db,
        principal,
        year,
        job_id,
        store=store,
        keyring=get_keyring(request),
        request=request,
    )
    # The key is destroyed and the audit row written before the first byte.
    db.commit()
    headers = attachment_headers(download.filename, "application/zip")
    headers["Content-Length"] = str(download.size)
    return StreamingResponse(download.chunks, headers=headers)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tax vault rules: validation, form data, tax years, checklist, category map.

Building blocks:

* :func:`check_jurisdiction` / :func:`check_filing_status` /
  :func:`check_form_type`: validate against the registered form sets
  (``app.domain.tax.forms``); ``422`` problems otherwise.
* :func:`record_form_data`: the **only** way a ``tax_form_data`` row is
  written. It runs :func:`~app.domain.tax.forms.sanitize_boxes`, so identity
  boxes (SSN/EIN/TIN, names, addresses, account numbers) never reach the
  database, and keeps the issuer's name only (control characters removed,
  whitespace collapsed, at most 200 characters).
* :func:`form_boxes`: the stored amounts as ``Decimal``.

Then the API rules (tax years, checklist learned from prior years and
editable, schedule hints per category, form data of visible documents);
see the section comments below. Parsed forms tick the checklist off through
:func:`mark_received` (``app.domain.tax.ingest``); roll-ups are in
``app.domain.tax.rollup``, the accountant export in ``app.domain.tax.export``
and the reminders in ``app.domain.tax.reminders``.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Iterable
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.domain.documents.models import Document
from app.domain.documents.service import get_visible_document, visible_document_filter
from app.domain.tax.forms import (
    FormDataError,
    FormDef,
    UnknownFormError,
    UnknownJurisdictionError,
    form_def,
    get_form_set,
    sanitize_boxes,
)
from app.domain.tax.models import (
    ISSUER_MAX,
    YEAR_MAX,
    YEAR_MIN,
    ChecklistOrigin,
    ChecklistStatus,
    FormDataSource,
    TaxCategoryMap,
    TaxChecklistItem,
    TaxFormData,
    TaxYear,
    TaxYearState,
)
from app.domain.tax.schemas import (
    CategoryMapIn,
    CategoryMapList,
    CategoryMapOut,
    ChecklistCounts,
    ChecklistItemCreate,
    ChecklistItemOut,
    ChecklistItemUpdate,
    ChecklistOut,
    FormBoxOut,
    FormDataList,
    FormDataOut,
    LearnOut,
    TaxYearCreate,
    TaxYearList,
    TaxYearOut,
    TaxYearUpdate,
)
from app.domain.transactions.models import Category

_SPACES: Final = re.compile(r"\s+")


def _invalid(detail: str) -> ProblemError:
    return ProblemError(422, "tax-form-invalid", "Tax form data is not valid", detail)


def check_jurisdiction(code: str) -> str:
    try:
        get_form_set(code)
    except UnknownJurisdictionError:
        raise ProblemError(
            422, "tax-jurisdiction-unknown", "Unknown tax jurisdiction", f"{code!r}"
        ) from None
    return code


def check_filing_status(jurisdiction: str, status: str | None) -> str | None:
    if status is None:
        return None
    if status not in get_form_set(check_jurisdiction(jurisdiction)).filing_statuses:
        raise ProblemError(422, "tax-filing-status-unknown", "Unknown filing status", f"{status!r}")
    return status


def check_form_type(jurisdiction: str, form_type: str) -> FormDef:
    check_jurisdiction(jurisdiction)
    try:
        return form_def(jurisdiction, form_type)
    except UnknownFormError:
        raise ProblemError(
            422, "tax-form-unknown", "Unknown tax form", f"{jurisdiction} {form_type!r}"
        ) from None


def clean_issuer_name(value: str | None) -> str | None:
    """Printable, single-spaced, at most :data:`ISSUER_MAX` characters; ``None`` if empty."""
    if value is None:
        return None
    printable = "".join(
        " " if unicodedata.category(ch).startswith(("C", "Z")) else ch
        for ch in unicodedata.normalize("NFKC", value)
    )
    text = _SPACES.sub(" ", printable).strip()[:ISSUER_MAX].strip()
    return text or None


def record_form_data(
    db: Session,
    household_id: uuid.UUID,
    *,
    document_id: uuid.UUID,
    jurisdiction: str,
    form_type: str,
    tax_year: int,
    boxes: dict[str, object],
    source: FormDataSource,
    issuer_name: str | None = None,
    form_index: int = 0,
    tax_year_id: uuid.UUID | None = None,
    confidence: Decimal | None = None,
) -> TaxFormData:
    """Write one form's amounts (module doc). The caller has set the RLS context."""
    form = check_form_type(jurisdiction, form_type)
    if not YEAR_MIN <= tax_year <= YEAR_MAX:
        raise _invalid("tax year out of range")
    try:
        sanitized = sanitize_boxes(form, boxes)
    except FormDataError as exc:
        raise _invalid(str(exc)) from None
    row = TaxFormData(
        household_id=household_id,
        document_id=document_id,
        jurisdiction=jurisdiction,
        form_type=form.form_type,
        form_index=form_index,
        tax_year=tax_year,
        tax_year_id=tax_year_id,
        issuer_name=clean_issuer_name(issuer_name),
        boxes=sanitized.as_json(),
        source=source,
        confidence=confidence,
    )
    db.add(row)
    db.flush()
    return row


def form_boxes(row: TaxFormData) -> dict[str, Decimal]:
    return {key: Decimal(str(value)) for key, value in row.boxes.items()}


# ---------------------------------------------------------------------------
# Tax years (API.md ``/tax-years``)
# ---------------------------------------------------------------------------
#
# * A tax year is household-wide: any role reads it, members and above
#   create and change it, admins and owners delete it or reopen a closed one.
# * ``state``: ``open`` -> ``filed`` -> ``closed`` (or ``open`` -> ``closed``
#   for a year with nothing to file); a filed year may go back to ``open``;
#   a closed one only by an admin or owner. A closed year's filing status and
#   notes are frozen (``409 tax-year-closed``).
# * Creating a year (through the API or when the first form of a year is
#   parsed) learns its checklist from the year before (:func:`learn_checklist`).


def tax_year_not_found() -> ProblemError:
    return ProblemError(404, "tax-year-not-found", "Tax year not found")


def checklist_item_not_found() -> ProblemError:
    return ProblemError(404, "tax-checklist-item-not-found", "Checklist item not found")


def _household(principal: Principal) -> uuid.UUID:
    if principal.household_id is None:  # the router's role guard already refused this
        raise forbidden("You are not a member of a household.")
    return principal.household_id


def issuer_key(value: str | None) -> str:
    """Comparison key of an issuer name: letters and digits, case-folded."""
    if not value:
        return ""
    return "".join(ch for ch in unicodedata.normalize("NFKC", value).casefold() if ch.isalnum())


def get_year_row(db: Session, household_id: uuid.UUID, year: int) -> TaxYear | None:
    return db.execute(
        select(TaxYear).where(TaxYear.household_id == household_id, TaxYear.year == year)
    ).scalar_one_or_none()


def require_year(db: Session, principal: Principal, year: int, *, lock: bool = False) -> TaxYear:
    query = select(TaxYear).where(
        TaxYear.household_id == _household(principal), TaxYear.year == year
    )
    row = db.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if row is None:
        raise tax_year_not_found()
    return row


def checklist_counts(db: Session, tax_year: TaxYear) -> ChecklistCounts:
    rows = db.execute(
        select(TaxChecklistItem.status, func.count())
        .where(
            TaxChecklistItem.household_id == tax_year.household_id,
            TaxChecklistItem.tax_year_id == tax_year.id,
        )
        .group_by(TaxChecklistItem.status)
    ).all()
    counts = {ChecklistStatus(status): int(n) for status, n in rows}
    return ChecklistCounts(
        expected=counts.get(ChecklistStatus.EXPECTED, 0),
        received=counts.get(ChecklistStatus.RECEIVED, 0),
        not_applicable=counts.get(ChecklistStatus.NOT_APPLICABLE, 0),
    )


def year_out(db: Session, row: TaxYear) -> TaxYearOut:
    return TaxYearOut(
        id=row.id,
        year=row.year,
        jurisdiction=row.jurisdiction,
        filing_status=row.filing_status,
        state=row.state,
        notes=row.notes,
        checklist=checklist_counts(db, row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _record(
    db: Session,
    household_id: uuid.UUID,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    *,
    after: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
    principal: Principal | None = None,
    request: HTTPConnection | None = None,
    event: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    audit(
        db,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
        household_id=household_id,
        user_id=principal.user_id if principal is not None else None,
        request=request,
    )
    if event is not None:
        emit(db, household_id, event, payload or {"id": str(target_id)})


def ensure_year(
    db: Session,
    household_id: uuid.UUID,
    year: int,
    *,
    jurisdiction: str = "US",
    filing_status: str | None = None,
    notes: str | None = None,
) -> tuple[TaxYear, bool]:
    """The household's row for ``year``, created (and its checklist learned) if missing."""
    row = get_year_row(db, household_id, year)
    if row is not None:
        return row, False
    check_filing_status(jurisdiction, filing_status)
    row = TaxYear(
        household_id=household_id,
        year=year,
        jurisdiction=check_jurisdiction(jurisdiction),
        filing_status=filing_status,
        state=TaxYearState.OPEN,
        notes=notes,
    )
    db.add(row)
    db.flush()
    learn_checklist(db, row)
    return row, True


def list_years(db: Session, principal: Principal) -> TaxYearList:
    rows = (
        db.execute(
            select(TaxYear)
            .where(TaxYear.household_id == _household(principal))
            .order_by(TaxYear.year.desc())
        )
        .scalars()
        .all()
    )
    return TaxYearList(items=[year_out(db, row) for row in rows])


def get_year(db: Session, principal: Principal, year: int) -> TaxYearOut:
    return year_out(db, require_year(db, principal, year))


def create_year(
    db: Session, principal: Principal, body: TaxYearCreate, *, request: HTTPConnection | None
) -> TaxYearOut:
    household_id = _household(principal)
    if get_year_row(db, household_id, body.year) is not None:
        raise ProblemError(
            409, "tax-year-exists", "Tax year exists", f"The household already has {body.year}."
        )
    row, _ = ensure_year(
        db,
        household_id,
        body.year,
        jurisdiction=body.jurisdiction,
        filing_status=body.filing_status,
        notes=body.notes,
    )
    _record(
        db,
        household_id,
        "tax_year.create",
        "tax_year",
        row.id,
        after={"year": row.year, "jurisdiction": row.jurisdiction},
        principal=principal,
        request=request,
        event="tax_year.created",
        payload={"id": str(row.id), "year": row.year},
    )
    return year_out(db, row)


_TRANSITIONS: Final[dict[TaxYearState, frozenset[TaxYearState]]] = {
    TaxYearState.OPEN: frozenset({TaxYearState.FILED, TaxYearState.CLOSED}),
    TaxYearState.FILED: frozenset({TaxYearState.OPEN, TaxYearState.CLOSED}),
    TaxYearState.CLOSED: frozenset({TaxYearState.OPEN}),
}


def update_year(
    db: Session,
    principal: Principal,
    year: int,
    body: TaxYearUpdate,
    *,
    request: HTTPConnection | None,
) -> TaxYearOut:
    row = require_year(db, principal, year, lock=True)
    fields = body.model_fields_set
    before = {"state": str(row.state), "filing_status": row.filing_status}
    if "state" in fields and body.state is not None and body.state is not row.state:
        if body.state not in _TRANSITIONS[row.state]:
            raise ProblemError(
                409,
                "tax-year-state-invalid",
                "State change not allowed",
                f"A tax year cannot go from {row.state} to {body.state}.",
            )
        if row.state is TaxYearState.CLOSED and not role_at_least(
            principal.role, HouseholdRole.ADMIN
        ):
            raise forbidden("Only an owner or admin can reopen a closed tax year.")
        row.state = body.state
    elif row.state is TaxYearState.CLOSED and fields - {"state"}:
        raise ProblemError(
            409, "tax-year-closed", "Tax year closed", "Reopen the tax year to change it."
        )
    if "filing_status" in fields:
        row.filing_status = check_filing_status(row.jurisdiction, body.filing_status)
    if "notes" in fields:
        row.notes = body.notes
    db.flush()
    _record(
        db,
        row.household_id,
        "tax_year.update",
        "tax_year",
        row.id,
        before=before,
        after={"state": str(row.state), "filing_status": row.filing_status},
        principal=principal,
        request=request,
        event="tax_year.updated",
        payload={"id": str(row.id), "year": row.year, "state": str(row.state)},
    )
    return year_out(db, row)


def delete_year(
    db: Session, principal: Principal, year: int, *, request: HTTPConnection | None
) -> None:
    row = require_year(db, principal, year, lock=True)
    db.delete(row)
    db.flush()
    _record(
        db,
        row.household_id,
        "tax_year.delete",
        "tax_year",
        row.id,
        before={"year": row.year},
        principal=principal,
        request=request,
        event="tax_year.deleted",
        payload={"id": str(row.id), "year": row.year},
    )


# ---------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------


def _form_title(jurisdiction: str, form_type: str) -> str | None:
    try:
        return form_def(jurisdiction, form_type).title
    except (UnknownFormError, UnknownJurisdictionError):
        return None


def _items(db: Session, tax_year: TaxYear) -> list[TaxChecklistItem]:
    return list(
        db.execute(
            select(TaxChecklistItem)
            .where(
                TaxChecklistItem.household_id == tax_year.household_id,
                TaxChecklistItem.tax_year_id == tax_year.id,
            )
            .order_by(TaxChecklistItem.expected_form, TaxChecklistItem.issuer, TaxChecklistItem.id)
        )
        .scalars()
        .all()
    )


def item_out(
    tax_year: TaxYear, item: TaxChecklistItem, visible: set[uuid.UUID]
) -> ChecklistItemOut:
    return ChecklistItemOut(
        id=item.id,
        expected_form=item.expected_form,
        form_title=_form_title(tax_year.jurisdiction, item.expected_form),
        issuer=item.issuer,
        status=item.status,
        origin=item.origin,
        document_id=item.document_id if item.document_id in visible else None,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _visible_documents(
    db: Session, principal: Principal, ids: Iterable[uuid.UUID | None]
) -> set[uuid.UUID]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return set()
    return set(
        db.execute(
            select(Document.id).where(Document.id.in_(wanted), visible_document_filter(principal))
        ).scalars()
    )


def get_checklist(db: Session, principal: Principal, year: int) -> ChecklistOut:
    row = require_year(db, principal, year)
    items = _items(db, row)
    visible = _visible_documents(db, principal, (i.document_id for i in items))
    return ChecklistOut(
        year=row.year,
        items=[item_out(row, item, visible) for item in items],
        counts=checklist_counts(db, row),
    )


def learn_checklist(db: Session, tax_year: TaxYear) -> int:
    """Expect this year what was received the year before (module doc).

    Sources: the prior year's checklist items that were ``received`` and the
    forms parsed with the prior year printed on them (form type and issuer).
    Items already present (same form, same issuer key) are left alone.
    Returns the number of items added (origin ``prior_year``).
    """
    household_id = tax_year.household_id
    prior = get_year_row(db, household_id, tax_year.year - 1)
    wanted: dict[tuple[str, str], tuple[str, str | None]] = {}
    if prior is not None:
        for item in _items(db, prior):
            if item.status is ChecklistStatus.RECEIVED:
                wanted[(item.expected_form, issuer_key(item.issuer))] = (
                    item.expected_form,
                    item.issuer,
                )
    forms = db.execute(
        select(TaxFormData.form_type, TaxFormData.issuer_name).where(
            TaxFormData.household_id == household_id,
            TaxFormData.tax_year == tax_year.year - 1,
            TaxFormData.jurisdiction == tax_year.jurisdiction,
        )
    ).all()
    for form_type, issuer in forms:
        wanted.setdefault((form_type, issuer_key(issuer)), (form_type, issuer))
    present = {(i.expected_form, issuer_key(i.issuer)) for i in _items(db, tax_year)}
    added = 0
    for key, (form_type, issuer) in sorted(wanted.items()):
        if key in present or _form_title(tax_year.jurisdiction, form_type) is None:
            continue
        db.add(
            TaxChecklistItem(
                household_id=household_id,
                tax_year_id=tax_year.id,
                expected_form=form_type,
                issuer=clean_issuer_name(issuer),
                status=ChecklistStatus.EXPECTED,
                origin=ChecklistOrigin.PRIOR_YEAR,
            )
        )
        present.add(key)
        added += 1
    db.flush()
    return added


def relearn(
    db: Session, principal: Principal, year: int, *, request: HTTPConnection | None
) -> LearnOut:
    row = require_year(db, principal, year, lock=True)
    added = learn_checklist(db, row)
    _record(
        db,
        row.household_id,
        "tax_checklist.learn",
        "tax_year",
        row.id,
        after={"added": added},
        principal=principal,
        request=request,
        event="tax_year.updated",
        payload={"id": str(row.id), "year": row.year},
    )
    return LearnOut(added=added)


def _duplicate_item() -> ProblemError:
    return ProblemError(
        409,
        "tax-checklist-item-exists",
        "Checklist item exists",
        "The year already expects this form from this issuer.",
    )


def _find_item(
    items: Iterable[TaxChecklistItem], form_type: str, issuer: str | None
) -> TaxChecklistItem | None:
    key = issuer_key(issuer)
    return next(
        (i for i in items if i.expected_form == form_type and issuer_key(i.issuer) == key), None
    )


def add_item(
    db: Session,
    principal: Principal,
    year: int,
    body: ChecklistItemCreate,
    *,
    request: HTTPConnection | None,
) -> ChecklistItemOut:
    row = require_year(db, principal, year, lock=True)
    form = check_form_type(row.jurisdiction, body.expected_form)
    issuer = clean_issuer_name(body.issuer)
    if _find_item(_items(db, row), form.form_type, issuer) is not None:
        raise _duplicate_item()
    item = TaxChecklistItem(
        household_id=row.household_id,
        tax_year_id=row.id,
        expected_form=form.form_type,
        issuer=issuer,
        status=ChecklistStatus.EXPECTED,
        origin=ChecklistOrigin.MANUAL,
    )
    db.add(item)
    db.flush()
    _record(
        db,
        row.household_id,
        "tax_checklist.create",
        "tax_checklist_item",
        item.id,
        after={"year": row.year, "form": item.expected_form},
        principal=principal,
        request=request,
        event="tax_year.updated",
        payload={"id": str(row.id), "year": row.year},
    )
    return item_out(row, item, set())


def require_item(
    db: Session, principal: Principal, year: int, item_id: uuid.UUID, *, lock: bool = False
) -> tuple[TaxYear, TaxChecklistItem]:
    row = require_year(db, principal, year)
    query = select(TaxChecklistItem).where(
        TaxChecklistItem.household_id == row.household_id,
        TaxChecklistItem.tax_year_id == row.id,
        TaxChecklistItem.id == item_id,
    )
    item = db.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if item is None:
        raise checklist_item_not_found()
    return row, item


def update_item(
    db: Session,
    principal: Principal,
    year: int,
    item_id: uuid.UUID,
    body: ChecklistItemUpdate,
    *,
    request: HTTPConnection | None,
) -> ChecklistItemOut:
    row, item = require_item(db, principal, year, item_id, lock=True)
    fields = body.model_fields_set
    before = {"status": str(item.status)}
    if "issuer" in fields:
        issuer = clean_issuer_name(body.issuer)
        other = _find_item(
            (i for i in _items(db, row) if i.id != item.id), item.expected_form, issuer
        )
        if other is not None:
            raise _duplicate_item()
        item.issuer = issuer
    status = body.status if "status" in fields and body.status is not None else item.status
    document_id = item.document_id
    if "document_id" in fields:
        document_id = body.document_id
        if document_id is not None:
            get_visible_document(db, principal, document_id)
            if "status" not in fields:
                status = ChecklistStatus.RECEIVED
    if status is not ChecklistStatus.RECEIVED:
        if "document_id" in fields and body.document_id is not None:
            raise ProblemError(
                422,
                "tax-checklist-document-needs-received",
                "Document needs status received",
                "Only a received item names the document holding the form.",
            )
        document_id = None
    item.status = status
    item.document_id = document_id
    db.flush()
    _record(
        db,
        row.household_id,
        "tax_checklist.update",
        "tax_checklist_item",
        item.id,
        before=before,
        after={"status": str(item.status), "document": item.document_id is not None},
        principal=principal,
        request=request,
        event="tax_year.updated",
        payload={"id": str(row.id), "year": row.year},
    )
    visible = _visible_documents(db, principal, [item.document_id])
    return item_out(row, item, visible)


def delete_item(
    db: Session,
    principal: Principal,
    year: int,
    item_id: uuid.UUID,
    *,
    request: HTTPConnection | None,
) -> None:
    row, item = require_item(db, principal, year, item_id, lock=True)
    db.delete(item)
    db.flush()
    _record(
        db,
        row.household_id,
        "tax_checklist.delete",
        "tax_checklist_item",
        item.id,
        before={"form": item.expected_form},
        principal=principal,
        request=request,
        event="tax_year.updated",
        payload={"id": str(row.id), "year": row.year},
    )


def mark_received(
    db: Session,
    tax_year: TaxYear,
    form_type: str,
    issuer: str | None,
    document_id: uuid.UUID,
) -> TaxChecklistItem:
    """Tick the checklist off for a parsed form (``app.domain.tax.ingest``).

    The expected item of the same form and issuer (issuer key) is marked
    ``received`` with the document; failing that an expected item of the
    form without issuer, or the only expected item of the form when the
    issuer could not be read; otherwise a ``detected`` item is added.
    """
    items = [i for i in _items(db, tax_year) if i.expected_form == form_type]
    key = issuer_key(issuer)
    expected = [i for i in items if i.status is ChecklistStatus.EXPECTED]
    item = next((i for i in items if key and issuer_key(i.issuer) == key), None)
    if item is None:
        item = next((i for i in expected if i.issuer is None), None)
    if item is None and not key and len(expected) == 1:
        item = expected[0]
    if item is None and not key:
        item = next((i for i in items if i.issuer is None), None)
    if item is None:
        item = TaxChecklistItem(
            household_id=tax_year.household_id,
            tax_year_id=tax_year.id,
            expected_form=form_type,
            issuer=clean_issuer_name(issuer),
            origin=ChecklistOrigin.DETECTED,
        )
        db.add(item)
    elif item.issuer is None and issuer:
        item.issuer = clean_issuer_name(issuer)
    item.status = ChecklistStatus.RECEIVED
    item.document_id = document_id
    db.flush()
    return item


# ---------------------------------------------------------------------------
# Form data and category map
# ---------------------------------------------------------------------------

_MONTH_CODES: Final = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)


def coverage_months(form_type: str, boxes: dict[str, Decimal]) -> list[str] | None:
    """Months with coverage on a 1095 (``cov_*`` marks, or 1095-A premiums in column A)."""
    if not form_type.startswith("1095"):
        return None
    if boxes.get("cov_ALL", Decimal(0)) > 0:
        return list(_MONTH_CODES)
    months = [m for m in _MONTH_CODES if boxes.get(f"cov_{m}", Decimal(0)) > 0]
    if form_type == "1095-A":
        months = [
            m for index, m in enumerate(_MONTH_CODES) if boxes.get(f"{21 + index}A", Decimal(0)) > 0
        ]
    return months


def box_amount(value: Decimal) -> str:
    """A box amount for display: two decimals unless more are significant."""
    cents = value.quantize(Decimal("0.01"))
    return f"{cents:f}" if cents == value else f"{value.normalize():f}"


def box_label(jurisdiction: str, form_type: str, key: str) -> str:
    try:
        form = form_def(jurisdiction, form_type)
    except (UnknownFormError, UnknownJurisdictionError):
        return key
    box = form.box(key) or form.box(key.partition("_")[0])
    if box is None:
        return key
    code = key.partition("_")[2]
    return f"{box.label} ({code})" if code else box.label


def form_data_out(row: TaxFormData) -> FormDataOut:
    boxes = form_boxes(row)
    return FormDataOut(
        id=row.id,
        document_id=row.document_id,
        form_type=row.form_type,
        form_index=row.form_index,
        tax_year=row.tax_year,
        issuer_name=row.issuer_name,
        source=row.source,
        confidence=f"{row.confidence:f}" if row.confidence is not None else None,
        boxes=[
            FormBoxOut(
                box=key,
                label=box_label(row.jurisdiction, row.form_type, key),
                amount=box_amount(value),
            )
            for key, value in sorted(boxes.items())
        ],
        coverage_months=coverage_months(row.form_type, boxes),
    )


def visible_form_rows(db: Session, principal: Principal, tax_year: TaxYear) -> list[TaxFormData]:
    """Form data of the year printed on the form, of documents the caller may see."""
    return list(
        db.execute(
            select(TaxFormData)
            .join(
                Document,
                (Document.household_id == TaxFormData.household_id)
                & (Document.id == TaxFormData.document_id),
            )
            .where(
                TaxFormData.household_id == tax_year.household_id,
                TaxFormData.tax_year == tax_year.year,
                TaxFormData.jurisdiction == tax_year.jurisdiction,
                visible_document_filter(principal),
            )
            .order_by(TaxFormData.form_type, TaxFormData.issuer_name, TaxFormData.id)
        )
        .scalars()
        .all()
    )


def list_forms(db: Session, principal: Principal, year: int) -> FormDataList:
    row = require_year(db, principal, year)
    return FormDataList(
        year=row.year, items=[form_data_out(f) for f in visible_form_rows(db, principal, row)]
    )


def require_category(db: Session, principal: Principal, category_id: uuid.UUID) -> Category:
    return _category(db, _household(principal), category_id)


def _category(db: Session, household_id: uuid.UUID, category_id: uuid.UUID) -> Category:
    category = db.execute(
        select(Category).where(Category.household_id == household_id, Category.id == category_id)
    ).scalar_one_or_none()
    if category is None:
        raise ProblemError(404, "category-not-found", "Category not found")
    return category


def _map_out(row: TaxCategoryMap, category: Category) -> CategoryMapOut:
    return CategoryMapOut(
        category_id=row.category_id,
        category_name=category.name,
        tax_treatment=category.tax_treatment,
        jurisdiction=row.jurisdiction,
        schedule=row.schedule,
        line=row.line,
    )


def list_category_map(
    db: Session, principal: Principal, jurisdiction: str | None
) -> CategoryMapList:
    query = (
        select(TaxCategoryMap, Category)
        .join(
            Category,
            (Category.household_id == TaxCategoryMap.household_id)
            & (Category.id == TaxCategoryMap.category_id),
        )
        .where(TaxCategoryMap.household_id == _household(principal))
        .order_by(TaxCategoryMap.schedule, TaxCategoryMap.line, Category.name)
    )
    if jurisdiction is not None:
        query = query.where(TaxCategoryMap.jurisdiction == jurisdiction)
    return CategoryMapList(items=[_map_out(m, c) for m, c in db.execute(query).all()])


def put_category_map(
    db: Session,
    principal: Principal,
    category_id: uuid.UUID,
    body: CategoryMapIn,
    *,
    request: HTTPConnection | None,
) -> CategoryMapOut:
    household_id = _household(principal)
    category = _category(db, household_id, category_id)
    jurisdiction = check_jurisdiction(body.jurisdiction)
    row = db.execute(
        select(TaxCategoryMap)
        .where(
            TaxCategoryMap.household_id == household_id,
            TaxCategoryMap.category_id == category_id,
            TaxCategoryMap.jurisdiction == jurisdiction,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        row = TaxCategoryMap(
            household_id=household_id, category_id=category_id, jurisdiction=jurisdiction
        )
        db.add(row)
    row.schedule = body.schedule.strip()
    row.line = body.line.strip() if body.line else None
    db.flush()
    _record(
        db,
        household_id,
        "tax_category_map.put",
        "category",
        category_id,
        after={"jurisdiction": jurisdiction, "schedule": row.schedule, "line": row.line},
        principal=principal,
        request=request,
        event="tax_category_map.updated",
        payload={"category_id": str(category_id)},
    )
    return _map_out(row, category)


def delete_category_map(
    db: Session,
    principal: Principal,
    category_id: uuid.UUID,
    jurisdiction: str,
    *,
    request: HTTPConnection | None,
) -> None:
    household_id = _household(principal)
    _category(db, household_id, category_id)
    row = db.execute(
        select(TaxCategoryMap).where(
            TaxCategoryMap.household_id == household_id,
            TaxCategoryMap.category_id == category_id,
            TaxCategoryMap.jurisdiction == jurisdiction,
        )
    ).scalar_one_or_none()
    if row is None:
        raise ProblemError(404, "tax-category-map-not-found", "No schedule hint for the category")
    db.delete(row)
    db.flush()
    _record(
        db,
        household_id,
        "tax_category_map.delete",
        "category",
        category_id,
        before={"jurisdiction": jurisdiction},
        principal=principal,
        request=request,
        event="tax_category_map.updated",
        payload={"category_id": str(category_id)},
    )

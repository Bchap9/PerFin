# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parsed tax forms into the tax vault (worker; INGESTION.md "Parse", DATA_MODEL.md "Tax vault").

:func:`import_tax_forms` runs in the ingestion job's transaction (household
RLS context set) for a document the tax form parser read
(``extraction.parse.tax_forms``, amounts only):

* the document becomes ``kind = tax_form`` with the tax year printed on its
  first form;
* earlier form data of the document is replaced (a re-parse never doubles
  it), then each form is written through
  :func:`~app.domain.tax.service.record_form_data` (identity boxes cannot
  get through it either), filed under the household's tax year of the year
  printed on it, which is created (and its checklist learned from the year
  before) when it does not exist yet;
* the year's checklist is ticked off (:func:`~app.domain.tax.service.mark_received`:
  the expected item of that form and issuer becomes ``received`` with the
  document, or a ``detected`` item is added);
* a form without a readable tax year, or with a field below the confidence
  threshold, raises a ``low_confidence_field`` review item naming the fields
  (never their values) and leaves the document in ``needs_review``.

Audited as ``tax_form.import`` (form types and counts only) and announced as
``tax_year.updated`` for each year touched.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.events import emit
from app.domain.documents import repo
from app.domain.documents.models import Document, DocumentKind, ReviewKind
from app.domain.tax.models import FormDataSource, TaxFormData, TaxYear
from app.domain.tax.service import ensure_year, mark_received, record_form_data
from app.ingestion.sandbox.protocol import TaxFormRead


@dataclass
class TaxImportResult:
    review: bool = False
    summary: dict[str, Any] = field(default_factory=dict)


def _confidence(form: TaxFormRead) -> Decimal | None:
    values = [v for k, v in form.confidence.items() if k != "issuer"]
    if not values:
        return None
    return Decimal(str(round(min(values), 4)))


def import_tax_forms(
    db: Session, document: Document, forms: Sequence[TaxFormRead]
) -> TaxImportResult:
    """Record ``forms`` read from ``document`` (module doc)."""
    household_id = document.household_id
    result = TaxImportResult()
    document.kind = DocumentKind.TAX_FORM
    years = [f.tax_year for f in forms if f.tax_year is not None]
    if years and document.tax_year is None:
        document.tax_year = years[0]
    db.execute(
        delete(TaxFormData).where(
            TaxFormData.household_id == household_id, TaxFormData.document_id == document.id
        )
    )
    touched: dict[int, TaxYear] = {}
    recorded: list[str] = []
    low: list[str] = []
    for form in forms:
        year = form.tax_year if form.tax_year is not None else document.tax_year
        if year is None:
            low.append(f"{form.form_type}:tax_year")
            continue
        tax_year, created = ensure_year(db, household_id, year, jurisdiction=form.jurisdiction)
        if created:
            audit(
                db,
                action="tax_year.create",
                target_type="tax_year",
                target_id=tax_year.id,
                after={"year": year, "jurisdiction": tax_year.jurisdiction, "from": "document"},
                household_id=household_id,
            )
        boxes: dict[str, object] = dict(form.boxes)
        record_form_data(
            db,
            household_id,
            document_id=document.id,
            jurisdiction=form.jurisdiction,
            form_type=form.form_type,
            tax_year=year,
            boxes=boxes,
            source=FormDataSource.PARSED,
            issuer_name=form.issuer_name,
            form_index=form.form_index,
            tax_year_id=tax_year.id,
            confidence=_confidence(form),
        )
        mark_received(db, tax_year, form.form_type, form.issuer_name, document.id)
        touched[year] = tax_year
        recorded.append(form.form_type)
        low.extend(f"{form.form_type}:{name}" for name in form.low_confidence)
    if low or not recorded:
        repo.add_review_item(
            db,
            household_id,
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "tax_form_low_confidence" if recorded else "tax_form_unread",
                "fields": sorted(set(low))[:50],
            },
            document_id=document.id,
        )
        result.review = True
    audit(
        db,
        action="tax_form.import",
        target_type="document",
        target_id=document.id,
        after={"forms": recorded, "years": sorted(touched), "review": result.review},
        household_id=household_id,
    )
    for year, tax_year in sorted(touched.items()):
        emit(db, household_id, "tax_year.updated", {"id": str(tax_year.id), "year": year})
    result.summary = {"tax_forms": len(recorded), "tax_years": sorted(touched)}
    return result

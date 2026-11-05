# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic tax vault rows for tests (Phase 6).

Flush-only builders like ``tests/fixtures/ledger.py``. Form data goes
through the real ``app.domain.tax.service.record_form_data`` so identity
boxes are discarded exactly as in production; :func:`synthetic_w2_boxes`
generates a full W-2 box set **including** made-up identity values (a
``900-``-prefixed SSN-like string, an invented EIN, name and address) so
tests can prove they never reach the database.
"""

from __future__ import annotations

import hashlib
import random
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.crypto import Keyring
from app.core.ids import uuid7
from app.domain.documents.blobstore import BlobStore, EncryptingWriter
from app.domain.documents.models import Blob, BlobArea, Document
from app.domain.portability.import_ import blob_row
from app.domain.tax.models import (
    ChecklistOrigin,
    ChecklistStatus,
    FormDataSource,
    TaxCategoryMap,
    TaxChecklistItem,
    TaxExport,
    TaxExportState,
    TaxFormData,
    TaxYear,
    TaxYearState,
)
from app.domain.tax.service import record_form_data
from app.domain.transactions.models import Category
from tests.fixtures.factories import unique_name
from tests.fixtures.ledger import household_context


def make_tax_year(
    db: Session,
    household_id: uuid.UUID,
    *,
    year: int = 2025,
    jurisdiction: str = "US",
    filing_status: str | None = "single",
    state: TaxYearState | str = TaxYearState.OPEN,
) -> TaxYear:
    with household_context(db, household_id):
        row = TaxYear(
            household_id=household_id,
            year=year,
            jurisdiction=jurisdiction,
            filing_status=filing_status,
            state=TaxYearState(state),
        )
        db.add(row)
        db.flush()
    return row


def make_checklist_item(
    db: Session,
    tax_year: TaxYear,
    *,
    expected_form: str = "W-2",
    issuer: str | None = None,
    status: ChecklistStatus | str = ChecklistStatus.EXPECTED,
    origin: ChecklistOrigin | str = ChecklistOrigin.MANUAL,
    document: Document | None = None,
) -> TaxChecklistItem:
    with household_context(db, tax_year.household_id):
        row = TaxChecklistItem(
            household_id=tax_year.household_id,
            tax_year_id=tax_year.id,
            expected_form=expected_form,
            issuer=issuer if issuer is not None else unique_name("Employer"),
            status=ChecklistStatus(status),
            origin=ChecklistOrigin(origin),
            document_id=document.id if document is not None else None,
        )
        db.add(row)
        db.flush()
    return row


def make_tax_category_map(
    db: Session,
    category: Category,
    *,
    jurisdiction: str = "US",
    schedule: str = "Schedule A",
    line: str | None = "5a",
) -> TaxCategoryMap:
    with household_context(db, category.household_id):
        row = TaxCategoryMap(
            household_id=category.household_id,
            category_id=category.id,
            jurisdiction=jurisdiction,
            schedule=schedule,
            line=line,
        )
        db.add(row)
        db.flush()
    return row


def synthetic_w2_boxes(seed: int = 0) -> dict[str, object]:
    """A W-2 as a parser would read it, identity boxes included (all invented)."""
    rng = random.Random(seed)
    wages = Decimal(rng.randint(30_000_00, 150_000_00)).scaleb(-2)
    federal = (wages * Decimal("0.12")).quantize(Decimal("0.01"))
    ss_tax = (wages * Decimal("0.062")).quantize(Decimal("0.01"))
    medicare = (wages * Decimal("0.0145")).quantize(Decimal("0.01"))
    return {
        "a": f"900-{rng.randint(70, 88)}-{rng.randint(1000, 9999)}",
        "b": f"00-{rng.randint(1000000, 9999999)}",
        "c": "Synthetic Employer Inc, 1 Example Way, Testville ZZ 00000",
        "d": f"CTRL{rng.randint(10000, 99999)}",
        "e": "Pat Example",
        "f": "2 Sample Street, Testville ZZ 00000",
        "1": f"{wages}",
        "2": f"{federal}",
        "3": f"{wages}",
        "4": f"{ss_tax}",
        "5": f"{wages}",
        "6": f"{medicare}",
        "12_D": "5000.00",
        "13": True,
        "14": "SDI 123.45",
        "15": "ZZ 123-4567-8",
        "16": f"{wages}",
        "17": f"{(wages * Decimal('0.05')).quantize(Decimal('0.01'))}",
    }


def make_form_data(
    db: Session,
    document: Document,
    *,
    form_type: str = "W-2",
    tax_year: int = 2025,
    boxes: dict[str, object] | None = None,
    issuer_name: str | None = "Synthetic Employer Inc",
    tax_year_row: TaxYear | None = None,
    form_index: int = 0,
    jurisdiction: str = "US",
) -> TaxFormData:
    with household_context(db, document.household_id):
        row = record_form_data(
            db,
            document.household_id,
            document_id=document.id,
            jurisdiction=jurisdiction,
            form_type=form_type,
            tax_year=tax_year,
            boxes=synthetic_w2_boxes() if boxes is None else boxes,
            source=FormDataSource.PARSED,
            issuer_name=issuer_name,
            form_index=form_index,
            tax_year_id=tax_year_row.id if tax_year_row is not None else None,
        )
    return row


def free_tax_year(db: Session, household_id: uuid.UUID) -> int:
    """A random year (1901..2199) the household has no tax year for."""
    with household_context(db, household_id):
        taken = set(
            db.execute(select(TaxYear.year).where(TaxYear.household_id == household_id)).scalars()
        )
    while True:
        year = 1901 + secrets.randbelow(299)
        if year not in taken:
            return year


def make_tax_export(
    db: Session,
    store: BlobStore,
    keyring: Keyring,
    tax_year: TaxYear,
    requested_by: uuid.UUID | None,
    *,
    data: bytes = b"PK\x05\x06" + bytes(18),
    state: TaxExportState = TaxExportState.READY,
) -> TaxExport:
    """A finished accountant export whose (synthetic) bytes are encrypted in ``store``."""
    with EncryptingWriter(store, keyring, uuid7(), area=BlobArea.PERMANENT) as writer:
        writer.write(data)
        sealed = writer.finish()
    now = clock.utcnow()
    household_id = tax_year.household_id
    with household_context(db, household_id):
        db.execute(insert(Blob), [blob_row(household_id, sealed)])
        row = TaxExport(
            household_id=household_id,
            tax_year_id=tax_year.id,
            year=tax_year.year,
            state=state,
            job_id=uuid7(),
            requested_by=requested_by,
            blob_id=sealed.blob_id,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            finished_at=now,
            expires_at=now + timedelta(hours=1),
            summary={"counts": {}},
        )
        db.add(row)
        db.flush()
    return row

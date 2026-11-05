# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tax vault tables (DATA_MODEL.md "Tax vault").

* ``tax_year``: one per household and calendar year. ``jurisdiction`` names a
  registered form set (``app.domain.tax.forms``, ``"US"``),
  ``filing_status`` one of that set's statuses (both validated by the
  service; the database checks the format). ``state`` is the workflow state
  open -> filed -> closed (not a US state).
* ``tax_checklist_item``: a form the household expects for the year
  (``expected_form`` a form type of the jurisdiction, ``issuer`` the
  employer's/payer's display name), its status and, once received, the
  ``document`` holding it (``SET NULL`` when the document is deleted).
  ``origin`` tells items learned from prior years from manual ones.
* ``tax_category_map``: per category and jurisdiction, the schedule/line a
  deductible or taxable category rolls up to (a hint for the accountant
  export, not tax advice).
* ``tax_export``: one accountant export of a tax year (zip of the year's
  originals plus a summary CSV and PDF), built by the ``tax.export`` job
  into the blob store (encrypted at rest like a full export), downloadable
  once by the owner/admin who asked for it until ``expires_at``; the blob is
  crypto-shredded on download or expiry.
* ``tax_form_data``: the amounts read from one form in a document (a
  consolidated 1099 holds several forms: ``form_index`` numbers them per
  type). ``boxes`` maps box ids to decimal strings and holds **only
  amounts**: identity boxes (SSN/EIN/TIN, names, addresses, account numbers)
  are discarded by ``app.domain.tax.forms.sanitize_boxes`` before a row is
  written, and a check constraint refuses any value that is not a decimal
  string and any key that is not a box id. ``issuer_name`` is the only text
  kept from the form. Deleting the document deletes its form data.

All ``HouseholdScoped`` (forced RLS) with household-consistent references.
Roll-ups over a tax year include only the accounts a member or viewer may
see (``app.domain.accounts.visibility``).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# Referenced tables must be in the metadata wherever these models are used.
import app.auth.models
import app.domain.documents.models
import app.domain.transactions.models  # noqa: F401
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import CONFIDENCE, household_fk, household_key, household_ref_fk, pg_enum

ISSUER_MAX = 200
NOTES_MAX = 2000
FORM_TYPE_MAX = 32
JURISDICTION_MAX = 8
FILING_STATUS_MAX = 40
SCHEDULE_MAX = 64
LINE_MAX = 32
YEAR_MIN = 1900
YEAR_MAX = 2200
#: Most boxes one form row may hold (1095-A has 39).
BOXES_MAX = 128

JURISDICTION_CHECK: Final = "^[A-Z]{2}(-[A-Z0-9]{1,3})?$"
FORM_TYPE_CHECK: Final = "^[A-Z0-9][A-Za-z0-9-]{0,31}$"
#: Every key a box id (``app.domain.tax.forms.base.STORED_KEY_RE``), every
#: value a decimal string that fits ``NUMERIC(20, 6)``.
BOXES_CHECK: Final = (
    "jsonb_typeof(boxes) = 'object' AND NOT jsonb_path_exists(boxes, "
    '\'$.keyvalue() ? (!(@.key like_regex "^[A-Za-z0-9_]{1,32}$") '
    '|| @.value.type() != "string" '
    '|| !(@.value like_regex "^-?[0-9]{1,14}([.][0-9]{1,6})?$"))\')'
)


class TaxYearState(enum.StrEnum):
    OPEN = "open"
    FILED = "filed"
    CLOSED = "closed"


class ChecklistStatus(enum.StrEnum):
    EXPECTED = "expected"
    RECEIVED = "received"
    NOT_APPLICABLE = "not_applicable"


class ChecklistOrigin(enum.StrEnum):
    MANUAL = "manual"
    PRIOR_YEAR = "prior_year"
    DETECTED = "detected"


class FormDataSource(enum.StrEnum):
    PARSED = "parsed"
    MANUAL = "manual"


class TaxYear(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "tax_year"
    __table_args__ = (
        household_key("tax_year"),
        UniqueConstraint("household_id", "year", name="uq_tax_year_household_year"),
        CheckConstraint(f"year BETWEEN {YEAR_MIN} AND {YEAR_MAX}", name="year_range"),
        CheckConstraint(f"jurisdiction ~ '{JURISDICTION_CHECK}'", name="jurisdiction_format"),
        CheckConstraint(
            "filing_status IS NULL OR filing_status ~ '^[a-z_]{1,40}$'",
            name="filing_status_format",
        ),
    )

    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(JURISDICTION_MAX), nullable=False)
    filing_status: Mapped[str | None] = mapped_column(String(FILING_STATUS_MAX))
    state: Mapped[TaxYearState] = mapped_column(
        pg_enum(TaxYearState, "tax_year_state"),
        nullable=False,
        default=TaxYearState.OPEN,
        server_default=TaxYearState.OPEN.value,
    )
    notes: Mapped[str | None] = mapped_column(
        String(NOTES_MAX), info={"classification": "free_text"}
    )


class TaxChecklistItem(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "tax_checklist_item"
    __table_args__ = (
        household_fk("tax_checklist_item", "tax_year_id", "tax_year"),
        household_ref_fk("tax_checklist_item", "document_id", "document"),
        UniqueConstraint(
            "tax_year_id",
            "expected_form",
            "issuer",
            name="uq_tax_checklist_item_form_issuer",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(f"expected_form ~ '{FORM_TYPE_CHECK}'", name="form_type_format"),
        CheckConstraint(
            "document_id IS NULL OR status = 'received'", name="document_only_when_received"
        ),
    )

    tax_year_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    expected_form: Mapped[str] = mapped_column(String(FORM_TYPE_MAX), nullable=False)
    issuer: Mapped[str | None] = mapped_column(String(ISSUER_MAX))
    status: Mapped[ChecklistStatus] = mapped_column(
        pg_enum(ChecklistStatus, "tax_checklist_status"),
        nullable=False,
        default=ChecklistStatus.EXPECTED,
        server_default=ChecklistStatus.EXPECTED.value,
    )
    origin: Mapped[ChecklistOrigin] = mapped_column(
        pg_enum(ChecklistOrigin, "tax_checklist_origin"),
        nullable=False,
        default=ChecklistOrigin.MANUAL,
        server_default=ChecklistOrigin.MANUAL.value,
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document.id", ondelete="SET NULL"), index=True
    )


class TaxCategoryMap(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "tax_category_map"
    __table_args__ = (
        household_fk("tax_category_map", "category_id", "category"),
        UniqueConstraint(
            "category_id", "jurisdiction", name="uq_tax_category_map_category_jurisdiction"
        ),
        CheckConstraint(f"jurisdiction ~ '{JURISDICTION_CHECK}'", name="jurisdiction_format"),
    )

    category_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    jurisdiction: Mapped[str] = mapped_column(String(JURISDICTION_MAX), nullable=False)
    #: e.g. ``"Schedule A"``, ``"Schedule C"``, ``"Form 1040"``.
    schedule: Mapped[str] = mapped_column(String(SCHEDULE_MAX), nullable=False)
    #: e.g. ``"5a"``, ``"Line 12"``.
    line: Mapped[str | None] = mapped_column(String(LINE_MAX))


class TaxFormData(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "tax_form_data"
    __table_args__ = (
        household_fk("tax_form_data", "document_id", "document"),
        household_ref_fk("tax_form_data", "tax_year_id", "tax_year"),
        UniqueConstraint(
            "document_id", "form_type", "form_index", name="uq_tax_form_data_document_form"
        ),
        Index("ix_tax_form_data_household_year", "household_id", "tax_year"),
        CheckConstraint(f"form_type ~ '{FORM_TYPE_CHECK}'", name="form_type_format"),
        CheckConstraint(f"jurisdiction ~ '{JURISDICTION_CHECK}'", name="jurisdiction_format"),
        CheckConstraint(f"tax_year BETWEEN {YEAR_MIN} AND {YEAR_MAX}", name="year_range"),
        CheckConstraint("form_index >= 0", name="form_index_non_negative"),
        CheckConstraint(BOXES_CHECK, name="boxes_amounts_only"),
        CheckConstraint(
            f"jsonb_array_length(jsonb_path_query_array(boxes, '$.keyvalue()')) <= {BOXES_MAX}",
            name="boxes_max",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    #: The year printed on the form.
    tax_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: The ``tax_year`` row it was filed under, if any (``SET NULL`` on delete).
    tax_year_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tax_year.id", ondelete="SET NULL"), index=True
    )
    jurisdiction: Mapped[str] = mapped_column(String(JURISDICTION_MAX), nullable=False)
    form_type: Mapped[str] = mapped_column(String(FORM_TYPE_MAX), nullable=False)
    form_index: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default=text("0")
    )
    issuer_name: Mapped[str | None] = mapped_column(String(ISSUER_MAX))
    boxes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    source: Mapped[FormDataSource] = mapped_column(
        pg_enum(FormDataSource, "tax_form_data_source"), nullable=False
    )
    confidence: Mapped[Decimal | None] = mapped_column(CONFIDENCE)


class TaxExportState(enum.StrEnum):
    PENDING = "pending"
    READY = "ready"
    DOWNLOADED = "downloaded"
    EXPIRED = "expired"
    FAILED = "failed"


class TaxExport(UUIDPk, Timestamps, HouseholdScoped, Base):
    """One accountant export of a tax year (module doc)."""

    __tablename__ = "tax_export"
    __table_args__ = (
        household_key("tax_export"),
        household_ref_fk("tax_export", "tax_year_id", "tax_year"),
        household_ref_fk("tax_export", "blob_id", "blob"),
        CheckConstraint(f"year BETWEEN {YEAR_MIN} AND {YEAR_MAX}", name="year_range"),
        CheckConstraint("size IS NULL OR size >= 0", name="size_non_negative"),
        CheckConstraint("sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        CheckConstraint("jsonb_typeof(summary) = 'object'", name="summary_object"),
        Index("ix_tax_export_household_state", "household_id", "state"),
    )

    tax_year_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tax_year.id", ondelete="SET NULL"), index=True
    )
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    state: Mapped[TaxExportState] = mapped_column(
        pg_enum(TaxExportState, "tax_export_state"),
        nullable=False,
        default=TaxExportState.PENDING,
        server_default=TaxExportState.PENDING.value,
    )
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), index=True
    )
    blob_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blob.id", ondelete="SET NULL"), index=True
    )
    size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(CHAR(64))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(64))
    #: Counts only (documents, forms, categories).
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

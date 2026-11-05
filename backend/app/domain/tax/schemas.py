# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request and response bodies of the tax vault API (API.md ``/tax-years``).

Inputs forbid unknown fields (SECURITY.md F4). Amounts are decimal strings;
money is ``{"amount", "currency"}``. Form data carries **amounts by box
only**: no identity value has a field to travel in.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.money import Money
from app.domain.tax.models import (
    FILING_STATUS_MAX,
    FORM_TYPE_MAX,
    ISSUER_MAX,
    LINE_MAX,
    NOTES_MAX,
    SCHEDULE_MAX,
    YEAR_MAX,
    YEAR_MIN,
    ChecklistOrigin,
    ChecklistStatus,
    FormDataSource,
    TaxExportState,
    TaxYearState,
)
from app.domain.transactions.models import TaxTreatment

EXPORT_JOB_TYPE: Final = "tax.export"
EXPORT_EXPIRE_JOB_TYPE: Final = "tax.export_expire"
REMINDER_DISPATCH_JOB_TYPE: Final = "tax_reminders"
REMINDER_JOB_TYPE: Final = "tax.reminders"

Year = Annotated[int, Field(ge=YEAR_MIN, le=YEAR_MAX)]
Jurisdiction = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")]
FormType = Annotated[
    str, StringConstraints(pattern=r"^[A-Z0-9][A-Za-z0-9-]*$", max_length=FORM_TYPE_MAX)
]
FilingStatus = Annotated[str, StringConstraints(pattern=r"^[a-z_]+$", max_length=FILING_STATUS_MAX)]
Issuer = Annotated[str, StringConstraints(min_length=1, max_length=ISSUER_MAX)]
Notes = Annotated[str, StringConstraints(max_length=NOTES_MAX)]
Schedule = Annotated[str, StringConstraints(min_length=1, max_length=SCHEDULE_MAX)]
Line = Annotated[str, StringConstraints(min_length=1, max_length=LINE_MAX)]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Tax years
# ---------------------------------------------------------------------------


class TaxYearCreate(_Input):
    year: Year
    jurisdiction: Jurisdiction = "US"
    filing_status: FilingStatus | None = None
    notes: Notes | None = None


class TaxYearUpdate(_Input):
    """Fields left out stay as they are; ``null`` clears filing status or notes."""

    filing_status: FilingStatus | None = None
    notes: Notes | None = None
    state: TaxYearState | None = None


class ChecklistCounts(_Output):
    expected: int
    received: int
    not_applicable: int


class TaxYearOut(_Output):
    id: uuid.UUID
    year: int
    jurisdiction: str
    filing_status: str | None
    state: TaxYearState
    notes: str | None
    checklist: ChecklistCounts
    created_at: datetime
    updated_at: datetime


class TaxYearList(_Output):
    items: list[TaxYearOut]


# ---------------------------------------------------------------------------
# Checklist
# ---------------------------------------------------------------------------


class ChecklistItemCreate(_Input):
    expected_form: FormType
    issuer: Issuer | None = None


class ChecklistItemUpdate(_Input):
    """``status`` ``received`` may name the ``document_id`` holding the form."""

    status: ChecklistStatus | None = None
    issuer: Issuer | None = None
    document_id: uuid.UUID | None = None


class ChecklistItemOut(_Output):
    id: uuid.UUID
    expected_form: str
    form_title: str | None
    issuer: str | None
    status: ChecklistStatus
    origin: ChecklistOrigin
    #: Only when the caller may see the document.
    document_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class ChecklistOut(_Output):
    year: int
    items: list[ChecklistItemOut]
    counts: ChecklistCounts


class LearnOut(_Output):
    added: int


# ---------------------------------------------------------------------------
# Form data, category map, summary
# ---------------------------------------------------------------------------


class FormBoxOut(_Output):
    box: str
    label: str
    amount: str


class FormDataOut(_Output):
    id: uuid.UUID
    document_id: uuid.UUID
    form_type: str
    form_index: int
    tax_year: int
    issuer_name: str | None
    source: FormDataSource
    confidence: str | None
    boxes: list[FormBoxOut]
    #: 1095 forms: the months with coverage (``JAN``..``DEC``).
    coverage_months: list[str] | None = None


class FormDataList(_Output):
    year: int
    items: list[FormDataOut]


class CategoryMapIn(_Input):
    jurisdiction: Jurisdiction = "US"
    schedule: Schedule
    line: Line | None = None


class CategoryMapOut(_Output):
    category_id: uuid.UUID
    category_name: str
    tax_treatment: TaxTreatment | None
    jurisdiction: str
    schedule: str
    line: str | None


class CategoryMapList(_Output):
    items: list[CategoryMapOut]


class SummaryCategory(_Output):
    category_id: uuid.UUID | None
    category_name: str | None
    tax_treatment: TaxTreatment | None
    schedule: str | None
    line: str | None
    #: Every tax-relevant line of the category (treated category: all its
    #: lines; otherwise the splits flagged ``tax_flag``).
    total: Money
    #: Of which splits flagged ``tax_flag``.
    flagged: Money


class SummaryHint(_Output):
    schedule: str | None
    line: str | None
    total: Money


class SummaryForm(_Output):
    form_type: str
    count: int
    boxes: list[FormBoxOut]


class TaxSummaryOut(_Output):
    year: int
    jurisdiction: str
    currency: str
    #: True when the caller does not see every account (members/viewers with
    #: some accounts not granted): the totals cover visible accounts only.
    partial: bool
    by_treatment: dict[str, Money]
    categories: list[SummaryCategory]
    by_hint: list[SummaryHint]
    forms: list[SummaryForm]
    checklist: ChecklistCounts
    unconverted: list[Money]


# ---------------------------------------------------------------------------
# Accountant export
# ---------------------------------------------------------------------------


class TaxExportJob(BaseModel):
    """Payload of ``tax.export`` / ``tax.export_expire``."""

    model_config = ConfigDict(extra="forbid")

    export_id: uuid.UUID


class TaxReminderDispatch(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaxReminderJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: date


class TaxExportQueuedOut(_Output):
    job_id: uuid.UUID
    year: int
    state: TaxExportState


class TaxExportOut(_Output):
    job_id: uuid.UUID
    year: int
    state: TaxExportState
    size: int | None
    sha256: str | None
    created_at: datetime
    finished_at: datetime | None
    expires_at: datetime | None
    downloaded_at: datetime | None
    error: str | None
    counts: dict[str, int]

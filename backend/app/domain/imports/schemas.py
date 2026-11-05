# SPDX-License-Identifier: AGPL-3.0-or-later
"""Column mappings, import previews and the apply-mapping job (API.md "Imports").

:class:`MappingSpec` is the JSON document stored in ``import_mapping.spec``.
Columns are zero-based indexes into the extracted table's rows. Exactly one
way of reading the amount is used:

* ``amount_column``: one signed column. ``sign`` says what a positive value
  means: ``inflow_positive`` (deposits positive: most bank exports) or
  ``outflow_positive`` (purchases positive: many card exports). With
  ``indicator_column`` the column holds an unsigned amount and the row is an
  outflow when the indicator cell is one of ``outflow_values`` (``DR``,
  ``Debit``; compared case-insensitively), an inflow otherwise.
* ``debit_column`` and/or ``credit_column``: unsigned outflow and inflow
  columns; the amount is ``credit - debit`` (signs in the cells are ignored).

``decimal_separator`` is ``.`` (``1,234.56``) or ``,`` (``1.234,56``);
currency symbols, spaces and apostrophes are ignored, ``(12.34)`` and
``12.34-`` are negative. ``date_format`` is one of :data:`DATE_FORMATS`
(month names are English three-letter abbreviations, whatever the locale).
``currency`` fixes the currency of every row (default: the account's);
``currency_column`` reads it per row (an empty cell means the account's
currency, a cell that is not a three-letter code makes the row unreadable);
a row in another currency than the account's is skipped and reported.
``skip_rows`` rows (a preamble) come before the header (``has_header``) or
the first data row.

``account_column`` (optional) holds an account number per row, for exports
that list several accounts in one file (a brokerage "Activity" export with
an Individual, a Roth IRA and an HSA account). The rows are grouped by that
column and each group is imported as its own statement, matched to an
account by the **last four digits** of the cell like an OFX statement
(``app.ingestion.match.accounts``); the cell text itself is never stored or
logged. Mappings saved before the field existed have no ``account_column``
and read exactly as before.

Inputs forbid unknown fields (SECURITY.md F4).
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.core.money import CurrencyCode
from app.domain.accounts.schemas import clean_text
from app.domain.documents.models import DocumentStatus
from app.domain.imports.models import NAME_MAX

#: Job type of ``POST /imports/{document_id}/apply-mapping``.
APPLY_MAPPING_JOB_TYPE: Final = "imports.apply_mapping"

MAX_PAGE: Final = 500
DEFAULT_PAGE: Final = 100
PREVIEW_MAX: Final = 500
PREVIEW_DEFAULT: Final = 50
MAX_COLUMN: Final = 1000
MAX_SKIP_ROWS: Final = 50
OUTFLOW_VALUES_MAX: Final = 10
#: Highest statement (group) index of a multi-account table an apply may name.
MAX_GROUP: Final = 10_000

#: Accepted ``date_format`` values and what they read (``D``/``M`` without
#: leading zeros are accepted too; a trailing time of day is ignored).
DATE_FORMATS: Final[dict[str, str]] = {
    "YYYY-MM-DD": "2026-01-31",
    "YYYY/MM/DD": "2026/01/31",
    "YYYY.MM.DD": "2026.01.31",
    "YYYYMMDD": "20260131",
    "MM/DD/YYYY": "01/31/2026",
    "MM/DD/YY": "01/31/26",
    "MM-DD-YYYY": "01-31-2026",
    "DD/MM/YYYY": "31/01/2026",
    "DD/MM/YY": "31/01/26",
    "DD-MM-YYYY": "31-01-2026",
    "DD.MM.YYYY": "31.01.2026",
    "DD.MM.YY": "31.01.26",
    "DD MMM YYYY": "31 Jan 2026",
    "DD-MMM-YYYY": "31-Jan-2026",
    "MMM DD, YYYY": "Jan 31, 2026",
}

DateFormat = Literal[
    "YYYY-MM-DD",
    "YYYY/MM/DD",
    "YYYY.MM.DD",
    "YYYYMMDD",
    "MM/DD/YYYY",
    "MM/DD/YY",
    "MM-DD-YYYY",
    "DD/MM/YYYY",
    "DD/MM/YY",
    "DD-MM-YYYY",
    "DD.MM.YYYY",
    "DD.MM.YY",
    "DD MMM YYYY",
    "DD-MMM-YYYY",
    "MMM DD, YYYY",
]

_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


class Sign(StrEnum):
    INFLOW_POSITIVE = "inflow_positive"
    OUTFLOW_POSITIVE = "outflow_positive"


def _fingerprint(value: str) -> str:
    if not _FINGERPRINT.fullmatch(value):
        raise ValueError("must be 64 lower-case hex digits")
    return value


def _indicator(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or not cleaned.isprintable():
        raise ValueError("must be non-empty printable text")
    return cleaned


Name = Annotated[str, Field(min_length=1, max_length=NAME_MAX), AfterValidator(clean_text)]
Column = Annotated[int, Field(ge=0, lt=MAX_COLUMN)]
Fingerprint = Annotated[str, Field(min_length=64, max_length=64), AfterValidator(_fingerprint)]
IndicatorValue = Annotated[str, Field(min_length=1, max_length=20), AfterValidator(_indicator)]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True)


class MappingSpec(BaseModel):
    """How to read one delimited layout (module doc)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    skip_rows: int = Field(default=0, ge=0, le=MAX_SKIP_ROWS)
    has_header: bool = True
    date_column: Column
    date_format: DateFormat
    amount_column: Column | None = None
    sign: Sign = Sign.INFLOW_POSITIVE
    indicator_column: Column | None = None
    outflow_values: list[IndicatorValue] = Field(
        default_factory=list, max_length=OUTFLOW_VALUES_MAX
    )
    debit_column: Column | None = None
    credit_column: Column | None = None
    decimal_separator: Literal[".", ","] = "."
    payee_column: Column | None = None
    memo_column: Column | None = None
    balance_column: Column | None = None
    currency_column: Column | None = None
    currency: CurrencyCode | None = None
    account_column: Column | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        split = self.debit_column is not None or self.credit_column is not None
        if (self.amount_column is None) == (not split):
            raise ValueError("give either amount_column or debit_column/credit_column")
        if self.indicator_column is not None:
            if self.amount_column is None:
                raise ValueError("indicator_column needs amount_column")
            if not self.outflow_values:
                raise ValueError("indicator_column needs outflow_values")
        elif self.outflow_values:
            raise ValueError("outflow_values need indicator_column")
        if self.currency is not None and self.currency_column is not None:
            raise ValueError("give currency or currency_column, not both")
        used = [c for c in self.columns().values() if c is not None]
        if len(used) != len(set(used)):
            raise ValueError("each column can be mapped once")
        return self

    def columns(self) -> dict[str, int | None]:
        """Every mapped column by role (``None`` when unused)."""
        return {
            "date": self.date_column,
            "amount": self.amount_column,
            "indicator": self.indicator_column,
            "debit": self.debit_column,
            "credit": self.credit_column,
            "payee": self.payee_column,
            "memo": self.memo_column,
            "balance": self.balance_column,
            "currency": self.currency_column,
            "account": self.account_column,
        }

    def max_column(self) -> int:
        return max(c for c in self.columns().values() if c is not None)


# ---------------------------------------------------------------------------
# Mappings API
# ---------------------------------------------------------------------------


class MappingCreate(_Input):
    """``POST /imports/mappings``.

    The header fingerprint comes from ``document_id`` (the server reads the
    header row ``spec.skip_rows`` of the document's extracted table) or is
    given as ``header_fingerprint``; exactly one of the two.
    """

    name: Name
    spec: MappingSpec
    document_id: uuid.UUID | None = None
    header_fingerprint: Fingerprint | None = None
    account_id: uuid.UUID | None = None
    institution_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if (self.document_id is None) == (self.header_fingerprint is None):
            raise ValueError("give exactly one of document_id and header_fingerprint")
        return self


class MappingUpdate(_Input):
    """``PATCH /imports/mappings/{id}``: only the named fields change.

    ``null`` clears ``account_id`` / ``institution_id``; ``document_id`` (or
    ``header_fingerprint``) recomputes (sets) the fingerprint.
    """

    name: Name | None = None
    spec: MappingSpec | None = None
    document_id: uuid.UUID | None = None
    header_fingerprint: Fingerprint | None = None
    account_id: uuid.UUID | None = None
    institution_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _fields(self) -> Self:
        given = self.model_fields_set
        if not given:
            raise ValueError("give at least one field to change")
        for name in given - {"account_id", "institution_id"}:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        if {"document_id", "header_fingerprint"} <= given:
            raise ValueError("give document_id or header_fingerprint, not both")
        return self


class MappingOut(_Output):
    id: uuid.UUID
    name: str
    institution_id: uuid.UUID | None
    account_id: uuid.UUID | None
    header_fingerprint: str
    spec: MappingSpec
    created_by: uuid.UUID | None
    use_count: int
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MappingPage(_Output):
    items: list[MappingOut]
    next_cursor: uuid.UUID | None


class ApplyMapping(_Input):
    """``POST /imports/{document_id}/apply-mapping``.

    ``mapping_id`` (CSV/TSV) names the mapping to read the table with (else
    the one whose fingerprint matches); ``account_id`` names the account to
    import into (else the document's, the mapping's or, for OFX/QFX/QIF, the
    account matched by its last four digits). The caller must be able to
    edit the target account. ``group`` (with ``account_id``) names the
    statement of a multi-account table the account is for (an
    ``unknown_account`` item's ``group``); without it a chosen account takes
    only the statements with its own last four digits, or the one statement
    no account matched.
    """

    mapping_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    group: int | None = Field(default=None, ge=0, le=MAX_GROUP)

    @model_validator(mode="after")
    def _group_needs_account(self) -> Self:
        if self.group is not None and self.account_id is None:
            raise ValueError("group needs account_id")
        return self


class ApplyMappingJob(BaseModel):
    """Payload of :data:`APPLY_MAPPING_JOB_TYPE` (ids only, never content)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: uuid.UUID
    mapping_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    group: int | None = None
    requested_by: uuid.UUID | None = None


class ImportAccepted(_Output):
    job_id: uuid.UUID
    state: str
    document_id: uuid.UUID


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


class PreviewTable(_Output):
    index: int
    encoding: str
    delimiter: str
    has_header: bool
    column_count: int
    row_count: int
    truncated: bool
    offset: int
    rows: list[list[str]]
    #: Fingerprint of row 0 read as a header (``skip_rows`` 0).
    header_fingerprint: str | None


class PreviewBalance(_Output):
    amount: str
    currency: str | None
    as_of: date | None


class PreviewTransaction(_Output):
    posted_date: date
    amount: str
    currency: str | None
    payee: str | None
    memo: str | None
    category: str | None
    check_number: str | None


class PreviewStatement(_Output):
    index: int
    source_format: str
    account_type: str | None
    last4: str | None
    currency: str | None
    institution: str | None
    period_start: date | None
    period_end: date | None
    ledger_balance: PreviewBalance | None
    transaction_count: int
    truncated: bool
    offset: int
    transactions: list[PreviewTransaction]


class SuggestionRole(_Output):
    """Why the suggestion gave a column a role (no cell text)."""

    role: Literal[
        "date",
        "amount",
        "debit",
        "credit",
        "indicator",
        "balance",
        "currency",
        "account",
        "payee",
        "memo",
    ]
    column: int
    #: How the header named the role.
    match: Literal["exact", "word", "substring"]
    #: Share of the column's non-empty sample cells that fit the role (0-1).
    fit: float


class MappingSuggestionOut(_Output):
    """The deterministic mapping guess and how far it can be trusted.

    ``confident`` (no ``doubts``) is what the importer needs to import a
    never-seen CSV/TSV layout without asking (INGESTION.md "Parse");
    ``doubts`` are codes: ``header_not_exact``, ``rows_unreadable``,
    ``date_order_guessed``, ``no_rows``, ``indicator_values_unknown`` (an
    indicator cell that is no direction word, or a signed amount beside
    it). ``sign_evidence`` is
    ``debit_credit``, ``indicator``, ``balance`` or null (a signed amount
    column read as "positive is money in" on trust).
    """

    spec: MappingSpec
    score: float
    confident: bool
    parsed_rows: int
    failed_rows: int
    footer_rows: int
    accounts: int
    sign_evidence: Literal["debit_credit", "indicator", "balance"] | None
    doubts: list[str]
    roles: list[SuggestionRole]


class ImportPreview(_Output):
    """``GET /imports/{document_id}/preview``: the stored sandbox output, never re-parsed."""

    document_id: uuid.UUID
    status: DocumentStatus
    kind: str | None
    account_id: uuid.UUID | None
    tables: list[PreviewTable]
    statements: list[PreviewStatement]
    warnings: list[str]
    #: The saved mapping whose fingerprint matches the table (visible to the caller).
    matched_mapping_id: uuid.UUID | None
    #: A guess from the header names and cell shapes, for the mapping editor.
    suggested_spec: MappingSpec | None
    #: The same guess with its confidence and per-role reasons.
    suggestion: MappingSuggestionOut | None
    last_import: dict[str, Any] | None

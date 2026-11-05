# SPDX-License-Identifier: AGPL-3.0-or-later
"""Saved column mappings for delimited statements (DATA_MODEL.md "Documents and ingestion").

``import_mapping`` tells the structured importer how to read one CSV/TSV
layout: which column holds the date (and in which format), the amount (one
signed column, separate debit/credit columns, or an amount plus a debit/credit
indicator), the payee, memo, running balance and currency. ``spec`` is the
JSON document :class:`app.domain.imports.schemas.MappingSpec` (validated on
every write and every read).

``header_fingerprint`` is
:func:`app.ingestion.parsers.structured.header_fingerprint` of the layout's
header row (row ``spec.skip_rows`` of the table); a newly extracted table
whose header has the same fingerprint is imported with the mapping without
asking anyone (INGESTION.md "Column-mapping review"). A mapping is saved by a
person through the editor, or by the importer from a confident suggestion
for a never-seen layout (``created_by`` is then the person whose document it
was; the audit row says ``origin: suggested``); both are ordinary mappings.

``account_id`` (optional) is the account files of this layout go to when the
upload did not name one; ``institution_id`` (optional) is recorded on the
documents imported with it. Both are household-consistent ``SET NULL``
references (ADR-0008). A mapping whose account the caller cannot see is
``404`` for them, like the account itself (SECURITY.md F3).

``sign_unverified`` is True on a mapping the importer saved from a
suggestion whose single signed amount column was read as "positive is money
in" on trust (``sign_evidence`` None: no debit/credit columns, indicator or
agreeing running balance). The importer does not apply such a mapping to a
liability account on its own (a card export may list purchases as
positive); a person saving the mapping's spec (``PATCH`` with ``spec``)
clears it, and choosing the mapping explicitly for a re-run uses it as is.
It is not part of the API.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# Referenced tables must be in the metadata wherever these models are used.
import app.auth.models
import app.domain.accounts.models  # noqa: F401
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import household_key, household_ref_fk

NAME_MAX = 200


class ImportMapping(UUIDPk, Timestamps, HouseholdScoped, Base):
    """A saved column mapping for one delimited statement layout."""

    __tablename__ = "import_mapping"
    __table_args__ = (
        household_key("import_mapping"),
        household_ref_fk("import_mapping", "account_id", "account"),
        household_ref_fk("import_mapping", "institution_id", "institution"),
        UniqueConstraint("household_id", "name", name="uq_import_mapping_household_name"),
        CheckConstraint("header_fingerprint ~ '^[0-9a-f]{64}$'", name="header_fingerprint_hex"),
        CheckConstraint("jsonb_typeof(spec) = 'object'", name="spec_object"),
        CheckConstraint("use_count >= 0", name="use_count_non_negative"),
        Index("ix_import_mapping_household_fingerprint", "household_id", "header_fingerprint"),
    )

    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    institution_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("institution.id", ondelete="SET NULL"), index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("account.id", ondelete="SET NULL"), index=True
    )
    header_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL")
    )
    use_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Saved from a suggestion whose sign was read on trust (module doc).
    sign_unverified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    def __repr__(self) -> str:
        return f"ImportMapping(id={self.id!s})"

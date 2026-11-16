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

``statement_record`` is one statement the importer committed for an account
(the statement-level duplicate guard, INGESTION.md "Match and
deduplicate"): the account, the document it came from, its period, where
the period came from (``period_stated``: printed by the statement or
declared by the format, else the span of its rows), the source format and
its closing figures (a bank statement's closing balance; a brokerage
statement's cash, holdings and account value). ``outcome`` is ``imported``,
or ``skipped`` when a person said the statement is the same as one already
imported (it then stays out). A new statement whose stated period overlaps
an imported, stated one of the same account from another document waits in
review (``statement_overlap``). Both references cascade: the record goes
with its account or document.

``statement_record.verification`` says how far the statement was checked
against its own balances (SECURITY.md C12): ``reconciled`` (its balances
agreed with its rows, at import or later in the reconcile view),
``accepted`` (a person accepted it without balancing) or ``unverified``
(everything else, including CSV, OFX and QIF, which carry nothing to
reconcile). Only a ``reconciled`` statement writes a balance snapshot.

``import_tombstone`` is one imported ledger row a person deleted: the
account and the row's ``import_hash``, when and by whom, and the row's
date and amount so the reconcile view can list it and offer "Put back".
No payee text is kept. A hash with a tombstone is never imported into that
account again (the same statement read again, or arriving as another
file). The account reference cascades; ``deleted_by`` is cleared with the
user.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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
from app.db.types import (
    AMOUNT,
    CURRENCY,
    currency_check,
    household_fk,
    household_key,
    household_ref_fk,
)

NAME_MAX = 200
#: ``statement_record.kind``: a ledger (bank, card, loan) or a brokerage statement.
STATEMENT_KINDS: Final = ("bank", "investment")
#: ``statement_record.outcome`` values.
STATEMENT_OUTCOMES: Final = ("imported", "skipped")
#: ``statement_record.verification`` values (module doc).
STATEMENT_VERIFICATIONS: Final = ("reconciled", "unverified", "accepted")


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


class StatementRecord(UUIDPk, Timestamps, HouseholdScoped, Base):
    """One statement committed for an account (module doc)."""

    __tablename__ = "statement_record"
    __table_args__ = (
        household_fk("statement_record", "account_id", "account"),
        household_fk("statement_record", "document_id", "document"),
        UniqueConstraint(
            "account_id",
            "document_id",
            "kind",
            "period_start",
            name="uq_statement_record_account_document_kind_start",
        ),
        CheckConstraint("period_start <= period_end", name="period_ordered"),
        CheckConstraint("kind IN ('bank', 'investment')", name="kind_known"),
        CheckConstraint("outcome IN ('imported', 'skipped')", name="outcome_known"),
        CheckConstraint("source_format ~ '^[a-z]{1,16}$'", name="source_format_code"),
        CheckConstraint(
            "verification IN ('reconciled', 'unverified', 'accepted')", name="verification_known"
        ),
        currency_check(),
        Index("ix_statement_record_account_period", "household_id", "account_id", "period_start"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(
        String(16), nullable=False, default="imported", server_default=text("'imported'")
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    #: Printed or declared by the statement (else the span of its rows).
    period_stated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_format: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    #: A bank statement's closing balance.
    closing_balance: Mapped[Decimal | None] = mapped_column(AMOUNT)
    #: A brokerage statement's closing cash, holdings total and account value.
    closing_cash: Mapped[Decimal | None] = mapped_column(AMOUNT)
    holdings_value: Mapped[Decimal | None] = mapped_column(AMOUNT)
    total_value: Mapped[Decimal | None] = mapped_column(AMOUNT)
    #: One of :data:`STATEMENT_VERIFICATIONS` (module doc).
    verification: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unverified", server_default=text("'unverified'")
    )

    def __repr__(self) -> str:
        return f"StatementRecord(id={self.id!s}, kind={self.kind})"


class ImportTombstone(UUIDPk, Timestamps, HouseholdScoped, Base):
    """One imported row a person deleted: its hash stays out of the account (module doc)."""

    __tablename__ = "import_tombstone"
    __table_args__ = (
        household_fk("import_tombstone", "account_id", "account"),
        UniqueConstraint(
            "household_id", "account_id", "import_hash", name="uq_import_tombstone_hash"
        ),
        CheckConstraint("import_hash ~ '^[0-9a-f]{64}$'", name="import_hash_hex"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    import_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL")
    )
    #: The deleted row's date and amount, for the list of removed rows.
    posted_date: Mapped[date | None] = mapped_column(Date)
    amount: Mapped[Decimal | None] = mapped_column(AMOUNT)

    def __repr__(self) -> str:
        return f"ImportTombstone(id={self.id!s}, account_id={self.account_id!s})"

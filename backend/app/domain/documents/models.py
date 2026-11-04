# SPDX-License-Identifier: AGPL-3.0-or-later
"""Documents and ingestion (DATA_MODEL.md "Documents and ingestion").

Every table is ``HouseholdScoped`` (forced RLS) and references between them
include ``household_id`` (ADR-0008), so no row can point at another
household's blob, document, account or institution.

* ``blob``: one encrypted object in the blob store (ADR-0003). ``store`` and
  ``key`` locate the ciphertext (``key`` is a random UUID, never derived from
  a file name: SECURITY.md C3); ``area`` is ``quarantine`` until the
  pipeline accepts the file, then ``permanent``. The per-object data key is
  stored wrapped by the master key (``key_version``, ``wrap_nonce``,
  ``wrapped_dek``; associated data binds the blob id); ``nonce`` is the
  secretstream header of the payload. Deleting a document sets
  ``wrapped_dek``/``wrap_nonce`` to NULL and ``shredded_at`` before the row and
  the file go (crypto-shredding, SECURITY.md G5). ``size`` is the plaintext
  size, ``stored_size`` the ciphertext size on disk.
* ``document``: what was received and what became of it. ``original_filename``
  is metadata only (C3): rendered escaped, never used to build a path.
  ``mime_detected`` comes from the magic-byte sniff
  (``app.ingestion.intake.sniff``); the declared type and extension are never
  trusted. ``extraction`` keeps the validated sandbox output so a later parser
  version can re-run without the original; ``extracted_text`` is the full-text
  index (Phase 3). The ``blob`` reference is ``NO ACTION``: a blob cannot be
  deleted while a document uses it.
* ``document_link``: a document attached to a transaction, investment
  transaction, loan payment, account or tax year (``target_id`` is polymorphic,
  checked by the service that writes it).
* ``review_item``: the review queue. ``payload`` and ``resolution`` are JSON
  objects owned by the stage that raised the item; payloads carry ids and
  short facts, never file content.
* ``parser_fingerprint``: a learned or shipped layout rule for an institution.
* ``ingest_job``: one row per pipeline stage run, for the document's history
  and debugging (``log`` holds stage facts, never document text).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

# Referenced tables must be in the metadata wherever these models are used.
import app.auth.models
import app.domain.accounts.models  # noqa: F401
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import household_fk, household_key, household_ref_fk, pg_enum

FILENAME_MAX = 255
FREE_TEXT: dict[str, Any] = {"classification": "free_text"}
SECRET: dict[str, Any] = {"classification": "secret"}


class BlobArea(enum.StrEnum):
    QUARANTINE = "quarantine"
    PERMANENT = "permanent"


class DocumentKind(enum.StrEnum):
    STATEMENT = "statement"
    RECEIPT = "receipt"
    INVOICE = "invoice"
    TAX_FORM = "tax_form"
    PAYSLIP = "payslip"
    INSURANCE = "insurance"
    OTHER = "other"
    UNKNOWN = "unknown"


class DocumentStatus(enum.StrEnum):
    RECEIVED = "received"
    SCANNING = "scanning"
    EXTRACTING = "extracting"
    CLASSIFYING = "classifying"
    PARSING = "parsing"
    NEEDS_REVIEW = "needs_review"
    PROCESSED = "processed"
    REJECTED = "rejected"
    FAILED = "failed"


#: Statuses after which the pipeline does nothing more until "reprocess".
TERMINAL_STATUSES = frozenset(
    {
        DocumentStatus.NEEDS_REVIEW,
        DocumentStatus.PROCESSED,
        DocumentStatus.REJECTED,
        DocumentStatus.FAILED,
    }
)


class DocumentSource(enum.StrEnum):
    UPLOAD = "upload"
    DROPFOLDER = "dropfolder"
    EMAIL = "email"
    API = "api"


class LinkTarget(enum.StrEnum):
    TRANSACTION = "transaction"
    INVESTMENT_TXN = "investment_txn"
    LOAN_PAYMENT = "loan_payment"
    ACCOUNT = "account"
    TAX_YEAR = "tax_year"


class ReviewKind(enum.StrEnum):
    UNKNOWN_OWNER = "unknown_owner"
    UNKNOWN_ACCOUNT = "unknown_account"
    LOW_CONFIDENCE_FIELD = "low_confidence_field"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    UNPARSED = "unparsed"
    NEW_INSTITUTION = "new_institution"
    REJECTED_EMAIL = "rejected_email"
    #: "Attach this receipt to ..." (``app.ingestion.match.receipts``).
    RECEIPT_MATCH = "receipt_match"


class ReviewItemState(enum.StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class IngestStage(enum.StrEnum):
    """Pipeline stages (INGESTION.md "Pipeline stages")."""

    RECEIVE = "receive"
    SCAN = "scan"
    EXTRACT = "extract"
    CLASSIFY = "classify"
    PARSE = "parse"
    MATCH = "match"
    COMMIT = "commit"


DOCUMENT_KIND = pg_enum(DocumentKind, "document_kind")


class Blob(UUIDPk, Timestamps, HouseholdScoped, Base):
    """One encrypted object in the blob store."""

    __tablename__ = "blob"
    __table_args__ = (
        household_key("blob"),
        UniqueConstraint("store", "key", name="uq_blob_store_key"),
        CheckConstraint("size >= 0 AND stored_size >= 0", name="sizes_non_negative"),
        CheckConstraint(
            "(wrapped_dek IS NULL) = (wrap_nonce IS NULL)", name="wrap_fields_together"
        ),
        CheckConstraint("shredded_at IS NULL OR wrapped_dek IS NULL", name="shredded_has_no_key"),
    )

    store: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    area: Mapped[BlobArea] = mapped_column(pg_enum(BlobArea, "blob_area"), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stored_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    enc_alg: Mapped[str] = mapped_column(String(64), nullable=False)
    key_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    wrap_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, info=SECRET)
    wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary, info=SECRET)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    shredded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"Blob(id={self.id!s}, area={self.area!s})"


class Document(UUIDPk, Timestamps, HouseholdScoped, Base):
    """A received file and the state of its ingestion."""

    __tablename__ = "document"
    __table_args__ = (
        household_key("document"),
        household_fk("document", "blob_id", "blob", ondelete=None),
        household_ref_fk("document", "account_id", "account"),
        household_ref_fk("document", "institution_id", "institution"),
        CheckConstraint("size >= 0", name="size_non_negative"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        CheckConstraint("jsonb_typeof(source_meta) = 'object'", name="source_meta_object"),
        CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_end >= period_start",
            name="period_order",
        ),
        CheckConstraint(
            "tax_year IS NULL OR (tax_year >= 1900 AND tax_year <= 2200)", name="tax_year_range"
        ),
        Index("ix_document_household_sha256", "household_id", "sha256"),
        Index("ix_document_household_status", "household_id", "status"),
        Index("ix_document_extracted_text", "extracted_text", postgresql_using="gin"),
    )

    blob_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    kind: Mapped[DocumentKind] = mapped_column(
        DOCUMENT_KIND,
        nullable=False,
        default=DocumentKind.UNKNOWN,
        server_default=text("'unknown'"),
    )
    status: Mapped[DocumentStatus] = mapped_column(
        pg_enum(DocumentStatus, "document_status"),
        nullable=False,
        default=DocumentStatus.RECEIVED,
        server_default=text("'received'"),
    )
    source: Mapped[DocumentSource] = mapped_column(
        pg_enum(DocumentSource, "document_source"), nullable=False
    )
    source_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_detected: Mapped[str] = mapped_column(String(128), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(FILENAME_MAX), info=FREE_TEXT)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("account.id", ondelete="SET NULL"), index=True
    )
    institution_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("institution.id", ondelete="SET NULL"), index=True
    )
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    tax_year: Mapped[int | None] = mapped_column(SmallInteger)
    extracted_text: Mapped[Any | None] = mapped_column(TSVECTOR, info=FREE_TEXT)
    extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONB, info=FREE_TEXT)
    confidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    parser_version: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(String(500))

    def __repr__(self) -> str:
        # Never include the file name (free text) in logs.
        return f"Document(id={self.id!s}, status={self.status!s})"


class DocumentLink(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "document_link"
    __table_args__ = (
        household_fk("document_link", "document_id", "document"),
        UniqueConstraint("document_id", "target_type", "target_id", name="uq_document_link_target"),
        Index("ix_document_link_target", "target_type", "target_id"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_type: Mapped[LinkTarget] = mapped_column(
        pg_enum(LinkTarget, "document_link_target"), nullable=False
    )
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)


class ReviewItem(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "review_item"
    __table_args__ = (
        household_fk("review_item", "document_id", "document"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_object"),
        CheckConstraint(
            "resolution IS NULL OR jsonb_typeof(resolution) = 'object'", name="resolution_object"
        ),
        CheckConstraint("(state = 'open') = (resolved_at IS NULL)", name="resolved_at_when_closed"),
        Index("ix_review_item_household_state", "household_id", "state"),
    )

    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), index=True)
    kind: Mapped[ReviewKind] = mapped_column(pg_enum(ReviewKind, "review_kind"), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    state: Mapped[ReviewItemState] = mapped_column(
        pg_enum(ReviewItemState, "review_item_state"),
        nullable=False,
        default=ReviewItemState.OPEN,
        server_default=text("'open'"),
    )
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ParserFingerprint(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "parser_fingerprint"
    __table_args__ = (
        household_fk("parser_fingerprint", "institution_id", "institution"),
        household_ref_fk("parser_fingerprint", "learned_from_document_id", "document"),
        CheckConstraint("jsonb_typeof(match_rules) = 'object'", name="match_rules_object"),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    institution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    kind: Mapped[DocumentKind] = mapped_column(DOCUMENT_KIND, nullable=False)
    match_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    parser_name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    learned_from_document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document.id", ondelete="SET NULL")
    )


class IngestJob(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "ingest_job"
    __table_args__ = (
        household_fk("ingest_job", "document_id", "document"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint("jsonb_typeof(log) = 'object'", name="log_object"),
        Index("ix_ingest_job_document_started", "document_id", "started_at"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stage: Mapped[IngestStage] = mapped_column(pg_enum(IngestStage, "ingest_stage"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(500))
    log: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

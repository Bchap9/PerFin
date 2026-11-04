# SPDX-License-Identifier: AGPL-3.0-or-later
"""Full export and import (docs/PORTABILITY.md, VISION.md "Everything exportable").

``portability_archive``: one export or import, keyed by the job that does
the work (``job_id``: ``GET /export/{job_id}/download`` names it).

* ``kind``: ``export`` (built by ``portability.export``) or ``import``
  (uploaded by ``POST /import``, applied by ``portability.import``).
* ``state``: an export goes ``pending`` -> ``ready`` -> ``downloaded`` (or
  ``expired`` once ``expires_at`` passed, or ``failed``); an import goes
  ``pending`` -> ``imported`` or ``failed`` (``error`` holds a short slug,
  never file content).
* ``blob_id``: the archive bytes in the blob store (ADR-0003: a per-object
  data key wrapped by the master key). An export's blob is in the
  ``permanent`` area until it is downloaded once or expires; an uploaded
  import's is in ``quarantine`` until the job ran. Either way the blob's key
  is destroyed and the row and file deleted afterwards (``blob_id`` becomes
  NULL).
* ``passphrase_key``: when the user chose a passphrase, the Argon2id key
  derived from it in the request (``app.core.archive_crypto``), stored only
  as an envelope under the master key bound to this row's id, and set to
  NULL as soon as the job is done. The passphrase itself is never stored.
* ``requested_by``: the only user who may see the job's outcome or download
  the export.
* ``summary``: counts per entity (export) or created/merged/skipped counts
  (import); ids and numbers only.
"""

from __future__ import annotations

import enum
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
    LargeBinary,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# Referenced tables must be in the metadata wherever these models are used.
import app.auth.models
import app.domain.documents.models  # noqa: F401
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import household_key, household_ref_fk, pg_enum

SECRET: dict[str, Any] = {"classification": "secret"}


class ArchiveKind(enum.StrEnum):
    EXPORT = "export"
    IMPORT = "import"


class ArchiveState(enum.StrEnum):
    PENDING = "pending"
    READY = "ready"
    DOWNLOADED = "downloaded"
    EXPIRED = "expired"
    IMPORTED = "imported"
    FAILED = "failed"


class PortabilityArchive(UUIDPk, Timestamps, HouseholdScoped, Base):
    """One full export or import of a household."""

    __tablename__ = "portability_archive"
    __table_args__ = (
        household_key("portability_archive"),
        household_ref_fk("portability_archive", "blob_id", "blob"),
        CheckConstraint("size IS NULL OR size >= 0", name="size_non_negative"),
        CheckConstraint("sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        CheckConstraint("jsonb_typeof(summary) = 'object'", name="summary_object"),
        Index("ix_portability_archive_household_state", "household_id", "kind", "state"),
    )

    kind: Mapped[ArchiveKind] = mapped_column(
        pg_enum(ArchiveKind, "portability_kind"), nullable=False
    )
    state: Mapped[ArchiveState] = mapped_column(
        pg_enum(ArchiveState, "portability_state"),
        nullable=False,
        default=ArchiveState.PENDING,
        server_default=text("'pending'"),
    )
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), index=True
    )
    blob_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("blob.id", ondelete="SET NULL"), index=True
    )
    passphrase_protected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    passphrase_key: Mapped[bytes | None] = mapped_column(LargeBinary, info=SECRET)
    size: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(CHAR(64))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    def __repr__(self) -> str:
        return f"PortabilityArchive(id={self.id!s}, kind={self.kind!s}, state={self.state!s})"

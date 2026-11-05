# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drop-folder state (INGESTION.md "Drop folder specifics").

* ``dropfolder_route``: a subfolder of the inbox (its *slug*: the folder
  names below the inbox, normalised by
  :func:`app.ingestion.intake.dropfolder.folder_slug`, ``""`` for the inbox
  itself) and the member, and optionally the account, its files belong to.
  Rows are written by the ``unknown_owner`` review resolution with
  ``remember`` ("Who does this belong to?"); without a route a folder named
  like a member's username (and below it a folder named like one of that
  member's editable accounts) is recognised too.
* ``dropfolder_entry``: one inbox file the watcher took (or refused), keyed
  by a hash of its relative path plus size, mtime and inode. It is the
  idempotency ledger: a file that has an entry is never taken again (so a
  read-only inbox in ``keep`` mode is not reprocessed after a restart), and
  ``after_state = 'pending'`` rows are the move/delete actions still to do
  once the transaction that recorded the document has committed.
  ``rel_path`` is metadata only (SECURITY.md C3): it is never used to build
  a path on the PerFin side.

Both tables are household-scoped (forced RLS).
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

# Referenced tables must be in the metadata wherever these models are used.
import app.auth.models
import app.domain.accounts.models
import app.domain.documents.models  # noqa: F401
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import household_ref_fk

FOLDER_MAX = 255
REL_PATH_MAX = 1024
FREE_TEXT = {"classification": "free_text"}

OUTCOMES = ("accepted", "rejected", "skipped")
AFTER_STATES = ("pending", "done", "kept", "error")


class DropfolderRoute(UUIDPk, Timestamps, HouseholdScoped, Base):
    """Files under ``folder`` belong to ``user_id`` (and ``account_id``, if set)."""

    __tablename__ = "dropfolder_route"
    __table_args__ = (
        UniqueConstraint("household_id", "folder", name="uq_dropfolder_route_folder"),
        household_ref_fk("dropfolder_route", "account_id", "account"),
        CheckConstraint("folder !~ '[[:cntrl:]]'", name="folder_printable"),
    )

    folder: Mapped[str] = mapped_column(String(FOLDER_MAX), nullable=False, info=FREE_TEXT)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("account.id", ondelete="SET NULL"), index=True
    )

    def __repr__(self) -> str:
        return f"DropfolderRoute(id={self.id!s})"


class DropfolderEntry(UUIDPk, Timestamps, HouseholdScoped, Base):
    """One inbox file seen by the watcher and what became of it."""

    __tablename__ = "dropfolder_entry"
    __table_args__ = (
        UniqueConstraint(
            "household_id",
            "path_key",
            "size",
            "mtime_ns",
            "inode",
            name="uq_dropfolder_entry_file",
        ),
        household_ref_fk("dropfolder_entry", "document_id", "document"),
        CheckConstraint("path_key ~ '^[0-9a-f]{64}$'", name="path_key_hex"),
        CheckConstraint("sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        CheckConstraint("size >= 0", name="size_non_negative"),
        CheckConstraint("outcome IN ('accepted', 'rejected', 'skipped')", name="outcome_known"),
        CheckConstraint(
            "after_state IN ('pending', 'done', 'kept', 'error')", name="after_state_known"
        ),
        Index("ix_dropfolder_entry_household_sha256", "household_id", "sha256"),
        Index("ix_dropfolder_entry_household_after_state", "household_id", "after_state"),
    )

    path_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    rel_path: Mapped[str] = mapped_column(String(REL_PATH_MAX), nullable=False, info=FREE_TEXT)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mtime_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inode: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(CHAR(64))
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document.id", ondelete="SET NULL"), index=True
    )
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64))
    after_state: Mapped[str] = mapped_column(String(16), nullable=False)
    # What happened to the original: moved (to processed/ or failed/),
    # deleted, kept, gone; never a path.
    after_result: Mapped[str | None] = mapped_column(String(32))

    def __repr__(self) -> str:
        return f"DropfolderEntry(id={self.id!s}, outcome={self.outcome})"

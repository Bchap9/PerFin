# SPDX-License-Identifier: AGPL-3.0-or-later
"""What review resolutions teach the importer.

``import_hash_alias``: "these two imported rows are the same transaction"
(INGESTION.md "Review queue": *confirm a duplicate -> both hashes recorded as
equivalent*). Confirming a ``possible_duplicate`` deletes the newly imported
row and records its ``import_hash`` as an alias of the transaction that is
kept, so importing the same statement again matches exactly instead of
raising the same question. The deduplicator reads aliases with
:func:`app.domain.review.repo.alias_rows` next to the account's own hashes.

Household-scoped (forced RLS); the account and the kept transaction are
household-consistent references (ADR-0008) that cascade.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CHAR, CheckConstraint, Index, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

# Referenced tables must be in the metadata wherever these models are used.
import app.domain.accounts.models
import app.domain.transactions.models  # noqa: F401
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import household_fk


class ImportHashAlias(UUIDPk, Timestamps, HouseholdScoped, Base):
    """``import_hash`` of a confirmed duplicate, pointing at the row that was kept."""

    __tablename__ = "import_hash_alias"
    __table_args__ = (
        household_fk("import_hash_alias", "account_id", "account"),
        household_fk("import_hash_alias", "transaction_id", "transaction"),
        UniqueConstraint(
            "household_id", "account_id", "import_hash", name="uq_import_hash_alias_hash"
        ),
        CheckConstraint("import_hash ~ '^[0-9a-f]{64}$'", name="import_hash_hex"),
        Index("ix_import_hash_alias_transaction_id", "transaction_id"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    import_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)

    def __repr__(self) -> str:
        return f"ImportHashAlias(id={self.id!s}, transaction_id={self.transaction_id!s})"

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Statement verification and import tombstones (the import trust policy).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-15 12:00:00.000000+00:00

Column added here (``app.domain.imports.models``):

* ``statement_record.verification`` (``reconciled``, ``unverified`` or
  ``accepted``; checked; ``unverified`` unless the importer says more): how
  far a statement was checked against its own balances (SECURITY.md C12).

Table created here, household-scoped (``ENABLE`` + ``FORCE ROW LEVEL
SECURITY`` with the ``household_isolation`` policy, and DML grants for the
application role ``PERFIN_DB_APP_ROLE``), listed explicitly in
:data:`TABLES`:

* ``import_tombstone``: one imported ledger row a person deleted (the
  account and the row's import hash, when and by whom, its date and amount;
  never its payee), so no later import brings it back. A
  household-consistent ``CASCADE`` key to ``account``; ``deleted_by`` is
  cleared with the user. Nothing is backfilled: rows deleted before this
  revision left no trace to build a tombstone from.

``verification`` of the records earlier imports wrote is backfilled from
what those imports left behind, in this order (:data:`BACKFILL`):

a. ``reconciled``: a bank statement read from a document (``pdf``,
   ``image``) for which a ``statement`` balance snapshot of the same
   account from the same document exists (only a statement whose balances
   agreed with its rows wrote one);
b. ``reconciled``: an imported brokerage statement read from a document
   (it was imported because its figures added up, unless (c) says a person
   imported it as read);
c. ``accepted``, overriding (b) and where (a) did not apply: an imported
   statement read from a document for which a resolved review item of that
   document shows a person's answer for that account: "import as read"
   (``resolution.import_as_read``, with the account in the resolution), or,
   for a bank statement, a ``reconciliation_failed`` /
   ``reconciliation_unavailable`` item naming the account;
d. everything else stays ``unverified`` (CSV, OFX and QIF carry nothing to
   reconcile; a skipped statement was never checked).

The backfill runs as the table owner with forced row-level security lifted
for its statements on the three tables it reads or writes, as revision
``0004`` does; the downgrade drops the table, then the check and the
column. No enum type: ``verification`` is a short code checked here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from app.db.rls import all_rls_ddl, table_grants_ddl

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables created by this revision (all household-scoped).
TABLES: tuple[str, ...] = ("import_tombstone",)
#: Household-scoped tables: forced RLS with ``household_isolation`` only.
RLS_TABLES: tuple[str, ...] = TABLES
#: Tables the backfill reads or writes across households (forced RLS is
#: lifted for the owner while it runs, then restored).
BACKFILL_TABLES: tuple[str, ...] = ("statement_record", "balance_snapshot", "review_item")

_RECONCILED_BANK = """
UPDATE statement_record AS r
SET verification = 'reconciled'
WHERE r.kind = 'bank'
  AND r.source_format IN ('pdf', 'image')
  AND EXISTS (
      SELECT 1
      FROM balance_snapshot AS b
      WHERE b.household_id = r.household_id
        AND b.account_id = r.account_id
        AND b.source_document_id = r.document_id
        AND b.source = 'statement'
  )
"""

_RECONCILED_INVESTMENT = """
UPDATE statement_record AS r
SET verification = 'reconciled'
WHERE r.kind = 'investment'
  AND r.outcome = 'imported'
  AND r.source_format IN ('pdf', 'image')
"""

_ACCEPTED = """
UPDATE statement_record AS r
SET verification = 'accepted'
WHERE r.outcome = 'imported'
  AND r.source_format IN ('pdf', 'image')
  AND NOT (
      r.kind = 'bank'
      AND EXISTS (
          SELECT 1
          FROM balance_snapshot AS b
          WHERE b.household_id = r.household_id
            AND b.account_id = r.account_id
            AND b.source_document_id = r.document_id
            AND b.source = 'statement'
      )
  )
  AND EXISTS (
      SELECT 1
      FROM review_item AS i
      WHERE i.household_id = r.household_id
        AND i.document_id = r.document_id
        AND i.state = 'resolved'
        AND (
            (
                i.resolution ->> 'import_as_read' = 'true'
                AND i.resolution ->> 'account_id' = r.account_id::text
            )
            OR (
                r.kind = 'bank'
                AND i.payload ->> 'reason'
                    IN ('reconciliation_failed', 'reconciliation_unavailable')
                AND i.payload ->> 'account_id' = r.account_id::text
            )
        )
  )
"""

#: The backfill statements, in the order of the module docstring.
BACKFILL: tuple[str, ...] = (_RECONCILED_BANK, _RECONCILED_INVESTMENT, _ACCEPTED)


def _app_role() -> str:
    """Role granted DML: ``perfin_app_role`` attribute (tests) or the setting."""
    supplied = context.config.attributes.get("perfin_app_role")
    if isinstance(supplied, str) and supplied:
        return supplied
    from app.core.settings import get_settings

    return get_settings().db_app_role


def _security_ddl(app_role: str) -> list[str]:
    return [*all_rls_ddl(RLS_TABLES), *table_grants_ddl(TABLES, app_role)]


def _backfill_verification() -> None:
    """Mark the records of earlier imports (see the module docstring)."""
    for table in BACKFILL_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    for statement in BACKFILL:
        op.execute(statement)
    for table in BACKFILL_TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    op.add_column(
        "statement_record",
        sa.Column(
            "verification",
            sa.String(length=16),
            server_default=sa.text("'unverified'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_statement_record_verification_known"),
        "statement_record",
        "verification IN ('reconciled', 'unverified', 'accepted')",
    )
    op.create_table(
        "import_tombstone",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("import_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.Column("posted_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "import_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_import_tombstone_import_hash_hex")
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by"],
            ["app_user.id"],
            name=op.f("fk_import_tombstone_deleted_by_app_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["household_id", "account_id"],
            ["account.household_id", "account.id"],
            name="fk_import_tombstone_account_id_hh_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_import_tombstone_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_tombstone")),
        sa.UniqueConstraint(
            "household_id", "account_id", "import_hash", name="uq_import_tombstone_hash"
        ),
    )
    op.create_index(
        op.f("ix_import_tombstone_household_id"),
        "import_tombstone",
        ["household_id"],
        unique=False,
    )
    for statement in _security_ddl(_app_role()):
        op.execute(statement)
    _backfill_verification()


def downgrade() -> None:
    op.drop_index(op.f("ix_import_tombstone_household_id"), table_name="import_tombstone")
    op.drop_table("import_tombstone")
    op.drop_constraint(
        op.f("ck_statement_record_verification_known"), "statement_record", type_="check"
    )
    op.drop_column("statement_record", "verification")

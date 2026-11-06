# SPDX-License-Identifier: AGPL-3.0-or-later
"""Statement records (the statement-level duplicate guard).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-06 12:00:00.000000+00:00

Table created here (``app.domain.imports.models``), household-scoped
(``ENABLE`` + ``FORCE ROW LEVEL SECURITY`` with the ``household_isolation``
policy, and DML grants for the application role ``PERFIN_DB_APP_ROLE``),
listed explicitly in :data:`TABLES`:

* ``statement_record``: one statement the importer committed (or a person
  skipped as the same as one already imported) for an account: the
  document, the period and whether it was printed or only the span of the
  rows, the source format and the closing figures. Household-consistent
  ``CASCADE`` keys to ``account`` and ``document``. No existing row is
  backfilled: statements imported before this revision are guarded by the
  row-level dedupe only.

No enum type: ``kind`` and ``outcome`` are short codes checked here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from app.db.rls import all_rls_ddl, table_grants_ddl

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables created by this revision (all household-scoped).
TABLES: tuple[str, ...] = ("statement_record",)
#: Household-scoped tables: forced RLS with ``household_isolation`` only.
RLS_TABLES: tuple[str, ...] = TABLES


def _app_role() -> str:
    """Role granted DML: ``perfin_app_role`` attribute (tests) or the setting."""
    supplied = context.config.attributes.get("perfin_app_role")
    if isinstance(supplied, str) and supplied:
        return supplied
    from app.core.settings import get_settings

    return get_settings().db_app_role


def _security_ddl(app_role: str) -> list[str]:
    return [*all_rls_ddl(RLS_TABLES), *table_grants_ddl(TABLES, app_role)]


def upgrade() -> None:
    op.create_table(
        "statement_record",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "outcome", sa.String(length=16), server_default=sa.text("'imported'"), nullable=False
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("period_stated", sa.Boolean(), nullable=False),
        sa.Column("source_format", sa.String(length=16), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("closing_balance", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("closing_cash", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("holdings_value", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("total_value", sa.Numeric(precision=20, scale=6), nullable=True),
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
            "currency ~ '^[A-Z]{3}$'", name=op.f("ck_statement_record_currency_format")
        ),
        sa.CheckConstraint(
            "kind IN ('bank', 'investment')", name=op.f("ck_statement_record_kind_known")
        ),
        sa.CheckConstraint(
            "outcome IN ('imported', 'skipped')", name=op.f("ck_statement_record_outcome_known")
        ),
        sa.CheckConstraint(
            "period_start <= period_end", name=op.f("ck_statement_record_period_ordered")
        ),
        sa.CheckConstraint(
            "source_format ~ '^[a-z]{1,16}$'",
            name=op.f("ck_statement_record_source_format_code"),
        ),
        sa.ForeignKeyConstraint(
            ["household_id", "account_id"],
            ["account.household_id", "account.id"],
            name="fk_statement_record_account_id_hh_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["household_id", "document_id"],
            ["document.household_id", "document.id"],
            name="fk_statement_record_document_id_hh_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_statement_record_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statement_record")),
        sa.UniqueConstraint(
            "account_id",
            "document_id",
            "kind",
            "period_start",
            name="uq_statement_record_account_document_kind_start",
        ),
    )
    op.create_index(
        "ix_statement_record_account_period",
        "statement_record",
        ["household_id", "account_id", "period_start"],
        unique=False,
    )
    op.create_index(
        op.f("ix_statement_record_document_id"), "statement_record", ["document_id"], unique=False
    )
    op.create_index(
        op.f("ix_statement_record_household_id"),
        "statement_record",
        ["household_id"],
        unique=False,
    )
    for statement in _security_ddl(_app_role()):
        op.execute(statement)


def downgrade() -> None:
    op.drop_index(op.f("ix_statement_record_household_id"), table_name="statement_record")
    op.drop_index(op.f("ix_statement_record_document_id"), table_name="statement_record")
    op.drop_index("ix_statement_record_account_period", table_name="statement_record")
    op.drop_table("statement_record")

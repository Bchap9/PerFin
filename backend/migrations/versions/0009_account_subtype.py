# SPDX-License-Identifier: AGPL-3.0-or-later
"""Account subtypes.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-05 22:00:00.000000+00:00

Column added here (``app.domain.accounts.models``):

* ``account.subtype`` (varchar(40), NULL for every existing row): what kind of
  account it is within its type (``roth_ira``, ``401k``, ``education_529``,
  ``heloc``...; ``app.domain.accounts.subtypes``). A short code checked for
  shape here and against its type by the application, so a new subtype needs
  no migration. No new table, no RLS change: the table keeps its policies
  and grants.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("account", sa.Column("subtype", sa.String(length=40), nullable=True))
    op.create_check_constraint(
        op.f("ck_account_subtype_code"),
        "account",
        "subtype IS NULL OR subtype ~ '^[a-z0-9_]{1,40}$'",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_account_subtype_code"), "account", type_="check")
    op.drop_column("account", "subtype")

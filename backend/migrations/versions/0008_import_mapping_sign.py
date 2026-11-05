# SPDX-License-Identifier: AGPL-3.0-or-later
"""Import mappings remember a sign read on trust.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-05 12:00:00.000000+00:00

Column added here (``app.domain.imports.models``):

* ``import_mapping.sign_unverified`` (boolean, ``false`` for every existing
  row): the importer saved the mapping from a suggestion whose single signed
  amount column was read as "positive is money in" on trust, so it does not
  apply the mapping to a liability account on its own (INGESTION.md
  "Never-seen layouts"). No new table, no RLS change: the table keeps its
  policies and grants.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "import_mapping",
        sa.Column("sign_unverified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("import_mapping", "sign_unverified")

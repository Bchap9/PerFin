# SPDX-License-Identifier: AGPL-3.0-or-later
"""Securities without a symbol.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-06 20:00:00.000000+00:00

A statement may print a holding by its name alone (an unlisted partnership,
a 529 portfolio: no ticker, CUSIP or ISIN). The importer keeps such a
security under its name (``app.domain.investments.statement_import``), so
``security.symbol`` may be NULL (``app.domain.investments.models``):

* ``symbol`` drops ``NOT NULL``; ``ck_security_symbol_format`` checks a
  symbol that is there;
* ``(household_id, symbol, exchange)`` stays unique among securities with a
  symbol (NULL exchange still counts as one value): the unique constraint
  ``uq_security_household_symbol_exchange`` becomes a partial unique index
  of the same name, so several securities may have no symbol.

No new table, no RLS change: the table keeps its policies and grants.
Downgrading fails (``NOT NULL`` violated) while a security has no symbol:
give each one a symbol first.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNIQUE = "uq_security_household_symbol_exchange"
_FORMAT = "^[A-Za-z0-9.^/_:-]{1,32}$"


def upgrade() -> None:
    op.drop_constraint(_UNIQUE, "security", type_="unique")
    op.create_index(
        _UNIQUE,
        "security",
        ["household_id", "symbol", "exchange"],
        unique=True,
        postgresql_nulls_not_distinct=True,
        postgresql_where=sa.text("symbol IS NOT NULL"),
    )
    op.drop_constraint(op.f("ck_security_symbol_format"), "security", type_="check")
    op.create_check_constraint(
        op.f("ck_security_symbol_format"),
        "security",
        f"symbol IS NULL OR symbol ~ '{_FORMAT}'",
    )
    op.alter_column("security", "symbol", existing_type=sa.String(length=32), nullable=True)


def downgrade() -> None:
    op.alter_column("security", "symbol", existing_type=sa.String(length=32), nullable=False)
    op.drop_constraint(op.f("ck_security_symbol_format"), "security", type_="check")
    op.create_check_constraint(
        op.f("ck_security_symbol_format"), "security", f"symbol ~ '{_FORMAT}'"
    )
    op.drop_index(_UNIQUE, table_name="security")
    op.create_unique_constraint(
        _UNIQUE,
        "security",
        ["household_id", "symbol", "exchange"],
        postgresql_nulls_not_distinct=True,
    )

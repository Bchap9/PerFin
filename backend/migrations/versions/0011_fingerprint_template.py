# SPDX-License-Identifier: AGPL-3.0-or-later
"""Learned layouts ("Teach PerFin").

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-06 18:00:00.000000+00:00

Column added here (``app.domain.documents.models``):

* ``parser_fingerprint.template`` (jsonb, NULL for every existing row): what
  a household taught PerFin about the layout
  (``app.ingestion.parsers.template.LayoutTemplate``: field anchors as label
  text and direction, labelled tables as column headers and roles, lines to
  ignore, free-text notes). Never a value of the document: labels and
  headers with digits are refused by the application. Checked to be a JSON
  object here. No new table, no RLS change: the table keeps its policies and
  grants.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "parser_fingerprint",
        sa.Column("template", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_parser_fingerprint_template_object"),
        "parser_fingerprint",
        "template IS NULL OR jsonb_typeof(template) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_parser_fingerprint_template_object"), "parser_fingerprint", type_="check"
    )
    op.drop_column("parser_fingerprint", "template")

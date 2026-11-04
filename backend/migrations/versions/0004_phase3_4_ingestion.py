# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phases 3+4 smart ingestion: drop folder and email intake tables, search backfill.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-04 07:00:00.866202+00:00

Most of the ingestion schema was created by 0003 already: ``document`` with
its ``extracted_text`` ``tsvector`` column and GIN index
``ix_document_extracted_text``, ``parser_fingerprint`` (user "remember this
layout" fingerprints; the shipped ones are YAML in the code), ``review_item``,
``ingest_job`` and ``blob``. Previews need no table: they are encrypted
permanent blobs referenced from ``document.extraction`` (DATA_MODEL.md).

Tables created here (all ``HouseholdScoped``: ``ENABLE`` + ``FORCE ROW LEVEL
SECURITY`` with the ``household_isolation`` policy, and DML grants for the
application role ``PERFIN_DB_APP_ROLE``):

* ``dropfolder_route`` (``app.domain.dropfolder.models``): remembered
  folder -> member/account routes for the drop folder (unknown_owner review).
* ``dropfolder_entry``: one row per drop-folder file seen (identity, outcome,
  after-action state), so a file is never ingested twice.
* ``mail_intake`` (``app.domain.intake_settings.models``): the household's
  mailbox settings with encrypted credentials and alias (D1, D5).
* ``mail_intake_message``: one row per mail message decided (accepted or
  rejected), keyed by a keyed hash of its identity.

Enum step: ``review_kind`` gains ``receipt_match`` (the receipt-to-transaction
suggestion item, Phase 4). Alembic autogenerate does not compare enum labels,
so this is written by hand; ``test_migrations`` compares every enum's labels
with the models. The downgrade deletes ``receipt_match`` review items (with
RLS lifted on ``review_item`` for that statement) and recreates the type with
the 0003 labels.

References to ``document`` and ``account`` include ``household_id``
(ADR-0008): a plain ``SET NULL`` key plus a deferred composite one.

Data step: documents extracted before this revision have no search vector
(the Phase 2 worker did not write one). The upgrade backfills
``extracted_text`` in SQL from the stored extraction (the mail subject, the
text or page text and the table cells, the same parts as
``app.domain.documents.search.index_text``), from a prefix of at most
:data:`BACKFILL_TEXT_MAX` characters so no vector reaches PostgreSQL's size
limit. ``FORCE ROW LEVEL SECURITY`` is lifted on ``document`` for that one
statement so the owner sees every household's rows (as 0003 does for
``account_grant``). Re-processing a document rewrites its vector with the
worker's own code. The downgrade drops the four tables; the backfilled
vectors stay (0003's column, harmless).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

from app.db.rls import all_rls_ddl, table_grants_ddl

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables created by this revision (all household-scoped), in dependency order.
TABLES: tuple[str, ...] = (
    "mail_intake",
    "dropfolder_route",
    "dropfolder_entry",
    "mail_intake_message",
)
#: Household-scoped tables: forced RLS with ``household_isolation`` only.
RLS_TABLES: tuple[str, ...] = TABLES
#: ``review_kind`` labels as 0003 created them (the downgrade restores these).
REVIEW_KINDS_0003: tuple[str, ...] = (
    "unknown_owner",
    "unknown_account",
    "low_confidence_field",
    "possible_duplicate",
    "unparsed",
    "new_institution",
    "rejected_email",
)
#: Labels this revision adds to ``review_kind``.
REVIEW_KINDS_ADDED: tuple[str, ...] = ("receipt_match",)
#: Characters of stored extraction text the search backfill indexes (in the SQL below).
BACKFILL_TEXT_MAX = 100_000

_BACKFILL_SEARCH = """
WITH src AS (
    SELECT d.id,
           concat_ws(
               E'\\n',
               d.extraction #>> '{email,headers,subject}',
               COALESCE(
                   NULLIF(d.extraction ->> 'text', ''),
                   (SELECT string_agg(COALESCE(p ->> 'text', ''), E'\\f')
                      FROM jsonb_array_elements(
                          CASE WHEN jsonb_typeof(d.extraction -> 'pages') = 'array'
                               THEN d.extraction -> 'pages' ELSE '[]'::jsonb END) AS p)
               ),
               (SELECT string_agg(
                           (SELECT string_agg(c #>> '{}', ' ')
                              FROM jsonb_array_elements(
                                  CASE WHEN jsonb_typeof(r) = 'array'
                                       THEN r ELSE '[]'::jsonb END) AS c
                             WHERE jsonb_typeof(c) <> 'null' AND c #>> '{}' <> ''),
                           E'\\n')
                  FROM jsonb_array_elements(
                      CASE WHEN jsonb_typeof(d.extraction -> 'tables') = 'array'
                           THEN d.extraction -> 'tables' ELSE '[]'::jsonb END) AS t,
                       jsonb_array_elements(
                      CASE WHEN jsonb_typeof(t -> 'rows') = 'array'
                           THEN t -> 'rows' ELSE '[]'::jsonb END) AS r)
           ) AS body
      FROM document AS d
     WHERE d.extracted_text IS NULL
       AND jsonb_typeof(d.extraction) = 'object'
       AND d.extraction ->> 'ok' = 'true'
)
UPDATE document AS d
   SET extracted_text = to_tsvector(
           'simple'::regconfig,
           left(translate(src.body, chr(57344) || chr(57345) || chr(57346), '   '),
                100000))
  FROM src
 WHERE d.id = src.id AND btrim(COALESCE(src.body, '')) <> ''
"""


def _app_role() -> str:
    """Role granted DML: ``perfin_app_role`` attribute (tests) or the setting."""
    supplied = context.config.attributes.get("perfin_app_role")
    if isinstance(supplied, str) and supplied:
        return supplied
    from app.core.settings import get_settings

    return get_settings().db_app_role


def _security_ddl(app_role: str) -> list[str]:
    return [*all_rls_ddl(RLS_TABLES), *table_grants_ddl(TABLES, app_role)]


def _backfill_search_vectors() -> None:
    """Index documents extracted before Phase 3 (see the module docstring)."""
    op.execute("ALTER TABLE document NO FORCE ROW LEVEL SECURITY")
    op.execute(_BACKFILL_SEARCH)
    op.execute("ALTER TABLE document FORCE ROW LEVEL SECURITY")


def _add_review_kinds() -> None:
    # Not usable in this transaction (PostgreSQL), and nothing here uses it.
    for label in REVIEW_KINDS_ADDED:
        op.execute(f"ALTER TYPE review_kind ADD VALUE IF NOT EXISTS '{label}'")


def _drop_review_kinds() -> None:
    kept = ", ".join(f"'{label}'" for label in REVIEW_KINDS_0003)
    op.execute("ALTER TABLE review_item NO FORCE ROW LEVEL SECURITY")
    for label in REVIEW_KINDS_ADDED:
        op.execute(
            sa.text("DELETE FROM review_item WHERE kind::text = :kind").bindparams(kind=label)
        )
    op.execute("ALTER TABLE review_item FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TYPE review_kind RENAME TO review_kind_0004")
    op.execute(f"CREATE TYPE review_kind AS ENUM ({kept})")
    op.execute(
        "ALTER TABLE review_item ALTER COLUMN kind TYPE review_kind USING kind::text::review_kind"
    )
    op.execute("DROP TYPE review_kind_0004")


def upgrade() -> None:
    _add_review_kinds()
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "mail_intake",
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=True),
        sa.Column("port", sa.Integer(), server_default=sa.text("993"), nullable=False),
        sa.Column("username_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("password_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("inbox_folder", sa.String(length=200), nullable=False),
        sa.Column("processed_folder", sa.String(length=200), nullable=False),
        sa.Column("rejected_folder", sa.String(length=200), nullable=False),
        sa.Column("alias_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("alias_rotated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "allowed_senders",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("member_addresses", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "require_auth_results", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("authserv_id", sa.String(length=253), nullable=True),
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_poll_outcome", sa.String(length=32), nullable=True),
        sa.Column(
            "last_poll_counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
            "jsonb_typeof(allowed_senders) = 'array'",
            name=op.f("ck_mail_intake_allowed_senders_array"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(last_poll_counts) = 'object'",
            name=op.f("ck_mail_intake_last_poll_counts_object"),
        ),
        sa.CheckConstraint(
            "NOT enabled OR (host IS NOT NULL AND username_encrypted IS NOT NULL "
            "AND password_encrypted IS NOT NULL)",
            name=op.f("ck_mail_intake_enabled_needs_credentials"),
        ),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name=op.f("ck_mail_intake_port_range")),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_mail_intake_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_intake")),
        sa.UniqueConstraint("household_id", name="uq_mail_intake_household"),
    )
    op.create_index(
        op.f("ix_mail_intake_household_id"), "mail_intake", ["household_id"], unique=False
    )
    op.create_table(
        "dropfolder_route",
        sa.Column("folder", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=True),
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
            "folder !~ '[[:cntrl:]]'", name=op.f("ck_dropfolder_route_folder_printable")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
            name=op.f("fk_dropfolder_route_account_id_account"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["household_id", "account_id"],
            ["account.household_id", "account.id"],
            name="fk_dropfolder_route_account_id_hh_account",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_dropfolder_route_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_dropfolder_route_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dropfolder_route")),
        sa.UniqueConstraint("household_id", "folder", name="uq_dropfolder_route_folder"),
    )
    op.create_index(
        op.f("ix_dropfolder_route_account_id"), "dropfolder_route", ["account_id"], unique=False
    )
    op.create_index(
        op.f("ix_dropfolder_route_household_id"), "dropfolder_route", ["household_id"], unique=False
    )
    op.create_table(
        "dropfolder_entry",
        sa.Column("path_key", sa.CHAR(length=64), nullable=False),
        sa.Column("rel_path", sa.String(length=1024), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("inode", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.CHAR(length=64), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("after_state", sa.String(length=16), nullable=False),
        sa.Column("after_result", sa.String(length=32), nullable=True),
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
            "after_state IN ('pending', 'done', 'kept', 'error')",
            name=op.f("ck_dropfolder_entry_after_state_known"),
        ),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'rejected', 'skipped')",
            name=op.f("ck_dropfolder_entry_outcome_known"),
        ),
        sa.CheckConstraint(
            "path_key ~ '^[0-9a-f]{64}$'", name=op.f("ck_dropfolder_entry_path_key_hex")
        ),
        sa.CheckConstraint(
            "sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_dropfolder_entry_sha256_hex"),
        ),
        sa.CheckConstraint("size >= 0", name=op.f("ck_dropfolder_entry_size_non_negative")),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name=op.f("fk_dropfolder_entry_document_id_document"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["household_id", "document_id"],
            ["document.household_id", "document.id"],
            name="fk_dropfolder_entry_document_id_hh_document",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_dropfolder_entry_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dropfolder_entry")),
        sa.UniqueConstraint(
            "household_id", "path_key", "size", "mtime_ns", "inode", name="uq_dropfolder_entry_file"
        ),
    )
    op.create_index(
        op.f("ix_dropfolder_entry_document_id"), "dropfolder_entry", ["document_id"], unique=False
    )
    op.create_index(
        "ix_dropfolder_entry_household_after_state",
        "dropfolder_entry",
        ["household_id", "after_state"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dropfolder_entry_household_id"), "dropfolder_entry", ["household_id"], unique=False
    )
    op.create_index(
        "ix_dropfolder_entry_household_sha256",
        "dropfolder_entry",
        ["household_id", "sha256"],
        unique=False,
    )
    op.create_table(
        "mail_intake_message",
        sa.Column("message_key", sa.CHAR(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
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
            "message_key ~ '^[0-9a-f]{64}$'", name=op.f("ck_mail_intake_message_message_key_hex")
        ),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'rejected')", name=op.f("ck_mail_intake_message_outcome_known")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name=op.f("fk_mail_intake_message_document_id_document"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["household_id", "document_id"],
            ["document.household_id", "document.id"],
            name="fk_mail_intake_message_document_id_hh_document",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_mail_intake_message_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mail_intake_message")),
        sa.UniqueConstraint("household_id", "message_key", name="uq_mail_intake_message_key"),
    )
    op.create_index(
        op.f("ix_mail_intake_message_document_id"),
        "mail_intake_message",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_mail_intake_message_household_id"),
        "mail_intake_message",
        ["household_id"],
        unique=False,
    )
    # ### end Alembic commands ###
    for statement in _security_ddl(_app_role()):
        op.execute(statement)
    _backfill_search_vectors()


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_mail_intake_message_household_id"), table_name="mail_intake_message")
    op.drop_index(op.f("ix_mail_intake_message_document_id"), table_name="mail_intake_message")
    op.drop_table("mail_intake_message")
    op.drop_index("ix_dropfolder_entry_household_sha256", table_name="dropfolder_entry")
    op.drop_index(op.f("ix_dropfolder_entry_household_id"), table_name="dropfolder_entry")
    op.drop_index("ix_dropfolder_entry_household_after_state", table_name="dropfolder_entry")
    op.drop_index(op.f("ix_dropfolder_entry_document_id"), table_name="dropfolder_entry")
    op.drop_table("dropfolder_entry")
    op.drop_index(op.f("ix_dropfolder_route_household_id"), table_name="dropfolder_route")
    op.drop_index(op.f("ix_dropfolder_route_account_id"), table_name="dropfolder_route")
    op.drop_table("dropfolder_route")
    op.drop_index(op.f("ix_mail_intake_household_id"), table_name="mail_intake")
    op.drop_table("mail_intake")
    # ### end Alembic commands ###
    _drop_review_kinds()

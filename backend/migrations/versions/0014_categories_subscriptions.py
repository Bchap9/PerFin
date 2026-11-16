# SPDX-License-Identifier: AGPL-3.0-or-later
"""Category provenance, merchant memory and subscription states.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-16 12:00:00.000000+00:00

Columns added to ``transaction`` (``app.domain.transactions.models``): how a
category was decided and what the row belongs to.

* ``merchant_key``: who was paid, normalised (indexed with the household);
* ``category_source`` (checked: ``person``, ``file``, ``rule``, ``payee``,
  ``learned``, ``dictionary``, ``keyword``, ``legacy``), ``category_confidence``
  (0..1) and ``category_rule_id`` (the rule, cleared with it);
* ``suggested_category_id``: a guess below the line, allowed only while
  ``category_id`` is NULL (checked), cleared with the category;
* ``scheduled_transaction_id``: the recurring series a charge belongs to,
  cleared with it.

The three references are household-consistent (ADR-0008): a plain
``SET NULL`` key plus a deferred composite one, for which ``rule`` and
``scheduled_transaction`` gain ``UNIQUE (household_id, id)``.

Columns added to ``scheduled_transaction``: ``kind`` and ``status`` with who
set each (``kind_source``, ``status_source``), ``merchant_key``,
``first_seen_on``, ``occurrences``, ``confidence``, ``last_amount``,
``previous_amount``, ``price_changed_on`` and ``ended_on``. ``active`` stays
and a check ties it to the status: ``active = (status IN ('active',
'confirmed'))``.

Tables created, household-scoped (``ENABLE`` + ``FORCE ROW LEVEL SECURITY``
with the ``household_isolation`` policy, and DML grants for the application
role ``PERFIN_DB_APP_ROLE``), listed explicitly in :data:`TABLES`:

* ``merchant_memory``: the household's counted corrections per merchant key,
  direction and category (``app.domain.categorisation.models``);
* ``subscription_event``: one change of one recurring series
  (``app.domain.recurring.models``).

Backfill (:data:`BACKFILL`), plain SQL that does **not** set ``updated_at``
(the importer reads ``updated_at == created_at`` as "untouched since
import", and a migration is not a person):

a. a transaction that has a category, or splits, was categorised before
   provenance existed: ``category_source = 'legacy'`` (a guess never
   overwrites it);
b. a schedule entered by hand that is active: ``confirmed`` by a ``person``;
   an active detected one: ``active`` by the ``detector`` (the column
   defaults); one that is switched off: ``paused`` by a ``person``;
c. a schedule of money coming in (``amount_min >= 0``) is ``income``; every
   other one is ``other`` until detection looks at it again.

The backfill runs as the table owner with forced row-level security lifted
for its statements on the tables it reads or writes, as revision ``0013``
does. The check that ties ``active`` to ``status`` is added after it. The
downgrade drops the tables, then the constraints, indexes and columns, in
reverse; ``active`` is untouched by either direction. No enum type: the
short codes are checked here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from app.db.rls import all_rls_ddl, table_grants_ddl

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables created by this revision (all household-scoped).
TABLES: tuple[str, ...] = ("merchant_memory", "subscription_event")
#: Household-scoped tables: forced RLS with ``household_isolation`` only.
RLS_TABLES: tuple[str, ...] = TABLES
#: Tables the backfill reads or writes across households (forced RLS is
#: lifted for the owner while it runs, then restored).
BACKFILL_TABLES: tuple[str, ...] = ("transaction", "transaction_split", "scheduled_transaction")

_LEGACY = """
UPDATE "transaction" AS t
SET category_source = 'legacy'
WHERE t.category_id IS NOT NULL
   OR EXISTS (
       SELECT 1
       FROM transaction_split AS s
       WHERE s.household_id = t.household_id
         AND s.transaction_id = t.id
   )
"""

_CONFIRMED = """
UPDATE scheduled_transaction
SET status = 'confirmed', status_source = 'person'
WHERE active AND origin = 'manual'
"""

_PAUSED = """
UPDATE scheduled_transaction
SET status = 'paused', status_source = 'person'
WHERE NOT active
"""

_INCOME = """
UPDATE scheduled_transaction
SET kind = 'income'
WHERE amount_min >= 0
"""

#: The backfill statements, in the order of the module docstring.
BACKFILL: tuple[str, ...] = (_LEGACY, _CONFIRMED, _PAUSED, _INCOME)

_SOURCES = "'person', 'file', 'rule', 'payee', 'learned', 'dictionary', 'keyword', 'legacy'"
_KINDS = "'subscription', 'bill', 'income', 'other'"
_STATUSES = "'suggested', 'active', 'confirmed', 'paused', 'ended', 'dismissed'"
_DECIDERS = "'detector', 'person'"
_EVENT_KINDS = (
    "'new', 'price_changed', 'missed', 'ended', 'charged_after_end', 'renewal_due', "
    "'trial_converted'"
)

#: ``(column, target)`` of the three optional references added to ``transaction``.
_TRANSACTION_REFS: tuple[tuple[str, str], ...] = (
    ("category_rule_id", "rule"),
    ("suggested_category_id", "category"),
    ("scheduled_transaction_id", "scheduled_transaction"),
)

#: ``(name, columns, predicate)`` of the partial indexes added to ``transaction``.
_TRANSACTION_INDEXES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "ix_transaction_household_merchant",
        ("household_id", "merchant_key"),
        "merchant_key IS NOT NULL",
    ),
    (
        "ix_transaction_schedule",
        ("scheduled_transaction_id",),
        "scheduled_transaction_id IS NOT NULL",
    ),
    (
        "ix_transaction_suggested",
        ("household_id", "suggested_category_id"),
        "suggested_category_id IS NOT NULL",
    ),
    ("ix_transaction_category_rule", ("category_rule_id",), "category_rule_id IS NOT NULL"),
)

#: ``(name, expression)`` of the checks added to ``transaction``.
_TRANSACTION_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "ck_transaction_category_source_known",
        f"category_source IS NULL OR category_source IN ({_SOURCES})",
    ),
    (
        "ck_transaction_category_confidence_range",
        "category_confidence IS NULL OR (category_confidence >= 0 AND category_confidence <= 1)",
    ),
    (
        "ck_transaction_suggestion_only_without_category",
        "suggested_category_id IS NULL OR category_id IS NULL",
    ),
)

#: ``(name, expression)`` of the checks added to ``scheduled_transaction``.
_SCHEDULE_CHECKS: tuple[tuple[str, str], ...] = (
    ("ck_scheduled_transaction_kind_known", f"kind IN ({_KINDS})"),
    ("ck_scheduled_transaction_status_known", f"status IN ({_STATUSES})"),
    (
        "ck_scheduled_transaction_deciders_known",
        f"kind_source IN ({_DECIDERS}) AND status_source IN ({_DECIDERS})",
    ),
    (
        "ck_scheduled_transaction_active_matches_status",
        "active = (status IN ('active', 'confirmed'))",
    ),
    ("ck_scheduled_transaction_occurrences_non_negative", "occurrences >= 0"),
    (
        "ck_scheduled_transaction_confidence_range",
        "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
    ),
)

#: Columns added to ``scheduled_transaction``, in the order they are added.
_SCHEDULE_COLUMNS: tuple[str, ...] = (
    "kind",
    "kind_source",
    "status",
    "status_source",
    "merchant_key",
    "first_seen_on",
    "occurrences",
    "confidence",
    "last_amount",
    "previous_amount",
    "price_changed_on",
    "ended_on",
)

#: Columns added to ``transaction``, in the order they are added.
_TRANSACTION_COLUMNS: tuple[str, ...] = (
    "merchant_key",
    "category_source",
    "category_confidence",
    "category_rule_id",
    "suggested_category_id",
    "scheduled_transaction_id",
)


def _hh_fk(table: str, column: str, target: str) -> str:
    """The name ``app.db.types.household_fk`` / ``household_ref_fk`` give the key."""
    return f"fk_{table}_{column}_hh_{target}"[:63]


def _app_role() -> str:
    """Role granted DML: ``perfin_app_role`` attribute (tests) or the setting."""
    supplied = context.config.attributes.get("perfin_app_role")
    if isinstance(supplied, str) and supplied:
        return supplied
    from app.core.settings import get_settings

    return get_settings().db_app_role


def _security_ddl(app_role: str) -> list[str]:
    return [*all_rls_ddl(RLS_TABLES), *table_grants_ddl(TABLES, app_role)]


def _backfill() -> None:
    """Mark what existed before this revision (see the module docstring)."""
    for table in BACKFILL_TABLES:
        op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
    for statement in BACKFILL:
        op.execute(statement)
    for table in BACKFILL_TABLES:
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')


def _timestamps() -> list[sa.Column[object]]:
    return [
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
    ]


def _upgrade_schedules() -> None:
    table = "scheduled_transaction"
    op.add_column(
        table,
        sa.Column("kind", sa.String(length=12), server_default=sa.text("'other'"), nullable=False),
    )
    op.add_column(
        table,
        sa.Column(
            "kind_source", sa.String(length=8), server_default=sa.text("'detector'"), nullable=False
        ),
    )
    op.add_column(
        table,
        sa.Column(
            "status", sa.String(length=12), server_default=sa.text("'active'"), nullable=False
        ),
    )
    op.add_column(
        table,
        sa.Column(
            "status_source",
            sa.String(length=8),
            server_default=sa.text("'detector'"),
            nullable=False,
        ),
    )
    op.add_column(table, sa.Column("merchant_key", sa.String(length=64), nullable=True))
    op.add_column(table, sa.Column("first_seen_on", sa.Date(), nullable=True))
    op.add_column(
        table,
        sa.Column("occurrences", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(table, sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True))
    op.add_column(table, sa.Column("last_amount", sa.Numeric(precision=20, scale=6), nullable=True))
    op.add_column(
        table, sa.Column("previous_amount", sa.Numeric(precision=20, scale=6), nullable=True)
    )
    op.add_column(table, sa.Column("price_changed_on", sa.Date(), nullable=True))
    op.add_column(table, sa.Column("ended_on", sa.Date(), nullable=True))


def _upgrade_transactions() -> None:
    table = "transaction"
    op.add_column(table, sa.Column("merchant_key", sa.String(length=64), nullable=True))
    op.add_column(table, sa.Column("category_source", sa.String(length=12), nullable=True))
    op.add_column(
        table, sa.Column("category_confidence", sa.Numeric(precision=5, scale=4), nullable=True)
    )
    for column, _ in _TRANSACTION_REFS:
        op.add_column(table, sa.Column(column, sa.Uuid(), nullable=True))
    for column, target in _TRANSACTION_REFS:
        op.create_foreign_key(
            op.f(f"fk_transaction_{column}_{target}"),
            table,
            target,
            [column],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_foreign_key(
            _hh_fk(table, column, target),
            table,
            target,
            ["household_id", column],
            ["household_id", "id"],
            deferrable=True,
            initially="DEFERRED",
        )
    for name, columns, predicate in _TRANSACTION_INDEXES:
        op.create_index(
            name, table, list(columns), unique=False, postgresql_where=sa.text(predicate)
        )


def _create_tables() -> None:
    op.create_table(
        "merchant_memory",
        sa.Column("merchant_key", sa.String(length=64), nullable=False),
        sa.Column("direction", sa.String(length=3), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("decisions", sa.Integer(), nullable=False),
        sa.Column("last_decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "merchant_key ~ '^[a-z0-9]+( [a-z0-9]+)*$'",
            name=op.f("ck_merchant_memory_merchant_key_form"),
        ),
        sa.CheckConstraint(
            "direction IN ('out', 'in')", name=op.f("ck_merchant_memory_direction_known")
        ),
        sa.CheckConstraint("decisions >= 1", name=op.f("ck_merchant_memory_decisions_positive")),
        sa.ForeignKeyConstraint(
            ["household_id", "category_id"],
            ["category.household_id", "category.id"],
            name=_hh_fk("merchant_memory", "category_id", "category"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_merchant_memory_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merchant_memory")),
        sa.UniqueConstraint(
            "household_id",
            "merchant_key",
            "direction",
            "category_id",
            name="uq_merchant_memory_choice",
        ),
    )
    op.create_index(
        op.f("ix_merchant_memory_category_id"), "merchant_memory", ["category_id"], unique=False
    )
    op.create_index(
        op.f("ix_merchant_memory_household_id"), "merchant_memory", ["household_id"], unique=False
    )
    op.create_table(
        "subscription_event",
        sa.Column("schedule_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("amount_before", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("amount_after", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("transaction_id", sa.Uuid(), nullable=True),
        sa.Column("state", sa.String(length=8), server_default=sa.text("'open'"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            f"kind IN ({_EVENT_KINDS})", name=op.f("ck_subscription_event_kind_known")
        ),
        sa.CheckConstraint(
            "state IN ('open', 'seen')", name=op.f("ck_subscription_event_state_known")
        ),
        sa.ForeignKeyConstraint(
            ["household_id", "schedule_id"],
            ["scheduled_transaction.household_id", "scheduled_transaction.id"],
            name=_hh_fk("subscription_event", "schedule_id", "scheduled_transaction"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["household_id", "account_id"],
            ["account.household_id", "account.id"],
            name=_hh_fk("subscription_event", "account_id", "account"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["household_id", "transaction_id"],
            ["transaction.household_id", "transaction.id"],
            name=_hh_fk("subscription_event", "transaction_id", "transaction"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transaction.id"],
            name=op.f("fk_subscription_event_transaction_id_transaction"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_subscription_event_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription_event")),
        sa.UniqueConstraint(
            "household_id",
            "schedule_id",
            "kind",
            "occurred_on",
            name="uq_subscription_event_once",
        ),
    )
    for column in ("account_id", "household_id", "schedule_id", "transaction_id"):
        op.create_index(
            op.f(f"ix_subscription_event_{column}"), "subscription_event", [column], unique=False
        )


def upgrade() -> None:
    # The targets of the new household-consistent references.
    op.create_unique_constraint("uq_rule_household_id_id", "rule", ["household_id", "id"])
    op.create_unique_constraint(
        "uq_scheduled_transaction_household_id_id",
        "scheduled_transaction",
        ["household_id", "id"],
    )
    _upgrade_schedules()
    _upgrade_transactions()
    _backfill()
    # After the backfill: a switched-off schedule had the default status until then.
    for name, expression in _SCHEDULE_CHECKS:
        op.create_check_constraint(op.f(name), "scheduled_transaction", expression)
    for name, expression in _TRANSACTION_CHECKS:
        op.create_check_constraint(op.f(name), "transaction", expression)
    _create_tables()
    for statement in _security_ddl(_app_role()):
        op.execute(statement)


def downgrade() -> None:
    for column in ("transaction_id", "schedule_id", "household_id", "account_id"):
        op.drop_index(op.f(f"ix_subscription_event_{column}"), table_name="subscription_event")
    op.drop_table("subscription_event")
    op.drop_index(op.f("ix_merchant_memory_household_id"), table_name="merchant_memory")
    op.drop_index(op.f("ix_merchant_memory_category_id"), table_name="merchant_memory")
    op.drop_table("merchant_memory")
    for name, _ in reversed(_TRANSACTION_CHECKS):
        op.drop_constraint(op.f(name), "transaction", type_="check")
    for name, _ in reversed(_SCHEDULE_CHECKS):
        op.drop_constraint(op.f(name), "scheduled_transaction", type_="check")
    for name, _, _ in reversed(_TRANSACTION_INDEXES):
        op.drop_index(name, table_name="transaction")
    for column, target in reversed(_TRANSACTION_REFS):
        op.drop_constraint(_hh_fk("transaction", column, target), "transaction", type_="foreignkey")
        op.drop_constraint(
            op.f(f"fk_transaction_{column}_{target}"), "transaction", type_="foreignkey"
        )
    for column in reversed(_TRANSACTION_COLUMNS):
        op.drop_column("transaction", column)
    for column in reversed(_SCHEDULE_COLUMNS):
        op.drop_column("scheduled_transaction", column)
    op.drop_constraint(
        "uq_scheduled_transaction_household_id_id", "scheduled_transaction", type_="unique"
    )
    op.drop_constraint("uq_rule_household_id_id", "rule", type_="unique")

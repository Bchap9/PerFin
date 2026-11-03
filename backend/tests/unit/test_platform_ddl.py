# SPDX-License-Identifier: AGPL-3.0-or-later
"""Platform DDL generation, model definitions and engine options (no database)."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from sqlalchemy import Engine, Index, Table, event, text
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex, CreateTable

from app.core.audit import VERIFY_SQL
from app.core.settings import Settings
from app.db.base import Base, household_scoped_tables
from app.db.models import AuditLog, Event, Job, JobState, Setting
from app.db.registry import import_all_models
from app.db.rls import (
    AUDIT_CHAIN_LOCK_KEY,
    AUDIT_GENESIS_HASH,
    AUDIT_HASH_COLUMNS,
    AUDIT_HASH_FUNCTION,
    CTX_HOUSEHOLD,
    CTX_INSTANCE_ADMIN,
    CTX_JOB_QUEUE,
    CTX_SYSTEM_ACTOR,
    CUSTOM_RLS,
    JOB_IMMUTABLE_COLUMNS,
    PLATFORM_FUNCTIONS,
    Policy,
    audit_ddl,
    custom_rls_ddl,
    downgrade_ddl,
    grants_ddl,
    household_ddl,
    job_ddl,
    schema_ddl,
    set_context,
)
from app.db.session import connection_options, make_app_engine, make_engine


def _ddl() -> list[str]:
    import_all_models()
    return schema_ddl(Base.metadata, "perfin_app")


def test_schema_ddl_has_no_accidental_bind_parameters() -> None:
    # Alembic's op.execute wraps strings in text(); a stray ":name" would
    # become a bind parameter and break the migration.
    for statement in _ddl() + downgrade_ddl():
        assert not text(statement).compile().params, statement
        assert "%" not in statement, statement


def test_schema_ddl_covers_household_and_custom_tables() -> None:
    statements = _ddl()
    assert "event" in household_scoped_tables()
    assert 'ALTER TABLE "event" FORCE ROW LEVEL SECURITY' in statements
    assert 'ALTER TABLE "setting" FORCE ROW LEVEL SECURITY' in statements
    assert 'ALTER TABLE "audit_log" ENABLE ROW LEVEL SECURITY' in statements
    assert 'ALTER TABLE "audit_log" NO FORCE ROW LEVEL SECURITY' in statements
    # Every platform table has RLS (review findings SR-1, SR-3, SR-4).
    for table in ("household", "job"):
        assert f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY' in statements
        assert f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY' in statements
    assert 'GRANT SELECT, INSERT ON "audit_log" TO "perfin_app"' in statements
    assert all(s in statements for s in audit_ddl())
    assert all(s in statements for s in job_ddl())
    assert all(s in statements for s in household_ddl("perfin_app"))
    # Grants come last so a migration never grants before policies exist.
    last_policy = max(i for i, s in enumerate(statements) if "POLICY" in s)
    first_grant = min(i for i, s in enumerate(statements) if s.startswith("GRANT"))
    assert last_policy < first_grant


def test_audit_policies_never_allow_update_or_delete() -> None:
    commands = {policy.command for policy in CUSTOM_RLS["audit_log"].policies}
    assert commands == {"SELECT", "INSERT"}
    statements = custom_rls_ddl("audit_log", CUSTOM_RLS["audit_log"])
    assert not any("FOR UPDATE" in s or "FOR DELETE" in s or "FOR ALL" in s for s in statements)


def test_audit_triggers_cover_update_delete_truncate() -> None:
    joined = "\n".join(audit_ddl())
    assert 'BEFORE UPDATE OR DELETE ON "audit_log"' in joined
    assert 'BEFORE TRUNCATE ON "audit_log"' in joined
    assert 'BEFORE INSERT ON "audit_log"' in joined
    assert "SECURITY DEFINER" in joined
    assert "SET search_path = pg_catalog, pg_temp" in joined
    assert "pg_advisory_xact_lock" in joined
    assert "REVOKE ALL ON FUNCTION perfin_audit_log_chain() FROM PUBLIC" in joined


def test_audit_constants_match_sql() -> None:
    joined = "\n".join(audit_ddl())
    assert f"pg_advisory_xact_lock({AUDIT_CHAIN_LOCK_KEY})" in joined
    assert AUDIT_GENESIS_HASH == "0" * 64
    assert "coalesce(last_hash, repeat('0', 64))" in joined
    assert f"CREATE OR REPLACE FUNCTION {AUDIT_HASH_FUNCTION}(" in joined
    assert f"NEW.hash := public.{AUDIT_HASH_FUNCTION}(" in joined
    assert any(
        s.endswith(
            f"{AUDIT_HASH_FUNCTION}(text, bigint, uuid, uuid, uuid, text, text, "
            "text, jsonb, jsonb, inet, text, text, timestamptz)"
        )
        for s in downgrade_ddl()
    )


def test_verify_sql_passes_hash_columns_in_order() -> None:
    call = f"{AUDIT_HASH_FUNCTION}(" + ", ".join(AUDIT_HASH_COLUMNS) + ")"
    assert call in VERIFY_SQL


def test_hash_columns_match_model() -> None:
    columns = set(AuditLog.__table__.c.keys())
    assert set(AUDIT_HASH_COLUMNS) == columns - {"hash"}


def test_downgrade_drops_every_function() -> None:
    statements = downgrade_ddl()
    assert len(statements) == len(PLATFORM_FUNCTIONS) == 5
    assert all(s.startswith("DROP FUNCTION IF EXISTS ") for s in statements)
    created = "\n".join([*audit_ddl(), *job_ddl(), *household_ddl("perfin_app")])
    for signature in PLATFORM_FUNCTIONS:
        assert f"FUNCTION {signature.split('(')[0]}(" in created, signature


def test_household_policies_are_own_context_only() -> None:
    """SR-1: no household policy lets one household change or delete another."""
    policies = {policy.command: policy for policy in CUSTOM_RLS["household"].policies}
    assert set(policies) == {"SELECT", "INSERT", "UPDATE", "DELETE"}
    assert CUSTOM_RLS["household"].force
    own = f"id = {CTX_HOUSEHOLD}"
    assert policies["UPDATE"].using == own
    assert policies["UPDATE"].with_check == own
    assert policies["DELETE"].using == own


def test_job_policies_and_immutable_columns() -> None:
    """SR-4: job rows follow the household context; identity is immutable."""
    spec = CUSTOM_RLS["job"]
    assert spec.force
    for policy in spec.policies:
        for predicate in (policy.using, policy.with_check):
            if predicate is not None and policy.command != "INSERT":
                assert f"household_id = {CTX_HOUSEHOLD}" in predicate
                assert CTX_JOB_QUEUE in predicate
    joined = "\n".join(job_ddl())
    assert 'BEFORE UPDATE ON "job"' in joined
    for column in JOB_IMMUTABLE_COLUMNS:
        assert f"NEW.{column} IS DISTINCT FROM OLD.{column}" in joined
    assert {"household_id", "type", "payload"} <= set(JOB_IMMUTABLE_COLUMNS)


def test_audit_append_requires_explicit_context_for_instance_rows() -> None:
    """SR-5: an instance-level audit row needs instance-admin or system-actor."""
    (append,) = (p for p in CUSTOM_RLS["audit_log"].policies if p.command == "INSERT")
    assert append.with_check is not None
    assert "household_id IS NULL OR" not in append.with_check
    assert CTX_SYSTEM_ACTOR in append.with_check
    assert CTX_INSTANCE_ADMIN in append.with_check


def test_grants_ddl_defaults_to_base_metadata() -> None:
    import_all_models()
    statements = grants_ddl("perfin_app")
    assert 'GRANT SELECT, INSERT, UPDATE, DELETE ON "job" TO "perfin_app"' in statements
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        grants_ddl("perfin_app; DROP")


@pytest.mark.parametrize(
    "policy",
    [
        Policy("p", "SELECT"),
        Policy("p", "INSERT", using="true"),
        Policy("p", "SELECT", with_check="true"),
        Policy("p", "DELETE", with_check="true"),
        Policy("Bad Name", "ALL", using="true"),
    ],
)
def test_invalid_policies_rejected(policy: Policy) -> None:
    with pytest.raises(ValueError, match=r"predicate|only|unsafe"):
        policy.ddl("thing")


def test_household_policy_uses_with_check() -> None:
    statement = Policy("p", "ALL", using="a", with_check="b").ddl("thing")[1]
    assert (
        statement == 'CREATE POLICY "p" ON "thing" AS PERMISSIVE FOR ALL USING (a) WITH CHECK (b)'
    )
    assert CTX_HOUSEHOLD == "nullif(current_setting('perfin.household_id', true), '')::uuid"


def test_set_context_rejects_non_uuid() -> None:
    session = cast(Session, object())
    with pytest.raises(TypeError):
        set_context(session, "00000000-0000-0000-0000-000000000000")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        set_context(session, uuid.uuid4(), 42)  # type: ignore[arg-type]


_PG = make_engine("postgresql+psycopg://u:p@localhost/db").dialect


def _create_sql(model: type[Base]) -> str:
    table = cast(Table, model.__table__)
    parts = [str(CreateTable(table).compile(dialect=_PG))]
    for index in sorted(table.indexes, key=lambda i: str(i.name)):
        assert isinstance(index, Index)
        parts.append(str(CreateIndex(index).compile(dialect=_PG)))
    return "\n".join(parts)


def test_job_table_shape() -> None:
    ddl = _create_sql(Job)
    for column in (
        "queue",
        "type",
        "payload JSONB",
        "state job_state",
        "priority",
        "run_after",
        "attempts",
        "max_attempts",
        "locked_by",
        "locked_at",
        "lease_expires_at",
        "last_error",
        "household_id",
        "created_at",
        "updated_at",
    ):
        assert column in ddl, column
    assert (
        "CREATE INDEX ix_job_dequeue ON job (queue, priority, run_after, id) WHERE state = 'queued'"
        in ddl
    )
    assert "ix_job_lease_expiry ON job (lease_expires_at) WHERE state = 'running'" in ddl
    assert [s.value for s in JobState] == ["queued", "running", "succeeded", "failed", "dead"]


def test_event_is_household_scoped() -> None:
    assert "event" in household_scoped_tables()
    assert not Event.__table__.c.household_id.nullable


def test_audit_log_has_no_foreign_keys_or_updated_at() -> None:
    table = AuditLog.__table__
    assert not table.foreign_keys
    assert "updated_at" not in table.c
    assert table.c.household_id.nullable
    assert table.c.user_id.nullable
    ddl = _create_sql(AuditLog)
    assert "ip INET" in ddl
    assert "seq BIGINT NOT NULL" in ddl


def test_setting_unique_nulls_not_distinct() -> None:
    ddl = _create_sql(Setting)
    assert "UNIQUE NULLS NOT DISTINCT (scope, scope_id, key)" in ddl
    assert "CHECK ((scope = 'system') = (scope_id IS NULL))" in ddl


def test_connection_options() -> None:
    assert connection_options() == ""
    assert connection_options(statement_timeout_ms=0, lock_timeout_ms=None) == ""
    assert (
        connection_options(statement_timeout_ms=30_000, lock_timeout_ms=10_000)
        == "-c statement_timeout=30000 -c lock_timeout=10000"
    )
    with pytest.raises(ValueError, match="lock_timeout"):
        connection_options(lock_timeout_ms=-1)


class _CapturedError(Exception):
    pass


def _connect_params(engine: Engine) -> dict[str, Any]:
    """The keyword arguments the engine would pass to psycopg.connect."""
    captured: dict[str, Any] = {}

    def grab(dialect: Any, conn_rec: Any, cargs: Any, cparams: dict[str, Any]) -> None:
        captured.update(cparams)
        raise _CapturedError

    event.listen(engine, "do_connect", grab)
    try:
        with pytest.raises(_CapturedError), engine.connect():
            pass
    finally:
        event.remove(engine, "do_connect", grab)
        engine.dispose()
    return captured


def test_engine_carries_timeouts_and_pre_ping() -> None:
    engine = make_engine(
        "postgresql+psycopg://u:p@localhost/db",
        statement_timeout_ms=5_000,
        lock_timeout_ms=1_000,
        connect_args={"options": "-c application_name=perfin"},
    )
    assert engine.pool._pre_ping
    assert engine.hide_parameters
    params = _connect_params(engine)
    assert params["options"] == (
        "-c application_name=perfin -c statement_timeout=5000 -c lock_timeout=1000"
    )


def test_app_engine_uses_settings_timeouts() -> None:
    settings = Settings(dev_mode=True, db_statement_timeout_ms=7_000, db_lock_timeout_ms=2_000)
    params = _connect_params(make_app_engine(settings))
    assert params["options"] == "-c statement_timeout=7000 -c lock_timeout=2000"
    assert Settings(dev_mode=True).db_lock_timeout_ms == 10_000


def test_db_app_role_setting_validated() -> None:
    assert Settings(dev_mode=True).db_app_role == "perfin_app"
    with pytest.raises(ValueError, match="db_app_role"):
        Settings(dev_mode=True, db_app_role='perfin_app"; DROP')

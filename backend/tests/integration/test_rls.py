# SPDX-License-Identifier: AGPL-3.0-or-later
"""Row-level security on the real platform tables, as the application role.

Covers SECURITY.md F2/F3: no context -> zero rows, wrong context -> zero
rows, writes into another household rejected by WITH CHECK; plus the
scope-aware ``setting`` policies and the role/grant layout (H3, I4).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

import pytest
from sqlalchemy import CursorResult, Engine, delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import Settings
from app.db.base import household_scoped_tables
from app.db.models import Event, Household, Job, JobState, Setting, SettingScope
from app.db.registry import import_all_models
from app.db.rls import CUSTOM_RLS, POLICY_NAME, get_context, queue_scope, set_context
from app.db.session import make_sessionmaker
from tests.conftest import TestDatabase

pytestmark = pytest.mark.integration

RLS_DENIED = "row-level security"


@dataclass(frozen=True)
class Households:
    a: uuid.UUID
    b: uuid.UUID


@pytest.fixture
def factory(app_engine: Engine) -> sessionmaker[Session]:
    return make_sessionmaker(app_engine)


@pytest.fixture
def households(factory: sessionmaker[Session], admin_engine: Engine) -> Iterator[Households]:
    """Two committed households (synthetic), removed with their rows afterwards."""
    with factory() as session, session.begin():
        # ``household`` is under RLS: an instance admin creates households.
        set_context(session, None, instance_admin=True)
        a = Household(name="Synthetic A")
        b = Household(name="Synthetic B")
        session.add_all([a, b])
    try:
        yield Households(a.id, b.id)
    finally:
        with admin_engine.begin() as conn:
            conn.execute(text("DELETE FROM household WHERE id IN (:a, :b)"), {"a": a.id, "b": b.id})


def _add_event(session: Session, household_id: uuid.UUID, kind: str = "test.synthetic") -> None:
    session.execute(insert(Event).values(household_id=household_id, type=kind))


def _event_count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(Event)).scalar_one()


# ---------------------------------------------------------------------------
# DDL applied to every table
# ---------------------------------------------------------------------------


def test_every_household_scoped_table_has_forced_rls(admin_engine: Engine) -> None:
    import_all_models()
    tables = household_scoped_tables()
    assert "event" in tables
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                "EXISTS (SELECT 1 FROM pg_policies p WHERE p.tablename = c.relname "
                "AND p.policyname = :policy) AS has_policy "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = ANY(:tables)"
            ),
            {"tables": tables, "policy": POLICY_NAME},
        ).all()
    assert {row.relname for row in rows} == set(tables)
    for row in rows:
        assert row.relrowsecurity, row.relname
        assert row.relforcerowsecurity, row.relname
        assert row.has_policy, row.relname


def test_custom_rls_tables_enabled(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        for table, spec in CUSTOM_RLS.items():
            row = conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = :t AND relnamespace = 'public'::regnamespace"
                ),
                {"t": table},
            ).one()
            assert row.relrowsecurity, table
            assert row.relforcerowsecurity is spec.force, table
            policies = set(
                conn.execute(
                    text("SELECT policyname FROM pg_policies WHERE tablename = :t"), {"t": table}
                ).scalars()
            )
            assert policies == {policy.name for policy in spec.policies}


def test_app_role_privileges_are_least(admin_engine: Engine, test_database: TestDatabase) -> None:
    role = test_database.app_role
    with admin_engine.connect() as conn:

        def has(table: str, privilege: str) -> bool:
            return bool(
                conn.execute(
                    text("SELECT has_table_privilege(:r, :t, :p)"),
                    {"r": role, "t": f"public.{table}", "p": privilege},
                ).scalar_one()
            )

        for table in ("household", "event", "job", "setting"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert has(table, privilege), (table, privilege)
        assert has("audit_log", "SELECT")
        assert has("audit_log", "INSERT")
        for table in ("household", "event", "job", "setting", "audit_log"):
            for privilege in ("TRUNCATE", "REFERENCES", "TRIGGER"):
                assert not has(table, privilege), (table, privilege)
        assert not has("audit_log", "UPDATE")
        assert not has("audit_log", "DELETE")
        owners = set(
            conn.execute(
                text("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
    assert owners == {test_database.owner_role}


def test_owner_role_is_not_superuser(owner_engine: Engine, test_database: TestDatabase) -> None:
    with owner_engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
        schema_owner = conn.execute(
            text("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'public'")
        ).scalar_one()
    assert not row.rolsuper
    assert not row.rolbypassrls
    assert schema_owner == test_database.owner_role


# ---------------------------------------------------------------------------
# Household isolation on a real HouseholdScoped table
# ---------------------------------------------------------------------------


def test_no_context_sees_zero_rows(factory: sessionmaker[Session], households: Households) -> None:
    with factory() as session, session.begin():
        set_context(session, households.a)
        _add_event(session, households.a)
    with factory() as session, session.begin():
        assert _event_count(session) == 0
        set_context(session, None)
        assert _event_count(session) == 0


def test_wrong_context_sees_zero_rows(
    factory: sessionmaker[Session], households: Households
) -> None:
    with factory() as session, session.begin():
        set_context(session, households.a)
        _add_event(session, households.a)
        _add_event(session, households.a)
    with factory() as session, session.begin():
        set_context(session, households.b)
        assert _event_count(session) == 0
        # Explicitly asking for the other household's rows changes nothing.
        rows = session.execute(select(Event.id).where(Event.household_id == households.a)).all()
        assert rows == []
        deleted = session.execute(text("DELETE FROM event RETURNING id")).all()
        assert deleted == []
    with factory() as session, session.begin():
        set_context(session, households.a)
        assert _event_count(session) == 2
    with factory() as session, session.begin():
        set_context(session, uuid.uuid4())  # a household that does not exist
        assert _event_count(session) == 0


def test_insert_into_other_household_rejected(
    factory: sessionmaker[Session], households: Households
) -> None:
    with factory() as session, session.begin():
        set_context(session, households.a)
        with pytest.raises(DBAPIError, match=RLS_DENIED):
            _add_event(session, households.b)


def test_insert_without_context_rejected(
    factory: sessionmaker[Session], households: Households
) -> None:
    with factory() as session, session.begin(), pytest.raises(DBAPIError, match=RLS_DENIED):
        _add_event(session, households.a)


def test_update_cannot_move_row_to_other_household(
    factory: sessionmaker[Session], households: Households
) -> None:
    with factory() as session, session.begin():
        set_context(session, households.a)
        _add_event(session, households.a)
    with factory() as session, session.begin():
        set_context(session, households.a)
        with pytest.raises(DBAPIError, match=RLS_DENIED):
            session.execute(update(Event).values(household_id=households.b))


def test_context_is_transaction_local(factory: sessionmaker[Session]) -> None:
    household, user = uuid.uuid4(), uuid.uuid4()
    with factory() as session:
        with session.begin():
            set_context(session, household, user, instance_admin=True)
            ctx = get_context(session)
            assert ctx.household_id == household
            assert ctx.user_id == user
            assert ctx.instance_admin
        with session.begin():
            ctx = get_context(session)
            assert ctx.household_id is None
            assert ctx.user_id is None
            assert not ctx.instance_admin


def test_context_values_are_bound_not_interpolated(factory: sessionmaker[Session]) -> None:
    with factory() as session, session.begin():
        with pytest.raises(TypeError):
            set_context(session, "x' OR true --")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            set_context(session, None, "nope")  # type: ignore[arg-type]


def test_hostile_context_value_matches_nothing(
    factory: sessionmaker[Session], households: Households
) -> None:
    with factory() as session, session.begin():
        set_context(session, households.a)
        _add_event(session, households.a)
    with factory() as session, session.begin():
        session.execute(
            text("SELECT set_config('perfin.household_id', :v, true)"),
            {"v": "00000000-0000-0000-0000-000000000000' OR '1'='1"},
        )
        with pytest.raises(DBAPIError, match="invalid input syntax for type uuid"):
            _event_count(session)


# ---------------------------------------------------------------------------
# setting: scope-aware policies
# ---------------------------------------------------------------------------


def _put_setting(
    session: Session, scope: SettingScope, scope_id: uuid.UUID | None, key: str, value: object
) -> None:
    session.execute(insert(Setting).values(scope=scope, scope_id=scope_id, key=key, value=value))


def _setting_keys(session: Session, prefix: str) -> set[str]:
    return set(
        session.execute(
            select(Setting.key).where(Setting.key.startswith(prefix, autoescape=True))
        ).scalars()
    )


@pytest.fixture
def setting_prefix(admin_engine: Engine) -> Iterator[str]:
    prefix = f"zz_test_{uuid.uuid4().hex[:8]}."
    yield prefix
    with admin_engine.begin() as conn:
        conn.execute(text("DELETE FROM setting WHERE key LIKE :p"), {"p": prefix + "%"})


def test_system_settings_readable_by_all_writable_by_instance_admin(
    factory: sessionmaker[Session], setting_prefix: str
) -> None:
    key = setting_prefix + "oidc_enabled"
    with factory() as session, session.begin(), pytest.raises(DBAPIError, match=RLS_DENIED):
        _put_setting(session, SettingScope.SYSTEM, None, key, True)
    with factory() as session, session.begin():
        set_context(session, None, uuid.uuid4(), instance_admin=True)
        _put_setting(session, SettingScope.SYSTEM, None, key, True)
    with factory() as session, session.begin():
        assert _setting_keys(session, setting_prefix) == {key}
        # Readable without context, but a non-admin cannot change or delete it.
        changed = session.execute(update(Setting).where(Setting.key == key).values(value=False))
        assert cast(CursorResult[Any], changed).rowcount == 0
        removed = session.execute(text("DELETE FROM setting WHERE key = :k"), {"k": key})
        assert cast(CursorResult[Any], removed).rowcount == 0
    with factory() as session, session.begin():
        set_context(session, None, None, instance_admin=True)
        removed = session.execute(text("DELETE FROM setting WHERE key = :k"), {"k": key})
        assert cast(CursorResult[Any], removed).rowcount == 1


def test_system_key_is_unique(factory: sessionmaker[Session], setting_prefix: str) -> None:
    key = setting_prefix + "dup"
    with factory() as session, session.begin():
        set_context(session, None, None, instance_admin=True)
        _put_setting(session, SettingScope.SYSTEM, None, key, 1)
        with pytest.raises(IntegrityError):
            _put_setting(session, SettingScope.SYSTEM, None, key, 2)


def test_scope_id_must_match_scope(factory: sessionmaker[Session], setting_prefix: str) -> None:
    with factory() as session, session.begin():
        set_context(session, None, None, instance_admin=True)
        with pytest.raises(IntegrityError, match="scope_id_matches_scope"):
            _put_setting(session, SettingScope.SYSTEM, uuid.uuid4(), setting_prefix + "x", 1)


def test_household_and_user_settings_isolated(
    factory: sessionmaker[Session], households: Households, setting_prefix: str
) -> None:
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    with factory() as session, session.begin():
        set_context(session, households.a, user_a)
        _put_setting(session, SettingScope.HOUSEHOLD, households.a, setting_prefix + "a", "x")
        _put_setting(session, SettingScope.USER, user_a, setting_prefix + "ua", "y")
        with pytest.raises(DBAPIError, match=RLS_DENIED), session.begin_nested():
            _put_setting(session, SettingScope.HOUSEHOLD, households.b, setting_prefix + "b", 1)
    with factory() as session, session.begin():
        set_context(session, households.a, user_a)
        with pytest.raises(DBAPIError, match=RLS_DENIED):
            _put_setting(session, SettingScope.USER, user_b, setting_prefix + "ub", 1)
    with factory() as session, session.begin():
        set_context(session, households.a, user_a)
        assert _setting_keys(session, setting_prefix) == {
            setting_prefix + "a",
            setting_prefix + "ua",
        }
    with factory() as session, session.begin():
        set_context(session, households.b, user_b)
        assert _setting_keys(session, setting_prefix) == set()
    with factory() as session, session.begin():
        set_context(session, households.b, user_a)
        assert _setting_keys(session, setting_prefix) == {setting_prefix + "ua"}


# ---------------------------------------------------------------------------
# job: no RLS by design (worker dequeues across households)
# ---------------------------------------------------------------------------


def test_household_row_only_reachable_in_its_own_context(
    factory: sessionmaker[Session], households: Households
) -> None:
    """Review finding SR-1: ``household`` is the parent of every scoped row.

    Its ``ON DELETE CASCADE`` children are removed by referential actions,
    which ignore RLS, so the parent row must not be deletable (or writable)
    from another household's context.
    """
    with factory() as session, session.begin():
        set_context(session, households.a)
        _add_event(session, households.a)
    for context in (households.b, None):
        with factory() as session, session.begin():
            set_context(session, context)
            assert session.execute(select(Household.id)).scalars().all() == (
                [households.b] if context else []
            )
            for statement in (
                update(Household).where(Household.id == households.a).values(name="pwned"),
                delete(Household).where(Household.id == households.a),
            ):
                result = cast(CursorResult[Any], session.execute(statement))
                assert result.rowcount == 0, (statement, context)
    with factory() as session, session.begin():
        set_context(session, households.a)
        assert _event_count(session) == 1
        assert session.execute(select(Household.name)).scalar_one() == "Synthetic A"
        # Moving the row to another id is refused too.
        with pytest.raises(DBAPIError, match=RLS_DENIED), session.begin_nested():
            session.execute(
                update(Household).where(Household.id == households.a).values(id=households.b)
            )


def test_household_creation_needs_own_context_or_instance_admin(
    factory: sessionmaker[Session], households: Households
) -> None:
    new_id = uuid.uuid4()
    with factory() as session, session.begin():
        set_context(session, households.a)
        with pytest.raises(DBAPIError, match=RLS_DENIED), session.begin_nested():
            session.execute(insert(Household).values(id=new_id, name="Forged"))
        set_context(session, new_id)
        session.execute(insert(Household).values(id=new_id, name="Synthetic C"))
        set_context(session, None, instance_admin=True)
        listed = set(session.execute(select(Household.id)).scalars())
        assert {households.a, households.b, new_id} <= listed
        # The instance admin may list households but not change them.
        result = cast(
            CursorResult[Any],
            session.execute(delete(Household).where(Household.id == households.a)),
        )
        assert result.rowcount == 0
        session.rollback()


def test_job_table_contract(factory: sessionmaker[Session], households: Households) -> None:
    with factory() as session, session.begin():
        set_context(session, households.a)
        job = Job(type="zz_test.noop", payload={"n": 1}, household_id=households.a)
        session.add(job)
        session.flush()
        assert job.state is JobState.QUEUED
        assert job.attempts == 0
        assert job.priority == 0
        assert job.max_attempts == 5
        # Without a context the job is invisible; the queue machinery sees it
        # inside queue_scope (it has no household context).
        set_context(session, None)
        assert session.execute(select(Job.id).where(Job.id == job.id)).scalar() is None
        with queue_scope(session):
            found = session.execute(select(Job.id).where(Job.id == job.id)).scalar_one()
        assert found == job.id
        set_context(session, households.a)
        with pytest.raises(IntegrityError, match="running_has_lease"), session.begin_nested():
            session.execute(update(Job).where(Job.id == job.id).values(state="running"))
        session.delete(job)


def test_lock_timeout_applied(app_engine: Engine, settings: Settings) -> None:
    with app_engine.connect() as conn:
        statement = conn.execute(text("SELECT current_setting('statement_timeout')")).scalar_one()
        lock = conn.execute(text("SELECT current_setting('lock_timeout')")).scalar_one()
    assert statement == f"{settings.db_statement_timeout_ms // 1000}s"
    assert lock == f"{settings.db_lock_timeout_ms // 1000}s"

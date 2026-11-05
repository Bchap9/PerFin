# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RLS DDL from app.db.rls really isolates households for the app role.

Uses a throw-away probe table created with raw DDL so Base.metadata (and
therefore migration drift checks) is untouched.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from app.db.rls import quote_ident, rls_ddl, set_context
from app.db.session import make_sessionmaker
from tests.conftest import TestDatabase

pytestmark = pytest.mark.integration

PROBE = "zz_rls_probe"  # literal in the SQL below as well


@pytest.fixture
def probe_table(admin_engine: Engine, test_database: TestDatabase) -> Iterator[str]:
    role = quote_ident(test_database.app_role)
    with admin_engine.begin() as conn:
        conn.exec_driver_sql(
            f"CREATE TABLE {PROBE} (id uuid PRIMARY KEY, household_id uuid NOT NULL, note text)"
        )
        for statement in rls_ddl(PROBE):
            conn.exec_driver_sql(statement)
        conn.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {PROBE} TO {role}")
    try:
        yield PROBE
    finally:
        with admin_engine.begin() as conn:
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS {PROBE}")


def test_rls_enabled_and_forced(admin_engine: Engine, probe_table: str) -> None:
    with admin_engine.connect() as conn:
        row = conn.execute(
            text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"),
            {"t": probe_table},
        ).one()
    assert row.relrowsecurity
    assert row.relforcerowsecurity


def test_household_isolation(app_engine: Engine, probe_table: str) -> None:
    factory = make_sessionmaker(app_engine)
    house_a, house_b = uuid.uuid4(), uuid.uuid4()
    insert = text("INSERT INTO zz_rls_probe (id, household_id, note) VALUES (:id, :h, :n)")
    count = text("SELECT count(*) FROM zz_rls_probe")

    for house in (house_a, house_b):
        with factory() as session, session.begin():
            set_context(session, house)
            session.execute(insert, {"id": uuid.uuid4(), "h": house, "n": "synthetic"})

    with factory() as session, session.begin():
        set_context(session, house_a)
        assert session.execute(count).scalar_one() == 1

    with factory() as session, session.begin():
        # No context: the policy matches nothing.
        assert session.execute(count).scalar_one() == 0

    with factory() as session, session.begin():
        set_context(session, house_a)
        updated = session.execute(text("UPDATE zz_rls_probe SET note = 'x' RETURNING id")).all()
        assert len(updated) == 1


def test_context_does_not_leak_between_transactions(app_engine: Engine, probe_table: str) -> None:
    factory = make_sessionmaker(app_engine)
    with factory() as session:
        with session.begin():
            set_context(session, uuid.uuid4())
        with session.begin():
            value = session.execute(
                text("SELECT current_setting('perfin.household_id', true)")
            ).scalar_one()
    assert value in (None, "")


def test_write_into_other_household_rejected(app_engine: Engine, probe_table: str) -> None:
    factory = make_sessionmaker(app_engine)
    statement = text("INSERT INTO zz_rls_probe (id, household_id) VALUES (:id, :h)")
    with factory() as session, session.begin():
        set_context(session, uuid.uuid4())
        with pytest.raises(DBAPIError, match="row-level security"):
            session.execute(statement, {"id": uuid.uuid4(), "h": uuid.uuid4()})

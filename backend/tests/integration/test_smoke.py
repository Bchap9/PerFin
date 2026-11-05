# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 0 smoke: the app boots and the database is reachable as the app role."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.core.ids import uuid7
from app.core.settings import Settings
from app.db.models import Household
from app.db.rls import set_context
from tests.conftest import TestDatabase

pytestmark = pytest.mark.integration


def test_app_boots(client: TestClient) -> None:
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "PerFin"
    assert client.get("/api/v1/definitely-not-a-route").status_code == 404


def test_app_role_is_unprivileged(app_engine: Engine, test_database: TestDatabase) -> None:
    with app_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_user, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                "FROM pg_roles WHERE rolname = current_user"
            )
        ).one()
    assert row.current_user == test_database.app_role
    assert not row.rolsuper
    assert not row.rolbypassrls
    assert not row.rolcreaterole
    assert not row.rolcreatedb


def test_app_role_does_not_own_tables(admin_engine: Engine, test_database: TestDatabase) -> None:
    with admin_engine.connect() as conn:
        owned = conn.execute(
            text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner = :r"),
            {"r": test_database.app_role},
        ).scalar_one()
    assert owned == 0


def test_db_session_round_trip(db: Session) -> None:
    household = Household(id=uuid7(), name="Synthetic Household")
    # ``household`` is under RLS: a new household is written in its own context.
    set_context(db, household.id)
    db.add(household)
    db.flush()
    loaded = db.scalars(select(Household).where(Household.id == household.id)).one()
    assert loaded.base_currency == "USD"
    assert loaded.id.version == 7
    assert loaded.created_at.tzinfo is not None


def test_set_context_is_transaction_local(db: Session) -> None:
    household_id = uuid.uuid4()
    user_id = uuid.uuid4()
    set_context(db, household_id, user_id)
    values = db.execute(
        text(
            "SELECT current_setting('perfin.household_id', true) AS h, "
            "current_setting('perfin.user_id', true) AS u"
        )
    ).one()
    assert values.h == str(household_id)
    assert values.u == str(user_id)


def test_db_fixture_rolls_back(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        # An instance admin sees every household, so a leftover would show.
        set_context(conn, None, instance_admin=True)
        count = conn.execute(
            text("SELECT count(*) FROM household WHERE name = 'Synthetic Household'")
        ).scalar_one()
    assert count == 0


def test_statement_timeout_applied(app_engine: Engine, settings: Settings) -> None:
    with app_engine.connect() as conn:
        value = conn.execute(text("SELECT current_setting('statement_timeout')")).scalar_one()
    assert value == f"{settings.db_statement_timeout_ms // 1000}s"


def test_app_cannot_create_objects_in_public_schema(app_engine: Engine) -> None:
    with app_engine.connect() as conn, pytest.raises(ProgrammingError):
        conn.execute(text("CREATE TABLE should_not_exist (id int)"))

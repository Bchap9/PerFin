# SPDX-License-Identifier: AGPL-3.0-or-later
"""deploy/db-init/10-perfin-roles.sh creates the owner and app roles (SECURITY.md H3).

Runs the real script with ``psql`` against a scratch database on the test
server, with unique role names, and checks the result as the superuser.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import URL, make_url

from tests.conftest import DEFAULT_ADMIN_URL

pytestmark = pytest.mark.integration

SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "db-init" / "10-perfin-roles.sh"


@dataclass(frozen=True)
class Scratch:
    admin: URL
    database: str
    owner_role: str
    app_role: str
    owner_password: str
    app_password: str


def _dsn(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def scratch() -> Iterator[Scratch]:
    admin = make_url(os.environ.get("PERFIN_TEST_DATABASE_URL", DEFAULT_ADMIN_URL))
    suffix = uuid.uuid4().hex[:10]
    env = Scratch(
        admin=admin,
        database=f"perfin_dbinit_{suffix}",
        owner_role=f"zz_owner_{suffix}",
        app_role=f"zz_app_{suffix}",
        owner_password=secrets.token_urlsafe(24),
        app_password=secrets.token_urlsafe(24),
    )
    with psycopg.connect(_dsn(admin), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(env.database)))
    try:
        yield env
    finally:
        with psycopg.connect(_dsn(admin), autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(env.database)
                )
            )
            for role in (env.owner_role, env.app_role):
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _run(env: Scratch, **overrides: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    assert shutil.which("psql") is not None, "psql (PostgreSQL client) is required"
    variables = {
        "PATH": os.environ.get("PATH", ""),
        "PGHOST": env.admin.host or "localhost",
        "PGPORT": str(env.admin.port or 5432),
        "PGPASSWORD": env.admin.password or "",
        "POSTGRES_USER": env.admin.username or "postgres",
        "POSTGRES_PASSWORD": env.admin.password or "",
        "POSTGRES_DB": env.database,
        "PERFIN_DB_OWNER_ROLE": env.owner_role,
        "PERFIN_DB_APP_ROLE": env.app_role,
        "POSTGRES_OWNER_PASSWORD": env.owner_password,
        "POSTGRES_APP_PASSWORD": env.app_password,
    }
    variables.update(overrides)
    return subprocess.run(  # fixed argv, no shell
        [bash, str(SCRIPT)], env=variables, capture_output=True, text=True, timeout=60, check=False
    )


def _connect_as(env: Scratch, role: str, password: str) -> psycopg.Connection[tuple[object, ...]]:
    url = env.admin.set(database=env.database, username=role, password=password)
    return psycopg.connect(_dsn(url), autocommit=True)


def test_script_is_executable_and_has_header() -> None:
    assert os.access(SCRIPT, os.X_OK)
    head = SCRIPT.read_text(encoding="utf-8").splitlines()[:3]
    assert head[0] == "#!/usr/bin/env bash"
    assert any("SPDX-License-Identifier: AGPL-3.0-or-later" in line for line in head)


def test_creates_least_privilege_roles(scratch: Scratch) -> None:
    result = _run(scratch)
    assert result.returncode == 0, result.stderr
    assert scratch.owner_password not in result.stdout + result.stderr
    assert scratch.app_password not in result.stdout + result.stderr

    admin = scratch.admin.set(database=scratch.database)
    with psycopg.connect(_dsn(admin), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, "
            "rolreplication, rolpassword LIKE 'SCRAM-SHA-256$%%' AS scram, "
            "coalesce((SELECT array_to_string(s.setconfig, ',') FROM pg_db_role_setting s "
            "WHERE s.setrole = a.oid AND s.setdatabase = 0), '') AS config "
            "FROM pg_authid a WHERE rolname = ANY(%s)",
            ([scratch.owner_role, scratch.app_role],),
        ).fetchall()
        assert len(rows) == 2
        for name, login, superuser, bypass, createdb, createrole, repl, scram, config in rows:
            assert login, name
            for flag in (superuser, bypass, createdb, createrole, repl):
                assert not flag, name
            assert scram, name
            if name == scratch.app_role:
                assert "statement_timeout=30s" in str(config)
                assert "lock_timeout=10s" in str(config)

        schema = conn.execute(
            "SELECT nspowner::regrole::text, "
            "EXISTS (SELECT 1 FROM aclexplode(nspacl) a WHERE a.grantee = 0) "
            "FROM pg_namespace WHERE nspname = 'public'"
        ).fetchone()
        assert schema == (scratch.owner_role, False)  # PUBLIC has no USAGE/CREATE
        public_db = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_database d, aclexplode(d.datacl) a "
            "WHERE d.datname = %s AND a.grantee = 0)",
            (scratch.database,),
        ).fetchone()
        assert public_db == (False,)  # PUBLIC may not connect or create temp tables

    with _connect_as(scratch, scratch.owner_role, scratch.owner_password) as owner:
        owner.execute("CREATE TABLE zz_probe (id int)")
        owner.execute(
            sql.SQL("GRANT SELECT ON zz_probe TO {}").format(sql.Identifier(scratch.app_role))
        )
    with _connect_as(scratch, scratch.app_role, scratch.app_password) as app:
        assert app.execute("SELECT count(*) FROM zz_probe").fetchone() == (0,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("CREATE TABLE zz_nope (id int)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("CREATE TEMP TABLE zz_nope (id int)")


def test_rerun_is_idempotent(scratch: Scratch) -> None:
    assert _run(scratch).returncode == 0
    rotated = secrets.token_urlsafe(24)
    result = _run(scratch, POSTGRES_APP_PASSWORD=rotated)
    assert result.returncode == 0, result.stderr
    with _connect_as(scratch, scratch.app_role, rotated) as app:
        assert app.execute("SELECT current_user").fetchone() == (scratch.app_role,)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"POSTGRES_APP_PASSWORD": ""}, "POSTGRES_APP_PASSWORD must be set"),
        ({"POSTGRES_OWNER_PASSWORD": "short"}, "at least 16 characters"),
        ({"POSTGRES_APP_PASSWORD": "CHANGE_ME_CHANGE_ME"}, "placeholder"),
        ({"POSTGRES_APP_PASSWORD": "same-password-0123456789"}, "must differ"),
        ({"PERFIN_DB_APP_ROLE": 'x"; DROP ROLE postgres; --'}, "invalid role name"),
        ({"PERFIN_DB_APP_ROLE": "postgres"}, "must differ from the bootstrap superuser"),
    ],
)
def test_insecure_input_refused(scratch: Scratch, overrides: dict[str, str], message: str) -> None:
    if overrides.get("POSTGRES_APP_PASSWORD") == "same-password-0123456789":
        overrides = {**overrides, "POSTGRES_OWNER_PASSWORD": "same-password-0123456789"}
    result = _run(scratch, **overrides)
    assert result.returncode != 0
    assert message in result.stderr
    admin = scratch.admin.set(database=scratch.database)
    with psycopg.connect(_dsn(admin), autocommit=True) as conn:
        count = conn.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)",
            ([scratch.owner_role, scratch.app_role],),
        ).fetchone()
    assert count == (0,)

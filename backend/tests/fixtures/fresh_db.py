# SPDX-License-Identifier: AGPL-3.0-or-later
"""A separate, empty test database for tests that need "no user exists".

The session database in ``tests/conftest.py`` fills up with users from every
test, so first-run setup (``app.auth.bootstrap``) is exercised against its
own database, built exactly like the shared one (owner role owns ``public``,
schema from ``Base.metadata`` plus ``app.db.rls.schema_ddl``, the app
connects as a non-owner role so RLS applies). :func:`reset_identity` empties
it between tests with the superuser connection (``audit_log`` is
append-only and is left alone).
"""

from __future__ import annotations

import base64
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy import URL, Engine, make_url, pool, text

from app.core.settings import Settings
from app.db.base import Base
from app.db.registry import import_all_models
from app.db.rls import schema_ddl
from app.db.session import make_engine


@dataclass(frozen=True)
class FreshDatabase:
    settings: Settings
    admin_url: URL


def _dsn(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@contextmanager
def fresh_database(base_admin_url: str) -> Iterator[FreshDatabase]:
    base = make_url(base_admin_url)
    suffix = uuid.uuid4().hex[:12]
    name = f"perfin_fresh_{suffix}"
    app_role, owner_role = f"perfin_fapp_{suffix}", f"perfin_fown_{suffix}"
    app_pw, owner_pw = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    with psycopg.connect(_dsn(base), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        for role, password in ((app_role, app_pw), (owner_role, owner_pw)):
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                sql.Identifier(name), sql.Identifier(app_role), sql.Identifier(owner_role)
            )
        )
    admin_url = base.set(database=name)
    try:
        import_all_models()
        with psycopg.connect(_dsn(admin_url), autocommit=True) as conn:
            conn.execute(
                sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(owner_role))
            )
            conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        owner = make_engine(
            base.set(database=name, username=owner_role, password=owner_pw).render_as_string(
                hide_password=False
            ),
            poolclass=pool.NullPool,
        )
        try:
            with owner.begin() as conn:
                Base.metadata.create_all(conn)
                for statement in schema_ddl(Base.metadata, app_role):
                    conn.exec_driver_sql(statement)
        finally:
            owner.dispose()
        app_url = base.set(database=name, username=app_role, password=app_pw)
        settings = Settings(
            dev_mode=True,
            database_url=SecretStr(app_url.render_as_string(hide_password=False)),
            master_key=SecretStr(base64.b64encode(secrets.token_bytes(32)).decode("ascii")),
            secret_key=SecretStr(secrets.token_urlsafe(48)),
            public_url="http://testserver",
        )
        yield FreshDatabase(settings=settings, admin_url=admin_url)
    finally:
        with psycopg.connect(_dsn(base), autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
            for role in (app_role, owner_role):
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def reset_identity(admin: Engine) -> None:
    """Remove every user, household, setting and limiter row (superuser)."""
    with admin.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE app_user, household, setting, rate_limit_event, user_session "
                "RESTART IDENTITY CASCADE"
            )
        )

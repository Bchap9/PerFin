# SPDX-License-Identifier: AGPL-3.0-or-later
"""Throw-away PerFin databases for tests that act on a whole instance.

Key rotation and backup/restore touch every row of every household, so they
cannot share the per-session test database (other tests in the same worker
keep using its master key). :func:`scratch_instance` creates an owner and an
application role, one database with the full schema (``create_all`` plus
``app.db.rls.schema_ddl``, stamped at the Alembic head, as
``tests/conftest.py`` builds it) and optionally further *empty* databases
for the same roles (restore targets), and drops everything afterwards.
"""

from __future__ import annotations

import base64
import os
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from alembic import command
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy import Engine, pool
from sqlalchemy.engine import URL, make_url

from app.cli import alembic_config
from app.core.settings import Settings
from app.db.base import Base
from app.db.registry import import_all_models
from app.db.rls import schema_ddl
from app.db.session import make_engine

DEFAULT_ADMIN_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"


def libpq(url: URL) -> str:
    """``postgresql://...`` with the password, for psql/pg_dump and psycopg."""
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def sqlalchemy_url(url: URL) -> str:
    return url.render_as_string(hide_password=False)


@dataclass(frozen=True)
class ScratchDb:
    name: str
    admin_url: URL
    owner_url: URL
    app_url: URL

    def engine(self, which: str = "app") -> Engine:
        url = {"app": self.app_url, "owner": self.owner_url, "admin": self.admin_url}[which]
        return make_engine(sqlalchemy_url(url), poolclass=pool.NullPool)


@dataclass(frozen=True)
class ScratchInstance:
    source: ScratchDb
    targets: tuple[ScratchDb, ...]
    owner_role: str
    app_role: str
    master_key: str
    secret_key: str

    def settings(self, db: ScratchDb, root: Path, *, master_key: str | None = None) -> Settings:
        return Settings(
            dev_mode=True,
            database_url=SecretStr(sqlalchemy_url(db.app_url)),
            migration_database_url=SecretStr(sqlalchemy_url(db.owner_url)),
            master_key=SecretStr(master_key or self.master_key),
            secret_key=SecretStr(self.secret_key),
            documents_dir=root / "documents",
            quarantine_dir=root / "quarantine",
            public_url="http://testserver",
        )

    def env(self, db: ScratchDb, root: Path, *, master_key: str | None = None) -> dict[str, str]:
        """``PERFIN_*`` variables for a ``perfin`` subprocess on ``db``."""
        return {
            "PERFIN_DEV_MODE": "true",
            "PERFIN_DATABASE_URL": sqlalchemy_url(db.app_url),
            "PERFIN_MIGRATION_DATABASE_URL": sqlalchemy_url(db.owner_url),
            "PERFIN_DB_APP_ROLE": self.app_role,
            "PERFIN_MASTER_KEY": master_key or self.master_key,
            "PERFIN_SECRET_KEY": self.secret_key,
            "PERFIN_DOCUMENTS_DIR": str(root / "documents"),
            "PERFIN_QUARANTINE_DIR": str(root / "quarantine"),
            "PERFIN_PUBLIC_URL": "http://testserver",
        }


def new_master_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


@contextmanager
def scratch_instance(*, targets: int = 0) -> Iterator[ScratchInstance]:
    admin = make_url(os.environ.get("PERFIN_TEST_DATABASE_URL", DEFAULT_ADMIN_URL))
    suffix = uuid.uuid4().hex[:12]
    owner_role, app_role = f"perfin_sc_owner_{suffix}", f"perfin_sc_app_{suffix}"
    owner_password, app_password = secrets.token_hex(24), secrets.token_hex(24)
    names = [f"perfin_sc_{suffix}_{i}" for i in range(targets + 1)]
    with psycopg.connect(libpq(admin), autocommit=True) as conn:
        for role, password in ((owner_role, owner_password), (app_role, app_password)):
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
        for name in names:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            conn.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
            )
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                    sql.Identifier(name), sql.Identifier(owner_role), sql.Identifier(app_role)
                )
            )
    dbs = [
        ScratchDb(
            name=name,
            admin_url=admin.set(database=name),
            owner_url=admin.set(database=name, username=owner_role, password=owner_password),
            app_url=admin.set(database=name, username=app_role, password=app_password),
        )
        for name in names
    ]
    try:
        for db in dbs:
            # As deploy/db-init: the owner role owns ``public``.
            with psycopg.connect(libpq(db.admin_url), autocommit=True) as conn:
                conn.execute(
                    sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(owner_role))
                )
                conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        source = dbs[0]
        import_all_models()
        engine = source.engine("owner")
        try:
            with engine.begin() as conn:
                Base.metadata.create_all(conn)
                for statement in schema_ddl(Base.metadata, app_role):
                    conn.exec_driver_sql(statement)
        finally:
            engine.dispose()
        cfg = alembic_config()
        cfg.set_main_option("sqlalchemy.url", sqlalchemy_url(source.owner_url).replace("%", "%%"))
        cfg.attributes["perfin_app_role"] = app_role
        command.stamp(cfg, "head")
        yield ScratchInstance(
            source=source,
            targets=tuple(dbs[1:]),
            owner_role=owner_role,
            app_role=app_role,
            master_key=new_master_key(),
            secret_key=secrets.token_urlsafe(48),
        )
    finally:
        with psycopg.connect(libpq(admin), autocommit=True) as conn:
            for name in names:
                conn.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
                )
            for role in (owner_role, app_role):
                conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))

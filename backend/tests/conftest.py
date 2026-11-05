# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures (contract in docs/DEVELOPMENT.md, "Tests").

* One uniquely named database per pytest session, created from the admin URL
  in ``PERFIN_TEST_DATABASE_URL`` and dropped at the end.
* Schema from ``Base.metadata.create_all`` plus ``app.db.rls.schema_ddl`` (RLS,
  audit triggers, grants), created by a fresh non-superuser owner role that
  owns the ``public`` schema, like ``perfin_owner`` in production.
* The application connects as a fresh non-owner, non-superuser login role, so
  row-level security is really enforced in tests.

Fixtures: ``settings``, ``admin_engine``, ``owner_engine``, ``app_engine``,
``db``, ``client``, and the identity factories ``factories``,
``make_household``, ``make_user``, ``login_as``, ``bearer_token`` and
``authed_client`` (``tests/fixtures/factories.py``), and ``ledger``
(committing ledger builders, ``tests/fixtures/ledger.py``).
"""

from __future__ import annotations

import base64
import os
import secrets
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from pydantic import SecretStr
from sqlalchemy import URL, Engine, make_url, pool
from sqlalchemy.orm import Session

from app.core.settings import Settings, get_settings
from app.db.base import Base
from app.db.models import Household
from app.db.registry import import_all_models
from app.db.rls import schema_ddl
from app.db.session import make_app_engine, make_engine, make_sessionmaker
from app.main import create_app
from tests.fixtures.factories import AuthedClient, Factories, LoggedIn, UserRecord
from tests.fixtures.ledger import LedgerFactories

DEFAULT_ADMIN_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"


@dataclass(frozen=True)
class TestDatabase:
    """Coordinates of the per-session test database."""

    __test__ = False  # not a test class

    name: str
    admin_url: URL
    app_url: URL
    app_role: str
    owner_url: URL
    owner_role: str


def _libpq_dsn(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _admin_connect(admin_url: URL) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(_libpq_dsn(admin_url), autocommit=True)


@pytest.fixture(scope="session")
def test_database() -> Iterator[TestDatabase]:
    base_admin = make_url(os.environ.get("PERFIN_TEST_DATABASE_URL", DEFAULT_ADMIN_URL))
    suffix = uuid.uuid4().hex[:12]
    name = f"perfin_test_{suffix}"
    role = f"perfin_app_{suffix}"
    password = secrets.token_urlsafe(24)
    owner_role = f"perfin_owner_{suffix}"
    owner_password = secrets.token_urlsafe(24)

    with _admin_connect(base_admin) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        conn.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS"
            ).format(sql.Identifier(role), sql.Literal(password))
        )
        conn.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS"
            ).format(sql.Identifier(owner_role), sql.Literal(owner_password))
        )
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                sql.Identifier(name), sql.Identifier(role), sql.Identifier(owner_role)
            )
        )

    admin_url = base_admin.set(database=name)
    app_url = base_admin.set(database=name, username=role, password=password)
    owner_url = base_admin.set(database=name, username=owner_role, password=owner_password)
    database = TestDatabase(
        name=name,
        admin_url=admin_url,
        app_url=app_url,
        app_role=role,
        owner_url=owner_url,
        owner_role=owner_role,
    )
    try:
        _create_schema(database)
        yield database
    finally:
        with _admin_connect(base_admin) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(owner_role)))


def _create_schema(database: TestDatabase) -> None:
    import_all_models()
    # As in deploy/db-init: the owner role owns ``public``; nobody else may
    # create objects in it.
    with _admin_connect(database.admin_url) as conn:
        conn.execute(
            sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(database.owner_role))
        )
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    engine = make_engine(
        database.owner_url.render_as_string(hide_password=False), poolclass=pool.NullPool
    )
    try:
        with engine.begin() as conn:
            Base.metadata.create_all(conn)
            for statement in schema_ddl(Base.metadata, database.app_role):
                conn.exec_driver_sql(statement)
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def settings(test_database: TestDatabase) -> Settings:
    """Settings for tests: app role URL, owner URL for migrations, synthetic keys."""
    return Settings(
        dev_mode=True,
        database_url=SecretStr(test_database.app_url.render_as_string(hide_password=False)),
        migration_database_url=SecretStr(
            test_database.admin_url.render_as_string(hide_password=False)
        ),
        master_key=SecretStr(base64.b64encode(secrets.token_bytes(32)).decode("ascii")),
        secret_key=SecretStr(secrets.token_urlsafe(48)),
        public_url="http://testserver",
    )


@pytest.fixture(autouse=True)
def _isolate_settings_cache() -> Iterator[None]:
    """Never let one test's environment leak into another through the cache."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def admin_engine(test_database: TestDatabase) -> Iterator[Engine]:
    """Superuser engine on the test database (schema setup and assertions only)."""
    engine = make_engine(test_database.admin_url.render_as_string(hide_password=False))
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def owner_engine(test_database: TestDatabase) -> Iterator[Engine]:
    """Engine as the non-superuser schema owner (the production ``perfin_owner``)."""
    engine = make_engine(test_database.owner_url.render_as_string(hide_password=False))
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_engine(settings: Settings) -> Iterator[Engine]:
    """Engine as the non-owner application role; RLS applies."""
    engine = make_app_engine(settings)
    yield engine
    engine.dispose()


@pytest.fixture
def db(app_engine: Engine) -> Iterator[Session]:
    """Session as the app role inside a transaction that is always rolled back."""
    with app_engine.connect() as connection:
        transaction = connection.begin()
        session = Session(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """TestClient for an app built from the test settings."""
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Identity factories (Phase 1; tests/fixtures/factories.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def factories(app_engine: Engine, settings: Settings) -> Factories:
    """Committing builders: rows are visible to a ``TestClient`` app."""
    return Factories(make_sessionmaker(app_engine), settings)


@pytest.fixture
def make_household(factories: Factories) -> Callable[..., Household]:
    """``make_household(name=None)`` -> committed ``Household``."""
    return factories.household


@pytest.fixture
def make_user(factories: Factories) -> Callable[..., UserRecord]:
    """``make_user(role="member", household=None, with_totp=False, ...)`` -> ``UserRecord``.

    A new household is created for the user unless ``household`` is given or
    ``role=None``. See ``tests.fixtures.factories.make_user`` for options.
    """
    return factories.user


@pytest.fixture
def login_as(factories: Factories) -> Callable[..., LoggedIn]:
    """``login_as(client, user, mfa_verified=None)``: a real session cookie on ``client``.

    Returns ``LoggedIn`` with the session id and ``headers()`` carrying the
    session-bound CSRF token, Origin and Sec-Fetch-Site for unsafe requests.
    """
    return factories.login


@pytest.fixture
def bearer_token(factories: Factories) -> Callable[..., str]:
    """``bearer_token(user, scopes=(), expires_at=None, revoked=False)`` -> raw PAT."""
    return factories.token


@pytest.fixture
def authed_client(client: TestClient, factories: Factories) -> Callable[..., AuthedClient]:
    """``authed_client(role="member", with_totp=True, ...)``: a logged-in client.

    Creates a user (see ``make_user``), logs the shared ``client`` in as that
    user and returns an ``AuthedClient`` whose unsafe methods add CSRF headers.
    Users get a TOTP factor by default so ``PERFIN_REQUIRE_MFA`` does not
    restrict them.
    """

    def build(**kwargs: Any) -> AuthedClient:
        kwargs.setdefault("with_totp", True)
        record = factories.user(**kwargs)
        return AuthedClient(client, record, factories.login(client, record))

    return build


# ---------------------------------------------------------------------------
# Ledger factories (Phase 2; tests/fixtures/ledger.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger(factories: Factories) -> LedgerFactories:
    """Committing ledger builders: ``ledger.account(household_id, type=...)``, ..."""
    return LedgerFactories(factories.session_factory)

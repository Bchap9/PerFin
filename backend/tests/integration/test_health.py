# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/healthz`` and ``/readyz`` (ARCHITECTURE.md "Observability", SECURITY.md H7)
and the ``perfin healthcheck`` container probe."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, text

from app import __version__
from app.api.routers.health import MIGRATIONS_DIR, migration_heads, readiness_problem
from app.cli import EXIT_FAIL, EXIT_OK, main
from app.core.settings import Settings
from app.main import API_PREFIX, create_app
from tests.conftest import TestDatabase

pytestmark = pytest.mark.integration


@pytest.fixture
def at_head(admin_engine: Engine, test_database: TestDatabase) -> Iterator[frozenset[str]]:
    """Record the shipped migration heads in ``alembic_version`` for the test DB,
    restoring whatever was there afterwards."""
    heads = migration_heads()
    with admin_engine.begin() as conn:
        existed = conn.execute(text("SELECT to_regclass('alembic_version') IS NOT NULL")).scalar()
        previous: list[str] = []
        if existed:
            previous = list(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
            conn.execute(text("DELETE FROM alembic_version"))
        else:
            conn.execute(
                text(
                    "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL "
                    "CONSTRAINT alembic_version_pkc PRIMARY KEY)"
                )
            )
        conn.exec_driver_sql(f'GRANT SELECT ON alembic_version TO "{test_database.app_role}"')
        for head in heads:
            conn.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
    try:
        yield heads
    finally:
        with admin_engine.begin() as conn:
            if existed:
                conn.execute(text("DELETE FROM alembic_version"))
                for value in previous:
                    conn.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": value})
            else:
                conn.execute(text("DROP TABLE alembic_version"))


def test_healthz_is_minimal(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert __version__ not in response.text
    assert "server" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_healthz_does_not_need_the_database() -> None:
    app = create_app(
        Settings(database_url=SecretStr("postgresql+psycopg://nobody@127.0.0.1:1/none"))
    )
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_ok_at_head(client: TestClient, at_head: frozenset[str]) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_503_when_not_at_head(
    client: TestClient, at_head: frozenset[str], admin_engine: Engine
) -> None:
    with admin_engine.begin() as conn:
        conn.execute(text("INSERT INTO alembic_version VALUES ('0000synthetic')"))
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "0000synthetic" not in response.text


def test_readyz_503_when_database_unreachable(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(
        Settings(database_url=SecretStr("postgresql+psycopg://nobody:pw@127.0.0.1:1/none"))
    )
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    for leaked in ("127.0.0.1", "nobody", "psycopg", "OperationalError"):
        assert leaked not in response.text
    assert any("not ready" in record.getMessage() for record in caplog.records)


def test_readiness_problem_reports_unreadable_scripts(app_engine: Engine, tmp_path: Path) -> None:
    missing = tmp_path / "no-migrations-here"
    migration_heads.cache_clear()
    try:
        reason = readiness_problem(app_engine, missing)
    finally:
        migration_heads.cache_clear()
    assert reason is not None


def test_migration_heads_match_scripts() -> None:
    assert MIGRATIONS_DIR.is_dir()
    assert isinstance(migration_heads(), frozenset)


def test_probes_are_root_only_and_not_in_openapi(client: TestClient) -> None:
    assert client.get(f"{API_PREFIX}/healthz").status_code == 404
    assert client.get(f"{API_PREFIX}/readyz").status_code == 404
    paths = client.get(f"{API_PREFIX}/openapi.json").json()["paths"]
    assert "/healthz" not in paths
    assert "/readyz" not in paths


# ---- perfin healthcheck -------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def test_healthcheck_cli_against_running_server(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        monkeypatch.setenv("PERFIN_BIND_PORT", str(port))
        assert main(["healthcheck"]) == EXIT_OK
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_healthcheck_cli_fails_when_nothing_listens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERFIN_BIND_PORT", str(_free_port()))
    assert main(["healthcheck"]) == EXIT_FAIL

# SPDX-License-Identifier: AGPL-3.0-or-later
"""create_app() and router auto-discovery."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config_check import InsecureConfigError
from app.core.settings import Settings, get_settings
from app.main import API_PREFIX, create_app, include_routers
from tests.fixtures import fake_routers


def test_discovery_includes_router_under_prefix_and_root_router() -> None:
    app = FastAPI()
    included = include_routers(app, fake_routers)
    assert included == ["ping"]
    client = TestClient(app)
    assert client.get(f"{API_PREFIX}/ping").json() == {"pong": "v1"}
    assert client.get("/rootping").json() == {"pong": "root"}
    assert client.get("/ping").status_code == 404


def test_openapi_and_docs_hidden_outside_dev_mode() -> None:
    client = TestClient(create_app(Settings(dev_mode=False)))
    assert client.get(f"{API_PREFIX}/openapi.json").status_code == 404
    assert client.get(f"{API_PREFIX}/docs").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_create_app_does_not_connect_to_the_database() -> None:
    # The engine is lazy: building the app (e.g. for `perfin openapi`) needs no DB.
    app = create_app(
        Settings(database_url="postgresql+psycopg://nobody@127.0.0.1:1/none")  # type: ignore[arg-type]
    )
    assert app.title == "PerFin"
    assert app.state.engine.pool.checkedout() == 0


def test_factory_without_settings_refuses_insecure_production_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review finding SR-11: ``uvicorn app.main:create_app --factory`` is gated too."""
    for name in ("PERFIN_MASTER_KEY", "PERFIN_MASTER_KEY_FILE", "PERFIN_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PERFIN_DEV_MODE", "false")
    get_settings.cache_clear()
    with pytest.raises(InsecureConfigError) as excinfo:
        create_app()
    codes = {finding.code for finding in excinfo.value.findings if finding.level == "error"}
    assert {"master_key.missing", "public_url.not_https"} <= codes
    # Dev mode downgrades the same problems to warnings and serves.
    monkeypatch.setenv("PERFIN_DEV_MODE", "true")
    get_settings.cache_clear()
    assert create_app().title == "PerFin"

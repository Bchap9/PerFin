# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``mcp`` role's configuration gate (AI_ACCESS.md M7, SECURITY.md H6).

The role needs only ``PERFIN_MCP_API_URL`` and ``PERFIN_MCP_TOKEN(_FILE)``;
it refuses to start with a database URL, the master key, the secret key or
another credential in its environment, and the HTTP transport needs explicit
https origins and a separate MCP-client bearer secret.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import cli
from app.mcp.config import FORBIDDEN_ENV, McpConfigError, check, load

pytestmark = pytest.mark.security

TOKEN = "pfa_" + secrets.token_urlsafe(32)
HTTP_TOKEN = secrets.token_urlsafe(40)
BASE = {
    "PERFIN_MCP_ENABLED": "true",
    "PERFIN_MCP_API_URL": "http://api:8000",
    "PERFIN_MCP_TOKEN": TOKEN,
}
HTTP = BASE | {
    "PERFIN_MCP_TRANSPORT": "http",
    "PERFIN_MCP_HTTP_ALLOWED_ORIGINS": "https://ai.example.test",
    "PERFIN_MCP_HTTP_TOKEN": HTTP_TOKEN,
}


def codes(env: dict[str, str]) -> set[str]:
    return {f.code for f in check(env)[1] if f.level == "error"}


def test_minimal_stdio_environment_is_enough() -> None:
    config, findings = check(BASE)
    assert config is not None, findings
    assert findings == []
    assert config.transport == "stdio"
    assert config.api_url == "http://api:8000"
    assert config.token == TOKEN
    assert TOKEN not in repr(config)


@pytest.mark.parametrize("name", sorted(FORBIDDEN_ENV))
def test_refuses_database_and_key_material(name: str) -> None:
    assert "mcp.forbidden_secret" in codes(BASE | {name: "postgresql+psycopg://x:y@db/perfin"})


@pytest.mark.parametrize(
    "name", ["PERFIN_FUTURE_DATABASE_URL", "PERFIN_BACKUP_MASTER_KEY", "PERFIN_X_PASSWORD"]
)
def test_refuses_renamed_secrets(name: str) -> None:
    assert "mcp.forbidden_secret" in codes(BASE | {name: "value"})


def test_empty_forbidden_variable_is_not_a_secret() -> None:
    # compose may blank a variable; an empty value carries nothing.
    assert codes(BASE | {"PERFIN_DATABASE_URL": ""}) == set()


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"PERFIN_MCP_ENABLED": "false"}, "mcp.disabled"),
        ({"PERFIN_MCP_ENABLED": ""}, "mcp.disabled"),
        ({"PERFIN_MCP_API_URL": ""}, "mcp.api_url_missing"),
        ({"PERFIN_MCP_API_URL": "ftp://api"}, "mcp.api_url_invalid"),
        ({"PERFIN_MCP_API_URL": "http://u:p@api:8000"}, "mcp.api_url_invalid"),
        ({"PERFIN_MCP_API_URL": "http://api:8000/?x=1"}, "mcp.api_url_invalid"),
        ({"PERFIN_MCP_TOKEN": ""}, "mcp.secret_missing"),
        ({"PERFIN_MCP_TOKEN": "a b"}, "mcp.secret_malformed"),
        ({"PERFIN_MCP_TRANSPORT": "sse"}, "mcp.transport_invalid"),
        ({"PERFIN_MCP_RATE_PER_MINUTE": "0"}, "mcp.invalid_number"),
        ({"PERFIN_MCP_RATE_PER_MINUTE": "100000"}, "mcp.invalid_number"),
    ],
)
def test_stdio_errors(change: dict[str, str], code: str) -> None:
    env = BASE | change
    assert code in codes(env)
    with pytest.raises(McpConfigError):
        load(env)


def test_token_file(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n")
    path.chmod(0o600)
    env = {k: v for k, v in BASE.items() if k != "PERFIN_MCP_TOKEN"}
    config = load(env | {"PERFIN_MCP_TOKEN_FILE": str(path)})
    assert config.token == TOKEN
    assert "mcp.secret_ambiguous" in codes(BASE | {"PERFIN_MCP_TOKEN_FILE": str(path)})
    assert "mcp.secret_unreadable" in codes(env | {"PERFIN_MCP_TOKEN_FILE": str(tmp_path / "x")})
    path.chmod(0o644)
    _, findings = check(env | {"PERFIN_MCP_TOKEN_FILE": str(path)})
    assert {f.code for f in findings} == {"mcp.secret_file_mode"}


def test_http_transport_requirements() -> None:
    config = load(HTTP)
    assert config.transport == "http"
    assert config.http_allowed_origins == ("https://ai.example.test",)
    assert config.http_token == HTTP_TOKEN
    for change, code in (
        ({"PERFIN_MCP_HTTP_ALLOWED_ORIGINS": ""}, "mcp.origins_missing"),
        ({"PERFIN_MCP_HTTP_ALLOWED_ORIGINS": "*"}, "mcp.origins_wildcard"),
        ({"PERFIN_MCP_HTTP_ALLOWED_ORIGINS": "https://*.example.test"}, "mcp.origins_wildcard"),
        ({"PERFIN_MCP_HTTP_ALLOWED_ORIGINS": "http://ai.example.test"}, "mcp.origin_not_https"),
        ({"PERFIN_MCP_HTTP_ALLOWED_ORIGINS": "https://a.test/path"}, "mcp.origin_invalid"),
        ({"PERFIN_MCP_HTTP_TOKEN": ""}, "mcp.secret_missing"),
        ({"PERFIN_MCP_HTTP_TOKEN": "short"}, "mcp.http_token_weak"),
        ({"PERFIN_MCP_HTTP_TOKEN": TOKEN}, "mcp.http_token_reused"),
    ):
        assert code in codes(HTTP | change), change
    # Plain http origins only in dev mode.
    dev = HTTP | {"PERFIN_DEV_MODE": "true", "PERFIN_MCP_HTTP_ALLOWED_ORIGINS": "http://localhost"}
    assert codes(dev) == set()


# ---- CLI gate ----------------------------------------------------------------


@pytest.fixture
def mcp_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in list(os.environ):
        if name.startswith(("PERFIN_", "POSTGRES_")):
            monkeypatch.delenv(name)
    for name, value in BASE.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


@pytest.fixture
def restore_logging() -> Iterator[None]:
    import logging

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.mark.usefixtures("restore_logging")
def test_perfin_mcp_refuses_to_start_with_database_or_keys(
    mcp_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[object] = []
    mcp_env.setattr("app.mcp.server.run", lambda config: started.append(config))
    assert cli.main(["mcp"]) == cli.EXIT_OK
    assert len(started) == 1
    for name, value in (
        ("PERFIN_DATABASE_URL", "postgresql+psycopg://perfin_app:pw@db/perfin"),
        ("PERFIN_MASTER_KEY", "x" * 44),
        ("PERFIN_SECRET_KEY", "y" * 64),
    ):
        mcp_env.setenv(name, value)
        assert cli.main(["mcp"]) == cli.EXIT_FAIL
        assert "mcp.forbidden_secret" in capsys.readouterr().err
        mcp_env.delenv(name)
    mcp_env.delenv("PERFIN_MCP_TOKEN")
    assert cli.main(["mcp"]) == cli.EXIT_FAIL
    assert "mcp.secret_missing" in capsys.readouterr().err
    assert len(started) == 1


def test_mcp_gate_never_loads_application_settings(
    mcp_env: pytest.MonkeyPatch, restore_logging: None
) -> None:
    def boom() -> object:
        raise AssertionError("the mcp role read Settings")

    mcp_env.setattr("app.core.settings.get_settings", boom)
    mcp_env.setattr("app.mcp.server.run", lambda config: None)
    assert cli.main(["mcp"]) == cli.EXIT_OK


def test_config_check_is_role_aware(
    mcp_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mcp_env.setenv("PERFIN_ROLE", "mcp")
    assert cli.main(["config", "check"]) == cli.EXIT_OK
    assert "config check (mcp): OK" in capsys.readouterr().out
    mcp_env.setenv("PERFIN_MASTER_KEY_FILE", "/run/secrets/master")
    assert cli.main(["config", "check"]) == cli.EXIT_FAIL
    assert "mcp.forbidden_secret" in capsys.readouterr().err

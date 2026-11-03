# SPDX-License-Identifier: AGPL-3.0-or-later
"""``perfin config check`` and the startup gate (SECURITY.md H6, F5, G2, G4)."""

from __future__ import annotations

import argparse
import base64
import logging
import os
from collections.abc import Iterator
from ipaddress import ip_network
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from app import cli
from app.core.config_check import (
    Finding,
    InsecureConfigError,
    check_config,
    enforce_config,
    has_errors,
)
from app.core.settings import Settings, get_settings

KEY = base64.b64encode(bytes(range(32))).decode("ascii")
# Synthetic, high-entropy, never used anywhere real.
SECRET = base64.b64encode(bytes((i * 37 + 11) % 256 for i in range(48))).decode("ascii")
DB_PASSWORD = "synthetic-db-pw-" + "Zq9xW2"
OIDC_SECRET = "synthetic-oidc-client-secret-Kd83"

SECURE_ENV: dict[str, str] = {
    "PERFIN_PUBLIC_URL": "https://perfin.example.test",
    "PERFIN_MASTER_KEY": KEY,
    "PERFIN_SECRET_KEY": SECRET,
    "PERFIN_DATABASE_URL": f"postgresql+psycopg://perfin:{DB_PASSWORD}@db:5432/perfin",
    "PERFIN_TRUSTED_PROXIES": "172.16.0.0/12",
    "PERFIN_BREAKGLASS_CIDRS": "127.0.0.1/32",
    "PERFIN_LOG_LEVEL": "INFO",
    "PERFIN_MASTER_KEY_BACKUP_ACKNOWLEDGED": "true",
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    for name in list(os.environ):
        if name.startswith("PERFIN_"):
            monkeypatch.delenv(name)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture
def secure_env(clean_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name, value in SECURE_ENV.items():
        clean_env.setenv(name, value)
    return clean_env


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    names = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
    saved = {
        n: (
            list(logging.getLogger(n).handlers),
            logging.getLogger(n).propagate,
            logging.getLogger(n).level,
            list(logging.getLogger(n).filters),
        )
        for n in names
    }
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for n, (handlers, propagate, level, filters) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers[:] = handlers
        lg.propagate = propagate
        lg.setLevel(level)
        lg.filters[:] = filters


def secure() -> Settings:
    """A configuration that passes every check (env must be ``secure_env``)."""
    return Settings()


def with_(**update: Any) -> Settings:
    return secure().model_copy(update=update)


def codes(findings: list[Finding], level: str | None = None) -> set[str]:
    return {f.code for f in findings if level is None or f.level == level}


# ---------------------------------------------------------------------------
# check_config
# ---------------------------------------------------------------------------


def test_secure_configuration_has_no_findings(secure_env: pytest.MonkeyPatch) -> None:
    assert check_config(secure()) == []


def test_defaults_are_refused_outside_dev_mode(clean_env: pytest.MonkeyPatch) -> None:
    findings = check_config(Settings())
    errors = codes(findings, "error")
    assert {
        "master_key.missing",
        "secret_key.missing",
        "public_url.not_https",
    } <= errors
    assert "master_key.backup_unacknowledged" in codes(findings, "warning")
    assert has_errors(findings)


@pytest.mark.parametrize(
    ("update", "code"),
    [
        ({"master_key": None}, "master_key.missing"),
        ({"master_key": SecretStr("CHANGE_ME")}, "master_key.placeholder"),
        (
            {"master_key": SecretStr(base64.b64encode(b"short").decode())},
            "master_key.invalid",
        ),
        ({"master_key": SecretStr("not base64 !!")}, "master_key.invalid"),
        ({"secret_key": None}, "secret_key.missing"),
        ({"secret_key": SecretStr("CHANGE_ME")}, "secret_key.placeholder"),
        ({"secret_key": SecretStr("changeme")}, "secret_key.placeholder"),
        ({"secret_key": SecretStr("tooShortSecret1234")}, "secret_key.weak"),
        ({"secret_key": SecretStr("ab" * 64)}, "secret_key.weak"),
        ({"secret_key": SecretStr(bytes(range(16)).hex())}, "secret_key.weak"),
        (
            {"database_url": SecretStr("postgresql+psycopg://perfin:CHANGE_ME@db:5432/perfin")},
            "database_url.default_password",
        ),
        (
            {"database_url": SecretStr("postgresql+psycopg://postgres:postgres@db/perfin")},
            "database_url.default_password",
        ),
        (
            {
                "migration_database_url": SecretStr(
                    "postgresql+psycopg://owner:password@db:5432/perfin"
                )
            },
            "migration_database_url.default_password",
        ),
        ({"public_url": "http://perfin.example.test"}, "public_url.not_https"),
        ({"public_url": "https://"}, "public_url.not_https"),
        ({"secure_cookies": False}, "cookies.insecure"),
        ({"log_level": "DEBUG"}, "log_level.debug"),
        (
            {"oidc_issuer": "http://auth.example.test/", "oidc_client_id": "perfin"},
            "oidc.issuer_not_https",
        ),
        (
            {
                "oidc_issuer": "https://auth.example.test/",
                "oidc_client_secret": SecretStr(OIDC_SECRET),
            },
            "oidc.client_id_missing",
        ),
        (
            {"oidc_issuer": "https://auth.example.test/", "oidc_client_id": "perfin"},
            "oidc.client_secret_missing",
        ),
        (
            {
                "oidc_issuer": "https://auth.example.test/",
                "oidc_client_id": "perfin",
                "oidc_client_secret": SecretStr("CHANGE_ME"),
            },
            "oidc.client_secret_placeholder",
        ),
        ({"local_login_enabled": False}, "auth.lockout"),
        ({"breakglass_cidrs": [ip_network("0.0.0.0/0")]}, "breakglass.any_address"),
        ({"breakglass_cidrs": [ip_network("::/0")]}, "breakglass.any_address"),
        (
            {"trusted_proxies": [ip_network("10.0.0.0/8"), ip_network("0.0.0.0/0")]},
            "trusted_proxies.any_address",
        ),
        ({"trusted_proxies": [ip_network("::/0")]}, "trusted_proxies.any_address"),
        ({"mcp_transport": "http"}, "mcp.origins_missing"),
        (
            {"mcp_transport": "http", "mcp_http_allowed_origins": ["*"]},
            "mcp.origins_wildcard",
        ),
    ],
)
def test_insecure_setting_is_an_error(
    secure_env: pytest.MonkeyPatch, update: dict[str, Any], code: str
) -> None:
    findings = check_config(with_(**update))
    assert code in codes(findings, "error"), findings
    assert has_errors(findings)


@pytest.mark.parametrize(
    ("update", "code"),
    [
        ({"oidc_allow_private_issuer": True}, "oidc.private_issuer_allowed"),
        ({"master_key_backup_acknowledged": False}, "master_key.backup_unacknowledged"),
        ({"require_mfa": False}, "mfa.not_required"),
        ({"csp_trusted_types": False}, "csp.trusted_types_disabled"),
        ({"metrics_bind_host": "0.0.0.0"}, "metrics.bind_all_interfaces"),  # noqa: S104
        ({"metrics_bind_host": "::"}, "metrics.bind_all_interfaces"),
        (
            {"database_url": SecretStr("postgresql+psycopg://perfin@db.internal:5432/perfin")},
            "database_url.no_password",
        ),
    ],
)
def test_risky_setting_is_a_warning(
    secure_env: pytest.MonkeyPatch, update: dict[str, Any], code: str
) -> None:
    findings = check_config(with_(**update))
    assert codes(findings) == {code}
    assert [f.level for f in findings] == ["warning"]
    assert not has_errors(findings)


def test_debug_message_matches_f5(secure_env: pytest.MonkeyPatch) -> None:
    (finding,) = check_config(with_(log_level="DEBUG"))
    assert "DEBUG cannot be enabled in production mode" in finding.message


@pytest.mark.parametrize(
    "secret",
    [
        base64.b64encode(bytes(range(100, 132))).decode(),  # openssl rand -base64 32
        base64.urlsafe_b64encode(bytes(range(60, 108))).decode().rstrip("="),
        bytes(range(200, 232)).hex(),  # 32 random bytes as hex
    ],
)
def test_strong_secret_key_forms_are_accepted(secure_env: pytest.MonkeyPatch, secret: str) -> None:
    assert check_config(with_(secret_key=SecretStr(secret))) == []


def test_oidc_fully_configured_is_accepted(secure_env: pytest.MonkeyPatch) -> None:
    settings = with_(
        oidc_issuer="https://auth.example.test/application/o/perfin/",
        oidc_client_id="perfin",
        oidc_client_secret=SecretStr(OIDC_SECRET),
        local_login_enabled=False,
    )
    assert check_config(settings) == []


def test_mcp_http_with_origins_is_accepted(secure_env: pytest.MonkeyPatch) -> None:
    settings = with_(mcp_transport="http", mcp_http_allowed_origins=["https://ai.example.test"])
    assert check_config(settings) == []


def test_findings_never_contain_secret_values(secure_env: pytest.MonkeyPatch) -> None:
    weak = "weak-but-unique-value-7Q"
    settings = with_(
        secret_key=SecretStr(weak),
        master_key=SecretStr("bm90LWEta2V5"),
        database_url=SecretStr("postgresql+psycopg://perfin:changeme@db/perfin"),
        oidc_issuer="https://auth.example.test/",
        oidc_client_id="perfin",
        oidc_client_secret=SecretStr("change_me"),
    )
    rendered = "\n".join(f.render() for f in check_config(settings))
    for value in (weak, "bm90LWEta2V5", "changeme", "change_me"):
        assert value not in rendered


def test_errors_sorted_before_warnings(clean_env: pytest.MonkeyPatch) -> None:
    levels = [f.level for f in check_config(Settings())]
    assert levels == sorted(levels, key=lambda lv: 0 if lv == "error" else 1)


# ---------------------------------------------------------------------------
# Dev mode
# ---------------------------------------------------------------------------


def test_dev_mode_is_a_loud_warning(secure_env: pytest.MonkeyPatch) -> None:
    findings = check_config(with_(dev_mode=True))
    assert findings[0].code == "dev_mode.enabled"
    assert findings[0].level == "warning"
    assert "SECURITY CHECKS ARE DISABLED" in findings[0].message
    assert not has_errors(findings)


def test_dev_mode_downgrades_errors_but_still_reports_them(
    clean_env: pytest.MonkeyPatch,
) -> None:
    findings = check_config(Settings(dev_mode=True))
    assert not has_errors(findings)
    assert {"dev_mode.enabled", "master_key.missing", "public_url.not_https"} <= codes(findings)
    assert all(f.level == "warning" for f in findings)


# ---------------------------------------------------------------------------
# Master key file permissions (G2)
# ---------------------------------------------------------------------------


def _key_file(tmp_path: Path, mode: int) -> Path:
    path = tmp_path / "master_key"
    path.write_text(KEY + "\n", encoding="ascii")
    path.chmod(mode)
    return path


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (0o400, set()),
        (0o600, set()),
        (0o440, {"master_key.file_group_readable"}),
        (0o444, {"master_key.file_world_readable"}),
        (0o602, {"master_key.file_world_readable"}),
    ],
)
def test_master_key_file_mode(
    secure_env: pytest.MonkeyPatch, tmp_path: Path, mode: int, expected: set[str]
) -> None:
    settings = with_(master_key=None, master_key_file=_key_file(tmp_path, mode))
    findings = check_config(settings)
    assert codes(findings) == expected
    if "master_key.file_world_readable" in expected:
        assert has_errors(findings)
    else:
        assert not has_errors(findings)


def test_master_key_file_missing(secure_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    findings = check_config(with_(master_key=None, master_key_file=tmp_path / "absent"))
    assert codes(findings, "error") == {"master_key.invalid"}


# ---------------------------------------------------------------------------
# enforce_config and the CLI
# ---------------------------------------------------------------------------


def test_enforce_config_raises_on_errors(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(InsecureConfigError) as info:
        enforce_config(Settings())
    assert has_errors(info.value.findings)
    assert "refusing to start" in str(info.value)


def test_enforce_config_logs_warnings_and_returns(
    secure_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="perfin.config"):
        findings = enforce_config(with_(oidc_allow_private_issuer=True))
    assert codes(findings) == {"oidc.private_issuer_allowed"}
    assert any(getattr(r, "code", None) == "oidc.private_issuer_allowed" for r in caplog.records)


def test_cli_config_check_ok(
    secure_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["config", "check"]) == cli.EXIT_OK
    assert "OK" in capsys.readouterr().out


def test_cli_config_check_warnings_still_ok(
    secure_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secure_env.setenv("PERFIN_MASTER_KEY_BACKUP_ACKNOWLEDGED", "false")
    assert cli.main(["config", "check"]) == cli.EXIT_OK
    assert "WARNING master_key.backup_unacknowledged" in capsys.readouterr().err


def test_cli_config_check_refuses_without_echoing_secrets(
    secure_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secure_env.setenv("PERFIN_SECRET_KEY", "short-synthetic-Xy")
    secure_env.setenv("PERFIN_LOG_LEVEL", "debug")
    assert cli.main(["config", "check"]) == cli.EXIT_FAIL
    err = capsys.readouterr().err
    assert "ERROR secret_key.weak" in err
    assert "ERROR log_level.debug" in err
    assert "short-synthetic-Xy" not in err
    assert DB_PASSWORD not in err
    assert KEY not in err


def test_cli_config_check_dev_mode_passes(
    clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clean_env.setenv("PERFIN_DEV_MODE", "true")
    assert cli.main(["config", "check"]) == cli.EXIT_OK
    assert "dev_mode.enabled" in capsys.readouterr().err


@pytest.mark.usefixtures("restore_logging")
def test_api_refuses_to_boot_on_insecure_config(
    clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    def fail(*_a: object, **_k: object) -> None:
        raise AssertionError("uvicorn must not start")

    clean_env.setattr(uvicorn, "run", fail)
    assert cli.main(["api"]) == cli.EXIT_FAIL
    assert "refusing to start" in capsys.readouterr().err


@pytest.mark.usefixtures("restore_logging")
def test_api_boots_on_secure_config_with_json_logging(secure_env: pytest.MonkeyPatch) -> None:
    import uvicorn

    calls: list[dict[str, object]] = []

    def record(*_a: object, **kwargs: object) -> None:
        calls.append(kwargs)

    secure_env.setattr(uvicorn, "run", record)
    assert cli.main(["api"]) == cli.EXIT_OK
    (kwargs,) = calls
    assert kwargs["log_config"] is None
    assert kwargs["access_log"] is True
    assert any(h.get_name() == "perfin-json" for h in logging.getLogger().handlers)


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("command", ["worker", "mcp"])
def test_every_serving_role_is_gated(
    clean_env: pytest.MonkeyPatch, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    ran: list[str] = []

    def real_handler(_args: object) -> int:
        ran.append(command)
        return cli.EXIT_OK

    def parser_with_real_handler() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="perfin")
        sub = parser.add_subparsers(dest="command", required=True)
        sub.add_parser(command).set_defaults(func=real_handler)
        return parser

    clean_env.setattr(cli, "build_parser", parser_with_real_handler)
    assert cli.main([command]) == cli.EXIT_FAIL
    assert ran == []
    for name, value in SECURE_ENV.items():
        clean_env.setenv(name, value)
    get_settings.cache_clear()
    assert cli.main([command]) == cli.EXIT_OK
    assert ran == [command]


def test_serving_commands_cover_all_roles() -> None:
    assert frozenset({"api", "worker", "mcp"}) == cli.SERVING_COMMANDS


def test_env_example_documents_backup_acknowledgement() -> None:
    env_example = Path(__file__).resolve().parents[3] / "deploy" / ".env.example"
    text = env_example.read_text(encoding="utf-8")
    assert "PERFIN_MASTER_KEY_BACKUP_ACKNOWLEDGED=" in text
    assert "PERFIN_SECURE_COOKIES=true" in text

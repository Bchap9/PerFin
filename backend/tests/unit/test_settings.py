# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings parsing from the environment."""

from __future__ import annotations

import base64
import re
from ipaddress import ip_network
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.core.settings import MasterKeyError, Settings, get_settings

ENV_EXAMPLE = Path(__file__).resolve().parents[3] / "deploy" / ".env.example"
KEY = base64.b64encode(bytes(range(32))).decode("ascii")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    import os

    for name in list(os.environ):
        if name.startswith("PERFIN_"):
            monkeypatch.delenv(name)
    return monkeypatch


def test_every_env_example_variable_is_a_setting() -> None:
    names = re.findall(r"^#?\s*PERFIN_([A-Z0-9_]+)=", ENV_EXAMPLE.read_text(), flags=re.M)
    assert names, "no PERFIN_ variables found in .env.example"
    missing = sorted({n.lower() for n in names} - set(Settings.model_fields))
    assert missing == []


def test_defaults_are_secure(clean_env: pytest.MonkeyPatch) -> None:
    s = Settings()
    assert s.dev_mode is False
    assert s.require_mfa is True
    assert s.oidc_require_group is True
    assert s.oidc_jit_provisioning is False
    assert s.oidc_allow_private_issuer is False
    assert s.mcp_enabled is False
    assert s.bind_host == "127.0.0.1"
    assert s.oidc_enabled is False


def test_env_parsing(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("PERFIN_TRUSTED_PROXIES", "172.16.0.0/12, 10.0.0.0/8")
    clean_env.setenv("PERFIN_BREAKGLASS_CIDRS", "127.0.0.1/32,::1/128")
    clean_env.setenv("PERFIN_OIDC_SCOPES", "openid profile  email groups")
    clean_env.setenv("PERFIN_MCP_HTTP_ALLOWED_ORIGINS", "")
    clean_env.setenv("PERFIN_OIDC_CLIENT_SECRET", "")
    clean_env.setenv("PERFIN_LOG_LEVEL", "debug")
    clean_env.setenv("PERFIN_DROPFOLDER_AFTER", "keep")
    clean_env.setenv("PERFIN_REQUIRE_MFA", "false")
    clean_env.setenv("PERFIN_OIDC_ISSUER", "https://auth.example.test/application/o/perfin/")
    s = Settings()
    assert s.trusted_proxies == [ip_network("172.16.0.0/12"), ip_network("10.0.0.0/8")]
    assert s.breakglass_cidrs == [ip_network("127.0.0.1/32"), ip_network("::1/128")]
    assert s.oidc_scopes == ["openid", "profile", "email", "groups"]
    assert s.mcp_http_allowed_origins == []
    assert s.oidc_client_secret is None
    assert s.log_level == "DEBUG"
    assert s.dropfolder_after == "keep"
    assert s.require_mfa is False
    assert s.oidc_enabled is True


def test_invalid_values_rejected(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("PERFIN_TRUSTED_PROXIES", "not-a-cidr")
    with pytest.raises(ValidationError):
        Settings()


def test_secrets_are_not_rendered(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("PERFIN_SECRET_KEY", "synthetic-secret-value")
    clean_env.setenv("PERFIN_DATABASE_URL", "postgresql+psycopg://u:hunter2pw@db:5432/perfin")
    s = Settings()
    rendered = repr(s) + str(s.model_dump())
    assert "synthetic-secret-value" not in rendered
    assert "hunter2pw" not in rendered


def test_migration_url_falls_back_to_app_url(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("PERFIN_DATABASE_URL", "postgresql+psycopg://app@db/perfin")
    clean_env.setenv("PERFIN_MIGRATION_DATABASE_URL", "")
    s = Settings()
    assert s.effective_migration_database_url.get_secret_value().startswith(
        "postgresql+psycopg://app@"
    )
    clean_env.setenv("PERFIN_MIGRATION_DATABASE_URL", "postgresql+psycopg://owner@db/perfin")
    s = Settings()
    assert s.effective_migration_database_url.get_secret_value().startswith(
        "postgresql+psycopg://owner@"
    )


def test_master_key_from_env(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("PERFIN_MASTER_KEY", KEY)
    assert Settings().load_master_key() == bytes(range(32))


def test_master_key_from_file(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key_file = tmp_path / "master_key"
    key_file.write_text(KEY + "\n", encoding="ascii")
    clean_env.setenv("PERFIN_MASTER_KEY_FILE", str(key_file))
    assert Settings().load_master_key() == bytes(range(32))


def test_master_key_both_sources_rejected(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clean_env.setenv("PERFIN_MASTER_KEY", KEY)
    clean_env.setenv("PERFIN_MASTER_KEY_FILE", str(tmp_path / "k"))
    with pytest.raises(ValidationError, match="only one"):
        Settings()


@pytest.mark.parametrize(
    "value",
    [
        "CHANGE_ME",
        base64.b64encode(b"short").decode("ascii"),
        base64.b64encode(bytes(33)).decode("ascii"),
    ],
)
def test_master_key_malformed(clean_env: pytest.MonkeyPatch, value: str) -> None:
    clean_env.setenv("PERFIN_MASTER_KEY", value)
    with pytest.raises(MasterKeyError) as info:
        Settings().load_master_key()
    assert value not in str(info.value)


def test_master_key_missing(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(MasterKeyError, match="no master key"):
        Settings().load_master_key()
    clean_env.setenv("PERFIN_MASTER_KEY_FILE", str(tmp_path / "absent"))
    with pytest.raises(MasterKeyError, match="not readable"):
        Settings().load_master_key()


def test_get_settings_is_cached(clean_env: pytest.MonkeyPatch) -> None:
    assert get_settings() is get_settings()


def test_settings_are_immutable(clean_env: pytest.MonkeyPatch) -> None:
    s = Settings()
    with pytest.raises(ValidationError):
        s.dev_mode = True  # type: ignore[misc]
    assert isinstance(s.database_url, SecretStr)

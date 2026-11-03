# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime configuration.

Every variable in ``deploy/.env.example`` that starts with ``PERFIN_`` maps to a
field here (the prefix is stripped, names are case-insensitive). Secrets are
``SecretStr`` so they never appear in ``repr()`` or logs. Insecure combinations
are not rejected here; ``app.core.config_check`` reports them so that
``perfin config check`` can explain every problem at once.
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

MASTER_KEY_BYTES = 32

IPNetwork = IPv4Network | IPv6Network
Role = Literal["api", "worker", "mcp"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _split(value: object, sep: str | None) -> object:
    """Parse a separator-delimited env string into a list; pass other values through."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(sep) if part.strip()]
    return value


class MasterKeyError(ValueError):
    """The master key is missing, unreadable or malformed."""


class Settings(BaseSettings):
    """PerFin settings, read from ``PERFIN_*`` environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="PERFIN_",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # ---- Instance -----------------------------------------------------------
    version: str = "latest"
    public_url: str = "http://localhost:8000"
    role: Role = "api"
    dev_mode: bool = False
    bind_host: str = "127.0.0.1"
    bind_port: Annotated[int, Field(ge=1, le=65535)] = 8000
    # CSP ``require-trusted-types-for 'script'`` (see app.core.headers).
    csp_trusted_types: bool = True
    # Built SPA (frontend/dist) served by the API (app.core.spa); the image
    # sets /app/static. Unset: the API serves no UI (development with Vite).
    static_dir: Path | None = None

    # ---- Secrets ------------------------------------------------------------
    master_key: SecretStr | None = None
    master_key_file: Path | None = None
    secret_key: SecretStr | None = None
    # G4: the operator confirms the master key is backed up separately from
    # the data. Until then every startup logs a warning (config check G4).
    master_key_backup_acknowledged: bool = False

    # ---- Database -----------------------------------------------------------
    # The application role (non-owner, RLS applies) and the owner role used
    # only by migrations. When the migration URL is unset the app URL is used.
    database_url: SecretStr = SecretStr("postgresql+psycopg://perfin@localhost:5432/perfin")
    migration_database_url: SecretStr | None = None
    db_statement_timeout_ms: Annotated[int, Field(ge=0)] = 30_000
    # F8: waits on row/table/advisory locks give up instead of piling up.
    db_lock_timeout_ms: Annotated[int, Field(ge=0)] = 10_000
    # Role the migrations grant DML to (deploy/db-init creates it); the
    # migration itself connects as the owner role.
    db_app_role: Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_]{0,62}$")] = "perfin_app"

    # ---- Network ------------------------------------------------------------
    trusted_proxies: Annotated[list[IPNetwork], NoDecode] = Field(default_factory=list)

    # ---- Sessions -----------------------------------------------------------
    session_idle_minutes: Annotated[int, Field(ge=1)] = 30
    session_absolute_hours: Annotated[int, Field(ge=1)] = 12
    # Cookies carry ``Secure`` (and the ``__Host-`` prefix). Turning this off
    # is refused by ``perfin config check`` outside dev mode.
    secure_cookies: bool = True
    require_mfa: bool = True

    # ---- Local login / break-glass -----------------------------------------
    local_login_enabled: bool = True
    breakglass_cidrs: Annotated[list[IPNetwork], NoDecode] = Field(
        default_factory=lambda: [ip_network("127.0.0.1/32")]
    )

    # ---- OIDC ---------------------------------------------------------------
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr | None = None
    oidc_scopes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["openid", "profile", "email", "groups"]
    )
    oidc_jit_provisioning: bool = False
    oidc_group_admin: str = "perfin-admins"
    oidc_group_member: str = "perfin-users"
    oidc_group_viewer: str = "perfin-viewers"
    oidc_require_group: bool = True
    oidc_allow_private_issuer: bool = False

    # ---- Intake -------------------------------------------------------------
    upload_max_mb: Annotated[int, Field(ge=1, le=1024)] = 50
    dropfolder_inbox: Path = Path("./inbox")
    dropfolder_processed: Path = Path("./processed")
    dropfolder_settle_seconds: Annotated[int, Field(ge=0)] = 10
    dropfolder_after: Literal["move", "delete", "keep"] = "move"
    email_intake_enabled: bool = False

    # ---- Integrations -------------------------------------------------------
    price_provider: str = "none"
    antivirus: Literal["none", "clamav"] = "none"

    # ---- AI access (MCP role) ----------------------------------------------
    mcp_enabled: bool = False
    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_http_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ---- Observability ------------------------------------------------------
    log_level: LogLevel = "INFO"
    metrics_port: Annotated[int, Field(ge=1, le=65535)] = 9100
    # Interface of the internal metrics server (H7). Loopback by default, even
    # in the container (where PERFIN_BIND_HOST is 0.0.0.0), so neither the
    # proxy nor the mcp container on the `web` network can reach it; the
    # container healthchecks scrape 127.0.0.1. To scrape from Prometheus, set
    # it to the container's address on the `backend` network.
    metrics_bind_host: str = "127.0.0.1"

    @field_validator("trusted_proxies", "breakglass_cidrs", mode="before")
    @classmethod
    def _parse_cidrs(cls, value: object) -> object:
        return _split(value, ",")

    @field_validator("mcp_http_allowed_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: object) -> object:
        return _split(value, ",")

    @field_validator("oidc_scopes", mode="before")
    @classmethod
    def _parse_scopes(cls, value: object) -> object:
        return _split(value, None)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("master_key", "secret_key", "oidc_client_secret", mode="before")
    @classmethod
    def _empty_secret_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("migration_database_url", mode="before")
    @classmethod
    def _empty_url_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def _one_master_key_source(self) -> Settings:
        if self.master_key is not None and self.master_key_file is not None:
            raise ValueError("set only one of PERFIN_MASTER_KEY and PERFIN_MASTER_KEY_FILE")
        return self

    # ---- Derived values -----------------------------------------------------
    @property
    def effective_migration_database_url(self) -> SecretStr:
        """URL for the owner role that runs migrations (falls back to the app URL)."""
        return self.migration_database_url or self.database_url

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer.strip())

    @property
    def upload_max_bytes(self) -> int:
        return self.upload_max_mb * 1024 * 1024

    def load_master_key(self) -> bytes:
        """Return the 32-byte master key from ``PERFIN_MASTER_KEY`` or ``..._FILE``.

        The value is standard base64 of exactly 32 random bytes. Raises
        ``MasterKeyError`` without echoing any part of the value.
        """
        if self.master_key_file is not None:
            try:
                raw = self.master_key_file.read_text(encoding="ascii").strip()
            except (OSError, UnicodeDecodeError) as exc:
                raise MasterKeyError("master key file is not readable") from exc
        elif self.master_key is not None:
            raw = self.master_key.get_secret_value().strip()
        else:
            raise MasterKeyError("no master key configured")
        try:
            key = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MasterKeyError("master key is not valid base64") from exc
        if len(key) != MASTER_KEY_BYTES:
            raise MasterKeyError(f"master key must decode to {MASTER_KEY_BYTES} bytes")
        return key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Tests build ``Settings(...)`` directly or clear the cache."""
    return Settings()

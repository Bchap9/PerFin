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
import uuid
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
    # "Remember this device": absolute lifetime of such sessions (step-up
    # actions still need a fresh MFA verification).
    session_remember_days: Annotated[int, Field(ge=1, le=90)] = 30
    # Cookies carry ``Secure`` (and the ``__Host-`` prefix). Turning this off
    # is refused by ``perfin config check`` outside dev mode.
    secure_cookies: bool = True
    require_mfa: bool = True
    # Personal access tokens: the longest expiry a user may choose (days).
    # Every token expires; the hard cap is 365 days (SECURITY.md I3).
    token_max_days: Annotated[int, Field(ge=1, le=365)] = 90

    # ---- Local login / break-glass -----------------------------------------
    local_login_enabled: bool = True
    breakglass_cidrs: Annotated[list[IPNetwork], NoDecode] = Field(
        default_factory=lambda: [ip_network("127.0.0.1/32")]
    )
    # Opt-in k-anonymity range check against Have I Been Pwned for new
    # passwords (an off-host call to api.pwnedpasswords.com; the local
    # breached-password list is always checked).
    hibp_check_enabled: bool = False
    # Where the API writes the one-time setup URL while no user exists
    # (AUTH.md "Admin bootstrap"); mode 0600, removed once setup completes.
    bootstrap_url_file: Path = Path("/var/lib/perfin/documents/.perfin-setup/setup-url")

    # ---- SMTP (password reset; notification channels later) --------------
    # Password reset is offered only when host and from address are set.
    # TLS is always required: STARTTLS (587) or implicit TLS (465).
    smtp_host: str = ""
    smtp_port: Annotated[int, Field(ge=1, le=65535)] = 587
    smtp_tls: Literal["starttls", "implicit"] = "starttls"
    smtp_username: str = ""
    smtp_password: SecretStr | None = None
    smtp_from: str = ""
    smtp_timeout_seconds: Annotated[int, Field(ge=1, le=120)] = 10

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
    # Treat the IdP's ``amr`` claim (``mfa``, or factors of two kinds) as
    # satisfied MFA (app.auth.mfa.policy). Only enable when the IdP enforces MFA.
    oidc_trust_idp_mfa: bool = False
    # Client authentication at the token endpoint (AUTH.md "OIDC"):
    # client_secret_basic (PERFIN_OIDC_CLIENT_SECRET) or private_key_jwt
    # (PEM private key in PERFIN_OIDC_PRIVATE_KEY_FILE, RSA or EC P-256).
    oidc_token_auth_method: Literal["client_secret_basic", "private_key_jwt"] = (
        "client_secret_basic"  # noqa: S105 - a method name, not a secret
    )
    oidc_private_key_file: Path | None = None
    oidc_private_key_id: str = ""
    # Offer to link an IdP identity to the local account with the same
    # verified email after a local password (+MFA) login (B3).
    oidc_link_by_verified_email: bool = False
    # The household that group-mapped roles apply to; unset means the
    # instance's only household (none is changed when there are several).
    oidc_household_id: uuid.UUID | None = None
    # Allowed post_logout_redirect_uri values for RP-initiated logout (B4);
    # empty means "<PERFIN_PUBLIC_URL>/".
    oidc_post_logout_redirect_uris: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Periodic claim refresh with the refresh token (B8); 0 disables it and
    # no refresh token is kept.
    oidc_claim_refresh_minutes: Annotated[int, Field(ge=0, le=1440)] = 0
    # JWKS refetch on an unknown ``kid`` at most this often (B9).
    oidc_jwks_min_refresh_seconds: Annotated[int, Field(ge=10, le=3600)] = 300
    # Maximum lifetime of the cached discovery document and JWKS.
    oidc_metadata_max_age_seconds: Annotated[int, Field(ge=60, le=86400)] = 3600
    oidc_http_timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 10
    # OIDC-only mode: the username of the break-glass local admin the operator
    # set up (``perfin config check`` requires it when local login is off).
    breakglass_admin_username: str = ""

    # ---- Intake -------------------------------------------------------------
    upload_max_mb: Annotated[int, Field(ge=1, le=1024)] = 50
    # Drop folder (INGESTION.md "Drop folder specifics",
    # app.ingestion.intake.dropfolder): the worker watches the inbox and
    # copies settled files into quarantine.
    dropfolder_enabled: bool = False
    dropfolder_inbox: Path = Path("./inbox")
    dropfolder_processed: Path = Path("./processed")
    # Where refused files go in "move" mode; unset means <processed>/failed.
    dropfolder_failed: Path | None = None
    # A file is complete when it has not changed for this long (C11).
    dropfolder_settle_seconds: Annotated[int, Field(ge=0, le=3600)] = 10
    # Also require a "<name>.ready" sidecar before a file is taken (C11).
    dropfolder_ready_sidecar: bool = False
    # move | delete need a writable inbox; with a read-only inbox they act
    # like keep (processed files are remembered by path, size, mtime, hash).
    dropfolder_after: Literal["move", "delete", "keep"] = "move"
    # Per-run caps (C10): files taken and bytes copied per watcher run.
    dropfolder_max_files_per_run: Annotated[int, Field(ge=1, le=10000)] = 100
    dropfolder_max_mb_per_run: Annotated[int, Field(ge=1, le=102400)] = 1024
    # How long one watcher run keeps watching (inotify, else polling) for
    # files still settling; 0 means a single pass. Runs start every minute.
    dropfolder_watch_seconds: Annotated[int, Field(ge=0, le=50)] = 45
    # The household drop-folder files belong to; unset means the instance's
    # only household (the watcher does nothing while there are several).
    dropfolder_household_id: uuid.UUID | None = None
    # Email intake (INGESTION.md "Email intake specifics",
    # app.ingestion.intake.email): with it on, the worker polls each
    # household's configured mailbox over IMAPS (port 993, implicit TLS,
    # certificate verified; through HTTPS_PROXY when that names a proxy).
    email_intake_enabled: bool = False
    # Per-run caps (C10): new messages taken and bytes fetched per poll.
    email_intake_max_messages_per_run: Annotated[int, Field(ge=1, le=1000)] = 50
    email_intake_max_mb_per_run: Annotated[int, Field(ge=1, le=10240)] = 200
    # Largest message fetched (also capped by upload_max_mb).
    email_intake_max_message_mb: Annotated[int, Field(ge=1, le=1024)] = 25
    email_intake_timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 30
    # Allow IMAP servers on private (RFC 1918, ULA) addresses for a direct
    # connection; without a proxy every other address must be public.
    email_intake_allow_private_hosts: bool = False

    # ---- Document store and extraction sandbox -----------------------------
    # Encrypted blobs (ADR-0003): processed documents and the quarantine area
    # for files not yet processed (separate volumes in docker-compose.yml).
    documents_dir: Path = Path("/var/lib/perfin/documents")
    quarantine_dir: Path = Path("/var/lib/perfin/quarantine")
    # Limits of the extraction subprocess (ADR-0006, SECURITY.md C1/C2).
    extract_memory_mb: Annotated[int, Field(ge=64, le=16384)] = 1024
    extract_cpu_seconds: Annotated[int, Field(ge=1, le=3600)] = 60
    extract_timeout_seconds: Annotated[int, Field(ge=1, le=600)] = 120
    extract_max_output_mb: Annotated[int, Field(ge=1, le=1024)] = 64
    # Numeric UID (and GID) the extraction child switches to when the worker
    # runs as root; ignored otherwise (the image already runs as 10001).
    extract_uid: Annotated[int, Field(ge=1, le=2**31 - 1)] | None = None
    # The child refuses to run without Landlock (SECURITY.md C1). Set to false
    # only on a kernel without Landlock: seccomp and the worker's non-dumpable
    # flag are then the only layers between a parser exploit and the files
    # the worker's UID can read.
    extract_require_landlock: bool = True
    # Document parsers (INGESTION.md "Commit", SECURITY.md C12): a parsed field
    # below this confidence is imported with review_state=pending and a
    # low_confidence_field review item. Never below 0.5 (a guess would pass).
    ingest_confidence_threshold: Annotated[float, Field(ge=0.5, le=1.0)] = 0.85

    # ---- Full export and import (docs/PORTABILITY.md) -----------------------
    # A finished export can be downloaded once within this many minutes; then
    # its encrypted archive is destroyed.
    export_ttl_minutes: Annotated[int, Field(ge=5, le=1440)] = 60
    # Largest archive accepted by POST /import (held in the worker's memory
    # while it is validated and applied).
    import_max_mb: Annotated[int, Field(ge=1, le=4096)] = 512

    # ---- Live updates (Server-Sent Events, ``GET /events``) -----------------
    # Comment line sent when nothing else was, so proxies keep the stream open.
    sse_heartbeat_seconds: Annotated[float, Field(gt=0, le=300)] = 15
    # A stream ends after this long; the browser reconnects with Last-Event-ID.
    sse_max_stream_seconds: Annotated[float, Field(gt=0, le=86400)] = 600
    # How often a stream re-checks its session, role and account grants.
    sse_revalidate_seconds: Annotated[float, Field(gt=0, le=3600)] = 30
    # Concurrent streams per user (per API process); more are 429.
    sse_max_streams_per_user: Annotated[int, Field(ge=1, le=100)] = 5

    # ---- Integrations -------------------------------------------------------
    price_provider: str = "none"
    antivirus: Literal["none", "clamav"] = "none"
    # clamd on the internal network: tcp://host:port or unix:///path (SECURITY.md C9).
    clamav_address: str = "tcp://clamav:3310"
    clamav_timeout_seconds: Annotated[int, Field(ge=1, le=600)] = 30
    # Largest document streamed to clamd (keep clamd's StreamMaxLength at least this).
    clamav_max_mb: Annotated[int, Field(ge=1, le=4096)] = 50
    # What a document does when the scan cannot run (scanner down, too large):
    # "closed" stops it (retried, then failed), "open" goes on unscanned.
    antivirus_fail_mode: Literal["closed", "open"] = "closed"

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

    @field_validator("mcp_http_allowed_origins", "oidc_post_logout_redirect_uris", mode="before")
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

    @field_validator(
        "master_key", "secret_key", "oidc_client_secret", "smtp_password", mode="before"
    )
    @classmethod
    def _empty_secret_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator(
        "migration_database_url",
        "oidc_household_id",
        "oidc_private_key_file",
        "extract_uid",
        "dropfolder_failed",
        "dropfolder_household_id",
        mode="before",
    )
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
    def oidc_redirect_uri(self) -> str:
        """The fixed OIDC redirect URI (never taken from a request)."""
        return self.public_url.rstrip("/") + "/api/v1/auth/oidc/callback"

    @property
    def effective_post_logout_redirect_uris(self) -> tuple[str, ...]:
        """The RP-initiated logout allowlist (default ``<PERFIN_PUBLIC_URL>/``)."""
        uris = tuple(u for u in self.oidc_post_logout_redirect_uris if u)
        return uris or (self.public_url.rstrip("/") + "/",)

    @property
    def smtp_configured(self) -> bool:
        """An SMTP relay and sender are set (password reset is offered)."""
        return bool(self.smtp_host.strip() and self.smtp_from.strip())

    @property
    def upload_max_bytes(self) -> int:
        return self.upload_max_mb * 1024 * 1024

    @property
    def import_max_bytes(self) -> int:
        return self.import_max_mb * 1024 * 1024

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

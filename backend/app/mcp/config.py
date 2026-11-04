# SPDX-License-Identifier: AGPL-3.0-or-later
"""Configuration of the ``mcp`` role (AI_ACCESS.md "Architecture", M7).

The MCP process is a protocol server that holds **one** scoped API token
(``ai:read``, minted by the AI-client flow) and nothing else. It is configured
from ``PERFIN_MCP_*`` variables only and never reads
``app.core.settings.Settings`` (which knows the database URL and the master
key):

=================================  ==========================================
``PERFIN_MCP_ENABLED``             must be ``true`` (the operator's switch;
                                   anything else refuses to start)
``PERFIN_MCP_API_URL``             base URL of the PerFin API (``http://api:8000``
                                   inside the compose stack); http(s) only,
                                   no credentials, query or fragment
``PERFIN_MCP_TOKEN`` /             the AI client's ``ai:read`` token (exactly
``PERFIN_MCP_TOKEN_FILE``          one of the two)
``PERFIN_MCP_TRANSPORT``           ``stdio`` (default) or ``http``
``PERFIN_MCP_HTTP_HOST`` /         where the streamable-HTTP transport listens
``PERFIN_MCP_HTTP_PORT``           (``127.0.0.1:8001`` by default)
``PERFIN_MCP_HTTP_ALLOWED_ORIGINS``  explicit ``https`` origins (comma
                                   separated) a browser-borne request may come
                                   from; required for ``http``
``PERFIN_MCP_HTTP_TOKEN`` /        the bearer secret the MCP *client* (the
``PERFIN_MCP_HTTP_TOKEN_FILE``     assistant host) must send on the HTTP
                                   transport; at least 32 characters and
                                   different from the API token; required
                                   for ``http``
``PERFIN_MCP_RATE_PER_MINUTE``     tool calls per minute this process accepts
                                   (default 30, at most 600)
``PERFIN_MCP_API_TIMEOUT_SECONDS`` per-request timeout towards the API
                                   (default 30)
``PERFIN_LOG_LEVEL``               as for every role
``PERFIN_DEV_MODE``                ``true`` allows ``http://`` origins
=================================  ==========================================

**M7 defence.** The role refuses to start when its environment carries
anything that would let it reach data around the API: a database URL, the
master key, the secret key or another service credential
(:data:`FORBIDDEN_ENV`, :data:`FORBIDDEN_ENV_RE`). The compose service sets
only the variables above; a hand-run ``perfin mcp`` from a shell that sourced
the full ``.env`` is refused instead of silently holding the keys.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlsplit

Level = Literal["error", "warning"]
Transport = Literal["stdio", "http"]

#: Exact variable names the MCP process must never receive (M7).
FORBIDDEN_ENV: Final = frozenset(
    {
        "PERFIN_DATABASE_URL",
        "PERFIN_MIGRATION_DATABASE_URL",
        "PERFIN_MASTER_KEY",
        "PERFIN_MASTER_KEY_FILE",
        "PERFIN_SECRET_KEY",
        "PERFIN_OIDC_CLIENT_SECRET",
        "PERFIN_SMTP_PASSWORD",
        "PERFIN_PRICE_PROVIDER_API_KEY",
        "POSTGRES_PASSWORD",
        "POSTGRES_OWNER_PASSWORD",
        "POSTGRES_APP_PASSWORD",
        "DATABASE_URL",
        "PGPASSWORD",
    }
)
#: Any other ``PERFIN_*`` / ``POSTGRES_*`` variable naming a database URL,
#: a key or a password is refused too (a renamed or future secret).
FORBIDDEN_ENV_RE: Final = re.compile(
    r"^(PERFIN|POSTGRES)_\w*(DATABASE_URL|MASTER_KEY|SECRET_KEY|PASSWORD|_KEY)$"
)

DEFAULT_RATE_PER_MINUTE: Final = 30
MAX_RATE_PER_MINUTE: Final = 600
DEFAULT_HTTP_PORT: Final = 8001
MIN_HTTP_TOKEN_LENGTH: Final = 32
_TRUE: Final = frozenset({"1", "true", "yes", "on"})
_FALSE: Final = frozenset({"0", "false", "no", "off", ""})


@dataclass(frozen=True, slots=True)
class Finding:
    """One configuration problem (same shape as ``app.core.config_check.Finding``)."""

    level: Level
    code: str
    message: str

    def render(self) -> str:
        return f"{self.level.upper()} {self.code}: {self.message}"


class McpConfigError(RuntimeError):
    """The ``mcp`` role must not start with this environment."""

    def __init__(self, findings: list[Finding]) -> None:
        self.findings = findings
        errors = [f for f in findings if f.level == "error"]
        super().__init__(
            f"refusing to start: {len(errors)} configuration error(s): "
            + "; ".join(f.code for f in errors)
        )


@dataclass(frozen=True, slots=True)
class McpConfig:
    """The validated configuration of one MCP process."""

    api_url: str
    token: str = field(repr=False)
    transport: Transport = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int = DEFAULT_HTTP_PORT
    http_allowed_origins: tuple[str, ...] = ()
    http_token: str | None = field(default=None, repr=False)
    rate_per_minute: int = DEFAULT_RATE_PER_MINUTE
    api_timeout_seconds: float = 30.0
    log_level: str = "INFO"


def _flag(value: str | None) -> bool | None:
    if value is None:
        return None
    clean = value.strip().lower()
    if clean in _TRUE:
        return True
    if clean in _FALSE:
        return False
    return None


def _secret(env: Mapping[str, str], name: str, out: list[Finding], *, required: bool) -> str | None:
    """``NAME`` or the content of ``NAME_FILE`` (exactly one of them)."""
    direct = env.get(name, "").strip()
    path = env.get(f"{name}_FILE", "").strip()
    if direct and path:
        out.append(Finding("error", "mcp.secret_ambiguous", f"set {name} or {name}_FILE, not both"))
        return None
    if path:
        try:
            file = Path(path)
            mode = file.stat().st_mode
            value = file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            out.append(
                Finding(
                    "error",
                    "mcp.secret_unreadable",
                    f"{name}_FILE cannot be read ({type(exc).__name__})",
                )
            )
            return None
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            out.append(
                Finding(
                    "warning",
                    "mcp.secret_file_mode",
                    f"{name}_FILE is readable by group or others; use mode 0600",
                )
            )
        direct = value
    if not direct:
        if required:
            out.append(Finding("error", "mcp.secret_missing", f"{name} or {name}_FILE is required"))
        return None
    if any(ch.isspace() for ch in direct) or not direct.isprintable():
        out.append(Finding("error", "mcp.secret_malformed", f"{name} contains whitespace"))
        return None
    return direct


def _check_forbidden(env: Mapping[str, str], out: list[Finding]) -> None:
    for name in sorted(env):
        if not env[name].strip():
            continue
        if name in FORBIDDEN_ENV or FORBIDDEN_ENV_RE.match(name):
            out.append(
                Finding(
                    "error",
                    "mcp.forbidden_secret",
                    f"{name} is set: the mcp role holds only its API token and must not "
                    "receive database URLs, keys or service credentials (AI_ACCESS.md M7)",
                )
            )


def _check_api_url(raw: str, out: list[Finding]) -> str | None:
    url = raw.strip()
    if not url:
        out.append(Finding("error", "mcp.api_url_missing", "PERFIN_MCP_API_URL is required"))
        return None
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        out.append(
            Finding(
                "error",
                "mcp.api_url_invalid",
                "PERFIN_MCP_API_URL must be an http(s) URL without credentials, query or fragment",
            )
        )
        return None
    return url.rstrip("/")


def _check_origins(raw: str, dev_mode: bool, out: list[Finding]) -> tuple[str, ...]:
    origins = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not origins:
        out.append(
            Finding(
                "error",
                "mcp.origins_missing",
                "PERFIN_MCP_TRANSPORT=http requires PERFIN_MCP_HTTP_ALLOWED_ORIGINS",
            )
        )
        return ()
    for origin in origins:
        parts = urlsplit(origin)
        if origin in ("*", "null") or "*" in origin:
            out.append(
                Finding(
                    "error",
                    "mcp.origins_wildcard",
                    "PERFIN_MCP_HTTP_ALLOWED_ORIGINS must list explicit origins, not * or null",
                )
            )
        elif (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
            or parts.username is not None
        ):
            out.append(
                Finding(
                    "error",
                    "mcp.origin_invalid",
                    f"PERFIN_MCP_HTTP_ALLOWED_ORIGINS entry {origin!r} is not an origin "
                    "(scheme://host[:port])",
                )
            )
        elif parts.scheme != "https" and not dev_mode:
            out.append(
                Finding(
                    "error",
                    "mcp.origin_not_https",
                    f"PERFIN_MCP_HTTP_ALLOWED_ORIGINS entry {origin!r} is not https "
                    "(the HTTP transport is served only through the TLS proxy)",
                )
            )
    return tuple(o.rstrip("/") for o in origins)


def _int(
    env: Mapping[str, str], name: str, default: int, lo: int, hi: int, out: list[Finding]
) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = lo - 1
    if not lo <= value <= hi:
        out.append(Finding("error", "mcp.invalid_number", f"{name} must be in {lo}..{hi}"))
        return default
    return value


def check(env: Mapping[str, str] | None = None) -> tuple[McpConfig | None, list[Finding]]:
    """Validate the ``mcp`` role's environment.

    Returns the configuration (``None`` when any error was found) and every
    finding, so ``perfin config check`` can explain all problems at once.
    """
    env = dict(os.environ if env is None else env)
    out: list[Finding] = []
    _check_forbidden(env, out)
    dev_mode = _flag(env.get("PERFIN_DEV_MODE")) is True

    enabled = _flag(env.get("PERFIN_MCP_ENABLED"))
    if enabled is not True:
        out.append(
            Finding(
                "error",
                "mcp.disabled",
                "the MCP role is switched off (set PERFIN_MCP_ENABLED=true to serve it)",
            )
        )
    api_url = _check_api_url(env.get("PERFIN_MCP_API_URL", ""), out)
    token = _secret(env, "PERFIN_MCP_TOKEN", out, required=True)

    transport_raw = env.get("PERFIN_MCP_TRANSPORT", "stdio").strip().lower() or "stdio"
    transport: Transport = "stdio"
    if transport_raw == "http":
        transport = "http"
    elif transport_raw != "stdio":
        out.append(
            Finding("error", "mcp.transport_invalid", "PERFIN_MCP_TRANSPORT must be stdio or http")
        )

    origins: tuple[str, ...] = ()
    http_token: str | None = None
    http_host = env.get("PERFIN_MCP_HTTP_HOST", "").strip() or "127.0.0.1"
    http_port = _int(env, "PERFIN_MCP_HTTP_PORT", DEFAULT_HTTP_PORT, 1, 65535, out)
    if transport == "http":
        origins = _check_origins(env.get("PERFIN_MCP_HTTP_ALLOWED_ORIGINS", ""), dev_mode, out)
        http_token = _secret(env, "PERFIN_MCP_HTTP_TOKEN", out, required=True)
        if http_token is not None and len(http_token) < MIN_HTTP_TOKEN_LENGTH:
            out.append(
                Finding(
                    "error",
                    "mcp.http_token_weak",
                    f"PERFIN_MCP_HTTP_TOKEN must be at least {MIN_HTTP_TOKEN_LENGTH} characters",
                )
            )
        if http_token is not None and token is not None and http_token == token:
            out.append(
                Finding(
                    "error",
                    "mcp.http_token_reused",
                    "PERFIN_MCP_HTTP_TOKEN must differ from the API token PERFIN_MCP_TOKEN",
                )
            )
    rate = _int(
        env, "PERFIN_MCP_RATE_PER_MINUTE", DEFAULT_RATE_PER_MINUTE, 1, MAX_RATE_PER_MINUTE, out
    )
    timeout = _int(env, "PERFIN_MCP_API_TIMEOUT_SECONDS", 30, 1, 300, out)
    log_level = env.get("PERFIN_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        out.append(Finding("error", "mcp.log_level_invalid", "PERFIN_LOG_LEVEL is not a level"))
        log_level = "INFO"
    if dev_mode:
        out.append(Finding("warning", "mcp.dev_mode", "PERFIN_DEV_MODE is on"))

    if any(f.level == "error" for f in out) or api_url is None or token is None:
        return None, out
    return (
        McpConfig(
            api_url=api_url,
            token=token,
            transport=transport,
            http_host=http_host,
            http_port=http_port,
            http_allowed_origins=origins,
            http_token=http_token,
            rate_per_minute=rate,
            api_timeout_seconds=float(timeout),
            log_level=log_level,
        ),
        out,
    )


def load(env: Mapping[str, str] | None = None) -> McpConfig:
    """:func:`check`, raising :class:`McpConfigError` on any error."""
    config, findings = check(env)
    if config is None:
        raise McpConfigError(findings)
    return config

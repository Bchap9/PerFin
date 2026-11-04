# SPDX-License-Identifier: AGPL-3.0-or-later
"""Insecure-configuration checks (SECURITY.md H6, F5, G2, G4, B4, B5, B7).

``check_config(settings)`` returns every problem at once as ``Finding``
objects. Outside ``PERFIN_DEV_MODE`` an ``error`` finding means the process
must not start: ``perfin config check`` exits non-zero and the ``api``,
``worker`` and ``mcp`` roles call ``enforce_config()`` before serving
anything. In dev mode the same problems are reported as warnings, plus one
loud warning that dev mode is on.

Messages never contain a secret value, only the name of the setting.
"""

from __future__ import annotations

import base64
import binascii
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.core.settings import MASTER_KEY_BYTES, MasterKeyError, Settings

Level = Literal["error", "warning"]

#: Minimum entropy of ``PERFIN_SECRET_KEY`` in bytes (H6).
SECRET_KEY_MIN_BYTES = 32

#: Placeholder or well-known values that must never reach production.
PLACEHOLDER_SECRETS = frozenset(
    {
        "change_me",
        "changeme",
        "change-me",
        "secret",
        "password",
        "default",
        "perfin",
        "postgres",
        "admin",
        "test",
        "example",
        "insecure",
        "dev",
        "development",
    }
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One configuration problem. ``code`` is stable for tests and docs."""

    level: Level
    code: str
    message: str

    def render(self) -> str:
        return f"{self.level.upper()} {self.code}: {self.message}"


class InsecureConfigError(RuntimeError):
    """Raised by ``enforce_config`` when the configuration must not start."""

    def __init__(self, findings: list[Finding]) -> None:
        self.findings = findings
        errors = [f for f in findings if f.level == "error"]
        super().__init__(
            f"refusing to start: {len(errors)} insecure configuration error(s); "
            "run `perfin config check` for details"
        )


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in PLACEHOLDER_SECRETS or "change_me" in normalized


#: A random secret of the required length uses far more distinct characters.
SECRET_KEY_MIN_DISTINCT_CHARS = 12


def _decoded_entropy_bytes(value: str) -> int:
    """Best estimate of how many random bytes ``value`` encodes.

    Hex and base64 (standard or URL-safe) strings count their decoded length;
    anything else counts its UTF-8 length. Values with very few distinct
    characters (``"ab" * 40``) count as zero whatever their length.
    """
    raw = value.strip()
    if len(set(raw)) < SECRET_KEY_MIN_DISTINCT_CHARS:
        return 0
    if len(raw) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in raw):
        return len(raw) // 2
    padded = (raw + "=" * (-len(raw) % 4)).encode("utf-8")
    for altchars in (None, b"-_"):
        try:
            return len(base64.b64decode(padded, altchars=altchars, validate=True))
        except (binascii.Error, ValueError):
            continue
    return len(raw.encode("utf-8"))


def _https(url: str) -> bool:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname)


def _any_network(networks: Iterable[IPv4Network | IPv6Network]) -> bool:
    return any(net.prefixlen == 0 for net in networks)


def _check_master_key(settings: Settings, out: list[Finding]) -> None:
    if settings.master_key is None and settings.master_key_file is None:
        out.append(
            Finding(
                "error",
                "master_key.missing",
                "set PERFIN_MASTER_KEY_FILE (preferred) or PERFIN_MASTER_KEY to "
                f"base64 of {MASTER_KEY_BYTES} random bytes (openssl rand -base64 32)",
            )
        )
        return
    if settings.master_key is not None and _is_placeholder(settings.master_key.get_secret_value()):
        out.append(
            Finding(
                "error",
                "master_key.placeholder",
                "PERFIN_MASTER_KEY still holds the CHANGE_ME placeholder",
            )
        )
        return
    if settings.master_key_file is not None:
        try:
            mode = settings.master_key_file.stat().st_mode
        except OSError:
            mode = None
        if mode is not None:
            if mode & stat.S_IROTH or mode & stat.S_IWOTH:
                out.append(
                    Finding(
                        "error",
                        "master_key.file_world_readable",
                        "PERFIN_MASTER_KEY_FILE is readable or writable by other users; "
                        "chmod 0400 (or 0440 for a dedicated group)",
                    )
                )
            elif mode & stat.S_IRGRP or mode & stat.S_IWGRP:
                out.append(
                    Finding(
                        "warning",
                        "master_key.file_group_readable",
                        "PERFIN_MASTER_KEY_FILE is group-accessible; make sure the group "
                        "contains only the PerFin service user, or chmod 0400",
                    )
                )
    try:
        settings.load_master_key()
    except MasterKeyError as exc:
        # MasterKeyError messages never echo the key material.
        out.append(Finding("error", "master_key.invalid", str(exc)))


def _check_secret_key(settings: Settings, out: list[Finding]) -> None:
    if settings.secret_key is None:
        out.append(
            Finding(
                "error",
                "secret_key.missing",
                "set PERFIN_SECRET_KEY (openssl rand -base64 48)",
            )
        )
        return
    value = settings.secret_key.get_secret_value()
    if _is_placeholder(value):
        out.append(
            Finding(
                "error",
                "secret_key.placeholder",
                "PERFIN_SECRET_KEY still holds a placeholder or well-known value",
            )
        )
    elif _decoded_entropy_bytes(value) < SECRET_KEY_MIN_BYTES:
        out.append(
            Finding(
                "error",
                "secret_key.weak",
                f"PERFIN_SECRET_KEY must carry at least {SECRET_KEY_MIN_BYTES} random "
                "bytes (openssl rand -base64 48)",
            )
        )


def _check_database_url(name: str, url_value: str, out: list[Finding]) -> None:
    try:
        url = make_url(url_value)
    except ArgumentError:
        out.append(Finding("error", f"{name}.invalid", f"PERFIN_{name.upper()} is not a valid URL"))
        return
    password = url.password
    if not password:
        if url.host not in (None, "", "localhost", "127.0.0.1", "::1"):
            out.append(
                Finding(
                    "warning",
                    f"{name}.no_password",
                    f"PERFIN_{name.upper()} has no password for a network host; make sure "
                    "the database requires authentication",
                )
            )
        return
    if _is_placeholder(str(password)) or "${" in str(password):
        out.append(
            Finding(
                "error",
                f"{name}.default_password",
                f"PERFIN_{name.upper()} uses a default or placeholder password",
            )
        )


def _plain_url(url: str) -> bool:
    """An absolute http(s) URL without userinfo, query or fragment."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and "@" not in parts.netloc
        and not parts.query
        and not parts.fragment
    )


def _check_oidc_client_auth(settings: Settings, out: list[Finding]) -> None:
    client_auth = settings.oidc_token_auth_method
    if client_auth == "private_key_jwt":
        path = settings.oidc_private_key_file
        if path is None:
            out.append(
                Finding(
                    "error",
                    "oidc.private_key_missing",
                    "PERFIN_OIDC_TOKEN_AUTH_METHOD=private_key_jwt needs "
                    "PERFIN_OIDC_PRIVATE_KEY_FILE",
                )
            )
            return
        try:
            mode = path.stat().st_mode
        except OSError:
            out.append(
                Finding(
                    "error",
                    "oidc.private_key_unreadable",
                    "PERFIN_OIDC_PRIVATE_KEY_FILE does not exist or is not readable",
                )
            )
            return
        if mode & stat.S_IROTH or mode & stat.S_IWOTH:
            out.append(
                Finding(
                    "error",
                    "oidc.private_key_world_readable",
                    "PERFIN_OIDC_PRIVATE_KEY_FILE is readable or writable by other users; "
                    "chmod 0400",
                )
            )
        return
    secret = settings.oidc_client_secret
    if secret is None or not secret.get_secret_value().strip():
        out.append(
            Finding(
                "error",
                "oidc.client_secret_missing",
                "OIDC is enabled but PERFIN_OIDC_CLIENT_SECRET is empty",
            )
        )
    elif _is_placeholder(secret.get_secret_value()):
        out.append(
            Finding(
                "error",
                "oidc.client_secret_placeholder",
                "PERFIN_OIDC_CLIENT_SECRET still holds a placeholder value",
            )
        )


def _check_oidc(settings: Settings, out: list[Finding]) -> None:
    """OIDC client, logout allowlist, OIDC-only mode (SECURITY.md B4, B5, B7)."""
    if settings.oidc_enabled:
        if not _https(settings.oidc_issuer):
            out.append(
                Finding(
                    "error",
                    "oidc.issuer_not_https",
                    "PERFIN_OIDC_ISSUER must be an https:// URL",
                )
            )
        elif not _plain_url(settings.oidc_issuer):
            out.append(
                Finding(
                    "error",
                    "oidc.issuer_invalid",
                    "PERFIN_OIDC_ISSUER must be a plain URL (no credentials, query or fragment)",
                )
            )
        if not settings.oidc_client_id.strip():
            out.append(
                Finding(
                    "error",
                    "oidc.client_id_missing",
                    "OIDC is enabled but PERFIN_OIDC_CLIENT_ID is empty",
                )
            )
        _check_oidc_client_auth(settings, out)
        for uri in settings.oidc_post_logout_redirect_uris:
            if not _https(uri) or not _plain_url(uri):
                out.append(
                    Finding(
                        "error",
                        "oidc.post_logout_uri_invalid",
                        "every PERFIN_OIDC_POST_LOGOUT_REDIRECT_URIS entry must be an "
                        "absolute https:// URL without query or fragment",
                    )
                )
                break
        if settings.oidc_jit_provisioning and not settings.oidc_require_group:
            out.append(
                Finding(
                    "warning",
                    "oidc.jit_without_group",
                    "PERFIN_OIDC_JIT_PROVISIONING=true with PERFIN_OIDC_REQUIRE_GROUP=false: "
                    "every user of the identity provider can create a PerFin account",
                )
            )
    elif settings.oidc_claim_refresh_minutes > 0:
        out.append(
            Finding(
                "warning",
                "oidc.claim_refresh_without_oidc",
                "PERFIN_OIDC_CLAIM_REFRESH_MINUTES is set but OIDC is not configured",
            )
        )
    if settings.oidc_allow_private_issuer:
        out.append(
            Finding(
                "warning",
                "oidc.private_issuer_allowed",
                "PERFIN_OIDC_ALLOW_PRIVATE_ISSUER=true: the OIDC issuer may resolve to a "
                "private address (allowed for a home-LAN identity provider; never "
                "link-local or metadata addresses, loopback only in dev mode)",
            )
        )
    if not settings.local_login_enabled:
        _check_oidc_only(settings, out)


def _check_oidc_only(settings: Settings, out: list[Finding]) -> None:
    """OIDC-only mode needs a working IdP client and a break-glass admin (B7)."""
    if not settings.oidc_enabled:
        out.append(
            Finding(
                "error",
                "auth.lockout",
                "PERFIN_LOCAL_LOGIN_ENABLED=false but OIDC is not configured: nobody could log in",
            )
        )
    elif not settings.oidc_client_id.strip():
        out.append(
            Finding(
                "error",
                "auth.oidc_only_incomplete",
                "PERFIN_LOCAL_LOGIN_ENABLED=false needs PERFIN_OIDC_ISSUER and "
                "PERFIN_OIDC_CLIENT_ID configured",
            )
        )
    if not settings.breakglass_admin_username.strip():
        out.append(
            Finding(
                "error",
                "breakglass.admin_missing",
                "PERFIN_LOCAL_LOGIN_ENABLED=false needs a break-glass local admin: create one "
                "and name it in PERFIN_BREAKGLASS_ADMIN_USERNAME",
            )
        )
    if not settings.breakglass_cidrs:
        out.append(
            Finding(
                "error",
                "breakglass.no_cidrs",
                "PERFIN_LOCAL_LOGIN_ENABLED=false needs PERFIN_BREAKGLASS_CIDRS (the networks "
                "the break-glass admin may log in from)",
            )
        )


def _check_network(settings: Settings, out: list[Finding]) -> None:
    if _any_network(settings.breakglass_cidrs):
        out.append(
            Finding(
                "error",
                "breakglass.any_address",
                "PERFIN_BREAKGLASS_CIDRS must not contain 0.0.0.0/0 or ::/0",
            )
        )
    if _any_network(settings.trusted_proxies):
        out.append(
            Finding(
                "error",
                "trusted_proxies.any_address",
                "PERFIN_TRUSTED_PROXIES must not contain 0.0.0.0/0 or ::/0 (any client could "
                "spoof X-Forwarded-For)",
            )
        )


def _binds_every_interface(host: str) -> bool:
    value = host.strip().strip("[]")
    if not value:
        return True
    try:
        return ip_address(value).is_unspecified
    except ValueError:
        return False


def _check_metrics(settings: Settings, out: list[Finding]) -> None:
    if _binds_every_interface(settings.metrics_bind_host):
        out.append(
            Finding(
                "warning",
                "metrics.bind_all_interfaces",
                "PERFIN_METRICS_BIND_HOST listens on every interface: the metrics port is "
                "reachable from every container network (including the proxy's); bind it to "
                "127.0.0.1 or the backend-network address of the container",
            )
        )


def _check_mcp(settings: Settings, out: list[Finding]) -> None:
    if settings.mcp_transport != "http":
        return
    origins = settings.mcp_http_allowed_origins
    if not origins:
        out.append(
            Finding(
                "error",
                "mcp.origins_missing",
                "PERFIN_MCP_TRANSPORT=http requires PERFIN_MCP_HTTP_ALLOWED_ORIGINS",
            )
        )
    elif any(origin.strip() in ("*", "null") for origin in origins):
        out.append(
            Finding(
                "error",
                "mcp.origins_wildcard",
                "PERFIN_MCP_HTTP_ALLOWED_ORIGINS must list explicit origins, not * or null",
            )
        )


def _check_smtp(settings: Settings, out: list[Finding]) -> None:
    """SMTP (password reset, A11) and the opt-in HIBP check."""
    host = settings.smtp_host.strip()
    sender = settings.smtp_from.strip()
    if host and not sender:
        out.append(
            Finding(
                "warning",
                "smtp.from_missing",
                "PERFIN_SMTP_HOST is set but PERFIN_SMTP_FROM is empty: password reset "
                "stays disabled",
            )
        )
    if sender and not host:
        out.append(
            Finding(
                "warning",
                "smtp.host_missing",
                "PERFIN_SMTP_FROM is set but PERFIN_SMTP_HOST is empty: password reset "
                "stays disabled",
            )
        )
    if sender and ("@" not in sender or any(ch in sender for ch in "\r\n")):
        out.append(
            Finding(
                "warning",
                "smtp.from_invalid",
                "PERFIN_SMTP_FROM is not a single email address: password reset mail "
                "will be refused",
            )
        )
    has_user = bool(settings.smtp_username.strip())
    has_password = settings.smtp_password is not None and bool(
        settings.smtp_password.get_secret_value()
    )
    if has_user != has_password:
        out.append(
            Finding(
                "warning",
                "smtp.credentials_incomplete",
                "set both PERFIN_SMTP_USERNAME and PERFIN_SMTP_PASSWORD, or neither",
            )
        )
    if settings.smtp_configured and not settings.local_login_enabled:
        out.append(
            Finding(
                "warning",
                "smtp.reset_disabled_oidc_only",
                "PERFIN_LOCAL_LOGIN_ENABLED=false: password reset is disabled even though "
                "SMTP is configured (OIDC-only mode)",
            )
        )
    if settings.hibp_check_enabled:
        out.append(
            Finding(
                "warning",
                "hibp.enabled",
                "PERFIN_HIBP_CHECK_ENABLED=true: new passwords are checked against "
                "api.pwnedpasswords.com (k-anonymity, an off-host call); the API needs "
                "egress to that host",
            )
        )


def _dir_writable(path: Path) -> bool:
    try:
        if os.statvfs(path).f_flag & os.ST_RDONLY:
            return False
    except OSError:
        return False
    return os.access(path, os.W_OK)


def _inside(child: Path, parent: Path) -> bool:
    child, parent = child.resolve(), parent.resolve()
    return child == parent or parent in child.parents


def _check_dropfolder(settings: Settings, out: list[Finding]) -> None:
    """Drop folder (INGESTION.md "Drop folder specifics", SECURITY.md C3/C10/C11)."""
    if not settings.dropfolder_enabled:
        return
    inbox = settings.dropfolder_inbox
    processed = settings.dropfolder_processed
    failed = settings.dropfolder_failed or processed / "failed"
    for name, path in (("PERFIN_DROPFOLDER_PROCESSED", processed), ("failed/", failed)):
        if _inside(path, inbox):
            out.append(
                Finding(
                    "error",
                    "dropfolder.output_in_inbox",
                    f"{name} must be a sibling of PERFIN_DROPFOLDER_INBOX, not inside it: "
                    "processed files must never land back in the inbox",
                )
            )
    if settings.dropfolder_settle_seconds < 2 and not settings.dropfolder_ready_sidecar:
        out.append(
            Finding(
                "warning",
                "dropfolder.settle_short",
                "PERFIN_DROPFOLDER_SETTLE_SECONDS below 2 without "
                "PERFIN_DROPFOLDER_READY_SIDECAR: files still being written may be taken",
            )
        )
    if settings.role != "worker":
        return  # only the worker has the mounts
    if not inbox.is_dir():
        out.append(
            Finding(
                "warning",
                "dropfolder.inbox_missing",
                "PERFIN_DROPFOLDER_INBOX is not a directory in this container; "
                "the drop folder is idle",
            )
        )
        return
    inbox_writable = _dir_writable(inbox)
    if settings.dropfolder_after == "keep" and inbox_writable:
        out.append(
            Finding(
                "warning",
                "dropfolder.inbox_writable",
                "the drop-folder inbox is writable but PERFIN_DROPFOLDER_AFTER=keep never "
                "writes to it: mount it read-only (SECURITY.md deployment checklist)",
            )
        )
    if settings.dropfolder_after != "keep" and not inbox_writable:
        out.append(
            Finding(
                "warning",
                "dropfolder.inbox_read_only",
                f"PERFIN_DROPFOLDER_AFTER={settings.dropfolder_after} needs a writable "
                "inbox; it is read-only, so originals are kept (set "
                "PERFIN_DROPFOLDER_AFTER=keep, or mount the inbox read-write)",
            )
        )
    if settings.dropfolder_after == "move" and inbox_writable and not _dir_writable(processed):
        out.append(
            Finding(
                "warning",
                "dropfolder.processed_not_writable",
                "PERFIN_DROPFOLDER_AFTER=move needs a writable PERFIN_DROPFOLDER_PROCESSED "
                "directory; originals stay in the inbox until it is",
            )
        )


def _check_antivirus(settings: Settings, out: list[Finding]) -> None:
    """ClamAV sidecar (SECURITY.md C9, ``app.integrations.antivirus``)."""
    if settings.antivirus != "clamav":
        return
    from app.integrations.antivirus.clamav import ClamdAddress, ClamdAddressError

    try:
        ClamdAddress.parse(settings.clamav_address)
    except ClamdAddressError as exc:
        out.append(Finding("error", "antivirus.address_invalid", f"PERFIN_CLAMAV_ADDRESS: {exc}"))
    if settings.antivirus_fail_mode == "open":
        out.append(
            Finding(
                "warning",
                "antivirus.fail_open",
                "PERFIN_ANTIVIRUS_FAIL_MODE=open: documents are processed unscanned "
                "whenever clamd cannot scan them",
            )
        )
    if settings.clamav_max_mb < settings.upload_max_mb:
        out.append(
            Finding(
                "warning",
                "antivirus.max_below_upload",
                "PERFIN_CLAMAV_MAX_MB is below PERFIN_UPLOAD_MAX_MB: larger documents "
                "cannot be scanned (they fail, or go on unscanned when failing open)",
            )
        )


def _production_checks(settings: Settings) -> list[Finding]:
    out: list[Finding] = []
    _check_master_key(settings, out)
    _check_secret_key(settings, out)
    _check_antivirus(settings, out)
    _check_database_url("database_url", settings.database_url.get_secret_value(), out)
    if settings.migration_database_url is not None:
        _check_database_url(
            "migration_database_url", settings.migration_database_url.get_secret_value(), out
        )
    if not _https(settings.public_url):
        out.append(
            Finding("error", "public_url.not_https", "PERFIN_PUBLIC_URL must be an https:// URL")
        )
    if not settings.secure_cookies:
        out.append(
            Finding(
                "error",
                "cookies.insecure",
                "PERFIN_SECURE_COOKIES=false is only allowed with PERFIN_DEV_MODE",
            )
        )
    if settings.log_level == "DEBUG":
        out.append(
            Finding(
                "error",
                "log_level.debug",
                "DEBUG cannot be enabled in production mode (PERFIN_LOG_LEVEL)",
            )
        )
    if not settings.require_mfa:
        out.append(
            Finding(
                "warning",
                "mfa.not_required",
                "PERFIN_REQUIRE_MFA=false: local accounts may log in with a password only",
            )
        )
    if not settings.csp_trusted_types:
        out.append(
            Finding(
                "warning",
                "csp.trusted_types_disabled",
                "PERFIN_CSP_TRUSTED_TYPES=false: the CSP no longer requires Trusted Types "
                "for DOM script sinks; re-enable it once the frontend no longer needs it",
            )
        )
    _check_oidc(settings, out)
    _check_network(settings, out)
    _check_metrics(settings, out)
    _check_mcp(settings, out)
    _check_smtp(settings, out)
    _check_dropfolder(settings, out)
    if not settings.master_key_backup_acknowledged:
        out.append(
            Finding(
                "warning",
                "master_key.backup_unacknowledged",
                "no master key backup acknowledgement recorded: losing the key loses every "
                "document. Back it up separately from the data, then set "
                "PERFIN_MASTER_KEY_BACKUP_ACKNOWLEDGED=true",
            )
        )
    return out


def check_config(settings: Settings) -> list[Finding]:
    """Return every configuration problem; errors first, then warnings.

    In dev mode errors are downgraded to warnings and a dev-mode warning is
    added, so a developer still sees what would block a production start.
    """
    findings = _production_checks(settings)
    if settings.dev_mode:
        findings = [
            Finding("warning", f.code, f"{f.message} (allowed only because dev mode is on)")
            if f.level == "error"
            else f
            for f in findings
        ]
        findings.insert(
            0,
            Finding(
                "warning",
                "dev_mode.enabled",
                "PERFIN_DEV_MODE=true: SECURITY CHECKS ARE DISABLED. Never run a real "
                "instance or real data in dev mode",
            ),
        )
    return sorted(findings, key=lambda f: 0 if f.level == "error" else 1)


def has_errors(findings: Iterable[Finding]) -> bool:
    return any(f.level == "error" for f in findings)


def enforce_config(settings: Settings) -> list[Finding]:
    """Startup gate for the api, worker and mcp roles.

    Logs every finding and raises ``InsecureConfigError`` if any is an error.
    Returns the (warning-only) findings otherwise.
    """
    from app.core.logging import get_logger

    log = get_logger("perfin.config")
    findings = check_config(settings)
    for finding in findings:
        log.log(
            40 if finding.level == "error" else 30,
            finding.message,
            extra={"code": finding.code, "check": "config"},
        )
    if has_errors(findings):
        raise InsecureConfigError(findings)
    return findings

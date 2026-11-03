# SPDX-License-Identifier: AGPL-3.0-or-later
"""Audit trail: ``audit(...)`` appends to ``audit_log``; ``verify_chain`` checks it.

``audit_log`` is append-only (SECURITY.md I4): the application role has only
``SELECT, INSERT`` and a trigger refuses ``UPDATE``/``DELETE``/``TRUNCATE``
for every role. A ``BEFORE INSERT`` trigger assigns ``seq``, ``prev_hash``,
``hash`` and ``created_at`` (see ``app.db.rls.audit_ddl``), so rows form a
SHA-256 hash chain that :func:`verify_chain` recomputes.

What is stored is minimised before it reaches the database:

* values under secret-looking keys (password, token, secret, MFA secrets,
  wrapped keys, cookies, ...) are replaced by ``[REDACTED]``, keeping the key
  so the trail still shows *that* the field changed;
* every string goes through the logging scrubber
  (``app.core.logging.scrub_text``: account numbers, emails, phones, tokens);
* strings longer than :data:`MAX_STRING_CHARS` are cut, and ``before``/
  ``after`` larger than :data:`MAX_STATE_BYTES` are replaced by a truncation
  marker.

The row is inserted in the caller's transaction, so an audited change and its
audit row commit or roll back together. Household rows must be written in
that household's RLS context; instance-level rows (``household_id=None``)
only in an instance-admin or system-actor context
(``set_context(session, None, system_actor=True)``, used by the auth layer for
pre-authentication events such as failed logins), so household code cannot
forge instance-level events.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import ipaddress
import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, SecretBytes, SecretStr
from sqlalchemy import Connection, insert, text
from sqlalchemy.orm import Session

from app.core.ids import uuid7
from app.db.models import AuditLog
from app.db.rls import AUDIT_GENESIS_HASH

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

REDACTED: Final = "[REDACTED]"
MAX_STATE_BYTES: Final = 64 * 1024
# Longer strings are cut before scrubbing: audit values are names and short
# notes, and the scrubber's cost grows with input length.
MAX_STRING_CHARS: Final = 1024
TRUNCATED_SUFFIX: Final = "...[truncated]"
MAX_DEPTH: Final = 8
USER_AGENT_MAX: Final = 512
REQUEST_ID_MAX: Final = 128
TARGET_ID_MAX: Final = 200

# Key fragments (after lower-casing and '-' -> '_') whose values are never stored.
SECRET_KEY_PARTS: Final = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "master_key",
    "signing_key",
    "encryption_key",
    "credential",
    "cookie",
    "authorization",
    "session_id",
    "csrf",
    "totp",
    "otp_secret",
    "recovery_code",
    "wrapped_dek",
    "code_verifier",
)

_ACTION_RE: Final = re.compile(r"^[a-z][a-z0-9_.:-]{0,99}$")
_TYPE_RE: Final = re.compile(r"^[a-z][a-z0-9_.:-]{0,99}$")


def is_secret_key(name: str) -> bool:
    """True when a mapping key names a secret whose value must not be stored."""
    key = name.lower().replace("-", "_")
    if any(part in key for part in SECRET_KEY_PARTS):
        return True
    denied = _logging_denylist()
    return denied is not None and denied(name)


def _logging_denylist() -> Callable[[str], bool] | None:
    try:
        from app.core.logging import is_denied_field
    except ImportError:  # pragma: no cover - logging is part of the core track
        return None
    return is_denied_field


def _scrubber() -> Callable[[str], str]:
    try:
        from app.core.logging import scrub_text
    except ImportError:  # pragma: no cover - logging is part of the core track
        return lambda value: value
    return scrub_text


def sanitize(value: Any, *, _depth: int = 0, _scrub: Callable[[str], str] | None = None) -> Any:
    """Return a JSON-able copy of ``value`` with secrets redacted and strings scrubbed."""
    scrub = _scrub or _scrubber()
    if _depth > MAX_DEPTH:
        return REDACTED
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, SecretStr | SecretBytes):
        return REDACTED
    if isinstance(value, str):
        return _scrub_bounded(value, scrub)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return sanitize(value.value, _depth=_depth + 1, _scrub=scrub)
    if isinstance(value, ipaddress.IPv4Address | ipaddress.IPv6Address):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(value)} bytes>"
    if isinstance(value, BaseModel):
        return sanitize(value.model_dump(mode="python"), _depth=_depth + 1, _scrub=scrub)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return sanitize(dataclasses.asdict(value), _depth=_depth + 1, _scrub=scrub)
    if isinstance(value, Mapping):
        return {
            str(k): REDACTED
            if is_secret_key(str(k))
            else sanitize(v, _depth=_depth + 1, _scrub=scrub)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [sanitize(v, _depth=_depth + 1, _scrub=scrub) for v in value]
    return _scrub_bounded(str(value), scrub)


def _scrub_bounded(value: str, scrub: Callable[[str], str]) -> str:
    if len(value) > MAX_STRING_CHARS:
        return scrub(value[:MAX_STRING_CHARS]) + TRUNCATED_SUFFIX
    return scrub(value)


def _bounded_state(value: Any) -> Any:
    if value is None:
        return None
    clean = sanitize(value)
    size = len(json.dumps(clean, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size > MAX_STATE_BYTES:
        return {"_truncated": True, "bytes": size}
    return clean


def _clean_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _clip(value: str | None, limit: int, scrub: Callable[[str], str] | None = None) -> str | None:
    if value is None:
        return None
    cleaned = "".join(ch for ch in value[: limit * 2] if ch.isprintable())[:limit]
    if scrub is not None:
        cleaned = scrub(cleaned)
    return cleaned[:limit] or None


def _current_request_id() -> str | None:
    try:
        from app.core.logging import get_request_id
    except ImportError:  # pragma: no cover
        return None
    return get_request_id()


@dataclass(frozen=True)
class RequestMeta:
    """Where an audited action came from (all optional)."""

    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None

    @classmethod
    def from_request(cls, conn: HTTPConnection) -> RequestMeta:
        """Client IP (trusted-proxy aware), user agent and request id of ``conn``."""
        from app.core.request_context import client_ip, request_id

        return cls(
            ip=client_ip(conn),
            user_agent=conn.headers.get("user-agent"),
            request_id=request_id(conn),
        )


def audit(
    session: Session | Connection,
    *,
    action: str,
    target_type: str | None = None,
    target_id: str | uuid.UUID | None = None,
    before: Any = None,
    after: Any = None,
    household_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    request: HTTPConnection | None = None,
    meta: RequestMeta | None = None,
) -> uuid.UUID:
    """Append one audit row in the caller's transaction and return its id.

    ``action`` is a dotted lower-case verb such as ``user.login_failed`` or
    ``account.update``. ``request`` (or an explicit ``meta``) supplies IP,
    user agent and request id; the request id falls back to the one bound
    for logging (workers). Secrets are never stored (see module doc).
    """
    if not _ACTION_RE.fullmatch(action):
        raise ValueError(f"invalid audit action: {action!r}")
    if target_type is not None and not _TYPE_RE.fullmatch(target_type):
        raise ValueError(f"invalid audit target_type: {target_type!r}")
    if household_id is not None and not isinstance(household_id, uuid.UUID):
        raise TypeError("household_id must be a UUID")
    if user_id is not None and not isinstance(user_id, uuid.UUID):
        raise TypeError("user_id must be a UUID")
    if meta is None:
        meta = RequestMeta.from_request(request) if request is not None else RequestMeta()
    scrub = _scrubber()
    row_id = uuid7()
    session.execute(
        insert(AuditLog).values(
            id=row_id,
            household_id=household_id,
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=_clip(None if target_id is None else str(target_id), TARGET_ID_MAX),
            before=_bounded_state(before),
            after=_bounded_state(after),
            ip=_clean_ip(meta.ip),
            user_agent=_clip(meta.user_agent, USER_AGENT_MAX, scrub),
            request_id=_clip(meta.request_id or _current_request_id(), REQUEST_ID_MAX),
        )
    )
    return row_id


# ---------------------------------------------------------------------------
# Hash chain verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainVerification:
    """Result of :func:`verify_chain`.

    ``head_hash`` is the hash of the last row; record it outside the database
    (e.g. with each backup) to detect truncation of the tail.
    """

    ok: bool
    rows_checked: int
    head_seq: int
    head_hash: str
    first_bad_seq: int | None = None
    reason: str | None = None


# Arguments in AUDIT_HASH_COLUMNS order (checked by a unit test).
VERIFY_SQL: Final = (
    "SELECT seq, prev_hash, hash, perfin_audit_hash(prev_hash, seq, id, household_id, "
    "user_id, action, target_type, target_id, before, after, ip, user_agent, request_id, "
    "created_at) AS expected FROM audit_log ORDER BY seq"
)


def verify_chain(session: Session | Connection, *, batch_size: int = 1000) -> ChainVerification:
    """Recompute the audit hash chain from the first row.

    Detects edited rows (hash mismatch), deleted or re-ordered rows (``seq``
    gap or broken ``prev_hash`` link). Run it as the owner/migration role:
    the application role sees only its RLS context, which shows up as gaps.
    """
    statement = text(VERIFY_SQL).execution_options(yield_per=batch_size)
    previous_hash = AUDIT_GENESIS_HASH
    expected_seq = 1
    checked = 0
    for row in session.execute(statement):
        seq = int(row.seq)
        if seq != expected_seq:
            return ChainVerification(
                False, checked, expected_seq - 1, previous_hash, seq, "sequence gap"
            )
        if row.prev_hash != previous_hash:
            return ChainVerification(
                False, checked, expected_seq - 1, previous_hash, seq, "broken link"
            )
        if row.hash != row.expected:
            return ChainVerification(
                False, checked, expected_seq - 1, previous_hash, seq, "hash mismatch"
            )
        previous_hash = row.hash
        expected_seq += 1
        checked += 1
    return ChainVerification(True, checked, expected_seq - 1, previous_hash)

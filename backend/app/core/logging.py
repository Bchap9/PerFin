# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured JSON logging with PII scrubbing (SECURITY.md H8, F5, G2).

Every record is written as one JSON object per line::

    {"ts": "...", "level": "INFO", "logger": "app.x", "msg": "...",
     "request_id": "...", <extra fields>}

``ScrubbingFilter`` runs on the handler, so it sees records from every
logger (ours, uvicorn, SQLAlchemy, libraries). It

* drops any extra field whose name is on the denylist (passwords, secrets,
  tokens, cookies, master key material, document text, account numbers);
* replaces identifiers in the message, extra values, exception text and
  stack info with typed placeholders: ``[ACCT]``, ``[GOVID]``, ``[EMAIL]``,
  ``[PHONE]``, ``[DOB]``, ``[REDACTED]``.

The uvicorn access log is rewritten into ``method``/``path``/``status``
fields with the query string removed before it is formatted.

Use ``get_logger(__name__)`` everywhere; pass structured data via
``extra={...}``. Never log document text or key material, even though the
filter would drop it: the filter is the safety net, not the policy.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import traceback
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import IO, Any, Final

from app.core.ids import uuid7
from app.core.request_context import request_id_var, valid_request_id

# ---------------------------------------------------------------------------
# Request id
# ---------------------------------------------------------------------------
#
# ``app.core.request_context.RequestContextMiddleware`` owns the request id
# for HTTP requests and sets ``request_id_var``; the formatter reads it.
# Workers and CLI jobs use ``bind_request_id`` to get the same field.


def get_request_id() -> str | None:
    return request_id_var.get()


@contextmanager
def bind_request_id(request_id: str | None = None) -> Iterator[str]:
    """Set the request id for the duration of the block (new UUIDv7 if invalid)."""
    value = request_id if valid_request_id(request_id) and request_id else str(uuid7())
    token = request_id_var.set(value)
    try:
        yield value
    finally:
        request_id_var.reset(token)


# ---------------------------------------------------------------------------
# Scrubbing
# ---------------------------------------------------------------------------

REDACTED: Final = "[REDACTED]"

#: Extra-field names that are dropped outright (compared after lower-casing
#: and mapping ``-`` to ``_``). A name is also dropped if it contains one of
#: ``DENYLIST_SUBSTRINGS``.
DENYLIST_FIELDS: Final = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "master_key",
        "extracted_text",
        "document_text",
        "ocr_text",
        "raw_text",
        "text_content",
        "authorization",
        "cookie",
        "cookies",
        "set_cookie",
        "session",
        "session_id",
        "sid",
        "csrf",
        "otp",
        "totp",
        "totp_code",
        "recovery_code",
        "recovery_codes",
        "ssn",
        "itin",
        "ein",
        "tax_id",
        "dob",
        "date_of_birth",
        "account_number",
        "card_number",
        "routing_number",
        "iban",
        "pan",
        "cvv",
        "dek",
        "kek",
        "wrapped_key",
        "private_key",
        "api_key",
        "client_secret",
        "credentials",
        "body",
        "request_body",
        "response_body",
    }
)
DENYLIST_SUBSTRINGS: Final = (
    "password",
    "passwd",
    "secret",
    "token",
    "master_key",
    "private_key",
    "api_key",
    "cookie",
    "authorization",
    "session_id",
    "csrf",
    "_text",
    "account_number",
    "card_number",
    "routing_number",
    "recovery_code",
)


def is_denied_field(name: str) -> bool:
    key = name.lower().replace("-", "_")
    return key in DENYLIST_FIELDS or any(part in key for part in DENYLIST_SUBSTRINGS)


# Every pattern below must run in linear time on hostile input (log lines and
# audit values are attacker-influenced). Two techniques keep them linear:
# a negative lookbehind so a match is only attempted at the *start* of a run
# of the characters its leading quantifier consumes, and possessive
# quantifiers (``*+``, ``++``) where the next token can never match a
# character of the quantified class, so giving characters back is pointless.
# tests/unit/test_scrubber_redos.py enforces this with timed hostile inputs.
_NAME_CHAR = r"[A-Za-z0-9_.-]"
_SENSITIVE_WORD = (
    r"(?:password|passwd|pwd|secret|token|session|csrf|"
    r"api[_-]?key|access[_-]?key|private[_-]?key|master[_-]?key|auth|cookie|code_verifier)"
)
# A whole run of name characters that contains a sensitive word. The lookahead
# finds the word (lazily, bounded by the run); the run is then taken whole.
_SENSITIVE_NAME = rf"(?:(?={_NAME_CHAR}*?{_SENSITIVE_WORD}){_NAME_CHAR}++)"

# Order matters: credentials first (they may contain digits or @), then
# structured identifiers, then the generic digit-run catch-all.
_RULES: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # Authorization / Proxy-Authorization headers, any scheme.
    (
        re.compile(r"(?i)\b((?:proxy-)?authorization\b[\"']?\s*[:=]\s*)[^\r\n,;}]+"),
        r"\1" + REDACTED,
    ),
    # Cookie / Set-Cookie header values (whole value).
    (re.compile(r"(?i)\b((?:set-)?cookie\b[\"']?\s*[:=]\s*)[^\r\n}]+"), r"\1" + REDACTED),
    # Bearer / Basic tokens anywhere.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}"), r"\1 " + REDACTED),
    # JWT-looking blobs.
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
        REDACTED,
    ),
    # Credentials embedded in URLs: scheme://user:password@host
    (
        re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*+://[^\s/:@]*+:)[^\s/@]++@"),
        r"\1" + REDACTED + "@",
    ),
    # name=value / name: value / "name": "value" for secret-ish names.
    (
        re.compile(
            r"(?i)(?<![A-Za-z0-9_.-])(" + _SENSITIVE_NAME + r"[\"']?\s*+[:=]\s*+)"
            r"(\"[^\"]*+\"|'[^']*+'|[^\s&;,\"'}]++)"
        ),
        r"\1" + REDACTED,
    ),
    # Header lists / tuples: ('x-csrf-token', 'value'), (b"cookie", b"value").
    (
        re.compile(r"(?i)([\"']" + _SENSITIVE_NAME + r"[\"']\s*+,\s*+b?)(\"[^\"]*+\"|'[^']*+')"),
        r"\1'" + REDACTED + "'",
    ),
    # Email addresses.
    (
        re.compile(
            r"(?i)(?<![a-z0-9._%+-])[a-z0-9._%+-]+(?:@|%40)"
            r"[a-z0-9-]++(?:\.[a-z0-9-]++)*\.[a-z]{2,}\b"
        ),
        "[EMAIL]",
    ),
    # US SSN / ITIN (123-45-6789, 123 45 6789) and EIN (12-3456789).
    (re.compile(r"(?<![\w-])\d{3}[- ]\d{2}[- ]\d{4}(?![\w-])"), "[GOVID]"),
    (re.compile(r"(?<![\w-])\d{2}-\d{7}(?![\w-])"), "[GOVID]"),
    # IBAN.
    (re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b"), "[ACCT]"),
    # Card numbers in 4-digit groups (4111 1111 1111 1111, 4111-1111-...).
    (re.compile(r"(?<![\w-])\d{4}(?:[ -]\d{4}){2,3}(?:[ -]\d{1,4})?(?![\w-])"), "[ACCT]"),
    # Amex style 4-6-5.
    (re.compile(r"(?<![\w-])\d{4}[ -]\d{6}[ -]\d{5}(?![\w-])"), "[ACCT]"),
    # Partial masks: ****1234, XXXX-1234, xx1234, ...1234, …1234, ••1234, #1234
    (
        re.compile(r"(?i)(?:(?<![*x•#])[*x•#]{2,}+|(?<!\.)\.{2,}+|…)[ -]?\d{2,}+"),
        "[ACCT]",
    ),
    # "ending in 1234", "last 4 1234", "acct 1234", "account #5678", "card no. 9999"
    (
        re.compile(
            r"(?i)\b(?:ending(?:\s+in)?|last\s*(?:4|four)(?:\s+digits)?|"
            r"acct|account|card|a/c|loan|policy|routing|member|checking|savings|"
            r"visa|mastercard|amex|discover)"
            r"(?:\s*+(?:no\.?|number|num|#|ending(?:\s+in)?))?\s*+[:#=]?\s*+"
            r"(?:[*x\u2022.\u2026]++[ -]?)?\d{4,}+"
        ),
        "[ACCT]",
    ),
    # US phone numbers: (555) 123-4567, 555-123-4567, +1 555 123 4567, 555.123.4567
    (
        re.compile(
            r"(?<![\w.-])(?:\+?1[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\w-])"
        ),
        "[PHONE]",
    ),
    # International phone numbers in E.164 form.
    (re.compile(r"(?<![\w])\+\d{7,15}(?!\d)"), "[PHONE]"),
    # Any run of 8+ digits (account, card, routing, SSN without dashes).
    (re.compile(r"\d{8,}"), "[ACCT]"),
)

# UUIDs (request ids, primary keys) are safe and would otherwise trip the
# digit-run rule when their hex happens to be all digits.
_UUID_RE: Final = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# ISO 8601 timestamps are safe too.
_ISO_TS_RE: Final = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
_PROTECT_PREFIX: Final = "\x00P"

# Dates of birth must go before ISO dates are protected as safe.
_DOB_RE: Final = re.compile(
    r"(?i)\b(dob|date[ _-]?of[ _-]?birth|birth[ _-]?date|birthday|born)"
    r"([\"']?\s*+[:=]?\s*+[\"']?\s*+)"
    r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})"
)


def scrub_text(text: str) -> str:
    """Replace identifiers and credentials in ``text`` with typed placeholders."""
    if not text:
        return text
    protected: list[str] = []

    def _protect(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"{_PROTECT_PREFIX}{len(protected) - 1}\x00"

    text = text.replace("\x00", "")
    text = _DOB_RE.sub(r"\1\2[DOB]", text)
    work = _UUID_RE.sub(_protect, text)
    work = _ISO_TS_RE.sub(_protect, work)
    for pattern, replacement in _RULES:
        work = pattern.sub(replacement, work)
    if protected:
        work = re.sub(
            re.escape(_PROTECT_PREFIX) + r"(\d+)\x00",
            lambda m: protected[int(m.group(1))],
            work,
        )
    return work


_MAX_DEPTH: Final = 6


def scrub_value(value: Any, depth: int = 0) -> Any:
    """Scrub a JSON-able value: strings scrubbed, denylisted keys dropped."""
    if depth > _MAX_DEPTH:
        return REDACTED
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(value)} bytes>"
    if isinstance(value, Mapping):
        return {
            str(k): scrub_value(v, depth + 1)
            for k, v in value.items()
            if not is_denied_field(str(k))
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [scrub_value(v, depth + 1) for v in value]
    # SecretStr renders as '**********'; everything else goes through str().
    return scrub_text(str(value))


# Attributes every LogRecord has; anything else came from ``extra=``.
_RESERVED_ATTRS: Final = frozenset(
    vars(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None)).keys()
) | {"message", "asctime", "taskName", "color_message"}


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in vars(record).items() if k not in _RESERVED_ATTRS}


class ScrubbingFilter(logging.Filter):
    """Redacts PII and credentials in place; never drops the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_perfin_scrubbed", False):
            return True
        try:
            message = record.getMessage()
        except (TypeError, ValueError):  # a bad format string must not lose the record
            message = f"{record.msg!s} (unformattable log arguments)"
        record.msg = scrub_text(message)
        record.args = None
        if record.exc_info:
            text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = scrub_text(text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = scrub_text(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub_text(record.stack_info)
        for key, value in _extra_fields(record).items():
            if is_denied_field(key):
                delattr(record, key)
            else:
                setattr(record, key, scrub_value(value))
        setattr(record, "_perfin_scrubbed", True)  # noqa: B010
        return True


# ---------------------------------------------------------------------------
# Access log
# ---------------------------------------------------------------------------


class AccessLogFilter(logging.Filter):
    """Turns uvicorn's access record into structured fields without the query string."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            client, method, full_path, http_version, status = args
            path = str(full_path).split("?", 1)[0].split("#", 1)[0]
            record.msg = "%s %s %s"
            record.args = (method, path, status)
            record.http = {
                "client": str(client),
                "method": str(method),
                "path": path,
                "status": status,
                "version": str(http_version),
            }
        return True


# ---------------------------------------------------------------------------
# Formatter and setup
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Expects ``ScrubbingFilter`` to have run."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or get_request_id(),
        }
        for key, value in _extra_fields(record).items():
            if key in ("_perfin_scrubbed", "request_id") or key in payload:
                continue
            payload[key] = value
        if record.exc_text:
            payload["exc"] = record.exc_text
        if record.stack_info:
            payload["stack"] = record.stack_info
        return json.dumps(payload, default=str, ensure_ascii=False, separators=(",", ":"))


_HANDLER_NAME: Final = "perfin-json"

#: Loggers that keep their own handlers by default and must route to root.
_ROUTED_LOGGERS: Final = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


def make_handler(stream: IO[str] | None = None) -> logging.Handler:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.addFilter(ScrubbingFilter())
    handler.setFormatter(JsonFormatter())
    return handler


def configure_logging(level: str = "INFO", stream: IO[str] | None = None) -> logging.Handler:
    """Install the JSON handler on the root logger (idempotent).

    Replaces any handler previously installed by this function, routes
    uvicorn's loggers through root, and strips query strings from the
    access log. Returns the handler (tests attach to a ``StringIO``).
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == _HANDLER_NAME:
            root.removeHandler(existing)
    handler = make_handler(stream)
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in _ROUTED_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
        lg.setLevel(level.upper())
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogFilter) for f in access.filters):
        access.addFilter(AccessLogFilter())
    return handler


def get_logger(name: str) -> logging.Logger:
    """The logger to use everywhere in PerFin (``get_logger(__name__)``)."""
    return logging.getLogger(name)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured JSON logging and the PII scrubbing filter (SECURITY.md H8, G2).

The corpus below is synthetic. Every entry is a string that must never
appear in a log line; the test asserts zero leaks across messages, format
arguments, extra fields, nested values and exception text.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.logging import (
    AccessLogFilter,
    bind_request_id,
    configure_logging,
    get_logger,
    get_request_id,
    is_denied_field,
    make_handler,
    scrub_text,
)

# (sensitive fragment that must not survive, text containing it, placeholder)
CORPUS: list[tuple[str, str, str]] = [
    # Account and card numbers
    ("4111111111111111", "card 4111111111111111 declined", "[ACCT]"),
    ("4111 1111 1111 1111", "pan 4111 1111 1111 1111 seen", "[ACCT]"),
    ("4111-1111-1111-1111", "pan 4111-1111-1111-1111 seen", "[ACCT]"),
    ("3782 822463 10005", "amex 3782 822463 10005", "[ACCT]"),
    ("12345678", "acct 12345678 opened", "[ACCT]"),
    ("021000021", "routing 021000021", "[ACCT]"),
    ("000123456789", "ACH to 000123456789 posted", "[ACCT]"),
    ("****1234", "card ****1234 charged", "[ACCT]"),
    ("**** 5678", "card **** 5678 charged", "[ACCT]"),
    ("XXXX1234", "ACCOUNT XXXX1234", "[ACCT]"),
    ("xxxx-9876", "acct xxxx-9876 overdrawn", "[ACCT]"),
    ("...5678", "ACCT ...5678 payment", "[ACCT]"),
    ("…5678", "checking …5678", "[ACCT]"),
    ("••••4321", "card ••••4321", "[ACCT]"),
    ("ending in 4455", "Visa ending in 4455", "[ACCT]"),
    ("Account #7788", "Account #7788 synced", "[ACCT]"),
    ("last 4 digits 3344", "last 4 digits 3344", "[ACCT]"),
    ("GB82WEST12345698765432", "iban GB82WEST12345698765432 ok", "[ACCT]"),
    ("DE89 3704 0044 0532 0130 00", "iban DE89 3704 0044 0532 0130 00", "[ACCT]"),
    # Government identifiers
    ("123-45-6789", "ssn 123-45-6789 on file", "[GOVID]"),
    ("123 45 6789", "ssn 123 45 6789 on file", "[GOVID]"),
    ("987654321", "tin 987654321", "[ACCT]"),
    ("12-3456789", "ein 12-3456789 employer", "[GOVID]"),
    # Contact data
    ("jane.doe@example.test", "login for jane.doe@example.test failed", "[EMAIL]"),
    ("J.Q+tag@mail.example.co.uk", "mail J.Q+tag@mail.example.co.uk", "[EMAIL]"),
    ("(555) 867-5309", "call (555) 867-5309 today", "[PHONE]"),
    ("555-867-5309", "call 555-867-5309 today", "[PHONE]"),
    ("+1 555 867 5309", "call +1 555 867 5309 today", "[PHONE]"),
    ("555.867.5309", "call 555.867.5309 today", "[PHONE]"),
    ("+447700900123", "uk +447700900123", "[PHONE]"),
    ("jane%40example.test", "GET /users/jane%40example.test", "[EMAIL]"),
    ("x4321", "visa x4321 charged", "[ACCT]"),
    ("*4321", "card *4321 charged", "[ACCT]"),
    ("1985-04-12", "DOB 1985-04-12", "[DOB]"),
    ("04/12/1985", '{"date_of_birth": "04/12/1985"}', "[DOB]"),
    # Credentials
    (
        "abcDEF123tokenvalue",
        "Authorization: Bearer abcDEF123tokenvalue",
        "[REDACTED]",
    ),
    ("dXNlcjpwYXNz", "authorization=Basic dXNlcjpwYXNz", "[REDACTED]"),
    ("rawtokenNoScheme9", "Authorization: rawtokenNoScheme9", "[REDACTED]"),
    ("sk_live_synthetic99", "bearer sk_live_synthetic99 used", "[REDACTED]"),
    (
        "s3ss10nV4lu3xyz",
        "Cookie: __Host-perfin_session=s3ss10nV4lu3xyz; theme=dark",
        "[REDACTED]",
    ),
    (
        "abcSetCookieVal",
        "Set-Cookie: __Host-perfin_session=abcSetCookieVal; Path=/; Secure",
        "[REDACTED]",
    ),
    ("deadbeefsessionid", "session_id=deadbeefsessionid", "[REDACTED]"),
    ("csrfTokenValue42", "X-CSRF-Token: csrfTokenValue42", "[REDACTED]"),
    ("hunter2!", "password=hunter2! rejected", "[REDACTED]"),
    ("pa ss word", '{"password": "pa ss word"}', "[REDACTED]"),
    ("cl13ntS3cr3t", "client_secret=cl13ntS3cr3t&grant_type=x", "[REDACTED]"),
    ("pk_api_KEY_77", "api_key: pk_api_KEY_77", "[REDACTED]"),
    ("dbpassw0rd", "connect postgresql://perfin:dbpassw0rd@db:5432/perfin", "[REDACTED]"),
    (
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl",
        "id_token eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl",
        "[REDACTED]",
    ),
    ("QmFzZTY0TWFzdGVyS2V5", "master_key=QmFzZTY0TWFzdGVyS2V5", "[REDACTED]"),
    ("v3rifierXYZ", "code_verifier=v3rifierXYZ", "[REDACTED]"),
    ("tok123csrf", "('x-csrf-token', 'tok123csrf')", "[REDACTED]"),
    ("rawCookieVal", "[(b'cookie', b'rawCookieVal')]", "[REDACTED]"),
]

# Extra fields that must be dropped by name whatever their value.
DENIED_EXTRAS: dict[str, str] = {
    "password": "plain-pw-1",
    "new_password": "plain-pw-2",
    "secret": "plain-secret-3",
    "client_secret": "plain-secret-4",
    "token": "plain-token-5",
    "access_token": "plain-token-6",
    "master_key": "plain-mk-7",
    "extracted_text": "Statement page text 8",
    "document_text": "Statement page text 9",
    "ocr_text": "Statement page text 10",
    "authorization": "plain-auth-11",
    "cookie": "plain-cookie-12",
    "session_id": "plain-sid-13",
    "csrf_token": "plain-csrf-14",
    "account_number": "plain-acct-15",
    "routing_number": "plain-rt-16",
    "ssn": "plain-ssn-17",
    "recovery_codes": "plain-rc-18",
    "totp": "plain-otp-19",
    "Set-Cookie": "plain-cookie-20",
}

# Strings that must survive scrubbing unchanged.
SAFE: list[str] = [
    "0190f3a2-1234-7abc-8def-0123456789ab",
    "12345678-1234-7234-8234-123456789012",
    "2026-11-03T12:34:56.789+00:00",
    "2026-11-03",
    "imported 42 transactions in 1234 ms",
    "status 200",
    "GET /api/v1/accounts",
    "amount 12.34 USD",
]


def _entries() -> list[Any]:
    return [pytest.param(secret, text, ph, id=f"{ph}:{secret[:12]}") for secret, text, ph in CORPUS]


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    handler = make_handler(buf)
    logger = logging.getLogger("perfin.test.logging")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    yield buf
    logger.handlers = []
    logger.propagate = True


def _lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line]


LOG = "perfin.test.logging"


# ---------------------------------------------------------------------------
# Corpus: zero leaks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("secret", "text", "placeholder"), _entries())
def test_scrub_text_corpus(secret: str, text: str, placeholder: str) -> None:
    scrubbed = scrub_text(text)
    assert secret not in scrubbed
    assert placeholder in scrubbed


@pytest.mark.parametrize("text", SAFE)
def test_safe_values_survive(text: str) -> None:
    assert scrub_text(text) == text


def test_corpus_has_zero_leaks_through_every_channel(stream: io.StringIO) -> None:
    log = get_logger(LOG)
    for _secret, text, _ph in CORPUS:
        log.info(text)
        log.info("formatted: %s", text)
        log.warning("field", extra={"detail": text, "nested": {"memo": [text, {"x": text}]}})
        try:
            raise ValueError(text)
        except ValueError:
            log.exception("failure while handling")
    log.info("denied", extra=DENIED_EXTRAS)
    log.info("denied nested", extra={"ctx": dict(DENIED_EXTRAS)})
    output = stream.getvalue()
    leaks = [secret for secret, _t, _p in CORPUS if secret in output]
    leaks += [value for value in DENIED_EXTRAS.values() if value in output]
    assert leaks == []
    # Every line is a standalone JSON object.
    assert len(_lines(stream)) == len(CORPUS) * 4 + 2


def test_denied_fields_are_dropped_not_renamed(stream: io.StringIO) -> None:
    get_logger(LOG).info("x", extra={**DENIED_EXTRAS, "household": "h1"})
    (line,) = _lines(stream)
    for name in DENIED_EXTRAS:
        assert name not in line
    assert line["household"] == "h1"


@pytest.mark.parametrize("name", [*DENIED_EXTRAS, "Authorization", "X-Csrf-Token", "dek"])
def test_denylist_names(name: str) -> None:
    assert is_denied_field(name)


@pytest.mark.parametrize("name", ["household_id", "user_id", "status", "path", "count", "code"])
def test_allowed_names(name: str) -> None:
    assert not is_denied_field(name)


def test_secretstr_and_bytes_never_rendered(stream: io.StringIO) -> None:
    get_logger(LOG).info(
        "objects",
        extra={"key_material": b"\x01\x02raw-bytes", "wrapped": SecretStr("never-show-me")},
    )
    (line,) = _lines(stream)
    out = stream.getvalue()
    assert "never-show-me" not in out
    assert "raw-bytes" not in out
    assert line["key_material"] == "<11 bytes>"


def test_bad_format_args_do_not_lose_the_record(stream: io.StringIO) -> None:
    get_logger(LOG).info("two %s %s", "only-one 4111111111111111")
    (line,) = _lines(stream)
    assert "unformattable" in line["msg"]


# ---------------------------------------------------------------------------
# JSON shape and request id
# ---------------------------------------------------------------------------


def test_json_line_shape(stream: io.StringIO) -> None:
    get_logger(LOG).info("hello %s", "world", extra={"count": 3, "ok": True})
    (line,) = _lines(stream)
    assert set(line) >= {"ts", "level", "logger", "msg", "request_id"}
    assert line["level"] == "INFO"
    assert line["logger"] == LOG
    assert line["msg"] == "hello world"
    assert line["request_id"] is None
    assert line["count"] == 3
    assert line["ok"] is True
    assert line["ts"].endswith("+00:00")
    assert "\n" not in stream.getvalue().rstrip("\n")


def test_exception_is_logged_once_as_text(stream: io.StringIO) -> None:
    try:
        raise RuntimeError("boom for user jane.doe@example.test")
    except RuntimeError:
        get_logger(LOG).exception("handler failed")
    (line,) = _lines(stream)
    assert "Traceback" in line["exc"]
    assert "RuntimeError" in line["exc"]
    assert "jane.doe@example.test" not in line["exc"]


def test_request_id_is_bound_and_reset(stream: io.StringIO) -> None:
    with bind_request_id("req-abc.123") as rid:
        assert rid == "req-abc.123"
        assert get_request_id() == rid
        get_logger(LOG).info("inside")
    get_logger(LOG).info("outside")
    inside, outside = _lines(stream)
    assert inside["request_id"] == "req-abc.123"
    assert outside["request_id"] is None


@pytest.mark.parametrize("supplied", [None, "", "x" * 129, "bad id\n", "jane@example.test"])
def test_unsafe_request_ids_are_replaced(supplied: str | None) -> None:
    with bind_request_id(supplied) as rid:
        assert rid != supplied
        assert len(rid) == 36


def test_request_context_middleware_id_reaches_log_lines(stream: io.StringIO) -> None:
    from app.core.request_context import request_id_var

    token = request_id_var.set("0190f3a2-1234-7abc-8def-0123456789ab")
    try:
        get_logger(LOG).info("in request")
    finally:
        request_id_var.reset(token)
    (line,) = _lines(stream)
    assert line["request_id"] == "0190f3a2-1234-7abc-8def-0123456789ab"


# ---------------------------------------------------------------------------
# Uvicorn access log and configure_logging
# ---------------------------------------------------------------------------


@pytest.fixture
def configured() -> Iterator[io.StringIO]:
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
    buf = io.StringIO()
    configure_logging("INFO", stream=buf)
    yield buf
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for n, (handlers, propagate, level, filters) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers[:] = handlers
        lg.propagate = propagate
        lg.setLevel(level)
        lg.filters[:] = filters


def test_uvicorn_access_log_drops_query_string(configured: io.StringIO) -> None:
    access = logging.getLogger("uvicorn.access")
    access.info(
        '%s - "%s %s HTTP/%s" %d',
        "203.0.113.7:51234",
        "GET",
        "/api/v1/reset?token=reset-token-secret&email=jane.doe@example.test#frag",
        "1.1",
        200,
    )
    lines = _lines(configured)
    (line,) = [entry for entry in lines if entry["logger"] == "uvicorn.access"]
    assert line["msg"] == "GET /api/v1/reset 200"
    assert line["http"]["path"] == "/api/v1/reset"
    assert line["http"]["status"] == 200
    out = configured.getvalue()
    assert "reset-token-secret" not in out
    assert "jane.doe" not in out
    assert "?" not in out


def test_access_filter_leaves_other_records_alone() -> None:
    record = logging.LogRecord("uvicorn.access", logging.INFO, "f", 1, "plain %s", ("x",), None)
    assert AccessLogFilter().filter(record)
    assert record.getMessage() == "plain x"


def test_configure_logging_is_idempotent_and_routes_uvicorn(configured: io.StringIO) -> None:
    configure_logging("WARNING", stream=configured)
    root = logging.getLogger()
    assert [h.get_name() for h in root.handlers].count("perfin-json") == 1
    assert root.level == logging.WARNING
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        assert lg.handlers == []
        assert lg.propagate is True
    access_filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, AccessLogFilter) for f in access_filters) == 1
    logging.getLogger("uvicorn.error").warning("Started for jane.doe@example.test")
    line = _lines(configured)[-1]
    assert line["logger"] == "uvicorn.error"
    assert line["msg"] == "Started for [EMAIL]"


def test_third_party_records_are_scrubbed(configured: io.StringIO) -> None:
    logging.getLogger("sqlalchemy.engine").warning(
        "connection failed for postgresql://perfin:hunter2pw@db/perfin"
    )
    assert "hunter2pw" not in configured.getvalue()


def test_master_key_never_logged_by_settings_or_check(
    configured: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64

    from app.core.config_check import check_config
    from app.core.settings import Settings

    key = base64.b64encode(bytes(range(32, 64))).decode("ascii")
    monkeypatch.setenv("PERFIN_MASTER_KEY", key)
    settings = Settings()
    log = get_logger("perfin.test.g2")
    log.warning("settings %s", settings)
    log.warning("dump", extra={"settings": settings.model_dump()})
    for finding in check_config(settings):
        log.warning(finding.message, extra={"code": finding.code})
    assert key not in configured.getvalue()

# SPDX-License-Identifier: AGPL-3.0-or-later
"""ClamAV client against a fake clamd (``app.integrations.antivirus``; SECURITY.md C9)."""

from __future__ import annotations

import base64
import secrets
import time
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config_check import check_config
from app.core.settings import Settings
from app.integrations.antivirus import clean_signature, scanner_from_settings
from app.integrations.antivirus.clamav import (
    ClamdAddress,
    ClamdAddressError,
    ClamdScanner,
    parse_reply,
)
from tests.fixtures.fake_clamd import MARKER, SIGNATURE, fake_clamd


@pytest.fixture
def settings() -> Settings:
    """Settings without a database (this is a unit test)."""
    return Settings(
        dev_mode=True,
        database_url=SecretStr("postgresql+psycopg://perfin:x@localhost:5432/perfin"),
        master_key=SecretStr(base64.b64encode(secrets.token_bytes(32)).decode("ascii")),
        secret_key=SecretStr(secrets.token_urlsafe(48)),
        public_url="http://testserver",
    )


def _scanner(address: str, **kwargs: float | int) -> ClamdScanner:
    return ClamdScanner(address=ClamdAddress.parse(address), **kwargs)  # type: ignore[arg-type]


def test_clean_and_infected_over_tcp() -> None:
    with fake_clamd("signature") as clamd:
        scanner = _scanner(clamd.address, timeout_seconds=5)
        clean = scanner.scan([b"hello ", b"world"], size=11)
        infected = scanner.scan(
            [b"x" * 100_000, MARKER, b"y" * 10], size=100_000 + len(MARKER) + 10
        )
    assert clean.status == "clean"
    assert not clean.infected
    assert infected.infected
    assert infected.signature == SIGNATURE
    # The whole plaintext reached clamd, length-prefixed in chunks.
    assert clamd.streams[0] == b"hello world"
    assert clamd.streams[1] == b"x" * 100_000 + MARKER + b"y" * 10
    assert clamd.commands == [b"zINSTREAM\0", b"zINSTREAM\0"]


def test_unix_socket(tmp_path: Path) -> None:
    with fake_clamd("signature", unix_path=tmp_path / "clamd.sock") as clamd:
        assert clamd.address.startswith("unix:///")
        result = _scanner(clamd.address, timeout_seconds=5).scan([MARKER], size=len(MARKER))
    assert result.infected


@pytest.mark.parametrize(
    ("mode", "error", "retryable"),
    [
        ("error", "scanner_error", True),
        ("size_limit", "too_large", False),
        ("garbage", "scanner_error", True),
        ("hangup", None, True),
    ],
)
def test_scanner_failures(mode: str, error: str | None, retryable: bool) -> None:
    with fake_clamd(mode) as clamd:  # type: ignore[arg-type]
        result = _scanner(clamd.address, timeout_seconds=5).scan([b"data"], size=4)
    assert result.status == "error"
    if error is not None:
        assert result.error == error
    assert result.retryable is retryable


def test_silent_clamd_times_out() -> None:
    with fake_clamd("silent") as clamd:
        started = time.monotonic()
        result = _scanner(clamd.address, timeout_seconds=1).scan([b"data"], size=4)
    assert time.monotonic() - started < 6
    assert result.status == "error"
    assert result.error == "timeout"
    assert result.retryable


def test_unreachable_clamd() -> None:
    with fake_clamd("clean") as clamd:
        address = clamd.address
    # Stopped: nothing listens there any more.
    result = _scanner(address, timeout_seconds=1).scan([b"data"], size=4)
    assert (result.status, result.error, result.retryable) == ("error", "unavailable", True)


def test_size_cap_is_checked_before_and_while_sending() -> None:
    with fake_clamd("clean") as clamd:
        scanner = _scanner(clamd.address, timeout_seconds=5, max_bytes=1000)
        declared = scanner.scan([b"x" * 10], size=1001)
        lying = scanner.scan([b"x" * 600, b"x" * 600], size=10)
    assert (declared.error, declared.retryable) == ("too_large", False)
    assert (lying.error, lying.retryable) == ("too_large", False)
    # The oversized declared file never reached clamd.
    assert all(len(s) <= 1000 for s in clamd.streams)


@pytest.mark.parametrize(
    ("reply", "status", "signature"),
    [
        (b"stream: OK\0", "clean", None),
        (b"1: stream: OK\0", "clean", None),
        (b"stream: Win.Test.EICAR_HDB-1 FOUND\0", "infected", "Win.Test.EICAR_HDB-1"),
        (b"stream: <script>alert(1)</script> FOUND\0", "infected", "_script_alert(1)_/script_"),
        (b"stream: lstat() failed ERROR\0", "error", None),
    ],
)
def test_parse_reply(reply: bytes, status: str, signature: str | None) -> None:
    result = parse_reply(reply)
    assert result.status == status
    assert result.signature == signature


def test_signature_names_are_bounded_and_allowlisted() -> None:
    assert clean_signature("a" * 1000) == "a" * 200
    assert clean_signature("  ") == "unknown"
    assert clean_signature("Evil\r\nX-Header: 1") == "Evil__X-Header:_1"


@pytest.mark.parametrize(
    "value",
    ["clamav:3310", "tcp://clamav", "tcp://:3310", "unix://relative", "http://clamav:3310", ""],
)
def test_bad_addresses(value: str) -> None:
    with pytest.raises(ClamdAddressError):
        ClamdAddress.parse(value)


def test_good_addresses() -> None:
    assert ClamdAddress.parse("tcp://clamav:3310") == ClamdAddress("tcp", host="clamav", port=3310)
    assert ClamdAddress.parse("unix:///run/clamav/clamd.sock") == ClamdAddress(
        "unix", path="/run/clamav/clamd.sock"
    )


def test_scanner_from_settings(settings: Settings) -> None:
    assert scanner_from_settings(settings.model_copy(update={"antivirus": "none"})) is None
    configured = settings.model_copy(
        update={"antivirus": "clamav", "clamav_address": "unix:///run/clamd.sock"}
    )
    scanner = scanner_from_settings(configured)
    assert isinstance(scanner, ClamdScanner)
    assert scanner.address.path == "/run/clamd.sock"
    assert scanner.max_bytes == configured.clamav_max_mb * 1024 * 1024


def test_config_check(settings: Settings) -> None:
    def codes(**update: object) -> set[str]:
        return {f.code for f in check_config(settings.model_copy(update=update))}

    assert not {c for c in codes(antivirus="none") if c.startswith("antivirus.")}
    assert "antivirus.address_invalid" in codes(antivirus="clamav", clamav_address="clamav")
    assert "antivirus.fail_open" in codes(antivirus="clamav", antivirus_fail_mode="open")
    assert "antivirus.max_below_upload" in codes(
        antivirus="clamav", clamav_max_mb=1, upload_max_mb=50
    )

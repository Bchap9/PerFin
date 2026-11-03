# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure helpers of local login and bootstrap (B7, A10)."""

from __future__ import annotations

import os
import stat
import string
from ipaddress import ip_network
from pathlib import Path

import pytest

from app.auth import bootstrap
from app.auth.local import from_breakglass_network, ip_in_networks, local_login_available
from app.core.logging import scrub_text
from app.core.settings import Settings


@pytest.mark.parametrize(
    ("ip", "expected"),
    [
        ("127.0.0.1", True),
        ("::ffff:127.0.0.1", True),  # IPv4-mapped IPv6 is unwrapped
        ("127.0.0.2", False),
        ("10.0.0.1", False),
        ("::1", False),
        ("not-an-ip", False),
        (None, False),
    ],
)
def test_breakglass_default_cidr(ip: str | None, expected: bool) -> None:
    assert from_breakglass_network(Settings(), ip) is expected


def test_custom_networks_v4_and_v6() -> None:
    nets = [ip_network("192.0.2.0/24"), ip_network("2001:db8::/32")]
    assert ip_in_networks("192.0.2.200", nets)
    assert ip_in_networks("2001:db8::5", nets)
    assert not ip_in_networks("198.51.100.1", nets)


def test_local_login_available() -> None:
    assert local_login_available(Settings(), "10.0.0.1")
    oidc_only = Settings(local_login_enabled=False)
    assert not local_login_available(oidc_only, "10.0.0.1")
    assert local_login_available(oidc_only, "127.0.0.1")


def test_setup_token_is_long_random_and_letters_only() -> None:
    tokens = {bootstrap.new_setup_token() for _ in range(50)}
    assert len(tokens) == 50
    for token in tokens:
        assert len(token) == bootstrap.TOKEN_LENGTH
        assert set(token) <= set(string.ascii_letters)


def test_setup_url_survives_the_log_scrubber_and_uses_the_fragment() -> None:
    settings = Settings(public_url="https://perfin.example.test/")
    for _ in range(50):
        url = bootstrap.setup_url(settings, bootstrap.new_setup_token())
        assert url.startswith("https://perfin.example.test/setup#t=")
        assert scrub_text(f"one-time setup URL: {url}") == f"one-time setup URL: {url}"


def test_url_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "setup" / "url"
    assert bootstrap.write_url_file(path, "https://x.test/setup#t=abc")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    # Rewriting an existing file with looser permissions tightens them.
    path.chmod(0o644)
    assert bootstrap.write_url_file(path, "https://x.test/setup#t=def")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == "https://x.test/setup#t=def\n"
    bootstrap.remove_url_file(path)
    assert not path.exists()
    bootstrap.remove_url_file(path)  # idempotent


def test_url_file_refuses_a_symlink(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("untouched", encoding="utf-8")
    link = tmp_path / "url"
    os.symlink(target, link)
    url = "https://x.test/setup#t=secretish"
    assert bootstrap.write_url_file(link, url) is False
    assert target.read_text(encoding="utf-8") == "untouched"
    assert url not in caplog.text


def test_unwritable_location_is_reported_without_the_url(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    url = "https://x.test/setup#t=secretish"
    assert bootstrap.write_url_file(blocker / "sub" / "url", url) is False
    assert url not in caplog.text
    assert "could not write the setup URL file" in caplog.text


def test_api_startup_announcement_never_blocks_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from sqlalchemy.exc import OperationalError

    from app import cli

    def unreachable(_settings: Settings) -> str | None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(bootstrap, "announce_on_startup", unreachable)
    cli._announce_bootstrap(Settings())  # logs, does not raise
    assert "could not check whether first-run setup is pending" in caplog.text

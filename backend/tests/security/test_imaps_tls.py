# SPDX-License-Identifier: AGPL-3.0-or-later
"""IMAPS with certificate verification, no STARTTLS, egress proxy (SECURITY.md D5, H2).

Real sockets against the in-process TLS IMAP server of
``tests/fixtures/fake_imap.py``, whose certificate comes from a synthetic CA:

* the production context (:func:`app.ingestion.intake.email.client_ssl_context`,
  the system trust store) refuses it: ``tls_failed``, and no credentials are
  sent;
* a context that trusts the test CA accepts it, but not for another name;
* a plaintext server on the IMAPS port is refused (the client never falls
  back to plain IMAP and never sends ``STARTTLS``);
* through a ``CONNECT`` proxy the tunnel names the host (TLS end to end);
  a refusing proxy is ``blocked``;
* loopback is refused for a direct connection unless explicitly allowed.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest

from app.core.http import IPAddress
from app.ingestion.intake.email import ConnectOptions, MailboxConfig, check_connection, connect
from tests.fixtures.fake_imap import (
    IMAP_HOST,
    PASSWORD,
    USERNAME,
    FakeImapServer,
    ImapPki,
    make_pki,
    running_imap,
    running_proxy,
)

pytestmark = pytest.mark.security


def _loopback(host: str, port: int) -> list[IPAddress]:
    return [ipaddress.ip_address("127.0.0.1")]


@pytest.fixture
def pki(tmp_path: Path) -> ImapPki:
    return make_pki(tmp_path / "pki")


@pytest.fixture
def server(pki: ImapPki) -> Iterator[FakeImapServer]:
    with running_imap(pki) as srv:
        yield srv


def _config(port: int, *, password: str = PASSWORD, host: str = IMAP_HOST) -> MailboxConfig:
    return MailboxConfig(host=host, port=port, username=USERNAME, password=password)


def _options(pki: ImapPki | None, **kwargs: object) -> ConnectOptions:
    base: dict[str, object] = {
        "proxy": None,
        "allow_private": True,
        "allow_loopback": True,
        "resolver": _loopback,
        "timeout_seconds": 10.0,
    }
    if pki is not None:
        base["ssl_context_factory"] = pki.client_context
    base.update(kwargs)
    return ConnectOptions(**base)  # type: ignore[arg-type]


def test_trusted_certificate_logs_in(pki: ImapPki, server: FakeImapServer) -> None:
    result = check_connection(_config(server.port), _options(pki))
    assert result.outcome == "ok"
    assert result.messages == 0
    assert result.move_supported is True
    assert not any("STARTTLS" in c for c in server.commands)
    assert any(c.endswith("LOGIN receipts@perfin.test ***") for c in server.commands)


def test_untrusted_certificate_is_refused_before_login(server: FakeImapServer) -> None:
    # The production context: the synthetic CA is not in the system store.
    result = check_connection(_config(server.port), _options(None))
    assert result.outcome == "tls_failed"
    assert not any("LOGIN" in c for c in server.commands)


def test_certificate_for_another_name_is_refused(tmp_path: Path) -> None:
    other = make_pki(tmp_path / "other", host="mail.elsewhere.test")
    with running_imap(other) as srv:
        result = check_connection(_config(srv.port), _options(other))
    assert result.outcome == "tls_failed"
    assert not srv.commands


def test_wrong_password_is_auth_failed(pki: ImapPki, server: FakeImapServer) -> None:
    result = check_connection(_config(server.port, password="nope"), _options(pki))
    assert result.outcome == "auth_failed"


def test_missing_inbox_is_folder_missing(pki: ImapPki, server: FakeImapServer) -> None:
    config = MailboxConfig(
        host=IMAP_HOST, port=server.port, username=USERNAME, password=PASSWORD, inbox_folder="Nope"
    )
    assert check_connection(config, _options(pki)).outcome == "folder_missing"


def test_plaintext_server_is_never_spoken_to_in_the_clear(pki: ImapPki) -> None:
    """A server that answers in plain IMAP: the TLS handshake fails, nothing else is sent."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    received: list[bytes] = []

    def serve() -> None:
        with suppress(OSError):
            conn, _ = listener.accept()
            conn.settimeout(5)
            conn.sendall(b"* OK [CAPABILITY IMAP4rev1 STARTTLS] plain server\r\n")
            with suppress(OSError):
                while chunk := conn.recv(4096):
                    received.append(chunk)
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        result = check_connection(_config(listener.getsockname()[1]), _options(pki))
    finally:
        listener.close()
        thread.join(timeout=5)
    assert result.outcome == "tls_failed"
    data = b"".join(received)
    # Only a TLS ClientHello (record type 0x16) was sent: no IMAP command in clear.
    assert data[:1] == b"\x16"
    assert b"LOGIN" not in data
    assert b"STARTTLS" not in data
    assert b"CAPABILITY" not in data


def test_through_connect_proxy(pki: ImapPki, server: FakeImapServer) -> None:
    with running_proxy({f"{IMAP_HOST}:993": ("127.0.0.1", server.port)}) as proxy:
        # The resolver must not be used: the proxy resolves the name.
        def no_dns(host: str, port: int) -> list[IPAddress]:
            raise AssertionError("resolved locally despite the proxy")

        options = _options(
            pki, proxy=proxy.url, allow_private=False, allow_loopback=False, resolver=no_dns
        )
        result = check_connection(_config(993), options)
    assert result.outcome == "ok"
    assert proxy.requests == [f"CONNECT {IMAP_HOST}:993 HTTP/1.1"]


def test_proxy_refusal_is_blocked(pki: ImapPki, server: FakeImapServer) -> None:
    with running_proxy({}, refuse=True) as proxy:
        result = check_connection(_config(993), _options(pki, proxy=proxy.url))
    assert result.outcome == "blocked"
    assert not server.commands


def test_tls_through_proxy_still_verifies(server: FakeImapServer) -> None:
    with running_proxy({f"{IMAP_HOST}:993": ("127.0.0.1", server.port)}) as proxy:
        result = check_connection(_config(993), _options(None, proxy=proxy.url))
    assert result.outcome == "tls_failed"


def test_loopback_needs_explicit_permission(pki: ImapPki, server: FakeImapServer) -> None:
    options = _options(pki, allow_loopback=False)
    assert check_connection(_config(server.port), options).outcome == "blocked"
    assert not server.commands


def test_connect_returns_a_logged_in_session(pki: ImapPki, server: FakeImapServer) -> None:
    server.deliver(b"Received: x\r\nFrom: a@b.invalid\r\nSubject: s\r\n\r\nbody\r\n")
    with connect(_config(server.port), _options(pki)) as mailbox:
        assert mailbox.select("INBOX") == 1
        uids = mailbox.uids(10)
        fetched = mailbox.fetch_header(uids[0])
        assert fetched is not None
        assert fetched.header.endswith(b"\r\n\r\n")
        assert fetched.size > len(fetched.header)
    assert any("LOGOUT" in c for c in server.commands)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notification channel adapters against local fake services (docs/security/notifications.md).

* webhook: HTTPS only, body ``{id, kind, title, message, sent_at}`` and a
  verifiable HMAC-SHA256 signature over ``"<timestamp>." + body``;
* ntfy, Gotify, Matrix: the documented request, credentials in headers;
* every HTTPS channel: certificate verified (an unknown CA is
  ``tls_failed``), loopback/private addresses refused unless allowed,
  instance host allowlist enforced, redirects refused, retryable vs
  permanent outcomes;
* SMTP: STARTTLS required before ``AUTH`` and the message (a server that
  does not offer it gets nothing), implicit TLS, certificate verified,
  outcome codes;
* the routing table: every routed kind is subscribable and every sentence
  is generic.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from app.core.config_check import check_config
from app.core.settings import Settings
from app.domain.notifications.routing import KINDS, ROUTES, TEST_MESSAGE
from app.domain.notifications.schemas import ChannelCreate, Outcome
from app.integrations.notifications.base import (
    OUTCOMES,
    RETRYABLE,
    ChannelError,
    DeliveryOptions,
    Notification,
)
from app.integrations.notifications.email import SmtpChannel
from app.integrations.notifications.web import (
    GotifyChannel,
    MatrixChannel,
    NtfyChannel,
    WebhookChannel,
    signature,
)
from tests.fixtures.fake_imap import ImapPki, make_pki
from tests.fixtures.fake_notify import (
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_USER,
    WEB_HOST,
    FakeHttpsServer,
    local_options,
    running_https,
    running_smtp,
)

SECRET = "synthetic-signing-secret-for-tests"


def _note(kind: str = "account_locked") -> Notification:
    route = next(r for r in ROUTES.values() if r.kind == kind)
    return Notification(
        id=uuid.uuid4(), kind=kind, message=route.message, sent_at=datetime.now(UTC)
    )


@pytest.fixture
def web_pki(tmp_path: Path) -> ImapPki:
    return make_pki(tmp_path / "web", WEB_HOST)


@pytest.fixture
def smtp_pki(tmp_path: Path) -> ImapPki:
    return make_pki(tmp_path / "smtp", SMTP_HOST)


@pytest.fixture
def https(web_pki: ImapPki) -> Iterator[FakeHttpsServer]:
    with running_https(web_pki) as server:
        yield server


def _code(exc: pytest.ExceptionInfo[ChannelError]) -> str:
    return exc.value.code


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def test_webhook_body_and_signature(https: FakeHttpsServer, web_pki: ImapPki) -> None:
    note = _note()
    channel = WebhookChannel(
        url=https.url("/hook?x=1"),
        secret=SECRET,
        options=local_options(web_pki),
        clock=lambda: 1_700_000_000.0,
    )
    channel.send(note)
    (request,) = https.requests
    assert request.method == "POST"
    assert request.path == "/hook?x=1"
    body = json.loads(request.body)
    assert set(body) == {"id", "kind", "title", "message", "sent_at"}
    assert body["id"] == str(note.id)
    assert body["kind"] == "account_locked"
    assert body["title"] == "PerFin"
    headers = request.headers
    assert headers["x-perfin-timestamp"] == "1700000000"
    assert headers["x-perfin-delivery"] == str(note.id)
    assert headers["x-perfin-event"] == "account_locked"
    expected = hmac.new(SECRET.encode(), b"1700000000." + request.body, hashlib.sha256).hexdigest()
    assert headers["x-perfin-signature"] == f"v1={expected}"
    assert signature(SECRET, 1700000000, request.body) == f"v1={expected}"
    # A tampered body or another secret does not verify.
    assert signature(SECRET, 1700000000, request.body + b" ") != headers["x-perfin-signature"]
    assert signature("other", 1700000000, request.body) != headers["x-perfin-signature"]
    assert SECRET not in json.dumps(headers)


def test_webhook_refuses_plain_http(web_pki: ImapPki) -> None:
    channel = WebhookChannel(
        url="http://notify.perfin.test/x", secret=SECRET, options=local_options(web_pki)
    )
    with pytest.raises(ChannelError) as exc:
        channel.send(_note())
    assert _code(exc) == "blocked"


def test_untrusted_certificate_is_refused(https: FakeHttpsServer, tmp_path: Path) -> None:
    other = make_pki(tmp_path / "other", WEB_HOST)  # a CA the server was not issued by
    channel = WebhookChannel(url=https.url("/hook"), secret=SECRET, options=local_options(other))
    with pytest.raises(ChannelError) as exc:
        channel.send(_note())
    assert _code(exc) == "tls_failed"
    assert https.requests == []


def test_system_trust_store_does_not_know_the_test_ca(
    https: FakeHttpsServer, web_pki: ImapPki
) -> None:
    options = local_options(web_pki, ssl_context_factory=DeliveryOptions().ssl_context_factory)
    with pytest.raises(ChannelError) as exc:
        WebhookChannel(url=https.url("/hook"), secret=SECRET, options=options).send(_note())
    assert _code(exc) == "tls_failed"


def test_loopback_and_private_addresses_are_refused(
    https: FakeHttpsServer, web_pki: ImapPki
) -> None:
    for options in (
        local_options(web_pki, allow_loopback=False),
        local_options(
            web_pki,
            allow_loopback=False,
            resolver=lambda host, port: [ipaddress.ip_address("10.1.2.3")],
        ),
        local_options(
            web_pki, resolver=lambda host, port: [ipaddress.ip_address("169.254.169.254")]
        ),
    ):
        with pytest.raises(ChannelError) as exc:
            WebhookChannel(url=https.url("/hook"), secret=SECRET, options=options).send(_note())
        assert _code(exc) == "blocked"
    assert https.requests == []


def test_private_addresses_only_for_listed_hosts() -> None:
    settings = Settings.model_validate(
        {
            "notifications_allowed_hosts": "gotify.lan.test, ntfy.example.org:8443",
            "notifications_allow_private_hosts": True,
        }
    )
    options = DeliveryOptions.from_settings(settings)
    assert options.host_allowed("GOTIFY.lan.test", 443)
    assert options.host_allowed("ntfy.example.org", 8443)
    assert not options.host_allowed("ntfy.example.org", 443)
    assert not options.host_allowed("evil.example.net", 443)
    assert options.private_allowed("gotify.lan.test", 443)
    assert not options.private_allowed("evil.example.net", 443)
    assert not options.allow_loopback  # loopback needs dev mode too
    open_options = DeliveryOptions.from_settings(Settings.model_validate({}))
    assert open_options.host_allowed("anything.example.org", 443)
    assert not open_options.private_allowed("anything.example.org", 443)


def test_instance_allowlist_blocks_other_hosts(https: FakeHttpsServer, web_pki: ImapPki) -> None:
    options = local_options(web_pki, allowed_hosts=("hooks.example.org",))
    with pytest.raises(ChannelError) as exc:
        WebhookChannel(url=https.url("/hook"), secret=SECRET, options=options).send(_note())
    assert _code(exc) == "blocked"
    allowed = local_options(web_pki, allowed_hosts=(WEB_HOST,))
    WebhookChannel(url=https.url("/hook"), secret=SECRET, options=allowed).send(_note())
    assert len(https.requests) == 1


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (503, "server_error"),
        (429, "server_error"),
        (400, "rejected"),
        (404, "rejected"),
        (401, "auth_failed"),
    ],
)
def test_status_outcomes(https: FakeHttpsServer, web_pki: ImapPki, status: int, code: str) -> None:
    https.statuses.append(status)
    with pytest.raises(ChannelError) as exc:
        WebhookChannel(url=https.url("/hook"), secret=SECRET, options=local_options(web_pki)).send(
            _note()
        )
    assert _code(exc) == code
    assert exc.value.retryable is (code in RETRYABLE)


def test_redirects_are_not_followed(https: FakeHttpsServer, web_pki: ImapPki) -> None:
    https.redirect_to = "https://elsewhere.example.org/steal"
    with pytest.raises(ChannelError) as exc:
        WebhookChannel(url=https.url("/hook"), secret=SECRET, options=local_options(web_pki)).send(
            _note()
        )
    assert _code(exc) == "blocked"
    assert len(https.requests) == 1


def test_unreachable_service_is_retryable(web_pki: ImapPki) -> None:
    def no_dns(host: str, port: int) -> list[ipaddress.IPv4Address]:
        raise OSError("no")

    options = local_options(web_pki, resolver=no_dns)
    with pytest.raises(ChannelError) as exc:
        WebhookChannel(url="https://notify.perfin.test:9/x", secret=SECRET, options=options).send(
            _note()
        )
    assert exc.value.retryable


# ---------------------------------------------------------------------------
# ntfy, Gotify, Matrix
# ---------------------------------------------------------------------------


def test_ntfy_request(https: FakeHttpsServer, web_pki: ImapPki) -> None:
    note = _note("tax_reminder")
    NtfyChannel(
        server_url=https.url(),
        topic="perfin-alerts_1",
        token="tk_synthetic",
        priority=4,
        options=local_options(web_pki),
    ).send(note)
    (request,) = https.requests
    assert (request.method, request.path) == ("POST", "/perfin-alerts_1")
    assert request.body.decode() == note.message
    assert request.headers["title"] == "PerFin"
    assert request.headers["priority"] == "4"
    assert request.headers["authorization"] == "Bearer tk_synthetic"


def test_gotify_request(https: FakeHttpsServer, web_pki: ImapPki) -> None:
    note = _note("backup_failed")
    GotifyChannel(
        server_url=https.url("/gotify"), token="AppTokenSynthetic", options=local_options(web_pki)
    ).send(note)
    (request,) = https.requests
    assert (request.method, request.path) == ("POST", "/gotify/message")
    assert request.headers["x-gotify-key"] == "AppTokenSynthetic"
    assert json.loads(request.body) == {"title": "PerFin", "message": note.message, "priority": 5}


def test_matrix_request_is_idempotent_per_notification(
    https: FakeHttpsServer, web_pki: ImapPki
) -> None:
    note = _note("role_changed")
    channel = MatrixChannel(
        homeserver_url=https.url(),
        room_id="!roomid:matrix.perfin.test",
        access_token="syt_synthetic",
        options=local_options(web_pki),
    )
    channel.send(note)
    channel.send(note)
    first, second = https.requests
    assert first.method == "PUT"
    assert first.path == (
        f"/_matrix/client/v3/rooms/%21roomid%3Amatrix.perfin.test/send/m.room.message/{note.id}"
    )
    assert second.path == first.path  # same transaction id: the homeserver dedupes
    assert first.headers["authorization"] == "Bearer syt_synthetic"
    assert json.loads(first.body) == {"msgtype": "m.notice", "body": f"PerFin: {note.message}"}


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------


def _smtp(port: int, pki: ImapPki, **kwargs: object) -> SmtpChannel:
    values: dict[str, object] = {
        "host": SMTP_HOST,
        "port": port,
        "tls": "starttls",
        "sender": "perfin@perfin.test",
        "recipients": ("owner@perfin.test", "second@perfin.test"),
        "username": SMTP_USER,
        "password": SMTP_PASSWORD,
        "options": local_options(pki),
    }
    values.update(kwargs)
    return SmtpChannel(**values)  # type: ignore[arg-type]


def test_smtp_starttls_before_auth_and_message(smtp_pki: ImapPki) -> None:
    note = _note("recovery_code_used")
    with running_smtp(smtp_pki) as server:
        _smtp(server.port, smtp_pki).send(note)
    assert server.record.auth == [(SMTP_USER, SMTP_PASSWORD, True)]
    ((sender, rcpts, data, tls),) = server.record.mails
    assert tls
    assert sender == "perfin@perfin.test"
    assert rcpts == ["owner@perfin.test", "second@perfin.test"]
    text = data.decode()
    assert "Subject: PerFin" in text
    assert note.message in text
    commands = [c.split(" ")[0].upper() for c in server.record.commands]
    assert commands.index("STARTTLS") < commands.index("AUTH") < commands.index("MAIL")


def test_smtp_without_starttls_sends_nothing(smtp_pki: ImapPki) -> None:
    with running_smtp(smtp_pki, mode="plain") as server, pytest.raises(ChannelError) as exc:
        _smtp(server.port, smtp_pki).send(_note())
    assert _code(exc) == "tls_failed"
    assert server.record.auth == []
    assert server.record.mails == []


def test_smtp_implicit_tls(smtp_pki: ImapPki) -> None:
    with running_smtp(smtp_pki, mode="implicit") as server:
        _smtp(server.port, smtp_pki, tls="implicit").send(_note())
    assert len(server.record.mails) == 1
    assert server.record.mails[0][3] is True


def test_smtp_certificate_is_verified(smtp_pki: ImapPki, tmp_path: Path) -> None:
    other = make_pki(tmp_path / "other-smtp", SMTP_HOST)
    for mode in ("starttls", "implicit"):
        with running_smtp(smtp_pki, mode=mode) as server, pytest.raises(ChannelError) as exc:
            _smtp(server.port, smtp_pki, tls=mode, options=local_options(other)).send(_note())
        assert _code(exc) == "tls_failed"
        assert server.record.auth == []
        assert server.record.mails == []


def test_smtp_outcomes(smtp_pki: ImapPki) -> None:
    with running_smtp(smtp_pki) as server, pytest.raises(ChannelError) as exc:
        _smtp(server.port, smtp_pki, password="wrong").send(_note())
    assert _code(exc) == "auth_failed"
    with (
        running_smtp(smtp_pki, mail_reply="451 Try later") as server,
        pytest.raises(ChannelError) as exc,
    ):
        _smtp(server.port, smtp_pki).send(_note())
    assert _code(exc) == "server_error"
    assert exc.value.retryable
    with running_smtp(smtp_pki, mail_reply="550 No") as server, pytest.raises(ChannelError) as exc:
        _smtp(server.port, smtp_pki).send(_note())
    assert _code(exc) == "rejected"


def test_smtp_loopback_refused_by_default(smtp_pki: ImapPki) -> None:
    with running_smtp(smtp_pki) as server, pytest.raises(ChannelError) as exc:
        _smtp(server.port, smtp_pki, options=local_options(smtp_pki, allow_loopback=False)).send(
            _note()
        )
    assert _code(exc) == "blocked"
    assert server.record.commands == []


# ---------------------------------------------------------------------------
# Schemas and routing table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        {"type": "webhook", "url": "http://hooks.example.org/x"},
        {"type": "webhook", "url": "https://user:pw@hooks.example.org/x"},
        {"type": "webhook", "url": "https://hooks.example.org/x#frag"},
        {"type": "webhook", "url": "ftp://hooks.example.org/x"},
        {"type": "ntfy", "server_url": "https://ntfy.sh?x=1", "topic": "ok"},
        {"type": "ntfy", "topic": "bad topic"},
        {
            "type": "matrix",
            "homeserver_url": "https://m.org",
            "room_id": "#alias:m.org",
            "access_token": "t",
        },
        {"type": "gotify", "server_url": "https://g.example.org"},
        {"type": "smtp", "host": "smtp.example.org", "sender": "a@b.org", "recipients": []},
        {
            "type": "smtp",
            "host": "smtp.example.org",
            "sender": "a@b.org\r\nBcc: x@y.org",
            "recipients": ["c@d.org"],
        },
        {
            "type": "smtp",
            "host": "smtp.example.org",
            "tls": "none",
            "sender": "a@b.org",
            "recipients": ["c@d.org"],
        },
        {"type": "webhook", "url": "https://hooks.example.org/x", "secret": "mine"},
    ],
    ids=lambda c: (
        f"{c['type']}-{hashlib.sha256(json.dumps(c, sort_keys=True).encode()).hexdigest()[:8]}"
    ),
)
def test_invalid_configurations_are_refused(config: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ChannelCreate.model_validate({"name": "x", "config": config})


def test_routing_table_is_generic() -> None:
    assert {route.kind for route in ROUTES.values()} == set(KINDS)
    assert set(OUTCOMES) == set(get_args(Outcome))
    for text in [*(route.message for route in ROUTES.values()), TEST_MESSAGE]:
        assert not re.search(r"\d|@|\{|https?:", text), text
        assert len(text) < 120


def test_config_check_findings() -> None:
    def notes(**values: object) -> dict[str, str]:
        settings = Settings.model_validate(values)
        return {
            f.code: f.level for f in check_config(settings) if f.code.startswith("notifications.")
        }

    assert notes() == {}
    assert notes(notifications_allow_private_hosts=True) == {
        "notifications.private_without_allowlist": "warning"
    }
    assert (
        notes(
            notifications_allowed_hosts="gotify.lan.test,ntfy.example.org:8443",
            notifications_allow_private_hosts=True,
        )
        == {}
    )
    assert notes(notifications_allowed_hosts="https://x.example.org/") == {
        "notifications.allowed_host_invalid": "error"
    }

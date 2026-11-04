# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTPS notification channels: webhook, ntfy, Gotify and Matrix.

Every request goes through :class:`app.core.http.SafeHttpClient` with an
egress policy of the channel's own ``host:port`` only (https, addresses
vetted and pinned, private addresses only as :class:`~.base.DeliveryOptions`
allows, no redirects, 64 KiB response cap, timeout), tunnelled through
``HTTPS_PROXY`` when that is set (the egress allowlist proxy).

What each one sends (docs/security/notifications.md, "What leaves the host"):

* **webhook** ``POST <url>``, ``Content-Type: application/json``, body
  ``{"id", "kind", "title", "message", "sent_at"}``; headers
  ``X-PerFin-Delivery`` (the id), ``X-PerFin-Event`` (the kind),
  ``X-PerFin-Timestamp`` (Unix seconds) and ``X-PerFin-Signature:
  v1=<hex>``, the HMAC-SHA256 of ``"<timestamp>." + body`` under the
  channel's signing secret (:func:`signature`). A receiver recomputes it,
  compares in constant time and refuses timestamps older than a few
  minutes (replay).
* **ntfy** ``POST <server>/<topic>``, body the message, headers ``Title``,
  ``Priority``, ``Tags: perfin`` and, with an access token,
  ``Authorization: Bearer``.
* **Gotify** ``POST <server>/message``, header ``X-Gotify-Key`` (the
  application token), JSON ``{"title", "message", "priority"}``.
* **Matrix** ``PUT <homeserver>/_matrix/client/v3/rooms/<room>/send/
  m.room.message/<id>`` with ``Authorization: Bearer <access token>``, JSON
  ``{"msgtype": "m.notice", "body"}``. The transaction id is the
  notification id, so a retry is not posted twice.

Credentials are only ever put in headers, never in URLs (except the ntfy
topic, which is the topic's name), and are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import quote

import httpx

from app.core.http import (
    EgressBlockedError,
    EgressError,
    EgressPolicy,
    PinnedTransport,
    SafeHttpClient,
    url_host_key,
)
from app.integrations.notifications.base import (
    MAX_RESPONSE_BYTES,
    ChannelError,
    DeliveryOptions,
    Notification,
)

SIGNATURE_VERSION: Final = "v1"
_RETRY_STATUSES: Final = frozenset({408, 425, 429})


def signature(secret: str, timestamp: int, body: bytes) -> str:
    """``v1=<hex HMAC-SHA256(secret, "<timestamp>." + body)>``."""
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("ascii") + body, hashlib.sha256)
    return f"{SIGNATURE_VERSION}={mac.hexdigest()}"


def _caused_by(exc: BaseException, kinds: tuple[type[BaseException], ...]) -> bool:
    seen: BaseException | None = exc
    for _ in range(8):
        if seen is None:
            return False
        if isinstance(seen, kinds):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _status_outcome(status: int) -> None:
    if 200 <= status < 300:
        return
    if status in (401, 403):
        raise ChannelError("auth_failed")
    if status >= 500 or status in _RETRY_STATUSES:
        raise ChannelError("server_error")
    if 400 <= status < 500:
        raise ChannelError("rejected")
    raise ChannelError("protocol_error")


def deliver(
    options: DeliveryOptions,
    method: str,
    url: str,
    *,
    body: bytes,
    headers: Mapping[str, str],
) -> int:
    """Send one request under the channel's egress policy; return the status."""
    parsed = httpx.URL(url)
    if parsed.scheme != "https" or not parsed.host:
        raise ChannelError("blocked")
    port = parsed.port or 443
    options.check_host(parsed.host, port)
    key = url_host_key(parsed)
    policy = EgressPolicy(
        allowed_hosts=frozenset({key}),
        private_hosts=frozenset({key})
        if options.private_allowed(parsed.host, port)
        else frozenset(),
        allow_loopback=options.allow_loopback,
        max_response_bytes=MAX_RESPONSE_BYTES,
        timeout_seconds=options.timeout_seconds,
        max_redirects=0,
    )
    transport = PinnedTransport(
        policy,
        resolver=options.resolver,
        proxy=options.proxy,
        ssl_context=options.ssl_context_factory(),
    )
    try:
        with SafeHttpClient(policy, transport=transport) as client:
            result = client.send(method, url, content=body, headers=headers)
    except EgressBlockedError:
        raise ChannelError("blocked") from None
    except EgressError as exc:
        if _caused_by(exc, (ssl.SSLError, ssl.CertificateError)):
            raise ChannelError("tls_failed") from None
        if _caused_by(exc, (httpx.TimeoutException, TimeoutError)):
            raise ChannelError("timeout") from None
        raise ChannelError("connect_failed") from None
    except OSError:
        raise ChannelError("connect_failed") from None
    finally:
        transport.close()
    _status_outcome(result.status_code)
    return result.status_code


def _base(url: str) -> str:
    return url.rstrip("/")


@dataclass(frozen=True)
class WebhookChannel:
    url: str
    secret: str = field(repr=False)
    options: DeliveryOptions = field(default_factory=DeliveryOptions)
    clock: Callable[[], float] = field(default=time.time, repr=False, compare=False)

    def payload(self, notification: Notification) -> bytes:
        data = {
            "id": str(notification.id),
            "kind": notification.kind,
            "title": notification.title,
            "message": notification.message,
            "sent_at": notification.sent_at.isoformat(),
        }
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def send(self, notification: Notification) -> None:
        body = self.payload(notification)
        timestamp = int(self.clock())
        headers = {
            "Content-Type": "application/json",
            "X-PerFin-Delivery": str(notification.id),
            "X-PerFin-Event": notification.kind,
            "X-PerFin-Timestamp": str(timestamp),
            "X-PerFin-Signature": signature(self.secret, timestamp, body),
        }
        deliver(self.options, "POST", self.url, body=body, headers=headers)


@dataclass(frozen=True)
class NtfyChannel:
    server_url: str
    topic: str = field(repr=False)
    token: str | None = field(default=None, repr=False)
    priority: int = 3
    options: DeliveryOptions = field(default_factory=DeliveryOptions)

    def send(self, notification: Notification) -> None:
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": notification.title,
            "Priority": str(self.priority),
            "Tags": "perfin",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        url = f"{_base(self.server_url)}/{quote(self.topic, safe='')}"
        deliver(
            self.options, "POST", url, body=notification.message.encode("utf-8"), headers=headers
        )


@dataclass(frozen=True)
class GotifyChannel:
    server_url: str
    token: str = field(repr=False)
    priority: int = 5
    options: DeliveryOptions = field(default_factory=DeliveryOptions)

    def send(self, notification: Notification) -> None:
        body = json.dumps(
            {
                "title": notification.title,
                "message": notification.message,
                "priority": self.priority,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Gotify-Key": self.token}
        deliver(
            self.options, "POST", f"{_base(self.server_url)}/message", body=body, headers=headers
        )


@dataclass(frozen=True)
class MatrixChannel:
    homeserver_url: str
    room_id: str
    access_token: str = field(repr=False)
    options: DeliveryOptions = field(default_factory=DeliveryOptions)

    def send(self, notification: Notification) -> None:
        body = json.dumps(
            {"msgtype": "m.notice", "body": f"{notification.title}: {notification.message}"},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        url = (
            f"{_base(self.homeserver_url)}/_matrix/client/v3/rooms/"
            f"{quote(self.room_id, safe='')}/send/m.room.message/{notification.id}"
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }
        deliver(self.options, "PUT", url, body=body, headers=headers)

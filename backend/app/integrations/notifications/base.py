# SPDX-License-Identifier: AGPL-3.0-or-later
"""Channel protocol, outcomes and the egress policy of notification channels.

What a channel sends is a :class:`Notification`: an id (used as the Matrix
transaction id and the webhook ``id``, so a retried delivery is recognisable),
a ``kind`` from a fixed list, a fixed title and one generic sentence. Nothing
about who did what, no amounts, no names (docs/security/notifications.md).

**Outcomes.** A delivery ends in ``ok`` or raises :class:`ChannelError` with
a short code (never the server's reply, never a URL or credential):

======================  ==========  ===========================================
code                    retried     meaning
======================  ==========  ===========================================
``blocked``             no          host not allowed (allowlist, address class)
``tls_failed``          no          certificate or TLS negotiation refused
``auth_failed``         no          the service refused the credentials
``rejected``            no          the service refused the message (4xx)
``connect_failed``      yes         could not reach the service
``timeout``             yes         the service did not answer in time
``server_error``        yes         5xx, 408, 425 or 429
``protocol_error``      no          an answer PerFin could not use
======================  ==========  ===========================================

**Egress** (:class:`DeliveryOptions`): https only, one host per channel,
every resolved address vetted and the connection pinned to it (no private,
loopback, link-local/metadata addresses unless allowed below), no
redirects on the POST/PUT, a small response cap and a timeout; through
``HTTPS_PROXY`` (the egress allowlist proxy) when set. An instance
administrator can restrict destinations to ``PERFIN_NOTIFICATIONS_ALLOWED_HOSTS``;
private addresses are allowed only for hosts on that list and only with
``PERFIN_NOTIFICATIONS_ALLOW_PRIVATE_HOSTS``.
"""

from __future__ import annotations

import enum
import ssl
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from app.core.http import ProxySetting, Resolver, host_key, system_resolver
from app.core.settings import Settings

TITLE: Final = "PerFin"
#: Response bodies are never used beyond a status; keep them small.
MAX_RESPONSE_BYTES: Final = 64 * 1024


class ChannelType(enum.StrEnum):
    SMTP = "smtp"
    WEBHOOK = "webhook"
    NTFY = "ntfy"
    GOTIFY = "gotify"
    MATRIX = "matrix"


@dataclass(frozen=True)
class Notification:
    """One generic message (see module doc)."""

    id: uuid.UUID
    kind: str
    message: str
    sent_at: datetime
    title: str = TITLE


RETRYABLE: Final = frozenset({"connect_failed", "timeout", "server_error"})
OUTCOMES: Final = (
    "ok",
    "blocked",
    "tls_failed",
    "auth_failed",
    "rejected",
    "connect_failed",
    "timeout",
    "server_error",
    "protocol_error",
)


class ChannelError(Exception):
    """Delivery failed with ``code`` (module doc); the message is safe to log."""

    def __init__(self, code: str) -> None:
        if code not in OUTCOMES or code == "ok":
            raise ValueError(f"unknown outcome {code!r}")
        super().__init__(code)
        self.code = code

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE


class Channel(Protocol):
    """Anything that can deliver one :class:`Notification`."""

    def send(self, notification: Notification) -> None: ...


def default_ssl_context() -> ssl.SSLContext:
    """System trust store, host name checked, TLS 1.2 or later."""
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _normalise_host(entry: str) -> str:
    return entry.strip().strip("[]").lower().rstrip(".")


@dataclass(frozen=True)
class DeliveryOptions:
    """How channels reach their services (module doc, "Egress")."""

    #: Instance allowlist: host names (any port) or ``host:port`` keys; empty = any host.
    allowed_hosts: tuple[str, ...] = ()
    allow_private: bool = False
    allow_loopback: bool = False
    timeout_seconds: float = 10.0
    proxy: ProxySetting = "env"
    resolver: Resolver = system_resolver
    ssl_context_factory: Callable[[], ssl.SSLContext] = field(default=default_ssl_context)

    @classmethod
    def from_settings(cls, settings: Settings) -> DeliveryOptions:
        allow_private = settings.notifications_allow_private_hosts
        return cls(
            allowed_hosts=tuple(_normalise_host(h) for h in settings.notifications_allowed_hosts),
            allow_private=allow_private,
            allow_loopback=allow_private and settings.dev_mode,
            timeout_seconds=float(settings.notifications_timeout_seconds),
        )

    def _listed(self, host: str, port: int) -> bool:
        name = _normalise_host(host)
        return name in self.allowed_hosts or host_key(name, port) in self.allowed_hosts

    def host_allowed(self, host: str, port: int) -> bool:
        """The instance allowlist admits ``host:port`` (always, when the list is empty)."""
        return not self.allowed_hosts or self._listed(host, port)

    def private_allowed(self, host: str, port: int) -> bool:
        """Private addresses only for explicitly listed hosts (never by default)."""
        return self.allow_private and self._listed(host, port)

    def check_host(self, host: str, port: int) -> None:
        if not self.host_allowed(host, port):
            raise ChannelError("blocked")

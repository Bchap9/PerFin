# SPDX-License-Identifier: AGPL-3.0-or-later
"""A household's SMTP notification channel.

* **TLS is mandatory and verified**: ``starttls`` requires the server to
  offer ``STARTTLS`` and upgrades before ``AUTH`` or any message (a stripped
  advertisement fails closed with ``tls_failed``); ``implicit`` speaks TLS
  from the first byte. Certificates and host names are checked against the
  system trust store, TLS 1.2 minimum (:func:`~.base.default_ssl_context`).
* **The connection is vetted** like every other egress: the host must pass
  the instance allowlist, every resolved address is checked (no loopback,
  link-local/metadata, private unless allowed for that host) and the socket
  is pinned to the checked address, or tunnelled through ``HTTPS_PROXY``
  (``CONNECT host:port``, the egress allowlist proxy) when one is set
  (:func:`app.ingestion.intake.email.open_socket`, shared with the IMAP
  intake).
* The message is :func:`~.smtp.build_message` (no CR/LF in headers):
  ``Subject: PerFin`` and the generic sentence; nothing else.
"""

from __future__ import annotations

import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from typing import Literal

from app.core.http import EgressBlockedError, EgressError
from app.ingestion.intake.email import ConnectOptions, open_socket
from app.integrations.notifications.base import ChannelError, DeliveryOptions, Notification
from app.integrations.notifications.smtp import MailError, build_message

TlsMode = Literal["starttls", "implicit"]


def _connect_options(options: DeliveryOptions, host: str, port: int) -> ConnectOptions:
    return ConnectOptions(
        timeout_seconds=options.timeout_seconds,
        proxy=options.proxy,
        allow_private=options.private_allowed(host, port),
        allow_loopback=options.allow_loopback,
        resolver=options.resolver,
        ssl_context_factory=options.ssl_context_factory,
    )


class _VettedSMTP(smtplib.SMTP):
    """``SMTP`` whose TCP connection comes from :func:`open_socket`."""

    def __init__(self, host: str, port: int, *, connect: ConnectOptions) -> None:
        self._connect_options = connect
        super().__init__(host, port, timeout=connect.timeout_seconds)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return open_socket(host, port, self._connect_options)


class _VettedSMTPSSL(smtplib.SMTP_SSL):
    """``SMTP_SSL`` over a vetted socket, wrapped with the verifying context."""

    def __init__(
        self, host: str, port: int, *, connect: ConnectOptions, context: ssl.SSLContext
    ) -> None:
        self._connect_options = connect
        super().__init__(host, port, timeout=connect.timeout_seconds, context=context)

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        raw = open_socket(host, port, self._connect_options)
        try:
            return self.context.wrap_socket(raw, server_hostname=host)
        except BaseException:
            raw.close()
            raise


def _classify(exc: BaseException) -> ChannelError:
    if isinstance(exc, ChannelError):
        return exc
    if isinstance(exc, EgressBlockedError):
        return ChannelError("blocked")
    if isinstance(exc, ssl.SSLError | ssl.CertificateError):
        return ChannelError("tls_failed")
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ChannelError("auth_failed")
    if isinstance(exc, smtplib.SMTPResponseException):
        return ChannelError("server_error" if 400 <= exc.smtp_code < 500 else "rejected")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _ in exc.recipients.values()]
        return ChannelError("server_error" if codes and min(codes) < 500 else "rejected")
    if isinstance(exc, TimeoutError | socket.timeout):
        return ChannelError("timeout")
    if isinstance(exc, smtplib.SMTPServerDisconnected | EgressError | OSError):
        return ChannelError("connect_failed")
    return ChannelError("protocol_error")


@dataclass(frozen=True)
class SmtpChannel:
    host: str
    port: int
    tls: TlsMode
    sender: str
    recipients: tuple[str, ...]
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    options: DeliveryOptions = field(default_factory=DeliveryOptions)

    def _open(self) -> smtplib.SMTP:
        connect = _connect_options(self.options, self.host, self.port)
        context = self.options.ssl_context_factory()
        if self.tls == "implicit":
            return _VettedSMTPSSL(self.host, self.port, connect=connect, context=context)
        client = _VettedSMTP(self.host, self.port, connect=connect)
        try:
            client.ehlo()
            if not client.has_extn("starttls"):
                raise ChannelError("tls_failed")
            client.starttls(context=context)
            client.ehlo()
        except BaseException:
            client.close()
            raise
        return client

    def send(self, notification: Notification) -> None:
        self.options.check_host(self.host, self.port)
        try:
            message = build_message(
                sender=self.sender,
                to=", ".join(self.recipients),
                subject=notification.title,
                body=notification.message + "\n",
            )
        except MailError:
            raise ChannelError("rejected") from None
        try:
            client = self._open()
            try:
                if self.username:
                    client.login(self.username, self.password or "")
                client.send_message(message, from_addr=self.sender, to_addrs=list(self.recipients))
            finally:
                try:
                    client.quit()
                except (smtplib.SMTPException, OSError):
                    client.close()
        except Exception as exc:  # noqa: BLE001 - mapped to an outcome code, nothing echoed
            raise _classify(exc) from None

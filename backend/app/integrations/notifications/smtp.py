# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minimal SMTP sender with mandatory TLS (password reset mail, SECURITY.md A11).

* TLS is never optional: ``PERFIN_SMTP_TLS=starttls`` (port 587) requires
  the server to offer ``STARTTLS`` and upgrades before any credential or
  message is sent (no plaintext fallback, so a stripped ``STARTTLS``
  advertisement fails closed); ``implicit`` (port 465) speaks TLS from the
  first byte. Certificates and host names are verified with the system trust
  store (``ssl.create_default_context``), TLS 1.2 minimum.
* Messages are built with :class:`email.message.EmailMessage` under the
  strict default policy, which refuses CR/LF in header values (no header
  injection through an address or subject).
* Nothing secret is logged: failures are reported by exception class only.

Household SMTP notification channels (:mod:`.email`) reuse
:func:`build_message`; tests replace the reset mailer with a fake through
``app.state.mailer``.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Final, Literal, Protocol

from app.core.settings import Settings

TlsMode = Literal["starttls", "implicit"]
MAX_SUBJECT_CHARS: Final = 200


class MailError(RuntimeError):
    """Sending failed (connection, TLS, authentication or refusal)."""


class TlsUnavailableError(MailError):
    """The server did not offer STARTTLS; nothing was sent."""


class Mailer(Protocol):
    """Anything that can deliver one plain-text message."""

    def send(self, *, to: str, subject: str, body: str) -> None: ...


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    tls: TlsMode
    sender: str
    username: str = ""
    password: str = field(default="", repr=False)
    timeout: float = 10.0

    @classmethod
    def from_settings(cls, settings: Settings) -> SmtpConfig:
        return cls(
            host=settings.smtp_host.strip(),
            port=settings.smtp_port,
            tls=settings.smtp_tls,
            sender=settings.smtp_from.strip(),
            username=settings.smtp_username.strip(),
            password=(
                settings.smtp_password.get_secret_value()
                if settings.smtp_password is not None
                else ""
            ),
            timeout=float(settings.smtp_timeout_seconds),
        )


def tls_context() -> ssl.SSLContext:
    """Verifying client context: system CAs, host name check, TLS >= 1.2."""
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _single_line(value: str, name: str) -> str:
    if "\r" in value or "\n" in value:
        raise MailError(f"{name} must be a single line")
    return value


def build_message(*, sender: str, to: str, subject: str, body: str) -> EmailMessage:
    """A plain-text message; header values may not contain line breaks."""
    message = EmailMessage()
    message["From"] = _single_line(sender, "sender")
    message["To"] = _single_line(to, "recipient")
    message["Subject"] = _single_line(subject[:MAX_SUBJECT_CHARS], "subject")
    domain = sender.rpartition("@")[2] or None
    message["Message-ID"] = make_msgid(domain=domain)
    message.set_content(body)
    return message


class SmtpMailer:
    """Send through the configured relay; TLS always, verified."""

    #: Replaceable in tests (``smtplib.SMTP``/``SMTP_SSL`` compatible).
    smtp_class: type[smtplib.SMTP] = smtplib.SMTP
    smtp_ssl_class: type[smtplib.SMTP_SSL] = smtplib.SMTP_SSL

    def __init__(self, config: SmtpConfig, *, context: ssl.SSLContext | None = None) -> None:
        if not config.host or not config.sender:
            raise MailError("SMTP is not configured")
        self.config = config
        self.context = context or tls_context()

    def _connect(self) -> smtplib.SMTP:
        cfg = self.config
        if cfg.tls == "implicit":
            return self.smtp_ssl_class(
                cfg.host, cfg.port, timeout=cfg.timeout, context=self.context
            )
        client = self.smtp_class(cfg.host, cfg.port, timeout=cfg.timeout)
        try:
            client.ehlo()
            if not client.has_extn("starttls"):
                raise TlsUnavailableError("the SMTP server does not offer STARTTLS")
            client.starttls(context=self.context)
            client.ehlo()
        except BaseException:
            client.close()
            raise
        return client

    def send(self, *, to: str, subject: str, body: str) -> None:
        message = build_message(sender=self.config.sender, to=to, subject=subject, body=body)
        try:
            client = self._connect()
            try:
                if self.config.username:
                    client.login(self.config.username, self.config.password)
                client.send_message(message)
            finally:
                try:
                    client.quit()
                except smtplib.SMTPException:
                    client.close()
        except MailError:
            raise
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise MailError(f"SMTP delivery failed ({type(exc).__name__})") from None


def mailer_from_settings(settings: Settings) -> Mailer | None:
    """An :class:`SmtpMailer` when SMTP is configured, else ``None``."""
    if not settings.smtp_configured:
        return None
    return SmtpMailer(SmtpConfig.from_settings(settings))

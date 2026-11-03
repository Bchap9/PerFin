# SPDX-License-Identifier: AGPL-3.0-or-later
"""SMTP sender: TLS is mandatory and verified; no header injection (A11)."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr

from app.core.config_check import check_config
from app.core.settings import Settings
from app.integrations.notifications.smtp import (
    MailError,
    SmtpConfig,
    SmtpMailer,
    TlsUnavailableError,
    build_message,
    mailer_from_settings,
    tls_context,
)


class FakeSMTP:
    """Records the conversation; ``offers_starttls`` controls the EHLO reply."""

    offers_starttls: ClassVar[bool] = True
    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, host: str, port: int, timeout: float = 0, **kwargs: Any) -> None:
        self.host, self.port, self.timeout, self.kwargs = host, port, timeout, kwargs
        self.calls: list[str] = []
        self.context: ssl.SSLContext | None = kwargs.get("context")
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def has_extn(self, name: str) -> bool:
        return name.lower() == "starttls" and self.offers_starttls

    def starttls(self, context: ssl.SSLContext | None = None) -> None:
        self.calls.append("starttls")
        self.context = context

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}")

    def send_message(self, message: EmailMessage) -> None:
        self.calls.append("send")
        self.sent.append(message)

    def quit(self) -> None:
        self.calls.append("quit")

    def close(self) -> None:
        self.calls.append("close")


class FakeSMTPNoTls(FakeSMTP):
    offers_starttls: ClassVar[bool] = False


CONFIG = SmtpConfig(
    host="smtp.example.test",
    port=587,
    tls="starttls",
    sender="perfin@example.test",
    username="relay-user",
    password="synthetic-relay-secret",
)


def mailer(cls: type[FakeSMTP], config: SmtpConfig = CONFIG) -> SmtpMailer:
    instance = SmtpMailer(config)
    instance.smtp_class = cls  # type: ignore[assignment]
    instance.smtp_ssl_class = cls  # type: ignore[assignment]
    return instance


@pytest.fixture(autouse=True)
def _reset() -> None:
    FakeSMTP.instances.clear()


def test_starttls_happens_before_login_and_send() -> None:
    mailer(FakeSMTP).send(to="user@example.test", subject="Hi", body="Body")
    (conn,) = FakeSMTP.instances
    assert conn.calls == ["ehlo", "starttls", "ehlo", "login:relay-user", "send", "quit"]
    assert conn.context is not None
    assert conn.context.verify_mode == ssl.CERT_REQUIRED
    assert conn.context.check_hostname is True
    assert conn.context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert conn.sent[0]["To"] == "user@example.test"


def test_missing_starttls_fails_closed() -> None:
    with pytest.raises(TlsUnavailableError):
        mailer(FakeSMTPNoTls).send(to="user@example.test", subject="Hi", body="Body")
    (conn,) = FakeSMTPNoTls.instances
    assert "starttls" not in conn.calls
    assert not any(call.startswith("login") or call == "send" for call in conn.calls)


def test_implicit_tls_uses_a_verifying_context() -> None:
    config = SmtpConfig(
        host="smtp.example.test", port=465, tls="implicit", sender="perfin@example.test"
    )
    mailer(FakeSMTP, config).send(to="user@example.test", subject="Hi", body="Body")
    (conn,) = FakeSMTP.instances
    assert conn.context is not None
    assert conn.context.verify_mode == ssl.CERT_REQUIRED
    assert conn.calls == ["send", "quit"]  # no login without a username


def test_transport_errors_become_mail_errors_without_secrets() -> None:
    class Broken(FakeSMTP):
        def login(self, user: str, password: str) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    with pytest.raises(MailError) as info:
        mailer(Broken).send(to="user@example.test", subject="Hi", body="Body")
    assert "synthetic-relay-secret" not in str(info.value)
    assert "SMTPAuthenticationError" in str(info.value)


@pytest.mark.parametrize("field", ["to", "subject"])
def test_header_injection_is_refused(field: str) -> None:
    values = {"sender": "perfin@example.test", "to": "user@example.test", "subject": "Hi"}
    values[field] = values[field] + "\r\nBcc: victim@example.test"
    with pytest.raises(MailError):
        build_message(body="x", **values)


def test_tls_context_defaults() -> None:
    context = tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_mailer_only_when_configured() -> None:
    assert mailer_from_settings(Settings()) is None
    assert mailer_from_settings(Settings(smtp_host="smtp.example.test")) is None
    configured = Settings(smtp_host="smtp.example.test", smtp_from="perfin@example.test")
    assert isinstance(mailer_from_settings(configured), SmtpMailer)
    assert repr(SmtpConfig.from_settings(configured)).count("password") == 0


def test_smtp_config_check_warnings() -> None:
    def codes(**kwargs: Any) -> set[str]:
        return {f.code for f in check_config(Settings(dev_mode=True, **kwargs))}

    assert "smtp.from_missing" in codes(smtp_host="smtp.example.test")
    assert "smtp.host_missing" in codes(smtp_from="perfin@example.test")
    assert "smtp.from_invalid" in codes(smtp_host="smtp.example.test", smtp_from="not-an-address")
    assert "smtp.credentials_incomplete" in codes(smtp_username="relay")
    assert "smtp.credentials_incomplete" in codes(smtp_password=SecretStr("x"))
    assert "smtp.reset_disabled_oidc_only" in codes(
        smtp_host="smtp.example.test",
        smtp_from="perfin@example.test",
        local_login_enabled=False,
        oidc_issuer="https://idp.example.test/",
    )
    assert "hibp.enabled" in codes(hibp_check_enabled=True)
    clean = codes(
        smtp_host="smtp.example.test",
        smtp_from="perfin@example.test",
        smtp_username="relay",
        smtp_password=SecretStr("x"),
    )
    assert not {c for c in clean if c.startswith("smtp.")}


def test_smtp_password_is_secret_and_empty_means_unset() -> None:
    settings = Settings(smtp_password=SecretStr("synthetic-relay-secret"))
    assert "synthetic-relay-secret" not in repr(settings)
    assert Settings(smtp_password="").smtp_password is None  # type: ignore[arg-type]

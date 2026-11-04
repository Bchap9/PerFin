# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local fake services for notification channel tests.

* :class:`FakeHttpsServer`: an HTTPS server (certificate from a synthetic CA,
  :func:`tests.fixtures.fake_imap.make_pki`) that records every request
  (method, path, headers, body) and answers with the statuses queued in
  :attr:`FakeHttpsServer.statuses` (then ``200``). It stands in for a
  webhook receiver, ntfy, Gotify and a Matrix homeserver.
* :class:`FakeSmtpServer`: an ESMTP server speaking ``STARTTLS`` (or
  implicit TLS, or no TLS at all) with ``AUTH PLAIN``; it records whether
  TLS was active when credentials and the message arrived.
* :func:`local_options`: :class:`DeliveryOptions` that resolve the test
  host names to ``127.0.0.1``, trust only the test CA, use no proxy and
  allow loopback (what the production policy refuses).

Everything listens on ``127.0.0.1`` with an OS-assigned port.
"""

from __future__ import annotations

import base64
import ipaddress
import socket
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal

from app.core.http import EgressError, IPAddress
from app.integrations.notifications.base import DeliveryOptions
from tests.fixtures.fake_imap import ImapPki, make_pki

WEB_HOST = "notify.perfin.test"
SMTP_HOST = "smtp.perfin.test"
SMTP_USER = "alerts@perfin.test"
SMTP_PASSWORD = "synthetic smtp password"


def loopback_resolver(host: str, port: int) -> list[IPAddress]:
    if host in (WEB_HOST, SMTP_HOST):
        return [ipaddress.ip_address("127.0.0.1")]
    raise EgressError(f"{host} does not resolve in tests")


def local_options(pki: ImapPki, **overrides: Any) -> DeliveryOptions:
    values: dict[str, Any] = {
        "allow_loopback": True,
        "proxy": None,
        "resolver": loopback_resolver,
        "ssl_context_factory": pki.client_context,
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return DeliveryOptions(**values)


# ---------------------------------------------------------------------------
# HTTPS
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class FakeHttpsServer:
    def __init__(self, pki: ImapPki) -> None:
        self.requests: list[Recorded] = []
        self.statuses: list[int] = []
        self.redirect_to: str | None = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                owner.requests.append(
                    Recorded(
                        self.command,
                        self.path,
                        {k.lower(): v for k, v in self.headers.items()},
                        body,
                    )
                )
                status = owner.statuses.pop(0) if owner.statuses else 200
                if owner.redirect_to is not None:
                    status = 307
                reply = b'{"id":"ok"}'
                self.send_response(status)
                if status == 307 and owner.redirect_to is not None:
                    self.send_header("Location", owner.redirect_to)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            do_POST = _handle  # noqa: N815 - http.server naming
            do_PUT = _handle  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.socket = pki.server_context().wrap_socket(
            self._server.socket, server_side=True
        )
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def url(self, path: str = "") -> str:
        return f"https://{WEB_HOST}:{self.port}{path}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@contextmanager
def running_https(pki: ImapPki) -> Iterator[FakeHttpsServer]:
    server = FakeHttpsServer(pki)
    server.start()
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------

SmtpMode = Literal["starttls", "implicit", "plain"]


@dataclass
class SmtpRecord:
    auth: list[tuple[str, str, bool]] = field(default_factory=list)  # user, password, tls
    mails: list[tuple[str, list[str], bytes, bool]] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    handshake_failures: int = 0


class FakeSmtpServer:
    def __init__(
        self,
        pki: ImapPki,
        *,
        mode: SmtpMode = "starttls",
        mail_reply: str = "250 OK",
    ) -> None:
        self.mode = mode
        self.mail_reply = mail_reply
        self.record = SmtpRecord()
        self._context = pki.server_context()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.1)
        self.port: int = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        with suppress(OSError):
            self._sock.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._client, args=(raw,), daemon=True).start()

    def _client(self, raw: socket.socket) -> None:
        raw.settimeout(10)
        conn: socket.socket = raw
        tls = False
        if self.mode == "implicit":
            try:
                conn = self._context.wrap_socket(raw, server_side=True)
            except (ssl.SSLError, OSError):
                self.record.handshake_failures += 1
                raw.close()
                return
            tls = True
        try:
            self._session(conn, tls)
        except (OSError, ssl.SSLError):
            pass
        finally:
            with suppress(OSError):
                conn.close()

    def _session(self, conn: socket.socket, tls: bool) -> None:
        reader = conn.makefile("rb")

        def send(line: str) -> None:
            conn.sendall(line.encode("ascii") + b"\r\n")

        send(f"220 {SMTP_HOST} ESMTP fake")
        sender = ""
        rcpts: list[str] = []
        while True:
            raw_line = reader.readline()
            if not raw_line:
                return
            line = raw_line.decode("ascii", "replace").rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            self.record.commands.append("AUTH ***" if verb == "AUTH" else line)
            if verb in ("EHLO", "HELO"):
                lines = [SMTP_HOST, "AUTH PLAIN"]
                if self.mode == "starttls" and not tls:
                    lines.append("STARTTLS")
                for item in lines[:-1]:
                    send(f"250-{item}")
                send(f"250 {lines[-1]}")
            elif verb == "STARTTLS" and self.mode == "starttls" and not tls:
                send("220 Ready to start TLS")
                reader.close()
                try:
                    conn = self._context.wrap_socket(conn, server_side=True)
                except (ssl.SSLError, OSError):
                    self.record.handshake_failures += 1
                    return
                tls = True
                reader = conn.makefile("rb")
            elif verb == "AUTH":
                parts = line.split(" ")
                decoded = base64.b64decode(parts[2]).split(b"\x00") if len(parts) > 2 else []
                if len(decoded) == 3:
                    user, password = decoded[1].decode(), decoded[2].decode()
                    self.record.auth.append((user, password, tls))
                    if (user, password) == (SMTP_USER, SMTP_PASSWORD):
                        send("235 Authentication successful")
                        continue
                send("535 Authentication failed")
            elif verb == "MAIL":
                sender = line.split(":", 1)[1].strip().strip("<>").split(">")[0]
                send(self.mail_reply)
                if not self.mail_reply.startswith("250"):
                    sender = ""
            elif verb == "RCPT":
                rcpts.append(line.split(":", 1)[1].strip().strip("<>"))
                send("250 OK")
            elif verb == "DATA":
                send("354 End data with <CR><LF>.<CR><LF>")
                data = bytearray()
                while True:
                    chunk = reader.readline()
                    if not chunk or chunk == b".\r\n":
                        break
                    data += chunk
                self.record.mails.append((sender, list(rcpts), bytes(data), tls))
                rcpts = []
                send("250 Queued")
            elif verb == "RSET":
                rcpts = []
                send("250 OK")
            elif verb == "QUIT":
                send("221 Bye")
                return
            else:
                send("502 Command not implemented")


@contextmanager
def running_smtp(pki: ImapPki, **kwargs: Any) -> Iterator[FakeSmtpServer]:
    server = FakeSmtpServer(pki, **kwargs)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def notify_pki(directory: Path) -> tuple[ImapPki, ImapPki]:
    """Certificates for the web and the SMTP test host (one CA each)."""
    return make_pki(directory / "web", WEB_HOST), make_pki(directory / "smtp", SMTP_HOST)

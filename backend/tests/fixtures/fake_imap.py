# SPDX-License-Identifier: AGPL-3.0-or-later
"""A small in-process IMAPS server, a CONNECT proxy and synthetic mail (email intake tests).

:class:`FakeImapServer` speaks just enough IMAP4rev1 over **implicit TLS**
for ``imaplib.IMAP4_SSL``: ``CAPABILITY``, ``LOGIN``, ``SELECT``/``EXAMINE``,
``CREATE``, ``UID SEARCH ALL``, ``UID FETCH`` (``RFC822.SIZE``,
``BODY.PEEK[HEADER]<0.n>``, ``BODY.PEEK[]``), ``UID MOVE``, ``UID COPY``,
``UID STORE``, ``UID EXPUNGE``, ``EXPUNGE``, ``NOOP`` and ``LOGOUT``.
``STARTTLS`` is refused (and recorded). Every command line is recorded
(:attr:`FakeImapServer.commands`, the ``LOGIN`` password masked) so tests
can check what the client asked for, e.g. that a rejected message's body
was never fetched.

Its certificate is issued by a synthetic CA made for the test run
(:class:`ImapPki`); it is trusted only by an ``SSLContext`` built from
:attr:`ImapPki.ca_path`, never by the system store. The server listens on
``127.0.0.1`` with an OS-assigned port (parallel-safe).

Mail is built with the standard library (:func:`make_mail`); senders,
merchants and amounts are invented.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
import socket
import ssl
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import default as default_policy
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

IMAP_HOST = "imap.perfin.test"
USERNAME = "receipts@perfin.test"
PASSWORD = "synthetic app password"


# ---------------------------------------------------------------------------
# PKI
# ---------------------------------------------------------------------------


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


@dataclass(frozen=True)
class ImapPki:
    ca_path: Path
    cert_path: Path
    key_path: Path

    def client_context(self) -> ssl.SSLContext:
        """A verifying client context that trusts only the test CA."""
        context = ssl.create_default_context(cafile=str(self.ca_path))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert_path), str(self.key_path))
        return context


def make_pki(directory: Path, host: str = IMAP_HOST) -> ImapPki:
    """A synthetic CA and a server certificate for ``host``."""
    directory.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("PerFin IMAP test CA"))
        .issuer_name(_name("PerFin IMAP test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(_name(host))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(host), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = directory / "ca.pem"
    cert_path = directory / "server.pem"
    key_path = directory / "server.key"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ImapPki(ca_path, cert_path, key_path)


# ---------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------


def make_mail(
    *,
    sender: str = "alex@home.invalid",
    to: str = "receipts+ALIAS@perfin.test",
    delivered_to: str | None = None,
    subject: str = "Your receipt from Corner Market",
    message_id: str | None = "<r-0001@shop.invalid>",
    auth_results: Sequence[str] = (),
    body: str = "Corner Market\nTotal 12.34 USD\nThank you for shopping.\n",
    attachments: Sequence[tuple[str, str, bytes]] = (),
    extra_from: str | None = None,
) -> bytes:
    """A synthetic message starting with a ``Received`` line like delivered mail."""
    msg = EmailMessage(policy=default_policy)
    msg["Received"] = "from mx.shop.invalid by mx.perfin.test; Sat, 14 Mar 2026 12:45:00 +0000"
    for value in auth_results:
        msg["Authentication-Results"] = value
    if delivered_to is not None:
        msg["Delivered-To"] = delivered_to
    msg["From"] = sender if extra_from is None else f"{sender}, {extra_from}"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = "Sat, 14 Mar 2026 12:45:00 +0000"
    if message_id is not None:
        msg["Message-ID"] = message_id
    msg.set_content(body)
    for name, ctype, data in attachments:
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


@dataclass
class Message:
    uid: int
    data: bytes
    deleted: bool = False

    @property
    def header(self) -> bytes:
        for sep in (b"\r\n\r\n", b"\n\n"):
            index = self.data.find(sep)
            if index >= 0:
                return self.data[: index + len(sep)]
        return self.data


@dataclass
class Folder:
    messages: list[Message] = field(default_factory=list)
    next_uid: int = 1

    def add(self, data: bytes) -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.messages.append(Message(uid, data))
        return uid

    def find(self, uid: int) -> Message | None:
        return next((m for m in self.messages if m.uid == uid), None)


_TOKEN = re.compile(rb'"((?:[^"\\]|\\.)*)"|(\([^)]*\))|(\S+)')


def _tokens(line: bytes) -> list[str]:
    out: list[str] = []
    for quoted, paren, atom in _TOKEN.findall(line):
        if quoted or (not paren and not atom):
            out.append(re.sub(rb"\\(.)", rb"\1", quoted).decode("utf-8", "replace"))
        else:
            out.append((paren or atom).decode("utf-8", "replace"))
    return out


class FakeImapServer:
    """See module doc. Use :func:`running_imap`."""

    def __init__(
        self,
        pki: ImapPki,
        *,
        username: str = USERNAME,
        password: str = PASSWORD,
        move: bool = True,
    ) -> None:
        self.username = username
        self.password = password
        self.capabilities = ["IMAP4rev1", "UIDPLUS", "AUTH=PLAIN"] + (["MOVE"] if move else [])
        self.folders: dict[str, Folder] = {"INBOX": Folder()}
        self.commands: list[str] = []
        self.handshake_failures = 0
        self._context = pki.server_context()
        self._lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.1)
        self.port: int = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    # -- state helpers ------------------------------------------------------

    def deliver(self, data: bytes, folder: str = "INBOX") -> int:
        with self._lock:
            return self.folders.setdefault(folder, Folder()).add(data)

    def count(self, folder: str) -> int:
        with self._lock:
            box = self.folders.get(folder)
            return 0 if box is None else sum(1 for m in box.messages if not m.deleted)

    def body_fetches(self) -> int:
        return sum(1 for c in self.commands if "BODY.PEEK[]" in c)

    # -- serving ------------------------------------------------------------

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
            raw.settimeout(None)
            threading.Thread(target=self._client, args=(raw,), daemon=True).start()

    def _client(self, raw: socket.socket) -> None:
        raw.settimeout(10)
        try:
            conn = self._context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            self.handshake_failures += 1
            raw.close()
            return
        session = _Session(self, conn)
        try:
            session.run()
        except (OSError, ssl.SSLError):
            pass
        finally:
            with suppress(OSError):
                conn.close()


class _Session:
    def __init__(self, server: FakeImapServer, conn: ssl.SSLSocket) -> None:
        self.server = server
        self.conn = conn
        self.reader = conn.makefile("rb")
        self.authed = False
        self.selected: str | None = None

    def send(self, data: bytes) -> None:
        self.conn.sendall(data)

    def line(self, text: str) -> None:
        self.send(text.encode("utf-8") + b"\r\n")

    def run(self) -> None:
        self.line("* OK fake IMAP ready")
        while True:
            raw = self.reader.readline(65536)
            if not raw:
                return
            line = raw.rstrip(b"\r\n")
            parts = _tokens(line)
            if len(parts) < 2:
                continue
            tag, command, args = parts[0], parts[1].upper(), parts[2:]
            logged = line.decode("utf-8", "replace")
            if command == "LOGIN":
                logged = f"{tag} LOGIN {args[0] if args else ''} ***"
            with self.server._lock:
                self.server.commands.append(logged)
            if not self.dispatch(tag, command, args):
                return

    def dispatch(self, tag: str, command: str, args: list[str]) -> bool:
        server = self.server
        if command == "CAPABILITY":
            self.line("* CAPABILITY " + " ".join(server.capabilities))
            self.line(f"{tag} OK done")
        elif command == "NOOP":
            self.line(f"{tag} OK done")
        elif command == "STARTTLS":
            self.line(f"{tag} BAD already using TLS")
        elif command == "LOGOUT":
            self.line("* BYE bye")
            self.line(f"{tag} OK done")
            return False
        elif command == "LOGIN":
            if len(args) == 2 and args[0] == server.username and args[1] == server.password:
                self.authed = True
                self.line(f"{tag} OK logged in")
            else:
                self.line(f"{tag} NO [AUTHENTICATIONFAILED] invalid credentials")
        elif not self.authed:
            self.line(f"{tag} BAD log in first")
        elif command in ("SELECT", "EXAMINE"):
            name = args[0] if args else ""
            with server._lock:
                folder = server.folders.get(name)
                exists = 0 if folder is None else sum(1 for m in folder.messages if not m.deleted)
            if folder is None:
                self.line(f"{tag} NO no such mailbox")
            else:
                self.selected = name
                self.line(f"* {exists} EXISTS")
                self.line("* OK [UIDVALIDITY 1] ok")
                self.line(f"{tag} OK [READ-WRITE] selected")
        elif command == "CREATE":
            name = args[0] if args else ""
            with server._lock:
                if name in server.folders:
                    self.line(f"{tag} NO [ALREADYEXISTS] exists")
                else:
                    server.folders[name] = Folder()
                    self.line(f"{tag} OK created")
        elif command == "EXPUNGE":
            self._expunge(None)
            self.line(f"{tag} OK done")
        elif command == "UID" and args:
            self.uid(tag, args[0].upper(), args[1:])
        else:
            self.line(f"{tag} BAD unknown command")
        return True

    def _box(self) -> Folder:
        assert self.selected is not None
        return self.server.folders[self.selected]

    def _expunge(self, uid: int | None) -> None:
        with self.server._lock:
            box = self._box()
            box.messages = [
                m for m in box.messages if not (m.deleted and (uid is None or m.uid == uid))
            ]

    def uid(self, tag: str, sub: str, args: list[str]) -> None:
        if self.selected is None:
            self.line(f"{tag} BAD select first")
            return
        server = self.server
        if sub == "SEARCH":
            with server._lock:
                uids = [str(m.uid) for m in self._box().messages if not m.deleted]
            self.line("* SEARCH " + " ".join(uids))
            self.line(f"{tag} OK done")
            return
        try:
            uid = int(args[0])
        except (IndexError, ValueError):
            self.line(f"{tag} BAD bad uid")
            return
        with server._lock:
            box = self._box()
            message = box.find(uid)
            seq = box.messages.index(message) + 1 if message is not None else 0
        if message is None or message.deleted:
            self.line(f"{tag} OK nothing")
            return
        if sub == "FETCH":
            items = args[1].upper() if len(args) > 1 else ""
            if "HEADER" in items:
                data = message.header
                head = f"* {seq} FETCH (UID {uid} RFC822.SIZE {len(message.data)} BODY[HEADER]<0>"
            else:
                data = message.data
                head = f"* {seq} FETCH (UID {uid} BODY[]"
            self.send(f"{head} {{{len(data)}}}\r\n".encode("ascii") + data + b")\r\n")
            self.line(f"{tag} OK done")
        elif sub in ("MOVE", "COPY"):
            if sub == "MOVE" and "MOVE" not in server.capabilities:
                self.line(f"{tag} BAD no MOVE")
                return
            target = args[1] if len(args) > 1 else ""
            with server._lock:
                folder = server.folders.get(target)
                if folder is None:
                    self.line(f"{tag} NO [TRYCREATE] no such mailbox")
                    return
                folder.add(message.data)
                if sub == "MOVE":
                    box.messages.remove(message)
            if sub == "MOVE":
                self.line(f"* {seq} EXPUNGE")
            self.line(f"{tag} OK done")
        elif sub == "STORE":
            if len(args) > 2 and "DELETED" in args[2].upper():
                message.deleted = True
            self.line(f"{tag} OK done")
        elif sub == "EXPUNGE":
            self._expunge(uid)
            self.line(f"{tag} OK done")
        else:
            self.line(f"{tag} BAD unknown UID command")


@contextmanager
def running_imap(pki: ImapPki, **kwargs: object) -> Iterator[FakeImapServer]:
    server = FakeImapServer(pki, **kwargs)  # type: ignore[arg-type]
    server.start()
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# CONNECT proxy
# ---------------------------------------------------------------------------


class ConnectProxy:
    """A CONNECT-only proxy that forwards ``IMAP_HOST:993`` to the fake server."""

    def __init__(self, routes: dict[str, tuple[str, int]], *, refuse: bool = False) -> None:
        self.routes = routes
        self.refuse = refuse
        self.requests: list[str] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.1)
        self.port: int = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

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
                client, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            client.settimeout(None)
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        client.settimeout(10)
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(1024)
                if not chunk:
                    return
                head += chunk
            request = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
            self.requests.append(request)
            parts = request.split()
            target = (
                self.routes.get(parts[1]) if len(parts) == 3 and parts[0] == "CONNECT" else None
            )
            if self.refuse or target is None:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            upstream = socket.create_connection(target, timeout=10)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            _pipe(client, upstream)
        except OSError:
            pass
        finally:
            with suppress(OSError):
                client.close()


def _pipe(a: socket.socket, b: socket.socket) -> None:
    def forward(src: socket.socket, dst: socket.socket) -> None:
        try:
            while chunk := src.recv(65536):
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            with suppress(OSError):
                dst.shutdown(socket.SHUT_WR)

    other = threading.Thread(target=forward, args=(b, a), daemon=True)
    other.start()
    forward(a, b)
    other.join(timeout=10)
    with suppress(OSError):
        b.close()


@contextmanager
def running_proxy(
    routes: dict[str, tuple[str, int]], *, refuse: bool = False
) -> Iterator[ConnectProxy]:
    proxy = ConnectProxy(routes, refuse=refuse)
    proxy.start()
    try:
        yield proxy
    finally:
        proxy.stop()

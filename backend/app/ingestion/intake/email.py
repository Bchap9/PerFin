# SPDX-License-Identifier: AGPL-3.0-or-later
"""Email intake: poll a household's receipts mailbox over IMAPS (INGESTION.md, D1-D5, C10).

**Connection (D5).** Implicit TLS only (``imaplib.IMAP4_SSL``, port 993 by
default): there is no plaintext IMAP and no ``STARTTLS``, so nothing can
downgrade the session. The TLS context is :func:`client_ssl_context`
(``ssl.create_default_context()``: certificate chain and host name verified
against the system trust store, which honours ``SSL_CERT_FILE``; TLS 1.2 at
least). When ``HTTPS_PROXY`` names a proxy for ``https://host:port`` (the
Compose ``egress`` profile, SECURITY.md H2) the TCP connection is a
``CONNECT host:port`` tunnel through it and TLS runs end to end inside the
tunnel; the proxy resolves and vets the name. Without a proxy the name is
resolved here and **every** address is vetted like the SSRF-safe HTTP client
does (``app.core.http.address_verdict``: link-local, metadata, multicast and
reserved addresses always refused, private ones only with
``PERFIN_EMAIL_INTAKE_ALLOW_PRIVATE_HOSTS``, loopback only in dev mode on
top), then the connection is pinned to the vetted address. A household
admin chooses the host, so this keeps the mailbox from being pointed at the
database or the LAN. Errors map to a short outcome code
(:class:`MailboxError`); server replies and credentials are never logged.

**What the worker reads of a message.** Untrusted mail is parsed in the
worker only as far as the acceptance policy needs: the header block
(``BODY.PEEK[HEADER]``, at most :data:`HEADER_MAX` bytes) with the standard
library's :class:`email.parser.BytesHeaderParser` (``compat32``: no MIME
decoding, no body), each value capped at :data:`VALUE_MAX` characters and
at most :data:`VALUES_MAX` values per header, plus ``RFC822.SIZE``. The body
of a rejected message is never fetched. An accepted message is fetched whole
(``BODY.PEEK[]``, after its declared size passed the cap) and handed to the
normal intake (:func:`app.ingestion.intake.upload.store_stream`: sniff, size
cap, SHA-256, encryption into quarantine) as a ``.eml`` document with
``source=email``; its body, HTML and attachments are parsed only by the
sandboxed EML extractor in the pipeline (``app.ingestion.extract.eml``,
D3/D4), which hands attachments and ``cid:`` images back as child
documents.

**Acceptance policy (D1),** :func:`evaluate`, in order:

1. the household's alias token appears as the local part, or as a ``+``
   sub-address of the local part, of an address in ``To``, ``Cc``,
   ``Delivered-To`` or ``X-Original-To`` (lower-cased, compared with
   :func:`hmac.compare_digest`) - else ``no_alias``;
2. exactly one ``From`` address, and it is allowed: a member's own address
   (``member_addresses``) or an entry of ``allowed_senders`` - else
   ``sender_invalid`` / ``sender_not_allowed``; the mail becomes a document
   of the member that address maps to;
3. with ``require_auth_results`` and an ``Authentication-Results`` header
   present: the topmost such header (or the topmost from ``authserv_id``,
   when configured; headers further down can be added by anyone) must say
   ``spf=pass`` and ``dkim=pass``, and the ``dkim=pass`` must name its
   signing domain (``header.d``/``header.i``; a bare pass counts as not
   aligned) and that domain must be the ``From`` domain or a parent of it
   (:func:`_aligned`) - else ``auth_failed``. Comments are removed before the
   results are read. The alias token is the real control: a self-hosted MTA
   may add no such header at all.

Messages larger than the cap are ``too_large``; mail the intake refuses
(not recognisable as a message) is ``unsupported``.

**Two-phase move and dedupe.** A run records its decision for each new
message in ``mail_intake_message`` (keyed by :func:`message_key`) together
with the document or the digest entry, in the job's transaction, and does
**not** move it. A message is moved (``UID MOVE``; ``COPY`` + ``\\Deleted`` +
``EXPUNGE`` where the server lacks ``MOVE``) only by a run that finds it
already recorded: accepted ones to ``processed_folder``, rejected ones to
``rejected_folder``. The handler enqueues a follow-up run whenever a run
recorded something, so the moves happen right after the commit. A crash
between the two leaves the message in the inbox, where the next run finds
its record and only moves it. The same key is the ``Message-ID`` dedupe: a
second copy of an accepted message is moved, never imported again. A
**rejected** message is recorded under :func:`rejected_key` instead (a hash
of its whole header block): otherwise an outsider's rejected forgery
carrying the ``Message-ID`` and ``From`` of a legitimate future message
would send that message to the rejected folder unprocessed (security review
P34-SR-R2-02). A copy with the identical header block is still moved to the
rejected folder without a second evaluation.

**Rejected digest (C10).** Rejections add to **one** open
``rejected_email`` review item per household and UTC day (no document;
owners and admins see it): a count per reason and the first
:data:`DIGEST_SAMPLES` senders, subjects and reasons. A flood of guessed
aliases produces one item, not one per message.

**Caps (C10).** Per run: at most ``max_messages`` new messages, at most
``max_run_bytes`` fetched, at most :data:`SCAN_FACTOR` times
``max_messages`` inbox entries looked at; the rest waits for the next run.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import imaplib
import ipaddress
import re
import socket
import ssl
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser
from email.policy import compat32
from email.utils import getaddresses
from typing import Any, Final, Literal

import httpx
from sqlalchemy import Boolean, String, select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.crypto import Keyring
from app.core.http import (
    EgressBlockedError,
    EgressError,
    IPAddress,
    Resolver,
    address_verdict,
    env_proxy_for,
    system_resolver,
)
from app.core.logging import get_logger
from app.core.settings import Settings
from app.domain.documents import repo as documents_repo
from app.domain.documents.blobstore import BlobStore
from app.domain.documents.models import DocumentSource, ReviewItem, ReviewItemState, ReviewKind
from app.domain.intake_settings.models import MailIntakeMessage
from app.ingestion.intake.upload import (
    UploadRejectedError,
    UploadTooLargeError,
    record_stored,
    store_stream,
)

log = get_logger(__name__)

HEADER_MAX: Final = 64 * 1024
VALUE_MAX: Final = 4096
VALUES_MAX: Final = 50
ADDRESSES_MAX: Final = 100
AUTH_RESULTS_MAX: Final = 20
SUBJECT_MAX: Final = 200
MESSAGE_ID_MAX: Final = 255
DIGEST_SAMPLES: Final = 50
SCAN_FACTOR: Final = 4
PROXY_HEAD_MAX: Final = 8192

ALIAS_HEADERS: Final = ("To", "Cc", "Delivered-To", "X-Original-To")

Outcome = Literal[
    "ok",
    "not_configured",
    "blocked",
    "connect_failed",
    "tls_failed",
    "auth_failed",
    "folder_missing",
    "protocol_error",
    "timeout",
]
Reason = Literal[
    "no_alias", "sender_invalid", "sender_not_allowed", "auth_failed", "too_large", "unsupported"
]


class MailboxError(Exception):
    """The mailbox could not be used; :attr:`outcome` is safe to store and show."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__(outcome)
        self.outcome: Outcome = outcome


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MailboxConfig:
    """Where the mailbox is and how to log in (decrypted for one run only)."""

    host: str
    port: int
    username: str
    password: str = field(repr=False)
    inbox_folder: str = "INBOX"
    processed_folder: str = "PerFin/Processed"
    rejected_folder: str = "PerFin/Rejected"

    def __repr__(self) -> str:
        return f"MailboxConfig(host={self.host!r}, port={self.port})"


@dataclass(frozen=True)
class AcceptancePolicy:
    """The household's acceptance rules (module doc, D1)."""

    alias_token: str = field(repr=False)
    #: Lower-cased sender address -> member the message belongs to.
    senders: Mapping[str, uuid.UUID]
    require_auth_results: bool = True
    authserv_id: str | None = None


@dataclass(frozen=True)
class RunLimits:
    max_messages: int = 50
    max_message_bytes: int = 25 * 1024 * 1024
    max_run_bytes: int = 200 * 1024 * 1024

    @classmethod
    def from_settings(cls, settings: Settings) -> RunLimits:
        return cls(
            max_messages=settings.email_intake_max_messages_per_run,
            max_message_bytes=min(
                settings.email_intake_max_message_mb * 1024 * 1024, settings.upload_max_bytes
            ),
            max_run_bytes=settings.email_intake_max_mb_per_run * 1024 * 1024,
        )


ProxySetting = str | None | Literal["env"]


def client_ssl_context() -> ssl.SSLContext:
    """Certificate and host name verified, TLS 1.2 or later (D5)."""
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


@dataclass(frozen=True)
class ConnectOptions:
    """How to reach IMAP servers (module doc, "Connection")."""

    timeout_seconds: float = 30.0
    proxy: ProxySetting = "env"
    allow_private: bool = False
    allow_loopback: bool = False
    resolver: Resolver = system_resolver
    ssl_context_factory: Callable[[], ssl.SSLContext] = client_ssl_context

    @classmethod
    def from_settings(cls, settings: Settings) -> ConnectOptions:
        allow_private = settings.email_intake_allow_private_hosts
        return cls(
            timeout_seconds=float(settings.email_intake_timeout_seconds),
            allow_private=allow_private,
            allow_loopback=allow_private and settings.dev_mode,
        )

    def proxy_for(self, host: str, port: int) -> str | None:
        if self.proxy != "env":
            return self.proxy
        netloc = f"[{host}]" if ":" in host else host
        return env_proxy_for(httpx.URL(f"https://{netloc}:{port}"))


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------


def _ip_literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _direct_socket(host: str, port: int, options: ConnectOptions) -> socket.socket:
    literal = _ip_literal(host)
    addresses = [literal] if literal is not None else options.resolver(host, port)
    if not addresses:
        raise EgressError(f"{host} did not resolve")
    for address in addresses:
        if address_verdict(
            address, allow_private=options.allow_private, allow_loopback=options.allow_loopback
        ):
            raise EgressBlockedError("the mail server's address is not allowed")
    # Pinned to the vetted address: no second lookup.
    return socket.create_connection((str(addresses[0]), port), timeout=options.timeout_seconds)


def _tunnel_socket(host: str, port: int, proxy: str, options: ConnectOptions) -> socket.socket:
    """A ``CONNECT host:port`` tunnel through an HTTP proxy (the egress proxy)."""
    url = httpx.URL(proxy)
    if url.scheme != "http" or not url.host:
        raise EgressBlockedError("only an http:// CONNECT proxy is supported")
    sock = socket.create_connection((url.host, url.port or 3128), timeout=options.timeout_seconds)
    try:
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii"))
        head = bytearray()
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(1024)
            if not chunk:
                raise EgressError("the proxy closed the connection")
            head += chunk
            if len(head) > PROXY_HEAD_MAX:
                raise EgressError("the proxy reply is too long")
        status_line = bytes(head).split(b"\r\n", 1)[0]
        parts = status_line.split(b" ", 2)
        if len(parts) < 2 or not parts[0].startswith(b"HTTP/1."):
            raise EgressError("the proxy reply is not HTTP")
        if parts[1] != b"200":
            if parts[1] in (b"403", b"405"):
                raise EgressBlockedError("the egress proxy refused the mail server")
            raise EgressError("the proxy could not reach the mail server")
        if not bytes(head).endswith(b"\r\n\r\n"):
            # Bytes after the reply head would be the server's TLS data; a
            # CONNECT proxy sends nothing before the client speaks TLS.
            raise EgressError("unexpected data after the proxy reply")
    except BaseException:
        sock.close()
        raise
    return sock


def open_socket(host: str, port: int, options: ConnectOptions) -> socket.socket:
    proxy = options.proxy_for(host, port)
    if proxy is not None:
        return _tunnel_socket(host, port, proxy, options)
    return _direct_socket(host, port, options)


class _Imaps(imaplib.IMAP4_SSL):
    """``IMAP4_SSL`` whose TCP connection comes from :func:`open_socket`."""

    def __init__(
        self, host: str, port: int, *, ssl_context: ssl.SSLContext, options: ConnectOptions
    ) -> None:
        self._options = options
        super().__init__(host, port, ssl_context=ssl_context, timeout=options.timeout_seconds)

    def _create_socket(self, timeout: float | None) -> ssl.SSLSocket:
        raw = open_socket(self.host, self.port, self._options)
        context: ssl.SSLContext = self.ssl_context
        try:
            return context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _classify(exc: BaseException) -> Outcome:
    if isinstance(exc, EgressBlockedError):
        return "blocked"
    if isinstance(exc, ssl.SSLError | ssl.CertificateError):
        return "tls_failed"
    if isinstance(exc, TimeoutError | socket.timeout):
        return "timeout"
    if isinstance(exc, EgressError | OSError):
        return "connect_failed"
    return "protocol_error"


def _quote(mailbox: str) -> str:
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


_FETCH_UID: Final = re.compile(rb"\bUID (\d{1,10})\b")
_FETCH_SIZE: Final = re.compile(rb"\bRFC822\.SIZE (\d{1,12})\b")


@dataclass(frozen=True)
class HeaderFetch:
    size: int
    header: bytes


class Mailbox:
    """A logged-in IMAPS session (use :func:`connect`)."""

    def __init__(self, imap: imaplib.IMAP4) -> None:
        self._imap = imap
        caps = getattr(imap, "capabilities", ())
        self.capabilities = frozenset(str(c).upper() for c in caps)

    @property
    def move_supported(self) -> bool:
        return "MOVE" in self.capabilities

    def close(self) -> None:
        with contextlib.suppress(imaplib.IMAP4.error, OSError):
            self._imap.logout()

    def __enter__(self) -> Mailbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _ok(self, result: tuple[str, list[Any]], outcome: Outcome = "protocol_error") -> list[Any]:
        status, data = result
        if status != "OK":
            raise MailboxError(outcome)
        return data

    def select(self, folder: str, *, readonly: bool = False) -> int:
        try:
            data = self._ok(self._imap.select(_quote(folder), readonly=readonly), "folder_missing")
        except imaplib.IMAP4.error as exc:
            raise MailboxError("folder_missing") from exc
        try:
            return int(data[0]) if data and data[0] is not None else 0
        except (TypeError, ValueError):
            return 0

    def ensure_folder(self, folder: str) -> None:
        """``CREATE`` the folder; an existing one answers ``NO``, which is fine."""
        with contextlib.suppress(imaplib.IMAP4.error):
            self._imap.create(_quote(folder))

    def uids(self, limit: int) -> list[int]:
        data = self._ok(self._imap.uid("SEARCH", "ALL"))
        found: list[int] = []
        for chunk in data:
            if isinstance(chunk, bytes):
                for token in chunk.split():
                    if token.isdigit() and len(token) <= 10:
                        found.append(int(token))
        return sorted(set(found))[:limit]

    def _fetch(self, uid: int, items: str) -> tuple[bytes, bytes] | None:
        data = self._ok(self._imap.uid("FETCH", str(uid), items))
        meta = bytearray()
        literal: bytes | None = None
        for part in data:
            if isinstance(part, tuple) and len(part) == 2:
                head, body = part
                if isinstance(head, bytes):
                    meta += head + b" "
                if literal is None and isinstance(body, bytes):
                    literal = body
            elif isinstance(part, bytes):
                meta += part + b" "
        match = _FETCH_UID.search(meta)
        if match is None or int(match.group(1)) != uid or literal is None:
            return None
        return bytes(meta), literal

    def fetch_header(self, uid: int) -> HeaderFetch | None:
        fetched = self._fetch(uid, f"(UID RFC822.SIZE BODY.PEEK[HEADER]<0.{HEADER_MAX}>)")
        if fetched is None:
            return None
        meta, header = fetched
        size = _FETCH_SIZE.search(meta)
        if size is None:
            return None
        return HeaderFetch(int(size.group(1)), header[:HEADER_MAX])

    def fetch_message(self, uid: int, max_bytes: int) -> bytes | None:
        fetched = self._fetch(uid, "(UID BODY.PEEK[])")
        if fetched is None:
            return None
        body = fetched[1]
        if len(body) > max_bytes:
            raise _TooLargeError
        return body

    def move(self, uid: int, folder: str) -> bool:
        target = _quote(folder)
        try:
            if self.move_supported:
                status, _ = self._imap.uid("MOVE", str(uid), target)
                return status == "OK"
            status, _ = self._imap.uid("COPY", str(uid), target)
            if status != "OK":
                return False
            self._imap.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Deleted)")
            if "UIDPLUS" in self.capabilities:
                self._imap.uid("EXPUNGE", str(uid))
            else:
                self._imap.expunge()
            return True
        except imaplib.IMAP4.error:
            return False


class _TooLargeError(Exception):
    pass


def connect(config: MailboxConfig, options: ConnectOptions) -> Mailbox:
    """Open an IMAPS session and log in (``MailboxError`` with the outcome on failure)."""
    context = options.ssl_context_factory()
    try:
        imap = _Imaps(config.host, config.port, ssl_context=context, options=options)
    except (imaplib.IMAP4.error, EgressError, OSError) as exc:
        raise MailboxError(_classify(exc)) from None
    try:
        imap.login(config.username, config.password)
    except imaplib.IMAP4.error:
        with contextlib.suppress(OSError):
            imap.shutdown()
        raise MailboxError("auth_failed") from None
    except OSError as exc:
        raise MailboxError(_classify(exc)) from None
    return Mailbox(imap)


@dataclass(frozen=True)
class CheckResult:
    outcome: Outcome
    messages: int | None = None
    move_supported: bool | None = None


def check_connection(config: MailboxConfig, options: ConnectOptions) -> CheckResult:
    """Connect, log in and look at the inbox read-only (``POST /settings/intake/email/test``)."""
    try:
        with connect(config, options) as mailbox:
            count = mailbox.select(config.inbox_folder, readonly=True)
            return CheckResult("ok", messages=count, move_supported=mailbox.move_supported)
    except MailboxError as exc:
        return CheckResult(exc.outcome)
    except (imaplib.IMAP4.error, OSError) as exc:
        return CheckResult(_classify(exc))


# ---------------------------------------------------------------------------
# Headers and the acceptance policy (pure)
# ---------------------------------------------------------------------------

_CONTROL: Final = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_COMMENT: Final = re.compile(r"\([^()]*\)")
_RESULT: Final = re.compile(r"^\s*([a-z0-9_-]{1,32})\s*=\s*([a-z]{1,16})\b")
_PROPERTY: Final = re.compile(r"\bheader\.([di])\s*=\s*\"?([^\s;\"]{1,253})")


@dataclass(frozen=True)
class MailHeaders:
    """The few header values the policy reads (bounded; module doc)."""

    from_addresses: tuple[str, ...]
    recipients: tuple[str, ...]
    message_id: str | None
    subject: str
    auth_results: tuple[str, ...]
    raw_size: int


def _values(message: Any, name: str) -> list[str]:
    try:
        raw = message.get_all(name) or []
    except (TypeError, ValueError):
        return []
    out: list[str] = []
    for value in raw[:VALUES_MAX]:
        text = str(value)[:VALUE_MAX]
        out.append(_CONTROL.sub(" ", text.replace("\r", "").replace("\n", " ")))
    return out


def _addresses(values: Iterable[str]) -> list[str]:
    found: list[str] = []
    try:
        pairs = getaddresses(list(values))
    except (TypeError, ValueError, IndexError):
        return found
    for _name, address in pairs[:ADDRESSES_MAX]:
        cleaned = address.strip().lower()
        if cleaned.count("@") == 1 and " " not in cleaned and len(cleaned) <= 254:
            found.append(cleaned)
    return found


def _subject(values: list[str]) -> str:
    if not values:
        return ""
    raw = values[0]
    try:
        text = str(make_header(decode_header(raw)))
    except (ValueError, LookupError, UnicodeError, TypeError):
        text = raw
    return _CONTROL.sub(" ", text).strip()[:SUBJECT_MAX]


def parse_headers(header: bytes) -> MailHeaders:
    """Read the policy's header values from a raw header block (no body, no MIME)."""
    block = header[:HEADER_MAX]
    message = BytesHeaderParser(policy=compat32).parsebytes(block)
    recipients: list[str] = []
    for name in ALIAS_HEADERS:
        recipients.extend(_addresses(_values(message, name)))
    message_ids = _values(message, "Message-ID")
    message_id = message_ids[0].strip()[:MESSAGE_ID_MAX] if message_ids else None
    return MailHeaders(
        from_addresses=tuple(_addresses(_values(message, "From"))),
        recipients=tuple(recipients[:ADDRESSES_MAX]),
        message_id=message_id or None,
        subject=_subject(_values(message, "Subject")),
        auth_results=tuple(_values(message, "Authentication-Results")[:AUTH_RESULTS_MAX]),
        raw_size=len(block),
    )


def message_key(headers: MailHeaders, header: bytes) -> str:
    """The dedupe key: ``Message-ID`` plus ``From``, else the whole header block."""
    if headers.message_id:
        sender = headers.from_addresses[0] if headers.from_addresses else ""
        material = b"mid\0" + headers.message_id.lower().encode("utf-8", "replace")
        material += b"\0" + sender.encode("utf-8", "replace")
    else:
        material = b"hdr\0" + header[:HEADER_MAX]
    return hashlib.sha256(material).hexdigest()


def rejected_key(header: bytes) -> str:
    """The key a rejected message is recorded under: its whole header block.

    Unlike :func:`message_key` an outsider cannot reproduce it for a message
    they did not send (it covers the receiving MTA's ``Received`` and
    ``Authentication-Results`` lines), so a rejected forgery never shadows
    an accepted message with the same ``Message-ID`` and ``From``.
    """
    return hashlib.sha256(b"rej\0" + header[:HEADER_MAX]).hexdigest()


def has_alias(recipients: Sequence[str], token: str) -> bool:
    """Whether the token is a recipient's local part or one of its ``+`` tags."""
    wanted = token.lower().encode("ascii")
    found = False
    for address in recipients[:ADDRESSES_MAX]:
        local = address.rsplit("@", 1)[0]
        for candidate in (local, *local.split("+")[1:]):
            # No early exit: every candidate is compared.
            found |= hmac.compare_digest(candidate.encode("utf-8", "replace"), wanted)
    return found


@dataclass(frozen=True)
class AuthVerdict:
    present: bool
    spf: str | None = None
    dkim: str | None = None
    dkim_aligned: bool = False

    @property
    def passed(self) -> bool:
        return self.spf == "pass" and self.dkim == "pass" and self.dkim_aligned

    def summary(self) -> dict[str, Any]:
        return {"present": self.present, "spf": self.spf, "dkim": self.dkim}


def _strip_comments(value: str) -> str:
    for _ in range(8):
        stripped = _COMMENT.sub(" ", value)
        if stripped == value:
            break
        value = stripped
    return value


def _aligned(signing: str, sender_domain: str) -> bool:
    """Whether a DKIM signing domain may vouch for the ``From`` domain.

    The signer must be the ``From`` domain itself or a parent of it
    (``home.example`` signs for ``mail.home.example``) and have at least two
    labels. A signer *below* the From domain is not enough: on a domain that
    hands out subdomains, ``attacker.home.example`` could otherwise vouch for
    ``alex@home.example`` (no public-suffix list needed for this direction:
    nobody but the parent's operator can sign as the parent).
    """
    signing = signing.lower().rstrip(".")
    if "@" in signing:
        signing = signing.rsplit("@", 1)[1]
    sender_domain = sender_domain.lower().rstrip(".")
    if not signing or "." not in signing or not sender_domain:
        return False
    return signing == sender_domain or sender_domain.endswith("." + signing)


def read_auth_results(
    values: Sequence[str], *, authserv_id: str | None, sender_domain: str
) -> AuthVerdict:
    """Read the trusted ``Authentication-Results`` header (module doc, policy 3)."""
    if not values:
        return AuthVerdict(present=False)
    chosen: str | None = None
    for value in values:
        server = _strip_comments(value).split(";", 1)[0].strip().split()
        name = server[0].lower() if server else ""
        if authserv_id is None or name == authserv_id:
            chosen = value
            break
    if chosen is None:
        # Headers exist but none from our receiving server: treated as present
        # and not passing (a sender can add any header it likes).
        return AuthVerdict(present=True)
    spf: str | None = None
    dkim: str | None = None
    aligned = False
    for segment in _strip_comments(chosen).split(";")[1:]:
        match = _RESULT.match(segment.lower())
        if match is None:
            continue
        method, result = match.group(1), match.group(2)
        if method == "spf" and spf != "pass":
            spf = result
        elif method == "dkim":
            # A pass must name its signer (header.d / header.i): a bare
            # "dkim=pass" could be a valid signature by any domain.
            prop = _PROPERTY.search(segment.lower())
            this_aligned = prop is not None and _aligned(prop.group(2), sender_domain)
            if result == "pass" and this_aligned:
                dkim, aligned = "pass", True
            elif dkim != "pass":
                dkim = result
    return AuthVerdict(present=True, spf=spf, dkim=dkim, dkim_aligned=aligned)


@dataclass(frozen=True)
class Decision:
    accepted: bool
    reason: Reason | None
    sender: str | None
    user_id: uuid.UUID | None
    auth: AuthVerdict


def evaluate(
    headers: MailHeaders, size: int, policy: AcceptancePolicy, limits: RunLimits
) -> Decision:
    """The acceptance policy (module doc, D1)."""
    sender = headers.from_addresses[0] if len(headers.from_addresses) == 1 else None
    domain = sender.rsplit("@", 1)[1] if sender else ""
    auth = read_auth_results(
        headers.auth_results, authserv_id=policy.authserv_id, sender_domain=domain
    )

    def reject(reason: Reason) -> Decision:
        return Decision(False, reason, sender, None, auth)

    if size > limits.max_message_bytes:
        return reject("too_large")
    if not has_alias(headers.recipients, policy.alias_token):
        return reject("no_alias")
    if sender is None:
        return reject("sender_invalid")
    user_id = policy.senders.get(sender)
    if user_id is None:
        return reject("sender_not_allowed")
    if policy.require_auth_results and auth.present and not auth.passed:
        return reject("auth_failed")
    return Decision(True, None, sender, user_id, auth)


# ---------------------------------------------------------------------------
# A poll run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntakeDeps:
    store: BlobStore
    keyring: Keyring


@dataclass
class PollStats:
    seen: int = 0
    accepted: int = 0
    rejected: int = 0
    moved: int = 0
    move_failed: int = 0
    skipped: int = 0
    bytes_fetched: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    #: Set when the connection failed part-way; what was recorded stays.
    error: Outcome | None = None

    @property
    def recorded(self) -> int:
        return self.accepted + self.rejected

    def counts(self) -> dict[str, Any]:
        return {
            "seen": self.seen,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "moved": self.moved,
            "move_failed": self.move_failed,
            "skipped": self.skipped,
            "reasons": dict(self.reasons),
            "error": self.error,
        }


def _digest_item(db: Session, household_id: uuid.UUID, day: str) -> ReviewItem | None:
    return db.execute(
        select(ReviewItem)
        .where(
            ReviewItem.household_id == household_id,
            ReviewItem.kind == ReviewKind.REJECTED_EMAIL,
            ReviewItem.state == ReviewItemState.OPEN,
            ReviewItem.document_id.is_(None),
            ReviewItem.payload.op("->>", return_type=String)("day") == day,
            ReviewItem.payload.op("?", return_type=Boolean)("count"),
        )
        .order_by(ReviewItem.created_at)
        .limit(1)
        .with_for_update()
    ).scalar_one_or_none()


def add_to_digest(
    db: Session, household_id: uuid.UUID, *, sender: str | None, subject: str, reason: str
) -> ReviewItem:
    """Count one rejected message in today's digest item (module doc, C10)."""
    now = clock.utcnow()
    day = now.date().isoformat()
    sample = {
        "sender": (sender or "")[:254],
        "subject": subject[:SUBJECT_MAX],
        "reason": reason,
        "at": now.isoformat(),
    }
    item = _digest_item(db, household_id, day)
    if item is None:
        return documents_repo.add_review_item(
            db,
            household_id,
            ReviewKind.REJECTED_EMAIL,
            {
                "day": day,
                "count": 1,
                "reasons": {reason: 1},
                "messages": [sample],
                "omitted": 0,
            },
        )
    payload = dict(item.payload)
    payload["count"] = int(payload.get("count", 0)) + 1
    reasons = dict(payload.get("reasons") or {})
    reasons[reason] = int(reasons.get(reason, 0)) + 1
    payload["reasons"] = reasons
    messages = list(payload.get("messages") or [])
    if len(messages) < DIGEST_SAMPLES:
        messages.append(sample)
    else:
        payload["omitted"] = int(payload.get("omitted", 0)) + 1
    payload["messages"] = messages
    item.payload = payload
    db.flush()
    return item


def _filename() -> str:
    return f"mail-{clock.utcnow().strftime('%Y%m%d-%H%M%S')}.eml"


def _record(
    db: Session,
    household_id: uuid.UUID,
    key: str,
    outcome: Literal["accepted", "rejected"],
    reason: str | None,
    document_id: uuid.UUID | None,
) -> None:
    db.add(
        MailIntakeMessage(
            household_id=household_id,
            message_key=key,
            outcome=outcome,
            reason=reason,
            document_id=document_id,
        )
    )
    db.flush()


def poll(
    db: Session,
    household_id: uuid.UUID,
    mailbox: Mailbox,
    config: MailboxConfig,
    policy: AcceptancePolicy,
    limits: RunLimits,
    deps: IntakeDeps,
    *,
    cancelled: threading.Event | None = None,
) -> PollStats:
    """One run over the inbox (module doc). Writes in ``db``; never commits."""
    stats = PollStats()
    mailbox.ensure_folder(config.processed_folder)
    mailbox.ensure_folder(config.rejected_folder)
    mailbox.select(config.inbox_folder)
    uids = mailbox.uids(limits.max_messages * SCAN_FACTOR)
    known: dict[str, str] = {}
    try:
        for uid in uids:
            if cancelled is not None and cancelled.is_set():
                break
            fetched = mailbox.fetch_header(uid)
            if fetched is None:
                stats.skipped += 1
                continue
            stats.seen += 1
            headers = parse_headers(fetched.header)
            key = message_key(headers, fetched.header)
            reject_key = rejected_key(fetched.header)
            keys = (key, reject_key)
            outcome = next((known[k] for k in keys if k in known), None)
            if outcome is not None:
                # A second copy within this run: moved by the follow-up run.
                stats.skipped += 1
                continue
            recorded: dict[str, str] = dict(
                db.execute(
                    select(MailIntakeMessage.message_key, MailIntakeMessage.outcome).where(
                        MailIntakeMessage.household_id == household_id,
                        MailIntakeMessage.message_key.in_(keys),
                    )
                ).all()
            )
            if recorded:
                # Recorded by an earlier, committed run (or a duplicate copy).
                # This very header block rejected: back to the rejected folder.
                folder = (
                    config.processed_folder
                    if recorded.get(key) == "accepted" and reject_key not in recorded
                    else config.rejected_folder
                )
                if mailbox.move(uid, folder):
                    stats.moved += 1
                else:
                    stats.move_failed += 1
                continue
            if stats.recorded >= limits.max_messages:
                break
            decision = evaluate(headers, fetched.size, policy, limits)
            document_id: uuid.UUID | None = None
            reason: str | None = decision.reason
            if decision.accepted:
                if stats.bytes_fetched + fetched.size > limits.max_run_bytes and stats.recorded:
                    break
                try:
                    raw = mailbox.fetch_message(uid, limits.max_message_bytes)
                except _TooLargeError:
                    raw, reason = None, "too_large"
                if raw is None and reason is None:
                    stats.skipped += 1
                    continue
                if raw is not None:
                    stats.bytes_fetched += len(raw)
                    document_id, reason = _receive(
                        db, household_id, raw, headers, decision, limits, deps
                    )
            if document_id is not None:
                stats.accepted += 1
                known[key] = "accepted"
                _record(db, household_id, key, "accepted", None, document_id)
            else:
                final_reason = reason or "unsupported"
                stats.rejected += 1
                stats.reasons[final_reason] += 1
                known[reject_key] = "rejected"
                add_to_digest(
                    db,
                    household_id,
                    sender=decision.sender
                    or (headers.from_addresses[0] if headers.from_addresses else None),
                    subject=headers.subject,
                    reason=final_reason,
                )
                _record(db, household_id, reject_key, "rejected", final_reason, None)
    except MailboxError as exc:
        stats.error = exc.outcome
    except (imaplib.IMAP4.error, OSError) as exc:
        # Keep what was recorded; the follow-up run moves it.
        stats.error = _classify(exc)
    log.info("mailbox polled", extra={"household_id": str(household_id), **stats.counts()})
    return stats


def _receive(
    db: Session,
    household_id: uuid.UUID,
    raw: bytes,
    headers: MailHeaders,
    decision: Decision,
    limits: RunLimits,
    deps: IntakeDeps,
) -> tuple[uuid.UUID | None, str | None]:
    try:
        stored = store_stream(
            store=deps.store,
            keyring=deps.keyring,
            chunks=(raw,),
            max_bytes=limits.max_message_bytes,
        )
    except UploadRejectedError:
        return None, "unsupported"
    except UploadTooLargeError:
        return None, "too_large"
    received = record_stored(
        db,
        stored,
        store=deps.store,
        household_id=household_id,
        source=DocumentSource.EMAIL,
        source_meta={
            "sender": decision.sender,
            "message_id": headers.message_id,
            "subject": headers.subject,
            "auth": decision.auth.summary(),
        },
        original_filename=_filename(),
        uploaded_by=decision.user_id,
    )
    return received.document_id, None

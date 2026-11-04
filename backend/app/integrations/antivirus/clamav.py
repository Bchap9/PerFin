# SPDX-License-Identifier: AGPL-3.0-or-later
"""ClamAV sidecar client: clamd ``INSTREAM`` (SECURITY.md C9, INGESTION.md "Scan").

The worker streams a quarantined document's **plaintext** to clamd on the
internal ``backend`` network (``PERFIN_CLAMAV_ADDRESS``: ``tcp://clamav:3310``
or ``unix:///run/clamav/clamd.sock``). Protocol (clamd(8)): the command
``zINSTREAM\\0``, then chunks each prefixed with their length as a 4-byte
big-endian integer, then a zero-length chunk; clamd answers one
NUL-terminated line: ``stream: OK``, ``stream: <signature> FOUND`` or
``... ERROR``.

* Nothing is sent beyond ``PERFIN_CLAMAV_MAX_MB``: a larger document is not
  scanned (``too_large``, not retryable), so the pipeline's failure mode
  decides (fail closed by default).
* Every socket operation has ``PERFIN_CLAMAV_TIMEOUT_SECONDS`` and the
  whole scan an overall deadline of the same length times four; a slow or
  silent clamd is ``timeout``.
* The reply is read up to :data:`REPLY_MAX` bytes; the signature name keeps
  only allowlisted characters (:func:`clean_signature`).
* No TLS: clamd speaks plain TCP, so the address must be on the internal
  network (``config_check`` refuses a non-private TCP host in production).
"""

from __future__ import annotations

import socket
import struct
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from app.integrations.antivirus import ScanResult, clean_signature

if TYPE_CHECKING:
    from app.core.settings import Settings

COMMAND: Final = b"zINSTREAM\0"
#: clamd's default ``StreamMaxLength`` is 25 MiB; chunks well below its limit.
CHUNK: Final = 64 * 1024
REPLY_MAX: Final = 4096


class ClamdAddressError(ValueError):
    """``PERFIN_CLAMAV_ADDRESS`` is not ``tcp://host:port`` or ``unix:///path``."""


@dataclass(frozen=True)
class ClamdAddress:
    family: str  # "tcp" | "unix"
    host: str = ""
    port: int = 0
    path: str = ""

    @classmethod
    def parse(cls, value: str) -> ClamdAddress:
        parts = urlsplit(value)
        if parts.scheme == "tcp":
            if not parts.hostname or parts.port is None or parts.path not in ("", "/"):
                raise ClamdAddressError("expected tcp://host:port")
            return cls("tcp", host=parts.hostname, port=parts.port)
        if parts.scheme == "unix":
            if parts.netloc or not parts.path.startswith("/"):
                raise ClamdAddressError("expected unix:///absolute/path")
            return cls("unix", path=parts.path)
        raise ClamdAddressError("expected tcp://host:port or unix:///path")


def _connect(address: ClamdAddress, timeout: float) -> socket.socket:
    if address.family == "unix":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(address.path)
        except BaseException:
            sock.close()
            raise
        return sock
    return socket.create_connection((address.host, address.port), timeout=timeout)


def parse_reply(reply: bytes) -> ScanResult:
    """clamd's answer to ``INSTREAM`` as a :class:`ScanResult`."""
    text = reply.split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    # "stream: OK" / "stream: Eicar-Signature FOUND" / "... ERROR"; some
    # clamd versions prefix a session id ("1: stream: OK").
    _, _, verdict = text.partition("stream:")
    verdict = verdict.strip() if verdict else text
    if verdict == "OK":
        return ScanResult("clean")
    if verdict.endswith(" FOUND"):
        return ScanResult("infected", signature=clean_signature(verdict[: -len(" FOUND")]))
    if "size limit exceeded" in verdict.lower():
        return ScanResult("error", error="too_large", retryable=False)
    return ScanResult("error", error="scanner_error", retryable=True)


@dataclass(frozen=True)
class ClamdScanner:
    address: ClamdAddress
    timeout_seconds: float = 30.0
    max_bytes: int = 50 * 1024 * 1024
    name: str = "clamav"

    @classmethod
    def from_settings(cls, settings: Settings) -> ClamdScanner:
        return cls(
            address=ClamdAddress.parse(settings.clamav_address),
            timeout_seconds=float(settings.clamav_timeout_seconds),
            max_bytes=settings.clamav_max_mb * 1024 * 1024,
        )

    def scan(self, chunks: Iterable[bytes], *, size: int) -> ScanResult:
        """Stream ``chunks`` (the plaintext, ``size`` bytes) to clamd and return the verdict."""
        if size > self.max_bytes:
            return ScanResult("error", error="too_large", retryable=False)
        deadline = time.monotonic() + self.timeout_seconds * 4
        try:
            sock = _connect(self.address, self.timeout_seconds)
        except (OSError, TimeoutError):
            return ScanResult("error", error="unavailable", retryable=True)
        with sock:
            try:
                sock.sendall(COMMAND)
                sent = 0
                for chunk in chunks:
                    for start in range(0, len(chunk), CHUNK):
                        piece = chunk[start : start + CHUNK]
                        sent += len(piece)
                        if sent > self.max_bytes:
                            return ScanResult("error", error="too_large", retryable=False)
                        if time.monotonic() > deadline:
                            return ScanResult("error", error="timeout", retryable=True)
                        sock.sendall(struct.pack(">I", len(piece)) + piece)
                sock.sendall(struct.pack(">I", 0))
                reply = bytearray()
                while b"\0" not in reply and len(reply) < REPLY_MAX:
                    if time.monotonic() > deadline:
                        return ScanResult("error", error="timeout", retryable=True)
                    data = sock.recv(REPLY_MAX - len(reply))
                    if not data:
                        break
                    reply += data
            except TimeoutError:
                return ScanResult("error", error="timeout", retryable=True)
            except OSError:
                # clamd closes the connection when a stream exceeds its own
                # StreamMaxLength; a reply may already be waiting.
                return ScanResult("error", error="connection_lost", retryable=True)
        if not reply:
            return ScanResult("error", error="no_reply", retryable=True)
        return parse_reply(bytes(reply))

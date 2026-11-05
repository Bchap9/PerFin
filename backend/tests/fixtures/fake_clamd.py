# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake clamd speaking ``INSTREAM`` over TCP or a UNIX socket (no ClamAV in tests).

``FakeClamd(mode=...)`` answers like clamd: ``clean`` (``stream: OK``),
``signature`` (``stream: <name> FOUND`` when the stream contains
:data:`MARKER`, else OK), ``error`` (``... ERROR``), ``size_limit``,
``silent`` (reads, never answers: the client must time out), ``hangup``
(closes the connection right after the command) and ``garbage`` (an
unparseable reply). It records each stream it received. The marker is a
synthetic string, not a real test signature.
"""

from __future__ import annotations

import contextlib
import socket
import struct
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

MARKER = b"PERFIN-SYNTHETIC-MALWARE-MARKER"
SIGNATURE = "Perfin.Test.Synthetic-1"

Mode = Literal["clean", "signature", "error", "size_limit", "silent", "hangup", "garbage"]


@dataclass
class FakeClamd:
    mode: Mode = "signature"
    unix_path: Path | None = None
    signature: str = SIGNATURE
    streams: list[bytes] = field(default_factory=list)
    commands: list[bytes] = field(default_factory=list)
    _sock: socket.socket | None = None
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)

    @property
    def address(self) -> str:
        assert self._sock is not None
        if self.unix_path is not None:
            return f"unix://{self.unix_path}"
        host, port = self._sock.getsockname()[:2]
        return f"tcp://{host}:{port}"

    def start(self) -> FakeClamd:
        if self.unix_path is not None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(self.unix_path))
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        sock.settimeout(0.2)
        self._sock = sock
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
        if self._sock is not None:
            self._sock.close()

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _read_exact(self, conn: socket.socket, n: int) -> bytes:
        data = bytearray()
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError("closed")
            data += chunk
        return bytes(data)

    def _handle(self, conn: socket.socket) -> None:
        with conn, contextlib.suppress(OSError, ConnectionError):
            conn.settimeout(10)
            command = bytearray()
            while not command.endswith(b"\0"):
                command += self._read_exact(conn, 1)
            self.commands.append(bytes(command))
            if self.mode == "hangup":
                return
            stream = bytearray()
            while True:
                (length,) = struct.unpack(">I", self._read_exact(conn, 4))
                if length == 0:
                    break
                stream += self._read_exact(conn, length)
            self.streams.append(bytes(stream))
            if self.mode == "silent":
                self._stop.wait(30)
                return
            conn.sendall(self._reply(bytes(stream)))

    def _reply(self, stream: bytes) -> bytes:
        if self.mode == "error":
            return b"stream: Can't allocate memory ERROR\0"
        if self.mode == "size_limit":
            return b"INSTREAM size limit exceeded. ERROR\0"
        if self.mode == "garbage":
            return b"\xff\xfe what?\0"
        if self.mode == "signature" and MARKER in stream:
            return f"stream: {self.signature} FOUND\0".encode()
        return b"stream: OK\0"


@contextlib.contextmanager
def fake_clamd(mode: Mode = "signature", *, unix_path: Path | None = None) -> Iterator[FakeClamd]:
    server = FakeClamd(mode=mode, unix_path=unix_path).start()
    try:
        yield server
    finally:
        server.stop()

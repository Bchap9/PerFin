# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run one extraction in the sandboxed child process (ADR-0006, SECURITY.md C1/C2).

:func:`run_extraction` is the only way the worker gets structured data out of
a file. It starts ``python -I -B -m app.ingestion.sandbox.child`` with:

* ``env={}`` (no inherited secrets: database URL, master key, tokens) and
  ``-I`` (no ``PYTHON*`` variables, no user site, no current directory on
  ``sys.path``), ``-B`` (no bytecode writes);
* a fresh private (``0700``) temporary directory as the working directory,
  removed afterwards;
* its own process group (``process_group=0``), so the whole group can be
  killed; ``close_fds``; ``umask 077``;
* when the worker runs as root and ``PERFIN_EXTRACT_UID`` is set, the
  dedicated UID/GID with no supplementary groups;
* the decrypted file on **stdin** (never a path into the blob store).

The parent enforces what the child cannot enforce on itself: a wall-clock
timeout (``SIGKILL`` to the process group), a cap on stdout (exceeding it
kills the child) and on the stderr kept for logs, and validation of stdout
against :class:`app.ingestion.sandbox.protocol.ExtractResult`. Any failure is
a :class:`SandboxError` with a stable ``code``: ``timeout``, ``cpu_limit``,
``memory_limit``, ``output_too_large``, ``bad_output``, ``harden_failed``,
``killed``, ``crashed``, ``usage`` or ``input_too_large``.

This module never imports ``app.ingestion.extract``: parsing happens only in
the child.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess  # nosec B404
import sys
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import IO, TYPE_CHECKING, Final

from pydantic import ValidationError

from app.core.logging import get_logger
from app.ingestion.sandbox.protocol import (
    ExtractOptions,
    ExtractResult,
    SandboxJob,
    SandboxLimits,
)

if TYPE_CHECKING:
    from app.core.settings import Settings

log = get_logger(__name__)

CHILD_MODULE: Final = "app.ingestion.sandbox.child"
_READ_CHUNK: Final = 64 * 1024
_STDERR_KEEP: Final = 8 * 1024
_JOIN_SECONDS: Final = 5.0

# Mirrors app.ingestion.sandbox.child.EXIT_CODES (not imported: the child
# module is only ever run, never loaded, by the worker).
_EXIT_CODES: Final = {64: "usage", 65: "harden_failed", 67: "memory_limit", 70: "crashed"}


class SandboxError(Exception):
    """The extraction did not produce a valid result. ``str()`` never includes child output."""

    def __init__(self, code: str, detail: str = "", *, stderr: str = "") -> None:
        super().__init__(f"sandbox {code}" + (f": {detail}" if detail else ""))
        self.code = code
        self.detail = detail
        self.stderr = stderr


@dataclass(frozen=True)
class SandboxConfig:
    """Limits and process settings for one extraction."""

    memory_bytes: int = 1024 * 1024 * 1024
    cpu_seconds: int = 60
    timeout_seconds: float = 120.0
    max_output_bytes: int = 64 * 1024 * 1024
    max_input_bytes: int = 1024 * 1024 * 1024
    max_open_files: int = 64
    max_file_bytes: int = 0
    uid: int | None = None
    gid: int | None = None
    python: str = field(default_factory=lambda: sys.executable)
    module: str = CHILD_MODULE

    @classmethod
    def from_settings(cls, settings: Settings) -> SandboxConfig:
        uid = settings.extract_uid
        return cls(
            memory_bytes=settings.extract_memory_mb * 1024 * 1024,
            cpu_seconds=settings.extract_cpu_seconds,
            timeout_seconds=float(settings.extract_timeout_seconds),
            max_output_bytes=settings.extract_max_output_mb * 1024 * 1024,
            max_input_bytes=settings.upload_max_bytes,
            uid=uid,
            gid=uid,
        )

    def limits(self) -> SandboxLimits:
        return SandboxLimits(
            memory_bytes=self.memory_bytes,
            cpu_seconds=self.cpu_seconds,
            max_open_files=self.max_open_files,
            max_file_bytes=self.max_file_bytes,
            max_input_bytes=self.max_input_bytes,
        )

    def drops_privileges(self) -> bool:
        return self.uid is not None and os.geteuid() == 0


@dataclass
class _Pumps:
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    output_exceeded: bool = False
    input_exceeded: bool = False


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def _feed(proc: subprocess.Popen[bytes], data: Iterable[bytes], limit: int, pumps: _Pumps) -> None:
    stdin = proc.stdin
    if stdin is None:  # pragma: no cover - Popen(stdin=PIPE)
        return
    sent = 0
    try:
        for chunk in data:
            sent += len(chunk)
            if sent > limit:
                pumps.input_exceeded = True
                break
            stdin.write(chunk)
    except (BrokenPipeError, OSError):
        pass  # the child stopped reading (it failed or was killed)
    finally:
        with contextlib.suppress(OSError):
            stdin.close()


def _drain_stdout(proc: subprocess.Popen[bytes], limit: int, pumps: _Pumps) -> None:
    stream = proc.stdout
    if stream is None:  # pragma: no cover - Popen(stdout=PIPE)
        return
    fd = stream.fileno()
    while chunk := os.read(fd, _READ_CHUNK):
        if len(pumps.stdout) + len(chunk) > limit:
            pumps.output_exceeded = True
            _kill_group(proc)
            break
        pumps.stdout += chunk
    # Keep draining (and discarding) so the child never blocks on a full pipe.
    while os.read(fd, _READ_CHUNK):
        pass


def _drain_stderr(stream: IO[bytes], pumps: _Pumps) -> None:
    while chunk := stream.read(_READ_CHUNK):
        room = _STDERR_KEEP - len(pumps.stderr)
        if room > 0:
            pumps.stderr += chunk[:room]


def _classify_exit(returncode: int) -> str:
    if returncode < 0:
        sig = -returncode
        if sig == signal.SIGXCPU:
            return "cpu_limit"
        if sig == signal.SIGKILL:
            # The CPU hard limit (if SIGXCPU was survived) or the kernel's OOM killer.
            return "killed"
        return "crashed"
    return _EXIT_CODES.get(returncode, "crashed")


def _spawn(config: SandboxConfig, job: SandboxJob, workdir: str) -> subprocess.Popen[bytes]:
    command = [config.python, "-I", "-B", "-m", config.module, job.model_dump_json()]
    drop = config.drops_privileges()
    gid = config.gid if config.gid is not None else config.uid
    # Fixed argv (our interpreter, our module, a validated JSON job), no shell.
    return subprocess.Popen(  # noqa: S603  # nosec B603
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=workdir,
        env={},
        close_fds=True,
        process_group=0,
        umask=0o077,
        user=config.uid if drop else None,
        group=gid if drop else None,
        extra_groups=[] if drop else None,
    )


def run_extraction(
    kind: str,
    data: bytes | Iterable[bytes],
    *,
    config: SandboxConfig | None = None,
    options: ExtractOptions | None = None,
) -> ExtractResult:
    """Extract ``data`` (plaintext bytes or a chunk iterator) with the ``kind`` extractor.

    Returns the validated result; an ``ok=False`` result means the input
    could not be extracted (malformed, forbidden content, ...). Raises
    :class:`SandboxError` when the sandbox itself failed or was killed.
    """
    config = config or SandboxConfig()
    job = SandboxJob(kind=kind, limits=config.limits(), options=options or ExtractOptions())
    chunks: Iterable[bytes] = (data,) if isinstance(data, bytes | bytearray) else data
    workdir = tempfile.mkdtemp(prefix="perfin-sandbox-")
    pumps = _Pumps()
    timed_out = False
    try:
        if config.drops_privileges() and config.uid is not None:
            os.chown(workdir, config.uid, config.gid if config.gid is not None else config.uid)
        proc = _spawn(config, job, workdir)
        if proc.stderr is None:  # pragma: no cover - Popen(stderr=PIPE)
            raise SandboxError("crashed", "no stderr pipe")
        threads = [
            threading.Thread(
                target=_feed, args=(proc, chunks, config.max_input_bytes, pumps), daemon=True
            ),
            threading.Thread(
                target=_drain_stdout, args=(proc, config.max_output_bytes, pumps), daemon=True
            ),
            threading.Thread(target=_drain_stderr, args=(proc.stderr, pumps), daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            proc.wait(timeout=config.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            proc.wait()
        # Nothing of the group may outlive the extraction.
        _kill_group(proc)
        for thread in threads:
            thread.join(_JOIN_SECONDS)
        returncode = proc.returncode
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    stderr = pumps.stderr.decode("utf-8", "replace")
    if timed_out:
        raise SandboxError(
            "timeout", f"no result within {config.timeout_seconds:g}s", stderr=stderr
        )
    if pumps.output_exceeded:
        raise SandboxError("output_too_large", stderr=stderr)
    if pumps.input_exceeded:
        raise SandboxError("input_too_large", stderr=stderr)
    if returncode != 0:
        code = _classify_exit(returncode)
        raise SandboxError(code, f"exit status {returncode}", stderr=stderr)
    try:
        result = ExtractResult.model_validate_json(bytes(pumps.stdout))
    except ValidationError:
        raise SandboxError("bad_output", "the child's output did not validate") from None
    if result.kind != kind:
        raise SandboxError("bad_output", "the child answered for another kind")
    return result

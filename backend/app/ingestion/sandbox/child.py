# SPDX-License-Identifier: AGPL-3.0-or-later
"""The extraction child process (ADR-0006, SECURITY.md C1/C2).

Started by :mod:`app.ingestion.sandbox.runner` as::

    python -I -B -m app.ingestion.sandbox.child '<SandboxJob JSON>'

with an empty environment, a private temporary working directory, the
file's plaintext on stdin and nothing else open. It never receives a path
into the blob store or any credential.

Before it reads a byte of input, :func:`harden` locks the process down and
the child **refuses to run** if any step fails:

1. ``setrlimit``: ``RLIMIT_AS`` (address space, default 1 GiB),
   ``RLIMIT_CPU`` (soft limit sends ``SIGXCPU``, hard limit one second later
   ``SIGKILL``), ``RLIMIT_NOFILE``, ``RLIMIT_FSIZE`` (default 0: no file may
   be written), ``RLIMIT_NPROC`` 0 and ``RLIMIT_CORE`` 0.
2. ``prctl(PR_SET_DUMPABLE, 0)`` and ``prctl(PR_SET_NO_NEW_PRIVS, 1)``.
3. A seccomp filter (libseccomp via ``pyseccomp``). **Choice: default-allow
   with a deny list**, each denied call failing with ``EPERM`` (``ENOSYS``
   for ``clone3`` so the C library falls back to the filtered ``clone``). A
   default-kill allowlist was rejected for now: the CPython runtime and the
   Phase 3 PDF/OCR libraries use a large and version-dependent set of
   syscalls, and an incomplete allowlist fails in production in ways tests
   do not see. The deny list removes what matters for a parser exploit:
   networking (``socket``, ``connect``, ``bind``, ...), running programs
   (``execve``, ``execveat``), new processes (``fork``, ``vfork``, ``clone``
   without ``CLONE_THREAD``), signals to other processes (``kill``/``tgkill``
   with another pid, ``tkill``, ``pidfd_*``), debugging and other processes'
   memory (``ptrace``, ``process_vm_*``), namespaces and mounts, kernel
   modules, BPF, keyrings, ``io_uring`` (which would bypass the filter),
   ``userfaultfd``, ``perf_event_open`` and identity changes. Syscalls of a
   non-native architecture are killed (libseccomp's bad-arch default). The
   argument checks on ``clone`` and ``kill`` assume the x86-64/arm64 argument
   order.

The worker container adds the outer layers (read-only root filesystem,
``cap_drop: ALL``, ``no-new-privileges``, no inbound ports, no egress).

Exit protocol: one :class:`ExtractResult` JSON document on stdout and exit
code 0 (also when the input cannot be extracted: ``ok=false``); any other
exit code is a sandbox failure (:data:`EXIT_CODES`).
"""

from __future__ import annotations

import ctypes
import errno
import os
import resource
import sys
from collections.abc import Callable, Mapping, Sequence
from types import ModuleType
from typing import Final

from pydantic import ValidationError

from app.ingestion.sandbox.protocol import (
    ExtractError,
    ExtractOptions,
    ExtractResult,
    SandboxJob,
    SandboxLimits,
)

Extractor = Callable[[bytes, ExtractOptions, str], ExtractResult]

# Loaded before hardening: finding libseccomp may spawn ``ldconfig``, which
# the limits and the filter rightly forbid later.
_SECCOMP: ModuleType | None
try:
    import pyseccomp as _pyseccomp

    _SECCOMP = _pyseccomp
except (ImportError, OSError):  # pragma: no cover - the image ships libseccomp2
    _SECCOMP = None

EXIT_OK: Final = 0
EXIT_USAGE: Final = 64
EXIT_HARDEN_FAILED: Final = 65
EXIT_MEMORY: Final = 67
EXIT_INTERNAL: Final = 70
EXIT_CODES: Final[Mapping[int, str]] = {
    EXIT_USAGE: "usage",
    EXIT_HARDEN_FAILED: "harden_failed",
    EXIT_MEMORY: "memory_limit",
    EXIT_INTERNAL: "crashed",
}

_PR_SET_DUMPABLE: Final = 4
_PR_SET_NO_NEW_PRIVS: Final = 38
_CLONE_THREAD: Final = 0x00010000
_READ_CHUNK: Final = 64 * 1024

#: Denied with EPERM regardless of arguments.
DENIED_SYSCALLS: Final = (
    # network
    "socket",
    "socketpair",
    "connect",
    "bind",
    "listen",
    "accept",
    "accept4",
    "sendto",
    "sendmsg",
    "sendmmsg",
    "recvfrom",
    "recvmsg",
    "recvmmsg",
    "getsockopt",
    "setsockopt",
    "shutdown",
    # programs and processes
    "execve",
    "execveat",
    "fork",
    "vfork",
    "tkill",
    "pidfd_open",
    "pidfd_send_signal",
    "pidfd_getfd",
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "process_madvise",
    "kcmp",
    # namespaces, mounts, root
    "unshare",
    "setns",
    "mount",
    "umount2",
    "pivot_root",
    "chroot",
    "move_mount",
    "open_tree",
    "fsopen",
    "fsconfig",
    "fsmount",
    "fspick",
    "mount_setattr",
    # kernel and system
    "init_module",
    "finit_module",
    "delete_module",
    "kexec_load",
    "kexec_file_load",
    "bpf",
    "perf_event_open",
    "userfaultfd",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "keyctl",
    "add_key",
    "request_key",
    "reboot",
    "swapon",
    "swapoff",
    "acct",
    "settimeofday",
    "clock_settime",
    "clock_adjtime",
    "adjtimex",
    "syslog",
    "quotactl",
    "name_to_handle_at",
    "open_by_handle_at",
    "lookup_dcookie",
    "vhangup",
    "iopl",
    "ioperm",
    "personality",
    "mknod",
    "mknodat",
    # identity
    "setuid",
    "setgid",
    "setreuid",
    "setregid",
    "setresuid",
    "setresgid",
    "setgroups",
    "setfsuid",
    "setfsgid",
    "capset",
)


class HardenError(RuntimeError):
    """A sandbox restriction could not be applied (the child refuses to run)."""


def _prctl(option: int, value: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        raise HardenError(f"prctl({option}) failed: errno {ctypes.get_errno()}")


def apply_rlimits(limits: SandboxLimits) -> None:
    cpu = limits.cpu_seconds
    settings = (
        (resource.RLIMIT_AS, limits.memory_bytes, limits.memory_bytes),
        (resource.RLIMIT_CPU, cpu, cpu + 1),
        (resource.RLIMIT_NOFILE, limits.max_open_files, limits.max_open_files),
        (resource.RLIMIT_FSIZE, limits.max_file_bytes, limits.max_file_bytes),
        (resource.RLIMIT_NPROC, 0, 0),
        (resource.RLIMIT_CORE, 0, 0),
    )
    for which, soft, hard in settings:
        resource.setrlimit(which, (soft, hard))


def install_seccomp() -> None:
    pyseccomp = _SECCOMP
    if pyseccomp is None:
        raise HardenError("libseccomp is not available")
    eperm = pyseccomp.ERRNO(errno.EPERM)
    pid = os.getpid()
    filt = pyseccomp.SyscallFilter(defaction=pyseccomp.ALLOW)
    for name in DENIED_SYSCALLS:
        try:
            filt.add_rule(eperm, name)
        except (OSError, ValueError):
            continue  # not a syscall on this architecture
    # New processes: clone without CLONE_THREAD (threads stay possible).
    filt.add_rule(eperm, "clone", pyseccomp.Arg(0, pyseccomp.MASKED_EQ, _CLONE_THREAD, 0))
    filt.add_rule(pyseccomp.ERRNO(errno.ENOSYS), "clone3")
    # Signals only to ourselves.
    filt.add_rule(eperm, "kill", pyseccomp.Arg(0, pyseccomp.NE, pid))
    filt.add_rule(eperm, "tgkill", pyseccomp.Arg(0, pyseccomp.NE, pid))
    filt.load()


def harden(limits: SandboxLimits) -> None:
    """Apply every restriction (module doc); raise :class:`HardenError` on any failure."""
    try:
        apply_rlimits(limits)
        _prctl(_PR_SET_DUMPABLE, 0)
        _prctl(_PR_SET_NO_NEW_PRIVS, 1)
        install_seccomp()
    except HardenError:
        raise
    except Exception as exc:
        raise HardenError(f"{type(exc).__name__}") from exc


def _write_result(result: ExtractResult) -> None:
    sys.stdout.buffer.write(result.model_dump_json().encode("utf-8"))
    sys.stdout.buffer.flush()


def _fail(kind: str, code: str, message: str = "") -> ExtractResult:
    return ExtractResult(kind=kind, ok=False, error=ExtractError(code=code, message=message))


def _read_input(limit: int) -> bytes | None:
    """All of stdin, or None past ``limit`` bytes (read in chunks: no huge preallocation)."""
    data = bytearray()
    fd = sys.stdin.fileno()
    while chunk := os.read(fd, _READ_CHUNK):
        data += chunk
        if len(data) > limit:
            return None
    return bytes(data)


def run(argv: Sequence[str], extractors: Mapping[str, Extractor] | None = None) -> int:
    """Validate the job, harden, read stdin, extract, print the result; return the exit code."""
    if len(argv) != 1:
        return EXIT_USAGE
    try:
        job = SandboxJob.model_validate_json(argv[0])
    except ValidationError:
        return EXIT_USAGE
    if extractors is None:
        from app.ingestion.extract.common import ExtractFailure
        from app.ingestion.extract.registry import builtin_extractors

        extractors = builtin_extractors()
    else:
        from app.ingestion.extract.common import ExtractFailure
    extractor = extractors.get(job.kind)
    try:
        harden(job.limits)
    except HardenError as exc:
        os.write(2, f"sandbox hardening failed: {exc}\n".encode())
        return EXIT_HARDEN_FAILED
    if extractor is None:
        _write_result(_fail(job.kind, "unknown_kind", "no extractor for this kind"))
        return EXIT_OK
    data = _read_input(job.limits.max_input_bytes)
    if data is None:
        _write_result(_fail(job.kind, "input_too_large", "input exceeds the limit"))
        return EXIT_OK
    try:
        result = extractor(data, job.options, job.kind)
    except ExtractFailure as failure:
        result = _fail(job.kind, failure.code, failure.message)
    except MemoryError:
        del data
        os.write(2, b"memory limit reached\n")
        return EXIT_MEMORY
    except RecursionError:
        result = _fail(job.kind, "too_deep", "recursion limit reached")
    except ValidationError:
        result = _fail(job.kind, "invalid_output", "extractor produced invalid data")
    except Exception as exc:  # noqa: BLE001 - reported as a result, never with input data
        result = _fail(job.kind, "extractor_crashed", type(exc).__name__)
    del data
    try:
        _write_result(result)
    except MemoryError:
        return EXIT_MEMORY
    return EXIT_OK


def main() -> None:  # pragma: no cover - exercised through the runner in a subprocess
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()

# SPDX-License-Identifier: AGPL-3.0-or-later
"""The extraction child process (ADR-0006, SECURITY.md C1/C2).

Started by :mod:`app.ingestion.sandbox.runner` as::

    python -I -B -m app.ingestion.sandbox.child '<SandboxJob JSON>'

with an empty environment, a private temporary working directory, the
file's plaintext on stdin and nothing else open. It is never *given* a path
into the blob store or any credential; the worker it was started from may
run under the same UID and hold both, so the steps below also make sure the
child cannot *reach* them (review finding P2-SR-01): the worker marks itself
non-dumpable before it spawns a child (:func:`app.ingestion.sandbox.runner.
protect_parent`), and the child confines itself with Landlock and seccomp.

Before it reads a byte of input, :func:`harden` locks the process down and
the child **refuses to run** if any step fails:

1. ``setrlimit``: ``RLIMIT_AS`` (address space, default 1 GiB),
   ``RLIMIT_CPU`` (soft limit sends ``SIGXCPU``, hard limit one second later
   ``SIGKILL``), ``RLIMIT_NOFILE``, ``RLIMIT_FSIZE`` (default 0: a backstop
   for ``write()``; changing files is stopped by steps 3 and 4),
   ``RLIMIT_NPROC`` 0 and ``RLIMIT_CORE`` 0.
2. ``prctl(PR_SET_DUMPABLE, 0)`` and ``prctl(PR_SET_NO_NEW_PRIVS, 1)``.
3. Landlock (:func:`install_landlock`): every filesystem access right the
   kernel's Landlock ABI knows is handled, and only *reading* is granted,
   beneath the interpreter's own directories (``sys.path``, the prefixes,
   the system library directories, ``/etc/ld.so.cache``) and the child's own
   ``/proc/<pid>``. Everything else is unreachable: the documents and
   quarantine volumes, a master key file, other processes' ``/proc`` entries
   (Landlock also refuses ptrace-mode access, i.e. ``/proc/<pid>/mem``,
   ``environ`` and ``fd``, to any process outside the child's domain). With
   ABI 4+ TCP bind/connect are handled too, with ABI 6+ signals and abstract
   UNIX sockets are scoped to the domain. If the kernel has no Landlock the
   child refuses to run unless the operator opted out
   (``PERFIN_EXTRACT_REQUIRE_LANDLOCK=false``, :attr:`SandboxLimits.
   require_landlock`), which leaves seccomp and the parent's non-dumpable
   flag as the only layers.
4. A seccomp filter (libseccomp via ``pyseccomp``). **Choice: default-allow
   with a deny list**, each denied call failing with ``EPERM`` (``ENOSYS``
   for ``clone3`` so the C library falls back to the filtered ``clone``). A
   default-kill allowlist was rejected for now: the CPython runtime and the
   Phase 3 PDF/OCR libraries use a large and version-dependent set of
   syscalls, and an incomplete allowlist fails in production in ways tests
   do not see. The deny list removes what matters for a parser exploit:
   networking (``socket``, ``connect``, ``bind``, ...), running programs
   (``execve``, ``execveat``), new processes (``fork``, ``vfork``, ``clone``
   without ``CLONE_THREAD``), signals to other processes (``kill``/``tgkill``/
   ``rt_sigqueueinfo``/``rt_tgsigqueueinfo`` with another pid, ``tkill``,
   ``pidfd_*``, ``fcntl`` ``F_SETOWN``/``F_SETOWN_EX``/``F_SETSIG``; review
   finding P2-SR-R2-01: Landlock only scopes signals from ABI 6, so seccomp
   must stop them on its own), debugging and other processes' memory
   (``ptrace``, ``process_vm_*``), namespaces and mounts, kernel modules,
   BPF, keyrings, ``io_uring`` (which would bypass the filter),
   ``userfaultfd``, ``perf_event_open`` and identity changes. Every call that
   changes the filesystem is denied as well (:data:`FILESYSTEM_DENIED_SYSCALLS`:
   ``unlink``/``rename``/``truncate``/``chmod``/``chown``/``mkdir``/``rmdir``/
   ``link``/``symlink``/``utimensat``/xattrs/``fallocate``/``creat`` and
   their ``*at`` forms), ``open``/``openat`` with ``O_WRONLY``, ``O_RDWR``,
   ``O_CREAT`` or ``O_TRUNC`` fail with ``EPERM`` (no file, nor
   ``/proc/<pid>/mem``, can be opened for writing) and ``openat2``, whose
   flags seccomp cannot inspect, with ``ENOSYS``. Syscalls of a
   non-native architecture are killed (libseccomp's bad-arch default). The
   argument checks on ``clone``, the signal calls and ``fcntl`` assume the
   x86-64/arm64 argument order.

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

#: Signal-sending calls whose first argument (a pid or thread-group id) must
#: be the child's own pid (``tkill`` and the ``pidfd_*`` calls are denied
#: outright in :data:`DENIED_SYSCALLS`).
SIGNAL_SELF_ONLY_SYSCALLS: Final = ("kill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo")
_F_SETOWN: Final = 8
_F_SETSIG: Final = 10
_F_SETOWN_EX: Final = 15
#: ``fcntl`` commands that direct signals at a process (``EPERM``).
FCNTL_DENIED_COMMANDS: Final = (_F_SETOWN, _F_SETSIG, _F_SETOWN_EX)

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


#: Calls that change the filesystem, denied with EPERM regardless of arguments.
FILESYSTEM_DENIED_SYSCALLS: Final = (
    "unlink",
    "unlinkat",
    "rename",
    "renameat",
    "renameat2",
    "truncate",
    "ftruncate",
    "fallocate",
    "chmod",
    "fchmod",
    "fchmodat",
    "fchmodat2",
    "chown",
    "fchown",
    "lchown",
    "fchownat",
    "mkdir",
    "mkdirat",
    "rmdir",
    "link",
    "linkat",
    "symlink",
    "symlinkat",
    "utime",
    "utimes",
    "utimensat",
    "futimesat",
    "setxattr",
    "lsetxattr",
    "fsetxattr",
    "removexattr",
    "lremovexattr",
    "fremovexattr",
    "creat",
)

#: ``open``/``openat`` flags that mean "may change the file"; any of them is refused.
WRITE_OPEN_FLAGS: Final = (os.O_WRONLY, os.O_RDWR, os.O_CREAT, os.O_TRUNC)

# Landlock (linux/landlock.h); the syscall numbers are the same on every
# architecture (generic syscall table).
_SYS_LANDLOCK_CREATE_RULESET: Final = 444
_SYS_LANDLOCK_ADD_RULE: Final = 445
_SYS_LANDLOCK_RESTRICT_SELF: Final = 446
_LANDLOCK_CREATE_RULESET_VERSION: Final = 1
_LANDLOCK_RULE_PATH_BENEATH: Final = 1
_LL_FS_READ_FILE: Final = 1 << 2
_LL_FS_READ_DIR: Final = 1 << 3
_LL_FS_ABI1: Final = (1 << 13) - 1  # EXECUTE .. MAKE_SYM
_LL_FS_REFER: Final = 1 << 13  # ABI 2
_LL_FS_TRUNCATE: Final = 1 << 14  # ABI 3
_LL_FS_IOCTL_DEV: Final = 1 << 15  # ABI 5
_LL_NET_ALL: Final = 0b11  # BIND_TCP | CONNECT_TCP, ABI 4
_LL_SCOPE_ALL: Final = 0b11  # ABSTRACT_UNIX_SOCKET | SIGNAL, ABI 6
#: Read-only roots besides the interpreter's own paths (shared libraries).
_SYSTEM_READ_ROOTS: Final = (
    "/usr/lib",
    "/usr/lib64",
    "/usr/local/lib",
    "/lib",
    "/lib64",
    "/etc/ld.so.cache",
)
#: Tesseract language data (read-only) for in-process OCR (ADR-0009 "OCR");
#: mirrors :data:`app.ingestion.extract.ocr.TESSDATA_DIRS` (not imported:
#: the extractors are loaded only after hardening).
TESSDATA_READ_ROOTS: Final = (
    "/usr/share/tesseract-ocr",
    "/usr/share/tessdata",
    "/usr/local/share/tessdata",
    "/usr/share/tesseract",
)


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = (
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    )


class _LandlockPathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))


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


def landlock_abi() -> int:
    """The kernel's Landlock ABI version, 0 when Landlock is unavailable."""
    libc = ctypes.CDLL(None, use_errno=True)
    version = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0), _LANDLOCK_CREATE_RULESET_VERSION
    )
    return max(int(version), 0)


def readable_roots() -> list[str]:
    """Directories (and files) the child may read after hardening: the interpreter's own."""
    candidates = [
        *sys.path,
        sys.prefix,
        sys.base_prefix,
        sys.exec_prefix,
        sys.base_exec_prefix,
        *_SYSTEM_READ_ROOTS,
        *TESSDATA_READ_ROOTS,
    ]
    roots: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        path = os.path.realpath(candidate)
        # Never the whole filesystem, and only what exists.
        if path == "/" or path in roots or not os.path.exists(path):
            continue
        roots.append(path)
    return roots


def install_landlock(*, required: bool = True) -> bool:
    """Restrict the filesystem to reading the interpreter's paths (module doc, step 3).

    Returns whether Landlock is in force; raises :class:`HardenError` when it
    is required but unavailable, or when any step fails.
    """
    abi = landlock_abi()
    if abi < 1:
        if required:
            raise HardenError("Landlock is not available")
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    handled = _LL_FS_ABI1
    if abi >= 2:
        handled |= _LL_FS_REFER
    if abi >= 3:
        handled |= _LL_FS_TRUNCATE
    if abi >= 5:
        handled |= _LL_FS_IOCTL_DEV
    attr = _LandlockRulesetAttr(
        handled_access_fs=handled,
        handled_access_net=_LL_NET_ALL if abi >= 4 else 0,
        scoped=_LL_SCOPE_ALL if abi >= 6 else 0,
    )
    size = 8 if abi < 4 else 16 if abi < 6 else 24
    ruleset = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(size), 0
    )
    if ruleset < 0:
        raise HardenError(f"landlock_create_ruleset failed: errno {ctypes.get_errno()}")
    try:
        own_proc = f"/proc/{os.getpid()}"
        for root in [*readable_roots(), own_proc]:
            fd = os.open(root, os.O_PATH | os.O_CLOEXEC)
            try:
                is_dir = os.path.isdir(root)
                rule = _LandlockPathBeneathAttr(
                    allowed_access=_LL_FS_READ_FILE | (_LL_FS_READ_DIR if is_dir else 0),
                    parent_fd=fd,
                )
                if (
                    libc.syscall(
                        _SYS_LANDLOCK_ADD_RULE,
                        ruleset,
                        _LANDLOCK_RULE_PATH_BENEATH,
                        ctypes.byref(rule),
                        0,
                    )
                    != 0
                ):
                    raise HardenError(f"landlock_add_rule failed: errno {ctypes.get_errno()}")
            finally:
                os.close(fd)
        if libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0) != 0:
            raise HardenError(f"landlock_restrict_self failed: errno {ctypes.get_errno()}")
    finally:
        os.close(ruleset)
    return True


def deny_filesystem_changes(filt: object, pyseccomp: ModuleType) -> None:
    """Add the rules that stop the child from changing any file (module doc, step 4)."""
    eperm = pyseccomp.ERRNO(errno.EPERM)
    add_rule = filt.add_rule  # type: ignore[attr-defined]
    for name in FILESYSTEM_DENIED_SYSCALLS:
        try:
            add_rule(eperm, name)
        except (OSError, ValueError):
            continue  # not a syscall on this architecture
    for syscall, flags_arg in (("open", 1), ("openat", 2)):
        for flag in WRITE_OPEN_FLAGS:
            try:
                add_rule(eperm, syscall, pyseccomp.Arg(flags_arg, pyseccomp.MASKED_EQ, flag, flag))
            except (OSError, ValueError):
                break  # ``open`` does not exist on arm64
    add_rule(pyseccomp.ERRNO(errno.ENOSYS), "openat2")


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
    # Signals only to ourselves (review finding P2-SR-R2-01): every call whose
    # first argument names the target process or thread group. Without
    # Landlock signal scoping (ABI 6+, Linux 6.12+) the kernel lets a
    # same-UID process be signalled by ``rt_sigqueueinfo`` and
    # ``rt_tgsigqueueinfo`` (negative ``si_code``) as well as by ``kill``.
    for name in SIGNAL_SELF_ONLY_SYSCALLS:
        filt.add_rule(eperm, name, pyseccomp.Arg(0, pyseccomp.NE, pid))
    # Signals by file ownership: ``F_SETOWN``/``F_SETOWN_EX`` would make the
    # kernel send ``SIGIO`` (or the ``F_SETSIG`` signal) to another process
    # when the worker reads or writes the child's pipes.
    for cmd in FCNTL_DENIED_COMMANDS:
        for syscall in ("fcntl", "fcntl64"):
            try:
                filt.add_rule(eperm, syscall, pyseccomp.Arg(1, pyseccomp.EQ, cmd))
            except (OSError, ValueError):
                continue  # ``fcntl64`` exists only on 32-bit architectures
    deny_filesystem_changes(filt, pyseccomp)
    filt.load()


def harden(limits: SandboxLimits) -> None:
    """Apply every restriction (module doc); raise :class:`HardenError` on any failure."""
    try:
        apply_rlimits(limits)
        _prctl(_PR_SET_DUMPABLE, 0)
        _prctl(_PR_SET_NO_NEW_PRIVS, 1)
        install_landlock(required=limits.require_landlock)
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

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
   for ``write()``; changing files is stopped by steps 3 and 4) and
   ``RLIMIT_CORE`` 0. ``RLIMIT_NPROC`` is deliberately *not* set: it counts
   every task (threads included) of the UID, which the child shares with the
   worker, so any value low enough to matter stops the child's own threads
   (PyMuPDF's table detection and Tesseract start threads; a non-root child
   with ``RLIMIT_NPROC`` 0 failed every PDF). New processes are stopped by
   seccomp instead (``fork``/``vfork``/non-thread ``clone`` ``EPERM``,
   ``clone3`` ``ENOSYS``, step 4).
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
4. A seccomp filter (libseccomp via ``pyseccomp``). **Default-kill
   allowlist**: a call outside it never runs and ends the child, which the
   runner reports as ``syscall_denied``. On x86-64 the default action is
   ``TRAP`` to a tiny SIGSYS handler (:func:`install_sigsys_reporter`) that
   writes the refused syscall's number to stderr and exits with
   :data:`EXIT_SYSCALL_DENIED`, so the runner can name the call; elsewhere it
   is ``KILL_PROCESS`` (the child dies with ``SIGSYS``). Only the calls in
   :data:`ALLOWED_SYSCALLS` run, plus a few with argument checks
   (:func:`install_seccomp`): ``clone`` only with ``CLONE_THREAD`` (threads),
   ``kill``/``tgkill``/``rt_sigqueueinfo``/``rt_tgsigqueueinfo`` only at the
   child's own pid, ``prlimit64`` only on itself, ``prctl`` only with
   :data:`PRCTL_ALLOWED_OPTIONS` (``PR_CAPBSET_READ``, a read-only query
   libcap's constructor makes), ``rt_sigaction`` except on ``SIGSYS`` (the
   reporter cannot be replaced; ``EPERM``), ``fcntl`` and ``ioctl``
   only with the commands in :data:`FCNTL_ALLOWED_COMMANDS` and
   :data:`IOCTL_ALLOWED_COMMANDS`, ``open``/``openat`` only without
   ``O_WRONLY``, ``O_RDWR``, ``O_CREAT`` or ``O_TRUNC``. The allowlist was
   derived from ``strace`` of every extractor (structured formats, PDF, OCR,
   images, EML, ZIP, the fuzz corpus) and kept to what CPython, PyMuPDF,
   Pillow and Tesseract need; anything else, including a call a library
   update starts to make, kills the child (a visible ``syscall_denied``
   failure, never a silent success). On top, the calls a parser exploit
   would want fail with ``EPERM`` instead of a kill, so the hostile-input
   tests can tell which layer stopped them: networking (``socket``,
   ``connect``, ``bind``, ...), running programs (``execve``, ``execveat``),
   new processes (``fork``, ``vfork``, ``clone`` without ``CLONE_THREAD``;
   ``clone3`` ``ENOSYS`` so the C library falls back to ``clone``), signals
   to other processes (the four calls above with another pid, ``tkill``,
   ``pidfd_*``, ``fcntl`` ``F_SETOWN``/``F_SETOWN_EX``/``F_SETSIG``; review
   finding P2-SR-R2-01: Landlock only scopes signals from ABI 6, so seccomp
   must stop them on its own), debugging and other processes' memory
   (``ptrace``, ``process_vm_*``), namespaces and mounts, kernel modules,
   BPF, keyrings, ``io_uring``, ``userfaultfd``, ``perf_event_open`` and
   identity changes (:data:`DENIED_SYSCALLS`). Every call that changes the
   filesystem fails with ``EPERM`` as well (:data:`FILESYSTEM_DENIED_SYSCALLS`:
   ``unlink``/``rename``/``truncate``/``chmod``/``chown``/``mkdir``/``rmdir``/
   ``link``/``symlink``/``utimensat``/xattrs/``fallocate``/``creat`` and
   their ``*at`` forms), as do ``open``/``openat`` with a write flag (no
   file, nor ``/proc/<pid>/mem``, can be opened for writing); ``openat2``,
   whose flags seccomp cannot inspect, fails with ``ENOSYS``. Syscalls of a
   non-native architecture are killed (libseccomp's bad-arch default). The
   argument checks assume the x86-64/arm64 argument order. libseccomp
   refuses overlapping rules with different actions, so every allow rule
   with an argument check is disjoint from its deny rule.

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
#: Exit code of the SIGSYS reporter (:func:`install_sigsys_reporter`): 128 + SIGSYS.
EXIT_SYSCALL_DENIED: Final = 159
EXIT_CODES: Final[Mapping[int, str]] = {
    EXIT_USAGE: "usage",
    EXIT_HARDEN_FAILED: "harden_failed",
    EXIT_MEMORY: "memory_limit",
    EXIT_INTERNAL: "crashed",
    EXIT_SYSCALL_DENIED: "syscall_denied",
}
#: The line the SIGSYS reporter writes to stderr, followed by the syscall
#: number in decimal and a newline (parsed by the runner).
SYSCALL_DENIED_PREFIX: Final = b"sandbox: seccomp denied syscall "

_PR_SET_DUMPABLE: Final = 4
_PR_SET_NO_NEW_PRIVS: Final = 38
_CLONE_THREAD: Final = 0x00010000
_SIGSYS: Final = 31  # x86-64 and arm64
#: ``__NR_SCMP_ERROR``: what libseccomp resolves an unknown syscall name to.
_UNKNOWN_SYSCALL: Final = -1
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
#: ``fcntl`` commands allowed (descriptor flags, duplication, status flags,
#: record locks and seals queries; ``linux/fcntl.h``). Any other is killed.
FCNTL_ALLOWED_COMMANDS: Final = (
    0,  # F_DUPFD
    1,  # F_GETFD
    2,  # F_SETFD
    3,  # F_GETFL
    4,  # F_SETFL
    5,  # F_GETLK
    6,  # F_SETLK
    7,  # F_SETLKW
    9,  # F_GETOWN
    1030,  # F_DUPFD_CLOEXEC
    1034,  # F_GET_SEALS
)
_PR_CAPBSET_READ: Final = 23
#: ``prctl`` options allowed; any other is killed. ``PR_CAPBSET_READ`` is a
#: read-only query of the child's own capability bounding set (the same facts
#: as ``CapBnd`` in its own ``/proc/<pid>/status``). libcap's constructor
#: probes it when the library is loaded, e.g. by glibc's ``systemd`` NSS
#: module (``libnss_systemd`` links libcap) when a library looks up the user
#: database: onnxruntime, which PyMuPDF's table detection loads, calls
#: ``getpwuid_r`` at import, the Landlock domain makes ``/etc/passwd``
#: unreadable, and NSS falls through to the next module of the host's
#: ``nsswitch.conf`` (``passwd: files systemd`` on Ubuntu hosts; CI run
#: 41915782513 died there with SIGSYS).
PRCTL_ALLOWED_OPTIONS: Final = (_PR_CAPBSET_READ,)
#: ``ioctl`` requests allowed: the terminal queries CPython makes on its
#: standard streams (``isatty``, window size), close-on-exec and bytes
#: readable. Any other request is killed.
IOCTL_ALLOWED_COMMANDS: Final = (
    0x5401,  # TCGETS
    0x5413,  # TIOCGWINSZ
    0x541B,  # FIONREAD
    0x5450,  # FIONCLEX
    0x5451,  # FIOCLEX
)
#: Allowed without argument checks (module doc, step 4). Names missing on an
#: architecture (``open``, ``stat``, ``arch_prctl`` on arm64) are skipped.
ALLOWED_SYSCALLS: Final = (
    # memory
    "brk",
    "mmap",
    "munmap",
    "mremap",
    "mprotect",
    "madvise",
    # reading files and descriptors (opening is checked; Landlock limits where)
    "read",
    "readv",
    "pread64",
    "preadv",
    "preadv2",
    "write",
    "writev",
    "lseek",
    "close",
    "close_range",
    "dup",
    "dup2",
    "dup3",
    "pipe",
    "pipe2",
    "fstat",
    "stat",
    "lstat",
    "newfstatat",
    "statx",
    "fstatfs",
    "statfs",
    "getdents",
    "getdents64",
    "access",
    "faccessat",
    "faccessat2",
    "readlink",
    "readlinkat",
    "getcwd",
    "fadvise64",
    "poll",
    "ppoll",
    "select",
    "pselect6",
    # time
    "clock_gettime",
    "clock_getres",
    "gettimeofday",
    "time",
    "nanosleep",
    "clock_nanosleep",
    # threads and synchronisation (thread creation: ``clone`` below)
    "futex",
    "set_robust_list",
    "get_robust_list",
    "set_tid_address",
    "rseq",
    "sched_yield",
    "sched_getaffinity",
    "membarrier",
    # own process facts
    "getpid",
    "gettid",
    "getppid",
    "getpgrp",
    "getpgid",
    "getsid",
    "getuid",
    "geteuid",
    "getgid",
    "getegid",
    "getresuid",
    "getresgid",
    "getgroups",
    "uname",
    "sysinfo",
    "getrlimit",
    "getrusage",
    "times",
    "arch_prctl",
    "getrandom",
    # signals to itself (sending: the checked calls below; ``rt_sigaction``
    # is checked too: the SIGSYS disposition is fixed once hardened)
    "rt_sigprocmask",
    "rt_sigreturn",
    "rt_sigpending",
    "rt_sigtimedwait",
    "rt_sigsuspend",
    "sigaltstack",
    "restart_syscall",
    # leaving
    "exit",
    "exit_group",
)

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


def _add_optional(
    filt: object, pyseccomp: ModuleType, action: int, name: str, *args: object
) -> None:
    """``filt.add_rule``, skipping a name libseccomp does not know at all.

    A known syscall this architecture lacks (``open``, ``fcntl64`` on arm64 or
    x86-64) resolves to a pseudo number that libseccomp accepts and ignores.
    Any other failure, such as a rule overlapping one with another action,
    raises: the child then refuses to run instead of loading a weaker filter.
    """
    if pyseccomp.resolve_syscall(pyseccomp.Arch.NATIVE, name) == _UNKNOWN_SYSCALL:
        return
    filt.add_rule(action, name, *args)  # type: ignore[attr-defined]


def allow_readonly_opens(filt: object, pyseccomp: ModuleType) -> None:
    """``open``/``openat`` without a write flag (write forms: :func:`deny_filesystem_changes`)."""
    mask = 0
    for flag in WRITE_OPEN_FLAGS:
        mask |= flag
    for syscall, flags_arg in (("open", 1), ("openat", 2)):
        _add_optional(
            filt,
            pyseccomp,
            pyseccomp.ALLOW,
            syscall,
            pyseccomp.Arg(flags_arg, pyseccomp.MASKED_EQ, mask, 0),
        )


#: x86-64 machine code of the SIGSYS reporter, a ``SA_SIGINFO`` handler
#: ``(int sig, siginfo_t *info, void *ctx)`` that formats ``info->si_syscall``
#: (offset 24) in decimal after :data:`SYSCALL_DENIED_PREFIX`, writes the line
#: to fd 2 and calls ``exit_group(159)``; it touches nothing but its own stack
#: frame, so it is async-signal-safe and needs only ``write`` and
#: ``exit_group``, both on the allowlist. Assembled from (GNU as, Intel syntax)::
#:
#:     mov  eax, dword ptr [rsi+24]   ; si_syscall
#:     sub  rsp, 96
#:     lea  rdi, [rsp+95]
#:     mov  byte ptr [rdi], 10        ; trailing newline
#:     mov  ecx, 10
#: 1:  xor  edx, edx                  ; digits, last first
#:     div  ecx
#:     add  dl, 48
#:     dec  rdi
#:     mov  byte ptr [rdi], dl
#:     test eax, eax
#:     jnz  1b
#:     lea  rsi, [rip+msg]            ; prefix in front of the digits
#:     mov  ecx, 32
#:     sub  rdi, rcx
#:     mov  r8, rdi
#:     cld
#:     rep  movsb
#:     mov  rsi, r8                   ; write(2, line, length)
#:     lea  rdx, [rsp+96]
#:     sub  rdx, rsi
#:     mov  edi, 2
#:     mov  eax, 1
#:     syscall
#:     mov  edi, 159                  ; exit_group(EXIT_SYSCALL_DENIED)
#:     mov  eax, 231
#:     syscall
#:     hlt
#: msg: .ascii "sandbox: seccomp denied syscall "
_SIGSYS_REPORTER_X86_64: Final = (
    bytes.fromhex(
        "8b46184883ec60488d7c245fc6070ab90a00000031d2f7f180c23048ffcf8817"
        "85c075f0488d3532000000b9200000004829cf4989f8fcf3a44c89c6488d5424"
        "604829f2bf02000000b8010000000f05bf9f000000b8e70000000f05f4"
    )
    + SYSCALL_DENIED_PREFIX
)
_SA_SIGINFO: Final = 0x00000004
_PROT_READ: Final = 0x1
_PROT_WRITE: Final = 0x2
_PROT_EXEC: Final = 0x4
_MAP_PRIVATE: Final = 0x02
_MAP_ANONYMOUS: Final = 0x20


class _SigAction(ctypes.Structure):
    """glibc's ``struct sigaction`` on x86-64 (the C library sets the restorer)."""

    _fields_ = (
        ("sa_sigaction", ctypes.c_void_p),
        ("sa_mask", ctypes.c_ulong * 16),
        ("sa_flags", ctypes.c_int),
        ("sa_restorer", ctypes.c_void_p),
    )


def install_sigsys_reporter() -> bool:
    """Report the number of a syscall the filter refuses, then exit (operability).

    With a reporter in place the filter's default action is ``TRAP`` instead
    of ``KILL_PROCESS``: the refused syscall is still never executed, the
    kernel delivers ``SIGSYS`` with the syscall number, and the handler
    (:data:`_SIGSYS_REPORTER_X86_64`) writes ``sandbox: seccomp denied
    syscall <n>`` to stderr and ends the whole process with
    :data:`EXIT_SYSCALL_DENIED`; the runner reports ``syscall_denied`` with
    the number and name. ``rt_sigaction`` on ``SIGSYS`` is refused once the
    filter is loaded, so nothing in the child can replace the handler, and a
    thread that blocks ``SIGSYS`` is still killed (the kernel resets a
    blocked forced signal to its default action). Only x86-64 has a reporter;
    elsewhere this returns ``False`` and the filter kills as before.
    """
    if os.uname().machine != "x86_64":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = (
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_long,
    )
    libc.mprotect.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
    size = resource.getpagesize()
    page = libc.mmap(None, size, _PROT_READ | _PROT_WRITE, _MAP_PRIVATE | _MAP_ANONYMOUS, -1, 0)
    if page in (None, ctypes.c_void_p(-1).value):
        raise HardenError(f"mmap failed: errno {ctypes.get_errno()}")
    ctypes.memmove(page, _SIGSYS_REPORTER_X86_64, len(_SIGSYS_REPORTER_X86_64))
    if libc.mprotect(page, size, _PROT_READ | _PROT_EXEC) != 0:
        raise HardenError(f"mprotect failed: errno {ctypes.get_errno()}")
    action = _SigAction(sa_sigaction=page, sa_flags=_SA_SIGINFO)
    for index in range(len(action.sa_mask)):
        action.sa_mask[index] = ~0 & 0xFFFFFFFFFFFFFFFF  # every signal blocked inside
    if libc.sigaction(_SIGSYS, ctypes.byref(action), None) != 0:
        raise HardenError(f"sigaction(SIGSYS) failed: errno {ctypes.get_errno()}")
    return True


def install_seccomp(*, report: bool = False) -> None:
    """Load the filter (module doc, step 4).

    ``report``: a SIGSYS reporter is installed (:func:`install_sigsys_reporter`),
    so a refused syscall traps to it instead of killing the process outright.
    """
    pyseccomp = _SECCOMP
    if pyseccomp is None:
        raise HardenError("libseccomp is not available")
    allow = pyseccomp.ALLOW
    eperm = pyseccomp.ERRNO(errno.EPERM)
    pid = os.getpid()
    filt = pyseccomp.SyscallFilter(defaction=pyseccomp.TRAP if report else pyseccomp.KILL_PROCESS)
    for name in ALLOWED_SYSCALLS:
        _add_optional(filt, pyseccomp, allow, name)
    for name in DENIED_SYSCALLS:
        _add_optional(filt, pyseccomp, eperm, name)
    # New processes: clone without CLONE_THREAD (threads stay possible).
    thread = pyseccomp.Arg(0, pyseccomp.MASKED_EQ, _CLONE_THREAD, _CLONE_THREAD)
    filt.add_rule(allow, "clone", thread)
    filt.add_rule(eperm, "clone", pyseccomp.Arg(0, pyseccomp.MASKED_EQ, _CLONE_THREAD, 0))
    filt.add_rule(pyseccomp.ERRNO(errno.ENOSYS), "clone3")
    # Signals only to ourselves (review finding P2-SR-R2-01): every call whose
    # first argument names the target process or thread group. Without
    # Landlock signal scoping (ABI 6+, Linux 6.12+) the kernel lets a
    # same-UID process be signalled by ``rt_sigqueueinfo`` and
    # ``rt_tgsigqueueinfo`` (negative ``si_code``) as well as by ``kill``.
    for name in SIGNAL_SELF_ONLY_SYSCALLS:
        filt.add_rule(allow, name, pyseccomp.Arg(0, pyseccomp.EQ, pid))
        filt.add_rule(eperm, name, pyseccomp.Arg(0, pyseccomp.NE, pid))
    # The SIGSYS disposition is fixed from here on: the reporter (or the
    # default action) cannot be replaced; every other signal's handler can.
    filt.add_rule(allow, "rt_sigaction", pyseccomp.Arg(0, pyseccomp.NE, _SIGSYS))
    filt.add_rule(eperm, "rt_sigaction", pyseccomp.Arg(0, pyseccomp.EQ, _SIGSYS))
    # ``prctl`` only to read the capability bounding set (module doc, step 4).
    for option in PRCTL_ALLOWED_OPTIONS:
        filt.add_rule(allow, "prctl", pyseccomp.Arg(0, pyseccomp.EQ, option))
    # Resource limits of this process only (0 = self): never the worker's.
    for target in (0, pid):
        filt.add_rule(allow, "prlimit64", pyseccomp.Arg(0, pyseccomp.EQ, target))
    # Signals by file ownership: ``F_SETOWN``/``F_SETOWN_EX`` would make the
    # kernel send ``SIGIO`` (or the ``F_SETSIG`` signal) to another process
    # when the worker reads or writes the child's pipes.
    for syscall in ("fcntl", "fcntl64"):
        for cmd in FCNTL_DENIED_COMMANDS:
            _add_optional(filt, pyseccomp, eperm, syscall, pyseccomp.Arg(1, pyseccomp.EQ, cmd))
        for cmd in FCNTL_ALLOWED_COMMANDS:
            _add_optional(filt, pyseccomp, allow, syscall, pyseccomp.Arg(1, pyseccomp.EQ, cmd))
    for request in IOCTL_ALLOWED_COMMANDS:
        filt.add_rule(allow, "ioctl", pyseccomp.Arg(1, pyseccomp.EQ, request))
    allow_readonly_opens(filt, pyseccomp)
    deny_filesystem_changes(filt, pyseccomp)
    filt.load()


def harden(limits: SandboxLimits) -> None:
    """Apply every restriction (module doc); raise :class:`HardenError` on any failure."""
    try:
        apply_rlimits(limits)
        _prctl(_PR_SET_DUMPABLE, 0)
        _prctl(_PR_SET_NO_NEW_PRIVS, 1)
        install_landlock(required=limits.require_landlock)
        install_seccomp(report=install_sigsys_reporter())
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

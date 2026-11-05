# SPDX-License-Identifier: AGPL-3.0-or-later
"""A sandbox child whose "extractors" misbehave on purpose (tests only).

Run by the real runner as ``python -I -B -m tests.fixtures.sandbox_evil``
(``SandboxConfig(module=...)``). It goes through exactly the production
child's code path (:func:`app.ingestion.sandbox.child.run`: job validation,
hardening, stdin, result) with these extractors instead of the shipped ones,
so the tests show that the *hardening* stops them. Each probe reports the
errno it got in ``warnings``; anything that should have been impossible but
succeeded reports ``...:ok``.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import platform
import signal
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable

from app.ingestion.sandbox.child import run
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult


def _errno_of(action: Callable[[], object]) -> str:
    try:
        action()
    except OSError as exc:
        return str(exc.errno)
    return "ok"


def _result(kind: str, *warnings: str) -> ExtractResult:
    return ExtractResult(kind=kind, ok=True, warnings=list(warnings))


def membomb(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    hoard = []
    while True:
        hoard.append(bytearray(64 * 1024 * 1024))


def cpuloop(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    counter = 0
    while True:
        counter += 1


def sleeper(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    time.sleep(3600)
    return _result(kind)


def forkbomb(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    outcomes = []
    for _ in range(20):
        try:
            pid = os.fork()
        except OSError as exc:
            outcomes.append(str(exc.errno))
            continue
        if pid == 0:  # only reached if the control is gone
            os._exit(0)
        outcomes.append("ok")
        os.waitpid(pid, 0)
    return _result(kind, *(f"fork:{o}" for o in sorted(set(outcomes))))


def network(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    return _result(
        kind,
        "socket_inet:" + _errno_of(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM)),
        "socket_unix:" + _errno_of(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)),
        "socket_udp:" + _errno_of(lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM)),
    )


def execve(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    true = "/bin/true" if os.path.exists("/bin/true") else "/usr/bin/true"
    return _result(kind, "execve:" + _errno_of(lambda: os.execv(true, [true])))  # noqa: S606


def kill_others(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    return _result(
        kind,
        "kill_parent:" + _errno_of(lambda: os.kill(os.getppid(), 0)),
        "kill_init:" + _errno_of(lambda: os.kill(1, 0)),
        "kill_self:" + _errno_of(lambda: os.kill(os.getpid(), 0)),
    )


# (rt_sigqueueinfo, rt_tgsigqueueinfo) by architecture.
_SIGQUEUE_SYSCALLS = {"x86_64": (129, 297), "aarch64": (138, 240)}
_SI_QUEUE = -1
_F_SETOWN, _F_SETSIG, _F_SETOWN_EX, _F_OWNER_PID = 8, 10, 15, 1


def _sigqueue(number: int, *pids: int) -> str:
    """``rt_(tg)sigqueueinfo(pids..., 0, info)`` with ``si_code`` SI_QUEUE.

    Signal 0 runs every permission check but delivers nothing, so the
    probe never harms the parent even when the control is missing.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    info = ctypes.create_string_buffer(struct.pack("iii", 0, 0, _SI_QUEUE), 128)
    ctypes.set_errno(0)
    args = [ctypes.c_long(pid) for pid in pids]
    if libc.syscall(ctypes.c_long(number), *args, ctypes.c_long(0), info) == 0:
        return "ok"
    return str(ctypes.get_errno())


def signal_others(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    """Signal the parent without ``kill``: queued signals and SIGIO file ownership."""
    parent, me = os.getppid(), os.getpid()
    queue, tgqueue = _SIGQUEUE_SYSCALLS[platform.machine()]
    read_end, write_end = os.pipe()
    owner_ex = struct.pack("ii", _F_OWNER_PID, parent)
    return _result(
        kind,
        "sigqueue_parent:" + _sigqueue(queue, parent),
        "tgsigqueue_parent:" + _sigqueue(tgqueue, parent, parent),
        "sigqueue_init:" + _sigqueue(queue, 1),
        "sigqueue_self:" + _sigqueue(queue, me),
        "setown_parent:" + _errno_of(lambda: fcntl.fcntl(write_end, _F_SETOWN, parent)),
        "setown_ex_parent:" + _errno_of(lambda: fcntl.fcntl(read_end, _F_SETOWN_EX, owner_ex)),
        "setsig:" + _errno_of(lambda: fcntl.fcntl(write_end, _F_SETSIG, 0)),
    )


def writefile(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    def write() -> None:
        with open("scratch.bin", "wb") as handle:
            handle.write(b"x" * 1024)

    return _result(kind, "write:" + _errno_of(write))


def escape_proc(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    """Reach into the parent (the worker): its environment, memory and open files."""
    parent = f"/proc/{os.getppid()}"

    def environ() -> str:
        with open(f"{parent}/environ", "rb") as handle:
            return "SECRET" if b"PROBE_SECRET=" in handle.read() else "read"

    def mem_read() -> None:
        with open(f"{parent}/mem", "rb") as handle:
            handle.seek(0x400000)
            handle.read(1)

    def mem_write() -> None:
        with open(f"{parent}/mem", "r+b"):
            pass

    def environ_probe() -> str:
        try:
            return environ()
        except OSError as exc:
            return str(exc.errno)

    return _result(
        kind,
        "environ:" + environ_probe(),
        "mem_read:" + _errno_of(mem_read),
        "mem_write:" + _errno_of(mem_write),
        "fd:" + _errno_of(lambda: os.listdir(f"{parent}/fd")),
    )


def fs_tamper(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    """Change files the child's UID owns; ``data`` is a directory holding ``victim`` and ``sub``."""
    root = data.decode()
    victim = os.path.join(root, "victim")

    def open_mode(mode: str) -> Callable[[], object]:
        def action() -> None:
            with open(victim, mode):
                pass

        return action

    def create() -> None:
        with open(os.path.join(root, "new"), "xb"):
            pass

    return _result(
        kind,
        "unlink:" + _errno_of(lambda: os.unlink(victim)),
        "rename:" + _errno_of(lambda: os.rename(victim, os.path.join(root, "moved"))),
        "truncate:" + _errno_of(lambda: os.truncate(victim, 0)),
        "chmod:" + _errno_of(lambda: os.chmod(victim, 0o700)),
        "chown:" + _errno_of(lambda: os.chown(victim, os.getuid(), os.getgid())),
        "utime:" + _errno_of(lambda: os.utime(victim, (0, 0))),
        "open_rw:" + _errno_of(open_mode("r+b")),
        "open_append:" + _errno_of(open_mode("ab")),
        "open_trunc:" + _errno_of(open_mode("wb")),
        "create:" + _errno_of(create),
        "mkdir:" + _errno_of(lambda: os.mkdir(os.path.join(root, "made"))),
        "rmdir:" + _errno_of(lambda: os.rmdir(os.path.join(root, "sub"))),
        "symlink:" + _errno_of(lambda: os.symlink(victim, os.path.join(root, "sym"))),
        "link:" + _errno_of(lambda: os.link(victim, os.path.join(root, "hard"))),
    )


def read_outside(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    """Read a file outside the interpreter's paths (``data`` is its path), e.g. a key file."""

    def read() -> str:
        with open(data.decode(), "rb") as handle:
            return "SECRET" if handle.read().startswith(b"PROBE_SECRET") else "read"

    try:
        outcome = read()
    except OSError as exc:
        outcome = str(exc.errno)
    return _result(kind, "read:" + outcome, "listdir:" + _errno_of(lambda: os.listdir("/")))


def bigoutput(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    block = b"x" * (1024 * 1024)
    for _ in range(64):
        sys.stdout.buffer.write(block)
    sys.stdout.buffer.flush()
    return _result(kind)


def whoami(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    with open("/proc/self/status", encoding="ascii") as handle:
        status = dict(line.split(":", 1) for line in handle.read().splitlines() if ":" in line)
    return _result(
        kind,
        f"uid:{os.getuid()}",
        f"env:{','.join(sorted(os.environ))}",
        f"cwd:{os.getcwd()}",
        f"nnp:{status.get('NoNewPrivs', '').strip()}",
        f"seccomp:{status.get('Seccomp', '').strip()}",
        f"input:{len(data)}",
        f"pgrp:{int(os.getpgrp() == os.getpid())}",
        f"sigxcpu:{int(signal.getsignal(signal.SIGXCPU) == signal.SIG_DFL)}",
    )


def unlisted_syscall(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    """A harmless call outside the allowlist: ``setpriority`` (lowering its own priority).

    Without a seccomp allowlist this succeeds; with it the child is killed
    (``SIGSYS``) before it can report anything.
    """
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    return _result(kind, "setpriority:ok")


def sigsys_takeover(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    """Replace or ignore the SIGSYS disposition, then make a call outside the allowlist.

    Must still die with ``syscall_denied``: ``rt_sigaction`` on SIGSYS is refused
    after hardening, so a refused call can never be survived.
    """
    for handler in (signal.SIG_IGN, lambda signum, frame: None):
        try:
            signal.signal(signal.SIGSYS, handler)
        except OSError:
            continue
        sys.stderr.write("sigsys_handler_replaced\n")
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    return _result(kind, "survived:ok")


def user_lookup(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    """Not hostile: look up the user and group databases, as onnxruntime does at import.

    Landlock makes ``/etc/passwd`` unreadable, so glibc falls through to the
    next NSS module of the host's ``nsswitch.conf``; on Ubuntu hosts that is
    ``systemd``, whose library loads libcap, whose constructor calls
    ``prctl(PR_CAPBSET_READ)`` (CI run 41915782513 died there). Then load
    libcap directly too, so the probe runs on hosts without that NSS module.
    """
    import grp
    import pwd

    found = []
    for label, lookup in (("pwd", pwd.getpwuid), ("grp", grp.getgrgid)):
        try:
            lookup(os.getuid())
            found.append(f"{label}:found")
        except KeyError:
            found.append(f"{label}:missing")
    try:
        ctypes.CDLL("libcap.so.2")
        found.append("libcap:loaded")
    except OSError:
        found.append("libcap:absent")
    return _result(kind, *found)


def prctl_probe(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    """``prctl(PR_CAPBSET_READ)`` (allowed: a read-only query of its own bounding set)."""
    libc = ctypes.CDLL(None, use_errno=True)
    cap_sys_admin = 21
    return _result(kind, f"capbset_read:{'ok' if libc.prctl(23, cap_sys_admin) >= 0 else 'err'}")


def prctl_dumpable(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    """Make itself dumpable again (``PR_SET_DUMPABLE`` 1): any other ``prctl`` is killed."""
    libc = ctypes.CDLL(None, use_errno=True)
    outcome = libc.prctl(4, 1, 0, 0, 0)
    return _result(kind, f"set_dumpable:{'ok' if outcome == 0 else ctypes.get_errno()}")


def threads(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    """Not hostile: start and join a few threads, as PyMuPDF and Tesseract do.

    A per-UID task limit (``RLIMIT_NPROC`` 0) or a seccomp rule that stopped
    ``clone(CLONE_THREAD)`` would make this fail for an unprivileged child.
    """
    results: list[int] = []
    workers = [threading.Thread(target=results.append, args=(n,)) for n in range(4)]
    try:
        for worker in workers:
            worker.start()
    except RuntimeError as exc:
        return _result(kind, f"threads:{type(exc).__name__}")
    for worker in workers:
        worker.join()
    return _result(kind, f"threads:{len(results)}")


def echo(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    return _result(kind, f"input:{len(data)}")


def bad_shape(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    sys.stdout.buffer.write(b'{"format_version":1,"kind":"' + kind.encode() + b'","ok":true,')
    sys.stdout.buffer.write(b'"injected":"x"}')
    sys.stdout.buffer.flush()
    os._exit(0)


def stray_stdout(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    """A native library writing to fd 1 ahead of the result corrupts the JSON."""
    os.write(1, b"warning: secret-ish text\n")
    return ExtractResult(kind=kind, ok=True)


def forged_statement(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    """Claims a parsed, reconciled PDF statement: only the worker's parsers may."""
    from datetime import date
    from decimal import Decimal

    from app.ingestion.sandbox.protocol import (
        ExtractedTransaction,
        ParserRef,
        Reconciliation,
        Statement,
    )

    statement = Statement(
        source_format="pdf",
        transactions=[ExtractedTransaction(posted=date(2026, 1, 2), amount=Decimal("-9.99"))],
        parser=ParserRef(name="statement_generic", version="1", kind="statement"),
        reconciliation=Reconciliation(
            status="ok", total=Decimal("-9.99"), tolerance=Decimal("0.005")
        ),
    )
    return ExtractResult(kind=kind, ok=True, statements=[statement])


def forged_parse_info(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    from app.ingestion.sandbox.protocol import ParseInfo

    return ExtractResult(kind=kind, ok=True, parse=ParseInfo(threshold=0.5))


EXTRACTORS = {
    "forged_statement": forged_statement,
    "forged_parse_info": forged_parse_info,
    "membomb": membomb,
    "cpuloop": cpuloop,
    "sleeper": sleeper,
    "forkbomb": forkbomb,
    "network": network,
    "execve": execve,
    "kill_others": kill_others,
    "signal_others": signal_others,
    "writefile": writefile,
    "escape_proc": escape_proc,
    "fs_tamper": fs_tamper,
    "read_outside": read_outside,
    "bigoutput": bigoutput,
    "whoami": whoami,
    "echo": echo,
    "threads": threads,
    "unlisted_syscall": unlisted_syscall,
    "sigsys_takeover": sigsys_takeover,
    "user_lookup": user_lookup,
    "prctl_probe": prctl_probe,
    "prctl_dumpable": prctl_dumpable,
    "bad_shape": bad_shape,
    "stray_stdout": stray_stdout,
}


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:], EXTRACTORS))

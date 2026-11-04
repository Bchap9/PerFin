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

import os
import signal
import socket
import sys
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


def writefile(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    def write() -> None:
        with open("scratch.bin", "wb") as handle:
            handle.write(b"x" * 1024)

    return _result(kind, "write:" + _errno_of(write))


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


def echo(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    return _result(kind, f"input:{len(data)}")


def bad_shape(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    sys.stdout.buffer.write(b'{"format_version":1,"kind":"' + kind.encode() + b'","ok":true,')
    sys.stdout.buffer.write(b'"injected":"x"}')
    sys.stdout.buffer.flush()
    os._exit(0)


EXTRACTORS = {
    "membomb": membomb,
    "cpuloop": cpuloop,
    "sleeper": sleeper,
    "forkbomb": forkbomb,
    "network": network,
    "execve": execve,
    "kill_others": kill_others,
    "writefile": writefile,
    "bigoutput": bigoutput,
    "whoami": whoami,
    "echo": echo,
    "bad_shape": bad_shape,
}


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:], EXTRACTORS))

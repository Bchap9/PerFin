# SPDX-License-Identifier: AGPL-3.0-or-later
"""The extraction sandbox contains hostile extractors (ADR-0006, SECURITY.md C1/C2).

Each test runs the real runner and the real child code path
(``app.ingestion.sandbox.child.run``) with a deliberately hostile extractor
from ``tests/fixtures/sandbox_evil.py``: the limits, seccomp filter and
parent-side controls must stop it. Removing any of those controls makes the
matching test fail.
"""

from __future__ import annotations

import errno
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from app.ingestion.sandbox import child
from app.ingestion.sandbox.child import DENIED_SYSCALLS
from app.ingestion.sandbox.protocol import (
    ExtractOptions,
    ExtractResult,
    SandboxJob,
    SandboxLimits,
)
from app.ingestion.sandbox.runner import SandboxConfig, SandboxError, run_extraction

pytestmark = pytest.mark.security

EVIL = SandboxConfig(
    module="tests.fixtures.sandbox_evil",
    memory_bytes=384 * 1024 * 1024,
    cpu_seconds=2,
    timeout_seconds=20,
    max_output_bytes=1024 * 1024,
)
EPERM = str(errno.EPERM)


def _warnings(kind: str, config: SandboxConfig = EVIL, data: bytes = b"input") -> dict[str, str]:
    result = run_extraction(kind, data, config=config)
    assert result.ok
    return dict(item.split(":", 1) for item in result.warnings)


def test_memory_bomb_is_stopped_by_rlimit_as() -> None:
    with pytest.raises(SandboxError) as caught:
        run_extraction("membomb", b"x", config=EVIL)
    assert caught.value.code == "memory_limit"


def test_cpu_loop_is_killed_by_rlimit_cpu() -> None:
    started = time.monotonic()
    with pytest.raises(SandboxError) as caught:
        run_extraction("cpuloop", b"x", config=EVIL)
    assert caught.value.code == "cpu_limit"
    # Killed by the CPU limit, well before the wall-clock timeout.
    assert time.monotonic() - started < EVIL.timeout_seconds


def test_sleeping_child_is_killed_by_the_wall_clock_timeout() -> None:
    config = replace(EVIL, timeout_seconds=1.5)
    started = time.monotonic()
    with pytest.raises(SandboxError) as caught:
        run_extraction("sleeper", b"x", config=config)
    assert caught.value.code == "timeout"
    assert time.monotonic() - started < 10


def test_fork_bomb_cannot_fork() -> None:
    assert _warnings("forkbomb") == {"fork": EPERM}


def test_no_network_sockets() -> None:
    assert _warnings("network") == {
        "socket_inet": EPERM,
        "socket_unix": EPERM,
        "socket_udp": EPERM,
    }


def test_cannot_execve() -> None:
    assert _warnings("execve") == {"execve": EPERM}


def test_cannot_signal_other_processes() -> None:
    found = _warnings("kill_others")
    assert found["kill_parent"] == EPERM
    assert found["kill_init"] == EPERM
    assert found["kill_self"] == "ok"


def test_cannot_write_files() -> None:
    assert _warnings("writefile") == {"write": str(errno.EFBIG)}


def test_huge_output_is_cut_off_and_killed() -> None:
    with pytest.raises(SandboxError) as caught:
        run_extraction("bigoutput", b"x", config=EVIL)
    assert caught.value.code == "output_too_large"


def test_output_must_match_the_schema() -> None:
    with pytest.raises(SandboxError) as caught:
        run_extraction("bad_shape", b"x", config=EVIL)
    assert caught.value.code == "bad_output"


def test_child_environment_is_empty_private_and_filtered() -> None:
    found = _warnings("whoami", data=b"abc")
    assert found["env"] == "0"
    assert found["nnp"] == "1"
    assert found["seccomp"] == "2"  # filter mode
    assert found["input"] == "3"  # stdin carried the bytes
    assert found["pgrp"] == "1"  # own process group: the parent kills the group
    cwd = Path(found["cwd"])
    assert cwd.name.startswith("perfin-sandbox-")
    assert not cwd.exists()  # removed after the run


def test_drops_to_dedicated_uid_when_root() -> None:
    config = replace(EVIL, uid=65534, gid=65534)
    found = _warnings("whoami", config=config)
    if os.geteuid() == 0:
        assert found["uid"] == "65534"
    else:
        # Not root: the worker already runs unprivileged; no switch is attempted.
        assert found["uid"] == str(os.getuid())


def test_input_over_the_limit_is_refused() -> None:
    config = replace(EVIL, max_input_bytes=1000)
    with pytest.raises(SandboxError) as caught:
        run_extraction("echo", b"x" * 5000, config=config)
    assert caught.value.code == "input_too_large"


def test_streamed_input_reaches_the_child() -> None:
    chunks = (b"y" * 70_000 for _ in range(10))
    assert _warnings("echo", data=b"") == {"input": "0"}
    result = run_extraction("echo", chunks, config=EVIL)
    assert result.warnings == ["input:700000"]


def test_unknown_kind_is_a_result_not_a_crash() -> None:
    result = run_extraction("nope", b"x", config=EVIL)
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "unknown_kind"


def test_child_refuses_to_run_unhardened(monkeypatch: pytest.MonkeyPatch) -> None:
    # In-process, with hardening made to fail (the real one would lock up the
    # test process): the extractor must never run and the exit code says why.
    calls: list[bytes] = []

    def spy(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
        calls.append(data)
        return ExtractResult(kind=kind, ok=True)

    def failing_harden(limits: SandboxLimits) -> None:
        raise child.HardenError("seccomp unavailable")

    monkeypatch.setattr(child, "harden", failing_harden)
    code = child.run([SandboxJob(kind="spy").model_dump_json()], {"spy": spy})
    assert code == child.EXIT_HARDEN_FAILED
    assert calls == []


def test_runner_reports_harden_failure_code() -> None:
    from app.ingestion.sandbox.runner import _classify_exit

    assert _classify_exit(child.EXIT_HARDEN_FAILED) == "harden_failed"
    assert _classify_exit(child.EXIT_MEMORY) == "memory_limit"


def test_deny_list_covers_the_essentials() -> None:
    for name in ("socket", "connect", "bind", "execve", "execveat", "fork", "vfork", "ptrace",
                 "mount", "unshare", "setns", "bpf", "io_uring_setup", "keyctl",
                 "process_vm_readv", "init_module", "kexec_load", "setuid", "chroot"):
        assert name in DENIED_SYSCALLS

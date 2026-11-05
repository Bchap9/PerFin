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
import json
import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import time
from collections.abc import Iterator
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


def test_syscall_outside_the_allowlist_kills_the_child() -> None:
    """The seccomp filter is a default-kill allowlist (C1), not a deny list.

    ``setpriority`` is harmless and on no deny list: only the allowlist
    stops it, so a default-allow filter makes this test fail.
    """
    assert "setpriority" not in child.ALLOWED_SYSCALLS
    assert "setpriority" not in DENIED_SYSCALLS
    with pytest.raises(SandboxError) as caught:
        run_extraction("unlisted_syscall", b"x", config=EVIL)
    assert caught.value.code == "syscall_denied"
    if os.uname().machine == "x86_64":
        # The SIGSYS reporter names the refused call (docs/OPERATIONS.md).
        assert caught.value.detail.endswith("syscall 141 (setpriority)")
        assert "syscall 141 (setpriority)" in str(caught.value)


def test_a_refused_syscall_cannot_be_survived_by_replacing_the_sigsys_handler() -> None:
    """``rt_sigaction`` on SIGSYS is refused once hardened: the reporter stays, the child dies."""
    with pytest.raises(SandboxError) as caught:
        run_extraction("sigsys_takeover", b"x", config=EVIL)
    assert caught.value.code == "syscall_denied"
    assert "sigsys_handler_replaced" not in caught.value.stderr


def test_denied_syscall_is_read_only_from_the_reporter_line() -> None:
    from app.ingestion.sandbox.runner import _classify_exit, denied_syscall

    assert _classify_exit(child.EXIT_SYSCALL_DENIED) == "syscall_denied"
    assert _classify_exit(-31) == "syscall_denied"  # killed outright (no reporter)
    line = child.SYSCALL_DENIED_PREFIX.decode() + "141\n"
    assert denied_syscall("noise\n" + line).startswith("141 (")
    assert denied_syscall("") == ""
    assert denied_syscall("sandbox: seccomp denied syscall 99999999999\n") == ""
    assert denied_syscall("sandbox: seccomp denied syscall rm -rf\n") == ""


def test_allowlist_still_kills_without_the_filesystem_rules() -> None:
    config = replace(EVIL, module="tests.fixtures.sandbox_evil_no_fs_seccomp")
    with pytest.raises(SandboxError) as caught:
        run_extraction("unlisted_syscall", b"x", config=config)
    assert caught.value.code == "syscall_denied"


def test_allowlist_and_deny_lists_do_not_overlap() -> None:
    """libseccomp refuses overlapping rules with different actions (the child would not start)."""
    allowed = set(child.ALLOWED_SYSCALLS)
    assert not allowed & set(DENIED_SYSCALLS)
    assert not allowed & set(child.FILESYSTEM_DENIED_SYSCALLS)
    assert not set(child.FCNTL_ALLOWED_COMMANDS) & set(child.FCNTL_DENIED_COMMANDS)
    checked = {
        "clone",
        "clone3",
        "prlimit64",
        "fcntl",
        "ioctl",
        "open",
        "openat",
        "openat2",
        "rt_sigaction",
        "prctl",
    }
    assert not allowed & (checked | set(child.SIGNAL_SELF_ONLY_SYSCALLS))


def test_cannot_signal_other_processes() -> None:
    found = _warnings("kill_others")
    assert found["kill_parent"] == EPERM
    assert found["kill_init"] == EPERM
    assert found["kill_self"] == "ok"


@pytest.mark.parametrize(
    "module",
    [
        "tests.fixtures.sandbox_evil",
        # Landlock signal scoping (ABI 6+) absent: only seccomp stops these
        # (review finding P2-SR-R2-01), so removing a seccomp rule fails here.
        "tests.fixtures.sandbox_evil_landlock_abi4",
        "tests.fixtures.sandbox_evil_no_landlock",
    ],
)
def test_cannot_signal_other_processes_without_kill(module: str) -> None:
    found = _warnings("signal_others", replace(EVIL, module=module))
    assert found == {
        "sigqueue_parent": EPERM,
        "tgsigqueue_parent": EPERM,
        "sigqueue_init": EPERM,
        "sigqueue_self": "ok",
        "setown_parent": EPERM,
        "setown_ex_parent": EPERM,
        "setsig": EPERM,
    }


def test_cannot_write_files() -> None:
    # open(O_WRONLY|O_CREAT|O_TRUNC) is refused by seccomp before RLIMIT_FSIZE matters.
    assert _warnings("writefile") == {"write": EPERM}


# ---- Isolation from the worker and its files (review finding P2-SR-01) ------
#
# The shipped worker runs as UID 10001 and the child under the same UID, so
# these probes run through a *driver* process that plays the worker: as an
# unprivileged UID (65534 when the tests run as root), with a secret in its
# environment, calling the real runner. Each sandbox layer is also tested on
# its own (fixture variants without Landlock / without the seccomp filesystem
# rules), so removing any one layer makes a test fail.

FULL = "tests.fixtures.sandbox_evil"
NO_LANDLOCK = "tests.fixtures.sandbox_evil_no_landlock"
NO_FS_SECCOMP = "tests.fixtures.sandbox_evil_no_fs_seccomp"
EACCES = str(errno.EACCES)
PROBE_UID = 65534

_DRIVER = """
import json, sys
from app.ingestion.sandbox.runner import SandboxConfig, run_extraction
module, kind, data = sys.argv[1], sys.argv[2], sys.argv[3].encode()
config = SandboxConfig(
    module=module, memory_bytes=384 * 1024 * 1024, cpu_seconds=2,
    timeout_seconds=20, max_output_bytes=1024 * 1024,
)
result = run_extraction(kind, data, config=config)
assert result.ok, result
print(json.dumps(result.warnings))
"""


def _unprivileged() -> int | None:
    return PROBE_UID if os.geteuid() == 0 else None


def _owned_by_driver(path: Path) -> None:
    if (uid := _unprivileged()) is not None:
        os.chown(path, uid, uid)


@pytest.fixture
def driver_dir() -> Iterator[Path]:
    """A private directory of the worker stand-in's UID that it can reach.

    Not ``tmp_path``: pytest's base directory is ``0700`` root's, so a
    probe there would be refused by ordinary permissions, not by the sandbox.
    """
    path = Path(tempfile.mkdtemp(prefix="perfin-sandbox-probe-", dir="/tmp"))
    try:
        _owned_by_driver(path)
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _drive(module: str, kind: str, data: str) -> dict[str, str]:
    """Run ``kind`` from ``module`` through the real runner in an unprivileged worker stand-in."""
    uid = _unprivileged()
    completed = subprocess.run(  # nosec B603 - fixed argv, our interpreter
        [sys.executable, "-I", "-c", _DRIVER, module, kind, data],
        env={"PROBE_SECRET": "worker-master-key"},
        cwd="/",
        capture_output=True,
        timeout=60,
        check=False,
        user=uid,
        group=uid,
        extra_groups=[] if uid is not None else None,
    )
    assert completed.returncode == 0, completed.stderr.decode()[-2000:]
    warnings: list[str] = json.loads(completed.stdout)
    return dict(item.split(":", 1) for item in warnings)


def test_child_cannot_reach_the_worker_process() -> None:
    found = _drive(FULL, "escape_proc", "")
    assert found == {"environ": EACCES, "mem_read": EACCES, "mem_write": EPERM, "fd": EACCES}


def test_child_cannot_reach_the_worker_process_as_root() -> None:
    # In-process (the test runner is the parent): with root's CAP_SYS_PTRACE and
    # DAC override, only Landlock stands between the child and the parent.
    found = _warnings("escape_proc")
    assert found["environ"] == EACCES
    assert found["mem_read"] == EACCES
    assert found["fd"] == EACCES
    assert found["mem_write"] == EPERM


def test_worker_is_non_dumpable_so_a_same_uid_child_without_landlock_finds_nothing() -> None:
    # Landlock removed: the worker's PR_SET_DUMPABLE 0 alone keeps its /proc
    # entries (environment with the master key, memory, open files) root-owned.
    found = _drive(NO_LANDLOCK, "escape_proc", "")
    assert found["environ"] == EACCES, "the worker's environment was readable"
    assert found["mem_read"] == EACCES
    assert found["fd"] == EACCES
    assert found["mem_write"] == EPERM


def test_protect_parent_marks_the_process_non_dumpable() -> None:
    code = (
        "import ctypes\n"
        "from app.ingestion.sandbox.runner import protect_parent\n"
        "protect_parent()\n"
        "print(ctypes.CDLL(None).prctl(3, 0, 0, 0, 0))\n"
    )
    completed = subprocess.run(  # nosec B603
        [sys.executable, "-I", "-c", code], capture_output=True, check=True, timeout=60
    )
    assert completed.stdout.strip() == b"0"


@pytest.mark.parametrize("module", [FULL, NO_LANDLOCK, NO_FS_SECCOMP])
def test_child_cannot_change_files_its_uid_owns(module: str, driver_dir: Path) -> None:
    root = driver_dir / "blobs"
    root.mkdir(mode=0o700)
    (root / "sub").mkdir()
    victim = root / "victim"
    victim.write_bytes(b"ciphertext")
    for path in (root, root / "sub", victim):
        _owned_by_driver(path)
    found = _drive(module, "fs_tamper", str(root))
    assert set(found) == {
        "unlink", "rename", "truncate", "chmod", "chown", "utime", "open_rw", "open_append",
        "open_trunc", "create", "mkdir", "rmdir", "symlink", "link",
    }  # fmt: skip
    changed = {name for name, outcome in found.items() if outcome == "ok"}
    # Landlock does not cover metadata (mode, owner, times): only the seccomp
    # rules stop those, so the Landlock-only variant may change them.
    metadata = {"chmod", "chown", "utime"} if module == NO_FS_SECCOMP else set()
    assert changed <= metadata
    assert victim.read_bytes() == b"ciphertext"
    assert sorted(p.name for p in root.iterdir()) == ["sub", "victim"]


@pytest.mark.parametrize("module", [FULL, NO_FS_SECCOMP])
def test_child_cannot_read_files_outside_the_interpreter(module: str, driver_dir: Path) -> None:
    # Stand-in for the master key file and the documents volume: owned by the
    # worker's (and so the child's) UID, mode 0600.
    secret = driver_dir / "master.key"
    secret.write_bytes(b"PROBE_SECRET")
    secret.chmod(0o600)
    _owned_by_driver(secret)
    found = _drive(module, "read_outside", str(secret))
    assert found == {"read": EACCES, "listdir": EACCES}


def test_landlock_is_required_unless_the_operator_opts_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(child, "landlock_abi", lambda: 0)
    with pytest.raises(child.HardenError, match="Landlock"):
        child.install_landlock(required=True)
    assert child.install_landlock(required=False) is False
    assert SandboxLimits().require_landlock is True


def test_readable_roots_never_include_the_filesystem_root_or_data_dirs() -> None:
    roots = child.readable_roots()
    assert "/" not in roots
    for forbidden in ("/var/lib/perfin", tempfile.gettempdir(), "/proc", "/etc", "/home"):
        assert forbidden not in roots


def test_huge_output_is_cut_off_and_killed() -> None:
    with pytest.raises(SandboxError) as caught:
        run_extraction("bigoutput", b"x", config=EVIL)
    assert caught.value.code == "output_too_large"


def test_output_must_match_the_schema() -> None:
    with pytest.raises(SandboxError) as caught:
        run_extraction("bad_shape", b"x", config=EVIL)
    assert caught.value.code == "bad_output"
    # The worker log says where the output failed, never what it held.
    assert "injected extra_forbidden" in caught.value.detail
    assert '"x"' not in caught.value.detail


def test_stray_stdout_is_reported_by_position_not_content() -> None:
    with pytest.raises(SandboxError) as caught:
        run_extraction("stray_stdout", b"x", config=EVIL)
    assert caught.value.code == "bad_output"
    detail = caught.value.detail
    assert "json_invalid" in detail
    assert "starts_with_brace=False" in detail
    assert "json_line=1" in detail
    assert "secret" not in detail
    assert "secret" not in str(caught.value)


def test_child_environment_is_empty_private_and_filtered() -> None:
    found = _warnings("whoami", data=b"abc")
    # Nothing inherited; CPython itself may set LC_CTYPE (PEP 538 locale coercion).
    assert set(filter(None, found["env"].split(","))) <= {"LC_CTYPE"}
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


def test_unprivileged_child_can_start_threads() -> None:
    """Threads work in an unprivileged child (CI failure 2026-11-04).

    ``RLIMIT_NPROC`` counts every task of the UID and binds only non-root
    processes, so the old limit of 0 passed as root but failed every PDF on an
    unprivileged worker. Run as root, the child drops to 65534 here, so the
    test binds on every host.
    """
    config = replace(EVIL, uid=65534, gid=65534)
    found = _warnings("threads", config=config)
    assert found == {"threads": "4"}
    assert _warnings("whoami", config=config)["uid"] != "0"


def test_user_database_lookups_do_not_kill_the_child() -> None:
    """A library looking up the user database must not kill the child (CI run 41915782513).

    onnxruntime (loaded by PyMuPDF's table detection) calls ``getpwuid_r`` at
    import. Under Landlock ``/etc/passwd`` is unreadable, so glibc tries the
    next NSS module of the host; Ubuntu's ``systemd`` module loads libcap,
    whose constructor calls ``prctl(PR_CAPBSET_READ)``, which the filter used
    to kill. Also run as an unprivileged UID when the tests run as root.
    """
    for config in (EVIL, replace(EVIL, uid=65534, gid=65534)):
        found = _warnings("user_lookup", config=config)
        assert found["pwd"] in {"found", "missing"}
        assert found["grp"] in {"found", "missing"}
        assert found["libcap"] in {"loaded", "absent"}


def test_prctl_only_reads_the_capability_bounding_set() -> None:
    assert child.PRCTL_ALLOWED_OPTIONS == (23,)  # PR_CAPBSET_READ
    assert _warnings("prctl_probe") == {"capbset_read": "ok"}
    # Any other option is outside the allowlist, e.g. becoming dumpable again,
    # which would expose the child's /proc entries to the rest of its UID.
    with pytest.raises(SandboxError) as caught:
        run_extraction("prctl_dumpable", b"x", config=EVIL)
    assert caught.value.code == "syscall_denied"


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
    for name in (
        "socket",
        "connect",
        "bind",
        "execve",
        "execveat",
        "fork",
        "vfork",
        "ptrace",
        "mount",
        "unshare",
        "setns",
        "bpf",
        "io_uring_setup",
        "keyctl",
        "process_vm_readv",
        "init_module",
        "kexec_load",
        "setuid",
        "chroot",
    ):
        assert name in DENIED_SYSCALLS
    for name in ("unlink", "unlinkat", "rename", "renameat2", "truncate", "ftruncate", "chmod"):
        assert name in child.FILESYSTEM_DENIED_SYSCALLS


def test_worker_marks_itself_non_dumpable_at_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from app import cli
    from app.ingestion.sandbox import runner

    calls: list[str] = []

    def refusing() -> None:
        calls.append("protect_parent")
        raise SandboxError("harden_failed", "could not make the worker non-dumpable")

    monkeypatch.setattr(runner, "protect_parent", refusing)
    # concurrency=0 is invalid: without the call the command would stop there.
    code = cli.cmd_worker(argparse.Namespace(queues=None, concurrency=0))
    assert calls == ["protect_parent"]
    assert code == cli.EXIT_FAIL
    assert "non-dumpable" in capsys.readouterr().err

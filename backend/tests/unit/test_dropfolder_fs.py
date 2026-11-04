# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drop-folder filesystem handling on real temp directories (no database).

SECURITY.md C3 (no symlinks followed, names never used as paths), C10 (scan
caps) and C11 (settle, open-for-writing, copy-then-check): listing,
opening by descriptor, refusing links and special files, after-action
renames, and the inotify/polling watchers.
"""

from __future__ import annotations

import errno
import os
import stat
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.ingestion.intake import dropfolder
from app.ingestion.intake.dropfolder import (
    Candidate,
    DropFolderConfig,
    InboxScan,
    InotifyWatcher,
    NotReadyError,
    PollingWatcher,
    RefusedError,
)


def _config(tmp_path: Path, **kwargs: object) -> DropFolderConfig:
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    inbox.mkdir(exist_ok=True)
    processed.mkdir(exist_ok=True)
    fields: dict[str, object] = {
        "inbox": inbox,
        "processed": processed,
        "failed": processed / "failed",
        "settle_seconds": 0.0,
        "watch_seconds": 0.0,
    }
    fields.update(kwargs)
    return DropFolderConfig(**fields)  # type: ignore[arg-type]


def _names(scan: InboxScan) -> list[str]:
    return sorted("/".join(c.parts) for c in scan.candidates)


def _only(scan: InboxScan, name: str) -> Candidate:
    (candidate,) = [c for c in scan.candidates if c.name == name]
    return candidate


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        ".hidden.csv",
        "~$statement.xlsx",
        "statement.csv.part",
        "statement.csv.crdownload",
        "statement.tmp",
        "statement.csv.TMP",
        "statement.pdf.partial",
        "statement.csv~",
        "statement.csv.ready",
        ".statement.csv.XXXXXX",
    ],
)
def test_partial_and_hidden_names_are_ignored(name: str) -> None:
    assert dropfolder.is_ignored_name(name)


@pytest.mark.parametrize("name", ["statement.csv", "2026-01 card.pdf", "receipt.JPG", "a"])
def test_ordinary_names_are_candidates(name: str) -> None:
    assert not dropfolder.is_ignored_name(name)


def test_destination_name_is_one_safe_component() -> None:
    entry_id = uuid.UUID("01900000-0000-7000-8000-0000deadbeef")
    when = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    for hostile in ("../../etc/passwd", "..", ".", "..\\..\\x.csv", "a/b", "\x00\n.csv", ""):
        name = dropfolder.destination_name(hostile, entry_id, when)
        assert "/" not in name
        assert "\\" not in name
        assert not name.startswith(".")
        assert name not in (".", "..")
        assert name.startswith("20260304T050607Z-0000deadbeef-")
        assert all(ch.isalnum() or ch in "._-" for ch in name)


def test_path_key_and_display_path() -> None:
    parts = ("alex", "bad\udcffname\n.csv")  # undecodable byte (surrogateescape) + newline
    key = dropfolder.path_key(parts)
    assert len(key) == 64
    assert key != dropfolder.path_key(("alex", "other.csv"))
    shown = dropfolder.display_path(parts)
    assert shown.isprintable()
    assert shown.startswith("alex/bad")
    assert len(dropfolder.display_path(("x" * 5000,))) == 1024


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_scan_lists_regular_files_and_never_follows_links(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inbox = config.inbox
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.csv").write_text("outside")
    (inbox / "flat.csv").write_text("a")
    (inbox / "alex" / "checking").mkdir(parents=True)
    (inbox / "alex" / "checking" / "deep.csv").write_text("b")
    (inbox / ".hidden.csv").write_text("c")
    (inbox / "upload.csv.part").write_text("d")
    (inbox / "link.csv").symlink_to(outside / "secret.csv")
    (inbox / "linkdir").symlink_to(outside, target_is_directory=True)
    os.mkfifo(inbox / "pipe.csv")
    with InboxScan(config).run() as scan:
        assert _names(scan) == ["alex/checking/deep.csv", "flat.csv"]
        assert scan.skipped["not_regular"] == 3  # file link, dir link, FIFO
        assert scan.skipped["ignored"] == 2
    # Every descriptor the scan opened is closed again.
    assert all(_closed(fd) for fd in scan.dir_fds)


def _closed(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError as exc:
        return exc.errno == errno.EBADF
    return False


def test_device_nodes_are_not_candidates(tmp_path: Path) -> None:
    config = _config(tmp_path)
    if os.geteuid() == 0:
        os.mknod(config.inbox / "null.csv", stat.S_IFCHR | 0o600, os.makedev(1, 3))
    else:  # an unprivileged run gets a socket instead: also not a regular file
        import socket

        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(config.inbox / "null.csv"))
        sock.close()
    with InboxScan(config).run() as scan:
        assert scan.candidates == []
        assert scan.skipped["not_regular"] == 1


def test_scan_skips_processed_inside_inbox_and_limits_depth(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    config = _config(tmp_path, processed=inbox / "processed", failed=inbox / "processed" / "f")
    config.processed.mkdir()
    (config.processed / "done.csv").write_text("x")
    deep = inbox.joinpath(*[f"d{i}" for i in range(dropfolder.MAX_DIR_DEPTH + 1)])
    deep.mkdir(parents=True)
    (deep / "too-deep.csv").write_text("x")
    (deep.parent / "deep-enough.csv").write_text("x")
    with InboxScan(config).run() as scan:
        assert [c.name for c in scan.candidates] == ["deep-enough.csv"]


def test_scan_entry_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dropfolder, "MAX_SCAN_ENTRIES", 3)
    config = _config(tmp_path)
    for i in range(5):
        (config.inbox / f"f{i}.csv").write_text(str(i))
    with InboxScan(config).run() as scan:
        assert len(scan.candidates) == 3
        assert scan.truncated


def test_missing_inbox_is_an_empty_scan(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.inbox.rmdir()
    with InboxScan(config).run() as scan:
        assert scan.candidates == []


# ---------------------------------------------------------------------------
# Opening by descriptor
# ---------------------------------------------------------------------------


def test_open_candidate_reads_the_listed_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (config.inbox / "a.csv").write_bytes(b"Date,Amount\n")
    with InboxScan(config).run() as scan:
        fd, st = dropfolder.open_candidate(_only(scan, "a.csv"), config)
        try:
            assert os.read(fd, 100) == b"Date,Amount\n"
            assert stat.S_ISREG(st.st_mode)
        finally:
            os.close(fd)


def test_file_swapped_for_symlink_after_listing_is_not_followed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    secret = tmp_path / "secret.csv"
    secret.write_text("outside the inbox")
    victim = config.inbox / "a.csv"
    victim.write_text("inside")
    with InboxScan(config).run() as scan:
        candidate = _only(scan, "a.csv")
        victim.unlink()
        victim.symlink_to(secret)
        with pytest.raises(RefusedError) as info:
            dropfolder.open_candidate(candidate, config)
        assert info.value.reason == "symlink"
        assert not info.value.record


def test_file_swapped_for_fifo_after_listing_does_not_block(tmp_path: Path) -> None:
    config = _config(tmp_path)
    victim = config.inbox / "a.csv"
    victim.write_text("inside")
    with InboxScan(config).run() as scan:
        candidate = _only(scan, "a.csv")
        victim.unlink()
        os.mkfifo(victim)
        started = time.monotonic()
        with pytest.raises(RefusedError) as info:
            dropfolder.open_candidate(candidate, config)
        assert info.value.reason == "not_regular"
        assert time.monotonic() - started < 5


def test_hard_link_to_an_outside_file_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path)
    secret = tmp_path / "secret.csv"
    secret.write_text("Date,Amount\n2026-01-01,1\n")
    os.link(secret, config.inbox / "a.csv")
    with InboxScan(config).run() as scan, pytest.raises(RefusedError) as info:
        dropfolder.open_candidate(_only(scan, "a.csv"), config)
    assert info.value.reason == "hard_link"
    assert info.value.record


def test_replaced_or_grown_file_is_not_ready(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = config.inbox / "a.csv"
    path.write_text("one")
    with InboxScan(config).run() as scan:
        candidate = _only(scan, "a.csv")
        with path.open("a") as handle:
            handle.write("two")
        with pytest.raises(NotReadyError) as info:
            dropfolder.open_candidate(candidate, config)
        assert info.value.reason == "changed"
        path.unlink()
        path.write_text("one")
        with pytest.raises(NotReadyError) as info:
            dropfolder.open_candidate(candidate, config)
        assert info.value.reason in ("replaced", "changed")
        path.unlink()
        with pytest.raises(NotReadyError) as info:
            dropfolder.open_candidate(candidate, config)
        assert info.value.reason == "gone"


def test_oversized_file_is_refused_before_reading(tmp_path: Path) -> None:
    config = _config(tmp_path, max_file_bytes=10)
    (config.inbox / "big.csv").write_bytes(b"x" * 11)
    with InboxScan(config).run() as scan, pytest.raises(RefusedError) as info:
        dropfolder.open_candidate(_only(scan, "big.csv"), config)
    assert info.value.reason == "too_large"


def test_file_open_for_writing_is_not_ready(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = config.inbox / "a.csv"
    with path.open("wb") as writer:
        writer.write(b"Date,Amount\n")
        writer.flush()
        fd = os.open(path, os.O_RDONLY)
        try:
            assert dropfolder.open_for_writing(fd) is True
        finally:
            os.close(fd)
        with InboxScan(config).run() as scan, pytest.raises(NotReadyError) as info:
            dropfolder.open_candidate(_only(scan, "a.csv"), config)
        assert info.value.reason == "open_for_writing"
    fd = os.open(path, os.O_RDONLY)
    try:
        assert dropfolder.open_for_writing(fd) is False
    finally:
        os.close(fd)


def test_ready_sidecar(tmp_path: Path) -> None:
    config = _config(tmp_path, ready_sidecar=True)
    (config.inbox / "a.csv").write_text("x")
    with InboxScan(config).run() as scan:
        assert not dropfolder.has_ready_sidecar(_only(scan, "a.csv"))
    (config.inbox / "a.csv.ready").symlink_to(tmp_path)  # a link is no sidecar
    with InboxScan(config).run() as scan:
        assert not dropfolder.has_ready_sidecar(_only(scan, "a.csv"))
    (config.inbox / "a.csv.ready").unlink()
    (config.inbox / "a.csv.ready").write_text("")
    with InboxScan(config).run() as scan:
        assert [c.name for c in scan.candidates] == ["a.csv"]  # the sidecar is no candidate
        assert dropfolder.has_ready_sidecar(_only(scan, "a.csv"))


# ---------------------------------------------------------------------------
# After-actions
# ---------------------------------------------------------------------------


class _Entry:
    def __init__(self, outcome: str = "accepted") -> None:
        self.id = uuid.UUID("01900000-0000-7000-8000-00000000abcd")
        self.outcome = outcome


WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _after(config: DropFolderConfig, name: str, outcome: str = "accepted") -> tuple[str, str]:
    with InboxScan(config).run() as scan:
        return dropfolder.apply_after(_only(scan, name), _Entry(outcome), config, WHEN)  # type: ignore[arg-type]


def test_move_renames_into_processed_under_a_safe_name(tmp_path: Path) -> None:
    config = _config(tmp_path, after="move")
    (config.inbox / "sub").mkdir()
    hostile = "a ..\\..\\evil name.csv"
    (config.inbox / "sub" / hostile).write_text("x")
    assert _after(config, hostile) == ("done", "moved")
    (moved,) = config.processed.iterdir()
    assert moved.name == "20260102T030405Z-00000000abcd-a_.._.._evil_name.csv"
    assert moved.read_text() == "x"
    assert not (config.inbox / "sub" / hostile).exists()


def test_refused_files_move_to_failed(tmp_path: Path) -> None:
    config = _config(tmp_path, after="move")
    (config.inbox / "a.csv").write_text("x")
    assert _after(config, "a.csv", outcome="rejected") == ("done", "moved_failed")
    assert [p.name for p in config.failed.iterdir()] == ["20260102T030405Z-00000000abcd-a.csv"]
    assert stat.S_IMODE(config.failed.stat().st_mode) == 0o700


def test_move_across_filesystems_copies_then_unlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, after="move")
    (config.inbox / "a.csv").write_bytes(b"payload" * 1000)

    def exdev(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(os, "rename", exdev)
    assert _after(config, "a.csv") == ("done", "moved")
    (moved,) = config.processed.iterdir()
    assert moved.read_bytes() == b"payload" * 1000
    assert stat.S_IMODE(moved.stat().st_mode) == 0o600
    assert not (config.inbox / "a.csv").exists()


def test_delete_and_keep(tmp_path: Path) -> None:
    config = _config(tmp_path, after="delete")
    (config.inbox / "a.csv").write_text("x")
    (config.inbox / "b.csv").write_text("y")
    assert _after(config, "b.csv", outcome="rejected") == ("kept", "kept")
    assert _after(config, "a.csv") == ("done", "deleted")
    assert sorted(p.name for p in config.inbox.iterdir()) == ["b.csv"]
    keep = _config(tmp_path, after="keep")
    assert _after(keep, "b.csv") == ("kept", "kept")
    assert (config.inbox / "b.csv").exists()


def test_after_action_leaves_a_changed_file(tmp_path: Path) -> None:
    config = _config(tmp_path, after="delete")
    path = config.inbox / "a.csv"
    path.write_text("x")
    with InboxScan(config).run() as scan:
        candidate = _only(scan, "a.csv")
        path.write_text("new content, not what was taken")
        result = dropfolder.apply_after(candidate, _Entry(), config, WHEN)  # type: ignore[arg-type]
    assert result == ("done", "changed")
    assert path.exists()


def test_read_only_inbox_keeps_originals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path, after="move")
    (config.inbox / "a.csv").write_text("x")

    def erofs(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EROFS, "read-only file system")

    monkeypatch.setattr(os, "rename", erofs)
    assert _after(config, "a.csv") == ("kept", "inbox_read_only")
    assert (config.inbox / "a.csv").exists()
    monkeypatch.setattr(dropfolder, "inbox_writable", lambda _config: False)
    assert dropfolder.effective_after(config) == "keep"


def test_sidecar_goes_with_the_file(tmp_path: Path) -> None:
    config = _config(tmp_path, after="delete", ready_sidecar=True)
    (config.inbox / "a.csv").write_text("x")
    (config.inbox / "a.csv.ready").write_text("")
    assert _after(config, "a.csv") == ("done", "deleted")
    assert list(config.inbox.iterdir()) == []


# ---------------------------------------------------------------------------
# Watchers
# ---------------------------------------------------------------------------


def test_inotify_wakes_on_activity_in_subfolders(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (config.inbox / "alex").mkdir()
    watcher = InotifyWatcher()
    try:
        with InboxScan(config).run() as scan:
            watcher.add_dirs(scan.dir_fds)
        assert watcher.watched == 2
        assert watcher.wait(0.05) is False
        timer = threading.Timer(0.2, (config.inbox / "alex" / "new.csv").write_text, ("x",))
        timer.start()
        started = time.monotonic()
        assert watcher.wait(10) is True
        assert time.monotonic() - started < 5
        timer.join()
    finally:
        watcher.close()


def test_watchers_stop_waiting_when_cancelled(tmp_path: Path) -> None:
    cancelled = threading.Event()
    cancelled.set()
    for watcher in (InotifyWatcher(cancelled), PollingWatcher(cancelled)):
        started = time.monotonic()
        assert watcher.wait(30) is False
        assert time.monotonic() - started < 2
        watcher.close()


def test_default_watcher_falls_back_to_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError(errno.ENOSYS, "no inotify")

    assert isinstance(dropfolder.default_watcher(), InotifyWatcher)
    monkeypatch.setattr(dropfolder.InotifyWatcher, "__init__", broken)
    assert isinstance(dropfolder.default_watcher(), PollingWatcher)

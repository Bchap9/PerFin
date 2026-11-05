# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drop-folder intake (INGESTION.md "Drop folder specifics", ARCHITECTURE.md).

The worker's ``watch_dropfolder`` job (``app.workers.handlers.dropfolder``)
calls :func:`watch` once a minute. A run

1. takes the drop-folder lock (``pg_try_advisory_xact_lock``; a second run
   while one is busy does nothing) and finishes the after-actions of files
   taken by earlier, committed runs (:func:`finish_pending`);
2. walks the inbox (:class:`InboxScan`) without following anything: every
   directory is opened relative to its parent with ``O_NOFOLLOW |
   O_DIRECTORY``, every entry is ``lstat``-ed, and only regular files are
   candidates; symlinks, FIFOs, sockets and devices are never opened.
   Hidden files and partial-download patterns (``.part``, ``~$``,
   ``.crdownload``, ``.tmp``, ...) are ignored, and so are ``processed/`` and
   ``failed/`` if they sit inside the inbox. At most :data:`MAX_DIR_DEPTH`
   levels and :data:`MAX_SCAN_ENTRIES` entries per pass are looked at (C10);
3. takes a file only once it is complete (SECURITY.md C11): unchanged
   (``mtime`` and ``ctime``) for ``PERFIN_DROPFOLDER_SETTLE_SECONDS``, not open
   for writing where the kernel can tell (a read lease, ``F_SETLEASE``, is
   refused while anyone has the file open for writing), and, with
   ``PERFIN_DROPFOLDER_READY_SIDECAR``, only when ``<name>.ready`` exists;
4. opens it by file descriptor (``O_NOFOLLOW | O_NONBLOCK``), checks that
   the descriptor is the regular file that was listed, with one link (a hard
   link may point at a file outside the inbox) and within the size cap, and
   streams it into quarantine through the same intake as an upload
   (``app.ingestion.intake.upload.store_stream``: sniff, SHA-256, size cap,
   encryption). If the file changed while it was copied (size, ``mtime``,
   bytes read), the copy is discarded and the file is tried again later;
5. records the document (``source=dropfolder``; ``source_meta`` holds the
   relative path, ``mtime``, size and folder slug as metadata, C3) for the
   member and account the folder maps to (``app.domain.dropfolder.service``);
   a file nobody can be found for waits behind an ``unknown_owner`` review
   item ("Who does this belong to?");
6. writes a ``dropfolder_entry`` per file taken or refused. The entry is the
   idempotency ledger (a file is never taken twice, across restarts too) and
   the to-do list for the after-action, which runs only after the run's
   transaction has committed: in a follow-up ``dropfolder.finish`` job, or at
   the start of the next run. ``move`` renames the original into
   ``processed/`` (``failed/`` for refused files) under a new name built from
   a timestamp, the entry id and a sanitised copy of its name; ``delete``
   unlinks it (refused files are kept); ``keep`` leaves it. With a read-only
   inbox, ``move`` and ``delete`` act like ``keep``.

Per run at most ``PERFIN_DROPFOLDER_MAX_FILES_PER_RUN`` files and
``PERFIN_DROPFOLDER_MAX_MB_PER_RUN`` are taken (C10); the rest waits for the
next run. Names from the inbox are never used to build a PerFin path (C3):
the after-action finds a file again by listing the inbox and matching its
(path hash, size, ``mtime``, inode), never by a stored path.

While files are still settling, a run keeps watching for up to
``PERFIN_DROPFOLDER_WATCH_SECONDS``: with inotify (a minimal ``ctypes``
binding) it wakes on activity, else it polls. It stops as soon as it has
taken something, so documents are committed promptly.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import fcntl
import hashlib
import os
import re
import select
import stat
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Literal, Protocol

from sqlalchemy import select as sql_select
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.crypto import Keyring
from app.core.logging import get_logger
from app.core.settings import Settings
from app.domain.documents.blobstore import BlobStore
from app.domain.documents.models import DocumentSource, ReviewKind
from app.domain.dropfolder.models import REL_PATH_MAX, DropfolderEntry
from app.domain.dropfolder.service import attribute
from app.ingestion.intake.upload import (
    READ_CHUNK,
    UploadRejectedError,
    UploadTooLargeError,
    record_stored,
    store_stream,
)

log = get_logger(__name__)

AfterMode = Literal["move", "delete", "keep"]

#: Directory levels below the inbox that are walked (``<member>/<account>/...``).
MAX_DIR_DEPTH: Final = 4
#: Directory entries looked at per pass (C10: a folder of a million files
#: cannot keep the worker busy).
MAX_SCAN_ENTRIES: Final = 10_000
#: Pending after-actions handled per run.
MAX_FINISH_PER_RUN: Final = 1_000
READY_SUFFIX: Final = ".ready"
IGNORED_PREFIXES: Final = (".", "~$")
IGNORED_SUFFIXES: Final = (
    ".part",
    ".partial",
    ".crdownload",
    ".download",
    ".tmp",
    ".temp",
    ".swp",
    "~",
    READY_SUFFIX,
)
#: ``pg_try_advisory_xact_lock`` key of the drop folder (one watcher at a time).
LOCK_KEY: Final = "perfin.dropfolder"
UNKNOWN_OWNER_QUESTION: Final = "Who does this belong to?"

_OPEN_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_UNSAFE_NAME: Final = re.compile(r"[^A-Za-z0-9._-]+")
_READ_ONLY_ERRNOS: Final = frozenset({errno.EROFS, errno.EACCES, errno.EPERM})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DropFolderConfig:
    inbox: Path
    processed: Path
    failed: Path
    settle_seconds: float = 10.0
    ready_sidecar: bool = False
    after: AfterMode = "move"
    max_files: int = 100
    max_bytes: int = 1024 * 1024 * 1024
    max_file_bytes: int = 50 * 1024 * 1024
    watch_seconds: float = 45.0

    @classmethod
    def from_settings(cls, settings: Settings) -> DropFolderConfig:
        processed = settings.dropfolder_processed
        return cls(
            inbox=settings.dropfolder_inbox,
            processed=processed,
            failed=settings.dropfolder_failed or processed / "failed",
            settle_seconds=float(settings.dropfolder_settle_seconds),
            ready_sidecar=settings.dropfolder_ready_sidecar,
            after=settings.dropfolder_after,
            max_files=settings.dropfolder_max_files_per_run,
            max_bytes=settings.dropfolder_max_mb_per_run * 1024 * 1024,
            max_file_bytes=settings.upload_max_bytes,
            watch_seconds=float(settings.dropfolder_watch_seconds),
        )


@dataclass(frozen=True)
class IntakeDeps:
    store: BlobStore
    keyring: Keyring


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def is_ignored_name(name: str) -> bool:
    """Hidden files, sidecars and partial-write patterns are never candidates."""
    lowered = name.casefold()
    return lowered.startswith(IGNORED_PREFIXES) or lowered.endswith(IGNORED_SUFFIXES)


def display_path(parts: tuple[str, ...]) -> str:
    """The relative path as printable metadata (never used as a path, C3)."""
    joined = "/".join(parts)
    return "".join(ch if ch.isprintable() else "�" for ch in joined)[:REL_PATH_MAX]


def path_key(parts: tuple[str, ...]) -> str:
    """SHA-256 of the exact relative path bytes (the ledger key; no name stored as key)."""
    raw = b"/".join(os.fsencode(p) for p in parts)
    return hashlib.sha256(raw).hexdigest()


def destination_name(name: str, entry_id: uuid.UUID, now: datetime) -> str:
    """Name of a moved original: ``<UTC stamp>-<entry id>-<sanitised name>``.

    One path component of ``[A-Za-z0-9._-]`` that cannot start with a dot, so
    it can never name another directory (no ``/``, no ``..``).
    """
    safe = _UNSAFE_NAME.sub("_", name).lstrip(".")[:120] or "file"
    return f"{now:%Y%m%dT%H%M%SZ}-{entry_id.hex[-12:]}-{safe}"


def _signed64(value: int) -> int:
    """``st_ino`` is unsigned 64-bit; ``BIGINT`` is signed."""
    value &= (1 << 64) - 1
    return value - (1 << 64) if value >= 1 << 63 else value


# ---------------------------------------------------------------------------
# Walking the inbox
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """A regular file found in the inbox; ``dir_fd`` stays open for the scan."""

    dir_fd: int
    name: str
    folders: tuple[str, ...]
    st: os.stat_result

    @property
    def parts(self) -> tuple[str, ...]:
        return (*self.folders, self.name)

    @property
    def key(self) -> tuple[str, int, int, int]:
        return (
            path_key(self.parts),
            self.st.st_size,
            self.st.st_mtime_ns,
            _signed64(self.st.st_ino),
        )


class InboxScan(contextlib.AbstractContextManager["InboxScan"]):
    """One listing of the inbox; directory descriptors close on exit."""

    def __init__(self, config: DropFolderConfig) -> None:
        self.config = config
        self.candidates: list[Candidate] = []
        self.dir_fds: list[int] = []
        self.skipped: Counter[str] = Counter()
        self.truncated = False
        self._seen = 0
        self._stack = contextlib.ExitStack()
        self._excluded: set[tuple[int, int]] = set()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stack.close()

    def _keep_open(self, fd: int) -> int:
        self._stack.callback(os.close, fd)
        self.dir_fds.append(fd)
        return fd

    def run(self) -> InboxScan:
        for path in (self.config.processed, self.config.failed):
            with contextlib.suppress(OSError):
                st = os.stat(path)
                self._excluded.add((st.st_dev, st.st_ino))
        try:
            # The configured inbox itself is trusted (it is the mount point).
            root = os.open(self.config.inbox, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as exc:
            log.warning("drop folder inbox unavailable", extra={"errno": exc.errno})
            return self
        self._walk(self._keep_open(root), ())
        return self

    def _walk(self, dir_fd: int, folders: tuple[str, ...]) -> None:
        try:
            with os.scandir(dir_fd) as entries:
                names = sorted(entry.name for entry in entries)
        except OSError:
            self.skipped["unlistable"] += 1
            return
        for name in names:
            if self._seen >= MAX_SCAN_ENTRIES:
                self.truncated = True
                return
            self._seen += 1
            if is_ignored_name(name):
                self.skipped["ignored"] += 1
                continue
            try:
                st = os.lstat(name, dir_fd=dir_fd)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                if len(folders) >= MAX_DIR_DEPTH or (st.st_dev, st.st_ino) in self._excluded:
                    self.skipped["directory"] += 1
                    continue
                try:
                    sub = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
                except OSError:
                    self.skipped["unlistable"] += 1
                    continue
                self._walk(self._keep_open(sub), (*folders, name))
            elif stat.S_ISREG(st.st_mode):
                self.candidates.append(Candidate(dir_fd, name, folders, st))
            else:
                # Symlinks, FIFOs, sockets, devices: never opened or touched.
                self.skipped["not_regular"] += 1


# ---------------------------------------------------------------------------
# Is the file complete?
# ---------------------------------------------------------------------------


def changed_at(st: os.stat_result) -> float:
    """Last change of content or metadata (``ctime`` also moves on ``utime``)."""
    return max(st.st_mtime, st.st_ctime)


def has_ready_sidecar(candidate: Candidate) -> bool:
    try:
        st = os.lstat(candidate.name + READY_SUFFIX, dir_fd=candidate.dir_fd)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def open_for_writing(fd: int) -> bool | None:
    """``True`` if someone has the file open for writing; ``None`` if unknown.

    A read lease is refused (``EAGAIN``) while the file is open for writing
    anywhere on this kernel. It needs the file's owner (or ``CAP_LEASE``) and
    a local filesystem; otherwise the settle time is the only signal.
    """
    try:
        fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_RDLCK)
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EBUSY):
            return True
        return None
    fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_UNLCK)
    return False


class NotReadyError(Exception):
    """Not yet: the file is still changing (tried again later, nothing recorded)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RefusedError(Exception):
    """The file is not taken. ``record``: write a ledger entry (else just skip it)."""

    def __init__(self, reason: str, *, record: bool = True) -> None:
        super().__init__(reason)
        self.reason = reason
        self.record = record


def open_candidate(candidate: Candidate, config: DropFolderConfig) -> tuple[int, os.stat_result]:
    """Open the listed file by descriptor and check it is still that regular file."""
    try:
        fd = os.open(candidate.name, _OPEN_FLAGS, dir_fd=candidate.dir_fd)
    except FileNotFoundError as exc:
        raise NotReadyError("gone") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RefusedError("symlink", record=False) from exc
        raise RefusedError("unreadable", record=False) from exc
    try:
        st = os.fstat(fd)
        listed = candidate.st
        if not stat.S_ISREG(st.st_mode):
            raise RefusedError("not_regular", record=False)
        if (st.st_dev, st.st_ino) != (listed.st_dev, listed.st_ino):
            raise NotReadyError("replaced")
        if st.st_size != listed.st_size or st.st_mtime_ns != listed.st_mtime_ns:
            raise NotReadyError("changed")
        if st.st_nlink != 1:
            raise RefusedError("hard_link")
        if st.st_size > config.max_file_bytes:
            raise RefusedError("too_large")
        if open_for_writing(fd):
            raise NotReadyError("open_for_writing")
    except BaseException:
        os.close(fd)
        raise
    return fd, st


@dataclass
class _ReadCount:
    total: int = 0


def _read_fd(fd: int, limit: int, count: _ReadCount) -> Iterator[bytes]:
    """Chunks of ``fd`` up to ``limit`` bytes (one past the listed size shows growth)."""
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, min(READ_CHUNK, remaining))
        if not chunk:
            return
        remaining -= len(chunk)
        count.total += len(chunk)
        yield chunk


# ---------------------------------------------------------------------------
# Taking files
# ---------------------------------------------------------------------------


@dataclass
class RunStats:
    taken: int = 0
    refused: int = 0
    known: int = 0
    duplicates: int = 0
    held: int = 0
    bytes: int = 0
    capped: bool = False
    locked: bool = False
    after_mode: AfterMode = "keep"
    pending_after: int = 0
    finished: Counter[str] = field(default_factory=Counter)
    not_ready: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    passes: int = 0

    @property
    def handled(self) -> int:
        return self.taken + self.refused + self.duplicates


def inbox_writable(config: DropFolderConfig) -> bool:
    """Whether originals can be moved or deleted (the inbox directory is writable)."""
    try:
        if os.statvfs(config.inbox).f_flag & os.ST_RDONLY:
            return False
    except OSError:
        return False
    return os.access(config.inbox, os.W_OK)


def effective_after(config: DropFolderConfig) -> AfterMode:
    if config.after != "keep" and not inbox_writable(config):
        log.warning(
            "drop folder inbox is read-only; originals are kept",
            extra={"configured": config.after},
        )
        return "keep"
    return config.after


def _entry(
    db: Session,
    household_id: uuid.UUID,
    candidate: Candidate,
    *,
    outcome: str,
    after_state: str,
    reason: str | None = None,
    sha256: str | None = None,
    document_id: uuid.UUID | None = None,
) -> DropfolderEntry:
    key, size, mtime_ns, inode = candidate.key
    entry = DropfolderEntry(
        household_id=household_id,
        path_key=key,
        rel_path=display_path(candidate.parts),
        size=size,
        mtime_ns=mtime_ns,
        inode=inode,
        sha256=sha256,
        document_id=document_id,
        outcome=outcome,
        reason=reason,
        after_state=after_state,
    )
    db.add(entry)
    db.flush()
    return entry


def _known(db: Session, household_id: uuid.UUID, candidate: Candidate) -> bool:
    key, size, mtime_ns, inode = candidate.key
    return (
        db.execute(
            sql_select(DropfolderEntry.id).where(
                DropfolderEntry.household_id == household_id,
                DropfolderEntry.path_key == key,
                DropfolderEntry.size == size,
                DropfolderEntry.mtime_ns == mtime_ns,
                DropfolderEntry.inode == inode,
            )
        ).first()
        is not None
    )


def _hash_taken(db: Session, household_id: uuid.UUID, sha256: str) -> bool:
    return (
        db.execute(
            sql_select(DropfolderEntry.id).where(
                DropfolderEntry.household_id == household_id,
                DropfolderEntry.sha256 == sha256,
                DropfolderEntry.outcome == "accepted",
            )
        ).first()
        is not None
    )


def _refuse(
    db: Session,
    household_id: uuid.UUID,
    candidate: Candidate,
    reason: str,
    after: AfterMode,
    stats: RunStats,
) -> None:
    after_state = "pending" if after == "move" else "kept"
    entry = _entry(
        db, household_id, candidate, outcome="rejected", after_state=after_state, reason=reason
    )
    audit(
        db,
        action="dropfolder.refuse",
        target_type="dropfolder_entry",
        target_id=entry.id,
        after={"reason": reason, "size": candidate.st.st_size},
        household_id=household_id,
    )
    stats.refused += 1
    if after_state == "pending":
        stats.pending_after += 1
    log.info("drop folder file refused", extra={"reason": reason, "entry_id": str(entry.id)})


def _source_meta(candidate: Candidate, folder: str, how: str) -> dict[str, object]:
    mtime = datetime.fromtimestamp(candidate.st.st_mtime, tz=UTC)
    return {
        "path": display_path(candidate.parts),
        "folder": folder,
        "mtime": mtime.isoformat(),
        "size": candidate.st.st_size,
        "attributed_by": how,
    }


def take(
    db: Session,
    household_id: uuid.UUID,
    candidate: Candidate,
    config: DropFolderConfig,
    deps: IntakeDeps,
    after: AfterMode,
    stats: RunStats,
) -> None:
    """Copy one settled file into quarantine and record it (module doc, steps 4-6).

    Raises :class:`NotReadyError` when the file must be tried again later.
    """
    try:
        fd, st = open_candidate(candidate, config)
    except RefusedError as refused:
        if refused.record:
            _refuse(db, household_id, candidate, refused.reason, after, stats)
        else:
            stats.skipped[refused.reason] += 1
        return
    try:
        count = _ReadCount()
        try:
            stored = store_stream(
                store=deps.store,
                keyring=deps.keyring,
                chunks=_read_fd(fd, st.st_size + 1, count),
                max_bytes=config.max_file_bytes,
            )
        except UploadRejectedError as rejected:
            _refuse(db, household_id, candidate, str(rejected.reason), after, stats)
            return
        except UploadTooLargeError as exc:
            raise NotReadyError("grew") from exc
        stats.bytes += count.total
        now_st = os.fstat(fd)
    finally:
        os.close(fd)
    sealed = stored.sealed
    if (
        count.total != st.st_size
        or now_st.st_size != st.st_size
        or now_st.st_mtime_ns != st.st_mtime_ns
    ):
        deps.store.delete(sealed.area, sealed.key)
        raise NotReadyError("changed_while_copying")
    if after == "keep" and _hash_taken(db, household_id, stored.sha256):
        # Read-only/keep inbox: the same content under another name was taken.
        deps.store.delete(sealed.area, sealed.key)
        _entry(
            db,
            household_id,
            candidate,
            outcome="skipped",
            after_state="kept",
            reason="already_taken",
            sha256=stored.sha256,
        )
        stats.duplicates += 1
        return
    who = attribute(db, household_id, candidate.folders)
    hold = None
    if who.user_id is None:
        hold = (
            ReviewKind.UNKNOWN_OWNER,
            {
                "reason": "unknown_owner",
                "question": UNKNOWN_OWNER_QUESTION,
                "folder": who.folder,
                "path": display_path(candidate.parts),
            },
        )
    received = record_stored(
        db,
        stored,
        store=deps.store,
        household_id=household_id,
        source=DocumentSource.DROPFOLDER,
        source_meta=_source_meta(candidate, who.folder, who.how),
        original_filename=candidate.name,
        uploaded_by=who.user_id,
        account_id=who.account_id,
        hold=hold,
    )
    after_state = "pending" if after in ("move", "delete") else "kept"
    _entry(
        db,
        household_id,
        candidate,
        outcome="accepted",
        after_state=after_state,
        sha256=stored.sha256,
        document_id=received.document_id,
    )
    stats.taken += 1
    if hold is not None:
        stats.held += 1
    if after_state == "pending":
        stats.pending_after += 1
    log.info(
        "drop folder file taken",
        extra={"document_id": str(received.document_id), "attributed_by": who.how},
    )


@dataclass(frozen=True)
class PassResult:
    waiting: int
    next_due: float | None


def scan_pass(
    db: Session,
    household_id: uuid.UUID,
    config: DropFolderConfig,
    deps: IntakeDeps,
    after: AfterMode,
    stats: RunStats,
    *,
    now: Callable[[], float] = time.time,
    watcher: Watcher | None = None,
) -> PassResult:
    """List the inbox once and take every settled file within the run's caps."""
    stats.passes += 1
    waiting = 0
    next_due: float | None = None
    with InboxScan(config).run() as scan:
        stats.skipped.update(scan.skipped)
        if watcher is not None:
            watcher.add_dirs(scan.dir_fds)
        for candidate in scan.candidates:
            if _known(db, household_id, candidate):
                stats.known += 1
                continue
            due = changed_at(candidate.st) + config.settle_seconds
            if now() < due:
                stats.not_ready["settling"] += 1
                waiting += 1
                next_due = due if next_due is None else min(next_due, due)
                continue
            if config.ready_sidecar and not has_ready_sidecar(candidate):
                stats.not_ready["no_ready_sidecar"] += 1
                continue
            if stats.handled >= config.max_files or (
                stats.bytes and stats.bytes + candidate.st.st_size > config.max_bytes
            ):
                stats.capped = True
                break
            try:
                with db.begin_nested():
                    take(db, household_id, candidate, config, deps, after, stats)
            except NotReadyError as not_ready:
                stats.not_ready[not_ready.reason] += 1
                waiting += 1
                retry = now() + max(config.settle_seconds, 1.0)
                next_due = retry if next_due is None else min(next_due, retry)
            except IntegrityError:
                # Another run recorded the same file meanwhile.
                stats.known += 1
    return PassResult(waiting, next_due)


# ---------------------------------------------------------------------------
# After-actions (move / delete / keep)
# ---------------------------------------------------------------------------


def _open_dest(config: DropFolderConfig, *, failed: bool) -> int:
    processed = os.open(config.processed, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    if not failed:
        return processed
    try:
        if config.failed.parent == config.processed:
            name = config.failed.name
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=processed)
            return os.open(name, _DIR_FLAGS, dir_fd=processed)
        return os.open(config.failed, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    finally:
        os.close(processed)


def _copy_across(src_dir: int, name: str, dest_dir: int, dest_name: str, ino: int) -> None:
    """``rename`` across filesystems: copy through descriptors, fsync, then unlink."""
    src = os.open(name, _OPEN_FLAGS, dir_fd=src_dir)
    try:
        if os.fstat(src).st_ino != ino:
            raise FileNotFoundError(errno.ENOENT, "replaced")
        dst = os.open(
            dest_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=dest_dir,
        )
        try:
            while chunk := os.read(src, READ_CHUNK):
                os.write(dst, chunk)
            os.fsync(dst)
        except BaseException:
            os.close(dst)
            os.unlink(dest_name, dir_fd=dest_dir)
            raise
        os.close(dst)
    finally:
        os.close(src)
    try:
        os.unlink(name, dir_fd=src_dir)
    except OSError:
        os.unlink(dest_name, dir_fd=dest_dir)
        raise


def _drop_sidecar(candidate: Candidate) -> None:
    with contextlib.suppress(OSError):
        st = os.lstat(candidate.name + READY_SUFFIX, dir_fd=candidate.dir_fd)
        if stat.S_ISREG(st.st_mode):
            os.unlink(candidate.name + READY_SUFFIX, dir_fd=candidate.dir_fd)


def apply_after(
    candidate: Candidate, entry: DropfolderEntry, config: DropFolderConfig, now: datetime
) -> tuple[str, str]:
    """Move or delete one original; returns ``(after_state, after_result)``."""
    try:
        st = os.lstat(candidate.name, dir_fd=candidate.dir_fd)
    except FileNotFoundError:
        return "done", "gone"
    if (st.st_ino, st.st_size, st.st_mtime_ns) != (
        candidate.st.st_ino,
        candidate.st.st_size,
        candidate.st.st_mtime_ns,
    ) or not stat.S_ISREG(st.st_mode):
        return "done", "changed"
    try:
        if config.after == "delete":
            if entry.outcome != "accepted":
                return "kept", "kept"
            os.unlink(candidate.name, dir_fd=candidate.dir_fd)
            result = "deleted"
        elif config.after == "move":
            failed = entry.outcome == "rejected"
            dest_dir = _open_dest(config, failed=failed)
            try:
                dest_name = destination_name(candidate.name, entry.id, now)
                try:
                    os.rename(
                        candidate.name, dest_name, src_dir_fd=candidate.dir_fd, dst_dir_fd=dest_dir
                    )
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    _copy_across(candidate.dir_fd, candidate.name, dest_dir, dest_name, st.st_ino)
            finally:
                os.close(dest_dir)
            result = "moved_failed" if failed else "moved"
        else:
            return "kept", "kept"
    except FileNotFoundError:
        return "done", "gone"
    except OSError as exc:
        if exc.errno in _READ_ONLY_ERRNOS:
            return "kept", "inbox_read_only"
        log.warning(
            "drop folder after-action failed",
            extra={"entry_id": str(entry.id), "errno": exc.errno},
        )
        return "error", "error"
    if config.ready_sidecar:
        _drop_sidecar(candidate)
    return "done", result


def finish_pending(
    db: Session,
    household_id: uuid.UUID,
    config: DropFolderConfig,
    *,
    now: datetime | None = None,
) -> Counter[str]:
    """Do the after-actions of committed entries (``after_state = 'pending'``).

    Originals are found again by listing the inbox and matching each file's
    ledger key, never by a stored path (C3). An entry whose file is gone is
    done; while the listing was truncated, unmatched entries stay pending.
    """
    results: Counter[str] = Counter()
    entries = list(
        db.execute(
            sql_select(DropfolderEntry)
            .where(
                DropfolderEntry.household_id == household_id,
                DropfolderEntry.after_state == "pending",
            )
            .order_by(DropfolderEntry.created_at, DropfolderEntry.id)
            .limit(MAX_FINISH_PER_RUN)
        ).scalars()
    )
    if not entries:
        return results
    when = now if now is not None else datetime.now(UTC)
    if config.after == "keep" or not inbox_writable(config):
        for entry in entries:
            entry.after_state, entry.after_result = "kept", "inbox_read_only"
            results["inbox_read_only"] += 1
        db.flush()
        return results
    by_key = {(e.path_key, e.size, e.mtime_ns, e.inode): e for e in entries}
    with InboxScan(config).run() as scan:
        matched: set[uuid.UUID] = set()
        for candidate in scan.candidates:
            found = by_key.get(candidate.key)
            if found is None:
                continue
            matched.add(found.id)
            found.after_state, result = apply_after(candidate, found, config, when)
            found.after_result = result
            results[result] += 1
        for entry in entries:
            if entry.id not in matched and not scan.truncated:
                entry.after_state, entry.after_result = "done", "gone"
                results["gone"] += 1
    db.flush()
    return results


# ---------------------------------------------------------------------------
# Watching
# ---------------------------------------------------------------------------


class Watcher(Protocol):
    def add_dirs(self, dir_fds: list[int]) -> None: ...

    def wait(self, timeout: float) -> bool: ...

    def close(self) -> None: ...


class PollingWatcher:
    """Fallback: sleep (interruptibly) and let the next pass look again."""

    def __init__(self, cancelled: threading.Event | None = None) -> None:
        self._cancelled = cancelled if cancelled is not None else threading.Event()

    def add_dirs(self, dir_fds: list[int]) -> None:
        return None

    def wait(self, timeout: float) -> bool:
        self._cancelled.wait(max(0.0, timeout))
        return False

    def close(self) -> None:
        return None


IN_MODIFY: Final = 0x00000002
IN_ATTRIB: Final = 0x00000004
IN_CLOSE_WRITE: Final = 0x00000008
IN_MOVED_FROM: Final = 0x00000040
IN_MOVED_TO: Final = 0x00000080
IN_CREATE: Final = 0x00000100
IN_DELETE: Final = 0x00000200
IN_ONLYDIR: Final = 0x01000000
WATCH_MASK: Final = (
    IN_MODIFY
    | IN_ATTRIB
    | IN_CLOSE_WRITE
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CREATE
    | IN_DELETE
    | IN_ONLYDIR
)


class InotifyWatcher:
    """A minimal ``ctypes`` inotify binding (Linux); events only wake the watcher.

    Directories are watched through ``/proc/self/fd/<fd>`` of the descriptors
    the scan opened (``IN_ONLYDIR``), so no path is built from inbox names:
    that magic link names the very directory the scan opened with
    ``O_NOFOLLOW``, not whatever a name points at now (which is why
    ``IN_DONT_FOLLOW``, which would refuse the link itself, is not set).
    Event contents are drained and ignored: the next pass
    lists the inbox again.
    """

    def __init__(self, cancelled: threading.Event | None = None) -> None:
        libc_name = ctypes.util.find_library("c")
        if libc_name is None:
            raise OSError(errno.ENOSYS, "libc not found")
        libc = ctypes.CDLL(libc_name, use_errno=True)
        self._add_watch = libc.inotify_add_watch
        self._add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._add_watch.restype = ctypes.c_int
        init = libc.inotify_init1
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        self.fd = fd
        self._watched: set[tuple[int, int]] = set()
        self._cancelled = cancelled if cancelled is not None else threading.Event()

    def add_dirs(self, dir_fds: list[int]) -> None:
        for dir_fd in dir_fds:
            try:
                st = os.fstat(dir_fd)
            except OSError:
                continue
            ident = (st.st_dev, st.st_ino)
            if ident in self._watched:
                continue
            wd = self._add_watch(self.fd, f"/proc/self/fd/{dir_fd}".encode(), WATCH_MASK)
            if wd >= 0:
                self._watched.add(ident)

    @property
    def watched(self) -> int:
        return len(self._watched)

    def wait(self, timeout: float) -> bool:
        """Block up to ``timeout`` seconds; ``True`` if anything happened."""
        deadline = time.monotonic() + max(0.0, timeout)
        poller = select.poll()
        poller.register(self.fd, select.POLLIN)
        while not self._cancelled.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if poller.poll(int(min(remaining, 1.0) * 1000)):
                self._drain()
                return True
        return False

    def _drain(self) -> None:
        with contextlib.suppress(BlockingIOError):
            while os.read(self.fd, 64 * 1024):
                pass

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.fd)


def default_watcher(cancelled: threading.Event | None = None) -> Watcher:
    """inotify where available, polling otherwise."""
    try:
        return InotifyWatcher(cancelled)
    except (OSError, AttributeError):
        return PollingWatcher(cancelled)


def try_lock(db: Session) -> bool:
    """The drop-folder lock for this transaction (one run or finish at a time)."""
    return bool(
        db.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"), {"key": LOCK_KEY}
        ).scalar()
    )


def watch(
    db: Session,
    household_id: uuid.UUID,
    config: DropFolderConfig,
    deps: IntakeDeps,
    *,
    cancelled: threading.Event | None = None,
    watcher_factory: Callable[[threading.Event | None], Watcher] = default_watcher,
    now: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
) -> RunStats:
    """One watcher run (module doc). The caller commits; nothing is moved before that."""
    stats = RunStats()
    if not try_lock(db):
        stats.locked = True
        return stats
    stats.finished = finish_pending(db, household_id, config)
    stats.after_mode = effective_after(config)
    deadline = monotonic() + config.watch_seconds
    watcher = watcher_factory(cancelled)
    try:
        while cancelled is None or not cancelled.is_set():
            result = scan_pass(
                db, household_id, config, deps, stats.after_mode, stats, now=now, watcher=watcher
            )
            if stats.handled or stats.capped or not result.waiting:
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            due = result.next_due if result.next_due is not None else now() + 1.0
            # Wake at the next settle deadline, or earlier on activity.
            if watcher.wait(min(remaining, max(0.2, due - now()))):
                time.sleep(min(0.5, max(0.0, deadline - monotonic())))
    finally:
        watcher.close()
    log.info(
        "drop folder run",
        extra={
            "taken": stats.taken,
            "refused": stats.refused,
            "held": stats.held,
            "known": stats.known,
            "duplicates": stats.duplicates,
            "bytes": stats.bytes,
            "capped": stats.capped,
            "passes": stats.passes,
            "not_ready": dict(stats.not_ready),
            "skipped": dict(stats.skipped),
            "finished": dict(stats.finished),
        },
    )
    return stats

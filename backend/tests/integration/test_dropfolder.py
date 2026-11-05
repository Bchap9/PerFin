# SPDX-License-Identifier: AGPL-3.0-or-later
"""The drop folder end to end on real temp directories and a real database.

INGESTION.md "Drop folder specifics"; SECURITY.md C3 (names are metadata,
no links followed, never processed in place), C10 (per-run caps), C11
(settle time, open-for-writing, ``.ready`` sidecar, copy-then-check).
Covers the race of a file still being written, links and special files,
traversal-looking names, caps, idempotency across restarts, every
after-mode, subfolder attribution and the "Who does this belong to?"
review item with its remember-the-subfolder resolution.
"""

from __future__ import annotations

import hashlib
import os
import stat
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.models import GrantLevel
from app.auth.principal import Principal
from app.core.settings import Settings
from app.db.models import AuditLog, Job
from app.db.rls import set_context
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Blob,
    BlobArea,
    Document,
    DocumentSource,
    DocumentStatus,
    ReviewItem,
    ReviewKind,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.dropfolder.models import DropfolderEntry, DropfolderRoute
from app.domain.review import service as review_service
from app.ingestion.intake import dropfolder
from app.ingestion.intake.dropfolder import DropFolderConfig, IntakeDeps, PollingWatcher, RunStats
from app.workers.handlers import dropfolder as handler
from app.workers.registry import REGISTRY, JobContext
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.households import API, Actors, problem_type
from tests.fixtures.ledger import make_account
from tests.fixtures.statements import csv_bytes, synth_transactions

pytestmark = pytest.mark.integration

REVIEW = f"{API}/review"
LATER = 3600.0  # "now" one hour ahead: every file has settled


def _csv(seed: int) -> bytes:
    return csv_bytes(synth_transactions(4, seed=seed))


@pytest.fixture
def doc_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={"documents_dir": tmp_path / "documents", "quarantine_dir": tmp_path / "quarantine"}
    )


@pytest.fixture
def deps(doc_settings: Settings, factories: Factories) -> IntakeDeps:
    return IntakeDeps(store=LocalFSBlobStore.from_settings(doc_settings), keyring=factories.keyring)


@pytest.fixture
def config(tmp_path: Path) -> DropFolderConfig:
    inbox, processed = tmp_path / "inbox", tmp_path / "processed"
    inbox.mkdir()
    processed.mkdir()
    return DropFolderConfig(
        inbox=inbox,
        processed=processed,
        failed=processed / "failed",
        settle_seconds=10.0,
        after="move",
        watch_seconds=0.0,
    )


@pytest.fixture
def owner(factories: Factories) -> UserRecord:
    return factories.user(role="owner", username=f"own{uuid.uuid4().hex[:10]}")


def _hh(record: UserRecord) -> uuid.UUID:
    assert record.household_id is not None
    return record.household_id


def _run(
    factories: Factories,
    household_id: uuid.UUID,
    config: DropFolderConfig,
    deps: IntakeDeps,
    *,
    ahead: float = LATER,
) -> RunStats:
    with factories.transaction() as db:
        set_context(db, household_id)
        return dropfolder.watch(
            db,
            household_id,
            config,
            deps,
            watcher_factory=PollingWatcher,
            now=lambda: time.time() + ahead,
        )


def _finish(factories: Factories, household_id: uuid.UUID, config: DropFolderConfig) -> Any:
    with factories.transaction() as db:
        set_context(db, household_id)
        return dropfolder.finish_pending(db, household_id, config)


def _rows(factories: Factories, household_id: uuid.UUID, model: Any) -> list[Any]:
    with factories.transaction() as db:
        set_context(db, household_id)
        rows = list(
            db.execute(
                select(model).where(model.household_id == household_id).order_by(model.created_at)
            ).scalars()
        )
        for row in rows:
            db.expunge(row)
        return rows


def _documents(factories: Factories, household_id: uuid.UUID) -> list[Document]:
    return _rows(factories, household_id, Document)


def _entries(factories: Factories, household_id: uuid.UUID) -> list[DropfolderEntry]:
    return _rows(factories, household_id, DropfolderEntry)


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _write_old(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# Taking a file
# ---------------------------------------------------------------------------


def test_member_folder_file_is_copied_encrypted_and_moved_after_commit(
    factories: Factories,
    owner: UserRecord,
    config: DropFolderConfig,
    deps: IntakeDeps,
    doc_settings: Settings,
) -> None:
    hh = _hh(owner)
    member = factories.user(
        role="member", household=owner.household, username=f"alex{uuid.uuid4().hex[:8]}"
    )
    with factories.transaction() as db:
        account = make_account(db, hh, name="Joint Checking")
    factories.grant(hh, member.id, account.id, GrantLevel.EDIT)
    data = _csv(1)
    original = _write_old(
        config.inbox / member.user.username.upper() / "joint_checking" / "jan.csv", data
    )
    mtime = original.stat().st_mtime

    stats = _run(factories, hh, config, deps)
    assert (stats.taken, stats.pending_after, stats.held) == (1, 1, 0)
    (doc,) = _documents(factories, hh)
    assert doc.source is DocumentSource.DROPFOLDER
    assert doc.status is DocumentStatus.RECEIVED
    assert doc.uploaded_by == member.id
    assert doc.account_id == account.id
    assert doc.original_filename == "jan.csv"
    assert doc.sha256 == hashlib.sha256(data).hexdigest()
    assert doc.source_meta["path"] == f"{member.user.username.upper()}/joint_checking/jan.csv"
    assert doc.source_meta["folder"] == f"{member.user.username}/joint-checking"
    assert doc.source_meta["attributed_by"] == "name"
    assert doc.source_meta["size"] == len(data)
    assert doc.source_meta["mtime"].startswith(time.strftime("%Y-%m-%d", time.gmtime(mtime)))
    with factories.transaction() as db:
        set_context(db, hh)
        blob = db.get(Blob, doc.blob_id)
        assert blob is not None
        assert blob.area is BlobArea.QUARANTINE
        jobs = db.execute(select(Job).where(Job.type == INGEST_JOB_TYPE)).scalars().all()
        assert [j.payload for j in jobs if j.household_id == hh] == [{"document_id": str(doc.id)}]
    (stored,) = _files(doc_settings.quarantine_dir)
    assert stored.name == blob.key
    assert data[:40] not in stored.read_bytes()  # ciphertext only
    # Nothing is moved before the transaction that recorded it committed.
    assert original.exists()
    (entry,) = _entries(factories, hh)
    assert (entry.outcome, entry.after_state, entry.document_id) == ("accepted", "pending", doc.id)

    assert _finish(factories, hh, config) == {"moved": 1}
    assert not original.exists()
    (moved,) = _files(config.processed)
    assert moved.parent == config.processed
    assert moved.name.endswith("-jan.csv")
    assert moved.read_bytes() == data
    (entry,) = _entries(factories, hh)
    assert (entry.after_state, entry.after_result) == ("done", "moved")
    # A later run neither takes it again nor finds anything to do.
    again = _run(factories, hh, config, deps)
    assert (again.taken, again.known, sum(again.finished.values())) == (0, 0, 0)


def test_file_still_being_written_is_not_taken(
    factories: Factories, owner: UserRecord, config: DropFolderConfig, deps: IntakeDeps
) -> None:
    hh = _hh(owner)
    path = config.inbox / "growing.csv"
    with path.open("wb") as writer:
        writer.write(_csv(2)[:20])
        writer.flush()
        # Just written: inside the settle time.
        stats = _run(factories, hh, config, deps, ahead=0.0)
        assert stats.taken == 0
        assert stats.not_ready["settling"] == 1
        # Settled by the clock but still open for writing.
        stats = _run(factories, hh, config, deps)
        assert stats.taken == 0
        assert stats.not_ready["open_for_writing"] == 1
        writer.write(_csv(2)[20:])
    assert _entries(factories, hh) == []
    assert _documents(factories, hh) == []
    stats = _run(factories, hh, config, deps)
    assert stats.taken == 1
    (doc,) = _documents(factories, hh)
    assert doc.size == len(_csv(2))


def test_watch_waits_for_a_settling_file_within_its_window(
    factories: Factories, owner: UserRecord, config: DropFolderConfig, deps: IntakeDeps
) -> None:
    hh = _hh(owner)
    window = DropFolderConfig(**{**config.__dict__, "settle_seconds": 1.0, "watch_seconds": 8.0})
    _write_old(config.inbox / "soon.csv", _csv(3))
    started = time.monotonic()
    with factories.transaction() as db:
        set_context(db, hh)
        stats = dropfolder.watch(db, hh, window, deps)  # real clock, inotify
    assert stats.taken == 1
    assert stats.passes >= 2
    assert time.monotonic() - started < 8


def test_file_changed_while_copying_is_discarded(
    factories: Factories,
    owner: UserRecord,
    config: DropFolderConfig,
    deps: IntakeDeps,
    doc_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hh = _hh(owner)
    path = _write_old(config.inbox / "a.csv", _csv(4))
    real = dropfolder._read_fd

    def appending(fd: int, limit: int, count: Any) -> Iterator[bytes]:
        for chunk in real(fd, limit, count):
            yield chunk
            with path.open("ab") as more:
                more.write(b"2026-02-01,1.00,Late,row\r\n")

    monkeypatch.setattr(dropfolder, "_read_fd", appending)
    stats = _run(factories, hh, config, deps)
    assert stats.taken == 0
    assert stats.not_ready["changed_while_copying"] == 1
    assert _documents(factories, hh) == []
    assert _entries(factories, hh) == []
    assert _files(doc_settings.quarantine_dir) == []  # the partial copy is gone
    monkeypatch.setattr(dropfolder, "_read_fd", real)
    assert _run(factories, hh, config, deps).taken == 1


# ---------------------------------------------------------------------------
# Links, special files, names
# ---------------------------------------------------------------------------


def test_links_to_outside_files_are_never_read(
    factories: Factories,
    owner: UserRecord,
    config: DropFolderConfig,
    deps: IntakeDeps,
    doc_settings: Settings,
    tmp_path: Path,
) -> None:
    hh = _hh(owner)
    secret = tmp_path / "outside" / "secret.csv"
    secret.parent.mkdir()
    secret.write_bytes(_csv(5))
    (config.inbox / "sym.csv").symlink_to(secret)
    (config.inbox / "symdir").symlink_to(secret.parent, target_is_directory=True)
    os.link(secret, config.inbox / "hard.csv")
    stats = _run(factories, hh, config, deps)
    assert (stats.taken, stats.refused) == (0, 1)
    assert stats.skipped["not_regular"] == 2
    assert _documents(factories, hh) == []
    assert _files(doc_settings.quarantine_dir) == []
    (entry,) = _entries(factories, hh)
    assert (entry.outcome, entry.reason, entry.rel_path) == ("rejected", "hard_link", "hard.csv")
    with factories.transaction() as db:
        set_context(db, hh)
        refusal = db.execute(
            select(AuditLog).where(
                AuditLog.household_id == hh, AuditLog.action == "dropfolder.refuse"
            )
        ).scalar_one()
        assert refusal.after == {"reason": "hard_link", "size": secret.stat().st_size}
    assert _finish(factories, hh, config) == {"moved_failed": 1}
    # The inbox name went to failed/; the outside file and the symlinks are untouched.
    (failed,) = _files(config.failed)
    assert failed.name.endswith("-hard.csv")
    assert secret.read_bytes() == _csv(5)
    assert (config.inbox / "sym.csv").is_symlink()
    assert (config.inbox / "symdir").is_symlink()


def test_fifos_and_devices_do_not_block_and_are_never_taken(
    factories: Factories, owner: UserRecord, config: DropFolderConfig, deps: IntakeDeps
) -> None:
    hh = _hh(owner)
    os.mkfifo(config.inbox / "pipe.csv")
    if os.geteuid() == 0:
        os.mknod(config.inbox / "zero.csv", stat.S_IFCHR | 0o600, os.makedev(1, 5))
    started = time.monotonic()
    stats = _run(factories, hh, config, deps)
    assert time.monotonic() - started < 10
    assert (stats.taken, stats.refused) == (0, 0)
    assert stats.skipped["not_regular"] == (2 if os.geteuid() == 0 else 1)
    assert _entries(factories, hh) == []
    assert _finish(factories, hh, config) == {}
    assert stat.S_ISFIFO(os.lstat(config.inbox / "pipe.csv").st_mode)


def test_traversal_looking_names_are_metadata_only(
    factories: Factories,
    owner: UserRecord,
    config: DropFolderConfig,
    deps: IntakeDeps,
    tmp_path: Path,
) -> None:
    hh = _hh(owner)
    hostile = "statement..\\..\\..\\escape.csv"
    _write_old(config.inbox / "a..b" / hostile, _csv(6))
    _write_old(config.inbox / "..." / "hidden-dir.csv", _csv(7))  # hidden folder: ignored
    before = sorted(str(p) for p in tmp_path.rglob("*") if "processed" not in p.parts)
    stats = _run(factories, hh, config, deps)
    assert stats.taken == 1
    (doc,) = _documents(factories, hh)
    assert doc.original_filename == "escape.csv"
    assert doc.source_meta["path"] == f"a..b/{hostile}"
    assert doc.source_meta["folder"] == "a-b"
    _finish(factories, hh, config)
    (moved,) = _files(config.processed)
    assert moved.parent == config.processed
    assert "\\" not in moved.name
    assert "/" not in moved.name
    after = sorted(
        str(p)
        for p in tmp_path.rglob("*")
        if "processed" not in p.parts and "documents" not in p.parts and "quarantine" not in p.parts
    )
    assert set(after) <= set(before)  # nothing appeared anywhere but processed/


def test_unsupported_and_empty_files_are_refused_and_moved_to_failed(
    factories: Factories, owner: UserRecord, config: DropFolderConfig, deps: IntakeDeps
) -> None:
    hh = _hh(owner)
    _write_old(config.inbox / "blob.bin", b"\x00\x01\x02\x03\xfe\xff" * 50)
    _write_old(config.inbox / "page.csv", b"<html><script>alert(1)</script></html>")
    _write_old(config.inbox / "empty.csv", b"")
    stats = _run(factories, hh, config, deps)
    assert (stats.taken, stats.refused) == (0, 3)
    reasons = {e.rel_path: e.reason for e in _entries(factories, hh)}
    assert reasons == {"blob.bin": "binary_unknown", "page.csv": "markup", "empty.csv": "empty"}
    assert _documents(factories, hh) == []
    assert _finish(factories, hh, config) == {"moved_failed": 3}
    assert len(_files(config.failed)) == 3
    assert _files(config.inbox) == []


# ---------------------------------------------------------------------------
# Caps (C10)
# ---------------------------------------------------------------------------


def test_per_run_file_and_byte_caps(
    factories: Factories, owner: UserRecord, config: DropFolderConfig, deps: IntakeDeps
) -> None:
    hh = _hh(owner)
    for i in range(5):
        _write_old(config.inbox / f"f{i}.csv", _csv(10 + i))
    capped = DropFolderConfig(**{**config.__dict__, "max_files": 2, "after": "keep"})
    first = _run(factories, hh, capped, deps)
    assert (first.taken, first.capped) == (2, True)
    second = _run(factories, hh, capped, deps)
    assert (second.taken, second.known, second.capped) == (2, 2, True)
    third = _run(factories, hh, capped, deps)
    assert (third.taken, third.capped) == (1, False)
    assert len(_documents(factories, hh)) == 5


def test_byte_cap_leaves_the_rest_for_the_next_run(
    factories: Factories, owner: UserRecord, config: DropFolderConfig, deps: IntakeDeps
) -> None:
    hh = _hh(owner)
    sizes = []
    for i in range(3):
        sizes.append(len(_write_old(config.inbox / f"f{i}.csv", _csv(20 + i)).read_bytes()))
    capped = DropFolderConfig(**{**config.__dict__, "max_bytes": sizes[0] + 1, "after": "keep"})
    stats = _run(factories, hh, capped, deps)
    assert (stats.taken, stats.capped, stats.bytes) == (1, True, sizes[0])
    assert _run(factories, hh, capped, deps).taken == 1


# ---------------------------------------------------------------------------
# After-modes and idempotency
# ---------------------------------------------------------------------------


def test_keep_mode_never_takes_a_file_twice_across_restarts(
    factories: Factories, owner: UserRecord, config: DropFolderConfig, deps: IntakeDeps
) -> None:
    hh = _hh(owner)
    keep = DropFolderConfig(**{**config.__dict__, "after": "keep"})
    path = _write_old(config.inbox / "a.csv", _csv(30))
    assert _run(factories, hh, keep, deps).taken == 1
    # A new run (new transaction, as after a worker restart) remembers it.
    again = _run(factories, hh, keep, deps)
    assert (again.taken, again.known) == (0, 1)
    # The same content under another name is not taken again either.
    (config.inbox / "copy-of-a.csv").write_bytes(path.read_bytes())
    third = _run(factories, hh, keep, deps)
    assert (third.taken, third.duplicates) == (0, 1)
    assert len(_documents(factories, hh)) == 1
    assert {e.after_state for e in _entries(factories, hh)} == {"kept"}
    assert path.exists()
    assert (config.inbox / "copy-of-a.csv").exists()
    assert _finish(factories, hh, keep) == {}
    # Changed content at the same path is a new file.
    path.write_bytes(_csv(31))
    assert _run(factories, hh, keep, deps).taken == 1


def test_move_is_finished_by_the_next_run_after_a_crash(
    factories: Factories, owner: UserRecord, config: DropFolderConfig, deps: IntakeDeps
) -> None:
    hh = _hh(owner)
    path = _write_old(config.inbox / "a.csv", _csv(32))
    assert _run(factories, hh, config, deps).taken == 1
    # The finish job never ran (worker died after the commit).
    assert path.exists()
    stats = _run(factories, hh, config, deps)
    assert stats.taken == 0
    assert stats.finished == {"moved": 1}
    assert not path.exists()
    assert len(_documents(factories, hh)) == 1


def test_rolled_back_run_leaves_the_file_for_the_next(
    factories: Factories, owner: UserRecord, config: DropFolderConfig, deps: IntakeDeps
) -> None:
    hh = _hh(owner)
    path = _write_old(config.inbox / "a.csv", _csv(33))
    taken: list[int] = []

    def crash() -> None:
        with factories.transaction() as db:
            set_context(db, hh)
            taken.append(
                dropfolder.watch(db, hh, config, deps, now=lambda: time.time() + LATER).taken
            )
            raise RuntimeError("crash before commit")

    with pytest.raises(RuntimeError):
        crash()
    assert taken == [1]
    assert path.exists()
    assert _entries(factories, hh) == []
    assert _run(factories, hh, config, deps).taken == 1


def test_delete_mode_deletes_taken_files_and_keeps_refused_ones(
    factories: Factories, owner: UserRecord, config: DropFolderConfig, deps: IntakeDeps
) -> None:
    hh = _hh(owner)
    delete = DropFolderConfig(**{**config.__dict__, "after": "delete"})
    _write_old(config.inbox / "a.csv", _csv(34))
    _write_old(config.inbox / "junk.bin", b"\x00\xff" * 10)
    stats = _run(factories, hh, delete, deps)
    assert (stats.taken, stats.refused, stats.pending_after) == (1, 1, 1)
    assert _finish(factories, hh, delete) == {"deleted": 1}
    assert [p.name for p in _files(config.inbox)] == ["junk.bin"]
    assert _files(config.processed) == []
    assert _run(factories, hh, delete, deps).refused == 0  # remembered


def test_read_only_inbox_acts_like_keep(
    factories: Factories,
    owner: UserRecord,
    config: DropFolderConfig,
    deps: IntakeDeps,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hh = _hh(owner)
    monkeypatch.setattr(dropfolder, "inbox_writable", lambda _config: False)
    path = _write_old(config.inbox / "a.csv", _csv(35))
    stats = _run(factories, hh, config, deps)
    assert (stats.taken, stats.after_mode, stats.pending_after) == (1, "keep", 0)
    assert _finish(factories, hh, config) == {}
    assert path.exists()
    assert _run(factories, hh, config, deps).known == 1


def test_ready_sidecar_mode(
    factories: Factories, owner: UserRecord, config: DropFolderConfig, deps: IntakeDeps
) -> None:
    hh = _hh(owner)
    ready = DropFolderConfig(**{**config.__dict__, "ready_sidecar": True, "after": "delete"})
    _write_old(config.inbox / "a.csv", _csv(36))
    stats = _run(factories, hh, ready, deps)
    assert stats.taken == 0
    assert stats.not_ready["no_ready_sidecar"] == 1
    (config.inbox / "a.csv.ready").write_bytes(b"")
    assert _run(factories, hh, ready, deps).taken == 1
    _finish(factories, hh, ready)
    assert _files(config.inbox) == []


def test_a_second_run_while_one_holds_the_lock_does_nothing(
    factories: Factories, owner: UserRecord, config: DropFolderConfig, deps: IntakeDeps
) -> None:
    hh = _hh(owner)
    _write_old(config.inbox / "a.csv", _csv(37))
    with factories.transaction() as busy:
        assert dropfolder.try_lock(busy)
        stats = _run(factories, hh, config, deps)
        assert stats.locked
        assert stats.taken == 0
    assert _run(factories, hh, config, deps).taken == 1


# ---------------------------------------------------------------------------
# Attribution and "Who does this belong to?"
# ---------------------------------------------------------------------------


@pytest.fixture
def actors(doc_settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(doc_settings, factories) as opened:
        yield opened


def _principal(record: UserRecord) -> Principal:
    return Principal(
        user=record.user, household_id=record.household_id, role=record.role, auth_method="session"
    )


def _open_items(factories: Factories, household_id: uuid.UUID) -> list[ReviewItem]:
    return [
        i for i in _rows(factories, household_id, ReviewItem) if i.kind is ReviewKind.UNKNOWN_OWNER
    ]


def test_unknown_folder_is_held_and_resolved_with_remember(
    actors: Actors, factories: Factories, config: DropFolderConfig, deps: IntakeDeps
) -> None:
    owner = actors.new(role="owner")
    hh = _hh(owner.record)
    household = owner.record.household
    member = actors.new(role="member", household=household)
    viewer = actors.new(role="viewer", household=household)
    with factories.transaction() as db:
        account = make_account(db, hh, name="Card")
        hidden = make_account(db, hh, name="Hidden")
    factories.grant(hh, member.record.id, account.id, GrantLevel.EDIT)

    _write_old(config.inbox / "Strangers" / "a.csv", _csv(40))
    stats = _run(factories, hh, config, deps)
    assert (stats.taken, stats.held) == (1, 1)
    (doc,) = _documents(factories, hh)
    assert doc.status is DocumentStatus.NEEDS_REVIEW
    assert doc.uploaded_by is None
    assert doc.account_id is None
    (item,) = _open_items(factories, hh)
    assert item.document_id == doc.id
    assert item.payload == {
        "reason": "unknown_owner",
        "question": "Who does this belong to?",
        "folder": "strangers",
        "path": "Strangers/a.csv",
    }
    with factories.transaction() as db:
        set_context(db, hh)
        assert db.execute(select(Job).where(Job.type == INGEST_JOB_TYPE)).scalars().all() == []

    # Owners and admins see it (the document has no uploader or account yet).
    listed = owner.get(REVIEW, params={"kind": "unknown_owner"}).json()["items"]
    assert [(i["id"], i["next_step"], i["can_resolve"]) for i in listed] == [
        (str(item.id), "choose_owner", True)
    ]
    assert member.get(f"{REVIEW}/{item.id}").status_code == 404
    with factories.transaction() as db:
        set_context(db, hh)
        page = review_service.list_items(
            db,
            _principal(member.record),
            state=None,
            kind=None,
            document_id=None,
            cursor=None,
            limit=50,
        )
        assert page.items == []

    url = f"{REVIEW}/{item.id}/resolve"
    body = {"kind": "unknown_owner", "user_id": str(viewer.record.id), "remember": True}
    refused = owner.post(url, json=body)
    assert refused.status_code == 422
    assert problem_type(refused) == "invalid-resolution"
    body = {"kind": "unknown_owner", "user_id": str(member.record.id), "account_id": str(hidden.id)}
    refused = owner.post(url, json=body)
    assert refused.status_code == 422  # the member may not import into that account
    body["account_id"] = str(account.id)
    body["remember"] = True
    resolved = owner.post(url, json=body)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["state"] == "resolved"
    assert resolved.json()["resolution"]["remembered"] is True

    (doc,) = _documents(factories, hh)
    assert doc.status is DocumentStatus.RECEIVED
    assert (doc.uploaded_by, doc.account_id) == (member.record.id, account.id)
    with factories.transaction() as db:
        set_context(db, hh)
        jobs = db.execute(select(Job).where(Job.type == INGEST_JOB_TYPE)).scalars().all()
        assert [j.payload["document_id"] for j in jobs if j.household_id == hh] == [str(doc.id)]
    (route,) = _rows(factories, hh, DropfolderRoute)
    assert (route.folder, route.user_id, route.account_id) == (
        "strangers",
        member.record.id,
        account.id,
    )
    # The next file in that folder goes straight to the member's account.
    _write_old(config.inbox / "strangers" / "b.csv", _csv(41))
    stats = _run(factories, hh, config, deps)
    assert (stats.taken, stats.held) == (1, 0)
    newest = _documents(factories, hh)[-1]
    assert (newest.uploaded_by, newest.account_id) == (member.record.id, account.id)
    assert newest.source_meta["attributed_by"] == "route"


def test_routes_and_names_that_no_longer_hold_are_ignored(
    factories: Factories, owner: UserRecord, config: DropFolderConfig, deps: IntakeDeps
) -> None:
    hh = _hh(owner)
    viewer = factories.user(
        role="viewer", household=owner.household, username=f"vic{uuid.uuid4().hex[:8]}"
    )
    member = factories.user(
        role="member", household=owner.household, username=f"mia{uuid.uuid4().hex[:8]}"
    )
    with factories.transaction() as db:
        not_hers = make_account(db, hh, name="Savings")
        set_context(db, hh)
        db.add(DropfolderRoute(household_id=hh, folder="old", user_id=viewer.id))
    # A viewer's folder, a route to a viewer: held.
    _write_old(config.inbox / viewer.user.username / "x.csv", _csv(50))
    _write_old(config.inbox / "old" / "y.csv", _csv(51))
    # A member's folder with an account folder she may not edit: hers, no account.
    _write_old(config.inbox / member.user.username / "savings" / "z.csv", _csv(52))
    # Directly in the inbox: nobody.
    _write_old(config.inbox / "flat.csv", _csv(53))
    stats = _run(factories, hh, config, deps)
    assert (stats.taken, stats.held) == (4, 3)
    by_path = {d.source_meta["path"]: d for d in _documents(factories, hh)}
    z = by_path[f"{member.user.username}/savings/z.csv"]
    assert (z.uploaded_by, z.account_id, z.status) == (member.id, None, DocumentStatus.RECEIVED)
    assert not_hers.id != z.account_id
    for path in (f"{viewer.user.username}/x.csv", "old/y.csv", "flat.csv"):
        assert by_path[path].uploaded_by is None
        assert by_path[path].status is DocumentStatus.NEEDS_REVIEW
    folders = sorted(i.payload["folder"] for i in _open_items(factories, hh))
    assert folders == sorted(["", "old", viewer.user.username])


# ---------------------------------------------------------------------------
# The worker handlers
# ---------------------------------------------------------------------------


def _handler_settings(
    doc_settings: Settings, config: DropFolderConfig, household_id: uuid.UUID | None, **kw: Any
) -> Settings:
    update: dict[str, Any] = {
        "dropfolder_enabled": True,
        "dropfolder_inbox": config.inbox,
        "dropfolder_processed": config.processed,
        "dropfolder_settle_seconds": 0,
        "dropfolder_watch_seconds": 0,
        "dropfolder_household_id": household_id,
    }
    update.update(kw)
    return doc_settings.model_copy(update=update)


def _run_handler(factories: Factories, job_type: str, household_id: uuid.UUID | None) -> None:
    from datetime import UTC, datetime

    spec = REGISTRY.require(job_type)
    with factories.transaction() as db:
        set_context(db, household_id)
        ctx = JobContext(
            job_id=uuid.uuid4(),
            job_type=job_type,
            attempt=1,
            max_attempts=spec.max_attempts,
            household_id=household_id,
            session=db,
            deadline=datetime.max.replace(tzinfo=UTC),
        )
        spec.func(ctx, spec.validate_payload({}))


def test_handlers_take_and_move_for_the_configured_household(
    factories: Factories,
    owner: UserRecord,
    config: DropFolderConfig,
    doc_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hh = _hh(owner)
    path = _write_old(config.inbox / "a.csv", _csv(60))
    old = time.time() - 120
    os.utime(path, (old, old))
    current = {"settings": _handler_settings(doc_settings, config, hh, dropfolder_enabled=False)}
    monkeypatch.setattr(handler, "get_settings", lambda: current["settings"])

    _run_handler(factories, handler.WATCH_JOB_TYPE, None)  # disabled: nothing
    assert _documents(factories, hh) == []
    factories.household()  # at least two households now
    current["settings"] = _handler_settings(doc_settings, config, None)
    _run_handler(factories, handler.WATCH_JOB_TYPE, None)  # several households, none chosen
    assert _documents(factories, hh) == []

    current["settings"] = _handler_settings(doc_settings, config, hh)
    _run_handler(factories, handler.WATCH_JOB_TYPE, None)
    (doc,) = _documents(factories, hh)
    assert doc.source is DocumentSource.DROPFOLDER
    with factories.transaction() as db:
        set_context(db, hh)
        finish = db.execute(
            select(Job).where(Job.household_id == hh, Job.type == handler.FINISH_JOB_TYPE)
        ).scalar_one()
        assert finish.payload == {}
    assert path.exists()
    _run_handler(factories, handler.FINISH_JOB_TYPE, hh)
    assert not path.exists()
    assert len(_files(config.processed)) == 1


def test_watch_is_a_recurring_job() -> None:
    from app.workers.scheduler import SCHEDULE

    (job,) = [j for j in SCHEDULE.jobs() if j.job_type == handler.WATCH_JOB_TYPE]
    assert job.interval_seconds == 60
    assert REGISTRY.require(handler.WATCH_JOB_TYPE).max_attempts == 1

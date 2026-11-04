# SPDX-License-Identifier: AGPL-3.0-or-later
"""``perfin rotate-key``: every data key rewrapped, no payload re-encrypted (SECURITY.md G4)."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, text

from app.cli import main
from app.core.audit import verify_chain
from app.core.crypto import (
    MIN_ENVELOPE_BYTES,
    NONCE_BYTES,
    TAG_BYTES,
    DecryptionError,
    Keyring,
    column_ad,
)
from app.core.settings import get_settings
from app.db.models import AuditLog
from app.db.rls import set_context
from app.db.session import make_sessionmaker
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import Blob, Document
from app.domain.documents.service import open_plaintext
from app.ops import keyrotation
from tests.fixtures.encrypted_rows import Populated, populate
from tests.fixtures.factories import Factories
from tests.fixtures.scratch_db import ScratchInstance, new_master_key, scratch_instance

# Envelope bytes after the header and DEK wrap: payload nonce + ciphertext.
_PAYLOAD_OFFSET = MIN_ENVELOPE_BYTES - NONCE_BYTES - TAG_BYTES


@dataclass
class World:
    instance: ScratchInstance
    root: Path
    engine: Engine
    store: LocalFSBlobStore
    populated: list[Populated]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    root = tmp_path / "data"
    with scratch_instance() as instance:
        settings = instance.settings(instance.source, root)
        engine = instance.source.engine()
        store = LocalFSBlobStore.from_settings(settings)
        factories = Factories(make_sessionmaker(engine), settings)
        populated = [populate(factories, store, tag=t) for t in ("a", "bb")]
        for name, value in instance.env(instance.source, root).items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        try:
            yield World(instance, root, engine, store, populated)
        finally:
            engine.dispose()


def _keyring(key_b64: str, version: int) -> Keyring:
    return Keyring.from_master_key(base64.b64decode(key_b64), version=version)


def _envelope_payloads(world: World) -> dict[tuple[str, str, str], bytes]:
    out: dict[tuple[str, str, str], bytes] = {}
    for target in keyrotation.ENVELOPE_COLUMNS:
        table = keyrotation._table(target.table)
        col = table.c[target.column]
        for scope in keyrotation._scopes(world.engine, target.table):
            with world.engine.begin() as conn:
                set_context(conn, scope)
                for row_id, value in conn.execute(select(table.c.id, col).where(col.is_not(None))):
                    out[(target.table, target.column, str(row_id))] = bytes(value)
    return out


def _blob_files(world: World) -> dict[str, str]:
    return {
        str(p.relative_to(world.root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(world.root.rglob("*"))
        if p.is_file()
    }


def _blob_nonces(world: World) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for household in keyrotation.household_ids(world.engine):
        with world.engine.begin() as conn:
            set_context(conn, household)
            for row in conn.execute(select(Blob.id, Blob.nonce)):
                out[str(row.id)] = bytes(row.nonce)
    return out


def _check_all_decrypt(world: World, keyring: Keyring) -> None:
    """Every expected plaintext and every document decrypt with ``keyring`` alone."""
    for populated in world.populated:
        for (table, column, row_id), plaintext in populated.values.items():
            tbl = keyrotation._table(table)
            with world.engine.begin() as conn:
                set_context(conn, populated.household_id)
                value = conn.execute(select(tbl.c[column]).where(tbl.c.id == row_id)).scalar_one()
            assert keyring.decrypt_bytes(bytes(value), ad=column_ad(table, column, row_id)) == (
                plaintext
            )
        with make_sessionmaker(world.engine)() as db, db.begin():
            set_context(db, populated.household_id)
            document = db.get(Document, populated.document_id)
            assert document is not None
            blob = db.get(Blob, document.blob_id)
            assert blob is not None
            data = b"".join(open_plaintext(world.store, keyring, blob))
            assert hashlib.sha256(data).hexdigest() == document.sha256


def _audit_actions(world: World) -> list[str]:
    with world.instance.source.engine("owner").connect() as conn:
        return list(conn.execute(select(AuditLog.action).order_by(AuditLog.seq)).scalars())


def test_rotate_key_rewraps_every_dek_and_keeps_payloads(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = _keyring(world.instance.master_key, 1)
    _check_all_decrypt(world, old)
    files_before = _blob_files(world)
    nonces_before = _blob_nonces(world)
    payloads_before = _envelope_payloads(world)
    counts = keyrotation.versions_by_target(world.engine)
    # Every target is populated, so the test covers every encrypted column.
    assert all(counts[name] for name in keyrotation.targets()), counts

    new_key = new_master_key()
    key_file = tmp_path / "new.key"
    key_file.write_text(new_key + "\n")
    assert main(["rotate-key", "--new-key-file", str(key_file), "--batch-size", "2"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["ok"] is True
    assert report["from_version"] == 1
    assert report["to_version"] == 2
    assert report["to_key_id"] == _keyring(new_key, 2).key_id(2)
    assert sum(report["rewrapped"].values()) == sum(n for c in counts.values() for n in c.values())
    assert "rewrapped" in captured.err  # progress on stderr
    assert "PERFIN_MASTER_KEY_VERSION=2" in captured.err
    assert new_key not in captured.out + captured.err

    after = keyrotation.versions_by_target(world.engine)
    assert all(set(c) == {2} for c in after.values()), after
    # Payloads untouched: blob files, blob stream headers, envelope payloads.
    assert _blob_files(world) == files_before
    assert _blob_nonces(world) == nonces_before
    payloads_after = _envelope_payloads(world)
    assert payloads_after.keys() == payloads_before.keys()
    for key, value in payloads_before.items():
        assert payloads_after[key][_PAYLOAD_OFFSET:] == value[_PAYLOAD_OFFSET:]
        assert payloads_after[key][:_PAYLOAD_OFFSET] != value[:_PAYLOAD_OFFSET]

    new = _keyring(new_key, 2)
    _check_all_decrypt(world, new)
    with pytest.raises(DecryptionError):
        _check_all_decrypt(world, _keyring(world.instance.master_key, 2))
    with pytest.raises(DecryptionError):
        _check_all_decrypt(world, old)

    assert _audit_actions(world)[-1] == "instance.master_key_rotated"
    with world.instance.source.engine("owner").connect() as conn:
        assert verify_chain(conn).ok

    # The old configuration is now flagged by --status; the new one is clean.
    assert main(["rotate-key", "--status"]) == 1
    status = json.loads(capsys.readouterr().out)
    assert status["ok"] is False
    assert status["key_version"] == 1
    get_settings.cache_clear()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PERFIN_MASTER_KEY", new_key)
        mp.setenv("PERFIN_MASTER_KEY_VERSION", "2")
        assert main(["rotate-key", "--status"]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["ok"] is True
        assert status["key_id"] == new.key_id(2)
        get_settings.cache_clear()
        # Running again with the new key configured rotates nothing new away.
        assert main(["backup-info"]) == 0
        info = json.loads(capsys.readouterr().out)
        assert info == {
            "master_key_id": new.key_id(2),
            "master_key_version": 2,
            "perfin_version": info["perfin_version"],
        }


class _InterruptedError(Exception):
    pass


def test_interrupted_rotation_resumes(world: World) -> None:
    old_key = base64.b64decode(world.instance.master_key)
    new_key_b64 = new_master_key()
    new_key = base64.b64decode(new_key_b64)
    calls = 0

    def stop_after_three(_message: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise _InterruptedError

    with pytest.raises(_InterruptedError):
        keyrotation.rotate(
            world.engine,
            old_key=old_key,
            old_version=1,
            new_key=new_key,
            new_version=2,
            batch_size=1,
            progress=stop_after_three,
        )
    mixed = keyrotation.versions_by_target(world.engine)
    assert mixed[keyrotation.BLOB_TARGET].get(2), mixed  # some committed
    assert any(1 in c for c in mixed.values()), mixed  # some still old
    assert "instance.master_key_rotated" not in _audit_actions(world)
    # The lock was released with the failed run.
    report = keyrotation.rotate(
        world.engine, old_key=old_key, old_version=1, new_key=new_key, new_version=2
    )
    assert report.ok
    assert sum(r.rewrapped for r in report.results.values()) == sum(
        n for c in mixed.values() for v, n in c.items() if v == 1
    )
    _check_all_decrypt(world, _keyring(new_key_b64, 2))
    assert _audit_actions(world).count("instance.master_key_rotated") == 1


def test_a_key_that_does_not_unwrap_fails_the_rotation(world: World) -> None:
    """A value under a key the run does not hold is reported; no audit row, exit 1."""
    stranger = _keyring(new_master_key(), 1)
    target = world.populated[0]
    channel = next(k for k in target.values if k[0] == "notification_channel")
    with world.engine.begin() as conn:
        set_context(conn, target.household_id)
        conn.execute(
            text("UPDATE notification_channel SET config_encrypted = :v WHERE id = :id"),
            {
                "v": stranger.encrypt_bytes(b"{}", ad=column_ad(*channel)),
                "id": channel[2],
            },
        )
    report = keyrotation.rotate(
        world.engine,
        old_key=base64.b64decode(world.instance.master_key),
        old_version=1,
        new_key=base64.b64decode(new_master_key()),
        new_version=2,
    )
    assert not report.ok
    assert report.results["notification_channel.config_encrypted"].failed == 1
    assert report.results["notification_channel.config_encrypted"].failed_ids == [str(channel[2])]
    assert report.verify_failed == 1
    assert report.remaining["notification_channel.config_encrypted"] == {1: 1, 2: 1}
    assert "instance.master_key_rotated" not in _audit_actions(world)


def test_rotation_refuses_bad_input_and_a_concurrent_run(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = base64.b64decode(world.instance.master_key)
    with pytest.raises(ValueError, match="same as the current key"):
        keyrotation.rotate(world.engine, old_key=key, old_version=1, new_key=key, new_version=2)
    with pytest.raises(ValueError, match="different version"):
        keyrotation.rotate(
            world.engine, old_key=key, old_version=1, new_key=bytes(32), new_version=1
        )
    with world.engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": keyrotation.LOCK_KEY})
        with pytest.raises(keyrotation.RotationLockedError):
            keyrotation.rotate(
                world.engine, old_key=key, old_version=1, new_key=bytes(32), new_version=2
            )
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": keyrotation.LOCK_KEY})
    bad = tmp_path / "bad.key"
    bad.write_text("not base64!")
    assert main(["rotate-key", "--new-key-file", str(bad)]) == 1
    assert "not valid base64" in capsys.readouterr().err
    short = tmp_path / "short.key"
    short.write_text(base64.b64encode(b"x" * 16).decode())
    assert main(["rotate-key", "--new-key-file", str(short)]) == 1
    assert "32 bytes" in capsys.readouterr().err
    assert main(["rotate-key"]) == 1
    assert "no new key" in capsys.readouterr().err
    # --dry-run counts and changes nothing.
    good = tmp_path / "good.key"
    good.write_text(new_master_key())
    assert main(["rotate-key", "--new-key-file", str(good), "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert dry["pending"][keyrotation.BLOB_TARGET] == 2
    assert all(set(c) == {1} for c in keyrotation.versions_by_target(world.engine).values())

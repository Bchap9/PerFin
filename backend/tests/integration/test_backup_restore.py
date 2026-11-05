# SPDX-License-Identifier: AGPL-3.0-or-later
"""Restore verification: scripts/backup.sh and scripts/restore.sh end to end (SECURITY.md G3).

A synthetic instance (two households: accounts with generated transactions,
a stored document each, a TOTP user, every encrypted column, audit rows) is
backed up with ``scripts/backup.sh --bare`` (pg_dump as the superuser, the
document directories, ``perfin backup-encrypt``) and restored with
``scripts/restore.sh --bare`` into a fresh database and fresh document
directories. The restore must reproduce the row count of every table, the
ledger totals, every document's SHA-256 and the audit-log chain head; and it
must refuse a tampered archive, a wrong passphrase, the wrong master key and
a non-empty target without ``--force``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess  # nosec B404 - runs the repository's own scripts
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import text

from app.cli import BACKEND_DIR
from app.core.audit import audit, verify_chain
from app.core.crypto import Keyring
from app.db.rls import quote_ident, set_context
from app.db.session import make_sessionmaker
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import Blob, Document
from app.domain.documents.service import open_plaintext
from app.ops.keyrotation import household_ids
from tests.fixtures.encrypted_rows import populate
from tests.fixtures.factories import Factories
from tests.fixtures.ledger import LedgerFactories, synthetic_transactions
from tests.fixtures.scratch_db import (
    ScratchDb,
    ScratchInstance,
    libpq,
    new_master_key,
    scratch_instance,
)

SCRIPTS = BACKEND_DIR.parent / "scripts"
PASSPHRASE = "synthetic backup passphrase 42"


@dataclass
class Setup:
    instance: ScratchInstance
    tmp: Path
    source_root: Path
    archive: Path


def _run(
    script: str, args: list[str], env: dict[str, str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    full_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(BACKEND_DIR)),
        "PERFIN_CLI": f"{sys.executable} -m app.cli",
        **env,
    }
    result = subprocess.run(  # nosec B603 - fixed script, test-built args
        ["bash", str(SCRIPTS / script), *args],
        cwd=BACKEND_DIR,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"{script} failed ({result.returncode}):\n{result.stderr}")
    return result


def _dirs(root: Path) -> None:
    for name in ("documents", "quarantine"):
        (root / name).mkdir(parents=True, exist_ok=True)


def _populate(instance: ScratchInstance, root: Path) -> None:
    settings = instance.settings(instance.source, root)
    engine = instance.source.engine()
    try:
        store = LocalFSBlobStore.from_settings(settings)
        factories = Factories(make_sessionmaker(engine), settings)
        ledger = LedgerFactories(make_sessionmaker(engine))
        for seed, tag in enumerate(("a", "bb")):
            populated = populate(factories, store, tag=tag)
            for kind in ("checking", "savings"):
                account = ledger.account(populated.household_id, type=kind)
                ledger.run(synthetic_transactions, account, count=25, seed=seed * 10 + len(kind))
            with engine.begin() as conn:
                set_context(conn, populated.household_id)
                audit(
                    conn,
                    action="test.backup_fixture",
                    target_type="household",
                    target_id=populated.household_id,
                    household_id=populated.household_id,
                )
        with engine.begin() as conn:
            set_context(conn, None, system_actor=True)
            audit(conn, action="test.instance_event", target_type="system")
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def setup(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Setup]:
    tmp = tmp_path_factory.mktemp("backup")
    with scratch_instance(targets=4) as instance:
        source_root = tmp / "source"
        _dirs(source_root)
        _populate(instance, source_root)
        out = tmp / "out"
        result = _run(
            "backup.sh",
            ["--bare", "--output-dir", str(out)],
            {
                **instance.env(instance.source, source_root),
                "PERFIN_BACKUP_PG_URL": libpq(instance.source.admin_url),
                "PERFIN_BACKUP_PASSPHRASE": PASSPHRASE,
                "PERFIN_BACKUP_TMPDIR": str(tmp),
            },
        )
        archive = Path(result.stdout.strip().splitlines()[-1])
        assert "master key is NOT in this backup" in result.stderr
        yield Setup(instance, tmp, source_root, archive)


def _restore(
    setup: Setup,
    target: ScratchDb,
    root: Path,
    *extra: str,
    archive: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    _dirs(root)
    return _run(
        "restore.sh",
        ["--bare", *extra, str(archive or setup.archive)],
        {
            **setup.instance.env(target, root),
            "PERFIN_RESTORE_PG_URL": libpq(target.admin_url),
            "PERFIN_BACKUP_PASSPHRASE": PASSPHRASE,
            "PERFIN_BACKUP_TMPDIR": str(setup.tmp),
            **(env or {}),
        },
        check=check,
    )


def _row_counts(db: ScratchDb) -> dict[str, int]:
    engine = db.engine("admin")
    try:
        with engine.connect() as conn:
            tables = conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")
            ).scalars()
            return {
                # Names from pg_tables, validated and quoted by quote_ident.
                name: int(
                    conn.execute(
                        text(f"SELECT count(*) FROM {quote_ident(name)}")  # noqa: S608
                    ).scalar_one()
                )
                for name in list(tables)
            }
    finally:
        engine.dispose()


def _ledger_totals(db: ScratchDb) -> dict[str, str]:
    engine = db.engine("admin")
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    'SELECT account_id, sum(amount), count(*) FROM "transaction" '
                    "GROUP BY account_id ORDER BY account_id"
                )
            ).all()
            return {str(r[0]): f"{r[1]}/{r[2]}" for r in rows}
    finally:
        engine.dispose()


def _document_hashes(instance: ScratchInstance, db: ScratchDb, root: Path) -> dict[str, str]:
    """Decrypt every document with the master key; id -> SHA-256 of the plaintext."""
    settings = instance.settings(db, root)
    keyring = Keyring.from_settings(settings)
    store = LocalFSBlobStore.from_settings(settings)
    engine = db.engine()
    out: dict[str, str] = {}
    try:
        for household in household_ids(engine):
            with make_sessionmaker(engine)() as session, session.begin():
                set_context(session, household)
                for document in session.query(Document).all():
                    blob = session.get(Blob, document.blob_id)
                    assert blob is not None
                    digest = hashlib.sha256(b"".join(open_plaintext(store, keyring, blob)))
                    assert digest.hexdigest() == document.sha256
                    out[str(document.id)] = digest.hexdigest()
    finally:
        engine.dispose()
    return out


def _audit_head(db: ScratchDb) -> tuple[int, str]:
    engine = db.engine("owner")
    try:
        with engine.connect() as conn:
            result = verify_chain(conn)
        assert result.ok
        return result.head_seq, result.head_hash
    finally:
        engine.dispose()


def _manifest(setup: Setup) -> dict[str, object]:
    from app.ops.backup import decrypt_to

    plain = setup.tmp / "manifest-check.tar"
    with setup.archive.open("rb") as source, plain.open("wb") as sink:
        decrypt_to(source, sink, PASSPHRASE)
    import tarfile

    with tarfile.open(plain) as archive:
        names = sorted(archive.getnames())
        member = archive.extractfile("manifest.json")
        assert member is not None
        manifest: dict[str, object] = json.loads(member.read())
    plain.unlink()
    assert names == ["db.dump", "documents.tar", "manifest.json", "quarantine.tar"]
    return manifest


def test_backup_archive_is_encrypted_and_described(setup: Setup) -> None:
    data = setup.archive.read_bytes()
    assert data.startswith(b"PFARCH\x00\x01")
    assert oct(setup.archive.stat().st_mode & 0o777) == "0o600"
    sidecar = setup.archive.with_name(setup.archive.name + ".sha256")
    assert sidecar.read_text().split()[0] == hashlib.sha256(data).hexdigest()
    # Nothing readable leaks: no table name, payee or the master key.
    for needle in (b"Synthetic Payee", b"CREATE TABLE", setup.instance.master_key.encode()):
        assert needle not in data
    manifest = _manifest(setup)
    assert manifest["format"] == "perfin-backup"
    assert manifest["format_version"] == 1
    seq, head = _audit_head(setup.instance.source)
    assert manifest["audit"] == {"head_seq": seq, "head_hash": head, "rows_checked": seq}
    keyring = Keyring.from_master_key(base64.b64decode(setup.instance.master_key))
    assert manifest["master_key"] == {"key_id": keyring.key_id(1), "key_version": 1}
    assert manifest["owner_role"] == setup.instance.owner_role
    assert manifest["alembic_head"]
    assert setup.instance.master_key not in json.dumps(manifest)
    # The plaintext staging directory is gone.
    assert not list(setup.tmp.glob("perfin-backup.*"))


def test_restore_into_a_fresh_stack_reproduces_everything(setup: Setup) -> None:
    target = setup.instance.targets[0]
    root = setup.tmp / "restored"
    result = _restore(setup, target, root)
    assert "restore complete and verified" in result.stderr
    assert '"ok": true' in result.stderr  # restore-check report

    source_counts = _row_counts(setup.instance.source)
    assert source_counts["transaction"] == 100
    assert source_counts["document"] == 2
    assert _row_counts(target) == source_counts
    assert _ledger_totals(target) == _ledger_totals(setup.instance.source)
    source_docs = _document_hashes(setup.instance, setup.instance.source, setup.source_root)
    assert len(source_docs) == 2
    assert _document_hashes(setup.instance, target, root) == source_docs
    assert _audit_head(target) == _audit_head(setup.instance.source)
    assert not list(setup.tmp.glob("perfin-restore.*"))

    # A second restore onto the now non-empty target is refused...
    again = _restore(setup, target, root, check=False)
    assert again.returncode == 1
    assert "refusing to overwrite existing data" in again.stderr
    # ...unless forced, which replaces it with the same content.
    forced = _restore(setup, target, root, "--force", "--check-all")
    assert "--force: dropping schema public" in forced.stderr
    assert _row_counts(target) == source_counts
    assert _document_hashes(setup.instance, target, root) == source_docs


def test_restore_refuses_a_tampered_archive_and_a_wrong_passphrase(setup: Setup) -> None:
    target = setup.instance.targets[1]
    root = setup.tmp / "tampered"
    data = bytearray(setup.archive.read_bytes())
    data[len(data) // 2] ^= 0x01
    tampered = setup.tmp / "tampered.pfbak"
    tampered.write_bytes(bytes(data))
    result = _restore(setup, target, root, archive=tampered, check=False)
    assert result.returncode == 1
    assert "decryption failed" in result.stderr
    # A sidecar that does not match is refused before decrypting.
    (setup.tmp / "tampered.pfbak.sha256").write_text(
        setup.archive.with_name(setup.archive.name + ".sha256").read_text()
    )
    result = _restore(setup, target, root, archive=tampered, check=False)
    assert result.returncode == 1
    assert "does not match" in result.stderr
    result = _restore(
        setup, target, root, env={"PERFIN_BACKUP_PASSPHRASE": "not the passphrase"}, check=False
    )
    assert result.returncode == 1
    assert "decryption failed" in result.stderr
    # Truncated.
    truncated = setup.tmp / "truncated.pfbak"
    truncated.write_bytes(setup.archive.read_bytes()[:-64])
    result = _restore(setup, target, root, archive=truncated, check=False)
    assert result.returncode == 1
    assert "decryption failed" in result.stderr
    assert _row_counts(target) == {}


def test_restore_refuses_the_wrong_master_key(setup: Setup) -> None:
    target = setup.instance.targets[2]
    root = setup.tmp / "wrong-key"
    result = _restore(setup, target, root, env={"PERFIN_MASTER_KEY": new_master_key()}, check=False)
    assert result.returncode == 1
    assert "is not the key this backup was made with" in result.stderr
    assert _row_counts(target) == {}
    assert not any((root / "documents").iterdir())


def test_restore_refuses_a_manifest_mismatch(setup: Setup, tmp_path: Path) -> None:
    """A re-encrypted archive whose member does not match its manifest is refused."""
    import tarfile

    from app.ops.backup import decrypt_to, encrypt_to

    plain = tmp_path / "plain.tar"
    with setup.archive.open("rb") as source, plain.open("wb") as sink:
        decrypt_to(source, sink, PASSPHRASE)
    unpacked = tmp_path / "x"
    with tarfile.open(plain) as archive:
        archive.extractall(unpacked, filter="data")
    with (unpacked / "quarantine.tar").open("ab") as handle:
        handle.write(b"\0" * 512)
    forged_plain = tmp_path / "forged.tar"
    with tarfile.open(forged_plain, "w") as archive:
        for name in ("manifest.json", "db.dump", "documents.tar", "quarantine.tar"):
            archive.add(unpacked / name, arcname=name)
    forged = tmp_path / "forged.pfbak"
    with forged_plain.open("rb") as source, forged.open("wb") as sink:
        encrypt_to(source, sink, PASSPHRASE)
    target = setup.instance.targets[3]
    result = _restore(setup, target, tmp_path / "root", archive=forged, check=False)
    assert result.returncode == 1
    assert "quarantine.tar: size differs from the manifest" in result.stderr
    assert _row_counts(target) == {}


def test_backup_refuses_a_role_subject_to_row_level_security(setup: Setup, tmp_path: Path) -> None:
    """The owner role cannot dump FORCE RLS tables; the script refuses it up front."""
    _dirs(tmp_path)
    result = _run(
        "backup.sh",
        ["--bare", "--output-dir", str(tmp_path / "out")],
        {
            **setup.instance.env(setup.instance.source, tmp_path),
            "PERFIN_BACKUP_PG_URL": libpq(setup.instance.source.owner_url),
            "PERFIN_BACKUP_PASSPHRASE": PASSPHRASE,
        },
        check=False,
    )
    assert result.returncode == 1
    assert "superuser or a BYPASSRLS role" in result.stderr
    short = _run(
        "backup.sh",
        ["--bare", "--output-dir", str(tmp_path / "out")],
        {
            **setup.instance.env(setup.instance.source, tmp_path),
            "PERFIN_BACKUP_PG_URL": libpq(setup.instance.source.admin_url),
            "PERFIN_BACKUP_PASSPHRASE": "too short",
        },
        check=False,
    )
    assert short.returncode == 1
    assert "at least 12 characters" in short.stderr
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())

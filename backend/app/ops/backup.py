# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backup archive encryption and restore checks (SECURITY.md G3, docs/BACKUP_RESTORE.md).

``scripts/backup.sh`` builds a plain tar (manifest, ``pg_dump`` custom-format
dump, the document volumes) and pipes it through ``perfin backup-encrypt``,
which writes the "PerFin encrypted archive" format of
:mod:`app.core.archive_crypto` (Argon2id + libsodium secretstream; the same
format as passphrase-protected exports). ``scripts/restore.sh`` pipes the
file through ``perfin backup-decrypt``, which authenticates every chunk and
fails on a wrong passphrase, tampering, truncation or appended data; the
script only uses the output once the command exited 0.

The passphrase comes from ``--passphrase-file`` or the environment variable
``PERFIN_BACKUP_PASSPHRASE``, never from the command line. The master key is
never part of a backup: :func:`backup_info` reports only its non-secret
fingerprint (``Keyring.key_id``) and version, which the manifest records so
a restore can tell which key it needs.

:func:`restore_check` is the restore's document spot-check: it decrypts
stored blobs with the configured master key and compares each document's
plaintext SHA-256 with ``document.sha256``.
"""

from __future__ import annotations

import hashlib
import os
import random
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Final

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app import __version__
from app.core.archive_crypto import DEFAULT_KDF as ARCHIVE_DEFAULT_KDF
from app.core.archive_crypto import (
    ArchiveEncryptor,
    KdfParams,
    decrypt_stream,
    derive_key,
    new_salt,
)
from app.core.crypto import DecryptionError, Keyring
from app.db.rls import set_context
from app.domain.documents.blobstore import BlobStoreError
from app.domain.documents.models import Blob, Document
from app.ops.defaults import DEFAULT_SAMPLE
from app.ops.keyrotation import household_ids

PASSPHRASE_ENV: Final = "PERFIN_BACKUP_PASSPHRASE"  # noqa: S105 - a variable name
MIN_PASSPHRASE_CHARS: Final = 12
READ_BYTES: Final = 1024 * 1024


class PassphraseError(ValueError):
    """No usable passphrase (never includes the value)."""


def read_passphrase(passphrase_file: str | None, *, for_encryption: bool) -> str:
    """The backup passphrase from ``passphrase_file`` or ``PERFIN_BACKUP_PASSPHRASE``.

    A file's trailing newline is dropped. Encryption needs at least
    :data:`MIN_PASSPHRASE_CHARS` characters.
    """
    if passphrase_file:
        try:
            value = Path(passphrase_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PassphraseError("the passphrase file is not readable") from exc
        value = value.rstrip("\r\n")
    else:
        value = os.environ.get(PASSPHRASE_ENV, "")
    if not value:
        raise PassphraseError(f"no passphrase (use --passphrase-file or set {PASSPHRASE_ENV})")
    if for_encryption and len(value) < MIN_PASSPHRASE_CHARS:
        raise PassphraseError(
            f"the backup passphrase must have at least {MIN_PASSPHRASE_CHARS} characters"
        )
    return value


def _chunks(stream: BinaryIO) -> Iterator[bytes]:
    while chunk := stream.read(READ_BYTES):
        yield chunk


def encrypt_to(
    source: BinaryIO, sink: BinaryIO, passphrase: str, *, kdf: KdfParams = ARCHIVE_DEFAULT_KDF
) -> int:
    """Encrypt everything from ``source`` into ``sink``; return the plaintext size."""
    salt = new_salt()
    encryptor = ArchiveEncryptor(derive_key(passphrase, salt, kdf), salt=salt, kdf=kdf)
    sink.write(encryptor.header)
    total = 0
    for chunk in _chunks(source):
        total += len(chunk)
        sealed = encryptor.update(chunk)
        if sealed:
            sink.write(sealed)
    sink.write(encryptor.finish())
    sink.flush()
    return total


def decrypt_to(source: BinaryIO, sink: BinaryIO, passphrase: str) -> int:
    """Authenticate and decrypt ``source`` into ``sink``; return the plaintext size.

    Raises :class:`app.core.archive_crypto.ArchiveCryptoError` on any
    failure; the output written so far must then be discarded.
    """
    total = 0
    for plain in decrypt_stream(_chunks(source), passphrase):
        total += len(plain)
        sink.write(plain)
    sink.flush()
    return total


def backup_info(keyring: Keyring) -> dict[str, object]:
    """Non-secret facts a backup manifest records (no key material)."""
    return {
        "perfin_version": __version__,
        "master_key_id": keyring.key_id(keyring.current),
        "master_key_version": keyring.current,
    }


# ---------------------------------------------------------------------------
# Restore spot-check
# ---------------------------------------------------------------------------


@dataclass
class RestoreCheck:
    blobs_total: int = 0
    checked: int = 0
    documents_compared: int = 0
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "blobs_total": self.blobs_total,
            "checked": self.checked,
            "documents_compared": self.documents_compared,
            "failed": self.failed[:50],
        }


@dataclass(frozen=True)
class _Candidate:
    household_id: uuid.UUID
    blob_id: uuid.UUID


def restore_check(
    engine: Engine,
    open_plaintext: Callable[[Blob], Iterable[bytes]],
    *,
    sample: int | None = DEFAULT_SAMPLE,
    rng: random.Random | None = None,
) -> RestoreCheck:
    """Decrypt ``sample`` random blobs (all when ``None``) and compare document hashes.

    ``open_plaintext(blob)`` is :func:`app.domain.documents.service.open_plaintext`
    bound to a blob store. A blob fails when its key does not unwrap, its file
    is missing or altered, or the plaintext SHA-256 differs from the
    ``document.sha256`` of a document stored in it.
    """
    report = RestoreCheck()
    candidates: list[_Candidate] = []
    for household_id in household_ids(engine):
        with engine.begin() as conn:
            set_context(conn, household_id)
            ids = conn.execute(
                select(Blob.id).where(Blob.wrapped_dek.is_not(None)).order_by(Blob.id)
            ).scalars()
            candidates.extend(_Candidate(household_id, blob_id) for blob_id in ids)
    report.blobs_total = len(candidates)
    if sample is not None and sample < len(candidates):
        candidates = (rng or random.SystemRandom()).sample(candidates, sample)
    for candidate in candidates:
        with Session(engine) as db, db.begin():
            set_context(db, candidate.household_id)
            blob = db.get(Blob, candidate.blob_id)
            if blob is None:  # deleted while checking
                continue
            expected = set(
                db.execute(select(Document.sha256).where(Document.blob_id == blob.id)).scalars()
            )
            digest = hashlib.sha256()
            try:
                for chunk in open_plaintext(blob):
                    digest.update(chunk)
            except (DecryptionError, BlobStoreError, OSError):
                report.failed.append(f"blob:{blob.id}:undecryptable")
                continue
            report.checked += 1
            for sha in expected:
                report.documents_compared += 1
                if sha != digest.hexdigest():
                    report.failed.append(f"blob:{blob.id}:sha256-mismatch")
    return report

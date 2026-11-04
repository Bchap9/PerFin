# SPDX-License-Identifier: AGPL-3.0-or-later
"""Export and import rules for the API and the jobs (docs/PORTABILITY.md, AUTH.md step-up).

Authorisation (the router's guard): a browser session of an owner or admin
with a step-up verification in the last 10 minutes (AUTH.md "export
everything"); owners and admins see every account, so an export is the
whole household. An archive's outcome and download belong to the user who
requested it: anyone else (another admin, another household) gets ``404
export-not-found`` / ``import-not-found``.

* :func:`request_export` derives the passphrase key (Argon2id) in the
  request when the user chose one, stores it only wrapped by the master key
  on the archive row, and queues ``portability.export``. One export and
  one import may be pending per household (``409``).
* :func:`run_export_job` writes the archive into the blob store
  (``permanent`` area, encrypted at rest) and marks it ``ready`` for
  ``PERFIN_EXPORT_TTL_MINUTES``; ``portability.expire`` shreds it then.
* :func:`open_export_download` is **one-time**: it opens the decrypting
  stream, then destroys the blob's key and deletes the blob row in the same
  transaction (committed before the first byte is sent), and unlinks the
  file once committed. The already-open stream carries on; nothing can open
  the archive again.
* :func:`store_import` streams an upload into the quarantine area; an
  encrypted archive is decrypted on the fly to check the passphrase and the
  archive's integrity before the job is queued. :func:`run_import_job`
  validates and applies it all-or-nothing (savepoint), then destroys the
  upload either way.

Every step is audit logged (``export.request|complete|fail|download|expire``,
``import.request|complete|fail``; ids, counts and error slugs only) and
emits an event (``export.*``, ``import.*``, plus ``account.imported``,
``transaction.imported``, ``budget.imported``, ``document.imported`` after
an import so open pages refresh).
"""

from __future__ import annotations

import itertools
import struct
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

from sqlalchemy import delete, event, insert, select
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.principal import Principal
from app.core import clock
from app.core.archive_crypto import (
    DEFAULT_KDF,
    MAX_KDF,
    ArchiveCryptoError,
    ArchiveDecryptionError,
    ArchiveDecryptor,
    ArchiveEncryptor,
    ArchiveFormatError,
    HeaderReader,
    KdfParams,
    derive_key,
    is_encrypted_archive,
    new_salt,
)
from app.core.audit import audit
from app.core.crypto import DecryptionError, Keyring, column_ad
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.core.settings import Settings
from app.db.queue import enqueue
from app.domain.documents import repo as documents_repo
from app.domain.documents import service as documents_service
from app.domain.documents.blobstore import (
    BlobStore,
    BlobStoreError,
    EncryptingWriter,
    LocalFSBlobStore,
    SealedBlob,
)
from app.domain.documents.models import Blob, BlobArea
from app.domain.portability.codec import ArchiveInvalidError
from app.domain.portability.export import ExportFailedError, write_export
from app.domain.portability.import_ import apply_archive, blob_row, read_archive
from app.domain.portability.models import ArchiveKind, ArchiveState, PortabilityArchive
from app.domain.portability.schemas import (
    EXPIRE_JOB_TYPE,
    EXPORT_JOB_TYPE,
    IMPORT_JOB_TYPE,
    PASSPHRASE_HEADER,
    ArchiveJob,
    ExportOut,
    ExportQueuedOut,
    ExportRequest,
    ImportOut,
    ImportQueuedOut,
)
from app.domain.portability.zipsafe import ZipLimits
from app.ingestion.intake.upload import UploadTooLargeError

log = get_logger(__name__)

#: Argon2id cost for export passphrases (tests lower it).
EXPORT_KDF: KdfParams = DEFAULT_KDF
_ZIP_MAGIC: Final = (b"PK\x03\x04", b"PK\x05\x06")
_KEY_PACK: Final = struct.Struct(">IQ")
_ERROR_DETAIL_MAX: Final = 300


@dataclass(frozen=True)
class PortabilityDeps:
    """What the jobs need besides the database."""

    store: BlobStore
    keyring: Keyring
    export_ttl: timedelta
    import_max_bytes: int
    zip_limits: ZipLimits = field(default_factory=ZipLimits)

    @classmethod
    def from_settings(cls, settings: Settings, keyring: Keyring | None = None) -> PortabilityDeps:
        return cls(
            store=LocalFSBlobStore.from_settings(settings),
            keyring=keyring or Keyring.from_settings(settings),
            export_ttl=timedelta(minutes=settings.export_ttl_minutes),
            import_max_bytes=settings.import_max_bytes,
        )


# ---------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------


def export_not_found() -> ProblemError:
    return ProblemError(404, "export-not-found", "Export not found")


def import_not_found() -> ProblemError:
    return ProblemError(404, "import-not-found", "Import not found")


def session_required() -> ProblemError:
    return ProblemError(
        403, "session-required", "Browser session required", "Use the web app for this."
    )


def _in_progress(kind: ArchiveKind) -> ProblemError:
    return ProblemError(
        409,
        f"{kind.value}-in-progress",
        f"An {kind.value} is already running",
        f"Wait until the household's current {kind.value} has finished.",
    )


def _gone(slug: str, detail: str) -> ProblemError:
    return ProblemError(410, slug, "Export no longer available", detail)


def export_expired() -> ProblemError:
    return _gone("export-expired", "The export expired; request a new one.")


def _bad_archive(slug: str, title: str, detail: str) -> ProblemError:
    return ProblemError(422, slug, title, detail)


def _household(principal: Principal) -> uuid.UUID:
    household_id = principal.household_id
    if household_id is None:  # the router's role guard already refused this
        raise ProblemError(403, "forbidden", "Forbidden", "You are not a member of a household.")
    return household_id


# ---------------------------------------------------------------------------
# Archive rows and blobs
# ---------------------------------------------------------------------------


def _key_ad(archive_id: uuid.UUID) -> bytes:
    return column_ad("portability_archive", "passphrase_key", archive_id)


def seal_passphrase_key(
    keyring: Keyring, archive_id: uuid.UUID, key: bytes, salt: bytes, kdf: KdfParams
) -> bytes:
    """The derived key (with its salt and cost) as a master-key envelope bound to the row."""
    packed = _KEY_PACK.pack(kdf.opslimit, kdf.memlimit) + salt + key
    return keyring.encrypt_bytes(packed, ad=_key_ad(archive_id))


def open_passphrase_key(
    keyring: Keyring, archive: PortabilityArchive
) -> tuple[bytes, bytes, KdfParams]:
    """``(key, salt, kdf)`` sealed by :func:`seal_passphrase_key`."""
    if archive.passphrase_key is None:
        raise DecryptionError
    packed = keyring.decrypt_bytes(archive.passphrase_key, ad=_key_ad(archive.id))
    opslimit, memlimit = _KEY_PACK.unpack_from(packed)
    salt = packed[_KEY_PACK.size : _KEY_PACK.size + 16]
    key = packed[_KEY_PACK.size + 16 :]
    return key, salt, KdfParams(opslimit, memlimit)


def _pending(db: Session, household_id: uuid.UUID, kind: ArchiveKind) -> bool:
    return (
        db.execute(
            select(PortabilityArchive.id)
            .where(
                PortabilityArchive.household_id == household_id,
                PortabilityArchive.kind == kind,
                PortabilityArchive.state == ArchiveState.PENDING,
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _lock_archive(
    db: Session, household_id: uuid.UUID, archive_id: uuid.UUID, kind: ArchiveKind
) -> PortabilityArchive | None:
    return db.execute(
        select(PortabilityArchive)
        .where(
            PortabilityArchive.household_id == household_id,
            PortabilityArchive.id == archive_id,
            PortabilityArchive.kind == kind,
        )
        .with_for_update()
    ).scalar_one_or_none()


def _own_archive(
    db: Session, principal: Principal, job_id: uuid.UUID, kind: ArchiveKind, *, lock: bool = False
) -> PortabilityArchive:
    household_id = _household(principal)
    query = select(PortabilityArchive).where(
        PortabilityArchive.household_id == household_id,
        PortabilityArchive.job_id == job_id,
        PortabilityArchive.kind == kind,
        PortabilityArchive.requested_by == principal.user_id,
    )
    archive = db.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if archive is None:
        raise export_not_found() if kind is ArchiveKind.EXPORT else import_not_found()
    return archive


def _after(
    session: Session, *, on_commit: Callable[[], None], on_rollback: Callable[[], None]
) -> None:
    """Run exactly one callback when the session's transaction ends (file cleanup)."""
    done = [False]

    def run(callback: Callable[[], None]) -> None:
        if done[0]:
            return
        done[0] = True
        try:
            callback()
        except OSError as exc:
            # A left-over file is ciphertext whose key is gone or was never stored.
            log.warning("archive file cleanup failed", extra={"error_class": type(exc).__name__})

    event.listen(session, "after_commit", lambda _: run(on_commit), once=True)
    event.listen(session, "after_rollback", lambda _: run(on_rollback), once=True)


def _delete_files(
    store: BlobStore, keys: Iterable[str], areas: Iterable[BlobArea]
) -> Callable[[], None]:
    """Delete ``keys`` (read when called: a list may still grow) from ``areas``."""
    areas = list(areas)

    def run() -> None:
        for key in keys:
            for area in areas:
                store.delete(area, key)

    return run


def _discard_blob(session: Session, store: BlobStore, archive: PortabilityArchive) -> None:
    """Destroy the archive's blob: key first, then the row; the file once committed."""
    if archive.blob_id is None:
        return
    blob_id = archive.blob_id
    blob = documents_repo.get_blob(session, archive.household_id, blob_id)
    documents_repo.shred_blob_key(session, archive.household_id, blob_id)
    archive.blob_id = None
    session.flush()
    session.execute(
        delete(Blob).where(Blob.household_id == archive.household_id, Blob.id == blob_id)
    )
    if blob is not None:
        _after(
            session,
            on_commit=_delete_files(store, [blob.key], BlobArea),
            on_rollback=lambda: None,
        )


def _insert_blob(session: Session, household_id: uuid.UUID, sealed: SealedBlob) -> None:
    session.execute(insert(Blob), [blob_row(household_id, sealed)])


def _record(
    db: Session,
    archive: PortabilityArchive,
    action: str,
    *,
    after: dict[str, Any],
    user_id: uuid.UUID | None,
    request: HTTPConnection | None = None,
) -> None:
    audit(
        db,
        action=action,
        target_type="portability_archive",
        target_id=archive.id,
        after={"job_id": archive.job_id, **after},
        household_id=archive.household_id,
        user_id=user_id,
        request=request,
    )
    emit(
        db,
        archive.household_id,
        action,
        {"job_id": str(archive.job_id), "state": str(archive.state)},
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def request_export(
    db: Session,
    principal: Principal,
    body: ExportRequest,
    *,
    keyring: Keyring,
    request: HTTPConnection | None,
    kdf: KdfParams | None = None,
) -> ExportQueuedOut:
    household_id = _household(principal)
    if _pending(db, household_id, ArchiveKind.EXPORT):
        raise _in_progress(ArchiveKind.EXPORT)
    archive_id = uuid7()
    sealed_key: bytes | None = None
    if body.passphrase is not None:
        cost = kdf or EXPORT_KDF
        salt = new_salt()
        key = derive_key(body.passphrase.get_secret_value(), salt, cost)
        sealed_key = seal_passphrase_key(keyring, archive_id, key, salt, cost)
    job_id = enqueue(
        db,
        EXPORT_JOB_TYPE,
        ArchiveJob(archive_id=archive_id),
        household_id=household_id,
        max_attempts=3,
    )
    archive = PortabilityArchive(
        id=archive_id,
        household_id=household_id,
        kind=ArchiveKind.EXPORT,
        state=ArchiveState.PENDING,
        job_id=job_id,
        requested_by=principal.user_id,
        passphrase_protected=sealed_key is not None,
        passphrase_key=sealed_key,
    )
    db.add(archive)
    db.flush()
    _record(
        db,
        archive,
        "export.request",
        after={"passphrase_protected": archive.passphrase_protected},
        user_id=principal.user_id,
        request=request,
    )
    return ExportQueuedOut(
        job_id=job_id, state=archive.state, passphrase_protected=archive.passphrase_protected
    )


def export_out(archive: PortabilityArchive) -> ExportOut:
    counts = archive.summary.get("counts", {}) if isinstance(archive.summary, dict) else {}
    return ExportOut(
        job_id=archive.job_id,
        state=archive.state,
        passphrase_protected=archive.passphrase_protected,
        size=archive.size,
        sha256=archive.sha256,
        created_at=archive.created_at,
        finished_at=archive.finished_at,
        expires_at=archive.expires_at,
        downloaded_at=archive.downloaded_at,
        error=archive.error,
        counts={str(k): int(v) for k, v in counts.items()},
    )


def get_export(db: Session, principal: Principal, job_id: uuid.UUID) -> ExportOut:
    return export_out(_own_archive(db, principal, job_id, ArchiveKind.EXPORT))


def _fail(
    session: Session,
    archive: PortabilityArchive,
    action: str,
    slug: str,
    detail: str | None,
    store: BlobStore,
) -> None:
    _discard_blob(session, store, archive)
    archive.state = ArchiveState.FAILED
    archive.error = slug
    archive.passphrase_key = None
    archive.finished_at = clock.utcnow()
    archive.summary = {"error_detail": (detail or "")[:_ERROR_DETAIL_MAX]} if detail else {}
    session.flush()
    _record(session, archive, action, after={"error": slug}, user_id=archive.requested_by)


def run_export_job(
    session: Session,
    household_id: uuid.UUID,
    archive_id: uuid.UUID,
    deps: PortabilityDeps,
    *,
    check_cancelled: Callable[[], None] = lambda: None,
) -> ArchiveState | None:
    """Build an export (``portability.export``); ``None`` when there is nothing to do."""
    archive = _lock_archive(session, household_id, archive_id, ArchiveKind.EXPORT)
    if archive is None or archive.state is not ArchiveState.PENDING:
        return None
    encryptor: ArchiveEncryptor | None = None
    if archive.passphrase_protected:
        try:
            key, salt, kdf = open_passphrase_key(deps.keyring, archive)
        except (DecryptionError, ArchiveCryptoError):
            _fail(session, archive, "export.fail", "passphrase-key-unavailable", None, deps.store)
            return archive.state
        encryptor = ArchiveEncryptor(key, salt=salt, kdf=kdf)
    now = clock.utcnow()
    try:
        result = write_export(
            session,
            household_id,
            store=deps.store,
            keyring=deps.keyring,
            blob_id=uuid7(),
            created=now,
            encryptor=encryptor,
            check_cancelled=check_cancelled,
        )
    except ExportFailedError as exc:
        _fail(session, archive, "export.fail", exc.slug, None, deps.store)
        return archive.state
    sealed = result.sealed
    _after(
        session,
        on_commit=lambda: None,
        on_rollback=_delete_files(deps.store, [sealed.key], [BlobArea.PERMANENT]),
    )
    _insert_blob(session, household_id, sealed)
    archive.blob_id = sealed.blob_id
    archive.state = ArchiveState.READY
    archive.size = result.size
    archive.sha256 = result.sha256
    archive.passphrase_key = None
    archive.finished_at = clock.utcnow()
    archive.expires_at = archive.finished_at + deps.export_ttl
    archive.summary = {"counts": result.counts}
    session.flush()
    enqueue(
        session,
        EXPIRE_JOB_TYPE,
        ArchiveJob(archive_id=archive.id),
        household_id=household_id,
        run_after=archive.expires_at,
    )
    _record(
        session,
        archive,
        "export.complete",
        after={"size": result.size, "sha256": result.sha256, "counts": result.counts},
        user_id=archive.requested_by,
    )
    return archive.state


def run_expire_job(
    session: Session, household_id: uuid.UUID, archive_id: uuid.UUID, deps: PortabilityDeps
) -> ArchiveState | None:
    """``portability.expire``: destroy a ready export whose time is up."""
    archive = _lock_archive(session, household_id, archive_id, ArchiveKind.EXPORT)
    if archive is None or archive.state is not ArchiveState.READY:
        return None
    now = clock.utcnow()
    if archive.expires_at is not None and archive.expires_at > now:
        enqueue(
            session,
            EXPIRE_JOB_TYPE,
            ArchiveJob(archive_id=archive.id),
            household_id=household_id,
            run_after=archive.expires_at,
        )
        return archive.state
    _expire(session, archive, deps.store)
    return archive.state


def _expire(session: Session, archive: PortabilityArchive, store: BlobStore) -> None:
    _discard_blob(session, store, archive)
    archive.state = ArchiveState.EXPIRED
    session.flush()
    _record(session, archive, "export.expire", after={}, user_id=None)


def _due(archive: PortabilityArchive) -> bool:
    return (
        archive.state is ArchiveState.READY
        and archive.expires_at is not None
        and archive.expires_at <= clock.utcnow()
    )


def expire_if_due(
    db: Session, principal: Principal, job_id: uuid.UUID, *, store: BlobStore
) -> bool:
    """Destroy the caller's export if its time is up (the caller commits when True).

    Run before :func:`open_export_download`, whose ``410`` would otherwise
    roll the destruction back with the request.
    """
    archive = _own_archive(db, principal, job_id, ArchiveKind.EXPORT, lock=True)
    if not _due(archive):
        return False
    _expire(db, archive, store)
    return True


@dataclass(frozen=True)
class ExportDownload:
    filename: str
    media_type: str
    size: int
    chunks: Iterator[bytes]


def open_export_download(
    db: Session,
    principal: Principal,
    job_id: uuid.UUID,
    *,
    store: BlobStore,
    keyring: Keyring,
    request: HTTPConnection | None,
) -> ExportDownload:
    """Open the one-time download (module doc); the caller commits before streaming."""
    archive = _own_archive(db, principal, job_id, ArchiveKind.EXPORT, lock=True)
    if _due(archive):
        raise export_expired()
    if archive.state is ArchiveState.DOWNLOADED:
        raise _gone("export-already-downloaded", "An export can be downloaded only once.")
    if archive.state is ArchiveState.EXPIRED:
        raise export_expired()
    if archive.state is not ArchiveState.READY or archive.blob_id is None:
        raise ProblemError(
            409, "export-not-ready", "Export not ready", "The export is not ready to download."
        )
    blob = documents_repo.get_blob(db, archive.household_id, archive.blob_id)
    if blob is None:
        raise ProblemError(500, "export-unreadable", "Export unreadable")
    chunks = documents_service.open_plaintext(store, keyring, blob)
    try:
        first = next(chunks, b"")
    except (DecryptionError, BlobStoreError, OSError) as exc:
        log.error("export download failed", extra={"error_class": type(exc).__name__})
        raise ProblemError(500, "export-unreadable", "Export unreadable") from None
    # One-time: the stream above already holds the data key and the open file;
    # from here on the stored copy can never be opened again.
    _discard_blob(db, store, archive)
    archive.state = ArchiveState.DOWNLOADED
    archive.downloaded_at = clock.utcnow()
    db.flush()
    _record(
        db,
        archive,
        "export.download",
        after={"size": archive.size},
        user_id=principal.user_id,
        request=request,
    )
    stamp = archive.created_at.strftime("%Y%m%d-%H%M%S")
    if archive.passphrase_protected:
        filename, media = f"perfin-export-{stamp}.pfarch", "application/octet-stream"
    else:
        filename, media = f"perfin-export-{stamp}.zip", "application/zip"
    return ExportDownload(
        filename=filename,
        media_type=media,
        size=blob.size,
        chunks=itertools.chain((first,), chunks) if first else chunks,
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def check_import_allowed(db: Session, principal: Principal) -> None:
    if _pending(db, _household(principal), ArchiveKind.IMPORT):
        raise _in_progress(ArchiveKind.IMPORT)


@dataclass(frozen=True)
class StoredImport:
    sealed: SealedBlob
    passphrase_key: tuple[bytes, bytes, KdfParams] | None

    def __repr__(self) -> str:
        return f"StoredImport(blob_id={self.sealed.blob_id!s})"


def unsupported_archive() -> ProblemError:
    return ProblemError(
        415,
        "unsupported-archive",
        "Not a PerFin export",
        "Upload the .zip or encrypted .pfarch file a PerFin export produced.",
    )


class _Verifier:
    """Checks an encrypted upload as it streams past (passphrase and integrity)."""

    def __init__(self, passphrase: str | None, max_kdf: KdfParams) -> None:
        self.passphrase = passphrase
        self.reader = HeaderReader(max_kdf)
        self.decryptor: ArchiveDecryptor | None = None
        self.key: tuple[bytes, bytes, KdfParams] | None = None
        self.head = bytearray()

    def feed(self, data: bytes) -> None:
        rest = self.reader.feed(data)
        header = self.reader.header
        if header is None:
            return
        if self.decryptor is None:
            if not self.passphrase:
                raise _bad_archive(
                    "passphrase-required",
                    "Passphrase required",
                    "This archive is encrypted: send its passphrase in the "
                    f"{PASSPHRASE_HEADER} header.",
                )
            key = derive_key(self.passphrase, header.salt, header.kdf)
            self.decryptor = ArchiveDecryptor(key, header)
            self.key = (key, header.salt, header.kdf)
        self._plain(self.decryptor.update(rest))

    def _plain(self, plain: bytes) -> None:
        if len(self.head) < 4:
            self.head += plain[: 4 - len(self.head)]
            if len(self.head) >= 4 and bytes(self.head) not in _ZIP_MAGIC:
                raise unsupported_archive()

    def finish(self) -> tuple[bytes, bytes, KdfParams]:
        if self.decryptor is None or self.key is None:
            raise _bad_archive("archive-damaged", "Damaged archive", "The archive is truncated.")
        self._plain(self.decryptor.finish())
        if len(self.head) < 4:
            raise unsupported_archive()
        return self.key


#: Bytes needed to tell an encrypted archive from anything else.
HEADER_PROBE: Final = 7


def _archive_kind(head: bytes, *, final: bool) -> str | None:
    """``"zip"``, ``"encrypted"``, or None while undecided (``415`` when it cannot be)."""
    if head[:4] in _ZIP_MAGIC:
        return "zip"
    if len(head) >= HEADER_PROBE:
        if is_encrypted_archive(head):
            return "encrypted"
        raise unsupported_archive()
    if final:
        raise unsupported_archive()
    return None


def store_import(
    chunks: Iterable[bytes],
    *,
    store: BlobStore,
    keyring: Keyring,
    max_bytes: int,
    passphrase: str | None,
    max_kdf: KdfParams = MAX_KDF,
) -> StoredImport:
    """Stream an uploaded archive into the quarantine area (module doc)."""
    writer = EncryptingWriter(store, keyring, uuid7(), area=BlobArea.QUARANTINE)
    verifier: _Verifier | None = None
    kind: str | None = None  # "zip" or "encrypted" once the first bytes decided it
    head = bytearray()
    try:
        for chunk in chunks:
            if not chunk:
                continue
            if writer.size + len(chunk) > max_bytes:
                raise UploadTooLargeError(max_bytes)
            writer.write(chunk)
            if kind is None:
                head += chunk
                kind = _archive_kind(bytes(head), final=False)
                if kind == "encrypted":
                    verifier = _Verifier(passphrase, max_kdf)
                    verifier.feed(bytes(head))
                continue
            if verifier is not None:
                verifier.feed(chunk)
        if kind is None and _archive_kind(bytes(head), final=True) is None:
            raise unsupported_archive()
        key = verifier.finish() if verifier is not None else None
        sealed = writer.finish()
    except ArchiveFormatError:
        writer.abort()
        raise unsupported_archive() from None
    except ArchiveDecryptionError:
        writer.abort()
        raise _bad_archive(
            "archive-wrong-passphrase",
            "Wrong passphrase or damaged archive",
            "The archive could not be decrypted with this passphrase.",
        ) from None
    except BaseException:
        writer.abort()
        raise
    return StoredImport(sealed=sealed, passphrase_key=key)


def record_import(
    session: Session,
    principal: Principal,
    stored: StoredImport,
    *,
    keyring: Keyring,
    request: HTTPConnection | None,
) -> ImportQueuedOut:
    household_id = _household(principal)
    check_import_allowed(session, principal)
    archive_id = uuid7()
    sealed_key = (
        seal_passphrase_key(keyring, archive_id, *stored.passphrase_key)
        if stored.passphrase_key is not None
        else None
    )
    _insert_blob(session, household_id, stored.sealed)
    job_id = enqueue(
        session,
        IMPORT_JOB_TYPE,
        ArchiveJob(archive_id=archive_id),
        household_id=household_id,
        max_attempts=3,
    )
    archive = PortabilityArchive(
        id=archive_id,
        household_id=household_id,
        kind=ArchiveKind.IMPORT,
        state=ArchiveState.PENDING,
        job_id=job_id,
        requested_by=principal.user_id,
        blob_id=stored.sealed.blob_id,
        passphrase_protected=sealed_key is not None,
        passphrase_key=sealed_key,
        size=stored.sealed.size,
    )
    session.add(archive)
    session.flush()
    _record(
        session,
        archive,
        "import.request",
        after={"size": stored.sealed.size, "passphrase_protected": archive.passphrase_protected},
        user_id=principal.user_id,
        request=request,
    )
    return ImportQueuedOut(
        job_id=job_id, state=archive.state, passphrase_protected=archive.passphrase_protected
    )


def import_out(archive: PortabilityArchive) -> ImportOut:
    summary = dict(archive.summary) if isinstance(archive.summary, dict) else {}
    detail = summary.pop("error_detail", None)
    return ImportOut(
        job_id=archive.job_id,
        state=archive.state,
        passphrase_protected=archive.passphrase_protected,
        created_at=archive.created_at,
        finished_at=archive.finished_at,
        error=archive.error,
        error_detail=str(detail) if detail else None,
        summary=summary,
    )


def get_import(db: Session, principal: Principal, job_id: uuid.UUID) -> ImportOut:
    return import_out(_own_archive(db, principal, job_id, ArchiveKind.IMPORT))


def _load_upload(session: Session, archive: PortabilityArchive, deps: PortabilityDeps) -> bytes:
    """The uploaded archive's zip bytes (decrypted twice: storage, then passphrase)."""
    if archive.blob_id is None:
        raise ArchiveInvalidError("archive-missing", "the uploaded archive is gone")
    blob = documents_repo.get_blob(session, archive.household_id, archive.blob_id)
    if blob is None:
        raise ArchiveInvalidError("archive-missing", "the uploaded archive is gone")
    if blob.size > deps.import_max_bytes:
        raise ArchiveInvalidError("archive-too-large", "the archive exceeds the import limit")
    stream = documents_service.open_plaintext(deps.store, deps.keyring, blob)
    out = bytearray()
    if archive.passphrase_protected:
        key, salt, kdf = open_passphrase_key(deps.keyring, archive)
        reader = HeaderReader(MAX_KDF)
        decryptor: ArchiveDecryptor | None = None
        for chunk in stream:
            rest = reader.feed(chunk)
            if reader.header is None:
                continue
            if decryptor is None:
                if reader.header.salt != salt or reader.header.kdf != kdf:
                    raise ArchiveDecryptionError("the archive header changed")
                decryptor = ArchiveDecryptor(key, reader.header)
            out += decryptor.update(rest)
        if decryptor is None:
            raise ArchiveDecryptionError("the archive is truncated")
        out += decryptor.finish()
    else:
        for chunk in stream:
            out += chunk
    return bytes(out)


def run_import_job(
    session: Session,
    household_id: uuid.UUID,
    archive_id: uuid.UUID,
    deps: PortabilityDeps,
    *,
    check_cancelled: Callable[[], None] = lambda: None,
) -> ArchiveState | None:
    """Apply an uploaded archive (``portability.import``); ``None`` when nothing to do."""
    archive = _lock_archive(session, household_id, archive_id, ArchiveKind.IMPORT)
    if archive is None or archive.state is not ArchiveState.PENDING:
        return None
    written: list[str] = []
    cleanup = _delete_files(deps.store, written, [BlobArea.PERMANENT])
    try:
        data = _load_upload(session, archive, deps)
        parsed = read_archive(data, limits=deps.zip_limits)
        del data
        with session.begin_nested():
            summary = apply_archive(
                session,
                household_id,
                parsed,
                store=deps.store,
                keyring=deps.keyring,
                written_keys=written,
                check_cancelled=check_cancelled,
            )
    except ArchiveInvalidError as exc:
        cleanup()
        _fail(session, archive, "import.fail", exc.slug, exc.detail, deps.store)
        return archive.state
    except (DecryptionError, ArchiveCryptoError, BlobStoreError):
        cleanup()
        _fail(session, archive, "import.fail", "archive-unreadable", None, deps.store)
        return archive.state
    except (IntegrityError, DataError):
        cleanup()
        _fail(
            session,
            archive,
            "import.fail",
            "archive-inconsistent",
            "the archive's rows break a database rule (references, uniqueness or a check)",
            deps.store,
        )
        return archive.state
    except BaseException:
        cleanup()
        raise
    _after(session, on_commit=lambda: None, on_rollback=cleanup)
    _discard_blob(session, deps.store, archive)
    archive.state = ArchiveState.IMPORTED
    archive.passphrase_key = None
    archive.finished_at = clock.utcnow()
    archive.summary = summary
    session.flush()
    _record(
        session,
        archive,
        "import.complete",
        after={"created": summary["created"], "merged": summary["merged"]},
        user_id=archive.requested_by,
    )
    created = summary["created"]
    for type_, entities in (
        ("account.imported", ("accounts", "balance_snapshots")),
        ("transaction.imported", ("transactions",)),
        ("budget.imported", ("budgets", "goals")),
        ("document.imported", ("documents",)),
    ):
        count = sum(int(created.get(name, 0)) for name in entities)
        if count:
            emit(session, household_id, type_, {"job_id": str(archive.job_id), "count": count})
    return archive.state

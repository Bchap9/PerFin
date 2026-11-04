# SPDX-License-Identifier: AGPL-3.0-or-later
"""Encrypted blob storage for documents (ADR-0003, SECURITY.md G1, G5, C3).

Two layers:

* :class:`BlobStore` moves opaque **ciphertext** bytes around. It never sees
  a plaintext byte or a key. :class:`LocalFSBlobStore` keeps objects on the
  local filesystem; :class:`S3BlobStore` is the interface for an
  S3-compatible store and refuses to work until it is implemented (ROADMAP
  parking lot).
* :class:`EncryptingWriter` / :func:`decrypt_stream` encrypt and decrypt a
  document as a stream with a fresh per-object data key (DEK), so a 50 MB
  upload is never held in memory.

**Payload format.** libsodium ``crypto_secretstream_xchacha20poly1305``
(XChaCha20-Poly1305 per chunk, with a ratcheting state) keyed by the DEK.
The plaintext is cut into :data:`CHUNK_BYTES` chunks; each is pushed with the
blob's associated data, the last with ``TAG_FINAL``, so reordering,
truncation and appending are all detected. The secretstream header (24
random bytes) is stored in ``blob.nonce``; the file holds only the encrypted
chunks. The DEK is wrapped with the master key through
:meth:`app.core.crypto.Keyring.new_data_key` with :func:`blob_ad` (the blob
id) as associated data, so a wrapped key cannot be moved to another blob row,
and stored as ``blob.wrapped_dek``/``wrap_nonce``/``key_version``. Rotation
rewraps the DEK and never touches the file.

**Local layout.** ``<root>/<2 hex>/<2 hex>/<uuid4>`` per area (quarantine and
permanent are separate roots, separate volumes in ``docker-compose.yml``).
Keys are random UUIDs, never derived from a file name (C3). Directories are
``0700``, files ``0600``. Writes go to a temporary file in the target
directory that is ``fsync``-ed and renamed into place (atomic); a crash
leaves at most a ``.tmp-*`` file, never a half-written object under a real
key. :meth:`LocalFSBlobStore.copy` promotes ciphertext from quarantine to
permanent byte for byte (hard link when both areas share a filesystem): the
object is never re-encrypted, and the quarantine copy is deleted by the
caller once the database says the object lives in ``permanent``.

**Deletion** (G5): the service first destroys the wrapped DEK in the
database, then unlinks the file. Whatever survives on the medium (a
snapshot, a copy left by a crash) is ciphertext whose key no longer exists.
"""

from __future__ import annotations

import abc
import contextlib
import errno
import hashlib
import os
import re
import secrets
import stat
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final

from nacl import bindings
from nacl.exceptions import CryptoError

from app.core.crypto import DecryptionError, Keyring, WrappedKey, column_ad
from app.domain.documents.models import BlobArea

if TYPE_CHECKING:
    from app.core.settings import Settings

#: Plaintext bytes per secretstream chunk.
CHUNK_BYTES: Final = 64 * 1024
_ABYTES: Final = bindings.crypto_secretstream_xchacha20poly1305_ABYTES  # 17
CIPHER_CHUNK_BYTES: Final = CHUNK_BYTES + _ABYTES
HEADER_BYTES: Final = bindings.crypto_secretstream_xchacha20poly1305_HEADERBYTES  # 24
_TAG_MESSAGE: Final = bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE
_TAG_FINAL: Final = bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL

#: ``blob.enc_alg`` for this format.
ENC_ALG: Final = "xchacha20poly1305-secretstream-64k"
_CHUNK_AD_CONTEXT: Final = b"perfin/blob/v1\x00"

_KEY_RE: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_DIR_MODE: Final = 0o700
_FILE_MODE: Final = 0o600
_COPY_BUFFER: Final = 1024 * 1024


class BlobStoreError(Exception):
    """A blob could not be stored or read (no paths or key material in the message)."""


class BlobNotFoundError(BlobStoreError):
    pass


class BlobStoreNotImplementedError(BlobStoreError):
    """The configured blob store backend is not implemented yet."""


def blob_ad(blob_id: uuid.UUID) -> bytes:
    """Associated data that binds a wrapped DEK and every chunk to ``blob.id``."""
    return column_ad("blob", "data", blob_id)


def _chunk_ad(blob_id: uuid.UUID) -> bytes:
    return _CHUNK_AD_CONTEXT + hashlib.sha256(blob_ad(blob_id)).digest()


def new_key() -> str:
    """A fresh random object key (UUIDv4; never derived from input)."""
    return str(uuid.uuid4())


def check_key(key: str) -> str:
    """``key`` if it is a canonical UUIDv4 string, else :class:`BlobStoreError`."""
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise BlobStoreError("invalid blob key")
    return key


# ---------------------------------------------------------------------------
# Ciphertext stores
# ---------------------------------------------------------------------------


class BlobSink(abc.ABC):
    """An object being written; nothing is visible under its key before :meth:`commit`."""

    @abc.abstractmethod
    def write(self, data: bytes) -> None: ...

    @abc.abstractmethod
    def commit(self) -> None: ...

    @abc.abstractmethod
    def abort(self) -> None: ...


class BlobStore(abc.ABC):
    """Ciphertext object storage with a quarantine and a permanent area."""

    #: Stored in ``blob.store``.
    name: str

    @abc.abstractmethod
    def create(self, area: BlobArea, key: str) -> BlobSink:
        """Start writing a new object (an existing key is an error)."""

    @abc.abstractmethod
    def open(self, area: BlobArea, key: str) -> BinaryIO:
        """Read an object's ciphertext; missing is :class:`BlobNotFoundError`."""

    @abc.abstractmethod
    def copy(self, key: str, source: BlobArea, target: BlobArea) -> None:
        """Copy the ciphertext of ``key`` to another area unchanged (idempotent)."""

    @abc.abstractmethod
    def delete(self, area: BlobArea, key: str) -> bool:
        """Remove an object; ``False`` if it was not there."""

    @abc.abstractmethod
    def exists(self, area: BlobArea, key: str) -> bool: ...


class _LocalSink(BlobSink):
    def __init__(self, target: Path) -> None:
        self._target = target
        _private_dir(target.parent)
        name = f".tmp-{secrets.token_hex(8)}"
        self._tmp = target.parent / name
        fd = os.open(self._tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, _FILE_MODE)
        self._file: BinaryIO | None = os.fdopen(fd, "wb")
        self._done = False

    def write(self, data: bytes) -> None:
        if self._file is None:
            raise BlobStoreError("blob sink is closed")
        self._file.write(data)

    def commit(self) -> None:
        if self._file is None or self._done:
            raise BlobStoreError("blob sink is closed")
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            self._file = None
            if self._target.exists():
                raise BlobStoreError("blob key already exists")
            os.replace(self._tmp, self._target)
            _fsync_dir(self._target.parent)
        except BaseException:
            self.abort()
            raise
        self._done = True

    def abort(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None
        with contextlib.suppress(FileNotFoundError):
            self._tmp.unlink()
        self._done = True


def _private_dir(path: Path) -> None:
    """Create ``path`` (and missing parents) as ``0700`` directories."""
    if path.is_dir():
        return
    _private_dir(path.parent)
    try:
        path.mkdir(mode=_DIR_MODE)
    except FileExistsError:
        return
    os.chmod(path, _DIR_MODE)  # mkdir's mode is filtered by the umask


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:  # pragma: no cover - platforms without directory fds
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class LocalFSBlobStore(BlobStore):
    """Objects as files under one root directory per area."""

    name = "local"

    def __init__(self, permanent_root: Path, quarantine_root: Path) -> None:
        self._roots = {
            BlobArea.PERMANENT: Path(permanent_root),
            BlobArea.QUARANTINE: Path(quarantine_root),
        }

    @classmethod
    def from_settings(cls, settings: Settings) -> LocalFSBlobStore:
        return cls(settings.documents_dir, settings.quarantine_dir)

    def path(self, area: BlobArea, key: str) -> Path:
        """Where ``key`` lives in ``area`` (two-level fan-out on the key's hex)."""
        check_key(key)
        root = self._roots[BlobArea(area)]
        return root / key[0:2] / key[2:4] / key

    def create(self, area: BlobArea, key: str) -> BlobSink:
        return _LocalSink(self.path(area, key))

    def open(self, area: BlobArea, key: str) -> BinaryIO:
        path = self.path(area, key)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise BlobNotFoundError("blob not found") from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise BlobStoreError("blob path is a symlink") from None
            raise
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise BlobStoreError("blob is not a regular file")
        return os.fdopen(fd, "rb")

    def copy(self, key: str, source: BlobArea, target: BlobArea) -> None:
        source_path = self.path(source, key)
        target_path = self.path(target, key)
        if target_path.exists():
            return
        _private_dir(target_path.parent)
        try:
            os.link(source_path, target_path, follow_symlinks=False)
            _fsync_dir(target_path.parent)
            return
        except FileNotFoundError:
            raise BlobNotFoundError("blob not found") from None
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP):
                raise
        # Different filesystems (separate volumes): copy the ciphertext bytes.
        sink = self.create(target, key)
        try:
            with self.open(source, key) as src:
                while chunk := src.read(_COPY_BUFFER):
                    sink.write(chunk)
        except BaseException:
            sink.abort()
            raise
        sink.commit()

    def delete(self, area: BlobArea, key: str) -> bool:
        try:
            self.path(area, key).unlink()
        except FileNotFoundError:
            return False
        return True

    def exists(self, area: BlobArea, key: str) -> bool:
        return self.path(area, key).is_file()


class S3BlobStore(BlobStore):
    """S3-compatible object storage: interface only (docs/ROADMAP.md parking lot)."""

    name = "s3"

    def __init__(self, *_: object, **__: object) -> None:
        raise BlobStoreNotImplementedError(
            "the S3-compatible blob store is not implemented yet; use the local store"
        )

    def create(self, area: BlobArea, key: str) -> BlobSink:  # pragma: no cover
        raise BlobStoreNotImplementedError("not implemented")

    def open(self, area: BlobArea, key: str) -> BinaryIO:  # pragma: no cover
        raise BlobStoreNotImplementedError("not implemented")

    def copy(self, key: str, source: BlobArea, target: BlobArea) -> None:  # pragma: no cover
        raise BlobStoreNotImplementedError("not implemented")

    def delete(self, area: BlobArea, key: str) -> bool:  # pragma: no cover
        raise BlobStoreNotImplementedError("not implemented")

    def exists(self, area: BlobArea, key: str) -> bool:  # pragma: no cover
        raise BlobStoreNotImplementedError("not implemented")


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SealedBlob:
    """What the ``blob`` row records about an object written by :class:`EncryptingWriter`."""

    blob_id: uuid.UUID
    store: str
    key: str
    area: BlobArea
    size: int
    stored_size: int
    header: bytes
    wrapped: WrappedKey
    enc_alg: str = ENC_ALG

    def __repr__(self) -> str:
        return f"SealedBlob(blob_id={self.blob_id!s}, size={self.size})"


class EncryptingWriter:
    """Encrypt a stream of plaintext into a new object of ``store``.

    ``write`` as often as needed, then :meth:`finish` (or :meth:`abort`).
    Nothing exists under the object key until ``finish`` returns.
    """

    def __init__(
        self,
        store: BlobStore,
        keyring: Keyring,
        blob_id: uuid.UUID,
        *,
        area: BlobArea = BlobArea.QUARANTINE,
    ) -> None:
        self.blob_id = blob_id
        self._store = store
        self._area = area
        self._key = new_key()
        self._ad = _chunk_ad(blob_id)
        dek, self._wrapped = keyring.new_data_key(ad=blob_ad(blob_id))
        self._state = bindings.crypto_secretstream_xchacha20poly1305_state()
        self._header: bytes = bindings.crypto_secretstream_xchacha20poly1305_init_push(
            self._state, dek
        )
        del dek
        self._sink = store.create(area, self._key)
        self._buffer = bytearray()
        self._size = 0
        self._stored = 0
        self._closed = False

    @property
    def size(self) -> int:
        """Plaintext bytes written so far."""
        return self._size

    def _push(self, chunk: bytes, tag: int) -> None:
        sealed: bytes = bindings.crypto_secretstream_xchacha20poly1305_push(
            self._state, chunk, self._ad, tag
        )
        self._sink.write(sealed)
        self._stored += len(sealed)

    def write(self, data: bytes) -> None:
        if self._closed:
            raise BlobStoreError("writer is closed")
        self._size += len(data)
        self._buffer += data
        while len(self._buffer) > CHUNK_BYTES:
            # Keep at least one byte buffered so the final chunk is never a
            # zero-length message after a full one (simpler reader).
            chunk = bytes(self._buffer[:CHUNK_BYTES])
            del self._buffer[:CHUNK_BYTES]
            self._push(chunk, _TAG_MESSAGE)

    def finish(self) -> SealedBlob:
        if self._closed:
            raise BlobStoreError("writer is closed")
        try:
            self._push(bytes(self._buffer), _TAG_FINAL)
            self._buffer.clear()
            self._sink.commit()
        except BaseException:
            self.abort()
            raise
        self._closed = True
        return SealedBlob(
            blob_id=self.blob_id,
            store=self._store.name,
            key=self._key,
            area=self._area,
            size=self._size,
            stored_size=self._stored,
            header=self._header,
            wrapped=self._wrapped,
        )

    def abort(self) -> None:
        self._buffer.clear()
        self._sink.abort()
        self._closed = True

    def __enter__(self) -> EncryptingWriter:
        return self

    def __exit__(self, exc_type: object, *_: object) -> None:
        if not self._closed:
            self.abort()


def decrypt_stream(
    store: BlobStore,
    keyring: Keyring,
    *,
    blob_id: uuid.UUID,
    area: BlobArea,
    key: str,
    header: bytes,
    wrapped: WrappedKey | None,
    size: int | None = None,
) -> Iterator[bytes]:
    """Yield the plaintext of an object chunk by chunk, authenticating every chunk.

    Raises :class:`app.core.crypto.DecryptionError` for a shredded key
    (``wrapped`` is None), any tampering, truncation, trailing data or a
    plaintext size other than ``size``.
    """
    if wrapped is None or len(header) != HEADER_BYTES:
        raise DecryptionError
    dek = keyring.unwrap_data_key(wrapped, ad=blob_ad(blob_id))
    state = bindings.crypto_secretstream_xchacha20poly1305_state()
    try:
        bindings.crypto_secretstream_xchacha20poly1305_init_pull(state, header, dek)
    except CryptoError:
        raise DecryptionError from None
    del dek
    ad = _chunk_ad(blob_id)
    total = 0
    with store.open(area, key) as handle:
        while True:
            sealed = handle.read(CIPHER_CHUNK_BYTES)
            if len(sealed) < _ABYTES:
                raise DecryptionError  # truncated: no final chunk
            try:
                plain, tag = bindings.crypto_secretstream_xchacha20poly1305_pull(state, sealed, ad)
            except CryptoError:
                raise DecryptionError from None
            total += len(plain)
            if tag == _TAG_FINAL:
                if handle.read(1):
                    raise DecryptionError  # data after the final chunk
                if size is not None and total != size:
                    raise DecryptionError
                if plain:
                    yield plain
                return
            if tag != _TAG_MESSAGE:
                raise DecryptionError
            yield plain


def decrypt_to_bytes(chunks: Iterable[bytes], *, limit: int) -> bytes:
    """Join a decrypted stream, refusing more than ``limit`` bytes."""
    out = bytearray()
    for chunk in chunks:
        out += chunk
        if len(out) > limit:
            raise BlobStoreError("blob exceeds the size limit")
    return bytes(out)

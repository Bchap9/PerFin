# SPDX-License-Identifier: AGPL-3.0-or-later
"""Encrypted blob store (ADR-0003; SECURITY.md G1, G5, C3)."""

from __future__ import annotations

import os
import secrets
import stat
import uuid
from pathlib import Path

import pytest

from app.core.crypto import DecryptionError, Keyring, WrappedKey
from app.domain.documents.blobstore import (
    CHUNK_BYTES,
    BlobNotFoundError,
    BlobStoreError,
    BlobStoreNotImplementedError,
    EncryptingWriter,
    LocalFSBlobStore,
    S3BlobStore,
    SealedBlob,
    blob_ad,
    check_key,
    decrypt_stream,
)
from app.domain.documents.models import BlobArea


@pytest.fixture
def keyring() -> Keyring:
    return Keyring.from_master_key(secrets.token_bytes(32))


@pytest.fixture
def store(tmp_path: Path) -> LocalFSBlobStore:
    return LocalFSBlobStore(tmp_path / "documents", tmp_path / "quarantine")


def _write(
    store: LocalFSBlobStore, keyring: Keyring, data: bytes, step: int = 10_000
) -> SealedBlob:
    with EncryptingWriter(store, keyring, uuid.uuid4()) as writer:
        for offset in range(0, len(data), step):
            writer.write(data[offset : offset + step])
        return writer.finish()


def _read(
    store: LocalFSBlobStore, keyring: Keyring, sealed: SealedBlob, **overrides: object
) -> bytes:
    params: dict[str, object] = {
        "blob_id": sealed.blob_id,
        "area": sealed.area,
        "key": sealed.key,
        "header": sealed.header,
        "wrapped": sealed.wrapped,
        "size": sealed.size,
    }
    params.update(overrides)
    return b"".join(decrypt_stream(store, keyring, **params))  # type: ignore[arg-type]


@pytest.mark.parametrize("size", [0, 1, CHUNK_BYTES - 1, CHUNK_BYTES, CHUNK_BYTES + 1, 300_000])
def test_round_trip(store: LocalFSBlobStore, keyring: Keyring, size: int) -> None:
    data = secrets.token_bytes(size)
    sealed = _write(store, keyring, data)
    assert sealed.size == size
    assert sealed.area is BlobArea.QUARANTINE
    assert _read(store, keyring, sealed) == data


def test_file_is_ciphertext_under_a_random_key(store: LocalFSBlobStore, keyring: Keyring) -> None:
    data = b"Synthetic statement line " * 1000
    sealed = _write(store, keyring, data)
    path = store.path(sealed.area, sealed.key)
    raw = path.read_bytes()
    assert b"Synthetic" not in raw
    assert len(raw) == sealed.stored_size
    check_key(sealed.key)  # UUIDv4: nothing derived from a file name (C3)
    assert path.parent.parent.parent == store.path(sealed.area, sealed.key).parents[2]
    assert path.parent.name == sealed.key[2:4]
    assert path.parent.parent.name == sealed.key[0:2]


def test_permissions_are_private(store: LocalFSBlobStore, keyring: Keyring) -> None:
    sealed = _write(store, keyring, b"x")
    path = store.path(sealed.area, sealed.key)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700


def test_aborted_write_leaves_nothing(store: LocalFSBlobStore, keyring: Keyring) -> None:
    writer = EncryptingWriter(store, keyring, uuid.uuid4())
    writer.write(b"partial")
    writer.abort()
    root = store.path(BlobArea.QUARANTINE, "00000000-0000-4000-8000-000000000000").parents[2]
    assert [p for p in root.rglob("*") if p.is_file()] == []


def test_tampering_truncation_and_appending_are_detected(
    store: LocalFSBlobStore, keyring: Keyring
) -> None:
    sealed = _write(store, keyring, secrets.token_bytes(CHUNK_BYTES * 2 + 10))
    path = store.path(sealed.area, sealed.key)
    original = path.read_bytes()
    for mutated in (
        original[:100] + bytes([original[100] ^ 1]) + original[101:],  # bit flip
        original[: CHUNK_BYTES + 17],  # whole chunks cut off
        original[:-5],  # truncated final chunk
        original + b"extra",  # appended data
        original[CHUNK_BYTES + 17 :] + original[: CHUNK_BYTES + 17],  # reordered
    ):
        path.write_bytes(mutated)
        with pytest.raises(DecryptionError):
            _read(store, keyring, sealed)


def test_wrapped_key_is_bound_to_the_blob_id(store: LocalFSBlobStore, keyring: Keyring) -> None:
    first = _write(store, keyring, b"first")
    second = _write(store, keyring, b"second")
    # Moving the wrapped key (or the file) to another blob row does not work.
    with pytest.raises(DecryptionError):
        _read(store, keyring, first, blob_id=second.blob_id)
    with pytest.raises(DecryptionError):
        _read(store, keyring, first, wrapped=second.wrapped)
    with pytest.raises(DecryptionError):
        _read(store, keyring, first, key=second.key)


def test_wrong_master_key_or_size_fails(store: LocalFSBlobStore, keyring: Keyring) -> None:
    sealed = _write(store, keyring, b"payload")
    other = Keyring.from_master_key(secrets.token_bytes(32))
    with pytest.raises(DecryptionError):
        _read(store, other, sealed)
    with pytest.raises(DecryptionError):
        _read(store, keyring, sealed, size=3)


def test_shredded_key_makes_the_file_unreadable(store: LocalFSBlobStore, keyring: Keyring) -> None:
    """G5: once the wrapped DEK is gone, the ciphertext is useless."""
    sealed = _write(store, keyring, b"synthetic secret")
    with pytest.raises(DecryptionError):
        _read(store, keyring, sealed, wrapped=None)


def test_key_rotation_rewraps_without_touching_the_file(
    store: LocalFSBlobStore, keyring: Keyring
) -> None:
    sealed = _write(store, keyring, b"rotate me")
    path = store.path(sealed.area, sealed.key)
    before = path.read_bytes()
    rotated = Keyring({1: keyring._key(1), 2: secrets.token_bytes(32)}, current=2)
    new_wrap = rotated.rewrap_data_key(sealed.wrapped, ad=blob_ad(sealed.blob_id))
    assert new_wrap.key_version == 2
    assert _read(store, rotated, sealed, wrapped=new_wrap) == b"rotate me"
    assert path.read_bytes() == before


def test_bad_wrapped_key_shapes_fail(keyring: Keyring) -> None:
    with pytest.raises(DecryptionError):
        keyring.unwrap_data_key(WrappedKey(1, b"short", b"x" * 48), ad=b"ad")
    assert "wrapped" not in repr(WrappedKey(1, b"n" * 24, b"k" * 48)).lower().replace(
        "wrappedkey", ""
    )


def test_promotion_copies_ciphertext_unchanged(store: LocalFSBlobStore, keyring: Keyring) -> None:
    sealed = _write(store, keyring, b"promote")
    source = store.path(BlobArea.QUARANTINE, sealed.key).read_bytes()
    store.copy(sealed.key, BlobArea.QUARANTINE, BlobArea.PERMANENT)
    store.copy(sealed.key, BlobArea.QUARANTINE, BlobArea.PERMANENT)  # idempotent
    assert store.path(BlobArea.PERMANENT, sealed.key).read_bytes() == source
    assert store.delete(BlobArea.QUARANTINE, sealed.key)
    assert not store.delete(BlobArea.QUARANTINE, sealed.key)
    assert _read(store, keyring, sealed, area=BlobArea.PERMANENT) == b"promote"


def test_promotion_across_filesystems_copies(
    store: LocalFSBlobStore, keyring: Keyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed = _write(store, keyring, secrets.token_bytes(3_000_000))

    def no_link(*_: object, **__: object) -> None:
        raise OSError(18, "Invalid cross-device link")  # EXDEV

    monkeypatch.setattr(os, "link", no_link)
    store.copy(sealed.key, BlobArea.QUARANTINE, BlobArea.PERMANENT)
    assert (
        store.path(BlobArea.PERMANENT, sealed.key).read_bytes()
        == store.path(BlobArea.QUARANTINE, sealed.key).read_bytes()
    )
    path = store.path(BlobArea.PERMANENT, sealed.key)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_symlinked_object_is_refused(
    store: LocalFSBlobStore, keyring: Keyring, tmp_path: Path
) -> None:
    sealed = _write(store, keyring, b"x")
    path = store.path(sealed.area, sealed.key)
    target = tmp_path / "elsewhere"
    target.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(BlobStoreError):
        _read(store, keyring, sealed)


def test_missing_object(store: LocalFSBlobStore, keyring: Keyring) -> None:
    sealed = _write(store, keyring, b"x")
    store.delete(sealed.area, sealed.key)
    with pytest.raises(BlobNotFoundError):
        _read(store, keyring, sealed)


@pytest.mark.parametrize(
    "key", ["../../etc/passwd", "a/b", "", "00000000-0000-1000-8000-000000000000"]
)
def test_keys_must_be_uuid4(store: LocalFSBlobStore, key: str) -> None:
    with pytest.raises(BlobStoreError):
        store.path(BlobArea.PERMANENT, key)


def test_s3_store_is_not_implemented() -> None:
    with pytest.raises(BlobStoreNotImplementedError):
        S3BlobStore()

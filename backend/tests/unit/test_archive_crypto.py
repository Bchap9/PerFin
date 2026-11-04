# SPDX-License-Identifier: AGPL-3.0-or-later
"""Passphrase-encrypted archives (``app.core.archive_crypto``; SECURITY.md G3).

Round trips at chunk boundaries, then every way an archive can be wrong:
the passphrase, a flipped byte in any chunk or in the header, swapped KDF
parameters, truncation (a whole final chunk or a few bytes), appended
data, reordered chunks, unknown versions, and hostile KDF parameters that
must be refused before any key is derived.
"""

from __future__ import annotations

import secrets

import nacl.pwhash
import pytest

from app.core import archive_crypto
from app.core.archive_crypto import (
    CHUNK_BYTES,
    CIPHER_CHUNK_BYTES,
    HEADER_BYTES,
    MAGIC,
    ArchiveDecryptionError,
    ArchiveFormatError,
    ArchiveHeader,
    KdfParams,
    decrypt_stream,
    encrypt_stream,
    is_encrypted_archive,
)

KDF = KdfParams(1, 8 * 1024)
PASS = "a synthetic passphrase"


def encrypt(data: bytes, passphrase: str = PASS, *, pieces: int = 1) -> bytes:
    step = max(1, len(data) // pieces) if data else 1
    chunks = [data[i : i + step] for i in range(0, len(data), step)] or [b""]
    return b"".join(encrypt_stream(chunks, passphrase, kdf=KDF))


def decrypt(data: bytes, passphrase: str = PASS, *, piece: int = 7000) -> bytes:
    chunks = [data[i : i + piece] for i in range(0, len(data), piece)]
    return b"".join(decrypt_stream(chunks, passphrase))


@pytest.mark.parametrize(
    "size",
    [0, 1, CHUNK_BYTES - 1, CHUNK_BYTES, CHUNK_BYTES + 1, 2 * CHUNK_BYTES, 3 * CHUNK_BYTES + 5],
)
def test_round_trip_at_chunk_boundaries(size: int) -> None:
    data = secrets.token_bytes(size)
    sealed = encrypt(data, pieces=3)
    assert sealed.startswith(MAGIC)
    assert is_encrypted_archive(sealed)
    assert data == decrypt(sealed)
    assert data == decrypt(sealed, piece=1)  # byte by byte, too
    chunks = -(-size // CHUNK_BYTES) if size else 1
    assert len(sealed) == HEADER_BYTES + size + chunks * (CIPHER_CHUNK_BYTES - CHUNK_BYTES)


def test_unicode_passphrases_are_normalised() -> None:
    composed, decomposed = "café au lait!!", "café au lait!!"
    assert decrypt(encrypt(b"data", composed), decomposed) == b"data"


def test_wrong_passphrase() -> None:
    with pytest.raises(ArchiveDecryptionError):
        decrypt(encrypt(b"secret ledger"), "another passphrase")


@pytest.mark.parametrize("where", ["first", "middle", "last"])
def test_any_flipped_byte_is_detected(where: str) -> None:
    sealed = bytearray(encrypt(secrets.token_bytes(2 * CHUNK_BYTES + 100)))
    index = {
        "first": HEADER_BYTES + 3,
        "middle": HEADER_BYTES + CIPHER_CHUNK_BYTES + 10,
        "last": len(sealed) - 1,
    }[where]
    sealed[index] ^= 0x01
    with pytest.raises(ArchiveDecryptionError):
        decrypt(bytes(sealed))


def test_header_fields_are_authenticated() -> None:
    sealed = bytearray(encrypt(b"x" * 100))
    salt_offset = len(MAGIC) + 13
    sealed[salt_offset] ^= 0x01  # another salt: another key
    with pytest.raises(ArchiveDecryptionError):
        decrypt(bytes(sealed))
    sealed = bytearray(encrypt(b"x" * 100))
    sealed[len(MAGIC) + 4] ^= 0x01  # opslimit 1 -> 0: out of bounds
    with pytest.raises(ArchiveFormatError):
        decrypt(bytes(sealed))


def test_swapped_kdf_parameters_are_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same key, other parameters in the header: the chunks' associated data differs."""
    sealed = encrypt(b"x" * 100)
    header = ArchiveHeader.parse(sealed)
    key = archive_crypto.derive_key(PASS, header.salt, header.kdf)
    other = ArchiveHeader(KdfParams(2, 8 * 1024), header.salt, header.stream_header)
    monkeypatch.setattr(archive_crypto, "derive_key", lambda *_: key)
    with pytest.raises(ArchiveDecryptionError):
        decrypt(other.to_bytes() + sealed[HEADER_BYTES:])


def test_truncation_appending_and_reordering_are_detected() -> None:
    sealed = encrypt(secrets.token_bytes(3 * CHUNK_BYTES))
    body = sealed[HEADER_BYTES:]
    chunks = [body[i : i + CIPHER_CHUNK_BYTES] for i in range(0, len(body), CIPHER_CHUNK_BYTES)]
    head = sealed[:HEADER_BYTES]
    for broken in (
        head + b"".join(chunks[:-1]),  # final chunk dropped: ends on a full chunk
        sealed[:-5],  # final chunk cut short
        sealed[: HEADER_BYTES + 10],
        sealed + b"\x00",  # data after the final chunk
        sealed + chunks[0],
        head + chunks[1] + chunks[0] + b"".join(chunks[2:]),  # reordered
    ):
        with pytest.raises(ArchiveDecryptionError):
            decrypt(broken)


@pytest.mark.parametrize(
    "data",
    [b"", b"PK\x03\x04" + bytes(100), MAGIC[:-1] + b"\x02" + bytes(100), MAGIC + bytes(10)],
)
def test_not_an_archive_or_unknown_version(data: bytes) -> None:
    with pytest.raises(ArchiveFormatError):
        decrypt(data)


@pytest.mark.parametrize(
    "kdf",
    [
        KdfParams(11, 8 * 1024),  # too many passes
        KdfParams(1, 2 * 1024 * 1024 * 1024),  # 2 GiB of memory
        KdfParams(0, 8 * 1024),
        KdfParams(1, 1024),
    ],
)
def test_hostile_kdf_parameters_are_refused_before_derivation(
    kdf: KdfParams, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed = encrypt(b"data")
    header = ArchiveHeader.parse(sealed)
    forged = ArchiveHeader(kdf, header.salt, header.stream_header).to_bytes()
    calls: list[object] = []
    monkeypatch.setattr(nacl.pwhash.argon2id, "kdf", lambda *a, **k: calls.append(a))
    with pytest.raises(ArchiveFormatError):
        decrypt(forged + sealed[HEADER_BYTES:])
    assert calls == []


def test_a_reader_can_lower_the_ceiling() -> None:
    sealed = encrypt(b"data")
    with pytest.raises(ArchiveFormatError):
        b"".join(decrypt_stream([sealed], PASS, max_kdf=KdfParams(1, 4 * 1024)))


def test_empty_passphrase_is_refused() -> None:
    with pytest.raises(archive_crypto.ArchiveCryptoError):
        encrypt(b"data", "")


def test_default_cost_is_libsodium_moderate() -> None:
    assert archive_crypto.DEFAULT_KDF.memlimit >= 256 * 1024 * 1024
    assert archive_crypto.DEFAULT_KDF.opslimit >= 3
    archive_crypto.DEFAULT_KDF.check()

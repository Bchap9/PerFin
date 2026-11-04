# SPDX-License-Identifier: AGPL-3.0-or-later
"""Passphrase encryption of export and backup archives (SECURITY.md G3).

An archive that leaves PerFin (a full export, later a backup) can be
encrypted with a passphrase the user chooses, so a leaked file is useless
without it. The format ("PerFin encrypted archive", version 1; documented in
docs/PORTABILITY.md) is libsodium only, streamable in both directions and
never needs the whole archive in memory:

====== ====== ============================================================
offset bytes  field
====== ====== ============================================================
0      8      magic ``b"PFARCH\\x00\\x01"`` (the last byte is the version)
8      1      KDF id: ``1`` = Argon2id (``crypto_pwhash_ALG_ARGON2ID13``)
9      4      Argon2id ``opslimit`` (unsigned, big-endian)
13     8      Argon2id ``memlimit`` in bytes (unsigned, big-endian)
21     16     salt (random)
37     24     ``crypto_secretstream_xchacha20poly1305`` header
61     ...    the encrypted chunks
====== ====== ============================================================

The key is ``Argon2id(passphrase as NFC UTF-8, salt, opslimit, memlimit)``,
32 bytes. The plaintext is cut into :data:`CHUNK_BYTES` chunks; every chunk
is pushed with the 61 header bytes as associated data (so the KDF parameters
and salt cannot be swapped), all but the last with ``TAG_MESSAGE`` and the
last (at most :data:`CHUNK_BYTES`, possibly empty) with ``TAG_FINAL``.
Truncation, reordering, appended bytes, a changed header and a wrong
passphrase are all detected as :class:`ArchiveDecryptionError`.

Decryption refuses KDF parameters outside :data:`MIN_KDF`..:data:`MAX_KDF`
before deriving anything (a hostile header cannot make the server spend
gigabytes of memory or minutes of CPU).

The key derivation is separate from the stream (:func:`derive_key`,
:class:`ArchiveEncryptor`, :class:`ArchiveDecryptor`) so a server can derive
the key while the passphrase is in the request and hand only a wrapped key
to a background job; :func:`encrypt_stream` / :func:`decrypt_stream` do both
for scripts.
"""

from __future__ import annotations

import secrets
import struct
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Final

from nacl import bindings
from nacl.exceptions import CryptoError
from nacl.pwhash import argon2id

MAGIC: Final = b"PFARCH\x00\x01"
FORMAT_VERSION: Final = 1
KDF_ARGON2ID: Final = 1
SALT_BYTES: Final = argon2id.SALTBYTES  # 16
KEY_BYTES: Final = bindings.crypto_secretstream_xchacha20poly1305_KEYBYTES  # 32
STREAM_HEADER_BYTES: Final = bindings.crypto_secretstream_xchacha20poly1305_HEADERBYTES  # 24
_PARAMS: Final = struct.Struct(">BIQ")
HEADER_BYTES: Final = len(MAGIC) + _PARAMS.size + SALT_BYTES + STREAM_HEADER_BYTES  # 61

#: Plaintext bytes per chunk.
CHUNK_BYTES: Final = 64 * 1024
_ABYTES: Final = bindings.crypto_secretstream_xchacha20poly1305_ABYTES  # 17
CIPHER_CHUNK_BYTES: Final = CHUNK_BYTES + _ABYTES
_TAG_MESSAGE: Final = bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE
_TAG_FINAL: Final = bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL

PASSPHRASE_MAX_CHARS: Final = 1024


class ArchiveCryptoError(Exception):
    """Base class; messages never contain key material or the passphrase."""


class ArchiveFormatError(ArchiveCryptoError):
    """Not an encrypted archive, an unknown version, or KDF parameters out of bounds."""


class ArchiveDecryptionError(ArchiveCryptoError):
    """Wrong passphrase, or the archive was truncated or changed."""


@dataclass(frozen=True)
class KdfParams:
    """Argon2id cost: ``opslimit`` passes over ``memlimit`` bytes of memory."""

    opslimit: int
    memlimit: int

    def check(self, low: KdfParams | None = None, high: KdfParams | None = None) -> KdfParams:
        low = low or MIN_KDF
        high = high or MAX_KDF
        if not (
            low.opslimit <= self.opslimit <= high.opslimit
            and low.memlimit <= self.memlimit <= high.memlimit
        ):
            raise ArchiveFormatError("key derivation parameters are out of bounds")
        return self


#: Libsodium's "moderate" profile: about a second and 256 MiB per derivation.
DEFAULT_KDF: Final = KdfParams(argon2id.OPSLIMIT_MODERATE, argon2id.MEMLIMIT_MODERATE)
#: The cheapest parameters accepted (libsodium's minimum memory is 8 KiB;
#: tests may go that low, archives written by PerFin never do).
MIN_KDF: Final = KdfParams(argon2id.OPSLIMIT_MIN, 8 * 1024)
#: The most expensive parameters a reader accepts from an archive header.
MAX_KDF: Final = KdfParams(10, 1024 * 1024 * 1024)


@dataclass(frozen=True)
class ArchiveHeader:
    """The 61 bytes in front of the encrypted chunks."""

    kdf: KdfParams
    salt: bytes
    stream_header: bytes

    def to_bytes(self) -> bytes:
        return (
            MAGIC
            + _PARAMS.pack(KDF_ARGON2ID, self.kdf.opslimit, self.kdf.memlimit)
            + self.salt
            + self.stream_header
        )

    @classmethod
    def parse(cls, data: bytes, *, max_kdf: KdfParams | None = None) -> ArchiveHeader:
        """The header at the start of ``data`` (at least :data:`HEADER_BYTES` bytes)."""
        if len(data) < HEADER_BYTES or not data.startswith(MAGIC[:-1]):
            raise ArchiveFormatError("not a PerFin encrypted archive")
        if data[len(MAGIC) - 1] != FORMAT_VERSION:
            raise ArchiveFormatError("unsupported encrypted archive version")
        offset = len(MAGIC)
        kdf_id, opslimit, memlimit = _PARAMS.unpack_from(data, offset)
        if kdf_id != KDF_ARGON2ID:
            raise ArchiveFormatError("unsupported key derivation function")
        offset += _PARAMS.size
        salt = bytes(data[offset : offset + SALT_BYTES])
        offset += SALT_BYTES
        stream_header = bytes(data[offset : offset + STREAM_HEADER_BYTES])
        kdf = KdfParams(opslimit, memlimit).check(high=max_kdf)
        return cls(kdf, salt, stream_header)

    def __repr__(self) -> str:
        return f"ArchiveHeader(opslimit={self.kdf.opslimit}, memlimit={self.kdf.memlimit})"


def is_encrypted_archive(head: bytes) -> bool:
    """True when ``head`` starts like an encrypted archive (any version)."""
    return head.startswith(MAGIC[:-1])


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_BYTES)


def _passphrase_bytes(passphrase: str) -> bytes:
    if not isinstance(passphrase, str) or not passphrase:
        raise ArchiveCryptoError("a passphrase is required")
    if len(passphrase) > PASSPHRASE_MAX_CHARS:
        raise ArchiveCryptoError("the passphrase is too long")
    return unicodedata.normalize("NFC", passphrase).encode("utf-8")


def derive_key(passphrase: str, salt: bytes, kdf: KdfParams) -> bytes:
    """The 32-byte archive key for ``passphrase`` (Argon2id, see the module doc)."""
    if len(salt) != SALT_BYTES:
        raise ArchiveFormatError("invalid salt")
    kdf.check()
    key: bytes = argon2id.kdf(
        KEY_BYTES,
        _passphrase_bytes(passphrase),
        salt,
        opslimit=kdf.opslimit,
        memlimit=kdf.memlimit,
    )
    return key


class ArchiveEncryptor:
    """Encrypt a plaintext stream: ``header`` first, then ``update``/``finish`` output.

    The key comes from :func:`derive_key` with the same ``salt`` and ``kdf``.
    """

    def __init__(self, key: bytes, *, salt: bytes, kdf: KdfParams) -> None:
        if len(key) != KEY_BYTES:
            raise ArchiveCryptoError("invalid archive key")
        if len(salt) != SALT_BYTES:
            raise ArchiveFormatError("invalid salt")
        self._state = bindings.crypto_secretstream_xchacha20poly1305_state()
        stream_header: bytes = bindings.crypto_secretstream_xchacha20poly1305_init_push(
            self._state, key
        )
        self._header = ArchiveHeader(kdf.check(), salt, stream_header).to_bytes()
        self._buffer = bytearray()
        self._finished = False

    @property
    def header(self) -> bytes:
        """The :data:`HEADER_BYTES` that precede the chunks."""
        return self._header

    def _push(self, chunk: bytes, tag: int) -> bytes:
        sealed: bytes = bindings.crypto_secretstream_xchacha20poly1305_push(
            self._state, chunk, self._header, tag
        )
        return sealed

    def update(self, data: bytes) -> bytes:
        """Ciphertext for every complete chunk so far (possibly empty)."""
        if self._finished:
            raise ArchiveCryptoError("the encryptor is finished")
        self._buffer += data
        out = bytearray()
        # Keep at least one byte back so the final chunk is never preceded by a
        # full chunk that could have been the last (the reader relies on it).
        while len(self._buffer) > CHUNK_BYTES:
            chunk = bytes(self._buffer[:CHUNK_BYTES])
            del self._buffer[:CHUNK_BYTES]
            out += self._push(chunk, _TAG_MESSAGE)
        return bytes(out)

    def finish(self) -> bytes:
        """The final chunk; nothing may be added afterwards."""
        if self._finished:
            raise ArchiveCryptoError("the encryptor is finished")
        self._finished = True
        final = self._push(bytes(self._buffer), _TAG_FINAL)
        self._buffer.clear()
        return final


class ArchiveDecryptor:
    """Decrypt the chunks that follow a parsed :class:`ArchiveHeader`.

    Feed ciphertext (without the header) to :meth:`update`, which returns
    authenticated plaintext; :meth:`finish` raises unless the final chunk
    arrived and nothing followed it.
    """

    def __init__(self, key: bytes, header: ArchiveHeader) -> None:
        if len(key) != KEY_BYTES:
            raise ArchiveCryptoError("invalid archive key")
        self._ad = header.to_bytes()
        self._state = bindings.crypto_secretstream_xchacha20poly1305_state()
        try:
            bindings.crypto_secretstream_xchacha20poly1305_init_pull(
                self._state, header.stream_header, key
            )
        except CryptoError:
            raise ArchiveDecryptionError("wrong passphrase or damaged archive") from None
        self._buffer = bytearray()
        self._done = False

    def _pull(self, sealed: bytes) -> tuple[bytes, int]:
        try:
            plain, tag = bindings.crypto_secretstream_xchacha20poly1305_pull(
                self._state, sealed, self._ad
            )
        except CryptoError:
            raise ArchiveDecryptionError("wrong passphrase or damaged archive") from None
        return bytes(plain), int(tag)

    def update(self, data: bytes) -> bytes:
        if self._done:
            if data:
                raise ArchiveDecryptionError("data after the end of the archive")
            return b""
        self._buffer += data
        out = bytearray()
        # A full-size chunk may be the final one: keep it until more arrives.
        while len(self._buffer) > CIPHER_CHUNK_BYTES:
            sealed = bytes(self._buffer[:CIPHER_CHUNK_BYTES])
            del self._buffer[:CIPHER_CHUNK_BYTES]
            plain, tag = self._pull(sealed)
            if tag != _TAG_MESSAGE:
                raise ArchiveDecryptionError("data after the end of the archive")
            out += plain
        return bytes(out)

    def finish(self) -> bytes:
        """The last plaintext; raises for a truncated archive or trailing data."""
        if self._done:
            raise ArchiveDecryptionError("the decryptor is finished")
        self._done = True
        if len(self._buffer) < _ABYTES:
            raise ArchiveDecryptionError("the archive is truncated")
        plain, tag = self._pull(bytes(self._buffer))
        self._buffer.clear()
        if tag != _TAG_FINAL:
            raise ArchiveDecryptionError("the archive is truncated")
        return plain


def encrypt_stream(
    chunks: Iterable[bytes], passphrase: str, *, kdf: KdfParams = DEFAULT_KDF
) -> Iterator[bytes]:
    """Encrypt plaintext ``chunks`` with ``passphrase`` (header first)."""
    salt = new_salt()
    encryptor = ArchiveEncryptor(derive_key(passphrase, salt, kdf), salt=salt, kdf=kdf)
    yield encryptor.header
    for chunk in chunks:
        sealed = encryptor.update(chunk)
        if sealed:
            yield sealed
    yield encryptor.finish()


class HeaderReader:
    """Collects the header from the first chunks of a stream, then passes the rest."""

    def __init__(self, max_kdf: KdfParams | None = None) -> None:
        self._head = bytearray()
        self._max_kdf = max_kdf
        self.header: ArchiveHeader | None = None

    def feed(self, data: bytes) -> bytes:
        """Ciphertext after the header (empty until the header is complete)."""
        if self.header is not None:
            return data
        self._head += data
        if len(self._head) < HEADER_BYTES:
            if not MAGIC[:-1].startswith(bytes(self._head[: len(MAGIC) - 1])):
                raise ArchiveFormatError("not a PerFin encrypted archive")
            return b""
        self.header = ArchiveHeader.parse(bytes(self._head), max_kdf=self._max_kdf)
        rest = bytes(self._head[HEADER_BYTES:])
        self._head.clear()
        return rest


def decrypt_stream(
    chunks: Iterable[bytes], passphrase: str, *, max_kdf: KdfParams | None = None
) -> Iterator[bytes]:
    """Decrypt an encrypted archive given as ciphertext ``chunks`` (header included)."""
    reader = HeaderReader(max_kdf)
    decryptor: ArchiveDecryptor | None = None
    for chunk in chunks:
        rest = reader.feed(chunk)
        if reader.header is None:
            continue
        if decryptor is None:
            decryptor = ArchiveDecryptor(
                derive_key(passphrase, reader.header.salt, reader.header.kdf), reader.header
            )
        plain = decryptor.update(rest)
        if plain:
            yield plain
    if decryptor is None:
        raise ArchiveFormatError("not a PerFin encrypted archive")
    last = decryptor.finish()
    if last:
        yield last

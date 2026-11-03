# SPDX-License-Identifier: AGPL-3.0-or-later
"""Envelope encryption with XChaCha20-Poly1305 (ADR-0003, SECURITY.md G1).

Every encrypted object gets its own random 256-bit data key (DEK). The DEK
encrypts the plaintext; the master key (the key-encryption key, KEK, from
``PERFIN_MASTER_KEY`` / ``PERFIN_MASTER_KEY_FILE``) wraps the DEK. Both steps
use the XChaCha20-Poly1305 AEAD from libsodium (PyNaCl bindings) with fresh
random 192-bit nonces, so nonce reuse is not a practical concern.

**Associated data.** Every call takes ``ad``: bytes that are authenticated
but not stored, binding the ciphertext to where it lives. For column
encryption use :func:`column_ad` (table, column and row id): a ciphertext
copied into another row, another column or another table then fails to
decrypt instead of silently yielding the other row's secret. The same ``ad``
is also bound into the DEK wrap, so a wrapped DEK cannot be lifted out and
reused for another object either.

**Key versions.** :class:`Keyring` holds master keys by integer version; the
version used is stored in the envelope so that rotation (``perfin
rotate-key``, Phase 7) can rewrap DEKs under a new master key while old
envelopes stay readable; :meth:`Keyring.rewrap` replaces only the wrapped DEK
and keeps the payload byte for byte (SECURITY.md G4). The key version is
authenticated through the wrap's associated data. Phase 1 has exactly one
key, version 1.

Envelope layout (:class:`Envelope`, ``bytes(envelope)``), all big-endian::

    magic "PFE" | format 0x01 | key_version u32 | wrap_nonce (24)
    | wrapped_dek (32 + 16 tag) | nonce (24) | ciphertext (+ 16 tag)

Errors never include key material or plaintext. Decryption failures of any
kind (tampering, wrong ``ad``, wrong key, truncation, unknown version) raise
:class:`DecryptionError` with a generic message.
"""

from __future__ import annotations

import hashlib
import secrets
import struct
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from nacl import bindings
from nacl.exceptions import CryptoError

if TYPE_CHECKING:
    from app.core.settings import Settings

KEY_BYTES: Final = bindings.crypto_aead_xchacha20poly1305_ietf_KEYBYTES  # 32
NONCE_BYTES: Final = bindings.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES  # 24
TAG_BYTES: Final = bindings.crypto_aead_xchacha20poly1305_ietf_ABYTES  # 16

MAGIC: Final = b"PFE"
FORMAT_VERSION: Final = 1
_HEADER = struct.Struct(">3sBI")  # magic, format, key_version
WRAPPED_DEK_BYTES: Final = KEY_BYTES + TAG_BYTES
MIN_ENVELOPE_BYTES: Final = _HEADER.size + NONCE_BYTES + WRAPPED_DEK_BYTES + NONCE_BYTES + TAG_BYTES

# Domain separation between the DEK wrap and the payload encryption.
_WRAP_CONTEXT: Final = b"perfin/dek-wrap/v1\x00"
_DATA_CONTEXT: Final = b"perfin/data/v1\x00"
_AD_CONTEXT: Final = b"perfin/column/v1"
# The payload AD does not include the master-key version (the wrap AD does),
# so a key rotation rewraps DEKs without touching payloads.
_PAYLOAD_VERSION: Final = 0

MAX_KEY_VERSION: Final = 0xFFFFFFFF


class CryptoConfigError(ValueError):
    """The keyring is misconfigured (bad key length, unknown current version)."""


class DecryptionError(Exception):
    """An envelope could not be authenticated or decrypted (no details given)."""

    def __init__(self) -> None:
        super().__init__("decryption failed")


def column_ad(table: str, column: str, row_id: uuid.UUID | str) -> bytes:
    """Associated data binding a value to ``table.column`` of row ``row_id``.

    Length-prefixed fields, so no two different triples encode the same.
    """
    parts = [_AD_CONTEXT, table.encode("utf-8"), column.encode("utf-8"), str(row_id).encode()]
    return b"".join(struct.pack(">I", len(p)) + p for p in parts)


def _ad_with_context(context: bytes, ad: bytes, key_version: int) -> bytes:
    return context + struct.pack(">I", key_version) + hashlib.sha256(ad).digest()


@dataclass(frozen=True)
class Envelope:
    """The stored form of one encrypted value."""

    key_version: int
    wrap_nonce: bytes
    wrapped_dek: bytes
    nonce: bytes
    ciphertext: bytes

    def __bytes__(self) -> bytes:
        return b"".join(
            (
                _HEADER.pack(MAGIC, FORMAT_VERSION, self.key_version),
                self.wrap_nonce,
                self.wrapped_dek,
                self.nonce,
                self.ciphertext,
            )
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> Envelope:
        """Parse an envelope; malformed input raises :class:`DecryptionError`."""
        if not isinstance(data, bytes | bytearray | memoryview):
            raise DecryptionError
        raw = bytes(data)
        if len(raw) < MIN_ENVELOPE_BYTES:
            raise DecryptionError
        magic, fmt, key_version = _HEADER.unpack_from(raw, 0)
        if magic != MAGIC or fmt != FORMAT_VERSION:
            raise DecryptionError
        offset = _HEADER.size
        wrap_nonce = raw[offset : offset + NONCE_BYTES]
        offset += NONCE_BYTES
        wrapped_dek = raw[offset : offset + WRAPPED_DEK_BYTES]
        offset += WRAPPED_DEK_BYTES
        nonce = raw[offset : offset + NONCE_BYTES]
        offset += NONCE_BYTES
        return cls(key_version, wrap_nonce, wrapped_dek, nonce, raw[offset:])


class Keyring:
    """Master keys by version; ``current`` is used for new envelopes.

    ``repr()`` never shows key bytes.
    """

    __slots__ = ("_keys", "current")

    def __init__(self, keys: Mapping[int, bytes], current: int) -> None:
        if not keys:
            raise CryptoConfigError("keyring is empty")
        for version, key in keys.items():
            if not 1 <= version <= MAX_KEY_VERSION:
                raise CryptoConfigError("key versions must be between 1 and 2**32-1")
            if len(key) != KEY_BYTES:
                raise CryptoConfigError(f"master keys must be {KEY_BYTES} bytes")
        if current not in keys:
            raise CryptoConfigError("current key version is not in the keyring")
        self._keys: dict[int, bytes] = {int(v): bytes(k) for v, k in keys.items()}
        self.current = current

    def __repr__(self) -> str:
        return f"Keyring(versions={sorted(self._keys)}, current={self.current})"

    @property
    def versions(self) -> tuple[int, ...]:
        return tuple(sorted(self._keys))

    def key_id(self, version: int) -> str:
        """A short non-secret fingerprint of a key (for logs and the rotation CLI)."""
        key = self._key(version)
        return hashlib.blake2b(key, digest_size=8, person=b"perfin-key-id").hexdigest()

    def _key(self, version: int) -> bytes:
        try:
            return self._keys[version]
        except KeyError:
            raise DecryptionError from None

    @classmethod
    def from_master_key(cls, key: bytes, version: int = 1) -> Keyring:
        return cls({version: key}, current=version)

    @classmethod
    def from_settings(cls, settings: Settings) -> Keyring:
        """Version 1 is the configured master key (``Settings.load_master_key``)."""
        return cls.from_master_key(settings.load_master_key(), version=1)

    # ---- envelope operations -------------------------------------------------
    def encrypt(self, plaintext: bytes, *, ad: bytes) -> Envelope:
        """Encrypt ``plaintext`` under a fresh DEK wrapped by the current key."""
        dek = secrets.token_bytes(KEY_BYTES)
        wrap_nonce = secrets.token_bytes(NONCE_BYTES)
        nonce = secrets.token_bytes(NONCE_BYTES)
        version = self.current
        wrapped = bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
            dek, _ad_with_context(_WRAP_CONTEXT, ad, version), wrap_nonce, self._key(version)
        )
        ciphertext = bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
            bytes(plaintext), _ad_with_context(_DATA_CONTEXT, ad, _PAYLOAD_VERSION), nonce, dek
        )
        return Envelope(version, wrap_nonce, wrapped, nonce, ciphertext)

    def unwrap_dek(self, envelope: Envelope, *, ad: bytes) -> bytes:
        try:
            dek: bytes = bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
                envelope.wrapped_dek,
                _ad_with_context(_WRAP_CONTEXT, ad, envelope.key_version),
                envelope.wrap_nonce,
                self._key(envelope.key_version),
            )
        except CryptoError:
            raise DecryptionError from None
        return dek

    def decrypt(self, envelope: Envelope, *, ad: bytes) -> bytes:
        """Authenticate and decrypt; any failure raises :class:`DecryptionError`."""
        dek = self.unwrap_dek(envelope, ad=ad)
        try:
            plaintext: bytes = bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
                envelope.ciphertext,
                _ad_with_context(_DATA_CONTEXT, ad, _PAYLOAD_VERSION),
                envelope.nonce,
                dek,
            )
        except CryptoError:
            raise DecryptionError from None
        return plaintext

    def rewrap(self, envelope: Envelope, *, ad: bytes, to_version: int | None = None) -> Envelope:
        """Re-wrap the DEK under another key version (rotation); payload unchanged."""
        target = self.current if to_version is None else to_version
        dek = self.unwrap_dek(envelope, ad=ad)
        wrap_nonce = secrets.token_bytes(NONCE_BYTES)
        wrapped = bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
            dek, _ad_with_context(_WRAP_CONTEXT, ad, target), wrap_nonce, self._key(target)
        )
        # Only the wrap changes: the payload (its nonce and ciphertext) is kept
        # byte for byte, so rotation never re-encrypts blobs (SECURITY.md G4).
        return Envelope(target, wrap_nonce, wrapped, envelope.nonce, envelope.ciphertext)

    # ---- byte-level helpers ----------------------------------------------------
    def encrypt_bytes(self, plaintext: bytes, *, ad: bytes) -> bytes:
        """``bytes(encrypt(...))``: the value to store in a ``bytea`` column."""
        return bytes(self.encrypt(plaintext, ad=ad))

    def decrypt_bytes(self, data: bytes, *, ad: bytes) -> bytes:
        return self.decrypt(Envelope.from_bytes(data), ad=ad)

    def encrypt_str(self, plaintext: str, *, ad: bytes) -> bytes:
        return self.encrypt_bytes(plaintext.encode("utf-8"), ad=ad)

    def decrypt_str(self, data: bytes, *, ad: bytes) -> str:
        try:
            return self.decrypt_bytes(data, ad=ad).decode("utf-8")
        except UnicodeDecodeError:
            raise DecryptionError from None


def encrypt_bytes(keyring: Keyring, plaintext: bytes, *, ad: bytes) -> bytes:
    """Module-level form of :meth:`Keyring.encrypt_bytes`."""
    return keyring.encrypt_bytes(plaintext, ad=ad)


def decrypt_bytes(keyring: Keyring, data: bytes, *, ad: bytes) -> bytes:
    """Module-level form of :meth:`Keyring.decrypt_bytes`."""
    return keyring.decrypt_bytes(data, ad=ad)


def envelope_key_version(data: bytes) -> int:
    """The master-key version an encrypted column value was written with."""
    return Envelope.from_bytes(data).key_version

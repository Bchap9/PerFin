# SPDX-License-Identifier: AGPL-3.0-or-later
"""Envelope encryption (ADR-0003, SECURITY.md G1): XChaCha20-Poly1305, a fresh
DEK per value, the DEK wrapped by the master key, associated data binding."""

from __future__ import annotations

import base64
import secrets
import uuid

import pytest
from nacl import bindings as nacl_bindings
from pydantic import SecretStr

from app.core.crypto import (
    KEY_BYTES,
    MIN_ENVELOPE_BYTES,
    NONCE_BYTES,
    CryptoConfigError,
    DecryptionError,
    Envelope,
    Keyring,
    column_ad,
    decrypt_bytes,
    encrypt_bytes,
    envelope_key_version,
)
from app.core.settings import Settings

ROW = uuid.UUID("01890000-0000-7000-8000-000000000001")
OTHER_ROW = uuid.UUID("01890000-0000-7000-8000-000000000002")
AD = column_ad("mfa_method", "secret_encrypted", ROW)


@pytest.fixture
def keyring() -> Keyring:
    return Keyring.from_master_key(secrets.token_bytes(KEY_BYTES))


def test_round_trip(keyring: Keyring) -> None:
    plaintext = b"synthetic secret value"
    sealed = keyring.encrypt_bytes(plaintext, ad=AD)
    assert plaintext not in sealed
    assert keyring.decrypt_bytes(sealed, ad=AD) == plaintext
    assert encrypt_bytes(keyring, b"", ad=AD) != b""
    assert decrypt_bytes(keyring, encrypt_bytes(keyring, b"", ad=AD), ad=AD) == b""


def test_str_round_trip_and_bad_utf8(keyring: Keyring) -> None:
    assert keyring.decrypt_str(keyring.encrypt_str("JBSWY3DPEHPK3PXP", ad=AD), ad=AD) == (
        "JBSWY3DPEHPK3PXP"
    )
    with pytest.raises(DecryptionError):
        keyring.decrypt_str(keyring.encrypt_bytes(b"\xff\xfe", ad=AD), ad=AD)


def test_every_encryption_uses_fresh_dek_and_nonces(keyring: Keyring) -> None:
    first = keyring.encrypt(b"same", ad=AD)
    second = keyring.encrypt(b"same", ad=AD)
    assert first.wrap_nonce != second.wrap_nonce
    assert first.nonce != second.nonce
    assert first.wrapped_dek != second.wrapped_dek
    assert first.ciphertext != second.ciphertext
    assert len(first.nonce) == NONCE_BYTES == 24  # XChaCha20 extended nonce


def test_envelope_layout(keyring: Keyring) -> None:
    sealed = keyring.encrypt_bytes(b"x", ad=AD)
    assert sealed[:3] == b"PFE"
    assert sealed[3] == 1
    assert len(sealed) == MIN_ENVELOPE_BYTES + 1
    assert envelope_key_version(sealed) == 1
    assert bytes(Envelope.from_bytes(sealed)) == sealed


@pytest.mark.parametrize("position", ["header", "wrap_nonce", "wrapped_dek", "nonce", "ciphertext"])
def test_tampering_anywhere_is_detected(keyring: Keyring, position: str) -> None:
    sealed = bytearray(keyring.encrypt_bytes(b"synthetic secret value", ad=AD))
    offsets = {
        "header": 4,  # key version byte
        "wrap_nonce": 8,
        "wrapped_dek": 8 + 24,
        "nonce": 8 + 24 + 48,
        "ciphertext": 8 + 24 + 48 + 24,
    }
    sealed[offsets[position]] ^= 0x01
    with pytest.raises(DecryptionError):
        keyring.decrypt_bytes(bytes(sealed), ad=AD)


def test_wrong_associated_data_fails(keyring: Keyring) -> None:
    """A ciphertext copied into another row/column/table does not decrypt."""
    sealed = keyring.encrypt_bytes(b"secret of row one", ad=AD)
    for other in (
        column_ad("mfa_method", "secret_encrypted", OTHER_ROW),
        column_ad("mfa_method", "public_key", ROW),
        column_ad("notification_channel", "secret_encrypted", ROW),
        b"",
    ):
        with pytest.raises(DecryptionError):
            keyring.decrypt_bytes(sealed, ad=other)


def test_wrapped_dek_cannot_be_moved_between_envelopes(keyring: Keyring) -> None:
    one = keyring.encrypt(b"one", ad=AD)
    other_ad = column_ad("mfa_method", "secret_encrypted", OTHER_ROW)
    two = keyring.encrypt(b"two", ad=other_ad)
    spliced = Envelope(one.key_version, two.wrap_nonce, two.wrapped_dek, one.nonce, one.ciphertext)
    with pytest.raises(DecryptionError):
        keyring.decrypt(spliced, ad=AD)
    with pytest.raises(DecryptionError):
        keyring.decrypt(spliced, ad=other_ad)


def test_associated_data_encoding_is_unambiguous() -> None:
    assert column_ad("ab", "c", ROW) != column_ad("a", "bc", ROW)
    assert column_ad("t", "c", ROW) == column_ad("t", "c", str(ROW))


def test_wrong_master_key_fails(keyring: Keyring) -> None:
    sealed = keyring.encrypt_bytes(b"secret", ad=AD)
    other = Keyring.from_master_key(secrets.token_bytes(KEY_BYTES))
    with pytest.raises(DecryptionError):
        other.decrypt_bytes(sealed, ad=AD)


def test_unknown_key_version_fails(keyring: Keyring) -> None:
    sealed = keyring.encrypt(b"secret", ad=AD)
    relabelled = Envelope(7, sealed.wrap_nonce, sealed.wrapped_dek, sealed.nonce, sealed.ciphertext)
    with pytest.raises(DecryptionError):
        keyring.decrypt(relabelled, ad=AD)


@pytest.mark.parametrize(
    "data",
    [b"", b"PFE", b"XYZ\x01" + bytes(200), b"PFE\x02" + bytes(200), bytes(MIN_ENVELOPE_BYTES - 1)],
)
def test_malformed_input_is_a_decryption_error(keyring: Keyring, data: bytes) -> None:
    with pytest.raises(DecryptionError):
        keyring.decrypt_bytes(data, ad=AD)


def test_error_messages_reveal_nothing(keyring: Keyring) -> None:
    sealed = keyring.encrypt_bytes(b"synthetic plaintext", ad=AD)
    with pytest.raises(DecryptionError) as info:
        keyring.decrypt_bytes(sealed, ad=b"wrong")
    assert str(info.value) == "decryption failed"
    assert info.value.__cause__ is None


def test_rotation_rewraps_under_new_version() -> None:
    old_key, new_key = secrets.token_bytes(KEY_BYTES), secrets.token_bytes(KEY_BYTES)
    old = Keyring({1: old_key}, current=1)
    sealed = old.encrypt(b"long lived secret", ad=AD)
    both = Keyring({1: old_key, 2: new_key}, current=2)
    assert both.decrypt(sealed, ad=AD) == b"long lived secret"  # old envelopes readable
    rotated = both.rewrap(sealed, ad=AD)
    assert rotated.key_version == 2
    # G4: only the DEK wrap changes; the payload is not re-encrypted.
    assert (rotated.nonce, rotated.ciphertext) == (sealed.nonce, sealed.ciphertext)
    assert rotated.wrapped_dek != sealed.wrapped_dek
    relabelled = Envelope(
        1, rotated.wrap_nonce, rotated.wrapped_dek, rotated.nonce, rotated.ciphertext
    )
    with pytest.raises(DecryptionError):
        both.decrypt(relabelled, ad=AD)  # the version is authenticated
    only_new = Keyring({2: new_key}, current=2)
    assert only_new.decrypt(rotated, ad=AD) == b"long lived secret"
    with pytest.raises(DecryptionError):
        only_new.decrypt(sealed, ad=AD)
    assert both.encrypt(b"new", ad=AD).key_version == 2


def test_keyring_validation_and_repr() -> None:
    key = secrets.token_bytes(KEY_BYTES)
    with pytest.raises(CryptoConfigError):
        Keyring({}, current=1)
    with pytest.raises(CryptoConfigError):
        Keyring({1: b"short"}, current=1)
    with pytest.raises(CryptoConfigError):
        Keyring({1: key}, current=2)
    with pytest.raises(CryptoConfigError):
        Keyring({0: key}, current=0)
    ring = Keyring({1: key}, current=1)
    assert key.hex() not in repr(ring)
    assert repr(ring) == "Keyring(versions=[1], current=1)"
    assert len(ring.key_id(1)) == 16
    assert ring.key_id(1) != Keyring.from_master_key(secrets.token_bytes(KEY_BYTES)).key_id(1)
    assert ring.versions == (1,)


def test_keyring_from_settings_uses_master_key() -> None:
    key = secrets.token_bytes(KEY_BYTES)
    settings = Settings(master_key=SecretStr(base64.b64encode(key).decode("ascii")))
    ring = Keyring.from_settings(settings)
    sealed = Keyring.from_master_key(key).encrypt_bytes(b"x", ad=AD)
    assert ring.decrypt_bytes(sealed, ad=AD) == b"x"


def test_uses_xchacha20poly1305_primitive(
    monkeypatch: pytest.MonkeyPatch, keyring: Keyring
) -> None:
    calls: list[int] = []
    real = nacl_bindings.crypto_aead_xchacha20poly1305_ietf_encrypt

    def spy(message: bytes, aad: bytes | None, nonce: bytes, key: bytes) -> bytes:
        calls.append(len(nonce))
        result: bytes = real(message, aad, nonce, key)
        return result

    monkeypatch.setattr(nacl_bindings, "crypto_aead_xchacha20poly1305_ietf_encrypt", spy)
    keyring.encrypt_bytes(b"x", ad=AD)
    assert calls == [24, 24]  # DEK wrap + payload

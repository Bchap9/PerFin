# SPDX-License-Identifier: AGPL-3.0-or-later
"""Key rotation coverage and backup encryption, without a database (SECURITY.md G3, G4)."""

from __future__ import annotations

import base64
import io
import secrets
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import LargeBinary

from app.core.archive_crypto import MIN_KDF, ArchiveCryptoError
from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.base import Base
from app.db.registry import import_all_models
from app.ops import backup, keyrotation


def _binary_columns() -> set[tuple[str, str]]:
    import_all_models()
    return {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, LargeBinary)
    }


def test_every_binary_column_is_rotated_or_explained() -> None:
    """A new encrypted column must be added to ENVELOPE_COLUMNS (or NOT_ENVELOPES)."""
    rotated = {(c.table, c.column) for c in keyrotation.ENVELOPE_COLUMNS}
    explained = set(keyrotation.NOT_ENVELOPES)
    assert not rotated & explained
    assert _binary_columns() == rotated | explained


def test_encrypted_looking_columns_are_rotated() -> None:
    rotated = {(c.table, c.column) for c in keyrotation.ENVELOPE_COLUMNS}
    import_all_models()
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if column.name.endswith("_encrypted"):
                assert (table.name, column.name) in rotated


def test_keyring_from_settings_uses_the_configured_version() -> None:
    key = secrets.token_bytes(32)
    settings = Settings(master_key=SecretStr(base64.b64encode(key).decode()), master_key_version=7)
    keyring = Keyring.from_settings(settings)
    assert keyring.current == 7
    envelope = keyring.encrypt(b"x", ad=b"ad")
    assert envelope.key_version == 7
    # A value written under version 7 is not readable as version 1 of the same key.
    assert Keyring.from_settings(Settings(master_key=settings.master_key)).current == 1


def test_master_key_version_bounds() -> None:
    with pytest.raises(ValueError, match="master_key_version"):
        Settings(master_key_version=0)


def test_version_bytes_match_the_envelope_header() -> None:
    keyring = Keyring.from_master_key(secrets.token_bytes(32), version=258)
    data = keyring.encrypt_bytes(b"secret", ad=b"ad")
    assert data[4:8] == keyrotation.version_bytes(258)


def test_backup_encryption_round_trip_and_tamper_detection() -> None:
    plain = secrets.token_bytes(300_000)
    sealed = io.BytesIO()
    assert backup.encrypt_to(io.BytesIO(plain), sealed, "correct horse battery", kdf=MIN_KDF) == (
        len(plain)
    )
    data = sealed.getvalue()
    assert plain not in data
    out = io.BytesIO()
    assert backup.decrypt_to(io.BytesIO(data), out, "correct horse battery") == len(plain)
    assert out.getvalue() == plain
    with pytest.raises(ArchiveCryptoError):
        backup.decrypt_to(io.BytesIO(data), io.BytesIO(), "wrong passphrase!")
    tampered = bytearray(data)
    tampered[len(data) // 2] ^= 1
    with pytest.raises(ArchiveCryptoError):
        backup.decrypt_to(io.BytesIO(bytes(tampered)), io.BytesIO(), "correct horse battery")
    with pytest.raises(ArchiveCryptoError):
        backup.decrypt_to(io.BytesIO(data[:-100]), io.BytesIO(), "correct horse battery")


def test_read_passphrase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(backup.PASSPHRASE_ENV, raising=False)
    with pytest.raises(backup.PassphraseError, match="no passphrase"):
        backup.read_passphrase(None, for_encryption=False)
    monkeypatch.setenv(backup.PASSPHRASE_ENV, "short")
    with pytest.raises(backup.PassphraseError, match="at least 12"):
        backup.read_passphrase(None, for_encryption=True)
    assert backup.read_passphrase(None, for_encryption=False) == "short"
    path = tmp_path / "pass"
    path.write_text("a long synthetic passphrase\n")
    assert backup.read_passphrase(str(path), for_encryption=True) == "a long synthetic passphrase"
    with pytest.raises(backup.PassphraseError, match="not readable"):
        backup.read_passphrase(str(tmp_path / "missing"), for_encryption=True)


def test_backup_info_has_no_key_material() -> None:
    key = secrets.token_bytes(32)
    info = backup.backup_info(Keyring.from_master_key(key, version=3))
    assert info["master_key_version"] == 3
    rendered = repr(info)
    assert base64.b64encode(key).decode() not in rendered
    assert key.hex() not in rendered

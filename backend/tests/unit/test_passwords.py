# SPDX-License-Identifier: AGPL-3.0-or-later
"""Argon2id password hashing and the unknown-user dummy hash (SECURITY.md A1/A2)."""

from __future__ import annotations

import argon2
import pytest

from app.auth import passwords
from app.auth.passwords import (
    DEFAULT_PARAMS,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PasswordParams,
    PasswordPolicyError,
    check_password_policy,
    dummy_verify,
    hash_password,
    needs_rehash,
    verify_password,
)

CHEAP = PasswordParams(memory_kib=1024, time_cost=1, parallelism=1)


def test_baseline_parameters_match_auth_md() -> None:
    assert DEFAULT_PARAMS.memory_kib == 64 * 1024
    assert DEFAULT_PARAMS.time_cost == 3
    assert DEFAULT_PARAMS.parallelism == 4


def test_default_hash_is_argon2id_with_baseline_params() -> None:
    hashed = hash_password("synthetic-password-1")
    assert hashed.encoded.startswith("$argon2id$")
    params = argon2.extract_parameters(hashed.encoded)
    assert (params.memory_cost, params.time_cost, params.parallelism) == (65536, 3, 4)
    assert params.type is argon2.Type.ID
    assert hashed.params_dict() == {
        "algorithm": "argon2id",
        "memory_kib": 65536,
        "time_cost": 3,
        "parallelism": 4,
        "hash_len": 32,
        "salt_len": 16,
    }
    assert verify_password(hashed.encoded, "synthetic-password-1")


def test_verify_and_salt() -> None:
    a = hash_password("synthetic-password-2", CHEAP)
    b = hash_password("synthetic-password-2", CHEAP)
    assert a.encoded != b.encoded  # random salt
    assert verify_password(a.encoded, "synthetic-password-2")
    assert not verify_password(a.encoded, "synthetic-password-3")
    assert not verify_password(a.encoded, "")


@pytest.mark.parametrize("bad", ["", "not-a-hash", "$argon2id$v=19$m=1,t=1,p=1$xx$yy"])
def test_invalid_hash_never_raises(bad: str) -> None:
    assert verify_password(bad, "synthetic-password") is False


def test_absurdly_long_input_is_rejected_without_error() -> None:
    hashed = hash_password("synthetic-password-4", CHEAP)
    assert verify_password(hashed.encoded, "x" * (MAX_PASSWORD_LENGTH * 4 + 1)) is False


def test_needs_rehash_when_parameters_are_weaker() -> None:
    weak = hash_password("synthetic-password-5", CHEAP)
    assert needs_rehash(weak.encoded)
    assert not needs_rehash(weak.encoded, CHEAP)
    assert needs_rehash("garbage")


def test_dummy_verify_does_a_full_baseline_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """A2: an unknown username costs one real Argon2id verification at baseline cost."""
    seen: list[str] = []
    real_verify = argon2.PasswordHasher.verify

    def spy(self: argon2.PasswordHasher, hash: str, password: str | bytes) -> bool:
        seen.append(hash)
        return real_verify(self, hash, password)

    monkeypatch.setattr(argon2.PasswordHasher, "verify", spy)
    assert dummy_verify("synthetic-password-6") is False
    assert dummy_verify(None) is False
    assert len(seen) == 2
    params = argon2.extract_parameters(seen[0])
    assert (params.memory_cost, params.time_cost, params.parallelism) == (65536, 3, 4)
    assert seen[0] == seen[1] == passwords._dummy_hash()


def test_policy_lengths() -> None:
    check_password_policy("a" * MIN_PASSWORD_LENGTH)
    check_password_policy("a" * MAX_PASSWORD_LENGTH)
    with pytest.raises(PasswordPolicyError) as short:
        check_password_policy("a" * (MIN_PASSWORD_LENGTH - 1))
    assert short.value.code == "too-short"
    with pytest.raises(PasswordPolicyError) as long:
        check_password_policy("a" * (MAX_PASSWORD_LENGTH + 1))
    assert long.value.code == "too-long"

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Password hashing with Argon2id (AUTH.md "Passwords", SECURITY.md A1/A2).

* Baseline parameters: memory 64 MiB, 3 iterations, parallelism 4, 16-byte
  salt, 32-byte hash (:data:`DEFAULT_PARAMS`). The parameters are encoded in
  the PHC hash string and also stored next to it (``credential.hash_params``)
  so they can be raised later; :func:`needs_rehash` tells the login flow to
  rehash after the next successful verification.
* Verification is argon2's constant-time comparison. :func:`dummy_verify`
  runs a full verification against a fixed hash with the current parameters;
  the login flow calls it for unknown usernames so the response time does not
  reveal whether an account exists (A2).
* :func:`check_password_policy`: length 12..128 characters, no composition
  rules (AUTH.md). The breached-password list check belongs to the local
  login flow.
"""

from __future__ import annotations

import contextlib
import secrets
import threading
from dataclasses import asdict, dataclass
from typing import Any, Final

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH: Final = 12
MAX_PASSWORD_LENGTH: Final = 128
ALGORITHM: Final = "argon2id"


@dataclass(frozen=True)
class PasswordParams:
    """Argon2id cost parameters (``memory_kib`` in KiB)."""

    memory_kib: int = 64 * 1024
    time_cost: int = 3
    parallelism: int = 4
    hash_len: int = 32
    salt_len: int = 16

    def as_dict(self) -> dict[str, Any]:
        return {"algorithm": ALGORITHM, **asdict(self)}

    def hasher(self) -> PasswordHasher:
        return PasswordHasher(
            time_cost=self.time_cost,
            memory_cost=self.memory_kib,
            parallelism=self.parallelism,
            hash_len=self.hash_len,
            salt_len=self.salt_len,
            type=Type.ID,
        )


DEFAULT_PARAMS: Final = PasswordParams()


class PasswordPolicyError(ValueError):
    """The password does not meet the policy; ``code`` is a stable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PasswordHash:
    """A PHC-format Argon2id hash plus the parameters it was made with."""

    encoded: str
    params: PasswordParams

    def params_dict(self) -> dict[str, Any]:
        return self.params.as_dict()


def check_password_policy(password: str) -> None:
    """Raise :class:`PasswordPolicyError` unless 12 <= len(password) <= 128."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            "too-short", f"Passwords must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            "too-long", f"Passwords must be at most {MAX_PASSWORD_LENGTH} characters."
        )


def hash_password(password: str, params: PasswordParams = DEFAULT_PARAMS) -> PasswordHash:
    """Hash ``password`` with a fresh random salt."""
    return PasswordHash(params.hasher().hash(password), params)


def verify_password(encoded: str, password: str) -> bool:
    """True when ``password`` matches ``encoded``; never raises for bad input.

    The parameters are read from ``encoded`` itself, so hashes made with older
    parameters still verify.
    """
    if len(password) > MAX_PASSWORD_LENGTH * 4:  # bound work on absurd input
        _burn()
        return False
    try:
        return DEFAULT_PARAMS.hasher().verify(encoded, password)
    except VerifyMismatchError:
        return False
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(encoded: str, params: PasswordParams = DEFAULT_PARAMS) -> bool:
    """True when ``encoded`` was made with parameters other than ``params``."""
    try:
        return params.hasher().check_needs_rehash(encoded)
    except InvalidHashError:
        return True


_dummy_lock = threading.Lock()
_dummy: str | None = None


def _dummy_hash() -> str:
    global _dummy
    with _dummy_lock:
        if _dummy is None:
            _dummy = hash_password(secrets.token_urlsafe(32)).encoded
        return _dummy


def _burn() -> None:
    with contextlib.suppress(VerificationError):
        DEFAULT_PARAMS.hasher().verify(_dummy_hash(), secrets.token_urlsafe(16))


def dummy_verify(password: str | None = None) -> bool:
    """Spend the time of one real verification and return ``False``.

    Called when the username is unknown (or the account has no password), so
    "no such user" and "wrong password" cost the same (SECURITY.md A2).
    """
    candidate = password if password and len(password) <= MAX_PASSWORD_LENGTH * 4 else "x"
    try:
        DEFAULT_PARAMS.hasher().verify(_dummy_hash(), candidate)
    except VerificationError:
        return False
    return False  # pragma: no cover - a random dummy password never matches

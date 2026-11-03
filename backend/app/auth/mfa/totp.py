# SPDX-License-Identifier: AGPL-3.0-or-later
"""TOTP second factor (RFC 6238 via pyotp; AUTH.md "Multi-factor", SECURITY.md A7).

* **Enrolment** (:func:`start_enrollment`, :func:`confirm_enrollment`): the
  server generates a 160-bit secret, keeps it encrypted in an
  ``mfa_challenge`` row of the session (``app.core.crypto``, bound to that
  row) and returns it once with an ``otpauth://`` URI for the QR code. Only
  a correct first code turns it into an ``mfa_method`` row; the secret is
  then re-encrypted bound to the method row
  (``column_ad("mfa_method", "secret_encrypted", id)``), so a ciphertext
  cannot be moved to another user's row.
* **Verification** (:func:`verify`): 6 digits, 30-second steps, a window of
  one step either side (:data:`WINDOW`). The time step (counter) of every
  accepted code is stored in ``totp_last_counter`` and only *later* steps are
  accepted afterwards, so each code (and every older one) is accepted once
  (A7). The method row is locked (``FOR UPDATE``) while checking, so two
  concurrent requests cannot both spend the same code.
"""

from __future__ import annotations

import hmac
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

import pyotp
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth.mfa import challenges
from app.auth.mfa.models import ChallengePurpose
from app.auth.models import MfaMethod, MfaType
from app.core import clock
from app.core.crypto import DecryptionError, Keyring, column_ad
from app.core.ids import uuid7
from app.core.logging import get_logger

log = get_logger(__name__)

DIGITS: Final = 6
PERIOD: Final = 30
#: Steps accepted either side of the current one (clock drift).
WINDOW: Final = 1
SECRET_LENGTH: Final = 32  # base32 characters = 160 bits (RFC 4226 recommendation)
ISSUER: Final = "PerFin"
_CODE_RE: Final = re.compile(r"^[0-9]{6}$")


class EnrollmentNotFoundError(LookupError):
    """No live TOTP enrolment for this session (never started, or expired)."""


def generate_secret() -> str:
    return pyotp.random_base32(length=SECRET_LENGTH)


def method_ad(method_id: uuid.UUID) -> bytes:
    return column_ad("mfa_method", "secret_encrypted", method_id)


def enrollment_ad(challenge_id: uuid.UUID) -> bytes:
    return column_ad("mfa_challenge", "secret_encrypted", challenge_id)


def normalize_code(code: str) -> str | None:
    """Digits only (spaces allowed between), else ``None``."""
    cleaned = "".join(code.split())
    return cleaned if _CODE_RE.fullmatch(cleaned) else None


def counter_at(now: datetime) -> int:
    """The RFC 6238 time step containing ``now``."""
    return int(now.timestamp()) // PERIOD


def match_counter(
    secret: str, code: str, *, last_counter: int | None, now: datetime | None = None
) -> int | None:
    """The time step ``code`` belongs to, if it is within the window and newer
    than ``last_counter``; else ``None``. Every candidate step is compared
    (constant-time) so timing does not reveal which one matched."""
    normalized = normalize_code(code)
    now = now or clock.utcnow()
    current = counter_at(now)
    totp = pyotp.TOTP(secret, digits=DIGITS, interval=PERIOD)
    matched: int | None = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        expected = totp.generate_otp(step)
        same = hmac.compare_digest(expected.encode(), (normalized or "x").encode())
        fresh = last_counter is None or step > last_counter
        if same and fresh and normalized is not None and matched is None:
            matched = step
    return matched


def provisioning_uri(secret: str, account_name: str) -> str:
    return pyotp.TOTP(secret, digits=DIGITS, interval=PERIOD).provisioning_uri(
        name=account_name, issuer_name=ISSUER
    )


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TotpEnrollment:
    """Returned once to the user: the secret and its ``otpauth://`` URI."""

    secret: str = field(repr=False)
    otpauth_uri: str = field(repr=False)
    expires_at: datetime


def user_totp_methods(db: Session, user_id: uuid.UUID, *, lock: bool = False) -> list[MfaMethod]:
    stmt = (
        select(MfaMethod)
        .where(MfaMethod.user_id == user_id, MfaMethod.type == MfaType.TOTP)
        .order_by(MfaMethod.created_at, MfaMethod.id)
    )
    if lock:
        stmt = stmt.with_for_update()
    return list(db.execute(stmt).scalars())


def start_enrollment(
    db: Session,
    keyring: Keyring,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    account_name: str,
    now: datetime | None = None,
) -> TotpEnrollment:
    """Generate a secret, park it (encrypted) on the session, return it once."""
    now = now or clock.utcnow()
    secret = generate_secret()
    row_id = uuid7()
    row = challenges.new_challenge_row(
        session_id=session_id,
        user_id=user_id,
        purpose=ChallengePurpose.TOTP_ENROLL,
        ttl=challenges.TOTP_ENROLL_TTL,
        row_id=row_id,
        secret_encrypted=keyring.encrypt_str(secret, ad=enrollment_ad(row_id)),
        now=now,
    )
    challenges.replace(db, row)
    return TotpEnrollment(secret, provisioning_uri(secret, account_name), row.expires_at)


def confirm_enrollment(
    db: Session,
    keyring: Keyring,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    code: str,
    name: str,
    now: datetime | None = None,
) -> MfaMethod | None:
    """Turn the parked secret into a TOTP factor if ``code`` is right.

    Returns ``None`` for a wrong code (the enrolment stays for another try
    until it expires); raises :class:`EnrollmentNotFoundError` when there is none.
    The accepted step is stored, so the confirming code cannot log in again.
    """
    now = now or clock.utcnow()
    row = challenges.load_for_update(
        db, session_id=session_id, purpose=ChallengePurpose.TOTP_ENROLL, now=now
    )
    if row is None or row.user_id != user_id or row.secret_encrypted is None:
        raise EnrollmentNotFoundError
    secret = keyring.decrypt_str(row.secret_encrypted, ad=enrollment_ad(row.id))
    step = match_counter(secret, code, last_counter=None, now=now)
    if step is None:
        return None
    method_id = uuid7()
    method = MfaMethod(
        id=method_id,
        user_id=user_id,
        type=MfaType.TOTP,
        name=name,
        secret_encrypted=keyring.encrypt_str(secret, ad=method_ad(method_id)),
        totp_last_counter=step,
        last_used_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(method)
    db.delete(row)
    db.flush()
    return method


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify(
    db: Session,
    keyring: Keyring,
    *,
    user_id: uuid.UUID,
    code: str,
    now: datetime | None = None,
) -> MfaMethod | None:
    """The user's TOTP method that accepts ``code`` now (replay-safe), else ``None``.

    On success ``totp_last_counter`` and ``last_used_at`` are updated in the
    caller's transaction.
    """
    now = now or clock.utcnow()
    for method in user_totp_methods(db, user_id, lock=True):
        if method.secret_encrypted is None:  # pragma: no cover - check constraint
            continue
        try:
            secret = keyring.decrypt_str(method.secret_encrypted, ad=method_ad(method.id))
        except DecryptionError:
            log.warning("totp secret could not be decrypted", extra={"mfa_method_id": method.id})
            continue
        step = match_counter(secret, code, last_counter=method.totp_last_counter, now=now)
        if step is not None:
            method.totp_last_counter = step
            method.last_used_at = now
            method.updated_at = now
            db.flush()
            return method
    return None


def remove(db: Session, user_id: uuid.UUID, method_id: uuid.UUID | None = None) -> int:
    """Delete the user's TOTP method(s); return how many."""
    stmt = delete(MfaMethod).where(MfaMethod.user_id == user_id, MfaMethod.type == MfaType.TOTP)
    if method_id is not None:
        stmt = stmt.where(MfaMethod.id == method_id)
    result = db.execute(stmt)
    return int(result.rowcount)  # type: ignore[attr-defined]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recovery codes (AUTH.md "Multi-factor", SECURITY.md A8).

* Ten single-use codes per user (:data:`CODE_COUNT`), each 16 symbols of
  Crockford base32 (80 random bits), shown once as ``XXXX-XXXX-XXXX-XXXX``.
  Input is normalised (case, dashes, spaces, ``O``->``0``, ``I``/``L``->``1``).
* Stored as one ``mfa_method`` row of type ``recovery`` per code with an
  **Argon2id** hash (:data:`RECOVERY_HASH_PARAMS`, PHC string in
  ``code_hash``); never the code. The codes carry 80 bits of entropy, so a
  lighter cost than for passwords keeps verification of ten candidates fast
  while an offline search stays out of reach.
* :func:`consume` marks a matching code ``consumed_at`` under a row lock, so
  a code works once even under concurrent use.
* :func:`regenerate` deletes every earlier code of the user (used or not)
  and returns ten new ones.

Rate limiting, lockout, audit and the notification on use live in
:mod:`app.auth.mfa.stepup`.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth.models import MfaMethod, MfaType
from app.auth.passwords import PasswordParams, verify_password
from app.core import clock

CODE_COUNT: Final = 10
CODE_LENGTH: Final = 16
GROUP: Final = 4
ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
_CONFUSABLES: Final = str.maketrans({"O": "0", "I": "1", "L": "1"})
RECOVERY_HASH_PARAMS: Final = PasswordParams(memory_kib=19 * 1024, time_cost=2, parallelism=1)


def generate_code() -> str:
    """A fresh code, formatted for display."""
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return format_code(raw)


def format_code(normalized: str) -> str:
    return "-".join(normalized[i : i + GROUP] for i in range(0, len(normalized), GROUP))


def normalize(code: str) -> str | None:
    """Canonical form of a typed code, or ``None`` if it cannot be one."""
    if len(code) > 64:
        return None
    cleaned = "".join(ch for ch in code.upper() if ch not in "- \t").translate(_CONFUSABLES)
    if len(cleaned) != CODE_LENGTH or any(ch not in ALPHABET for ch in cleaned):
        return None
    return cleaned


def hash_code(normalized: str) -> str:
    return RECOVERY_HASH_PARAMS.hasher().hash(normalized)


def regenerate(db: Session, user_id: uuid.UUID, *, now: datetime | None = None) -> list[str]:
    """Replace all of the user's recovery codes with :data:`CODE_COUNT` new ones."""
    now = now or clock.utcnow()
    delete_all(db, user_id)
    codes = [generate_code() for _ in range(CODE_COUNT)]
    for index, code in enumerate(codes, start=1):
        normalized = normalize(code)
        if normalized is None:  # pragma: no cover - generated codes are always valid
            raise RuntimeError("generated an invalid recovery code")
        db.add(
            MfaMethod(
                user_id=user_id,
                type=MfaType.RECOVERY,
                name=f"Recovery code {index}",
                code_hash=hash_code(normalized),
                created_at=now,
                updated_at=now,
            )
        )
    db.flush()
    return codes


def consume(
    db: Session, user_id: uuid.UUID, code: str, *, now: datetime | None = None
) -> MfaMethod | None:
    """Spend the unused code matching ``code``; ``None`` when none matches."""
    now = now or clock.utcnow()
    normalized = normalize(code)
    rows = db.execute(
        select(MfaMethod)
        .where(
            MfaMethod.user_id == user_id,
            MfaMethod.type == MfaType.RECOVERY,
            MfaMethod.consumed_at.is_(None),
        )
        .order_by(MfaMethod.id)
        .with_for_update()
    ).scalars()
    if normalized is None:
        return None
    for row in rows:
        if row.code_hash is not None and verify_password(row.code_hash, normalized):
            row.consumed_at = now
            row.last_used_at = now
            row.updated_at = now
            db.flush()
            return row
    return None


def remaining(db: Session, user_id: uuid.UUID) -> int:
    """How many unused recovery codes the user has."""
    count = db.execute(
        select(func.count())
        .select_from(MfaMethod)
        .where(
            MfaMethod.user_id == user_id,
            MfaMethod.type == MfaType.RECOVERY,
            MfaMethod.consumed_at.is_(None),
        )
    ).scalar_one()
    return int(count)


def delete_all(db: Session, user_id: uuid.UUID) -> int:
    result = db.execute(
        delete(MfaMethod).where(MfaMethod.user_id == user_id, MfaMethod.type == MfaType.RECOVERY)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]

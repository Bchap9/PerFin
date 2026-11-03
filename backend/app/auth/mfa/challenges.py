# SPDX-License-Identifier: AGPL-3.0-or-later
"""Issue and consume ``mfa_challenge`` rows (see :mod:`app.auth.mfa.models`).

* :func:`replace` stores a new ceremony for a session, deleting any earlier
  one of the same purpose, in the caller's transaction.
* :func:`consume` deletes the session's ceremony of a purpose and returns it
  **in its own committed transaction**: a WebAuthn challenge is spent before
  the response is checked, so a failed verification (whose request
  transaction rolls back) cannot leave it reusable.
* :func:`load_for_update` reads a ceremony in the caller's transaction (TOTP
  enrolment, which may be retried with a corrected code until it expires).

Expired rows are never returned.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth.mfa.models import ChallengePurpose, MfaChallenge
from app.core import clock

#: How long a WebAuthn challenge may be answered.
WEBAUTHN_CHALLENGE_TTL: Final = timedelta(minutes=5)
#: How long a generated TOTP secret waits for its first code.
TOTP_ENROLL_TTL: Final = timedelta(minutes=15)


@dataclass(frozen=True)
class SpentChallenge:
    """A consumed ceremony (detached from any session)."""

    id: uuid.UUID
    user_id: uuid.UUID
    challenge: bytes | None
    secret_encrypted: bytes | None
    expires_at: datetime


def replace(db: Session, row: MfaChallenge) -> MfaChallenge:
    """Store ``row``, replacing the session's earlier ceremony of the same purpose."""
    db.execute(
        delete(MfaChallenge).where(
            MfaChallenge.session_id == row.session_id, MfaChallenge.purpose == row.purpose
        )
    )
    db.add(row)
    db.flush()
    return row


def new_challenge_row(
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    purpose: ChallengePurpose,
    ttl: timedelta,
    challenge: bytes | None = None,
    row_id: uuid.UUID | None = None,
    secret_encrypted: bytes | None = None,
    now: datetime | None = None,
) -> MfaChallenge:
    now = now or clock.utcnow()
    row = MfaChallenge(
        session_id=session_id,
        user_id=user_id,
        purpose=purpose.value,
        challenge=challenge,
        secret_encrypted=secret_encrypted,
        expires_at=now + ttl,
        created_at=now,
        updated_at=now,
    )
    if row_id is not None:
        row.id = row_id
    return row


def consume(
    session_factory: sessionmaker[Session],
    *,
    session_id: uuid.UUID,
    purpose: ChallengePurpose,
    now: datetime | None = None,
) -> SpentChallenge | None:
    """Delete and return the session's live ceremony of ``purpose`` (own transaction)."""
    now = now or clock.utcnow()
    with session_factory() as db, db.begin():
        rows = db.execute(
            delete(MfaChallenge)
            .where(MfaChallenge.session_id == session_id, MfaChallenge.purpose == purpose.value)
            .returning(
                MfaChallenge.id,
                MfaChallenge.user_id,
                MfaChallenge.challenge,
                MfaChallenge.secret_encrypted,
                MfaChallenge.expires_at,
            )
        ).all()
    live = [r for r in rows if r.expires_at > now]
    if not live:
        return None
    r = live[0]
    return SpentChallenge(r.id, r.user_id, r.challenge, r.secret_encrypted, r.expires_at)


def load_for_update(
    db: Session,
    *,
    session_id: uuid.UUID,
    purpose: ChallengePurpose,
    now: datetime | None = None,
) -> MfaChallenge | None:
    """The session's live ceremony of ``purpose``, row-locked, or ``None``."""
    now = now or clock.utcnow()
    return db.execute(
        select(MfaChallenge)
        .where(
            MfaChallenge.session_id == session_id,
            MfaChallenge.purpose == purpose.value,
            MfaChallenge.expires_at > now,
        )
        .with_for_update()
    ).scalar_one_or_none()


def prune(db: Session, *, now: datetime | None = None) -> int:
    """Delete expired ceremonies; return how many."""
    now = now or clock.utcnow()
    result = db.execute(delete(MfaChallenge).where(MfaChallenge.expires_at <= now))
    return int(result.rowcount)  # type: ignore[attr-defined]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""New-device sign-in detection for notifications.

A sign-in is from a **new device** when the user has signed in before (some
other ``user_session`` row exists) but no other session row carries the same
``User-Agent``. Session rows are kept until the hourly maintenance prunes
them after their expiry, so a browser unused for longer than that counts as
new again. Only a fact is routed (``security.new_device``); the user agent,
IP address and user are never part of the notification.
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.auth.models import UserSession
from app.auth.principal import memberships
from app.db.rls import get_context, restore_context, set_context
from app.domain.notifications.routing import notify

EVENT: str = "security.new_device"


def is_new_device(db: Session, session_row: UserSession) -> bool:
    others = select(UserSession.id).where(
        UserSession.user_id == session_row.user_id, UserSession.id != session_row.id
    )
    if not db.execute(select(exists(others))).scalar_one():
        return False  # the first sign-in ever: nothing to compare with
    if session_row.user_agent is None:
        same = others.where(UserSession.user_agent.is_(None))
    else:
        same = others.where(UserSession.user_agent == session_row.user_agent)
    return not db.execute(select(exists(same))).scalar_one()


def note_sign_in(db: Session, session_row: UserSession) -> list[uuid.UUID]:
    """Route ``security.new_device`` to each of the user's households when new."""
    if not is_new_device(db, session_row):
        return []
    saved = get_context(db)
    jobs: list[uuid.UUID] = []
    try:
        for member in memberships(db, session_row.user_id):
            set_context(db, member.household_id)
            jobs.extend(notify(db, member.household_id, EVENT, source_id=session_row.id))
    finally:
        restore_context(db, saved)
    return jobs

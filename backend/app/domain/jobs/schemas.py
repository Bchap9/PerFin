# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /jobs/{id}`` output: state only (no payload, no error text)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.db.models import JobState


class JobOut(BaseModel):
    """A job's progress through its life cycle.

    ``finished`` is true once the job will not run again on its own
    (``succeeded``, ``failed`` or ``dead``). The payload and the last error
    are never returned (they are for operators, not for API callers).
    """

    id: uuid.UUID
    type: str
    state: JobState
    attempts: int
    max_attempts: int
    finished: bool
    created_at: datetime
    updated_at: datetime

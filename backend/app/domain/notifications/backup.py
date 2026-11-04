# SPDX-License-Identifier: AGPL-3.0-or-later
"""``perfin notify backup-failed``: tell every household that a backup failed.

Backups are made outside the API (a scheduled script or the host's backup
tool, see docs/security/notifications.md). Such a job runs, when it fails::

    docker compose exec -T worker perfin notify backup-failed

which routes ``backup.failed`` to each household's channels subscribed to
``backup_failed`` (one notification per household per run of the command).
The command connects as the application role (``PERFIN_DATABASE_URL``) and
works household by household under RLS.
"""

from __future__ import annotations

import sys
import uuid

from pydantic import ValidationError
from sqlalchemy import pool, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.ids import uuid7
from app.db.rls import set_context
from app.domain.notifications.routing import notify

EVENT = "backup.failed"


def notify_backup_failed(db: Session) -> int:
    """Queue ``backup.failed`` for every household; return the number of jobs."""
    source = uuid7()
    households = [
        value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        for value in db.execute(text("SELECT * FROM perfin_household_ids()")).scalars()
    ]
    queued = 0
    for household_id in households:
        set_context(db, household_id)
        queued += len(notify(db, household_id, EVENT, source_id=f"backup:{source}"))
    set_context(db, None)
    return queued


def run_cli() -> int:
    from app.core.settings import get_settings
    from app.db.session import make_app_engine, make_sessionmaker

    try:
        settings = get_settings()
    except ValidationError:
        print("perfin notify: invalid configuration (run 'perfin config check')", file=sys.stderr)
        return 1
    engine = make_app_engine(settings, poolclass=pool.NullPool)
    try:
        with make_sessionmaker(engine)() as db, db.begin():
            queued = notify_backup_failed(db)
    except SQLAlchemyError as exc:
        print(f"perfin notify: database error ({type(exc).__name__})", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(f"queued {queued} notification(s)")
    return 0

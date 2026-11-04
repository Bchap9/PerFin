# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reminders as a tax year closes ("3 expected documents still missing"; ROADMAP Phase 6).

``tax_reminders`` (daily, instance-level; ``app.workers.handlers.tax``)
queues ``tax.reminders`` for every household (dedupe key per household and
day). On each day listed in ``PERFIN_TAX_REMINDER_DATES`` (``MM-DD`` of the
year *after* the tax year; default end of January to early April), every
**open** tax year of the previous calendar year with checklist items still
``expected`` gets one ``tax.reminder`` event::

    {"year": 2025, "missing": 3, "message": "3 expected documents still missing"}

(SSE ``GET /events``; notification channels pick the same event up in
Phase 7). Filed and closed years and complete checklists stay quiet. A day
missed while no worker ran is not caught up (the scheduler's slots), so the
dates are spread out. Each reminder is audited (``tax.reminder``, counts
only).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.events import emit
from app.domain.tax.models import TaxYear, TaxYearState
from app.domain.tax.service import checklist_counts

REMINDER_EVENT: Final = "tax.reminder"


def reminder_days(setting: str) -> frozenset[tuple[int, int]]:
    """``(month, day)`` pairs of ``PERFIN_TAX_REMINDER_DATES`` (``"01-31,02-15"``)."""
    days: set[tuple[int, int]] = set()
    for part in setting.split(","):
        part = part.strip()
        if not part:
            continue
        month, _, day = part.partition("-")
        days.add((int(month), int(day)))
    return frozenset(days)


def message(missing: int) -> str:
    noun = "document" if missing == 1 else "documents"
    return f"{missing} expected {noun} still missing"


@dataclass(frozen=True)
class Reminder:
    year: int
    missing: int


def send_reminders(db: Session, household_id: uuid.UUID, day: date, setting: str) -> list[Reminder]:
    """Emit the reminders due on ``day`` for one household (module doc)."""
    if (day.month, day.day) not in reminder_days(setting):
        return []
    sent: list[Reminder] = []
    years = db.execute(
        select(TaxYear).where(
            TaxYear.household_id == household_id,
            TaxYear.year == day.year - 1,
            TaxYear.state == TaxYearState.OPEN,
        )
    ).scalars()
    for tax_year in years:
        missing = checklist_counts(db, tax_year).expected
        if missing <= 0:
            continue
        emit(
            db,
            household_id,
            REMINDER_EVENT,
            {"year": tax_year.year, "missing": missing, "message": message(missing)},
        )
        audit(
            db,
            action="tax.reminder",
            target_type="tax_year",
            target_id=tax_year.id,
            after={"year": tax_year.year, "missing": missing, "day": day.isoformat()},
            household_id=household_id,
        )
        sent.append(Reminder(tax_year.year, missing))
    return sent

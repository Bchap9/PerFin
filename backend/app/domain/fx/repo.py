# SPDX-License-Identifier: AGPL-3.0-or-later
"""``fx_rate`` queries (explicit ``household_id`` filter; RLS is the backstop)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.fx.models import MANUAL_SOURCE, FxRate


def latest(db: Session, household_id: uuid.UUID, base: str, quote: str, on: date) -> FxRate | None:
    """The ``base -> quote`` row with the latest ``rate_date <= on``."""
    return db.execute(
        select(FxRate)
        .where(
            FxRate.household_id == household_id,
            FxRate.base == base,
            FxRate.quote == quote,
            FxRate.rate_date <= on,
        )
        .order_by(FxRate.rate_date.desc())
        .limit(1)
    ).scalar_one_or_none()


def upsert_rate(
    db: Session,
    household_id: uuid.UUID,
    base: str,
    quote: str,
    rate_date: date,
    rate: Decimal,
    *,
    source: str = MANUAL_SOURCE,
) -> FxRate:
    """Create or replace the ``base -> quote`` rate of ``rate_date``."""
    row = db.execute(
        select(FxRate).where(
            FxRate.household_id == household_id,
            FxRate.base == base,
            FxRate.quote == quote,
            FxRate.rate_date == rate_date,
        )
    ).scalar_one_or_none()
    if row is None:
        row = FxRate(
            household_id=household_id,
            base=base,
            quote=quote,
            rate_date=rate_date,
            rate=rate,
            source=source,
        )
        db.add(row)
    else:
        row.rate = rate
        row.source = source
    db.flush()
    return row

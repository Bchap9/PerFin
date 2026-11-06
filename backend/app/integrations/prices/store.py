# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetched quotes into ``price`` rows (DATA_MODEL.md "Investments").

Runs in one household's RLS context. A quote applies to every security of
the household with that exact symbol **and** the quote's currency (a quote
in another currency is skipped, never converted). One price per security
and day: a fetched quote creates the day's row or replaces a fetched one;
a ``manual`` row is never touched (manual prices win). Nothing here sends
anything anywhere.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.dates import in_business_range
from app.domain.investments.models import MANUAL_SOURCE, Price, Security
from app.integrations.prices.base import SYMBOL_RE, PriceQuote


def household_symbols(db: Session, household_id: uuid.UUID) -> set[str]:
    """The ticker symbols of the household's securities (valid ones only)."""
    rows = db.execute(
        select(Security.symbol)
        .where(Security.household_id == household_id, Security.symbol.is_not(None))
        .distinct()
    ).scalars()
    return {s for s in rows if s is not None and SYMBOL_RE.fullmatch(s)}


def store_quotes(
    db: Session,
    household_id: uuid.UUID,
    quotes: Iterable[PriceQuote],
    *,
    source: str,
    today: date,
) -> int:
    """Write ``quotes`` as ``source`` prices; return how many rows were written."""
    if source == MANUAL_SOURCE:
        raise ValueError("fetched prices cannot claim the manual source")
    by_symbol: Mapping[str, PriceQuote] = {
        q.symbol: q for q in quotes if in_business_range(q.price_date) and q.price_date <= today
    }
    if not by_symbol:
        return 0
    securities = db.execute(
        select(Security).where(
            Security.household_id == household_id, Security.symbol.in_(list(by_symbol))
        )
    ).scalars()
    written = 0
    for security in securities:
        if security.symbol is None:  # pragma: no cover - the query names a symbol
            continue
        quote = by_symbol[security.symbol]
        if quote.currency != security.currency:
            continue
        row = db.execute(
            select(Price).where(
                Price.household_id == household_id,
                Price.security_id == security.id,
                Price.price_date == quote.price_date,
            )
        ).scalar_one_or_none()
        if row is None:
            db.add(
                Price(
                    household_id=household_id,
                    security_id=security.id,
                    price_date=quote.price_date,
                    close=quote.close,
                    currency=security.currency,
                    source=source,
                )
            )
        elif row.source == MANUAL_SOURCE:
            continue
        else:
            row.close = quote.close
            row.source = source
        written += 1
    db.flush()
    return written

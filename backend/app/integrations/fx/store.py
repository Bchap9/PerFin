# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetched reference rates into ``fx_rate`` rows (DATA_MODEL.md "Ledger").

Runs in one household's RLS context. Conversion never triangulates
(``app.domain.fx.service``), so the rates a household needs are stored
directly against its base currency: for every currency ``C`` its accounts
and securities use (other than the base ``B``), a provider publishing rates
against one currency ``P`` (``1 P = r[X] X``, the ECB's euro) yields
``1 C = r[B] / r[C] B``, computed in ``Decimal`` (34 digits) and rounded once,
half-even, to the 12 stored decimals. A currency the provider does not
publish is skipped.

One rate per pair and day: a fetched rate creates the day's ``C -> B`` row or
replaces a fetched one; when a **manual** rate exists for that day in
either direction (``C -> B`` or ``B -> C``) nothing is written (manual rates
win, also over the lookup's tie rule). Nothing here sends anything anywhere.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.dates import in_business_range
from app.core.money import calc_context
from app.domain.accounts.models import Account
from app.domain.fx.models import MANUAL_SOURCE, FxRate
from app.domain.fx.service import base_currency
from app.domain.investments.models import Security
from app.integrations.fx.base import FxQuote
from app.integrations.fx.ecb import MAX_RATE, RATE_PLACES


def household_currencies(db: Session, household_id: uuid.UUID) -> set[str]:
    """Currencies of the household's accounts and securities."""
    accounts = db.execute(
        select(Account.currency).where(Account.household_id == household_id).distinct()
    ).scalars()
    securities = db.execute(
        select(Security.currency).where(Security.household_id == household_id).distinct()
    ).scalars()
    return {*accounts, *securities}


def cross_rates(quotes: Iterable[FxQuote], target: str, currencies: Iterable[str]) -> list[FxQuote]:
    """``C -> target`` rates for ``currencies`` from quotes against one pivot currency."""
    by_day: dict[tuple[str, date], dict[str, Decimal]] = {}
    for q in quotes:
        table = by_day.setdefault((q.base, q.rate_date), {q.base: Decimal(1)})
        table[q.quote] = q.rate
    out: list[FxQuote] = []
    for (_pivot, on), table in sorted(by_day.items(), key=lambda item: item[0][1]):
        if target not in table:
            continue
        for currency in sorted(set(currencies) - {target}):
            if currency not in table:
                continue
            with localcontext(calc_context()):
                value = table[target] / table[currency]
            rate = value.quantize(RATE_PLACES, rounding=ROUND_HALF_EVEN)
            if 0 < rate < MAX_RATE:
                out.append(FxQuote(base=currency, quote=target, rate_date=on, rate=rate))
    return out


def store_rates(
    db: Session,
    household_id: uuid.UUID,
    quotes: Iterable[FxQuote],
    *,
    source: str,
    today: date,
) -> int:
    """Write ``quotes`` as ``source`` rates (module doc); return rows written."""
    if source == MANUAL_SOURCE:
        raise ValueError("fetched rates cannot claim the manual source")
    written = 0
    for q in quotes:
        if q.rate_date > today or not in_business_range(q.rate_date) or q.base == q.quote:
            continue
        rows = list(
            db.execute(
                select(FxRate).where(
                    FxRate.household_id == household_id,
                    FxRate.rate_date == q.rate_date,
                    or_(
                        (FxRate.base == q.base) & (FxRate.quote == q.quote),
                        (FxRate.base == q.quote) & (FxRate.quote == q.base),
                    ),
                )
            ).scalars()
        )
        if any(r.source == MANUAL_SOURCE for r in rows):
            continue
        direct = next((r for r in rows if r.base == q.base), None)
        if direct is None:
            db.add(
                FxRate(
                    household_id=household_id,
                    base=q.base,
                    quote=q.quote,
                    rate_date=q.rate_date,
                    rate=q.rate,
                    source=source,
                )
            )
        else:
            direct.rate = q.rate
            direct.source = source
        written += 1
    db.flush()
    return written


def store_reference_rates(
    db: Session,
    household_id: uuid.UUID,
    quotes: list[FxQuote],
    *,
    source: str,
    today: date,
) -> int:
    """Cross ``quotes`` to the household's base currency and store them."""
    target = base_currency(db, household_id)
    needed = household_currencies(db, household_id)
    return store_rates(
        db, household_id, cross_rates(quotes, target, needed), source=source, today=today
    )

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Currency conversion with ``fx_rate`` (DATA_MODEL.md "Ledger").

Rules:

* A row ``(base, quote, rate_date, rate)`` means ``1 base = rate quote`` on
  that date.
* Converting ``amount`` from ``A`` to ``B`` on date ``d`` uses the **latest
  rate on or before** ``d``, looked up in both directions: a direct ``A->B``
  row multiplies, an inverse ``B->A`` row divides. If both exist, the one
  with the later ``rate_date`` wins (the direct row on a tie). There is no
  triangulation through a third currency: keep rates against the household's
  base currency. Rates after ``d`` are never used (no look-ahead), and there
  is no age limit: a stale rate is better than none for a personal ledger;
  callers that care can read :attr:`RateQuote.rate_date`.
* Same currency: the amount is returned unchanged (rounded to storage
  precision).
* Arithmetic is ``Decimal`` in a 34-digit context; the product (or quotient)
  is rounded **once**, half-even (``ROUND_HALF_EVEN``), to 6 decimals, the
  stored precision (``app.core.money.to_storage``). Totals in the base
  currency are summed from these and rounded to the currency's minor unit
  only for display (``app.core.money.round_to_minor``).
* A missing rate is :class:`FxRateNotFoundError` (``422 fx-rate-missing``); bulk
  callers use :class:`FxConverter`, whose ``try_convert`` returns ``None``
  instead, so a report can flag the unconverted amounts.
"""

from __future__ import annotations

import bisect
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import ProblemError
from app.core.money import calc_context, to_storage
from app.db.models import Household
from app.domain.fx import repo
from app.domain.fx.models import FxRate


class FxRateNotFoundError(ProblemError):
    def __init__(self, source: str, target: str, on: date) -> None:
        super().__init__(
            422,
            "fx-rate-missing",
            "Exchange rate missing",
            f"No {source}/{target} exchange rate on or before {on.isoformat()}.",
        )
        self.source = source
        self.target = target
        self.on = on


@dataclass(frozen=True)
class RateQuote:
    """A usable rate for ``source -> target``: the stored row and its direction."""

    source: str
    target: str
    rate_date: date
    stored_rate: Decimal
    inverted: bool

    @property
    def rate(self) -> Decimal:
        """Units of ``target`` per unit of ``source`` (34 significant digits)."""
        if not self.inverted:
            return self.stored_rate
        with localcontext(calc_context()):
            return Decimal(1) / self.stored_rate

    def apply(self, amount: Decimal) -> Decimal:
        """``amount`` in ``target``, rounded half-even to 6 decimals."""
        with localcontext(calc_context()):
            value = amount / self.stored_rate if self.inverted else amount * self.stored_rate
        return to_storage(value)


def _choose(
    direct: FxRate | None, inverse: FxRate | None, source: str, target: str
) -> RateQuote | None:
    if direct is not None and (inverse is None or direct.rate_date >= inverse.rate_date):
        return RateQuote(source, target, direct.rate_date, direct.rate, inverted=False)
    if inverse is not None:
        return RateQuote(source, target, inverse.rate_date, inverse.rate, inverted=True)
    return None


def latest_rate(
    db: Session, household_id: uuid.UUID, source: str, target: str, on: date
) -> RateQuote | None:
    """The rate for ``source -> target`` on ``on`` (module doc), or ``None``."""
    direct = repo.latest(db, household_id, source, target, on)
    inverse = repo.latest(db, household_id, target, source, on)
    return _choose(direct, inverse, source, target)


def convert(
    db: Session, household_id: uuid.UUID, amount: Decimal, source: str, target: str, on: date
) -> Decimal:
    """``amount`` of ``source`` in ``target`` on ``on``; no rate: :class:`FxRateNotFoundError`."""
    if source == target:
        return to_storage(amount)
    quote = latest_rate(db, household_id, source, target, on)
    if quote is None:
        raise FxRateNotFoundError(source, target, on)
    return quote.apply(amount)


def base_currency(db: Session, household_id: uuid.UUID) -> str:
    currency = db.execute(
        select(Household.base_currency).where(Household.id == household_id)
    ).scalar_one_or_none()
    if currency is None:
        raise ProblemError(404, "household-not-found", "Household not found")
    return currency


def to_base(
    db: Session, household_id: uuid.UUID, amount: Decimal, currency: str, on: date
) -> Decimal:
    """``amount`` in the household's base currency on ``on``."""
    return convert(db, household_id, amount, currency, base_currency(db, household_id), on)


class FxConverter:
    """Converts many amounts into one ``target`` currency with one query per currency.

    Loads every rate of a pair (both directions, up to ``until``) the first
    time the pair is needed, then answers each lookup with a binary search.
    Same rules as :func:`convert`.
    """

    def __init__(
        self,
        db: Session,
        household_id: uuid.UUID,
        target: str | None = None,
        *,
        until: date | None = None,
    ) -> None:
        self._db = db
        self._household_id = household_id
        self.target = target or base_currency(db, household_id)
        self._until = until
        self._pairs: dict[str, tuple[list[date], list[FxRate], list[date], list[FxRate]]] = {}

    def _load(self, source: str) -> tuple[list[date], list[FxRate], list[date], list[FxRate]]:
        cached = self._pairs.get(source)
        if cached is not None:
            return cached
        stmt = select(FxRate).where(
            FxRate.household_id == self._household_id,
            or_(
                (FxRate.base == source) & (FxRate.quote == self.target),
                (FxRate.base == self.target) & (FxRate.quote == source),
            ),
        )
        if self._until is not None:
            stmt = stmt.where(FxRate.rate_date <= self._until)
        rows = sorted(self._db.execute(stmt).scalars(), key=lambda r: r.rate_date)
        direct = [r for r in rows if r.base == source]
        inverse = [r for r in rows if r.base == self.target]
        loaded = ([r.rate_date for r in direct], direct, [r.rate_date for r in inverse], inverse)
        self._pairs[source] = loaded
        return loaded

    def quote(self, source: str, on: date) -> RateQuote | None:
        if self._until is not None and on > self._until:
            raise ValueError("date after the converter's 'until'")
        d_dates, direct, i_dates, inverse = self._load(source)
        d_at = bisect.bisect_right(d_dates, on)
        i_at = bisect.bisect_right(i_dates, on)
        return _choose(
            direct[d_at - 1] if d_at else None,
            inverse[i_at - 1] if i_at else None,
            source,
            self.target,
        )

    def try_convert(self, amount: Decimal, source: str, on: date) -> Decimal | None:
        if source == self.target:
            return to_storage(amount)
        quote = self.quote(source, on)
        return None if quote is None else quote.apply(amount)

    def convert(self, amount: Decimal, source: str, on: date) -> Decimal:
        converted = self.try_convert(amount, source, on)
        if converted is None:
            raise FxRateNotFoundError(source, self.target, on)
        return converted

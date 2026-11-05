# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prices and currency conversion for valuing holdings (shared by holdings and performance).

* :class:`PriceBook`: the close of a security on a day is the most recent of
  (a) its latest stored ``price`` on or before the day and (b) the latest
  trade price of a buy/sell/reinvest/transfer of it on or before the day
  among the transactions the caller can see; a stored price wins a tie.
  No look-ahead. A security with neither is unpriced (valued at nothing and
  listed in ``unpriced_security_ids``).
* :class:`Converter`: ``FxConverter`` into one currency that records every
  missing rate (``missing_rates``) instead of failing, so a response can say
  what it could not convert (DATA_MODEL.md "Ledger": latest rate on or
  before the day, both directions, no triangulation).
"""

from __future__ import annotations

import bisect
import uuid
from collections import defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.fx.service import FxConverter
from app.domain.investments import lots as engine
from app.domain.investments.models import InvestmentTxn, Price
from app.domain.investments.schemas import MissingRate

Basis = Literal["price", "trade"]


@dataclass(frozen=True)
class Close:
    close: Decimal
    on: date
    basis: Basis


class PriceBook:
    def __init__(
        self,
        db: Session,
        household_id: uuid.UUID,
        security_ids: Collection[uuid.UUID],
        *,
        until: date,
        trades: Iterable[InvestmentTxn] = (),
    ) -> None:
        self._stored: dict[uuid.UUID, tuple[list[date], list[Decimal]]] = {}
        self._traded: dict[uuid.UUID, tuple[list[date], list[Decimal]]] = {}
        stored: dict[uuid.UUID, list[tuple[date, Decimal]]] = defaultdict(list)
        if security_ids:
            rows = db.execute(
                select(Price.security_id, Price.price_date, Price.close)
                .where(
                    Price.household_id == household_id,
                    Price.security_id.in_(list(security_ids)),
                    Price.price_date <= until,
                )
                .order_by(Price.security_id, Price.price_date)
            ).all()
            for security_id, price_date, close in rows:
                stored[security_id].append((price_date, close))
        traded: dict[uuid.UUID, dict[date, Decimal]] = defaultdict(dict)
        for txn in sorted(trades, key=lambda t: (t.trade_date, t.id.bytes)):
            if (
                txn.security_id is not None
                and txn.type not in engine.RATIO_TYPES
                and txn.price is not None
                and txn.price > 0
                and txn.trade_date <= until
            ):
                traded[txn.security_id][txn.trade_date] = txn.price
        for security_id, points in stored.items():
            self._stored[security_id] = ([d for d, _ in points], [c for _, c in points])
        for security_id, by_day in traded.items():
            days = sorted(by_day)
            self._traded[security_id] = (days, [by_day[d] for d in days])

    @staticmethod
    def _latest(
        series: tuple[list[date], list[Decimal]] | None, on: date
    ) -> tuple[date, Decimal] | None:
        if series is None:
            return None
        at = bisect.bisect_right(series[0], on)
        return (series[0][at - 1], series[1][at - 1]) if at else None

    def close(self, security_id: uuid.UUID, on: date) -> Close | None:
        stored = self._latest(self._stored.get(security_id), on)
        traded = self._latest(self._traded.get(security_id), on)
        if stored is not None and (traded is None or stored[0] >= traded[0]):
            return Close(stored[1], stored[0], "price")
        if traded is not None:
            return Close(traded[1], traded[0], "trade")
        return None


class Converter:
    """Converts into ``target``; a missing rate gives ``None`` and is remembered."""

    def __init__(
        self, db: Session, household_id: uuid.UUID, target: str, *, until: date | None = None
    ) -> None:
        self.target = target
        self._fx = FxConverter(db, household_id, target, until=until)
        self._missing: set[tuple[str, date]] = set()

    def __call__(self, amount: Decimal, currency: str, on: date) -> Decimal | None:
        converted = self._fx.try_convert(amount, currency, on)
        if converted is None:
            self._missing.add((currency, on))
        return converted

    def missing(self) -> list[MissingRate]:
        # One entry per currency: its latest missing day.
        latest: dict[str, date] = {}
        for currency, on in self._missing:
            if on > latest.get(currency, date.min):
                latest[currency] = on
        return [MissingRate(currency=c, on=latest[c]) for c in sorted(latest)]

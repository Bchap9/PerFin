# SPDX-License-Identifier: AGPL-3.0-or-later
"""Holdings, unrealised gains and allocation (``GET /investments/holdings``).

Only accounts the caller can see (``service.visible_holdings`` /
``visible_lots``); ``?account=`` narrows to one (``404 account-not-found``
when hidden). As of today:

* per holding: quantity and cost basis from the ``holding`` cache (account
  currency), the close (``valuation.PriceBook``), market value in the
  account and base currencies, unrealised gain split into short and long
  term by each open lot's holding period (``lots.holding_term``), and its
  weight in the total;
* per holding too: the average cost per unit, where its close came from
  and when it was written (``price.source``, ``price.updated_at``: a
  statement, a manual entry or the price feed), and how it moved from the
  stored close before it (``price_change``);
* per account: the last day an imported statement or export covers
  (``positions_as_of``). Market values between imports are **estimates**:
  the latest price times the units of the last import, which the account
  may no longer hold. ``price_feed`` says whether prices are fetched and
  how often;
* totals and allocation by asset class, sector (``unclassified`` without
  one) and account in the household's base currency. Weights are fractions
  rounded half-even to 10 decimals. Values that cannot be priced or
  converted are left out of totals and named in ``unpriced_security_ids`` /
  ``missing_rates``.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.principal import Principal
from app.core.money import Money, calc_context, to_storage
from app.core.settings import get_settings
from app.domain.accounts.models import Account
from app.domain.accounts.visibility import get_visible_account
from app.domain.fx.service import base_currency
from app.domain.imports.models import StatementRecord
from app.domain.investments import lots as engine
from app.domain.investments import service
from app.domain.investments.lots import format_units, quantize_units
from app.domain.investments.models import Holding, InvestmentTxn, Lot, Price, Security
from app.domain.investments.returns import round_rate
from app.domain.investments.schemas import (
    AllocationOut,
    GainsOut,
    HoldingAccountOut,
    HoldingOut,
    HoldingsOut,
    LotOut,
    PriceBrief,
    PriceChange,
    PriceFeedOut,
    SecurityBrief,
)
from app.domain.investments.valuation import Close, Converter, PriceBook
from app.integrations.prices.refresh import price_feed_enabled

UNCLASSIFIED = "unclassified"


def _order(security: Security) -> str:
    """Holdings by symbol; a security without one by its name among them."""
    return security.symbol or security.name


def gains_out(short: Decimal, long: Decimal, currency: str) -> GainsOut:
    return GainsOut(
        short_term=Money.of(to_storage(short), currency),
        long_term=Money.of(to_storage(long), currency),
        total=Money.of(to_storage(short + long), currency),
    )


def weight(part: Decimal, total: Decimal) -> str:
    if total == 0:
        return "0"
    with localcontext(calc_context()):
        return f"{round_rate(part / total):f}"


def allocation(values: dict[str, Decimal], total: Decimal, currency: str) -> list[AllocationOut]:
    return [
        AllocationOut(
            key=key, value=Money.of(to_storage(value), currency), weight=weight(value, total)
        )
        for key, value in sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


@dataclass(frozen=True)
class _Stored:
    """One stored close of a security."""

    on: date
    close: Decimal
    source: str
    updated_at: datetime


def latest_stored(
    db: Session, household_id: uuid.UUID, security_ids: Collection[uuid.UUID], until: date
) -> dict[uuid.UUID, list[_Stored]]:
    """The two latest stored closes of each security on or before ``until``, newest first."""
    if not security_ids:
        return {}
    rank = (
        func.row_number()
        .over(partition_by=Price.security_id, order_by=Price.price_date.desc())
        .label("rank")
    )
    ranked = (
        select(
            Price.security_id, Price.price_date, Price.close, Price.source, Price.updated_at, rank
        )
        .where(
            Price.household_id == household_id,
            Price.security_id.in_(list(security_ids)),
            Price.price_date <= until,
        )
        .subquery()
    )
    found: dict[uuid.UUID, list[_Stored]] = defaultdict(list)
    rows = db.execute(
        select(ranked).where(ranked.c.rank <= 2).order_by(ranked.c.security_id, ranked.c.rank)
    )
    for security_id, on, close, source, updated_at, _ in rows:
        found[security_id].append(_Stored(on, close, source, updated_at))
    return found


def positions_as_of(
    db: Session, household_id: uuid.UUID, account_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, date]:
    """The last day an imported brokerage statement or export of each account covers."""
    if not account_ids:
        return {}
    rows = db.execute(
        select(StatementRecord.account_id, func.max(StatementRecord.period_end))
        .where(
            StatementRecord.household_id == household_id,
            StatementRecord.account_id.in_(list(account_ids)),
            StatementRecord.kind == "investment",
            StatementRecord.outcome == "imported",
        )
        .group_by(StatementRecord.account_id)
    )
    return {account_id: day for account_id, day in rows if day is not None}


def price_feed() -> PriceFeedOut:
    settings = get_settings()
    enabled = price_feed_enabled(settings)
    return PriceFeedOut(
        enabled=enabled, refresh_minutes=settings.price_refresh_minutes if enabled else None
    )


def _price_brief(close: Close, currency: str, stored: list[_Stored]) -> PriceBrief:
    latest = stored[0] if stored and close.basis == "price" and stored[0].on == close.on else None
    return PriceBrief(
        close=format_units(close.close),
        currency=currency,
        price_date=close.on,
        basis=close.basis,
        source=latest.source if latest is not None else None,
        updated_at=latest.updated_at if latest is not None else None,
    )


def _price_change(
    close: Close, stored: list[_Stored], value: Decimal | None, currency: str
) -> PriceChange | None:
    """The move from the stored close before the latest (``value``: per unit, converted)."""
    if close.basis != "price" or len(stored) < 2 or stored[0].on != close.on:
        return None
    previous = stored[1]
    if previous.close <= 0:
        return None
    return PriceChange(
        since=previous.on,
        previous_close=format_units(previous.close),
        value=Money.of(to_storage(value), currency) if value is not None else None,
        fraction=f"{round_rate((close.close - previous.close) / previous.close):f}",
    )


def holdings(
    db: Session, principal: Principal, *, account_id: uuid.UUID | None = None
) -> HoldingsOut:
    household_id = service.household_of(principal)
    as_of = service.today()
    holding_stmt = service.visible_holdings(principal)
    lot_stmt = service.visible_lots(principal).where(Lot.remaining_quantity > 0)
    txn_stmt = service.visible_investment_txns(principal).where(InvestmentTxn.price.is_not(None))
    if account_id is not None:
        get_visible_account(db, principal, account_id)
        holding_stmt = holding_stmt.where(Holding.account_id == account_id)
        lot_stmt = lot_stmt.where(Lot.account_id == account_id)
        txn_stmt = txn_stmt.where(InvestmentTxn.account_id == account_id)
    rows = list(db.execute(holding_stmt).scalars())
    open_lots: dict[tuple[uuid.UUID, uuid.UUID], list[Lot]] = defaultdict(list)
    for lot in db.execute(lot_stmt.order_by(Lot.open_date, Lot.id)).scalars():
        open_lots[(lot.account_id, lot.security_id)].append(lot)
    security_ids = sorted({r.security_id for r in rows}, key=lambda u: u.bytes)
    account_ids = sorted({r.account_id for r in rows}, key=lambda u: u.bytes)
    securities = {
        s.id: s
        for s in (
            db.execute(
                select(Security).where(
                    Security.household_id == household_id, Security.id.in_(security_ids)
                )
            ).scalars()
            if security_ids
            else []
        )
    }
    accounts = {
        a.id: a
        for a in (
            db.execute(
                select(Account).where(
                    Account.household_id == household_id, Account.id.in_(account_ids)
                )
            ).scalars()
            if account_ids
            else []
        )
    }
    methods = service.cost_methods(db, household_id, account_ids)
    book = PriceBook(
        db, household_id, security_ids, until=as_of, trades=db.execute(txn_stmt).scalars()
    )
    stored = latest_stored(db, household_id, security_ids, as_of)
    covered = positions_as_of(db, household_id, account_ids)
    base = base_currency(db, household_id)
    to_base = Converter(db, household_id, base, until=as_of)
    to_account: dict[str, Converter] = {}
    unpriced: set[uuid.UUID] = set()
    out: list[HoldingOut] = []
    base_values: dict[uuid.UUID, Decimal] = {}
    totals = {"value": Decimal(0), "cost": Decimal(0), "short": Decimal(0), "long": Decimal(0)}
    by_class: dict[str, Decimal] = defaultdict(Decimal)
    by_sector: dict[str, Decimal] = defaultdict(Decimal)
    by_account: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    with localcontext(calc_context()):
        for row in sorted(
            rows, key=lambda r: (_order(securities[r.security_id]), r.account_id.bytes)
        ):
            security = securities[row.security_id]
            account = accounts[row.account_id]
            convert = to_account.setdefault(
                account.currency, Converter(db, household_id, account.currency, until=as_of)
            )
            close = book.close(security.id, as_of)
            if close is None:
                unpriced.add(security.id)
            unit = convert(close.close, security.currency, as_of) if close is not None else None
            lots_out: list[LotOut] = []
            short = long = Decimal(0)
            for lot in open_lots.get((row.account_id, row.security_id), []):
                cost = lot.remaining_quantity * lot.cost_per_unit
                value = lot.remaining_quantity * unit if unit is not None else None
                term = engine.holding_term(lot.open_date, as_of)
                if value is not None:
                    if term == "long":
                        long += value - cost
                    else:
                        short += value - cost
                lots_out.append(
                    LotOut(
                        id=lot.id,
                        open_date=lot.open_date,
                        quantity=format_units(lot.quantity),
                        remaining_quantity=format_units(lot.remaining_quantity),
                        cost_per_unit=format_units(lot.cost_per_unit),
                        cost_basis=Money.of(to_storage(cost), account.currency),
                        market_value=(
                            Money.of(to_storage(value), account.currency)
                            if value is not None
                            else None
                        ),
                        unrealized_gain=(
                            Money.of(to_storage(value - cost), account.currency)
                            if value is not None
                            else None
                        ),
                        term=term,
                    )
                )
            market = row.quantity * unit if unit is not None else None
            closes = stored.get(security.id, [])
            moved: Decimal | None = None
            if close is not None and len(closes) > 1:
                per_unit = convert(close.close - closes[1].close, security.currency, as_of)
                moved = row.quantity * per_unit if per_unit is not None else None
            market_base = (
                to_base(row.quantity * close.close, security.currency, as_of)
                if close is not None
                else None
            )
            if market_base is not None and market is not None:
                cost_base = to_base(row.cost_basis, account.currency, as_of)
                short_base = to_base(short, account.currency, as_of)
                long_base = to_base(long, account.currency, as_of)
                if cost_base is not None and short_base is not None and long_base is not None:
                    totals["value"] += market_base
                    totals["cost"] += cost_base
                    totals["short"] += short_base
                    totals["long"] += long_base
                    base_values[row.id] = market_base
                    by_class[security.asset_class.value] += market_base
                    by_sector[security.sector or UNCLASSIFIED] += market_base
                    by_account[account.id] += market_base
            out.append(
                HoldingOut(
                    account_id=row.account_id,
                    security=SecurityBrief(
                        id=security.id,
                        symbol=security.symbol,
                        name=security.name,
                        asset_class=security.asset_class,
                        sector=security.sector,
                        currency=security.currency,
                    ),
                    quantity=format_units(row.quantity),
                    cost_basis=Money.of(row.cost_basis, account.currency),
                    average_cost=(
                        format_units(quantize_units(row.cost_basis / row.quantity))
                        if row.quantity > 0
                        else None
                    ),
                    price=(
                        _price_brief(close, security.currency, closes)
                        if close is not None
                        else None
                    ),
                    price_change=(
                        _price_change(close, closes, moved, account.currency)
                        if close is not None
                        else None
                    ),
                    market_value=(
                        Money.of(to_storage(market), account.currency)
                        if market is not None
                        else None
                    ),
                    market_value_base=(
                        Money.of(to_storage(market_base), base) if market_base is not None else None
                    ),
                    unrealized=(
                        gains_out(short, long, account.currency) if market is not None else None
                    ),
                    weight=None,
                    lots=lots_out,
                )
            )
    total = totals["value"]
    weighted = [
        h.model_copy(
            update={"weight": weight(base_values[r.id], total) if r.id in base_values else None}
        )
        for h, r in zip(
            out,
            sorted(rows, key=lambda r: (_order(securities[r.security_id]), r.account_id.bytes)),
            strict=True,
        )
    ]
    missing = {m.currency: m for m in [*to_base.missing()]}
    for converter in to_account.values():
        for m in converter.missing():
            missing.setdefault(m.currency, m)
    return HoldingsOut(
        as_of=as_of,
        base_currency=base,
        holdings=weighted,
        accounts=[
            HoldingAccountOut(
                account_id=a,
                cost_method=methods[a],
                market_value_base=Money.of(to_storage(by_account.get(a, Decimal(0))), base),
                positions_as_of=covered.get(a),
            )
            for a in account_ids
        ],
        price_feed=price_feed(),
        market_value=Money.of(to_storage(total), base),
        cost_basis=Money.of(to_storage(totals["cost"]), base),
        unrealized=gains_out(totals["short"], totals["long"], base),
        allocation={
            "asset_class": allocation(dict(by_class), total, base),
            "sector": allocation(dict(by_sector), total, base),
            "account": allocation({str(k): v for k, v in by_account.items()}, total, base),
        },
        unpriced_security_ids=sorted(unpriced, key=lambda u: u.bytes),
        missing_rates=[missing[c] for c in sorted(missing)],
    )

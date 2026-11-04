# SPDX-License-Identifier: AGPL-3.0-or-later
"""Investment performance (``GET /investments/performance?range=&account=``).

Scope: the securities of the accounts the caller can see (or the one
``account``), valued in the household's base currency. Cash in the account
is outside the portfolio: money moving between cash and securities is an
external flow.

* **Period**: ends today; ``range`` is ``1m``, ``3m``, ``6m``, ``ytd``,
  ``1y``, ``3y``, ``5y`` or ``max``. The starting value is the value at the
  end of the start day (for ``ytd`` the last day of the previous year), and
  the start never lies before the day before the first transaction (``max``
  starts there, from nothing). **Bound** (SECURITY.md F8): the work is one
  valuation per day and position, so the period is at most
  :data:`MAX_PERFORMANCE_DAYS` days (ten years, the report cap); ``max``
  over an older history starts that many days before today, from the value
  held then (``start`` in the response says so).
* **Daily valuation**: units held at the end of each day (the lot engine's
  positions, splits applied) times the close (``valuation.PriceBook``),
  converted on that day.
* **Flows** of a day, per transaction, in the base currency on the trade
  date: ``-amount`` (a purchase or a fee puts money in, a sale, dividend or
  interest takes it out); a transfer in adds and a transfer out removes the
  units' value at that day's close. Positive flows are inflows, negative
  ones outflows (``returns`` module doc for how they enter the chain).
* **TWR**: the daily chain (``returns.twr``); ``twr_annualized`` only for
  periods of at least 365 days. **MWR**: XIRR of the starting value (paid
  in), the daily net flows and the ending value (received), as an annual
  rate (``mwr_annualized``, periods of a year or more) and over the period
  (``mwr``). With no flows inside the period both equal ``end/start - 1``.
* **Gains**: realised from the sales inside the period (proceeds share
  minus cost of each consumed lot portion, converted on the sale date, short
  or long term by holding period); unrealised at the end from the open lots.
* **Income** is dividends and interest; **fees** are fee transactions plus
  the ``fees`` of other transactions.

Missing prices value a position at nothing and missing exchange rates leave
the amount out; both are listed (``unpriced_security_ids``,
``missing_rates``). All rates are rounded once, half-even, to 10 decimals;
money to 6 decimals in the base currency.
"""

from __future__ import annotations

import bisect
import calendar
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.principal import Principal
from app.core.money import Money, calc_context, to_storage
from app.domain.accounts.models import Account
from app.domain.accounts.visibility import get_visible_account
from app.domain.fx.service import FxConverter, base_currency
from app.domain.investments import lots as engine
from app.domain.investments import returns, service
from app.domain.investments.holdings import gains_out
from app.domain.investments.models import InvestmentTxn, InvestmentTxnType, Lot, Security
from app.domain.investments.schemas import PerformanceOut, Range, SeriesPoint
from app.domain.investments.valuation import Converter, PriceBook

T = InvestmentTxnType
#: Most points in ``series`` (evenly sampled, the last day always included).
MAX_SERIES_POINTS: Final = 400
#: Longest period valued day by day (the reports' range cap, SECURITY.md F8).
MAX_PERFORMANCE_DAYS: Final = 3660
_MONTHS: Final[dict[str, int]] = {"1m": 1, "3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60}


def months_before(day: date, months: int) -> date:
    """The same day ``months`` earlier (clamped to the month's last day)."""
    total = day.year * 12 + day.month - 1 - months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def range_start(range_: Range, end: date) -> date | None:
    """The start day of ``range_`` ending on ``end``; ``None`` for ``max``."""
    if range_ == "max":
        return None
    if range_ == "ytd":
        return date(end.year - 1, 12, 31)
    return months_before(end, _MONTHS[range_])


@dataclass
class _Position:
    account: Account
    security: Security
    dates: list[date]
    quantities: list[Decimal]

    def at(self, day: date) -> Decimal:
        index = bisect.bisect_right(self.dates, day)
        return self.quantities[index - 1] if index else Decimal(0)


def _money(value: Decimal, currency: str) -> Money:
    return Money.of(to_storage(value), currency)


def performance(
    db: Session,
    principal: Principal,
    *,
    range_: Range = "1y",
    account_id: uuid.UUID | None = None,
) -> PerformanceOut:
    household_id = service.household_of(principal)
    end = service.today()
    stmt = service.visible_investment_txns(principal).where(InvestmentTxn.trade_date <= end)
    if account_id is not None:
        get_visible_account(db, principal, account_id)
        stmt = stmt.where(InvestmentTxn.account_id == account_id)
    txns = list(db.execute(stmt.order_by(InvestmentTxn.trade_date, InvestmentTxn.id)).scalars())
    # Lots without an opening transaction (statement seeds) enter like transfers in.
    seed_stmt = service.visible_lots(principal).where(
        Lot.opening_txn_id.is_(None), Lot.open_date <= end
    )
    if account_id is not None:
        seed_stmt = seed_stmt.where(Lot.account_id == account_id)
    seed_lots = list(db.execute(seed_stmt).scalars())
    base = base_currency(db, household_id)
    first = min([t.trade_date for t in txns[:1]] + [s.open_date for s in seed_lots], default=end)
    start = range_start(range_, end)
    floor = first - timedelta(days=1)
    if start is None or start < floor:
        start = floor
    start = min(max(start, end - timedelta(days=MAX_PERFORMANCE_DAYS)), end)

    account_ids = sorted(
        {t.account_id for t in txns} | {s.account_id for s in seed_lots}, key=lambda u: u.bytes
    )
    security_ids = sorted(
        {t.security_id for t in txns if t.security_id} | {s.security_id for s in seed_lots},
        key=lambda u: u.bytes,
    )
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
    methods = service.cost_methods(db, household_id, account_ids)
    lot_ids = service.stored_lot_ids(db, household_id, account_ids)
    book = PriceBook(db, household_id, security_ids, until=end, trades=txns)
    to_base = Converter(db, household_id, base, until=end)
    unpriced: set[uuid.UUID] = set()

    # Replay every (account, security) pair in memory.
    by_pair: dict[tuple[uuid.UUID, uuid.UUID], list[InvestmentTxn]] = defaultdict(list)
    for txn in txns:
        if txn.security_id is not None:
            by_pair[(txn.account_id, txn.security_id)].append(txn)
    seeds_by_pair: dict[tuple[uuid.UUID, uuid.UUID], list[Lot]] = defaultdict(list)
    for seed_lot in seed_lots:
        seeds_by_pair[(seed_lot.account_id, seed_lot.security_id)].append(seed_lot)
        by_pair.setdefault((seed_lot.account_id, seed_lot.security_id), [])
    seeded: list[tuple[Security, engine.EngineTxn]] = []
    positions: list[_Position] = []
    disposals: list[tuple[Account, engine.Disposal]] = []
    open_lots: list[tuple[Account, Security, engine.EngineLot]] = []
    account_fx: dict[str, FxConverter] = {}
    for (acct_id, sec_id), rows in sorted(
        by_pair.items(), key=lambda kv: (kv[0][0].bytes, kv[0][1].bytes)
    ):
        account, security = accounts[acct_id], securities[sec_id]
        fx = account_fx.setdefault(
            account.currency, FxConverter(db, household_id, account.currency)
        )
        seeds = service.seed_txns(seeds_by_pair.get((acct_id, sec_id), []), rows, strict=False)
        seeded.extend((security, seed) for seed in seeds or [])
        try:
            result = engine.replay(
                [
                    service.engine_txn(r, service.open_cost(r, security.currency, fx.convert))
                    for r in rows
                ]
                + (seeds or []),
                method=methods[acct_id],
                lot_ids=lot_ids,
            )
        except engine.LotError as exc:  # pragma: no cover - writes are replayed first
            raise service.lot_problem(exc) from exc
        positions.append(
            _Position(
                account,
                security,
                [d for d, _ in result.positions],
                [q for _, q in result.positions],
            )
        )
        disposals.extend((account, d) for d in result.disposals)
        open_lots.extend((account, security, lot) for lot in result.lots if lot.remaining > 0)

    def value_on(day: date) -> Decimal:
        total = Decimal(0)
        for position in positions:
            quantity = position.at(day)
            if quantity == 0:
                continue
            close = book.close(position.security.id, day)
            if close is None:
                unpriced.add(position.security.id)
                continue
            converted = to_base(quantity * close.close, position.security.currency, day)
            if converted is not None:
                total += converted
        return total

    def market(security: Security, quantity: Decimal | None, day: date) -> Decimal:
        close = book.close(security.id, day)
        if close is None or quantity is None:
            unpriced.add(security.id)
            return Decimal(0)
        return to_base(quantity * close.close, security.currency, day) or Decimal(0)

    inflows: dict[date, Decimal] = defaultdict(Decimal)
    outflows: dict[date, Decimal] = defaultdict(Decimal)
    income = fees = Decimal(0)
    with localcontext(calc_context()):
        for txn in txns:
            if txn.trade_date <= start:
                continue
            day = txn.trade_date
            account = accounts[txn.account_id]
            flow = to_base(-txn.amount, account.currency, day) or Decimal(0)
            if txn.type is T.TRANSFER_IN and txn.security_id is not None:
                flow += market(securities[txn.security_id], txn.quantity, day)
            elif txn.type is T.TRANSFER_OUT and txn.security_id is not None:
                flow -= market(securities[txn.security_id], txn.quantity, day)
            if flow > 0:
                inflows[day] += flow
            elif flow < 0:
                outflows[day] -= flow
            if txn.type in (T.DIVIDEND, T.INTEREST):
                income += to_base(txn.amount, account.currency, day) or Decimal(0)
            if txn.type is T.FEE:
                fees += to_base(-txn.amount, account.currency, day) or Decimal(0)
            elif txn.fees:
                fees += to_base(txn.fees, account.currency, day) or Decimal(0)

        for security, seed in seeded:
            if start < seed.trade_date <= end:
                inflows[seed.trade_date] += market(security, seed.quantity, seed.trade_date)

        start_value = value_on(start)
        days: list[returns.Day] = []
        dates: list[date] = []
        day = start + timedelta(days=1)
        while day <= end:
            days.append(
                returns.Day(
                    value_on(day), inflows.get(day, Decimal(0)), outflows.get(day, Decimal(0))
                )
            )
            dates.append(day)
            day += timedelta(days=1)
        end_value = days[-1].value if days else start_value
        daily = returns.daily_returns(start_value, days)
        growth = Decimal(1)
        cumulative: list[Decimal] = []
        for r in daily:
            growth *= 1 + r
            cumulative.append(growth - 1)
        twr = cumulative[-1] if cumulative else Decimal(0)
        period = (end - start).days

        flows: list[tuple[int, Decimal]] = [(0, -start_value)]
        for offset, item in enumerate(days, start=1):
            net = item.outflow - item.inflow
            if offset == len(days):
                net += item.value
            flows.append((offset, net))
        if not days:
            flows.append((0, start_value))
        log_growth = returns.xirr_growth(flows)

        realised = {"short": Decimal(0), "long": Decimal(0)}
        for account, disposal in disposals:
            gain = disposal.gain
            if gain is None or not (start < disposal.close_date <= end):
                continue
            converted = to_base(gain, account.currency, disposal.close_date)
            if converted is not None:
                realised[disposal.term] += converted
        unrealised = {"short": Decimal(0), "long": Decimal(0)}
        for account, security, lot in open_lots:
            close = book.close(security.id, end)
            if close is None:
                unpriced.add(security.id)
                continue
            value = to_base(lot.remaining * close.close, security.currency, end)
            cost = to_base(lot.remaining_cost, account.currency, end)
            if value is not None and cost is not None:
                unrealised[engine.holding_term(lot.open_date, end)] += value - cost

    step = max(1, -(-len(days) // MAX_SERIES_POINTS))
    picked = [i for i in range(len(days)) if i % step == step - 1 or i == len(days) - 1]
    series = [
        SeriesPoint(
            date=dates[i],
            value=_money(days[i].value, base),
            twr=f"{returns.round_rate(cumulative[i]):f}",
        )
        for i in picked
    ]

    def rate(value: Decimal | None) -> str | None:
        return f"{returns.round_rate(value):f}" if value is not None else None

    return PerformanceOut(
        range=range_,
        start=start,
        end=end,
        days=period,
        base_currency=base,
        account_id=account_id,
        start_value=_money(start_value, base),
        end_value=_money(end_value, base),
        inflows=_money(sum(inflows.values(), Decimal(0)), base),
        outflows=_money(sum(outflows.values(), Decimal(0)), base),
        income=_money(income, base),
        fees=_money(fees, base),
        twr=f"{returns.round_rate(twr):f}",
        twr_annualized=rate(returns.annualize(twr, period)),
        mwr=rate(returns.period_return(log_growth, period) if log_growth is not None else None),
        mwr_annualized=rate(
            returns.period_return(log_growth, 365)
            if log_growth is not None and period >= 365
            else None
        ),
        realized=gains_out(realised["short"], realised["long"], base),
        unrealized=gains_out(unrealised["short"], unrealised["long"], base),
        series=series,
        unpriced_security_ids=sorted(unpriced, key=lambda u: u.bytes),
        missing_rates=to_base.missing(),
    )

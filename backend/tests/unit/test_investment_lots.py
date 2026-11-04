# SPDX-License-Identifier: AGPL-3.0-or-later
"""The lot engine (``app.domain.investments.lots``): FIFO, specific lots, average cost,
splits, holding periods, and the conservation and split-invariance properties."""

from __future__ import annotations

import itertools
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.domain.investments.lots import (
    EngineTxn,
    LotError,
    Pick,
    ReplayResult,
    holding_term,
    replay,
)
from app.domain.investments.models import CostMethod, InvestmentTxnType

T = InvestmentTxnType
D0 = date(2024, 1, 2)
_counter = itertools.count(1)


def _id() -> uuid.UUID:
    # Increasing ids, so entry order is the tie-break as with UUIDv7.
    return uuid.UUID(int=next(_counter))


def buy(day: date, quantity: str, cost: str) -> EngineTxn:
    return EngineTxn(
        _id(), T.BUY, day, Decimal(quantity), amount=-Decimal(cost), open_cost=Decimal(cost)
    )


def sell(day: date, quantity: str, proceeds: str, picks: tuple[Pick, ...] = ()) -> EngineTxn:
    return EngineTxn(
        _id(),
        T.SELL,
        day,
        Decimal(quantity),
        amount=Decimal(proceeds),
        specified=bool(picks),
        picks=picks,
    )


def split(day: date, new: str, old: str) -> EngineTxn:
    return EngineTxn(_id(), T.SPLIT, day, ratio=(Decimal(new), Decimal(old)))


def test_fifo_takes_the_oldest_lots_and_splits_proceeds() -> None:
    first = buy(D0, "10", "1000")
    second = buy(D0 + timedelta(days=30), "10", "1200")
    sale = sell(date(2025, 3, 1), "15", "2250")
    result = replay([sale, second, first])
    lots = {lot.opening_txn_id: lot for lot in result.lots}
    assert lots[first.id].remaining == 0
    assert lots[first.id].closed_date == date(2025, 3, 1)
    assert lots[second.id].remaining == Decimal(5)
    assert [(d.quantity, d.cost, d.proceeds, d.term) for d in result.disposals] == [
        (Decimal(10), Decimal(1000), Decimal(1500), "long"),
        (Decimal(5), Decimal(600), Decimal(750), "long"),
    ]
    assert result.quantity == 5
    assert result.cost_basis == 600
    assert result.positions == [
        (D0, Decimal(10)),
        (D0 + timedelta(days=30), Decimal(20)),
        (date(2025, 3, 1), Decimal(5)),
    ]


def test_specific_lots_are_honoured_and_checked() -> None:
    first = buy(D0, "10", "1000")
    second = buy(D0 + timedelta(days=1), "10", "2000")
    probe = replay([first, second])
    second_lot = probe.opened[second.id]
    lot_ids = probe.opened
    sale = sell(D0 + timedelta(days=5), "4", "1000", picks=(Pick(second_lot, Decimal(4)),))
    result = replay([first, second, sale], lot_ids=lot_ids)
    assert result.disposals[0].lot_id == second_lot
    assert result.disposals[0].cost == 800
    assert result.disposals[0].term == "short"
    too_many = sell(D0 + timedelta(days=5), "11", "1", picks=(Pick(second_lot, Decimal(11)),))
    with pytest.raises(LotError) as exc:
        replay([first, second, too_many], lot_ids=lot_ids)
    assert exc.value.code == "insufficient-quantity"
    mismatch = sell(D0 + timedelta(days=5), "5", "1", picks=(Pick(second_lot, Decimal(4)),))
    with pytest.raises(LotError) as exc:
        replay([first, second, mismatch], lot_ids=lot_ids)
    assert exc.value.code == "lot-links-mismatch"
    early = sell(D0, "1", "1", picks=(Pick(second_lot, Decimal(1)),))
    with pytest.raises(LotError) as exc:
        replay([first, second, early], lot_ids=lot_ids)
    assert exc.value.code == "lot-not-available"
    with pytest.raises(LotError) as exc:
        replay([first, second, sale], method=CostMethod.AVERAGE, lot_ids=lot_ids)
    assert exc.value.code == "lot-selection-not-allowed"


def test_lot_ids_are_stable_across_replays() -> None:
    first = buy(D0, "1", "10")
    once = replay([first])
    again = replay([first], lot_ids=once.opened)
    assert again.lots[0].id == once.lots[0].id


def test_average_cost_pools_and_carries_the_average() -> None:
    first = buy(D0, "10", "1000")
    second = buy(D0 + timedelta(days=1), "10", "2000")
    sale = sell(D0 + timedelta(days=2), "5", "1000")
    result = replay([first, second, sale], method=CostMethod.AVERAGE)
    assert result.disposals[0].cost == 750  # 5 x 150
    assert {lot.cost_per_unit for lot in result.lots if lot.remaining > 0} == {Decimal(150)}
    assert result.cost_basis == 2250
    fifo = replay([first, second, sale])
    assert fifo.disposals[0].cost == 500


def test_selling_more_than_held_is_refused() -> None:
    with pytest.raises(LotError) as exc:
        replay([buy(D0, "1", "10"), sell(D0 + timedelta(days=1), "2", "30")])
    assert exc.value.code == "insufficient-quantity"


def test_same_day_buy_then_sell_and_split_before_trades() -> None:
    entry_sell = sell(D0, "1", "60")
    entry_buy = buy(D0, "1", "100")
    result = replay([entry_sell, entry_buy])  # entered sell first: openings still go first
    assert result.quantity == 0
    held = buy(D0, "10", "1000")
    on_split_day = buy(D0 + timedelta(days=1), "5", "250")
    result = replay([held, on_split_day, split(D0 + timedelta(days=1), "2", "1")])
    # The split comes first that day, so the new buy is not doubled.
    assert result.quantity == 25
    assert result.cost_basis == 1250


def test_delta_split_and_reverse_split() -> None:
    held = buy(D0, "10", "1000")
    added = EngineTxn(_id(), T.SPLIT, D0 + timedelta(days=3), Decimal(20))
    result = replay([held, added])
    assert result.quantity == 30
    assert result.lots[0].cost_per_unit == Decimal("33.3333333333")
    reverse = replay([held, split(D0 + timedelta(days=3), "1", "10")])
    assert reverse.quantity == 1
    assert reverse.cost_basis == 1000


def test_transfer_out_realises_nothing() -> None:
    held = buy(D0, "10", "1000")
    out = EngineTxn(_id(), T.TRANSFER_OUT, D0 + timedelta(days=1), Decimal(4))
    result = replay([held, out])
    assert result.disposals[0].proceeds is None
    assert result.disposals[0].gain is None
    assert result.disposals[0].cost == 400


def test_seed_lots_keep_their_id() -> None:
    seed_id = _id()
    seed = EngineTxn(
        seed_id, T.TRANSFER_IN, D0, Decimal(5), open_cost=Decimal(50), seed_lot=seed_id
    )
    result = replay([seed, sell(D0 + timedelta(days=1), "2", "30")])
    assert result.lots[0].id == seed_id
    assert result.lots[0].opening_txn_id is None
    assert result.opened == {}
    assert result.quantity == 3


@pytest.mark.parametrize(
    ("opened", "closed", "term"),
    [
        (date(2024, 1, 2), date(2025, 1, 2), "short"),
        (date(2024, 1, 2), date(2025, 1, 3), "long"),
        (date(2024, 2, 29), date(2025, 2, 28), "short"),
        (date(2024, 2, 29), date(2025, 3, 1), "long"),
    ],
)
def test_holding_term(opened: date, closed: date, term: str) -> None:
    assert holding_term(opened, closed) == term


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

_units = st.integers(min_value=1, max_value=10_000).map(lambda n: Decimal(n) / 100)
_cost = st.integers(min_value=0, max_value=10_000_000).map(lambda n: Decimal(n) / 100)
_events = st.lists(
    st.tuples(st.sampled_from(["buy", "sell"]), _units, _cost, st.integers(0, 3)),
    min_size=1,
    max_size=25,
)


def _history(events: list[tuple[str, Decimal, Decimal, int]]) -> list[EngineTxn]:
    """Buys and sells; a sell takes at most what is held, so every history is valid."""
    txns: list[EngineTxn] = []
    held = Decimal(0)
    day = D0
    for kind, quantity, money, gap in events:
        day += timedelta(days=gap * 100)
        if kind == "buy":
            txns.append(buy(day, f"{quantity}", f"{money}"))
            held += quantity
        elif held > 0:
            take = min(quantity, held)
            txns.append(sell(day, f"{take}", f"{money}"))
            held -= take
    return txns


@settings(max_examples=150, deadline=None)
@given(_events, st.sampled_from(list(CostMethod)))
def test_property_lots_conserve_units_and_cost(
    events: list[tuple[str, Decimal, Decimal, int]], method: CostMethod
) -> None:
    txns = _history(events)
    result = replay(txns, method=method)
    bought = sum((t.quantity or Decimal(0) for t in txns if t.type is T.BUY), Decimal(0))
    sold = sum((t.quantity or Decimal(0) for t in txns if t.type is T.SELL), Decimal(0))
    disposed = sum((d.quantity for d in result.disposals), Decimal(0))
    # Units: opened = held + disposed, exactly; per lot too.
    assert result.quantity + disposed == bought
    assert disposed == sold
    for lot in result.lots:
        taken = sum((d.quantity for d in result.disposals if d.lot_id == lot.id), Decimal(0))
        assert lot.remaining + taken == lot.quantity
        assert (lot.closed_date is None) == (lot.remaining > 0)
    # Cost: what was paid = cost still held + cost of what left (per-unit
    # costs are kept to 10 decimals, so allow that rounding per lot and sale).
    paid = sum((t.open_cost or Decimal(0) for t in txns), Decimal(0))
    left = sum((d.cost for d in result.disposals), Decimal(0))
    slack = Decimal("1e-9") * (len(txns) + 1) * max(bought, Decimal(1))
    assert abs(result.cost_basis + left - paid) <= slack
    # Proceeds of each sale are shared out exactly.
    for txn in txns:
        if txn.type is T.SELL:
            share = sum(
                (d.proceeds or 0 for d in result.disposals if d.txn_id == txn.id), Decimal(0)
            )
            assert share == txn.amount


@settings(max_examples=100, deadline=None)
@given(_events, st.integers(min_value=2, max_value=10), st.booleans())
def test_property_split_invariance(
    events: list[tuple[str, Decimal, Decimal, int]], factor: int, reverse: bool
) -> None:
    txns = _history(events)
    assume(txns)
    before = replay(txns)
    after_day = max(t.trade_date for t in txns) + timedelta(days=1)
    new, old = (str(factor), "1") if not reverse else ("1", str(factor))
    after = replay([*txns, split(after_day, new, old)])
    ratio = Decimal(new) / Decimal(old)
    # Total cost is unchanged up to the rounding of units and per-unit cost
    # to 10 decimals (half a unit in the last place of each, per open lot).
    bound = sum(
        ((a.cost_per_unit + a.remaining) * Decimal("1e-10") for a in after.lots if a.remaining),
        Decimal("1e-20"),
    )
    assert abs(after.cost_basis - before.cost_basis) <= bound
    for b, a in zip(before.lots, after.lots, strict=True):
        if b.remaining > 0:
            assert abs(a.remaining - b.remaining * ratio) <= Decimal("1e-10")

    # Realised results before the split are untouched.
    def realised(result: ReplayResult) -> list[tuple[object, ...]]:
        return [(d.txn_id, d.quantity, d.cost, d.proceeds, d.term) for d in result.disposals]

    assert realised(before) == realised(after)

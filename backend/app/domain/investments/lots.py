# SPDX-License-Identifier: AGPL-3.0-or-later
"""The lot engine: replays one account's transactions of one security (pure, no I/O).

Input is every :class:`EngineTxn` of the (account, security) pair; output is
the lots, what each sale or transfer out consumed (:class:`Disposal`), the
lot each opening transaction opened and the position after every trade
date. ``app.domain.investments.service.replay_position`` feeds it from the
database and writes the result back (lots, ``lot_links``, the ``holding``
cache row); performance replays it in memory for realised gains.

Order: by trade date; on one day splits and ratio corporate actions first
(trades on the ex-date are in post-split units), then openings
(buy/reinvest/transfer_in), then cash-only rows, then closings
(sell/transfer_out); ties by transaction id (UUIDv7, so entry order).

Rules (DATA_MODEL.md "Investments"):

* **Opening** (buy, reinvest, transfer_in): one lot of ``quantity`` units
  whose total cost is ``open_cost`` (account currency, computed by the
  caller: ``-amount`` for a buy, ``quantity * price + fees`` converted for a
  transfer in). ``cost_per_unit`` is the cost divided by the quantity,
  rounded half-even to 10 decimals (the column's scale). A lot keeps its id
  across replays (``lot_ids``: opening transaction -> existing lot), so
  specific-lot references stay valid.
* **Closing** (sell, transfer_out): ``quantity`` units leave.
  ``fifo`` takes the oldest open lots first unless the transaction names its
  lots (``specified``: each named lot must be open, of this pair, with
  enough units, and the named quantities must add up to the transaction's).
  ``average`` takes units first in first out (that decides the holding
  period) at the average cost of every open unit, and afterwards every
  remaining lot carries that average cost (average cost, single category);
  named lots are refused under ``average``. A sale's net proceeds
  (``amount``) are shared between the consumed portions in proportion to
  their units, each share rounded half-even to 6 decimals (money) and the
  last portion taking the remainder, so the shares add up exactly. A transfer
  out realises nothing (``proceeds`` is ``None``): the cost leaves with the
  units.
* **Split** (and a ``corporate_action`` with a ratio): every open lot's
  quantities are multiplied by ``new/old`` (rounded half-even to 10
  decimals) and its cost per unit divided by it, so the total cost is
  unchanged up to that rounding. A split stored as the units it added
  (``quantity`` without a ratio, as brokerage statements print it) has the
  ratio ``(held + added) / held`` at that point of the replay.
* **Seed lots** (``EngineTxn.seed_lot``): lots without an opening
  transaction (positions a statement reported before any imported activity
  explained them) enter like a transfer in on their open date and keep
  their lot id.
* Holding period: **long term** when the units were sold after the first
  anniversary of the lot's open date (held more than one year), else short
  term.
* Selling more than is held at that point is :class:`LotError`
  ``insufficient-quantity`` (no short positions).

Arithmetic is ``Decimal`` in the 34-digit half-even context
(``app.core.money.calc_context``); quantities never lose digits except in a
split whose ratio does not divide them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Final, Literal

from app.core.money import calc_context, to_storage
from app.domain.investments.models import CostMethod, InvestmentTxnType

T = InvestmentTxnType

#: Units and per-unit costs are stored with 10 decimals (``NUMERIC(28, 10)``).
QUANTITY_PLACES: Final = 10
QUANTITY_QUANTUM: Final = Decimal(1).scaleb(-QUANTITY_PLACES)

OPENING: Final = frozenset({T.BUY, T.REINVEST, T.TRANSFER_IN})
CLOSING: Final = frozenset({T.SELL, T.TRANSFER_OUT})
RATIO_TYPES: Final = frozenset({T.SPLIT, T.CORPORATE_ACTION})
#: Same-day processing order (module doc).
_RANK: Final[Mapping[InvestmentTxnType, int]] = {
    T.SPLIT: 0,
    T.CORPORATE_ACTION: 0,
    T.BUY: 1,
    T.REINVEST: 1,
    T.TRANSFER_IN: 1,
    T.DIVIDEND: 2,
    T.INTEREST: 2,
    T.FEE: 2,
    T.SELL: 3,
    T.TRANSFER_OUT: 3,
}

Term = Literal["short", "long"]


class LotError(ValueError):
    """A transaction the lots cannot satisfy (``code`` is the problem type)."""

    def __init__(self, code: str, detail: str, txn_id: uuid.UUID) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.txn_id = txn_id


def quantize_units(value: Decimal) -> Decimal:
    """``value`` rounded half-even to 10 decimals (quantities and per-unit costs)."""
    result = value.quantize(QUANTITY_QUANTUM, rounding=ROUND_HALF_EVEN, context=calc_context())
    return result + 0 if result.is_zero() else result


def format_units(value: Decimal) -> str:
    """API string for units, unit prices and ratios: plain decimal, no trailing zeros."""
    normalized = value.normalize(context=calc_context())
    return f"{normalized + 0 if normalized.is_zero() else normalized:f}"


def holding_term(open_date: date, close_date: date) -> Term:
    """``long`` when ``close_date`` is after the first anniversary of ``open_date``."""
    try:
        anniversary = open_date.replace(year=open_date.year + 1)
    except ValueError:  # 29 February: the anniversary is 28 February
        anniversary = open_date.replace(year=open_date.year + 1, day=28)
    return "long" if close_date > anniversary else "short"


@dataclass(frozen=True)
class Pick:
    """Units a closing transaction takes from one lot (a named lot when ``specified``)."""

    lot_id: uuid.UUID
    quantity: Decimal


@dataclass(frozen=True)
class EngineTxn:
    """One transaction as the engine needs it.

    ``open_cost``: total cost in the account currency (openings only).
    ``ratio``: ``(new, old)`` units (splits and ratio corporate actions).
    ``picks``: the named lots of a specific-lot closing (``specified``).
    ``quantity`` of a split without ``ratio``: the units it added.
    ``seed_lot``: this "transaction" stands for an existing lot without an
    opening transaction (module doc); ``id`` is then the lot's id.
    """

    id: uuid.UUID
    type: InvestmentTxnType
    trade_date: date
    quantity: Decimal | None = None
    amount: Decimal = Decimal(0)
    open_cost: Decimal | None = None
    ratio: tuple[Decimal, Decimal] | None = None
    specified: bool = False
    picks: tuple[Pick, ...] = ()
    seed_lot: uuid.UUID | None = None


@dataclass
class EngineLot:
    id: uuid.UUID
    opening_txn_id: uuid.UUID | None
    open_date: date
    quantity: Decimal
    remaining: Decimal
    cost_per_unit: Decimal
    closed_date: date | None = None

    @property
    def remaining_cost(self) -> Decimal:
        with localcontext(calc_context()):
            return self.remaining * self.cost_per_unit


@dataclass(frozen=True)
class Disposal:
    """Units of one lot that left with one closing transaction (amounts in the account currency).

    ``proceeds`` is ``None`` for a transfer out (nothing realised).
    """

    txn_id: uuid.UUID
    lot_id: uuid.UUID
    open_date: date
    close_date: date
    quantity: Decimal
    cost: Decimal
    proceeds: Decimal | None
    term: Term

    @property
    def gain(self) -> Decimal | None:
        if self.proceeds is None:
            return None
        with localcontext(calc_context()):
            return self.proceeds - self.cost


@dataclass
class ReplayResult:
    lots: list[EngineLot] = field(default_factory=list)
    disposals: list[Disposal] = field(default_factory=list)
    #: Opening transaction -> the lot it opened.
    opened: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    #: Closing transaction -> the units it took from each lot, in order.
    consumed: dict[uuid.UUID, list[Pick]] = field(default_factory=dict)
    #: Units held at the end of each trade date with a change, in date order.
    positions: list[tuple[date, Decimal]] = field(default_factory=list)

    @property
    def quantity(self) -> Decimal:
        return sum((lot.remaining for lot in self.lots), Decimal(0))

    @property
    def cost_basis(self) -> Decimal:
        with localcontext(calc_context()):
            return sum((lot.remaining_cost for lot in self.lots), Decimal(0))


def ordered(txns: Sequence[EngineTxn]) -> list[EngineTxn]:
    return sorted(txns, key=lambda t: (t.trade_date, _RANK[t.type], t.id.bytes))


def replay(
    txns: Sequence[EngineTxn],
    *,
    method: CostMethod = CostMethod.FIFO,
    lot_ids: Mapping[uuid.UUID, uuid.UUID] | None = None,
    new_id: Callable[[], uuid.UUID] = uuid.uuid4,
) -> ReplayResult:
    """Replay ``txns`` (one account, one security) from nothing (module doc)."""
    result = ReplayResult()
    known = dict(lot_ids or {})
    open_lots: list[EngineLot] = []
    with localcontext(calc_context()):
        for txn in ordered(txns):
            if txn.type in OPENING:
                lot = _open(txn, known, new_id)
                open_lots.append(lot)
                result.lots.append(lot)
                if txn.seed_lot is None:
                    result.opened[txn.id] = lot.id
            elif txn.type in CLOSING:
                _close(txn, open_lots, method, result)
                open_lots = [lot for lot in open_lots if lot.remaining > 0]
            elif txn.type in RATIO_TYPES and txn.ratio is not None:
                _split(txn, open_lots, txn.ratio)
            elif txn.type is T.SPLIT and txn.quantity is not None:
                held = sum((lot.remaining for lot in open_lots), Decimal(0))
                if held <= 0:
                    continue
                if held + txn.quantity <= 0:
                    raise LotError(
                        "invalid-investment-transaction",
                        "A split cannot remove every unit held.",
                        txn.id,
                    )
                _split(txn, open_lots, (held + txn.quantity, held))
            else:
                continue
            held = sum((lot.remaining for lot in open_lots), Decimal(0))
            if result.positions and result.positions[-1][0] == txn.trade_date:
                result.positions[-1] = (txn.trade_date, held)
            else:
                result.positions.append((txn.trade_date, held))
    return result


def _open(
    txn: EngineTxn, known: Mapping[uuid.UUID, uuid.UUID], new_id: Callable[[], uuid.UUID]
) -> EngineLot:
    quantity = txn.quantity
    if quantity is None or quantity <= 0:
        raise LotError("invalid-investment-transaction", "A quantity above zero is needed.", txn.id)
    cost = txn.open_cost if txn.open_cost is not None else Decimal(0)
    if cost < 0:
        raise LotError("invalid-investment-transaction", "A lot cannot cost less than 0.", txn.id)
    if txn.seed_lot is not None:
        lot_id, opening = txn.seed_lot, None
    else:
        lot_id, opening = known.get(txn.id) or new_id(), txn.id
    return EngineLot(
        id=lot_id,
        opening_txn_id=opening,
        open_date=txn.trade_date,
        quantity=quantity,
        remaining=quantity,
        cost_per_unit=quantize_units(cost / quantity),
    )


def _average(open_lots: Sequence[EngineLot]) -> Decimal:
    units = sum((lot.remaining for lot in open_lots), Decimal(0))
    cost = sum((lot.remaining_cost for lot in open_lots), Decimal(0))
    return quantize_units(cost / units) if units > 0 else Decimal(0)


def _picks(txn: EngineTxn, open_lots: Sequence[EngineLot], method: CostMethod) -> list[Pick]:
    quantity = txn.quantity
    if quantity is None:  # checked by the caller
        raise LotError("invalid-investment-transaction", "A quantity is needed.", txn.id)
    if txn.specified:
        if method is CostMethod.AVERAGE:
            raise LotError(
                "lot-selection-not-allowed",
                "Specific lots cannot be chosen in an average-cost account.",
                txn.id,
            )
        by_id = {lot.id: lot for lot in open_lots}
        total = Decimal(0)
        for pick in txn.picks:
            lot = by_id.get(pick.lot_id)
            if lot is None:
                raise LotError(
                    "lot-not-available",
                    "A named lot is not open in this account and security on the trade date.",
                    txn.id,
                )
            if pick.quantity > lot.remaining:
                raise LotError(
                    "insufficient-quantity", "A named lot holds fewer units than taken.", txn.id
                )
            total += pick.quantity
        if total != quantity:
            raise LotError(
                "lot-links-mismatch",
                "The units taken from the named lots must add up to the quantity.",
                txn.id,
            )
        return list(txn.picks)
    left = quantity
    picks: list[Pick] = []
    for lot in sorted(open_lots, key=lambda lot: lot.open_date):
        if left <= 0:
            break
        take = min(left, lot.remaining)
        if take > 0:
            picks.append(Pick(lot.id, take))
            left -= take
    if left > 0:
        raise LotError(
            "insufficient-quantity",
            "More units leave than are held on the trade date (short positions are not supported).",
            txn.id,
        )
    return picks


def _close(
    txn: EngineTxn, open_lots: list[EngineLot], method: CostMethod, result: ReplayResult
) -> None:
    quantity = txn.quantity
    if quantity is None or quantity <= 0:
        raise LotError("invalid-investment-transaction", "A quantity above zero is needed.", txn.id)
    average = _average(open_lots) if method is CostMethod.AVERAGE else None
    picks = _picks(txn, open_lots, method)
    by_id = {lot.id: lot for lot in open_lots}
    realised = txn.type is T.SELL
    proceeds_left = txn.amount
    for index, pick in enumerate(picks):
        lot = by_id[pick.lot_id]
        unit_cost = average if average is not None else lot.cost_per_unit
        share: Decimal | None = None
        if realised:
            share = (
                proceeds_left
                if index == len(picks) - 1
                else to_storage(txn.amount * pick.quantity / quantity)
            )
            proceeds_left -= share
        result.disposals.append(
            Disposal(
                txn_id=txn.id,
                lot_id=lot.id,
                open_date=lot.open_date,
                close_date=txn.trade_date,
                quantity=pick.quantity,
                cost=pick.quantity * unit_cost,
                proceeds=share,
                term=holding_term(lot.open_date, txn.trade_date),
            )
        )
        lot.remaining -= pick.quantity
        if lot.remaining == 0:
            lot.closed_date = txn.trade_date
    if average is not None:
        for lot in open_lots:
            if lot.remaining > 0:
                lot.cost_per_unit = average
    result.consumed[txn.id] = picks


def _split(txn: EngineTxn, open_lots: Sequence[EngineLot], ratio: tuple[Decimal, Decimal]) -> None:
    new, old = ratio
    if new <= 0 or old <= 0:
        raise LotError("invalid-investment-transaction", "A split ratio must be positive.", txn.id)
    for lot in open_lots:
        lot.quantity = quantize_units(lot.quantity * new / old)
        lot.remaining = quantize_units(lot.remaining * new / old)
        lot.cost_per_unit = quantize_units(lot.cost_per_unit * old / new)
        if lot.remaining <= 0:
            raise LotError(
                "invalid-investment-transaction",
                "The split ratio leaves a lot with no units at 10 decimals.",
                txn.id,
            )
        if lot.remaining > lot.quantity:  # rounding of a partly sold lot
            lot.remaining = lot.quantity

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Investments building blocks shared by the API, brokerage import and net worth.

Skeleton for the Phase 5 tracks to extend:

* :func:`visible_holdings` / :func:`visible_lots` / :func:`visible_investment_txns`:
  queries already restricted to the principal's visible accounts
  (``app.domain.accounts.visibility``); every read path starts from them.
* :func:`latest_price`: the latest ``price`` on or before a day.
* :func:`parse_lot_links`: the validated form of ``investment_txn.lot_links``.
* :func:`rebuild_holdings`: refresh the ``holding`` cache of an account from
  its open lots (the cache is never written any other way).
* :func:`visible_security_filter` / :func:`get_visible_security`: which
  catalogue entries a principal may see (below).
* :func:`replay_position`: the only writer of ``lot``, ``lot_links`` and the
  ``holding`` row of one (account, security) pair: it replays the pair's
  transactions through the lot engine (``app.domain.investments.lots``) and
  writes the result. Every change of an investment transaction (API, imports)
  calls it for the pairs it touched, so the cache is rebuilt incrementally,
  one pair at a time; :func:`replay_account` replays every pair of an account
  (cost method change).
* :func:`cost_method` / :func:`open_cost` / :func:`engine_txn`: the inputs of
  the engine.

**Security visibility.** Owners and admins see the whole catalogue. A member
or viewer sees a security that has activity (a transaction or a lot) on an
account they can see, or no activity at all; a security used only on
accounts hidden from them is ``404 security-not-found``, exactly like one of
another household, so a hidden account's positions are not revealed through
the catalogue. Prices follow their security.

Quantities and costs are ``Decimal`` (34 significant digits); amounts are
rounded once, half-even, to the column's 6 decimals (``app.core.money``).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import date
from decimal import Decimal, localcontext
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import ColumnElement, Select, delete, exists, or_, select, true
from sqlalchemy.orm import Session

from app.auth.principal import Principal
from app.core import clock
from app.core.errors import ProblemError
from app.core.ids import uuid7
from app.core.money import calc_context, to_storage
from app.domain.accounts.models import Account, AccountType
from app.domain.accounts.visibility import sees_all_accounts, visible_account_filter
from app.domain.fx.service import FxConverter
from app.domain.investments import lots as engine
from app.domain.investments.models import (
    CostMethod,
    Holding,
    InvestmentAccount,
    InvestmentTxn,
    InvestmentTxnType,
    Lot,
    Price,
    Security,
)

#: Account types that hold securities (investment transactions are refused elsewhere).
INVESTMENT_ACCOUNT_TYPES: Final = frozenset({AccountType.BROKERAGE, AccountType.RETIREMENT})

#: Most lots one investment transaction may name.
LOT_LINKS_MAX = 1000


class LotLink(BaseModel):
    """One element of ``investment_txn.lot_links``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lot_id: uuid.UUID
    #: Units taken from (or put into) the lot, a positive decimal string.
    quantity: Decimal = Field(gt=0, max_digits=28, decimal_places=10, allow_inf_nan=False)


_LOT_LINKS = TypeAdapter(list[LotLink])


def parse_lot_links(value: object) -> list[LotLink]:
    """Validate a ``lot_links`` document; ``ValueError`` when it does not fit."""
    links = _LOT_LINKS.validate_python(value)
    if len(links) > LOT_LINKS_MAX:
        raise ValueError(f"at most {LOT_LINKS_MAX} lot links")
    if len({link.lot_id for link in links}) != len(links):
        raise ValueError("a lot is linked twice")
    return links


def dump_lot_links(links: list[LotLink]) -> list[dict[str, str]]:
    """The JSON stored in ``investment_txn.lot_links`` (quantities as plain decimal strings)."""
    return [{"lot_id": str(link.lot_id), "quantity": f"{link.quantity:f}"} for link in links]


def _scoped(principal: Principal) -> uuid.UUID:
    if principal.household_id is None:
        raise ValueError("principal has no active household")
    return principal.household_id


def household_of(principal: Principal) -> uuid.UUID:
    """The principal's active household (``ValueError`` without one; routers ensure it)."""
    return _scoped(principal)


def visible_holdings(principal: Principal) -> Select[Holding]:
    household_id = _scoped(principal)
    return select(Holding).where(
        Holding.household_id == household_id,
        visible_account_filter(principal, Holding.account_id),
    )


def visible_lots(principal: Principal) -> Select[Lot]:
    household_id = _scoped(principal)
    return select(Lot).where(
        Lot.household_id == household_id, visible_account_filter(principal, Lot.account_id)
    )


def visible_investment_txns(principal: Principal) -> Select[InvestmentTxn]:
    household_id = _scoped(principal)
    return select(InvestmentTxn).where(
        InvestmentTxn.household_id == household_id,
        visible_account_filter(principal, InvestmentTxn.account_id),
    )


def latest_price(
    db: Session, household_id: uuid.UUID, security_id: uuid.UUID, on: date
) -> Price | None:
    """The latest closing price of the security on or before ``on`` (no look-ahead)."""
    return db.execute(
        select(Price)
        .where(
            Price.household_id == household_id,
            Price.security_id == security_id,
            Price.price_date <= on,
        )
        .order_by(Price.price_date.desc())
        .limit(1)
    ).scalar_one_or_none()


def rebuild_holdings(db: Session, account: Account, as_of: date) -> list[Holding]:
    """Replace the account's ``holding`` rows with the sums of its open lots.

    ``quantity`` is the sum of ``remaining_quantity``; ``cost_basis`` the sum
    of ``remaining_quantity * cost_per_unit``, computed exactly and rounded
    once to 6 decimals. Securities without open lots lose their row.
    """
    lots = db.execute(
        select(Lot).where(
            Lot.household_id == account.household_id,
            Lot.account_id == account.id,
            Lot.remaining_quantity > 0,
        )
    ).scalars()
    quantity: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    cost: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    with localcontext(calc_context()):
        for lot in lots:
            quantity[lot.security_id] += lot.remaining_quantity
            cost[lot.security_id] += lot.remaining_quantity * lot.cost_per_unit
    db.execute(
        delete(Holding).where(
            Holding.household_id == account.household_id, Holding.account_id == account.id
        )
    )
    rows = [
        Holding(
            household_id=account.household_id,
            account_id=account.id,
            security_id=security_id,
            quantity=quantity[security_id],
            cost_basis=to_storage(cost[security_id]),
            currency=account.currency,
            as_of=as_of,
        )
        for security_id in sorted(quantity)
    ]
    db.add_all(rows)
    db.flush()
    return rows


# ---------------------------------------------------------------------------
# Securities: visibility
# ---------------------------------------------------------------------------


def today() -> date:
    return clock.utcnow().date()


def security_not_found() -> ProblemError:
    return ProblemError(404, "security-not-found", "Security not found")


def visible_security_filter(principal: Principal) -> ColumnElement[bool]:
    """SQL predicate on ``security``: the principal may see it (module doc)."""
    if sees_all_accounts(principal):
        return true()
    used_txn = select(InvestmentTxn.id).where(
        InvestmentTxn.household_id == Security.household_id,
        InvestmentTxn.security_id == Security.id,
    )
    used_lot = select(Lot.id).where(
        Lot.household_id == Security.household_id, Lot.security_id == Security.id
    )
    seen_txn = used_txn.where(visible_account_filter(principal, InvestmentTxn.account_id))
    seen_lot = used_lot.where(visible_account_filter(principal, Lot.account_id))
    return or_(
        exists(seen_txn),
        exists(seen_lot),
        ~exists(used_txn) & ~exists(used_lot),
    )


def get_visible_security(
    db: Session, principal: Principal, security_id: uuid.UUID, *, lock: bool = False
) -> Security:
    """The security, or ``404 security-not-found`` (hidden, foreign or missing alike)."""
    household_id = _scoped(principal)
    stmt = select(Security).where(
        Security.household_id == household_id,
        Security.id == security_id,
        visible_security_filter(principal),
    )
    if lock:
        stmt = stmt.with_for_update()
    found = db.execute(stmt).scalar_one_or_none()
    if found is None:
        raise security_not_found()
    return found


# ---------------------------------------------------------------------------
# The lot engine's inputs and the writer of lots, lot_links and holdings
# ---------------------------------------------------------------------------


def cost_method(db: Session, household_id: uuid.UUID, account_id: uuid.UUID) -> CostMethod:
    """The account's cost method (``fifo`` without an ``investment_account`` row)."""
    found = db.execute(
        select(InvestmentAccount.cost_method).where(
            InvestmentAccount.household_id == household_id,
            InvestmentAccount.account_id == account_id,
        )
    ).scalar_one_or_none()
    return found or CostMethod.FIFO


def cost_methods(
    db: Session, household_id: uuid.UUID, account_ids: list[uuid.UUID]
) -> dict[uuid.UUID, CostMethod]:
    rows = (
        db.execute(
            select(InvestmentAccount.account_id, InvestmentAccount.cost_method).where(
                InvestmentAccount.household_id == household_id,
                InvestmentAccount.account_id.in_(account_ids),
            )
        ).all()
        if account_ids
        else []
    )
    found: dict[uuid.UUID, CostMethod] = {a: m for a, m in rows}  # noqa: C416 - Row tuples
    return {a: found.get(a, CostMethod.FIFO) for a in account_ids}


#: ``convert(amount, from_currency, on)`` into the account's currency.
Convert = Callable[[Decimal, str, date], Decimal]


def open_cost(
    row: InvestmentTxn, security_currency: str | None, convert: Convert
) -> Decimal | None:
    """Total cost (account currency) of the lot an opening transaction opens.

    buy: ``-amount``; reinvest: ``-amount`` when cash left the account,
    else ``quantity * price`` (converted on the trade date) plus fees;
    transfer_in: ``quantity * price`` converted plus fees. ``None`` for
    other types.
    """
    kind = row.type
    if kind is InvestmentTxnType.BUY or (kind is InvestmentTxnType.REINVEST and row.amount < 0):
        return -row.amount
    if kind in (InvestmentTxnType.REINVEST, InvestmentTxnType.TRANSFER_IN):
        if row.quantity is None or row.price is None or security_currency is None:
            return None
        with localcontext(calc_context()):
            gross = row.quantity * row.price
        return convert(gross, security_currency, row.trade_date) + row.fees
    return None


def ratio_of(row: InvestmentTxn) -> tuple[Decimal, Decimal] | None:
    """``(new, old)`` of a split or ratio corporate action (stored as ``quantity``/``price``)."""
    if row.type not in engine.RATIO_TYPES or row.quantity is None or row.price is None:
        return None
    return row.quantity, row.price


def engine_txn(row: InvestmentTxn, cost: Decimal | None) -> engine.EngineTxn:
    picks: tuple[engine.Pick, ...] = ()
    if row.lots_specified:
        picks = tuple(
            engine.Pick(link.lot_id, link.quantity) for link in parse_lot_links(row.lot_links)
        )
    ratio = ratio_of(row)
    return engine.EngineTxn(
        id=row.id,
        type=row.type,
        trade_date=row.trade_date,
        quantity=row.quantity if ratio is None else None,
        amount=row.amount,
        open_cost=cost,
        ratio=ratio,
        specified=row.lots_specified,
        picks=picks,
    )


def seed_txns(
    seeds: list[Lot], rows: list[InvestmentTxn], *, strict: bool = True
) -> list[engine.EngineTxn] | None:
    """Engine inputs for lots without an opening transaction (``lots`` module doc).

    A seed's stored quantity and cost already include the splits after its
    open date; they are taken back out (``quantity / ratio``, ``cost *
    ratio``) so the replay can apply them again. A split stored as units
    added after a seed has no fixed ratio: ``None`` (``strict``), or the
    seed is taken as stored (``strict=False``, read-only estimates).
    """
    out: list[engine.EngineTxn] = []
    with localcontext(calc_context()):
        for lot in seeds:
            factor = Decimal(1)
            for row in rows:
                if row.type not in engine.RATIO_TYPES or row.trade_date <= lot.open_date:
                    continue
                ratio = ratio_of(row)
                if ratio is not None:
                    factor *= ratio[0] / ratio[1]
                elif row.type is InvestmentTxnType.SPLIT and row.quantity is not None and strict:
                    return None
            quantity = engine.quantize_units(lot.quantity / factor)
            unit_cost = engine.quantize_units(lot.cost_per_unit * factor)
            out.append(
                engine.EngineTxn(
                    id=lot.id,
                    type=InvestmentTxnType.TRANSFER_IN,
                    trade_date=lot.open_date,
                    quantity=quantity,
                    open_cost=quantity * unit_cost,
                    seed_lot=lot.id,
                )
            )
    return out


def lot_problem(error: engine.LotError, *, status: int = 422) -> ProblemError:
    titles = {
        "insufficient-quantity": "Not enough units",
        "lot-not-available": "Lot not available",
        "lot-links-mismatch": "Lot quantities do not match",
        "lot-selection-not-allowed": "Lot selection not allowed",
    }
    return ProblemError(
        status,
        error.code,
        titles.get(error.code, "Invalid investment transaction"),
        error.detail,
        extra={"transaction_id": str(error.txn_id)},
    )


def account_converter(db: Session, account: Account) -> Convert:
    converter = FxConverter(db, account.household_id, account.currency)

    def convert(amount: Decimal, currency: str, on: date) -> Decimal:
        return converter.convert(amount, currency, on)

    return convert


def replay_position(
    db: Session, account: Account, security_id: uuid.UUID, *, conflict_status: int = 422
) -> engine.ReplayResult:
    """Replay one (account, security) pair and write lots, ``lot_links`` and its holding row.

    A transaction the lots cannot satisfy is a ``ProblemError`` with the
    engine's code (``conflict_status``, 422 by default); the caller's
    database transaction must then be rolled back (the request fails).
    """
    household_id = account.household_id
    rows = list(
        db.execute(
            select(InvestmentTxn)
            .where(
                InvestmentTxn.household_id == household_id,
                InvestmentTxn.account_id == account.id,
                InvestmentTxn.security_id == security_id,
            )
            .order_by(InvestmentTxn.trade_date, InvestmentTxn.id)
        ).scalars()
    )
    currency = db.execute(
        select(Security.currency).where(
            Security.household_id == household_id, Security.id == security_id
        )
    ).scalar_one_or_none()
    convert = account_converter(db, account)
    existing = list(
        db.execute(
            select(Lot).where(
                Lot.household_id == household_id,
                Lot.account_id == account.id,
                Lot.security_id == security_id,
            )
        ).scalars()
    )
    seeds = seed_txns([lot for lot in existing if lot.opening_txn_id is None], rows)
    if seeds is None:
        raise ProblemError(
            409,
            "lots-not-replayable",
            "Lots cannot be replayed",
            "Units of this security came from a statement and a later split was recorded as "
            "units added; enter that split with its ratio first.",
        )
    try:
        result = engine.replay(
            [engine_txn(r, open_cost(r, currency, convert)) for r in rows] + seeds,
            method=cost_method(db, household_id, account.id),
            lot_ids={lot.opening_txn_id: lot.id for lot in existing if lot.opening_txn_id},
            new_id=uuid7,
        )
    except engine.LotError as exc:
        raise lot_problem(exc, status=conflict_status) from exc
    _write_lots(db, account, security_id, existing, result)
    _write_links(rows, result)
    db.flush()
    _write_holding(db, account, security_id, result)
    return result


def _write_lots(
    db: Session,
    account: Account,
    security_id: uuid.UUID,
    existing: list[Lot],
    result: engine.ReplayResult,
) -> None:
    by_id = {lot.id: lot for lot in existing}
    keep: set[uuid.UUID] = set()
    for computed in result.lots:
        keep.add(computed.id)
        row = by_id.get(computed.id)
        if row is None:
            row = Lot(
                id=computed.id,
                household_id=account.household_id,
                account_id=account.id,
                security_id=security_id,
                currency=account.currency,
            )
            db.add(row)
        row.open_date = computed.open_date
        row.quantity = computed.quantity
        row.remaining_quantity = computed.remaining
        row.cost_per_unit = computed.cost_per_unit
        row.closed_date = computed.closed_date
        row.opening_txn_id = computed.opening_txn_id
    for lot in existing:
        if lot.id not in keep:
            db.delete(lot)


def _write_links(rows: list[InvestmentTxn], result: engine.ReplayResult) -> None:
    for row in rows:
        if row.id in result.opened and row.quantity is not None:
            links = dump_lot_links([LotLink(lot_id=result.opened[row.id], quantity=row.quantity)])
        elif row.id in result.consumed and not row.lots_specified:
            links = dump_lot_links(
                [LotLink(lot_id=p.lot_id, quantity=p.quantity) for p in result.consumed[row.id]]
            )
        elif row.lots_specified:
            continue
        else:
            links = []
        if row.lot_links != links:
            row.lot_links = links


def _write_holding(
    db: Session, account: Account, security_id: uuid.UUID, result: engine.ReplayResult
) -> None:
    row = db.execute(
        select(Holding).where(
            Holding.household_id == account.household_id,
            Holding.account_id == account.id,
            Holding.security_id == security_id,
        )
    ).scalar_one_or_none()
    quantity = result.quantity
    if quantity <= 0:
        if row is not None:
            db.delete(row)
            db.flush()
        return
    if row is None:
        row = Holding(
            household_id=account.household_id,
            account_id=account.id,
            security_id=security_id,
            currency=account.currency,
        )
        db.add(row)
    row.quantity = quantity
    row.cost_basis = to_storage(result.cost_basis)
    row.as_of = today()
    db.flush()


def replay_account(db: Session, account: Account) -> None:
    """Replay every security of the account (after a cost method change)."""
    traded = db.execute(
        select(InvestmentTxn.security_id).where(
            InvestmentTxn.household_id == account.household_id,
            InvestmentTxn.account_id == account.id,
        )
    ).scalars()
    lotted = db.execute(
        select(Lot.security_id).where(
            Lot.household_id == account.household_id, Lot.account_id == account.id
        )
    ).scalars()
    found = {s for s in traded if s is not None} | set(lotted)
    for security_id in sorted(found, key=lambda u: u.bytes):
        replay_position(db, account, security_id)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Account balances from snapshots and transactions (DATA_MODEL.md "Ledger").

Definitions (shared by every report, the dashboard and net worth):

* A ``balance_snapshot`` is the balance at the **end** of its
  ``as_of_date`` (transactions posted that day are included in it).
* The **anchor** for a day ``d`` is the latest snapshot with
  ``as_of_date <= d``; on one date a ``manual`` snapshot beats a
  ``statement`` one, which beats a ``computed`` one (:data:`SOURCE_PRIORITY`).
* The balance at the end of day ``d`` is:

  - for a ``manual_balance`` account (property, vehicle, manual liability):
    the anchor's balance, or ``0`` without one; transactions do not move it;
  - otherwise: the anchor's balance plus every transaction posted after the
    anchor's date and on or before ``d``; without an anchor, the sum of
    every transaction posted on or before ``d`` (opening balance ``0``).

  Every status counts (``pending``, ``cleared``, ``reconciled``); the date is
  ``posted_date``.
* A snapshot inside a range re-anchors the series from that day on, so a
  statement balance corrects earlier drift instead of being added to it.
* Per-transaction running balances (:func:`running_balances`) start from the
  previous day's end balance and add the day's transactions in
  ``(posted_date, id)`` order (ids are UUIDv7, so insertion order).

Balances are in the account's currency (``app.domain.fx`` converts). These
helpers take accounts the caller has already checked with
``app.domain.accounts.visibility``. Arithmetic is exact ``Decimal``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.core.money import sum_amounts
from app.domain.accounts.models import Account, BalanceSnapshot, BalanceSource
from app.domain.transactions.models import Transaction

#: Lower wins when two snapshots share a date.
SOURCE_PRIORITY: Final[dict[BalanceSource, int]] = {
    BalanceSource.MANUAL: 0,
    BalanceSource.STATEMENT: 1,
    BalanceSource.COMPUTED: 2,
}
ZERO: Final = Decimal(0)


@dataclass(frozen=True)
class Anchor:
    as_of_date: date
    balance: Decimal
    source: BalanceSource = BalanceSource.MANUAL


@dataclass(frozen=True)
class Movement:
    posted_date: date
    amount: Decimal


@dataclass(frozen=True)
class DailyBalance:
    on: date
    balance: Decimal


@dataclass(frozen=True)
class RunningBalance:
    transaction_id: uuid.UUID
    posted_date: date
    amount: Decimal
    balance: Decimal


# ---------------------------------------------------------------------------
# Pure computation
# ---------------------------------------------------------------------------


def best_anchors(snapshots: Iterable[Anchor]) -> list[Anchor]:
    """One anchor per date (the highest-priority source), sorted by date."""
    best: dict[date, Anchor] = {}
    for snap in snapshots:
        current = best.get(snap.as_of_date)
        if current is None or SOURCE_PRIORITY[snap.source] < SOURCE_PRIORITY[current.source]:
            best[snap.as_of_date] = snap
    return [best[d] for d in sorted(best)]


def pick_anchor(snapshots: Iterable[Anchor], on: date) -> Anchor | None:
    """The anchor for the end of ``on``: latest date ``<= on``, then source priority."""
    candidates = [a for a in best_anchors(snapshots) if a.as_of_date <= on]
    return candidates[-1] if candidates else None


def balance_on(
    snapshots: Iterable[Anchor], movements: Iterable[Movement], on: date, *, manual: bool = False
) -> Decimal:
    """Balance at the end of ``on`` (module doc)."""
    anchor = pick_anchor(snapshots, on)
    if manual:
        return anchor.balance if anchor is not None else ZERO
    start = anchor.as_of_date if anchor is not None else None
    moved = sum_amounts(
        m.amount
        for m in movements
        if m.posted_date <= on and (start is None or m.posted_date > start)
    )
    return (anchor.balance if anchor is not None else ZERO) + moved


def daily_balances(
    snapshots: Iterable[Anchor],
    movements: Iterable[Movement],
    start: date,
    end: date,
    *,
    manual: bool = False,
) -> list[DailyBalance]:
    """End-of-day balances for every day of ``start..end`` (inclusive), in one sweep."""
    if end < start:
        return []
    anchors = best_anchors(snapshots)
    moves = sorted(movements, key=lambda m: m.posted_date)
    by_day: dict[date, Decimal] = {}
    for m in moves:
        by_day[m.posted_date] = by_day.get(m.posted_date, ZERO) + m.amount
    # State at the end of the day before ``start``.
    before = start - timedelta(days=1)
    running = balance_on(anchors, moves, before, manual=manual)
    anchor_at = {a.as_of_date: a for a in anchors}
    out: list[DailyBalance] = []
    day = start
    while day <= end:
        anchor = anchor_at.get(day)
        if anchor is not None:
            running = anchor.balance
        elif not manual:
            running += by_day.get(day, ZERO)
        out.append(DailyBalance(day, running))
        day += timedelta(days=1)
    return out


def running_balance(opening: Decimal, amounts: Iterable[Decimal]) -> list[Decimal]:
    """``opening`` plus each amount in turn: the balance after every row."""
    out: list[Decimal] = []
    total = opening
    for amount in amounts:
        total += amount
        out.append(total)
    return out


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def _anchors(db: Session, account: Account, until: date) -> list[Anchor]:
    rows = db.execute(
        select(BalanceSnapshot.as_of_date, BalanceSnapshot.balance, BalanceSnapshot.source).where(
            BalanceSnapshot.household_id == account.household_id,
            BalanceSnapshot.account_id == account.id,
            BalanceSnapshot.as_of_date <= until,
        )
    )
    return [Anchor(d, b, s) for d, b, s in rows]


def _movements(db: Session, account: Account, after: date | None, until: date) -> list[Movement]:
    stmt = select(Transaction.posted_date, Transaction.amount).where(
        Transaction.household_id == account.household_id,
        Transaction.account_id == account.id,
        Transaction.posted_date <= until,
    )
    if after is not None:
        stmt = stmt.where(Transaction.posted_date > after)
    return [Movement(d, a) for d, a in db.execute(stmt)]


def balance_as_of(db: Session, account: Account, on: date) -> Decimal:
    """Balance of ``account`` at the end of ``on`` (module doc)."""
    anchor = pick_anchor(_anchors(db, account, on), on)
    if account.manual_balance:
        return anchor.balance if anchor is not None else ZERO
    after = anchor.as_of_date if anchor is not None else None
    moved = sum_amounts(m.amount for m in _movements(db, account, after, on))
    return (anchor.balance if anchor is not None else ZERO) + moved


def balances_as_of(db: Session, accounts: Sequence[Account], on: date) -> dict[uuid.UUID, Decimal]:
    """:func:`balance_as_of` for many accounts of one household in two queries."""
    if not accounts:
        return {}
    household_ids = {a.household_id for a in accounts}
    if len(household_ids) != 1:
        raise ValueError("accounts must belong to one household")
    household_id = household_ids.pop()
    ids = [a.id for a in accounts]
    priority = case(
        dict(SOURCE_PRIORITY),
        value=BalanceSnapshot.source,
    )
    anchor_rows = db.execute(
        select(BalanceSnapshot.account_id, BalanceSnapshot.as_of_date, BalanceSnapshot.balance)
        .where(
            BalanceSnapshot.household_id == household_id,
            BalanceSnapshot.account_id.in_(ids),
            BalanceSnapshot.as_of_date <= on,
        )
        .order_by(BalanceSnapshot.account_id, BalanceSnapshot.as_of_date.desc(), priority)
        .ext(distinct_on(BalanceSnapshot.account_id))
    )
    anchors = {account_id: (d, b) for account_id, d, b in anchor_rows}
    result: dict[uuid.UUID, Decimal] = {}
    per_account = []
    for account in accounts:
        anchor = anchors.get(account.id)
        result[account.id] = anchor[1] if anchor is not None else ZERO
        if account.manual_balance:
            continue
        if anchor is None:
            per_account.append(Transaction.account_id == account.id)
        else:
            per_account.append(
                and_(Transaction.account_id == account.id, Transaction.posted_date > anchor[0])
            )
    if per_account:
        sums = db.execute(
            select(Transaction.account_id, func.sum(Transaction.amount))
            .where(
                Transaction.household_id == household_id,
                Transaction.posted_date <= on,
                or_(*per_account),
            )
            .group_by(Transaction.account_id)
        )
        for account_id, total in sums:
            result[account_id] += total if total is not None else ZERO
    return result


def balance_history(db: Session, account: Account, start: date, end: date) -> list[DailyBalance]:
    """End-of-day balances of ``account`` for every day of ``start..end``."""
    if end < start:
        return []
    anchors = _anchors(db, account, end)
    movements = [] if account.manual_balance else _movements(db, account, None, end)
    return daily_balances(anchors, movements, start, end, manual=account.manual_balance)


def running_balances(db: Session, account: Account, start: date, end: date) -> list[RunningBalance]:
    """Each transaction of ``start..end`` with the balance after it (module doc)."""
    if end < start:
        return []
    history = {
        d.on: d.balance
        for d in balance_history(db, account, start - timedelta(days=1), end - timedelta(days=1))
    }
    rows = db.execute(
        select(Transaction.id, Transaction.posted_date, Transaction.amount)
        .where(
            Transaction.household_id == account.household_id,
            Transaction.account_id == account.id,
            Transaction.posted_date >= start,
            Transaction.posted_date <= end,
        )
        .order_by(Transaction.posted_date, Transaction.id)
    )
    out: list[RunningBalance] = []
    current_day: date | None = None
    total = ZERO
    for transaction_id, posted, amount in rows:
        if posted != current_day:
            current_day = posted
            total = history[posted - timedelta(days=1)]
        if not account.manual_balance:
            total += amount
        out.append(RunningBalance(transaction_id, posted, amount, total))
    return out

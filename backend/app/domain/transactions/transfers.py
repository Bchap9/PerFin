# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transfers: two transactions sharing a ``transfer_group_id`` (DATA_MODEL.md "Ledger").

A transfer moves money between two accounts of the household: one leg
leaves an account (negative amount), the other arrives in another account
(positive amount). Pairing rules:

* **Automatic** (:func:`auto_pair`, the matcher): two unlinked transactions
  pair when they are on **different accounts**, in the **same currency**,
  with amounts **equal and opposite** (exactly, ``-x`` and ``x``, ``x != 0``)
  and posted dates at most ``window_days`` apart (default
  :data:`~app.domain.transactions.schemas.TRANSFER_WINDOW_DEFAULT`). Among
  candidates the closest dates win. The matcher is conservative: when a leg
  has two equally close candidates on *different* accounts it cannot tell
  which one is right, so neither is paired and the case is counted as
  ``ambiguous`` (left for a person to link by hand). Interchangeable
  candidates (same account, same date) are paired in id order.
* **Manual** (:func:`link`): the caller names both legs. Different accounts,
  amounts of opposite sign; in the same currency they must be exactly
  opposite, across currencies (an exchange) any opposite amounts are
  accepted. Neither leg may already belong to another transfer.
* :func:`unlink` dissolves the whole group (both legs).

Visibility (SECURITY.md F3): a user-triggered link or auto-pair only
touches transactions on accounts the caller may **edit** (owner/admin: all;
member: ``edit`` grants). The after-import run (:func:`auto_pair` without a
principal, from the ingest worker) works on the whole household, as the
system. A leg on an account the caller cannot see is shown as a transfer
without its counterpart (``TransferOut.counterpart_id`` is ``null``).

Every link, unlink and auto-pair run is audited and emits a
``transaction.*`` event so the SPA refreshes.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

from sqlalchemy import ColumnElement, and_, func, or_, select, true, update
from sqlalchemy.orm import Session, aliased

from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.ids import uuid7
from app.db.queue import enqueue
from app.domain.accounts.visibility import require_visible_accounts, visible_account_filter
from app.domain.transactions import repo
from app.domain.transactions.models import Transaction
from app.domain.transactions.schemas import (
    AUTO_PAIR_JOB_TYPE,
    TRANSFER_WINDOW_DEFAULT,
    AutoPairJob,
    transaction_not_found,
)

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

#: Most candidate edges one auto-pair run considers (bounds memory and time).
MAX_EDGES: Final = 50_000
#: Most suggestions returned for one transaction.
MAX_CANDIDATES: Final = 20


def _problem(status: int, slug: str, title: str, detail: str) -> ProblemError:
    return ProblemError(status, slug, title, detail)


def transfer_conflict(detail: str) -> ProblemError:
    return _problem(409, "transfer-conflict", "Transfer conflict", detail)


def transfer_invalid(detail: str) -> ProblemError:
    return _problem(422, "transfer-invalid", "Not a valid transfer", detail)


@dataclass(frozen=True)
class Pair:
    group_id: uuid.UUID
    outflow_id: uuid.UUID
    inflow_id: uuid.UUID


@dataclass(frozen=True)
class AutoPairOutcome:
    pairs: list[Pair]
    ambiguous: int


# ---------------------------------------------------------------------------
# Manual link / unlink
# ---------------------------------------------------------------------------


def check_pair(a: Transaction, b: Transaction) -> None:
    """``422 transfer-invalid`` unless ``a`` and ``b`` can be the two legs of a transfer."""
    if a.id == b.id:
        raise transfer_invalid("A transaction cannot be its own counterpart.")
    if a.account_id == b.account_id:
        raise transfer_invalid("The two legs must be on different accounts.")
    if a.amount.is_zero() or b.amount.is_zero():
        raise transfer_invalid("A transfer leg cannot be zero.")
    if (a.amount > 0) == (b.amount > 0):
        raise transfer_invalid("One leg must leave an account and the other arrive in one.")
    if a.currency == b.currency and a.amount != -b.amount:
        raise transfer_invalid("In one currency the two amounts must be equal and opposite.")


def link(
    db: Session,
    principal: Principal,
    transaction: Transaction,
    counterpart_id: uuid.UUID,
    *,
    request: HTTPConnection | None,
) -> uuid.UUID:
    """Pair ``transaction`` (already loaded for edit and locked) with ``counterpart_id``.

    The counterpart must be on an account the caller may edit
    (``404 transaction-not-found`` otherwise). Linking two legs that already
    form this transfer is a no-op.
    """
    household_id = transaction.household_id
    edit = visible_account_filter(principal, Transaction.account_id, need="edit")
    other = repo.get(db, household_id, counterpart_id, edit, lock=True)
    if other is None:
        raise transaction_not_found()
    if (
        transaction.transfer_group_id is not None
        and transaction.transfer_group_id == other.transfer_group_id
    ):
        return transaction.transfer_group_id
    check_pair(transaction, other)
    if transaction.transfer_group_id is not None or other.transfer_group_id is not None:
        raise transfer_conflict("Unlink the existing transfer first.")
    group_id = uuid7()
    now = clock.utcnow()
    for leg in (transaction, other):
        leg.transfer_group_id = group_id
        leg.updated_at = now
    db.flush()
    audit(
        db,
        action="transaction.transfer_link",
        target_type="transaction",
        target_id=transaction.id,
        after={"group_id": group_id, "transaction_ids": [transaction.id, other.id]},
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(
        db,
        household_id,
        "transaction.transfer_linked",
        {"group_id": str(group_id), "ids": [str(transaction.id), str(other.id)]},
    )
    return group_id


def unlink(
    db: Session,
    principal: Principal | None,
    transaction: Transaction,
    *,
    request: HTTPConnection | None,
) -> list[uuid.UUID]:
    """Dissolve the transfer ``transaction`` belongs to; return the ids that were in it.

    The caller has loaded ``transaction`` for edit. Every leg of the group
    loses its ``transfer_group_id`` (a group never keeps a single leg).
    """
    group_id = transaction.transfer_group_id
    if group_id is None:
        return []
    household_id = transaction.household_id
    ids = list(
        db.execute(
            update(Transaction)
            .where(
                Transaction.household_id == household_id,
                Transaction.transfer_group_id == group_id,
            )
            .values(transfer_group_id=None, updated_at=clock.utcnow())
            .returning(Transaction.id)
        ).scalars()
    )
    transaction.transfer_group_id = None
    audit(
        db,
        action="transaction.transfer_unlink",
        target_type="transaction",
        target_id=transaction.id,
        before={"group_id": group_id, "transaction_ids": sorted(ids)},
        household_id=household_id,
        user_id=principal.user_id if principal is not None else None,
        request=request,
    )
    emit(
        db,
        household_id,
        "transaction.transfer_unlinked",
        {"group_id": str(group_id), "ids": [str(i) for i in sorted(ids)]},
    )
    return sorted(ids)


def candidates(
    db: Session,
    principal: Principal,
    transaction: Transaction,
    *,
    window_days: int = TRANSFER_WINDOW_DEFAULT,
) -> list[Transaction]:
    """Unlinked transactions the caller may edit that would pair with ``transaction``.

    Same rules as the matcher (different account, same currency, equal and
    opposite amount, dates within ``window_days``), closest date first.
    """
    if transaction.transfer_group_id is not None or transaction.amount.is_zero():
        return []
    edit = visible_account_filter(principal, Transaction.account_id, need="edit")
    distance = func.abs(Transaction.posted_date - transaction.posted_date)
    stmt = (
        select(Transaction)
        .where(
            Transaction.household_id == transaction.household_id,
            edit,
            Transaction.id != transaction.id,
            Transaction.account_id != transaction.account_id,
            Transaction.currency == transaction.currency,
            Transaction.amount == -transaction.amount,
            Transaction.transfer_group_id.is_(None),
            distance <= window_days,
        )
        .order_by(distance, Transaction.posted_date, Transaction.id)
        .limit(MAX_CANDIDATES)
    )
    return list(db.execute(stmt).scalars())


# ---------------------------------------------------------------------------
# Automatic pairing (the matcher)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Edge:
    distance: int
    out_id: uuid.UUID
    out_account: uuid.UUID
    out_date: date
    in_id: uuid.UUID
    in_account: uuid.UUID


#: Builds a predicate on one alias of ``transaction`` (the two legs are aliases).
LegPredicate = Callable[[type[Transaction]], ColumnElement[bool]]


def _edges(
    db: Session,
    household_id: uuid.UUID,
    scope: LegPredicate,
    seed: LegPredicate,
    window_days: int,
) -> list[_Edge]:
    """Candidate (outflow, inflow) pairs in ``scope`` where at least one leg is a ``seed``."""
    out = aliased(Transaction, name="out_leg")
    inn = aliased(Transaction, name="in_leg")
    distance = func.abs(out.posted_date - inn.posted_date)
    stmt = (
        select(
            distance.label("distance"),
            out.id,
            out.account_id,
            out.posted_date,
            inn.id,
            inn.account_id,
        )
        .select_from(out)
        .join(
            inn,
            and_(
                inn.household_id == out.household_id,
                inn.currency == out.currency,
                inn.amount == -out.amount,
                inn.account_id != out.account_id,
                distance <= window_days,
            ),
        )
        .where(
            out.household_id == household_id,
            out.amount < 0,
            out.transfer_group_id.is_(None),
            inn.transfer_group_id.is_(None),
            scope(out),
            scope(inn),
            or_(seed(out), seed(inn)),
        )
        .order_by(distance, out.posted_date, out.id, inn.id)
        .limit(MAX_EDGES)
    )
    return [_Edge(int(d), o, oa, od, i, ia) for d, o, oa, od, i, ia in db.execute(stmt)]


def _match(edges: list[_Edge]) -> tuple[list[tuple[uuid.UUID, uuid.UUID]], int]:
    """Greedy closest-first matching that refuses ambiguous legs.

    ``edges`` is sorted by distance. For each edge whose legs are both free,
    look at the other free candidates of each leg at the same distance: if
    any of them is on a different account than the chosen counterpart, the
    leg cannot be matched with confidence, so both legs are blocked.
    """
    by_out: dict[uuid.UUID, list[_Edge]] = defaultdict(list)
    by_in: dict[uuid.UUID, list[_Edge]] = defaultdict(list)
    for edge in edges:
        by_out[edge.out_id].append(edge)
        by_in[edge.in_id].append(edge)
    used: set[uuid.UUID] = set()
    blocked: set[uuid.UUID] = set()
    pairs: list[tuple[uuid.UUID, uuid.UUID]] = []
    ambiguous = 0
    for edge in edges:
        if {edge.out_id, edge.in_id} & (used | blocked):
            continue
        rivals_in = {
            e.in_account
            for e in by_out[edge.out_id]
            if e.distance == edge.distance and e.in_id not in used | blocked
        }
        rivals_out = {
            e.out_account
            for e in by_in[edge.in_id]
            if e.distance == edge.distance and e.out_id not in used | blocked
        }
        if len(rivals_in) > 1 or len(rivals_out) > 1:
            blocked.update((edge.out_id, edge.in_id))
            ambiguous += 1
            continue
        used.update((edge.out_id, edge.in_id))
        pairs.append((edge.out_id, edge.in_id))
    return pairs, ambiguous


def auto_pair(
    db: Session,
    household_id: uuid.UUID,
    *,
    principal: Principal | None = None,
    account_ids: Collection[uuid.UUID] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    source_document_id: uuid.UUID | None = None,
    window_days: int = TRANSFER_WINDOW_DEFAULT,
    request: HTTPConnection | None = None,
) -> AutoPairOutcome:
    """Pair unlinked transfers (rules in the module doc); return the new pairs.

    With a ``principal`` only accounts it may edit take part (``account_ids``
    must all be editable: ``404 account-not-found`` otherwise); without one
    (the after-import run) the whole household does. ``account_ids``,
    ``date_from``/``date_to`` and ``source_document_id`` choose the **seed**
    transactions: a pair needs at least one seed leg, while its other leg
    may be on any account in scope and up to ``window_days`` outside the
    date range.
    """
    if principal is not None:
        if account_ids:
            require_visible_accounts(db, principal, account_ids, need="edit")
        caller = principal

        def scope(t: type[Transaction]) -> ColumnElement[bool]:
            return visible_account_filter(caller, t.account_id, need="edit")

    else:

        def scope(t: type[Transaction]) -> ColumnElement[bool]:
            return true()

    accounts = list(account_ids or ())

    def seed(t: type[Transaction]) -> ColumnElement[bool]:
        parts: list[ColumnElement[bool]] = [true()]
        if accounts:
            parts.append(t.account_id.in_(accounts))
        if date_from is not None:
            parts.append(t.posted_date >= date_from)
        if date_to is not None:
            parts.append(t.posted_date <= date_to)
        if source_document_id is not None:
            parts.append(t.source_document_id == source_document_id)
        return and_(*parts)

    edges = _edges(db, household_id, scope, seed, window_days)
    matched, ambiguous = _match(edges)
    pairs: list[Pair] = []
    if matched:
        legs = repo.get_many(
            db, household_id, [i for pair in matched for i in pair], true(), lock=True
        )
        now = clock.utcnow()
        for out_id, in_id in matched:
            out_leg, in_leg = legs.get(out_id), legs.get(in_id)
            # Re-checked under the row lock: a concurrent link may have won.
            if out_leg is None or in_leg is None:
                continue
            if out_leg.transfer_group_id is not None or in_leg.transfer_group_id is not None:
                continue
            group_id = uuid7()
            for leg in (out_leg, in_leg):
                leg.transfer_group_id = group_id
                leg.updated_at = now
            pairs.append(Pair(group_id, out_id, in_id))
        db.flush()
    if pairs or ambiguous:
        audit(
            db,
            action="transaction.transfer_auto_pair",
            target_type="household",
            target_id=household_id,
            after={
                "pairs": [[p.outflow_id, p.inflow_id] for p in pairs[:200]],
                "paired": len(pairs),
                "ambiguous": ambiguous,
                "window_days": window_days,
                "source_document_id": source_document_id,
            },
            household_id=household_id,
            user_id=principal.user_id if principal is not None else None,
            request=request,
        )
    if pairs:
        emit(
            db,
            household_id,
            "transaction.transfers_paired",
            {"paired": len(pairs), "ambiguous": ambiguous},
        )
    return AutoPairOutcome(pairs, ambiguous)


def request_auto_pair(
    db: Session,
    household_id: uuid.UUID,
    *,
    source_document_id: uuid.UUID | None = None,
    account_ids: Collection[uuid.UUID] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> uuid.UUID:
    """Enqueue a household-wide :func:`auto_pair` run (in the caller's transaction)."""
    return enqueue(
        db,
        AUTO_PAIR_JOB_TYPE,
        AutoPairJob(
            source_document_id=source_document_id,
            account_ids=list(account_ids) if account_ids else None,
            date_from=date_from,
            date_to=date_to,
        ),
        household_id=household_id,
    )

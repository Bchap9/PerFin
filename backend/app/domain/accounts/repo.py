# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared account queries. Every query filters ``household_id`` explicitly
(RLS is the backstop, SECURITY.md F3)."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.accounts.models import Account, BalanceSnapshot, BalanceSource


def get_account(
    db: Session, household_id: uuid.UUID, account_id: uuid.UUID, *, lock: bool = False
) -> Account | None:
    stmt = select(Account).where(Account.household_id == household_id, Account.id == account_id)
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def account_ids(db: Session, household_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        db.execute(
            select(Account.id).where(Account.household_id == household_id).order_by(Account.id)
        ).scalars()
    )


def existing_account_ids(
    db: Session, household_id: uuid.UUID, ids: Collection[uuid.UUID]
) -> set[uuid.UUID]:
    """The subset of ``ids`` that are accounts of ``household_id``."""
    if not ids:
        return set()
    return set(
        db.execute(
            select(Account.id).where(Account.household_id == household_id, Account.id.in_(ids))
        ).scalars()
    )


def upsert_snapshot(
    db: Session,
    account: Account,
    as_of_date: date,
    balance: Decimal,
    source: BalanceSource,
    *,
    source_document_id: uuid.UUID | None = None,
) -> BalanceSnapshot:
    """Create or replace the ``source`` snapshot of ``account`` on ``as_of_date``.

    Concurrent writers for the same account should hold the account row lock
    (``get_account(..., lock=True)``); otherwise the loser gets the unique
    violation of ``(account_id, as_of_date, source)``.
    """
    existing = db.execute(
        select(BalanceSnapshot).where(
            BalanceSnapshot.household_id == account.household_id,
            BalanceSnapshot.account_id == account.id,
            BalanceSnapshot.as_of_date == as_of_date,
            BalanceSnapshot.source == source,
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = BalanceSnapshot(
            household_id=account.household_id,
            account_id=account.id,
            as_of_date=as_of_date,
            balance=balance,
            currency=account.currency,
            source=source,
            source_document_id=source_document_id,
        )
        db.add(existing)
    else:
        existing.balance = balance
        existing.source_document_id = source_document_id
    db.flush()
    return existing

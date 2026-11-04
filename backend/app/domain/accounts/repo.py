# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared account queries. Every query filters ``household_id`` explicitly
(RLS is the backstop, SECURITY.md F3)."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import date
from decimal import Decimal

from sqlalchemy import ColumnElement, delete, exists, or_, select
from sqlalchemy.orm import Session

from app.auth.models import AccountGrant
from app.domain.accounts.models import (
    Account,
    AccountType,
    BalanceSnapshot,
    BalanceSource,
    Institution,
)
from app.domain.transactions.models import ScheduledTransaction, Transaction


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


# ---------------------------------------------------------------------------
# Listing and lookups for the accounts API
# ---------------------------------------------------------------------------


def list_accounts(
    db: Session,
    household_id: uuid.UUID,
    *,
    visible: ColumnElement[bool],
    after: uuid.UUID | None,
    limit: int,
    closed: bool | None = None,
    account_type: AccountType | None = None,
) -> list[Account]:
    """Accounts in id (creation) order after ``after``, filtered by ``visible``.

    Returns up to ``limit`` rows; callers ask for one more to know whether a
    next page exists.
    """
    stmt = select(Account).where(Account.household_id == household_id, visible)
    if after is not None:
        stmt = stmt.where(Account.id > after)
    if closed is True:
        stmt = stmt.where(Account.closed_on.is_not(None))
    elif closed is False:
        stmt = stmt.where(Account.closed_on.is_(None))
    if account_type is not None:
        stmt = stmt.where(Account.type == account_type)
    return list(db.execute(stmt.order_by(Account.id).limit(limit)).scalars())


def institution_exists(db: Session, household_id: uuid.UUID, institution_id: uuid.UUID) -> bool:
    found = db.execute(
        select(Institution.id).where(
            Institution.household_id == household_id, Institution.id == institution_id
        )
    ).scalar_one_or_none()
    return found is not None


def has_ledger_rows(db: Session, account: Account) -> bool:
    """Whether transactions, balance snapshots or schedules reference ``account``.

    They carry the account's currency in their foreign key, so the currency
    cannot change while any exists.
    """
    household_id = account.household_id
    query = select(
        or_(
            exists().where(
                Transaction.household_id == household_id, Transaction.account_id == account.id
            ),
            exists().where(
                BalanceSnapshot.household_id == household_id,
                BalanceSnapshot.account_id == account.id,
            ),
            exists().where(
                ScheduledTransaction.household_id == household_id,
                ScheduledTransaction.account_id == account.id,
            ),
        )
    )
    return bool(db.execute(query).scalar_one())


def list_snapshots(db: Session, account: Account, start: date, end: date) -> list[BalanceSnapshot]:
    return list(
        db.execute(
            select(BalanceSnapshot)
            .where(
                BalanceSnapshot.household_id == account.household_id,
                BalanceSnapshot.account_id == account.id,
                BalanceSnapshot.as_of_date >= start,
                BalanceSnapshot.as_of_date <= end,
            )
            .order_by(BalanceSnapshot.as_of_date, BalanceSnapshot.source)
        ).scalars()
    )


def delete_account(db: Session, account: Account) -> None:
    """Delete ``account``; its transactions, snapshots, schedules and grants cascade."""
    db.execute(
        delete(Account).where(
            Account.household_id == account.household_id, Account.id == account.id
        )
    )


def grants_on_account(
    db: Session, household_id: uuid.UUID, account_id: uuid.UUID
) -> list[AccountGrant]:
    return list(
        db.execute(
            select(AccountGrant)
            .where(AccountGrant.household_id == household_id, AccountGrant.account_id == account_id)
            .order_by(AccountGrant.user_id)
        ).scalars()
    )

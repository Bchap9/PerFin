# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic ledger data for tests (accounts, transactions, categories, ...).

Like ``tests/fixtures/factories.py``: plain ``make_*`` functions take a
``Session`` and only ``flush`` (usable inside the rolled-back ``db``
fixture); each runs in the household's RLS context and puts the caller's
context back. :class:`LedgerFactories` (the ``ledger`` fixture in
``tests/conftest.py``) commits each object in its own transaction so a
``TestClient`` app sees it.

Everything is generated: names like ``Account-3f9a1c``, amounts and dates
chosen by the test or derived from a seeded ``random.Random``. No real
institutions, payees or numbers.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.auth.models import GrantLevel
from app.db.rls import get_context, restore_context, set_context
from app.domain.accounts.models import (
    Account,
    AccountType,
    AssetKind,
    BalanceSnapshot,
    BalanceSource,
    Institution,
    default_is_asset,
    default_manual_balance,
)
from app.domain.budgets.models import Budget, BudgetLine, BudgetPeriod
from app.domain.fx.models import FxRate
from app.domain.fx.repo import upsert_rate
from app.domain.goals.models import Goal, GoalAccount, GoalContribution
from app.domain.transactions.categories import seed_default_categories
from app.domain.transactions.import_hash import import_hash, normalize_payee
from app.domain.transactions.models import (
    Cadence,
    Category,
    CategoryKind,
    Payee,
    ReviewState,
    Rule,
    ScheduledTransaction,
    ScheduleOrigin,
    Tag,
    TaxTreatment,
    Transaction,
    TransactionSplit,
    TransactionStatus,
    TransactionTag,
)
from tests.fixtures.factories import grant_account, unique_name

DEFAULT_DATE = date(2026, 1, 15)


def _amount(value: Decimal | str | int) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@contextmanager
def household_context(db: Session, household_id: uuid.UUID) -> Iterator[None]:
    """Run in ``household_id``'s RLS context, then restore the caller's."""
    saved = get_context(db)
    set_context(db, household_id)
    yield
    # Not in ``finally``: after a failed statement the transaction is aborted.
    restore_context(db, saved)


def _add[T](db: Session, household_id: uuid.UUID, row: T) -> T:
    with household_context(db, household_id):
        db.add(row)
        db.flush()
    return row


# ---------------------------------------------------------------------------
# Flush-only builders
# ---------------------------------------------------------------------------


def make_institution(
    db: Session, household_id: uuid.UUID, *, name: str | None = None, **fields: Any
) -> Institution:
    return _add(
        db,
        household_id,
        Institution(household_id=household_id, name=name or unique_name("Institution"), **fields),
    )


def make_account(
    db: Session,
    household_id: uuid.UUID,
    *,
    type: AccountType | str = AccountType.CHECKING,
    name: str | None = None,
    currency: str = "USD",
    asset_kind: AssetKind | str | None = None,
    is_asset: bool | None = None,
    manual_balance: bool | None = None,
    **fields: Any,
) -> Account:
    """An account; ``is_asset``/``manual_balance`` default from the type.

    A ``manual_asset`` gets ``asset_kind="other"`` unless one is given.
    """
    account_type = AccountType(type)
    kind = AssetKind(asset_kind) if asset_kind is not None else None
    if account_type is AccountType.MANUAL_ASSET and kind is None:
        kind = AssetKind.OTHER
    return _add(
        db,
        household_id,
        Account(
            household_id=household_id,
            type=account_type,
            asset_kind=kind,
            name=name or unique_name("Account"),
            currency=currency,
            is_asset=default_is_asset(account_type) if is_asset is None else is_asset,
            manual_balance=(
                default_manual_balance(account_type) if manual_balance is None else manual_balance
            ),
            **fields,
        ),
    )


def make_category(
    db: Session,
    household_id: uuid.UUID,
    *,
    name: str | None = None,
    kind: CategoryKind | str = CategoryKind.EXPENSE,
    parent: Category | None = None,
    tax_treatment: TaxTreatment | None = None,
    system_key: str | None = None,
) -> Category:
    return _add(
        db,
        household_id,
        Category(
            household_id=household_id,
            name=name or unique_name("Category"),
            kind=CategoryKind(kind),
            parent_id=parent.id if parent is not None else None,
            tax_treatment=tax_treatment,
            system_key=system_key,
        ),
    )


def make_payee(
    db: Session,
    household_id: uuid.UUID,
    *,
    name: str | None = None,
    aliases: Iterable[str] = (),
    default_category: Category | None = None,
) -> Payee:
    payee_name = name or unique_name("Payee")
    return _add(
        db,
        household_id,
        Payee(
            household_id=household_id,
            name=payee_name,
            normalized_name=normalize_payee(payee_name),
            aliases=list(aliases),
            default_category_id=default_category.id if default_category else None,
        ),
    )


def make_tag(db: Session, household_id: uuid.UUID, *, name: str | None = None) -> Tag:
    return _add(db, household_id, Tag(household_id=household_id, name=name or unique_name("tag")))


def make_transaction(
    db: Session,
    account: Account,
    *,
    amount: Decimal | str | int = "-12.34",
    posted_date: date = DEFAULT_DATE,
    raw_payee: str | None = None,
    payee: Payee | None = None,
    category: Category | None = None,
    status: TransactionStatus = TransactionStatus.CLEARED,
    review_state: ReviewState = ReviewState.NONE,
    with_import_hash: bool = False,
    hash_index: int = 0,
    **fields: Any,
) -> Transaction:
    """A transaction on ``account`` (household and currency from the account).

    ``with_import_hash`` fills ``import_hash`` with ``hash_index`` as the
    running index.
    """
    value = _amount(amount)
    payee_text = raw_payee if raw_payee is not None else (payee.name if payee else None)
    if with_import_hash:
        fields.setdefault(
            "import_hash", import_hash(account.id, posted_date, value, payee_text, hash_index)
        )
    return _add(
        db,
        account.household_id,
        Transaction(
            household_id=account.household_id,
            account_id=account.id,
            posted_date=posted_date,
            amount=value,
            currency=account.currency,
            raw_payee=payee_text,
            payee_id=payee.id if payee else None,
            category_id=category.id if category else None,
            status=status,
            review_state=review_state,
            **fields,
        ),
    )


def make_split(
    db: Session,
    transaction: Transaction,
    *,
    amount: Decimal | str | int,
    category: Category | None = None,
    memo: str | None = None,
    tax_flag: bool = False,
) -> TransactionSplit:
    return _add(
        db,
        transaction.household_id,
        TransactionSplit(
            household_id=transaction.household_id,
            transaction_id=transaction.id,
            amount=_amount(amount),
            category_id=category.id if category else None,
            memo=memo,
            tax_flag=tax_flag,
        ),
    )


def tag_transaction(db: Session, transaction: Transaction, tag: Tag) -> TransactionTag:
    return _add(
        db,
        transaction.household_id,
        TransactionTag(
            household_id=transaction.household_id, transaction_id=transaction.id, tag_id=tag.id
        ),
    )


def make_snapshot(
    db: Session,
    account: Account,
    *,
    as_of_date: date = DEFAULT_DATE,
    balance: Decimal | str | int = "1000.00",
    source: BalanceSource = BalanceSource.MANUAL,
) -> BalanceSnapshot:
    return _add(
        db,
        account.household_id,
        BalanceSnapshot(
            household_id=account.household_id,
            account_id=account.id,
            as_of_date=as_of_date,
            balance=_amount(balance),
            currency=account.currency,
            source=source,
        ),
    )


def make_fx_rate(
    db: Session,
    household_id: uuid.UUID,
    *,
    base: str = "EUR",
    quote: str = "USD",
    rate_date: date = DEFAULT_DATE,
    rate: Decimal | str = "1.1",
    source: str = "manual",
) -> FxRate:
    with household_context(db, household_id):
        row = upsert_rate(db, household_id, base, quote, rate_date, _amount(rate), source=source)
    return row


def make_rule(
    db: Session,
    household_id: uuid.UUID,
    *,
    name: str | None = None,
    sort_order: int = 0,
    conditions: Any = None,
    actions: Any = None,
    enabled: bool = True,
) -> Rule:
    return _add(
        db,
        household_id,
        Rule(
            household_id=household_id,
            name=name or unique_name("Rule"),
            sort_order=sort_order,
            enabled=enabled,
            conditions=conditions if conditions is not None else {"all": []},
            actions=actions if actions is not None else [],
        ),
    )


def make_scheduled_transaction(
    db: Session,
    account: Account,
    *,
    name: str | None = None,
    cadence: Cadence = Cadence.MONTHLY,
    next_due: date = DEFAULT_DATE,
    amount_min: Decimal | str = "-50.00",
    amount_max: Decimal | str = "-50.00",
    origin: ScheduleOrigin = ScheduleOrigin.MANUAL,
    payee: Payee | None = None,
    category: Category | None = None,
) -> ScheduledTransaction:
    return _add(
        db,
        account.household_id,
        ScheduledTransaction(
            household_id=account.household_id,
            account_id=account.id,
            name=name or unique_name("Schedule"),
            cadence=cadence,
            next_due=next_due,
            amount_min=_amount(amount_min),
            amount_max=_amount(amount_max),
            currency=account.currency,
            origin=origin,
            payee_id=payee.id if payee else None,
            category_id=category.id if category else None,
        ),
    )


def make_budget(
    db: Session,
    household_id: uuid.UUID,
    *,
    name: str | None = None,
    period: BudgetPeriod = BudgetPeriod.MONTHLY,
    start_date: date = date(2026, 1, 1),
    end_date: date | None = None,
    currency: str = "USD",
    lines: Mapping[uuid.UUID, Decimal | str] | None = None,
) -> Budget:
    """A budget plus one ``budget_line`` per ``{category_id: amount}``."""
    budget = _add(
        db,
        household_id,
        Budget(
            household_id=household_id,
            name=name or unique_name("Budget"),
            period=period,
            start_date=start_date,
            end_date=end_date,
            currency=currency,
        ),
    )
    for category_id, amount in (lines or {}).items():
        _add(
            db,
            household_id,
            BudgetLine(
                household_id=household_id,
                budget_id=budget.id,
                category_id=category_id,
                amount=_amount(amount),
            ),
        )
    return budget


def make_goal(
    db: Session,
    household_id: uuid.UUID,
    *,
    name: str | None = None,
    target_amount: Decimal | str = "5000.00",
    currency: str = "USD",
    target_date: date | None = None,
    accounts: Iterable[Account] = (),
) -> Goal:
    goal = _add(
        db,
        household_id,
        Goal(
            household_id=household_id,
            name=name or unique_name("Goal"),
            target_amount=_amount(target_amount),
            currency=currency,
            target_date=target_date,
        ),
    )
    for account in accounts:
        _add(
            db,
            household_id,
            GoalAccount(household_id=household_id, goal_id=goal.id, account_id=account.id),
        )
    return goal


def make_goal_contribution(
    db: Session,
    goal: Goal,
    *,
    amount: Decimal | str = "100.00",
    contributed_on: date = DEFAULT_DATE,
    transaction: Transaction | None = None,
) -> GoalContribution:
    return _add(
        db,
        goal.household_id,
        GoalContribution(
            household_id=goal.household_id,
            goal_id=goal.id,
            amount=_amount(amount),
            currency=goal.currency,
            contributed_on=contributed_on,
            transaction_id=transaction.id if transaction else None,
        ),
    )


def seed_categories(db: Session, household_id: uuid.UUID) -> int:
    """The production default category set for a test household."""
    with household_context(db, household_id):
        added = seed_default_categories(db, household_id)
    return added


def synthetic_transactions(
    db: Session,
    account: Account,
    *,
    count: int,
    start: date = date(2026, 1, 1),
    seed: int = 0,
) -> list[Transaction]:
    """``count`` generated transactions over consecutive days (seeded, reproducible)."""
    rng = random.Random(seed)
    rows = []
    for i in range(count):
        cents = rng.randint(-25_000, 5_000)
        rows.append(
            make_transaction(
                db,
                account,
                amount=Decimal(cents).scaleb(-2),
                posted_date=start + timedelta(days=i % 28),
                raw_payee=f"Synthetic Payee {rng.randint(1, 20)}",
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Committed variants
# ---------------------------------------------------------------------------


class LedgerFactories:
    """Committing wrappers: each call runs in its own committed transaction.

    ``ledger.account(household_id, type="savings")`` ->
    ``make_account(db, household_id, type="savings")`` committed. Objects stay
    usable after commit (``expire_on_commit=False``).
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def run[T](self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        with self.session_factory() as db, db.begin():
            return fn(db, *args, **kwargs)

    def institution(self, household_id: uuid.UUID, **kwargs: Any) -> Institution:
        return self.run(make_institution, household_id, **kwargs)

    def account(self, household_id: uuid.UUID, **kwargs: Any) -> Account:
        return self.run(make_account, household_id, **kwargs)

    def category(self, household_id: uuid.UUID, **kwargs: Any) -> Category:
        return self.run(make_category, household_id, **kwargs)

    def payee(self, household_id: uuid.UUID, **kwargs: Any) -> Payee:
        return self.run(make_payee, household_id, **kwargs)

    def tag(self, household_id: uuid.UUID, **kwargs: Any) -> Tag:
        return self.run(make_tag, household_id, **kwargs)

    def transaction(self, account: Account, **kwargs: Any) -> Transaction:
        return self.run(make_transaction, account, **kwargs)

    def split(self, transaction: Transaction, **kwargs: Any) -> TransactionSplit:
        return self.run(make_split, transaction, **kwargs)

    def snapshot(self, account: Account, **kwargs: Any) -> BalanceSnapshot:
        return self.run(make_snapshot, account, **kwargs)

    def fx_rate(self, household_id: uuid.UUID, **kwargs: Any) -> FxRate:
        return self.run(make_fx_rate, household_id, **kwargs)

    def rule(self, household_id: uuid.UUID, **kwargs: Any) -> Rule:
        return self.run(make_rule, household_id, **kwargs)

    def budget(self, household_id: uuid.UUID, **kwargs: Any) -> Budget:
        return self.run(make_budget, household_id, **kwargs)

    def goal(self, household_id: uuid.UUID, **kwargs: Any) -> Goal:
        return self.run(make_goal, household_id, **kwargs)

    def seed_categories(self, household_id: uuid.UUID) -> int:
        return self.run(seed_categories, household_id)

    def grant(
        self, household_id: uuid.UUID, user_id: uuid.UUID, account: Account, level: GrantLevel
    ) -> None:
        self.run(grant_account, household_id, user_id, account.id, level)

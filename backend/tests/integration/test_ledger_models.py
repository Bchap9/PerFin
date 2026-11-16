# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ledger tables: forced RLS, household-consistent references, constraints, cascades.

SECURITY.md F3: Postgres checks foreign keys without row-level security, so
a plain ``account_id`` foreign key would accept another household's account.
Every reference between household-scoped tables includes ``household_id``
(``app.db.types``); these tests try each one across households and expect
the database to refuse. Optional references (``SET NULL`` on delete) use a
deferred composite key, checked here with ``SET CONSTRAINTS ALL IMMEDIATE``
(production checks it at commit).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import AccountGrant, GrantLevel
from app.db.base import Base, household_scoped_tables
from app.db.models import Household
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType, BalanceSnapshot, BalanceSource
from app.domain.budgets.models import Budget, BudgetLine, BudgetPeriod
from app.domain.fx.models import FxRate
from app.domain.goals.models import GoalAccount, GoalContribution
from app.domain.imports.models import ImportTombstone
from app.domain.imports.tombstones import (
    add_tombstone,
    list_tombstones,
    remove_tombstone,
    tombstoned_hashes,
)
from app.domain.transactions.models import (
    Cadence,
    Category,
    CategoryKind,
    Payee,
    ScheduledTransaction,
    ScheduleOrigin,
    Transaction,
    TransactionSplit,
    TransactionTag,
)
from tests.fixtures.factories import grant_account, make_household, make_user
from tests.fixtures.ledger import (
    household_context,
    make_account,
    make_budget,
    make_category,
    make_goal,
    make_goal_contribution,
    make_institution,
    make_payee,
    make_rule,
    make_scheduled_transaction,
    make_snapshot,
    make_split,
    make_tag,
    make_transaction,
    seed_categories,
    tag_transaction,
)

pytestmark = pytest.mark.integration

LEDGER_TABLES = frozenset(
    {
        "institution",
        "account",
        "balance_snapshot",
        "transaction",
        "transaction_split",
        "payee",
        "category",
        "tag",
        "transaction_tag",
        "rule",
        "scheduled_transaction",
        "fx_rate",
        "budget",
        "budget_line",
        "goal",
        "goal_account",
        "goal_contribution",
    }
)


def _refused(db: Session, write: Callable[[], object]) -> None:
    """``write`` (in a savepoint) violates a constraint, deferred ones included."""

    def attempt() -> None:
        with db.begin_nested():
            write()
            db.flush()
            db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    with pytest.raises((IntegrityError, DBAPIError)):
        attempt()


def _insert(db: Session, household_id: uuid.UUID, row: object) -> Callable[[], object]:
    def write() -> object:
        set_context(db, household_id)
        db.add(row)
        db.flush()
        return row

    return write


def _count(db: Session, model: type[object]) -> int:
    return int(db.execute(select(func.count()).select_from(model)).scalar_one())


# ---------------------------------------------------------------------------
# RLS
# ---------------------------------------------------------------------------


def test_every_ledger_table_is_household_scoped_with_forced_rls(db: Session) -> None:
    assert set(household_scoped_tables()) >= LEDGER_TABLES
    rows = db.execute(
        text(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relkind = 'r' AND relname = ANY(:names)"
        ),
        {"names": sorted(LEDGER_TABLES)},
    ).all()
    assert {r[0] for r in rows} == LEDGER_TABLES
    assert all(r[1] and r[2] for r in rows), rows


def _populate(db: Session, household_id: uuid.UUID) -> Account:
    institution = make_institution(db, household_id)
    account = make_account(db, household_id, institution_id=institution.id)
    category = make_category(db, household_id)
    payee = make_payee(db, household_id, default_category=category)
    txn = make_transaction(db, account, payee=payee, category=category)
    make_split(db, txn, amount="-12.34", category=category)
    tag_transaction(db, txn, make_tag(db, household_id))
    make_snapshot(db, account)
    make_rule(db, household_id)
    make_scheduled_transaction(db, account, payee=payee, category=category)
    with household_context(db, household_id):
        db.add(
            FxRate(
                household_id=household_id,
                base="EUR",
                quote="USD",
                rate_date=date(2026, 1, 1),
                rate=Decimal("1.1"),
            )
        )
        db.flush()
    make_budget(db, household_id, lines={category.id: "100"})
    goal = make_goal(db, household_id, accounts=[account])
    make_goal_contribution(db, goal, transaction=txn)
    return account


def test_ledger_rows_are_invisible_to_other_households(db: Session) -> None:
    a = make_household(db)
    b = make_household(db)
    _populate(db, a.id)
    tables = sorted(LEDGER_TABLES)
    # Fixed table names from this module, not input.
    selects = [f"SELECT '{t}' AS t, count(*) AS n FROM \"{t}\"" for t in tables]  # noqa: S608
    count_sql = " UNION ALL ".join(selects)
    set_context(db, a.id)
    mine: dict[str, int] = dict(db.execute(text(count_sql)).all())
    assert all(mine[t] >= 1 for t in tables), mine
    set_context(db, b.id)
    assert set(dict(db.execute(text(count_sql)).all()).values()) == {0}
    set_context(db, None)
    assert set(dict(db.execute(text(count_sql)).all()).values()) == {0}


def test_rows_cannot_be_written_into_another_household(db: Session) -> None:
    a = make_household(db)
    b = make_household(db)
    account = make_account(db, a.id)
    # In B's context, inserting a row that claims household A fails WITH CHECK.
    _refused(
        db,
        lambda: _insert(
            db,
            b.id,
            Account(
                household_id=a.id,
                type=AccountType.CASH,
                name="Synthetic",
                currency="USD",
                is_asset=True,
            ),
        )(),
    )
    # And moving A's account to B is refused too.
    set_context(db, a.id)
    with pytest.raises(DBAPIError), db.begin_nested():
        db.execute(update(Account).where(Account.id == account.id).values(household_id=b.id))


# ---------------------------------------------------------------------------
# Household-consistent references (F3)
# ---------------------------------------------------------------------------


def test_cross_household_parents_are_refused(db: Session) -> None:
    a = make_household(db)
    b = make_household(db)
    member = make_user(db, role="member", household=a)
    account_b = make_account(db, b.id)
    txn_b = make_transaction(db, account_b)
    tag_b = make_tag(db, b.id)
    category_b = make_category(db, b.id)
    account_a = make_account(db, a.id)
    txn_a = make_transaction(db, account_a)
    goal_a = make_goal(db, a.id)
    budget_a = make_budget(db, a.id)
    cases: list[object] = [
        Transaction(
            household_id=a.id,
            account_id=account_b.id,
            posted_date=date(2026, 1, 1),
            amount=Decimal(1),
            currency="USD",
        ),
        TransactionSplit(household_id=a.id, transaction_id=txn_b.id, amount=Decimal(1)),
        TransactionTag(household_id=a.id, transaction_id=txn_a.id, tag_id=tag_b.id),
        BalanceSnapshot(
            household_id=a.id,
            account_id=account_b.id,
            as_of_date=date(2026, 1, 1),
            balance=Decimal(1),
            currency="USD",
            source=BalanceSource.MANUAL,
        ),
        AccountGrant(
            household_id=a.id, user_id=member.id, account_id=account_b.id, level=GrantLevel.VIEW
        ),
        BudgetLine(
            household_id=a.id, budget_id=budget_a.id, category_id=category_b.id, amount=Decimal(1)
        ),
        GoalAccount(household_id=a.id, goal_id=goal_a.id, account_id=account_b.id),
        Category(
            household_id=a.id, parent_id=category_b.id, name="Child", kind=CategoryKind.EXPENSE
        ),
    ]
    for row in cases:
        _refused(db, _insert(db, a.id, row))


def test_cross_household_optional_references_are_refused(db: Session) -> None:
    a = make_household(db)
    b = make_household(db)
    payee_b = make_payee(db, b.id)
    category_b = make_category(db, b.id)
    institution_b = make_institution(db, b.id)
    txn_b = make_transaction(db, make_account(db, b.id))
    account_a = make_account(db, a.id)
    txn_a = make_transaction(db, account_a)
    goal_a = make_goal(db, a.id)
    # Plain ids: a rolled-back savepoint expires ORM objects (and the context).
    payee_b_id, category_b_id, institution_b_id, txn_b_id = (
        payee_b.id,
        category_b.id,
        institution_b.id,
        txn_b.id,
    )
    account_a_id, txn_a_id, goal_a_id = account_a.id, txn_a.id, goal_a.id

    def change(model: type[object], row_id: uuid.UUID, **values: object) -> Callable[[], object]:
        def write() -> object:
            set_context(db, a.id)
            return db.execute(update(model).where(model.id == row_id).values(**values))  # type: ignore[attr-defined]

        return write

    _refused(db, change(Transaction, txn_a_id, payee_id=payee_b_id))
    _refused(db, change(Transaction, txn_a_id, category_id=category_b_id))
    _refused(db, change(Account, account_a_id, institution_id=institution_b_id))
    _refused(
        db,
        _insert(
            db,
            a.id,
            Payee(
                household_id=a.id,
                name="Synthetic",
                normalized_name="synthetic",
                default_category_id=category_b_id,
            ),
        ),
    )
    _refused(
        db,
        _insert(
            db,
            a.id,
            GoalContribution(
                household_id=a.id,
                goal_id=goal_a_id,
                contributed_on=date(2026, 1, 1),
                amount=Decimal(1),
                currency="USD",
                transaction_id=txn_b_id,
            ),
        ),
    )
    _refused(
        db,
        _insert(
            db,
            a.id,
            TransactionSplit(
                household_id=a.id,
                transaction_id=txn_a_id,
                amount=Decimal(1),
                category_id=category_b_id,
            ),
        ),
    )
    # The same references within the household are fine (also at commit time).
    payee_a = make_payee(db, a.id)
    with db.begin_nested():
        change(Transaction, txn_a_id, payee_id=payee_a.id)()
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_currency_follows_the_account(db: Session) -> None:
    a = make_household(db)
    account = make_account(db, a.id, currency="USD")
    _refused(
        db,
        _insert(
            db,
            a.id,
            Transaction(
                household_id=a.id,
                account_id=account.id,
                posted_date=date(2026, 1, 1),
                amount=Decimal(1),
                currency="EUR",
            ),
        ),
    )
    _refused(
        db,
        _insert(
            db,
            a.id,
            BalanceSnapshot(
                household_id=a.id,
                account_id=account.id,
                as_of_date=date(2026, 1, 1),
                balance=Decimal(1),
                currency="EUR",
                source=BalanceSource.MANUAL,
            ),
        ),
    )
    # An empty account may change currency; one with transactions may not.
    set_context(db, a.id)
    with db.begin_nested():
        db.execute(update(Account).where(Account.id == account.id).values(currency="EUR"))
        db.execute(update(Account).where(Account.id == account.id).values(currency="USD"))
    make_transaction(db, account)
    set_context(db, a.id)
    _refused(
        db,
        lambda: db.execute(update(Account).where(Account.id == account.id).values(currency="EUR")),
    )


# ---------------------------------------------------------------------------
# Deletes
# ---------------------------------------------------------------------------


def test_deleting_lookups_clears_optional_references(db: Session) -> None:
    a = make_household(db)
    account = make_account(db, a.id)
    category = make_category(db, a.id)
    payee = make_payee(db, a.id, default_category=category)
    txn = make_transaction(db, account, payee=payee, category=category)
    split = make_split(db, txn, amount="-12.34", category=category)
    set_context(db, a.id)
    with db.begin_nested():
        db.delete(payee)
        db.delete(category)
        db.flush()
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    db.expire_all()
    set_context(db, a.id)
    refreshed = db.get(Transaction, txn.id)
    assert refreshed is not None
    assert (refreshed.payee_id, refreshed.category_id) == (None, None)
    split_row = db.get(TransactionSplit, split.id)
    assert split_row is not None
    assert split_row.category_id is None


def test_parent_category_with_children_is_protected(db: Session) -> None:
    a = make_household(db)
    parent = make_category(db, a.id)
    make_category(db, a.id, parent=parent)
    set_context(db, a.id)
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(text('DELETE FROM "category" WHERE id = :id'), {"id": parent.id})


def test_deleting_an_account_removes_its_data_and_grants(db: Session) -> None:
    owner = make_user(db, role="owner")
    household_id = owner.household_id
    assert household_id is not None
    member = make_user(db, role="member", household=owner.household)
    account = _populate(db, household_id)
    grant_account(db, household_id, member.id, account.id, GrantLevel.VIEW)
    set_context(db, household_id)
    with db.begin_nested():
        db.execute(text('DELETE FROM "account" WHERE id = :id'), {"id": account.id})
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    for model in (
        Transaction,
        TransactionSplit,
        TransactionTag,
        BalanceSnapshot,
        AccountGrant,
        GoalAccount,
    ):
        assert _count(db, model) == 0, model
    # The goal contribution stays, without its transaction.
    contribution = db.execute(select(GoalContribution)).scalar_one()
    assert contribution.transaction_id is None
    assert _count(db, Category) >= 1


def test_deleting_a_household_removes_its_ledger(db: Session) -> None:
    a = make_household(db)
    b = make_household(db)
    _populate(db, a.id)
    seed_categories(db, a.id)
    _populate(db, b.id)
    a_id, b_id = a.id, b.id
    set_context(db, a_id)
    with db.begin_nested():
        db.execute(text("DELETE FROM household WHERE id = :id"), {"id": a_id})
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    gone = db.execute(select(Household.id).where(Household.id == a_id)).scalar_one_or_none()
    assert gone is None
    for t in sorted(LEDGER_TABLES):
        n = db.execute(
            text(f'SELECT count(*) FROM "{t}" WHERE household_id = :h'),  # noqa: S608
            {"h": a_id},
        ).scalar_one()
        assert n == 0, t
    set_context(db, b_id)
    assert _count(db, Transaction) == 1


# ---------------------------------------------------------------------------
# Check constraints
# ---------------------------------------------------------------------------


def test_check_constraints(db: Session) -> None:
    a = make_household(db)
    account = make_account(db, a.id)

    def account_row(**values: object) -> Account:
        fields: dict[str, object] = {
            "household_id": a.id,
            "type": AccountType.CHECKING,
            "name": "Synthetic",
            "currency": "USD",
            "is_asset": True,
        }
        fields.update(values)
        return Account(**fields)

    for bad in (
        account_row(number_last4="12a4"),
        account_row(currency="usd"),
        account_row(type=AccountType.MANUAL_ASSET),  # needs asset_kind
        account_row(asset_kind="vehicle"),  # only for manual_asset
        account_row(opened_on=date(2026, 2, 1), closed_on=date(2026, 1, 1)),
        Transaction(
            household_id=a.id,
            account_id=account.id,
            posted_date=date(2026, 1, 1),
            amount=Decimal(1),
            currency="USD",
            confidence=Decimal("1.5"),
        ),
        FxRate(
            household_id=a.id,
            base="USD",
            quote="USD",
            rate_date=date(2026, 1, 1),
            rate=Decimal(1),
        ),
        FxRate(
            household_id=a.id,
            base="EUR",
            quote="USD",
            rate_date=date(2026, 1, 1),
            rate=Decimal(0),
        ),
        Budget(
            household_id=a.id,
            name="Synthetic",
            period=BudgetPeriod.CUSTOM,
            start_date=date(2026, 1, 1),
            currency="USD",
        ),
    ):
        _refused(db, _insert(db, a.id, bad))
    good = make_account(db, a.id, number_last4="0042", type="manual_asset", asset_kind="vehicle")
    assert good.number_last4 == "0042"
    assert "0042" not in repr(good)


# ---------------------------------------------------------------------------
# Import tombstones (a deleted imported row stays deleted)
# ---------------------------------------------------------------------------


def test_tombstone_is_idempotent_and_goes_with_its_account(db: Session) -> None:
    owner = make_user(db, role="owner")
    household_id = owner.household_id
    assert household_id is not None
    account = make_account(db, household_id)
    imported = make_transaction(db, account, amount="-41.07", with_import_hash=True)
    typed = make_transaction(db, account, amount="-3.00")
    assert imported.import_hash is not None
    set_context(db, household_id)

    assert add_tombstone(db, imported, user_id=owner.id) is True
    assert add_tombstone(db, imported, user_id=owner.id) is False
    # A row a person typed has no import hash: nothing could bring it back.
    assert add_tombstone(db, typed, user_id=owner.id) is False

    kept = db.execute(select(ImportTombstone)).scalars().all()
    assert len(kept) == 1
    tombstone = kept[0]
    assert tombstone.household_id == household_id
    assert tombstone.account_id == account.id
    assert tombstone.import_hash == imported.import_hash
    assert tombstone.deleted_by == owner.id
    assert tombstone.deleted_at is not None
    assert tombstone.posted_date == imported.posted_date
    assert tombstone.amount == Decimal("-41.07")
    listed = list_tombstones(db, household_id, account.id, [imported.import_hash])
    assert [t.id for t in listed] == [tombstone.id]
    assert list_tombstones(db, household_id, account.id, []) == []

    # Taking it back removes it once; a second time there is nothing to take back.
    assert remove_tombstone(db, household_id, uuid.uuid4()) is None
    removed = remove_tombstone(db, household_id, tombstone.id)
    assert removed is not None
    assert removed.import_hash == imported.import_hash
    assert remove_tombstone(db, household_id, tombstone.id) is None
    assert tombstoned_hashes(db, household_id, account.id, [imported.import_hash]) == set()

    assert add_tombstone(db, imported, user_id=None) is True
    with db.begin_nested():
        db.execute(text('DELETE FROM "account" WHERE id = :id'), {"id": account.id})
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert _count(db, ImportTombstone) == 0


def test_tombstoned_hashes_only_names_the_account_asked_for(db: Session) -> None:
    owner = make_user(db, role="owner")
    household_id = owner.household_id
    assert household_id is not None
    first = make_account(db, household_id)
    second = make_account(db, household_id)
    gone = make_transaction(db, first, amount="-18.40", with_import_hash=True)
    kept = make_transaction(db, first, amount="-18.40", with_import_hash=True, hash_index=1)
    elsewhere = make_transaction(db, second, amount="-7.25", with_import_hash=True)
    assert gone.import_hash is not None
    assert kept.import_hash is not None
    assert elsewhere.import_hash is not None
    set_context(db, household_id)
    add_tombstone(db, gone, user_id=owner.id)
    add_tombstone(db, elsewhere, user_id=owner.id)

    asked = [gone.import_hash, kept.import_hash, elsewhere.import_hash]
    assert tombstoned_hashes(db, household_id, first.id, asked) == {gone.import_hash}
    assert tombstoned_hashes(db, household_id, second.id, asked) == {elsewhere.import_hash}
    assert tombstoned_hashes(db, household_id, first.id, [kept.import_hash]) == set()
    assert tombstoned_hashes(db, household_id, first.id, []) == set()
    assert [t.import_hash for t in list_tombstones(db, household_id, second.id, asked)] == [
        elsewhere.import_hash
    ]

    # Another household sees none of it, and cannot take one back.
    other = make_household(db)
    tombstone = db.execute(
        select(ImportTombstone).where(ImportTombstone.account_id == first.id)
    ).scalar_one()
    set_context(db, other.id)
    assert tombstoned_hashes(db, household_id, first.id, asked) == set()
    assert remove_tombstone(db, household_id, tombstone.id) is None
    assert remove_tombstone(db, other.id, tombstone.id) is None
    set_context(db, household_id)
    assert tombstoned_hashes(db, household_id, first.id, asked) == {gone.import_hash}


# ---------------------------------------------------------------------------
# The pinned write (a system write never looks like a person)
# ---------------------------------------------------------------------------


def _untouched(db: Session, household_id: uuid.UUID, *rows: Transaction) -> None:
    """Leave ``rows`` as an import does: ``updated_at == created_at``."""
    set_context(db, household_id)
    db.execute(
        update(Transaction)
        .where(Transaction.id.in_([row.id for row in rows]))
        .values(updated_at=Transaction.created_at)
    )
    for row in rows:
        db.refresh(row)
        assert row.updated_at == row.created_at


def test_pinned_update_keeps_updated_at(db: Session) -> None:
    from app.domain.categorisation.pinned import pinned_update

    a = make_household(db)
    account = make_account(db, a.id)
    category = make_category(db, a.id)
    first = make_transaction(db, account, raw_payee="CORNER GROCER 0417 SPRINGFIELD")
    second = make_transaction(db, account, raw_payee="CORNER GROCER 0932 SPRINGFIELD")
    third = make_transaction(db, account, raw_payee="LAMPLIGHT CINEMA 12")
    _untouched(db, a.id, first, second, third)

    changed = pinned_update(
        db,
        a.id,
        {
            first.id: {"merchant_key": "corner grocer", "suggested_category_id": category.id},
            second.id: {"merchant_key": "corner grocer", "suggested_category_id": category.id},
            # Another set of columns: a second statement of the same call.
            third.id: {
                "merchant_key": "lamplight cinema",
                "category_id": category.id,
                "category_source": "dictionary",
                "category_confidence": Decimal("0.9000"),
            },
            uuid.uuid4(): {"merchant_key": "nobody"},
        },
    )

    assert changed == 3
    # The instances the caller holds show the new values without a refresh call.
    assert (first.merchant_key, first.suggested_category_id) == ("corner grocer", category.id)
    assert (second.merchant_key, second.suggested_category_id) == ("corner grocer", category.id)
    assert (third.category_id, third.category_source, third.category_confidence) == (
        category.id,
        "dictionary",
        Decimal("0.9000"),
    )
    stamps = db.execute(
        select(Transaction.id, Transaction.created_at, Transaction.updated_at).where(
            Transaction.id.in_([first.id, second.id, third.id])
        )
    ).all()
    assert len(stamps) == 3
    assert all(created == updated for _, created, updated in stamps)
    # The same change through the ORM is what a person's edit looks like.
    third.merchant_key = "lamplight"
    db.flush()
    db.refresh(third)
    assert third.updated_at > third.created_at
    # Nothing to write is not an error.
    assert pinned_update(db, a.id, {}) == 0
    assert pinned_update(db, a.id, {first.id: {}}) == 0


def test_pinned_update_refuses_other_columns(db: Session) -> None:
    from app.domain.categorisation.pinned import PINNED_COLUMNS, pinned_update

    a = make_household(db)
    row = make_transaction(db, make_account(db, a.id), amount="-20.00")
    set_context(db, a.id)
    for column in ("amount", "updated_at", "raw_payee", "household_id", "review_state"):
        assert column not in PINNED_COLUMNS
        with pytest.raises(ValueError, match=column):
            pinned_update(db, a.id, {row.id: {"merchant_key": "corner grocer", column: None}})
    db.refresh(row)
    # Refused whole: the allowed column of the same change was not written either.
    assert (row.amount, row.merchant_key) == (Decimal("-20.00"), None)


def test_pinned_update_stays_inside_the_household(db: Session) -> None:
    from app.domain.categorisation.pinned import pinned_update

    a = make_household(db)
    b = make_household(db)
    row_a = make_transaction(db, make_account(db, a.id))
    row_b = make_transaction(db, make_account(db, b.id))
    change = {row_a.id: {"merchant_key": "corner grocer"}}
    # Naming the wrong household finds nothing, even in the owner's context ...
    set_context(db, a.id)
    assert pinned_update(db, b.id, change) == 0
    # ... and another household's context cannot reach the row at all.
    set_context(db, b.id)
    assert pinned_update(db, a.id, change) == 0
    assert pinned_update(db, b.id, {row_b.id: {"merchant_key": "lamplight cinema"}}) == 1
    set_context(db, a.id)
    db.refresh(row_a)
    assert row_a.merchant_key is None
    assert pinned_update(db, a.id, change) == 1
    assert row_a.merchant_key == "corner grocer"


def test_schedule_status_follows_the_active_flag(db: Session) -> None:
    """Code that only knows ``active`` (today's recurring service, an archive
    import) still writes a status the check accepts."""
    a = make_household(db)
    account = make_account(db, a.id)
    by_hand = make_scheduled_transaction(db, account)
    found = make_scheduled_transaction(db, account, origin=ScheduleOrigin.DETECTED)
    assert (by_hand.active, by_hand.status, by_hand.status_source) == (True, "confirmed", "person")
    assert (found.active, found.status, found.status_source) == (True, "active", "detector")
    assert (found.kind, found.kind_source, found.occurrences) == ("other", "detector", 0)
    set_context(db, a.id)
    # A person switches one off and on again through the flag alone.
    found.active = False
    db.flush()
    assert (found.status, found.status_source) == ("paused", "person")
    found.active = True
    db.flush()
    assert (found.status, found.status_source) == ("confirmed", "person")
    # A status set on its own carries the flag with it.
    found.status = "ended"
    db.flush()
    assert found.active is False
    # A plain insert that names only the flag (the archive import's shape).
    table = Base.metadata.tables[ScheduledTransaction.__tablename__]
    row_id = uuid.uuid4()
    db.execute(
        insert(table),
        [
            {
                "id": row_id,
                "household_id": a.id,
                "account_id": account.id,
                "name": "Synthetic series",
                "cadence": Cadence.MONTHLY,
                "next_due": date(2026, 3, 1),
                "amount_min": Decimal("-5.00"),
                "amount_max": Decimal("-5.00"),
                "currency": "USD",
                "origin": ScheduleOrigin.DETECTED,
                "active": False,
            }
        ],
    )
    stored = db.execute(
        select(table.c.status, table.c.status_source, table.c.active).where(table.c.id == row_id)
    ).one()
    assert tuple(stored) == ("paused", "person", False)

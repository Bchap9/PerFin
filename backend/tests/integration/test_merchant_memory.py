# SPDX-License-Identifier: AGPL-3.0-or-later
"""The household's counted corrections (``merchant_memory``).

Design note, section 4, "Learned": per household, merchant key and direction
one row per category with the number of decisions; the answer for a key is
the category with the most decisions (the most recent on a tie) and its
share of all decisions for the key. It is a table, so it survives deleting
and re-importing a statement, and it is bounded.

Merchant keys here are invented shops; no row is a line of a statement.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.rls import set_context
from app.domain.categorisation import memory
from app.domain.categorisation.models import MerchantMemory
from app.domain.categorisation.pinned import pinned_update
from app.domain.transactions.models import Category
from tests.fixtures.factories import make_household
from tests.fixtures.ledger import make_account, make_category, make_split, make_transaction

pytestmark = pytest.mark.integration

T0 = datetime(2026, 3, 2, 8, 30, tzinfo=UTC)
GROCER = "corner grocer"
CINEMA = "lamplight cinema"


def _rows(db: Session) -> int:
    return int(db.execute(select(func.count()).select_from(MerchantMemory)).scalar_one())


def test_direction_follows_the_sign() -> None:
    assert memory.direction_of(Decimal("-4.20")) == "out"
    assert memory.direction_of(Decimal("1800.00")) == "in"
    # A zero amount is not income.
    assert memory.direction_of(Decimal(0)) == "out"


def test_record_counts_and_lookup_names_the_winner(db: Session) -> None:
    household = make_household(db)
    groceries = make_category(db, household.id)
    eating_out = make_category(db, household.id)
    set_context(db, household.id)

    memory.record(db, household.id, GROCER, "out", groceries.id, at=T0)
    memory.record(db, household.id, GROCER, "out", eating_out.id, at=T0 + timedelta(days=1))
    memory.record(db, household.id, GROCER, "out", groceries.id, at=T0 + timedelta(days=2))

    assert _rows(db) == 2
    found = memory.lookup(db, household.id, [(GROCER, "out"), (CINEMA, "out"), (GROCER, "in")])
    assert set(found) == {(GROCER, "out")}
    learned = found[(GROCER, "out")]
    assert learned.category_id == groceries.id
    assert (learned.decisions, learned.total) == (2, 3)
    assert learned.share == Decimal("0.67")
    assert learned.last_decided_at == T0 + timedelta(days=2)
    assert (learned.merchant_key, learned.direction) == (GROCER, "out")
    stored = db.get(MerchantMemory, learned.memory_id)
    assert stored is not None
    assert (stored.category_id, stored.decisions) == (groceries.id, 2)
    assert memory.lookup(db, household.id, []) == {}
    # One merchant filed in one place only: the whole share.
    memory.record(db, household.id, CINEMA, "out", eating_out.id, at=T0)
    assert memory.lookup(db, household.id, [(CINEMA, "out")])[(CINEMA, "out")].share == Decimal(
        "1.00"
    )


def test_a_tie_goes_to_the_latest(db: Session) -> None:
    household = make_household(db)
    first = make_category(db, household.id)
    second = make_category(db, household.id)
    set_context(db, household.id)
    memory.record(db, household.id, GROCER, "out", second.id, at=T0 + timedelta(hours=3))
    memory.record(db, household.id, GROCER, "out", first.id, at=T0)

    learned = memory.lookup(db, household.id, [(GROCER, "out")])[(GROCER, "out")]
    assert learned.category_id == second.id
    assert (learned.decisions, learned.total, learned.share) == (1, 2, Decimal("0.50"))
    # One more decision for the other side ends the tie.
    memory.record(db, household.id, GROCER, "out", first.id, at=T0 + timedelta(hours=1))
    learned = memory.lookup(db, household.id, [(GROCER, "out")])[(GROCER, "out")]
    assert (learned.category_id, learned.decisions, learned.total) == (first.id, 2, 3)


def test_withdraw_moves_a_decision_and_deletes_at_zero(db: Session) -> None:
    household = make_household(db)
    old = make_category(db, household.id)
    new = make_category(db, household.id)
    set_context(db, household.id)
    memory.record(db, household.id, GROCER, "out", old.id, at=T0)
    memory.record(db, household.id, GROCER, "out", old.id, at=T0 + timedelta(days=1))

    # A person changes one of the two rows: the decision moves.
    memory.withdraw(db, household.id, GROCER, "out", old.id)
    memory.record(db, household.id, GROCER, "out", new.id, at=T0 + timedelta(days=2))
    learned = memory.lookup(db, household.id, [(GROCER, "out")])[(GROCER, "out")]
    assert (learned.category_id, learned.decisions, learned.total) == (new.id, 1, 2)
    # The count that was withdrawn keeps the time it was last decided for.
    kept = db.execute(
        select(MerchantMemory).where(MerchantMemory.category_id == old.id)
    ).scalar_one()
    assert (kept.decisions, kept.last_decided_at) == (1, T0 + timedelta(days=1))

    memory.withdraw(db, household.id, GROCER, "out", old.id)
    assert _rows(db) == 1
    # Nothing left to withdraw is no error, nor is a key never seen.
    memory.withdraw(db, household.id, GROCER, "out", old.id)
    memory.withdraw(db, household.id, CINEMA, "out", old.id)
    assert _rows(db) == 1


def test_out_and_in_are_separate(db: Session) -> None:
    household = make_household(db)
    spending = make_category(db, household.id)
    refunds = make_category(db, household.id, kind="income")
    set_context(db, household.id)
    memory.record(db, household.id, GROCER, "out", spending.id, at=T0)
    memory.record(db, household.id, GROCER, "in", refunds.id, at=T0)
    memory.record(db, household.id, GROCER, "in", refunds.id, at=T0 + timedelta(days=1))

    found = memory.lookup(db, household.id, [(GROCER, "out"), (GROCER, "in")])
    assert (found[(GROCER, "out")].category_id, found[(GROCER, "out")].total) == (spending.id, 1)
    assert (found[(GROCER, "in")].category_id, found[(GROCER, "in")].total) == (refunds.id, 2)
    memory.withdraw(db, household.id, GROCER, "in", spending.id)
    assert _rows(db) == 2
    # Another household learned nothing from this.
    other = make_household(db)
    set_context(db, other.id)
    assert memory.lookup(db, other.id, [(GROCER, "out")]) == {}
    assert memory.lookup(db, household.id, [(GROCER, "out")]) == {}


def test_forget_removes_every_category_of_the_entry(db: Session) -> None:
    household = make_household(db)
    one = make_category(db, household.id)
    two = make_category(db, household.id)
    set_context(db, household.id)
    memory.record(db, household.id, GROCER, "out", one.id, at=T0)
    memory.record(db, household.id, GROCER, "out", two.id, at=T0)
    memory.record(db, household.id, GROCER, "in", one.id, at=T0)
    memory.record(db, household.id, CINEMA, "out", two.id, at=T0)
    listed = memory.entries(db, household.id, after=None, limit=10)
    assert [(e.merchant_key, e.direction) for e in listed] == [
        (GROCER, "in"),
        (GROCER, "out"),
        (CINEMA, "out"),
    ]
    # Pages follow the same order, from the entry named by the cursor.
    page = memory.entries(db, household.id, after=None, limit=2)
    assert [e.memory_id for e in page] == [e.memory_id for e in listed[:2]]
    rest = memory.entries(db, household.id, after=page[-1].memory_id, limit=2)
    assert [e.memory_id for e in rest] == [listed[2].memory_id]
    assert memory.entries(db, household.id, after=uuid.uuid4(), limit=2) == []

    entry = memory.lookup(db, household.id, [(GROCER, "out")])[(GROCER, "out")]
    # Another household cannot forget it.
    other = make_household(db)
    set_context(db, other.id)
    assert memory.forget(db, other.id, entry.memory_id) == 0
    assert memory.forget(db, household.id, entry.memory_id) == 0
    set_context(db, household.id)
    assert memory.forget(db, household.id, entry.memory_id) == 2
    assert memory.forget(db, household.id, entry.memory_id) == 0
    assert memory.forget(db, household.id, uuid.uuid4()) == 0
    left = memory.entries(db, household.id, after=None, limit=10)
    assert [(e.merchant_key, e.direction) for e in left] == [(GROCER, "in"), (CINEMA, "out")]


def test_the_bound_evicts_the_oldest(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory, "MAX_ROWS", 3)
    household = make_household(db)
    other = make_household(db)
    category = make_category(db, household.id)
    elsewhere = make_category(db, other.id)
    set_context(db, other.id)
    memory.record(db, other.id, "harbour ferry", "out", elsewhere.id, at=T0 - timedelta(days=90))
    set_context(db, household.id)
    keys = ["alder bakery", "birch books", "cedar cycles", "dune diner"]
    for day, key in enumerate(keys[:3]):
        memory.record(db, household.id, key, "out", category.id, at=T0 + timedelta(days=day))
    # Deciding again for the oldest makes it the newest; nothing is evicted yet.
    memory.record(db, household.id, keys[0], "out", category.id, at=T0 + timedelta(days=5))
    assert _rows(db) == 3

    memory.record(db, household.id, keys[3], "out", category.id, at=T0 + timedelta(days=6))

    found = memory.lookup(db, household.id, [(key, "out") for key in keys])
    assert set(found) == {(keys[0], "out"), (keys[2], "out"), (keys[3], "out")}
    assert found[(keys[0], "out")].decisions == 2
    # The bound is per household.
    set_context(db, other.id)
    assert set(memory.lookup(db, other.id, [("harbour ferry", "out")])) == {
        ("harbour ferry", "out")
    }


def test_seed_from_legacy_counts_rows_and_skips_known_keys(db: Session) -> None:
    household = make_household(db)
    account = make_account(db, household.id)
    groceries = make_category(db, household.id)
    fun = make_category(db, household.id)
    income = make_category(db, household.id, kind="income")

    def legacy(key: str | None, category: Category, amount: str, source: str) -> None:
        row = make_transaction(db, account, amount=amount, category=category)
        pinned_update(db, household.id, {row.id: {"merchant_key": key, "category_source": source}})

    set_context(db, household.id)
    for _ in range(3):
        legacy(GROCER, groceries, "-31.10", "legacy")
    legacy(GROCER, fun, "-8.00", "legacy")
    legacy(GROCER, income, "12.00", "legacy")  # a refund: the other direction
    legacy(CINEMA, fun, "-14.00", "legacy")
    legacy("alder bakery", groceries, "-6.00", "legacy")
    legacy("birch books", fun, "-22.00", "dictionary")  # a guess is not a decision
    legacy("cedar cycles", fun, "-80.00", "person")  # counted when the person decided
    legacy(None, fun, "-3.00", "legacy")  # no merchant to remember
    # A split row is legacy without a category of its own: nothing to count.
    split = make_transaction(db, account, amount="-40.00")
    make_split(db, split, amount="-40.00", category=fun)
    pinned_update(
        db, household.id, {split.id: {"merchant_key": "dune diner", "category_source": "legacy"}}
    )
    # The household already taught this key: its entry is left as it is.
    memory.record(db, household.id, "alder bakery", "out", fun.id, at=T0)

    assert memory.seed_from_legacy(db, household.id) == 4

    keys: list[tuple[str, memory.Direction]] = [
        (GROCER, "out"),
        (GROCER, "in"),
        (CINEMA, "out"),
        ("alder bakery", "out"),
        ("birch books", "out"),
        ("cedar cycles", "out"),
        ("dune diner", "out"),
    ]
    found = memory.lookup(db, household.id, keys)
    assert set(found) == {(GROCER, "out"), (GROCER, "in"), (CINEMA, "out"), ("alder bakery", "out")}
    grocer = found[(GROCER, "out")]
    assert (grocer.category_id, grocer.decisions, grocer.total) == (groceries.id, 3, 4)
    assert grocer.share == Decimal("0.75")
    assert (found[(GROCER, "in")].category_id, found[(GROCER, "in")].total) == (income.id, 1)
    assert (found[("alder bakery", "out")].category_id, found[("alder bakery", "out")].total) == (
        fun.id,
        1,
    )
    # A second run adds nothing: every key has an entry now.
    assert memory.seed_from_legacy(db, household.id) == 0
    assert found == memory.lookup(db, household.id, keys)
    # Another household's rows are not counted.
    other = make_household(db)
    set_context(db, other.id)
    assert memory.seed_from_legacy(db, other.id) == 0


def test_deleting_the_category_removes_its_memory(db: Session) -> None:
    household = make_household(db)
    going = make_category(db, household.id)
    staying = make_category(db, household.id)
    set_context(db, household.id)
    memory.record(db, household.id, GROCER, "out", going.id, at=T0)
    memory.record(db, household.id, GROCER, "out", going.id, at=T0 + timedelta(days=1))
    memory.record(db, household.id, GROCER, "out", staying.id, at=T0)

    with db.begin_nested():
        db.execute(text('DELETE FROM "category" WHERE id = :id'), {"id": going.id})
        db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    learned = memory.lookup(db, household.id, [(GROCER, "out")])[(GROCER, "out")]
    assert (learned.category_id, learned.decisions, learned.total) == (staying.id, 1, 1)
    assert learned.share == Decimal("1.00")

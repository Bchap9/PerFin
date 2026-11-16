# SPDX-License-Identifier: AGPL-3.0-or-later
"""The default category set, second edition (design note, section 1).

Twelve groups and 68 leaves: the 55 of the first edition, whose keys never
change, and the 13 a merchant dictionary needs. A new household gets all of
them; an existing one gets only the 13, under the groups it still has, and a
default it deleted stays deleted.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.rls import set_context
from app.domain.transactions import categories
from app.domain.transactions.categories import (
    DEFAULT_CATEGORIES,
    add_new_defaults,
    default_category_keys,
    seed_default_categories,
)
from app.domain.transactions.models import Category, CategoryKind
from tests.fixtures.factories import make_household
from tests.fixtures.ledger import make_category

#: The leaves of the first edition, written out: a household's rows, rules
#: and budgets hang on these keys.
FIRST_EDITION = (
    "income.salary",
    "income.bonus",
    "income.interest",
    "income.dividends",
    "income.refunds",
    "income.gifts",
    "income.other",
    "housing.rent",
    "housing.mortgage_interest",
    "housing.property_tax",
    "housing.insurance",
    "housing.maintenance",
    "housing.fees",
    "utilities.electricity",
    "utilities.heating",
    "utilities.water",
    "utilities.internet",
    "utilities.phone",
    "food.groceries",
    "food.restaurants",
    "food.coffee",
    "transport.fuel",
    "transport.public_transit",
    "transport.parking",
    "transport.car_maintenance",
    "transport.car_insurance",
    "transport.taxi",
    "health.medical",
    "health.pharmacy",
    "health.dental",
    "health.insurance",
    "health.fitness",
    "shopping.clothing",
    "shopping.electronics",
    "shopping.household",
    "leisure.subscriptions",
    "leisure.hobbies",
    "leisure.events",
    "leisure.travel",
    "family.personal_care",
    "family.education",
    "family.childcare",
    "family.pets",
    "family.gifts",
    "family.donations",
    "financial.bank_fees",
    "financial.interest_paid",
    "financial.taxes",
    "financial.insurance",
    "transfers.between_accounts",
    "transfers.credit_card_payment",
    "transfers.savings",
    "transfers.investments",
    "transfers.loan_payment",
    "excluded.reimbursable",
)

SECOND_EDITION = {
    "food.fast_food": "Fast food",
    "food.alcohol": "Alcohol and bars",
    "food.delivery": "Food delivery",
    "shopping.general": "General merchandise",
    "shopping.home": "Home and garden",
    "leisure.games": "Games",
    "leisure.news": "News and books",
    "leisure.software": "Software and apps",
    "leisure.memberships": "Memberships",
    "utilities.other": "Other utilities",
    "housing.improvement": "Home improvement",
    "financial.services": "Financial and legal services",
    "transfers.cash": "Cash and ATM",
}

GROUPS = 12
LEAVES = 68


def _leaves() -> dict[str, categories.DefaultCategory]:
    return {leaf.key: leaf for group in DEFAULT_CATEGORIES for leaf in group.children}


def _keys(db: Session, household_id: uuid.UUID) -> set[str]:
    return {
        key
        for key in db.execute(
            select(Category.system_key).where(Category.household_id == household_id)
        ).scalars()
        if key is not None
    }


def _count(db: Session, household_id: uuid.UUID) -> int:
    return int(
        db.execute(
            select(func.count()).select_from(Category).where(Category.household_id == household_id)
        ).scalar_one()
    )


def _by_key(db: Session, household_id: uuid.UUID, key: str) -> Category:
    return db.execute(
        select(Category).where(Category.household_id == household_id, Category.system_key == key)
    ).scalar_one()


def _first_edition_household(db: Session) -> uuid.UUID:
    """A household as the first edition left it: 12 groups and the 55 leaves."""
    household = make_household(db)
    set_context(db, household.id)
    seed_default_categories(db, household.id)
    for key in SECOND_EDITION:
        db.delete(_by_key(db, household.id, key))
    db.flush()
    assert _count(db, household.id) == GROUPS + len(FIRST_EDITION)
    return household.id


def test_the_default_set_has_68_leaves_in_12_groups() -> None:
    assert categories.TAXONOMY_VERSION == 2
    assert len(DEFAULT_CATEGORIES) == GROUPS
    leaves = _leaves()
    assert len(leaves) == LEAVES
    assert len(default_category_keys()) == GROUPS + LEAVES
    assert len(set(default_category_keys())) == GROUPS + LEAVES
    added = {key: leaf.name for key, leaf in leaves.items() if leaf.since == 2}
    assert added == SECOND_EDITION
    assert {leaf.since for leaf in leaves.values()} == {1, 2}
    assert {group.since for group in DEFAULT_CATEGORIES} == {1}
    # A leaf sits under the group its key names and has that group's kind.
    for group in DEFAULT_CATEGORIES:
        for leaf in group.children:
            assert leaf.key.split(".")[0] == group.key
            assert leaf.kind is group.kind
    # Sibling names are unique ignoring case (the service's rule).
    for group in DEFAULT_CATEGORIES:
        names = [leaf.name.lower() for leaf in group.children]
        assert len(names) == len(set(names))
    assert leaves["leisure.subscriptions"].name == "Streaming"


def test_keys_of_the_first_edition_did_not_change() -> None:
    assert len(FIRST_EDITION) == 55
    first = [key for key, leaf in _leaves().items() if leaf.since == 1]
    assert sorted(first) == sorted(FIRST_EDITION)
    assert not set(FIRST_EDITION) & set(SECOND_EDITION)


def test_cash_card_payment_loan_and_transfers_are_transfer_kind() -> None:
    leaves = _leaves()
    for key in (
        "transfers.cash",
        "transfers.credit_card_payment",
        "transfers.loan_payment",
        "transfers.between_accounts",
    ):
        assert leaves[key].kind is CategoryKind.TRANSFER, key
    # What a descriptor's kind maps to otherwise keeps its own kind.
    assert leaves["income.salary"].kind is CategoryKind.INCOME
    assert leaves["income.refunds"].kind is CategoryKind.INCOME
    assert leaves["financial.bank_fees"].kind is CategoryKind.EXPENSE
    assert leaves["financial.interest_paid"].kind is CategoryKind.EXPENSE


@pytest.mark.integration
def test_a_new_household_gets_all_of_them(db: Session) -> None:
    household = make_household(db)
    set_context(db, household.id)

    assert seed_default_categories(db, household.id) == GROUPS + LEAVES
    assert _keys(db, household.id) == set(default_category_keys())
    assert _by_key(db, household.id, "leisure.subscriptions").name == "Streaming"
    cash = _by_key(db, household.id, "transfers.cash")
    assert cash.kind is CategoryKind.TRANSFER
    assert cash.parent_id == _by_key(db, household.id, "transfers").id
    # Nothing is left for a later edition to add, and a second seed adds nothing.
    assert add_new_defaults(db, household.id, 1) == 0
    assert seed_default_categories(db, household.id) == 0
    assert _count(db, household.id) == GROUPS + LEAVES


@pytest.mark.integration
def test_an_existing_household_gets_only_the_new_ones(db: Session) -> None:
    household_id = _first_edition_household(db)
    streaming = _by_key(db, household_id, "leisure.subscriptions")
    streaming.name = "Streaming and subscriptions"  # what the first edition called it
    db.delete(_by_key(db, household_id, "food.coffee"))
    db.flush()

    assert add_new_defaults(db, household_id, 1) == 13

    keys = _keys(db, household_id)
    assert set(SECOND_EDITION) <= keys
    assert "food.coffee" not in keys
    assert _count(db, household_id) == GROUPS + len(FIRST_EDITION) - 1 + 13
    for key, name in SECOND_EDITION.items():
        leaf = _by_key(db, household_id, key)
        parent = _by_key(db, household_id, key.split(".")[0])
        assert (leaf.name, leaf.parent_id, leaf.kind) == (name, parent.id, parent.kind)
    # Nothing of the first edition is renamed.
    db.refresh(streaming)
    assert streaming.name == "Streaming and subscriptions"
    # Already at this edition, or run twice: nothing more.
    assert add_new_defaults(db, household_id, 2) == 0
    assert add_new_defaults(db, household_id, 1) == 0
    assert "food.coffee" not in _keys(db, household_id)


@pytest.mark.integration
def test_a_removed_group_gets_nothing(db: Session) -> None:
    household_id = _first_edition_household(db)
    for leaf in db.execute(
        select(Category).where(
            Category.household_id == household_id, Category.system_key.like("leisure.%")
        )
    ).scalars():
        db.delete(leaf)
    db.flush()
    db.delete(_by_key(db, household_id, "leisure"))
    db.flush()
    # A group of their own with the default's name is not the default group.
    make_category(db, household_id, name="Leisure")
    set_context(db, household_id)

    assert add_new_defaults(db, household_id, 1) == 9

    keys = _keys(db, household_id)
    assert not {key for key in keys if key.split(".")[0] == "leisure"}
    assert {key for key in SECOND_EDITION if not key.startswith("leisure.")} <= keys


@pytest.mark.integration
def test_a_same_named_category_takes_the_key(db: Session) -> None:
    household_id = _first_edition_household(db)
    food = _by_key(db, household_id, "food")
    theirs = make_category(db, household_id, name="fast FOOD", parent=food)
    # The same name under another parent is not the same category.
    elsewhere = make_category(
        db, household_id, name="Games", parent=_by_key(db, household_id, "family")
    )
    set_context(db, household_id)

    assert add_new_defaults(db, household_id, 1) == 12

    db.refresh(theirs)
    db.refresh(elsewhere)
    assert theirs.system_key == "food.fast_food"
    assert theirs.name == "fast FOOD"
    assert elsewhere.system_key is None
    assert _by_key(db, household_id, "leisure.games").id != elsewhere.id
    under_food = db.execute(
        select(func.lower(Category.name)).where(
            Category.household_id == household_id, Category.parent_id == food.id
        )
    ).scalars()
    assert sorted(under_food).count("fast food") == 1

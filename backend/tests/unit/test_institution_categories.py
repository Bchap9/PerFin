# SPDX-License-Identifier: AGPL-3.0-or-later
"""A card issuer's category words read as default category keys."""

from __future__ import annotations

import pytest

from app.domain.categorisation.institution import GROUPS, LABELS, category_key
from app.domain.transactions.categories import DEFAULT_CATEGORIES


@pytest.mark.parametrize(
    ("label", "key"),
    [
        ("Restaurant-Bar & Café", "food.restaurants"),
        ("Restaurant-Restaurant", "food.restaurants"),
        ("Transportation-Fuel", "transport.fuel"),
        ("Transportation-Taxis & Coach", "transport.taxi"),
        ("Merchandise & Supplies-Groceries", "food.groceries"),
        ("Merchandise & Supplies-Hardware Supplies", "shopping.home"),
        ("Merchandise & Supplies-Pharmacies", "health.pharmacy"),
        ("Business Services-Health Care Services", "health.medical"),
        ("Travel-Lodging", "leisure.travel"),
        ("Other-Charities", "family.donations"),
        ("Fees & Adjustments-Fees & Adjustments", "financial.bank_fees"),
        ("Food & Drink", "food.restaurants"),
        ("Gas", "transport.fuel"),
        ("Dining", "food.restaurants"),
        ("Supermarkets", "food.groceries"),
        ("  restaurant-bar & cafe ", "food.restaurants"),
    ],
)
def test_issuer_labels(label: str, key: str) -> None:
    assert category_key(label) == key


def test_an_unlisted_subcategory_falls_back_to_its_group() -> None:
    assert category_key("Restaurant-Synthetic New Kind") == "food.restaurants"
    assert category_key("Transportation-Synthetic Ferry") == "transport"


@pytest.mark.parametrize(
    "label",
    [
        None,
        "",
        "Other-Miscellaneous",
        "Business Services-Other Services",
        "Merchandise & Supplies-Synthetic Unknown",
        "Synthetic Words",
        "x" * 200,
    ],
)
def test_labels_that_say_nothing(label: str | None) -> None:
    assert category_key(label) is None


def test_every_key_is_a_default_category() -> None:
    known: set[str] = set()
    for group in DEFAULT_CATEGORIES:
        known.add(group.key)
        known.update(child.key for child in group.children)
    assert set(LABELS.values()) | set(GROUPS.values()) <= known

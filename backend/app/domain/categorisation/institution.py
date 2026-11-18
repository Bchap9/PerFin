# SPDX-License-Identifier: AGPL-3.0-or-later
"""A card issuer's own category words, as a default category key.

Card exports often carry the issuer's category for each row (American
Express ``Restaurant-Bar & Café``, Chase ``Food & Drink``, Capital One
``Dining``, Discover ``Supermarkets``). The importer keeps a file's category
when it names one of the household's own categories; :func:`category_key`
reads the rest, the issuer's wording, as the ``system_key`` of a default
category (``food.restaurants``), which the engine then weighs as one guess
among its others (``engine.categorise_rows``, ``file_hints``).

The words are compared normalised (case, accents and punctuation do not
matter). American Express prints ``<group>-<subcategory>``: the whole label
is looked up first, then its group alone (:data:`GROUPS`), so a
subcategory not listed still lands in its group's category. A label that
says nothing about what was bought (``Other-Miscellaneous``,
``Business Services-Other Services``) answers ``None``.

The words are the issuers' public category names, nothing from anyone's
statement.
"""

from __future__ import annotations

import unicodedata
from typing import Final

from app.ingestion.parsers.text import normalise

#: Whole labels (normalised) to a default category key.
LABELS: Final[dict[str, str]] = {
    # American Express: "<group>-<subcategory>".
    "restaurant restaurant": "food.restaurants",
    "restaurant bar cafe": "food.restaurants",
    "restaurant fast food restaurant": "food.fast_food",
    "transportation fuel": "transport.fuel",
    "transportation taxis coach": "transport.taxi",
    "transportation parking charges": "transport.parking",
    "transportation tolls fees": "transport.parking",
    "transportation auto services": "transport.car_maintenance",
    "transportation rail services": "transport.public_transit",
    "transportation auto rental": "leisure.travel",
    "transportation vehicle rental": "leisure.travel",
    "merchandise supplies groceries": "food.groceries",
    "merchandise supplies computer supplies": "shopping.electronics",
    "merchandise supplies electronics stores": "shopping.electronics",
    "merchandise supplies hardware supplies": "shopping.home",
    "merchandise supplies furnishing": "shopping.home",
    "merchandise supplies florists garden": "shopping.home",
    "merchandise supplies clothing stores": "shopping.clothing",
    "merchandise supplies pharmacies": "health.pharmacy",
    "merchandise supplies book stores": "leisure.news",
    "merchandise supplies sporting goods stores": "leisure.hobbies",
    "merchandise supplies internet purchase": "shopping.general",
    "merchandise supplies general retail": "shopping.general",
    "merchandise supplies department stores": "shopping.general",
    "merchandise supplies wholesale stores": "shopping.general",
    "merchandise supplies mail order": "shopping.general",
    "merchandise supplies arts jewelry": "shopping.general",
    "merchandise supplies industrial supplies": "shopping.general",
    "business services health care services": "health.medical",
    "business services internet services": "utilities.internet",
    "business services office supplies": "shopping.general",
    "communications cable internet comm": "utilities.internet",
    "communications telephone comm": "utilities.phone",
    "entertainment theatrical events": "leisure.events",
    "entertainment sports events": "leisure.events",
    "entertainment general events": "leisure.events",
    "entertainment associations": "leisure.memberships",
    "travel airline": "leisure.travel",
    "travel lodging": "leisure.travel",
    "travel travel agencies": "leisure.travel",
    "other charities": "family.donations",
    "other education": "family.education",
    "fees adjustments fees adjustments": "financial.bank_fees",
    # Chase.
    "food drink": "food.restaurants",
    "groceries": "food.groceries",
    "gas": "transport.fuel",
    "automotive": "transport.car_maintenance",
    "health wellness": "health",
    "bills utilities": "utilities",
    "personal": "family.personal_care",
    "home": "shopping.home",
    "education": "family.education",
    "gifts donations": "family.donations",
    "entertainment": "leisure.events",
    "travel": "leisure.travel",
    "shopping": "shopping.general",
    "fees adjustments": "financial.bank_fees",
    # Capital One.
    "dining": "food.restaurants",
    "grocery": "food.groceries",
    "gas automotive": "transport.fuel",
    "merchandise": "shopping.general",
    "lodging": "leisure.travel",
    "airfare": "leisure.travel",
    "car rental": "leisure.travel",
    "other travel": "leisure.travel",
    "health care": "health.medical",
    "phone cable": "utilities",
    "internet": "utilities.internet",
    "fee interest charge": "financial.bank_fees",
    # Discover.
    "restaurants": "food.restaurants",
    "supermarkets": "food.groceries",
    "gasoline": "transport.fuel",
    "department stores": "shopping.general",
    "warehouse clubs": "shopping.general",
    "home improvement": "shopping.home",
    "medical services": "health.medical",
    "travel entertainment": "leisure.travel",
}

#: An American Express group (the words before the hyphen) when its
#: subcategory is not in :data:`LABELS`. ``Other``, ``Business Services``
#: and ``Merchandise & Supplies`` alone say too little and are left out.
GROUPS: Final[dict[str, str]] = {
    "restaurant": "food.restaurants",
    "transportation": "transport",
    "travel": "leisure.travel",
    "entertainment": "leisure.events",
    "communications": "utilities",
    "fees adjustments": "financial.bank_fees",
}

#: Longest label read (a cell longer than this is no category).
MAX_LABEL: Final = 80


def _plain(text: str) -> str:
    """Normalised, accents dropped (``Café`` -> ``cafe``)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return normalise("".join(ch for ch in decomposed if not unicodedata.combining(ch)))


def category_key(label: str | None) -> str | None:
    """The default category key an issuer's category label names, or ``None``."""
    if not label or len(label) > MAX_LABEL:
        return None
    whole = _plain(label)
    if not whole:
        return None
    found = LABELS.get(whole)
    if found is not None:
        return found
    group, hyphen, _ = label.partition("-")
    if not hyphen:
        return None
    return GROUPS.get(_plain(group))

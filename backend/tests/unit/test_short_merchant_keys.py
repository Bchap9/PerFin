# SPDX-License-Identifier: AGPL-3.0-or-later
"""A merchant key of one or two letters names nobody.

What is left of a line once the bank's words, numbers and place are gone can
be a single letter or two ("E", "XY #0042"): a column cut short, an initial,
a code. As a key it would join every such line of the household into one
merchant, so that one correction, one rule or one series would cover rows
that have nothing to do with each other. The key is then ``None``, as for a
line that names nobody; a brand the dictionary knows by two letters keeps
its own key. Clean-up's core is left as it is: the dictionary and the
keywords still read it.

Real clean-up, real dictionary; every descriptor is invented.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.categorisation import cleanup, engine
from app.domain.categorisation.memory import Direction, Learned


@pytest.mark.parametrize(
    "raw",
    [
        "E",
        "Q",
        "XY",
        "POS DEBIT 0042 E",
        "CHECKCARD 1104 ZQ #0042",
        "XY 0042",
        "Z.",
        "J K",
    ],
)
def test_a_key_of_fewer_than_three_letters_names_nobody(raw: str) -> None:
    identity = engine.identify(raw)

    assert identity.key is None, raw
    assert identity.name is None
    assert identity.hit is None
    assert identity.also is None
    # Clean-up still read the line: only the key is withheld.
    assert identity.core == cleanup.clean(raw).core != ""


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("ZQX", "zqx"),
        ("POS DEBIT 0042 ZQX", "zqx"),
        ("ZQ BAKEHOUSE", "zq bakehouse"),
        ("E VORTEXA", "e vortexa"),
        ("J K L", "j k l"),
    ],
)
def test_three_letters_or_more_are_a_key(raw: str, key: str) -> None:
    assert engine.identify(raw).key == key


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        # Brands the dictionary knows by two letters keep the dictionary's key.
        ("BP#8812345 RIVERTON", "bp"),
        ("QT 0412", "quiktrip"),
        ("H&M 0187", "h and m"),
    ],
)
def test_a_short_brand_the_dictionary_knows_keeps_its_key(raw: str, key: str) -> None:
    identity = engine.identify(raw)

    assert identity.hit is not None, raw
    assert identity.key == key


def test_what_was_learned_under_a_short_key_answers_nothing() -> None:
    # A household keyed before this change may hold an entry under "e".
    entry = Learned(
        memory_id=uuid.uuid4(),
        category_id=uuid.uuid4(),
        decisions=3,
        total=3,
        share=Decimal(1),
        last_decided_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    learned: dict[tuple[str, Direction], Learned] = {("e", "out"): entry}

    assert engine.learned_for(engine.identify("E"), "out", learned) is None

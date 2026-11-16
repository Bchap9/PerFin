# SPDX-License-Identifier: AGPL-3.0-or-later
"""Response models of ``/categorisation`` (the summary and what was learned).

Counts are over the accounts the caller may see. A share is decimal text
with two places. ``merchant_key`` and ``merchant`` are shown to the people of
the household only; neither is audited or exposed to an AI client.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel

from app.domain.transactions.schemas import CategorySource

#: Page size bounds of the learned list.
DEFAULT_LEARNED: Final = 50
MAX_LEARNED: Final = 200


class SourceCount(BaseModel):
    """How many transactions got their category this way (``null``: nobody decided)."""

    source: CategorySource | None
    transactions: int


class CategorisationSummary(BaseModel):
    """Where categorisation stands for the household.

    ``up_to_date``: every row was last evaluated with the dictionary and the
    clean-up that ship with this version. ``uncategorised`` rows have no
    category and no split; ``suggestions`` of them carry a suggestion.
    """

    dictionary_version: int
    dictionary_merchants: int
    cleaner_version: int
    up_to_date: bool
    by_source: list[SourceCount]
    uncategorised: int
    suggestions: int
    learned_merchants: int


class LearnedCategory(BaseModel):
    """What the household's own corrections say about one merchant.

    ``decisions`` of ``total`` person-decided transactions of the merchant
    went to ``category_id`` (``share``). ``rule_id``: the merchant already has
    an "always" rule. ``offer_rule``: the household was unanimous often
    enough that a rule is worth offering; ``example_transaction_id`` is a row
    to make it from.
    """

    id: uuid.UUID
    merchant_key: str
    merchant: str
    direction: Literal["out", "in"]
    category_id: uuid.UUID
    decisions: int
    total: int
    share: str
    last_decided_at: datetime
    rule_id: uuid.UUID | None
    offer_rule: bool
    example_transaction_id: uuid.UUID | None


class LearnedCategoryPage(BaseModel):
    items: list[LearnedCategory]
    next_cursor: uuid.UUID | None

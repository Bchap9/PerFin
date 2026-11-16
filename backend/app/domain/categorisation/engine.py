# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who a transaction paid, what it probably is, and the one write path for guesses.

:func:`identify` turns a descriptor into an :class:`Identity` (merchant key,
display name, kind). :func:`categorise_rows` is called for rows just created,
:func:`person_decided` when a person set or cleared a category, and
:func:`reconsider` to look again at rows nobody decided. Every write here
goes through ``app.domain.categorisation.pinned.pinned_update`` and never
looks like a person's edit.

This is the declaration the other modules build against: for now
``identify`` knows no merchant, and the three write functions write nothing.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from app.domain.categorisation import cleanup, dictionary
from app.domain.categorisation.cleanup import DescriptorKind
from app.domain.categorisation.dictionary import Hit
from app.domain.transactions.models import MERCHANT_KEY_MAX, Transaction

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from starlette.requests import HTTPConnection

#: A guess at least this sure is written as the category; below it, as a suggestion.
APPLY_MIN: Final = Decimal("0.80")
#: Tokens of the core that make the key of a merchant the dictionary does not know.
KEY_TOKENS: Final = 4


@dataclass(frozen=True)
class Identity:
    """Who a descriptor names (module doc)."""

    #: The merchant key, or ``None`` when the descriptor names nobody.
    key: str | None
    #: The name to show for it.
    name: str | None
    kind: DescriptorKind
    via: str | None
    hit: Hit | None
    core: str


@dataclass(frozen=True)
class Decision:
    """What to write for one row: a category (a fact), a suggestion, or nothing."""

    category_id: uuid.UUID | None = None
    suggested_category_id: uuid.UUID | None = None
    source: str | None = None
    confidence: Decimal | None = None


@dataclass
class Summary:
    """What one pass did (counts and account ids only)."""

    scanned: int = 0
    by_source: Counter[str] = field(default_factory=Counter)
    suggested: int = 0
    keyed: int = 0
    changed_accounts: set[uuid.UUID] = field(default_factory=set)


def _key_of(core: str) -> str | None:
    """The first :data:`KEY_TOKENS` tokens of ``core`` that fit a merchant key."""
    tokens = core.split()[:KEY_TOKENS]
    while tokens and len(" ".join(tokens)) > MERCHANT_KEY_MAX:
        tokens.pop()
    if tokens:
        return " ".join(tokens)
    # One token longer than a key: its start still names the merchant.
    return core[:MERCHANT_KEY_MAX].strip() or None


def display_name(key: str | None) -> str | None:
    """The name to show for a merchant key: the dictionary's, else the key in title case."""
    if not key:
        return None
    return dictionary.name_for(key) or key.title()


def identify(raw: str | None) -> Identity:
    """Who ``raw`` names: the dictionary's merchant, else the start of the core."""
    cleaned = cleanup.clean(raw)
    hit = dictionary.lookup(cleaned.core) if cleaned.core else None
    key = hit.merchant.key if hit is not None else _key_of(cleaned.core)
    name = hit.merchant.name if hit is not None else display_name(key)
    return Identity(
        key=key, name=name, kind=cleaned.kind, via=cleaned.via, hit=hit, core=cleaned.core
    )


def categorise_rows(
    db: Session,
    household_id: uuid.UUID,
    rows: Sequence[Transaction],
    *,
    file_categorised: Collection[uuid.UUID] = (),
    protect: Mapping[uuid.UUID, Collection[str]] | None = None,
    user_id: uuid.UUID | None = None,
    request: HTTPConnection | None = None,
) -> Summary:
    """Key and categorise rows just created and flushed, in the caller's transaction."""
    del db, household_id, rows, file_categorised, protect, user_id, request
    return Summary()


def person_decided(
    db: Session,
    row: Transaction,
    *,
    before_category_id: uuid.UUID | None,
    before_source: str | None,
    at: datetime,
) -> None:
    """Record that a person set or cleared ``row``'s category (or its splits)."""
    del db, row, before_category_id, before_source, at


def reconsider(
    db: Session,
    household_id: uuid.UUID,
    *,
    transaction_ids: Collection[uuid.UUID] | None = None,
    rekey: bool = False,
    check_cancelled: Callable[[], None] | None = None,
) -> Summary:
    """Look again at the rows nobody decided (never at a person's, a file's or a rule's)."""
    del db, household_id, transaction_ids, rekey, check_cancelled
    return Summary()

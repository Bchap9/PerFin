# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who a transaction paid, what it probably is, and the one write path for guesses.

:func:`identify` turns a descriptor into an :class:`Identity` (merchant key,
display name, kind). :func:`categorise_rows` is called for rows just created,
:func:`person_decided` when a person set or cleared a category, and
:func:`reconsider` to look again at rows nobody decided. Every write here
goes through ``app.domain.categorisation.pinned.pinned_update`` and never
looks like a person's edit.

:func:`decide` is the order in which a guess is made for a row nobody
decided: the payee's default category, what the household taught, the
descriptor's kind, the dictionary's merchant, a telling word. It is pure: the
caller hands it what it read from the database. A guess at least
:data:`APPLY_MIN` sure is a fact (``category_id``); below that it is only a
suggestion (``suggested_category_id``).

The three write functions are still declarations and write nothing.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Final

from app.domain.categorisation import cleanup, dictionary, memory
from app.domain.categorisation.cleanup import DescriptorKind
from app.domain.categorisation.dictionary import Hit
from app.domain.categorisation.memory import Direction, Learned
from app.domain.transactions.categories import resolve as resolve_category
from app.domain.transactions.models import MERCHANT_KEY_MAX, Transaction

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from starlette.requests import HTTPConnection

#: A guess at least this sure is written as the category; below it, as a suggestion.
APPLY_MIN: Final = Decimal("0.80")
#: Tokens of the core that make the key of a merchant the dictionary does not know.
KEY_TOKENS: Final = 4

_PLACES: Final = Decimal("0.01")

PAYEE_CONFIDENCE: Final = Decimal("0.95")
#: Confidence of a learned answer by its decisions (3 stands for "3 or more"),
#: before it is multiplied by the winner's share.
LEARNED_BASE: Final[Mapping[int, Decimal]] = {
    1: Decimal("0.80"),
    2: Decimal("0.90"),
    3: Decimal("0.95"),
}
#: Below this share the household files the merchant in several places: the
#: memory gives no answer and nothing else may state a fact.
LEARNED_SHARE_MIN: Final = Decimal("0.60")
#: The most a learned answer is worth once its last decision is older than
#: ``memory.STALE_DAYS``.
STALE_MAX: Final = Decimal("0.70")
#: The most any guess is worth for a merchant the household is split on.
SPLIT_MAX: Final = Decimal("0.60")

HIT_CONFIDENCE: Final = Decimal("0.90")
HIT_ANYWHERE_CONFIDENCE: Final = Decimal("0.85")
#: Taken off a hit read behind a processor's prefix.
VIA_PENALTY: Final = Decimal("0.05")
#: A store that sells everything: only ever a suggestion.
MIXED_CONFIDENCE: Final = Decimal("0.60")
#: Money back from a merchant that takes money: its own category, as a refund.
MONEY_BACK_CONFIDENCE: Final = Decimal("0.80")
KEYWORD_CONFIDENCE: Final = Decimal("0.60")
KEYWORD_STRONG_CONFIDENCE: Final = Decimal("0.80")

#: Money that is not spending at a merchant, read from the descriptor's kind:
#: ``(kind, direction) -> (default category key, confidence)``. A pair that is
#: not here (a salary going out, a refund going out) is not guessed from the
#: kind. A refund is listed for the case that it names no known merchant; one
#: that does takes that merchant's category instead (:func:`decide`).
KIND_CATEGORIES: Final[Mapping[tuple[DescriptorKind, Direction], tuple[str, Decimal]]] = {
    ("card_payment", "out"): ("transfers.credit_card_payment", Decimal("0.85")),
    ("card_payment", "in"): ("transfers.credit_card_payment", Decimal("0.85")),
    ("transfer", "out"): ("transfers.between_accounts", Decimal("0.80")),
    ("transfer", "in"): ("transfers.between_accounts", Decimal("0.80")),
    ("loan_payment", "out"): ("transfers.loan_payment", Decimal("0.80")),
    ("loan_payment", "in"): ("transfers.loan_payment", Decimal("0.80")),
    ("cash", "out"): ("transfers.cash", Decimal("0.90")),
    ("cash", "in"): ("transfers.cash", Decimal("0.90")),
    ("fee", "out"): ("financial.bank_fees", Decimal("0.90")),
    ("fee", "in"): ("financial.bank_fees", Decimal("0.90")),
    ("interest", "in"): ("income.interest", Decimal("0.90")),
    ("interest", "out"): ("financial.interest_paid", Decimal("0.85")),
    ("payroll", "in"): ("income.salary", Decimal("0.85")),
    ("refund", "in"): ("income.refunds", Decimal("0.80")),
}
#: Kinds that say nothing about what the money was for: never guessed, whatever
#: the memory, the dictionary or a keyword would say.
NEVER_GUESSED: Final[frozenset[DescriptorKind]] = frozenset({"check", "deposit", "p2p"})


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


def _guess(category_id: uuid.UUID | None, source: str, confidence: Decimal) -> Decision:
    """A fact at :data:`APPLY_MIN` or above, a suggestion below; nothing without a category."""
    if category_id is None:
        return Decision()
    confidence = confidence.quantize(_PLACES, rounding=ROUND_HALF_UP)
    if confidence >= APPLY_MIN:
        return Decision(category_id=category_id, source=source, confidence=confidence)
    return Decision(suggested_category_id=category_id, source=source, confidence=confidence)


def _hit_confidence(identity: Identity, hit: Hit, direction: Direction) -> Decimal | None:
    """How sure a dictionary hit is, or ``None`` when the merchant does not answer."""
    merchant = hit.merchant
    if merchant.inflow and direction == "out":
        return None
    confidence = HIT_ANYWHERE_CONFIDENCE if hit.how == "anywhere" else HIT_CONFIDENCE
    if identity.via is not None:
        confidence -= VIA_PENALTY
    if direction == "in" and not merchant.inflow:
        confidence = MONEY_BACK_CONFIDENCE
    if merchant.mixed:
        confidence = MIXED_CONFIDENCE
    return confidence


def _from_data(identity: Identity, direction: Direction) -> tuple[str, str, Decimal] | None:
    """The kind's, the merchant's or a keyword's answer: ``(source, key, confidence)``.

    The first that applies is the answer, also when it turns out to be none:
    a merchant that does not answer is not handed on to a keyword.
    """
    by_kind = KIND_CATEGORIES.get((identity.kind, direction))
    hit = identity.hit
    if by_kind is not None and not (identity.kind == "refund" and hit is not None):
        return ("dictionary", *by_kind)
    if hit is not None:
        confidence = _hit_confidence(identity, hit, direction)
        return None if confidence is None else ("dictionary", hit.merchant.category, confidence)
    word = dictionary.keyword(identity.core, identity.via) if identity.core else None
    if word is not None:
        confidence = KEYWORD_STRONG_CONFIDENCE if word.strong else KEYWORD_CONFIDENCE
        return ("keyword", word.category, confidence)
    return None


def decide(
    identity: Identity,
    direction: Direction,
    *,
    learned: Learned | None,
    payee_default: uuid.UUID | None,
    categories: Mapping[str, uuid.UUID],
    now: datetime,
) -> Decision:
    """The guess for a row nobody decided; the first of these that answers:

    1. the default category of the row's payee (``payee``, 0.95);
    2. a kind in :data:`NEVER_GUESSED`: nothing;
    3. what the household taught for this merchant and direction, when one
       category has at least :data:`LEARNED_SHARE_MIN` of the decisions
       (``learned``; :data:`LEARNED_BASE` times the share, at most
       :data:`STALE_MAX` when the last decision is older than
       ``memory.STALE_DAYS``);
    4. the descriptor's kind (:data:`KIND_CATEGORIES`, ``dictionary``);
    5. the dictionary's merchant (``dictionary``);
    6. a telling word (``keyword``).

    When the household is split on the merchant (a share below the minimum),
    steps 4 to 6 give a suggestion at most. ``categories`` is the household's
    ``system_key -> id`` map; a key it has neither itself nor by its group
    answers nothing. ``learned`` is the memory's answer for ``identity.key``
    and ``direction``, or ``None``.
    """
    if payee_default is not None:
        return _guess(payee_default, "payee", PAYEE_CONFIDENCE)
    if identity.kind in NEVER_GUESSED:
        return Decision()
    split = False
    if learned is not None:
        if learned.share >= LEARNED_SHARE_MIN:
            confidence = LEARNED_BASE[max(1, min(learned.decisions, 3))] * learned.share
            if now - learned.last_decided_at > timedelta(days=memory.STALE_DAYS):
                confidence = min(confidence, STALE_MAX)
            return _guess(learned.category_id, "learned", confidence)
        split = True
    answer = _from_data(identity, direction)
    if answer is None:
        return Decision()
    source, key, confidence = answer
    if split:
        confidence = min(confidence, SPLIT_MAX)
    return _guess(resolve_category(categories, key), source, confidence)


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

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

What the three write functions leave alone: a row is **undecided** when it
has no category, no source and no split (:func:`undecided_clause`), and only
such a row is ever guessed for. :func:`reconsider` also looks again at its
own earlier guesses (:func:`reconsidered_clause`): rows that carry a
suggestion and facts whose source is ``dictionary`` or ``keyword``. A row
whose source is ``person``, ``file``, ``rule``, ``payee``, ``learned`` or
``legacy``, a row a person cleared, and a split row are never read for a
guess. Audit rows and events carry counts and account ids only.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.core.audit import audit
from app.core.clock import utcnow
from app.core.events import emit
from app.domain.categorisation import cleanup, dictionary, memory
from app.domain.categorisation.cleanup import DescriptorKind
from app.domain.categorisation.dictionary import Hit
from app.domain.categorisation.memory import Direction, Learned
from app.domain.categorisation.models import MerchantMemory
from app.domain.categorisation.pinned import pinned_update
from app.domain.recurring.detection import BILL_KEYS
from app.domain.transactions.categories import category_ids_by_key
from app.domain.transactions.categories import resolve as resolve_category
from app.domain.transactions.models import MERCHANT_KEY_MAX, Transaction, TransactionSplit

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

#: A guess at least this sure is written as the category; below it, as a suggestion.
APPLY_MIN: Final = Decimal("0.80")
#: Tokens of the core that make the key of a merchant the dictionary does not know.
KEY_TOKENS: Final = 4
#: Letters and digits such a key needs at least. What is left of "E" or "XY
#: #0042" is no name: as a key it would make one merchant of every such line,
#: for what is learned, for a rule and for a series. A brand the dictionary
#: knows by fewer ("BP") keeps the dictionary's key.
KEY_CHARS_MIN: Final = 3
#: Words that say only that cash went in or out. A core of these alone (and
#: numbers), with at least one of :data:`CASH_CORE_WORDS`, or one that starts
#: with ``atm``, names nobody: "WITHDRAWAL", "CASH DEPOSIT", "TELLER DEPOSIT",
#: "ATM 0412 <town>". Like the cash lines clean-up reads by their kind, it has
#: no merchant key; the keywords still read its core.
CASH_WORDS: Final = frozenset(
    {
        "atm",
        "cash",
        "deposit",
        "deposits",
        "withdrawal",
        "withdrawals",
        "withdraw",
        "withdrwl",
        "teller",
        "counter",
        "branch",
        "cheque",
    }
)
CASH_CORE_WORDS: Final = frozenset(
    {"atm", "cash", "deposit", "deposits", "withdrawal", "withdrawals", "withdraw", "withdrwl"}
)
#: The words some banks wrap around a teller's cash ("WITHDRAWAL MADE IN THE
#: STORE", "CUSTOMER WITHDRAWAL IMAGE", "OVER THE COUNTER WITHDRAWAL").
#: They name nobody only beside a word for money taken out or paid in
#: (:data:`CASH_MOVE_WORDS`): around CASH alone they may be a shop ("CASH IN A BRANCH").
CASH_FILLER_WORDS: Final = frozenset(
    {"made", "in", "a", "the", "store", "customer", "image", "over"}
)
CASH_MOVE_WORDS: Final = frozenset(
    {"deposit", "deposits", "withdrawal", "withdrawals", "withdraw", "withdrwl"}
)

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
#: Kinds whose money coming in from a merchant is money back: the line says
#: it is a refund, or it is a card purchase with the sign turned round.
MONEY_BACK_KINDS: Final[frozenset[DescriptorKind]] = frozenset(
    {"refund", "purchase", "recurring_purchase"}
)
#: The most a merchant's or a keyword's category is worth for money coming in
#: on any other kind of line (a bank credit, a line of no known kind): it may
#: be a refund, or earnings and payouts that are no spending at all, so it is
#: only ever a suggestion. A merchant marked ``inflow`` is not held to it.
MONEY_IN_MAX: Final = Decimal("0.70")
KEYWORD_CONFIDENCE: Final = Decimal("0.60")
KEYWORD_STRONG_CONFIDENCE: Final = Decimal("0.80")
#: Groups of categories a keyword answers for money coming in only: a word for
#: pay ("wages") on money going out is pay for somebody else.
INFLOW_ONLY_GROUPS: Final = frozenset({"income"})
#: Keywords that say which way the money went, and answer only that way: no
#: money comes in by a withdrawal, none goes out by a deposit.
KEYWORD_DIRECTIONS: Final[Mapping[str, Direction]] = {
    "withdrawal": "out",
    "cash deposit": "in",
}
#: Categories a keyword in the entry description of a labelled ACH line
#: answers: a bill's (``DES:RENT``, ``DES:TUITION``), the recurring payments
#: detection calls bills. Any other word there answers nothing: a company is on
#: the other end, so ``DES:WITHDRAWAL`` is no cash at the counter, and what it
#: says it charged for (``DES:PHARMACY``, ``DES:GYM``) is its own wording.
DESCRIBED: Final = BILL_KEYS

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
#: The most a wire is trusted as a transfer between the household's own
#: accounts: it may as well pay a person or a company, so it is a suggestion.
WIRE_MAX: Final = Decimal("0.70")
#: Processors that are who was paid: an order through a delivery or ticket
#: platform is the platform's (its ``via`` is the key of its dictionary
#: entry), whatever restaurant, shop or event the line names behind it. The
#: platform's own membership, a dictionary entry with a ``series``, stays
#: what it is.
PLATFORMS: Final = frozenset({"doordash", "grubhub", "instacart", "eventbrite"})
#: Letters the first of two words in front of a state needs to be a name the
#: shop may also go by (:func:`_also`).
ALSO_MIN: Final = 5
#: First words many shops share, which are therefore nobody's name alone:
#: articles, titles, saints and mounts, sizes, ages, colours, directions and
#: what a street, a town or a trade is called.
NOT_A_NAME: Final = frozenset(
    {
        *("the", "los", "las", "les", "casa", "villa", "saint", "santa", "mount", "point"),
        *("north", "south", "east", "west", "upper", "lower", "central", "midtown"),
        *("little", "great", "grand", "royal", "super", "happy", "lucky", "fresh"),
        *("first", "second", "third", "prime", "premier", "family", "american", "national"),
        *("black", "white", "green", "brown", "golden", "silver", "purple", "orange"),
        *("yellow", "crimson", "scarlet"),
        *("main", "street", "market", "corner", "village", "valley", "river", "harbor"),
        *("harbour", "island", "mountain", "forest", "garden", "sunset", "sunrise", "downtown"),
        *("uptown", "county", "country", "city", "town", "state", "union", "liberty"),
        *("classic", "modern", "urban", "rustic", "simply", "quick", "express", "discount"),
        *("house", "home", "shop", "store", "studio", "world", "planet", "global"),
        *("doctor", "mister", "uncle", "auntie", "mama", "papa", "sister", "brother"),
    }
)
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
    #: A shorter name the same merchant may be filed under in the household's
    #: memory (:func:`learned_for`); never a key of its own.
    also: str | None = None
    #: What clean-up read on the way (``Cleaned.signals``).
    signals: frozenset[str] = frozenset()


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
    #: Rows given a category, by how it was decided.
    by_source: Counter[str] = field(default_factory=Counter)
    #: Rows given a suggestion only.
    suggested: int = 0
    #: Rows whose merchant key was written.
    keyed: int = 0
    #: Rows whose categorisation was written (a category, a suggestion, or a
    #: guess taken back); a key alone does not count.
    changed: int = 0
    #: Accounts of the rows counted in ``changed``.
    changed_accounts: set[uuid.UUID] = field(default_factory=set)
    #: Learned entries (memory rows) carried over to a merchant's new key.
    memory_moved: int = 0
    #: After a pass that computed every key again: for each merchant key a row
    #: carried, the keys those rows carry now (``None``: a row that names
    #: nobody any more). Read with :func:`key_now`; never stored or audited.
    renamed: dict[str, frozenset[str | None]] = field(default_factory=dict)
    #: The same, for the rows of one account and direction only:
    #: ``(account, direction, old key) -> keys now``. What lives on one account
    #: (a recurring series) follows its own rows, not a stray row of the same
    #: key elsewhere (:func:`keys_at`).
    renamed_at: dict[tuple[uuid.UUID, Direction, str], frozenset[str | None]] = field(
        default_factory=dict
    )


def cash_wording(core: str) -> bool:
    """Whether ``core`` says only that cash went in or out (:data:`CASH_WORDS`).

    Also with :data:`CASH_FILLER_WORDS`, when a word of :data:`CASH_MOVE_WORDS`
    is among them.
    """
    words = [token for token in core.split() if not token.isdigit()]
    if not words:
        return False
    if words[0] == "atm":
        return True
    filler = any(w in CASH_FILLER_WORDS for w in words)
    return all(w in CASH_WORDS or w in CASH_FILLER_WORDS for w in words) and any(
        w in (CASH_MOVE_WORDS if filler else CASH_CORE_WORDS) for w in words
    )


def _key_of(core: str) -> str | None:
    """The first :data:`KEY_TOKENS` tokens of ``core`` that fit a merchant key.

    ``None`` when they hold fewer than :data:`KEY_CHARS_MIN` letters and digits,
    and for bare cash wording (:func:`cash_wording`).
    """
    if cash_wording(core):
        return None
    tokens = core.split()[:KEY_TOKENS]
    while tokens and len(" ".join(tokens)) > MERCHANT_KEY_MAX:
        tokens.pop()
    # One token longer than a key: its start still names the merchant.
    key = " ".join(tokens) if tokens else core[:MERCHANT_KEY_MAX].strip()
    if sum(ch.isalnum() for ch in key) < KEY_CHARS_MIN:
        return None
    return key


def display_name(key: str | None) -> str | None:
    """The name to show for a merchant key: the dictionary's, else the key in title case."""
    if not key:
        return None
    return dictionary.name_for(key) or key.title()


def _platform(cleaned: cleanup.Cleaned, hit: Hit | None) -> Hit | None:
    """The platform an order came through, when that is who was paid (:data:`PLATFORMS`).

    Behind the platform's prefix every line is the platform's, except its own
    membership. Without the prefix, a line nobody else claims that begins
    with the platform's name is ("GRUBHUB HARBOR PIZZA", "PAYPAL *DOORDASH").
    """
    name: str | None = None
    if cleaned.via in PLATFORMS:
        if hit is not None and hit.merchant.series is not None:
            return hit
        name = cleaned.via
    elif hit is None:
        first = cleaned.core.partition(" ")[0]
        name = first if first in PLATFORMS else None
    merchant = dictionary.merchants().get(name) if name is not None else None
    return hit if merchant is None else Hit(merchant=merchant, how="exact")


def _also(cleaned: cleanup.Cleaned) -> str | None:
    """The shorter name an unknown merchant may also go by (``Identity.also``), or ``None``.

    Clean-up leaves "JUNEBUG ASHGROVE OH" open (``Cleaned.shorter``): a shop
    of one word in Ashgrove, or the shop "Junebug Ashgrove". The key keeps
    both words, because shops that share a first word ("THE HIDEOUT TX" and
    "THE PANTRY TX") must never become one merchant. The first word alone is
    only a second name to look under in what the household taught
    (:func:`learned_for`), and not even that when it is no name of its own:
    a word in :data:`NOT_A_NAME`, one shorter than :data:`ALSO_MIN` letters,
    a word that says what is sold (as does a keyword in the last word, which
    is then no town), or the name of a merchant the dictionary knows.
    """
    shorter = cleaned.shorter
    if not shorter or " " in shorter or len(shorter) < ALSO_MIN or shorter in NOT_A_NAME:
        return None
    if dictionary.lookup(shorter) is not None or dictionary.keyword(cleaned.core, None) is not None:
        return None
    return _key_of(shorter)


def identify(raw: str | None) -> Identity:
    """Who ``raw`` names: the dictionary's merchant, else the start of the core."""
    cleaned = cleanup.clean(raw)
    hit = _platform(cleaned, dictionary.lookup(cleaned.core) if cleaned.core else None)
    key = hit.merchant.key if hit is not None else _key_of(cleaned.core)
    name = hit.merchant.name if hit is not None else display_name(key)
    also = _also(cleaned) if hit is None else None
    return Identity(
        key=key,
        name=name,
        kind=cleaned.kind,
        via=cleaned.via,
        hit=hit,
        core=cleaned.core,
        also=also if also != key else None,
        signals=cleaned.signals,
    )


def learned_for(
    identity: Identity, direction: Direction, learned: Mapping[tuple[str, Direction], Learned]
) -> Learned | None:
    """What the household taught for the merchant of ``identity`` and ``direction``.

    Under its key, else under the shorter name it may also go by
    (``Identity.also``): a household that filed "SQ *JUNEBUG" has said what
    "JUNEBUG ASHGROVE OH" is. Never the other way round: what was filed
    under two words says nothing about a shop called by the first alone.
    """
    if identity.key is None:
        return None
    own = learned.get((identity.key, direction))
    if own is not None or identity.also is None:
        return own
    return learned.get((identity.also, direction))


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
        confidence = MONEY_BACK_CONFIDENCE if identity.kind in MONEY_BACK_KINDS else MONEY_IN_MAX
    if merchant.mixed:
        confidence = MIXED_CONFIDENCE
    return confidence


def _described(identity: Identity) -> dictionary.KeywordHit | None:
    """A keyword in the entry description of a labelled ACH line (``DES:RENT``), or ``None``.

    Read only when the company's name holds none: "<name> DES:TUITION" is
    tuition at a business no list knows. Only a bill's word answers there
    (:data:`DESCRIBED`), or pay's word (an ``income`` category, which the
    caller allows for money in only): a company that pulls money by ACH
    ("<name> DES:WITHDRAWAL") was paid, it handed out no cash.
    """
    for signal in sorted(identity.signals):
        prefix, _, described = signal.partition(":")
        if prefix == "descr" and described:
            hit = dictionary.keyword(described, None)
            if hit is None:
                return None
            group = hit.category.partition(".")[0]
            return hit if _is_bill(hit.category) or group in INFLOW_ONLY_GROUPS else None
    return None


def _is_bill(key: str) -> bool:
    """Whether ``key`` is under one of :data:`DESCRIBED` (``housing`` is under ``housing.``)."""
    return key.startswith(DESCRIBED) or f"{key}." in DESCRIBED


def _from_data(identity: Identity, direction: Direction) -> tuple[str, str, Decimal] | None:
    """The kind's, the merchant's or a keyword's answer: ``(source, key, confidence)``.

    The first that applies is the answer, also when it turns out to be none:
    a merchant that does not answer is not handed on to a keyword.
    """
    by_kind = KIND_CATEGORIES.get((identity.kind, direction))
    hit = identity.hit
    if by_kind is not None and not (identity.kind == "refund" and hit is not None):
        key, sure = by_kind
        if identity.kind == "transfer" and cleanup.WIRE_SIGNAL in identity.signals:
            sure = min(sure, WIRE_MAX)
        return ("dictionary", key, sure)
    if hit is not None:
        confidence = _hit_confidence(identity, hit, direction)
        return None if confidence is None else ("dictionary", hit.merchant.category, confidence)
    word = dictionary.keyword(identity.core, identity.via) if identity.core else None
    if word is None:
        word = _described(identity)
    if word is not None and (
        (direction == "out" and word.category.partition(".")[0] in INFLOW_ONLY_GROUPS)
        or KEYWORD_DIRECTIONS.get(word.word, direction) != direction
    ):
        return None
    if word is not None:
        confidence = KEYWORD_STRONG_CONFIDENCE if word.strong else KEYWORD_CONFIDENCE
        if direction == "in" and identity.kind not in MONEY_BACK_KINDS:
            confidence = min(confidence, MONEY_IN_MAX)
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
    6. a telling word (``keyword``), in the core or else, for a bill only
       (:data:`DESCRIBED`), in a labelled ACH line's entry description.

    When the household is split on the merchant (a share below the minimum),
    steps 4 to 6 give a suggestion at most. So do steps 5 and 6 for money
    coming in on a line that is neither a refund nor a card purchase
    (:data:`MONEY_BACK_KINDS`, :data:`MONEY_IN_MAX`), unless the merchant is
    one that pays (``inflow``): earnings and payouts are not stored as
    spending taken back. ``categories`` is the household's
    ``system_key -> id`` map; a key it has neither itself nor by its group
    answers nothing. ``learned`` is the memory's answer for the merchant and
    ``direction`` (:func:`learned_for`), or ``None``.
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


# ---------------------------------------------------------------------------
# The write path
# ---------------------------------------------------------------------------

FILE_SOURCE: Final = "file"
RULE_SOURCE: Final = "rule"
PERSON_SOURCE: Final = "person"
FILE_CONFIDENCE: Final = Decimal("0.95")
CERTAIN: Final = Decimal(1)
#: Sources of a fact the engine may look at again (a suggestion always is).
GUESS_AGAIN_SOURCES: Final = ("dictionary", "keyword")
#: Rows read (and locked) per batch by :func:`reconsider`.
BATCH: Final = 500

_DECISION_COLUMNS: Final = (
    "category_id",
    "suggested_category_id",
    "category_source",
    "category_confidence",
)


def _no_split() -> ColumnElement[bool]:
    return ~(
        select(TransactionSplit.id)
        .where(
            TransactionSplit.household_id == Transaction.household_id,
            TransactionSplit.transaction_id == Transaction.id,
        )
        .exists()
    )


def undecided_clause() -> ColumnElement[bool]:
    """Rows nobody decided: no category, no source and no split.

    A row with only a suggestion has a source and is not among them.
    """
    return and_(
        Transaction.category_id.is_(None), Transaction.category_source.is_(None), _no_split()
    )


def reconsidered_clause() -> ColumnElement[bool]:
    """Rows :func:`reconsider` evaluates: undecided ones and the engine's own guesses.

    That is a row that carries a suggestion, or a fact whose source is
    ``dictionary`` or ``keyword``. Never a split row.
    """
    return and_(
        _no_split(),
        or_(
            and_(Transaction.category_id.is_(None), Transaction.category_source.is_(None)),
            Transaction.suggested_category_id.is_not(None),
            Transaction.category_source.in_(GUESS_AGAIN_SOURCES),
        ),
    )


@dataclass(frozen=True)
class _Open:
    """One row to guess for."""

    id: uuid.UUID
    account_id: uuid.UUID
    identity: Identity
    direction: Direction
    payee_id: uuid.UUID | None


@dataclass(frozen=True)
class _Known:
    """What one pass reads once: the household's keyed categories and payee defaults."""

    categories: Mapping[str, uuid.UUID]
    payee_defaults: Mapping[uuid.UUID, uuid.UUID]
    now: datetime


def _known(db: Session, household_id: uuid.UUID) -> _Known:
    # Imported here: the importer's package is among this module's callers.
    from app.domain.imports.repo import payee_default_categories

    return _Known(
        categories=category_ids_by_key(db, household_id),
        payee_defaults=payee_default_categories(db, household_id),
        now=utcnow(),
    )


def _decisions(
    db: Session, household_id: uuid.UUID, known: _Known, rows: Sequence[_Open]
) -> dict[uuid.UUID, Decision]:
    """:func:`decide` for each of ``rows``, with the memory read once for all of them."""
    keys = {
        (key, row.direction)
        for row in rows
        if row.identity.key
        for key in (row.identity.key, row.identity.also)
        if key
    }
    learned = memory.lookup(db, household_id, keys) if keys else {}
    found: dict[uuid.UUID, Decision] = {}
    for row in rows:
        found[row.id] = decide(
            row.identity,
            row.direction,
            learned=learned_for(row.identity, row.direction, learned),
            payee_default=known.payee_defaults.get(row.payee_id) if row.payee_id else None,
            categories=known.categories,
            now=known.now,
        )
    return found


def _decision_values(decision: Decision) -> dict[str, Any]:
    return {
        "category_id": decision.category_id,
        "suggested_category_id": decision.suggested_category_id,
        "category_source": decision.source,
        "category_confidence": decision.confidence,
    }


def _mark(summary: Summary, account_id: uuid.UUID, source: str | None) -> None:
    """Count one row whose categorisation was written (``source``: it has a category now)."""
    if source is not None:
        summary.by_source[source] += 1
    summary.changed += 1
    summary.changed_accounts.add(account_id)


def _count(summary: Summary, account_id: uuid.UUID, decision: Decision) -> None:
    """Count one written guess: a fact, a suggestion, or one taken back."""
    if decision.category_id is None and decision.suggested_category_id is not None:
        summary.suggested += 1
    _mark(summary, account_id, decision.source if decision.category_id is not None else None)


def _write(
    db: Session,
    household_id: uuid.UUID,
    changes: Mapping[uuid.UUID, Mapping[str, Any]],
    rows: Mapping[uuid.UUID, Transaction] | None = None,
) -> None:
    """The pinned write; ``rows`` (the caller's ORM rows) then show what was written.

    The values are set as loaded state, not as changes: the rows are not
    dirty afterwards and reading them costs no query per row.
    """
    if not changes:
        return
    pinned_update(db, household_id, changes)
    for row_id, values in changes.items():
        row = (rows or {}).get(row_id)
        if row is not None:
            for column, value in values.items():
                set_committed_value(row, column, value)


def _announce(db: Session, household_id: uuid.UUID, summary: Summary) -> None:
    """``transaction.categorised`` for live clients, when a categorisation was written."""
    if not summary.changed:
        return
    emit(
        db,
        household_id,
        "transaction.categorised",
        {
            # Members and viewers hear only about accounts they see.
            "account_ids": sorted(str(account) for account in summary.changed_accounts),
            "count": summary.changed,
        },
    )


def _split_ids(db: Session, household_id: uuid.UUID, ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """Those of ``ids`` that are split."""
    found: set[uuid.UUID] = set()
    for start in range(0, len(ids), BATCH):
        found.update(
            db.execute(
                select(TransactionSplit.transaction_id)
                .where(
                    TransactionSplit.household_id == household_id,
                    TransactionSplit.transaction_id.in_(ids[start : start + BATCH]),
                )
                .distinct()
            ).scalars()
        )
    return found


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
    """Key and categorise rows just created and flushed, in the caller's transaction.

    In this order:

    1. every row gets its ``merchant_key`` (before the rules run: a rule may
       name a merchant);
    2. a row in ``file_categorised`` (the file gave it a category or split
       it) is marked ``file``;
    3. the household's rules run as the creation hook always ran them
       (``app.domain.rules.service.apply_rules_to``): once per set of
       protected fields, ``categorization`` protected for the file's rows and
       ``protect[row.id]`` for what a person typed on a manual row. A row
       that had neither a category nor a split and has one afterwards is the
       rule's;
    4. what is still undecided is guessed for (:func:`decide`).

    A row that arrives with a category the file did not give (a person typed
    it) is keyed and otherwise left as it is: recording that choice is
    :func:`person_decided`'s. Everything this function writes itself is a
    pinned write, so ``updated_at`` stays where it was; a rule's write moves
    it, as it always did, and the importer equalises its rows afterwards.
    One ``transaction.categorise`` audit row and one
    ``transaction.categorised`` event say how many rows were categorised,
    when any was. ``ValueError`` when a row is not the household's.
    """
    rows = list(rows)
    if any(row.household_id != household_id for row in rows):
        raise ValueError("every transaction must belong to household_id")
    summary = Summary(scanned=len(rows))
    if not rows:
        return summary
    # Imported here: ``app.domain.rules.service`` imports the transactions
    # service, which calls this module.
    from app.domain.rules.service import apply_rules_to

    db.flush()
    by_id = {row.id: row for row in rows}
    ids = list(by_id)
    filed = set(file_categorised)
    identities = {row.id: identify(row.raw_payee) for row in rows}
    split_before = _split_ids(db, household_id, ids)
    source_before = {row.id: row.category_source for row in rows}
    #: Rows that could still be decided by a rule or a guess.
    blank = {
        row.id
        for row in rows
        if row.category_id is None and row.category_source is None and row.id not in split_before
    }

    first: dict[uuid.UUID, dict[str, Any]] = {}
    for row in rows:
        values: dict[str, Any] = {}
        key = identities[row.id].key
        if row.merchant_key != key:
            values["merchant_key"] = key
            summary.keyed += 1
        if row.id in filed and row.category_source is None:
            values["category_source"] = FILE_SOURCE
            values["category_confidence"] = FILE_CONFIDENCE
            # Also when the file split it: the lines carry the categories.
            _mark(summary, row.account_id, FILE_SOURCE)
        if values:
            first[row.id] = values
    _write(db, household_id, first, by_id)

    groups: dict[frozenset[str], list[Transaction]] = {}
    for row in rows:
        guarded = set((protect or {}).get(row.id, ()))
        if row.id in filed:
            guarded.add("categorization")
        groups.setdefault(frozenset(guarded), []).append(row)
    for guarded_fields, group in sorted(groups.items(), key=lambda item: sorted(item[0])):
        apply_rules_to(
            db,
            household_id,
            group,
            protect=sorted(guarded_fields),
            user_id=user_id,
            request=request,
        )
    db.flush()

    split_after = _split_ids(db, household_id, sorted(blank))
    second: dict[uuid.UUID, dict[str, Any]] = {}
    still_open: list[_Open] = []
    for row in rows:
        source = row.category_source
        if source == RULE_SOURCE:
            if source_before[row.id] != RULE_SOURCE:
                _mark(summary, row.account_id, RULE_SOURCE)
        elif source is None and row.id in blank:
            if row.category_id is not None or row.id in split_after:
                second[row.id] = {
                    "category_source": RULE_SOURCE,
                    "category_confidence": CERTAIN,
                }
                _mark(summary, row.account_id, RULE_SOURCE)
            else:
                still_open.append(
                    _Open(
                        id=row.id,
                        account_id=row.account_id,
                        identity=identities[row.id],
                        direction=memory.direction_of(row.amount),
                        payee_id=row.payee_id,
                    )
                )
    if still_open:
        decided = _decisions(db, household_id, _known(db, household_id), still_open)
        for item in still_open:
            decision = decided[item.id]
            if decision.source is not None:
                second[item.id] = _decision_values(decision)
                _count(summary, item.account_id, decision)
    _write(db, household_id, second, by_id)

    if summary.changed:
        audit(
            db,
            action="transaction.categorise",
            target_type="transaction",
            after={
                "count": summary.changed,
                "by_source": dict(sorted(summary.by_source.items())),
                "suggested": summary.suggested,
            },
            household_id=household_id,
            user_id=user_id,
            request=request,
        )
    _announce(db, household_id, summary)
    return summary


def person_decided(
    db: Session,
    row: Transaction,
    *,
    before_category_id: uuid.UUID | None,
    before_source: str | None,
    at: datetime,
) -> None:
    """Record that a person set or cleared ``row``'s category (or its splits).

    Called by the transactions service after it assigned ``row.category_id``
    (``None`` when the person cleared it or split the row), with what the row
    held before. The row becomes the person's: source ``person`` at full
    confidence, no rule and no suggestion. This is the person's own write,
    made on the ORM row; ``updated_at`` moves in the caller.

    The household's memory learns from it when the row has a merchant key
    and its descriptor is of a teachable kind: the category this row counted
    for before is taken back when that was a person's choice too (the memory
    counts rows, not clicks), and the new one, if any, is counted.
    """
    row.category_source = PERSON_SOURCE
    row.category_confidence = CERTAIN
    row.category_rule_id = None
    row.suggested_category_id = None
    key = row.merchant_key
    if not key or cleanup.clean(row.raw_payee).kind not in cleanup.TEACHABLE:
        return
    direction = memory.direction_of(row.amount)
    if before_source == PERSON_SOURCE and before_category_id is not None:
        memory.withdraw(db, row.household_id, key, direction, before_category_id)
    if row.category_id is not None:
        memory.record(db, row.household_id, key, direction, row.category_id, at=at)


#: A person's row whose key moved: ``(old key, direction, category, new key)``.
type _Taught = tuple[str, str, uuid.UUID, str]


def key_now(old: str, renamed: Mapping[str, Collection[str | None]]) -> str | None:
    """The one key the merchant ``old`` has after a re-key, or ``None``.

    For whatever names a merchant by its key across the household (a
    merchant rule's condition; a recurring series lives on one account and
    follows its own rows instead, :func:`keys_at`). ``renamed`` is
    :attr:`Summary.renamed`. Answers the new key only when rows carried
    ``old`` and every one of them carries that one key now. ``None`` when the
    key did not move, when its rows went to several keys (nobody can say
    which was meant), when they name nobody any more, and when no row
    carried ``old`` at all: unlike a learned entry (:func:`_move_memory`), a
    rule is never pointed at another merchant on the dictionary's reading of
    its key alone, since a prefix match would make it decide more rows than
    it was made for.
    """
    now = renamed.get(old)
    if now is None or len(now) != 1:
        return None
    [only] = now
    return only if only != old else None


def keys_at(
    account_id: uuid.UUID,
    direction: Direction,
    old: str,
    renamed_at: Mapping[tuple[uuid.UUID, Direction, str], Collection[str | None]],
) -> frozenset[str]:
    """The keys the rows of ``account_id`` and ``direction`` that carried ``old`` carry now.

    For a recurring series (``Summary.renamed_at``): rows that name nobody
    any more are left out, since no key can follow them (a series whose rows
    all name nobody loses its key: ``service._follow_keys``). Empty when no
    such row carried ``old`` or none names anybody now; ``{old}`` alone when
    the key did not move.
    """
    return frozenset(
        key for key in renamed_at.get((account_id, direction, old), ()) if key is not None
    )


def _move_memory(
    db: Session,
    household_id: uuid.UUID,
    renamed: Mapping[str, Collection[str | None]],
    taught: Mapping[_Taught, int],
) -> int:
    """Carry what the household taught over to the keys its merchants have now.

    ``renamed`` says, for every merchant key a row of the household carried,
    the keys those rows have after the pass (``None``: a row that names
    nobody any more); ``taught`` counts the person-decided rows that moved.
    For each learned entry (one memory row: key, direction, category):

    * every row of its key went to one new key: the entry goes there whole
      (also the decisions of rows that were deleted since);
    * the rows of its key now carry several keys (some may have kept it):
      only the decisions of the person's rows that moved go with them, and
      never more than the entry holds (what was forgotten stays forgotten);
    * no row carries its key any more: it goes to the merchant the
      dictionary now reads that key as, if it names one.

    An entry that arrives where the household already has one for the same
    direction and category is added to it; the later ``last_decided_at``
    stays. Otherwise the memory is left as it is. Returns the entries moved,
    in whole or in part.
    """
    held = db.execute(
        select(
            MerchantMemory.id,
            MerchantMemory.merchant_key,
            MerchantMemory.direction,
            MerchantMemory.category_id,
            MerchantMemory.decisions,
            MerchantMemory.last_decided_at,
        )
        .where(MerchantMemory.household_id == household_id)
        .order_by(MerchantMemory.merchant_key, MerchantMemory.id)
        .with_for_update()
    ).all()
    #: ``(memory row, new key, decisions)``, all read before anything is
    #: written: one key may move to a key that itself moves on.
    moves: list[tuple[Any, str, int]] = []
    for row in held:
        old = row.merchant_key
        now = renamed.get(old)
        if now is None:
            hit = dictionary.lookup(old)
            if hit is not None and hit.merchant.key != old:
                moves.append((row, hit.merchant.key, row.decisions))
        elif len(now) == 1:
            [only] = now
            if only is not None and only != old:
                moves.append((row, only, row.decisions))
        else:
            left = row.decisions
            for new in sorted(key for key in now if key is not None and key != old):
                count = min(taught.get((old, row.direction, row.category_id, new), 0), left)
                if count:
                    moves.append((row, new, count))
                    left -= count
    if not moves:
        return 0
    taken: Counter[uuid.UUID] = Counter()
    for row, _, count in moves:
        taken[row.id] += count
    at = utcnow()
    emptied = sorted(row.id for row in held if taken[row.id] == row.decisions)
    for start in range(0, len(emptied), BATCH):
        db.execute(
            delete(MerchantMemory).where(
                MerchantMemory.household_id == household_id,
                MerchantMemory.id.in_(emptied[start : start + BATCH]),
            )
        )
    for row in held:
        if 0 < taken[row.id] < row.decisions:
            db.execute(
                update(MerchantMemory)
                .where(MerchantMemory.household_id == household_id, MerchantMemory.id == row.id)
                .values(decisions=row.decisions - taken[row.id], updated_at=at)
            )
    for row, new, count in moves:
        statement = insert(MerchantMemory).values(
            household_id=household_id,
            merchant_key=new,
            direction=row.direction,
            category_id=row.category_id,
            decisions=count,
            last_decided_at=row.last_decided_at,
        )
        db.execute(
            statement.on_conflict_do_update(
                constraint="uq_merchant_memory_choice",
                set_={
                    "decisions": MerchantMemory.decisions + statement.excluded.decisions,
                    "last_decided_at": func.greatest(
                        MerchantMemory.last_decided_at, statement.excluded.last_decided_at
                    ),
                    "updated_at": at,
                },
            )
        )
    return len(taken)


def reconsider(
    db: Session,
    household_id: uuid.UUID,
    *,
    transaction_ids: Collection[uuid.UUID] | None = None,
    rekey: bool = False,
    check_cancelled: Callable[[], None] | None = None,
) -> Summary:
    """Look again at the rows nobody decided (never at a person's, a file's or a rule's).

    Over the household's rows, or those of ``transaction_ids``, newest first
    in locked batches of :data:`BATCH`: the rows of
    :func:`reconsidered_clause` are decided again (:func:`decide`; the rules
    are not run). A row whose decision is unchanged is not written; a former
    fact that is now only a suggestion, or nothing, loses its category. With
    ``rekey`` the merchant key of **every** row of the scope is recomputed
    first, decided rows included: their key may change, their category,
    source and confidence never do. Without it only a missing key of a row
    being decided is filled in.

    What the household taught goes with the keys: when ``rekey`` covers the
    whole household (no ``transaction_ids``), the memory is carried over to
    the new keys in the same transaction (:func:`_move_memory`,
    ``Summary.memory_moved``). Rows decided earlier in the same pass did not
    see it yet under the new key: the caller looks again when anything
    moved.

    Pinned writes only: no ``updated_at`` moves. ``check_cancelled`` is
    called before each batch and stops the pass by raising. One
    ``transaction.categorised`` event when a categorisation changed; the
    audit row is the caller's (a refresh, a revert).
    """
    summary = Summary()
    clauses: list[ColumnElement[bool]] = [Transaction.household_id == household_id]
    if transaction_ids is not None:
        wanted = sorted(set(transaction_ids))
        if not wanted:
            return summary
        clauses.append(Transaction.id.in_(wanted))
    if not rekey:
        clauses.append(reconsidered_clause())
    known = _known(db, household_id)
    #: Only a pass over every row knows where all the rows of a key went.
    moving = rekey and transaction_ids is None
    renamed: dict[str, set[str | None]] = {}
    renamed_at: dict[tuple[uuid.UUID, Direction, str], set[str | None]] = {}
    taught: Counter[_Taught] = Counter()
    after: tuple[Any, uuid.UUID] | None = None
    while True:
        if check_cancelled is not None:
            check_cancelled()
        statement = select(
            Transaction.id,
            Transaction.account_id,
            Transaction.posted_date,
            Transaction.raw_payee,
            Transaction.amount,
            Transaction.payee_id,
            Transaction.merchant_key,
            Transaction.category_id,
            Transaction.suggested_category_id,
            Transaction.category_source,
            Transaction.category_confidence,
            reconsidered_clause().label("open"),
        ).where(*clauses)
        if after is not None:
            statement = statement.where(
                tuple_(Transaction.posted_date, Transaction.id) < tuple_(after[0], after[1])
            )
        batch = db.execute(
            statement.order_by(Transaction.posted_date.desc(), Transaction.id.desc())
            .limit(BATCH)
            .with_for_update(of=Transaction)
        ).all()
        if not batch:
            break
        summary.scanned += len(batch)
        changes: dict[uuid.UUID, dict[str, Any]] = {}
        to_decide: list[_Open] = []
        for found in batch:
            if not (found.open or rekey):
                continue
            identity = identify(found.raw_payee)
            if identity.key != found.merchant_key and (rekey or found.merchant_key is None):
                changes[found.id] = {"merchant_key": identity.key}
                summary.keyed += 1
            if moving and found.merchant_key is not None:
                renamed.setdefault(found.merchant_key, set()).add(identity.key)
                place = (found.account_id, memory.direction_of(found.amount), found.merchant_key)
                renamed_at.setdefault(place, set()).add(identity.key)
                if (
                    identity.key is not None
                    and identity.key != found.merchant_key
                    and found.category_source == PERSON_SOURCE
                    and found.category_id is not None
                    and identity.kind in cleanup.TEACHABLE
                ):
                    # One decision of the memory stands for this row.
                    taught[
                        (
                            found.merchant_key,
                            memory.direction_of(found.amount),
                            found.category_id,
                            identity.key,
                        )
                    ] += 1
            if found.open:
                to_decide.append(
                    _Open(
                        id=found.id,
                        account_id=found.account_id,
                        identity=identity,
                        direction=memory.direction_of(found.amount),
                        payee_id=found.payee_id,
                    )
                )
        decided = _decisions(db, household_id, known, to_decide)
        current = {
            found.id: Decision(
                category_id=found.category_id,
                suggested_category_id=found.suggested_category_id,
                source=found.category_source,
                confidence=found.category_confidence,
            )
            for found in batch
        }
        for item in to_decide:
            decision = decided[item.id]
            if decision != current[item.id]:
                changes.setdefault(item.id, {}).update(_decision_values(decision))
                _count(summary, item.account_id, decision)
        _write(db, household_id, changes)
        after = (batch[-1].posted_date, batch[-1].id)
        if len(batch) < BATCH:
            break
    if moving:
        summary.memory_moved = _move_memory(db, household_id, renamed, taught)
        summary.renamed = {old: frozenset(now) for old, now in renamed.items()}
        summary.renamed_at = {place: frozenset(now) for place, now in renamed_at.items()}
    _announce(db, household_id, summary)
    return summary

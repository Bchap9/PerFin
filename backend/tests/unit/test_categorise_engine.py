# SPDX-License-Identifier: AGPL-3.0-or-later
"""``engine.identify`` and ``engine.decide``: who was paid and what it probably is.

Pure: no database. The clean-up, the dictionary and the memory are stubbed
here, so these tests pin the decision order and the confidences and nothing
about what the dictionary holds. Merchants and descriptors are invented.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest

from app.domain.categorisation import cleanup, dictionary, engine, memory
from app.domain.categorisation.cleanup import Cleaned, DescriptorKind
from app.domain.categorisation.dictionary import Hit, KeywordHit, Merchant
from app.domain.categorisation.engine import Decision, Identity
from app.domain.categorisation.memory import Direction, Learned

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

#: The household's keyed categories: a default tree, cut down to what is used here.
KEYS = (
    "food",
    "food.groceries",
    "food.restaurants",
    "food.coffee",
    "leisure.subscriptions",
    "shopping.general",
    "income.salary",
    "income.interest",
    "income.refunds",
    "financial.bank_fees",
    "financial.interest_paid",
    "transfers.credit_card_payment",
    "transfers.between_accounts",
    "transfers.loan_payment",
    "transfers.cash",
)
CATEGORIES = {key: uuid.uuid5(uuid.NAMESPACE_DNS, key) for key in KEYS}
OWN = uuid.uuid5(uuid.NAMESPACE_DNS, "a category the household made itself")

STREAMBOX = Merchant(key="streambox", name="StreamBox", category="leisure.subscriptions")
EVERYMART = Merchant(key="everymart", name="EveryMart", category="shopping.general", mixed=True)
PAYWORKS = Merchant(key="payworks", name="PayWorks", category="income.salary", inflow=True)
DRIVE_THRU = Merchant(key="drive thru", name="Drive Thru", category="food.fast_food")
FAR_AWAY = Merchant(key="far away", name="Far Away", category="travel.hotels")


def _identity(
    core: str = "",
    *,
    merchant: Merchant | None = None,
    how: Literal["exact", "prefix", "anywhere"] = "exact",
    kind: DescriptorKind = "purchase",
    via: str | None = None,
) -> Identity:
    hit = Hit(merchant=merchant, how=how) if merchant is not None else None
    key = merchant.key if merchant is not None else (core or None)
    return Identity(
        key=key,
        name=merchant.name if merchant is not None else None,
        kind=kind,
        via=via,
        hit=hit,
        core=core or (merchant.key if merchant is not None else ""),
    )


def _learned(
    decisions: int, total: int, *, at: datetime = NOW, category_id: uuid.UUID = OWN
) -> Learned:
    share = (Decimal(decisions) / Decimal(total)).quantize(Decimal("0.01"))
    return Learned(
        memory_id=uuid.uuid4(),
        category_id=category_id,
        decisions=decisions,
        total=total,
        share=share,
        last_decided_at=at,
    )


def _decide(
    identity: Identity,
    direction: Direction = "out",
    *,
    learned: Learned | None = None,
    payee_default: uuid.UUID | None = None,
    categories: dict[str, uuid.UUID] | None = None,
) -> Decision:
    return engine.decide(
        identity,
        direction,
        learned=learned,
        payee_default=payee_default,
        categories=CATEGORIES if categories is None else categories,
        now=NOW,
    )


def _fact(key_or_id: str | uuid.UUID, source: str, confidence: str) -> Decision:
    category_id = CATEGORIES[key_or_id] if isinstance(key_or_id, str) else key_or_id
    return Decision(category_id=category_id, source=source, confidence=Decimal(confidence))


def _suggestion(key_or_id: str | uuid.UUID, source: str, confidence: str) -> Decision:
    category_id = CATEGORIES[key_or_id] if isinstance(key_or_id, str) else key_or_id
    return Decision(
        suggested_category_id=category_id, source=source, confidence=Decimal(confidence)
    )


@pytest.fixture
def no_keywords(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dictionary, "keyword", lambda core, via: None)


@pytest.fixture
def keywords(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pizza`` is a weak keyword; anything behind the processor ``toast`` a strong one."""

    def keyword(core: str, via: str | None) -> KeywordHit | None:
        if via == "toast":
            return KeywordHit(category="food.restaurants", word="toast", strong=True)
        if "pizza" in core.split():
            return KeywordHit(category="food.restaurants", word="pizza", strong=False)
        return None

    monkeypatch.setattr(dictionary, "keyword", keyword)


# --- identify -----------------------------------------------------------------


def test_identity_uses_the_dictionary_key_and_name(monkeypatch: pytest.MonkeyPatch) -> None:
    hit = Hit(merchant=STREAMBOX, how="prefix")
    seen: list[str] = []

    def lookup(core: str) -> Hit | None:
        seen.append(core)
        return hit

    monkeypatch.setattr(
        cleanup,
        "clean",
        lambda raw: Cleaned(core="streambox com monthly", kind="recurring_purchase", via="paypal"),
    )
    monkeypatch.setattr(dictionary, "lookup", lookup)

    identity = engine.identify("PAYPAL *STREAMBOX.COM MONTHLY 0412")

    assert seen == ["streambox com monthly"]
    assert identity == Identity(
        key="streambox",
        name="StreamBox",
        kind="recurring_purchase",
        via="paypal",
        hit=hit,
        core="streambox com monthly",
    )


def test_identity_without_a_hit_keeps_four_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cleanup,
        "clean",
        lambda raw: Cleaned(core="lantern books and paper goods annex", kind="purchase"),
    )
    monkeypatch.setattr(dictionary, "lookup", lambda core: None)
    monkeypatch.setattr(dictionary, "name_for", lambda key: None)

    identity = engine.identify("POS DEBIT LANTERN BOOKS AND PAPER GOODS ANNEX")

    assert identity.key == "lantern books and paper"
    assert identity.name == "Lantern Books And Paper"
    assert identity.hit is None
    assert identity.core == "lantern books and paper goods annex"
    assert engine.display_name("lantern books and paper") == "Lantern Books And Paper"
    assert engine.display_name(None) is None


def test_identity_of_nothing_names_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    looked_up: list[str] = []
    monkeypatch.setattr(cleanup, "clean", lambda raw: Cleaned(core="", kind="fee"))
    monkeypatch.setattr(dictionary, "lookup", looked_up.append)

    identity = engine.identify("MONTHLY MAINTENANCE FEE")

    assert (identity.key, identity.name, identity.kind) == (None, None, "fee")
    assert looked_up == []


# --- decide: the order --------------------------------------------------------


@pytest.mark.usefixtures("no_keywords")
def test_payee_default_comes_first() -> None:
    decision = _decide(
        _identity(merchant=STREAMBOX),
        learned=_learned(5, 5),
        payee_default=CATEGORIES["food.coffee"],
    )
    assert decision == _fact("food.coffee", "payee", "0.95")


@pytest.mark.usefixtures("no_keywords")
def test_learned_once_is_a_fact_at_080() -> None:
    # The household's own answer beats the dictionary's for the same merchant.
    decision = _decide(_identity(merchant=STREAMBOX), learned=_learned(1, 1))
    assert decision == _fact(OWN, "learned", "0.80")
    assert _decide(_identity("corner grocer"), learned=_learned(2, 2)) == _fact(
        OWN, "learned", "0.90"
    )


@pytest.mark.usefixtures("no_keywords")
def test_learned_three_of_four_is_a_suggestion() -> None:
    decision = _decide(_identity("corner grocer"), learned=_learned(3, 4))
    assert decision == _suggestion(OWN, "learned", "0.71")


@pytest.mark.usefixtures("no_keywords")
def test_learned_nine_of_ten_is_a_fact() -> None:
    decision = _decide(_identity("corner grocer"), learned=_learned(9, 10))
    assert decision == _fact(OWN, "learned", "0.86")


@pytest.mark.usefixtures("keywords")
def test_a_split_household_gets_no_learned_answer_and_only_a_suggestion_from_the_dictionary() -> (
    None
):
    split = _learned(5, 10)  # share 0.50: filed in several places
    assert _decide(_identity(merchant=STREAMBOX), learned=split) == _suggestion(
        "leisure.subscriptions", "dictionary", "0.60"
    )
    # Neither the memory nor anything else states a fact for it.
    assert _decide(_identity("corner grocer"), learned=split) == Decision()
    assert _decide(_identity(kind="fee"), learned=split) == _suggestion(
        "financial.bank_fees", "dictionary", "0.60"
    )
    assert _decide(_identity("harbor", via="toast"), learned=split) == _suggestion(
        "food.restaurants", "keyword", "0.60"
    )
    # Just at the line it is an answer again.
    assert _decide(_identity(merchant=STREAMBOX), learned=_learned(3, 5)).source == "learned"


@pytest.mark.usefixtures("no_keywords")
def test_stale_learning_is_a_suggestion() -> None:
    old = NOW - timedelta(days=memory.STALE_DAYS + 1)
    assert _decide(_identity("corner grocer"), learned=_learned(4, 4, at=old)) == _suggestion(
        OWN, "learned", "0.70"
    )
    # On the last day it still counts in full, and the cap never raises a weak answer.
    edge = NOW - timedelta(days=memory.STALE_DAYS)
    assert _decide(_identity("corner grocer"), learned=_learned(4, 4, at=edge)) == _fact(
        OWN, "learned", "0.95"
    )
    assert _decide(_identity("corner grocer"), learned=_learned(2, 3, at=old)) == _suggestion(
        OWN, "learned", "0.60"
    )


@pytest.mark.usefixtures("no_keywords")
def test_dictionary_hit_is_a_fact_at_090() -> None:
    assert _decide(_identity(merchant=STREAMBOX)) == _fact(
        "leisure.subscriptions", "dictionary", "0.90"
    )
    assert _decide(_identity(merchant=STREAMBOX, how="prefix")) == _fact(
        "leisure.subscriptions", "dictionary", "0.90"
    )
    assert _decide(_identity(merchant=STREAMBOX, how="anywhere")) == _fact(
        "leisure.subscriptions", "dictionary", "0.85"
    )


@pytest.mark.usefixtures("no_keywords")
def test_behind_a_processor_it_is_085() -> None:
    assert _decide(_identity(merchant=STREAMBOX, via="paypal")) == _fact(
        "leisure.subscriptions", "dictionary", "0.85"
    )
    assert _decide(_identity(merchant=STREAMBOX, how="anywhere", via="paypal")) == _fact(
        "leisure.subscriptions", "dictionary", "0.80"
    )


@pytest.mark.usefixtures("no_keywords")
def test_a_mixed_store_is_a_suggestion() -> None:
    expected = _suggestion("shopping.general", "dictionary", "0.60")
    assert _decide(_identity(merchant=EVERYMART)) == expected
    assert _decide(_identity(merchant=EVERYMART, how="anywhere", via="paypal")) == expected
    # Money back from it is no surer than money spent there.
    assert _decide(_identity(merchant=EVERYMART), "in") == expected


@pytest.mark.usefixtures("no_keywords")
def test_money_back_from_a_merchant_takes_its_category() -> None:
    expected = _fact("leisure.subscriptions", "dictionary", "0.80")
    assert _decide(_identity(merchant=STREAMBOX), "in") == expected
    assert _decide(_identity(merchant=STREAMBOX, kind="refund"), "in") == expected
    assert _decide(_identity(merchant=STREAMBOX, how="anywhere", via="paypal"), "in") == expected


@pytest.mark.usefixtures("keywords")
@pytest.mark.parametrize("kind", ["ach_credit", "ach_debit", "unknown"])
def test_money_in_that_is_no_refund_is_only_suggested_for_a_spending_category(
    kind: DescriptorKind,
) -> None:
    # A bank credit that names a ride service may be a refund, or earnings.
    unsure = _suggestion("leisure.subscriptions", "dictionary", "0.70")
    assert _decide(_identity(merchant=STREAMBOX, kind=kind), "in") == unsure
    assert _decide(_identity(merchant=STREAMBOX, kind=kind, via="paypal"), "in") == unsure
    # A store that sells everything is no surer for it.
    assert _decide(_identity(merchant=EVERYMART, kind=kind), "in") == _suggestion(
        "shopping.general", "dictionary", "0.60"
    )
    # A strong word is held to the same line, a weak one is below it anyway.
    assert _decide(_identity("harbor", kind=kind, via="toast"), "in") == _suggestion(
        "food.restaurants", "keyword", "0.70"
    )
    assert _decide(_identity("harbor pizza", kind=kind), "in") == _suggestion(
        "food.restaurants", "keyword", "0.60"
    )

    # Not held to it: the same line going out, a merchant that pays, and
    # money back on a line that says so or is a card purchase.
    assert _decide(_identity(merchant=STREAMBOX, kind=kind)) == _fact(
        "leisure.subscriptions", "dictionary", "0.90"
    )
    assert _decide(_identity("harbor", kind=kind, via="toast")) == _fact(
        "food.restaurants", "keyword", "0.80"
    )
    assert _decide(_identity(merchant=PAYWORKS, kind=kind), "in") == _fact(
        "income.salary", "dictionary", "0.90"
    )
    for back in ("refund", "purchase", "recurring_purchase"):
        assert _decide(_identity(merchant=STREAMBOX, kind=back), "in") == _fact(
            "leisure.subscriptions", "dictionary", "0.80"
        )
    assert _decide(_identity("harbor", kind="purchase", via="toast"), "in") == _fact(
        "food.restaurants", "keyword", "0.80"
    )


@pytest.mark.usefixtures("keywords")
def test_payroll_only_for_money_in() -> None:
    salary = _fact("income.salary", "dictionary", "0.90")
    assert _decide(_identity(merchant=PAYWORKS, kind="ach_credit"), "in") == salary
    # Money going out to a payroll processor is not a salary, and no word in its
    # name makes it anything else.
    assert _decide(_identity(merchant=PAYWORKS, kind="ach_debit"), "out") == Decision()
    assert _decide(_identity("payworks pizza", merchant=PAYWORKS), "out") == Decision()
    # The kind alone says salary only for money in.
    assert _decide(_identity("maple studio", kind="payroll"), "in") == _fact(
        "income.salary", "dictionary", "0.85"
    )
    assert _decide(_identity("maple studio", kind="payroll"), "out") == Decision()


@pytest.mark.usefixtures("no_keywords")
@pytest.mark.parametrize(
    ("kind", "direction", "key", "confidence"),
    [
        ("card_payment", "out", "transfers.credit_card_payment", "0.85"),
        ("card_payment", "in", "transfers.credit_card_payment", "0.85"),
        ("transfer", "out", "transfers.between_accounts", "0.80"),
        ("transfer", "in", "transfers.between_accounts", "0.80"),
        ("loan_payment", "out", "transfers.loan_payment", "0.80"),
        ("loan_payment", "in", "transfers.loan_payment", "0.80"),
        ("cash", "out", "transfers.cash", "0.90"),
        ("cash", "in", "transfers.cash", "0.90"),
        ("fee", "out", "financial.bank_fees", "0.90"),
        ("fee", "in", "financial.bank_fees", "0.90"),
        ("interest", "in", "income.interest", "0.90"),
        ("interest", "out", "financial.interest_paid", "0.85"),
        ("payroll", "in", "income.salary", "0.85"),
        ("refund", "in", "income.refunds", "0.80"),
    ],
)
def test_kinds(kind: DescriptorKind, direction: Direction, key: str, confidence: str) -> None:
    assert (kind, direction) in engine.KIND_CATEGORIES
    assert _decide(_identity(kind=kind), direction) == _fact(key, "dictionary", confidence)


def test_the_kind_table_has_no_other_rows() -> None:
    assert len(engine.KIND_CATEGORIES) == 14
    assert not {kind for kind, _ in engine.KIND_CATEGORIES} & engine.NEVER_GUESSED


@pytest.mark.usefixtures("no_keywords")
def test_a_kind_is_read_before_a_merchant_except_for_a_refund() -> None:
    # A card payment that happens to name a merchant is still a card payment.
    assert _decide(_identity(merchant=STREAMBOX, kind="card_payment")) == _fact(
        "transfers.credit_card_payment", "dictionary", "0.85"
    )
    # A refund that names nobody is income; a refund going out is not guessed.
    assert _decide(_identity("corner grocer", kind="refund"), "in") == _fact(
        "income.refunds", "dictionary", "0.80"
    )
    assert _decide(_identity("corner grocer", kind="refund"), "out") == Decision()


@pytest.mark.usefixtures("keywords")
@pytest.mark.parametrize("kind", ["check", "deposit", "p2p"])
def test_cheques_deposits_and_p2p_are_never_guessed(kind: DescriptorKind) -> None:
    assert kind in engine.NEVER_GUESSED
    directions: tuple[Direction, ...] = ("out", "in")
    for direction in directions:
        decision = _decide(
            _identity("harbor pizza", merchant=STREAMBOX, kind=kind, via="toast"),
            direction,
            learned=_learned(5, 5),
        )
        assert decision == Decision()
    # A payee a person gave a default category is their decision, not a guess.
    assert _decide(_identity(kind=kind), payee_default=OWN) == _fact(OWN, "payee", "0.95")


@pytest.mark.usefixtures("keywords")
def test_a_weak_keyword_is_a_suggestion_and_a_strong_one_a_fact() -> None:
    assert _decide(_identity("harbor pizza")) == _suggestion("food.restaurants", "keyword", "0.60")
    assert _decide(_identity("harbor", via="toast")) == _fact("food.restaurants", "keyword", "0.80")
    assert _decide(_identity("harbor books")) == Decision()
    # A merchant is asked before a word in its name.
    assert _decide(_identity("streambox pizza", merchant=STREAMBOX)).source == "dictionary"


@pytest.mark.usefixtures("keywords")
def test_a_key_the_household_does_not_have_falls_back_to_the_group_then_nothing() -> None:
    # No ``food.fast_food`` here, but the group ``food`` is.
    assert _decide(_identity(merchant=DRIVE_THRU)) == _fact("food", "dictionary", "0.90")
    # Neither ``travel.hotels`` nor ``travel``.
    assert _decide(_identity(merchant=FAR_AWAY)) == Decision()
    # A household with its own tree gets no guess from the dictionary or a keyword...
    assert _decide(_identity(merchant=STREAMBOX), categories={}) == Decision()
    assert _decide(_identity(kind="fee"), categories={}) == Decision()
    assert _decide(_identity("harbor pizza"), categories={}) == Decision()
    # ...a merchant whose category it lacks is not handed to a keyword instead...
    assert _decide(_identity("far away pizza", merchant=FAR_AWAY)) == Decision()
    # ...and is still served by what it taught.
    assert _decide(_identity(merchant=STREAMBOX), learned=_learned(1, 1), categories={}) == _fact(
        OWN, "learned", "0.80"
    )


def test_a_decision_is_a_fact_or_a_suggestion_never_both() -> None:
    assert Decimal("0.80") == engine.APPLY_MIN
    assert Decision() == Decision(None, None, None, None)


# --- found by the quality targets (tests/unit/test_categorisation_quality.py) ---


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        # An order through a platform is the platform's, whatever it names.
        ("DD *DOORDASH PANDAEXPR", "doordash"),
        ("DOORDASH*LANTERN THAI", "doordash"),
        ("GRUBHUB*TACOBELL", "grubhub"),
        ("IC* COSTCO BY INSTACART", "instacart"),
        ("EB *SUMMER LANTERN FEST 877-555-0136 TX", "eventbrite"),
        # Without the prefix, when the line begins with its name.
        ("PP*DOORDASH", "doordash"),
        ("GRUBHUB HARBOR PIZZA", "grubhub"),
        ("CHECKCARD 1218 GRUBHUB HOLDINGS INC 04412322799069168386015", "grubhub"),
        # The platform's own membership stays what it is.
        ("DD *DOORDASH DASHPASS", "dashpass"),
        # Any other processor only fronts the merchant.
        ("PAYPAL *TACOBELL", "taco bell"),
        ("SQ *LANTERN THAI", "lantern thai"),
    ],
)
def test_an_order_through_a_platform_is_the_platform_s(raw: str, key: str) -> None:
    assert engine.identify(raw).key == key


def test_a_platform_order_is_a_fact_in_the_platform_s_category() -> None:
    categories = {"food.delivery": CATEGORIES["food.restaurants"], "food.fast_food": OWN}
    identity = engine.identify("PURCHASE AUTHORIZED ON 02/12 GRUBHUB*TACOBELL S336228209290428")
    assert identity.hit is not None
    assert (identity.hit.merchant.category, identity.via) == ("food.delivery", "grubhub")
    assert _decide(identity, categories=categories) == Decision(
        category_id=categories["food.delivery"], source="dictionary", confidence=Decimal("0.85")
    )


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        # A shop nobody knows: the word in front of the state is the city.
        ("JUNEBUG ASHGROVE OH", "junebug"),
        ("SQ *BUTTONWOOD HARLOWTON GA", "buttonwood"),
        ("POS PURCHASE STONECROP ELKTON FALLS MT", "stonecrop"),
        # ... as its other lines say.
        ("IN *JUNEBUG", "junebug"),
        ("JUNEBUG #12 ASHGROVE OH", "junebug"),
        # A word that says what is sold is no town.
        ("CORNER PHARMACY OH", "corner pharmacy"),
        ("LANTERN PIZZA TX", "lantern pizza"),
        # The dictionary knows the longer name.
        ("MASSAGE ENVY CA", "massage envy"),
        ("TRADER JOE S WA", "trader joes"),
        # A chain's name in front of another word is left as printed: it is
        # neither the chain nor a shop of one word.
        ("SUBWAY ALDERTON TX", "subway alderton"),
        ("TARGET SPORTS OH", "target sports"),
    ],
)
def test_an_unknown_shop_in_front_of_a_state_loses_the_city(raw: str, key: str) -> None:
    assert engine.identify(raw).key == key


def test_the_shorter_name_is_only_the_key() -> None:
    identity = engine.identify("JUNEBUG ASHGROVE OH")
    # The core, which keywords are read from, is what clean-up gave.
    assert (identity.key, identity.core, identity.hit) == ("junebug", "junebug ashgrove", None)
    assert identity.name == "Junebug"

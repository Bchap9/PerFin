# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rules engine without a database (``app.domain.rules.engine`` / ``.schemas``).

Covers the condition types, the ordering semantics (order by ``sort_order``,
first matching rule wins per field, ``stop_processing``, chaining over the
working state), splits, the glob matcher (agrees with :mod:`fnmatch` and is
ReDoS-proof), the document limits, and property tests for determinism.
"""

from __future__ import annotations

import fnmatch
import random
import time
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from app.domain.rules import engine
from app.domain.rules.engine import CompiledRule, RuleField, SplitLine, TxnFacts
from app.domain.rules.schemas import (
    CONDITION_DEPTH_MAX,
    TEXT_VALUE_MAX,
    Action,
    AddTagsAction,
    AllCondition,
    AmountCondition,
    AnyCondition,
    CategoryIsCondition,
    CategoryUnsetCondition,
    Condition,
    CurrencyCondition,
    DateCondition,
    FlagForReviewAction,
    MemoCondition,
    NotCondition,
    PayeeCondition,
    PayeeIsCondition,
    RuleBody,
    RuleCreate,
    SetCategoryAction,
    SetNoteAction,
    SetPayeeAction,
    SignCondition,
    SplitAmountAction,
    SplitAmountLine,
    SplitPercentAction,
    SplitPercentLine,
)
from app.domain.rules.schemas import AccountCondition as AccountIs
from app.domain.transactions.models import ReviewState

ACCOUNT = uuid.UUID("00000000-0000-7000-8000-00000000000a")
OTHER_ACCOUNT = uuid.UUID("00000000-0000-7000-8000-00000000000b")
CATS = [uuid.UUID(f"00000000-0000-7000-8000-0000000001{i:02d}") for i in range(6)]
TAGS = [uuid.UUID(f"00000000-0000-7000-8000-0000000002{i:02d}") for i in range(4)]
PAYEES = [uuid.UUID(f"00000000-0000-7000-8000-0000000003{i:02d}") for i in range(3)]
PAYEE_NAMES = {PAYEES[0]: "Corner Coffee", PAYEES[1]: "Grocer Co", PAYEES[2]: "Rent Ltd"}


def txn(**overrides: Any) -> TxnFacts:
    base: dict[str, Any] = {
        "id": uuid.UUID("00000000-0000-7000-8000-000000000f01"),
        "account_id": ACCOUNT,
        "posted_date": date(2026, 3, 14),
        "amount": Decimal("-42.50"),
        "currency": "USD",
        "raw_payee": "CORNER COFFEE #123  Springfield",
        "memo": "card purchase",
    }
    base.update(overrides)
    return TxnFacts(**base)


def rule(
    conditions: Any,
    *actions: Any,
    order: int = 0,
    stop: bool = False,
    enabled: bool = True,
    rule_id: uuid.UUID | None = None,
) -> CompiledRule:
    return CompiledRule(
        id=rule_id or uuid.UUID(int=1000 + order),
        sort_order=order,
        conditions=conditions,
        actions=tuple(actions),
        enabled=enabled,
        stop_processing=stop,
    )


EVERYTHING = AllCondition()


# ---- conditions ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (PayeeCondition(op="contains", value="corner coffee"), True),
        (PayeeCondition(op="contains", value="CORNER   coffee"), True),  # whitespace folded
        (PayeeCondition(op="equals", value="corner coffee"), False),
        (PayeeCondition(op="equals", value="Corner Coffee #123 springfield"), True),
        (PayeeCondition(op="starts_with", value="corner"), True),
        (PayeeCondition(op="ends_with", value="springfield"), True),
        (PayeeCondition(op="glob", value="corner*#1?3*"), True),
        (PayeeCondition(op="glob", value="corner"), False),  # glob is anchored
        (PayeeCondition(op="glob", value="*[a-z]*"), False),  # brackets are literal
        (MemoCondition(op="contains", value="CARD"), True),
        (MemoCondition(op="equals", value="card"), False),
        (AmountCondition(min=Decimal("-50"), max=Decimal("-40")), True),
        (AmountCondition(min=Decimal("-40")), False),
        (AmountCondition(min=Decimal("40"), max=Decimal("45"), absolute=True), True),
        (AmountCondition(max=Decimal("42.49"), absolute=True), False),
        (SignCondition(sign="outflow"), True),
        (SignCondition(sign="inflow"), False),
        (AccountIs(account_ids=[OTHER_ACCOUNT, ACCOUNT]), True),
        (AccountIs(account_ids=[OTHER_ACCOUNT]), False),
        (CurrencyCondition(currencies=["EUR", "USD"]), True),
        (CurrencyCondition(currencies=["EUR"]), False),
        (DateCondition(date_from=date(2026, 3, 14), date_to=date(2026, 3, 14)), True),
        (DateCondition(date_from=date(2026, 3, 15)), False),
        (DateCondition(date_to=date(2026, 3, 13)), False),
        (CategoryUnsetCondition(), True),
        (CategoryIsCondition(category_ids=[CATS[0]]), False),
        (PayeeIsCondition(payee_ids=[PAYEES[0]]), False),
        (AllCondition(), True),
        (
            AllCondition(conditions=[SignCondition(sign="outflow"), CategoryUnsetCondition()]),
            True,
        ),
        (AnyCondition(conditions=[SignCondition(sign="inflow"), CategoryUnsetCondition()]), True),
        (AnyCondition(conditions=[SignCondition(sign="inflow")]), False),
        (NotCondition(condition=SignCondition(sign="inflow")), True),
    ],
)
def test_condition(condition: Condition, expected: bool) -> None:
    assert engine.matches(condition, txn()) is expected


def test_payee_condition_also_matches_the_canonical_name() -> None:
    named = txn(raw_payee="POS 99812", payee_id=PAYEES[0], payee_name="Corner Coffee")
    assert engine.matches(PayeeCondition(op="equals", value="corner coffee"), named)
    assert engine.matches(PayeeIsCondition(payee_ids=[PAYEES[0]]), named)
    assert not engine.matches(PayeeCondition(op="contains", value="x"), txn(raw_payee=None))


def test_category_conditions_see_splits() -> None:
    split = txn(splits=(SplitLine(CATS[1], Decimal("-20")), SplitLine(None, Decimal("-22.50"))))
    assert engine.matches(CategoryIsCondition(category_ids=[CATS[1]]), split)
    assert not engine.matches(CategoryUnsetCondition(), split)
    assert engine.matches(CategoryIsCondition(category_ids=[CATS[2]]), txn(category_id=CATS[2]))


# ---- ordering semantics --------------------------------------------------------------


def test_rules_run_in_sort_order_and_first_writer_wins() -> None:
    late = rule(EVERYTHING, SetCategoryAction(category_id=CATS[1]), order=5)
    early = rule(EVERYTHING, SetCategoryAction(category_id=CATS[0]), order=1)
    outcome = engine.evaluate([late, early], txn())
    assert outcome.after.category_id == CATS[0]
    assert outcome.matched == (early.id, late.id)
    assert [(s.rule_id, s.action) for s in outcome.skipped] == [(late.id, "set_category")]
    assert "already set by rule" in outcome.skipped[0].reason


def test_ties_in_sort_order_break_by_id() -> None:
    a = rule(EVERYTHING, SetNoteAction(note="a"), rule_id=uuid.UUID(int=1))
    b = rule(EVERYTHING, SetNoteAction(note="b"), rule_id=uuid.UUID(int=2))
    assert engine.evaluate([b, a], txn()).after.notes == "a"


def test_stop_processing_ends_the_run() -> None:
    first = rule(EVERYTHING, AddTagsAction(tag_ids=[TAGS[0]]), order=0, stop=True)
    second = rule(EVERYTHING, AddTagsAction(tag_ids=[TAGS[1]]), order=1)
    outcome = engine.evaluate([first, second], txn())
    assert outcome.matched == (first.id,)
    assert outcome.after.tag_ids == {TAGS[0]}
    # A stop rule that does not match does not stop anything.
    unmatched = rule(SignCondition(sign="inflow"), FlagForReviewAction(), order=0, stop=True)
    assert engine.evaluate([unmatched, second], txn()).matched == (second.id,)


def test_all_matches_mode_accumulates_tags_and_flags() -> None:
    rules = [
        rule(EVERYTHING, AddTagsAction(tag_ids=[TAGS[0]]), order=0),
        rule(SignCondition(sign="outflow"), AddTagsAction(tag_ids=[TAGS[1], TAGS[0]]), order=1),
        rule(EVERYTHING, FlagForReviewAction(), order=2),
    ]
    outcome = engine.evaluate(rules, txn(tag_ids=frozenset({TAGS[3]})))
    assert outcome.after.tag_ids == {TAGS[0], TAGS[1], TAGS[3]}
    assert outcome.added_tags == {TAGS[0], TAGS[1]}
    assert outcome.after.review_state is ReviewState.PENDING
    # A confirmed transaction stays confirmed.
    confirmed = engine.evaluate(rules, txn(review_state=ReviewState.CONFIRMED))
    assert confirmed.after.review_state is ReviewState.CONFIRMED


def test_later_rules_see_the_working_state() -> None:
    rename = rule(
        PayeeCondition(op="contains", value="corner coffee"),
        SetPayeeAction(payee_id=PAYEES[0]),
        order=0,
    )
    by_payee = rule(
        PayeeIsCondition(payee_ids=[PAYEES[0]]), SetCategoryAction(category_id=CATS[0]), order=1
    )
    fallback = rule(CategoryUnsetCondition(), SetCategoryAction(category_id=CATS[5]), order=9)
    outcome = engine.evaluate([fallback, by_payee, rename], txn(), payee_names=PAYEE_NAMES)
    assert outcome.after.payee_id == PAYEES[0]
    assert outcome.after.payee_name == "Corner Coffee"
    assert outcome.after.category_id == CATS[0]
    assert outcome.matched == (rename.id, by_payee.id)  # the fallback no longer matches
    other = engine.evaluate([fallback, by_payee, rename], txn(raw_payee="Unknown"))
    assert other.after.category_id == CATS[5]


def test_disabled_rules_never_run() -> None:
    off = rule(EVERYTHING, SetNoteAction(note="x"), enabled=False)
    assert not engine.evaluate([off], txn()).matched


def test_protected_fields_are_never_written() -> None:
    rules = [
        rule(
            EVERYTHING,
            SetCategoryAction(category_id=CATS[0]),
            AddTagsAction(tag_ids=[TAGS[0]]),
            SetNoteAction(note="auto"),
            FlagForReviewAction(),
        )
    ]
    outcome = engine.evaluate(
        rules,
        txn(category_id=CATS[3]),
        protect=[RuleField.CATEGORIZATION, RuleField.TAGS, RuleField.REVIEW_STATE],
    )
    assert outcome.after.category_id == CATS[3]
    assert outcome.after.tag_ids == frozenset()
    assert outcome.after.review_state is ReviewState.NONE
    assert outcome.after.notes == "auto"
    assert outcome.changed == {RuleField.NOTES}
    assert {s.action for s in outcome.skipped} == {"set_category", "add_tags", "flag_for_review"}


def test_set_category_replaces_splits_and_unchanged_values_are_no_change() -> None:
    split = txn(splits=(SplitLine(CATS[1], Decimal("-42.50")),))
    outcome = engine.evaluate([rule(EVERYTHING, SetCategoryAction(category_id=CATS[0]))], split)
    assert (outcome.after.category_id, outcome.after.splits) == (CATS[0], ())
    same = engine.evaluate(
        [rule(EVERYTHING, SetCategoryAction(category_id=CATS[0]))], txn(category_id=CATS[0])
    )
    assert same.matched
    assert not same.changed


# ---- splits --------------------------------------------------------------------------


def test_split_percent_rounds_to_minor_units_and_keeps_the_total() -> None:
    action = SplitPercentAction(
        lines=[
            SplitPercentLine(category_id=CATS[0], percent=Decimal("33.3333")),
            SplitPercentLine(category_id=CATS[1], percent=Decimal("33.3333")),
            SplitPercentLine(category_id=CATS[2], percent=Decimal("33.3334")),
        ]
    )
    lines = engine.split_percent(action, txn(amount=Decimal("-100.00")))
    assert not isinstance(lines, str)
    assert [line.amount for line in lines] == [
        Decimal("-33.33"),
        Decimal("-33.33"),
        Decimal("-33.34"),
    ]
    jpy = engine.split_percent(action, txn(amount=Decimal("1000"), currency="JPY"))
    assert not isinstance(jpy, str)
    assert [line.amount for line in jpy] == [Decimal("333"), Decimal("333"), Decimal("334")]
    tiny = engine.split_percent(action, txn(amount=Decimal("-0.01")))
    assert isinstance(tiny, str)
    assert isinstance(engine.split_percent(action, txn(amount=Decimal(0))), str)


def test_split_amount_with_and_without_remainder() -> None:
    with_rest = SplitAmountAction(
        lines=[
            SplitAmountLine(category_id=CATS[0], amount=Decimal("10")),
            SplitAmountLine(category_id=CATS[1], memo="rest"),
        ]
    )
    lines = engine.split_amount(with_rest, txn(amount=Decimal("-42.50")))
    assert lines == (
        SplitLine(CATS[0], Decimal("-10"), None),
        SplitLine(CATS[1], Decimal("-32.50"), "rest"),
    )
    assert isinstance(engine.split_amount(with_rest, txn(amount=Decimal("-10"))), str)
    exact = SplitAmountAction(
        lines=[
            SplitAmountLine(category_id=CATS[0], amount=Decimal("40")),
            SplitAmountLine(category_id=CATS[1], amount=Decimal("2.50")),
        ]
    )
    assert not isinstance(engine.split_amount(exact, txn()), str)
    assert isinstance(engine.split_amount(exact, txn(amount=Decimal("42.51"))), str)
    outcome = engine.evaluate([rule(EVERYTHING, exact)], txn(amount=Decimal("1")))
    assert outcome.after.splits == ()
    assert outcome.skipped[0].action == "split_amount"


# ---- documents -----------------------------------------------------------------------


def _body(conditions: Any, actions: Any = None) -> RuleBody:
    return RuleBody.model_validate(
        {
            "conditions": conditions,
            "actions": [{"type": "flag_for_review"}] if actions is None else actions,
        }
    )


@pytest.mark.parametrize(
    "conditions",
    [
        {"type": "payee", "op": "regex", "value": "(a+)+$"},  # no regular expressions
        {"type": "payee", "op": "contains", "value": ""},
        {"type": "payee", "op": "contains", "value": "x" * (TEXT_VALUE_MAX + 1)},
        {"type": "payee", "op": "contains", "value": "a\nb"},
        {"type": "memo", "op": "contains", "value": "x", "flags": "i"},  # unknown field
        {"type": "amount"},
        {"type": "amount", "min": "5", "max": "1"},
        {"type": "amount", "min": 1.5},  # floats refused
        {"type": "amount", "min": "-1", "absolute": True},
        {"type": "date", "date_from": "2026-02-01", "date_to": "2026-01-01"},
        {"type": "account", "account_ids": []},
        {"type": "account", "account_ids": [str(ACCOUNT), str(ACCOUNT)]},
        {"type": "currency", "currencies": ["XYZ"]},
        {"type": "any", "conditions": []},
        {"type": "unknown"},
        {"conditions": []},
    ],
)
def test_invalid_conditions_are_refused(conditions: Any) -> None:
    with pytest.raises(ValidationError):
        _body(conditions)


@pytest.mark.parametrize(
    "actions",
    [
        [],
        [{"type": "set_note", "note": "a"}, {"type": "set_note", "note": "b"}],
        [
            {"type": "set_category", "category_id": str(CATS[0])},
            {
                "type": "split_amount",
                "lines": [{"amount": "1"}, {"category_id": None}],
            },
        ],
        [{"type": "split_percent", "lines": [{"percent": "50"}, {"percent": "49.99"}]}],
        [{"type": "split_percent", "lines": [{"percent": "100"}]}],
        [{"type": "split_percent", "lines": [{"percent": "0"}, {"percent": "100"}]}],
        [{"type": "split_amount", "lines": [{}, {}]}],
        [{"type": "split_amount", "lines": [{"amount": "-1"}, {}]}],
        [{"type": "add_tags", "tag_ids": []}],
        [{"type": "delete_transaction"}],
    ],
)
def test_invalid_actions_are_refused(actions: Any) -> None:
    with pytest.raises(ValidationError):
        _body({"type": "all", "conditions": []}, actions)


def test_condition_trees_are_bounded_before_recursive_validation() -> None:
    node: dict[str, Any] = {"type": "sign", "sign": "inflow"}
    for _ in range(CONDITION_DEPTH_MAX):
        node = {"type": "not", "condition": node}
    with pytest.raises(ValidationError, match="nest at most"):
        _body(node)
    hostile: dict[str, Any] = {"type": "sign", "sign": "inflow"}
    for _ in range(50_000):  # far deeper than any recursive validator could take
        hostile = {"type": "not", "condition": hostile}
    with pytest.raises(ValidationError, match="nest at most"):
        _body(hostile)
    wide = {"type": "all", "conditions": [{"type": "category_unset"}] * 51}
    with pytest.raises(ValidationError):
        _body(wide)


def test_documents_round_trip_through_json() -> None:
    body = RuleCreate.model_validate(
        {
            "name": "  Coffee  ",
            "conditions": {
                "type": "all",
                "conditions": [
                    {"type": "payee", "op": "glob", "value": "corner*"},
                    {"type": "amount", "min": "-20.5", "absolute": False},
                ],
            },
            "actions": [
                {"type": "split_percent", "lines": [{"percent": "60"}, {"percent": "40"}]},
                {"type": "add_tags", "tag_ids": [str(TAGS[0])]},
            ],
        }
    )
    assert body.name == "Coffee"
    stored_conditions = body.conditions.model_dump(mode="json")
    stored_actions = [a.model_dump(mode="json") for a in body.actions]
    compiled = engine.compile_rule(
        uuid.uuid4(), sort_order=0, conditions=stored_conditions, actions=stored_actions
    )
    assert compiled.conditions == body.conditions
    assert list(compiled.actions) == body.actions
    with pytest.raises(engine.InvalidRuleError):
        engine.compile_rule(uuid.uuid4(), sort_order=0, conditions={"all": []}, actions=[])


def test_references_and_remap() -> None:
    document = [
        {"type": "account", "account_ids": [str(ACCOUNT)]},
        [
            {"type": "set_category", "category_id": str(CATS[0])},
            {"type": "split_amount", "lines": [{"category_id": str(CATS[1]), "amount": "1"}]},
            {"type": "add_tags", "tag_ids": [str(TAGS[0])]},
            {"type": "set_payee", "payee_id": str(PAYEES[0])},
        ],
    ]
    found = engine.references(document)
    assert found == {
        "account": {ACCOUNT},
        "category": {CATS[0], CATS[1]},
        "tag": {TAGS[0]},
        "payee": {PAYEES[0]},
    }
    moved = engine.remap(document, {"category": {CATS[0]: CATS[4]}, "account": {}})
    assert engine.references(moved)["category"] == {CATS[4], CATS[1]}
    assert engine.references(document)["category"] == {CATS[0], CATS[1]}  # not mutated


# ---- glob and ReDoS ------------------------------------------------------------------

_GLOB_ALPHABET = st.sampled_from("ab*?")
_TEXT_ALPHABET = st.sampled_from("ab")


@settings(max_examples=500, deadline=None)
@given(
    pattern=st.text(_GLOB_ALPHABET, max_size=12),
    text=st.text(_TEXT_ALPHABET, max_size=12),
)
def test_glob_agrees_with_fnmatch(pattern: str, text: str) -> None:
    assert engine.glob_match(pattern, text) is fnmatch.fnmatchcase(text, pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        "*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*b",
        "*" * TEXT_VALUE_MAX,
        "a*" * (TEXT_VALUE_MAX // 2),
        "(a+)+$",  # a classic ReDoS regex is only literal text here
        "?*" * (TEXT_VALUE_MAX // 2),
    ],
)
def test_adversarial_patterns_finish_quickly(pattern: str) -> None:
    text = "a" * 1000  # the longest text the matchers look at
    hostile_rule = rule(
        PayeeCondition(op="glob", value=pattern[:TEXT_VALUE_MAX]), FlagForReviewAction()
    )
    started = time.perf_counter()
    for _ in range(20):
        engine.glob_match(pattern, text + "c")
        engine.evaluate([hostile_rule], txn(raw_payee=text, memo=text))
    elapsed = time.perf_counter() - started
    # Worst case is len(text) * len(pattern) steps (~200k): milliseconds, not
    # the seconds-to-forever of a backtracking regex engine.
    assert elapsed < 5.0


def test_the_engine_does_not_use_regular_expressions_for_matching() -> None:
    source = engine.__loader__.get_source(engine.__name__)  # type: ignore[union-attr]
    assert source is not None
    assert "import re\n" not in source
    assert "import regex" not in source


# ---- determinism (property tests) ----------------------------------------------------

_amounts = st.decimals(min_value=Decimal("-500"), max_value=Decimal("500"), places=2)
_text = st.sampled_from(["Corner Coffee", "Grocer Co", "RENT LTD", "atm 12", "", "coffee bar"])

_conditions = st.recursive(
    st.one_of(
        st.builds(
            PayeeCondition,
            op=st.sampled_from(["contains", "equals", "starts_with", "ends_with", "glob"]),
            value=st.sampled_from(["coffee", "co*", "grocer co", "?ent*", "atm"]),
        ),
        st.builds(MemoCondition, op=st.just("contains"), value=st.sampled_from(["card", "x"])),
        st.builds(SignCondition, sign=st.sampled_from(["outflow", "inflow"])),
        st.builds(
            AmountCondition,
            min=st.one_of(st.none(), st.just(Decimal("-100"))),
            max=st.just(Decimal("100")),
        ),
        st.builds(
            CategoryIsCondition,
            category_ids=st.lists(st.sampled_from(CATS), min_size=1, max_size=2, unique=True),
        ),
        st.just(CategoryUnsetCondition()),
        st.builds(
            PayeeIsCondition,
            payee_ids=st.lists(st.sampled_from(PAYEES), min_size=1, max_size=2, unique=True),
        ),
        st.builds(AccountIs, account_ids=st.just([ACCOUNT])),
    ),
    lambda children: st.one_of(
        st.builds(AllCondition, conditions=st.lists(children, max_size=3)),
        st.builds(AnyCondition, conditions=st.lists(children, min_size=1, max_size=3)),
        st.builds(NotCondition, condition=children),
    ),
    max_leaves=6,
)

_actions = st.one_of(
    st.builds(SetCategoryAction, category_id=st.sampled_from(CATS)),
    st.builds(
        AddTagsAction, tag_ids=st.lists(st.sampled_from(TAGS), min_size=1, max_size=2, unique=True)
    ),
    st.builds(SetPayeeAction, payee_id=st.sampled_from(PAYEES)),
    st.builds(SetNoteAction, note=st.sampled_from(["a", "b"])),
    st.just(FlagForReviewAction()),
    st.just(
        SplitPercentAction(
            lines=[
                SplitPercentLine(category_id=CATS[0], percent=Decimal("50")),
                SplitPercentLine(category_id=CATS[1], percent=Decimal("50")),
            ]
        )
    ),
)


@st.composite
def _rules(draw: st.DrawFn) -> list[CompiledRule]:
    count = draw(st.integers(min_value=0, max_value=6))
    out = []
    for i in range(count):
        actions = draw(st.lists(_actions, min_size=1, max_size=3, unique_by=lambda a: a.type))
        if sum(a.type in ("set_category", "split_percent") for a in actions) > 1:
            actions = [a for a in actions if a.type != "split_percent"]
        out.append(
            CompiledRule(
                id=uuid.UUID(int=draw(st.integers(min_value=1, max_value=10**9)) * 10 + i),
                sort_order=draw(st.integers(min_value=0, max_value=3)),
                conditions=draw(_conditions),
                actions=tuple(actions),
                enabled=draw(st.booleans()),
                stop_processing=draw(st.booleans()),
            )
        )
    return out


_txns = st.builds(
    txn,
    amount=_amounts,
    raw_payee=_text,
    memo=st.sampled_from(["card purchase", None]),
    payee_id=st.one_of(st.none(), st.sampled_from(PAYEES)),
    category_id=st.one_of(st.none(), st.sampled_from(CATS)),
    tag_ids=st.frozensets(st.sampled_from(TAGS), max_size=2),
    review_state=st.sampled_from(list(ReviewState)),
)


@settings(max_examples=300, deadline=None)
@given(rules=_rules(), transaction=_txns, seed=st.integers(min_value=0, max_value=2**32))
def test_evaluation_is_deterministic(
    rules: list[CompiledRule], transaction: TxnFacts, seed: int
) -> None:
    first = engine.evaluate(rules, transaction, payee_names=PAYEE_NAMES)
    shuffled = list(rules)
    random.Random(seed).shuffle(shuffled)
    assert engine.evaluate(shuffled, transaction, payee_names=PAYEE_NAMES) == first
    assert engine.evaluate(rules, transaction, payee_names=PAYEE_NAMES) == first
    assert engine.evaluate_many(shuffled, [transaction], payee_names=PAYEE_NAMES) == [first]
    # Only enabled rules match, and they are reported in evaluation order.
    order = [r.id for r in engine.order_rules(rules)]
    assert list(first.matched) == [i for i in order if i in first.matched]
    # The input is never mutated, and the outcome only adds tags.
    assert first.before == transaction
    assert first.after.tag_ids >= transaction.tag_ids
    # Splits always add up to the amount.
    if first.after.splits:
        assert sum(line.amount for line in first.after.splits) == transaction.amount
        assert first.after.category_id is None


@settings(max_examples=200, deadline=None)
@given(rules=_rules(), transaction=_txns)
def test_stored_documents_evaluate_like_the_models(
    rules: list[CompiledRule], transaction: TxnFacts
) -> None:
    """A rule survives the JSONB round trip unchanged (what the service stores)."""
    condition_adapter: TypeAdapter[Any] = TypeAdapter(Condition)
    action_adapter: TypeAdapter[Any] = TypeAdapter(list[Action])
    stored = [
        engine.compile_rule(
            r.id,
            sort_order=r.sort_order,
            conditions=condition_adapter.dump_python(r.conditions, mode="json"),
            actions=action_adapter.dump_python(list(r.actions), mode="json"),
            enabled=r.enabled,
            stop_processing=r.stop_processing,
        )
        for r in rules
    ]
    assert engine.evaluate(stored, transaction, payee_names=PAYEE_NAMES) == engine.evaluate(
        rules, transaction, payee_names=PAYEE_NAMES
    )

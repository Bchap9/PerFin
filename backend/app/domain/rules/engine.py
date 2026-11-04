# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure rule evaluation: no database, no clock, no I/O.

Input is a transaction DTO (:class:`TxnFacts`) and compiled rules
(:class:`CompiledRule`); output an :class:`Outcome` holding the state before
and after plus what matched. The service turns outcomes into writes (or, in
a dry run, into a diff).

Semantics (deterministic: the result depends only on the inputs, never on
the order the rules are passed in):

1. Only enabled rules run, in ``(sort_order, id)`` order.
2. Each rule's conditions are evaluated against the **working state**: the
   transaction as changed by the earlier matching rules of this run (a
   ``set_payee`` rule can feed a later ``payee_is`` rule; a trailing
   ``category_unset`` rule is a fallback).
3. When a rule matches, its actions apply in their listed order:

   * a field written by an earlier rule in this run is not written again
     (**first matching rule wins** per field: categorisation, payee,
     notes); the action is reported as skipped;
   * ``add_tags`` accumulates (a union); ``flag_for_review`` moves
     ``review_state`` from ``none`` to ``pending`` (a confirmed or already
     pending transaction is left as it is);
   * fields in ``protect`` (what a person just entered by hand) are never
     written; the action is reported as skipped.

4. A matching rule with ``stop_processing`` ends the run (first-match mode
   for that rule); otherwise later rules are evaluated too (all-matches).

Text matching folds both sides (NFKC, case folding, whitespace runs to one
space, trimmed). ``glob`` supports ``*`` and ``?`` only and is matched by
:func:`glob_match`, a two-pointer matcher with a single backtrack point:
``O(len(text) * len(pattern))`` worst case, no recursion, no regular
expressions, so no input can trigger catastrophic backtracking.
"""

from __future__ import annotations

import enum
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, localcontext
from functools import lru_cache
from typing import Any, Final

from pydantic import TypeAdapter, ValidationError

from app.core.money import calc_context, round_to_minor, sum_amounts
from app.domain.rules.schemas import (
    AccountCondition,
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
    SetCategoryAction,
    SetNoteAction,
    SetPayeeAction,
    SignCondition,
    SplitAmountAction,
    SplitPercentAction,
    TextOp,
    check_actions,
    check_condition_shape,
)
from app.domain.transactions.models import ReviewState

#: Longest text the matchers look at (longer free text is cut, never scanned whole).
MATCH_TEXT_MAX: Final = 1000


class RuleField(enum.StrEnum):
    """What an action writes; also the names accepted in ``protect``."""

    CATEGORIZATION = "categorization"
    PAYEE = "payee"
    NOTES = "notes"
    TAGS = "tags"
    REVIEW_STATE = "review_state"


@dataclass(frozen=True)
class SplitLine:
    category_id: uuid.UUID | None
    amount: Decimal
    memo: str | None = None


@dataclass(frozen=True)
class TxnFacts:
    """Everything the engine may look at or change about one transaction."""

    id: uuid.UUID
    account_id: uuid.UUID
    posted_date: date
    amount: Decimal
    currency: str
    raw_payee: str | None = None
    memo: str | None = None
    notes: str | None = None
    payee_id: uuid.UUID | None = None
    payee_name: str | None = None
    category_id: uuid.UUID | None = None
    splits: tuple[SplitLine, ...] = ()
    tag_ids: frozenset[uuid.UUID] = frozenset()
    review_state: ReviewState = ReviewState.NONE


@dataclass(frozen=True)
class CompiledRule:
    id: uuid.UUID
    sort_order: int
    conditions: Condition
    actions: tuple[Action, ...]
    enabled: bool = True
    stop_processing: bool = False


@dataclass(frozen=True)
class Skipped:
    rule_id: uuid.UUID
    action: str
    reason: str


@dataclass(frozen=True)
class Outcome:
    before: TxnFacts
    after: TxnFacts
    matched: tuple[uuid.UUID, ...] = ()
    skipped: tuple[Skipped, ...] = ()

    @property
    def added_tags(self) -> frozenset[uuid.UUID]:
        return self.after.tag_ids - self.before.tag_ids

    @property
    def changed(self) -> frozenset[RuleField]:
        """The fields whose value differs between ``before`` and ``after``."""
        b, a = self.before, self.after
        out: set[RuleField] = set()
        if (b.category_id, b.splits) != (a.category_id, a.splits):
            out.add(RuleField.CATEGORIZATION)
        if b.payee_id != a.payee_id:
            out.add(RuleField.PAYEE)
        if b.notes != a.notes:
            out.add(RuleField.NOTES)
        if a.tag_ids != b.tag_ids:
            out.add(RuleField.TAGS)
        if a.review_state != b.review_state:
            out.add(RuleField.REVIEW_STATE)
        return frozenset(out)


class InvalidRuleError(ValueError):
    """A stored rule document that does not validate (it is never run)."""


_CONDITION: Final[TypeAdapter[Any]] = TypeAdapter(Condition)
_ACTIONS: Final[TypeAdapter[list[Any]]] = TypeAdapter(list[Action])


def compile_rule(
    rule_id: uuid.UUID,
    *,
    sort_order: int,
    conditions: Any,
    actions: Any,
    enabled: bool = True,
    stop_processing: bool = False,
) -> CompiledRule:
    """Validate a stored (JSON) rule document; :class:`InvalidRuleError` if it does not."""
    try:
        condition = _CONDITION.validate_python(check_condition_shape(conditions))
        parsed = check_actions(_ACTIONS.validate_python(actions))
    except (ValidationError, ValueError) as exc:
        raise InvalidRuleError(f"rule {rule_id} does not validate") from exc
    if not parsed:
        raise InvalidRuleError(f"rule {rule_id} has no actions")
    return CompiledRule(
        id=rule_id,
        sort_order=sort_order,
        conditions=condition,
        actions=tuple(parsed),
        enabled=enabled,
        stop_processing=stop_processing,
    )


def order_rules(rules: Iterable[CompiledRule]) -> list[CompiledRule]:
    """The enabled rules in evaluation order."""
    return sorted((r for r in rules if r.enabled), key=lambda r: (r.sort_order, r.id))


# ---------------------------------------------------------------------------
# Text matching
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8192)
def fold(text: str) -> str:
    """Comparison form: NFKC, case folded, whitespace collapsed, trimmed."""
    normal = unicodedata.normalize("NFKC", text[:MATCH_TEXT_MAX]).casefold()
    return " ".join(normal.split())


def glob_match(pattern: str, text: str) -> bool:
    """``*`` = any run, ``?`` = one character, everything else literal.

    Iterative with one backtrack point (the last ``*``): every character
    of ``text`` is revisited at most once per ``*``, so the work is bounded
    by ``len(text) * len(pattern)``.
    """
    p = t = 0
    star = -1
    mark = 0
    n, m = len(text), len(pattern)
    while t < n:
        if p < m and (pattern[p] == "?" or pattern[p] == text[t]):
            p += 1
            t += 1
        elif p < m and pattern[p] == "*":
            star = p
            mark = t
            p += 1
        elif star != -1:
            p = star + 1
            mark += 1
            t = mark
        else:
            return False
    while p < m and pattern[p] == "*":
        p += 1
    return p == m


def text_matches(op: TextOp, value: str, text: str | None) -> bool:
    if text is None:
        return False
    subject = fold(text)
    wanted = fold(value)
    if op == "contains":
        return wanted in subject
    if op == "equals":
        return subject == wanted
    if op == "starts_with":
        return subject.startswith(wanted)
    if op == "ends_with":
        return subject.endswith(wanted)
    return glob_match(wanted, subject)


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def matches(condition: Condition, txn: TxnFacts) -> bool:
    """Whether ``txn`` satisfies ``condition`` (depth is bounded by the schema)."""
    match condition:
        case AllCondition():
            return all(matches(child, txn) for child in condition.conditions)
        case AnyCondition():
            return any(matches(child, txn) for child in condition.conditions)
        case NotCondition():
            return not matches(condition.condition, txn)
        case PayeeCondition():
            return text_matches(condition.op, condition.value, txn.raw_payee) or text_matches(
                condition.op, condition.value, txn.payee_name
            )
        case MemoCondition():
            return text_matches(condition.op, condition.value, txn.memo)
        case PayeeIsCondition():
            return txn.payee_id is not None and txn.payee_id in condition.payee_ids
        case AmountCondition():
            value = abs(txn.amount) if condition.absolute else txn.amount
            if condition.min is not None and value < condition.min:
                return False
            return condition.max is None or value <= condition.max
        case SignCondition():
            return txn.amount < 0 if condition.sign == "outflow" else txn.amount > 0
        case AccountCondition():
            return txn.account_id in condition.account_ids
        case CurrencyCondition():
            return txn.currency in condition.currencies
        case DateCondition():
            if condition.date_from is not None and txn.posted_date < condition.date_from:
                return False
            return condition.date_to is None or txn.posted_date <= condition.date_to
        case CategoryIsCondition():
            wanted = set(condition.category_ids)
            if txn.category_id in wanted:
                return True
            return any(line.category_id in wanted for line in txn.splits)
        case CategoryUnsetCondition():
            return txn.category_id is None and not txn.splits
    raise AssertionError(f"unhandled condition {condition!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------


def split_percent(action: SplitPercentAction, txn: TxnFacts) -> tuple[SplitLine, ...] | str:
    """Lines rounded to the currency's minor unit; the last line takes the rest."""
    if txn.amount == 0:
        return "a zero amount cannot be split"
    lines: list[SplitLine] = []
    with localcontext(calc_context()):
        for line in action.lines[:-1]:
            share = round_to_minor(txn.amount * line.percent / Decimal(100), txn.currency)
            lines.append(SplitLine(line.category_id, share, line.memo))
        rest = txn.amount - sum_amounts(line.amount for line in lines)
    last = action.lines[-1]
    lines.append(SplitLine(last.category_id, rest, last.memo))
    if any(line.amount == 0 or (line.amount < 0) != (txn.amount < 0) for line in lines):
        return "the amount is too small to split by these percentages"
    return tuple(lines)


def split_amount(action: SplitAmountAction, txn: TxnFacts) -> tuple[SplitLine, ...] | str:
    """Fixed magnitudes with the transaction's sign; one optional remainder line."""
    if txn.amount == 0:
        return "a zero amount cannot be split"
    sign = Decimal(-1) if txn.amount < 0 else Decimal(1)
    magnitude = abs(txn.amount)
    fixed = sum_amounts(line.amount for line in action.lines if line.amount is not None)
    has_rest = any(line.amount is None for line in action.lines)
    if has_rest and fixed >= magnitude:
        return "the fixed amounts leave nothing for the remainder line"
    if not has_rest and fixed != magnitude:
        return "the amounts do not add up to the transaction amount"
    rest = magnitude - fixed
    return tuple(
        SplitLine(
            line.category_id,
            sign * (line.amount if line.amount is not None else rest),
            line.memo,
        )
        for line in action.lines
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class _Run:
    state: TxnFacts
    protect: frozenset[RuleField]
    payee_names: Mapping[uuid.UUID, str]
    assigned: dict[RuleField, uuid.UUID] = field(default_factory=dict)
    skipped: list[Skipped] = field(default_factory=list)

    def claim(self, rule: CompiledRule, action: Action, what: RuleField) -> bool:
        if what in self.protect:
            self.skipped.append(Skipped(rule.id, action.type, f"{what} was set by hand"))
            return False
        earlier = self.assigned.get(what)
        if earlier is not None:
            self.skipped.append(
                Skipped(rule.id, action.type, f"{what} already set by rule {earlier}")
            )
            return False
        return True

    def apply(self, rule: CompiledRule, action: Action) -> None:
        state = self.state
        match action:
            case SetCategoryAction():
                if self.claim(rule, action, RuleField.CATEGORIZATION):
                    self.state = replace(state, category_id=action.category_id, splits=())
                    self.assigned[RuleField.CATEGORIZATION] = rule.id
            case SplitPercentAction() | SplitAmountAction():
                if not self.claim(rule, action, RuleField.CATEGORIZATION):
                    return
                lines = (
                    split_percent(action, state)
                    if isinstance(action, SplitPercentAction)
                    else split_amount(action, state)
                )
                if isinstance(lines, str):
                    self.skipped.append(Skipped(rule.id, action.type, lines))
                    return
                self.state = replace(state, category_id=None, splits=lines)
                self.assigned[RuleField.CATEGORIZATION] = rule.id
            case SetPayeeAction():
                if self.claim(rule, action, RuleField.PAYEE):
                    self.state = replace(
                        state,
                        payee_id=action.payee_id,
                        payee_name=self.payee_names.get(action.payee_id),
                    )
                    self.assigned[RuleField.PAYEE] = rule.id
            case SetNoteAction():
                if self.claim(rule, action, RuleField.NOTES):
                    self.state = replace(state, notes=action.note)
                    self.assigned[RuleField.NOTES] = rule.id
            case AddTagsAction():
                if RuleField.TAGS in self.protect:
                    self.skipped.append(Skipped(rule.id, action.type, "tags were set by hand"))
                    return
                self.state = replace(state, tag_ids=state.tag_ids | frozenset(action.tag_ids))
            case FlagForReviewAction():
                if RuleField.REVIEW_STATE in self.protect:
                    self.skipped.append(
                        Skipped(rule.id, action.type, "review_state was set by hand")
                    )
                    return
                if state.review_state is ReviewState.NONE:
                    self.state = replace(state, review_state=ReviewState.PENDING)


def evaluate(
    rules: Iterable[CompiledRule],
    txn: TxnFacts,
    *,
    payee_names: Mapping[uuid.UUID, str] | None = None,
    protect: Iterable[RuleField] = (),
    ordered: bool = False,
) -> Outcome:
    """Run ``rules`` over ``txn`` (see the module doc for the semantics).

    ``payee_names`` maps the payees ``set_payee`` may assign to their names,
    so a later ``payee`` condition sees the new name. ``ordered=True`` means
    ``rules`` already went through :func:`order_rules` (batch callers).
    """
    run = _Run(state=txn, protect=frozenset(protect), payee_names=payee_names or {})
    matched: list[uuid.UUID] = []
    for rule in rules if ordered else order_rules(rules):
        if not matches(rule.conditions, run.state):
            continue
        matched.append(rule.id)
        for action in rule.actions:
            run.apply(rule, action)
        if rule.stop_processing:
            break
    return Outcome(before=txn, after=run.state, matched=tuple(matched), skipped=tuple(run.skipped))


def evaluate_many(
    rules: Iterable[CompiledRule],
    txns: Sequence[TxnFacts],
    *,
    payee_names: Mapping[uuid.UUID, str] | None = None,
    protect: Iterable[RuleField] = (),
) -> list[Outcome]:
    """:func:`evaluate` for each transaction (each one independently)."""
    ordered = order_rules(rules)
    guarded = frozenset(protect)
    return [
        evaluate(ordered, txn, payee_names=payee_names, protect=guarded, ordered=True)
        for txn in txns
    ]


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

#: Keys of the JSON rule document that hold ids, and the kind of object they name.
REFERENCE_KEYS: Final[Mapping[str, str]] = {
    "category_id": "category",
    "category_ids": "category",
    "payee_id": "payee",
    "payee_ids": "payee",
    "tag_ids": "tag",
    "account_ids": "account",
}


def references(document: Any) -> dict[str, set[uuid.UUID]]:
    """Ids named by a JSON rule document (conditions and/or actions), by kind."""
    found: dict[str, set[uuid.UUID]] = {kind: set() for kind in set(REFERENCE_KEYS.values())}
    stack: list[Any] = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            for key, value in node.items():
                kind = REFERENCE_KEYS.get(key)
                if kind is None:
                    stack.append(value)
                    continue
                for item in value if isinstance(value, list) else [value]:
                    if item is not None:
                        found[kind].add(uuid.UUID(str(item)))
    return found


def remap(document: Any, mapping: Mapping[str, Mapping[uuid.UUID, uuid.UUID]]) -> Any:
    """A copy of a JSON rule document with ids replaced per kind (unmapped ids kept)."""
    if isinstance(document, list):
        return [remap(item, mapping) for item in document]
    if not isinstance(document, dict):
        return document
    out: dict[str, Any] = {}
    for key, value in document.items():
        kind = REFERENCE_KEYS.get(key)
        if kind is None:
            out[key] = remap(value, mapping)
            continue
        table = mapping.get(kind, {})

        def swap(item: Any, table: Mapping[uuid.UUID, uuid.UUID] = table) -> Any:
            if item is None:
                return None
            ident = uuid.UUID(str(item))
            return str(table.get(ident, ident))

        out[key] = [swap(v) for v in value] if isinstance(value, list) else swap(value)
    return out

# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rule document and the ``/rules`` API models.

A rule is ``{name, enabled, stop_processing, conditions, actions}``. The
``rule.conditions`` and ``rule.actions`` JSONB columns hold exactly the JSON
form of :data:`Condition` and ``list[Action]`` below (``model_dump(mode=
"json")``); every write goes through these models, and every read validates
again (a stored document that no longer validates is skipped, never run).

Conditions (a tree; ``type`` is the discriminator):

* ``payee`` / ``memo``: ``{"op": "contains"|"equals"|"starts_with"|
  "ends_with"|"glob", "value": "..."}``. Text is compared after NFKC
  normalisation, case folding and collapsing whitespace. ``payee`` matches
  when the statement payee text *or* the canonical payee's name matches.
  ``glob`` is the only pattern language: ``*`` matches any run of
  characters and ``?`` exactly one; everything else is literal. There are
  deliberately **no regular expressions** (the ``regex`` module with a
  timeout is not a dependency, and ``re`` can backtrack exponentially): the
  glob matcher in :mod:`app.domain.rules.engine` runs in
  ``O(len(text) * len(pattern))`` with both bounded, so a pattern cannot
  cause catastrophic backtracking (ReDoS).
* ``payee_is``: the canonical payee is one of ``payee_ids``.
* ``amount``: ``min`` and/or ``max`` (inclusive, signed, decimal strings;
  ``absolute: true`` compares the absolute amount, both bounds then
  non-negative). Amounts are in the transaction's own currency; combine with
  ``currency`` when that matters.
* ``sign``: ``outflow`` (negative amount) or ``inflow`` (positive).
* ``account``: the account is one of ``account_ids``.
* ``currency``: the currency is one of ``currencies``.
* ``date``: ``date_from`` / ``date_to`` (inclusive) on the posted date.
* ``category_is``: the category (or the category of any split) is one of
  ``category_ids``; ``category_unset``: no category and no splits.
* Combinators ``all`` (every child; an empty list matches everything),
  ``any`` (at least one child) and ``not``. At most
  :data:`CONDITION_DEPTH_MAX` levels and :data:`CONDITION_NODES_MAX` nodes.

Actions (at most one of each type per rule):

* ``set_category``; ``split_percent`` (lines with ``percent`` adding up to
  exactly 100) and ``split_amount`` (lines with a positive ``amount``,
  applied with the transaction's sign; at most one line without an amount
  takes the remainder). ``set_category`` and the split actions exclude each
  other: they write the same thing (the categorisation).
* ``add_tags``; ``set_payee`` (rename to a canonical payee);
  ``flag_for_review`` (``review_state`` ``none`` -> ``pending``); ``set_note``
  (replaces ``notes``).

Evaluation semantics are in :mod:`app.domain.rules.engine`.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.core.money import CurrencyCode, Money, parse_amount
from app.domain.transactions.models import MEMO_MAX, NAME_MAX, NOTES_MAX, ReviewState
from app.domain.transactions.schemas import MAX_LIMIT, TransactionOut

#: Most rules per household (each transaction is evaluated against all of them).
RULES_MAX: Final = 500
#: Longest text value of a ``payee`` / ``memo`` condition.
TEXT_VALUE_MAX: Final = 200
#: Most ids (or currencies) in one condition or action.
IDS_MAX: Final = 50
#: Condition tree bounds.
CONDITION_NODES_MAX: Final = 50
CONDITION_DEPTH_MAX: Final = 5
#: Most actions per rule, most lines per split action.
ACTIONS_MAX: Final = 10
SPLIT_LINES_MAX: Final = 20
#: Dry-run page size.
TEST_DEFAULT_LIMIT: Final = 100
TEST_MAX_LIMIT: Final = MAX_LIMIT
#: Export format marker and version.
EXPORT_FORMAT: Final = "perfin-rules"
EXPORT_VERSION: Final = 1
#: Most names per reference kind in an import document.
REFERENCES_MAX: Final = 2000

#: Job type of a re-run of the rules over existing transactions (ARCHITECTURE.md).
RUN_RULES_JOB_TYPE: Final = "run_rules"
#: Stand-in for an account the reader cannot see (conditions shown to members/viewers).
HIDDEN_ACCOUNT: Final = uuid.UUID(int=0)
#: Id of an unsaved rule in a dry run.
DRAFT_RULE_ID: Final = uuid.UUID(int=0)

# C0/C1 control characters other than tab/newline, plus bidi overrides.
_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def _clean_line(value: str) -> str:
    if _CONTROL_RE.search(value) or "\n" in value or "\r" in value:
        raise ValueError("must be a single line without control characters")
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


def _clean_text(value: str) -> str:
    if _CONTROL_RE.search(value):
        raise ValueError("must not contain control characters")
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


def _unique[T](values: list[T]) -> list[T]:
    if len(set(values)) != len(values):
        raise ValueError("values must be unique")
    return values


RuleName = Annotated[str, Field(min_length=1, max_length=NAME_MAX), AfterValidator(_clean_line)]
TextValue = Annotated[
    str, Field(min_length=1, max_length=TEXT_VALUE_MAX), AfterValidator(_clean_line)
]
NoteText = Annotated[str, Field(min_length=1, max_length=NOTES_MAX), AfterValidator(_clean_text)]
SplitMemo = Annotated[str, Field(min_length=1, max_length=MEMO_MAX), AfterValidator(_clean_text)]
Ids = Annotated[list[uuid.UUID], Field(min_length=1, max_length=IDS_MAX), AfterValidator(_unique)]
Currencies = Annotated[
    list[CurrencyCode], Field(min_length=1, max_length=IDS_MAX), AfterValidator(_unique)
]


def _decimal_text(value: object, what: str) -> str:
    """A decimal string from JSON (a string) or from code (a ``Decimal``); never a float."""
    if isinstance(value, Decimal) and value.is_finite():
        return f"{value:f}"
    if isinstance(value, str):
        return value.strip()
    raise ValueError(f'{what} must be a decimal string such as "12.5"')


def _rule_amount(value: object) -> Decimal:
    return parse_amount(_decimal_text(value, "amount"))


#: An amount in a rule document: a decimal string (a ``Decimal`` from code).
RuleAmount = Annotated[Decimal, BeforeValidator(_rule_amount)]


def _percent(value: object) -> Decimal:
    raw = _decimal_text(value, "percent")
    if not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,4})?", raw):
        raise ValueError("percent must be between 0 and 100 with at most 4 decimals")
    number = Decimal(raw)
    if not Decimal(0) < number <= Decimal(100):
        raise ValueError("percent must be more than 0 and at most 100")
    return number


Percent = Annotated[Decimal, BeforeValidator(_percent)]

TextOp = Literal["contains", "equals", "starts_with", "ends_with", "glob"]


class _Doc(BaseModel):
    """A node of the rule document: unknown fields refused, immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


class PayeeCondition(_Doc):
    type: Literal["payee"] = "payee"
    op: TextOp
    value: TextValue


class MemoCondition(_Doc):
    type: Literal["memo"] = "memo"
    op: TextOp
    value: TextValue


class PayeeIsCondition(_Doc):
    type: Literal["payee_is"] = "payee_is"
    payee_ids: Ids


class AmountCondition(_Doc):
    type: Literal["amount"] = "amount"
    min: RuleAmount | None = None
    max: RuleAmount | None = None
    absolute: bool = False

    @model_validator(mode="after")
    def _bounds(self) -> Self:
        if self.min is None and self.max is None:
            raise ValueError("give min, max or both")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must not exceed max")
        if self.absolute and any(b is not None and b < 0 for b in (self.min, self.max)):
            raise ValueError("absolute bounds must not be negative")
        return self


class SignCondition(_Doc):
    type: Literal["sign"] = "sign"
    sign: Literal["outflow", "inflow"]


class AccountCondition(_Doc):
    type: Literal["account"] = "account"
    account_ids: Ids


class CurrencyCondition(_Doc):
    type: Literal["currency"] = "currency"
    currencies: Currencies


class DateCondition(_Doc):
    type: Literal["date"] = "date"
    date_from: date | None = None
    date_to: date | None = None

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.date_from is None and self.date_to is None:
            raise ValueError("give date_from, date_to or both")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")
        return self


class CategoryIsCondition(_Doc):
    type: Literal["category_is"] = "category_is"
    category_ids: Ids


class CategoryUnsetCondition(_Doc):
    type: Literal["category_unset"] = "category_unset"


class AllCondition(_Doc):
    type: Literal["all"] = "all"
    conditions: list[Condition] = Field(default_factory=list, max_length=CONDITION_NODES_MAX)


class AnyCondition(_Doc):
    type: Literal["any"] = "any"
    conditions: list[Condition] = Field(min_length=1, max_length=CONDITION_NODES_MAX)


class NotCondition(_Doc):
    type: Literal["not"] = "not"
    condition: Condition


Condition = Annotated[
    PayeeCondition
    | MemoCondition
    | PayeeIsCondition
    | AmountCondition
    | SignCondition
    | AccountCondition
    | CurrencyCondition
    | DateCondition
    | CategoryIsCondition
    | CategoryUnsetCondition
    | AllCondition
    | AnyCondition
    | NotCondition,
    Field(discriminator="type"),
]

AllCondition.model_rebuild()
AnyCondition.model_rebuild()
NotCondition.model_rebuild()


def check_condition_shape(raw: object) -> object:
    """Refuse trees deeper or larger than the limits *before* recursive validation.

    Walks the raw JSON iteratively (no recursion), so a hostile, deeply
    nested body is refused without exercising the recursive validators.
    """
    stack: list[tuple[object, int]] = [(raw, 1)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, dict):
            continue
        nodes += 1
        if nodes > CONDITION_NODES_MAX:
            raise ValueError(f"at most {CONDITION_NODES_MAX} conditions")
        if depth > CONDITION_DEPTH_MAX:
            raise ValueError(f"conditions nest at most {CONDITION_DEPTH_MAX} levels")
        children = node.get("conditions")
        if isinstance(children, list):
            if len(children) > CONDITION_NODES_MAX:
                raise ValueError(f"at most {CONDITION_NODES_MAX} conditions")
            stack.extend((child, depth + 1) for child in children)
        child = node.get("condition")
        if child is not None:
            stack.append((child, depth + 1))
    return raw


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class SetCategoryAction(_Doc):
    type: Literal["set_category"] = "set_category"
    category_id: uuid.UUID


class AddTagsAction(_Doc):
    type: Literal["add_tags"] = "add_tags"
    tag_ids: Ids


class SetPayeeAction(_Doc):
    type: Literal["set_payee"] = "set_payee"
    payee_id: uuid.UUID


class SplitPercentLine(_Doc):
    category_id: uuid.UUID | None = None
    percent: Percent
    memo: SplitMemo | None = None


class SplitPercentAction(_Doc):
    type: Literal["split_percent"] = "split_percent"
    lines: list[SplitPercentLine] = Field(min_length=2, max_length=SPLIT_LINES_MAX)

    @model_validator(mode="after")
    def _hundred(self) -> Self:
        if sum((line.percent for line in self.lines), Decimal(0)) != Decimal(100):
            raise ValueError("percentages must add up to exactly 100")
        return self


class SplitAmountLine(_Doc):
    """``amount`` is a positive magnitude; ``null`` takes the remainder."""

    category_id: uuid.UUID | None = None
    amount: RuleAmount | None = None
    memo: SplitMemo | None = None

    @field_validator("amount")
    @classmethod
    def _positive(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("split amounts are positive (the transaction's sign applies)")
        return value


class SplitAmountAction(_Doc):
    type: Literal["split_amount"] = "split_amount"
    lines: list[SplitAmountLine] = Field(min_length=2, max_length=SPLIT_LINES_MAX)

    @model_validator(mode="after")
    def _one_remainder(self) -> Self:
        if sum(1 for line in self.lines if line.amount is None) > 1:
            raise ValueError("at most one line may take the remainder")
        return self


class FlagForReviewAction(_Doc):
    type: Literal["flag_for_review"] = "flag_for_review"


class SetNoteAction(_Doc):
    type: Literal["set_note"] = "set_note"
    note: NoteText


Action = Annotated[
    SetCategoryAction
    | AddTagsAction
    | SetPayeeAction
    | SplitPercentAction
    | SplitAmountAction
    | FlagForReviewAction
    | SetNoteAction,
    Field(discriminator="type"),
]

_CATEGORISING: Final = frozenset({"set_category", "split_percent", "split_amount"})


def check_actions(actions: list[Any]) -> list[Any]:
    kinds = [action.type for action in actions]
    if len(set(kinds)) != len(kinds):
        raise ValueError("each action type may appear once per rule")
    if len(_CATEGORISING.intersection(kinds)) > 1:
        raise ValueError("set_category, split_percent and split_amount exclude each other")
    return actions


Actions = Annotated[
    list[Action],
    Field(min_length=1, max_length=ACTIONS_MAX),
    AfterValidator(check_actions),
]
ConditionIn = Annotated[Condition, BeforeValidator(check_condition_shape)]


# ---------------------------------------------------------------------------
# API inputs
# ---------------------------------------------------------------------------


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuleBody(_In):
    """What a rule does: shared by create, import and the inline dry run."""

    conditions: ConditionIn
    actions: Actions
    stop_processing: bool = False


class RuleCreate(RuleBody):
    """A new rule; it is appended after the existing ones (reorder to move it)."""

    name: RuleName
    enabled: bool = True


class RuleUpdate(_In):
    """Change any of the fields; ``null`` is not a value for any of them."""

    name: RuleName | None = None
    enabled: bool | None = None
    stop_processing: bool | None = None
    conditions: ConditionIn | None = None
    actions: Actions | None = None

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class RuleReorder(_In):
    """Every rule id of the household, in the new evaluation order."""

    rule_ids: Annotated[
        list[uuid.UUID], Field(min_length=1, max_length=RULES_MAX), AfterValidator(_unique)
    ]


class _Scope(_In):
    """Which transactions: accounts and posted-date range (all when omitted)."""

    account_ids: Ids | None = None
    date_from: date | None = None
    date_to: date | None = None

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")
        return self


class RuleTest(_Scope):
    """Dry run against history; nothing is written.

    ``rule_id`` tests one saved rule on its own (also a disabled one),
    ``rule`` an unsaved one; neither runs the household's enabled rules in
    order, exactly as ``POST /rules/apply`` would.
    """

    rule_id: uuid.UUID | None = None
    rule: RuleBody | None = None
    only_changes: bool = False
    cursor: Annotated[str | None, Field(max_length=128)] = None
    limit: int = Field(default=TEST_DEFAULT_LIMIT, ge=1, le=TEST_MAX_LIMIT)

    @model_validator(mode="after")
    def _one_rule(self) -> Self:
        if self.rule_id is not None and self.rule is not None:
            raise ValueError("give rule_id or rule, not both")
        return self


class RuleApply(_Scope):
    """Re-run the enabled rules (or the enabled ones among ``rule_ids``) as a job."""

    rule_ids: Ids | None = None


class RunRulesJob(_Scope):
    """Payload of :data:`RUN_RULES_JOB_TYPE` (ids and dates only)."""

    rule_ids: Ids | None = None
    requested_by: uuid.UUID | None = None


class Reference(_In):
    """The name of a referenced object, so an export can be imported elsewhere.

    Categories are named by their path (``"Food / Groceries"``).
    """

    id: uuid.UUID
    name: Annotated[str, Field(min_length=1, max_length=4 * NAME_MAX)]


class References(_In):
    categories: list[Reference] = Field(default_factory=list, max_length=REFERENCES_MAX)
    payees: list[Reference] = Field(default_factory=list, max_length=REFERENCES_MAX)
    tags: list[Reference] = Field(default_factory=list, max_length=REFERENCES_MAX)
    accounts: list[Reference] = Field(default_factory=list, max_length=REFERENCES_MAX)


class RuleExportItem(RuleCreate):
    """One rule of an export document (no ids, no statistics)."""


class RuleExport(_In):
    """``GET /rules/export``; also the body of ``POST /rules/import``."""

    format: Literal["perfin-rules"] = EXPORT_FORMAT
    version: Literal[1] = EXPORT_VERSION
    rules: list[RuleExportItem] = Field(max_length=RULES_MAX)
    references: References = Field(default_factory=References)


class RuleImport(RuleExport):
    """An export document plus how to combine it with the existing rules.

    ``append`` adds the rules after the existing ones; ``replace`` deletes
    the existing rules first. Every id is kept when it names an object of
    this household, else resolved by its name in ``references`` (``422
    rule-reference-unresolved`` when that fails).
    """

    mode: Literal["append", "replace"] = "append"


# ---------------------------------------------------------------------------
# API outputs
# ---------------------------------------------------------------------------


class RuleOut(BaseModel):
    """A rule. ``redacted``: accounts the reader cannot see show as the nil UUID."""

    id: uuid.UUID
    name: str
    sort_order: int
    enabled: bool
    stop_processing: bool
    conditions: Condition
    actions: list[Action]
    hit_count: int
    last_hit_at: datetime | None
    created_at: datetime
    updated_at: datetime
    redacted: bool = False


class RuleList(BaseModel):
    """All rules of the household in evaluation order (at most :data:`RULES_MAX`)."""

    items: list[RuleOut]


class SplitPreview(BaseModel):
    category_id: uuid.UUID | None
    amount: Money
    memo: str | None


class IdChange(BaseModel):
    before: uuid.UUID | None
    after: uuid.UUID | None


class TextChange(BaseModel):
    before: str | None
    after: str | None


class ReviewChange(BaseModel):
    before: ReviewState
    after: ReviewState


class SplitChange(BaseModel):
    before: list[SplitPreview]
    after: list[SplitPreview]


class RuleDiff(BaseModel):
    """What applying the rules would change (fields that stay the same are ``null``)."""

    category_id: IdChange | None = None
    splits: SplitChange | None = None
    payee_id: IdChange | None = None
    notes: TextChange | None = None
    review_state: ReviewChange | None = None
    added_tag_ids: list[uuid.UUID] = Field(default_factory=list)


class SkippedAction(BaseModel):
    rule_id: uuid.UUID
    action: str
    reason: str


class RuleTestItem(BaseModel):
    transaction: TransactionOut
    matched_rule_ids: list[uuid.UUID]
    changes: RuleDiff
    skipped: list[SkippedAction]


class RuleTestResult(BaseModel):
    """One page of matches; continue with ``next_cursor`` (``null``: history exhausted).

    ``scanned`` counts the transactions examined for this page (at most
    ``app.domain.rules.service.TEST_SCAN_MAX``), ``matched`` the items.
    """

    items: list[RuleTestItem]
    next_cursor: str | None
    scanned: int
    matched: int


class JobAccepted(BaseModel):
    job_id: uuid.UUID
    state: str

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rules: CRUD, reorder, dry run, apply (job), export/import, and the new-transaction hook.

Authorisation (AUTH.md, SECURITY.md F3/I3). Rules are household-wide and
rewrite transactions on **every** account, including accounts a member or
viewer cannot see or edit, so:

* reading rules (``GET /rules``, export) needs any household role; for a
  reader who does not see every account, account ids in conditions that
  are hidden from them show as :data:`~app.domain.rules.schemas.HIDDEN_ACCOUNT`
  (``redacted: true``);
* creating, changing, deleting, reordering, importing and applying rules
  needs owner or admin (who see and edit every account);
* the dry run (``POST /rules/test``) needs any household role and only
  ever looks at transactions on accounts the caller can see.

The dry run never writes: it switches its database transaction to ``READ
ONLY`` before reading anything, so PostgreSQL itself refuses any write that
could slip into that code path. Call :func:`test_rules` in a transaction of
its own (the API request is one).

Ids named by a rule (categories, payees, tags, accounts) must be rows of
the household when the rule is saved (``404 <kind>-not-found``). An object
deleted later leaves a dangling id: conditions naming it simply do not
match, actions naming it are dropped when the rules run (logged).

Every write is audited (``rule.create|update|delete|reorder|import|
apply_requested|apply``; the audit trail keeps names, flags, condition and
action types and referenced ids, never the text values of conditions or
notes) and emits an event (``rule.*``; ``transaction.rules_applied`` when
rules changed transactions; ``job.queued`` for an apply).
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.logging import get_logger
from app.core.money import Money
from app.db.queue import enqueue
from app.domain.accounts.visibility import (
    require_visible_accounts,
    sees_all_accounts,
    visible_account_filter,
    visible_account_ids,
)
from app.domain.rules import engine
from app.domain.rules import repo as rules_repo
from app.domain.rules.engine import CompiledRule, Outcome, RuleField, SplitLine, TxnFacts
from app.domain.rules.schemas import (
    DRAFT_RULE_ID,
    HIDDEN_ACCOUNT,
    RULES_MAX,
    RUN_RULES_JOB_TYPE,
    IdChange,
    JobAccepted,
    Reference,
    References,
    ReviewChange,
    RuleApply,
    RuleBody,
    RuleCreate,
    RuleDiff,
    RuleExport,
    RuleExportItem,
    RuleImport,
    RuleList,
    RuleOut,
    RuleReorder,
    RuleTest,
    RuleTestItem,
    RuleTestResult,
    RuleUpdate,
    RunRulesJob,
    SetPayeeAction,
    SkippedAction,
    SplitChange,
    SplitPreview,
    TextChange,
)
from app.domain.transactions import repo as tx_repo
from app.domain.transactions.import_hash import normalize_payee
from app.domain.transactions.models import Category, Payee, Rule, Tag, Transaction
from app.domain.transactions.schemas import decode_cursor, encode_cursor
from app.domain.transactions.service import to_out

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

log = get_logger(__name__)

#: Most transactions one dry-run page examines.
TEST_SCAN_MAX: Final = 5000
#: Transactions read (and, when applying, locked) per batch.
BATCH: Final = 500
#: Transaction ids listed in one audit row or event at most.
IDS_IN_RECORD_MAX: Final = 100


# ---------------------------------------------------------------------------
# Problems and guards
# ---------------------------------------------------------------------------


def rule_not_found() -> ProblemError:
    return ProblemError(404, "rule-not-found", "Rule not found")


def _not_found(kind: str) -> ProblemError:
    return ProblemError(404, f"{kind}-not-found", f"{kind.capitalize()} not found")


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return principal.household_id


def _rule_limit() -> ProblemError:
    return ProblemError(
        409, "rule-limit", "Too many rules", f"A household can have at most {RULES_MAX} rules."
    )


# ---------------------------------------------------------------------------
# Documents, references, output
# ---------------------------------------------------------------------------


def _documents(body: RuleBody) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The JSON stored in ``rule.conditions`` / ``rule.actions``."""
    return (
        body.conditions.model_dump(mode="json"),
        [action.model_dump(mode="json") for action in body.actions],
    )


def _check_references(db: Session, household_id: uuid.UUID, *documents: Any) -> None:
    """``404 <kind>-not-found`` unless every id the documents name is a row here."""
    found = engine.references(list(documents))
    checks: tuple[tuple[str, type[Category] | type[Payee] | type[Tag] | None], ...] = (
        ("account", None),
        ("category", Category),
        ("payee", Payee),
        ("tag", Tag),
    )
    for kind, model in checks:
        ids = found.get(kind, set())
        if not ids:
            continue
        present = (
            rules_repo.existing_account_ids(db, household_id, ids)
            if model is None
            else tx_repo.existing_ids(db, model, household_id, ids)
        )
        if present != ids:
            raise _not_found(kind)


def _condition_types(document: Any) -> list[str]:
    types: list[str] = []
    stack: list[Any] = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "type" in node:
                types.append(str(node["type"]))
            stack.extend(node.get("conditions", []) or [])
            if node.get("condition") is not None:
                stack.append(node["condition"])
    return sorted(types)


def _facts(rule: Rule) -> dict[str, Any]:
    """What the audit trail keeps about a rule (no condition text, no note text)."""
    refs = engine.references([rule.conditions, rule.actions])
    return {
        "name": rule.name,
        "enabled": rule.enabled,
        "stop_processing": rule.stop_processing,
        "sort_order": rule.sort_order,
        "conditions": _condition_types(rule.conditions),
        "actions": sorted(str(a.get("type")) for a in rule.actions if isinstance(a, dict)),
        "references": {kind: sorted(str(i) for i in ids) for kind, ids in refs.items() if ids},
    }


def _record(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    *,
    action: str,
    event: str,
    target_id: uuid.UUID | None,
    before: Any = None,
    after: Any = None,
    request: HTTPConnection | None,
) -> None:
    audit(
        db,
        action=action,
        target_type="rule",
        target_id=target_id,
        before=before,
        after=after,
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(db, household_id, event, {"id": str(target_id)} if target_id else {})


def _reader_accounts(db: Session, principal: Principal) -> set[uuid.UUID] | None:
    """``None`` when the reader sees every account, else the accounts they see."""
    if sees_all_accounts(principal):
        return None
    return set(visible_account_ids(db, principal))


def _redact(document: Any, visible: set[uuid.UUID] | None) -> tuple[Any, bool]:
    """Hidden account ids in a condition document replaced by :data:`HIDDEN_ACCOUNT`."""
    if visible is None:
        return document, False
    redacted = False

    def walk(node: Any) -> Any:
        nonlocal redacted
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {key: walk(value) for key, value in node.items()}
        ids = node.get("account_ids")
        if isinstance(ids, list):
            shown: list[str] = []
            for raw in ids:
                ident = uuid.UUID(str(raw))
                if ident not in visible:
                    redacted = True
                    ident = HIDDEN_ACCOUNT
                if str(ident) not in shown:
                    shown.append(str(ident))
            out["account_ids"] = shown
        return out

    return walk(document), redacted


def rule_out(rule: Rule, visible: set[uuid.UUID] | None = None) -> RuleOut:
    conditions, redacted = _redact(rule.conditions, visible)
    return RuleOut.model_validate(
        {
            "id": rule.id,
            "name": rule.name,
            "sort_order": rule.sort_order,
            "enabled": rule.enabled,
            "stop_processing": rule.stop_processing,
            "conditions": conditions,
            "actions": rule.actions,
            "hit_count": rule.hit_count,
            "last_hit_at": rule.last_hit_at,
            "created_at": rule.created_at,
            "updated_at": rule.updated_at,
            "redacted": redacted,
        }
    )


def _get(db: Session, household_id: uuid.UUID, rule_id: uuid.UUID, *, lock: bool = False) -> Rule:
    rule = rules_repo.get_rule(db, household_id, rule_id, lock=lock)
    if rule is None:
        raise rule_not_found()
    return rule


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def list_rules(db: Session, principal: Principal) -> RuleList:
    household_id = _household(principal, HouseholdRole.VIEWER)
    visible = _reader_accounts(db, principal)
    return RuleList(items=[rule_out(r, visible) for r in rules_repo.list_rules(db, household_id)])


def get_rule(db: Session, principal: Principal, rule_id: uuid.UUID) -> RuleOut:
    household_id = _household(principal, HouseholdRole.VIEWER)
    return rule_out(_get(db, household_id, rule_id), _reader_accounts(db, principal))


def require_rule(db: Session, principal: Principal, rule_id: uuid.UUID) -> None:
    """``404 rule-not-found`` unless the rule is one of the caller's household."""
    _get(db, _household(principal, HouseholdRole.VIEWER), rule_id)


def create_rule(
    db: Session, principal: Principal, data: RuleCreate, *, request: HTTPConnection | None
) -> RuleOut:
    household_id = _household(principal, HouseholdRole.ADMIN)
    if rules_repo.count_rules(db, household_id) >= RULES_MAX:
        raise _rule_limit()
    conditions, actions = _documents(data)
    _check_references(db, household_id, conditions, actions)
    rule = Rule(
        household_id=household_id,
        name=data.name,
        sort_order=rules_repo.next_sort_order(db, household_id),
        enabled=data.enabled,
        stop_processing=data.stop_processing,
        conditions=conditions,
        actions=actions,
    )
    db.add(rule)
    db.flush()
    _record(
        db,
        principal,
        household_id,
        action="rule.create",
        event="rule.created",
        target_id=rule.id,
        after=_facts(rule),
        request=request,
    )
    return rule_out(rule)


def update_rule(
    db: Session,
    principal: Principal,
    rule_id: uuid.UUID,
    data: RuleUpdate,
    *,
    request: HTTPConnection | None,
) -> RuleOut:
    household_id = _household(principal, HouseholdRole.ADMIN)
    rule = _get(db, household_id, rule_id, lock=True)
    given = data.model_fields_set
    before = _facts(rule)
    if "conditions" in given and data.conditions is not None:
        conditions = data.conditions.model_dump(mode="json")
        _check_references(db, household_id, conditions)
        rule.conditions = conditions
    if "actions" in given and data.actions is not None:
        actions = [action.model_dump(mode="json") for action in data.actions]
        _check_references(db, household_id, actions)
        rule.actions = actions
    if "name" in given and data.name is not None:
        rule.name = data.name
    if "enabled" in given and data.enabled is not None:
        rule.enabled = data.enabled
    if "stop_processing" in given and data.stop_processing is not None:
        rule.stop_processing = data.stop_processing
    if given:
        rule.updated_at = clock.utcnow()
        db.flush()
        _record(
            db,
            principal,
            household_id,
            action="rule.update",
            event="rule.updated",
            target_id=rule.id,
            before=before,
            after=_facts(rule),
            request=request,
        )
    return rule_out(rule)


def delete_rule(
    db: Session, principal: Principal, rule_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    household_id = _household(principal, HouseholdRole.ADMIN)
    rule = _get(db, household_id, rule_id, lock=True)
    _record(
        db,
        principal,
        household_id,
        action="rule.delete",
        event="rule.deleted",
        target_id=rule.id,
        before=_facts(rule),
        request=request,
    )
    rules_repo.delete_rule(db, rule)
    db.expunge(rule)


def reorder_rules(
    db: Session, principal: Principal, data: RuleReorder, *, request: HTTPConnection | None
) -> RuleList:
    """``sort_order`` becomes each rule's index in ``data.rule_ids`` (every rule, once)."""
    household_id = _household(principal, HouseholdRole.ADMIN)
    rules = {r.id: r for r in rules_repo.list_rules(db, household_id, lock=True)}
    if set(data.rule_ids) != set(rules):
        raise ProblemError(
            422,
            "rule-order-mismatch",
            "Rule order does not match",
            "List every rule of the household exactly once.",
        )
    before = [str(r.id) for r in sorted(rules.values(), key=lambda r: (r.sort_order, r.id))]
    now = clock.utcnow()
    for index, rule_id in enumerate(data.rule_ids):
        rule = rules[rule_id]
        if rule.sort_order != index:
            rule.sort_order = index
            rule.updated_at = now
    db.flush()
    _record(
        db,
        principal,
        household_id,
        action="rule.reorder",
        event="rule.reordered",
        target_id=None,
        before={"order": before},
        after={"order": [str(i) for i in data.rule_ids]},
        request=request,
    )
    return RuleList(items=[rule_out(rules[i]) for i in data.rule_ids])


# ---------------------------------------------------------------------------
# Compiling and transaction facts
# ---------------------------------------------------------------------------


def _compile(rule: Rule, *, force_enabled: bool = False) -> CompiledRule:
    return engine.compile_rule(
        rule.id,
        sort_order=rule.sort_order,
        conditions=rule.conditions,
        actions=rule.actions,
        enabled=True if force_enabled else rule.enabled,
        stop_processing=rule.stop_processing,
    )


def compiled_rules(
    db: Session, household_id: uuid.UUID, *, rule_ids: Collection[uuid.UUID] | None = None
) -> list[CompiledRule]:
    """The household's enabled rules in evaluation order; invalid documents are skipped."""
    out: list[CompiledRule] = []
    for rule in rules_repo.list_rules(db, household_id, enabled_only=True):
        if rule_ids is not None and rule.id not in rule_ids:
            continue
        try:
            out.append(_compile(rule))
        except engine.InvalidRuleError:
            log.warning("skipping a rule that does not validate", extra={"rule_id": str(rule.id)})
    return engine.order_rules(out)


def _without_dangling(
    db: Session, household_id: uuid.UUID, rules: Sequence[CompiledRule]
) -> list[CompiledRule]:
    """Drop actions that name a category, payee or tag that no longer exists.

    (The household-consistent foreign keys would refuse such a write at
    commit; a deleted category must not wedge every later import.)
    """
    wanted: dict[str, set[uuid.UUID]] = defaultdict(set)
    per_action: list[list[dict[str, set[uuid.UUID]]]] = []
    for rule in rules:
        refs = [engine.references(a.model_dump(mode="json")) for a in rule.actions]
        per_action.append(refs)
        for ref in refs:
            for kind, ids in ref.items():
                wanted[kind] |= ids
    models: dict[str, type[Category] | type[Payee] | type[Tag]] = {
        "category": Category,
        "payee": Payee,
        "tag": Tag,
    }
    present = {
        kind: tx_repo.existing_ids(db, model, household_id, wanted.get(kind, set()))
        for kind, model in models.items()
    }
    out: list[CompiledRule] = []
    for rule, refs in zip(rules, per_action, strict=True):
        keep = tuple(
            action
            for action, ref in zip(rule.actions, refs, strict=True)
            if all(ref.get(kind, set()) <= present[kind] for kind in models)
        )
        if len(keep) != len(rule.actions):
            log.warning(
                "rule names a deleted object; those actions are skipped",
                extra={"rule_id": str(rule.id)},
            )
            rule = CompiledRule(
                id=rule.id,
                sort_order=rule.sort_order,
                conditions=rule.conditions,
                actions=keep,
                enabled=rule.enabled,
                stop_processing=rule.stop_processing,
            )
        out.append(rule)
    return out


def _payee_targets(rules: Iterable[CompiledRule]) -> set[uuid.UUID]:
    return {
        action.payee_id
        for rule in rules
        for action in rule.actions
        if isinstance(action, SetPayeeAction)
    }


def transaction_facts(
    db: Session, household_id: uuid.UUID, rows: Sequence[Transaction]
) -> list[TxnFacts]:
    """DTOs for ``rows`` (payee names, splits and tags loaded in batches)."""
    ids = [row.id for row in rows]
    tags = tx_repo.tag_ids_for(db, household_id, ids)
    splits = tx_repo.splits_for(db, household_id, ids)
    names = rules_repo.payee_names(
        db, household_id, {row.payee_id for row in rows if row.payee_id is not None}
    )
    return [
        TxnFacts(
            id=row.id,
            account_id=row.account_id,
            posted_date=row.posted_date,
            amount=row.amount,
            currency=row.currency,
            raw_payee=row.raw_payee,
            memo=row.memo,
            notes=row.notes,
            payee_id=row.payee_id,
            payee_name=names.get(row.payee_id) if row.payee_id is not None else None,
            category_id=row.category_id,
            splits=tuple(
                SplitLine(s.category_id, s.amount, s.memo) for s in splits.get(row.id, [])
            ),
            tag_ids=frozenset(tags.get(row.id, [])),
            review_state=row.review_state,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def make_read_only(db: Session) -> None:
    """Make the current database transaction refuse every write (PostgreSQL enforced)."""
    db.execute(text("SET TRANSACTION READ ONLY"))


def _test_rules_for(db: Session, household_id: uuid.UUID, data: RuleTest) -> list[CompiledRule]:
    if data.rule_id is not None:
        rule = _get(db, household_id, data.rule_id)
        try:
            return [_compile(rule, force_enabled=True)]
        except engine.InvalidRuleError:
            raise ProblemError(
                422, "rule-invalid", "Rule is invalid", "Save the rule again to repair it."
            ) from None
    if data.rule is not None:
        return [
            CompiledRule(
                id=DRAFT_RULE_ID,
                sort_order=0,
                conditions=data.rule.conditions,
                actions=tuple(data.rule.actions),
                stop_processing=data.rule.stop_processing,
            )
        ]
    return compiled_rules(db, household_id)


def _split_previews(lines: Iterable[SplitLine], currency: str) -> list[SplitPreview]:
    return [
        SplitPreview(
            category_id=line.category_id, amount=Money.of(line.amount, currency), memo=line.memo
        )
        for line in lines
    ]


def diff(outcome: Outcome) -> RuleDiff:
    """The API form of what ``outcome`` changes."""
    b, a = outcome.before, outcome.after
    changed = outcome.changed
    out = RuleDiff(added_tag_ids=sorted(outcome.added_tags))
    if RuleField.CATEGORIZATION in changed:
        if b.category_id != a.category_id:
            out.category_id = IdChange(before=b.category_id, after=a.category_id)
        if b.splits != a.splits:
            out.splits = SplitChange(
                before=_split_previews(b.splits, b.currency),
                after=_split_previews(a.splits, a.currency),
            )
    if RuleField.PAYEE in changed:
        out.payee_id = IdChange(before=b.payee_id, after=a.payee_id)
    if RuleField.NOTES in changed:
        out.notes = TextChange(before=b.notes, after=a.notes)
    if RuleField.REVIEW_STATE in changed:
        out.review_state = ReviewChange(before=b.review_state, after=a.review_state)
    return out


def test_rules(db: Session, principal: Principal, data: RuleTest) -> RuleTestResult:
    """Dry run over the caller's visible history, newest first; writes nothing.

    Scans at most :data:`TEST_SCAN_MAX` transactions per call and returns up
    to ``data.limit`` matches; ``next_cursor`` continues after the last
    transaction examined (``None`` once history is exhausted).
    """
    household_id = _household(principal, HouseholdRole.VIEWER)
    make_read_only(db)
    if data.account_ids:
        require_visible_accounts(db, principal, data.account_ids)
    rules = _test_rules_for(db, household_id, data)
    clauses = rules_repo.transaction_scope(
        household_id,
        account_ids=data.account_ids,
        date_from=data.date_from,
        date_to=data.date_to,
        extra=[visible_account_filter(principal, Transaction.account_id)],
    )
    after = decode_cursor(data.cursor) if data.cursor else None
    targets = rules_repo.payee_names(db, household_id, _payee_targets(rules))
    kept: list[tuple[Transaction, Outcome]] = []
    scanned = 0
    exhausted = False
    while scanned < TEST_SCAN_MAX:
        want = min(BATCH, TEST_SCAN_MAX - scanned)
        batch = rules_repo.transactions_after(db, clauses, after=after, limit=want)
        short = len(batch) < want  # nothing after this batch
        facts = transaction_facts(db, household_id, batch)
        outcomes = engine.evaluate_many(rules, facts, payee_names=targets)
        page_full = False
        for index, (row, outcome) in enumerate(zip(batch, outcomes, strict=True)):
            scanned += 1
            after = (row.posted_date, row.id)
            if outcome.matched and (outcome.changed or not data.only_changes):
                kept.append((row, outcome))
                if len(kept) == data.limit:
                    page_full = True
                    exhausted = short and index == len(batch) - 1
                    break
        if page_full:
            break
        if short:
            exhausted = True
            break
    next_cursor = None if exhausted or after is None else encode_cursor(*after)
    rows_out = to_out(db, principal, [row for row, _ in kept])
    items = [
        RuleTestItem(
            transaction=row_out,
            matched_rule_ids=list(outcome.matched),
            changes=diff(outcome),
            skipped=[
                SkippedAction(rule_id=s.rule_id, action=s.action, reason=s.reason)
                for s in outcome.skipped
            ],
        )
        for row_out, (_, outcome) in zip(rows_out, kept, strict=True)
    ]
    return RuleTestResult(items=items, next_cursor=next_cursor, scanned=scanned, matched=len(items))


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


@dataclass
class ApplySummary:
    """What one application of the rules did."""

    scanned: int = 0
    matched: int = 0
    changed_ids: list[uuid.UUID] = field(default_factory=list)
    hits: Counter[uuid.UUID] = field(default_factory=Counter)

    @property
    def changed(self) -> int:
        return len(self.changed_ids)

    def as_record(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "matched": self.matched,
            "changed": self.changed,
            "transaction_ids": [str(i) for i in self.changed_ids[:IDS_IN_RECORD_MAX]],
            "rule_hits": {str(k): v for k, v in sorted(self.hits.items())},
        }


def write_outcome(db: Session, row: Transaction, outcome: Outcome) -> bool:
    """Apply ``outcome`` to ``row`` (and its splits and tags); True when it changed."""
    changed = outcome.changed
    if not changed:
        return False
    after = outcome.after
    if RuleField.CATEGORIZATION in changed:
        if after.splits:
            tx_repo.replace_splits(
                db,
                row,
                [
                    {
                        "amount": line.amount,
                        "category_id": line.category_id,
                        "memo": line.memo,
                        "tax_flag": False,
                    }
                    for line in after.splits
                ],
            )
            row.category_id = None
        else:
            if outcome.before.splits:
                tx_repo.replace_splits(db, row, [])
            row.category_id = after.category_id
    if RuleField.PAYEE in changed:
        row.payee_id = after.payee_id
    if RuleField.NOTES in changed:
        row.notes = after.notes
    if RuleField.REVIEW_STATE in changed:
        row.review_state = after.review_state
    if RuleField.TAGS in changed:
        tx_repo.add_tags(db, row.household_id, [row.id], outcome.added_tags)
    row.updated_at = clock.utcnow()
    return True


def _apply_batch(
    db: Session,
    household_id: uuid.UUID,
    rules: Sequence[CompiledRule],
    rows: Sequence[Transaction],
    summary: ApplySummary,
    *,
    payee_names: Mapping[uuid.UUID, str],
    protect: Iterable[RuleField] = (),
) -> None:
    facts = transaction_facts(db, household_id, rows)
    outcomes = engine.evaluate_many(rules, facts, payee_names=payee_names, protect=protect)
    for row, outcome in zip(rows, outcomes, strict=True):
        summary.scanned += 1
        if not outcome.matched:
            continue
        summary.matched += 1
        summary.hits.update(outcome.matched)
        if write_outcome(db, row, outcome):
            summary.changed_ids.append(row.id)
    db.flush()


def _finish(
    db: Session,
    household_id: uuid.UUID,
    summary: ApplySummary,
    *,
    source: str,
    user_id: uuid.UUID | None,
    job_id: uuid.UUID | None = None,
    request: HTTPConnection | None = None,
) -> None:
    if summary.hits:
        rules_repo.add_hits(db, household_id, summary.hits, clock.utcnow())
    if not summary.matched:
        return
    after = {"source": source, **summary.as_record()}
    audit(
        db,
        action="rule.apply",
        target_type="job" if job_id else "rule",
        target_id=job_id,
        after=after,
        household_id=household_id,
        user_id=user_id,
        request=request,
    )
    if summary.changed:
        payload: dict[str, Any] = {
            "changed": summary.changed,
            "ids": [str(i) for i in summary.changed_ids[:IDS_IN_RECORD_MAX]],
        }
        if job_id is not None:
            payload["job_id"] = str(job_id)
        emit(db, household_id, "transaction.rules_applied", payload)


def apply_rules_to(
    db: Session,
    household_id: uuid.UUID,
    transactions: Sequence[Transaction],
    *,
    protect: Iterable[RuleField | str] = (),
    user_id: uuid.UUID | None = None,
    request: HTTPConnection | None = None,
) -> ApplySummary:
    """Run the household's enabled rules over new transactions (the creation hook).

    Called by the code that creates transactions (manual create, the
    structured importer) after it flushed them, in the same database
    transaction and RLS context (``household_id``). ``protect`` names the
    fields a person just entered by hand (:class:`RuleField` values, e.g.
    ``{"categorization"}`` when the transaction was created with a
    category); rules never overwrite those. Updates the rows (and their
    splits and tags) in place, counts rule hits, writes one ``rule.apply``
    audit row and emits ``transaction.rules_applied`` when anything changed.
    """
    rows = [row for row in transactions if row.household_id == household_id]
    if len(rows) != len(transactions):
        raise ValueError("every transaction must belong to household_id")
    summary = ApplySummary()
    if not rows:
        return summary
    rules = _without_dangling(db, household_id, compiled_rules(db, household_id))
    if not rules:
        return summary
    guarded = frozenset(RuleField(p) for p in protect)
    names = rules_repo.payee_names(db, household_id, _payee_targets(rules))
    for start in range(0, len(rows), BATCH):
        _apply_batch(
            db,
            household_id,
            rules,
            rows[start : start + BATCH],
            summary,
            payee_names=names,
            protect=guarded,
        )
    _finish(db, household_id, summary, source="new_transactions", user_id=user_id, request=request)
    return summary


def request_apply(
    db: Session, principal: Principal, data: RuleApply, *, request: HTTPConnection | None
) -> JobAccepted:
    """Queue a ``run_rules`` job (owner/admin); the answer carries its id."""
    household_id = _household(principal, HouseholdRole.ADMIN)
    if data.account_ids:
        require_visible_accounts(db, principal, data.account_ids, need="edit")
    if data.rule_ids:
        known = {r.id for r in rules_repo.list_rules(db, household_id)}
        if not set(data.rule_ids) <= known:
            raise rule_not_found()
    payload = RunRulesJob(
        rule_ids=data.rule_ids,
        account_ids=data.account_ids,
        date_from=data.date_from,
        date_to=data.date_to,
        requested_by=principal.user_id,
    )
    job_id = enqueue(db, RUN_RULES_JOB_TYPE, payload, household_id=household_id)
    audit(
        db,
        action="rule.apply_requested",
        target_type="job",
        target_id=job_id,
        after=payload.model_dump(mode="json"),
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(db, household_id, "job.queued", {"id": str(job_id), "type": RUN_RULES_JOB_TYPE})
    return JobAccepted(job_id=job_id, state="queued")


def run_rules(
    db: Session,
    household_id: uuid.UUID,
    payload: RunRulesJob,
    *,
    job_id: uuid.UUID | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> ApplySummary:
    """The ``run_rules`` job: re-apply the rules over existing transactions.

    Runs as the system over the whole household (an owner or admin asked
    for it), newest first, in locked batches of :data:`BATCH`.
    """
    summary = ApplySummary()
    rules = compiled_rules(
        db, household_id, rule_ids=set(payload.rule_ids) if payload.rule_ids else None
    )
    rules = _without_dangling(db, household_id, rules)
    if not rules:
        return summary
    names = rules_repo.payee_names(db, household_id, _payee_targets(rules))
    clauses = rules_repo.transaction_scope(
        household_id,
        account_ids=payload.account_ids,
        date_from=payload.date_from,
        date_to=payload.date_to,
    )
    after = None
    while True:
        if check_cancelled is not None:
            check_cancelled()
        batch = rules_repo.transactions_after(db, clauses, after=after, limit=BATCH, lock=True)
        if not batch:
            break
        _apply_batch(db, household_id, rules, batch, summary, payee_names=names)
        after = (batch[-1].posted_date, batch[-1].id)
        if len(batch) < BATCH:
            break
    _finish(
        db, household_id, summary, source="run_rules", user_id=payload.requested_by, job_id=job_id
    )
    return summary


# ---------------------------------------------------------------------------
# Export / import
# ---------------------------------------------------------------------------


def _category_paths(db: Session, household_id: uuid.UUID) -> dict[uuid.UUID, str]:
    nodes = {
        cid: (parent, name) for cid, parent, name in rules_repo.all_categories(db, household_id)
    }
    paths: dict[uuid.UUID, str] = {}
    for cid in nodes:
        names: list[str] = []
        current: uuid.UUID | None = cid
        seen: set[uuid.UUID] = set()
        while current is not None and current in nodes and current not in seen:
            seen.add(current)
            parent, name = nodes[current]
            names.append(name)
            current = parent
        paths[cid] = " / ".join(reversed(names))
    return paths


def _name_tables(db: Session, household_id: uuid.UUID) -> dict[str, dict[uuid.UUID, str]]:
    return {
        "category": _category_paths(db, household_id),
        "payee": {pid: name for pid, name, _ in rules_repo.all_payees(db, household_id)},
        "tag": dict(rules_repo.all_tags(db, household_id)),
        "account": dict(rules_repo.all_accounts(db, household_id)),
    }


def export_rules(db: Session, principal: Principal) -> RuleExport:
    """Every rule plus the names of what it references (portable to another household)."""
    household_id = _household(principal, HouseholdRole.VIEWER)
    visible = _reader_accounts(db, principal)
    items: list[RuleExportItem] = []
    for rule in rules_repo.list_rules(db, household_id):
        conditions, _ = _redact(rule.conditions, visible)
        items.append(
            RuleExportItem.model_validate(
                {
                    "name": rule.name,
                    "enabled": rule.enabled,
                    "stop_processing": rule.stop_processing,
                    "conditions": conditions,
                    "actions": rule.actions,
                }
            )
        )
    named = engine.references([item.model_dump(mode="json") for item in items])
    tables = _name_tables(db, household_id)

    def refs(kind: str) -> list[Reference]:
        table = tables[kind]
        return [
            Reference(id=i, name=table[i])
            for i in sorted(named.get(kind, set()))
            if i in table and (kind != "account" or visible is None or i in visible)
        ]

    return RuleExport(
        rules=items,
        references=References(
            categories=refs("category"),
            payees=refs("payee"),
            tags=refs("tag"),
            accounts=refs("account"),
        ),
    )


def _resolver(db: Session, household_id: uuid.UUID) -> dict[str, Callable[[str], uuid.UUID | None]]:
    """Per kind: name -> the one matching object of this household (``None``: none or several)."""
    paths: dict[str, list[uuid.UUID]] = defaultdict(list)
    for cid, path in _category_paths(db, household_id).items():
        paths[engine.fold(path)].append(cid)
    payees = {norm: pid for pid, _, norm in rules_repo.all_payees(db, household_id)}
    tags: dict[str, list[uuid.UUID]] = defaultdict(list)
    for tid, name in rules_repo.all_tags(db, household_id):
        tags[engine.fold(name)].append(tid)
    accounts: dict[str, list[uuid.UUID]] = defaultdict(list)
    for aid, name in rules_repo.all_accounts(db, household_id):
        accounts[engine.fold(name)].append(aid)

    def unique(table: Mapping[str, list[uuid.UUID]]) -> Callable[[str], uuid.UUID | None]:
        def find(name: str) -> uuid.UUID | None:
            found = table.get(engine.fold(name), [])
            return found[0] if len(found) == 1 else None

        return find

    return {
        "category": unique(paths),
        "payee": lambda name: payees.get(normalize_payee(name)),
        "tag": unique(tags),
        "account": unique(accounts),
    }


def import_rules(
    db: Session, principal: Principal, data: RuleImport, *, request: HTTPConnection | None
) -> RuleList:
    """Add (``append``) or replace (``replace``) the rules from an export document."""
    household_id = _household(principal, HouseholdRole.ADMIN)
    existing = rules_repo.count_rules(db, household_id)
    if (0 if data.mode == "replace" else existing) + len(data.rules) > RULES_MAX:
        raise _rule_limit()
    documents = [_documents(item) for item in data.rules]
    named = engine.references([list(doc) for doc in documents])
    given: dict[str, dict[uuid.UUID, str]] = {
        "category": {r.id: r.name for r in data.references.categories},
        "payee": {r.id: r.name for r in data.references.payees},
        "tag": {r.id: r.name for r in data.references.tags},
        "account": {r.id: r.name for r in data.references.accounts},
    }
    models: dict[str, type[Category] | type[Payee] | type[Tag]] = {
        "category": Category,
        "payee": Payee,
        "tag": Tag,
    }
    resolve = _resolver(db, household_id)
    mapping: dict[str, dict[uuid.UUID, uuid.UUID]] = {}
    unresolved: list[str] = []
    for kind, ids in sorted(named.items()):
        if not ids:
            continue
        present = (
            rules_repo.existing_account_ids(db, household_id, ids)
            if kind == "account"
            else tx_repo.existing_ids(db, models[kind], household_id, ids)
        )
        table: dict[uuid.UUID, uuid.UUID] = {}
        for ident in sorted(ids - present):
            name = given[kind].get(ident)
            target = resolve[kind](name) if name is not None else None
            if target is None:
                unresolved.append(f"{kind} {name!r}" if name is not None else f"an unnamed {kind}")
            else:
                table[ident] = target
        mapping[kind] = table
    if unresolved:
        shown = ", ".join(unresolved[:10]) + (" ..." if len(unresolved) > 10 else "")
        raise ProblemError(
            422,
            "rule-reference-unresolved",
            "Rule references could not be resolved",
            f"Create these first or remove them from the rules: {shown}.",
        )
    try:
        bodies = [
            RuleBody.model_validate(
                {
                    "conditions": engine.remap(conditions, mapping),
                    "actions": engine.remap(actions, mapping),
                    "stop_processing": item.stop_processing,
                }
            )
            for item, (conditions, actions) in zip(data.rules, documents, strict=True)
        ]
    except ValidationError:
        raise ProblemError(
            422,
            "rule-import-invalid",
            "Rules do not validate here",
            "After resolving references by name, a rule no longer validates "
            "(for example two ids became the same object).",
        ) from None
    removed = rules_repo.delete_all(db, household_id) if data.mode == "replace" else 0
    start = rules_repo.next_sort_order(db, household_id)
    created: list[Rule] = []
    for offset, (item, body) in enumerate(zip(data.rules, bodies, strict=True)):
        conditions, actions = _documents(body)
        rule = Rule(
            household_id=household_id,
            name=item.name,
            sort_order=start + offset,
            enabled=item.enabled,
            stop_processing=body.stop_processing,
            conditions=conditions,
            actions=actions,
        )
        db.add(rule)
        created.append(rule)
    db.flush()
    _record(
        db,
        principal,
        household_id,
        action="rule.import",
        event="rule.imported",
        target_id=None,
        after={
            "mode": data.mode,
            "imported": len(created),
            "removed": removed,
            "ids": [str(r.id) for r in created],
        },
        request=request,
    )
    return RuleList(items=[rule_out(r) for r in rules_repo.list_rules(db, household_id)])

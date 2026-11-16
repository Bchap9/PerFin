# SPDX-License-Identifier: AGPL-3.0-or-later
"""Merchant rules: "always categorise this merchant as...", from a row.

A merchant rule is an ordinary rule whose conditions are one ``merchant``
leaf and whose actions are one ``set_category``. A member may preview one,
create one and revert one; every other rule write stays with owners and
admins (``app.domain.rules.service``, which also holds the writes used
here, so that every rule write is audited in one place).

* :func:`merchant_rule` finds the rule a merchant has.
* :func:`preview` counts, in SQL and over the accounts the caller may
  **edit**, what the rule would change. It writes nothing.
* :func:`create` appends the rule (last; the 500 cap applies) or changes the
  category of the one the merchant has, and makes the row it was made from
  the person's own choice. It is refused when the row cannot teach (``422
  merchant-not-teachable``) or when an earlier rule would decide the row
  before the merchant rule is reached (``409 rule-shadowed``), so a rule
  that can never fire is never saved. "Apply to existing" is a ``run_rules``
  job for that one rule over the accounts the caller may edit; such a job
  changes exactly what the preview counted as ``uncategorised`` and
  ``guessed`` and never a category somebody decided.
* :func:`revert` deletes a rule and returns exactly the rows it categorised
  to undecided, as the system (``updated_at`` does not move), then asks the
  engine to guess again.

Audit rows and events carry ids and counts only: never the merchant's key
or name (which is why a merchant rule's own name is not audited either).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement
from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.errors import ProblemError
from app.core.events import emit
from app.db.queue import enqueue
from app.domain.accounts.visibility import (
    can_edit_account,
    sees_all_accounts,
    visible_account_filter,
    visible_account_ids,
)
from app.domain.categorisation import cleanup, engine
from app.domain.categorisation.pinned import pinned_update
from app.domain.rules import engine as rules_engine
from app.domain.rules import repo as rules_repo
from app.domain.rules import service as rules_service
from app.domain.rules.schemas import (
    IDS_MAX,
    PREVIEW_SAMPLE_MAX,
    MerchantRuleIn,
    MerchantRuleOut,
    MerchantRulePreview,
    MerchantRulePreviewIn,
    RuleRevertOut,
)
from app.domain.transactions import repo as tx_repo
from app.domain.transactions import service as tx_service
from app.domain.transactions.models import NAME_MAX, Category, Rule, Transaction
from app.domain.transactions.schemas import TransactionUpdate
from app.workers.registry import REGISTRY

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

#: The job that looks again at the rows nobody decided (``app.domain.categorisation``).
REFRESH_JOB_TYPE: Final = "categorise.refresh"
#: Rows written per statement when a revert undoes many.
REVERT_BATCH: Final = 1000

Reason = Literal["no_merchant", "not_a_purchase", "split"]


class RefreshJob(BaseModel):
    """Payload of :data:`REFRESH_JOB_TYPE` as this module queues it: look again
    whatever the versions say."""

    model_config = ConfigDict(extra="forbid")

    force: bool = True


def _member(principal: Principal) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, HouseholdRole.MEMBER):
        raise forbidden()
    return principal.household_id


def not_teachable(reason: Reason) -> ProblemError:
    details = {
        "no_merchant": "This transaction does not name a merchant.",
        "not_a_purchase": (
            "This transaction is a transfer, a payment to a person or another movement "
            "that says nothing about where money was spent."
        ),
        "split": "This transaction is split between categories.",
    }
    return ProblemError(
        422,
        "merchant-not-teachable",
        "No rule can be made from this transaction",
        details[reason],
        extra={"reason": reason},
    )


def rule_shadowed(rule_id: uuid.UUID) -> ProblemError:
    return ProblemError(
        409,
        "rule-shadowed",
        "An earlier rule already decides this transaction",
        f"Rule {rule_id} runs first and decides transactions like this one, so a rule for "
        "the merchant would never apply. Change or reorder that rule first.",
        extra={"rule_id": str(rule_id)},
    )


def merchant_rule(db: Session, household_id: uuid.UUID, key: str) -> Rule | None:
    """The household's enabled rule for the merchant ``key``, or ``None``.

    That is the first rule, in rule order, whose conditions are exactly one
    ``merchant`` leaf with this key (bare, or the only member of an ``all``)
    and whose actions are exactly one ``set_category``. A rule that also
    looks at anything else, or does anything else, is a general rule.
    """
    for rule in rules_repo.list_rules(db, household_id, enabled_only=True):
        found = rules_engine.merchant_document(rule.conditions, rule.actions)
        if found is not None and found[0] == key:
            return rule
    return None


def _category(db: Session, household_id: uuid.UUID, category_id: uuid.UUID) -> Category:
    category = db.get(Category, category_id)
    if category is None or category.household_id != household_id:
        raise ProblemError(404, "category-not-found", "Category not found")
    return category


def _unteachable(db: Session, row: Transaction) -> Reason | None:
    """Why no merchant rule can be made from ``row``, or ``None`` when one can."""
    if not row.merchant_key:
        return "no_merchant"
    if cleanup.clean(row.raw_payee).kind not in cleanup.TEACHABLE:
        return "not_a_purchase"
    if tx_repo.has_splits(db, row.household_id, row.id):
        return "split"
    return None


def _scope(principal: Principal, household_id: uuid.UUID, key: str) -> list[ColumnElement[bool]]:
    """The merchant's rows on the accounts the caller may edit."""
    return [
        Transaction.household_id == household_id,
        Transaction.merchant_key == key,
        visible_account_filter(principal, Transaction.account_id, need="edit"),
    ]


def _rule_name(key: str, category: Category) -> str:
    return f"{engine.display_name(key) or key}: {category.name}"[:NAME_MAX]


def _own_category(rule: Rule | None) -> uuid.UUID | None:
    if rule is None:
        return None
    found = rules_engine.merchant_document(rule.conditions, rule.actions)
    return found[1] if found is not None else None


def preview(db: Session, principal: Principal, data: MerchantRulePreviewIn) -> MerchantRulePreview:
    """What a merchant rule made from this row would change (nothing is written)."""
    household_id = _member(principal)
    row = tx_service.get_visible_transaction(db, principal, data.transaction_id)
    _category(db, household_id, data.category_id)
    key = row.merchant_key or None
    reason = _unteachable(db, row)
    if key is None or reason is not None:
        return MerchantRulePreview(
            merchant_key=key,
            merchant=engine.display_name(key),
            teachable=False,
            reason=reason,
            uncategorised=0,
            guessed=0,
            decided=0,
            same_category=0,
            existing_rule_id=None,
            shadowed_by=None,
            sample=[],
        )
    existing = merchant_rule(db, household_id, key)
    clauses = _scope(principal, household_id, key)
    state = rules_repo.merchant_state(
        data.category_id,
        existing.id if existing is not None else None,
        rules_service.GUESS_SOURCES,
    )
    counts = rules_repo.merchant_counts(db, clauses, state)
    return MerchantRulePreview(
        merchant_key=key,
        merchant=engine.display_name(key),
        teachable=True,
        reason=None,
        uncategorised=counts.get(rules_repo.UNCATEGORISED, 0),
        guessed=counts.get(rules_repo.GUESSED, 0),
        decided=counts.get(rules_repo.DECIDED, 0),
        same_category=counts.get(rules_repo.SAME_CATEGORY, 0),
        existing_rule_id=existing.id if existing is not None else None,
        shadowed_by=rules_service.deciding_rule(
            db, household_id, row, merchant_key=key, before=existing
        ),
        sample=rules_repo.merchant_sample(db, clauses, state, PREVIEW_SAMPLE_MAX),
    )


def _decide_source_row(
    db: Session,
    principal: Principal,
    row: Transaction,
    category_id: uuid.UUID,
    *,
    request: HTTPConnection | None,
) -> None:
    """Make ``row`` the person's own choice of ``category_id``, if it is not already.

    "Always" said from a row is first of all a decision about that row. A
    row on an account the caller may only read is left as it is.
    """
    if row.category_source == rules_service.PERSON_SOURCE and row.category_id == category_id:
        return
    if not can_edit_account(db, principal, row.account_id):
        return
    locked = tx_service.get_visible_transaction(db, principal, row.id, need="edit", lock=True)
    before_category, before_source = locked.category_id, locked.category_source
    if before_category != category_id:
        # A person's edit, through the one place that makes them (audited there).
        tx_service.update_transaction(
            db, principal, locked, TransactionUpdate(category_id=category_id), request=request
        )
    if locked.category_source != rules_service.PERSON_SOURCE:
        # The category was already this one, as a guess or a rule's: now a person said so.
        now = clock.utcnow()
        engine.person_decided(
            db, locked, before_category_id=before_category, before_source=before_source, at=now
        )
        locked.updated_at = now
        db.flush()


def _queue_apply(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    key: str,
    rule: Rule,
    *,
    request: HTTPConnection | None,
) -> uuid.UUID | None:
    """One job per ``IDS_MAX`` editable accounts that hold the merchant; the first job's id."""
    accounts = rules_repo.merchant_accounts(db, _scope(principal, household_id, key))
    first: uuid.UUID | None = None
    for start in range(0, len(accounts), IDS_MAX):
        job_id = rules_service.queue_apply(
            db,
            principal,
            rule_id=rule.id,
            account_ids=accounts[start : start + IDS_MAX],
            request=request,
        )
        first = first or job_id
    return first


def create(
    db: Session, principal: Principal, data: MerchantRuleIn, *, request: HTTPConnection | None
) -> MerchantRuleOut:
    """Create the merchant's rule, or change the category of the one it has."""
    household_id = _member(principal)
    row = tx_service.get_visible_transaction(db, principal, data.transaction_id)
    category = _category(db, household_id, data.category_id)
    reason = _unteachable(db, row)
    key = row.merchant_key
    if reason is not None or not key:
        raise not_teachable(reason or "no_merchant")
    existing = merchant_rule(db, household_id, key)
    if existing is not None:
        existing = rules_service.locked_rule(db, principal, existing.id)
    shadow = rules_service.deciding_rule(db, household_id, row, merchant_key=key, before=existing)
    if shadow is not None:
        raise rule_shadowed(shadow)

    name = _rule_name(key, category)
    previous = _own_category(existing)
    replaced = existing is not None and previous != category.id
    if existing is None:
        rule = rules_service.add_merchant_rule(
            db, principal, name=name, key=key, category_id=category.id, request=request
        )
    else:
        rule = existing
        if replaced:
            rules_service.set_merchant_rule_category(
                db, principal, rule, category.id, name=name, request=request
            )
    _decide_source_row(db, principal, row, category.id, request=request)
    job_id = (
        _queue_apply(db, principal, household_id, key, rule, request=request)
        if data.apply_to_existing
        else None
    )
    visible = None if sees_all_accounts(principal) else set(visible_account_ids(db, principal))
    return MerchantRuleOut(
        rule=rules_service.rule_out(rule, visible),
        replaced=replaced,
        previous_category_id=previous if replaced else None,
        job_id=job_id,
    )


def queue_refresh(db: Session, household_id: uuid.UUID) -> uuid.UUID:
    """Ask the engine to guess again for the household, whatever its versions say."""
    spec = REGISTRY.get(REFRESH_JOB_TYPE)
    # Where the worker's handler is loaded, its own payload model is the one to send.
    payload = RefreshJob() if spec is None else spec.payload_model.model_validate({"force": True})
    job_id = enqueue(db, REFRESH_JOB_TYPE, payload, household_id=household_id)
    emit(db, household_id, "job.queued", {"id": str(job_id), "type": REFRESH_JOB_TYPE})
    return job_id


def revert(
    db: Session, principal: Principal, rule_id: uuid.UUID, *, request: HTTPConnection | None
) -> RuleRevertOut:
    """Delete a rule and return the rows it categorised to undecided.

    The rows are collected first (on every account: the rule was the
    household's), then the rule goes, then exactly those rows lose their
    category, its source and any split the rule made, through the pinned
    write: this undoes a system write, so ``updated_at`` stays. A row a
    person re-categorised since is theirs and is not among them. When rows
    were returned, a refresh is queued so the engine guesses for them again.
    """
    rule = rules_service.locked_rule(db, principal, rule_id)
    household_id = rules_service.require_revertible(principal, rule)
    rows = rules_repo.rows_set_by_rule(db, household_id, rule.id)
    rules_service.remove_reverted_rule(db, principal, rule, reverted=len(rows), request=request)
    undecided = {
        "category_id": None,
        "category_source": None,
        "category_confidence": None,
        "category_rule_id": None,
    }
    ids = [row_id for row_id, _ in rows]
    for start in range(0, len(ids), REVERT_BATCH):
        batch = ids[start : start + REVERT_BATCH]
        rules_repo.delete_splits_of(db, household_id, batch)
        pinned_update(db, household_id, dict.fromkeys(batch, undecided))
    job_id: uuid.UUID | None = None
    if rows:
        emit(
            db,
            household_id,
            "transaction.rules_applied",
            {
                "changed": len(rows),
                "ids": [str(i) for i in ids[: rules_service.IDS_IN_RECORD_MAX]],
                # Members and viewers hear only about accounts they see (SECURITY.md I2).
                "account_ids": sorted({str(account_id) for _, account_id in rows}),
            },
        )
        job_id = queue_refresh(db, household_id)
    return RuleRevertOut(rule_id=rule_id, reverted=len(rows), job_id=job_id)

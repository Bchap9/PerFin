# SPDX-License-Identifier: AGPL-3.0-or-later
"""Merchant rules: "always categorise this merchant as...", from a row.

A merchant rule is an ordinary rule whose conditions are one ``merchant``
leaf and whose actions are one ``set_category``. A member may preview one,
create one and revert one; every other rule write stays with owners and
admins (``app.domain.rules.service``).

:func:`merchant_rule` finds the rule a merchant has. The three operations
are still the declaration the router is built against: they answer ``409
not-ready`` once the caller and the object they name are checked.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.errors import ProblemError
from app.domain.rules import repo as rules_repo
from app.domain.rules import service as rules_service
from app.domain.rules.schemas import (
    MerchantRuleIn,
    MerchantRuleOut,
    MerchantRulePreview,
    MerchantRulePreviewIn,
    RuleRevertOut,
)
from app.domain.transactions import service as tx_service
from app.domain.transactions.models import Rule

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection


def not_ready() -> ProblemError:
    return ProblemError(409, "not-ready", "Not available yet", "This is not available yet.")


def _member(principal: Principal) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, HouseholdRole.MEMBER):
        raise forbidden()
    return principal.household_id


def merchant_rule(db: Session, household_id: uuid.UUID, key: str) -> Rule | None:
    """The household's enabled rule for the merchant ``key``, or ``None``.

    That is the first rule, in rule order, whose conditions are exactly one
    ``merchant`` leaf with this key (bare, or the only member of an ``all``)
    and whose actions are exactly one ``set_category``. A rule that also
    looks at anything else, or does anything else, is a general rule.
    """
    for rule in rules_repo.list_rules(db, household_id, enabled_only=True):
        if _merchant_key(rule.conditions) == key and _sets_one_category(rule.actions):
            return rule
    return None


def _merchant_key(conditions: Any) -> str | None:
    """The key of a document that is one ``merchant`` leaf, else ``None``."""
    if not isinstance(conditions, dict):
        return None
    leaf = conditions
    if leaf.get("type") == "all":
        members = leaf.get("conditions")
        if not isinstance(members, list) or len(members) != 1 or not isinstance(members[0], dict):
            return None
        leaf = members[0]
    found = leaf.get("key")
    return found if leaf.get("type") == "merchant" and isinstance(found, str) else None


def _sets_one_category(actions: Any) -> bool:
    if not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], dict):
        return False
    return actions[0].get("type") == "set_category" and actions[0].get("category_id") is not None


def preview(db: Session, principal: Principal, data: MerchantRulePreviewIn) -> MerchantRulePreview:
    """What a merchant rule made from this row would change (nothing is written)."""
    _member(principal)
    tx_service.get_visible_transaction(db, principal, data.transaction_id)
    raise not_ready()


def create(
    db: Session, principal: Principal, data: MerchantRuleIn, *, request: HTTPConnection | None
) -> MerchantRuleOut:
    """Create the merchant's rule, or change the category of the one it has."""
    del request
    _member(principal)
    tx_service.get_visible_transaction(db, principal, data.transaction_id)
    raise not_ready()


def revert(
    db: Session, principal: Principal, rule_id: uuid.UUID, *, request: HTTPConnection | None
) -> RuleRevertOut:
    """Delete a rule and return the rows it categorised to undecided."""
    del request
    _member(principal)
    rules_service.require_rule(db, principal, rule_id)
    raise not_ready()

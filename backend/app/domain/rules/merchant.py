# SPDX-License-Identifier: AGPL-3.0-or-later
"""Merchant rules: "always categorise this merchant as...", from a row.

A merchant rule is an ordinary rule whose conditions are one ``merchant``
leaf and whose actions are one ``set_category``. A member may preview one,
create one and revert one; every other rule write stays with owners and
admins (``app.domain.rules.service``).

This is the declaration the router is built against: no merchant has a rule
yet, and the three operations answer ``409 not-ready`` once the caller and
the object they name are checked.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.errors import ProblemError
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
    """The household's enabled rule for the merchant ``key``, or ``None``."""
    del db, household_id, key
    return None


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

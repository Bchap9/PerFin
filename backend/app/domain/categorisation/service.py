# SPDX-License-Identifier: AGPL-3.0-or-later
"""What ``/categorisation`` answers: the summary, what was learned, forgetting, a refresh.

Reading needs any household role, forgetting a learned merchant a member,
asking for a refresh an admin. A learned entry of another household is
``404 memory-not-found``.

This is the declaration the router is built against: the summary is all
zeros and the learned list is empty for now, and forgetting and refreshing
answer ``409 not-ready`` once the caller and the object are checked.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.errors import ProblemError
from app.domain.categorisation import cleanup, dictionary
from app.domain.categorisation.models import MerchantMemory
from app.domain.categorisation.schemas import CategorisationSummary, LearnedCategoryPage
from app.domain.rules.schemas import JobAccepted

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection


def memory_not_found() -> ProblemError:
    return ProblemError(404, "memory-not-found", "Not found", "No such learned category.")


def not_ready() -> ProblemError:
    return ProblemError(409, "not-ready", "Not available yet", "This is not available yet.")


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return principal.household_id


def summary(db: Session, principal: Principal) -> CategorisationSummary:
    """Where categorisation stands, counted over the accounts the caller may see."""
    del db
    _household(principal, HouseholdRole.VIEWER)
    return CategorisationSummary(
        dictionary_version=dictionary.version(),
        dictionary_merchants=len(dictionary.merchants()),
        cleaner_version=cleanup.CLEANER_VERSION,
        up_to_date=False,
        by_source=[],
        uncategorised=0,
        suggestions=0,
        learned_merchants=0,
    )


def list_learned(
    db: Session, principal: Principal, *, cursor: uuid.UUID | None, limit: int
) -> LearnedCategoryPage:
    """One page of what the household's corrections taught, in merchant-key order."""
    del db, cursor, limit
    _household(principal, HouseholdRole.VIEWER)
    return LearnedCategoryPage(items=[], next_cursor=None)


def require_memory(db: Session, principal: Principal, memory_id: uuid.UUID) -> None:
    """``404 memory-not-found`` unless the entry is one of the caller's household."""
    household_id = _household(principal, HouseholdRole.MEMBER)
    found = db.execute(
        select(MerchantMemory.id).where(
            MerchantMemory.household_id == household_id, MerchantMemory.id == memory_id
        )
    ).first()
    if found is None:
        raise memory_not_found()


def forget(
    db: Session, principal: Principal, memory_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    """Forget what was learned about one merchant (every category of the entry)."""
    del request
    require_memory(db, principal, memory_id)
    raise not_ready()


def request_refresh(
    db: Session, principal: Principal, *, request: HTTPConnection | None
) -> JobAccepted:
    """Queue a refresh of the household's undecided rows (admin)."""
    del db, request
    _household(principal, HouseholdRole.ADMIN)
    raise not_ready()

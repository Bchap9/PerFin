# SPDX-License-Identifier: AGPL-3.0-or-later
"""The personal-access-token scope catalog and each role's ceiling (SECURITY.md I3).

A token carries an explicit list of scopes (``documents:write``,
``transactions:read``, ...). What it may actually do is the intersection of

* the scopes it was created with,
* the **ceiling** of its user's *current* household role
  (:data:`ROLE_SCOPES`, re-read on every request by
  ``app.auth.principal.authenticate_token``), so a demoted user's token loses
  the scopes the new role does not allow, and a token whose user left the
  household stops working altogether,
* and the endpoint's own role and account-grant checks (scopes are a ceiling,
  never a grant: ``require_scope`` runs *in addition to* ``require_role`` and
  ``account_access``).

Browser sessions carry no scope list (``Principal.scopes is None``): they may
use every scope, and their role guards decide what the role allows.

Rules for adding a scope: add it to :data:`CATALOG` with a description, put it
in the right role ceilings below, and guard the endpoints with
``app.auth.authz.require_scope("<scope>")`` (which refuses names that are not
in the catalog, so a typo fails at import time rather than silently denying
every token).

``ai:read`` is **reserved**: it exists so the ceiling and the AI views
(Phase 6b, ADR-0007) can refer to it, but a personal access token can never
be created with it; only the AI-client flow (``/settings/ai/clients``) mints
such tokens.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Final

from app.auth.models import HouseholdRole

#: Every scope a token can name, with the description shown in the UI.
CATALOG: Final[Mapping[str, str]] = MappingProxyType(
    {
        "accounts:read": "Read accounts and balances",
        "accounts:write": "Create, change and delete accounts and balances",
        "transactions:read": "Read transactions",
        "transactions:write": "Create, change, split and delete transactions",
        "budgets:read": "Read budgets and goals",
        "budgets:write": "Create, change and delete budgets and goals",
        "documents:read": "List documents and their extracted data",
        "documents:write": "Upload, reprocess, link and delete documents",
        "investments:read": "Read securities, holdings and investment transactions",
        "investments:write": "Change securities and investment transactions",
        "loans:read": "Read loans and their schedules",
        "loans:write": "Create, change and delete loans",
        "networth:read": "Read net worth",
        "tax:read": "Read tax years and checklists",
        "tax:write": "Change tax years and checklists",
        "review:read": "Read the review queue",
        "review:write": "Resolve and dismiss review items",
        "settings:read": "Read household and user settings",
        "households:read": "Read the household and its members",
        "households:write": "Rename the household or change its base currency",
        "export": "Run a full export of the household",
        "ai:read": "Reserved for AI clients (MCP); cannot be granted to a personal token",
    }
)

ALL_SCOPES: Final[frozenset[str]] = frozenset(CATALOG)

#: The AI client scope (ADR-0007): minted only by ``app.domain.ai.service``.
AI_SCOPE: Final = "ai:read"

#: Scopes that exist but cannot be put on a personal access token.
RESERVED_SCOPES: Final[frozenset[str]] = frozenset({AI_SCOPE})

#: Scopes a personal access token may be created with (role permitting).
GRANTABLE_SCOPES: Final[frozenset[str]] = ALL_SCOPES - RESERVED_SCOPES

READ_SCOPES: Final[frozenset[str]] = frozenset(s for s in GRANTABLE_SCOPES if s.endswith(":read"))

#: The most each role may hold. Owner > admin > member > viewer.
ROLE_SCOPES: Final[Mapping[HouseholdRole, frozenset[str]]] = MappingProxyType(
    {
        # The owner manages AI clients (ADR-0007), so only the owner's ceiling
        # includes ai:read; it is still never grantable to a personal token.
        HouseholdRole.OWNER: ALL_SCOPES,
        HouseholdRole.ADMIN: GRANTABLE_SCOPES,
        # Members work on granted accounts, their documents and the review
        # queue; they neither change the household nor export all of it.
        HouseholdRole.MEMBER: GRANTABLE_SCOPES - {"households:write", "export"},
        # Viewers read only (document downloads are further limited, I1).
        HouseholdRole.VIEWER: READ_SCOPES,
    }
)


def is_known(scope: str) -> bool:
    return scope in CATALOG


def scopes_for_role(role: HouseholdRole | None) -> frozenset[str]:
    """The ceiling of ``role``; nothing without a household role."""
    if role is None:
        return frozenset()
    return ROLE_SCOPES[role]


def grantable_for_role(role: HouseholdRole | None) -> frozenset[str]:
    """What a user with ``role`` may put on a new personal access token."""
    return scopes_for_role(role) - RESERVED_SCOPES


def effective_scopes(granted: Iterable[str], role: HouseholdRole | None) -> frozenset[str]:
    """A token's scopes narrowed to its user's current role (unknown names drop out)."""
    return frozenset(granted) & scopes_for_role(role)


class ScopeError(ValueError):
    """A requested scope list is not acceptable; ``code`` is the problem slug."""

    def __init__(self, code: str, message: str, scopes: Iterable[str]) -> None:
        super().__init__(message)
        self.code = code
        self.scopes = tuple(sorted(scopes))


def check_requested(requested: Iterable[str], role: HouseholdRole | None) -> frozenset[str]:
    """Validate the scopes for a new personal access token; return them as a set.

    Raises :class:`ScopeError` with ``code``:

    * ``unknown-scope``: a name that is not in :data:`CATALOG`;
    * ``scope-reserved``: ``ai:read`` (only the AI-client flow mints it);
    * ``scope-not-allowed``: a scope above the caller's role ceiling.
    """
    wanted = frozenset(requested)
    unknown = wanted - ALL_SCOPES
    if unknown:
        raise ScopeError("unknown-scope", "Unknown scope.", unknown)
    reserved = wanted & RESERVED_SCOPES
    if reserved:
        raise ScopeError(
            "scope-reserved", "This scope cannot be put on a personal token.", reserved
        )
    beyond = wanted - grantable_for_role(role)
    if beyond:
        raise ScopeError("scope-not-allowed", "Your role does not allow this scope.", beyond)
    return wanted

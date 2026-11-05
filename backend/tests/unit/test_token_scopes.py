# SPDX-License-Identifier: AGPL-3.0-or-later
"""The token scope catalog, role ceilings and request validation (SECURITY.md I3)."""

from __future__ import annotations

import pytest

from app.auth.authz import require_scope
from app.auth.models import HouseholdRole
from app.auth.scopes import (
    ALL_SCOPES,
    CATALOG,
    GRANTABLE_SCOPES,
    RESERVED_SCOPES,
    ROLE_SCOPES,
    ScopeError,
    check_requested,
    effective_scopes,
    grantable_for_role,
    scopes_for_role,
)

EXPECTED_CATALOG = {
    "accounts:read",
    "accounts:write",
    "transactions:read",
    "transactions:write",
    "budgets:read",
    "budgets:write",
    "documents:read",
    "documents:write",
    "investments:read",
    "investments:write",
    "loans:read",
    "loans:write",
    "networth:read",
    "tax:read",
    "tax:write",
    "review:read",
    "review:write",
    "settings:read",
    "households:read",
    "households:write",
    "export",
    "ai:read",
}


def test_catalog_is_the_documented_set() -> None:
    assert set(CATALOG) == EXPECTED_CATALOG == ALL_SCOPES
    assert all(description.strip() for description in CATALOG.values())


def test_ai_read_is_reserved_and_never_grantable() -> None:
    assert {"ai:read"} == RESERVED_SCOPES
    assert "ai:read" not in GRANTABLE_SCOPES
    for role in HouseholdRole:
        assert "ai:read" not in grantable_for_role(role)
        with pytest.raises(ScopeError) as caught:
            check_requested(["accounts:read", "ai:read"], role)
        assert caught.value.code == "scope-reserved"
        assert caught.value.scopes == ("ai:read",)


def test_role_ceilings_are_nested() -> None:
    owner, admin, member, viewer = (ROLE_SCOPES[r] for r in HouseholdRole)
    assert viewer < member < admin < owner
    assert owner == ALL_SCOPES
    assert all(scope.endswith(":read") for scope in viewer)
    assert "households:write" not in member
    assert "export" not in member
    assert {"transactions:write", "documents:write", "review:write"} <= member


def test_no_role_means_no_scopes() -> None:
    assert scopes_for_role(None) == frozenset()
    assert effective_scopes(["transactions:read"], None) == frozenset()


def test_effective_scopes_narrow_to_the_current_role() -> None:
    granted = ["transactions:read", "transactions:write", "households:write", "gone:scope"]
    assert effective_scopes(granted, HouseholdRole.OWNER) == {
        "transactions:read",
        "transactions:write",
        "households:write",
    }
    assert effective_scopes(granted, HouseholdRole.MEMBER) == {
        "transactions:read",
        "transactions:write",
    }
    assert effective_scopes(granted, HouseholdRole.VIEWER) == {"transactions:read"}


@pytest.mark.parametrize(
    ("role", "scopes", "code"),
    [
        (HouseholdRole.OWNER, ["accounts:read", "nope:read"], "unknown-scope"),
        (HouseholdRole.VIEWER, ["transactions:write"], "scope-not-allowed"),
        (HouseholdRole.MEMBER, ["export"], "scope-not-allowed"),
        (None, ["accounts:read"], "scope-not-allowed"),
    ],
)
def test_check_requested_refusals(role: HouseholdRole | None, scopes: list[str], code: str) -> None:
    with pytest.raises(ScopeError) as caught:
        check_requested(scopes, role)
    assert caught.value.code == code


def test_check_requested_accepts_within_ceiling() -> None:
    assert check_requested(["accounts:read", "export"], HouseholdRole.ADMIN) == {
        "accounts:read",
        "export",
    }
    assert check_requested(sorted(ROLE_SCOPES[HouseholdRole.VIEWER]), HouseholdRole.VIEWER)


def test_require_scope_refuses_names_outside_the_catalog() -> None:
    require_scope("transactions:read")
    with pytest.raises(ValueError, match="unknown token scope"):
        require_scope("transaction:read")

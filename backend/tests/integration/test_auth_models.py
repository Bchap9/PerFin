# SPDX-License-Identifier: AGPL-3.0-or-later
"""Identity tables: constraints and row-level security of the household-scoped
ones (``household_member``, ``account_grant``, ``api_token``)."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import (
    AccountGrant,
    ApiToken,
    GrantLevel,
    HouseholdMember,
    HouseholdRole,
    Identity,
    IdentityProvider,
    User,
)
from app.auth.principal import token_lookup_scope
from app.db.base import household_scoped_tables
from app.db.rls import set_context
from tests.fixtures.factories import (
    add_member,
    grant_account,
    make_api_token,
    make_household,
    make_user,
)
from tests.fixtures.ledger import make_account

pytestmark = pytest.mark.integration


def test_identity_tables_registered_for_rls() -> None:
    scoped = set(household_scoped_tables())
    assert {"household_member", "account_grant", "api_token"} <= scoped
    assert not {"app_user", "credential", "mfa_method", "identity", "user_session"} & scoped


def test_rls_forced_on_scoped_identity_tables(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname IN ('household_member', 'account_grant', 'api_token')"
            )
        ).all()
        policies = conn.execute(
            text(
                "SELECT tablename, policyname, cmd FROM pg_policies "
                "WHERE tablename IN ('household_member', 'account_grant', 'api_token')"
            )
        ).all()
    assert {(r.relname, r.relrowsecurity, r.relforcerowsecurity) for r in rows} == {
        ("household_member", True, True),
        ("account_grant", True, True),
        ("api_token", True, True),
    }
    extra = {(p.tablename, p.policyname, p.cmd) for p in policies}
    assert ("household_member", "member_self_read", "SELECT") in extra
    assert ("account_grant", "grant_self_read", "SELECT") in extra
    assert ("api_token", "token_self_read", "SELECT") in extra
    assert ("api_token", "token_prefix_lookup", "SELECT") in extra


def test_username_must_be_lowercase(db: Session) -> None:
    db.add(User(username="MixedCase", display_name="x"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_username_unique(db: Session) -> None:
    make_user(db, role=None, username="unique-synthetic-name")
    with pytest.raises(IntegrityError):
        make_user(db, role=None, username="unique-synthetic-name")


def test_identity_unique_on_issuer_subject(db: Session) -> None:
    a = make_user(db, role=None, with_password=False)
    b = make_user(db, role=None, with_password=False)
    issuer = "https://idp.example.test/application/o/perfin/"
    db.add(Identity(user_id=a.id, provider=IdentityProvider.OIDC, issuer=issuer, subject="s1"))
    db.flush()
    db.add(Identity(user_id=b.id, provider=IdentityProvider.OIDC, issuer=issuer, subject="s1"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_membership_unique(db: Session) -> None:
    record = make_user(db, role=HouseholdRole.MEMBER)
    assert record.household is not None
    with pytest.raises(IntegrityError):
        add_member(db, record.household.id, record.id, HouseholdRole.VIEWER)


def test_members_isolated_by_household(db: Session) -> None:
    a = make_user(db, role=HouseholdRole.OWNER)
    b = make_user(db, role=HouseholdRole.OWNER)
    assert a.household
    assert b.household
    set_context(db, a.household.id)
    visible = db.execute(select(HouseholdMember.user_id)).scalars().all()
    assert visible == [a.id]
    set_context(db, None)
    assert db.execute(select(HouseholdMember)).scalars().all() == []


def test_user_can_list_own_memberships_without_household_context(db: Session) -> None:
    """``member_self_read``: memberships by user id before a household is chosen."""
    record = make_user(db, role=HouseholdRole.MEMBER)
    other_household = make_household(db)
    add_member(db, other_household.id, record.id, HouseholdRole.VIEWER)
    stranger = make_user(db, role=HouseholdRole.OWNER)
    set_context(db, None, record.id)
    rows = db.execute(select(HouseholdMember)).scalars().all()
    assert {r.user_id for r in rows} == {record.id}
    assert len(rows) == 2
    assert stranger.household is not None
    assert stranger.household.id not in {r.household_id for r in rows}


def test_self_read_does_not_allow_writes(db: Session) -> None:
    """A user-only context cannot add the user to someone else's household."""
    record = make_user(db, role=None)
    victim = make_user(db, role=HouseholdRole.OWNER)
    assert victim.household is not None
    set_context(db, None, record.id)
    db.add(
        HouseholdMember(
            household_id=victim.household.id, user_id=record.id, role=HouseholdRole.OWNER
        )
    )
    with pytest.raises(DBAPIError, match="row-level security"):
        db.flush()


def test_self_read_does_not_allow_updates(db: Session) -> None:
    record = make_user(db, role=HouseholdRole.VIEWER)
    set_context(db, None, record.id)
    result = db.execute(
        text("UPDATE household_member SET role = 'owner' WHERE user_id = :u"), {"u": record.id}
    )
    assert result.rowcount == 0  # type: ignore[attr-defined]


def test_grants_isolated_and_self_readable(db: Session) -> None:
    a = make_user(db, role=HouseholdRole.MEMBER)
    b = make_user(db, role=HouseholdRole.MEMBER)
    assert a.household
    assert b.household
    account = make_account(db, a.household.id).id
    grant_account(db, a.household.id, a.id, account, GrantLevel.VIEW)
    grant_account(db, b.household.id, b.id, make_account(db, b.household.id).id, GrantLevel.EDIT)
    set_context(db, b.household.id)
    assert db.execute(select(AccountGrant.account_id)).scalars().all() != [account]
    set_context(db, None, a.id)
    assert db.execute(select(AccountGrant.account_id)).scalars().all() == [account]


def test_api_token_lookup_by_prefix_only(db: Session) -> None:
    a = make_user(db, role=HouseholdRole.MEMBER)
    b = make_user(db, role=HouseholdRole.MEMBER)
    assert a.household
    assert b.household
    token_a, _ = make_api_token(db, a.id, a.household.id)
    make_api_token(db, b.id, b.household.id)
    set_context(db, None)
    assert db.execute(select(ApiToken)).scalars().all() == []
    with token_lookup_scope(db, token_a.prefix):
        rows = db.execute(select(ApiToken)).scalars().all()
    assert [r.id for r in rows] == [token_a.id]
    assert db.execute(select(ApiToken)).scalars().all() == []  # scope ended
    with token_lookup_scope(db, ""):
        assert db.execute(select(ApiToken)).scalars().all() == []


def test_api_token_expiry_is_mandatory(db: Session) -> None:
    record = make_user(db, role=HouseholdRole.MEMBER)
    assert record.household is not None
    set_context(db, record.household.id)
    db.add(
        ApiToken(
            household_id=record.household.id,
            user_id=record.id,
            prefix="0123456789ab",
            token_hash="0" * 64,
            name="no expiry",
            scopes=[],
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()

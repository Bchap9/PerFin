# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure helpers of the identity core: role order, step-up window, lockout
schedule, token format, session ids and cookie naming."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.auth.authz import ROLE_RANK, STEP_UP_WINDOW, role_at_least, step_up_is_fresh
from app.auth.models import HouseholdRole, User, normalize_username
from app.auth.principal import (
    Principal,
    generate_api_token,
    hash_api_token,
    parse_api_token,
)
from app.auth.ratelimit import Limit, LockoutPolicy, account_key, key_hash
from app.auth.sessions import (
    DEV_SESSION_COOKIE,
    SESSION_COOKIE,
    SessionPolicy,
    cookie_name,
    hash_session_id,
    is_well_formed,
    new_session_id,
)
from app.core.settings import Settings

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def test_role_ordering() -> None:
    order = [HouseholdRole.OWNER, HouseholdRole.ADMIN, HouseholdRole.MEMBER, HouseholdRole.VIEWER]
    assert sorted(order, key=lambda r: -ROLE_RANK[r]) == order
    assert role_at_least(HouseholdRole.OWNER, HouseholdRole.ADMIN)
    assert role_at_least(HouseholdRole.MEMBER, HouseholdRole.MEMBER)
    assert not role_at_least(HouseholdRole.VIEWER, HouseholdRole.MEMBER)
    assert not role_at_least(HouseholdRole.ADMIN, HouseholdRole.OWNER)
    assert not role_at_least(None, HouseholdRole.VIEWER)


def test_step_up_window_is_ten_minutes() -> None:
    assert timedelta(minutes=10) == STEP_UP_WINDOW
    assert step_up_is_fresh(NOW - timedelta(minutes=9, seconds=59), NOW)
    assert step_up_is_fresh(NOW - timedelta(minutes=10), NOW)
    assert not step_up_is_fresh(NOW - timedelta(minutes=10, seconds=1), NOW)
    assert not step_up_is_fresh(None, NOW)


def test_lockout_schedule_is_progressive_then_locks() -> None:
    policy = LockoutPolicy()
    assert policy.locked_until(1, NOW) is None
    assert policy.locked_until(2, NOW) is None
    assert policy.locked_until(3, NOW) == NOW + timedelta(seconds=1)
    assert policy.locked_until(4, NOW) == NOW + timedelta(seconds=2)
    assert policy.locked_until(6, NOW) == NOW + timedelta(seconds=8)
    assert policy.locked_until(9, NOW) == NOW + timedelta(seconds=64)
    assert policy.locked_until(10, NOW) == NOW + timedelta(minutes=15)
    big = LockoutPolicy(lock_after=1000).locked_until(500, NOW)
    assert big == NOW + LockoutPolicy().max_delay  # capped, no overflow


def test_limit_validation() -> None:
    with pytest.raises(ValueError, match="positive"):
        Limit("x", 0, timedelta(minutes=1))
    with pytest.raises(ValueError, match="scope"):
        Limit("", 1, timedelta(minutes=1))


def test_rate_limit_keys_are_hashed_and_scoped() -> None:
    assert key_hash("login:ip", "192.0.2.1") != key_hash("login:account", "192.0.2.1")
    assert "192.0.2.1" not in key_hash("login:ip", "192.0.2.1")
    assert account_key(None, " Alice ") == "name:alice"


def test_api_token_format_round_trip() -> None:
    material = generate_api_token()
    assert material.raw.startswith(f"pfn_{material.prefix}_")
    assert parse_api_token(material.raw) == material.prefix
    assert material.token_hash == hash_api_token(material.raw)
    assert material.raw not in repr(material)
    assert generate_api_token().raw != material.raw


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "pfn_",
        "pfn_abc_def",
        "xyz_0123456789ab_" + "a" * 43,
        "pfn_0123456789ab_" + "a" * 42,
        "pfn_0123456789AB_" + "a" * 43,
        "pfn_0123456789ab_" + "a" * 43 + "\n",
    ],
)
def test_malformed_tokens_do_not_parse(raw: str) -> None:
    assert parse_api_token(raw) is None


def test_session_ids() -> None:
    raw = new_session_id()
    assert len(raw) == 43
    assert is_well_formed(raw)
    assert len(hash_session_id(raw)) == 64
    assert raw not in hash_session_id(raw)
    assert new_session_id() != raw
    for bad in (None, "", "short", raw + "x", raw[:-1] + "!"):
        assert not is_well_formed(bad)


def test_cookie_names() -> None:
    assert cookie_name(Settings()) == SESSION_COOKIE == "__Host-perfin-session"
    dev = Settings(dev_mode=True, secure_cookies=False)
    assert cookie_name(dev) == DEV_SESSION_COOKIE == "perfin-session-dev"


def test_session_policy_from_settings() -> None:
    policy = SessionPolicy.from_settings(
        Settings(session_idle_minutes=5, session_absolute_hours=2, session_remember_days=7)
    )
    assert policy == SessionPolicy(
        idle=timedelta(minutes=5), absolute=timedelta(hours=2), remember=timedelta(days=7)
    )
    assert SessionPolicy.from_settings(Settings()) == SessionPolicy()


def test_username_normalisation() -> None:
    assert normalize_username("  Alice.Example ") == "alice.example"


def _principal(**kwargs: object) -> Principal:
    user = User(username="synthetic", display_name="Synthetic", is_instance_admin=False)
    base: dict[str, object] = {"user": user, "household_id": None, "role": None}
    base.update(kwargs)
    return Principal(auth_method="session", **base)  # type: ignore[arg-type]


def test_principal_restriction_rules() -> None:
    assert not _principal(mfa_required=False).restricted
    assert _principal(mfa_required=True).restricted
    assert not _principal(mfa_required=True, mfa_enrolled=True).restricted
    assert not _principal(mfa_required=True, mfa_verified_at=NOW).restricted  # IdP amr


def test_principal_scopes() -> None:
    assert _principal().has_scope("anything")  # sessions: all user-level scopes
    token = _principal(scopes=frozenset({"transactions:read"}))
    assert token.has_scope("transactions:read")
    assert not token.has_scope("documents:write")
    assert not _principal(scopes=frozenset()).has_scope("transactions:read")

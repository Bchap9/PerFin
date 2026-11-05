# SPDX-License-Identifier: AGPL-3.0-or-later
"""Personal access token lifecycle over HTTP: create, list, use, revoke (SECURITY.md I3).

The attack-side checks (hash only at rest, constant-time comparison, role
downgrade, deactivation, rate limits) are in
``tests/security/test_token_security.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from app.auth.models import ApiToken, HouseholdRole
from app.auth.tokens import TokenLimits
from app.core import clock
from app.core.settings import Settings
from app.db.rls import set_context
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, household_audit, problem_type

pytestmark = pytest.mark.integration

TOKENS = f"{API}/auth/tokens"


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


def create(actor: AuthedClient, **overrides: Any) -> Any:
    body: dict[str, Any] = {
        "name": "Synthetic script",
        "scopes": ["households:read"],
        "expires_in_days": 30,
    }
    body.update(overrides)
    return actor.post(TOKENS, json=body)


def bearer(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def token_row(factories: Factories, household_id: uuid.UUID, token_id: str) -> ApiToken:
    with factories.transaction() as db:
        set_context(db, household_id)
        row = db.get(ApiToken, uuid.UUID(token_id))
        assert row is not None
        db.expunge(row)
        return row


# ---- create ------------------------------------------------------------------------


def test_create_returns_the_token_once_and_it_works(actors: Actors) -> None:
    owner = actors.new(role="owner")
    before = clock.utcnow()
    response = create(owner, name="  Nightly sync ", scopes=["households:read", "accounts:read"])
    assert response.status_code == 201, response.text
    body = response.json()
    raw = body["token"]
    assert raw.startswith(f"pfn_{body['prefix']}_")
    assert body["name"] == "Nightly sync"
    assert body["scopes"] == ["accounts:read", "households:read"]
    assert body["active_scopes"] == body["scopes"]
    assert body["status"] == "active"
    assert body["last_used_at"] is None
    assert body["revoked_at"] is None
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(days=30) <= expires - before <= timedelta(days=30, minutes=1)

    client = actors.anonymous()
    household_id = owner.record.household_id
    response = client.get(f"{API}/households/{household_id}", headers=bearer(raw))
    assert response.status_code == 200, response.text

    listed = owner.get(TOKENS).json()
    assert [item["id"] for item in listed] == [body["id"]]
    assert "token" not in listed[0]
    assert "token_hash" not in listed[0]
    assert raw not in owner.get(TOKENS).text
    assert listed[0]["last_used_at"] is not None  # the request above touched it


def test_create_is_audited_without_the_secret(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    body = create(owner).json()
    household_id = owner.record.household_id
    assert household_id is not None
    rows = household_audit(factories, household_id, "token.create")
    assert [row.target_id for row in rows] == [body["id"]]
    assert rows[0].user_id == owner.user.id
    assert rows[0].after is not None
    assert rows[0].after["scopes"] == ["households:read"]
    assert rows[0].after["name"] == "Synthetic script"
    # pfn_<12 hex id>_<43 base64url chars>; the secret itself may contain "_".
    secret = body["token"][len("pfn_") + 12 + 1 :]
    assert len(secret) == 43
    assert secret not in str(rows[0].after)
    assert body["token"] not in str(rows[0].after)


def test_create_requires_step_up(actors: Actors) -> None:
    stale = actors.new(role="owner", mfa_verified=False)
    response = create(stale)
    assert (response.status_code, problem_type(response)) == (403, "step-up-required")
    assert stale.get(TOKENS).json() == []  # listing does not need step-up


def test_tokens_cannot_manage_tokens(actors: Actors) -> None:
    owner = actors.new(role="owner")
    raw = create(owner, scopes=["households:read", "settings:read"]).json()["token"]
    client = actors.anonymous()
    body = {"name": "child", "scopes": ["households:read"], "expires_in_days": 1}
    for method, url, payload in (
        ("GET", TOKENS, None),
        ("GET", f"{TOKENS}/scopes", None),
        ("POST", TOKENS, body),
        ("DELETE", f"{TOKENS}/{uuid.uuid4()}", None),
    ):
        response = client.request(method, url, json=payload, headers=bearer(raw))
        assert (response.status_code, problem_type(response)) == (403, "session-required"), url


def test_create_needs_a_household(actors: Actors) -> None:
    loner = actors.new(role=None)
    response = create(loner)
    assert (response.status_code, problem_type(response)) == (403, "forbidden")


@pytest.mark.parametrize(
    ("overrides", "status", "slug"),
    [
        ({"scopes": ["ai:read"]}, 422, "scope-reserved"),
        ({"scopes": ["households:read", "ai:read"]}, 422, "scope-reserved"),
        ({"scopes": ["accounts:read", "accounts:admin"]}, 422, "unknown-scope"),
        ({"scopes": []}, 422, "validation-failed"),
        ({"scopes": ["accounts:read", "accounts:read"]}, 422, "validation-failed"),
        ({"expires_in_days": None}, 422, "validation-failed"),
        ({"expires_in_days": 0}, 422, "validation-failed"),
        ({"expires_in_days": 366}, 422, "validation-failed"),
        ({"expires_in_days": 91}, 422, "expiry-too-long"),
        ({"name": "   "}, 422, "validation-failed"),
        ({"name": "x" * 101}, 422, "validation-failed"),
        ({"name": "bad\nname"}, 422, "validation-failed"),
        ({"household_id": str(uuid.uuid4())}, 422, "validation-failed"),
        ({"token": "pfn_000000000000_" + "A" * 43}, 422, "validation-failed"),
    ],
)
def test_create_rejects_bad_input(
    actors: Actors, overrides: dict[str, Any], status: int, slug: str
) -> None:
    owner = actors.new(role="owner")
    body: dict[str, Any] = {
        "name": "Synthetic script",
        "scopes": ["households:read"],
        "expires_in_days": 30,
    }
    body.update(overrides)
    if overrides.get("expires_in_days", 0) is None:
        body.pop("expires_in_days")
    response = owner.post(TOKENS, json=body)
    assert response.status_code == status, response.text
    assert problem_type(response) == slug
    assert owner.get(TOKENS).json() == []


def test_expiry_is_mandatory(actors: Actors) -> None:
    owner = actors.new(role="owner")
    response = owner.post(TOKENS, json={"name": "forever", "scopes": ["households:read"]})
    assert response.status_code == 422


def test_max_expiry_is_configurable(settings: Settings, factories: Factories) -> None:
    short = settings.model_copy(update={"token_max_days": 7})
    with Actors.open(short, factories) as actors:
        owner = actors.new(role="owner")
        response = create(owner, expires_in_days=8)
        assert (response.status_code, problem_type(response)) == (422, "expiry-too-long")
        assert response.json()["max_days"] == 7
        assert create(owner, expires_in_days=7).status_code == 201
        assert owner.get(f"{TOKENS}/scopes").json()["max_expires_in_days"] == 7


def test_token_max_days_setting_is_capped_at_365() -> None:
    with pytest.raises(ValueError, match="token_max_days"):
        Settings(token_max_days=366)
    assert Settings(token_max_days=365).token_max_days == 365
    assert Settings().token_max_days == 90


@pytest.mark.parametrize(
    ("role", "scope", "allowed"),
    [
        ("viewer", "transactions:read", True),
        ("viewer", "transactions:write", False),
        ("viewer", "households:write", False),
        ("member", "documents:write", True),
        ("member", "households:write", False),
        ("member", "export", False),
        ("admin", "export", True),
        ("owner", "households:write", True),
    ],
)
def test_scopes_limited_to_the_creators_role(
    actors: Actors, role: str, scope: str, allowed: bool
) -> None:
    actor = actors.new(role=role)
    response = create(actor, scopes=[scope])
    if allowed:
        assert response.status_code == 201, response.text
    else:
        assert (response.status_code, problem_type(response)) == (403, "scope-not-allowed")
        assert response.json()["scopes"] == [scope]


def test_scope_catalog_marks_what_my_role_may_grant(actors: Actors) -> None:
    viewer = actors.new(role="viewer")
    body = viewer.get(f"{TOKENS}/scopes").json()
    by_name = {item["scope"]: item for item in body["scopes"]}
    assert by_name["transactions:read"]["grantable"] is True
    assert by_name["transactions:write"]["grantable"] is False
    assert by_name["ai:read"] == {
        "scope": "ai:read",
        "description": by_name["ai:read"]["description"],
        "grantable": False,
        "reserved": True,
    }
    assert body["max_expires_in_days"] == 90
    owner = actors.new(role="owner")
    owner_view = {i["scope"]: i for i in owner.get(f"{TOKENS}/scopes").json()["scopes"]}
    assert owner_view["ai:read"]["grantable"] is False
    assert all(item["grantable"] for name, item in owner_view.items() if name != "ai:read")


def test_active_token_cap(actors: Actors) -> None:
    actors.app.state.token_limits = TokenLimits(max_active=2)
    owner = actors.new(role="owner")
    first = create(owner).json()
    assert create(owner).status_code == 201
    response = create(owner)
    assert (response.status_code, problem_type(response)) == (409, "too-many-tokens")
    assert owner.delete(f"{TOKENS}/{first['id']}").status_code == 204
    assert create(owner).status_code == 201


# ---- list and revoke ----------------------------------------------------------------


def test_list_shows_only_my_tokens_in_the_active_household(
    actors: Actors, factories: Factories
) -> None:
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    other = actors.new(role="member", household=household)
    mine = create(owner).json()
    create(other)
    elsewhere = factories.household()
    factories.member(elsewhere.id, owner.user.id, HouseholdRole.OWNER)
    factories.token(owner.record, household_id=elsewhere.id, scopes=["households:read"])
    assert [item["id"] for item in owner.get(TOKENS).json()] == [mine["id"]]


def test_revoke_kills_the_token_and_is_audited(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    body = create(owner).json()
    client = actors.anonymous()
    url = f"{API}/households/{owner.record.household_id}"
    assert client.get(url, headers=bearer(body["token"])).status_code == 200

    assert owner.delete(f"{TOKENS}/{body['id']}").status_code == 204
    response = client.get(url, headers=bearer(body["token"]))
    assert (response.status_code, problem_type(response)) == (401, "invalid-token")

    listed = owner.get(TOKENS).json()
    assert listed[0]["status"] == "revoked"
    assert listed[0]["revoked_at"] is not None
    assert owner.delete(f"{TOKENS}/{body['id']}").status_code == 204  # idempotent
    household_id = owner.record.household_id
    assert household_id is not None
    assert len(household_audit(factories, household_id, "token.revoke")) == 1


def test_cannot_revoke_someone_elses_token(actors: Actors) -> None:
    owner = actors.new(role="owner")
    household = owner.record.household
    member = actors.new(role="member", household=household)
    theirs = create(member).json()
    for target in (theirs["id"], str(uuid.uuid4())):
        response = owner.delete(f"{TOKENS}/{target}")
        assert (response.status_code, problem_type(response)) == (404, "token-not-found")
    response = actors.anonymous().get(
        f"{API}/households/{household.id if household else ''}", headers=bearer(theirs["token"])
    )
    assert response.status_code == 200


def test_expired_token_is_listed_as_expired_and_refused(
    actors: Actors, factories: Factories
) -> None:
    owner = actors.new(role="owner")
    body = create(owner).json()
    household_id = owner.record.household_id
    assert household_id is not None
    with factories.transaction() as db:
        set_context(db, household_id)
        db.execute(
            update(ApiToken)
            .where(ApiToken.id == uuid.UUID(body["id"]))
            .values(expires_at=clock.utcnow() - timedelta(seconds=1))
        )
    response = actors.anonymous().get(
        f"{API}/households/{household_id}", headers=bearer(body["token"])
    )
    assert (response.status_code, problem_type(response)) == (401, "invalid-token")
    assert owner.get(TOKENS).json()[0]["status"] == "expired"


def test_last_used_at_writes_are_throttled(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    body = create(owner).json()
    household_id = owner.record.household_id
    assert household_id is not None
    client = actors.anonymous()
    url = f"{API}/households/{household_id}"
    assert client.get(url, headers=bearer(body["token"])).status_code == 200
    first = token_row(factories, household_id, body["id"]).last_used_at
    assert first is not None
    assert client.get(url, headers=bearer(body["token"])).status_code == 200
    assert token_row(factories, household_id, body["id"]).last_used_at == first

    earlier = clock.utcnow() - timedelta(minutes=2)
    with factories.transaction() as db:
        set_context(db, household_id)
        db.execute(
            update(ApiToken)
            .where(ApiToken.id == uuid.UUID(body["id"]))
            .values(last_used_at=earlier)
        )
    assert client.get(url, headers=bearer(body["token"])).status_code == 200
    touched = token_row(factories, household_id, body["id"]).last_used_at
    assert touched is not None
    assert touched > earlier


def test_token_rows_belong_to_the_household(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    body = create(owner).json()
    with factories.transaction() as db:
        set_context(db, None)  # no household, no user: RLS hides every token
        assert db.execute(select(ApiToken)).scalars().all() == []
    household_id = owner.record.household_id
    assert household_id is not None
    row = token_row(factories, household_id, body["id"])
    assert (row.household_id, row.user_id) == (household_id, owner.user.id)

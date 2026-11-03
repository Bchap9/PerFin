# SPDX-License-Identifier: AGPL-3.0-or-later
"""Personal access tokens against attack (SECURITY.md I3, review SR-8).

* only the SHA-256 of a token is stored, never the token or its secret;
* the secret is compared in constant time (``hmac.compare_digest``);
* a token acts with at most its user's *current* role: a demotion narrows
  its scopes on the next request, losing the membership or being deactivated
  kills it, and it never reaches another household of the same user;
* failed bearer attempts are rate limited per IP and use per token.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete, text, update

from app.auth.models import HouseholdMember, HouseholdRole, User
from app.auth.ratelimit import Limit
from app.auth.tokens import TokenLimits
from app.core.settings import Settings
from app.db.rls import set_context
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, problem_type

pytestmark = [pytest.mark.integration, pytest.mark.security]

TOKENS = f"{API}/auth/tokens"


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


def mint(actor: AuthedClient, scopes: list[str]) -> dict[str, Any]:
    response = actor.post(
        TOKENS, json={"name": "Synthetic", "scopes": scopes, "expires_in_days": 30}
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def bearer(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def test_only_the_hash_is_stored(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    body = mint(owner, ["households:read"])
    raw = body["token"]
    secret = raw.rsplit("_", 1)[1]
    household_id = owner.record.household_id
    assert household_id is not None
    with factories.transaction() as db:
        set_context(db, household_id)
        stored = db.execute(
            text("SELECT row_to_json(t)::text AS j, t.token_hash FROM api_token t WHERE id = :id"),
            {"id": uuid.UUID(body["id"])},
        ).one()
        audit_rows = db.execute(
            text("SELECT coalesce(after::text, '') FROM audit_log WHERE target_id = :id"),
            {"id": body["id"]},
        ).scalars()
        audit_text = " ".join(audit_rows)
    assert stored.token_hash == hashlib.sha256(raw.encode("ascii")).hexdigest()
    assert raw not in stored.j
    assert secret not in stored.j
    assert audit_text
    assert raw not in audit_text
    assert secret not in audit_text


def test_secret_is_compared_in_constant_time(
    actors: Actors, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = actors.new(role="owner")
    raw = mint(owner, ["households:read"])["token"]
    expected = hashlib.sha256(raw.encode("ascii")).hexdigest().encode()
    calls: list[tuple[Any, Any]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    client = actors.anonymous()
    url = f"{API}/households/{owner.record.household_id}"
    assert client.get(url, headers=bearer(raw)).status_code == 200
    assert any(expected in pair for pair in calls)

    calls.clear()
    wrong = raw[:-4] + ("AAAA" if not raw.endswith("AAAA") else "BBBB")
    assert client.get(url, headers=bearer(wrong)).status_code == 401
    assert any(expected in pair for pair in calls)  # the mismatch went through it too


def _set_role(
    factories: Factories, household_id: uuid.UUID, user_id: uuid.UUID, role: HouseholdRole
) -> None:
    with factories.transaction() as db:
        set_context(db, household_id)
        db.execute(
            update(HouseholdMember)
            .where(HouseholdMember.household_id == household_id, HouseholdMember.user_id == user_id)
            .values(role=role)
        )


def test_demotion_narrows_a_token_and_removal_kills_it(
    actors: Actors, factories: Factories
) -> None:
    owner = actors.new(role="owner")
    household_id = owner.record.household_id
    assert household_id is not None
    body = mint(owner, ["households:read", "households:write"])
    client = actors.anonymous()
    url = f"{API}/households/{household_id}"
    response = client.patch(url, json={"name": "By token"}, headers=bearer(body["token"]))
    assert response.status_code == 200, response.text

    _set_role(factories, household_id, owner.user.id, HouseholdRole.VIEWER)
    response = client.patch(url, json={"name": "Still?"}, headers=bearer(body["token"]))
    assert (response.status_code, problem_type(response)) == (403, "insufficient-scope")
    response = client.get(url, headers=bearer(body["token"]))
    assert response.status_code == 200
    assert response.json()["role"] == "viewer"
    listed = owner.get(TOKENS).json()
    assert listed[0]["scopes"] == ["households:read", "households:write"]
    assert listed[0]["active_scopes"] == ["households:read"]

    # Promoted back: the stored scopes apply again (never more than granted).
    _set_role(factories, household_id, owner.user.id, HouseholdRole.OWNER)
    response = client.patch(url, json={"name": "Again"}, headers=bearer(body["token"]))
    assert response.status_code == 200

    with factories.transaction() as db:
        set_context(db, household_id)
        db.execute(
            delete(HouseholdMember).where(
                HouseholdMember.household_id == household_id,
                HouseholdMember.user_id == owner.user.id,
            )
        )
    response = client.get(url, headers=bearer(body["token"]))
    assert (response.status_code, problem_type(response)) == (401, "invalid-token")


def test_token_never_gains_scopes_it_was_not_given(actors: Actors) -> None:
    owner = actors.new(role="owner")
    raw = mint(owner, ["accounts:read"])["token"]
    client = actors.anonymous()
    url = f"{API}/households/{owner.record.household_id}"
    response = client.get(url, headers=bearer(raw))
    assert (response.status_code, problem_type(response)) == (403, "insufficient-scope")


def test_deactivation_invalidates_tokens_at_once(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    raw = mint(owner, ["households:read"])["token"]
    client = actors.anonymous()
    url = f"{API}/households/{owner.record.household_id}"
    assert client.get(url, headers=bearer(raw)).status_code == 200
    with factories.transaction() as db:
        db.execute(update(User).where(User.id == owner.user.id).values(is_active=False))
    response = client.get(url, headers=bearer(raw))
    assert (response.status_code, problem_type(response)) == (401, "invalid-token")


def test_token_is_bound_to_its_household(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    raw = mint(owner, ["households:read"])["token"]
    other = factories.household()
    factories.member(other.id, owner.user.id, HouseholdRole.OWNER)
    client = actors.anonymous()
    response = client.get(f"{API}/households/{other.id}", headers=bearer(raw))
    assert (response.status_code, problem_type(response)) == (404, "household-not-found")
    response = client.get(f"{API}/households", headers=bearer(raw))
    assert response.status_code == 200
    assert [h["id"] for h in response.json()] == [str(owner.record.household_id)]


def test_failed_bearer_attempts_are_limited_per_ip(actors: Actors) -> None:
    actors.app.state.token_limits = TokenLimits(
        failures_per_ip=Limit("token:fail:ip", 3, timedelta(minutes=5))
    )
    owner = actors.new(role="owner")
    good = mint(owner, ["households:read"])["token"]
    url = f"{API}/households/{owner.record.household_id}"
    attacker = actors.anonymous()  # its own client address
    # A wrong last character (never the real one: a fixed "Q" would be the
    # real token one time in 64).
    near_miss = good[:-1] + ("R" if good.endswith("Q") else "Q")
    for guess in ("pfn_0123456789ab_" + "x" * 43, "bogus", near_miss):
        response = attacker.get(url, headers=bearer(guess))
        assert (response.status_code, problem_type(response)) == (401, "invalid-token")
    response = attacker.get(url, headers=bearer(good))
    assert (response.status_code, problem_type(response)) == (429, "rate-limited")
    assert int(response.headers["retry-after"]) >= 1
    # Another address is unaffected.
    assert actors.anonymous().get(url, headers=bearer(good)).status_code == 200


def test_token_use_is_rate_limited_per_token(actors: Actors) -> None:
    actors.app.state.token_limits = TokenLimits(
        use_per_token=Limit("token:use", 2, timedelta(minutes=1))
    )
    owner = actors.new(role="owner")
    first = mint(owner, ["households:read"])["token"]
    second = mint(owner, ["households:read"])["token"]
    url = f"{API}/households/{owner.record.household_id}"
    client = actors.anonymous()
    assert client.get(url, headers=bearer(first)).status_code == 200
    assert client.get(url, headers=bearer(first)).status_code == 200
    response = client.get(url, headers=bearer(first))
    assert (response.status_code, problem_type(response)) == (429, "rate-limited")
    assert client.get(url, headers=bearer(second)).status_code == 200


def test_bearer_header_never_falls_back_to_the_session(actors: Actors) -> None:
    owner = actors.new(role="owner")
    raw = mint(owner, ["households:read"])["token"]
    owner.delete(f"{TOKENS}/{owner.get(TOKENS).json()[0]['id']}")
    response = owner.get(TOKENS, headers=bearer(raw))  # valid cookie, revoked bearer
    assert (response.status_code, problem_type(response)) == (401, "invalid-token")

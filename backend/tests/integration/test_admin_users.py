# SPDX-License-Identifier: AGPL-3.0-or-later
"""Instance administration of users (``/admin/users``)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select

from app.auth.mfa.recovery import regenerate
from app.auth.models import ApiToken, MfaMethod, User, UserSession
from app.core.settings import Settings
from app.db.rls import set_context
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, household_events, instance_audit, problem_type

pytestmark = [pytest.mark.integration, pytest.mark.security]

URL = f"{API}/admin/users"


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


def _admin(actors: Actors, **kwargs: object) -> AuthedClient:
    return actors.new(role="owner", is_instance_admin=True, **kwargs)  # type: ignore[arg-type]


def _user(factories: Factories, user_id: uuid.UUID) -> User:
    with factories.transaction() as db:
        user = db.get(User, user_id)
        assert user is not None
        db.expunge(user)
        return user


def _live_sessions(factories: Factories, user_id: uuid.UUID) -> int:
    with factories.transaction() as db:
        rows = db.execute(
            select(UserSession.id).where(
                UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
            )
        ).all()
        return len(rows)


# ---- access ------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["owner", "admin", "member", "viewer"])
def test_household_roles_are_not_instance_admins(actors: Actors, role: str) -> None:
    actor = actors.new(role=role)
    target = actors.user(role=None)
    for method, url, body in (
        ("GET", URL, None),
        ("GET", f"{URL}/{target.id}", None),
        ("POST", URL, {"username": "x-new", "display_name": "X", "password": "p" * 20}),
        ("PATCH", f"{URL}/{target.id}", {"is_active": False}),
        ("POST", f"{URL}/{target.id}/mfa-reset", None),
    ):
        response = actor.request(method, url, json=body)
        assert (response.status_code, problem_type(response)) == (403, "forbidden"), url
    assert _user(actors.factories, target.id).is_active


def test_tokens_never_administer(actors: Actors, factories: Factories) -> None:
    admin = actors.user(role="owner", is_instance_admin=True)
    raw = factories.token(admin)
    client = actors.anonymous()
    response = client.get(URL, headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 403


def test_changes_need_step_up(actors: Actors, factories: Factories) -> None:
    admin = actors.user(role="owner", is_instance_admin=True)
    stale = actors.login(admin, mfa_verified=False)
    target = actors.user(role=None, with_totp=True)
    assert stale.get(URL).status_code == 200  # reading does not
    for method, url, body in (
        ("POST", URL, {"username": "x-new2", "display_name": "X", "password": "p" * 20}),
        ("PATCH", f"{URL}/{target.id}", {"is_active": False}),
        ("PATCH", f"{URL}/{target.id}", {"is_breakglass": True}),
        ("POST", f"{URL}/{target.id}/mfa-reset", None),
    ):
        response = stale.request(method, url, json=body)
        assert (response.status_code, problem_type(response)) == (403, "step-up-required"), url
    user = _user(factories, target.id)
    assert user.is_active
    assert not user.is_breakglass


# ---- listing -----------------------------------------------------------------------


def test_list_users_paginates(actors: Actors) -> None:
    admin = _admin(actors)
    created = {actors.user(role=None).id for _ in range(3)}
    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        response = admin.get(URL, params=params)
        assert response.status_code == 200
        page = response.json()
        assert len(page["items"]) <= 2
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert {str(u) for u in created} <= set(seen)
    assert len(seen) == len(set(seen))
    assert seen == sorted(seen, key=uuid.UUID)
    item = admin.get(f"{URL}/{admin.user.id}").json()
    assert item["is_instance_admin"] is True
    assert item["has_password"] is True
    assert item["mfa_methods"] == ["totp"]
    assert not {"password", "password_hash", "totp_secret"} & set(item)
    assert admin.get(URL, params={"limit": 501}).status_code == 422
    assert admin.get(f"{URL}/{uuid.uuid4()}").status_code == 404


# ---- creating ----------------------------------------------------------------------


def test_create_local_user(actors: Actors, factories: Factories) -> None:
    admin = _admin(actors)
    username = f"made-{uuid.uuid4().hex[:8]}"
    body = {
        "username": username,
        "display_name": "Made By Admin",
        "password": "an entirely synthetic passphrase 9",
    }
    response = admin.post(URL, json=body)
    assert response.status_code == 201, response.text
    out = response.json()
    assert out["username"] == username
    assert (out["is_instance_admin"], out["is_breakglass"], out["is_active"]) == (
        False,
        False,
        True,
    )
    assert out["has_password"] is True
    assert out["mfa_methods"] == []
    rows = instance_audit(factories, "user.create", uuid.UUID(out["id"]))
    assert len(rows) == 1
    assert "password" not in str(rows[0].after)
    response = admin.post(URL, json=body)
    assert (response.status_code, problem_type(response)) == (409, "username-taken")


@pytest.mark.parametrize("field", ["is_instance_admin", "is_breakglass", "id", "is_active"])
def test_create_rejects_privileged_fields(actors: Actors, field: str) -> None:
    admin = _admin(actors)
    body = {
        "username": f"priv-{uuid.uuid4().hex[:8]}",
        "display_name": "Priv",
        "password": "an entirely synthetic passphrase 9",
        field: True,
    }
    assert admin.post(URL, json=body).status_code == 422


def test_create_applies_password_policy(actors: Actors) -> None:
    admin = _admin(actors)
    username = f"weak-{uuid.uuid4().hex[:8]}"
    response = admin.post(
        URL, json={"username": username, "display_name": "W", "password": username.upper()}
    )
    assert response.status_code == 422
    assert problem_type(response) == "weak-password"
    assert response.json()["reason"] == "same-as-username"


# ---- activation and break-glass ---------------------------------------------------


def test_deactivate_revokes_sessions_and_tokens(actors: Actors, factories: Factories) -> None:
    admin = _admin(actors)
    target = actors.new(role="member")
    raw = factories.token(target.record)
    response = admin.patch(f"{URL}/{target.user.id}", json={"is_active": False})
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False
    assert _live_sessions(factories, target.user.id) == 0
    assert target.get(f"{API}/auth/session").status_code == 401
    with factories.transaction() as db:
        set_context(db, target.record.household_id)
        token = db.execute(select(ApiToken).where(ApiToken.user_id == target.user.id)).scalar_one()
        assert token.revoked_at is not None
    rows = instance_audit(factories, "user.deactivate", target.user.id)
    assert len(rows) == 1
    assert rows[0].after == {
        "is_active": False,
        "sessions_revoked": 1,
        "pats_revoked": 1,
    }
    # Reactivation does not bring old tokens back.
    response = admin.patch(f"{URL}/{target.user.id}", json={"is_active": True})
    assert response.status_code == 200
    client = actors.anonymous()
    assert (
        client.get(f"{API}/auth/session", headers={"Authorization": f"Bearer {raw}"}).status_code
        == 401
    )
    assert len(instance_audit(factories, "user.activate", target.user.id)) == 1


def test_admin_cannot_change_own_account(actors: Actors) -> None:
    admin = _admin(actors)
    for body in ({"is_active": False}, {"is_breakglass": True}):
        response = admin.patch(f"{URL}/{admin.user.id}", json=body)
        assert (response.status_code, problem_type(response)) == (409, "own-account")
    response = admin.post(f"{URL}/{admin.user.id}/mfa-reset")
    assert (response.status_code, problem_type(response)) == (409, "own-account")


def test_breakglass_flag(actors: Actors, factories: Factories) -> None:
    admin = _admin(actors)
    target = actors.user(role=None)
    response = admin.patch(f"{URL}/{target.id}", json={"is_breakglass": True})
    assert response.status_code == 200, response.text
    assert response.json()["is_breakglass"] is True
    assert _user(factories, target.id).is_breakglass
    rows = instance_audit(factories, "user.breakglass_set", target.id)
    assert rows[0].before == {"is_breakglass": False}
    assert rows[0].after == {
        "is_breakglass": True,
        "via": "api",
        "sessions_revoked": 0,
        "pats_revoked": 0,
    }
    assert rows[0].user_id == admin.user.id
    response = admin.patch(f"{URL}/{target.id}", json={"is_breakglass": False})
    assert response.json()["is_breakglass"] is False
    no_password = actors.user(role=None, with_password=False)
    response = admin.patch(f"{URL}/{no_password.id}", json={"is_breakglass": True})
    assert (response.status_code, problem_type(response)) == (409, "no-local-password")


@pytest.mark.parametrize("enable_first", [False, True])
def test_breakglass_change_ends_the_accounts_sessions(
    actors: Actors, factories: Factories, enable_first: bool
) -> None:
    """A session that predates the flag is not a break-glass login (B7), so it ends."""
    admin = _admin(actors)
    target = actors.new(role="member")
    if enable_first:
        assert admin.patch(f"{URL}/{target.user.id}", json={"is_breakglass": True}).is_success
        target = actors.login(target.record)
    assert _live_sessions(factories, target.user.id) == 1
    response = admin.patch(f"{URL}/{target.user.id}", json={"is_breakglass": not enable_first})
    assert response.status_code == 200, response.text
    assert _live_sessions(factories, target.user.id) == 0
    assert target.get(f"{API}/auth/session").status_code == 401
    rows = instance_audit(factories, "user.breakglass_set", target.user.id)
    assert rows[-1].after == {
        "is_breakglass": not enable_first,
        "via": "api",
        "sessions_revoked": 1,
        "pats_revoked": 0,
    }


def test_email_verified_flag(actors: Actors, factories: Factories) -> None:
    """An instance admin vouches for a local address (OIDC linking needs it, B3)."""
    admin = _admin(actors)
    target = actors.user(role=None, email=f"{uuid.uuid4().hex[:10]}@example.test")
    with factories.transaction() as db:  # as POST /admin/users and setup create them
        user = db.get(User, target.id)
        assert user is not None
        user.email_verified = False
    assert admin.get(f"{URL}/{target.id}").json()["email_verified"] is False
    response = admin.patch(f"{URL}/{target.id}", json={"email_verified": True})
    assert response.status_code == 200, response.text
    assert response.json()["email_verified"] is True
    assert _user(factories, target.id).email_verified
    rows = instance_audit(factories, "user.email_verified_set", target.id)
    assert (rows[0].before, rows[0].after) == ({"email_verified": False}, {"email_verified": True})
    assert rows[0].user_id == admin.user.id
    response = admin.patch(f"{URL}/{target.id}", json={"email_verified": False})
    assert response.json()["email_verified"] is False
    assert not _user(factories, target.id).email_verified

    no_email = actors.user(role=None)
    response = admin.patch(f"{URL}/{no_email.id}", json={"email_verified": True})
    assert (response.status_code, problem_type(response)) == (409, "no-email")
    assert not _user(factories, no_email.id).email_verified
    # Nobody vouches for their own address.
    response = admin.patch(f"{URL}/{admin.user.id}", json={"email_verified": True})
    assert (response.status_code, problem_type(response)) == (409, "own-account")


@pytest.mark.parametrize(
    "body", [{}, {"is_instance_admin": True}, {"is_active": False, "username": "renamed"}]
)
def test_update_rejects_unknown_fields(actors: Actors, factories: Factories, body: object) -> None:
    admin = _admin(actors)
    target = actors.user(role=None)
    assert admin.patch(f"{URL}/{target.id}", json=body).status_code == 422
    user = _user(factories, target.id)
    assert user.is_active
    assert not user.is_instance_admin


# ---- MFA reset ---------------------------------------------------------------------


def test_mfa_reset_removes_factors_and_sessions(actors: Actors, factories: Factories) -> None:
    admin = _admin(actors)
    target = actors.new(role="member")
    with factories.transaction() as db:
        regenerate(db, target.user.id)
    response = admin.post(f"{URL}/{target.user.id}/mfa-reset")
    assert response.status_code == 200, response.text
    assert response.json()["factors_removed"] == 11  # TOTP + 10 recovery codes
    with factories.transaction() as db:
        assert (
            db.execute(select(MfaMethod).where(MfaMethod.user_id == target.user.id)).first() is None
        )
    assert _live_sessions(factories, target.user.id) == 0
    assert target.get(f"{API}/auth/session").status_code == 401
    rows = instance_audit(factories, "user.mfa_reset", target.user.id)
    assert len(rows) == 1
    assert rows[0].user_id == admin.user.id
    assert target.record.household_id is not None
    events = household_events(factories, target.record.household_id, "user.mfa_reset")
    assert [e.payload["user_id"] for e in events] == [str(target.user.id)]
    response = admin.post(f"{URL}/{uuid.uuid4()}/mfa-reset")
    assert (response.status_code, problem_type(response)) == (404, "user-not-found")

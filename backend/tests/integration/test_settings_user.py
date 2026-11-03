# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET/PATCH /settings/user``: display name, locale, theme, time zone."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select

from app.core.settings import Settings
from app.db.models import Setting, SettingScope
from app.db.rls import set_context
from app.domain.households.users import PREFERENCES_KEY
from tests.fixtures.factories import Factories
from tests.fixtures.households import API, Actors, instance_audit, problem_type

pytestmark = pytest.mark.integration

URL = f"{API}/settings/user"


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


def test_defaults(actors: Actors) -> None:
    me = actors.new(role="member", display_name="Synthetic Person")
    response = me.get(URL)
    assert response.status_code == 200
    assert response.json() == {
        "display_name": "Synthetic Person",
        "locale": "en-US",
        "theme": "system",
        "timezone": "UTC",
    }


def test_anonymous_is_401(actors: Actors) -> None:
    assert actors.anonymous().get(URL).status_code == 401


def test_update_and_read_back(actors: Actors, factories: Factories) -> None:
    me = actors.new(role=None)
    response = me.patch(
        URL,
        json={
            "display_name": "  New Name ",
            "locale": "de-DE",
            "theme": "dark",
            "timezone": "Europe/Berlin",
        },
    )
    assert response.status_code == 200, response.text
    expected = {
        "display_name": "New Name",
        "locale": "de-DE",
        "theme": "dark",
        "timezone": "Europe/Berlin",
    }
    assert response.json() == expected
    assert me.get(URL).json() == expected
    assert me.get(f"{API}/auth/session").json()["user"]["display_name"] == "New Name"
    # Partial update keeps the rest.
    assert me.patch(URL, json={"theme": "light"}).json() == {**expected, "theme": "light"}
    rows = instance_audit(factories, "user.preferences_update", me.user.id)
    assert [r.after for r in rows] == [expected, {"theme": "light"}]


def test_preferences_are_per_user(actors: Actors, factories: Factories) -> None:
    alice = actors.new(role="owner")
    bob = actors.new(role="member", household=alice.record.household)
    assert alice.patch(URL, json={"theme": "dark", "timezone": "Asia/Tokyo"}).status_code == 200
    assert bob.get(URL).json()["theme"] == "system"
    # RLS: in Bob's context Alice's user setting is invisible.
    with factories.transaction() as db:
        set_context(db, bob.record.household_id, bob.user.id)
        found = db.execute(
            select(Setting).where(
                Setting.scope == SettingScope.USER,
                Setting.scope_id == alice.user.id,
                Setting.key == PREFERENCES_KEY,
            )
        ).first()
        assert found is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"theme": "neon"},
        {"timezone": "Mars/Olympus_Mons"},
        {"locale": "english"},
        {"display_name": "   "},
        {"display_name": "x" * 201},
        {"is_instance_admin": True},
        {"username": "someone-else"},
        {"theme": "dark", "email": "a@b.test"},
    ],
)
def test_invalid_or_unknown_fields_are_422(actors: Actors, body: dict[str, object]) -> None:
    me = actors.new(role="member")
    response = me.patch(URL, json=body)
    assert response.status_code == 422, body
    assert me.get(URL).json()["theme"] == "system"


def test_tokens_read_but_do_not_change(actors: Actors, factories: Factories) -> None:
    record = actors.user(role="member")
    raw = factories.token(record, scopes=("settings:read",))
    client = actors.anonymous()
    auth = {"Authorization": f"Bearer {raw}"}
    assert client.get(URL, headers=auth).status_code == 200
    response = client.patch(URL, json={"theme": "dark"}, headers=auth)
    assert (response.status_code, problem_type(response)) == (403, "session-required")


def test_tokens_need_the_settings_read_scope(actors: Actors, factories: Factories) -> None:
    record = actors.user(role="member")
    raw = factories.token(record, scopes=("households:read",))
    response = actors.anonymous().get(URL, headers={"Authorization": f"Bearer {raw}"})
    assert (response.status_code, problem_type(response)) == (403, "insufficient-scope")


def test_needs_csrf(actors: Actors) -> None:
    me = actors.new(role="member")
    response = me.client.patch(URL, json={"theme": "dark"})
    assert response.status_code == 403
    assert me.get(URL).json()["theme"] == "system"

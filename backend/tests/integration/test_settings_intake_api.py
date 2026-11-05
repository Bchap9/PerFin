# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/settings/intake`` through the app (API.md; SECURITY.md D1, D2, A6).

* owners and admins read the settings; the password is never returned;
* changing them and rotating the alias need a fresh step-up; members are
  refused; tokens are refused (also covered by the authz matrix);
* enabling without credentials, and allowed senders that are not members,
  are ``422``;
* the connection test logs in to the (fake, TLS) server and reports a
  short outcome; without stored credentials it is ``409``;
* audit rows name the changed fields, never their values or the alias.
"""

from __future__ import annotations

import ipaddress
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.auth.models import HouseholdRole
from app.core.settings import Settings
from app.ingestion.intake.email import ConnectOptions
from tests.fixtures.factories import Factories
from tests.fixtures.fake_imap import (
    IMAP_HOST,
    PASSWORD,
    USERNAME,
    FakeImapServer,
    ImapPki,
    make_pki,
    running_imap,
)
from tests.fixtures.households import API, Actors, household_audit, problem_type

pytestmark = pytest.mark.integration

URL = f"{API}/settings/intake"


@pytest.fixture
def pki(tmp_path: Path) -> ImapPki:
    return make_pki(tmp_path / "pki")


@pytest.fixture
def imap(pki: ImapPki) -> Iterator[FakeImapServer]:
    with running_imap(pki) as server:
        yield server


@pytest.fixture
def actors(settings: Settings, factories: Factories, pki: ImapPki) -> Iterator[Actors]:
    with Actors.open(settings, factories) as opened:
        opened.app.state.mail_connect_options = ConnectOptions(
            proxy=None,
            allow_private=True,
            allow_loopback=True,
            resolver=lambda host, port: [ipaddress.ip_address("127.0.0.1")],
            ssl_context_factory=pki.client_context,
            timeout_seconds=10.0,
        )
        yield opened


def _credentials(port: int, **extra: Any) -> dict[str, Any]:
    return {"host": IMAP_HOST, "port": port, "username": USERNAME, "password": PASSWORD, **extra}


def test_owner_configures_the_mailbox(
    actors: Actors, factories: Factories, imap: FakeImapServer
) -> None:
    owner = actors.new(role="owner")
    household_id = owner.record.household_id
    assert household_id is not None

    first = owner.get(URL)
    assert first.status_code == 200, first.text
    assert first.json()["configured"] is False
    assert first.json()["alias_token"] is None

    response = owner.patch(URL, json=_credentials(imap.port, enabled=True))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"]
    assert body["enabled"]
    assert body["password_set"]
    assert body["username"] == USERNAME
    assert body["host"] == IMAP_HOST
    assert "password" not in body
    assert PASSWORD not in response.text
    assert len(body["alias_token"]) == 16
    assert body["processed_folder"] == "PerFin/Processed"
    assert body["rejected_folder"] == "PerFin/Rejected"
    assert owner.get(URL).json()["alias_token"] == body["alias_token"]

    (row,) = household_audit(factories, household_id, "mail_intake.update")
    assert row.after is not None
    dumped = json.dumps(row.after)
    assert set(row.after["changed"]) == {"host", "port", "username", "password", "enabled"}
    assert PASSWORD not in dumped
    assert USERNAME not in dumped

    check = owner.post(f"{URL}/email/test")
    assert check.status_code == 200, check.text
    assert check.json() == {"ok": True, "outcome": "ok", "messages": 0, "move_supported": True}


def test_connection_test_reports_failures(actors: Actors, imap: FakeImapServer) -> None:
    owner = actors.new(role="owner")
    not_yet = owner.post(f"{URL}/email/test")
    assert not_yet.status_code == 409
    assert problem_type(not_yet) == "mail-intake-not-configured"
    owner.patch(URL, json=_credentials(imap.port, password="wrong"))
    result = owner.post(f"{URL}/email/test").json()
    assert result == {
        "ok": False,
        "outcome": "auth_failed",
        "messages": None,
        "move_supported": None,
    }


def test_enabling_needs_credentials(actors: Actors) -> None:
    owner = actors.new(role="owner")
    response = owner.patch(URL, json={"enabled": True, "host": IMAP_HOST})
    assert response.status_code == 422
    assert problem_type(response) == "mail-intake-incomplete"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"host": "not a host"}, id="host"),
        pytest.param({"host": "imap.example.invalid", "port": 0}, id="port"),
        pytest.param({"inbox_folder": "bad\nfolder"}, id="folder"),
        pytest.param(
            {"allowed_senders": [{"address": "nope", "user_id": str(uuid.uuid4())}]}, id="addr"
        ),
        pytest.param({"password": "x", "extra": 1}, id="extra"),
    ],
)
def test_invalid_input_is_422(actors: Actors, body: dict[str, Any]) -> None:
    owner = actors.new(role="owner")
    assert owner.patch(URL, json=body).status_code == 422


def test_allowed_senders_must_be_members(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    household_id = owner.record.household_id
    assert household_id is not None
    stranger = factories.user(role="owner")
    viewer = factories.user(role=None)
    factories.member(household_id, viewer.user.id, HouseholdRole.VIEWER)
    member = factories.user(role=None)
    factories.member(household_id, member.user.id, HouseholdRole.MEMBER)
    for user_id in (stranger.user.id, viewer.user.id):
        refused = owner.patch(
            URL, json={"allowed_senders": [{"address": "shop@r.invalid", "user_id": str(user_id)}]}
        )
        assert refused.status_code == 422
        assert problem_type(refused) == "invalid-sender-user"
    ok = owner.patch(
        URL,
        json={
            "allowed_senders": [
                {"address": "Shop@R.invalid", "user_id": str(member.user.id)},
                {"address": "shop@r.invalid", "user_id": str(member.user.id)},
            ]
        },
    )
    assert ok.status_code == 200
    assert ok.json()["allowed_senders"] == [
        {"address": "shop@r.invalid", "user_id": str(member.user.id)}
    ]


def test_step_up_and_roles(actors: Actors) -> None:
    owner = actors.new(role="owner")
    household = owner.record.household
    stale = actors.new(role="admin", household=household, mfa_verified=False)
    member = actors.new(role="member", household=household)
    assert stale.get(URL).status_code == 200
    for response in (
        stale.patch(URL, json={"enabled": False}),
        stale.post(f"{URL}/email/rotate-alias"),
    ):
        assert response.status_code == 403
        assert problem_type(response) == "step-up-required"
    for response in (
        member.get(URL),
        member.patch(URL, json={"enabled": False}),
        member.post(f"{URL}/email/test"),
        member.post(f"{URL}/email/rotate-alias"),
    ):
        assert response.status_code == 403


def test_rotate_alias(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    household_id = owner.record.household_id
    assert household_id is not None
    first = owner.post(f"{URL}/email/rotate-alias")
    assert first.status_code == 200, first.text
    second = owner.post(f"{URL}/email/rotate-alias").json()
    assert first.json()["alias_token"] != second["alias_token"]
    assert owner.get(URL).json()["alias_token"] == second["alias_token"]
    rows = household_audit(factories, household_id, "mail_intake.alias_rotate")
    assert len(rows) == 2
    for row in rows:
        assert second["alias_token"] not in json.dumps([row.before, row.after])

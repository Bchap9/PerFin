# SPDX-License-Identifier: AGPL-3.0-or-later
"""A break-glass account cannot leave its network through a token (SECURITY.md B7).

Review P1-SR-01: a break-glass admin could log in from
``PERFIN_BREAKGLASS_CIDRS``, step up and mint a personal access token that
then worked from any address, because a bearer request carries no network
check. Now:

* ``POST /auth/tokens`` refuses a break-glass account (``403
  breakglass-account``), even on a fresh, step-up-verified session from
  inside the CIDR;
* a token that a break-glass account holds anyway (minted before the flag
  was set) is ``401 invalid-token`` from everywhere, inside the CIDR too;
* setting the flag revokes the account's tokens, so clearing it again does
  not bring them back.
"""

from __future__ import annotations

from ipaddress import ip_network
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.models import ApiToken
from app.core.settings import Settings
from app.db.rls import set_context
from app.main import API_PREFIX
from tests.fixtures.auth_clients import browser, build_app, pre_auth_headers, session_headers
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.households import Actors
from tests.security.test_breakglass_sessions import INSIDE, OUTSIDE, ROOMY

pytestmark = [pytest.mark.integration, pytest.mark.security]

LOGIN = f"{API_PREFIX}/auth/login"
VERIFY = f"{API_PREFIX}/auth/mfa/verify"
TOKENS = f"{API_PREFIX}/auth/tokens"
HOUSEHOLDS = f"{API_PREFIX}/households"
ADMIN_USERS = f"{API_PREFIX}/admin/users"


@pytest.fixture
def oidc_only(settings: Settings) -> FastAPI:
    custom = settings.model_copy(
        update={"local_login_enabled": False, "breakglass_cidrs": [ip_network("192.0.2.0/24")]}
    )
    return build_app(custom, limits=ROOMY)


def _problem(response: Any) -> str:
    assert response.headers["content-type"] == "application/problem+json", response.text
    return str(response.json()["type"]).removeprefix("urn:perfin:problem:")


def _bearer(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def _full_login(client: TestClient, record: UserRecord) -> str:
    """Password plus TOTP from ``client``; returns the session's CSRF token."""
    first = client.post(
        LOGIN,
        json={"username": record.user.username, "password": record.password},
        headers=pre_auth_headers(client),
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "mfa_required"
    second = client.post(
        VERIFY,
        json={"method": "totp", "code": record.totp_now()},
        headers=session_headers(first.json()["csrf_token"]),
    )
    assert second.status_code == 200, second.text
    return str(second.json()["csrf_token"])


def _tokens(factories: Factories, record: UserRecord) -> list[ApiToken]:
    household_id = record.household_id
    assert household_id is not None
    with factories.transaction() as db:
        set_context(db, household_id, record.id)
        rows = list(db.execute(select(ApiToken).where(ApiToken.user_id == record.id)).scalars())
        for row in rows:
            db.expunge(row)
        return rows


def test_breakglass_account_cannot_mint_a_token(factories: Factories, oidc_only: FastAPI) -> None:
    glass = factories.user(role="owner", is_instance_admin=True, is_breakglass=True, with_totp=True)
    with browser(oidc_only, ip=INSIDE) as inside:
        csrf = _full_login(inside, glass)
        # The session is fine (and freshly MFA-verified, so step-up passes) ...
        assert inside.get(HOUSEHOLDS).status_code == 200
        response = inside.post(
            TOKENS,
            json={
                "name": "Synthetic script",
                "scopes": ["households:read", "households:write"],
                "expires_in_days": 30,
            },
            headers=session_headers(csrf),
        )
        # ... but a token would carry the account off the allowlisted network.
        assert (response.status_code, _problem(response)) == (403, "breakglass-account")
    assert _tokens(factories, glass) == []


@pytest.mark.parametrize("ip", [OUTSIDE, INSIDE])
def test_breakglass_token_is_refused_from_every_address(
    factories: Factories, oidc_only: FastAPI, ip: str
) -> None:
    """A token the account holds anyway (say, from before the flag) never authenticates."""
    glass = factories.user(role="owner", is_instance_admin=True, is_breakglass=True, with_totp=True)
    raw = factories.token(glass, scopes=["households:read", "households:write"])
    household_id = glass.household_id
    with browser(oidc_only, ip=ip) as client:
        listed = client.get(HOUSEHOLDS, headers=_bearer(raw))
        assert (listed.status_code, _problem(listed)) == (401, "invalid-token")
        renamed = client.patch(
            f"{HOUSEHOLDS}/{household_id}", json={"name": "Renamed"}, headers=_bearer(raw)
        )
        assert (renamed.status_code, _problem(renamed)) == (401, "invalid-token")


def test_ordinary_tokens_still_work_from_any_address(
    factories: Factories, oidc_only: FastAPI
) -> None:
    member = factories.user(role="owner", with_totp=True)
    raw = factories.token(member, scopes=["households:read"])
    for ip in (INSIDE, OUTSIDE):
        with browser(oidc_only, ip=ip) as client:
            assert client.get(HOUSEHOLDS, headers=_bearer(raw)).status_code == 200


def test_setting_the_flag_revokes_the_accounts_tokens(
    settings: Settings, factories: Factories
) -> None:
    with Actors.open(settings, factories) as actors:
        admin = actors.new(role=None, is_instance_admin=True)
        target = actors.user(role="owner")
        raw = factories.token(target, scopes=["households:read"])
        anonymous = actors.anonymous()
        assert anonymous.get(HOUSEHOLDS, headers=_bearer(raw)).status_code == 200

        response = admin.patch(f"{ADMIN_USERS}/{target.id}", json={"is_breakglass": True})
        assert response.status_code == 200, response.text
        rows = _tokens(factories, target)
        assert [row.revoked_at is not None for row in rows] == [True]
        assert anonymous.get(HOUSEHOLDS, headers=_bearer(raw)).status_code == 401

        # Clearing the flag again does not revive the token.
        response = admin.patch(f"{ADMIN_USERS}/{target.id}", json={"is_breakglass": False})
        assert response.status_code == 200, response.text
        assert anonymous.get(HOUSEHOLDS, headers=_bearer(raw)).status_code == 401

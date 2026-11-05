# SPDX-License-Identifier: AGPL-3.0-or-later
"""OIDC login against the fake IdP (AUTH.md "OIDC (Authentik)").

Happy paths and the behaviour around them: JIT provisioning, ``(iss, sub)``
lookup, group-to-role mapping, MFA (local factor, trusted ``amr``), the
pre-auth cookie, session rotation, RP-initiated and back-channel logout,
claim refresh, link by verified email, ``private_key_jwt`` and JWKS rotation.
The refusals live in ``tests/security/test_oidc_negative.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.auth.models import (
    Identity,
    IdentityProvider,
    LoginMethod,
    SessionStage,
    UserSession,
)
from app.auth.oidc.client import OidcClient, PreAuthState
from app.auth.oidc.logout import decrypt_refresh_token, prune
from app.auth.oidc.models import OidcSession, OidcUsedState
from app.auth.oidc.refresh import refresh_claims
from app.auth.sessions import hash_session_id
from app.core import clock
from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.models import Household
from app.main import API_PREFIX
from tests.fixtures.auth_clients import (
    SESSION_URL,
    browser,
    build_app,
    pre_auth_headers,
    session_headers,
)
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.fake_idp import (
    BACKCHANNEL,
    CLIENT_ID,
    GROUP_ADMIN,
    GROUP_MEMBER,
    GROUP_VIEWER,
    ISSUER,
    LINK,
    OIDC_LOGOUT,
    FakeIdP,
    clear_proxy_env,
    login_via_idp,
    mocked_idp,
    oidc_error,
    oidc_settings,
    resolver,
    start,
)

pytestmark = pytest.mark.integration

SESSION_COOKIE = "__Host-perfin-session"
STATE_COOKIE = "__Host-perfin-oidc"
VERIFY = f"{API_PREFIX}/auth/mfa/verify"
LOGIN = f"{API_PREFIX}/auth/login"


@dataclass
class OidcEnv:
    app: FastAPI
    settings: Settings
    household: Household


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeIdP]:
    clear_proxy_env(monkeypatch)
    with mocked_idp() as mock:
        yield mock.idp


@pytest.fixture
def oidc(settings: Settings, factories: Factories) -> Callable[..., OidcEnv]:
    """``oidc(**settings_update)``: an app with OIDC pointed at the fake IdP."""

    def build(**update: Any) -> OidcEnv:
        household = factories.household()
        update.setdefault("oidc_household_id", household.id)
        configured = oidc_settings(settings, **update)
        return OidcEnv(build_app(configured, oidc_resolver=resolver()), configured, household)

    return build


def new_sub() -> str:
    return f"sub-{uuid.uuid4().hex[:12]}"


def claims(sub: str, *groups: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "sub": sub,
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sid": f"sid-{uuid.uuid4().hex[:8]}",
        "preferred_username": f"oidc-{sub[-8:]}",
        "name": "Synthetic Person",
        "email": f"{sub}@example.test",
        "email_verified": True,
        "groups": list(groups),
    }
    out.update(extra)
    return out


def rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params))


def user_by_sub(engine: Engine, sub: str) -> Any:
    found = rows(
        engine,
        "SELECT u.* FROM app_user u JOIN identity i ON i.user_id = u.id "
        "WHERE i.issuer = :iss AND i.subject = :sub",
        iss=ISSUER,
        sub=sub,
    )
    assert len(found) == 1
    return found[0]


def session_row(engine: Engine, client: TestClient) -> Any:
    raw = client.cookies.get(SESSION_COOKIE)
    assert raw
    (row,) = rows(
        engine, "SELECT * FROM user_session WHERE token_hash = :h", h=hash_session_id(raw)
    )
    return row


def membership(engine: Engine, user_id: uuid.UUID, household_id: uuid.UUID) -> Any:
    found = rows(
        engine,
        "SELECT role FROM household_member WHERE user_id = :u AND household_id = :h",
        u=user_id,
        h=household_id,
    )
    return found[0].role if found else None


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


def test_start_redirects_with_pkce_state_nonce_and_sealed_cookie(
    oidc: Callable[..., OidcEnv], idp: FakeIdP
) -> None:
    env = oidc()
    with browser(env.app) as web:
        response = start(web, "/accounts?tab=1")
    assert response.status_code == 303
    parts = urlsplit(response.headers["location"])
    params = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert params["response_type"] == "code"
    assert params["redirect_uri"] == "http://testserver/api/v1/auth/oidc/callback"
    assert params["scope"] == "openid profile email groups"
    assert params["code_challenge_method"] == "S256"
    assert len(params["state"]) >= 43
    assert len(params["nonce"]) >= 43
    cookies = response.headers.get_list("set-cookie")
    (state_cookie,) = [c for c in cookies if c.startswith(f"{STATE_COOKIE}=")]
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=300"):
        assert attribute.lower() in state_cookie.lower()
    assert "domain" not in state_cookie.lower()
    value = state_cookie.split(";", 1)[0].split("=", 1)[1]
    # Sealed: neither the state, the nonce nor the verifier is readable.
    assert params["state"] not in value
    assert params["nonce"] not in value
    client = env.app.state.oidc_client
    assert isinstance(client, OidcClient)
    payload = client.sealer.unseal("state", value)
    pre = PreAuthState.from_payload(payload)
    assert pre is not None
    assert pre.state == params["state"]
    assert pre.return_to == "/accounts?tab=1"
    # A redirect URI or issuer can never come from the request.
    assert client.sealer.unseal("link", value) is None


def test_disabled_oidc_is_404(settings: Settings) -> None:
    with browser(build_app(settings)) as web:
        assert web.get(f"{API_PREFIX}/auth/oidc/status").json() == {"enabled": False}
        response = start(web)
    assert response.status_code == 404
    assert response.json()["type"] == "urn:perfin:problem:oidc-disabled"


# ---------------------------------------------------------------------------
# Login, provisioning, roles
# ---------------------------------------------------------------------------


def test_jit_login_creates_user_identity_membership_and_session(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, admin_engine: Engine
) -> None:
    env = oidc()
    sub = new_sub()
    with browser(env.app) as web:
        response = login_via_idp(web, idp, claims(sub, GROUP_MEMBER), return_to="/budget")
        assert response.status_code == 303, response.text
        assert response.headers["location"] == "/budget"
        set_cookies = response.headers.get_list("set-cookie")
        assert any(c.startswith(f"{SESSION_COOKIE}=") and "HttpOnly" in c for c in set_cookies)
        # The pre-auth cookie is single-use: cleared on the callback.
        assert any(c.startswith(f"{STATE_COOKIE}=") and "Max-Age=0" in c for c in set_cookies)
        me = web.get(SESSION_URL)
        assert me.status_code == 200, me.text
        body = me.json()
        assert body["auth_method"] == "oidc"
        assert body["household"]["id"] == str(env.household.id)
        assert body["household"]["role"] == "member"
        assert body["user"]["is_instance_admin"] is False
        row = session_row(admin_engine, web)
    user = user_by_sub(admin_engine, sub)
    assert user.username == f"oidc-{sub[-8:]}"
    assert user.email_verified is True
    assert user.is_instance_admin is False
    assert row.login_method == LoginMethod.OIDC.value
    assert rows(admin_engine, "SELECT 1 FROM credential WHERE user_id = :u", u=user.id) == []
    (oidc_row,) = rows(admin_engine, "SELECT * FROM oidc_session WHERE session_id = :s", s=row.id)
    assert oidc_row.subject == sub
    assert oidc_row.sid is not None
    # The ID token is kept only encrypted (for id_token_hint); no refresh token
    # without claim refresh; the access token never.
    assert oidc_row.id_token_encrypted is not None
    assert b"eyJ" not in bytes(oidc_row.id_token_encrypted)
    assert oidc_row.refresh_token_encrypted is None
    audit = rows(
        admin_engine,
        "SELECT action, after FROM audit_log WHERE user_id = :u ORDER BY seq",
        u=user.id,
    )
    actions = [a.action for a in audit]
    assert "user.provisioned" in actions
    assert "user.login" in actions
    login = [a for a in audit if a.action == "user.login"][-1]
    assert login.after["method"] == "oidc"
    assert "eyJ" not in str([a.after for a in audit])


def test_existing_identity_logs_in_and_role_follows_groups(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, admin_engine: Engine
) -> None:
    env = oidc(oidc_jit_provisioning=True)
    sub = new_sub()
    with browser(env.app) as web:
        assert login_via_idp(web, idp, claims(sub, GROUP_ADMIN)).status_code == 303
    user = user_by_sub(admin_engine, sub)
    assert membership(admin_engine, user.id, env.household.id) == "admin"
    # Next login: the IdP moved the user to the viewers group.
    env2 = OidcEnv(
        build_app(
            env.settings.model_copy(update={"oidc_jit_provisioning": False}),
            oidc_resolver=resolver(),
        ),
        env.settings,
        env.household,
    )
    with browser(env2.app) as web:
        response = login_via_idp(web, idp, claims(sub, GROUP_VIEWER, GROUP_MEMBER))
        assert response.status_code == 303
        assert oidc_error(response) is None
        assert web.get(SESSION_URL).json()["household"]["role"] == "member"
    assert user_by_sub(admin_engine, sub).id == user.id  # same account, matched by (iss, sub)
    assert membership(admin_engine, user.id, env.household.id) == "member"
    synced = rows(
        admin_engine,
        "SELECT after FROM audit_log WHERE user_id = :u AND action = "
        "'household.member_role_synced' ORDER BY seq",
        u=user.id,
    )
    assert [s.after["after"] for s in synced] == ["admin", "member"]


def test_owner_membership_is_never_changed_by_groups(
    oidc: Callable[..., OidcEnv],
    idp: FakeIdP,
    admin_engine: Engine,
    factories: Factories,
) -> None:
    env = oidc()
    owner = factories.user(role="owner", household=env.household, with_password=False)
    sub = new_sub()
    with factories.transaction() as db:
        db.add(
            Identity(user_id=owner.id, provider=IdentityProvider.OIDC, issuer=ISSUER, subject=sub)
        )
    with browser(env.app) as web:
        response = login_via_idp(web, idp, claims(sub, GROUP_VIEWER))
        assert oidc_error(response) is None
        assert web.get(SESSION_URL).json()["household"]["role"] == "owner"
    assert membership(admin_engine, owner.id, env.household.id) == "owner"


def test_without_jit_an_unknown_identity_is_refused(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, admin_engine: Engine
) -> None:
    env = oidc(oidc_jit_provisioning=False)
    sub = new_sub()
    with browser(env.app) as web:
        response = login_via_idp(web, idp, claims(sub, GROUP_MEMBER))
        assert oidc_error(response) == "no-account"
        assert SESSION_COOKIE not in web.cookies
    assert rows(admin_engine, "SELECT 1 FROM identity WHERE subject = :s", s=sub) == []


def test_return_to_survives_and_mfa_pending_goes_to_the_mfa_page(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, factories: Factories, admin_engine: Engine
) -> None:
    """A user with a local TOTP still needs it after an OIDC login (IdP MFA untrusted)."""
    env = oidc()
    record = factories.user(role="member", household=env.household, with_totp=True)
    sub = new_sub()
    with factories.transaction() as db:
        db.add(
            Identity(user_id=record.id, provider=IdentityProvider.OIDC, issuer=ISSUER, subject=sub)
        )
    with browser(env.app) as web:
        response = login_via_idp(
            web, idp, claims(sub, GROUP_MEMBER, amr=["pwd", "otp"]), return_to="/reports"
        )
        assert response.headers["location"] == "/login/mfa?return_to=/reports"
        pending = session_row(admin_engine, web)
        assert pending.stage == SessionStage.MFA_PENDING.value
        me = web.get(SESSION_URL)
        assert me.status_code == 401
        assert me.json()["type"] == "urn:perfin:problem:mfa-required"
        verified = web.post(
            VERIFY,
            json={"method": "totp", "code": record.totp_now()},
            headers=session_headers(me.json()["csrf_token"]),
        )
        assert verified.status_code == 200, verified.text
        full = session_row(admin_engine, web)
        assert full.stage == SessionStage.AUTHENTICATED.value
        assert full.rotated_from_id == pending.id
    # The OIDC data followed the rotation (back-channel logout still finds it).
    (oidc_row,) = rows(
        admin_engine, "SELECT session_id FROM oidc_session WHERE subject = :s", s=sub
    )
    assert oidc_row.session_id == full.id


def test_trusted_idp_mfa_satisfies_require_mfa(oidc: Callable[..., OidcEnv], idp: FakeIdP) -> None:
    trusted = oidc(require_mfa=True, oidc_trust_idp_mfa=True)
    with browser(trusted.app) as web:
        response = login_via_idp(web, idp, claims(new_sub(), GROUP_MEMBER, amr=["pwd", "hwk"]))
        assert oidc_error(response) is None
        body = web.get(SESSION_URL).json()
        assert body["mfa"]["verified"] is True
        assert body["mfa"]["enrollment_required"] is False
    untrusted = oidc(require_mfa=True, oidc_trust_idp_mfa=False)
    with browser(untrusted.app) as web:
        login_via_idp(web, idp, claims(new_sub(), GROUP_MEMBER, amr=["mfa"]))
        body = web.get(SESSION_URL).json()
        assert body["mfa"]["verified"] is False
        assert body["mfa"]["enrollment_required"] is True


def test_login_replaces_a_presented_session(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, admin_engine: Engine
) -> None:
    env = oidc()
    sub = new_sub()
    with browser(env.app) as web:
        login_via_idp(web, idp, claims(sub, GROUP_MEMBER))
        first = session_row(admin_engine, web)
        login_via_idp(web, idp, claims(sub, GROUP_MEMBER))
        second = session_row(admin_engine, web)
    assert first.id != second.id
    (old,) = rows(admin_engine, "SELECT revoked_reason FROM user_session WHERE id = :i", i=first.id)
    assert old.revoked_reason == "replaced_by_login"


def test_private_key_jwt_client_authentication(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, tmp_path: Path
) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "client-key.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o400)
    idp.auth_method = "private_key_jwt"
    idp.client_public_key = key.public_key()
    env = oidc(oidc_token_auth_method="private_key_jwt", oidc_private_key_file=path)
    with browser(env.app) as web:
        response = login_via_idp(web, idp, claims(new_sub(), GROUP_MEMBER))
        assert oidc_error(response) is None, response.headers["location"]
        assert web.get(SESSION_URL).status_code == 200
    # No secret travelled in a Basic header.
    token_requests = [r for r in idp.requests if r.url.path.endswith("/token/")]
    assert token_requests
    assert "authorization" not in token_requests[-1].headers


def test_jwks_rotation_is_picked_up(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = oidc(oidc_jwks_min_refresh_seconds=10)
    with browser(env.app) as web:
        assert oidc_error(login_via_idp(web, idp, claims(new_sub(), GROUP_MEMBER))) is None
        fetched = idp.jwks_requests
        # The IdP rotates to a new key and publishes it, after PerFin's
        # minimum refresh interval has passed.
        idp.published = ["rsa-other", "ec"]
        idp.sign_with = "rsa-other"
        later = clock.utcnow() + timedelta(seconds=20)
        monkeypatch.setattr(clock, "utcnow", lambda: later)
        response = login_via_idp(web, idp, claims(new_sub(), GROUP_MEMBER))
        assert oidc_error(response) is None
    assert idp.jwks_requests == fetched + 1


def test_es256_tokens_are_accepted(oidc: Callable[..., OidcEnv], idp: FakeIdP) -> None:
    idp.sign_with = "ec"
    env = oidc()
    with browser(env.app) as web:
        assert oidc_error(login_via_idp(web, idp, claims(new_sub(), GROUP_MEMBER))) is None


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def test_rp_initiated_logout(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, admin_engine: Engine
) -> None:
    env = oidc()
    sub = new_sub()
    with browser(env.app) as web:
        login_via_idp(web, idp, claims(sub, GROUP_MEMBER))
        issued = idp.last_id_token
        row = session_row(admin_engine, web)
        csrf = web.get(SESSION_URL).json()["csrf_token"]
        response = web.post(OIDC_LOGOUT, json={}, headers=session_headers(csrf))
        assert response.status_code == 200, response.text
        url = response.json()["redirect_url"]
        assert any(
            c.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" in c
            for c in response.headers.get_list("set-cookie")
        )
    parts = urlsplit(url)
    assert url.startswith("https://idp.example.test/application/o/perfin/end-session/?")
    params = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert params["id_token_hint"] == issued
    assert params["post_logout_redirect_uri"] == "http://testserver/"
    assert params["client_id"] == CLIENT_ID
    (revoked,) = rows(
        admin_engine, "SELECT revoked_reason FROM user_session WHERE id = :i", i=row.id
    )
    assert revoked.revoked_reason == "logout"
    assert rows(admin_engine, "SELECT 1 FROM oidc_session WHERE subject = :s", s=sub) == []


def test_rp_logout_accepts_only_allowlisted_redirects(
    oidc: Callable[..., OidcEnv], idp: FakeIdP
) -> None:
    env = oidc(oidc_post_logout_redirect_uris=["https://perfin.example.test/bye"])
    with browser(env.app) as web:
        login_via_idp(web, idp, claims(new_sub(), GROUP_MEMBER))
        csrf = web.get(SESSION_URL).json()["csrf_token"]
        bad = web.post(
            OIDC_LOGOUT,
            json={"post_logout_redirect_uri": "https://perfin.example.test/bye/../evil"},
            headers=session_headers(csrf),
        )
        assert bad.status_code == 400
        assert bad.json()["type"] == "urn:perfin:problem:invalid-redirect"
        assert web.get(SESSION_URL).status_code == 200  # nothing happened
        good = web.post(
            OIDC_LOGOUT,
            json={"post_logout_redirect_uri": "https://perfin.example.test/bye"},
            headers=session_headers(csrf),
        )
        assert good.status_code == 200
        assert (
            "post_logout_redirect_uri=https%3A%2F%2Fperfin.example.test%2Fbye"
            in (good.json()["redirect_url"])
        )


def test_backchannel_logout_by_sid_revokes_only_that_session(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, admin_engine: Engine
) -> None:
    env = oidc()
    sub = new_sub()
    with browser(env.app) as one, browser(env.app) as two:
        login_via_idp(one, idp, claims(sub, GROUP_MEMBER, sid="sid-one"))
        login_via_idp(two, idp, claims(sub, GROUP_MEMBER, sid="sid-two"))
        response = one.post(BACKCHANNEL, data={"logout_token": idp.logout_token(sid="sid-one")})
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert one.get(SESSION_URL).status_code == 401
        assert two.get(SESSION_URL).status_code == 200


def test_backchannel_logout_by_sub_revokes_every_session(
    oidc: Callable[..., OidcEnv], idp: FakeIdP
) -> None:
    env = oidc()
    sub = new_sub()
    with browser(env.app) as one, browser(env.app) as two, browser(env.app) as idp_server:
        login_via_idp(one, idp, claims(sub, GROUP_MEMBER, sid="a"))
        login_via_idp(two, idp, claims(sub, GROUP_MEMBER, sid="b"))
        # Server to server: no cookies, no CSRF token, no Origin.
        response = idp_server.post(BACKCHANNEL, data={"logout_token": idp.logout_token(sub=sub)})
        assert response.status_code == 200
        assert one.get(SESSION_URL).status_code == 401
        assert two.get(SESSION_URL).status_code == 401


def test_backchannel_logout_reaches_a_rotated_session(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, factories: Factories
) -> None:
    env = oidc()
    record = factories.user(role="member", household=env.household, with_totp=True)
    sub = new_sub()
    with factories.transaction() as db:
        db.add(
            Identity(user_id=record.id, provider=IdentityProvider.OIDC, issuer=ISSUER, subject=sub)
        )
    with browser(env.app) as web:
        login_via_idp(web, idp, claims(sub, GROUP_MEMBER, sid="rot"))
        csrf = web.get(SESSION_URL).json()["csrf_token"]
        verified = web.post(
            VERIFY,
            json={"method": "totp", "code": record.totp_now()},
            headers=session_headers(csrf),
        )
        assert verified.status_code == 200, verified.text
        assert web.get(SESSION_URL).status_code == 200
        web.post(BACKCHANNEL, data={"logout_token": idp.logout_token(sid="rot")})
        assert web.get(SESSION_URL).status_code == 401


# ---------------------------------------------------------------------------
# Claim refresh (B8)
# ---------------------------------------------------------------------------


def _refresh(env: OidcEnv, app_engine: Engine, minutes: int) -> Any:
    client = OidcClient(env.settings, resolver=resolver())
    try:
        with sessionmaker(app_engine)() as db, db.begin():
            return refresh_claims(
                db,
                client,
                Keyring.from_settings(env.settings),
                env.settings,
                now=clock.utcnow() + timedelta(minutes=minutes),
            )
    finally:
        client.close()


def test_claim_refresh_updates_roles_and_revokes_on_invalid_grant(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, app_engine: Engine, admin_engine: Engine
) -> None:
    env = oidc(oidc_claim_refresh_minutes=5)
    sub = new_sub()
    with browser(env.app) as web:
        login_via_idp(web, idp, claims(sub, GROUP_MEMBER))
        row = session_row(admin_engine, web)
        (stored,) = rows(
            admin_engine,
            "SELECT refresh_token_encrypted FROM oidc_session WHERE subject = :s",
            s=sub,
        )
        assert stored.refresh_token_encrypted is not None
        (issued_refresh,) = idp.refresh_tokens
        assert issued_refresh.encode() not in bytes(stored.refresh_token_encrypted)
        user = user_by_sub(admin_engine, sub)

        # Not due yet: nothing happens.
        assert _refresh(env, app_engine, 1).refreshed == 0
        # The IdP moved the user to the viewers group.
        idp.refresh_tokens[issued_refresh].claims["groups"] = [GROUP_VIEWER]
        stats = _refresh(env, app_engine, 6)
        assert (stats.refreshed, stats.revoked) == (1, 0)
        assert membership(admin_engine, user.id, env.household.id) == "viewer"
        assert web.get(SESSION_URL).json()["household"]["role"] == "viewer"
        # The refresh token rotated; the IdP then revokes it.
        (rotated,) = idp.refresh_tokens
        assert rotated != issued_refresh
        idp.refresh_tokens[rotated].revoked = True
        stats = _refresh(env, app_engine, 12)
        assert stats.revoked == 1
        assert web.get(SESSION_URL).status_code == 401
    (revoked,) = rows(
        admin_engine, "SELECT revoked_reason FROM user_session WHERE id = :i", i=row.id
    )
    assert revoked.revoked_reason == "idp_refresh_denied"


def test_claim_refresh_revokes_when_the_group_is_gone(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, app_engine: Engine
) -> None:
    env = oidc(oidc_claim_refresh_minutes=5)
    with browser(env.app) as web:
        login_via_idp(web, idp, claims(new_sub(), GROUP_MEMBER))
        (token,) = idp.refresh_tokens
        idp.refresh_tokens[token].claims["groups"] = ["someone-else"]
        assert _refresh(env, app_engine, 6).revoked == 1
        assert web.get(SESSION_URL).status_code == 401


def test_refresh_token_is_bound_to_its_row(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, app_engine: Engine
) -> None:
    env = oidc(oidc_claim_refresh_minutes=5)
    subs = [new_sub(), new_sub()]
    with browser(env.app) as one, browser(env.app) as two:
        login_via_idp(one, idp, claims(subs[0], GROUP_MEMBER))
        login_via_idp(two, idp, claims(subs[1], GROUP_MEMBER))
    keyring = Keyring.from_settings(env.settings)
    with sessionmaker(app_engine)() as db, db.begin():
        first, second = (
            db.execute(select(OidcSession).where(OidcSession.subject == sub)).scalar_one()
            for sub in subs
        )
        assert decrypt_refresh_token(keyring, first) is not None
        # Copying one row's ciphertext into another row makes it undecryptable.
        second.refresh_token_encrypted = first.refresh_token_encrypted
        assert decrypt_refresh_token(keyring, second) is None
        db.rollback()


# ---------------------------------------------------------------------------
# Link by verified email (B3)
# ---------------------------------------------------------------------------


def _local_login(web: TestClient, record: UserRecord) -> Any:
    return web.post(
        LOGIN,
        json={"username": record.user.username, "password": record.password},
        headers=pre_auth_headers(web),
    )


def _linkable_user(factories: Factories, env: OidcEnv, **kwargs: Any) -> UserRecord:
    email = f"person-{uuid.uuid4().hex[:8]}@example.test"
    return factories.user(role="member", household=env.household, email=email, **kwargs)


def test_link_by_verified_email_after_local_login(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, factories: Factories, admin_engine: Engine
) -> None:
    env = oidc(oidc_link_by_verified_email=True)
    record = _linkable_user(factories, env)
    sub = new_sub()
    with browser(env.app) as web:
        response = login_via_idp(
            web,
            idp,
            claims(sub, GROUP_MEMBER, email=record.user.email.upper()),  # type: ignore[union-attr]
        )
        assert response.headers["location"] == "/login/link-oidc"
        assert SESSION_COOKIE not in web.cookies  # nothing is logged in yet
        offer = web.get(LINK)
        assert offer.status_code == 200
        assert offer.json()["email"].lower() == record.user.email
        assert _local_login(web, record).status_code == 200
        csrf = web.get(SESSION_URL).json()["csrf_token"]
        linked = web.post(LINK, headers=session_headers(csrf))
        assert linked.status_code == 204, linked.text
        assert web.get(LINK).status_code == 404  # the offer cookie is gone
    identity = rows(
        admin_engine,
        "SELECT user_id FROM identity WHERE issuer = :i AND subject = :s",
        i=ISSUER,
        s=sub,
    )
    assert [r.user_id for r in identity] == [record.id]
    # From now on the IdP login lands in that account.
    with browser(env.app) as web:
        assert oidc_error(login_via_idp(web, idp, claims(sub, GROUP_MEMBER))) is None
        assert web.get(SESSION_URL).json()["user"]["id"] == str(record.id)


def test_link_needs_an_admin_verified_local_email(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, factories: Factories
) -> None:
    """Accounts are created with an unverified email; an instance admin vouches for it."""
    from app.auth.models import User
    from app.domain.households.users import set_email_verified

    env = oidc(oidc_link_by_verified_email=True)
    record = _linkable_user(factories, env)
    admin = factories.user(role=None, is_instance_admin=True)
    with factories.transaction() as db:  # as POST /admin/users and /setup create them
        user = db.get(User, record.id)
        assert user is not None
        user.email_verified = False
    email = record.user.email
    with browser(env.app) as web:
        response = login_via_idp(web, idp, claims(new_sub(), GROUP_MEMBER, email=email))
        assert oidc_error(response) == "account-exists"
    with factories.transaction() as db:
        user = db.get(User, record.id)
        assert user is not None
        set_email_verified(db, user, True, actor_id=admin.id, request=None)
    with browser(env.app) as web:
        response = login_via_idp(web, idp, claims(new_sub(), GROUP_MEMBER, email=email))
        assert response.headers["location"] == "/login/link-oidc"


def test_link_needs_mfa_when_the_account_has_a_factor(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, factories: Factories
) -> None:
    env = oidc(oidc_link_by_verified_email=True)
    record = _linkable_user(factories, env, with_totp=True)
    with browser(env.app) as web:
        login_via_idp(web, idp, claims(new_sub(), GROUP_MEMBER, email=record.user.email))
        pending = _local_login(web, record)
        assert pending.json()["status"] == "mfa_required"
        csrf = pending.json()["csrf_token"]
        # A password-only (pending) session cannot link.
        assert web.post(LINK, headers=session_headers(csrf)).status_code == 401
        verified = web.post(
            VERIFY,
            json={"method": "totp", "code": record.totp_now()},
            headers=session_headers(csrf),
        )
        assert verified.status_code == 200
        linked = web.post(LINK, headers=session_headers(verified.json()["csrf_token"]))
        assert linked.status_code == 204, linked.text


def test_link_needs_a_login_after_the_offer(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, factories: Factories
) -> None:
    env = oidc(oidc_link_by_verified_email=True)
    record = _linkable_user(factories, env)
    with browser(env.app) as web:
        assert _local_login(web, record).status_code == 200  # logged in *before* the offer
        clock_csrf = web.get(SESSION_URL).json()["csrf_token"]
        response = login_via_idp(web, idp, claims(new_sub(), GROUP_MEMBER, email=record.user.email))
        assert response.headers["location"] == "/login/link-oidc"
        refused = web.post(LINK, headers=session_headers(clock_csrf))
        assert refused.status_code == 403
        assert refused.json()["reason"] == "reauthentication-required"


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------


def test_prune_drops_ended_sessions_and_old_states(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, app_engine: Engine, admin_engine: Engine
) -> None:
    env = oidc()
    sub = new_sub()
    with browser(env.app) as web:
        login_via_idp(web, idp, claims(sub, GROUP_MEMBER))
        row = session_row(admin_engine, web)
    factory: sessionmaker[Session] = sessionmaker(app_engine)
    with factory() as db, db.begin():
        db.get(UserSession, row.id).revoked_at = clock.utcnow()  # type: ignore[union-attr]
    with factory() as db, db.begin():
        sessions, states = prune(db, now=clock.utcnow() + timedelta(minutes=10))
    assert sessions >= 1
    assert states >= 1
    assert rows(admin_engine, "SELECT 1 FROM oidc_session WHERE subject = :s", s=sub) == []
    with factory() as db, db.begin():
        left = db.execute(
            select(OidcUsedState).where(OidcUsedState.expires_at < clock.utcnow())
        ).all()
    assert left == []


def test_breakglass_and_inactive_accounts_never_log_in_through_oidc(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, factories: Factories
) -> None:
    env = oidc()
    for kwargs, reason in (
        ({"is_breakglass": True, "is_instance_admin": True}, "login-failed"),
        ({"is_active": False}, "account-disabled"),
    ):
        record = factories.user(role="member", household=env.household, **kwargs)
        sub = new_sub()
        with factories.transaction() as db:
            db.add(
                Identity(
                    user_id=record.id, provider=IdentityProvider.OIDC, issuer=ISSUER, subject=sub
                )
            )
        with browser(env.app) as web:
            response = login_via_idp(web, idp, claims(sub, GROUP_ADMIN))
            assert oidc_error(response) == reason
            assert SESSION_COOKIE not in web.cookies


def test_membership_lookup_uses_the_configured_household(
    oidc: Callable[..., OidcEnv], idp: FakeIdP, factories: Factories, admin_engine: Engine
) -> None:
    other = factories.household()
    env = oidc(oidc_household_id=other.id)
    sub = new_sub()
    with browser(env.app) as web:
        login_via_idp(web, idp, claims(sub, GROUP_VIEWER))
    user = user_by_sub(admin_engine, sub)
    assert membership(admin_engine, user.id, other.id) == "viewer"
    assert membership(admin_engine, user.id, env.household.id) is None

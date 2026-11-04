# SPDX-License-Identifier: AGPL-3.0-or-later
"""OIDC negative tests (SECURITY.md A9, B1-B9; docs/SECURITY.md "Testing").

Every attack ends at ``/login?oidc_error=<code>`` with no session cookie, no
new account and an audited reason, or at a ``400`` for back-channel logout.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.auth.models import Identity, IdentityProvider
from app.auth.oidc.logout import logout_jti_hash
from app.auth.oidc.tokens import CLOCK_SKEW, MAX_TOKEN_AGE
from app.auth.ratelimit import Limit
from app.core.settings import Settings
from app.main import API_PREFIX
from tests.fixtures.auth_clients import (
    SESSION_URL,
    browser,
    build_app,
    pre_auth_headers,
    random_ip,
    session_headers,
)
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.fake_idp import (
    BACKCHANNEL,
    CALLBACK,
    CLIENT_ID,
    GROUP_ADMIN,
    GROUP_MEMBER,
    IDP_HOST,
    IDP_IP,
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

pytestmark = [pytest.mark.integration, pytest.mark.security]

SESSION_COOKIE = "__Host-perfin-session"
STATE_COOKIE = "__Host-perfin-oidc"


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_proxy_env(monkeypatch)


@pytest.fixture
def idp() -> Iterator[FakeIdP]:
    with mocked_idp() as mock:
        yield mock.idp


@pytest.fixture
def make_app(settings: Settings, factories: Factories) -> Callable[..., FastAPI]:
    def build(*, resolve: Any = None, **update: Any) -> FastAPI:
        update.setdefault("oidc_household_id", factories.household().id)
        return build_app(oidc_settings(settings, **update), oidc_resolver=resolve or resolver())

    return build


def new_sub() -> str:
    return f"sub-{uuid.uuid4().hex[:12]}"


def claims(sub: str | None = None, *groups: str, **extra: Any) -> dict[str, Any]:
    sub = sub or new_sub()
    out: dict[str, Any] = {
        "sub": sub,
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "preferred_username": f"neg-{sub[-8:]}",
        "email": f"{sub}@example.test",
        "email_verified": True,
        "groups": list(groups) or [GROUP_MEMBER],
    }
    out.update(extra)
    return out


def failed_reasons(engine: Engine, ip: str) -> list[str]:
    with engine.connect() as conn:
        return [
            r.after["reason"]
            for r in conn.execute(
                text(
                    "SELECT after FROM audit_log WHERE action = 'user.login_failed' "
                    "AND ip = CAST(:ip AS inet) ORDER BY seq"
                ),
                {"ip": ip},
            )
        ]


def count(engine: Engine, sql: str, **params: Any) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(sql), params).scalar_one())


def refused(
    app: FastAPI,
    idp: FakeIdP,
    admin_engine: Engine,
    *,
    expect_public: str,
    expect_reason: str | None,
    login_claims: dict[str, Any] | None = None,
) -> None:
    """Run a login that must fail; check the redirect, the cookie and the audit."""
    ip = random_ip()
    with browser(app, ip) as web:
        response = login_via_idp(web, idp, login_claims or claims())
        assert response.status_code == 303
        assert oidc_error(response) == expect_public, response.headers["location"]
        assert SESSION_COOKIE not in web.cookies
        assert web.get(SESSION_URL).status_code == 401
    if expect_reason is not None:
        assert failed_reasons(admin_engine, ip)[-1] == expect_reason


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _replace_header(token: str, header: dict[str, Any], signature: bytes = b"") -> str:
    _, payload, _ = token.split(".")
    return f"{_b64(json.dumps(header).encode())}.{payload}.{_b64(signature)}"


# ---------------------------------------------------------------------------
# ID token validation (A9, B2, B9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FromNow:
    """A timestamp claim relative to when the test *runs*, not when it is collected.

    ``int(time.time()) + 600`` in a ``parametrize`` list is evaluated at
    collection; on a slow single-worker CI run the test executed twelve
    minutes later and the "future" ``iat``/``nbf`` was already in the past.
    """

    seconds: int


def at_run_time(values: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    return {k: now + v.seconds if isinstance(v, FromNow) else v for k, v in values.items()}


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"nonce": "attacker-chosen-nonce"}, "bad-nonce"),
        ({"aud": "some-other-client"}, "invalid-aud"),
        ({"aud": [CLIENT_ID, "some-other-client"]}, "invalid-azp"),
        ({"aud": CLIENT_ID, "azp": "some-other-client"}, "invalid-azp"),
        ({"iss": "https://evil.example.test/application/o/perfin/"}, "invalid-iss"),
        ({"iss": ISSUER.rstrip("/")}, "invalid-iss"),  # exact match, no normalising
        ({"exp": FromNow(-120)}, "expired"),
        ({"iat": FromNow(600), "exp": FromNow(900)}, "iat-in-future"),
        ({"iat": FromNow(-3600)}, "iat-too-old"),
        ({"nbf": FromNow(600)}, "not-yet-valid"),
        ({"at_hash": _b64(hashlib.sha256(b"another access token").digest()[:16])}, "bad-at-hash"),
        ({"sub": ""}, "invalid-sub"),
    ],
)
def test_bad_id_token_claims_are_refused(
    make_app: Callable[..., FastAPI],
    idp: FakeIdP,
    admin_engine: Engine,
    overrides: dict[str, Any],
    reason: str,
) -> None:
    idp.claim_overrides = at_run_time(overrides)
    refused(make_app(), idp, admin_engine, expect_public="token-invalid", expect_reason=reason)


@pytest.mark.parametrize("claim", ["nonce", "exp", "iat"])
def test_missing_required_claims_are_refused(
    make_app: Callable[..., FastAPI], idp: FakeIdP, admin_engine: Engine, claim: str
) -> None:
    idp.remove_claims = {claim}
    expected = "bad-nonce" if claim == "nonce" else f"missing-{claim}"
    refused(make_app(), idp, admin_engine, expect_public="token-invalid", expect_reason=expected)


def test_unknown_kid_refreshes_jwks_once_then_rejects(
    make_app: Callable[..., FastAPI], idp: FakeIdP, admin_engine: Engine
) -> None:
    app = make_app()
    # Warm the cache with a good login.
    with browser(app) as web:
        assert oidc_error(login_via_idp(web, idp, claims())) is None
    fetched = idp.jwks_requests
    # A token signed with a key the IdP never published.
    idp.sign_with = "rsa-other"
    idp.header_overrides = {"kid": "unpublished-key"}
    refused(app, idp, admin_engine, expect_public="token-invalid", expect_reason="unknown-kid")
    assert idp.jwks_requests == fetched  # within the minimum interval: no refetch at all


def test_unknown_kid_refetch_is_rate_limited(
    make_app: Callable[..., FastAPI],
    idp: FakeIdP,
    admin_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import clock

    app = make_app(oidc_jwks_min_refresh_seconds=10)
    with browser(app) as web:
        assert oidc_error(login_via_idp(web, idp, claims())) is None
    base = idp.jwks_requests
    idp.sign_with = "rsa-other"  # signed by a key that is not in the JWKS
    later = clock.utcnow() + timedelta(seconds=30)
    monkeypatch.setattr(clock, "utcnow", lambda: later)
    refused(app, idp, admin_engine, expect_public="token-invalid", expect_reason="unknown-kid")
    assert idp.jwks_requests == base + 1  # refreshed once on the unknown kid
    refused(app, idp, admin_engine, expect_public="token-invalid", expect_reason="unknown-kid")
    assert idp.jwks_requests == base + 1  # and not again within the interval


def test_signature_by_wrong_key_with_published_kid_is_refused(
    make_app: Callable[..., FastAPI], idp: FakeIdP, admin_engine: Engine
) -> None:
    idp.sign_with = "rsa-other"
    idp.header_overrides = {"kid": FakeIdP.kid("rsa")}
    refused(
        make_app(), idp, admin_engine, expect_public="token-invalid", expect_reason="bad-signature"
    )


def test_alg_none_is_refused(
    make_app: Callable[..., FastAPI], idp: FakeIdP, admin_engine: Engine
) -> None:
    idp.token_mutator = lambda token: _replace_header(
        token, {"alg": "none", "typ": "JWT", "kid": FakeIdP.kid("rsa")}
    )
    refused(
        make_app(),
        idp,
        admin_engine,
        expect_public="token-invalid",
        expect_reason="alg-not-allowed",
    )


def test_hs256_signed_with_the_public_key_is_refused(
    make_app: Callable[..., FastAPI], idp: FakeIdP, admin_engine: Engine
) -> None:
    """Classic key confusion: HMAC "signed" with the RSA public key PEM."""
    public_pem = (
        idp.keys["rsa"]
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )

    def forge(token: str) -> str:
        _, payload, _ = token.split(".")
        header = _b64(
            json.dumps({"alg": "HS256", "typ": "JWT", "kid": FakeIdP.kid("rsa")}).encode()
        )
        signature = hmac.new(public_pem, f"{header}.{payload}".encode(), hashlib.sha256).digest()
        return f"{header}.{payload}.{_b64(signature)}"

    idp.token_mutator = forge
    refused(
        make_app(),
        idp,
        admin_engine,
        expect_public="token-invalid",
        expect_reason="alg-not-allowed",
    )


@pytest.mark.parametrize("alg", ["RS512", "PS256", "ES384", "HS512", "EdDSA"])
def test_only_rs256_and_es256_are_accepted(
    make_app: Callable[..., FastAPI], idp: FakeIdP, admin_engine: Engine, alg: str
) -> None:
    idp.token_mutator = lambda token: _replace_header(
        token, {"alg": alg, "kid": FakeIdP.kid("rsa")}, b"sig"
    )
    refused(
        make_app(),
        idp,
        admin_engine,
        expect_public="token-invalid",
        expect_reason="alg-not-allowed",
    )


def test_crit_header_and_embedded_jwk_do_not_help(
    make_app: Callable[..., FastAPI], idp: FakeIdP, admin_engine: Engine
) -> None:
    idp.header_overrides = {"crit": ["exp"], "exp": 1}
    refused(
        make_app(), idp, admin_engine, expect_public="token-invalid", expect_reason="crit-header"
    )
    # A token signed with an attacker key that carries that key in "jwk" is
    # still checked against the issuer's JWKS only.
    idp.header_overrides = {"jwk": idp.jwk("rsa-other"), "kid": FakeIdP.kid("rsa")}
    idp.sign_with = "rsa-other"
    refused(
        make_app(), idp, admin_engine, expect_public="token-invalid", expect_reason="bad-signature"
    )


# ---------------------------------------------------------------------------
# State, PKCE, code (B1)
# ---------------------------------------------------------------------------


def test_state_is_single_use_even_with_the_cookie_replayed(
    make_app: Callable[..., FastAPI], idp: FakeIdP, admin_engine: Engine
) -> None:
    app = make_app()
    ip = random_ip()
    with browser(app, ip) as web:
        started = start(web)
        cookie = web.cookies.get(STATE_COOKIE)
        assert cookie
        code, state = idp.authorize(started.headers["location"], claims())
        first = web.get(CALLBACK, params={"code": code, "state": state}, follow_redirects=False)
        assert oidc_error(first) is None
        assert web.cookies.get(STATE_COOKIE) is None  # cleared by the callback
    # An attacker replays the whole thing (cookie + state + a fresh code).
    with browser(app, ip) as attacker:
        code2, _ = idp.authorize(started.headers["location"], claims())
        replay = attacker.get(
            CALLBACK,
            params={"code": code2, "state": state},
            headers={"Cookie": f"{STATE_COOKIE}={cookie}"},
            follow_redirects=False,
        )
        assert oidc_error(replay) == "state-invalid"
        assert SESSION_COOKIE not in attacker.cookies
    assert failed_reasons(admin_engine, ip)[-1] == "state-reused"


def test_state_mismatch_and_missing_cookie_are_refused(
    make_app: Callable[..., FastAPI], idp: FakeIdP, admin_engine: Engine
) -> None:
    app = make_app()
    ip = random_ip()
    with browser(app, ip) as web:
        started = start(web)
        code, _state = idp.authorize(started.headers["location"], claims())
        wrong = web.get(
            CALLBACK, params={"code": code, "state": "forged-state"}, follow_redirects=False
        )
        assert oidc_error(wrong) == "state-invalid"
    assert failed_reasons(admin_engine, ip)[-1] == "state-mismatch"
    # Login CSRF: a callback URL planted in a victim's browser without the
    # victim's own pre-auth cookie.
    with browser(app, ip) as victim:
        planted = victim.get(
            CALLBACK, params={"code": code, "state": _state}, follow_redirects=False
        )
        assert oidc_error(planted) == "state-invalid"
        assert SESSION_COOKIE not in victim.cookies
    assert failed_reasons(admin_engine, ip)[-1] == "missing-state-cookie"


def test_tampered_or_expired_state_cookie_is_refused(
    make_app: Callable[..., FastAPI],
    idp: FakeIdP,
    admin_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import clock

    app = make_app()
    ip = random_ip()
    with browser(app, ip) as web:
        started = start(web)
        cookie = web.cookies.get(STATE_COOKIE)
        assert cookie
        code, state = idp.authorize(started.headers["location"], claims())
    flipped = cookie[:-2] + ("A" if cookie[-2] != "A" else "B") + cookie[-1]
    with browser(app, ip) as web:
        bad = web.get(
            CALLBACK,
            params={"code": code, "state": state},
            headers={"Cookie": f"{STATE_COOKIE}={flipped}"},
            follow_redirects=False,
        )
        assert oidc_error(bad) == "state-invalid"
        later = clock.utcnow() + timedelta(minutes=6)  # the cookie lives 5 minutes
        monkeypatch.setattr(clock, "utcnow", lambda: later)
        expired = web.get(
            CALLBACK,
            params={"code": code, "state": state},
            headers={"Cookie": f"{STATE_COOKIE}={cookie}"},
            follow_redirects=False,
        )
        assert oidc_error(expired) == "state-invalid"
    assert failed_reasons(admin_engine, ip)[-2:] == ["missing-state-cookie"] * 2


def test_idp_error_is_not_reflected(
    make_app: Callable[..., FastAPI], idp: FakeIdP, admin_engine: Engine
) -> None:
    app = make_app()
    ip = random_ip()
    with browser(app, ip) as web:
        started = start(web)
        _code, state = idp.authorize(started.headers["location"], claims())
        response = web.get(
            CALLBACK,
            params={"error": "<script>alert(1)</script>", "state": state},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/login?oidc_error=idp-error"
    assert failed_reasons(admin_engine, ip)[-1] == "idp:other"


def test_code_injection_fails_pkce(
    make_app: Callable[..., FastAPI], idp: FakeIdP, admin_engine: Engine
) -> None:
    """A code issued for another browser's flow fails the PKCE check at the IdP."""
    app = make_app()
    ip = random_ip()
    with browser(app) as attacker:
        attacker_code, _ = idp.authorize(start(attacker).headers["location"], claims())
    with browser(app, ip) as victim:
        _, victim_state = idp.authorize(start(victim).headers["location"], claims())
        response = victim.get(
            CALLBACK,
            params={"code": attacker_code, "state": victim_state},
            follow_redirects=False,
        )
        assert oidc_error(response) == "idp-error"
        assert SESSION_COOKIE not in victim.cookies
    assert failed_reasons(admin_engine, ip)[-1] == "token-exchange-failed"
    # Every token request PerFin made carried a verifier.
    for request in idp.requests:
        if request.url.path.endswith("/token/"):
            assert b"code_verifier=" in request.content


def test_provider_without_s256_is_refused(make_app: Callable[..., FastAPI], idp: FakeIdP) -> None:
    idp.discovery_overrides = {"code_challenge_methods_supported": ["plain"]}
    with browser(make_app()) as web:
        response = start(web)
        assert oidc_error(response) == "idp-unavailable"
        assert STATE_COOKIE not in web.cookies


def test_callback_is_rate_limited_per_ip(make_app: Callable[..., FastAPI], idp: FakeIdP) -> None:
    app = make_app()
    app.state.oidc_callback_limit = Limit("oidc:ip", 2, timedelta(minutes=5))
    with browser(app) as web:
        for _ in range(2):
            assert oidc_error(web.get(CALLBACK, follow_redirects=False)) == "state-invalid"
        assert oidc_error(web.get(CALLBACK, follow_redirects=False)) == "rate-limited"


# ---------------------------------------------------------------------------
# Open redirects (B4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "return_to",
    [
        "https://evil.example.test/",
        "//evil.example.test/",
        "/\\evil.example.test/",
        "\\\\evil.example.test",
        "/\\/evil.example.test",
        "javascript:alert(1)",
        "http:/evil.example.test",
        "evil.example.test",
        "/%0d%0aSet-Cookie:x=y",
        "/ok\r\nLocation: https://evil.example.test",
        "/" + "a" * 600,
        "/a/..//evil.example.test",
        "/%2e%2e//evil.example.test/x",
    ],
)
def test_return_to_cannot_leave_the_site(
    make_app: Callable[..., FastAPI], idp: FakeIdP, return_to: str
) -> None:
    with browser(make_app()) as web:
        response = login_via_idp(web, idp, claims(), return_to=return_to)
        location = response.headers["location"]
        if return_to == "/%0d%0aSet-Cookie:x=y":
            assert location == return_to  # still a same-origin path, encoded
        else:
            assert location == "/"
        assert web.get(SESSION_URL).status_code == 200


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example.test/",
        "http://testserver/",  # scheme differs from the allowlisted entry
        "http://testserver",
        "http://testserver/?next=https://evil.example.test",
        "//evil.example.test/",
    ],
)
def test_post_logout_redirect_must_be_allowlisted(
    make_app: Callable[..., FastAPI], idp: FakeIdP, uri: str
) -> None:
    app = make_app(oidc_post_logout_redirect_uris=["https://testserver/"])
    with browser(app) as web:
        login_via_idp(web, idp, claims())
        csrf = web.get(SESSION_URL).json()["csrf_token"]
        response = web.post(
            OIDC_LOGOUT, json={"post_logout_redirect_uri": uri}, headers=session_headers(csrf)
        )
        assert response.status_code == 400
        assert web.get(SESSION_URL).status_code == 200


def test_logout_needs_csrf(make_app: Callable[..., FastAPI], idp: FakeIdP) -> None:
    with browser(make_app()) as web:
        login_via_idp(web, idp, claims())
        assert web.post(OIDC_LOGOUT, json={}).status_code == 403
        assert web.get(SESSION_URL).status_code == 200


# ---------------------------------------------------------------------------
# Account takeover and privilege (B3, B6)
# ---------------------------------------------------------------------------


def test_email_collision_cannot_take_over_an_account(
    make_app: Callable[..., FastAPI],
    idp: FakeIdP,
    admin_engine: Engine,
    factories: Factories,
) -> None:
    victim = factories.user(role="owner", email=f"victim-{uuid.uuid4().hex[:6]}@example.test")
    attacker_claims = claims(None, GROUP_ADMIN, email=victim.user.email, email_verified=True)
    identities_before = count(admin_engine, "SELECT count(*) FROM identity")
    # Linking off: refused outright, no duplicate account.
    refused(
        make_app(oidc_link_by_verified_email=False),
        idp,
        admin_engine,
        expect_public="account-exists",
        expect_reason="account-exists",
        login_claims=attacker_claims,
    )
    # Linking on but the IdP does not vouch for the email: refused too.
    refused(
        make_app(oidc_link_by_verified_email=True),
        idp,
        admin_engine,
        expect_public="account-exists",
        expect_reason="account-exists",
        login_claims={**attacker_claims, "email_verified": False, "sub": new_sub()},
    )
    # Linking on, email verified: only an offer, which needs the victim's own
    # local login. The attacker has no session to confirm it with...
    app = make_app(oidc_link_by_verified_email=True)
    with browser(app) as attacker:
        response = login_via_idp(attacker, idp, {**attacker_claims, "sub": new_sub()})
        assert response.headers["location"] == "/login/link-oidc"
        assert SESSION_COOKIE not in attacker.cookies
        assert attacker.get(SESSION_URL).status_code == 401
        confirm = attacker.post(LINK, headers=pre_auth_headers(attacker))
        assert confirm.status_code == 401
        # ...and logging in as someone else does not help.
        own = factories.user(role="member", email=f"att-{uuid.uuid4().hex[:6]}@example.test")
        login = attacker.post(
            f"{API_PREFIX}/auth/login",
            json={"username": own.user.username, "password": own.password},
            headers=pre_auth_headers(attacker),
        )
        assert login.status_code == 200
        mismatch = attacker.post(LINK, headers=session_headers(login.json()["csrf_token"]))
        assert mismatch.status_code == 403
        assert mismatch.json()["reason"] == "account-mismatch"
    # Only the attacker's own local account was added; no IdP identity anywhere.
    assert count(admin_engine, "SELECT count(*) FROM identity") == identities_before + 1
    assert (
        count(
            admin_engine,
            "SELECT count(*) FROM identity WHERE provider = :p AND "
            "(user_id = :u OR lower(email_at_link) = lower(:e))",
            u=victim.id,
            e=victim.user.email,
            p=IdentityProvider.OIDC.value,
        )
        == 0
    )


def test_groups_never_grant_instance_admin_or_owner(
    make_app: Callable[..., FastAPI], idp: FakeIdP, admin_engine: Engine
) -> None:
    sub = new_sub()
    hostile = claims(
        sub,
        GROUP_ADMIN,
        "admins",
        "authentik Admins",
        "perfin-instance-admins",
        "owner",
        is_instance_admin=True,
        is_admin=True,
        roles=["instance_admin", "owner"],
        perfin_role="owner",
    )
    with browser(make_app()) as web:
        response = login_via_idp(web, idp, hostile)
        assert oidc_error(response) is None
        me = web.get(SESSION_URL).json()
        assert me["user"]["is_instance_admin"] is False
        assert me["household"]["role"] == "admin"  # household admin from the mapping, no more
    with admin_engine.connect() as conn:
        flag = conn.execute(
            text(
                "SELECT u.is_instance_admin FROM app_user u JOIN identity i ON i.user_id = u.id "
                "WHERE i.subject = :s"
            ),
            {"s": sub},
        ).scalar_one()
    assert flag is False


def test_require_group_refuses_users_outside_the_mapping(
    make_app: Callable[..., FastAPI], idp: FakeIdP, admin_engine: Engine
) -> None:
    sub = new_sub()
    refused(
        make_app(),
        idp,
        admin_engine,
        expect_public="not-in-group",
        expect_reason="not-in-group",
        login_claims=claims(sub, "unrelated-group"),
    )
    refused(
        make_app(),
        idp,
        admin_engine,
        expect_public="not-in-group",
        expect_reason="not-in-group",
        login_claims={**claims(new_sub()), "groups": "perfin-users"},  # not a list
    )
    assert count(admin_engine, "SELECT count(*) FROM identity WHERE subject = :s", s=sub) == 0


# ---------------------------------------------------------------------------
# SSRF through the issuer configuration (B5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.20.30.40", "::1"])
def test_issuer_resolving_to_internal_addresses_is_never_contacted(
    make_app: Callable[..., FastAPI], idp: FakeIdP, address: str
) -> None:
    app = make_app(resolve=resolver({IDP_HOST: [address]}))
    with browser(app) as web:
        assert oidc_error(start(web)) == "idp-unavailable"
    assert idp.requests == []


def test_allow_private_issuer_reaches_a_lan_idp_but_not_metadata(
    make_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    lan_idp = FakeIdP(expected_ip="10.20.30.40")
    with mocked_idp(lan_idp):
        app = make_app(
            oidc_allow_private_issuer=True, resolve=resolver({IDP_HOST: ["10.20.30.40"]})
        )
        with browser(app) as web:
            assert oidc_error(login_via_idp(web, lan_idp, claims())) is None
        for address in ("169.254.169.254", "127.0.0.1"):
            # Dev mode is on in tests, but loopback still needs the explicit
            # allowance and metadata is never allowed.
            blocked = make_app(
                oidc_allow_private_issuer=address == "169.254.169.254",
                resolve=resolver({IDP_HOST: [address]}),
            )
            with browser(blocked) as web:
                assert oidc_error(start(web)) == "idp-unavailable"


def test_dns_rebinding_between_requests_is_caught(
    make_app: Callable[..., FastAPI], idp: FakeIdP, admin_engine: Engine
) -> None:
    calls: list[str] = []

    def rebinding(host: str, _port: int) -> list[Any]:
        import ipaddress

        calls.append(host)
        # Discovery resolves publicly; the token endpoint then points inside.
        return [ipaddress.ip_address(IDP_IP if len(calls) == 1 else "169.254.169.254")]

    app = make_app(resolve=rebinding)
    refused(
        app, idp, admin_engine, expect_public="idp-error", expect_reason="token-exchange-failed"
    )
    assert len(calls) >= 2
    assert all(r.url.host == IDP_IP for r in idp.requests)


@pytest.mark.parametrize(
    "overrides",
    [
        {"jwks_uri": "http://169.254.169.254/latest/meta-data/"},
        {"token_endpoint": "https://evil.example.test/token"},
        {"authorization_endpoint": "https://evil.example.test/authorize"},
        {"end_session_endpoint": "https://idp.example.test:8443/logout"},
        {"issuer": "https://evil.example.test/application/o/perfin/"},
    ],
)
def test_discovery_cannot_point_elsewhere(
    make_app: Callable[..., FastAPI], idp: FakeIdP, overrides: dict[str, str]
) -> None:
    idp.discovery_overrides = overrides
    with browser(make_app()) as web:
        assert oidc_error(start(web)) == "idp-unavailable"
    assert all(r.url.host == IDP_IP for r in idp.requests)


def test_discovery_redirect_to_another_host_is_refused(
    make_app: Callable[..., FastAPI],
) -> None:
    redirecting = FakeIdP()
    original = redirecting.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            original(request)  # records and checks the request
            return httpx.Response(
                302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
            )
        return original(request)

    redirecting.handle = handle  # type: ignore[method-assign]
    with mocked_idp(redirecting), browser(make_app()) as web:
        assert oidc_error(start(web)) == "idp-unavailable"
    assert redirecting.discovery_requests == 1


# ---------------------------------------------------------------------------
# Back-channel logout (B8)
# ---------------------------------------------------------------------------


def _bcl(client: TestClient, token: str) -> Any:
    return client.post(BACKCHANNEL, data={"logout_token": token})


@pytest.mark.parametrize(
    "mutation",
    [
        {"nonce": "n"},  # an ID token is not a logout token
        {"events": None},
        {"events": {"http://schemas.openid.net/event/other": {}}},
        {"aud": "some-other-client"},
        {"iss": "https://evil.example.test/"},
        {"iat": FromNow(600)},
        {"iat": FromNow(-3600)},
        {"sub": None, "sid": None},
        {"exp": FromNow(-600)},
        {"jti": None},  # REQUIRED by Back-Channel Logout 1.0 (review P1-SR-03)
        {"jti": ""},
        {"jti": 12345},
        {"jti": "j" * 256},
    ],
)
def test_invalid_logout_tokens_are_rejected(
    make_app: Callable[..., FastAPI], idp: FakeIdP, mutation: dict[str, Any]
) -> None:
    app = make_app()
    sub = new_sub()
    with browser(app) as web:
        login_via_idp(web, idp, claims(sub, sid="bcl-sid"))
        claims_ = {"sub": sub, "sid": "bcl-sid", **at_run_time(mutation)}
        response = _bcl(web, idp.logout_token(**claims_))
        assert response.status_code == 400
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json() == {
            "type": "urn:perfin:problem:invalid-logout-token",
            "title": "Invalid logout token",
            "status": 400,
            "error": "invalid_request",
        }
        assert web.get(SESSION_URL).status_code == 200


def test_forged_logout_tokens_are_rejected(make_app: Callable[..., FastAPI], idp: FakeIdP) -> None:
    app = make_app()
    sub = new_sub()
    with browser(app) as web:
        login_via_idp(web, idp, claims(sub, sid="forge-sid"))
        good = idp.logout_token(sub=sub, sid="forge-sid")
        forgeries = [
            idp.logout_token(sub=sub, sid="forge-sid", key="rsa-other"),  # unpublished key
            _replace_header(good, {"alg": "none", "typ": "logout+jwt"}),
            "not-a-jwt",
            good[:-4] + "AAAA",
        ]
        for forged in forgeries:
            assert _bcl(web, forged).status_code == 400
        assert web.get(SESSION_URL).status_code == 200
        typ_wrong = idp.sign(
            {**json.loads(base64.urlsafe_b64decode(good.split(".")[1] + "==")), "jti": "x"},
            headers={"typ": "at+jwt"},
        )
        assert _bcl(web, typ_wrong).status_code == 400
        assert _bcl(web, good).status_code == 200
        assert web.get(SESSION_URL).status_code == 401


def test_logout_token_cannot_be_replayed(make_app: Callable[..., FastAPI], idp: FakeIdP) -> None:
    """Review P1-SR-03: a captured token must not end sessions opened after it."""
    app = make_app()
    sub = new_sub()
    token = idp.logout_token(sub=sub)
    with browser(app) as web, browser(app) as idp_server:
        login_via_idp(web, idp, claims(sub, sid="replay-one"))
        assert _bcl(idp_server, token).status_code == 200
        assert web.get(SESSION_URL).status_code == 401
        # The user logs in again; the same token is posted a second time.
        login_via_idp(web, idp, claims(sub, sid="replay-two"))
        assert web.get(SESSION_URL).status_code == 200
        replay = _bcl(idp_server, token)
        assert replay.status_code == 400
        assert replay.json()["type"] == "urn:perfin:problem:invalid-logout-token"
        assert web.get(SESSION_URL).status_code == 200
        # A fresh token (new jti) for the same subject still works.
        assert _bcl(idp_server, idp.logout_token(sub=sub)).status_code == 200
        assert web.get(SESSION_URL).status_code == 401


def test_failed_revocation_does_not_burn_the_logout_token(
    make_app: Callable[..., FastAPI], idp: FakeIdP, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review P1-SR2-03: the jti is recorded with the revocation, so when
    revoking fails the IdP's retry of the same token still ends the session."""
    from app.auth.oidc.logout import revoke_for_logout_token as real

    calls = {"n": 0}

    def flaky(db: Any, token: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated database failure during revocation")
        return real(db, token)

    monkeypatch.setattr("app.api.routers.auth_oidc.revoke_for_logout_token", flaky)
    app = make_app()
    sub = new_sub()
    token = idp.logout_token(sub=sub)
    with (
        browser(app) as web,
        TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as idp_server,
    ):
        login_via_idp(web, idp, claims(sub, sid="flaky-one"))
        failed = _bcl(idp_server, token)
        assert failed.status_code == 500
        assert web.get(SESSION_URL).status_code == 200  # nothing was revoked
        retried = _bcl(idp_server, token)
        assert retried.status_code == 200, retried.text
        assert web.get(SESSION_URL).status_code == 401
        # And it is still single use afterwards.
        assert _bcl(idp_server, token).status_code == 400
    assert calls["n"] == 2


@pytest.mark.parametrize("first_commits", [True, False], ids=["commit", "rollback"])
def test_concurrent_jti_records_wait_for_the_first_transaction(
    factories: Factories, first_commits: bool
) -> None:
    """Two deliveries of one token in flight: the second waits on the first
    transaction; it is a replay if that committed, and records the jti
    itself if that rolled back (P1-SR2-03)."""
    from concurrent.futures import ThreadPoolExecutor

    from app.auth.oidc.logout import consume_logout_jti
    from app.auth.oidc.tokens import ValidatedToken

    token = ValidatedToken(
        claims={"jti": uuid.uuid4().hex, "iat": int(time.time())},
        issuer=ISSUER,
        subject="s",
        algorithm="RS256",
        kid=None,
    )
    factory = factories.session_factory

    def second() -> bool:
        with factory() as db, db.begin():
            return consume_logout_jti(db, token)

    with factory() as first:
        first.begin()
        assert consume_logout_jti(first, token)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(second)
            time.sleep(0.3)
            assert not pending.done()  # blocked on the first transaction's row
            if first_commits:
                first.commit()
            else:
                first.rollback()
            assert pending.result(timeout=10) is (not first_commits)


def test_logout_token_ids_are_per_issuer_and_expire(
    make_app: Callable[..., FastAPI], idp: FakeIdP, admin_engine: Engine
) -> None:
    """The used-jti row is keyed by (iss, jti), domain-separated, and pruned after use."""
    jti = uuid.uuid4().hex
    with browser(make_app()) as web:
        assert _bcl(web, idp.logout_token(sub=new_sub(), jti=jti)).status_code == 200
    key = logout_jti_hash(ISSUER, jti)
    assert key != logout_jti_hash("https://other.example.test/", jti)
    assert key != hashlib.sha256(jti.encode()).hexdigest()  # not a login state hash
    with admin_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT expires_at - created_at AS ttl FROM oidc_used_state WHERE state_hash = :k"
            ),
            {"k": key},
        ).one()
    assert MAX_TOKEN_AGE < row.ttl <= MAX_TOKEN_AGE + CLOCK_SKEW + timedelta(seconds=5)


def test_logout_token_for_another_issuer_session_does_nothing(
    make_app: Callable[..., FastAPI], idp: FakeIdP
) -> None:
    app = make_app()
    with browser(app) as web:
        login_via_idp(web, idp, claims(new_sub(), sid="mine"))
        response = _bcl(web, idp.logout_token(sub=new_sub(), sid="someone-else"))
        assert response.status_code == 200
        assert web.get(SESSION_URL).status_code == 200


def test_backchannel_parse_limit(make_app: Callable[..., FastAPI], idp: FakeIdP) -> None:
    with browser(make_app()) as web:
        assert _bcl(web, "x" * (16 * 1024 + 1)).status_code == 422


def test_start_link_and_redirect_queries_reflect_nothing(
    make_app: Callable[..., FastAPI], idp: FakeIdP
) -> None:
    with browser(make_app()) as web:
        response = start(web, return_to="/ok")
        params = parse_qs(urlsplit(response.headers["location"]).query)
        # The redirect URI is the configured one, whatever the request says.
        assert params["redirect_uri"] == ["http://testserver/api/v1/auth/oidc/callback"]
        hijack = web.get(
            f"{API_PREFIX}/auth/oidc/start",
            params={"redirect_uri": "https://evil.example.test/cb"},
            headers={"Host": "evil.example.test"},
            follow_redirects=False,
        )
        hijacked = parse_qs(urlsplit(hijack.headers["location"]).query)
        assert hijacked["redirect_uri"] == ["http://testserver/api/v1/auth/oidc/callback"]


# ---------------------------------------------------------------------------
# Trusted IdP MFA and step-up freshness (A6, review P1-SR2-01)
# ---------------------------------------------------------------------------

TOKENS = f"{API_PREFIX}/auth/tokens"
STEP_UP = f"{API_PREFIX}/auth/step-up"
NEW_TOKEN = {"name": "Probe", "scopes": ["households:read"], "expires_in_days": 1}


def _owner_with_totp_linked(factories: Factories) -> tuple[UserRecord, str]:
    """A household owner with a PerFin TOTP factor, linked to a new IdP subject."""
    record = factories.user(role="owner", with_totp=True)
    sub = new_sub()
    with factories.transaction() as db:
        db.add(
            Identity(user_id=record.id, provider=IdentityProvider.OIDC, issuer=ISSUER, subject=sub)
        )
    return record, sub


def test_stale_idp_mfa_is_not_a_fresh_step_up(
    make_app: Callable[..., FastAPI], idp: FakeIdP, factories: Factories
) -> None:
    """An SSO re-login whose IdP MFA (``auth_time``) is 30 days old satisfies
    MFA for the login, but no step-up action opens until a fresh factor."""
    record, sub = _owner_with_totp_linked(factories)
    household_id = record.household_id
    assert household_id is not None
    app = make_app(oidc_household_id=household_id, oidc_trust_idp_mfa=True)
    month_ago = int(time.time()) - 30 * 24 * 3600
    with browser(app) as web:
        callback = login_via_idp(
            web, idp, claims(sub, GROUP_MEMBER, amr=["pwd", "mfa"], auth_time=month_ago)
        )
        assert oidc_error(callback) is None
        assert callback.headers["location"] == "/"  # trusted: no PerFin MFA page
        me = web.get(SESSION_URL).json()
        assert me["household"]["role"] == "owner"
        assert me["mfa"]["verified"] is True
        verified_at = me["mfa"]["verified_at"]
        assert verified_at.startswith(time.strftime("%Y-%m-%d", time.gmtime(month_ago)))
        headers = session_headers(me["csrf_token"])
        refused_token = web.post(TOKENS, json=NEW_TOKEN, headers=headers)
        assert (refused_token.status_code, refused_token.json()["type"]) == (
            403,
            "urn:perfin:problem:step-up-required",
        )
        other = factories.user(role=None)
        refused_member = web.post(
            f"{API_PREFIX}/households/{household_id}/members",
            json={"user_id": str(other.id), "role": "member"},
            headers=headers,
        )
        assert refused_member.status_code == 403
        assert refused_member.json()["type"] == "urn:perfin:problem:step-up-required"
        # The PerFin factor (the documented mitigation) gives the step-up.
        stepped = web.post(
            STEP_UP, json={"method": "totp", "code": record.totp_now()}, headers=headers
        )
        assert stepped.status_code == 200, stepped.text
        created = web.post(
            TOKENS, json=NEW_TOKEN, headers=session_headers(stepped.json()["csrf_token"])
        )
        assert created.status_code == 201, created.text


@pytest.mark.parametrize(
    "auth_time",
    [None, "not-a-number", True, 10**20],
    ids=["missing", "string", "bool", "out-of-range"],
)
def test_idp_mfa_without_usable_auth_time_is_never_a_step_up(
    make_app: Callable[..., FastAPI], idp: FakeIdP, auth_time: Any
) -> None:
    """PerFin cannot tell when the IdP did MFA: it counts for the login only,
    even for a forced sign-in (``reauth=true``)."""
    app = make_app(require_mfa=True, oidc_trust_idp_mfa=True)
    extra: dict[str, Any] = {} if auth_time is None else {"auth_time": auth_time}
    with browser(app) as web:
        callback = login_via_idp(web, idp, claims(new_sub(), amr=["mfa"], **extra), reauth=True)
        assert oidc_error(callback) is None
        me = web.get(SESSION_URL).json()
        assert me["mfa"]["verified"] is True
        assert me["mfa"]["enrollment_required"] is False  # PERFIN_REQUIRE_MFA is satisfied
        refused_token = web.post(TOKENS, json=NEW_TOKEN, headers=session_headers(me["csrf_token"]))
        assert refused_token.status_code == 403
        assert refused_token.json()["type"] == "urn:perfin:problem:step-up-required"


def test_plain_idp_login_with_a_current_auth_time_is_no_step_up(
    make_app: Callable[..., FastAPI], idp: FakeIdP, factories: Factories
) -> None:
    """Review P1-SR3-01: some IdPs stamp ``auth_time`` = now on a silent SSO
    answer. A login not started with ``reauth=true`` therefore never earns a
    step-up, however current its ``auth_time``: MFA counts for the login
    only, and the session is dated just outside the step-up window."""
    record, sub = _owner_with_totp_linked(factories)
    app = make_app(oidc_household_id=record.household_id, oidc_trust_idp_mfa=True)
    with browser(app) as web:
        callback = login_via_idp(
            web, idp, claims(sub, amr=["pwd", "mfa"], auth_time=int(time.time()))
        )
        assert oidc_error(callback) is None
        assert callback.headers["location"] == "/"  # trusted: no PerFin MFA page
        me = web.get(SESSION_URL).json()
        assert me["mfa"]["verified"] is True
        refused_token = web.post(TOKENS, json=NEW_TOKEN, headers=session_headers(me["csrf_token"]))
        assert (refused_token.status_code, refused_token.json()["type"]) == (
            403,
            "urn:perfin:problem:step-up-required",
        )


def test_idp_mfa_step_up_window_runs_from_auth_time(
    make_app: Callable[..., FastAPI],
    idp: FakeIdP,
    factories: Factories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forced IdP sign-in (``reauth=true``) is a step-up for 10 minutes
    after its ``auth_time``: 9 minutes later still passes, 11 do not."""
    from app.core import clock

    record, sub = _owner_with_totp_linked(factories)
    app = make_app(oidc_household_id=record.household_id, oidc_trust_idp_mfa=True)
    real_utcnow = clock.utcnow
    for minutes, expected in ((9, 201), (11, 403)):
        monkeypatch.setattr(clock, "utcnow", real_utcnow)
        with browser(app) as web:
            callback = login_via_idp(
                web, idp, claims(sub, amr=["mfa"], auth_time=int(time.time())), reauth=True
            )
            assert oidc_error(callback) is None
            csrf = web.get(SESSION_URL).json()["csrf_token"]
            later = real_utcnow() + timedelta(minutes=minutes)
            monkeypatch.setattr(clock, "utcnow", lambda later=later: later)
            response = web.post(TOKENS, json=NEW_TOKEN, headers=session_headers(csrf))
            assert response.status_code == expected, (minutes, response.text)


@pytest.mark.parametrize(
    ("seconds_before_start", "expected"),
    [(CLOCK_SKEW.total_seconds() - 5, 201), (CLOCK_SKEW.total_seconds() + 5, 403), (180, 403)],
    ids=["within-clock-skew", "just-beyond-clock-skew", "ignored-reauth"],
)
def test_reauth_counts_only_with_an_auth_time_from_this_login(
    make_app: Callable[..., FastAPI],
    idp: FakeIdP,
    factories: Factories,
    seconds_before_start: float,
    expected: int,
) -> None:
    """An IdP that ignores ``prompt=login`` answers from its SSO session with
    the old ``auth_time``: older than the reauth start (minus the clock
    skew), it is no step-up even if it is less than 10 minutes old."""
    record, sub = _owner_with_totp_linked(factories)
    app = make_app(oidc_household_id=record.household_id, oidc_trust_idp_mfa=True)
    auth_time = int(time.time() - seconds_before_start)
    with browser(app) as web:
        callback = login_via_idp(
            web, idp, claims(sub, amr=["pwd", "otp"], auth_time=auth_time), reauth=True
        )
        assert oidc_error(callback) is None
        me = web.get(SESSION_URL).json()
        assert me["mfa"]["verified"] is True
        response = web.post(TOKENS, json=NEW_TOKEN, headers=session_headers(me["csrf_token"]))
        assert response.status_code == expected, response.text


def test_reauth_start_asks_the_idp_to_authenticate_again(
    make_app: Callable[..., FastAPI], idp: FakeIdP
) -> None:
    """``reauth=true`` is the IdP step-up: ``prompt=login`` and ``max_age=0``."""
    with browser(make_app(oidc_trust_idp_mfa=True)) as web:
        plain = parse_qs(urlsplit(start(web, return_to="/x").headers["location"]).query)
        assert "prompt" not in plain
        assert "max_age" not in plain
        response = web.get(
            f"{API_PREFIX}/auth/oidc/start",
            params={"return_to": "/settings/security", "reauth": "true"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        params = parse_qs(urlsplit(response.headers["location"]).query)
        assert params["prompt"] == ["login"]
        assert params["max_age"] == ["0"]
        # The IdP authenticated just now: the new session is a fresh step-up.
        code, state = idp.authorize(
            response.headers["location"],
            claims(new_sub(), amr=["pwd", "otp"], auth_time=int(time.time())),
        )
        callback = web.get(CALLBACK, params={"code": code, "state": state}, follow_redirects=False)
        assert callback.headers["location"] == "/settings/security"
        csrf = web.get(SESSION_URL).json()["csrf_token"]
        assert web.post(TOKENS, json=NEW_TOKEN, headers=session_headers(csrf)).status_code == 201

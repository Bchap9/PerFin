# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake OpenID Connect provider for tests (synthetic keys, no network).

:class:`FakeIdP` signs tokens with freshly generated RSA-2048 and EC P-256
keys and answers the discovery, JWKS and token endpoints through ``respx``
(:meth:`FakeIdP.handle` is the side effect of one route on
:data:`IDP_IP`). The PerFin HTTP client resolves :data:`IDP_HOST` with
:func:`resolver` and connects to the vetted address, so the requests arrive
addressed to :data:`IDP_IP` with ``Host: idp.example.test`` and the TLS
server name set; :meth:`FakeIdP.handle` asserts both.

The browser half of the flow is :meth:`FakeIdP.authorize`: it takes the
authorization URL PerFin redirected to, checks it like a strict provider
would (``response_type=code``, the client id, the registered redirect URI,
PKCE ``S256``) and returns ``(code, state)`` for the callback. The token
endpoint enforces client authentication, single-use codes, the redirect URI
and the PKCE verifier.

Negative tests change what is issued through :attr:`FakeIdP.claim_overrides`,
:attr:`FakeIdP.header_overrides`, :attr:`FakeIdP.sign_with` or
:attr:`FakeIdP.token_mutator`.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import secrets
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.http import IPAddress
from app.core.settings import Settings
from app.main import API_PREFIX

IDP_HOST = "idp.example.test"
#: A global unicast address; respx answers it, nothing ever leaves the host.
IDP_IP = "151.101.10.10"
ISSUER = f"https://{IDP_HOST}/application/o/perfin/"
BASE = f"https://{IDP_HOST}/application/o"
CLIENT_ID = "perfin-test-client"
CLIENT_SECRET = "synthetic-oidc-client-secret-" + "q7Lm2Xv9"
GROUP_ADMIN = "perfin-admins"
GROUP_MEMBER = "perfin-users"
GROUP_VIEWER = "perfin-viewers"

KeyName = Literal["rsa", "ec", "rsa-other", "ec-other"]


def oidc_settings(base: Settings, **update: Any) -> Settings:
    """``base`` (the conftest settings) with OIDC pointed at the fake IdP."""
    values: dict[str, Any] = {
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": SecretStr(CLIENT_SECRET),
        "oidc_group_admin": GROUP_ADMIN,
        "oidc_group_member": GROUP_MEMBER,
        "oidc_group_viewer": GROUP_VIEWER,
        "oidc_require_group": True,
        "oidc_jit_provisioning": True,
        "oidc_jwks_min_refresh_seconds": 300,
        "require_mfa": False,
    }
    values.update(update)
    return base.model_copy(update=values)


def resolver(mapping: dict[str, list[str]] | None = None) -> Callable[[str, int], list[IPAddress]]:
    """A resolver answering :data:`IDP_HOST` (and ``mapping``) without DNS."""
    table = {IDP_HOST: [IDP_IP], **(mapping or {})}

    def resolve(host: str, _port: int) -> list[IPAddress]:
        try:
            return [ipaddress.ip_address(a) for a in table[host]]
        except KeyError:
            raise AssertionError(f"unexpected DNS lookup for {host}") from None

    return resolve


def clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sandbox has HTTPS_PROXY set; the OIDC client would honour it."""
    for name in (
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def at_hash_for(access_token: str) -> str:
    digest = hashlib.sha256(access_token.encode("ascii")).digest()
    return _b64(digest[:16])


@dataclass
class CodeGrant:
    nonce: str
    challenge: str
    redirect_uri: str
    claims: dict[str, Any]


@dataclass
class RefreshGrant:
    claims: dict[str, Any]
    revoked: bool = False


@dataclass
class FakeIdP:
    """See the module doc."""

    keys: dict[str, Any] = field(default_factory=dict)
    published: list[str] = field(default_factory=lambda: ["rsa", "ec"])
    sign_with: KeyName = "rsa"
    claim_overrides: dict[str, Any] = field(default_factory=dict)
    header_overrides: dict[str, Any] = field(default_factory=dict)
    remove_claims: set[str] = field(default_factory=set)
    #: ``mutator(id_token) -> id_token`` applied last (e.g. alg=none forgery).
    token_mutator: Callable[[str], str] | None = None
    include_at_hash: bool = True
    discovery_overrides: dict[str, Any] = field(default_factory=dict)
    auth_method: str = "client_secret_basic"
    client_public_key: Any = None
    codes: dict[str, CodeGrant] = field(default_factory=dict)
    refresh_tokens: dict[str, RefreshGrant] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    expected_ip: str = IDP_IP
    last_id_token: str | None = None
    discovery_requests: int = 0
    jwks_requests: int = 0
    token_requests: int = 0

    def __post_init__(self) -> None:
        self.keys = {
            "rsa": rsa.generate_private_key(public_exponent=65537, key_size=2048),
            "ec": ec.generate_private_key(ec.SECP256R1()),
            "rsa-other": rsa.generate_private_key(public_exponent=65537, key_size=2048),
            "ec-other": ec.generate_private_key(ec.SECP256R1()),
        }

    # ---- documents -----------------------------------------------------------
    def discovery(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{BASE}/authorize/",
            "token_endpoint": f"{BASE}/token/",
            "userinfo_endpoint": f"{BASE}/userinfo/",
            "end_session_endpoint": f"{BASE}/perfin/end-session/",
            "jwks_uri": f"{BASE}/perfin/jwks/",
            "response_types_supported": ["code", "id_token", "id_token token"],
            "id_token_signing_alg_values_supported": ["RS256", "ES256"],
            "code_challenge_methods_supported": ["plain", "S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "private_key_jwt"],
            "backchannel_logout_supported": True,
        }
        doc.update(self.discovery_overrides)
        return doc

    @staticmethod
    def kid(name: str) -> str:
        return f"{name}-key"

    def jwk(self, name: str) -> dict[str, Any]:
        key = self.keys[name]
        algorithm = "RS256" if name.startswith("rsa") else "ES256"
        data = json.loads(
            jwt.algorithms.get_default_algorithms()[algorithm].to_jwk(key.public_key())
        )
        data.update({"kid": self.kid(name), "use": "sig", "alg": algorithm})
        return dict(data)

    def jwks(self) -> dict[str, Any]:
        return {"keys": [self.jwk(name) for name in self.published]}

    # ---- signing ---------------------------------------------------------------
    def sign(
        self,
        claims: dict[str, Any],
        *,
        key: KeyName | None = None,
        headers: dict[str, Any] | None = None,
    ) -> str:
        name = key or self.sign_with
        algorithm = "RS256" if name.startswith("rsa") else "ES256"
        merged = {"kid": self.kid(name), **(headers or {})}
        return jwt.encode(claims, self.keys[name], algorithm=algorithm, headers=merged)

    def base_claims(self, sub: str, **claims: Any) -> dict[str, Any]:
        now = int(time.time())
        out: dict[str, Any] = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": sub,
            "iat": now,
            "exp": now + 300,
            "auth_time": now,
            "sid": f"sid-{sub}",
        }
        out.update(claims)
        return out

    def issue_id_token(
        self, claims: dict[str, Any], *, nonce: str | None, access_token: str
    ) -> str:
        body = dict(claims)
        now = int(time.time())
        body["iat"], body["exp"] = now, now + 300
        if nonce is not None:
            body["nonce"] = nonce
        if self.include_at_hash:
            body["at_hash"] = at_hash_for(access_token)
        body.update(self.claim_overrides)
        for name in self.remove_claims:
            body.pop(name, None)
        token = self.sign(body, headers=self.header_overrides or None)
        if self.token_mutator is not None:
            token = self.token_mutator(token)
        self.last_id_token = token
        return token

    def logout_token(self, *, key: KeyName = "rsa", **claims: Any) -> str:
        now = int(time.time())
        body: dict[str, Any] = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 120,
            "jti": uuid.uuid4().hex,
            "events": {"http://schemas.openid.net/event/backchannel-logout": {}},
        }
        body.update(claims)
        for name in [k for k, v in body.items() if v is None]:
            body.pop(name)
        return self.sign(body, key=key, headers={"typ": "logout+jwt"})

    # ---- browser side ---------------------------------------------------------
    def authorize(self, auth_url: str, claims: dict[str, Any]) -> tuple[str, str]:
        """Check the authorization request; return ``(code, state)``."""
        parts = urlsplit(auth_url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{BASE}/authorize/"
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert params["response_type"] == "code"
        assert params["client_id"] == CLIENT_ID
        assert params["code_challenge_method"] == "S256"
        assert "openid" in params["scope"].split()
        code = secrets.token_urlsafe(24)
        self.codes[code] = CodeGrant(
            nonce=params["nonce"],
            challenge=params["code_challenge"],
            redirect_uri=params["redirect_uri"],
            claims=dict(claims),
        )
        return code, params["state"]

    # ---- endpoints ---------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["host"] == IDP_HOST, "the Host header must stay the hostname"
        assert request.url.host == self.expected_ip, "the client must connect to the vetted address"
        assert request.extensions.get("sni_hostname") == IDP_HOST
        self.requests.append(request)
        path = request.url.path
        if path == "/application/o/perfin/.well-known/openid-configuration":
            self.discovery_requests += 1
            return httpx.Response(200, json=self.discovery())
        if path == "/application/o/perfin/jwks/":
            self.jwks_requests += 1
            return httpx.Response(200, json=self.jwks())
        if path == "/application/o/token/" and request.method == "POST":
            self.token_requests += 1
            return self._token(request)
        return httpx.Response(404, json={"error": "not_found"})

    def _client_ok(self, request: httpx.Request, form: dict[str, str]) -> bool:
        if self.auth_method == "private_key_jwt":
            if form.get("client_assertion_type") != (
                "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            ):
                return False
            try:
                claims = jwt.decode(
                    form.get("client_assertion", ""),
                    self.client_public_key,
                    algorithms=["RS256", "ES256"],
                    audience=f"{BASE}/token/",
                    issuer=CLIENT_ID,
                )
            except jwt.PyJWTError:
                return False
            return bool(claims.get("sub") == CLIENT_ID and claims.get("jti"))
        header = request.headers.get("authorization", "")
        if not header.startswith("Basic "):
            return False
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        return unquote(user) == CLIENT_ID and unquote(password) == CLIENT_SECRET

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if not self._client_ok(request, form):
            return httpx.Response(401, json={"error": "invalid_client"})
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            grant = self.codes.pop(form.get("code", ""), None)
            if grant is None or form.get("redirect_uri") != grant.redirect_uri:
                return httpx.Response(400, json={"error": "invalid_grant"})
            verifier = form.get("code_verifier", "")
            if not verifier or _b64(hashlib.sha256(verifier.encode()).digest()) != grant.challenge:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return self._tokens(grant.claims, nonce=grant.nonce)
        if grant_type == "refresh_token":
            refresh = self.refresh_tokens.pop(form.get("refresh_token", ""), None)
            if refresh is None or refresh.revoked:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return self._tokens(refresh.claims, nonce=None)
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _tokens(self, claims: dict[str, Any], *, nonce: str | None) -> httpx.Response:
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        self.refresh_tokens[refresh_token] = RefreshGrant(claims=dict(claims))
        return httpx.Response(
            200,
            json={
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": 300,
                "id_token": self.issue_id_token(claims, nonce=nonce, access_token=access_token),
                "refresh_token": refresh_token,
            },
        )


@dataclass
class IdpMock:
    """A :class:`FakeIdP` wired into ``respx``."""

    idp: FakeIdP
    router: respx.MockRouter


@contextmanager
def mocked_idp(idp: FakeIdP | None = None) -> Iterator[IdpMock]:
    """The fake IdP answering :data:`FakeIdP.expected_ip` through ``respx``."""
    fake = idp or FakeIdP()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.route(host=fake.expected_ip).mock(side_effect=fake.handle)
        yield IdpMock(fake, router)


# ---------------------------------------------------------------------------
# Driving the flow from a TestClient
# ---------------------------------------------------------------------------

START = f"{API_PREFIX}/auth/oidc/start"
CALLBACK = f"{API_PREFIX}/auth/oidc/callback"
LINK = f"{API_PREFIX}/auth/oidc/link"
OIDC_LOGOUT = f"{API_PREFIX}/auth/oidc/logout"
BACKCHANNEL = f"{API_PREFIX}/auth/oidc/backchannel-logout"


# TestClient responses are typed by Starlette's own httpx build; ``Any`` here.
def start(client: TestClient, return_to: str | None = None, *, reauth: bool = False) -> Any:
    params: dict[str, str] = {}
    if return_to is not None:
        params["return_to"] = return_to
    if reauth:
        params["reauth"] = "true"
    return client.get(START, params=params or None, follow_redirects=False)


def login_via_idp(
    client: TestClient,
    idp: FakeIdP,
    claims: dict[str, Any],
    *,
    return_to: str | None = None,
    reauth: bool = False,
) -> Any:
    """Start, "log in" at the fake IdP and call back; return the callback response.

    ``reauth`` starts with ``reauth=true`` (the forced IdP sign-in that is
    the only OIDC login able to earn a fresh step-up).
    """
    started = start(client, return_to, reauth=reauth)
    assert started.status_code == 303, started.text
    code, state = idp.authorize(started.headers["location"], claims)
    return client.get(CALLBACK, params={"code": code, "state": state}, follow_redirects=False)


def oidc_error(response: Any) -> str | None:
    """The ``oidc_error`` of a redirect to ``/login``, else ``None``."""
    location = str(response.headers.get("location", ""))
    parts = urlsplit(location)
    if parts.path != "/login":
        return None
    values = parse_qs(parts.query).get("oidc_error", [])
    return values[0] if values else None

# SPDX-License-Identifier: AGPL-3.0-or-later
"""OIDC relying party: authorization request, code exchange, refresh (AUTH.md "OIDC").

:class:`OidcClient` (one per app, :func:`get_oidc_client`) bundles the
SSRF-safe HTTP client (``app.core.http``), the discovery/JWKS cache
(``app.auth.oidc.discovery``) and the sealed pre-auth cookies.

* **Authorization request** (:meth:`OidcClient.begin`): ``response_type=code``
  with a random ``state`` (256 bits), ``nonce`` (256 bits) and PKCE S256
  (verifier of 64 random bytes). ``redirect_uri`` is always
  ``Settings.oidc_redirect_uri`` (derived from ``PERFIN_PUBLIC_URL``, never
  from the request). The three secrets and the validated ``return_to`` travel
  in the **pre-auth cookie** ``__Host-perfin-oidc`` (``HttpOnly; Secure;
  SameSite=Lax; Path=/``, 5 minutes), sealed with XChaCha20-Poly1305 under a
  key derived from ``PERFIN_SECRET_KEY`` (:class:`CookieSealer`): the browser
  can neither read nor forge it, and a callback is only accepted from the
  browser that started the flow (B1). The cookie also records when the
  flow started and whether it was a ``reauth`` request.
  With ``reauth`` it adds ``prompt=login&max_age=0`` (step-up through the
  IdP, A6): only such a login, with an ``auth_time`` not older than its
  start, earns a fresh step-up (review P1-SR3-01).
* **Code exchange** (:meth:`OidcClient.exchange_code`): back-channel ``POST``
  to the token endpoint with the PKCE verifier and the fixed redirect URI;
  client authentication ``client_secret_basic`` (RFC 6749 2.3.1 encoding) or
  ``private_key_jwt`` (RFC 7523 assertion signed with
  ``PERFIN_OIDC_PRIVATE_KEY_FILE``, 60 s lifetime, random ``jti``).
* **Refresh** (:meth:`OidcClient.refresh`): ``grant_type=refresh_token`` for
  the optional claim refresh (B8).

Dev mode with ``PERFIN_SECURE_COOKIES=false`` uses ``perfin-oidc-dev`` /
``perfin-oidc-link-dev`` without ``Secure`` (the ``__Host-`` prefix requires
it), like the session cookie.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from nacl import bindings
from nacl.exceptions import CryptoError
from starlette.requests import HTTPConnection
from starlette.responses import Response

from app.auth.oidc.discovery import ProviderCache, ProviderMetadata
from app.auth.oidc.tokens import KeyLookup
from app.core import clock
from app.core.http import EgressError, EgressPolicy, Resolver, SafeHttpClient, url_host_key
from app.core.settings import Settings

STATE_COOKIE: Final = "__Host-perfin-oidc"
DEV_STATE_COOKIE: Final = "perfin-oidc-dev"
LINK_COOKIE: Final = "__Host-perfin-oidc-link"
DEV_LINK_COOKIE: Final = "perfin-oidc-link-dev"
#: Lifetime of the pre-auth (state) and pending-link cookies.
PRE_AUTH_TTL: Final = timedelta(minutes=5)
VERIFIER_BYTES: Final = 64
RANDOM_BYTES: Final = 32
MAX_COOKIE_CHARS: Final = 3800
CLIENT_ASSERTION_TYPE: Final = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

_SEAL_CONTEXT: Final = b"perfin/oidc-cookie/v1"
_NONCE_BYTES: Final = bindings.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES


class OidcDisabledError(Exception):
    """OIDC is not configured on this instance."""


class TokenEndpointError(Exception):
    """The token endpoint refused the request or answered nonsense.

    ``invalid_grant`` is set when the IdP said the grant (code or refresh
    token) is no longer valid.
    """

    def __init__(self, message: str, *, invalid_grant: bool = False) -> None:
        super().__init__(message)
        self.invalid_grant = invalid_grant


# ---------------------------------------------------------------------------
# PKCE and random values
# ---------------------------------------------------------------------------


def random_token(nbytes: int = RANDOM_BYTES) -> str:
    return secrets.token_urlsafe(nbytes)


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 S256: base64url(SHA-256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# return_to (B4)
# ---------------------------------------------------------------------------

MAX_RETURN_TO: Final = 512
#: A ``.`` or ``..`` path segment, plain or percent-encoded (``%2e``).
_DOT_SEGMENT: Final = re.compile(r"(?:^|/)(?:\.|%2e){1,2}(?:/|$)", re.IGNORECASE)


def safe_return_to(value: str | None) -> str | None:
    """``value`` if it is a same-origin relative path, else ``None``.

    Accepted: starts with a single ``/``, no scheme or authority (so no
    ``//host``, ``/\\host`` or ``https:``), no backslashes, no control or
    whitespace characters, no ``.`` or ``..`` path segment (also not
    percent-encoded: ``/a/..//host`` normalises to ``//host``; the SPA
    applies the same rule, review P1-SR2-02), at most 512 characters.
    Anything else would let the post-login redirect leave the site (open
    redirect).
    """
    if not value or len(value) > MAX_RETURN_TO:
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    if "\\" in value or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme or parts.netloc:
        return None
    if _DOT_SEGMENT.search(parts.path):
        return None
    return value


# ---------------------------------------------------------------------------
# Sealed cookies
# ---------------------------------------------------------------------------


class CookieSealer:
    """Authenticated encryption of small JSON payloads for cookies.

    XChaCha20-Poly1305 with a key derived from ``PERFIN_SECRET_KEY`` (HMAC
    with a fixed context), a random nonce per cookie and the cookie's purpose
    as associated data, so a state cookie cannot be replayed as a link cookie.
    The payload carries its own expiry.
    """

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 16:
            raise ValueError("cookie secret is too short")
        self._key = hmac.new(secret, _SEAL_CONTEXT, hashlib.sha256).digest()

    def seal(self, purpose: str, payload: Mapping[str, Any], *, ttl: timedelta) -> str:
        body = dict(payload)
        body["exp"] = int(clock.utcnow().timestamp() + ttl.total_seconds())
        plaintext = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        sealed = bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
            plaintext, purpose.encode("ascii"), nonce, self._key
        )
        value = base64.urlsafe_b64encode(nonce + sealed).rstrip(b"=").decode("ascii")
        if len(value) > MAX_COOKIE_CHARS:
            raise ValueError("sealed cookie is too large")
        return value

    def unseal(self, purpose: str, value: str | None) -> dict[str, Any] | None:
        """The payload, or ``None`` when missing, forged, for another purpose or expired."""
        if not value or len(value) > MAX_COOKIE_CHARS:
            return None
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (ValueError, TypeError):
            return None
        if len(raw) <= _NONCE_BYTES:
            return None
        try:
            plaintext = bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
                raw[_NONCE_BYTES:], purpose.encode("ascii"), raw[:_NONCE_BYTES], self._key
            )
            payload = json.loads(plaintext)
        except (CryptoError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp")
        if not isinstance(exp, int) or exp <= clock.utcnow().timestamp():
            return None
        return payload


def state_cookie_name(settings: Settings) -> str:
    return STATE_COOKIE if settings.secure_cookies else DEV_STATE_COOKIE


def link_cookie_name(settings: Settings) -> str:
    return LINK_COOKIE if settings.secure_cookies else DEV_LINK_COOKIE


def set_sealed_cookie(
    response: Response, settings: Settings, name: str, value: str, *, samesite: str
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=int(PRE_AUTH_TTL.total_seconds()),
        path="/",
        secure=settings.secure_cookies,
        httponly=True,
        samesite=samesite,  # type: ignore[arg-type]
    )


def clear_cookie(response: Response, settings: Settings, name: str, *, samesite: str) -> None:
    response.delete_cookie(
        name,
        path="/",
        secure=settings.secure_cookies,
        httponly=True,
        samesite=samesite,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Flow state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthRequest:
    """What :meth:`OidcClient.begin` produced: the IdP URL and the cookie value."""

    url: str
    cookie_value: str = field(repr=False)


#: Largest plausible start time in the pre-auth cookie (ms; year 9999).
_MAX_STARTED_MS: Final = 253_402_300_799_000


@dataclass(frozen=True)
class PreAuthState:
    """The unsealed pre-auth cookie.

    ``reauth`` is true only for a flow started with ``reauth=true`` whose
    start time (``started_at``, this server's clock) is known; anything else
    in the cookie reads as a plain login, which never earns a step-up.
    """

    state: str = field(repr=False)
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)
    return_to: str
    reauth: bool = False
    started_at: datetime | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> PreAuthState | None:
        if payload is None:
            return None
        values = [payload.get(k) for k in ("s", "n", "v", "r")]
        if not all(isinstance(v, str) and v for v in values):
            return None
        state, nonce, verifier, return_to = (str(v) for v in values)
        if not 43 <= len(verifier) <= 128:
            return None
        started_ms = payload.get("t")
        started_at = (
            datetime.fromtimestamp(started_ms / 1000, tz=UTC)
            if isinstance(started_ms, int)
            and not isinstance(started_ms, bool)
            and 0 < started_ms <= _MAX_STARTED_MS
            else None
        )
        reauth = payload.get("a") is True and started_at is not None
        return cls(
            state,
            nonce,
            verifier,
            safe_return_to(return_to) or "/",
            reauth=reauth,
            started_at=started_at,
        )


@dataclass(frozen=True)
class TokenResponse:
    """A token endpoint answer; the tokens are dropped after validation."""

    id_token: str | None = field(repr=False)
    access_token: str | None = field(repr=False)
    refresh_token: str | None = field(repr=False)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _issuer_policy(settings: Settings) -> EgressPolicy:
    issuer_key = url_host_key(settings.oidc_issuer)
    return EgressPolicy(
        allowed_hosts=frozenset({issuer_key}),
        private_hosts=frozenset({issuer_key})
        if settings.oidc_allow_private_issuer
        else frozenset(),
        allow_http=settings.dev_mode,
        allow_loopback=settings.dev_mode and settings.oidc_allow_private_issuer,
        timeout_seconds=float(settings.oidc_http_timeout_seconds),
    )


def _cookie_secret(settings: Settings) -> bytes:
    if settings.secret_key is not None:
        return settings.secret_key.get_secret_value().encode("utf-8")
    # Refused outside dev mode by `perfin config check`; per-process key.
    return secrets.token_bytes(32)


def load_private_key(path: Path) -> rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey:
    """The ``private_key_jwt`` signing key (PEM, RSA >= 2048 or EC P-256)."""
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if isinstance(key, rsa.RSAPrivateKey) and key.key_size >= 2048:
        return key
    if isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1):
        return key
    raise ValueError("PERFIN_OIDC_PRIVATE_KEY_FILE must hold an RSA (>= 2048) or EC P-256 key")


class OidcClient:
    """Relying-party operations for one configured issuer (module doc)."""

    def __init__(
        self,
        settings: Settings,
        *,
        resolver: Resolver | None = None,
        http: SafeHttpClient | None = None,
    ) -> None:
        if not settings.oidc_enabled:
            raise OidcDisabledError
        self.settings = settings
        self.issuer = settings.oidc_issuer.strip()
        self.client_id = settings.oidc_client_id.strip()
        policy = _issuer_policy(settings)
        if http is None:
            http = (
                SafeHttpClient(policy, resolver=resolver)
                if resolver is not None
                else SafeHttpClient(policy)
            )
        self.http = http
        self.cache = ProviderCache(
            http,
            issuer=self.issuer,
            auth_method=settings.oidc_token_auth_method,
            max_age=timedelta(seconds=settings.oidc_metadata_max_age_seconds),
            jwks_min_refresh=timedelta(seconds=settings.oidc_jwks_min_refresh_seconds),
        )
        self.sealer = CookieSealer(_cookie_secret(settings))
        self._private_key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey | None = None

    def close(self) -> None:
        self.http.close()

    @property
    def keys(self) -> KeyLookup:
        return self.cache.signing_keys

    def metadata(self) -> ProviderMetadata:
        return self.cache.metadata()

    # ---- authorization request ---------------------------------------------
    def begin(self, return_to: str | None, *, reauth: bool = False) -> AuthRequest:
        """The authorization request and its sealed pre-auth cookie.

        ``reauth`` asks the IdP to authenticate the user again even when it
        has an SSO session (``prompt=login``, ``max_age=0``; OIDC Core 3.1.2.1).
        That is how a user who relies on trusted IdP MFA gets a fresh step-up:
        the sealed cookie records ``reauth`` and the start time, and only
        such a login whose ID token ``auth_time`` is not older than the start
        (minus the clock skew) counts as a step-up
        (``app.auth.mfa.policy.mark_mfa_from_idp``). A plain login never
        does, whatever its ``auth_time`` (review P1-SR3-01), and an IdP that
        ignores the reauth request (old ``auth_time``) yields no step-up
        rather than a false one.
        """
        metadata = self.metadata()
        state, nonce, verifier = random_token(), random_token(), random_token(VERIFIER_BYTES)
        target = safe_return_to(return_to) or "/"
        started_ms = int(clock.utcnow().timestamp() * 1000)
        cookie = self.sealer.seal(
            "state",
            {
                "s": state,
                "n": nonce,
                "v": verifier,
                "r": target,
                "a": reauth,
                "t": started_ms,
            },
            ttl=PRE_AUTH_TTL,
        )
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.settings.oidc_redirect_uri,
            "scope": " ".join(self._scopes()),
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        if reauth:
            params["prompt"] = "login"
            params["max_age"] = "0"
        parts = urlsplit(metadata.authorization_endpoint)
        query = f"{parts.query}&{urlencode(params)}" if parts.query else urlencode(params)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
        return AuthRequest(url=url, cookie_value=cookie)

    def _scopes(self) -> list[str]:
        scopes = [s for s in self.settings.oidc_scopes if s]
        if "openid" not in scopes:
            scopes.insert(0, "openid")
        return scopes

    def read_state(self, conn: HTTPConnection) -> PreAuthState | None:
        value = conn.cookies.get(state_cookie_name(self.settings))
        return PreAuthState.from_payload(self.sealer.unseal("state", value))

    # ---- token endpoint ------------------------------------------------------
    def _client_auth(self, token_endpoint: str) -> tuple[dict[str, str], dict[str, str]]:
        """``(form fields, headers)`` authenticating this client."""
        client_auth = self.settings.oidc_token_auth_method
        if client_auth == "private_key_jwt":
            return {
                "client_id": self.client_id,
                "client_assertion_type": CLIENT_ASSERTION_TYPE,
                "client_assertion": self._client_assertion(token_endpoint),
            }, {}
        secret = self.settings.oidc_client_secret
        if secret is None:
            raise TokenEndpointError("no client secret configured")
        user = quote(self.client_id, safe="")
        password = quote(secret.get_secret_value(), safe="")
        basic = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        return {}, {"Authorization": f"Basic {basic}"}

    def _client_assertion(self, audience: str) -> str:
        if self._private_key is None:
            path = self.settings.oidc_private_key_file
            if path is None:
                raise TokenEndpointError("no private key configured for private_key_jwt")
            try:
                self._private_key = load_private_key(path)
            except (OSError, ValueError) as exc:
                raise TokenEndpointError("the private_key_jwt key cannot be loaded") from exc
        key = self._private_key
        algorithm = "RS256" if isinstance(key, rsa.RSAPrivateKey) else "ES256"
        now = int(clock.utcnow().timestamp())
        headers = (
            {"kid": self.settings.oidc_private_key_id}
            if self.settings.oidc_private_key_id
            else None
        )
        return jwt.encode(
            {
                "iss": self.client_id,
                "sub": self.client_id,
                "aud": audience,
                "jti": str(uuid.uuid4()),
                "iat": now,
                "exp": now + 60,
            },
            key,
            algorithm=algorithm,
            headers=headers,
        )

    def _token_request(self, fields: dict[str, str]) -> TokenResponse:
        metadata = self.metadata()
        auth_fields, headers = self._client_auth(metadata.token_endpoint)
        try:
            result = self.http.post_form(
                metadata.token_endpoint,
                {**fields, **auth_fields},
                headers={**headers, "Accept": "application/json"},
            )
        except EgressError as exc:
            raise TokenEndpointError(f"token endpoint unreachable: {exc}") from exc
        try:
            body = result.json()
        except EgressError:
            body = None
        if result.status_code != 200 or not isinstance(body, dict):
            error = body.get("error") if isinstance(body, dict) else None
            raise TokenEndpointError(
                f"token endpoint returned HTTP {result.status_code}",
                invalid_grant=error == "invalid_grant",
            )
        token_type = body.get("token_type")
        if token_type is not None and (
            not isinstance(token_type, str) or token_type.lower() != "bearer"
        ):
            raise TokenEndpointError("unexpected token_type")

        def text(name: str) -> str | None:
            value = body.get(name)
            return value if isinstance(value, str) and value else None

        return TokenResponse(
            id_token=text("id_token"),
            access_token=text("access_token"),
            refresh_token=text("refresh_token"),
        )

    def exchange_code(self, code: str, verifier: str) -> TokenResponse:
        """Authorization code + PKCE verifier -> tokens (an ID token is required)."""
        response = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.oidc_redirect_uri,
                "code_verifier": verifier,
            }
        )
        if response.id_token is None:
            raise TokenEndpointError("token response has no id_token")
        return response

    def refresh(self, refresh_token: str) -> TokenResponse:
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    # ---- logout ----------------------------------------------------------------
    def end_session_url(
        self, *, id_token_hint: str | None, post_logout_redirect_uri: str | None
    ) -> str | None:
        """RP-initiated logout URL, or ``None`` when the IdP has no end-session endpoint."""
        endpoint = self.metadata().end_session_endpoint
        if endpoint is None:
            return None
        params: dict[str, str] = {"client_id": self.client_id}
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        if post_logout_redirect_uri:
            params["post_logout_redirect_uri"] = post_logout_redirect_uri
        parts = urlsplit(endpoint)
        query = f"{parts.query}&{urlencode(params)}" if parts.query else urlencode(params)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def get_oidc_client(conn: HTTPConnection) -> OidcClient:
    """The app's :class:`OidcClient` (``app.state.oidc_client``), built on first use.

    Raises :class:`OidcDisabledError` when OIDC is not configured. Tests may
    set ``app.state.oidc_client`` (or ``app.state.oidc_resolver``) first.
    """
    client = getattr(conn.app.state, "oidc_client", None)
    if isinstance(client, OidcClient):
        return client
    settings = conn.app.state.settings
    resolver = getattr(conn.app.state, "oidc_resolver", None)
    client = OidcClient(settings, resolver=resolver)
    conn.app.state.oidc_client = client
    return client

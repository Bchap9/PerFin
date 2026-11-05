# SPDX-License-Identifier: AGPL-3.0-or-later
"""OIDC discovery and JWKS, fetched through the SSRF-safe client (B5, B9).

* The discovery document is read from
  ``<issuer without trailing slash>/.well-known/openid-configuration`` and
  its ``issuer`` must equal ``PERFIN_OIDC_ISSUER`` exactly (byte for byte,
  trailing slash included).
* **Egress allowlist = the issuer's host.** Every endpoint the API itself
  calls or sends the browser to (``authorization_endpoint``,
  ``token_endpoint``, ``jwks_uri``, ``end_session_endpoint``,
  ``userinfo_endpoint``) must use the issuer's scheme, host and port, or the
  document is rejected. Authentik serves all of them from one host; an IdP
  that spreads them over several hosts is not supported.
* The document must allow the authorization code flow, S256 PKCE and a
  pinned signing algorithm (``RS256``/``ES256``) when it lists those
  capabilities at all.
* Both are cached in memory for at most ``PERFIN_OIDC_METADATA_MAX_AGE_SECONDS``.
  A token signed with an unknown ``kid`` triggers a JWKS refetch at most once
  per ``PERFIN_OIDC_JWKS_MIN_REFRESH_SECONDS``; otherwise it is rejected
  (:class:`app.auth.oidc.tokens` never accepts unsigned tokens). JWKS keys
  are filtered to signing keys of a pinned type (RSA >= 2048 bits, EC P-256).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from httpx import URL, InvalidURL

from app.core import clock
from app.core.http import EgressError, SafeHttpClient, url_host_key

#: Signing algorithms accepted for ID and logout tokens (SECURITY.md A9).
ALLOWED_ALGORITHMS: Final = ("RS256", "ES256")
MIN_RSA_BITS: Final = 2048
MAX_JWKS_KEYS: Final = 50
WELL_KNOWN: Final = "/.well-known/openid-configuration"

_ENDPOINT_FIELDS: Final = (
    "authorization_endpoint",
    "token_endpoint",
    "jwks_uri",
    "end_session_endpoint",
    "userinfo_endpoint",
)


class DiscoveryError(Exception):
    """The provider metadata or keys could not be fetched or are not acceptable."""


def discovery_url(issuer: str) -> str:
    return issuer.rstrip("/") + WELL_KNOWN


@dataclass(frozen=True)
class ProviderMetadata:
    """The parts of the discovery document PerFin uses."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None = None
    userinfo_endpoint: str | None = None
    backchannel_logout_supported: bool = False
    token_endpoint_auth_methods: tuple[str, ...] = ("client_secret_basic",)


def _origin(url: str) -> tuple[str, str]:
    parsed = URL(url)
    return parsed.scheme, url_host_key(parsed)


def _str_list(doc: dict[str, Any], key: str) -> list[str] | None:
    value = doc.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise DiscoveryError(f"discovery field {key} is not a list of strings")
    return value


def parse_metadata(doc: object, *, issuer: str, auth_method: str) -> ProviderMetadata:
    """Validate a discovery document against the configured issuer (module doc)."""
    if not isinstance(doc, dict):
        raise DiscoveryError("discovery document is not a JSON object")
    if doc.get("issuer") != issuer:
        raise DiscoveryError("discovery issuer does not match PERFIN_OIDC_ISSUER exactly")
    issuer_origin = _origin(issuer)
    endpoints: dict[str, str | None] = {}
    for name in _ENDPOINT_FIELDS:
        value = doc.get(name)
        if value is None:
            endpoints[name] = None
            continue
        if not isinstance(value, str) or not value:
            raise DiscoveryError(f"discovery field {name} is not a URL")
        try:
            origin = _origin(value)
        except (InvalidURL, ValueError, TypeError) as exc:
            raise DiscoveryError(f"discovery field {name} is not a URL") from exc
        if origin != issuer_origin:
            raise DiscoveryError(f"{name} is not on the issuer's host (egress allowlist)")
        endpoints[name] = value
    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if endpoints[required] is None:
            raise DiscoveryError(f"discovery document has no {required}")
    response_types = _str_list(doc, "response_types_supported")
    if response_types is not None and "code" not in response_types:
        raise DiscoveryError("provider does not support the authorization code flow")
    pkce = _str_list(doc, "code_challenge_methods_supported")
    if pkce is not None and "S256" not in pkce:
        raise DiscoveryError("provider does not support PKCE S256")
    algs = _str_list(doc, "id_token_signing_alg_values_supported")
    if algs is not None and not set(algs) & set(ALLOWED_ALGORITHMS):
        raise DiscoveryError("provider signs ID tokens with no allowed algorithm")
    auth_methods = _str_list(doc, "token_endpoint_auth_methods_supported")
    methods = tuple(auth_methods) if auth_methods is not None else ("client_secret_basic",)
    if auth_method not in methods:
        raise DiscoveryError(f"provider does not support {auth_method}")
    return ProviderMetadata(
        issuer=issuer,
        authorization_endpoint=str(endpoints["authorization_endpoint"]),
        token_endpoint=str(endpoints["token_endpoint"]),
        jwks_uri=str(endpoints["jwks_uri"]),
        end_session_endpoint=endpoints["end_session_endpoint"],
        userinfo_endpoint=endpoints["userinfo_endpoint"],
        backchannel_logout_supported=doc.get("backchannel_logout_supported") is True,
        token_endpoint_auth_methods=methods,
    )


# ---------------------------------------------------------------------------
# JWKS
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SigningKey:
    """One usable verification key from the JWKS."""

    kid: str | None
    algorithm: str
    key: Any = field(repr=False)


def _usable_key(raw: object) -> SigningKey | None:
    """A pinned-type signing key, or ``None`` for anything else (skipped)."""
    if not isinstance(raw, dict):
        return None
    kty = raw.get("kty")
    if raw.get("use") not in (None, "sig"):
        return None
    ops = raw.get("key_ops")
    if ops is not None and (not isinstance(ops, list) or "verify" not in ops):
        return None
    if kty == "RSA":
        algorithm = "RS256"
    elif kty == "EC" and raw.get("crv") == "P-256":
        algorithm = "ES256"
    else:
        return None
    declared = raw.get("alg")
    if declared is not None and declared != algorithm:
        return None
    kid = raw.get("kid")
    if kid is not None and not isinstance(kid, str):
        return None
    if "d" in raw:
        # A private key published by mistake is still only used to verify,
        # but its presence means the JWKS is broken: skip it.
        return None
    try:
        parsed = jwt.PyJWK(raw, algorithm=algorithm)
    except (jwt.PyJWKError, jwt.InvalidKeyError, ValueError, TypeError):
        return None
    key = parsed.key
    if algorithm == "RS256":
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < MIN_RSA_BITS:
            return None
    elif not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        return None
    return SigningKey(kid=kid, algorithm=algorithm, key=key)


def parse_jwks(doc: object) -> list[SigningKey]:
    if not isinstance(doc, dict) or not isinstance(doc.get("keys"), list):
        raise DiscoveryError("JWKS is not a JSON object with a keys list")
    keys = [k for k in (_usable_key(raw) for raw in doc["keys"][:MAX_JWKS_KEYS]) if k]
    if not keys:
        raise DiscoveryError("JWKS has no usable RS256/ES256 signing key")
    return keys


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class ProviderCache:
    """Discovery document and JWKS with bounded lifetimes (thread-safe)."""

    def __init__(
        self,
        http: SafeHttpClient,
        *,
        issuer: str,
        auth_method: str,
        max_age: timedelta,
        jwks_min_refresh: timedelta,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._http = http
        self._issuer = issuer
        self._auth_method = auth_method
        self._max_age = max_age
        self._jwks_min_refresh = jwks_min_refresh
        # Late-bound so tests can monkeypatch ``app.core.clock.utcnow``.
        self._now = now or (lambda: clock.utcnow())
        self._lock = threading.Lock()
        self._metadata: ProviderMetadata | None = None
        self._metadata_at: datetime | None = None
        self._keys: list[SigningKey] = []
        self._keys_at: datetime | None = None

    def _fetch_json(self, url: str, what: str) -> object:
        try:
            result = self._http.get(url, headers={"Accept": "application/json"})
        except EgressError as exc:
            raise DiscoveryError(f"{what} fetch failed: {exc}") from exc
        if result.status_code != 200:
            raise DiscoveryError(f"{what} fetch returned HTTP {result.status_code}")
        try:
            return result.json()
        except EgressError as exc:
            raise DiscoveryError(f"{what} is not valid JSON") from exc

    def metadata(self) -> ProviderMetadata:
        with self._lock:
            now = self._now()
            if (
                self._metadata is not None
                and self._metadata_at is not None
                and now - self._metadata_at < self._max_age
            ):
                return self._metadata
            doc = self._fetch_json(discovery_url(self._issuer), "discovery")
            metadata = parse_metadata(doc, issuer=self._issuer, auth_method=self._auth_method)
            if self._metadata is not None and metadata.jwks_uri != self._metadata.jwks_uri:
                self._keys, self._keys_at = [], None
            self._metadata, self._metadata_at = metadata, now
            return metadata

    def signing_keys(self, kid: str | None, algorithm: str) -> list[SigningKey]:
        """Keys that may verify a token with this ``kid`` and ``alg``.

        An unknown ``kid`` refetches the JWKS at most once per minimum refresh
        interval; an expired cache (max age) is always refetched.
        """
        with self._lock:
            now = self._now()
            if self._keys_at is None or now - self._keys_at >= self._max_age:
                self._unlocked_refresh(now)
            found = _matching(self._keys, kid, algorithm)
            fetched = self._keys_at
            stale = fetched is None or now - fetched >= self._jwks_min_refresh
            if not found and kid is not None and stale:
                self._unlocked_refresh(now)
                found = _matching(self._keys, kid, algorithm)
            return found

    def _unlocked_refresh(self, now: datetime) -> None:
        """Refetch the JWKS (and stale metadata); the caller holds the lock."""
        metadata = self._metadata
        if (
            metadata is None
            or self._metadata_at is None
            or now - self._metadata_at >= self._max_age
        ):
            doc = self._fetch_json(discovery_url(self._issuer), "discovery")
            metadata = parse_metadata(doc, issuer=self._issuer, auth_method=self._auth_method)
            self._metadata, self._metadata_at = metadata, now
        self._keys_at = now  # counts even when the fetch fails (rate limit, B9)
        self._keys = parse_jwks(self._fetch_json(metadata.jwks_uri, "JWKS"))

    def clear(self) -> None:
        with self._lock:
            self._metadata = self._metadata_at = None
            self._keys, self._keys_at = [], None


def _matching(keys: list[SigningKey], kid: str | None, algorithm: str) -> list[SigningKey]:
    typed = [k for k in keys if k.algorithm == algorithm]
    if kid is None:
        # Without a kid only an unambiguous key of the right type is used.
        return typed if len(typed) == 1 else []
    return [k for k in typed if k.kid == kid]

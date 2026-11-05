# SPDX-License-Identifier: AGPL-3.0-or-later
"""ID token and logout token validation (SECURITY.md A9, B2, B8, B9).

Both are JWTs signed by the IdP. Validation order (any failure raises
:class:`TokenValidationError` with a stable ``reason`` for the audit trail):

1. Size cap and the JOSE header: ``alg`` must be ``RS256`` or ``ES256``
   (``none``, ``HS*`` and everything else are refused before any key is
   touched, so an HMAC "signed" with the public key, the classic key
   confusion, never verifies); no ``crit``; embedded keys (``jwk``, ``jku``,
   ``x5u``, ``x5c``) are never used.
2. The verification key comes only from the issuer's JWKS
   (:class:`~app.auth.oidc.discovery.ProviderCache`), matched by ``kid`` and
   key type; an unknown ``kid`` refreshes the JWKS at most once per
   interval, then fails (B9).
3. Signature (PyJWT with exactly that one algorithm), then the claims, with
   :func:`app.core.clock.utcnow` and :data:`CLOCK_SKEW` (60 s): ``iss`` equal
   to the configured issuer, ``aud`` containing the client id (and, when there
   are several audiences or an ``azp``, ``azp`` equal to it), ``exp`` in the
   future, ``iat`` not in the future and not older than :data:`MAX_TOKEN_AGE`,
   ``nbf`` reached, non-empty ``sub``.
4. ID tokens: ``nonce`` equal to the one stored in the pre-auth cookie;
   ``at_hash`` (if present) equal to the left half of SHA-256 of the access
   token.
5. Logout tokens (OpenID Back-Channel Logout 1.0): the
   ``http://schemas.openid.net/event/backchannel-logout`` event, ``sid`` or
   ``sub``, a non-empty ``jti`` of at most 255 characters (REQUIRED by the
   spec; the caller records it so a token works once,
   ``app.auth.oidc.logout.consume_logout_jti``), **no** ``nonce``, ``typ``
   (if any) ``logout+jwt`` or ``JWT``.

The tokens are not kept: callers take the claims they need and drop the
JWT (the ID token is only stored encrypted as ``id_token_hint``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import jwt

from app.auth.oidc.discovery import ALLOWED_ALGORITHMS, DiscoveryError, SigningKey
from app.core import clock

CLOCK_SKEW: Final = timedelta(seconds=60)
#: An ID token (or logout token) issued longer ago than this is refused.
MAX_TOKEN_AGE: Final = timedelta(minutes=10)
MAX_TOKEN_BYTES: Final = 16 * 1024
#: Longest ``jti`` accepted in a logout token.
MAX_JTI_LENGTH: Final = 255
LOGOUT_EVENT: Final = "http://schemas.openid.net/event/backchannel-logout"
_FORBIDDEN_HEADERS: Final = frozenset({"crit"})
_LOGOUT_TYPES: Final = frozenset({"logout+jwt", "jwt"})

KeyLookup = Callable[[str | None, str], list[SigningKey]]


class TokenValidationError(Exception):
    """The token was refused. ``reason`` is a stable code (never token content)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ValidatedToken:
    """Claims of a token that passed every check."""

    claims: Mapping[str, Any] = field(repr=False)
    issuer: str
    subject: str | None
    algorithm: str
    kid: str | None

    @property
    def sid(self) -> str | None:
        value = self.claims.get("sid")
        return value if isinstance(value, str) and 0 < len(value) <= 255 else None

    @property
    def jti(self) -> str | None:
        value = self.claims.get("jti")
        return value if isinstance(value, str) and 0 < len(value) <= MAX_JTI_LENGTH else None

    @property
    def issued_at(self) -> datetime | None:
        try:
            return _timestamp(self.claims, "iat", required=False)
        except TokenValidationError:
            return None

    @property
    def auth_time(self) -> datetime | None:
        """When the IdP authenticated the user (OIDC Core ``auth_time``), if valid.

        Not checked during validation: a missing or malformed value only
        means PerFin cannot tell how fresh the login is
        (``app.auth.mfa.policy.mark_mfa_from_idp`` then never counts it as
        step-up).
        """
        try:
            return _timestamp(self.claims, "auth_time", required=False)
        except TokenValidationError:
            return None

    @property
    def amr(self) -> list[str]:
        value = self.claims.get("amr")
        if not isinstance(value, list):
            return []
        return [v for v in value if isinstance(v, str)]

    @property
    def groups(self) -> list[str]:
        value = self.claims.get("groups")
        if not isinstance(value, list):
            return []
        return [v for v in value if isinstance(v, str)][:200]

    def string(self, name: str, max_length: int = 320) -> str | None:
        value = self.claims.get(name)
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value[:max_length] if value else None

    @property
    def email_verified(self) -> bool:
        return self.claims.get("email_verified") is True


def at_hash(access_token: str) -> str:
    """OIDC Core 3.1.3.6 ``at_hash`` for RS256/ES256 (SHA-256, left half)."""
    digest = hashlib.sha256(access_token.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest[: len(digest) // 2]).rstrip(b"=").decode("ascii")


def _header(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_BYTES:
        raise TokenValidationError("malformed")
    if token.count(".") != 2:
        raise TokenValidationError("malformed")
    # Parsed here (not by PyJWT) so the policy checks below see the raw header.
    segment = token.split(".", 1)[0]
    try:
        header = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, TypeError):
        raise TokenValidationError("malformed") from None
    if not isinstance(header, dict):
        raise TokenValidationError("malformed")
    return header


def _timestamp(claims: Mapping[str, Any], name: str, *, required: bool) -> datetime | None:
    value = claims.get(name)
    if value is None:
        if required:
            raise TokenValidationError(f"missing-{name}")
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TokenValidationError(f"invalid-{name}")
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise TokenValidationError(f"invalid-{name}") from None


def _check_audience(claims: Mapping[str, Any], client_id: str) -> None:
    aud = claims.get("aud")
    audiences: list[str]
    if isinstance(aud, str):
        audiences = [aud]
    elif isinstance(aud, list) and aud and all(isinstance(a, str) for a in aud):
        audiences = list(aud)
    else:
        raise TokenValidationError("invalid-aud")
    if client_id not in audiences:
        raise TokenValidationError("invalid-aud")
    azp = claims.get("azp")
    if (len(audiences) > 1 or azp is not None) and azp != client_id:
        raise TokenValidationError("invalid-azp")


def _verify(
    token: str,
    *,
    issuer: str,
    client_id: str,
    keys: KeyLookup,
    now: datetime | None,
    require_exp: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    header = _header(token)
    algorithm = header.get("alg")
    if algorithm not in ALLOWED_ALGORITHMS:
        raise TokenValidationError("alg-not-allowed")
    if _FORBIDDEN_HEADERS & set(header):
        raise TokenValidationError("crit-header")
    kid = header.get("kid")
    if kid is not None and (not isinstance(kid, str) or len(kid) > 256):
        raise TokenValidationError("malformed")
    try:
        candidates = keys(kid, str(algorithm))
    except DiscoveryError:
        raise TokenValidationError("jwks-unavailable") from None
    if not candidates:
        raise TokenValidationError("unknown-kid")
    claims: dict[str, Any] | None = None
    for candidate in candidates:
        try:
            decoded = jwt.decode(
                token,
                key=candidate.key,
                algorithms=[str(algorithm)],
                options={
                    "verify_signature": True,
                    "verify_aud": False,
                    "verify_iss": False,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.InvalidSignatureError:
            continue
        except jwt.PyJWTError:
            raise TokenValidationError("malformed") from None
        if isinstance(decoded, dict):
            claims = decoded
            break
    if claims is None:
        raise TokenValidationError("bad-signature")

    now = now or clock.utcnow()
    if claims.get("iss") != issuer:
        raise TokenValidationError("invalid-iss")
    _check_audience(claims, client_id)
    exp = _timestamp(claims, "exp", required=require_exp)
    if exp is not None and now >= exp + CLOCK_SKEW:
        raise TokenValidationError("expired")
    iat = _timestamp(claims, "iat", required=True)
    if iat is None or iat > now + CLOCK_SKEW:
        raise TokenValidationError("iat-in-future")
    if iat < now - MAX_TOKEN_AGE - CLOCK_SKEW:
        raise TokenValidationError("iat-too-old")
    nbf = _timestamp(claims, "nbf", required=False)
    if nbf is not None and nbf > now + CLOCK_SKEW:
        raise TokenValidationError("not-yet-valid")
    return header, claims


def _subject(claims: Mapping[str, Any], *, required: bool) -> str | None:
    sub = claims.get("sub")
    if sub is None and not required:
        return None
    if not isinstance(sub, str) or not sub or len(sub) > 255:
        raise TokenValidationError("invalid-sub")
    return sub


def validate_id_token(
    token: str,
    *,
    issuer: str,
    client_id: str,
    keys: KeyLookup,
    nonce: str | None,
    access_token: str | None = None,
    now: datetime | None = None,
) -> ValidatedToken:
    """Validate an ID token from the token endpoint (module doc, steps 1-4).

    ``nonce=None`` is only for refresh-token responses, where OIDC Core says
    the ID token carries no new nonce; a nonce, if present, is then ignored.
    """
    header, claims = _verify(
        token, issuer=issuer, client_id=client_id, keys=keys, now=now, require_exp=True
    )
    subject = _subject(claims, required=True)
    if nonce is not None:
        claimed = claims.get("nonce")
        if not isinstance(claimed, str) or not hmac.compare_digest(
            claimed.encode("utf-8"), nonce.encode("utf-8")
        ):
            raise TokenValidationError("bad-nonce")
    claimed_hash = claims.get("at_hash")
    if claimed_hash is not None:
        if not access_token or not isinstance(claimed_hash, str):
            raise TokenValidationError("bad-at-hash")
        if not hmac.compare_digest(
            claimed_hash.encode("ascii", "replace"), at_hash(access_token).encode("ascii")
        ):
            raise TokenValidationError("bad-at-hash")
    return ValidatedToken(
        claims=claims,
        issuer=issuer,
        subject=subject,
        algorithm=str(header["alg"]),
        kid=header.get("kid"),
    )


def validate_logout_token(
    token: str,
    *,
    issuer: str,
    client_id: str,
    keys: KeyLookup,
    now: datetime | None = None,
) -> ValidatedToken:
    """Validate a back-channel ``logout_token`` (module doc, steps 1-3 and 5)."""
    header, claims = _verify(
        token, issuer=issuer, client_id=client_id, keys=keys, now=now, require_exp=False
    )
    typ = header.get("typ")
    if typ is not None and (not isinstance(typ, str) or typ.lower() not in _LOGOUT_TYPES):
        raise TokenValidationError("invalid-typ")
    if "nonce" in claims:
        raise TokenValidationError("nonce-in-logout-token")
    jti = claims.get("jti")
    if jti is None:
        raise TokenValidationError("missing-jti")
    if not isinstance(jti, str) or not jti or len(jti) > MAX_JTI_LENGTH:
        raise TokenValidationError("invalid-jti")
    events = claims.get("events")
    if not isinstance(events, dict) or not isinstance(events.get(LOGOUT_EVENT), dict):
        raise TokenValidationError("missing-logout-event")
    subject = _subject(claims, required=False)
    sid = claims.get("sid")
    if sid is not None and (not isinstance(sid, str) or not sid or len(sid) > 255):
        raise TokenValidationError("invalid-sid")
    if subject is None and sid is None:
        raise TokenValidationError("missing-sub-and-sid")
    return ValidatedToken(
        claims=claims,
        issuer=issuer,
        subject=subject,
        algorithm=str(header["alg"]),
        kid=header.get("kid"),
    )

# SPDX-License-Identifier: AGPL-3.0-or-later
"""WebAuthn / passkeys (py_webauthn; AUTH.md "Multi-factor").

* **Relying party** (:class:`RelyingParty`): the RP id is the host of
  ``PERFIN_PUBLIC_URL`` and the only accepted origin is that URL's origin;
  neither is ever taken from the request.
* **Challenges**: 32 random bytes per ceremony, stored server-side in
  ``mfa_challenge`` bound to the session (and user), valid for
  :data:`app.auth.mfa.challenges.WEBAUTHN_CHALLENGE_TTL`, and **spent before
  the response is checked** (``challenges.consume`` commits on its own), so a
  challenge is accepted at most once even when verification fails.
* **Registration**: attestation ``none`` (no attestation trust decisions),
  user verification *preferred*, resident keys (discoverable credentials /
  passkeys) *preferred*, the user's existing credentials excluded. The
  credential id, COSE public key, sign counter and transports are stored in
  ``mfa_method``.
* **Assertions**: the credential must belong to the user of the session; the
  ``userHandle``, when present, must be that user; the signature, RP id hash,
  origin, challenge and user presence are checked by py_webauthn, and the
  **sign counter** must increase whenever the authenticator reports one
  (a cloned authenticator is refused). The new counter is stored.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import parse_authentication_credential_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticationCredential,
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.auth.mfa import challenges
from app.auth.mfa.models import ChallengePurpose
from app.auth.models import MfaMethod, MfaType, User
from app.core import clock
from app.core.logging import get_logger
from app.core.settings import Settings

log = get_logger(__name__)

RP_NAME: Final = "PerFin"
CHALLENGE_BYTES: Final = 32
TIMEOUT_MS: Final = 120_000
MAX_CREDENTIALS_PER_USER: Final = 20
_TRANSPORTS: Final = frozenset(t.value for t in AuthenticatorTransport)
# Malformed client input surfaces as any of these from the parsers.
_BAD_INPUT: Final = (WebAuthnException, ValueError, TypeError, KeyError, IndexError, AttributeError)


class WebAuthnError(Exception):
    """A ceremony failed. ``reason`` is for logs only, never for the client."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NoCredentialsError(LookupError):
    """The user has no WebAuthn credential to authenticate with."""


@dataclass(frozen=True)
class RelyingParty:
    id: str
    name: str
    origin: str

    @classmethod
    def from_public_url(cls, public_url: str) -> RelyingParty:
        parts = urlsplit(public_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("PERFIN_PUBLIC_URL must be an absolute http(s) URL")
        host = parts.hostname.lower()
        default_port = 443 if parts.scheme == "https" else 80
        port = parts.port
        netloc = host if port in (None, default_port) else f"{host}:{port}"
        if ":" in host:  # IPv6 literal
            netloc = f"[{host}]" if port in (None, default_port) else f"[{host}]:{port}"
        return cls(id=host, name=RP_NAME, origin=f"{parts.scheme}://{netloc}")

    @classmethod
    def from_settings(cls, settings: Settings) -> RelyingParty:
        return cls.from_public_url(settings.public_url)


def user_credentials(db: Session, user_id: uuid.UUID) -> list[MfaMethod]:
    return list(
        db.execute(
            select(MfaMethod)
            .where(MfaMethod.user_id == user_id, MfaMethod.type == MfaType.WEBAUTHN)
            .order_by(MfaMethod.created_at, MfaMethod.id)
        ).scalars()
    )


def _descriptor(method: MfaMethod) -> PublicKeyCredentialDescriptor:
    if method.credential_id is None:  # pragma: no cover - check constraint
        raise ValueError("webauthn method without credential id")
    transports = [AuthenticatorTransport(t) for t in (method.transports or []) if t in _TRANSPORTS]
    return PublicKeyCredentialDescriptor(id=method.credential_id, transports=transports or None)


def _options_dict(options: Any) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(options_to_json(options))
    return loaded


def _transports_from(credential: Mapping[str, Any]) -> list[str] | None:
    response = credential.get("response")
    raw = response.get("transports") if isinstance(response, Mapping) else None
    if not isinstance(raw, list):
        return None
    clean = sorted({t for t in raw if isinstance(t, str) and t in _TRANSPORTS})
    return clean or None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def registration_options(
    db: Session,
    rp: RelyingParty,
    *,
    session_id: uuid.UUID,
    user: User,
    now: datetime | None = None,
) -> dict[str, Any]:
    """``PublicKeyCredentialCreationOptions`` (JSON) and a stored challenge."""
    existing = user_credentials(db, user.id)
    if len(existing) >= MAX_CREDENTIALS_PER_USER:
        raise WebAuthnError("too many credentials")
    options = generate_registration_options(
        rp_id=rp.id,
        rp_name=rp.name,
        user_name=user.username,
        user_id=user.id.bytes,
        user_display_name=user.display_name,
        timeout=TIMEOUT_MS,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=[_descriptor(m) for m in existing],
        challenge=_new_challenge(),
    )
    challenges.replace(
        db,
        challenges.new_challenge_row(
            session_id=session_id,
            user_id=user.id,
            purpose=ChallengePurpose.WEBAUTHN_REGISTER,
            ttl=challenges.WEBAUTHN_CHALLENGE_TTL,
            challenge=options.challenge,
            now=now,
        ),
    )
    return _options_dict(options)


def verify_registration(
    db: Session,
    session_factory: sessionmaker[Session],
    rp: RelyingParty,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    credential: Mapping[str, Any],
    name: str,
    now: datetime | None = None,
) -> MfaMethod:
    """Verify ``navigator.credentials.create()`` output and store the credential."""
    now = now or clock.utcnow()
    spent = challenges.consume(
        session_factory,
        session_id=session_id,
        purpose=ChallengePurpose.WEBAUTHN_REGISTER,
        now=now,
    )
    if spent is None or spent.user_id != user_id or spent.challenge is None:
        raise WebAuthnError("no live registration challenge")
    try:
        verified = verify_registration_response(
            credential=dict(credential),
            expected_challenge=spent.challenge,
            expected_rp_id=rp.id,
            expected_origin=rp.origin,
            require_user_presence=True,
            require_user_verification=False,
        )
    except _BAD_INPUT as exc:
        raise WebAuthnError(f"registration rejected: {type(exc).__name__}") from None
    taken = db.execute(
        select(MfaMethod.id).where(MfaMethod.credential_id == verified.credential_id)
    ).first()
    if taken is not None:
        raise WebAuthnError("credential already registered")
    method = MfaMethod(
        user_id=user_id,
        type=MfaType.WEBAUTHN,
        name=name,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=_transports_from(credential),
        created_at=now,
        updated_at=now,
    )
    db.add(method)
    db.flush()
    return method


# ---------------------------------------------------------------------------
# Authentication (login second step and step-up)
# ---------------------------------------------------------------------------


def _new_challenge() -> bytes:
    return secrets.token_bytes(CHALLENGE_BYTES)


def authentication_options(
    db: Session,
    rp: RelyingParty,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    now: datetime | None = None,
) -> dict[str, Any]:
    """``PublicKeyCredentialRequestOptions`` (JSON) for the user's credentials."""
    existing = user_credentials(db, user_id)
    if not existing:
        raise NoCredentialsError
    options = generate_authentication_options(
        rp_id=rp.id,
        challenge=_new_challenge(),
        timeout=TIMEOUT_MS,
        allow_credentials=[_descriptor(m) for m in existing],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    challenges.replace(
        db,
        challenges.new_challenge_row(
            session_id=session_id,
            user_id=user_id,
            purpose=ChallengePurpose.WEBAUTHN_AUTHENTICATE,
            ttl=challenges.WEBAUTHN_CHALLENGE_TTL,
            challenge=options.challenge,
            now=now,
        ),
    )
    return _options_dict(options)


def _parse_assertion(credential: Mapping[str, Any]) -> AuthenticationCredential:
    try:
        return parse_authentication_credential_json(dict(credential))
    except _BAD_INPUT as exc:
        raise WebAuthnError(f"malformed assertion: {type(exc).__name__}") from None


def verify_assertion(
    db: Session,
    session_factory: sessionmaker[Session],
    rp: RelyingParty,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    credential: Mapping[str, Any],
    now: datetime | None = None,
) -> MfaMethod:
    """Verify ``navigator.credentials.get()`` output for the session's user.

    The challenge is spent first (own transaction). On success the stored
    sign counter and ``last_used_at`` are updated in the caller's transaction.
    """
    now = now or clock.utcnow()
    spent = challenges.consume(
        session_factory,
        session_id=session_id,
        purpose=ChallengePurpose.WEBAUTHN_AUTHENTICATE,
        now=now,
    )
    if spent is None or spent.user_id != user_id or spent.challenge is None:
        raise WebAuthnError("no live authentication challenge")
    parsed = _parse_assertion(credential)
    method = db.execute(
        select(MfaMethod)
        .where(
            MfaMethod.user_id == user_id,
            MfaMethod.type == MfaType.WEBAUTHN,
            MfaMethod.credential_id == parsed.raw_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if method is None or method.public_key is None:
        raise WebAuthnError("unknown credential")
    handle = parsed.response.user_handle
    if handle and bytes(handle) != user_id.bytes:
        raise WebAuthnError("user handle mismatch")
    try:
        verified = verify_authentication_response(
            credential=parsed,
            expected_challenge=spent.challenge,
            expected_rp_id=rp.id,
            expected_origin=rp.origin,
            credential_public_key=method.public_key,
            credential_current_sign_count=method.sign_count,
            require_user_verification=False,
        )
    except _BAD_INPUT as exc:
        raise WebAuthnError(f"assertion rejected: {type(exc).__name__}") from None
    method.sign_count = verified.new_sign_count
    method.last_used_at = now
    method.updated_at = now
    db.flush()
    return method


def remove(db: Session, user_id: uuid.UUID, method_id: uuid.UUID) -> bool:
    method = db.execute(
        select(MfaMethod).where(
            MfaMethod.id == method_id,
            MfaMethod.user_id == user_id,
            MfaMethod.type == MfaType.WEBAUTHN,
        )
    ).scalar_one_or_none()
    if method is None:
        return False
    db.delete(method)
    db.flush()
    return True

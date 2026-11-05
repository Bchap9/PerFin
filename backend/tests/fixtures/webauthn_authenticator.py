# SPDX-License-Identifier: AGPL-3.0-or-later
"""A software WebAuthn authenticator for tests (no browser, no hardware).

:class:`SoftAuthenticator` holds a fresh P-256 key and produces what
``navigator.credentials.create()`` / ``.get()`` would return, as the JSON the
SPA posts: attestation ``none``, ES256 signatures over ``authenticatorData ||
SHA-256(clientDataJSON)``, a sign counter that increases by one per assertion
unless a test says otherwise. Every value is generated in-test.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import struct
from dataclasses import dataclass, field
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

FLAG_UP = 0x01
FLAG_UV = 0x04
FLAG_AT = 0x40


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass
class SoftAuthenticator:
    rp_id: str
    origin: str
    credential_id: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    sign_count: int = 0
    user_handle: bytes | None = None
    private_key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1()), repr=False
    )

    # ---- building blocks -------------------------------------------------
    def cose_public_key(self) -> bytes:
        numbers = self.private_key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,  # kty: EC2
                3: -7,  # alg: ES256
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _rp_hash(self, rp_id: str | None = None) -> bytes:
        return hashlib.sha256((rp_id or self.rp_id).encode()).digest()

    @staticmethod
    def _client_data(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False},
            separators=(",", ":"),
        ).encode()

    # ---- ceremonies --------------------------------------------------------
    def create(self, options: dict[str, Any], *, origin: str | None = None) -> dict[str, Any]:
        """A registration response for ``PublicKeyCredentialCreationOptions`` JSON."""
        self.user_handle = base64.urlsafe_b64decode(options["user"]["id"] + "==")
        client_data = self._client_data(
            "webauthn.create", options["challenge"], origin or self.origin
        )
        attested = (
            bytes(16)  # AAGUID
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self.cose_public_key()
        )
        auth_data = (
            self._rp_hash()
            + bytes([FLAG_UP | FLAG_UV | FLAG_AT])
            + struct.pack(">I", self.sign_count)
            + attested
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "attestationObject": b64url(attestation),
                "transports": ["usb", "nfc", "bogus-transport"],
            },
            "clientExtensionResults": {},
        }

    def get(
        self,
        options: dict[str, Any],
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        increment: int = 1,
        user_handle: bytes | None = None,
        challenge: str | None = None,
    ) -> dict[str, Any]:
        """An assertion for ``PublicKeyCredentialRequestOptions`` JSON."""
        self.sign_count += increment
        client_data = self._client_data(
            "webauthn.get", challenge or options["challenge"], origin or self.origin
        )
        auth_data = (
            self._rp_hash(rp_id) + bytes([FLAG_UP | FLAG_UV]) + struct.pack(">I", self.sign_count)
        )
        signature = self.private_key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        handle = user_handle if user_handle is not None else self.user_handle
        response: dict[str, Any] = {
            "clientDataJSON": b64url(client_data),
            "authenticatorData": b64url(auth_data),
            "signature": b64url(signature),
        }
        if handle is not None:
            response["userHandle"] = b64url(handle)
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": response,
            "clientExtensionResults": {},
        }

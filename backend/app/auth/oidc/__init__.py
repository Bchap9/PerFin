# SPDX-License-Identifier: AGPL-3.0-or-later
"""OpenID Connect login (AUTH.md "OIDC (Authentik)"; SECURITY.md A9, B1-B9).

* ``client``: authorization request (state, nonce, PKCE S256 in a sealed
  pre-auth cookie), code exchange, refresh, end-session URL.
* ``discovery``: discovery document (exact issuer, endpoints on the issuer's
  host) and JWKS cache through ``app.core.http`` (SSRF-safe, B5, B9).
* ``tokens``: ID and logout token validation (pinned RS256/ES256, A9, B2).
* ``provisioning``: ``(iss, sub)`` lookup, link by verified email, JIT,
  group-to-role mapping (B3, B6).
* ``flow``: the callback and the link confirmation.
* ``logout``: OIDC session data, RP-initiated and back-channel logout (B4, B8).
* ``refresh``: optional claim refresh with the refresh token (B8).
* ``models``: ``oidc_session``, ``oidc_used_state``.

Importing the package registers the session-rotation hook that keeps
``oidc_session`` attached to the current session id.
"""

from __future__ import annotations

from app.auth.oidc import logout as _logout  # noqa: F401 - registers the rotation hook

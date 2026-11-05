# OIDC sign-in: what leaves the host

Scope: optional single sign-on with an OpenID Connect provider such as
Authentik (`PERFIN_OIDC_*`, [AUTH.md](../AUTH.md), [AUTHENTIK.md](../AUTHENTIK.md)).
Control ids refer to [SECURITY.md](../SECURITY.md) (B1 to B9); status is in
[CONTROLS.md](CONTROLS.md).

## Short answer

**Nothing leaves the host unless you set `PERFIN_OIDC_ISSUER`.** PerFin talks
only to the issuer you configured, never to a provider-supplied host outside
that origin, and sends no financial data, document, or household information.
It sends the protocol messages OIDC needs and receives identity claims.

## What crosses the boundary

| When | From | To | What is sent | What comes back |
| --- | --- | --- | --- | --- |
| First login, then at most hourly (`PERFIN_OIDC_METADATA_MAX_AGE_SECONDS`) | `api` | `<issuer>/.well-known/openid-configuration` | An HTTPS GET, no credentials | Endpoint URLs; must carry the exact issuer and stay on the issuer's origin |
| Login, and when an unknown key id appears (at most every `PERFIN_OIDC_JWKS_MIN_REFRESH_SECONDS`, 300) | `api` | the provider's `jwks_uri` | An HTTPS GET | Public signing keys |
| Login callback | `api` | the provider's token endpoint | The authorisation `code`, the PKCE verifier, `redirect_uri`, `client_id` and the client secret (`client_secret_basic`) or a signed `private_key_jwt` assertion | ID token (signature, `iss`, `aud`, `exp`, `iat`, `nonce`, `at_hash` checked), access token (not kept), refresh token only when claim refresh is on |
| Claim refresh, if `PERFIN_OIDC_CLAIM_REFRESH_MINUTES` > 0 | `worker` job `oidc_claim_refresh` | the token endpoint | The stored refresh token and client authentication | Fresh claims (groups, account still enabled) |
| Logout | the user's browser | the provider's `end_session_endpoint` | A redirect carrying `id_token_hint` and an allowlisted `post_logout_redirect_uri` | |
| Back-channel logout | the provider | `POST /api/v1/auth/oidc/backchannel-logout` | A signed `logout_token` (inbound) | |

Browser redirects (the authorisation request with `state`, `nonce`, PKCE
challenge and scopes `PERFIN_OIDC_SCOPES`) go from the user's browser to the
provider, not from PerFin. The provider learns that a user is signing in to
PerFin at `PERFIN_PUBLIC_URL`; that is inherent to single sign-on.

## What is stored

The provider's subject and issuer to link the account; claims used for the
role mapping at login (group names, verified email); the ID token as a
logout hint and the refresh token, both **column-encrypted** and stored only
when needed for logout or claim refresh. Access tokens and ID tokens are not
kept beyond the request otherwise.

## Transport rules

- All requests use the SSRF-safe HTTP client (B5): HTTPS only, every resolved
  address must be public (a LAN provider needs
  `PERFIN_OIDC_ALLOW_PRIVATE_ISSUER=true`, and warns), redirects only within
  the same origin, bodies capped, timeouts (`PERFIN_OIDC_HTTP_TIMEOUT_SECONDS`).
- In the Compose deployment `api` and `worker` have no route out. With
  `HTTPS_PROXY` set the connection is a `CONNECT` tunnel through the `egress`
  proxy, which reaches only `deploy/egress-allowlist.txt`; add exactly the
  issuer's `host:443`.
- The redirect URI is fixed in configuration, never taken from a request.

## Your controls

Leave `PERFIN_OIDC_ISSUER` empty to disable. Choose
`client_secret_basic` or `private_key_jwt` (`PERFIN_OIDC_TOKEN_AUTH_METHOD`).
Leave `PERFIN_OIDC_CLAIM_REFRESH_MINUTES=0` to avoid storing a refresh token
(back-channel logout still works). Keep
`PERFIN_OIDC_TRUST_IDP_MFA=false` unless the provider enforces MFA.
`PERFIN_OIDC_CLIENT_SECRET` reaches only `api` and `worker`, never `migrate`
or `mcp`.

## Verifying it

- `backend/tests/security/test_oidc_negative.py`: bad `state`, `nonce`,
  `aud`, `iss`, expired or future tokens, unknown `kid`, `alg=none` and key
  confusion are all refused.
- `backend/tests/integration/test_oidc.py`, `backend/tests/unit/test_oidc_units.py`:
  the flow, discovery origin rules, group mapping and logout.
- `backend/tests/security/test_egress_ssrf.py`: private and metadata
  addresses are never contacted.

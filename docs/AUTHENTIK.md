# Authentik setup

This guide connects PerFin to [Authentik](https://goauthentik.io) 2024.x or
2025.x as its OpenID Connect provider. It covers the provider and application,
the groups that map to PerFin roles, MFA at Authentik, logout, user
provisioning, OIDC-only mode with the break-glass admin, an Authentik on the
home LAN, an optional forward-auth gate, and troubleshooting. There are no
screenshots: menu paths and field labels are given as text, and labels can
differ slightly between Authentik releases.

The design behind it is in [AUTH.md](AUTH.md) ("OIDC (Authentik)") and
[SECURITY.md](SECURITY.md) (rows A9, B1-B9). Every `PERFIN_*` variable below
is defined in `backend/app/core/settings.py` and listed in
`deploy/.env.example`.

This guide matches PerFin 0.1 (Phase 1). Limits that PerFin itself still
has are called out where they matter, under **Limitation**.

Placeholders used throughout:

| Placeholder | Meaning |
|---|---|
| `perfin.example.com` | PerFin's host name; `PERFIN_PUBLIC_URL=https://perfin.example.com` |
| `auth.example.com` | Authentik's host name, as browsers reach it |
| `perfin` | The Authentik application **slug** |

## What PerFin requires of the provider

PerFin checks these rules itself and refuses a provider that breaks them.
Authentik meets every one of them when configured as described below.

| Requirement | Why | What breaks it in Authentik |
|---|---|---|
| Issuer is `https://` and matches `PERFIN_OIDC_ISSUER` byte for byte, trailing slash included | B2, B5 | Copying the issuer without the trailing `/`; Authentik behind a proxy that does not pass `X-Forwarded-Proto` (the issuer becomes `http://...`) |
| Every endpoint in the discovery document is on the issuer's scheme, host and port | Egress allowlist = issuer host (B5) | Reaching Authentik by a different name than browsers do (Authentik builds every URL from the request's `Host`) |
| Authorization code flow with PKCE `S256` | B1 | Nothing; Authentik supports both |
| ID tokens signed `RS256` or `ES256`, not encrypted | A9 | A provider **without a signing key** signs with `HS256` (refused); an **encryption key** makes the ID token a JWE (refused) |
| Token endpoint authentication `client_secret_basic` | B1 | A **public** client type (no secret). Authentik does not list `private_key_jwt`, so `PERFIN_OIDC_TOKEN_AUTH_METHOD` stays at its default |
| All claims PerFin reads are **in the ID token** (`sub`, `email`, `email_verified`, `name`, `preferred_username`, `groups`, `amr`, `sid`) | PerFin never calls the userinfo endpoint | "Include claims in id_token" switched off |
| Clocks within 60 seconds of each other | `exp`, `iat`, `nbf` skew (B2) | A host without NTP |

## 1. Create the groups

PerFin maps three group names to the household roles admin, member and
viewer. Create the groups first, so the application binding in step 5 can
use them.

1. **Directory > Groups > Create**.
2. Create three groups with exactly these names. Leave **Is superuser** off
   and **Parent** empty:
   - `perfin-admins`
   - `perfin-users`
   - `perfin-viewers`
3. Add people: open a group, go to the **Users** tab and click **Add existing
   user**. You can also open the user in **Directory > Users**, go to the
   **Groups** tab and click **Add to existing group**.

How PerFin uses them (`app/auth/oidc/provisioning.py`):

| Authentik group | PerFin setting | Household role |
|---|---|---|
| `perfin-admins` | `PERFIN_OIDC_GROUP_ADMIN` | admin |
| `perfin-users` | `PERFIN_OIDC_GROUP_MEMBER` | member |
| `perfin-viewers` | `PERFIN_OIDC_GROUP_VIEWER` | viewer |

- Names are compared exactly, and the comparison is case-sensitive. If you
  name the groups differently, set the three variables to your names.
- If a user is in several mapped groups, the strongest role wins.
- The role applies in one household: `PERFIN_OIDC_HOUSEHOLD_ID`, or the
  instance's only household when that variable is unset. If there are several
  households and none is configured, no membership is created or changed (the
  API logs a warning).
- The membership is created or updated on every OIDC login, and on every
  claim refresh if that is enabled. Removing someone from `perfin-admins` in
  Authentik demotes them in PerFin at their next login or refresh.
- A group never makes anyone a household **owner** and never makes anyone a
  PerFin **instance admin** (SECURITY.md B6). An existing `owner` membership
  is never changed by groups. Ownership is granted inside PerFin by another
  owner. Instance admin comes only from the local database (see
  [OIDC-only mode](#9-oidc-only-mode-and-the-break-glass-admin)).
- With `PERFIN_OIDC_REQUIRE_GROUP=true` (the default), a user in none of the
  three groups is refused with `not-in-group`, even if their account was
  already linked.

## 2. Create the `groups` scope mapping

PerFin requests the scopes `openid profile email groups`
(`PERFIN_OIDC_SCOPES`). Authentik has built-in mappings for the first three.
Create the fourth one:

1. **Customization > Property Mappings > Create**, then choose **Scope
   Mapping**. Older releases call it **OAuth2/OpenID Scope Mapping**.
2. Fill in:
   - **Name**: `PerFin: groups`
   - **Scope name**: `groups`
   - **Description**: `Your PerFin groups`. This text appears on the consent
     screen when you use explicit consent.
   - **Expression**:

     ```python
     return {
         "groups": [
             group.name
             for group in request.user.ak_groups.all()
             if group.name.startswith("perfin-")
         ],
     }
     ```

     The `startswith("perfin-")` filter keeps the user's other Authentik
     groups out of PerFin's tokens. PerFin ignores unmapped names anyway, so
     the filter only limits what is disclosed. If you renamed the groups in
     step 1, adjust the prefix, or drop the `if` line to send every group.
     `groups` must be a JSON list of strings; PerFin reads at most 200
     entries.
3. **Finish**.

Authentik's built-in **profile** mapping (`authentik default OAuth Mapping:
OpenID 'profile'`) also emits a `groups` claim with **every** group of the
user. To keep the filter effective, you can replace that mapping with your own
`profile` scope mapping that leaves `groups` out:

```python
return {
    "name": request.user.name,
    "given_name": request.user.name,
    "preferred_username": request.user.username,
    "nickname": request.user.username,
}
```

Give it **Scope name** `profile`, then select it instead of the built-in one
in step 4. If you do not mind PerFin seeing all group names, keep the
built-in profile mapping.

## 3. Choose the signing key

The ID token must be signed with `RS256` or `ES256`. Authentik derives the
algorithm from the certificate you select as the provider's **Signing Key**:

- **RS256**: select `authentik Self-signed Certificate` (an RSA key), or any
  RSA certificate of at least 2048 bits.
- **ES256**: go to **System > Certificates > Generate**, set **Common Name**
  to `perfin-oidc-signing`, **Private key Algorithm** to `ECDSA` (curve
  P-256), and **Validity days** to suit your rotation schedule (for example
  `365`). Then select it in step 4.

Never leave **Signing Key** empty. Without one, Authentik signs with `HS256`
using the client secret, and PerFin refuses the provider: the discovery
document lists no allowed algorithm, so the login stops with `idp-unavailable`.

## 4. Create the provider

**Applications > Providers > Create > OAuth2/OpenID Provider**, then fill in:

| Field | Value |
|---|---|
| Name | `PerFin` |
| Authentication flow | Leave empty (the brand's default authentication flow), or a dedicated flow that enforces MFA (step 7) |
| Authorization flow | `default-provider-authorization-implicit-consent` (no consent screen; fine for your own household app) or `default-provider-authorization-explicit-consent` (users confirm the scopes once). PerFin works with either |
| Invalidation flow (2024.10+) | `default-provider-invalidation-flow` |
| **Protocol settings** | |
| Client type | **Confidential** |
| Client ID | Keep the generated value; it becomes `PERFIN_OIDC_CLIENT_ID` |
| Client Secret | Keep the generated value; it becomes `PERFIN_OIDC_CLIENT_SECRET`. Treat it like a password |
| Redirect URIs | See below |
| Signing Key | The certificate from step 3 |
| Encryption Key | **Empty**. PerFin does not accept encrypted (JWE) ID tokens |
| **Advanced protocol settings** | |
| Access code validity | `minutes=1` |
| Access Token validity | `minutes=5`. PerFin uses the access token only to check `at_hash` and then discards it |
| Refresh Token validity | Only matters with claim refresh (step 8). Then make it at least as long as PerFin's longest session: `days=30`, matching `PERFIN_SESSION_REMEMBER_DAYS` |
| Scopes | Select `authentik default OAuth Mapping: OpenID 'openid'`, `... 'email'`, `... 'profile'` (or your own profile mapping from step 2), and `PerFin: groups`. Add `... 'offline_access'` only for claim refresh (step 8) |
| Subject mode | **Based on the User's hashed ID** (the default) |
| Include claims in id_token | **On**. PerFin reads claims only from the ID token |
| Issuer mode | **Each provider has a different issuer, based on the application slug** (the default) |

**Redirect URIs.** PerFin's redirect URI is fixed:
`<PERFIN_PUBLIC_URL>/api/v1/auth/oidc/callback`. PerFin never takes it from
the request. Register exactly that URI and nothing else:

- **Authentik 2024.12 and later**: add one entry with matching mode
  **Strict** and URL `https://perfin.example.com/api/v1/auth/oidc/callback`.
- **Earlier 2024.x**: the field is a list of regular expressions, matched
  against the whole URI. Escape the dots and anchor the pattern:
  `^https://perfin\.example\.com/api/v1/auth/oidc/callback$`. Never leave the
  field empty: Authentik would then save the first redirect URI it sees.
  Never use `*`.

**Subject mode.** Choose it once and never change it. PerFin finds an
account by `(issuer, sub)` only (SECURITY.md B3). Changing the subject mode,
the application slug (which is part of the issuer), the issuer mode, or moving
to a new Authentik install (the hashed ID includes the install's ID) gives
everyone a new identity. PerFin then refuses them with `account-exists`, or
JIT-creates second accounts if their email is unknown to PerFin. Hashed IDs
also avoid putting a guessable value or a username in PerFin's database.

**Back-channel logout** (Authentik 2025.x releases that support it): on the
same form, set the logout URI to
`https://perfin.example.com/api/v1/auth/oidc/backchannel-logout` and the
logout method to **Back-channel**. See step 8. If your release shows no
logout URI field, it cannot do back-channel logout; use claim refresh instead.

Click **Finish**.

## 5. Create the application and bind the groups

1. **Applications > Applications > Create**:
   - **Name**: `PerFin`
   - **Slug**: `perfin`. The slug is part of the issuer URL; do not change it
     later.
   - **Provider**: `PerFin` (from step 4)
   - **Policy engine mode**: `any`
   - **Launch URL**: `https://perfin.example.com/`
2. Open the application, go to the **Policy / Group / User Bindings** tab and
   use **Bind existing policy/group/user**. Bind the **groups**
   `perfin-admins`, `perfin-users` and `perfin-viewers`, one binding each.

With these bindings, Authentik itself refuses users outside the three groups
(they see Authentik's "permission denied" page and never reach PerFin).
`PERFIN_OIDC_REQUIRE_GROUP=true` repeats the check on PerFin's side, so a
missing binding cannot let them in.

3. On the provider's overview page, copy:
   - **OpenID Configuration Issuer**, for example
     `https://auth.example.com/application/o/perfin/` **with** the trailing
     slash. It becomes `PERFIN_OIDC_ISSUER`.
   - **Client ID** and **Client Secret** (under **Edit**).

Check the discovery document from any machine:

```sh
curl -s https://auth.example.com/application/o/perfin/.well-known/openid-configuration \
  | jq '{issuer, id_token_signing_alg_values_supported, token_endpoint_auth_methods_supported, code_challenge_methods_supported}'
```

Check that `issuer` equals the value you copied, that the algorithms include
`RS256` or `ES256`, that the methods include `client_secret_basic`, and that
the code challenge methods include `S256`.

## 6. Configure PerFin

In `deploy/.env` (the variable names are those of `deploy/.env.example`):

```sh
PERFIN_PUBLIC_URL=https://perfin.example.com

PERFIN_OIDC_ISSUER=https://auth.example.com/application/o/perfin/
PERFIN_OIDC_CLIENT_ID=<client id from Authentik>
PERFIN_OIDC_CLIENT_SECRET=<client secret from Authentik>
PERFIN_OIDC_SCOPES=openid profile email groups
# Authentik offers client_secret_basic; leave the default.
# PERFIN_OIDC_TOKEN_AUTH_METHOD=client_secret_basic

PERFIN_OIDC_GROUP_ADMIN=perfin-admins
PERFIN_OIDC_GROUP_MEMBER=perfin-users
PERFIN_OIDC_GROUP_VIEWER=perfin-viewers
PERFIN_OIDC_REQUIRE_GROUP=true
# Only needed with more than one household:
# PERFIN_OIDC_HOUSEHOLD_ID=<household uuid>

# Step 10 (users):
PERFIN_OIDC_JIT_PROVISIONING=false
PERFIN_OIDC_LINK_BY_VERIFIED_EMAIL=false
# Step 7 (MFA at Authentik):
PERFIN_OIDC_TRUST_IDP_MFA=false
# Step 8 (logout and claim refresh):
# PERFIN_OIDC_POST_LOGOUT_REDIRECT_URIS=https://perfin.example.com/
PERFIN_OIDC_CLAIM_REFRESH_MINUTES=0
```

`PERFIN_OIDC_HOUSEHOLD_ID` is the `household.id` that
`GET /api/v1/auth/session` returns while that household is active.

**Egress.** In the reference Compose deployment the API container has no
route to the internet or the LAN. All of its networks are `internal`. It
reaches the IdP only through the `egress` CONNECT proxy:

1. In `deploy/egress-allowlist.txt`, add the Authentik host:
   `auth.example.com:443`.
2. In `deploy/.env`, uncomment `HTTPS_PROXY=http://egress:3128` and
   `NO_PROXY=localhost,127.0.0.1,db,api,clamav`.
3. Start with the profile: `docker compose --profile egress up -d`.

For an Authentik on a private address, also see
[step 11](#11-authentik-on-a-private-lan-address).

Then check and restart:

```sh
docker compose exec api perfin config check
docker compose up -d api worker
```

`perfin config check` refuses a non-`https` or malformed issuer, a missing
client id or secret, or a placeholder secret. It warns about JIT without
`PERFIN_OIDC_REQUIRE_GROUP`.

**Test.** `GET /api/v1/auth/oidc/status` reports whether OIDC login is
offered. A browser that opens `https://perfin.example.com/api/v1/auth/oidc/start`
is sent to Authentik and, after login, back to PerFin. In the web app, the
sign-in page (`/login`) shows a **Sign in with Authentik** button whenever
OIDC is configured; it starts the same flow and returns to the page the user
wanted. A user with a PerFin second factor (and IdP MFA not trusted) continues
at `/login/mfa`; a pending link by verified email opens `/login/link-oidc`,
which asks for that account's PerFin password before linking. Callback errors
are shown on `/login` (`?oidc_error=<code>`).

To see the claims Authentik will send, open the provider in Authentik and use
its **Preview** tab (available in recent releases): choose a user and check
`iss`, `sub`, `email`, `email_verified`, `groups` and, after step 7, `amr`.

## 7. Enforce MFA at Authentik and let PerFin trust it

By default PerFin does its own second factor. A user with a PerFin TOTP or
WebAuthn factor gets an `mfa_pending` session after Authentik and must verify
locally. With `PERFIN_REQUIRE_MFA=true`, a user without one gets a session
that can only enrol a factor. If Authentik already enforces MFA, PerFin can
accept that instead.

### 7a. Enforce MFA in Authentik

The default authentication flow, `default-authentication-flow`, already
contains an **Authenticator Validation Stage**
(`default-authentication-mfa-validation`) that by default *skips* users
without a device. Make it mandatory:

1. **Flows and Stages > Stages**, then edit
   `default-authentication-mfa-validation`. To restrict this to PerFin
   instead, create a new **Authenticator Validation Stage**, put it in a copy
   of the authentication flow, and select that flow as the provider's
   **Authentication flow** (but see the caveat below).
2. Set:
   - **Device classes**: `WebAuthn Authenticators` and `TOTP Authenticators`.
     Add `Static Tokens` if you want Authentik recovery codes. Avoid SMS and
     email codes.
   - **Not configured action**: **Force the user to configure an
     authenticator**. Then select the configuration stages
     `default-authenticator-webauthn-setup` and
     `default-authenticator-totp-setup`.
   - **Last validation threshold**: `seconds=0`, so MFA runs on every login
     and Authentik's "remembered" validation never stands in for it.
   - **WebAuthn user verification**: `preferred` or `required`.
3. **Update**.

**Caveat on dedicated flows.** A provider's **Authentication flow** runs only
when the user has no Authentik session yet. A user who signed in to Authentik
for another application through a flow without MFA is already logged in, so
Authentik skips your flow. The `amr` claim then says no MFA happened, so
PerFin does not count it (it fails safe). To make MFA cover every path,
enforce it in the brand's default authentication flow, and check any source
logins (social or LDAP) and recovery flows that log users in.

### 7b. What Authentik puts in `amr`

Authentik fills `amr` from the login event that started the user's Authentik
session. For a password plus a validated device it is `["pwd", "mfa"]`.
PerFin counts `amr` as MFA when it contains `mfa`, or names factors of two
different kinds per RFC 8176 (knowledge `pwd`/`pin`/`kba`, possession
`hwk`/`swk`/`otp`/`sms`/`tel`/`sc`, inherence
`fpt`/`face`/`iris`/`retina`/`vbm`). A passwordless WebAuthn login gives a
single-factor `amr`, so PerFin does not count it as MFA. Check `amr` with the
provider's **Preview** tab or by decoding an ID token.

### 7c. Trust it in PerFin

```sh
PERFIN_OIDC_TRUST_IDP_MFA=true
```

With this set, an OIDC login whose `amr` shows MFA
(`app.auth.mfa.policy.mark_mfa_from_idp`):

- creates a full session with no PerFin second factor, with
  `mfa_verified_at` set to the ID token's `auth_time`: when Authentik
  authenticated the user, **not** when PerFin received the login;
- satisfies `PERFIN_REQUIRE_MFA`;
- counts as step-up **only for a forced sign-in**: a login started with
  `GET /api/v1/auth/oidc/start?reauth=true`, which sends
  `prompt=login&max_age=0`, whose ID token `auth_time` is not older than
  the moment PerFin started that login (minus 60 s of clock skew). The
  step-up then lasts 10 minutes from `auth_time`. A plain sign-in is never
  a step-up, whatever its `auth_time`, so a sign-in that Authentik answers
  from its existing session (SSO, no new MFA) cannot open the step-up
  window even if Authentik stamped it with the current time; neither can
  an ID token without `auth_time`. A sensitive action (creating an API
  token, managing members, changing MFA, `/admin/users`) therefore needs
  either `POST /auth/step-up` with a **PerFin** factor, or the forced
  sign-in at Authentik, which runs the authentication flow again
  (including the MFA stage of 7a). The SPA's verification dialog offers
  this link to OIDC users without a PerFin factor. Enrolling a PerFin
  factor (Settings, Security) remains the simplest step-up for users who
  manage members or tokens. Such a user is not asked for the PerFin factor
  at login while Authentik's MFA is trusted; the PerFin factor then serves
  step-up;
- does **not** let the user create API tokens while `PERFIN_REQUIRE_MFA` is
  on: a bearer request carries no MFA of its own, so tokens need a PerFin
  second factor on the account (`403 mfa-enrollment-required` otherwise).

An `amr` without MFA falls back to PerFin's own rules: `mfa_pending` for
users with a PerFin factor, otherwise a full session (or the enrol-only
session under `PERFIN_REQUIRE_MFA`).

**Check your Authentik version before relying on this** (PerFin cannot
verify the IdP's behaviour; decode the ID tokens, for example from the
provider's Preview tab or a test client, and compare `auth_time` with the
time of day):

1. *Forced sign-in.* Start `GET /api/v1/auth/oidc/start?reauth=true`
   while you already have an Authentik session. Authentik must ask for
   your password **and the MFA device again**, and the ID token's
   `auth_time` must be the time of this sign-in. If it lets you through
   without the MFA stage, the forced sign-in is a step-up without MFA:
   do not trust the IdP's MFA (leave `PERFIN_OIDC_TRUST_IDP_MFA=false`) or
   fix the flow so `prompt=login` re-runs the MFA stage.
2. *Silent SSO sign-in.* Sign in to PerFin normally while you still have
   an Authentik session from earlier (no password asked). The ID token's
   `auth_time` should still be the time of that earlier sign-in, not now.
   PerFin does not depend on this (a plain sign-in is never a step-up), but
   an IdP that stamps `auth_time` at token issue makes the `mfa.verified_at`
   PerFin shows for such sessions misleading, and other relying parties
   may depend on it.

Only set `PERFIN_OIDC_TRUST_IDP_MFA=true` when step 7a is in place for
**every** way a user can obtain an Authentik session. Anyone who can sign in
to Authentik without MFA but gets a forged or misconfigured `amr` would
otherwise bypass PerFin's MFA.

## 8. Logout, back-channel logout and claim refresh

**RP-initiated logout.** PerFin's logout (`POST /api/v1/auth/oidc/logout`)
ends the PerFin session and returns Authentik's `end_session_endpoint` with
`id_token_hint`, `client_id` and a `post_logout_redirect_uri` from
`PERFIN_OIDC_POST_LOGOUT_REDIRECT_URIS` (default
`https://perfin.example.com/`; each entry must be an absolute `https` URL
without query or fragment). Authentik then runs the provider's **Invalidation
flow**. With `default-provider-invalidation-flow` the user sees Authentik's
logout page and can also end their Authentik session there. Whether Authentik
then redirects back to the `post_logout_redirect_uri` depends on the release
and the flow. PerFin's session is gone either way.

**Back-channel logout.** In Authentik releases that support it, set the
provider's logout URI to
`https://perfin.example.com/api/v1/auth/oidc/backchannel-logout` with logout
method **Back-channel** (step 4). When the user's Authentik session ends
(logout, or an admin terminating it under **Directory > Users > Sessions**),
Authentik POSTs a signed `logout_token` to that URL. PerFin validates it
like an ID token (issuer, audience, signature against the JWKS, `iat` window,
back-channel event, no `nonce`). It then revokes every PerFin session of that
issuer matching the token's `sid` (one browser) or `sub` (all of the user's
sessions). Notes:

- Authentik must be able to reach PerFin's public URL. A LAN Authentik needs
  DNS for `perfin.example.com` that resolves to the proxy (hairpin NAT or
  split DNS).
- The endpoint is exempt from CSRF and limited to 60 requests per 5 minutes
  per client IP. It answers `200`, or `400` for any invalid token. Rejections
  are audited as `oidc.backchannel_logout_rejected`.
- If you add the forward-auth gate (step 12), exempt this path from it.

**Claim refresh** (an alternative or addition to back-channel logout).
Set `PERFIN_OIDC_CLAIM_REFRESH_MINUTES` (for example `15`). The worker then
uses each live OIDC session's refresh token every N minutes:

- an `invalid_grant` answer (refresh token revoked or expired) revokes the
  PerFin session;
- a user who has lost every mapped group (with `PERFIN_OIDC_REQUIRE_GROUP`)
  or is inactive in PerFin is revoked;
- otherwise the group mapping is applied again.

Authentik only issues refresh tokens when the client asks for the
`offline_access` scope, so:

1. Add `authentik default OAuth Mapping: OpenID 'offline_access'` to the
   provider's **Scopes** (step 4), and set **Refresh Token validity** at least
   as long as PerFin's longest session (`days=30`).
2. In PerFin, add `offline_access` to the scopes:

   ```sh
   PERFIN_OIDC_SCOPES=openid profile email groups offline_access
   PERFIN_OIDC_CLAIM_REFRESH_MINUTES=15
   ```

The refresh token is stored encrypted and bound to its session row. It is
only stored when claim refresh is on. Keep **Include claims in id_token** on:
the refreshed ID token must carry `groups`, otherwise every refresh looks like
a lost group and revokes the session.

## 9. OIDC-only mode and the break-glass admin

`PERFIN_LOCAL_LOGIN_ENABLED=false` turns off the local password form and
password reset. `POST /auth/login` from outside `PERFIN_BREAKGLASS_CIDRS`
answers `403 local-login-disabled` for every username. From inside those
networks, only accounts flagged **break-glass** can log in. A break-glass
account:

- is refused outside `PERFIN_BREAKGLASS_CIDRS` in **every** mode, including
  while local login is still enabled;
- never logs in through OIDC, and is never offered for email linking;
- never receives a password-reset mail;
- is checked against the CIDR at login and on every later request: its
  session cookie is ignored (`401`) when presented from outside
  `PERFIN_BREAKGLASS_CIDRS`.

`perfin config check` refuses OIDC-only mode unless `PERFIN_OIDC_ISSUER`
and `PERFIN_OIDC_CLIENT_ID` are set, `PERFIN_BREAKGLASS_CIDRS` is set (and
is not `0.0.0.0/0` or `::/0`), and `PERFIN_BREAKGLASS_ADMIN_USERNAME` names
the break-glass account. The check cannot see the database, so it trusts that
name; `perfin breakglass status` checks the account itself (9c).

### 9a. Which address PerFin sees

The CIDR is matched against the client address PerFin resolves: the
`X-Forwarded-For` from the proxy listed in `PERFIN_TRUSTED_PROXIES`, which in
the Compose deployment is the address Caddy saw.

- **From the LAN** (IPv4), Docker keeps the client's real address. A home
  LAN range such as `192.168.1.0/24` therefore works, if you are willing to
  let any device on that LAN attempt a break-glass login.
- **From the Docker host itself**, which is where an SSH tunnel ends, the
  address is **not** `127.0.0.1`. Docker's port publishing forwards host-local
  connections with the source address of the `edge` network's gateway. The
  Compose file gives `edge` a fixed subnet, so that gateway is
  `172.30.79.1` (`EDGE_SUBNET`/`EDGE_GATEWAY` in `deploy/.env` change it if
  the range collides with one of yours). The shipped default
  `PERFIN_BREAKGLASS_CIDRS=127.0.0.1/32` therefore never matches a request
  that goes through the proxy: break-glass login stays closed until you set
  the gateway deliberately, after reading the caveats below.

Check the gateway address:

```sh
docker network inspect perfin_edge --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'
```

To confirm it, make one deliberately failed login through the tunnel and read
the address PerFin recorded:

```sh
docker compose exec db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U postgres perfin -c "SELECT created_at, ip, after FROM audit_log WHERE action = '"'"'user.login_failed'"'"' ORDER BY seq DESC LIMIT 3"'
```

Then set `PERFIN_BREAKGLASS_CIDRS=172.30.79.1/32` and restart the API.

Caveats:

- If you change `EDGE_SUBNET` or `EDGE_GATEWAY`, change
  `PERFIN_BREAKGLASS_CIDRS` with them.
- If the host has IPv6 and the published ports listen on IPv6 while the
  `edge` network is IPv4-only, Docker's userland proxy forwards **every IPv6
  client from the internet** with that same gateway address, and those clients
  would pass the CIDR check. They would still need the break-glass password and
  second factor, and rate limits still apply. Close this hole by publishing
  the proxy on IPv4 only (`PROXY_HTTP_PORT=0.0.0.0:80` and
  `PROXY_HTTPS_PORT=0.0.0.0:443` in `deploy/.env`), or by setting
  `"userland-proxy": false` in `/etc/docker/daemon.json`.
- For the same reason the shipped default is not the gateway: on such a
  host it would admit internet clients to the break-glass check by default.

### 9b. Using the break-glass account through an SSH tunnel

The browser must use PerFin's real origin. The CSRF check requires `Origin`
to equal `PERFIN_PUBLIC_URL`, and WebAuthn is bound to that host name. So
tunnel port 443 and point the name at the tunnel:

1. On your laptop, add a temporary line to `/etc/hosts`:
   `127.0.0.1 perfin.example.com`
2. Open the tunnel. Binding local port 443 needs root:
   `sudo ssh -N -L 127.0.0.1:443:127.0.0.1:443 you@docker-host`
3. Browse to `https://perfin.example.com/`. The certificate is valid because
   the name is unchanged. Log in with the break-glass username, password and
   second factor.
4. When you are done, log out, close the tunnel and remove the `/etc/hosts`
   line.

### 9c. Creating the break-glass account

Set this up and test it **before** switching local login off. While doing
so, keep a second session open as the instance admin.

PerFin's only instance admin is the account created at
[first boot](AUTH.md#admin-bootstrap), and the API never grants instance
admin to anyone else (B6). In OIDC-only mode that bootstrap account cannot
log in locally unless it is the break-glass account. It also cannot use OIDC,
because it has no linked identity (see step 10). So the break-glass account
should be the bootstrap admin. Nobody may change their own account through
`PATCH /api/v1/admin/users/{id}`, and there is no second instance admin to do
it, so the flag is set by the operator on the Docker host with
`perfin breakglass` (`backend/app/cli.py`). It writes a `user.breakglass_set`
audit row (`"via": "cli"`) and ends every session of the account, because
sessions opened before the flag are not break-glass logins:

1. Make sure the account has a local password and an enrolled second factor
   (TOTP or WebAuthn), and that `PERFIN_BREAKGLASS_CIDRS` is correct (9a).
   Once flagged, the account is refused from anywhere else.
2. Test a normal login through the tunnel (9b) while local login is still on.
3. Set the flag, replacing `admin` with the bootstrap username:

   ```sh
   docker compose exec api perfin breakglass enable admin
   ```

   It prints how many sessions it ended and warns if the account has no
   second factor or is not an instance admin. `perfin breakglass disable
   admin` clears the flag the same way.
4. Set `PERFIN_BREAKGLASS_ADMIN_USERNAME=admin`, restart the API, and check
   the account from the database's point of view (`perfin config check`
   cannot see the database):

   ```sh
   docker compose exec api perfin breakglass status
   ```

   It prints a JSON report and exits `1` unless the named account exists, is
   active, flagged, an instance admin, and has a local password and a second
   factor (`"problems": []`).
5. Log in through the tunnel again to confirm.

Alternatively, an instance admin can create a separate local account
(`POST /api/v1/admin/users`), make it a household owner and flag it with
`PATCH /api/v1/admin/users/{id}` `{"is_breakglass": true}`. That account
keeps the household reachable during an IdP outage, but it is **not** an
instance admin.

### 9d. Switching to OIDC-only

1. Every day-to-day user signs in through Authentik successfully (step 10).
   Link any existing local accounts **first**: linking needs a local login,
   and that is impossible once local login is off.
2. Set:

   ```sh
   PERFIN_LOCAL_LOGIN_ENABLED=false
   PERFIN_BREAKGLASS_CIDRS=<from 9a>
   PERFIN_BREAKGLASS_ADMIN_USERNAME=admin
   ```

3. `docker compose exec api perfin config check`, then
   `docker compose up -d api worker`.
4. Test both paths: an Authentik login from anywhere, and a break-glass login
   through the tunnel.

## 10. Users: just-in-time provisioning, pre-created users and linking

PerFin finds the account for an OIDC login by `(issuer, sub)` alone. The
email claim never selects an account (B3). If there is no identity for that
`(issuer, sub)` yet, the outcome depends on these settings:

| Situation | Result |
|---|---|
| Any PerFin account already has the token's email, and linking is not possible (see below) | Refused: `account-exists`. PerFin never creates a second account with the same email |
| `PERFIN_OIDC_LINK_BY_VERIFIED_EMAIL=true`, token says `email_verified: true`, and exactly one active, non-break-glass account has that email **marked verified locally** | Link offer (see below) |
| No account has the email, `PERFIN_OIDC_JIT_PROVISIONING=true` | A new account is created |
| No account has the email, JIT off | Refused: `no-account` |
| User in no mapped group, `PERFIN_OIDC_REQUIRE_GROUP=true` | Refused first: `not-in-group` |

**JIT provisioning** (`PERFIN_OIDC_JIT_PROVISIONING=true`): the first
Authentik login creates the account. The username comes from
`preferred_username` (lower-cased; characters other than `a-z 0-9 . _ -`
replaced; a random suffix if taken), the display name from `name`, and the
email and its verified flag from the token. The account has no local password
and is never an instance admin. It joins the OIDC household with the role
from its groups. Keep `PERFIN_OIDC_REQUIRE_GROUP=true` with JIT, so that the
Authentik group, not mere existence in Authentik, decides who gets an
account. `perfin config check` warns otherwise. This is the recommended way
to onboard household members.

**Pre-created users.** PerFin attaches an Authentik identity to an account
created in advance only by linking by verified email (below). Local accounts
created through `POST /api/v1/admin/users`, as a new household member or at
bootstrap start with `email_verified = false`; PerFin sends no verification
mail. An instance admin vouches for the address with
`PATCH /api/v1/admin/users/{id}` `{"email_verified": true}` (step-up,
audited as `user.email_verified_set`; `409 no-email` without an address, and
not for their own account). Until then the account cannot be linked, and its
email blocks JIT for that person (`account-exists`).

**Linking by verified email** (`PERFIN_OIDC_LINK_BY_VERIFIED_EMAIL=true`):

1. An instance admin has marked the local account's email verified (above).
2. The user signs in through Authentik. PerFin logs **nothing** in. It sets a
   5-minute sealed offer cookie and redirects to `/login/link-oidc`.
3. Within 5 minutes, the user logs in **locally** to the matching account,
   with password and, if the account has one, its PerFin second factor.
4. The user confirms (`POST /api/v1/auth/oidc/link`). From then on the
   account signs in through Authentik.

Authentik's built-in email mapping has historically reported
`email_verified: true` for every address, whether or not Authentik ever
verified it. That is why PerFin never links on the IdP's word alone: the
local account must have a verified email, and the person must prove they can
log in to it. Linking needs local login, so do it before switching to
OIDC-only mode.

**Deactivating.** Deactivating a user in Authentik stops new logins at once.
Existing PerFin sessions end through back-channel logout or claim refresh
(step 8), or at their idle or absolute timeout. To cut access in PerFin
immediately, an instance admin deactivates the account in PerFin, which
revokes every session and token.

## 11. Authentik on a private LAN address

By default, PerFin refuses to contact an issuer whose host name resolves to a
non-public address (SECURITY.md B5). For an Authentik on the home LAN, set:

```sh
PERFIN_OIDC_ALLOW_PRIVATE_ISSUER=true
```

What it allows (`app/core/http.py`, `app/auth/oidc/client.py`):

- **Only the issuer's host and port** (the egress allowlist is that host
  alone) may resolve to non-global addresses: RFC 1918 (`10/8`,
  `172.16/12`, `192.168/16`), CGNAT (`100.64/10`), IPv6 ULA (`fc00::/7`), and
  other non-global unicast ranges.
- In dev mode only (`PERFIN_DEV_MODE=true`), it additionally allows loopback
  (`127.0.0.0/8`, `::1`) for a local test IdP.

What it still blocks:

- link-local addresses, always: `169.254.0.0/16` (including the cloud
  metadata address `169.254.169.254`) and `fe80::/10`;
- multicast, unspecified (`0.0.0.0`, `::`), broadcast and reserved addresses,
  and Teredo. IPv4-mapped, IPv4-compatible, NAT64 and 6to4 IPv6 forms are
  judged by the IPv4 address inside;
- loopback outside dev mode;
- a host whose DNS answer contains **any** forbidden address, even next to an
  allowed one. Each request resolves the name, checks every address and
  connects to the checked one (no DNS rebinding);
- every host other than the issuer's, including discovery endpoints elsewhere
  and redirects to another origin;
- plain `http` (`perfin config check` requires an `https://` issuer);
- certificates that do not verify. Authentik needs a certificate that the API
  container trusts for `auth.example.com`. A publicly trusted certificate
  (for example from Let's Encrypt with a DNS-01 challenge) works even for a
  LAN-only host name. For a certificate from your own private CA, see
  "Trusting a private CA" below.

`perfin config check` logs the warning `oidc.private_issuer_allowed` at
every start while the setting is on.

**Trusting a private CA.** PerFin verifies the issuer's certificate against
OpenSSL's default trust store (`ssl.create_default_context()` in
`app/core/http.py`), which honours `SSL_CERT_FILE`. The variable *replaces*
the default CA file, so give it a bundle of the public CAs plus yours, and
mount it read-only into the `api` container (the one that talks to the IdP):

```sh
cd deploy
docker compose exec api cat /etc/ssl/certs/ca-certificates.crt > perfin-ca-bundle.pem
cat /path/to/your-private-ca.pem >> perfin-ca-bundle.pem
```

```yaml
# deploy/docker-compose.override.yml
services:
  api:
    volumes:
      - ./perfin-ca-bundle.pem:/etc/perfin/ca-bundle.pem:ro
    environment:
      SSL_CERT_FILE: /etc/perfin/ca-bundle.pem
```

The certificate must still name `auth.example.com` (host name checks are
unchanged), and TLS runs end to end through the egress proxy, so this applies
with `HTTPS_PROXY` too. `tests/security/test_egress_private_ca.py` covers
this with a synthetic CA: refused without the bundle, accepted with it, and
refused for a host name the certificate does not carry. Re-create the bundle
after image updates so the public CAs stay current.

**In the Compose deployment**, the API reaches the IdP only through the
`egress` proxy (step 6). When a proxy applies, the address check is the
proxy's, not the API's: PerFin sends the host name in the CONNECT request,
and the proxy resolves it and refuses private addresses unless they fall
inside `EGRESS_ALLOW_PRIVATE_CIDRS`. So for a LAN Authentik at `192.168.1.10`:

```sh
# deploy/egress-allowlist.txt
auth.example.com:443

# deploy/.env
HTTPS_PROXY=http://egress:3128
NO_PROXY=localhost,127.0.0.1,db,api,clamav
EGRESS_ALLOW_PRIVATE_CIDRS=192.168.1.10/32
PERFIN_OIDC_ALLOW_PRIVATE_ISSUER=true
```

Behind the proxy, `PERFIN_OIDC_ALLOW_PRIVATE_ISSUER` has no effect on the
address check. Set it anyway, so the configuration states its intent and the
check still applies if the API is ever run without the proxy. Keep
`EGRESS_ALLOW_PRIVATE_CIDRS` as narrow as one address: the proxy exempts
every address inside it, for every allowlisted host. The egress container
must be able to resolve `auth.example.com` (Docker's DNS forwards to the
host's resolvers). A LAN issuer needs its `deploy/egress-allowlist.txt` entry
too, as the comment in that file says.

## 12. Optional: Authentik forward-auth as an outer gate

An Authentik **proxy provider** in forward-auth mode can stand in front of
PerFin, so nobody without an Authentik session reaches PerFin's login page,
API or static files. It is an additional layer only. PerFin still runs its
own OIDC login, sessions, CSRF and RLS, and it **never** reads the
`X-Authentik-*` identity headers. Do not copy them to the upstream, and do not
build anything that trusts them.

In Authentik:

1. **Applications > Providers > Create > Proxy Provider**, mode **Forward
   auth (single application)**, **External host**
   `https://perfin.example.com`, the same authorization flow as in step 4.
2. **Applications > Applications > Create** a second application, for
   example `PerFin gate` (slug `perfin-gate`), with that provider, and bind the
   same three groups.
3. **Applications > Outposts**, then edit **authentik Embedded Outpost** and
   add `PerFin gate` to its applications.

In the Caddyfile's `perfin.example.com` block, before the `handle` blocks
(an example to adapt; PerFin's CI does not exercise it):

```caddyfile
	# Authentik forward-auth gate (outer layer only).
	@gated not path /outpost.goauthentik.io/* /api/v1/auth/oidc/backchannel-logout
	forward_auth @gated https://auth.example.com {
		uri /outpost.goauthentik.io/auth/caddy
		header_up Host {upstream_hostport}
	}
	handle /outpost.goauthentik.io/* {
		reverse_proxy https://auth.example.com {
			header_up Host {upstream_hostport}
		}
	}
```

Trade-offs:

- `/api/v1/auth/oidc/backchannel-logout` must stay outside the gate. Authentik
  calls it server to server, without the gate's cookie.
- Clients using PerFin personal access tokens (`Authorization: Bearer
  pfn_...`) and a network MCP endpoint (`/mcp`) cannot pass a browser
  forward-auth flow. Either do not use the gate, or give those clients a
  separate path (for example over a VPN). Exempting requests that merely carry
  an `Authorization` header would let anyone skip the gate by adding one.
- The gate's own session and PerFin's session expire independently. A user
  may be sent through Authentik twice (once for the gate, once for PerFin),
  silently if their Authentik session is still valid.

## 13. Troubleshooting

When the OIDC callback fails, the browser lands on
`/login?oidc_error=<code>`. The precise reason is in the `user.login_failed`
audit row (`after.reason`). Until PerFin has an audit viewer for
instance-level rows, read them with:

```sh
docker compose exec db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U postgres perfin -c "SELECT created_at, ip, after FROM audit_log WHERE action = '"'"'user.login_failed'"'"' ORDER BY seq DESC LIMIT 10"'
```

Discovery problems also appear in the API log as `OIDC discovery failed`
with the reason.

| `oidc_error` / reason | Likely cause | Fix |
|---|---|---|
| `idp-unavailable`, log: `discovery issuer does not match PERFIN_OIDC_ISSUER exactly` | **Issuer mismatch.** Usually the trailing slash: Authentik's issuer is `https://auth.example.com/application/o/perfin/` with the `/`, and PerFin compares byte for byte. PerFin builds the discovery URL the same way with or without the slash, so `curl` works while the comparison fails. Other causes: an `http://` issuer because the proxy in front of Authentik does not send `X-Forwarded-Proto: https`; a different host name, because Authentik builds the issuer from the request's `Host` | Copy **OpenID Configuration Issuer** from the provider page exactly. Fix the forwarded headers on Authentik's proxy. Reach Authentik by the same name the browser uses |
| `idp-unavailable`, log: `... is not on the issuer's host` | Authentik reached by an internal name (for example `http://authentik-server:9000`) | Use the public or LAN host name everywhere |
| `idp-unavailable`, log: `provider signs ID tokens with no allowed algorithm` | No **Signing Key** on the provider (HS256) | Step 3 |
| `idp-unavailable`, log: `provider does not support private_key_jwt` | `PERFIN_OIDC_TOKEN_AUTH_METHOD=private_key_jwt` | Use `client_secret_basic` with Authentik |
| `idp-unavailable`, log mentions egress, `private`, `loopback` or a timeout | The API cannot reach Authentik: no `egress` profile or `HTTPS_PROXY`, host missing from `egress-allowlist.txt`, private address not in `EGRESS_ALLOW_PRIVATE_CIDRS`, certificate not trusted | Steps 6 and 11. The egress proxy logs one JSON line per decision |
| `token-invalid`, reason `expired`, `iat-in-future`, `iat-too-old` or `not-yet-valid` | **Clock skew** beyond 60 s between PerFin and Authentik (ID tokens older than 10 minutes are also refused) | Run NTP (`chrony`/`systemd-timesyncd`) on both hosts; compare `date -u` on each |
| `token-invalid`, reason `unknown-kid` | **Signing key rotated** (a new certificate selected on the provider), and PerFin fetched the JWKS less than `PERFIN_OIDC_JWKS_MIN_REFRESH_SECONDS` (300 s) ago, so it will not refetch yet. This is the deliberate B9 rate limit | Wait 5 minutes and retry, or `docker compose restart api` to drop the cached JWKS. Authentik publishes only the current key, so tokens signed with the old key fail after the switch; rotate when few logins are in flight |
| `token-invalid`, reason `jwks-unavailable` | JWKS fetch failed | As for `idp-unavailable` |
| `token-invalid`, reason `alg-not-allowed` or `malformed` | HS256 signing, or an **Encryption Key** set (JWE) | Steps 3 and 4 |
| `token-invalid`, reason `invalid-iss` / `invalid-aud` / `invalid-azp` | Issuer or client id differs from Authentik's | Re-copy `PERFIN_OIDC_ISSUER` and `PERFIN_OIDC_CLIENT_ID` |
| `not-in-group` | **Group claim missing or not matching.** Causes: `PerFin: groups` not selected in the provider's **Scopes** (Authentik silently drops scopes it has no mapping for); **Include claims in id_token** off (PerFin does not call userinfo); the user is not in a group; names differ from `PERFIN_OIDC_GROUP_*` (case-sensitive); the step 2 prefix filter drops your custom group names | Check the provider's **Preview** tab for the user; fix the scope, the toggle, the membership or the names |
| `account-exists` | A PerFin account already has this email, and no link was offered | Have an instance admin mark the local email verified, then link (step 10); or remove the duplicate account |
| `no-account` | No identity, JIT off | Turn on `PERFIN_OIDC_JIT_PROVISIONING` or link the account |
| `account-disabled` | Account inactive in PerFin | Reactivate in PerFin |
| `login-failed`, reason `breakglass-account` | A break-glass account tried OIDC | Use the tunnel (step 9b) |
| `state-invalid` | The 5-minute pre-auth cookie expired while the user was on Authentik's page; the browser reached PerFin under a host name other than `PERFIN_PUBLIC_URL`'s (cookie set for another host); the callback was replayed | Retry from PerFin's login page; always use the `PERFIN_PUBLIC_URL` host |
| `idp-error`, reason `idp:access_denied` | The user declined consent, or Authentik refused | Check the application's bindings |
| `idp-error`, reason `token-exchange-failed` | Wrong client secret, the client type is **Public**, or the code expired (**Access code validity**) | Re-copy the secret; set **Confidential** |
| Authentik shows "Redirect URI error" | Redirect URI not registered exactly, or `PERFIN_PUBLIC_URL` differs from it | Step 4, **Redirect URIs** |
| `rate-limited` | More than 30 callbacks in 5 minutes from one IP | Wait; check for a redirect loop |
| Session is `mfa_pending` despite Authentik MFA | `PERFIN_OIDC_TRUST_IDP_MFA` is off, or `amr` lacks MFA (passwordless WebAuthn; Authentik session started without MFA; validation threshold skipped the stage) | Step 7 |
| Users are logged out at every claim refresh | The refreshed ID token has no `groups` (**Include claims in id_token** off), or the refresh token expired (**Refresh Token validity** shorter than PerFin sessions) | Step 8 |
| Changes in Authentik not picked up | Discovery and JWKS are cached for up to `PERFIN_OIDC_METADATA_MAX_AGE_SECONDS` (3600 s) | Wait, or `docker compose restart api` |

## Settings reference

| Variable | Default | Section |
|---|---|---|
| `PERFIN_OIDC_ISSUER` | empty (OIDC off) | 5, 6 |
| `PERFIN_OIDC_CLIENT_ID`, `PERFIN_OIDC_CLIENT_SECRET` | empty | 5, 6 |
| `PERFIN_OIDC_SCOPES` | `openid profile email groups` | 2, 8 |
| `PERFIN_OIDC_TOKEN_AUTH_METHOD` | `client_secret_basic` | 6 |
| `PERFIN_OIDC_PRIVATE_KEY_FILE`, `PERFIN_OIDC_PRIVATE_KEY_ID` | unset (not used with Authentik) | 6 |
| `PERFIN_OIDC_GROUP_ADMIN` / `_MEMBER` / `_VIEWER` | `perfin-admins` / `perfin-users` / `perfin-viewers` | 1 |
| `PERFIN_OIDC_REQUIRE_GROUP` | `true` | 1, 10 |
| `PERFIN_OIDC_HOUSEHOLD_ID` | unset (the only household) | 1, 6 |
| `PERFIN_OIDC_JIT_PROVISIONING` | `false` | 10 |
| `PERFIN_OIDC_LINK_BY_VERIFIED_EMAIL` | `false` | 10 |
| `PERFIN_OIDC_TRUST_IDP_MFA` | `false` | 7 |
| `PERFIN_OIDC_POST_LOGOUT_REDIRECT_URIS` | `<PERFIN_PUBLIC_URL>/` | 8 |
| `PERFIN_OIDC_CLAIM_REFRESH_MINUTES` | `0` (off) | 8 |
| `PERFIN_OIDC_ALLOW_PRIVATE_ISSUER` | `false` | 11 |
| `PERFIN_OIDC_JWKS_MIN_REFRESH_SECONDS` | `300` | 13 |
| `PERFIN_OIDC_METADATA_MAX_AGE_SECONDS` | `3600` | 13 |
| `PERFIN_OIDC_HTTP_TIMEOUT_SECONDS` | `10` | 13 |
| `PERFIN_LOCAL_LOGIN_ENABLED` | `true` | 9 |
| `PERFIN_BREAKGLASS_CIDRS` | `127.0.0.1/32` (see 9a) | 9 |
| `PERFIN_BREAKGLASS_ADMIN_USERNAME` | empty | 9 |
| `PERFIN_REQUIRE_MFA` | `true` | 7 |
| `PERFIN_PUBLIC_URL` | (required) | 4, 9b |
| `PERFIN_TRUSTED_PROXIES` | `172.30.80.10/32` in Compose | 9a |

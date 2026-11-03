# Authentication and authorisation

Two login methods are first-class: a local username/password account and an
OpenID Connect identity from Authentik (or any compliant provider). An instance
can run with both, or be locked to OIDC only.

## Local accounts

### Passwords
- Hashed with **Argon2id** (memory 64 MiB, iterations 3, parallelism 4 as the
  baseline; parameters stored alongside the hash so they can be raised later
  and rehashed on next successful login).
- Minimum length 12, maximum 128, no composition rules. Checked against a
  local copy of a breached-password list (k-anonymity range check against
  HIBP is opt-in because it is an off-host call).
- Constant-time comparison; a login attempt against an unknown username runs
  a dummy hash so timing does not reveal account existence.
- Implementation notes (`app/auth/local.py`, `app/auth/breached.py`,
  `app/api/routers/auth_local.py`):
  - The breached-password list is bundled
    (`app/auth/data/breached-passwords.txt.gz`: the entries of 12 to 128
    characters from the SecLists NCSC top 100k and the xato.net and pwdb top
    1M lists, about 73,000; provenance, licence and rebuild command in
    `app/auth/data/README.md`). The check is case-insensitive. A new password
    must also differ from the username (and, on a change, from the current
    password). `PERFIN_HIBP_CHECK_ENABLED=true` adds the HIBP range query
    (only the first 5 hex characters of the SHA-1 leave the host,
    `Add-Padding: true`, no redirects, 1 MiB cap, 5 s timeout; an unreachable
    HIBP does not block the user, the local list still applies). Rejections
    are `422 weak-password` with a stable `reason` (`too-short`, `too-long`,
    `breached`, `same-as-username`, `same-as-current`).
  - `POST /auth/login` answers every refusal (unknown user, wrong password,
    locked or delayed account, inactive account, break-glass account outside
    its CIDR, non-break-glass account in OIDC-only mode) with the same `401
    invalid-credentials` body and header set, after exactly one full-cost
    Argon2id verification (the dummy hash when there is no password to
    check). Only the audit row (`user.login_failed`) records the reason. A
    successful login upgrades a hash made with older parameters, revokes the
    session the browser presented (if any) and creates a fresh one: an
    `mfa_pending` session when the user has a TOTP or WebAuthn factor
    (`status: "mfa_required"`, completed by `POST /auth/mfa/verify`), else a
    full one (`status: "authenticated"`). The response carries the CSRF token
    bound to the new session.
  - `POST /auth/password` (change) needs a browser session, the current
    password and, when the user has a second factor, a step-up within the
    last 10 minutes; a user without any factor (only possible with
    `PERFIN_REQUIRE_MFA=false`) re-authenticates with the current password
    alone. The session id rotates, every other session is revoked, and the
    response carries the new CSRF token. Wrong current passwords are rate
    limited per account (5 per 15 minutes) and audited.

### Multi-factor
- **WebAuthn / passkeys** are the recommended second factor (and can be the
  only factor for passwordless login later). Resident keys supported.
- **TOTP** (RFC 6238) supported with encrypted secret storage and replay
  protection (a code is accepted once).
- **Recovery codes**: ten single-use codes, hashed at rest, shown once.
- MFA can be required instance-wide by the admin.
- Step-up: sensitive actions (change password, add MFA method, create API
  token, change OIDC settings, export everything, purge) require a fresh MFA
  verification within the last 10 minutes.
- Implementation notes (`app/auth/mfa/`, `app/api/routers/auth_mfa.py`):
  - WebAuthn: RP id = host of `PERFIN_PUBLIC_URL`, the only accepted origin
    is that URL's origin. Attestation `none`, user verification and resident
    keys *preferred*. Challenges (32 random bytes) live server-side in
    `mfa_challenge`, bound to the session and user, expire after 5 minutes
    and are spent before the response is checked, so each is usable once even
    when verification fails. The stored sign counter must increase whenever
    the authenticator reports one.
  - TOTP: 160-bit secret generated server-side and shown once with an
    `otpauth://` URI; it is parked encrypted on the session until the first
    code confirms it (15 minutes), then stored encrypted bound to its
    `mfa_method` row. 6 digits, 30 s, ±1 step; the accepted step is stored
    and only later steps are accepted, so the confirming code cannot be
    reused either. One authenticator app per user.
  - Recovery codes: 16 Crockford base32 symbols (80 bits), Argon2id at
    m=19 MiB, t=2, p=1 (the entropy makes the lighter cost safe and keeps
    checking ten candidates fast). Issued with the first factor, regenerated
    behind step-up (all earlier codes void), dropped with the last factor.
  - Every verification counts against `mfa:session` (5 per 5 min) and
    `mfa:account` (10 per 15 min), recovery codes also `mfa:recovery`
    (5 per hour); failures feed the account's progressive lockout and are
    audited; a locked account gets `429`. The counter is reset only by a
    verified second factor: a correct password for an account with a factor
    leaves it as it is (the session is only `mfa_pending`), so password
    logins interleaved with code guesses never undo the lockout. A used recovery code is audited and
    emits a `security.recovery_code_used` event into each of the user's
    households (delivery through notification channels: Phase 7).
  - Adding a factor needs step-up once the user has one; confirming a new
    factor proves possession, so the session rotates and counts as freshly
    verified. Removing the last factor is refused while
    `PERFIN_REQUIRE_MFA` is on.
  - Login flows call `app.auth.mfa.policy.login_stage` to decide between an
    `mfa_pending` and an `authenticated` session; the OIDC callback calls
    `mark_mfa_from_idp(db, session, amr, auth_time=..., step_up_from=..., settings=...)`,
    which accepts `amr` containing `mfa` or factors of two kinds (knowledge,
    possession, inherence) only when `PERFIN_OIDC_TRUST_IDP_MFA=true`.
  - Trusted IdP MFA satisfies MFA for the login (and `PERFIN_REQUIRE_MFA`),
    but it is a **step-up** only for a forced sign-in:
    `GET /auth/oidc/start?reauth=true` sends `prompt=login&max_age=0` so the
    IdP authenticates again, and the sealed pre-auth cookie records that the
    login was a reauth and when it started. Only when such a login's ID
    token `auth_time` is at or after that start (minus the 60 s clock skew)
    is `mfa_verified_at` set to `min(now, auth_time)`, a step-up for 10
    minutes after the IdP's authentication. Every other trusted-MFA login
    (a plain login, a silent SSO re-login, a reauth the IdP answered from
    its old session, an ID token without a usable `auth_time`) gets
    `mfa_verified_at = min(auth_time, now - 10 min - 1 µs)`: MFA for the
    login, never a step-up. PerFin therefore never relies on the IdP dating
    `auth_time` correctly on an SSO answer (reviews P1-SR2-01, P1-SR3-01);
    it does rely on the IdP really re-running its MFA for `prompt=login`
    (an RP cannot verify that; see AUTHENTIK.md 7c). A user with a PerFin
    factor can always step up with it (`POST /auth/step-up`).

### Sessions
- Server-side sessions. The cookie carries only a random 256-bit session id;
  the database stores its SHA-256. No JWTs for browser sessions (ADR-0005).
- Cookie flags: `HttpOnly; Secure; SameSite=Lax; Path=/`, with the `__Host-`
  prefix so it is bound to the origin and cannot be set by subdomains.
- Idle timeout 30 minutes (configurable), absolute lifetime 12 hours
  (configurable), "remember this device" extends absolute lifetime to 30 days
  but still requires MFA re-verification for step-up actions.
- Session id rotates on login, on privilege change and on MFA completion
  (prevents fixation).
- Users can list and revoke their sessions. Password change revokes all other
  sessions.
- Implementation notes (`app/auth/sessions.py`): a remembered device is not
  logged out for being idle (only its 30-day absolute lifetime applies); a
  password-accepted session waiting for the second factor (`mfa_pending`)
  lives at most 10 minutes and authorises nothing but MFA verification. In
  dev mode with `PERFIN_SECURE_COOKIES=false` (plain http) the cookie is
  `perfin-session-dev` without `Secure`, because the `__Host-` prefix
  requires `Secure`.
- With `PERFIN_REQUIRE_MFA` a user without a second factor (and whose IdP did
  not assert MFA) gets a restricted session that can only enrol MFA.

### CSRF
Double-submit token bound to the session, sent in an `X-CSRF-Token` header for
every state-changing request. `SameSite=Lax` is defence in depth, not the
control. The `Origin`/`Sec-Fetch-Site` headers are additionally checked.

### Brute force and enumeration
- Per-account and per-IP sliding-window rate limits on login, MFA, password
  reset and OIDC callback.
- Progressive delay then temporary lock after N failures; lock notifies the
  user via configured channel.
- Identical responses for "no such user" and "wrong password".
- Password reset is only available if an SMTP channel is configured; tokens
  are single-use, 15-minute, hashed at rest, and the response never confirms
  whether the email exists.
- Implementation notes: login limits are 30 per 5 minutes per client IP and
  10 per 15 minutes per account (unknown usernames are keyed and limited
  exactly like real accounts); a wrong password counts towards the lockout
  (from the 3rd consecutive failure a delay that doubles per failure, from
  the 10th a 15-minute lock; during either the login is refused like a wrong
  password). When the lock starts the API writes a `user.locked` audit row,
  emits a `user.locked` event into each of the user's households and calls
  the hooks registered with `app.auth.local.register_lock_notifier`
  (notification channels deliver it from Phase 7). Every login success and
  failure, logout, password change and reset is audited at instance level
  without any password, hash or token.
- Password reset (`app/auth/reset.py`): SMTP is `PERFIN_SMTP_HOST`,
  `PERFIN_SMTP_PORT`, `PERFIN_SMTP_TLS` (`starttls`, which must be offered
  and is required before anything is sent, or `implicit`),
  `PERFIN_SMTP_USERNAME`, `PERFIN_SMTP_PASSWORD`, `PERFIN_SMTP_FROM`;
  certificates are verified. Reset is offered when host and from address are
  set and local login is enabled. `POST /auth/password-reset/request`
  (`identifier`: username or email) always answers `202`; the lookup, the
  token and the mail happen after the response is sent. Break-glass,
  inactive and email-less accounts are never sent a reset. The mail links to
  `<PERFIN_PUBLIC_URL>/reset-password#token=...` (the token is in the
  fragment, so it never reaches a server log or a `Referer`); the page posts
  it with the new password to `POST /auth/password-reset/confirm`. A new
  request voids earlier tokens; a confirmed reset clears the lockout and
  revokes every session. All responses carry `Referrer-Policy: no-referrer`.

## OIDC (Authentik)

### Flow
Authorization Code with **PKCE (S256)**. No implicit, no hybrid, no resource
owner password flow. Discovery via `.well-known/openid-configuration`; the
issuer URL is admin-configured and the document's `issuer` must match it
exactly.

1. User clicks "Sign in with Authentik". API generates `state`, `nonce` and
   PKCE verifier, stores them in a short-lived pre-auth session cookie (5 min),
   redirects to the authorization endpoint with `scope=openid profile email
   groups`.
2. Authentik redirects back to `/api/v1/auth/oidc/callback` with `code` and
   `state`.
3. API verifies `state`, exchanges the code (back-channel, client secret or
   private_key_jwt), validates the ID token: signature against cached JWKS
   (with key rotation handling), `iss`, `aud`, `exp`, `iat` skew, `nonce`,
   and `at_hash` if present.
4. User lookup by `(issuer, sub)`. If none:
   - If a local user exists with the same **verified** email and the admin has
     enabled "link by verified email", prompt the user to confirm linking by
     authenticating locally (prevents account takeover through an IdP that
     allows email changes).
   - Else, if just-in-time provisioning is enabled, create the user; else
     reject with a friendly message.
5. Map claims: `groups` -> household role via admin-configured mapping
   (e.g. `perfin-admins` -> admin, `perfin-users` -> member, `perfin-viewers`
   -> viewer). A user in no mapped group is denied if "require group" is on.
6. Create a normal server-side session. The ID/access tokens are **not** kept
   beyond the request except the refresh token if claim refresh is enabled
   (stored encrypted; back-channel logout needs no refresh token, it matches
   sessions by `sid`/`sub`) and the ID token as the logout `id_token_hint`
   (stored encrypted, only when the IdP has an `end_session_endpoint`).

### Logout
RP-initiated logout to Authentik's `end_session_endpoint` with
`id_token_hint` and `post_logout_redirect_uri` from an allowlist. Back-channel
logout (`logout_token`) supported so a session killed in Authentik dies in
PerFin too.

### Hardening specifics
- Redirect URI is fixed in configuration; never taken from the request.
- The post-login "return to" URL is validated as a same-origin relative path
  (no open redirect): a single leading `/`, no scheme or authority, no
  backslash or control character, and no `.` or `..` path segment, plain or
  percent-encoded (`/a/..//host` would normalise to `//host`). The server
  (`safe_return_to`) and the SPA (`safeReturnTo`, which also re-checks its
  normalised result) apply the same rule (review P1-SR2-02).
- Discovery and JWKS fetches go through an egress allowlist containing only
  the issuer host; responses are cached with a maximum lifetime; the HTTP
  client refuses redirects to other hosts and private address ranges (SSRF).
- Clock skew tolerance 60 s.
- OIDC-only mode disables the local password form and the local password
  reset, but keeps a **break-glass local admin** that is only usable from an
  allowlisted CIDR (default: loopback) so an IdP outage does not lock the
  owner out. Implementation: `app_user.is_breakglass` marks the account (set
  from the local database only). With `PERFIN_LOCAL_LOGIN_ENABLED=false`,
  `POST /auth/login` from outside `PERFIN_BREAKGLASS_CIDRS` is `403
  local-login-disabled` for every username; from inside, only break-glass
  accounts can succeed. A break-glass account is also refused outside its
  CIDR when local login is enabled. The client address is the
  trusted-proxy-aware one (`PERFIN_TRUSTED_PROXIES`). The CIDR is enforced
  at login and again on every request (`app.auth.deps.session_acceptor`): a
  break-glass session (`login_method = breakglass`) presented from outside
  `PERFIN_BREAKGLASS_CIDRS` counts as no session (`401`), is not touched
  (its idle expiry does not slide) and cannot finish a pending MFA step.
  A personal access token carries no such network check, so a break-glass
  account has none: `POST /auth/tokens` is `403 breakglass-account` for
  it, a bearer token of a break-glass account is `401 invalid-token` from
  every address, and setting the flag revokes the account's tokens.
  Setting or clearing the flag ends every session of the account (sessions
  opened before the flag are not break-glass logins). The flag is set by an
  instance admin (`PATCH /admin/users/{id}`, step-up) or, for the bootstrap
  admin who cannot change their own account, by the operator with `perfin
  breakglass enable|disable <username>` (audited `user.breakglass_set`,
  `via: cli`); `perfin breakglass status` checks the account named by
  `PERFIN_BREAKGLASS_ADMIN_USERNAME` against the database (exists, active,
  flagged, instance admin, local password, second factor).
- Authentik-specific docs will show the provider setup, the group mapping, and
  how to enforce MFA at the IdP so PerFin can treat `amr` claims as satisfied
  MFA (`PERFIN_OIDC_TRUST_IDP_MFA=true`; see "Multi-factor").

### Implementation notes (OIDC)

Code: `app/auth/oidc/` (`client`, `discovery`, `tokens`, `provisioning`,
`flow`, `logout`, `refresh`, `models`), `app/core/http.py`,
`app/api/routers/auth_oidc.py`, `app/workers/handlers/oidc_maintenance.py`.

- **Settings.** `PERFIN_OIDC_ISSUER`, `_CLIENT_ID`, `_CLIENT_SECRET`,
  `_SCOPES`, `_TOKEN_AUTH_METHOD` (`client_secret_basic`, default, or
  `private_key_jwt` with `_PRIVATE_KEY_FILE`, RSA >= 2048 or EC P-256, and
  optional `_PRIVATE_KEY_ID`), `_JIT_PROVISIONING`, `_GROUP_ADMIN` /
  `_GROUP_MEMBER` / `_GROUP_VIEWER`, `_REQUIRE_GROUP`, `_HOUSEHOLD_ID`,
  `_LINK_BY_VERIFIED_EMAIL`, `_TRUST_IDP_MFA`, `_POST_LOGOUT_REDIRECT_URIS`,
  `_CLAIM_REFRESH_MINUTES`, `_ALLOW_PRIVATE_ISSUER`,
  `_JWKS_MIN_REFRESH_SECONDS` (300), `_METADATA_MAX_AGE_SECONDS` (3600),
  `_HTTP_TIMEOUT_SECONDS` (10). The redirect URI is always
  `<PERFIN_PUBLIC_URL>/api/v1/auth/oidc/callback`.
- **Pre-auth cookie.** `__Host-perfin-oidc` (`HttpOnly; Secure;
  SameSite=Lax; Path=/`, `Max-Age=300`; `perfin-oidc-dev` without `Secure`
  only with insecure dev cookies) holds `state` (256 bits), `nonce` (256
  bits), the PKCE verifier (64 random bytes) and the validated `return_to`,
  sealed with XChaCha20-Poly1305 under a key derived from
  `PERFIN_SECRET_KEY`, with the cookie's purpose as associated data and the
  expiry inside. The callback compares `state` in constant time, then
  records its SHA-256 in `oidc_used_state` in its own transaction before
  exchanging the code, so a state (with its cookie) works once even if the
  login then fails. The cookie is cleared on every callback.
- **Discovery and egress.** The discovery document's `issuer` must equal
  `PERFIN_OIDC_ISSUER` byte for byte. Every endpoint in it
  (`authorization_endpoint`, `token_endpoint`, `jwks_uri`,
  `end_session_endpoint`, `userinfo_endpoint`) must be on the issuer's
  scheme, host and port: the egress allowlist is the issuer host only.
  Authentik serves all of them from one host; providers that spread
  endpoints over several hosts are not supported. It must allow `code`,
  `S256` and `RS256`/`ES256` when it lists those capabilities.
  `app.core.http.SafeHttpClient` resolves DNS itself, refuses the host if
  any answer is link-local (including `169.254.169.254` and `fe80::/10`),
  multicast, unspecified, reserved or broadcast (always), loopback (unless
  dev mode **and** `PERFIN_OIDC_ALLOW_PRIVATE_ISSUER`) or otherwise
  non-global (RFC 1918, ULA, CGNAT: unless `PERFIN_OIDC_ALLOW_PRIVATE_ISSUER`,
  and then only for the issuer host); IPv4-mapped, IPv4-compatible, NAT64
  and 6to4 forms are judged by the IPv4 address inside, Teredo is refused.
  It then connects to the vetted address (URL rewritten to the IP, `Host`
  and TLS SNI/certificate check on the hostname), so there is no second
  lookup to rebind. `https` only (`http` in dev mode), no credentials in
  URLs, redirects only to the same origin (at most 3, never on `POST`),
  bodies capped at 1 MiB, compressed bodies refused, connect/read/write
  timeouts `PERFIN_OIDC_HTTP_TIMEOUT_SECONDS`. When `HTTPS_PROXY` applies
  (the Compose `egress` profile) the hostname goes to the CONNECT proxy,
  which refuses IP literals, resolves the name itself and applies the same
  address rules with no rebinding window (SECURITY.md H2); the allowlist,
  scheme, redirect, size and time rules still apply in the client.
  `PERFIN_OIDC_ALLOW_PRIVATE_ISSUER=true` is logged as a warning at every
  start (`perfin config check`). The discovery document and JWKS are cached
  in memory for at most `PERFIN_OIDC_METADATA_MAX_AGE_SECONDS`; a token whose
  `kid` is not in the cached JWKS refetches it at most once per
  `PERFIN_OIDC_JWKS_MIN_REFRESH_SECONDS` and is otherwise refused.
- **ID token.** `alg` must be `RS256` or `ES256` (checked on the raw
  header before any key is used, so `none` and `HS256` signed with the public
  key fail); `crit` is refused; embedded keys (`jwk`, `jku`, `x5u`, `x5c`)
  are ignored; the key comes from the JWKS by `kid` and type (RSA >= 2048,
  EC P-256, `use` `sig`, no private parts). Then `iss` equal to the issuer,
  `aud` containing the client id and `azp` equal to it when there are several
  audiences or an `azp`, `exp` (+60 s), `iat` not more than 60 s in the
  future and not older than 10 minutes, `nbf`, non-empty `sub`, `nonce`
  equal to the cookie's (constant time), `at_hash` equal to the left half of
  SHA-256 of the access token when present.
- **Accounts.** `(iss, sub)` in `identity` (provider `oidc`) selects the
  account; inactive and break-glass accounts are refused. Without an
  identity: when any account has the token's email, the login is refused
  (`account-exists`) unless `PERFIN_OIDC_LINK_BY_VERIFIED_EMAIL` is on, the
  token says `email_verified: true` and exactly one active, non-break-glass
  account has that email marked verified locally (local accounts start
  unverified; an instance admin vouches for the address with `PATCH
  /admin/users/{id}` `{"email_verified": true}`, audited); then nothing is logged in,
  a sealed `__Host-perfin-oidc-link` cookie (5 min, `SameSite=Strict`) is set
  and the browser goes to `/login/link-oidc`. The user logs in **locally** to
  that account (`POST /auth/login`, plus `POST /auth/mfa/verify` when it has
  a second factor) and confirms with `POST /auth/oidc/link`, which requires a
  `local` browser session started after the offer (its rotation chain's
  first session), MFA verified after the offer when the account has a
  factor, and the same account and verified email. Otherwise JIT creates the
  user (username from `preferred_username`, made unique; no password; never
  an instance admin) or the login is refused (`no-account`).
- **Roles.** The strongest mapped group gives `admin`, `member` or `viewer`
  in the OIDC household (`PERFIN_OIDC_HOUSEHOLD_ID`, else the instance's
  only household; with several and none configured no membership is
  changed). The membership is created or updated on every login and claim
  refresh, an `owner` membership is never changed, and without a mapped
  group (only with `PERFIN_OIDC_REQUIRE_GROUP=false`) nothing changes.
  `is_instance_admin` is never read from or written by the OIDC code.
- **Session.** `login_method = oidc`; `mfa_pending` (redirect to
  `/login/mfa?return_to=...`) when the user has a local TOTP/WebAuthn factor
  and the IdP's `amr` is not trusted, else full, with `mfa_verified_at`
  from `mark_mfa_from_idp` (see "Multi-factor": only a forced sign-in
  with `reauth=true` and an `auth_time` from that sign-in is a step-up). A session the browser presented is revoked
  (`replaced_by_login`). `oidc_session` keeps `sid`, `sub`, the encrypted ID
  token (hint) and, with claim refresh, the encrypted refresh token; it
  follows session rotation through `app.auth.sessions.register_rotation_hook`.
- **Callback outcome.** Always a `303`: to `return_to`, to the MFA or link
  page, or to `/login?oidc_error=<code>` with `code` one of `rate-limited`,
  `state-invalid`, `idp-error`, `idp-unavailable`, `token-invalid`,
  `not-in-group`, `no-account`, `account-exists`, `account-disabled`,
  `login-failed`. The precise reason is in the `user.login_failed` audit row.
  Callbacks are limited to 30 per 5 minutes per client IP (`oidc:ip`).
- **Logout.** `POST /auth/oidc/logout` (CSRF-protected) revokes the session,
  deletes its `oidc_session` row and returns the end-session URL with
  `id_token_hint`, `client_id` and a `post_logout_redirect_uri` that must be
  exactly one of `PERFIN_OIDC_POST_LOGOUT_REDIRECT_URIS` (default
  `<PERFIN_PUBLIC_URL>/`). `POST /auth/oidc/backchannel-logout` (form field
  `logout_token`, CSRF-exempt, 60 per 5 minutes per IP) validates the token
  like an ID token (no `exp` required) plus the back-channel event, no
  `nonce`, `sid` or `sub`, a `jti` (1-255 characters, required by the
  spec), `typ` `logout+jwt`/`JWT` if present, and revokes every session of
  that issuer matching the `sid` and/or `sub`. Each token works once: the
  SHA-256 of `(iss, jti)` (domain-separated from login `state` hashes) is
  recorded in `oidc_used_state` until the token would be too old anyway
  (`iat` + 10 minutes + skew), and a replay is `400`, so a captured token
  cannot end sessions opened after it. The `jti` is recorded in the same
  transaction as the revocation (`INSERT ... ON CONFLICT DO NOTHING`; a
  concurrent duplicate waits for the first delivery's transaction), so a
  revocation that fails rolls the `jti` back and the IdP's retry still
  works (review P1-SR2-03).
- **Claim refresh.** With `PERFIN_OIDC_CLAIM_REFRESH_MINUTES` > 0 the
  worker job `oidc_claim_refresh` (every 5 minutes) uses the refresh token of
  each due live session: `invalid_grant` revokes the session; a new ID token
  (same `iss`/`sub`) re-applies the group mapping, revoking the session when
  `PERFIN_OIDC_REQUIRE_GROUP` finds no mapped group or the account is
  inactive; a rotated refresh token is stored. The hourly `oidc_prune` job
  drops `oidc_session` rows of ended sessions and expired `oidc_used_state`
  rows.
- **OIDC-only mode.** `perfin config check` refuses
  `PERFIN_LOCAL_LOGIN_ENABLED=false` without issuer and client id, without
  `PERFIN_BREAKGLASS_CIDRS`, or without `PERFIN_BREAKGLASS_ADMIN_USERNAME`
  (the break-glass account the operator created; the check cannot see the
  database, `perfin breakglass status` does).

## Authorisation model

```
instance admin  -> manages users, OIDC config, system settings, backups
household owner -> everything in the household, can delete the household
household admin -> everything except delete and member removal of the owner
member          -> read/write on accounts granted to them, own documents, review queue
viewer          -> read-only on granted accounts; no documents download by default
```

- Every query is scoped by `household_id` in the application **and** by a
  Postgres RLS policy keyed on a session variable set per request. A bug that
  forgets a `WHERE` clause returns nothing rather than someone else's data.
- Object-level checks use the `(household, account_grant)` pair; IDs are
  UUIDv7 so they are unguessable but authorisation never relies on that.
- API tokens carry explicit scopes (`documents:write`, `transactions:read`,
  ...) and inherit the user's household grants, never more.
- Personal access tokens (`app/auth/tokens.py`, `app/auth/scopes.py`,
  router `auth_tokens`; API.md "Token notes"):
  - Created from a browser session with step-up, bound to the session's
    active household, with a name, at least one scope and a mandatory expiry
    (`PERFIN_TOKEN_MAX_DAYS`, default 90, capped at 365). `ai:read` is
    reserved for the AI-client flow (Phase 6b) and refused here. Only the
    SHA-256 of the token is stored; the raw value is shown once. A
    break-glass account gets no tokens (`403 breakglass-account`; B7). While
    `PERFIN_REQUIRE_MFA` is on, the account also needs a PerFin second
    factor (`403 mfa-enrollment-required`): a bearer request has no MFA of
    its own and is refused for an account without one, so an OIDC session
    verified only by trusted IdP MFA (`PERFIN_OIDC_TRUST_IDP_MFA`) cannot
    create tokens until the user enrols a factor in PerFin.
  - Scopes are a ceiling, not a grant: the catalog in `app/auth/scopes.py`
    gives each role a ceiling (owner all, admin all but `ai:read`, member
    without `households:write`/`export`, viewer the `:read` scopes). A user
    can only create tokens within their ceiling, and on every request the
    token's scopes are intersected with the ceiling of the user's *current*
    role, so a demotion narrows the token at once. Endpoints still apply
    their role and account-grant checks to the token's user. Browser
    sessions carry no scope list; their role guards decide.
    `require_scope` refuses names that are not in the catalog.
  - Bearer requests never fall back to the cookie. The token dies with the
    membership, the user's deactivation, revocation or expiry. Failed bearer
    attempts are limited per client IP before the lookup and use per token
    (`TokenLimits`, overridable on `app.state.token_limits`). `last_used_at`
    is written at most once a minute. Create and revoke are audited
    (`token.create`, `token.revoke`) without the secret.
- Implementation (`app/domain/households/`, routers `households`,
  `admin_users`, `settings_user`):
  - Household endpoints act on the **active** household only (the
    session's, or the token's); any other id is `404`, also for an instance
    admin. Tokens need the `households:read` / `households:write` scopes. Switching (`POST /auth/session/household`) checks the membership
    and rotates the session (`sessions.switch_household`).
  - Members: any role reads the list; owner and admin manage it, on a browser
    session with step-up MFA. Only an owner appoints, demotes or removes an
    owner (so an admin can neither demote nor remove the owner, nor make
    themselves owner); several owners are allowed. Nobody changes or removes
    their own membership through these endpoints (another owner/admin does),
    and a household always keeps an owner. Owner and admin may rename the
    household and change its base currency; only an owner (with step-up)
    deletes it.
  - Every membership change is audited (before/after role), emits a
    household event (`member.added`, `member.role_changed`,
    `member.removed`) for the SSE stream and the notification channels of
    Phase 7, and revokes the affected user's sessions acting in that
    household (or in none) with reason `privilege_change` (SECURITY.md A3,
    I2); their other households' sessions are untouched. Removal also
    deletes their account grants and revokes their personal access tokens
    in the household. Member management is serialised per household by a
    row lock on the household.
  - Account grants: service functions (`set_account_grant`,
    `revoke_account_grant`, `list_account_grants`) for owner/admin; only
    members and viewers get grants (owners and admins see every account),
    viewers at most `view`. The endpoints come with accounts in Phase 2.
  - Instance admin (`/admin/users`, browser session; every change with
    step-up): list, create local users (never instance admin or
    break-glass), deactivate (revokes every session and token), set the
    break-glass flag (needs a local password; ends the account's sessions
    and revokes its tokens),
    mark a local email verified (needs an address), reset another user's MFA
    (removes every factor and session). An admin cannot change their own
    account there. The instance-admin flag has no API at all (B6).
  - Inviting a member can create a local account (same password policy as
    everywhere). Adding an existing user by username tells a household
    admin whether that username exists on the instance; household admins
    are trusted with that. Adding an existing user needs no consent from
    that user, and like every membership change it ends their live sessions
    acting in the household or in none yet (A3), so an owner or admin who
    adds a user without a household (for example an instance admin) logs
    that user out; each add is audited (`member.add`) and needs step-up.
    An invitation the user accepts would remove both effects; it is not
    built yet (review P1-SR-05, CONTROLS.md I2).

## Admin bootstrap

First boot with no users: the API prints a one-time setup URL with a random
token to the log (and to a file in the data volume readable only by the
container user). Visiting it creates the first instance admin. The route
disables itself permanently afterwards. No default credentials, ever.

Implementation (`app/auth/bootstrap.py`, `app/api/routers/setup.py`):

- `perfin api` checks at start whether setup is pending (no user and setup
  never completed). If so it draws a 256-bit token (45 letters, so the log
  scrubber leaves it intact), stores only its SHA-256 with a 24-hour expiry
  in the system setting `bootstrap.setup_token` (replacing any earlier
  token), logs `<PERFIN_PUBLIC_URL>/setup#t=<token>` once at WARNING (the
  only log line that carries a secret) and writes it to
  `PERFIN_BOOTSTRAP_URL_FILE` (default
  `/var/lib/perfin/documents/.perfin-setup/setup-url` on the documents
  volume, file 0600, directory 0700, symlinks refused). `perfin bootstrap-url`
  (for example `docker compose exec api perfin bootstrap-url`) prints a
  fresh URL the same way and exits 1 once setup has happened.
- The token is in the URL fragment, which the browser never sends; the setup
  page posts it in the body of `POST /api/v1/setup` with username, password
  (normal password policy, breached list included), display name, household
  name and base currency (default `USD`). `GET /api/v1/setup/status` tells
  the SPA whether to show the page.
- Setup runs under a transaction-scoped advisory lock and re-checks "no user
  and not completed" inside it, so of concurrent requests exactly one
  succeeds and the others get `404`. It creates the user (instance admin),
  the household (owner membership) and a session (`login_method =
  bootstrap`; with `PERFIN_REQUIRE_MFA` restricted to enrolling a second
  factor), records `bootstrap.completed`, deletes the token and the URL file,
  and audits `instance.bootstrap` and `household.create`. From then on the
  route is `404` for good, even if every user is later deleted. A wrong or
  expired token is `403 invalid-setup-token`; attempts are rate limited per
  client IP (10 per 15 minutes).

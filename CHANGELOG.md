# Changelog

All notable changes to PerFin are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Resume here

Phase 0 (Foundations) is integrated: every gate in `make check` passes and the
Compose stack boots (db-init roles, `migrate` applies revision `0001`, api and
worker healthy behind Caddy). One Phase 0 box is still open in
`docs/ROADMAP.md`: CI has not yet run on GitHub Actions, and the full runtime
image (Tesseract via apt) and its Trivy image scan have not been built, because
the build sandbox blocks its external fetches (deb.debian.org, the ghcr.io uv
image); live compose and Trivy evidence so far used a local variant image.

Phase 1 (Identity, v0.1) is finalized: every gate in `make check` plus
`pip-audit` and `npm audit --audit-level=high` passes, and the identity flow
was run once end to end against a live `perfin api` (dev mode, real
PostgreSQL, built SPA): bootstrap through `POST /setup`, enrolment-only
session, TOTP enrolment, logout, login with password plus TOTP (replayed code
refused), step-up, personal access token creation, bearer calls with scope
enforcement, revocation. Seven of nine Phase 1 roadmap boxes are ticked; the
authz matrix box waits for CI to run on GitHub Actions (as the Phase 0 CI box
does) and the Authentik guide box waits for a walk-through against a live
Authentik. Status per control: `docs/security/CONTROLS.md` (A1, A6, A8, F3,
I2 and G1 stay partial for later-phase reasons stated in each row).

Next: Phase 2 (Ledger). Open items carried over: `Idempotency-Key` is not
implemented (docs/API.md says so); the pre-auth CSRF cookie
(`__Host-perfin-pre`) and the OIDC cookies are always `Secure`, so dev mode
over plain http works only on `localhost` (where browsers allow `Secure`
cookies), unlike the session cookie, which has a `perfin-session-dev`
fallback; npm audit lists 14 moderate advisories in dev tooling (eslint,
typescript-eslint, openapi-typescript via minimatch), none high. (Time-zone
validation uses the system tz database; the `python:3.12-slim` runtime base
ships it, checked with `zoneinfo.ZoneInfo("Europe/Berlin")` in that image.)

## [Unreleased]

### Added

- Licence: GNU AGPL-3.0-or-later (`LICENSE`), SPDX header on every source
  file enforced by `scripts/check_spdx.py`.
- Backend scaffold: `perfin` package (`app`), hash-locked `uv.lock` with the
  runtime dependencies for the whole roadmap, Ruff / mypy `--strict` / Bandit /
  pytest configuration.
- `create_app(settings)` with router auto-discovery from `app/api/routers`
  (`router` under `/api/v1`, optional `root_router` at the root); OpenAPI and
  docs only in dev mode.
- `perfin` CLI: `api`, `migrate`, `openapi`, `healthcheck`, `config check`
  (settings validation), placeholders for later-phase commands.
- `Settings` covering every `PERFIN_*` variable in `deploy/.env.example`,
  secrets as `SecretStr`, master key from `PERFIN_MASTER_KEY` or
  `PERFIN_MASTER_KEY_FILE`.
- RFC 9562 UUIDv7 generator, monotonic within a process.
- Database base: `Base`, `UUIDPk`, `Timestamps`, `HouseholdScoped` (RLS
  registry), minimal `household` table, engine/session factories with a
  statement timeout, RLS DDL and least-privilege grant generation,
  transaction-local `set_context`, model registry, Alembic environment.
- Test fixtures: a unique database and a non-owner, non-superuser login role
  per pytest session so row-level security is really exercised.
- Frontend scaffold: Vite + React 18 + TypeScript (strict,
  `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`), TanStack Router
  with an explicit feature list, ESLint strict-type-checked + security +
  no-unsanitized + react-hooks with `dangerouslySetInnerHTML`/`innerHTML`
  banned, Vitest + Testing Library, no inline assets.
- Root `Makefile` (`check`, `backend-check`, `frontend-check`, `audit`, `test`,
  `dev`, `openapi`) and `docs/security/CONTROLS.md`.

#### Phase 0: Foundations

- `perfin config check` (SECURITY.md H6): refuses placeholder or weak master
  and secret keys, world-readable key files, default database passwords,
  non-https public URL or OIDC issuer, insecure cookies, `DEBUG` logging,
  local-login lockout, any-address break-glass or trusted-proxy ranges, and
  MCP HTTP without explicit origins; warns on risky settings (private OIDC
  issuer, MFA off, Trusted Types off, no key-backup acknowledgement). `api`,
  `worker` and `mcp` run it at startup and refuse to boot on errors.
- Structured JSON logging with a PII/secret scrubber on every handler
  (accounts, cards, IBANs including masked forms, SSN/EIN, emails, phones,
  dates of birth, tokens, cookies, URL credentials, JWTs) and denylisted
  fields dropped (H8).
- Web hardening: strict CSP with Trusted Types, HSTS, `X-Frame-Options`,
  Referrer/Permissions policies, COOP/CORP, `Cache-Control: no-store`
  everywhere except hashed assets; CSRF middleware (`Sec-Fetch-Site`/`Origin`
  checks, HMAC session token hook, signed pre-auth double submit); RFC 9457
  problem-details errors with no stack traces; request ids (UUIDv7) and
  forwarded headers honoured only from `PERFIN_TRUSTED_PROXIES`; body-size
  limits (F1, F5-F9).
- `/healthz` and `/readyz` (database reachable and at the migration head)
  outside `/api/v1`, and Prometheus metrics on an internal port only (H7).
- The API serves the built SPA from `PERFIN_STATIC_DIR` (`/app/static` in the
  image) under the same CSP; client routes fall back to `index.html` without
  changing API 404s or 405s.
- Platform tables `household`, `job`, `event`, `audit_log`, `setting`; forced
  row-level security on every household-scoped table; `audit_log` append-only
  (grants plus triggers for every role) and SHA-256 hash-chained; least-privilege
  grants for the application role; `statement_timeout` and `lock_timeout` on
  every connection (F2, F3, F8, I4).
- Alembic revision `0001_phase0_foundations` (autogenerated tables plus the RLS,
  audit and grant DDL from `app.db.rls`, and `SELECT` on `alembic_version` for
  `/readyz`); `perfin migrate` runs it as the owner role.
- `perfin audit verify`: recomputes the audit hash chain as the owner role and
  prints the head hash.
- Postgres job queue (ADR-0002): `enqueue`/`dequeue` with `SKIP LOCKED`,
  priorities, leases and a reaper, retries with backoff, dead-lettering,
  per-household concurrency limit, idempotent `dedupe_key`; `perfin worker`
  with handler registry, job timeouts, graceful shutdown, a recurring-job
  scheduler and the `noop` handler (C10).
- `event` emission with `NOTIFY` for the future SSE stream, and pruning.
- Deployment: multi-stage image running as UID 10001 on a read-only root;
  hardened Compose (all capabilities dropped, no-new-privileges, tmpfs, limits,
  only the proxy publishes ports, isolated internal networks, one-shot
  `migrate`); `deploy/db-init` creates non-superuser `perfin_owner` and
  `perfin_app` roles with SCRAM passwords and restricts the bootstrap superuser
  to the local socket; Caddy example with TLS, HSTS, body limits and log
  filtering; CONNECT-only egress allowlist proxy (H1-H3, H5).
- CI and security workflows: SPDX, lockfile integrity, Ruff, mypy, Bandit,
  pytest on PostgreSQL 16, OpenAPI and generated-type drift, ESLint, `tsc`,
  Vitest, build, Compose/Caddy validation, image build and run test,
  gitleaks, pip-audit, npm audit, Trivy fs and image scans, CycloneDX SBOM,
  Dependabot (H4, H5, F10).
- Frontend shell: layout with navigation for every section, theme (system,
  light, dark), error boundary, design-system components, decimal-string money
  and timezone-safe date helpers, the typed API client with CSRF header and
  problem errors, and SSE-to-query invalidation.
- Tests: migrations on a fresh database (no drift, forced RLS, least
  privilege, append-only audit log, downgrade and re-upgrade), the
  `app/api`/`app/domain` -> `app/ingestion/extract` import boundary, and
  linear-time scrubber patterns.

#### Phase 1: Identity (v0.1)

- `perfin breakglass status|enable|disable` (B7): the operator flags the
  bootstrap instance admin as the break-glass account (audited
  `user.breakglass_set` with `via: cli`; ends every session of the account)
  and checks the account named by `PERFIN_BREAKGLASS_ADMIN_USERNAME` against
  the database (exists, active, flagged, instance admin, local password,
  second factor), which `perfin config check` cannot do. Replaces the
  interim SQL procedure in `docs/AUTHENTIK.md`.
- `PATCH /admin/users/{id}` takes `email_verified` (instance admin, step-up,
  audited `user.email_verified_set`, `409 no-email` without an address), and
  `GET /admin/users` returns it: an instance admin vouches for a local
  account's address so OIDC link-by-verified-email (B3) can complete for
  pre-created accounts.
- `docs/AUTHENTIK.md`: Authentik 2024.x/2025.x setup guide (provider,
  groups and role mapping, signing key, MFA and `amr`, logout, OIDC-only mode
  with the break-glass admin through an SSH tunnel, a LAN issuer and a
  private CA via `SSL_CERT_FILE`, optional forward-auth gate,
  troubleshooting by `oidc_error` code), resolved against the final code.
- `tests/security/test_input_models.py` (F4): every request body of every
  route, walked from the OpenAPI document, must be a named model that
  forbids unknown fields, recursively; exemptions are listed and checked for
  staleness.
- `tests/security/test_egress_private_ca.py`: a synthetic private CA is
  trusted only through `SSL_CERT_FILE`, and only for the names on the
  certificate.

- Web app identity pages (`frontend/src/features/auth`, `setup`, `settings`):
  sign-in with the local form and a "Sign in with Authentik" button (from
  `GET /auth/login/options` and `GET /auth/oidc/status`), the second-factor
  step (security key or passkey via `@simplewebauthn/browser`, authenticator
  code, recovery code), the enrolment-only flow of `PERFIN_REQUIRE_MFA`, the
  OIDC "link by verified email" page, first-run `/setup` and
  `/reset-password` (one-time tokens read from the URL fragment, removed from
  the address bar and sent only in the request body), and `/forgot-password`.
  Settings: profile; security (password change, authenticator app with a
  locally rendered QR code, security keys, recovery codes shown once,
  sessions, personal access tokens with a scope picker and a show-once
  token); household members by role. A reusable step-up dialog retries the
  original request after verification. A route guard sends anonymous
  visitors to `/login?return_to=` (relative paths only), pending logins to
  `/login/mfa` and enrolment-only sessions to `/login/enroll-mfa`. The
  header shows the household switcher and sign-out (ending OIDC sessions at
  the IdP when it offers an end-session URL).
- Identity core: `app_user`, `credential`, `mfa_method`, `identity`,
  `user_session`, `household_member`, `account_grant`, `api_token` and
  `rate_limit_event` models; the household-scoped ones under forced RLS with
  SELECT-only self-read policies (and a token-prefix lookup policy).
- `app.core.crypto`: XChaCha20-Poly1305 envelope encryption with a DEK per
  value, master-key version in the envelope, associated data binding each
  value to its table, column and row, and DEK rewrap for rotation (G1).
- Argon2id password hashing (64 MiB, t=3, p=4, parameters stored,
  rehash detection) and a full-cost dummy verification for unknown users
  (A1, A2).
- Server-side sessions: `__Host-perfin-session` cookie (`HttpOnly; Secure;
  SameSite=Lax`), SHA-256 at rest, idle/absolute/remember-device lifetimes,
  MFA-pending stage, rotation on MFA completion, step-up and household
  switch, list/revoke (A3, A4, A6); the CSRF token is now bound to the
  session (A5).
- Principal resolution from a session cookie or a `Bearer` personal access
  token (an `Authorization` header never falls back to the cookie, SR-8),
  setting the RLS context per request; guards `require_authenticated`,
  `require_role`, `require_instance_admin`, `require_scope`,
  `require_step_up`, `require_mfa_if_enforced` and account-grant checks (I3).
- DB-backed sliding-window rate limits per IP and per account and
  progressive account lockout (A1); hourly `auth_prune` job.
- `GET /api/v1/auth/session` (who am I, household, MFA state, CSRF token, or
  a `401` problem with a pre-auth CSRF token) and `GET/DELETE
  /api/v1/auth/sessions[/{id}]`.
- `PERFIN_SESSION_REMEMBER_DAYS` (default 30).
- Test factories `make_household`, `make_user`, `login_as`, `bearer_token`,
  `authed_client` (docs/DEVELOPMENT.md, "Tests").
- Second factors (`app/auth/mfa/`, `app/api/routers/auth_mfa.py`): TOTP
  enrolment with a server-generated, encrypted secret and replay protection
  (A7); WebAuthn / passkeys via py_webauthn with server-side, session-bound,
  single-use challenges and sign-counter checks; ten Argon2id-hashed
  single-use recovery codes with their own rate limit, lockout, audit and a
  `security.recovery_code_used` event (A8). `POST /auth/mfa/verify` completes
  an MFA-pending login and `POST /auth/step-up` re-verifies, both rotating
  the session id (A3, A6). New `mfa_challenge` table and hourly `mfa_prune`
  job. `app.auth.mfa.policy` (`login_stage`, `mark_mfa_from_idp`) for the
  login and OIDC flows; `PERFIN_OIDC_TRUST_IDP_MFA` (default false) lets an
  IdP `amr` claim satisfy MFA.
- Local login (`app/auth/local.py`, `app/api/routers/auth_local.py`):
  `POST /auth/login` with per-IP and per-account limits, one full-cost
  Argon2id verification on every path (dummy hash for unknown users), the
  same `401 invalid-credentials` for every refusal, progressive lockout with
  a `user.locked` audit row, household event and notifier hook, hash upgrade
  on login, and an `mfa_pending` session for users with a second factor
  (A1, A2, A3). `POST /auth/logout`, `POST /auth/password` (current password
  plus step-up when the user has a factor; rotates the session, revokes the
  others), `GET /auth/login/options`. Every login success and failure,
  logout, password change and reset is audited without secrets.
- OIDC-only mode: with `PERFIN_LOCAL_LOGIN_ENABLED=false` local login works
  only for `is_breakglass` accounts from `PERFIN_BREAKGLASS_CIDRS`; a
  break-glass account is refused outside its CIDR in every mode (B7).
- Breached-password list bundled in `app/auth/data/` (about 73,000 common
  passwords of 12 to 128 characters from SecLists, MIT; provenance in its
  README; `scripts/build_breached_passwords.py` rebuilds it) and an opt-in
  Have I Been Pwned k-anonymity check (`PERFIN_HIBP_CHECK_ENABLED`).
- Password reset by email (A11): `PERFIN_SMTP_*` settings with config-check
  warnings, a TLS-only SMTP sender (`app/integrations/notifications/smtp.py`),
  `POST /auth/password-reset/request` (always `202`; the work happens after
  the response) and `POST /auth/password-reset/confirm` (token from the
  link's fragment in the body; 15 minutes, single use, hashed at rest; revokes
  every session). New `password_reset_token` table and hourly
  `password_reset_prune` job.
- First-run setup (A10): with no user, `perfin api` logs a one-time setup URL
  (token in the fragment, only its hash stored, 24 h) and writes it to
  `PERFIN_BOOTSTRAP_URL_FILE` (0600); `perfin bootstrap-url` prints a fresh
  one; `GET /setup/status` and `POST /setup` create the first instance admin
  and household under an advisory lock, after which the route is gone for
  good.
- Households and members (`app/domain/households/`,
  `app/api/routers/households.py`): `GET/POST /households` (all households
  and creation for instance admins), `GET/PATCH/DELETE /households/{id}`
  (name, ISO 4217 base currency; delete by an owner with step-up) and
  `GET/POST/PATCH/DELETE /households/{id}/members[/{user_id}]` (add an
  existing user or create a local one, change role, remove). Member changes
  need owner/admin plus step-up; only owners appoint, change or remove
  owners, nobody edits their own membership, the last owner stays; every
  change is audited, emits a household event and revokes the affected
  user's sessions in that household (I2, A3). Other households are `404`
  (F3); inputs forbid unknown fields (F4). `POST /auth/session/household`
  switches the session's household (membership checked, session rotated).
  Account-grant service functions (endpoints with accounts in Phase 2).
- `GET/PATCH /settings/user`: display name, locale, theme, time zone.
- Instance administration (`app/api/routers/admin_users.py`): list and
  create local users, activate/deactivate (revokes sessions and tokens),
  set the break-glass flag, reset another user's MFA; changes need step-up.
- OIDC login (`app/auth/oidc/`, `app/api/routers/auth_oidc.py`): Authorization
  Code with PKCE S256, `state` and `nonce` in a sealed 5-minute
  `__Host-perfin-oidc` cookie, single-use `state` (`oidc_used_state`), code
  exchange with `client_secret_basic` or `private_key_jwt`, ID token
  validation with pinned `RS256`/`ES256`, JWKS by `kid` (refetch on unknown
  `kid` at most once per interval), exact `iss`, `aud`/`azp`, `exp`, `iat`
  (60 s skew, 10 minutes max age), `nonce` and `at_hash` (A9, B1, B2, B9).
  Users matched by `(iss, sub)` only; JIT provisioning; link by verified
  email only after a fresh local login (+MFA) to that account; group-to-role
  mapping in the OIDC household, never `owner` or instance admin (B3, B6).
  Same-origin `return_to` (B4). RP-initiated logout with `id_token_hint`
  and an allowlisted `post_logout_redirect_uri`; back-channel logout by
  `sid`/`sub`; optional claim refresh with an encrypted refresh token
  (`oidc_claim_refresh` job); `oidc_session` table following session
  rotation through the new `app.auth.sessions.register_rotation_hook` (B8);
  hourly `oidc_prune` job.
- `app.core.http.SafeHttpClient`: SSRF-safe outbound HTTP (issuer-host
  allowlist, own DNS resolution with every address vetted and the connection
  pinned to it, link-local/metadata never, loopback only in dev mode,
  private ranges only with `PERFIN_OIDC_ALLOW_PRIVATE_ISSUER`, same-origin
  redirects only, 1 MiB cap, timeouts; `HTTPS_PROXY` honoured) (B5).
- New settings `PERFIN_OIDC_TOKEN_AUTH_METHOD`, `_PRIVATE_KEY_FILE`,
  `_PRIVATE_KEY_ID`, `_LINK_BY_VERIFIED_EMAIL`, `_HOUSEHOLD_ID`,
  `_POST_LOGOUT_REDIRECT_URIS`, `_CLAIM_REFRESH_MINUTES`,
  `_JWKS_MIN_REFRESH_SECONDS`, `_METADATA_MAX_AGE_SECONDS`,
  `_HTTP_TIMEOUT_SECONDS` and `PERFIN_BREAKGLASS_ADMIN_USERNAME`;
  `perfin config check` refuses OIDC-only mode without issuer, client id,
  break-glass CIDRs and a named break-glass admin, checks the
  `private_key_jwt` key file and the logout allowlist (B4, B7).
- Personal access tokens (`app/auth/tokens.py`, `app/auth/scopes.py`,
  `app/api/routers/auth_tokens.py`): `GET/POST /auth/tokens`, `DELETE
  /auth/tokens/{id}` and `GET /auth/tokens/scopes`, browser sessions only,
  creation behind step-up MFA. Tokens are `pfn_<id>_<256-bit secret>`, stored
  as SHA-256 only and shown once. Each has a name, at least one scope from the
  catalog that the creator's role may grant (`ai:read` is reserved), and a
  mandatory expiry of at most `PERFIN_TOKEN_MAX_DAYS` (new setting, default
  90, cap 365). At most 50 live tokens per user and household. Scopes are
  narrowed on every request to the ceiling of the user's current role, and
  `require_scope` refuses names outside the catalog. Failed bearer attempts
  are rate limited per IP and use per token. Create and revoke are audited.
  A token now lists only its own household in `GET /households` (I3).
- Phase 1 integration: Alembic revision `0002` (`0002_phase1_identity`)
  creates the thirteen identity tables, applies forced RLS with the
  self-read and token-prefix policies to `household_member`,
  `account_grant` and `api_token`, grants the application role DML, and its
  downgrade drops the six enum types. `tests/integration/test_migrations.py`
  now pins the expected Phase 1 tables, policies and grants, the explicit
  list of identity tables without RLS, the `0002 -> 0001` step and RLS
  behaviour on the migrated schema.
- End-to-end tests through the real app (`tests/integration/test_auth_e2e.py`):
  local login, MFA, `GET /auth/session`, step-up, token creation, bearer
  calls, revocation and logout; OIDC login with the fake IdP, token creation
  and back-channel logout.
- `tests/security/test_auth_surface.py`: every state-changing route (enumerated
  from the routers and checked against the OpenAPI document) refuses a
  request without a CSRF token, anonymous or with a session, except the OIDC
  back-channel logout; every error of the auth, setup, household, settings
  and admin routes is `application/problem+json`.
- Authorisation matrix (`tests/security/test_authz_matrix.py`, policy table
  `tests/security/authz_policy.py`): every route of the app, walked from
  `app.routes`, must have a policy entry (minimum role, token scope,
  step-up, instance admin or public), and is called as fourteen principals
  (anonymous, each role, instance admin, another household's owner,
  MFA-restricted and MFA-pending sessions, stale step-up, tokens without
  scopes, with the route's scope and expired, a valid cookie plus a junk
  bearer token) with real objects as path parameters; foreign objects and
  other households are `404`. A CSRF matrix refuses every state-changing
  route without the token, with another session's or the pre-auth token,
  and cross-site (F3, I3, A5, A6).
- `perfin api` on first boot also prints the setup URL to stderr as plain text
  when `PERFIN_LOG_LEVEL` suppresses the WARNING log line that normally
  carries it; tested end to end on an empty database.

### Security

- Phase 1 security review round 1 fixes:
  - A break-glass account can no longer escape `PERFIN_BREAKGLASS_CIDRS`
    through a personal access token (P1-SR-01, B7). `POST /auth/tokens` is
    `403 breakglass-account` for it, bearer authentication refuses its
    tokens from every address, and setting the break-glass flag revokes the
    account's tokens (audited as `pats_revoked`). Before, a token minted on
    the allowlisted network worked from anywhere.
  - A correct password no longer resets the lockout counter that failed
    second-factor attempts built up (P1-SR-02, A1/A8). The counter is reset
    only by a verified second factor (or by the password of an account
    without one), so interleaving password logins with code guesses no
    longer avoids the progressive delay and the 15-minute lock.
  - Back-channel logout tokens must carry a `jti` and work once (P1-SR-03,
    B8): the domain-separated SHA-256 of `(iss, jti)` is recorded in
    `oidc_used_state` (no schema change) until the token is too old anyway,
    and a replay is `400`. Before, a captured token could be replayed for
    ten minutes to end sessions opened after the original logout.
  - The CSRF matrix test pins the exemption list instead of deriving it
    from the production constant, so a new exemption can no longer remove
    a route from the test silently (P1-SR-04, A5).
  - Documented that adding an existing user to a household needs no consent
    from that user and ends their household-less sessions (P1-SR-05); an
    invitation flow is still open (CONTROLS.md I2).
- Phase 1 security review round 2 fixes:
  - Trusted IdP MFA (`PERFIN_OIDC_TRUST_IDP_MFA`) is dated by the ID
    token's `auth_time`, not by the callback (P1-SR2-01, A6). Before, any
    OIDC login whose `amr` showed MFA, including a silent SSO re-login on
    an IdP session that did its MFA weeks ago, gave the session a fresh
    10-minute step-up, also for users with a PerFin factor. An ID token
    without a usable `auth_time` now satisfies MFA but is never a step-up.
    New: `GET /auth/oidc/start?reauth=true` forces a new IdP login
    (`prompt=login&max_age=0`) as step-up through the IdP, and the SPA's
    verification dialog offers it to OIDC users without a PerFin factor.
  - `return_to` refuses `.` and `..` path segments, plain or
    percent-encoded, on the server and in the SPA, and the SPA re-checks its
    normalised result (P1-SR2-02, B4). Before, `/a/..//evil.example`
    normalised to the protocol-relative `//evil.example` in the SPA; only
    the router's own checks kept it on the site.
  - A back-channel logout records the token's `jti` in the same
    transaction as the revocation (P1-SR2-03, B8). Before, a revocation
    that failed after the `jti` was committed made the IdP's retry a
    `400` replay, and the sessions stayed alive.
- Phase 1 security review round 3 fixes:
  - Trusted IdP MFA is a step-up only for a forced sign-in (P1-SR3-01, A6):
    the sealed pre-auth cookie records whether the login was started with
    `reauth=true` and when, and only such a login whose ID token
    `auth_time` is not older than its start (minus the 60 s clock skew)
    stamps `mfa_verified_at = min(now, auth_time)`. Every other trusted-MFA
    login satisfies MFA but is dated at least just outside the 10-minute
    window. Before, a plain login with a recent `auth_time` was a step-up,
    so an IdP that stamps `auth_time` at token issue on a silent SSO answer
    gave a step-up with no MFA done. The `user.login` audit records
    `reauth`. AUTHENTIK.md 7c now asks admins to check that a forced
    sign-in re-runs the MFA stage and what `auth_time` a silent SSO sign-in
    carries.
- Phase 1 finalize: setting or clearing an account's break-glass flag (API
  or CLI) now ends every session of that account. Before, a session opened
  before the flag was set was not a break-glass login, so the per-request
  network check (B7) did not apply to it and it kept working from anywhere
  until it expired.
- Phase 1 integration fixes:
  - Break-glass sessions are re-checked against `PERFIN_BREAKGLASS_CIDRS` on
    every request, not only at login: from elsewhere the cookie counts as no
    session, is not touched and cannot finish a pending MFA step (B7).
  - Token creation is refused (`403 mfa-enrollment-required`) while
    `PERFIN_REQUIRE_MFA` is on and the account has no PerFin second factor.
    Bearer requests are refused for such accounts, so a session verified
    only by trusted IdP MFA used to mint tokens that never worked (I3).
  - `GET /settings/user` needs the `settings:read` scope for tokens; a
    token without any scope could read the user's settings (I3, found by
    the authorisation matrix).
  - `POST /auth/oidc/link` refuses bearer callers up front
    (`403 session-required`) instead of relying on the link flow alone.
  - The OIDC back-channel logout's `400` is now a problem-details response
    (`invalid-logout-token`) that keeps the OAuth `"error":
    "invalid_request"` member (F5).
- Phase 0 security review fixes:
  - `household` and `job` now have forced RLS, so all five platform tables
    are covered. A household row can only be read, changed or deleted in its
    own context: the `ON DELETE CASCADE` from every household-scoped table
    runs outside RLS, so before this fix one wrong `DELETE FROM household` in
    another household's context wiped that household's data (SR-1, SR-3).
    `prune_events` lists households through `perfin_household_ids()` (ids
    only).
  - `job` follows the household context. The queue machinery works across
    households only inside `queue_scope`, and a trigger keeps a job's `id`,
    `queue`, `type`, `payload`, `household_id`, `dedupe_key` and
    `created_at` immutable for every role (SR-4).
  - Job payloads must be JSON objects: enqueue and handler registration
    refuse `RootModel` payloads, a check constraint backs this, and
    `dequeue` no longer coerces the payload. A malformed row is now
    dead-lettered instead of wedging its queue forever (SR-2).
  - Instance-level audit rows can only be appended in an instance-admin or
    system-actor context (`set_context(..., system_actor=True)`) (SR-5).
  - CSRF checks every method except `GET`/`HEAD`/`OPTIONS` (SR-7).
  - The proxy refuses `/healthz*`, `/readyz*` and `/metrics*` by prefix. The
    metrics server binds `PERFIN_METRICS_BIND_HOST`, which defaults to
    loopback, so the proxy and mcp containers cannot reach it (SR-9).
  - `create_app()` without explicit settings (the uvicorn factory path)
    refuses an insecure production configuration (SR-11).
- The log scrubber's masked-account, secret-name, email and URL-credential
  patterns backtracked quadratically (8-31 s for one 20 000-character
  string); they are now linear (about 50 ms for 50 000 characters) and a test
  enforces it.

### Changed

- `deploy/docker-compose.yml`: the `edge` network has a fixed subnet
  (`EDGE_SUBNET`, default `172.30.79.0/24`, gateway `EDGE_GATEWAY`,
  `172.30.79.1`), so the address host-local clients such as an SSH tunnel
  arrive from is predictable for `PERFIN_BREAKGLASS_CIDRS`. The shipped
  `PERFIN_BREAKGLASS_CIDRS=127.0.0.1/32` stays as a fail-closed default
  (break-glass login through the proxy is off until set); `.env.example` and
  `docs/AUTHENTIK.md` explain the gateway value and the IPv6 userland-proxy
  caveat.
- `deploy/egress-allowlist.txt`: the OIDC issuer comment no longer suggests
  that a LAN issuer needs no allowlist entry.
- `docs/API.md` lists the endpoints Phase 1 added beyond the first cut
  (`/setup`, `/auth/login/options`, `/auth/password`, `/auth/password-reset/*`,
  `/auth/oidc/status|link|logout`, `/auth/step-up`, `/auth/mfa/*`,
  `/auth/tokens/scopes`, `/auth/session/household`, `/settings/user`,
  `/admin/users`), corrects the sessions and tokens lines, and states that
  `Idempotency-Key` is not implemented yet.
- `deploy/.env.example` keeps comments on their own lines, so the file also
  works with `docker run --env-file` and systemd `EnvironmentFile`.
- Ruff and mypy now cover `backend/migrations`.

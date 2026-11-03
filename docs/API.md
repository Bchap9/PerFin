# API conventions and surface

Base path `/api/v1`. JSON request and response bodies. Browser clients
authenticate with the session cookie plus CSRF header; scripts use
`Authorization: Bearer <personal access token>`.

## Conventions
- UUIDv7 identifiers. Timestamps in RFC 3339 UTC; dates as `YYYY-MM-DD`.
- Money as `{"amount": "123.45", "currency": "USD"}` strings, never floats.
- Pagination: cursor-based (`?cursor=&limit=`), max limit 500.
- Filtering: explicit query parameters, no free-form query language.
- Errors: RFC 9457 problem details (`application/problem+json`) with a stable
  `type` URI and no stack traces or internal identifiers.
- Idempotency: `Idempotency-Key` header honoured on all `POST` that create.
  Not implemented yet (no shared mechanism in v0.1): the Phase 1 creating
  `POST`s (`/households`, `/households/{id}/members`, `/admin/users`) ignore
  it; `POST /auth/tokens` never will (a replay could not return the secret).
- Versioning: path version; breaking changes bump the path.
- Every response carries `Cache-Control: no-store` except static assets.

## Endpoint families (first cut)

Phase 1 (v0.1) implements the identity block below in full; it grew beyond
the first cut with `/setup`, `/auth/login/options`, `/auth/password`,
`/auth/password-reset/*`, `/auth/oidc/status`, `/auth/oidc/link`,
`/auth/oidc/logout`, `/auth/step-up`, `/auth/mfa` (status, TOTP confirm,
WebAuthn options), `/auth/tokens/scopes`, `/auth/session/household`,
`DELETE /households/{id}`, `/settings/user` and `/admin/users`. Once the
first admin exists, `POST /setup` is `404` for good and `GET /setup/status`
answers `{"setup_required": false}`. Everything from `/accounts` on is later
phases.

```
GET    /setup/status                    first-run setup pending? (no user yet)
POST   /setup                           one-time token -> first instance admin + household
GET    /auth/login/options              local form / password reset offered to this client?
POST   /auth/login                      local login (step 1)
POST   /auth/mfa/verify                 local login (step 2)
POST   /auth/logout
POST   /auth/password                   change my password (step-up if I have a factor)
POST   /auth/password-reset/request     always 202; mail sent only to a real account
POST   /auth/password-reset/confirm     token (from the link fragment) + new password
GET    /auth/oidc/status                is OIDC login offered
GET    /auth/oidc/start                 redirect to IdP (?return_to=/same-origin/path&reauth=true)
GET    /auth/oidc/callback              redirect URI; 303 into the app or /login?oidc_error=
GET/POST/DELETE /auth/oidc/link         pending "link by verified email" offer / confirm / drop
POST   /auth/oidc/logout                RP-initiated logout; returns the IdP end-session URL
POST   /auth/oidc/backchannel-logout    IdP posts logout_token (CSRF-exempt)
GET    /auth/session                    who am I, household, role, MFA state, csrf token;
                                        401 problem with a pre-auth csrf_token when anonymous
GET/DELETE      /auth/sessions          list mine / revoke all others
DELETE          /auth/sessions/{id}     revoke one
POST   /auth/step-up                    re-verify any factor; rotates the session
GET    /auth/mfa                        my factors and unused recovery codes
POST   /auth/mfa/webauthn/authentication-options   challenge for verify / step-up
GET    /auth/mfa/webauthn               list security keys / passkeys
POST   /auth/mfa/webauthn/registration-options     start registering one
POST   /auth/mfa/webauthn               finish registering one
DELETE /auth/mfa/webauthn/{id}          remove (step-up)
POST   /auth/mfa/totp                   start enrolment (secret + otpauth URI, once)
POST   /auth/mfa/totp/confirm           finish enrolment with the first code
DELETE /auth/mfa/totp                   remove (step-up)
POST   /auth/mfa/recovery-codes         regenerate (step-up)
GET/POST        /auth/tokens            personal access tokens (browser session; POST step-up)
GET             /auth/tokens/scopes     scope catalog and what my role may grant
DELETE          /auth/tokens/{id}       revoke

GET             /households             my households (instance admin: all of them)
POST            /households             (instance admin)
GET/PATCH       /households/{id}        my active household; PATCH owner/admin
DELETE          /households/{id}        owner, step-up
GET/POST        /households/{id}/members        POST owner/admin, step-up
PATCH/DELETE    /households/{id}/members/{user_id}  owner/admin, step-up
POST            /auth/session/household switch the session's household (rotates)

GET/POST        /accounts
GET/PATCH/DELETE /accounts/{id}
GET             /accounts/{id}/balances         history
POST            /accounts/{id}/balances         manual balance

GET/POST        /transactions                  filters: account, date range, category, payee, tag, review_state, q
GET/PATCH/DELETE /transactions/{id}
POST            /transactions/{id}/split
POST            /transactions/bulk              categorise/tag/delete many

GET/POST/PATCH/DELETE /categories
GET/POST/PATCH/DELETE /payees
GET/POST/PATCH/DELETE /tags
GET/POST/PATCH/DELETE /rules
POST            /rules/test                     dry-run a rule against history
POST            /rules/apply                    re-run all rules (job)

GET/POST/PATCH/DELETE /budgets
GET             /budgets/{id}/report?period=
GET/POST/PATCH/DELETE /goals

GET/POST/PATCH/DELETE /securities
GET/POST/PATCH/DELETE /investments/transactions
GET             /investments/holdings?account=
GET             /investments/performance?range=

GET/POST/PATCH/DELETE /loans
GET             /loans/{id}/schedule
POST            /loans/{id}/simulate            extra payments what-if

GET             /networth?range=
GET             /networth/latest

POST            /documents                     multipart upload -> 202
GET             /documents                     filters: kind, status, account, tax_year, q (full text)
GET             /documents/{id}
GET             /documents/{id}/download        decrypts and streams, attachment
GET             /documents/{id}/preview?page=   rendered PNG, sandboxed
POST            /documents/{id}/reprocess
DELETE          /documents/{id}
POST            /documents/{id}/link            attach to transaction etc.

GET             /review                         open items
GET             /review/{id}
POST            /review/{id}/resolve            payload: chosen values, "remember" flags
POST            /review/{id}/dismiss

GET/POST/PATCH/DELETE /tax-years
GET             /tax-years/{year}/checklist
PATCH           /tax-years/{year}/checklist/{item}
GET             /tax-years/{year}/export        zip (job)

GET             /events                         SSE stream
GET             /jobs/{id}

GET/PATCH       /settings/household
GET/PATCH       /settings/user
GET/PATCH       /settings/system               (instance admin; OIDC, intake, integrations)
GET/POST        /admin/users                   (instance admin; POST step-up)
GET/PATCH       /admin/users/{id}              (instance admin; PATCH step-up)
POST            /admin/users/{id}/mfa-reset    (instance admin, step-up)
POST            /settings/intake/email/test
POST            /settings/intake/email/rotate-alias
GET/POST/PATCH/DELETE /settings/notifications/channels

GET/POST/DELETE /settings/ai/clients            MCP clients (owner, step-up)
GET             /settings/ai/clients/{id}/preview  exposure preview with chosen options
GET             /settings/ai/calls              audit of MCP calls, searchable
GET             /ai/v1/...                      read-only views used by the MCP role
                                                (scope ai:read; allowlist serialisers,
                                                scrubbed free text)

POST            /export                         full export (job)
POST            /import                         full import (job)
GET             /audit?filters                 (owner/admin)

GET             /healthz  /readyz              (unauthenticated, proxy-restricted)
```

MFA notes: `POST /auth/mfa/verify` and `POST /auth/step-up` take one proof:
`{"method": "totp", "code": "123456"}`, `{"method": "recovery", "code":
"XXXX-XXXX-XXXX-XXXX"}` or `{"method": "webauthn", "credential":
<PublicKeyCredential JSON>}`. On success both rotate the session (new cookie)
and return the new session's `csrf_token`; a rejected proof is `400
mfa-invalid` (not `401`, so the SPA keeps its session state), too many
attempts or a locked account `429` with `Retry-After`. WebAuthn options are
`POST` (they store a challenge, so they go through CSRF protection). Factor
management works for the enrolment-only session of `PERFIN_REQUIRE_MFA`;
adding a factor needs step-up once the user has one.

OIDC notes: `GET /auth/oidc/start` and `GET /auth/oidc/callback` are
browser navigations and always answer `303`. `reauth=true` on the start
asks the IdP to authenticate again even with an SSO session
(`prompt=login&max_age=0`): with `PERFIN_OIDC_TRUST_IDP_MFA` the IdP's MFA
counts as step-up only for such a forced sign-in whose `auth_time` is not
older than its start, and then for 10 minutes after that `auth_time` (any
other OIDC login is never a step-up), so this is how an account without a
PerFin factor steps up. The callback goes to the
validated `return_to`, to `/login/mfa?return_to=...` (an `mfa_pending`
session; finish with `POST /auth/mfa/verify`), to `/login/link-oidc` (a
pending link offer, see below) or to `/login?oidc_error=<code>` with `code`
one of `rate-limited`, `state-invalid`, `idp-error`, `idp-unavailable`,
`token-invalid`, `not-in-group`, `no-account`, `account-exists`,
`account-disabled`, `login-failed`. `GET /auth/oidc/link` returns `{"email",
"expires_at"}` or `404 no-pending-link`; `POST /auth/oidc/link` needs a fresh
local browser session of that account (`204`; `403 link-refused` with a
`reason`, `409 already-linked`). `POST /auth/oidc/logout`
(`{"post_logout_redirect_uri"?}`) returns `{"redirect_url": string | null}`;
a URI not on the allowlist is `400 invalid-redirect`. `POST
/auth/oidc/backchannel-logout` takes `logout_token` as a form field and
answers `200` or a `400 invalid-logout-token` problem that also carries the
OAuth member `"error": "invalid_request"`. The token must carry a `jti` and
works once; a replay is `400`. A delivery that fails (`5xx`) does not use
the `jti` up, so the IdP can retry it. All OIDC endpoints are
`404 oidc-disabled` without `PERFIN_OIDC_ISSUER`.

Local login notes: `POST /auth/login` (`{"username", "password",
"remember_device"}`, posted with the pre-auth CSRF token) returns `200` with
`{"status": "authenticated" | "mfa_required", "csrf_token", "mfa": {"required",
"methods"}}` and the session cookie; every refused login is the same `401
invalid-credentials`, too many attempts `429 rate-limited` with `Retry-After`,
OIDC-only mode outside the break-glass networks `403 local-login-disabled`.
New passwords failing the policy are `422 weak-password` with a `reason`
(`too-short`, `too-long`, `breached`, `same-as-username`, `same-as-current`).
`POST /auth/password` and `POST /setup` return the new session's
`csrf_token`. Password reset endpoints are `404 password-reset-unavailable`
unless SMTP is configured and local login is enabled; an invalid, expired or
used reset token is `400 invalid-reset-token`. `POST /setup` is `403
invalid-setup-token` for a wrong or expired token and `404` once setup has
happened.

Household notes: household endpoints act on the caller's **active**
household (the session's, or the token's); any other id is `404
household-not-found`, whatever the caller's role elsewhere. A user in several
households switches with `POST /auth/session/household` (`{"household_id"}`;
`404` unless a member there), which rotates the session and returns
`{"household": {"id", "name", "role"}, "csrf_token"}` plus the new cookie.
`GET /households` lists `{"id", "name", "base_currency", "created_at",
"role"}`; for an instance admin on a browser session it lists every
household (`role` null where they are not a member). `POST /households`
takes `{"name", "base_currency" (ISO 4217, default USD), "owner_user_id"?}`
(the caller becomes the owner when omitted); `PATCH /households/{id}` takes
`name` and/or `base_currency`. `POST /households/{id}/members` takes `role`
and exactly one of `user_id`, `username` (an existing user; unknown is `404
user-not-found`) or `new_user` (`{"username", "display_name", "password",
"email"?}`: creates a local account under the password policy; `409
username-taken`). Member changes need a browser session with step-up (`403
step-up-required`); only an owner appoints, changes or removes an owner (`403
owner-protected`), nobody changes or removes their own membership (`409
own-membership`), and the last owner stays (`409 last-owner`). Adding a
member ends that user's sessions acting in the household or in no household
yet (they log in again). Member
management by tokens is refused; tokens need the `households:read` scope to
read households and members and `households:write` to change the household.
Unknown input fields are `422` everywhere.

User settings notes: `GET /settings/user` returns `{"display_name", "locale",
"theme", "timezone"}` (defaults `en-US`, `system`, `UTC`); `PATCH` takes any
of them (locale a BCP 47 tag, theme `system|light|dark`, timezone an IANA
name) and needs a browser session. A token reads them only with the
`settings:read` scope (`403 insufficient-scope` otherwise).

Admin users notes: `GET /admin/users?cursor=&limit=` (max 500) returns
`{"items": [...], "next_cursor"}` with `id`, `username`, `display_name`,
`email`, `is_active`, `is_instance_admin`, `is_breakglass`, `has_password`,
`mfa_methods`, `locked_until`, `last_login_at`, `created_at`. `POST` creates
a local account (`{"username", "display_name", "password", "email"?}`);
`PATCH` takes `is_active` (deactivation revokes every session and token),
`is_breakglass` (needs a local password, `409 no-local-password`; setting or
clearing it ends every session and revokes every token of the account) and/or `email_verified` (the
admin vouches for the address, which OIDC link-by-verified-email needs; `409
no-email` without one); `email_verified` is also in the response;
the instance-admin flag cannot be set through the API. `mfa-reset` removes
every factor and session of the user. An admin cannot change their own
account here (`409 own-account`).

Token notes (`/auth/tokens`, SECURITY.md I3): personal access tokens look
like `pfn_<12 hex id>_<43 base64url chars>` (a 256-bit secret); only the
SHA-256 is stored and the raw value is returned once, by `POST`. Every
`/auth/tokens` endpoint needs a browser session (`403 session-required`
for bearer callers) in a household (`403 forbidden` without one). A
break-glass account cannot create tokens (`403 breakglass-account`) and any
token it holds is `401 invalid-token` (SECURITY.md B7).
`GET /auth/tokens/scopes` lists the catalog
(`{"scopes": [{"scope", "description", "grantable", "reserved"}],
"max_expires_in_days"}`; `grantable` is what the caller's role may put on a
token). `GET /auth/tokens` lists the caller's tokens in the active household,
newest first: `{"id", "name", "prefix", "scopes", "active_scopes", "status"
("active" | "expired" | "revoked"), "created_at", "expires_at",
"last_used_at", "revoked_at"}`; `active_scopes` are the scopes still within
the caller's current role. `POST /auth/tokens` needs step-up (`403
step-up-required`) and, while `PERFIN_REQUIRE_MFA` is on, a PerFin second
factor on the account (`403 mfa-enrollment-required`: bearer requests are
refused without one, so a session verified only by the IdP's MFA cannot
mint a token that would never work), and takes `{"name" (1-100 chars, no control characters),
"scopes" (1-64, unique), "expires_in_days"}`; expiry is mandatory, at most
`PERFIN_TOKEN_MAX_DAYS` (default 90, `422 expiry-too-long` with `max_days`;
never more than 365). It answers `201` with the list fields plus `token`.
Refusals: `422 unknown-scope`, `422 scope-reserved` (`ai:read`, minted only
by the AI-client flow in Phase 6b), `403 scope-not-allowed` (above the
caller's role), each with `scopes`; `409 too-many-tokens` beyond 50 live
tokens per user and household. `Idempotency-Key` is not honoured here: a
replay could not return the secret again. `DELETE /auth/tokens/{id}` revokes
(`204`, idempotent; `404 token-not-found` for anyone else's token). A token
acts only in its household (`GET /households` lists just that one) with its
scopes narrowed to the user's current role; a demotion takes effect on the
next request, and losing the membership, deactivation, revocation or expiry
answers `401 invalid-token`. Failed bearer attempts are limited per client
IP (60 per 5 minutes) and use per token (600 per minute), both `429
rate-limited` with `Retry-After`. Scopes: `accounts:read|write`,
`transactions:read|write`, `budgets:read|write`, `documents:read|write`,
`investments:read|write`, `loans:read|write`, `networth:read`,
`tax:read|write`, `review:read|write`, `settings:read`,
`households:read|write`, `export`, `ai:read` (reserved). Role ceilings: owner
all; admin all but `ai:read`; member as admin without `households:write` and
`export`; viewer the `:read` scopes.

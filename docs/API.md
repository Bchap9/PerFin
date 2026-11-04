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
- Idempotency: `Idempotency-Key` header honoured on all `POST` that create
  (`app.core.idempotency`; from Phase 2). The key is 1-255 visible ASCII
  characters (else `422 invalid-idempotency-key`), scoped to the household
  and user, and kept 24 hours. The first request runs; a repeat with the
  same key, route, path and body gets the stored status and body plus
  `Idempotent-Replayed: true`; the same key on a different request is `422
  idempotency-key-reused`; a repeat while the first is still running waits
  for it (`409 idempotency-key-in-use` with `Retry-After` after the lock
  timeout). Only successful answers are stored: a failed request may be
  retried with its key. The Phase 1 creating `POST`s (`/households`,
  `/households/{id}/members`, `/admin/users`) ignore it; `POST /auth/tokens`
  never will (a replay could not return the secret).
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
POST            /accounts/{id}/close            closing date (default today)
POST            /accounts/{id}/reopen
GET             /accounts/{id}/balances         history
POST            /accounts/{id}/balances         manual balance
GET             /accounts/{id}/grants           who has a grant (owner/admin)
GET             /households/{id}/members/{user_id}/grants   a member's grants
PUT/DELETE      /households/{id}/members/{user_id}/grants/{account_id}  (owner/admin, step-up)

GET/POST        /transactions                  filters: account, date range, category, payee, tag, review_state, q
GET/PATCH/DELETE /transactions/{id}
POST            /transactions/{id}/split
POST            /transactions/bulk              categorise/tag/untag/delete/confirm many
GET             /transactions/{id}/transfer-candidates   possible other legs of a transfer
POST/DELETE     /transactions/{id}/transfer     link with another leg / unlink
POST            /transactions/transfers/auto-pair        run the transfer matcher

GET/POST/PATCH/DELETE /categories
POST            /categories/{id}/merge          {"into": id}
GET/POST/PATCH/DELETE /payees
POST            /payees/{id}/merge              {"into": id}
GET/POST/PATCH/DELETE /tags
GET/POST/PATCH/DELETE /rules                   read: any role; change: owner/admin
POST            /rules/reorder                  every rule id in the new order
POST            /rules/test                     dry-run a rule against history (no writes)
POST            /rules/apply                    re-run all rules (job; 202 + job id)
GET             /rules/export                   portable JSON (ids plus referenced names)
POST            /rules/import                   append or replace from an export
GET             /jobs/{id}                      state of a job of my household

GET/POST/PATCH/DELETE /budgets
GET             /budgets/{id}/report?period=   budgeted vs actual vs forecast per line
GET/POST/PATCH/DELETE /goals
GET/POST        /goals/{id}/contributions
DELETE          /goals/{id}/contributions/{contribution_id}

GET             /reports/spending               group_by category|payee|month|account, depth
GET             /reports/income
GET             /reports/trends                 month-on-month, year-on-year
GET             /reports/cashflow               monthly in / out / net

GET/POST/PATCH/DELETE /securities
GET/POST/PATCH/DELETE /investments/transactions
GET             /investments/holdings?account=
GET             /investments/performance?range=

GET/POST/PATCH/DELETE /loans
GET             /loans/{id}/schedule
POST            /loans/{id}/simulate            extra payments what-if

GET             /networth?range=
GET             /networth/latest

POST            /documents                     multipart upload (streamed) -> 202
GET             /documents                     filters: kind, status, account, tax_year, q (full text)
GET             /documents/{id}
GET             /documents/{id}/download        decrypts and streams, attachment
GET             /documents/{id}/preview?page=   rendered PNG, sandboxed
POST            /documents/{id}/reprocess
DELETE          /documents/{id}
POST            /documents/{id}/link            attach to transaction etc.

GET/POST        /imports/mappings               saved CSV/TSV column mappings
GET/PATCH/DELETE /imports/mappings/{id}
GET             /imports/{document_id}/preview  extracted rows/statements (stored, never re-parsed)
POST            /imports/{document_id}/apply-mapping  re-run the import (job; 202 + job id)

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

POST            /export                         full export (job; owner/admin, step-up)
GET             /export/{job_id}                state of my export
GET             /export/{job_id}/download       once, within the expiry (step-up)
POST            /import                         full import (job; multipart; step-up)
GET             /import/{job_id}                state and outcome of my import
GET             /audit?filters                 (owner/admin)

GET             /healthz  /readyz              (unauthenticated, proxy-restricted)
```

Export and import notes ([PORTABILITY.md](PORTABILITY.md)): every route
needs a browser session of an owner or admin (`403 session-required` for
tokens, even with the `export` scope); `POST /export`, the download and
`POST /import` also need step-up (`403 step-up-required`). `POST /export`
takes `{"passphrase"?: 12-1024 characters}` and answers `202 {"job_id",
"state", "passphrase_protected"}` (`Idempotency-Key` honoured; the
passphrase is not part of the stored fingerprint; `409 export-in-progress`
while one is pending). `GET /export/{job_id}` -> `{"job_id", "state"
(pending | ready | downloaded | expired | failed), "passphrase_protected",
"size", "sha256" (of the file to download), "created_at", "finished_at",
"expires_at", "downloaded_at", "error", "counts"}`. `GET
/export/{job_id}/download` streams the archive once as an attachment
(`application/zip`, or `application/octet-stream` `.pfarch` when encrypted):
`409 export-not-ready`, `410 export-already-downloaded`, `410
export-expired`. `POST /import` takes `multipart/form-data` with one part
`file` (at most `PERFIN_IMPORT_MAX_MB`, `413`) and, for an encrypted
archive, the `X-Archive-Passphrase` header; `415 unsupported-archive` for
anything else, `422 passphrase-required`, `422 archive-wrong-passphrase`,
`409 import-in-progress`; `202 {"job_id", "state",
"passphrase_protected"}`. `GET /import/{job_id}` -> `{"job_id", "state"
(pending | imported | failed), "passphrase_protected", "created_at",
"finished_at", "error", "error_detail", "summary"}` (`summary`: `created`,
`merged`, `skipped` counts per entity, `import_hash_unrecoverable`,
`archive_created_at`, `archive_base_currency`). An export or import of
another user is `404 export-not-found` / `import-not-found`. `GET
/jobs/{id}` shows these jobs' state to the household's members (sessions;
tokens cannot read them).

Imports notes (structured statements, INGESTION.md): a mapping is `{"id",
"name", "institution_id", "account_id", "header_fingerprint", "spec",
"created_by", "use_count", "last_used_at", "created_at", "updated_at"}`;
`spec` is the column mapping (`date_column`, `date_format` (one of
`YYYY-MM-DD`, `YYYY/MM/DD`, `YYYY.MM.DD`, `YYYYMMDD`, `MM/DD/YYYY`,
`MM/DD/YY`, `MM-DD-YYYY`, `DD/MM/YYYY`, `DD/MM/YY`, `DD-MM-YYYY`,
`DD.MM.YYYY`, `DD.MM.YY`, `DD MMM YYYY`, `DD-MMM-YYYY`, `MMM DD, YYYY`),
either `amount_column` with `sign` (`inflow_positive` | `outflow_positive`)
and optionally `indicator_column` + `outflow_values`, or `debit_column`
and/or `credit_column`; optional `payee_column`, `memo_column`,
`balance_column`, `currency_column` or a fixed `currency`;
`decimal_separator` `.` | `,`; `skip_rows` 0-50; `has_header`). Columns are
zero-based and used once each. `GET /imports/mappings` (cursor, `limit`
1-500) and `GET /imports/mappings/{id}` need any role and hide mappings tied
to an account the caller cannot see (`404 import-mapping-not-found`).
`POST /imports/mappings` (member or above, `Idempotency-Key` honoured) takes
`name`, `spec`, optional `account_id` (must be editable by the caller, else
`404 account-not-found`) and `institution_id` (`404 institution-not-found`),
and exactly one of `document_id` (the fingerprint is computed from that
document's stored table at row `skip_rows`; `422 import-no-table`, `422
import-mapping-mismatch` when the spec names a column the table lacks) or
`header_fingerprint` (64 hex digits); a duplicate name is `409
import-mapping-name-taken`. `PATCH` changes the named fields (`null` clears
`account_id`/`institution_id`; `document_id` or `header_fingerprint` resets
the fingerprint) and `DELETE` removes it; both need `edit` on the mapping's
account when it has one. `GET /imports/{document_id}/preview?offset=&limit=`
(any role that can see the document, `limit` 1-500, default 50) returns
`{"document_id", "status", "kind", "account_id", "tables": [{"index",
"encoding", "delimiter", "has_header", "column_count", "row_count",
"truncated", "offset", "rows", "header_fingerprint"}], "statements":
[{"index", "source_format", "account_type", "last4", "currency",
"institution", "period_start", "period_end", "ledger_balance",
"transaction_count", "truncated", "offset", "transactions"}], "warnings",
"matched_mapping_id", "suggested_spec", "last_import"}` from the stored
sandbox output (never re-parsed). `POST
/imports/{document_id}/apply-mapping` (member or above, `Idempotency-Key`
honoured) takes optional `mapping_id` and `account_id`; the target account
(the given one, the document's, else the mapping's) must be editable (`404
account-not-found`); a document that is not `needs_review`/`processed` or
has no extraction is `409 import-not-applicable`. It answers `202 {"job_id",
"state", "document_id"}`; the `imports.apply_mapping` job re-imports from
the stored extraction (rows already imported are skipped by `import_hash`),
resolves the `unparsed`/`unknown_account` review items it answered, sets
the document `processed` or `needs_review` and queues the transfer matcher.
Tokens need `documents:read` / `documents:write`.

Documents notes (`app.domain.documents.manage`, router `documents`; SECURITY.md
C3, C4, G5, I1): a document is `{"id", "kind", "status", "source", "size",
"mime_detected", "sha256", "original_filename", "uploaded_by", "account_id",
"institution_id", "period_start", "period_end", "tax_year",
"parser_version", "error", "created_at", "updated_at"}`; never the file, the
extraction or extracted text. Owners and admins see every document of the
household; members and viewers the documents of accounts they may view and
their own unattached uploads (anything else `404 document-not-found`). `POST
/documents?account_id=` (member or above) takes `multipart/form-data` with
exactly one part named `file` (`415 unsupported-media-type` otherwise, `422
invalid-upload` for other parts, a second file or a truncated body). The
body is streamed, never spooled: sniffed (`415 unsupported-file-type`),
hashed and encrypted into quarantine as it arrives; the body and the file
are capped at `PERFIN_UPLOAD_MAX_MB` (`413`). `account_id` must be an account
the caller edits (`404 account-not-found`). It answers `202 {"id", "status",
"duplicate_of", "job_id"}` (`job_id` is the `ingest_document` job; `null`
when the same content is already in the household and the document waits as
a `possible_duplicate`). `Idempotency-Key` is honoured with the content's
SHA-256 in the request fingerprint (another file under the same key is `422
idempotency-key-reused`; a replay keeps nothing). `GET
/documents?cursor=&limit=&kind=&status=&account=&tax_year=` lists newest
first (`limit` 1-500, default 100; a hidden `account` is `404`); `q` arrives
in Phase 4. `GET /documents/{id}` adds `links` (`{"id", "document_id",
"target_type", "target_id", "created_at"}`), `open_review_items`,
`quarantined`, `downloadable` and `can_edit`. `GET /documents/{id}/download`
needs member or above, or a viewer in a household whose setting
`documents.viewer_download` is `true` (I1; else `403 forbidden`); a document
whose blob is still in quarantine (received, failed, rejected, a possible
duplicate) is never served (`409 document-quarantined`). The file is
decrypted as it streams with `Content-Type` = the sniffed type (no charset
added), `Content-Length`, `Content-Disposition: attachment` with an ASCII
fallback name and an RFC 5987 `filename*` (control characters, quotes and
separators replaced; `document-<id>.<ext>` without a name), `nosniff`, a
`sandbox` CSP and `no-store` (C4); each download is audited
(`document.download`). A file that fails authentication is `500
document-unreadable`. Changing a document (`POST /documents/{id}/reprocess`,
`POST /documents/{id}/link`, `DELETE /documents/{id}`) needs member or above
and edit on its account (owner/admin always; an unattached document: its
uploader), else `403 forbidden`. Reprocess (`202 {"id", "status",
"job_id"}`) needs a finished document (`409 document-busy`), dismisses its
open review items and queues `ingest_document`. Link takes `{"target_type":
"transaction" | "account", "target_id"}` (other targets `422
link-target-unsupported` until their phases; a target the caller cannot edit
is its `404`), answers `201`, or `200` with the existing link;
`Idempotency-Key` honoured. Delete (`204`) destroys the blob's wrapped key
first, then the rows (links, review items, ingest history), then the files
(G5); imported transactions keep their rows with `source_document_id`
`null`. Events: `document.created|updated|linked|deleted` (ids and status).
`GET /documents/{id}/preview` is Phase 3.

Review notes (`app.domain.review.service`, router `review`; INGESTION.md
"Review queue"): an item is `{"id", "kind", "state", "document_id",
"payload", "resolution", "resolved_by", "resolved_at", "created_at",
"next_step", "can_resolve"}` (`payload` holds ids and short facts written by
the pipeline or importer, never file content; `next_step` is
`confirm_duplicate`, `choose_account`, `column_mapping` or `reprocess`).
`GET /review?state=&kind=&document=&cursor=&limit=` (any role; `state`
defaults to `open`, `all` for every state; oldest first, `limit` 1-500)
lists items of documents the caller can see whose payload `account_id`, if
any, is visible (owner/admin: all; anything else `404
review-item-not-found`). `POST /review/{id}/resolve` and `POST
/review/{id}/dismiss` need member or above who may change the item's
document (`403 forbidden`), on an open item (`409 review-item-closed`).
Resolutions are typed by `kind`, which must match the item (`422
review-kind-mismatch`): `{"kind": "possible_duplicate", "decision": "confirm"
| "reject", "duplicate_of"?}` (same file: confirm marks the new document
`rejected`, reject sends it through the pipeline; imported row: confirm
deletes it and records its `import_hash` for the kept transaction, which
takes the hash when it has none and otherwise gets an `import_hash_alias`;
reject confirms it; `duplicate_of` must be one of the item's `candidates`
and is required when there are several, `422 invalid-resolution`); `{"kind":
"unknown_account", "account_id", "remember"}` (an account the caller edits;
re-imports the stored extraction through `imports.apply_mapping`, or
reprocesses a document without one; `remember` saves the statement's last
four digits on the account, `409 account-last4-conflict` if it records
others, or the account on the item's CSV mapping); `{"kind": "unparsed",
"action": "reprocess"}` or `{"kind": "unparsed", "action": "apply_mapping",
"mapping_id"?, "account_id"?}` (`409 document-not-importable` without a
stored extraction). Other kinds can only be dismissed for now (`409
review-resolution-unsupported`). Closing the last open item of an accepted
`needs_review` document makes it `processed`. Audit `review.resolve` /
`review.dismiss`; events `review.resolved` / `review.dismissed` (`{"id",
"kind", "document_id", "account_id"?}`). Tokens need `review:read` /
`review:write`.

Events notes (`app.domain.events`, router `events`): `GET /events` is
`text/event-stream` for browser sessions only (any household role; a token
is `403 session-required`). Each message is `id: <event id>` and `data:
{"id", "type"}` (unnamed, so `EventSource.onmessage` gets it); payloads
never leave the server, the client refetches. Events of the active
household only, and for members and viewers only those whose payload
`account_id`/`account_ids` names a visible account (or none), that are
addressed to them (`user_id`), and whose document (`document.*` `id`,
`review.*` `document_id`) is visible. `Last-Event-ID` (or
`?last_event_id=`) resumes after that event within the 7-day event
retention; an unknown or expired id gets `data: {"type": "stream.resync"}`
(refetch everything). A comment line (`: keep-alive`) is sent after
`PERFIN_SSE_HEARTBEAT_SECONDS` of silence; the stream ends after
`PERFIN_SSE_MAX_STREAM_SECONDS` (the browser reconnects; `retry: 3000`),
re-checks session, role and grants every `PERFIN_SSE_REVALIDATE_SECONDS`
and ends with `data: {"type": "session.ended"}` when they are gone. At most
`PERFIN_SSE_MAX_STREAMS_PER_USER` concurrent streams per user and API
process (`429 too-many-streams`); `503 events-unavailable` when the
database cannot be listened to.

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

Transactions notes: reading needs any household role (token scope
`transactions:read`), writing at least `member` (`transactions:write`); on
top of that every transaction is filtered by account visibility (owner and
admin: all accounts; member and viewer: accounts granted `view`/`edit`,
writes need `edit` and viewers never write). A transaction on a hidden
account, of another household or missing is `404 transaction-not-found`; an
account the caller cannot see, named in a filter or body, is `404
account-not-found`; a payee, category or tag that is not the household's is
`404 payee-|category-|tag-not-found`. `GET /transactions` takes `account`,
`category`, `payee`, `tag` (repeatable, at most 50 values each),
`date_from`/`date_to` (posted date, inclusive), `uncategorized`,
`review_state`, `status`, `transfer` (`true`: only transfer legs) and `q`
(1-100 characters: a case-insensitive substring of the payee text, the
payee's name, memo or notes; `%`, `_` and `\` are literal characters, there
is no query language). A `category` matches the transaction's category or
any of its splits'. Results are newest first (`posted_date`, then id) with a
keyset cursor: `{"items", "next_cursor"}`, `limit` 1-500 (default 100); rows
added or removed while a client pages never shift later pages; a malformed
cursor is `422 invalid-cursor`. A transaction is `{"id", "account_id",
"posted_date", "effective_date", "amount": {"amount", "currency"},
"payee_id", "category_id", "raw_payee", "memo", "notes", "status",
"review_state", "confidence", "transfer": {"group_id", "counterpart_id"} |
null, "source_document_id", "tag_ids", "splits": [{"id", "category_id",
"amount", "memo", "tax_flag"}], "created_at", "updated_at"}`;
`counterpart_id` is null when the other leg is on an account the caller
cannot see. `POST /transactions` (`Idempotency-Key` honoured) takes
`account_id`, `posted_date`, `amount` (`{"amount": "<decimal string>",
"currency"}`, the account's currency or `422 currency-mismatch`) and
optionally `effective_date`, `payee_id`, `category_id`, `raw_payee`, `memo`,
`notes`, `status`, `tag_ids`; manual transactions start with `review_state`
`none` and then go through the household's enabled rules in the same
request (the response shows their effect); rules never overwrite the
category, payee, notes or tags given in the request. `PATCH` takes the same fields (not `account_id`) plus
`review_state`; an absent field is unchanged, `null` clears an optional one,
`tag_ids` replaces the tag set. `POST /transactions/{id}/split` takes
`{"splits": [{"amount", "category_id"?, "memo"?, "tax_flag"?}, ...]}` with
either no lines (remove the splits; optional `category_id` for the
transaction) or at least two whose amounts, in the transaction's currency,
add up exactly to its amount (`422 split-sum-mismatch`); a split
transaction's category is null and its amount and category cannot be
patched (`409 transaction-split`). `POST /transactions/bulk` takes
`{"action": "categorise" | "tag" | "untag" | "delete" | "confirm", "ids"
(1-500, unique), "category_id"? (categorise; null clears), "tag_ids"? (tag,
untag)}` and authorises every id on its own: the answer lists
`{"id", "result": "updated" | "deleted" | "unchanged" | "skipped" |
"not_found", "reason"?}` per id (`skipped` with reason `split` for split
transactions in a categorise), and ids the caller may not edit are
`not_found` and untouched. Transfers are two transactions sharing a
`group_id`: `POST /transactions/{id}/transfer` (`{"counterpart_id"}`) links
two legs on different accounts the caller may edit, of opposite sign and,
in one currency, equal and opposite amounts (`422 transfer-invalid`; `409
transfer-conflict` when a leg already belongs to another transfer); `DELETE
/transactions/{id}/transfer` dissolves the transfer (`204`); a leg keeps its
amount while linked (`409 transfer-conflict`) and deleting a leg unlinks the
other. `GET /transactions/{id}/transfer-candidates?window_days=` (member and
above) lists unlinked equal-and-opposite transactions on other editable
accounts within the window (default 3 days, at most 14), closest first.
`POST /transactions/transfers/auto-pair` (`{"account_ids"?, "date_from"?,
"date_to"?, "window_days"?}`) pairs unlinked transfers among the caller's
editable accounts and returns `{"paired": [{"group_id", "transaction_ids"}],
"ambiguous"}` (rules in DATA_MODEL.md "Ledger"); the worker runs the same
matcher over the whole household after every import. Deleting a
transaction keeps its audit row (`transaction.delete`, with its amounts,
dates and ids but no free text). Every write emits a `transaction.*` event.

Rules notes (`app.domain.rules`, router `rules`; document format and
evaluation semantics in DATA_MODEL.md "Ledger"): any household role reads
(`transactions:read`); creating, changing, deleting, reordering, importing
and applying need owner or admin (`transactions:write`), because rules
rewrite transactions on every account. A rule is `{"id", "name",
"sort_order", "enabled", "stop_processing", "conditions", "actions",
"hit_count", "last_hit_at", "created_at", "updated_at", "redacted"}`; for a
reader who does not see every account, hidden account ids in conditions are
the nil UUID and `redacted` is `true`. `GET /rules` returns `{"items"}` in
evaluation order (at most 500 rules, `409 rule-limit` beyond). `POST /rules`
(`Idempotency-Key` honoured) takes `name`, `conditions`, `actions`,
`enabled`, `stop_processing` and appends the rule; `PATCH` takes any of them
(`null` is refused). Ids named by a rule must be the household's (`404
category-|payee-|tag-|account-not-found`); a missing rule is `404
rule-not-found`. `POST /rules/reorder` takes `{"rule_ids"}`, every rule
exactly once (`422 rule-order-mismatch`). `POST /rules/test` takes
`rule_id` (one saved rule, also a disabled one) or `rule` (an unsaved
`{"conditions", "actions", "stop_processing"}`) or neither (the enabled
rule set), plus `account_ids`, `date_from`, `date_to`, `only_changes`,
`cursor`, `limit` (1-500, default 100); it examines at most 5000 of the
caller's visible transactions per call, newest first, and returns
`{"items": [{"transaction", "matched_rule_ids", "changes": {"category_id",
"splits", "payee_id", "notes", "review_state", "added_tag_ids"},
"skipped": [{"rule_id", "action", "reason"}]}], "next_cursor", "scanned",
"matched"}` (`next_cursor` null once history is exhausted). It never writes:
its database transaction is switched to `READ ONLY` first. `POST
/rules/apply` (`{"rule_ids"?, "account_ids"?, "date_from"?, "date_to"?}`,
`Idempotency-Key` honoured) answers `202 {"job_id", "state"}`; the
`run_rules` job applies the enabled rules (among `rule_ids`) to the whole
household and emits `transaction.rules_applied`. `GET /rules/export`
returns `{"format": "perfin-rules", "version": 1, "rules": [...],
"references": {"categories", "payees", "tags", "accounts": [{"id",
"name"}]}}` (category names are paths, `"Food / Groceries"`); `POST
/rules/import` takes that document plus `mode` (`append` or `replace`): an
id that is not this household's is resolved by its referenced name (case
and spacing ignored; payees by their normalised name), else `422
rule-reference-unresolved` and nothing is imported.

Jobs notes (`app.domain.jobs.service`, router `jobs`): `GET /jobs/{id}`
returns `{"id", "type", "state", "attempts", "max_attempts", "finished",
"created_at", "updated_at"}` for a job of the caller's active household
(any role; never the payload or error text); anything else is `404
job-not-found`. Tokens need the job family's read scope (`run_rules` and
`transactions.auto_pair`: `transactions:read`; `ingest_document` and
`imports.apply_mapping`: `documents:read`; other families, such as
`portability.*`, are not readable by tokens). There is no
progress counter yet beyond `state`.

Accounts notes (`app.domain.accounts.service`, router `accounts`): any
household role reads, member and above create and change, owner and admin
delete (token scopes `accounts:read` / `accounts:write`). Members and
viewers only see the accounts granted to them (`view` or `edit`; viewers
never write), owners and admins see all; a hidden account, another
household's or a missing one is `404 account-not-found` on every account
route, and an account the caller can see but not edit is `403 forbidden`.
A member who creates an account gets an `edit` grant on it. `GET
/accounts?cursor=&limit=&closed=&type=` lists `{"items", "next_cursor"}` in
creation order (`limit` 1-500, default 100). An account is `{"id", "type",
"asset_kind", "name", "nickname", "currency", "institution_id",
"number_last4", "is_asset", "manual_balance", "include_in_net_worth",
"opened_on", "closed_on", "is_closed", "balance": {"amount", "currency"},
"balance_date", "can_edit", "created_at", "updated_at"}` (`balance` at the
end of today, UTC). `POST /accounts` (`Idempotency-Key` honoured) takes
`type`, `name` and optionally `nickname`, `currency` (default the
household's base currency), `institution_id` (`404 institution-not-found`),
`asset_kind` (required for and only for `manual_asset`), `number_last4`
(four digits), `is_asset`/`manual_balance` (default from the type),
`include_in_net_worth`, `opened_on`, `opening_balance` (`{"amount",
"currency"}` in the account currency, `422 currency-mismatch`) and
`opening_balance_date` (default `opened_on`, else today). `PATCH` takes
the same fields except the opening balance; `null` clears `nickname`,
`institution_id`, `number_last4` and `opened_on`; a type change resets
`is_asset`/`manual_balance` to the new type's defaults unless given;
`currency` changes only while the account has no transactions, balances or
schedules (`409 account-currency-locked`). `POST /accounts/{id}/close`
(`{"closed_on"?}`, not in the future, not before `opened_on`: `422
invalid-account-dates`; `409 account-closed`) and `/reopen` (`409
account-not-closed`). `DELETE` removes the account with its transactions,
balances, schedules and grants. `GET /accounts/{id}/balances?start=&end=`
returns `{"account_id", "currency", "start", "end", "items": [{"date",
"balance"}], "snapshots": [{"id", "as_of_date", "balance", "source",
...}]}`: end-of-day balances (DATA_MODEL.md "Balances"), default the 90 days
up to today, at most 3660 days (`422 range-too-long`, `422 invalid-range`).
`POST /accounts/{id}/balances` (`{"as_of_date", "balance": {"amount",
"currency"}}`, `Idempotency-Key` honoured) records or replaces the manual
balance of that date (`422 balance-in-future`). `number_last4` is returned
to callers who may see the account but never written to the audit trail or
events. Events: `account.created|updated|closed|reopened|deleted`,
`account.balance_recorded`, with ids only.

Account grant notes (router `account_grants`; AUTH.md): `GET
/households/{id}/members/{user_id}/grants` lists `{"id", "user_id",
"account_id", "level", "updated_at"}`; owners and admins read anyone's,
members and viewers only their own (`403`); tokens need `households:read`.
`PUT .../grants/{account_id}` (`{"level": "none" | "view" | "edit"}`) and
`DELETE` (`204`, also when there was no grant) need owner or admin on a
browser session with step-up (`403 step-up-required`); owners and admins
get no grants (`422 grant-not-applicable`), viewers at most `view` (`422
grant-level-not-allowed`); an unknown member is `404 member-not-found`, an
account not of the household `404 account-not-found`. `GET
/accounts/{id}/grants` lists every grant on one account (owner/admin,
`accounts:read`). Changes are audited (`account_grant.set|revoke`), emit
`account_grant.changed` and apply from the next request.

Categories, payees and tags notes (`app.domain.taxonomy.service`): any role
reads, member and above create and change, owner and admin delete and merge
(those rewrite transactions on every account); token scopes
`transactions:read` / `transactions:write`. Lists are `{"items",
"next_cursor"}` in creation order (`limit` 1-500; default 500 for
categories, 100 otherwise). Objects of another household are `404
category-|payee-|tag-not-found`, in paths and in bodies. Categories
(`{"id", "parent_id", "name", "kind", "tax_treatment", "system_key",
"created_at", "updated_at"}`; `?kind=` filter): at most three levels (`422
category-too-deep`), no cycles (`422 category-cycle`), sibling names unique
ignoring case (`409 category-name-taken`). A child has its parent's `kind`
(`422 category-kind-mismatch` otherwise); a top-level category defaults to
`expense`. `PATCH` renames, moves (`parent_id`, `null` for top level; the
moved subtree takes the new parent's kind), changes `kind` (a top-level
change cascades to the descendants) and `tax_treatment`. `DELETE` refuses a
category with subcategories (`409 category-has-children`) and clears it from
transactions, splits, schedules and payee defaults; its budget lines go.
`POST /categories/{id}/merge` (`{"into"}`, same kind, not into itself or a
descendant: `422 category-merge-invalid`) moves transactions, splits,
schedules, payee defaults and subcategories to `into`, adds the budget lines
into `into`'s line of the same budget, and deletes the source. Payees
(`{"id", "name", "normalized_name", "aliases", "default_category_id", ...}`;
`?q=` matches inside the normalised name): the normalised name is unique
(`409 payee-exists`, `422 invalid-payee-name` when it has no letter or
digit); aliases are statement spellings (at most 100, deduplicated by their
normalised form) and no alias or name may belong to another payee (`409
payee-alias-taken`). `PATCH` replaces `aliases` as a whole. `POST
/payees/{id}/merge` moves transactions and schedules to `into`, adds the
source's name and aliases to its aliases, keeps `into`'s default category
(the source's when it has none) and deletes the source. Tags: names unique
ignoring case (`409 tag-exists`); deleting one untags its transactions.
Events `category.changed`, `payee.changed`, `tag.changed` (ids only).

Budgets notes (`app.domain.budgets`, router `budgets`): any role reads,
member and above create, change and delete (token scopes `budgets:read` /
`budgets:write`). A budget is a household-wide plan; its report's actuals
come only from the accounts the caller may see. `GET
/budgets?cursor=&limit=` lists `{"items", "next_cursor"}` in creation order
(`limit` 1-500, default 100). A budget is `{"id", "name", "period":
"monthly" | "annual" | "custom", "start_date", "end_date", "currency",
"rollover", "lines": [{"id", "category_id", "amount"}], "created_at",
"updated_at"}`. `POST /budgets` (`Idempotency-Key` honoured) takes `name`,
`period`, `start_date`, optional `end_date`, `currency` (default the
household's base currency), `rollover` (default `false`) and `lines`
(`[{"category_id", "amount": {"amount", "currency"}}]`, at most 500, one per
category). Monthly and annual budgets start on the first day of a month and
have no end (periods: calendar months, or twelve months from the start); a
custom budget is one period `start_date..end_date`, at most 3660 days (`422
invalid-budget-dates`). Lines: expense or income categories of the household
(`404 category-not-found`, `422 budget-category-kind`), amounts `>= 0`
(`422 invalid-budget-amount`) in the budget's currency (`422
currency-mismatch`). `PATCH` takes the same fields; `lines` replaces the
whole set; a currency change needs `lines` in the same request (`422
budget-lines-required`). Another household's or a missing budget is `404
budget-not-found`. `GET /budgets/{id}/report?period=&include_pending=`
(`period` `YYYY-MM` or `YYYY-MM-DD`, default today; `422 invalid-period`,
`422 period-out-of-range`) returns `{"budget_id", "name", "currency",
"as_of", "period": {"start", "end", "state": "past" | "current" |
"future"}, "include_pending", "lines": [{"category_id", "name", "kind",
"budgeted", "carried_over", "available", "actual", "remaining",
"forecast"}], "unbudgeted": [{"category_id", "name", "actual"}],
"unbudgeted_total", "expense_totals", "income_totals", "unconverted"}`. A
line counts its category and the subcategories without a line of their
own; expense amounts are money out, income lines money in; amounts in
other currencies are converted into the budget's currency with the rate of
each posting day, and sums without a rate are listed in `unconverted` in
their own currency instead. Rollover (expense lines of monthly and annual
budgets) carries `available - actual` into the next period, at most ten
years back. Forecast: past periods their actual; the current period `actual
+ (actual not matching an active schedule) * remaining_days / elapsed_days +
scheduled occurrences until the period end`; future periods `actual +
scheduled` (rules in `app/domain/budgets/reports.py`). Events
`budget.created|updated|deleted` (ids only).

Goals notes (`app.domain.goals`, router `goals`; scopes `budgets:read` /
`budgets:write`): any role reads, member and above writes; a goal is visible
only to callers who may see every one of its accounts and every account of
a transaction linked to one of its contributions (otherwise `404
goal-not-found`, like another household's). A goal is `{"id", "name",
"target", "target_date", "account_ids", "archived", "achieved_at",
"progress": {"as_of", "contributed", "remaining", "percent",
"accounts_balance", "monthly_rate", "projected_completion", "on_track"},
"created_at", "updated_at"}`. `GET /goals?cursor=&limit=&archived=`; `POST
/goals` (`Idempotency-Key` honoured) takes `name`, `target` (`> 0`, `422
invalid-goal-target`; sets the currency), `target_date`, `account_ids` (at
most 50, visible to the caller: `404 account-not-found`); `PATCH` takes the
same plus `archived` (`account_ids` replaces the set; the currency changes
only without contributions, `409 goal-currency-locked`). Contributions
(`{"id", "goal_id", "contributed_on", "amount", "transaction_id", "memo",
"created_at"}`): `POST /goals/{id}/contributions` (`Idempotency-Key`
honoured) takes `amount` (goal currency, non-zero; negative is a
withdrawal), `contributed_on` (default today, not in the future: `422
contribution-in-future`), `transaction_id` (a visible transaction on one of
the goal's accounts, `422 contribution-transaction-invalid`, once per goal,
`409 contribution-exists`; without `amount` its amount converted on its
posted date) and `memo`; `DELETE .../contributions/{contribution_id}`
(`404 contribution-not-found`). Progress: `percent` two decimals; the
observed rate is the contributions of the last 180 days (at least a 30-day
window, not before the first contribution), `monthly_rate = daily * 365.25
/ 12`; `projected_completion` the day the target was reached, or today plus
`remaining / daily` days; `on_track` compares it with `target_date`
(`app/domain/goals/progress.py`). Events `goal.created|updated|deleted`.

Reports notes (`app.domain.reports`, router `reports`; any role, scope
`transactions:read`): amounts come only from accounts the caller may see,
read splits instead of split transactions, leave out transfer legs and
`transfer`/`ignore` categories, and leave out `review_state = pending` rows
unless `include_pending=true` (default: the household setting
`reports.include_pending_review`). Everything is in the household's base
currency, converted with the rate of each posting day; sums without a rate
are listed in `unconverted` in their own currency. `account` (repeatable, at
most 50; `404 account-not-found` for one the caller cannot see) filters every
report. Ranges are at most 3660 days (`422 range-too-long`, `422
invalid-range`). `GET /reports/spending` and `/reports/income`
(`date_from`/`date_to`, default the current month; `group_by` `category`
(default) | `payee` | `month` | `account`; `depth` 1-3 rolls categories up
to that level) return `{"flow", "currency", "date_from", "date_to",
"group_by", "depth", "include_pending", "items": [{"key", "name",
"amount"}], "total", "unconverted"}`: spending is expense categories plus
uncategorised money out, shown positive (refunds reduce it); income is
income categories plus uncategorised money in; `key` is the id (or
`YYYY-MM`), `null` for uncategorised or no payee; months are listed even
when empty. `GET /reports/trends?flow=&month=&months=` (`month` `YYYY-MM`,
default this month; `months` 1-60, default 12) returns the monthly `series`
and, for the total and each category (`depth`), `amount`, `previous`,
`mom_change`, `mom_percent`, `year_ago`, `yoy_change`, `yoy_percent`
(percent strings with two decimals, `null` when the base is zero). `GET
/reports/cashflow` (default this month and the eleven before) returns per
month `inflow`, `outflow` (both positive) and `net`, plus the totals.

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

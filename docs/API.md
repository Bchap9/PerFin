# API conventions and surface

Base path `/api/v1`. JSON request and response bodies. Browser clients
authenticate with the session cookie plus CSRF header; scripts use
`Authorization: Bearer <personal access token>`.

## Conventions
- UUIDv7 identifiers. Timestamps in RFC 3339 UTC; dates as `YYYY-MM-DD`.
  Business dates a client supplies (investment trade/settle dates, prices,
  loan origination, payment, rate-change and simulation dates, asset
  valuations, FX rates, recurring due dates) are never before `1900-01-01`
  (`422` request validation; `app/core/dates.py`, SECURITY.md F8); the
  database refuses older rows too and importers and feeds hold or drop them.
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
  retried with its key. A user's stored answers are dropped when their
  account grants or role change, so a replay never returns what they may
  no longer see (the request runs again). The Phase 1 creating `POST`s (`/households`,
  `/households/{id}/members`, `/admin/users`) ignore it; `POST /auth/tokens`
  never will (a replay could not return the secret).
- Read your writes: a request's database transaction commits before its
  response is sent (`DbSession` in `app/auth/deps.py` is a
  `scope="function"` dependency), so a `2xx` means the change is stored and
  the client's next request sees it; a failed commit is a `500`, never a
  success (`tests/integration/test_commit_before_response.py`).
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
answers `{"setup_required": false}`.

Phase 2 (v0.2) implements, as listed below: `/accounts` (with balances,
close/reopen and grants), `/transactions` (splits, transfers, bulk),
`/categories`, `/payees`, `/tags`, `/rules` (reorder, test, apply, export,
import), `/jobs/{id}`, `/budgets` and their report, `/goals` and
contributions, `/reports/*`, `/documents`, `/imports/*`, `/review`, `/events`, `/export`
and `/import`. Phases 3-4 (v0.3, v0.4) add the document routes `preview`
and `regions`, the full-text `q` on `GET /documents`, the review tools
(correct, confirm held rows, remember this layout, rule draft, unknown
owner) and `/settings/intake` (mailbox settings, connection test, alias
rotation); the drop folder and mailbox intake are worker jobs, not routes. It also adds `POST /transactions/transfers/auto-pair` and the
grant routes. Not built yet among the Phase 2 families: `GET/PATCH
/settings/household` (so the household settings `reports.include_pending_review`
and `documents.viewer_download` have their defaults and no API) and `GET
/audit`. Phases 5-6 (v0.5, v0.6) add `/securities`, `/investments/*`,
`/fx-rates`, `/loans`, `/assets`, `/networth`, `/recurring`,
`/cashflow/calendar`, `/tax-years` and `/tax-category-map` (notes below);
the Phase 7+ routes in the list are not built yet. The Phase 2 routes, and
the Phase 5-6 routes for brokerage, loan and tax-form uploads, holdings,
performance, loan payments, schedules and simulation, manual assets, net
worth, recurring detection, the calendar, the tax checklist and the
accountant export, were run end to end against a live `perfin api` and
`perfin worker` on a freshly migrated database (CHANGELOG "Resume here").

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
GET/POST        /securities/{id}/prices         manual closing prices
DELETE          /securities/{id}/prices/{price_id}
GET/POST        /fx-rates                       rate history; manual rate of a pair and day
DELETE          /fx-rates/{id}
GET/POST/PATCH/DELETE /investments/transactions
GET             /investments/holdings?account=
GET             /investments/performance?range=&account=
GET/PUT         /investments/accounts/{id}/cost-method   fifo | average

GET/POST/PATCH/DELETE /loans                   terms of a loan/mortgage/line-of-credit account
GET             /loans/{id}/schedule?basis=     original | projected (from today)
POST            /loans/{id}/simulate            extra payments what-if (stores nothing)
GET/POST        /loans/{id}/payments            payment splits, checked against the schedule
DELETE          /loans/{id}/payments/{payment_id}
GET/POST        /loans/{id}/rate-changes
DELETE          /loans/{id}/rate-changes/{change_id}
GET/POST/PATCH/DELETE /assets                  manual assets (property, vehicle, other)
GET/POST        /assets/{id}/valuations         valuation history; manual valuation
DELETE          /assets/{id}/valuations/{valuation_id}

GET             /networth?range=&granularity=   history (stored snapshots or recomputed)
GET             /networth/latest
GET             /networth/projection?months=&basis_months=   linear trend and savings rate

GET/POST        /recurring                      recurring transactions (detected and manual)
GET             /recurring/candidates           detection dry run (nothing stored)
POST            /recurring/detect               detect and store (member or above)
GET/PATCH/DELETE /recurring/{id}
GET             /cashflow/calendar?days=&account=   expected income and bills, day by day

POST            /documents                     multipart upload (streamed) -> 202
GET             /documents                     filters: kind, status, account, tax_year, q (full text)
GET             /documents/{id}
GET             /documents/{id}/download        decrypts and streams, attachment
GET             /documents/{id}/preview?page=   rendered PNG, sandboxed
GET             /documents/{id}/regions?page=   word boxes for review highlighting
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
POST            /review/{id}/correct            document facts and row values
POST            /review/{id}/remember-layout    household parser fingerprint, reprocess similar
GET             /review/{id}/rule-draft         pre-filled rule for POST /rules

GET/POST        /tax-years
GET/PATCH/DELETE /tax-years/{year}
GET/POST        /tax-years/{year}/checklist
POST            /tax-years/{year}/checklist/learn
PATCH/DELETE    /tax-years/{year}/checklist/{item_id}
GET             /tax-years/{year}/forms         amounts by box (no identity data)
GET             /tax-years/{year}/summary       roll-up by category and schedule hint
POST            /tax-years/{year}/export        accountant export (job; 202 + job id)
GET             /tax-years/{year}/export/{job_id}
GET             /tax-years/{year}/export/{job_id}/download   zip, once
GET             /tax-category-map
PUT/DELETE      /tax-category-map/{category_id}

GET             /events                         SSE stream
GET             /jobs/{id}

GET/PATCH       /settings/household            (not built yet)
GET/PATCH       /settings/user
GET/PATCH       /settings/system               (instance admin; OIDC, intake, integrations)
GET/POST        /admin/users                   (instance admin; POST step-up)
GET/PATCH       /admin/users/{id}              (instance admin; PATCH step-up)
POST            /admin/users/{id}/mfa-reset    (instance admin, step-up)
GET/PATCH       /settings/intake               receipts mailbox (owner/admin session; PATCH step-up)
POST            /settings/intake/email/test
POST            /settings/intake/email/rotate-alias  (step-up)
GET/POST/PATCH/DELETE /settings/notifications/channels

GET/PATCH       /settings/ai                    household AI flag + live client count (owner; PATCH step-up)
GET/POST        /settings/ai/clients            MCP clients (owner session; POST step-up, token once)
GET/DELETE      /settings/ai/clients/{id}       one client; DELETE revokes now (step-up)
GET             /settings/ai/clients/{id}/preview  exposure preview with the saved options
POST            /settings/ai/preview            exposure preview with chosen options, before saving
GET             /settings/ai/calls              audit of MCP calls, searchable (?client_id&contains&limit)
GET/POST        /settings/ai/scrubber-preview   my free text: original vs scrubbed (owner)
GET/PATCH       /settings/system/ai             instance AI kill switch (instance admin; PATCH step-up)
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
extraction or extracted text. A document whose extraction failed has
`status: "failed"` and `error: "extraction failed: <code>"` with the
sandbox code (`timeout`, `cpu_limit`, `memory_limit`, `output_too_large`,
`bad_output`, `harden_failed`, `killed`, `syscall_denied` (the child made a
syscall outside its seccomp allowlist), `crashed`, `usage`, `input_too_large`) and an
`unparsed` review item. Owners and admins see every document of the
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
first (`limit` 1-500, default 100; a hidden `account` is `404`). With `q`
(1-200 characters; `app.domain.documents.search`) only documents whose
extracted text matches are listed, best match first: the text is a bound
parameter to `websearch_to_tsquery('simple', :q)` (quoted phrases, `or`,
`-word`; never a syntax error) against `document.extracted_text`, ranked by
`ts_rank_cd`; the keyset cursor is `<score>.<id>` (a cursor from the other
ordering is `422 invalid-cursor`). Each item then carries `rank` and
`snippet`: a list of plain-text segments `{"text", "match"}` (at most 400
characters, two fragments; control and bidi characters replaced) that the
client renders as text, never as markup. For callers who may not download
the document (a viewer, unless the household setting
`documents.viewer_download` is `true`: the same I1 rule as the download)
`q` never touches the extracted text: it matches and ranks only the
`original_filename` (split into words at punctuation), and `snippet` is
`null`. Otherwise every query would tell them whether a token (an account
number, a tax id, a salary) appears in a document they may not open. Without `q`, `rank` and
`snippet` are `null`. `GET /documents/{id}` adds `links` (`{"id", "document_id",
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
`GET /documents/{id}/preview?page=` (default page 1) returns the
sandbox-rendered PNG of that page (decrypted from its preview blob and
checked again: plain PNG chunks only) as `image/png`, `Content-Disposition:
inline`, `nosniff`, a `sandbox` CSP and `no-store`; it follows the download
rule (member or above, a viewer only with `documents.viewer_download`, else
`403 forbidden`), a quarantined document is `409 document-quarantined`, a
page without a preview `404 preview-not-found`, a blob that fails
authentication or is not a plain PNG `500 document-unreadable`; audited as
`document.preview`. `GET /documents/{id}/regions?page=` (any role that can
see the document) returns `{"document_id", "page", "page_count", "width",
"height", "unit" (pt | px), "text_source", "text_included",
"words_truncated", "preview": {"width", "height", "dpi", "scale"} | null,
"words": [{"bbox", "line", "text", "conf"}]}` from the stored extraction
(boxes in page units, origin top left; `px = unit * scale`); for callers
the download rule refuses `text_included` is `false` and `words` is empty
(per-word boxes would show the layout, such as how many digits a figure
has, of a document they may not open);
`404 page-not-found` without that page, `409 document-quarantined`.

Review notes (`app.domain.review.service`, router `review`; INGESTION.md
"Review queue"): an item is `{"id", "kind", "state", "document_id",
"payload", "resolution", "resolved_by", "resolved_at", "created_at",
"next_step", "can_resolve"}` (`payload` holds ids and short facts written by
the pipeline or importer, never file content; `next_step` is
`confirm_duplicate`, `choose_account`, `column_mapping`, `reprocess`,
`choose_owner`, `attach_receipt` or `correct_fields`; `GET /review/{id}`
also returns `regions` (`[{"page", "bbox", "label": "transaction" |
"account_number" | "page", "transaction_id"?, "fields"}]`) and
`alternatives` (`[{"field", "value", "label", "score", "transaction_id",
"source": "document" | "ledger" | "household"}]`; values read from the
document only for callers the download rule allows), both `null` in lists).
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
stored extraction); `{"kind": "unknown_owner", "user_id", "account_id"?,
"remember"}` (a held drop-folder file: assigned to that member, admin or
owner and the account, which the caller must edit and the member may import
into, `422 invalid-resolution` otherwise; the document goes through the
pipeline; `remember` routes the item's `folder` to them; `next_step` is
`choose_owner`); `{"kind": "receipt_match", "transaction_id"}` ("attach this
receipt to ...": payload `reason` and ranked `suggestions` of
`{transaction_id, account_id, score, amount, days, already_linked}`;
attaches the receipt to that transaction, which need not be a suggestion,
with the checks of `POST /documents/{id}/links`: a transaction the caller
cannot edit is `404 transaction-not-found`; `"transaction_id": null`
records that none applies; `next_step` is `attach_receipt`; INGESTION.md
"Receipts"); `{"kind": "low_confidence_field", "transactions": [{"transaction_id",
"posted_date"?, "amount"? (`{"amount", "currency"}`), "raw_payee"?,
"memo"?}], "confirm"}` (corrects rows imported from the item's document,
each one the caller edits, else `422 invalid-resolution`; with `confirm`,
default `true`, the item's pending rows become `confirmed`, for a
`reconciliation_failed`/`reconciliation_unavailable` item every pending row
of the document on the item's account; audited as `transaction.update`).
Other kinds can only be dismissed for now (`409
review-resolution-unsupported`). Review tools, same callers and open-item
rule as resolve unless noted: `POST /review/{id}/correct` `{"document"?:
{"institution_id"?, "account_id"?, "period_start"?, "period_end"?,
"tax_year"?, "kind"?}, "transactions": [...as above], "resolve"}` (an
institution of the household, `404 institution-not-found`; an account the
caller edits; `null` clears institution, period and tax year; a period that
ends before it starts is `422`; audit `document.correct`; `resolve` closes
the item) answers the item. `POST /review/{id}/remember-layout`
`{"institution_id", "kind"?, "parser"?, "match"?, "reprocess_similar"}`
stores a household `parser_fingerprint` (`match` has the shipped
fingerprints' schema, derived from the document's header lines and table
layout when absent): `409 layout-unavailable` without extracted pages, `422
kind-required`, `422 parser-unknown`, `422` for rules outside the schema or
with an unsafe regex, `422 layout-not-distinctive`, `422 layout-does-not-match`
(the rules must recognise this document), `422 layout-too-costly` (the rules
did not finish within the household matching budget on this document), `409
fingerprint-limit` (the household already remembers 100 layouts, or the
layout 10 000 versions); it resolves the item, reprocesses the document and
up to 50 `needs_review` documents the caller may change whose stored layout
matches (the scan of up to 500 of them stops when its 2 s matching budget is
spent), and answers `{"fingerprint_id", "version",
"institution_id", "kind", "parser", "match", "ambiguous", "reprocessed",
"item"}` (audit `parser_fingerprint.create`). `GET /review/{id}/rule-draft`
(owner or admin, who may create rules; any item state) answers
`{"item_id", "transaction_id", "rule"}` with a rule body for `POST /rules`
pre-filled from the first row the item names that the caller can see (`409
rule-draft-unavailable` without one or without a payee). Closing the last open item of an accepted
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
`account_id`/`account_ids` names a visible account (or none), whose
`all_account_ids` (goals) are all visible, that are addressed to them
(`user_id`), and whose document (`document.*` `id`, `review.*`
`document_id`) is visible; `transaction.*`, `balance.*` and `goal.*` events
that name no account are not sent to them, and `document.deleted` without an
account only to the uploader. `Last-Event-ID` (or
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
matcher after every import over the accounts the document's uploader may
import into (all of them for an owner's or admin's document). Unlinking, or
deleting a leg, needs edit on every leg of the transfer (`409
transfer-conflict` otherwise). Deleting a
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

Investments notes (`app.domain.investments`, routers `securities` and
`investments`; scopes `investments:read` / `investments:write`; any role
reads, member and above writes; DATA_MODEL.md "Investments"). Quantities,
unit prices and split ratios are decimal **strings** (at most 18 + 10
digits, never JSON numbers) and come back without trailing zeros; rates are
fractions rounded half-even to 10 decimals (`"0.2100000000"` = 21 %).

- **Securities** (`{"id", "symbol", "name", "asset_class", "currency",
  "isin", "cusip", "sector", "exchange", "created_at", "updated_at"}`): the
  household's catalogue. Owners and admins see all of it; members and
  viewers see securities with activity on an account they can see, or with
  none at all (a security used only on hidden accounts is `404
  security-not-found`, like another household's). `GET
  /securities?q=&asset_class=&cursor=&limit=` (by symbol); `POST`
  (`Idempotency-Key` honoured; symbol and exchange unique, `409
  security-exists`; ISIN/CUSIP format-checked and upper-cased); `PATCH`
  (currency only while there are no prices or transactions, `409
  security-currency-locked`); `DELETE` (`409 security-in-use` with
  transactions or lots; prices go with it). Prices: `GET
  /securities/{id}/prices?from=&to=` (newest first), `POST` `{"price_date",
  "close"}` sets that day's close (`source` `manual`, replaces the day's
  price; not in the future, `422 price-date-in-future`), `DELETE
  .../prices/{price_id}` (`404 price-not-found`). Events `security.*`
  (security id only) reach only callers who see every account.
- **Transactions** (`{"id", "account_id", "security_id", "type",
  "trade_date", "settle_date", "quantity", "price", "ratio", "amount",
  "fees", "lots", "lots_specified", "memo", "source_document_id",
  "created_at", "updated_at"}`): `GET
  /investments/transactions?account=&security=&type=&from=&to=&cursor=&limit=`
  (newest trade date first; visible accounts only); `POST` (`Idempotency-Key`
  honoured) takes `account_id` (a brokerage or retirement account the
  caller may edit: `404 account-not-found`, `422 not-an-investment-account`),
  `type`, `trade_date` (not in the future, `422 trade-date-in-future`),
  `settle_date`, `security_id`, `quantity`, `price` (security currency),
  `amount` and `fees` (account currency, `422 currency-mismatch`), `ratio`
  (`{"new", "old"}`, splits and corporate actions), `lots` (specific lots
  of a sale or transfer out: `[{"lot_id", "quantity"}]`, `fifo` accounts
  only) and `memo`; the rules per type are in
  `app/domain/investments/transactions.py` (`422
  invalid-investment-transaction`). `PATCH` takes the same fields except
  `account_id` (`"lots": null` returns a sale to the cost method);
  `DELETE`. Every write replays the lots of the (account, security) pairs
  it touches; what the lots cannot satisfy is `422` (`409` for a delete)
  `insufficient-quantity`, `lot-not-available`, `lot-links-mismatch` or
  `lot-selection-not-allowed`, and `409 lots-not-replayable` when units
  that came from a statement precede a split recorded as units added.
  Transactions on hidden accounts are `404
  investment-transaction-not-found`; on accounts the caller may only view,
  writes are `404` too. Events `investment.txn_created|txn_updated|txn_deleted`
  carry the account id.
- **Cost method**: `GET /investments/accounts/{id}/cost-method` (any role, a
  visible account), `PUT` `{"method": "fifo"|"average"}` (an account the
  caller may edit) replays every lot of the account (`422
  lot-selection-not-allowed` while a sale names its lots). Event
  `investment.cost_method_changed`.
- **Holdings** (`GET /investments/holdings?account=`): as of today, per
  account and security `{"account_id", "security", "quantity",
  "cost_basis", "price": {"close", "currency", "price_date", "basis":
  "price"|"trade"}, "market_value", "market_value_base", "unrealized":
  {"short_term", "long_term", "total"}, "weight", "lots": [...]}`, plus
  `accounts` (cost method and value per account), totals (`market_value`,
  `cost_basis`, `unrealized`) and `allocation` (`asset_class`, `sector`,
  `account`: `[{"key", "value", "weight"}]`) in the household's base
  currency, `unpriced_security_ids` and `missing_rates`. Only visible
  accounts count; a hidden `account` filter is `404 account-not-found`.
- **Performance** (`GET /investments/performance?range=&account=`, range
  `1m|3m|6m|ytd|1y|3y|5y|max`, default `1y`): `start`, `end`, `days`,
  `start_value`, `end_value`, `inflows`, `outflows`, `income`, `fees`,
  `twr` and `mwr` over the period, `twr_annualized` and `mwr_annualized`
  (periods of a year or more), `realized` and `unrealized` gains (short and
  long term), a `series` of `{"date", "value", "twr"}` (at most 400 points)
  and the unpriced securities / missing rates, all in the base currency over
  the securities of visible accounts. The period is valued day by day and is
  at most 3660 days: `max` over an older history starts 3660 days before
  today, from the value held then (`start` says so; SECURITY.md F8). Method: `app/domain/investments/performance.py`
  and `returns.py` (daily valuation chain; XIRR by Newton with a bisection
  fallback in `Decimal`).

Loans notes (`app.domain.loans`, router `loans`; scopes `loans:read` /
`loans:write`; any role reads, member and above writes on an account they
may edit; DATA_MODEL.md "Loans"). Rates are **fractions** as decimal
strings (`"0.06125"` = 6.125 %, at most eight decimals, `0 <= rate < 1`;
a JSON number is `422`). Hidden, foreign and missing loans are `404
loan-not-found`; on an account the caller may only view, writes are `403`.

- **Loans** (`{"id", "account_id", "principal_original", "rate",
  "rate_type", "term_months", "origination_date", "payment_amount",
  "payment_day", "escrow_amount", "lender", "maturity_date", "projection",
  "created_at", "updated_at"}`): `POST /loans` (`Idempotency-Key` honoured)
  takes `account_id` (type loan, mortgage or line_of_credit: `422
  invalid-loan-account`; one loan per account, `409 loan-exists`), the
  terms (money in the account currency, `422 currency-mismatch`),
  `payment_amount` (principal and interest; default the level payment),
  `payment_day` (default the origination day) and `escrow_amount`. Terms
  whose payment does not cover a period's interest are `422
  loan-payment-too-low`. `PATCH` changes any term (`escrow_amount` and
  `lender` may be null; `422 invalid-payment-date` when payments would
  precede the origination date); `DELETE` removes the terms, payments and
  rate changes (the account and its statement/manual balances stay).
  `projection` is `{"as_of", "balance", "balance_source": "recorded" |
  "scheduled", "rate", "scheduled_payment", "next_due_date",
  "payoff_date", "remaining_payments", "remaining_interest"}`: from the
  loan account's balance and the first payment due more than 14 days after
  the latest recorded payment or statement balance, or, with nothing
  recorded, the original schedule with every payment due by today assumed
  made.
- **Schedule** (`GET /loans/{id}/schedule?basis=original|projected`):
  `{"loan_id", "basis", "start_balance", "rows": [{"number", "due_date",
  "rate", "payment", "principal", "interest", "extra", "escrow", "total",
  "balance"}], "totals": {"payments", "principal", "interest", "extra",
  "escrow", "paid", "payoff_date"}}`. Payment `k` is due `k` months after
  origination on `payment_day`; interest is monthly (rate / 12) at the rate
  in force on the period's first day, rounded to the minor unit each
  period; the final payment takes what rounding left, so `principal` adds
  up to the start balance exactly. A variable loan re-amortises over the
  months left at each rate change; a fixed one keeps its payment.
- **Simulate** (`POST /loans/{id}/simulate`, any role, nothing stored):
  `{"one_off": [{"on", "amount"}] (max 240), "recurring": {"amount",
  "start"?, "end"?}, "include_schedule"}`; extras are principal, `> 0`
  (`422 invalid-extra-payment`) in the loan currency. A one-off goes with
  the first payment due on or after its date, a recurring extra with every
  payment due in its window (default from today). Answer: `{"loan_id",
  "as_of", "balance", "baseline", "with_extras"` (each `{"payoff_date",
  "payments", "total_interest", "total_extra"}`), `"interest_saved",
  "payments_saved", "schedule"}` (the rows with extras, when asked).
- **Payments** (`{"id", "loan_id", "paid_on", "principal", "interest",
  "escrow", "fees", "total", "balance_after", "transaction_id",
  "source_document_id", "schedule_check", "created_at"}`): `GET
  /loans/{id}/payments?from=&to=&limit=` (oldest first, at most 1200);
  `POST` (`Idempotency-Key` honoured) `{"paid_on", "principal",
  "interest", "escrow"?, "fees"?, "transaction_id"?}` (parts `>= 0`, not
  all zero, `422 invalid-payment`; dated from origination to today, `422
  invalid-payment-date`; a linked transaction must be visible, `404
  transaction-not-found`); `DELETE .../payments/{payment_id}` (`404
  loan-payment-not-found`). `schedule_check` compares the interest with the
  interest due on the balance owed the day before at the period's rate:
  `{"status": "ok" (within one minor unit) | "differs" | "unchecked",
  "expected_interest", "difference"}`. Loan statements add payments too
  (INGESTION.md "Loan statements"). Every change rebuilds the loan
  account's `computed` balances, so its balance is the principal owed.
- **Rate changes** (`{"id", "loan_id", "effective_date", "new_rate",
  "created_at"}`): `GET /loans/{id}/rate-changes`; `POST` after the
  origination date (`422 invalid-rate-change`, one per day `409
  rate-change-exists`); `DELETE .../rate-changes/{change_id}` (`404
  loan-rate-change-not-found`).

Events `loan.created|updated|deleted` carry the loan and account ids
(account-scoped). Audit actions `loan.create|update|delete`,
`loan.payment_add|payment_delete|payment_import`,
`loan.rate_change_add|rate_change_delete`.

Manual assets notes (`app.domain.assets`, router `assets`; scopes
`accounts:read` / `accounts:write`, a manual asset is an account; any role
reads, member and above writes on an account they may edit). `{"id",
"account_id", "name", "kind": "property"|"vehicle"|"other",
"description", "valuation_method", "current_value", "created_at",
"updated_at"}`; `kind` is the account's `asset_kind`, `current_value` the
latest valuation on or before today (`manual` beats `provider` on a day).
`POST /assets` (`Idempotency-Key` honoured) `{"account_id" (type
manual_asset, `422 invalid-asset-account`; one per account, `409
asset-exists`), "description"?, "valuation_method"?, "value"?,
"valued_on"?}`; `PATCH` `{"kind"?, "description"? (null clears),
"valuation_method"?}`; `DELETE` removes the details (the account and its
valuations stay). Valuations (`{"id", "account_id", "valued_on", "value",
"source", "created_at"}`): `GET /assets/{id}/valuations?from=&to=&limit=`
(newest first); `POST` `{"valued_on", "value"}` (`>= 0`, `422
invalid-valuation`; account currency, `422 currency-mismatch`; not in the
future, `422 valuation-in-future`) replaces that day's manual valuation and
is also the account's `manual` balance of that day; `DELETE
.../valuations/{valuation_id}` (`404 valuation-not-found`). `description`
may hold an address: callers who see the account get it, the audit trail
and events never do (`has_description` only), and it is on the `ai:read`
denylist (`classification: identity`). Hidden, foreign and missing assets
are `404 asset-not-found`. Events `asset.created|updated|deleted`.

FX rates notes (`app.domain.fx.manage`, router `fx_rates`; scopes
`accounts:read` / `accounts:write`): `1 base = rate quote` on `rate_date`,
rates as decimal **strings** (at most 12 digits before and 12 after the
point, above zero; output without trailing zeros), one per household, pair
and day (DATA_MODEL.md "Ledger" for the conversion rules). `GET
/fx-rates?base=&quote=&from=&to=&cursor=&limit=` (any role): newest day
first, then base and quote; `base`/`quote` filter the stored direction (`422
invalid-range`, `422 invalid-cursor`). `POST /fx-rates` `{"base", "quote",
"rate_date", "rate"}` (owner or admin: rates are household-wide and change
converted totals over accounts members cannot see; `Idempotency-Key`
honoured) sets the day's **manual** rate, replacing what that pair had that
day, a fetched rate included (`source` becomes `manual`); not in the future
(`422 fx-rate-date-in-future`), `base` and `quote` must differ. `DELETE
/fx-rates/{id}` (owner or admin, `404 fx-rate-not-found`). Writes are
audited (`fx_rate.set|delete`) and emit `fx.rate_set|rate_deleted` with the
rate id. Fetched rates (`PERFIN_FX_PROVIDER=ecb`) and fetched prices
(`PERFIN_PRICE_PROVIDER=http_json`) come from the `refresh_prices` worker
job, never replace a manual row, and carry the provider name as `source`;
what they send is in docs/security/integrations.md.

Net worth notes (`app.domain.networth`, router `networth`; any role, scope
`networth:read`; DATA_MODEL.md "Net worth"):

- **Scope**: owners, admins and members/viewers granted every account get
  household figures (`"scope": "household"`); anyone else gets figures
  recomputed over the accounts they may see (`"visible_accounts"`). A stored
  `net_worth_snapshot` (whose breakdown names every account) is only ever
  read for household scope.
- `GET /networth?range=&granularity=`: `range` `1m|3m|6m|ytd|1y|3y|5y|max`
  (default `1y`; `max` from the first data), `granularity` `day|week|month`
  (default: `day` up to 400 days, `week` up to 7 years, else `month`; more
  than 1000 points is `422 too-many-points`; `max` without `granularity`
  starts at most 1000 months back instead). Answer `{"currency", "range",
  "granularity", "scope", "date_from", "date_to", "points": [{"as_of",
  "assets", "liabilities", "net", "source": "snapshot"|"computed"}],
  "change"}`: one point per bucket end (every day, every 7 days back from
  today, every month end), the last one today and always computed live.
- `GET /networth/latest`: `{"as_of", "currency", "scope", "assets",
  "liabilities", "net", "by_class": {"assets": {class: Money},
  "liabilities": {...}}, "by_type": {account_type: signed Money},
  "accounts": [{"account_id", "name", "type", "account_class", "side",
  "amount" (account currency), "amount_base"}], "unconverted": [account
  ids], "unpriced_security_ids", "last_snapshot_date"}` (the snapshot date
  only for household scope). Classes: `cash`, `investments`, `real_estate`,
  `vehicles`, `other_assets`, `credit_cards`, `mortgages`, `loans`,
  `other_liabilities`.
- `GET /networth/projection?months=&basis_months=` (1-360, default 12;
  1-120, default 12): `{"as_of", "currency", "scope", "months",
  "basis_months", "current", "trend": {"slope_per_month", "points": [{"on",
  "net"}]} | null, "savings": {"average_monthly_savings", "savings_rate",
  "points"}, "unconverted"}`. Trend: least-squares line through the month
  ends of the basis window and today; savings: the average monthly net cash
  flow of the basis window's whole months (`/reports/cashflow` rules) added
  each month. Neither compounds returns (`app/domain/networth/projection.py`).

Recurring notes (`app.domain.recurring`, router `recurring`; scopes
`transactions:read` / `transactions:write`):

- Schedules (`{"id", "account_id", "name", "cadence":
  daily|weekly|monthly|quarterly|yearly, "interval_count", "next_due",
  "amount_min", "amount_max", "payee_id", "category_id", "origin":
  detected|manual, "active", "last_seen_on"}`) follow their account's
  visibility: hidden, foreign or missing is `404 schedule-not-found`; writes
  need member or above and an edit grant (`403` on a view-only account).
  `GET /recurring?origin=&active=&account=` lists at most 500, soonest first.
  `POST /recurring` (`Idempotency-Key` honoured) adds a manual schedule;
  amounts are in the account currency (`422 currency-mismatch`) with
  `amount_min <= amount_max` (`422 invalid-amount-range`); payee and
  category must be the household's (`404`). `PATCH` changes any field
  (`null` clears payee/category), `DELETE` removes it.
- `GET /recurring/candidates?account=` (any role) runs detection over the
  visible accounts and stores nothing: `{"as_of", "items": [{"account_id",
  "payee_id", "name", "pattern": weekly|biweekly|monthly|quarterly|annual,
  "cadence", "interval_count", "amount_min", "amount_max", "category_id",
  "occurrences", "first_seen", "last_seen", "next_due", "regularity",
  "confidence", "schedule_id"}]}`. `POST /recurring/detect` (`{"account_ids"?}`,
  member or above; every account editable, else `404 account-not-found`)
  stores them as `detected` schedules and answers `{"as_of", "created",
  "updated", "unchanged", "skipped_manual", "items"}`. Manual schedules are
  never changed; a detected schedule keeps the name and `active` flag a
  person gave it. Detection rules: `app/domain/recurring/detection.py`.
- `GET /cashflow/calendar?days=&account=` (any role; `days` 1-366, default
  30): `{"currency", "date_from", "date_to", "opening_balance", "days":
  [{"on", "events": [{"schedule_id", "name", "account_id", "kind":
  income|bill, "amount", "amount_min", "amount_max", "amount_base"}],
  "income", "bills", "net", "balance"}], "income", "bills", "net",
  "unconverted"}`: occurrences of the active visible schedules from today
  at the midpoint of their range; `opening_balance` and `balance` cover the
  visible checking, savings and cash accounts.

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

Tax vault notes (`app.domain.tax`, router `tax`; scopes `tax:read` /
`tax:write`; DATA_MODEL.md "Tax vault"): a tax year is household-wide. Any
role reads `GET /tax-years` (newest first) and `GET /tax-years/{year}`
(`{"id", "year", "jurisdiction", "filing_status", "state", "notes",
"checklist": {"expected", "received", "not_applicable"}, ...}`); `404
tax-year-not-found` for a year the household has not opened. Member or
above: `POST /tax-years` `{"year", "jurisdiction"? ("US"), "filing_status"?,
"notes"?}` (`201`, `Idempotency-Key` honoured; `409 tax-year-exists`; `422
tax-jurisdiction-unknown` / `tax-filing-status-unknown`), which learns the
checklist from the year before: every form received last year (checklist
items `received` and forms parsed with last year printed on them) becomes an
`expected` item with origin `prior_year`; `PATCH /tax-years/{year}`
`{"filing_status"?, "notes"?, "state"?}`: `open -> filed -> closed`, `open ->
closed`, `filed -> open`; a closed year changes only back to `open`, by an
owner or admin (`403` for others), and is otherwise frozen (`409
tax-year-closed`); other moves are `409 tax-year-state-invalid`. `DELETE
/tax-years/{year}`: owner or admin (its checklist goes with it; form data
stays with its documents). A year is also opened by the worker when the
first form printed with that year is parsed.

Checklist: `GET /tax-years/{year}/checklist` (any role) `{"year", "items":
[{"id", "expected_form", "form_title", "issuer", "status":
expected|received|not_applicable, "origin": manual|prior_year|detected,
"document_id", ...}], "counts"}`; `document_id` is set only when the caller
may see the document (owners/admins always; members and viewers for
documents of accounts they see and their own unattached uploads). Member or
above: `POST` `{"expected_form", "issuer"?}` (`201`; the form type must
belong to the year's jurisdiction, `422 tax-form-unknown`; `409
tax-checklist-item-exists` for the same form and issuer, compared on letters
and digits only), `PATCH .../checklist/{item_id}` `{"status"?, "issuer"?,
"document_id"?}` (naming a document marks the item `received`; the document
must be one the caller can see, else `404 document-not-found`; any other
status clears it; a document with a status other than `received` is `422
tax-checklist-document-needs-received`), `DELETE`, and `POST
.../checklist/learn` (`{"added"}`: learn again, adding only what is missing).
A parsed form ticks the checklist off by itself (INGESTION.md "Parse").

`GET /tax-years/{year}/forms` (any role): the forms parsed with that year,
of documents the caller may see: `{"id", "document_id", "form_type",
"form_index", "tax_year", "issuer_name", "source", "confidence", "boxes":
[{"box", "label", "amount"}], "coverage_months"?}` (amounts as decimal
strings; `12_D` is W-2 box 12 code D, `cov_<MON>` a covered month of a
1095). Identity boxes are never stored, so they cannot be returned.

`GET /tax-years/{year}/summary` (any role): deductible tagging rolled up
over the calendar year: every ledger line (splits instead of their
transaction; pending review rows and transfers left out) in a category with
a `tax_treatment`, and every split flagged `tax_flag` (`POST
/transactions/{id}/split`), summed per category in the household's base
currency (rate of each posting day; amounts without a rate under
`unconverted`): `{"year", "jurisdiction", "currency", "partial",
"by_treatment": {"deductible"|"taxable_income"|"tax_exempt_income"|
"tax_payment"|"flagged": money}, "categories": [{"category_id",
"category_name", "tax_treatment", "schedule", "line", "total", "flagged"}],
"by_hint": [{"schedule", "line", "total"}], "forms": [{"form_type",
"count", "boxes"}], "checklist", "unconverted"}`. A member or viewer gets
totals over their visible accounts only and `partial: true` unless they see
every account.

Schedule hints: `GET /tax-category-map?jurisdiction=` (any role) and, member
or above, `PUT /tax-category-map/{category_id}` `{"jurisdiction"? ("US"),
"schedule", "line"?}` (upsert; `404 category-not-found`) and `DELETE
/tax-category-map/{category_id}?jurisdiction=` (`404
tax-category-map-not-found`). A hint for the accountant, not tax advice.

Accountant export (`app.domain.tax.export`): `POST
/tax-years/{year}/export` needs an owner or admin **browser session with a
step-up** (tokens refused, as for `POST /export`) and answers `202
{"job_id", "year", "state"}` (`Idempotency-Key` honoured; `409
tax-export-in-progress` while another is pending). The `tax.export` job
writes a zip of `originals/<document id>/<file>` (every accepted document of
the year: its `tax_year`, named by the checklist or holding the year's form
data), `summary.csv` (sections `year`, `checklist`, `form`, `category`,
`hint`; formula cells neutralised, SECURITY.md C7) and `summary.pdf`, into
the blob store, encrypted at rest. `GET /tax-years/{year}/export/{job_id}`
(the requester only, else `404 tax-export-not-found`) gives its state, size,
SHA-256, expiry and counts; `GET .../download` (step-up) streams it once,
within `PERFIN_EXPORT_TTL_MINUTES` (`410 tax-export-already-downloaded` /
`tax-export-expired`, `409 tax-export-not-ready`); the stored copy is
crypto-shredded before the first byte is sent.

Events: `tax_year.created|updated|deleted`, `tax_category_map.updated`,
`tax_export.request|complete|fail|download|expire`, and `tax.reminder`
`{"year", "missing", "message"}` ("3 expected documents still missing") on
the days of `PERFIN_TAX_REMINDER_DATES` for open years of the previous
calendar year with expected items left.

Intake settings notes (INGESTION.md "Email intake specifics"): every
`/settings/intake` route needs a browser session of an owner or admin
(tokens: `403 session-required`). `GET /settings/intake` returns
`{"configured", "instance_enabled", "enabled", "host", "port", "username",
"password_set", "inbox_folder", "processed_folder", "rejected_folder",
"alias_token", "alias_rotated_at", "allowed_senders": [{"address",
"user_id"}], "member_addresses", "require_auth_results", "authserv_id",
"last_poll_at", "last_poll_outcome", "last_poll_counts"}`; the password is
never returned, and `configured` is `false` with no alias until the first
change. `PATCH` (step-up) takes any of the writable fields plus the
write-only `password`; `422 mail-intake-incomplete` when enabling without
host, username and password, `422 invalid-sender-user` when an allowed
sender maps to someone who is not a member, admin or owner. `POST
/settings/intake/email/test` logs in and examines the inbox read-only:
`{"ok", "outcome", "messages", "move_supported"}` with `outcome` one of
`ok`, `blocked`, `connect_failed`, `tls_failed`, `auth_failed`,
`folder_missing`, `protocol_error`, `timeout` (`409
mail-intake-not-configured` without stored credentials). `POST
/settings/intake/email/rotate-alias` (step-up) returns `{"alias_token",
"alias_rotated_at"}`; mail to the old token is rejected from then on.

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

AI views notes (`/ai/v1`, Phase 6b, AI_ACCESS.md, `backend/app/api/ai/`):
every route is a `GET` that answers **only** the token of an active AI
client (`ai:read`, minted by `app.domain.ai.service.create_client`, the
AI-client flow; `422 scope-reserved` for personal tokens). Anything else is
`401` (no or bad credential) or `403 ai-client-required` (sessions, personal
tokens, a revoked or expired client); a demoted owner's client gets `403
insufficient-scope`. An AI token on any other route is `403
ai-token-confined`. Per client: `429 rate-limited` beyond 60 calls a minute
or 300 in ten minutes (on top of the per-token limit). Every call, refusals
in the view included, is recorded in `ai_call` with the response as sent.
Money is `{"amount", "currency"}` after the client's rounding (an integer
string when rounding is on). Periods: `period` = `this_month`, `last_month`,
`last_30_days`, `last_90_days`, `last_12_months`, `this_year`, `last_year`,
`ytd`, `YYYY` or `YYYY-MM`, overridden by `date_from` / `date_to`; at most
1830 days (`422 invalid-period`), clamped to the client's date cap (`422
outside-date-range` when wholly before it). In Aggregates mode the
transaction-level views (`/transactions`, `/recurring`, `/holdings`,
`/loans`, `/loans/{id}/schedule|simulate`, `/anomalies`, spending by payee)
are `403 not-in-aggregates-mode`, `group_by=account` becomes
`account_type`, goal and budget names become "Goal A"/"Budget A" and tax
issuers are omitted. Free text (payees, memos, notes, names people gave
things, lenders, issuers) is scrubbed (`[ACCT]`, `[GOVID]`, `[EMAIL]`,
`[PHONE]`, `[ADDRESS]`, `[MEMBER]`, `[REDACTED]`). Routes and tools:

```
GET /ai/v1/policy                      explain_data_policy
GET /ai/v1/overview?period             get_overview (net worth now, cash flow, savings rate)
GET /ai/v1/spending|income?period&group_by=category|payee|month|account|account_type&depth
GET /ai/v1/trends?metric=spending|income&month=YYYY-MM&months
GET /ai/v1/budgets/status?on           get_budget_status
GET /ai/v1/goals                       get_savings_goals
GET /ai/v1/recurring                   get_recurring (active schedules)
GET /ai/v1/net-worth/history?range&granularity
GET /ai/v1/allocation                  by asset class, sector and account (type)
GET /ai/v1/accounts                    type, label, institution, currency, balance (no number)
GET /ai/v1/transactions?period&account_id&category&payee&min_amount&max_amount&limit(<=200)&cursor
GET /ai/v1/anomalies?period            large for its category, or a new payee
GET /ai/v1/holdings?account_id         by ticker
GET /ai/v1/performance?range&account_id
GET /ai/v1/loans                       lender, balance, rate, term, payoff (no loan number)
GET /ai/v1/loans/{loan_id}/schedule?basis&offset&limit(<=600)
GET /ai/v1/loans/{loan_id}/simulate?extra_monthly&extra_once&extra_once_on   (stores nothing)
GET /ai/v1/tax/{year}/summary          totals by category/schedule, amounts by box label, checklist
```

AI views also answer `403 ai-access-disabled` (`"scope": "instance"` or
`"household"`) while the instance kill switch or the household's AI flag is
off (see the AI settings notes).

`category` and `payee` match canonical names exactly (case-insensitive),
never as patterns, **as the client sees them** (scrubbed): a value the
client's scrubber would change (a member's name, a denylisted term) matches
nothing, like a miss, and `Gifts for [MEMBER]` matches the names that scrub
to it (AI_ACCESS.md "Free-text scrubbing"). There is no merchant category code in the ledger, so
`get_spending(group_by=merchant_category)` of AI_ACCESS.md is served by
`category` (the household's categories).

AI settings notes (`/settings/ai`, `/settings/system/ai`, Phase 6b,
AI_ACCESS.md "Client management", `backend/app/api/routers/settings_ai.py`):
every `/settings/ai` route needs the **owner** of the active household on a
browser session (`403 forbidden` for other roles, `403 session-required`
for tokens; an AI token gets `403 ai-token-confined`); changes need step-up
(`403 step-up-required`).

* `GET /settings/ai` -> `{"enabled", "household_enabled", "system_enabled",
  "active_clients"}`. `PATCH /settings/ai` `{"enabled"?}` switches AI access
  of the household (an empty body changes nothing). Off: every AI view of
  the household answers `403 ai-access-disabled` and no client can be
  created; clients are kept and resume when it is turned back on.
* `GET /settings/system/ai` -> `{"enabled"}`; `PATCH` `{"enabled"?}` (instance
  admin, step-up) is the kill switch of the whole instance, with the same
  effect for every household. Both switches default to on (no row); both
  changes are audited (`ai.system_switch`, `ai.household_switch`).
* `POST /settings/ai/clients` `{"name", "expires_in_days" (required, 1 to
  the token maximum), "mode"?, "transport"?, "rounding"? (1|10|100),
  "hide_nicknames"?, "hide_merchant_location"?, "date_range_cap"? (days),
  "custom_denylist"?, "member_aliases"?}` -> `201` client with `token`, the
  raw `ai:read` token, shown once (SHA-256 at rest). `409
  too-many-ai-clients`, `422 invalid-ai-options`, `422 expiry-too-long`,
  `403 ai-access-disabled`. A client is `{"id", "name", "mode",
  "transport", "rounding", "hide_nicknames", "hide_merchant_location",
  "date_range_cap", "custom_denylist", "member_aliases", "token_prefix",
  "status" (active | expired | revoked), "created_at", "expires_at",
  "revoked_at", "last_used_at"}`. `DELETE /settings/ai/clients/{id}`
  revokes the client and its token (`204`, idempotent); the next call with
  the token is `401`. Another household's client is `404
  ai-client-not-found`.
* Exposure preview: `GET /settings/ai/clients/{id}/preview` (saved options)
  and `POST /settings/ai/preview` (the create body's options, no expiry) ->
  `{"mode", "tools": [{"tool", "params", "status", "bytes", "body",
  "placeholders": [{"path", "placeholders"}]}], "placeholder_counts"}`:
  every tool run with its default parameters on the household's real data
  through the same views, serialisers and scrubber, `body` exactly as the
  view would send it (a refusal as its problem body), each string holding a
  placeholder marked by JSON path (`items[2].payee`) for highlighting.
  Nothing is stored: no client, token, `ai_call` row or cache entry.
* `GET /settings/ai/calls?client_id&contains(1-200)&limit(<=500)` ->
  `{"items": [{"id", "client_id", "tool", "params", "status", "bytes",
  "created_at", "response"}]}`, newest first; `contains` is a
  case-insensitive substring of the stored response (no pattern syntax).
* Scrubber preview: `GET /settings/ai/scrubber-preview?client_id&limit` and
  `POST` `{"client_id"?, "custom_denylist"?, "member_aliases"?, "text"?,
  "limit"?}` (terms to try before saving them; added to the client's) ->
  `{"samples": [{"source", "original", "scrubbed", "changed",
  "placeholders"}], "changed"}` over recent transaction payees, memos and
  notes, split memos, payee names and account names/nicknames the owner can
  see (and `text`, first).

`GET /auth/session` carries `ai_clients_active` (the active household has a
client that is neither revoked nor expired) for the persistent banner (M5),
for every member of the household.

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
- Versioning: path version; breaking changes bump the path.
- Every response carries `Cache-Control: no-store` except static assets.

## Endpoint families (first cut)

```
POST   /auth/login                      local login (step 1)
POST   /auth/mfa/verify                 local login (step 2)
POST   /auth/logout
GET    /auth/oidc/start                 redirect to IdP
GET    /auth/oidc/callback
POST   /auth/oidc/backchannel-logout
GET    /auth/session                    who am I, household, role, csrf token
GET/POST/DELETE /auth/sessions          list and revoke
GET/POST/DELETE /auth/mfa/webauthn      register / remove
GET/POST/DELETE /auth/mfa/totp
POST   /auth/mfa/recovery-codes         regenerate (step-up)
GET/POST/DELETE /auth/tokens            personal access tokens (step-up)

GET/POST        /households             (instance admin)
GET/PATCH       /households/{id}
GET/POST/PATCH/DELETE /households/{id}/members

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

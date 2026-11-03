# Data model

Entity names are singular. All tables carry `id` (UUIDv7), `created_at`,
`updated_at`. Every household-scoped table carries `household_id` and is covered
by a Postgres row-level security policy. Monetary amounts are stored as
`NUMERIC(20,6)` plus an ISO 4217 `currency` column; never floats.

## Identity and access

```
household ─────< household_member >───── user
                      │
                      └── role: owner | admin | member | viewer

user ───< credential          (local password hash, Argon2id params, rotated_at)
user ───< mfa_method          (type: webauthn|totp|recovery, public key / secret, last_used)
user ───< identity            (provider: local|oidc, issuer, subject, email_at_link, linked_at)
user ───< session             (opaque id hash, user agent, ip, created, last_seen, expires, mfa_verified)
user ───< api_token           (hashed token, scopes[], name, expires, last_used)
household ──< ai_client       (name, tier 0|1|2, transport, pseudonym_salt, rounding,
                               date_coarsening, model_endpoint?, token_id, revoked_at)
ai_client ──< ai_call         (tool, params JSONB, response JSONB (redacted), bytes, created)
household ──< account_grant   (member -> account visibility: none|view|edit)
```

- `identity` allows one user to have both a local credential and an OIDC
  identity. Matching is by `(issuer, subject)`; email is informational.
- A household is the unit of data sharing. Most self-hosters will have one.

## Ledger

```
institution           (name, country, fingerprint rules, default parser)
account               (household, institution?, type, name, currency, number_last4,
                       opened, closed, visibility, is_asset, manual_balance?)
transaction           (account, posted_date, effective_date, amount, currency,
                       payee_id?, raw_payee, memo, status: pending|cleared|reconciled,
                       transfer_group_id?, source_document_id?, import_hash,
                       review_state, confidence)
transaction_split     (transaction, category_id, amount, memo, tax_flag?)
payee                 (household, canonical name, aliases[], default category)
category              (household, parent_id?, name, kind: expense|income|transfer|ignore,
                       tax_treatment?)
tag                   (household, name)   + transaction_tag (m:n)
rule                  (household, order, enabled, conditions JSONB, actions JSONB,
                       hit_count, last_hit)
scheduled_transaction (household, account, cadence, next_due, amount range, payee,
                       detected_or_manual)
balance_snapshot      (account, as_of_date, balance, source: statement|computed|manual)
fx_rate               (base, quote, date, rate, source)
```

- `import_hash` is a stable hash of (account, date, amount, normalised payee,
  running index within day) used for cross-statement deduplication.
- Transfers are two transactions sharing a `transfer_group_id`; the matcher
  pairs them by amount, opposite sign, and date proximity across accounts.

## Budgets and goals

```
budget           (household, name, period: monthly|annual|custom, start, rollover)
budget_line      (budget, category, amount)
goal             (household, name, target_amount, target_date, accounts[], created)
goal_contribution (goal, date, amount, transaction_id?)
```

## Investments

```
security          (symbol, isin?, cusip?, name, asset_class, exchange, currency)
holding           (account, security, quantity, cost_basis, as_of)          -- derived cache
lot               (account, security, open_date, quantity, cost_per_unit, closed_date?)
investment_txn    (account, security?, type: buy|sell|dividend|interest|fee|split|
                   transfer_in|transfer_out|reinvest|corporate_action,
                   date, quantity, price, amount, fees, lot_links JSONB, source_document_id?)
price             (security, date, close, source)
```

## Loans

```
loan              (account, principal_original, rate, rate_type: fixed|variable,
                   term_months, origination_date, payment_amount, payment_day,
                   escrow_amount?, lender)
loan_payment      (loan, date, principal, interest, escrow, fees, transaction_id?)
loan_rate_change  (loan, effective_date, new_rate)
```

A loan is also an `account` of type `loan` so it appears in net worth with its
current balance.

## Manual assets and liabilities

```
manual_asset      (account, kind: property|vehicle|other, description, valuation_method)
asset_valuation   (account, date, value, source: manual|provider)
```

## Net worth

```
net_worth_snapshot (household, as_of_date, assets, liabilities, net, breakdown JSONB)
```

Computed daily by the worker from `balance_snapshot`, `holding` x `price`,
`loan` balances and `asset_valuation`.

## Documents and ingestion

```
document          (household, kind: statement|receipt|invoice|tax_form|payslip|
                   insurance|other|unknown, status: received|scanning|extracting|
                   classifying|parsing|needs_review|processed|rejected|failed,
                   source: upload|dropfolder|email|api, source_meta JSONB,
                   blob_id, sha256, size, mime_detected, original_filename,
                   uploaded_by?, account_id?, institution_id?,
                   period_start?, period_end?, tax_year?, extracted_text (tsvector),
                   extraction JSONB, confidence JSONB, parser_version)
blob              (store, key, size, enc_alg, wrapped_dek, nonce, created)
document_link     (document, target_type: transaction|investment_txn|loan_payment|
                   account|tax_year, target_id)
review_item       (household, document?, kind: unknown_owner|unknown_account|
                   low_confidence_field|possible_duplicate|unparsed|new_institution|
                   rejected_email, payload JSONB, state: open|resolved|dismissed,
                   resolved_by?, resolution JSONB)
parser_fingerprint(institution, kind, match rules JSONB, parser name, version,
                   learned_from_document_id?)
ingest_job        (document, stage, attempt, started, finished, error, log JSONB)
```

## Tax vault

```
tax_year          (household, year, jurisdiction, filing_status?, state: open|filed|closed)
tax_checklist_item(tax_year, expected_form: W-2|1099-INT|1099-DIV|1099-B|1098|...,
                   issuer, status: expected|received|not_applicable, document_id?)
tax_category_map  (household, category -> schedule/line hint)
```

## Platform

```
job               (queue, type, payload JSONB, state, run_after, attempts, locked_by,
                   locked_at, last_error, household_id?)
event             (household, type, payload JSONB, created)   -- feeds SSE, pruned
audit_log         (household?, user?, action, target_type, target_id, before JSONB,
                   after JSONB, ip, user_agent, created)       -- append-only
notification_channel (household, type, config (encrypted), enabled)
setting           (scope: system|household|user, key, value JSONB)
```

## Encryption at the column level

Columns encrypted with the application master key before storage: IMAP
credentials, notification channel secrets, API provider keys, full account
numbers (if ever stored; default is last four only), MFA TOTP secrets. Argon2id
hashes and WebAuthn public keys are not encrypted (they are not secrets).

## Retention and deletion

Deleting a document deletes the blob and dependent rows; deleting a transaction
keeps the audit row. A household owner can run a full export then a full purge.
Purge zero-fills nothing; it relies on encryption: destroying the wrapped DEKs
renders blobs unreadable even on media that cannot be securely erased.

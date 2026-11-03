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
household ──< ai_client       (name, mode: detail|aggregates, transport, rounding,
                               hide_nicknames, hide_merchant_location, date_range_cap,
                               custom_denylist[], token_id, revoked_at)
ai_client ──< ai_call         (tool, params JSONB, response JSONB (redacted), bytes, created)
household ──< account_grant   (member -> account visibility: none|view|edit)
```

- `identity` allows one user to have both a local credential and an OIDC
  identity. Matching is by `(issuer, subject)`; email is informational.
- A household is the unit of data sharing. Most self-hosters will have one.

Phase 1 notes (`app/auth/models.py`, `app/auth/mfa/models.py`,
`app/auth/oidc/models.py`; created by revision `0002_phase1_identity`, which
also applies the RLS below and the application-role grants, and whose
downgrade drops the Phase 1 enum types):

- Table names: `user` and `session` are SQL keywords (`SELECT user` returns
  the database role), so the tables are **`app_user`** and **`user_session`**.
  `rate_limit_event` (scope, `key_hash` = SHA-256 of scope and key, created)
  backs the login/MFA rate limits and is pruned hourly; it has a `bigint` id
  and no `updated_at` (append-and-delete only).
- `app_user`: `username` unique and stored lower-case (check constraint;
  `normalize_username`), `display_name`, `email?`, `email_verified`,
  `is_instance_admin` (set only from the local database, never from an OIDC
  claim; SECURITY.md B6), `is_active`, `is_breakglass`, `locked_until`,
  `failed_login_count`, `last_failed_login_at`, `last_login_at`.
- `credential`: one per user; Argon2id PHC string plus `hash_params` JSONB
  and `rotated_at`.
- `mfa_method`: `type` totp|webauthn|recovery; TOTP `secret_encrypted` is an
  `app.core.crypto` envelope with associated data
  `column_ad("mfa_method", "secret_encrypted", id)` and `totp_last_counter`;
  WebAuthn `credential_id` (unique), `public_key`, `sign_count`,
  `transports`; recovery codes one row each with `code_hash` and
  `consumed_at`.
- `user_session`: `token_hash` (SHA-256 of the cookie value), `household_id`
  (current; `ON DELETE SET NULL`), `stage` mfa_pending|authenticated,
  `login_method` local|oidc|breakglass|bootstrap, `user_agent`, `ip`,
  `last_seen_at`, `idle_expires_at`, `absolute_expires_at`,
  `mfa_verified_at`, `remember_device`, `revoked_at`, `revoked_reason`,
  `rotated_from_id`.
- `household_member` (unique `(household_id, user_id)`, `role`),
  `account_grant` (unique `(household_id, user_id, account_id)`, `level`) and
  `api_token` (`prefix` unique public id, `token_hash`, `scopes[]`,
  `expires_at` NOT NULL, `last_used_at`, `revoked_at`) are `HouseholdScoped`
  (forced RLS). Each also has a **SELECT-only** policy `user_id =
  perfin.user_id` so a user's memberships, grants and tokens can be listed
  before a household context exists; `api_token` additionally allows
  `prefix = perfin.token_prefix` (set by
  `app.auth.principal.token_lookup_scope` while a bearer token is looked up).
  Writes always need the household context.
- `mfa_challenge` (`app/auth/mfa/models.py`): outstanding MFA ceremony of a
  session: `session_id` (FK `user_session`, `ON DELETE CASCADE`), `user_id`,
  `purpose` webauthn_register|webauthn_authenticate|totp_enroll (string with
  a check constraint), `challenge` (WebAuthn), `secret_encrypted` (pending
  TOTP secret, envelope bound to the challenge row), `expires_at`. At most
  one per session and purpose; WebAuthn challenges are deleted when used;
  expired rows are pruned hourly. Not household-scoped.
- `oidc_session` (`app/auth/oidc/models.py`): the OIDC side of one browser
  session: `session_id` (FK `user_session`, unique, `ON DELETE CASCADE`;
  moved to the new id on every session rotation), `user_id` (FK
  `app_user`, `ON DELETE CASCADE`), `issuer`, `subject`, `sid` (indexes
  `(issuer, sid)` and `(issuer, subject)` for back-channel logout),
  `id_token_encrypted` (the `id_token_hint` for RP-initiated logout, only
  when the IdP has an end-session endpoint) and `refresh_token_encrypted`
  (only with claim refresh): `app.core.crypto` envelopes bound to the row
  (`column_ad("oidc_session", <column>, id)`); `claims_refreshed_at`. Rows of
  ended sessions are pruned hourly. Not household-scoped.
- `oidc_used_state` (`app/auth/oidc/models.py`): `state_hash` (primary
  key: SHA-256 of a consumed OIDC `state`, or the domain-separated SHA-256
  of a back-channel `logout_token`'s `(iss, jti)`), `expires_at` (cookie
  lifetime plus skew for a state; `iat` + 10 minutes + skew for a logout
  token), `created_at`. Makes each `state` and each logout token
  single-use; pruned hourly. Not household-scoped.
- `password_reset_token` (`app/auth/reset.py`): `token_hash` (SHA-256 of
  the emailed token, unique), `user_id` (FK `app_user`, `ON DELETE
  CASCADE`), `expires_at` (15 minutes), `used_at` (single use),
  `requested_ip`. A new request deletes the user's unused tokens; spent rows
  are pruned hourly after a day. Not household-scoped.
- First-run setup keeps two **system** settings: `bootstrap.setup_token`
  (`{"hash", "created_at", "expires_at"}`, only the SHA-256 of the one-time
  token; deleted when setup completes) and `bootstrap.completed`
  (`{"completed_at", "user_id"}`; once present, setup never reopens).
- `account_grant.(household_id, account_id)` references
  `account(household_id, id)` `ON DELETE CASCADE` (Phase 2), so a grant can
  only name an account of its own household.
- `account_grant` rows exist only for members and viewers (owners and admins
  see every account; `app.domain.households.service.set_account_grant`
  refuses them), viewers at most `view`. Removing a member deletes their
  grants in that household.
- User preferences are the **user** setting `user.preferences`
  (`{"locale", "theme", "timezone"}`, defaults `en-US`, `system`, `UTC`;
  invalid stored values fall back to the default). The display name stays on
  `app_user.display_name`. No new table.
- `household.base_currency` is an active ISO 4217 code (fund, metal, test and
  withdrawn codes refused; `app.domain.households.schemas.ISO_4217_CURRENCIES`).

## Ledger

```
institution           (household, name, country?, fingerprint_rules JSONB,
                       default_parser?, builtin_key?)
account               (household, institution?, type, asset_kind?, name, nickname?,
                       currency, number_last4?, opened_on?, closed_on?, is_asset,
                       manual_balance, include_in_net_worth)
transaction           (account, posted_date, effective_date?, amount, currency,
                       payee_id?, category_id?, raw_payee?, memo?, notes?,
                       status: pending|cleared|reconciled, transfer_group_id?,
                       source_document_id?, import_hash?,
                       review_state: none|pending|confirmed, confidence?)
transaction_split     (transaction, category_id?, amount, memo?, tax_flag)
payee                 (household, name, normalized_name, aliases[], default_category?)
category              (household, parent_id?, name, kind: expense|income|transfer|ignore,
                       tax_treatment?: taxable_income|tax_exempt_income|deductible|
                       tax_payment, system_key?)
tag                   (household, name)   + transaction_tag (m:n)
rule                  (household, name, sort_order, enabled, conditions JSONB,
                       actions JSONB, hit_count, last_hit_at)
scheduled_transaction (household, account, name, cadence: daily|weekly|monthly|
                       quarterly|yearly, interval_count, next_due, amount_min,
                       amount_max, currency, payee?, category?, origin: detected|manual,
                       active, last_seen_on?)
balance_snapshot      (account, as_of_date, balance, currency,
                       source: statement|computed|manual, source_document_id?)
fx_rate               (household, base, quote, rate_date, rate NUMERIC(24,12), source)
```

Phase 2 notes (`app/domain/accounts/models.py`,
`app/domain/transactions/models.py`, `app/domain/fx/models.py`; every table
is `HouseholdScoped` with forced RLS):

- **Household-consistent references** (SECURITY.md F3). Postgres checks
  foreign keys without RLS, so every reference between household-scoped
  tables includes `household_id`: `(household_id, x_id) -> x(household_id,
  id)`, with `UNIQUE (household_id, id)` on the target (`app.db.types`).
  Mandatory parents cascade (`transaction -> account`, `transaction_split ->
  transaction`, ...). Optional lookups that are cleared when the target is
  deleted (`transaction.payee_id`/`category_id`, `transaction_split.category_id`,
  `payee.default_category_id`, `account.institution_id`,
  `scheduled_transaction.payee_id`/`category_id`,
  `goal_contribution.transaction_id`) keep a plain `ON DELETE SET NULL` key
  plus a composite `DEFERRABLE INITIALLY DEFERRED` key that refuses another
  household's id at commit. `category.parent_id` is a composite `NO ACTION`
  key: a parent with children cannot be deleted until they move.
- **Currency**: `transaction`, `balance_snapshot` and `scheduled_transaction`
  reference `account(household_id, id, currency)`, so their currency always
  equals the account's, and an account's currency cannot change while such
  rows exist. A foreign-currency purchase is recorded in the account
  currency.
- **Sign**: `amount > 0` increases the account balance, `< 0` decreases it;
  a liability's balance is normally negative.
- **Categories**: an unsplit transaction carries `category_id`; a split one
  has `category_id` NULL and splits that add up to its amount (checked by
  the service writing splits). Reports read the splits when there are any,
  else the transaction. The default set (`app.domain.transactions.categories`,
  keyed by `system_key`) is seeded for every new household (first-run setup
  and `POST /households`); seeding is idempotent and skips a default whose
  name the household already uses under the same parent.
- **Account fields**: `type` is one of checking, savings, credit_card, cash,
  brokerage, retirement, loan, mortgage, line_of_credit, manual_asset,
  manual_liability; `asset_kind` (property|vehicle|other) is set exactly for
  `manual_asset`. `is_asset` defaults from the type (cards, loans, mortgages,
  lines of credit and manual liabilities are liabilities). `manual_balance`
  (default for manual assets and liabilities) means the balance comes from
  `balance_snapshot` only. `number_last4` (four digits) is identity data:
  stored for statement matching, never logged, never in audit rows, events
  or AI views (column info `classification: identity`); `raw_payee`, `memo`,
  `notes` are `classification: free_text`. The design's `visibility` column
  is not a column: who sees an account is `account_grant`
  (`app.domain.accounts.visibility`: owners and admins see all; members and
  viewers see granted accounts, viewers read-only; a hidden account is `404
  account-not-found`). Every query over account-derived data (transactions,
  balances, budgets spent, goals, reports) applies `visible_account_filter`.
- **Institutions** are household-scoped; `builtin_key` marks one created from
  a shipped default (the Phase 3 fingerprint catalogue), NULL for the
  household's own.
- **Balances** (`app.domain.accounts.balances`): a snapshot is the balance at
  the end of its date; on one date `manual` beats `statement` beats
  `computed`. The balance at the end of day `d` is the latest snapshot on or
  before `d` plus the transactions posted after it up to `d` (every status);
  without a snapshot the sum of all transactions up to `d`; a
  `manual_balance` account ignores transactions. A snapshot inside a range
  re-anchors the series.
- **FX** (`app.domain.fx.service`): `1 base = rate quote` on `rate_date`. A
  conversion on date `d` uses the latest rate on or before `d` in either
  direction (multiply a direct rate, divide by an inverse one; the later
  date wins, the direct rate on a tie); no triangulation, no look-ahead.
  `Decimal` in a 34-digit context, rounded once, half-even, to 6 decimals; a
  missing rate is `422 fx-rate-missing`. At most one rate per household, pair
  and date.
- **Money** (`app.core.money`): API amounts are decimal strings with at most
  6 decimals and 14 integer digits (no floats, exponents, `NaN`); output
  keeps at least the currency's minor-unit decimals and drops further
  trailing zeros (`"12.34"`, `"1500"` JPY, `"0.1234"`).
- `rule.sort_order` is the evaluation order (`order` is an SQL keyword);
  `conditions` and `actions` are owned by the rules engine.
- `source_document_id` (transaction, balance snapshot) is a plain UUID until
  the documents foundation adds its foreign key to `document`.

- `import_hash` is a stable hash of (account, date, amount, normalised payee,
  running index within day) used for cross-statement deduplication:
  `sha256("perfin-import-v1" US account_id US posted_date US amount US payee
  US index)` in hex, `US` the ASCII unit separator, the amount half-even to 6
  decimals in plain notation, the payee normalised (NFKC, case-folded,
  accents removed, every run of non-alphanumerics one space, trimmed, at most
  200 characters; digits kept), and the index counting earlier rows of the
  **same source** with the same date, amount and normalised payee (so an
  overlapping export with extra rows on that day still matches). Indexed
  with the account, not unique: a duplicate goes to the review queue.
  Specification and tests: `app/domain/transactions/import_hash.py`,
  `tests/unit/test_import_hash.py`.
- Transfers are two transactions sharing a `transfer_group_id`; the matcher
  pairs them by amount, opposite sign, and date proximity across accounts.

## Budgets and goals

```
budget           (household, name, period: monthly|annual|custom, start_date, end_date?,
                  currency, rollover)
budget_line      (budget, category, amount)
goal             (household, name, target_amount, currency, target_date?, achieved_at?,
                  archived, created_at)
goal_account     (goal, account)                      -- accounts[] as m:n
goal_contribution (goal, contributed_on, amount, currency, transaction_id?, memo?)
```

Phase 2 notes (`app/domain/budgets/models.py`, `app/domain/goals/models.py`;
`HouseholdScoped`, household-consistent references as in the ledger):
`end_date` is set exactly for a `custom` budget; a budget line is in the
budget's currency, one per category, and goes with its category; a goal's
progress is shown to a member or viewer only if they can see every account
of the goal.

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
household         (name, base_currency)
job               (queue, type, payload JSONB, state: queued|running|succeeded|failed|dead,
                   priority (lower first), run_after, attempts, max_attempts, locked_by,
                   locked_at, lease_expires_at, last_error, household_id?, dedupe_key?)
event             (household, type, payload JSONB, created)   -- feeds SSE, pruned
audit_log         (seq, household?, user?, action, target_type, target_id, before JSONB,
                   after JSONB, ip, user_agent, request_id, created, prev_hash, hash)
                                                               -- append-only, hash-chained
notification_channel (household, type, config (encrypted), enabled)   -- Phase 7
setting           (scope: system|household|user, scope_id?, key, value JSONB)
```

Phase 0 notes (migration `0001_phase0_foundations`, `app/db/models.py`,
`app/db/rls.py`):

- `household` has forced RLS: a row is readable, updatable and deletable only
  in its own context (an instance admin may also list and create households).
  Every household-scoped table references it `ON DELETE CASCADE`, and
  referential actions bypass RLS, so this is what stops one household from
  deleting another's data. Maintenance that needs every household id calls
  `perfin_household_ids()` (ids only).
- `job` has forced RLS: a household context sees and changes only its own jobs;
  the worker dequeues across households (`FOR UPDATE SKIP LOCKED`, ordered by
  `priority, run_after, id`) inside `app.db.rls.queue_scope`. A trigger keeps
  `id`, `queue`, `type`, `payload`, `household_id`, `dedupe_key` and
  `created_at` immutable, and `payload` must be a JSON object (check
  constraint). Reads on behalf of a user still filter on `household_id`
  explicitly. `dedupe_key` (unique, nullable) makes enqueue idempotent; the
  scheduler uses it so several workers create one job per recurring slot.
- `event` is household-scoped (forced RLS).
- `audit_log` has no foreign keys (rows outlive what they describe). The
  application role may only `SELECT`/`INSERT`; triggers refuse `UPDATE`,
  `DELETE` and `TRUNCATE` for every role. A `BEFORE INSERT` trigger assigns
  `seq`, `created_at` (server clock), `prev_hash` and
  `hash = sha256(prev_hash || canonical JSON of the row)`; `perfin audit
  verify` recomputes the chain. Household rows are visible in that
  household's context, instance-level rows (`household_id` NULL) only to an
  instance admin; instance-level rows are appended only in an instance-admin
  or system-actor context.
- `setting` is unique on `(scope, scope_id, key)` with NULLs not distinct;
  `scope_id` is NULL exactly for `system`. System settings are readable by
  everyone and writable only in an instance-admin context.

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

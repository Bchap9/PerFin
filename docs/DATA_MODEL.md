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
                               custom_denylist[], member_aliases[], token_id, created_by,
                               expires_at, revoked_at, last_used_at)
ai_client ──< ai_call         (tool, params JSONB, status, response JSONB (as sent,
                               already scrubbed), bytes, created)
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
- AI access switches (Phase 6b, `app/domain/ai/switches.py`): the **system**
  setting `ai.enabled` is the instance kill switch and the **household**
  setting `ai.enabled` the household's flag, each `{"enabled": bool}`; a
  missing row means enabled. No new table.
- Learned statement wording (`app/domain/imports/wording.py`): the
  **household** setting `ingestion.learned_wording`, `{"version": 1,
  "entries": [{"layout", "kind", "phrase", "meaning", "learned_at",
  "document_id"}]}` (at most 500 entries, each checked again on every
  read): phrases of a statement layout a person confirmed after the local
  model suggested their kind (`kind`: `action`, `section` or `column`, a
  table's column heading and the role it names). A phrase holds no digit and nothing the
  household's scrubber would mask; no value of any statement is stored. No
  new table.
- `household.base_currency` is an active ISO 4217 code (fund, metal, test and
  withdrawn codes refused; `app.domain.households.schemas.ISO_4217_CURRENCIES`).

AI access notes (Phase 6b, `app/domain/ai/models.py`, AI_ACCESS.md): both
tables are household-scoped. `ai_client.rounding` is NULL, 1, 10 or 100;
`date_range_cap` is days before today (1-3660, NULL for none);
`custom_denylist` and `member_aliases` (terms the scrubber turns into
`[REDACTED]` / `[MEMBER]`, at most 200 each) are identity data themselves
(`classification: identity`); `token_id` is the client's `api_token` (scope
exactly `ai:read`, minted only by `app.domain.ai.service.create_client`;
revoking the client revokes it). `ai_call` stores every `/ai/v1` call with
its tool name, validated parameters, HTTP status, byte count and the body as
sent, refusals included. Every column of every table is classified for AI
access in `app/domain/ai/classification.py` (exposed, free text, denied with
a reason); a new column must be classified there before CI passes.

## Ledger

```
institution           (household, name, country?, fingerprint_rules JSONB,
                       default_parser?, builtin_key?)
account               (household, institution?, type, asset_kind?, subtype?, name, nickname?,
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
rule                  (household, name, sort_order, enabled, stop_processing,
                       conditions JSONB, actions JSONB, hit_count, last_hit_at)
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
  `manual_asset`. `subtype` (optional) says which kind of account it is
  within its type: a Roth, traditional, rollover, SEP, SIMPLE, inherited or
  spousal IRA, a 401(k), Roth 401(k), solo 401(k), 403(b), 457(b), TSP, HSA,
  pension or annuity under `retirement`; an individual, joint, trust,
  custodial (UTMA/UGMA), 529, Coverdell ESA or ABLE account under
  `brokerage`; a HELOC or margin line under `line_of_credit`; and so on
  (`app.domain.accounts.subtypes`, `GET /accounts/subtypes`). It is a short
  code validated against the type by the application (a new subtype needs no
  migration) and gives the account a tax treatment: taxable, tax-deferred or
  tax-free (a brokerage account without one is taxable). Account names in
  imported files (`ROTH IRA`, `529 College Savings`) propose it. `is_asset` defaults from the type (cards, loans, mortgages,
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
  `conditions` and `actions` are owned by the rules engine
  (`app/domain/rules/`): they hold exactly the JSON form of the Pydantic
  discriminated unions in `app.domain.rules.schemas` (conditions `payee`,
  `memo` with `contains|equals|starts_with|ends_with|glob`, `payee_is`,
  `amount`, `sign`, `account`, `currency`, `date`, `category_is`,
  `category_unset`, combinators `all|any|not`, at most 5 levels and 50
  nodes; actions `set_category`, `split_percent`, `split_amount`,
  `add_tags`, `set_payee`, `flag_for_review`, `set_note`, at most one of
  each). There are no regular expressions: `glob` (`*`, `?`) is matched in
  bounded linear-time-per-star code, so a pattern cannot cause ReDoS.
  Evaluation (`app.domain.rules.engine`, pure): enabled rules in
  `(sort_order, id)` order, each against the working state left by the
  earlier matches; the first matching rule wins per field
  (categorisation, payee, notes), tags accumulate, `flag_for_review` moves
  `none` to `pending` only; a matching rule with `stop_processing` ends the
  run. `hit_count`/`last_hit_at` count matches when rules are applied (not in
  a dry run). Ids a rule names must exist when it is saved; an object
  deleted later leaves a dangling id that never matches and whose actions
  are skipped. At most 500 rules per household.
- `source_document_id` (transaction, balance snapshot) references `document`
  `ON DELETE SET NULL`, plus the deferred household-consistent key of
  ADR-0008 (`household_ref_fk`); deleting a statement keeps the rows it
  produced.

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
  Rules (`app/domain/transactions/transfers.py`): automatic pairing needs
  two unlinked transactions on different accounts, in the same currency,
  with exactly equal and opposite non-zero amounts and posted dates at most
  `window_days` apart (default 3, at most 14); the closest dates win, and a
  leg with two equally close candidates on *different* accounts is left
  unpaired (counted as ambiguous) rather than guessed. A person may link
  two legs by hand regardless of the window, also across currencies (an
  exchange: any amounts of opposite sign). Unlinking or deleting a leg
  dissolves the whole group, and needs edit on every leg (`409
  transfer-conflict` otherwise). A user-triggered run only touches accounts
  the user may edit; the run after an import (`transactions.auto_pair` job,
  seeded with the document's transactions) only the accounts the document's
  uploader may import into (every account for an owner's or admin's
  document, or one without an uploader).
- Reports leave out transactions with `review_state = pending` (still in
  the review queue) unless the household setting
  `reports.include_pending_review` is `true`
  (`app.domain.transactions.service.reportable_clause`).

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
budget's currency, one per category, and goes with its category.

- **Budget periods** (`app/domain/budgets/reports.py`): monthly and annual
  budgets start on the first day of a month; periods are calendar months,
  or twelve months from `start_date` (a fiscal year); a custom budget is the
  single period `start_date..end_date` (at most 3660 days). A line on a
  category counts that category and its subcategories that have no line of
  their own (nearest line wins). Line amounts are not versioned: rollover
  applies today's amounts to earlier periods.
- **Rollover**: expense lines of a `rollover` budget carry `available -
  actual` (negative when overspent) into the next period, starting from the
  budget's first period and looking back at most ten years.
- **Forecast**: a past period's forecast is its actual; the current one adds
  the linear projection of the lines that do not match an active
  `scheduled_transaction` (same account, payee and category) plus the
  occurrences of active schedules due after today (at the midpoint of
  `amount_min..amount_max`); a future period is actual plus scheduled.
- **Goal visibility**: a goal (with its progress and contributions) is
  visible to a member or viewer only if they can see every account of the
  goal **and** every account of a transaction linked to one of its
  contributions; otherwise it is `404 goal-not-found` as a whole (stricter
  than hiding only the progress, so a goal never reveals a hidden account's
  money). `goal_contribution.currency` always equals the goal's (the goal's
  currency is locked while it has contributions); a linked transaction is on
  one of the goal's accounts when linked and is linked once per goal.
  `achieved_at` is maintained by the service (set when the contributions
  first reach the target, cleared when they fall below it).
- **Report lines** (`app/domain/reports/ledger.py`): reports and budget
  actuals read one line per unsplit transaction or per split, on visible
  accounts, without transfer legs and `transfer`/`ignore` categories;
  uncategorised lines count as expense when negative and income otherwise.
  Dates are posted dates; foreign amounts are converted per posting day.

## Investments

```
security          (household, symbol?, isin?, cusip?, name, asset_class, sector?, exchange?,
                   currency)
holding           (account, security, quantity, cost_basis, currency, as_of)  -- derived cache
lot               (account, security, open_date, quantity, remaining_quantity, cost_per_unit,
                   currency, closed_date?, opening_txn_id?)
investment_txn    (account, security?, type: buy|sell|dividend|interest|fee|split|
                   transfer_in|transfer_out|reinvest|corporate_action,
                   trade_date, settle_date?, quantity?, price?, amount, fees, currency,
                   lot_links JSONB, memo?, import_hash?, source_document_id?)
price             (security, price_date, close, currency, source)
investment_account (account, cost_method: fifo|average)  -- per-account settings
```

Phase 5 notes (`app/domain/investments/models.py`, building blocks in
`app/domain/investments/service.py`; every table `HouseholdScoped` with
forced RLS and household-consistent references, ADR-0008):

- **Units**: quantities and per-unit prices/costs are `NUMERIC(28, 10)`
  (`app.db.types.QUANTITY`, `UNIT_PRICE`), money `NUMERIC(20, 6)`.
- **security** is the household's own catalogue (no instance-wide table:
  another household never learns that a ticker is held).
  `asset_class` is one of equity, etf, mutual_fund, bond, cash_equivalent,
  option, crypto, real_estate, commodity, other. `(household, symbol,
  exchange)` is unique among securities with a symbol (a NULL exchange
  counts as one value; partial unique index, migration `0012`). `symbol`
  is NULL for a security an imported statement printed by its name alone
  (an unlisted partnership, a 529 portfolio): the statement importer
  matches those by their exact normalised name (NFKC, case-folded, spaces
  collapsed) and fills the symbol in when a later statement prints one;
  the API creates securities only with a symbol. `isin` (12,
  check digit last) and `cusip` (9) are format-checked and indexed, not
  unique. `currency` is the quote currency: `price.currency` must equal it
  (composite key on `(household_id, id, currency)`). A security with
  transactions or lots cannot be deleted (`RESTRICT`); its prices and
  holding cache rows go with it.
- **price**: at most one per security and date; `close > 0`; `source` is
  `manual` or a provider name. Lookups take the latest price on or before
  the day (no look-ahead).
- **investment_txn**: `amount` is the signed cash effect on the account in
  the account currency (buy negative, sale/dividend/interest positive, fee
  negative); `fees` (>= 0) is the part of it that was fees; `quantity`
  (units, > 0 for buy/sell/reinvest/transfer_in/transfer_out) and `price`
  (one unit's, in the security currency; a bond trade quoted per 100 of
  face value is stored divided by 100) describe the trade. buy, sell, split,
  transfer_in, transfer_out and reinvest must name a security; dividend,
  interest, fee and corporate_action may. `currency` equals the account's.
  `lot_links` is a JSON array of `{"lot_id", "quantity"}` (positive decimal
  strings, each lot once; `parse_lot_links`): the lots a sell/transfer_out
  consumed or the lot a buy/reinvest/transfer_in opened. `import_hash`
  (64 hex) dedupes brokerage statement rows like `transaction.import_hash`:
  sha256 of `perfin-invest-v1`, account, trade date, type, security,
  units, amount and the running index among identical rows (no `FITID`,
  so the same month as OFX and as PDF matches;
  `app/domain/investments/statement_import.py`). Units held before the
  first imported statement enter as an opening `transfer_in` (amount 0,
  memo `Opening position from statement`). A positions export (what an
  account holds on a day) brings the holdings to what it lists with
  `transfer_in` / `transfer_out` rows on that day (memos `Units held per
  positions export` / `Units no longer held per positions export`;
  INGESTION.md "Brokerage positions exports").
  The design's `date` is `trade_date` (`settle_date` optional, not before
  it).
- **Business dates are bounded below** (SECURITY.md F8, security review
  P56-SR-R1-01): `price.price_date`, `investment_txn.trade_date`,
  `lot.open_date`, `loan.origination_date`, `loan_payment.paid_on`,
  `loan_rate_change.effective_date`, `asset_valuation.valued_on` and
  `fx_rate.rate_date` carry a CHECK `>= '1900-01-01'` (`*_min`,
  `app/core/dates.py`; `settle_date` follows from `settle_after_trade`).
- **lot**: `quantity` opened (> 0), `remaining_quantity` still held (0 ..
  `quantity`); `closed_date` is set exactly when nothing remains. Cost per
  unit is in the account currency. `opening_txn_id` is a `SET NULL`
  reference (plus the deferred household check) to the transaction that
  opened it.
- **holding** is a derived cache, one row per account and security,
  written only by `rebuild_holdings` (sum of remaining lot quantities, cost
  basis = sum of `remaining_quantity * cost_per_unit` rounded once to 6
  decimals, in the account currency). Never exported.
- **Visibility**: holdings, lots and investment transactions are
  account-derived; reads start from `visible_holdings` / `visible_lots` /
  `visible_investment_txns` (the principal's visible accounts only).
- **Lot engine** (`app/domain/investments/lots.py`, pure): lots,
  `lot_links` and the `holding` row of one (account, security) pair are
  written only by `service.replay_position`, which replays the pair's
  transactions in trade order (on one day: splits, openings, cash rows,
  closings; then entry order) every time one of them changes (incremental
  per pair; `replay_account` after a cost-method change). A lot keeps its
  id across replays (keyed by its opening transaction), so specific-lot
  references stay valid. Opening cost (account currency): buy `-amount`;
  reinvest `-amount`, or `quantity * price` converted plus fees when no
  cash moved; transfer in `quantity * price` converted plus fees (`price`
  is the carried-over cost per unit). Closings: `fifo` (oldest lots first)
  unless `lots_specified` (the person named the lots in `lot_links`),
  or `average` (`investment_account.cost_method`: units leave first in
  first out for the holding period, at the average cost of every open
  unit, and the remaining lots then carry that average). Sale proceeds are
  shared between the consumed lots by units (6 decimals, remainder to the
  last). Holding period: long term when sold after the first anniversary
  of the open date. Selling more than is held is refused (no shorts).
- **Splits**: a `split` (or a `corporate_action` with a ratio) stores its
  ratio exactly as `quantity` new units per `price` old units (3-for-1:
  3/1; 1-for-10 reverse: 1/10); open lots are rescaled (quantities to 10
  decimals, cost per unit divided, total cost unchanged up to that
  rounding). A split stored with `quantity` only (units added, as the
  brokerage statement import records it) has the ratio `(held + added) /
  held` at that point of the replay. Other corporate actions (mergers,
  spin-offs) move cash only.
- **Statement seed lots**: a lot without `opening_txn_id` (units a
  brokerage statement reported that no imported activity explains) enters
  the replay like a transfer in on its open date and keeps its id; the
  splits after it are taken back out of its stored quantity first. A split
  recorded as units added after such a lot has no fixed ratio, so replaying
  that pair is refused (`409 lots-not-replayable`). Deleting a transaction
  deletes the lots it opened first (the `SET NULL` would otherwise make
  them look like seeds).
- **investment_account**: one optional row per account (`fifo` without
  one); forced RLS, household-consistent key to `account`, deleted with it.
  Not in export format v1 yet (PORTABILITY.md).
- `investment_txn.lots_specified` (boolean, default false) marks a sale or
  transfer out whose `lot_links` were chosen by the person; the engine
  rewrites the `lot_links` of every other closing on each replay.

## Loans

```
loan              (account, principal_original, currency, rate, rate_type: fixed|variable,
                   term_months, origination_date, payment_amount, payment_day,
                   escrow_amount?, lender?)
loan_payment      (loan, paid_on, principal, interest, escrow, fees, currency,
                   transaction_id?, source_document_id?)
loan_rate_change  (loan, effective_date, new_rate)
```

A loan is also an `account` of type `loan`, `mortgage` or `line_of_credit`
(`app.domain.loans.service.LOAN_ACCOUNT_TYPES`, checked by the service) so
it appears in net worth with its current balance; one `loan` row per
account, deleted with it. Phase 5 notes (`app/domain/loans/`):

- `rate` and `new_rate` are the annual nominal rate as a **fraction**
  (`0.06125` = 6.125 %, `NUMERIC(10, 8)`, `0 <= rate < 1`). The rate in force
  on a day is the latest `loan_rate_change` on or before it, else `rate`
  (`rate_on`); at most one change per loan and date.
- Money columns are in the account currency (composite keys carry
  `currency` from account to loan to payment). `term_months` is 1..1200,
  `payment_day` 1..31 (29-31 fall back to the month's last day).
- `loan_payment` parts are non-negative; `transaction_id` (the ledger row
  that paid it) and `source_document_id` (the statement) are `SET NULL`
  references with the deferred household check. The design's `date` is
  `paid_on`.
- `lender` is a display name. Loan numbers are identity data
  (AI_ACCESS.md) and are not stored.
- A loan is visible to a member or viewer exactly when its account is
  (`visible_loans`).
- Schedules are computed, never stored (`app/domain/loans/amortization.py`,
  exact `Decimal`, interest rounded to the minor unit per period, the final
  payment absorbing the rounding; API.md "Loans notes").
- **Balance sync**: the loan account's balance is the principal owed (a
  negative balance on the liability side). `app.domain.loans.service`
  owns the account's `computed` `balance_snapshot` rows: the original
  principal on the origination date and, for each day with payments, the
  balance at the end of the day before less the principal paid. The day
  before origination has no balance: payments made on the origination
  date come off the original principal, unless a `statement` or `manual`
  snapshot dated earlier gives a balance (read like on any other day).
  The snapshots are rebuilt after every change to the loan or its
  payments and deleted with the loan. Loan statements add `statement`
  snapshots (previous and new principal balance), which beat `computed`
  ones on the same day, so a balance read from a statement anchors the
  chain.

## Manual assets and liabilities

```
manual_asset      (account, description?, valuation_method:
                   manual|appraisal|purchase_price|index|provider)
asset_valuation   (account, valued_on, value, currency, source: manual|provider)
```

Phase 5 notes (`app/domain/assets/`): `manual_asset` details an account of
type `manual_asset` (one per account). The design's `kind` is
`account.asset_kind` (property|vehicle|other), which every manual asset
account already has, so it is not repeated. `description` is free text
(`classification: identity`: it may hold an address, so it is kept out of `ai:read` views, audit rows and events). `asset_valuation` values a manual asset or
manual liability account on a date: a non-negative magnitude in the account
currency (`account.is_asset` decides the side), at most one per account,
date and source; on one date `manual` beats `provider` (`latest_valuation`).
Manual liabilities need no table of their own (`manual_liability` accounts
valued by `asset_valuation` or `balance_snapshot`). The design's `date` is
`valued_on`.

## Net worth

```
net_worth_snapshot (household, as_of_date, currency, assets, liabilities, net, breakdown JSONB)
```

Computed daily by the worker from `balance_snapshot`, `holding` x `price`,
`loan` balances and `asset_valuation`, converted to the household base
currency (`currency`, the base currency on that day). Phase 5 notes
(`app/domain/networth/`): one per household and date; `assets` and
`liabilities` are non-negative magnitudes and `net = assets - liabilities`
(check constraint). `breakdown` is a JSON object owned by the net worth
service (per account: side, amount, currency, base amount; unconverted
currencies). Because it names every account, a stored snapshot is shown
only to a principal who sees every account (`sees_household_totals`:
owners, admins, or members/viewers granted every account); anyone else gets
figures recomputed over their visible accounts. Exported as the stored
history (PORTABILITY.md, format version 2; the ids in `breakdown` are
re-mapped on import).

How a day is valued (`app/domain/networth/compute.py`, one implementation for
the daily job, the backfill and recomputed views):

- Accounts with `include_in_net_worth` false, opened after the day or closed
  before it are left out.
- Manual balance accounts: the newer of the balance anchor and the latest
  `asset_valuation` (a magnitude signed by `account.is_asset`; the anchor
  wins a tie). Investment accounts (brokerage, retirement) with activity:
  cash plus holdings, where holdings are the lot engine's units at the end
  of the day times the close (stored price or latest trade price, no
  look-ahead) and cash moves with ledger transactions and
  `investment_txn.amount`; a balance anchor (a statement's whole-account
  value) carries forward as `anchor + holdings(d) - holdings(anchor day) +
  cash moves since`. Loan accounts use their `computed` snapshots (kept by
  the loans service), falling back to `principal_original` minus paid
  principal when there is no anchor yet. Everything else: the ledger
  balance.
- Positive values are assets, negative ones liabilities (by sign, not by
  account type), converted to the base currency on that day; an account
  whose currency has no rate is listed as unconverted and left out.
- `breakdown` (version 1): `{"version", "by_class": {"assets": {class:
  amount}, "liabilities": {...}}, "by_type": {account_type: signed amount},
  "accounts": [{"account_id", "type", "class", "side", "amount", "currency",
  "amount_base"}], "unconverted": [{"account_id", "amount", "currency"}],
  "unpriced_security_ids"}`; amounts are decimal strings. Ids, types and
  amounts only: no names or free text.
- The daily `networth.snapshot` job upserts the day's row and backfills the
  days missing since the last snapshot (from the first data on a first run,
  at most ten years).

Recurring detection (`app/domain/recurring/`) feeds `scheduled_transaction`
with `origin = detected`: transactions of the last 1100 days (transfers
excluded) grouped by account, payee (or normalised payee text) and
direction, clustered by amount (10 % + 1.00), with the cadence from the
median gap (weekly, biweekly = weekly x 2, monthly, quarterly, annual =
yearly) and a confidence from regularity, amount stability and count.
Manual schedules are never changed; a detected schedule keeps the name and
`active` flag a person set. Confidence is not stored (computed per run).

## Documents and ingestion

```
document          (household, kind: statement|receipt|invoice|tax_form|payslip|
                   insurance|other|unknown, status: received|scanning|extracting|
                   classifying|parsing|needs_review|processed|rejected|failed,
                   source: upload|dropfolder|email|api, source_meta JSONB,
                   blob_id, sha256, size, mime_detected, original_filename,
                   uploaded_by?, account_id?, institution_id?,
                   period_start?, period_end?, tax_year?, extracted_text (tsvector,
                   GIN, written by the worker),
                   extraction JSONB, confidence JSONB, parser_version)
blob              (household, store, key, area: quarantine|permanent, size, stored_size,
                   enc_alg, key_version, wrap_nonce, wrapped_dek, nonce, shredded_at?,
                   created)
document_link     (document, target_type: transaction|investment_txn|loan_payment|
                   account|tax_year, target_id)
review_item       (household, document?, kind: unknown_owner|unknown_account|
                   low_confidence_field|possible_duplicate|unparsed|new_institution|
                   rejected_email|receipt_match, payload JSONB,
                   state: open|resolved|dismissed,
                   resolved_by?, resolution JSONB)
parser_fingerprint(institution, kind, match rules JSONB, parser name, version,
                   learned_from_document_id?, template JSONB?: a learned layout
                   ("Teach PerFin": field anchors as label text + direction,
                   table column headers + roles, ignored lines, notes without
                   digits; never a value of the document; a new version of the
                   same layout carries the earlier template forward))
ingest_job        (document, stage, attempt, started, finished, error, log JSONB)
import_mapping    (household, name, institution_id?, account_id?, header_fingerprint,
                   spec JSONB, created_by?, use_count, last_used_at?,
                   sign_unverified)
import_hash_alias (household, account, transaction, import_hash)
statement_record  (household, account, document, kind: bank|investment,
                   outcome: imported|skipped, period_start, period_end,
                   period_stated, source_format, currency, closing_balance?,
                   closing_cash?, holdings_value?, total_value?)
dropfolder_route  (household, folder slug, user, account?)
dropfolder_entry  (household, path_key sha256, rel_path, size, mtime_ns, inode,
                   sha256?, document?, outcome: accepted|rejected|skipped, reason?,
                   after_state: pending|done|kept|error, after_result?)
```

`dropfolder_route` and `dropfolder_entry` (`app/domain/dropfolder/models.py`)
are the drop folder's attribution and idempotency ledger (INGESTION.md "Drop
folder specifics"): a route says who files under a subfolder slug belong to
(written by the `unknown_owner` resolution with `remember`); an entry is one
inbox file the watcher took or refused, unique by (path hash, size, mtime,
inode), with the move/delete still to do (`after_state = pending`) until the
transaction that recorded it has committed. `rel_path` is metadata only.
Neither is exported (household-specific mount state).

Notes on the documents foundation (`app/domain/documents/`):

- Every table above is household-scoped (forced RLS) with household-consistent
  references (ADR-0008); `document.blob_id` is `NO ACTION` (a blob cannot go
  while a document uses it); links, review items and ingest history cascade
  with their document; `transaction.source_document_id` references `document`
  `ON DELETE SET NULL`.
- `blob` locates the ciphertext (`store`, random UUIDv4 `key`, `area`) and
  holds the per-object data key wrapped by the master key (`key_version`,
  `wrap_nonce`, `wrapped_dek`, associated data = the blob id). The payload is
  a libsodium secretstream (XChaCha20-Poly1305, 64 KiB chunks); `nonce` is
  its 24-byte header (`enc_alg = xchacha20poly1305-secretstream-64k`). `size`
  is the plaintext size, `stored_size` the ciphertext size. Deleting a
  document nulls the wrapped key (`shredded_at`) and then deletes the rows;
  the files are unlinked after commit.
- `document.mime_detected` is the magic-byte sniff result
  (`app.ingestion.intake.sniff.FileType` values); `document.error` is a short
  machine-readable failure (`extraction failed: <code>`). `extraction` is the
  validated sandbox output (`app.ingestion.sandbox.protocol.ExtractResult`),
  which only ever carries the last four digits of an account number.
  For PDFs and images it holds the pages (words with boxes, lines, tables,
  text, OCR facts) but never image bytes: page previews are separate blobs
  (permanent area, encrypted like any document) listed in
  `extraction.stored_previews` (page, blob id, width, height, DPI, `scale`
  from page units to preview pixels; `app/domain/documents/previews.py`).
  These blobs have no `document` row of their own; deleting or re-extracting
  the document shreds and removes them. For EML and ZIP it lists the
  children in `extraction.stored_children` (origin, name, size, the child's
  `document_id` or the intake's rejection reason).
- `document.source_meta` of a document found inside an EML or ZIP is
  `{parent_document_id, origin: attachment|inline|zip_member, depth,
  content_id?}`; `depth` (1) is what the pipeline extracts it at, so a
  container inside a container is not expanded (SECURITY.md D4).
- `document.extracted_text` is the full-text index (`tsvector`, config
  `simple`, GIN index `ix_document_extracted_text`), written only by the
  worker from the validated extraction (`app.domain.documents.search.
  update_search_index`: pages or text, an email's subject, structured table
  cells; at most 500 000 characters, a shorter prefix when the vector would
  exceed PostgreSQL's 1 MB limit) and cleared when an extraction fails.
  It is not exported (rebuilt by re-extraction) and never returned by the API;
  snippets are computed from `extraction.text` per request (API.md).
- `review_item.state` is `open` exactly while `resolved_at` is NULL.
- `import_hash_alias` (`app/domain/review/models.py`) records "these two
  imported rows are the same transaction": confirming a `near_match`
  `possible_duplicate` deletes the imported row and keeps its `import_hash`
  for the transaction that stays (on the kept row itself when it has no
  hash, else as an alias row; unique per household, account and hash;
  household-consistent references to `account` and `transaction` that
  cascade). `app.domain.review.repo.alias_rows` lists the aliases of an
  account in a date range for the deduplicator.
- Review resolutions are typed per kind (`app.domain.review.schemas`; API.md
  "Review notes") and stored in `review_item.resolution` as ids and
  decisions only.

Notes on structured imports (`app/domain/imports/`, Phase 2):

- `statement_record` (revision 0010, not in the API) is one statement the
  importer committed for an account, or one a person said is the same as a
  statement already imported (`outcome = skipped`): the statement-level
  duplicate guard (INGESTION.md "Match and deduplicate", "Same statement
  twice"). `period_stated` is true for a period an OFX/QFX file declares or
  a document parser read from the page, false for the span of a CSV's or
  QIF's rows (recorded, never compared). Closing figures: a ledger
  statement's closing balance; a brokerage statement's closing cash,
  holdings total and account value (`NUMERIC(20, 6)`, the account's
  currency). Unique per account, document, kind and period start; both
  references are household-consistent `CASCADE` keys, so a record goes with
  its document or account. Not exported in a full export (instance import
  history, like `ingest_job`).

- `import_mapping` is a saved column mapping for one CSV/TSV layout. `spec`
  is `app.domain.imports.schemas.MappingSpec` (zero-based columns for the
  date and its format, the amount as one signed column with a sign
  convention, an unsigned column plus a debit/credit indicator, or debit and
  credit columns; payee, memo, running balance, currency column or fixed
  currency; decimal separator; preamble rows to skip; header or not),
  validated on every write. `header_fingerprint` is SHA-256 over the
  version, column count and normalised header cells (or, without a header,
  the shape of the first row) of row `spec.skip_rows`
  (`app.ingestion.parsers.structured.header_fingerprint`): a new table with
  that fingerprint is imported with the mapping automatically.
  `account_id`/`institution_id` are optional household-consistent `SET NULL`
  references (ADR-0008); the account is where files of this layout go when
  the upload names none. Name unique per household. A mapping on an account
  the caller cannot see is invisible to them (`404`). `sign_unverified`
  (revision 0008, not in the API) marks a mapping the importer saved from a
  suggestion whose single signed amount column was read on trust: it is not
  applied to a liability account on its own until a person saves its spec.
- Imported transactions carry `source_document_id` and `import_hash`; a near
  duplicate (same account, date and amount, no hash match) is written with
  `review_state = pending` next to a `possible_duplicate` review item. A
  statement's closing balance (OFX `LEDGERBAL`, the last running balance of a
  CSV) becomes a `balance_snapshot` with `source = statement` and
  `source_document_id`. A brokerage statement's first import for an account
  also writes one for the day before its period, from the *Beginning
  Account Value* its own summary prints, when that summary adds up to the
  closing value and no snapshot exists from that day to the period's end.
- Review item payloads of the importer (ids, counts and row numbers only):
  `unparsed` (`mapping_required`, `mapping_ambiguous`, `mapping_failed`,
  `no_records`), `unknown_account` (`account_not_found`,
  `account_ambiguous` with candidate ids, `account_required`,
  `currency_mismatch`; a multi-account CSV's with `group` and
  `spec_digest`), `possible_duplicate` (`near_match`:
  `transaction_id`, `candidates`), `low_confidence_field` (`rows_skipped`,
  `balance_mismatch`, `date_order_assumed`, `currency_mismatch`,
  `truncated`). The latest import's counts are the `log` of the document's
  last finished `parse` `ingest_job`.
- PDF and image statements (document parsers, Phases 3-4; INGESTION.md
  "Parse"): the parsed statements are `extraction.statements` with
  `source_format` `pdf`/`image` (rows with `confidence`, `low_confidence`,
  `page`, `bbox`; the statement with `opening_balance`, `parser`,
  per-field `confidence`, `reconciliation`; in a document of several
  accounts `first_page`/`last_page` and the account's printed `name`,
  `account_type` and `subtype`, never more than its last four digits), and
  `extraction.parse` records
  the parser run (parser and version, scores, threshold, pending and skipped
  line counts, warnings, a `payslip`). `document.parser_version` is the
  parser's `name/version`; `document.confidence.parse` summarises the run
  next to the classifier's fields. Rows below the confidence threshold, or
  of a statement that does not reconcile, are `transaction.review_state =
  pending` with `transaction.confidence` (0..1, the row's lowest field).
  Their review payloads are `low_confidence_field` with `low_confidence`
  (`fields`, `parser`, `count`, `transactions`: id, fields, confidence,
  page), `reconciliation_failed`/`reconciliation_unavailable` (`opening`,
  `closing`, `total`, `difference` as decimal strings, `running_mismatches`
  count and row indices, `rows_in_doubt` of `rows_total`: only the rows the
  statement's running balance does not vouch for are pending),
  `lines_skipped` (`count`, `lines`: page and box where known),
  `accounts_not_read` (`accounts`: the last four digits of accounts the
  document's own index lists and no statement was read for) and, for payslips,
  `payslip_does_not_add_up`/`low_confidence` (`fields`, `adds_up`). A
  statement that does not reconcile writes no balance snapshot. Brokerage
  statements are `extraction.investments` (positions, activity, cash and
  totals, `parser`, `confidence`, `reconciliation`, the pages and account
  facts as above; INGESTION.md
  "Brokerage statements"); their items are `investment_reconciliation_failed`
  (`checks`, `failed`, `positions`, cash and totals as decimal strings,
  `differences`, `sections`, `lines`, `unread_summary_lines`),
  `investment_low_confidence` (`fields`, `positions`, `activity`, `lines`),
  `investment_lines_held` (the lines and holdings of an imported
  statement held back: `fields`, `lines`, `positions`, `sections`, `held`,
  `imported`, `selectable`),
  `investment_figures_differ` (a statement imported whole whose own totals
  disagree: `failed`, `differences`, `sections`; a notice, it does not keep
  the document in `needs_review`),
  `plan_statement_does_not_add_up` (a 401(k) plan statement whose balance
  was not recorded: `status`, `failed`, `period_stated`, `printed`,
  `computed`, `difference`, `currency`; never importable as read),
  `investment_opening_basis_unknown` (`securities`; `costed_at: "price"`
  for a statement's holding opened at its price),
  `investment_short_position` (`positions`: symbol, name, quantity, value;
  `lines_left_out`),
  `position_mismatch` (`securities`: `history`/`reported` units),
  `investment_lot_conflict` (`code`), `investment_out_of_order`
  (`rows`), `investment_same_name_securities` (`names`) and
  `investment_date_out_of_range` (`earliest_allowed`), each
  with `account_id`; nothing of a held statement is written.

## Tax vault

```
tax_year          (household, year, jurisdiction, filing_status?, state: open|filed|closed,
                   notes?)
tax_checklist_item(tax_year, expected_form: W-2|1099-INT|1099-DIV|1099-B|1098|...,
                   issuer?, status: expected|received|not_applicable,
                   origin: manual|prior_year|detected, document_id?)
tax_category_map  (household, category, jurisdiction, schedule, line?)
                                          -- category -> schedule/line hint
tax_form_data     (document, tax_year (printed), tax_year_id?, jurisdiction, form_type,
                   form_index, issuer_name?, boxes JSONB, source: parsed|manual,
                   confidence?)
tax_export        (tax_year_id?, year, state: pending|ready|downloaded|expired|failed,
                   job_id, requested_by?, blob_id?, size?, sha256?, expires_at?,
                   finished_at?, downloaded_at?, error?, summary JSONB)
```

Phase 6 notes (`app/domain/tax/models.py`, `app/domain/tax/service.py`,
`app/domain/tax/forms/`; all `HouseholdScoped`, forced RLS,
household-consistent references):

- **Form sets** (`app.domain.tax.forms`): a pluggable registry of
  jurisdictions. A `FormSet` has a `jurisdiction` code (ISO 3166-1 alpha-2,
  optionally `-` subdivision), a name, its `filing_statuses` and a
  `FormDef` per form type; each form lists its boxes (`id`, `label`,
  `kind`: `amount`, `coded_amount`, `text`, `checkbox` or `identity`). `US`
  ships W-2, 1099-INT, 1099-DIV, 1099-B, 1099-MISC, 1099-NEC, 1099-R,
  1099-G, 1099-SA, 1098, 1098-E, 1098-T and 1095-A/B/C. A new jurisdiction
  is a module in that package listed in its `_BUILTIN` (or `register(...)`).
  The service validates `jurisdiction`, `filing_status`, `expected_form` and
  `form_type` against the registry; the database checks their format only.
- **tax_year**: one per household and calendar year (`/tax-years/{year}`),
  1900..2200. `state` is the workflow state, not a US state.
- **tax_checklist_item**: unique per year, form and issuer (NULL issuers
  count as one); `document_id` (`SET NULL` reference) only while `received`.
  `origin` tells items learned from prior years from manual and detected
  ones.
- **tax_category_map**: one hint per category and jurisdiction (e.g.
  `Schedule A` / `5a`), deleted with the category.
- **tax_form_data** holds the amounts of one form read from a document (a
  consolidated 1099 holds several: `form_index` numbers them per type,
  unique per document, type and index); it goes with its document.
  `boxes` maps box ids to decimal strings, **amounts only**: identity boxes
  (SSN/TIN/EIN, names, addresses, ZIP codes, account, control and policy
  numbers, state employer ids) and text/checkbox boxes are discarded by
  `sanitize_boxes` before a row is written (`record_form_data` is the only
  writer); a coded amount (W-2 box 12, 1095-C line 15) is stored as
  `<box>_<code>` (`12_D`, `15_ALL`). A check constraint refuses any key
  that is not `[A-Za-z0-9_]{1,32}`, any value that is not a decimal string
  fitting `NUMERIC(20, 6)` and more than 128 boxes, even if the service is
  bypassed. `issuer_name` (control characters removed, at most 200
  characters) is the only text kept from a form. Roll-ups over a tax year
  include only the accounts a member or viewer may see.
- Besides the printed boxes the US set stores three derived ones: `14t`
  (W-2 box 14 "Other": the sum of its amounts; the labels are text and are
  dropped), `10t` (1098 box 10 "Other": its amount, e.g. property tax) and
  `cov_<MON>` / `cov_ALL` on 1095-B/C (`1` for each month with coverage).
  `FormDef.issuer_box` names the identity box printing the issuer (W-2 box
  `c`, the 1098 lender box...), from which parsers take the display name.
- A parsed tax form's stored extraction (`document.extraction`) and search
  vector keep no identity text either: the worker masks everything but form
  vocabulary, amounts, years, box ids and codes (INGESTION.md "Parse").
- **tax_export**: one accountant export (API.md "Tax vault notes"), keyed by
  its job id; the zip is a blob (encrypted at rest) until it is downloaded
  once or expires, then the blob's key is destroyed and the row's `blob_id`
  becomes NULL. Like `portability_archive` it is not part of the
  household export (NOT_EXPORTED).

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
notification_channel (household, name, type: smtp|webhook|ntfy|gotify|matrix,
                   config_encrypted (whole configuration incl. credentials, bound to
                   the row), events JSONB (subscribed kinds), enabled, last_sent_at?,
                   last_outcome?)                              -- Phase 7; not exported
setting           (scope: system|household|user, scope_id?, key, value JSONB)
mail_intake       (household (unique), enabled, host?, port, username_encrypted?,
                   password_encrypted?, inbox_folder, processed_folder, rejected_folder,
                   alias_encrypted, alias_rotated_at, allowed_senders JSONB,
                   member_addresses, require_auth_results, authserv_id?,
                   last_poll_at?, last_poll_outcome?, last_poll_counts JSONB)   -- Phase 4
mail_intake_message (household, message_key (unique per household), outcome:
                   accepted|rejected, reason?, document?)                       -- Phase 4
idempotency_key   (household, user_id, key, request_hash, status_code?, response_body JSONB?,
                   created_at, expires_at)                     -- Phase 2
portability_archive (household, kind: export|import, state: pending|ready|downloaded|
                   expired|imported|failed, job_id (unique), requested_by?, blob?,
                   passphrase_protected, passphrase_key? (encrypted), size?, sha256?,
                   expires_at?, finished_at?, downloaded_at?, error?, summary JSONB)
                                                               -- Phase 2
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
- `idempotency_key` (Phase 2, `app.core.idempotency`) is household-scoped
  (forced RLS) and unique on `(household_id, user_id, key)`; `request_hash`
  is SHA-256 over the route, path parameters and body. The row is inserted
  before a creating `POST` runs and completed with its status and response
  body in the same transaction, so a failed request leaves nothing and a
  concurrent duplicate waits on the unique index. Rows live 24 hours;
  expired rows of a household are deleted whenever it stores a new key.
  `user_id` has no foreign key (the row is short-lived).
- `portability_archive` (Phase 2, `app/domain/portability/`,
  [PORTABILITY.md](PORTABILITY.md)) is household-scoped (forced RLS): one
  full export or import, keyed by the job that does it. `blob_id` is a
  household-consistent `SET NULL` reference to the archive bytes in the
  blob store (`permanent` for a ready export, `quarantine` for an uploaded
  import) and becomes NULL once the archive was downloaded, expired or
  imported (the blob's key is destroyed first). `passphrase_key` holds the
  Argon2id key derived from an export's or import's passphrase, as an
  envelope under the master key bound to the row id (column info
  `secret`), only until the job has used it. `summary` holds counts and,
  for a failed import, a short `error_detail`; never file content.

- `mail_intake` and `mail_intake_message` (Phase 4,
  `app/domain/intake_settings/models.py`) are household-scoped (forced RLS).
  `mail_intake` holds one household's receipts mailbox: the username, app
  password and alias token are envelopes under the master key bound to
  `column_ad("mail_intake", <column>, id)` (column info `secret`); enabling
  requires host and credentials (check constraint). `allowed_senders` is a
  JSON array of `{"address", "user_id"}`. `mail_intake_message` is the
  mailbox ledger and dedupe index: `message_key` is SHA-256 over the
  `Message-ID` and `From` (or the whole header block), `document_id` a
  household-consistent `SET NULL` reference to the `.eml` document of an
  accepted message. Neither table is exported (instance state, secrets).

## Encryption at the column level

Columns encrypted with the application master key before storage: IMAP
credentials, notification channel secrets, API provider keys, full account
numbers (if ever stored; default is last four only), MFA TOTP secrets. Argon2id
hashes and WebAuthn public keys are not encrypted (they are not secrets).

## Retention and deletion

Deleting a document deletes the blob and dependent rows; deleting a transaction
keeps the audit row. A household owner can run a full export
([PORTABILITY.md](PORTABILITY.md)) then a full purge.
Purge zero-fills nothing; it relies on encryption: destroying the wrapped DEKs
renders blobs unreadable even on media that cannot be securely erased.

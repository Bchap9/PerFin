# AI access boundary (MCP server and on-prem models)

## Principle

PerFin itself never calls a language model, hosted or local. Files and
extracted document text have no code path off the premises.

AI assistance is provided *to* an assistant, not *by* one, through a read-only
**MCP server**. The rule it enforces is simple:

> **Financial facts are exposed. Identity is not.**

An assistant may see what you spent, where, on what, how much you saved, what
you owe and what you own. It may never see who you are, where you live, your
date of birth, your SSN, or any account number.

## The identity denylist

These are **never** returned by the MCP server or the `ai:read` API scope, in
any mode, in any field, to any client:

| Class | Examples | How it is kept out |
| --- | --- | --- |
| Government identifiers | SSN, ITIN, EIN, passport, driver's licence numbers | Never stored in exposable columns; pattern scrubber on free text |
| Account identifiers | Full account numbers, last-4 digits, routing numbers, card numbers, IBAN, loan numbers, policy numbers | Fields excluded from the `ai:read` serialisers; pattern scrubber on free text |
| Personal identity | Legal names of household members, usernames, email addresses, phone numbers, date of birth | Fields excluded; member-name and contact scrubber on free text |
| Location of a person | Home or mailing addresses, property addresses of owned real estate | Fields excluded; `manual_asset.description` excluded (column `classification: identity`); street-address scrubber on free text |
| Documents | Original files, page previews, extracted full text, OCR output | No tool exposes them; the MCP process has no document mount |
| Credentials and secrets | Everything in the credentials class of SECURITY.md | Not reachable from the API at all |
| Tax form identity boxes | SSN/EIN fields, employer/payer addresses on W-2/1099 | Tax tools return amounts by box label only |

Merchant locations (the city and state where a card was used) are **financial
facts**, not personal location, and are allowed. A home address is identity
and is not.

## What is exposed

Everything else that describes money, by default at full detail:

- Accounts by **type and user-given nickname** ("Chase Checking", "Roth IRA"),
  institution name, currency, balance, balance history. Not the number.
- Transactions: date, amount, canonical payee, merchant category, merchant
  city/state when the statement carried it, category, tags, notes and memos
  after scrubbing (see below), split details, transfer pairing.
- Budgets, goals and progress, savings rate, recurring spend, cash-flow
  calendar.
- Holdings by ticker, quantity, cost basis, gains, allocation, performance.
- Loans: lender, balance, rate, term, payment split, payoff projections.
- Manual assets: kind, nickname, valuation history. Not the address.
- Net worth history and breakdown.
- Tax year: deductible totals by category and schedule hint, which expected
  forms are received or missing by form type and issuer name, amounts by box
  label.

Document text itself is never exposed; the structured fields parsed from it
are.

## Free-text scrubbing

Memos, notes and payee strings come from statements and can carry stray
identifiers ("ZELLE TO JANE DOE", "CARD 1234", "ACCT ...5678", a phone number
in a utility memo). Every free-text field that leaves through `ai:read` passes
a scrubber that replaces matches with typed placeholders:

- Account, card, routing, IBAN and loan number patterns, including partial
  masks like `...5678`, `XXXX1234`, `*1234` and `x 1234`, numbers named by a
  keyword, a card brand or a network with up to two words in between
  (`AMEX 4455`, `DEBIT CARD PURCHASE 6655`) with every further
  space-separated group (`Account 9876 5432`, `routing 0210 0002 1`), in
  brackets (`Checking (4455)`, `Card [4455]`) or glued to the keyword
  (`visa4455`, `card_4455`, `AMEX·1357`), 12-digit numbers split by `/`, `_`
  or `,`, and the household's own stored last-four digits
  (`account.number_last4`) wherever they stand alone (`Sapphire 6611`) ->
  `[ACCT]`
- SSN/ITIN/EIN patterns (also `123,456,789`) -> `[GOVID]`
- Email (also spaced: `bob . smith @ gmail . com`), phone -> `[EMAIL]`,
  `[PHONE]`
- Street-address patterns -> `[ADDRESS]`
- Household member legal names and configured aliases, and the family-name
  words of the household's name ("The Wolcott Family" -> `Wolcott`) ->
  `[MEMBER]`, also spelled out (`j a n e`, `J.A.N.E`), with Greek or
  Cyrillic look-alike letters or digits for letters (`JΑNE`, `D0E`) and,
  for a word of five letters or more, with a plural or possessive `s` and no
  apostrophe (`WOLCOTTS TRUST`)
- A user-maintained custom denylist (an ex-employer, a child's school) ->
  `[REDACTED]`

Free text means every value a person typed or a statement supplied: payees,
memos, notes, category, tag, budget and goal names, lenders, issuers, account
nicknames, and a security's symbol, name, sector and exchange (a member can
create a security with any name, and OFX `SECNAME` names one on import).

**Filters.** The `payee` and `category` filters of `list_transactions` match a
name as the client sees it, after scrubbing. A filter value the scrubber
would change (a member's name, a denylisted term: "Gifts for Jane") matches
nothing, exactly like a miss, so a filter cannot confirm a guess of what a
placeholder hides; a value with a placeholder ("Gifts for [MEMBER]") matches
the names that scrub to it. With the merchant location hidden, a payee is
compared in the form shown (location cut), so a guessed city and state answer
like a miss too.

**Known limits.** Name parts shorter than three letters ("Li", "Bo") are not
matched on their own (too much ordinary text would go); they are matched next
to a longer part of any member's name, in either order ("Jane Li",
"LI JANE"). The scrubber knows the household's current members only: a former
member's name, or a relative who never had an account, is not identity to it
until the owner adds it as a member alias or a custom denylist term. A
thousands-separated whole number of seven or more digits (`1,234,567`) is
redacted as a possible account number; with cents (`1,234,567.89`) it is an
amount and kept. A date or an amount after an account keyword (`card payment
12.50`, `VISA PURCHASE 03/14`) is kept.

Known limits of the security review P6B-SR-R2 fixes: the household's stored
last-four digits are known to the API stage only (the MCP process holds no
identity), so a bare `Sapphire 6611` is caught once, by the API; any other
stand-alone use of those four digits (a year `2026` when an account ends in
2026) is replaced too, by design. Look-alike letters are folded for a fixed
Greek and Cyrillic set and digits `0 1 3 4 5 7` inside a word that has
letters; other confusables (Armenian, Cherokee, mathematical forms that NFKC
does not fold) are not. The plural/possessive `s` without an apostrophe is
matched for name words of five letters or more only ("Doe" would take
"does"). A spaced e-mail is caught only when a dot has a space next to it
(`bob @ gmail . com`); `bob @ gmail.com` keeps the local part (`Coffee @
Starbucks.com` names a merchant and must survive), unless it is a member's.
A closing bracket after a bracketed number stays (`Checking [ACCT])`).

Text is normalised before matching (NFKC, digits of any script, accents,
zero-width and other format characters removed), so obfuscated identifiers
are caught; the output is the normalised text. Store numbers of up to six
digits after a merchant name (`STARBUCKS #1234`) survive; any run of seven or
more digits is treated as an account number. Implementation and the exact
rules: `backend/app/core/scrubber.py`; corpus:
`backend/tests/security/ai/corpus/`.

The scrubber runs in the API and again in the MCP process. Its test corpus in
CI includes known-bad strings for every class and must reach zero leaks before
a build ships. Users can see the scrubbed value next to the original in the
"AI access" settings page to tune the custom list.

## Modes

Two modes. Both obey the denylist; the difference is detail.

| Mode | Returns | Intended for |
| --- | --- | --- |
| **Detail** (default) | Everything in "What is exposed", transaction-level | Analysing spending, finding savings, planning payoff |
| **Aggregates** | Totals and trends by category, month, account type and merchant category only; no individual transactions, payees or nicknames | Owners who want a conservative first step |

Options per client: round amounts to the nearest 1/10/100, limit the date
range the client can query, hide nicknames (fall back to "Checking A"), hide
merchant location.

The options apply server-side in the API (`app.domain.ai.policy`). In
Aggregates mode labels are always hidden, goals and budgets are named
"Goal A"/"Budget A", tax issuers are omitted and the transaction-level views
answer `403 not-in-aggregates-mode`. Merchant location is the trailing
`CITY ST` of a payee string (the ledger has no separate merchant-location
column); hiding it cuts that tail. The date cap clamps every period to
"today minus N days".

## Architecture

```
  AI client (Claude Desktop / Claude Code / any MCP host; hosted or on-prem model)
        │  MCP over stdio (local) or streamable HTTP (token auth, TLS via proxy)
        ▼
  perfin-mcp  (separate process and container, role "mcp")
        │  - holds ONE scoped read-only API token for ONE household
        │  - applies mode, options and the free-text scrubber on every response
        │  - rate limited; every call audit-logged with the full response
        │  - no write tools exist in the codebase
        ▼
  perfin-api  /ai/v1  (token scope `ai:read`; serialisers built from an
                       explicit field allowlist, scrubber applied, denylist
                       classes have no serialiser at all)
```

- The `ai:read` serialisers are **allowlists**: a new column on a model is
  invisible to AI clients until someone deliberately adds it, and the PR that
  adds it must classify it.
- The MCP process has no database access, no master key and no document
  mount. A bug there cannot reach anything the API would not return to an
  `ai:read` token.
- Stdio transport is the default and keeps the server off the network for
  assistants on the same machine or over SSH. HTTP transport is only served
  through the reverse proxy with TLS and an origin allowlist.

## Tools (first cut)

Read-only. Parameters are validated; free-text parameters are matched against
canonical payee and category names as the client sees them (scrubbed), never
used as raw query patterns.

```
get_overview(period)                       net worth, cash flow, savings rate
get_spending(period, group_by: category|payee|merchant_category|month|account, depth)
get_income(period, group_by)
get_budget_status(period)
get_trends(metric, periods)
get_recurring()                            subscriptions and bills detected
get_savings_goals()
get_net_worth_history(range, granularity)
get_allocation()
list_accounts()                            type, nickname, institution, balance
list_transactions(period, account?, category?, payee?, min_amount?, max_amount?, limit)
find_anomalies(period)                     unusually large or new spend
list_holdings(account?)
get_performance(range)
list_loans()  get_loan_schedule(loan)  simulate_loan(loan, extra_payment)
get_tax_year_summary(year)                 deductible totals, form checklist status
explain_data_policy()                      this client's mode, options, and the denylist
```

Deliberately absent: anything returning documents, document text, review
items, settings, members, sessions, audit logs, or that writes. Suggestions
from an assistant (a new category, a rule) are applied by the user in the
PerFin UI.

## The MCP server (`perfin mcp`, `backend/app/mcp/`)

The process is configured from its own variables only (`app/mcp/config.py`)
and never loads the application settings:

| Variable | Meaning |
|---|---|
| `PERFIN_MCP_ENABLED` | must be `true`, otherwise `perfin mcp` refuses to start |
| `PERFIN_MCP_API_URL` | the API base URL (`http://api:8000` in compose); http(s), no credentials |
| `PERFIN_MCP_TOKEN` / `_FILE` | the AI client's `ai:read` token (shown once at creation) |
| `PERFIN_MCP_TRANSPORT` | `stdio` (default) or `http` |
| `PERFIN_MCP_HTTP_HOST` / `_PORT` | HTTP listener (`127.0.0.1:8001` by default; `0.0.0.0` in its container) |
| `PERFIN_MCP_HTTP_ALLOWED_ORIGINS` | explicit `https` origins (no `*`, `null` or paths); required for `http` |
| `PERFIN_MCP_HTTP_TOKEN` / `_FILE` | bearer secret the assistant host sends on HTTP; 32+ characters, not the API token; required for `http` |
| `PERFIN_MCP_RATE_PER_MINUTE` | tool calls per minute this process accepts (default 30) |

- **M7 at start-up.** `perfin mcp` (and `PERFIN_ROLE=mcp perfin config
  check`) refuses an environment that carries a database URL, the master key,
  the secret key or another service credential (`FORBIDDEN_ENV`, plus any
  `PERFIN_*`/`POSTGRES_*` name ending in `DATABASE_URL`, `MASTER_KEY`,
  `SECRET_KEY`, `PASSWORD` or `_KEY`). No module of `app/mcp` imports a
  database, crypto, settings, auth, domain or API module; a test walks the
  imports and checks `sys.modules` of a started server.
- **Data path.** One `httpx` client, GET only, on `/api/v1/ai/v1/*`, with the
  token; redirects are not followed. Each tool calls exactly one AI view.
- **Arguments** are validated twice (the tool's schema, then the API). An
  argument the tool does not have is refused rather than ignored. `category`
  and `payee` are passed to the API, which matches them against canonical
  names exactly (case-insensitive) after scrubbing (see "Free-text
  scrubbing", Filters); neither side builds a pattern from them.
  `account` and `loan` are ids from `list_accounts` and `list_loans`.
- **Second scrub (M2).** Every string of every answer, keys included, and every
  error message passes through the fixed patterns of `app/core/scrubber.py`
  again (the MCP process does not know member names or the denylist; the API
  stage applies those). Left as they are: canonical UUIDs, a money object's
  decimal `amount`, the opaque `next_cursor`, and a plain decimal number
  (at most 15 integer digits) in one of the numeric fields the serialisers
  send as strings (`weight`, `rate`, `price`, `quantity`, `percent`,
  `savings_rate`, `twr`/`mwr` and their annualised forms, `mom_percent`,
  `yoy_percent`; `app.mcp.scrub.NUMERIC_FIELDS`). As built in v0.7 the
  second scrub first replaced `"weight": "1.0000000000"` with `[ACCT]` (ten
  digits read as an account number); the live run of every tool found it
  (`tests/security/mcp/test_mcp_live.py`, `tests/unit/test_mcp_scrub.py`).
- **Documents.** No AI view reads a document, a preview, extracted text, a
  review item or the search index, and every document column and every
  reference to a document is classified `denied`. This holds even for the
  page text PerFin keeps for statements and payslips (the limit of security
  review P56-SR-R1-02): `tests/security/ai/test_ai_documents.py` plants an
  unredacted Form 1040, a payslip and a statement carrying an SSN, a name,
  an address and an employee id and reads every AI view; nothing of them
  leaves, and the `ai:read` token is refused on every document, review and
  import route.
- **Refusals are policy.** A `403` from the API (kill switch or household flag
  off: `ai-access-disabled`; client revoked or expired; `not-in-aggregates-mode`)
  and `422 outside-date-range` are reported to the assistant as "refused by
  policy" with the problem type; `429` as the API's rate limit.
- **Rate limit.** Per process, a sliding minute of
  `PERFIN_MCP_RATE_PER_MINUTE` calls, before the API's per-client limits.
- **HTTP transport** (stateless streamable HTTP at `/mcp`): every request
  needs `Authorization: Bearer <PERFIN_MCP_HTTP_TOKEN>` (constant-time
  comparison, `401` otherwise); a request carrying an `Origin` outside the
  allowlist is `403` (non-browser hosts send none). TLS ends at the reverse
  proxy (`deploy/Caddyfile.example`). In compose the `mcp` service sits on
  the internal `ai` network (api and mcp only) and `mcp_edge` (proxy and mcp
  only), mounts nothing, runs read-only with every capability dropped, and
  gets no `env_file`.

Tool arguments as implemented (the first-cut list above, made concrete):
`get_budget_status(on?)` reports on a day; `get_trends(metric, periods,
month?)` takes `periods` in months (1-60); the period tools take `period` or
`date_from`/`date_to`; `list_transactions` also takes `limit` (at most 200)
and `cursor`; `simulate_loan(loan, extra_payment?, extra_once?,
extra_once_on?)`; `get_spending`/`get_income` also accept `account_type`.
`explain_data_policy` returns the API's policy (mode, options, the classes
never exposed, placeholders, rate limits) plus an `mcp_server` block (tools,
`write_tools: []`, the second-stage placeholders, the process rate limit).

## Client management

- Created in Settings by a household owner with step-up MFA: name, mode,
  options, transport, expiry.
- The **exposure preview** renders a real sample from every tool with the
  chosen settings before the client is saved, with scrubbed placeholders
  highlighted.
- Every call is written to `ai_call` with tool, parameters, byte count and the
  full response as sent. The "AI access" page lists calls per client and lets
  the owner search responses for a string to confirm something never left.
- One-click revoke. Tokens hashed at rest, scoped to `ai:read`, expiring.
- A kill switch in system settings disables the MCP role entirely.

As built (Phase 6b, API.md "AI settings notes"): the exposure preview runs
every tool with its default parameters on the household's real data through
the same views, serialisers and scrubber with the chosen options and stores
nothing; each string holding a placeholder is marked by its JSON path. The
expiry (`expires_in_days`) is mandatory on create. The kill switch is the
system setting `ai.enabled` (`/settings/system/ai`, instance admin,
step-up); a household setting with the same key (`/settings/ai`, owner,
step-up) does the same for one household. Either one off makes every AI
view answer `403 ai-access-disabled` and refuses new clients; clients are
kept, not revoked. Since the MCP process reaches data only through the AI
views, this disables it too. `GET /auth/session` carries
`ai_clients_active` for the persistent banner (M5). The scrubbed-vs-original
view is `/settings/ai/scrubber-preview` (owner only), which also takes
denylist terms and aliases to try before saving them.

## Hosted versus on-prem models

Because the denylist applies to every client, the same MCP server can serve a
hosted assistant or an on-prem one (Ollama, llama.cpp, vLLM behind an MCP
host). PerFin does not talk to the model and does not need to know which it
is. The guidance in the docs will be:

- Hosted assistant: fine for Detail mode; consider rounding and hiding
  nicknames if you prefer less fidelity off-site. Remember that merchant names
  and amounts are themselves revealing even without identity.
- On-prem model: no additional restrictions needed; this is the private
  option for people who do not want merchant-level detail leaving at all.

A future optional **in-app assistant** backed only by an on-prem endpoint is
parked in the roadmap. It would reuse the same `ai:read` serialisers and would
never support hosted providers.

## Ingestion without a model

Classification and parsing rely on deterministic fingerprints, institution
parsers, a small local heuristic classifier and the review queue with a
column-mapping editor. A never-seen layout needs a person once; the saved
mapping becomes a fingerprint.

## Threats specific to this boundary

| # | Vulnerability | Control |
| --- | --- | --- |
| M1 | Identity field leaks through a serialiser | Allowlist serialisers; denylist classes have no `ai:read` serialiser; PR template requires classifying new fields; CI test enumerates every model column and fails on unclassified ones |
| M2 | Identifier hiding in free text (memo, note, payee, asset description) | Two-stage scrubber with a CI corpus that must show zero leaks; user-visible scrubbed-vs-original view; custom denylist |
| M3 | Prompt injection via scrubbed free text reaching the assistant | MCP has no write tools, so an injected instruction has nothing to act on inside PerFin; the host application's own protections cover the rest |
| M4 | Stolen MCP token | Read-only `ai:read` scope, hashed, expiring, rate limited, revocable, origin-checked on HTTP transport, full audit trail |
| M5 | Over-exposure by misconfiguration | Exposure preview before save, step-up MFA, Aggregates mode available, persistent banner while a client exists |
| M6 | Aggregation queries as denial of service | Per-client rate limits, bounded periods, cached aggregates |
| M7 | MCP process compromise | Own container, only an API token; no DB credentials, master key or document store mount |
| M8 | Re-identification from rich financial detail (merchants, amounts, locations) | Accepted by design per owner's policy; mitigations offered as options: rounding, hide nicknames, hide merchant location, Aggregates mode, on-prem model |

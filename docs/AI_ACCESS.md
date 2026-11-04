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
  masks like `...5678` and `XXXX1234` -> `[ACCT]`
- SSN/ITIN/EIN patterns -> `[GOVID]`
- Email, phone -> `[EMAIL]`, `[PHONE]`
- Street-address patterns -> `[ADDRESS]`
- Household member legal names and configured aliases -> `[MEMBER]`
- A user-maintained custom denylist (an ex-employer, a child's school) ->
  `[REDACTED]`

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
canonical payee and category names, never used as raw query patterns.

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

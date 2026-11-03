# AI access boundary (MCP server and on-prem models)

## Principle

PerFin itself never calls a language model, hosted or local, as part of
ingestion or any other core function. Files, extracted text, account numbers
and personal details never leave the premises because no code path sends them
anywhere.

AI assistance is instead provided *to* an assistant, not *by* one, through a
read-only **MCP server** that exposes de-identified, aggregated financial data.
The MCP server is the privacy boundary: whatever it returns is, by design, what
an AI client is allowed to see. An operator who runs an on-prem model can point
the same MCP server at it and widen the boundary for that client only.

## What an assistant can and cannot see

Data is classified into three tiers. Each MCP client (a named connection with
its own token) is assigned a tier, and each tool declares the tier it needs.

| Tier | Name | Included | Never included |
| --- | --- | --- | --- |
| 0 | **Aggregates** | Totals and trends by category and month, budget vs actual, savings rate, net worth movement, allocation percentages, loan payoff projections, goal progress. Amounts optionally rounded or bucketed. Time granularity no finer than a day. | Account names, institutions, payees, memos, document text, individual transactions, any identifier |
| 1 | **Pseudonymised detail** | Everything in Tier 0 plus individual transactions and holdings with: pseudonymous account labels ("Checking A"), category, canonical payee name (from the `payee` table, not raw statement text), amount, date, tags. Securities by ticker. | Raw payee strings, memos, account numbers including last 4, institution names, document text, notes, attachments, names of household members, addresses, emails |
| 2 | **On-prem only** | Everything in Tier 1 plus raw memo text and institution names. | Account numbers, document originals, extracted document text, tax form box values that carry identifiers (SSN, EIN), member names, contact details |

Tier 2 can only be granted to a client whose model endpoint PerFin has verified
is on a private network (see "On-prem models"). Tiers 0 and 1 may be granted to
any client, including a hosted assistant, because they contain no identifiers.
There is no tier that returns account numbers, documents, extracted text or
member identities to any model, anywhere.

Pseudonyms are stable per client (derived from account id + per-client salt) so
an assistant can refer to "Checking A" consistently, and are reset when the
client's token is rotated.

## Architecture

```
  AI client (Claude Desktop / Claude Code / any MCP host)
        │  MCP over stdio (local) or streamable HTTP (token auth)
        ▼
  perfin-mcp  (separate process and container, role "mcp")
        │  - holds ONE scoped read-only API token for ONE household
        │  - applies tier policy + redaction + rounding on every response
        │  - rate limited, every call audit-logged with the full response
        │  - no write tools exist in the codebase
        ▼
  perfin-api  /api/v1  (same authz as any user; token scope `ai:read:tier{N}`)
```

- The MCP server is a thin client of the public API. It has no database
  access, so a bug in it cannot reach anything the API would not return to a
  token with that scope.
- The API enforces the tier server-side: a `tier0` token calling a transaction
  endpoint gets `403`, regardless of what the MCP process asks for. Redaction
  is applied in the API's response layer for `ai:*` scopes, and then again in
  the MCP server (defence in depth).
- Transport defaults to **stdio**, which keeps the server off the network
  entirely for desktop assistants on the same machine or over SSH. Streamable
  HTTP is available for remote clients behind the reverse proxy with the
  client token plus origin allowlisting; it is never exposed without TLS.

## Tools (first cut)

Read-only, each tagged with its minimum tier. Parameters are validated;
free-text parameters are not passed through to any query as patterns.

```
Tier 0
  get_overview(period)                      net worth, cash flow, savings rate
  get_spending_by_category(period, group_by: month|quarter|year, depth)
  get_income_summary(period)
  get_budget_status(period)
  get_trends(metric, periods)               category or total over time
  get_savings_goals()                       progress and projected completion
  get_net_worth_history(range, granularity)
  get_allocation()                          asset class percentages
  get_loan_projections(extra_payment?)      payoff dates, interest saved
  get_recurring_summary()                   subscription-like spend by category
  get_tax_year_summary(year)                deductible totals by hint, no form values
  explain_data_policy()                     returns this client's tier and what is excluded

Tier 1 (adds)
  list_transactions(period, category?, min_amount?, max_amount?, limit)
  list_holdings()
  list_accounts()                           pseudonymous labels and types only
  find_anomalies(period)                    unusually large or new-category spend

Tier 2 (adds, on-prem only)
  list_transactions(..., include_memo=true)
  list_accounts(include_institution=true)
```

Deliberately absent: anything that returns documents, extracted text, review
items, settings, members, audit logs, or that writes. Categorisation help from
an assistant flows back through the human: the user reads the suggestion and
applies it in the PerFin UI.

## Client management

- Created in Settings by a household owner with step-up MFA: name, tier,
  transport, optional expiry, optional amount rounding (nearest 1, 10, 100),
  optional date coarsening (month only).
- The **exposure preview** shows, before saving, a sample response from every
  tool at that tier using the household's real data, so the user sees exactly
  what the assistant will see.
- Every call is written to the audit log with tool, parameters, byte count and
  the full redacted response. An "AI access" page lists calls per client.
- One-click revoke. Tokens are hashed at rest, scoped, and cannot be upgraded
  to a higher tier without re-creation.
- A kill switch in system settings disables the MCP role entirely.

## On-prem models

An operator may run a model on their own hardware (Ollama, llama.cpp,
vLLM) and connect an MCP-capable client to it. PerFin does not talk to the
model; the client does. PerFin's only involvement is deciding whether a client
may be granted Tier 2:

- The client registration includes the model endpoint URL the assistant will
  use. PerFin resolves it and requires an RFC 1918, link-local, loopback or
  unique-local address, or a hostname on an admin-configured allowlist.
- This is a policy check, not an enforcement: PerFin cannot prevent a client
  from forwarding data elsewhere. The settings page says so plainly. Tier 2 is
  for operators who control both ends.

A future phase may add an optional **in-app assistant** backed only by an
on-prem endpoint, using the same tier policy internally. It is explicitly out of
scope until the MCP route has proven the policy model, and it would never
support hosted providers.

## Ingestion without a model

The classification and parsing pipeline relies on deterministic fingerprints,
institution parsers, a small local heuristic classifier and the review queue.
Where a model would have helped (a never-seen layout), the user instead gets a
review item with the extracted tables and a "map these columns" editor that
produces a reusable fingerprint. Coverage grows through contributed parsers
with synthetic fixtures, not through inference.

## Threats specific to this boundary

| # | Vulnerability | Control |
| --- | --- | --- |
| M1 | Identifier leakage through a tool response | Tier policy enforced in the API, redaction layer tested with a denylist corpus (account number formats, emails, member names, raw payee strings) in CI; responses that trip the detector are blocked and alerted |
| M2 | Re-identification from detailed data | Pseudonyms per client, optional rounding and date coarsening, canonical payee names only below Tier 2 |
| M3 | Prompt injection via data the assistant reads (a crafted payee or memo) | Tiers 0 and 1 never return raw statement text; canonical payee names are user-curated; the MCP server has no write tools so injected instructions have nothing to act on inside PerFin |
| M4 | Stolen MCP token | Scoped read-only, hashed at rest, expiring, rate limited, revocable, origin-checked on HTTP transport, audit trail of every call |
| M5 | Over-broad tier granted by mistake | Exposure preview before save, step-up MFA, Tier 2 gated on private endpoint check, persistent banner on the client while it exists |
| M6 | Aggregation queries used as a denial of service | Per-client rate limits, bounded periods, cached aggregates |
| M7 | MCP process compromise | Runs as its own container with only an API token; no DB credentials, no master key, no document store mount |

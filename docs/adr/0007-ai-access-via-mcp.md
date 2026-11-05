# ADR-0007: No model calls inside PerFin; AI access only through a read-only MCP server that exposes finance, never identity

Status: Accepted. Date: 2026-11-03. Supersedes ADR-0004. Its first decision
("no LLM client") is partly superseded by ADR-0010: an optional, local-only
model container may suggest what a document is.

## Context
The original design allowed an optional LLM fallback inside ingestion, with
redaction and local-provider preference. On reflection the owner's requirement
is stricter: files and personal details must never leave the premises, full
stop, and the application should not contain a code path that could send them
anywhere. At the same time, AI help analysing spending and savings is wanted.

## Decision
- PerFin contains **no LLM client**. Ingestion is deterministic parsers plus a
  local heuristic classifier plus the review queue.
- AI assistance is provided through a separate, read-only **MCP server** role
  governed by one rule: **financial facts are exposed, identity is not.**
  Spending, merchants and their locations, balances, savings, holdings and
  loans are available at full detail. Addresses, dates of birth, government
  identifiers, account and card numbers (including last 4), member names and
  contact details, and all document originals and extracted text are on a
  denylist that applies to every client in every mode.
- The denylist is enforced in the API by allowlist serialisers for the
  `ai:read` scope plus a free-text scrubber, and again in the MCP process.
  The MCP process has no database or document access.
- Two modes, Detail (default) and Aggregates, plus per-client options
  (rounding, hide nicknames, hide merchant location, date range cap).
- Hosted and on-prem models are treated the same because the denylist already
  holds for both; on-prem is documented as the choice for people who do not
  want merchant-level detail off-site.
- Every MCP call is audit-logged with its full response, and an exposure
  preview shows real sample output before a client is created.

## Alternatives considered
- **Keep the in-app local-only LLM fallback.** Simpler ingestion coverage, but
  it keeps a model client in the codebase, and "local only" is one config
  change away from "remote". The owner prefers the code path not exist.
- **Expose the full API to the assistant with a normal token.** Would leak
  identifiers and document text; the whole point is a narrower surface.
- **Export CSVs for the user to paste into a chat.** Works today, but leaks
  whatever is in the CSV and gives no audit trail or policy.

## Consequences
- Long-tail statement layouts need a human once, via the column-mapping
  editor, instead of a model guess. Parser contributions matter more.
- A new container role (`mcp`) and a new token scope (`ai:read`).
- A scrubber test corpus and a column-classification test become required CI
  gates; every new model column must be classified before it can ship.
- See docs/AI_ACCESS.md for the full design.

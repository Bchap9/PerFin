# ADR-0007: No model calls inside PerFin; AI access only through a tiered, read-only MCP server

Status: Accepted. Date: 2026-11-03. Supersedes ADR-0004.

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
  that exposes de-identified, aggregated data under a three-tier policy
  (aggregates; pseudonymised detail; on-prem-only detail). No tier ever
  returns account numbers, documents, extracted text or member identities.
- Tiers are enforced in the API by token scope, with redaction applied again in
  the MCP process. The MCP process has no database or document access.
- Tier 2 (raw memos, institution names) can only be granted when the client's
  model endpoint resolves to a private address, and the UI states that this is
  a policy check the operator is responsible for honouring.
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
- A new container role (`mcp`) and a new token scope family (`ai:read:tierN`).
- A redaction test corpus becomes a required CI gate.
- See docs/AI_ACCESS.md for the full design.

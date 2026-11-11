# AI access: what leaves the host

Scope: the AI boundary of Phase 6b (`perfin mcp`, the `ai:read` views). Control
ids refer to `docs/AI_ACCESS.md` (M1-M8) and `docs/SECURITY.md`; status is in
[`CONTROLS.md`](CONTROLS.md). Setup: [`../MCP_CLIENTS.md`](../MCP_CLIENTS.md).

## Short answer

**PerFin sends nothing to any hosted model or AI service.** There is no
outbound call to an AI provider and no setting for one. The optional local
model container (ADR-0010, [`../ML.md`](../ML.md)) is refused unless it runs on
your own network, and it only suggests (what a document is, which account
it is about, what kind of activity an unfamiliar wording on a brokerage
statement is, what an unfamiliar column heading of one of its tables
names; a person confirms each). A connected assistant host pulls data from PerFin; PerFin
never pushes.

## When an assistant is connected

Data crosses a boundary only on a tool call, over the transport you chose:

| Path | What crosses | Protection |
|---|---|---|
| API -> `perfin mcp` | The `ai:read` view answer (financial facts after the client's mode, rounding, date cap and the API scrub) | Internal `ai` Docker network; token `ai:read`, hashed, expiring, revocable; per-client rate limits |
| `perfin mcp` -> assistant host | The same, after the second scrub | stdio (no network), or HTTPS through the proxy with the bearer secret and origin allowlist |
| Host -> model | Whatever the host puts in the prompt, including tool results | Outside PerFin. Hosted: the provider receives it. On-prem: stays on your network |

## What is exposed

Financial facts: accounts by type, nickname and institution, balances,
transactions (date, amount, payee, category, tags, scrubbed notes), budgets,
goals, holdings, loans, manual-asset nicknames and values, net worth, tax
totals by category and box label. Amounts may be rounded; nicknames and
merchant location may be hidden; Aggregates mode removes transactions.

## What is never exposed

Government identifiers; account, card, routing, IBAN and loan numbers
including last 4 and partial masks; household member names, usernames, emails,
phones, date of birth; home and property addresses; documents, previews,
extracted text and OCR; credentials and keys; tax-form identity boxes. Free
text is scrubbed to typed placeholders in the API and again in `perfin mcp`.
Corpus tests must show zero leaks.
PerFin keeps the page text of statements and payslips for its review tools
(tax documents and anything carrying tax identity are redacted first); no
`ai:read` view reads any document data, and
`backend/tests/security/ai/test_ai_documents.py` proves that planted document
text never reaches an AI answer or audit row.

## What PerFin cannot control

- What the assistant host and model do with the answers once received
  (retention, training, logs). Use Aggregates mode, rounding or an on-prem
  model if that matters (M8).
- Re-identification from rich financial detail (merchants, amounts): accepted
  by design, mitigated by options (M8).
- Prompt injection in scrubbed free text: `perfin mcp` has no write tools, so
  an injected instruction has nothing to act on inside PerFin (M3). The host's
  own protections apply to anything else it can do.

## Your controls

Exposure preview before saving (M5); audit of every call with the response as
sent, searchable (M4); one-click revoke; household and instance kill switches;
persistent banner while a client exists. Operators can stop the `mcp`
container or leave `PERFIN_MCP_ENABLED=false`; the process refuses to start
holding a database URL, key or other credential (M7).

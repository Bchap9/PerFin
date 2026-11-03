# ADR-0004: The LLM is an optional, untrusted, constrained fallback

Status: **Superseded by ADR-0007** (2026-11-03). Kept for the record; the
in-app LLM fallback described here will not be built.

Original status: Accepted. Date: 2026-11-03.

## Context
Large language models are very good at reading messy statements and receipts,
and very bad at being trusted: they hallucinate, follow instructions embedded
in their input, and (for hosted providers) require sending data off-host.

## Decision
- LLM use is **off by default** and local providers (Ollama, llama.cpp
  server) are documented first.
- It runs only after deterministic fingerprints/parsers and the local
  heuristic classifier fail to reach the confidence threshold.
- Input is redacted (account numbers to last 4, optional name
  pseudonymisation) and truncated.
- Output must validate against a JSON Schema; the values are suggestions
  whose confidence is capped **below** the auto-commit threshold, so they
  always pass through the review queue the first time they are seen.
- The model has no tools, no network through PerFin, and no action surface.
- Every call is audit-logged with provider, model, bytes sent and document id.

## Alternatives considered
- **LLM-first extraction.** Simpler to build, better coverage on day one, but
  makes correctness depend on a non-deterministic component and prompt
  injection a data-integrity risk.
- **No LLM at all.** Safer but leaves long-tail documents unparsed; the review
  queue would fill with "unparsed" items. Opt-in strikes the balance.

## Consequences
- A crafted statement can at worst produce a wrong suggestion in review.
- Parser coverage, not the LLM, is the quality roadmap; LLM output in review
  can help a user build a fingerprint faster.

# Architecture decision records

Short records of decisions that are expensive to reverse. Format: context,
decision, alternatives considered, consequences. New ADRs get the next number;
superseded ADRs are kept and marked.

| # | Title | Status |
| --- | --- | --- |
| [0001](0001-language-and-framework.md) | Python/FastAPI backend, React/TS frontend, modular monolith | Accepted |
| [0002](0002-postgres-as-queue.md) | PostgreSQL as the job queue (no Redis) | Accepted |
| [0003](0003-document-encryption.md) | Envelope encryption for documents and sensitive columns | Accepted |
| [0004](0004-llm-as-untrusted-oracle.md) | LLM is an optional, untrusted, constrained fallback | Superseded by 0007 |
| [0005](0005-server-side-sessions.md) | Server-side sessions, not JWTs, for browser auth | Accepted |
| [0006](0006-sandboxed-extraction.md) | File content is only ever parsed in a sandboxed worker subprocess | Accepted |
| [0007](0007-ai-access-via-mcp.md) | No model calls inside PerFin; AI access only through a read-only MCP server that exposes finance, never identity | Accepted |
| [0008](0008-household-consistent-foreign-keys.md) | Household-consistent foreign keys between household-scoped tables | Accepted |
| [0009](0009-extraction-isolation-under-a-shared-uid.md) | Isolating the extraction child from a worker that shares its UID (non-dumpable worker, Landlock, seccomp filesystem rules) | Accepted |

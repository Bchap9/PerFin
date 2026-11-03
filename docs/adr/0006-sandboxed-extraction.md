# ADR-0006: File content is only ever parsed in a sandboxed worker subprocess

Status: Accepted. Date: 2026-11-03.

## Context
PDF renderers, image decoders and OCR engines have long CVE histories. PerFin
feeds them files from email and a drop folder, both of which an attacker can
reach. The API process holds session data and database credentials.

## Decision
- The API process never opens file content beyond magic-byte sniffing and
  hashing on a stream.
- All extraction runs in the **worker** role, which has no published ports and
  default-deny egress.
- Within the worker, each extraction runs in a **subprocess** with
  `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_NOFILE`, a wall-clock timeout, a seccomp
  profile, a read-only filesystem plus tmpfs scratch, and a dedicated low
  privilege UID. Output is passed back as JSON over a pipe and validated.
- The worker container runs with `read_only`, `cap_drop: ALL`,
  `no-new-privileges`, and a non-root user.
- Later hardening options: run extraction in gVisor or a Firecracker microVM;
  replace the Python extractor with a memory-safe Rust binary.

## Alternatives considered
- **Parse in-process in the worker.** Simpler, but a parser exploit then holds
  database credentials and the master key.
- **Separate extraction microservice.** More isolation, more moving parts.
  The subprocess approach gets most of the benefit; the interface is designed
  so it can become a separate container without changing callers.

## Consequences
- Slight per-document overhead (process spawn), irrelevant at PerFin's volume.
- Parsers must be written to a strict JSON output contract, which also makes
  them testable and re-runnable.

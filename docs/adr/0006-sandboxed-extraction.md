# ADR-0006: File content is only ever parsed in a sandboxed worker subprocess

Status: Accepted. Date: 2026-11-03. Amended by ADR-0009 (the child may share
the worker's UID; a non-dumpable worker, Landlock and seccomp filesystem
rules isolate it instead). Addendum 2026-11-04: the seccomp profile is a
default-kill allowlist.

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
  privilege UID (see ADR-0009 for the shipped same-UID deployment).
  Output is passed back as JSON over a pipe and validated.
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

## Addendum (2026-11-04, Phases 3-4): the seccomp profile is a default-kill allowlist

**Context.** Until Phase 3 the child's seccomp filter was default-allow with
a deny list (network, exec, new processes, signals to others, ptrace,
namespaces, BPF, `io_uring`, identity changes, filesystem changes). That was
chosen while the PDF, image and OCR libraries were not yet in the child,
because an incomplete allowlist fails in production in ways tests do not
see. With every Phase 3-4 extractor running in the child, the syscall set is
known: a deny list leaves the whole remaining kernel surface (any syscall
not thought of) reachable from a parser exploit.

**Decision.** `install_seccomp` (`app/ingestion/sandbox/child.py`) loads a
filter whose default action is `KILL_PROCESS`. `ALLOWED_SYSCALLS` lists
what CPython, PyMuPDF, Pillow, pillow-heif and libtesseract need: memory
management, reading and `stat`-ing descriptors and paths (where Landlock
allows), time, futexes and thread bookkeeping, its own process facts,
signal handling and `getrandom`. Calls with dangerous argument forms are
allowed only in their safe form: `clone` with `CLONE_THREAD`, `kill`/
`tgkill`/`rt_sigqueueinfo`/`rt_tgsigqueueinfo` and `prlimit64` on the
child itself, `fcntl` and `ioctl` with listed commands, `open`/`openat`
without a write flag. The previous deny rules stay as `EPERM` rules on top
(disjoint from the allow rules, which libseccomp requires), so a probe for
sockets, exec, fork, signals or file changes gets a clean error and the
hostile-input tests can tell which layer stopped it. A child killed by the
filter (`SIGSYS`) is reported by the runner as the sandbox code
`syscall_denied`, so an allowlist gap after a library update is a visible,
per-document failure, never a silent bypass.

The list was derived from `strace -f` of the child across every extractor,
the upload fuzz corpus and the mutation fuzzer, and checked with the
sandbox and fuzz test suites on the host (Ubuntu glibc 2.39) and in the
`python:3.12-slim` base image (Debian glibc 2.41).
`tests/security/test_sandbox.py::test_syscall_outside_the_allowlist_kills_the_child`
fails if the filter is default-allow again.

**Alternatives considered.**
- *Keep the deny list*: rejected; every unlisted syscall (and any added by
  a future kernel) stays reachable.
- *Default `ERRNO` instead of kill*: a library that hits an unexpected
  `ENOSYS`/`EPERM` may carry on with a wrong result; a kill fails the
  document visibly and is the conventional default-deny action.

**Consequences.** A library or C-library update that starts using a new
syscall makes extraction fail with `syscall_denied` until the allowlist is
extended; the dependency review for PyMuPDF, Pillow, pillow-heif, tesserocr
and the base image includes running `tests/security/test_sandbox*.py` and
the fuzz corpus in the built image.

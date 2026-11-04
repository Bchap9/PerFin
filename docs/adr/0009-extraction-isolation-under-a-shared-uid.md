# ADR-0009: Isolating the extraction child from a worker that shares its UID

Status: Accepted. Date: 2026-11-04. Amends ADR-0006 ("a dedicated low
privilege UID").

## Context
ADR-0006 runs every extraction in a subprocess under "a dedicated low
privilege UID". The shipped compose file runs the worker as UID 10001 with
`cap_drop: ALL` and `no-new-privileges`, so the worker cannot switch the
child to another UID: the child ran as 10001 too. Security review round 1
of Phase 2 (finding P2-SR-01) showed what that meant with a hostile
extractor: the child could read `/proc/<worker>/environ` (the master key,
the secret key and the database URL), open `/proc/<worker>/mem`, and
unlink, truncate, chmod and create files the UID owns, including every
encrypted blob in the documents volume. `RLIMIT_FSIZE` 0 only stopped
`write()`.

## Decision
The child stays under the worker's UID in the default deployment, and three
independent layers take the place of the UID boundary:

1. **The worker is non-dumpable.** `perfin worker` calls
   `prctl(PR_SET_DUMPABLE, 0)` at start-up and the runner calls it again
   before every spawn (`app.ingestion.sandbox.runner.protect_parent`). The
   worker's `/proc/<pid>` entries then belong to root and need
   `CAP_SYS_PTRACE`, which nothing in the container has.
2. **Landlock in the child.** Every filesystem right of the kernel's
   Landlock ABI is handled and only reading is granted, beneath the
   interpreter's own paths (`sys.path`, the prefixes, the system library
   directories) and the child's own `/proc/<pid>`. The documents and
   quarantine volumes, key files and other processes are unreachable;
   Landlock also refuses ptrace-mode access to processes outside the
   child's domain. Without Landlock the child refuses to run unless the
   operator sets `PERFIN_EXTRACT_REQUIRE_LANDLOCK=false`.
3. **Seccomp filesystem rules in the child.** Every call that changes the
   filesystem (`unlink*`, `rename*`, `truncate`, `ftruncate`, `fallocate`,
   `chmod*`, `chown*`, `mkdir*`, `rmdir`, `link*`, `symlink*`, time and
   xattr changes, `creat`) is `EPERM`, as is `open`/`openat` with
   `O_WRONLY`, `O_RDWR`, `O_CREAT` or `O_TRUNC`; `openat2` is `ENOSYS`.
   This also covers what Landlock does not (file mode, owner and times).
   The same filter keeps signals inside the child: `kill`, `tgkill`,
   `rt_sigqueueinfo` and `rt_tgsigqueueinfo` only with the child's own pid,
   `tkill` and `pidfd_*` denied, and `fcntl` `F_SETOWN`/`F_SETOWN_EX`/
   `F_SETSIG` denied (SIGIO to the worker through the child's pipes).
   Landlock scopes signals only from ABI 6 (Linux 6.12), so on common host
   kernels this is the only layer that stops a same-UID child from stopping
   or killing the worker and sibling children (review P2-SR-R2-01).

`PERFIN_EXTRACT_UID` remains for deployments that start the worker as root;
there the child also switches UID.

## Alternatives considered
- **Start the worker as root and drop to a dedicated UID per child.** A real
  UID boundary, but the long-running worker then holds root (or `SETUID`
  and `SETGID`) for its whole life, which is the larger risk.
- **A setuid helper.** Contradicts `no-new-privileges` and adds a privileged
  binary to the image.
- **Remove the secrets from `os.environ` after the settings load.** Does not
  help: `/proc/<pid>/environ` shows the original environment block, not the
  current `os.environ`.

## Consequences
- The host kernel needs Landlock (Linux 5.13+, enabled in the LSM list) and
  the container runtime must allow the three Landlock syscalls (Docker's
  default seccomp profile does). Hosts without it get a clear
  `harden_failed` until the operator either fixes the host or opts out.
- Each layer is tested on its own (`backend/tests/security/test_sandbox.py`
  with fixture variants that remove one layer), so removing any of them
  makes a test fail.
- **Without Landlock** (`PERFIN_EXTRACT_REQUIRE_LANDLOCK=false`, or a kernel
  without it) the child can read every file its UID can read, and the
  `/proc/<pid>` entries of every *dumpable* process of that UID in the
  worker container: a sibling child during its start-up, before it hardens
  itself, and any other process of UID 10001 there. The worker's own
  healthcheck runs under `env -i` so it carries no secrets (review
  P2-SR-R2-02), but a `docker compose exec worker ...` admin command inherits
  the container environment from `env_file` (`PERFIN_MASTER_KEY`,
  `PERFIN_SECRET_KEY`, `PERFIN_DATABASE_URL`) and is readable while it runs.
  Operators who opt out should run admin commands in the `api` container or
  a separate one-shot container, or give the child a real UID boundary
  (a root-started worker with `PERFIN_EXTRACT_UID`).
- A worker that is non-dumpable has unreadable `/proc/self/{environ,fd,...}`
  for its own non-root UID; nothing in PerFin reads them.

## Addendum (Phases 3-4): OCR inside the same sandbox

Status: Accepted. Date: 2026-11-04.

The PDF, image, EML and ZIP extractors run in the same hardened child as the
structured ones. OCR was the one library that did not fit: `pytesseract`
starts the `tesseract` program, and the child's seccomp filter denies
`execve`, `fork` and non-thread `clone`. (An earlier `RLIMIT_NPROC` 0 was
dropped on 2026-11-04: it counts every thread of the UID and binds only
non-root processes, so it broke PyMuPDF's and Tesseract's threads in an
unprivileged child; seccomp alone stops new processes.)

**Decision.** Tesseract runs **in process** through `tesserocr` (the C++ API
of libtesseract, linked into the child; its manylinux wheel bundles
libtesseract and leptonica). `pytesseract` was removed from the dependencies.
The child's guarantees are unchanged: no `execve`, no new processes, no
sockets, no file writes; Landlock gains one more read-only root, the system
tessdata directories (`TESSDATA_READ_ROOTS` in
`app/ingestion/sandbox/child.py`: `/usr/share/tesseract-ocr`,
`/usr/share/tessdata`, ...; the image keeps `tesseract-ocr-eng` for the
language data). OCR time is bounded per page and per document by
Tesseract's own deadline (`Recognize(timeout=...)`,
`ExtractOptions.ocr_page_seconds`/`ocr_total_seconds`), with `RLIMIT_CPU`,
`RLIMIT_AS` and the runner's wall clock as the outer bounds.
`tests/security/test_sandbox_documents.py::test_ocr_runs_inside_the_hardened_child`
OCRs a scanned PDF through the real runner.

**Alternatives considered.**
- *A second sandboxed invocation that execs `tesseract`*: a launcher child
  that hardens itself but keeps `execve` allowed (Landlock `EXECUTE` limited
  to the binary), fed preprocessed page images by the worker, with its TSV
  output parsed by a third child. It keeps the parsing child exec-free, but
  needs a filter variant that allows `execve`, three process round trips per
  OCR'd document and page images passing through the worker; rejected as
  more moving parts for the same isolation.
- *Allowing `execve` in the parsing child*: rejected; it is the most useful
  primitive for an exploit of PyMuPDF, Pillow or libheif.

**Consequences.** libtesseract's parsers run with the same rlimits, Landlock
and seccomp as PyMuPDF; a tesserocr wheel update is a native-library update
and goes through the same dependency review (F-controls).

## Addendum (v1.0.0 CI): the user database, libcap and a named SIGSYS

Status: Accepted. Date: 2026-11-04.

**Context.** On the GitHub `ubuntu-24.04` runner (non-root, kernel 7.0
azure) every text-layer PDF with ruled tables died with `SIGSYS`
(`syscall_denied`, CI run 41915782513); it passed on the development host.
The kernel's audit record (`type=1326 ... syscall=157`) and an `strace` of
the child showed the chain: `page.find_tables()` makes PyMuPDF load its
layout model, which imports onnxruntime; onnxruntime calls `getpwuid_r` at
import; the child's Landlock domain makes `/etc/passwd` unreadable, so
glibc falls through to the next NSS module of the host's `nsswitch.conf`,
on Ubuntu `passwd: files systemd`; `libnss_systemd.so.2` links libcap, and
libcap's constructor probes the capability bounding set with
`prctl(PR_CAPBSET_READ, n)`. `prctl` was not on the allowlist. The
development host's `nsswitch.conf` lists only `files`, so no module was
loaded there; the result depends on the host, not on the file.

**Decision.**
- `prctl` is allowed with one option only, `PR_CAPBSET_READ`
  (`PRCTL_ALLOWED_OPTIONS`): it reads the child's own capability bounding
  set, which the child can already read in its own `/proc/<pid>/status`
  (`CapBnd`). It changes nothing and reaches no other process. Every other
  `prctl` option (`PR_SET_DUMPABLE`, `PR_SET_NAME`, `PR_SET_SECCOMP`, ...)
  is still outside the allowlist and ends the child. What the NSS module
  does next (a varlink `socket`/`connect` to systemd-userdb) is `EPERM`, so
  the lookup ends "user not found" and onnxruntime carries on.
- Making the call fail (`EPERM`) instead would also work for libcap (it
  treats an error as "no such capability") but would hide from libcap
  facts the child can read anyway; allowing the read-only option is the
  smaller surprise.
- **Operability.** On x86-64 the filter's default action is `TRAP` instead
  of `KILL_PROCESS`, to a 93-byte SIGSYS handler (machine code in an
  `mmap`ed read-execute page, `install_sigsys_reporter`) that writes
  `sandbox: seccomp denied syscall <n>` to stderr and calls
  `exit_group(159)`. The refused call still never runs; the runner reports
  `syscall_denied` with `exit status 159, syscall <n> (<name>)` (name from
  libseccomp's table in the worker, only the number is taken from the
  child). `rt_sigaction` on `SIGSYS` is refused (`EPERM`) once the filter is
  loaded, so nothing in the child can replace the handler, and a thread
  that blocks `SIGSYS` is killed by the kernel instead (a blocked forced
  signal is reset to its default action). Other architectures keep
  `KILL_PROCESS` (no reporter; writing machine code there would need
  explicit instruction-cache maintenance) and the number is in the host's
  audit log (docs/OPERATIONS.md).

**Consequences.** `tests/security/test_sandbox.py::
test_user_database_lookups_do_not_kill_the_child` (as the current user and
as UID 65534; looks up the user and group databases and loads libcap in the
hardened child), `test_prctl_only_reads_the_capability_bounding_set` (and
that `PR_SET_DUMPABLE` 1 is still killed),
`test_a_refused_syscall_cannot_be_survived_by_replacing_the_sigsys_handler`,
`test_denied_syscall_is_read_only_from_the_reporter_line` and
`tests/security/test_sandbox_documents.py::
test_text_pdf_tables_as_an_unprivileged_uid` cover it; the first and the
last fail without the `prctl` rule on a host whose NSS falls through to
`systemd`. A machine with an exploitable parser and arbitrary code
execution could remap and patch the handler page, but that only changes
what happens *after* a refused call, which still does not run.

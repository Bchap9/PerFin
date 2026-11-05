# Ingestion: what leaves the host

Scope: the smart-ingestion paths of Phases 3-4 (upload, drop folder, email
intake, extraction, OCR, classification, parsing, receipt matching,
search). This page answers one question for an operator or reviewer: when
a statement, receipt or mail goes through PerFin, which bytes cross a
network boundary, and to where. Controls are referenced by their
`docs/SECURITY.md` id; their implementation status is in
[`CONTROLS.md`](CONTROLS.md).

## Short answer

**Document content never leaves the host.** No part of the pipeline calls
an external service with a document, its text, its extracted fields or its
metadata:

- **No language model**, local or remote (ADR-0007; `INGESTION.md`, "No
  models in the pipeline"). Classification is YAML fingerprints plus a
  naive Bayes model shipped with the code and trained offline on synthetic
  samples; parsing, OCR and receipt matching are deterministic code.
- **OCR is local.** Tesseract runs in process (`tesserocr`) inside the
  sandboxed extraction child, reading the language data installed in the
  image. No cloud OCR.
- **Antivirus is local.** With `PERFIN_ANTIVIRUS=clamav` the worker
  streams the decrypted quarantined file to clamd over the internal
  `backend` Docker network (`tcp://clamav:3310`) or a Unix socket
  (`PERFIN_CLAMAV_ADDRESS`); the clamd port is never published. No
  cloud scanning, no hash lookups.
- **Search is local.** The full-text index is a PostgreSQL `tsvector`
  column in the household's own rows (RLS-scoped).
- **Previews are local.** Page images are rendered in the sandbox and
  stored encrypted in the blob store (C5, D2 / ADR-0003).

## The only egress: the IMAP mailbox you configure

Email intake (Phase 4, off by default: `PERFIN_EMAIL_INTAKE_ENABLED`) is
the single ingestion feature that opens an outbound connection, and only to
the mailbox host an owner or admin entered in Settings > Intake:

| Property | Behaviour |
| --- | --- |
| Destination | The configured `host:port` (993 by default) and nothing else. No autodiscovery, no MX lookups, no remote images or links in mail bodies are ever fetched. |
| Transport | Implicit TLS (`IMAP4_SSL`), TLS 1.2 or later, certificate and host name verified against the system store (a private CA through `SSL_CERT_FILE`). No `STARTTLS`, no plaintext fallback. |
| Path | With the shipped compose and `HTTPS_PROXY` set, a `CONNECT host:993` tunnel through the egress proxy, which reaches only hosts listed in `deploy/egress-allowlist.txt` and never private addresses. Without a proxy, every resolved address is vetted like the SSRF-safe HTTP client; private and loopback addresses are refused unless `PERFIN_EMAIL_INTAKE_ALLOW_PRIVATE_HOSTS=true`. |
| What is sent | The mailbox login (app password, stored column-encrypted, D1) and IMAP commands to list, fetch and move messages. No document content is uploaded. |
| What is read | Header blocks (capped at 64 KiB) for the acceptance policy; only accepted messages are fetched whole, then parsed in the sandbox as `.eml` documents. |

Everything the worker receives from the mailbox is treated as hostile input:
the same quarantine, size caps, antivirus scan and sandboxed extraction as an
upload (C1-C10, D1-D5).

## Other outbound connections (not ingestion)

Listed so the picture is complete; none of them carries document data.

- **OIDC** (optional, Phase 1): discovery, JWKS and token exchange with the
  configured identity provider, through the egress proxy.
- **Breached-password check** (optional): the k-anonymity range API sends
  the first five hex characters of a SHA-1 hash of a new password, never the
  password; a local list is always used first.
- **ClamAV signature updates** (optional `av` profile): `freshclam` in the
  clamav container fetches signature databases through the egress proxy
  when `database.clamav.net` is allowlisted. It downloads; it uploads
  nothing about scanned files.

## Inside the host: who sees what

| Component | Sees document plaintext? | Network |
| --- | --- | --- |
| API | Only while streaming an upload to sniff its type, hash it and encrypt it (never parsed). Previews and downloads are decrypted as they stream to an authorised caller (I1 viewer rule). | `web`, `backend` |
| Worker | Decrypts a quarantined blob to stream it to clamd and to the extraction child's stdin. | `backend`, optional `egress` |
| Extraction child | Parses the file. Default-kill seccomp allowlist, no sockets (`EPERM`), no exec, no new processes, no file writes, Landlock read-only view of the interpreter and system libraries only, rlimits and a wall-clock timeout (C1, ADR-0006 and its addendum, ADR-0009). | none: socket calls are refused |
| clamd | The plaintext stream of each scanned file. | `backend` (plus `egress` for signature updates) |
| PostgreSQL | Extracted text and fields of the household's documents (RLS, F3). | `backend` only |
| Blob store | Ciphertext only (XChaCha20-Poly1305 per-document keys, D2). | local volume |

## Drop folder

The drop folder is a host directory mounted **read-only** into the worker;
processed files go to a separate writable directory. It is local
filesystem access, not network egress. Files are copied into quarantine and
encrypted before anything parses them (C11).

## Verifying it

- `backend/tests/security/test_sandbox.py`: the child cannot open sockets,
  exec, fork, signal other processes, write files, or make a syscall outside
  its allowlist (`test_syscall_outside_the_allowlist_kills_the_child`).
- `backend/tests/security/test_imaps_tls.py`: implicit TLS only (a
  plaintext server is never spoken to in the clear), untrusted or
  wrong-name certificates refused before login, the `CONNECT` tunnel through
  the egress proxy, loopback only with explicit permission;
  `backend/tests/unit/test_email_policy.py`: certificate and host-name
  checks of the client context, direct connections refusing forbidden
  addresses, proxy taken from the environment.
- `backend/tests/integration/test_antivirus_pipeline.py`: clamd over a
  local address; fail-closed by default
  (`test_scanner_down_fails_closed_retry_then_failed`).
- `backend/tests/unit/test_egress_proxy.py`: the egress proxy refuses hosts
  outside the allowlist and private addresses.
- Live run (Phases 3+4 finalization, 2026-11-04): a fresh database, `perfin
  api` and `perfin worker` with a fake local clamd; statements, a scanned
  PDF, a receipt photo, a drop-folder file and search, with no outbound
  connection made by the worker (email intake not configured). See
  `CHANGELOG.md`, "Resume here".

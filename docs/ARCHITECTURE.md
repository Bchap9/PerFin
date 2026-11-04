# Architecture

## Overview

PerFin is a modular monolith: one codebase, one container image, two runtime
roles (API and worker), one PostgreSQL database, one encrypted document store.
A reverse proxy terminates TLS in front. Everything that touches untrusted file
content runs in the worker role in a more tightly sandboxed container than the
API.

```
                         ┌──────────────────────────────────────────────┐
   Browser  ──HTTPS──▶   │  Reverse proxy (Caddy/Traefik)               │
                         │  TLS, HSTS, rate limiting, body size limits  │
                         └───────────────┬──────────────────────────────┘
                                         │ HTTP (private network)
                         ┌───────────────▼──────────────┐
                         │  perfin-api (FastAPI)        │
                         │  - auth, sessions, OIDC      │
                         │  - REST + SSE                │
                         │  - serves built SPA          │
                         │  - enqueues jobs             │
                         └───────┬──────────────┬───────┘
                                 │              │
             ┌───────────────────▼───┐    ┌─────▼──────────────────────┐
             │  PostgreSQL 16        │    │  Document store            │
             │  - ledger, users,     │◀───│  (encrypted blobs: local   │
             │    jobs, audit        │    │   FS default, S3 optional) │
             └───────────▲───────────┘    └─────▲──────────────────────┘
                         │ SKIP LOCKED queue     │
             ┌───────────┴───────────────────────┴──────────┐
             │  perfin-worker (same image, hardened)        │
             │  - no inbound network, egress denied by      │
             │    default (allowlist for IMAP/price feeds)  │
             │  - drop-folder watcher                       │
             │  - IMAP poller                               │
             │  - classify / OCR / parse / dedupe / rules   │
             │  - snapshots, schedules, notifications       │
             └──────────────────────────────────────────────┘
                         ▲
        Drop folder ─────┘ (read-only bind mount into worker; worker moves
        (host path)        processed files into the encrypted store)

   AI client (MCP host) ─▶ perfin-mcp (optional role, API token only,
                           identity scrubbed, read-only) ─▶ perfin-api

   Authentik (OIDC) ◀──── browser redirects + back-channel token exchange from API
   Receipts mailbox ◀──── worker polls over IMAPS
```

## Components

### Reverse proxy
Caddy is the documented default because it handles TLS automatically and the
config is short. Traefik and nginx examples will be provided. Responsibilities:
TLS 1.2+/1.3 only, HSTS preload-ready, request body limit (default 50 MB for
uploads, 1 MB elsewhere), connection and request rate limits, and forwarding
`X-Forwarded-*` headers that the API is configured to trust only from the proxy
network.

### perfin-api
FastAPI application. Stateless apart from the DB. Serves the compiled SPA from
`/` and the API from `/api/v1`. Owns:

- Authentication (local + OIDC), session management, CSRF, MFA.
- Authorisation (household membership, per-account visibility, roles).
- All reads and writes to the ledger, with Pydantic schemas at the boundary.
- Upload endpoint: streams the file straight into quarantine storage with a
  size cap, computes a hash, writes a `document` row with status `received`,
  enqueues an `ingest_document` job. The API process never parses file content.
- Server-sent events endpoint for live updates (job progress, new review items,
  balance changes). Clients use TanStack Query and invalidate on events.
- Audit log writes.

Implemented in Phase 2: every change writes an `event` row and a
`pg_notify('perfin_events', ...)` in the same transaction
(`app.core.events.emit`), so a notice exists only once the change commits;
the notice carries `{id, household_id, type}` only. `GET /api/v1/events`
holds one `LISTEN` connection per stream, filters each event by the
subscriber's household, role and account grants, sends `id` and `type`
(never the payload), and replays from `Last-Event-ID` within the event
retention window. Streams are capped per user and per API process, re-check
their session periodically and end after `PERFIN_SSE_MAX_STREAM_SECONDS`
(the browser reconnects). Writes accept an optional `Idempotency-Key`
(`app.core.idempotency`, keys kept 24 hours per household and user).

### perfin-mcp (optional)
Same image, entrypoint `perfin mcp`. Speaks the Model Context Protocol over
stdio or streamable HTTP and holds exactly one scoped read-only API token. It
has no database credentials, no master key and no document mount. Every
response passes through the identity denylist and free-text scrubber before it
reaches the assistant. Details in AI_ACCESS.md.

### perfin-worker
Same image, different entrypoint, stricter container settings (see
SECURITY.md "Container hardening"). Pulls jobs from a Postgres table using
`SELECT ... FOR UPDATE SKIP LOCKED`. Job families:

- `ingest_document`: the full pipeline (see INGESTION.md).
- `imports.apply_mapping`: re-run a structured import with a saved CSV
  column mapping (Phase 2).
- `transactions.auto_pair`: after an import, pair transfers between the
  accounts the document's uploader may import into (Phase 2).
- `portability.export`, `portability.import`, `portability.expire`: full
  export and import archives (Phase 2, PORTABILITY.md).
- `watch_dropfolder`: long-running; inotify where available, polling fallback.
- `poll_mailbox`: IMAP IDLE/poll on a schedule.
- `snapshot_net_worth`: daily.
- `refresh_prices`: scheduled, only if a price provider is enabled.
- `run_rules`: re-apply rules after a rule change.
- `send_notification`: fan out to configured channels.
- `backup_export`: produce encrypted export archives.

Each job has a timeout, a retry policy, a dead-letter state and a per-user
concurrency limit. OCR and parsing run in a subprocess with CPU and memory
limits so a pathological PDF cannot take down the worker.

Status after Phase 2: `ingest_document`, `imports.apply_mapping`,
`run_rules`, `transactions.auto_pair`, `portability.*` and the identity
maintenance jobs exist. `watch_dropfolder`, `poll_mailbox`,
`snapshot_net_worth`, `refresh_prices`, `send_notification` and
`backup_export` are later phases. The extraction subprocess is built
(`app.ingestion.sandbox`: a fresh interpreter per document, rlimits on
memory, CPU, open files and file size, a wall-clock timeout, its own process
group, Landlock limiting the filesystem to reading the interpreter's own
paths, a seccomp filter that denies network, exec, ptrace-style calls and
every filesystem change, a non-dumpable worker so a same-UID child cannot
read its environment or memory (ADR-0009), privileges dropped to
`PERFIN_EXTRACT_UID` when the worker runs as root,
plaintext only on stdin and the validated result on stdout; ADR-0006). The
seccomp filter is a deny list, not yet a default-deny allowlist (SECURITY.md
C1 partial).

### PostgreSQL
Single database, schema-managed with Alembic. Row-level security policies
enforce household scoping as a second line of defence behind application
authorisation. JSONB columns hold raw parser output for debugging and
re-processing. Full-text search (`tsvector`) over extracted document text.

### Document store
Abstract `BlobStore` interface with two implementations: local filesystem
(default) and S3-compatible (MinIO, Backblaze B2, etc.). Every blob is
encrypted client-side by the app with a per-object data key (XChaCha20-Poly1305
via libsodium or AES-256-GCM), the data key wrapped by a master key. The master
key comes from an environment variable, a mounted file, or (later) a KMS. Blob
names are random UUIDs; original filenames live only in the database. Blobs are
never served directly; the API decrypts and streams with `Content-Disposition:
attachment` and a strict content type.

Implemented in Phase 2: `LocalFSBlobStore` only, with two areas
(`quarantine` for files not yet accepted, the permanent `documents` area).
Each object is encrypted as a stream (libsodium secretstream,
XChaCha20-Poly1305 per chunk) under its own data key, with the blob id as
associated data; the data key is wrapped by the master key (ADR-0003). The
S3-compatible store is deferred: `S3BlobStore` is an interface that refuses
to run (ROADMAP "Later / ideas parking lot").

### Frontend
React 18 + TypeScript, built with Vite, routed with TanStack Router, data
fetching via TanStack Query with an SSE-driven invalidation layer
(`src/lib/sse.ts` maps event types to query roots). Charts use Recharts
(account balance history); simple bar summaries are plain CSS. Design system: a small component
set with light/dark themes. No server-side rendering is needed; the SPA is
static assets served by the API with a strict CSP (no inline scripts, hashed
styles).

### Optional integrations (all off by default)
- **MCP server** (`perfin mcp` role): a read-only window for AI assistants
  that exposes financial facts at full detail and never identity (addresses,
  DOB, SSN, account numbers, names, documents). PerFin never calls a model
  itself. See AI_ACCESS.md and ADR-0007.
- **Price feeds** for securities and FX.
- **Notification channels**: SMTP, webhooks, ntfy, Gotify, Matrix.
- **Antivirus**: ClamAV sidecar scanning quarantined files before parsing.

## Key flows

### Upload a statement
1. Browser `POST /api/v1/documents` (multipart, CSRF token, session cookie).
2. API checks size, sniffs magic bytes, rejects disallowed types, streams to
   quarantine blob, computes SHA-256, inserts `document(status=received)`,
   enqueues job, returns `202` with document id.
3. Worker picks the job: scans (optional AV), extracts text in a sandboxed
   subprocess, classifies, fingerprints the institution, parses, deduplicates,
   runs rules, computes confidence per field, writes transactions or review
   items, moves blob from quarantine to the permanent store, sets
   `status=processed` or `needs_review`. (Phase 2 handles structured files
   only: CSV/TSV, OFX/QFX, QIF; there is no antivirus or per-field
   confidence yet, and a CSV without a matching saved column mapping waits
   in review until one is saved.)
4. Worker emits a `document.updated` event; API relays over SSE; the UI
   refreshes the document card and the review badge.

### Drop folder
1. Worker watches the mounted directory. A new file is considered complete when
   its size is stable for N seconds and it is not open for writing (or a
   `.ready` sidecar exists, configurable).
2. Worker copies the file into quarantine (never processes in place), records
   provenance (`source=dropfolder`, original path, mtime), and either deletes
   the original or moves it to a `processed/` or `failed/` subfolder depending on
   configuration.
3. Same pipeline as upload. Files that cannot be attributed to a household
   member or account produce a review item: "Who does this belong to?"

### Email intake
1. Worker polls a dedicated mailbox over IMAPS with an app password or OAuth
   token stored encrypted.
2. A message is accepted only if it passes the configured sender policy
   (allowlisted addresses AND, where headers are available, SPF/DKIM pass) and
   is addressed to the household's secret alias (e.g.
   `receipts+a8f3k2@yourdomain`). Everything else is moved to a `rejected`
   folder, never processed.
3. Attachments (and the HTML/text body for e-receipts) go through the same
   quarantine and pipeline. The email itself is stored as a `.eml` document for
   provenance.

### Login (local)
Argon2id verification, optional MFA step (WebAuthn preferred, TOTP supported),
server-side session created, cookie set `HttpOnly; Secure; SameSite=Lax;
Path=/`, CSRF token issued. See AUTH.md.

### Login (OIDC / Authentik)
Authorization Code with PKCE, `state` and `nonce` validated, ID token
signature/issuer/audience/expiry validated against the discovered JWKS, user
matched by `sub` (never by email alone), groups claim mapped to roles. See
AUTH.md.

## Configuration

Twelve-factor: environment variables for secrets and deployment settings,
database for user-facing settings. A `perfin config check` command validates the
environment at startup and refuses to boot with insecure settings (default
secret, missing master key, `SECURE_COOKIES=false` without `DEV_MODE`).

## Observability

Structured JSON logs with PII scrubbing (no account numbers, no document text,
no emails in logs at INFO). Prometheus metrics endpoint on a separate internal
port (never exposed through the proxy). Health endpoints: `/healthz` (process
alive) and `/readyz` (DB reachable, migrations current).

## Deployment topology

Default: a single host running Compose with `proxy`, `api`, `worker`, `db`,
and optional `clamav`, `mcp`. The worker has `network_mode` restricted to
the internal network plus an egress allowlist implemented with a small
forward-proxy sidecar when IMAP or price feeds are enabled. Volumes: `pgdata`,
`documents` (encrypted blobs), `quarantine`, and the user-supplied drop folder
bind-mounted read-only into the worker.

Later: a Helm chart with the same roles as separate Deployments.

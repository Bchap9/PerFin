# Operating PerFin

Day-two reference for a Docker Compose installation ([DEPLOYMENT.md](DEPLOYMENT.md)).
All commands run in the `deploy/` directory. Service names are `proxy`, `api`,
`worker`, `db`, `migrate` (one-shot) and, with profiles, `egress`, `clamav` and
`mcp`.

## Quick status

```sh
docker compose ps                                  # health of every service
docker compose run --rm --no-deps api config check # configuration problems, explained
docker compose run --rm --no-deps migrate audit verify   # audit-log hash chain
docker compose exec api perfin healthcheck && echo api ok
```

## Logs

Every PerFin process (`api`, `worker`, `mcp`, `migrate`, CLI commands) writes
one JSON object per line to stderr/stdout; Docker collects it with the
`json-file` driver, capped at 10 MB x 5 files per container (see the
`logging` anchor in `docker-compose.yml`).

```json
{"ts": "2026-11-04T18:40:12.123456+00:00", "level": "INFO", "logger": "perfin.worker",
 "msg": "...", "request_id": "0192...", "job_id": "...", "job_type": "..."}
```

- `ts` is UTC. `request_id` ties a log line to an API response header and an
  audit row; workers bind one per job. Extra fields vary by event.
- HTTP access lines carry method, path and status only; query strings are
  removed before formatting.
- Identifiers are scrubbed from messages and exception text before they are
  written (`[ACCT]`, `[GOVID]`, `[EMAIL]`, `[PHONE]`, `[DOB]`, `[REDACTED]`);
  passwords, tokens, cookies, keys and document text are never logged (H8).
  Do not turn that off for debugging: `PERFIN_LOG_LEVEL=DEBUG` is refused
  outside `PERFIN_DEV_MODE`.
- The one secret that does appear is the one-time setup link (first boot only).
- `proxy` logs JSON access lines (no query, cookies, `Authorization`); `db`
  logs connections and disconnections.

```sh
docker compose logs -f --since 15m api worker
docker compose logs api | jq -r 'select(.level=="ERROR" or .level=="WARNING") | [.ts,.logger,.msg] | @tsv'
docker compose logs worker | jq -c 'select(.job_id=="<job id>")'
```

Ship them elsewhere by pointing Docker's logging driver at your collector;
PerFin has no other log sink. Keep the audit log (the `audit_log` table, not
the process log) for security questions: it is append-only and
hash-chained.

## Health endpoints

| Endpoint | Meaning | Used by |
| --- | --- | --- |
| `GET /healthz` | The process is serving requests. Never touches the database. Returns `{"status":"ok"}`. | Image `HEALTHCHECK` (`perfin healthcheck` probes `127.0.0.1:8000/healthz`) |
| `GET /readyz` | The database answers as `perfin_app` **and** its Alembic revision equals the head of the migrations shipped in this image. `503 {"status":"unavailable"}` otherwise; the reason is in the `api` log (`perfin.health`, "not ready: ..."). | Orchestrators, your own checks |

Both are unauthenticated, return nothing else, and are answered `404` by the
proxy: reach them from inside the network (`docker compose exec api python -c
"import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/readyz').read())"`).
The worker serves no HTTP; its container health check requests its internal
metrics endpoint.

## Metrics

`api` and `worker` each serve Prometheus text format at `/metrics` on
`PERFIN_METRICS_PORT` (default 9100). The listener binds to
`PERFIN_METRICS_BIND_HOST`, which defaults to `127.0.0.1` **inside the
container**, so neither the proxy nor any other container can reach it, and
nothing publishes it (H7). To scrape it, either run a Prometheus exporter
sidecar with `network_mode: "service:api"`, or set
`PERFIN_METRICS_BIND_HOST` to the container's address on the `backend`
network and attach your Prometheus container to that network only; never
publish the port or route it through the proxy. The worker's health check
uses `127.0.0.1`, so keep loopback reachable (bind to `0.0.0.0` on the
internal network if you need both).

| Metric | Type | Labels | Watch for |
| --- | --- | --- | --- |
| `perfin_http_requests_total` | counter | `method`, `route` (template), `status` | rising `5xx`; `401`/`429` bursts on `/api/v1/auth/*` (credential stuffing) |
| `perfin_http_request_duration_seconds` | histogram | `method`, `route` | p95 over a few seconds; statement timeouts (30 s) |
| `perfin_jobs_total` | counter | `type`, `outcome` | `outcome` of `error`, `permanent_error`, `timeout`, `lease_lost`, `invalid_payload`, `unknown_type` |
| `perfin_job_duration_seconds` | histogram | `type` | extraction or import jobs approaching their timeout |
| `process_*`, `python_gc_*`, `python_info` | standard | | resident memory near the container limit (1 GB; worker 2 GB), open file descriptors |

`outcome` values: `succeeded`, `error` (retry scheduled), `permanent_error`
(dead-lettered), `timeout`, `lease_lost` (result discarded), `invalid_payload`
and `unknown_type` (both dead-lettered). Route labels are templates
(`/api/v1/accounts/{account_id}`), so cardinality is bounded.

Useful alerts: `/readyz` failing for 2 minutes; `increase(perfin_jobs_total
{outcome=~"permanent_error|invalid_payload|unknown_type"}[1h]) > 0`;
`rate(perfin_http_requests_total{status=~"5.."}[5m])` above your baseline;
a container restarting (`docker inspect -f '{{.RestartCount}}'`); free disk
under 15 %.

## The worker, jobs and dead letters

PerFin uses PostgreSQL as its queue (ADR-0002): no Redis or broker. The
`worker` container claims rows of the `job` table with `FOR UPDATE SKIP
LOCKED`, runs the handler with a per-type timeout, and records the result.
Recurring maintenance (auth pruning, price refresh, mailbox polling, net
worth snapshots, tax reminders, drop-folder scans) is enqueued by the
worker's scheduler.

| State | Meaning |
| --- | --- |
| `queued` | Waiting. Runs when `run_after <= now()`, lowest `priority` first, then in enqueue order. |
| `running` | Leased by `locked_by` until `lease_expires_at` (handler timeout + 30 s). |
| `succeeded` | Done. Pruned after 7 days. |
| `failed` | The last attempt failed and the job will not run again as it is. Pruned after 30 days. |
| `dead` | Dead letter: `max_attempts` exhausted, a permanent error, an unknown job type or an invalid payload. Pruned after 30 days. |

A failed attempt returns to `queued` with exponential backoff and jitter
(5 s base, capped at 1 hour). A job whose lease expires (worker crashed or
was killed) is returned to `queued` by the reaper, or marked `dead` when its
attempts are used up. A handler that overruns its timeout is abandoned; if
three handlers overrun and cannot be stopped, the worker exits with code 3
and Docker restarts it (`restart: unless-stopped`).

Inspect the queue as the database superuser (the only role that bypasses
row-level security; it can log in only through the socket inside the `db`
container). The socket also uses `scram-sha-256`, so `psql` asks for
`POSTGRES_PASSWORD` from `.env`; for a non-interactive run pass it with
`docker compose exec -e PGPASSWORD=... db psql ...` (it then appears in your
shell history, so prefer typing it at the prompt):

```sh
docker compose exec db psql -U postgres -d perfin -c \
  "SELECT state, count(*) FROM job GROUP BY state ORDER BY 1"
docker compose exec db psql -U postgres -d perfin -c \
  "SELECT id, type, attempts, max_attempts, updated_at, left(last_error, 160) AS error
     FROM job WHERE state IN ('dead','failed') ORDER BY updated_at DESC LIMIT 20"
docker compose exec db psql -U postgres -d perfin -c \
  "SELECT id, type, locked_by, lease_expires_at FROM job WHERE state='running'"
```

`last_error` holds the exception class and a scrubbed message (never a
payload or document text). After fixing the cause, give a dead job a fresh
set of attempts:

```sh
docker compose exec db psql -U postgres -d perfin -c \
  "UPDATE job SET state='queued', attempts=0, run_after=now(), last_error=NULL,
          locked_by=NULL, locked_at=NULL, lease_expires_at=NULL, updated_at=now()
    WHERE id='<job id>' AND state IN ('dead','failed')"
```

Most user-visible work has its own state too: a document that cannot be
processed shows `failed` or `needs_review` in the web app, with a
**Reprocess** action, so check there before the queue.

## Common failure modes

### Migrations pending (`/readyz` is 503; api waits or refuses)

Symptom: `perfin.health` logs "database is not at the migration head"; the
`migrate` container is `Exited (1)`; `api` and `worker` never start
(`depends_on: migrate: service_completed_successfully`). This is expected
right after an upgrade until `migrate` finished.

Fix: read why it failed and re-run it.

```sh
docker compose logs migrate | tail -30
docker compose run --rm --no-deps migrate          # re-runs `perfin migrate`
docker compose up -d
```

Typical causes: the database is not reachable or healthy yet
(`docker compose ps db`), `PERFIN_MIGRATION_DATABASE_URL` or
`POSTGRES_OWNER_PASSWORD` wrong (owner role password differs from the one in
the volume: it is only read when the volume is created; fix with `ALTER ROLE`
inside the database), or an older image against a newer database (downgrades
are not supported; restore the pre-upgrade backup, see
[UPGRADING.md](UPGRADING.md#rollback)).

### Master key mismatch

Symptoms: the stack refuses to start with `master_key.missing`,
`master_key.placeholder` or `master_key.invalid`; documents will not open or
extract and the logs show decryption failures; a restore finishes but
`perfin restore-check` reports failed blobs.

```sh
docker compose exec api perfin backup-info          # key_id fingerprint + version
docker compose exec api perfin rotate-key --status  # exit 1: data keys under another version
```

Compare `key_id` with the one in your backups' manifests (`perfin
backup-info` records it there, never the key). The key must be the one the
data was encrypted with, with `PERFIN_MASTER_KEY_VERSION` matching (default
1). Recover the right key from your separate backup; never invent a new one:
a new key cannot decrypt old documents. After a *planned* rotation,
`rotate-key --status` must exit 0. See
[BACKUP_RESTORE.md](BACKUP_RESTORE.md#key-rotation).

### Sandbox: seccomp or Landlock unsupported

The worker parses untrusted files in a confined child (ADR-0006, ADR-0009).
Symptoms: documents end `failed` with extraction code `harden_failed`
(stderr `sandbox hardening failed: Landlock is not available`) or
`syscall_denied` / `crashed`.

- `harden_failed`: the host kernel has no Landlock (`grep landlock
  /sys/kernel/security/lsm` prints nothing, kernel older than 5.13, or a
  container host that disables it). Fix by running a kernel with Landlock.
  Only as a last resort set `PERFIN_EXTRACT_REQUIRE_LANDLOCK=false`
  and accept that seccomp and the non-dumpable worker are then the only
  layers; `perfin config check` warns.
- `syscall_denied`: the child hit the seccomp allowlist. Causes: a custom
  `seccomp` profile on the container (use Docker's default), `seccomp=unconfined`
  mixed with a missing `libseccomp2`, or a parser library (or C library,
  or kernel) update that needs a new syscall (report it with the file kind
  and the syscall below; do not widen the filter locally).
  `docker compose logs worker | jq 'select(.msg|test("sandbox"))'`.
  On x86-64 the log entry names the refused call: `detail` reads
  `exit status 159, syscall 157 (prctl)` and `stderr` holds the child's line
  `sandbox: seccomp denied syscall 157`. (The child's SIGSYS handler writes
  that line and exits with 159; the call itself never runs. On other
  architectures the child is killed outright, `detail` is
  `exit status -31`, and the number is in the host's audit log:
  `dmesg | grep type=1326` shows `syscall=<n>`.) Look the number up with
  `ausyscall <n>` or `python -c "import pyseccomp as s; print(s.resolve_syscall(s.Arch.NATIVE, <n>))"`.
- `memory_limit`, `cpu_limit`, `timeout`: the file is too big or complex for
  `PERFIN_EXTRACT_MEMORY_MB` (1024), `PERFIN_EXTRACT_CPU_SECONDS` (60) or
  `PERFIN_EXTRACT_TIMEOUT_SECONDS` (120, at most 600). Raise them together
  with the container's `mem_limit` only for files you trust.

### OCR missing

Symptom: scanned PDFs and photos end `failed` with `ocr_unavailable`
("no Tesseract language data" or "tesserocr is not installed"). The shipped
image includes Tesseract and English data; this appears with a self-built or
modified image. Check inside the worker:
`docker compose exec worker tesseract --list-langs` must list `eng`. Rebuild
from `deploy/Dockerfile` (it installs `tesseract-ocr`, `tesseract-ocr-eng`,
`libseccomp2`), or add other languages' data packages to your image.
Text-layer PDFs, CSV, OFX and QIF do not need OCR.

### IMAP (email intake) TLS errors

Symptom: the intake settings show a last poll outcome other than `ok`:
`tls_failed`, `connect_failed`, `blocked`, `auth_failed`, `folder_missing`,
`timeout` or `protocol_error`.

- `tls_failed`: the certificate does not verify (expired, wrong host name,
  private CA) or the server does not offer implicit TLS on 993. PerFin
  never falls back to plaintext or `STARTTLS`. For a private CA, give the
  worker its bundle: mount it and set `SSL_CERT_FILE`.
- `blocked`: the host is not in `deploy/egress-allowlist.txt` (restart
  `egress` after editing) or is a private address without
  `EGRESS_ALLOW_PRIVATE_CIDRS` / `PERFIN_EMAIL_INTAKE_ALLOW_PRIVATE_HOSTS`.
  `HTTPS_PROXY=http://egress:3128` must be set in `.env`.
- `connect_failed` / `timeout`: DNS, firewall or `PERFIN_EMAIL_INTAKE_TIMEOUT_SECONDS`.
- `auth_failed`: wrong user or app password (the password is write-only;
  re-enter it). `folder_missing`: the inbox/processed/rejected folder names
  do not exist on the server.

### OIDC issuer mismatch

Symptom: "Sign in with Authentik" fails and the `api` log says "discovery
issuer does not match PERFIN_OIDC_ISSUER exactly" or the ID token is
rejected on `iss`. The issuer is compared byte for byte, including a
trailing slash: copy the value from the provider's
`.well-known/openid-configuration` (`issuer` field), for example
`https://auth.example.com/application/o/perfin/`. Also check that the issuer
host is in the egress allowlist (the API reaches it only through the egress
proxy), that a LAN issuer has `PERFIN_OIDC_ALLOW_PRIVATE_ISSUER=true` and
`EGRESS_ALLOW_PRIVATE_CIDRS`, that the clock is in sync (60 s skew), and the
redirect URI at the provider is `<PERFIN_PUBLIC_URL>/api/v1/auth/oidc/callback`.
More in [AUTHENTIK.md](AUTHENTIK.md) "Troubleshooting". If SSO is down, use the
break-glass admin from an allowed CIDR.

### Disk full

Symptoms: PostgreSQL logs `No space left on device` and goes read-only or
crashes; uploads fail with `5xx`; the worker stalls. Find the culprit:

```sh
df -h /var/lib/docker
docker system df -v | head -40
docker compose exec api du -sh /var/lib/perfin/documents /var/lib/perfin/quarantine
docker compose exec db du -sh /var/lib/postgresql/data
```

Free space first (old backup archives on the host, `docker image prune`,
Docker logs are already capped), then `docker compose up -d`; PostgreSQL
recovers from its WAL. The quarantine area holds uploads until they are
processed or rejected; many old files there mean the worker is stuck or
down. Grow the volume rather than deleting document blobs by hand (blobs are
referenced by the database). Alert at 85 % used.

### Stuck jobs and documents

- A job `running` past `lease_expires_at` is requeued by the reaper within
  15 seconds of a worker being up; none are reaped if the `worker`
  container is down: `docker compose ps worker`, `docker compose logs
  worker`.
- Many `queued` jobs with a healthy worker: one household is limited to two
  concurrent jobs and long extraction jobs hold the slots; check
  `perfin_job_duration_seconds` and the slowest `running` rows.
- Documents sitting in `scanning`: with `PERFIN_ANTIVIRUS=clamav` and the
  fail mode `closed`, they wait while clamd is down or still downloading
  signatures (`docker compose ps clamav`, up to 5 minutes on first start).
- Drop-folder files never taken: the inbox must be readable by UID 10001;
  see the drop-folder section of [DEPLOYMENT.md](DEPLOYMENT.md) and
  `perfin config check`.
- A dead job that you fixed: requeue it as shown above, or use **Reprocess**
  on the document.

### Other quick checks

| Symptom | Look at |
| --- | --- |
| `403` on every unsafe request, login loops | `PERFIN_PUBLIC_URL` must equal the browser origin exactly; the proxy must pass `Host` and set `X-Forwarded-Proto`; `PERFIN_TRUSTED_PROXIES` must contain the proxy's address. |
| Live updates never arrive | The proxy is buffering `/api/v1/events` (DEPLOYMENT.md, SSE row). |
| Upload or import returns `413` | Proxy body limit below `PERFIN_UPLOAD_MAX_MB` + 2 or `PERFIN_IMPORT_MAX_MB` + 2. |
| `api` exits at start with `ERROR ...` lines | `docker compose run --rm --no-deps api config check` lists each setting and why it was refused. |
| Break-glass login refused | The client address must be in `PERFIN_BREAKGLASS_CIDRS`; behind Docker an SSH tunnel arrives from `172.30.79.1`. `perfin breakglass status`. |
| No price or FX updates | `PERFIN_PRICE_PROVIDER` / `PERFIN_FX_PROVIDER` set, host in the egress allowlist, `perfin_jobs_total{type=~".*prices.*"}` outcomes. |

## Routine tasks

| Task | How |
| --- | --- |
| Backup | `scripts/backup.sh` ([BACKUP_RESTORE.md](BACKUP_RESTORE.md)); schedule it and alert on failure. |
| Restore drill | Quarterly, into a fresh stack, with `perfin restore-check`. |
| Audit chain | `docker compose run --rm --no-deps migrate audit verify` weekly. |
| Upgrade | [UPGRADING.md](UPGRADING.md). |
| Rotate the master key | [BACKUP_RESTORE.md](BACKUP_RESTORE.md#key-rotation). |
| Rotate the email alias token | Settings > Intake (step-up). |
| Revoke an AI client or token | Settings > AI access, or the kill switch ([MCP_CLIENTS.md](MCP_CLIENTS.md)). |
| Check what leaves the host | [security/what-leaves-the-host.md](security/what-leaves-the-host.md). |

# PerFin

> [!WARNING]
> **Disclaimer: this project is entirely vibe coded.** PerFin was written by
> AI coding agents directed through prompts, not written line by line by a
> human. A person has not reviewed it line by line, and no external security
> review or penetration test has been done. Its tests and internal reviews
> are evidence, not proof.
>
> It is **not intended for, and not proven safe for, exposure to the open
> internet.** Run it only on a private network: your LAN, a VPN, or behind
> an authenticating gateway such as Authentik's forward-auth outpost. Use it
> at your own risk, keep your own backups, and do not rely on it as your only
> record of anything that matters. The AGPL-3.0 licence's "no warranty"
> terms apply in full.

**PerFin** is a self-hosted, security-first personal finance application. It is a
web application (served as a single page that loads and refreshes dynamically)
that lets a household track spending, savings, investments, loans, net worth and
tax documents from one place, with ingestion that does most of the work for you:
upload a statement, drop a file into a watched folder, or forward a receipt by
email, and PerFin works out what it is looking at and files it away.

> Status: **1.0.0 release candidate**, tagged `v1.0.0-rc.5`. Every roadmap phase
> (0-7) is implemented: login with MFA, WebAuthn and OIDC, households and
> account grants; the ledger (accounts, transactions, rules, budgets, goals,
> reports); smart ingestion of CSV/OFX/QIF/XLSX, text-layer and scanned PDFs,
> receipt photos and `.eml` mail through uploads, a watched drop folder and an
> IMAPS mailbox, parsed in a sandboxed subprocess and sent to a review queue
> when unsure; investments, loans, manual assets and net worth; a tax vault;
> a read-only MCP server for AI assistants behind an identity denylist;
> notification channels, a configurable dashboard, a PWA, encrypted
> backup/restore, master-key rotation and a Helm chart. Before the `v1.0.0`
> tag: an external security review is still required, and the release
> workflow (signed images, SBOMs, provenance) runs for the first time on that
> tag. See [docs/ROADMAP.md](docs/ROADMAP.md) for what is done and what is
> open, [docs/security/CONTROLS.md](docs/security/CONTROLS.md) for the status
> of every security control, and [CHANGELOG.md](CHANGELOG.md).

## Goals

- **Own your data.** Everything runs on hardware you control. No telemetry, no
  third-party sync, and no hosted model calls: files, account numbers and
  personal details have no code path off the premises. An optional local model
  container (`ml`/`ml-cuda` profiles, like Immich's machine learning) can
  suggest what an unfamiliar document is; it must run on your own network,
  and a person confirms everything it suggests ([docs/ML.md](docs/ML.md),
  ADR-0010).
- **AI help without identity exposure.** An optional read-only MCP server
  lets an assistant analyse spending, merchants, savings, holdings and loans
  in full detail, while addresses, dates of birth, SSNs, account numbers,
  names and documents are on a hard denylist. You preview exactly what a
  client sees before enabling it. Works with hosted or on-prem models.
- **Security as a feature, not a layer.** Threat-modelled from day one. Argon2id
  passwords, WebAuthn/TOTP MFA, OIDC via Authentik, encrypted document storage,
  sandboxed parsing of untrusted files, strict CSP, hardened containers.
- **Smart ingestion.** Statements (PDF, CSV, OFX/QFX, QIF, images) are classified,
  date-ranged, parsed, deduplicated and categorised automatically. Anything the
  system is not confident about lands in a review queue instead of silently
  polluting your ledger.
- **Many intake paths.** Browser upload, a watched drop folder (mount point), and
  a receipts mailbox.
- **Full picture.** Accounts, transactions, budgets, savings goals, investment
  holdings and performance, loans with amortisation, manual assets (house, car),
  net worth history, and a tax vault organised by tax year.
- **Highly configurable.** Categories, rules, institutions, parsers, dashboards
  and notification channels are all user-defined and exportable.

## Documents

| Document | What it covers |
| --- | --- |
| [docs/VISION.md](docs/VISION.md) | Product vision, personas, feature inventory, non-goals |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System components, tech stack, request/job flows, deployment topology |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | Core entities and their relationships |
| [docs/INGESTION.md](docs/INGESTION.md) | Upload, drop-folder and email pipelines; classification, parsing, review queue |
| [docs/AUTH.md](docs/AUTH.md) | Local login, MFA, sessions, OIDC with Authentik, authorisation model |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, identified vulnerabilities and the controls that address them |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Deploying on one Linux host with Docker Compose: secrets, master key, first boot, reverse proxy (Caddy, Traefik, nginx), drop folder, email intake, MCP role, hardening checklist |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Logs, health and metrics endpoints, jobs and dead letters, common failures and fixes |
| [docs/UPGRADING.md](docs/UPGRADING.md) and [docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md) | Verifying and installing releases, rollback, encrypted backups, restore, key rotation |
| [docs/security/what-leaves-the-host.md](docs/security/what-leaves-the-host.md) | Index of everything that can leave the host (nothing, by default), one page per integration: [ingestion](docs/security/ingestion.md), [prices and FX](docs/security/integrations.md), [notifications](docs/security/notifications.md), [AI access](docs/security/ai.md), [OIDC](docs/security/oidc.md), [email](docs/security/email.md) |
| [docs/MCP_CLIENTS.md](docs/MCP_CLIENTS.md) | Connecting Claude Desktop, Claude Code and on-prem hosts to the read-only MCP server |
| [docs/AI_ACCESS.md](docs/AI_ACCESS.md) | MCP server, identity denylist, free-text scrubbing, modes |
| [docs/API.md](docs/API.md) | API conventions and the first-cut endpoint surface |
| [docs/REPO_STRUCTURE.md](docs/REPO_STRUCTURE.md) | What lives where in this repository |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phased delivery plan with milestones |
| [docs/adr/](docs/adr/) | Architecture decision records |
| [SECURITY.md](SECURITY.md) | How to report a vulnerability |

## Stack (summary)

| Layer | Choice | Why |
| --- | --- | --- |
| API | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 | Strong typing at the boundary, best-in-class document/ML ecosystem for ingestion |
| Workers | Same codebase, separate hardened container, Postgres-backed job queue | One language, one deployable image, no Redis to run or secure |
| Database | PostgreSQL 16 | Transactions, row-level security, JSONB for parser payloads, `SKIP LOCKED` queues |
| Frontend | React 18 + TypeScript, Vite, TanStack Query/Router, server-sent events | Dynamic loading and live refresh with a small, well-understood toolchain |
| Documents | Encrypted blob store on the local filesystem (an S3-compatible store is a later idea) | Envelope encryption per file; originals are never served raw |
| Auth | Local (Argon2id + WebAuthn/TOTP) and OIDC (Authentik) | Both are first-class; OIDC-only mode available |
| Edge | Caddy or Traefik (TLS) in front of the app | TLS termination, HSTS, rate limits at the edge |
| Packaging | Docker Compose; Helm chart (`deploy/helm/perfin`) | Simple self-hosting story |

See [ADR-0001](docs/adr/0001-language-and-framework.md) for the reasoning and the
alternatives that were considered.

## Quick start

The full installation guide is [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
(secrets and master key, first boot, reverse proxy, drop folder, email
intake, MCP role, hardening checklist); Kubernetes users start from
[deploy/helm/perfin/README.md](deploy/helm/perfin/README.md), and upgrades
are covered by [docs/UPGRADING.md](docs/UPGRADING.md). The short version with
Docker Compose:

```bash
cd deploy
cp .env.example .env && chmod 600 .env
# Replace every CHANGE_ME (generation commands are at the top of the file):
#   PERFIN_MASTER_KEY        openssl rand -base64 32   (back it up separately)
#   PERFIN_SECRET_KEY        openssl rand -base64 48
#   POSTGRES_PASSWORD, POSTGRES_OWNER_PASSWORD, POSTGRES_APP_PASSWORD
#                            openssl rand -hex 32      (three different values)
# and set PERFIN_PUBLIC_URL to your https:// address.
cp Caddyfile.example Caddyfile          # set your host name
mkdir -p inbox processed && sudo chown 10001:10001 processed
docker compose up -d                    # db -> migrate -> api, worker -> proxy
docker compose run --rm --no-deps api config check   # explains any refused setting
```

Then open the one-time setup link that `api` prints while no user exists
(`docker compose logs api | grep -o 'https://[^" ]*/setup#t=[A-Za-z]*'`) to
create the first administrator; there are no default credentials.

`api`, `worker` and `mcp` refuse to start on insecure configuration
(placeholder secrets, non-https public URL, `DEBUG` logging, ...); `perfin
config check` lists every problem. `docker compose run --rm --no-deps migrate
audit verify` checks the audit-log hash chain as the owner role.

Full walk-through: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). The documentation
is also built as a site with `mkdocs-material` (`pip install --require-hashes -r
requirements-docs.txt && mkdocs build --strict`; CI runs the same).

### Development

`make dev` starts a local stack (Docker Postgres, migrations, API with reload,
worker, Vite on http://localhost:5173) on a **synthetic household**: accounts of
every type, months of transactions, budgets, goals, rules, a brokerage account,
a mortgage, manual assets, net worth history, a tax year with forms, and
documents with items waiting in the review queue. The seed creates the
`dev-admin` user with a **generated password printed once** (there are no
default credentials; `make dev-reset` starts over). Needs Linux (the
extraction sandbox uses seccomp and Landlock), Docker, `uv` and Node 22;
behind a registry mirror set `POSTGRES_IMAGE`. Details:
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#dev-stack).

`make check` runs every gate (SPDX, Ruff, mypy `--strict`,
Bandit, pytest against a local PostgreSQL 16, ESLint, `tsc`, Vitest, the
production build, `pip-audit`, `npm audit`); see
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Licence

PerFin is free software: you can redistribute it and/or modify it under the
terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version (`AGPL-3.0-or-later`). See [LICENSE](LICENSE) for the full text.

Every source file carries the SPDX identifier
`SPDX-License-Identifier: AGPL-3.0-or-later`; `scripts/check_spdx.py` enforces
it in CI. If you run a modified PerFin as a network service, the AGPL requires
you to offer your users the corresponding source.

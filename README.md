# PerFin

**PerFin** is a self-hosted, security-first personal finance application. It is a
web application (served as a single page that loads and refreshes dynamically)
that lets a household track spending, savings, investments, loans, net worth and
tax documents from one place, with ingestion that does most of the work for you:
upload a statement, drop a file into a watched folder, or forward a receipt by
email, and PerFin works out what it is looking at and files it away.

> Status: **design phase**. This repository currently holds the architecture,
> security model, data model and roadmap. See [docs/ROADMAP.md](docs/ROADMAP.md)
> for what gets built when.

## Goals

- **Own your data.** Everything runs on hardware you control. No telemetry, no
  third-party sync, and no model calls inside the app: files, account numbers
  and personal details have no code path off the premises.
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
| [docs/AI_ACCESS.md](docs/AI_ACCESS.md) | MCP server, identity denylist, free-text scrubbing, modes |
| [docs/API.md](docs/API.md) | API conventions and the first-cut endpoint surface |
| [docs/REPO_STRUCTURE.md](docs/REPO_STRUCTURE.md) | What lives where in this repository |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phased delivery plan with milestones |
| [docs/adr/](docs/adr/) | Architecture decision records |
| [SECURITY.md](SECURITY.md) | How to report a vulnerability |

## Planned stack (summary)

| Layer | Choice | Why |
| --- | --- | --- |
| API | Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 | Strong typing at the boundary, best-in-class document/ML ecosystem for ingestion |
| Workers | Same codebase, separate hardened container, Postgres-backed job queue | One language, one deployable image, no Redis to run or secure |
| Database | PostgreSQL 16 | Transactions, row-level security, JSONB for parser payloads, `SKIP LOCKED` queues |
| Frontend | React 18 + TypeScript, Vite, TanStack Query/Router, server-sent events | Dynamic loading and live refresh with a small, well-understood toolchain |
| Documents | Encrypted blob store (local filesystem by default, S3-compatible optional) | Envelope encryption per file; originals are never served raw |
| Auth | Local (Argon2id + WebAuthn/TOTP) and OIDC (Authentik) | Both are first-class; OIDC-only mode available |
| Edge | Caddy or Traefik (TLS) in front of the app | TLS termination, HSTS, rate limits at the edge |
| Packaging | Docker Compose; Helm chart later | Simple self-hosting story |

See [ADR-0001](docs/adr/0001-language-and-framework.md) for the reasoning and the
alternatives that were considered.

## Quick start (target state, not yet functional)

```bash
cp deploy/.env.example deploy/.env   # fill in secrets
docker compose -f deploy/docker-compose.yml up -d
```

## Licence

To be decided before the first public release (AGPL-3.0 is the current leaning
for a self-hosted application; see roadmap Phase 0).

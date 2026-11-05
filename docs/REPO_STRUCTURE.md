# Repository structure

```
PerFin/
├── README.md
├── SECURITY.md                 vulnerability disclosure policy
├── .editorconfig
├── .gitignore
├── .github/
│   └── workflows/              ci.yml (lint/type/test, helm chart), security.yml (trivy, gitleaks, audit),
│                               release.yml (tags only: multi-arch image, scan, SBOM, cosign, provenance)
├── docs/
│   ├── VISION.md
│   ├── ARCHITECTURE.md
│   ├── DATA_MODEL.md
│   ├── INGESTION.md
│   ├── AUTH.md
│   ├── API.md
│   ├── SECURITY.md             threat model and controls
│   ├── ROADMAP.md
│   ├── REPO_STRUCTURE.md       this file
│   └── adr/                    architecture decision records
├── backend/                    Python 3.12, managed with uv
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── alembic.ini
│   ├── migrations/             Alembic versions
│   ├── app/
│   │   ├── main.py             FastAPI app factory, middleware stack
│   │   ├── cli.py              `perfin` CLI: config check, migrate, ingest --dry-run, worker
│   │   ├── core/               settings, security headers, csrf, crypto (envelope), logging, errors
│   │   ├── db/                 engine, session, RLS context, base models, queue (SKIP LOCKED)
│   │   ├── auth/               local, mfa (webauthn/totp), sessions, oidc, tokens, authz
│   │   ├── domain/             one package per bounded context:
│   │   │   ├── households/
│   │   │   ├── accounts/
│   │   │   ├── transactions/   incl. rules engine, transfers, dedupe
│   │   │   ├── budgets/
│   │   │   ├── investments/
│   │   │   ├── loans/
│   │   │   ├── networth/
│   │   │   ├── documents/      blob store, document lifecycle, review items
│   │   │   └── tax/
│   │   ├── ingestion/
│   │   │   ├── intake/         upload, dropfolder, email
│   │   │   ├── sandbox/        subprocess runner, rlimits, seccomp
│   │   │   ├── extract/        pdf, image/ocr, csv, ofx, qif, xlsx, eml
│   │   │   ├── classify/       fingerprints (yaml), heuristic model, column-mapping
│   │   │   ├── parsers/        per-kind and per-institution parsers + fixtures
│   │   │   ├── match/          dedupe, transfers, receipt linking
│   │   │   └── pipeline.py     stage orchestration
│   │   ├── integrations/       prices, fx, notifications, antivirus
│   │   ├── mcp/                MCP server role: tools, modes, scrubber, transports
│   │   ├── workers/            job handlers, scheduler, watcher processes
│   │   └── api/                routers per domain, SSE, schemas
│   └── tests/
│       ├── unit/
│       ├── integration/        against a real Postgres (testcontainers)
│       ├── security/           authz matrix, csrf, oidc negatives, upload fuzz corpus,
│       │                       scrubber leak corpus and column classification test for ai:read
│       └── fixtures/           synthetic statements only
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── index.html
│   ├── public/
│   └── src/
│       ├── app/                router, providers, layout, theme
│       ├── lib/                api client, sse, query keys, money/date utils
│       ├── components/         design system
│       └── features/           one folder per domain mirroring backend/app/domain
├── deploy/
│   ├── docker-compose.yml      hardened default deployment
│   ├── .env.example
│   ├── Caddyfile.example
│   ├── Dockerfile              multi-stage: frontend build -> backend image
│   └── helm/perfin/            Helm chart: api, worker, mcp, migrate hook, NetworkPolicies (docs/UPGRADING.md)
└── scripts/
    ├── backup.sh / restore.sh  encrypted backups and verified restore (docs/BACKUP_RESTORE.md)
    └── dev.sh                  local dev bootstrap (later)
```

Conventions:
- Backend domain packages expose a `service.py` (use cases), `models.py`
  (SQLAlchemy), `schemas.py` (Pydantic in/out), `repo.py` (queries). Routers
  in `app/api` call services only.
- Nothing in `app/api` or `app/domain` imports from `app/ingestion/extract`;
  file content handling is confined to the worker role.
- Frontend features own their queries, components and routes; `lib/api` is
  generated from the OpenAPI schema.

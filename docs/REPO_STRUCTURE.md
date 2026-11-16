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
│   ├── USING.md                finding your way around the app: sections, old addresses, keyboard
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
│   │   │   ├── categorisation/ payee clean-up, the bundled merchant dictionary (data/), what was learned, the engine
│   │   │   ├── budgets/
│   │   │   ├── investments/
│   │   │   ├── loans/
│   │   │   ├── networth/
│   │   │   ├── documents/      blob store, document lifecycle, review items
│   │   │   ├── inbox/          the Inbox count (GET /inbox/summary), read-only
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
│   ├── scripts/                check-bundle.mjs (bundle budget after the build), icon generation
│   └── src/
│       ├── app/                routeMap.ts (sections, tabs, redirects), routeTree.tsx (every page,
│       │                       lazily loaded), shell (rail, bottom bar), command palette, providers, theme
│       │   └── styles/         tokens.css (every colour, light and dark), fonts, layout, charts
│       ├── lib/                api client, sse, query keys, URL state, media queries, money/date utils
│       ├── components/         design system (drawer, menu, section tabs, page header, stats, chips, charts)
│       ├── test/               axe check for page tests
│       └── features/           one folder per domain; app/routeTree.tsx mounts their pages
│                               under the six sections and Settings
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
- Frontend features own their queries, components, styles and tests. Their
  pages are registered in `app/routeMap.ts` and `app/routeTree.tsx`, not in
  the feature (DEVELOPMENT.md "Plug-in points"). `lib/api` is generated from
  the OpenAPI schema.

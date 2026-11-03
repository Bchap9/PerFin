# Development conventions

This is the contributor contract: how the code is laid out, how the pieces
plug together, and which gates every change must pass. The design documents
(ARCHITECTURE, DATA_MODEL, API, AUTH, INGESTION, AI_ACCESS, SECURITY) say
*what* to build; this file says *how* to build it so independent changes fit
together. When code and this file disagree, fix one of them in the same
commit.

## Toolchain

| Area | Tool | Command |
| --- | --- | --- |
| Python | 3.12, managed by **uv** (`backend/uv.lock`, hash-locked) | `cd backend && uv sync` |
| Lint/format | Ruff (lint + format) | `uv run ruff check . && uv run ruff format --check .` |
| Types | mypy `--strict` (config in `pyproject.toml`, covers `app` and `tests`) | `uv run mypy` |
| Python SAST | Bandit (`app/` only) | `uv run bandit -c pyproject.toml -r app` |
| Python deps | pip-audit | `uv run pip-audit` |
| Tests | pytest (+ pytest-cov) | `uv run pytest` |
| Node | 22, npm with `package-lock.json` | `cd frontend && npm ci` |
| TS lint | ESLint flat config, `typescript-eslint` strict-type-checked, `eslint-plugin-security`, `eslint-plugin-react-hooks`, `no-unsanitized` | `npm run lint` |
| TS types | `tsc --strict --noEmit` | `npm run typecheck` |
| TS tests | Vitest + Testing Library (jsdom) | `npm test` |
| Repo checks | SPDX header check, gitleaks | `make check` runs everything |

`make check` at the repo root runs every gate. Nothing merges red.

## Licence headers

Every source file starts with an SPDX line:

- Python, shell, YAML, Dockerfile, Makefile: `# SPDX-License-Identifier: AGPL-3.0-or-later`
- TypeScript/JavaScript/CSS: `// SPDX-License-Identifier: AGPL-3.0-or-later` (CSS: `/* ... */`)

`scripts/check_spdx.py` enforces this in CI. Generated files (lockfiles,
`frontend/src/lib/api/schema.d.ts`, Alembic `script.py.mako` output) are
listed in its exclusion set.

## Backend layout

The importable package is `app` inside `backend/`; the console script is
`perfin = "app.cli:main"`.

```
app/
  main.py            create_app(settings) -> FastAPI; middleware stack; router auto-discovery
  cli.py             perfin api | worker | mcp | migrate | config check | healthcheck |
                     openapi | ingest --dry-run | bootstrap-url | rotate-key ...
  core/
    settings.py      Settings (pydantic-settings, env prefix PERFIN_), get_settings()
    config_check.py  check_config(settings) -> list[Finding]; refuses insecure prod config
    ids.py           uuid7()
    clock.py         utcnow() (monkeypatchable in tests)
    errors.py        ProblemError + RFC 9457 handlers
    headers.py       security headers + CSP middleware
    csrf.py          CSRF middleware / dependency
    logging.py       JSON logging + PII scrubbing filter; get_logger()
    crypto.py        envelope encryption (XChaCha20-Poly1305 via PyNaCl), master key loading
    audit.py         audit(...) helper writing audit_log
    events.py        emit(session, household_id, type, payload) -> event row + NOTIFY
    money.py         Money schema, Decimal helpers, currency validation
    metrics.py       Prometheus registry and internal metrics server
  db/
    base.py          Base(DeclarativeBase), mixins: UUIDPk, Timestamps, HouseholdScoped
    session.py       engine/sessionmaker factories, get_db dependency
    rls.py           RLS DDL generation, set_context(session, household_id, user_id, ...)
    registry.py      import_all_models(): imports every models module so metadata is complete
    queue.py         Postgres job queue (SKIP LOCKED), enqueue(), dequeue(), reaper
    models.py        platform tables: job, event, audit_log, setting
  auth/              local, mfa, sessions, oidc, tokens, authz, deps (Phase 1)
  domain/<context>/  models.py, schemas.py, repo.py, service.py (one per bounded context)
  ingestion/         intake/, sandbox/, extract/, classify/, parsers/, match/, pipeline.py
  integrations/      prices, fx, notifications, antivirus
  mcp/               MCP server role
  workers/           registry.py (@job_handler), runner.py, scheduler.py, handlers/
  api/
    routers/<name>.py  each module exports `router: APIRouter` (auto-included under /api/v1)
    ai/              /api/v1/ai/v1 read-only views for the ai:read scope (Phase 6b)
```

### Plug-in points (so parallel work does not collide)

- **Routers**: any module in `app/api/routers/` exporting `router` is included
  by `create_app()` under `/api/v1`. Do not edit `main.py` to add a router.
- **Models**: `app/db/registry.py` imports `app.db.models`, `app.auth.models`
  and every `app.domain.*.models` module it finds with `pkgutil`. Alembic's
  `env.py` and the test fixtures call `import_all_models()`.
- **Job handlers**: decorate with `@job_handler("type_name", payload=Model)` in
  any module under `app/workers/handlers/`; the runner discovers them.
- **Settings**: add fields to `Settings`; insecure combinations go in
  `config_check.py` with a test.
- **Frontend features**: `src/features/<name>/routes.tsx` exports
  `routes(parent)`; `src/app/router.tsx` lists features explicitly (one line
  each, appended at the end of the list).

### Database rules

- Sync SQLAlchemy 2 ORM (typed `Mapped[...]`) on psycopg 3
  (`postgresql+psycopg://`). FastAPI path functions that touch the DB are
  plain `def` (run in the threadpool).
- Primary keys are UUIDv7 (`app.core.ids.uuid7`). Timestamps are
  `timestamptz` in UTC. Money is `Numeric(20, 6)` plus a `currency` `CHAR(3)`
  column. Never floats.
- Every household-scoped table uses the `HouseholdScoped` mixin. That mixin
  registers the table for RLS: `ENABLE` **and** `FORCE ROW LEVEL SECURITY`
  with a policy
  `household_id = nullif(current_setting('perfin.household_id', true), '')::uuid`.
  Tables that need extra visibility (membership lookup by user, instance-admin
  audit reads) declare an extra policy predicate in `rls.py`.
- Per-request context is set with
  `set_config('perfin.household_id', :id, true)` (transaction-local) by
  `app.db.rls.set_context`. Code never builds SQL strings; bound parameters
  only.
- Roles: the application connects as a non-owner role (`perfin_app`) with
  `SELECT/INSERT/UPDATE/DELETE` on ordinary tables and only `SELECT/INSERT`
  on `audit_log`. Migrations run as the owner role (`perfin_owner`,
  `PERFIN_MIGRATION_DATABASE_URL`). Neither is a superuser.
- **Migrations**: one Alembic revision per roadmap phase (more if a phase
  needs data migrations), written at phase integration with autogenerate plus
  explicit RLS and grant statements generated from `rls.py`. Feature work in
  parallel does not add revisions. `tests/integration/test_migrations.py`
  upgrades a fresh database to head, asserts `alembic check` reports no
  drift, asserts every `HouseholdScoped` table has RLS enabled and forced,
  and downgrades to base.

### Tests

- `tests/unit` (no DB), `tests/integration` (real Postgres), `tests/security`
  (authz matrix, CSRF, OIDC negatives, upload fuzz corpus, AI column
  classification, scrubber corpus). Mark DB tests with
  `@pytest.mark.integration`.
- `tests/conftest.py` reads `PERFIN_TEST_DATABASE_URL` (an admin URL,
  default `postgresql+psycopg://postgres:postgres@localhost:5432/postgres`),
  creates a uniquely named database per pytest session (so parallel runs do
  not collide), creates the schema with `Base.metadata.create_all` plus the
  RLS DDL and grants, and connects the application as a non-owner role so
  RLS is really exercised. It drops the database at the end.
- Fixtures: `settings`, `admin_engine`, `app_engine`, `db` (a Session in a
  rolled-back transaction where possible), `client` (FastAPI `TestClient`),
  plus factories added by later phases (`make_user`, `make_household`,
  `login_as(role)`).
- Test data is synthetic, produced by code in `tests/fixtures/` or
  `scripts/synth/`. No real statements, names or numbers.
- Never skip, xfail or weaken a test to get green.

## API conventions

As in [API.md](API.md): `/api/v1`, problem-details errors raised with
`ProblemError(status, slug, title, detail)` whose `type` is
`urn:perfin:problem:<slug>`, cursor pagination (`limit` max 500), money as
`{"amount": "12.34", "currency": "USD"}`, `Idempotency-Key` on creating
`POST`s, `Cache-Control: no-store` everywhere except static assets.

Input and output schemas are separate Pydantic models
(`XCreate`/`XUpdate`/`XOut`, `extra="forbid"` on inputs). Routers call
services; services call repos; nothing in `app/api` or `app/domain` imports
`app/ingestion/extract` (enforced by a test).

## Frontend layout

```
src/
  main.tsx           bootstraps QueryClient, RouterProvider, theme
  app/               router.tsx, layout, providers, theme, error boundary
  lib/
    api/             openapi-fetch client + generated schema.d.ts (npm run gen:api)
    csrf.ts          reads the CSRF token from /auth/session and adds X-CSRF-Token
    sse.ts           EventSource -> queryClient.invalidateQueries mapping
    money.ts, dates.ts
  components/        design system (Button, Input, Table, Dialog, Card, ...)
  features/<name>/   routes.tsx, queries.ts, components, tests
```

No `dangerouslySetInnerHTML`, no inline scripts or styles that need
`unsafe-inline`, no third-party CDNs; everything is bundled.

## Security controls register

`docs/security/CONTROLS.md` maps every row of `docs/SECURITY.md` (A1-I4) and
`docs/AI_ACCESS.md` (M1-M8) to the code that implements it and the tests that
prove it. Anyone adding or changing a control updates the row in the same
change. A row may be `planned (Phase N)` until its phase lands; at 1.0 none
may be planned.

## Commits

Conventional commits (`feat(auth): ...`, `fix(queue): ...`, `docs: ...`).
One roadmap item per commit at minimum. `CHANGELOG.md` follows Keep a
Changelog.

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
| Types | mypy `--strict` (config in `pyproject.toml`, covers `app`, `tests` and `migrations`) | `uv run mypy` |
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
  cli.py             perfin api | worker | mcp | migrate | config check | audit verify |
                     healthcheck | openapi | ingest --dry-run | bootstrap-url |
                     breakglass status|enable|disable | rotate-key
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
    http.py          SafeHttpClient: SSRF-safe outbound HTTP (host allowlist, own DNS +
                     address vetting + pinned connect, no cross-origin redirects, caps)
    audit.py         audit(...) helper writing audit_log
    events.py        emit(session, household_id, type, payload) -> event row + NOTIFY
    money.py         Money schema, Decimal helpers, currency validation
    metrics.py       Prometheus registry and internal metrics server
    request_context.py  request id, client IP/scheme from trusted proxies only
    limits.py        request body size limits
    spa.py           serves the built SPA (PERFIN_STATIC_DIR) behind the CSP
  db/
    base.py          Base(DeclarativeBase), mixins: UUIDPk, Timestamps, HouseholdScoped
    session.py       engine/sessionmaker factories, get_db dependency
    rls.py           RLS DDL generation, set_context(session, household_id, user_id, ...)
    registry.py      import_all_models(): imports every models module so metadata is complete
    queue.py         Postgres job queue (SKIP LOCKED), enqueue(), dequeue(), reaper
    models.py        platform tables: job, event, audit_log, setting
  auth/              Phase 1. models (identity tables + RLS self-read policies),
                     passwords (Argon2id), sessions (server-side, cookie, rotation),
                     principal (session/PAT -> Principal + RLS context), deps (FastAPI
                     resolution), authz (require_* guards, account grants), ratelimit
                     (sliding windows, lockout); mfa/ (totp, webauthn, recovery,
                     stepup, policy, challenges); local (login, lockout notice,
                     new-password policy), breached (+ data/ list), reset,
                     bootstrap (first-run setup); oidc/ (client, discovery, tokens,
                     provisioning, flow, logout, refresh, models); tokens (personal
                     access tokens: create/list/revoke, bearer rate limits); scopes
                     (token scope catalog and role ceilings: a new scope goes there
                     before `require_scope("...")` can name it)
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
  by `create_app()` under `/api/v1`; a module may also export `root_router`
  for the few unversioned paths (`/healthz`, `/readyz`). Do not edit
  `main.py` to add a router.
- **Models**: `app/db/registry.py` imports `app.db.models`, `app.auth.models`,
  `app.auth.mfa.models`, `app.auth.oidc.models` and every `app.domain.*.models` module it finds with `pkgutil`. Alembic's
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
- References between household-scoped tables include `household_id`
  (ADR-0008, `app.db.types`): the target declares `household_key(table)`,
  a mandatory parent uses `household_fk(...)`, an optional `SET NULL` lookup
  keeps its plain `ForeignKey(..., ondelete="SET NULL")` plus
  `household_ref_fk(...)`. A models module imports the models modules whose
  tables it references (flush ordering resolves every foreign key). Money
  columns use `app.db.types.AMOUNT` and `CURRENCY` with `currency_check()`;
  enums use `pg_enum`.
- Account-derived data (transactions, balances, budgets spent, goals,
  reports) is filtered with `app.domain.accounts.visibility`
  (`visible_account_filter`, `get_visible_account`); a hidden account is
  `404 account-not-found`.
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
  parallel does not add revisions. Files are `migrations/versions/NNNN_<slug>.py`
  with revision id `NNNN` (`uv run alembic revision --autogenerate --rev-id
  0002 -m "phase1 identity"`). A revision names the tables it creates
  explicitly (`all_rls_ddl(tables)`, `table_grants_ddl(tables, role)`) so it
  never depends on tables a later phase adds to `Base.metadata`; the role
  that gets the grants is `PERFIN_DB_APP_ROLE`. `perfin migrate` runs
  `alembic upgrade head` as the owner role (`PERFIN_MIGRATION_DATABASE_URL`). `tests/integration/test_migrations.py`
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
  plus the identity factories from `tests/fixtures/factories.py`:
  - `make_household(name=None)` -> committed `Household`.
  - `make_user(role="member", household=None, with_totp=False,
    is_instance_admin=False, ...)` -> `UserRecord` (`.user`, `.household`,
    `.role`, `.password`, `.totp_secret`, `.totp_now()`). A new household is
    created unless `household` is passed or `role=None`. Passwords use cheap
    test-only Argon2id parameters.
  - `login_as(client, user, mfa_verified=None, stage=..., household_id=...)`
    creates a real session through `app.auth.sessions.create_session`, puts
    its cookie on the client and returns `LoggedIn` (`session_id`,
    `csrf_token`, `headers()` with `X-CSRF-Token`, `Origin` and
    `Sec-Fetch-Site` for unsafe requests). `mfa_verified` defaults to whether
    the user has TOTP.
  - `bearer_token(user, scopes=(), expires_at=None, revoked=False)` -> a raw
    personal access token for `Authorization: Bearer`.
  - `authed_client(role="member", ...)` -> `AuthedClient`: the `client`
    logged in as a new user (with TOTP, so `PERFIN_REQUIRE_MFA` does not
    restrict it) whose `post`/`patch`/`put`/`delete` add the CSRF headers.
  - `factories` is the underlying `Factories` object (`.member()`,
    `.grant()`, `.session()`, `.token()`, `.transaction()`). The flush-only
    functions in `tests/fixtures/factories.py` (`make_user(db, ...)`, ...)
    work inside the rolled-back `db` fixture; the fixtures above commit so a
    `TestClient` app (another connection) sees the rows.
- Ledger data comes from `tests/fixtures/ledger.py`: flush-only
  `make_account(db, household_id, type=..., currency=...)`,
  `make_transaction(db, account, amount=..., posted_date=...)`,
  `make_category`, `make_payee`, `make_tag`, `make_split`,
  `make_snapshot`, `make_fx_rate`, `make_rule`, `make_budget`, `make_goal`,
  `seed_categories(db, household_id)` (households from the identity
  factories have no categories until this runs), and the committing `ledger`
  fixture (`ledger.account(household_id, ...)`, `ledger.grant(household_id,
  user_id, account, level)`, `ledger.run(make_x, ...)`).
- Tests that drive the login, setup or reset endpoints use
  `tests/fixtures/auth_clients.py`: `browser(app, ip=None)` is a
  `TestClient` on `https://testserver` (so `Secure` cookies round-trip) with
  its own random client address (per-IP rate limits are shared through the
  session database), `build_app(settings, limits=AuthLimits(...))` sets
  test limits on `app.state.auth_limits`, and `pre_auth_headers(client)`
  fetches the anonymous CSRF token. Tests that need a database with no user
  at all (first-run setup) use `tests/fixtures/fresh_db.py`.
- Tests that need several users logged in at once (an owner demoting a
  member and checking the member's session) use `tests/fixtures/households.py`:
  `Actors.open(settings, factories)` owns one app; `actors.new(role=...,
  mfa_verified=None, ...)` / `actors.login(record)` give an `AuthedClient` on
  its own https `TestClient` (TOTP user, fresh MFA, so step-up passes; pass
  `mfa_verified=False` for a stale one); `household_audit`,
  `instance_audit`, `household_events` and `problem_type` read back what an
  endpoint recorded.
- OIDC tests use `tests/fixtures/fake_idp.py`: `FakeIdP` (synthetic RSA and
  EC keys, discovery, JWKS and a strict token endpoint: client auth,
  single-use codes, redirect URI, PKCE) answering through `respx`;
  `mocked_idp()`, `resolver()` (names the IdP without DNS, so the
  SSRF-safe client pins to the fake address), `oidc_settings(settings,
  ...)`, `clear_proxy_env(monkeypatch)` (the client honours `HTTPS_PROXY`),
  `login_via_idp(client, idp, claims)` and `oidc_error(response)`. Knobs for
  negative tests: `claim_overrides`, `header_overrides`, `remove_claims`,
  `sign_with`, `published`, `token_mutator`, `discovery_overrides`.
- Endpoint guards come from `app.auth.authz` (`require_authenticated`,
  `require_role(HouseholdRole.X)`, `require_instance_admin`,
  `require_scope("x:y")`, `require_step_up`); never read the session cookie
  or `Authorization` header in a router. With `from __future__ import
  annotations`, build `Annotated[Principal, Depends(require_role(...))]`
  aliases at module level (string annotations are resolved from module
  globals).
- Test data is synthetic, produced by code in `tests/fixtures/` or
  `scripts/synth/`. No real statements, names or numbers.
- `tests/security/test_auth_surface.py` enumerates every route of every
  router module (and checks the list against the OpenAPI document): each
  state-changing `/api/v1` route must refuse a request without a CSRF token
  (only the OIDC back-channel logout is exempt), and every error of the
  auth, setup, household, user-settings and admin routes must be problem
  details. A new router is covered automatically; extend `PHASE1_PREFIXES`
  (or add a prefix list for the new phase) to check its errors too.
- `tests/security/test_input_models.py` (F4) walks the OpenAPI document and
  fails for any request body that is not a named Pydantic model with
  `extra="forbid"` (recursively through nested models and unions). Give
  every new input model `model_config = ConfigDict(extra="forbid")`; a
  free-form JSON field or a non-JSON body needs an entry with its reason in
  `FREE_FORM_FIELDS` / `NON_MODEL_BODIES` there.
- `tests/security/test_authz_matrix.py` is the authorisation matrix. It
  walks `app.routes` (so every new route is seen) and **fails for any route
  without an entry in `tests/security/authz_policy.py`** (`POLICIES`), and
  for entries whose route is gone. Each route is then called as every
  principal in `PRINCIPALS` (anonymous, viewer, member, admin, owner,
  instance admin, owner of another household, MFA-restricted, MFA-pending,
  stale step-up, token without scopes / with the route's scope / expired,
  valid cookie plus a junk bearer token) and the answer must match the
  policy; a CSRF matrix refuses every state-changing route without the
  token, with another session's or the pre-auth token, and cross-site. To
  add an endpoint, add one line to `POLICIES` keyed `"<METHOD> <full path
  template>"` (`"MOUNT <path>"` for a mount) using the helpers there:
  - `public()` for no authentication (plus `overrides=` for documented
    exceptions such as `401` on `GET /auth/session`);
  - `session(...)` for browser sessions only, `scoped("x:read", ...)` when
    a token with that scope may call it, `instance_admin(...)`;
  - `role=HouseholdRole.X` for a minimum role in the active household,
    `step_up=True`, `enrol=True` (MFA-restricted sessions allowed),
    `household_object=True` when the body names a household;
  - `params={"name": kind}` for every path parameter, with a kind from
    `PARAM_KINDS` (`household`, `member`, `user`, `own_session`,
    `own_token`, `own_webauthn`); add a kind (and its generator in
    `build_object`, and its `not_found` problem) for a new object type, so
    cross-household access is checked to be `404`;
  - `body={...}` only when a check runs inside the handler after body
    validation (an empty body is sent otherwise).
  Never loosen an entry to make the matrix pass; fix the guard.
- Per-request conditions on a browser session go through
  `app.auth.deps.session_acceptor` (passed as `load_session(accept=...)`);
  today it keeps break-glass sessions on `PERFIN_BREAKGLASS_CIDRS`.
- `tests/integration/test_auth_e2e.py` drives the whole login chain through
  the real app (local and OIDC); keep it green when changing any auth step.
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
`app/ingestion/extract` (enforced by `tests/unit/test_import_boundaries.py`).

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
    auth/            sign-in pages, guard.ts (root beforeLoad: session required
                     except on the public sign-in/setup/reset pages),
                     returnTo.ts (relative return_to only), StepUp.tsx
                     (useStepUp().run(action): step-up dialog, then retry),
                     session.ts (typed session; call sessionChanged /
                     sessionRotated with each rotated csrf_token), testing.tsx
                     (mockApi fetch router, session bodies, renderApp; tests only)
    setup/, settings/ first-run setup; profile, security, household members
```

Feature tests stub `fetch` with `mockApi` from `features/auth/testing.tsx`
(msw is not used) and assert the `X-CSRF-Token` header on state-changing
calls. Pages under `/settings` are wrapped in `StepUpProvider`; a sensitive
action elsewhere needs its own provider.

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

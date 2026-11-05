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
| Tests | pytest (+ pytest-cov, pytest-xdist) | `uv run pytest -n auto` |
| Node | 22, npm with `package-lock.json` | `cd frontend && npm ci` |
| TS lint | ESLint flat config, `typescript-eslint` strict-type-checked, `eslint-plugin-security`, `eslint-plugin-react-hooks`, `no-unsanitized` | `npm run lint` |
| TS types | `tsc --strict --noEmit` | `npm run typecheck` |
| TS tests | Vitest + Testing Library (jsdom) | `npm test` |
| Helm chart | helm 3.19 + kubeconform, built by `scripts/install-chart-tools.sh <dir>` (Go module hashes pinned); `backend/tests/unit/test_helm_chart.py` needs `helm` on `PATH` (or `PERFIN_HELM`) | `make chart-check` |
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
                     breakglass status|enable|disable | notify backup-failed |
                     rotate-key | backup-encrypt | backup-decrypt | backup-info |
                     restore-check (the last five live in ops/commands.py)
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
    idempotency.py   Idempotency-Key for creating POSTs: IdempotencyKeyHeader + idempotent(...)
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
                     (taxonomy/ has no models: categories, payees and tags are
                     tables of transactions/models.py; it holds their API logic;
                     rules/ likewise for `rule`, plus engine.py (pure evaluation);
                     jobs/ reads `job` rows for GET /jobs/{id};
                     imports/ holds `import_mapping` and the structured
                     importer the worker's pipeline calls;
                     documents/manage.py holds the documents API rules on top
                     of documents/service.py (storage); review/ lists and
                     resolves `review_item` rows and owns `import_hash_alias`;
                     events/ is the SSE stream behind GET /events;
                     dropfolder/ holds drop-folder routes and the
                     processed-file ledger (the watcher itself is
                     ingestion/intake/dropfolder.py);
                     portability/ is the full export/import: entities.py lists
                     the exported tables (a new household-scoped table goes
                     there or in NOT_EXPORTED), codec.py, zipsafe.py,
                     export.py, import_.py, service.py)
  ingestion/         intake/, sandbox/, extract/, classify/, parsers/, match/, pipeline.py
  integrations/      prices, fx, notifications, antivirus
  mcp/               MCP server role
  ops/               operator tooling: keyrotation.py (`perfin rotate-key`; a new encrypted
                     column goes in ENVELOPE_COLUMNS), backup.py (backup archive encryption,
                     restore document check), commands.py (CLI registration);
                     scripts/backup.sh and scripts/restore.sh drive them (docs/BACKUP_RESTORE.md)
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
- **Tax form sets**: a jurisdiction is a module in `app/domain/tax/forms/`
  building a `FormSet` (code, filing statuses, `FormDef`s with box ids,
  labels and kinds; identity boxes are kind `identity` and never stored),
  listed in that package's `_BUILTIN` or added with `register(...)`.
  `FormDef.issuer_box` names the box printing the issuer's name. The tax
  form parser (`app/ingestion/parsers/tax_forms.py`) reads every registered
  set by its labels (`Form <type>` heading, box id plus the first words of
  the label) and its redaction keeps the words of the registered titles and
  labels, so a new set needs no parser change unless its layout has tables
  (see the 1095 handling there).
- **Price and FX providers**: an external feed is a module in
  `app/integrations/prices/` (protocol `PriceProvider`: symbols in, quotes
  out) or `app/integrations/fx/` (`FxProvider`), a new `Literal` value of
  `Settings.price_provider` / `fx_provider` (default `none`) and a branch in
  that package's `registry.py`. It must use `app.core.http.SafeHttpClient`
  with an allowlist of its own host, send ticker symbols or currency codes
  only, get a `config_check` warning naming its destination and a section
  in docs/security/integrations.md. The `refresh_prices` job
  (`app/workers/handlers/market_data.py`) is its only caller.
- **Notifications**: a new notifiable event is one entry in
  `app.domain.notifications.routing.ROUTES` (event type -> kind and a
  generic sentence without identity or financial detail; a new kind is also
  a value of `Kind`) and a call `notify(db, household_id, event_type,
  source_id=...)` next to the `emit` that records it, in the household's
  RLS context. A new channel type is a module in
  `app/integrations/notifications/` (protocol `Channel`, raise `ChannelError`
  with an outcome code), a `ChannelType` value, a config model in
  `app.domain.notifications.schemas` (`CONFIG_MODELS`, `SECRET_FIELDS`,
  `DESTINATION_FIELDS`) and a branch in `registry.py`; HTTPS goes through
  `app.integrations.notifications.web.deliver`. Tests use the local fake
  services of `tests/fixtures/fake_notify.py` (`running_https`,
  `running_smtp`, `local_options`) and `tests/fixtures/notifications.py`
  (`make_channel`). docs/security/notifications.md lists what leaves.
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
  `404 account-not-found`. Sums over the ledger (reports, budget actuals)
  start from `app.domain.reports.ledger.ledger_lines` (visibility, splits,
  transfers and pending review rows already handled), aggregate with its
  `aggregate` in SQL, and convert with its `Accumulator` (rate per posting
  day, missing rates reported as `unconverted`).
- Investment lots, `investment_txn.lot_links` and `holding` rows are
  derived: only `app.domain.investments.service.replay_position(db,
  account, security_id)` writes them (the lot engine in
  `app/domain/investments/lots.py`). Code that adds, changes or deletes
  investment transactions calls it for every (account, security) pair it
  touched, in the same database transaction; a `ProblemError` from it means
  the change does not fit the lots and must be rolled back.
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
  and downgrades to base and to each previous revision. Revisions so far:
  `0001_phase0_foundations`, `0002_phase1_identity`, `0003_phase2_ledger`
  (27 household-scoped ledger, document, import and platform tables; the
  `account_grant -> account` key, deleting orphan Phase 1 grants first; a
  data step that needs to see every household's rows of a forced-RLS table
  lifts `FORCE ROW LEVEL SECURITY` for that statement only, as 0003 does),
  `0004_phase3_4_ingestion` (`dropfolder_route`, `dropfolder_entry`,
  `mail_intake`, `mail_intake_message`; the search vector of documents
  extracted before it backfilled in SQL, with `FORCE` lifted on `document`
  for that statement; the `tsvector` column, its GIN index and
  `parser_fingerprint` already came with 0003),
  `0005_phase5_6_wealth_tax` (17 household-scoped tables: `security`,
  `price`, `investment_account`, `investment_txn`, `lot`, `holding`, `loan`,
  `loan_payment`, `loan_rate_change`, `manual_asset`, `asset_valuation`,
  `net_worth_snapshot`, `tax_year`, `tax_checklist_item`, `tax_category_map`,
  `tax_form_data`, `tax_export`; 11 new enum types dropped by its
  downgrade), `0006_phase6b_ai` (`ai_client` and `ai_call`, both
  household-scoped, the `ai_call -> ai_client` household-consistent key,
  enum types `ai_mode` and `ai_transport`; the AI switches are `setting`
  rows and need no DDL), `0007_phase7` (`notification_channel`,
  household-scoped, enum type `notification_channel_type`; the dashboard
  layout lives in the existing user settings JSON and key rotation rewraps
  existing columns, so neither needs DDL). `alembic check` does not compare CHECK constraint text, so a
  `:word` inside a CHECK string is parsed as a bind parameter and silently
  rendered as `NULL` (both by `create_all` and by autogenerate): write regex
  character classes so no `:` is followed by a word character (0005 found
  this in `ck_security_symbol_format`).

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
- The suite runs in parallel with pytest-xdist (`uv run pytest -n auto`, used
  by `make check` and CI). Each xdist worker is its own pytest session, so it
  gets its own database and roles; tests must not share state across workers
  in any other way (fixed ports, fixed paths outside `tmp_path`, global
  files). Parametrize ids must be deterministic (xdist checks that every
  worker collects the same tests): wrap random values in
  `pytest.param(..., id="...")`. For a single file while iterating,
  `uv run pytest -n auto tests/path/test_x.py`.
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
  `make_snapshot`, `make_fx_rate`, `make_rule` (by default a valid rule
  document: `all` of nothing, `flag_for_review`), `make_budget`, `make_goal`,
  `seed_categories(db, household_id)` (households from the identity
  factories have no categories until this runs), and the committing `ledger`
  fixture (`ledger.account(household_id, ...)`, `ledger.grant(household_id,
  user_id, account, level)`, `ledger.run(make_x, ...)`).
  `tests/fixtures/report_ledger.py` builds a fixed multi-account,
  multi-currency ledger (splits, a transfer, a pending row, a hidden
  account, a currency without a rate) whose report and budget totals are
  written down exactly in `tests/integration/test_reports_api.py` and
  `test_budgets_api.py`; tests freeze "today" by patching the `today`
  function of `app.domain.reports.service`, `app.domain.budgets.service` or
  `app.domain.goals.service`.
- Investments, loans, manual assets and net worth (Phase 5) come from
  `tests/fixtures/wealth.py` (flush-only `make_brokerage`, `make_security`,
  `make_price`, `make_investment_txn`, `make_lot`, `make_holding`,
  `make_loan` (creates its mortgage account unless one is given),
  `make_loan_payment`, `make_loan_rate_change`, `make_manual_asset`,
  `make_asset_valuation`, `make_net_worth_snapshot`); the tax vault
  (Phase 6) from `tests/fixtures/tax.py` (`make_tax_year`,
  `make_checklist_item`, `make_tax_category_map`, `make_form_data`, which
  goes through the real `record_form_data`, and `synthetic_w2_boxes`, a
  W-2 with invented identity boxes for proving they are discarded;
  `free_tax_year`, and `make_tax_export`, a ready accountant export with its
  bytes in a blob store). Commit them with `ledger.run(make_x, ...)`.
  `tests/fixtures/synth_tax_forms.py` draws every US form as a PDF
  (reportlab; `synth_form(form_type, seed=, tax_year=)`, `form_pdf`,
  `forms_pdf` for a consolidated statement, `expected_boxes` is what the
  parser must return) with obviously fake identity values listed in
  `IDENTITY_TOKENS` (`000-00-0000`, `00-0000000`, an invented name and
  address...). Parser tests: `tests/unit/test_tax_form_parser.py`; through
  the worker, with a scan of every database column and API response for
  those values, the accountant export and the reminders:
  `tests/integration/test_tax_form_pipeline.py` (it patches
  `app.workers.handlers.tax.get_settings`); API: `tests/integration/test_tax_api.py`.
- Documents and ingestion: `tests/fixtures/statements.py` generates
  synthetic CSV/TSV, OFX (SGML and XML), QFX and QIF files (plus XXE and
  entity-bomb prologs); tests build a `LocalFSBlobStore` under `tmp_path`.
  Extractors may be called in-process in tests; production code reaches
  them only through `app.ingestion.sandbox.runner.run_extraction` (the
  child is the only module that imports `app.ingestion.extract`, enforced
  by `tests/unit/test_sandbox_boundaries.py`). PDFs (reportlab; PyMuPDF
  only to add hostile active content), scans, receipts, EXIF JPEGs, TIFF,
  HEIC, pixel and zip bombs, nested archives and receipt mails with HTML,
  `cid:` images and attachments come from `tests/fixtures/synth_documents.py`
  (OCR tests need Tesseract's English data in a standard tessdata
  directory, e.g. Debian/Ubuntu `tesseract-ocr-eng`). Sandbox hardening is tested
  with hostile extractors in `tests/fixtures/sandbox_evil.py`, run through
  the real child code path with `SandboxConfig(module="tests.fixtures.sandbox_evil")`.
  The child's seccomp filter is a default-kill allowlist
  (`ALLOWED_SYSCALLS` in `app/ingestion/sandbox/child.py`, ADR-0006
  addendum). A new extractor or an update of PyMuPDF, Pillow, pillow-heif,
  tesserocr or the base image can need a syscall that is not listed: the
  document then fails with `extraction failed: syscall_denied`. Find the
  call with `strace -f` on the sandbox tests, add it only if it cannot
  reach the network, other processes or the filesystem for writing (argument
  checks where it can), and run `tests/security/test_sandbox*.py` and
  `tests/security/fuzz` in the built image as well as on the host.
  Receipts for parser, accuracy and end-to-end tests come from
  `tests/fixtures/synth_receipts.py` (Pillow thermal prints with noise and
  skew, reportlab e-receipt PDFs, HTML receipt mails); `TUNING_SEEDS` may be
  used while tuning OCR or the parser, the held-out seeds only for measuring.
- Statement parsers: `tests/fixtures/synth_pdf_statements.py` draws PDF
  statements (reportlab) in the layout of each fictional institution of the
  shipped fingerprints from a `SynthStatement` (multi-page, pending items,
  fees/interest, debit/credit columns, a card, `tamper="extra_row"` /
  `"closing"` variants that do not reconcile, an unknown bank with
  ambiguous dates) and a payslip; `expected(stmt)` gives the rows a parser
  must return. Unit tests extract them in process
  (`app.ingestion.extract.pdf.extract_pdf`), classify and parse
  (`tests/unit/test_statement_parsers.py`); end-to-end tests go through the
  worker like the structured imports
  (`tests/integration/test_pdf_statement_pipeline.py`). Adding a layout:
  `docs/CONTRIBUTING-parsers.md`.
- Brokerage statements: `tests/fixtures/synth_brokerage.py` builds
  consecutive monthly `SynthBrokerage` statements of the fictional
  Placeholder Brokerage (`brokerage_series(seed, months=...)`: positions,
  buys, a sale, dividends, a reinvestment, interest, a fee, cash in and
  out) and renders them as a PDF (`placeholder_pdf`, also under another
  broker's name for the generic parser) or OFX `INVSTMTRS` SGML/XML
  (`ofx_investment`); `tamper="extra_row" | "market_value" | "total"`
  breaks one reconciliation check; `expected_activity` /
  `expected_positions` are what a parser must return. Unit tests:
  `tests/unit/test_brokerage_parser.py`; end to end through the worker
  (holdings, lots, prices, snapshot, dedupe, holds):
  `tests/integration/test_brokerage_pipeline.py`.
- Loan statements: `tests/fixtures/synth_loans.py` builds monthly
  `SynthLoanStatement`s of the fictional Placeholder Home Lending from a
  schedule of the real amortisation engine (`loan_series(months, extra=,
  late_fee_month=)`) and draws them as PDFs (`loan_statement_pdf`, layouts
  `classic` / `compact`; `tamper="parts" | "balance"` breaks one
  reconciliation check). Unit tests: `tests/unit/test_loan_statement_parser.py`;
  end to end through the worker: `tests/integration/test_loan_statement_pipeline.py`.
  Amortisation properties (hypothesis): `tests/unit/test_amortization.py`.
- Drop folder: `tests/integration/test_dropfolder.py` builds the inbox and
  `processed/` under `tmp_path` and calls
  `app.ingestion.intake.dropfolder.watch(...)` in a committed transaction
  with `watcher_factory=PollingWatcher` and a `now` shifted an hour ahead
  (so files count as settled without sleeping); `finish_pending` runs the
  after-actions as the `dropfolder.finish` job would. The handlers read
  settings through `app.workers.handlers.dropfolder.get_settings` (patch
  it); with several households in the test database a run needs
  `dropfolder_household_id`.
- Documents API, review queue and SSE: `tests/fixtures/documents.py` has
  flush-only `make_document(db, store, keyring, household_id, ...)` (the
  synthetic `data` really encrypted into the given `LocalFSBlobStore`,
  `processed` with a permanent blob by default, no job queued) and
  `make_review_item(db, document, kind, payload)`. API tests copy the
  `settings` with `documents_dir`/`quarantine_dir` under `tmp_path` and open
  an `Actors` app on them. `GET /events` streams until
  `sse_max_stream_seconds` (a `TestClient` reads a response to its end), so
  SSE tests set it to about a second and emit committed events from a
  thread meanwhile; the authz matrix uses 0.2 s and a temporary blob store
  (parameter kinds `stored_document`, `review_item`).
- Structured imports: `tests/fixtures/synth_statements.py` (standard library
  only; also `scripts/synth/statements.py --out DIR` writes the samples as files;
  the dev seed (`scripts/seed_dev.py`, "Dev stack") uses `synth_ledger`)
  generates invented institutions, merchants and account numbers: a seeded
  history (`synth_ledger`), statement windows with opening/closing balances
  (`statement`, `sample_statement`; overlapping windows share rows), CSV/TSV
  in the bank-like `LAYOUTS` (each with the `MappingSpec` JSON that reads
  it), OFX SGML/XML, QFX and QIF. `tests/fixtures/imports.py` has
  flush-only `make_extracted_document` (a document whose `extraction` is a
  given sandbox result, no file behind it), `make_import_mapping`, and
  `run_jobs(factories, household_id, job_type, done=...)`, which runs the
  household's queued jobs of a type through their registered handlers (each
  in its own committed transaction). End-to-end import tests patch
  `app.workers.handlers.ingest.get_settings` to tmp blob directories and use
  the real `default_deps()` (sandbox and structured importer).
- The Phase 2 end-to-end test (`tests/integration/test_phase2_end_to_end.py`)
  drives one household through the real app over HTTP and the real
  `app.workers.runner.Worker` (`run_once` until idle): OFX and CSV uploads,
  rules, `/transactions`, `/review` resolutions and a live `/events`
  stream. The test database is shared, so it holds back other households'
  queued jobs (`run_after` pushed out under `queue_scope`, restored after);
  copy that pattern for any test that runs the real worker loop.
- Export and import: `tests/fixtures/portability.py` has `build_rich_ledger`
  (the report ledger plus one of everything an export carries, including
  formula-looking text cells and a document), flush-only
  `make_ready_export(db, store, keyring, household_id, requested_by)` /
  `make_import_record(...)`, `rezip(data, mutate, manifest=...)` to derive
  hostile archives from a real one, `run_portability_jobs(...)` (the real
  handlers with test dependencies) and `CHEAP_KDF` (patch
  `app.domain.portability.service.EXPORT_KDF` with it so passphrase tests
  are fast). The authz matrix kinds `export_archive` / `import_archive`
  build an archive requested by the caller.
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
- AI access (Phase 6b): every column of every table must be classified in
  `app/domain/ai/classification.py` (exposed, free_text or denied with a
  reason); `tests/security/ai/test_column_classification.py` fails for a
  column that is not, and for an AI serialiser field (`app/api/ai/schemas.py`,
  declared with `src("<table>.<column>", ...)`) that reads a denied one. A new
  AI view returns an `AiOut` model, scrubs every free-text value through its
  `AiContext` (`app/api/ai/views.py`) and gets an `ai()` entry in the authz
  policy. `tests/security/ai/test_scrubber_corpus.py` runs the leak corpus in
  `tests/security/ai/corpus/*.yaml` (add a case for every new identifier
  shape; zero leaks required). AI clients for tests come from
  `tests/fixtures/ai.py` (`ai_client(factories, owner, **options)` through the
  real `create_client`, `ai_headers(raw)`); the authz matrix principal
  `ai_token` is such a client of the matrix owner and must be refused
  (`403 ai-token-confined`) on every route outside `/api/v1/ai/v1`.
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
    `own_token`, `own_webauthn`, and household objects such as
    `transaction`); add a kind (and its generator in `build_object`, and its
    `not_found` problem) for a new object type, so cross-household access is
    checked to be `404`. A household object (listed in
    `HOUSEHOLD_OBJECT_KINDS`) is created on an account on which the matrix
    member holds `edit` and the viewer `view`, so the role columns decide;
    grant-specific outcomes are tested in the domain's own tests. A
    nested object receives the objects built for the earlier parameters of
    the same path (`build_object(..., built)`; a `goal_contribution`
    belongs to the path's `goal`);
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
(`XCreate`/`XUpdate`/`XOut`, `extra="forbid"` on inputs). A creating `POST`
takes `idempotency_key: IdempotencyKeyHeader = None` and returns
`idempotent(db, principal, idempotency_key, scope="POST /things",
payload=payload_of(body, **path_params), run=lambda: service.create(...))`
(`app.core.idempotency`; the replay is a `JSONResponse`, so annotate the
handler `-> Any` and keep `response_model`). Ledger routers build guards
with `app.api.routers.accounts.scoped_role(scope, minimum_role)`; a route
with a request body and an object in its path checks the object in a
dependency (`require_account_writer`, `require_category_writer`, ...), so a
foreign or hidden object is its `404` before the body is validated (the
authz matrix sends empty bodies). Code that creates transactions (manual
create, importers) calls `app.domain.rules.service.apply_rules_to(session,
household_id, rows, protect=...)` after flushing them, in the same
transaction, passing in `protect` the fields a person entered by hand.
Routers call services; services call repos; nothing in `app/api` or `app/domain` imports
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

## Dev stack

`make dev` (`scripts/dev.sh up`) brings up a complete local stack on a
synthetic household. Requirements: Linux (the worker's extraction sandbox
uses seccomp and Landlock), Docker with the compose plugin, `uv`, Node 22
with npm, and `openssl`. Behind a registry mirror set the image, e.g.
`POSTGRES_IMAGE=mirror.gcr.io/library/postgres:16-alpine make dev`.
On a kernel without Landlock the extraction sandbox refuses to run, so the
seed's document uploads (and any upload) fail; for local development only,
add `PERFIN_EXTRACT_REQUIRE_LANDLOCK=false` to `.dev/env` (it is generated
once and kept) and run `make dev` again. ADR-0009 describes what the child
can still read without Landlock.

| Command | Does |
| --- | --- |
| `make dev` | everything below, in the foreground until Ctrl-C |
| `make dev-down` | stops the database container (data kept) |
| `make dev-reset` | deletes the database volume and `.dev/`; the next `make dev` creates a new database, new secrets and a new dev admin |
| `scripts/dev.sh seed` | runs only the seed (database up and migrated) |

`make dev` does, in order:

1. Generates `.dev/env` once (mode 0600, git-ignored): random Postgres
   passwords, `PERFIN_MASTER_KEY`, `PERFIN_SECRET_KEY`, and the dev settings
   (`PERFIN_DEV_MODE=true`, `PERFIN_SECURE_COOKIES=false`,
   `PERFIN_REQUIRE_MFA=false`, `PERFIN_PUBLIC_URL=http://localhost:5173`,
   documents and quarantine under `.dev/`). There are no default credentials.
2. Starts PostgreSQL 16 from `deploy/docker-compose.dev.yml` (project
   `perfin-dev`, published on `127.0.0.1:55432`, override with
   `PERFIN_DEV_DB_PORT`) with the production `deploy/db-init` scripts, so the
   API connects as the non-owner `perfin_app` role and row-level security
   applies.
3. `uv sync`, `npm ci` (only when `frontend/node_modules` is missing) and
   `perfin migrate` as the owner role.
4. Runs `scripts/seed_dev.py` (below).
5. Starts `uvicorn app.main:create_app --factory --reload` on
   `127.0.0.1:8000`, `perfin worker`, and the Vite dev server on
   `http://localhost:5173` (proxies `/api`). Each service's log lines are
   prefixed `[api]`, `[worker]` and `[web]`. The OpenAPI UI is at
   `http://127.0.0.1:8000/api/v1/docs` (dev mode only).

### The seed

`scripts/seed_dev.py` (run from `backend/`: `uv run python
../scripts/seed_dev.py`) builds the household **through the real code
paths**: first-run setup (`app.auth.bootstrap`) for the admin, the HTTP API
(an in-process app with a short-lived seeding session that is ended
afterwards) for everything else, the real upload endpoint and ingestion jobs
for documents. It refuses (exit 2) unless `PERFIN_DEV_MODE=true`, and refuses
a database that has any user other than its own `dev-admin` or a seed that did
not finish (the last step creates the tag `dev-seed`).

* **Credentials:** the username is `dev-admin`; the password is generated
  (`secrets.token_urlsafe`) and printed **once** to stdout when the seed
  creates the admin. It is stored nowhere (only the Argon2id hash). Lost it:
  `make dev-reset`. A second run prints `already seeded` and changes nothing.
* **Data:** 12 accounts covering every account type (checking, savings, credit
  card, cash, brokerage, retirement, loan, mortgage, line of credit, manual
  asset x2, manual liability); about 6 months of transactions from
  `tests/fixtures/synth_statements.synth_ledger` (rent, payroll, groceries,
  coffee, card purchases, savings transfers paired by the transfer matcher) and
  detected recurring schedules; three rules, a monthly budget, two goals with a
  contribution; a brokerage account (two buys of one security, a sale of a
  chosen lot, a dividend), a retirement account (average cost), three
  securities with price history; a mortgage with 13 payments and a car loan; a
  house and a car with valuations; monthly net worth snapshots; a tax year
  (last calendar year) with an expectations checklist; and an AI client
  example that is created and then **revoked** (its token is discarded, so it
  is a disabled example).
* **Documents:** a bank statement CSV, a brokerage statement PDF, a receipt
  image and four tax form PDFs (`synth_tax_forms`) are uploaded through
  `POST /documents` and processed by the real ingestion jobs run in the seed
  process (sandboxed extraction). The forms fill the tax year; the statement,
  brokerage statement and receipt leave open items in the review queue.
* Everything is generated; dates are relative to the day the seed runs.

`backend/tests/integration/test_seed_dev.py` runs the seed on an empty
database and reads the household back through the API as the dev admin
(every account type, ledger, budget, goals, rules, holdings, loans, assets,
net worth history, tax forms, documents, review queue, the revoked AI client),
and checks that a second run changes nothing and that it refuses outside dev
mode, next to other users and after an unfinished run.

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

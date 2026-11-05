# Changelog

All notable changes to PerFin are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Status

The current release candidate is `v1.0.0-rc.3`; the final `v1.0.0` follows the
external security review. Every phase (0-7) is implemented. Alembic head is
`0009`. On 2026-11-05: SPDX; backend ruff, ruff format, mypy --strict (657
source files), bandit, OpenAPI drift and 7742 pytest tests with `-n auto`,
of which the OCR and Helm tests (67) need Tesseract and `helm` and were run
only in CI; frontend eslint, tsc, 439 vitest tests in 57 files and the
production build. As of rc.2: `pip-audit` finds no known vulnerabilities and
`npm audit --audit-level=high` passes with 10 moderate advisories in dev
tooling (eslint, typescript-eslint, minimatch), none high.
CI is green end to end on GitHub Actions for commit `21d2b38`: `ci` run
41927816446 (including the full backend pytest job) and `security` run
41927816284 (gitleaks over every commit, pip-audit, npm audit, Semgrep, `trivy
fs`, and the shipped image built and passing `trivy image` with an SBOM).

What remains (details in docs/ROADMAP.md):

- An external security review or penetration test before the tag. An
  internal review was done every phase (docs/security/CONTROLS.md).
- The `v1.0.0-rc.1` release run failed in its download step (fixed in rc.2,
  #8). `v1.0.0-rc.3` also publishes the new `perfin-ml` image.
- Not exercised: the optional local-model containers (`ml`/`ml-cuda`
  profiles) against a real Ollama runtime and GPU; the adapter is tested
  against a fake runtime only.
- Not exercised: the Helm chart on a live cluster, a live Authentik, real
  notification providers, the egress profile with real outbound
  integrations, WebAuthn with a hardware key, Claude Desktop and on-prem MCP
  hosts, and contributed classifier samples.

## [Unreleased]

Nothing yet.

## [1.0.0-rc.3] - 2026-11-05

Third release candidate of 1.0.0. Fidelity activity CSVs import as investment
activity, the review queue names each document and explains how to fix it,
accounts can be created from a document's own details, account subtypes (IRA
kinds, 401(k)s, 529s and more) with their tax treatment, and an optional
local-model container suggests what unfamiliar documents are; details below.
Migration `0009` adds `account.subtype`.

### Fixed

- Fidelity "Activity & History" CSV exports import as investment activity in
  more cases:
  - The column-mapping editor has an "Investment activity" mode with action,
    symbol, security, quantity, price, commission, fees, settlement date,
    account number and account name columns; its suggestion for a brokerage
    header fills them in. An activity mapping, saved or chosen on a re-run,
    imports buys, sells and reinvestments instead of plain cash rows.
  - More of Fidelity's wording is typed: debit card purchases, checks, bill
    payments, direct debits, margin interest, tax withholdings, participant
    and employer contributions, return of capital, core-account sweeps;
    journal entries, rollovers, conversions and exchanges by the row's signs.
    Unknown actions are listed (first words) in the review item.
  - Masked account numbers (`Z2xxxxxxxx`) no longer merge accounts; the
    `Account` nickname tells them apart and marks IRA/Roth/401(k)/HSA
    accounts as retirement accounts. Choosing an account in the review queue
    takes that item's account only.
  - A sale of shares bought before the export's first row opens them with
    an unknown cost basis and asks for it, instead of rolling back the
    account's whole import.
  - Files written as `a, "b, c"` no longer split inside quoted fields, and a
    short export followed by long disclaimers is still read as CSV.
  - The upload list no longer offers "Map columns" for a file already read
    as investment activity.

### Added

- "Accounts in this file": for a statement or export whose accounts PerFin
  does not know yet, a dialog (from the review queue's "Set up accounts from
  this file" and the document page) lists every account the file names, which
  ones are already known, and an editable proposal for each new one (name from
  the file's nickname or institution, type, institution, currency, last four
  digits, the statement's opening balance). One click creates the accounts
  (and institutions by name) and imports the file into all of them.
  `GET /documents/{id}/accounts`, `POST /documents/{id}/accounts/apply`;
  `apply-mapping` gains per-statement `assignments`.

- Account subtypes: what kind of account it is within its type, with its tax
  treatment. Retirement: traditional, Roth, rollover, SEP, SIMPLE, inherited
  and spousal IRAs, 401(k), Roth 401(k), solo 401(k), 403(b), Roth 403(b),
  457(b), TSP, HSA, pension, annuity, Keogh, profit sharing. Investment
  (brokerage): individual, joint, trust, custodial (UTMA/UGMA), 529, Coverdell
  ESA, ABLE, cash management, crypto, business. Also checking, savings (CD,
  money market...), card, loan, mortgage and line-of-credit kinds (HELOC,
  margin). Chosen in the account form, shown in the accounts list, and proposed
  from account names in imported files (`ROTH IRA`, `Health Savings Account`).
  Migration `0009` adds `account.subtype`; `GET /accounts/subtypes` lists them.
- Optional local-model hints (ADR-0010, docs/ML.md), like Immich's machine
  learning container: the `ml` (CPU) and `ml-cuda` (NVIDIA GPU) Compose
  profiles start an adapter (`perfin-ml`) and an Ollama runtime on an internal
  network. With `PERFIN_ML_URL`/`PERFIN_ML_TOKEN` set, the worker asks it about
  documents whose kind is unknown or whose account nothing matched, sending
  only extracted text. Answers are checked (last four digits only when printed
  in the document) and used only as suggestions: proposed accounts, account
  choices in the review queue, an unknown document kind. `PERFIN_ML_URL` must
  be a local address; anything else stops the app from starting.

- `perfin review-report --household ID`: a household's review items counted
  by kind, reason and state, and the open ones by file type (read-only, counts
  and codes only), to see which misses are most common and whether a change
  helped.

### Changed

- Institution names are compared by their distinctive words, so `Chase` and
  `JPMorgan Chase Bank, N.A.`, or `Fidelity` and `Fidelity Investments`, are
  the same institution when breaking a last-four-digits tie, resolving a
  classified document's institution, and creating institutions from
  "Accounts in this file" (no duplicates). Names made only of common words
  (`First`, `American`, `Capital`...) still need an exact match.
- More ways of printing an account's last four digits are read: `x1234`,
  `x-1234`, `...1234`, "ends in 1234", "ending with 1234", "last 4 digits of
  account number: 1234".
- Review queue items name the file they are about (a link, when it arrived,
  the period it covers) and explain what happened, why PerFin stopped and how
  to fix it, for every reason the importer raises. A "Fix..." dialog shows the
  document's pages beside the explanation and the controls. `ReviewItemOut`
  gains `document` (filename, kind, status, type, source, dates,
  `previewable`) and `reason`, loaded in one query for the whole list.

## [1.0.0-rc.2] - 2026-11-05

Second release candidate of 1.0.0. It adds Fidelity statement and activity
imports, flexible CSV mapping, a pull request template check, a fix to the
release workflow and dependency updates; details below.

### Added

- Pull request template (`.github/pull_request_template.md`) and a
  `pr-template` CI workflow that fails a PR whose description drops a
  template section, leaves Summary or How it was tested empty, or ticks no
  Type of change box (`scripts/check_pr_body.py`). Dependabot PRs are exempt.
- **Fidelity Investments statements** (PDF "Investment Report"): an
  institution parser (`fidelity_investments_statement`) and a shipped
  classifier fingerprint (`fidelity-investments-statement`, both
  "Fidelity Investments" and "Investment Report" required). The core money
  market position is read as the account's cash, so every trade, income
  and transfer is checked against the change in cash; the *Core Fund
  Activity* table is not read (it would count each trade twice). Tests use
  a synthetic three-account report (`tests/fixtures/synth_fidelity.py`,
  invented accounts and instruments), also through the sandboxed extractor.
- **Several accounts in one brokerage PDF**: account headings on each
  account's pages (`Account # Z12-345678`, `INDIVIDUAL - TOD Z12-345678`)
  split a statement "of all accounts" into one statement per account, each
  with its own last four digits, positions, activity, cash, totals and
  reconciliation; each is matched to its own account and a tampered
  account is held alone. A single-account PDF is read as before.
- CSV/TSV imports of never-seen layouts: when no saved mapping fits and the
  deterministic suggestion is confident (date and amount columns named
  exactly, every row before the footer readable, the day/month order not a
  guess), a CSV, TSV or XLSX uploaded by a member or above is imported with
  it and the suggestion is saved as a household mapping (audit
  `import_mapping.create`, `origin: suggested`; run summary
  `mapping_suggested`). A sign taken on trust is never written into a
  liability account. Otherwise the `mapping_required` review item carries
  the suggestion and why it was not used (`auto_import`).
- `suggest_mapping` (`app/ingestion/parsers/structured.py`): the header row
  is searched among the first 11 rows, (column, role) pairs are scored
  exact > whole word > substring and assigned globally best first, a column
  only takes a role when its cells fit, posting/transaction/run dates beat
  settlement dates, a running balance can prove the sign; it returns a
  score, per-role reasons and doubts. `GET /imports/{id}/preview` has
  `suggestion`; `suggest_spec` keeps its signature.
- Multi-account CSVs: `MappingSpec.account_column` (optional; mappings
  saved without it read as before). Rows are grouped by the last four
  digits of the cell and each group is matched to an account like an OFX
  statement; an unmatched group is an `unknown_account` item with `group`
  (no digits), and "remember" re-reads them from the stored table. The
  mapping editor shows and sets the account-number column and the
  suggestion's confidence; the review queue says how many transactions of
  a multi-account file wait for an account.
- Synthetic multi-account activity export
  (`backend/tests/fixtures/synth_activity_csv.py`).
- **Brokerage activity exports** (CSV, TSV, XLSX; Fidelity's "Activity"
  export and layouts like it) import as investment activity: a worker-side
  table parser (`brokerage_activity_table`,
  `app/ingestion/parsers/brokerage_table.py`) recognises the header (date,
  action, amount, symbol or quantity) and builds one investment statement
  per account (last four digits of the account-number column), which
  `statement_import` commits like an OFX `INVSTMTRS`: buys, sells,
  reinvestments, dividends, interest and fees as investment transactions,
  cash deposits and withdrawals as ledger rows. Unknown actions and
  unreadable rows are skipped and reported (`lines_skipped`), never
  guessed; a guessed day/month order holds the statement. Re-uploads and
  re-runs add nothing. The run summary reports the reading
  (`brokerage_table`) and any saved column mapping it took precedence over
  (`mapping_bypassed`); a mapping chosen explicitly still imports the table
  as a cash ledger. New worker-only `source_format` `csv`.

### Changed

- Dependabot no longer proposes major-version bumps for react, react-dom,
  their type packages, typescript and @types/node. Those are deliberate
  upgrades (React 18 per ADR-0001, the TypeScript compiler the lint
  toolchain depends on, and Node types matching the Node 22 runtime);
  minor, patch and security updates still arrive.
- The generic brokerage parser (version 2) reads more layouts: header cells
  over up to three lines or ending in a date, `Symbol/CUSIP` (a valid
  CUSIP is kept as the CUSIP), a security-name column, a settlement date as
  the only date, the description as the action when there is no action
  column but the table names a security; holdings grouped by asset class
  (the group sets the position's asset class, `Total <group>` subtotals are
  no longer taken for the holdings total, `Total Holdings` is), wrapped
  security names, beginning market values; `Beginning Account Value` is no
  longer read as the account value, `Ending Account Value`/`Ending Net
  Value` are. A table header the parser cannot use ends the table above it.
- `activity_type` knows the action words of Fidelity statements and CSV
  exports (`You Bought`, `You Sold`, `Dividend Received`, `Reinvestment`,
  `Interest Earned`, `Long-Term`/`Short-Term Cap Gain`, `Direct Deposit`,
  `Electronic Funds Transfer Received`/`Paid`, `Foreign Tax Paid`, `Fee
  Charged`, `Transferred From`/`To`: a transfer of units with a security
  and a quantity, else cash) whatever their case or trailing `(Cash)`.
- A text file whose first lines are a few delimited rows followed by note
  lines (a short brokerage export's disclaimers and "Date downloaded" line)
  is sniffed as CSV/TSV; before, it was plain text and not imported.
- The synthetic multi-account cash-ledger tests write the activity export
  with its `Action` column named `Details` (`LEDGER_HEADER`): with the real
  header the file is brokerage activity.
- A document of several brokerage accounts is linked to no account: the
  account the classifier matched is undone after parsing (an account
  chosen at upload stays), and an account chosen on a re-run takes only
  the statements with its own last four digits or, when it records none,
  the single unmatched one, never several accounts' holdings at once.

### Fixed

- A library printing to stdout inside the extraction sandbox (Python
  `print`, C `printf`, a native runtime's log) corrupted the JSON result,
  so the document failed with `extraction failed: bad_output`. The child
  now keeps a private duplicate of stdout for the result and sends
  descriptor 1 to stderr; a `bad_output` detail names the failing fields
  or the JSON position, never the values.
- PDF table finding no longer loads PyMuPDF's neural layout model
  (onnxruntime) into the sandbox, where its thread pool made calls the
  seccomp allowlist refuses (`syscall_denied`, `eventfd2`) at random.
- The mapping suggestion took the first column whose header merely
  contained a role word: an always-empty "Exchange Currency" became the
  currency column (and then every row was refused), a "Type" column of
  `Cash`/`Margin` became the debit/credit indicator, and a settlement date
  could become the date. Exports with a preamble got no suggestion.
- An empty cell in a mapped currency column refused the row; it now means
  the account's currency (a cell that is not a three-letter code still
  refuses it).
- Rows with a single filled cell (disclaimer paragraphs, "Date downloaded")
  are no longer reported as skipped rows.
- "Remember" on an `unknown_account` item of a brokerage statement (OFX,
  PDF, activity export) read the last four digits of the bank statement
  with the same index; it now reads the investment statement's, and the
  item's account suggestions rank by them.
- Resolving one group's `unknown_account` item of a multi-account CSV gave
  the chosen account every other group that matched no account too. The
  review queue now passes the item's `group` to the re-run
  (`apply-mapping` takes an optional `group` with `account_id`), and
  without one a chosen account takes only the groups with its own last
  four digits or, when it records none, the single unmatched group. The
  same rule now applies to brokerage statements: an account that records
  its digits no longer also takes the one statement left unmatched.
- A group `unknown_account` item carries the digest of the mapping spec
  that numbered its groups (`spec_digest`); once someone edits the mapping
  the item is `409 review-item-stale`, so "remember" can no longer write
  another account's digits onto the chosen account.
- The rule that a sign read on trust never writes into a liability account
  could be bypassed: a card export uploaded without an account saved its
  trust-only mapping, and picking the card account then imported every
  purchase as money in. Such a mapping is now marked `sign_unverified`
  (`import_mapping.sign_unverified`, migration 0008) and waits as
  `mapping_required` (`sign_unverified`) whenever it meets a liability
  account, until a person saves its spec or chooses it explicitly.
- A household mapping auto-saved from a member's upload could take over
  the owner's account-bound mapping for the same header (most recently
  used first), so the owner's files without an account stopped being
  routed to it; the one account-bound mapping now wins.
- The suggestion read a `Type` column of `DEBIT`/`CREDIT`/`CHECK` beside a
  signed amount as a debit/credit indicator, turning every `CHECK` into
  money in, and still imported it on its own. An indicator is no longer
  suggested beside amounts that carry a minus sign, and an indicator cell
  that is no direction word is a doubt (`indicator_values_unknown`).
- The Fidelity activity CSV dates a trade by its run date and the PDF
  Investment Report by its settlement date, so importing both recorded the
  trade twice (lots and holdings doubled). A row the account already
  records from another document under a trade or settlement date it
  shares (or within 5 days when one has no settlement date) is no longer
  written; an `investment_possible_duplicate` item says so.
- The brokerage activity table reader dropped the extractors' formula
  guard from the security names and memos it kept (SECURITY.md C7); they
  keep it now, and a symbol that would be a formula is refused.

## [1.0.0-rc.1] - 2026-11-05

First release candidate of 1.0.0 (the final `v1.0.0` follows the external
security review). It covers every roadmap phase:

- **Phase 0, Foundations:** monorepo (FastAPI, SQLAlchemy, Alembic; Vite,
  React, TypeScript), `perfin config check` with insecure-config refusal,
  forced row-level security, a hash-chained audit log, the Postgres job
  queue, security headers, strict CSP and CSRF, PII-scrubbed logging,
  health and metrics endpoints, and a hardened Compose stack.
- **Phase 1, Identity (v0.1):** first-run setup, Argon2id local login with
  rate limits and lockout, server-side sessions, TOTP, WebAuthn, recovery
  codes and step-up, OIDC (Authentik) with PKCE and back-channel logout,
  households, roles and account grants, personal access tokens, and the
  authz matrix test.
- **Phase 2, Ledger (v0.2):** accounts, transactions, splits and transfers,
  categories, payees, tags, rules, budgets, goals, reports, CSV/OFX/QIF/XLSX
  imports with dedupe, the review queue, server-sent events, and encrypted
  full export and import.
- **Phases 3-4, Smart ingestion (v0.3, v0.4):** the sandboxed extraction
  subprocess (seccomp default-kill allowlist, Landlock, rlimits), text-layer
  and scanned PDFs with OCR, receipt photos, the drop folder, IMAPS email
  intake, optional ClamAV, a document classifier, learned layouts and
  full-text search.
- **Phases 5-6, Wealth and tax (v0.5, v0.6):** securities, prices and FX,
  lots and cost methods, performance, loans with amortisation, manual
  assets, net worth history, recurring transactions and the cash-flow
  calendar; the tax vault with checklists, parsed forms and the accountant
  export.
- **Phase 6b, AI access boundary (v0.7):** the `ai:read` scope with
  allowlist serialisers, the column classification gate, the identity
  scrubber with a zero-leak corpus, and the read-only `perfin mcp` server
  (stdio and HTTP) with client management, an exposure preview and a full
  audit log.
- **Phase 7, Polish and 1.0:** notification channels (SMTP, webhook, ntfy,
  Gotify, Matrix), a configurable dashboard, PWA manifest, service worker
  and responsive layout, encrypted backup and restore with a restore test,
  master-key rotation, a Helm chart, a tag-only signed release workflow,
  the `make dev` stack with a synthetic seed, deployment, operations,
  upgrading and backup guides, the documentation site, and the v1.0.0
  release verification (docs/RELEASE_CHECKLIST.md).

Still open before the `v1.0.0` tag is pushed: an external security review,
and the first run of the release workflow, which only runs on a tag. Other
known limits are listed in docs/ROADMAP.md and docs/security/CONTROLS.md.

### Added

- Alembic revision `0007_phase7` (Phase 7 integration):
  `notification_channel` with forced RLS and application-role grants; the
  downgrade drops it and the `notification_channel_type` enum type. The
  dashboard layout (user settings JSON) and key rotation need no DDL.
- Dev stack (Phase 7). `make dev` (`scripts/dev.sh`) generates `.dev/env`
  (random database passwords, master and secret keys; no defaults), starts
  PostgreSQL 16 from `deploy/docker-compose.dev.yml` (mirror-able with
  `POSTGRES_IMAGE`), runs the migrations, seeds a synthetic household and runs
  the API with reload, the worker and Vite; `make dev-down` and `make
  dev-reset` stop and wipe it. `scripts/seed_dev.py` refuses unless
  `PERFIN_DEV_MODE=true` and unless the database is empty or holds only its own
  finished seed; it creates `dev-admin` with a generated password printed once,
  then builds accounts of every type, six months of transactions, rules, a
  budget, goals, investments, a mortgage and a car loan, manual assets, net
  worth history, a tax year with forms, a revoked AI client example and
  uploaded documents that leave items in the review queue.
  `test_seed_dev.py` covers it. Documented in README and
  `docs/DEVELOPMENT.md` ("Dev stack").
- Operator documentation and documentation site (Phase 7).
  `docs/DEPLOYMENT.md` (single-host Compose install: prerequisites, exact
  secret-generation commands, the master key and its acknowledgement, first
  boot and where the one-time setup URL appears, the Caddyfile plus tested
  nginx and Traefik equivalents, drop folder, email intake, MCP role,
  hardening checklist walk-through) and `docs/OPERATIONS.md` (JSON logs,
  `/healthz` and `/readyz`, the internal metrics port and metric names, job
  states and dead letters, fixes for pending migrations, master key mismatch,
  sandbox, OCR, IMAP TLS, OIDC issuer, disk and stuck jobs). New
  `docs/security/` pages `oidc.md`, `email.md` and `what-leaves-the-host.md`
  complete the per-integration set linked from the README. `mkdocs.yml`
  (mkdocs-material, no analytics, no external fonts) with hash-pinned
  `requirements-docs.txt` and a CI `docs` job running `mkdocs build --strict`.
  The shipped `Caddyfile.example` now allows the full-import archive size on
  `/api/v1/import` (it was capped at 1 MB, below `PERFIN_IMPORT_MAX_MB`).
- Release pipeline and Helm chart (SECURITY.md H1, H2, H4; docs/UPGRADING.md).
  `.github/workflows/release.yml` runs only on pushed `v*` tags: it checks the
  tag against every version string and the CHANGELOG (`scripts/release_notes.py`),
  builds `ghcr.io/bchap9/perfin` and `ghcr.io/bchap9/perfin-egress` for
  linux/amd64 and linux/arm64 (buildx + QEMU), pushes them by digest, scans
  both platforms with Trivy (fixable HIGH/CRITICAL fails the release) before
  adding the semver tags, writes CycloneDX and SPDX SBOMs, signs the digest and
  SBOMs with cosign keyless signing, attests SLSA provenance and the SBOMs
  (GitHub attestations, pushed to the registry), publishes the signed Helm
  chart to `oci://ghcr.io/bchap9/charts/perfin`, and creates the GitHub
  release with the CHANGELOG section, SBOMs, Sigstore bundles and
  `SHA256SUMS`. Actions are SHA-pinned and each job has least-privilege
  permissions. `deploy/helm/perfin` deploys the api, worker, optional mcp,
  a `perfin migrate` hook Job and an optional bundled PostgreSQL with the
  Compose hardening (non-root, read-only root, no capabilities, RuntimeDefault
  seccomp, no service-account token), secrets only from existing Secrets
  (per-role keys, the mcp pod gets none), and default-deny NetworkPolicies
  mirroring the Compose networks. CI lints and validates the chart (`helm
  lint --strict`, `kubeconform`) with tools built by
  `scripts/install-chart-tools.sh` from hash-pinned Go modules;
  `backend/tests/unit/test_helm_chart.py` and `test_release_workflow.py`
  check the hardening and the pipeline. `docs/UPGRADING.md` covers the
  versioning policy, verification with cosign, Compose and Helm upgrades,
  migrations and rollback.
- Backup, restore and key rotation (SECURITY.md G3, G4; docs/BACKUP_RESTORE.md):
  `scripts/backup.sh` writes one passphrase-encrypted archive (`pg_dump`
  custom format as the superuser, the encrypted document volumes, a manifest
  with versions, Alembic head, the verified audit-log head and the master
  key's fingerprint, never the key) for Docker Compose or a bare-metal
  install; `scripts/restore.sh` verifies and decrypts it, refuses a non-empty
  target without `--force`, restores into a fresh stack, migrates, checks the
  audit chain against the manifest and decrypts a sample of documents. New
  commands `perfin backup-encrypt`, `backup-decrypt`, `backup-info`,
  `restore-check` and `perfin rotate-key` (rewraps every data key under a new
  master key in resumable batches without re-encrypting any payload, verifies,
  audits; `--status`, `--dry-run`). New setting `PERFIN_MASTER_KEY_VERSION`
  (default 1). A restore verification test runs both scripts in CI.

- Configurable dashboard (Phase 7): widgets from a catalogue of twelve (net
  worth, cash flow, spending, budgets, goals, recent transactions, review
  queue, upcoming bills, holdings and allocation, loans, tax documents,
  accounts) that the person adds, removes, reorders and resizes; the layout is
  saved per user as `dashboard_layout` of `GET/PATCH /settings/user`.
- PWA: web app manifest, generated PNG icons (`npm run gen:icons`) and a
  service worker that caches only fingerprinted `/static/` files and icons and
  never touches `/api` or the HTML shell; registered from a module through a
  Trusted Types policy (no inline script).
- Responsive pass: tables collapse to cards below 40rem, the small-screen
  drawer has a backdrop and closes on Escape, touch targets are 44px on
  touch screens, no horizontal scroll at 375px.

- Notification channels (Phase 7, docs/security/notifications.md): SMTP
  (STARTTLS required or implicit TLS, certificate verified), webhook (HTTPS,
  HMAC-SHA256 signature over timestamp and body), ntfy, Gotify and Matrix,
  configured per household under Settings > Notifications
  (`/api/v1/settings/notifications/channels`: owner/admin browser session,
  changes behind step-up, credentials write-only and encrypted per row, test
  send). Account lock (A1), recovery code use and MFA reset (A8), role and
  membership changes (I2), sign-in from a new device, AI client creation,
  tax reminders, daily "review items waiting" and `perfin notify
  backup-failed` queue a `send_notification` job per subscribed channel,
  retried with backoff; every message is one generic sentence without
  identity or financial detail. Egress through `SafeHttpClient` / a vetted
  SMTP socket, optional instance allowlist
  `PERFIN_NOTIFICATIONS_ALLOWED_HOSTS`. New table `notification_channel`
  (needs the Phase 7 migration).
- Full export completeness (PORTABILITY.md, archive format version 2): the
  export now carries every user-data table added in Phases 3-6 (learned
  parser layouts, securities, prices, per-account cost method, investment
  transactions with re-mapped `lot_links`, lots, loans, payments, rate
  changes, manual assets, valuations, stored net worth snapshots, tax years,
  checklists, category-to-schedule maps, tax form data, AI client
  definitions without their token). Import rebuilds the `holding` cache,
  merges securities and tax years, recomputes investment import hashes and
  still reads format version 1 archives. `NOT_EXPORTED` keeps only secrets,
  per-instance identity, caches and job/audit history, each with a reason.
  A round-trip test of a fully populated household compares ledger,
  holdings, lots, loans, net worth and tax figures.
- AI access boundary, API side (Phase 6b, AI_ACCESS.md, ADR-0007): read-only
  `/api/v1/ai/v1` views (policy, overview, spending, income, trends, budget
  status, goals, recurring, net worth history, allocation, accounts,
  transactions, anomalies, holdings, performance, loans, loan schedule and
  simulation, tax year summary) built from allowlist serialisers with the
  client's mode (Detail/Aggregates) and options (rounding 1/10/100, hidden
  nicknames, hidden merchant location, date cap) applied server-side;
  `ai_client` and `ai_call` tables; the AI-client flow as the only minter of
  `ai:read` tokens (owner, step-up), confined to the AI views; per-client
  rate limits, bounded periods, cached aggregates; every call audited with
  the full response. Free-text scrubber (`app/core/scrubber.py`) with a
  zero-leak corpus and a false-positive corpus; a CI test that fails on any
  unclassified database column or an AI field reading a denied one.
- AI client management (Phase 6b, `app/api/routers/settings_ai.py`):
  `/settings/ai/clients` (owner session; create and revoke with step-up;
  mandatory expiry; the `ai:read` token shown once, hashed at rest;
  revocation immediate), the exposure preview of every tool with chosen
  options on real data before saving (`POST /settings/ai/preview`) or for a
  saved client, placeholders marked by JSON path, nothing stored; the
  searchable call audit (`/settings/ai/calls`); the scrubbed-vs-original
  view of the owner's free text with terms to try
  (`/settings/ai/scrubber-preview`); the instance kill switch
  (`/settings/system/ai`, instance admin) and the household AI flag
  (`/settings/ai`), either of which makes every AI view answer `403
  ai-access-disabled`; `ai_clients_active` on `GET /auth/session` for the
  persistent banner.
- MCP server (Phase 6b, `perfin mcp`, `backend/app/mcp/`): a protocol server
  only (the `mcp` SDK; no model client, ADR-0007) with the 19 read-only tools
  of AI_ACCESS.md, each one GET on an `/ai/v1` view with the client's
  `ai:read` token; arguments validated (unknown ones refused, free-text
  filters matched against canonical names by the API); a second scrub of
  every answer and error; a per-process rate limit; API refusals (kill
  switch, revoked client, Aggregates mode, date cap) reported as policy.
  stdio by default; streamable HTTP behind the proxy with a separate bearer
  secret and an origin allowlist. Its own configuration gate: no database
  URL, master key or secret key (refuses to start with one), only
  `PERFIN_MCP_API_URL` and `PERFIN_MCP_TOKEN(_FILE)`; `PERFIN_ROLE=mcp perfin
  config check` runs it. Compose: the `mcp` service shares the internal `ai`
  network with the API only (and `mcp_edge` with the proxy), no volumes,
  read-only, every capability dropped; `.env.example` gains `MCP_API_TOKEN`,
  `MCP_HTTP_TOKEN` and `MCP_RATE_PER_MINUTE` for it.

#### Phase 6b web UI

- Settings > AI access (`frontend/src/features/settings/ai/`, owner only):
  create-client wizard (name, mode, transport, expiry, rounding, hide
  nicknames and merchant location, date range cap, denylist, aliases) with a
  live exposure preview of every tool on real data; scrubber placeholders are
  highlighted by splitting text into styled spans (never `innerHTML`);
  step-up on create and revoke; the token is shown once with a copy button
  and the MCP setup snippet; client list with preview and revoke; calls audit
  with a search box; scrubbed-versus-original tuning view; household and
  instance kill switch status. A persistent banner in the app layout shows
  while `ai_clients_active`. `ShowOnceDialog` accepts children.

#### Phase 6b integration

- Alembic revision `0006_phase6b_ai`: `ai_client` and `ai_call` with forced
  RLS, application-role grants and the household-consistent
  `ai_call -> ai_client` key; the downgrade drops them and the `ai_mode` and
  `ai_transport` enum types. The AI switches are `setting` rows (no DDL).
  `test_migrations.py` covers the 0006 downgrade and cross-household
  isolation of both tables on a migrated database.
- `tests/security/mcp/test_mcp_e2e.py`: the owner creates a client over HTTP
  after a TOTP step-up and reads its exposure preview; `perfin mcp` runs
  over stdio against a live API with only its token, and an MCP client
  session calls six tools; no planted identity in any answer or `ai_call`
  row, each row equal to the answer the client got; the household flag and
  the instance kill switch refuse and resume the running server; revoking
  stops the token at once.
- Fix: QIF and OFX extraction stopped with a validation error on a file with more
  than 100 statements (repeated `!Type:Bank` headers or `STMTRS` blocks; found
  by the sandbox mutation fuzzer); extractors now keep the first
  `MAX_STATEMENTS` (100) and warn `statements_truncated:100`.

#### Phase 6b: finalization (live verification, setup guides)

- `tests/security/mcp/test_mcp_live.py`: the setup of docs/MCP_CLIENTS.md
  run for real. The installed `perfin mcp`, given only `PERFIN_MCP_ENABLED`,
  `PERFIN_MCP_API_URL`, a mode-0600 `PERFIN_MCP_TOKEN_FILE` and
  `PERFIN_MCP_TRANSPORT=stdio`, serves an `mcp` SDK client session over
  stdio against a live uvicorn API. The session calls all 19 tools (24
  calls) on a household with identity and documents planted. No identity
  appears in any answer or `ai_call` row, every audit row equals the answer
  received, and the transcript is written for review.
- `tests/security/ai/test_ai_documents.py`: documents never reach `ai:read`
  (the P56-SR-R1-02 limit at the AI boundary). Every document column and
  every reference to a document is denied, and no AI route serves a
  document. An unredacted Form 1040, a payslip and a statement with an SSN,
  a name, an address and an employee id in their kept page text appear in
  no AI answer and no audit row, and the `ai:read` token is refused on
  every document, review and import route.
- `tests/security/mcp/test_mcp_deploy.py::test_stdio_override_of_the_compose_service_needs_nothing_else`:
  the documented `docker exec` and `docker compose run` stdio forms start
  from the compose service's environment plus `PERFIN_MCP_TRANSPORT=stdio`.
- docs/MCP_CLIENTS.md verified against the code and finalized. Page labels
  now match the shipped Settings > AI access page, the kill switches are
  located correctly, a stdio-only `docker compose run` form was added, and
  the `mcphost` model flag was corrected to `ollama/<model>`. The Claude
  Code stdio forms (installed `perfin`, `uv run --project`) and the HTTP
  form with its bearer header were run against Claude Code 2.1 and reported
  *Connected*; a wrong bearer was refused with 401. A verification-status
  note lists what was not run: Claude Desktop, the real image, and
  `mcphost` with Ollama.

#### Phases 5+6: finalization (live run, wealth screens)

- Wealth screens (`frontend/src/features/{investments,loans,networth,assets,calendar}/`):
  `/investments` (holdings with lots, gains, short/long term and allocation
  by asset class, sector and account; performance with TWR/MWR over eight
  ranges; transaction list and entry for all ten types with exact decimal
  arithmetic; manual prices and new securities), `/loans` and
  `/loans/<id>` (summary and payoff projection, schedule as originated or
  from today, extra-payment simulator, payments with the schedule check,
  rate changes), `/net-worth` (history chart, breakdown by class and
  account, projection, "Your accounts only" when the scope is partial),
  `/assets` and `/assets/<id>` (valuation history and chart), `/calendar`
  (cash-flow calendar, recurring items, detection). Navigation gains Assets
  and Calendar; SSE invalidation covers `asset.*`, `schedule.*`,
  `networth.*`, `security.*` and `fx.*`. Charts plot display-only numbers;
  every figure is formatted from the API's decimal strings.
- Live verification of Phases 5+6 on a fresh database migrated to `0005`
  with `perfin api`, `perfin worker` and the built SPA (ROADMAP Phase 5
  "Live verification"): brokerage PDF and OFX, two mortgage statements, a
  W-2 and a 1099-INT through the sandbox; holdings, performance, payment
  split, schedule and simulation, manual asset, recurring detection and
  calendar, the `snapshot_net_worth` job, the tax checklist and the
  accountant export over HTTP; then every Phase 5/6 page in headless
  Chrome.
- `tests/integration/test_commit_before_response.py`: an ASGI probe checks
  that a `POST /accounts` row is committed by the time the last byte of its
  response is sent.
- `src/app/shell.test.tsx`: every navigation section is served by a
  feature route (none falls back to the "Not built yet" placeholder).
- Tax vault screens (`frontend/src/features/tax/`): `/tax` years list with the missing-documents reminder, `/tax/<year>` with the checklist (status changes, linked documents, issuer, amounts by box label), deductible roll-up by category and schedule hint, accountant export with step-up and progress, `/tax/mapping` schedule-hint editor, and a dashboard card for missing tax documents.

#### Phases 5+6 integration

- Alembic revision `0005_phase5_6_wealth_tax`: `security`, `price`,
  `investment_account`, `investment_txn`, `lot`, `holding`, `loan`,
  `loan_payment`, `loan_rate_change`, `manual_asset`, `asset_valuation`,
  `net_worth_snapshot`, `tax_year`, `tax_checklist_item`,
  `tax_category_map`, `tax_form_data` and `tax_export`, with forced RLS and
  application-role grants listed explicitly; the downgrade drops them and
  their 11 enum types. `test_migrations.py` covers the 0005 downgrade and
  cross-household isolation of the new tables on a migrated database.
- `tests/integration/test_phase5_6_end_to_end.py` (see "Resume here").
- `backend/openapi.json` and `frontend/src/lib/api/schema.d.ts` regenerated
  with every Phase 5/6 route.

#### Phase 6: tax vault

- Tax form parser (`app/ingestion/parsers/tax_forms.py`, first in the
  parser registry): form type, tax year, issuer name and amount boxes of
  every form of the registered jurisdiction form sets (US: W-2 boxes 1-6,
  12 per code, 14 and 16-19, the 1099 family, 1098 interest/points/property
  tax, 1098-E/T, 1095-A monthly amounts, 1095-B/C coverage months and
  1095-C line 15); consolidated statements give one form per heading.
  Identity boxes are never read, and the stored pages, words, layout
  tables and search index of a tax form are masked (`redact_identity`).
  New stored boxes `14t`, `10t` and `cov` (`app/domain/tax/forms/us.py`)
  and `FormDef.issuer_box`.
- Parsed forms go to the tax vault (`app/domain/tax/ingest.py`): form data,
  the tax year opened when its first form arrives, the checklist ticked off
  (expected item received, or a `detected` one).
- Tax vault API (`app/api/routers/tax.py`, `app/domain/tax/service.py`):
  tax years (open/filed/closed), a checklist learned from the year before
  and editable, parsed form amounts, schedule/line hints per category
  (`/tax-category-map`) and a yearly roll-up by category and hint of
  deductible tagging (`category.tax_treatment`, `transaction_split.tax_flag`;
  visible accounts only, `partial` otherwise; `app/domain/tax/rollup.py`).
  `ledger_lines` exposes `tax_flag`.
- Accountant export (`app/domain/tax/export.py`, job `tax.export`, table
  `tax_export`): zip of the year's originals plus `summary.csv` (C7) and
  `summary.pdf` (reportlab), owner/admin with step-up, encrypted at rest,
  downloadable once, shredded after `PERFIN_EXPORT_TTL_MINUTES`.
- Reminders as a year closes (`app/domain/tax/reminders.py`, daily
  `tax_reminders` -> `tax.reminders`): a `tax.reminder` event ("3 expected
  documents still missing") on the days of `PERFIN_TAX_REMINDER_DATES`.
- Synthetic tax forms (`tests/fixtures/synth_tax_forms.py`, reportlab) with
  obviously fake identity values, and a test that none of them reaches any
  database column or API response.

#### Phase 5: net worth, recurring transactions and the cash-flow calendar

- Net worth engine (`app/domain/networth/compute.py`): values every account
  on any day from balance snapshots, ledger transactions, investment
  positions x prices (lot engine replay, no look-ahead), loan balances and
  manual valuations, converted to the base currency; breakdown by asset /
  liability class, account type and account (version 1 JSON).
- Daily `snapshot_net_worth` job (`app/workers/handlers/networth.py`): one
  `networth.snapshot` per household and day (dedupe key), upserting the day's
  `net_worth_snapshot` and backfilling missing days (up to ten years).
- Net worth API (`GET /networth?range=&granularity=`, `/networth/latest`,
  `/networth/projection`; scope `networth:read`): stored snapshots only for
  principals who see every account, figures recomputed over visible
  accounts for everyone else; projections by least-squares trend and by
  average monthly savings (`app/domain/networth/projection.py`).
- Recurring detection (`app/domain/recurring/detection.py`): payee + amount
  clustering, weekly/biweekly/monthly/quarterly/annual cadences with
  tolerance windows, confidence from regularity, amount stability and
  count. Feeds `scheduled_transaction` (`origin = detected`) through
  `POST /recurring/detect` and the daily `detect_recurring` job; manual
  schedules win, a detected schedule keeps a person's name and `active`.
- Recurring API (`/recurring`, `/recurring/candidates`, `/recurring/{id}`;
  scopes `transactions:read|write`, account visibility, edit grants for
  writes) and the cash-flow calendar (`GET /cashflow/calendar?days=`):
  expected income and bills per day with a running balance of the visible
  liquid accounts.
- `schedule.*` and `networth.*` events are account-scoped in the event
  stream (dropped for members/viewers when they name no visible account).

#### Phase 5: loans and manual assets

- Amortisation engine (`app/domain/loans/amortization.py`, pure `Decimal`):
  fixed and variable schedules (a variable loan re-amortises at each rate
  change), interest rounded to the minor unit per period with the final
  payment absorbing the rounding, escrow, one-off and recurring extra
  principal, payoff projections and extra-payment comparisons. Property
  tests (hypothesis): principal parts sum to the principal exactly, zero-rate
  loans, rate changes mid-term, extras never cost more.
- Loans API (`/loans`, `/loans/{id}/schedule?basis=original|projected`,
  `POST /loans/{id}/simulate`, `/loans/{id}/payments`,
  `/loans/{id}/rate-changes`; scopes `loans:read|write`, account
  visibility, edit grants for writes): terms with a default level payment,
  payment splits checked against the schedule, payoff projection from the
  balance owed. The loan account's balance is kept in sync with the
  principal owed (`computed` balance snapshots rebuilt on every change).
- Manual assets API (`/assets`, `/assets/{id}/valuations`; scopes
  `accounts:read|write`): kind (property, vehicle, other, the account's
  `asset_kind`), description, valuation method and manual valuation
  history; each manual valuation is also the account's manual balance of
  that day. `manual_asset.description` is now `classification: identity`
  (it may hold an address): kept out of audit rows, events and `ai:read`.
- Loan statement parser (`app/ingestion/parsers/loan_statement.py`) and
  commit (`app/domain/loans/statement_import.py`): payment split, previous
  and new principal balance, rate and next due date, reconciled (parts vs
  total, previous - principal vs new) and held for review when they do not
  reconcile (C12); matched to a loan the uploader may edit (F3); statement
  balance snapshots; rate differences flagged. Synthetic statements:
  `tests/fixtures/synth_loans.py`.
- Events `loan.*` and `asset.*` are account-scoped for members and viewers.

#### Phase 5: price and FX feeds

- Price providers (`app/integrations/prices/`): a provider protocol, the
  always-on manual provider and one opt-in external provider,
  `PERFIN_PRICE_PROVIDER=http_json`, a generic JSON-over-HTTPS adapter
  configured by a URL template with one `{symbol}` placeholder and dotted
  JSON paths (no vendor or API key built in; an optional credential header).
- FX providers (`app/integrations/fx/`): manual rates and the opt-in ECB
  daily reference rates (`PERFIN_FX_PROVIDER=ecb`, parsed with `defusedxml`,
  crossed to each household's base currency).
- `refresh_prices` worker job (every 6 hours; a no-op while both feeds are
  `none`, the default): one request per distinct symbol of the instance,
  only symbols leave the host; manual prices and rates are never replaced.
  Both feeds use the SSRF-safe client with an allowlist of their own host
  and the egress proxy when `HTTPS_PROXY` is set.
- `GET/POST /fx-rates` (history; manual rate of a pair and day, owner or
  admin) and `DELETE /fx-rates/{id}`.
- `perfin config check`: `prices.provider_enabled` / `fx.provider_enabled`
  warnings naming the destination, `prices.provider_invalid` error,
  `prices.url_unused` warning.
- docs/security/integrations.md: what leaves the host for each feed.

#### Phase 5: brokerage statement parsing

- OFX/QFX investment statements (`INVSTMTRS`: `INVPOSLIST`, `INVTRANLIST`,
  `INVBAL`, `SECLIST`) are extracted in the sandbox
  (`app/ingestion/extract/ofx_invest.py`, extractor `ofx/2`; previously a
  warning) into `ExtractResult.investments`.
- Brokerage PDF/scan parser (`app/ingestion/parsers/brokerage.py`):
  holdings and activity tables, cash and totals by label;
  `brokerage_statement_generic` and `placeholder_brokerage` (the fictional
  Placeholder Brokerage of the shipped fingerprint).
- Reconciliation for every brokerage statement: positions x prices vs
  market values, holdings total, cash, account value (SECURITY.md C12).
- Importer (`app/domain/investments/statement_import.py`): securities,
  `investment_txn` rows with a cross-format `import_hash`, opening
  positions, lots and holdings through the investments lot engine, prices
  (`source = statement`), a balance snapshot, cash transfers as ledger
  transactions; statements that do not reconcile or are out of order are
  held in review.
- Synthetic fixtures `tests/fixtures/synth_brokerage.py` (PDF and OFX),
  unit tests and end-to-end tests through the worker.

#### Phase 5: investments API, lot engine and performance

- `GET/POST/PATCH/DELETE /securities` (household catalogue; securities used
  only on hidden accounts are `404` for members and viewers) and manual
  prices (`GET/POST /securities/{id}/prices`, `DELETE
  .../prices/{price_id}`).
- `GET/POST/PATCH/DELETE /investments/transactions`, `GET
  /investments/holdings?account=`, `GET /investments/performance?range=&account=`,
  `GET/PUT /investments/accounts/{id}/cost-method` (`fifo` | `average`).
- Lot engine (`app/domain/investments/lots.py`): FIFO with specific-lot
  selection, average cost per account, buys, sells, reinvestments,
  transfers, splits and ratio corporate actions (exact ratios), statement
  seed lots; lots, `lot_links` and the holding row of a pair are replayed on
  every change (`service.replay_position`). Realised and unrealised gains
  split short/long term by holding period.
- Performance (`performance.py`, `returns.py`): daily valuation chain TWR
  with external flows, MWR by XIRR (Newton with a bisection fallback on
  `ln(1 + x)`, `Decimal` only), allocation by asset class, sector and
  account in the base currency.
- New table `investment_account` (cost method) and column
  `investment_txn.lots_specified`; the Phase 5/6 Alembic revision must
  create them (enum `cost_method`).
- Property tests: unit and cost conservation of lots, split invariance,
  TWR = MWR without flows (`tests/unit/test_investment_lots.py`,
  `test_investment_returns.py`); API tests
  `tests/integration/test_investments_api.py`; authz matrix kinds
  `security`, `investment_txn`, `price`.

#### Phases 5+6: investment, loan, asset, net worth and tax vault models

- Household-scoped models with forced RLS and household-consistent keys:
  `security`, `price`, `investment_txn` (`lot_links` JSON), `lot`, the
  `holding` cache (`app/domain/investments/`), `loan`, `loan_payment`,
  `loan_rate_change` (`app/domain/loans/`), `manual_asset`,
  `asset_valuation` (`app/domain/assets/`), `net_worth_snapshot`
  (`app/domain/networth/`), `tax_year`, `tax_checklist_item`,
  `tax_category_map` and `tax_form_data` (`app/domain/tax/`). Quantities
  and unit prices are `NUMERIC(28, 10)` (`app.db.types.QUANTITY`,
  `UNIT_PRICE`).
- Pluggable tax form sets (`app/domain/tax/forms/`): the `FormSet`
  protocol and registry, and `US` with W-2, 1099-INT/DIV/B/MISC/NEC/R/G/SA,
  1098, 1098-E, 1098-T and 1095-A/B/C box definitions. `tax_form_data`
  keeps amounts only: identity boxes (SSN/EIN/TIN, names, addresses,
  account numbers) are discarded by `sanitize_boxes` before storage, and a
  check constraint refuses non-amount box values.
- Service building blocks for the Phase 5/6 tracks (visible holdings, lots,
  investment transactions, loans and manual assets; holdings rebuild from
  lots; latest price, rate on a day, latest valuation; household-totals
  visibility; `record_form_data`), factories in `tests/fixtures/wealth.py`
  and `tests/fixtures/tax.py`. No Alembic revision yet (written at phase
  integration); the tables are not in the export archive yet.

#### Phases 3-4: finalization (live run, seccomp allowlist, ingestion security page)

- Live verification of Phases 3+4 against `perfin api` and `perfin worker`
  on a fresh database migrated to `0004` (dev mode, built SPA, fake local
  clamd): setup and TOTP login; a text-layer PDF statement into the ledger;
  a scanned PDF through OCR (22 of 22 rows) with its page preview; a receipt
  photo linked to its card transaction; a drop-folder PDF attributed to the
  subfolder's member, imported and moved to `processed/`; an infected test
  file rejected; full-text search; a doubtful OCR row corrected, confirmed
  and turned into a rule through the review UI in headless Chrome; `perfin
  ingest --dry-run`. Repeated after the seccomp change below.
- `docs/security/ingestion.md`: what leaves the host during ingestion
  (document content never does; ClamAV and OCR are local; the only egress
  is the IMAPS connection to the mailbox host an owner or admin configures,
  through the egress proxy when one is set).
- `frontend/src/features/imports/mapping.test.tsx`: the mapping editor
  shows the PDF page above the table and saves the mapping with its
  `document_id`, so the server fingerprints the table header.
- `tests/integration/test_migrations.py`: every enum type's labels on a
  migrated database equal the models' (`alembic check` does not compare
  enum labels), and the `0004` downgrade removes `receipt_match` items only.

#### Phases 3-4: frontend (document library, page viewer, review tools, intake settings)

- Document library (`/documents/library`): filters for kind, status and tax
  year plus full-text search, matched words shown as `<mark>` elements (no
  `innerHTML`). Document detail (`/documents/{id}`): file facts, a download
  link only when the server says `downloadable`, and the page viewer.
- Page viewer (`frontend/src/features/documents/PageViewer.tsx`): the
  sandbox-rendered page image, previous/next page, highlight boxes placed
  as percentages of the page, optional outline of recognised words.
- Review tools (`frontend/src/features/review/ReviewTools.tsx`): doubtful
  rows highlighted on the page with other readings to pick and "Save
  corrections and confirm"; "Create a rule from this"; "Remember this
  layout" for a new institution (optionally re-reading received files);
  member and account choice with "remember this folder" for drop-folder
  files of unknown owner; "Show receipt" beside receipt-match suggestions;
  "Map columns" for any item that needs it, with the PDF or image page
  above the mapping form.
- Settings > Intake (`/settings/intake`, owner/admin): mailbox form (the
  password is sent only when typed), connection test, alias display and
  rotation (step-up), allowed senders.

#### Phases 3-4: integration (migration 0004, end-to-end test)

- Alembic revision `0004_phase3_4_ingestion`: `dropfolder_route`,
  `dropfolder_entry`, `mail_intake` and `mail_intake_message`, each with
  forced RLS (`household_isolation`) and DML grants for the application
  role, household-consistent keys to `document`/`account` (ADR-0008); a
  data step backfills `document.extracted_text` for documents extracted
  before Phase 3 (in SQL, `FORCE` lifted on `document` for that statement
  only). The `tsvector` column, its GIN index and `parser_fingerprint` came
  with `0003`; previews are blobs referenced from the extraction (no table).
  Tests: `test_0004_backfills_search_vectors_of_earlier_documents`,
  `test_downgrade_0004_leaves_phase2_and_upgrades_again`,
  `test_phase34_tables_refuse_other_households_on_migrated_database`.
- `tests/integration/test_phase3_4_end_to_end.py`: through the real app and
  the real worker (sandbox, Tesseract), a text-layer PDF statement reaches
  the ledger; the same layout scanned is OCR'd into the ledger or held in
  review; a receipt photo is linked to its card transaction; a drop-folder
  file is ingested and moved; a mailbox message is accepted and a spoofed
  one digested; `GET /documents?q=` finds the statement; `perfin ingest
  --dry-run` prints every stage.
- Generated client types: `GET /review/{id}/rule-draft` keeps a single
  `RuleCreate` schema (its model is generated in validation mode, so
  OpenAPI no longer splits `RuleCreate-Input`/`-Output`, which broke the
  rules builder); region boxes (`bbox`) are four-number lists rather than
  tuples (openapi-fetch dropped the tuple type, so the review client types
  disagreed with themselves); the upload page lists `DocumentListItem`
  (formerly `DocumentOut`).
- Docs: the Phase 2 "structured files only" limits in ARCHITECTURE.md,
  API.md, INGESTION.md and the README status are replaced by the Phases 3-4
  state; ROADMAP boxes ticked where the work is done and tested.

#### Phases 3-4: review tools (previews, regions, corrections, remember this layout)

- `GET /documents/{id}/preview?page=`: the sandbox-rendered page PNG,
  decrypted and checked again (plain PNG chunks only) before it is sent
  inline as `image/png` with `nosniff`, a `sandbox` CSP and `no-store`;
  download rule (I1), never from quarantine, audited (`document.preview`).
  `GET /documents/{id}/regions?page=`: word boxes for highlighting; the
  words only for callers the download rule allows (SECURITY.md C5, I1).
- `GET /review/{id}` adds `regions` (where a doubtful row or an account
  number was read) and `alternatives` (other readings on the row's line,
  ranked accounts, candidate transactions, institutions).
- `low_confidence_field` items can be resolved: rows corrected and the held
  rows confirmed (a failed reconciliation: the whole statement on that
  account). `POST /review/{id}/correct` corrects the document's
  institution, account, period, tax year and kind and its rows' values.
- `POST /review/{id}/remember-layout`: a household parser fingerprint
  derived from the header text and table layout (or given), validated and
  required to match its source document; similar pending documents are
  reprocessed. `GET /review/{id}/rule-draft`: a pre-filled rule for `POST
  /rules`.
- PDF/image page tables no parser read go through the Phase 2 column
  mapping (preview, mapping editor, importer); a mapping saved from one is
  reused for the next PDF of the same layout.

#### Phases 3-4: search, dry-run CLI, XLSX, antivirus and upload fuzz corpus

- Full-text search: `document.extracted_text` (`tsvector`, `simple` config,
  GIN index) is written by the worker after every extraction and cleared on
  failure (`app/domain/documents/search.py`); `GET /documents?q=` matches with
  `websearch_to_tsquery` on a bound parameter, ranks by `ts_rank_cd`, pages
  on a `<score>.<id>` cursor, and returns `rank` plus plain-text snippet
  segments (`{"text", "match"}`, never markup) only to callers who may
  download the document (I1); for other viewers `q` matches only the
  original filename (P34-SR-02 below).
- `perfin ingest --dry-run FILE [--household ID]` (`app/ingestion/dryrun.py`):
  receive (sniff and the upload's refusals), scan, extract through the real
  sandbox, classify, parse and, with a household, a read-only account match;
  one JSON document per stage; no blob or database write.
- XLSX extractor (`app/ingestion/extract/xlsx.py`, sandbox only): ZIP
  central-directory caps before openpyxl, DTD/entity refusal in every XML
  part, defusedxml required, `read_only`/`data_only` (formulas never
  evaluated), sheet/row/column/cell caps, formula neutralisation (C6, C7);
  worksheets import like CSV through the column mapping.
- Optional ClamAV (`app/integrations/antivirus/`, compose profile `av`):
  clamd `INSTREAM` with size cap and timeouts as a scan stage before
  extraction; positive -> `rejected` with the signature; fail closed by
  default (`PERFIN_ANTIVIRUS_FAIL_MODE`), checked by `perfin config check`
  (C9). Tested against a fake clamd.
- Upload fuzz corpus (`backend/tests/security/fuzz/`): about 55 generated
  hostile files (malformed PDFs, flat and nested zip bombs, polyglots,
  XXE/billion laughs in OFX/XML/XLSX, pixel bombs, malformed MIME, foreign
  archives and executables) through intake, the sandbox and the worker
  handler, plus a time-boxed hypothesis mutation fuzz of the sandbox entry
  point.

#### Phase 4: email intake

- Email intake (Phase 4, INGESTION.md "Email intake specifics", SECURITY.md
  D1, D2, D5, C10): `poll_mailboxes`/`poll_mailbox` worker jobs poll each
  household's receipts mailbox over IMAPS (`imaplib.IMAP4_SSL`, verified
  certificate, no STARTTLS; through the egress proxy as a `CONNECT` tunnel
  when `HTTPS_PROXY` is set, otherwise with every resolved address vetted).
  Acceptance: the household's secret alias token in `To`/`Cc`/`Delivered-To`/
  `X-Original-To` (constant-time compare), exactly one allowed `From`
  (members' own addresses plus `allowed_senders` mapped to members), and
  `spf=pass` plus an aligned `dkim=pass` in the topmost (or `authserv_id`'s)
  `Authentication-Results` when present. The worker reads only the header
  block; accepted mail becomes an `.eml` document (`source=email`, sender,
  message id, subject and auth results in `source_meta`) whose body and
  attachments the sandboxed EML extractor handles; rejected mail is never
  fetched, goes to `PerFin/Rejected` and is counted in one `rejected_email`
  digest item per day. Messages move only after their decision committed
  (`mail_intake_message` ledger, also the `Message-ID` dedupe); per-run
  caps. New tables `mail_intake` (credentials and alias column-encrypted)
  and `mail_intake_message`; settings `PERFIN_EMAIL_INTAKE_MAX_MESSAGES_PER_RUN`,
  `_MAX_MB_PER_RUN`, `_MAX_MESSAGE_MB`, `_TIMEOUT_SECONDS`,
  `_ALLOW_PRIVATE_HOSTS`. API: `GET/PATCH /settings/intake` (owner/admin
  session; `PATCH` step-up), `POST /settings/intake/email/test`, `POST
  /settings/intake/email/rotate-alias` (step-up).

#### Phase 3: drop folder

- `watch_dropfolder` (`app/workers/handlers/dropfolder.py`,
  `app/ingestion/intake/dropfolder.py`): a recurring worker job (one bounded
  run a minute, one at a time) that watches the inbox with inotify (a small
  `ctypes` binding; polling fallback), takes files once settled (`mtime` and
  `ctime` stable for `PERFIN_DROPFOLDER_SETTLE_SECONDS`, not open for writing
  where a read lease can tell, optional `.ready` sidecar), ignores hidden and
  partial-download names, never follows symlinks (directories and files
  opened by descriptor with `O_NOFOLLOW`; FIFOs, devices and hard links
  refused) and copies each file into quarantine through the upload intake
  (sniff, hash, size cap, encryption), discarding a copy whose source changed
  meanwhile (SECURITY.md C3, C11). Documents get `source = dropfolder` and
  the relative path, folder, size and `mtime` as metadata.
- Subfolder attribution (`app/domain/dropfolder/`): `inbox/<member>/<account>/`
  maps to a member by username and one of their editable accounts by name,
  or by a remembered `dropfolder_route`; anything else is held in
  `needs_review` behind an `unknown_owner` item ("Who does this belong
  to?"), resolved with `POST /review/{id}/resolve` `{"kind":
  "unknown_owner", "user_id", "account_id"?, "remember"}`
  (`next_step = choose_owner`).
- After-modes `move` (into `processed/`, refused files into
  `processed/failed/` or `PERFIN_DROPFOLDER_FAILED`, under generated names),
  `delete` and `keep`, applied only after the run committed
  (`dropfolder.finish`, or the next run after a crash); a read-only inbox
  acts like `keep`. `dropfolder_entry` is the idempotency ledger (path hash,
  size, `mtime`, inode; content hash in `keep` mode), so nothing is taken
  twice across restarts. Per-run caps on files and bytes (C10).
- Settings `PERFIN_DROPFOLDER_ENABLED`, `_FAILED`, `_READY_SIDECAR`,
  `_MAX_FILES_PER_RUN`, `_MAX_MB_PER_RUN`, `_WATCH_SECONDS`,
  `_HOUSEHOLD_ID`; `perfin config check` drop-folder findings
  (`dropfolder.output_in_inbox` error; `inbox_read_only`, `inbox_writable`,
  `inbox_missing`, `processed_not_writable`, `settle_short` warnings).
  `deploy/.env.example` enables the drop folder with `keep`, matching the
  read-only inbox mount.

#### Phases 3-4: receipts (parsing and transaction matching)

- `app/ingestion/parsers/receipt.py` (`receipt_basic`): reads receipts in
  the worker from the stored extraction of thermal-print photos (OCR in the
  sandbox), e-receipt PDFs and receipt mail bodies: merchant, date (and
  time), subtotal/tax/tip/discount/total with a consistency check, best
  effort line items, payment brand and card last four (identity data:
  stored, never logged, audited or put in a review payload), currency, and
  per-field confidence. OCR label folding (`T0TAL`, `AMOUNTDUE`, `TOTAI`);
  a lost decimal point or a currency symbol read as a digit is believed
  only when it makes the figures consistent.
- `app/ingestion/match/receipts.py`: receipt -> transaction matching on
  amount (exact, refund, tip up to 30%), date within 3 days and merchant
  similarity (payee name and aliases first). A confident unique match is
  linked (`document_link`, audit `document.link` with `auto: true`);
  otherwise a `receipt_match` review item with up to five ranked
  suggestions, limited to accounts the uploader may edit (and to the
  receipt's own account when it was filed under one). Resolving it
  (`{"kind": "receipt_match", "transaction_id"}`) links the chosen
  transaction; later imports re-match open items. Review UI: "Attach this
  receipt" with the suggestions.
- `tests/fixtures/synth_receipts.py`: Pillow-rendered thermal receipts
  (58/80 mm, faded ink, noise, specks, blur, skew), reportlab e-receipt
  PDFs and HTML receipt mails, from tuning and held-out seeds; field
  accuracy on the held-out set is in INGESTION.md ("Receipts") and
  enforced by `tests/unit/test_receipt_accuracy.py`.

#### Phases 3-4: statement parsers, reconciliation and confidence

- `app/ingestion/parsers/`: document parsers running in the worker on the
  stored PDF/image pages (never on file bytes): a generic table statement
  parser (header-defined columns over words and boxes, multi-page tables,
  wrapped lines, pending sections left out, fee/interest/dividend rows,
  signed, debit/credit and card `CR` conventions, yearless dates, period,
  last four digits, currency, labelled and in-table balances), a generic
  card variant, institution parsers for the fictional institutions of the
  shipped fingerprints (Sample National Bank, Example Federal Credit Union,
  Demo Card Services, Mock Savings and Loan) and a basic payslip parser.
  Chosen by the classifier's fingerprint hint, else by detection score;
  versioned; output kept in `document.extraction` (`statements` with
  `source_format` `pdf`/`image`, `parse`), so re-imports and `reparse` need
  no new upload or extraction.
- `app/ingestion/reconcile.py`: opening + transactions = closing (less than
  one cent) and running-balance checks (SECURITY.md C12).
- Per-field confidence (`PERFIN_INGEST_CONFIDENCE_THRESHOLD`, default 0.85):
  rows below it, and every row of a statement that does not reconcile, are
  imported `review_state = pending` with `transaction.confidence` and
  `low_confidence_field` items (`low_confidence`, `reconciliation_failed`/
  `reconciliation_unavailable` with the figures, `lines_skipped`); an
  unreconciled statement writes no balance snapshot. Payslips are kept on
  the document (`kind = payslip`) and go to review when they do not add up.
- The sandbox runner refuses a child answer carrying parser output
  (`bad_output`).
- `tests/fixtures/synth_pdf_statements.py` (reportlab layouts of every
  institution, multi-page, pending items, fees/interest, debit/credit,
  tampered non-reconciling variants, an unknown bank, a payslip);
  `docs/CONTRIBUTING-parsers.md`.

#### Phases 3-4: classification (kind, institution, account, period)

- `app/ingestion/classify/`: fingerprints (shipped YAML for six fictional
  institutions plus the household's `parser_fingerprint` rows from
  "remember this layout"; strict schema, anchors/aliases refused, ReDoS-safe
  regex subset compiled with possessive quantifiers), then a pure-Python
  naive Bayes kind classifier (statement, receipt, invoice, tax form,
  payslip, insurance, other) shipped as a versioned JSON artefact trained by
  `scripts/train_classifier.py` on synthetic documents
  (`backend/tests/fixtures/synth_classify.py`), then review. Period
  detection from labelled periods in many date formats, else inferred from
  transaction dates; receipts' printed date; tax forms' tax year; last four
  digits and account matching by last four and institution within the
  uploader's editable accounts; per-field confidence in
  `document.confidence`. The pipeline classifies PDFs, images and text with
  it.

#### Phases 3-4: document extraction (PDF, images, OCR, EML, ZIP)

- Sandbox extractors for PDF (PyMuPDF: words with boxes, lines, tables from
  rulings and aligned columns, metadata; JavaScript, launch actions,
  embedded files and links counted and stripped, C8; password-protected
  PDFs to review), PNG/JPEG/TIFF/HEIC (format-restricted decoders, pixel
  caps before decoding, EXIF dropped), OCR (Tesseract in process through
  `tesserocr`, so the child still cannot `execve`; ADR-0009 addendum;
  grayscale, upscaling, denoise, deskew, Otsu binarisation, a receipt
  profile, per-page and per-document time caps; OCR fallback for PDF pages
  with no, garbage or sparse text), EML (stdlib `email` with
  `policy.default`, strict HTML-to-text converter without fetches, D3;
  attachments and `cid:` images as child documents) and ZIP (central
  directory checks against bombs, names as metadata, depth 1, D4). Page
  previews are rendered in the sandbox (bounded DPI and pixels, plain PNG)
  and stored by the worker as encrypted blobs referenced from
  `document.extraction.stored_previews`; children of mails and archives go
  through the upload intake as documents of their own with their depth.
  `pytesseract` was replaced by `tesserocr`. Synthetic fixtures:
  `backend/tests/fixtures/synth_documents.py`.
- `.gitignore`: `backend/app/domain/documents/` was matched by the
  `documents/` data pattern and therefore never committed; it is now
  explicitly included.
- Security review round 1 fixes (Phases 3-4). P34-SR-01 (high, C12/C10):
  fingerprint regexes now compile every alternation atomic (`(?>...)`) as
  well as every quantifier possessive; `(?:a|a)` repeated 36 times then `b`
  passed the old subset and backtracked 2^36 times in the API thread of
  "remember this layout" and in the worker
  (`test_alternation_cannot_backtrack_exponentially`). P34-SR-02 (medium,
  I1): a viewer who may not download documents searched their extracted
  text through `GET /documents?q=` (a yes/no oracle for account numbers or
  tax ids); their `q` now matches only `original_filename`. P34-SR-03 (low,
  D1): a `dkim=pass` must name its signer, and that signer must be the
  `From` domain or a parent of it. P34-SR-04 (low, I1): `GET
  /documents/{id}/regions` returns no word boxes to callers who may not
  download.
- Security review round 2 fixes (Phases 3-4). P34-SR-R2-01 (medium,
  C10/F8/C12): fingerprint matching had no total work budget: a safe
  pattern still costs up to pattern x line^2 per line, so 32 `any` rules
  of `[a ]*` x 12 then `!` over 4000 lines of 300 characters took 64 s for
  one `match_fingerprint` call, in the API thread of "remember this layout"
  (learning check, `best_match`, the scan of up to 500 similar documents)
  and in the worker, and remembered versions were unlimited. Now household
  fingerprints' pattern rules run under a wall-clock `MatchBudget` checked
  before every line (0.25 s shared by all household fingerprints of a
  document; out of budget = no match), the similar-document scan shares
  2 s, the learning check answers `422 layout-too-costly`, a household
  remembers at most 100 layouts (`409 fingerprint-limit`) and only the
  latest version of each is matched (`tests/security/test_fingerprint_budget.py`,
  `tests/integration/test_fingerprint_limits.py`). P34-SR-R2-02 (low, D1):
  a rejected message is recorded under a hash of its whole header block
  instead of `Message-ID` plus `From`, so a rejected forgery can no longer
  send a later legitimate message with the same `Message-ID` to the
  rejected folder (`test_rejected_forgery_does_not_shadow_the_real_message`).

#### Phase 2: Ledger (v0.2)

- Security review round 1 fixes. P2-SR-01 (high, C1): the extraction child
  could read the worker's environment and memory through `/proc` and change
  files of the shared UID 10001. The worker is now non-dumpable
  (`protect_parent`, at `perfin worker` start-up and before every spawn),
  the child confines itself with Landlock (read-only access to the
  interpreter's paths and its own `/proc/<pid>`; refuses to run without
  Landlock unless `PERFIN_EXTRACT_REQUIRE_LANDLOCK=false`), and seccomp
  denies every filesystem-changing call and `open` for writing (ADR-0009,
  amending ADR-0006). P2-SR-02 (F3): the after-import transfer matcher only
  pairs accounts the uploader may import into, and unlinking or deleting a
  transfer leg needs edit on every leg (`409 transfer-conflict`). P2-SR-03
  (I2): transfer, rules, goal and document-deletion events name their
  accounts, and the stream drops account-derived events that name none for
  members and viewers. P2-SR-04 (C4): text uploads are scanned for active
  markup over the whole stream, not only the first 8 KiB. P2-SR-05: a
  user's stored `Idempotency-Key` answers are dropped when their grants or
  role change. Setting `PERFIN_EXTRACT_REQUIRE_LANDLOCK` (default `true`).
- Security review round 2 fixes. P2-SR-R2-01 (medium, C1): seccomp now
  limits `rt_sigqueueinfo` and `rt_tgsigqueueinfo` to the child's own pid,
  like `kill` and `tgkill`, and denies `fcntl` `F_SETOWN`, `F_SETOWN_EX`
  and `F_SETSIG` (SIGIO file ownership on the child's pipes, found while
  fixing this). Without Landlock signal scoping (ABI 6+, Linux 6.12+) a
  parser exploit could otherwise stop or kill the worker and sibling
  children; the regression test runs with Landlock capped at ABI 4 and
  without Landlock. P2-SR-R2-02 (low, C1): the worker healthcheck runs under
  `env -i`, so the dumpable same-UID probe no longer carries the env_file
  secrets, and ADR-0009 names the remaining opt-out exposure (`docker exec`
  into the worker).
- Phase 2 finalize: the whole ledger path was run live against `perfin api`
  and `perfin worker` (dev mode, real PostgreSQL 16, database freshly
  migrated to `0003`, built SPA) with synthetic data only: first-run setup,
  TOTP enrolment, login, accounts (checking matched by last four digits,
  a manual asset with a balance snapshot and history), an OFX upload
  imported by the worker through the sandbox while a live `/events` stream
  delivered `document.*`, `transaction.created` and `balance.updated`
  (ids and types only), a CSV upload mapped through `/review` (exact
  duplicates skipped, a respelled row held as a near duplicate and
  confirmed), a QIF upload with a split, a rule dry run (`/rules/test`,
  nothing written) then create and apply to existing rows (exactly the
  dry-run matches changed), a monthly budget report whose actual equals the
  categorised spending, the cash-flow report, an encrypted full export
  (step-up, SHA-256 checked, second download `410`) imported into a second
  household (same ledger, categories, rules, budget and documents;
  re-uploading the statement adds nothing; household A unchanged; B's
  account `404` from A). In headless Chrome every SPA page rendered live
  data without console or CSP errors, a transaction created over the API
  appeared in the open grid through SSE without a reload, and a CSV was
  mapped and a duplicate confirmed through the UI.
- Frontend ledger screens (Phase 2): accounts list grouped by type with
  balances, create/edit/close/reopen and a balance-history chart with
  manual balances; the transactions grid with cursor paging, filters,
  inline edit (category, payee, tags, notes) with rollback on refusal,
  exact-decimal split editor, transfer link/unlink and bulk actions;
  document upload with progress and live status; the CSV column-mapping
  editor (starts from the server's suggestion, saves a mapping and applies
  it); the review queue (duplicate side by side with its candidates,
  unknown-account picker with "remember", unparsed items to the mapping
  editor, dismiss). Viewers and view-only accounts get a read-only UI.
- Frontend planning screens (Phase 2): a dashboard at `/` (this month's cash
  flow, spending by category, budget status, recent transactions, review
  count, account totals; each card loads on its own), budgets with a period
  report (budgeted, carried over, actual, forecast, remaining, status) and a
  line editor, goals with progress and projection and a contribution history,
  rules (ordered list with move up/down, condition and action builder that
  matches the backend rule schema, a dry run showing matches and the diff
  before saving, and apply-to-existing with job progress), category hierarchy,
  payee (merge, aliases) and tag management under Settings, plus Settings pages
  for the household base currency, per-member account access (step-up) and
  full data export (start, progress, one-time download) and import, each
  behind step-up. The old placeholder home page is replaced by the dashboard.
- Phase 2 integration: Alembic revision `0003` (`0003_phase2_ledger`) creates
  the 27 Phase 2 tables (ledger, documents and ingestion, imports,
  `idempotency_key`, `portability_archive`), each with forced RLS
  (`household_isolation`) and DML grants for the application role, and the
  20 enum types they use; adds `account_grant (household_id, account_id) ->
  account` `ON DELETE CASCADE` (orphan Phase 1 grants are deleted first) and
  the `source_document_id -> document` keys of `transaction` and
  `balance_snapshot` (the latter is new: `SET NULL` plus the deferred
  household-consistent key). The downgrade removes exactly those tables,
  types and the grant key. `test_migrations.py` covers 0003 (drift, RLS,
  grants, orphan grants, downgrade to 0002, cross-household references on
  the migrated schema). `tests/integration/test_phase2_end_to_end.py` runs
  OFX and CSV uploads through `POST /documents`, the real worker loop, the
  sandbox and the importer, then `/transactions` (rules applied), `/review`
  (column mapping, near duplicate confirmed), reports with and without
  pending rows, and `/events` live and resumed.
- Backend tests run in parallel with pytest-xdist (`uv run pytest -n auto`
  in `make check`, `make backend-test` and CI). Each worker builds its own
  per-session database; random parametrize values now carry fixed ids so
  every worker collects the same tests.
- Manual transactions (`POST /transactions`) now run through the
  household's rules (`apply_rules_to`); category, payee, notes and tags
  given in the request are protected.
- The structured importer's deduplication reads `import_hash_alias`: a
  statement line confirmed as a duplicate of a transaction that had its own
  hash now matches that transaction exactly on the next import instead of
  raising the same review question again.
- `GET /jobs/{id}` accepts tokens with `documents:read` for
  `imports.apply_mapping` jobs.
- SPA live updates: `rule.*`, `goal.*`, `category.*`, `payee.*`, `tag.*`,
  `account_grant.*`, `member.*` and `household.*` events invalidate their
  query families (new roots `goals`, `reports`, `rules`, `taxonomy`);
  `transaction.*` also refreshes goals and reports.
- Full export and import (Phase 2, docs/PORTABILITY.md): `POST /export`
  (owner/admin browser session with step-up; optional passphrase),
  `GET /export/{job_id}`, `GET /export/{job_id}/download` (once, within
  `PERFIN_EXPORT_TTL_MINUTES`; the stored archive's key is destroyed before
  the first byte is sent), `POST /import` (streamed multipart upload, at most
  `PERFIN_IMPORT_MAX_MB`) and `GET /import/{job_id}`; jobs
  `portability.export`, `portability.import`, `portability.expire`. The
  archive is a zip of `data/<entity>.json` for every ledger table,
  formula-neutralised `csv/transactions.csv` and `csv/accounts.csv` (C7),
  the decrypted document originals and a manifest with the SHA-256 of every
  member; it is built as a stream straight into an encrypted blob (G1).
  `app.core.archive_crypto`: "PerFin encrypted archive" v1 (libsodium
  secretstream under an Argon2id passphrase key; G3, reusable for backups).
  Import validates the zip (C2/C3: names, links, member count, sizes,
  compression ratio, overlaps), the manifest version and hashes and every
  row, then applies all or nothing into the current household with new ids,
  merging institutions, categories, payees and tags and recomputing
  `import_hash` for the new accounts. New table `portability_archive`
  (created by revision `0003`).
- Documents API, review queue API and live updates (Phase 2):
  `POST /documents` streams the multipart body straight into the encrypted
  quarantine area (no spooled plaintext; `Idempotency-Key` fingerprinted
  with the content hash; account must be editable); `GET /documents`
  (filters `kind`, `status`, `account`, `tax_year`; cursor),
  `GET /documents/{id}` (metadata only), `GET /documents/{id}/download`
  (member or above, viewers only with the household setting
  `documents.viewer_download`; never from quarantine; attachment with the
  sniffed type, RFC 5987 name, `nosniff`, `sandbox` CSP), `POST
  /documents/{id}/reprocess`, `POST /documents/{id}/link` (transaction or
  account), `DELETE /documents/{id}` (crypto-shredding).
  `GET /review`, `GET /review/{id}`, `POST /review/{id}/resolve` with typed
  resolutions (`possible_duplicate` confirm/reject recording the duplicate's
  `import_hash` on the kept transaction or in the new `import_hash_alias`
  table; `unknown_account` assign with "remember" (last four digits or the
  CSV mapping's account) and re-import; `unparsed` reprocess or apply a
  column mapping) and `POST /review/{id}/dismiss`. `GET /events`
  (Server-Sent Events for browser sessions): `LISTEN perfin_events` per
  stream, household-scoped, filtered to visible accounts and documents,
  ids and types only, `Last-Event-ID` resume within retention, heartbeats,
  session re-check, per-user stream cap (`PERFIN_SSE_*` settings). Authz
  matrix kinds `stored_document` and `review_item`; controls C4 and I1
  implemented.

- Structured imports (Phase 2, `app.domain.imports`,
  `app.ingestion.parsers.structured`, `app.ingestion.match`): the ingest
  worker now turns extracted OFX/QFX/QIF statements and CSV/TSV tables into
  transactions in the job's transaction. OFX/QFX accounts are matched by the
  last four digits plus the institution (`FI/ORG`, new optional
  `StatementAccount.institution` in the sandbox protocol), only among
  accounts the uploader may edit; CSV/TSV layouts by a header fingerprint
  against saved column mappings (`import_mapping`: date column and format,
  signed amount with a sign convention, amount plus DR/CR indicator, or
  debit/credit columns, payee, memo, running balance, currency, decimal
  comma, preamble rows). Dedupe by `import_hash` (re-imports and overlapping
  statements skip known rows); near matches (same date and amount) are
  imported as pending with a `possible_duplicate` review item. Rules run on
  new rows (QIF categories protected, QIF splits kept), OFX `LEDGERBAL` / the
  last CSV running balance becomes a `statement` balance snapshot, the CSV
  running balance is reconciled, and the transfer matcher is queued.
  Problems become review items and leave the document `needs_review`. API:
  `GET/POST /imports/mappings`, `GET/PATCH/DELETE /imports/mappings/{id}`,
  `GET /imports/{document_id}/preview` (stored sandbox output, never
  re-parsed, plus the matching mapping and a suggested one) and `POST
  /imports/{document_id}/apply-mapping` (`imports.apply_mapping` job: re-run
  from the stored extraction with a chosen mapping and/or account). New
  authz matrix kinds `document`, `import_mapping`. Synthetic statement
  generator `tests/fixtures/synth_statements.py` (several bank-like CSV
  layouts, OFX SGML/XML, QFX, QIF for invented institutions), also
  `scripts/synth/statements.py`.
- Budgets, goals and reports (Phase 2). `/budgets`: monthly, annual
  (twelve months from any first-of-month start) and custom budgets with
  lines per expense or income category, rollover of what is left (or
  overspent) per period, and `GET /budgets/{id}/report?period=` with
  budgeted, carried over, available, actual, remaining and forecast per
  line (forecast: linear projection of the period so far, without lines
  matching an active schedule, plus scheduled occurrences until the period
  end), unbudgeted spending and totals, converted into the budget's
  currency per posting day. `/goals`: savings goals over accounts with a
  target amount and date, manual or transaction-linked contributions
  (withdrawals negative), progress, accounts balance, observed monthly rate,
  projected completion and on-track flag; a goal is visible only to callers
  who can see all of its accounts. `/reports/spending|income` (group by
  category with depth roll-up, payee, month or account), `/reports/trends`
  (month-on-month and year-on-year per category, monthly series) and
  `/reports/cashflow` (monthly in, out, net). All amounts come from the
  caller's visible accounts only, read splits, leave out transfers and
  pending review rows by default, are aggregated in SQL and bounded (F8).
  New authz matrix kinds `budget`, `goal`, `goal_contribution`.
- Rules engine (Phase 2, `app.domain.rules`): ordered rules with
  `stop_processing` (new `rule` column) whose conditions (payee/memo text
  with `contains|equals|starts_with|ends_with|glob`, canonical payee,
  amount range, sign, account, currency, date range, category is/unset,
  `all|any|not`) and actions (set category, split by percentages or
  amounts, add tags, rename to a canonical payee, flag for review, set
  note) are Pydantic discriminated unions stored as JSONB. A pure,
  deterministic engine (first matching rule wins per field, tags
  accumulate, later rules see earlier changes). No regular expressions:
  `glob` is matched by a bounded iterative matcher (no ReDoS). API: `GET/
  POST /rules`, `GET/PATCH/DELETE /rules/{id}`, `POST /rules/reorder`,
  `POST /rules/test` (paginated dry run over the caller's visible history
  in a `READ ONLY` transaction), `POST /rules/apply` (`run_rules` job,
  `202` with the job id), `GET /rules/export` and `POST /rules/import`
  (portable: ids resolved by name in another household). Changing rules
  needs owner/admin; account ids hidden from a reader are redacted. Hook
  for new transactions: `app.domain.rules.service.apply_rules_to(session,
  household_id, transactions, protect=...)`; hit counts and `last_hit_at`.
- `GET /jobs/{id}` (`app.domain.jobs`): state of a job of the active
  household (no payload or error text); tokens need the job family's read
  scope.
- Accounts API (Phase 2): `GET/POST /accounts`, `GET/PATCH/DELETE
  /accounts/{id}`, `POST /accounts/{id}/close|reopen`, `GET/POST
  /accounts/{id}/balances` (daily history, manual balance). Lists and
  object routes apply account visibility (hidden accounts are `404`,
  view-only `403`); a member who creates an account gets an `edit` grant.
  Account grant endpoints: `GET /households/{id}/members/{user_id}/grants`,
  `PUT/DELETE .../grants/{account_id}` (owner/admin, step-up), `GET
  /accounts/{id}/grants`.
- Categories, payees and tags API (`app.domain.taxonomy`): CRUD with cursor
  pagination; category tree rules (depth 3, no cycles, kinds follow the
  parent), category and payee merge with reassignment of transactions,
  splits, schedules, payee defaults and budget lines; payee aliases unique
  across payees.
- `Idempotency-Key` on creating `POST`s (`app.core.idempotency`, table
  `idempotency_key`): replay of the first answer for 24 hours, `422` when a
  key is reused for another request, concurrent duplicates wait for the
  first; used by accounts, balances, categories, payees and tags.
- Ledger core (Phase 2): household-scoped models with forced RLS for
  `institution`, `account` (every VISION.md type, `manual_asset` kinds,
  `number_last4` as identity data, `is_asset`, `manual_balance`),
  `balance_snapshot`, `transaction`, `transaction_split`, `payee`,
  `category` (tree, kind, tax treatment), `tag`/`transaction_tag`, `rule`,
  `scheduled_transaction`, `fx_rate`, `budget`/`budget_line`,
  `goal`/`goal_account`/`goal_contribution`. Every reference between
  household-scoped tables includes `household_id` (`app.db.types`), so the
  database refuses cross-household links (F3); a transaction's currency is
  its account's. `account_grant.account_id` now references `account`.
- Shared ledger services: account visibility (`app.domain.accounts.visibility`:
  role plus `account_grant`, `404 account-not-found` for hidden accounts),
  `import_hash` with documented payee normalisation, the default category
  set seeded for every new household (setup and `POST /households`), FX
  conversion to the base currency (latest rate on or before the date,
  half-even), balances from snapshots and transactions,
  `app.core.money` (Decimal parsing, half-even rounding, `{amount,
  currency}` API shapes), test factories in `tests/fixtures/ledger.py`.
- Transactions API (Phase 2): `GET /transactions` with explicit filters
  (account, date range, category including splits, uncategorized, payee,
  tag, review state, status, transfer, and `q` as an escaped `ILIKE`
  substring; no query language) and a keyset cursor (max 500, stable while
  rows are added); `POST` (manual, `Idempotency-Key`), `GET/PATCH/DELETE
  /transactions/{id}`, `POST /transactions/{id}/split` (lines must add up
  exactly, in the transaction's currency), `POST /transactions/bulk`
  (categorise/tag/untag/delete/confirm, each id authorised alone). Account
  grants apply everywhere (hidden, foreign or missing transactions are `404
  transaction-not-found`; F3); every write is audited without free text and
  emits a `transaction.*` event; deleting keeps the audit row. Transfers
  (`app.domain.transactions.transfers`): a conservative matcher (different
  accounts, same currency, equal and opposite amounts, closest dates within
  a window, ambiguous legs left alone), manual link/unlink and candidates,
  `POST /transactions/transfers/auto-pair`, and a `transactions.auto_pair`
  job enqueued after each import. Reports exclude `review_state=pending`
  unless the household setting `reports.include_pending_review` is set
  (`reportable_clause`). The authz matrix gained the `transaction` object
  kind.
- Document store and extraction sandbox (Phase 2): household-scoped `blob`,
  `document`, `document_link`, `review_item`, `parser_fingerprint` and
  `ingest_job` with forced RLS; `transaction.source_document_id` references
  `document`. `LocalFSBlobStore` (random UUIDv4 keys, two-level fan-out,
  `0600` files, atomic writes, quarantine and permanent areas, promotion
  without re-encryption; `S3BlobStore` is an interface that refuses to run)
  with streamed per-object envelope encryption (secretstream
  XChaCha20-Poly1305, DEK wrapped by the master key, bound to the blob id;
  `Keyring.new_data_key`/`unwrap_data_key`/`rewrap_data_key`) and
  crypto-shredding on delete (G1, G5). Upload intake
  (`app.ingestion.intake.upload`): streaming size cap, magic-byte sniffing
  (SVG/HTML/polyglots refused, C4/C5), SHA-256, encrypted quarantine write,
  `document(status=received)` and the `ingest_document` job in one
  transaction, duplicate content to the review queue. Sandboxed extraction
  (`app.ingestion.sandbox`, ADR-0006, C1/C2): child process with empty
  environment, private working directory, rlimits, no-new-privs and a
  seccomp deny list, parent-side timeout and output cap, strict JSON
  contract; CSV/TSV (dialect and encoding detection, formula neutralisation,
  C7), OFX/QFX SGML and XML (`defusedxml`, no DTDs or entities, C6) and QIF
  extractors. Pipeline skeleton (`app.ingestion.pipeline`) with stage
  history and a hook for the structured importer; `ingest_document` worker
  handler. Settings `PERFIN_DOCUMENTS_DIR`, `PERFIN_QUARANTINE_DIR`,
  `PERFIN_EXTRACT_MEMORY_MB`, `PERFIN_EXTRACT_CPU_SECONDS`,
  `PERFIN_EXTRACT_TIMEOUT_SECONDS`, `PERFIN_EXTRACT_MAX_OUTPUT_MB`,
  `PERFIN_EXTRACT_UID`.

#### Phase 0: Scaffold

- Licence: GNU AGPL-3.0-or-later (`LICENSE`), SPDX header on every source
  file enforced by `scripts/check_spdx.py`.
- Backend scaffold: `perfin` package (`app`), hash-locked `uv.lock` with the
  runtime dependencies for the whole roadmap, Ruff / mypy `--strict` / Bandit /
  pytest configuration.
- `create_app(settings)` with router auto-discovery from `app/api/routers`
  (`router` under `/api/v1`, optional `root_router` at the root); OpenAPI and
  docs only in dev mode.
- `perfin` CLI: `api`, `migrate`, `openapi`, `healthcheck`, `config check`
  (settings validation), placeholders for later-phase commands.
- `Settings` covering every `PERFIN_*` variable in `deploy/.env.example`,
  secrets as `SecretStr`, master key from `PERFIN_MASTER_KEY` or
  `PERFIN_MASTER_KEY_FILE`.
- RFC 9562 UUIDv7 generator, monotonic within a process.
- Database base: `Base`, `UUIDPk`, `Timestamps`, `HouseholdScoped` (RLS
  registry), minimal `household` table, engine/session factories with a
  statement timeout, RLS DDL and least-privilege grant generation,
  transaction-local `set_context`, model registry, Alembic environment.
- Test fixtures: a unique database and a non-owner, non-superuser login role
  per pytest session so row-level security is really exercised.
- Frontend scaffold: Vite + React 18 + TypeScript (strict,
  `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`), TanStack Router
  with an explicit feature list, ESLint strict-type-checked + security +
  no-unsanitized + react-hooks with `dangerouslySetInnerHTML`/`innerHTML`
  banned, Vitest + Testing Library, no inline assets.
- Root `Makefile` (`check`, `backend-check`, `frontend-check`, `audit`, `test`,
  `dev`, `openapi`) and `docs/security/CONTROLS.md`.

#### Phase 0: Foundations

- `perfin config check` (SECURITY.md H6): refuses placeholder or weak master
  and secret keys, world-readable key files, default database passwords,
  non-https public URL or OIDC issuer, insecure cookies, `DEBUG` logging,
  local-login lockout, any-address break-glass or trusted-proxy ranges, and
  MCP HTTP without explicit origins; warns on risky settings (private OIDC
  issuer, MFA off, Trusted Types off, no key-backup acknowledgement). `api`,
  `worker` and `mcp` run it at startup and refuse to boot on errors.
- Structured JSON logging with a PII/secret scrubber on every handler
  (accounts, cards, IBANs including masked forms, SSN/EIN, emails, phones,
  dates of birth, tokens, cookies, URL credentials, JWTs) and denylisted
  fields dropped (H8).
- Web hardening: strict CSP with Trusted Types, HSTS, `X-Frame-Options`,
  Referrer/Permissions policies, COOP/CORP, `Cache-Control: no-store`
  everywhere except hashed assets; CSRF middleware (`Sec-Fetch-Site`/`Origin`
  checks, HMAC session token hook, signed pre-auth double submit); RFC 9457
  problem-details errors with no stack traces; request ids (UUIDv7) and
  forwarded headers honoured only from `PERFIN_TRUSTED_PROXIES`; body-size
  limits (F1, F5-F9).
- `/healthz` and `/readyz` (database reachable and at the migration head)
  outside `/api/v1`, and Prometheus metrics on an internal port only (H7).
- The API serves the built SPA from `PERFIN_STATIC_DIR` (`/app/static` in the
  image) under the same CSP; client routes fall back to `index.html` without
  changing API 404s or 405s.
- Platform tables `household`, `job`, `event`, `audit_log`, `setting`; forced
  row-level security on every household-scoped table; `audit_log` append-only
  (grants plus triggers for every role) and SHA-256 hash-chained; least-privilege
  grants for the application role; `statement_timeout` and `lock_timeout` on
  every connection (F2, F3, F8, I4).
- Alembic revision `0001_phase0_foundations` (autogenerated tables plus the RLS,
  audit and grant DDL from `app.db.rls`, and `SELECT` on `alembic_version` for
  `/readyz`); `perfin migrate` runs it as the owner role.
- `perfin audit verify`: recomputes the audit hash chain as the owner role and
  prints the head hash.
- Postgres job queue (ADR-0002): `enqueue`/`dequeue` with `SKIP LOCKED`,
  priorities, leases and a reaper, retries with backoff, dead-lettering,
  per-household concurrency limit, idempotent `dedupe_key`; `perfin worker`
  with handler registry, job timeouts, graceful shutdown, a recurring-job
  scheduler and the `noop` handler (C10).
- `event` emission with `NOTIFY` for the future SSE stream, and pruning.
- Deployment: multi-stage image running as UID 10001 on a read-only root;
  hardened Compose (all capabilities dropped, no-new-privileges, tmpfs, limits,
  only the proxy publishes ports, isolated internal networks, one-shot
  `migrate`); `deploy/db-init` creates non-superuser `perfin_owner` and
  `perfin_app` roles with SCRAM passwords and restricts the bootstrap superuser
  to the local socket; Caddy example with TLS, HSTS, body limits and log
  filtering; CONNECT-only egress allowlist proxy (H1-H3, H5).
- CI and security workflows: SPDX, lockfile integrity, Ruff, mypy, Bandit,
  pytest on PostgreSQL 16, OpenAPI and generated-type drift, ESLint, `tsc`,
  Vitest, build, Compose/Caddy validation, image build and run test,
  gitleaks, pip-audit, npm audit, Trivy fs and image scans, CycloneDX SBOM,
  Dependabot (H4, H5, F10).
- Frontend shell: layout with navigation for every section, theme (system,
  light, dark), error boundary, design-system components, decimal-string money
  and timezone-safe date helpers, the typed API client with CSRF header and
  problem errors, and SSE-to-query invalidation.
- Tests: migrations on a fresh database (no drift, forced RLS, least
  privilege, append-only audit log, downgrade and re-upgrade), the
  `app/api`/`app/domain` -> `app/ingestion/extract` import boundary, and
  linear-time scrubber patterns.

#### Phase 1: Identity (v0.1)

- `perfin breakglass status|enable|disable` (B7): the operator flags the
  bootstrap instance admin as the break-glass account (audited
  `user.breakglass_set` with `via: cli`; ends every session of the account)
  and checks the account named by `PERFIN_BREAKGLASS_ADMIN_USERNAME` against
  the database (exists, active, flagged, instance admin, local password,
  second factor), which `perfin config check` cannot do. Replaces the
  interim SQL procedure in `docs/AUTHENTIK.md`.
- `PATCH /admin/users/{id}` takes `email_verified` (instance admin, step-up,
  audited `user.email_verified_set`, `409 no-email` without an address), and
  `GET /admin/users` returns it: an instance admin vouches for a local
  account's address so OIDC link-by-verified-email (B3) can complete for
  pre-created accounts.
- `docs/AUTHENTIK.md`: Authentik 2024.x/2025.x setup guide (provider,
  groups and role mapping, signing key, MFA and `amr`, logout, OIDC-only mode
  with the break-glass admin through an SSH tunnel, a LAN issuer and a
  private CA via `SSL_CERT_FILE`, optional forward-auth gate,
  troubleshooting by `oidc_error` code), resolved against the final code.
- `tests/security/test_input_models.py` (F4): every request body of every
  route, walked from the OpenAPI document, must be a named model that
  forbids unknown fields, recursively; exemptions are listed and checked for
  staleness.
- `tests/security/test_egress_private_ca.py`: a synthetic private CA is
  trusted only through `SSL_CERT_FILE`, and only for the names on the
  certificate.

- Web app identity pages (`frontend/src/features/auth`, `setup`, `settings`):
  sign-in with the local form and a "Sign in with Authentik" button (from
  `GET /auth/login/options` and `GET /auth/oidc/status`), the second-factor
  step (security key or passkey via `@simplewebauthn/browser`, authenticator
  code, recovery code), the enrolment-only flow of `PERFIN_REQUIRE_MFA`, the
  OIDC "link by verified email" page, first-run `/setup` and
  `/reset-password` (one-time tokens read from the URL fragment, removed from
  the address bar and sent only in the request body), and `/forgot-password`.
  Settings: profile; security (password change, authenticator app with a
  locally rendered QR code, security keys, recovery codes shown once,
  sessions, personal access tokens with a scope picker and a show-once
  token); household members by role. A reusable step-up dialog retries the
  original request after verification. A route guard sends anonymous
  visitors to `/login?return_to=` (relative paths only), pending logins to
  `/login/mfa` and enrolment-only sessions to `/login/enroll-mfa`. The
  header shows the household switcher and sign-out (ending OIDC sessions at
  the IdP when it offers an end-session URL).
- Identity core: `app_user`, `credential`, `mfa_method`, `identity`,
  `user_session`, `household_member`, `account_grant`, `api_token` and
  `rate_limit_event` models; the household-scoped ones under forced RLS with
  SELECT-only self-read policies (and a token-prefix lookup policy).
- `app.core.crypto`: XChaCha20-Poly1305 envelope encryption with a DEK per
  value, master-key version in the envelope, associated data binding each
  value to its table, column and row, and DEK rewrap for rotation (G1).
- Argon2id password hashing (64 MiB, t=3, p=4, parameters stored,
  rehash detection) and a full-cost dummy verification for unknown users
  (A1, A2).
- Server-side sessions: `__Host-perfin-session` cookie (`HttpOnly; Secure;
  SameSite=Lax`), SHA-256 at rest, idle/absolute/remember-device lifetimes,
  MFA-pending stage, rotation on MFA completion, step-up and household
  switch, list/revoke (A3, A4, A6); the CSRF token is now bound to the
  session (A5).
- Principal resolution from a session cookie or a `Bearer` personal access
  token (an `Authorization` header never falls back to the cookie, SR-8),
  setting the RLS context per request; guards `require_authenticated`,
  `require_role`, `require_instance_admin`, `require_scope`,
  `require_step_up`, `require_mfa_if_enforced` and account-grant checks (I3).
- DB-backed sliding-window rate limits per IP and per account and
  progressive account lockout (A1); hourly `auth_prune` job.
- `GET /api/v1/auth/session` (who am I, household, MFA state, CSRF token, or
  a `401` problem with a pre-auth CSRF token) and `GET/DELETE
  /api/v1/auth/sessions[/{id}]`.
- `PERFIN_SESSION_REMEMBER_DAYS` (default 30).
- Test factories `make_household`, `make_user`, `login_as`, `bearer_token`,
  `authed_client` (docs/DEVELOPMENT.md, "Tests").
- Second factors (`app/auth/mfa/`, `app/api/routers/auth_mfa.py`): TOTP
  enrolment with a server-generated, encrypted secret and replay protection
  (A7); WebAuthn / passkeys via py_webauthn with server-side, session-bound,
  single-use challenges and sign-counter checks; ten Argon2id-hashed
  single-use recovery codes with their own rate limit, lockout, audit and a
  `security.recovery_code_used` event (A8). `POST /auth/mfa/verify` completes
  an MFA-pending login and `POST /auth/step-up` re-verifies, both rotating
  the session id (A3, A6). New `mfa_challenge` table and hourly `mfa_prune`
  job. `app.auth.mfa.policy` (`login_stage`, `mark_mfa_from_idp`) for the
  login and OIDC flows; `PERFIN_OIDC_TRUST_IDP_MFA` (default false) lets an
  IdP `amr` claim satisfy MFA.
- Local login (`app/auth/local.py`, `app/api/routers/auth_local.py`):
  `POST /auth/login` with per-IP and per-account limits, one full-cost
  Argon2id verification on every path (dummy hash for unknown users), the
  same `401 invalid-credentials` for every refusal, progressive lockout with
  a `user.locked` audit row, household event and notifier hook, hash upgrade
  on login, and an `mfa_pending` session for users with a second factor
  (A1, A2, A3). `POST /auth/logout`, `POST /auth/password` (current password
  plus step-up when the user has a factor; rotates the session, revokes the
  others), `GET /auth/login/options`. Every login success and failure,
  logout, password change and reset is audited without secrets.
- OIDC-only mode: with `PERFIN_LOCAL_LOGIN_ENABLED=false` local login works
  only for `is_breakglass` accounts from `PERFIN_BREAKGLASS_CIDRS`; a
  break-glass account is refused outside its CIDR in every mode (B7).
- Breached-password list bundled in `app/auth/data/` (about 73,000 common
  passwords of 12 to 128 characters from SecLists, MIT; provenance in its
  README; `scripts/build_breached_passwords.py` rebuilds it) and an opt-in
  Have I Been Pwned k-anonymity check (`PERFIN_HIBP_CHECK_ENABLED`).
- Password reset by email (A11): `PERFIN_SMTP_*` settings with config-check
  warnings, a TLS-only SMTP sender (`app/integrations/notifications/smtp.py`),
  `POST /auth/password-reset/request` (always `202`; the work happens after
  the response) and `POST /auth/password-reset/confirm` (token from the
  link's fragment in the body; 15 minutes, single use, hashed at rest; revokes
  every session). New `password_reset_token` table and hourly
  `password_reset_prune` job.
- First-run setup (A10): with no user, `perfin api` logs a one-time setup URL
  (token in the fragment, only its hash stored, 24 h) and writes it to
  `PERFIN_BOOTSTRAP_URL_FILE` (0600); `perfin bootstrap-url` prints a fresh
  one; `GET /setup/status` and `POST /setup` create the first instance admin
  and household under an advisory lock, after which the route is gone for
  good.
- Households and members (`app/domain/households/`,
  `app/api/routers/households.py`): `GET/POST /households` (all households
  and creation for instance admins), `GET/PATCH/DELETE /households/{id}`
  (name, ISO 4217 base currency; delete by an owner with step-up) and
  `GET/POST/PATCH/DELETE /households/{id}/members[/{user_id}]` (add an
  existing user or create a local one, change role, remove). Member changes
  need owner/admin plus step-up; only owners appoint, change or remove
  owners, nobody edits their own membership, the last owner stays; every
  change is audited, emits a household event and revokes the affected
  user's sessions in that household (I2, A3). Other households are `404`
  (F3); inputs forbid unknown fields (F4). `POST /auth/session/household`
  switches the session's household (membership checked, session rotated).
  Account-grant service functions (endpoints with accounts in Phase 2).
- `GET/PATCH /settings/user`: display name, locale, theme, time zone.
- Instance administration (`app/api/routers/admin_users.py`): list and
  create local users, activate/deactivate (revokes sessions and tokens),
  set the break-glass flag, reset another user's MFA; changes need step-up.
- OIDC login (`app/auth/oidc/`, `app/api/routers/auth_oidc.py`): Authorization
  Code with PKCE S256, `state` and `nonce` in a sealed 5-minute
  `__Host-perfin-oidc` cookie, single-use `state` (`oidc_used_state`), code
  exchange with `client_secret_basic` or `private_key_jwt`, ID token
  validation with pinned `RS256`/`ES256`, JWKS by `kid` (refetch on unknown
  `kid` at most once per interval), exact `iss`, `aud`/`azp`, `exp`, `iat`
  (60 s skew, 10 minutes max age), `nonce` and `at_hash` (A9, B1, B2, B9).
  Users matched by `(iss, sub)` only; JIT provisioning; link by verified
  email only after a fresh local login (+MFA) to that account; group-to-role
  mapping in the OIDC household, never `owner` or instance admin (B3, B6).
  Same-origin `return_to` (B4). RP-initiated logout with `id_token_hint`
  and an allowlisted `post_logout_redirect_uri`; back-channel logout by
  `sid`/`sub`; optional claim refresh with an encrypted refresh token
  (`oidc_claim_refresh` job); `oidc_session` table following session
  rotation through the new `app.auth.sessions.register_rotation_hook` (B8);
  hourly `oidc_prune` job.
- `app.core.http.SafeHttpClient`: SSRF-safe outbound HTTP (issuer-host
  allowlist, own DNS resolution with every address vetted and the connection
  pinned to it, link-local/metadata never, loopback only in dev mode,
  private ranges only with `PERFIN_OIDC_ALLOW_PRIVATE_ISSUER`, same-origin
  redirects only, 1 MiB cap, timeouts; `HTTPS_PROXY` honoured) (B5).
- New settings `PERFIN_OIDC_TOKEN_AUTH_METHOD`, `_PRIVATE_KEY_FILE`,
  `_PRIVATE_KEY_ID`, `_LINK_BY_VERIFIED_EMAIL`, `_HOUSEHOLD_ID`,
  `_POST_LOGOUT_REDIRECT_URIS`, `_CLAIM_REFRESH_MINUTES`,
  `_JWKS_MIN_REFRESH_SECONDS`, `_METADATA_MAX_AGE_SECONDS`,
  `_HTTP_TIMEOUT_SECONDS` and `PERFIN_BREAKGLASS_ADMIN_USERNAME`;
  `perfin config check` refuses OIDC-only mode without issuer, client id,
  break-glass CIDRs and a named break-glass admin, checks the
  `private_key_jwt` key file and the logout allowlist (B4, B7).
- Personal access tokens (`app/auth/tokens.py`, `app/auth/scopes.py`,
  `app/api/routers/auth_tokens.py`): `GET/POST /auth/tokens`, `DELETE
  /auth/tokens/{id}` and `GET /auth/tokens/scopes`, browser sessions only,
  creation behind step-up MFA. Tokens are `pfn_<id>_<256-bit secret>`, stored
  as SHA-256 only and shown once. Each has a name, at least one scope from the
  catalog that the creator's role may grant (`ai:read` is reserved), and a
  mandatory expiry of at most `PERFIN_TOKEN_MAX_DAYS` (new setting, default
  90, cap 365). At most 50 live tokens per user and household. Scopes are
  narrowed on every request to the ceiling of the user's current role, and
  `require_scope` refuses names outside the catalog. Failed bearer attempts
  are rate limited per IP and use per token. Create and revoke are audited.
  A token now lists only its own household in `GET /households` (I3).
- Phase 1 integration: Alembic revision `0002` (`0002_phase1_identity`)
  creates the thirteen identity tables, applies forced RLS with the
  self-read and token-prefix policies to `household_member`,
  `account_grant` and `api_token`, grants the application role DML, and its
  downgrade drops the six enum types. `tests/integration/test_migrations.py`
  now pins the expected Phase 1 tables, policies and grants, the explicit
  list of identity tables without RLS, the `0002 -> 0001` step and RLS
  behaviour on the migrated schema.
- End-to-end tests through the real app (`tests/integration/test_auth_e2e.py`):
  local login, MFA, `GET /auth/session`, step-up, token creation, bearer
  calls, revocation and logout; OIDC login with the fake IdP, token creation
  and back-channel logout.
- `tests/security/test_auth_surface.py`: every state-changing route (enumerated
  from the routers and checked against the OpenAPI document) refuses a
  request without a CSRF token, anonymous or with a session, except the OIDC
  back-channel logout; every error of the auth, setup, household, settings
  and admin routes is `application/problem+json`.
- Authorisation matrix (`tests/security/test_authz_matrix.py`, policy table
  `tests/security/authz_policy.py`): every route of the app, walked from
  `app.routes`, must have a policy entry (minimum role, token scope,
  step-up, instance admin or public), and is called as fourteen principals
  (anonymous, each role, instance admin, another household's owner,
  MFA-restricted and MFA-pending sessions, stale step-up, tokens without
  scopes, with the route's scope and expired, a valid cookie plus a junk
  bearer token) with real objects as path parameters; foreign objects and
  other households are `404`. A CSRF matrix refuses every state-changing
  route without the token, with another session's or the pre-auth token,
  and cross-site (F3, I3, A5, A6).
- `perfin api` on first boot also prints the setup URL to stderr as plain text
  when `PERFIN_LOG_LEVEL` suppresses the WARNING log line that normally
  carries it; tested end to end on an empty database.

### Changed

- Version 1.0.0: `backend/pyproject.toml` (and `backend/uv.lock`),
  `app.__version__` (served in the OpenAPI document, the MCP server info,
  backup manifests and export archives), `frontend/package.json` and
  `package-lock.json`; the Helm chart was already `1.0.0`.
  `scripts/release_notes.py check v1.0.0` passes. No tag has been created.
- CI backend job: timeout raised from 20 to 45 minutes and pytest runs with
  `-n logical` instead of `-n auto` (which started a single worker on the
  hosted runner); run 41870347026 was cancelled at the 20-minute limit.
- README: status is the 1.0.0 release candidate, the quick start points to
  docs/DEPLOYMENT.md, the Helm chart README and docs/UPGRADING.md, and
  names the setup link; DEVELOPMENT.md documents
  `PERFIN_EXTRACT_REQUIRE_LANDLOCK=false` in `.dev/env` for kernels without
  Landlock.
- CONTROLS.md at 1.0: no row is `planned`. G2 now names what is missing (no
  native KMS client; key separation is documented), G5 describes household
  deletion and what it leaves, A6 lists household deletion and notification
  channels under step-up, and H1/H4 record the shipped image built, run
  read-only and Trivy-scanned on GitHub Actions.
- Phase 6b finalize: CONTROLS.md E1 is now `implemented`. The
  P56-SR-R1-02 limit (statement and payslip page text kept as extracted)
  does not reach the AI boundary, and `test_ai_documents.py` proves it. M3
  (no write tools) and M8 (accepted re-identification risk, with options)
  now describe how they are covered and which tests pin them. AI_ACCESS.md
  documents the second scrub's numeric fields and the documents boundary.
  docs/security/ai.md states that the kept document text is never read by
  an AI view.
- Phases 5+6 finalize: the SPA's hashed bundles are built into and served
  from `/static/` (`build.assetsDir`, `app/core/spa.py`, the immutable
  cache rule in `app/core/headers.py`) instead of Vite's default
  `/assets/`, which is the Assets page's client route. Docs:
  `docs/ROADMAP.md` (Phase 5 net worth and recurring boxes ticked, live-run
  record, deferrals), `docs/API.md` (read-your-writes convention, live-run
  status), `docs/ARCHITECTURE.md` (job status, SPA paths),
  `docs/INGESTION.md` (live check), `docs/security/integrations.md` (feeds
  off in the live run; not run against real services), `docs/VISION.md`
  (status notes on corporate actions and the export),
  `docs/security/CONTROLS.md` (A6 accountant export step-up, F1/F9 static
  path, F3 stale revision text and tax vault visibility, F8 Phase 5/6
  bounds, G1 `manual_asset.description` not encrypted).
- `app/domain/portability/entities.py`: the `api_token` reason is a named
  constant instead of a literal under `# nosec B105`, which bandit applied
  to the whole `NOT_EXPORTED` dict and reported as 63 "nosec encountered"
  warnings.
- The tax checklist's status select is wide enough for its labels, and a
  wide table scrolls inside its card instead of widening the page
  (`.app-main { min-width: 0 }`).
- Phases 3+4 finalize: `docs/INGESTION.md` (seccomp allowlist, the review
  UI), `docs/ARCHITECTURE.md` (sandbox status), `docs/API.md` (the
  document `error` values from the sandbox, including `syscall_denied`),
  ADR-0006 (addendum: default-kill allowlist), `docs/security/CONTROLS.md`
  (C1 and C12 implemented, H2 narrowed), `docs/ROADMAP.md` (Phase 3 and 4
  boxes, live-run record and deferrals) and the README document table.
- The sandbox runner reports a child killed by `SIGSYS` as
  `syscall_denied` instead of `crashed`.

- Phase 2 finalize: `docs/ARCHITECTURE.md` describes the implemented SSE
  relay (event row + `NOTIFY`, one `LISTEN` per stream, `Last-Event-ID`
  replay), the Phase 2 worker jobs, the sandbox and the local-only blob
  store; `docs/API.md` states which Phase 2 routes exist and that
  `/settings/household` and `/audit` are not built yet; `docs/INGESTION.md`
  states the Phase 2 intake scope (upload only, structured formats
  extracted, no ZIP expansion, no auto-dedupe, no opening/closing
  reconciliation yet); `docs/ROADMAP.md` lists the deferrals from Phase 2
  (S3 blob store, household settings API, auto-dedupe, sweepers, seccomp
  allowlist, SSE resume in the SPA, rule-reference remapping) and notes
  that the sandboxed extraction subprocess (a Phase 3 box) is already
  built. `docs/security/CONTROLS.md`: F8 lists every paginated or capped
  Phase 2 list endpoint (still partial for the Phase 1 identity lists),
  A6 records the Phase 2 step-up routes, C12 the review UI, G3 the SPA's
  optional passphrase.
- The placeholder home page is replaced by the dashboard; the app shell
  test now checks the placeholder pages on routes that are still
  placeholders (`/tax`, `/loans`).
- `tests/integration/test_events.py`: the "payload stays out of the
  NOTIFY" check used the value `d-1`, which can occur inside a random
  UUIDv7 in the notice (it failed once in a full run); it now uses a marker
  that cannot be part of a hex id. `test_api_tokens.py` and
  `tests/security/test_token_security.py` took the token secret as the text
  after the last `_`, but the base64url secret can itself contain `_` (a
  run failed with the "secret" `0`); they now slice the documented
  `pfn_<12 hex>_<43 chars>` layout.
- `deploy/docker-compose.yml`: the `edge` network has a fixed subnet
  (`EDGE_SUBNET`, default `172.30.79.0/24`, gateway `EDGE_GATEWAY`,
  `172.30.79.1`), so the address host-local clients such as an SSH tunnel
  arrive from is predictable for `PERFIN_BREAKGLASS_CIDRS`. The shipped
  `PERFIN_BREAKGLASS_CIDRS=127.0.0.1/32` stays as a fail-closed default
  (break-glass login through the proxy is off until set); `.env.example` and
  `docs/AUTHENTIK.md` explain the gateway value and the IPv6 userland-proxy
  caveat.
- `deploy/egress-allowlist.txt`: the OIDC issuer comment no longer suggests
  that a LAN issuer needs no allowlist entry.
- `docs/API.md` lists the endpoints Phase 1 added beyond the first cut
  (`/setup`, `/auth/login/options`, `/auth/password`, `/auth/password-reset/*`,
  `/auth/oidc/status|link|logout`, `/auth/step-up`, `/auth/mfa/*`,
  `/auth/tokens/scopes`, `/auth/session/household`, `/settings/user`,
  `/admin/users`), corrects the sessions and tokens lines, and states that
  `Idempotency-Key` is not implemented yet.
- `deploy/.env.example` keeps comments on their own lines, so the file also
  works with `docker run --env-file` and systemd `EnvironmentFile`.
- Ruff and mypy now cover `backend/migrations`.

### Fixed

- The extraction sandbox killed every text-layer PDF with ruled tables
  (`syscall_denied`, `SIGSYS`) on hosts whose `nsswitch.conf` falls through
  to glibc's `systemd` module (Ubuntu, the GitHub runner): PyMuPDF's table
  detection imports onnxruntime, which calls `getpwuid_r`; Landlock hides
  `/etc/passwd`, `libnss_systemd` is loaded, and libcap's constructor calls
  `prctl(PR_CAPBSET_READ)`, which was not on the seccomp allowlist. That
  read-only `prctl` option is now allowed; every other option still ends
  the child (ADR-0009 addendum).
  `test_sandbox.py::test_user_database_lookups_do_not_kill_the_child` and
  `test_sandbox_documents.py::test_text_pdf_tables_as_an_unprivileged_uid`
  fail without the fix.
- A `syscall_denied` extraction now names the refused call on x86-64
  (`exit status 159, syscall 157 (prctl)` in the worker log): the child's
  filter traps to a small SIGSYS handler that reports the number and exits;
  the call still never runs, and the handler cannot be replaced
  (docs/OPERATIONS.md).
- gitleaks flagged the synthetic webhook signing secret in
  `backend/tests/fixtures/notifications.py` (history); the fixture now
  builds an obviously fake value at runtime and `.gitleaks.toml` allowlists
  the old literal for that path only.
- Tests that check audit and review payloads for card or account digits
  failed now and then (CI run 41924198511) when a random row UUID in the
  payload contained the same four digits; ids are masked before the check
  (`tests.fixtures.factories.without_ids`).
- A sale of hand-picked lots (`lots` on a sell) made
  `GET /investments/performance` answer `422 lot-not-available`, and net
  worth silently left that account and security out: the in-memory replays
  in `app/domain/investments/performance.py` and
  `app/domain/networth/compute.py` drew fresh lot ids instead of the stored
  ones the sale names. Both now pass the stored ids
  (`investments.service.stored_lot_ids`).
  `test_investments_api.py::test_sale_of_hand_picked_lots_in_performance_and_net_worth`
  fails without either half of the fix.
- Compose `proxy-init` failed on every `docker compose up` after the first
  (`chown: /data/caddy/certificates: Permission denied`): Caddy creates its
  state directories with mode 0700, which the one-shot root container
  (CHOWN only) cannot traverse, so the proxy was not started again after an
  upgrade or a `down`/`up`. It now leaves a volume Caddy already owns alone.
  Found by the v1.0.0 verification run (`docs/RELEASE_CHECKLIST.md`);
  `test_proxy_init_leaves_volumes_caddy_already_owns`.
- The Compose `av` profile could not start: the ClamAV image's `/init`
  re-links `/var/lock` and edits `/etc/clamav` in place, which the read-only
  root filesystem refuses, so `clamav` restarted forever. The new
  `deploy/clamav/entrypoint.sh` does the same work in `/tmp` and takes
  `FRESHCLAM_CONF_*` / `CLAMD_CONF_*` settings (freshclam's proxy for the
  egress profile). Found by the v1.0.0 verification run; real clamd then
  rejected an EICAR upload. `test_clamav_runs_read_only_with_own_entrypoint`.
- The AI scrubber let a street name through when a government id came just
  before the address (`SSN 123-45-6789 742 Evergreen Terrace` reached the MCP
  client and `ai_call` as `[GOVID] Evergreen Terrace`, SECURITY.md/AI_ACCESS.md
  M2): the keyworded-id rule ran over spaces into the house number. It now
  stops after nine digit or mask positions (four after "last 4"), and a house
  number is no longer taken from the tail of a dashed id (`123-45-6789 742
  ...` left `123-` before). Found by the v1.0.0 verification run; new cases in
  the scrubber corpus.
- `scripts/backup.sh` (Compose mode) failed with `service "api" is not
  running` when api and worker were stopped, which its own documentation
  recommends for a consistent snapshot: it now reads the documents and
  encrypts in one-off `api` containers. Found by the v1.0.0 verification run;
  `test_backup_script_compose.py`.
- `perfin api` and `perfin worker` served normally when `PERFIN_DATABASE_URL`
  named the schema owner (or a superuser), leaving row-level security one
  `ALTER TABLE` away (SECURITY.md H3). Both now ask the database at startup
  and refuse a superuser, `BYPASSRLS` or table-owning role (dev mode only
  warns; an unreachable database fails closed). Found by the v1.0.0
  verification run; `tests/integration/test_database_role_gate.py`.
- The MCP process loaded `app.db`, `app.auth` and the key modules again
  (SECURITY.md E4): `build_parser` registers the operator commands, whose
  option defaults were imported from `app.ops.backup` and
  `app.ops.keyrotation`. The defaults now live in `app.ops.defaults`, and
  `test_a_running_mcp_process_loads_no_database_or_key_module` passes again.
- `recurring.detect` failed every run at INFO log level (`KeyError: Attempt to
  overwrite 'created' in LogRecord`): its counts were passed as `extra` fields
  that collide with `LogRecord` attributes; they are now prefixed `detect_`.
  Found by the dev seed's worker; regression test in `test_recurring_api.py`.
- `DbSession` (`app/auth/deps.py`) is a `scope="function"` dependency, so
  the transaction commits before the response is sent. The live run saw 1
  failed read in 300 `POST /accounts` then `GET /accounts/{id}` pairs (0 in
  1000 after the fix) and once a `401` right after `POST /auth/step-up`
  rotated the session. Regression test:
  `tests/integration/test_commit_before_response.py`.
- Reloading `/assets` or opening `/assets/<id>` directly answered a problem
  `404`: the path belonged to the SPA bundle mount. Bundles now live under
  `/static/`; `tests/integration/test_spa.py` serves `index.html` for both.
- `src/app/shell.test.tsx` expected the Tax section to be a placeholder
  after the tax screens replaced it.
- `security.symbol`'s CHECK constraint: the character class `[...:_-]`
  contained `:_`, which SQLAlchemy parses as a bind parameter in a CHECK
  string and rendered as `NULL`, so the database refused symbols with `:` or
  `_` that the API accepts. The class is reordered (`_:-`) in the model and
  in revision `0005`; `test_phase56_tables_refuse_other_households_on_migrated_database`
  inserts such a symbol on a migrated database.
- Migration `0004` adds the `receipt_match` label to the `review_kind`
  enum. Without it, on a migrated database (tests build the schema from
  the models, so they passed) every document that reached the matching
  stage (which looks up open receipt suggestions) failed with `invalid
  input value for enum review_kind` and was dead-lettered, the first PDF
  statement of the live run included. The downgrade deletes `receipt_match` items and restores the 0003
  labels.
- `.gitignore`: the `documents/` data pattern also matched the SPA's
  `frontend/src/features/documents/` (document library, detail page and page
  viewer), so those sources were silently left out of git; it is now
  re-included like `backend/app/domain/documents/`.

### Security

- Phase 7 security review round 1 (P7-SR-R1-01..07):
  - The Helm chart refuses an empty or null `networkPolicy.ingressFrom`,
    `networkPolicy.dns.peers` or `externalDatabase.networkPeers`, an empty
    peer, and an `egressAllowlist` rule without a non-empty `to`: an empty
    peer list matches every source or destination, so such a value made the
    api reachable from every pod or opened port 53 to any host.
  - A webhook notification channel moved to another host or port gets a new
    signing secret (returned once), so signed bodies cannot be replayed
    from one receiver to the other.
  - docs/DEPLOYMENT.md: the key-file recipe writes the existing master key
    to `secrets/master.key` instead of generating a different one, and comes
    before the backup acknowledgement; the key never changes except through
    `perfin rotate-key`. docs/BACKUP_RESTORE.md: the rotation procedure
    covers `PERFIN_MASTER_KEY_FILE` and keeps the old key file until
    `rotate-key --status` exits 0 and a new backup exists.
  - The release workflow builds the chart tools with the same pinned Go
    (`actions/setup-go`, Go 1.24) as CI.
- Phase 6b security review round 2 (P6B-SR-R2-01..03):
  - Account, card and routing digits, last four included, no longer reach an
    assistant in four shapes both scrubber stages missed: the further groups
    of a space-grouped number after a keyword (`Account 9876 5432`,
    `routing 0210 0002 1`), a bracketed number (`Checking (4455)`,
    `Card [4455]`), a keyword glued to its digits or joined by a non-ASCII
    separator (`visa4455`, `card_4455`, `AMEX·1357`), and a bare stored
    last four in an account's name or a memo (`Sapphire 6611`: the API
    stage now knows every `account.number_last4` of the household). All in
    the corpus, which now also runs through the MCP stage alone, and
    planted end to end in every AI view.
  - Member names are also caught spelled out (`j a n e`, `J.A.N.E`), with
    Greek or Cyrillic look-alike letters or digits for letters (`JΑNE`,
    `D0E`) and, for words of five letters or more, with a plural `s` and no
    apostrophe (`WOLCOTTS`); spaced e-mails (`bob . smith @ gmail . com`)
    are caught. The scrubber runs its stages to a fixed point, so it stays
    idempotent. Remaining limits are in AI_ACCESS.md "Known limits".
  - With the merchant location hidden, the `payee` filter compares the
    payee as shown (location cut), so it no longer confirms a guessed city
    and state.
- Phase 6b security review round 1 (P6B-SR-R1-01..06):
  - A security's symbol, name, sector and exchange are now classified as
    free text and scrubbed in `list_holdings` and `get_allocation`. Sector
    keys that scrub to the same value are merged. Before, a security named
    "Jane Doe Family Trust 9988776" (typed by a member or taken from OFX
    `SECNAME`) reached the assistant verbatim.
  - The scrubber now catches more card-number forms: `*1234` and `x 1234`
    masks, card brands and networks (`AMEX 4455`, `MASTERCARD 5678`), up to
    two words between a keyword and its number (`DEBIT CARD PURCHASE 6655`),
    12-digit numbers split by `/`, `_` or `,`, and an SSN written
    `123,456,789`. Thousands-separated amounts (`12,345.67`) and dates
    (`01/15/2026`, `VISA PURCHASE 03/14`) are kept. All of these are in the
    corpus.
  - The `payee` and `category` filters of `list_transactions` match the
    scrubbed name. A guess at a hidden member name or denylisted term now
    answers like a miss and no longer confirms it.
  - The family-name words of the household's name are scrubber terms. A
    two-letter name part is matched next to a longer one ("LI JANE").
  - The AI aggregate cache key includes a digest of the scrubber terms, so
    a renamed or new member never gets a body scrubbed with the old terms.
  - `test_streams_per_user_are_capped` now waits for the first stream's slot
    instead of sleeping a fixed time. This removes a flake under `-n auto`.
- Phase 6b finalize: the second scrub in `perfin mcp` mangled numeric fields.
  `get_allocation` sent `"weight": "1.0000000000"`, the 10-digit run read as an
  account number, and the assistant got `[ACCT]` instead. Returns, rates,
  prices and quantities with many digits were affected the same way. Found
  by the live run of every tool. The fixed fields listed in
  `app.mcp.scrub.NUMERIC_FIELDS` now keep plain decimals (at most 15 integer
  digits); anything else in them is still scrubbed
  (`tests/unit/test_mcp_scrub.py`).
- Security review round 1 fixes (Phases 5+6). P56-SR-R1-01 (medium, F8):
  a member could record a trade dated 0001-01-02, after which
  `GET /investments/performance?range=max` valued 739 796 days (17 s per
  request) and `GET /networth?range=max` was a permanent `422`. Every
  client-supplied business date (trade/settle dates, prices, loan
  origination/payment/rate-change/simulation dates, asset valuations, FX
  rates, recurring due dates) must now be on or after 1900-01-01
  (`app/core/dates.py`: `422` at the API, `*_min` CHECKs in revision 0005,
  brokerage statements with an older date held as
  `investment_date_out_of_range`, older feed quotes dropped); performance is
  valued over at most 3660 days (`max` starts there) and `max` net worth
  without a granularity starts at most 1000 months back. P56-SR-R1-02 (low,
  E1): tax documents outside the registered form set (a prior-year 1040,
  an unrecognised form) kept the SSN, name and address in their extraction,
  search index and a parsed receipt's merchant when the classifier did not
  call them tax forms; any document no statement/brokerage/loan/payslip
  parser read whose text holds an SSN/ITIN, a labelled EIN or a tax-return
  heading is now redacted like a tax form, a receipt read from a redacted
  document is dropped, and the receipt parser never takes a merchant from a
  line with an SSN/EIN-shaped token.
- Phases 5+6: a request's database transaction now commits before its
  response is sent. Before, FastAPI ended the `DbSession` dependency after
  the response, so a success could be reported for a write whose commit
  then failed (a revoked token, a logout, a grant change) and a client could
  briefly read the state from before its own write.
- Phases 3-4: the extraction child's seccomp filter is now a **default-kill
  allowlist** (C1 implemented; ADR-0006 addendum). Only the syscalls
  CPython, PyMuPDF, Pillow, pillow-heif and libtesseract need are allowed,
  some only in a safe form (`clone` for threads, `kill`/`tgkill`/
  `rt_sigqueueinfo`/`rt_tgsigqueueinfo` and `prlimit64` on the child
  itself, listed `fcntl` and `ioctl` commands, `open`/`openat` without a
  write flag); anything else kills the child and the document fails with
  `extraction failed: syscall_denied`. Before, everything not on a deny list
  was allowed. The list comes from `strace` of every extractor, the fuzz
  corpus and the mutation fuzzer; the sandbox, document and fuzz suites
  pass with it on the host and in the `python:3.12-slim` base image (Debian
  glibc 2.41) under Docker's default seccomp profile.
  `test_syscall_outside_the_allowlist_kills_the_child` fails if the filter
  is default-allow again.
- Phases 3-4: gitleaks allowlists the synthetic mail-intake alias token in
  `backend/tests/unit/test_email_policy.py` (path and exact value), which
  the history scan of the Phases 3+4 checkpoint commit reported; gitleaks
  `git` and `dir` scans are clean. The security workflow keeps Semgrep
  blocking (no `continue-on-error`) with the reviewed SHA-1 suppression in
  `backend/app/auth/breached.py`.
- Phases 3-4: H2 is closer: the IMAPS intake tunnels through the egress
  proxy (`CONNECT host:993`) when `HTTPS_PROXY` is set; only the Phase 5
  price feeds remain.
- Phase 2 (Ledger): every Phase 2 table (27, ledger, documents, imports,
  `idempotency_key`, `portability_archive`) has forced RLS
  (`household_isolation`) and least-privilege grants (revision `0003`);
  references between household-scoped rows carry `household_id`, so the
  database itself refuses a link into another household (ADR-0008, F3).
  Account-level visibility (`account_grant`) is applied by transactions,
  balances, reports, budgets, goals, documents, review and SSE (full
  export and import are owner/admin only, who see every account); the
  authz matrix covers every Phase 2 route with cross-household `404`
  rows. Uploaded files are sniffed by magic bytes (SVG/HTML/polyglots
  refused), stored encrypted per object and parsed only in the sandboxed
  child (rlimits, seccomp deny list, no network, privilege drop; C1
  partial). Full export, download and import and account-grant changes
  need step-up; export archives can be passphrase-encrypted; CSV cells are
  neutralised on import and export (C7). SSE messages carry ids and types
  only.
- Phase 1 security review round 1 fixes:
  - A break-glass account can no longer escape `PERFIN_BREAKGLASS_CIDRS`
    through a personal access token (P1-SR-01, B7). `POST /auth/tokens` is
    `403 breakglass-account` for it, bearer authentication refuses its
    tokens from every address, and setting the break-glass flag revokes the
    account's tokens (audited as `pats_revoked`). Before, a token minted on
    the allowlisted network worked from anywhere.
  - A correct password no longer resets the lockout counter that failed
    second-factor attempts built up (P1-SR-02, A1/A8). The counter is reset
    only by a verified second factor (or by the password of an account
    without one), so interleaving password logins with code guesses no
    longer avoids the progressive delay and the 15-minute lock.
  - Back-channel logout tokens must carry a `jti` and work once (P1-SR-03,
    B8): the domain-separated SHA-256 of `(iss, jti)` is recorded in
    `oidc_used_state` (no schema change) until the token is too old anyway,
    and a replay is `400`. Before, a captured token could be replayed for
    ten minutes to end sessions opened after the original logout.
  - The CSRF matrix test pins the exemption list instead of deriving it
    from the production constant, so a new exemption can no longer remove
    a route from the test silently (P1-SR-04, A5).
  - Documented that adding an existing user to a household needs no consent
    from that user and ends their household-less sessions (P1-SR-05); an
    invitation flow is still open (CONTROLS.md I2).
- Phase 1 security review round 2 fixes:
  - Trusted IdP MFA (`PERFIN_OIDC_TRUST_IDP_MFA`) is dated by the ID
    token's `auth_time`, not by the callback (P1-SR2-01, A6). Before, any
    OIDC login whose `amr` showed MFA, including a silent SSO re-login on
    an IdP session that did its MFA weeks ago, gave the session a fresh
    10-minute step-up, also for users with a PerFin factor. An ID token
    without a usable `auth_time` now satisfies MFA but is never a step-up.
    New: `GET /auth/oidc/start?reauth=true` forces a new IdP login
    (`prompt=login&max_age=0`) as step-up through the IdP, and the SPA's
    verification dialog offers it to OIDC users without a PerFin factor.
  - `return_to` refuses `.` and `..` path segments, plain or
    percent-encoded, on the server and in the SPA, and the SPA re-checks its
    normalised result (P1-SR2-02, B4). Before, `/a/..//evil.example`
    normalised to the protocol-relative `//evil.example` in the SPA; only
    the router's own checks kept it on the site.
  - A back-channel logout records the token's `jti` in the same
    transaction as the revocation (P1-SR2-03, B8). Before, a revocation
    that failed after the `jti` was committed made the IdP's retry a
    `400` replay, and the sessions stayed alive.
- Phase 1 security review round 3 fixes:
  - Trusted IdP MFA is a step-up only for a forced sign-in (P1-SR3-01, A6):
    the sealed pre-auth cookie records whether the login was started with
    `reauth=true` and when, and only such a login whose ID token
    `auth_time` is not older than its start (minus the 60 s clock skew)
    stamps `mfa_verified_at = min(now, auth_time)`. Every other trusted-MFA
    login satisfies MFA but is dated at least just outside the 10-minute
    window. Before, a plain login with a recent `auth_time` was a step-up,
    so an IdP that stamps `auth_time` at token issue on a silent SSO answer
    gave a step-up with no MFA done. The `user.login` audit records
    `reauth`. AUTHENTIK.md 7c now asks admins to check that a forced
    sign-in re-runs the MFA stage and what `auth_time` a silent SSO sign-in
    carries.
- Phase 1 finalize: setting or clearing an account's break-glass flag (API
  or CLI) now ends every session of that account. Before, a session opened
  before the flag was set was not a break-glass login, so the per-request
  network check (B7) did not apply to it and it kept working from anywhere
  until it expired.
- Phase 1 integration fixes:
  - Break-glass sessions are re-checked against `PERFIN_BREAKGLASS_CIDRS` on
    every request, not only at login: from elsewhere the cookie counts as no
    session, is not touched and cannot finish a pending MFA step (B7).
  - Token creation is refused (`403 mfa-enrollment-required`) while
    `PERFIN_REQUIRE_MFA` is on and the account has no PerFin second factor.
    Bearer requests are refused for such accounts, so a session verified
    only by trusted IdP MFA used to mint tokens that never worked (I3).
  - `GET /settings/user` needs the `settings:read` scope for tokens; a
    token without any scope could read the user's settings (I3, found by
    the authorisation matrix).
  - `POST /auth/oidc/link` refuses bearer callers up front
    (`403 session-required`) instead of relying on the link flow alone.
  - The OIDC back-channel logout's `400` is now a problem-details response
    (`invalid-logout-token`) that keeps the OAuth `"error":
    "invalid_request"` member (F5).
- Phase 0 security review fixes:
  - `household` and `job` now have forced RLS, so all five platform tables
    are covered. A household row can only be read, changed or deleted in its
    own context: the `ON DELETE CASCADE` from every household-scoped table
    runs outside RLS, so before this fix one wrong `DELETE FROM household` in
    another household's context wiped that household's data (SR-1, SR-3).
    `prune_events` lists households through `perfin_household_ids()` (ids
    only).
  - `job` follows the household context. The queue machinery works across
    households only inside `queue_scope`, and a trigger keeps a job's `id`,
    `queue`, `type`, `payload`, `household_id`, `dedupe_key` and
    `created_at` immutable for every role (SR-4).
  - Job payloads must be JSON objects: enqueue and handler registration
    refuse `RootModel` payloads, a check constraint backs this, and
    `dequeue` no longer coerces the payload. A malformed row is now
    dead-lettered instead of wedging its queue forever (SR-2).
  - Instance-level audit rows can only be appended in an instance-admin or
    system-actor context (`set_context(..., system_actor=True)`) (SR-5).
  - CSRF checks every method except `GET`/`HEAD`/`OPTIONS` (SR-7).
  - The proxy refuses `/healthz*`, `/readyz*` and `/metrics*` by prefix. The
    metrics server binds `PERFIN_METRICS_BIND_HOST`, which defaults to
    loopback, so the proxy and mcp containers cannot reach it (SR-9).
  - `create_app()` without explicit settings (the uvicorn factory path)
    refuses an insecure production configuration (SR-11).
- The log scrubber's masked-account, secret-name, email and URL-credential
  patterns backtracked quadratically (8-31 s for one 20 000-character
  string); they are now linear (about 50 ms for 50 000 characters) and a test
  enforces it.

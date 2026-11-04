# Roadmap

Phases are ordered so that every release is usable and secure on its own. The
security plumbing lands first because retrofitting it is how self-hosted apps
end up on CVE lists. Smart ingestion is spread across phases: deterministic
formats first, OCR and learning later.

Checkboxes are the definition of done for each phase.

## Phase 0: Foundations (this repository's current state -> first boot)

Goal: a running skeleton that boots securely, has a login, and has nothing to
exploit yet.

- [x] Architecture, data model, ingestion design, security model, roadmap
- [x] Licence decision: GNU AGPL-3.0-or-later (`LICENSE`, SPDX header on
      every source file, enforced by `scripts/check_spdx.py`)
- [x] Monorepo scaffolding: `backend/` (uv, FastAPI, SQLAlchemy, Alembic),
      `frontend/` (Vite, React, TS), `deploy/` (Compose, Caddy)
- [ ] CI: lint, type-check, tests, Trivy image scan, gitleaks, lockfile check
      (workflows written and every command run locally; not yet run on
      GitHub Actions, and the full image with Tesseract plus its Trivy image
      scan has not been built because the build sandbox blocks its external
      fetches (the `ghcr.io` uv image, the Debian mirror); the live compose
      and Trivy evidence so far comes from a local variant image)
- [x] `perfin config check` with insecure-config refusal
- [x] Database bootstrap, migrations, RLS policy scaffolding, audit log table
      (forced RLS on all five platform tables, `household` and `job`
      included, after the Phase 0 security review)
- [x] Postgres-backed job queue with a no-op job and worker entrypoint
- [x] Security headers middleware, CSP, CSRF, problem-details errors
      (session-bound CSRF tokens are issued by the Phase 1 session endpoint)
- [x] Structured logging with PII scrubbing
- [x] Health/readiness endpoints, metrics on internal port
- [x] Hardened Compose: non-root, read-only, cap_drop, no published ports
      except proxy

## Phase 1: Identity (v0.1)

Goal: username/password and Authentik OIDC both work, with MFA, sessions and
roles. Still no finance features.

- [x] One-time bootstrap setup flow for the first admin
- [x] Local login with Argon2id, rate limits, lockout, uniform responses
      (telling the user about a lock out of band waits for the Phase 7
      notification channels; until then it is an audit row and a household
      event)
- [x] Server-side sessions, `__Host-` cookie, rotation, idle/absolute timeouts,
      session list/revoke
- [x] WebAuthn registration/login, TOTP, recovery codes, step-up
      (WebAuthn is tested with a software authenticator in the backend and
      with mocked browser APIs in the SPA; not yet tried with a hardware key
      in a real browser)
- [x] OIDC: discovery, PKCE, full ID token validation, `(iss, sub)` matching,
      JIT provisioning toggle, group-to-role mapping, RP-initiated and
      back-channel logout, OIDC-only mode with break-glass admin
      (tested against the strict fake IdP in `tests/fixtures/fake_idp.py`;
      not yet against a live Authentik)
- [x] Households, members, roles, account grants (data model only; accounts
      arrive in Phase 2)
- [ ] Authz test matrix in CI (every endpoint x every role x cross-household)
      (the matrix exists and passes in `make check`:
      `backend/tests/security/test_authz_matrix.py`; not ticked because, as
      for the Phase 0 CI item, the CI workflows have not run on GitHub
      Actions yet)
- [ ] Authentik setup guide with screenshots-free step list
      (`docs/AUTHENTIK.md` is complete against the final code, with no open
      TODOs; not ticked until it has been walked through against a live
      Authentik, which this build sandbox cannot run: some Authentik field
      labels and version details are from documentation, not a live instance)
- [x] Personal access tokens with scopes

## Phase 2: Ledger (v0.2)

Goal: a usable manual finance tracker. Accounts, transactions, categories,
rules, budgets, basic dashboard. Import of **structured** formats only.

- [x] Accounts (all types), manual balances, balance history (API)
- [x] Transactions with splits, transfers, payees, categories, tags, notes (API)
- [x] Rules engine with dry-run and re-apply
- [x] Budgets (monthly/annual/custom, rollover, forecast) and spending, income, trend and cash-flow reports (API)
- [x] Savings goals (accounts, contributions, progress, projected completion; API)
- [x] Structured import: CSV (with column mapping UI and saved mappings),
      OFX/QFX, QIF, with `import_hash` dedupe and the review queue for
      duplicates (backend covered end to end through the real app and worker
      in `backend/tests/integration/test_phase2_end_to_end.py`; mapping
      editor and review UI in `frontend/src/features/imports/` and
      `frontend/src/features/review/` with vitest coverage; the whole path,
      including mapping a CSV and confirming a duplicate in the browser, was
      run live against `perfin api` + `perfin worker`, see CHANGELOG
      "Resume here". Not built: auto-dedupe, the opening + transactions =
      closing reconciliation check (Phase 3 box))
- [x] Document store with envelope encryption; uploaded CSV/OFX kept as
      documents
- [x] SPA: dashboard, accounts, transactions grid with inline edit, rules,
      budgets, review queue, settings; SSE-driven refresh
      (every screen has vitest coverage against mocked fetch, and every page
      was loaded live in headless Chrome against the real API, with a
      transaction created over the API appearing in the open grid through
      SSE without a reload. Known gaps, not part of this box: a reconnecting
      stream refetches instead of resuming with `Last-Event-ID`; no UI for
      rule import/export or for the household settings
      `reports.include_pending_review` / `documents.viewer_download`, which
      have no API yet either; transaction filters take one value per field)
- [x] Full export/import (JSON + CSV + documents zip; docs/PORTABILITY.md)

Deferred from Phase 2 (built later or waiting on a decision; none is needed
for the boxes above):

- S3-compatible blob store: only the local filesystem store exists
  (`S3BlobStore` refuses to run; parking lot below).
- `GET/PATCH /settings/household` and a UI for the household settings
  `reports.include_pending_review` and `documents.viewer_download` (both use
  their defaults until then).
- Auto-dedupe of byte-identical uploads (they always go to review).
- Periodic jobs: a sweeper for orphaned ciphertext files and a prune of
  expired idempotency keys (both are pruned only opportunistically today).
- Seccomp default-deny allowlist for the extraction child (C1 partial).
- SSE resume on reconnect in the SPA (`Last-Event-ID`; it refetches instead).
- Remapping rule references when a category, payee or tag is merged or
  deleted (such conditions never match and such actions are skipped).
- Audit rows for rejected uploads; `import_hash_alias` in export archives;
  a streaming reader for large import archives.

## Phase 3: Smart ingestion I (v0.3)

Goal: "upload a statement" works for text-layer PDFs; drop folder works.

- [x] Sandboxed extraction subprocess (rlimits, seccomp, timeouts)
      (`backend/app/ingestion/sandbox/`: every extractor (CSV/TSV, OFX/QFX,
      QIF, XLSX, PDF, images with OCR, EML, ZIP) runs in it; the seccomp
      filter is a default-kill allowlist since this phase (ADR-0006
      addendum), tested by `backend/tests/security/test_sandbox*.py` and the
      fuzz corpus on the host and in the `python:3.12-slim` base image, and
      used by the live run below)
- [x] PDF text extraction and layout tables
- [x] Classification: kind detection, institution fingerprints (YAML), period
      detection, account matching by last-4
- [x] Statement parsers: generic bank/card table parser plus fingerprints for
      an initial set of institutions (contributed with synthetic fixtures)
- [x] Reconciliation check and confidence scoring
- [x] Review queue UI with page preview and region highlight; "remember this
      layout" and "create a rule from this" actions
      (`frontend/src/features/review/ReviewTools.tsx`,
      `frontend/src/features/documents/PageViewer.tsx`; vitest coverage
      for every tool, backend `tests/integration/test_review_tools.py`;
      preview, highlight, correct-and-confirm and rule drafting were also
      walked in headless Chrome against a live API and worker.
      "Remember this layout" and the drop folder's unknown-owner dialog are
      covered by vitest and the backend tests only, not by the live walk)
- [x] Drop folder watcher with settle time, read-only mount, subfolder
      mapping, processed/failed handling, caps
      (live: a PDF dropped into a member's subfolder was attributed,
      imported and moved to `processed/`. No API or UI yet to list the drop
      folder's status, routes or entries: routes are created only by the
      unknown-owner "remember" choice and by name matching)
- [x] `perfin ingest --dry-run` CLI
- [x] Upload fuzz corpus in CI (malformed PDF, zip bomb, polyglot, XXE)
- [x] Optional ClamAV sidecar
      (tested against a protocol-faithful fake clamd, also in the live run;
      not run against a real clamd here)

## Phase 4: Smart ingestion II (v0.4)

Goal: scanned documents, receipts, email intake.

- [x] OCR pipeline (ocrmypdf/Tesseract), image preprocessing, receipt profile
      (Tesseract in process through `tesserocr` inside the sandbox child,
      not ocrmypdf: ADR-0009 addendum)
- [x] Receipt parsing (merchant, date, total, tax, line items) and
      receipt-to-transaction matching with suggestions
- [x] Email intake: IMAPS poller, alias token, sender policy, SPF/DKIM check,
      rejected digest, `.eml` provenance, attachment expansion
      (against a fake IMAPS server in tests; not run against a real mail
      provider. IMAP IDLE and OAuth mailbox login are not built)
- [x] Full-text search over extracted text
- [x] Column-mapping editor in review that produces reusable fingerprints
      (a mapping saved from a PDF or image table carries the table's header
      fingerprint and imports the next document of that layout without a
      question: `tests/integration/test_review_tools.py::
      test_pdf_table_mapping_is_reused_for_the_next_document`; the editor
      shows the page above the mapping form:
      `frontend/src/features/imports/mapping.test.tsx`)
- [ ] Heuristic classifier trained on synthetic + contributed samples
      (the naive Bayes classifier is trained and evaluated on synthetic
      samples only; no contributed samples exist yet, so its accuracy on
      real documents is unknown)

Live verification of Phases 3+4 (2026-11-04; fresh database migrated to
`0004`, `perfin api` and `perfin worker` in dev mode, a fake local clamd):
setup and TOTP login; a text-layer PDF statement imported into the ledger
with no review; an image-only scan of a statement OCR'd and imported (22 of
22 rows), with its page preview served as PNG; a thermal-receipt photo
linked to its card transaction; a drop-folder PDF attributed to the
subfolder's member, imported and moved; an infected test file rejected by
clamd; `GET /documents?q=` finding the statement; a second scan held for a
doubtful OCR reading, corrected, confirmed and turned into a rule through
the review UI in headless Chrome; `perfin ingest --dry-run` printing every
stage. The run found that migration `0004` lacked the `receipt_match`
review kind; fixed with a regression test (CHANGELOG).

Deferred from Phases 3-4 (not needed for the boxes above):

- Parsers for invoices, tax forms (the `tax_1099_int` fingerprint),
  insurance declarations and brokerage statements (they wait in review);
  payslips create no ledger rows.
- An API and UI for the drop folder's status, routes and entries, and for
  the antivirus status.
- The compose stack with the real worker image (its Debian
  `tesseract-ocr-eng` data) and Semgrep have not been run here
  (deb.debian.org and semgrep.dev are blocked in the build sandbox).

## Phase 5: Investments, loans, net worth (v0.5)

- [x] Securities, lots, investment transactions, holdings cache
- [x] Brokerage statement parsing (holdings and activity sections)
- [x] Price providers (manual first; pluggable external, opt-in), FX rates
      (`app/integrations/prices/`, `app/integrations/fx/`, `refresh_prices`,
      `/fx-rates`; docs/security/integrations.md; the external feeds are
      tested against a mock transport only, and FX rates have an API but no
      screen yet: see the deferrals below)
- [x] Performance: TWR, MWR, allocation views, realised/unrealised gains
      (API: `/investments/holdings`, `/investments/performance`; screens:
      `frontend/src/features/investments/`)
- [x] Loans: amortisation, payment split from statements, extra-payment
      simulation, rate changes
- [x] Manual assets and valuations
- [x] Daily net worth snapshots, history chart, breakdown, projections
      (`/networth`, `/networth/latest`, `/networth/projection`,
      `snapshot_net_worth`; `frontend/src/features/networth/`)
- [x] Recurring transaction detection and cash-flow calendar
      (`app/domain/recurring/`, `/recurring`, `/cashflow/calendar`,
      `detect_recurring`; `frontend/src/features/calendar/`)

Phases 5+6 integration: revision `0005_phase5_6_wealth_tax` (17 tables,
11 enum types) and `tests/integration/test_phase5_6_end_to_end.py` (brokerage
PDF and OFX to holdings and performance, a loan statement's payment split, a
manual asset valuation, the net worth snapshot job and history, recurring
detection and the calendar, a W-2 and a 1099-INT ticking off the checklist
with no identity value stored, the accountant export) through the real
worker.

Live verification of Phases 5+6 (2026-11-04; fresh database migrated to
`0005` by `perfin migrate`, `perfin api` and `perfin worker` in dev mode,
built SPA, synthetic documents from `tests/fixtures/synth_*`): setup and
TOTP login; a brokerage PDF (January) and OFX (February) processed through
the sandbox, `GET /investments/holdings` equal to the February positions
(4 securities, market value 33365.731046 against the statement's
33365.7310454 unrounded) and `/investments/performance` over the account
(TWR 0.0554402266, MWR 0.1097820888); two mortgage statements recorded
with the printed principal/interest/escrow split (`schedule_check` `ok`)
and the account balance equal to the second statement's; the 360-row
schedule and a +200/month simulation; a manual asset valuation; recurring
detection finding the monthly bill and biweekly salary and the 45-day
calendar; the `snapshot_net_worth` job through the worker storing today's
snapshot and backfilling the year (`/networth` serving stored snapshots);
a W-2 and a 1099-INT ticking off the 2025 checklist with exact box amounts
and no identity value anywhere in a database dump; the accountant export
(refused to a personal access token with `session-required`, built after
step-up, the zip holding both originals plus `summary.csv`/`summary.pdf`,
a second download `410`); then the investments, loans, loan detail, net
worth, assets, asset detail, calendar, tax and tax-year pages in headless
Chrome with the live data. The run found two real bugs, fixed with
regression tests (CHANGELOG "Fixed"): request transactions committed after
the response was sent (a write could be invisible to the next request:
1 failed read in 300 create-then-read pairs, 0 in 1000 after the fix; the
likely cause of a `401` seen once right after a step-up rotated the session), and a reload of `/assets` or
`/assets/<id>` hitting Vite's `/assets/` bundle mount instead of the SPA
(bundles now live under `/static/`).

Deferred from Phases 5-6 (not needed for the boxes above):

- The Phase 5/6 tables are not in the portability export (format v1;
  PORTABILITY.md): investments, loans, manual assets and the tax vault do
  not leave in `/export` yet, and `lot_links` ids need remapping on import.
- No screen for `/fx-rates`, the cost-method switch, specific-lot sales,
  editing loan terms, or resolving held brokerage and loan statements
  (the backend has no resolve action for those either: dismiss or reprocess).
- Corporate actions are splits/ratios and cash only (no mergers, spin-offs
  or return of capital); performance covers the securities portfolio only.
- The external price and FX feeds have not been run against a real service,
  and there is no feed-specific test through the egress proxy (H2 partial).
- Net worth and recurring days are UTC dates; tax years are one per
  household and calendar year (no separate state jurisdiction year);
  `tax.reminder` events are not delivered anywhere until notifications
  (Phase 7).

## Phase 6: Tax vault (v0.6)

- [x] Tax years, checklist items learned from prior years, received/missing
- [x] Tax form parsing (W-2, 1099 family, 1098, 1095) with key boxes
- [x] Deductible tagging and category-to-schedule hints, yearly roll-up
- [x] Accountant export: originals zip + summary CSV/PDF
- [x] Reminders as the year closes ("3 expected documents still missing")
      (the `tax.reminder` event and the missing-documents line on `/tax` and
      the dashboard; delivery by mail or push waits for Phase 7 notifications)

Screens: `frontend/src/features/tax/` (years, checklist, roll-up, schedule
hints, accountant export with step-up). The tax forms are parsed from
synthetic reportlab forms only; no real issuer's layout has been tried.
Live run: see "Live verification of Phases 5+6" under Phase 5.

## Phase 6b: AI access boundary (v0.7)

Goal: an assistant can analyse spending, merchants, savings, holdings and
loans at full detail while addresses, dates of birth, government identifiers,
account numbers, names, contact details and documents never leave the
premises. See AI_ACCESS.md.

- [ ] `ai:read` token scope with allowlist serialisers in the API; CI test
      that fails on any model column not classified as exposed or denied
- [ ] Free-text scrubber (account/card/routing patterns incl. partial masks,
      SSN/EIN, email, phone, street address, member names, custom list) with
      a CI corpus that must show zero leaks
- [ ] `perfin mcp` role: stdio and streamable HTTP transports, full tool set,
      Detail and Aggregates modes, rate limits, full-response audit log
- [ ] Client management UI: create with step-up MFA, exposure preview with
      highlighted placeholders, rounding / hide-nickname / hide-location /
      date-cap options, scrubbed-vs-original view, revoke, kill switch,
      searchable "AI access" audit page
- [ ] Setup guides for Claude Desktop and Claude Code as MCP hosts, and for an
      on-prem Ollama/llama.cpp client

## Phase 7: Polish and 1.0

- [ ] Notification channels: SMTP, webhook, ntfy, Gotify, Matrix
- [ ] Configurable dashboard widgets
- [ ] Encrypted backup/restore scripts with restore test
- [ ] Key rotation procedure (rewrap DEKs)
- [ ] Helm chart
- [ ] PWA manifest and responsive pass
- [ ] Documentation site; per-integration "what leaves the host" pages
- [ ] External security review / pen test before 1.0 tag
- [ ] Signed releases, SBOM, published image provenance

## Later / ideas parking lot

- Optional in-app assistant backed only by an on-prem endpoint, reusing the
  `ai:read` serialisers internally (never hosted providers)
- Bank sync plugin interface (open banking, Plaid-compatible shim) with the
  same review-queue semantics as documents
- Multi-household instances for families with separate finances
- Shared budgets between households
- Mobile receipt capture via PWA camera (upload path already covers it)
- Plugin API for community parsers delivered as signed packages
- Read-only "advisor" share links with expiry
- S3-compatible blob store (`app.domain.documents.blobstore.S3BlobStore` is an
  interface that refuses to run; documents are stored on the local
  filesystem, encrypted per object, until it is implemented)

## Milestone cadence

Target one phase every 6-10 weeks of part-time effort. Each phase ends with a
tagged release, a changelog, an updated SECURITY.md row count check and a
manual run of the hardening checklist against the Compose deployment.

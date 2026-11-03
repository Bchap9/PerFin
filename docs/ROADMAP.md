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
- [ ] Licence decision (leaning AGPL-3.0)
- [ ] Monorepo scaffolding: `backend/` (uv, FastAPI, SQLAlchemy, Alembic),
      `frontend/` (Vite, React, TS), `deploy/` (Compose, Caddy)
- [ ] CI: lint, type-check, tests, Trivy image scan, gitleaks, lockfile check
- [ ] `perfin config check` with insecure-config refusal
- [ ] Database bootstrap, migrations, RLS policy scaffolding, audit log table
- [ ] Postgres-backed job queue with a no-op job and worker entrypoint
- [ ] Security headers middleware, CSP, CSRF, problem-details errors
- [ ] Structured logging with PII scrubbing
- [ ] Health/readiness endpoints, metrics on internal port
- [ ] Hardened Compose: non-root, read-only, cap_drop, no published ports
      except proxy

## Phase 1: Identity (v0.1)

Goal: username/password and Authentik OIDC both work, with MFA, sessions and
roles. Still no finance features.

- [ ] One-time bootstrap setup flow for the first admin
- [ ] Local login with Argon2id, rate limits, lockout, uniform responses
- [ ] Server-side sessions, `__Host-` cookie, rotation, idle/absolute timeouts,
      session list/revoke
- [ ] WebAuthn registration/login, TOTP, recovery codes, step-up
- [ ] OIDC: discovery, PKCE, full ID token validation, `(iss, sub)` matching,
      JIT provisioning toggle, group-to-role mapping, RP-initiated and
      back-channel logout, OIDC-only mode with break-glass admin
- [ ] Households, members, roles, account grants (data model only; accounts
      arrive in Phase 2)
- [ ] Authz test matrix in CI (every endpoint x every role x cross-household)
- [ ] Authentik setup guide with screenshots-free step list
- [ ] Personal access tokens with scopes

## Phase 2: Ledger (v0.2)

Goal: a usable manual finance tracker. Accounts, transactions, categories,
rules, budgets, basic dashboard. Import of **structured** formats only.

- [ ] Accounts (all types), manual balances, balance history
- [ ] Transactions with splits, transfers, payees, categories, tags, notes
- [ ] Rules engine with dry-run and re-apply
- [ ] Budgets (monthly/annual) and spending reports
- [ ] Savings goals
- [ ] Structured import: CSV (with column mapping UI and saved mappings),
      OFX/QFX, QIF, with `import_hash` dedupe and the review queue for
      duplicates
- [ ] Document store with envelope encryption; uploaded CSV/OFX kept as
      documents
- [ ] SPA: dashboard, accounts, transactions grid with inline edit, rules,
      budgets, review queue, settings; SSE-driven refresh
- [ ] Full export/import (JSON + CSV + documents zip)

## Phase 3: Smart ingestion I (v0.3)

Goal: "upload a statement" works for text-layer PDFs; drop folder works.

- [ ] Sandboxed extraction subprocess (rlimits, seccomp, timeouts)
- [ ] PDF text extraction and layout tables
- [ ] Classification: kind detection, institution fingerprints (YAML), period
      detection, account matching by last-4
- [ ] Statement parsers: generic bank/card table parser plus fingerprints for
      an initial set of institutions (contributed with synthetic fixtures)
- [ ] Reconciliation check and confidence scoring
- [ ] Review queue UI with page preview and region highlight; "remember this
      layout" and "create a rule from this" actions
- [ ] Drop folder watcher with settle time, read-only mount, subfolder
      mapping, processed/failed handling, caps
- [ ] `perfin ingest --dry-run` CLI
- [ ] Upload fuzz corpus in CI (malformed PDF, zip bomb, polyglot, XXE)
- [ ] Optional ClamAV sidecar

## Phase 4: Smart ingestion II (v0.4)

Goal: scanned documents, receipts, email intake.

- [ ] OCR pipeline (ocrmypdf/Tesseract), image preprocessing, receipt profile
- [ ] Receipt parsing (merchant, date, total, tax, line items) and
      receipt-to-transaction matching with suggestions
- [ ] Email intake: IMAPS poller, alias token, sender policy, SPF/DKIM check,
      rejected digest, `.eml` provenance, attachment expansion
- [ ] Full-text search over extracted text
- [ ] Column-mapping editor in review that produces reusable fingerprints
- [ ] Heuristic classifier trained on synthetic + contributed samples

## Phase 5: Investments, loans, net worth (v0.5)

- [ ] Securities, lots, investment transactions, holdings cache
- [ ] Brokerage statement parsing (holdings and activity sections)
- [ ] Price providers (manual first; pluggable external, opt-in), FX rates
- [ ] Performance: TWR, MWR, allocation views, realised/unrealised gains
- [ ] Loans: amortisation, payment split from statements, extra-payment
      simulation, rate changes
- [ ] Manual assets and valuations
- [ ] Daily net worth snapshots, history chart, breakdown, projections
- [ ] Recurring transaction detection and cash-flow calendar

## Phase 6: Tax vault (v0.6)

- [ ] Tax years, checklist items learned from prior years, received/missing
- [ ] Tax form parsing (W-2, 1099 family, 1098, 1095) with key boxes
- [ ] Deductible tagging and category-to-schedule hints, yearly roll-up
- [ ] Accountant export: originals zip + summary CSV/PDF
- [ ] Reminders as the year closes ("3 expected documents still missing")

## Phase 6b: AI access boundary (v0.7)

Goal: an assistant can analyse spending and savings without any identifier,
document or personal detail leaving the premises. See AI_ACCESS.md.

- [ ] `ai:read:tier{0,1,2}` token scopes with server-side tier enforcement and
      a response redaction layer in the API
- [ ] Redaction denylist corpus (account number formats, emails, member names,
      raw payee strings) as a CI gate
- [ ] `perfin mcp` role: stdio and streamable HTTP transports, Tier 0 and
      Tier 1 tools, rate limits, full-response audit log
- [ ] Client management UI: create with step-up MFA, exposure preview,
      rounding and date-coarsening options, revoke, kill switch, "AI access"
      audit page
- [ ] Tier 2 gating on private model endpoint check, with the honest caveat
      in the UI
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
  MCP tier policy internally (never hosted providers)
- Bank sync plugin interface (open banking, Plaid-compatible shim) with the
  same review-queue semantics as documents
- Multi-household instances for families with separate finances
- Shared budgets between households
- Mobile receipt capture via PWA camera (upload path already covers it)
- Plugin API for community parsers delivered as signed packages
- Read-only "advisor" share links with expiry

## Milestone cadence

Target one phase every 6-10 weeks of part-time effort. Each phase ends with a
tagged release, a changelog, an updated SECURITY.md row count check and a
manual run of the hardening checklist against the Compose deployment.

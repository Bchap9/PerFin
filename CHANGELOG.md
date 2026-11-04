# Changelog

All notable changes to PerFin are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Resume here

Phase 0 (Foundations) is integrated: every gate in `make check` passes and the
Compose stack boots (db-init roles, `migrate` applies revision `0001`, api and
worker healthy behind Caddy). One Phase 0 box is still open in
`docs/ROADMAP.md`: CI has not yet run on GitHub Actions, and the full runtime
image (Tesseract via apt) and its Trivy image scan have not been built, because
the build sandbox blocks its external fetches (deb.debian.org, the ghcr.io uv
image); live compose and Trivy evidence so far used a local variant image.

Phase 1 (Identity, v0.1) is finalized: every gate in `make check` plus
`pip-audit` and `npm audit --audit-level=high` passes, and the identity flow
was run once end to end against a live `perfin api` (dev mode, real
PostgreSQL, built SPA): bootstrap through `POST /setup`, enrolment-only
session, TOTP enrolment, logout, login with password plus TOTP (replayed code
refused), step-up, personal access token creation, bearer calls with scope
enforcement, revocation. Seven of nine Phase 1 roadmap boxes are ticked; the
authz matrix box waits for CI to run on GitHub Actions (as the Phase 0 CI box
does) and the Authentik guide box waits for a walk-through against a live
Authentik. Status per control: `docs/security/CONTROLS.md` (A1, A6, A8, F3,
I2 and G1 stay partial for later-phase reasons stated in each row).

Phase 2 (Ledger, v0.2) is finalized: `make check` passes from the repo root
(SPDX; backend ruff, format, mypy --strict, bandit, 4402 pytest tests with
`-n auto`, OpenAPI drift; frontend eslint, tsc, 269 vitest tests, build;
`pip-audit` no known vulnerabilities; `npm audit --audit-level=high` passes
with 14 moderate advisories in dev tooling, none high). Alembic head is
`0003`. The ledger was run end to end live (`perfin migrate` on a fresh
database, `perfin api` and `perfin worker` in dev mode, built SPA): setup,
TOTP login, accounts and a manual balance, OFX upload imported by the worker
through the sandbox with a live `/events` stream, CSV mapped through
`/review` with exact duplicates skipped and a near duplicate confirmed, QIF
with a split, rule dry run then apply, budget report, cash-flow report,
encrypted export (step-up, one-time download) and import into a second
household with isolation checks; then every SPA page in headless Chrome,
SSE refresh of the open grid, CSV mapping and duplicate confirmation through
the UI. All eight Phase 2 roadmap boxes are ticked; the deferrals are listed
under Phase 2 in `docs/ROADMAP.md` (S3 blob store, `/settings/household`,
auto-dedupe, sweepers, seccomp allowlist, SSE resume in the SPA, rule
reference remapping). The sandboxed extraction subprocess is built but its
box belongs to Phase 3.

Next: Phase 3 (Smart ingestion I). Open items carried over: the Phase 0 CI
box and the Phase 1 authz-matrix CI and Authentik boxes still wait for a run
on GitHub Actions and a live Authentik; security review round 1 of Phase 2
(P2-SR-01..05) and round 2 (P2-SR-R2-01..02) are fixed in the working tree with regression tests; the pre-auth CSRF cookie
(`__Host-perfin-pre`) and the OIDC cookies are always `Secure`, so dev mode
over plain http works only on `localhost`; C1 stays partial until the
seccomp filter is a default-deny allowlist. (Time-zone validation uses the
system tz database; the `python:3.12-slim` runtime base ships it.)

## [Unreleased]

### Added

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

### Security

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

### Changed

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

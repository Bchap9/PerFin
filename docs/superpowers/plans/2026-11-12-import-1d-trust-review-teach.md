# Import 1d: trust policy and a Review that can be finished. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A statement that reads cleanly and that nothing contradicts is imported as posted with a quiet "not checked against a balance" marker; a statement that does not add up is fixed in one view that works on the ledger rows; a deleted row stays deleted, a re-read replaces instead of adding; the queue shows one card per document; what a file imported can be traced both ways.

**Architecture:** The policy lives in the importer (`app/domain/imports/service.py`), not in the parsers: a parser still scores an unchecked row `conf.UNCHECKED`, and the importer decides that a statement whose only doubt is "nothing to check against" is a clean read. One migration (`0013`) adds `statement_record.verification` and the table `import_tombstone`. A new module `app/domain/review/reconciliation.py` computes a statement's sum from the rows the document produced in the ledger and serves `GET /documents/{id}/reconciliation` with three exits (confirm, finish later, accept). Review items stay one row per finding (resolutions and re-runs depend on that); `GET /review/cards` groups them by document and names the one next action. The API shapes and the routes are declared first (Task 2), so the backend and frontend lanes work side by side against one generated schema.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16, pytest; React 18, TypeScript, TanStack Query, vitest. Run the backend in the Linux dev container (`uv run ...` from `backend/`), the frontend on Node 22 (`npm ...` from `frontend/`).

**Spec:** `docs/superpowers/specs/2026-11-12-import-reliability-design.md` (sections "1d", "Data model", "Security"). Evidence: findings N1 to N11 and recommendations P0/P1/P2 of the ingestion audit, and section 4.1 of the frontend audit, kept with the corpus results.

**Builds on:** plans 1a, 1b and 1c merged into `main`. 1b and 1c edit `app/ingestion/parsers/`, `app/ingestion/pipeline.py`, `app/domain/imports/service.py` (the `no_records` branch), `app/domain/investments/statement_import.py` and `frontend/src/features/review/reviewCopy.ts` before this plan runs: find code by the function name given, never by line number.

## After 1c and 1b2

Packages 1c and 1b2 landed before this plan runs. What that changes here:
- Parser versions moved (`layout_template` is already 3 after 1c; `statement_generic` and the institution parsers were bumped twice). Where a task says "version X to Y", bump by one from the current value and update `tests/unit/test_parser_versions.py` and the pipeline tests that pin it.
- 1c added review reasons `receipt_unreadable`, `payslip_unreadable`, `tax_form_unreadable`, `loan_unreadable`, `loan_bill_several_loans`, `plan_statement_does_not_add_up` and the "Written option left out" item, with copy in `reviewCopy.ts` and rules in `app/domain/review/layout.py` (such documents are never offered the column mapper). The cards of Task 13 and Task 20 must give each of these its one next action and must not undo those rules.
- 1c reworked `app/domain/investments/statement_import.py` (written options, history units, short positions) and `app/domain/imports/service.py` (unread documents say so), and added `plan_statement.py` (a balance-only retirement statement) and loan documents filed without a payment. Task 9 and Task 11 decide `verification` for these too: a plan statement whose roll-forward adds up and a loan statement whose amount due adds up are `reconciled`; say so in the task report if the code makes another choice necessary.
- Locate code by name; line numbers are stale.

## Global Constraints

- Test first: write the failing test, run it and see it fail for the stated reason, then implement.
- Fixtures are synthetic and invented. Never copy a real sample's rows, names or numbers into the repository. New PDF builders use reportlab the way `tests/fixtures/synth_pdf_statements.py` does, invented institution names and a "synthetic test document" footer.
- Every new file starts with the SPDX header (`# SPDX-License-Identifier: AGPL-3.0-or-later`; `// SPDX-License-Identifier: AGPL-3.0-or-later` in TypeScript and CSS comment form in `.css`).
- Gates on the files you touch before each commit. Backend, from `backend/`: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` (strict), `uv run bandit -q -c pyproject.toml -r app`, and the tests named in the task. Frontend, from `frontend/`: `npm run lint`, `npm run typecheck`, and the tests named in the task.
- No test may be skipped or marked xfail (docs/DEVELOPMENT.md). A test that pinned behaviour this plan changes on purpose is rewritten in the task that changes it, and the commit message names it.
- Every request body is a Pydantic model with `extra="forbid"` (`tests/security/test_input_models.py`). Every route has a line in `tests/security/authz_policy.py`. Every column of a new table is classified in `app/domain/ai/classification.py`. The MCP tool count stays 19 (`tests/security/mcp/test_mcp_server.py`) and the AI read API stays at 19 routes: nothing in this plan adds a tool or an AI route.
- The sandbox contract does not change. Nothing is added to `app/ingestion/sandbox/protocol.py`. Every existing reject and security test (`tests/security/`) passes unchanged, except `authz_policy.py`, which gains lines.
- Frontend: CSP is `style-src 'self'`. No `style` prop, no inline style, no runtime style injection in new code; layout goes into a `.css` file imported by the component. No `dangerouslySetInnerHTML`. Computed property access goes through `Map` lookups (`security/detect-object-injection`).
- Payloads, resolutions and audit rows carry ids, dates, counts, amounts and digests only: never a payee, a file name or account digits beyond what the item kind already stores.
- Money over the API is decimal text. A balance typed by a person is read with `read_printed` (Task 7): thousands separators and one currency symbol are accepted; anything else is `422`.
- Conventional commits, one commit per task, ending with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`. Stage only the files of your task (`git add <paths>`), never `git add -A`.
- Review reasons are stable strings used by the frontend copy (`frontend/src/features/review/reviewCopy.ts`, keys `"<kind>:<reason>"`); a new one needs a copy entry there, a line in `REASONS` of `reviewCopy.test.ts` and a vitest assertion.

## Decisions where the spec leaves a choice

1. **The policy is applied by the importer.** A *clean read* is a statement of a document format (`DOCUMENT_STATEMENT_FORMATS`: `pdf`, `image`) whose reconciliation is `unavailable`, with at least one row, on which every row's `low_confidence` is empty or exactly `["reconciliation"]`, with no skipped row, no balance mismatch, no truncation, no assumed date order, in a document whose parse counted no skipped line and carries none of the warnings `summary_totals_mismatch`, `section_total_mismatch`, `rows_outside_account`. One doubt anywhere makes the whole statement wait as today. Parser scores are not changed.
2. **`verification` on every record.** `reconciled`: the statement's own balances agreed with its rows (at import, or later in the reconcile view). `accepted`: a person accepted it without balancing. `unverified`: everything else, including CSV, OFX and QIF, which carry nothing to reconcile. The marker "Not checked against a balance" is shown only for an imported, unverified record of a document format (`unchecked` in the API).
3. **No balance snapshot from an unverified or accepted statement.** Only `reconciled` writes one (control C12 keeps this half unchanged).
4. **Review items stay one row per finding; the card is a view.** `GET /review/cards` groups a document's open items and names one action. Stage 3's Inbox reuses the endpoint.
5. **Reconciliation rows** of a statement are the account's ledger rows whose `source_document_id` is the document, plus rows of the account whose `import_hash` (or alias) is a hash of the stored statement's rows (a row that came first from another file). A row a person adds in the view is created with `source_document_id` set. When one document holds two statements of one account, rows are split by hash first, then by period.
6. **"Untouched since import"** means `updated_at == created_at`. The importer sets both equal at the end of a commit (rules and payee defaults applied during the import do not count as a change); every later change (an edit, a confirmation, a bulk action, a rule run, a transfer link) moves `updated_at`. Rows imported before this release that a rule changed at import look touched: they are kept and listed, and the reconcile view removes them in one action. No backfill guesses otherwise.
7. **An acceptance is kept on the statement record** and survives a re-run only while the re-run changes no ledger row of that document on that account and the statement still does not reconcile. A re-read that changes a row asks again; one that now reconciles becomes `reconciled`. No extra column is needed.
8. **A stable statement reference** (`statement_ref`, Task 24) is a digest of the statement's target, account digits, account name, currency and its ordinal among the document's statements with the same four. It leaves out the period, the rows, the balances and the parser version, so a placement survives a balance correction and a parser bump. "Import anyway" and "import as read" keep using the exact `statement_key`: a statement read differently is asked about again.
9. **Tombstones are written for single-row deletes only** (the Transactions page, the reconcile view). Deleting a document with its data writes none, so the file can be uploaded again whole. A tombstone can be taken back in the reconcile view ("Put back").
10. **A sign answer never flips rows already imported.** When every account group of a table waits, the answer sets the mapping's sign and clears `sign_unverified` (asked once per layout). When other groups were imported, "money out" is remembered for that account of that document only.
11. **Not exported.** `import_tombstone` joins `NOT_EXPORTED` with the reason the alias table has: its hashes contain the account id and cannot be recomputed for another instance. `FORMAT_VERSION` of the archive stays 2. `statement_record` is already not exported, so `verification` needs nothing.

## Out of this plan

Automatic merging of near duplicates (they become one item with a list; a person still decides). The Inbox layout, navigation and visual treatment (stage 3). A password field for encrypted PDFs. Comparing a CSV's period with a PDF's. Recording a payment from a held loan statement. Editing a saved column mapping in place. Retiring or raising `new_institution` (the dead `NewInstitutionTools` is left alone). A one-click "it was another year" for `year_assumed` (1b deferred it here): correcting the dates by hand is now stable across re-runs, because an edited row keeps its `import_hash` and is never replaced (Task 23); the one-click answer goes to the stage 2 backlog. Institutions as an API resource.

## Review Focus

1. **A statement with no balances that is not quite clean.** One row dated outside the period, or one counted skipped line, or a `section_total_mismatch` warning: no row of it may be posted without review, and no snapshot is written. The same statement with none of these posts every row, ends `processed`, and carries the marker (Tasks 9, 25).
2. **Reprocessing a document a person worked on.** A row they edited, confirmed, categorised by hand or linked as a transfer is still there with its values; rows of another document on the same account and rows of the same document on another account are untouched; only never-touched rows the new reading no longer contains are removed, and the kept ones are listed (Task 23).
3. **Two identical purchases on one day, one deleted.** After any re-run, and after the same statement arrives as another file, exactly one remains; the deleted one is neither re-imported nor offered as a near duplicate of its twin (Tasks 4, 5, 23).
4. **A card statement reconciled against the ledger.** Balances are typed as printed (owed is positive, `1,204.18` is accepted); a row that first came from an OFX file counts in the sum; a row on another account does not; "Accept as read" never writes a snapshot; a member who may not edit the account is refused and nothing changes; after an acceptance, a re-read that changes a row asks again (Tasks 7, 8, 24).
5. **A multi-account export whose sign was taken on trust.** Only the card group waits, the checking group imports; answering "money out" for the card changes no row of the checking group, and the card rows are spending (Tasks 12, 14, 25).

---

## Lanes

The Start tasks run first, in order. After them every lane may start at once. A lane never edits another lane's files during the parallel phase; a red test that comes from another lane's half-finished file is reported, not fixed. The Final tasks need several lanes' output and run in order after all lanes are merged.

| Lane | Tasks | Owns |
|---|---|---|
| Start (in order) | 1, 2 | `backend/migrations/versions/0013_import_trust.py` (new), `app/domain/imports/models.py`, `app/domain/imports/tombstones.py` (new), `app/domain/ai/classification.py`, `app/domain/portability/entities.py`, every `schemas.py` named in Task 2, `app/api/routers/reconciliation.py` (new, declared), `app/api/routers/review.py` and `transactions.py` (declarations), `tests/security/authz_policy.py`, `backend/openapi.json`, `frontend/src/lib/api/schema.d.ts` |
| L (ledger and matching) | 3, 4, 5 | `app/domain/transactions/service.py`, `app/domain/transactions/repo.py`, `app/api/routers/transactions.py`, `app/ingestion/match/dedupe.py`, `app/ingestion/match/accounts.py`, `tests/integration/test_transactions_api.py`, `tests/unit/test_dedupe_tombstones.py` (new), `tests/integration/test_ledger_matching.py` (new) |
| R (review backend) | 6, 7, 8, 13, 14 | `app/domain/review/causes.py` (new), `reconciliation.py` (new), `service.py`, `actions.py`, `assist.py`, `repo.py`, `app/api/routers/reconciliation.py`, `app/api/routers/review.py`, `tests/unit/test_reconcile_causes.py` (new), `tests/integration/test_reconciliation_api.py` (new), `tests/integration/test_review_cards.py` (new), `tests/integration/test_review_api.py`, `test_review_tools.py` |
| I (importer) | 9, 10, 11, 12 | `app/domain/imports/service.py`, `statement_records.py`, `statement_answers.py`, `app/ingestion/pipeline.py`, `app/domain/investments/statement_import.py`, `tests/fixtures/synth_trust.py` (new), `tests/integration/test_trust_policy.py` (new), `tests/fixtures/synth_brokerage.py` (one defaulted keyword), `tests/integration/test_structured_import.py`, `tests/unit/test_structured_import.py`, `test_pdf_statement_pipeline.py`, `test_multi_account_statements.py`, `test_brokerage_pipeline.py`, `test_imports_api.py`, `test_document_accounts.py` |
| D (documents backend) | 15, 16 | `app/domain/documents/service.py`, `manage.py`, `app/api/routers/documents.py`, `tests/integration/test_documents_api.py`, `test_documents.py`, `test_dropfolder.py`, `test_email_intake.py` |
| K (teach backend) | 17, 18 | `app/ingestion/parsers/template.py`, `app/domain/review/teach.py`, `tests/fixtures/synth_taught_layout.py`, `tests/unit/test_layout_template.py`, `tests/integration/test_teach_layout.py` |
| FR (frontend review) | 19, 20 | `frontend/src/features/review/ReviewPage.tsx`, `ReviewTools.tsx`, `reviewCopy.ts`, `api.ts`, their tests, and new `ReconcileView.tsx`, `ReconcileView.test.tsx`, `reconcile.ts`, `reconcile.test.ts`, `DocumentCard.tsx`, `review.css` |
| FT (frontend teach) | 21 | `frontend/src/features/review/TeachDialog.tsx`, `teach.ts`, `TeachDialog.test.tsx` |
| FD (frontend traceability) | 22 | `frontend/src/features/documents/DocumentDetailPage.tsx`, `documents/api.ts`, `DocumentsPage.test.tsx`, `imports/UploadPage.tsx`, `imports/api.ts`, `UploadPage.test.tsx`, `imports/MappingEditor.tsx`, `imports/mapping.test.tsx`, `transactions/TransactionDialog.tsx`, `TransactionsPage.tsx`, `TransactionsPage.test.tsx`, `transactions/api.ts`, `documents/documents.css` |
| Final (in order) | 23, 24, 25, 26, 27 | everything above, `docs/`, `CHANGELOG.md`, `app/ingestion/parsers/table_statement.py` and `confidence.py` (Task 25 only) |

Lane R's five tasks are numbered 6, 7, 8, 13 and 14; inside a lane the tasks run in order. Lanes R and I meet only through the names below: lane R's tests build the payloads of lane I's new items by hand, and the Final tasks run both together. The frontend lanes need only Task 2 (the generated schema) and mock the API in their tests; the one order between them is that Task 22 (lane FD) renders `ReconcileView` and so starts after Task 19 (lane FR) is committed. A lane edits only its own test files: when its change turns a test red in a file another lane owns, it names the test in its commit message and the owning lane, or Task 25 at the latest, rewrites it.

**Names used across tasks.**

- `statement_record.verification`: `reconciled`, `unverified`, `accepted` (`STATEMENT_VERIFICATIONS` in `imports/models.py`).
- Review reasons, new: `near_matches` (kind `possible_duplicate`), `same_content_skipped` (kind `possible_duplicate`, a notice), `earlier_rows_kept` (kind `low_confidence_field`). Changed: `reconciliation_unavailable` is raised only for a statement that is not a clean read; a parse `lines_skipped` item is raised only when a statement of the document does not reconcile. Payload keys added to `reconciliation_failed` / `reconciliation_unavailable`: `statement` (index), `statement_ref`, `lines_skipped`, `cause` (`summary_totals_mismatch` or `section_total_mismatch`, when that is why).
- Resolution keys, new: `accepted` (true), `statement_ref`, `sign` (`inflow_positive` / `outflow_positive`), `scope` (`mapping` / `account`), `rows` (near-duplicate decisions), `line_fixes`.
- Card actions (`CardAction`): `choose_account`, `map_columns`, `answer_sign`, `reconcile`, `check_rows`, `confirm_duplicate`, `attach_receipt`, `choose_owner`, `teach`, `reprocess`, `acknowledge`.
- Cause kinds (`CauseKind`): `extra_row`, `sign_flipped`, `transposed_digits`, `fee_or_interest`, `opening_differs_from_previous`, `duplicated_row`, `out_of_period`, `earlier_reading`, `missing_row`.
- Problem slugs: `statement-unavailable` (409, existing), `statement-does-not-reconcile` (409, existing), `statement-not-found` (existing), `tombstone-not-found` (404, new).
- Audit actions, new: `statement.reconcile`, `statement.accept`, `tombstone.restore`, `import.replace`.
- Import summary keys, new: `unverified_statements`, `replaced_rows`, `kept_rows`, `tombstoned_rows`.

---

### Task 1: Migration 0013, the two models, the tombstone store (Start)

**Files:**
- Create: `backend/migrations/versions/0013_import_trust.py`, `backend/app/domain/imports/tombstones.py`
- Modify: `backend/app/domain/imports/models.py` (`StatementRecord`, module doc; new `ImportTombstone`), `backend/app/domain/ai/classification.py` (the `statement_record` entry; a new `_deny_table("import_tombstone", R_INGEST, ...)`), `backend/app/domain/portability/entities.py` (`NOT_EXPORTED`), `backend/tests/unit/test_portability.py` (`test_not_exported_only_holds_justified_tables`: add `import_tombstone` to `allowed`)
- Test: `backend/tests/integration/test_migrations.py`, `backend/tests/integration/test_ledger_models.py`

**Interfaces:**
- `imports/models.py`: `STATEMENT_VERIFICATIONS: Final = ("reconciled", "unverified", "accepted")`; `StatementRecord.verification: Mapped[str]` (`String(16)`, not null, default and server default `'unverified'`, check `verification IN ('reconciled', 'unverified', 'accepted')` named `verification_known`).
- `ImportTombstone(UUIDPk, Timestamps, HouseholdScoped, Base)`, table `import_tombstone`: `account_id` (uuid, `household_fk` to `account`, cascade), `import_hash` (`CHAR(64)`, check `^[0-9a-f]{64}$` named `import_hash_hex`), `deleted_at` (timestamptz, not null), `deleted_by` (uuid, `ForeignKey("app_user.id", ondelete="SET NULL")`, nullable), `posted_date` (date, nullable), `amount` (`AMOUNT`, nullable); `UniqueConstraint("household_id", "account_id", "import_hash", name="uq_import_tombstone_hash")`. `posted_date` and `amount` are two more columns than the spec lists: without them "Put back" and the list of removed rows could show nothing. No payee text is kept.
- Revision `0013` (`down_revision = "0012"`): adds the column with its check, creates the table with `all_rls_ddl` and `table_grants_ddl` as `0010` does (`TABLES = ("import_tombstone",)`), and backfills `verification` in this order: (a) `reconciled` for `kind = 'bank'`, `source_format IN ('pdf', 'image')` records for which a `balance_snapshot` with `source = 'statement'`, the same `account_id` and `source_document_id = statement_record.document_id` exists; (b) `reconciled` for `kind = 'investment'`, `outcome = 'imported'`, `source_format IN ('pdf', 'image')`; (c) `accepted`, overriding (b) and where (a) did not apply, when a resolved `review_item` of the document has `resolution->>'import_as_read' = 'true'` and `resolution->>'account_id'` equal to the record's account, or (bank) `payload->>'reason' IN ('reconciliation_failed', 'reconciliation_unavailable')` with `payload->>'account_id'` equal to the record's account; (d) everything else stays `unverified`. Downgrade drops the table, then the check and the column.
- `tombstones.py`: `add_tombstone(db, row: Transaction, *, user_id: uuid.UUID | None) -> bool` (no-op and False when `row.import_hash` is None; idempotent on the unique key), `tombstoned_hashes(db, household_id, account_id, hashes: Collection[str]) -> set[str]`, `list_tombstones(db, household_id, account_id, hashes: Collection[str]) -> list[ImportTombstone]`, `remove_tombstone(db, household_id, tombstone_id) -> ImportTombstone | None`.

- [ ] **Step 1: Failing tests.**
  - `test_migrations.py`: `IMPORT_TRUST_TABLES = frozenset({"import_tombstone"})` joins the table sets the head checks use; `test_0013_verification_is_backfilled_and_tombstones_are_isolated`: at `0012` insert four statement records (a PDF bank record with a statement snapshot; a PDF bank record with a resolved `reconciliation_failed` item and no snapshot; an OFX bank record; a PDF investment record with a resolved `import_as_read` item), upgrade, and assert `reconciled`, `accepted`, `unverified`, `accepted`; under the app role a tombstone of household A is invisible to household B, an insert naming B's account from A is refused, `verification = 'maybe'` is refused; downgrade to `0012` removes the table and the column and upgrades again.
  - `test_ledger_models.py`: `test_tombstone_is_idempotent_and_goes_with_its_account` (`add_tombstone` twice gives one row; deleting the account removes it; a row without `import_hash` adds none); `test_tombstoned_hashes_only_names_the_account_asked_for`.
  - `tests/security/ai/test_column_classification.py` and `tests/unit/test_portability.py` fail until the two registrations exist (run them; no new test).
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_migrations.py -k "0013 or forced_rls or drift or single_head" tests/integration/test_ledger_models.py -k tombstone tests/security/ai/test_column_classification.py tests/unit/test_portability.py`. Expected: no revision `0013`; unclassified column.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the same, plus `tests/integration/test_rls.py` and `tests/integration/test_migrations.py` whole. Expected: pass.
- [ ] **Step 5:** Commit `feat(db): statement verification and import tombstones (migration 0013)`.

### Task 2: API shapes and routes declared, schema generated (Start)

**Files:**
- Modify: `backend/app/domain/review/schemas.py`, `backend/app/domain/documents/schemas.py`, `backend/app/domain/transactions/schemas.py`, `backend/app/domain/transactions/repo.py` (`TransactionFilters`, `filter_clauses`), `backend/app/api/routers/transactions.py` (`list_transactions`), `backend/app/api/routers/review.py`, `backend/tests/security/authz_policy.py`, `backend/openapi.json`, `frontend/src/lib/api/schema.d.ts`
- Create: `backend/app/api/routers/reconciliation.py`, `backend/app/domain/review/reconciliation.py` (placeholders)
- Test: `backend/tests/integration/test_transactions_api.py`, `backend/tests/security/test_authz_matrix.py` (run), `backend/tests/security/test_input_models.py` (run)

Why first: the frontend lanes build against `schema.d.ts`, and the backend lanes must not edit one `schemas.py` at the same time. A lane that finds a field missing reports it; it does not add it.

**Interfaces** (all in `review/schemas.py` unless said; response models are plain `BaseModel`, request models extend `_Input`):
- `Verification = Literal["reconciled", "unverified", "accepted"]`; `PrintedAmount = Annotated[str, Field(min_length=1, max_length=32)]`.
- `ReconcileRow`: `transaction_id`, `posted_date`, `payee: str | None`, `amount: str`, `review_state: ReviewState`, `origin: Literal["read", "added", "matched"]`, `edited: bool`, `in_period: bool`, `page: int | None`, `bbox: Bbox | None`.
- `RemovedRow`: `tombstone_id`, `posted_date: date | None`, `amount: str | None`, `deleted_at`.
- `ReconcileCause`: `kind: CauseKind`, `transaction_ids: list[uuid.UUID]`, `amount: str | None`, `page: int | None`, `bbox: Bbox | None`.
- `StatementReconciliation`: `statement: int`, `account_id`, `account_name: str`, `currency`, `printed_sign: Literal["ledger", "owed"]`, `period_start`, `period_end`, `opening: str | None`, `closing: str | None` (both as printed), `previous_closing: str | None`, `rows: list[ReconcileRow]`, `rows_truncated: bool`, `removed: list[RemovedRow]`, `total: str`, `difference: str | None`, `status: Literal["balanced", "off", "unchecked"]`, `verification: Verification | None`, `causes: list[ReconcileCause]`, `lines_skipped: int`, `can_edit: bool`.
- `DocumentReconciliation`: `document_id`, `statements: list[StatementReconciliation]`.
- `ReconcileConfirm(_Input)`: `statement: int = Field(ge=0, le=10_000)`, `opening_balance: PrintedAmount | None = None`, `closing_balance: PrintedAmount | None = None`. `ReconcileAccept(_Input)`: `statement`. `ReconcileRestore(_Input)`: `tombstone_id: uuid.UUID`. `ReconcileRemoveEarlier(_Input)`: `statement`.
- `ReconcileOutcome`: `statement: StatementReconciliation`, `confirmed: int`, `snapshot: bool`, `items_closed: int`.
- `CardAction` (the literal under "Names"); `ReviewCard`: `document: ReviewDocument`, `items: list[ReviewItemOut]`, `action: CardAction`, `action_item_id: uuid.UUID`, `open: int`, `notices: int`; `ReviewCardPage`: `cards: list[ReviewCard]`, `loose: list[ReviewItemOut]`, `next_cursor: uuid.UUID | None`.
- `ResolveUnparsed.action` gains `"answer_sign"`; new field `positive_is: Literal["money_in", "money_out"] | None = None`.
- `ResolveDuplicate` gains `rows: list[DuplicateDecision] | None = Field(default=None, max_length=50)`; `DuplicateDecision(_Input)`: `transaction_id`, `decision: Literal["confirm", "reject"]`, `duplicate_of: uuid.UUID | None = None`.
- `ResolveLowConfidence` gains `line_fixes: list[LineFix] = Field(default_factory=list, max_length=200)`; `LineFix(_Input)`: `line: int (ge 0)`, `trade_date: date | None = None`, `amount: MoneyIn | None = None`, `quantity: MoneyIn | None = None`, `type: Annotated[str, Field(pattern=r"^[a-z_]{1,32}$")] | None = None`.
- `TeachTable` gains `direction: Literal["as_printed", "money_in", "money_out", "card"] = "as_printed"` and `continues: bool = False`; `TeachRequest.tables` `max_length` 2 becomes 5; `TeachRequest.preview: bool = False`. `TaughtRow`: `posted_date`, `amount: str`, `payee: str | None`, `table: int`, `page: int`. `TeachPreview`: `rows: list[TaughtRow]` (at most 50), `row_count: int`, `reconciliation: Literal["ok", "mismatch", "unavailable"] | None`, `difference: str | None`, `similar_waiting: int`, `account_id: uuid.UUID | None`. `TeachOut.preview: TeachPreview | None = None`.
- `documents/schemas.py`: `ImportedStatement`: `account_id`, `account_name`, `kind: Literal["bank", "investment"]`, `period_start`, `period_end`, `outcome`, `verification: Verification`, `source_format`, `unchecked: bool`, `transactions: int`, `pending: int`. `DocumentImportedOut.statements: list[ImportedStatement] = []`, `DocumentImportedOut.skipped_lines: int = 0`. `DocumentListItem.unchecked: bool = False`. `DocumentDetailOut.previewable: bool = False`.
- `transactions/schemas.py`: `TransactionCreate.source_document_id: uuid.UUID | None = None`.
- `GET /transactions` gains `document: uuid.UUID | None`; `TransactionFilters.document_id: uuid.UUID | None = None`; `filter_clauses` adds `Transaction.source_document_id == f.document_id`. This one is implemented here, whole (it is three lines and the visibility filter already applies).
- Routes declared in `reconciliation.py` (`DocumentWriter` for writes, `Reader` for the read, `idempotent(...)` on the four POSTs like `post_link`): `GET /documents/{document_id}/reconciliation -> DocumentReconciliation`; `POST /documents/{document_id}/reconciliation/confirm`, `/accept`, `/restore`, `/remove-earlier -> ReconcileOutcome`. In `review.py`, declared **above** `GET /review/{item_id}` (else `cards` is read as an item id): `GET /review/cards -> ReviewCardPage` with `cursor` and `limit`.
- Placeholders until lane R fills them: `reconciliation.get_reconciliation` and the four actions raise `ProblemError(409, "statement-unavailable", "No statement to check", ...)` after `get_visible_document` / `get_editable_document`; `service.list_cards` returns an empty page.
- `authz_policy.py`: `GET .../reconciliation` `scoped("documents:read", role=V, params=SDOC)`; the four POSTs `scoped("documents:write", role=M, params=SDOC)`; `GET /review/cards` `scoped("review:read", role=V)`.

- [ ] **Step 1: Failing tests** in `test_transactions_api.py`: `test_list_by_source_document` (two documents on one account: `?document=<a>` returns only a's rows; a document on an account the caller cannot see returns an empty page, never `403`); `test_document_filter_combines_with_review_state`.
- [ ] **Step 2:** Run them and `uv run pytest -q tests/security/test_authz_matrix.py tests/security/test_input_models.py`. Expected: unknown query parameter ignored (all rows returned); the matrix passes before the routes exist.
- [ ] **Step 3: Implement** the models, the filter, the declarations, the placeholders and the policy lines. Then `make openapi` (writes `backend/openapi.json` and `frontend/src/lib/api/schema.d.ts`).
- [ ] **Step 4:** Run the three test files again, `uv run perfin openapi | diff -u openapi.json -`, and in `frontend/`: `npm run typecheck`. Expected: pass, no drift, the frontend still compiles (every new field is optional or defaulted).
- [ ] **Step 5:** Commit `feat(api): shapes and routes for reconciliation, review cards and transactions by document`.

### Task 3: A deleted imported row leaves a tombstone; a row can be added for a document (lane L)

**Files:**
- Modify: `backend/app/domain/transactions/service.py` (`delete_transaction`, `create_transaction`, `_facts`)
- Test: `backend/tests/integration/test_transactions_api.py`

**Interfaces:**
- `delete_transaction` calls `tombstones.add_tombstone(db, row, user_id=principal.user_id)` before the row is removed, and also for each `import_hash_alias` of the row (the alias hashes are read before the cascade removes them). The audit row `transaction.delete` gains `"tombstoned": bool`. Bulk delete (`bulk`) goes through the same function per row, so it writes them too. `app.domain.documents.manage._delete_imported` does not call it (decision 9).
- `create_transaction`: with `source_document_id`, the document must be one the caller may change (`manage.get_editable_document`: `404` when hidden or of another household, `403` when read-only); the row is created with it and no `import_hash`.

- [ ] **Step 1: Failing tests:** `test_deleting_an_imported_row_leaves_a_tombstone` (row with `import_hash`: one `import_tombstone` with its hash, date and amount, `deleted_by` the caller); `test_deleting_a_manual_row_leaves_none`; `test_alias_hashes_are_tombstoned_with_the_kept_row`; `test_bulk_delete_tombstones_each_imported_row`; `test_create_for_a_document_needs_an_editable_document` (another household's document: `404`; a document the caller may only read: `403`; own document: `201` and `source_document_id` set).
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_transactions_api.py -k "tombstone or for_a_document"`. Expected: no tombstone row; `source_document_id` rejected as an unknown field before Task 2, ignored after.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `tests/integration/test_transactions_api.py tests/integration/test_transfers.py tests/security/test_ledger_surface.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(transactions): a deleted imported row leaves a tombstone`.

### Task 4: The deduplicator never brings a tombstoned row back (lane L)

**Files:**
- Modify: `backend/app/ingestion/match/dedupe.py` (`DedupePlan`, `plan`, `dedupe`, module doc)
- Test: `backend/tests/unit/test_dedupe_tombstones.py` (create, pure `plan` cases), `backend/tests/integration/test_ledger_matching.py` (create, the one case that needs a database). `tests/integration/test_structured_import.py` is not edited here (lane I owns it; the pipeline tests are in Task 23)

**Interfaces:**
- `DedupePlan.removed: list[int]` (incoming indexes whose hash is tombstoned).
- `plan(hashes, rows, existing, tombstoned: Collection[str] = ()) -> DedupePlan`: an incoming row whose hash is in `tombstoned` goes to `removed` first; it is in none of `exact`, `near`, `new`, and it claims no existing row. An existing row is still free to back a near match of another incoming row.
- `dedupe(db, household_id, account_id, rows)` loads `tombstones.tombstoned_hashes` for the hashes it computed.

- [ ] **Step 1: Failing tests:** `test_a_tombstoned_hash_is_not_inserted`; `test_one_of_two_identical_rows_stays_deleted` (rows A and A again on one day, hashes with running index 0 and 1; existing holds index 0, index 1 tombstoned: `exact == [0]`, `removed == [1]`, `near == {}`, `new == []`); `test_the_deleted_twin_is_no_near_match_of_the_other` (existing holds nothing, index 0 tombstoned, index 1 new: `new == [1]`); `test_a_tombstone_of_another_account_does_not_apply` (in `test_ledger_matching.py`, through `dedupe`: a tombstone on another account of the household is not loaded); `test_without_tombstones_the_plan_is_unchanged` (the alias case of `test_structured_import.py` reproduced: same result as before).
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_dedupe_tombstones.py tests/integration/test_ledger_matching.py`. Expected: `TypeError` for the new argument, `AttributeError` for `removed`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the new file and `tests/unit/test_structured_import.py tests/integration/test_structured_import.py tests/integration/test_pdf_statement_pipeline.py`. Expected: pass unchanged.
- [ ] **Step 5:** Commit `feat(ingestion): the deduplicator skips rows a person deleted`.

### Task 5: The sole account at the statement's institution matches without digits (lane L)

**Files:**
- Modify: `backend/app/ingestion/match/accounts.py` (`match_account`, `AccountMatch`)
- Test: `backend/tests/integration/test_ledger_matching.py` (the matcher reads accounts, so its tests need the database; factories as in `tests/integration/test_document_accounts.py`)

**Interfaces:**
- `match_account(db, household_id, *, last4, institution=None, allowed=None, institution_id: uuid.UUID | None = None, liability: bool | None = None) -> AccountMatch`. New last step, only when `last4` is empty: the accounts of the household with `institution_id` (and, when `liability` is not None, on that side by `LIABILITY_TYPES`), with `closed_on` empty, in `allowed`: exactly one is the match (`AccountMatch(id, (id,), by="institution")`); several are the `candidates` with no match. `AccountMatch.by: Literal["digits", "institution"] | None = None`.
- When `last4` is given, nothing changes: digits that match no account never fall back to the institution.

- [ ] **Step 1: Failing tests:** `test_sole_account_of_the_institution_matches_without_digits`; `test_two_accounts_of_the_institution_are_candidates_only`; `test_card_statement_does_not_land_on_the_checking_account` (`liability=True`, the institution has one checking account: no match); `test_digits_that_match_nothing_do_not_fall_back`; `test_an_account_outside_allowed_is_never_chosen`; `test_closed_account_is_not_chosen`.
- [ ] **Step 2:** Run; expected: `TypeError` for the new arguments.
- [ ] **Step 3: Implement.** (The importer passes the new arguments in Task 9.)
- [ ] **Step 4:** Run the new file and `tests/integration/test_document_accounts.py tests/integration/test_multi_account_statements.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(ingestion): match the one account of a statement's institution when it prints no digits`.

### Task 6: Likely causes of a difference, ranked (lane R)

**Files:**
- Create: `backend/app/domain/review/causes.py`
- Test: `backend/tests/unit/test_reconcile_causes.py` (create)

**Interfaces** (pure, no database):
- `@dataclass(frozen=True) class LedgerRow: id: uuid.UUID; posted: date; amount: Decimal; earlier: bool = False` and `@dataclass(frozen=True) class PageAmount: amount: Decimal; page: int; bbox: tuple[float, float, float, float]`.
- `@dataclass(frozen=True) class Cause: kind: str; rows: tuple[uuid.UUID, ...] = (); amount: Decimal | None = None; page: int | None = None; bbox: tuple[float, float, float, float] | None = None`.
- `rank_causes(difference: Decimal | None, rows: Sequence[LedgerRow], *, period: tuple[date, date] | None, opening: Decimal | None, previous_closing: Decimal | None, fee_lines: Sequence[PageAmount] = (), grace_days: int = 0) -> list[Cause]`, in this order, each at most once, at most `CAUSES_MAX = 8`:
  1. `earlier_reading`: the rows with `earlier` (always first when any);
  2. `opening_differs_from_previous`: `previous_closing` known and not within `TOLERANCE` of `opening` (`amount` is `previous_closing`);
  3. `extra_row`: the rows whose amount equals the difference (`difference = opening + total - closing`, so a row too many has the difference's sign);
  4. `sign_flipped`: the rows whose amount is half the difference;
  5. `duplicated_row`: pairs with the same date and amount, when the amount equals the difference;
  6. `fee_or_interest`: a `fee_lines` entry whose magnitude equals the magnitude of the difference (with its page and box);
  7. `transposed_digits`: the difference in cents is a non-zero multiple of 9 and no cause 3 to 6 was found (`rows` empty);
  8. `out_of_period`: rows dated outside `period` widened by `grace_days`;
  9. `missing_row`: none of 3 to 7 was found and the difference is not zero (`amount` is minus the difference).
  A zero or None difference yields only causes 1, 2 and 8.
- `fee_lines(view: DocumentView, pages: Collection[int] | None = None) -> list[PageAmount]`: lines whose normalised text holds one of `fee`, `interest`, `finance charge`, `service charge` and exactly one amount (`parsers.text.find_amounts`); at most 50.

- [ ] **Step 1: Failing tests:** one per cause (`test_a_row_equal_to_the_difference_is_named`, `test_half_the_difference_is_a_sign`, `test_multiple_of_nine_cents_is_a_transposition`, `test_transposition_is_not_named_when_a_row_explains_it`, `test_fee_line_of_the_same_size`, `test_opening_against_the_previous_closing`, `test_same_day_same_amount_pair`, `test_rows_outside_the_period_with_grace`, `test_nothing_explains_it_names_the_missing_amount`, `test_earlier_reading_comes_first`), plus `test_zero_difference_names_no_row_cause` and `test_order_and_cap` (ten candidate causes give eight, in the order above). Values invented.
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_reconcile_causes.py`. Expected: import error.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run again. Expected: pass.
- [ ] **Step 5:** Commit `feat(review): rank the likely causes of a statement that does not add up`.

### Task 7: Reconciliation is read from the ledger (lane R)

**Files:**
- Modify: `backend/app/domain/review/reconciliation.py` (replace the placeholder read), `backend/app/domain/review/assist.py` (reuse `row_region` for row boxes)
- Test: `backend/tests/integration/test_reconciliation_api.py` (create; `env` fixture and upload helper copied from `test_review_tools.py`)

**Interfaces:**
- `read_printed(text: str) -> Decimal`: strips spaces, one leading or trailing currency symbol (`$ € £`), thousands separators (`,` between digit groups of three), accepts a leading `-` or surrounding brackets as negative; anything else raises `422 invalid-amount`.
- `to_ledger(value: Decimal, account: Account) -> Decimal` and `as_printed(value: Decimal | None, account: Account) -> str | None`: negate for `account.type in LIABILITY_TYPES`.
- `statement_rows(db, document, account, statement: Statement, *, others: Sequence[Statement]) -> list[Transaction]`: decision 5. Hashes come from `assign_import_hashes(account.id, [HashInput(...)])` on the stored statement's rows, aliases included through `review.repo.alias_rows`.
- `get_reconciliation(db, principal, document_id) -> DocumentReconciliation`: one entry per stored bank statement (`stored_result(document.extraction).statements`) whose account is known: the `statement_record` of the document with the statement's period start, else the document's account for a single statement. A statement whose account the caller cannot see is left out (never an error). `opening` / `closing` from the stored statement, shown `as_printed`; `previous_closing` from the latest `statement_record` of the account with `period_end` before this period's start and another document; `total` the sum of the rows; `difference = opening + total - closing` when both balances are known; `status`: `unchecked` without both balances, `balanced` within `reconcile.TOLERANCE`, else `off`; `causes` from `rank_causes` (rows with `earlier=True` are those `earlier_rows(...)` of Task 23 returns; until then none); `removed` from `tombstones.list_tombstones` for the statement's hashes; `lines_skipped` from the stored `parse.skipped_lines`; `rows` capped at `ROWS_MAX = 2000` with `rows_truncated`; `origin`: `matched` when `source_document_id` is another document, `added` when it is this document and `import_hash` is None, else `read`; `edited` is `updated_at != created_at`; `can_edit` from `can_edit_document` and `get_visible_account(..., need="edit")`.
- A document with no stored bank statement: `409 statement-unavailable`.

- [ ] **Step 1: Failing tests:**
  - `test_reconciliation_lists_the_ledger_rows_and_the_sum` (upload `synth.generic_pdf(stmt, tamper="extra_row")`: `status == "off"`, `difference` equals the extra row's amount, `causes[0].kind == "extra_row"` naming that row's transaction id, every row has a page).
  - `test_an_edited_row_changes_the_sum` (PATCH the extra row's amount to zero through the transactions API, read again: `status == "balanced"`, the row has `edited` true).
  - `test_a_deleted_row_is_listed_as_removed_and_leaves_the_sum` (DELETE the extra row: `balanced`, `removed` has one entry with its amount).
  - `test_card_balances_are_shown_as_printed` (`synth.demo_card_pdf`: `printed_sign == "owed"`, `closing` is positive while the stored ledger balance is negative).
  - `test_a_row_that_came_from_another_file_counts` (the same month first as OFX from `tests/fixtures`, then the PDF answered "import anyway": the PDF's reconciliation is `balanced` and its rows have `origin == "matched"`).
  - `test_rows_of_another_account_do_not_count` and `test_two_statements_of_one_document_are_separate` (`synth_multi_account` builders already used by `test_multi_account_statements.py`).
  - `test_viewer_reads_and_cannot_edit` (`can_edit` false); `test_statement_on_an_invisible_account_is_left_out`; `test_document_without_a_statement_is_409`.
  - Unit, same file: `test_read_printed[...]` (`"1,204.18"`, `"$1,204.18"`, `"(12.00)"`, `"-12"`, `" 3 "` read; `"1,20"`, `"12.3.4"`, `"abc"`, `"1e5"`, `""` are `422`).
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_reconciliation_api.py`. Expected: `409 statement-unavailable` from the placeholder.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the file and `tests/integration/test_review_tools.py tests/security/test_authz_matrix.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(review): reconcile a statement against the ledger rows it produced`.

### Task 8: Three exits: confirm, accept, put back (lane R)

**Files:**
- Modify: `backend/app/domain/review/reconciliation.py`, `backend/app/domain/review/actions.py` (`correct_balances` gains `as_printed: bool = False`), `backend/app/domain/review/service.py` (`confirm_pending` reused; new `close_statement_items`)
- Test: `backend/tests/integration/test_reconciliation_api.py`

**Interfaces:**
- `confirm(db, principal, document_id, body: ReconcileConfirm, *, request) -> ReconcileOutcome`: stores the typed balances (`read_printed`, then `to_ledger`) on the stored statement through `correct_balances`; recomputes from the ledger (Task 7); when not `balanced`: `409 statement-does-not-reconcile` with the difference in `detail`; when balanced: every pending row of the statement becomes `confirmed`, the snapshot is written (`accounts_repo.upsert_snapshot`, `BalanceSource.STATEMENT`, as `accept_statement` does), the record's `verification` becomes `reconciled`, and `close_statement_items` resolves the document's open items for that account whose reason is in `STATEMENT_FINDINGS = {"reconciliation_failed", "reconciliation_unavailable", "lines_skipped", "low_confidence", "balance_mismatch", "earlier_rows_kept"}` with `{"reconciled": True, "statement_ref": ...}`. Audit `statement.reconcile` (ids, counts, the two balances).
- `accept(...) -> ReconcileOutcome`: pending rows of the statement become `confirmed`; **no snapshot**; `verification = accepted`; the same items are resolved with `{"accepted": True, "statement_ref": ..., "account_id": ...}`. Audit `statement.accept` with the difference accepted. Allowed for `off` and `unchecked`; on a `balanced` statement it is `409 statement-balanced` ("Confirm it instead").
- `restore(...)`: removes the tombstone (`404 tombstone-not-found` when it is not one of this document's statement hashes) and inserts the row again from the stored statement with `review_state = pending`, the document as its source and its original hash. Audit `tombstone.restore`.
- `remove_earlier(...)`: deletes the statement's `earlier` rows (Task 23's `earlier_rows`) through `transactions_service.delete_transaction`, so each leaves a tombstone. Until Task 23 it removes nothing and returns the unchanged statement.
- All four need `get_editable_document(..., lock=True)` and `get_visible_account(..., need="edit")`; whatever those two raise for the caller (`404` hidden, `403` read-only) is the answer, before anything is written.
- "Finish later" is no request: the items stay open.

- [ ] **Step 1: Failing tests:** `test_confirm_writes_the_snapshot_and_closes_the_findings` (the tampered statement after the extra row is deleted: `confirmed` counts the pending rows, one statement snapshot at the closing balance, record `reconciled`, document `processed`, no open item); `test_confirm_refuses_while_off` (`409`, nothing changed, no snapshot); `test_confirm_takes_card_balances_as_printed` (card statement stored with a wrong closing: posting the printed positive `"1,204.18"`-style value balances it); `test_accept_posts_the_rows_and_writes_no_snapshot` (record `accepted`, zero `balance_snapshot` rows for the document, rows `confirmed`, items resolved with `accepted`); `test_accept_on_a_balanced_statement_is_refused`; `test_restore_puts_a_deleted_row_back_pending`; `test_restore_refuses_a_tombstone_of_another_document`; `test_member_without_edit_on_the_account_is_refused` (an account grant of `view` only: the status `get_visible_account(need="edit")` gives, no row, record or item changed); `test_other_findings_of_the_document_stay_open` (an `unknown_account` item of a second statement is untouched by the first one's confirmation).
- [ ] **Step 2:** Run; expected: `409 statement-unavailable` from the placeholders.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the file, `tests/integration/test_review_tools.py` (`test_reconciliation_confirmation_covers_the_whole_statement` and `test_correct_fixes_document_facts_and_can_resolve` unchanged: the old route still works), `tests/security/test_authz_matrix.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(review): confirm, accept or finish a reconciliation later; put a removed row back`.

### Task 9: A clean read is imported as posted and marked (lane I)

**Files:**
- Create: `backend/tests/fixtures/synth_trust.py`
- Modify: `backend/app/domain/imports/service.py` (`_Run`, `_row_problems`, `_commit_statement`, `_import_statements`, `import_extraction`), `backend/app/domain/imports/statement_records.py` (`record_statement`), `backend/app/ingestion/pipeline.py` (`_parse_document`: the `lines_skipped` item)
- Test: `backend/tests/integration/test_trust_policy.py` (create; `env` and upload helper as in `test_pdf_statement_pipeline.py`)

**Interfaces:**
- `CONTRADICTION_WARNINGS: Final = frozenset({"summary_totals_mismatch", "section_total_mismatch", "rows_outside_account"})`; `_Run.parse_skipped: int` and `_Run.parse_warnings: tuple[str, ...]`, filled in `import_extraction` from `result.parse`.
- `clean_read(run: _Run, parsed: ParsedStatement) -> bool` (decision 1; module level, pure on its arguments).
- `_commit_statement`: for a clean read a row is `pending` only when it is a near duplicate; `_row_problems` raises no `reconciliation_unavailable` item for it; no snapshot. It returns the verification it reached: signature becomes `-> Verification | None` (None: nothing written), `reconciled` when `parsed.reconciled` and a reconciliation exists with status `ok`, else `unverified`. Call sites pass it on.
- `record_statement(..., verification: str = "unverified")` writes the column.
- The importer passes `institution_id=run.document.institution_id` and `liability` (None when `parsed.account_type` is empty, else whether it names one of `accounts.models.LIABILITY_TYPES`) to `match_account` (Task 5).
- Summary key `unverified_statements`.
- `pipeline._parse_document`: the parse `lines_skipped` item is raised only when `not _all_reconciled(parsed, brokerage)` (`_all_reconciled`: at least one statement and each has a reconciliation with status `ok`). The count stays in the stage facts and goes into `document.confidence["parse"]["skipped_lines"]`.
- Builders in `synth_trust.py`, each returning `bytes`, invented names, a labelled period with years, a masked account number, six dated rows with signed amounts: `no_balances_statement()` (no opening, no closing, no running balance column); `no_balances_with_a_stray_row()` (the same plus one row dated 40 days before the period); `no_balances_with_an_unread_line()` (the same as the first plus a dated line whose amount cell is not an amount, so it is counted as skipped); `balanced_with_an_unread_line()` (opening, closing and running balances that agree, plus an undated notice line the parser counts as skipped).

- [ ] **Step 1: Failing tests:**
  - `test_clean_read_without_balances_is_posted_and_marked`: `no_balances_statement()` into a matching account: six rows, none pending; document `processed`; no open review item; one `statement_record` with `verification == "unverified"`; no `balance_snapshot` for the document; the import summary has `unverified_statements == 1`.
  - `test_a_row_outside_the_period_holds_the_whole_statement`: every row pending, an open `reconciliation_unavailable` item, document `needs_review`, record `unverified`.
  - `test_an_unread_line_holds_the_whole_statement`: the same, plus an open `lines_skipped` item.
  - `test_reconciled_statement_with_an_unread_line_is_processed`: `balanced_with_an_unread_line()`: no open item, document `processed`, record `reconciled`, `document.confidence["parse"]["skipped_lines"] == 1`.
  - `test_reconciled_statement_is_recorded_reconciled` (`synth.generic_pdf`) and `test_statement_that_does_not_add_up_is_still_held` (`tamper="extra_row"`: pending rows and the `reconciliation_failed` item as today, record `unverified`).
  - `test_csv_statement_is_unverified_without_a_marker_item` (a CSV import: record `unverified`, no item raised because of it).
  - `test_contradiction_warning_is_never_a_clean_read` (unit: `clean_read` on a hand-built `ParsedStatement` with `run.parse_warnings = ("section_total_mismatch",)` is False; with one transaction whose `low_confidence == ["posted_date"]` False; with `["reconciliation"]` on every row True; with no rows False; with `source_format == "ofx"` False).
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_trust_policy.py`. Expected: the first test finds six pending rows and a `reconciliation_unavailable` item.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the new file and `tests/integration/test_pdf_statement_pipeline.py tests/integration/test_multi_account_statements.py tests/integration/test_structured_import.py tests/integration/test_review_tools.py tests/integration/test_teach_layout.py tests/integration/test_ocr_fallback_review.py`. Expected: pass in lane I's own files. A test that pinned "an uncheckable statement waits" on a fixture that is in fact a clean read, or "`lines_skipped` on a statement that reconciles", is rewritten on a fixture that still waits (`no_balances_with_an_unread_line`) and named in the commit message; in a file of another lane (`test_review_tools.py::test_skipped_lines_are_highlighted_on_the_page` is the likely one, lane R) it is only named, and lane R rewrites it on that fixture.
- [ ] **Step 5:** Commit `feat(imports): import a clean read as posted and mark it not checked against a balance`.

### Task 10: One finding per cause (lane I)

**Files:**
- Modify: `backend/app/domain/imports/service.py` (`_row_problems`, `_low_confidence_review`, `_commit_statement`)
- Test: `backend/tests/integration/test_trust_policy.py`, `backend/tests/integration/test_structured_import.py`

**Interfaces:**
- `_low_confidence_review` lists only rows with a low field other than `reconciliation`; `fields` leaves `reconciliation` out; no item when no such row is left.
- The `reconciliation_failed` / `reconciliation_unavailable` payload gains `statement` (the index, None for a table), `statement_ref` (Task 24 fills it; until then the exact `statement_key` under the same name is **not** used: the key is omitted), `lines_skipped` (`run.parse_skipped`), and `cause` when the status is `mismatch`, the difference is zero or None, and a warning of `CONTRADICTION_WARNINGS` is present (the first of them in the order `summary_totals_mismatch`, `section_total_mismatch`, `rows_outside_account`).
- Near duplicates: when a statement has two or more, one `possible_duplicate` item `{"reason": "near_matches", "account_id", "statement", "count", "rows": [{"transaction_id", "candidates": [...]}]}` (rows capped at `IDS_MAX`) replaces the items per row. Exactly one keeps today's `near_match` item unchanged.

- [ ] **Step 1: Failing tests:** `test_rows_in_doubt_only_for_reconciliation_are_not_listed_twice` (tampered statement: one open item, the reconciliation one; no `low_confidence` item); `test_a_guessed_date_is_still_listed` (a statement with one ambiguous-date row and a failed reconciliation: two items, the low-confidence one lists one row and `fields == ["posted_date"]`); `test_totals_agree_but_the_summary_does_not_names_its_cause` (a card statement whose equation summary disagrees with the rows while opening + rows = closing: payload `cause == "summary_totals_mismatch"`); `test_several_near_duplicates_are_one_item` (three hand-entered rows on the dates and amounts of three statement rows, then the statement: one `near_matches` item with `count == 3`); `test_one_near_duplicate_keeps_its_item` (existing behaviour, asserted through `payload["reason"] == "near_match"`).
- [ ] **Step 2:** Run; expected: two items where one is wanted, three items where one is wanted.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `tests/integration/test_trust_policy.py tests/integration/test_structured_import.py tests/integration/test_review_api.py tests/integration/test_pdf_statement_pipeline.py`. Expected: pass in lane I's files; a test that counted the duplicate `low_confidence` item is rewritten and named in the commit message (in `test_review_api.py`, lane R's file, it is named only).
- [ ] **Step 5:** Commit `fix(imports): one review finding per cause; near duplicates of a statement in one item`.

### Task 11: A brokerage statement that cannot be checked and has no other doubt is imported (lane I)

**Files:**
- Modify: `backend/app/domain/investments/statement_import.py` (`_held`, the `record_statement` call, module doc step 3)
- Test: `backend/tests/integration/test_brokerage_pipeline.py`

**Interfaces:**
- In `_held`: `unchecked_document` no longer holds a statement by itself. When `check.status == "unavailable"` and the statement is of a document format, `reconciliation` is ignored among the doubts (as `accepted` does for `CONFIRMABLE_DOUBTS`, but only that one doubt). Any other doubt on a line or a position holds it as today, and then the whole statement is treated as today (`investment_low_confidence`).
- `record_statement(..., verification=...)`: `reconciled` when `check.status == "ok"`, `accepted` when the import ran on an "import as read" answer, else `unverified`.

- [ ] **Step 1: Failing tests:** `test_brokerage_statement_without_totals_is_imported_unverified` (a `synth_brokerage` PDF built without its summary section and totals, all rows typed: holdings and activity written, document `processed`, record `unverified`, no open item); `test_unchecked_statement_with_a_doubtful_line_is_still_held` (the same with one activity row of an unknown action: held as today); `test_import_as_read_is_recorded_accepted`; `test_reconciled_brokerage_statement_is_recorded_reconciled`.
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_brokerage_pipeline.py -k "unverified or still_held or accepted or recorded"`. Expected: the first is held with `investment_low_confidence`.
- [ ] **Step 3: Implement.** If `synth_brokerage.py` has no way to leave the totals out, add a keyword to its builder in this task (lane I owns the test file; the fixture file is edited only by adding a defaulted keyword).
- [ ] **Step 4:** Run `tests/integration/test_brokerage_pipeline.py tests/integration/test_investments_api.py tests/integration/test_name_only_securities.py tests/integration/test_positions_export_import.py`. Expected: pass; a test that pinned "an unchecked brokerage PDF waits" with no other doubt is rewritten and named.
- [ ] **Step 5:** Commit `feat(investments): import a brokerage statement that reads cleanly and prints nothing to check, marked unverified`.

### Task 12: Only the account group in doubt waits for the sign; a header and a lone total is no table (lane I)

**Files:**
- Modify: `backend/app/domain/imports/service.py` (`_sign_held`, `_import_table`, `_mapping_review`, `header_only`), `backend/app/domain/imports/statement_answers.py` (`StatementAnswers`, `document_answers`)
- Test: `backend/tests/integration/test_structured_import.py`, `backend/tests/unit/test_structured_import.py`

**Interfaces:**
- `_sign_held(on_trust, targets, doubted=False) -> list[_Target]` (was `str | None`): every target when `doubted`; else the targets whose account is a liability; empty when the sign is proven.
- `_import_table`: targets not held are committed (the suggested mapping is saved with `sign_unverified=True` when at least one target is committed). Each held target raises one `unparsed` item through `_mapping_review` with `auto_import = "sign_unverified"` and the added keys `account_id`, `group`, `transactions` (row count), `scope` (`"mapping"` when every readable target is held, else `"account"`), `mapping_id` (when a mapping exists).
- `statement_answers`: `SIGN: Final = "sign"`; `StatementAnswers.signs: dict[uuid.UUID, str]` (account id to `inflow_positive` / `outflow_positive`), read from resolved items whose resolution has `action == "answer_sign"`, `scope == "account"`, an `account_id` and a `sign`. `sign_answer(sign: str, account_id: uuid.UUID, scope: str) -> dict[str, Any]` builds the resolution keys.
- `_import_table` applies `run.answers.signs.get(account.id)`: when it differs from `spec.sign`, the target's statement is read with the other sign (`from_table_grouped(table, spec.model_copy(update={"sign": ...}))`, the same group taken from it) before hashing, and the target is not held. A mapping whose `sign_unverified` is False (a person answered for the layout) holds nothing, whatever a fresh suggestion doubts: this is "asked once per layout" for balance-only and mostly-deposit exports.
- `header_only(table)`: also true when, after the header row, every non-empty row is a total line: its first non-empty cell, case-folded, starts with `total`, `totals`, `balance`, `ending balance` or `closing balance`, and the row holds no date-shaped cell.

- [ ] **Step 1: Failing tests:**
  - `test_only_the_card_group_waits_for_the_sign` (a two-account CSV with an account-number column, one lone signed amount column, accounts: one checking, one credit card): the checking rows are imported; one open `unparsed` item with `auto_import == "sign_unverified"`, the card's `account_id` and `scope == "account"`; a mapping exists with `sign_unverified` true.
  - `test_single_card_file_is_held_whole_with_mapping_scope` (`scope == "mapping"`, nothing imported).
  - `test_account_sign_answer_is_applied_on_the_next_run` (a resolved item with `sign_answer("outflow_positive", card.id, "account")`, then `import_extraction` again: the card's purchases are negative; the checking rows keep their ids and amounts; no item open).
  - `test_answered_layout_is_not_asked_again` (a mostly-deposit export whose mapping has `sign_unverified` false: a second file with the same header imports with no item).
  - Unit: `test_header_and_a_lone_total_row_is_header_only` (`["Date", "Description", "Amount"]`, `["Total", "", "0.00"]` is true; a second row with a date is false; `["Totally Fine Shop", ...]` with a date is false); the existing single-row cases unchanged.
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_structured_import.py tests/unit/test_structured_import.py -k "sign or header_only or lone_total or asked_again"`. Expected: nothing imported in the first; `mapping_required` instead of `no_rows` in the unit case.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run both files whole and `tests/integration/test_imports_api.py tests/integration/test_document_accounts.py`. Expected: pass; tests that asserted "the whole table waits" for a mixed file are rewritten and named.
- [ ] **Step 5:** Commit `fix(imports): hold only the account group whose sign is unproven; a header with a lone total row has no rows`.

### Task 13: One card per document, with its one next action (lane R)

**Files:**
- Modify: `backend/app/domain/review/service.py` (`list_cards`, `card_action`), `backend/app/domain/review/repo.py` (`open_documents`, `NOTICE_REASONS`)
- Test: `backend/tests/integration/test_review_cards.py` (create)

**Interfaces:**
- `repo.NOTICE_REASONS` gains `same_content_skipped`.
- `repo.open_documents(db, household_id, visible, *, cursor: uuid.UUID | None, limit: int) -> list[uuid.UUID]`: ids of documents with at least one open item the caller may see, ordered by the document id, after `cursor`.
- `card_action(items: Sequence[ReviewItem]) -> tuple[CardAction, uuid.UUID]`: the first match in this order, with the item that asks for it: an `unknown_account` item gives `choose_account`; `unknown_owner` gives `choose_owner`; an `unparsed` item with `auto_import == "sign_unverified"` gives `answer_sign`; an `unparsed` item whose reason is in `MAPPING_REASONS` gives `map_columns`; `possible_duplicate` (not a notice) gives `confirm_duplicate`; a `low_confidence_field` item whose reason is in `{"reconciliation_failed", "reconciliation_unavailable", "earlier_rows_kept"}`, or `lines_skipped` / `low_confidence` / `balance_mismatch` on a document that has a stored bank statement, gives `reconcile`; any other `low_confidence_field` item that lists transactions gives `check_rows`; `receipt_match` gives `attach_receipt`; an `unparsed` item with reason `no_records` or `no_rows` on a previewable document gives `teach`; any other `unparsed` gives `reprocess`, except the reasons `format_not_supported`, `encrypted`, `depth_limit`, `container_incomplete`, which give `acknowledge`; only notices give `acknowledge`.
- `list_cards(db, principal, *, cursor, limit) -> ReviewCardPage`: one `ReviewCard` per document (items oldest first, `open` counting items that wait on a person, `notices` the rest); `loose` holds open items with no document, on the first page only; `limit` counts documents (default 25, at most 100).

- [ ] **Step 1: Failing tests:** `test_a_document_with_three_findings_is_one_card` (one uploaded statement with three open items inserted by hand, `reconciliation_failed`, `lines_skipped` and `low_confidence`: one card, three items, `action == "reconcile"` naming the reconciliation item); `test_account_comes_before_reconciliation` (a statement with no matching account and a failed check: `choose_account` naming the `unknown_account` item); `test_sign_question_is_the_action` (Task 12's payload shape, hand-inserted item); `test_notices_alone_ask_only_to_be_acknowledged`; `test_cards_page_by_document` (three documents, `limit=2`: two cards and a cursor, then one); `test_items_of_documents_the_caller_cannot_see_make_no_card`; `test_resolved_items_are_not_in_the_card`; `test_encrypted_file_is_acknowledged_not_reprocessed`.
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_review_cards.py`. Expected: an empty page from the placeholder.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the file and `tests/integration/test_review_api.py tests/security/test_authz_matrix.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(review): one card per document with its next action`.

### Task 14: Answers in one step: the sign, several near duplicates, a held brokerage line, a skipped copy (lane R)

**Files:**
- Modify: `backend/app/domain/review/service.py` (`_resolve_unparsed`, `_resolve_duplicate`, `_resolve_transaction_duplicate`, `_resolve_low_confidence`, `_import_as_read`), `backend/app/domain/review/actions.py` (new `correct_lines`), `backend/app/domain/review/assist.py` (`_account_alternatives` order)
- Test: `backend/tests/integration/test_review_api.py`, `backend/tests/integration/test_review_tools.py`

**Interfaces:**
- `_resolve_unparsed`, `action == "answer_sign"` (needs `positive_is`; `422` otherwise, and `422` when the item's `auto_import` is not `sign_unverified`): `sign = "outflow_positive"` for `money_out`, else `"inflow_positive"`. With payload `scope == "mapping"`: the mapping named by the payload, or one created from `suggested_spec` through `imports_service.create_mapping`, gets that sign through `imports_service.update_mapping` (saving a spec clears `sign_unverified`), then `_reimport(mapping_id=...)`. With `scope == "account"`: nothing is saved on the mapping; the resolution is `statement_answers.sign_answer(sign, account_id, "account")` plus `{"action": "answer_sign"}` and the document is re-imported; the importer reads the answer (Task 12). The lane's tests insert the item by hand with Task 12's payload, so they do not wait for lane I.
- `_resolve_duplicate` for `near_matches`: needs `rows`; each decision is applied with the existing single-row logic (`_resolve_transaction_duplicate` split into a helper that takes ids); decisions for transactions the payload does not name are `422`; rows left without a decision keep the item open only when `rows` covers fewer than `payload["count"]` listed rows, in which case the payload is rewritten to the rest. `decision` at the top level applies to every listed row when `rows` is None.
- `_resolve_duplicate` for `same_content_skipped`: `confirm` only closes the item; `reject` queues ingestion (`document.not_duplicate`), as `same_content` does today.
- `correct_lines(db, document, statement: int, fixes: Sequence[LineFix]) -> int`: rewrites the named activity lines of the stored investment statement (`trade_date`, `amount`, `quantity`, `type`), removes exactly the fixed field names from each line's `low_confidence`, stores the extraction; returns how many lines changed; `422` for a line the item does not list. `_resolve_low_confidence` calls it before `_import_as_read` when `line_fixes` is given on an `investment_lines_held` / `investment_low_confidence` item, then imports the lines the answer names. The statement's key changes with the stored statement, so the answer is recorded under the new key.
- `_account_alternatives`: unchanged scores; the result is sorted by score descending with ties broken by id, so the frontend can pre-select when the first is strictly ahead of the second.

- [ ] **Step 1: Failing tests:** `test_sign_answer_for_the_layout_saves_the_mapping_and_imports` (single card CSV held with `scope == "mapping"`, answer `money_out`: the mapping has `sign == "outflow_positive"` and `sign_unverified` false, a job is queued, the item is resolved); `test_sign_answer_for_one_account_leaves_the_mapping` (hand-inserted item with `scope == "account"`: the mapping's spec is unchanged, the resolution carries `sign`, `scope`, `account_id`); `test_sign_answer_on_another_item_is_422`; `test_near_matches_are_decided_in_one_request` (three rows: two `confirm`, one `reject`: two imported rows deleted with aliases recorded, one confirmed, item resolved); `test_a_decision_for_a_row_the_item_does_not_list_is_422`; `test_skipped_copy_can_be_imported_anyway` and `test_skipped_copy_is_acknowledged` (hand-inserted `same_content_skipped` item on a `rejected` document); `test_a_held_brokerage_line_is_corrected_and_imported` (`test_review_tools.py`: a held line with a doubtful `trade_date`: `line_fixes` with a date, the line is written with that date, no item open for it); `test_account_alternatives_are_ranked` (strictly descending scores).
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_review_api.py tests/integration/test_review_tools.py -k "sign_answer or near_matches or skipped_copy or held_brokerage_line or ranked or does_not_list"`. Expected: `422` unknown action; `409` kind mismatch.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run both files whole and `tests/integration/test_brokerage_pipeline.py tests/security/test_authz_matrix.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(review): answer the sign question, several near duplicates and a held line in one step`.

### Task 15: A byte-identical upload is skipped with a notice (lane D)

**Files:**
- Modify: `backend/app/domain/documents/service.py` (`record_received`)
- Test: `backend/tests/integration/test_documents.py`, `backend/tests/integration/test_documents_api.py`

**Interfaces:**
- When `duplicate_of` is set: the document is recorded with status `rejected` and `error = "duplicate"`; the review item is `possible_duplicate` with `{"reason": "same_content_skipped", "duplicate_of": <id>}`; no ingest job. `ReceivedDocument.status` reports `rejected`. The audit row keeps `duplicate_of`. A hold (`hold is not None`) is unchanged.
- The undo is the item's `reject` decision (Task 14) or `POST /documents/{id}/reprocess`, which already accepts a terminal document.

- [ ] **Step 1: Failing tests:** `test_same_bytes_again_are_skipped_with_a_notice` (second upload: status `rejected`, one open item with reason `same_content_skipped`, no job queued, the first document untouched); `test_skipped_copy_does_not_count_as_waiting` (`repo.open_items_of` for the copy is 0 once lane R's `NOTICE_REASONS` has the reason; in this lane assert the payload and status only); `test_reprocessing_the_copy_imports_it` (API: `POST /documents/{id}/reprocess` on the copy returns a job).
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_documents.py tests/integration/test_documents_api.py -k "same_bytes or skipped_copy or reprocessing_the_copy"`. Expected: status `needs_review` and reason `same_content`.
- [ ] **Step 3: Implement.** Existing tests that assert `needs_review` / `same_content` for a second upload (in `test_documents.py`, `test_documents_api.py`, `test_dropfolder.py`, `test_email_intake.py`) are rewritten to the new status and reason and named in the commit message; the drop-folder and email tests are run, and edited only on those two assertions.
- [ ] **Step 4:** Run `tests/integration/test_documents.py tests/integration/test_documents_api.py tests/integration/test_dropfolder.py tests/integration/test_email_intake.py tests/integration/test_idempotency.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(documents): skip a byte-identical upload with a notice instead of a question`.

### Task 16: What a document produced (lane D)

**Files:**
- Modify: `backend/app/domain/documents/manage.py` (`imported`, `list_documents`, `get_document`), `backend/app/api/routers/documents.py` (docstrings only if needed)
- Test: `backend/tests/integration/test_documents_api.py`

**Interfaces:**
- `imported(...)` fills `DocumentImportedOut.statements` from the document's `statement_record` rows on accounts the caller can see (ordered by account name, then period start): `transactions` and `pending` count the ledger rows with this `source_document_id` on the record's account dated inside the record's period (for a single record of the account: all of them); `unchecked = outcome == "imported" and verification == "unverified" and source_format in DOCUMENT_STATEMENT_FORMATS`; `skipped_lines` from the stored `parse.skipped_lines` (0 when absent).
- `list_documents`: `DocumentListItem.unchecked` is true when any visible record of the document is unchecked (one `EXISTS` subquery; no N+1).
- `get_document`: `previewable` as `review.service._documents` computes it (the two share a helper moved into `manage.py`: `is_previewable(document, area, pages: bool) -> bool`; lane R keeps its own call until Final Task 24 switches it).

- [ ] **Step 1: Failing tests:** `test_imported_lists_the_statements_with_their_verification` (a clean-read record inserted by hand as `unverified`/`pdf`: `unchecked` true, counts right); `test_reconciled_and_csv_records_are_not_unchecked`; `test_statements_on_invisible_accounts_are_left_out`; `test_list_marks_an_unchecked_document`; `test_detail_says_whether_pages_can_be_previewed` (a CSV: false; a PDF in permanent storage for a member: true).
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_documents_api.py -k "imported_lists or unchecked or invisible_accounts or previewed"`. Expected: empty `statements`, `unchecked` false.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `tests/integration/test_documents_api.py tests/integration/test_account_visibility.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(documents): list the statements a file produced, the accounts they went to and how they were checked`.

### Task 17: A taught layout: direction per table, several tables, pages without a header, month-name dates (lane K)

**Files:**
- Modify: `backend/tests/fixtures/synth_taught_layout.py` (new builders only), `backend/app/ingestion/parsers/template.py` (`TableTemplate`, `LayoutTemplate._check`, `MAX_TABLES`, `read_table`, `_styles`, `_transactions`, `read_template`, `PARSER_VERSION` "2" to "3", module doc)
- Test: `backend/tests/unit/test_layout_template.py`

**Interfaces:**
- `TableTemplate.direction: Literal["as_printed", "money_in", "money_out", "card"] = "as_printed"` and `TableTemplate.continues: bool = False`. Both defaulted: every stored template (`SCHEMA_VERSION` stays 1) still validates and reads as before.
- `MAX_TRANSACTION_TABLES: Final = 4`; `MAX_TABLES` becomes 5; `LayoutTemplate._check` allows up to four `transactions` tables and still one `holdings` table.
- Direction applies to an `amount` column only (separate `debit` / `credit` columns already carry it): `money_in` makes every amount positive, `money_out` negative (a printed sign or bracket on a row flips it back, so a refund in a withdrawals table is money in); `card` negates the printed sign (charges printed positive become spending).
- `continues`: on pages after the one where the header was found, when no header line is present, rows are read with the column spans of the last header found, starting at the first line whose date column holds a date, until the template's `end` text or a line that is a header of another table.
- `_styles` adds the year-less month-name styles the generic reader has (`mon_d`, `d_mon`), resolved with the statement period as `table_statement` does; a taught table with year-less dates and no period reads no rows and warns `dates_without_year` as today.
- `read_template` reads every transactions table and returns their rows in page order; each row keeps the index of its table (`TemplateOutcome.tables: list[TableRead]` already exists per table; add `table: int` to the row record used by the preview).
- Builders added to `synth_taught_layout.py` (bytes, invented): `two_tables_statement()` (a "Deposits" table and a "Withdrawals" table, both with unsigned amounts, opening and closing that reconcile only when the second is money out); `continued_table_statement()` (one table of 30 rows over two pages, header on page 1 only); `month_name_rows_statement()` (rows dated `Mar 03`, a labelled period); `card_positive_charges_statement()`.

- [ ] **Step 1: Failing tests:** `test_stored_templates_without_the_new_fields_still_parse` (a v1 template dict as stored today); `test_two_transaction_tables_with_directions_reconcile` (rows of the second table negative, `rebalance` status `ok`); `test_a_printed_minus_in_a_money_out_table_is_money_in`; `test_card_direction_negates`; `test_table_continues_on_a_page_without_its_header` (30 rows; with `continues=False` only page 1's rows); `test_a_continued_table_stops_at_its_end_text`; `test_month_name_dates_take_the_period_year`; `test_five_transaction_tables_are_refused` (validation error); `test_parser_version_is_3`.
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_layout_template.py`. Expected: validation errors ("one table of each kind"), missing rows.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the file and `tests/integration/test_teach_layout.py tests/unit -k "template or teach"`. Expected: pass; a test that pinned `layout_template/2` is updated to 3 and named.
- [ ] **Step 5:** Commit `feat(ingestion): taught layouts with a direction per table, several tables, continued pages and month-name dates`.

### Task 18: Teach shows what it will read before it is saved (lane K)

**Files:**
- Modify: `backend/app/domain/review/teach.py` (`teach`, `_Lesson`, `_read_labels`)
- Test: `backend/tests/integration/test_teach_layout.py`

**Interfaces:**
- `_read_labels` passes `direction` and `continues` of each `TeachTable` into the lesson's `TableTemplate`s; up to four transactions tables.
- `teach(..., body)` with `body.preview` true: reads the labels and the template exactly as an apply would, then returns `TeachOut` with `preview` set and **changes nothing**: no document field, no stored extraction, no layout, no review item, no job, no audit row except none. `TeachPreview`: the first 50 rows in reading order (payee text only when `may_download(db, principal)`, else None), `row_count`, the reconciliation of the taught statement (`rebalance` on the statement `fill_statement` builds, with typed opening and closing), `difference` as text, `similar_waiting` (the number of other documents `actions._similar` would reprocess for the lesson's institution and match, 0 when no institution is named), `account_id` (the request's, else the document's).
- An apply (`preview` false) is unchanged in what it writes. Its warning `rows_already_imported` stays; Final Task 23 makes the re-import replace.

- [ ] **Step 1: Failing tests:** `test_preview_returns_rows_and_changes_nothing` (`two_tables_statement()` with two taught tables: `preview.row_count` is the builder's, `reconciliation == "ok"`, the document's `extraction`, `account_id`, `institution_id`, `updated_at` and the count of `parser_fingerprint`, `review_item`, `job` and `audit_log` rows are as before); `test_preview_then_apply_reads_the_same_rows` (the applied document's stored statement has the previewed rows); `test_preview_counts_the_waiting_documents_of_the_layout` (two more uploads of the same layout in `needs_review`: `similar_waiting == 2`); `test_preview_hides_payee_text_from_a_viewer_without_downloads`; `test_wrong_direction_shows_in_the_preview` (both tables `as_printed`: `reconciliation == "mismatch"` with a difference of twice the withdrawals); `test_teach_takes_the_account_from_the_request` (existing behaviour, pinned).
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_teach_layout.py -k "preview or direction or account_from"`. Expected: `preview` is None and the document changed.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the file and `tests/integration/test_review_tools.py -k teach`. Expected: pass.
- [ ] **Step 5:** Commit `feat(review): preview what a taught layout reads before it is saved`.

### Task 19: The reconcile view (lane FR)

**Files:**
- Create: `frontend/src/features/review/reconcile.ts`, `reconcile.test.ts`, `ReconcileView.tsx`, `ReconcileView.test.tsx`, `review.css`
- Modify: `frontend/src/features/review/api.ts` (calls and keys only)
- Test: the two new test files

**Interfaces:**
- `api.ts`: `getReconciliation(documentId)`, `confirmReconciliation(documentId, body)`, `acceptReconciliation(documentId, body)`, `restoreRow(documentId, body)`, `removeEarlierRows(documentId, body)`; `reviewKeys.reconciliation(documentId)`. Row edits use the existing transactions calls (`updateTransaction`, `deleteTransaction`, `createTransaction` with `source_document_id`) imported from `features/transactions/api.ts`; each success invalidates `reviewKeys.reconciliation`.
- `reconcile.ts` (pure): `liveDifference(opening: string, closing: string, total: string, printedSign: "ledger" | "owed"): { text: string; state: "balanced" | "off" | "unchecked" | "invalid" }` (reads typed balances with thousands separators and one currency symbol, as the server does; decimal arithmetic on integer cents, no floats); `causeText(cause, currency): string`; `statusLine(statement): string`; `teachHint(edits: { flipped: number; removed: number }): string | null` (three or more of either).
- Copy, exact:
  - heading `Check against the statement`; labels `Opening balance`, `Closing balance`, `Rows in PerFin`, `Opening + rows`, `Difference`.
  - status: `Balanced.`; `Off by ${amount}.`; `No balances to check against. Enter them as printed on the statement.`
  - card hint (`printed_sign === "owed"`): `Enter the balances as printed. For a card, the amount owed is a positive number.`
  - buttons: `Confirm and record the balance` (enabled only when balanced), `Finish later`, `Accept as read`; row actions `Edit`, `Flip sign`, `Remove`; `Add a row`; `Put back` on a removed row; `Remove the rows of the earlier reading`.
  - accept confirmation: `Accept this statement as read? Its rows are posted and it is marked “accepted without balancing”. No balance is recorded.`
  - causes: `extra_row`: `One row is exactly the difference. It may be a row too many, such as a total or a balance line read as a transaction.`; `sign_flipped`: `The difference is twice this row. Its sign may be the wrong way round.`; `transposed_digits`: `The difference divides by 9. Two digits of an amount may be swapped.`; `fee_or_interest`: `A fee or interest line on the statement has the size of the difference and was not read as a row.`; `opening_differs_from_previous`: `` `The opening balance is not the closing balance of the previous statement (${amount}).` ``; `duplicated_row`: `These rows have the same date and amount. One may be a duplicate.`; `out_of_period`: `These rows are dated outside the statement period.`; `earlier_reading`: `These rows come from an earlier reading of this file. They were kept because they had been changed.`; `missing_row`: `` `No row explains the difference. A row of ${amount} may be missing.` ``
  - teach hint: `PerFin read several rows of this layout wrongly. Teach it the layout so the next statement reads right.`
  - skipped lines: `` `${n} line(s) of the statement were not read. They are marked on the page.` ``
- `ReconcileView({ documentId, statement, onDone })`: two panes (page viewer from `features/documents/PageViewer.tsx` left, used read-only; figures and the row table right). Rows named by a cause are marked and the cause list scrolls to them; choosing a row shows its box on the page. The difference line is an `aria-live="polite"` region and recomputes while balances are typed. Layout classes in `review.css` (`reconcile`, `reconcile__panes`, `reconcile__figures`, `reconcile__row--flagged`); no `style` prop.

- [ ] **Step 1: Failing tests.** `reconcile.test.ts`: `liveDifference` cases (`"1,204.18"` and `"$1,204.18"` parse; owed balances are negated before the sum; `"12.3.4"` is `invalid`; within half a cent is `balanced`); every `CauseKind` has its own sentence (iterate the kinds; none falls back); `teachHint` thresholds. `ReconcileView.test.tsx` (routed fetch mock as `ReviewTools.test.tsx`): `shows opening, closing, the rows' total and the difference`; `marks the row the first cause names`; `confirm is disabled until the difference is zero and then posts the typed balances`; `removing a row deletes the transaction and reloads the sum`; `flip sign patches the amount with the opposite sign`; `add a row posts a transaction with the document as its source`; `accept asks first and posts accept`; `finish later calls onDone without a request`; `a removed row can be put back`; `a viewer sees the figures and no actions` (`can_edit` false); `shows the card hint for a card`.
- [ ] **Step 2:** Run `npm test -- reconcile ReconcileView`. Expected: modules not found.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the two files, `npm run lint`, `npm run typecheck`. Expected: pass; `grep -n "style=" src/features/review/ReconcileView.tsx` prints nothing.
- [ ] **Step 5:** Commit `feat(review): a reconcile view with the sum, the difference, likely causes and three exits`.

### Task 20: One card per document, and only actions that do something (lane FR)

**Files:**
- Create: `frontend/src/features/review/DocumentCard.tsx`
- Modify: `frontend/src/features/review/ReviewPage.tsx`, `ReviewTools.tsx`, `reviewCopy.ts`, `api.ts`, `review.css`
- Test: `frontend/src/features/review/ReviewPage.test.tsx`, `ReviewTools.test.tsx`, `reviewCopy.test.ts`

**Interfaces:**
- `api.ts`: `listReviewCards(cursor?)` (all pages, as `listReviewItems` does); `signResolution(positiveIs)`, `nearMatchesResolution(rows)`, `lineFixResolution(lines, fixes)`; `listReviewItems` accepts `document`. `ReviewPage` reads `?document=<id>` and shows only that document's card.
- `DocumentCard({ card })`: the file line (`DocumentLine`), a heading `` `${open} thing(s) to settle` `` (or `For your information` when only notices), one section per item with its `explain(item)` title and text, and **one** primary control chosen by `card.action`:
  - `choose_account`: the account select, pre-selected when the first alternative's score is strictly above the second's or it is the only one; button `Use this account`.
  - `answer_sign`: text `Positive amounts in this file are:` and two buttons `Money in`, `Money out`; under them `PerFin remembers the answer for files with the same columns.` when the item's `scope` is `mapping`, else `The answer is kept for this account in this file.`
  - `map_columns`: link `Map columns`; `reconcile`: button `Check against the statement` opening `ReconcileView` in the dialog; `check_rows`: `LowConfidenceTools`; `confirm_duplicate`: `DuplicateBody`, and for `near_matches` a list with `Same row` / `Different row` per row and `All are the same rows`; `teach`: `Teach PerFin`; `reprocess`: `Process again`; `acknowledge`: `OK`; `attach_receipt`, `choose_owner`: today's bodies.
  - Secondary, always: `Dismiss` per section (today's dismiss), and `Teach PerFin` when the document is previewable and the action is not `teach`.
- Removed where they do nothing: `RuleDraftButton` is no longer rendered in review (the export stays for its test until the Transactions flow replaces it; delete the component and its test in this task); `LowConfidenceTools` is rendered only for an item that lists transactions; `BalanceTools` is no longer rendered (the reconcile view replaces it; the component and its tests are deleted); "Confirm as read" is not offered on an item with no pending rows; "Process again" only through the `reprocess` action.
- `HeldLines`: a held line whose doubts are among `trade_date`, `amount`, `quantity`, `type` gets `Correct` (date, amount, quantity inputs and the kind select from `KIND_WORDS`); `Import the corrected lines` sends `lineFixResolution`.
- After a card's last waiting item is resolved, the card stays until the list reloads and shows `` `Settled. ${n} row(s) are in ${account}.` `` with links `View the rows` (`/transactions?document=<id>`) and `Open the file` (`/documents/<id>`); the toast "Item resolved" is removed.
- Copy added to `reviewCopy.ts` (appended at the end of their kind's block): `possible_duplicate:near_matches` (title `Rows that may already be in PerFin`; what `` `${count} rows of this statement have the same date and amount as rows already in the account.` ``; steps `Compare each pair.`, `Say which are the same row; the others are kept as new.`); `possible_duplicate:same_content_skipped` (title `Already uploaded`; what `This file is byte for byte the same as one you uploaded before, so PerFin skipped it.`; steps `Nothing to do.`, `If you do want it read again, choose “Import it anyway”.`); `low_confidence_field:earlier_rows_kept` (title `Rows from an earlier reading`; what `` `PerFin read this file again. ${count} rows from the earlier reading are not in the new one and were kept because they had been changed.` ``; steps `Open “Check against the statement”.`, `Remove the rows that are wrong, or keep them.`). The texts of `reconciliation_failed` and `reconciliation_unavailable` are rewritten to point at the one button: steps `Open “Check against the statement”.`, `Correct, add or remove rows until the difference is zero, then confirm.`, `If the statement cannot be made to balance, accept it as read.`; with `cause === "summary_totals_mismatch"` the what-text is `The opening balance, the rows and the closing balance agree, but the statement's own summary of payments and charges does not match the rows.`

- [ ] **Step 1: Failing tests.** `reviewCopy.test.ts`: the three new `[kind, reason]` pairs in `REASONS`; `names the summary as the cause`. `ReviewPage.test.tsx` (mock `GET /api/v1/review/cards`): `shows one card for a document with three findings`; `the card's only primary button is its action` (exactly one element with the primary class inside the card); `pre-selects the account that ranks first` and `does not pre-select on a tie`; `answers the sign question in one click` (POST resolve with `action: "answer_sign"`, `positive_is: "money_out"`); `decides several near duplicates at once`; `offers no rule button and no confirm on a card with nothing pending`; `an encrypted file can only be acknowledged or dismissed`; `opens the reconcile view from the card`; `after the last answer the card says where the rows went`; `filters to one document from the query string`. `ReviewTools.test.tsx`: `corrects a held line and imports it`; the `BalanceTools` and `RuleDraftButton` tests are deleted with the components.
- [ ] **Step 2:** Run `npm test -- ReviewPage ReviewTools reviewCopy`. Expected: the new tests fail on missing elements.
- [ ] **Step 3: Implement.** Existing `ReviewPage.test.tsx` tests that mock `GET /api/v1/review` for the queue are moved to the cards endpoint (same items wrapped in one card each); their assertions on names and requests stay.
- [ ] **Step 4:** Run the three files, then `npm run lint`, `npm run typecheck`, `npm test`. Expected: pass.
- [ ] **Step 5:** Commit `feat(review): one card per document with its next action; actions that did nothing are gone`.

### Task 21: Teach PerFin: preview, direction, several tables, the account (lane FT)

**Files:**
- Modify: `frontend/src/features/review/TeachDialog.tsx`, `frontend/src/features/review/teach.ts`
- Test: `frontend/src/features/review/TeachDialog.test.tsx`

**Interfaces:**
- `teach.ts`: `Selection` for a transactions table gains `direction` (default `as_printed`) and `continues` (default false); `DIRECTION_CHOICES: readonly Choice<...>[]` with labels `As printed (the amounts carry their own sign)`, `Money in (deposits, credits)`, `Money out (withdrawals, charges)`, `Card: charges are printed as positive amounts`; `problems()` allows up to four transactions tables and one holdings table (message `A layout takes at most four transaction tables.`); `TeachOptions.accountId: string | null` and `preview: boolean`; `teachBody` sends `account_id`, `preview`, and each table's `direction` and `continues`; `previewSummary(preview): string` (`` `${row_count} rows read. ` `` then `They add up to the balances.` / `` `They differ from the balances by ${difference}.` `` / `There are no balances to check them against.`); `warningText("rows_already_imported")` becomes `The rows imported earlier from this file were replaced by this reading; rows you had changed were kept.`
- `TeachDialog`: an account select (`Account for this file`, the document's account pre-selected, `Not chosen` allowed); per transactions table a `Direction` select and a tick `The table continues on the following pages without its header`; a `Preview` button that posts with `preview: true` and shows `previewSummary`, a table of the first rows (date, payee, amount, which table), and, when `similar_waiting > 0` and "Remember this layout" is ticked, `` `${similar_waiting} other waiting file(s) with this layout will be read again.` ``; `Apply` is enabled only after a preview of the current selections (any change to a selection, the direction or the account clears the preview). The outcome block links to `/documents/learned` with the text `See what PerFin has learned`.

- [ ] **Step 1: Failing tests:** `teachBody carries the direction, the continuation and the account`; `allows a second transactions table and refuses a fifth`; `apply is disabled until a preview was shown`; `changing a direction clears the preview`; `the preview lists rows and says whether they add up`; `says how many waiting files will be read again`; `the account select starts on the document's account`; `the outcome links to what PerFin has learned`; `previewSummary` three cases.
- [ ] **Step 2:** Run `npm test -- TeachDialog`. Expected: fail on missing controls.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the file, `npm run lint`, `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(review): Teach PerFin previews its reading, takes a direction per table and the account`.

### Task 22: From a file to its rows and back (lane FD)

**Files:**
- Modify: `frontend/src/features/documents/DocumentDetailPage.tsx`, `documents/api.ts`, `documents/documents.css`, `frontend/src/features/imports/UploadPage.tsx`, `imports/api.ts`, `imports/MappingEditor.tsx`, `frontend/src/features/transactions/TransactionDialog.tsx`, `transactions/TransactionsPage.tsx`, `transactions/api.ts`
- Test: `frontend/src/features/documents/DocumentsPage.test.tsx`, `frontend/src/features/imports/UploadPage.test.tsx`, `frontend/src/features/imports/mapping.test.tsx`, and the transactions page's existing test file

**Interfaces:**
- `transactions/api.ts`: the list call accepts `document`; `TransactionsPage` reads `document` from the query string once, like `account`, and shows a removable filter chip `From one file` linking to the document.
- `TransactionDialog`: the sentence about the source becomes a link `Imported from a document: open it` to `/documents/$documentId` with `?row=<transaction id>`.
- `DocumentDetailPage`: a card `Imported from this file` fed by `GET /documents/{id}/imported`: per statement the account (link to `/accounts/$accountId`), the period, `` `${transactions} rows` `` (link `/transactions?document=<id>&account=<id>`), `` `${pending} waiting for review` `` when not zero, and one status: `Balanced against the statement` (`reconciled`), `Accepted without balancing` (`accepted`), `Not checked against a balance` (`unchecked`), nothing otherwise. Under an unchecked or accepted statement and for anyone who can edit: button `Check against the statement` opening `ReconcileView` (imported from `features/review/ReconcileView.tsx`; lane FR owns the file, this lane only renders it). `` `${skipped_lines} line(s) were not read` `` when not zero. The link "N item(s) to review" goes to `/review?document=<id>`. `Process again` button (calls the existing `reprocessDocument`, which had no caller). `Teach PerFin` is shown only when `previewable`. With `?row=`, the matching row of the reconcile view is scrolled into view when the view is opened.
- Explanation shown once under the marker: `This statement prints no balances PerFin could check its rows against. The rows read cleanly and were imported. You can enter the balances, or remove a row that does not belong.`
- `UploadPage`: the finished progress row and every file name in "Recent uploads" link to `/documents/$documentId`; a processed row's "Next step" cell shows the account as a link and `View the rows`; a document with `unchecked` shows the muted text `Not checked against a balance`; the "Map columns" link is removed from the list (the review card offers it when it applies) and `Review` links to `/review?document=<id>`.
- `MappingEditor`: after saving, and on cancel, it returns to `/documents/$documentId`; the default mapping name is the first three header cells joined by `, ` (cut to 60 characters) instead of `CSV import`.

- [ ] **Step 1: Failing tests:** documents: `lists what the file imported with its account and marker`; `offers the reconcile view on an unchecked statement and not to a viewer`; `hides Teach PerFin for a file with no pages`; `links its open questions to the filtered queue`; `process again posts reprocess`. Uploads: `links each recent upload to its document`; `a processed file names its account and links its rows`; `shows the quiet marker`; `offers no Map columns link in the list`. Mapping: `returns to the document after saving`; `names a new mapping after its header`. Transactions: `reads the document filter from the address and sends it`; `the dialog links to the source document`.
- [ ] **Step 2:** Run `npm test -- DocumentsPage UploadPage mapping TransactionsPage`. Expected: fail on missing links and text.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run those files, `npm run lint`, `npm run typecheck`. Expected: pass; no `style=` added.
- [ ] **Step 5:** Commit `feat(documents): see what a file imported, where it went, and get from a transaction back to its file`.

### Task 23: Reprocessing replaces the rows nobody touched (Final)

**Files:**
- Modify: `backend/app/domain/imports/service.py` (`_commit_statement`, `_Run`, `import_extraction`), `backend/app/domain/imports/statement_records.py` (new `drop_stale_records`), `backend/app/domain/review/reconciliation.py` (`earlier_rows`, used by Tasks 7 and 8), `backend/app/domain/review/teach.py` (the `rows_already_imported` branch)
- Test: `backend/tests/integration/test_reprocess_replace.py` (create)

**Interfaces:**
- End of `_commit_statement`: every row this call created gets `updated_at = created_at` (decision 6), after rules and payee defaults ran.
- Rows the dedupe plan lists in `removed` (Task 4) are counted in `_Run.tombstoned` (summary `tombstoned_rows`) and never inserted.
- `earlier_rows(db, document, account, statements: Sequence[ParsedStatement | Statement]) -> list[Transaction]` in `reconciliation.py`: the account's rows with this `source_document_id` and an `import_hash` that is not a hash (or alias) of any statement of the document's current reading for that account. Rows without `import_hash` (added by hand) are never earlier rows.
- After all statements of a document for an account are committed (`import_extraction`, once per account, only when at least one statement of that account was committed in this run and none of the document's statements for it was held): earlier rows that are untouched (`updated_at == created_at`), have no transfer link and no `document_link` are deleted without a tombstone (`_Run.replaced`, summary `replaced_rows`); the others are kept (`_Run.kept`, summary `kept_rows`) and one `low_confidence_field` item `{"reason": "earlier_rows_kept", "account_id", "count", "transactions": [{"transaction_id"}]}` (capped at `IDS_MAX`) is raised. Audit `import.replace` (`document`, account id, counts, at most `IDS_MAX` ids).
- `drop_stale_records(db, document, account, kept_periods: Collection[date]) -> int`: deletes the document's `statement_record` rows of that account and kind whose `period_start` this run did not record (a re-read that changed the period leaves no ghost record).
- `teach.py`: when rows are already imported and a table was taught, the document is re-imported (the `request_apply` path) instead of only warning; `rows_already_imported` is still returned so the dialog can say the earlier rows were replaced.

- [ ] **Step 1: Failing tests** (each uploads a PDF, changes the stored extraction by hand to simulate a new reading, and calls `import_extraction` or `POST /documents/{id}/reprocess` with a parser stub that returns the second reading):
  - `test_untouched_rows_the_new_reading_drops_are_removed` (reading 1 has a bogus row; reading 2 does not: the bogus row is gone, no tombstone exists, summary `replaced_rows == 1`, the other rows keep their ids).
  - `test_an_edited_row_is_kept_and_listed` (the bogus row's memo was edited: still there with its memo, `kept_rows == 1`, one `earlier_rows_kept` item naming it).
  - `test_a_confirmed_a_categorised_and_a_transfer_row_are_kept` (three rows, each changed one way).
  - `test_rows_of_another_document_on_the_account_are_untouched` and `test_rows_of_the_same_document_on_another_account_are_untouched`.
  - `test_a_row_added_by_hand_for_the_document_is_never_replaced`.
  - `test_nothing_is_replaced_while_a_statement_of_the_account_is_held` (the second reading is held as a statement overlap: every earlier row stays).
  - `test_a_deleted_row_is_not_imported_again_on_reprocess` and `test_a_deleted_row_is_not_imported_from_another_file` (the same month as OFX after the PDF: the tombstoned row stays out, `tombstoned_rows == 1`).
  - `test_one_of_two_identical_purchases_stays_deleted_through_a_reprocess` (exactly one remains; no `near_match` item).
  - `test_a_changed_period_leaves_one_statement_record`.
  - `test_imported_rows_have_equal_timestamps_after_rules_ran` (a rule categorises a row at import: `updated_at == created_at`).
  - `test_teaching_a_table_on_an_imported_statement_replaces_its_rows` (`test_teach_layout.py` helper reused).
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_reprocess_replace.py`. Expected: the bogus row survives; the deleted row comes back.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the file and `tests/integration/test_reconciliation_api.py tests/integration/test_teach_layout.py tests/integration/test_pdf_statement_pipeline.py tests/integration/test_structured_import.py tests/integration/test_multi_account_statements.py tests/integration/test_phase3_4_end_to_end.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(imports): reprocessing replaces the rows nobody touched and keeps a deleted row deleted`.

### Task 24: Answers that survive a re-read (Final)

**Files:**
- Modify: `backend/app/domain/imports/statement_answers.py` (`statement_ref`, `StatementAnswers`, `document_answers`, `placement`), `backend/app/domain/imports/service.py` (`_import_statements`, `_row_problems`, `_commit_statement`), `backend/app/domain/imports/statement_records.py` (`record_statement`), `backend/app/domain/review/service.py` (`_documents` uses `manage.is_previewable`; `_resolve_account` records the ref), `backend/app/domain/review/reconciliation.py` (resolutions carry the ref)
- Test: `backend/tests/unit/test_statement_ref.py` (create), `backend/tests/integration/test_reconciliation_api.py`, `backend/tests/integration/test_multi_account_statements.py`

**Interfaces:**
- `statement_ref(target: Target, statement: Statement | InvestmentStatement, ordinal: int) -> str`: SHA-256 in the key alphabet of the canonical JSON `[target, last4 or "", casefolded account name or "", currency or "", ordinal]` (decision 8). `statement_refs(target, statements) -> list[str]` computes the ordinals. `REF_FIELD: Final = "statement_ref"`.
- `placement(target, key, account_id, ref: str | None = None)` records both; `StatementAnswers.placed_by_ref: dict[tuple[Target, str], uuid.UUID]`; `StatementAnswers.account(target, key, ref=None)` looks up the exact key first, then the ref. `overlap` and `as_read` stay on the exact key.
- The reconciliation payload's `statement_ref` (Task 10 left it out) is filled.
- Acceptance (decision 7): `record_statement` never lowers an `accepted` record by itself. `_commit_statement` decides: when the document's record for this account and period start is `accepted`, this run inserted no row, replaced none and kept none for the account, and the statement still does not reconcile, the statement stays `accepted`: its rows are not made pending again, no reconciliation item is raised, no snapshot is written. When the run changed a row, the record goes back to what the reading says (`unverified`, with its item). When the reading now reconciles, it becomes `reconciled` and the snapshot is written. The same rule keeps a `reconciled` record that a person balanced in the view (rows edited there) from being asked about again while nothing changes.

- [ ] **Step 1: Failing tests:**
  - Unit: `test_ref_ignores_balances_rows_period_and_parser` (two statements differing only there share a ref); `test_ref_differs_by_account_digits_name_currency_and_target`; `test_two_statements_of_one_account_get_ordinals`; `test_ref_has_no_digits` (the scrubber cannot mistake it).
  - `test_a_placed_statement_keeps_its_account_after_its_balances_are_corrected` (`test_multi_account_statements.py`: place statement 2 on an account, correct statement 2's closing balance, re-run: no `unknown_account` item, same account).
  - `test_an_accepted_statement_is_not_asked_about_again` (accept, then `apply-mapping` re-run with nothing changed: record still `accepted`, no open item, no pending row, no snapshot).
  - `test_a_re_read_that_changes_a_row_asks_again` (accept, change the stored reading by one row, re-run: record `unverified`, an open `reconciliation_failed` item).
  - `test_a_re_read_that_now_reconciles_is_reconciled` (accept, correct the stored closing so it balances, re-run: `reconciled`, one snapshot).
  - `test_answering_the_second_statement_does_not_reopen_the_first` (N5: a two-account PDF, statement 1 confirmed in the reconcile view after an edit, statement 2 placed: no reconciliation item for statement 1 afterwards).
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_statement_ref.py tests/integration/test_reconciliation_api.py tests/integration/test_multi_account_statements.py -k "ref or placed or accepted or re_read or reopen"`. Expected: import error; the placement is lost; the item comes back.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the three files whole and `tests/integration/test_document_accounts.py tests/integration/test_review_api.py tests/integration/test_brokerage_pipeline.py tests/integration/test_trust_policy.py tests/integration/test_reprocess_replace.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(imports): a placement and an acceptance survive a re-read; a changed reading asks again`.

### Task 25: The policy through the real pipeline; a grace window for the period (Final)

**Files:**
- Modify: `backend/app/ingestion/parsers/confidence.py` (new constant), `backend/app/ingestion/parsers/table_statement.py` (the `posted_date` score of a row outside the period, in `read_statement`; `version` bumped by one from what 1b left), `backend/app/ingestion/parsers/template.py` (the same score in `_transactions`), `backend/app/ingestion/parsers/institutions.py` (versions bumped as 1b's Task 19 did), `backend/tests/fixtures/synth_trust.py`
- Test: `backend/tests/integration/test_trust_policy.py`, `backend/tests/unit/test_statement_period.py`, `backend/tests/security/test_parser_trust.py` (run)

This is the only task that edits parser files. It runs after 1b and 1c are merged and after every lane of this plan.

**Interfaces:**
- `confidence.PERIOD_GRACE_DAYS: Final = 10`. A row dated within that many days before the period start or after the period end keeps the score it would have inside the period; a row further out scores `conf.INFERRED` as today. The 45-day cut of year-less rows (`parsers/text.py`) is unchanged.
- `rank_causes(..., grace_days=PERIOD_GRACE_DAYS)` is what `get_reconciliation` passes (Task 7 passed 0 until now).
- Builders added: `no_balances_row_five_days_early()` (one row five days before the period start), `mixed_sign_accounts_csv()` (the two-account export of Task 12 as bytes).

- [ ] **Step 1: Failing tests** (worker path, `env` fixture):
  - `test_a_row_a_few_days_outside_the_period_is_still_a_clean_read` (`no_balances_row_five_days_early`: posted, `processed`, marker); `test_a_row_forty_days_outside_is_not` (Task 9's builder: still held).
  - Review Focus 1, end to end: `test_not_quite_clean_statements_post_nothing[...]` parametrised over the stray row, the unread line, and a section-total mismatch built from 1b's `synth_bank_layouts.section_total_off` with its balances removed: zero rows with `review_state` none, zero snapshots.
  - Review Focus 5, end to end: `test_mixed_export_holds_only_the_card_and_the_answer_leaves_checking_alone` (upload `mixed_sign_accounts_csv`, resolve the card's item with `answer_sign` / `money_out` through the API, run the job: checking rows have the same ids and amounts as before the answer; every card purchase is negative; the document is `processed`).
  - Review Focus 4, end to end: `test_card_statement_is_reconciled_from_the_card` (a tampered `demo_card_pdf`: read the card through `GET /review/cards`, delete the extra row, confirm with printed balances: snapshot written, document `processed`).
  - `test_statement_period.py`: `test_rows_within_the_grace_keep_their_score` and `test_rows_beyond_the_grace_score_inferred`.
- [ ] **Step 2:** Run; expected: the five-day row holds the statement; the others pass or fail by lane, naming the task whose rule is incomplete.
- [ ] **Step 3: Implement** the grace and the version bumps; fix what the end-to-end tests show, in the owning function, in this commit.
- [ ] **Step 4:** Run `uv run pytest -q tests/integration/test_trust_policy.py tests/unit/test_statement_period.py tests/unit/test_bank_layouts.py tests/unit/test_layout_template.py tests/integration/test_pdf_statement_pipeline.py tests/security` and 1b's statement suite. Expected: pass; the one parser-version assertion in `test_pdf_statement_pipeline.py` is updated.
- [ ] **Step 5:** Commit `feat(ingestion): ten days of grace around a statement period; the trust policy pinned through the pipeline`.

### Task 26: Control C12, documentation, schema and full gates (Final)

**Files:**
- Modify: `docs/security/CONTROLS.md` (row C12), `docs/SECURITY.md` (C12 in the threat table), `docs/DATA_MODEL.md`, `docs/UPGRADING.md`, `docs/INGESTION.md`, `docs/API.md`, `docs/PORTABILITY.md`, `CHANGELOG.md`, `backend/app/ingestion/reconcile.py` (module doc only), `backend/app/ingestion/sandbox/protocol.py` (the docstring of `Reconciliation` only; no field), `backend/openapi.json`, `frontend/src/lib/api/schema.d.ts`
- Test: `backend/tests/security/test_authz_matrix.py`, `test_input_models.py`, `tests/security/ai/test_column_classification.py`, `tests/security/mcp/test_mcp_server.py`, `tests/unit/test_portability.py`, `tests/integration/test_migrations.py` (all run, none edited here)

- [ ] **Step 1: C12.** `docs/SECURITY.md`: the mitigation reads "a statement whose own figures contradict each other is held; a statement that reads cleanly and prints nothing to check is imported and marked `unverified`, visibly and reversibly (each row deletable for good, the statement re-checkable, no balance snapshot written from it); provenance on every row; audit log". `CONTROLS.md` C12: implementation gains `app/domain/imports/service.py` (`clean_read`, `CONTRADICTION_WARNINGS`), `app/domain/review/reconciliation.py`, `app/domain/imports/tombstones.py`, `frontend/src/features/review/ReconcileView.tsx`; tests gain `tests/integration/test_trust_policy.py`, `test_reconciliation_api.py`, `test_reprocess_replace.py`; the paths of the removed `BalanceTools` are dropped. State the reasoning in two sentences: what "untrusted" meant, why "contradicted" is the line now, and what keeps a clean read reversible.
- [ ] **Step 2: Documents.** `DATA_MODEL.md` ("Documents and ingestion": `statement_record.verification`, `import_tombstone` with its columns, keys, cascade and why it holds a date and an amount). `UPGRADING.md` (a "1.0.0-rc.9" note: migration `0013`, how `verification` is backfilled, that statements imported before show "Not checked against a balance" unless a snapshot proves otherwise, that "Process again" now replaces untouched rows and keeps changed ones, that a second upload of the same bytes is skipped). `INGESTION.md` (stage 6: tombstones, replace on reprocess, near duplicates as one item, the sole-account match; stage 7: the clean-read rule and `verification`; "Review queue": cards, the reconcile view and its exits, the sign answer, the three new reasons, `lines_skipped` only when a statement does not reconcile; "Teaching PerFin a layout": directions, several tables, continuation, preview; fix the four stale places the audit lists as N12). `API.md` (the six routes, the `document` filter, the changed resolutions). `PORTABILITY.md` (the `import_tombstone` line in the not-exported table). `CHANGELOG.md` `## [Unreleased]`: Added, Changed and Fixed entries in the file's voice, one per task that a person can notice; say plainly that an uncheckable statement no longer waits and how to check it.
- [ ] **Step 3: Schema.** `make openapi`; `git diff --stat backend/openapi.json frontend/src/lib/api/schema.d.ts` shows only what Tasks 2 to 25 changed (expect no change unless a docstring moved).
- [ ] **Step 4: Full gates.** In the container: `make check` (SPDX, ruff, format, mypy, bandit, `pytest -n auto`, OpenAPI drift, frontend lint, typecheck, test, build, audit) and `mkdocs build --strict`. Check by hand: `grep -rn "style=" frontend/src/features/review frontend/src/features/documents/DocumentDetailPage.tsx` shows no new site; the MCP tool count test and the AI route count test pass unedited.
- [ ] **Step 5:** Commit `docs(security): control C12 holds contradictions, not every unchecked statement; documentation for import 1d`.

### Task 27: The measurement (Final)

**Files:** none in the repository. Commands: section 12 ("How to run it again") of the corpus baseline report kept with the corpus results; move the earlier `results.jsonl` aside first.

- [ ] **Step 1:** Re-run the corpus harness on this branch and write `derived.jsonl`.
- [ ] **Step 2:** Compare per file with the run recorded after 1c: status (`processed` / `needs_review`), review reasons, rows posted and pending, `verification`. Count: statements imported `unverified` as clean reads; documents that left Review; cards per document (must be 1). Read by eye every row of 15 clean-read statements chosen at random against the extracted lines: a wrong sign, a balance line or a total imported as a row there is a defect of this package's policy and is fixed (a contradiction signal added to `clean_read`, with a synthetic fixture) before merge.
- [ ] **Step 3:** For 5 documents that do not reconcile, open the reconcile data (`GET /documents/{id}/reconciliation`) and record whether the first cause names the real reason; a wrong first cause gets a synthetic case in `test_reconcile_causes.py`.
- [ ] **Step 4:** Put the summary into the pull request body: PDF and image statements processed without review before and after, how many of those are unverified, how many wait and why (top reasons), and what did not improve.

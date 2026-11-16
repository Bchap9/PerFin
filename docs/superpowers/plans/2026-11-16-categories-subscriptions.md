# Stage 2: categorisation and subscriptions. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A first import is mostly categorised from a merchant dictionary that ships with PerFin; every category says how it was decided and how sure PerFin is, an unsure guess is only a suggestion, and a person's choice is never overwritten; one correction teaches and "always categorise this merchant as..." is one step from the row with a true preview and an exact undo; subscriptions and bills have their own view, with habits and transfers kept out, decisions that stick and alerts on changes.

**Architecture:** A new package `app/domain/categorisation` holds four pure or near-pure parts: `cleanup` (descriptor to core, kind and processor), `dictionary` (bundled YAML, token matching), `memory` (the household's counted corrections, table `merchant_memory`) and `engine` (the decision order and the only write path for guesses). The rules engine stays where it is and gains a `merchant` condition and provenance. Subscriptions are not a new table: `scheduled_transaction` gains a kind, a status with its decider and price facts, and `subscription_event` records what changed. One migration (`0014`). Every system write to a transaction goes through `pinned_update`, which keeps `updated_at` where it was, so stage 1's "untouched since import" still means what it says. The API shapes, routes and placeholders are declared first (Task 2) and the two labelled sets are written first (Task 3), so lanes build side by side against one generated schema and are measured against data they did not write.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16, pytest; React 18, TypeScript, TanStack Query, vitest. Run the backend in the Linux dev container (`uv run ...` from `backend/`), the frontend on Node 22 (`npm ...` from `frontend/`).

**Spec:** `docs/superpowers/specs/2026-11-16-categories-subscriptions-design.md`. Evidence: the rc.8 audit of categorisation, rules and recurring detection (findings H1 to H5, N1 to N12) and the market notes on taxonomies, descriptor anatomy and subscription detection, kept with the corpus results.

**Builds on:** stage 1 merged into `main` (rc.9, Alembic head `0013`): import tombstones, `statement_record.verification`, replace-on-reprocess with "untouched = `updated_at == created_at`", review cards. Find code by the function name given, never by line number: stage 1 moved most of `app/domain/imports/service.py` (`_commit_statement`, `_mark_untouched`, `_replace_earlier`).

## What the audit says that is no longer true

- The next revision is `0014`, not `0013`.
- "Create a rule from this" in the review queue has no button any more (stage 1 removed it; `getRuleDraft` in `frontend/src/features/review/api.ts` has no caller). The route `GET /review/{item_id}/rule-draft` still exists and is left alone.
- "New columns on an exported table travel automatically": they do not. `portability.import_._decode_rows` refuses a row whose fields are not exactly the current columns. Task 17 handles this.
- Line numbers in `imports/service.py`, `transactions/service.py` and `review/actions.py` have all moved. The import has one rule hook (`_commit_statement`); there is no `commit_cash`.
- Everything else was checked and holds: `domain/rules`, `domain/recurring`, `domain/taxonomy`, `domain/notifications`, `app/mcp`, `api/ai`, `features/calendar`, `features/categories` and `features/rules` have no commit since the audit.

## Global Constraints

- Test first: write the failing test, run it and see it fail for the stated reason, then implement.
- Fixtures are synthetic. Descriptors are written by us in the shapes banks print; a merchant's public name may appear (a dictionary of real merchants cannot be tested otherwise), but store numbers, references, cities paired with people, and every person's name are invented. Never copy a line of a real statement or of the scratch corpus into the repository, a commit message or a pull request.
- Dictionary sources are limited to the list in the spec (section 2). No scraping, no export of another product's data, no enrichment service. Every merchant file starts with the SPDX comment; `data/README.md` names the sources.
- Every new file starts with the SPDX header (`# SPDX-License-Identifier: AGPL-3.0-or-later`; `//` form in TypeScript; comment form in `.css` and `.yaml`).
- Gates on the files you touch before each commit. Backend, from `backend/`: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` (strict), `uv run bandit -q -c pyproject.toml -r app`, and the tests named in the task. Frontend, from `frontend/`: `npm run lint`, `npm run typecheck`, and the tests named in the task.
- No test may be skipped or marked xfail. A test that pinned behaviour this plan changes on purpose is rewritten in the task that changes it, and the commit message names it.
- **The pinned write.** A transaction row is changed by the system only through `app.domain.categorisation.pinned.pinned_update` (Task 1), which sets `updated_at = updated_at`. No lane assigns `merchant_key`, `category_source`, `category_confidence`, `category_rule_id`, `suggested_category_id` or `scheduled_transaction_id` on an ORM `Transaction` that is already committed, except `transactions/service.py` acting for a person and `rules/service.py::write_outcome`. During an import the importer's `_mark_untouched` still runs last.
- **A person's choice is final for guesses.** No code path writes `category_id` on a row whose `category_source` is `person`, `file`, `rule`, `payee` or `legacy` except: a person (any), a rule job on request (`rule`, `payee`, `file`, `legacy` rows only, never `person`).
- No regular expression is built from dictionary data or from a descriptor. Clean-up patterns are module constants, linear, and applied to at most `PAYEE_MAX` (500) characters.
- Every request body is a Pydantic model with `extra="forbid"`. Every route has a line in `tests/security/authz_policy.py`. Every new column is classified in `app/domain/ai/classification.py`. The MCP tool count stays 19 and the AI read API stays at 19 routes; the one deliberate AI change is two fields on `RecurringAi` (Task 20).
- Audit rows, events and notification payloads carry ids, counts, dates, amounts and category ids only: never a descriptor, a merchant key, a merchant name or a payee. Notification sentences carry no name, amount, count, account or date.
- Frontend: CSP is `style-src 'self'`. No `style` prop, no inline style, no runtime style injection; layout goes into a `.css` file imported by the component. No `dangerouslySetInnerHTML`. Computed property access goes through `Map` lookups. The new screens live in the current shell; do not add, reorder or regroup navigation beyond the one entry in Task 26.
- Money over the API is decimal text. Confidence over the API is decimal text with two places.
- Conventional commits, one commit per task, ending with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`. Stage only the files of your task (`git add <paths>`), never `git add -A`.

## Decisions where the spec leaves a choice

1. **Rules stay in the rules service.** `engine.categorise_rows` calls `rules.service.apply_rules_to` (as the importer does today) and then guesses for what is still undecided. The refresh never runs rules.
2. **A suggestion is a column, not a state.** `suggested_category_id` is set only while `category_id` is NULL (a check constraint). Accepting it is a person's choice (`person`, 1.00).
3. **`legacy`.** Rows that had a category or a split before `0014` get `category_source = 'legacy'` in the migration. A guess never touches them; a rule job may, as today.
4. **Undecided** is exactly: `category_id IS NULL AND category_source IS NULL` and no split. **Re-evaluated by a refresh**: undecided rows, rows with a suggestion, and rows whose source is `dictionary` or `keyword`.
5. **The memory counts rows, not clicks.** One person-decided transaction contributes one decision; changing it moves the decision; deleting the transaction leaves the memory as it is (that is the point of a table).
6. **Merchant rules are ordinary rules** with the condition `{"type": "merchant", "key": ...}`. They count towards the 500-rule cap. Members may create and revert them; every other rule write stays admin-only.
7. **`active` stays** on `scheduled_transaction`, tied to `status` by a check. The calendar, the budget forecast, `reports/ledger.py` and the AI view are not edited for it.
8. **The data clock.** For an account, `as_of = min(today, latest posted_date on the account)`. Missed, ended, new and renewal are all computed against `as_of`.
9. **`DELETE /recurring/{id}`** on a detected series sets `dismissed` (the row stays as the tombstone); on a manual series it deletes, as today. `PATCH` with `active: false` sets `paused`, `active: true` sets `confirmed`, both by a person.
10. **Descriptor kind is not stored.** It is recomputed from `raw_payee` where needed (detection input, the teachable check). Only `merchant_key` is stored.
11. **Archive format 3.** `EntitySpec` gains `added` (column to format version). Exported: the new transaction columns except `category_rule_id` and `scheduled_transaction_id` (both point at entities written later in the archive and are re-derived), every new `scheduled_transaction` column, and `merchant_memory` (`since=3`). `subscription_event` is not exported.
12. **Processor prefixes and kind patterns are code** (`cleanup.py`); merchants and keywords are data (`data/*.yaml`).

## Out of this plan

A category model of any kind. Merchant category codes and bank category columns as priors. Auto-created payees, logos, a merchant editor, a per-merchant "do not learn". Guesses for split lines. The report by merchant and the dashboard card for subscription changes (stage 3). The new navigation and the Inbox (stage 3). Changing `GET /review/{id}/rule-draft`. Replacing the four older text normalisers (`import_hash.normalize_payee` must not move). The format 2 archives that predate `account.subtype` (Task 17 verifies and documents; it does not fix). Adopting `CategoryPicker` in the rule editor, the budget and the calendar forms (stage 3).

## Review Focus

1. **A re-read after categorisation.** Import a statement; let the engine guess; run a refresh with a bumped dictionary version; reprocess the document with a reading that drops one row. That row is removed (it was never touched), no `earlier_rows_kept` item is raised for guessed rows, and a row whose category a person changed is kept and listed (Tasks 1, 11, 12, 14, 27).
2. **A person's category under every later pass.** A row set by hand, a row cleared by hand and an accepted suggestion keep their value through: a second import of the same file, a refresh, `POST /rules/apply` with a rule that matches, a new merchant rule applied to existing rows, and a revert (Tasks 11, 13, 15, 16, 27).
3. **A `mixed` store and a household that disagrees with the dictionary.** Amazon-like rows arrive as suggestions, never facts. After the household files one merchant under two categories (share below 0.60), neither the memory nor the dictionary states a fact for it (Tasks 9, 10).
4. **A subscription with a late statement.** Twelve monthly charges, then no upload for three months: the series is still active and nothing is "missed". After the next statement arrives without the charge twice, it is ended with one alert; a dismissed series and a person-ended series are untouched by any of this, and a charge on the person-ended one raises `charged_after_end` (Tasks 18, 19, 20).
5. **A member and accounts they cannot see.** The preview counts, the "apply to existing" job, the learned list, the subscription totals and the events never include or change a row on an account outside the caller's grants; a foreign `rule_id`, `memory_id` or `schedule_id` is `404` (Tasks 12, 16, 21).

---

## Lanes

The Start tasks run first, in order. After them every lane may start at once. A lane never edits another lane's files during the parallel phase; a red test that comes from another lane's half-finished file is reported, not fixed. The Final tasks need several lanes' output and run in order after all lanes are merged.

| Lane | Tasks | Owns |
|---|---|---|
| Start (in order) | 1, 2, 3 | `backend/migrations/versions/0014_categories_subscriptions.py` (new); `app/domain/transactions/models.py`; `app/domain/categorisation/__init__.py`, `models.py`, `memory.py`, `pinned.py`, `schemas.py` (all new); `app/domain/recurring/models.py` (new); `app/domain/ai/classification.py`; every `schemas.py` named in Task 2; the placeholders and router declarations named in Task 2; `tests/security/authz_policy.py`, `tests/security/test_authz_matrix.py`; `tests/integration/test_migrations.py`, `test_ledger_models.py`, `test_merchant_memory.py` (new); `tests/fixtures/descriptors.py`, `synth_subscription_ledger.py`, `quality.py` (new), `tests/unit/test_quality_fixtures.py` (new); `backend/openapi.json`, `frontend/src/lib/api/schema.d.ts` |
| C (clean-up) | 4, 5 | `app/domain/categorisation/cleanup.py`, `tests/unit/test_merchant_cleanup.py` (new) |
| D (defaults and dictionary) | 6, 7, 8, 9 | `app/domain/transactions/categories.py`, `app/domain/taxonomy/service.py`, `app/domain/taxonomy/repo.py`, `app/domain/categorisation/dictionary.py`, `app/domain/categorisation/data/**` (new), `tests/unit/test_default_categories.py` (new), `tests/unit/test_merchant_dictionary.py` (new), `tests/integration/test_taxonomy_api.py` |
| E (engine) | 10, 11, 12 | `app/domain/categorisation/engine.py`, `service.py`, `app/api/routers/categorisation.py`, `app/workers/handlers/categorise.py` (new), `tests/unit/test_categorise_engine.py` (new), `tests/integration/test_categorise_write.py` (new), `tests/integration/test_categorisation_api.py` (new), `tests/unit/test_worker_registry.py` |
| L (ledger and importer) | 13, 14 | `app/domain/transactions/service.py`, `repo.py`, `app/api/routers/transactions.py`, `app/domain/imports/service.py`, `payees.py`, `repo.py`, `summary.py`, `tests/integration/test_transactions_api.py`, `tests/unit/test_payee_matcher.py`, `tests/integration/test_structured_import.py`, `tests/integration/test_import_categorisation.py` (new) |
| R (rules and archive) | 15, 16, 17 | `app/domain/rules/engine.py`, `service.py`, `repo.py`, `merchant.py` (new), `app/api/routers/rules.py`, `app/workers/handlers/rules.py`, `app/domain/portability/entities.py`, `schemas.py`, `codec.py`, `export.py`, `import_.py`, `tests/unit/test_rules_engine.py`, `tests/integration/test_rules_api.py`, `tests/integration/test_merchant_rules.py` (new), `tests/security/test_rules_dry_run.py`, `tests/unit/test_portability.py`, `tests/integration/test_portability.py`, `test_portability_domains.py` |
| S (subscriptions and alerts) | 18, 19, 20, 21 | `app/domain/recurring/detection.py`, `service.py`, `subscriptions.py`, `app/api/routers/recurring.py`, `subscriptions.py`, `app/workers/handlers/recurring.py`, `app/domain/notifications/routing.py`, `app/domain/events/service.py`, `app/api/ai/schemas.py`, `views.py`, `app/mcp/tools.py` (one docstring), `tests/unit/test_recurring_detection.py`, `tests/unit/test_subscription_lifecycle.py` (new), `tests/integration/test_recurring_api.py`, `test_subscriptions_api.py` (new), `test_notifications.py`, `test_event_visibility.py`, `tests/security/ai/test_ai_views.py` |
| FC (frontend categorisation) | 22, 23, 24 | `frontend/src/features/transactions/` (every file; `remember.ts`, `remember.test.tsx` and `RememberCategoryDialog.tsx` are deleted), `frontend/src/components/CategoryPicker.tsx` (new) and `components/index.ts`, `features/imports/LearnedPage.tsx`, `learned.ts`, `LearnedPage.test.tsx`, `features/categories/api.ts`, `features/rules/builder.ts`, `builder.test.ts`, `describe.ts`, `portable.ts`, `api.ts`, `RulesPage.tsx`, `RulesPage.test.tsx`, `features/settings/notifications/form.ts`, `NotificationsPage.test.tsx` |
| FS (frontend subscriptions) | 25, 26 | `frontend/src/features/subscriptions/` (new), `features/calendar/CalendarPage.tsx`, `RecurringTab.tsx`, `api.ts`, `calendar.test.tsx`, `frontend/src/app/nav.ts`, `router.tsx`, `shell.test.tsx`, `frontend/src/lib/queryKeys.ts`, `sse.ts`, `sse.test.ts` |
| Final (in order) | 27, 28, 29, 30 | everything above, `scripts/seed_dev.py`, `tests/integration/test_seed_dev.py`, `tests/integration/test_categorisation_e2e.py` (new), `test_subscriptions_e2e.py` (new), `tests/unit/test_categorisation_quality.py` (new), `tests/integration/test_subscription_quality.py` (new), `docs/`, `mkdocs.yml`, `CHANGELOG.md` |

Inside a lane the tasks run in order. One order crosses lanes: lane L (Tasks 13 and 14) calls the real `engine.person_decided`, `reconsider` and `categorise_rows` and starts after Task 11 (lane E) is committed. The frontend lanes need only Task 2 and mock the API. Everything else meets only through the names below: a lane's tests set the columns of another lane by hand (`merchant_key`, `category_source`, a `Cleaned` built in the test), and the Final tasks run the lanes together. A lane edits only its own test files: when its change turns a test red in a file another lane owns, it names the test in its commit message and the owning lane, or Task 27 at the latest, rewrites it.

**Names used across tasks.**

- Category sources (`CATEGORY_SOURCES`): `person`, `file`, `rule`, `payee`, `learned`, `dictionary`, `keyword`, `legacy`. `APPLY_MIN = Decimal("0.80")`.
- Descriptor kinds (`DescriptorKind`): `purchase`, `recurring_purchase`, `ach_debit`, `ach_credit`, `payroll`, `transfer`, `card_payment`, `loan_payment`, `cash`, `check`, `fee`, `interest`, `refund`, `p2p`, `deposit`, `unknown`. Teachable (a correction is remembered, a merchant rule may be made): `purchase`, `recurring_purchase`, `ach_debit`, `ach_credit`, `payroll`, `refund`, `unknown`.
- Merchant key form: `^[a-z0-9]+( [a-z0-9]+)*$`, at most 64 characters.
- Versions: `cleanup.CLEANER_VERSION = 1`, `dictionary.version()` (from `data/meta.yaml`, starts at 1), `categories.TAXONOMY_VERSION = 2`. Household state: setting key `categorisation.state` with `cleaner`, `dictionary`, `taxonomy`, `memory_seeded`.
- Series kinds: `subscription`, `bill`, `income`, `other`. Series statuses: `suggested`, `active`, `confirmed`, `paused`, `ended`, `dismissed`. Deciders: `detector`, `person`.
- Subscription event kinds: `new`, `price_changed`, `missed`, `ended`, `charged_after_end`, `renewal_due`, `trial_converted`. States: `open`, `seen`.
- Notification kinds, new: `subscription_new`, `subscription_price_changed`, `subscription_missed`, `subscription_renewal`. Routed events: `subscription.new`, `subscription.trial_converted` (kind `subscription_new`); `subscription.price_changed`; `subscription.missed`, `subscription.ended`, `subscription.charged_after_end` (kind `subscription_missed`); `subscription.renewal_due`.
- Job types, new: `categorise.dispatch` (hourly), `categorise.refresh` (per household). Renewals are announced inside the existing daily `recurring.detect` job.
- Audit actions, new: `transaction.categorise`, `categorise.refresh`, `memory.forget`, `rule.revert`, `subscription.decide`. Existing and reused: `rule.create`, `rule.update`, `rule.apply_requested`, `schedule.detect`.
- Live events, new: `transaction.categorised` (`account_ids`, `count`), `subscription.changed` (`account_id`, `schedule_id`).
- Problem slugs, new: `merchant-not-teachable` (422), `rule-shadowed` (409), `rule-not-revertible` (403), `memory-not-found` (404), `suggestion-missing` (bulk item reason, not a status).
- Import summary keys, new: `categorised` (object: source to count), `suggested`. Kept: `payee_categorised`, `rules_changed`.

---

### Task 1: Migration 0014, the models, the memory store, the pinned write (Start)

**Files:**
- Create: `backend/migrations/versions/0014_categories_subscriptions.py`, `backend/app/domain/categorisation/__init__.py`, `models.py`, `memory.py`, `pinned.py`, `backend/app/domain/recurring/models.py`, `backend/tests/integration/test_merchant_memory.py`
- Modify: `backend/app/domain/transactions/models.py` (`Transaction`, `Rule`, `ScheduledTransaction`), `backend/app/domain/ai/classification.py`, `backend/app/domain/portability/entities.py` (only so the suite stays green: the new columns join `exclude`, the two tables join `NOT_EXPORTED`; Task 17 replaces this), `backend/tests/unit/test_portability.py` (the allow-list)
- Test: `backend/tests/integration/test_migrations.py`, `backend/tests/integration/test_ledger_models.py`

**Interfaces:**
- `transactions/models.py`: `CATEGORY_SOURCES`, `SERIES_KINDS`, `SERIES_STATUSES`, `DECIDERS` (tuples under "Names"). `Rule` and `ScheduledTransaction` gain `household_key(...)`.
- `Transaction`: `merchant_key` (`String(64)`, null); `category_source` (`String(12)`, null, check `category_source_known`); `category_confidence` (`CONFIDENCE`, null, check `category_confidence_range` 0..1); `category_rule_id`, `suggested_category_id`, `scheduled_transaction_id` (uuid, null, plain `ForeignKey(..., ondelete="SET NULL")` plus `household_ref_fk` to `rule`, `category`, `scheduled_transaction`); check `suggestion_only_without_category` (`suggested_category_id IS NULL OR category_id IS NULL`); `Index("ix_transaction_household_merchant", "household_id", "merchant_key", postgresql_where=merchant_key IS NOT NULL)`; partial indexes `ix_transaction_schedule` and `ix_transaction_suggested`.
- `ScheduledTransaction`: `kind` (`String(12)`, not null, default `'other'`), `kind_source` and `status_source` (`String(8)`, default `'detector'`), `status` (`String(12)`, default `'active'`), `merchant_key` (`String(64)`, null), `first_seen_on`, `price_changed_on`, `ended_on` (date, null), `occurrences` (int, default 0, check `>= 0`), `confidence` (`CONFIDENCE`, null), `last_amount`, `previous_amount` (`AMOUNT`, null); checks `kind_known`, `status_known`, `deciders_known`, and `active_matches_status` (`active = (status IN ('active', 'confirmed'))`).
- `categorisation/models.py`: `MerchantMemory(UUIDPk, Timestamps, HouseholdScoped, Base)`, table `merchant_memory`: `merchant_key` (`String(64)`, check on the key form), `direction` (`String(3)`, check `IN ('out', 'in')`), `category_id` (`household_fk` to `category`, cascade), `decisions` (int, check `>= 1`), `last_decided_at` (timestamptz); `UniqueConstraint("household_id", "merchant_key", "direction", "category_id", name="uq_merchant_memory_choice")`.
- `recurring/models.py`: `SubscriptionEvent`, table `subscription_event`: `schedule_id` (`household_fk` to `scheduled_transaction`, cascade), `account_id` (`household_fk` to `account`, cascade), `kind` (`String(20)`, check), `occurred_on` (date), `amount_before`, `amount_after` (`AMOUNT`, null), `transaction_id` (null, `SET NULL`, `household_ref_fk`), `state` (`String(8)`, default `'open'`, check); `UniqueConstraint("household_id", "schedule_id", "kind", "occurred_on", name="uq_subscription_event_once")`.
- Revision `0014` (`down_revision = "0013"`): columns, keys, checks and indexes; both tables with `all_rls_ddl` and `table_grants_ddl` as `0013` does. Backfill: `category_source = 'legacy'` where `category_id IS NOT NULL` or a `transaction_split` exists; schedules: `status = 'confirmed', status_source = 'person'` where `active` and `origin = 'manual'`; `'active', 'detector'` where `active` and detected; `'paused', 'person'` where not `active`; `kind = 'income'` where `amount_min >= 0`, else `'other'`. The backfill is SQL and does not set `updated_at`. Downgrade drops in reverse.
- `pinned.py`: `PINNED_COLUMNS: Final = frozenset({"merchant_key", "category_id", "category_source", "category_confidence", "category_rule_id", "suggested_category_id", "scheduled_transaction_id"})`; `pinned_update(db: Session, household_id: uuid.UUID, changes: Mapping[uuid.UUID, Mapping[str, Any]]) -> int`: one Core `UPDATE transaction SET ..., updated_at = transaction.updated_at WHERE household_id = :h AND id = :id` per distinct column set (executemany), `ValueError` for a column outside `PINNED_COLUMNS`, the affected ORM instances expired for those columns; returns rows changed.
- `memory.py`: `Direction = Literal["out", "in"]`; `direction_of(amount: Decimal) -> Direction`; `MAX_ROWS: Final = 5000`; `STALE_DAYS: Final = 1095`; `@dataclass(frozen=True) class Learned: memory_id: uuid.UUID; category_id: uuid.UUID; decisions: int; total: int; share: Decimal; last_decided_at: datetime`; `record(db, household_id, key: str, direction: Direction, category_id: uuid.UUID, *, at: datetime) -> None` (upsert, +1; evicts the least recently decided rows beyond `MAX_ROWS`); `withdraw(db, household_id, key, direction, category_id) -> None` (-1, deleted at zero, a missing row is no error); `lookup(db, household_id, keys: Collection[tuple[str, Direction]]) -> dict[tuple[str, Direction], Learned]` (the winner per key: most decisions, then latest; `share` two places); `forget(db, household_id, memory_id: uuid.UUID) -> int` (every row of that entry's key and direction; 0 when unknown); `entries(db, household_id, *, after: uuid.UUID | None, limit: int) -> list[Learned]`; `seed_from_legacy(db, household_id) -> int` (one decision per `legacy` row with a merchant key, grouped; only keys with no entry).
- Classification: `transaction`: exposed `category_source category_confidence suggested_category_id scheduled_transaction_id`, denied `merchant_key` (new reason `R_MERCHANT_KEY = "derived from raw statement text; the payee and the category are what an assistant reads"`) and `category_rule_id` (`R_SETTINGS`). `scheduled_transaction`: exposed `kind kind_source status status_source first_seen_on occurrences confidence last_amount previous_amount price_changed_on ended_on`, denied `merchant_key`. `_deny_table("merchant_memory", R_MERCHANT_KEY, ...)`, `_deny_table("subscription_event", R_SETTINGS, ...)`.

- [ ] **Step 1: Failing tests.**
  - `test_migrations.py`: `"0014"` heads the revision list, `down_revision == "0013"`; `CATEGORISATION_TABLES = frozenset({"merchant_memory", "subscription_event"})` joins the head checks; `test_0014_backfills_sources_and_statuses_and_isolates_the_new_tables`: at `0013` insert a categorised row, an uncategorised row, a split row with no category, an active detected schedule with a negative amount, an inactive detected one, an active manual one with a positive amount; upgrade; assert `legacy`, NULL, `legacy`; `active/detector/other`, `paused/person/other`, `confirmed/person/income`; `updated_at` of the three transactions unchanged; under the app role a memory row and an event of household A are invisible to B and an insert naming B's category or schedule from A is refused; `category_source = 'maybe'`, `status = 'on'`, a suggestion together with a category, and `active = true` with `status = 'paused'` are each refused; downgrade to `0013` and upgrade again.
  - `test_ledger_models.py`: `test_pinned_update_keeps_updated_at` (a row with `updated_at == created_at`: after `pinned_update` of `merchant_key` and `suggested_category_id` the two timestamps are still equal and the ORM instance shows the new values); `test_pinned_update_refuses_other_columns` (`amount`: `ValueError`); `test_pinned_update_stays_inside_the_household`.
  - `test_merchant_memory.py`: `test_record_counts_and_lookup_names_the_winner` (2 x A, 1 x B: A, `decisions 2`, `total 3`, `share 0.67`); `test_a_tie_goes_to_the_latest`; `test_withdraw_moves_a_decision_and_deletes_at_zero`; `test_out_and_in_are_separate`; `test_forget_removes_every_category_of_the_entry`; `test_the_bound_evicts_the_oldest` (with `MAX_ROWS` patched to 3); `test_seed_from_legacy_counts_rows_and_skips_known_keys`; `test_deleting_the_category_removes_its_memory`.
  - `tests/security/ai/test_column_classification.py` and `tests/unit/test_portability.py` fail until the registrations exist (run them; no new test).
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_migrations.py -k "0014 or forced_rls or drift or single_head" tests/integration/test_ledger_models.py -k pinned tests/integration/test_merchant_memory.py tests/security/ai/test_column_classification.py tests/unit/test_portability.py`. Expected: no revision `0014`; import errors; unclassified columns.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the same, plus `tests/integration/test_rls.py`, `tests/integration/test_migrations.py` whole, `tests/integration/test_recurring_api.py` and `tests/integration/test_portability.py` (unchanged behaviour). Expected: pass.
- [ ] **Step 5:** Commit `feat(db): category provenance, merchant memory and subscription states (migration 0014)`.

### Task 2: API shapes and routes declared, placeholders, schema generated (Start)

**Files:**
- Modify: `backend/app/domain/transactions/schemas.py`, `backend/app/domain/transactions/categories.py` (two lookups and two declarations), `backend/app/domain/transactions/repo.py` (`TransactionFilters`, `filter_clauses`), `backend/app/api/routers/transactions.py` (`list_transactions`), `backend/app/domain/rules/schemas.py`, `backend/app/api/routers/rules.py`, `backend/app/domain/recurring/schemas.py`, the module that includes `reconciliation.router` (two more routers), `backend/tests/security/authz_policy.py`, `backend/tests/security/test_authz_matrix.py` (a generator for `merchant_memory`), `backend/openapi.json`, `frontend/src/lib/api/schema.d.ts`
- Create: `backend/app/domain/categorisation/schemas.py`; placeholders `cleanup.py`, `dictionary.py`, `engine.py`, `service.py` in `app/domain/categorisation/`, `app/domain/rules/merchant.py`, `app/domain/recurring/subscriptions.py`; `backend/app/api/routers/categorisation.py`, `backend/app/api/routers/subscriptions.py`
- Test: `backend/tests/integration/test_transactions_api.py`, `backend/tests/security/test_authz_matrix.py`, `test_input_models.py` (run)

Why first: the frontend lanes build against `schema.d.ts`, and no two backend lanes may edit one `schemas.py`. A lane that finds a field missing reports it; it does not add it.

**Interfaces** (response models plain `BaseModel`, request models extend the module's forbidding base):
- `transactions/schemas.py`: `CategorySource = Literal[...]`; `MerchantKey = Annotated[str, Field(pattern=r"^[a-z0-9]+( [a-z0-9]+)*$", max_length=64)]`. `TransactionOut` gains, all defaulting to `None`: `merchant_key: str | None`, `merchant: str | None` (display name), `category_source: CategorySource | None`, `category_confidence: str | None`, `category_rule_id`, `suggested_category_id`, `subscription_id` (uuid or None). `BulkActionName` gains `"accept_suggestions"` (no `category_id`, no `tag_ids`).
- `GET /transactions` gains `category_source: CategorySource | None`, `suggested: bool | None`, `merchant: MerchantKey | None`, `subscription: uuid.UUID | None`; `TransactionFilters` gains `category_source`, `suggested`, `merchant_key`, `schedule_id`; `filter_clauses` implements all four here, whole (`suggested=True` is `suggested_category_id IS NOT NULL`). `to_out` fills the stored fields; `merchant` stays None until Task 13.
- `categorisation/schemas.py`: `SourceCount`: `source: CategorySource | None`, `transactions: int`. `CategorisationSummary`: `dictionary_version: int`, `dictionary_merchants: int`, `cleaner_version: int`, `up_to_date: bool`, `by_source: list[SourceCount]`, `uncategorised: int`, `suggestions: int`, `learned_merchants: int`. `LearnedCategory`: `id`, `merchant_key`, `merchant: str`, `direction: Literal["out", "in"]`, `category_id`, `decisions: int`, `total: int`, `share: str`, `last_decided_at`, `rule_id: uuid.UUID | None`, `offer_rule: bool`, `example_transaction_id: uuid.UUID | None`. `LearnedCategoryPage`: `items`, `next_cursor: uuid.UUID | None`.
- `rules/schemas.py`: `MerchantCondition`: `type: Literal["merchant"]`, `key: MerchantKey`; it joins the leaf condition union. `MerchantRulePreviewIn`: `transaction_id`, `category_id`. `MerchantRuleIn`: the same plus `apply_to_existing: bool = False`. `MerchantRulePreview`: `merchant_key: str | None`, `merchant: str | None`, `teachable: bool`, `reason: Literal["no_merchant", "not_a_purchase", "split"] | None`, `uncategorised: int`, `guessed: int`, `decided: int`, `same_category: int`, `existing_rule_id: uuid.UUID | None`, `shadowed_by: uuid.UUID | None`, `sample: list[uuid.UUID]` (at most 20). `MerchantRuleOut`: `rule: RuleOut`, `replaced: bool`, `previous_category_id: uuid.UUID | None`, `job_id: uuid.UUID | None`. `RuleRevertOut`: `rule_id`, `reverted: int`, `job_id: uuid.UUID | None`.
- `recurring/schemas.py`: `SeriesKind`, `SeriesStatus`, `Decider`, `EventKind` literals. `ScheduleOut` gains `kind`, `status`, `status_source`, `merchant: str | None`. `ScheduleUpdate` gains `kind: Literal["subscription", "bill", "income", "other"] | None = None`. `PriceChange`: `previous: Money`, `current: Money`, `on: date`, `pending: bool`. `SubscriptionOut`: `id`, `account_id`, `name`, `merchant: str | None`, `kind`, `kind_source`, `status`, `status_source`, `origin`, `cadence`, `interval_count`, `amount: Money`, `amount_min: Money`, `amount_max: Money`, `monthly_cost: Money`, `yearly_cost: Money`, `next_due: date | None`, `overdue: bool`, `first_seen_on: date | None`, `last_seen_on: date | None`, `occurrences: int`, `confidence: str | None`, `category_id`, `payee_id`, `price_change: PriceChange | None`, `open_events: int`. `SubscriptionTotals`: `currency`, `monthly: Money`, `yearly: Money`, `count: int`, `unconverted: list[uuid.UUID]`. `SubscriptionList`: `as_of: date`, `subscriptions: SubscriptionTotals`, `bills: SubscriptionTotals`, `items: list[SubscriptionOut]`, `needs_look: int`. `SubscriptionCharge`: `transaction_id`, `posted_date`, `amount: Money`. `SubscriptionEventOut`: `id`, `schedule_id`, `name`, `kind: EventKind`, `occurred_on`, `amount_before: Money | None`, `amount_after: Money | None`, `transaction_id: uuid.UUID | None`, `state: Literal["open", "seen"]`. `SubscriptionDetail`: `subscription`, `charges: list[SubscriptionCharge]` (at most 60, newest first), `events: list[SubscriptionEventOut]`. `SubscriptionEventPage`: `items`, `next_cursor: uuid.UUID | None`, `open: int`. `SubscriptionDecision`: `decision: Literal["confirm", "dismiss", "pause", "resume", "end", "reopen"]`, `kind: Literal["subscription", "bill"] | None = None`. `EventsSeen`: `ids: list[uuid.UUID]` (1 to 200, unique). `EventsSeenOut`: `seen: int`.
- Routes and policy lines (`V`, `M`, `A` as in `authz_policy.py`; scopes `transactions:read` / `transactions:write`):
  - `GET /categorisation/summary` V; `GET /categorisation/learned` V (`cursor`, `limit` 1..200); `DELETE /categorisation/learned/{memory_id}` M, params `{"memory_id": "merchant_memory"}` (new `ParamKind("merchant_memory", "memory-not-found")`, in `HOUSEHOLD_OBJECT_KINDS`); `POST /categorisation/refresh` A, returns `JobAccepted`.
  - `POST /rules/merchant/preview` M; `POST /rules/merchant` M (201, `idempotent(...)`); both declared **above** `/rules/{rule_id}`. `POST /rules/{rule_id}/revert` M, params `RULE`.
  - `GET /subscriptions` V (`kind`, `status`, `account` filters); `GET /subscriptions/events` V (`state`, `cursor`, `limit`) and `POST /subscriptions/events/seen` M, both declared **above** `GET /subscriptions/{schedule_id}` V, params `SCHED`; `POST /subscriptions/{schedule_id}/decision` M, params `SCHED`.
- Placeholders, each real enough to import and type-check: `cleanup.py` declares `CLEANER_VERSION`, `DescriptorKind`, `TEACHABLE`, `@dataclass(frozen=True) class Cleaned: core: str; kind: DescriptorKind = "unknown"; via: str | None = None; signals: frozenset[str] = frozenset()` and `clean(raw: str | None) -> Cleaned` returning the lower-cased `a-z0-9` tokens as `core`; `dictionary.py` declares `Merchant`, `Hit`, `KeywordHit` (Task 7) with `lookup` and `keyword` returning None, `version()` 1, `name_for` None; `engine.py` declares `APPLY_MIN`, `Identity`, `Decision`, `Summary` and `identify` (an `Identity` from `clean` with no hit), `display_name` (title case), `categorise_rows`, `reconsider`, `person_decided` (Tasks 10, 11; they write nothing and return an empty `Summary`); `rules/merchant.py` declares `merchant_rule(db, household_id, key) -> Rule | None` returning None; `transactions/categories.py` gains, whole, `category_ids_by_key(db, household_id) -> dict[str, uuid.UUID]` and `resolve(by_key: Mapping[str, uuid.UUID], key: str) -> uuid.UUID | None` (the key, else its group's key, else None), and declares `TAXONOMY_VERSION: Final = 1` and `add_new_defaults(db, household_id, since: int) -> int` returning 0 (Task 6 makes them 2 and real); the functions of `service.py`, `rules/merchant.py` and `recurring/subscriptions.py` behind the new routes raise `ProblemError(409, "not-ready", ...)` after the caller's household and object checks, except the two lists, which return empty pages, and `summary`, which returns zeros.

- [ ] **Step 1: Failing tests** in `test_transactions_api.py`: `test_resolve_falls_back_to_the_group_then_to_nothing` (a unit case in this file's module for the two lookups), `test_filter_by_category_source`, `test_filter_suggested_only`, `test_filter_by_merchant_key` (two keys on one account; a key on an account the caller cannot see gives an empty page, never `403`), `test_filter_by_subscription`, `test_provenance_fields_are_returned` (columns set through the session: the response repeats them; `category_confidence` is `"0.90"`).
- [ ] **Step 2:** Run them and `uv run pytest -q tests/security/test_authz_matrix.py tests/security/test_input_models.py`. Expected: unknown query parameters ignored; fields missing.
- [ ] **Step 3: Implement**, then `make openapi`.
- [ ] **Step 4:** Run the three files again, `uv run perfin openapi | diff -u openapi.json -`, `uv run pytest -q tests/security/mcp/test_mcp_server.py tests/security/ai/test_ai_documents.py` (19 and 19), and in `frontend/`: `npm run typecheck`. Expected: pass, no drift, the frontend still compiles.
- [ ] **Step 5:** Commit `feat(api): shapes and routes for category provenance, merchant rules and subscriptions`.

### Task 3: The two labelled sets and the measuring functions; the "today" figures (Start)

**Files:**
- Create: `backend/tests/fixtures/descriptors.py`, `backend/tests/fixtures/synth_subscription_ledger.py`, `backend/tests/fixtures/quality.py`, `backend/tests/unit/test_quality_fixtures.py`

Why first, and by whom: the sets are the yardstick for lanes C, D, E and S. They are written before those lanes start and not by the agents who write the dictionary or the detector. No lane edits them; a label a lane believes wrong is reported to the coordinator.

**Interfaces:**
- `descriptors.py`: `@dataclass(frozen=True) class Labelled: text: str; group: Literal["known", "invented", "non_purchase"]; merchant: str | None; category: str | None; kind: str; sign: Literal[-1, 1] = -1; private: tuple[str, ...] = ()`. `merchant` is a grouping label only (lines with one label must get one key; the key's spelling is not prescribed). `category` is a default `system_key` after Task 6 (the 68 keys are listed in the spec, section 1) or None ("must not be a fact"). `private` holds invented personal names that must never reach a core. `LABELLED: Final[tuple[Labelled, ...]]`: exactly 800 `known` (at least 250 distinct merchants; each in at least two of six shapes: bare, `POS DEBIT` with store number and city/state, `PURCHASE AUTHORIZED ON mm/dd ... CARD nnnn`, `CHECKCARD mmdd`, labelled ACH (`DES:` / `ORIG CO NAME:`), processor-fronted), 250 `invented` (local shops, half with a telling word such as PIZZA or PHARMACY and that category expected as a suggestion at most, half with none), 150 `non_purchase` (transfers, card payments, ATM, fees, interest, payroll, generic refunds, cheques, person-to-person with `private` names).
- `synth_subscription_ledger.py`: `TODAY: Final = date(2026, 10, 31)`; `@dataclass(frozen=True) class Row: account: str; posted: date; amount: Decimal; text: str; category: str | None`; `Planted`: `name`, `account`, `kind: Literal["subscription", "bill"]`, `cadence`, `expect: Literal["active", "suggested"]`, `price_change_on: date | None`, `stopped_after: date | None`; `Decoy`: `name`, `account`; `build() -> Ledger(rows, planted, decoys, coverage: dict[str, date])`. 40 planted (22 monthly fixed, 4 annual of which 1 seen once, 3 weekly, 2 quarterly, 1 two-monthly, 2 plans at one opaque merchant, 5 with a price change, 4 stopped, 1 trial, 3 variable utilities, rent, a loan; the counts overlap) and 25 decoys (weekly groceries at one store, a coffee habit, weekly fuel, a monthly transfer to savings, a card payment, rent by a person-to-person service, a monthly restaurant, salary is not a decoy: it is `income`). One account has no rows in the last three months (`coverage`) although its series run on.
- `quality.py`: `categorisation_metrics(identify: Callable[[str], tuple[str | None, str]], decide: Callable[[Labelled], tuple[str | None, bool]]) -> CategorisationMetrics` (fields named after the rows of the spec's table: `known_fact_right`, `fact_precision`, `suggestion_precision`, `invented_wrong_fact`, `fact_coverage`, `one_key_per_merchant`, `kind_right`, `private_leaks`); `subscription_metrics(found: Sequence[FoundSeries], ledger: Ledger) -> SubscriptionMetrics` (`found`, `found_active`, `decoys_active`, `decoys_suggested`, `kind_right`, `price_changes_flagged`, `price_changes_false`, `ended_right`, `ended_wrong`); `python -m tests.fixtures.quality --today` prints both, using the code as it is now: categories by importing the 1,200 lines as a CSV into an empty household of a scratch database (`tests/fixtures/scratch_db.py`) and reading back `category_id`, series through the current `recurring.detection.detect` on the ledger's rows.

- [ ] **Step 1: Failing tests** in `test_quality_fixtures.py`: `test_the_descriptor_set_has_its_shape` (counts per group, distinct merchants, every line at most 500 characters, no duplicate text, every `category` in the spec's key list, every `non_purchase` kind a `DescriptorKind`); `test_no_private_name_is_a_merchant_label`; `test_the_ledger_is_deterministic` (two builds are equal); `test_the_ledger_has_its_plants_and_decoys` (40 and 25; every planted series has at least one row; the gap account's last row is before `TODAY - 90 days`); `test_metrics_on_a_perfect_and_an_empty_answer` (a `decide` that returns the label scores 100% and 0 leaks; one that returns nothing scores 0% coverage and no precision).
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_quality_fixtures.py`. Expected: import error.
- [ ] **Step 3: Implement** the three modules.
- [ ] **Step 4:** Run the file; then `uv run python -m tests.fixtures.quality --today` and keep its output for the commit message and the pull request ("today" column of the spec's table).
- [ ] **Step 5:** Commit `test(quality): labelled descriptors and a planted subscription ledger, with today's figures`.

### Task 4: Clean-up, first half: canonical text, the kind, labelled ACH, bank trailers (lane C)

**Files:**
- Modify: `backend/app/domain/categorisation/cleanup.py`
- Test: `backend/tests/unit/test_merchant_cleanup.py` (create)

**Interfaces** (pure, no database):
- `canonical(raw: str | None) -> str`: NFKC, upper case, control and format characters removed, whitespace collapsed, cut at 500 characters.
- `KIND_PREFIXES: Final[tuple[tuple[re.Pattern[str], DescriptorKind, str | None], ...]]`: anchored prefix patterns in the order of the market notes' step 1 (POS and card purchase forms, `CHECKCARD mmdd`, `PURCHASE AUTHORIZED ON mm/dd`, `RECURRING PAYMENT AUTHORIZED ON` and the `RECURRING` forms giving `recurring_purchase` and the signal `recurring`, ACH and pre-authorised debit and credit, online and mobile payment, transfer to and from, the person-to-person services, ATM, deposits and cheques, payroll and direct deposit), plus whole-line patterns for `fee`, `interest`, `refund`, `card_payment` (`PAYMENT THANK YOU`, `AUTOPAY`, the card issuers' payment descriptors) and `loan_payment`.
- `_ach(text) -> tuple[str, frozenset[str]] | None`: `ORIG CO NAME:<name> ...` gives the name and the signal `descr:<co entry descr>`; `<name> DES:<descr> ...` gives the text before `DES:`; the `IND NAME:` / `INDN:` value is dropped; generic `PPD|WEB|CCD|TEL ID:`, `TRACE#`, `REF#`, `CONF#`, `AUTH#` are removed.
- Trailers removed: `CARD nnnn`, `CARD ENDING IN nnnn`, `XXXXnnnn`, a 16-character reference of one letter and 15 digits, `mm/dd` and `mm/dd/yy(yy)`, `hh:mm(:ss)`, a final `US` or `USA`.
- After this task `clean` returns the kind, the signals and a core that still holds processor prefixes and tails (Task 5).

- [ ] **Step 1: Failing tests** (inputs invented, expectations exact):
  - `test_kind[...]`: `POS DEBIT 0042 QUIET BEAN COFFEE` purchase; `RECURRING PAYMENT AUTHORIZED ON 04/12 STREAMBOX.COM` recurring_purchase with signal `recurring`; `ONLINE TRANSFER TO SAV ...1234` transfer; `ZELLE PAYMENT TO JORDAN SAMPLE` p2p; `ATM WITHDRAWAL 004512 MAIN ST` cash; `MONTHLY MAINTENANCE FEE` fee; `INTEREST PAYMENT` interest; `DIRECT DEP MAPLE STUDIO PAYROLL` payroll; `PAYMENT THANK YOU - WEB` card_payment; `CHECK # 1042` check; `MOBILE CHECK DEPOSIT` deposit; `QUIET BEAN COFFEE` unknown.
  - `test_labelled_ach_keeps_the_company_and_drops_the_person`: `OAKFIELD RENTALS DES:RENT ID:88123 INDN:JORDAN SAMPLE CO ID:XXXXX12345 PPD` has kind `ach_debit`, signal `descr:rent`, and neither `jordan` nor `sample` nor a digit run in the core.
  - `test_trailers_are_removed`: `PURCHASE AUTHORIZED ON 04/12 HARBOR FUEL 0091234 SPRINGFIELD IL S385102345678901 CARD 1234` keeps no `04/12`, no reference and no `card`.
  - `test_empty_and_none`; `test_long_and_hostile_input_is_linear` (500 characters of `A *` repeated, and of nested separators: under 50 ms each).
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_merchant_cleanup.py`. Expected: every kind is `unknown`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run again. Expected: pass.
- [ ] **Step 5:** Commit `feat(categorisation): read a descriptor's kind and strip what the bank added`.

### Task 5: Clean-up, second half: processors, tails, numbers, the core (lane C)

**Files:**
- Modify: `backend/app/domain/categorisation/cleanup.py`
- Test: `backend/tests/unit/test_merchant_cleanup.py`

**Interfaces:**
- `PROCESSORS: Final[tuple[tuple[str, str], ...]]`: literal prefix and the `via` value, longest first: `SQ *SQ *`, `SQ *`, `GOSQ.COM` (square); `TST*`, `TST *` (toast); `PAYPAL *`, `PP*` (paypal); `DD *`, `DOORDASH*` (doordash); `IC* ` (instacart); `GH *`, `GRUBHUB*` (grubhub); `PADDLE.NET*` (paddle); `FS *` (fastspring); `DRI*` (digitalriver); `BT*` (braintree); `WPY*` (wepay); `IN *` (intuit); `EB *` (eventbrite); `ACT*` (active); `ABC*` (abc); `ACI*` (aci); `SP * `, `SP ` (shopify, only when at least two tokens follow). Not processors, kept as the merchant: `APPLE.COM/BILL`, `GOOGLE *`, `AMZN`, `AMAZON`, `UBER *`, `LYFT *`, `MICROSOFT*`.
- Tails removed, in this order: phone numbers (delimited and the undelimited toll-free forms); a ZIP; a final two-letter token that is a state, DC or PR, and with it up to two tokens of city when at least two tokens remain; store, terminal and order numbers (`#123`, `STORE 123`, `STR 123`, `T-1234`, `F12345`, a `*` followed by six or more letters and digits, a final run of three or more digits). A token `NAME.COM|NET|ORG|IO|CO|TV|US` loses its ending unless a `/path` follows; `WWW.` goes.
- Final: `INC|LLC|CORP|CO|LTD|LLP` at the end, possessive `'S` and ` S `, every character outside `a-z0-9` becomes a space; lower case; at most `CORE_TOKENS_MAX = 6` tokens and 64 characters. For kind `p2p` the core is the service name only. For `transfer`, `card_payment`, `cash`, `check`, `fee`, `interest`, `deposit` the core is `""`.
- `clean(raw) -> Cleaned`: complete.

- [ ] **Step 1: Failing tests:**
  - `test_core[...]`: `POS DEBIT 0042 QUIET BEAN COFFEE` gives `quiet bean coffee`; `Card Purchase 01/14 Corner Grocer #204 Springfield IL Card 7731` gives `corner grocer`; `SQ *CORNER GROCER 0412 SPRINGFIELD IL` gives `corner grocer`, via `square`; `TST* HARBOR PIZZA - SPRINGFIELD` gives `harbor pizza springfield` or `harbor pizza` (pin the one implemented; no state, so the city may stay), via `toast`; `PAYPAL *STREAMBOX 402-935-7733 CA` gives `streambox`, via `paypal`; `STREAMBOX.COM` gives `streambox`; `STREAMBOX *MONTHLY` gives `streambox monthly`; `CHECKCARD 0412 LANTERN BOOKS #123 SPRINGFIELD IL` gives `lantern books`; `APPLE.COM/BILL 866-712-7753 CA` gives `apple com bill`, via None; `AMZN MKTP US*7T4TU7Z13 AMZN.COM/BILL WA` gives `amzn mktp us`; `MAPLE BAKERY LLC` gives `maple bakery`; `TRADER JOE S #123` gives `trader joe`.
  - `test_the_five_examples_the_old_dialog_pinned` (the audit's N1 table: each core is non-empty and each of its tokens occurs in the folded source text).
  - `test_p2p_core_is_the_service` (`ZELLE PAYMENT TO JORDAN SAMPLE`: `zelle`); `test_non_purchases_have_no_core`.
  - `test_core_form` (property test over `descriptors.LABELLED`: every core matches the key form or is empty).
  - `test_kinds_on_the_labelled_set`: over the 150 `non_purchase` lines, `kind` equals the label for at least 97%; over all 1,200 lines no `private` token is in a core.
- [ ] **Step 2:** Run. Expected: cores still hold prefixes and tails.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `tests/unit/test_merchant_cleanup.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(categorisation): strip processors, places and numbers down to the merchant`.

### Task 6: Default categories, second edition; keys follow a merge (lane D)

**Files:**
- Modify: `backend/app/domain/transactions/categories.py`, `backend/app/domain/taxonomy/service.py` (`merge_category`), `backend/app/domain/taxonomy/repo.py` (the tables a merge and a delete re-point)
- Test: `backend/tests/unit/test_default_categories.py` (create), `backend/tests/integration/test_taxonomy_api.py`

**Interfaces:**
- `TAXONOMY_VERSION` becomes 2; `DefaultCategory.since: int = 1`. The 13 leaves of the spec (section 1) with `since=2` and these names: Fast food, Alcohol and bars, Food delivery, General merchandise, Home and garden, Games, News and books, Software and apps, Memberships, Other utilities, Home improvement, Financial and legal services, Cash and ATM (`transfers.cash`, kind transfer). `leisure.subscriptions` is named `Streaming` for households seeded from now on.
- `add_new_defaults(db, household_id, since: int) -> int`: for each default leaf with `since > since_arg`: skipped when the household has no category with the parent's key; when a category with the same name (case-insensitive) exists under that parent and has no key, it takes the key; else inserted. Never touches a leaf with `since <= since_arg`. `seed_default_categories` is unchanged for a new household (all 68).
- `category_ids_by_key` and `resolve` exist since Task 2 and are not changed.
- `merge_category`: when the source has a `system_key` and the target has none, the target takes it. The merge re-points `transaction.suggested_category_id` and folds `merchant_memory` rows into the target (decisions added, the later `last_decided_at`); a delete relies on the foreign keys.

- [ ] **Step 1: Failing tests:** `test_the_default_set_has_68_leaves_in_12_groups`; `test_keys_of_the_first_edition_did_not_change` (the 55 keys of today, written out in the test); `test_cash_card_payment_loan_and_transfers_are_transfer_kind`; `test_a_new_household_gets_all_of_them`; `test_an_existing_household_gets_only_the_new_ones` (delete `food.coffee`, call `add_new_defaults(since=1)`: 13 added, `food.coffee` still absent); `test_a_removed_group_gets_nothing`; `test_a_same_named_category_takes_the_key`; API: `test_merge_moves_the_key_the_suggestions_and_the_memory`.
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_default_categories.py tests/integration/test_taxonomy_api.py -k "merge or default"`. Expected: 55 leaves; attribute errors.
- [ ] **Step 3: Implement.** A test elsewhere that counts 55 defaults is named in the commit message.
- [ ] **Step 4:** Run both files and `tests/integration/test_seed_dev.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(categories): thirteen more default categories and keys that survive a merge`.

### Task 7: The dictionary's format, loader, matcher and gate (lane D)

**Files:**
- Create: `backend/app/domain/categorisation/data/meta.yaml`, `data/README.md`, `data/merchants/streaming.yaml` (a first file of about 40 entries), `data/keywords.yaml`
- Modify: `backend/app/domain/categorisation/dictionary.py`
- Test: `backend/tests/unit/test_merchant_dictionary.py` (create)

**Interfaces:**
- A merchant entry: `key` (key form, unique across files), `name`, `category` (a default key), `match` (list of strings in key form; a string ending in ` ~` matches anywhere, else as a prefix of the core), optional `series: subscription | bill`, `cadences: [monthly, annual, ...]`, `mixed: true`, `inflow: true`. A keyword entry: `word` (one or two tokens), `category`, optional `strong: true`, optional `via` (applies only behind that processor).
- `@dataclass(frozen=True) class Merchant: key: str; name: str; category: str; series: Literal["subscription", "bill"] | None = None; cadences: tuple[str, ...] = (); mixed: bool = False; inflow: bool = False`; `Hit: merchant: Merchant; how: Literal["exact", "prefix", "anywhere"]`; `KeywordHit: category: str; word: str; strong: bool`.
- `lookup(core: str) -> Hit | None`: whole tokens only; candidates by first token (prefix) and by any token (anywhere); the match with the most tokens wins; `exact` when it covers the core; two longest matches of different merchants give None. `keyword(core: str, via: str | None) -> KeywordHit | None`: whole tokens; a `via` keyword first; the first word in file order wins. `version() -> int`, `merchants() -> Mapping[str, Merchant]`, `name_for(key: str) -> str | None`, `digest() -> str` (SHA-256 of the canonical JSON of all entries). Loaded once (`functools.cache`), with `yaml.safe_load`; a malformed file raises at import of the first call, with the file and the entry named.
- `meta.yaml`: `version: 1` and `digest:` (the value `digest()` returns).

- [ ] **Step 1: Failing tests:** `test_every_entry_is_well_formed` (schema, key form of `key` and every `match`, unique keys, a `match` owned by one merchant only, every category in `default_category_keys()`, cadences from the six names, no `match` equal to a keyword or to a processor's letters); `test_the_digest_is_pinned_to_the_version` (`meta.yaml` digest equals `digest()`; the failure message says "bump `version` and update `digest`"); `test_lookup_prefix_and_exact` (`netflix` exact; `netflix com` prefix); `test_the_longer_match_wins`; `test_two_merchants_equally_long_is_no_match` (with a table built in the test); `test_anywhere_needs_the_marker`; `test_whole_tokens_only` (`netflixx` is no match); `test_keyword_and_via_keyword` (`harbor pizza` gives `food.restaurants`, not strong; anything behind `toast` gives `food.restaurants`, strong); `test_yaml_is_loaded_safely` (a file with a Python tag is refused); `test_lookup_is_fast` (10,000 lookups under one second).
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_merchant_dictionary.py`. Expected: `lookup` returns None.
- [ ] **Step 3: Implement.** `README.md`: the format, the allowed sources and the excluded ones word for word from the spec, the BSD-3-Clause notice of `name-suggestion-index` if a line was taken from it, and how to add a merchant (edit, bump, run the test).
- [ ] **Step 4:** Run again, and `uv run python -c "from app.domain.categorisation import dictionary as d; print(d.version(), len(d.merchants()))"` from an installed wheel layout (the data files are inside the package). Expected: pass; `1 40` or so.
- [ ] **Step 5:** Commit `feat(categorisation): a bundled merchant dictionary: format, matcher and its gate`.

### Task 8: Everyday merchants (lane D)

**Files:**
- Create: `data/merchants/groceries.yaml`, `restaurants.yaml`, `fuel_transport.yaml`, `shopping.yaml`, `home.yaml`, `health.yaml`, `travel.yaml`, `family_education.yaml`, `financial.yaml`
- Modify: `data/meta.yaml`
- Test: `backend/tests/unit/test_merchant_dictionary.py`

**Content rules:** national and large regional US chains and services, each with the descriptor forms a statement prints (the squashed and abbreviated ones too: one `match` each). Sub-brands that change the category are their own entries with longer matches (a warehouse club's fuel, a grocer's pharmacy, a ride service's food delivery, a marketplace's membership and digital services). General stores are `mixed: true`. Payroll processors are `inflow: true` with `income.salary`. No entry for a person-to-person service, a card issuer's payment or a bank. At least 600 merchants after this task.

- [ ] **Step 1: Failing tests:** `test_size_after_everyday_merchants` (at least 600); `test_sector_floors` (groceries 60, restaurants and fast food 120, fuel and transport 50, shopping 120, health 40, travel 50, home 40); `test_mixed_stores_are_marked` (a written list of twelve); `test_sub_brands_win_over_the_brand` (ten pairs written in the test); `test_no_entry_for_p2p_or_card_issuers` (a written deny-list of cores gives None).
- [ ] **Step 2:** Run. Expected: about 40 merchants.
- [ ] **Step 3: Write the entries** (version 2, new digest).
- [ ] **Step 4:** Run the file. Expected: pass.
- [ ] **Step 5:** Commit `feat(categorisation): everyday merchants in the dictionary`.

### Task 9: Subscription services, bills and keywords (lane D)

**Files:**
- Create: `data/merchants/software.yaml`, `news_music_games.yaml`, `memberships_boxes.yaml`, `fitness.yaml`, `telecom_utilities.yaml`, `insurance.yaml`
- Modify: `data/merchants/streaming.yaml`, `data/keywords.yaml`, `data/meta.yaml`
- Test: `backend/tests/unit/test_merchant_dictionary.py`

**Content rules:** at least 150 entries with `series: subscription` (streaming, music, news, software, storage, games, memberships, boxes, fitness and wellness) with their usual cadences; at least 60 with `series: bill` (mobile, internet and cable, electricity and gas utilities by their billing names, insurers, storage, security). An opaque biller that fronts many products (`apple com bill`, a play-store prefix) is one merchant with `series: subscription` and no fixed category beyond `leisure.software`, `mixed: true`. Keywords: about 120, each a word that says what was bought (`pizza`, `grill`, `cafe`, `pharmacy`, `dental`, `vet`, `fuel`, `parking`, `liquor`, `daycare`, ...), none that is a common surname or a place. At least 800 merchants in all.

- [ ] **Step 1: Failing tests:** `test_size_for_1_0` (800 to 1,500 merchants, 100 to 150 keywords); `test_series_floors` (150 and 60); `test_every_series_has_a_cadence`; `test_opaque_billers_are_mixed`; `test_keywords_are_not_merchant_tokens` (no keyword equals the first token of a merchant match); `test_every_merchant_finds_itself` (each entry's every `match`, looked up as a core, returns that entry). How many of the labelled lines hit is measured in Task 28, with the real clean-up; this lane does not read `descriptors.LABELLED`.
- [ ] **Step 2:** Run. Expected: about 600 merchants, few series.
- [ ] **Step 3: Write the entries** (version 3, new digest).
- [ ] **Step 4:** Run the file. Expected: pass.
- [ ] **Step 5:** Commit `feat(categorisation): subscription services, bills and keywords in the dictionary`.

### Task 10: Who it is and what it probably is: `identify` and `decide` (lane E)

**Files:**
- Modify: `backend/app/domain/categorisation/engine.py`
- Test: `backend/tests/unit/test_categorise_engine.py` (create)

**Interfaces** (pure):
- `@dataclass(frozen=True) class Identity: key: str | None; name: str | None; kind: DescriptorKind; via: str | None; hit: Hit | None; core: str`. `identify(raw: str | None) -> Identity`: `clean`, then `dictionary.lookup(core)`; `key` is the merchant's key on a hit, else the first `KEY_TOKENS = 4` tokens of the core, else None; `name` is the merchant's name, else the key in title case.
- `display_name(key: str | None) -> str | None` (`dictionary.name_for`, else title case).
- `KIND_CATEGORIES: Final`: the table of the spec (section 1) as `(kind, direction) -> (system_key, confidence)`: card_payment `transfers.credit_card_payment` 0.85; transfer `transfers.between_accounts` 0.80; loan_payment `transfers.loan_payment` 0.80; cash `transfers.cash` 0.90; fee `financial.bank_fees` 0.90; interest in `income.interest` 0.90, out `financial.interest_paid` 0.85; payroll in `income.salary` 0.85; refund without a hit `income.refunds` 0.80. `NEVER_GUESSED: Final = frozenset({"check", "deposit", "p2p"})`.
- `@dataclass(frozen=True) class Decision: category_id: uuid.UUID | None = None; suggested_category_id: uuid.UUID | None = None; source: str | None = None; confidence: Decimal | None = None`. `decide(identity: Identity, direction: Direction, *, learned: Learned | None, payee_default: uuid.UUID | None, categories: Mapping[str, uuid.UUID], now: datetime) -> Decision`, first that answers:
  1. `payee_default`: `payee`, 0.95.
  2. kind in `NEVER_GUESSED`: nothing.
  3. `learned` with `share >= 0.60`: `learned`, `LEARNED_BASE[min(decisions, 3)]` (0.80, 0.90, 0.95) times `share`, two places; capped at 0.70 when `last_decided_at` is older than `memory.STALE_DAYS`.
  4. a `KIND_CATEGORIES` entry: `dictionary`.
  5. a hit: `dictionary`, 0.90 (`anywhere` 0.85), minus 0.05 when `via` is set, 0.60 when the merchant is `mixed`; an `inflow` merchant answers only for direction `in`; any other merchant on direction `in` answers with its own category at 0.80 (a refund).
  6. `dictionary.keyword`: `keyword`, 0.80 when strong, else 0.60.
  A `learned` with `share < 0.60` caps steps 4 to 6 at 0.60. The category comes from `categories.resolve`; an unresolvable key answers nothing. Confidence at or above `APPLY_MIN` fills `category_id`, below it `suggested_category_id`.

- [ ] **Step 1: Failing tests** (the dictionary and the memory are stubbed in the test; no database): `test_identity_uses_the_dictionary_key_and_name`; `test_identity_without_a_hit_keeps_four_tokens`; `test_payee_default_comes_first`; `test_learned_once_is_a_fact_at_080`; `test_learned_three_of_four_is_a_suggestion` (0.95 x 0.75 = 0.71); `test_learned_nine_of_ten_is_a_fact` (0.86); `test_a_split_household_gets_no_learned_answer_and_only_a_suggestion_from_the_dictionary`; `test_stale_learning_is_a_suggestion`; `test_dictionary_hit_is_a_fact_at_090`; `test_behind_a_processor_it_is_085`; `test_a_mixed_store_is_a_suggestion`; `test_money_back_from_a_merchant_takes_its_category`; `test_payroll_only_for_money_in`; `test_kinds[...]` (one per row of `KIND_CATEGORIES`); `test_cheques_deposits_and_p2p_are_never_guessed` (even with a learned entry and a keyword); `test_a_weak_keyword_is_a_suggestion_and_a_strong_one_a_fact`; `test_a_key_the_household_does_not_have_falls_back_to_the_group_then_nothing`.
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_categorise_engine.py`. Expected: placeholders return nothing.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run again. Expected: pass.
- [ ] **Step 5:** Commit `feat(categorisation): decide a category from the payee, what was learned, the dictionary and keywords`.

### Task 11: The write path: new rows, a person's decision, a second look (lane E)

**Files:**
- Modify: `backend/app/domain/categorisation/engine.py`
- Test: `backend/tests/integration/test_categorise_write.py` (create; household, account and row factories as `tests/integration/test_transactions_api.py` uses)

**Interfaces:**
- `@dataclass class Summary: scanned: int = 0; by_source: Counter[str]; suggested: int = 0; keyed: int = 0; changed_accounts: set[uuid.UUID]`.
- `categorise_rows(db, household_id, rows: Sequence[Transaction], *, file_categorised: Collection[uuid.UUID] = (), protect: Mapping[uuid.UUID, Collection[str]] | None = None, user_id: uuid.UUID | None = None, request: HTTPConnection | None = None) -> Summary`, for rows just created and flushed, in the caller's transaction: (1) `merchant_key` for every row; (2) rows in `file_categorised` get `file`, 0.95; (3) the rules, exactly as the importer calls them today (`apply_rules_to` once for the unprotected rows, once with `protect=("categorization",)` for file-categorised ones; `protect[row.id]` for a manual row); a row that has a category or a split after the rule pass and no source gets `rule`; (4) for rows still undecided and without a split: payee defaults (`imports.repo.payee_default_categories`), `memory.lookup` for their keys, `decide`; (5) one `pinned_update`; (6) one audit row `transaction.categorise` (`count`, `by_source`, `suggested`) and one `transaction.categorised` event when anything was written.
- `person_decided(db, row: Transaction, *, before_category_id: uuid.UUID | None, before_source: str | None, at: datetime) -> None`: called by the transactions service after it set or cleared `row.category_id` (or the splits) for a person. Sets `category_source = "person"`, `category_confidence = 1`, clears `category_rule_id` and `suggested_category_id` on the ORM row (this is the person's write; `updated_at` moves in the caller). Memory, only when the row has a `merchant_key` and its kind is teachable: `withdraw` the old category when `before_source == "person"` and it had one; `record` the new one when it is not None.
- `reconsider(db, household_id, *, transaction_ids: Collection[uuid.UUID] | None = None, rekey: bool = False, check_cancelled: Callable[[], None] | None = None) -> Summary`: over rows that are undecided, carry a suggestion, or have source `dictionary` or `keyword` (decision 4), newest first, locked batches of 500: with `rekey`, `merchant_key` is recomputed for **every** row of the scope first; then `decide`; a row whose decision is unchanged is not written; a former fact that is now a suggestion or nothing loses its `category_id`. No rules. Pinned writes only.
- `undecided_clause() -> ColumnElement[bool]` and `reconsidered_clause()` for other modules' counts.

- [ ] **Step 1: Failing tests** (the dictionary is patched with three merchants):
  - `test_new_rows_get_a_key_and_a_guess_and_stay_untouched`: three rows created with `updated_at == created_at`; after `categorise_rows` each has a key, the known one `dictionary` 0.90 with a category, the mixed one a suggestion and no category, the unknown one nothing; the timestamps are still equal on all three; one `transaction.categorise` audit row whose payload holds no text.
  - `test_a_file_category_is_kept_and_marked_file`; `test_a_rule_wins_over_a_guess_and_is_marked_rule`; `test_a_payee_default_wins_over_the_dictionary`.
  - `test_a_learned_merchant_is_categorised_from_memory` (one `memory.record`, then a new row of that key: `learned`, 0.80).
  - `test_person_decided_teaches_and_moving_the_choice_moves_the_count` (A then B on one row: memory holds B once and A not at all); `test_clearing_a_category_is_a_decision` (source `person`, category NULL, never guessed again by `reconsider`); `test_a_p2p_row_teaches_nothing`.
  - `test_reconsider_touches_only_what_nobody_decided`: rows with source `person`, `file`, `rule`, `payee`, `learned`, `legacy` and a split row are byte-identical afterwards; an undecided row and a `dictionary` row are re-evaluated; `updated_at` of every row is unchanged.
  - `test_reconsider_withdraws_a_fact_the_dictionary_no_longer_states`; `test_rekey_recomputes_keys_of_decided_rows_without_changing_their_category`.
  - `test_rows_of_another_household_are_refused` (`ValueError`, as `apply_rules_to`).
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_categorise_write.py`. Expected: the placeholders write nothing.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the file and `tests/integration/test_rules_api.py tests/integration/test_structured_import.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(categorisation): one write path for guesses that never looks like a person`.

### Task 12: The refresh job; what was learned, listed and forgotten; the summary (lane E)

**Files:**
- Create: `backend/app/workers/handlers/categorise.py`
- Modify: `backend/app/domain/categorisation/service.py`, `backend/app/api/routers/categorisation.py`, `backend/tests/unit/test_worker_registry.py`
- Test: `backend/tests/integration/test_categorisation_api.py` (create)

**Interfaces:**
- `service.STATE_KEY: Final = "categorisation.state"`; `current_versions() -> dict[str, int]`; `refresh(db, household_id, *, force: bool = False, check_cancelled=None) -> RefreshResult`: reads the household's state (a household-scoped `setting` row, validated on read like `imports/wording.py`); when it equals `current_versions()` and not `force`, returns at once. Else, in order: `categories.add_new_defaults(since=state.taxonomy)`; `engine.reconsider(rekey=True)` when the cleaner or dictionary version moved, else `reconsider()`; `memory.seed_from_legacy` once (`memory_seeded`), followed by a second `reconsider()` only when it added entries; stores the state; audit `categorise.refresh` (versions before and after, `Summary` counts, categories added, entries seeded).
- Handlers: `categorise.dispatch` (hourly, `recurring(...)` like `detect_recurring`): one `categorise.refresh` per household whose state is behind, dedupe key per household and version triple; `categorise.refresh` (timeout 900 s, 3 attempts, cancellable).
- `summary(db, principal) -> CategorisationSummary` (counts over visible accounts only); `list_learned(db, principal, *, cursor, limit) -> LearnedCategoryPage` (`merchant` from `engine.display_name`; `rule_id` from `rules.merchant.merchant_rule` (None until lane R is merged); `offer_rule` when `decisions >= 3`, `share == 1`, no rule, direction `out`; `example_transaction_id` the newest visible row of the key or None); `forget(db, principal, memory_id, *, request) -> None` (`404 memory-not-found`; audit `memory.forget` with the id and the number of rows removed); `request_refresh(db, principal, *, request) -> JobAccepted` (admin; `force=True`).

- [ ] **Step 1: Failing tests:** `test_refresh_is_a_no_op_when_versions_match` (no audit row, no write); `test_a_new_dictionary_version_reconsiders_only_undecided_rows` (patch `dictionary.version` to 2 and add a merchant: an undecided row of it becomes a fact, a `person` row of it is unchanged, `updated_at` of both unchanged, the state stores 2); `test_the_first_refresh_adds_new_defaults_and_seeds_the_memory_once` (a household with no state and three `legacy` rows of one key: `categories.add_new_defaults` is called once with `since=1` (patched; the 13 are asserted in Task 27), one memory entry with three decisions; a second refresh calls neither); `test_dispatch_queues_only_households_behind`; `test_summary_counts_by_source_for_visible_accounts`; `test_learned_list_offers_a_rule_at_three_unanimous_decisions`; `test_forget_removes_the_entry_and_is_404_for_another_household`; `test_viewer_cannot_forget_and_member_cannot_refresh`; `test_worker_registry` gains the two job types.
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_categorisation_api.py tests/unit/test_worker_registry.py`. Expected: `409 not-ready`; unknown job types.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run both and `tests/security/test_authz_matrix.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(categorisation): re-evaluate undecided rows when the dictionary moves; list and forget what was learned`.

### Task 13: A person's choice is recorded as one, wherever it is made (lane L)

Starts after Task 11 is committed. The engine is imported inside the functions that use it, as `_apply_rules_to_new` imports the rules service today (the engine imports this module's package).

**Files:**
- Modify: `backend/app/domain/transactions/service.py` (`create_transaction`, `_apply_rules_to_new`, `update_transaction`, `set_splits`, `bulk`, `to_out`), `backend/app/domain/transactions/repo.py`, `backend/app/api/routers/transactions.py`
- Test: `backend/tests/integration/test_transactions_api.py`

**Interfaces:**
- `update_transaction`: when `category_id` is in the request and differs, `engine.person_decided(...)` after the assignment (also for `None`). When `raw_payee` changes: the key is recomputed through `engine.reconsider(transaction_ids=[row.id], rekey=True)` after the flush (a decided row keeps its category). The audit row gains `category_source` before and after.
- `set_splits`: a person's split is `person_decided` with `category_id` None on the row; removing every split leaves the row `person`, uncategorised.
- `bulk`: `categorise` calls `person_decided` per changed row; `accept_suggestions` sets `category_id = suggested_category_id` and calls `person_decided` (a row without a suggestion is `skipped`, reason `suggestion-missing`; a split row is `skipped`).
- `create_transaction`: replaces `_apply_rules_to_new` by `engine.categorise_rows(db, household_id, [row], protect={row.id: [...]})` with the same four protections; a category given in the request is `person_decided`.
- `to_out`: `merchant = engine.display_name(row.merchant_key)`; `subscription_id = row.scheduled_transaction_id`.

- [ ] **Step 1: Failing tests:** `test_setting_a_category_marks_it_person_and_clears_the_suggestion`; `test_clearing_a_category_marks_it_person`; `test_patching_the_same_category_changes_nothing` (no audit row, source as before); `test_bulk_categorise_marks_each_row`; `test_accept_suggestions_takes_the_suggested_category` (two rows with suggestions, one without: `updated`, `updated`, `skipped`); `test_a_split_by_a_person_is_a_decision`; `test_a_manual_row_with_a_category_is_person_and_one_without_goes_to_the_engine` (assert `categorise_rows` was called with the row and the protections; patch it); `test_changing_the_description_recomputes_the_key_and_keeps_a_decided_category`; `test_the_merchant_name_is_returned`; `test_a_member_without_edit_on_the_account_cannot_accept` (the status `get_visible_account(need="edit")` gives; nothing changed).
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_transactions_api.py -k "person or suggestion or split_by or manual_row or description or merchant_name or cannot_accept"`. Expected: source stays NULL; unknown bulk action.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `tests/integration/test_transactions_api.py tests/integration/test_transfers.py tests/security/test_ledger_surface.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(transactions): a category a person chose is recorded as theirs and teaches`.

### Task 14: The importer hands its rows to the engine; payees are found behind a processor (lane L)

Follows Task 13.

**Files:**
- Modify: `backend/app/domain/imports/service.py` (`_commit_statement`, `_Run`, the summary), `backend/app/domain/imports/summary.py`, `backend/app/domain/imports/payees.py`, `backend/app/domain/imports/repo.py`
- Test: `backend/tests/integration/test_import_categorisation.py` (create), `backend/tests/unit/test_payee_matcher.py`, `backend/tests/integration/test_structured_import.py`

**Interfaces:**
- `_commit_statement`: the two `apply_rules_to` passes and the payee-default block are replaced by one `engine.categorise_rows(db, run.household_id, [row for row, _ in created], file_categorised={row.id for row, categorised in created if categorised}, user_id=run.user_id)`. `run.rules_changed` and `run.payee_categorised` are filled from the `Summary` (`by_source["rule"]`, `by_source["payee"]`); the summary gains `categorised` and `suggested`. `_mark_untouched` stays the last write to the rows.
- `PayeeMatcher.match(raw_payee: str | None, *, core: str | None = None) -> uuid.UUID | None`: tries the raw text as today, then the `core`; a name shorter than `MIN_CONTAINED_CHARS` matches only when it equals the whole core. The importer passes `cleanup.clean(raw).core`.
- Stage 1's rule is unchanged and now tested with guesses in play: an earlier row is removed on a re-read only when `updated_at == created_at`.

- [ ] **Step 1: Failing tests:**
  - `test_import_categorisation.py`: `test_a_first_import_is_keyed_and_guessed` (a CSV of six invented rows with bare descriptors, the dictionary patched with two of their merchants: six keys, two facts with `dictionary`, summary `categorised == {"dictionary": 2}`); `test_every_imported_row_is_untouched_after_the_engine_ran` (`updated_at == created_at` for all six); `test_a_file_category_beats_a_rule_and_a_rule_beats_the_dictionary`; `test_a_second_import_keeps_what_a_person_chose` (PATCH one row's category, upload the same file again: the row is an exact duplicate, its category and `person` source unchanged); `test_a_reprocess_removes_a_guessed_row_the_new_reading_dropped` (the row had a `dictionary` category from the engine and from a later `reconsider`: it is removed, no `earlier_rows_kept` item); `test_a_reprocess_keeps_a_row_whose_category_a_person_changed` (kept and listed); `test_learned_applies_on_the_next_import` (correct a row, import the next month: `learned`).
  - `test_payee_matcher.py`: `test_a_payee_is_found_behind_a_processor` (`PAYPAL *STREAMBOX` with payees `PayPal` and `Streambox`: Streambox, given the core); `test_a_short_name_matches_a_core_it_equals` (`BP` with core `bp`); `test_a_short_name_inside_a_longer_core_is_still_no_match`.
  - `test_structured_import.py`: the tests on `rules_changed` and `payee_categorised` pass unchanged or are rewritten here and named in the commit.
- [ ] **Step 2:** Run the three files. Expected: no key, no source; `TypeError` for `core`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run them and `tests/integration/test_pdf_statement_pipeline.py tests/integration/test_trust_policy.py tests/integration/test_reprocess_replace.py` (whatever stage 1 named its replace tests). Expected: pass.
- [ ] **Step 5:** Commit `feat(imports): imported rows are categorised by the engine and stay untouched`.

### Task 15: Rules know merchants, say what they decided, and leave a person's choice alone (lane R)

**Files:**
- Modify: `backend/app/domain/rules/engine.py`, `backend/app/domain/rules/service.py` (`transaction_facts`, `write_outcome`, `_apply_batch`, `run_rules`), `backend/app/domain/rules/repo.py`, `backend/app/domain/rules/merchant.py` (`merchant_rule`)
- Test: `backend/tests/unit/test_rules_engine.py`, `backend/tests/integration/test_rules_api.py`, `backend/tests/security/test_rules_dry_run.py` (run)

**Interfaces:**
- `TxnFacts.merchant_key: str | None`; the leaf `merchant` matches when it equals `key`. It names no id, so `REFERENCE_KEYS` is unchanged; export and import of rule documents carry it as it is.
- `Outcome.category_rule: uuid.UUID | None`: the rule whose action set the final categorisation (None when no rule changed it).
- `write_outcome`: when `RuleField.CATEGORIZATION` changed, sets `category_source = "rule"`, `category_confidence = 1`, `category_rule_id = outcome.category_rule`, clears `suggested_category_id`. It still moves `updated_at` (stage 1, decision 6; the importer equalises afterwards).
- `_apply_batch(..., respect_people: bool = False)`: with it, a row whose `category_source == "person"` is evaluated with `categorization` protected. `run_rules` passes `respect_people=True`; `apply_rules_to` does not need it (new rows).
- `rules.merchant.merchant_rule(db, household_id, key: str) -> Rule | None` (declared in Task 2, implemented here; `rules/merchant.py` is this lane's): the enabled rule whose conditions are exactly one `merchant` leaf with that key (bare, or as the only member of an `all`) and whose actions are exactly one `set_category`.

- [ ] **Step 1: Failing tests:** unit: `test_merchant_condition_matches_the_key_only` (not a prefix, not None); `test_outcome_names_the_rule_that_set_the_category` (two matching rules, the later one's action wins: its id); `test_a_rule_that_only_tags_names_no_category_rule`. API: `test_a_rule_applied_at_creation_marks_the_row` (`rule`, the rule's id); `test_reapply_does_not_change_a_category_a_person_chose` (the rewrite of the behaviour the audit lists as old 2.3; a `legacy` row and a `dictionary` row are changed); `test_reapply_still_tags_a_person_row` (only the category is protected); `test_a_merchant_rule_round_trips_through_export_and_import`; `test_deleting_a_rule_leaves_source_rule_and_no_rule_id`.
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_rules_engine.py tests/integration/test_rules_api.py -k "merchant or names or person or marks or deleting"`. Expected: unknown condition type; source NULL; the person's category overwritten.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the three files whole. Expected: pass.
- [ ] **Step 5:** Commit `feat(rules): a merchant condition, provenance on what a rule sets, and re-apply that respects people`.

### Task 16: "Always categorise this merchant as...": preview, create, revert (lane R)

**Files:**
- Modify: `backend/app/domain/rules/merchant.py`, `backend/app/domain/rules/service.py` (an internal `queue_apply`), `backend/app/api/routers/rules.py`, `backend/app/workers/handlers/rules.py` (only if the job payload needs it)
- Test: `backend/tests/integration/test_merchant_rules.py` (create)

**Interfaces:**
- `preview(db, principal, data: MerchantRulePreviewIn) -> MerchantRulePreview`: the transaction must be visible to the caller (member). `teachable` is false with a `reason` when the row has no `merchant_key`, its kind is not teachable (`cleanup.clean(row.raw_payee).kind`), or it is split. Counts are SQL counts over rows with that key on accounts the caller may **edit**, by state: `uncategorised` (undecided, suggestions included), `guessed` (source `learned`, `dictionary`, `keyword` with another category), `decided` (source `person`, `file`, `payee`, `legacy`, or `rule` by another rule), `same_category`. `existing_rule_id` from `merchant_rule`. `shadowed_by`: the first enabled rule, in evaluation order and other than a merchant rule for this key, that matches the source row's facts and sets a category (found with the engine's dry run on that one row).
- `create(db, principal, data: MerchantRuleIn, *, request) -> MerchantRuleOut`: `422 merchant-not-teachable`; `409 rule-shadowed` (the detail names the rule's id); with an existing merchant rule: its `set_category` is changed (`replaced`, `previous_category_id`, audit `rule.update`); else a rule named `"<merchant name>: <category name>"` (200 characters at most) is appended with the highest `sort_order`, enabled, `stop_processing` false (the 500 cap applies; audit `rule.create` with `origin: "merchant"`). The source row itself becomes `person` with that category if it is not already. With `apply_to_existing`: `queue_apply` enqueues `RunRulesJob(rule_ids=[rule.id], account_ids=<accounts the caller may edit>, requested_by=...)` without the admin check of `request_apply`; `job_id` is returned.
- `revert(db, principal, rule_id, *, request) -> RuleRevertOut`: a member may revert a merchant rule; any other rule needs admin (`403 rule-not-revertible`). The rows with `category_rule_id == rule_id` and source `rule` are collected first (on every account: the rule was the household's), the rule is deleted, and those rows return to undecided through `pinned_update` (`category_id`, `category_source`, `category_confidence`, `category_rule_id` to NULL; `updated_at` kept); then `categorise.refresh` is queued for the household with `force` (`job_id`). Audit `rule.revert` (`reverted`). Undoing a replacement ("Change the rule") is not a revert: the client calls `create` again with `previous_category_id`.

- [ ] **Step 1: Failing tests:** `test_preview_counts_every_state_truthfully` (30 rows of one key: 14 undecided, 3 suggested, 5 `dictionary` elsewhere, 4 `person`, 2 `legacy`, 2 already in the category: `uncategorised 17`, `guessed 5`, `decided 6`, `same_category 2`; a key with 700 undecided rows counts 700); `test_preview_leaves_out_accounts_the_member_cannot_edit`; `test_preview_says_why_a_transfer_or_a_p2p_row_cannot_teach`; `test_create_appends_a_merchant_rule_a_member_may_make`; `test_create_without_apply_changes_no_other_row`; `test_create_with_apply_changes_undecided_and_guessed_rows_and_never_a_person_row` (run the queued job); `test_a_second_create_for_the_key_replaces_the_category`; `test_an_earlier_rule_that_already_decides_the_row_refuses_with_its_id`; `test_revert_returns_exactly_the_rows_the_rule_set_to_undecided` (rows the person re-categorised afterwards keep their category; `updated_at` of reverted rows unchanged); `test_replacing_back_with_the_previous_category_restores_the_rule`; `test_a_member_cannot_revert_a_general_rule`; `test_viewer_is_refused_and_a_foreign_rule_is_404`; `test_the_rule_cap_applies`.
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_merchant_rules.py`. Expected: `409 not-ready`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the file, `tests/integration/test_rules_api.py`, `tests/security/test_authz_matrix.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(rules): always categorise a merchant from a row, with a true preview and an exact undo`.

### Task 17: Archive format 3 (lane R)

**Files:**
- Modify: `backend/app/domain/portability/entities.py`, `schemas.py`, `codec.py`, `export.py`, `import_.py`
- Test: `backend/tests/unit/test_portability.py`, `backend/tests/integration/test_portability.py`, `test_portability_domains.py`

**Interfaces:**
- `FORMAT_VERSION = 3`; `SUPPORTED_FORMAT_VERSIONS = frozenset({1, 2, 3})`.
- `EntitySpec.added: Mapping[str, int]` (column to the format version that introduced it); `EntitySpec.columns(version: int | None = None)` leaves out columns added after `version`. `_decode_rows` compares a row's fields with `columns(archive_version)`; a column added later takes its server default or NULL.
- `transactions`: `added` for `merchant_key`, `category_source`, `category_confidence`, `suggested_category_id` (also in `refs` to `categories`); `exclude` gains `category_rule_id` and `scheduled_transaction_id`. A format 1 or 2 archive's transactions get `category_source = 'legacy'` where they have a category or a split (the migration's rule, applied at import).
- `scheduled_transactions`: `added` for every column of Task 1; from an older archive: the migration's backfill rule, applied at import.
- `EntitySpec("merchant_memory", MerchantMemory, refs={"category_id": "categories"}, since=3)`, placed after `categories`. `subscription_event` stays in `NOT_EXPORTED`: "what detection found on this instance; found again from the imported transactions".
- After an import the importer queues `categorise.refresh` with `force` for the household (keys, links and events are derived there).

- [ ] **Step 1: Failing tests:** `test_format_3_round_trips_provenance_memory_and_series_states` (a `person` row, a suggestion, a dismissed series and a memory entry come back equal); `test_a_format_2_archive_imports_with_legacy_sources` (an archive built by the test with the format 2 column sets); `test_a_format_1_archive_still_imports`; `test_rule_links_and_series_links_are_not_exported`; `test_entities_are_in_dependency_order` (run); `test_version_1_entities_are_the_original_ones` (run, unchanged); `test_not_exported_only_holds_justified_tables` (the allow-list loses `merchant_memory`); and one finding to record, not fix: `test_a_format_2_archive_without_account_subtype` documents by its assertion what happens today (refused or accepted), and the commit message states it for Task 29's upgrade note.
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_portability.py tests/integration/test_portability.py tests/integration/test_portability_domains.py`. Expected: version 2 written; unexpected fields.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the three files. Expected: pass.
- [ ] **Step 5:** Commit `feat(portability): archive format 3 carries how categories were decided and what was learned`.

### Task 18: Detection that knows merchants, habits and bills (lane S)

**Files:**
- Modify: `backend/app/domain/recurring/detection.py` (rewritten; the module doc is the algorithm of the spec, section 6)
- Test: `backend/tests/unit/test_recurring_detection.py` (rewritten; say so in the commit)

**Interfaces** (pure):
- `@dataclass(frozen=True) class Observed: id; account_id; posted_date; amount; merchant_key: str | None; text_key: str | None; kind: str; category_key: str | None; category_kind: str | None; series_hint: Literal["subscription", "bill"] | None = None; payee_id: uuid.UUID | None = None`. `text_key` is today's letters-only key, used only when there is no merchant key and for `p2p`.
- `EXCLUDED_KINDS: Final = frozenset({"transfer", "card_payment", "cash", "check", "fee", "interest", "deposit"})`; rows of category kind `transfer` or `ignore` are excluded too.
- `PATTERNS`: weekly 6-8 (4, 4), fortnightly 12-16 (3, 3), monthly 26-35 (3, 2), two-monthly 55-66 (3, 2; monthly x 2), quarterly 83-99 (3, 2), half-yearly 170-195 (2, 2; monthly x 6), annual 350-380 (2, 1): each with `min_count` and `min_count_known`. `MIN_REGULARITY = Decimal("0.8")`. `CLUSTER_TOLERANCE = Decimal("0.075")`, `CLUSTER_SLACK = Decimal("0.50")` (running mean). `FIXED_TOLERANCE = Decimal("0.01")`, floor 0.50.
- `HABIT_KEYS: Final` (the spec's list as `system_key` prefixes: `food.`, `transport.fuel`, `transport.parking`, `transport.taxi`, `transport.public_transit`, `shopping.`, `transfers.cash`); `BILL_KEYS: Final` (`housing.`, `utilities.`, `transfers.loan_payment`, `transport.car_insurance`, `health.insurance`, `financial.insurance`, `financial.taxes`, `family.childcare`, `family.education`).
- `Candidate` gains `merchant_key`, `kind: Literal["subscription", "bill", "income", "other"]`, `status: Literal["active", "suggested"]`, `fixed: bool`, `known: bool`, `last_amount: Decimal`; `payee_id` is the most common payee of its rows or None. Confidence `0.4 * regularity + 0.3 * stability + 0.2 * count + 0.1 * known`, two places. `status` is `active` when confidence is at least 0.80 and the unreduced `min_count` is met; else `suggested`. A habit that passes its strict gate (fixed, regularity 1, six charges), a `p2p` series and a known merchant seen once (annual) are `suggested` whatever the confidence.
- `detect(rows: Iterable[Observed], as_of: Mapping[uuid.UUID, date]) -> list[Candidate]`: "still running" is judged against `as_of[account]` (last charge at most 1.5 nominal cycles plus the pattern's grace before it); `next_due` is the first step after the last charge.
- `monthly_cost(amount: Decimal, cadence: Cadence, interval: int) -> Decimal` and `yearly_cost(...)` (weekly x 52 / 12, monthly / interval, quarterly / 3, yearly / 12; exact `Decimal`).

- [ ] **Step 1: Failing tests** (rows built in the test; no database): `test_monthly_fixed_at_a_known_merchant_is_an_active_subscription`; `test_two_charges_of_a_known_merchant_are_enough_and_three_of_an_unknown_one`; `test_one_annual_charge_of_a_known_merchant_is_suggested`; `test_two_plans_at_one_merchant_are_two_series`; `test_weekly_groceries_are_not_a_series`; `test_a_coffee_habit_is_not_a_series`; `test_a_fixed_weekly_car_wash_in_a_habit_category_is_only_suggested`; `test_two_charges_per_cycle_are_dropped`; `test_a_variable_utility_is_a_bill`; `test_rent_and_a_loan_are_bills_not_subscriptions`; `test_transfers_card_payments_and_cash_are_never_series`; `test_a_monthly_p2p_payment_is_a_suggested_bill`; `test_salary_is_income`; `test_history_split_between_rows_with_and_without_a_payee_is_one_series` (the audit's N8); `test_two_monthly_and_half_yearly_are_found`; `test_irregular_gaps_below_80_percent_are_dropped`; `test_a_series_is_not_ended_by_the_wall_clock` (last charge five months before today, `as_of` one week after it: found); `test_costs[...]`.
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_recurring_detection.py`. Expected: the old signatures.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the file. `tests/integration/test_recurring_api.py` is expected red until Task 19; name it in the commit.
- [ ] **Step 5:** Commit `feat(recurring): detect series by merchant, keep habits and transfers out, tell bills from subscriptions`.

### Task 19: Series that keep a person's decision; charges linked; the data clock (lane S)

**Files:**
- Modify: `backend/app/domain/recurring/service.py` (`observed`, `_match_rank`, `_match`, `persist`, `detect_household`, `update_schedule`, `delete_schedule`, `schedule_out`, `candidates`)
- Test: `backend/tests/integration/test_recurring_api.py`, `backend/tests/unit/test_subscription_lifecycle.py` (create)

**Interfaces:**
- `observed(db, household_id, account_ids, on) -> tuple[list[Observed], dict[uuid.UUID, date]]`: joins the category for its key and kind; `kind` and `series_hint` from `engine.identify(raw_payee)` (a row without `merchant_key` yet is identified on the fly); `as_of` per account is `min(on, max(posted_date))`.
- Matching a candidate to an existing series: same account and direction, then (0) same `merchant_key` and overlapping amounts, (1) same `merchant_key`, (2) no key on the series and the same payee or normalised name with overlapping amounts (rows from before this release; the series then takes the key). Cadence is not part of the match (a plan that moves from monthly to annual is the same series). A candidate that matches a `dismissed` series is dropped.
- `persist`, per matched series: `kind` is written only when `kind_source == "detector"`; `status` only when `status_source == "detector"` (`suggested` may rise to `active`, never the reverse), with the one exception of decision "confirmed and stopped" in Task 20; facts are always refreshed (`occurrences`, `first_seen_on`, `last_seen_on`, `confidence`, `next_due`, `amount_min`, `amount_max`, `last_amount`, the cadence). A new series is created with the candidate's `status` and `kind`, `origin = detected`, deciders `detector`. `active` follows `status`.
- Charges: `transaction.scheduled_transaction_id` is set through `pinned_update` for the candidate's rows that do not carry it; rows no longer in the series are not unlinked.
- `update_schedule`: `kind` sets `kind_source = "person"`; `active` maps as decision 9. `delete_schedule`: decision 9. `schedule_out` fills `kind`, `status`, `status_source`, `merchant`.
- `subscription_decide` lives in Task 21; this task only guarantees the detector respects what it will write.

- [ ] **Step 1: Failing tests:** `test_a_detected_series_starts_as_the_detector_found_it` (`active` or `suggested`, kind set, `active` flag consistent); `test_a_dismissed_series_is_never_created_again` (delete a detected series, detect twice: one row, `dismissed`); `test_a_person_s_kind_and_pause_survive_detection`; `test_suggested_rises_to_active_with_more_charges_but_not_back`; `test_a_series_from_before_the_upgrade_is_adopted_not_doubled` (a detected series with no key and a payee-less name, new rows with a key: one series, now keyed); `test_a_manual_series_is_matched_and_never_rewritten`; `test_charges_are_linked_without_touching_the_rows` (`scheduled_transaction_id` set, `updated_at == created_at`); `test_a_plan_that_became_annual_is_the_same_series`; `test_patch_active_false_is_a_person_s_pause`; `test_the_calendar_shows_only_active_and_confirmed` (unchanged code, new states); `test_detection_leaves_accounts_the_caller_cannot_edit_alone` (the manual `POST /recurring/detect`).
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_recurring_api.py tests/unit/test_subscription_lifecycle.py`. Expected: red from Task 18's signatures; deleted series return.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run both and `tests/integration/test_budgets_api.py tests/integration/test_reports_api.py` (they read `active`). Expected: pass.
- [ ] **Step 5:** Commit `feat(recurring): a person's decision about a series sticks, and its charges are linked`.

### Task 20: What changed: price, missed, ended, new, renewal, trial; the alerts (lane S)

**Files:**
- Modify: `backend/app/domain/recurring/subscriptions.py`, `backend/app/domain/recurring/service.py` (`persist` calls it), `backend/app/workers/handlers/recurring.py`, `backend/app/domain/notifications/routing.py`, `backend/app/domain/events/service.py` (`ACCOUNT_SCOPED_PREFIXES` gains `"subscription."`), `backend/app/api/ai/schemas.py` and `views.py` (`RecurringAi.kind`, `.status` with `src("scheduled_transaction.kind")` and `.status`), `backend/app/mcp/tools.py` (the `get_recurring` description says the two fields)
- Test: `backend/tests/unit/test_subscription_lifecycle.py`, `backend/tests/integration/test_notifications.py`, `test_event_visibility.py`, `backend/tests/security/ai/test_ai_views.py`

**Interfaces:**
- `subscriptions.GRACE_DAYS: Final` (weekly and fortnightly 3, monthly 5, two-monthly and quarterly 10, longer 21); `PRICE_MIN_ABS = Decimal("1.00")`, `PRICE_MIN_SHARE = Decimal("0.05")`, `DRIFT_SHARE = Decimal("0.02")`; `NEW_WINDOW_DAYS = 45`; `RENEWAL_LEAD_DAYS = 7`; `TRIAL_MAX = Decimal("1.00")`, `TRIAL_GAP = (3, 35)`.
- Pure: `price_change(series: SeriesFacts, charges: Sequence[Charge]) -> PriceVerdict | None` (`changed`, `pending`, `drift`); `missed_cycles(next_due: date, cadence, interval, as_of: date) -> int`; `trial(charges, known: bool) -> date | None`.
- `settle(db, household_id, series: ScheduledTransaction, candidate: Candidate | None, charges, as_of: date, *, quiet: bool) -> list[SubscriptionEvent]`, called by `persist` for every series with status `active`, `confirmed`, `ended` or `suggested`:
  - a fixed series whose latest charge differs from `last_amount` by more than `max(1.00, 5%)`: `previous_amount`, `last_amount`, `price_changed_on` set; event `price_changed` (`pending` in the API while only one charge has the new price and the cadence is shorter than half-yearly); between 2% and 5%: `last_amount` updated, no event; under 2%: nothing.
  - no candidate and `missed_cycles >= 1`: event `missed` (once per due date); `>= 2`: `status = ended` with `ended_on`, event `ended`; this also applies to a `confirmed` series (deciders become `detector`), and to nothing a person paused, ended or dismissed.
  - a charge after `ended_on` on a series a person ended: event `charged_after_end`, status unchanged. On a series the detector ended: back to `active`.
  - a series created now with `first_seen_on` within 45 days of `as_of`: event `new`; `trial_converted` when `trial(...)` says so. With `quiet` (the household has no series with a `merchant_key` yet: the first run after the upgrade) only series first seen in the window raise `new`.
  - each event is inserted once (the unique key), raises `notify(db, household_id, "subscription.<kind>", source_id=event.id)` and emits `subscription.changed`.
- `renewals(db, household_id, on: date) -> int`: for `active` and `confirmed` series of kind `subscription` or `bill` with cadence half-yearly or longer and `next_due - 7 days <= on < next_due`: event `renewal_due` dated `next_due`. Run by the daily detect job after `persist`.
- `routing.py`: the four kinds join `Kind`; `ROUTES` gains the seven events with these sentences: "A new subscription was found. Open PerFin to see it."; "A subscription's price changed. Open PerFin to see it."; "An expected subscription or bill charge did not arrive. Open PerFin to see it."; "A subscription or bill renews within a week. Open PerFin to see it."; the trial event uses the first, ended and charged-after-end the third.

- [ ] **Step 1: Failing tests:** `test_price_change[...]` (10.99 to 12.99 changed and pending; a second 12.99 clears pending; 10.99 to 11.30 drift without an event; 10.99 to 11.05 nothing; an annual 99.00 to 119.00 changed at once; a variable series never); `test_missed_then_ended_on_the_data_clock` (monthly, `as_of` 40 days after `next_due`: one `missed`; 70 days: `ended`; `as_of` before `next_due`: nothing, whatever today is); `test_a_confirmed_series_that_stopped_is_ended_with_an_alert`; `test_a_paused_a_dismissed_and_a_person_ended_series_are_left_alone`; `test_a_charge_after_a_person_ended_it_is_reported`; `test_the_first_run_is_quiet_about_old_series`; `test_a_new_series_in_the_window_is_reported_once` (detect twice: one event); `test_trial_then_full_price`; `test_renewal_is_announced_once_a_week_ahead`; notifications: `test_subscription_events_queue_one_job_per_subscribed_channel` and `test_subscription_sentences_carry_no_detail` (no digit, no currency sign, none of the fixture's merchant names in any routed sentence); visibility: `test_subscription_changed_reaches_only_those_who_see_the_account`; AI: `test_recurring_rows_say_kind_and_status`, and the tool and route counts are still 19 (run `tests/security/mcp/test_mcp_server.py tests/security/ai/test_ai_documents.py`).
- [ ] **Step 2:** Run the four files. Expected: `not-ready`; unknown kinds.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run them and `tests/security/ai/test_column_classification.py tests/unit/test_notification_channels.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(subscriptions): notice price changes, missed and stopped charges, new ones and renewals`.

### Task 21: The subscriptions API: the list with its totals, one series, a decision, the changes (lane S)

**Files:**
- Modify: `backend/app/domain/recurring/subscriptions.py`, `backend/app/api/routers/subscriptions.py`
- Test: `backend/tests/integration/test_subscriptions_api.py` (create)

**Interfaces:**
- `list_subscriptions(db, principal, *, kind: Collection[str] | None, status: Collection[str] | None, account_ids) -> SubscriptionList`: series of kind `subscription` and `bill` on visible accounts (default: every status but `dismissed`). `amount` is `last_amount`, else the midpoint. `subscriptions` and `bills` totals: the monthly and yearly cost of `active` and `confirmed` series of that kind, in the household's base currency (`FxConverter`; a series that cannot be converted is listed in `unconverted` and left out). `needs_look`: `suggested` series plus series with an open event. `overdue`: `next_due` plus grace is before the account's `as_of`. Order: open events first, then `next_due`.
- `get_subscription(db, principal, schedule_id) -> SubscriptionDetail`: charges are the visible transactions with `scheduled_transaction_id == id`; events newest first.
- `decide(db, principal, schedule_id, data: SubscriptionDecision, *, request) -> SubscriptionOut` (member with edit on the account): `confirm` to `confirmed`; `dismiss` to `dismissed` (its transactions are unlinked through `pinned_update`); `pause` to `paused`; `resume` to `confirmed`; `end` to `ended` with `ended_on` today; `reopen` (from `ended` or `dismissed`) to `confirmed`. Always `status_source = "person"`. `kind`, when given, with `kind_source = "person"`. Open events of the series become `seen`. Audit `subscription.decide` (id, decision, kind, status before and after). Emits `schedule.updated`.
- `list_events(db, principal, *, state, cursor, limit) -> SubscriptionEventPage` (visible accounts; `open` is the count of open ones); `mark_seen(db, principal, data: EventsSeen) -> EventsSeenOut` (ids of other households or invisible accounts are ignored, not errors).

- [ ] **Step 1: Failing tests:** `test_totals_count_active_and_confirmed_only_and_split_subscriptions_from_bills` (a monthly 10.00, an annual 120.00, a weekly 5.00 subscription, a suggested one, a paused one, a 60.00 bill: subscriptions monthly `41.67`, yearly `500.00`, count 3; bills monthly `60.00`); `test_a_foreign_currency_series_is_converted_or_listed`; `test_needs_look_counts_suggested_and_open_events`; `test_overdue_uses_the_account_s_latest_row`; `test_detail_lists_charges_and_price_history`; `test_each_decision[...]` (six, with the resulting status, `status_source == "person"` and the `active` flag); `test_dismiss_unlinks_its_charges_and_detection_does_not_bring_it_back`; `test_a_decision_marks_the_series_events_seen`; `test_viewer_cannot_decide_and_a_member_needs_edit_on_the_account`; `test_series_on_invisible_accounts_are_absent_from_list_totals_and_events`; `test_mark_seen_ignores_foreign_ids`; `test_a_foreign_schedule_is_404`.
- [ ] **Step 2:** Run `uv run pytest -q tests/integration/test_subscriptions_api.py`. Expected: `409 not-ready`; empty pages.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the file and `tests/security/test_authz_matrix.py tests/security/test_input_models.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(subscriptions): the list with monthly and yearly cost, one series with its history, decisions that stick`.

### Task 22: The grid says why, shows a suggestion as one, and groups the category list (lane FC)

**Files:**
- Create: `frontend/src/components/CategoryPicker.tsx`, `frontend/src/features/transactions/provenance.ts`, `provenance.test.ts`, `transactions.css` (or the feature's existing stylesheet)
- Modify: `frontend/src/components/index.ts`, `features/transactions/cells.tsx`, `TransactionsPage.tsx`, `BulkBar.tsx`, `TransactionDialog.tsx`, `api.ts`, `optimistic.ts`, `fixtures.ts`
- Test: `frontend/src/features/transactions/TransactionsPage.test.tsx`, `TransactionActions.test.tsx`

**Interfaces:**
- `CategoryPicker`: a native `<select>` with one `<optgroup>` per top-level category, an "Uncategorised" option, the same props as the current select; used by the grid cell, the dialog and the bulk bar here.
- `provenance.ts`: `sourceLabel(row, ruleName?: string): string | null` with exactly these texts: `person` "You chose this"; `file` "From the file"; `rule` "Rule: <name>" (or "A rule that no longer exists"); `payee` "Payee default"; `learned` "Learned from you"; `dictionary` "Merchant list"; `keyword` "Keyword"; `legacy` null. `confidenceText("0.90") === "90%"`. `isSuggestion(row)`.
- The category cell: the category, and under it in small muted text the source and, for guesses, the confidence. A suggestion renders as "<Category>?" with two buttons, "Accept" (bulk `accept_suggestions` with the one id) and the picker to choose another; it never looks like a set category. The payee cell shows `merchant` above the raw descriptor when they differ; the raw descriptor is always visible.
- Filters: two toggles above the grid, "Suggestions" (`suggested=true`) and "No category", each with its count from `GET /categorisation/summary`. The bulk bar gains "Accept suggestions" when a selected row has one.
- Optimistic updates set `category_source: "person"` and clear the suggestion.

- [ ] **Step 1: Failing tests:** `provenance.test.ts` (each label; confidence text; a row with a suggestion and no category is a suggestion). Page: `shows how a category was decided` (rows with `dictionary` 0.90 and `rule`: the texts "Merchant list" and "90%", "Rule: Coffee"); `shows a suggestion as a question and accepts it` (the bulk call with `accept_suggestions` and that id; afterwards "You chose this"); `the raw descriptor stays visible beside the merchant name`; `filters to suggestions`; `the category list is grouped` (an `optgroup` named after the parent); `a viewer sees the source and no accept button`.
- [ ] **Step 2:** Run `npm test -- src/features/transactions`. Expected: the texts are missing.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the tests, `npm run lint`, `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(transactions): show how each category was decided and offer unsure ones as suggestions`.

### Task 23: "Always categorise <merchant> as...", from the row and after a correction (lane FC)

**Files:**
- Create: `frontend/src/features/transactions/AlwaysCategoriseDialog.tsx`, `AlwaysCategoriseDialog.test.tsx`, `TeachPrompt.tsx`
- Delete: `frontend/src/features/transactions/remember.ts`, `remember.test.tsx`, `RememberCategoryDialog.tsx`
- Modify: `frontend/src/features/transactions/RowActions.tsx`, `TransactionsPage.tsx`, `api.ts`, `frontend/src/features/rules/api.ts` (`previewMerchantRule`, `createMerchantRule`, `revertRule`)

**Interfaces:**
- The row menu item "Always categorise <merchant> as..." is shown to members and above for a row with a `merchant_key` that is not split (the old item was owner and admin only).
- The dialog: the merchant's name and the raw descriptor; a `CategoryPicker` (preset to the row's category or suggestion); the server preview in sentences built from the counts ("17 transactions without a category", "5 that PerFin guessed differently will change", "6 that were chosen by hand or by another rule stay as they are"); a checkbox "Also change the existing <n> transactions", **unticked**, hidden when `uncategorised + guessed` is 0; when `teachable` is false the reason in words and no Save; when `shadowed_by` is set, "The rule <name> already decides this merchant" with a link to that rule and no Save; when `existing_rule_id` is set, the button reads "Change the rule".
- After Save: a toast "Rule saved" with "Undo" (`POST /rules/{id}/revert` for a new rule; for a changed rule, `POST /rules/merchant` with `previous_category_id`), and the job's progress when existing rows are changed (the existing `JobProgress`).
- `TeachPrompt`: after a category change in the grid on a teachable row, when the preview's `uncategorised + guessed` is at least 1 and there is no merchant rule: a non-blocking line under the grid's toolbar, "Use <category> for <merchant> from now on? <n> other transactions have no category yet." with "Always..." (opens the dialog) and "Not now"; one prompt at a time; dismissed for that merchant for the session.

- [ ] **Step 1: Failing tests:** `the dialog states the three counts from the server`; `existing transactions are changed only when the box is ticked` (the request body's `apply_to_existing`); `a row that cannot teach says why and cannot be saved`; `an earlier rule is named and nothing is saved`; `undo of a new rule calls revert and undo of a changed rule puts the old category back`; `a member sees the menu item, a viewer does not`; `changing a category offers the rule once and "Not now" hides it`; `no prompt when nothing else would change`.
- [ ] **Step 2:** Run `npm test -- src/features/transactions`. Expected: the component does not exist.
- [ ] **Step 3: Implement**; remove the three old files and their imports.
- [ ] **Step 4:** Run the tests, `npm run lint`, `npm run typecheck`. Expected: pass; `grep -rn "descriptionKey\|RememberCategory" frontend/src` finds nothing.
- [ ] **Step 5:** Commit `feat(transactions): always categorise a merchant from its row, with a true preview and undo`.

### Task 24: Settings: what was learned, merchant rules in the editor, the four notifications (lane FC)

**Files:**
- Modify: `frontend/src/features/imports/LearnedPage.tsx`, `learned.ts`, `LearnedPage.test.tsx`, `frontend/src/features/categories/api.ts` (`getCategorisationSummary`, `listLearned`, `forgetLearned`, `refreshCategorisation`), `frontend/src/features/rules/builder.ts`, `builder.test.ts`, `describe.ts`, `portable.ts`, `RulesPage.tsx`, `RulesPage.test.tsx`, `frontend/src/features/settings/notifications/form.ts`, `NotificationsPage.test.tsx`

**Interfaces:**
- Learned page, two new cards above the layout cards. "How transactions got their category": one line per source with its count, the dictionary's version and merchant count, "Not up to date yet" while `up_to_date` is false, and for admins "Check again" (`POST /categorisation/refresh`). "Categories learned from you": a table (merchant, category, "3 of 3 times", last time) with "Forget" (members and above) and, when `offer_rule` and there is an `example_transaction_id`, "Make it a rule" (a link to the transactions page that opens the dialog of Task 23 for that row). The intro line "Categories through rules" is replaced by one sentence naming the three ways (rules, what was learned, the merchant list).
- Rules: `builder.ts` gains the leaf `merchant` (a text field checked against the key form; label "Merchant is"); `describe.ts` says "the merchant is <name or key>"; `portable.ts` accepts it. The rules page shows "Undo" on a rule (revert) for those who may: members on a merchant rule, admins on any.
- `form.ts` `KINDS` gains: "A new subscription is found", "A subscription's price changes", "An expected charge does not arrive", "A subscription or bill renews within a week".

- [ ] **Step 1: Failing tests:** `lists what was learned and forgets an entry`; `offers a rule at three unanimous decisions`; `shows the counts by source and the dictionary version`; `a member sees no "Check again"`; builder: `a merchant leaf round-trips` and `a key with a capital letter is refused`; describe: the sentence; rules page: `undo reverts and removes the rule from the list`; notifications: `the four subscription kinds can be ticked and are sent`.
- [ ] **Step 2:** Run `npm test -- src/features/imports/LearnedPage.test.tsx src/features/rules src/features/settings/notifications`. Expected: missing cards and kinds.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the tests, `npm run lint`, `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(settings): what PerFin learned about categories, merchant rules and subscription notifications`.

### Task 25: The Subscriptions view: cost, what needs a look, decisions (lane FS)

**Files:**
- Create: `frontend/src/features/subscriptions/SubscriptionsPage.tsx`, `SubscriptionsPage.test.tsx`, `api.ts`, `text.ts`, `text.test.ts`, `subscriptions.css`, `routes.tsx`
- Modify: `frontend/src/lib/queryKeys.ts` (`subscriptions`), `frontend/src/lib/sse.ts` (`"subscription.*"` and `"schedule.*"` invalidate it), `sse.test.ts`

**Interfaces:**
- `text.ts`: `cadenceText`, `statusText` ("Might be a subscription", "Found for you", "Confirmed", "Paused", "Ended", "Not a subscription"), `eventText(event)` ("Price went from <a> to <b>", "Expected on <date>, not seen", "Stopped", "New", "Charged after you ended it", "Renews on <date>", "Trial became paid").
- The page, in this order: a header with two figures, "Per month" and "Per year", and the count, for the chosen tab; tabs "Subscriptions" and "Bills"; "Needs a look" (suggested series and series with open changes; per row the change in words and the buttons "Confirm", "Not a subscription", and for a change "Got it"); "Next 30 days" (date, name, amount); "Active" (name, merchant, account, amount, repeats, next charge, per year; sortable by per year, the default); "Ended and dismissed", folded. Row menu: Pause or Resume, Ended, "This is a bill" or "This is a subscription", Reopen. Series in `unconverted` are listed under the figures as "not included: <name>". Empty state: "No subscriptions found yet. They appear after a few statements are imported."
- Layout in `subscriptions.css`: a table from 720 px; below that each row is a card with the name, the amount and the next date first. No inline style.
- Every action calls `POST /subscriptions/{id}/decision` and invalidates `subscriptions` and `recurring`.

- [ ] **Step 1: Failing tests:** `text.test.ts` (each text); page: `shows the monthly and yearly cost of the tab` ; `bills have their own figures`; `a suggested series can be confirmed or dismissed` (the two request bodies; a dismissed one moves to the folded section); `a price change is shown in words and acknowledged` (`POST /subscriptions/events/seen`); `sorts by yearly cost`; `a viewer sees no buttons`; `series that could not be converted are named`; `the empty state`.
- [ ] **Step 2:** Run `npm test -- src/features/subscriptions src/lib/sse.test.ts`. Expected: no such module.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the tests, `npm run lint`, `npm run typecheck`. Expected: pass.
- [ ] **Step 5:** Commit `feat(subscriptions): a view of what you pay for, what needs a look, and decisions that stick`.

### Task 26: One series up close; the view in today's shell (lane FS)

**Files:**
- Create: `frontend/src/features/subscriptions/SubscriptionDetail.tsx`, `SubscriptionDetail.test.tsx`
- Modify: `frontend/src/features/subscriptions/SubscriptionsPage.tsx`, `routes.tsx`, `frontend/src/app/nav.ts` (one entry, "Subscriptions", after "Calendar"), `router.tsx`, `shell.test.tsx`, `frontend/src/features/calendar/CalendarPage.tsx` (the second tab is named "Recurring items"), `RecurringTab.tsx`, `api.ts`, `calendar.test.tsx`

**Interfaces:**
- `/subscriptions/:id` opens `SubscriptionDetail` (a dialog over the list on wide screens, a page on narrow ones): name, merchant, account, status and who set it, per month and per year, next expected charge; "Charges" (date, amount; newest first) with a link "See in transactions" (`/transactions?subscription=<id>`); "Price history" (the `price_changed` events as "<date>: <a> to <b>"); "Changes" (the other events). Editing the name, the amount range and the cadence reuses the existing schedule form (`PATCH /recurring/{id}`).
- `RecurringTab`: the Kind column reads the series' `kind` (Subscription, Bill, Income, Other), the State column its `status` through `statusText`; "Delete" on a detected item reads "Not recurring" (it dismisses); a line above the table links to the Subscriptions view. The Suggestions card is removed (suggested series are rows now).
- Stage 3 moves the route under Plan; nothing else about navigation changes here.

- [ ] **Step 1: Failing tests:** `the detail lists charges, price history and the next charge`; `links to the transactions of the series`; `the nav has a Subscriptions entry and its route renders` (`shell.test.tsx`: the pinned list gains the one label); calendar: `the recurring tab shows kind and status and dismisses a detected item`.
- [ ] **Step 2:** Run `npm test -- src/features/subscriptions src/features/calendar src/app/shell.test.tsx`. Expected: no route; the old labels.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the tests, `npm run lint`, `npm run typecheck`, `npm run build`. Expected: pass.
- [ ] **Step 5:** Commit `feat(subscriptions): one series with its charges and price history; the view in the current shell`.

### Task 27: Everything together through the real pipeline (Final)

**Files:**
- Create: `backend/tests/integration/test_categorisation_e2e.py`, `backend/tests/integration/test_subscriptions_e2e.py`
- Modify: whatever a red test names (any lane's file), `scripts/seed_dev.py` (the card account's rows are created without a category, so the demo shows the engine), `backend/tests/integration/test_seed_dev.py`

- [ ] **Step 1: Failing tests**, with the real clean-up, dictionary, engine, rules and detector and no patching:
  - `test_review_focus_1_a_re_read_after_categorisation`, `test_review_focus_2_a_person_s_category_under_every_later_pass`, `test_review_focus_3_a_mixed_store_and_a_split_household`, `test_review_focus_4_a_subscription_with_a_late_statement`, `test_review_focus_5_a_member_and_accounts_they_cannot_see`: each is the scenario of the Review Focus section, step for step, through the HTTP API and the job handlers.
  - `test_a_new_household_s_first_csv` (a CSV of 60 lines taken from `descriptors.LABELLED`, 20 of each group: at least 15 of the 20 known lines are facts, no invented line is a wrong fact, no non-purchase line is in an expense category).
  - `test_teach_once_then_import_next_month`; `test_always_then_undo_leaves_the_ledger_as_it_was` (a snapshot of `category_id`, `category_source` and `updated_at` of every row before the rule and after the revert and the queued refresh: equal).
  - `test_two_years_of_statements_month_by_month` (the planted ledger imported one month at a time with detection after each: no decoy is ever `active`; a dismissed series stays dismissed through the remaining months; `new` events are raised only for series that start inside the run).
  - `test_seed_dev` gains: after the seed, `GET /categorisation/summary` shows `dictionary` facts and `GET /subscriptions` at least one active subscription.
- [ ] **Step 2:** Run them. Fix what they show in the file that owns the defect; each fix gets a unit test beside the lane's tests.
- [ ] **Step 3:** Run the whole backend suite (`uv run pytest -n auto`) and the whole frontend suite. Rewrite, here, any test another lane named in a commit message as turned red by its change.
- [ ] **Step 4:** Commit `test(categorisation): the lanes together: imports, corrections, rules, re-reads and subscriptions`.

### Task 28: The quality targets become tests (Final)

**Files:**
- Create: `backend/tests/unit/test_categorisation_quality.py`, `backend/tests/integration/test_subscription_quality.py`
- Modify: `backend/tests/fixtures/quality.py` (the `--today` adapter for the old detector is removed; `--now` prints the current figures), dictionary files, `cleanup.py`, `engine.py`, `detection.py` as the misses require

- [ ] **Step 1: Failing tests**, one per row of the spec's table (section 9), with the thresholds written as constants at the top of each file: `known_fact_right >= 0.85`, `fact_precision >= 0.97`, `suggestion_precision >= 0.80`, `invented_wrong_fact <= 0.01`, `fact_coverage >= 0.60`, `one_key_per_merchant >= 0.95`, `kind_right >= 0.97`, `private_leaks == 0`, `test_one_correction_teaches` (correct the first line of each invented merchant that occurs twice; the second is right for at least 95%); `found >= 0.90`, `found_active >= 0.80`, `decoys_active == 0`, `decoys_suggested <= 2`, `kind_right >= 0.90`, `price_changes_flagged == all`, `price_changes_false == 0`, `ended_right == all`, `ended_wrong == 0`. A failing assertion prints the misses (label and what was answered), at most 40.
- [ ] **Step 2:** Run. For each miss decide: a dictionary entry or match form is missing (add it, bump the version and the digest), clean-up left noise (fix, add the line to `test_merchant_cleanup.py`), the engine or detector decided wrongly (fix with a unit test), or the label is wrong (report to the coordinator; a label is changed only with a reason in the commit message). Never lower a threshold; a target that cannot be met is reported with the figure reached and the owner decides.
- [ ] **Step 3:** Run both files and the suites of lanes C, D, E and S. Expected: pass.
- [ ] **Step 4:** Commit `test(quality): categorisation and subscription targets are gates`, with the before and after figures in the message.

### Task 29: Documentation, controls, changelog, upgrade note, full gates (Final)

**Files:**
- Create: `docs/CATEGORISATION.md`
- Modify: `mkdocs.yml` (the new page under Design, after Ingestion; `superpowers/` stays excluded), `docs/DATA_MODEL.md`, `docs/API.md`, `docs/AI_ACCESS.md`, `docs/MCP_CLIENTS.md`, `docs/PORTABILITY.md`, `docs/INGESTION.md` (the categorisation step of the import; the "untouched" rule now names the pinned write), `docs/security/CONTROLS.md`, `docs/security/what-leaves-the-host.md`, `docs/security/notifications.md`, `docs/security/ai.md`, `docs/UPGRADING.md`, `docs/ROADMAP.md`, `CHANGELOG.md`, `backend/openapi.json`, `frontend/src/lib/api/schema.d.ts`
- Test: `tests/security/test_authz_matrix.py`, `test_input_models.py`, `tests/security/ai/test_column_classification.py`, `tests/security/mcp/test_mcp_server.py`, `tests/unit/test_portability.py`, `tests/integration/test_migrations.py` (all run, none edited here)

- [ ] **Step 1: `docs/CATEGORISATION.md`.** For a person running PerFin: how a category is decided (the order, the eight sources in the words the UI uses, the 0.80 line); what "learned" is and how to forget it; the merchant dictionary (what it is, where it lives, its sources and licence, how to propose a merchant, how an update re-evaluates only what nobody decided); "always categorise" and undo; subscriptions (what counts, bills against subscriptions, the states, why nothing is "missed" before a statement is imported, the changes it reports); what the figures of Task 28 are and what they are not.
- [ ] **Step 2: Controls and security.** `CONTROLS.md`: E1 and M1 gain the classification of the new columns and the denial of `merchant_key`, `merchant_memory` and `subscription_event`; E2 and M3 say the tool count is still 19 and name the two added fields; F3 gains the two tables and their isolation tests; I4 gains the five audit actions and the rule that they carry no descriptor or merchant; C12 gains one sentence: categorisation never marks a row as touched (`pinned_update`), with `test_ledger_models.py` and `test_import_categorisation.py` as its tests. `what-leaves-the-host.md`: a line "Categorisation and subscription detection: nothing; the merchant list is a file in the image". `notifications.md`: the four kinds and their sentences. `ai.md` and `AI_ACCESS.md`: `kind` and `status` on recurring rows.
- [ ] **Step 3: The rest.** `DATA_MODEL.md` (the columns, the two tables, the checks, why `active` stays). `API.md` (the twelve routes, the filters, the bulk action, the changed delete of a detected series). `PORTABILITY.md` (format 3, what travels, the two links that do not, older archives). `UPGRADING.md`, a "1.0.0-rc.10" note: migration `0014`; existing categories become "before tracking" and are never guessed over; within an hour of the upgrade a background pass adds 13 categories, computes merchants, seeds what was learned from existing categories and guesses for uncategorised rows only; existing recurring items keep working, deleted detected ones no longer return, the first detection is quiet; "Always categorise like this" rules made before keep working as ordinary rules; archives are now format 3 and what Task 17 found about old format 2 archives. `CHANGELOG.md` `## [Unreleased]`: Added, Changed, Fixed and Removed entries in the file's voice, one per thing a person can notice, with the measured figures.
- [ ] **Step 4: Schema and gates.** `make openapi` (no change expected). In the container: `make check` and `mkdocs build --strict`. By hand: `grep -rn "style=" frontend/src/features/subscriptions frontend/src/features/transactions` shows no new site; the MCP tool count and AI route count tests pass unedited; `git grep -n "updated_at" backend/app/domain/categorisation` shows only `pinned.py`.
- [ ] **Step 5:** Commit `docs: categorisation and subscriptions; controls, changelog and the rc.10 upgrade note`.

### Task 30: Corpus and quality measurement (coordinator)

**Files:** none in the repository. Reserved for the coordinator; it runs after Task 29 and before the release candidate is tagged.

- [ ] **Step 1:** Outside the repository, feed the payee strings of the scratch corpus results through `engine.identify` and `engine.decide` (a new household, no rules, no learning). Record only aggregates: rows, share with a merchant key, with a dictionary hit, with a fact, with a suggestion, with a non-purchase kind. Compare with the indicative targets (a key for 80% of purchase-like rows; a fact or suggestion for 45%).
- [ ] **Step 2:** Read 200 random facts by eye against their descriptors. More than 3% wrong blocks rc.10: each cause gets an invented line in the labelled set or a unit test, and a fix through Task 28's loop.
- [ ] **Step 3:** List the 100 most frequent cores with no hit (kept in scratch). For those that are genuine, common US merchants, write dictionary entries from our own knowledge (never by copying the corpus line), bump the version, re-run Tasks 28 and this step once.
- [ ] **Step 4:** Run detection over the corpus households that have at least six months of rows; read every `active` series by eye; a habit or a transfer among them is a defect fixed with a synthetic case in `test_recurring_detection.py`.
- [ ] **Step 5:** Put into the pull request body: the spec's table with the "today" and "now" columns, the corpus aggregates with the caveat that the corpus is look-alike samples, what did not reach its target, and no corpus string.

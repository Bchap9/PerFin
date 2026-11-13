# Import 1b: bank and card PDF statements. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The generic bank and card statement reader recognises the headers, sections, periods, rows and balances that real statements print, never files a statement as something else, and counts every row it does not read.

**Architecture:** Everything stays in the worker-side document parsers (`app/ingestion/parsers/`), which read the stored, validated extraction. Header recognition moves out of `table_statement.py` (1,796 lines) into a new pure module `statement_header.py` first (Task 1, no behaviour change), so header work and row work then touch different files. Amount and date shapes are fixed in `text.py`, ordering and section checks in `reconcile.py`, parser choice in `registry.py`. The classifier's period and the day the file arrived reach the parsers on `DocumentView` (no change to the `parse` signature, none to the sandbox protocol). No new tables, no API change, one new review reason (`year_assumed`).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, PyMuPDF, reportlab (fixtures), pytest; vitest for the one copy entry. Run everything in the Linux dev container (`uv run ...` from `backend/`).

**Spec:** `docs/superpowers/specs/2026-11-12-import-reliability-design.md` (section "1b").

## Global Constraints

- Test first: write the failing test, run it and see it fail for the stated reason, then implement.
- Fixtures are synthetic and invented. Never copy a real sample's rows, names or numbers into the repository. New PDF builders use reportlab the way `tests/fixtures/synth_unseen_layouts.py` does (`_line`, `_rows`), invented institution names and a "synthetic test document" footer. Unless a task is about its absence, every statement builder prints a masked account number, a labelled period with years, an opening and a closing balance, and figures that reconcile.
- Every new `.py` file starts with `# SPDX-License-Identifier: AGPL-3.0-or-later`.
- Gates on the files you touch before each commit, from `backend/`: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and the tests named in the task.
- No test may be skipped or marked xfail (docs/DEVELOPMENT.md). A test that pinned behaviour this plan changes on purpose is rewritten in the task that changes it, and the commit message names it.
- The sandbox contract does not change: parsers read validated extraction output only; nothing is added to `app/ingestion/sandbox/protocol.py`. Counts go into the existing `skipped_lines` / `skipped_regions`, notes into `ParseInfo.warnings` (codes of at most 64 characters matching `^[A-Za-z0-9_.:-]*$`).
- No phantom rows. A rule that makes more lines into headers or rows carries a negative fixture in the same task (a balance table, a summary box, a notice, a title) that must stay unread. A printed sign, `CR`/`DR`/`DB` or a debit/credit column always beats a rule that infers direction.
- A row that is found and not imported is counted (`skipped_lines`, with its box in `skipped_regions`). A held file is better than a wrong import; a counted row is better than a vanished one.
- Every existing test under `tests/unit/test_statement_parsers.py`, `test_unseen_layouts.py`, `test_card_statement.py`, `test_statement_period.py`, `test_statement_accounts.py`, `test_synth_multi_account_fixtures.py`, `test_table_reading.py`, `test_statement_evidence.py`, `test_layout_template.py`, `test_receipt_parser.py` and `tests/integration/test_pdf_statement_pipeline.py`, `test_multi_account_statements.py` keeps passing unchanged unless the task says otherwise. This list is "the statement suite" below.
- Line numbers are those of `ab1f258`. Plan 1a edits `pipeline.py` and `imports/service.py` before this plan runs: find code by the function name given.
- Conventional commits (`fix(ingestion): ...`), one commit per task, ending with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`. Stage only the files of your task (`git add <paths>`), never `git add -A`.
- Review reasons are stable strings used by the frontend copy (`frontend/src/features/review/reviewCopy.ts`); a new one needs a copy entry there and a vitest assertion.

## Out of this plan

Headerless titled sections are not read (finding A4; Task 13 only counts their lines). Also out: one field per line (A5), decimal-comma and non-English months (F), extraction (E1 and E2 are in 1a), rotated watermark text (E3: it needs a different PyMuPDF text call inside the sandbox child and a new extraction of stored documents, so it is neither small nor safe here), two accounts with the same masked number (C6), description lines printed above their dated line (C7), the brokerage parser scoring on a card statement (D4), loan sub-accounts of a member statement (U5), and everything in `receipt.py` (plan 1c owns "no receipt for a multi-page document"). The score of a row dated outside the period (`conf.INFERRED`, audit 2.2) is left as it is: the spec's 1b bullets do not ask for a change and 1d decides what a clean read needs.

## Review Focus

1. **A checking statement that prints positive figures under "Deposits and additions" and "Electronic withdrawals", continued on page 2 under a repeated header.** The person expects withdrawals to be money out on both pages and the statement to balance. No test may show a withdrawal imported as income, or a `Beginning Balance` / `Ending Balance` line imported as a transaction (Tasks 10, 12, 15).
2. **A statement whose rows print `MM/DD` and that prints no period, with a notice dated later on the page.** The rows must all land in one run of the calendar, never split around the notice's date, each waiting with one question that names the year (Task 16).
3. **A statement that reads today.** A header rule must not swallow a title into a header, turn a `Date | Amount` daily-balance table into transactions, or change a payee. Every existing fixture reads the same rows (Tasks 6, 7, 8; the statement suite).
4. **A checking statement whose fee note mentions a credit card, and a card statement.** The first is read with bank signs; the second is still read by the card parser (Task 11).
5. **A page of activity with "Total" lines, and an itemised receipt classified as a receipt.** The first is never a receipt; the second still is (Task 4).
6. **A line with a date and an amount inside a table.** There is no path on which it disappears without being a row, a balance, a total or a counted skip (Tasks 9, 13, 17).

---

## Lanes

Task 1 comes first for lanes H and M. Lanes T, R and P do not depend on it and may start at once. A lane never edits another lane's files during the parallel phase; a red test that comes from another lane's half-finished file is reported, not fixed. The Final tasks need two lanes' output and run in order after all lanes are merged.

| Lane | Tasks | Owns |
|---|---|---|
| Start | 1 | `app/ingestion/parsers/statement_header.py` (create), `table_statement.py` (move only) |
| T | 2 | `app/ingestion/parsers/text.py`, `tests/unit/test_amount_shapes.py` |
| R | 3 | `app/ingestion/reconcile.py`, `tests/unit/test_reconcile_order.py` |
| P | 4, 5 | `app/ingestion/parsers/registry.py`, `base.py`, `app/ingestion/pipeline.py` (`_parse_hints`, `_parse_document` call only), `tests/unit/test_parser_choice.py`, `tests/unit/test_view_hints.py`, `tests/fixtures/synth_parser_choice.py` |
| H | 6, 7, 8 | `app/ingestion/parsers/statement_header.py`, `tests/unit/test_statement_header.py`, `tests/fixtures/synth_statement_headers.py` |
| M | 9, 10, 11, 12, 13, 14 | `app/ingestion/parsers/table_statement.py`, `tests/unit/test_bank_layouts.py`, `tests/fixtures/synth_bank_layouts.py`, `tests/unit/test_unseen_layouts.py`, `tests/unit/test_card_statement.py` |
| Final (in order) | 15, 16, 17, 18, 19, 20 | all of the above, `app/ingestion/parsers/institutions.py` (version only), `frontend/src/features/review/reviewCopy.ts`, `tests/integration/test_pdf_statement_pipeline.py`, docs |

Lane M's fixtures print plain one-line headers from today's vocabulary and amounts in shapes `read_amount` reads today, so lane M never waits for lanes H or T. Lane H's fixtures print rows the current row reader reads (figures under their headings, a date cell of its own).

**Names used across tasks.** Parse warnings: `rows_newest_first`, `section_total_mismatch`, `dates_carried`, `rows_without_header:<n>`, `table_across_unread:<n>`, `period_from_classifier`, `period_unlabelled`, `year_assumed:<YYYY>`; the existing `rows_outside_account` gains one more use. Review reason: `year_assumed` (kind `low_confidence_field`). No new counters: every count is added to `skipped_lines`.

---

### Task 1: Header recognition gets its own module (no behaviour change)

**Files:**
- Create: `backend/app/ingestion/parsers/statement_header.py`
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`Role`, `NUMERIC_ROLES`, `HEADERS`, `MONEY_ROLES`, `TABLE_HEADING_WORDS` at 185-289; `Column` at 486-490; `columns`, `_headless`, `_header_like` at 693-732; `has_table` 734; `parse` 1281; `read_statement` 1506; `CardStatementParser.detect` 1785)
- Test: `backend/tests/unit/test_statement_header.py` (create)

Why a module: Tasks 6 to 8 and 17 add about 300 lines of header logic (word matching, units, stacking, repeated groups). As pure functions on a `ViewLine` they are tested on hand-built lines without a PDF, and lane H can work beside lane M.

**Interfaces:**
- Produces in `statement_header.py`: `Role` (today's literal plus `"other"` and `"across"`, declared and not yet produced), `HEADERS`, `NUMERIC_ROLES`, `MONEY_ROLES`, `TABLE_HEADING_WORDS`, `Column`, and
  - `read_header(line: ViewLine, headers: Mapping[str, Role] = HEADERS) -> list[Column] | None` (today's `columns` plus `_headless`, moved),
  - `header_like(line: ViewLine, headers: Mapping[str, Role] = HEADERS) -> bool` (today's `_header_like`, moved),
  - `stack_headers(lines: Sequence[ViewLine], headers: Mapping[str, Role] = HEADERS) -> list[ViewLine]` (returns `list(lines)` until Task 8).
- `table_statement.py` re-exports the moved names (other modules and tests import them from there) and keeps `TableStatementParser.columns` and `_header_like` as one-line delegates passing `self.headers`, so a subclass that overrides `headers` or `columns` behaves as before.
- Produces `TableStatementParser.prepare(self, lines: Sequence[ViewLine]) -> list[ViewLine]` (calls `stack_headers`) and the keyword `read_statement(..., prepared: bool = False)`. `parse` prepares `view.lines` once and passes `prepared=True` to `read_statement`, `_parse_accounts` and `_parse_sections`; `has_table` and `CardStatementParser.detect` prepare the lines they look at; `read_statement` prepares only when `prepared` is false (the brokerage parser's call at `brokerage.py:3747`).

- [ ] **Step 1: Tests** in `test_statement_header.py` on hand-built lines (a local `_line(cells: list[tuple[str, float]]) -> ViewLine` like `test_card_statement._view`): `test_one_line_header_is_read` (`Date | Description | Amount | Balance` gives roles `date, description, amount, balance` with the cells' x spans); `test_headless_heading_gives_a_lead_column`; `test_a_line_of_headings_we_do_not_read_is_header_like` (`Date | Balance | Date | Balance` is `header_like` and `read_header` is None); `test_stack_headers_returns_the_lines_unchanged`; `test_moved_names_still_import_from_table_statement` (`HEADERS`, `Column`, `MONEY_ROLES`, `NUMERIC_ROLES` are the same objects in both modules).
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_statement_header.py`. Expected: import error (the module does not exist).
- [ ] **Step 3: Implement** the move exactly; no rule changes. Parser versions are not bumped.
- [ ] **Step 4:** Run the new file and the statement suite, plus `uv run pytest -q tests/unit -k "brokerage or fidelity or template or teach"`. Expected: pass with no test edited.
- [ ] **Step 5:** Commit `refactor(ingestion): statement header recognition in its own module`.

### Task 2: Amounts and dates in the shapes statements print (lane T)

**Files:**
- Modify: `backend/app/ingestion/parsers/text.py` (`_AMOUNT` 66-70, `read_amount` 95-118, `find_amounts` 149-168, `_D_MON_Y` 72)
- Test: `backend/tests/unit/test_amount_shapes.py` (create)

- [ ] **Step 1: Failing tests** (parametrised, values invented):
  - `test_debit_marker_db`: `read_amount("4,120.00 DB")` has value `4120.00` and `credit is False`; `"75.10Db"` likewise.
  - `test_detached_symbol_and_sign`: `"$ 1,480.25"` reads `1480.25` with `symbol == "$"`; `"- $310.00"`, `"-$ 310.00"` and `"- 310.00"` read `-310.00` with `negative_marked`; `"+ 25.00"` reads `25.00` with `positive_marked`.
  - `test_gap_before_the_decimal_point`: `"7,310 .00"` reads `7310.00`; `"$12 ,040 .50"` reads `12040.50`.
  - `test_footnote_mark_after_the_figure`: `"642.18*"`, `"642.18 *"`, `"642.18†"`, `"642.18¹"` read `642.18`; `"18.00-*"` reads `-18.00`.
  - `test_still_not_amounts`: `"*"`, `"- -5.00"`, `"5 .0"`, `"12 34.00"`, `"1,23.00"`, `"(5.00"` are None.
  - `test_find_amounts_joins_a_lone_symbol`: `find_amounts("Ending balance $ 2,010.40")` gives one amount `2010.40` with symbol `$`; `find_amounts("Fee - $12.00")` gives `-12.00`.
  - `test_an_equation_line_keeps_its_operators`: `find_amounts("900.00 - 150.00 + 40.00 = 790.00")` gives `[900.00, 150.00, 40.00, 790.00]`, all positive.
  - `test_find_amounts_reads_db`: `find_amounts("310.00 DB 55.00 CR")` gives credit flags `[False, True]`.
  - `test_month_name_date_with_two_digit_year`: `read_date("03-Nov-25", "d_mon_y") == date(2025, 11, 3)`; `read_date("03/Nov/25", "d_mon_y")` likewise; `read_date("03 Nov 25", "d_mon_y") is None` (a space-separated two-digit year is not read: `16 Dec 20` may be a day, a month and a count).
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_amount_shapes.py`. Expected: each new shape returns None.
- [ ] **Step 3: Implement.** In `read_amount`, before the pattern: drop whitespace that stands between a digit and a `.` or `,` followed by a digit; drop one trailing footnote mark (`*`, `†`, `‡`, `§`, `¹`, `²`, `³`), with or without a space before it. In `_AMOUNT`: at most one space after the sign and after the currency symbol; the marker group also accepts `DB` / `Db` / `db` (debit). In `find_amounts`: a token that is only a currency symbol is joined to the next token; a token that is only a sign is joined when the next token starts with a currency symbol (or is a lone symbol), never before bare digits; `DB` follows a figure like `CR` / `DR`. `_D_MON_Y` accepts a two-digit year only when the parts are joined by `-` or `/`. Every pattern stays anchored and free of nested quantifiers (module doc).
- [ ] **Step 4:** Run the new file and `uv run pytest -q tests/unit/test_statement_parsers.py tests/unit/test_card_statement.py tests/unit/test_receipt_parser.py tests/unit -k "brokerage or loan or payslip"`. Expected: pass; `test_not_amounts` and `test_find_amounts_and_long_input` unchanged.
- [ ] **Step 5:** Commit `fix(ingestion): read DB markers, detached signs and symbols, and footnote marks on amounts`.

### Task 3: Newest-first order and section totals as pure checks (lane R)

**Files:**
- Modify: `backend/app/ingestion/reconcile.py`
- Test: `backend/tests/unit/test_reconcile_order.py` (create)

**Interfaces:**
- Produces `newest_first(dates: Sequence[date | None]) -> bool`: true when at least 3 dates are present and, ignoring None, no date is later than the one before it and the first is later than the last.
- Produces `section_mismatches(rows: Sequence[Row], sections: Sequence[int], totals: Mapping[int, Decimal]) -> list[int]`: the section ids (sorted) for which `abs(abs(sum of the amounts of the rows with that id) - abs(total))` exceeds `TOLERANCE`. `sections[i]` is the section of `rows[i]`; a section with a total and no rows compares against zero.

- [ ] **Step 1: Failing tests:** `test_descending_dates_are_newest_first`; `test_ascending_equal_and_two_row_lists_are_not` (ascending, all equal, two rows, a list that goes down then up); `test_none_dates_are_ignored`; `test_reversed_rows_reconcile` (20 invented rows with running balances printed newest first: `running_mismatches(opening, rows)` is long, and on `rows[::-1]` it is `[]`); `test_section_total_that_agrees`, `test_section_total_that_disagrees_names_its_section` (sections `[1, 1, 2, 2, 2]`, totals `{1: 300.00, 2: 99.00}` with section 2 summing to `-120.00`: result `[2]`); `test_sign_of_the_printed_total_does_not_matter`; `test_section_without_a_total_is_not_checked`.
- [ ] **Step 2:** Run; expected: import error for the two names.
- [ ] **Step 3: Implement** both functions; `reconcile`, `running_mismatches` and `doubtful_rows` are untouched.
- [ ] **Step 4:** Run the new file and `tests/unit/test_statement_parsers.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(ingestion): order and section-total checks for statement reconciliation`.

### Task 4: A statement is never filed as a receipt (lane P)

**Files:**
- Create: `backend/tests/fixtures/synth_parser_choice.py`
- Modify: `backend/app/ingestion/parsers/registry.py` (`parse_extraction` 191-196, module doc)
- Test: `backend/tests/unit/test_parser_choice.py` (create)

Decision: the rule lives in the registry, not in `ReceiptParser.detect`. `detect` sees only the pages; the rule needs the classifier's kind, and `_hinted` only picks a parser whose score is above 0, so zeroing `detect` would also take the receipt parser away from a document classified as a receipt. `receipt.py` is not edited.

**Interfaces:**
- Produces `_without_receipt(scores: dict[str, float], kind: str | None) -> dict[str, float]` in `registry.py`: when `kind != "receipt"` and (`kind == "statement"` or `scores["statement_generic"] > 0`, which means a transaction table header is on the pages), the score of every parser whose `kind` is `"receipt"` becomes `0.0`. Applied to `scores` before `_hinted`, the best-score choice and `_fallback`; `ParseInfo.scores` records the result.
- Produces builders returning `bytes`: `activity_page_with_totals()` (one page, a `Date | Description | Amount` table of 6 rows dated `MM/DD/YYYY`, then `Subtotal`, `Tax` and `Total` lines and "Thank you", none of the statement phrases of `receipt._STATEMENT_WORDS`); `receipt_with_item_table()` (a shop receipt whose items sit under `Date | Description | Amount`, with subtotal, tax, total and a card tender line).

- [ ] **Step 1: Failing tests:**
  - `test_a_page_with_a_transaction_table_is_not_a_receipt`: `parse_extraction(extract(activity_page_with_totals()))` has `parse.parser.name == "statement_generic"`, `parse.receipt is None`, 6 transactions, and the recorded score of `receipt_basic` is `0.0`.
  - `test_a_statement_classification_vetoes_the_receipt_parser`: the existing `synth_receipts.receipt_pdf(receipt_spec(4242))` (no table) parsed with `kind="statement"`: `parse.parser is None`, warnings contain `no_parser`, `parse.receipt is None`.
  - Guards that pass today and must keep passing: `test_a_classified_receipt_with_an_item_table_is_still_a_receipt` (`parse_extraction(extract(receipt_with_item_table()), kind="receipt")` has parser `receipt_basic` and a receipt whose total is the builder's); `test_receipts_without_a_table_are_unchanged` (the same existing receipt PDF with no kind is still `receipt_basic`).
- [ ] **Step 2:** Run; expected: the first two name `receipt_basic`.
- [ ] **Step 3: Implement** as the interface says.
- [ ] **Step 4:** Run the new file, `tests/unit/test_receipt_parser.py`, `tests/unit/test_receipt_accuracy.py`, `tests/integration/test_receipt_pipeline.py`, `tests/unit/test_statement_parsers.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): a page with a transaction table, or classified as a statement, is never a receipt`.

### Task 5: The classifier's period and the day of receipt reach the parsers (lane P)

**Files:**
- Modify: `backend/app/ingestion/parsers/base.py` (`DocumentView` 178-238), `registry.py` (`parse_extraction` signature and the view it builds), `backend/app/ingestion/pipeline.py` (`_parse_hints` neighbourhood and the `parse_extraction` call inside `_parse_document`)
- Test: `backend/tests/unit/test_view_hints.py` (create)

Decision: hints travel on the view, so `DocumentParser.parse` keeps its signature and no other parser changes.

**Interfaces:**
- Produces in `base.py`:
  - `@dataclass(frozen=True) class ViewHints: period: tuple[date, date] | None = None; period_confidence: float = 0.0; not_after: date | None = None`.
  - `DocumentView.hints: ViewHints = ViewHints()` and `DocumentView.created: date | None = None`, both defaulted, so every existing constructor call still works.
  - `created_day(text: str | None) -> date | None`: the day of a PDF creation date (`D:YYYYMMDD...` or `YYYYMMDD...`, year 1990 or later, a real calendar day), else None. `DocumentView.from_result` fills `created` from `result.meta.created`.
  - `DocumentView.ceiling` (property) `-> date | None`: the earlier of `created` and `hints.not_after`; None when neither is known.
- Produces `parse_extraction(..., period: tuple[date, date, float] | None = None, not_after: date | None = None)`; the view is `dataclasses.replace(DocumentView.from_result(result), hints=ViewHints(...))`.
- Produces `_period_hint(document: Document) -> tuple[date, date, float] | None` in `pipeline.py`: `document.confidence["period"]` when its `inferred` is false, both ISO dates parse, start is not after end, and `document.confidence["fields"]["period"]` is at least `classify.service.FIELD_THRESHOLD`; the third element is that confidence. `_parse_document` passes `period=_period_hint(document)` and `not_after=document.created_at.date()`.
- Nothing reads the hints yet (Task 16 does).

- [ ] **Step 1: Failing tests:** `test_created_day[...]` (`"D:20260312101500+00'00'"` gives `date(2026, 3, 12)`; `"20260312"` likewise; `"D:20261340"`, `"yesterday"`, `None`, `"D:18990101"` give None); `test_view_carries_the_pdf_creation_day` (a reportlab PDF extracted in process has a `created` within one day of `date.today()`; one day of slack for the time zone); `test_ceiling_is_the_earlier_day` (created 2026-04-02 and `not_after` 2026-03-31 give 2026-03-31; only one known gives that one; none gives None); `test_parse_extraction_hands_hints_to_the_view` (patch one parser's `parse_with` to capture the view: `view.hints.period == (start, end)`, `period_confidence == 0.95`, `not_after` as passed); `test_period_hint_reads_only_a_printed_period` (a `Document` with `confidence={"period": {"inferred": False, "start": "2026-03-01", "end": "2026-03-31", "source": "label"}, "fields": {"period": 0.95}}` gives the tuple; `inferred: True`, a missing end, or `fields.period` 0.4 give None).
- [ ] **Step 2:** Run; expected: attribute and import errors.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the new file, the statement suite and `tests/integration/test_document_extraction_pipeline.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(ingestion): hand the classifier's period and the day of receipt to the document parsers`.

### Task 6: Two headings in one cell (lane H)

**Files:**
- Create: `backend/tests/fixtures/synth_statement_headers.py`
- Modify: `backend/app/ingestion/parsers/statement_header.py` (`read_header`, `header_like`)
- Test: `backend/tests/unit/test_statement_header.py`

**Interfaces:**
- Produces `split_heading_cell(cell: Cell, headers: Mapping[str, Role]) -> tuple[tuple[Role, Cell], ...] | None`: for a cell of 2 to 12 words that is no heading as a whole, the fewest known phrases that use **every** word, in order, each with the span of its own words (`Cell.word_cells`, `join_cells`); None when a word is left over.
- `read_header` order of work: (1) whole cells as today; (2) each unmatched cell of two words or more through `split_heading_cell`; (3) a second cell with role `date` becomes `post_date` when no `post_date` exists (its values are claimed and not read, as `post_date` already is); (4) an unmatched cell that starts right of the leftmost money column becomes a `Column("other", x0, x1)`; unmatched cells elsewhere are ignored as today; (5) the header test and `_headless` as today. Steps 2 to 4 are skipped on a line that holds an amount or has more than 12 cells.
- `header_like` is true when the line has no amount and, after splitting, at least two heading phrases and no unmatched cell.
- Produces builders returning `bytes`, each an invented one-page checking statement with a labelled period, a summary and 6 rows that reconcile: `merged_heading_checking()` (header `DATE | DESCRIPTION | DEPOSIT | WITHDRAWAL BALANCE`, the last two words 5 points apart), `amount_before_description()` (header `Date | Amount Description`), `two_dates_and_category()` (header `Trans Date Post Date | Description | Amount | Category`; every row prints both dates and a one-word category right of the amount), and `daily_balance_after_rows()` (a `Date | Description | Amount` table followed by a block titled `DAILY ENDING BALANCE` with the header `Date | Amount | Date | Amount Date | Amount` and three lines of date and balance pairs).

- [ ] **Step 1: Failing tests:**
  - Unit, hand-built lines: `test_two_headings_in_one_cell_are_two_columns` (`WITHDRAWAL BALANCE` gives `debit` and `balance`, each with its own word span); `test_date_then_two_word_heading_in_one_cell` (`Date Transaction Description | Withdrawal | Deposit | Balance` gives `date, description, debit, credit, balance`); `test_a_cell_with_a_leftover_word_is_not_split` (`Amount Enclosed`, `Date of Birth`); `test_second_date_heading_is_the_post_date` (`Transaction Date | Posting Date | Description | Amount`: roles `date, post_date, description, amount`); `test_unknown_heading_right_of_the_money_is_an_other_column`; `test_prose_is_no_header` (a 14-word sentence containing "date", "description" and "amount" gives None); `test_header_like_reads_words` (`Date | Amount | Date | Amount Date | Amount` is `header_like` and no header).
  - Documents, through `parse_extraction(extract(...), kind="statement")`: `test_merged_heading_statement_reads_both_columns` (6 rows, `reconciliation.status == "ok"`, withdrawals negative); `test_amount_before_description_is_read` (6 rows, payees are the builder's); `test_category_is_not_joined_to_the_amount` (6 rows, no row skipped, no payee contains a category word); `test_daily_balances_are_not_transactions` (exactly the table's rows; none dated and valued like a daily-balance line; `skipped_lines == 0`).
- [ ] **Step 2:** Run; expected: `no_table_header` or missing rows for the first three documents and extra rows for the fourth.
- [ ] **Step 3: Implement** as the interface says.
- [ ] **Step 4:** Run the new tests and the statement suite. Expected: pass with no existing test edited.
- [ ] **Step 5:** Commit `fix(ingestion): read two column headings printed in one cell`.

### Task 7: Reworded headings, units, the one unknown cell, a damaged word (lane H)

**Files:**
- Modify: `backend/app/ingestion/parsers/statement_header.py` (`HEADERS`, `read_header`), `backend/tests/fixtures/synth_statement_headers.py`
- Test: `backend/tests/unit/test_statement_header.py`

**Interfaces:**
- Vocabulary added to `HEADERS` (normalised text, so `/`, `&` and brackets are already spaces):
  - `date`: `date of transaction`, `date posted`, `effective date`, `txn date`, `tran date`.
  - `description`: `information`, `narration`, `merchant name`, `merchant name or transaction description`, `description of transaction or credit`, `transaction detail`, `activity description`.
  - `credit`: `deposits credits`, `deposits additions`, `deposits other credits`, `deposits and other credits`, `deposits and credits`, `deposits and additions`, `additions`, `deposit amt`, `deposit amount`, `credit amount`.
  - `debit`: `withdrawals debits`, `withdrawals subtractions`, `withdrawals other debits`, `withdrawals and other debits`, `withdrawals and debits`, `withdrawals and subtractions`, `subtractions`, `other debits`, `withdrawal amt`, `withdrawal amount`, `debit amount`.
  - `balance`: `ending daily balance`, `daily balance`, `closing balance`.
  - `amount`: `transaction amount`. `check`: `check nbr`, `chk`.
  - Not added, on purpose: `payments`, `charges`, `receipts` (their direction differs between layouts).
- Produces `heading_role(text: str, headers: Mapping[str, Role]) -> Role | None`: the role of the normalised text; else, for a heading whose role is in `NUMERIC_ROLES`, the role after dropping one trailing unit: a bracketed group of at most 12 characters (`Amount (SGD)`, `Balance (USD)`) or one trailing three-letter word in capitals (`AMOUNT EUR`). `read_header` and `split_heading_cell` use it for every lookup.
- The one unknown cell: when the line names a date and a money column and no description or check column, and **exactly one** cell that is not right of the leftmost money column is still unmatched, that cell is the description when it lies between the date column's right edge and the first money column's left edge (1 unit of slack), has at most 4 words and no digit, and none of its words is `balance`, `total`, `summary`, `amount`, `date`, `rate`. Otherwise the line is no header.
- Produces `repair_heading(cell: Cell, headers: Mapping[str, Role]) -> Role | None`: only for a cell read by OCR (`cell.conf is not None`) of one word with 6 letters or more: the role of the single one-word heading of 6 letters or more at edit distance 1 (one letter added, lost or changed); None when two headings are that close. No warning: the rows' fields are already scaled by the OCR confidence.
- Produces builders: `reworded_headings_checking()` (header `Txn Date | Narration | Withdrawals / Debits | Deposits / Credits | Ending Daily Balance`), `amount_with_unit()` (header `Date | Details | Amount (CAD) | Balance (CAD)`), `unknown_description_heading()` (header `Date | Memo Text | Amount`), and `date_balance_pairs_with_title()` (a line `Date | DAILY ENDING BALANCE | Amount` over lines of a date and a figure: must stay unread).

- [ ] **Step 1: Failing tests:** `test_new_heading_words[...]` (one case per added phrase, asserting its role through `heading_role`); `test_unit_after_a_money_heading`; `test_unit_is_not_dropped_from_a_text_heading` (`Description (USD)` is no heading); `test_single_unknown_cell_between_date_and_money_is_the_description`; `test_two_unknown_cells_are_no_header`; `test_unknown_cell_naming_a_balance_is_not_a_description` (the `date_balance_pairs_with_title` line gives None); `test_cut_heading_is_the_description` (`Date | Descri | Amount`); `test_ocr_damaged_heading_is_repaired` (`ithdrawal` with `conf=0.9` is `debit`; the same cell with `conf=None` is not; `Deposit5` is `credit`); `test_short_words_are_not_repaired` (`Dat`, `Debi`); documents: `test_reworded_statement_reads_and_reconciles` (rows of the builder, status `ok`), `test_amounts_with_a_unit_are_read`, `test_unknown_description_heading_statement_is_read`, `test_title_between_date_and_amount_makes_no_table` (no statement, or a statement with zero transactions, and `skipped_lines == 0`).
- [ ] **Step 2:** Run; expected: None roles and `no_table_header`.
- [ ] **Step 3: Implement.** Check first that no existing fixture prints `Closing Balance`, `Daily Balance` or `Additions` as a label cell beside a bare `Date` label (`grep -rn` over `tests/fixtures`); `_bare_date_labels` treats a line with another heading as a table header, so such a fixture would lose its statement date. If one does, stop and report instead of dropping the word.
- [ ] **Step 4:** Run the new tests and the statement suite. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): more statement headings, units after a money heading, and the lone unknown cell as description`.

### Task 8: Headers printed over two or three lines (lane H)

**Files:**
- Modify: `backend/app/ingestion/parsers/statement_header.py` (`stack_headers`), `backend/tests/fixtures/synth_statement_headers.py`
- Test: `backend/tests/unit/test_statement_header.py`

**Interfaces:**
- `stack_headers` replaces a group of 3 (tried first) or 2 consecutive lines by one line when all hold:
  1. same page; no line of the group holds an amount; no line is longer than 130 characters;
  2. the tops of consecutive lines are at most `2.2 * max(height of the first line, 6.0)` apart;
  3. the merged line is a header by `read_header`, where cells whose x spans overlap are one cell (text top to bottom, then left to right; words kept for `word_cells`). A merged cell may be split into several headings only when those headings stand side by side (their x spans do not overlap); headings one above the other must form one phrase, so a right-hand title `Withdrawals` over `Amount` is no second column;
  4. the merged header names **more** columns than any single line of the group names by itself (a tie keeps the single line, so nothing that reads today changes);
  5. every cell of the group's first line overlaps a cell of a later line of the group (a title that stands over nothing is no header part), and every line of the group gives at least one cell to a merged cell that names a role;
  6. the lone-unknown-cell rule of Task 7 may fire on the merged line only when that cell comes from a line that also holds a matched heading.
  The merged line takes the last line's `page`, `index` and `height`; its cells keep their own boxes. Lines outside a group pass through. At most 3 lines are ever merged.
- Produces builders: `stacked_header_checking()` (two header lines, `Check | Deposits/ | Withdrawals/ | Ending daily` 9 points over `Date | Number Description | Credits | Debits | balance`, where `Number` and `Description` are 6 points apart and so one cell; `M/D` rows; unsigned figures under their columns; the balance printed only on the last row of each day; 10 rows, 2 of which print a check number and no description), `description_on_its_own_line()` (line 1 `POSTING | DEPOSITS & OTHER | WITHDRAWALS & | DAILY`, line 2 only `TRANSACTION DESCRIPTION`, line 3 `DATE | CREDITS (+) | OTHER DEBITS (-) | BALANCE`), `card_long_heading()` (`Date of` over `Transaction`, `Merchant Name or Transaction Description`, `$ Amount`; a card statement with `Minimum Payment Due`), `title_over_header()` (a bold `Deposits and other credits` 14 points over `Date | Description | Amount`, and a right-hand title `Balance` 14 points over a header without a balance column).

- [ ] **Step 1: Failing tests:** unit: `test_two_line_header_is_one_header` (roles and that the `credit` column spans both `Deposits/` and `Credits`); `test_three_line_header_with_a_single_cell_line`; `test_a_title_over_a_header_is_not_stacked` (both titles of `title_over_header`: the header line comes back unchanged and the title is still its own line); `test_a_title_over_the_amount_heading_is_no_second_column` (`Withdrawals` right-aligned over `Amount`: not stacked); `test_a_header_that_reads_alone_wins_a_tie`; `test_lines_far_apart_are_not_stacked` (tops 3 line heights apart); `test_a_line_with_an_amount_is_never_part_of_a_header`; `test_stacking_stops_at_three_lines`; documents: `test_stacked_header_statement_reads_and_reconciles` (10 rows, deposits positive and withdrawals negative by column, 2 rows whose payee starts `CHECK`, status `ok`), `test_description_on_its_own_line_statement` (rows and status `ok`), `test_card_long_heading_is_read_as_a_card` (parser `card_statement_generic`, purchases negative).
- [ ] **Step 2:** Run; expected: `no_table_header` for the three documents.
- [ ] **Step 3: Implement** as the interface says.
- [ ] **Step 4:** Run the new tests, the statement suite and `uv run pytest -q tests/unit -k "brokerage or fidelity"` (the brokerage parser calls `read_statement`). Expected: pass with no existing test edited.
- [ ] **Step 5:** Commit `fix(ingestion): read statement headers printed over two or three lines`.

### Task 9: Rows whose cells sit a little off their columns (lane M)

**Files:**
- Create: `backend/tests/fixtures/synth_bank_layouts.py`
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`_assign` 759-778, `_date_shaped` 780-785, `_split_lead` 787-815, `_collect` 1055-1063, `_signed` 1180-1184, `summary_lines` 851-853, `balance_sections` 1396-1397)
- Test: `backend/tests/unit/test_bank_layouts.py` (create)

**Interfaces:**
- Produces `TableStatementParser._split_fused_date(self, cells: dict[Role, list[Cell]], columns: Sequence[Column]) -> None`, called right after `_split_lead` at its three call sites. It acts only when the `date` role has no cell: the leftmost assigned cell, if its `x0` is between `date.x0 - 25` and `date.x1`, loses its first one to three words when they are date-shaped; those become the date cell and the rest stays in the description (in front of what is there). When the table has exactly one money column, no money cell was assigned, and the last remaining word reads as an amount, that word becomes the money cell.
- `_assign`: after today's overlap and nearest-centre rules, a cell still unassigned that reads as an amount goes to the last column (by `x0`) that starts at or left of the cell's centre, when that column's role is in `NUMERIC_ROLES`; otherwise it stays unassigned.
- `_signed`: with debit and credit both printed, a zero in one of them is absent (`250.00 | 0.00` is `-250.00`); two non-zero figures or two zeros stay unread and counted, as today.
- `_date_shaped`: a year-less numeric text is date-shaped only when it can be a day and a month in one order (one part 1 to 12, the other 1 to 31).
- Produces builders with expected rows in `EXPECTED_ROWS[name]` (`list[tuple[date, Decimal, str]]`): `left_heading_right_figures()` (`Debit | Credit | Balance` headings 20 points wide at the left of 70-point columns whose figures are right-aligned; 8 rows; reconciles), `tight_date_checking()` (the date 2 points before the description, so both are one cell; 7 rows; reconciles), `tight_date_one_money_column()` (the same with `Date | Description | Amount` and the amount 3 points after the description), `zero_placeholder_columns()` (`Credits | Debits | Balance`, every row printing `$250.00 | -$0.00` style pairs; 6 rows; reconciles).

- [ ] **Step 1: Failing tests** (documents through `parse_extraction(extract(...), kind="statement")` unless noted):
  - `test_right_aligned_figures_outside_their_heading_are_read`: rows equal `EXPECTED_ROWS`, status `ok`, `skipped_lines == 0`.
  - `test_a_date_fused_with_the_description_is_split`: 7 rows with the builder's dates; no payee starts with a digit.
  - `test_fused_date_and_trailing_amount`: rows of `tight_date_one_money_column`.
  - `test_zero_beside_the_amount_is_absent`: 6 rows, signs by column, status `ok`.
  - `test_two_figures_in_debit_and_credit_are_still_skipped` (hand-built view): `skipped_lines == 1`.
  - `test_an_amount_under_no_numeric_column_is_not_moved` (hand-built: an amount-shaped cell whose centre lies over the description column stays description text).
  - `test_an_impossible_day_and_month_is_no_date` (hand-built: `2.80` in the date column does not make a dated row).
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_bank_layouts.py`. Expected: zero rows with skipped counts (first, fourth) and zero rows uncounted (second, third).
- [ ] **Step 3: Implement** as the interface says. Each rule acts only on a cell or row that is dropped today.
- [ ] **Step 4:** Run the new file and the statement suite. Expected: pass with no existing test edited.
- [ ] **Step 5:** Commit `fix(ingestion): read rows whose date or amount sits outside its column`.

### Task 10: Balances are balances (lane M)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`opening_labels` / `closing_labels` 617-631, `_label_amount` 871-879, `_scan_summary` 881-886, `_scan_above` 974-1002, `_Summary` 516-537, `_collect` 1069-1088, `_opening_row` 1352-1360, `read_statement` 1602-1603), `backend/tests/fixtures/synth_bank_layouts.py`
- Test: `backend/tests/unit/test_bank_layouts.py`

**Interfaces:**
- Labels added. `opening_labels`: `previous statement balance`, `last statement balance`, `balance last statement`, `prior balance`. `closing_labels`: `balance this statement`.
- `_label_amount(self, line, labels, *, inside: Sequence[str] = ())`: labels are tried longest first; a match that lies inside a match of one of `inside` on the same text is no match (the rule `_date_label_values` already uses); the closing lookup passes `inside=self.opening_labels` and additionally refuses a match whose preceding word is `previous`, `last` or `prior`.
- Produces `TableStatementParser._balance_row(self, desc_norm: str, cells: Mapping[Role, list[Cell]]) -> tuple[Literal["opening", "closing"], Amount | None] | None`: for a dated line whose description starts with an opening or closing label, the kind and its figure: the balance cell when it reads, else the rightmost money cell that reads. `_collect` calls it before it looks at the amounts and never makes such a line a row (opening: first one wins; closing: last one wins; both set the `*_in_table` flag). `_opening_row` uses it too, so `balance_sections` sees an opening row whose figure sits in a money column.
- Label and amount on neighbouring lines: a line that is an opening or closing label and holds no amount takes the amount of an **amount-only line** (one cell, which reads as an amount) that is its neighbour in line order on the same page, with tops at most `1.5 *` the label line's height apart: the next line first, else the previous one. `_Summary` gains `opening_adjacent: bool` and `closing_adjacent: bool`; `read_statement` scores such a balance `conf.DERIVED` instead of `self.base`.
- Produces builders: `summary_table_balances()` (a summary box with the header `Date | Description | Amount` holding dated `Beginning Balance` and `Ending Balance` lines, then the real table under the same header wording; 6 rows; reconciles), `summary_label_wrapped()` (`Beginning Balance on`, the figure and the date printed as three lines 5 points apart, the figure to the right of the label; `Ending Balance` on one line; 5 rows; reconciles), `previous_statement_balance_first()` (a summary that prints `Previous Statement Balance` first and `New Balance` three lines later; reconciles only when the first is the opening balance).

- [ ] **Step 1: Failing tests:** `test_balance_lines_in_a_money_column_are_not_rows` (6 rows, opening and closing equal the builder's, status `ok`; no transaction's payee contains `Balance`); `test_label_and_figure_on_neighbouring_lines` (opening read, status `ok`, `confidence["opening_balance"] == 0.9`); `test_an_amount_two_lines_away_is_not_taken` (hand-built: label, a text line, then a figure: opening stays None); `test_previous_statement_balance_is_the_opening_balance` (opening is the first figure, closing the `New Balance` figure, status `ok`); `test_longest_label_wins` (hand-built line `Balance last statement 410.00` is opening; `Last statement balance` likewise; `Statement balance 55.00` is closing); `test_shares_with_opening_rows_in_the_amount_column_are_two_accounts` (the `credit_union_shares` layout redrawn in the test with its two `Beginning Balance` figures under `Deposit`: two statements, warning `accounts_by_balance`).
- [ ] **Step 2:** Run; expected: 8 rows with two balance lines among them; `unavailable`; a closing balance equal to the previous balance.
- [ ] **Step 3: Implement** as the interface says.
- [ ] **Step 4:** Run the new tests and the statement suite. Expected: pass with no existing test edited.
- [ ] **Step 5:** Commit `fix(ingestion): a balance line is never a transaction; read balances beside or below their label`.

### Task 11: The right statement parser (lane M)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`TableStatementParser.detect` 747-755, `CARD_VOCABULARY` and `CardStatementParser.detect` 1756-1796), `backend/tests/fixtures/synth_bank_layouts.py`
- Test: `backend/tests/unit/test_bank_layouts.py`, `backend/tests/unit/test_card_statement.py`

**Interfaces:**
- A parser with `fingerprints` scores above 0 only when **every** phrase is on page 1 (`fingerprint_score(view) == 1.0`): `1.0` with a table, `0.54` without, else `0.0`.
- `CardStatementParser.detect` (no fingerprints): the phrases of `CARD_VOCABULARY` and `WEAK_CARD_VOCABULARY` count only on summary lines of at most `CARD_CUE_WORDS = 8` words; and a summary line of at most 8 words on page 1 that holds one of `DEPOSIT_ACCOUNT_WORDS = ("checking", "savings", "deposits", "withdrawals")` as a whole word returns `0.0` whatever card words are there.
- Produces builders: `checking_with_card_note()` (a checking statement titled `Business Checking`, a `Date | Description | Amount | Balance` table of 6 signed rows that reconcile, and a 22-word fee note that mentions `business credit card`), `other_current_account()` (an invented bank's `Current Account Statement`, day-first dates, `Money out | Money in | Balance`, sterling; prints none of the other fingerprint phrases), `other_card_ending_in()` (an invented issuer's card statement that prints `Card ending in 4410` and `Minimum Payment Due`).

- [ ] **Step 1: Failing tests:** `test_a_card_mentioned_in_a_note_does_not_make_a_card_statement` (parser `statement_generic`, account type not `creditcard`, signs as printed, status `ok`); `test_one_fingerprint_phrase_is_not_the_institution[mock_savings_and_loan]` (`other_current_account`: the recorded score of `mock_savings_and_loan` is `0.0`, parser `statement_generic`, `account.institution is None`); `...[demo_card_services]` (`other_card_ending_in`: score `0.0`, parser `card_statement_generic`); `test_a_card_statement_is_still_a_card` (the three existing card fixtures, `synth_card_statement.card_pdf`, `synth_unseen_layouts.card_statement` and `synth_pdf_statements.demo_card_pdf` unhinted, are read by a card parser); in `test_card_statement.py`: `test_card_words_in_a_long_sentence_are_no_cue` (hand-built 12-word line containing `credit limit`), `test_a_deposit_account_word_vetoes_the_card_reader` (`Corporate Card` plus a line `Checking Summary`).
- [ ] **Step 2:** Run; expected: `card_statement_generic` on the first, scores of `0.8` on the two fingerprint cases.
- [ ] **Step 3: Implement.** Before changing the card rule, confirm with a `grep -n -i` over the three card fixtures that none prints one of the four deposit words on a short page-1 line outside its rows; if one does, stop and report instead of loosening the rule.
- [ ] **Step 4:** Run the new tests and the statement suite (`test_institution_layouts_parse_exactly_and_reconcile` covers the four fictional layouts hinted and unhinted). Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): card words in a note and a single fingerprint phrase no longer choose the parser`.

### Task 12: A section title sets the direction of unsigned amounts (lane M)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`sections` 671-682, `_section` 1023-1031, `_Row` 497-513, `_collect` 1033-1063 and the row it builds, `_signed` 1168-1184, `_type` 1272-1279), `backend/tests/fixtures/synth_bank_layouts.py`
- Test: `backend/tests/unit/test_bank_layouts.py`

**Interfaces:**
- Produces `Direction = Literal["in", "out"]`, `SectionRule(NamedTuple): bank: Direction | None; card: Direction | None; kind: str | None` and `SECTION_TITLES: Final[Mapping[str, SectionRule]]`, replacing the `sections` mapping (class attribute `section_titles`). A title matches a key when its normalised text, after a trailing `continued` or `cont` is dropped, equals the key or starts with the key and a space; the longest key wins. Rows of the table (`bank`, `card`, `kind`):

  | Keys | bank | card | kind |
  |---|---|---|---|
  | `deposits and additions`, `deposits and other additions`, `deposits and other credits`, `deposits and credits`, `deposits credits`, `deposits`, `additions`, `electronic deposits`, `electronic credits` | in | none | none |
  | `other credits`, `credits` | in | in | none |
  | `withdrawals and other debits`, `withdrawals and other subtractions`, `withdrawals and debits`, `withdrawals and subtractions`, `withdrawals debits`, `withdrawals`, `other withdrawals`, `electronic withdrawals`, `electronic debits`, `atm and debit card withdrawals`, `atm debit card withdrawals`, `subtractions`, `checks paid`, `checks cleared`, `checks` | out | none | none |
  | `other debits`, `debits`, `purchases and adjustments`, `purchases` | out | out | none |
  | `payments and other credits`, `payments and credits`, `payments credits`, `payments` | none | in | none |
  | `new charges`, `cash advances`, `balance transfers` | none | out | none |
  | `fees charged`, `service fees`, `service charges`, `fees` | out | out | `fee` |
  | `interest charged` | out | out | `int` |
  | `interest paid`, `interest earned` | in | none | `int` |
  | `interest` | none | out | `int` |
  | `transactions` | none | none | none |

  A title has no direction when the words after the matched key name the other side (`deposits and withdrawals`: any of `withdrawals`, `debits`, `subtractions`, `checks`, `fees`, `charges`, `purchases` after an "in" key; any of `deposits`, `credits`, `additions`, `payments` after an "out" key). A line is no section title at all when it holds an amount, has more than 8 words, or contains one of `summary`, `calculation`, `rate`, `information` (so `Interest summary` no longer types the rows under it).
- Where a title is looked for: (a) between rows under one header, as today; (b) **above a header**: the nearest of the two lines before the header (in the prepared line list, same page) that has letters and no amount.
- At a header line, `_collect` decides: a title above it starts that title's section; no title, the header on a later page than the last row read, and the same roles as the current columns, is a **repeated page header**: direction and kind are kept; anything else resets them as today. `pending` is reset at every header, as today (a pending block is not carried over a page break).
- `_section(self, norm: str) -> tuple[bool, SectionRule | None] | None` (pending, rule). `_Row` gains `direction: Direction | None = None` and `section: int = 0` (a counter that goes up each time a section starts or is reset; Task 15 uses it). `_signed`: an amount from the `amount` column with no `CR`/`DR`/`DB`, no minus, plus or brackets, on a row with a direction, is `+abs` for `in` and `-abs` for `out`; every other case is as today. The value of the direction is taken from the `bank` or `card` column of the table by `self.sign`. The amount keeps `self.base` as its confidence: reconciliation is what proves or doubts it.
- Produces builders: `sectioned_unsigned_checking()` (two pages; a summary; `DEPOSITS AND ADDITIONS` over `Date | Description | Amount` with 3 positive figures and a `Total Deposits and Additions` line; `ELECTRONIC WITHDRAWALS` with 5 positive figures of which 2 are on page 2 under the repeated header and the title `ELECTRONIC WITHDRAWALS (continued)`; `FEES` with 1 figure; each with its total line; opening + deposits - withdrawals - fees = closing), `sectioned_repeat_without_title()` (the same with page 2 printing only the header again), `card_unsigned_payments()` (a card statement whose `Payments and Credits` table prints a payment without a sign and whose `Purchases` table prints one refund with a minus).

- [ ] **Step 1: Failing tests:** `test_section_title_matching[...]` (pure, on `_section`: each key group, the `continued` suffix, `Deposits and withdrawals` without direction, `Interest summary` and a 9-word line as no title, `Service fees` typed `fee`); `test_unsigned_amounts_take_the_direction_of_their_section` (3 positive and 6 negative rows equal to `EXPECTED_ROWS`, the fee row has `type == "fee"`, status `ok`); `test_direction_survives_a_repeated_page_header[with-title]` and `[header-only]` (the 2 page-2 withdrawals are negative); `test_a_new_table_without_a_title_has_no_direction` (hand-built: an `out` section, then on the same page a second header with no title: the next unsigned row is positive as printed); `test_a_printed_sign_beats_the_section` (a `-` figure under an "in" title stays negative; a `CR` under an "out" title stays positive); `test_card_payment_without_a_sign_is_a_credit` (payment positive in the ledger, purchases negative, refund positive, status `ok`); `test_signed_sectioned_statement_is_unchanged` (`synth_unseen_layouts.sectioned_checking` gives the same 8 amounts as before).
- [ ] **Step 2:** Run; expected: every figure positive on the bank fixtures; the card payment negative.
- [ ] **Step 3: Implement** as the interface says.
- [ ] **Step 4:** Run the new tests and the statement suite (`test_multi_page_statement_with_wrapped_lines_pending_items_fees_and_interest` and `test_card_statement_signs_sections_and_balance_owed` guard the sub-heading path). Expected: pass. If a multi-page fixture now keeps a `fee` or `int` type across a page break where it lost it before, that is the intended change: update the assertion and say so in the commit message.
- [ ] **Step 5:** Commit `fix(ingestion): section titles give unsigned amounts their direction and survive a repeated header`.

### Task 13: Dates printed once per day, and no row dropped without a count (lane M)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`_Row`, `_collect` 1052-1063, `_not_a_row` 1111-1160, `_parse_sections` 1441-1445, `read_statement` 1576-1578), `backend/tests/fixtures/synth_bank_layouts.py`
- Test: `backend/tests/unit/test_bank_layouts.py`

**Interfaces:**
- A **candidate** is an undated line inside a table that has description text and exactly one figure that reads in a money column (or one of debit and credit), is no total (`skip_prefixes`), no balance label, no section title, and starts at most `2.5 *` its height below the row or candidate above it on the same page. It is built as a `_Row` with the date text of the row above and `carried=True` (new field), and held back.
  - Candidates become rows, in printed order, when a dated row follows in the same table (a repeated page header does not end the table).
  - Candidates left when the table ends become rows only when the statement already has at least 2 carried rows (the layout is proven); otherwise they are left alone exactly as today (they are summary lines under the table; `_scan_summary` still sees them).
  - An undated line with a figure in a money column that is followed by a dated row and is neither a candidate, a wrapped line, a total, a balance label nor a section title is counted (`skipped_lines`, its box).
- A carried row's `posted_date` scores `min(date confidence, conf.DERIVED)`; the statement gets the warning `dates_carried` once.
- `_parse_sections` counts the rows it reads in the lines before the first account (today they are thrown away uncounted): `skipped_lines += len(rows)`, their boxes, warning `rows_outside_account`, as `_parse_accounts` does.
- Lines of a section with no header (finding A4, counted only): outside any table, after a line that matches a `SECTION_TITLES` key and before the next header, block title or page end, a line whose first one to three words are date-shaped and whose last cell reads as an amount is counted (`skipped_lines`, its box); the statement (or the outcome, when no header is found at all) gets `rows_without_header:<n>`. Nothing is imported from such lines.
- Produces builders: `date_once_per_day()` (`Date | Description | Paid out | Paid in | Balance`, 14 rows over 5 days, the date on the first row of each day only, the balance on every row, the last day holding 3 rows; reconciles), `summary_lines_under_table()` (an ordinary dated table followed directly by `Interest earned this period` and `Average ledger balance` lines with figures in the amount column), `titled_sections_without_headers()` (after the summary, `DEPOSITS & CREDITS` followed by 3 lines of date, text and figure with no header; then `WITHDRAWALS` over a `Date | Description | Amount` header with 4 rows).

- [ ] **Step 1: Failing tests:** `test_rows_take_the_date_printed_above_them` (14 rows with the builder's dates, status `ok`, warning `dates_carried`, every carried row's `posted_date` is not in `low_confidence`); `test_lines_under_an_ordinary_table_are_not_rows` (`summary_lines_under_table`: exactly the dated rows, `skipped_lines == 0`, no `dates_carried`); `test_an_unreadable_line_between_rows_is_counted` (hand-built: between two dated rows, an undated line with two figures in the amount column: `skipped_lines == 1` with its region); `test_rows_before_the_first_share_are_counted` (hand-built two-share document with one dated row above the first title: `skipped_lines == 1`, warning `rows_outside_account`); `test_lines_of_a_headerless_section_are_counted_not_read` (4 rows, all negative, `skipped_lines == 3`, warning `rows_without_header:3`, 3 skipped regions).
- [ ] **Step 2:** Run; expected: 5 rows on the first; `skipped_lines == 0` on the third, fourth and fifth.
- [ ] **Step 3: Implement** as the interface says.
- [ ] **Step 4:** Run the new tests and the statement suite. Expected: pass; no existing fixture gains a skipped line (if one does, the line is a summary line the candidate rule wrongly counted: fix the rule, not the test).
- [ ] **Step 5:** Commit `fix(ingestion): rows under a date printed once per day; count every statement line that is found and not read`.

### Task 14: Credit-union share titles (lane M)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`_SECTION_ID` 562-565, `section_name` 574-590, `_section_title` 1362-1375, `balance_sections` 1415-1417)
- Test: `backend/tests/unit/test_unseen_layouts.py` (the existing `section_name` parametrised test), `backend/tests/unit/test_bank_layouts.py`

**Interfaces:**
- `section_name` also reads the number first: `ID 0001 - SECONDARY SHARE` is `("SECONDARY SHARE", "0001")`; `Share 02: HOLIDAY CLUB` is `("HOLIDAY CLUB", None)`. Pattern: one of `id`, `share`, `suffix`, `sub`, `account`, `acct`, `no`, `#`, then 1 to 12 digits, then `-`, an en dash or `:`, then the name.
- `_section_title` judges a line by its **first cell**: it has a letter, is no amount and no date, the line is no header, and its normalised text does not start with a `skip_prefixes` word or an opening, closing, period or statement-date label. Figures in later cells no longer disqualify the line. The title is the first cell's text, not the whole line.

- [ ] **Step 1: Failing tests:** `section_name` cases added to the existing parametrisation: `("ID 0001 - SECONDARY SHARE", ("SECONDARY SHARE", "0001"))`, `("ID 0040 – SHARE DRAFT", ("SHARE DRAFT", "0040"))`, `("Share 02: HOLIDAY CLUB", ("HOLIDAY CLUB", None))`; `test_a_title_with_a_figure_on_its_line_names_the_share` (hand-built two-share document whose titles are `PRIMARY SHARE ID 0000 | Dividends Paid YTD: | 4.12` and `ID 0040 - SHARE DRAFT | Dividends Paid YTD: | 0.00`: names `PRIMARY SHARE` and `SHARE DRAFT`, last four `0000` and `0040`); `test_a_total_line_is_never_a_share_title` (a `Total dividends 4.12` line between the shares: the second share is still named by its own title).
- [ ] **Step 2:** Run; expected: name None or the line above the title.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the two files and `tests/unit/test_statement_accounts.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): name credit-union shares by titles that carry a figure or start with the id`.

### Task 15: Section totals are checked; newest-first statements reconcile (final)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`_Summary`, `_not_a_row` 1120-1121, `read_statement` 1554-1631), `backend/tests/fixtures/synth_bank_layouts.py`
- Test: `backend/tests/unit/test_bank_layouts.py`

**Interfaces:**
- Consumes `newest_first` and `section_mismatches` (Task 3) and `_Row.section` (Task 12).
- A **total line** is an undated line in a table whose normalised text starts with `total`, holds exactly one amount, and names no other span of time: none of `year to date`, `ytd`, `this year`, `last year`, `previous year`, and no four-digit year. The first total line after a section's rows is recorded in `_Summary.section_totals: dict[int, Amount]`; later ones for the same section and totals of a pending section are ignored. The line is still not a row.
- In `read_statement`, after the rows are signed: sections whose rows do not add up to their total (`section_mismatches`) add the warning `section_total_mismatch` and make an `ok` statement `mismatch` (the way `summary_totals_mismatch` does). When the statement is a `mismatch` with both balances known, no running balance printed and at least one such section, `doubtful` is the rows of those sections instead of every row; the one row whose amount is the whole difference (today's rule in `doubtful_rows`) still wins when there is one. A section total never raises a status and never vouches for a row.
- Newest first: when `newest_first` is true for the rows' dates, rows and dates are reversed before transactions, ledger rows and derived balances are built, and the statement gets the warning `rows_newest_first`. Transactions are emitted oldest first.
- Produces builders: `section_total_off()` (`sectioned_unsigned_checking` with one withdrawal row removed from the page while every printed total and balance stays), `newest_first_checking()` (20 rows printed from the last day back to the first, each with its running balance, opening and closing in the summary), `year_to_date_totals()` (a card statement whose fee section is followed by `Total fees charged in 2026` and `Total interest charged in 2026` lines that differ from the period's figures; reconciles).

- [ ] **Step 1: Failing tests:** `test_section_totals_that_agree_change_nothing` (`sectioned_unsigned_checking`: status `ok`, no warning); `test_a_section_that_does_not_add_up_is_named` (status `mismatch`, warning `section_total_mismatch`, `reconciliation.doubtful` is exactly the indices of the withdrawal rows, and the deposit rows have no `reconciliation` in `low_confidence`); `test_year_to_date_totals_are_not_section_checks` (status `ok`); `test_newest_first_statement_reconciles` (status `ok`, `running_mismatches == []`, warning `rows_newest_first`, `transactions[0].posted` is the earliest day, opening and closing as printed); `test_oldest_first_statement_is_not_reversed` (`synth_pdf_statements.generic_pdf`: no warning).
- [ ] **Step 2:** Run; expected: `ok` where a mismatch is wanted; a `mismatch` with about 19 running mismatches on the newest-first fixture.
- [ ] **Step 3: Implement** as the interface says.
- [ ] **Step 4:** Run the new tests and the statement suite. Expected: pass with no existing test edited.
- [ ] **Step 5:** Commit `fix(ingestion): check section totals and reconcile statements printed newest first`.

### Task 16: The period, and a year for rows that print none (final)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`period_labels` 632-640, `statement_date_labels` 654-661, `PeriodRead` 411-430, `_period` 1219-1227, `read_statement` 1544-1566 and 1576-1578), `backend/app/ingestion/pipeline.py` (`_parse_document`, beside the `lines_skipped` item), `frontend/src/features/review/reviewCopy.ts`, `backend/tests/fixtures/synth_bank_layouts.py`
- Test: `backend/tests/unit/test_bank_layouts.py`, `backend/tests/unit/test_statement_period.py`, `backend/tests/integration/test_pdf_statement_pipeline.py`, `frontend/src/features/review/reviewCopy.test.ts`

**Interfaces:**
- Consumes `DocumentView.hints` and `DocumentView.ceiling` (Task 5).
- Labels added. `period_labels`: `statement for`, `billing cycle`, `period covered`, `statement covers`, `activity from`, `activity for`. `statement_date_labels`: `statement issued`, `statement ending`, `statement end date`. Not added: `created on`, `generated` (a creation date is not a statement date; it only bounds the assumed year, through `ceiling`).
- `PeriodRead.source` gains `"classifier"` and `"range"`. The period is looked for in this order, and each later step runs only when the earlier ones found nothing:
  1. a labelled period, then the header band (today's `read_period`);
  2. the classifier's period, `view.hints.period`: `PeriodRead(start, end, "classifier", cap=view.hints.period_confidence)`, warning `period_from_classifier`;
  3. a statement date (today's anchor);
  4. an unlabelled range printed once on page 1: among the lines of page 1 above the first table header, a cell whose text holds a separated range with a year (`classify.dates.find_ranges`) starting within its first 5 characters, spanning at most 100 days; used only when exactly one distinct range qualifies: `PeriodRead(start, end, "range", cap=ONE_PAGE_BAND_CONFIDENCE, less AMBIGUOUS_PENALTY when its day/month order is a guess)`, warning `period_unlabelled`. At 0.75 it dates the rows and is no stated period for the duplicate guard, like a one-page header band;
  5. an **assumed year** (below).
- Produces `assume_years(parts: Sequence[tuple[int, int] | None], ceiling: date) -> list[date | None] | None` (module level, pure). `parts` are the rows' `(month, day)` in printed order (None for a row that is not year-less). Rule: place the distinct pairs on the circle of a year; the largest gap between neighbours ends at the **first** pair and starts after the **last**; when first to last spans more than `ASSUMED_SPAN_DAYS = 184`, return None. The last pair takes the latest year in which it is on or before `ceiling`; a pair later in the calendar than the last takes the year before, every other pair the same year; a pair that is no real day in its year (29 February) is None. Dates printed elsewhere on the pages take no part (the experiment that used the latest printed date split one month's rows over two years).
- `read_statement` uses it only when no period, no statement date and no range was found, no row prints a year, and `view.ceiling` is known: the rows get those dates, `posted_date` scores `conf.GUESSED` (so every row is written pending), the statement's period is the rows' span at the existing inferred confidence, and the warning `year_assumed:<YYYY>` (the year of the latest row) is added. Without a ceiling (a hand-built view), or when rows mix dates with and without a year, or the span is too long, the rows are skipped and counted as today.
- The question: `_parse_document` raises one `ReviewKind.LOW_CONFIDENCE_FIELD` item per document with payload `{"reason": "year_assumed", "field": "posted_date", "year": <int>, "parser": <name>}` when a warning matches `year_assumed:(\d{4})` (the latest year when statements differ). The rows themselves wait under the existing `low_confidence` item (`fields` contains `posted_date`). Answering in 1b: confirm the rows when the year is right, correct their dates when it is not; a one-click "it was another year" needs a remembered answer that a re-run reads and belongs to plan 1d.
- Copy in `reviewCopy.ts`, key `"low_confidence_field:year_assumed"`: title `Which year?`; what: `` `The dates on this statement print no year and PerFin found no statement period, so it placed the rows in ${year}.` `` (the payload's `year`; "the most recent year that fits" when it is missing); steps: `Check the year against the statement.` and `If it is right, confirm the rows. If it is not, correct their dates.`
- Produces builders: `yearless_no_period()` (`MM/DD` rows from 03/02 to 03/27 with running balances, no period and no statement date, and a notice line `Effective June 1, 2031, the monthly fee changes.`), `yearless_over_new_year()` (rows from 12/27 to 01/06, nothing else dated), `unlabelled_period_once()` (three lines of address, then `November 30, 2025 through December 31, 2025` as line 10 of page 1, `MM/DD` rows, two pages), `two_ranges_on_page_one()` (the same plus a second, different range in a promotion line), `statement_for_period()` (`Statement For: 06/01/2026 - 06/30/2026`, `MM/DD` rows).

- [ ] **Step 1: Failing tests:**
  - Pure: `test_assume_years_one_month` (pairs 03/02 to 03/27, ceiling 2026-03-31: all 2026; ceiling 2026-03-10: all 2025); `test_assume_years_over_new_year` (ceiling 2026-01-20: December rows 2025, January rows 2026); `test_assume_years_refuses_a_long_span` (pairs in January, May and September: None); `test_assume_years_leap_day` (02/28, 02/29, 03/01 with ceiling 2026-03-15: the middle one None); `test_assume_years_ignores_printed_order` (descending input gives the same dates).
  - Documents, `parse_extraction(extract(...), kind="statement", not_after=date(2026, 3, 31))`: `test_yearless_rows_without_a_period_keep_their_rows` (all rows, every date in March 2026, none in 2031 or split across years, each row has `posted_date` in `low_confidence`, warning `year_assumed:2026`, `skipped_lines == 0`, status `ok` by running balance); `test_rows_over_new_year_take_two_years` (`not_after=date(2026, 1, 20)`); `test_unlabelled_range_on_page_one_is_the_period` (period as printed, `confidence["period"] == 0.75`, rows dated in it, warnings contain `period_unlabelled` and no `year_assumed`); `test_two_different_ranges_are_no_period` (no `period_unlabelled`; the rows are kept with a `year_assumed:` warning); `test_statement_for_is_a_period_label` (period as printed at `confidence["period"] == 0.95`); `test_the_classifiers_period_dates_the_rows` (`yearless_no_period` parsed with `period=(date(2026, 3, 1), date(2026, 3, 31), 0.9)`: rows dated in it, `confidence["period"] == 0.9`, warning `period_from_classifier`, no row has `posted_date` low); `test_a_labelled_period_beats_the_classifiers` (a fixture with its own label and a different hint: the printed one is used).
  - `test_statement_period.py`: `test_without_a_period_yearless_rows_are_not_guessed` is replaced by `test_without_a_period_yearless_rows_take_an_assumed_year` (2 rows, both pending on `posted_date`, a `year_assumed:` warning) and `test_without_a_ceiling_yearless_rows_are_not_guessed` (the same view with `created=None` through `dataclasses.replace`: no rows, 2 skipped).
  - Integration (the module's `env` fixture and upload helper): `test_yearless_statement_without_a_period_waits_with_a_year_question`: upload `yearless_no_period()` into a household with a matching account; all rows are in the ledger with `ReviewState.PENDING`; the document is `needs_review`; one open item has `payload["reason"] == "year_assumed"` and `payload["year"]` equal to the year the test computes from `date.today()` (this year when 27 March is not after today, else last year).
  - Frontend: add `["low_confidence_field", "year_assumed"]` to `REASONS` in `reviewCopy.test.ts`, and `it("names the assumed year")` asserting `what` contains `2026` for `{ reason: "year_assumed", year: 2026 }`.
- [ ] **Step 2:** Run; expected: zero rows with skipped counts on the yearless fixtures; the copy test falls back to the generic text.
- [ ] **Step 3: Implement** as the interface says.
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_bank_layouts.py tests/unit/test_statement_period.py tests/unit/test_card_statement.py tests/integration/test_pdf_statement_pipeline.py tests/integration/test_review_api.py` and the statement suite, and `npm test -- reviewCopy` in `frontend/` (Node 22). Expected: pass. Any other PDF-built test that pinned "no rows without a period" is rewritten the same way and named in the commit message.
- [ ] **Step 5:** Commit `fix(ingestion): more ways to find the statement period; year-less rows keep their rows with an assumed year and a question`.

### Task 17: Tables printed two or three across are counted (final)

**Files:**
- Modify: `backend/app/ingestion/parsers/statement_header.py` (`read_header`), `backend/app/ingestion/parsers/table_statement.py` (`_assign`, `_collect`), `backend/tests/fixtures/synth_bank_layouts.py`
- Test: `backend/tests/unit/test_statement_header.py`, `backend/tests/unit/test_bank_layouts.py`

Decision: the left group is read as far as it is read today; the entries of the other groups are counted, not read. Reading every group is a later improvement.

**Interfaces:**
- Produces `across_start(line: ViewLine, headers: Mapping[str, Role] = HEADERS) -> float | None`: for a line with no amount on which a `date` heading and an `amount` heading each appear at least twice in the same left-to-right order, the `x0` of the first heading of the second group; else None. Inside such a line, a cell reading `Number` or `No` next to a date heading is the `check` column.
- `read_header` on such a line: when the first group is a header by itself (a date, a description or check, a money column), it returns that group's columns followed by `Column("across", x, inf)`. When it is not (`Date | Amount` or `Date | Balance` pairs: a table of daily balances), it returns None and the line is `header_like` as today: it ends the table above, nothing under it is read and nothing is counted. `_assign` gives every cell whose `x0` is at or right of `x` to `across`, before any other rule.
- `_collect`: for each line under such a header, every date-shaped cell among the `across` cells is one unread entry (`skipped_lines += 1`; the line's box once); the statement gets `table_across_unread:<n>`.
- Produces builders: `checks_three_across()` (a signed one-column table of 5 rows, then `Checks Paid` over `Number | Date Paid | Amount` printed three times across, 4 lines holding 11 checks, one number marked `*`), `daily_balances_three_across()` (the same first table, then `Daily Balance Summary` over `Date | Amount | Date | Amount | Date | Amount` with 3 lines).

- [ ] **Step 1: Failing tests:** unit: `test_repeated_column_group_is_found` (`across_start` is the second `Number` heading's `x0`); `test_a_plain_header_has_no_repeat`; `test_date_balance_pairs_are_not_an_across_table` (`Date | Balance | Date | Balance`: None and `header_like`); documents: `test_checks_across_the_page_are_counted` (the 5 rows of the first table, plus 4 rows `CHECK <number>` from the left group as negative amounts, `skipped_lines == 7`, warning `table_across_unread:7`, 4 skipped regions); `test_daily_balances_across_the_page_are_neither_read_nor_counted` (5 rows, `skipped_lines == 0`; a guard that passes today).
- [ ] **Step 2:** Run; expected: `skipped_lines == 0` on the first.
- [ ] **Step 3: Implement** as the interface says.
- [ ] **Step 4:** Run the new tests and the statement suite. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): count the entries of tables printed across the page that are not read`.

### Task 18: A whole member statement, and the layouts through the real pipeline (final)

**Files:**
- Modify: `backend/tests/fixtures/synth_bank_layouts.py`
- Test: `backend/tests/unit/test_bank_layouts.py`, `backend/tests/integration/test_pdf_statement_pipeline.py`

No production code is expected. If a test fails, the fix goes into the function the owning task names, in this task's commit.

**Interfaces:**
- Produces `credit_union_member_statement()`: an invented credit union; `Statement For: 04/01/2026 - 04/30/2026`; a member number printed once; three sub-accounts, each with a dated `Beginning Balance` and `Ending Balance` row: `PRIMARY SHARE ID 0000 | Dividends Paid YTD: | 4.12` (2 rows), `ID 0040 - SHARE DRAFT | Dividends Paid YTD: | 0.00` (6 rows, 4 withdrawals), `HOLIDAY CLUB ID 0050` (1 row); the header `Date Transaction Description | Withdrawal | Deposit | Balance` with the first two headings in one cell; `MM/DD` rows.

- [ ] **Step 1: Tests:**
  - Unit: `test_member_statement_is_three_accounts_read_whole`: 3 statements named `PRIMARY SHARE`, `SHARE DRAFT`, `HOLIDAY CLUB` with last four `0000`, `0040`, `0050`; 2, 6 and 1 rows; every status `ok`; period 2026-04-01 to 2026-04-30 on each; warning `accounts_by_balance`; no `year_assumed`; `skipped_lines == 0`.
  - Unit: `test_member_statement_with_a_damaged_heading_reads_its_withdrawals` (hand-built from the share draft's lines with `conf=0.9` on every cell and the heading printed `ithdrawal`: its 4 withdrawals are read as negative amounts).
  - Integration (worker path, `env` fixture): `test_sectioned_unsigned_statement_imports_clean` (`sectioned_unsigned_checking` into a matching checking account: 9 ledger rows, none pending, a closing balance snapshot, document `processed`, no open review item); `test_stacked_header_statement_imports_clean` (`synth_statement_headers.stacked_header_checking`: all rows posted, document `processed`); `test_checks_across_the_page_raise_lines_skipped` (`checks_three_across`: document `needs_review`, one open item with `reason == "lines_skipped"` and `count == 7`).
- [ ] **Step 2:** Run. Expected: pass at once if Tasks 6 to 17 are complete; a failure names the task whose rule is incomplete.
- [ ] **Step 3:** Fix what fails, in the owning function.
- [ ] **Step 4:** Run the two files and the statement suite. Expected: pass.
- [ ] **Step 5:** Commit `test(ingestion): a whole credit-union member statement and the new layouts through the pipeline`.

### Task 19: Versions and documentation (final)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`version` "6" to "7"; module doc sections 1 to 5), `backend/app/ingestion/parsers/institutions.py` (the four `version` values "4" to "5"), `backend/tests/integration/test_pdf_statement_pipeline.py` (the `card_statement_generic/6` assertion at about line 300), `docs/INGESTION.md` (section "5. Parse": headers, sections, period and year, balances, parser choice, what is counted; the review reason `year_assumed` under "Review queue"), `docs/CONTRIBUTING-parsers.md` (a fingerprinted parser needs every phrase; `section_titles` replaces `sections`), `CHANGELOG.md` (`## [Unreleased]`: one Fixed or Changed entry per task, in the file's voice; note that stored documents read differently after "Process again").

- [ ] **Step 1:** Bump the versions; run `uv run pytest -q tests/unit/test_statement_parsers.py tests/integration/test_pdf_statement_pipeline.py tests/unit -k "parser_names or reparse"`. Expected: pass after the one assertion is updated.
- [ ] **Step 2:** Update the documents; `mkdocs build --strict` passes (`requirements-docs.txt`).
- [ ] **Step 3:** Full gates: `make check` equivalents in the container (`uv run ruff check .`, `ruff format --check .`, `mypy`, `bandit`, `pytest -n 3`, OpenAPI drift) and the frontend (`npm run lint`, `typecheck`, `test`, `build`).
- [ ] **Step 4:** Commit `docs(ingestion): the bank and card statement reader of 1b; parser versions bumped`.

### Task 20: The measurement (final)

**Files:** none in the repository. Commands: section 12 ("How to run it again") of the corpus baseline report kept with the corpus results (`reports/corpus-baseline.md` in the session scratchpad); move the earlier `results.jsonl` aside first, as that section says.

- [ ] **Step 1:** Re-run the corpus harness on this branch and write `derived.jsonl` for the run (`summarize.py ... --derived`).
- [ ] **Step 2:** Compare with the baseline (and with the run recorded after 1a) **per file** on: parser chosen, rows read, reconciliation status, review reasons, parse warnings. Rules: no file may lose rows it read correctly before; no file that reconciled may stop reconciling; a file that gains rows and does not reconcile is listed with its warnings. For each of the 14 documents of the root-cause report's spot-check, check ten rows by eye against the extracted lines (signs, dates, payees), and every row of any file whose rows now carry `year_assumed` or `dates_carried`.
- [ ] **Step 3:** For every regression, add a synthetic fixture that reproduces it to the owning task's test file, fix it, and re-run that file's comparison. Nothing from a sample is copied into the repository.
- [ ] **Step 4:** Put the summary into the pull request body: bank and card PDF statements with rows, reconciled, processed without review, before and after; the files that did not improve and why (A4, A5, C6, C7, E3, F and documents that do not add up by themselves are expected there).

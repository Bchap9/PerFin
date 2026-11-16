# Import 1b2: bank and card PDF statements, round two. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The three misreadings 1b introduced are gone; sections without a header row, tables printed across the page and titles printed with their totals are read and checked; two accounts with one masked number are two statements; no table loses its lines without a count.

**Architecture:** Everything stays in the worker-side document parsers, which read the stored, validated extraction. Three small pure modules keep the lanes apart: `statement_header.py` (exists) gains the groups of a table printed across; a new `statement_lines.py` reads single lines (a date-led row, a title with its total, the start of a second account's summary); a new `statement_kind.py` says what a page-1 summary names (a card, a loan bill). Row work is in `table_statement.py`, amount shapes in `text.py`. The Final tasks wire the pure functions into `_collect`, `read_statement`, `parse` and `detect`. No new tables, no API change, no new review reason, nothing in `app/ingestion/sandbox/protocol.py`.

**Tech Stack:** Python 3.12, PyMuPDF, reportlab (fixtures), pytest. Run everything in the Linux dev container (`uv run ...` from `backend/`).

**Spec:** `docs/superpowers/specs/2026-11-12-import-reliability-design.md` (section "1b"). Root causes: `reports/rootcause-banks-cards-pdf-round2.md` kept with the corpus results (ids R1 to I1 below refer to its cause table).

## Global Constraints

- Test first: write the failing test, run it and see it fail for the stated reason, then implement.
- Fixtures are synthetic and invented. Never copy a real sample's rows, names or numbers into the repository. New PDF builders use reportlab the way `tests/fixtures/synth_bank_layouts.py` does, invented institution names and a "synthetic test document" footer. Unless a task is about its absence, every statement builder prints a masked account number, a labelled period with years, an opening and a closing balance, and figures that reconcile.
- Every new `.py` file starts with `# SPDX-License-Identifier: AGPL-3.0-or-later`.
- Gates on the files you touch before each commit, from `backend/`: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` (strict), and the tests named in the task.
- No test may be skipped or marked xfail. A test that pinned behaviour this plan changes on purpose is rewritten in the task that changes it, and the commit message names it.
- The sandbox contract and protocol do not change. Counts go into `skipped_lines` / `skipped_regions`, notes into `ParseInfo.warnings` (codes of at most 64 characters matching `^[A-Za-z0-9_.:-]*$`).
- No phantom rows. Every rule that reads more carries a negative fixture in the same task that must stay unread. A printed sign, `CR`/`DR`/`DB` or a debit/credit column always beats a rule that infers direction.
- A line that is found and not imported is counted. A held statement is better than a wrong import; a counted line is better than a vanished one.
- "The statement suite" is the list of the 1b plan (`tests/unit/test_statement_parsers.py`, `test_unseen_layouts.py`, `test_card_statement.py`, `test_statement_period.py`, `test_statement_accounts.py`, `test_synth_multi_account_fixtures.py`, `test_table_reading.py`, `test_statement_evidence.py`, `test_layout_template.py`, `test_receipt_parser.py`, `tests/integration/test_pdf_statement_pipeline.py`, `test_multi_account_statements.py`) plus 1b's own `tests/unit/test_bank_layouts.py`, `test_statement_header.py`, `test_amount_shapes.py`, `test_parser_choice.py`, `test_reconcile_order.py`. It keeps passing unchanged unless the task says otherwise.
- Code is found by the function name given; line numbers are those of `4ad1d35` and only a hint. Plan 1c works in the same tree on `brokerage*.py`, `loan_statement.py`, `payslip.py`, `tax_forms.py`, `receipt.py`: none of them is edited here. `brokerage.py` calls `read_statement`; its tests are run where a task says so.
- Conventional commits (`fix(ingestion): ...`), one commit per task, ending with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`. Stage only the files of your task (`git add <paths>`), never `git add -A`.

## After 1c

Package 1c landed before this plan runs: it bumped `statement_generic`/`card_statement_generic` to 8 and the institution parsers to 6, added a loan-bill parser (`loan_statement.py`, review reason `loan_bill_several_loans`), a plan-statement parser (`plan_statement.py`, which stands down when a transaction table is found) and a currency rule (`currency.py`). Before Task 11 and Task 15 (cause D2, the loan bill), check what 1c already does with a loan bill; if 1c reads or declines it, Task 15 keeps only the card half and says so in its report. `tests/unit/test_parser_versions.py` pins versions.

## Out of this plan

Reflowed text with one cell per line (the two Amex reprints, Capital One 360, the Citi variation): a word-order reader would serve one anonymiser. Decimal-comma and non-English months (F). Rotated watermark text (E3: a sandbox-child change and a new extraction). The line-printer grid of balances with an `OD` marker (Frost). A year named elsewhere on the page (`2025 Totals Year-to-Date`) as evidence for the assumed year: no count changes and the question is asked already; it goes with 1d's remembered answer. Loan sub-accounts of a member statement (U5) and everything else 1c owns. Documents whose printed totals and balances disagree stay held, as they should.

## Review Focus

1. **A summary block that prints dated `Beginning Balance` and `Ending Balance` lines with count lines between them, and a section with a title and no header.** No summary line is ever a row; a headerless section is read only when it has rows shaped date, text, amount, and a section whose printed total disagrees is counted, not read (Task 2 `test_lines_between_two_balance_lines_are_not_rows`; Task 12 `test_a_section_whose_total_disagrees_is_counted_not_read`, `test_dated_sentences_under_a_title_are_not_rows`).
2. **A table whose rows carry `CR`/`DB` in a column of their own, and a section titled `Withdrawals / Debits   5 items totaling 912.40`.** Money out is negative in both; a printed minus under a "money in" title stays a minus (Task 1 `test_marker_in_its_own_column_signs_the_amount`; Task 13 `test_title_with_its_total_gives_direction_and_a_check`, `test_a_printed_sign_beats_a_title_with_a_total`).
3. **Checks listed across the page.** When the transaction table already holds them they are not imported twice; when it does not, every one of them is a row; one check missing from the main table keeps the whole list (Task 13 `test_a_second_listing_of_checks_is_not_read_twice`, `test_checks_only_in_their_table_are_rows`, `test_one_unmatched_check_keeps_the_whole_table`).
4. **Two accounts printed under one masked number, and one account whose summary is printed again on page 2.** The first is two statements that each reconcile; the second is one (Task 14 `test_two_accounts_by_their_summaries`, `test_a_summary_repeated_on_page_two_is_one_account`).
5. **A statement that reads today.** A checking statement is not read as a card because its summary says `New Balance`; a member statement with a loan share is not declined as a loan bill; every existing fixture reads the same rows (Task 15 `test_checking_summary_is_no_card`, `test_member_statement_with_a_loan_share_is_still_read`; the statement suite in every task).

---

## Lanes

All lanes start at once. A lane never edits another lane's files during the parallel phase; a red test that comes from another lane's half-finished file is reported, not fixed. Tasks inside a lane run in order. The Final tasks need two lanes' output and run in order after all lanes are merged.

| Lane | Tasks | Owns |
|---|---|---|
| M | 1, 2, 5, 6, 7 | `app/ingestion/parsers/table_statement.py`, `tests/unit/test_bank_layouts_round2.py`, `tests/fixtures/synth_bank_layouts2.py` |
| H | 3, 4 | `app/ingestion/parsers/statement_header.py`, `tests/unit/test_statement_header_round2.py` |
| L | 8, 9 | `app/ingestion/parsers/statement_lines.py` (create), `tests/unit/test_statement_lines.py` |
| T | 10 | `app/ingestion/parsers/text.py`, `tests/unit/test_amount_shapes.py` |
| K | 11 | `app/ingestion/parsers/statement_kind.py` (create), `tests/unit/test_statement_kind.py` |
| Final (in order) | 12, 13, 14, 15, 16, 17, 18 | all of the above, `tests/unit/test_bank_layouts.py` (two rewritten tests), `tests/fixtures/synth_sections.py` (create), `tests/unit/test_statement_sections.py` (create), `app/ingestion/parsers/institutions.py` (version only), `tests/integration/test_pdf_statement_pipeline.py`, docs |

Lane M's fixtures print one-line headers from today's vocabulary and amounts `read_amount` reads today. Lanes H, L, T and K are tested on hand-built lines and strings only (a local `_line(cells: list[tuple[str, float]]) -> ViewLine` as in `test_statement_header.py`).

**Names used across tasks.** Parse warnings: `sections_without_header:<n>` (sections read; `rows_without_header:<n>` stays for lines counted), `checks_listed_twice`, `accounts_by_summary`; `table_across_unread:<n>` stays for entries of a group that do not read. `_Row` gains `listing: int = 0` (Task 13) and `closing_dated: bool = False` (Task 16). No new counters, no new review reason.

---

### Task 1: A credit or debit marker in a column of its own (lane M; regression R1)

**Files:**
- Create: `backend/tests/fixtures/synth_bank_layouts2.py`, `backend/tests/unit/test_bank_layouts_round2.py`
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`_row`)

**Interfaces:**
- Produces `TableStatementParser._own_column_marker(self, cells: Mapping[Role, list[Cell]]) -> bool | None`: when the `amount` role holds one figure that reads with `credit is None`, and the nearest `other` cell to the right of the amount cell (no cell that reads as an amount between them) has the normalised text `cr`, `dr` or `db`: True for `cr`, False for the others; else None. `_row` applies it with `dataclasses.replace(amount, credit=...)`. With a debit or a credit column on the row nothing is applied.
- Produces builder `marker_column_checking() -> bytes` with `EXPECTED_ROWS["marker_column_checking"]`: header `Date | Description | Reference | Amount | DC | Balance`, 8 rows (5 `DB`, 3 `CR`), a running balance on every row, opening and closing in the summary.

- [ ] **Step 1: Failing tests:** `test_marker_in_its_own_column_signs_the_amount` (rows equal `EXPECTED_ROWS`, status `ok`, `running_mismatches == []`, `skipped_lines == 0`); hand-built guards: `test_a_category_cell_is_no_marker` (an `other` cell reading `Dr. Office`: the amount keeps its printed sign), `test_a_marker_left_of_the_amount_is_not_taken`, `test_a_marker_behind_another_figure_is_not_taken` (`410.00 | 9,590.00 | CR` with no balance heading).
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_bank_layouts_round2.py`. Expected: every amount positive, a `mismatch` with five running mismatches.
- [ ] **Step 3: Implement** as the interface says.
- [ ] **Step 4:** Run the new file and the statement suite. Expected: pass with no existing test edited (`test_category_is_not_joined_to_the_amount` of 1b guards the `other` column).
- [ ] **Step 5:** Commit `fix(ingestion): read a credit or debit marker printed in a column of its own`.

### Task 2: No row from the lines between two balance lines; a table that reads no row counts its lines (lane M; regression R2)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`_Row`, `_collect`: the `_balance_row` branch, `settle`, the three places a table ends), `backend/tests/fixtures/synth_bank_layouts2.py`
- Test: `backend/tests/unit/test_bank_layouts_round2.py`

**Interfaces:**
- `_Row` gains `balance_line: bool = False`, set on the anchor a balance row leaves. A candidate takes it over from its anchor (a chain that starts at a balance line stays one).
- In the `_balance_row` branch: candidates whose chain starts at a balance line are **not** promoted (`settle(dated=False)` for them); candidates anchored on a transaction row are, as today. A dated transaction row promotes both kinds, as today.
- `_collect` counts the dated rows read since the last header (`table_rows`). When a table ends (a header, a header-like line, the end of the lines) with `table_rows == 0`, the lines in `loose` are counted (`skipped`, their boxes). With one row or more `loose` is treated as today.
- Produces builders: `summary_grid_with_counts()` (a block `Account Summary` over the header `Date | Description | Amount`, holding a dated `Beginning Balance` line, the undated lines `6 Credits This Period 1,870.00` and `9 Debits This Period 1,420.50`, a dated `Ending Balance` line; then two titled tables with 6 and 9 dated rows that reconcile) and `brought_forward_dated_once()` (`Date | Description | Paid out | Paid in | Balance`; a dated `Balance brought forward` line, then 9 rows over 4 days with the date printed once per day, a balance on every row).

- [ ] **Step 1: Failing tests:** `test_lines_between_two_balance_lines_are_not_rows` (15 rows, none with a payee containing `This Period`, status `ok`, no `dates_carried`, `skipped_lines == 0`); `test_undated_rows_after_a_brought_forward_line_are_still_carried` (9 rows, status `ok`, warning `dates_carried`); `test_a_table_that_reads_no_row_counts_its_lines` (hand-built: a header, three undated lines each with words and one figure under `Amount`, then the end: no rows, `skipped_lines == 3`, three regions). Guard that passes today: 1b's `test_rows_take_the_date_printed_above_them` and `test_lines_under_an_ordinary_table_are_not_rows`.
- [ ] **Step 2:** Run. Expected: 17 rows on the first; `skipped_lines == 0` on the third.
- [ ] **Step 3: Implement** as the interface says.
- [ ] **Step 4:** Run the new tests and the statement suite. Expected: pass; no existing fixture gains a skipped line.
- [ ] **Step 5:** Commit `fix(ingestion): summary lines between two balance lines are never rows; a table that reads nothing counts its lines`.

### Task 3: A label and value grid is no table header; a stray glyph after a unit (lane H; regression R2, cause H1)

**Files:**
- Create: `backend/tests/unit/test_statement_header_round2.py`
- Modify: `backend/app/ingestion/parsers/statement_header.py` (`read_header`, `_UNIT_BRACKETS`)

**Interfaces:**
- `read_header`: on a plain line that is no table printed across, when the headings name `description` twice (two cells, or two phrases of one cell, whose spans do not overlap), the line is no header (None). It stays `header_like`, so it ends a table above it.
- `heading_role`: after a bracketed unit one more character of `|`, `I`, `l` or `!` printed directly behind the closing bracket is dropped with it (`AMOUNT (EUR)I`). Nothing else changes: `Amount I` and `Balance (EUR) due` are no headings.

- [ ] **Step 1: Failing tests:** `test_description_and_amount_pairs_are_no_header` (`Date | Description | Amount | Description | Amount`: `read_header` is None, `header_like` is True); `test_two_date_headings_are_still_a_header` (`Transaction Date | Posting Date | Description | Amount`, a guard); `test_a_table_across_is_still_read` (`Number | Date Paid | Amount` twice, a guard); `test_rule_glyph_after_a_unit[...]` (`AMOUNT (EUR)I`, `Balance (USD)|` give `amount`, `balance`); `test_a_letter_after_a_heading_is_no_unit` (`Amount I`, `Balance (EUR) due`).
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_statement_header_round2.py`. Expected: a header of three columns on the first; None on the glyph cases.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the new file, `tests/unit/test_statement_header.py` and the statement suite. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): a grid of labels and values is no transaction header; a rule glyph after a unit is dropped`.

### Task 4: The groups of a table printed across the page (lane H; cause A2)

**Files:**
- Modify: `backend/app/ingestion/parsers/statement_header.py` (beside `_across`)
- Test: `backend/tests/unit/test_statement_header_round2.py`

**Interfaces:**
- Produces `across_groups(line: ViewLine, headers: Mapping[str, Role] = HEADERS) -> list[list[Column]] | None`: for a line `_across` accepts, one list of columns per printed group, left to right, each column with its own heading's span; None for any other line. `read_header` is unchanged (first group plus `Column("across", x, inf)`), so nothing reads differently until Task 13.

- [ ] **Step 1: Failing tests:** `test_groups_of_a_table_across` (`Number | Date Paid | Amount` three times: three groups, roles `check, date, amount` each, the second group's `date` span is the second `Date Paid` cell's); `test_groups_of_one_cell_headings` (the second and third group printed as one cell `Amount Date` each: spans by word); `test_a_plain_header_has_no_groups`; `test_date_balance_pairs_have_no_groups` (`Date | Balance | Date | Balance`: None).
- [ ] **Step 2:** Run. Expected: import error.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the new file and `tests/unit/test_statement_header.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(ingestion): the column groups of a table printed across the page`.

### Task 5: Cells that sit in two columns, or in the wrong one (lane M; causes A4, A6)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`_assign`, a new `_split_fused_amount` called after `_split_fused_date` at its three call sites, a new `_first_date` step in `_collect` and `summary_lines`), `backend/tests/fixtures/synth_bank_layouts2.py`
- Test: `backend/tests/unit/test_bank_layouts_round2.py`

**Interfaces:**
- Produces `_split_fused_amount(self, cells: dict[Role, list[Cell]], columns: Sequence[Column]) -> None`: only when the table has exactly one money column and no cell of the line reads as an amount in it. The cell that overlaps both that column and the description column loses its first word (money column left of the description) or its last word (right of it) when that word reads as an amount; the word becomes the money cell and the rest the description.
- Two dates in the date column: when the cells of the `date` role are two date-shaped texts (two cells, or one cell of two date-shaped halves) the first is the date and the second is dropped as a posting date. A date and words stay what they are today.
- `_assign`: a **worded** cell (it has a letter, reads as no amount and is no `CR`/`DR`/`DB`) goes to the non-numeric column it overlaps most, and to a numeric column only when it overlaps no other. Cells that read as amounts are assigned as today.
- Produces builders: `amount_before_description_fused()` (header `Date | Amount Description`, rows `MM/DD | $NNN.NN Words`, 7 rows, reconciles), `two_dates_left_aligned()` (a card: header `Trans. Date Post Date | Description | Amount | Category`, the two dates of each row 22 points apart under the one heading cell, the category printed left of its heading so that it overlaps `Amount` as much; 7 rows of which one credit with a minus; reconciles).

- [ ] **Step 1: Failing tests:** `test_an_amount_fused_with_the_description_is_split` (7 rows equal to `EXPECTED_ROWS`, status `ok`, `skipped_lines == 0`); `test_two_dates_under_one_heading_cell` (7 rows, each dated with its first date, no payee holds a category word, status `ok`); hand-built: `test_a_worded_cell_is_not_put_in_a_money_column`, `test_a_marker_cell_still_reaches_the_amount` (`CR` alone under the amount: as today), `test_a_fused_cell_without_an_amount_word_is_left_alone`, `test_a_date_and_a_word_in_the_date_column_are_no_two_dates`.
- [ ] **Step 2:** Run. Expected: zero rows with 7 skipped on the first; zero rows and zero skipped on the second before Task 2, 7 skipped after it.
- [ ] **Step 3: Implement** as the interface says. Each rule acts only on a line that reads no row today.
- [ ] **Step 4:** Run the new tests and the statement suite. Expected: pass with no existing test edited.
- [ ] **Step 5:** Commit `fix(ingestion): split an amount fused with its description; a second date and a category stay out of the date and amount columns`.

### Task 6: The amount printed on a line of its own under its row (lane M; cause A5)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`_collect`: the dated row without a figure), `backend/tests/fixtures/synth_bank_layouts2.py`
- Test: `backend/tests/unit/test_bank_layouts_round2.py`

**Interfaces:**
- `AMOUNT_BELOW_LINES: Final = 3`. In a table with exactly one money column, a dated line with description text and no figure in any numeric column waits. Of the next lines on its page: a line without a figure whose cells hold words extends its memo; a line whose **whole text** reads as one amount gives the row its amount, and the row is read; anything else (a dated line, a total, a balance label, a title, a header, a fourth line) ends the wait and the row is skipped and counted as today. Lines of a foreign amount (`63.00 CAD`, `1.28 Exchange Rate`: a figure and words) extend the memo.
- Produces builder `amount_under_row_card()`: a card statement (`Minimum Payment Due`), header `Date | Description | Amount`, 6 rows of which 2 print the description on the dated line, one or two lines of foreign amount and rate, and the amount alone on the last line; reconciles.

- [ ] **Step 1: Failing tests:** `test_amount_on_a_line_under_its_row` (6 rows, the two amounts are the builder's, their memo holds the rate line, status `ok`, `skipped_lines == 0`); hand-built: `test_a_total_under_a_row_without_amount_is_not_its_amount` (`Total 55.00` after the dated line: `skipped_lines == 1`, no row), `test_a_figure_four_lines_down_is_not_taken`, `test_two_money_columns_take_no_amount_from_below`, `test_a_dated_line_ends_the_wait`.
- [ ] **Step 2:** Run. Expected: 4 rows and 2 skipped.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the new tests and the statement suite. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): read an amount printed alone under its row`.

### Task 7: A balance label takes no figure from behind the next label (lane M; cause B1)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`_label_amount`)
- Test: `backend/tests/unit/test_bank_layouts_round2.py`

**Interfaces:**
- `_label_amount`: the text searched for the figure ends where the next label starts: the first match, after this label's end, of any of `opening_labels`, `closing_labels`, `credit_total_labels`, `charge_total_labels`. A longer label that contains this one is not "the next label".

- [ ] **Step 1: Failing tests** (hand-built lines through `_scan_summary`): `test_a_label_does_not_take_the_next_labels_figure` (`Opening Balance pending | Closing Balance 410.00`: opening None, closing `410.00`); `test_two_labels_and_two_figures_on_one_line` (`Opening Balance 120.00 Closing Balance 410.00`, a guard); `test_previous_statement_balance_is_one_label` (`Previous Statement Balance 88.00`, a guard).
- [ ] **Step 2:** Run. Expected: opening `410.00` on the first.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the new tests and the statement suite. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): a balance label reads its own figure, never the next label's`.

### Task 8: Lines read by themselves: a date-led row, a title with its total (lane L; causes A1, A3)

**Files:**
- Create: `backend/app/ingestion/parsers/statement_lines.py`, `backend/tests/unit/test_statement_lines.py`

**Interfaces** (pure; imports only `base` and `text`, never `table_statement`):
- `@dataclass(frozen=True) class LedRow: date: str; description: tuple[str, ...]; amount: Amount`.
- `led_row(line: ViewLine, date_shaped: Callable[[str], bool]) -> LedRow | None`: the line's first one to three words are a date; a second date right after it is dropped; the **last** cell, or the last word of a one-cell line, reads as an amount; what stands between holds a letter. None when the word or cell before that amount reads as an amount too (a balance beside it: the caller counts the line), when the line starts with a range of dates, or when it has more than 30 words.
- `titled_total(line: ViewLine) -> tuple[str, Amount] | None`: the first cell has letters and no amount; the other cells hold exactly one amount and otherwise only a count and words of `TOTAL_WORDS = {"item", "items", "totaling", "total", "checks", "credits", "debits", "deposits", "withdrawals"}` (brackets and `s)` ignored). Returns the first cell's text and the figure.
- `items_total(line: ViewLine) -> Amount | None`: `<count> item(s) totaling <amount>` and nothing else on the line.
- `amount_only(line: ViewLine) -> Amount | None`: the whole line is one amount.

- [ ] **Step 1: Failing tests:** `test_led_row_by_cells` (`04/02 | Dn | 107.40`); `test_led_row_in_one_cell` (`04/02 04/03 CARD PAYMENT 7711 3302 -45.10`: date `04/02`, amount `-45.10`); `test_two_trailing_figures_are_no_led_row`; `test_a_range_is_no_led_row` (`04/01 through 04/30 interest 1.20`); `test_figures_without_words_are_no_led_row` (`04/02 | 1,200.00`); `test_titled_total` (`Withdrawals / Debits | 5 items totaling $912.40`); `test_a_label_with_two_figures_is_no_titled_total`; `test_a_sentence_with_a_figure_is_no_titled_total`; `test_items_total[...]`; `test_amount_only[...]`.
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_statement_lines.py`. Expected: import error.
- [ ] **Step 3: Implement.** Patterns anchored, no nested quantifiers.
- [ ] **Step 4:** Run the new file. Expected: pass.
- [ ] **Step 5:** Commit `feat(ingestion): read a date-led row and a title printed with its total from one line`.

### Task 9: Where a second account's summary starts (lane L; cause C6)

**Files:**
- Modify: `backend/app/ingestion/parsers/statement_lines.py`
- Test: `backend/tests/unit/test_statement_lines.py`

**Interfaces:**
- Produces `summary_starts(lines: Sequence[ViewLine], opening: Callable[[ViewLine], Amount | None], closing: Callable[[ViewLine], Amount | None], in_table: Callable[[int], bool]) -> list[int]`: the index at which each account starts, or `[]` for fewer than two. An account starts at a line outside a table (`in_table(index)` false) for which `opening` gives a figure, when it is the first such line, or when a `closing` figure was met since the last start **and** the figure differs from that start's opening figure. The start moves up to the nearest of the three lines above, on its page, that has letters and no digit (the block's title).

- [ ] **Step 1: Failing tests:** `test_two_summaries_are_two_starts` (title lines' indexes); `test_the_same_summary_printed_again_is_no_start` (same opening figure on page 2); `test_an_opening_without_a_closing_before_it_is_no_start`; `test_opening_lines_inside_a_table_are_ignored`; `test_one_summary_gives_nothing`.
- [ ] **Step 2:** Run. Expected: import error.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the file. Expected: pass.
- [ ] **Step 5:** Commit `feat(ingestion): find the start of each account's summary`.

### Task 10: Cents without a zero, and lakh grouping (lane T; causes T1, B1)

**Files:**
- Modify: `backend/app/ingestion/parsers/text.py` (`_AMOUNT`, `read_amount`)
- Test: `backend/tests/unit/test_amount_shapes.py`

**Interfaces:**
- `read_amount(text: str, *, bare_cents: bool = False) -> Amount | None`: with `bare_cents` a figure printed as `.NN` (with the signs, symbol and markers of any amount) reads as `0.NN`. `find_amounts` never passes it.
- The integer part also reads Indian grouping: one or two digits, groups of two, a last group of three (`1,14,964.36`).

- [ ] **Step 1: Failing tests:** `test_cents_without_a_zero` (`read_amount(".15", bare_cents=True)` is `0.15`; `"-.30"`, `"$.45"`, `".81 CR"` likewise; without the flag None; `find_amounts("build .15 of 3")` is `[]`); `test_lakh_grouping` (`"1,14,964.36"` is `114964.36`, `"12,34,567.00"` is `1234567.00`); `test_still_not_amounts` gains `"1,23.00"`, `"1,23,45.00"`, `"."`, `".5"`.
- [ ] **Step 2:** Run. Expected: None for each new shape.
- [ ] **Step 3: Implement.** The pattern stays anchored and free of nested quantifiers.
- [ ] **Step 4:** Run the file and `uv run pytest -q tests/unit/test_statement_parsers.py tests/unit/test_card_statement.py tests/unit/test_receipt_parser.py tests/unit -k "brokerage or loan or payslip"`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): read amounts under one unit printed without a zero, and lakh grouping`.

### Task 11: What a page-1 summary names: a card, a loan bill (lane K; causes D1, D2)

**Files:**
- Create: `backend/app/ingestion/parsers/statement_kind.py`, `backend/tests/unit/test_statement_kind.py`

**Interfaces** (pure, on normalised short lines):
- `card_summary(lines: Sequence[str]) -> bool`: one line starts with `new balance`, one with `payments and credits`, `payments credits`, `payment credits` or `total payments and credits`, and one with `purchases`, `total purchases`, `new charges` or `minimum payment`; and none starts with `beginning balance`, `ending balance`, `opening balance`, `closing balance`, `deposits` or `withdrawals`.
- `loan_bill(lines: Sequence[str]) -> bool`: one line holds `principal balance` or `payoff amount`, one holds `amount due` or `payment due`, and none starts with one of the six deposit phrases above.

- [ ] **Step 1: Failing tests:** `test_card_equation_is_a_card`; `test_new_balance_alone_is_no_card`; `test_a_deposit_summary_is_no_card` (the three card lines plus `Beginning balance 10.00`); `test_loan_bill`; `test_amount_due_alone_is_no_loan_bill` (a card's `minimum payment due`); `test_a_member_statement_is_no_loan_bill` (`principal balance`, `amount due` and `beginning balance`).
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_statement_kind.py`. Expected: import error.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the file. Expected: pass.
- [ ] **Step 5:** Commit `feat(ingestion): tell a card summary and a loan bill from their first-page labels`.

### Task 12: Sections with a title and no header row are read and checked (final; cause A1)

**Files:**
- Create: `backend/tests/fixtures/synth_sections.py`, `backend/tests/unit/test_statement_sections.py`
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`_collect`: the `columns is None` branch; `lines_have_table`; `_amount`; `read_statement`: warnings and scores), `backend/tests/unit/test_bank_layouts.py` (one test rewritten)

**Interfaces:**
- Consumes `led_row`, `items_total` (Task 8) and `read_amount(..., bare_cents=True)` (Task 10; `_amount` passes it: a cell of a money column).
- A **headerless section** opens at a line outside a table that is a known title (`_title`) and stays open over a page break. It closes at a `Total ...` or `items_total` line (its total), another title, a header, a block title, or after `HEADERLESS_GAP = 6` lines in a row that are no `led_row`. A title marked `continued`, or the same title again on a later page, runs it on.
- When it closes with at least `MIN_HEADERLESS_ROWS = 2` led rows, and its printed total (when one is printed) equals the sum of the rows' absolute amounts within `reconcile.TOLERANCE`, the led rows become `_Row`s of a new section (`direction` and `section_type` from the title, as under a header) and the total is recorded in `section_totals`. Otherwise every led line is counted as today (`skipped`, box, `headerless`). An undated line of words right under a led row is its memo.
- `read_statement`: `sections_without_header:<n>` for the sections read. A row of a headerless section whose section printed no total scores `amount` at most `conf.INFERRED` unless the statement's status is `ok`.
- `lines_have_table` is also true when the lines hold a known title followed on its page (or at the head of the next) by three or more `led_row` lines before any header: a document with no header at all then has a statement parser (`found_header` is true for it in `_collect`).
- Produces builders: `headerless_sections_checking()` (two pages; a summary; `DEPOSITS & CREDITS` over 4 lines of date, text, figure and `Total Deposits & Credits`; `WITHDRAWALS` over 7 lines of which 3 on page 2 under `WITHDRAWALS (CONTINUED)`, and its total; reconciles), `headerless_total_off()` (the same with one deposit line removed), `headerless_card()` (a card with no header anywhere: `Payments and Other Credits` at the foot of page 1, its one line and total at the head of page 2, `Purchases and Adjustments` with 5 lines and its total; reconciles), `dated_notices_under_a_title()` (a headed table that reconciles, then `Fees` over three sentences that start with a date and end with a figure of more than 30 words each, and `Deposits` over five lines of a date and a balance with no words).

- [ ] **Step 1: Failing tests:** `test_headerless_sections_are_read_and_checked` (11 rows, 4 positive and 7 negative, status `ok`, `skipped_lines == 0`, warning `sections_without_header:2`); `test_a_section_whose_total_disagrees_is_counted_not_read` (only the withdrawals are rows; `skipped_lines == 3`, warning `rows_without_header:3`, status not `ok`); `test_a_statement_with_no_header_is_read` (`parse.parser` is a statement parser, 6 rows, status `ok`); `test_dated_sentences_under_a_title_are_not_rows` and `test_balances_by_day_under_a_title_are_not_rows` (exactly the headed table's rows, `skipped_lines == 0`); `test_a_printed_sign_beats_the_title` (hand-built: `-12.00` under `Deposits`: negative); `test_one_led_line_is_no_section` (hand-built: counted, not read); `test_rows_of_an_unchecked_section_wait` (hand-built section with no total in a statement that does not reconcile: each row has `amount` in `low_confidence`). In `test_bank_layouts.py`, `test_lines_of_a_headerless_section_are_counted_not_read` becomes `test_lines_of_a_headerless_section_are_read` (7 rows, the three deposits positive, no `rows_without_header`).
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_statement_sections.py`. Expected: counted lines and `no_parser`.
- [ ] **Step 3: Implement** as the interface says.
- [ ] **Step 4:** Run the new file, the statement suite, `tests/unit/test_parser_choice.py`, `tests/unit/test_receipt_parser.py`, `tests/unit/test_receipt_accuracy.py` and `uv run pytest -q tests/unit -k "brokerage or fidelity"`. Expected: pass; a receipt fixture that now scores as a statement is a defect of the rule (three led rows under a known title), not of the test.
- [ ] **Step 5:** Commit `fix(ingestion): read sections printed with a title and no header row, checked against their totals` (names the rewritten test).

### Task 13: Tables across the page are read whole; a second listing is not read twice; titles with totals (final; causes A2, A3, R3)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`_Row`, `_collect`, `_row`, `_title_above`, `_total_line`, `read_statement`), `backend/tests/fixtures/synth_sections.py`, `backend/tests/unit/test_bank_layouts.py` (one test rewritten), `backend/tests/integration/test_pdf_statement_pipeline.py` (one test rewritten)
- Test: `backend/tests/unit/test_statement_sections.py`

**Interfaces:**
- Consumes `across_groups` (Task 4), `titled_total`, `items_total` (Task 8).
- Under a header printed across, each line is read group by group: the cells whose centre lies in a group's span are assigned to that group's columns; a group with a date and a figure is a `_Row` (left to right), a group with cells that reads no row is counted (`across`, as today). `_Row.listing` is the number of the across table (0: none). In a table whose header names only `check`, `date` and `amount`, a row is a check paid whether its number is printed or not (`CHECK` and the number when there is one).
- Second listing, in `read_statement` after the rows are signed and dated: for each listing whose **every** row has a partner among the rows of other tables with the same date and the same absolute amount (each partner used once), the listing's rows are removed, none is counted, and the statement gets `checks_listed_twice`. With one row unmatched the whole listing stays.
- `_title_above`: a line among the two above a header for which `titled_total` gives a first cell that is a section title is that header's title; its figure is the section's total (`section_totals`, kept when a `Total` line follows). `_total_line` also takes an `items_total` line. A `titled_total` line anywhere else is what it is today.
- Produces builders: `checks_listed_twice()` (a table with a `Check No.` column holding 12 rows, 4 of them checks; then `Summary of checks` over `Number | Date | Amount` twice across, listing the same 4; reconciles), `checks_only_across()` (the same main table without its 4 check rows, opening and closing unchanged, so that it reconciles only with the 4 checks read), `titles_with_totals()` (`Deposits / Credits | 3 items totaling $1,870.00`, `Withdrawals / Debits | 5 items totaling $912.40` and `Checks | 4 checks totaling $640.00`, each over its header, unsigned figures, the checks three across with one number blank; `Withdrawals / Debits - continued` on page 2; reconciles).

- [ ] **Step 1: Failing tests:** `test_checks_across_the_page_are_all_read` (in `test_bank_layouts.py`, replacing `test_checks_across_the_page_are_counted`: 16 rows, the 11 checks negative, `skipped_lines == 0`, no `table_across_unread`); `test_a_second_listing_of_checks_is_not_read_twice` (12 rows, status `ok`, warning `checks_listed_twice`, `skipped_lines == 0`); `test_checks_only_in_their_table_are_rows` (12 rows, status `ok`, no warning); `test_one_unmatched_check_keeps_the_whole_table` (hand-built: 4 listed, 3 matched: 4 extra rows, status `mismatch`); `test_title_with_its_total_gives_direction_and_a_check` (3 positive, 9 negative, status `ok`; with one withdrawal removed from the page: `section_total_mismatch`); `test_a_printed_sign_beats_a_title_with_a_total`; `test_a_check_without_its_number_is_a_check`; `test_a_summary_line_with_a_figure_is_no_title` (`Deposits and additions 900.00` in a summary box three lines over a header: no direction from it); `test_items_totaling_is_the_sections_total`; guard: 1b's `test_daily_balances_across_the_page_are_neither_read_nor_counted`.
- [ ] **Step 2:** Run. Expected: `skipped_lines == 7` on the first; 14 rows and a `mismatch` on the second.
- [ ] **Step 3: Implement** as the interface says.
- [ ] **Step 4:** Run the new tests, the statement suite and `tests/integration/test_pdf_statement_pipeline.py` (`test_checks_across_the_page_raise_lines_skipped` becomes `test_checks_across_the_page_import_clean`: 16 ledger rows, document `processed`). Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): read every group of a table printed across; a second listing of checks is not imported twice; a title printed with its total names its section` (names the two rewritten tests).

### Task 14: Two accounts by their summaries (final; cause C6)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`parse`, a new `summary_sections` beside `balance_sections`), `backend/tests/fixtures/synth_sections.py`
- Test: `backend/tests/unit/test_statement_sections.py`

**Interfaces:**
- Consumes `summary_starts` (Task 9) with `opening` / `closing` built on `_label_amount` and `in_table` from a pass that marks the lines between a header and the table's end.
- Produces `TableStatementParser.summary_sections(self, lines: Sequence[ViewLine]) -> tuple[list[ViewLine], list[BalanceSection]] | None`: the preamble and one `BalanceSection` per start (title: the start line's text when it is a title line), when there are two starts or more and at least two of the sections hold a table. `parse` tries it after `_accounts` and `balance_sections`, and reads the sections through `_parse_sections`; the outcome carries `accounts_by_summary`.
- Produces builders: `two_accounts_one_number()` (page 1: an overview table of both accounts; `CHECKING SUMMARY` with opening, totals and closing; two titled tables of unsigned figures; page 2: `SAVINGS SUMMARY` with its own opening and closing; a table `Date | Description | Amount | Balance` whose first and last lines are undated `Beginning Balance` and `Ending Balance`; both accounts print `Account Number: 0000`; each reconciles by itself), `summary_printed_twice()` (one account, its summary printed again at the head of page 2 with the same figures).

- [ ] **Step 1: Failing tests:** `test_two_accounts_by_their_summaries` (2 statements, 9 and 3 rows, both status `ok`, each with its own opening and closing, warning `accounts_by_summary`, no `rows_outside_account`); `test_a_summary_repeated_on_page_two_is_one_account` (1 statement, status `ok`, no warning); guards that pass today: `test_shares_with_opening_rows_in_the_amount_column_are_two_accounts` and `test_member_statement_is_three_accounts_read_whole` of 1b (`accounts_by_balance`, unchanged).
- [ ] **Step 2:** Run. Expected: one statement, `mismatch`, the closing balance of the second account.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the new tests, the statement suite and `tests/integration/test_multi_account_statements.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): two accounts printed under one number are split at their summaries`.

### Task 15: A card named by its summary; a loan bill is not a bank statement (final; causes D1, D2)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`TableStatementParser.detect`, `CardStatementParser.detect`), `backend/tests/fixtures/synth_sections.py`
- Test: `backend/tests/unit/test_statement_sections.py`, `backend/tests/unit/test_card_statement.py`

**Interfaces:**
- Consumes `card_summary`, `loan_bill` (Task 11), fed with the normalised page-1 summary lines of at most `CARD_CUE_WORDS` words.
- `CardStatementParser.detect`: `card_summary(...)` is a strong cue beside `CARD_VOCABULARY`; the deposit-account veto is unchanged and comes first.
- `TableStatementParser.detect`, for a parser without fingerprints (the generic bank and card parsers): `0.0` when `loan_bill(...)`. Before implementing, check plan 1c's loan parser on the builder below: when it scores above 0.45 there, the guard is still kept (the receipt veto and `_fallback` would otherwise hand the bill back to the bank reader when the loan parser reads nothing).
- Produces builders: `card_named_by_its_summary()` (an invented issuer; no word of `CARD_VOCABULARY`; a summary `Previous Balance`, `Payments and Credits`, `Purchases`, `New Balance`; header `Date | Description | Amount`; 5 unsigned purchases and a payment with a minus; reconciles as a card), `loan_bill_with_activity()` (an invented lender's bill: `Total Amount Due`, `Payment Due Date`, `Current Principal Balance`, a table `Date | Activity | Amount` with 2 lines, a payment coupon; no opening or closing balance).

- [ ] **Step 1: Failing tests:** `test_card_without_a_card_word_is_read_as_a_card` (parser `card_statement_generic`, purchases negative in the ledger, the payment positive, account type `creditcard`, status `ok`); `test_checking_summary_is_no_card` (1b's `sectioned_unsigned_checking` and `checking_with_card_note`: still `statement_generic`); `test_loan_bill_is_declined_by_the_statement_parsers` (the recorded scores of `statement_generic` and `card_statement_generic` are `0.0`; `parse.statements == []`); `test_member_statement_with_a_loan_share_is_still_read` (1b's `credit_union_member_statement` redrawn in the test with a fourth sub-account titled `AUTO LOAN ID 0060` that prints `Principal Balance` and `Amount Due` lines: still three share statements, each `ok`); in `test_card_statement.py`: `test_the_card_equation_is_a_cue` (hand-built view).
- [ ] **Step 2:** Run. Expected: `statement_generic` on the first; a statement with 2 rows on the third.
- [ ] **Step 3: Implement.** Confirm first with `grep -n -i` over `tests/fixtures` that no checking fixture prints the three card-equation lines on page 1; if one does, stop and report.
- [ ] **Step 4:** Run the new tests, the statement suite and `tests/unit/test_parser_choice.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): a card statement is known by its summary; a loan bill is not read as a bank statement`.

### Task 16: Undated interest and fee lines of a card's section (final; cause I1)

**Files:**
- Modify: `backend/app/ingestion/parsers/table_statement.py` (`_Row`, `_collect`, `read_statement`), `backend/tests/fixtures/synth_sections.py`
- Test: `backend/tests/unit/test_statement_sections.py`

**Interfaces:**
- Only for a parser with `sign == "card"`, in a section whose title has kind `fee` or `int` (under a header or headerless): an undated line with words and exactly one figure, not zero, in the amount column (headerless: the line's last cell), that is no total and no balance label, is held. When the section's total line follows and equals the sum of the section's dated rows and held lines, the held lines become rows with `closing_dated=True`; otherwise they stay the summary lines they are today.
- `read_statement` dates a `closing_dated` row on the period's last day and scores its `posted_date` `min(date confidence, conf.DERIVED)`. Without a period end (an assumed year, a statement date only) such rows are skipped and counted.
- Produces builder `card_interest_lines()`: a card statement whose `Interest Charged` section prints `Interest Charge on Purchases 6.20`, `Interest Charge on Cash Advances 0.00` and `TOTAL INTEREST FOR THIS PERIOD 6.20`, and whose `Fees` section prints one dated fee and its total; then `Total fees charged in 2026` and `Total interest charged in 2026` with other figures; reconciles only with the interest line.

- [ ] **Step 1: Failing tests:** `test_undated_interest_lines_are_rows_on_the_closing_day` (one extra row of type `int`, dated the period's end, negative in the ledger, status `ok`); `test_a_zero_interest_line_is_no_row`; `test_interest_lines_that_miss_their_total_stay_out` (hand-built: lines sum 6.20, total 9.00: no row, `skipped_lines == 0`); `test_year_to_date_lines_are_not_rows`; `test_a_bank_statement_takes_no_undated_interest_line` (`Interest earned this period 0.42` under `Interest`: 1b's `summary_lines_under_table`, unchanged).
- [ ] **Step 2:** Run. Expected: a `mismatch` of 6.20 on the first.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the new tests and the statement suite. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): undated interest and fee lines of a card section are rows when they make its total`.

### Task 17: The layouts through the pipeline; versions and documentation (final)

**Files:**
- Modify: `backend/tests/integration/test_pdf_statement_pipeline.py`, `backend/app/ingestion/parsers/table_statement.py` (`version` one up from its current value, "8" to "9" after 1c; module doc), `backend/app/ingestion/parsers/institutions.py` (the four `version` values one up, "6" to "7" after 1c), `docs/INGESTION.md` (section "5. Parse": sections without a header, tables across and second listings, titles with totals, accounts by summary, the marker column, card cue and loan guard, what is counted), `docs/CONTRIBUTING-parsers.md` (the three pure modules and what belongs in each), `CHANGELOG.md` (`## [Unreleased]`: one Fixed entry per task, in the file's voice; stored documents read differently after "Process again")

- [ ] **Step 1: Integration tests** (worker path, the module's `env` fixture): `test_headerless_statement_imports_clean` (`headerless_sections_checking` into a matching checking account: 11 ledger rows, none pending, document `processed`); `test_two_accounts_by_summary_import_into_two_accounts` (`two_accounts_one_number` with two matching accounts: 9 and 3 rows, two balance snapshots); `test_marker_column_statement_imports_clean`; `test_loan_bill_writes_no_bank_rows` (`loan_bill_with_activity`: no transaction in the ledger). Run; a failure names the task whose rule is incomplete and is fixed in the owning function, in this commit.
- [ ] **Step 2:** Bump the versions; update the `card_statement_generic/<n>` and institution version assertions (also `tests/unit/test_parser_versions.py`) of the pipeline test; run `uv run pytest -q tests/unit/test_statement_parsers.py tests/integration/test_pdf_statement_pipeline.py tests/unit -k "parser_names or reparse"`. Expected: pass.
- [ ] **Step 3:** Update the documents; `mkdocs build --strict` passes.
- [ ] **Step 4:** Full gates in the container: `uv run ruff check .`, `ruff format --check .`, `mypy`, `bandit`, `pytest -n 3`, OpenAPI drift (none expected).
- [ ] **Step 5:** Commit `docs(ingestion): the bank and card statement reader, round two; parser versions bumped`.

### Task 18: The measurement (final)

**Files:** none in the repository. Commands: section 12 of the corpus baseline report kept with the corpus results; move the earlier `results.jsonl` aside first.

- [ ] **Step 1:** Re-run the corpus harness on this branch and write `derived.jsonl` for the run.
- [ ] **Step 2:** Compare with `derived.1b.jsonl` **per file** on parser chosen, rows, reconciliation status, review reasons and parse warnings. Rules: no file that reconciled may stop reconciling; no file may lose rows it read correctly; every file that gains rows from a headerless section, an across table or a split by summary is listed with ten rows checked by eye against the extracted lines (signs, dates, payees). Check by name: far1h G and T, the generalized sample and the three Wells Fargo files (the regressions), the five explainer documents of section 3.3 of the 1b report (they must not gain rows from Task 12), the receipts and the payslips (no statement parser may now score on them).
- [ ] **Step 3:** For every regression, add a synthetic fixture that reproduces it to the owning task's test file, fix it, and re-run that file's comparison. Nothing from a sample is copied into the repository.
- [ ] **Step 4:** Put the summary into the pull request body: bank and card PDF statements with rows, reconciled, processed without review, before and after, against the expectation of the root-cause report (rows 59 to about 67, reconciled 23 to 32 or more); the files that did not improve and why.

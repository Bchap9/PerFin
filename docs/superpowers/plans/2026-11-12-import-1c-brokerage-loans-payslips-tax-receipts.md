# Import 1c: brokerage, loans, payslips, tax forms, receipts. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Brokerage exports and OFX positions, loan and mortgage statements, payslips, tax forms and receipts are read the way their issuers print them, and none of them stores a figure that means something else (a year-to-date total as a payment, a bill as a receipt, a dividend as nothing).

**Architecture:** Fixes stay inside the worker-side document parsers (`app/ingestion/parsers/`), the sandboxed OFX investment extractor (`app/ingestion/extract/ofx_invest.py`) and the two importers that commit what they read (`app/domain/investments/statement_import.py`, `app/domain/loans/statement_import.py`). Three small new parser modules (`labelled_cells.py`, `loan_periodic.py`, `currency.py`) and, in the last and optional task, `plan_statement.py`. **No migration, no new column, no change to the sandbox protocol models, no API change.** Every behaviour is pinned by a synthetic fixture first.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, PyMuPDF, tesserocr, reportlab and Pillow (fixtures), pytest; TypeScript and vitest for the review copy. Run everything in the Linux dev container (`uv run ...` from `backend/`, `npm ...` from `frontend/` on Node 22).

**Spec:** `docs/superpowers/specs/2026-11-12-import-reliability-design.md` (section "1c"). Evidence: the root-cause tables kept with the corpus results (brokerage C2 to C8, C14, R1; structured E3, E4; loans L1, L2; receipts R1 to R4; payslips P1, P2; tax T1, T2; the currency side finding).

**Builds on:** plan 1a merged into `main` first. 1a already does and this plan does not repeat: Task 9 (a table with symbol, quantity and price is never cash: `investment_columns`), Task 12 (`best_date_format` tie rules), Task 13 (duplicate price per security and day; `INV401K` types the account `retirement`: the spec lists it under 1c, it is done in 1a), Task 14 (`no_rows`), Task 8 (OCR crash retry), Task 7 (damaged PDF). Plan 1b runs beside this one; the files both touch are named under "Shared with 1b" below.

## Global Constraints

- Test first: write the failing test, run it and see it fail for the stated reason, then implement.
- Fixtures are synthetic and invented. Never copy a real sample's rows, names or numbers into the repository. Every figure in this plan is invented; where the evidence names a broker's own words (action phrases, column headings, form labels) those words are vocabulary, not sample data.
- Every new `.py` file starts with `# SPDX-License-Identifier: AGPL-3.0-or-later`.
- Gates on the files you touch before each commit, from `backend/`: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and the tests named in the task.
- No test may be skipped or marked xfail (docs/DEVELOPMENT.md). An existing test is edited only where a task says so; otherwise it passes unchanged.
- The sandbox contract does not change: parsers read validated extraction output only; nothing new in the seccomp filter or Landlock rules. `ofx_invest.py` keeps importing only `app.ingestion.extract.common` and the protocol (it runs in the child).
- No Alembic revision in this package (`0013` belongs to 1d). No field is added to `app/ingestion/sandbox/protocol.py`: an option's contract size is folded into the stored unit price, a loan's next-payment split is used for a check and not stored, a plan statement uses fields that exist. If an implementer finds a field is unavoidable, stop and report; do not add it.
- Identity rules stay: only the last four digits of an account or loan number leave a parser; tax-form identity boxes are never read; nothing new is logged.
- A held document is better than a wrong record. When a rule cannot prove a reading it leaves the field unread, or holds the document with a named reason; it never guesses.
- Review reasons are stable strings used by the frontend copy (`frontend/src/features/review/reviewCopy.ts`, keys `"<kind>:<reason>"`); a new one needs a copy entry there, a line in the `REASONS` list of `reviewCopy.test.ts` and a vitest assertion, in the owning task. Entries are appended at the end of their kind's block.
- Conventional commits (`fix(ingestion): ...`), one commit per task, ending with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`. Stage only the files of your task (`git add <paths>`), never `git add -A`.

### Shared with 1b (exact places)

| File | 1c touches | 1c leaves to 1b |
|---|---|---|
| `app/ingestion/parsers/receipt.py` | `MAX_RECEIPT_PAGES` (new constant beside `MAX_RECEIPT_LINES`, line 83); a new tuple `_BILL_WORDS` placed after `_RECEIPT_WORDS` (after line 220); in `_scores` (780-802) a new keyword `pages`, one early return inserted directly after the `_STATEMENT_WORDS` return (after line 785) and one cap before the final `return` (line 802); the body of `ReceiptParser.detect` (936-937) gains the page guard and passes `pages`. Also `_typed` (350-372), `_find_date` (581-609), `read_receipt` (809-928), which 1b has no finding on. | The entries of `_STATEMENT_WORDS` (193-209) and its check (784-785); any test for a transaction-table header on the page; any use of the classifier's kind. 1c adds no statement word, reads no table header and does not touch `registry.py`'s choice (192-205). If 1b also limits pages in `detect`, keep one constant, `MAX_RECEIPT_PAGES`, with the smaller value. |
| `app/ingestion/parsers/table_statement.py` | Imports only (`STUB_MARKERS`, `label_end`, `read_period`, `PeriodCandidates`); one edit in Task 18: the `_CURRENCY` use at line 897 (pattern at 315). | Everything else. |
| `app/ingestion/parsers/registry.py`, `base.py`, `app/ingestion/pipeline.py` | One line each, only in the optional Task 21. | Parser choice. |
| `app/domain/imports/service.py`, `app/domain/review/layout.py` | Task 19 only: the `no_records` branch (about 1881) and `with_layout_tables` (130-142), after 1a Task 14 has landed there. | |

## Review Focus

1. **A mortgage statement that prints "Paid Last Month" beside "Paid Year to Date"**, uploaded for a loan the household tracks: the payment recorded is last month's split, dated by the payment row of the activity table; the balance is the printed outstanding principal. No test may show a year-to-date figure, the next payment's split or the due date stored as the payment. The two existing layouts of `synth_loans.py` read exactly as before.
2. **A broker activity export with reinvested dividends and bank transfers**: every dividend is income, every reinvestment buys units, every transfer from the bank is a deposit; the file's total line and an "as of" date lose no row. A file that read cleanly before reads the same rows.
3. **An export or OFX file with written (sold to open) options**: everything else imports; no lot, holding or trade of the written option is created; a review item says which lines were left out. A plain sale of shares bought before the export's first row still opens at an unknown cost basis, as today.
4. **A two-page auto-loan bill with "Total amount due"** and a four-page statement with "Total" lines: neither is a receipt. Every receipt of `synth_receipts.py` (tuning and held-out sets) keeps its score and its reading.
5. **A payslip with Current and YTD columns**: gross, deductions and net come from the current column, add up, and the document ends `processed` with no review item; one garbled OCR word elsewhere on the page does not lower a field read from clean cells.
6. **An OFX statement whose market values are cut to the cent**: imported. A position off by more than a cent is still held.

Each has its test in the owning task below (Tasks 10, 1 and 2, 3 and 17, 14 and 19, 8 and 9, 5 and 17).

---

## Lanes

Tasks are grouped by the files they own so lanes can run side by side in one
checkout. A lane never edits another lane's files during the parallel phase.
Inside a lane the tasks run in order.

| Lane | Tasks | Owns |
|---|---|---|
| A | 1, 2, 3, 4, 5 | `app/ingestion/parsers/brokerage.py`, `brokerage_table.py`, `tests/fixtures/synth_activity_csv.py`, `tests/unit/test_brokerage_table_parser.py`, `test_brokerage_parser.py`, `test_activity_csv_fidelity.py` |
| B | 6, 7 | `app/ingestion/extract/ofx_invest.py`, `app/ingestion/parsers/positions_table.py`, `tests/fixtures/synth_brokerage.py`, `synth_positions_csv.py`, `tests/unit/test_ofx_invest_positions.py` (new), `test_positions_table_parser.py`, `tests/integration/test_positions_export_import.py` |
| C | 8, 9, 10, 11 | `app/ingestion/parsers/confidence.py`, `payslip.py`, `loan_statement.py`, `labelled_cells.py` (new), `loan_periodic.py` (new), `app/domain/loans/statement_import.py`, `tests/fixtures/synth_payslips.py` (new), `synth_loans.py`, `tests/unit/test_payslip_parser.py` (new), `test_loan_statement_parser.py`, `test_loan_periodic.py` (new), `tests/integration/test_loan_statement_pipeline.py` |
| D | 12, 13 | `app/ingestion/parsers/tax_forms.py`, `app/domain/tax/forms/us.py`, `tests/fixtures/synth_tax_forms.py`, `tests/unit/test_tax_form_parser.py`, `test_tax_forms.py`, `tests/integration/test_tax_form_pipeline.py`, `frontend/src/features/tax/api.ts` |
| E | 14, 15, 16 | `app/ingestion/parsers/receipt.py`, `tests/fixtures/synth_receipts.py`, `tests/unit/test_receipt_parser.py`, `tests/integration/test_receipt_pipeline.py` |
| Final (in order) | 17, 18, 19, 20, 21, 22 | `app/domain/investments/statement_import.py`, `app/ingestion/parsers/currency.py` (new), `app/domain/imports/service.py`, `app/domain/review/layout.py`, `frontend/src/features/review/reviewCopy.ts` and its test, `tests/integration/test_brokerage_pipeline.py`, `test_activity_table_import.py`, docs; Task 21 adds `plan_statement.py` and its files |

Task 21 (401(k) plan statements) is optional: dropping it removes nothing the other tasks need. Task 22 (the measurement) runs last either way.

---

### Task 1: Broker action words and abbreviations type their rows (C2, E3)

**Files:**
- Modify: `backend/app/ingestion/parsers/brokerage.py` (`ACTIVITY_TYPES` 466-549, `SIGNED_TYPES` 565-580, `activity_type` 605-655)
- Test: `backend/tests/unit/test_brokerage_parser.py` (beside `test_activity_words_and_split_ratios`)

**Interfaces:**
- Produces `expand_action(norm: str) -> str` (public, in `brokerage.py`): a normalised action with broker abbreviations written out and dividend qualifiers dropped. `activity_type` calls it right after `normalise(text)`; nothing else changes in `activity_type`. The unknown-action keys of `vocabulary.py` are untouched (they are built from the printed words).
- `ACTION_ABBREVIATIONS` (whole tokens): `div`, `divs` -> `dividend`; `int` -> `interest`; `mgmt` -> `management`; `qual` -> `qualified`; `cap` -> `capital`; `yr` -> `year`; `pr` -> `prior` only when the next token is `yr` or `year`.
- `DIVIDEND_QUALIFIERS`: `special`, `prior`, `year`, `qualified`, `non`, `nonqualified`, `ordinary`. Leading qualifier tokens are dropped only when the tokens after them start with `dividend` or `cash dividend`.
- New `ACTIVITY_TYPES` phrases: `service fee`, `adr management fee`, `nra withholding`, `tax withholding` -> `fee`; `bank interest` -> `interest`; `payment in lieu` -> `dividend`; `spin off` -> `corporate_action`.
- New `SIGNED_TYPES` phrases, each `("transfer_in", "transfer_out")` (units by the quantity's sign when the row moves a security, else `deposit`/`withdrawal` by the amount's sign, never without a sign): `moneylink`, `wire sent`, `wire received`, `wire funds`, `internal transfer`, `bank transfer`, `funds paid`, `funds received`, `security transfer`.

- [ ] **Step 1: Failing tests** in `test_brokerage_parser.py`:
  - `test_abbreviated_actions_are_expanded`, parametrised: `("Pr Yr Cash Div", "dividend")`, `("Special Non Qual Div", "dividend")`, `("Non-Qualified Div", "dividend")`, `("Qual Div Reinvest", "dividend")`, `("MUNI EXEMPT INT EXAMPLE CITY BONDS", "interest")`, `("Long Term Cap Gain", "dividend")`, `("ADR Mgmt Fee", "fee")`, `("Service Fee", "fee")`, `("NRA Withholding", "fee")`, `("Bank Interest", "interest")`, `("Payment in Lieu", "dividend")`, `("Spin-off", "corporate_action")`: `activity_type(text) == kind`.
  - `test_cash_movements_take_their_direction_from_the_amount`, parametrised over `MoneyLink Transfer`, `MoneyLink Deposit`, `Wire Sent`, `Wire Received`, `Internal Transfer`, `Funds Received`: `cash_sign=1` gives `deposit`, `cash_sign=-1` gives `withdrawal`, `cash_sign=0` gives `None`.
  - `test_security_transfer_moves_units_by_sign`: `activity_type("Security Transfer", moves_security=True, units_sign=-1) == "transfer_out"`, `units_sign=1` gives `transfer_in`.
  - `test_expansion_leaves_known_phrases_alone`: `activity_type(key) == ACTIVITY_TYPES[key]` for every key of `ACTIVITY_TYPES` (among them `non resident tax` -> `fee`, `qualified dividend` -> `dividend`, `long term cap gain` -> `dividend`); `activity_type("REINVESTMENT EXAMPLE FUND (EXMPL) (Cash)") == "reinvest"`; `expand_action("interest earned") == "interest earned"`.
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_brokerage_parser.py -k "abbreviated or cash_movements or security_transfer or expansion"`. Expected: `None` where a kind is asserted; `NameError` for `expand_action`.
- [ ] **Step 3: Implement** the maps and `expand_action` as the interface says. Decision: the row-level meaning of `Reinvest Dividend` is not a phrase (Task 2 decides it from the row); `nra tax adj`, `tax reversal`, `adjustment` and `forex trade component` are not added (a signed adjustment has no safe type; IBKR wording is out of scope).
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_brokerage_parser.py tests/unit/test_brokerage_table_parser.py tests/unit/test_activity_csv_fidelity.py tests/unit/test_fidelity_parser.py tests/unit/test_suggested_vocabulary.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): type brokers' abbreviated and cash-movement action words`.

### Task 2: Reinvested dividends, "as of" dates, total lines and "Fees & Comm" (C3, C4, C6)

**Files:**
- Modify: `backend/app/ingestion/parsers/brokerage_table.py` (`HEADERS` 198-241, `_Reader.read` 494-566, `_date_format` 569-574, `read_activity_table` 606-629, `activity_spec` 697, `suggest_activity_mapping` 732), `backend/tests/fixtures/synth_activity_csv.py`
- Test: `backend/tests/unit/test_brokerage_table_parser.py`

**Interfaces:**
- `HEADERS` gains, all with role `fees`: `fees comm`, `fees and comm`, `fees commissions`, `fees and commissions`, `commission fees`, `commissions fees`, `commissions and fees`.
- Produces `_trade_date_text(cell: str) -> str`: the cell cut at the first ` as of ` (any case); the first date is the trade date, the second is dropped. Used for the `date` and `settle` roles wherever a date cell is read or sampled.
- Produces `_is_total_row(row: Sequence[str], header: Header) -> bool`: the first filled cell, normalised, is `total`, starts with `total ` or ends with ` total`, and every other filled cell parses as an amount. Such a row is a note (`ActivityTable.notes`), never `skipped`, and is left out of the body used for the typed share and the date format.
- Row rule in `_Reader.read`: a row typed `reinvest` with no quantity and an amount above zero is the income leg and becomes `dividend` (its security kept). A `reinvest` row with no quantity and an amount of zero or below is still skipped.
- Produces in `synth_activity_csv.py`: `BROKER_HEADER = ("Date", "Action", "Symbol", "Description", "Quantity", "Price", "Fees & Comm", "Amount")` and `broker_activity_csv(rows: Sequence[Sequence[str]], *, title: bool = True, total: str | None = None) -> bytes`: an optional one-cell title line, the header, the rows with every cell quoted and amounts written `$1,234.56` / `-$1,234.56`, and with `total` a last row `"Transactions Total","","","","","","",<total>`. Tasks 3, 4 and 17 reuse it.

- [ ] **Step 1: Failing tests** (rows invented; fund `SGRX`; the sketches below list action, symbol, quantity, price, fees and amount, and each built row carries all eight cells with an invented date and description):
  - `test_fees_and_comm_column_is_read`: a `Buy` of 10 at `$25.00` with `Fees & Comm` `$4.95` and amount `-$254.95` gives `fees == Decimal("4.95")`.
  - `test_cash_leg_of_a_reinvestment_is_a_dividend`: rows `Reinvest Dividend, SGRX, "", "", "", $18.40` and `Reinvest Shares, SGRX, 0.736, $25.00, "", -$18.40` give two activities: `("dividend", Decimal("18.40"), quantity None)` and `("reinvest", Decimal("-18.40"), quantity Decimal("0.736"))`; `skipped == 0`.
  - `test_reinvest_without_units_and_without_income_is_still_skipped`: `Reinvest Shares` with no quantity and `-$18.40`: `skipped == 1`.
  - `test_as_of_date_keeps_the_first_date`: date cell `10/14/2026 as of 10/10/2026` reads `trade_date == date(2026, 10, 14)`; `"date_order_guessed"` is decided on the first dates only.
  - `test_total_line_is_a_note`: with `total="-$254.95"`, `notes` counts it, `skipped == 0`, `unknown_activity == 0`, no `rows_unreadable` warning.
  - `test_a_row_named_total_with_text_is_not_a_total_line`: action `Total Return Swap Fee`, symbol `SGRX`, amount `-$1.00` is read (or counted unknown), never a note.
- [ ] **Step 2:** Run the six; expect `fees == 0`, `skipped` of 1 to 2, a `None` date.
- [ ] **Step 3: Implement** as the interfaces say.
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_brokerage_table_parser.py tests/unit/test_activity_csv_fidelity.py tests/integration/test_activity_table_import.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): read reinvested dividends, "as of" dates, total lines and the fees column of activity exports`.

### Task 3: Option symbols in activity tables (C5, first half)

**Files:**
- Modify: `backend/app/ingestion/parsers/brokerage.py` (after `is_cusip`, about 452-462), `backend/app/ingestion/parsers/brokerage_table.py` (`_security` 428-442, `_Row` 445-452, `_Reader.read`, the grouping loop 632-662), `backend/tests/fixtures/synth_activity_csv.py`
- Test: `backend/tests/unit/test_brokerage_parser.py`, `backend/tests/unit/test_brokerage_table_parser.py`

**Interfaces:**
- Produces `option_symbol(text: str) -> str | None` in `brokerage.py`: the OCC form `ROOT` + `YYMMDD` + `C|P` + strike x 1000 as 8 digits, for `ROOT MM/DD/YYYY STRIKE C|P` (strike with up to 3 decimals) and for the OCC form printed with or without an inner space; root 1 to 6 letters; a date that does not exist gives `None`. The result always fits `SecurityRef.symbol` (at most 21 characters, letters and digits).
- `brokerage_table._security`: when `option_symbol(symbol)` is not `None`, the security is `SecurityRef(symbol=<OCC>, name=<description, else the printed symbol>, asset_class="option")`.
- Price: an option row's `unit_price` is stored per contract (printed price x 100) when `abs(amount)` is nearer to quantity x price x 100 than to quantity x price; otherwise as printed. (Same convention as `positions_table._position`.)
- Closings: a row on an option security whose action starts with `expired` or `assigned` takes no direction from its own sign. After the rows of an account are read, `_close_options(rows: list[_Row], out: ActivityTable) -> list[_Row]` walks each option security in date order: with net units above zero from the other rows the closing is a `sell`, below zero a `buy`, each moving the net toward zero; a closing with nothing open, or larger than what is open, is dropped and counted in `skipped`. A blank amount on a closing is zero. A row whose action starts with `buy to close` or `buy to cover` closes a short by its own words: when the other rows leave nothing short open (net units zero or above) it is dropped and counted in `skipped`, never read as a purchase (the short was opened before the export's first row).

- [ ] **Step 1: Failing tests:**
  - `test_option_symbol_forms` (in `test_brokerage_parser.py`), parametrised: `"ZZOP 03/20/2026 45.00 C"` and `"ZZOP 260320C00045000"` and `"ZZOP260320C00045000"` all give `"ZZOP260320C00045000"`; `"ZZOP 06/19/2026 7.50 P"` gives `"ZZOP260619P00007500"`; `"ZZOP 02/30/2026 45.00 C"`, `"SGRX"`, `"SYNTHETIC GROWTH FUND"` give `None`.
  - `test_long_option_trades_read` (`broker_activity_csv`): `Buy to Open, "ZZOP 03/20/2026 45.00 C", 2, $1.85, $0.65, -$370.65` and `Sell to Close, same, 2, $2.40, $0.65, $479.35`: two activities `buy` and `sell`, `security.symbol == "ZZOP260320C00045000"`, `security.asset_class == "option"`, quantities 2, `unit_price` 185 and 240, `skipped == 0`.
  - `test_expiry_closes_what_is_open`: `Buy to Open` 1 contract then `Expired, same symbol, 1, "", "", ""` gives `buy` then `sell` with `amount == 0`; printing the expiry's quantity as `-1` gives the same.
  - `test_expiry_with_nothing_open_is_counted`: an `Expired` row alone: `rows == 0`, `skipped == 1`, warning `rows_unreadable`.
  - `test_buy_to_close_with_no_short_open_is_counted`: a `Buy to Close` of 1 contract alone: `rows == 0`, `skipped == 1`.
  - `test_written_option_rows_read_as_sell_then_buy`: `Sell to Open` 1 at `$1.10` (`$109.35`) then `Buy to Close` 1 at `$0.40` (`-$40.65`): `sell` then `buy` on the option security (Task 17 decides what the importer does with them).
- [ ] **Step 2:** Run; expect `skipped` rows (`sell_without_security`) and `NameError`.
- [ ] **Step 3: Implement.** `Buy to Open`, `Sell to Close`, `Sell to Open`, `Buy to Close` already type through the `buy`/`sell` prefixes; no phrase is added.
- [ ] **Step 4:** Run the two unit files and `tests/integration/test_activity_table_import.py`. Expected: pass.
- [ ] **Step 5:** Commit `feat(ingestion): read option symbols and expiries in brokerage activity exports`.

### Task 4: A day/month tie on a known US broker layout is no guess (C7)

**Files:**
- Modify: `backend/app/ingestion/parsers/brokerage_table.py` (`LAYOUT_INSTITUTIONS` 243-246, `_date_format` 569-574 and its three callers)
- Test: `backend/tests/unit/test_brokerage_table_parser.py`

What is left after 1a Task 12: a column whose days and months are all 12 or less, with neither part constant, no second date column and no reading in the future.

**Interfaces:**
- `LAYOUT_INSTITUTIONS` gains two whole-header keys, both `"Charles Schwab"`: `("date", "action", "symbol", "description", "quantity", "price", "fees comm", "amount")` and the same with `price` before `quantity`.
- `_date_format(table: Table, header: Header, body: Sequence[Sequence[str]]) -> tuple[DateFormat, bool]`: when `best_date_format` reports a tie between `MM/DD/YYYY` and `DD/MM/YYYY` (or their two-digit-year forms), the answer is month-first and not a guess when both hold: `layout_institution(table, header.row)` is not `None`, and `_dollar_first(amounts)`.
- Produces `_dollar_first(amounts: Sequence[str]) -> bool`: at least 4 filled amount cells and at least 90% of them start with `$` after an optional `-`, `+` or `(`.

- [ ] **Step 1: Failing tests:**
  - `test_tied_dates_on_a_known_us_layout_read_month_first`: `broker_activity_csv` with six rows dated `04/02/2021`, `04/05/2021`, `09/03/2020`, `08/08/2020`, `08/01/2020`, `05/06/2017` (every part 12 or less, neither part constant): no `date_order_guessed`, first row `date(2021, 4, 2)`, no `low_confidence`, account institution `Charles Schwab`.
  - `test_tied_dates_without_dollar_amounts_stay_a_guess`: the same rows with amounts written `-254.95`: `date_order_guessed`.
  - `test_tied_dates_on_an_unknown_layout_stay_a_guess`: the same rows under `Date, Action, Symbol, Description, Quantity, Price, Amount` (no fees column): `date_order_guessed`.
  - The existing `test_a_guessed_date_order_marks_every_row_low_confidence` passes unchanged (its amounts print no `$`).
- [ ] **Step 2:** Run; expect `date_order_guessed` in the first.
- [ ] **Step 3: Implement.** Decision: two independent signals are required (a whole known header and currency-symbol-first amounts); the order of the rows is not used (it does not separate the readings).
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_brokerage_table_parser.py tests/integration/test_activity_table_import.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): month-first dates on a known US broker export are not a guess`.

### Task 5: One cent of slack on quantity x price (C14, E4)

**Files:**
- Modify: `backend/app/ingestion/parsers/brokerage.py` (`_position_agrees` 320-324, module doc lines 9-11)
- Test: `backend/tests/unit/test_brokerage_parser.py`, `backend/tests/unit/test_positions_table_parser.py` (run only)

**Interfaces:**
- Produces `PRODUCT_SLACK: Final = Decimal("0.01")` in `brokerage.py`. `_position_agrees` allows `PRODUCT_SLACK + abs(quantity) * _price_unit(price) / scale` (today `TOLERANCE` in place of `PRODUCT_SLACK`). `app.ingestion.reconcile.TOLERANCE` (0.005) is unchanged and still bounds every sum of printed amounts (`holdings_total`, `cash`, `total_value`, `index_value`).

- [ ] **Step 1: Failing tests** beside `test_position_tolerance_follows_the_printed_price_precision`:
  - `test_a_value_cut_to_the_cent_agrees`: `_position("41.263", "19.8700000", "819.89")` (the product is 819.89581) reconciles `ok`.
  - `test_more_than_a_cent_is_still_a_mismatch`: `_position("41.263", "19.8700000", "819.87")` is `mismatch` with `position_mismatches == [0]`.
  - The existing tight case (`1000 x 41.2300` against `41234.50`) stays `mismatch`.
- [ ] **Step 2:** Run; expect `mismatch` in the first.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_brokerage_parser.py tests/unit/test_positions_table_parser.py tests/unit/test_statement_evidence.py tests/unit/test_import_as_read.py tests/unit/test_fidelity_parser.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): allow a truncated cent when a holding's value is quantity times price`.

### Task 6: OFX bonds priced per 100 and option contracts (C14, E4)

**Files:**
- Modify: `backend/app/ingestion/extract/ofx_invest.py` (`_Catalogue` 145-179, `_positions` 182-217, `_trade` 241-267), `backend/tests/fixtures/synth_brokerage.py`
- Test: `backend/tests/unit/test_ofx_invest_positions.py` (create)

**Interfaces:**
- Produces `_Catalogue.contract_size(secid: OfxNode | None) -> Decimal`: `SHPERCTRCT` of the security's `OPTINFO`, else 100; a value outside 1 to 10,000 is 100.
- Produces `_nearer(printed: Decimal | None, plain: Decimal, scaled: Decimal) -> bool`: `printed` is strictly nearer to `scaled` than to `plain`; `True` when `printed` is `None` (the OFX definition applies).
- `POSDEBT`: `price_per_100 = _nearer(MKTVAL, units x price, units x price / 100)`. `BUYDEBT`/`SELLDEBT`: the same against `abs(TOTAL)`.
- `POSOPT`: `unit_price = UNITPRICE x contract_size` when `_nearer(MKTVAL, units x price, units x price x size)`. `BUYOPT`/`SELLOPT`: the same against `abs(TOTAL)`. Units stay contracts. No field is added.
- `_trade` passes a default asset class to `catalogue.ref` for `BUYDEBT`/`SELLDEBT` (`bond`) and `BUYOPT`/`SELLOPT` (`option`); the other trade kinds keep `None`.
- Produces builders in `synth_brokerage.py` returning `bytes` (SGML, one invented account, invented CUSIPs): `ofx_debt_position()` (25,000 units, `UNITPRICE` 98.750, `MKTVAL` 24687.50), `ofx_debt_position_per_unit()` (25,000 units, `UNITPRICE` 0.98750, `MKTVAL` 24687.50), `ofx_option_position(shperctrct: int | None = 100)` (`POSOPT` 2 units, `UNITPRICE` 3.40, `MKTVAL` 680.00, with an `OPTINFO`), `ofx_truncated_values()` (two `POSSTOCK`: 41.263 at 19.8700000 printed 819.89, and 12 at 50.00 printed 600.00), `ofx_option_trades()` (`BUYOPT` 2 at 1.85 `TOTAL` -370.65; no `OPTINFO`).

- [ ] **Step 1: Failing tests** (extract with `extract_ofx(data, OPTIONS, "ofx")` as `test_brokerage_parser.py` does):
  - `test_debt_position_is_priced_per_100`: `price_per_100 is True`, `price_per_unit == Decimal("0.98750")`, `reconcile_investment(statement).status == "ok"`.
  - `test_debt_position_quoted_per_unit_is_left_alone`: `price_per_100 is False`, status `ok`.
  - `test_option_position_price_is_per_contract`: `quantity == 2`, `unit_price == Decimal("340.00")`, status `ok`; with `shperctrct=None` the same (default 100).
  - `test_option_trade_price_is_per_contract_and_classed`: `unit_price == Decimal("185.00")`, `security.asset_class == "option"`, `amount == Decimal("-370.65")`.
  - `test_child_may_say_price_per_100`: `result.worker_only_fields()` is `False` for the debt file.
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_ofx_invest_positions.py`; expect `mismatch` and `price_per_100 is False`.
- [ ] **Step 3: Implement.** Decision: the choice is made by which reading is nearer to the printed value, so the extractor needs no tolerance and no import from the parsers.
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_ofx_invest_positions.py tests/unit/test_brokerage_parser.py tests/unit/test_ofx_variants.py tests/security -k "ofx or fuzz or sandbox_documents"`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): OFX bond prices per 100 and option prices per contract`.

### Task 7: Positions exports with cash rows, total rows and per-account blocks (C8)

**Files:**
- Modify: `backend/app/ingestion/parsers/positions_table.py` (`CASH_SYMBOLS` 210, `downloaded_on` 338-360, `_is_cash` 424-428, `_Group` 445-449, `_statement` 554-612, `read_positions_table` 615-691, `PositionsTable.summary`), `backend/tests/fixtures/synth_positions_csv.py`
- Test: `backend/tests/unit/test_positions_table_parser.py`, `backend/tests/integration/test_positions_export_import.py`

**Interfaces:**
- Cash: a symbol cell whose normalised text is in `CASH_SYMBOLS` or starts with `cash ` (`Cash & Cash Investments`, `Cash and Money Market`).
- Produces `TOTAL_SYMBOLS: Final = frozenset({"account total", "total", "totals", "positions total", "grand total"})`. A row whose symbol cell is one of them is a total row: not data, not `skipped`, not in the body for `READ_SHARE_MIN`; counted in `PositionsTable.total_rows` (summary key `total_rows`). Its value is the block's printed account total: `_Group.printed_total`, which `_statement` uses as `total_value` (in place of the percentage-implied total) when the group is complete, so the existing `total_value` check compares holdings plus cash with it.
- A row whose cells normalise to the header row's cells is a repeated header: skipped as a note, and it starts a new block.
- Produces `block_account(text: str) -> tuple[str | None, str | None]`: `(last four digits, name)` of a one-cell line. Both are read from the text left after dropping a leading `positions for` / `positions for account` and everything from ` as of ` on (so a date or time in a title is never the account's digits): the digits are `cell_last4` of that text; the name is what stands before the first token holding a digit or a mask character; an empty name, or one that normalises to `all accounts`, is `None`. Used only when the header has no `account` column: the nearest one-cell row above a header row (blank rows between them allowed) names the block that follows; rows of different blocks never share a statement.
- Produces `as_of_title(rows: Sequence[Sequence[str]]) -> date | None`: the first date (the three `_DAY_FORMATS`) after the words `as of` on a one-cell row above the header. `read_positions_table` uses `downloaded_on(...) or as_of_title(...) or received_on`.
- Produces builders: `broker_positions_csv()` (title `"Positions for account Individual ...4417 as of 09:30 AM ET, 11/06/2026"`; header `Symbol, Description, Quantity, Price, Market Value, Cost Basis` and a trailing comma on every line; `SGRX` 10 at `$52.10` = `$521.00`; `SBND` 20 at `$24.05` = `$481.00`; `Cash & Cash Investments` with `--` cells and `$298.00`; `Account Total` with `$1,300.00`) and `broker_all_accounts_positions_csv()` (a title, then `Individual ...4417` and `Roth IRA ...8820`, each a one-cell line followed by the header, two holdings, a cash row and an `Account Total`).

- [ ] **Step 1: Failing unit tests:**
  - `test_cash_and_total_rows_of_a_broker_export`: one statement; 2 positions; `cash_closing.amount == Decimal("298.00")`; `total_value.amount == Decimal("1300.00")`; reconciliation `ok` with `total_value` among the checks; `skipped == 0`; `total_rows == 1`; `as_of == date(2026, 11, 6)` and `as_of_assumed is False`; `account.last4 == "4417"`.
  - `test_a_total_row_that_disagrees_fails_the_total_check`: `Account Total` of `$1,350.00`: `failed == ["total_value"]`.
  - `test_blocks_are_separate_accounts`: 2 statements, `last4` `4417` and `8820`, names `Individual` and `Roth IRA`, `account_type` `brokerage` and `retirement`, 2 positions each, `total_rows == 2`.
  - `test_block_account`, parametrised: `"Roth IRA ...8820"` -> `("8820", "Roth IRA")`; `"Positions for account Individual ...4417 as of 09:30 AM ET, 11/06/2026"` -> `("4417", "Individual")`; `"Positions for All-Accounts as of 09:30 AM ET, 11/06/2026"` -> `(None, None)`.
  - Existing `test_cash_rows_are_cash_whatever_they_are_called`, `test_the_header_alone_never_routes_a_file` and `test_a_generic_holdings_layout_reads_without_an_account_column` pass unchanged.
- [ ] **Step 2: Failing integration test** in `test_positions_export_import.py`: `test_all_accounts_export_lands_on_each_account` (two accounts with those last four digits exist: each gets its two holdings and a balance snapshot of its `Account Total`; no review item).
- [ ] **Step 3:** Run; expect `read_positions_table(...) is None` (read share below one half).
- [ ] **Step 4: Implement.**
- [ ] **Step 5:** Run `uv run pytest -q tests/unit/test_positions_table_parser.py tests/integration/test_positions_export_import.py tests/integration/test_structured_import.py`. Expected: pass.
- [ ] **Step 6:** Commit `fix(ingestion): read positions exports with cash rows, total rows and one block per account`.

### Task 8: OCR confidence follows the cells a field was read from (P2)

**Files:**
- Modify: `backend/app/ingestion/parsers/confidence.py`, `payslip.py` (162-164 and the loop that reads each figure), `loan_statement.py` (210-212, `_last_amount` 100-105 and the loop)
- Test: `backend/tests/unit/test_payslip_parser.py` (create), `backend/tests/unit/test_loan_statement_parser.py`

**Interfaces:**
- Produces in `confidence.py`: `scaled_fields(scores: Mapping[str, float], ocr: Mapping[str, Iterable[float | None]], *, confirmed: Collection[str] = ()) -> dict[str, float]`. A field in `confirmed` keeps its score; any other is `scaled(score, lowest non-None value of ocr[field])`; a field with no entry, or only `None` values (a text layer), is unchanged. Module doc bullet "OCR" updated.
- Both parsers record, per field, the cells its value and its label were read from (`dict[str, list[Cell]]`) and call `scaled_fields`. `confirmed`: for a payslip `gross`, `net`, `deductions` when `adds_up is True`; for a loan the money fields a passing check covers (`_scores`' `confirmed` set) when the reconciliation is `ok`. Dates, names and unchecked figures are always scaled by their own cells.

- [ ] **Step 1: Failing tests.** Build views by hand (`DocumentView(lines=..., source_format="image", ...)` with `Cell(..., conf=...)`), no OCR run:
  - `test_scaled_fields` (in `test_payslip_parser.py`, pure): `scaled_fields({"a": 1.0, "b": 0.95, "c": 0.5}, {"a": [0.4], "b": [0.9, None, 0.8]}, confirmed={"a"}) == {"a": 1.0, "b": 0.76, "c": 0.5}`.
  - `test_a_bad_word_elsewhere_does_not_lower_a_payslip_field`: a payslip view whose figure cells have `conf=0.96` and one unrelated line has `conf=0.01`, with figures that add up: `confidence["gross"] == 1.0`, `confidence["pay_date"] == pytest.approx(0.96)`, `low_confidence == []`.
  - `test_a_field_read_from_a_bad_cell_is_low`: the pay date's cell at `conf=0.30`: `"pay_date" in low_confidence`.
  - `test_loan_fields_scale_by_their_own_cells` (in `test_loan_statement_parser.py`): the `classic` layout's lines as an image view, one footer line at `conf=0.02`: every money field 1.0, `paid_on == pytest.approx(conf.GENERIC * 0.96)`.
- [ ] **Step 2:** Run; expect every field near 0.01.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_payslip_parser.py tests/unit/test_loan_statement_parser.py tests/unit/test_statement_parsers.py -k "payslip or loan or scaled"` and `tests/integration/test_loan_statement_pipeline.py tests/integration/test_pdf_statement_pipeline.py -k "payslip or loan"`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): scale a payslip or loan field by the OCR confidence of its own cells`.

### Task 9: Payslips in the wordings and columns payroll services print (P1)

**Files:**
- Create: `backend/app/ingestion/parsers/labelled_cells.py`, `backend/tests/fixtures/synth_payslips.py`
- Modify: `backend/app/ingestion/parsers/payslip.py` (`LABELS` 43, `_amount` 48-53, `parse` 83-180, module doc)
- Test: `backend/tests/unit/test_payslip_parser.py`, `backend/tests/unit/test_statement_parsers.py` (run only: its two payslip tests pass unchanged)

**Interfaces:**
- Produces `labelled_cells.py` (shared with Task 10):
  - `@dataclass(frozen=True) class LabelPair: label: Cell; amounts: tuple[Cell, ...]` and `label_pairs(line: ViewLine) -> list[LabelPair]`: each cell holding letters and no amount, with the amount cells that follow it up to the next such cell. A label and its amount inside one cell (`Net Pay 1,808.40`) is split with `Cell.word_cells()`.
  - `@dataclass(frozen=True) class PeriodColumn: kind: Literal["current", "ytd"]; x0: float; x1: float` and `period_columns(line: ViewLine) -> list[PeriodColumn]`: header cells naming a column. `ytd`: the normalised cell holds `year to date` or the token `ytd`. `current`: the cell is `current`, `this period`, `current period`, `this pay period`, `amount`, `paid last month`, `last month`, `last payment`, `paid this period`, or starts with `paid since` or `since last`. Empty unless the line holds at least one `ytd` cell or two header cells.
  - `current_cells(amounts: Sequence[Cell], columns: Sequence[PeriodColumn]) -> tuple[Cell, ...]`: the amount cells whose centre is nearest a `current` column; with no `current` column known, those not nearest a `ytd` column; with no columns, all of them.
  - `value_under(lines: Sequence[ViewLine], index: int, label: Cell) -> Cell | None` and `value_over(...)`: the cell with the widest x-overlap with `label` on the next (previous) line of the same page, when that line holds amounts or dates and no letters in that cell.
- `payslip.py` vocabulary (normalised; a label matches at the start of a cell or through `label_end`):
  - gross: `gross pay`, `total pay`, `gross earnings`, `total gross`, `total earnings`, `gross wages`;
  - net: `net pay`, `net amount`, `take home pay`;
  - deductions total: `total deductions`, `total taxes and deductions`, `taxes and deductions`, and `deductions` when an amount follows it on its line;
  - deductions heading (starts the section when no amount follows): `deductions`, `taxes`, `taxes deductions`, `taxes and deductions`, `withholdings`; earnings heading: `earnings`, `payments`, `pay`;
  - period: `pay period <date> - <date>` as today; else `period beginning` / `period ending`, `period start` / `period end`, `pay begin date` / `pay end date`, `pay period start` / `pay period end`, each with a date after the label or in the next cell;
  - employer: the cell under an `employer` heading cell (`value_under`), when that cell is not itself a known label; else today's rules, except that text after the word `employer` that is a known label (`pay period`) is never the employer.
- Reading rules: a figure's amount is `current_cells(pair.amounts, columns)[0]`, where `columns` are the `period_columns` of the nearest header line above it in the same block; a heading that is one cell of a wider line (`PAY | Hours | Rate | Current | YTD | DEDUCTIONS | Current | YTD`) splits the lines under it into blocks at each heading cell's `x0`; a line of two or more label cells with no amount takes its values from `value_under`. In a section, a line starting with `total` is never a line item. With no labelled total, the deductions total is the sum of the section's lines. `LABELS` (detection) gains `gross earnings`, `total earnings`, `period ending`, `taxes and deductions`; the threshold (a vocabulary word, or three labels) is unchanged. Page 1 only, as today.
- Produces builders in `synth_payslips.py` (reportlab; employer `Initech Example Ltd`; all figures invented): `summary_columns_pdf()`: a heading row `EMPLOYER | PAY PERIOD`, the employer under the first; `Period Beginning: 02/01/2026`, `Period Ending: 02/15/2026`, `Pay Date: 02/20/2026`; `PAY | Hours | Rate | Current | YTD | DEDUCTIONS | Current | YTD` with `Regular 80.00 30.00 2,400.00 9,600.00` beside `Federal tax 288.00 1,152.00`, `Social insurance 183.60 734.40`, `Retirement plan 120.00 480.00`; a summary `Total Pay $2,400.00 $9,600.00`, `Deductions $591.60 $2,366.40`, `Net Pay $1,808.40 $7,233.60`. `this_period_pdf()`: `Earnings Statement`, `Pay period: 03/01/2026 to 03/15/2026`, `Pay date: 03/20/2026`, `Gross Pay 1,750.00`, a table `Taxes / Deductions | Type | This Period | YTD` with `Federal income tax | Tax | 175.00 | 700.00`, `State tax | Tax | 61.25 | 245.00`, `Health plan | Deduction | 88.00 | 352.00`, no total line, `Net Pay 1,425.75`. `labels_over_values_lines()`: a list of `ViewLine` (image, cells at `conf=0.95`) with `Pay Stub`, `Gross pay | Total deductions | Net pay` over `$3,100.00 | - $930.00 | $2,170.00`, and one unrelated line at `conf=0.02`.

- [ ] **Step 1: Failing tests** in `test_payslip_parser.py` (parse through `parse_extraction` after `extract_pdf`, as `test_statement_parsers.py` does):
  - `test_summary_layout_reads_the_current_column`: employer `Initech Example Ltd`; period `(date(2026, 2, 1), date(2026, 2, 15))`; pay date `date(2026, 2, 20)`; `gross == Decimal("2400.00")`, `total_deductions == Decimal("591.60")`, `net == Decimal("1808.40")`; three deduction lines with the current amounts (`288.00`, `183.60`, `120.00`); `adds_up is True`; `low_confidence == []`; no figure equals a year-to-date amount.
  - `test_deductions_are_summed_when_no_total_is_printed`: `this_period_pdf()`: `total_deductions == Decimal("324.25")`, `adds_up is True`, `low_confidence == []`.
  - `test_labels_over_values`: `labels_over_values_lines()`: gross 3,100.00, deductions 930.00, net 2,170.00, `adds_up is True`, the three at 1.0.
  - `test_a_heading_is_never_the_employer`: in `summary_columns_pdf()` the employer is not `PAY PERIOD`.
  - `test_ytd_first_columns_are_still_read_by_header`: a variant with the columns printed `YTD | Current`: the same figures.
  - `test_existing_payslip_reads_as_before`: `synth_pdf_statements.payslip_pdf()` gives the figures `test_statement_parsers.py` asserts (gross 4,250.00, deductions 1,402.50 from its printed total, net 2,847.50, three deduction lines).
- [ ] **Step 2:** Run; expect `gross is None`, employer `PAY PERIOD`, `adds_up is None`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_payslip_parser.py` and `uv run pytest -q tests/unit/test_statement_parsers.py tests/integration/test_pdf_statement_pipeline.py -k "payslip"`. Expected: pass (a payslip without doubtful fields ends `processed`: `test_payslip_is_kept_on_the_document` already pins that path).
- [ ] **Step 5:** Commit `fix(ingestion): read payslips with current and year-to-date columns and common label wordings`.

### Task 10: Loan and mortgage statements in the periodic-statement layout (L1)

**Decision: extend, not replace.** `loan_statement.py` keeps its line reader for the payment-receipt layout (today's loop moved unchanged into `_read_lines`). A new cell and section reader in `loan_periodic.py` is used only when `is_periodic(view)`. The two layouts of `synth_loans.py` print none of the periodic markers, so they take the line reader and stay green; a test pins that.

**Files:**
- Create: `backend/app/ingestion/parsers/loan_periodic.py`, `backend/tests/unit/test_loan_periodic.py`
- Modify: `backend/app/ingestion/parsers/loan_statement.py` (`_PERCENT` 72, `_LAST4` 73, lender 151-154, `parse` 135-233, `reconcile_loan` 236-255, `_scores` 258-293, module doc), `backend/app/ingestion/sandbox/protocol.py` (the docstring of `LoanReconciliation` gains the `amount_due` line; no field changes), `backend/tests/fixtures/synth_loans.py`
- Test: `backend/tests/unit/test_loan_statement_parser.py`, `backend/tests/integration/test_loan_statement_pipeline.py`

**Interfaces:**
- `loan_periodic.py`:
  - `@dataclass class LoanRead: money: dict[str, Decimal]; due: dict[str, Decimal]; dates: dict[str, date]; rate: Decimal | None; lender: str | None; last4: str | None; next_due_amount: Decimal | None; symbols: set[str]; cells: dict[str, list[Cell]]; warnings: list[str]`. `money` keys are `MONEY_FIELDS`; `due` keys are `principal`, `interest`, `escrow`, `fees`, `regular`, `total`.
  - `is_periodic(view: DocumentView) -> bool`: some line on any page holds a `year to date`/`ytd` column header beside another column header (`period_columns`), or the heading `past payments breakdown`, or the heading `explanation of amount due` / `explanation of payment amount`.
  - `read_periodic(view: DocumentView, styles: tuple[DateStyle, ...]) -> LoanRead`.
- How `read_periodic` reads (each rule has a test):
  1. Lines from a stub marker (`table_statement.STUB_MARKERS`) to the end of its page are ignored.
  2. Section headings (a cell): `explanation of amount due`, `explanation of payment amount` -> `due`; `past payments breakdown`, or any line with period columns -> `paid`; `transaction activity`, `account activity` -> `activity`; `account information`, `loan information` -> `account`. A section owns the cells below its heading whose `x0` lies between the heading cell's `x0` and the `x0` of the next heading cell on the heading's line, down to the next heading in that x-range. Cells outside every section are `account`.
  3. Every line is read as `label_pairs`. In `due`: `principal`, `interest`, `escrow` (also `escrow taxes and insurance`), `fees`/`fees charges`/`total fees charged`/`late charges`, `regular monthly payment` (`regular`), `total amount due`/`amount due` (`total`) fill `LoanRead.due` and `next_due_amount` (the total); they never fill `money`. In `paid`: the same part labels and `total` fill `money`, each from `current_cells(...)`; a column whose header is `ytd` is never read; when the header names a `ytd` column and exactly one other column, that other column is the payment whatever its words; with two or more unnamed others nothing is read and the warning is `loan_columns_unknown`. In `account`: `outstanding principal`, `outstanding principal balance`, `principal balance`, `current principal balance`, `unpaid principal`, `unpaid principal balance`, `new principal balance` -> `balance`; `previous principal balance` -> `previous`; `interest rate` -> the rate.
  4. A label with no amount on its line takes `value_under`, else `value_over` (a balance printed above its label).
  5. Dates: `statement date`, `statement ending` -> `statement_date`; `payment due date`, `next payment due`, `due date`, and `payment date` (in this layout it is the next due date) -> `next_due`; `date last payment received`, `last payment received`, `payment received on` -> `paid_on`. Labels over values use `value_under`.
  6. `paid_on` from the `activity` table when no label gave it: a header line with `date` and `description` and one of `payments`, `amount`, `charges`; rows are lines whose first cell is a date; a payment row's description holds `payment` and none of `late`, `fee`, `due`, `reversal`, `returned`. With `money["total"]` known: the latest payment row whose amount equals it within `TOLERANCE`, else, when all payment rows of the table add up to it, the latest of them; otherwise no `paid_on` and the warning `loan_payment_row_mismatch`. With no total: the latest payment row.
  7. When principal, interest and total of the `paid` column are all zero: no `paid_on` is looked for and the warning is `loan_no_payment_in_period`.
- `loan_statement.py`:
  - `parse` uses `read_periodic` when `is_periodic(view)`, else `_read_lines(view, styles) -> LoanRead`.
  - `reconcile_loan(money: dict[str, Decimal], due: Mapping[str, Decimal] | None = None) -> LoanReconciliation`: `parts` and `balance` as today (on the payment made); new check `amount_due` when `due` holds `principal`, `interest` and (`regular` or `total`): principal + interest + escrow = `regular` when printed, and that sum + fees = `total` when printed, within `TOLERANCE`. Any failed check is a `mismatch`.
  - `_scores`: for a periodic read whose reconciliation is `ok`, a figure no check covers that was read from a label and amount pair (`balance`) scores `conf.GENERIC` in place of `conf.UNCHECKED`; the line reader's scores are unchanged. `paid_on` gets no score when the read carries `loan_no_payment_in_period`.
  - `_PERCENT`: digits directly preceded by a digit and `,` or `.` (with or without a space) are not a rate (an OCR split such as `4, 75%`): the rate stays `None`.
  - Lender: a `Lender` line counts only with a colon after the word (`^\s*lender\s*:`); the first-line rule is unchanged.
  - `_LAST4`: after `loan number`, `loan no`, `account number`, `account no`, a run of 6 or more digits (hyphens and spaces allowed inside) also gives its last four digits. Never more than four are kept.
- Produces in `synth_loans.py` (lender `Placeholder Home Lending`; invented, internally consistent figures): `@dataclass(frozen=True) class PeriodicStatement` with defaults: statement date 2026-03-20; due date 2026-04-01; rate 0.05; outstanding principal 182,440.17; due block principal 412.08, interest 760.17, escrow 301.50, regular 1,473.75, fees 25.00, total 1,498.75; last month principal 410.37, interest 761.88, escrow 301.50, fees 0.00, total 1,473.75; year to date principal 2,444.91, interest 4,588.59, escrow 1,809.00, fees 25.00, total 8,867.50; activity rows `03/02/2026 Payment received, thank you 1,473.75` and `03/17/2026 Late charge 25.00`; loan number `0004417755` printed in full. `periodic_statement_pdf(stmt: PeriodicStatement = PeriodicStatement(), *, variant: Literal["blocks", "labels_over_values", "payment_date_is_due_date", "no_payment"] = "blocks", tamper: Literal["paid", "due"] | None = None) -> bytes`. `blocks`: `Account Information` (with `Outstanding Principal`) and `Explanation of Amount Due` side by side, `Past Payments Breakdown` with `Paid Last Month | Paid Year to Date`, `Transaction Activity` with `Date | Description | Charges | Payments`, a coupon under `Please detach` repeating the amount due. `labels_over_values`: `STATEMENT DATE | PAYMENT DUE DATE` over the two dates, `PRINCIPAL BALANCE` over its amount, columns headed `PAID SINCE 02/20/2026 | PAID YEAR TO DATE`, and a line `LENDER PAID EXPENSES $0.00`. `payment_date_is_due_date`: a `Payment Date 04/01/2026` label in the account block. `no_payment`: the last-month column all `0.00`, no payment row. `tamper="paid"` adds 100.00 to the last-month total; `tamper="due"` to the total amount due.

- [ ] **Step 1: Failing unit tests** in `test_loan_periodic.py`:
  - `test_existing_layouts_take_the_line_reader`: `is_periodic` is `False` for the `classic` and `compact` PDFs; `True` for every `periodic_statement_pdf` variant.
  - `test_last_payment_split_is_read_never_the_year_to_date`: `blocks`: `principal == Decimal("410.37")`, `interest == Decimal("761.88")`, `escrow == Decimal("301.50")`, `fees == Decimal("0.00")`, `total == Decimal("1473.75")`; no field equals 2,444.91, 4,588.59 or 412.08.
  - `test_balance_paid_date_and_due`: `balance == Decimal("182440.17")`, `paid_on == date(2026, 3, 2)`, `statement_date == date(2026, 3, 20)`, `next_due_date == date(2026, 4, 1)`, `next_amount_due == Decimal("1498.75")`, `rate == Decimal("0.05")`, `last4 == "7755"`.
  - `test_blocks_reconcile`: `reconciliation.status == "ok"`, `checks == ["parts", "amount_due"]`, `low_confidence == []`.
  - `test_tampered_blocks_do_not_reconcile`, parametrised `("paid", "parts")`, `("due", "amount_due")`: `status == "mismatch"`, `failed == [check]`.
  - `test_labels_over_values_variant`: the same figures; `lender` is not `LENDER PAID EXPENSES $0.00`.
  - `test_payment_date_label_is_the_due_date`: `paid_on == date(2026, 3, 2)` (the activity row), `next_due_date == date(2026, 4, 1)`.
  - `test_no_payment_in_the_period`: `paid_on is None`, principal and interest zero, warning `loan_no_payment_in_period` in the parse warnings, `"paid_on" not in low_confidence`, `status == "ok"` with `amount_due` among the checks.
  - `test_coupon_figures_are_not_read`: with the coupon's amount changed to another figure, `next_amount_due` is unchanged.
  - `test_split_percentage_is_not_a_rate` (in `test_loan_statement_parser.py`, line view): `Interest rate 4, 75%` gives `rate is None`; `Interest rate 4.75%` gives `Decimal("0.0475")`.
- [ ] **Step 2: Failing integration test** in `test_loan_statement_pipeline.py`: `test_periodic_statement_records_last_months_payment` (a loan with last four `7755` exists, originated before 2026-03): one `LoanPayment` with `paid_on == date(2026, 3, 2)`, principal 410.37, interest 761.88, escrow 301.50; `env.balance(account, date(2026, 3, 2))` is the liability of 182,440.17; no open review item except possibly `loan_rate_differs` (set the loan's rate to 5% so there is none).
- [ ] **Step 3:** Run; expect principal 2,444.91 (the last amount on the line), `balance is None`, `status == "unavailable"`.
- [ ] **Step 4: Implement** `loan_periodic.py`, then the `loan_statement.py` changes. Record every field's cells (Task 8).
- [ ] **Step 5:** Run `uv run pytest -q tests/unit/test_loan_periodic.py tests/unit/test_loan_statement_parser.py tests/integration/test_loan_statement_pipeline.py tests/integration/test_loans_api.py`. Expected: pass, the pre-existing tests unchanged.
- [ ] **Step 6:** Commit `fix(ingestion): read periodic mortgage statements by section and column, never the year-to-date figures`.

### Task 11: Loan bills are loan documents; a statement with no payment is filed (L2)

**Files:**
- Modify: `backend/app/ingestion/parsers/loan_statement.py` (`LABELS` 60-68, `detect` 113-119, `parse`), `backend/app/ingestion/parsers/loan_periodic.py`, `backend/app/domain/loans/statement_import.py` (`import_loan_statement` 108-156, module doc), `backend/tests/fixtures/synth_loans.py`
- Test: `backend/tests/unit/test_loan_statement_parser.py`, `backend/tests/unit/test_loan_periodic.py`, `backend/tests/integration/test_loan_statement_pipeline.py`

**Interfaces:**
- `detect`: `VOCABULARY` on page 1 as today. Labels are counted over **all pages**, each once, a label that is part of a longer matched label not counted. New labels: `outstanding principal`, `current principal balance`, `unpaid principal`, `principal paid`, `interest paid`, `payoff`, `payment due date`, `payment allocation`, `past payments breakdown`, `explanation of amount due`, `date last payment received`, `loan seq`, `int rate`, `amount past due`. Without a vocabulary phrase the score is above zero only with at least 4 labels **and** at least one of `PRINCIPAL_LABELS` (`principal balance`, `outstanding principal`, `current principal balance`, `unpaid principal`, `principal paid`, `loan seq`, `escrow`); the formula is unchanged. (A card statement prints `payment due date`, `payment received`, `amount past due` and `interest rate`, and no principal label.)
- `is_periodic` is also true for a page-2 heading `payment allocation`; under it `principal`, `interest`, `fees`, `late charges` and `total` are the payment made (`money`). A payment row `date | Payment received | amount` anywhere gives `paid_on` by rule 6 of Task 10.
- A bill listing several loans: a header line with `loan seq`, or with `int rate` beside `balance`, followed by two or more rows. The parser reads nothing, returns an empty outcome and the warning `loan_bill_several_loans` (one loan account with sub-loans is out of scope; Task 19 gives the document its reason).
- `import_loan_statement`: when `stmt.paid_on is None`, principal and interest are zero, `stmt.low_confidence` is empty and the reconciliation is `ok` with `amount_due` among its checks, nothing is held: no payment is written, `outcome.summary["no_payment"] = True`, and when `statement_date` and `balance` are both read the balance becomes a `statement` snapshot on `statement_date`. Every other path is unchanged.
- Produces in `synth_loans.py`: `auto_loan_bill_pdf() -> bytes` (two pages; lender `Placeholder Auto Finance`; page 1: `Account Statement`, `Account number 300044171234`, an amount `$18,250.40` printed above the label `CURRENT PRINCIPAL BALANCE`, `Payment Due Date 04/15/2026`, `Total Amount Due $455.20`, a row `03/14/2026  Payment Received. Thank you!  $455.20`; page 2: `PAYMENT ALLOCATION` with `Principal $371.85`, `Interest $83.35`, `Total $455.20`) and `student_loan_bill_pdf() -> bytes` (one page: `Date Last Payment Received`, `Principal Paid Since Last Statement`, `Interest Paid Since Last Statement` over a row of values, and a table `Loan Seq | Program | Int Rate | Balance | Amount Past Due | Current Amount Due` with two loans).

- [ ] **Step 1: Failing tests:**
  - `test_loan_bills_are_detected`: `LoanStatementParser().detect(view) >= 0.6` for both bills; `0.0` for `synth_card_statement`'s card statement and for the bank statements `test_bank_statements_are_not_loan_statements` already uses.
  - `test_auto_loan_bill_is_read`: forced through `LoanStatementParser().parse`: principal 371.85, interest 83.35, total 455.20, `paid_on == date(2026, 3, 14)`, `balance == Decimal("18250.40")`, `last4 == "1234"`, `status == "ok"` (`parts`).
  - `test_bill_of_several_loans_reads_nothing_and_says_so`: outcome empty, warnings `["loan_bill_several_loans"]`.
  - Integration `test_statement_without_a_payment_files_the_balance`: `periodic_statement_pdf(variant="no_payment")` for a matching loan: no `LoanPayment`, document `processed`, no review item, balance snapshot on 2026-03-20.
- [ ] **Step 2:** Run; expect detect `0.0` and a `loan_low_confidence` item.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the three test files of Task 10 plus `tests/unit/test_card_statement.py tests/unit/test_statement_parsers.py tests/unit/test_unseen_layouts.py tests/security/test_parser_trust.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): recognise loan bills; file a loan statement that reports no payment`.

### Task 12: SSA-1099 (T2)

**Files:**
- Modify: `backend/app/domain/tax/forms/us.py` (a new `FormDef`, listed in `US_FORMS` 445-466), `backend/tests/fixtures/synth_tax_forms.py` (`ISSUERS`, `KEY_BOXES`), `backend/tests/unit/test_tax_forms.py` (`US_REQUIRED`), `frontend/src/features/tax/api.ts` (`US_FORMS`), `backend/app/ingestion/parsers/tax_forms.py` (module doc only), `docs/DATA_MODEL.md` (about 843), `docs/INGESTION.md` (about 407)
- Test: `backend/tests/unit/test_tax_form_parser.py`, `backend/tests/integration/test_tax_form_pipeline.py`

**Interfaces:**
- `FSSA_1099 = FormDef("SSA-1099", "Social Security Benefit Statement", "payer", (...))` with boxes: `ident("1", "Name")`, `ident("2", "Beneficiary's social security number")`, `amt("3", "Benefits paid")`, `amt("4", "Benefits repaid to SSA")`, `amt("5", "Net benefits")`, `amt("6", "Voluntary federal income tax withheld")`, `ident("7", "Address")`, `ident("8", "Claim number")`. No issuer box (`issuer_name` stays `None`). Placed after `F1099_SA` in `US_FORMS`.
- The heading `Form SSA-1099` and the keys `3 benefits paid`, `4 benefits repaid`, `5 net benefits`, `6 voluntary federal` follow from the definition. One parser change in `_FormInfo.label_of` (`tax_forms.py` 181-186): a leading token `box` before the id is ignored (`Box 3. Benefits Paid in 2025` is the label of box 3), for every form.

- [ ] **Step 1: Failing tests:** add `"SSA-1099": ("3", "5", "6")` to `KEY_BOXES` (which parametrises `test_every_form_is_read_exactly` and `test_identity_values_never_leave_the_parser`) and `"SSA-1099"` to `US_REQUIRED`; add `test_ssa_1099_is_not_read_as_a_1099` (a page headed `Form SSA-1099` gives exactly one form, of type `SSA-1099`); `test_box_prefix_before_an_id_is_a_label` (a line view with `Box 3. Benefits Paid in 2025` over `18,240.00` and `Box 5. Net Benefits for 2025` over `18,240.00` under that heading reads boxes 3 and 5); integration `test_ssa_1099_lands_in_the_tax_vault` (boxes 3, 5 and 6 stored; the stored pages hold no fake SSN or claim number).
- [ ] **Step 2:** Run `uv run pytest -q tests/unit/test_tax_forms.py tests/unit/test_tax_form_parser.py -k "SSA or required"`; expect `UnknownFormError` / a missing form type.
- [ ] **Step 3: Implement**; adapt the fixture's drawing where it assumes an issuer box.
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_tax_forms.py tests/unit/test_tax_form_parser.py tests/unit/test_tax_identity_detection.py tests/integration/test_tax_form_pipeline.py tests/integration/test_tax_api.py`, then in `frontend/`: `npm run typecheck` and `npm test -- tax`. Expected: pass.
- [ ] **Step 5:** Commit `feat(tax): read the SSA-1099`.

### Task 13: Composite 1099 statements (T1)

**Files:**
- Modify: `backend/app/ingestion/parsers/tax_forms.py` (`_forms_of` 192-205, `_Section` 238-242, `_heading` 245-252, `_sections` 255-268, `_read_boxes` 486-512, `_tax_year` 473-483, `TaxFormParser.detect` 573-580, module doc), `backend/tests/fixtures/synth_tax_forms.py`
- Test: `backend/tests/unit/test_tax_form_parser.py`

**Interfaces:**
- Produces `split_leaders(cell: Cell) -> tuple[Cell, ...]`: the cell cut at every run of three or more dots (also dots separated by single spaces); pieces keep x-bounds from the cell's word boxes, a cut inside one word placed by character offset; a cell with no leader is returned alone. Every line of a view is passed through it once before sections are built.
- Headings: `_headings(line: ViewLine, forms: Sequence[_FormInfo]) -> list[tuple[_FormInfo, Cell]]`. A heading cell is (a) as today, the cell holding `form <type>`, or (b) new, a cell whose normalised text starts with a year from 1990 to 2100 followed by the type (`2025 1099 div ...`). Not a heading: a contents line (after leader splitting its last cell is a whole number of at most three digits and the line holds no amount with decimals); a line naming three or more different registered form types; a `form <type>` that stands inside parentheses.
- Sections: `_Section` gains `x0: float` and `x1: float` (default minus and plus infinity). A line with two heading cells of different forms opens two sections side by side, cut at the second heading cell's `x0`; both take the following lines until the next heading line; `_read_boxes` reads only cells whose `x0` lies in its section's range. `_tax_year` reads the year from the section's own heading cell first.
- Lone box id: in `_read_boxes`, a cell that is exactly a box id of the form (at most three characters) followed by a cell starting with a letter is read as one label cell when the two joined (`join_cells`) satisfy `label_of`.
- `detect`: 0.97 when any section holds at least two label cells of **amount** boxes (`BoxKind.AMOUNT`, `CODED_AMOUNT`); identity labels do not count; else 0.3 when there is a section; else 0.
- Produces `composite_1099_pdf(tax_year: int = 2025) -> bytes` (issuer `Placeholder Brokerage`): page 1 with a sentence `This statement includes IRS Form 1099-B, 1099-DIV, 1099-INT and 1099-MISC.` and a contents block (`Form 1099-DIV Dividends and Distributions ........ 2`, `Details of dividends (Form 1099-DIV) ........ 4`); page 2 with two forms side by side on one line of headings, left `Summary of Form 1099-B`, right `2025 1099-DIV Dividends and Distributions OMB No. 0000-0000`; under them rows drawn as one string each: left `Gross proceeds......................9,412.77`, right `1a` then `Total ordinary dividends......................1,840.25` (the id drawn apart from its label), `1b Qualified dividends......1,210.40`, `2a Total capital gain distributions......96.15`, `3 Nondividend distributions......12.00`, `4 Federal income tax withheld......184.02`; lower on the page `2025 1099-INT Interest Income` with `1 Interest income......311.08` and `4 Federal income tax withheld......31.10`.

- [ ] **Step 1: Failing tests:**
  - `test_composite_statement_reads_each_form`: forms by type: `1099-DIV` boxes `{"1a": 1840.25, "1b": 1210.40, "2a": 96.15, "3": 12.00, "4": 184.02}`, `tax_year == 2025`; `1099-INT` boxes `{"1": 311.08, "4": 31.10}`; no `1099-B` form (its summary prints no box ids); no form from page 1.
  - `test_box_4_of_one_form_is_not_read_into_its_neighbour`: the `1099-DIV` box 4 is 184.02 and the `1099-INT` box 4 is 31.10.
  - `test_contents_and_sentences_start_no_form`: a view of page 1 alone: `read_tax_forms(...) == []` and `TaxFormParser().detect(view) < 0.35`.
  - `test_detect_looks_at_every_section`: `detect` of the whole composite is above 0.9.
  - `test_split_leaders`, parametrised: `"Total ordinary dividends......1,840.25"` gives two cells; `"U.S. Treasury obligations"` and `"No. 0000-0000"` give one.
  - `test_every_form_is_read_exactly`, `test_consolidated_statement_gives_one_form_per_heading`, `test_a_mention_of_a_form_is_not_a_form` and `test_identity_values_never_leave_the_parser` pass unchanged.
- [ ] **Step 2:** Run; expect an empty list and `no_tax_form_boxes`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_tax_form_parser.py tests/unit/test_tax_forms.py tests/unit/test_tax_identity_detection.py tests/integration/test_tax_form_pipeline.py tests/security -k "tax"`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): read composite 1099 statements (year-first headings, lone box ids, dot leaders)`.

### Task 14: A multi-page document or a bill is never a receipt (R1)

**Files:**
- Modify: `backend/app/ingestion/parsers/receipt.py` (only the places listed under "Shared with 1b"), `backend/tests/fixtures/synth_receipts.py`
- Test: `backend/tests/unit/test_receipt_parser.py`, `backend/tests/unit/test_receipt_accuracy.py` (run only)

**Interfaces:**
- `MAX_RECEIPT_PAGES: Final = 2`. `ReceiptParser.detect`: `0.0` when `view.page_count > MAX_RECEIPT_PAGES`; else `_scores(lines, pages=view.page_count)`.
- `_scores(view_lines: Sequence[ViewLine], *, pages: int = 1) -> float`: after the `_STATEMENT_WORDS` return, `0.0` when the folded text holds any of `_BILL_WORDS`; before the final return, `min(score, 0.3)` when `pages == 2` and none of `_RECEIPT_WORDS` is present (0.3 is below `registry.MIN_SCORE`, so such a document is a receipt only when the classifier says so). `parse_email_receipt` keeps calling `_scores(lines)`.
- `_BILL_WORDS` (whole words, matched in the folded text; none is printed by a till or an order confirmation): `principal balance`, `outstanding principal`, `principal paid`, `interest paid`, `interest rate`, `escrow`, `payoff`, `loan number`, `loan seq`, `payment due date`, `past due`, `amount past due`, `payment coupon`, `payment allocation`, `account statement`, `activity statement`, `starting cash`, `ending cash`, `net asset value`, `vested balance`. Decision: `year to date`, `ytd`, `due date` and `account number` are left out (loyalty totals, invoices and store accounts print them on real receipts).
- Produces in `synth_receipts.py`: `bill_text(kind: Literal["auto_loan", "student_loan", "activity_statement"]) -> str` (short invented texts, each with a `Total` or `Total amount due` line and two or three of the words above) for `view_lines_from_text`.

- [ ] **Step 1: Failing tests:**
  - `test_a_bill_is_not_a_receipt`, parametrised over the three kinds: `_scores(view_lines_from_text(bill_text(kind))) == 0.0`.
  - `test_more_than_two_pages_is_not_a_receipt`: a `DocumentView` built from a receipt's lines with `page_count=4`: `ReceiptParser().detect(view) == 0.0`; with `page_count=1` the score is the one `test_detection` already expects.
  - `test_two_pages_without_receipt_words_is_below_the_minimum`: `_scores(lines, pages=2) <= 0.3` for a text with `Total` and no receipt word; a two-page text with `Thank you` and `Subtotal` keeps its score.
  - `test_a_mail_bill_is_not_a_receipt_mail`: `parse_email_receipt` of a mail whose body is `bill_text("auto_loan")` is `None`.
  - `test_no_synthetic_receipt_prints_a_bill_word`: for every spec of `tuning_specs()` and `held_out_specs()`, the folded text of `receipt_text(spec)` holds none of `_BILL_WORDS` (so the one new zero path cannot change a score of the accuracy sets; the page rules do not apply to one page).
- [ ] **Step 2:** Run; expect scores of 0.45 to 0.8 for the bills.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_receipt_parser.py tests/unit/test_receipt_accuracy.py tests/integration/test_receipt_pipeline.py tests/integration/test_receipt_matching.py tests/integration/test_email_intake.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): a bill or a document of more than two pages is not a receipt`.

### Task 15: A purchase date beside an expiry date is kept (R2)

**Files:**
- Modify: `backend/app/ingestion/parsers/receipt.py` (`_dates_in` 547-578, `_find_date` 581-609)
- Test: `backend/tests/unit/test_receipt_parser.py`

**Interfaces:**
- Produces `_purchase_dates(text: str, order: date_rules.DateOrder | None) -> list[tuple[date, DateKind, Literal["ymd", "mdy", "dmy", "named", "guessed"]]]`: the dates of `_dates_in` without those whose own lead-in says they are not the purchase: `_NOT_PURCHASE_DATE` matched in the folded text between the previous date's end (or the line start) and the date's start. `_find_date` drops the whole-line `continue` (591-592) and iterates `_purchase_dates`.

- [ ] **Step 1: Failing tests:**
  - `test_purchase_date_survives_an_expiry_on_its_line`: a receipt whose date line is `03/14/2026 11:42 AM   EXPIRES 06/12/26`: `purchase_date == date(2026, 3, 14)`, `time == "11:42"`, date confidence 1.0 (a date beside a time).
  - `test_a_lone_expiry_or_return_date_is_still_ignored`: lines `EXPIRES 06/12/26` and `Return by 04/13/2026` with no other date: `purchase_date is None`.
  - `test_expiry_first_then_purchase_date`: `EXP 06/12/26  DATE 03/14/2026` gives 2026-03-14.
  - The existing `test_labelled_date_wins_and_expiry_dates_are_ignored` passes unchanged.
- [ ] **Step 2:** Run; expect `purchase_date is None` in the first.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_receipt_parser.py tests/unit/test_receipt_accuracy.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): keep a receipt's date when its line also prints an expiry`.

### Task 16: A label takes the nearest amount-only line, and the set that adds up wins (R3)

**Files:**
- Modify: `backend/app/ingestion/parsers/receipt.py` (`_typed` 350-372, `read_receipt` 818-828), `backend/tests/fixtures/synth_receipts.py`
- Test: `backend/tests/unit/test_receipt_parser.py`

**Interfaces:**
- `_typed(lines: Iterable[ViewLine], *, pairing: Literal["below", "nearest", "above"] = "below") -> list[_Line]`. A keyword line without an amount (`subtotal`, `tax`, `tip`, `discount`, `total`, `tender`, `change`) may take the amounts of an adjacent amount-only line (fewer than two letters) on the same page: `below` the following line (today's rule, unchanged); `above` the preceding line; `nearest` whichever of the two is vertically nearer by the distance between line centres, the following line on a tie, and none farther than 1.5 line heights. An amount-only line is given to one label only.
- `read_receipt` reads with `below`, then `nearest`, then `above`, and keeps the first reading whose `_solve(...).adds_up` is `True`; when none adds up it keeps `below`. So a receipt that adds up today reads exactly as today. `_scores` keeps the default.
- Produces in `synth_receipts.py`: `amounts_above_labels_lines() -> list[ViewLine]`: a header, three item lines (`COFFEE BEANS 18.40`, `OAT MILK 4.80`, `BREAD 18.00`), then `41.20` half a line height above `SUBTOTAL`, `3.30` above `TAX`, `44.50` above `TOTAL` (each amount on its own line, y positions set so each amount is nearer the label below it than the label above it), and a date line.

- [ ] **Step 1: Failing tests:**
  - `test_amounts_printed_above_their_labels`: `subtotal == Decimal("41.20")`, `tax == Decimal("3.30")`, `total == Decimal("44.50")`, `adds_up is True`, `confidence["total"] == 1.0`.
  - `test_the_reading_that_adds_up_wins`: the same lines with vertical positions that make `nearest` ambiguous (equal distances): the result is still the consistent set (reached by `above`).
  - `test_label_and_amount_on_separate_lines` (existing: amounts below) passes unchanged.
  - `test_a_receipt_that_adds_up_reads_as_before`: for every spec of `tuning_specs()`, `read_receipt` gives the totals of the spec (already asserted by `test_receipt_accuracy.py`; run it).
- [ ] **Step 2:** Run; expect subtotal 3.30 and tax 44.50, or no total.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_receipt_parser.py tests/unit/test_receipt_accuracy.py tests/integration/test_receipt_pipeline.py`. Expected: pass, with the accuracy thresholds of `test_receipt_accuracy.py` unchanged.
- [ ] **Step 5:** Commit `fix(ingestion): pair a receipt label with the nearest amount line and keep the set that adds up`.

### Task 17: A written option stays out of the lot engine; the OFX fixes end to end (C5, second half; final phase)

**Files:**
- Modify: `backend/app/domain/investments/statement_import.py` (`_commit` 2046-2069 and the `investment_short_position` item 2147-2171, module doc step 7), `backend/tests/fixtures/synth_brokerage.py`, `frontend/src/features/review/reviewCopy.ts` (`low_confidence_field:investment_short_position`, 537-556) and `reviewCopy.test.ts`
- Test: `backend/tests/integration/test_brokerage_pipeline.py`, `backend/tests/integration/test_activity_table_import.py`

**Interfaces:**
- Produces `_written_options(ctx: _Context, account: Account, statement: InvestmentStatement) -> list[tuple[SecurityRef, Decimal]]`: for each security whose rows carry `asset_class == "option"` on any of their refs, the running units over the statement's unit rows (`UNIT_TYPES`, ordered as `_shortfall_openings` orders them), starting from the units the account holds of the security `_find_security` resolves (zero when it is unknown); returned with its lowest running units when that falls below `-UNIT_TOLERANCE`. Only options: a non-option security that would go negative keeps today's paths (an activity export opens the shortfall at an unknown basis; otherwise the lot engine decides).
- `_commit`: the keys of those securities join `shorted`; their unit lines are left out of `writable` exactly as a printed short position's are; cash rows and every other security import. The `investment_short_position` item is raised once per statement with the existing payload plus `"from_activity": true` when no printed position is negative, and one `positions` entry per written option (`symbol`, `name`, `quantity` = the lowest running units, `market_value` null, `page` null).
- Limit, stated in the docs: the rule sees one statement. An OFX file that only closes a short opened in an earlier period (`BUYOPT` with nothing short on record) is read as a purchase, because the extractor carries no open/close marker and no field is added for one; the activity-table reader counts such a row instead (Task 3).
- Copy: with `from_activity` the sentence reads that the statement sells an option the account did not hold (a written option), that PerFin does not track written options yet and that its trades were left out; the steps stay.
- Produces `ofx_written_options() -> bytes` in `synth_brokerage.py`: no position list; `SELLOPT` 1 contract `OPTSELLTYPE` `SELLTOOPEN` (`TOTAL` 109.35), `TRANSFER` `TFERACTION` `IN` of 1 unit of the same `SECID` (the expiry), one `BUYSTOCK` of 5 at 40.00 (`TOTAL` -200.00), one `INCOME` `DIV` of 3.10, one `INVBANKTRAN` deposit of 500.00.

- [ ] **Step 1: Failing tests:**
  - `test_written_options_are_left_out_and_the_rest_imports` (`test_brokerage_pipeline.py`, OFX): one holding (the stock, 5 units); no `investment_txn` on the option security; the deposit is a ledger transaction; one open item `investment_short_position` with `payload["from_activity"] is True`, `payload["lines_left_out"] == 2`; no `investment_lot_conflict`.
  - `test_written_option_in_an_activity_export` (`test_activity_table_import.py`): `broker_activity_csv` with a stock `Buy`, a `MoneyLink Transfer` of `$500.00`, `Sell to Open` and `Buy to Close` on `ZZOP 06/19/2026 40.00 P`: the stock and the deposit import; no opening at an unknown basis is created for the option; the item as above.
  - `test_a_stock_sold_before_the_exports_first_row_still_opens_at_unknown_basis`: a `Sell` of 3 `SGRX` with nothing held: `investment_opening_basis_unknown` as today, no `investment_short_position`.
  - `test_long_option_round_trip_goes_through_the_lot_engine`: `Buy to Open` 2 then `Sell to Close` 2: two `investment_txn` rows, no holding left, no item.
  - `test_ofx_with_truncated_cent_values_imports` (`ofx_truncated_values()`, Tasks 5 and 6): statement imported, 2 holdings, no `investment_reconciliation_failed`.
  - `test_ofx_bond_position_price_is_one_units` (`ofx_debt_position()`): the stored `price.close` for the bond is `0.98750`.
  - vitest: `investment_short_position` with `from_activity: true` renders `written option`; without it the existing assertion (`a short position`) still holds.
- [ ] **Step 2:** Run; expect `investment_lot_conflict` (OFX) and an unknown-basis opening plus a phantom holding (CSV).
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `uv run pytest -q tests/integration/test_brokerage_pipeline.py tests/integration/test_activity_table_import.py tests/integration/test_investments_api.py tests/integration/test_name_only_securities.py tests/unit/test_investment_lots.py` and `npm test -- reviewCopy`. Expected: pass.
- [ ] **Step 5:** Commit `fix(investments): keep written options out of the lot engine and import the rest of the statement`.

### Task 18: A currency is an ISO code PerFin knows (final phase)

**Files:**
- Create: `backend/app/ingestion/parsers/currency.py`, `backend/tests/unit/test_parser_currency.py`
- Modify: `backend/app/ingestion/parsers/loan_statement.py` (71, 155-157), `loan_periodic.py`, `payslip.py` (45, 104-106), `brokerage.py` (429, 2455-2457), `brokerage_table.py` (251, 530-533), `positions_table.py` (212, 504-506), `table_statement.py` (315, 897: this one use only)

**Interfaces:**
- Produces `stated_currency(text: str) -> str | None`: the code after the word `currency` (any case, optional colon) when it is three **upper-case** letters as printed and `app.core.money.is_currency(code)`; else `None`. Produces `known_currency(cell: str) -> str | None`: the cell upper-cased when it is a known ISO code; else `None`.
- Every `_CURRENCY.search(...)` listed above is replaced by `stated_currency(line.text)` and the local patterns are deleted. In the two table readers a filled currency cell that is not a known code makes the row unreadable (`skipped`, as a malformed cell does today).

- [ ] **Step 1: Failing tests** in `test_parser_currency.py`: `stated_currency("Currency: USD") == "USD"`; `"Currency Forward Contracts"`, `"currency for the period"`, `"Currency: XQZ"`, `"Currency: usd"` give `None`; `known_currency("eur") == "EUR"`, `known_currency("ABC") is None`. In `test_brokerage_parser.py`: `test_a_word_after_currency_is_not_the_accounts_currency` (a `DocumentView` of the placeholder brokerage PDF's lines with one added line `Currency Forward Contracts`, parsed by the brokerage parser: `account.currency` equals what the PDF alone gives, never `FOR`). In `test_brokerage_table_parser.py`: a row whose currency cell is `ABC` is skipped and counted.
- [ ] **Step 2:** Run; expect `FOR` and an accepted `ABC`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `uv run pytest -q tests/unit -k "currency or brokerage or positions or payslip or loan or statement_parsers or card_statement"`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): accept a document's currency only when it is a known ISO code`.

### Task 19: A receipt, payslip, tax form or loan document that could not be read says so (R4; final phase)

**Files:**
- Modify: `backend/app/domain/review/layout.py` (`with_layout_tables` 130-142), `backend/app/domain/imports/service.py` (the `no_records` branch, about 1881, as 1a Task 14 left it), `frontend/src/features/review/reviewCopy.ts`, `reviewCopy.test.ts`
- Test: `backend/tests/integration/test_receipt_pipeline.py`, `backend/tests/integration/test_loan_statement_pipeline.py`, `backend/tests/integration/test_tax_form_pipeline.py`, `backend/tests/unit/test_payslip_parser.py`

**Interfaces:**
- Produces in `layout.py`: `unread_kind(result: ExtractResult) -> Literal["receipt", "payslip", "tax_form", "loan"] | None`: the parser the registry chose (`result.parse.parser.name`) is `receipt_basic`, `payslip_basic`, `tax_forms` or `loan_statement_generic`, it read nothing (`parse.receipt`, `parse.payslip`, `parse.loan` are `None`, `parse.tax_forms` is empty) and the result holds no statement and no investment statement.
- `with_layout_tables` returns the result unchanged when `unread_kind(result)` is not `None`: the page's table is not offered to the column mapper.
- New `unparsed` reasons, raised where `no_records` is raised today, with payload `{"reason": ..., "parser": <name>, "warnings": <the parse warnings>}`: `receipt_unreadable`, `payslip_unreadable`, `tax_form_unreadable`, `loan_unreadable`. They take precedence over `no_records` and 1a's `no_rows`. None joins `MAPPING_REASONS`. (`low_confidence_field:tax_form_unread`, raised when forms were found and none could be stored, is unchanged.)
- Copy, one entry each (title, what, steps): what the document looks like, that PerFin could not read its figures (for `loan_unreadable` with the warning `loan_bill_several_loans`: that it lists several loans, which PerFin does not read yet), and what to do: look at the document, enter the figures where they belong, dismiss. Before writing a step, check in the frontend that the action it names exists; name no action that does not.

- [ ] **Step 1: Failing tests:**
  - `test_unreadable_receipt_says_so` (`test_receipt_pipeline.py`): a one-page PDF with a store name, `Thank you`, item names and a `TOTAL` label whose amounts are missing (what OCR leaves of a faded print): one open `unparsed` item with `reason == "receipt_unreadable"`; no item with `mapping_required`; document `needs_review`.
  - `test_bill_of_several_loans_says_so` (`test_loan_statement_pipeline.py`, `student_loan_bill_pdf()`): `reason == "loan_unreadable"`, `"loan_bill_several_loans" in payload["warnings"]`.
  - `test_auto_loan_bill_is_a_loan_document_not_a_receipt` (same file, `auto_loan_bill_pdf()` with a matching loan): parser `loan_statement_generic`, one `LoanPayment` of principal 371.85 on 2026-03-14, document kind `statement`, no `receipt_match` item. (Needs Tasks 11 and 14.)
  - `test_blank_tax_form_says_so` (`test_tax_form_pipeline.py`): a form drawn with labels and no amounts: `reason == "tax_form_unreadable"`; the stored pages are still redacted.
  - `test_unread_kind` (unit, in `test_payslip_parser.py`): an `ExtractResult` whose parse names `payslip_basic` with no payslip gives `"payslip"`; with a payslip gives `None`; a result with a statement gives `None`.
  - vitest: the four reasons are in `REASONS` (each has its own words and at least one step) and `explain(...)` of `loan_unreadable` with that warning mentions several loans.
- [ ] **Step 2:** Run; expect `mapping_required` or `no_records`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run `uv run pytest -q tests/integration/test_receipt_pipeline.py tests/integration/test_loan_statement_pipeline.py tests/integration/test_tax_form_pipeline.py tests/integration/test_structured_import.py tests/integration/test_imports_api.py tests/integration/test_review_api.py tests/integration/test_teach_layout.py` and `npm test -- reviewCopy`. Expected: pass.
- [ ] **Step 5:** Commit `fix(review): say when a receipt, payslip, tax form or loan document could not be read`.

### Task 20: Documentation (final phase)

**Files:**
- Modify: `docs/INGESTION.md` ("5. Parse": the payslip, receipt, tax form and loan parser paragraphs about 407, 447 and 562; "Brokerage activity exports" from 1219; "Brokerage positions exports" from 1344; "Loan statements" from 1468; the review queue list of `unparsed` reasons from 1810), `CHANGELOG.md` (`## [Unreleased]`: one Fixed or Changed entry per task, in the file's voice), `docs/security/CONTROLS.md` only if a cited test name moved

- [ ] **Step 1:** Update the documents: the action words and what each types as, the cash-leg rule, option symbols and the written-option rule, the one-cent slack and what it does not cover, the per-100 and per-contract rules, positions blocks and total rows, the periodic loan layout (sections, columns, checks, the no-payment case, what is not stored), payslip columns, composite 1099 headings, the receipt exclusions and pairing, per-field OCR confidence, the currency rule, the four new reasons. `mkdocs build --strict` passes.
- [ ] **Step 2:** Full gates: `make check` equivalents in the container (`uv run ruff check .`, `ruff format --check .`, `mypy`, `bandit`, `pytest -n 3`, OpenAPI drift: expect none) and the frontend (`npm run lint`, `typecheck`, `test`, `build`).
- [ ] **Step 3:** Commit `docs(ingestion): the brokerage, loan, payslip, tax form and receipt fixes of 1c`.

### Task 21 (optional, last feature): 401(k) plan statements in the roll-forward layout (R1)

May be dropped or moved to a follow-up without touching Tasks 1 to 20: it adds one new module, one new review reason, and one clause in each of four shared files. Its documentation and changelog lines are part of this task.

**What a plan statement becomes (decision).** A **balance of the retirement account and nothing else**: the plan's ending balance on the period's last day as a `statement` balance snapshot, and, when the roll-forward adds up, the beginning balance on the day before the period (the existing `opening_value` rule). No holdings, no lots, no investment transactions, no ledger transactions.

Why not per-fund positions: in the data model a holding is the lot engine's cache (`holding` is rewritten from `lot` rows by `replay_position`), a lot needs units and a cost per unit, and a roll-forward table prints dollars per fund. Writing the dollars as units at a price of 1 would report a gain of zero for ever and raise `position_mismatch` every quarter the market falls. A per-fund balance needs a table of its own, which means a migration; `0013` is taken by 1d, so that is a later package.

Limits, stated in the docs: the Investments page shows no funds for the plan; contributions, earnings, fees and withdrawals are not transactions (the salary deferral is on the payslip, the employer match appears only in the balance); vesting is not recorded (the balance stored is the ending balance, not the vested balance); one plan account per document.

**Files:**
- Create: `backend/app/ingestion/parsers/plan_statement.py`, `backend/tests/unit/test_plan_statement.py`, `backend/tests/fixtures/synth_plan_statement.py`
- Modify: `backend/app/ingestion/parsers/registry.py` (`PARSERS` 73-84: one entry after the brokerage parsers), `backend/app/ingestion/parsers/base.py` (`ParseOutcome.empty` 270-279: one clause), `backend/app/ingestion/pipeline.py` (the `read` list, about 624-627: one clause), `backend/app/domain/investments/statement_import.py` (`import_investment_statements`, before `_held`, about 2428), `frontend/src/features/review/reviewCopy.ts` and its test, `docs/INGESTION.md`, `CHANGELOG.md`
- Test: `backend/tests/integration/test_brokerage_pipeline.py`

**Interfaces:**
- `plan_statement.py`: `PARSER_NAME: Final = "retirement_plan_statement"`; `class RetirementPlanParser(DocumentParser)` (`kind = "statement"`, version `"1"`).
  - `detect`: 0.65 when a line names `beginning balance` and `ending balance` in separate cells with at least one cell between them from `contributions`, `earnings`, `earnings fees`, `gain loss`, `withdrawals`, `distributions`, `transfers`, `forfeitures`, `fees`; else 0. (The generic brokerage parser scores 0 on such a page: it needs a quantity column.)
  - `parse`: rows under that header are a name and amounts placed under the header cells by nearest centre; `sub total` rows are skipped; the `total` row is the printed total. Checks, in `InvestmentReconciliation.checks`: `roll_forward` (the total row: beginning plus the movement columns is ending) and `roll_forward_rows` (the same for every row, and the rows' ending balances add up to the total's). Movement columns are added as printed (parentheses negative); when that fails, the columns headed `withdrawals`, `distributions`, `fees` and `forfeitures` are subtracted instead, and the convention that makes the total row add up is used for every row. Tolerance `TOLERANCE` per comparison. A second roll-forward table on the document with the same ending total (the same money by source) is ignored; one with another ending total means several accounts: nothing is read, warning `plan_statement_several_accounts`. No total row: warning `plan_statement_no_total`, status `unavailable`.
  - Output: one `InvestmentStatement` (`source_format` `pdf` or `image`, `account_type="retirement"`, institution from the first line, period from `for period:` / `statement period:` through `read_period`, `as_of` = the period's end, no positions, no activity, `total_value` = the ending total, `figures` of group `account_value`: the beginning total as `opening`, each movement column's total as an `item` labelled by its header, the ending total as `closing`), with its reconciliation and `confidence` for `period` and `total_value` (scaled by the cells read, Task 8).
- `base.py`: `BALANCE_ONLY_PARSERS: Final = frozenset({"retirement_plan_statement"})`; `ParseOutcome.empty` also counts an investment statement whose parser is one of them and whose `total_value` is set. `pipeline.py` counts the same statement as read (document kind `statement`, its period).
- `statement_import.py`: `_balance_only(statement) -> bool` (parser in `BALANCE_ONLY_PARSERS`, no positions, no activity, `total_value` set) and `_commit_balance(ctx, statement, account, check) -> bool`. After the account and overlap steps: status `ok` and `"period"` not in doubt: the closing snapshot, the opening snapshot under `opening_value`'s rule, `record_statement`, the `investment.import` audit row with `"balance_only": true`; otherwise nothing is written and one `low_confidence_field` item `plan_statement_does_not_add_up` with `account_id`, `currency`, `status`, `failed`, `printed` (the ending total), `computed` and `difference`. "Import as read" is not offered for it (`acceptable_as_read` is false).
- Copy for `low_confidence_field:plan_statement_does_not_add_up`: the statement's beginning balance plus the period's changes is not its ending balance (or could not be checked), so no balance was recorded; steps: check the document, enter the balance on the account, dismiss.
- Produces `plan_statement_pdf(*, tamper: bool = False, by_source: bool = True, second_plan: bool = False) -> bytes`: `Account Summary` with a period line `for period: 01/01/2026 - 03/31/2026`; a table `By Investment | Beginning Balance | Earnings/Fees | Contributions | Withdrawals | Transfers | Ending Balance` with three invented funds (`Example Target 2050 Fund 20,000.00 500.00 1,200.00 0.00 (300.00) 21,400.00`; `Example Bond Index Fund 8,000.00 (120.00) 600.00 0.00 300.00 8,780.00`; `Example Stable Value Fund 2,000.00 10.00 0.00 0.00 0.00 2,010.00`), `Total $30,000.00 $390.00 $1,800.00 $0.00 $0.00 $32,190.00`, a `Vested Balance $31,000.00` line, and with `by_source` a second table by contribution source with the same totals.

- [ ] **Step 1: Failing tests:** unit: `test_plan_statement_is_detected_and_read` (parser `retirement_plan_statement`; `total_value.amount == Decimal("32190.00")`; period 2026-01-01 to 2026-03-31; `account.account_type == "retirement"`; checks `["roll_forward", "roll_forward_rows"]`, status `ok`; no positions, no activity; figures: opening 30,000.00, closing 32,190.00); `test_tampered_total_does_not_reconcile`; `test_table_by_source_is_the_same_money`; `test_second_plan_reads_nothing` (warning `plan_statement_several_accounts`); `test_brokerage_and_bank_fixtures_score_zero` (the parser's `detect` is 0 for the placeholder brokerage PDF, the Fidelity fixture and a bank statement). Integration: `test_plan_statement_records_the_balance_and_no_holdings` (a retirement account exists and is the document's account: snapshots of 32,190.00 on 2026-03-31 and 30,000.00 on 2025-12-31; no `holding`, `lot`, `investment_txn` or ledger row; document `processed`); `test_plan_statement_that_does_not_add_up_records_nothing` (item `plan_statement_does_not_add_up`, no snapshot). vitest: the reason is in `REASONS`.
- [ ] **Step 2:** Run; expect `no_parser` and `no_records`.
- [ ] **Step 3: Implement**, then add the INGESTION.md paragraph ("Retirement plan statements": the decision and its limits) and the changelog entry.
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_plan_statement.py tests/unit/test_brokerage_parser.py tests/unit/test_fidelity_parser.py tests/unit/test_statement_parsers.py tests/unit/test_unseen_layouts.py tests/unit/test_statement_opening_value.py tests/integration/test_brokerage_pipeline.py tests/integration/test_multi_account_statements.py tests/security/test_parser_trust.py`, `npm test -- reviewCopy`, and the gates of Task 20 Step 2. Expected: pass.
- [ ] **Step 5:** Commit `feat(ingestion): record the balance of a 401(k) plan statement`.

### Task 22: The measurement (final phase, always last)

Not run by the plan's author; the commands are in section 12 ("How to run it again") of the corpus baseline report kept with the corpus results (`reports/corpus-baseline.md` in the session scratchpad). The samples stay outside the repository.

- [ ] **Step 1:** Move the earlier `results.jsonl` aside (the supervisor appends). Re-run the harness on the whole work list with the section 12 commands (`sync.sh`, `setup_db.py create`, `run.py --worklist ... --workers 2 --timeout 90`, copy `results.jsonl` back, `summarize.py ... --derived derived.jsonl`).
- [ ] **Step 2:** Diff `derived.jsonl` against the baseline's on `file`, `primary`, `rows`, `verified`, and record per file: parser chosen, rows read, review reasons, before and after. **No file may lose rows it read correctly before.** A file whose row count falls is examined one by one; the only accepted cases are rows that were wrong before (a year-to-date figure stored as a loan payment, a bill filed as a receipt, a total line counted as a row) and each is named.
- [ ] **Step 3:** Check the files the evidence names for this package and say plainly which did not improve: the broker activity exports (rows read, fees), the investment OFX files held for a cent, a bond or an option, the two positions exports, the six periodic mortgage statements and the two loan bills, the six payslips, the composite 1099 and the SSA-1099, the two receipts with a lost date or a swapped split, the unreadable photos (their reason), and, when Task 21 was done, the plan statements. Expected to stay as they are: files whose own figures disagree, the IBKR Flex exports and PDF, QIF investment sections, the sectioned and two-table CSVs, the equity-award export, the student bill with several loans (recognised, not read).
- [ ] **Step 4:** Put the per-file table and the summary in the pull request body. No commit.

---

## Out of scope (named so nobody starts them here)

IBKR Flex XML and CSV and the IBKR PDF layout (C12, R3); QIF investment sections (C18); several tables in one delimited file (C11: the extractor returns one table and the importer reads `tables[0]` in many places; not small); short option positions in the lot engine (E6); the Schwab equity-award export (R6); reading a bill of several student loans; `fund` and `category` column aliases and sign normalisation for fund ledgers (C10 b and c: 1a Task 9 already keeps such a table out of the cash ledger); holding only the failing positions of an OFX statement; stock splits printed as shares added (S2); the classifier's kind model (no retraining here: every parser in this plan is chosen by its own `detect`).

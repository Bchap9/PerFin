# Import 1a: never wrong silently, open what is valid. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Structured files (CSV, TSV, XLSX, OFX, QFX, QIF) never import wrong data unnoticed, and valid files that are refused or crash today are read.

**Architecture:** Fixes stay inside the existing stages: the sniffer (`app/ingestion/intake/sniff.py`), the sandboxed extractors (`app/ingestion/extract/`), the deterministic mapping suggestion (`app/ingestion/parsers/structured.py`) and the structured importer (`app/domain/imports/service.py`). No new tables, no API change. Every behaviour is pinned by a synthetic fixture first.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, PyMuPDF, tesserocr, pytest. Run everything in the Linux dev container (`uv run ...` from `backend/`).

**Spec:** `docs/superpowers/specs/2026-11-12-import-reliability-design.md` (section "1a").

## Global Constraints

- Test first: write the failing test, run it and see it fail for the stated reason, then implement.
- Fixtures are synthetic and invented. Never copy a real sample's rows, names or numbers into the repository.
- Every new `.py` file starts with `# SPDX-License-Identifier: AGPL-3.0-or-later`.
- Gates on the files you touch before each commit, from `backend/`: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and the tests named in the task.
- No test may be skipped or marked xfail (docs/DEVELOPMENT.md).
- The sandbox contract does not change: parsers and the importer read validated extraction output only; nothing new is allowed in the seccomp filter or Landlock rules.
- The sniffer is a security boundary: every existing reject test in `tests/unit/test_sniff.py` must keep passing unchanged, and the whole-file markup scan and control-byte rules stay.
- Conventional commits (`fix(ingestion): ...`), one commit per task, ending with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`. Stage only the files of your task (`git add <paths>`), never `git add -A`.
- A held file is better than a wrong import. When a rule cannot prove a reading, it holds the file with a named doubt; it never guesses.
- Review reasons and doubts are stable strings used by the frontend copy (`frontend/src/features/review/reviewCopy.ts`); a new one needs a copy entry there and a vitest assertion.

## Review Focus

1. **A card export with every amount positive** (Amex, Discover, Apple Card style) into an empty household: the person expects purchases to be spending. No test may show them imported as income; the file is held with `sign_unverified` unless a column proves the sign.
2. **A bank export that prints the same transaction on two physical lines** (description continuation) or with commas inside quoted cells: expected to be read as CSV, not refused as plain text, and the continuation kept in the memo.
3. **An OFX file from a bank that loses rows in parsing**: the person expects to be told how many records were not read. The document must not end `processed` with fewer rows than the file has records.
4. **An aggregator export mixing a checking account and a card in one `Account` column**: expected to be split per account, never merged; but a `Payee`, `Category` or other text column must not be taken for accounts.
5. **A date column where every day is 12 or less**: a file of one month's days imports with the right month; a two-row file and a file of the same day in several months stay held.

Each has its test in the owning task below (Tasks 9, 5, 3 and 4, 10, 12).

---

## Lanes

Tasks are grouped by the files they own so lanes can run side by side in one
checkout. A lane never edits another lane's files during the parallel phase.

| Lane | Tasks | Owns |
|---|---|---|
| A | 1, 2, 3, 13 | `tests/security/mcp/test_mcp_server.py`, `app/ingestion/extract/ofx.py`, `qif.py`, `ofx_invest.py`, `app/domain/investments/statement_import.py`, their tests, `tests/fixtures/synth_ofx_variants.py` |
| B | 5, 6, 7 | `app/ingestion/intake/sniff.py`, `app/ingestion/extract/common.py`, `delimited.py`, `pdf.py`, their tests, `tests/fixtures/synth_sniff_cases.py`, `tests/fixtures/synth_pdf_damaged.py` |
| C | 9, 10, 11, 12, 15 | `app/ingestion/parsers/structured.py`, `app/domain/imports/service.py`, `proposals.py`, `schemas.py`, their tests, `tests/fixtures/synth_csv_layouts.py`, `frontend/src/features/review/reviewCopy.ts` |
| Final (in order) | 4, 8, 14, 16 | `app/ingestion/pipeline.py`, `app/ingestion/extract/ocr.py`, `app/ingestion/sandbox/protocol.py`, `app/domain/review/layout.py`, docs |

---

### Task 1: The MCP leak test no longer depends on a random UUID

**Files:**
- Modify: `backend/tests/security/mcp/test_mcp_server.py` (`leaky_api`, about line 277, and the digit assertions about line 322)

The test asserts that digit strings such as `742` and `5678` are absent from a response that also carries `str(uuid.uuid4())`; about one run in 175 the UUID contains one of them (it failed on `main` in CI run 43144085478).

- [ ] **Step 1:** Replace the random id with the constant `ITEM_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"` (no decimal digit that any asserted string contains) and keep `assert uuid.UUID(item["id"])`. Add `test_leak_test_id_holds_no_asserted_digits` asserting none of `("123456789012", "5678", "6789", "5551234567", "4111", "742")` is a substring of `ITEM_ID`.
- [ ] **Step 2:** Run `uv run pytest -q tests/security/mcp/test_mcp_server.py -p no:cacheprovider` 3 times. Expected: all pass each time.
- [ ] **Step 3:** Commit `test(mcp): fixed item id in the leak test so UUID digits cannot fail it`.

### Task 2: OFX files open whatever their declaration says

**Files:**
- Create: `backend/tests/fixtures/synth_ofx_variants.py`
- Modify: `backend/app/ingestion/extract/ofx.py` (`_parse` about 308-316, `parse_xml` about 180-192, entity unescape about 120-123)
- Test: `backend/tests/unit/test_ofx_variants.py` (create)

**Interfaces:**
- Produces: `synth_ofx_variants.py` builders returning `bytes`, each from an invented 3-row statement (`rows: list[tuple[date, Decimal, str]]`): `ofx2_without_xml_declaration()` (starts `<?OFX OFXHEADER="200" ...?>`), `ofx2_unknown_encoding(encoding: str)` (`USASCII`, `utf-16` on ASCII bytes), `xml_declaration_over_sgml_body()` (unclosed leaves such as `<TRNAMT>-5.50`), `sgml_double_escaped_payee()` (payee `A &amp;lt;B&amp;gt;`). Task 3 adds builders to the same module.

- [ ] **Step 1: Failing tests** in `test_ofx_variants.py`, each extracting through the registered `ofx` extractor the way `tests/unit` OFX tests already do:
  - `test_ofx2_without_xml_declaration_is_read`: 3 rows, amounts and dates equal the builder's rows.
  - `test_unknown_declared_encoding_is_read[USASCII]`, `[utf-16]`: 3 rows; no `extractor_crashed`.
  - `test_sgml_body_under_xml_declaration_is_read`: 3 rows.
  - `test_other_processing_instructions_stay_refused`: a file with `<?xml-stylesheet ...?>` or `<!DOCTYPE` after the header still fails with `markup_declaration_forbidden`.
  - `test_double_escaped_entity_is_unescaped_once_more`: payee equals `A <B>`.
- [ ] **Step 2:** Run them; expect failures `markup_declaration_forbidden`, `malformed_xml`, `extractor_crashed` (LookupError).
- [ ] **Step 3: Implement** in `ofx.py`: a leading `<?OFX` routes to the XML parser like `<?xml`; the bytes are decoded with `decode_text` and the XML declaration dropped before parsing, so a declared encoding is never trusted; on `ParseError`, `LookupError` or `ValueError` fall back to the SGML tokenizer after removing only the leading `<?xml ...?>` and `<?OFX ...?>` instructions. XXE protections (defusedxml) unchanged. Unescape entities again while the value still holds `&lt;`, `&gt;` or `&amp;` (at most twice).
- [ ] **Step 4:** Run the new file and `uv run pytest -q tests/unit -k "ofx" tests/security -k "ofx or xxe or fuzz"`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): read OFX whatever its declaration says`.

### Task 3: OFX and QIF count every record they do not read

**Files:**
- Modify: `backend/app/ingestion/extract/ofx.py` (tokenizer about 145-151, `_transaction` about 228-233, statement reading about 267-279), `qif.py` (about 241), `ofx_invest.py` (about 460), `backend/tests/fixtures/synth_ofx_variants.py`
- Test: `backend/tests/unit/test_ofx_variants.py`, `backend/tests/unit/test_qif_skips.py` (create)

**Interfaces:**
- Produces: extraction warnings of the exact form `transaction_skipped:<n>` (one per statement that skipped records, `n >= 1`), replacing today's per-record warning text for skipped records. Task 4 parses this form with `re.fullmatch(r"transaction_skipped:(\d+)", w)`.
- Produces builders: `sgml_with_valueless_leaves()` (4 transactions, each with empty `<TRNTYPE>`, `<FITID>`, `<NAME>` lines before `<MEMO>`), `sgml_with_stray_list_end()` (2 transactions with `</BANKTRANLIST>` between them), `sgml_with_unreadable_amount()` (3 transactions, one `<TRNAMT>abc`).

- [ ] **Step 1: Failing tests:**
  - `test_valueless_sgml_leaves_do_not_swallow_fields`: 4 rows read (today 0).
  - `test_transactions_outside_the_list_are_read`: 2 rows (today 1).
  - `test_unreadable_record_is_counted`: 2 rows and warnings contain exactly `transaction_skipped:1`.
  - `test_qif_record_without_amount_is_counted` (QIF with 3 records, one lacking a `T` line): 2 rows, `transaction_skipped:1`.
  - `test_aggregate_with_end_tag_is_still_an_aggregate`: `<STMTTRN>` ... `</STMTTRN>` nesting unchanged for a normal file.
- [ ] **Step 2:** Run; expect the row-count failures above.
- [ ] **Step 3: Implement:** an empty-valued SGML tag opens an aggregate only when that tag name has an end tag somewhere in the file or is in the known aggregate set; otherwise it is an empty leaf. Statement reading also takes `STMTTRN` nodes that are direct children of the statement node. Count skipped records per statement and emit one `transaction_skipped:<n>` warning (bank OFX, investment OFX, QIF).
- [ ] **Step 4:** Run `uv run pytest -q tests/unit -k "ofx or qif" tests/integration/test_structured_import.py tests/security -k "fuzz or sandbox_documents"`. Expected: pass (update any test that asserted the old warning text to the new form).
- [ ] **Step 5:** Commit `fix(ingestion): OFX and QIF count the records they could not read`.

### Task 4: A statement that lost records says so (final phase)

**Files:**
- Modify: `backend/app/domain/imports/service.py` (`_import_statements`, about 1139-1187; review raising about 807-914)
- Test: `backend/tests/integration/test_structured_import.py`
- Modify: `frontend/src/features/review/reviewCopy.ts` and its test if the payload gains a key

**Interfaces:**
- Consumes: `transaction_skipped:<n>` warnings (Task 3).

- [ ] **Step 1: Failing tests** (through the real worker path the file already uses):
  - `test_ofx_with_skipped_records_raises_lines_skipped`: `sgml_with_unreadable_amount()` uploaded to a matching account: 2 ledger rows, document status `needs_review`, one open `low_confidence_field` item with `reason == "lines_skipped"` and `payload["count"] == 1`, `payload["source"] == "extraction"`.
  - `test_statement_with_transactions_and_no_rows_is_not_processed`: a file whose every record is unreadable ends `needs_review`, never `processed`.
  - `test_clean_ofx_raises_nothing`: the existing sample statement still ends `processed` with no item.
- [ ] **Step 2:** Run; expect `processed` and zero items.
- [ ] **Step 3: Implement:** sum `transaction_skipped:<n>` over `result.warnings` in `_import_statements` and raise one `lines_skipped` item per document through the existing `_Run.review` path.
- [ ] **Step 4:** Run `uv run pytest -q tests/integration/test_structured_import.py tests/integration/test_imports_api.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(imports): a statement that lost records waits in review`.

### Task 5: The sniffer reads real-world text tables

**Files:**
- Create: `backend/tests/fixtures/synth_sniff_cases.py`
- Modify: `backend/app/ingestion/intake/sniff.py` (`_delimited_type` about 267-282, `_rows_then_notes` 213-231, `_rows_after_preamble` 240-264, OFX and QIF header tests about 152-155 and 288-296, NUL check about 182), `backend/app/ingestion/extract/common.py` (`decode_text`)
- Test: `backend/tests/unit/test_sniff.py`

**Interfaces:**
- Produces builders returning `bytes`: `csv_quoted_decimal_commas()` (6-column header; rows with `"-8,78"` and commas inside a quoted description), `csv_multiline_quoted_cell()` (semicolon file, one quoted cell over three physical lines), `csv_preamble_with_continuation_rows()` (4-line preamble; each transaction row followed by a one-cell continuation row), `csv_summary_block_then_table()` (a 5-line summary of label,value pairs, a blank line, then `Date,Description,Amount,Running Bal.` rows with quoted thousands), `ofx_header_keys_before_ofxheader()` (`ENCODING:UTF-8` first), `ofx_data_key_header()` (`DATA:OFXSGML`), `ofx_xml_declaration_without_ofx_pi()` (`<?xml ...?>` then `<OFX>`), `qif_with_leading_comments()`, `qfx_with_trailing_nul()`.

- [ ] **Step 1: Failing tests** in `test_sniff.py`: one per builder asserting the sniffed type (`text/csv`, `application/x-ofx` or the module's existing names for OFX and QIF), plus:
  - `test_nul_inside_text_is_still_binary`: a NUL anywhere but trailing padding is `binary_unknown`.
  - `test_prose_with_commas_is_still_plain_text`: three paragraphs of prose with commas stay `text/plain`.
  - `test_xml_that_is_not_ofx_is_still_refused`: `<?xml ...?><Document>` is `xml_not_ofx`.
  - `test_markup_in_accepted_csv_is_still_refused`: `csv_quoted_decimal_commas()` with `<script>` in a cell is refused as before.
- [ ] **Step 2:** Run; expect the new accept tests to fail with `text/plain`, `xml_not_ofx` or `binary_unknown`.
- [ ] **Step 3: Implement:** count delimiters outside double quotes over logical records (physical lines joined while a quote is open) before the three existing shape tests; in `_rows_after_preamble` accept a run where shorter rows alternate with rows of the table's shape; accept OFX when `<OFX>` is in the sniff window and everything before it is `KEY:VALUE` lines, and under `<?xml` when the root is `<OFX>`; skip leading `#` lines before the QIF header test; ignore NUL bytes only when they are trailing padding (sniff and `decode_text`).
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_sniff.py tests/security -k "sniff or upload or fuzz"`. Expected: pass, with every pre-existing test unchanged.
- [ ] **Step 5:** Commit `fix(ingestion): sniff quoted, prefaced and oddly headed text files as what they are`.

### Task 6: The delimited reader tolerates stray blanks and single quotes

**Files:**
- Modify: `backend/app/ingestion/extract/delimited.py` (about 56-107)
- Test: `backend/tests/unit/test_delimited_reader.py` (create or extend the existing delimited test module if one exists under `tests/unit`)

- [ ] **Step 1: Failing tests:** `test_blank_after_closing_quote_is_read` (fully quoted 5-column file whose last line ends `"12.50" ` with no newline: 5 rows, last cell `12.50`); `test_single_quoted_cells_are_unquoted` (`'01/08/2008';'CARD 1';'9.3'`: first cell `01/08/2008`); `test_apostrophe_in_text_is_not_a_quote` (`O'Brien Hardware` in an unquoted cell stays intact).
- [ ] **Step 2:** Run; expect `no_delimiter` and quoted cell text.
- [ ] **Step 3: Implement:** strip spaces and tabs between a closing double quote and the delimiter or line end before the strict parse; when most sampled cells start and end with `'`, read with `quotechar="'"`. The formula guard (leading `'` neutralisation, SECURITY.md C7) must still apply to cell values after unquoting.
- [ ] **Step 4:** Run `uv run pytest -q tests/unit -k "delimited or csv or formula"`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): read CSV cells with trailing blanks or single quotes`.

### Task 7: A PDF with one broken cross-reference entry is read

**Files:**
- Create: `backend/tests/fixtures/synth_pdf_damaged.py`
- Modify: `backend/app/ingestion/extract/pdf.py` (`_actions` about 116-123 and 147; scrub about 422-453)
- Test: `backend/tests/unit/test_pdf_damaged.py` (create); `backend/tests/security/test_sandbox_documents.py` if it lists extractor cases

**Interfaces:**
- Produces: `damaged_xref_statement_pdf(javascript: bool) -> bytes`: a one-page reportlab statement whose bytes are post-processed so one cross-reference entry points at a missing object (and, when `javascript`, a document-level `/OpenAction` JavaScript is present). The builder asserts in its own body that PyMuPDF's `xref_object` raises for that entry, so the fixture cannot silently stop exercising the bug.

- [ ] **Step 1: Failing tests:** `test_pdf_with_broken_xref_entry_is_extracted` (result `ok`, page text contains the statement's header line); `test_javascript_is_still_removed_from_damaged_pdf` (`meta` counts 1 JavaScript action and the saved in-memory document has none: assert through the same inventory call the existing active-content tests use); `test_truly_unreadable_pdf_is_still_malformed` (random bytes with a `%PDF-` header).
- [ ] **Step 2:** Run; expect `malformed_pdf` for the first two.
- [ ] **Step 3: Implement:** catch PyMuPDF's base error class per object in `_actions`; make the scrub step robust to the same object (disarm what can be loaded, keep the module's own action-clearing loop as the authority). Do not weaken removal: if active content cannot be inventoried *and* cannot be cleared, the document stays refused.
- [ ] **Step 4:** Run `uv run pytest -q tests/unit -k "pdf" tests/security -k "pdf or sandbox_documents or fuzz"`. Expected: pass.
- [ ] **Step 5:** Commit `fix(ingestion): read PDFs with an unreadable cross-reference entry; active content still removed`.

### Task 8: An OCR engine crash does not lose the document (final phase)

**Files:**
- Modify: `backend/app/ingestion/sandbox/protocol.py` (`ExtractOptions`), `backend/app/ingestion/extract/ocr.py` (about 231-235), `backend/app/ingestion/pipeline.py` (extraction failure handling about 916-930)
- Test: `backend/tests/unit/test_ocr_options.py` (create), `backend/tests/integration/test_document_extraction_pipeline.py`

**Interfaces:**
- Produces: `ExtractOptions.ocr_psm: Literal["auto", "single_block"] = "auto"`; `ExtractOptions.ocr: bool` already exists (verify; add if not). Pipeline retry order after a sandbox failure with code `crashed` on a PDF or image: (1) same options with `ocr_psm="single_block"`, (2) `ocr=False`. A document read on a retry carries the extraction warning `ocr_retried:single_block` or `ocr_disabled_after_crash`.

- [ ] **Step 1: Failing tests:**
  - Unit: `test_single_block_mode_selects_psm_6` (the OCR call receives Tesseract page-segmentation mode 6 when `ocr_psm == "single_block"`, mode 3 otherwise; patch the engine constructor).
  - Integration, with the pipeline's extraction dependency replaced by a stub that raises the sandbox `crashed` error for `ocr_psm == "auto"` and returns a result otherwise: `test_ocr_crash_is_retried_with_single_block` (document not `failed`; warning `ocr_retried:single_block`); `test_second_crash_falls_back_to_no_ocr` (warning `ocr_disabled_after_crash`; an open review item tells the person the scan could not be read by OCR); `test_other_sandbox_errors_are_not_retried` (`memory_limit` fails as today).
- [ ] **Step 2:** Run; expect `failed` documents.
- [ ] **Step 3: Implement** as the interface says. Each retry is a new sandbox child; the filter and limits are unchanged.
- [ ] **Step 4:** Run `uv run pytest -q tests/unit -k "ocr" tests/integration/test_document_extraction_pipeline.py tests/security/test_sandbox.py tests/security/test_sandbox_documents.py`. Expected: pass (apart from `test_memory_bomb_is_stopped_by_rlimit_as`, which fails on arm64 before this change; report its result separately).
- [ ] **Step 5:** Commit `fix(ingestion): retry OCR in single-block mode after an engine crash`.

### Task 9: The sign of a file is proven, or the file waits

**Files:**
- Create: `backend/tests/fixtures/synth_csv_layouts.py`
- Modify: `backend/app/ingestion/parsers/structured.py` (`suggest_mapping` about 1126-1202, `_candidate` about 1016-1045, indicator detection about 692-701 and 908-912, `_row_amount` about 400), `backend/app/domain/imports/proposals.py` (`_type` about 309-314), `backend/app/domain/imports/service.py` (`_sign_held` about 1305-1318), `frontend/src/features/review/reviewCopy.ts`
- Test: `backend/tests/unit/test_csv_suggestions.py`, `backend/tests/integration/test_structured_import.py`, the vitest file beside `reviewCopy.ts`

**Interfaces:**
- Produces doubts (strings in `MappingSuggestion.doubts`): `sign_unverified` (no evidence for the sign and the table is card-like or one-signed), `investment_columns` (the table has a symbol or ticker column plus a quantity and a price column and is not read as brokerage activity), `indicator_contradicts_sign` (direction words and signs disagree row to row).
- Produces `sign_evidence` values in addition to today's: `"indicator_correlation"`.
- Produces builders in `synth_csv_layouts.py` returning `bytes`: `card_positive_purchases()` (`Date,Description,Amount`; 8 purchases positive, 1 payment negative, no other column), `card_with_type_column()` (same plus `Type` = `Purchase`/`Payment`), `aggregator_debit_positive()` (`Date,Description,Amount,Transaction Type`; `debit` rows positive, `credit` rows negative), `unsigned_with_indicator_header_unknown()` (`Amount` unsigned beside `Credit Debit Indicator` = `Debit`/`Credit`), `lone_inflow_column_of_negatives()` (`Date;Payee;Inflow` with negative cells), `trades_list()` (`date,ticker,qty,price,amount,name`), `fund_transactions()` (`Date,Fund,Transaction Type,Shares,Price,Amount` with `Buy`/`Sell`), `checking_with_running_balance()` (signed amounts and a balance that reconciles).

- [ ] **Step 1: Failing unit tests** in `test_csv_suggestions.py`:
  - `test_card_like_table_without_evidence_is_not_confident`: `card_positive_purchases()` gives doubts containing `sign_unverified`.
  - `test_type_words_prove_purchases_positive`: `card_with_type_column()` is confident with `sign == "outflow_positive"` and `sign_evidence == "indicator_correlation"`.
  - `test_direction_words_beside_signed_amounts_are_evidence`: `aggregator_debit_positive()` is confident, `outflow_positive`.
  - `test_indicator_is_found_by_content`: `unsigned_with_indicator_header_unknown()` maps the indicator column; a debit row reads negative.
  - `test_mixed_words_and_signs_are_a_doubt`: half the `debit` rows negative gives `indicator_contradicts_sign`.
  - `test_lone_debit_or_credit_column_keeps_its_sign`: `lone_inflow_column_of_negatives()` rows read negative.
  - `test_both_columns_negative_value_is_the_opposite_column`: a negative cell in `Debit` with `Credit` also mapped reads as a credit.
  - `test_trades_and_fund_tables_are_never_cash`: `trades_list()` and `fund_transactions()` give doubts containing `investment_columns` (unless the brokerage activity reader recognises them, in which case assert they are read as activity).
  - `test_balance_proven_checking_stays_confident`: `checking_with_running_balance()` unchanged.
- [ ] **Step 2: Failing integration tests:** `test_card_export_is_held_not_imported_as_income` (upload `card_positive_purchases()` into an empty household, accept proposed accounts the way `proposals` tests do: zero ledger rows, an open item whose payload names `sign_unverified`); `test_proposed_account_type_is_not_checking_when_sign_unproven` (the proposal's type is `credit_card` when a header names a card, else absent).
- [ ] **Step 3:** Run both; expect confident suggestions and positive rows.
- [ ] **Step 4: Implement.** Card-like header words: `card no`, `card number`, `card used`, `member name`, `merchant`, `post date` beside `transaction date`, `statement balance`. Outflow words: `debit`, `dr`, `purchase`, `sale`, `fee`, `withdrawal`, `payment sent`; inflow words: `credit`, `cr`, `payment`, `refund`, `return`, `deposit`. Correlation needs at least 4 rows, both directions present and at least 95% agreement; below that it is no evidence, and a contradiction above 5% is the doubt. The mapping editor is the answer to every doubt; add copy for the three doubts to `reviewCopy.ts` with a vitest assertion each.
- [ ] **Step 5:** Run `uv run pytest -q tests/unit/test_csv_suggestions.py tests/unit/test_suggested_vocabulary.py tests/integration/test_structured_import.py tests/integration/test_imports_api.py tests/integration/test_activity_table_import.py` and `npm test -- reviewCopy` in `frontend/` (Node 22). Expected: pass.
- [ ] **Step 6:** Commit `fix(imports): a file's sign is proven or the file waits`.

### Task 10: An account column of names splits the file

**Files:**
- Modify: `backend/app/ingestion/parsers/structured.py` (`_looks_like_account` about 880-887, `_role_fit` about 913-916, grouping about 545-593), `backend/tests/fixtures/synth_csv_layouts.py`
- Test: `backend/tests/unit/test_csv_suggestions.py`, `backend/tests/integration/test_structured_import.py`

**Interfaces:**
- Produces builders: `aggregator_two_accounts()` (`Date,Account,Description,Amount`; `Everyday Checking` and `Rewards Card` rows interleaved), `export_with_unique_account_cells()` (an `Account` column with a different value on every row), `export_with_payee_and_category()` (no account column; `Payee` with 3 repeated values).
- Statements grouped by a name carry `account_name` and no last four; each raises its own `unknown_account` item (existing behaviour for nameless groups).

- [ ] **Step 1: Failing tests:** `test_account_name_column_groups_rows` (2 statements, 5 and 4 rows); `test_unique_account_cells_do_not_split` (1 statement); `test_payee_column_is_never_an_account_column` (1 statement); integration `test_aggregator_export_asks_for_each_account` (two `unknown_account` items, zero rows committed before they are answered).
- [ ] **Step 2:** Run; expect one statement holding all rows.
- [ ] **Step 3: Implement:** a text column takes the account role only when its header is exactly one of `account`, `account name`, `source account`, it has between 2 and 20 distinct values, and every value appears at least twice. Group by case-folded, whitespace-collapsed name.
- [ ] **Step 4:** Run the two test files plus `tests/integration/test_imports_api.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(imports): split an export by its account name column`.

### Task 11: Words, shapes and rows that are not transactions

**Files:**
- Modify: `backend/app/ingestion/parsers/structured.py` (`ROLE_WORDS` about 646-745, `_UNIT_SUFFIX` 771, `_TIME_SUFFIX` 262-264, `_read_rows` 457-486, `_reconcile` 613-621, `parse_cell_amount` 319-360, `_data_rows` 994-996), `backend/app/domain/imports/schemas.py` (`DATE_FORMATS` about 85-101), `backend/app/domain/imports/service.py` (`rows_skipped` about 807-818), `backend/tests/fixtures/synth_csv_layouts.py`
- Test: `backend/tests/unit/test_suggested_vocabulary.py`, `backend/tests/unit/test_csv_suggestions.py`

- [ ] **Step 1: Failing tests** (builders added to `synth_csv_layouts.py`, one per case):
  - Vocabulary: `created`, `datetime`, `timestamp`, `date processed` are date headers; `gross` is the amount when `Gross`, `Fee`, `Net` are all present; `Credit Amount` / `Debit Amount` are the credit and debit columns; `Amount (total)` is exact for amount (a parenthesised qualifier is a unit suffix); a timestamp `2026-02-25 12:34:56 +0000` parses.
  - Rows: a one-cell continuation row directly after a transaction row is appended to that row's memo (`csv_preamble_with_continuation_rows()` layout: the merchant text survives); a first row with a name and `$0` only is a note row; a short trailing footer does not lower the date column's fit below the threshold; rows after the last readable row are footers and raise no `rows_skipped`; a date cell `Pending` gives a pending row that is not imported and is counted (`pending_rows`).
  - Shapes: `771,65111111111` rounds to 6 places instead of failing the table; an amount with leading currency letters and direction marks parses; `31Dec2004` and `10-Nov-09` parse.
  - Running balance printed on the last row of each day reconciles (the amounts since the previous balance are summed).
- [ ] **Step 2:** Run; expect `no suggestion` / `header_not_exact` / `rows_unreadable` failures.
- [ ] **Step 3: Implement** each rule where the file list says. The fit check (`_role_fit`) keeps guarding every new word.
- [ ] **Step 4:** Run `uv run pytest -q tests/unit/test_suggested_vocabulary.py tests/unit/test_csv_suggestions.py tests/unit/test_structured_import.py tests/integration/test_structured_import.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(imports): more header words, date and amount shapes, and note rows in exports`.

### Task 12: A day/month tie that the file itself resolves is no guess

**Files:**
- Modify: `backend/app/ingestion/parsers/structured.py` (`best_date_format` about 860-877, doubt about 1180)
- Test: `backend/tests/unit/test_csv_suggestions.py`

Lands only after Tasks 9 and 10: the date doubt is what holds several unsafe files today.

- [ ] **Step 1: Failing tests:**
  - `test_same_day_under_both_orders_is_no_guess`: dates `1/1/2026`, `2/2/2026`, `3/3/2026` give no `date_order_guessed`.
  - `test_constant_component_is_the_month`: `06/01/2026` ... `06/10/2026` (8 distinct days, first component constant) reads month-first with no doubt.
  - `test_two_rows_stay_held`: two rows keep `date_order_guessed`.
  - `test_same_day_of_several_months_stays_held`: `01/05`, `02/05`, `03/05` (second component constant, first takes three values) is read with the first as month only if the rule's minimum holds; assert that a file where *both* components take fewer than three distinct values keeps the doubt.
  - `test_unambiguous_second_date_column_decides`: a `Post Date` column with a day above 12 settles the order for `Transaction Date`.
  - `test_reading_that_dates_rows_after_upload_is_rejected`: with `today` injected, the order that puts a row in the future loses.
  - `test_card_file_is_still_held_for_its_sign`: `card_positive_purchases()` with month-proven dates still carries `sign_unverified` (Task 9).
- [ ] **Step 2:** Run; expect `date_order_guessed` on the first, second, fifth and sixth.
- [ ] **Step 3: Implement** the four rules; the constant-component rule requires the other component to take at least 3 distinct values.
- [ ] **Step 4:** Run the suggestion and structured-import test files. Expected: pass.
- [ ] **Step 5:** Commit `fix(imports): a day/month order the file proves is not a guess`.

### Task 13: Two positions of one security do not crash the import

**Files:**
- Modify: `backend/app/domain/investments/statement_import.py` (`_record_price` about 1185-1214, flush about 2125), `backend/app/ingestion/extract/ofx_invest.py` (about 478), `backend/tests/fixtures/synth_brokerage.py`
- Test: `backend/tests/integration/test_brokerage_pipeline.py`

- [ ] **Step 1: Failing tests:** `test_same_security_listed_twice_imports` (an OFX `INVPOSLIST` naming one fund twice on the same day at 100.00 and 100.42: the job succeeds, one `price` row for that day, the holding's units are the sum); `test_inv401k_account_is_typed_retirement` (an `INVSTMTRS` carrying `INV401K` proposes or matches a retirement account).
- [ ] **Step 2:** Run; expect `IntegrityError` (`uq_price_security_date`) and `brokerage`.
- [ ] **Step 3: Implement:** remember the (security, day) pairs recorded in the run; the first price printed wins and a differing second one adds the parse warning `duplicate_price_ignored`. Read `INV401K`.
- [ ] **Step 4:** Run `uv run pytest -q tests/integration/test_brokerage_pipeline.py tests/integration/test_investments_api.py`. Expected: pass.
- [ ] **Step 5:** Commit `fix(investments): a security listed twice in one statement no longer fails the import`.

### Task 14: "Nothing to import" is said as such (final phase)

**Files:**
- Modify: `backend/app/domain/imports/service.py` (about 1260-1337 and 1851), `backend/app/domain/review/layout.py` (about 126-142), `backend/app/domain/review/service.py` (`MAPPING_REASONS` about 150), `frontend/src/features/review/reviewCopy.ts`, `frontend/src/features/imports/UploadPage.tsx` (the "Map columns" link about 269-275)
- Test: `backend/tests/integration/test_structured_import.py`, `backend/tests/integration/test_imports_api.py`, the vitest files beside the two frontend files

**Interfaces:**
- Produces `unparsed` reasons: `no_rows` (a table with a header and no data rows; a document whose pages hold no transactions) and keeps `mapping_required` for tables that have rows. `activity_unreadable` joins `MAPPING_REASONS`.

- [ ] **Step 1: Failing tests:** `test_header_only_export_says_no_rows` (reason `no_rows`, not `mapping_required` or `format_not_supported`); `test_activity_unreadable_offers_the_mapping_editor`; frontend: the upload list shows "Map columns" only for reasons in the mapping set, and `no_rows` renders its own sentence ("This file has no rows to import.").
- [ ] **Step 2:** Run; expect `mapping_required`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Run the named backend files and `npm test` for the two frontend test files. Expected: pass.
- [ ] **Step 5:** Commit `fix(review): say when a file has nothing to import; offer the mapping editor where it works`.

### Task 15: A table without a header gets a suggestion

**Files:**
- Modify: `backend/app/ingestion/parsers/structured.py` (`_assign` about 937-944, `_candidate` 1016-1017, `suggest_mapping`), `backend/tests/fixtures/synth_csv_layouts.py`
- Test: `backend/tests/unit/test_csv_suggestions.py`, `backend/tests/integration/test_structured_import.py`

**Interfaces:**
- Produces: `MappingSuggestion.has_header == False` suggestions built from column shapes, with the doubt `no_header` unless a running balance proves both the columns and the sign, or an indicator column of direction words decides the sign.
- Produces builders: `headerless_five_quoted_columns()` (`"12/04/2025","-12.07","*","","PURCHASE ..."`), `headerless_with_balance()` (`date, check no, description, debit, credit, balance` that reconciles), `headerless_with_direction_cell()` (`date, description, category, amount, debit|credit, account`).

- [ ] **Step 1: Failing tests:** `test_headerless_table_with_reconciling_balance_is_confident` (roles date, debit, credit, balance; no doubts); `test_headerless_signed_amount_is_suggested_not_confident` (roles date, amount, description; doubts `["no_header"]`); `test_headerless_with_direction_cell_uses_it`; `test_one_row_file_gets_no_suggestion`; integration `test_headerless_export_opens_a_prefilled_mapping` (the review item's payload carries the suggestion).
- [ ] **Step 2:** Run; expect `suggestion is None`.
- [ ] **Step 3: Implement:** when no header candidate is found, the date column is the first column whose cells all read in one date format; money columns are those whose cells all fit money; with three money columns try (debit, credit, balance) and (amount, balance) and keep the assignment whose running balance reconciles; the description is the longest-text column that is not a date, money or direction column.
- [ ] **Step 4:** Run the suggestion and structured-import files. Expected: pass.
- [ ] **Step 5:** Commit `feat(imports): suggest a mapping for tables without a header row`.

### Task 16: Documentation and the measurement (final phase)

**Files:**
- Modify: `docs/INGESTION.md` (the sniffer, OFX, CSV suggestion and OCR passages this plan changes; fix the drift at about 1789-1790, 1816-1817, 1899 and 1988), `CHANGELOG.md` (`## [Unreleased]`: Fixed and Changed entries, one per task, in the file's voice), `docs/security/CONTROLS.md` only if a cited test name moved.

- [ ] **Step 1:** Update the documents; `mkdocs build --strict` passes (docs requirements in `requirements-docs.txt`).
- [ ] **Step 2:** Full gates: `make check` equivalents in the container (`uv run ruff check .`, `ruff format --check .`, `mypy`, `bandit`, `pytest -n 3`, OpenAPI drift) and the frontend (`npm run lint`, `typecheck`, `test`, `build`).
- [ ] **Step 3:** Re-run the corpus harness and record, per file, the before and after of: sniffed type, rows read, review reasons. No file may lose rows it read correctly before. The summary goes into the pull request body.
- [ ] **Step 4:** Commit `docs(ingestion): the structured-import fixes of 1a`.

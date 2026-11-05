# Contributing a statement parser

PerFin reads PDF and scanned statements with small, deterministic parsers
(no language model of any kind, ADR-0007). This page is the checklist for
adding a layout. The design is in [INGESTION.md](INGESTION.md) ("Parse",
"Parser contribution model").

## Ground rules

- **Synthetic fixtures only.** Never commit a real statement, a redacted real
  statement, or text copied from one. Layouts are re-drawn by code from
  invented rows (`backend/tests/fixtures/synth_pdf_statements.py`, reportlab).
  Institution names in tests are fictional (the shipped fingerprints use
  "Sample National Bank", "Demo Card Services" and similar names).
- **Parsers read pages, not files.** A parser receives the validated sandbox
  output (`Page.words`/`lines` with boxes, from the PDF text layer or OCR) as
  a `DocumentView`. It must not import `app.ingestion.extract`, open files,
  use the network or run regular expressions that can backtrack (anchored,
  bounded patterns on lines cut to 2000 characters, as in
  `app/ingestion/parsers/text.py`).
- **Never keep more than the last four digits** of an account number.
- **Every field carries a confidence.** A parser does not decide what is
  imported; the importer holds anything below the threshold for review.

## Steps

1. **Fingerprint** (`backend/app/ingestion/classify/fingerprints/<key>.yaml`):
   the classifier recognises the institution and names a generic parser to
   fall back on (`parser: statement_generic` or `card_statement_generic`)
   and the date order. See INGESTION.md "Classify" for the schema.
2. **Fixture**: a function in `synth_pdf_statements.py` that draws the
   layout from a `SynthStatement`, plus a statement factory. Include what
   makes the layout hard: several pages with the header repeated, wrapped
   descriptions, pending items, fees and interest, debit/credit columns,
   yearless dates. Support `tamper="extra_row"` and `tamper="closing"`.
3. **Parser**: in `backend/app/ingestion/parsers/institutions.py`, a
   subclass of `TableStatementParser` (or `CardStatementParser`) that only
   declares facts:

   | Attribute | Meaning |
   | --- | --- |
   | `name` | unique, `[a-z0-9_]`, stable forever |
   | `version` | bump whenever output for the same pages may change |
   | `institution` | the fingerprint `key` (the classifier's hint) |
   | `display_name` | the institution name accounts carry (account matching) |
   | `fingerprints` | phrases of page 1 for detection without a classification |
   | `date_styles`, `period_styles` | `mdy`, `dmy`, `ymd`, `d_mon_y`, `mon_d_y`, `md`, `dm` |
   | `sign` | `bank` or `card` |
   | `default_currency`, `account_type` | when the statement does not print them |
   | `headers`, `opening_labels`, `closing_labels`, `period_labels`, `sections`, `pending_markers`, `skip_prefixes` | extend the generic vocabularies when the layout uses other words |
   | `base` | 1.0 for an institution parser (its format is declared) |

   Add the class to `INSTITUTION_PARSERS`. Write code (override
   `_collect`, `_signed`, ...) only when the layout really needs it, and
   keep it in the subclass.
4. **Tests**: add the layout to `LAYOUTS` in
   `backend/tests/unit/test_statement_parsers.py`. The shared tests then
   require exact rows, balances, period, last four digits, currency, full
   confidence and a reconciled statement, both with and without the
   classifier's hint, and require the tampered variants not to reconcile.
   Add an end-to-end case to
   `backend/tests/integration/test_pdf_statement_pipeline.py` for a new kind
   of layout.
5. **Docs**: mention the institution in INGESTION.md's institution parser
   list and add a CHANGELOG entry.

## Brokerage layouts

A brokerage statement (holdings and activity) is a subclass of
`BrokerageStatementParser` in `backend/app/ingestion/parsers/brokerage.py`,
listed in `BROKERAGE_PARSERS` (like `PlaceholderBrokerageParser`): `name`,
`version`, `institution`, `display_name`, `fingerprints` (the institution's
name first: detection requires it), `date_styles`, `period_styles`,
`default_currency`, `base`, and when the layout uses other words
`summary_labels`, `period_labels` or the module's `HOLDING_HEADERS`,
`ACTIVITY_HEADERS` and `ACTIVITY_TYPES`. The fingerprint names
`parser: brokerage_statement_generic`. Draw the layout from a
`SynthBrokerage` in `backend/tests/fixtures/synth_brokerage.py` (several
pages of activity, a reinvestment, cash in and out, the three `tamper`
variants) and extend `tests/unit/test_brokerage_parser.py` and
`tests/integration/test_brokerage_pipeline.py`: exact activity and
positions, a reconciled statement at full confidence, tampered variants
held.

## CSV and TSV exports

Delimited exports need no parser. The generic path reads them with a column
mapping (`MappingSpec`), and `suggest_mapping` in
`backend/app/ingestion/parsers/structured.py` finds one from the header
names and the cells (INGESTION.md "Match and deduplicate"). When an export
is not suggested well:

- Add its header words to `ROLE_WORDS` (exact words score best; a word
  that also names other things, like "value", stays exact only for that
  role) rather than special-casing an institution. Cells must still fit
  the role, so a header word alone never maps an empty or unrelated column.
- Add a synthetic layout: a `Layout` in `LAYOUTS`
  (`backend/tests/fixtures/synth_statements.py`) for a one-account export,
  or a writer like `backend/tests/fixtures/synth_activity_csv.py` for a
  multi-account one (blank lines, footers and disclaimers included, every
  value invented). `test_every_synthetic_layout_is_suggested_exactly_but_the_sign`
  (`tests/unit/test_csv_suggestions.py`) then requires the exact spec and
  a confident suggestion, and `test_confident_layouts_import_on_their_own_and_save_the_mapping`
  (`tests/integration/test_structured_import.py`) the end-to-end import.
- A file listing several accounts maps its account-number column
  (`account_column`); only the last four digits of the cell are kept.
- Investment activity (buys, sells, reinvestments as positions) is not read
  by this path; it only writes the cash ledger.

## Review checklist

- [ ] Fixture is generated by code; no real names, numbers or text.
- [ ] `name` unique; `version` bumped if an existing parser changed.
- [ ] No new import of `app.ingestion.extract`; no I/O; patterns bounded.
- [ ] Only the last four digits of an account number are kept.
- [ ] Unit tests pass with and without the hint; tampered variants fail
      reconciliation.
- [ ] `uv run ruff check`, `ruff format --check`, `mypy`, `bandit` and
      `pytest -n auto` pass.

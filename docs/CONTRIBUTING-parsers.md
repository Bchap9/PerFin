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
  "Sample National Bank", "Demo Card Services" and similar names). A
  parser for a real institution's layout (like the Fidelity Investments
  one) prints the institution's name, its headings and column names only
  because the layout is recognised by them; everything else in its fixture
  (accounts, numbers, instruments, amounts) is invented and every page
  says it is a synthetic test document.
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
listed in `BROKERAGE_PARSERS` ahead of the generic parser (like
`PlaceholderBrokerageParser` and `FidelityStatementParser`): `name`,
`version`, `institution`, `display_name`, `fingerprints` (the institution's
name first: detection requires it; `all_fingerprints = True` requires every
phrase, for a name other documents print too), `date_styles`,
`period_styles`, `default_currency`, `base`, and when the layout needs it:

| Attribute | Meaning |
| --- | --- |
| `summary_labels`, `period_labels`, `skip_prefixes` | other words for the summary values, the period, lines to skip |
| `description_types` | an activity table without an action column: its description is the action |
| `cash_groups` | holdings groups whose rows are the account's cash (a core or sweep money market): opening/closing cash, no position, included in the printed holdings total |
| `ignored_tables`, `section_headings` | headings of tables not to read (they would count money twice), and the headings that end them |

New column names go into the module's `HOLDING_HEADERS` (role `other` for a
column to recognise but not read) and `ACTIVITY_HEADERS`, asset-class group
headings into `ASSET_GROUPS`, action words into `ACTIVITY_TYPES` (or
`TRANSFER_TYPES` for a word whose type depends on whether units moved).
Several accounts in one PDF need no code: account headings at the top or
bottom of each account's pages split the document (INGESTION.md "Several
accounts in one PDF"). The fingerprint names
`parser: brokerage_statement_generic`. Draw the layout from invented data
(`SynthBrokerage` in `backend/tests/fixtures/synth_brokerage.py`, or a
module of its own like `synth_fidelity.py`: several pages of activity with
the header repeated, a reinvestment, cash in and out, the `tamper`
variants, several accounts if the layout has them) and extend
`tests/unit/test_brokerage_parser.py` (or a test module of its own) and
`tests/integration/test_brokerage_pipeline.py`: exact activity and
positions, a reconciled statement at full confidence per account, tampered
variants held (only the tampered account in a multi-account document), the
PDF through the sandboxed extractor (`run_extraction`).

## Review checklist

- [ ] Fixture is generated by code; no real names, numbers or text (a real
      institution's own name and headings only for its layout's parser).
- [ ] `name` unique; `version` bumped if an existing parser changed.
- [ ] No new import of `app.ingestion.extract`; no I/O; patterns bounded.
- [ ] Only the last four digits of an account number are kept.
- [ ] Unit tests pass with and without the hint; tampered variants fail
      reconciliation.
- [ ] `uv run ruff check`, `ruff format --check`, `mypy`, `bandit` and
      `pytest -n auto` pass.

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
   | `date_styles`, `period_styles` | `mdy`, `dmy`, `ymd`, `d_mon_y`, `mon_d_y`, `md`, `dm`, `mon_d` (`December 16`), `d_mon` (`16 Dec`) |
   | `sign` | `bank` or `card` |
   | `default_currency`, `account_type` | when the statement does not print them |
   | `headers`, `opening_labels`, `closing_labels`, `period_labels`, `sections`, `pending_markers`, `skip_prefixes` | extend the generic vocabularies when the layout uses other words |
   | `statement_date_labels`, `previous_date_labels`, `credit_total_labels`, `charge_total_labels`, `stub_markers` | a statement date instead of a period, the summary's own totals (an equation summary) and the words that start a payment stub |
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

A card **statement of account** needs no institution parser: a statement
date instead of a period, a table headed only `New Transactions for <NAME>
| Foreign Spending | Amount $`, month-named dates without a year across a
year boundary, `CR` under the amount, a two-line description, a boxed
equation summary and a payment stub are read by `card_statement_generic`
(INGESTION.md "Card statements of account"). Its synthetic layout is
`backend/tests/fixtures/synth_card_statement.py`
(`tests/unit/test_card_statement.py`); extend that rather than special-casing
an issuer.

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
| `metadata_names`, `name_anchors`, `anchors` | for a name printed only in a logo: other names in the PDF's title or author, phrases anywhere that name the institution (a subsidiary in a footer), and the layout's own wording that must appear beside either |

New column names go into the module's `HOLDING_HEADERS` (role `other` for a
column to recognise but not read) and `ACTIVITY_HEADERS`, asset-class group
headings into `ASSET_GROUPS`, action words into `ACTIVITY_TYPES` (or
`TRANSFER_TYPES` for a word whose type depends on whether units moved).
Several accounts in one PDF need no code: account headings anywhere on a
page (also beside an amount) and an account index split the document
(`app/ingestion/parsers/accounts.py`; INGESTION.md "Several accounts in one
PDF"). A period printed unlabelled in the page headers needs no code either,
nor does a holding printed by its name alone (no ticker, CUSIP or ISIN: an
unlisted partnership, a 529 portfolio): it is a `SecurityRef` with only a
name (which must hold a letter or digit; it is bounded and
formula-neutralised by the protocol), kept and counted like any other, and
the importer matches it by its exact normalised name among the securities
of the account (then of its institution) in its currency. Keep every
wrapped line of such a name (`Class R6`, `2031 (Example Funds)` is often
all that tells two portfolios apart; only a sentence under a name in
capitals is a note), never invent a symbol for one, and never name a
security by an activity's description column (a trade's *Security Name*
column may).
When a parser reads nothing usable the registry tries the next-best one. The fingerprint names
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
- A brokerage "Activity" export (an action column typed by
  `activity_type`, an amount, a symbol or quantity column) is not read by
  this path: `backend/app/ingestion/parsers/brokerage_table.py` turns it
  into investment statements per account (INGESTION.md "Brokerage activity
  exports"). For a new broker's export add its header words to that
  module's `HEADERS` (role `other` for a column to recognise and not read)
  and its action words to `ACTIVITY_TYPES` in `brokerage.py`; write the
  file from invented rows (`investment_rows` in
  `backend/tests/fixtures/synth_activity_csv.py`: sales never exceed the
  units bought before them) and extend
  `tests/unit/test_brokerage_table_parser.py` and
  `tests/integration/test_activity_table_import.py`. Bump `PARSER_VERSION`
  when the reading of an existing layout changes.
- Table cells arrive formula-guarded (a leading `'` on a cell that would be
  a formula, SECURITY.md C7). Strip the guard only to match headers and
  action words and to parse dates and numbers; text you keep (a security
  name, a memo, a payee) keeps it (`_text` in `brokerage_table.py`, or
  `app.core.csv_safety.neutralise_formula` again), and an identifier such
  as a symbol that would be a formula is refused.
- A debit/credit indicator column only signs unsigned amounts: the
  suggestion drops it when the amounts already carry a minus sign, and
  doubts it (`indicator_values_unknown`) when a row's indicator is not a
  direction word. A layout with such a column needs every value in
  `OUTFLOW_WORDS`/`INFLOW_WORDS` (`structured.py`) to be suggested
  confidently.

## Review checklist

- [ ] Fixture is generated by code; no real names, numbers or text (a real
      institution's own name and headings only for its layout's parser).
- [ ] `name` unique; `version` bumped if an existing parser changed.
- [ ] No new import of `app.ingestion.extract`; no I/O; patterns bounded.
- [ ] Only the last four digits of an account number are kept.
- [ ] Unit tests pass with and without the hint; tampered variants fail
      reconciliation.
- [ ] Figures are returned as printed, never adjusted to make a statement
      add up: a statement that does not reconcile waits in review, where
      the person can "Import as read" (rows as printed, no balance
      snapshot) once they have seen what is off. That answer is bound to
      the statement exactly as your parser returned it (every figure, the
      parser's name and version): when a new version reads it
      differently, the person is asked again.
- [ ] `uv run ruff check`, `ruff format --check`, `mypy`, `bandit` and
      `pytest -n auto` pass.

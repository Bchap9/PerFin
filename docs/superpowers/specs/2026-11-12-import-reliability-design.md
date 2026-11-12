# Import reliability (v1.0.0 stage 1)

Status: approved by the owner on 2026-11-12. Target release: `v1.0.0-rc.9`.

This is the first of four stages on the way to `v1.0.0`: import reliability
(this document), categorisation and subscriptions, the UI redesign, and the
release review. Each stage has its own design note and plan.

## Why

The owner runs rc.8 and reports: too many documents stuck in Review, "values
don't add up" items that cannot be reconciled in the UI, fixing and teaching
that are not intuitive, institutions that are not recognised, and wrong or
missing rows.

A corpus of 566 publicly published sample documents (411 of them genuine
statements or exports) was run through the real pipeline at `e37ffa8`
(sandbox, classifier, parsers, structured importer, each document in its own
empty household, proposed accounts accepted):

| | Statement-like | Parser produced rows | Verified |
|---|---|---|---|
| PDFs and images | 137 | 40 (29%) | 13 (9%) |
| CSV, OFX, QIF | 274 | 182 (66%) | 133 (49%) |

"Verified" means reconciled (PDF, image) or imported with nothing left in
review (structured). Of 90 bank and card PDF statements, 18 produced rows, 4
reconciled and 1 was processed without review. Some imports that looked clean
were wrong: card purchases imported as income, fund trades posted as cash,
several accounts merged into one, OFX rows dropped without notice, balance
lines imported as transactions. Twelve documents crash the OCR engine and 19
valid PDFs are refused as malformed.

The corpus is public samples only. Many are look-alikes, some are not from
the US, and it holds no complete credit-union statement and no real Wells
Fargo, Bank of America, American Express or Discover CSV. Gains measured on
it may not carry over fully to a household's own files. The samples are never
committed; every fix is pinned by a synthetic fixture under
`backend/tests/fixtures/`.

## Goals

1. Nothing wrong enters the ledger unnoticed.
2. More documents are read correctly the first time.
3. A clean read that cannot be proven imports as posted with a quiet marker;
   only real contradictions go to Review (owner decision, 2026-11-12).
4. Every Review item can be resolved where it is shown, and what a file
   imported can be seen.

## Non-goals for 1.0

- New formats: camt.053, Interactive Brokers Flex XML and its PDF layout, QIF
  investment sections.
- Locales with a decimal comma or non-English month names.
- Short option positions in the lot engine (the other rows of such a statement
  import; the option lines wait).
- Institution-specific parsers. Fixes are general; a layout that truly needs
  its own rules is taught with Teach PerFin.
- A UTF-16 text reader (refused by design today; revisit with a security
  review after 1.0).

## Work packages

Each package is one pull request into `main`. Finding ids (A1, C10, ...) refer
to the root-cause tables kept with the corpus results; the plan names the
files and tests.

### 1a. Never wrong silently; open what is valid

Structured files (CSV, TSV, XLSX, OFX, QFX, QIF):

- A table with quantity and price columns is never imported as a cash ledger
  without a person's say (fund and trade exports).
- A lone signed amount column is not taken on trust for a card-like table;
  the proposed account is not "checking" by default when nothing says so.
- Direction words beside amounts are evidence: a column of `debit`/`credit`
  that contradicts the signs holds the file.
- A debit or credit cell keeps its printed sign when it is the only amount
  column.
- An `Account` column of names splits the file per account, as an account
  number column does.
- Every record an extractor skips is counted and raises `lines_skipped`; a
  statement that loses rows never ends `processed` in silence. OFX
  transactions outside `BANKTRANLIST`, and SGML tags with no value, are read.
- Two positions of one security no longer crash the import.

Sniffer and extraction:

- Quoted multi-line cells, a preamble before the table, OFX headers that do
  not start with `OFXHEADER:`, `<?OFX` without `<?xml`, QIF comment lines and
  trailing NUL padding are accepted; a wrong declared OFX encoding falls back
  instead of crashing.
- A PDF with one unreadable cross-reference entry is read; active content is
  still removed (control C8 unchanged).
- When the OCR engine crashes in page-layout analysis, the page is retried
  once with single-block segmentation; if that fails the document is kept
  with what was read and a review item says so.
- Header-only exports and documents that print no transactions say "no rows
  to import" instead of asking for a column mapping.

CSV suggestions: more header words, date and amount shapes, note rows and
footers recognised; a day/month tie that every reading resolves to the same
day is no guess. The tie fix lands only together with the sign rules above,
because today the date doubt is the only thing holding several unsafe files.

### 1b. Bank and card PDF statements

- Headers printed over two or three lines, two headings in one cell, common
  rewordings, and a single unknown cell between date and money read as the
  description.
- A section title above the rows ("Deposits and additions", "Withdrawals",
  "Checks paid", "Payments and credits", "Purchases", "Fees", "Interest")
  sets the direction of unsigned amounts and survives a repeated page header.
  Section totals are used as checks.
- The statement period: an unlabelled range on page 1, more labels, and the
  classifier's period handed to the parser. Year-less rows with no period
  keep their rows with an assumed year and a question, instead of vanishing.
- Row geometry: right-aligned amounts outside their heading's span, a date
  fused with the description, a date printed once per day, both debit and
  credit printed with one zero, `DB` markers, detached currency symbols and
  signs, footnote marks.
- Balances: a balance line is never a transaction whatever its column; a
  label and its amount on neighbouring lines; newest-first statements;
  "previous balance" is not the closing balance.
- Parser choice: a statement is never filed as a receipt; a checking
  statement that mentions a credit card in a note is not read as a card; the
  fictional institutions' parsers need every phrase of their fingerprint.
- Rows dropped for any reason are counted.

### 1c. Brokerage, loans, payslips, tax forms, receipts

- Broker wording: more action phrases, cash-leg reinvestments as dividends,
  `<date> as of <date>`, total lines as notes, `Fees & Comm`, option symbols.
- OFX positions: one cent of slack on quantity x price, bonds priced per 100,
  option contract size; `INV401K` types the account as retirement.
- Positions exports with cash and total rows and per-account blocks.
- Loan and mortgage statements in the periodic-statement layout: the last
  payment's split, never the year-to-date column; the balance; the paid date.
  Loan bills are recognised as loan documents.
- Payslips: common label wordings, current and year-to-date columns,
  deductions summed, period beginning and ending.
- Tax forms: composite 1099 headings and dot-leader amounts; SSA-1099.
- Receipts: no receipt for a multi-page document or one with bill and loan
  words; a date on a line that also prints an expiry; label and amount paired
  by nearest line.
- OCR confidence scales a field by the cells it was read from, not by the
  worst word on the page.
- A currency is only an ISO code PerFin knows.
- 401(k) plan statements (roll-forward layout) come last in this package and
  may move to a follow-up if the rest is at risk.

### 1d. Trust policy and a Review that can be finished

Policy:

- A PDF or image statement whose every row read cleanly and that nothing
  contradicts is imported as posted. The statement record carries
  `verification = unverified` and the UI shows "not checked against a
  balance". A contradiction (totals that disagree, a running balance that
  disagrees) still holds the rows it touches.
- `lines_skipped` is raised only when the statement does not reconcile.
- A byte-identical upload is skipped with a notice and an undo, not a
  question. Near duplicates of one statement are one item with a list.
- One Review item per document: its reasons are sections of one card.
- The account choice is pre-selected when one candidate ranks first; a sole
  account at the statement's institution matches without digits.

Fixing:

- Reconciliation works on the ledger rows the document produced, not on the
  first reading. `GET /documents/{id}/reconciliation` returns opening,
  closing, the rows, their sum and the difference; rows can be corrected,
  added and removed; balances are entered as printed (a card's balance owed
  is positive on paper) and accept thousands separators.
- Likely causes are ranked from the difference: equal to one row, twice a
  row (sign), divisible by nine (transposed digits), equal to a fee or
  interest line, opening not equal to the previous statement's closing.
- Three exits: balanced (confirm and write the balance snapshot), finish
  later, accept as read. An acceptance is remembered for that statement
  across re-runs.
- A deleted imported row stays deleted (a tombstone on its import hash), and
  reprocessing a document replaces the rows it produced that nobody edited,
  instead of adding a second reading.
- Actions that do nothing are removed from the cards where they do nothing.
- Held brokerage lines can be corrected, not only taught or dismissed.

Teaching:

- A preview of what the taught layout reads before it is saved; direction per
  table; several tables; tables continued across pages; month-name dates; the
  account can be chosen in the dialog.

Traceability:

- `GET /transactions?document=<id>`; the document page lists what the file
  imported, the account it went to and its open questions; the upload list
  links each file.

The full visual treatment of Review belongs to the Inbox of stage 3. This
package delivers the working reconcile view and the per-document card inside
the current shell.

## Data model

One migration (`0013`):

- `statement_record.verification` (`reconciled`, `unverified`, `accepted`).
- `import_tombstone` (household, account, import hash, deleted at, deleted
  by): a deleted imported row is not imported again. Household scoped with
  forced row-level security; not exported (it is derivable noise for another
  instance) unless the portability review in the plan says otherwise.

Every gate applies: revision list and downgrade tests, RLS, the authz policy
matrix for new routes, AI column classification, OpenAPI and `schema.d.ts`
drift, request models with `extra="forbid"`.

## Security

- The sandbox contract is unchanged: parsers see validated extraction output
  only. The OCR retry starts a new child; it does not relax the filter.
- The PDF change catches a wider exception class per object and keeps
  removing active content; a test proves JavaScript is still stripped from a
  PDF with a broken cross-reference entry.
- Control C12 (an unreconciled statement is untrusted) changes: "untrusted"
  becomes "contradicted". `docs/SECURITY.md` and
  `docs/security/CONTROLS.md` are updated in 1d with the reasoning: a clean
  read with no contradiction is marked, visible and reversible; a
  contradiction is still held.
- New sniffer acceptances keep the markup scan and the existing reject tests.

## Testing and measurement

- Test first: each finding gets a failing test on a synthetic fixture before
  its fix.
- The corpus harness is re-run after each package. The table above is the
  baseline; the release notes of rc.9 state the after figures and say plainly
  what did not improve.
- `make check` in a Linux container and CI green before each merge.

## Risks

- General parser changes can harm layouts that work today. Mitigation: the
  full fixture suite, the corpus diff per file (rows and reconciliation), and
  flags for the riskier readers (headerless sections are deferred).
- The trust policy admits rows no balance vouched for. Mitigation: only when
  every row read cleanly; the marker; the reconcile view; statement and
  transaction deletes that stick.
- Replace-on-reprocess could remove a person's work. Mitigation: only rows
  untouched since import are replaced; edited rows are kept and listed.

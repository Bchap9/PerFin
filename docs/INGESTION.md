# Ingestion pipeline

Ingestion is the heart of PerFin's "smart" promise and also its largest attack
surface. The design goal is: maximum automation where confidence is high,
mandatory human review where it is not, and no path by which file content can
influence anything other than data fields.

## Intake paths

| Path | Trigger | Provenance recorded | Default owner attribution |
| --- | --- | --- | --- |
| Browser upload | user action | user, session, filename | the uploading user |
| Drop folder | file appears in watched mount | path, mtime, subfolder | subfolder name mapping, else review |
| Email | message in receipts mailbox | sender, message-id, subject, DKIM/SPF result | alias token -> household, sender -> user map |
| API | `POST /documents` with token | token id | token owner |

All four converge on the same `document` row and the same `ingest_document`
job. The pipeline never trusts the path to tell it what the file is.

## Pipeline stages

```
received ─▶ scanning ─▶ extracting ─▶ classifying ─▶ parsing ─▶ matching ─▶ committing
                │            │             │             │           │
                ▼            ▼             ▼             ▼           ▼
             rejected      failed     needs_review   needs_review  needs_review
```

### 1. Receive
- Hard size limit (configurable, default 50 MB). Streaming write, never buffered
  in memory.
- Magic-byte sniff; the declared MIME type and extension are ignored. Allowed:
  PDF, PNG, JPEG, HEIC (converted), TIFF, CSV, TSV, OFX/QFX, QIF, XLSX, EML,
  plain text, and ZIP (expanded once, depth 1, with total-size and file-count
  caps to defeat zip bombs). Everything else is rejected.
- SHA-256 computed on the stream. An identical hash already in the household
  short-circuits to a `possible_duplicate` review item unless auto-dedupe is on.
- Written to the **quarantine** blob area (encrypted like everything else, but
  logically separate and never linked from the UI as a downloadable file).

Status after Phases 3-4 (`app/ingestion/intake/`): browser and token uploads
(`POST /documents`), the drop folder and email intake (below). All
the types above are accepted by the sniffer and stored. CSV/TSV, OFX/QFX,
QIF (Phase 2) and PDF, PNG, JPEG, TIFF, HEIC, EML, ZIP and XLSX (Phases 3-4,
see "Extract") are extracted; an XLSX worksheet is a table like a CSV and
goes through the same column mapping. A ZIP is expanded once
inside the sandbox and each member becomes a document of its own (below).
Auto-dedupe is
not built: a byte-identical upload always becomes a `possible_duplicate`
item (`payload.reason: same_content`, `duplicate_of` the earlier document).

### 2. Scan (optional)
If the ClamAV sidecar is enabled, the quarantined blob is scanned. Positive
results move the document to `rejected` with the signature name recorded.

Status (`app/integrations/antivirus/`, `pipeline._scan`; SECURITY.md C9):
with `PERFIN_ANTIVIRUS=clamav` (compose profile `av`) the worker streams the
decrypted quarantined blob to clamd `INSTREAM` at `PERFIN_CLAMAV_ADDRESS`
(`tcp://clamav:3310` on the internal `backend` network, or `unix:///...`),
in 64 KiB chunks, never more than `PERFIN_CLAMAV_MAX_MB` (default 50), each
socket operation bounded by `PERFIN_CLAMAV_TIMEOUT_SECONDS` (default 30) and
the whole scan by four times that. The scan runs before any extraction. A
positive verdict sets `rejected` with `error = malware detected: <signature>`
and the stage log holding the signature name (allowlisted characters, at most 200); the blob stays in
quarantine and is never served. When the scan cannot run (clamd down,
timeout, no reply, file above the cap), `PERFIN_ANTIVIRUS_FAIL_MODE=closed`
(the default) retries the job while the failure is retryable and then marks
the document `failed` with an `unparsed` review item (`reason: scan_failed`);
`open` goes on unscanned and logs it. `perfin config check` refuses an
invalid address and warns about fail-open and a scan cap below the upload
cap.

### 3. Extract
Runs in a **sandboxed subprocess** inside the worker container: separate
process, `RLIMIT_AS` and `RLIMIT_CPU`, wall-clock timeout, no network (the
worker has none to begin with), read-only filesystem except a scratch tmpfs,
seccomp profile. Output is text and structured tables only.

Implementation (`app/ingestion/sandbox/`): the worker starts `python -I -B -m
app.ingestion.sandbox.child '<job JSON>'` with an empty environment, a private
temporary working directory, its own process group and the decrypted file on
stdin. Before reading input the child sets `RLIMIT_AS` (default 1 GiB,
`PERFIN_EXTRACT_MEMORY_MB`), `RLIMIT_CPU` (`PERFIN_EXTRACT_CPU_SECONDS`),
`RLIMIT_NOFILE`, `RLIMIT_FSIZE` 0, `RLIMIT_NPROC` 0 and `RLIMIT_CORE` 0,
`PR_SET_NO_NEW_PRIVS`, Landlock and a seccomp filter, and refuses to run if
any of that fails. Landlock grants only *reading* beneath the interpreter's
own paths (`sys.path`, the prefixes, system library directories) and the
child's own `/proc/<pid>`: the documents and quarantine volumes, key files
and other processes are out of reach (`PERFIN_EXTRACT_REQUIRE_LANDLOCK=false`
lets it run on a kernel without Landlock, with only the other layers). The
worker makes itself non-dumpable before it spawns a child, so a child under
the same UID cannot open `/proc/<worker>/{environ,mem,fd}` (ADR-0009). The
seccomp filter is a **default-kill allowlist** (Phases 3-4): only the
syscalls CPython, PyMuPDF, Pillow and Tesseract need run (memory, reading
already-permitted files, time, futexes and threads, its own process facts,
signals to itself), some only with argument checks (`clone` only for
threads, `prlimit64`/`kill`/`tgkill` only on itself, `fcntl` and `ioctl`
only with listed commands, `open`/`openat` only without a write flag); any
other syscall kills the child with `SIGSYS` and the document fails with the
sandbox code `syscall_denied`. The list was derived from `strace` of every
extractor, the fuzz corpus and the mutation fuzzer, and the sandbox tests
pass with it on the host (Ubuntu glibc) and in the `python:3.12-slim` base
image (Debian glibc). The calls a parser exploit wants fail with `EPERM`
rather than a kill so tests can tell which layer stopped them: sockets,
`execve`, `fork`/non-thread `clone`, signals to other processes, `ptrace`,
mounts and namespaces, BPF, `io_uring`, keyrings, identity changes, every
call that changes the filesystem and `open`/`openat` for writing. The
container's `cap_drop: ALL`, read-only root and missing egress are the outer
layers (ADR-0006 addendum).
When the worker runs as root, `PERFIN_EXTRACT_UID` makes the child switch to
that UID; the shipped image runs the worker as 10001 and the child shares
that UID, isolated by the layers above. The parent kills the process group after
`PERFIN_EXTRACT_TIMEOUT_SECONDS` or when stdout exceeds
`PERFIN_EXTRACT_MAX_OUTPUT_MB`, and validates stdout against a strict
Pydantic schema. Extractors are registered by kind
(`app/ingestion/extract/registry.py`): CSV/TSV, OFX/QFX (SGML and XML) and
QIF since Phase 2; PDF, images (with OCR), EML, ZIP and XLSX since Phases
3-4 (below).

- PDF (`app/ingestion/extract/pdf.py`): PyMuPDF on the in-memory stream.
  Words with boxes per page (points, origin top left), grouped into lines;
  tables from ruling lines (`find_tables`, cells filled with the page's
  words by position) and from aligned columns (`layout.column_tables`: runs
  of three or more lines sharing a whitespace gutter); title/author/producer
  metadata; at most `max_pages` pages and `max_pdf_objects` objects. Before
  anything is read, JavaScript, launch actions, embedded files and links are
  counted (`meta`) and removed from the in-memory document (MuPDF's JS
  engine off, `scrub`, catalog `OpenAction`/`AA` cleared, any remaining
  action dictionaries disarmed); nothing of them is returned and pages render
  with `annots=False` (C8). A PDF that needs a password is `ok=false`,
  `encrypted`: the pipeline parks it in review. OCR fallback per page when
  the text layer is empty (`no_text`), mostly not text (`garbage`: share of
  letters, digits and common punctuation below 0.75) or sparse (under 2
  characters per square inch) on a page mostly covered by images
  (`low_density`); OCR words are converted back to points.
- Images (`image.py`): PNG, JPEG, TIFF (frames up to `max_pages`) and HEIC
  (pillow-heif), each decoder restricted to the sniffed format;
  `Image.MAX_IMAGE_PIXELS` and an explicit header check against
  `max_image_pixels` before decoding (`too_many_pixels`); EXIF orientation
  applied, then all metadata dropped (OCR and previews use a fresh image
  from the pixels; `meta.exif_stripped`).
- OCR (`ocr.py`): Tesseract **in process** through `tesserocr` (the child
  may not `execve`; ADR-0009 addendum). Preprocessing: grayscale, upscaling
  of small text (the median text-line height, measured from the row profile
  of a binarised copy, brought up to 24 px, at most 3x; a narrow receipt
  whose lines cannot be measured is widened to 1000 px), 3x3 median denoise,
  deskew (projection profile, +-5 degrees), Otsu binarisation. Both
  profiles use automatic page segmentation (mode 3); `receipt` (tall,
  narrow images) also keeps inter-word spaces. The upscaling target and the
  segmentation mode were tuned on synthetic thermal receipts
  (`tests/fixtures/synth_receipts.py`, tuning seeds): upscaling every narrow
  image to 1000 px, as before, and mode 4 ("single column") both read fewer
  totals and dates (on 40 tuning receipts: all of date, total, subtotal
  and tax right on 25 before, 30 after). Time caps per page and per document (`ocr_page_seconds`,
  `ocr_total_seconds`), at most `max_ocr_pages` pages; what is left out is
  `truncated` with an `ocr_page_cap`/`ocr_time_budget` warning.
- Previews (`render.py`): every page (up to `max_preview_pages`) rendered to
  PNG at `preview_dpi` (default 100), the DPI lowered until it fits
  `max_render_pixels`; written as a new image with no ancillary chunks (the
  parent's `Preview` model accepts only `IHDR`/`PLTE`/`IDAT`/`IEND`). The
  worker stores each one as an encrypted blob in the permanent area
  (`app/domain/documents/previews.py`) and keeps only references in
  `document.extraction.stored_previews` (page, blob id, size, DPI and
  `scale`, the factor from page units to preview pixels for region
  highlight); deleting or re-extracting a document shreds them.
- CSV/TSV/XLSX: parsed with strict dialect detection; formulas in XLSX are
  never evaluated (values only). Cells beginning with `=`, `+`, `-`, `@` are
  neutralised so a later CSV export cannot carry formula injection.
- OFX/QFX/QIF: dedicated parsers; XML external entities disabled.
- EML (`eml.py`): standard-library `email` with `policy.default`. Headers
  (From, To, Cc, Delivered-To, Reply-To, Subject, Date, Message-ID,
  Authentication-Results), the text/plain body or else the HTML body
  converted by `htmltext.html_to_text` (a tokenizer pass on `html.parser`:
  no rendering, no I/O; script/style/head/iframe/svg content dropped, images
  dropped with their alt text kept, link targets dropped; remote resources
  counted, never fetched; D3). Attachments, inline `cid:` images and
  attached messages come back as child documents (bytes) at depth + 1;
  attached messages are not descended into, and a message extracted at
  depth 1 does not hand back its attachments (`depth_limit`; D4).
- ZIP (`archive.py`): expanded once, in memory, checked on the central
  directory before any member is decompressed: entry count
  (`max_children`), declared total (`max_children_total_bytes`), ratio above
  1 MiB (`max_zip_ratio`) and overlapping members refuse the whole archive
  (`too_many_members`, `zip_too_large`, `zip_bomb`); large, encrypted,
  linked, oddly compressed members, directories and `__MACOSX` forks are
  skipped and reported; reads stop at the declared size. Member names are
  metadata only (last component; C3). A ZIP at depth 1 is `depth_limit`.
- Children (`app/ingestion/children.py`): the worker receives each child
  through the normal intake (`receive_stream`: sniff, upload cap, hash,
  encryption into quarantine, its own ingest job) with `source_meta`
  `{parent_document_id, origin, depth, content_id?}`; the pipeline extracts
  it with `ExtractOptions.depth` from there. A refused child (HTML, SVG,
  unknown binary, too large) is listed with its reason in the container's
  `stored_children`, and the container goes to review
  (`container_incomplete`); otherwise the container is `processed`.

The extraction contract (`app/ingestion/sandbox/protocol.py`) carries
`pages` (words, lines, tables, text, OCR facts), `previews`, `meta`,
`email`, `children` and `text`; all bounded and validated in the parent.
The worker-only fields `stored_previews`/`stored_children` are refused in a
child's answer (`bad_output`). PDF and image pages are read into statements
and payslips by the worker's document parsers ("Parse" below); a page with
nothing they recognise (invoices, tax forms have no parser yet) waits in
review (`unparsed`, `no_records`); receipts are read by the receipt parser
and matched to their transaction ("Receipts" below).

### 4. Classify
Determine `kind` (statement, receipt, invoice, tax form, payslip, insurance,
other) and, for statements, the **institution** and **account**.

Order of attempts, each producing a confidence score:
1. **Fingerprints.** Deterministic rules per institution: header strings,
   logos' OCR text, account-number masks, column layouts. These are shipped as
   data (YAML) and can be added by the user from the review UI ("remember this
   layout").
2. **Heuristic classifier.** Small multinomial naive Bayes model over
   token features; trained offline on synthetic and contributed samples; runs
   locally with no network.
3. **Column-mapping review.** No model is consulted. If neither stage reaches
   threshold, the document goes to review with its extracted tables and a
   mapping editor; saving the mapping creates a fingerprint so the next
   statement from that layout is automatic.

Output: `kind`, `institution_id?`, `account_id?` (matched by last-4 and
institution), `period_start`, `period_end`, `tax_year?`, and per-field
confidence.

**Time window detection.** Statement period is pulled from explicit "Statement
period" strings when present; otherwise inferred as `[min(txn date),
max(txn date)]` with a flag that it was inferred. Receipts use the printed
date; tax forms use the tax year printed on the form.

Implementation (Phases 3-4, `app/ingestion/classify/`; runs in the worker on
the validated sandbox output, never on file content):

- **View** (`text.py`): at most 4000 lines of at most 300 characters; the
  header is the first 15 lines of page 1 (or the mail subject and body
  start); table headers are the first row of each extracted table.
- **Fingerprints** (`fingerprints.py`, shipped files in
  `classify/fingerprints/*.yaml`): `schema`, `key`, `institution`,
  `country?`, `kind`, `parser`, `version`, `date_order?` and `match` with
  `all` rules (every one must hold), `any` rules and `min_any`. A rule is
  one of `header` (substring of the header), `text` (substring of any
  line), `regex` (`region: header|text`, `ignore_case`), `account_mask`
  (`XXXX-XXXX-####`: `X`/`*` masked or any digit, `#` kept digit) or
  `columns` (a table header row starting with these cells). Confidence is
  0.85 plus up to 0.14 for the share of `any` rules that hold; two
  institutions matching equally strongly is no match. YAML is read with
  `yaml.safe_load` after refusing anchors and aliases and files over 64 KiB,
  then validated by a strict schema (unknown keys refused, bounded lists).
  Regexes accept only a restricted subset (no backreferences, lookarounds,
  inline flags, lazy quantifiers, nested quantifiers or quantified
  alternations; at most 256 characters, 12 quantifiers, repeat counts up to
  100) and are compiled with **possessive** quantifiers and **atomic
  alternation** (every group with `|`, and a pattern with a top-level `|`,
  becomes `(?>...)`), so the engine never returns into a quantifier or an
  alternation it has left: one match attempt costs at most pattern length
  times line length, a search at most that times the line length. They run
  over one line of at most 300 characters (`safe_regex.py`). Possessive
  quantifiers alone were not enough: `(?:a|a)` repeated 36 times then `b`
  backtracked 2^36 times (security review P34-SR-01). The first alternative
  that matches wins, so longer alternatives go first (`(?:ab|a)c`). A user's "remember this
  layout" is a `parser_fingerprint` row (`remember_layout`: same `match`
  schema, version incremented per institution, kind and parser); household
  fingerprints are tried with the shipped ones and win ties; a stored row
  that no longer validates is skipped. Matching work is bounded in total
  (security review P34-SR-R2-01: a linear-per-attempt pattern still costs
  pattern x line^2 per line, and 64 rules over 4000 lines took a minute):
  only the latest version of each layout is matched, a household remembers
  at most 100 layouts (`MAX_HOUSEHOLD_FINGERPRINTS`), and the pattern rules
  (`regex`, `account_mask`) of household fingerprints run under a
  wall-clock `MatchBudget` checked before every line, 0.25 s per document
  shared by all of them (`HOUSEHOLD_MATCH_SECONDS`); a fingerprint whose
  evaluation runs out of budget does not match, and the document goes on to
  the classifier and review. Shipped fingerprints are not budgeted. Six fictional institutions ship
  (Example Federal Credit Union, Sample National Bank (checking and
  1099-INT), Demo Card Services, Placeholder Brokerage, Fictional Mutual
  Insurance, Mock Savings and Loan with day-first dates).
- **Classifier** (`model.py`, artefact `classify/artefacts/kind_nb_v1.json`):
  binarised multinomial naive Bayes over unigram and bigram tokens (digits
  by shape), class score = log prior + 3 x (sum of known features' log
  likelihoods) / max(features, 30), softmax as confidence; a kind below 0.6
  stays `unknown` (`low_confidence_kind`). `scripts/train_classifier.py`
  trains it deterministically on `backend/tests/fixtures/synth_classify.py`
  (1540 synthetic documents, 40% with OCR-like noise); a test retrains it
  and requires the shipped bytes. Held-out accuracy (unseen seed, 420
  documents, classifier alone): 100% on this synthetic set, which comes from
  the same generator and so overstates accuracy on real documents.
- **Period, date, tax year** (`period.py`, `dates.py`): labelled periods
  ("Statement period", "Billing period", "For the period", "Pay period",
  "Policy period", ...; also on the next line), "Opening/Closing date"
  pairs, else the inferred `[min, max]` of parsed transaction dates or of
  dates that start a line (`inferred` flag, lower confidence). Dates: ISO,
  numeric month-first or day-first (a component above 12 decides, then the
  fingerprint's `date_order`, then the separator, marked ambiguous with
  lower confidence), month names in both orders, two-digit years, ranges
  sharing a year ("Jan 1 - Jan 31, 2026"); periods longer than 400 days or
  reversed are refused. Receipts and invoices: labelled printed date, else
  the first date (stored as `period_start = period_end`). Tax forms: "Tax
  year 2025", "2025 Form 1098", "Form W-2 ... 2025", "For calendar year".
- **Account** (`accounts.py`, `service.py`): last four digits from a
  structured statement, the fingerprint's mask, generic masks ("ending in
  1234", `****1234`, `xxxx-1234`) or a labelled number cut to its last four
  digits (nothing longer is kept); a document naming two different accounts
  has none. Statements and tax forms are matched with
  `match_account(last4, institution)` among `importable_account_ids` of the
  uploader; an account given at upload is never replaced. The institution
  is resolved by name among the household's institutions (none is created).
- **Output**: `document.kind`, `institution_id`, `account_id`,
  `period_start`/`period_end`, `tax_year` (fields below 0.5 confidence are
  not written) and `document.confidence` = `{version, method:
  structured|fingerprint|classifier|none, kind, fields: {kind, institution,
  last4, account, period, printed_date, tax_year}, fingerprint?: {key,
  origin, version, parser, id?}, institution_name?, period?: {source,
  inferred}, classifier?: {model, scores}, review?: [...]}`. Structured
  statements (OFX/QIF/CSV) keep the Phase 2 classify step; containers are
  not classified. The pipeline does not raise a review item for a low
  classification confidence by itself: a document whose kind stays
  `unknown` or that no parser reads waits in review (`unparsed`,
  `no_records`), and a statement whose account cannot be matched waits as
  `unknown_account`; the classification reasons are kept in
  `document.confidence.review` and the ingest log.

### 5. Parse
Produce structured records appropriate to the kind:

- Statement -> opening/closing balance, list of transactions (date, amount,
  payee, memo, type), fees, interest. For brokerage statements also holdings,
  trades, dividends. For loan statements principal/interest/escrow split and
  remaining balance.
- Receipt -> merchant, date, total, tax, line items (best effort), payment
  method last-4.
- Tax form -> form type, tax year, issuer, key boxes (W-2 boxes 1-6, 1099-INT
  box 1, etc.).
- Payslip -> gross, net, deductions, period, employer.

Parsers are versioned. Raw parser output is kept in `document.extraction` so a
later parser version can re-run without the original being re-uploaded.

**Reconciliation check.** For statements: opening balance + sum(transactions)
must equal closing balance within rounding. Failure drops confidence and raises
a review item describing the discrepancy. (Structured OFX/QIF statements state
no opening balance and are not reconciled this way; a CSV running balance is
checked row by row, "Match and deduplicate" below.)

Implementation for PDF and image statements and payslips (Phases 3-4,
`app/ingestion/parsers/`, `app/ingestion/reconcile.py`). The parsers run in
the **worker** on the validated pages of `document.extraction` (words with
boxes from the text layer or OCR); they never see file bytes (ADR-0006), and
a child answering with parser output is `bad_output`.

- **Choice** (`registry.py`): the classifier's fingerprint match is the hint
  (`document.confidence.fingerprint`): the parser written for its `key`
  first, then the parser it names (`statement_generic`,
  `card_statement_generic`), then the payslip parser for kind `payslip`; the
  fingerprint's `date_order` goes to a generic parser. Without a usable hint
  every parser scores the pages (`detect`) and the best at or above 0.35
  wins. All scores are recorded.
- **Generic table statement parser** (`table_statement.py`): a header line
  naming date, description and amount (or debit/credit style) columns, with
  optional balance and posting date, starts or continues the table (repeated
  per page); cells of later lines go to the column they overlap most. Dated
  lines with an amount are rows; table rows labelled opening/closing balance
  give the balances; a following description-only line without an amount is
  the previous row's memo; section headings type rows (`fee`, `int`, `div`)
  or start a **pending** section, whose rows are counted
  (`parse.pending_items`) and not imported (not posted, outside the
  balances); totals, footers and "continued" lines are skipped; dated lines
  without a readable amount are counted (`skipped_lines`, a
  `low_confidence_field` `lines_skipped` item). Outside the rows: the period,
  the account's last four digits (masked numbers, "ending in"; never more),
  the currency and labelled opening/closing balances. Signs: `bank` (signed
  amount, debit negative, `CR`/`DR`) or `card` (charges printed positive,
  credits negative or `CR`; balances owed become negative). Numeric dates
  whose day/month order the values do not show are guessed `mdy` at low
  confidence; yearless dates take their year from the period. The card
  variant (`card_statement_generic`) needs card vocabulary on page 1.
- **Institution parsers** (`institutions.py`): subclasses for the fictional
  institutions of the shipped fingerprints (Sample National Bank checking,
  Example Federal Credit Union, Demo Card Services, Mock Savings and Loan)
  that pin the date formats, sign convention, currency and institution name,
  so values read in their declared format score 1.0.
- **Payslip parser** (`payslip.py`, basic): employer, pay period, pay date,
  earnings and deduction lines, gross, total deductions, net; checked
  `gross - deductions = net`. Kept in `extraction.parse.payslip`
  (`kind = payslip`, the period on the document); no ledger rows. One that
  does not add up is a `low_confidence_field` item
  (`payslip_does_not_add_up`).
- **Receipt parser** (`receipt.py`; "Receipts" below): merchant, date and
  time, subtotal, tax, tip, discount, total, item lines, payment method and
  card last four, currency, each with a confidence. Kept in
  `extraction.parse.receipt` (`kind = receipt`, the date as the period).
- **Reconciliation** (`reconcile.py`, SECURITY.md C12): opening + sum =
  closing within less than one cent, and each printed running balance =
  previous + amount. A statement that does not reconcile (`mismatch`), or
  has neither balances nor a running balance (`unavailable`), has every row
  capped below the threshold (`reconciliation` field); the importer then
  imports all its rows pending, raises a `low_confidence_field` item
  (`reconciliation_failed`/`reconciliation_unavailable` with opening,
  closing, total, difference and the rows whose running balance disagrees)
  and writes **no** balance snapshot from it. An amount confirmed by the
  running balance scores 1.0.
- **Confidence** (`confidence.py`): per field (`posted_date`, `amount`,
  `payee`, `reconciliation`; statement: `period`, `opening_balance`,
  `closing_balance`, `account_number`, `currency`, `reconciliation`): 1.0 in
  an institution's declared format, 0.95 for the generic parsers, 0.9 for a
  balance derived from the running balance, 0.7 for an inferred period, 0.6
  for a guessed day/month order, 0.5 for a missing value or an unreconciled
  statement, 0.4 for an amount the running balance contradicts; OCR scales
  by the lowest Tesseract word confidence of the cell. Fields below
  `PERFIN_INGEST_CONFIDENCE_THRESHOLD` (default 0.85, at least 0.5) are
  listed per row (`low_confidence`).
- **Stored output**: parsed statements are `extraction.statements` with
  `source_format` `pdf`/`image`, each row with its `confidence`,
  `low_confidence`, `page` and `bbox` (region highlight), the statement with
  `opening_balance`, `parser` (name, version, institution key), per-field
  `confidence` and `reconciliation`; `extraction.parse` records the parser,
  every score, the threshold, pending/skipped counts, warnings and a
  payslip. `document.parser_version` is `name/version` and
  `document.confidence.parse` summarises it. Re-importing (choosing an
  account in review, `imports.apply_mapping`) uses the stored statements;
  `registry.reparse` runs a newer parser on the stored pages, so neither
  needs the file again. A statement then goes through the structured
  importer like an OFX one (account by last four digits and institution,
  `import_hash` dedupe, rules, provenance), with the confidence rules of
  "Commit".

### 6. Match and deduplicate
- Each parsed transaction gets an `import_hash`. Existing rows with the same
  hash are skipped; near-matches (same amount and date, different payee text)
  are surfaced as `possible_duplicate`.
- Overlapping statement periods are expected (e.g. a pending transaction that
  posts in the next cycle) and handled by hash, not by date range.
- Transfer pairing runs across accounts.
- Receipt -> transaction linking: match on amount, date within +/- 3 days,
  merchant alias. Confident match links the document; otherwise a review item
  "attach this receipt to…" with suggestions (implementation: "Receipts"
  below).
- Rules engine runs on every new transaction for category, tags, renames,
  splits.

Implementation for structured formats (Phase 2; `app/domain/imports/`,
`app/ingestion/parsers/structured.py`, `app/ingestion/match/`): the worker's
pipeline hands the validated sandbox output to the structured importer in the
same database transaction.

- **Account.** An OFX/QFX statement is matched by the last four digits of
  `ACCTID` and, when several accounts share them, the institution (`FI/ORG`
  compared with the account's institution name). A single-statement file
  uploaded to an account uses that account; QIF (no account number) needs
  one. Only accounts the uploader may **edit** are candidates (owner/admin:
  all), so a member's file never lands in an account hidden from them. No
  match, or several, is an `unknown_account` review item.
- **CSV/TSV.** The header row's fingerprint (case, spacing and a
  formula-guard quote ignored; for headerless files the shape of the first
  row) is looked up in the household's saved `import_mapping`s, trying the
  first rows so a preamble is found. No mapping is an `unparsed`
  (`mapping_required`) item: the mapping editor reads the stored table
  through `GET /imports/{document_id}/preview` (never re-parsed; it also
  proposes a mapping from header names and cell shapes, deterministic, no
  model) and saves it with `POST /imports/mappings`; `POST
  /imports/{document_id}/apply-mapping` re-runs the import from the stored
  extraction as the `imports.apply_mapping` job and resolves the items it
  answered. The next file with the same header is imported without asking.
  Mapped rows that do not parse (footers, totals) are skipped and listed in
  a `low_confidence_field` item; a running-balance column gives the closing
  balance and a reconciliation check (each balance = previous + amount;
  mismatches are a `low_confidence_field` item).
- **Dedupe.** `import_hash` exact matches are skipped (re-imports and
  overlapping statements); an incoming row with no hash match but an
  existing, unclaimed transaction on the same date with the same amount
  (other payee spelling, or typed by hand) is imported as `review_state =
  pending` with a `possible_duplicate` item naming the candidates.
- **Commit.** New transactions carry `source_document_id`; the household's
  rules run on them (categories from a QIF file are protected); QIF splits
  that add up become `transaction_split`s; the closing balance becomes a
  `statement` balance snapshot; a statement in another currency than its
  account is not imported (`unknown_account`, `currency_mismatch`). The
  worker queues the transfer matcher for the document afterwards. The
  document is `processed` when nothing is open for it in the review queue,
  `needs_review` otherwise. Audit rows (`transaction.import`,
  `document.ingest`/`document.import`) and events (`transaction.created`,
  `balance.updated`, `document.updated`) carry ids and counts only.
- Synthetic statements for every format and several bank-like CSV layouts
  come from `backend/tests/fixtures/synth_statements.py` (also
  `scripts/synth/statements.py --out DIR`).

#### Receipts (Phases 3-4)

Receipts arrive as photos or scans of thermal prints (PNG, JPEG, TIFF,
HEIC: OCR in the sandbox), e-receipt PDFs (text layer) and receipt mails
(the HTML or plain body of an EML; an attached PDF or image is a child
document and is read on its own). The parser runs in the worker on the
stored extraction, the matcher in the same transaction; neither sees file
bytes or calls a model (ADR-0006, ADR-0007).

**Parser** (`app/ingestion/parsers/receipt.py`, `receipt_basic`, chosen by
detection or the classifier's `receipt` kind; a mail body is read when it
scores at least 0.5):

- Labels are folded for OCR (digits Tesseract confuses with letters inside
  words, `T0TAL` -> `total`; keywords OCR runs together, `AMOUNTDUE`; near
  misses, `TOTAI`). Amounts need two decimals after a point or a comma, with
  an optional symbol or ISO code, minus/parentheses/trailing minus, a tax
  letter, OCR noise around them, or a colon that cannot be a time (`94:29`).
- Lines are typed (subtotal, tax/VAT/GST, tip, discount, total, change,
  tender, item); a keyword whose amount OCR put on the next line takes it.
  The **consistent set** is looked for: a total (strong labels such as
  "amount due" first, then the later line, then the card payment) for which
  subtotal + tax (+ tip - discount) = total, or the items add up, or (VAT
  included) subtotal = total. A figure whose currency symbol OCR read as a
  leading digit (`8116.83` for `£116.83`) and digits whose decimal point was
  lost (`6778` on a TOTAL line) are tried only as variants that make the set
  consistent, never on their own. A missing subtotal or tax is derived.
- Date: every format of `classify/dates.py` plus `14-MAR-26`; a labelled
  date or one next to a time first; expiry, return-by and due dates are
  ignored; an ambiguous numeric date follows the currency (GBP, EUR and
  other day-first currencies: `dmy`; USD/CAD: `mdy`) at 0.85, without one it
  is a guess (0.6). A mail without a date in its body takes the `Date`
  header (0.9).
- Merchant: the first header line that is not an address (when it has
  digits), phone, date, URL or receipt boilerplate; a mail's sender display
  name first. Payment: brand (or cash) and the last four digits of a masked
  card number; a printed full number is cut to its last four. The digits are
  identity data: stored in `extraction.parse.receipt`, never logged, audited
  or put in a review item (SECURITY.md H8).
- Confidence per field (`merchant`, `date`, `total`, `subtotal`, `tax`,
  `currency` when printed, `payment_last4`, `lines`): a consistent total,
  subtotal and tax score 1.0 whatever OCR's own confidence (three figures
  read independently agree, as a running balance confirms a statement row);
  a labelled total that cannot be checked 0.8, one that contradicts the
  others 0.6; otherwise the field's base score times the Tesseract
  confidence of the cell it was read from.

**Matcher** (`app/ingestion/match/receipts.py`): the merchant candidates are
first looked up among the household's payees (name and aliases; a known
payee becomes the merchant at confidence 1.0). Candidates are transactions
(not transfers) on accounts the uploader may edit (owner/admin and mail
without an uploader: all; member: `edit` grants; viewer: none), only the
receipt's account when it was filed under one (F3), posted within 3 days
of the receipt date (undated: the 60 days before it arrived, exact amounts
only), in the printed currency, for exactly the total (purchase, or a
refund) or up to 30% above it (a tip). Score `0.5 amount + 0.2 date + 0.3
merchant` (amount: exact 1.0, refund 0.6, tip 0.6 down to 0; date: 1.0,
0.9, 0.75, 0.6 for 0-3 days; merchant: payee identity 1.0, equal keys 1.0,
word containment 0.9 ("POS CORNER MARKET 0412" / "Corner Market"),
otherwise character similarity). The best candidate is **linked**
(`document_link`, audit `document.link` with `auto: true`, event
`document.linked`) when the amount is exact, the receipt's total is at or
above the threshold and it has a date, the score is at least 0.85, it leads
the next candidate by at least 0.1, and the transaction carries no other
receipt (documents from the same mail do not count). Otherwise a
`receipt_match` review item (`reason`: `ambiguous`, `not_confident`,
`receipt_uncertain`, `already_has_receipt`, `no_candidates`, `no_total`)
lists up to five `suggestions` (`transaction_id`, `account_id`, `score`,
`amount`, `days`, `already_linked`), best first. Resolving it
(`POST /review/{id}/resolve` with `{"kind": "receipt_match",
"transaction_id"}`) links the chosen transaction through the document
link checks; `transaction_id: null` records that none applies. When a later
import (worker `ingest_document`, `imports.apply_mapping`) adds
transactions, open `receipt_match` items are matched again against them
and resolved automatically (`auto_link`) when the match is now confident
and unique.

**Field accuracy** on the held-out synthetic set (40 receipts each,
`tests/unit/test_receipt_accuracy.py`, seeds never used for tuning;
Tesseract 5 with the Ubuntu `eng` data; precision = share of right values
among those scored at or above 0.85):

| Field | Thermal photos (OCR): accuracy / precision | PDF and mail e-receipts |
|---|---|---|
| total | 85.0% / 100% (33 confident) | 100% / 100% |
| date | 85.0% / 100% (23 confident) | 100% / 100% |
| merchant | 87.5% / 100% | 100% / 100% |
| subtotal | 82.5% / 96.8% | 100% / 100% |
| tax | 82.5% / 100% | 100% / 100% |
| card last four | 87.5% / (none confident) | 100% / 100% |
| currency | 90.0% / 100% | 100% / 100% |

The test fails below 80% accuracy for an OCR total or 97% precision for a
confident one (no wrong total may be linked automatically), and below 100%
for e-receipts. The synthetic photos are deliberately hard (58 mm prints
at 16-20 px text, faded ink, noise, specks, blur, +-3 degrees of skew);
most OCR misses are single digits in dates and a lost decimal point.

### 7. Commit
Within one database transaction: write transactions/holdings/payments, link the
document, update balance snapshots, move the blob from quarantine to permanent,
set `document.status`, emit events. Anything below the configured confidence
threshold (default 0.85 per field) is written with `review_state=pending` and a
`review_item`, so it is visible but marked and excluded from reports until
confirmed (configurable).

Implementation (`app/domain/imports/service.py`): a parsed row with any field
below the threshold (`PERFIN_INGEST_CONFIDENCE_THRESHOLD`) is written with
`review_state = pending` and its `confidence`; the statement's such rows share
one `low_confidence_field` item (`low_confidence`: the fields, the parser,
and per row the transaction id, fields, confidence and page). Confirming them
is a transaction change (`PATCH /transactions/{id}` or the bulk `confirm`
action); resolving the item itself is only "dismiss" so far.

## Review queue

The review UI is where the user teaches the system. Each item shows the source
document page with the relevant region highlighted, the proposed values, and
the alternatives considered. Resolutions feed back:

- Correct an institution guess -> offer to save a fingerprint.
- Correct a category -> offer to create a rule.
- Confirm a duplicate -> both hashes recorded as equivalent.
- Assign an owner/account for a drop-folder file -> remember the subfolder
  mapping.

Phase 2 implementation (`app/domain/review/`, API.md "Review notes"): the
queue lists items of documents the caller can see; resolutions are typed
per kind. `possible_duplicate`: a byte-identical file is confirmed
(`rejected`, it stays in quarantine) or released into the pipeline; a near
match of an imported row is confirmed (the row is deleted and its
`import_hash` kept for the surviving transaction: on that row when it has
none, else as an `import_hash_alias`) or kept (`confirmed`).
`unknown_account`: the chosen account (one the person edits) gets the
document and the stored extraction is imported again
(`imports.apply_mapping`); "remember" saves the statement's last four digits
on the account, or the account on the CSV mapping. `unparsed`: reprocess,
or re-import with a column mapping saved in the mapping editor. Closing the
last open item of an accepted document marks it `processed`. Upload and
download (`POST /documents` streams the multipart body straight into the
encrypting writer; downloads are attachments of the sniffed type, never
from quarantine) are in API.md "Documents notes".

Phases 3-4 implementation (`app/domain/review/assist.py`, `actions.py`,
`layout.py`; API.md "Review notes"):

- **Page and region.** `GET /documents/{id}/preview?page=` serves the page
  PNG the sandbox rendered (stored encrypted; checked again for plain PNG
  chunks; `image/png` inline, `nosniff`, `sandbox` CSP, `no-store`;
  audited). It follows the download rule (member or above, a viewer only
  where `documents.viewer_download` is on) and never serves a quarantined
  document. `GET /documents/{id}/regions?page=` gives the page's word
  boxes to anyone who can see the document, the words themselves only
  under the download rule. `GET /review/{id}` adds `regions` (the box a
  parser read a doubtful row from, matched by page, date and amount in the
  stored extraction; where an account number was printed for
  `unknown_account`) and `alternatives` (other amounts and dates on the
  row's line, for callers who may see the content; accounts ranked by last
  four digits, institution and currency; candidate and suggested
  transactions; the household's institutions).
- **Correct.** `POST /review/{id}/correct` corrects the document's
  institution, account, period, tax year and kind and the values of rows
  imported from it (through the transactions service: edit rights,
  audit). The `low_confidence_field` resolution corrects rows and confirms
  the item's pending rows (a statement that did not reconcile: all its
  pending rows on that account).
- **Remember this layout.** `POST /review/{id}/remember-layout` writes a
  household `parser_fingerprint`: rules from the request, or derived from
  the document (the first header line without digit runs must hold; other
  such header lines and the table's column layout corroborate, at least
  one of them). The rules pass the fingerprint schema and the ReDoS-safe
  regex subset, must recognise the document they are learned from, and
  the parser must exist (the generic parser of the kind by default; an
  institution parser is never reused for another layout). The rules must
  hold within the matching budget (`422 layout-too-costly`), and a
  household remembers at most 100 layouts (`409 fingerprint-limit`). The
  item is resolved; the document and up to 50 other `needs_review`
  documents the person may change whose stored layout the fingerprint
  matches are reprocessed (of the 500 newest scanned; the scan stops when
  its 2 s matching budget is spent).
- **Create a rule from this.** `GET /review/{id}/rule-draft` returns a rule
  pre-filled from one of the item's rows (payee text, account and sign;
  the row's category, else payee, else a review flag), validated by the
  rules schema but not saved; it is created through `POST /rules`.
- **PDF table column mapping.** A PDF or image that no parser read and
  that has page tables offers its largest table (same header across pages
  joined, cells formula-neutralised) to `GET /imports/{id}/preview`, the
  mapping editor and the importer as if it were a CSV table. A mapping
  saved from it (`POST /imports/mappings` with `document_id`) carries that
  header's fingerprint, so the next PDF of the same layout is imported by
  it without a question. Receipts and payslips are excluded.
- **Unknown owner.** The `unknown_owner` resolution with `remember` routes
  the drop-folder subfolder (see "Drop folder specifics").
- **The review UI** (`frontend/src/features/review/ReviewTools.tsx`,
  `frontend/src/features/documents/PageViewer.tsx`): each item shows the
  page image with the doubtful row's box highlighted (positions as
  percentages of the page, so they hold at any size; recognised words can
  be outlined), the other readings to pick ("Use date ...", a picked amount
  keeps the row's sign), "Confirm as read" / "Save corrections and
  confirm", "Create a rule from this" (draft, then "Save this rule"),
  "Remember this layout" for a new institution (optionally re-reading files
  already received), the member/account choice with "remember this
  folder" for an unknown owner, "Show receipt" beside ranked receipt
  suggestions, and "Map columns" with the page above the mapping form for a
  PDF or image table. The document library (`/documents/library`: kind,
  status, tax year and full-text search with matched words marked) and
  document detail (`/documents/{id}`, page viewer, download only when the
  server says `downloadable`) use the same viewer. A low-confidence OCR row
  was corrected and confirmed and a rule saved through this UI in headless
  Chrome against a live API and worker (Phases 3+4 finalization).

## Drop folder specifics

- Mounted **read-only** into the worker where possible; the worker copies into
  quarantine. If the user wants files removed or moved after processing, a
  second read-write mount of a `processed/` sibling is used, never the inbox.
- Structure is optional but recognised: `inbox/<member>/<account>/file.pdf`
  maps directly; a flat folder works with review items.
- Symlinks are not followed. Hidden files and partial-write temp patterns
  (`.part`, `~$`, `.crdownload`) are ignored. Files must be size-stable for a
  configurable settle time (default 10 s).
- Path components are never used to build filesystem paths on the PerFin side
  (defends against `../` in names). Original names are stored as metadata only.
- A per-run cap on files processed and total bytes prevents a runaway copy
  from exhausting storage.

How it is built (`app/ingestion/intake/dropfolder.py`, the
`watch_dropfolder` job in `app/workers/handlers/dropfolder.py`,
attribution in `app/domain/dropfolder/`):

- `PERFIN_DROPFOLDER_ENABLED` turns it on. `watch_dropfolder` is a recurring
  instance-level job (one per minute, one run at a time through an advisory
  lock) for one household: `PERFIN_DROPFOLDER_HOUSEHOLD_ID`, else the only
  household (with several and none set it stays idle and logs why). A run
  watches for up to `PERFIN_DROPFOLDER_WATCH_SECONDS` (inotify through a
  small `ctypes` binding, polling where inotify is unavailable) while files
  settle, and returns as soon as it has taken something so its writes
  commit promptly.
- Walking: directories are opened relative to their parent with
  `O_NOFOLLOW | O_DIRECTORY` (at most four levels, 10 000 entries a pass);
  every entry is `lstat`-ed and only regular files are candidates
  (symlinks, FIFOs, sockets and devices are never opened). Ignored names:
  hidden (leading `.`), `~$`, and `.part`, `.partial`, `.crdownload`,
  `.download`, `.tmp`, `.temp`, `.swp`, `~`, `.ready`.
- Complete means: neither `mtime` nor `ctime` moved for
  `PERFIN_DROPFOLDER_SETTLE_SECONDS`; not open for writing where the kernel
  can tell (a read lease is refused while a writer has it open; needs the
  file's owner or `CAP_LEASE`, otherwise the settle time is the signal);
  with `PERFIN_DROPFOLDER_READY_SIDECAR`, a regular `<name>.ready` exists.
- The file is opened by descriptor (`O_NOFOLLOW | O_NONBLOCK`), must still
  be the listed regular file, have one link (a hard link may reach outside
  the inbox: refused) and fit `PERFIN_UPLOAD_MAX_MB`, then goes through the
  same intake as an upload (sniff, SHA-256, size cap, encryption into
  quarantine). If size, `mtime` or the bytes read changed during the copy,
  the copy is discarded and the file is tried again later.
- Provenance: `source = dropfolder`, `source_meta` = relative path, folder
  slug, `mtime`, size and how it was attributed; `original_filename` is the
  cleaned last component.
- Attribution: a remembered route for `<member>/<account>`, then
  `<member>`; else a `<member>` folder named like a member's username (slug
  comparison) and below it an `<account>` folder named like exactly one
  account that member may edit. Routes and names that no longer hold (a
  viewer, a lost grant) are ignored. A member without an account goes
  through the pipeline as their upload (account matching among the
  accounts they edit). Nobody found: the document is held in
  `needs_review` behind an `unknown_owner` item ("Who does this belong
  to?", visible to owners and admins), whose resolution assigns it and can
  remember the subfolder.
- After-modes (`PERFIN_DROPFOLDER_AFTER`), applied only once the run has
  committed (a `dropfolder.finish` job, or the next run after a crash):
  `move` renames the original into `processed/` (refused files into
  `PERFIN_DROPFOLDER_FAILED`, default `processed/failed/`) under
  `<UTC stamp>-<entry id>-<sanitised name>`, copying through descriptors
  when the two are on different filesystems; `delete` unlinks taken files
  and keeps refused ones; `keep` leaves them. The original is found again by
  listing the inbox and matching its ledger key, never by a stored path. A
  read-only inbox (the default compose mount) makes `move` and `delete`
  act like `keep`, which is why `deploy/.env.example` ships `keep`.
- Idempotency: every taken or refused file has a `dropfolder_entry` keyed by
  its path hash, size, `mtime` and inode, so nothing is taken twice, across
  restarts too; in `keep` mode the same content under another name is
  skipped by its SHA-256 as well.
- Caps: `PERFIN_DROPFOLDER_MAX_FILES_PER_RUN` files (taken or refused) and
  `PERFIN_DROPFOLDER_MAX_MB_PER_RUN` per run; the rest waits for the next.
- `perfin config check` (worker role) warns about a read-only inbox with
  `move`/`delete`, a writable one with `keep`, a missing inbox, a
  non-writable `processed/`, and a settle time under 2 s without the
  sidecar; `processed/` or `failed/` inside the inbox is an error.

## Email intake specifics

- Dedicated mailbox strongly recommended; PerFin asks for IMAPS and a mailbox
  it may move messages within. Credentials are stored encrypted at the column
  level.
- Acceptance policy, all configurable:
  1. `To`/`Delivered-To` must contain the household's secret alias token.
  2. `From` must be in the allowlist (user's own addresses by default).
  3. If `Authentication-Results` headers are present, require `spf=pass` and
     `dkim=pass`. Note the limitation: a self-hosted receiving MTA may not add
     these headers; the alias token is the real control.
- Rejected messages are moved to a `PerFin/Rejected` folder and produce a
  single daily digest review item, not one per message (prevents a flood from
  an attacker who guesses the alias).
- The alias token can be rotated at any time from settings.
- HTML bodies are converted to text; remote images and scripts are never
  fetched. Inline `cid:` images are extracted as attachments.

Phase 4 status (`app/ingestion/intake/email.py`, `app/workers/handlers/mailbox.py`,
`app/domain/intake_settings/`, `/settings/intake`):

- **Settings.** Per household (`mail_intake`): host, port (993), username
  and app password (both column-encrypted; the password is write-only),
  inbox / `PerFin/Processed` / `PerFin/Rejected` folder names, the alias
  token (16 characters of lower-case base32, encrypted, rotatable),
  `member_addresses` (members', admins' and owners' own addresses are
  allowed senders, on by default), `allowed_senders` (extra addresses,
  each mapped to a member whose document the mail becomes),
  `require_auth_results` (on) and an optional `authserv_id`. Owners and
  admins manage them; changes and rotation need step-up. The instance
  switch is `PERFIN_EMAIL_INTAKE_ENABLED`.
- **Connection.** `IMAP4_SSL` only (implicit TLS, no `STARTTLS`),
  certificate and host name verified against the system store (a private
  CA via `SSL_CERT_FILE`), TLS 1.2+. With `HTTPS_PROXY` the connection is a
  `CONNECT host:993` tunnel through the egress proxy (add the host to
  `deploy/egress-allowlist.txt`); without one, every resolved address is
  vetted like the SSRF-safe HTTP client (private addresses only with
  `PERFIN_EMAIL_INTAKE_ALLOW_PRIVATE_HOSTS`).
- **What the worker parses.** Only the header block (at most 64 KiB, with
  the standard library's `BytesHeaderParser` in `compat32` mode: no MIME
  decoding, values and counts capped) and `RFC822.SIZE`, which is all the
  acceptance policy needs. A rejected message's body is never fetched. An
  accepted message is fetched whole and goes through the normal intake as
  an `.eml` document (`source=email`, `source_meta`: `sender`,
  `message_id`, `subject`, `auth: {present, spf, dkim}`, owner: the
  member the sender maps to); its body, HTML and attachments are parsed
  only by the sandboxed EML extractor, which returns attachments and
  `cid:` images as child documents (depth 1).
- **Policy details.** The token must be the local part or a `+` tag of an
  address in `To`, `Cc`, `Delivered-To` or `X-Original-To`. Exactly one
  `From` address. Only the topmost `Authentication-Results` header (or the
  topmost from `authserv_id`) is read, since anyone can add such headers
  further down; comments are ignored; `spf=pass` and `dkim=pass` are both
  required, and the DKIM pass must name its signing domain (`header.d` or
  `header.i`; a bare `dkim=pass` counts as unaligned) and that domain must
  be the `From` domain or a parent of it (a signer below the `From` domain
  or a one-label domain does not count).
- **Two-phase move and dedupe.** Each decision is recorded in
  `mail_intake_message` (key of an accepted message: SHA-256 of
  `Message-ID` plus `From`, or of the header block; of a rejected one:
  SHA-256 of the whole header block, so that a rejected forgery carrying a
  legitimate message's `Message-ID` and `From` cannot send that message to
  the rejected folder, security review P34-SR-R2-02) in the job's
  transaction; a message is moved only by a later run that finds it
  recorded, and the handler queues that follow-up run whenever a run
  recorded something. A second copy of an accepted message (or an
  identical copy of a rejected one) is moved without being imported or
  evaluated again. Servers without `MOVE`
  get `COPY`, `\Deleted` and `UID EXPUNGE`.
- **Caps.** Per run `PERFIN_EMAIL_INTAKE_MAX_MESSAGES_PER_RUN` new
  messages (50), `PERFIN_EMAIL_INTAKE_MAX_MB_PER_RUN` (200) fetched;
  messages over `PERFIN_EMAIL_INTAKE_MAX_MESSAGE_MB` (25, and never over
  the upload cap) are rejected from their declared size. Polls run every 5
  minutes (`poll_mailboxes` dispatches `poll_mailbox` per enabled
  household).
- **Digest.** Rejections add to one open `rejected_email` review item per
  household and UTC day (no document, owners and admins only): `day`,
  `count`, `reasons` (`no_alias`, `sender_invalid`, `sender_not_allowed`,
  `auth_failed`, `too_large`, `unsupported`), up to 50 `messages`
  (`sender`, `subject`, `reason`, `at`) and `omitted`. Dismissing it starts
  a new item with the next rejection that day.
- **Not built:** IMAP IDLE (polling only) and OAuth mailbox login.

## No models in the pipeline

PerFin does not call a language model at any stage of ingestion, local or
hosted (ADR-0007). Document text, account numbers and personal details have no
code path off the host. The cost is that a never-seen statement layout needs a
person once, through the column-mapping editor; the benefit is that the
pipeline is deterministic, reproducible and auditable, and a crafted document
cannot talk its way into the ledger.

The heuristic classifier is a small local model trained offline on synthetic
and contributed samples; it never makes network calls and ships as a versioned
artefact in the image.

## Parser contribution model

Institution fingerprints live in `backend/app/ingestion/classify/fingerprints/`
(YAML) and parsers in `backend/app/ingestion/parsers/` (code), with a test
fixture format that uses **synthetic** statements (never real ones). A
contributor adds a fixture, a fingerprint and, if needed, a parser subclass.
A CLI `perfin ingest --dry-run file.pdf` prints every stage's output for
debugging without writing to the database (`app/ingestion/dryrun.py`): one
JSON document per stage (`receive` with the sniff and the same refusals as an
upload, `scan` when ClamAV is configured, `extract` through the real sandbox,
`classify`, `parse`, and with `--household` a `match` read in a rolled-back
`READ ONLY` transaction). Nothing is written to the blob store or the
database; previews and child documents are summarised, not printed.

The steps, the parser contract and the review checklist are in
[CONTRIBUTING-parsers.md](CONTRIBUTING-parsers.md). In short:

1. **Fixture.** A generator in `backend/tests/fixtures/synth_pdf_statements.py`
   (reportlab) that draws the layout from a `SynthStatement` of invented
   rows, including what makes the layout hard (several pages, pending
   items, fees and interest, debit/credit columns) and a `tamper` variant
   that does not reconcile. Never a real statement, never a redacted real
   one.
2. **Fingerprint.** A YAML file in `classify/fingerprints/` whose `key` the
   parser declares and whose `parser` names the generic parser to fall
   back on.
3. **Parser.** Usually a `TableStatementParser` (or `CardStatementParser`)
   subclass that only declares facts: `name`, `version`, `institution` (the
   fingerprint key), `display_name`, `fingerprints`, `date_styles`,
   `period_styles`, `sign`, `default_currency`, `account_type`, extra
   `headers`/labels/`sections`. Code only when the layout needs it. Listed
   in `INSTITUTION_PARSERS`.
4. **Version.** Bump `version` whenever the output for the same pages may
   change; the stored `parser` reference says which version read a
   statement, and `reparse` re-runs from the stored pages.
5. **Tests.** The unit test parametrisation in
   `tests/unit/test_statement_parsers.py` (exact rows, balances, period,
   last four, currency, full confidence, reconciliation, the tampered
   variant failing) and, for a new kind of layout, an end-to-end case in
   `tests/integration/test_pdf_statement_pipeline.py`.

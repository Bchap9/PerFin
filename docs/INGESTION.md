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
goes through the same column mapping (a brokerage activity export instead
goes to the investments importer, "Brokerage activity exports" below). The
sniffer reads a text file as CSV/TSV when its first non-empty lines carry
the same number of delimiters, or when a leading block of most lines, two
at least, are rows of three columns or more followed only by notes (a short
brokerage export's disclaimer paragraphs and "Date downloaded" line; rows of
five delimiters or more need only two, however many notes follow). A file
that writes `a, "b, c"` (a space between the delimiter and a quote) is read
with the space after each delimiter skipped, so its quoted fields hold. A ZIP is expanded once
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
`RLIMIT_NOFILE`, `RLIMIT_FSIZE` 0 and `RLIMIT_CORE` 0 (no `RLIMIT_NPROC`: it
counts the threads of the whole UID, shared with the worker, and 0 stopped the
extractors' own threads; seccomp stops new processes instead),
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
- OFX/QFX/QIF: dedicated parsers; XML external entities disabled. OFX
  investment statements (`INVSTMTRS`, `extract/ofx_invest.py`, extractor
  `ofx/2`) are read from the same hardened tree: positions (`INVPOSLIST`),
  activity (`INVTRANLIST`: buys, sells, income, reinvestments, expenses,
  transfers, splits, cash `INVBANKTRAN`), closing cash (`INVBAL/AVAILCASH`)
  and the `SECLIST` names; only the account's last four digits and
  instrument identifiers leave the child (`ExtractResult.investments`,
  "Brokerage statements" below).
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
nothing they recognise (invoices have no parser yet) waits in
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
   statement from that layout is automatic. A CSV/TSV/XLSX whose header
   names its columns plainly is mapped deterministically and saved without
   asking ("Never-seen layouts" under "Match and deduplicate").

Output: `kind`, `institution_id?`, `account_id?` (matched by last-4 and
institution), `period_start`, `period_end`, `tax_year?`, and per-field
confidence.

**Time window detection.** Statement period is pulled from explicit "Statement
period" strings when present; else from the **header band**: a date range
printed without a label in the top lines of at least half the pages
(`July 1 – July 31, 2015` under the title of every page, `07/01/2015 -
07/31/2015`, `July 1-31, 2015`; dash, en/em dash, "to", "through",
"until"), the start borrowing the end's year; otherwise inferred as `[min(txn date), max(txn
date)]` with a flag that it was inferred. An inferred period is a hint only
(kept in `document.confidence.period`): it is never stored as the
document's period, which comes from the parsed statements. Receipts use the
printed date; tax forms use the tax year printed on the form.

Implementation (Phases 3-4, `app/ingestion/classify/`; runs in the worker on
the validated sandbox output, never on file content):

- **View** (`text.py`): at most 4000 lines of at most 300 characters; the
  header is the first 15 lines of page 1 (or the mail subject and body
  start); table headers are the first row of each extracted table.
- **Fingerprints** (`fingerprints.py`, shipped files in
  `classify/fingerprints/*.yaml`): `schema`, `key`, `institution`,
  `country?`, `kind`, `parser`, `version`, `date_order?` and `match` with
  `all` rules (every one must hold), `one_of` rules (at least one must
  hold, when any are listed), `any` rules and `min_any`. A rule is
  one of `header` (substring of the header), `text` (substring of any
  line), `regex` (`region: header|text`, `ignore_case`), `account_mask`
  (`XXXX-XXXX-####`: `X`/`*` masked or any digit, `#` kept digit),
  `columns` (a table header row starting with these cells) or `metadata`
  (substring of the PDF's title or author). Confidence is
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
  Insurance, Mock Savings and Loan with day-first dates), and one real
  layout, the Fidelity Investments "Investment Report"
  (`fidelity-investments-statement`: both "Fidelity Investments" and
  "Investment Report" in the header, so a bank statement naming the broker
  as a payee does not match; its documents are drawn from invented facts by
  `tests/fixtures/synth_fidelity.py` and `synth_classify.py` and are not in
  the classifier's training corpus).
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
  "Policy period", ...; also on the next line), else the header band
  (`header_band_range`: the separated range found in the first 8 lines of
  the most pages, on at least half of them; source `header`, confidence 0.9,
  0.75 for a one-page document), "Opening/Closing date" pairs, else the
  inferred `[min, max]` of parsed transaction dates or of dates that start
  a line (`inferred` flag, lower confidence, never written to
  `document.period_start/end`). An ambiguous numeric end takes the order the
  other end proves (`07/01/2015 - 07/31/2015` is month-first,
  `01/07/2015 - 31/07/2015` day-first), and that proof beats the
  fingerprint's `date_order` (`settle_order`). Ranges whose end is only a
  day: `July 1-31, 2015`, `1 - 31 July 2015`. Dates: ISO,
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
  inferred, start, end}, classifier?: {model, scores}, review?: [...]}`. Structured
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
  box 1, etc.). Built in Phase 6 as `app/ingestion/parsers/tax_forms.py`
  (`tax_forms`, kind `tax_form`, first in the registry: a `Form <type>`
  heading with its box labels is unambiguous even on a bank's letterhead).
  It reads the forms of every registered jurisdiction form set
  (`app.domain.tax.forms`; US: W-2, 1099-INT/DIV/B/MISC/NEC/R/G/SA, 1098,
  1098-E, 1098-T, 1095-A/B/C): one form per `Form <type>` heading (a
  consolidated 1099 gives several, numbered per type; identical copies are
  kept once), the tax year (`Tax year 2025`, `For calendar year 2025`, or the
  year on the heading line), amount boxes by label (a cell starting with the
  box id and the first words of its label; the amount below it in its
  column, or right after it), W-2 box 12 per code (`12_D`), the sum of the
  "Other" lines of W-2 box 14 (`14t`) and 1098 box 10 (`10t`, e.g. property
  tax), the 1095-A Part III monthly table, 1095-B/C covered months (`cov_<MON>`
  / `cov_ALL` from `X` marks under a month header) and 1095-C line 15, and
  the issuer's display name from the form's issuer box (W-2 box c, the 1099
  payer box, the 1098 lender box). **Identity boxes are never read**: the
  result (`extraction.parse.tax_forms`, `TaxFormRead`) holds amounts by box
  id and the issuer name only. Because the stored pages would still carry
  the SSN, EIN, names and addresses, the worker then masks the stored
  extraction of a tax form (`redact_identity`: pages, words, lines, layout
  tables and text keep only form vocabulary, amounts, years, box ids and
  codes; everything else becomes `***`; the forms read are appended to the
  text so the document is found by issuer and form type) before
  `document.extraction` and the search index are written. The same masking
  covers tax documents outside the registered set (a prior-year Form 1040,
  an unrecognised form) whatever the classifier called them: a document no
  statement, brokerage, loan or payslip parser read whose text holds an
  SSN/ITIN-shaped token, a labelled EIN or a tax-return heading
  (`carries_tax_identity`) is redacted too, with the parse warning
  `tax_identity_redacted`; a receipt read from the unredacted pages of any
  redacted document is dropped (its "merchant" would be a name) and a
  `receipt` kind becomes `unknown`. The receipt parser never takes a
  merchant from a line with an SSN/EIN-shaped token (security review
  P56-SR-R1-02). Statements and payslips keep their page text (their review
  tools need it). Word regions,
  snippets and search therefore never see identity text; the original file
  (encrypted, I1 rules) and its page previews (images) still show the form
  as printed. `app.domain.tax.ingest.import_tax_forms` then records each
  form (`record_form_data`), opens the tax year if needed (checklist learned
  from the year before) and ticks the checklist off (the expected item of
  that form and issuer, else a `detected` one). A form without a year, or
  with a field below the threshold, raises `low_confidence_field`
  (`tax_form_low_confidence` / `tax_form_unread`, field names only) and the
  document stays `needs_review`. Unit tests: `tests/unit/test_tax_form_parser.py`;
  end to end: `tests/integration/test_tax_form_pipeline.py`.
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
  wins. All scores are recorded. When the chosen parser reads nothing
  usable, the next-best parsers at or above 0.35 are tried in turn
  (warning `parser_fallback:<name>`).
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
  the currency and labelled opening/closing balances. The period
  (`read_period`): every line with a period label is a candidate, tried in
  order until one reads (a label alone on its line offers the next line; a
  label that matches something else first, "Change from last period", no
  longer blocks the real one), a range's start borrowing the end's year;
  without one, the header band (the classifier's `header_band_range` on
  the parser's page lines), scored as the classifier scores it
  (`band_confidence`: 0.9 on two or more pages, 0.75 on a one-page document,
  0.1 less with a guessed day/month order), so a range atop a one-page PDF
  is no stated period for the duplicate guard or `document.period_*`; a
  labelled period gets the parser's confidence. The day/month order of a
  numeric period is the one its values prove (a part above 12), whatever
  default the rows suggest; when the rows fit both orders or are not
  numeric (`2 Jul 2015`), the order the period proves dates the rows too
  (`01/07/2015 - 31/07/2015` with rows `06/07/2015`: 6 July). Signs: `bank` (signed
  amount, debit negative, `CR`/`DR`) or `card` (charges printed positive,
  credits negative or `CR`; balances owed become negative). Numeric dates
  whose day/month order the values do not show are guessed `mdy` at low
  confidence; yearless dates take their year from the period. The card
  variant (`card_statement_generic`) needs card vocabulary on page 1
  ("card ending", "credit card", "card statement", "new charges",
  "corporate card", "charge card", "cardmember", ...).
- **Card statements of account** (`table_statement.py`, version 4, any
  institution): the layout of a charge or corporate card statement that
  prints a **statement date** and no period.
  - *Headless table header*: a heading naming the table (`New Transactions
    for <NAME>`) beside money columns only (`Foreign Spending | Amount $`):
    left of the first column heading, each row gives its date (the first
    cell, or its first words on a line with an amount) and then its
    description. A *Foreign Spending* / *Foreign Amount* / *Original
    Currency* column is informational, in any table: the memo keeps it
    (`Goods or Services; foreign amount 360.00 US Dollar`), the amount is
    the card currency's.
  - *Rows*: dates with a month name and no year (`December 16`, `16 Dec`),
    a mark after the date (`January 9 *`, a processing date), a second
    description line (a category) as the memo, `CR`/`DR` after the amount
    or alone under it on the next line.
  - *Statement date*: with no period printed, `Statement Date`, `Closing
    Date`, ... or a cell reading only `Date` (on a line naming no column
    heading; its value beside it or in the cell under it) ends the period.
    It starts the day after a printed previous statement date, else the
    day after the same date a month before, or the earliest row when one
    is older. Yearless rows take the latest year on or before the
    statement date (December rows of a January statement are the year
    before; no row lands after it); a row printed with a year after the
    "statement date" drops it (the rows' span is the period, inferred).
    Confidence 0.9 (`DERIVED`): a stated period for the duplicate guard
    and `document.period_*`. A range after `Opening/Closing Date` is a
    period label, not a statement date.
  - *Labels over values*: a row of labels over a row of boxes (`Previous
    Balance | Payments and Credits | New Charges | New Balance` over
    `9,803.59 - 11.39 + 4,330.71 = 14,122.91`; stacked label lines add
    up) gives each value its label. A summary that is a whole equation
    (opening + credits - charges = closing, ledger convention) must also
    match the rows' money in and out, or the statement does not reconcile
    (warning `summary_totals_mismatch`).
  - *Payment stub*: from a line saying to detach it (`Please detach here`,
    `Return this portion`, ..., no amount) to the end of its page nothing
    is read (unless a table header follows on that page), so its dated
    `Amount Due` line is no row and its full account number is never the
    statement's.
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
  `document.confidence.parse` summarises it. The document's
  `period_start`/`period_end` is the span of the periods its statements
  (bank and brokerage) read at or above the threshold. Re-importing (choosing an
  account in review, `imports.apply_mapping`) uses the stored statements;
  `registry.reparse` runs a newer parser on the stored pages, so neither
  needs the file again. A statement then goes through the structured
  importer like an OFX one (account by last four digits and institution,
  `import_hash` dedupe, rules, provenance), with the confidence rules of
  "Commit".

#### Brokerage statements (Phase 5)

Live check (Phases 5+6 finalization): a synthetic brokerage PDF (January)
and OFX (February), two mortgage statement PDFs, a W-2 and a 1099-INT were
uploaded to a live `perfin api` and processed by a live `perfin worker`
through the sandbox on a database migrated to `0005`; every one was
`processed` with no review item, the holdings equalled the February
positions, the payment splits equalled the statements (`schedule_check`
`ok`), the checklist showed both forms received, and no identity value of
the tax forms was found in a dump of the database.

Brokerage statements are `ExtractResult.investments`
(`InvestmentStatement`: account, period, `as_of`, positions, activity,
opening/closing cash, the printed holdings total and account value), from
OFX `INVSTMTRS` in the sandbox or from a PDF/scan's pages in the worker.

- **Parser** (`app/ingestion/parsers/brokerage.py`): a **holdings** table
  (header naming a quantity, a market value and a symbol or security name;
  optional price, cost basis, beginning market value; unrealized gain/loss
  and estimated income columns are recognised and not read) and an
  **activity** table (date, activity/action and amount; optional settle
  date, description, security name, symbol or CUSIP, quantity, price,
  fees), each repeated per page; outside them the period (read like the
  bank parsers': label candidates, then the header band; yearless `MM/DD`
  activity dates in the generic parser too), last four digits,
  currency, opening/closing cash, holdings total, account value and the
  holdings' "as of" date by label (`Beginning Account Value` is not the
  account value; `Ending Account Value` and `Ending Net Value` are).
  Header cells may run over up to three lines (`Price Per` / `Unit`,
  `Symbol/` / `CUSIP`, a date under `Ending Market Value`) or end in a
  date; a table with only a settlement date is dated by it; without an
  action column the description is the action when the table also names a
  security, symbol or quantity (so a bank's *Date / Description / Amount*
  table stays a bank's); a header line of known column names the parser
  cannot use ends the table above it. Holdings may be grouped under
  asset-class headings (Mutual Funds, Exchange Traded Products, Stocks,
  Bonds, Core Account, ...) that set the positions' asset class; `Total
  <group>` lines are subtotals, `Total Holdings` (or `Total Market Value`,
  `Total Securities`, ..., else the first unlabelled `Total`) is the
  holdings total, read from the market value column; a security name
  wrapped onto the next line is joined. The activity text types a row
  (`activity_type`, also meant for CSV action columns: case, punctuation,
  the security and a trailing `(Cash)`/`(Margin)` after the leading words
  do not matter): buy (`Buy`, `You Bought`), sell (`You Sold`), dividend
  (`Dividend Received`, `Long-Term`/`Short-Term Cap Gain`), interest
  (`Interest Earned`), fee (`Fee Charged`, `Foreign Tax Paid`), reinvest,
  split with its ratio from the description, transfer in/out, deposit
  (`Direct Deposit`, `Electronic Funds Transfer Received`), withdrawal
  (`Electronic Funds Transfer Paid`), corporate action; `Transferred
  From`/`Transferred To` is a `transfer_in`/`transfer_out` when the row
  names a security and a non-zero quantity, else a cash `deposit`/
  `withdrawal`, and a transfer of units printed with no amount (`-`) moves
  no cash. An unsigned amount takes the type's direction; a symbol is read
  from its column (a valid CUSIP there is the CUSIP), from the
  description's first word when the row needs a security or the symbol is
  held, or from `(SYMBOL)`. `brokerage_statement_generic` (version 4)
  scores 0.5-0.7 when it sees these tables (above the bank parsers'
  0.4-0.45 for the same activity table); `placeholder_brokerage` pins the
  fictional Placeholder Brokerage (`MM/DD/YYYY`, USD) and needs the
  institution's name on page 1; `fidelity_investments_statement` pins the
  Fidelity Investments "Investment Report" (USD, `MM/DD` rows dated from
  the period, `MM/DD/YYYY`, `Month D, YYYY` period after "Investment
  Report") and needs "Investment Report" on page 1 and the institution's
  name on page 1, in the PDF's title or author (the name on a report's
  page 1 can be only its logo), or its subsidiary's name ("Fidelity
  Brokerage Services") anywhere; a name found only in the metadata or by
  the subsidiary also needs one of the layout's own phrases somewhere
  (*Accounts Included in This Report*, *Core Account*, *Ending Account
  Value*, *Securities Bought & Sold*). The shipped fingerprint (version 2)
  likewise takes the name from the header or the metadata (`one_of` with
  a `metadata` rule). Its core money market position is the account's cash: the
  rows under *Core Account* are no position, their beginning and ending
  market values are the opening and closing cash and are taken out of the
  printed holdings total, so the cash check covers every trade, income and
  transfer; the *Core Fund Activity* table (the core's own trades settling
  them) is not read.
- **Table reading** (any institution; the synthetic Fidelity-layout report
  `tests/fixtures/synth_fidelity_report.py` and the provider-neutral
  combined statement `synth_neutral_report.py` are read to the last row
  by the generic parser as well as by the layout's):
  - **Header vocabulary**: *Transaction Description* (the action),
    *Transaction Total* / *Transaction Amount* (the amount), *Settlement
    Date*, *Post Date*, *Trans. Date*, *Symbol/CUSIP*, *Transaction Cost*
    (fees), *Cost Basis of Close*, *Check Num.*, *Code*, *Payee*,
    *Maturity*, *Coupon Rate*, *Est. Annual Income (EAI)*, *Accrued
    Interest (AI)*, ... A header cell naming no known column is still a
    column (not read), so its values never slide into a neighbour; a
    header cell whose words are several headers (`Transaction Description
    Quantity`, `Check Num. Post Date Code Description`) is split into them,
    and a row cell spanning two columns (`120 $35.61700`, `96205LQK3 Muni
    exempt interest`) is shared out word by word.
  - **Headers over lines**: the line with the most cells gives the
    columns, the words of the other lines join the column they lie over
    (`Price` / `Per Unit`, `Settlement` / `Date`, `Cost Basis` / `of
    Close`, `Ending Market Value` / `Accrued Interest (AI)`); a heading is
    never a header line (nor is a short top line that changes no column
    the lines under it name: an unknown heading such as `Electronic Funds
    Transfers`), and a group heading right above a holdings header (`Core
    Account`) still groups its rows.
  - **Side-by-side tables**: a header repeating one group of cells (`Date |
    Reference | Transaction Description | Transaction Total` twice) is
    that many tables, each reading its half of every line (a `Total` in
    one half ends only that half's row).
  - **Sections**: the heading above an activity table says how its rows
    are read when their own words do not: *Trades Pending Settlement*,
    *Core Account Cash Flow*, *Daily Additions and Subtractions*,
    *Estimated Cash Flow*, *Income Summary*, *Margin Activity* are no
    activity; *Securities Transferred In/Out* move units (the printed
    amount is their value, no cash) or, without units, cash (a deposit or
    withdrawal); *Dividends, Interest & Other Income* rows are dividends,
    or interest when they say so; *Deposits*/*Contributions* and
    *Withdrawals*/*Distributions*, *Checking Activity*, *Debit Card
    Activity*, *Bill Payments* and *Cash Activity* are cash by their sign;
    *Electronic Funds Transfers* are cash by their sign;
    *Fees and Charges* and *Taxes Withheld* are fees. A section lasts until
    the next heading: after its own table, a heading no section names
    (`Miscellaneous Items`) right above the next table ends it, so that
    table's rows are typed by their own words only (an untyped row is
    held, never booked as the section above's type) and the table after a
    skipped section is read; right under the section's heading such a line
    is a sub-heading (`SAMPLE CHECK CARD (ACCOUNT ...0000)`) and the section
    stays. A table running onto the next page continues its section: the
    section's own header repeated on a later page keeps it at the page's
    top or under the page's running header: every line above it on the
    page (at most eight) must be a line of that header. Such a
    line is printed in the header band (the top lines, above the first row
    or figure) of both that page and the page the section's header was on
    (the client's name, the institution's; one cell of a band line of
    several cells will do), unless it headed a table of its own on that
    page; or it names the account (`Account Number: XXXX-1234`, its
    number) or the page (`Page 2 of 4`); or it is a date or period line
    (`March 1, 2024 - March 31, 2024`); or it says the table runs on
    (`Account Activity (continued)`). A line saying none of this (the
    client's name printed on later pages only, a figure such as `Total
    Account Value $6,025.00` in a value box on every page) is accepted only
    when another line of the block names the account or the page. A line
    holding a word naming a list of entries (`transactions`, `items`,
    `transfers`, `withdrawals`, `checks`, ...) is a heading, also beside a
    page label or the account number (`Other Transactions (Page 2)`,
    `Miscellaneous Items, page 2`, `Securities Lending Activity - Account
    XXXX-1234`), unless both header bands print it or it is the statement's
    title: a line naming the statement itself (`statement`, `report`) or
    the account or the page whose other words only name all of its entries
    (`activity`, `transactions`) or say `account`, `your`, `for`,
    `monthly` and the like (`Monthly Activity Statement`, `Statement of
    Transactions - Account XXXX-1234`, `Account Activity for XXXX-1234`,
    `Account Transactions - Page 2 of 2`). One other word makes it a
    heading, whatever it names (`Miscellaneous Items Report - Account
    XXXX-1234`, `Checks Outstanding Since Last Statement`, `Transactions
    After Statement Date`). Any other line there
    (`Miscellaneous Items`, `Other Transactions` at the top of the page, or
    under its running header) is a heading and ends the section, exactly as
    on one page; under a skipped section its rows are then held, not
    dropped. Known limit: a heading without a list word in a block where
    another line names the account or the page (`Bank Sweep Program` under
    `Page 2 of 4`, `Securities Lending` under `Account XXXX-1234`) is taken
    for the running header, like a client's name there, and its rows are
    read as the section's (a withdrawal under a dividends section is booked
    as a dividend, and the cash still reconciles). A short line over a header that names no column
    the header's other lines do not (it would only add unnamed columns) is a
    heading, never the header's top line, also under a running page header
    (`ATM Withdrawals`, `Wire Transfers`: the rows keep their whole
    description). In an account with a holdings or security table, such a cash
    table (date, description, amount) is activity even for the generic
    parser; its description naming no action is the row's memo, and in
    *Securities Transferred In/Out* with a quantity it moves those units. A
    row's description that is the name of a security the statement holds
    names that security (not a deposit or withdrawal), a holding printed by
    its name alone included. A trade or transfer of units (`buy`, `sell`,
    `reinvest`, `split`, `transfer_in`/`transfer_out`) with no symbol or
    CUSIP is named by its *Security Name* column alone (a security no
    longer held); a description column never names a security that way. A table printing
    minus signs has its unsigned amounts read as inflows (a refund, a fee
    rebate).
  - **Holdings rows**: the symbol is the ticker in parentheses ending
    the name (`APPLE INC (AAPL)`); `CUSIP: ...` and `ISIN: ...` lines under
    a row are its identifiers. Only a holding nothing else identifies (no
    symbol column, `(SYMBOL)`, CUSIP or ISIN) takes a ticker printed in
    front of its name: a first word of two to five capitals (`ZZAB
    Example Index Fund`, `ZZB.A ...`) before a name that is not all
    capitals, never for a bond priced per 100 or a name with parentheses
    (`ZZ Portfolio 2030 (Example Funds)`); in an all-capitals name the
    first word is the issuer's (`ZZQ NET LEASE INC ...`). A holding nothing
    identifies at all is kept, **named by its name alone** (an unlisted
    partnership, a 529 portfolio: `SecurityRef` with only a name, which must
    hold a letter or digit; names are bounded to 200 characters, whitespace
    collapsed and formula-neutralised); it counts in the holdings total like
    any other. A sentence under such a name printed in capitals (a word a
    title would capitalise written in lower case, no `(SYMBOL)`: `Valued
    from an independent estimate`) is a note on the holding, not part of
    its name; a line in title case (`Class R6`, `2031 (Example Funds)`) is
    the rest of the name, which may be all that tells two holdings apart,
    and a holding with a symbol, or one a `CUSIP:` line then identifies,
    keeps every wrapped line in its name. A name wraps until
    its `(SYMBOL)`, also when its next line reads like a section heading
    and ends the name with its `(SYMBOL)` (`EXAMPLE STRATEGIC` / `INCOME
    FUND (ZZSIX)`); without one such a line is the heading (`Dividends and
    Interest` under `ZZCD Example Bond Fund`), note lines (`— 30-day
    yield`, bond terms) are read for nothing; footnote markers in figures
    (`147.85A`, `$8,412.36 c`) are dropped and `unknown`/`N/A` cost basis
    is unknown. A price is per 100 of face value
    (`InvestmentPosition.price_per_100`) when quantity x price / 100 is the
    printed value and quantity x price is not; reconciliation checks it so
    and the import stores one unit's price. A trade's price is per 100
    (`InvestmentActivity.price_per_100`) when quantity x price / 100 is
    within a tenth of its amount and quantity x price is not (fees and
    accrued interest are in the amount), else as the statement prices the
    security's position; `investment_txn.price` is one unit's, so a bond
    is never valued at 100 times its price after a trade. A cash sweep
    printed as a holding whose value is the closing cash is the account's
    cash when the positions add up to the account value only without it.
  - **Cash figures**: `Beginning/Ending Core Account Balance` are the
    opening and closing cash; a cash balance line (`Cash & Sweep Balance`)
    under a `This Period | Last Period` header gives the closing and the
    opening cash. Dated cash balance lines (`Cash balance on 03/01/2024`,
    `... on 03/31/2024`) give the opening cash (the earliest) and the
    closing cash (the latest); one alone is the closing cash, or the
    opening cash when dated the period's first day. A statement printing no closing cash is checked with the
    account value less the holdings as its closing cash.
  - **Checks tables** of bank statements (`Check No. | Date Paid | Amount`)
    are read: each row is `CHECK <number>`, money out; a line of header
    words only (`Date | Balance | Date | Balance`) ends the table above it.
  - **Parser fallback** (`app/ingestion/parsers/registry.py`): when the
    chosen parser reads nothing usable, the next-best parsers scoring at
    least 0.35 are tried in turn (warning `parser_fallback:<name>`).
- **Several accounts in one PDF** (a statement "of all accounts", a bank's
  combined statement; `app/ingestion/parsers/accounts.py`, for any
  institution):
  - **Account headings**: `Account # Z12-345678`, `Account Number:
    123-456789`, `Account XXXX-4821` at the start of a line or cell, after
    at most an all-capitals title, or an all-capitals title ending in a
    number such as `INDIVIDUAL - TOD Z12-345678`. They are looked for in
    each **cell** of a line, so in a page's header band (its first eight
    lines above its first dated row, and its last three) a cell beside an
    amount is a heading: `$103,351.18 | Account 111-111111`, `Account
    Value: $142,413.12 | Account 222-222222`. Lines of the header band
    naming two or more accounts beside amounts are an overview, not
    headings. A heading mid-page counts only on a line without amounts and
    only for an account the account index lists (a combined statement
    starting its next account half-way down a page). A mention never
    switches the account: `Trans. from account 222-222222` in a transfer
    row, a dated row in the header band, a line with an amount mid-page,
    a description wrapped under its row (an undated line at the row's
    spacing starting at one of the row's text columns after its date, such
    as `TO SAVINGS ACCOUNT XXXX-2468` or `Account XXXX-2468`, anywhere on
    the page, also in its last lines and for an account the index lists;
    a heading centred or right-aligned straight under a row starts at no
    such column and still counts), a title that reads as a transaction's
    description (a word no product name carries, such as `ACH`, `XFER`,
    `PMT`, `WIRE`, or `TO`/`FROM` leading the title, ending it or followed
    by an account noun: `TO SAVINGS`, `ONLINE TRANSFER TO ACCOUNT`), or a
    number shaped like a date (`2026-07-31`) or a telephone number
    (`1-800-555-0100`, a toll-free area code). Product titles sharing
    words with descriptions are headings: `CERTIFICATE OF DEPOSIT`,
    `ONLINE SAVINGS`, `TRANSFER ON DEATH INDIVIDUAL`, `UNIFORM TRANSFERS
    TO MINORS ACT`.
  - **Account index**: a table whose header names an account number column
    (`Account Number`, `Account #`, `Account No`) and a name or type column
    (`Account Type/Name`, `Account Name`), such as *Accounts Included in
    This Report* or *Your Accounts at a Glance*, is the document's map of
    its accounts: each row's last four digits and printed name, the
    all-capitals group lines between the rows (`PERSONAL RETIREMENT`,
    `EDUCATION (529) ACCOUNTS`) qualifying the rows under them, an owner's
    name printed before every account's name dropped. The name, then the
    group, give the account's type and subtype (`account_kind`, through
    `app.domain.accounts.subtypes.guess_subtype`; bank words such as
    checking or savings win over a brokerage subtype, a retirement or
    education plan's own words over a bank word): `StatementAccount.name`,
    `account_type` (`brokerage`/`retirement` for brokerage statements,
    `checking`/`savings`/... for bank ones) and `subtype`, which account
    proposals use. The index's page numbers are never used (layouts print
    wrong ones); the headings decide.
  - **Pages**: a page whose headings name one account belongs to it; on a
    page naming several, the lines before the first heading continue the
    previous page's account and each heading starts its account; a page
    without a heading continues the account before it; pages before the
    first heading (an overview) belong to no account and only give the
    period, currency and "as of" date to fall back on, and a table row
    there belongs to no account (skipped, warning `rows_outside_account`).
    Each statement records its `first_page` and `last_page`.
  - **Statements**: two or more accounts give one statement per account
    with its own last four digits, name, type, pages, rows, totals and
    reconciliation. In a brokerage report (`brokerage_statement_generic`
    and the institution parsers built on it) an account with holdings or
    activity tables is an `InvestmentStatement`; one without them but with
    a bank transaction table (a combined statement's checking or savings)
    is read by the table statement parser into a bank `Statement`; one
    with neither gives no statement (`account_without_tables`). A bank
    statement of two or more accounts with a transaction table each
    (`statement_generic`, the card and institution parsers) is likewise
    one `Statement` per account. A document naming one account is one
    statement, read as before (its last four digits from the first account
    number, else from the heading); a lone brokerage account also takes
    the name, type and subtype its heading's title or the index prints
    (`HEALTH SAVINGS ACCOUNT`: `retirement`, `hsa`) for its account
    proposal, while a lone bank account keeps the parser's type and no
    name. Each statement is imported or held on
    its own: one account that does not reconcile holds only itself, and a
    line given to the wrong account breaks that account's reconciliation,
    so it is held, never imported silently. Only last four digits are kept.
- **Reconciliation** (`reconcile_investment`, SECURITY.md C12), for every
  source: each position's quantity x price = market value (within half a
  unit of the price's last printed decimal per unit plus 0.005; a price
  per 100 of face value divided by 100), the positions add up to the
  printed holdings total, opening cash + activity = closing cash (no
  closing cash printed: the account value less the holdings), holdings +
  closing cash = the printed account value. Any
  failure is `mismatch`; none checkable is `unavailable`. The parser caps
  every row and position of a `mismatch` at 0.5 (`unavailable`: 0.8) and a
  position whose value disagrees at 0.4; values read in an institution's
  declared format score 1.0, the generic parser 0.95, OCR scales them.
- **Commit** (`app/domain/investments/statement_import.py`, called by the
  structured importer): the account as for bank statements (document's,
  chosen on re-run, else last four digits + institution among the
  accounts the uploader may edit) and of type brokerage or retirement in
  the statement's currency. In a document of several accounts each
  statement (brokerage, and the bank statements of a combined statement:
  a lone bank or brokerage statement beside the other kind is never taken
  for the document's only account) is matched by its own last four
  digits; the document is linked
  to no account (the pipeline undoes an account the classifier matched; one
  chosen at upload stays), and an account chosen on a re-run takes only the
  statements with its own last four digits or, when it records none, the
  single statement nothing matched, never several accounts at once. No account, or one that does not
  fit, raises `unknown_account` (`account_not_found`, `account_ambiguous`,
  `account_type_mismatch`, `currency_mismatch`). A
  statement that does not reconcile, or has any row or position below the
  threshold, writes **nothing** (investment rows have no pending state) and
  raises `low_confidence_field` `investment_reconciliation_failed` (checks,
  failed checks, mismatched positions, cash and totals, the statement's
  index and key (`statement_key`), the account's currency and
  `differences`: per failed check
  `printed`, `computed` and `difference` = computed - printed, `count` for
  `positions`) or `investment_low_confidence`. Real statements do not
  always add up on their own (rounding, items the issuer leaves out), so
  the review queue offers **Import as read** for an
  `investment_reconciliation_failed` item, and for an
  `investment_low_confidence` item whose only doubt is `reconciliation` (a
  document statement with nothing to check against). The answer is for
  that statement only: its key is a digest of the statement exactly as
  read (`app/domain/imports/statement_answers.py`), the resolution records
  it with the item's account, and every import of the document from then
  on (the `apply-mapping` re-run the answer queues, any other re-run
  whoever starts it, and the pipeline when the document is processed
  again) puts the statement on that account wherever it now sits in the
  document and imports it there as read. Another statement, even of the
  same account, and the same statement read differently (a reprocess, a
  lesson, a parser change) are checked on their own figures and held with
  their own items. Items raised before statements had keys offer no
  "Import as read" (`409 review-item-stale`); reprocessing raises current
  ones. The statement is then imported as printed (activity, opening positions,
  prices, cash rows, its statement record) but writes **no balance
  snapshot**: the account's value is never taken from totals that did not
  add up. Any other doubt (a guessed date, an unread figure, a date before
  1900) still holds it. The answer is recorded in the item's resolution
  (`import_as_read`, account, statement, `statement_key`, failed checks,
  `snapshot: false`) and audited (`review.resolve`, `investment.import`
  with `accepted_as_read`); a later "Import anyway" about the same
  statement keeps it, as "Import as read" keeps an earlier "Import
  anyway". A statement with any date before
  1900-01-01 is held the same way (`investment_date_out_of_range`,
  SECURITY.md F8); OFX with nothing to check is imported like
  an OFX bank statement. Otherwise: securities by CUSIP, ISIN, then symbol,
  then by name (the same words, case and spacing aside, after Unicode
  NFKC; never a similar name). By name only a security in the account's
  currency is taken, looked for in turn among those the account holds or
  traded, those another account at its institution holds or traded, and
  those no account holds or traded (added by hand): a generic name in
  another plan is never the same security, and an account keeps the
  security it holds when another of that name appears. A security printed
  by its name alone takes, in the first of these groups with any security
  of that name, the one without identifiers, else the only one, else (or
  with none) a new security **without a symbol**; one printed with
  identifiers takes by name only a security that has none (created in the
  account currency; a CUSIP, ISIN, and a symbol on a security without
  one, are filled in from later statements). Two positions of one
  statement that take one security by name alone at different prices are
  different securities nothing tells apart: the statement writes nothing
  and raises `investment_same_name_securities` (`names`);
  activity as `investment_txn` with `import_hash` = sha256 of
  `perfin-invest-v1`, account, trade date, type, security, units, amount
  and the running index among identical rows (the same month as OFX and
  PDF is imported once). A row the account already records from another
  document under another date (same type, security, units and amount; the
  two share a trade or settlement date, or one has no settlement date and
  they are at most 5 days apart) is the same trade dated differently by
  another format and is **not written**: one `low_confidence_field` item
  per statement (`investment_possible_duplicate`: `account_id`, `rows`,
  `duplicate_of` ids) says so, and a trade that really happened twice is
  added by hand. Cash deposits/withdrawals as ledger transactions
  on the account through the structured importer. Units the history does
  not explain become one opening `transfer_in` per security on the
  period's first day (memo `Opening position from statement`, cost per
  unit from the printed cost basis, else the price); history holding more
  than reported raises `position_mismatch`. Lots, `lot_links` and holdings
  are then written by the investments lot engine
  (`service.replay_position`); a history it refuses rolls the statement
  back (savepoint) with `investment_lot_conflict`. Position prices become
  `price` rows (`source = statement`) unless another source has that day;
  holdings + closing cash on `as_of` is a `statement` balance snapshot when
  reconciled. New rows of a statement older than activity already recorded
  are not written (`investment_out_of_order`): import statements oldest
  first. Audit `investment.import`; events `investment.imported` and
  `balance.updated` (both with `account_id`).

#### Brokerage activity exports (CSV, TSV, XLSX)

A brokerage's "Activity" export (Fidelity's `Run Date, Account, Account
Number, Action, Symbol, Description, Type, Exchange Quantity, Exchange
Currency, Currency, Price, Quantity, Exchange Rate, Commission, Fees,
Accrued Interest, Amount, Settlement Date`, and layouts like it) is
investment activity, not a cash ledger. The structured importer
(`import_extraction`) reads table 0 of a CSV, TSV or XLSX with
`app/ingestion/parsers/brokerage_table.py` (in the worker, on the stored
table; never a PDF's page table) before it looks for a column mapping:

- **Recognised** when a header among the first 11 rows names a date (`Run
  Date`, `Trade Date`, `Date`; a settlement date is read as the settle date
  and is not enough alone), an action (`Action`, `Activity`, `Transaction
  Type`; Fidelity's `Type` column of `Cash`/`Margin` is not the action), an
  amount, and a symbol or a quantity column (so a bank export typed
  `Deposit`/`Withdrawal` never qualifies), and at least half of its data
  rows have an action `activity_type` knows. Optional: settlement date,
  symbol (a valid CUSIP there is the CUSIP), description (the security's
  name), quantity, price, commission, fees, currency, account number
  (`Account Number`, else an `Account` column whose cells carry digits).
- **Rows**: one-cell rows (blank lines, disclaimer paragraphs, the "Date
  downloaded" line) are notes. A row is typed by its action (`YOU BOUGHT`,
  `YOU SOLD`, `REINVESTMENT ... (Cash)`, `DIVIDEND RECEIVED`, `INTEREST
  EARNED`, `LONG-TERM CAP GAIN`, `DIRECT DEPOSIT`, `Electronic Funds Transfer
  Received/Paid`, `FEE CHARGED`, `FOREIGN TAX PAID`, `CONTRIBUTION`,
  `DISTRIBUTION`, `TRANSFERRED FROM/TO`: units when the row names a security
  and a non-zero quantity, else cash; Fidelity's cash-management and
  retirement wording: `DEBIT CARD PURCHASE`, `CHECK PAID`, `BILL PAYMENT`,
  `DIRECT DEBIT` as withdrawals, `PARTIC CONTR`, `EMPLOYER CONTRIBUTION`,
  `RETURN OF CAPITAL`, `CASH IN LIEU` as deposits, `MARGIN INTEREST` and
  the tax withholdings as fees, `REDEMPTION FROM`/`PURCHASE INTO CORE
  ACCOUNT` as a sale/buy of the core fund). `JOURNALED`, `TRANSFER`,
  `ROLLOVER`, `CONVERSION` take their direction from the row: units in or
  out by the quantity's sign when the row moves a security, else a deposit
  or withdrawal by the amount's sign; `EXCHANGED`, `ASSIGNED`, `EXPIRED` are
  a buy or sale by the quantity's sign. Without a sign such a row is not
  typed. An action no phrase types is
  **skipped and counted, never guessed** (its first three words, digits
  removed, are listed in the review item: `unknown_actions`); so is a row whose date or amount
  does not read, a buy/sell/reinvestment/transfer of units without a symbol
  or a quantity, a split without its ratio, or a row in another currency
  than its account's other rows. `Amount` is kept as printed (the cash
  effect: negative is cash out); a blank amount is accepted only on a
  transfer of units (no cash moves). Quantities are printed signed and
  carried positive (the type gives the direction); `fees` = commission +
  fees; the action text is the memo. The extractors' formula guard (a
  leading `'`, SECURITY.md C7) is set aside only to recognise headers and
  actions and to parse dates and numbers: the security's name and the memo
  keep it, and a symbol that would be a formula is no symbol.
- **Accounts**: one `InvestmentStatement` per last four digits of the
  account-number column (one statement without such a column), period =
  first to last trade date, no positions, no cash balances. Only the last
  four digits are kept. A masked number (`Z2xxxxxxxx`, fewer than four
  digits) groups by the cell's text, else by the `Account` nickname, so
  accounts never merge; such statements carry no last four digits and each
  gets its own `unknown_account` item. The nickname (`Individual`, `ROTH
  IRA`) names the statement's account in memory (never in a summary or an
  item) and makes it a `retirement` account when it says IRA, Roth, 401(k),
  403(b), 457, SEP, SIMPLE, HSA, Rollover or Keogh. A Fidelity header
  (`Run Date, Account, Account Number, Action, Symbol`) names the
  institution, Fidelity Investments, which breaks last-four ties.
- **Scoring and holding**: an export prints no positions, cash or totals,
  so reconciliation is `unavailable`. The statements are a structured
  source like OFX (`source_format = "csv"`): every value comes from a named
  column of a delimited file, with no OCR or layout guess, so they carry no
  per-row confidence and `statement_import` imports them the way it imports
  OFX with nothing to check. The parser's one possible guess, the
  day/month order of an all-numeric date column whose days are all 12 or
  less, marks every row `low_confidence` (`trade_date`, confidence 0.6), and
  the statement is held (`investment_low_confidence`). `csv` is a
  worker-only `source_format` (`WORKER_STATEMENT_FORMATS`): a sandbox child
  answering with it is `bad_output`.
- **Commit**: `import_investment_statements` exactly as for an OFX
  `INVSTMTRS` (account by last four digits among the accounts the person
  may import into, each statement of a multi-account file on its own
  account, the document linked to none; buys, sells, reinvestments, income
  and fees as `investment_txn` rows with the `import_hash` dedupe; cash
  deposits and withdrawals as ledger transactions on the brokerage account,
  their action text the payee). A re-upload or an `apply-mapping` re-run
  reads the stored table again and adds nothing (every row matches its
  hash). An account no account matches is an `unknown_account` item
  (`investment`, `statement`, never the digits); "remember" re-reads its
  last four digits from the stored table. The run summary has
  `brokerage_table` (statements, rows, notes, unknown actions, skipped rows,
  warnings); skipped rows raise one `low_confidence_field` item
  (`lines_skipped`: count, `unknown_activity`, parser), a table none of
  whose rows reads an `unparsed` item (`activity_unreadable`).
- **Limits**: an export has no positions. A sale of units bought before
  its first row (with no earlier statement giving the account's positions)
  opens those units: one `transfer_in` of the shortfall on the security's
  first row date, memo `Opening position before this export (cost basis
  unknown)`, no price (the lot opens at zero cost), and a
  `low_confidence_field` item (`investment_opening_basis_unknown`: the
  securities, quantities, dates and transaction ids) asks for the cost
  basis, or for the older export or statement to be imported first. A
  security with a split in the export is left to the lot engine
  (`investment_lot_conflict`, nothing of that account written). The
  out-of-order rule applies (import the oldest statement or export first).
  Its rows are dated by the run (trade) date; a PDF statement of the same
  month that dates rows by settlement date has other `import_hash`es, so
  its trades are caught by the near-duplicate check instead (not written,
  `investment_possible_duplicate`). Cash deposits and withdrawals are ledger
  rows and keep the ledger's same-date near-duplicate check.
- **Precedence over column mappings**: the brokerage reading wins over a
  saved mapping for the same header and over the suggestion (no mapping is
  suggested or saved for such a file); the run summary names the saved
  mapping it took precedence over (`mapping_bypassed`). A mapping chosen
  explicitly for a run (`apply-mapping` with `mapping_id`, "use this
  mapping" in the review queue) imports the table as a cash ledger instead,
  unless it is an **activity-mode mapping** (`MappingSpec.mode =
  "activity"`): its columns (date, action, amount, symbol or quantity, and
  optionally security name, price, commission, fees, settlement date,
  currency, account number and account name) read the table as activity
  whatever share of its actions is known. A saved activity mapping for the
  header is used for a table not recognised on its own. The mapping
  editor's suggestion for a brokerage header is an activity mapping
  (`suggest_activity_mapping`, `sign_evidence: activity`, doubt
  `activity_type_unknown` when some actions are unknown); it is never saved
  on its own. An account chosen in the review queue for an investment
  `unknown_account` item takes that item's statement only
  (`apply-mapping` `statement`). A document read this way records
  `parser_version = brokerage_activity_table/1`, and the upload list offers
  the review queue rather than the cash-ledger column mapping.

#### Loan statements (Phase 5)

A loan or mortgage statement is one payment and the balances around it,
kept in `extraction.parse.loan` (`LoanStatement`: lender, the loan
number's last four digits only, currency, statement date, payment date,
principal, interest, escrow, fees, total received, previous and new
principal balance, interest rate as a fraction, next due date and amount,
reconciliation, per-field confidence).

- **Parser** (`app/ingestion/parsers/loan_statement.py`,
  `loan_statement_generic`, kind `statement`): recognised by "mortgage
  statement"/"loan statement" or at least four loan labels (principal
  balance, escrow, interest rate, payment received, next payment due, total
  payment received, loan number); reads labelled lines, the amount being
  the last one on the line (year-to-date and escrow-balance lines are left
  out). Synthetic layouts: `tests/fixtures/synth_loans.py`.
- **Reconciliation** (SECURITY.md C12): `parts` (principal + interest +
  escrow + fees = total received) and `balance` (previous - principal =
  new), within the reconciliation tolerance. Figures a check confirmed
  score 1.0, unchecked ones 0.8 (below the default threshold), all of a
  statement that does not reconcile 0.5.
- **Commit** (`app/domain/loans/statement_import.py`, called by the
  pipeline): the loan of the document's account, else the loan account
  with the last four digits (lender breaks ties) among those the uploader
  may edit (F3); none is an `unparsed` item `loan_unmatched` (describe the
  loan with `POST /loans`, then reprocess). A statement that does not
  reconcile, has nothing to check, has a field below the threshold, is in
  another currency or is dated outside the loan's life writes **nothing**
  and raises `low_confidence_field` (`loan_reconciliation_failed`,
  `loan_low_confidence`, `loan_currency_mismatch`,
  `loan_payment_out_of_range`). Otherwise a `loan_payment` with the split
  and `source_document_id` (reprocessing replaces the document's payment;
  the same day, principal and interest already recorded is not added
  again), `statement` balance snapshots of the previous balance (the day
  before the payment) and the new one (the payment day), then the loan
  account's `computed` balances are rebuilt. A printed rate other than the
  rate in force raises `loan_rate_differs` (the payment is still recorded).
  Audit `loan.payment_import`; event `loan.updated` (with `account_id`).

### 6. Match and deduplicate
- Each parsed transaction gets an `import_hash`. Existing rows with the same
  hash are skipped; near-matches (same amount and date, different payee text)
  are surfaced as `possible_duplicate`.
- Overlapping statement periods are expected (e.g. a pending transaction that
  posts in the next cycle) and handled by hash, not by date range.
- **Same statement twice** (`app/domain/imports/statement_records.py`,
  table `statement_record`): every statement the importer commits (bank,
  card, brokerage, a CSV table or activity export when it has rows) records
  its account, document, period, whether the period was **stated** (an
  OFX/QFX `DTSTART`/`DTEND`, or a period a document parser read from the
  page with confidence 0.9 or more; a CSV/QIF period is only the span of
  its rows), source format and closing figures (closing balance; cash,
  holdings and account value). A statement with a stated period that
  shares at least half of the shorter period with an imported, stated
  record of the same account and kind (bank or brokerage) from **another**
  document is held: nothing of it is written and a `possible_duplicate`
  item (`statement_overlap`: `account_id`, `statement` index and
  `statement_key`, `investment`,
  `period_start`/`period_end`, `other_document_id` with its
  `other_period_start`/`other_period_end`, `others`) asks. *Confirm*
  ("Same statement — skip") records it as `skipped`; *reject* ("Import
  anyway") records the answer for that statement and account and re-runs
  the import (the same path as "Import as read": the answer lasts for that
  statement on every later import and covers no other), and the row hashes
  still skip rows already recorded. Statements that only share
  their edges (one closing on the 31st, the next opening on the 31st) and
  overlapping CSV exports are left to the row hashes. Two OFX/QFX files are
  compared by their declared windows: the same `DTSTART`/`DTEND` is the same
  download again and is held, different windows (a bank's rolling "last 90
  days" downloads) are left to the row hashes; an OFX/QFX against a printed
  statement is compared like any other pair. A document that already has a
  record for the account is never held again, so `apply-mapping` re-runs
  and reprocessing stay idempotent, and a skipped statement stays skipped
  unless `apply-mapping` names its account in `allow_overlap` (that run
  only), which imports it (a "same statement" answer taken back).
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
  first rows so a preamble is found.
- **Suggested mapping** (`suggest_mapping` in
  `app/ingestion/parsers/structured.py`; deterministic, no model). The
  header row is searched among the first 11 rows (`skip_rows` is what lies
  above it). Every (column, role) pair is scored: an exact header word
  ("Amount", "Run Date", "Account Number") beats a whole word inside the
  header ("Exchange Currency" for currency) beats a substring; posting,
  booking, transaction, trade and run dates beat settlement and value
  dates. Pairs are assigned globally, best first, one role per column, so
  an earlier partial match no longer takes a role from a later exact one.
  A column only takes a role when its cells fit (dates parse in one format,
  amounts parse, currencies are three-letter codes, an indicator column
  holds direction words such as `DR`/`CR`/`Debit`, an account column holds
  several account-number-like values); an empty column never takes one,
  and a "Type" column of `Cash`/`Margin` is no indicator, nor is a column of
  direction words beside amounts that already carry a minus sign (it
  describes the row; the amount signs it). A running balance
  that agrees with only one sign convention sets `sign`. The suggestion is
  **confident** when the date and amount (or debit/credit) columns are
  named exactly, every row before the footer reads, the day/month order
  is not a guess and, with an indicator, every row's indicator is a
  direction word beside an unsigned amount (else the doubt
  `indicator_values_unknown`: a `CHECK` row would otherwise read as money
  in); it reports a score, per-role reasons (role, column, match
  kind, share of cells that fit) and its doubts.
- **Never-seen layouts.** When no saved mapping fits and the suggestion is
  confident, a CSV, TSV or XLSX whose person (the uploader, or who asked for
  the re-run) is a member or above is imported with it, and the suggestion
  is saved as an ordinary household mapping named after the header
  (`created_by` that person; audit `import_mapping.create` with `origin:
  suggested`; the run summary has `mapping_suggested`), so the next file is
  recognised by its fingerprint. A single signed amount column read as
  "positive is money in" on trust (no debit/credit columns, indicator or
  agreeing balance) is never written into a liability account (card exports
  often list purchases as positive): the mapping saved from such a
  suggestion while the account is still unknown is marked
  `sign_unverified`, and whenever it later meets a liability account (the
  re-run after a person picks the card, next month's file) the import
  waits as `mapping_required` (`auto_import: sign_unverified`) until a
  person saves the mapping's spec in the editor or chooses the mapping
  explicitly for the run. With one account-bound mapping among the fitting
  ones, it wins over household mappings (a member's upload may have saved
  one meanwhile) for an upload that names no account. Page tables of PDFs and images, drop
  folder and mail intake without a person, and doubtful suggestions are not
  imported on their own.
- **Mapping editor.** Otherwise the document gets an `unparsed`
  (`mapping_required`) item carrying the suggestion (`suggested_spec`, its
  facts, and `auto_import`: why it was not used): the mapping editor reads
  the stored table through `GET /imports/{document_id}/preview` (never
  re-parsed; prefilled with the same suggestion and showing its confidence)
  and saves it with `POST /imports/mappings`; `POST
  /imports/{document_id}/apply-mapping` re-runs the import from the stored
  extraction as the `imports.apply_mapping` job and resolves the items it
  answered. The next file with the same header is imported without asking.
- **Rows.** Mapped rows that do not parse (totals, rows without an amount)
  are skipped and listed in a `low_confidence_field` item; rows with at most
  one filled cell (disclaimer paragraphs, a "Date downloaded" line) are
  notes, neither imported nor reported. An empty currency cell is the
  account's currency; another cell that is not a three-letter code makes
  the row unreadable. A running-balance column gives the closing balance and
  a reconciliation check (each balance = previous + amount; mismatches are a
  `low_confidence_field` item).
- **Multi-account files.** A mapping with `account_column` (a brokerage
  "Activity" export lists an Individual, a Roth IRA and an HSA account in
  one file) imports one statement per account: rows are grouped by the
  last four digits of the cell and each group is matched like an OFX
  statement (`match_account` among the accounts the person may import
  into; the document's or the mapping's account only for a file with one
  account). An account chosen on re-run takes the group it was chosen for
  (the review item's `group`, passed on as `apply-mapping`'s `group`), else
  the groups with its own last four digits when it records them, else the
  one group that matches nothing when there is only one: one choice never
  pours several accounts into one. A group that matches none or several
  accounts is an `unknown_account` item naming the group and the digest of
  the spec that numbered it (`spec_digest`), never the digits; "remember"
  re-reads them from the stored table, and once the mapping's spec has
  changed the item is stale (`409 review-item-stale`; import again). The cell text is used in memory for
  grouping only: statements keep the last four digits, and review items,
  audit rows, events and run logs carry ids and counts. A brokerage
  "Activity" export with an action column never reaches this path unless a
  person chooses a mapping for it ("Brokerage activity exports"); the
  synthetic multi-account file the tests import here has its `Action`
  column named `Details`.
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
  household remembers at most 100 layouts (`409 fingerprint-limit`). A
  template taught for the layout is kept in the new version, and another
  layout of the institution that would replace a taught one needs
  `replace` (`409 layout-conflict`; see "Teaching PerFin a layout"). The
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

### Teaching PerFin a layout

When no parser reads a statement, or reads it wrong, the person shows
PerFin where things are ("Teach PerFin", `POST /documents/{id}/teach`;
`app/domain/review/teach.py`, `app/ingestion/parsers/template.py`). It is
offered from every review item about a page the person may see and from
the document page.

- **Selecting.** The page is shown; a click picks a word, a drag picks a
  box, and a list of the page's lines does the same from the keyboard (the
  chosen line is added with "Add this line", so moving through the list
  adds nothing). Each
  selection is named from a list that always ends with **Other — describe
  it** and **Ignore this**: account number, account name/type, statement
  start or end date, opening or closing balance, institution, transaction
  table or holdings table. A table gets a role per column (the mapping
  editor's roles; holdings add market value and cost basis), each list
  again with Other and Ignore. Next to each field the dialog shows what
  PerFin read so far.
- **This document.** Each selection is read from the stored words (a value
  the person types wins): the period, the institution (one of the
  household's, or created by name), the account and the balances are
  applied at once. A labelled table is read over the whole document and its
  statement replaces what the parsers read in the stored extraction; when
  no row of the document was imported yet, the normal import runs on it
  (reconciliation, deduplication, account matching by the last four
  digits, review items). Balances for a statement already imported go
  through the same correction as `POST /review/{id}/correct`.
- **Anchors, not values.** For each field PerFin derives an **anchor**: the
  label printed next to the value (`right`), above it (`below`), or, for an
  unlabelled period, the header band (`band`). A label or column header
  that holds any digit is refused, so no account number, date or amount of
  the document is ever stored (the same rule as the remembered header
  lines, which now also skip table headers holding digits). A label is kept
  only when reading it on this document finds the selected value again (a
  label printed twice over different figures, such as `Balance` on two
  summary lines, would read the first one next time); the value may be
  found on another page (a label in every page header, selected on page 2,
  reads the same value on page 1). A value with no such label applies to this
  document only (`no_label:<field>`).
- **Remember for this layout.** With "Remember this layout", the anchors,
  the tables (column headers and roles, the line that ends them), the lines
  to ignore and the notes are stored as the `template` of a household
  `parser_fingerprint` (migration `0011`) whose rules are derived from the
  header like "Remember this layout" (and must match the document). This
  document and up to 50 similar ones are then read again through the
  pipeline. One version per institution, kind and parser is used, so a
  template is never dropped silently: when the same layout is taught or
  remembered again, the earlier template is carried into the new version
  (what is taught now wins per field and per table kind; ignored lines and
  notes are added; the answer names what was `kept`), and when another
  layout of the same institution is remembered while a taught template
  exists, `409 layout-conflict` asks the person to replace it (`replace`).
  The dialog offers "Replace" whenever it remembers, so a line, note or
  label taught by mistake can be dropped by teaching the layout afresh.
- **The review item.** The item the dialog was opened from is closed only
  when the teaching answers it: a held statement (`reconciliation_*`) once
  it reconciles with the balances entered (its pending rows confirmed, like
  "Check and accept"); any item once the document is read again through
  the pipeline (reprocessing raises afresh what is still wrong); an unread
  document (`unparsed`) or an unknown account once the taught rows are
  imported; a new institution once it is set or the layout remembered; a
  low-confidence item once a fact it names changed (the period for a
  period item, the balances for a balance item). A fact that does not
  answer the item (an institution or an account set without rows imported
  for an unknown account, anything for a possible duplicate), a note or an
  ignored line leaves it open (warning `item_left_open`).
- **Reading the next statement.** When the classifier matches a household
  fingerprint with a template, the parse stage reads the pages with it
  after the parsers (`registry.parse_extraction(template=...)`): a table
  starts at every line holding its column headers in order (a header
  repeated on a later page continues it), words go to the column whose
  span holds their centre, a row needs a date and an amount (a holdings row
  a quantity and a symbol or name), description text right under a row
  continues it, ignored lines and lines after the end label are skipped,
  and a dated line without an amount is skipped with its box recorded. The
  anchors give the period (yearless row dates take its year), the account's
  last four digits and the balances, and the statement is reconciled and
  scored like a parser's (parser `layout_template/1`), so a template that
  misreads a statement is held back the same way. A template without
  tables only fills a parser's statement (the period always; the account
  and balances when there is one statement). Then the normal import runs.
- **Notes.** Free text from "Other" is kept on the template, in the review
  item's resolution and in the `document.teach` audit row for the
  household to read, without any word that holds a digit (an account or
  sort number typed into a note is not stored). PerFin never interprets it.

Per-reason fixes in the review UI: cost basis per unit for opening
positions with an unknown basis (`PATCH /investments/transactions/{id}`),
opening/closing balance for statements that do not add up ("Check and
accept": confirmed rows and a balance snapshot once it reconciles),
"Import as read" for brokerage statements held back by their figures (what
did not add up is spelled out per check: positions, holdings total, cash,
account value, with the difference; imported as printed, no balance
snapshot),
skipped lines highlighted on the page (parsers record the boxes of the
lines they skip; `lines_skipped` items carry them) with "Teach PerFin",
and "Map columns"/"Teach PerFin" for brokerage dates that could not be
told apart.

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

## Models in the pipeline: optional, local, suggestions only

By default PerFin calls no language model at any stage of ingestion. The
pipeline is deterministic, reproducible and auditable, and a crafted document
cannot talk its way into the ledger. A never-seen statement layout needs a
person once, through the column-mapping editor or "Accounts in this file".

The optional `ml` containers (ADR-0010, [ML.md](ML.md)) add a model on the
same host. When `PERFIN_ML_URL` is set (it must be a local address), the
worker asks it, after the classify step, about a document whose kind is
unknown or whose account nothing matched, sending only the extracted text
(`PERFIN_ML_MAX_CHARS`). The answer is checked (`app.ml.client.validated`:
known kinds, shape-checked fields, last four digits only when printed in the
text) and kept on the document as `confidence.ml`. It is used only as a
suggestion: an unknown kind selects a parser (whose own checks apply), and
proposed accounts and the review queue's account suggestions are filled in
from it, marked as the model's. Nothing reaches the ledger on its say-so, and
a failed or slow call leaves the document as it would have been.

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
`classify`, `parse` (per bank statement and per brokerage account its last
four digits, printed name, type, subtype and pages; the bank statements'
period and row counts; the brokerage accounts' period, positions and
activity counts, figures and reconciliation), and with `--household` a `match` read in a
rolled-back `READ ONLY` transaction). Nothing is written to the blob store or the
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

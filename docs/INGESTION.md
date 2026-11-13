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
five delimiters or more need only two, however many notes follow), or when
a table of four columns or more (three lines at least: a header and two
rows) stands under a few lines that carry fewer delimiters, the way a bank's
export opens with the account's name, masked number and date range
(`_rows_after_preamble`; the column mapping finds the header row,
`skip_rows`, and the account's last four digits are read from those lines
when they name one account, `preamble_last4`). Under such a preamble one
shorter row between two rows of the table's shape (the rest of a long
description printed on a line of its own) does not end the table, once the
header and a first row have been seen together. When the text has double
quotes that pair up, the delimiters are counted per logical record and
outside the quotes first (`_record_counts`: a quoted cell may hold the
delimiter, as in `"1,234.56"`, or run over up to 10 lines), then by
physical lines as before; either reading may show the table, so no file
accepted before is refused. OFX/QFX is also recognised when `<OFX>` opens
a line under `KEY:VALUE` header lines in any order (a header that leaves
`OFXHEADER` out or puts another key first; mail, whose headers have that
shape, is tested first) and when `<OFX>` is the root right under an XML
declaration; QIF may open with `#` comment lines. NUL bytes that pad the
very end of a file are not content: the sniff ignores them when its window
holds the whole file, and `decode_text` drops them; a NUL anywhere else is
refused as before. The whole-file markup scan and the control-byte rules
are unchanged. A text file stored as
unsupported by an earlier release is sniffed again when it is reprocessed,
so "Process again" reads it. A file
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
  with `annots=False` (C8). A cross-reference entry whose object is not in
  the file (common in files saved more than once) does not refuse the
  document: the inventory and the removal step over an object MuPDF cannot
  load, which holds nothing it could run. `scrub` stops at such an entry,
  so it is run again without its JavaScript pass and the module's own loop
  over every object disarms the actions, those numbered after the entry
  included. An action that is found and cannot be cleared refuses the
  document (`malformed_pdf`). A PDF that needs a password is `ok=false`,
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
  Tesseract's page layout analysis crashes natively on some scans and
  takes the sandbox child with it. After a `crashed` child on a PDF or
  image that uses OCR, the worker (`pipeline._extract`) reads the document
  again, each time in a new child with the same filter and limits: first
  with `ExtractOptions.ocr_psm = single_block` (mode 6, no layout analysis;
  warning `ocr_retried:single_block`), and if that child crashes too,
  without OCR (text layers and previews only; warning
  `ocr_disabled_after_crash`). In the last case the pipeline opens an
  `unparsed` item (`ocr_failed`) and the document ends `needs_review`
  whatever was imported from its text layers; an import re-run (an account
  chosen, a mapping applied, the rows of a table taught in "Teach PerFin")
  does not resolve that item. Any other sandbox
  error fails the document as before, and when both retries crash the
  first crash is the error reported.
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
  neutralised so a later CSV export cannot carry formula injection. Two
  departures from RFC 4180 that real exports make are read
  (`delimited.py`). Blanks between a closing quote and the delimiter or
  the line end (`"12.50" ,`) are dropped before the strict parse, in step
  with the reader's own quote state; when the blank is a tab on every
  line the sniffer calls the file TSV, and the reader sees that the tabs
  are blanks and reads with the real delimiter. A file whose cells are
  mostly written `'like this'` is read with `'` as its quote character
  when the sample reads cleanly that way and is a table at least as much
  as with `'` as text; an apostrophe inside a cell, at the start of a few
  cells or at the two ends of a row stays text. Anything else after a
  closing quote still fails the file (`malformed_csv`), and the formula
  guard runs on the cell values once their quotes are off.
- OFX/QFX/QIF: dedicated parsers; XML external entities disabled
  (`ofx.py`, `qif.py`).
  - *Route and encoding.* A file that opens with `<?xml` or `<?OFX` is
    parsed as XML (`defusedxml`; a DTD or an entity refuses it,
    `xml_forbidden`). The bytes are decoded before parsing and the XML
    declaration is dropped, so an encoding the file declares wrongly, or
    one the parser does not know (`USASCII`), cannot fail it: UTF-8 when
    the bytes are UTF-8; else the declared encoding when it is one of a
    fixed list of one-byte character sets (`DECLARABLE_ENCODINGS`:
    cp1250-1258, ISO-8859-2 to -16, KOI8-R/U) and every byte is a
    character of it; else cp1252, then latin-1. No other codec a
    declaration names is ever applied.
  - *An OFX 2 opening over an SGML body* (not well-formed XML, no tag that
    carries a value closed by an end tag, `</OFX>` present) is read by the
    SGML tokenizer once its leading `<?xml ?>` and `<?OFX ?>` instructions
    are removed. Nothing else is removed, so any other declaration still
    refuses the file. Damaged XML (leaves closed, not well-formed) and a
    file cut short (no `</OFX>`) do not fall back: they stay
    `malformed_xml`, so half an amount is never imported as if whole.
  - *SGML.* A tag printed with no value opens an aggregate only when its
    name has an end tag somewhere in the file or is an aggregate the
    readers walk; otherwise it is an empty leaf, so a value-less
    `<TRNTYPE>`, `<FITID>` or `<NAME>` no longer swallows the date and
    amount after it. A value its exporter escaped twice (`&amp;lt;`) is
    unescaped again, at most two more times. Bank transactions are the
    `STMTTRN` of every `BANKTRANLIST` of the statement and those directly
    under it (a stray `</BANKTRANLIST>` closes the list early in some
    exports).
  - *No record is dropped without a count.* A transaction with no
    readable date or amount, a `STMTTRN` nested where the reader does not
    look or under no statement at all, an investment activity that cannot
    be read or is of a kind with no reading (margin interest, a return of
    capital, a journal entry; `investment_activity_kind_unsupported`
    names the reason), known activity left beside `INVTRANLIST` by a stray
    end tag, a QIF record with no readable date or amount and a last QIF
    record with no `^` are counted. Each statement that lost records adds
    one warning `transaction_skipped:<n>` (kept per statement, the surplus
    folded into the last when the result has no room for more, so the
    counts add up to the records lost); it replaces
    `transaction_skipped_invalid_date_or_amount` and
    `investment_activity_skipped_invalid`. The records of a QIF section
    that is skipped whole (`Invst`) are not counted:
    `section_skipped:<name>` is all that says so. The importer turns the
    counts into a review item ("Match and deduplicate", "Records the
    extractor did not read").

  OFX investment statements (`INVSTMTRS`, `extract/ofx_invest.py`, extractor
  `ofx/2`) are read from the same hardened tree: positions (`INVPOSLIST`),
  activity (`INVTRANLIST`: buys, sells, income, reinvestments, expenses,
  transfers, splits, cash `INVBANKTRAN`), closing cash (`INVBAL/AVAILCASH`)
  and the `SECLIST` names. Two kinds of security are not priced per unit
  in OFX and are rescaled, and no other kind ever is: a bond's
  `UNITPRICE` is a percentage of par while its `UNITS` are face value, so
  a `POSDEBT`, `BUYDEBT` or `SELLDEBT` is marked `price_per_100`; an
  option's is the price of one share while its `UNITS` are contracts, so
  a `POSOPT`, `BUYOPT` or `SELLOPT` carries the price of one contract
  (`UNITPRICE` x `SHPERCTRCT` of the security's `OPTINFO`, else the trade
  aggregate's own, else 100). Brokers do not all follow the definition,
  so the value printed beside the price settles it: the rescaled reading
  is kept only when its product is strictly nearer to `MKTVAL` (a
  trade's `TOTAL`) than quantity x price as given, and the definition
  applies when no value is printed. Debt and option trades name their
  asset class when the file has no `SECLIST`. The contract size is folded
  into the stored price; the protocol has no field for it. A statement
  that carries `INV401K` or
  `INV401KBAL` is a `retirement` account of subtype `401k` (nothing inside
  those aggregates is read), any other a `brokerage` account; only the
  account's last four digits and
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
- **Institution of a never-seen layout** (`institution.py`): when no
  fingerprint matches a statement, the header lines (the first 15 of page
  1) are read for the name of who sent it: a run of capitalised words
  ending in a word that says what kind of company it is (`Bank`, `Credit
  Union`, `Building Society`, `Savings and Loan`, `FCU`; weaker: `Financial`,
  `Brokerage`, `Securities`, `Investments`, `Insurance`, `Mortgage`, `Card
  Services`...), with its corporate form (`N.A.`, `FSB`, `Inc.`) and, for a
  bank, `of <Place>`. `Northwind Bank, N.A.`, `Issued by Meridian Card
  Bank`, `Thistle & Wren Building Society` are names; `Your Financial
  Summary`, `Mortgage Statement`, `Federal Credit Union` (nothing
  distinctive left), a line with digits and a payee in a transaction row
  (never in the header) are not. Confidence 0.7 (0.6 for the weaker
  words), kept as `confidence.institution_name`. It is a suggestion: it
  resolves to one of the household's institutions by name
  (`match.institutions`), breaks a tie between accounts sharing their last
  four digits, and is offered as the institution when an account is set up
  from the file ("Accounts in this file"). It never creates an institution
  by itself. So the first statement of a new bank proposes the bank, and
  the next one is recognised by it.
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
  (`app.domain.tax.forms`; US: W-2, 1099-INT/DIV/B/MISC/NEC/R/G/SA,
  SSA-1099, 1098,
  1098-E, 1098-T, 1095-A/B/C): one form per `Form <type>` heading (a
  consolidated 1099 gives several, numbered per type; identical copies are
  kept once), the tax year (`Tax year 2025`, `For calendar year 2025`, or the
  year on the heading line), amount boxes by label (a cell starting with the
  box id and the first words of its label; the amount below it in its
  column, or right after it; "Composite statements and the SSA-1099" below
  has the other ways a heading and a box are printed), W-2 box 12 per code (`12_D`), the sum of the
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

  **Composite statements and the SSA-1099** (`tax_forms` version 2; the
  module doc has every rule). A broker's composite 1099 heads its forms
  with the year and the type and no word "Form", prints two forms side by
  side, and joins label, dot leaders and amount in one string:
  - *Headings.* A cell that starts with the year and the type (`2025
    1099-DIV Dividends and Distributions`) is a heading and gives the tax
    year, when the type is followed by the form's title or by nothing; a
    year and a type followed by other words (`2025 1099-B Summary`, `2024
    1099-INT corrections were issued separately`) is not. A mention is no
    heading either: a contents line (it ends in a page number and holds no
    amount), a line naming three or more forms, a `Form <type>` inside
    parentheses.
  - *Side by side.* Two headings of different forms on one line open two
    sections, cut at the second heading's left edge; each reads only the
    cells on its side. A later heading on that page that fits inside one
    of those columns starts its form in that column only; the columns to
    its right are read by nobody until a heading opens them again.
  - *Boxes.* A cell is cut at every run of three or more dots, so `Total
    ordinary dividends......1,840.25` is a label and its amount, and such
    a label is read from its own line only (the next amount down the page
    is not its box). A box id printed apart from its label (`1a` | `Total
    ordinary dividends`) is read with it. The word `Box` before an id is
    part of the label (`Box 3. Benefits Paid in 2025`); a box named inside
    a sentence (`Description of Amount in Box 3`) is no label. A cell
    that says the box is empty (`NONE`, `N/A`, a dash) ends the search
    for its amount.
  - *SSA-1099.* Boxes 3 to 6 are stored. Boxes 1, 2, 7 and 8 identify
    the beneficiary and are never read, and the form has no issuer box,
    so no issuer name is kept.
  - *Detection.* 0.97 when any section of the document holds amount-box
    labels of its form, 0.3 for a heading with none; identity labels are
    not counted.
  - *Redaction* keeps a word made of label, leaders and amount when each
    piece would be kept, so the stored pages read the same on "Process
    again".
  - *Not read:* the right-hand boxes of a full-width form printed below a
    side-by-side block when its short heading starts in the left column
    (the form is stored with the boxes of its left side), and a right
    column whose rows start left of its heading.
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
  (warning `parser_fallback:<name>`). **A statement is never a receipt**:
  before any of this the receipt parser's score is set to 0 for a document
  the classifier calls a statement, and for one with a transaction table
  header on its pages that the classifier does not call a receipt (a page
  of activity that ends in "Total" lines is not a purchase). A document
  classified as a receipt keeps its parser. The period the classifier read
  from a printed label (at or above its field threshold) and the day the
  file arrived are handed to the parsers on the view (`view.hints`), with
  the day the PDF says it was made (`view.created`); "Period and year"
  below says what the statement reader does with them.
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
  variant (`card_statement_generic`) needs card vocabulary on page 1,
  outside the transaction rows and their wrapped lines (a checking
  account's `CARDMEMBER SERV WEB PYMT` or `CREDIT CARD PAYMENT` row is
  no cue): one of "card ending", "credit card", "card statement",
  "minimum payment", "credit limit", "corporate card", "charge card", or
  two of the weaker "new charges" and "cardmember"/"card member"
  ("Parser choice" below has the limits).
  Numeric rows with and without a year in one table are both read.
- **Bank and card statement layouts** (`table_statement.py` version 7,
  `statement_header.py`; the two module docs have every rule and its
  limits). What the reader does with the ways statements are printed:
  - *Headers* (`statement_header.read_header`). Two headings printed so
    close that they are one cell (`Date Transaction Description`,
    `WITHDRAWAL BALANCE`) are read as the fewest known headings that use
    every word of the cell (a cell with a word left over is not split:
    `Amount Enclosed`). A second date heading is the posting date. A
    heading PerFin does not know right of the money (`Category`,
    `Reference`) is a column whose cells are claimed and not read. When a
    line names a date and a money column and leaves exactly one cell
    unknown between them, that cell is the description (`Memo Text`),
    unless it names figures (`DAILY ENDING BALANCE`). A money heading may
    carry a unit (`Amount (SGD)`); on a scanned page a one-word heading
    may be one letter off (`ithdrawal`). A header printed over two or
    three lines is one header when the lines together name more columns
    than any of them alone (`stack_headers`). These rules apply to a line
    of headings only: a line with an amount is no header. A line of
    heading words that heads a table PerFin does not read (`Date |
    Balance | Date | Balance`) ends the table above it; a title or a
    row's wrapped text made of heading words does not.
  - *Rows off their columns.* A figure under no heading goes to the
    numeric column that starts left of it when that column has none on
    the line; a date printed so close to the description that both are
    one cell is split off it, on a line with a figure. With a figure in
    both the debit and the credit column a zero counts as none; two
    amounts say nothing and the row is counted as skipped. Amounts are
    read with a `DB` marker, a sign or a currency symbol a space away
    from the figure (`-$ 12.00`) and one footnote mark after it.
  - *Sections* (`SECTION_TITLES`). A section title (`Deposits and
    additions`, `Electronic withdrawals`, `Checks paid`, `Payments and
    credits`, `Purchases`, `Fees`, ...) types its rows as before and now
    also says which way their money went, for a bank account, a card or
    both. It is read between the rows of a table and above a header. Only
    an amount of the `Amount` column printed with **no sign at all** takes
    that direction: a minus, a plus, brackets, `CR`/`DR`/`DB` and a debit
    or credit column always decide first. A title naming both sides
    (`Deposits and withdrawals`) gives none. A header without a title on a
    later page, with the columns of the table being read, is that table's
    header printed again, and direction and type carry on, unless the line
    over it speaks of a section. A `Total ...` line after a section's rows
    is that section's total: a section whose rows do not add up to it
    holds a statement that would otherwise reconcile (warning
    `section_total_mismatch`), and when what it lacks is the whole
    difference only its rows are left in doubt. A total with more dated
    rows of its section after it is a subtotal (one per cardholder): the
    total after the last row is checked, against all the section's rows or
    those since the last subtotal. A total under rows that each agree
    with their printed running balance, on a statement that reconciles,
    is the total of something else and holds nothing. A total says how
    much, never which way: one that agrees vouches for no row.
  - *A date printed once per day.* An undated line with description text
    and one figure in a money column, close under the row above it, takes
    that row's date when a dated row follows in the same table (or, at the
    table's end, when the statement already has two such rows); otherwise
    it stays what it was, a summary line. Its date scores at most 0.9
    (warning `dates_carried`).
  - *Newest first.* Rows printed from the latest day back are read in the
    order they happened, so the running balances agree (warning
    `rows_newest_first`); only when a running balance is printed or every
    row stands in one section.
  - *Titles.* The line printed over a header, and the title of a block
    (`Account summary`), is never the second line of the row above it.
  - *Balances.* A dated line whose description starts with an opening or
    closing label is a balance, whatever column its figure is in, and
    never a transaction (a payee that starts with a label, `New Balance
    Outlet 114`, is a row). `Previous statement balance`, `Balance last
    statement` and `Prior balance` open a statement; the longest label on
    a line is the one printed, and a closing label after `previous`,
    `last` or `prior` closes nothing. A label printed without its figure
    takes the figure printed alone on the line next to it (confidence
    0.9). A dash standing apart between a label and its figure (`Ending
    Balance - $992.50`) is a minus or only a separator: the reading the
    rows agree with is taken (the other balance and the rows' total, or
    the running balance beside the first or last row), and a balance
    nothing settles is not given at all, so the statement is held
    unreconciled instead of recorded with the wrong sign. `-$992.50`, and
    `- $992.50` in a cell of its own, are negative as printed.
  - *Period and year.* The period is looked for in this order, each step
    only when those before it found nothing: (1) a labelled period (also
    `Statement For`, `Billing Cycle`, `Period Covered`, `Statement
    Covers`, `Activity From`, `Activity For`) or the header band; (2) the
    period the classifier read (warning `period_from_classifier`); (3) a
    statement date (also `Statement Issued`, `Statement Ending`,
    `Statement End Date`); (4) one range with a year printed once on page
    1 above the first table without a label, of at most 100 days, with
    half the rows or more dated inside it (warning
    `period_unlabelled`; believed at 0.75, so it dates the rows and is no
    stated period for the duplicate guard); (5) an **assumed year**. Rows
    that print a month and a day and no year, on a statement with none of
    the above, used to be skipped. They are now placed in the latest run
    of the calendar that ends on or before the earlier of the day the PDF
    says it was made and the day the file arrived (`view.ceiling`):
    December rows before January rows, never split around a date printed
    in a notice. Every such date scores 0.6, so each row is written
    pending, and the warning `year_assumed:<YYYY>` raises one review item
    that names the year ("Review queue" below). Such a statement gives no
    closing balance (it is still reconciled with the printed figures), so
    no balance snapshot is written for a day nobody confirmed. Without
    that day, with
    rows that mix dates with and without a year, or with rows spread over
    more than 184 days, the year-less rows are counted as skipped as
    before.
  - *Parser choice.* A parser written for one institution scores above 0
    only when **every** phrase of its fingerprint is on page 1 (1.0 with
    a table, 0.54 without): one phrase alone (`card ending in`) is what
    any issuer prints. The generic card reader (0.45 with a table, above
    `statement_generic` at 0.4) counts its card words only on page-1
    lines outside the rows of at most eight words, so a fee note that
    mentions a credit card chooses nothing; and such a line that names a
    deposit account (`checking`, `savings`, `deposits`, `withdrawals` as
    one of the first two of at most three words: `Business Checking`,
    `Total Deposits`) rules the card reader out. A line that only
    mentions one (`AutoPay from Checking ending 7731`) does not.
  - *What is counted.* A line that is found and not imported is counted
    in `skipped_lines`, with its box in `skipped_regions`, and raises the
    `lines_skipped` item, so the document waits instead of ending
    `processed` with fewer rows than it prints: a dated line without a
    readable amount; an undated line with a figure in a money column that
    a dated row follows and that is no carried-date row; rows above the
    first account or share (`rows_outside_account`); date-led lines
    ending in an amount under a section title and no header
    (`rows_without_header:<n>`; a titled section without a header is not
    read), and the same lines with words on them after a header of
    heading words that ended the table without being one PerFin reads
    (`Date | Balance`; figures by day under it have no words and are not
    counted); and each dated entry right of the left group of a table
    printed two or three times across the page (`Number | Date Paid |
    Amount` three times: the left group is read, the others are counted,
    `table_across_unread:<n>`, and the statement cannot add up and is
    held). `Date | Amount` pairs printed across are balances by day: not
    read and not counted.
  - *Credit-union shares.* The shares of a member statement are named by
    the title over each table, also when the title carries a figure
    (`PRIMARY SHARE ID 0000 | Dividends Paid YTD: | 4.12`) or starts with
    the number (`ID 0040 - SHARE DRAFT`).
  - *Not read* (counted or held, never guessed): titled sections without
    a header row, one field per line, decimal commas and month names
    other than English, two accounts with the same masked number,
    description lines printed above their dated line, and loan
    sub-accounts of a member statement.

  Parser versions are part of `document.parser_version`
  (`statement_generic/7`, `card_statement_generic/7`, the institution
  parsers `/5`). A document stored by an earlier version keeps its rows
  until it is read again; "Process again" reads it with the current
  rules, which can change its rows, its period and whether it waits in
  review.
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
    is older. A label's value runs to the next label on its line (both
    dates on one line), and a label after `Next` (`Next Closing Date`)
    is the next statement's, never this one's. Yearless rows take the
    latest year on or before the statement date (December rows of a
    January statement are the year before; no row lands after it). When
    half the dated rows or more are printed with a year after the
    "statement date", it is none (the rows' span is the period,
    inferred); fewer such rows are outliers (a stub's dated line read as
    a row when the stub has no marker we know) and are skipped.
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
    `Return this portion`, `Detach and mail`, `Tear and return`, ...;
    with no amount, or starting with the marker) to the end of its page nothing
    is read (unless a table header follows on that page), so its dated
    `Amount Due` line is no row and its full account number is never the
    statement's.
- **Institution parsers** (`institutions.py`): subclasses for the fictional
  institutions of the shipped fingerprints (Sample National Bank checking,
  Example Federal Credit Union, Demo Card Services, Mock Savings and Loan)
  that pin the date formats, sign convention, currency and institution name,
  so values read in their declared format score 1.0. Each claims a
  document only when every phrase of its fingerprint is on page 1.
- **Payslip parser** (`payslip.py`, `payslip_basic` version 2, page 1
  only): employer, pay period, pay date,
  earnings and deduction lines, gross, total deductions, net; checked
  `gross - deductions = net`. Kept in `extraction.parse.payslip`
  (`kind = payslip`, the period on the document); no ledger rows. One that
  does not add up is a `low_confidence_field` item
  (`payslip_does_not_add_up`). How it reads what payroll services print
  (cells and columns through `labelled_cells.py`, shared with the periodic
  loan statement reader):
  - *Current and year-to-date columns.* Under a header line naming them
    (`Current | YTD`, `This Period | YTD`, in either order) a figure is
    the one in the period's column, never the year-to-date one. A label
    whose figures all stand outside the period's column is left unread, a
    single figure under a lone `Year to Date` header included. A figure
    under another header of that line (`Hours`, `Rate`) belongs to neither
    column. A label naming the year so far (`YTD gross pay`) is never
    read. With no header naming the columns the first figure after the
    label is taken.
  - *Blocks side by side.* A heading may be one cell of a wider line
    (`PAY | Hours | Rate | Current | YTD | DEDUCTIONS | Current | YTD`):
    the lines under it are split into blocks at each heading. Lines under
    *Deductions* (*Taxes*, *Withholdings*) are deduction lines, those
    under *Earnings* (*Payments*, *Pay*) earnings, down to the labelled
    total; a line starting with `Total` is never a line item. With no
    printed total the deductions total is the sum of the deduction lines.
  - *Labels over values.* A line of two or more labels with no amount
    takes each value from the cell under its label; a row naming the year
    so far is skipped.
  - *Period and employer.* `Pay period <date> - <date>`, else a start and
    an end label (`Period Beginning` / `Period Ending`), both or neither.
    The employer is the cell under an `EMPLOYER` heading, else what
    follows the word when it is a cell of its own or a colon follows it
    (never the rest of a heading such as `Employer Paid Benefits`), else
    the first line of the page.
  - The first reading of gross, net and the deductions total stands (a
    second figure under the same label further down does not replace it).
- **Receipt parser** (`receipt.py`, `receipt_basic` version 2; "Receipts"
  below): merchant, date and
  time, subtotal, tax, tip, discount, total, item lines, payment method and
  card last four, currency, each with a confidence. Kept in
  `extraction.parse.receipt` (`kind = receipt`, the date as the period).
- **Reconciliation** (`reconcile.py`, SECURITY.md C12): opening + sum =
  closing within less than one cent, and each printed running balance =
  previous + amount. A statement that has neither balances nor a running
  balance (`unavailable`) has every row capped below the threshold
  (`reconciliation` field). One that does not reconcile (`mismatch`) has
  only the rows left in doubt capped (`reconcile.doubtful_rows`,
  `Reconciliation.doubtful`): a row whose printed running balance is the
  previous balance plus its amount is vouched for (rows without a balance
  between two balances that agree are vouched for together); without
  running balances, the one row whose amount is the whole difference;
  otherwise every row. A printed opening or closing balance that is off
  while the running balance holds leaves no row in doubt. The importer
  imports the doubtful rows pending and the others as usual, raises a
  `low_confidence_field` item (`reconciliation_failed`/
  `reconciliation_unavailable` with opening, closing, total, difference,
  the rows whose running balance disagrees, `rows_in_doubt` and
  `rows_total`) and writes **no** balance snapshot from it. An amount confirmed by the
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
  listed per row (`low_confidence`). A date carried from the row above
  and a balance read beside its label score at most 0.9; a date in an
  assumed year scores 0.6. On a scanned payslip or loan statement each
  field is scaled by the OCR confidence of its own cells
  (`scaled_fields`: the lowest word confidence among the cells its label
  and value were read from), so one garbled word elsewhere on the page
  lowers no field it did not print. A value a check proved keeps its
  score whatever OCR said: a payslip's gross, deductions and net that add
  up, and the loan figures a passing check covers.
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
  no cash. Brokers' abbreviations are written out first (`expand_action`,
  whole tokens only): `Div`/`Divs` dividend, `Int` interest, `Mgmt`
  management, `Qual` qualified, `Cap` capital, `Yr` year, and `Pr` prior
  only before `Yr`; words that stand before a dividend without changing
  what it is are dropped (`Pr Yr Cash Div`, `Special Non Qual Div`). So
  `Qual Div` and `Cash Div` are dividends, `Bank Interest` is interest,
  `ADR Mgmt Fee`, `Service Fee`, `NRA Withholding`, `Tax Withholding`,
  `Interest Charged` and `Interest on Debit` (interest the account pays)
  are fees, `Payment in Lieu` is a dividend and `Spin Off` a corporate
  action. What is left of `Int'l` in a fund's name is not interest, `Int`
  or `Div` before a word that is no action word starts a name (`Div
  Example Fund`), and a `Non Dividend Distribution` is no dividend. A
  movement between the account and a bank or another account
  (`MoneyLink`, `Wire Sent`/`Received`, `Wire Funds`, `Internal
  Transfer`, `Bank Transfer`, `Funds Paid`/`Received`, `Security
  Transfer`) is named by its words and directed by the row's sign only:
  without a sign it is not typed. An unsigned amount takes the type's direction; a symbol is read
  from its column (a valid CUSIP there is the CUSIP), from the
  description's first word when the row needs a security or the symbol is
  held, or from `(SYMBOL)` (beside a symbol column a name's first word is
  never taken for a ticker: a bond's issuer). `brokerage_statement_generic` (version 9)
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
- **What a statement says about its own rows** (`brokerage.section_checks`,
  `brokerage.localise`; `InvestmentReconciliation.sections`,
  `doubtful_activity`, `doubtful_positions`, `summary_cash_difference`,
  `unread_summary_lines`). The four checks say *whether* a statement adds
  up; what the statement prints about itself says *where* it does not, so
  one line that makes no sense is one question, not a held file:
  - **Blocks side by side** (`app/ingestion/parsers/blocks.py`): a page
    printing two things next to each other (*Account Summary* beside
    *Account Holdings*, *Core Account Cash Flow* beside *Income Summary*,
    a *Debit Card Summary* box beside the card tables) is read block by
    block. Two section headings on one line, or the right block's heading
    of two or more words alone on its line, open the region; each line is
    cut at the right heading's left edge, the pieces of each side are
    joined again by their own height (a label keeps its amounts whatever
    the other block printed a few points higher) and the left block is
    read first. The region ends with the page, at a line with a cell
    across the cut, at a heading of the left block standing clear of
    the lines above (the next full-width section), or at the header of a
    table printed across the page (a line the parser reads as a table
    header as a whole and on neither side of the cut alone). The bank and
    card statement parser unfolds blocks the same way (its block titles:
    *Account Summary*, *Interest Summary*, *Transaction Detail*, ...). A
    table's header cell
    that is also a heading word (`Description | Contributions`) opens
    nothing.
  - **Summary tables are figures, not rows** (`InvestmentStatement.figures`,
    `StatementFigure`: `group`, `label`, `role` opening/closing/total/item,
    `detail` for an indented breakdown, the period's `amount` and the
    `year_to_date`): the account-value bridge from *Beginning Account
    Value* to *Ending Account Value* (additions, subtractions, change in
    investment value), the core account's cash flow line by line, the
    income summary by tax treatment and the realized gains. A dash is no
    figure. `perfin ingest --dry-run` prints them.
  - **Section totals**: every activity row carries the heading it was
    printed under (`InvestmentActivity.section`) and each `Total ...`
    line of an activity table is kept. A total sums the rows its words
    say: the section's heading (`Total Deposits & EFT In` under
    *Deposits*, `Net Securities Bought & Sold`) all of its rows; nothing
    (`Total`, `Total this period`) the table it ends; the purchases or the
    sales of the section (`Total Securities Bought`); any other label
    (`Total Federal Taxes Withheld`, a card's `Subtotal`) is not used. A
    transfer of units counts by the value printed. The last total of each
    scope is checked: `ok`, `mismatch` (with the difference) or, for a
    section printing none, `unchecked`.
  - **The cash-flow summary is a second account** of every section: a
    line is matched to the rows of the section it names (*Securities
    Bought*/*Sold* to the buys/sells, *Margin Interest* to the margin
    table, *Transfers In/Out* to the cash rows of the transfer sections),
    compared where the section's own total does not already vouch for
    them (`source: summary`), and a line no row was read for is listed
    (`unread_summary_lines`: the statement itself names cash flow that was
    not read).
  - **Rows in doubt** (only when a check failed). Activity, when the
    `cash` check failed: a row is vouched for when a total covering it
    agrees; in a section that does not add up, one row that is the whole
    difference (a row too many) is alone in doubt; a row moving cash that
    a bridging cash-flow summary does not list at all is in doubt whatever
    its section's total says (value moved between accounts under
    *Securities Transferred In*, not core cash); a row moving no cash is
    never in doubt. When every section adds up and the cash still does
    not, the rows stand only if the summary bridges and names a line that
    was not read; otherwise the one row that is the whole difference is in
    doubt, else every row moving cash (a forged row whose section total
    was adjusted to match is still caught by the cash). Positions: those
    whose quantity x price is not their market value; all of them when
    they do not add up to the printed holdings total, or when holdings +
    cash is not the account value and no holdings total is printed. Only
    these rows are capped below the threshold with the `reconciliation`
    doubt. A source that prints none of this (OFX, an activity export)
    has no rows singled out and is held whole.
  - **The document's account list is a check too** (a document of several
    accounts, `app/ingestion/parsers/accounts.py`): the index of accounts
    on its first pages prints a beginning and an ending value for each.
    The ending value is compared with the account value printed on the
    account's own pages (the `index_value` check; it singles out no row),
    stands in for an account whose pages print no value, and an account
    the index lists that no statement was read for is reported
    (`ParseInfo.unread_accounts`, warning `accounts_not_read`, an
    `accounts_not_read` review item naming the last four digits).
  - **Lines under a row belong to it**: the first line under a trade in
    the name column continues the security's name; text under the action
    or description (accrued interest, a wash sale note, a bond's coupon
    and maturity) is kept as the row's notes and ends in its memo. A row
    whose date prints no year and falls outside the statement's period (a
    card purchase posted the month before) is placed in the nearest year
    with a guessed-date confidence, not dropped.
  - **Entries nothing read are said**: a line with a date and an amount
    under no table the parser could read (the table's header names no
    column it knows as the date or the amount) is counted and its place
    kept (`lines_unread`, shown by the `lines_skipped` item's highlight).
    The header of such a table ends the table above it, so its entries
    are never read through another table's columns. Its unknown column
    headings are what the optional local model may be asked about
    (`ParseInfo.unknown_columns`; "Models in the pipeline").
- **Table reading** (any institution; the synthetic Fidelity-layout report
  `tests/fixtures/synth_fidelity_report.py` and the provider-neutral
  combined statement `synth_neutral_report.py` are read to the last row
  by the generic parser as well as by the layout's):
  - **Header vocabulary**: *Transaction Description* and *Location
    Reference/ Description* (the action),
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
    *Estimated Cash Flow*, *Income Summary*, *Account Holdings*, *Debit
    Card Summary* are no activity (the summary tables among them are read
    as figures); *Margin Activity* (`Period | Period Balance | Interest
    Rate | Average Daily Balance | Interest Paid`, a header naming no
    action) gives one fee per period, dated by the period's last day
    (`7/1-7/31`) and described as margin interest; *Securities Transferred In/Out* move units (the printed
    amount is their value, no cash) or, without units, cash (a deposit or
    withdrawal); *Dividends, Interest & Other Income* rows are dividends,
    or interest when they say so (a *Return of capital* or *Cash in lieu*
    there is income too, not a deposit, the words kept in the memo); *Deposits*/*Contributions* and
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
    stays, also for a later heading worded like it over the same columns
    (the next card's table). A table running onto the next page continues its section: the
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
- **The account's number in a statement's summary**
  (`parsers/accounts.py`, `summary_last4`; bank, card and brokerage
  statements): a masked number (`****1234`), `ending in 1234` or `Account
  Ending 4-71009` (not `Period ending 2026`), or a labelled number read
  **whole**: a number printed in three or more short groups (`Account
  number: 4410 0092 6654`, `4111 1111 1111 1111`) ends in the digits of its
  last group, not its first; two numbers side by side (`12345678 2026`)
  stay apart; a group that starts a date or an amount is not part of it; a
  number shaped like a date or a telephone number is none. Only four
  digits are kept.
- **The currency** of a bank or card statement: a printed code
  (`Currency: CAD`; `parsers/currency.py`, `stated_currency`, used by the
  statement, brokerage, payslip and loan parsers alike: the word
  "currency" in any case, then three capital letters as printed that are
  an active ISO 4217 code, so `currency for the period`, `Currency
  Forward Contracts`, a lower-case word and a code PerFin does not know
  name no currency), else the symbol its amounts carry when that names one
  currency (`£`, `€`), every symbol-bearing amount carries the same one and
  two amounts at least do (`symbol_currency`), else the layout's own. A
  dollar sign is no evidence. A statement in pounds is then proposed, and
  matched, as a GBP account instead of the household's base currency.
- **Accounts without a number** (`TableStatementParser.balance_sections`):
  a credit union prints a member's shares one after the other under titles
  (`PRIMARY SAVINGS - ID 0000`, `FREE CHECKING - ID 0010`), each with its
  own opening and closing balance row and no account number. They are
  found by the balance **starting over**: an opening-balance row in a table
  whose figure is not the balance the rows before it ended on. Each such
  section is one statement, named by the title over its table (which also
  gives the account type), reconciled on its own; its last four digits are
  the title's own number when that has four digits or more, never the
  member number printed once for all of them (parse warning
  `accounts_by_balance`). A balance **brought forward** at the top of a
  page (the figure carried forward at the foot of the page before)
  continues its account, and the last `Balance carried forward` printed is
  the statement's closing balance.
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
  unit of the price's last printed decimal per unit plus one cent,
  `PRODUCT_SLACK`: a feed that cuts the product to the cent instead of
  rounding it is up to a cent off; a price
  per 100 of face value divided by 100; the cent covers this product
  only, and every sum of printed amounts below keeps the half-cent
  tolerance), the positions add up to the
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
  `account_type_mismatch`, `currency_mismatch`). **No
  row a statement's own figures do not vouch for is written** (investment
  rows have no pending state, so what is in doubt waits in a
  `low_confidence_field` item); what is held depends on what the
  statement says about itself ("What a statement says about its own rows"
  below):
  - *nothing in doubt, figures disagree* (the printed account value is not
    holdings + cash; a section's total is off by an amount no row
    explains): the statement is imported whole and
    `investment_figures_differ` (`failed`, `differences`, `sections`,
    `unread_summary_lines`) tells the person what does not add up;
  - *some lines or holdings are in doubt and the statement says which*
    (for the `reconciliation` doubt or any other: a date outside the
    period, an amount hard to read): what is in doubt waits and the rest
    is written. A line moving cash only (a dividend, interest, a fee, a
    deposit, a withdrawal) waits on its own. A line moving units, or a
    position, makes **its security** wait: the position and every line
    moving that security are held together (the units are reconciled
    against each other; `fields: ["held_with_security"]` for the lines no
    figure doubts), nothing is opened or compared for it, and no balance
    snapshot is written while a position waits. `investment_lines_held`
    lists the lines held back (`lines`: `index`, `page`, `bbox`, `date`,
    `type`, `description`, `amount`, `section`, `fields`; `held`,
    `imported`), the holdings that wait (`positions`: `index`, `symbol`,
    `name`, `fields`, `page`, `bbox`) and the `sections` that do not add
    up (`label`, `printed`, `computed`, `difference`, `rows`, `source`,
    `page`). `selectable: true` when no held line moves units: the answer
    may then name the lines to import (`lines`), and the others are left
    out without being asked about again;
  - *anything else* (a line moving units that names no security, or
    nothing that could be written without what is in doubt; OFX and
    activity exports, which do not say where a failed check comes from; a
    document with nothing to check against): the statement writes
    **nothing** and
    raises `investment_reconciliation_failed` (checks,
    failed checks, mismatched positions, cash and totals, the statement's
    index and key (`statement_key`), the account's currency,
    `differences`: per failed check
    `printed`, `computed` and `difference` = computed - printed, `count` for
    `positions`; and, as above, `sections`, `unread_summary_lines` and the
    `lines` in doubt when the statement says which) or
    `investment_low_confidence`.

  The account's value (the balance snapshot) is taken from a statement
  whose every check held, and also from one where only the `cash` activity
  check failed while holdings + closing cash is the printed account value
  (the lines in doubt do not change what the account is worth); an account
  that prints no cash at all and whose account value is its holdings (a
  529 plan) holds none. With that snapshot, the first statement of an
  account also records the value it **opens** with (the *Beginning
  Account Value* of its account-value summary) for the day before the
  period, when that summary adds up to the closing value recorded and no
  snapshot of any source exists from that day to the period's end: an
  account's history then starts where its first statement does. A holding
  the account already had whose statement prints a cost basis for its
  other holdings but not for this one is opened at the statement's price
  and raises `investment_opening_basis_unknown` (`costed_at: "price"`); a
  position with a negative quantity (a short position) is in the
  statement's value and not among the holdings: the lot engine tracks no
  short position, so the position and the lines moving that security's
  units are left out of it whole (a short sale would read as selling units
  never held) and an `investment_short_position` notice says so
  (`positions`, `lines_left_out`). A **written option** is short without
  a printed position saying so (`_written_options`): an option (asset
  class `option` on any of its rows) whose running units over the
  statement's own rows fall below zero was sold without being held. The
  lines that move it while it is short (from the sale that takes the
  count below zero through the purchase that brings it back to zero) are
  left out in the same way; its lines before and after, such as the sale
  that closes a long position bought in an earlier file, go to the lot
  engine like any other. One line that takes the count from above zero
  to below it (or back) closes and opens at once and cannot be split:
  every unit line of that option is then left out, and units of it the
  account already holds from another document stay as they are.
  Everything else of the statement is imported, and the same notice says
  so with
  `from_activity: true` and one `positions` entry per option with its
  lowest running units ("Written option left out" in the review queue).
  The count starts from the units the statement implies when it lists
  positions (the listed units less what its rows add), else from what the
  account's history from other documents holds. Only options: a share
  sold before a statement's first row still opens at an unknown cost
  basis. The rule sees one statement at a time.
  An `investment_figures_differ` item is a notice: the statement was
  imported whole, so it stays in the queue until seen and does not keep
  its document in `needs_review`. Real statements do not
  always add up on their own (rounding, items the issuer leaves out), so
  the review queue offers **Import as read** for an
  `investment_reconciliation_failed` item, and for an
  `investment_low_confidence` or `investment_lines_held` item whose only
  doubts are `reconciliation`, a suggested reading (`suggested_type`) or,
  where the `positions` check failed, a holding's `market_value` (a
  document statement with nothing to check against; the held lines and
  holdings of an imported one, whose rows already written are skipped by
  their hashes). The answer is for
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
  than reported raises `position_mismatch`. A short position (a negative
  quantity) counts as none: the lot engine holds no short positions, so
  units the history holds against one raise `position_mismatch` too. Lots,
  `lot_links` and holdings
  are then written by the investments lot engine
  (`service.replay_position`); a history it refuses rolls the statement
  back (savepoint) with `investment_lot_conflict`. Position prices become
  `price` rows (`source = statement`) unless another source has that day.
  A security listed in two positions on one day (a plan's position per
  contribution source, a sub-account's) gives one price row per run: the
  first price printed is the day's, a later one that differs is left out
  and the run's summary says so (`investments.warnings:
  duplicate_price_ignored`, in the parse stage log and the audit facts;
  no review item); the units of every position still count;
  holdings + closing cash on `as_of` is a `statement` balance snapshot when
  reconciled and both are known (a statement printing no cash, such as a
  529 plan's, gives none; nor does one imported as read). New rows of a statement older than activity already recorded
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
  name), quantity, price, commission, fees (also one `Fees & Comm` column
  and its other wordings), currency, account number
  (`Account Number`, else an `Account` column whose cells carry digits).
- **Rows**: one-cell rows (blank lines, disclaimer paragraphs, the "Date
  downloaded" line) are notes, and so is the export's total line (a total
  label such as `Transactions Total` and figures only): it is not counted
  as unread and takes no part in the typed share or the date format. A
  date cell printed `10/14/2026 as of 10/10/2026` is read as its first
  date, the trade date; the second decides nothing, the day/month order
  included. A row is typed by its action (the action words and
  abbreviations of "Brokerage statements" above; `YOU BOUGHT`,
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
- **Reinvested dividends**: a broker that prints a reinvestment as two
  rows gives the income first (`Reinvest Dividend`: no quantity, cash in)
  and the purchase second (`Reinvest Shares`: units, cash out). A row
  typed reinvest with an empty quantity cell and an amount above zero is
  that income leg and is read as a dividend of its security, so every
  dividend is income and every reinvestment buys units. With no cash
  coming in, or with a quantity cell that is filled and gives no units
  (`n/a`, `0`), it proves neither leg and is skipped and counted. The
  other way round, a row typed dividend whose action says reinvest in its
  first words (`Div Reinvest`, `Qual Div Reinvest`) and that carries
  units is the purchase of a broker printing one row: it is a reinvest.
- **Options**: a symbol cell that names an option (`ZZOP 03/20/2026 45.00
  C`, or the OCC form with or without its inner space) is the security
  `ZZOP260320C00045000` with the asset class `option`. One contract is
  100 shares: when the amount before fees is nearer to quantity x price x
  100 than to quantity x price the price is stored per contract, else as
  printed. `Buy to Open`, `Sell to Close`, `Sell to Open` and `Buy to
  Close` are typed by their first word. An `Expired` or `Assigned` row
  says that a position closed, not which way: it becomes a sale when the
  account's other rows leave the option held and a buy when they leave it
  written, whatever sign its quantity prints, and a blank amount is zero.
  Only rows of the export prove what is open. A sale that closes by its
  own words never leaves the option written; a plain `Sell` with nothing
  held proves no short (it may sell contracts bought before the export's
  first row); only a sale that opens by its own words (`Sell to Open`,
  `You Sold Opening Transaction`, `Sell Short`) does. A closing with
  nothing open or larger than what is open, an expiry against a short
  that is not proven, and a `Buy to Close` of an option the rows do not
  leave written are skipped and counted, never read as a trade. A
  written option then stays out of the lot engine ("Brokerage
  statements", the short-position rule): the rest of the export imports.
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
  the statement is held (`investment_low_confidence`). The tie is no
  guess, and is read month first, when the header's filled cells are
  exactly a known US broker header (a layout known by its first cells
  only, or a known header with one more column, is not enough) and the
  amounts are written dollar-sign first; either sign alone, fewer than
  four such amounts, or dashed dates leave it a guess. A filled currency
  cell that is not an ISO code PerFin knows makes its row unreadable
  (skipped and counted), as a malformed cell does. `csv` is a
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
  whose rows reads an `unparsed` item (`activity_unreadable`), which the
  mapping editor answers (its columns are checked there).
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
  `parser_version = brokerage_activity_table/2`, and the upload list offers
  the review queue rather than the cash-ledger column mapping.

#### Brokerage positions exports (CSV, TSV, XLSX)

A brokerage's "Positions" export (Fidelity's *Portfolio Positions* download:
`Account Number, Account Name, Symbol, Description, Quantity, Last Price,
Last Price Change, Current Value, Today's Gain/Loss Dollar, Today's
Gain/Loss Percent, Total Gain/Loss Dollar, Total Gain/Loss Percent, Percent
Of Account, Cost Basis Total, Average Cost Basis, Type`, and layouts like
it) lists what every account holds at the moment of the download: one row
per holding, with its units, price, value and cost basis. It has no dates
and no actions, so it is neither activity nor a cash ledger. The structured
importer reads table 0 of a CSV, TSV or XLSX with
`app/ingestion/parsers/positions_table.py` (in the worker, on the stored
table; never a PDF's page table) before it looks for an activity table or a
column mapping:

- **Recognised** when a header among the first 11 rows names a symbol, a
  quantity and a market value column (`Current Value`, `Market Value`,
  `Total Value`, `Value`) and names **no** date and action columns (that is
  an activity export), and at least half of its data rows read. Optional:
  account number, account name, description (the security's name), price,
  cost basis (the position's total: `Cost Basis Total`, `Cost Basis`),
  percent of account, currency. The columns that restate these (price
  change, the day's and total gain, the average cost, `Type`) are not read.
- **The day**: the export's `Date downloaded Nov-11-2026 7:35 p.m ET` note
  (also `11/11/2026`, `2026-11-11`) is the day the positions are valued.
  An export that prints none is dated the day the file was received (the
  run summary says `as_of_assumed`).
  A title above the header that says `as of` a date dates the positions
  when there is no download note.
- **Rows**: one-cell rows (blank lines, the disclaimer paragraphs, the
  download note) are notes, and so is a row that repeats the header. A
  row whose symbol cell is a total label (`Account Total`) is a **total**
  whatever its other cells print: not a holding, not skipped, not part of
  the share of rows that must read; it is counted (`total_rows`) and its
  value is the account's printed total (below). A row is the account's
  **cash** when its symbol
  ends in `**` (the core position: `SPAXX**`, `FCASH**`), reads `Pending
  Activity`, starts with the word cash (`Cash & Cash Investments`), or it
  has no quantity and a description that reads like cash
  (`HELD IN MONEY MARKET`): its value is part of the statement's closing
  cash, never a holding, as the PDF statement parser treats a core account.
  Any other row is a **position**: a security (the symbol; a CUSIP with a
  valid check digit is kept as the CUSIP; without a usable symbol, the
  description alone, matched by name like any name-only security) with a
  non-zero quantity, and its price, value and cost basis when printed
  (`n/a` and `--` are blanks; `$1,000.71`, `+$53.11`, `-$250.00` and
  `(12.50)` are numbers). A price quoted per 100 of face value (a bond) is
  marked `price_per_100`, and one quoted per share of a contract of 100 (an
  option) is stored per contract, so quantity x price is the value either
  way. A negative quantity is a short position (kept out of the lot engine
  with an `investment_short_position` notice, as for any statement). A row
  that gives no security, no quantity or no value is **skipped and
  counted, never guessed**. The formula guard is set aside only to
  recognise headers and parse numbers; names keep it and a symbol that
  would be a formula is no symbol.
- **Accounts**: one `InvestmentStatement` per account-number cell and
  account name together (so two accounts never fold into one, even in a
  file whose numbers are all masked alike), in order of first appearance;
  one statement without an account column. Only the last four digits of
  the number are kept. The account name (`Individual`, `ROTH IRA`) names
  the statement's account and makes it a `retirement` account when it reads
  like one; the Fidelity header names the institution. Period and `as_of`
  are the one day of the download; there is no activity.
- **Blocks, without an account-number column**: the table is read in
  blocks, and rows of different blocks never share a statement. A block
  starts at the header row, at every later header row (the same one
  repeated, or another positions header without an account column, whose
  columns then read the rows under it) and at a one-cell line that names
  another account than the block it stands in. The nearest such line
  above a block's rows names its account. A line names an account only
  when it ends in an account number or a mask (`Roth IRA ...8820`):
  digits are never gathered across words, and years, days, amounts,
  telephone numbers and sentences name nothing. Rows that follow a
  block's total row, or a one-cell line that prints a number or a mask
  and is not read as an account, may be another account's: that block's
  statement then names no account and carries no cash and no total
  (warning `accounts_unclear`), so the importer asks whose it is instead
  of matching it by digits. A table with one header and no such line is
  one statement, as before. Not told apart: a second account under one
  header introduced by a line with no digit and no mask (`Roth IRA`
  alone) and no total row before it folds into the block above.
- **What the export says about itself**: each position's quantity x price
  against its printed value (`positions`), and, when every valued row
  prints its **percent of account**, whether the rows are the whole
  account: the percentages add up to 100 (within the rounding of two
  decimals per row). Then `total_value` is the sum of the rows; when they
  do not add up (rows were cut from the file), it is the total the
  percentages imply and the `total_value` check fails by the difference. A
  positions export has no activity, so a failed `positions` check is the
  fault of the positions that fail it and of nothing else: they carry the
  `reconciliation` doubt and are named in `doubtful_positions`. An
  account whose rows end in a total row is checked against that instead:
  the printed total is the statement's `total_value`, compared with the
  holdings plus the cash rows. Total rows of one account that disagree (a
  subtotal and a total) are no total, and a `Grand Total` is an account's
  only when the file has one account. A total row of an account with no
  cash row is kept only when the holdings add up to it; otherwise nothing
  printed makes up the difference and it is not used (warning
  `total_unchecked`). An account
  with a skipped row prints no cash and no total.
- **Hold** (step 3 of "Brokerage statements"): a position whose value is
  not quantity x price waits (an `investment_lines_held` item listing it,
  or `investment_reconciliation_failed` when nothing else of the account
  would be written) until a person says "import as read"; the account's
  other positions are imported. Rows that are not the whole account
  (`total_value`) import with an `investment_figures_differ` notice.
  Either way no balance is recorded from figures that did not add up.
- **Commit, as a snapshot**: the positions are the whole account on that
  day, so `statement_import` brings the account's holdings to them rather
  than only opening what its history does not explain. Per security:
  units the account does not hold yet enter as one `transfer_in` on the day
  (memo `Units held per positions export`), costed so that the position's
  cost basis is the printed one (the printed basis less what the account
  already carries; the price when no basis is printed, with an
  `investment_opening_basis_unknown` item); units the export no longer
  lists leave as one `transfer_out` (memo `Units no longer held per
  positions export`; a transfer realises no gain). A position known only
  from statements and exports (every open lot opened by an opening or
  snapshot row, in a FIFO account) whose basis would still differ from the
  printed one by more than a cent is **reset**: everything held leaves and
  the reported units enter at the printed basis, so units and basis are
  the export's exactly. A position with lots of its own (bought through
  imported activity) keeps them: only its units follow the export, its
  basis stays what its history says (an average-cost account is never
  reset either). Each position's price becomes the day's `price` row
  (`source = statement`), and holdings plus cash the account's balance
  snapshot for the day, so net worth follows. When the account already
  holds what the export lists nothing is written, so a re-run, the same
  export twice, or tomorrow's export of unchanged holdings adds no row
  (the day's prices and balance are still recorded).
- **Order**: an export older than investment activity already recorded for
  the account, that would change its units, is not applied
  (`investment_out_of_order`). Activity moving units on or before the day
  of a snapshot row already recorded is held the same way: the export
  already counts those units. With both of a broker's exports, import the
  activity first, then the positions.
- **Accounts without digits**: an export that masks its account numbers
  gives no last four digits, so each statement is an `unknown_account`
  item answered in the Accounts dialog (create the proposed account, or
  pick one). Where a statement was put is remembered for the document (the
  item's resolution records the statement's key and the account,
  `app/domain/imports/statement_answers.py`), so a later re-run of the same
  document (an "import as read" answer, "process again") does not ask
  again. Another file asks again; real account numbers match by their last
  four digits from the second file on.
- **Limits**: lots are dated the day of the export that first reported
  them, not the day of purchase, so the short/long-term split of a
  position known only from exports is not the broker's. Between two
  exports nothing is known: a market value is the latest price times the
  units of the last export (`GET /investments/holdings` names that day per
  account, `positions_as_of`, and the app says the values are estimates).
  A document read this way records `parser_version =
  brokerage_positions_table/2`; its run summary has `positions_table`
  (statements, `as_of`, positions, cash rows, total rows, notes, skipped
  rows, warnings, among them `accounts_unclear` and `total_unchecked`); skipped rows raise one `lines_skipped` item, a table none of
  whose rows reads an `unparsed` item (`positions_unreadable`). A mapping
  chosen explicitly for a run still imports the table as a cash ledger.

#### Retirement plan statements

A 401(k) plan's quarterly statement prints no units and no prices. It
prints one row of dollars per fund, often the same money again per
contribution source, under a header such as `By Investment | Beginning
Balance | Earnings/Fees | Contributions | Withdrawals | Transfers | Ending
Balance`, and a `Total` row (the roll-forward layout).

- **What it becomes (decision).** The **balance of the retirement account
  and nothing else**: the plan's ending balance on the period's last day as
  a `statement` balance snapshot and, when the statement's own figures add
  up, the beginning balance on the day before the period (the existing
  opening-value rule of "Brokerage statements", step 9). No holdings, no
  lots, no prices, no investment transactions, no ledger transactions. A
  holding in PerFin is the lot engine's cache: it needs units and a cost
  per unit, and this table prints dollars. Writing the dollars as units at
  a price of 1 would report a gain of zero for ever and raise
  `position_mismatch` whenever the market falls.
- **Parser** (`app/ingestion/parsers/plan_statement.py`,
  `retirement_plan_statement`, version 1, kind `statement`): scores 0.65
  when a line names `beginning balance` and, after it, `ending balance`
  with at least one movement column between them and nothing between them
  but movement columns (contributions, earnings, earnings/fees, gain/loss,
  withdrawals, distributions, transfers, forfeitures, fees), and no figure
  on the line; otherwise 0. A bank summary headed `Beginning Balance |
  Deposits | Withdrawals | Ending Balance` is not one, nor is a sentence
  naming both balances. A table with a column the parser does not know is
  not read at all. A cash account (a health savings account) may print
  the same header over one summary row and its dated transactions below
  it: when the pages carry a transaction table that the bank or card
  statement reader recognises, this parser scores 0 and reads nothing,
  and the statement reader has the document.
- **Rows**: a name followed by figures with two decimals (parentheses are
  negative, a lone dash is zero). As many figures as columns are taken in
  order; fewer are placed under the header whose centre is nearest, each
  column at most once and in the printed order (a blank cell is zero). A
  line with figures that is no such row is counted (`lines_skipped`),
  never guessed. `Sub Total` rows are skipped; only a row named `Total`
  (`Totals`, `Grand Total`, `Account Total`, `Plan Total`) is a total, so
  a fund called "Total Market ..." is a fund. A table goes on over a page
  break when its header is printed again.
- **Which total**: the **last** total row before the next roll-forward
  header or the document's end. A statement that groups its funds prints
  a `Total` under each group and the plan's total after them: when
  another total row follows, the one before it was a group's and is
  skipped, the rows between are the table's, and every fund row must add
  up to the last total (`roll_forward_rows`). Group totals with no total
  of the whole therefore fail the check and nothing is recorded. Any
  later line named as a total and carrying a figure counts, also one
  whose figures cannot be put under the columns (the check then fails).
  Lines after the last total row (a `Vested Balance` line) are not the
  table's.
- **Checks** (`InvestmentReconciliation.checks`, each comparison within
  half a cent): `roll_forward`, on the total row beginning plus the
  movement columns is ending; `roll_forward_rows`, every row adds up the
  same way and the rows' ending balances add up to the total's. Columns
  are added as printed; when the total row does not add up that way, the
  columns headed withdrawals, distributions, fees and forfeitures are
  subtracted instead, and the convention under which the total row adds up
  is used for every row.
- **Several tables**: a second roll-forward table with the same beginning
  and ending totals is the same money by source and is ignored. One with
  the same ending total and another beginning total covers another span
  of time (a `Year to Date` table beside the period's): the ending total
  is recorded as the balance, no figure of the total row is kept and no
  opening balance is recorded, because which beginning total is the
  period's is not known. One with another
  ending total means several plans or participants in one document:
  nothing is read (warning `plan_statement_several_accounts`) and the
  document waits as any unread document does. No total row: nothing can be
  recorded (warning `plan_statement_no_total`).
- **Kept** in `extraction.investments`: one `InvestmentStatement` with
  `account_type` `retirement`, the institution from the document's first
  line, the period from a `for period:` / `statement period:` label
  (`as_of` is its last day), no positions, no activity, `total_value` (the
  printed ending total) and the total row as `figures` of the group
  `account_value` (opening, one item per movement column as it counts,
  closing). No account or participant number is read. A numeric period
  that reads two ways (`01/02/2026 - 03/04/2026`) is kept with a
  confidence below any threshold.
- **Commit** (`statement_import._commit_balance`): the statement names no
  account digits, so it lands on the account the document was uploaded
  for or classified as; otherwise an `unknown_account` item
  (`account_not_found`, proposing a retirement account) asks, and the
  chosen account takes it on the re-run. The account must be of type
  brokerage or retirement, and the statement-level duplicate guard
  applies. The balance is recorded only when both checks
  held, the period is stated and its dates are in range; the audit row
  `investment.import` then carries `balance_only: true`. Otherwise nothing
  is written and one `low_confidence_field` item,
  `plan_statement_does_not_add_up`, gives the printed ending total, what
  the figures come to and the difference. It cannot be answered "Import as
  read": a balance its own statement does not vouch for is entered by hand.
- **Limits**: the Investments page shows no funds for the plan.
  Contributions, earnings, fees and withdrawals are not transactions (the
  salary deferral is on the payslip; the employer match appears only in
  the balance). Vesting is not recorded: the balance stored is the ending
  balance, not the vested balance. One plan account per document. A header
  printed over two lines, whole-dollar figures and tables with other
  columns (loan repayments, rollovers) are not read. A per-fund balance
  needs a table of its own in the data model and is left to a later
  package.

#### Loan statements (Phase 5)

A loan or mortgage statement is one payment and the balances around it,
kept in `extraction.parse.loan` (`LoanStatement`: lender, the loan
number's last four digits only, currency, statement date, payment date,
principal, interest, escrow, fees, total received, previous and new
principal balance, interest rate as a fraction, next due date and amount,
reconciliation, per-field confidence).

- **Parser** (`app/ingestion/parsers/loan_statement.py`,
  `loan_statement_generic`, version 2, kind `statement`): recognised by
  "mortgage statement"/"loan statement" on page 1 or, without those words
  (a loan bill headed `Account Statement`), by at least four loan labels
  (principal balance, escrow, interest rate, payment received, payment
  allocation, next payment due, total payment received, loan number) of
  which one is about the principal: a card statement prints a payment due
  date, a payment received and an interest rate too, and no principal.
  Labels are counted over all pages, each once, and a label printed only
  inside a longer one is not counted beside it. Two layouts, each with
  its own reader; synthetic layouts: `tests/fixtures/synth_loans.py`.
- **The payment receipt**: labelled lines, the amount being the last one
  on the line (year-to-date and escrow-balance lines are left out).
- **The periodic statement** (`loan_periodic.py`; the module doc has
  every rule). The statement a servicer sends each month prints
  principal, interest and escrow three times: for the next payment, for
  last month and for the year so far. Only last month's column is a
  payment made, so this layout is read by section and by column, never by
  the last amount on a line:
  - *Sections.* A heading cell starts a section that owns the labels
    below it, from its left edge to the next heading on its line (blocks
    printed side by side), down to the next heading: *amount due* (the
    next payment), *paid* (a line naming a year-to-date column starts or
    heads it), *activity* (a `Date | Description | Payments` header);
    labels outside every section are *account* labels. Sections do not
    carry over a page break. From a line saying to detach the coupon to
    the end of its page nothing is read.
  - *Columns.* In the paid section the parts and the total are the
    payment, each from the period's column (`Paid Last Month`). A
    year-to-date column is never read. A header naming it and exactly one
    other column makes that other the period's, unless its words say it
    is the next payment (`Amount Due`, `Current Payment`, `Next`,
    `Billed`, `Scheduled`). With more than one unnamed column, a column
    of the next payment, or no header saying what the figures are,
    nothing is read (warning `loan_columns_unknown`). A paid figure
    printed negative (a reversal) is not read.
  - *The day paid* is the labelled day the last payment was received,
    else a row of the activity table whose description says payment (and
    not late, fee, due, reversal, returned, scheduled, pending, upcoming,
    next or future) with its amount in the payments column, never one
    dated after the statement date: the latest row of the payment's
    total, else the latest row when all payment rows add up to it, else
    none (warning `loan_payment_row_mismatch`).
  - *The balance* is the printed outstanding principal; the previous
    balance and the rate are read from the account labels, and a label
    with no amount on its line takes the cell under it, else the one
    over it.
  - *Not stored:* the next payment's split, the regular payment and the
    total amount due are used for the `amount_due` check only; the due
    date is the next due date and never the day paid; no year-to-date
    figure is kept anywhere.
- **Loan bills** (auto and personal loans) are read by the same reader:
  under a `Payment Allocation` heading the parts and the total are the
  payment made, one figure a label (two figures: not read,
  `loan_columns_unknown`), dated by a bare payment row (a line outside
  any table that starts with a date, says payment and prints exactly one
  amount). A bill that lists several loans in one table is not one loan's
  statement: nothing is read (warning `loan_bill_several_loans`) and the
  document waits as `loan_unreadable` ("Review queue").
- **Both layouts** read the interest rate (a percentage, stored as a
  fraction; the tail of a number OCR split, `4, 75%`, is no rate), the
  lender (a `Lender:` line with its colon, else the first line), the loan
  number's last four digits (`ending in 1234`, `XXXX1234`, or the end of
  a number of six or more digits printed in full after its label; never
  more than four are kept) and the currency.
- **Reconciliation** (SECURITY.md C12): `parts` (principal + interest +
  escrow + fees = total received), `balance` (previous - principal =
  new) and, on a periodic statement, `amount_due` (the next payment's
  principal + interest + escrow = the regular payment, and with the fees
  = the total amount due, whichever are printed), within the
  reconciliation tolerance. Figures a check confirmed
  score 1.0, unchecked ones 0.8 (below the default threshold), all of a
  statement that does not reconcile 0.5. A periodic statement prints its
  principal balance once with nothing to check it against: when the
  statement reconciles it scores as a labelled value (0.95). On a scan
  each field is scaled by the OCR confidence of its own cells
  ("Confidence" above).
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
  **No payment in the period** is not a doubt: a statement that prints no
  day paid, a principal and an interest paid of zero (warning
  `loan_no_payment_in_period`), no doubtful field and a next amount due
  that adds up (`amount_due`) is filed. No payment is written
  (`no_payment` in the summary); its principal balance, when it and the
  statement date were read, becomes a `statement` snapshot on the
  statement date (a date before the loan began or after today is left
  out).

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
  several account-number-like values or, under a header that is exactly
  `Account`, `Account Name` or `Source Account`, the names of a few
  accounts: "Multi-account files" below); an empty column never takes one,
  and a "Type" column of `Cash`/`Margin` is no indicator. A column of
  direction words is also found by its cells when no header names it
  (match kind `content`). Beside unsigned amounts it is the indicator
  that signs each row; beside amounts that already carry a minus sign it
  is left out of the spec (it describes the row; the amount signs it) and
  compared with the signs instead ("The sign" below). A running balance
  that agrees with only one sign convention sets `sign`. The suggestion is
  **confident** when it has no doubts: the date and amount (or
  debit/credit) columns are named exactly (`header_not_exact`), every row
  before the footer reads, the day/month order is not a guess
  (`date_order_guessed`), the sign is not in doubt and, with an indicator,
  every row's indicator is a direction word beside an unsigned amount
  (else `indicator_values_unknown`: a `CHECK` row would otherwise read as
  money in); it reports a score, per-role reasons (role, column, match
  kind, share of cells that fit) and its doubts.
- **The sign is proven or the file waits.** `sign_evidence` says what
  proves the convention: `debit_credit` columns, an `indicator`, a
  reconciling running `balance`, or `indicator_correlation` (direction or
  transaction-type words beside signed amounts: at least 4 rows, both
  directions seen; plain `debit`/`credit` words prove a sign only when
  every row agrees, type words such as `purchase`/`payment`/`refund` when
  95% agree overall and in each direction; `sale` and `return` vote only
  under a card header; the spec's `sign` is set to what they prove). With
  no evidence, signed amounts are read as "positive is money in" on
  trust, and that is the doubt `sign_unverified` when the header names a
  card (`names_card`: `Card No.`, `Name on Card`, `Member Name`,
  `Merchant`, `Statement Balance`, a `Post Date` beside a `Transaction
  Date`...), more rows are money in than money out (every row among
  them), or every amount is zero: a card export lists purchases as
  positive, and read the other way every purchase would be income. An
  amount column whose cells are all negative is read as money out with no
  doubt: no export prints money in that way save a card export of
  payments alone, and a sign on trust is never written into a liability
  account (below). A lone `Inflow` or credit column of negatives stays a
  doubt. A running balance is no proof under a card header or
  when it has most rows as money in (what is owed on a card adds up with
  purchases read as money in), unless direction words go with the signs
  the same way (a savings export with `Credit`/`Debit` beside its signed
  amounts); nor is a debit or credit column that mixes
  signs, most of them negative. Words that go against the signs, or with
  them against a reconciling balance, are the doubt
  `indicator_contradicts_sign`. A header naming a security, a quantity
  and a price is `investment_columns`: trades are never a confident cash
  ledger. A debit or credit cell keeps its printed sign (a negative debit
  is money in, a negative credit money out, a lone `Inflow` column of
  negatives is money out), unless that column prints its amounts negative
  as a rule, where it is read by magnitude.
- **Day/month order.** A date column that fits month first and day first
  alike is read month first and is the doubt `date_order_guessed` unless
  the table settles the order (`_settled_date_format`; the whole table is
  asked, not the sample): a cell only one order reads; a second column
  named a date, of the same form, with such a cell; one order dating two
  rows or more after the day the file arrived (plus a day's slack) while
  the other dates none (the importer and the preview pass the upload
  day; one row dated ahead, a scheduled payment, rules nothing out);
  every cell the same day either way (`1/1`, `2/2`); or, read month
  first, at least three distinct days of one month of one year (an export
  covers the days of a month, not the same day of as many months). Proofs
  that disagree, two rows, the same day of several months, and an
  unbroken run of days with as many rows each (what one row a month on
  the 1st looks like written day first) stay held.
- **Tables without a header row** (`_headerless`). When no row names a
  date and a money column, a table of two rows or more is read from the
  shapes of its columns (`has_header` false, every role's match
  `content`): the date column is the first whose cells all read in one
  date format, money columns are those whose cells all fit money, the
  description is the column with the most text; no account column is
  read from shapes. A first row of words no role knows fits no shape, so
  an unknown header still gives no suggestion. With one date column and
  two to six money columns, every reading of them as an amount and a
  balance, or a debit, a credit and a balance, is tried: exactly one
  whose running balance adds up over the whole table (at least two
  balances checked) proves the columns and the sign (`sign_evidence:
  balance`, no doubt), unless it has most rows as money in or every
  amount of one sign (`sign_unverified`). Otherwise the reading carries
  the doubt `no_header` in place of `header_not_exact`: it fills the
  mapping editor and the file waits. A column of direction words decides
  the sign there, and the doubt stays (such a table is as a rule an
  aggregator's export of several accounts, whose account column cannot
  be told from a category without a header). A mapping saved from such
  a suggestion is named `No header, N columns`, never after the cells of
  its first transaction.
- **Header words.** The suggestion knows the words English exports use
  for each role and those of German, French, Spanish, Dutch, Italian and
  Portuguese ones (`Buchungstag`, `Betrag`, `Libellé`, `Débit`/`Crédit`,
  `Fecha`, `Importe`, `Omschrijving`, `Af Bij`, `Saldo`...; `ROLE_WORDS`),
  and reads a unit after a money header as part of it (`Bedrag (EUR)`,
  `Amount (USD)`); any other parenthesised qualifier (`Amount (total)`,
  `Date (UTC)`) is read the same way unless it names a part (`fee`, `tax`,
  `vat`, `tip`) or is all that tells two columns apart. `Created`,
  `Datetime`, `Timestamp` and `Date Processed` name the date, `Credit
  Amount`/`Debit Amount` the credit and debit columns, and `Gross` is the
  amount beside a `Fee` and a `Net` column (a payment service's export;
  an exact match only when gross and net agree on every row). Dates may
  carry a zone offset after a blank or be written `31Jan2026` or
  `31-Jan-26`; a currency may be written in letters (`zł`, `Kč`). A word
  alone never assigns a role: the column's values
  must fit it, which also settles a word two languages use differently
  (`Valuta` is a value date in German and the currency in Dutch and
  Italian). Decimal commas and day-first dates are detected from the
  values as before.
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
  re-run after a person picks the card, next month's file), or a file
  whose own suggestion doubts the sign (whatever the account, which also
  stops a mapping saved from a card export by an earlier release), the
  import waits as `mapping_required` (`auto_import: sign_unverified`)
  until a person saves the mapping's spec in the editor or chooses the
  mapping explicitly for the run. A suggestion whose only doubt is the
  sign waits with the same `auto_import: sign_unverified`; any other
  doubt is `not_confident`. The account proposed for a table
  (`app/domain/imports/proposals.py`) is a credit card when the header
  names one, has no type when the mapping's sign is on trust (a person
  chooses; `AccountProposal.type` is nullable) and is a checking account
  otherwise. With one account-bound mapping among the fitting
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
  The review queue asks the question of the suggestion's doubt (trades,
  no header row, words against signs, a sign nothing proves; `reviewCopy.ts`),
  each answered in the editor. The preview also carries a brokerage
  activity suggestion (its roles are in the response schema), so an
  `activity_unreadable` item leads to the editor too. The upload list
  links "Map columns" only for a file with an open item the editor
  answers (`next_step: column_mapping`).
- **Nothing to import.** A table that is only a header row (`header_only`:
  an export of a date range without transactions), and a PDF or image no
  parser read whose page tables hold no dated row (`pages_without_rows`
  in `app/domain/review/layout.py`: a summary box of labels and totals),
  have nothing to map: they wait as `unparsed` with reason `no_rows`
  (`source`: `table` or `pages`), and the mapping editor is not offered.
  The test for a dated row is deliberately wide (any date shape, with or
  without a year, beside a second cell with a digit), so a page table
  that might be mappable is offered as before. Two limits of these tests
  as they stand:
  - Only a table of a header row and no other row says `no_rows`. A
    header row followed by a "Total" row or a "No transactions" line is
    two rows: it is asked about like any other table (`mapping_required`,
    the mapping editor), although no mapping reads a transaction from it.
  - The page-table test reads a date within one cell. It does not
    recognise a month and a day printed in separate cells (`Jan` | `15`,
    `01` | `15`), nor a date of numbers separated by spaces (`15 01 2026`,
    `2026 01 15`; `15 Jan 2026` is recognised). A page table whose rows
    are all dated that way waits as `no_rows` with no mapping editor,
    although it holds transactions.
- **Records the extractor did not read.** The OFX, QFX and QIF extractors
  count the records they could not read in `transaction_skipped:<n>`
  warnings ("Extract"). The importer adds them up (`records_skipped`;
  only a short ASCII count is read from a warning) and raises one
  `low_confidence_field` item per document (`lines_skipped`: `count`,
  `source: extraction`), for bank and brokerage statements alike. The
  rows that were read are imported, and the document ends `needs_review`
  instead of `processed` with fewer rows than the file has records.
- **Rows.** Mapped rows that do not parse (rows without an amount, a
  date in another form, a second section laid out differently) are skipped
  and listed in a `low_confidence_field` item (`rows_skipped`); rows with
  at most one filled cell (disclaimer paragraphs, a "Date downloaded"
  line), or without a date and without an amount, are notes, neither
  imported nor reported. A one-cell row under the description of the
  transaction row directly above it continues that description and is
  appended to the row's memo. A row whose date cell says `Pending` is not
  booked yet: counted (`pending_rows` in the run summary) and not
  imported. At most five unreadable rows after the table's last readable
  row that hold no transaction (totals, closing lines) are footers
  (`footer_rows`), not reported; more than five are all reported. A cash
  amount with more than six decimals is rounded half-even to six, as OFX
  amounts are (a share quantity is read exactly or not at all). A running
  balance printed once a day is checked over the rows since the balance
  before. An empty currency cell is the
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
  account). A column of account **names** (an aggregator's export telling
  `Everyday Checking` from `Rewards Card`) takes the account role only
  under a header that is exactly `Account`, `Account Name` or `Source
  Account`, with 2 to 20 distinct names of which those standing on two
  rows or more cover at least 60% of the rows (a payee or category column
  never splits a file), and a column of account numbers wins when the
  table has one. Rows are grouped by the case-folded,
  whitespace-collapsed name; such a statement carries `account_name` and
  no last four, so each raises its own `unknown_account` item, and the
  detected account and its proposal are named after it. Digits are never
  gathered across the words of a name into a last four, and two names
  ending in the same digits are two accounts, each asked about. The
  account a person puts a group on is remembered for the document by the
  group's digest (`_group_key`, `statement_answers`; never the name), so
  the groups can be answered one after the other.
  An account chosen on re-run takes the group it was chosen for
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

**Parser** (`app/ingestion/parsers/receipt.py`, `receipt_basic` version 2,
chosen by detection or the classifier's `receipt` kind; a mail body is read
when it scores at least 0.5):

- **What is no receipt.** A total line does not make a document a
  receipt. The parser scores 0 for a document of more than two pages
  (`MAX_RECEIPT_PAGES`), for one that prints a statement word, and for
  one that prints what a loan bill, a mortgage statement or an investment
  statement prints beside its total and no till does (`principal
  balance`, `interest rate`, `escrow`, `payoff`, `loan number`, `payment
  due date`, `past due`, `payment allocation`, `account statement`,
  `ending cash`, `vested balance`, ...). `year to date`, `due date` and
  `account number` are left out of that list on purpose: loyalty totals,
  invoices and store accounts print them on real receipts. A two-page
  document with no receipt word scores at most 0.3, below the registry's
  minimum, so it is a receipt only when the classifier says so. ("Choice"
  under "Parse" has the rule for pages with a transaction table.)

- Labels are folded for OCR (digits Tesseract confuses with letters inside
  words, `T0TAL` -> `total`; keywords OCR runs together, `AMOUNTDUE`; near
  misses, `TOTAI`). Amounts need two decimals after a point or a comma, with
  an optional symbol or ISO code, minus/parentheses/trailing minus, a tax
  letter, OCR noise around them, or a colon that cannot be a time (`94:29`).
- Lines are typed (subtotal, tax/VAT/GST, tip, discount, total, change,
  tender, item); a keyword whose amount OCR put on a line of its own
  takes the amount-only line below it. When that reading finds no
  labelled total, is contradicted, or paired no label at all, the nearest
  amount-only line (above or below, within a line and a half) and then
  the line above are tried, and the first reading whose figures add up is
  kept; when none does, the reading below stands, as before. So a receipt
  whose paper curled (each amount half a line above its label) no longer
  reads the subtotal as the tax. The amount under an unpriced item line
  is that item's price and is never paired with the label below it, and a
  reading below that nothing contradicts is kept, so no other pairing can
  make the figures add up by reading a price as the tax.
  The **consistent set** is looked for: a total (strong labels such as
  "amount due" first, then the later line, then the card payment) for which
  subtotal + tax (+ tip - discount) = total, or the items add up, or (VAT
  included) subtotal = total. A figure whose currency symbol OCR read as a
  leading digit (`8116.83` for `£116.83`) and digits whose decimal point was
  lost (`6778` on a TOTAL line) are tried only as variants that make the set
  consistent, never on their own. A missing subtotal or tax is derived.
- Date: every format of `classify/dates.py` plus `14-MAR-26`; a labelled
  date or one next to a time first; a line that says expiry, valid,
  return by, due date or the like gives a date only where a date label in the
  date's own lead-in, or a time beside it, marks it as the purchase date
  (`EXP 06/12/26  DATE 03/14/2026` keeps its second date, `03/14/2026
  11:42 AM  EXPIRES 06/12/26` its first), never the second date of a
  range or an unmarked date in front of the word, and no date at all when
  the word stands in front of none of them (`06/12/26 EXPIRY`); an
  ambiguous numeric date follows the currency (GBP, EUR and
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
action) or the item's own resolution: `low_confidence_field` corrects the
rows it names and, with `confirm`, confirms the item's pending rows;
`import_as_read` answers a brokerage statement held back by its figures
("Review queue" below). Dismissing remains possible.

**Payee and category of an imported row** (`app/domain/imports/payees.py`,
`_commit_statement`). A row's description is kept as printed (`raw_payee`).
Its payee is the household payee whose normalised name or alias the
description **is**, or **contains** as whole words: `Card Purchase 01/14
Corner Grocer #204 Springfield IL Card 7731` is payee `Corner Grocer`, and
`POS QB COFFEE 0042` is the payee with the alias `QB Coffee`. When one
payee's name is part of another's the longer wins (`Corner Grocer Deli`); a
description naming two different payees side by side is no match; names
under four characters or made of digits match only as the whole description.
The category is, in this order: the file's own (QIF, a category column);
what a rule sets; else the payee's **default category**
(`payee.default_category_id`, edited under "Categories, payees and tags";
a row a rule split is left alone). The run summary counts the last as
`payee_categorised`. So naming a payee once, with its usual category and
any odd spelling as an alias, categorises that merchant's rows on every
later import, with no model involved and nothing learned that a person
did not write down.

## Review queue

The review UI is where the user teaches the system. Each item shows the source
document page with the relevant region highlighted, the proposed values, and
the alternatives considered. Resolutions feed back:

- A layout nothing recognised -> "Teach PerFin" (offered from every item
  about a page the person may see) saves a fingerprint with "Remember this
  layout". The review flow does not offer this by itself after an
  institution is corrected.
- A row the person recognises -> "Create a rule from this" on a
  low-confidence item drafts a rule from the row. Correcting a category
  does not offer one by itself.
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
- **Which year?** A statement whose rows print no year and that prints
  no period, statement date or range has its rows placed in an assumed
  year ("Parse", "Period and year"). The pipeline raises one
  `low_confidence_field` item per document with the reason
  `year_assumed` (payload: `field` `posted_date`, `year`, the latest
  when the statements of the document differ, and `parser`), beside the
  importer's `low_confidence` item under which the rows wait pending.
  The review copy (`reviewCopy.ts`, "Which year?") names the year and
  asks the person to check it against the statement, then to confirm the
  rows or correct their dates.
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
  it without a question. Receipts and payslips are excluded, and so is
  any document the receipt, payslip, tax-form or loan parser was chosen
  for and read nothing from (next item).
- **A document of a known kind that could not be read.** A PDF or image
  whose pages the receipt, payslip, tax-form or loan parser was chosen
  for, and from which it read nothing (`layout.unread_kind`: no receipt,
  payslip, loan or tax form in the parse and no statement of any kind in
  the result; when another parser read the pages after all, the
  registry's fallback names that one and this does not apply), used to
  wait as `no_records`, `no_rows` or, when a box of labels looked like a
  table, `mapping_required`. It now waits as `unparsed` with the reason
  `receipt_unreadable`, `payslip_unreadable`, `tax_form_unreadable` or
  `loan_unreadable` (payload: `parser` and the parse `warnings`, which
  say why), and its page tables are never offered to the mapping editor,
  also when one of them has dated rows (a payment history on a bill of
  several loans): such a document cannot be mapped by hand, and "Process
  again" reads it the same way until a parser can read it.
  The review copy (`reviewCopy.ts`) says what the document looks like,
  that nothing was recorded, and what to do; a `loan_unreadable` item
  whose warnings hold `loan_bill_several_loans` says it is a bill for
  several loans and points to "Record a payment" on each loan.
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
  already received; the `new_institution` kind is resolved and shown but
  no code path raises it today, so in practice a layout is remembered
  through "Teach PerFin"), the member/account choice with "remember this
  folder" for an unknown owner, "Show receipt" beside ranked receipt
  suggestions, and "Map columns" with the page above the mapping form for a
  PDF or image table (only one with a dated row: "Nothing to import"
  above). The document library (`/documents/library`: kind,
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
  period item, the balances for a balance item). An `ocr_failed` item
  (scanned pages OCR never read) is the exception to the imported rows: it
  stays open until the person answers it or the document is read again
  through the pipeline. A fact that does not
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
  scored like a parser's (parser `layout_template/2`), so a template that
  misreads a statement is held back the same way. A template without
  tables only fills a parser's statement (the period always; the account
  and balances when there is one statement). Then the normal import runs.
- **Looking at it and taking it back.** `GET /imports/layouts` lists the
  layouts in use (one per institution, kind and parser: the version the
  classifier uses) with the phrases that recognise them, the facts and
  table columns taught, the number of ignored lines and the notes;
  `DELETE /imports/layouts/{id}` (member or above) forgets every version of
  one, so an older version is not used in its place
  (`app/domain/imports/layouts.py`; audit `parser_fingerprint.delete`). The
  next statement of that layout is read by the ordinary parsers again and
  can be taught afresh. The SPA shows both, with the saved column mappings
  (rename, delete) and the statement wording learned from confirmed
  suggestions ("Models in the pipeline"), on "What PerFin has learned"
  (`/documents/learned`).
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

A second question, for brokerage statements only (`app.ml.client.activity_hints`,
`app/ingestion/parsers/vocabulary.py`; ADR-0010's amendment). The parsers type
an activity row by its action words and its section's heading from fixed
lists; a row neither names is left unread and its words are reported
(`ParseInfo.unknown_actions` / `unknown_headings`: normalised, every digit
masked, so no reference or account number is kept). The pages are then read
once more with what is known about those words, in this order:

1. **Learned wording** (`app/domain/imports/wording.py`): what the household
   confirmed on an earlier statement of this layout. It types a row like
   the parser's own lists do: no doubt, nobody asked.
2. **The model's suggestions**, for what is still untyped and when the model
   is on.

For the second, the worker sends those phrases (`POST /v1/vocabulary`, at
most 40 of each) with the statement's text to read them in and asks what
kind of activity each is. One call carries `PERFIN_ML_MAX_CHARS` of text; a
statement that fits is sent whole (the pages that print a phrase first), a
longer one in several calls, each phrase with the page that prints it and
the pages that print none left out (`pipeline._vocabulary_batches`; at most
six calls a document, what is left over is asked in the last one). The
answers are from a closed list: an activity type for a phrase (not `split`, which needs a ratio no
phrase gives), a row-typing section kind for a heading (never `skip`: a
model may not hide a table), or `unknown`. Answers are matched to the phrases
PerFin sent by their number and only listed kinds are kept
(`validated_vocabulary`); they are stored on the document
(`confidence.ml_vocabulary`, used again without asking) and the pages are
read once more with them. A row typed this way has its date, amount,
quantity and security read from the page as any other, is checked by the
statement's reconciliation like any other, and carries the `suggested_type`
doubt (confidence 0.4, below any threshold): the importer holds it as it
holds any row in doubt ("What a statement says about its own rows": alone
when it moves cash, with its statement when it moves units) and the review
queue's **Import as read** is the person's confirmation of the suggested
kinds (`CONFIRMABLE_DOUBTS`: `reconciliation`, `suggested_type`). A heading
or phrase the parser's own lists know is never the model's to type.

**Column headings** are asked the same way. A table whose header names no
column the parser knows as the date or the amount (`Posted | Description |
Money In/Out`) is not read; when the line looks like a header (three or
more cells without an amount, at least one a column word the parser knows,
an entry under it) its unknown cells are reported
(`ParseInfo.unknown_columns`: words, no digits) and sent as `columns`. The
model says what each names from a closed list of roles (date, settlement
date, kind of transaction, description, symbol, quantity, price, amount,
fees) or `unknown`; it cannot hide a column, and a heading the parser knows
keeps the parser's meaning whatever the answer. The parser then reads the
table itself, through its ordinary header rules; every row of a table read
through a suggested heading carries the `suggested_type` doubt and waits
for a person like any suggestion (`InvestmentActivity.suggestion`:
`column:<heading>`). A statement no brokerage parser recognises at all is
not helped by this: the question is only asked about a document a parser is
already reading.

That confirmation is also what teaches: the wording of the confirmed rows
is kept for the household (`wording.remember_confirmed`, the household
setting `ingestion.learned_wording`; the resolution says `wording_learned`).
Only layout is kept. An entry is a layout (the parser's institution key or
name), a phrase and its kind: a section's heading, the action words of a
table that names its securities in a column of their own (the parser marks
those rows, `InvestmentActivity.suggestion`), or a table's column heading
and the role it names (kind `column`). The free text of a cash table
(a payee, a merchant, a memo) is typed for its own document and never
kept; a key holds no digit; a phrase the household's scrubber would mask (a
member's or the household's name, an address, a stored account number) is
refused; and no amount, date, security or account of the statement is
stored. The model itself keeps nothing: it is never trained on what it is
sent and every call stands alone. `GET /imports/wording` (any role) lists
what was learned and `DELETE /imports/wording/{id}` (member or above)
forgets a phrase (audit `learned_wording.add` / `learned_wording.delete`,
ids and counts only); the SPA shows it on "What PerFin has learned". It is
a household setting, so it is not part of a full export.

**Measuring a model.** `perfin ml eval` (`app/ml/evaluate.py`) asks the
configured adapter about PerFin's own vocabulary, the activity words and
section headings its parsers already know the kind of, without any
statement text, and prints how often the model agrees, with the phrases it
got wrong (`--json`; `--min-agreement 0.9` exits 1 below that share). The
phrases are PerFin's word lists, so nothing personal is sent, and nothing is
stored. A suggestion stays a suggestion whatever the score.

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
activity counts, figures and reconciliation; per table the suggested
mapping, with the day/month order settled against today's date as an
import settles it against the day of the upload), and with `--household` a `match` read in a
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
   `headers`/labels/`section_titles`. Every phrase of `fingerprints` must
   be on page 1 for the parser to claim a document. Code only when the
   layout needs it. Listed in `INSTITUTION_PARSERS`.
4. **Version.** Bump `version` whenever the output for the same pages may
   change; the stored `parser` reference says which version read a
   statement, and `reparse` re-runs from the stored pages.
5. **Tests.** The unit test parametrisation in
   `tests/unit/test_statement_parsers.py` (exact rows, balances, period,
   last four, currency, full confidence, reconciliation, the tampered
   variant failing) and, for a new kind of layout, an end-to-end case in
   `tests/integration/test_pdf_statement_pipeline.py`.

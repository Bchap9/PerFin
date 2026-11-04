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

Phase 2 status (`app/ingestion/intake/`): browser and token uploads
(`POST /documents`) only; the drop folder and email intake are Phase 3. All
the types above are accepted by the sniffer and stored, but only CSV/TSV,
OFX/QFX and QIF are extracted; any other type is kept and waits in review
(`unparsed`, `format_not_supported`) until its extractor lands. ZIP
expansion is not built yet (a ZIP is stored as one document). Auto-dedupe is
not built: a byte-identical upload always becomes a `possible_duplicate`
item (`payload.reason: same_content`, `duplicate_of` the earlier document).

### 2. Scan (optional)
If the ClamAV sidecar is enabled, the quarantined blob is scanned. Positive
results move the document to `rejected` with the signature name recorded.

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
seccomp filter is **default-allow with a deny list** (`EPERM` for sockets,
`execve`, `fork`/non-thread `clone`, signals to other processes, `ptrace`,
mounts and namespaces, BPF, `io_uring`, keyrings, identity changes, every
call that changes the filesystem, and `open`/`openat` for writing, ...): an
allowlist was not chosen yet because CPython and the Phase 3 PDF/OCR
libraries need a large, version-dependent syscall set; the container's
`cap_drop: ALL`, read-only root and missing egress are the outer layers.
When the worker runs as root, `PERFIN_EXTRACT_UID` makes the child switch to
that UID; the shipped image runs the worker as 10001 and the child shares
that UID, isolated by the layers above. The parent kills the process group after
`PERFIN_EXTRACT_TIMEOUT_SECONDS` or when stdout exceeds
`PERFIN_EXTRACT_MAX_OUTPUT_MB`, and validates stdout against a strict
Pydantic schema. Extractors are registered by kind
(`app/ingestion/extract/registry.py`); Phase 2 ships CSV/TSV, OFX/QFX (SGML
and XML) and QIF.

- PDF: PyMuPDF (fitz) for text and layout; fall back to OCR (Tesseract via
  `ocrmypdf`/`pytesseract`) when the text layer is missing or looks like
  garbage. PDF JavaScript, embedded files and external links are never
  followed; the renderer is told to ignore them.
- Images: deskew, denoise, OCR with layout analysis. Receipt-specific OCR
  profile (narrow, thermal-print fonts).
- CSV/TSV/XLSX: parsed with strict dialect detection; formulas in XLSX are
  never evaluated (values only). Cells beginning with `=`, `+`, `-`, `@` are
  neutralised so a later CSV export cannot carry formula injection.
- OFX/QFX/QIF: dedicated parsers; XML external entities disabled.
- EML: headers, text/HTML body (HTML sanitised to text), attachments expanded
  through the same intake with depth limit 1.

### 4. Classify
Determine `kind` (statement, receipt, invoice, tax form, payslip, insurance,
other) and, for statements, the **institution** and **account**.

Order of attempts, each producing a confidence score:
1. **Fingerprints.** Deterministic rules per institution: header strings,
   logos' OCR text, account-number masks, column layouts. These are shipped as
   data (YAML) and can be added by the user from the review UI ("remember this
   layout").
2. **Heuristic classifier.** Small gradient-boosted or logistic model over
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
a review item describing the discrepancy. (Not built in Phase 2: structured
statements record their closing balance as a `balance_snapshot`, but the
opening + transactions = closing check and per-field confidence come with
the Phase 3 parsers; SECURITY.md C12 partial.)

### 6. Match and deduplicate
- Each parsed transaction gets an `import_hash`. Existing rows with the same
  hash are skipped; near-matches (same amount and date, different payee text)
  are surfaced as `possible_duplicate`.
- Overlapping statement periods are expected (e.g. a pending transaction that
  posts in the next cycle) and handled by hash, not by date range.
- Transfer pairing runs across accounts.
- Receipt -> transaction linking: match on amount, date within +/- 3 days,
  merchant alias. Confident match links the document; otherwise a review item
  "attach this receipt to…" with suggestions.
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

### 7. Commit
Within one database transaction: write transactions/holdings/payments, link the
document, update balance snapshots, move the blob from quarantine to permanent,
set `document.status`, emit events. Anything below the configured confidence
threshold (default 0.85 per field) is written with `review_state=pending` and a
`review_item`, so it is visible but marked and excluded from reports until
confirmed (configurable).

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

Institution fingerprints and parsers live in `backend/app/ingestion/parsers/`
as code plus YAML, with a test fixture format that uses **synthetic**
statements (never real ones). A contributor adds a fixture, a fingerprint and,
if needed, a parser subclass. A CLI `perfin ingest --dry-run file.pdf` prints
every stage's output for debugging without writing to the database.

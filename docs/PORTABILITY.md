# Full export and import

VISION.md promises "Everything exportable": if PerFin disappears tomorrow,
the data walks out with the user. This document specifies the export
archive, its optional passphrase encryption, and how an archive is imported
again. Code: `backend/app/domain/portability/`,
`backend/app/core/archive_crypto.py`, `backend/app/api/routers/portability.py`,
`backend/app/workers/handlers/portability.py`. Endpoints: [API.md](API.md)
("Export and import notes").

## Who may do it

An owner or admin of the household, from a **browser session**, with a
step-up verification in the last 10 minutes (AUTH.md: "export everything").
Personal access tokens are refused, even with the `export` scope (step-up
exists only for sessions). Owners and admins see every account, so an
export is always the whole household. The state of an export or import and
the download belong to the user who requested it; anyone else gets `404`.

## Life cycle

1. `POST /export {"passphrase"?}` creates a `portability_archive` row
   (`pending`) and queues `portability.export`. With a passphrase, its
   Argon2id key is derived in the request and stored only as an envelope
   under the master key (bound to the row id); the passphrase itself is
   never stored, logged or put in a job payload. One pending export per
   household.
2. The job writes the archive as a stream (no plaintext ever touches the
   disk) through the passphrase layer (if any) into a new blob of the blob
   store (ADR-0003: a fresh data key wrapped by the master key, `permanent`
   area), records its size and SHA-256 (of the file the user will receive),
   wipes the wrapped passphrase key and marks the row `ready` for
   `PERFIN_EXPORT_TTL_MINUTES` (default 60). `portability.expire` is queued
   for the expiry time.
3. `GET /export/{job_id}/download` works **once**: it opens the decrypting
   stream, destroys the blob's key, deletes the blob row and commits (with
   the `export.download` audit row) before the first byte is sent; the file
   is unlinked after the commit while the already-open stream finishes. A
   second download is `410 export-already-downloaded`; after the expiry it
   is `410 export-expired` and the archive is destroyed.
4. `POST /import` (multipart, one part `file`) streams the upload into the
   `quarantine` area under a new blob key, at most `PERFIN_IMPORT_MAX_MB`
   (default 512). An encrypted archive needs its passphrase in the
   `X-Archive-Passphrase` header; it is decrypted while uploading, so a
   wrong passphrase or a damaged file is refused at once (`422`). The job
   `portability.import` then validates and applies the archive and destroys
   the upload either way. One pending import per household.

Every step is audited (`export.request|complete|fail|download|expire`,
`import.request|complete|fail`; ids, counts, sizes and error slugs only) and
emits an event of the same name; a finished import also emits
`account.imported`, `transaction.imported`, `budget.imported` and
`document.imported` so open pages refresh.

## Archive layout (format version 1)

A zip archive:

| Member | Content |
| --- | --- |
| `data/<entity>.json` | A JSON array of every row of the entity, in id order |
| `csv/accounts.csv`, `csv/transactions.csv` | The same data for spreadsheets and accountants |
| `documents/<document id>/<name>` | The decrypted original of each document |
| `manifest.json` | Written last; see below |

Entities, in dependency order (`app/domain/portability/entities.py`):
`institutions`, `categories`, `payees`, `tags`, `accounts`, `documents`
(metadata, including the stored extraction), `transactions`,
`transaction_splits`, `transaction_tags`, `balance_snapshots`,
`scheduled_transactions`, `rules`, `budgets`, `budget_lines`, `goals`,
`goal_accounts`, `goal_contributions`, `fx_rates`, `import_mappings`,
`document_links`. Each row has every column of the table except
`household_id`; user references (`document.uploaded_by`,
`import_mapping.created_by`), the document's blob and its full-text index
are left out. Not exported, by design: blobs (originals are exported
decrypted), the review queue, ingestion history, learned parser
fingerprints, `import_hash_alias` (see below), members, grants, tokens,
events and the export/import jobs themselves. The Phase 5/6 tables
(securities, prices, investment transactions, lots, per-account
investment settings (`investment_account`), loans and their
payments and rate changes, manual assets and valuations, tax years,
checklist items, category-to-schedule maps and tax form data) are **not in
format version 1 yet** (listed in `NOT_EXPORTED` until the archive gains
them); the `holding` cache and `net_worth_snapshot` are derived and will
stay out.

Values (`app/domain/portability/codec.py`): UUIDs, dates and timestamps
(ISO 8601 with offset) as strings, decimals as plain decimal strings
(`"-12.500000"`), enums as their value, JSON columns as JSON, booleans and
integers as JSON booleans and numbers.

CSV files are UTF-8 with `CRLF` line ends and a header row. Every cell goes
through `app.core.csv_safety.neutralise_formula` (SECURITY.md C7): text
starting with `=`, `+`, `-`, `@`, TAB or CR gets a leading `'` unless it is a
plain number. `transactions.csv` has one line per transaction (splits in a
`splits` column as `Category path: amount; ...`); `accounts.csv` adds the
balance on the export day. The JSON members keep the stored values unchanged.

`<name>` of a document original is its stored file name reduced to ASCII
letters, digits, `.`, `_`, `-` and spaces, without leading or trailing dots
and spaces, at most 120 characters (`document` when nothing is left).

`manifest.json`:

```json
{
  "format": "perfin-export",
  "format_version": 1,
  "app_version": "0.2.0",
  "created_at": "2026-11-04T10:00:00+00:00",
  "household": {"name": "...", "base_currency": "USD"},
  "counts": {"accounts": 6, "transactions": 23, "document_files": 1, "...": 0},
  "members": {"data/accounts.json": {"sha256": "<64 hex>", "size": 1234}, "...": {}}
}
```

`members` lists every other member with its SHA-256 and size.

## Encrypted archives ("PerFin encrypted archive" v1)

With a passphrase, the downloaded file (`.pfarch`) is the zip encrypted by
`app.core.archive_crypto` (also meant for backups, SECURITY.md G3):

| Offset | Bytes | Field |
| --- | --- | --- |
| 0 | 8 | magic `PFARCH\x00\x01` (the last byte is the version) |
| 8 | 1 | KDF id: `1` = Argon2id (libsodium `crypto_pwhash_ALG_ARGON2ID13`) |
| 9 | 4 | Argon2id `opslimit`, unsigned big-endian |
| 13 | 8 | Argon2id `memlimit` in bytes, unsigned big-endian |
| 21 | 16 | random salt |
| 37 | 24 | `crypto_secretstream_xchacha20poly1305` header |
| 61 | ... | encrypted chunks |

The key is Argon2id(passphrase as NFC-normalised UTF-8, salt, opslimit,
memlimit), 32 bytes; PerFin writes libsodium's "moderate" cost (3 passes,
256 MiB). The plaintext is split into 64 KiB chunks, each pushed with the
61 header bytes as associated data; all but the last carry `TAG_MESSAGE`,
the last (at most 64 KiB, possibly empty) `TAG_FINAL`. A reader refuses
KDF parameters above 10 passes or 1 GiB before deriving anything, and any
authentication failure, a missing final chunk or data after it.
Decrypting by hand (Python, PyNaCl): parse the header, derive the key with
`nacl.pwhash.argon2id.kdf(32, passphrase, salt, opslimit=..., memlimit=...)`,
`crypto_secretstream_xchacha20poly1305_init_pull(header[37:61], key)`, then
pull chunks of 65 553 bytes (the last may be shorter) with `header[:61]` as
associated data; or run `app.core.archive_crypto.decrypt_stream`.

## Import

Into the **current** household, all or nothing (one savepoint; deferred
household foreign keys are forced to be checked inside it). Before
anything is written (`app/domain/portability/import_.py`, `zipsafe.py`):

* zip safety (SECURITY.md C2, C3): at most 100 000 members; none above
  1 GiB, all together at most 16 GiB uncompressed; no member above 1 MiB
  that expands more than 200 times; no overlapping members; names are
  relative POSIX paths without `..`, `.`, empty components, backslashes,
  drive letters or control characters; no directories, links, duplicates,
  encrypted members or compression other than stored/deflated. Member names
  are never used as file system paths;
* the manifest: `format` must be `perfin-export`; an unknown
  `format_version` is `archive-version-unsupported`;
* the members must be exactly those the manifest lists, each matching its
  SHA-256 and size, each in a known place of the layout, every document row
  with exactly one original;
* every row: exactly the expected columns, strictly typed values that fit
  their columns, unique ids, counts as in the manifest; split sums, rule
  documents, import mapping specs and the category depth are validated like
  the services that normally write them.

Then every row gets a new id and references are re-mapped (a reference to a
row that is not in the archive refuses the import). Institutions (by
built-in key, then name), categories (by system key, then parent and name),
payees (by normalised name) and tags (by name) are **merged** into existing
ones; an existing import mapping with the same name and an existing FX rate
for the same pair and day win over the archive's; names that would clash
get ` (imported)`. Rules are appended after the household's own.

`import_hash` includes the account id, so it is recomputed for the new
account: the running index is recovered by trying indexes until the stored
hash matches, and the same index is hashed with the new account id.
Importing the same statement again into the new household is therefore
still recognised as a duplicate. `import_hash_alias` rows (hashes of
deleted duplicates) cannot be recomputed and are not exported, so a
statement whose duplicate was resolved by hand may raise the same review
question once more.

Document originals are checked against both the manifest and the document
row's SHA-256, sniffed again (an archive's `mime_detected` is not trusted;
a type intake would refuse is `archive-document-rejected`), and encrypted
into new blobs in the `permanent` area; a document that was still being
processed at export time arrives as `needs_review`.

Failures are recorded on the import (`GET /import/{job_id}`: `state:
failed`, `error` slug, a short `error_detail` naming the entity and column,
never a value): `archive-not-zip`, `archive-unsafe`,
`archive-manifest-invalid`, `archive-version-unsupported`,
`archive-members-mismatch`, `archive-unexpected-member`,
`archive-hash-mismatch`, `archive-invalid-value`,
`archive-reference-unknown`, `archive-document-rejected`, `archive-limit`
(rule count, category depth), `archive-inconsistent` (a database rule such
as a transaction whose currency differs from its account's),
`archive-unreadable`. The household is unchanged after any of them and
every stored file the import wrote is removed.

The household's base currency is not changed by an import; the archive's
is reported in the import summary (`archive_base_currency`).

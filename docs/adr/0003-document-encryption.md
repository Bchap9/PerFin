# ADR-0003: Envelope encryption for documents and sensitive columns

Status: Accepted. Date: 2026-11-03.

## Context
Original statements and tax forms are the most sensitive artefacts PerFin
stores. Self-hosters often run on commodity disks, keep snapshots, and copy
backups to cloud storage. Full-disk encryption on the host is recommended but
cannot be assumed.

## Decision
- Every blob is encrypted by the application before it reaches the blob store
  using **XChaCha20-Poly1305** (libsodium via PyNaCl) with a random per-object
  data encryption key (DEK). The DEK is wrapped with the **master key** and
  stored with the blob metadata in the database.
- Sensitive columns (IMAP credentials, integration keys, TOTP secrets,
  notification secrets) use the same wrap-with-master-key pattern with a
  per-row DEK.
- The master key is 256 bits, supplied at runtime via `PERFIN_MASTER_KEY`
  (env) or `PERFIN_MASTER_KEY_FILE` (mounted secret), later optionally via a
  KMS. It is never written to the data volume or logs.
- Key rotation rewraps DEKs; blobs are not re-encrypted. Deletion destroys the
  wrapped DEK (crypto-shredding).

## Alternatives considered
- **Rely on disk encryption only.** Does not protect backups, snapshots, or an
  S3 bucket.
- **Postgres pgcrypto for everything.** Keys would have to pass through SQL
  and be visible in logs/`pg_stat_statements`.
- **Per-user keys derived from passwords.** Strong, but makes shared
  households, OIDC-only users and background ingestion (no password present)
  impractical. Possible later as an optional "vault" tier.

## Consequences
- Losing the master key loses all documents. Documentation and a startup
  nag address this; backups of the key must be separate from data backups.
- Full-text search operates on extracted text stored in Postgres, which is
  therefore also sensitive; the recommendation to encrypt the Postgres volume
  stands.

# Backup, restore and key rotation

This guide covers backing up a PerFin instance, restoring it into a fresh
stack, and rotating the master key. It covers SECURITY.md rows G3
(encrypted backups, verified restore), G4 (key backup, key rotation) and G5
(crypto-shredding and backups). The tools are `scripts/backup.sh`,
`scripts/restore.sh` and these `perfin` commands: `backup-encrypt`,
`backup-decrypt`, `backup-info`, `restore-check`, `rotate-key` and
`audit verify`.

## What you must keep

A restore needs three things. Keep each one in a different place.

| Item | What it is | Where it comes from |
| --- | --- | --- |
| Backup archive | `perfin-backup-<UTC>.pfbak` and its `.sha256` sidecar | `scripts/backup.sh` |
| Backup passphrase | Encrypts the archive (Argon2id + libsodium) | You choose it: at least 12 characters |
| Master key | `PERFIN_MASTER_KEY` / `PERFIN_MASTER_KEY_FILE` and `PERFIN_MASTER_KEY_VERSION` | Generated at install: `openssl rand -base64 32` |

**The master key is never part of a backup.** Every document and secret
column is encrypted with its own data key, and the master key wraps those
data keys (ADR-0003). An archive without the matching master key restores
the ledger but not a single document. Store the key in a password manager
or as an offline copy, never next to the archives. Once it is stored, set
`PERFIN_MASTER_KEY_BACKUP_ACKNOWLEDGED=true` in `.env`. Until you do, every
`api`, `worker` and `mcp` start and every `perfin config check` logs the
warning `master_key.backup_unacknowledged` (G4).

Each archive's manifest records the key's fingerprint (`key_id`, from
`perfin backup-info`) and its version, but never the key itself. Use them
to tell which key an old archive needs.

## Taking a backup

### Docker Compose

Run this from the repository checkout, or pass `--compose-dir` to point at
the directory that holds `docker-compose.yml` and `.env`:

```sh
export PERFIN_BACKUP_PASSPHRASE_FILE=/root/perfin-backup.pass   # chmod 600
scripts/backup.sh --compose-dir deploy --output-dir /srv/backups/perfin
```

The stack must be running. The script does the following:

1. Runs `perfin audit verify` in a one-off `migrate` container, which has
   the owner role. It stops if the hash chain is broken. Otherwise it
   records `head_seq` and `head_hash` in the manifest as tamper evidence.
2. Runs `perfin backup-info` in `api` to get the version, the key
   fingerprint and the key version.
3. Runs `pg_dump -Fc` in `db` as the bootstrap superuser over the local
   socket. The PerFin tables use `FORCE ROW LEVEL SECURITY`, so the owner
   role `perfin_owner` cannot dump them: PostgreSQL refuses with "query
   would be affected by row-level security policy". The superuser can log
   in only through the socket inside the container (deploy/db-init).
4. Tars `/var/lib/perfin/documents` and `/var/lib/perfin/quarantine` in a
   one-off `api` container (`docker compose run --rm --no-deps`). These
   files are already ciphertext. Only `db` has to be running, so `docker
   compose stop api worker` before the backup gives a strictly consistent
   snapshot.
5. Writes `manifest.json`, then builds one tar
   (`manifest.json`, `db.dump`, `documents.tar`, `quarantine.tar`) and
   pipes it through `perfin backup-encrypt` in a one-off `api` container.
   The passphrase is passed with `docker compose run -e
   PERFIN_BACKUP_PASSPHRASE` from the environment, never as an argument.
6. Writes `perfin-backup-<UTC>.pfbak` (mode 0600) and
   `perfin-backup-<UTC>.pfbak.sha256`, and prints the archive path on stdout.

### Bare-metal install

```sh
export PERFIN_BACKUP_PG_URL='postgresql:///perfin?host=/var/run/postgresql&user=postgres'
export PERFIN_DOCUMENTS_DIR=/var/lib/perfin/documents
export PERFIN_QUARANTINE_DIR=/var/lib/perfin/quarantine
export PERFIN_CLI='sudo -u perfin --preserve-env=PERFIN_BACKUP_PASSPHRASE /opt/perfin/.venv/bin/perfin'
scripts/backup.sh --bare --output-dir /srv/backups/perfin
```

`PERFIN_BACKUP_PG_URL` must connect as a superuser or as a role with
`BYPASSRLS`. The script checks this and refuses any other role, because
such a role would dump nothing or fail. `PERFIN_CLI` must run `perfin`
with the instance's environment: `PERFIN_MIGRATION_DATABASE_URL` for
`audit verify`, and the master key for `backup-info`. `pg_dump`, `psql`,
`tar` and `sha256sum` must be installed, with `pg_dump` at least as new as
the server (16).

### Passphrase, staging, consistency

- **Passphrase.** The script looks in `--passphrase-file`, then
  `PERFIN_BACKUP_PASSPHRASE_FILE`, then `PERFIN_BACKUP_PASSPHRASE`. If none
  is set it prompts twice on a terminal. The passphrase must have at least
  12 characters.
- **Staging.** While the script runs, the dump sits unencrypted in a 0700
  directory under `PERFIN_BACKUP_TMPDIR` (default `$TMPDIR` or `/tmp`). The
  script removes it on exit, including on failure. Point
  `PERFIN_BACKUP_TMPDIR` at a tmpfs or an encrypted disk.
- **Consistency.** The backup is taken online. The audit head is recorded
  before the dump, so every row up to that head is in the dump. A document
  that arrives between the dump and the tar leaves an orphan file in the
  archive, which is harmless. A document deleted in that window leaves a
  row without a file. For a strictly consistent snapshot, run
  `docker compose stop api worker` first and `docker compose start api worker`
  afterwards.

### Scheduling and failure notifications

Example: a nightly run at 02:17 from root's crontab, with notifications
when it fails:

```cron
17 2 * * * cd /opt/perfin && PERFIN_BACKUP_PASSPHRASE_FILE=/root/perfin-backup.pass scripts/backup.sh --compose-dir deploy --output-dir /srv/backups/perfin --notify-on-failure >>/var/log/perfin-backup.log 2>&1
```

With `--notify-on-failure`, a failed run also runs
`perfin notify backup-failed` (in Compose, inside `worker`). That sends
`backup.failed` to every household channel subscribed to `backup_failed`
(docs/security/notifications.md). Copy the archives off the host (rsync,
restic, object storage). They are already encrypted. Keep at least one copy
offline.

## Archive format

The outer file uses the "PerFin encrypted archive" format of
`backend/app/core/archive_crypto.py` (described in docs/PORTABILITY.md), the
same format as passphrase-protected exports. It starts with the magic
`PFARCH\0\1`, the Argon2id parameters (libsodium "moderate": 3 passes,
256 MiB) and a salt. After that come `crypto_secretstream_xchacha20poly1305`
chunks of 64 KiB, with the header bound into every chunk. A wrong
passphrase, any changed byte, truncation, reordering or appended data is
detected.

Inside is a plain tar with exactly four members:

| Member | Content |
| --- | --- |
| `manifest.json` | `format` `perfin-backup`, `format_version` 1, `created_at`, `mode`, `perfin_version`, `postgres_server_version`, `pg_dump_version`, `alembic_head`, `owner_role`, `audit` (`head_seq`, `head_hash`, `rows_checked`), `master_key` (`key_id`, `key_version`), and `members` (SHA-256 and size of the other three) |
| `db.dump` | `pg_dump` custom format of the whole database |
| `documents.tar` | the permanent document area: encrypted blobs, file names are random UUIDs |
| `quarantine.tar` | the quarantine area (files not yet processed) |

## Restoring

`scripts/restore.sh` restores **into a fresh stack**. It refuses a database
with any relation in schema `public`, or a non-empty document directory,
unless you pass `--force`. `--force` drops schema `public` and empties both
directories first.

### Docker Compose

```sh
cd deploy
cp /safe/place/.env .env          # the instance's .env: same master key, key version, role passwords
docker compose up -d db           # only db: its first start creates perfin_owner and perfin_app
cd ..
PERFIN_BACKUP_PASSPHRASE_FILE=/root/perfin-backup.pass \
  scripts/restore.sh --compose-dir deploy /srv/backups/perfin/perfin-backup-20261104T021700Z.pfbak
docker compose -f deploy/docker-compose.yml up -d
```

Do not start `api` or `worker` before the restore. Their first start writes
to the database (setup token, jobs), and the restore would then refuse the
non-empty target. Use the same `.env` as the instance you backed up, or at
least the same master key, `PERFIN_MASTER_KEY_VERSION` and role names.

### Bare-metal install

```sh
export PERFIN_RESTORE_PG_URL='postgresql:///perfin?host=/var/run/postgresql&user=postgres'
export PERFIN_DOCUMENTS_DIR=/var/lib/perfin/documents
export PERFIN_QUARANTINE_DIR=/var/lib/perfin/quarantine
export PERFIN_CLI='sudo -u perfin --preserve-env=PERFIN_BACKUP_PASSPHRASE /opt/perfin/.venv/bin/perfin'
scripts/restore.sh --bare /srv/backups/perfin/perfin-backup-20261104T021700Z.pfbak
```

The target database must exist, and the roles named in the dump (the
owner, `owner_role` in the manifest, and the application role) must exist.
Create them as `deploy/db-init/10-perfin-roles.sh` does.

### What the restore verifies

Each step stops the restore when it fails:

1. If a `.sha256` sidecar is next to the archive, the archive must match it.
2. `perfin backup-decrypt` must authenticate every chunk. In Compose it
   runs in a one-off `migrate` container, which holds no master key. On
   failure nothing has been touched yet.
3. The tar must hold exactly the four members, and each member's SHA-256
   and size must match the manifest.
4. `perfin backup-info` must report the same master-key fingerprint and
   version as the manifest. Otherwise the restore stops and names both
   keys, before the database or files are touched.
5. The target database and document directories must be empty, unless you
   pass `--force`.
6. `pg_restore --exit-on-error` runs as the superuser. Triggers and RLS
   policies are created after the data is loaded, so the audit log's chain
   trigger does not rewrite restored rows. The document tars are then
   extracted, owned by UID 10001 in Compose.
7. `perfin migrate` runs. An image newer than the backup upgrades the
   restored schema. A downgrade is not supported.
8. `perfin audit verify` must pass, and the restored `audit_log` must still
   have the manifest's `head_hash` at the manifest's `head_seq`.
9. `perfin restore-check --sample 20` decrypts 20 random stored objects
   with the configured master key and compares each document's plaintext
   SHA-256 with `document.sha256`. Pass `--check-all` to check every
   object, or `--sample N` to check N. You can also run
   `perfin restore-check --all` by hand at any time
   (`docker compose run --rm --no-deps worker restore-check --all`).

### Restore verification test

`backend/tests/integration/test_backup_restore.py` runs both scripts in
`--bare` mode in CI against PostgreSQL 16. It builds a synthetic instance:
two households with generated transactions, a stored document each, a TOTP
user, a value in every master-key-encrypted column, and audit rows. It then
backs the instance up, restores it into a fresh database and fresh
directories, and compares the row count of every table, the ledger totals
per account, every document's decrypted SHA-256 and the audit-chain head.
It also checks that the restore refuses a tampered, truncated or forged
archive, a wrong passphrase, the wrong master key, a non-empty target
without `--force`, and a dump role that row-level security applies to.

## Key rotation

`perfin rotate-key` puts every data key under a new master key. It rewraps
data keys and **never re-encrypts a payload**:

- Each document's data key in `blob` is unwrapped with the old key and
  wrapped with the new one. The file on disk and `blob.nonce` stay byte for
  byte the same.
- Each encrypted column (TOTP secrets, pending TOTP enrollments, OIDC
  tokens, notification channel settings, mailbox credentials and the intake
  alias, export passphrase keys) gets a new envelope header and wrap. Its
  payload nonce and ciphertext are kept.

Each data key records the key version that wrapped it. Rotation works in
batches (`--batch-size`, default 500), and each batch is committed. A run
that is interrupted can simply be started again: it picks up only the data
keys not yet under the new version. When all are rewrapped, it **verifies**
every data key and decrypts every envelope using a keyring that holds only
the new key. Only a verified run appends `instance.master_key_rotated` to
the audit log and exits 0. An advisory lock stops two rotations from
running at once.

### Procedure (Docker Compose)

```sh
cd deploy
../scripts/backup.sh --compose-dir .           # 1. a fresh backup under the old key
docker compose stop api worker                 # 2. nothing may write with the old key
umask 077; openssl rand -base64 32 > new-master.key
# 3. back up new-master.key NOW, separately (password manager / offline copy)
PERFIN_NEW_MASTER_KEY="$(cat new-master.key)" \
  docker compose run --rm --no-deps -e PERFIN_NEW_MASTER_KEY worker rotate-key
#    progress on stderr, a JSON report on stdout; exit 1 = incomplete (see below)
# 4. switch the configured key, ONLY after the run above exited 0:
#    a) key in .env:  PERFIN_MASTER_KEY=<contents of new-master.key>
#    b) key file (PERFIN_MASTER_KEY_FILE, DEPLOYMENT.md section 3): keep the old
#       file, then put the new key in place of the mounted one:
#         sudo cp -p secrets/master.key secrets/master.key.old
#         sudo install -o 10001 -g 10001 -m 0400 new-master.key secrets/master.key
#    either way, also in .env:
#       PERFIN_MASTER_KEY_VERSION=2   (the "to_version" of the report)
docker compose up -d
docker compose exec api perfin rotate-key --status   # 5. exit 0: every data key under the configured version
cd .. && scripts/backup.sh --compose-dir deploy      # 6. a fresh backup under the new key
shred -u deploy/new-master.key
# 7. only after step 5 exited 0 and step 6 produced a backup: remove the old
#    key file (sudo shred -u deploy/secrets/master.key.old). Keep the old key
#    itself offline for as long as you keep archives made with it (below).
```

Keep the old key (the old `.env` value or `secrets/master.key.old`) until
`perfin rotate-key --status` exits 0 under the new key **and** a new backup
has been made: until then it is the only key that opens the backups made
before the rotation. If step 5 exits 1, the configured key or
`PERFIN_MASTER_KEY_VERSION` does not match the rotation report: check that
the configured key is the content of `new-master.key` and the version is the
report's `to_version`, and delete neither key until it exits 0.

On a bare-metal install, run `perfin rotate-key --new-key-file FILE` as the
service user with the instance's environment. The command options are:

| Option | Meaning |
| --- | --- |
| `--new-key-file FILE` | the new key (base64 of 32 bytes); or set `PERFIN_NEW_MASTER_KEY_FILE` or `PERFIN_NEW_MASTER_KEY`. Never on the command line |
| `--new-version N` | version of the new key (default `PERFIN_MASTER_KEY_VERSION` + 1) |
| `--batch-size N` | data keys per committed batch (default 500) |
| `--dry-run` | count the data keys that would be rewrapped; change nothing |
| `--status` | print the configured key's fingerprint and version and the data keys per version; exit 1 if any data key is under another version |

The current key always comes from `PERFIN_MASTER_KEY(_FILE)` and
`PERFIN_MASTER_KEY_VERSION`. The command connects as the application role
(`PERFIN_DATABASE_URL`) and works household by household under RLS.

**If the report says `"ok": false`**, some values did not unwrap under the
current key. Their ids are in `failed_ids`, and `remaining` shows the data
keys per version. Typically such a value was written under yet another
key. Keep the old key configured, investigate those rows, and run the
command again. Do not switch keys until a run exits 0. After you switch,
`perfin rotate-key --status` exits 1 if anything is still under another
version. This also catches a key switched without updating
`PERFIN_MASTER_KEY_VERSION`.

**Old backups keep the old key.** An archive made before the rotation holds
data keys wrapped by the old key. Its manifest names the old `key_id` and
version. To restore it, configure that key and version, then rotate again
if you like. Keep every old key for as long as you keep archives made with
it.

## Deletion and backups (G5)

Deleting a document destroys its wrapped data key (crypto-shredding), and
the file becomes unreadable on every medium. That does **not** reach archives
made before the deletion. They still contain the wrapped data key and the
ciphertext, and the master key decrypts them. To make a deleted document
unrecoverable, expire the archives made before the deletion, or rotate the
master key and destroy the old key together with every archive made under
it.

## Limits

- There is no point-in-time recovery (WAL archiving). A restore returns to
  the moment of the backup.
- An S3 blob store is not implemented (ROADMAP parking lot). The scripts
  back up the local document volumes.
- The restore does not downgrade. Restore with an image at least as new as
  `perfin_version` in the manifest.

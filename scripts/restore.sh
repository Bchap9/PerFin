#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# PerFin restore: verify, decrypt and restore a scripts/backup.sh archive
# into a fresh stack (docs/BACKUP_RESTORE.md, SECURITY.md G3).
#
#   scripts/restore.sh [--compose-dir DIR | --bare] [--passphrase-file FILE]
#                      [--force] [--sample N | --check-all] ARCHIVE
#
# Steps, each of which stops the restore on failure:
#   1. the archive's .sha256 sidecar matches (when present);
#   2. `perfin backup-decrypt` authenticates every chunk (wrong passphrase,
#      tampering, truncation and appended data are refused);
#   3. the tar holds exactly manifest.json, db.dump, documents.tar and
#      quarantine.tar, and every member's SHA-256 and size match the
#      manifest;
#   4. the configured master key is the one the backup was made with
#      (fingerprint and version from `perfin backup-info`);
#   5. the target database and document directories are empty, unless
#      --force (which drops schema public and empties the directories);
#   6. pg_restore as the superuser, then the document volumes;
#   7. `perfin migrate` (a newer image upgrades the restored schema);
#   8. `perfin audit verify` passes and the restored chain still has the
#      manifest's head hash at the manifest's head seq (tamper evidence);
#   9. `perfin restore-check` decrypts a random sample of documents (all
#      with --check-all) with the master key and compares their SHA-256.
#
# Modes
#   --compose-dir DIR (default: this repository's deploy/): a fresh Compose
#       stack with only `db` running (`docker compose up -d db`; the roles
#       perfin_owner and perfin_app come from deploy/db-init) and the same
#       .env as the backed-up instance (same master key). Do not start api or
#       worker before the restore: their first start writes to the database.
#   --bare: a host install, with
#       PERFIN_RESTORE_PG_URL  libpq URL of the (empty) target database as a
#                              superuser (required); the roles named in the
#                              dump must exist
#       PERFIN_DOCUMENTS_DIR, PERFIN_QUARANTINE_DIR, PERFIN_CLI as for
#                              backup.sh (PERFIN_CLI's environment points at
#                              the target database and holds the master key)
#
# Passphrase as for backup.sh (--passphrase-file, PERFIN_BACKUP_PASSPHRASE_FILE,
# PERFIN_BACKUP_PASSPHRASE or a prompt). Plaintext staging under
# PERFIN_BACKUP_TMPDIR as for backup.sh, removed on exit.

set -Eeuo pipefail
umask 077

readonly FORMAT="perfin-backup"
readonly FORMAT_VERSION=1
readonly MEMBERS="db.dump documents.tar manifest.json quarantine.tar"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mode=compose
compose_dir="$script_dir/../deploy"
passphrase_file="${PERFIN_BACKUP_PASSPHRASE_FILE:-}"
force=false
sample=20
check_all=false
archive=""

usage() {
    sed -n '7,8p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

log() { printf 'perfin restore: %s\n' "$*" >&2; }
die() {
    log "ERROR: $*"
    exit 1
}

while (($#)); do
    case "$1" in
        --compose-dir)
            [[ $# -ge 2 ]] || usage
            mode=compose compose_dir="$2"
            shift 2
            ;;
        --bare)
            mode=bare
            shift
            ;;
        --passphrase-file)
            [[ $# -ge 2 ]] || usage
            passphrase_file="$2"
            shift 2
            ;;
        --force)
            force=true
            shift
            ;;
        --sample)
            [[ $# -ge 2 && $2 =~ ^[1-9][0-9]*$ ]] || usage
            sample="$2"
            shift 2
            ;;
        --check-all)
            check_all=true
            shift
            ;;
        -h | --help) usage ;;
        -*) usage ;;
        *)
            [[ -z $archive ]] || usage
            archive="$1"
            shift
            ;;
    esac
done
[[ -n $archive ]] || usage
[[ -f $archive && -r $archive ]] || die "archive not found or not readable: $archive"

# ---------------------------------------------------------------- helpers --
read -r -a perfin_cli <<<"${PERFIN_CLI:-perfin}"

compose() { docker compose -f "$compose_dir/docker-compose.yml" "$@"; }

db_query() { # psql as the superuser on the target database
    if [[ $mode == compose ]]; then
        # shellcheck disable=SC2016 # expanded by the container's shell
        compose exec -T db sh -c \
            'PGPASSWORD="$POSTGRES_PASSWORD" exec psql --no-psqlrc -X -qtA -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"' \
            sh "$1"
    else
        psql --no-psqlrc -X -qtA -v ON_ERROR_STOP=1 -d "$PERFIN_RESTORE_PG_URL" -c "$1"
    fi
}

run_perfin_owner() { # needs the owner role: migrate, audit verify; no master key
    if [[ $mode == compose ]]; then
        compose run --rm --no-deps -T migrate "$@"
    else
        "${perfin_cli[@]}" "$@"
    fi
}

run_perfin_app() { # needs the master key (and the app role, document volumes)
    if [[ $mode == compose ]]; then
        compose run --rm --no-deps -T worker "$@"
    else
        "${perfin_cli[@]}" "$@"
    fi
}

# Run a command with access to the document volumes (compose: as UID 10001).
in_volumes() { # in_volumes PROGRAM ARGS...
    local program="$1"
    shift
    if [[ $mode == compose ]]; then
        compose run --rm --no-deps -T --entrypoint "$program" worker "$@"
    else
        "$program" "$@"
    fi
}

json_field() {
    sed -n "s/.*\"$1\": *\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p"
}
member_field() { # member_field MEMBER FIELD < manifest.json
    sed -n "s/.*\"$1\": {\"sha256\": \"\([0-9a-f]*\)\", \"size\": \([0-9]*\)}.*/\\$2/p"
}

staging=""
cleanup() {
    local status=$?
    if [[ -n $staging && -d $staging ]]; then
        rm -rf -- "$staging"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 1' INT TERM

# ------------------------------------------------------------ passphrase --
if [[ -n $passphrase_file ]]; then
    [[ -r $passphrase_file ]] || die "passphrase file is not readable"
    passphrase="$(<"$passphrase_file")"
elif [[ -n ${PERFIN_BACKUP_PASSPHRASE:-} ]]; then
    passphrase="$PERFIN_BACKUP_PASSPHRASE"
elif [[ -t 0 ]]; then
    read -r -s -p "Backup passphrase: " passphrase
    echo >&2
else
    die "no passphrase: use --passphrase-file, PERFIN_BACKUP_PASSPHRASE_FILE or PERFIN_BACKUP_PASSPHRASE"
fi
[[ -n $passphrase ]] || die "empty passphrase"
export PERFIN_BACKUP_PASSPHRASE="$passphrase"
unset passphrase

if [[ $mode == compose ]]; then
    command -v docker >/dev/null || die "docker is required for --compose-dir"
    [[ -f $compose_dir/docker-compose.yml ]] || die "no docker-compose.yml in $compose_dir"
    documents_dir=/var/lib/perfin/documents
    quarantine_dir=/var/lib/perfin/quarantine
else
    [[ -n ${PERFIN_RESTORE_PG_URL:-} ]] || die "--bare needs PERFIN_RESTORE_PG_URL"
    command -v pg_restore >/dev/null || die "pg_restore is required"
    command -v psql >/dev/null || die "psql is required"
    documents_dir="${PERFIN_DOCUMENTS_DIR:-/var/lib/perfin/documents}"
    quarantine_dir="${PERFIN_QUARANTINE_DIR:-/var/lib/perfin/quarantine}"
    [[ -d $documents_dir ]] || die "documents directory not found: $documents_dir"
    [[ -d $quarantine_dir ]] || die "quarantine directory not found: $quarantine_dir"
fi

staging="$(mktemp -d "${PERFIN_BACKUP_TMPDIR:-${TMPDIR:-/tmp}}/perfin-restore.XXXXXX")"
chmod 700 "$staging"

# ---------------------------------------------------- 1-3: verify + decrypt --
if [[ -f $archive.sha256 ]]; then
    expected="$(cut -d' ' -f1 <"$archive.sha256")"
    actual="$(sha256sum "$archive" | cut -d' ' -f1)"
    [[ $expected == "$actual" ]] || die "the archive does not match $archive.sha256"
    log "archive checksum matches $archive.sha256"
fi
log "decrypting and authenticating the archive"
if [[ $mode == compose ]]; then
    compose run --rm --no-deps -T -e PERFIN_BACKUP_PASSPHRASE migrate backup-decrypt \
        <"$archive" >"$staging/plain.tar" \
        || die "decryption failed (wrong passphrase, or the archive was changed or truncated)"
else
    "${perfin_cli[@]}" backup-decrypt <"$archive" >"$staging/plain.tar" \
        || die "decryption failed (wrong passphrase, or the archive was changed or truncated)"
fi

listing="$(tar -tf "$staging/plain.tar" | LC_ALL=C sort | tr '\n' ' ')"
[[ $listing == "$MEMBERS " ]] || die "unexpected archive members: $listing"
mkdir "$staging/x"
tar -C "$staging/x" --no-same-owner -xf "$staging/plain.tar" manifest.json db.dump documents.tar quarantine.tar
rm -f -- "$staging/plain.tar"
manifest="$staging/x/manifest.json"
grep -q "\"format\": \"$FORMAT\"" "$manifest" || die "not a PerFin backup manifest"
grep -q "\"format_version\": $FORMAT_VERSION," "$manifest" || die "unsupported backup format version"
for member in db.dump documents.tar quarantine.tar; do
    want_sha="$(member_field "$member" 1 <"$manifest")"
    want_size="$(member_field "$member" 2 <"$manifest")"
    [[ -n $want_sha && -n $want_size ]] || die "manifest has no entry for $member"
    [[ "$(stat -c %s "$staging/x/$member")" == "$want_size" ]] || die "$member: size differs from the manifest"
    [[ "$(sha256sum "$staging/x/$member" | cut -d' ' -f1)" == "$want_sha" ]] \
        || die "$member: SHA-256 differs from the manifest"
done
log "manifest and members verified"
want_key_id="$(json_field key_id <"$manifest")"
want_key_version="$(json_field key_version <"$manifest")"
audit_seq="$(json_field head_seq <"$manifest")"
audit_hash="$(json_field head_hash <"$manifest")"
backup_version="$(json_field perfin_version <"$manifest")"

# ------------------------------------------------------ 4: the master key --
info_json="$(run_perfin_app backup-info)" || die "perfin backup-info failed (is the master key configured?)"
have_key_id="$(json_field master_key_id <<<"$info_json")"
have_key_version="$(json_field master_key_version <<<"$info_json")"
if [[ $have_key_id != "$want_key_id" || $have_key_version != "$want_key_version" ]]; then
    die "the configured master key (id $have_key_id, version $have_key_version) is not the key this backup was made with (id $want_key_id, version $want_key_version); configure that key and PERFIN_MASTER_KEY_VERSION"
fi
log "master key matches (id $have_key_id, version $have_key_version); backup made by PerFin $backup_version"

# ------------------------------------------------- 5: empty targets / force --
tables="$(db_query "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S')")"
nonempty_dirs=""
for dir in "$documents_dir" "$quarantine_dir"; do
    if [[ -n "$(in_volumes find "$dir" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
        nonempty_dirs+=" $dir"
    fi
done
if [[ $tables != 0 || -n $nonempty_dirs ]]; then
    if [[ $force != true ]]; then
        [[ $tables == 0 ]] || log "the target database has $tables relation(s) in schema public"
        [[ -z $nonempty_dirs ]] || log "not empty:$nonempty_dirs"
        die "refusing to overwrite existing data; restore into a fresh stack or pass --force"
    fi
    log "--force: dropping schema public and emptying the document directories"
    db_query "DROP SCHEMA public CASCADE; CREATE SCHEMA public; REVOKE ALL ON SCHEMA public FROM PUBLIC" >/dev/null
    for dir in "$documents_dir" "$quarantine_dir"; do
        in_volumes find "$dir" -mindepth 1 -delete
    done
fi

# ------------------------------------------------------- 6: database + files --
log "restoring the database"
if [[ $mode == compose ]]; then
    # shellcheck disable=SC2016 # expanded by the container's shell
    compose exec -T db sh -c \
        'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_restore --no-password --exit-on-error -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
        <"$staging/x/db.dump" || die "pg_restore failed"
else
    pg_restore --no-password --exit-on-error -d "$PERFIN_RESTORE_PG_URL" "$staging/x/db.dump" \
        || die "pg_restore failed"
fi
log "restoring the document volumes"
for area in documents quarantine; do
    dir_var="${area}_dir"
    in_volumes tar -C "${!dir_var}" --no-same-owner -xf - <"$staging/x/$area.tar" \
        || die "could not extract $area.tar"
done

# -------------------------------------------------------------- 7: migrate --
log "applying migrations"
if [[ $mode == compose ]]; then
    compose run --rm --no-deps -T migrate >&2 || die "perfin migrate failed"
else
    "${perfin_cli[@]}" migrate >&2 || die "perfin migrate failed"
fi

# ---------------------------------------------------------- 8: audit chain --
log "verifying the audit-log hash chain"
audit_json="$(run_perfin_owner audit verify)" || die "the restored audit chain does not verify: $audit_json"
[[ "$(json_field ok <<<"$audit_json")" == true ]] || die "the restored audit chain does not verify: $audit_json"
if [[ $audit_seq != 0 ]]; then
    [[ $audit_seq =~ ^[0-9]+$ && $audit_hash =~ ^[0-9a-f]{64}$ ]] || die "malformed audit head in the manifest"
    restored_hash="$(db_query "SELECT hash FROM audit_log WHERE seq = $audit_seq")"
    [[ $restored_hash == "$audit_hash" ]] \
        || die "the restored audit log does not have the manifest's head hash at seq $audit_seq"
fi
log "audit chain verified up to seq $(json_field head_seq <<<"$audit_json"); manifest head seq $audit_seq matches"

# ------------------------------------------------------ 9: document check --
log "decrypting documents with the master key"
if [[ $check_all == true ]]; then
    check_json="$(run_perfin_app restore-check --all)" || die "document check failed: $check_json"
else
    check_json="$(run_perfin_app restore-check --sample "$sample")" || die "document check failed: $check_json"
fi
log "document check: $check_json"

log "restore complete and verified"
if [[ $mode == compose ]]; then
    log "next: docker compose -f $compose_dir/docker-compose.yml up -d"
fi

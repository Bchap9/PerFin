#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# PerFin backup: one passphrase-encrypted archive holding the database, the
# document volumes and a manifest (docs/BACKUP_RESTORE.md, SECURITY.md G3).
#
#   scripts/backup.sh [--compose-dir DIR | --bare] [--output-dir DIR]
#                     [--passphrase-file FILE] [--notify-on-failure]
#
# The archive (perfin-backup-<UTC timestamp>.pfbak) is a tar of
#
#   manifest.json    format, PerFin/PostgreSQL/pg_dump versions, Alembic
#                    head, the verified audit-log head (seq + hash, tamper
#                    evidence), the master key's fingerprint and version
#                    (never the key), SHA-256 and size of every member
#   db.dump          pg_dump custom format
#   documents.tar    the permanent document area (already encrypted blobs)
#   quarantine.tar   the quarantine area (already encrypted blobs)
#
# encrypted with `perfin backup-encrypt` (the "PerFin encrypted archive"
# format of backend/app/core/archive_crypto.py: Argon2id + libsodium
# secretstream; any change, truncation or wrong passphrase is detected).
# The MASTER KEY IS NOT IN THE BACKUP: without it the documents and secret
# columns cannot be decrypted. Back it up separately.
#
# Modes
#   --compose-dir DIR (default: the directory of this repository's
#       deploy/docker-compose.yml) uses `docker compose` against a running
#       stack: pg_dump runs in `db` as the bootstrap superuser over the local
#       socket (PerFin tables use FORCE ROW LEVEL SECURITY, so the owner role
#       perfin_owner cannot dump them); `perfin audit verify` runs in a
#       one-off `migrate` container (owner role); the documents are read and
#       the archive is encrypted in one-off `api` containers (`compose run`,
#       so this also works with api and worker stopped, as recommended
#       below for a consistent snapshot; only `db` must be running).
#   --bare runs against a host install with these variables:
#       PERFIN_BACKUP_PG_URL   libpq URL of the PerFin database for pg_dump,
#                              as a superuser or a BYPASSRLS role (required)
#       PERFIN_DOCUMENTS_DIR   default /var/lib/perfin/documents
#       PERFIN_QUARANTINE_DIR  default /var/lib/perfin/quarantine
#       PERFIN_CLI             the perfin command, with its environment
#                              (PERFIN_MIGRATION_DATABASE_URL and the master
#                              key) already set; default "perfin"
#
# Passphrase: --passphrase-file FILE, else PERFIN_BACKUP_PASSPHRASE_FILE,
# else PERFIN_BACKUP_PASSPHRASE, else an interactive prompt (twice). At
# least 12 characters. It is never put on a command line.
#
# Plaintext staging: the database dump exists unencrypted for the duration
# of the run in a 0700 directory under PERFIN_BACKUP_TMPDIR (default
# $TMPDIR or /tmp), removed on exit. Point it at a tmpfs or an encrypted
# disk.
#
# Consistency: the backup is taken online. A document added while it runs
# may be missing from the dump (its file is then an orphan, harmless); for
# a strictly consistent snapshot stop api and worker first
# (`docker compose stop api worker`).
#
# Exit status: 0 on success; 1 on any failure (the partial archive is
# removed). With --notify-on-failure a failure also runs
# `perfin notify backup-failed` (compose: in the worker container; bare:
# with PERFIN_CLI).

set -Eeuo pipefail
umask 077

readonly FORMAT="perfin-backup"
readonly FORMAT_VERSION=1
readonly MIN_PASSPHRASE_CHARS=12

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mode=compose
compose_dir="$script_dir/../deploy"
output_dir="."
passphrase_file="${PERFIN_BACKUP_PASSPHRASE_FILE:-}"
notify_on_failure=false

usage() {
    sed -n '6,8p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

log() { printf 'perfin backup: %s\n' "$*" >&2; }
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
        --output-dir)
            [[ $# -ge 2 ]] || usage
            output_dir="$2"
            shift 2
            ;;
        --passphrase-file)
            [[ $# -ge 2 ]] || usage
            passphrase_file="$2"
            shift 2
            ;;
        --notify-on-failure)
            notify_on_failure=true
            shift
            ;;
        -h | --help) usage ;;
        *) usage ;;
    esac
done

# ---------------------------------------------------------------- helpers --
read -r -a perfin_cli <<<"${PERFIN_CLI:-perfin}"

compose() { docker compose -f "$compose_dir/docker-compose.yml" "$@"; }

# Run psql as the database superuser (compose) or PERFIN_BACKUP_PG_URL (bare).
db_query() {
    if [[ $mode == compose ]]; then
        # shellcheck disable=SC2016 # expanded by the container's shell
        compose exec -T db sh -c \
            'PGPASSWORD="$POSTGRES_PASSWORD" exec psql --no-psqlrc -X -qtA -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"' \
            sh "$1"
    else
        psql --no-psqlrc -X -qtA -v ON_ERROR_STOP=1 -d "$PERFIN_BACKUP_PG_URL" -c "$1"
    fi
}

run_perfin_owner() { # perfin commands that need the owner role (audit verify)
    if [[ $mode == compose ]]; then
        compose run --rm --no-deps -T migrate "$@"
    else
        "${perfin_cli[@]}" "$@"
    fi
}

run_perfin_app() { # perfin commands that need the master key or the app role
    if [[ $mode == compose ]]; then
        compose run --rm --no-deps -T api "$@"
    else
        "${perfin_cli[@]}" "$@"
    fi
}

json_field() { # json_field NAME <<<'{"NAME": value, ...}' (flat JSON from perfin)
    sed -n "s/.*\"$1\": *\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p"
}

file_size() { stat -c %s "$1"; }
file_sha256() { sha256sum "$1" | cut -d' ' -f1; }

staging=""
partial=""
cleanup() {
    local status=$?
    if [[ -n $staging && -d $staging ]]; then
        rm -rf -- "$staging"
    fi
    if [[ -n $partial && -e $partial ]]; then
        rm -f -- "$partial"
    fi
    if ((status != 0)) && [[ $notify_on_failure == true ]]; then
        log "sending backup.failed notifications"
        if [[ $mode == compose ]]; then
            compose run --rm --no-deps -T worker notify backup-failed >&2 || true
        else
            "${perfin_cli[@]}" notify backup-failed >&2 || true
        fi
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
    read -r -s -p "Repeat passphrase: " again
    echo >&2
    [[ $passphrase == "$again" ]] || die "the passphrases differ"
    unset again
else
    die "no passphrase: use --passphrase-file, PERFIN_BACKUP_PASSPHRASE_FILE or PERFIN_BACKUP_PASSPHRASE"
fi
((${#passphrase} >= MIN_PASSPHRASE_CHARS)) \
    || die "the passphrase must have at least $MIN_PASSPHRASE_CHARS characters"
export PERFIN_BACKUP_PASSPHRASE="$passphrase"
unset passphrase

# ---------------------------------------------------------------- checks --
command -v sha256sum >/dev/null || die "sha256sum is required"
command -v tar >/dev/null || die "tar is required"
if [[ $mode == compose ]]; then
    command -v docker >/dev/null || die "docker is required for --compose-dir"
    [[ -f $compose_dir/docker-compose.yml ]] || die "no docker-compose.yml in $compose_dir"
    documents_dir=/var/lib/perfin/documents
    quarantine_dir=/var/lib/perfin/quarantine
else
    [[ -n ${PERFIN_BACKUP_PG_URL:-} ]] || die "--bare needs PERFIN_BACKUP_PG_URL"
    command -v pg_dump >/dev/null || die "pg_dump is required"
    command -v psql >/dev/null || die "psql is required"
    documents_dir="${PERFIN_DOCUMENTS_DIR:-/var/lib/perfin/documents}"
    quarantine_dir="${PERFIN_QUARANTINE_DIR:-/var/lib/perfin/quarantine}"
    [[ -d $documents_dir ]] || die "documents directory not found: $documents_dir"
    [[ -d $quarantine_dir ]] || die "quarantine directory not found: $quarantine_dir"
    can_dump="$(db_query "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")"
    [[ $can_dump == t ]] \
        || die "PERFIN_BACKUP_PG_URL must connect as a superuser or a BYPASSRLS role (PerFin tables force row-level security)"
fi
mkdir -p -- "$output_dir"
[[ -d $output_dir && -w $output_dir ]] || die "output directory is not writable: $output_dir"

staging="$(mktemp -d "${PERFIN_BACKUP_TMPDIR:-${TMPDIR:-/tmp}}/perfin-backup.XXXXXX")"
chmod 700 "$staging"

# -------------------------------------------- audit head (before the dump) --
# Recorded first, so every row up to this head is in the dump; restore.sh
# checks that the restored chain verifies and still has this hash at this seq.
log "verifying the audit-log hash chain"
audit_json="$(run_perfin_owner audit verify)" || die "perfin audit verify failed: $audit_json"
[[ "$(json_field ok <<<"$audit_json")" == true ]] || die "the audit chain does not verify: $audit_json"
audit_seq="$(json_field head_seq <<<"$audit_json")"
audit_hash="$(json_field head_hash <<<"$audit_json")"
audit_rows="$(json_field rows_checked <<<"$audit_json")"

info_json="$(run_perfin_app backup-info)" || die "perfin backup-info failed"
perfin_version="$(json_field perfin_version <<<"$info_json")"
key_id="$(json_field master_key_id <<<"$info_json")"
key_version="$(json_field master_key_version <<<"$info_json")"

server_version="$(db_query "SHOW server_version")"
alembic_head=""
if [[ "$(db_query "SELECT to_regclass('public.alembic_version') IS NOT NULL")" == t ]]; then
    alembic_head="$(db_query "SELECT coalesce(string_agg(version_num, ',' ORDER BY version_num), '') FROM alembic_version")"
fi
owner_role="$(db_query "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'public'")"

# ------------------------------------------------------------------- dump --
log "dumping the database (pg_dump custom format)"
if [[ $mode == compose ]]; then
    # shellcheck disable=SC2016 # expanded by the container's shell
    compose exec -T db sh -c \
        'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump --no-password -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
        >"$staging/db.dump"
    pg_dump_version="$(compose exec -T db pg_dump --version)"
else
    pg_dump --no-password -d "$PERFIN_BACKUP_PG_URL" -Fc -f "$staging/db.dump"
    pg_dump_version="$(pg_dump --version)"
fi
[[ -s $staging/db.dump ]] || die "pg_dump wrote nothing"

# -------------------------------------------------------------- documents --
log "archiving the document volumes (ciphertext only)"
for area in documents quarantine; do
    dir_var="${area}_dir"
    if [[ $mode == compose ]]; then
        compose run --rm --no-deps -T --entrypoint tar api -C "${!dir_var}" -cf - . >"$staging/$area.tar"
    else
        tar -C "${!dir_var}" -cf "$staging/$area.tar" .
    fi
done

# --------------------------------------------------------------- manifest --
member_json() {
    printf '"%s": {"sha256": "%s", "size": %s}' "$1" "$(file_sha256 "$staging/$1")" "$(file_size "$staging/$1")"
}
created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
json_string() { # escape for a JSON string (backslash and quote; values are simple)
    local value="${1//\\/\\\\}"
    printf '"%s"' "${value//\"/\\\"}"
}
{
    printf '{\n'
    printf '  "format": "%s",\n  "format_version": %s,\n' "$FORMAT" "$FORMAT_VERSION"
    printf '  "created_at": "%s",\n  "mode": "%s",\n' "$created_at" "$mode"
    printf '  "perfin_version": %s,\n' "$(json_string "$perfin_version")"
    printf '  "postgres_server_version": %s,\n' "$(json_string "$server_version")"
    printf '  "pg_dump_version": %s,\n' "$(json_string "$pg_dump_version")"
    printf '  "alembic_head": %s,\n' "$(json_string "$alembic_head")"
    printf '  "owner_role": %s,\n' "$(json_string "$owner_role")"
    printf '  "audit": {"head_seq": %s, "head_hash": "%s", "rows_checked": %s},\n' \
        "$audit_seq" "$audit_hash" "$audit_rows"
    printf '  "master_key": {"key_id": "%s", "key_version": %s},\n' "$key_id" "$key_version"
    printf '  "members": {\n    %s,\n    %s,\n    %s\n  }\n' \
        "$(member_json db.dump)" "$(member_json documents.tar)" "$(member_json quarantine.tar)"
    printf '}\n'
} >"$staging/manifest.json"

# ------------------------------------------------------- archive + encrypt --
name="perfin-backup-$(date -u +%Y%m%dT%H%M%SZ).pfbak"
target="$output_dir/$name"
partial="$target.partial"
log "encrypting $name (Argon2id key derivation takes a moment)"
tar -C "$staging" -cf - manifest.json db.dump documents.tar quarantine.tar \
    | if [[ $mode == compose ]]; then
        compose run --rm --no-deps -T -e PERFIN_BACKUP_PASSPHRASE api backup-encrypt
    else
        "${perfin_cli[@]}" backup-encrypt
    fi >"$partial"
[[ -s $partial ]] || die "encryption produced no output"
chmod 600 "$partial"
mv -f -- "$partial" "$target"
partial=""
(cd "$output_dir" && sha256sum "$name" >"$name.sha256")

log "wrote $target ($(file_size "$target") bytes; sha256 in $name.sha256)"
log "audit head seq $audit_seq hash $audit_hash; master key id $key_id (version $key_version)"
log "REMINDER: the master key is NOT in this backup. Keep a copy of key $key_id"
log "          separately (password manager, offline copy); without it this backup"
log "          cannot be restored. Keep the passphrase separately as well."
printf '%s\n' "$target"

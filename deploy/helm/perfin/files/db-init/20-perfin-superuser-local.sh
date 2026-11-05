#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Restrict the bootstrap superuser to the local Unix socket (SECURITY.md H3).
#
# The official postgres image appends `host all all all <method>` to
# pg_hba.conf, so POSTGRES_USER could authenticate over the network with
# POSTGRES_PASSWORD. PerFin never needs that: migrations run as perfin_owner
# and the app as perfin_app. This script, run once by the image entrypoint
# after 10-perfin-roles.sh, puts `reject` lines for the superuser at the top
# of pg_hba.conf (first match wins), so any TCP login as the superuser is
# refused whatever the password. Administration still works through the
# local socket inside the container (SCRAM, POSTGRES_PASSWORD):
#   docker compose exec db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U postgres perfin'
#
# The final server start (after initialisation) reads the edited file.
# Re-running it is safe: the block is only added once.
#
# Environment: POSTGRES_USER (default postgres), PGDATA (set by the image).

perfin_superuser_local_only() (
    set -euo pipefail

    : "${POSTGRES_USER:=postgres}"
    : "${PGDATA:?PGDATA must be set}"
    hba="$PGDATA/pg_hba.conf"
    marker="# perfin: superuser local socket only (SECURITY.md H3)"

    fail() {
        echo "perfin db-init: $*" >&2
        exit 1
    }

    [[ $POSTGRES_USER =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || fail "invalid POSTGRES_USER: $POSTGRES_USER"
    [[ -f $hba && ! -L $hba ]] || fail "$hba is not a regular file"

    if grep -qxF "$marker" "$hba"; then
        echo "perfin db-init: pg_hba.conf already restricts $POSTGRES_USER"
        return 0
    fi

    tmp="$(mktemp "$PGDATA/.pg_hba.XXXXXX")"
    {
        echo "$marker"
        printf 'host\tall\t\t%s\tall\treject\n' "$POSTGRES_USER"
        printf 'host\treplication\t%s\tall\treject\n' "$POSTGRES_USER"
        echo
        cat "$hba"
    } >"$tmp"
    chmod 0600 "$tmp"
    mv -f "$tmp" "$hba"
    echo "perfin db-init: $POSTGRES_USER may connect only through the local socket"
)

perfin_superuser_local_only

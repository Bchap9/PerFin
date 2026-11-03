#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# PostgreSQL first-start initialisation for PerFin (SECURITY.md H3).
#
# Mounted read-only at /docker-entrypoint-initdb.d/ in the official postgres
# image, which runs it once, against an empty data directory, as the
# bootstrap superuser (POSTGRES_USER) on the database POSTGRES_DB. It creates
#
#   perfin_owner  LOGIN, not superuser, no BYPASSRLS. Owns schema public and
#                 every object in it; runs migrations
#                 (PERFIN_MIGRATION_DATABASE_URL).
#   perfin_app    LOGIN, not superuser, NOBYPASSRLS, owns nothing. The API,
#                 worker and scheduler connect as this role
#                 (PERFIN_DATABASE_URL); the migrations grant it DML.
#
# and removes PUBLIC's rights on the database and on schema public, so no
# other role can connect, create objects or use temporary tables.
#
# Environment:
#   POSTGRES_OWNER_PASSWORD, POSTGRES_APP_PASSWORD   required, >= 16 chars,
#       distinct from each other and from POSTGRES_PASSWORD
#   PERFIN_DB_OWNER_ROLE, PERFIN_DB_APP_ROLE          optional role names
#   PGHOST / PGPORT / PGPASSWORD                      optional (tests); the
#       entrypoint uses the local socket
#
# Passwords are read by psql from the environment (\getenv), never placed on
# a command line, and stored as SCRAM-SHA-256 verifiers. Re-running it is
# safe: roles are created if missing and their attributes reset.
#
# Works whether the entrypoint executes or sources it: everything runs in a
# subshell so `set -u` and variables do not leak into the entrypoint.

perfin_db_init() (
    set -euo pipefail

    : "${POSTGRES_USER:=postgres}"
    : "${POSTGRES_DB:=$POSTGRES_USER}"
    : "${PERFIN_DB_OWNER_ROLE:=perfin_owner}"
    : "${PERFIN_DB_APP_ROLE:=perfin_app}"
    export POSTGRES_USER POSTGRES_DB PERFIN_DB_OWNER_ROLE PERFIN_DB_APP_ROLE

    fail() {
        echo "perfin db-init: $*" >&2
        exit 1
    }

    ident='^[a-z_][a-z0-9_]{0,62}$'
    for name in "$PERFIN_DB_OWNER_ROLE" "$PERFIN_DB_APP_ROLE"; do
        [[ $name =~ $ident ]] || fail "invalid role name: $name"
    done
    [[ $PERFIN_DB_OWNER_ROLE != "$PERFIN_DB_APP_ROLE" ]] || fail "owner and app roles must differ"
    [[ $PERFIN_DB_OWNER_ROLE != "$POSTGRES_USER" && $PERFIN_DB_APP_ROLE != "$POSTGRES_USER" ]] \
        || fail "PerFin roles must differ from the bootstrap superuser"

    for var in POSTGRES_OWNER_PASSWORD POSTGRES_APP_PASSWORD; do
        value="${!var:-}"
        [[ -n $value ]] || fail "$var must be set (generate: openssl rand -base64 32)"
        ((${#value} >= 16)) || fail "$var must be at least 16 characters"
        [[ $value != *CHANGE_ME* ]] || fail "$var still has the placeholder value"
    done
    [[ $POSTGRES_OWNER_PASSWORD != "$POSTGRES_APP_PASSWORD" ]] \
        || fail "POSTGRES_OWNER_PASSWORD and POSTGRES_APP_PASSWORD must differ"
    [[ $POSTGRES_OWNER_PASSWORD != "${POSTGRES_PASSWORD:-}" && $POSTGRES_APP_PASSWORD != "${POSTGRES_PASSWORD:-}" ]] \
        || fail "PerFin role passwords must differ from POSTGRES_PASSWORD"

    psql --no-psqlrc --set ON_ERROR_STOP=1 --quiet \
        --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv owner_role PERFIN_DB_OWNER_ROLE
\getenv app_role PERFIN_DB_APP_ROLE
\getenv owner_password POSTGRES_OWNER_PASSWORD
\getenv app_password POSTGRES_APP_PASSWORD
\getenv db_name POSTGRES_DB

SET password_encryption = 'scram-sha-256';
SET client_min_messages = warning;

SELECT format('CREATE ROLE %I', :'owner_role')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'owner_role') \gexec
SELECT format('CREATE ROLE %I', :'app_role')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'app_role') \gexec

SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION '
              'NOBYPASSRLS INHERIT PASSWORD %L', :'owner_role', :'owner_password') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION '
              'NOBYPASSRLS INHERIT PASSWORD %L', :'app_role', :'app_password') \gexec

-- F8 backstop: the application also sets these per connection.
SELECT format('ALTER ROLE %I SET statement_timeout = %L', :'app_role', '30s') \gexec
SELECT format('ALTER ROLE %I SET lock_timeout = %L', :'app_role', '10s') \gexec
SELECT format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', :'app_role', '5min') \gexec

-- Only the PerFin roles (and the superuser) may connect; no TEMP for PUBLIC.
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db_name') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I, %I', :'db_name', :'owner_role', :'app_role') \gexec

-- Schema public: owned by perfin_owner, nobody else may create in it.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SELECT format('ALTER SCHEMA public OWNER TO %I', :'owner_role') \gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_role') \gexec
SQL

    echo "perfin db-init: roles $PERFIN_DB_OWNER_ROLE (owner) and $PERFIN_DB_APP_ROLE (app) ready on $POSTGRES_DB"
)

perfin_db_init

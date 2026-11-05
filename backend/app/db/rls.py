# SPDX-License-Identifier: AGPL-3.0-or-later
"""Row-level security, grants and audit-log DDL, plus per-transaction context.

Everything the database needs beyond ``Base.metadata.create_all`` comes from
here, so the Alembic revisions and the test fixtures apply exactly the same
statements:

* :func:`schema_ddl` -- the full list (RLS, audit triggers, grants). Run it
  after the tables exist. :func:`downgrade_ddl` removes the functions again.
* Every ``HouseholdScoped`` table gets ``ENABLE`` **and** ``FORCE ROW LEVEL
  SECURITY`` and the ``household_isolation`` policy (``USING`` and ``WITH
  CHECK``) on ``perfin.household_id``.
* Tables with other visibility rules (``household``, ``job``, ``audit_log``,
  ``setting``) declare their policies in :data:`CUSTOM_RLS`; household tables
  that need an extra permissive policy (e.g. membership lookup by user) add it
  to :data:`EXTRA_POLICIES` (the hook for later phases).
* ``household`` itself is under forced RLS: a household row can be read,
  updated or deleted only in its own context. Every household-scoped table
  references it ``ON DELETE CASCADE`` and referential actions ignore RLS, so
  without this one wrong ``DELETE FROM household`` in another household's
  context would wipe that household's data. Maintenance code that needs every
  household id calls ``perfin_household_ids()`` (:data:`HOUSEHOLD_IDS_FUNCTION`).
* ``job`` is under forced RLS too: in a household context only that
  household's jobs are visible; the queue machinery (``app.db.queue``) works
  across households inside :func:`queue_scope`. A trigger makes ``id``,
  ``queue``, ``type``, ``payload``, ``household_id``, ``dedupe_key`` and
  ``created_at`` immutable, so no code path can retarget a job at another
  household or change what it runs.
* ``audit_log`` is append-only twice over: the application role is granted
  only ``SELECT, INSERT`` and a trigger raises on ``UPDATE``/``DELETE``/
  ``TRUNCATE`` for every role (SECURITY.md I4). A ``BEFORE INSERT`` trigger
  extends a SHA-256 hash chain under an advisory lock; ``app.core.audit.
  verify_chain`` recomputes it.

The context is set transaction-locally with ``set_config(..., true)`` and
bound parameters (:func:`set_context`); it never outlives the transaction.

Limitation (SECURITY.md F2): the context variables are ordinary custom GUCs
that the application role itself sets. RLS therefore contains *logic* bugs (a
forgotten ``WHERE household_id``, an IDOR in a handler, a path that writes in
the wrong context) but not arbitrary SQL execution as the application role:
injected SQL can call ``set_config`` too. SQL injection is prevented by never
building SQL from input (bound parameters only, ``quote_ident`` for the few
fixed identifiers), not by RLS.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Connection, MetaData, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.base import household_scoped_tables

POLICY_NAME = "household_isolation"

# Transaction-local context set by ``set_context``. ``current_setting(..., true)``
# returns NULL (never an error) when unset; ``nullif`` maps the reset value ''
# to NULL, so with no context every comparison is NULL and no row matches.
CTX_HOUSEHOLD = "nullif(current_setting('perfin.household_id', true), '')::uuid"
CTX_USER = "nullif(current_setting('perfin.user_id', true), '')::uuid"
CTX_INSTANCE_ADMIN = "coalesce(current_setting('perfin.instance_admin', true), '') = 'true'"
# Pre-authentication and background code acting for the instance (login
# failures, bootstrap, scheduler): may append instance-level audit rows.
CTX_SYSTEM_ACTOR = "coalesce(current_setting('perfin.system_actor', true), '') = 'true'"
# The job queue machinery (``app.db.queue``) inside :func:`queue_scope`.
CTX_JOB_QUEUE = "coalesce(current_setting('perfin.job_queue', true), '') = 'true'"

HOUSEHOLD_PREDICATE = f"household_id = {CTX_HOUSEHOLD}"

# Tables the application role may only read and append to.
APPEND_ONLY_TABLES: frozenset[str] = frozenset({"audit_log"})

Command = Literal["ALL", "SELECT", "INSERT", "UPDATE", "DELETE"]

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def quote_ident(name: str) -> str:
    """Validate and double-quote a lower-case SQL identifier.

    DDL cannot bind identifiers as parameters, so only names matching a strict
    pattern are accepted.
    """
    if not _IDENT.fullmatch(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return f'"{name}"'


@dataclass(frozen=True)
class Policy:
    """One permissive RLS policy. Predicates are fixed SQL written in this module."""

    name: str
    command: Command = "ALL"
    using: str | None = None
    with_check: str | None = None

    def ddl(self, table: str) -> list[str]:
        t = quote_ident(table)
        p = quote_ident(self.name)
        if self.using is None and self.with_check is None:
            raise ValueError(f"policy {self.name!r} has no predicate")
        if self.command == "INSERT" and self.using is not None:
            raise ValueError("INSERT policies take WITH CHECK only")
        if self.command in ("SELECT", "DELETE") and self.with_check is not None:
            raise ValueError(f"{self.command} policies take USING only")
        create = f"CREATE POLICY {p} ON {t} AS PERMISSIVE FOR {self.command}"
        if self.using is not None:
            create += f" USING ({self.using})"
        if self.with_check is not None:
            create += f" WITH CHECK ({self.with_check})"
        return [f"DROP POLICY IF EXISTS {p} ON {t}", create]


@dataclass(frozen=True)
class TableRls:
    """RLS for a table that is not (only) governed by ``household_isolation``.

    ``force=False`` leaves the table owner (the migration role) outside RLS;
    the application role is never the owner, so it is always subject to it.
    """

    policies: tuple[Policy, ...]
    force: bool = True


# Extra permissive policies on HouseholdScoped tables (OR-ed with
# household_isolation). Later phases register e.g. membership lookup by user.
EXTRA_POLICIES: dict[str, list[Policy]] = {}

_AUDIT_VISIBLE = (
    f"household_id = {CTX_HOUSEHOLD} OR (household_id IS NULL AND {CTX_INSTANCE_ADMIN})"
)
# Instance-level rows only from an explicit instance-admin or system-actor
# context, so household code cannot forge instance events (SECURITY.md I4).
_AUDIT_APPENDABLE = (
    f"household_id = {CTX_HOUSEHOLD}"
    f" OR (household_id IS NULL AND ({CTX_INSTANCE_ADMIN} OR {CTX_SYSTEM_ACTOR}))"
)

_HOUSEHOLD_OWN = f"id = {CTX_HOUSEHOLD}"
_HOUSEHOLD_VISIBLE = f"{_HOUSEHOLD_OWN} OR {CTX_INSTANCE_ADMIN}"

_JOB_VISIBLE = (
    f"household_id = {CTX_HOUSEHOLD} OR {CTX_JOB_QUEUE}"
    f" OR (household_id IS NULL AND {CTX_INSTANCE_ADMIN})"
)
_JOB_WRITABLE = f"household_id = {CTX_HOUSEHOLD} OR {CTX_JOB_QUEUE}"
_JOB_INSERTABLE = _JOB_WRITABLE

_SETTING_VISIBLE = (
    "scope = 'system'"
    f" OR (scope = 'household' AND scope_id = {CTX_HOUSEHOLD})"
    f" OR (scope = 'user' AND scope_id = {CTX_USER})"
)
_SETTING_WRITABLE = (
    f"(scope = 'system' AND {CTX_INSTANCE_ADMIN})"
    f" OR (scope = 'household' AND scope_id = {CTX_HOUSEHOLD})"
    f" OR (scope = 'user' AND scope_id = {CTX_USER})"
)

CUSTOM_RLS: dict[str, TableRls] = {
    # A household row is visible, updatable and deletable only in its own
    # context (an instance admin may also list households). A new household is
    # inserted either by an instance admin or in the context of its own new id
    # (``set_context(session, new_id)`` first). Referential actions (the
    # ``ON DELETE CASCADE`` of every household-scoped table) bypass RLS, which
    # is why the parent row itself must be protected (review finding SR-1).
    # Phase 1 adds membership-based visibility here.
    "household": TableRls(
        policies=(
            Policy("household_read", "SELECT", using=_HOUSEHOLD_VISIBLE),
            Policy(
                "household_insert",
                "INSERT",
                with_check=f"{_HOUSEHOLD_OWN} OR {CTX_INSTANCE_ADMIN}",
            ),
            Policy("household_update", "UPDATE", using=_HOUSEHOLD_OWN, with_check=_HOUSEHOLD_OWN),
            Policy("household_delete", "DELETE", using=_HOUSEHOLD_OWN),
        ),
    ),
    # A household context sees and may update or delete only its own jobs; an
    # instance admin may read instance-level jobs. The queue machinery (claim,
    # finish, reap, prune, operator requeue) runs inside ``queue_scope``.
    # Instance-level jobs (household_id NULL) may be enqueued from any context
    # but are not readable from a household context. ``perfin_job_immutable``
    # refuses changes to what a job is and whom it runs for.
    "job": TableRls(
        policies=(
            Policy("job_read", "SELECT", using=_JOB_VISIBLE),
            Policy("job_insert", "INSERT", with_check=_JOB_INSERTABLE),
            Policy("job_update", "UPDATE", using=_JOB_WRITABLE, with_check=_JOB_WRITABLE),
            Policy("job_delete", "DELETE", using=_JOB_WRITABLE),
        ),
    ),
    # Household rows are visible in that household's context; instance-level
    # rows (household_id NULL: logins, user and system changes) only to an
    # instance admin. A row for the current household may be appended in that
    # context; an instance-level row only in an instance-admin or system-actor
    # context (``set_context(..., system_actor=True)``, set by the auth layer
    # for pre-authentication events). No UPDATE/DELETE policy exists (and no grant, and a
    # trigger refuses them). Not forced: the owner role reads the whole chain
    # for verification and the chain trigger runs as the owner.
    "audit_log": TableRls(
        force=False,
        policies=(
            Policy("audit_read", "SELECT", using=_AUDIT_VISIBLE),
            Policy("audit_append", "INSERT", with_check=_AUDIT_APPENDABLE),
        ),
    ),
    # System settings are readable by everyone (needed before login) and
    # writable only in an instance-admin context; household and user settings
    # follow the household/user context.
    "setting": TableRls(
        policies=(
            Policy("setting_read", "SELECT", using=_SETTING_VISIBLE),
            Policy("setting_insert", "INSERT", with_check=_SETTING_WRITABLE),
            Policy(
                "setting_update", "UPDATE", using=_SETTING_WRITABLE, with_check=_SETTING_WRITABLE
            ),
            Policy("setting_delete", "DELETE", using=_SETTING_WRITABLE),
        ),
    ),
}


def rls_ddl(table: str) -> list[str]:
    """DDL enabling and forcing RLS on ``table`` with the household policy."""
    t = quote_ident(table)
    statements = [
        f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY",
        *Policy(POLICY_NAME, "ALL", using=HOUSEHOLD_PREDICATE, with_check=HOUSEHOLD_PREDICATE).ddl(
            table
        ),
    ]
    for policy in EXTRA_POLICIES.get(table, []):
        statements.extend(policy.ddl(table))
    return statements


def custom_rls_ddl(table: str, spec: TableRls) -> list[str]:
    """DDL for a table listed in :data:`CUSTOM_RLS`."""
    t = quote_ident(table)
    statements = [f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY"]
    statements.append(
        f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY"
        if spec.force
        else f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY"
    )
    for policy in spec.policies:
        statements.extend(policy.ddl(table))
    return statements


def all_rls_ddl(tables: Iterable[str] | None = None) -> list[str]:
    """RLS DDL for every household-scoped table and every :data:`CUSTOM_RLS` table.

    With ``tables`` only those are emitted (custom spec if present, otherwise
    the household policy).
    """
    if tables is None:
        names = sorted(set(household_scoped_tables()) | set(CUSTOM_RLS))
    else:
        names = sorted(tables)
    statements: list[str] = []
    for name in names:
        spec = CUSTOM_RLS.get(name)
        statements.extend(rls_ddl(name) if spec is None else custom_rls_ddl(name, spec))
    return statements


# ---------------------------------------------------------------------------
# audit_log: append-only triggers and hash chain (SECURITY.md I4)
# ---------------------------------------------------------------------------

# The SQL below is written out literally (no string formatting); unit tests
# check that these constants match it.
AUDIT_GENESIS_HASH = "0" * 64
# Arbitrary fixed key for pg_advisory_xact_lock serialising chain appends.
AUDIT_CHAIN_LOCK_KEY = 0x5045_5246_494E_4155  # "PERFINAU" = 5784119758258323797
AUDIT_HASH_FUNCTION = "perfin_audit_hash"

# Column order hashed for each row; verify_chain passes the same columns.
AUDIT_HASH_COLUMNS: tuple[str, ...] = (
    "prev_hash",
    "seq",
    "id",
    "household_id",
    "user_id",
    "action",
    "target_type",
    "target_id",
    "before",
    "after",
    "ip",
    "user_agent",
    "request_id",
    "created_at",
)

# sha256(prev_hash || canonical JSON array of the row). jsonb renders
# deterministically; the timestamp is hashed as exact epoch seconds so the
# session TimeZone cannot change the result. Individual arguments (not the
# table row type) keep the function independent of the table for DROP TABLE.
_AUDIT_HASH_FN = """
CREATE OR REPLACE FUNCTION perfin_audit_hash(
    prev_hash text, seq bigint, id uuid, household_id uuid, user_id uuid,
    action text, target_type text, target_id text, before jsonb, after jsonb,
    ip inet, user_agent text, request_id text, created_at timestamptz
) RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $fn$
    SELECT encode(sha256(convert_to(
        prev_hash || jsonb_build_array(
            seq, id, household_id, user_id, action, target_type, target_id,
            before, after, ip, user_agent, request_id,
            extract(epoch FROM created_at)
        )::text,
        'UTF8')), 'hex')
$fn$
"""

# Runs as the function owner (the migration role, which owns audit_log and is
# not subject to its RLS) so the previous row is visible whatever the caller's
# household context. The advisory lock serialises appends until commit; the
# UNIQUE(seq) constraint turns any fork (e.g. REPEATABLE READ snapshots) into
# an error instead of a branched chain. created_at is the server's clock.
_AUDIT_CHAIN_FN = """
CREATE OR REPLACE FUNCTION perfin_audit_log_chain() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
    last_seq bigint;
    last_hash text;
BEGIN
    PERFORM pg_advisory_xact_lock(5784119758258323797);
    SELECT a.seq, a.hash INTO last_seq, last_hash
        FROM public.audit_log AS a ORDER BY a.seq DESC LIMIT 1;
    NEW.seq := coalesce(last_seq, 0) + 1;
    NEW.prev_hash := coalesce(last_hash, repeat('0', 64));
    NEW.created_at := now();
    NEW.hash := public.perfin_audit_hash(
        NEW.prev_hash, NEW.seq, NEW.id, NEW.household_id, NEW.user_id,
        NEW.action, NEW.target_type, NEW.target_id, NEW.before, NEW.after,
        NEW.ip, NEW.user_agent, NEW.request_id, NEW.created_at);
    RETURN NEW;
END
$fn$
"""

_AUDIT_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION perfin_audit_log_immutable() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $fn$
BEGIN
    RAISE EXCEPTION USING
        MESSAGE = 'audit_log is append-only: ' || TG_OP || ' denied',
        ERRCODE = 'insufficient_privilege';
END
$fn$
"""

AUDIT_FUNCTIONS: tuple[str, ...] = (
    "perfin_audit_hash(text, bigint, uuid, uuid, uuid, text, text, text, jsonb, jsonb, "
    "inet, text, text, timestamptz)",
    "perfin_audit_log_chain()",
    "perfin_audit_log_immutable()",
)


def audit_ddl() -> list[str]:
    """Functions and triggers making ``audit_log`` append-only and hash-chained."""
    return [
        _AUDIT_HASH_FN.strip(),
        _AUDIT_CHAIN_FN.strip(),
        "REVOKE ALL ON FUNCTION perfin_audit_log_chain() FROM PUBLIC",
        _AUDIT_IMMUTABLE_FN.strip(),
        "REVOKE ALL ON FUNCTION perfin_audit_log_immutable() FROM PUBLIC",
        'CREATE OR REPLACE TRIGGER audit_log_chain BEFORE INSERT ON "audit_log" '
        "FOR EACH ROW EXECUTE FUNCTION perfin_audit_log_chain()",
        'CREATE OR REPLACE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON "audit_log" '
        "FOR EACH ROW EXECUTE FUNCTION perfin_audit_log_immutable()",
        'CREATE OR REPLACE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON "audit_log" '
        "FOR EACH STATEMENT EXECUTE FUNCTION perfin_audit_log_immutable()",
    ]


# ---------------------------------------------------------------------------
# job: immutable identity (review finding SR-4)
# ---------------------------------------------------------------------------

JOB_IMMUTABLE_COLUMNS: tuple[str, ...] = (
    "id",
    "queue",
    "type",
    "payload",
    "household_id",
    "dedupe_key",
    "created_at",
)

# The worker runs a job's handler in ``set_context(job.household_id)``, so
# whoever could rewrite household_id/type/payload of a queued job could run
# work in another household's context. Only the life-cycle columns (state,
# attempts, lease, run_after, priority, last_error, updated_at) may change.
# The application role does not own ``job`` and cannot disable the trigger.
_JOB_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION perfin_job_immutable() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $fn$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.queue IS DISTINCT FROM OLD.queue
       OR NEW.type IS DISTINCT FROM OLD.type
       OR NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.household_id IS DISTINCT FROM OLD.household_id
       OR NEW.dedupe_key IS DISTINCT FROM OLD.dedupe_key
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION USING
            MESSAGE = 'job identity columns are immutable',
            ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END
$fn$
"""


def job_ddl() -> list[str]:
    """Trigger making a job's identity columns immutable after insert."""
    return [
        _JOB_IMMUTABLE_FN.strip(),
        "REVOKE ALL ON FUNCTION perfin_job_immutable() FROM PUBLIC",
        'CREATE OR REPLACE TRIGGER job_immutable BEFORE UPDATE ON "job" '
        "FOR EACH ROW EXECUTE FUNCTION perfin_job_immutable()",
    ]


# ---------------------------------------------------------------------------
# household: id listing for maintenance (review finding SR-1)
# ---------------------------------------------------------------------------

HOUSEHOLD_IDS_FUNCTION = "perfin_household_ids"

# ``household`` is under forced RLS, so maintenance that works through every
# household (``app.core.events.prune_events``) cannot list them in a household
# context. This function returns ids only: it raises the instance-admin flag
# for its one query and puts the caller's value back before returning, so it
# never exposes names or any other row. SECURITY INVOKER: the caller's grants
# and RLS apply to everything else. (A function-level ``SET`` clause would be
# simpler, but Postgres lets only superusers attach placeholder GUCs to
# functions.)
_HOUSEHOLD_IDS_FN = """
CREATE OR REPLACE FUNCTION perfin_household_ids() RETURNS SETOF uuid
LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
    previous text := coalesce(current_setting('perfin.instance_admin', true), '');
BEGIN
    PERFORM set_config('perfin.instance_admin', 'true', true);
    RETURN QUERY SELECT h.id FROM public.household AS h ORDER BY h.id;
    PERFORM set_config('perfin.instance_admin', previous, true);
END
$fn$
"""


def household_ddl(app_role: str) -> list[str]:
    """``perfin_household_ids()``, executable by the application role only."""
    r = quote_ident(app_role)
    return [
        _HOUSEHOLD_IDS_FN.strip(),
        "REVOKE ALL ON FUNCTION perfin_household_ids() FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION perfin_household_ids() TO {r}",
    ]


#: Every function the platform DDL creates (dropped on downgrade).
PLATFORM_FUNCTIONS: tuple[str, ...] = (
    *AUDIT_FUNCTIONS,
    "perfin_job_immutable()",
    "perfin_household_ids()",
)


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------


def grant_ddl(metadata: MetaData, role: str) -> list[str]:
    """Least-privilege grants for the application role on every table.

    DML on ordinary tables, ``SELECT, INSERT`` only on append-only tables,
    sequence usage. Never ``TRUNCATE``, ``REFERENCES``, ``TRIGGER`` or DDL.
    """
    return table_grants_ddl([table.name for table in metadata.sorted_tables], role)


def table_grants_ddl(tables: Iterable[str], role: str) -> list[str]:
    """:func:`grant_ddl` for an explicit list of table names.

    Alembic revisions use this with the tables that revision creates, so an
    old revision never references tables added by a later phase.
    """
    r = quote_ident(role)
    statements = [f"GRANT USAGE ON SCHEMA public TO {r}"]
    for name in tables:
        t = quote_ident(name)
        privileges = (
            "SELECT, INSERT" if name in APPEND_ONLY_TABLES else "SELECT, INSERT, UPDATE, DELETE"
        )
        statements.append(f"GRANT {privileges} ON {t} TO {r}")
    statements.append(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {r}")
    return statements


def grants_ddl(app_role: str, metadata: MetaData | None = None) -> list[str]:
    """:func:`grant_ddl` for ``Base.metadata`` (call ``import_all_models()`` first)."""
    if metadata is None:
        from app.db.base import Base

        metadata = Base.metadata
    return grant_ddl(metadata, app_role)


def schema_ddl(metadata: MetaData, app_role: str) -> list[str]:
    """Everything to run after the tables exist: RLS, audit triggers, grants.

    Used by the Alembic revisions (``op.execute`` each statement, as the owner
    role) and by the test fixtures. Idempotent.
    """
    tables = {t.name for t in metadata.sorted_tables}
    statements = all_rls_ddl()
    if "audit_log" in tables:
        statements.extend(audit_ddl())
    if "job" in tables:
        statements.extend(job_ddl())
    statements.extend(grant_ddl(metadata, app_role))
    if "household" in tables:
        statements.extend(household_ddl(app_role))
    return statements


def downgrade_ddl() -> list[str]:
    """Drop the platform functions (their triggers go with the tables)."""
    return [f"DROP FUNCTION IF EXISTS {signature}" for signature in reversed(PLATFORM_FUNCTIONS)]


# ---------------------------------------------------------------------------
# Per-transaction context
# ---------------------------------------------------------------------------


def set_context(
    session: Session | Connection,
    household_id: uuid.UUID | None,
    user_id: uuid.UUID | None = None,
    instance_admin: bool = False,
    system_actor: bool = False,
) -> None:
    """Set the transaction-local RLS context.

    ``perfin.household_id``, ``perfin.user_id``, ``perfin.instance_admin`` and
    ``perfin.system_actor`` are set with ``set_config(..., true)``: they end
    with the transaction and are reset to '' (no access) when
    ``None``/``False``. Call it at the start of every transaction that touches
    household data. ``system_actor`` marks code acting for the instance before
    or without a user (pre-authentication audit events, bootstrap); it allows
    appending instance-level audit rows and nothing else.
    """
    if household_id is not None and not isinstance(household_id, uuid.UUID):
        raise TypeError("household_id must be a UUID")
    if user_id is not None and not isinstance(user_id, uuid.UUID):
        raise TypeError("user_id must be a UUID")
    session.execute(
        text(
            "SELECT set_config('perfin.household_id', :household_id, true), "
            "set_config('perfin.user_id', :user_id, true), "
            "set_config('perfin.instance_admin', :instance_admin, true), "
            "set_config('perfin.system_actor', :system_actor, true)"
        ),
        {
            "household_id": "" if household_id is None else str(household_id),
            "user_id": "" if user_id is None else str(user_id),
            "instance_admin": "true" if instance_admin is True else "",
            "system_actor": "true" if system_actor is True else "",
        },
    )


@dataclass(frozen=True)
class RlsContext:
    """The context currently set on a transaction (see :func:`get_context`)."""

    household_id: uuid.UUID | None
    user_id: uuid.UUID | None
    instance_admin: bool
    system_actor: bool = False


def get_context(session: Session | Connection) -> RlsContext:
    """Read back the transaction's RLS context."""
    row = session.execute(
        text(
            "SELECT current_setting('perfin.household_id', true) AS h, "
            "current_setting('perfin.user_id', true) AS u, "
            "current_setting('perfin.instance_admin', true) AS a, "
            "current_setting('perfin.system_actor', true) AS s"
        )
    ).one()
    return RlsContext(
        household_id=uuid.UUID(row.h) if row.h else None,
        user_id=uuid.UUID(row.u) if row.u else None,
        instance_admin=row.a == "true",
        system_actor=row.s == "true",
    )


def restore_context(session: Session | Connection, context: RlsContext) -> None:
    """Set a context previously read with :func:`get_context`."""
    set_context(
        session,
        context.household_id,
        context.user_id,
        instance_admin=context.instance_admin,
        system_actor=context.system_actor,
    )


_READ_QUEUE_FLAG = text("SELECT current_setting('perfin.job_queue', true)")
_SET_QUEUE_FLAG = text("SELECT set_config('perfin.job_queue', :value, true)")


@contextmanager
def queue_scope(session: Session | Connection) -> Iterator[None]:
    """Let the enclosed statements see and change jobs of every household.

    Sets the transaction-local ``perfin.job_queue`` flag that the ``job``
    policies accept, and restores its previous value on exit. Only the queue
    machinery in ``app.db.queue`` (claim, finish, reap, prune, operator
    requeue) uses it; code acting for a household never does, so a missing
    household filter on a job query returns nothing. The household context is
    left as it is.
    """
    previous = session.execute(_READ_QUEUE_FLAG).scalar()
    session.execute(_SET_QUEUE_FLAG, {"value": "true"})
    try:
        yield
    finally:
        # Resetting fails only if the transaction is already aborted (a
        # statement inside failed); the flag then ends with the transaction.
        with suppress(SQLAlchemyError):
            session.execute(_SET_QUEUE_FLAG, {"value": previous or ""})

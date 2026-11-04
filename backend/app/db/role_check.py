# SPDX-License-Identifier: AGPL-3.0-or-later
"""Refuse to serve as a privileged database role (SECURITY.md H3).

``PERFIN_DATABASE_URL`` must name the unprivileged application role
(``perfin_app`` from ``deploy/db-init``): no superuser, no ``BYPASSRLS`` and
owning no table, so row-level security always applies and a SQL injection
cannot ``ALTER TABLE ... DISABLE ROW LEVEL SECURITY`` or drop the audit log.
The static configuration check cannot see which role a URL logs in as (the
owner role name is configurable and the api/worker containers never receive
the migration URL), so ``perfin api`` and ``perfin worker`` ask the database
at startup and refuse to start when the role is privileged. Found by the
v1.0.0 verification run (docs/RELEASE_CHECKLIST.md): pointing
``PERFIN_DATABASE_URL`` at ``perfin_owner`` used to start normally.
"""

from __future__ import annotations

from sqlalchemy import Engine, text

_ROLE_SQL = text(
    "SELECT r.rolsuper, r.rolbypassrls, "
    "(SELECT count(*) FROM pg_catalog.pg_class c "
    " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    " WHERE c.relowner = r.oid AND n.nspname = 'public' "
    " AND c.relkind IN ('r', 'p')) AS owned_tables "
    "FROM pg_catalog.pg_roles r WHERE r.rolname = current_user"
)


def privileged_role_problems(engine: Engine) -> list[str]:
    """Why the role ``engine`` logs in as is too privileged (empty: fine).

    Raises ``sqlalchemy.exc.SQLAlchemyError`` when the database cannot be
    asked; callers fail closed.
    """
    with engine.connect() as conn:
        row = conn.execute(_ROLE_SQL).one()
    problems: list[str] = []
    if row.rolsuper:
        problems.append("is a superuser")
    if row.rolbypassrls:
        problems.append("has BYPASSRLS")
    if row.owned_tables:
        problems.append(f"owns {row.owned_tables} table(s) in schema public (the migration role?)")
    return problems

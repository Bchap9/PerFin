# SPDX-License-Identifier: AGPL-3.0-or-later
"""Liveness and readiness probes (ARCHITECTURE.md "Observability", SECURITY.md H7).

``GET /healthz`` and ``GET /readyz`` live at the root, outside ``/api/v1``,
need no authentication and return only ``{"status": ...}``: no version, no
database name, no error text. They are meant for the container healthcheck
(``perfin healthcheck``) and orchestration, and the reverse proxy should not
forward them from the internet.

* ``/healthz``: the process is serving requests. Never touches the database.
* ``/readyz``: the database answers as the application role **and** its
  Alembic revision equals the head(s) of ``migrations/``; otherwise ``503``.
  The reason is logged, not returned.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"

logger = logging.getLogger("perfin.health")

root_router = APIRouter(include_in_schema=False)


@lru_cache(maxsize=4)
def migration_heads(migrations_dir: Path = MIGRATIONS_DIR) -> frozenset[str]:
    """Head revision ids of the migration scripts shipped with this build."""
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    return frozenset(ScriptDirectory.from_config(cfg).get_heads())


def database_revisions(engine: Engine) -> frozenset[str]:
    """Revisions recorded in ``alembic_version`` (empty if never migrated)."""
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT to_regclass('alembic_version') IS NOT NULL")).scalar()
        if not exists:
            return frozenset()
        rows = conn.execute(text("SELECT version_num FROM alembic_version")).scalars()
        return frozenset(str(row) for row in rows)


def readiness_problem(engine: Engine, migrations_dir: Path = MIGRATIONS_DIR) -> str | None:
    """``None`` when ready, else a short reason for the log."""
    try:
        current = database_revisions(engine)
    except SQLAlchemyError as exc:
        return f"database unavailable ({type(exc).__name__})"
    try:
        heads = migration_heads(migrations_dir)
    except Exception as exc:  # noqa: BLE001 - alembic raises many error types
        return f"migration scripts unreadable ({type(exc).__name__})"
    if current != heads:
        return "database is not at the migration head"
    return None


@root_router.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@root_router.get("/readyz")
def readyz(request: Request) -> JSONResponse:
    reason = readiness_problem(request.app.state.engine)
    if reason is not None:
        logger.warning("not ready: %s", reason)
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alembic environment.

Runs as the owner role (``PERFIN_MIGRATION_DATABASE_URL``). Revisions are
written once per roadmap phase with autogenerate plus explicit RLS and grant
statements from ``app.db.rls``. Tests may pass an open connection through
``config.attributes["connection"]``.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool

from app.core.settings import get_settings
from app.db.base import Base
from app.db.registry import import_all_models
from app.db.session import make_engine

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

import_all_models()
target_metadata = Base.metadata


def _url() -> str:
    explicit = config.get_main_option("sqlalchemy.url")
    if explicit:
        return explicit
    return get_settings().effective_migration_database_url.get_secret_value()


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        transaction_per_migration=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied = config.attributes.get("connection")
    if isinstance(supplied, Connection):
        _configure(supplied)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            _configure(connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

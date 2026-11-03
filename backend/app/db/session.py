# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine and session factories plus the FastAPI ``get_db`` dependency.

Every application connection carries a server-side ``statement_timeout`` and
``lock_timeout`` (SECURITY.md F8), passed as libpq startup options so they
hold from the first statement and survive pool resets. The pool pings
connections before use and never logs bound parameters.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from fastapi import Request
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from app.core.settings import Settings


def connection_options(
    *, statement_timeout_ms: int | None = None, lock_timeout_ms: int | None = None
) -> str:
    """libpq ``options`` string setting the timeouts (0/None leaves the server default)."""
    parts: list[str] = []
    for name, value in (
        ("statement_timeout", statement_timeout_ms),
        ("lock_timeout", lock_timeout_ms),
    ):
        if value is None or value == 0:
            continue
        number = int(value)
        if number < 0:
            raise ValueError(f"{name} must be >= 0")
        parts.append(f"-c {name}={number}")
    return " ".join(parts)


def make_engine(
    url: str | SecretStr,
    *,
    statement_timeout_ms: int | None = None,
    lock_timeout_ms: int | None = None,
    **kwargs: Any,
) -> Engine:
    """Create a sync SQLAlchemy engine on psycopg 3.

    ``statement_timeout_ms`` / ``lock_timeout_ms`` set the server-side
    timeouts for every connection (0 or None leaves the server default).
    """
    raw = url.get_secret_value() if isinstance(url, SecretStr) else url
    connect_args: dict[str, Any] = dict(kwargs.pop("connect_args", {}))
    options = connection_options(
        statement_timeout_ms=statement_timeout_ms, lock_timeout_ms=lock_timeout_ms
    )
    if options:
        existing = str(connect_args.get("options", "")).strip()
        connect_args["options"] = f"{existing} {options}".strip()
    return create_engine(
        raw,
        pool_pre_ping=True,
        connect_args=connect_args,
        hide_parameters=True,
        **kwargs,
    )


def make_app_engine(settings: Settings, **kwargs: Any) -> Engine:
    """Engine for the application role with the configured timeouts."""
    return make_engine(
        settings.database_url,
        statement_timeout_ms=settings.db_statement_timeout_ms,
        lock_timeout_ms=settings.db_lock_timeout_ms,
        **kwargs,
    )


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    """Session factory bound to ``engine``; objects stay usable after commit."""
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def get_db(request: Request) -> Iterator[Session]:
    """Yield a Session in one transaction: commit on success, roll back on error.

    The factory is ``app.state.sessionmaker`` set by ``create_app``.
    """
    factory: sessionmaker[Session] = request.app.state.sessionmaker
    with factory() as session, session.begin():
        yield session

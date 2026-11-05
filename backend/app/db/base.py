# SPDX-License-Identifier: AGPL-3.0-or-later
"""Declarative base and the mixins every model composes.

* ``UUIDPk``: UUIDv7 primary key generated in the application.
* ``Timestamps``: ``created_at`` / ``updated_at`` as ``timestamptz`` in UTC.
* ``HouseholdScoped``: ``household_id`` foreign key plus registration for
  row-level security. Every household-scoped table MUST use it; ``app.db.rls``
  turns the registry into ``ENABLE``/``FORCE ROW LEVEL SECURITY`` and a policy.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime, ForeignKey, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from app.core.clock import utcnow
from app.core.ids import uuid7

# Deterministic constraint names so Alembic autogenerate produces stable diffs.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Table names of every model that mixes in HouseholdScoped, filled at import.
HOUSEHOLD_SCOPED_TABLES: set[str] = set()


class Base(DeclarativeBase):
    """Declarative base for all PerFin models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPk:
    """UUIDv7 primary key."""

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid7)


class Timestamps:
    """``created_at`` and ``updated_at`` in UTC (``timestamptz``)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


class HouseholdScoped:
    """Owning ``household_id`` column and RLS registration.

    Mixed into concrete mapped classes only; abstract classes
    (``__abstract__ = True``) are not registered.
    """

    __household_scoped__: ClassVar[bool] = True

    @declared_attr
    def household_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805 - SQLAlchemy declared_attr
        return mapped_column(
            Uuid(as_uuid=True),
            ForeignKey("household.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("__abstract__", False):
            return
        tablename = cls.__dict__.get("__tablename__")
        if isinstance(tablename, str):
            HOUSEHOLD_SCOPED_TABLES.add(tablename)


def household_scoped_tables() -> list[str]:
    """Sorted names of all registered household-scoped tables."""
    return sorted(HOUSEHOLD_SCOPED_TABLES)

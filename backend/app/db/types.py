# SPDX-License-Identifier: AGPL-3.0-or-later
"""Column types and constraints shared by the domain models.

* :func:`pg_enum`: a native Postgres enum from a ``StrEnum`` (stores values).
* :data:`AMOUNT`/:data:`RATE` and :func:`currency_check`: money columns are
  ``NUMERIC(20, 6)`` plus a ``CHAR(3)`` currency (DATA_MODEL.md).
* :func:`household_fk` and :func:`household_ref_fk`: **household-consistent
  references** (SECURITY.md F3). A plain foreign key is checked by Postgres
  without row-level security, so a row of household A could otherwise point
  at an account, payee or category of household B. Every reference between
  household-scoped tables therefore includes ``household_id``:
  ``(household_id, x_id) -> x(household_id, id)``. The referenced table
  carries ``UniqueConstraint("household_id", "id")`` (:func:`household_key`).

  - Mandatory parents (``transaction.account_id``,
    ``transaction_split.transaction_id``...) use :func:`household_fk` with
    ``ON DELETE CASCADE`` (or ``NO ACTION`` to protect the parent).
  - Optional lookups that should be cleared when the target goes
    (``transaction.payee_id``, ``transaction.category_id``...) keep a plain
    ``ForeignKey(..., ondelete="SET NULL")`` on the column (a composite
    ``SET NULL`` would also null ``household_id``) **plus**
    :func:`household_ref_fk`, a composite ``NO ACTION`` key that is
    ``DEFERRABLE INITIALLY DEFERRED``: it is checked at commit, after the
    ``SET NULL`` has run, and still refuses a cross-household id.
"""

from __future__ import annotations

import enum
from typing import Final

from sqlalchemy import CHAR, CheckConstraint, Enum, ForeignKeyConstraint, Numeric, UniqueConstraint

#: ``NUMERIC(20, 6)`` for every monetary amount and balance.
AMOUNT: Final = Numeric(20, 6, asdecimal=True)
#: ``NUMERIC(24, 12)`` for exchange rates (``JPY -> USD`` needs the digits).
RATE: Final = Numeric(24, 12, asdecimal=True)
#: ``NUMERIC(5, 4)`` for confidence scores in ``[0, 1]``.
CONFIDENCE: Final = Numeric(5, 4, asdecimal=True)
#: ``CHAR(3)`` ISO 4217 code.
CURRENCY: Final = CHAR(3)


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_cls]


def pg_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    """Native Postgres enum type ``name`` storing the members' values."""
    return Enum(enum_cls, name=name, values_callable=_enum_values, validate_strings=True)


def currency_check(column: str = "currency") -> CheckConstraint:
    """``column`` is three upper-case letters (validity is checked in the service)."""
    return CheckConstraint(f"{column} ~ '^[A-Z]{{3}}$'", name=f"{column}_format")


def household_key(table: str) -> UniqueConstraint:
    """``UNIQUE (household_id, id)``: the target of household-consistent references."""
    return UniqueConstraint("household_id", "id", name=f"uq_{table}_household_id_id")


def household_fk(
    table: str,
    column: str,
    target: str,
    *,
    ondelete: str | None = "CASCADE",
    extra: tuple[tuple[str, str], ...] = (),
) -> ForeignKeyConstraint:
    """``(household_id, column, *extra) -> target(household_id, id, *extra)``.

    ``extra`` pairs ``(local column, target column)`` extend the key, e.g.
    ``(("currency", "currency"),)`` so a transaction's currency always equals
    its account's.
    """
    local = ["household_id", column, *(pair[0] for pair in extra)]
    remote = [f"{target}.household_id", f"{target}.id", *(f"{target}.{pair[1]}" for pair in extra)]
    return ForeignKeyConstraint(
        local, remote, ondelete=ondelete, name=f"fk_{table}_{column}_hh_{target}"[:63]
    )


def household_ref_fk(table: str, column: str, target: str) -> ForeignKeyConstraint:
    """Deferred composite check for an optional ``SET NULL`` reference (see module doc)."""
    return ForeignKeyConstraint(
        ["household_id", column],
        [f"{target}.household_id", f"{target}.id"],
        deferrable=True,
        initially="DEFERRED",
        name=f"fk_{table}_{column}_hh_{target}"[:63],
    )

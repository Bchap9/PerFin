# SPDX-License-Identifier: AGPL-3.0-or-later
"""Column values to and from the JSON of an export (docs/PORTABILITY.md "Values").

Encoding follows the column type: UUIDs, decimals, dates and timestamps as
strings (decimals in plain notation, timestamps ISO 8601 in UTC), enums as
their value, JSON columns as JSON, arrays as lists.

Decoding is the strict inverse and treats the archive as untrusted input:
the type must match exactly (no booleans for integers, no floats for
decimals), strings must fit the column, decimals the column's precision and
scale, timestamps must carry an offset, enums must be known members, and a
``NOT NULL`` column cannot be null. Anything else is
:class:`ArchiveInvalidError` naming the entity and column (never the value).
The database's own check and foreign-key constraints run on top.
"""

from __future__ import annotations

import enum
import json
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import (
    ARRAY,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Integer,
    Numeric,
    String,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB

_INT_LIMITS: Final = {"SmallInteger": 2**15, "Integer": 2**31, "BigInteger": 2**63}
#: Largest JSON column value accepted from an archive (serialised characters).
JSON_VALUE_MAX: Final = 4 * 1024 * 1024


class ArchiveInvalidError(Exception):
    """The archive cannot be imported; ``slug`` is a short machine-readable reason."""

    def __init__(self, slug: str, detail: str) -> None:
        super().__init__(detail)
        self.slug = slug
        self.detail = detail


def invalid(entity: str, column: str, why: str) -> ArchiveInvalidError:
    return ArchiveInvalidError("archive-invalid-value", f"{entity}.{column}: {why}")


def encode(column: Column[Any], value: Any) -> Any:
    """The JSON form of ``value`` read from ``column``."""
    if value is None:
        return None
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [encode(column, item) for item in value]
    return value


def _decimal(entity: str, column: Column[Any], raw: Any) -> Decimal:
    if not isinstance(raw, str) or len(raw) > 64:
        raise invalid(entity, column.name, "expected a decimal string")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise invalid(entity, column.name, "expected a decimal string") from None
    if not value.is_finite():
        raise invalid(entity, column.name, "not a finite number")
    precision = getattr(column.type, "precision", None)
    scale = getattr(column.type, "scale", None)
    if precision is not None and scale is not None:
        _, digits, exponent = value.normalize().as_tuple()
        places = max(0, -int(exponent))
        integer_digits = max(0, len(digits) + int(exponent))
        if places > scale or integer_digits > precision - scale:
            raise invalid(entity, column.name, "does not fit the column")
    return value


def _integer(entity: str, column: Column[Any], raw: Any) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise invalid(entity, column.name, "expected an integer")
    limit = _INT_LIMITS.get(type(column.type).__name__, 2**31)
    if not -limit <= raw < limit:
        raise invalid(entity, column.name, "integer out of range")
    return raw


def _string(entity: str, column: Column[Any], raw: Any) -> str:
    if not isinstance(raw, str):
        raise invalid(entity, column.name, "expected a string")
    length = getattr(column.type, "length", None)
    if length is not None and len(raw) > length:
        raise invalid(entity, column.name, "string too long")
    if "\x00" in raw:
        raise invalid(entity, column.name, "NUL character")
    return raw


def _json_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")))


def decode(entity: str, column: Column[Any], raw: Any) -> Any:
    """The Python value for ``column`` from its JSON form (module doc)."""
    if raw is None:
        if not column.nullable:
            raise invalid(entity, column.name, "must not be null")
        return None
    kind = column.type
    if isinstance(kind, Uuid):
        if not isinstance(raw, str):
            raise invalid(entity, column.name, "expected a UUID string")
        try:
            return uuid.UUID(raw)
        except ValueError:
            raise invalid(entity, column.name, "expected a UUID string") from None
    if isinstance(kind, Enum):
        enum_class = kind.enum_class
        if not isinstance(raw, str) or enum_class is None:
            raise invalid(entity, column.name, "expected an enum value")
        try:
            return enum_class(raw)
        except ValueError:
            raise invalid(entity, column.name, "unknown enum value") from None
    if isinstance(kind, Boolean):
        if not isinstance(raw, bool):
            raise invalid(entity, column.name, "expected a boolean")
        return raw
    if isinstance(kind, Numeric):
        return _decimal(entity, column, raw)
    if isinstance(kind, Integer):
        return _integer(entity, column, raw)
    if isinstance(kind, DateTime):
        if not isinstance(raw, str) or len(raw) > 64:
            raise invalid(entity, column.name, "expected an ISO 8601 timestamp")
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            raise invalid(entity, column.name, "expected an ISO 8601 timestamp") from None
        if value.tzinfo is None:
            raise invalid(entity, column.name, "timestamp without a UTC offset")
        return value
    if isinstance(kind, Date):
        if not isinstance(raw, str) or len(raw) != 10:
            raise invalid(entity, column.name, "expected an ISO 8601 date")
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise invalid(entity, column.name, "expected an ISO 8601 date") from None
    if isinstance(kind, ARRAY):
        if not isinstance(raw, list) or len(raw) > 1000:
            raise invalid(entity, column.name, "expected a list")
        item = Column(column.name, kind.item_type, nullable=False)
        return [decode(entity, item, value) for value in raw]
    if isinstance(kind, JSONB):
        if not isinstance(raw, dict | list) or _json_size(raw) > JSON_VALUE_MAX:
            raise invalid(entity, column.name, "expected a JSON object or list")
        return raw
    if isinstance(kind, String):
        return _string(entity, column, raw)
    raise invalid(entity, column.name, "column type cannot be imported")

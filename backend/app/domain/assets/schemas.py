# SPDX-License-Identifier: AGPL-3.0-or-later
"""Input and output schemas for manual assets and their valuations (API.md ``/assets``).

Inputs forbid unknown fields (SECURITY.md F4). Money is ``{"amount":
"12.34", "currency": "USD"}``. ``description`` is identity-sensitive free
text (it may hold a property's address): returned only to callers who may
see the asset's account, never audited, logged or put in events, and kept
out of ``ai:read`` views (AI_ACCESS.md).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Final, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.core.dates import BusinessDate
from app.core.money import Money, MoneyIn
from app.domain.accounts.models import AssetKind
from app.domain.assets.models import DESCRIPTION_MAX, ValuationMethod, ValuationSource

MAX_PAGE: Final = 500
DEFAULT_PAGE: Final = 100
#: Most valuations ``GET /assets/{id}/valuations`` returns at once.
MAX_VALUATIONS: Final = 2000


def _clean(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or any(not (c.isprintable() or c in "\n\t") for c in cleaned):
        raise ValueError("must be non-empty printable text")
    return cleaned


Description = Annotated[
    str, Field(min_length=1, max_length=DESCRIPTION_MAX), AfterValidator(_clean)
]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True)


class AssetCreate(_Input):
    """``POST /assets``: details of an existing ``manual_asset`` account.

    ``value`` records a first manual valuation on ``valued_on`` (default today).
    """

    account_id: uuid.UUID
    description: Description | None = None
    valuation_method: ValuationMethod = ValuationMethod.MANUAL
    value: MoneyIn | None = None
    valued_on: BusinessDate | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.valued_on is not None and self.value is None:
            raise ValueError("valued_on needs value")
        return self


class AssetUpdate(_Input):
    """``PATCH /assets/{id}``: ``kind`` is the account's ``asset_kind``.

    ``description`` may be null (cleared).
    """

    kind: AssetKind | None = None
    description: Description | None = None
    valuation_method: ValuationMethod | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("give at least one field to change")
        for name in ("kind", "valuation_method"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class ValuationOut(_Output):
    id: uuid.UUID
    account_id: uuid.UUID
    valued_on: date
    value: Money
    source: ValuationSource
    created_at: datetime


class AssetOut(_Output):
    id: uuid.UUID
    account_id: uuid.UUID
    name: str
    kind: AssetKind
    description: str | None
    valuation_method: ValuationMethod
    #: The latest valuation on or before today (``manual`` beats ``provider``).
    current_value: ValuationOut | None
    created_at: datetime
    updated_at: datetime


class AssetPage(_Output):
    items: list[AssetOut]
    next_cursor: uuid.UUID | None


class ValuationCreate(_Input):
    """``POST /assets/{id}/valuations``: a manual value on a day.

    It replaces the manual valuation of that day, if any.
    """

    valued_on: BusinessDate
    value: MoneyIn


class ValuationList(_Output):
    """Newest first, at most ``limit``."""

    items: list[ValuationOut]

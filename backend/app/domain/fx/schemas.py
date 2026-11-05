# SPDX-License-Identifier: AGPL-3.0-or-later
"""Input and output schemas for ``/fx-rates`` (API.md "FX rates").

Rates are decimal **strings** (``"1.0812"``; at most 12 digits before and 12
after the point, ``NUMERIC(24, 12)``), never JSON numbers. Inputs forbid
unknown fields (SECURITY.md F4).
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Final, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, model_validator

from app.core.dates import BusinessDate
from app.core.money import CurrencyCode

MAX_PAGE: Final = 500
DEFAULT_PAGE: Final = 100
_RATE_RE: Final = re.compile(r"^[0-9]{1,12}(?:\.[0-9]{1,12})?$")


def _rate(value: object) -> Decimal:
    if not isinstance(value, str) or not _RATE_RE.fullmatch(value.strip()):
        raise ValueError(
            "must be a decimal string with at most 12 digits before and 12 after the point, "
            'such as "1.0812"'
        )
    rate = Decimal(value.strip())
    if rate <= 0:
        raise ValueError("must be above zero")
    return rate


RateIn = Annotated[Decimal, BeforeValidator(_rate)]


class FxRateSet(BaseModel):
    """``POST /fx-rates``: ``1 base = rate quote`` on ``rate_date`` (replaces that day's rate)."""

    model_config = ConfigDict(extra="forbid")

    base: CurrencyCode
    quote: CurrencyCode
    rate_date: BusinessDate
    rate: RateIn

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if self.base == self.quote:
            raise ValueError("base and quote must differ")
        return self


class FxRateOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    base: str
    quote: str
    rate_date: date
    rate: str
    source: str
    created_at: datetime
    updated_at: datetime


class FxRatePage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[FxRateOut]
    next_cursor: uuid.UUID | None

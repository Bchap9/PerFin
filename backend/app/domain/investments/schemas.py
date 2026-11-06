# SPDX-License-Identifier: AGPL-3.0-or-later
"""Input and output schemas for securities, prices and investments (API.md).

Inputs forbid unknown fields (SECURITY.md F4). Money is ``{"amount":
"12.34", "currency": "USD"}``; quantities, unit prices and ratios are
decimal **strings** (at most 10 decimals, ``NUMERIC(28, 10)``), never JSON
numbers. Rates in output are fractions as strings rounded half-even to 10
decimals (``app.domain.investments.returns``).
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.core.dates import BusinessDate
from app.core.money import CurrencyCode, Money, MoneyIn
from app.domain.accounts.schemas import Name, clean_text
from app.domain.investments.models import (
    EXCHANGE_MAX,
    MEMO_MAX,
    SECTOR_MAX,
    AssetClass,
    CostMethod,
    InvestmentTxnType,
)

MAX_PAGE: Final = 500
DEFAULT_PAGE: Final = 100
#: Integer digits that fit ``NUMERIC(28, 10)``.
UNITS_INTEGER_DIGITS: Final = 18
UNITS_PLACES: Final = 10
_DECIMAL_RE: Final = re.compile(r"^(?:[0-9]{1,18})(?:\.[0-9]{1,10})?$")
_SYMBOL_RE: Final = re.compile(r"^[A-Za-z0-9.^/:_-]{1,32}$")
_ISIN_RE: Final = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_CUSIP_RE: Final = re.compile(r"^[0-9A-Z*@#]{9}$")


def _units(value: object) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL_RE.fullmatch(value.strip()):
        raise ValueError(
            "must be a non-negative decimal string with at most 18 digits before and 10 after "
            'the point, such as "12.5"'
        )
    return Decimal(value.strip())


def _positive(value: Decimal) -> Decimal:
    if value <= 0:
        raise ValueError("must be above zero")
    return value


#: Units, a unit price or a ratio part: a decimal string, >= 0.
UnitsIn = Annotated[Decimal, BeforeValidator(_units)]
#: As :data:`UnitsIn`, > 0.
PositiveUnitsIn = Annotated[Decimal, BeforeValidator(_units), AfterValidator(_positive)]


def _pattern(regex: re.Pattern[str], what: str) -> AfterValidator:
    def check(value: str) -> str:
        if not regex.fullmatch(value):
            raise ValueError(f"is not a valid {what}")
        return value

    return AfterValidator(check)


def _upper(value: str) -> str:
    return value.strip().upper()


Symbol = Annotated[
    str,
    Field(min_length=1, max_length=32),
    AfterValidator(str.strip),
    _pattern(_SYMBOL_RE, "symbol"),
]
Isin = Annotated[str, Field(max_length=12), AfterValidator(_upper), _pattern(_ISIN_RE, "ISIN")]
Cusip = Annotated[str, Field(max_length=9), AfterValidator(_upper), _pattern(_CUSIP_RE, "CUSIP")]
Sector = Annotated[str, Field(min_length=1, max_length=SECTOR_MAX), AfterValidator(clean_text)]
Exchange = Annotated[str, Field(min_length=1, max_length=EXCHANGE_MAX), AfterValidator(clean_text)]
Memo = Annotated[str, Field(max_length=MEMO_MAX)]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Securities and prices
# ---------------------------------------------------------------------------


class SecurityCreate(_Input):
    symbol: Symbol
    name: Name
    asset_class: AssetClass
    currency: CurrencyCode
    isin: Isin | None = None
    cusip: Cusip | None = None
    sector: Sector | None = None
    exchange: Exchange | None = None


class SecurityUpdate(_Input):
    """``PATCH /securities/{id}``: absent fields stay; ``currency`` only while unused."""

    symbol: Symbol | None = None
    name: Name | None = None
    asset_class: AssetClass | None = None
    currency: CurrencyCode | None = None
    isin: Isin | None = None
    cusip: Cusip | None = None
    sector: Sector | None = None
    exchange: Exchange | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        for name in ("symbol", "name", "asset_class", "currency"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class SecurityOut(_Output):
    id: uuid.UUID
    #: None for a security a statement printed by its name alone.
    symbol: str | None
    name: str
    asset_class: AssetClass
    currency: str
    isin: str | None
    cusip: str | None
    sector: str | None
    exchange: str | None
    created_at: datetime
    updated_at: datetime


class SecurityPage(_Output):
    items: list[SecurityOut]
    next_cursor: uuid.UUID | None


class PriceSet(_Input):
    """``POST /securities/{id}/prices``: the close on a day (replaces that day's price)."""

    price_date: BusinessDate
    close: PositiveUnitsIn


class PriceOut(_Output):
    id: uuid.UUID
    security_id: uuid.UUID
    price_date: date
    close: str
    currency: str
    source: str


class PricePage(_Output):
    items: list[PriceOut]
    next_cursor: uuid.UUID | None


# ---------------------------------------------------------------------------
# Investment transactions
# ---------------------------------------------------------------------------


class LotPickIn(_Input):
    lot_id: uuid.UUID
    quantity: PositiveUnitsIn


class RatioIn(_Input):
    """A split ratio, ``new`` units for every ``old`` one (3-for-1: new 3, old 1)."""

    new: PositiveUnitsIn
    old: PositiveUnitsIn

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.new == self.old:
            raise ValueError("a ratio of 1 changes nothing")
        return self


class InvestmentTxnFields(_Input):
    """Fields shared by create and update (rules per type: ``service`` module doc)."""

    security_id: uuid.UUID | None = None
    type: InvestmentTxnType | None = None
    trade_date: BusinessDate | None = None
    settle_date: BusinessDate | None = None
    quantity: PositiveUnitsIn | None = None
    price: UnitsIn | None = None
    amount: MoneyIn | None = None
    fees: MoneyIn | None = None
    ratio: RatioIn | None = None
    #: Specific lots for a sell/transfer_out (``fifo`` accounts only); ``null`` or
    #: absent lets the cost method choose.
    lots: Annotated[list[LotPickIn], Field(max_length=1000)] | None = None
    memo: Memo | None = None

    @model_validator(mode="after")
    def _unique_lots(self) -> Self:
        if self.lots is not None and len({p.lot_id for p in self.lots}) != len(self.lots):
            raise ValueError("a lot is named twice")
        return self


class InvestmentTxnCreate(InvestmentTxnFields):
    account_id: uuid.UUID
    type: InvestmentTxnType
    trade_date: BusinessDate
    amount: MoneyIn


class InvestmentTxnUpdate(InvestmentTxnFields):
    """``PATCH``: absent fields stay; the account cannot change (delete and re-enter)."""

    @model_validator(mode="after")
    def _check(self) -> Self:
        for name in ("type", "trade_date", "amount", "fees"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class LotPickOut(_Output):
    lot_id: uuid.UUID
    quantity: str


class InvestmentTxnOut(_Output):
    id: uuid.UUID
    account_id: uuid.UUID
    security_id: uuid.UUID | None
    type: InvestmentTxnType
    trade_date: date
    settle_date: date | None
    quantity: str | None
    price: str | None
    ratio: dict[str, str] | None
    amount: Money
    fees: Money
    lots: list[LotPickOut]
    lots_specified: bool
    memo: str | None
    source_document_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class InvestmentTxnPage(_Output):
    items: list[InvestmentTxnOut]
    next_cursor: uuid.UUID | None


class CostMethodSet(_Input):
    method: CostMethod


class CostMethodOut(_Output):
    account_id: uuid.UUID
    method: CostMethod


# ---------------------------------------------------------------------------
# Holdings and performance
# ---------------------------------------------------------------------------


class SecurityBrief(_Output):
    id: uuid.UUID
    #: None for a security a statement printed by its name alone.
    symbol: str | None
    name: str
    asset_class: AssetClass
    sector: str | None
    currency: str


class PriceBrief(_Output):
    close: str
    currency: str
    price_date: date
    #: ``price`` (a stored close) or ``trade`` (the last trade price).
    basis: Literal["price", "trade"]


class GainsOut(_Output):
    short_term: Money
    long_term: Money
    total: Money


class LotOut(_Output):
    id: uuid.UUID
    open_date: date
    quantity: str
    remaining_quantity: str
    cost_per_unit: str
    cost_basis: Money
    market_value: Money | None
    unrealized_gain: Money | None
    term: Literal["short", "long"]


class HoldingOut(_Output):
    account_id: uuid.UUID
    security: SecurityBrief
    quantity: str
    cost_basis: Money
    price: PriceBrief | None
    market_value: Money | None
    market_value_base: Money | None
    unrealized: GainsOut | None
    weight: str | None
    lots: list[LotOut]


class AllocationOut(_Output):
    key: str
    value: Money
    weight: str


class HoldingAccountOut(_Output):
    account_id: uuid.UUID
    cost_method: CostMethod
    market_value_base: Money | None


class MissingRate(_Output):
    currency: str
    on: date


class HoldingsOut(_Output):
    """``GET /investments/holdings``: visible accounts only; totals in the base currency."""

    as_of: date
    base_currency: str
    holdings: list[HoldingOut]
    accounts: list[HoldingAccountOut]
    market_value: Money
    cost_basis: Money
    unrealized: GainsOut
    allocation: dict[str, list[AllocationOut]]
    unpriced_security_ids: list[uuid.UUID]
    missing_rates: list[MissingRate]


Range = Literal["1m", "3m", "6m", "ytd", "1y", "3y", "5y", "max"]


class SeriesPoint(_Output):
    date: date
    value: Money
    twr: str


class PerformanceOut(_Output):
    """``GET /investments/performance`` (rules: ``app.domain.investments.performance``)."""

    range: Range
    start: date
    end: date
    days: int
    base_currency: str
    account_id: uuid.UUID | None
    start_value: Money
    end_value: Money
    inflows: Money
    outflows: Money
    income: Money
    fees: Money
    twr: str
    twr_annualized: str | None
    mwr: str | None
    mwr_annualized: str | None
    realized: GainsOut
    unrealized: GainsOut
    series: list[SeriesPoint]
    unpriced_security_ids: list[uuid.UUID]
    missing_rates: list[MissingRate]

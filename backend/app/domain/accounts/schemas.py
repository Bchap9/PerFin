# SPDX-License-Identifier: AGPL-3.0-or-later
"""Input and output schemas for accounts, balances and account grants (API.md).

Inputs forbid unknown fields (SECURITY.md F4, mass assignment): a request
that adds ``household_id``, ``id``, ``closed_on``, ``created_at`` or any other
field an endpoint does not accept is a ``422``; services copy validated
fields one by one. Money is ``{"amount": "12.34", "currency": "USD"}`` with
the amount as a decimal string (``app.core.money``).

``number_last4`` is identity data (SECURITY.md E1/H8): accepted and returned
to callers who may see the account, never logged, audited or put in events.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from datetime import date, datetime
from typing import Annotated, Final, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.auth.models import GrantLevel
from app.core.money import CurrencyCode, Money, MoneyIn
from app.domain.accounts.models import NAME_MAX, AccountType, AssetKind, BalanceSource

#: Largest ``limit`` of a list endpoint (API.md: cursor pagination, max 500).
MAX_PAGE: Final = 500
DEFAULT_PAGE: Final = 100
_LAST4: Final = re.compile(r"^[0-9]{4}$")


def clean_text(value: str) -> str:
    """Stripped, non-empty, printable text (names shown in the UI)."""
    cleaned = value.strip()
    if not cleaned or not cleaned.isprintable():
        raise ValueError("must be non-empty printable text")
    return cleaned


def clean_last4(value: str) -> str:
    cleaned = value.strip()
    if not _LAST4.fullmatch(cleaned):
        raise ValueError("must be exactly four digits")
    return cleaned


Name = Annotated[str, Field(min_length=1, max_length=NAME_MAX), AfterValidator(clean_text)]
Last4 = Annotated[str, Field(min_length=4, max_length=8), AfterValidator(clean_last4)]


class _Input(BaseModel):
    """Base for every input: unknown fields are an error (F4)."""

    model_config = ConfigDict(extra="forbid")


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


class AccountCreate(_Input):
    """``POST /accounts``.

    ``currency`` defaults to the household's base currency; ``is_asset`` and
    ``manual_balance`` default from ``type``. ``asset_kind`` is required for
    (and only for) ``manual_asset``. ``opening_balance`` records a manual
    balance snapshot on ``opening_balance_date`` (default ``opened_on``, else
    today); its currency must be the account's.
    """

    type: AccountType
    name: Name
    nickname: Name | None = None
    currency: CurrencyCode | None = None
    institution_id: uuid.UUID | None = None
    asset_kind: AssetKind | None = None
    number_last4: Last4 | None = None
    is_asset: bool | None = None
    manual_balance: bool | None = None
    include_in_net_worth: bool = True
    opened_on: date | None = None
    opening_balance: MoneyIn | None = None
    opening_balance_date: date | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.type is AccountType.MANUAL_ASSET) != (self.asset_kind is not None):
            raise ValueError("asset_kind is required for manual_asset accounts and only for them")
        if self.opening_balance_date is not None and self.opening_balance is None:
            raise ValueError("opening_balance_date needs opening_balance")
        return self


#: Fields of :class:`AccountUpdate` that may be set to ``null`` (cleared).
NULLABLE_UPDATE_FIELDS: Final = frozenset(
    {"nickname", "institution_id", "asset_kind", "number_last4", "opened_on"}
)


class AccountUpdate(_Input):
    """``PATCH /accounts/{id}``: only the fields given change.

    ``null`` clears ``nickname``, ``institution_id``, ``number_last4`` and
    ``opened_on``. Changing ``type`` resets ``is_asset``/``manual_balance`` to
    the new type's defaults unless they are given too. ``currency`` can only
    change while the account has no transactions, balances or schedules.
    Closing and reopening have their own endpoints.
    """

    type: AccountType | None = None
    name: Name | None = None
    nickname: Name | None = None
    currency: CurrencyCode | None = None
    institution_id: uuid.UUID | None = None
    asset_kind: AssetKind | None = None
    number_last4: Last4 | None = None
    is_asset: bool | None = None
    manual_balance: bool | None = None
    include_in_net_worth: bool | None = None
    opened_on: date | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        given = self.model_fields_set
        if not given:
            raise ValueError("give at least one field to change")
        for name in given - NULLABLE_UPDATE_FIELDS:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class AccountClose(_Input):
    """``POST /accounts/{id}/close``: the closing date (default today)."""

    closed_on: date | None = None


class AccountOut(_Output):
    id: uuid.UUID
    type: AccountType
    asset_kind: AssetKind | None
    name: str
    nickname: str | None
    currency: str
    institution_id: uuid.UUID | None
    number_last4: str | None
    is_asset: bool
    manual_balance: bool
    include_in_net_worth: bool
    opened_on: date | None
    closed_on: date | None
    is_closed: bool
    #: Balance at the end of ``balance_date`` (today, UTC) in the account currency.
    balance: Money
    balance_date: date
    #: Whether the caller may change this account (edit grant, or owner/admin).
    can_edit: bool
    created_at: datetime
    updated_at: datetime


class AccountPage(_Output):
    items: list[AccountOut]
    #: Pass as ``cursor`` for the next page; ``null`` on the last page.
    next_cursor: uuid.UUID | None


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------


class ManualBalanceIn(_Input):
    """``POST /accounts/{id}/balances``: the balance at the end of ``as_of_date``.

    Replaces an earlier manual balance of the same date. The currency must be
    the account's.
    """

    as_of_date: date
    balance: MoneyIn


class SnapshotOut(_Output):
    id: uuid.UUID
    as_of_date: date
    balance: Money
    source: BalanceSource
    created_at: datetime
    updated_at: datetime


class BalancePoint(_Output):
    date: dt.date
    balance: Money


class BalanceHistoryOut(_Output):
    account_id: uuid.UUID
    currency: str
    start: date
    end: date
    #: End-of-day balances, one per day of ``start..end``.
    items: list[BalancePoint]
    #: The balance snapshots inside the range (statement, manual, computed).
    snapshots: list[SnapshotOut]


# ---------------------------------------------------------------------------
# Account grants
# ---------------------------------------------------------------------------


class AccountGrantLevelSet(_Input):
    """``PUT /households/{id}/members/{user_id}/grants/{account_id}``."""

    level: GrantLevel

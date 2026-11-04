# SPDX-License-Identifier: AGPL-3.0-or-later
"""Securities, lots, investment transactions, holdings and prices (DATA_MODEL.md "Investments").

All ``HouseholdScoped`` (forced RLS); references between household-scoped
tables include ``household_id`` (ADR-0008, ``app.db.types``).

* ``security``: the household's own catalogue (no shared instance-wide
  table: what a household holds is not visible to another one, not even the
  fact that a ticker exists). ``(household_id, symbol, exchange)`` is unique
  (NULL exchange counts as one value). ``currency`` is the quote currency;
  ``price`` rows are in it (composite key on ``(household_id, id,
  currency)``).
* ``price``: closing price of a security on a date, at most one per security
  and date. ``source`` is ``manual`` or a provider name (Phase 5 price feeds
  are opt-in and send only the symbol).
* ``investment_txn``: one brokerage activity row. ``amount`` is the signed
  cash effect on the account in the account currency (a buy is negative, a
  sale or dividend positive), ``fees`` is a non-negative part of it.
  ``quantity`` and ``price`` are in units and the security's currency.
  ``lot_links`` is a JSON array of ``{"lot_id": "<uuid>", "quantity":
  "<decimal>"}``: the lots a sell/transfer_out consumed or the lot a
  buy/reinvest/transfer_in opened (owned by ``app.domain.investments.service``).
* ``lot``: a tax lot of a security in an account. ``quantity`` is what was
  opened, ``remaining_quantity`` what is still held; a lot is closed exactly
  when nothing remains. ``cost_per_unit`` is in the account currency.
* ``holding``: derived cache, one row per account and security, rebuilt from
  lots (``quantity`` = sum of remaining lot quantities, ``cost_basis`` their
  cost in the account currency). Never the source of truth, never exported.
* ``investment_account``: per-account investment settings (the cost method,
  ``fifo`` or ``average``; no row means ``fifo``).

Lots, ``lot_links`` and holdings are written only by the lot engine
(``app.domain.investments.lots`` through ``service.replay_position``), which
replays an account's transactions of one security in trade order. A
``split`` (or a ``corporate_action`` with a ratio) stores its ratio as
``quantity`` new units for every ``price`` old units (``3``/``1`` for a 3-for-1
split, ``1``/``10`` for a 1-for-10 reverse split), so the ratio is exact.
``lots_specified`` marks a sell/transfer_out whose ``lot_links`` were chosen
by the person (specific lots); otherwise the engine rewrites them on every
replay.

Quantities and unit prices are ``NUMERIC(28, 10)`` (``app.db.types.QUANTITY``),
money ``NUMERIC(20, 6)``. Account visibility (``account_grant``) applies to
every account-derived row here: holdings, lots and investment transactions
of a hidden account are never shown to a member or viewer.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# Referenced tables must be in the metadata wherever these models are used.
import app.domain.accounts.models
import app.domain.documents.models  # noqa: F401
from app.core.dates import min_date_check
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import (
    AMOUNT,
    CURRENCY,
    QUANTITY,
    UNIT_PRICE,
    currency_check,
    household_fk,
    household_key,
    household_ref_fk,
    pg_enum,
)

NAME_MAX = 200
SYMBOL_MAX = 32
EXCHANGE_MAX = 32
SECTOR_MAX = 100
SOURCE_MAX = 64
MEMO_MAX = 1000
MANUAL_SOURCE = "manual"


class AssetClass(enum.StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    MUTUAL_FUND = "mutual_fund"
    BOND = "bond"
    CASH_EQUIVALENT = "cash_equivalent"
    OPTION = "option"
    CRYPTO = "crypto"
    REAL_ESTATE = "real_estate"
    COMMODITY = "commodity"
    OTHER = "other"


class CostMethod(enum.StrEnum):
    """How a sale picks its cost: first in first out (specific lots allowed) or average cost."""

    FIFO = "fifo"
    AVERAGE = "average"


class InvestmentTxnType(enum.StrEnum):
    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    INTEREST = "interest"
    FEE = "fee"
    SPLIT = "split"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    REINVEST = "reinvest"
    CORPORATE_ACTION = "corporate_action"


#: Types that always name a security.
SECURITY_TXN_TYPES = frozenset(
    {
        InvestmentTxnType.BUY,
        InvestmentTxnType.SELL,
        InvestmentTxnType.SPLIT,
        InvestmentTxnType.TRANSFER_IN,
        InvestmentTxnType.TRANSFER_OUT,
        InvestmentTxnType.REINVEST,
    }
)
#: Types that move units and so carry a positive ``quantity``.
QUANTITY_TXN_TYPES = frozenset(
    {
        InvestmentTxnType.BUY,
        InvestmentTxnType.SELL,
        InvestmentTxnType.TRANSFER_IN,
        InvestmentTxnType.TRANSFER_OUT,
        InvestmentTxnType.REINVEST,
    }
)


def _sql_in(values: frozenset[InvestmentTxnType]) -> str:
    return ", ".join(f"'{v.value}'" for v in sorted(values))


class Security(UUIDPk, Timestamps, HouseholdScoped, Base):
    """An instrument in the household's catalogue."""

    __tablename__ = "security"
    __table_args__ = (
        household_key("security"),
        UniqueConstraint(
            "household_id", "id", "currency", name="uq_security_household_id_id_currency"
        ),
        UniqueConstraint(
            "household_id",
            "symbol",
            "exchange",
            name="uq_security_household_symbol_exchange",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_security_household_isin", "household_id", "isin"),
        Index("ix_security_household_cusip", "household_id", "cusip"),
        currency_check(),
        CheckConstraint("symbol ~ '^[A-Za-z0-9.^/_:-]{1,32}$'", name="symbol_format"),
        CheckConstraint("isin IS NULL OR isin ~ '^[A-Z]{2}[A-Z0-9]{9}[0-9]$'", name="isin_format"),
        CheckConstraint("cusip IS NULL OR cusip ~ '^[0-9A-Z*@#]{9}$'", name="cusip_format"),
    )

    symbol: Mapped[str] = mapped_column(String(SYMBOL_MAX), nullable=False)
    isin: Mapped[str | None] = mapped_column(CHAR(12))
    cusip: Mapped[str | None] = mapped_column(CHAR(9))
    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    asset_class: Mapped[AssetClass] = mapped_column(
        pg_enum(AssetClass, "asset_class"), nullable=False
    )
    sector: Mapped[str | None] = mapped_column(String(SECTOR_MAX))
    exchange: Mapped[str | None] = mapped_column(String(EXCHANGE_MAX))
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)


class Price(UUIDPk, Timestamps, HouseholdScoped, Base):
    """Closing price of a security on ``price_date`` in the security's currency."""

    __tablename__ = "price"
    __table_args__ = (
        household_fk("price", "security_id", "security", extra=(("currency", "currency"),)),
        # Also the index for "latest price on or before a date".
        UniqueConstraint("security_id", "price_date", name="uq_price_security_date"),
        currency_check(),
        CheckConstraint("close > 0", name="close_positive"),
        CheckConstraint(min_date_check("price_date"), name="price_date_min"),
    )

    security_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    price_date: Mapped[date] = mapped_column(Date, nullable=False)
    close: Mapped[Decimal] = mapped_column(UNIT_PRICE, nullable=False)
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    source: Mapped[str] = mapped_column(String(SOURCE_MAX), nullable=False, default=MANUAL_SOURCE)


class InvestmentTxn(UUIDPk, Timestamps, HouseholdScoped, Base):
    """One brokerage activity row (module doc for signs and ``lot_links``)."""

    __tablename__ = "investment_txn"
    __table_args__ = (
        household_key("investment_txn"),
        household_fk("investment_txn", "account_id", "account", extra=(("currency", "currency"),)),
        household_fk("investment_txn", "security_id", "security", ondelete="RESTRICT"),
        household_ref_fk("investment_txn", "source_document_id", "document"),
        Index("ix_investment_txn_account_date", "account_id", "trade_date"),
        Index("ix_investment_txn_account_hash", "account_id", "import_hash"),
        currency_check(),
        CheckConstraint(
            f"security_id IS NOT NULL OR type NOT IN ({_sql_in(SECURITY_TXN_TYPES)})",
            name="security_required",
        ),
        CheckConstraint(
            f"type NOT IN ({_sql_in(QUANTITY_TXN_TYPES)}) OR "
            "(quantity IS NOT NULL AND quantity > 0)",
            name="quantity_required",
        ),
        CheckConstraint("price IS NULL OR price >= 0", name="price_non_negative"),
        CheckConstraint("fees >= 0", name="fees_non_negative"),
        CheckConstraint(
            "settle_date IS NULL OR settle_date >= trade_date", name="settle_after_trade"
        ),
        CheckConstraint(min_date_check("trade_date"), name="trade_date_min"),
        CheckConstraint("jsonb_typeof(lot_links) = 'array'", name="lot_links_array"),
        CheckConstraint(
            "import_hash IS NULL OR import_hash ~ '^[0-9a-f]{64}$'", name="import_hash_hex"
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    #: ``RESTRICT``: a security with activity cannot be deleted.
    security_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), index=True)
    type: Mapped[InvestmentTxnType] = mapped_column(
        pg_enum(InvestmentTxnType, "investment_txn_type"), nullable=False
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    settle_date: Mapped[date | None] = mapped_column(Date)
    quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    price: Mapped[Decimal | None] = mapped_column(UNIT_PRICE)
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    fees: Mapped[Decimal] = mapped_column(
        AMOUNT, nullable=False, default=Decimal(0), server_default=text("0")
    )
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    lot_links: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    memo: Mapped[str | None] = mapped_column(String(MEMO_MAX), info={"classification": "free_text"})
    #: ``lot_links`` of a sell/transfer_out were chosen by the person (specific lots).
    lots_specified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Cross-statement deduplication hash (as ``transaction.import_hash``).
    import_hash: Mapped[str | None] = mapped_column(CHAR(64))
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document.id", ondelete="SET NULL"), index=True
    )


class Lot(UUIDPk, Timestamps, HouseholdScoped, Base):
    """A tax lot: units of a security opened in an account on ``open_date``."""

    __tablename__ = "lot"
    __table_args__ = (
        household_key("lot"),
        household_fk("lot", "account_id", "account", extra=(("currency", "currency"),)),
        household_fk("lot", "security_id", "security", ondelete="RESTRICT"),
        household_ref_fk("lot", "opening_txn_id", "investment_txn"),
        Index("ix_lot_account_security", "account_id", "security_id", "open_date"),
        currency_check(),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "remaining_quantity >= 0 AND remaining_quantity <= quantity",
            name="remaining_within_quantity",
        ),
        CheckConstraint("cost_per_unit >= 0", name="cost_non_negative"),
        CheckConstraint(
            "(closed_date IS NULL) = (remaining_quantity > 0)", name="closed_when_empty"
        ),
        CheckConstraint(
            "closed_date IS NULL OR closed_date >= open_date", name="closed_after_open"
        ),
        CheckConstraint(min_date_check("open_date"), name="open_date_min"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    security_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    open_date: Mapped[date] = mapped_column(Date, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    remaining_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    cost_per_unit: Mapped[Decimal] = mapped_column(UNIT_PRICE, nullable=False)
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    closed_date: Mapped[date | None] = mapped_column(Date)
    #: The buy/reinvest/transfer_in that opened the lot (``SET NULL`` on delete).
    opening_txn_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("investment_txn.id", ondelete="SET NULL"), index=True
    )


class Holding(UUIDPk, Timestamps, HouseholdScoped, Base):
    """Derived cache: current position of a security in an account."""

    __tablename__ = "holding"
    __table_args__ = (
        household_fk("holding", "account_id", "account", extra=(("currency", "currency"),)),
        household_fk("holding", "security_id", "security"),
        UniqueConstraint("account_id", "security_id", name="uq_holding_account_security"),
        currency_check(),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    security_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    #: In the account currency.
    cost_basis: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)


class InvestmentAccount(UUIDPk, Timestamps, HouseholdScoped, Base):
    """Investment settings of one account (no row: the defaults)."""

    __tablename__ = "investment_account"
    __table_args__ = (
        household_fk("investment_account", "account_id", "account"),
        UniqueConstraint("account_id", name="uq_investment_account_account"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    cost_method: Mapped[CostMethod] = mapped_column(
        pg_enum(CostMethod, "cost_method"), nullable=False, default=CostMethod.FIFO
    )

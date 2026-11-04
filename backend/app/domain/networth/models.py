# SPDX-License-Identifier: AGPL-3.0-or-later
"""Daily net worth snapshots (DATA_MODEL.md "Net worth").

``net_worth_snapshot`` is a derived, household-wide figure computed by the
worker (``snapshot_net_worth``, daily) from ``balance_snapshot``,
``holding`` x ``price``, loan balances and ``asset_valuation``, converted to
the household's base currency (``currency``, the base currency on the day
it was computed). At most one per household and date; ``assets`` and
``liabilities`` are non-negative magnitudes and ``net = assets -
liabilities`` (check constraint).

``breakdown`` is a JSON object owned by ``app.domain.networth.service``
(``{"accounts": [{"account_id", "side", "amount", "currency", "amount_base"}],
"unconverted": [...]}``). Because it names every account, a snapshot is shown
only to principals who see every account of the household (owners, admins,
or members/viewers granted every account); everyone else gets figures
recomputed over their visible accounts. It is exported as stored history
(docs/PORTABILITY.md; the importer re-maps the ids in ``breakdown``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, Date, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import AMOUNT, CURRENCY, currency_check


class NetWorthSnapshot(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "net_worth_snapshot"
    __table_args__ = (
        UniqueConstraint("household_id", "as_of_date", name="uq_net_worth_snapshot_date"),
        currency_check(),
        CheckConstraint("assets >= 0 AND liabilities >= 0", name="sides_non_negative"),
        CheckConstraint("net = assets - liabilities", name="net_is_difference"),
        CheckConstraint("jsonb_typeof(breakdown) = 'object'", name="breakdown_object"),
    )

    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    assets: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    liabilities: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    net: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

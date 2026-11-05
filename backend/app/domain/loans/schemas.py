# SPDX-License-Identifier: AGPL-3.0-or-later
"""Input and output schemas for loans, payments, rate changes and simulations (API.md ``/loans``).

Inputs forbid unknown fields (SECURITY.md F4). Money is ``{"amount":
"12.34", "currency": "USD"}`` (``app.core.money``). Interest rates are
**fractions** given as decimal strings (``"0.06125"`` is 6.125 %), at most
eight decimals, ``0 <= rate < 1``.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.core.dates import BusinessDate
from app.core.money import Money, MoneyIn
from app.domain.loans.models import LENDER_MAX, TERM_MONTHS_MAX, RateType

MAX_PAGE: Final = 500
DEFAULT_PAGE: Final = 100
#: Most payments ``GET /loans/{id}/payments`` returns at once.
MAX_PAYMENTS: Final = 1200
#: Most one-off extra payments one simulation may name.
MAX_ONE_OFF_EXTRAS: Final = 240
_RATE_RE: Final = re.compile(r"^(?:0(?:\.[0-9]{1,8})?|\.[0-9]{1,8})$")


def _rate(value: object) -> Decimal:
    if not isinstance(value, str) or not _RATE_RE.fullmatch(value.strip()):
        raise ValueError('rate must be a decimal fraction string such as "0.06125" (0 <= rate < 1)')
    return Decimal(value.strip())


#: An annual rate as a fraction string (``"0.06125"``).
RateIn = Annotated[Decimal, BeforeValidator(_rate)]
Lender = Annotated[str, Field(min_length=1, max_length=LENDER_MAX)]
TermMonths = Annotated[int, Field(ge=1, le=TERM_MONTHS_MAX)]
PaymentDay = Annotated[int, Field(ge=1, le=31)]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True)


def _not_null(model: BaseModel, names: tuple[str, ...]) -> None:
    for name in names:
        if name in model.model_fields_set and getattr(model, name) is None:
            raise ValueError(f"{name} cannot be null")


# ---------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------


class LoanCreate(_Input):
    """``POST /loans``: the terms of an existing loan, mortgage or line-of-credit account.

    ``payment_amount`` (principal and interest) defaults to the level
    payment; ``payment_day`` to the origination date's day.
    """

    account_id: uuid.UUID
    principal_original: MoneyIn
    rate: RateIn
    rate_type: RateType = RateType.FIXED
    term_months: TermMonths
    origination_date: BusinessDate
    payment_amount: MoneyIn | None = None
    payment_day: PaymentDay | None = None
    escrow_amount: MoneyIn | None = None
    lender: Lender | None = None


class LoanUpdate(_Input):
    """``PATCH /loans/{id}``: absent fields stay; ``escrow_amount``/``lender`` may be null."""

    principal_original: MoneyIn | None = None
    rate: RateIn | None = None
    rate_type: RateType | None = None
    term_months: TermMonths | None = None
    origination_date: BusinessDate | None = None
    payment_amount: MoneyIn | None = None
    payment_day: PaymentDay | None = None
    escrow_amount: MoneyIn | None = None
    lender: Lender | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _not_null(
            self,
            (
                "principal_original",
                "rate",
                "rate_type",
                "term_months",
                "origination_date",
                "payment_amount",
                "payment_day",
            ),
        )
        return self


class ProjectionOut(_Output):
    """Where the loan stands and where it is heading (no extra payments).

    ``balance`` is the principal owed at the end of ``as_of`` (the loan
    account's balance, kept in sync with payments and statements).
    """

    as_of: date
    balance: Money
    #: ``recorded``: from payments and statements; ``scheduled``: none
    #: recorded yet, so the original schedule's balance (payments assumed
    #: made as scheduled).
    balance_source: Literal["recorded", "scheduled"]
    rate: str
    scheduled_payment: Money
    next_due_date: date | None
    payoff_date: date | None
    remaining_payments: int
    remaining_interest: Money


class LoanOut(_Output):
    id: uuid.UUID
    account_id: uuid.UUID
    principal_original: Money
    rate: str
    rate_type: RateType
    term_months: int
    origination_date: date
    payment_amount: Money
    payment_day: int
    escrow_amount: Money | None
    lender: str | None
    maturity_date: date
    projection: ProjectionOut
    created_at: datetime
    updated_at: datetime


class LoanPage(_Output):
    items: list[LoanOut]
    next_cursor: uuid.UUID | None


# ---------------------------------------------------------------------------
# Schedules and simulations
# ---------------------------------------------------------------------------


class ScheduleRowOut(_Output):
    number: int
    due_date: date
    rate: str
    payment: Money
    principal: Money
    interest: Money
    extra: Money
    escrow: Money
    total: Money
    balance: Money


class ScheduleTotalsOut(_Output):
    payments: int
    principal: Money
    interest: Money
    extra: Money
    escrow: Money
    paid: Money
    payoff_date: date | None


class ScheduleOut(_Output):
    """``GET /loans/{id}/schedule``.

    ``basis=original``: from origination over the original principal (rate
    changes applied); ``basis=projected``: from the balance owed today.
    """

    loan_id: uuid.UUID
    basis: Literal["original", "projected"]
    start_balance: Money
    rows: list[ScheduleRowOut]
    totals: ScheduleTotalsOut


class OneOffExtraIn(_Input):
    on: BusinessDate
    amount: MoneyIn


class RecurringExtraIn(_Input):
    """An extra added to every payment due from ``start`` (default: the next one) to ``end``."""

    amount: MoneyIn
    start: BusinessDate | None = None
    end: BusinessDate | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("end must not be before start")
        return self


class SimulateIn(_Input):
    """``POST /loans/{id}/simulate``: extra principal payments to try (nothing is stored)."""

    one_off: Annotated[list[OneOffExtraIn], Field(max_length=MAX_ONE_OFF_EXTRAS)] = Field(
        default_factory=list
    )
    recurring: RecurringExtraIn | None = None
    include_schedule: bool = False


class ScenarioOut(_Output):
    payoff_date: date | None
    payments: int
    total_interest: Money
    total_extra: Money


class SimulationOut(_Output):
    loan_id: uuid.UUID
    as_of: date
    balance: Money
    baseline: ScenarioOut
    with_extras: ScenarioOut
    interest_saved: Money
    payments_saved: int
    #: The schedule with the extras (``include_schedule``).
    schedule: list[ScheduleRowOut] | None = None


# ---------------------------------------------------------------------------
# Payments and rate changes
# ---------------------------------------------------------------------------


class LoanPaymentCreate(_Input):
    """``POST /loans/{id}/payments``: a payment's split (parts >= 0, loan currency)."""

    paid_on: BusinessDate
    principal: MoneyIn
    interest: MoneyIn
    escrow: MoneyIn | None = None
    fees: MoneyIn | None = None
    transaction_id: uuid.UUID | None = None


class ScheduleCheckOut(_Output):
    """A payment compared with the schedule: interest expected on the balance before it.

    ``status``: ``ok`` within one minor unit, ``differs`` otherwise,
    ``unchecked`` when the balance before the payment is not known.
    """

    status: Literal["ok", "differs", "unchecked"]
    expected_interest: Money | None
    difference: Money | None


class LoanPaymentOut(_Output):
    id: uuid.UUID
    loan_id: uuid.UUID
    paid_on: date
    principal: Money
    interest: Money
    escrow: Money
    fees: Money
    total: Money
    balance_after: Money | None
    transaction_id: uuid.UUID | None
    source_document_id: uuid.UUID | None
    schedule_check: ScheduleCheckOut
    created_at: datetime


class LoanPaymentList(_Output):
    """``GET /loans/{id}/payments``: oldest first, at most ``limit``."""

    items: list[LoanPaymentOut]


class RateChangeCreate(_Input):
    effective_date: BusinessDate
    new_rate: RateIn


class RateChangeOut(_Output):
    id: uuid.UUID
    loan_id: uuid.UUID
    effective_date: date
    new_rate: str
    created_at: datetime


class RateChangeList(_Output):
    items: list[RateChangeOut]

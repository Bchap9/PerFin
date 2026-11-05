# SPDX-License-Identifier: AGPL-3.0-or-later
"""Allowlist serialisers of the AI views (AI_ACCESS.md M1, SECURITY.md E1).

Every response model here is built **only** from allowlisted fields: each
field names where its value comes from with :func:`src` (``x-ai-source`` in
the OpenAPI document), as ``"<table>.<column>"`` or one of the pseudo
sources in :data:`PSEUDO_SOURCES`. ``tests/security/ai/test_column_classification.py``
fails for any field without sources, for any source column that is not in
``app.domain.ai.classification`` or is classified ``denied``, and for a model
here that the views do not use. A new column is therefore invisible to AI
clients until someone adds a field naming it, and that change must classify
the column first.

Identity classes (government ids, account and card numbers including the
last four, member names and contacts, addresses, documents and their text,
credentials, tax identity boxes, manual asset descriptions) have **no**
serialiser at all. Free-text values (payees, memos, notes, names people type)
are scrubbed (``app.core.scrubber``) before they are put into these models.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Sources that are not a column: ``computed`` (arithmetic or a constant over
#: the field's other sources or the request), ``policy`` (the client's own
#: exposure policy), ``nested`` (a model or list of models checked field by
#: field), ``value`` (a money or label carrier whose parent names the
#: columns), ``request`` (echo of a validated request parameter).
PSEUDO_SOURCES: Final = frozenset({"computed", "policy", "nested", "value", "request"})

SOURCE_KEY: Final = "x-ai-source"


def src(*sources: str, default: Any = ...) -> Any:
    """A field with its allowlisted sources (module doc)."""
    if not sources:
        raise ValueError("an AI field needs at least one source")
    return Field(default, json_schema_extra={SOURCE_KEY: list(sources)})


class AiOut(BaseModel):
    """Base of every AI view model: frozen, no extra fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class AiMoney(AiOut):
    """Money after the client's rounding; the parent field names the columns."""

    amount: str = src("value")
    currency: str = src("value")


# ---------------------------------------------------------------------------
# Data policy
# ---------------------------------------------------------------------------


class AiDataPolicyOut(AiOut):
    """``explain_data_policy``: this client's mode, options and the denylist."""

    client_name: str = src("ai_client.name")
    mode: Literal["detail", "aggregates"] = src("ai_client.mode")
    rounding: int | None = src("ai_client.rounding")
    hide_nicknames: bool = src("ai_client.hide_nicknames")
    hide_merchant_location: bool = src("ai_client.hide_merchant_location")
    date_range_cap_days: int | None = src("ai_client.date_range_cap")
    earliest_date: date | None = src("ai_client.date_range_cap", "computed")
    custom_denylist_entries: int = src("policy")
    placeholders: list[str] = src("computed")
    never_exposed: list[str] = src("computed")
    exposed: list[str] = src("computed")
    rate_limits: list[str] = src("computed")


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


class AccountAi(AiOut):
    id: str = src("account.id")
    label: str = src("account.nickname", "account.name", "account.type", "policy")
    type: str = src("account.type")
    asset_kind: str | None = src("account.asset_kind")
    institution: str | None = src("institution.name")
    currency: str = src("account.currency")
    balance: AiMoney = src("balance_snapshot.balance", "transaction.amount", "computed")
    is_asset: bool = src("account.is_asset")
    closed: bool = src("account.closed_on")


class AccountTypeTotal(AiOut):
    type: str = src("account.type")
    accounts: int = src("computed")
    balance: list[AiMoney] = src("balance_snapshot.balance", "transaction.amount", "computed")


class AiAccountsOut(AiOut):
    as_of: date = src("computed")
    mode: Literal["detail", "aggregates"] = src("policy")
    items: list[AccountAi] = src("nested")
    by_type: list[AccountTypeTotal] = src("nested")


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class SplitAi(AiOut):
    category: str | None = src("category.name")
    amount: AiMoney = src("transaction_split.amount")
    memo: str | None = src("transaction_split.memo")


class TransactionAi(AiOut):
    id: str = src("transaction.id")
    posted_date: date = src("transaction.posted_date")
    amount: AiMoney = src("transaction.amount", "transaction.currency")
    account_id: str = src("transaction.account_id")
    account: str = src("account.nickname", "account.name", "account.type", "policy")
    payee: str | None = src("payee.name", "transaction.raw_payee")
    merchant_city: str | None = src("transaction.raw_payee", "policy")
    merchant_state: str | None = src("transaction.raw_payee", "policy")
    category: str | None = src("category.name")
    tags: list[str] = src("tag.name")
    memo: str | None = src("transaction.memo")
    notes: str | None = src("transaction.notes")
    status: str = src("transaction.status")
    transfer: bool = src("transaction.transfer_group_id")
    splits: list[SplitAi] = src("nested")


class AiTransactionsOut(AiOut):
    date_from: date = src("request", "policy")
    date_to: date = src("request")
    items: list[TransactionAi] = src("nested")
    next_cursor: str | None = src("transaction.posted_date", "transaction.id")


# ---------------------------------------------------------------------------
# Spending, income, trends, overview
# ---------------------------------------------------------------------------


class FlowItemAi(AiOut):
    name: str | None = src(
        "category.name",
        "payee.name",
        "account.type",
        "account.nickname",
        "account.name",
        "computed",
    )
    amount: AiMoney = src("transaction.amount", "transaction_split.amount", "fx_rate.rate")


class AiFlowOut(AiOut):
    flow: Literal["spending", "income"] = src("request")
    group_by: str = src("request")
    currency: str = src("household.base_currency")
    date_from: date = src("request", "policy")
    date_to: date = src("request")
    items: list[FlowItemAi] = src("nested")
    total: AiMoney = src("transaction.amount", "transaction_split.amount", "fx_rate.rate")


class TrendPointAi(AiOut):
    month: str = src("computed")
    amount: AiMoney = src("transaction.amount", "transaction_split.amount")


class TrendRowAi(AiOut):
    name: str | None = src("category.name", "computed")
    amount: AiMoney = src("transaction.amount", "transaction_split.amount")
    previous: AiMoney | None = src("transaction.amount", "transaction_split.amount", "policy")
    year_ago: AiMoney | None = src("transaction.amount", "transaction_split.amount", "policy")
    mom_percent: str | None = src("computed")
    yoy_percent: str | None = src("computed")


class AiTrendsOut(AiOut):
    flow: Literal["spending", "income"] = src("request")
    month: str = src("request")
    months: int = src("request", "policy")
    currency: str = src("household.base_currency")
    series: list[TrendPointAi] = src("nested")
    total: TrendRowAi = src("nested")
    categories: list[TrendRowAi] = src("nested")


class CashflowMonthAi(AiOut):
    month: str = src("computed")
    inflow: AiMoney = src("transaction.amount")
    outflow: AiMoney = src("transaction.amount")
    net: AiMoney = src("transaction.amount")


class AiOverviewOut(AiOut):
    """``get_overview``: net worth now, cash flow and savings rate over the period."""

    as_of: date = src("computed")
    currency: str = src("household.base_currency")
    date_from: date = src("request", "policy")
    date_to: date = src("request")
    assets: AiMoney = src(
        "balance_snapshot.balance", "transaction.amount", "holding.quantity", "price.close"
    )
    liabilities: AiMoney = src("balance_snapshot.balance", "transaction.amount")
    net_worth: AiMoney = src(
        "balance_snapshot.balance", "transaction.amount", "holding.quantity", "price.close"
    )
    inflow: AiMoney = src("transaction.amount")
    outflow: AiMoney = src("transaction.amount")
    net_cash_flow: AiMoney = src("transaction.amount")
    savings_rate: str | None = src("computed")
    months: list[CashflowMonthAi] = src("nested")


# ---------------------------------------------------------------------------
# Budgets, goals, recurring
# ---------------------------------------------------------------------------


class BudgetLineAi(AiOut):
    category: str | None = src("category.name")
    kind: str | None = src("category.kind")
    budgeted: AiMoney = src("budget_line.amount")
    available: AiMoney = src("budget_line.amount", "transaction.amount")
    actual: AiMoney = src("transaction.amount", "transaction_split.amount")
    remaining: AiMoney = src("budget_line.amount", "transaction.amount")
    forecast: AiMoney = src("transaction.amount", "computed")


class BudgetTotalsAi(AiOut):
    budgeted: AiMoney = src("budget_line.amount")
    actual: AiMoney = src("transaction.amount")
    remaining: AiMoney = src("budget_line.amount", "transaction.amount")
    forecast: AiMoney = src("transaction.amount", "computed")


class BudgetAi(AiOut):
    name: str = src("budget.name", "policy")
    period: str = src("budget.period")
    period_start: date = src("budget.start_date", "computed")
    period_end: date = src("budget.end_date", "computed")
    currency: str = src("budget.currency")
    lines: list[BudgetLineAi] = src("nested")
    unbudgeted: AiMoney = src("transaction.amount")
    expense: BudgetTotalsAi = src("nested")
    income: BudgetTotalsAi = src("nested")


class AiBudgetStatusOut(AiOut):
    as_of: date = src("request")
    budgets: list[BudgetAi] = src("nested")


class GoalAi(AiOut):
    name: str = src("goal.name", "policy")
    target: AiMoney = src("goal.target_amount", "goal.currency")
    target_date: date | None = src("goal.target_date")
    contributed: AiMoney = src("goal_contribution.amount")
    remaining: AiMoney = src("goal.target_amount", "goal_contribution.amount")
    percent: str = src("computed")
    projected_completion: date | None = src("computed")
    on_track: bool | None = src("computed")
    achieved: bool = src("goal.achieved_at")
    archived: bool = src("goal.archived")


class AiGoalsOut(AiOut):
    items: list[GoalAi] = src("nested")


class RecurringAi(AiOut):
    name: str = src("scheduled_transaction.name")
    payee: str | None = src("payee.name")
    category: str | None = src("category.name")
    account: str = src("account.nickname", "account.name", "account.type", "policy")
    cadence: str = src("scheduled_transaction.cadence")
    interval_count: int = src("scheduled_transaction.interval_count")
    next_due: date = src("scheduled_transaction.next_due")
    amount_min: AiMoney = src("scheduled_transaction.amount_min", "scheduled_transaction.currency")
    amount_max: AiMoney = src("scheduled_transaction.amount_max", "scheduled_transaction.currency")
    origin: str = src("scheduled_transaction.origin")
    active: bool = src("scheduled_transaction.active")
    last_seen_on: date | None = src("scheduled_transaction.last_seen_on")


class AiRecurringOut(AiOut):
    items: list[RecurringAi] = src("nested")


# ---------------------------------------------------------------------------
# Net worth, investments
# ---------------------------------------------------------------------------


class NetWorthPointAi(AiOut):
    as_of: date = src("net_worth_snapshot.as_of_date", "computed")
    assets: AiMoney = src("net_worth_snapshot.assets", "balance_snapshot.balance", "computed")
    liabilities: AiMoney = src(
        "net_worth_snapshot.liabilities", "balance_snapshot.balance", "computed"
    )
    net: AiMoney = src("net_worth_snapshot.net", "computed")


class AiNetWorthHistoryOut(AiOut):
    currency: str = src("household.base_currency")
    range: str = src("request")
    granularity: str = src("request", "computed")
    date_from: date = src("computed", "policy")
    date_to: date = src("computed")
    points: list[NetWorthPointAi] = src("nested")
    change: AiMoney | None = src("computed")


class AllocationAi(AiOut):
    key: str = src(
        "security.asset_class", "security.sector", "security.currency", "account.type", "computed"
    )
    value: AiMoney = src("holding.quantity", "price.close")
    weight: str = src("computed")


class AiAllocationOut(AiOut):
    as_of: date = src("computed")
    currency: str = src("household.base_currency")
    market_value: AiMoney = src("holding.quantity", "price.close")
    by: dict[str, list[AllocationAi]] = src("nested")


class GainsAi(AiOut):
    short_term: AiMoney = src("lot.cost_per_unit", "price.close")
    long_term: AiMoney = src("lot.cost_per_unit", "price.close")
    total: AiMoney = src("lot.cost_per_unit", "price.close")


class HoldingAi(AiOut):
    account: str = src("account.nickname", "account.name", "account.type", "policy")
    symbol: str = src("security.symbol")
    name: str = src("security.name")
    asset_class: str = src("security.asset_class")
    sector: str | None = src("security.sector")
    currency: str = src("security.currency")
    quantity: str = src("holding.quantity")
    cost_basis: AiMoney = src("holding.cost_basis")
    price: str | None = src("price.close", "investment_txn.price")
    price_date: date | None = src("price.price_date", "investment_txn.trade_date")
    market_value: AiMoney | None = src("holding.quantity", "price.close")
    unrealized: GainsAi | None = src("nested")
    weight: str | None = src("computed")


class AiHoldingsOut(AiOut):
    as_of: date = src("computed")
    currency: str = src("household.base_currency")
    items: list[HoldingAi] = src("nested")
    market_value: AiMoney = src("holding.quantity", "price.close")
    cost_basis: AiMoney = src("holding.cost_basis")
    unrealized: GainsAi = src("nested")


class AiPerformanceOut(AiOut):
    range: str = src("request")
    start: date = src("computed", "policy")
    end: date = src("computed")
    currency: str = src("household.base_currency")
    start_value: AiMoney = src("holding.quantity", "price.close", "investment_txn.amount")
    end_value: AiMoney = src("holding.quantity", "price.close")
    inflows: AiMoney = src("investment_txn.amount")
    outflows: AiMoney = src("investment_txn.amount")
    income: AiMoney = src("investment_txn.amount")
    fees: AiMoney = src("investment_txn.fees")
    twr: str = src("computed")
    twr_annualized: str | None = src("computed")
    mwr: str | None = src("computed")
    mwr_annualized: str | None = src("computed")
    realized: GainsAi = src("nested")
    unrealized: GainsAi = src("nested")


# ---------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------


class LoanAi(AiOut):
    id: str = src("loan.id")
    account: str = src("account.nickname", "account.name", "account.type", "policy")
    type: str = src("account.type")
    lender: str | None = src("loan.lender")
    currency: str = src("loan.currency")
    principal_original: AiMoney = src("loan.principal_original")
    balance: AiMoney = src("loan_payment.principal", "balance_snapshot.balance", "computed")
    rate: str = src("loan.rate", "loan_rate_change.new_rate")
    rate_type: str = src("loan.rate_type")
    term_months: int = src("loan.term_months")
    origination_date: date = src("loan.origination_date")
    payment_amount: AiMoney = src("loan.payment_amount")
    escrow_amount: AiMoney | None = src("loan.escrow_amount")
    maturity_date: date = src("computed")
    next_due_date: date | None = src("computed")
    payoff_date: date | None = src("computed")
    remaining_payments: int = src("computed")
    remaining_interest: AiMoney = src("computed")


class AiLoansOut(AiOut):
    items: list[LoanAi] = src("nested")


class ScheduleRowAi(AiOut):
    number: int = src("computed")
    due_date: date = src("computed")
    rate: str = src("loan.rate", "loan_rate_change.new_rate")
    payment: AiMoney = src("loan.payment_amount", "computed")
    principal: AiMoney = src("computed")
    interest: AiMoney = src("computed")
    extra: AiMoney = src("computed", "request")
    escrow: AiMoney = src("loan.escrow_amount")
    balance: AiMoney = src("computed")


class ScheduleTotalsAi(AiOut):
    payments: int = src("computed")
    principal: AiMoney = src("computed")
    interest: AiMoney = src("computed")
    extra: AiMoney = src("computed")
    payoff_date: date | None = src("computed")


class AiLoanScheduleOut(AiOut):
    loan_id: str = src("loan.id")
    basis: str = src("request")
    start_balance: AiMoney = src("loan.principal_original", "loan_payment.principal", "computed")
    rows: list[ScheduleRowAi] = src("nested")
    total_rows: int = src("computed")
    totals: ScheduleTotalsAi = src("nested")


class ScenarioAi(AiOut):
    payoff_date: date | None = src("computed")
    payments: int = src("computed")
    total_interest: AiMoney = src("computed")
    total_extra: AiMoney = src("computed", "request")


class AiSimulationOut(AiOut):
    loan_id: str = src("loan.id")
    as_of: date = src("computed")
    balance: AiMoney = src("loan_payment.principal", "computed")
    extra_monthly: AiMoney | None = src("request")
    extra_once: AiMoney | None = src("request")
    baseline: ScenarioAi = src("nested")
    with_extras: ScenarioAi = src("nested")
    interest_saved: AiMoney = src("computed")
    payments_saved: int = src("computed")


# ---------------------------------------------------------------------------
# Tax
# ---------------------------------------------------------------------------


class TaxCategoryAi(AiOut):
    category: str | None = src("category.name")
    tax_treatment: str | None = src("category.tax_treatment")
    schedule: str | None = src("tax_category_map.schedule")
    line: str | None = src("tax_category_map.line")
    total: AiMoney = src("transaction.amount", "transaction_split.amount")


class TaxBoxAi(AiOut):
    box: str = src("computed")
    label: str = src("computed")
    amount: AiMoney = src("tax_form_data.boxes")


class TaxFormAi(AiOut):
    form_type: str = src("tax_form_data.form_type")
    count: int = src("computed")
    boxes: list[TaxBoxAi] = src("nested")


class TaxChecklistAi(AiOut):
    form_type: str = src("tax_checklist_item.expected_form")
    issuer: str | None = src("tax_checklist_item.issuer", "policy")
    status: str = src("tax_checklist_item.status")


class AiTaxYearSummaryOut(AiOut):
    year: int = src("tax_year.year")
    jurisdiction: str = src("tax_year.jurisdiction")
    filing_status: str | None = src("tax_year.filing_status")
    state: str = src("tax_year.state")
    currency: str = src("household.base_currency")
    partial: bool = src("computed")
    by_treatment: dict[str, AiMoney] = src("category.tax_treatment", "transaction.amount")
    categories: list[TaxCategoryAi] = src("nested")
    forms: list[TaxFormAi] = src("nested")
    checklist: list[TaxChecklistAi] = src("nested")
    expected: int = src("computed")
    received: int = src("computed")
    missing: int = src("computed")


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------


class AnomalyAi(AiOut):
    kind: Literal["large", "new_payee"] = src("computed")
    posted_date: date = src("transaction.posted_date")
    amount: AiMoney = src("transaction.amount", "transaction.currency")
    payee: str | None = src("payee.name", "transaction.raw_payee")
    category: str | None = src("category.name")
    account: str = src("account.nickname", "account.name", "account.type", "policy")
    typical: AiMoney | None = src("transaction.amount", "computed")
    reason: str = src("computed")


class AiAnomaliesOut(AiOut):
    date_from: date = src("request", "policy")
    date_to: date = src("request")
    items: list[AnomalyAi] = src("nested")


#: Every top-level response model (the routes' ``response_model``s).
RESPONSE_MODELS: Final[tuple[type[AiOut], ...]] = (
    AiDataPolicyOut,
    AiAccountsOut,
    AiTransactionsOut,
    AiFlowOut,
    AiTrendsOut,
    AiOverviewOut,
    AiBudgetStatusOut,
    AiGoalsOut,
    AiRecurringOut,
    AiNetWorthHistoryOut,
    AiAllocationOut,
    AiHoldingsOut,
    AiPerformanceOut,
    AiLoansOut,
    AiLoanScheduleOut,
    AiSimulationOut,
    AiTaxYearSummaryOut,
    AiAnomaliesOut,
)

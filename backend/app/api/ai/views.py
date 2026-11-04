# SPDX-License-Identifier: AGPL-3.0-or-later
"""The read-only views behind ``/api/v1/ai/v1`` (AI_ACCESS.md "Tools", M1, M2, M6).

Each view calls the same domain services the web app uses (so visibility,
splits, transfers, FX and amortisation rules are shared), then copies the
allowlisted facts into the models of ``app.api.ai.schemas``, applying the
client's policy on the way:

* every free-text value (payees, memos, notes, names people gave things,
  lenders, issuers) goes through the request's scrubber
  (``app.domain.ai.service.scrubber_for``);
* money is rounded (``ClientPolicy.money``);
* account labels are the scrubbed nickname (or name), or "<Type> A" when
  nicknames are hidden or in Aggregates mode;
* merchant location is cut from payees when hidden;
* periods are bounded (:data:`MAX_PERIOD_DAYS`) and clamped to the client's
  date-range cap (a period entirely before the cap is ``422
  outside-date-range``);
* in Aggregates mode, transaction-level views (transactions, recurring,
  holdings, loans, anomalies, spending by payee) answer ``403
  not-in-aggregates-mode``.
"""

from __future__ import annotations

import re
import statistics
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.ai import schemas as s
from app.auth.principal import Principal
from app.core import clock
from app.core.errors import ProblemError
from app.core.money import Money
from app.core.scrubber import PLACEHOLDERS, Scrubber, normalise
from app.domain.accounts import balances
from app.domain.accounts.models import Account, Institution
from app.domain.accounts.visibility import visible_account_filter
from app.domain.ai.models import AiClient
from app.domain.ai.policy import ClientPolicy, account_labels, split_merchant_location
from app.domain.budgets import service as budgets_service
from app.domain.budgets.schemas import BudgetTotals
from app.domain.goals import service as goals_service
from app.domain.investments import holdings as holdings_view
from app.domain.investments import performance as performance_view
from app.domain.investments.schemas import GainsOut
from app.domain.loans import service as loans_service
from app.domain.loans.schemas import (
    OneOffExtraIn,
    RecurringExtraIn,
    ScenarioOut,
    SimulateIn,
)
from app.domain.networth import service as networth_service
from app.domain.networth.schemas import Granularity
from app.domain.recurring import service as recurring_service
from app.domain.reports import service as reports_service
from app.domain.reports.dates import add_months, month_end, month_start
from app.domain.reports.schemas import Flow, GroupBy, ReportItem, TrendRow
from app.domain.tax import rollup as tax_rollup
from app.domain.tax import service as tax_service
from app.domain.transactions import repo as txn_repo
from app.domain.transactions import service as txn_service
from app.domain.transactions.models import Category, Payee, Tag, Transaction

#: Longest period any view accepts (about five years), M6.
MAX_PERIOD_DAYS: Final = 1830
MAX_TRANSACTIONS: Final = 200
MAX_SCHEDULE_ROWS: Final = 600
MAX_GOALS: Final = 100
MAX_BUDGETS: Final = 50
MAX_LOANS: Final = 100
MAX_ANOMALIES: Final = 100

PeriodName = Literal[
    "this_month",
    "last_month",
    "last_30_days",
    "last_90_days",
    "last_12_months",
    "this_year",
    "last_year",
    "ytd",
]
_YEAR_RE: Final = re.compile(r"\d{4}")
_MONTH_RE: Final = re.compile(r"(\d{4})-(\d{2})")


def outside_range(detail: str) -> ProblemError:
    return ProblemError(422, "outside-date-range", "Outside the allowed date range", detail)


def not_in_aggregates() -> ProblemError:
    return ProblemError(
        403,
        "not-in-aggregates-mode",
        "Not available in Aggregates mode",
        "This client sees totals and trends only; transaction-level views are off.",
    )


def invalid_period(detail: str) -> ProblemError:
    return ProblemError(422, "invalid-period", "Invalid period", detail)


@dataclass
class AiContext:
    """One AI request: the caller, its client's policy and scrubber."""

    db: Session
    principal: Principal
    client: AiClient
    policy: ClientPolicy
    scrubber: Scrubber
    today: date = field(default_factory=lambda: clock.utcnow().date())
    _accounts: dict[uuid.UUID, Account] | None = None
    _labels: dict[uuid.UUID, str] | None = None

    @property
    def household_id(self) -> uuid.UUID:
        return self.client.household_id

    # -- values ------------------------------------------------------------

    def text(self, value: str | None) -> str | None:
        return self.scrubber.scrub(value)

    def payee(self, value: str | None) -> str | None:
        return self.policy.payee_text(self.scrubber, value)

    def money(self, value: Money) -> s.AiMoney:
        rounded = self.policy.money(value.amount, value.currency)
        return s.AiMoney(amount=rounded.amount, currency=rounded.currency)

    def money_of(self, amount: Decimal, currency: str) -> s.AiMoney:
        return self.money(Money.of(amount, currency))

    def opt_money(self, value: Money | None) -> s.AiMoney | None:
        return None if value is None else self.money(value)

    def require_detail(self) -> None:
        if self.policy.aggregates:
            raise not_in_aggregates()

    # -- accounts ----------------------------------------------------------

    def accounts(self) -> dict[uuid.UUID, Account]:
        if self._accounts is None:
            rows = self.db.execute(
                select(Account)
                .where(
                    Account.household_id == self.household_id,
                    visible_account_filter(self.principal, Account.id),
                )
                .order_by(Account.created_at, Account.id)
            ).scalars()
            self._accounts = {a.id: a for a in rows}
        return self._accounts

    def label(self, account_id: uuid.UUID | None) -> str:
        if self._labels is None:
            accounts = self.accounts()
            if self.policy.labels_hidden:
                hidden = account_labels((a.id, a.type.value) for a in accounts.values())
                self._labels = {k: v for k, v in hidden.items() if isinstance(k, uuid.UUID)}
            else:
                self._labels = {
                    a.id: self.text(a.nickname or a.name) or "Account" for a in accounts.values()
                }
        if account_id is None:
            return "Account"
        return self._labels.get(account_id, "Account")

    # -- periods -----------------------------------------------------------

    def period(
        self,
        period: str | None,
        date_from: date | None,
        date_to: date | None,
        *,
        default: str = "this_month",
    ) -> tuple[date, date]:
        """A bounded, capped ``(start, end)`` (module doc)."""
        start, end = resolve_period(period or default, self.today)
        if date_from is not None:
            start = date_from
        if date_to is not None:
            end = date_to
        if start > end:
            raise invalid_period("The start is after the end.")
        if (end - start).days + 1 > MAX_PERIOD_DAYS:
            raise invalid_period(f"A period is at most {MAX_PERIOD_DAYS} days.")
        return self.clamp(start, end)

    def clamp(self, start: date, end: date) -> tuple[date, date]:
        first = self.policy.earliest(self.today)
        if first is None:
            return start, end
        if end < first:
            raise outside_range(f"This client may not look before {first.isoformat()}.")
        return max(start, first), end


def resolve_period(name: str, today: date) -> tuple[date, date]:
    """A named period, a year ``YYYY`` or a month ``YYYY-MM``."""
    if name == "this_month":
        return month_start(today), month_end(today)
    if name == "last_month":
        last = add_months(month_start(today), -1)
        return last, month_end(last)
    if name == "last_30_days":
        return today - timedelta(days=29), today
    if name == "last_90_days":
        return today - timedelta(days=89), today
    if name == "last_12_months":
        return add_months(month_start(today), -11), month_end(today)
    if name in ("this_year", "ytd"):
        return date(today.year, 1, 1), today if name == "ytd" else date(today.year, 12, 31)
    if name == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if _YEAR_RE.fullmatch(name):
        year = int(name)
        if not 1900 <= year <= 2200:
            raise invalid_period("Years are 1900-2200.")
        return date(year, 1, 1), date(year, 12, 31)
    month = _MONTH_RE.fullmatch(name)
    if month is not None:
        try:
            first = date(int(month.group(1)), int(month.group(2)), 1)
        except ValueError as exc:
            raise invalid_period("Use a month as YYYY-MM.") from exc
        return first, month_end(first)
    raise invalid_period(
        "Use this_month, last_month, last_30_days, last_90_days, last_12_months, "
        "this_year, last_year, ytd, a year (YYYY) or a month (YYYY-MM)."
    )


# ---------------------------------------------------------------------------
# Data policy
# ---------------------------------------------------------------------------

NEVER_EXPOSED: Final = (
    "government identifiers (SSN, ITIN, EIN, passport, driver's licence)",
    "account, card, routing, IBAN, loan and policy numbers, including the last 4 digits",
    "household members' names, usernames, e-mail addresses, phone numbers, dates of birth",
    "home, mailing and property addresses (manual asset descriptions)",
    "documents, page previews, extracted text and OCR output",
    "credentials, sessions, tokens and settings",
    "tax form identity boxes (SSN/EIN, employer and payer addresses)",
)
EXPOSED: Final = (
    "accounts by type, nickname, institution, currency and balance",
    "transactions: date, amount, payee, merchant city/state, category, tags, scrubbed memo/notes",
    "spending, income, trends, cash flow, budgets, goals, recurring bills",
    "net worth history, allocation, holdings by ticker, performance",
    "loans: lender, balance, rate, term, schedule, payoff simulation",
    "tax year: totals by category and schedule, form checklist, amounts by box label",
)


def data_policy(ctx: AiContext, *, rate_limits: Iterable[str]) -> s.AiDataPolicyOut:
    p = ctx.policy
    return s.AiDataPolicyOut(
        client_name=ctx.text(ctx.client.name) or "AI client",
        mode=p.mode.value,
        rounding=p.rounding,
        hide_nicknames=p.hide_nicknames,
        hide_merchant_location=p.hide_merchant_location,
        date_range_cap_days=p.date_range_cap,
        earliest_date=p.earliest(ctx.today),
        custom_denylist_entries=len(p.custom_denylist),
        placeholders=list(PLACEHOLDERS),
        never_exposed=list(NEVER_EXPOSED),
        exposed=list(EXPOSED),
        rate_limits=list(rate_limits),
    )


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def accounts(ctx: AiContext) -> s.AiAccountsOut:
    rows = list(ctx.accounts().values())
    amounts = balances.balances_as_of(ctx.db, rows, ctx.today)
    institutions = {
        i.id: i.name
        for i in ctx.db.execute(
            select(Institution).where(Institution.household_id == ctx.household_id)
        ).scalars()
    }
    if ctx.policy.aggregates:
        grouped: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        counts: dict[str, int] = defaultdict(int)
        for a in rows:
            counts[a.type.value] += 1
            grouped[a.type.value][a.currency] += amounts[a.id]
        by_type = [
            s.AccountTypeTotal(
                type=t,
                accounts=counts[t],
                balance=[ctx.money_of(v, c) for c, v in sorted(grouped[t].items())],
            )
            for t in sorted(grouped)
        ]
        return s.AiAccountsOut(as_of=ctx.today, mode="aggregates", items=[], by_type=by_type)
    items = [
        s.AccountAi(
            id=str(a.id),
            label=ctx.label(a.id),
            type=a.type.value,
            asset_kind=a.asset_kind.value if a.asset_kind is not None else None,
            institution=ctx.text(institutions.get(a.institution_id))
            if a.institution_id is not None
            else None,
            currency=a.currency,
            balance=ctx.money_of(amounts[a.id], a.currency),
            is_asset=a.is_asset,
            closed=a.closed_on is not None,
        )
        for a in rows
    ]
    return s.AiAccountsOut(as_of=ctx.today, mode="detail", items=items, by_type=[])


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


#: Most names compared by their scrubbed form for one filter (payees, categories).
MAX_FILTER_SCAN: Final = 20_000
_PLACEHOLDER_ANY_CASE: Final = re.compile("|".join(re.escape(p) for p in PLACEHOLDERS), re.I)


def _ids_by_name(ctx: AiContext, model: type[Category] | type[Payee], name: str) -> list[uuid.UUID]:
    """Ids whose name **as the client sees it** (scrubbed) equals ``name``.

    Canonical names only, case-insensitive, never a pattern (AI_ACCESS.md
    "Tools"). The filter must not confirm a guess the scrubber hides
    (P6B-SR-R1-03): a value the request's scrubber would change ("Gifts for
    Jane", "Oakwood Clinic") matches nothing, exactly like a miss, and a name
    is matched only when its scrubbed form equals the value. A value carrying
    a placeholder ("Gifts for [MEMBER]") is compared with the scrubbed names,
    so the client can filter by what it was shown and learns nothing more.

    A payee is compared in the form the client is shown (``ctx.payee``): with
    ``hide_merchant_location`` its city and state are cut, so a guessed
    location ("ACME HARDWARE SPRINGFIELD IL") answers like a miss and the
    shown name ("ACME HARDWARE") filters (P6B-SR-R2-03).
    """
    shown: Callable[[str | None], str | None] = ctx.payee if model is Payee else ctx.text
    wanted = _PLACEHOLDER_ANY_CASE.sub(
        lambda m: m.group(0).upper(), normalise(" ".join(name.split()))
    )
    scrubbed = shown(wanted) or ""
    if scrubbed != wanted:
        return []
    key = scrubbed.casefold()
    stmt = select(model.id, model.name).where(model.household_id == ctx.household_id)
    cut_location = model is Payee and ctx.policy.hide_merchant_location
    if not cut_location and not any(p in scrubbed for p in PLACEHOLDERS):
        # No placeholder: only the raw name can scrub to it (the scrubber keeps
        # text without identity unchanged up to normalisation). Not so when a
        # location is cut: "ACME HARDWARE SPRINGFIELD IL" is shown as
        # "ACME HARDWARE".
        stmt = stmt.where(func.lower(model.name) == name.strip().lower())
    rows = ctx.db.execute(stmt.order_by(model.id).limit(MAX_FILTER_SCAN)).all()
    return [row[0] for row in rows if " ".join((shown(row[1]) or "").split()).casefold() == key]


def _names(
    db: Session, model: type[Category] | type[Payee] | type[Tag], ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, str]:
    wanted = sorted(set(ids), key=lambda u: u.bytes)
    if not wanted:
        return {}
    rows = db.execute(select(model.id, model.name).where(model.id.in_(wanted))).all()
    return {row[0]: row[1] for row in rows}


def transactions(
    ctx: AiContext,
    *,
    period: str | None,
    date_from: date | None,
    date_to: date | None,
    account_id: uuid.UUID | None,
    category: str | None,
    payee: str | None,
    min_amount: Decimal | None,
    max_amount: Decimal | None,
    limit: int,
    cursor: str | None,
) -> s.AiTransactionsOut:
    ctx.require_detail()
    start, end = ctx.period(period, date_from, date_to, default="last_30_days")
    extra = []
    if min_amount is not None:
        extra.append(func.abs(Transaction.amount) >= min_amount)
    if max_amount is not None:
        extra.append(func.abs(Transaction.amount) <= max_amount)
    category_ids: list[uuid.UUID] = []
    payee_ids: list[uuid.UUID] = []
    empty = s.AiTransactionsOut(date_from=start, date_to=end, items=[], next_cursor=None)
    if category:
        category_ids = _ids_by_name(ctx, Category, category)
        if not category_ids:
            return empty
    if payee:
        payee_ids = _ids_by_name(ctx, Payee, payee)
        if not payee_ids:
            return empty
    filters = txn_repo.TransactionFilters(
        account_ids=(account_id,) if account_id is not None else (),
        date_from=start,
        date_to=end,
        category_ids=category_ids,
        payee_ids=payee_ids,
        extra=tuple(extra),
    )
    page = txn_service.list_transactions(
        ctx.db, ctx.principal, filters, cursor=cursor, limit=min(limit, MAX_TRANSACTIONS)
    )
    rows = page.items
    payees = _names(ctx.db, Payee, (r.payee_id for r in rows if r.payee_id is not None))
    category_refs = [r.category_id for r in rows if r.category_id is not None] + [
        sp.category_id for r in rows for sp in r.splits if sp.category_id is not None
    ]
    categories = _names(ctx.db, Category, category_refs)
    tags = _names(ctx.db, Tag, (t for r in rows for t in r.tag_ids))
    items: list[s.TransactionAi] = []
    for r in rows:
        payee_text = payees.get(r.payee_id) if r.payee_id is not None else r.raw_payee
        location = (
            split_merchant_location(ctx.text(r.raw_payee) or "")
            if r.raw_payee and not ctx.policy.hide_merchant_location
            else None
        )
        items.append(
            s.TransactionAi(
                id=str(r.id),
                posted_date=r.posted_date,
                amount=ctx.money(r.amount),
                account_id=str(r.account_id),
                account=ctx.label(r.account_id),
                payee=ctx.payee(payee_text),
                merchant_city=location.city if location else None,
                merchant_state=location.state if location else None,
                category=ctx.text(categories.get(r.category_id))
                if r.category_id is not None
                else None,
                tags=sorted(ctx.text(tags[t]) or "" for t in r.tag_ids if t in tags),
                memo=ctx.text(r.memo),
                notes=ctx.text(r.notes),
                status=r.status.value,
                transfer=r.transfer is not None,
                splits=[
                    s.SplitAi(
                        category=ctx.text(categories.get(sp.category_id))
                        if sp.category_id is not None
                        else None,
                        amount=ctx.money(sp.amount),
                        memo=ctx.text(sp.memo),
                    )
                    for sp in r.splits
                ],
            )
        )
    return s.AiTransactionsOut(
        date_from=start, date_to=end, items=items, next_cursor=page.next_cursor
    )


# ---------------------------------------------------------------------------
# Spending, income, trends, overview
# ---------------------------------------------------------------------------

AiGroupBy = Literal["category", "payee", "month", "account", "account_type"]


def flow(
    ctx: AiContext,
    *,
    flow: Flow,
    period: str | None,
    date_from: date | None,
    date_to: date | None,
    group_by: AiGroupBy,
    depth: int,
) -> s.AiFlowOut:
    start, end = ctx.period(period, date_from, date_to)
    if ctx.policy.aggregates and group_by == "payee":
        raise not_in_aggregates()
    if ctx.policy.aggregates and group_by == "account":
        group_by = "account_type"
    service_group = GroupBy.ACCOUNT if group_by == "account_type" else GroupBy(group_by)
    report = reports_service.flow_report(
        ctx.db,
        ctx.principal,
        flow=flow,
        date_from=start,
        date_to=end,
        group_by=service_group,
        depth=depth,
    )
    items = _flow_items(ctx, report.items, group_by)
    return s.AiFlowOut(
        flow=flow.value,
        group_by=group_by,
        currency=report.currency,
        date_from=report.date_from,
        date_to=report.date_to,
        items=items,
        total=ctx.money(report.total),
    )


def _flow_items(ctx: AiContext, items: list[ReportItem], group_by: str) -> list[s.FlowItemAi]:
    if group_by == "account_type":
        totals: dict[str, Decimal] = defaultdict(Decimal)
        currency = items[0].amount.currency if items else ""
        accounts = ctx.accounts()
        for item in items:
            account = accounts.get(uuid.UUID(item.key)) if item.key else None
            kind = account.type.value if account is not None else "other"
            totals[kind] += item.amount.as_decimal()
        return [
            s.FlowItemAi(name=k, amount=ctx.money_of(v, currency))
            for k, v in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
    out: list[s.FlowItemAi] = []
    for item in items:
        if group_by == "account":
            name: str | None = ctx.label(uuid.UUID(item.key)) if item.key else None
        elif group_by == "month":
            name = item.key
        elif group_by == "payee":
            name = ctx.payee(item.name)
        else:
            name = ctx.text(item.name)
        out.append(s.FlowItemAi(name=name, amount=ctx.money(item.amount)))
    return out


def trends(ctx: AiContext, *, flow: Flow, month: str | None, months: int) -> s.AiTrendsOut:
    current = month_start(resolve_period(month, ctx.today)[0]) if month else month_start(ctx.today)
    first = ctx.policy.earliest(ctx.today)
    if first is not None:
        if month_end(current) < first:
            raise outside_range(f"This client may not look before {first.isoformat()}.")
        # Fewer months rather than any month before the cap.
        while months > 1 and add_months(current, -(months - 1)) < month_start(first):
            months -= 1
    report = reports_service.trends(ctx.db, ctx.principal, flow=flow, month=current, months=months)
    previous_ok = first is None or add_months(current, -1) >= month_start(first)
    year_ago_ok = first is None or add_months(current, -12) >= month_start(first)

    def row(r: TrendRow) -> s.TrendRowAi:
        return s.TrendRowAi(
            name=ctx.text(r.name),
            amount=ctx.money(r.amount),
            previous=ctx.money(r.previous) if previous_ok else None,
            year_ago=ctx.money(r.year_ago) if year_ago_ok else None,
            mom_percent=r.mom_percent if previous_ok else None,
            yoy_percent=r.yoy_percent if year_ago_ok else None,
        )

    return s.AiTrendsOut(
        flow=flow.value,
        month=report.month,
        months=months,
        currency=report.currency,
        series=[s.TrendPointAi(month=p.month, amount=ctx.money(p.amount)) for p in report.series],
        total=row(report.total),
        categories=[row(c) for c in report.categories],
    )


def overview(
    ctx: AiContext, *, period: str | None, date_from: date | None, date_to: date | None
) -> s.AiOverviewOut:
    start, end = ctx.period(period, date_from, date_to, default="last_12_months")
    latest = networth_service.latest(ctx.db, ctx.principal)
    cash = reports_service.cashflow(ctx.db, ctx.principal, date_from=start, date_to=end)
    inflow = cash.inflow.as_decimal()
    rate = str((cash.net.as_decimal() / inflow * 100).quantize(Decimal("0.01"))) if inflow else None
    return s.AiOverviewOut(
        as_of=latest.as_of,
        currency=latest.currency,
        date_from=cash.date_from,
        date_to=cash.date_to,
        assets=ctx.money(latest.assets),
        liabilities=ctx.money(latest.liabilities),
        net_worth=ctx.money(latest.net),
        inflow=ctx.money(cash.inflow),
        outflow=ctx.money(cash.outflow),
        net_cash_flow=ctx.money(cash.net),
        savings_rate=rate,
        months=[
            s.CashflowMonthAi(
                month=m.month,
                inflow=ctx.money(m.inflow),
                outflow=ctx.money(m.outflow),
                net=ctx.money(m.net),
            )
            for m in cash.months
        ],
    )


# ---------------------------------------------------------------------------
# Budgets, goals, recurring
# ---------------------------------------------------------------------------


def _hidden_name(prefix: str, index: int) -> str:
    return f"{prefix} {account_labels([(index, 'x')])[index].split(' ', 1)[1]}"


def budget_status(ctx: AiContext, *, on: date | None) -> s.AiBudgetStatusOut:
    day = on or ctx.today
    ctx.clamp(day, day)
    page = budgets_service.list_budgets(ctx.db, ctx.principal, cursor=None, limit=MAX_BUDGETS)
    out: list[s.BudgetAi] = []
    for index, budget in enumerate(page.items):
        report = budgets_service.budget_report(
            ctx.db, ctx.principal, budget.id, period=day.isoformat()
        )

        def totals(t: BudgetTotals) -> s.BudgetTotalsAi:
            return s.BudgetTotalsAi(
                budgeted=ctx.money(t.budgeted),
                actual=ctx.money(t.actual),
                remaining=ctx.money(t.remaining),
                forecast=ctx.money(t.forecast),
            )

        out.append(
            s.BudgetAi(
                name=_hidden_name("Budget", index)
                if ctx.policy.aggregates
                else ctx.text(budget.name) or "Budget",
                period=budget.period.value,
                period_start=report.period.start,
                period_end=report.period.end,
                currency=report.currency,
                lines=[
                    s.BudgetLineAi(
                        category=ctx.text(line.name),
                        kind=line.kind.value if line.kind is not None else None,
                        budgeted=ctx.money(line.budgeted),
                        available=ctx.money(line.available),
                        actual=ctx.money(line.actual),
                        remaining=ctx.money(line.remaining),
                        forecast=ctx.money(line.forecast),
                    )
                    for line in report.lines
                ],
                unbudgeted=ctx.money(report.unbudgeted_total),
                expense=totals(report.expense_totals),
                income=totals(report.income_totals),
            )
        )
    return s.AiBudgetStatusOut(as_of=day, budgets=out)


def goals(ctx: AiContext) -> s.AiGoalsOut:
    page = goals_service.list_goals(ctx.db, ctx.principal, cursor=None, limit=MAX_GOALS)
    items: list[s.GoalAi] = []
    for index, g in enumerate(page.items):
        progress = g.progress
        items.append(
            s.GoalAi(
                name=_hidden_name("Goal", index)
                if ctx.policy.aggregates
                else ctx.text(g.name) or "Goal",
                target=ctx.money(g.target),
                target_date=g.target_date,
                contributed=ctx.money(progress.contributed),
                remaining=ctx.money(progress.remaining),
                percent=progress.percent,
                projected_completion=progress.projected_completion,
                on_track=progress.on_track,
                achieved=g.achieved_at is not None,
                archived=g.archived,
            )
        )
    return s.AiGoalsOut(items=items)


def recurring(ctx: AiContext) -> s.AiRecurringOut:
    ctx.require_detail()
    listing = recurring_service.list_schedules(ctx.db, ctx.principal, active=True)
    payees = _names(ctx.db, Payee, (r.payee_id for r in listing.items if r.payee_id))
    categories = _names(ctx.db, Category, (r.category_id for r in listing.items if r.category_id))
    return s.AiRecurringOut(
        items=[
            s.RecurringAi(
                name=ctx.payee(r.name) or "Recurring",
                payee=ctx.payee(payees.get(r.payee_id)) if r.payee_id else None,
                category=ctx.text(categories.get(r.category_id)) if r.category_id else None,
                account=ctx.label(r.account_id),
                cadence=r.cadence.value,
                interval_count=r.interval_count,
                next_due=r.next_due,
                amount_min=ctx.money(r.amount_min),
                amount_max=ctx.money(r.amount_max),
                origin=r.origin.value,
                active=r.active,
                last_seen_on=r.last_seen_on,
            )
            for r in listing.items
        ]
    )


# ---------------------------------------------------------------------------
# Net worth and investments
# ---------------------------------------------------------------------------

Range = Literal["1m", "3m", "6m", "ytd", "1y", "3y", "5y", "max"]
GranularityName = Literal["day", "week", "month"]


def net_worth_history(
    ctx: AiContext, *, range_: Range, granularity: GranularityName | None
) -> s.AiNetWorthHistoryOut:
    gran = Granularity(granularity) if granularity is not None else None
    history = networth_service.history(ctx.db, ctx.principal, range_=range_, granularity=gran)
    first = ctx.policy.earliest(ctx.today)
    points = [p for p in history.points if first is None or p.as_of >= first]
    start = max(history.date_from, first) if first is not None else history.date_from
    change = None
    if len(points) >= 2:
        change = ctx.money_of(
            points[-1].net.as_decimal() - points[0].net.as_decimal(), history.currency
        )
    return s.AiNetWorthHistoryOut(
        currency=history.currency,
        range=range_,
        granularity=history.granularity.value,
        date_from=start,
        date_to=history.date_to,
        points=[
            s.NetWorthPointAi(
                as_of=p.as_of,
                assets=ctx.money(p.assets),
                liabilities=ctx.money(p.liabilities),
                net=ctx.money(p.net),
            )
            for p in points
        ],
        change=change,
    )


def _gains(ctx: AiContext, g: GainsOut) -> s.GainsAi:
    return s.GainsAi(
        short_term=ctx.money(g.short_term),
        long_term=ctx.money(g.long_term),
        total=ctx.money(g.total),
    )


def allocation(ctx: AiContext) -> s.AiAllocationOut:
    view = holdings_view.holdings(ctx.db, ctx.principal)
    by: dict[str, list[s.AllocationAi]] = {}
    for dimension, rows in view.allocation.items():
        if dimension == "account":
            if ctx.policy.aggregates:
                totals: dict[str, Decimal] = defaultdict(Decimal)
                weights: dict[str, Decimal] = defaultdict(Decimal)
                currency = view.base_currency
                for r in rows:
                    acct = ctx.accounts().get(uuid.UUID(r.key))
                    kind = acct.type.value if acct is not None else "other"
                    totals[kind] += r.value.as_decimal()
                    weights[kind] += Decimal(r.weight)
                by["account_type"] = [
                    s.AllocationAi(key=k, value=ctx.money_of(v, currency), weight=str(weights[k]))
                    for k, v in sorted(totals.items())
                ]
                continue
            by["account"] = [
                s.AllocationAi(
                    key=ctx.label(uuid.UUID(r.key)), value=ctx.money(r.value), weight=r.weight
                )
                for r in rows
            ]
            continue
        # Sector (and any other text key) is free text a member typed or a
        # statement supplied: scrubbed, and rows that scrub to the same key
        # are merged so a placeholder appears once (P6B-SR-R1-01).
        merged: dict[str, tuple[Decimal, Decimal, str]] = {}
        for r in rows:
            key = ctx.text(r.key) or ""
            value, weight, currency = merged.get(key, (Decimal(0), Decimal(0), r.value.currency))
            merged[key] = (value + r.value.as_decimal(), weight + Decimal(r.weight), currency)
        by[dimension] = [
            s.AllocationAi(key=k, value=ctx.money_of(v, c), weight=str(w))
            for k, (v, w, c) in merged.items()
        ]
    return s.AiAllocationOut(
        as_of=view.as_of,
        currency=view.base_currency,
        market_value=ctx.money(view.market_value),
        by=by,
    )


def holdings(ctx: AiContext, *, account_id: uuid.UUID | None) -> s.AiHoldingsOut:
    ctx.require_detail()
    view = holdings_view.holdings(ctx.db, ctx.principal, account_id=account_id)
    return s.AiHoldingsOut(
        as_of=view.as_of,
        currency=view.base_currency,
        items=[
            s.HoldingAi(
                account=ctx.label(h.account_id),
                # Symbol, name and sector are free text (classification;
                # P6B-SR-R1-01): a member or an OFX SECNAME can put a name or
                # a number in them.
                symbol=ctx.text(h.security.symbol) or "",
                name=ctx.text(h.security.name) or "",
                asset_class=h.security.asset_class.value,
                sector=ctx.text(h.security.sector),
                currency=h.security.currency,
                quantity=h.quantity,
                cost_basis=ctx.money(h.cost_basis),
                price=h.price.close if h.price is not None else None,
                price_date=h.price.price_date if h.price is not None else None,
                market_value=ctx.opt_money(h.market_value),
                unrealized=_gains(ctx, h.unrealized) if h.unrealized is not None else None,
                weight=h.weight,
            )
            for h in view.holdings
        ],
        market_value=ctx.money(view.market_value),
        cost_basis=ctx.money(view.cost_basis),
        unrealized=_gains(ctx, view.unrealized),
    )


def performance(
    ctx: AiContext, *, range_: Range, account_id: uuid.UUID | None
) -> s.AiPerformanceOut:
    first = ctx.policy.earliest(ctx.today)
    if first is not None:
        start = performance_view.range_start(range_, ctx.today)
        if start is None or start < first:
            raise outside_range(
                f"This client may not look before {first.isoformat()}; choose a shorter range."
            )
    out = performance_view.performance(ctx.db, ctx.principal, range_=range_, account_id=account_id)
    return s.AiPerformanceOut(
        range=out.range,
        start=out.start,
        end=out.end,
        currency=out.base_currency,
        start_value=ctx.money(out.start_value),
        end_value=ctx.money(out.end_value),
        inflows=ctx.money(out.inflows),
        outflows=ctx.money(out.outflows),
        income=ctx.money(out.income),
        fees=ctx.money(out.fees),
        twr=out.twr,
        twr_annualized=out.twr_annualized,
        mwr=out.mwr,
        mwr_annualized=out.mwr_annualized,
        realized=_gains(ctx, out.realized),
        unrealized=_gains(ctx, out.unrealized),
    )


# ---------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------


def loans(ctx: AiContext) -> s.AiLoansOut:
    ctx.require_detail()
    page = loans_service.list_loans(ctx.db, ctx.principal, cursor=None, limit=MAX_LOANS)
    accounts = ctx.accounts()
    items: list[s.LoanAi] = []
    for loan in page.items:
        account = accounts.get(loan.account_id)
        p = loan.projection
        items.append(
            s.LoanAi(
                id=str(loan.id),
                account=ctx.label(loan.account_id),
                type=account.type.value if account is not None else "loan",
                lender=ctx.text(loan.lender),
                currency=loan.principal_original.currency,
                principal_original=ctx.money(loan.principal_original),
                balance=ctx.money(p.balance),
                rate=p.rate,
                rate_type=loan.rate_type.value,
                term_months=loan.term_months,
                origination_date=loan.origination_date,
                payment_amount=ctx.money(loan.payment_amount),
                escrow_amount=ctx.opt_money(loan.escrow_amount),
                maturity_date=loan.maturity_date,
                next_due_date=p.next_due_date,
                payoff_date=p.payoff_date,
                remaining_payments=p.remaining_payments,
                remaining_interest=ctx.money(p.remaining_interest),
            )
        )
    return s.AiLoansOut(items=items)


def loan_schedule(
    ctx: AiContext,
    loan_id: uuid.UUID,
    *,
    basis: Literal["original", "projected"],
    offset: int,
    limit: int,
) -> s.AiLoanScheduleOut:
    ctx.require_detail()
    plan = loans_service.get_schedule(ctx.db, ctx.principal, loan_id, basis=basis)
    rows = plan.rows[offset : offset + min(limit, MAX_SCHEDULE_ROWS)]
    t = plan.totals
    return s.AiLoanScheduleOut(
        loan_id=str(plan.loan_id),
        basis=plan.basis,
        start_balance=ctx.money(plan.start_balance),
        rows=[
            s.ScheduleRowAi(
                number=r.number,
                due_date=r.due_date,
                rate=r.rate,
                payment=ctx.money(r.payment),
                principal=ctx.money(r.principal),
                interest=ctx.money(r.interest),
                extra=ctx.money(r.extra),
                escrow=ctx.money(r.escrow),
                balance=ctx.money(r.balance),
            )
            for r in rows
        ],
        total_rows=len(plan.rows),
        totals=s.ScheduleTotalsAi(
            payments=t.payments,
            principal=ctx.money(t.principal),
            interest=ctx.money(t.interest),
            extra=ctx.money(t.extra),
            payoff_date=t.payoff_date,
        ),
    )


def _scenario(ctx: AiContext, sc: ScenarioOut) -> s.ScenarioAi:
    return s.ScenarioAi(
        payoff_date=sc.payoff_date,
        payments=sc.payments,
        total_interest=ctx.money(sc.total_interest),
        total_extra=ctx.money(sc.total_extra),
    )


def simulate_loan(
    ctx: AiContext,
    loan_id: uuid.UUID,
    *,
    extra_monthly: Decimal | None,
    extra_once: Decimal | None,
    extra_once_on: date | None,
) -> s.AiSimulationOut:
    ctx.require_detail()
    loan = loans_service.get_loan(ctx.db, ctx.principal, loan_id)
    currency = loan.principal_original.currency
    one_off = []
    if extra_once is not None:
        one_off.append(
            OneOffExtraIn.model_validate(
                {
                    "on": (extra_once_on or ctx.today).isoformat(),
                    "amount": {"amount": str(extra_once), "currency": currency},
                }
            )
        )
    recurring_extra = (
        RecurringExtraIn.model_validate(
            {"amount": {"amount": str(extra_monthly), "currency": currency}}
        )
        if extra_monthly is not None
        else None
    )
    data = SimulateIn(one_off=one_off, recurring=recurring_extra, include_schedule=False)
    out = loans_service.simulate(ctx.db, ctx.principal, loan_id, data)
    return s.AiSimulationOut(
        loan_id=str(out.loan_id),
        as_of=out.as_of,
        balance=ctx.money(out.balance),
        extra_monthly=ctx.money_of(extra_monthly, currency) if extra_monthly is not None else None,
        extra_once=ctx.money_of(extra_once, currency) if extra_once is not None else None,
        baseline=_scenario(ctx, out.baseline),
        with_extras=_scenario(ctx, out.with_extras),
        interest_saved=ctx.money(out.interest_saved),
        payments_saved=out.payments_saved,
    )


# ---------------------------------------------------------------------------
# Tax
# ---------------------------------------------------------------------------


def tax_year_summary(ctx: AiContext, *, year: int) -> s.AiTaxYearSummaryOut:
    ctx.clamp(date(year, 1, 1), date(year, 12, 31))
    summary = tax_rollup.summary(ctx.db, ctx.principal, year)
    checklist = tax_service.get_checklist(ctx.db, ctx.principal, year)
    row = tax_service.require_year(ctx.db, ctx.principal, year)
    counts = checklist.counts
    return s.AiTaxYearSummaryOut(
        year=summary.year,
        jurisdiction=summary.jurisdiction,
        filing_status=row.filing_status,
        state=row.state.value,
        currency=summary.currency,
        partial=summary.partial,
        by_treatment={k: ctx.money(v) for k, v in sorted(summary.by_treatment.items())},
        categories=[
            s.TaxCategoryAi(
                category=ctx.text(c.category_name),
                tax_treatment=c.tax_treatment.value if c.tax_treatment is not None else None,
                schedule=c.schedule,
                line=c.line,
                total=ctx.money(c.total),
            )
            for c in summary.categories
        ],
        forms=[
            s.TaxFormAi(
                form_type=f.form_type,
                count=f.count,
                boxes=[
                    s.TaxBoxAi(
                        box=b.box,
                        label=b.label,
                        amount=ctx.money_of(Decimal(b.amount), summary.currency),
                    )
                    for b in f.boxes
                ],
            )
            for f in summary.forms
        ],
        checklist=[
            s.TaxChecklistAi(
                form_type=i.expected_form,
                issuer=None if ctx.policy.aggregates else ctx.text(i.issuer),
                status=i.status.value,
            )
            for i in checklist.items
        ],
        expected=counts.expected,
        received=counts.received,
        missing=max(counts.expected - counts.received - counts.not_applicable, 0),
    )


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------

#: How far back "typical" spend per category and "seen before" payees reach.
ANOMALY_HISTORY_DAYS: Final = 180
NEW_PAYEE_HISTORY_DAYS: Final = 365
#: A spend is "large" above this multiple of its category's median.
LARGE_FACTOR: Final = Decimal(3)
MIN_HISTORY: Final = 3
NEW_PAYEE_MIN: Final = Decimal(20)


def anomalies(
    ctx: AiContext, *, period: str | None, date_from: date | None, date_to: date | None
) -> s.AiAnomaliesOut:
    """Unusually large spend for its category, and spend at payees never seen before.

    History before the client's date cap is used to judge "typical" but is
    never returned.
    """
    ctx.require_detail()
    start, end = ctx.period(period, date_from, date_to, default="last_30_days")
    visible = visible_account_filter(ctx.principal, Transaction.account_id)
    base = (
        select(Transaction)
        .where(
            Transaction.household_id == ctx.household_id,
            visible,
            Transaction.amount < 0,
            Transaction.transfer_group_id.is_(None),
        )
        .order_by(Transaction.posted_date, Transaction.id)
    )
    current = list(
        ctx.db.execute(
            base.where(Transaction.posted_date >= start, Transaction.posted_date <= end)
        ).scalars()
    )
    history = list(
        ctx.db.execute(
            base.where(
                Transaction.posted_date < start,
                Transaction.posted_date >= start - timedelta(days=NEW_PAYEE_HISTORY_DAYS),
            )
        ).scalars()
    )
    by_category: dict[uuid.UUID | None, list[Decimal]] = defaultdict(list)
    seen: set[str] = set()
    typical_since = start - timedelta(days=ANOMALY_HISTORY_DAYS)
    for t in history:
        seen.add(_payee_key(t))
        if t.posted_date >= typical_since:
            by_category[t.category_id].append(-t.amount)
    payees = _names(ctx.db, Payee, (t.payee_id for t in current if t.payee_id is not None))
    categories = _names(ctx.db, Category, (t.category_id for t in current if t.category_id))
    found: list[s.AnomalyAi] = []
    for t in current:
        spend = -t.amount
        samples = by_category.get(t.category_id, [])
        median = Decimal(statistics.median(samples)) if len(samples) >= MIN_HISTORY else None
        key = _payee_key(t)
        kind: Literal["large", "new_payee"] | None = None
        reason = ""
        if median is not None and median > 0 and spend > median * LARGE_FACTOR:
            kind, reason = "large", "more than three times the typical spend in its category"
        elif key not in seen and spend >= NEW_PAYEE_MIN:
            kind, reason = "new_payee", "first spend at this payee in a year"
        seen.add(key)
        if kind is None:
            continue
        payee_text = payees.get(t.payee_id) if t.payee_id is not None else t.raw_payee
        found.append(
            s.AnomalyAi(
                kind=kind,
                posted_date=t.posted_date,
                amount=ctx.money_of(t.amount, t.currency),
                payee=ctx.payee(payee_text),
                category=ctx.text(categories.get(t.category_id)) if t.category_id else None,
                account=ctx.label(t.account_id),
                typical=ctx.money_of(median, t.currency) if kind == "large" and median else None,
                reason=reason,
            )
        )
    found.sort(key=lambda a: (a.posted_date, a.kind), reverse=True)
    return s.AiAnomaliesOut(date_from=start, date_to=end, items=found[:MAX_ANOMALIES])


def _payee_key(t: Transaction) -> str:
    if t.payee_id is not None:
        return f"id:{t.payee_id}"
    return "raw:" + " ".join((t.raw_payee or "").lower().split())


Builder = Callable[[], s.AiOut]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MCP tools (AI_ACCESS.md "Tools (first cut)"): read-only, one AI view each.

Every tool:

* validates its arguments here first (types, enums, ranges, lengths; an
  unknown argument is refused by :class:`UnknownArguments` in
  ``app.mcp.server``), then calls exactly one ``GET /api/v1/ai/v1/*`` route
  with the AI client's token, where the API validates again, applies the
  client's mode and options, scrubs and audits;
* passes free-text filters (``category``, ``payee``) to the API, which matches
  them against the household's **canonical** category and payee names exactly
  (case-insensitive) as the client sees them, scrubbed (a guess of a hidden
  name matches nothing); they are never used as patterns, here or there;
* runs the second-stage scrub (:mod:`app.mcp.scrub`) over the whole answer,
  and over every error message, before it leaves the process;
* is counted by the per-process rate limit (:class:`RateLimiter`).

There are no write tools: :data:`TOOL_NAMES` is the complete, read-only set,
and ``tests/security/mcp/test_mcp_server.py`` fails if the server lists
anything else. A refusal from the API (``403``: AI access switched off, the
client revoked or expired, a view refused in Aggregates mode; ``429``: the
API's per-client limit) is reported to the assistant as a policy answer.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from app.core.logging import get_logger
from app.mcp.client import AiApi, ApiRefusalError, Params
from app.mcp.scrub import ResponseScrubber

log = get_logger("perfin.mcp")

#: The complete tool set. Every one is read-only; there is no write tool.
TOOL_NAMES: Final = (
    "explain_data_policy",
    "get_overview",
    "get_spending",
    "get_income",
    "get_budget_status",
    "get_trends",
    "get_recurring",
    "get_savings_goals",
    "get_net_worth_history",
    "get_allocation",
    "list_accounts",
    "list_transactions",
    "find_anomalies",
    "list_holdings",
    "get_performance",
    "list_loans",
    "get_loan_schedule",
    "simulate_loan",
    "get_tax_year_summary",
)

#: Non-403 refusals that are the client's policy too (the date-range cap).
POLICY_CODES: Final = frozenset({"outside-date-range"})

READ_ONLY: Final = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

# ---- Argument types ----------------------------------------------------------

Period = Annotated[
    str | None,
    Field(
        pattern=r"^(this_month|last_month|last_30_days|last_90_days|last_12_months|this_year"
        r"|last_year|ytd|[0-9]{4}|[0-9]{4}-[0-9]{2})$",
        description="this_month, last_month, last_30_days, last_90_days, last_12_months, "
        "this_year, last_year, ytd, YYYY or YYYY-MM (default: this month)",
    ),
]
DateFrom = Annotated[date | None, Field(description="first day (overrides period)")]
DateTo = Annotated[date | None, Field(description="last day (overrides period)")]
GroupBy = Literal["category", "payee", "merchant_category", "month", "account", "account_type"]
Depth = Annotated[int, Field(ge=1, le=3, description="category levels (1-3)")]
RangeName = Literal["1m", "3m", "6m", "ytd", "1y", "3y", "5y", "max"]
#: A canonical category or payee name, matched exactly (case-insensitive).
CanonicalName = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=200,
        pattern=r"^[^\x00-\x1f\x7f]+$",
        description="exact category or payee name as PerFin shows it (not a pattern)",
    ),
]
Amount = Annotated[Decimal | None, Field(ge=0, le=Decimal("1e12"), max_digits=16, decimal_places=2)]
ItemId = Annotated[uuid.UUID, Field(description="an id returned by a list tool")]
OptionalItemId = Annotated[uuid.UUID | None, Field(description="an id returned by list_accounts")]


# ---- Rate limit --------------------------------------------------------------


class RateLimiter:
    """Sliding one-minute window of tool calls for this process (M6)."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = per_minute
        self._clock = clock
        self._lock = threading.Lock()
        self._calls: deque[float] = deque()

    def allow(self) -> bool:
        now = self._clock()
        with self._lock:
            while self._calls and self._calls[0] <= now - 60.0:
                self._calls.popleft()
            if len(self._calls) >= self.per_minute:
                return False
            self._calls.append(now)
            return True


# ---- Runtime -----------------------------------------------------------------


class ToolRuntime:
    """What every tool shares: the API client, the second scrub, the rate limit."""

    def __init__(
        self,
        api: AiApi,
        *,
        limiter: RateLimiter,
        scrubber: ResponseScrubber | None = None,
    ) -> None:
        self.api = api
        self.limiter = limiter
        self.scrubber = scrubber or ResponseScrubber()

    def _refusal(self, tool: str, exc: ApiRefusalError) -> ToolError:
        if exc.status == 403 or exc.code in POLICY_CODES:
            text = (
                f"PerFin refused {tool} by policy: {exc.title} ({exc.code}). {exc.detail} "
                "AI access may be switched off, this client revoked or expired, the view "
                "not available in this client's mode or the period before its date cap; "
                "explain_data_policy shows the policy."
            )
        elif exc.status == 429:
            text = f"PerFin rate limit reached ({exc.code}); try again in a minute."
        elif exc.status in (401,):
            text = "PerFin did not accept this MCP server's token (revoked or expired)."
        else:
            text = f"PerFin could not answer {tool}: {exc.title} ({exc.code}). {exc.detail}"
        return ToolError(self.scrubber.text(" ".join(text.split())))

    async def call(self, tool: str, path: str, params: Params | None = None) -> str:
        """Fetch one AI view, scrub it again and return it as JSON text."""
        started = time.monotonic()
        if not self.limiter.allow():
            log.warning("mcp tool call rate limited", extra={"tool": tool, "status": 429})
            raise ToolError(
                f"Rate limit of this MCP server reached ({self.limiter.per_minute} calls a "
                "minute); try again shortly."
            )
        try:
            body = await anyio.to_thread.run_sync(lambda: self.api.get(path, params))
        except ApiRefusalError as exc:
            log.info("mcp tool call refused", extra={"tool": tool, "status": exc.status})
            raise self._refusal(tool, exc) from None
        clean = self.scrubber.json(body)
        text = json.dumps(clean, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        log.info(
            "mcp tool call",
            extra={
                "tool": tool,
                "status": 200,
                "bytes": len(text.encode()),
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return text


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _uid(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


def _dec(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _period(period: str | None, date_from: date | None, date_to: date | None) -> dict[str, Any]:
    return {"period": period, "date_from": _iso(date_from), "date_to": _iso(date_to)}


# ---- Registration ------------------------------------------------------------


def register(server: MCPServer, rt: ToolRuntime, *, mcp_info: Mapping[str, Any]) -> None:
    """Add every tool of :data:`TOOL_NAMES` to ``server``."""

    def add(fn: Callable[..., Any], title: str) -> None:
        server.add_tool(fn, title=title, annotations=READ_ONLY, structured_output=False)

    async def explain_data_policy() -> str:
        """This client's data policy: mode (detail or aggregates), rounding, hidden labels
        and merchant location, date cap, the classes of data never exposed (identity,
        documents, credentials), the scrubber placeholders and the rate limits."""
        text = await rt.call("explain_data_policy", "/policy")
        policy = json.loads(text)
        policy["mcp_server"] = dict(mcp_info)
        return json.dumps(policy, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    async def get_overview(
        period: Period = None, date_from: DateFrom = None, date_to: DateTo = None
    ) -> str:
        """Net worth now, cash flow and savings rate for a period."""
        return await rt.call("get_overview", "/overview", _period(period, date_from, date_to))

    async def get_spending(
        period: Period = None,
        date_from: DateFrom = None,
        date_to: DateTo = None,
        group_by: GroupBy = "category",
        depth: Depth = 1,
    ) -> str:
        """Spending for a period grouped by category, payee, month or account
        (merchant_category is served by the household's categories)."""
        params = _period(period, date_from, date_to) | {
            "group_by": "category" if group_by == "merchant_category" else group_by,
            "depth": depth,
        }
        return await rt.call("get_spending", "/spending", params)

    async def get_income(
        period: Period = None,
        date_from: DateFrom = None,
        date_to: DateTo = None,
        group_by: GroupBy = "category",
        depth: Depth = 1,
    ) -> str:
        """Income for a period grouped by category, payee, month or account."""
        params = _period(period, date_from, date_to) | {
            "group_by": "category" if group_by == "merchant_category" else group_by,
            "depth": depth,
        }
        return await rt.call("get_income", "/income", params)

    async def get_budget_status(
        on: Annotated[date | None, Field(description="day to report on (default today)")] = None,
    ) -> str:
        """Budget lines with budgeted, spent and remaining amounts."""
        return await rt.call("get_budget_status", "/budgets/status", {"on": _iso(on)})

    async def get_trends(
        metric: Literal["spending", "income"] = "spending",
        periods: Annotated[int, Field(ge=1, le=60, description="number of months")] = 12,
        month: Annotated[
            str | None, Field(pattern=r"^[0-9]{4}-[0-9]{2}$", description="last month YYYY-MM")
        ] = None,
    ) -> str:
        """Monthly spending or income per category over recent months."""
        params = {"metric": metric, "months": periods, "month": month}
        return await rt.call("get_trends", "/trends", params)

    async def get_recurring() -> str:
        """Subscriptions and bills PerFin tracks as recurring."""
        return await rt.call("get_recurring", "/recurring")

    async def get_savings_goals() -> str:
        """Savings goals with target, saved amount and progress."""
        return await rt.call("get_savings_goals", "/goals")

    async def get_net_worth_history(
        range: RangeName = "1y",
        granularity: Literal["day", "week", "month"] | None = None,
    ) -> str:
        """Net worth over time."""
        params = {"range": range, "granularity": granularity}
        return await rt.call("get_net_worth_history", "/net-worth/history", params)

    async def get_allocation() -> str:
        """Investment allocation by asset class, sector and account type."""
        return await rt.call("get_allocation", "/allocation")

    async def list_accounts() -> str:
        """Accounts: type, label, institution, currency and balance (never a number)."""
        return await rt.call("list_accounts", "/accounts")

    async def list_transactions(
        period: Period = None,
        date_from: DateFrom = None,
        date_to: DateTo = None,
        account: OptionalItemId = None,
        category: CanonicalName = None,
        payee: CanonicalName = None,
        min_amount: Amount = None,
        max_amount: Amount = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        cursor: Annotated[
            str | None, Field(pattern=r"^[A-Za-z0-9_-]{1,200}={0,2}$", description="next_cursor")
        ] = None,
    ) -> str:
        """Transactions of a period (not in Aggregates mode). category and payee must be
        exact names as PerFin shows them; amounts filter by absolute value."""
        params = _period(period, date_from, date_to) | {
            "account_id": _uid(account),
            "category": category,
            "payee": payee,
            "min_amount": _dec(min_amount),
            "max_amount": _dec(max_amount),
            "limit": limit,
            "cursor": cursor,
        }
        return await rt.call("list_transactions", "/transactions", params)

    async def find_anomalies(
        period: Period = None, date_from: DateFrom = None, date_to: DateTo = None
    ) -> str:
        """Unusually large spending for its category, and new payees."""
        return await rt.call("find_anomalies", "/anomalies", _period(period, date_from, date_to))

    async def list_holdings(account: OptionalItemId = None) -> str:
        """Investment holdings by ticker (not in Aggregates mode)."""
        return await rt.call("list_holdings", "/holdings", {"account_id": _uid(account)})

    async def get_performance(
        range: RangeName = "1y",
        account: OptionalItemId = None,
    ) -> str:
        """Investment performance over a range."""
        params = {"range": range, "account_id": _uid(account)}
        return await rt.call("get_performance", "/performance", params)

    async def list_loans() -> str:
        """Loans: lender, balance, rate, term and payoff (never a loan number)."""
        return await rt.call("list_loans", "/loans")

    async def get_loan_schedule(
        loan: ItemId,
        basis: Literal["original", "projected"] = "projected",
        offset: Annotated[int, Field(ge=0, le=10_000)] = 0,
        limit: Annotated[int, Field(ge=1, le=600)] = 120,
    ) -> str:
        """Amortisation schedule of one loan (id from list_loans)."""
        params = {"basis": basis, "offset": offset, "limit": limit}
        return await rt.call("get_loan_schedule", f"/loans/{loan}/schedule", params)

    async def simulate_loan(
        loan: ItemId,
        extra_payment: Annotated[Amount, Field(description="extra amount paid every month")] = None,
        extra_once: Annotated[Amount, Field(description="one extra payment")] = None,
        extra_once_on: Annotated[date | None, Field(description="date of extra_once")] = None,
    ) -> str:
        """What extra payments would do to a loan (computes only, stores nothing)."""
        params = {
            "extra_monthly": _dec(extra_payment),
            "extra_once": _dec(extra_once),
            "extra_once_on": _iso(extra_once_on),
        }
        return await rt.call("simulate_loan", f"/loans/{loan}/simulate", params)

    async def get_tax_year_summary(year: Annotated[int, Field(ge=1900, le=2200)]) -> str:
        """Deductible and taxable totals of a tax year and the form checklist status."""
        return await rt.call("get_tax_year_summary", f"/tax/{year}/summary")

    tools: dict[str, tuple[Callable[..., Any], str]] = {
        "explain_data_policy": (explain_data_policy, "Explain data policy"),
        "get_overview": (get_overview, "Overview"),
        "get_spending": (get_spending, "Spending"),
        "get_income": (get_income, "Income"),
        "get_budget_status": (get_budget_status, "Budget status"),
        "get_trends": (get_trends, "Trends"),
        "get_recurring": (get_recurring, "Recurring"),
        "get_savings_goals": (get_savings_goals, "Savings goals"),
        "get_net_worth_history": (get_net_worth_history, "Net worth history"),
        "get_allocation": (get_allocation, "Allocation"),
        "list_accounts": (list_accounts, "Accounts"),
        "list_transactions": (list_transactions, "Transactions"),
        "find_anomalies": (find_anomalies, "Anomalies"),
        "list_holdings": (list_holdings, "Holdings"),
        "get_performance": (get_performance, "Performance"),
        "list_loans": (list_loans, "Loans"),
        "get_loan_schedule": (get_loan_schedule, "Loan schedule"),
        "simulate_loan": (simulate_loan, "Loan simulation"),
        "get_tax_year_summary": (get_tax_year_summary, "Tax year summary"),
    }
    if tuple(tools) != TOOL_NAMES:
        raise RuntimeError("the registered tools differ from TOOL_NAMES")
    for fn, title in tools.values():
        add(fn, title)

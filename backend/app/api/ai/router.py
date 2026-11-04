# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/api/v1/ai/v1``: the read-only AI views (AI_ACCESS.md "Tools", API.md "AI views").

Mounted by ``app.api.routers.ai_v1``. Every route:

* answers only an active AI client's token (``ai:read``, minted by the
  AI-client flow): anything else is ``401`` (no or a bad credential) or
  ``403 ai-client-required`` (sessions, personal tokens, a revoked client);
  the token's household is the only one it reads;
* answers ``403 ai-access-disabled`` while the instance kill switch or the
  household's AI flag is off (``app.domain.ai.switches``);
* is rate limited per client (M6; ``429 rate-limited``) on top of the
  per-token limit of every bearer token;
* is a ``GET`` with validated query parameters only (no write exists here,
  E2/M3); free-text parameters (``category``, ``payee``) are matched against
  canonical names as scrubbed for the client (a guess of a hidden name is a
  miss), never used as patterns;
* returns an allowlisted model (``app.api.ai.schemas``) with the client's
  mode and options applied and free text scrubbed (``app.api.ai.views``);
* writes one ``ai_call`` row with the tool, the parameters, the status, the
  byte count and the body exactly as sent (M4). A refusal inside the view
  (``404``, ``422``, ``403 not-in-aggregates-mode``) is answered as a
  problem response and recorded the same way.

Cheap-to-repeat aggregate views (spending, income, trends, overview, net
worth history, allocation, performance, tax summary) are cached per client,
tool, parameters and scrubber terms (a digest: a member who joins or is
renamed changes it) for :data:`CACHE_SECONDS` (M6); each call is still
audited with the body it received.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from app.api.ai import schemas as s
from app.api.ai import views
from app.api.ai.views import AiContext
from app.auth.deps import DbSession, OptionalPrincipal, mark_ai_route, unauthenticated
from app.core.errors import PROBLEM_MEDIA_TYPE, ProblemError, problem_body
from app.domain.ai import service, switches
from app.domain.ai.policy import ClientPolicy
from app.domain.reports.schemas import MAX_DEPTH, MAX_TREND_MONTHS, Flow

router = APIRouter(prefix="/ai/v1", tags=["ai"], dependencies=[Depends(mark_ai_route)])

#: How long a cached aggregate answer is reused (M6).
CACHE_SECONDS: Final = 60.0
CACHE_MAX: Final = 512
CACHED_TOOLS: Final = frozenset(
    {
        "get_spending",
        "get_income",
        "get_trends",
        "get_overview",
        "get_net_worth_history",
        "get_allocation",
        "get_performance",
        "get_tax_year_summary",
    }
)

PROBLEMS: Final[dict[int | str, dict[str, Any]]] = {
    code: {"description": "Problem details", "content": {PROBLEM_MEDIA_TYPE: {}}}
    for code in (401, 403, 404, 422, 429)
}


class _Cache:
    """A small per-process TTL cache of response bodies."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[float, int, bytes]] = OrderedDict()

    def get(self, key: str) -> tuple[int, bytes] | None:
        now = time.monotonic()
        with self._lock:
            found = self._items.get(key)
            if found is None:
                return None
            if found[0] < now:
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return found[1], found[2]

    def put(self, key: str, status: int, body: bytes) -> None:
        with self._lock:
            self._items[key] = (time.monotonic() + CACHE_SECONDS, status, body)
            self._items.move_to_end(key)
            while len(self._items) > CACHE_MAX:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


CACHE: Final = _Cache()


def ai_context(request: Request, db: DbSession, principal: OptionalPrincipal) -> AiContext:
    """The AI request context: client, policy, scrubber (module doc)."""
    if principal is None:
        raise unauthenticated()
    if principal.restricted:
        raise ProblemError(
            403,
            "mfa-enrollment-required",
            "Multi-factor authentication required",
            "This instance requires a second factor. Set one up to continue.",
        )
    client = service.resolve_client(db, principal)
    # The instance kill switch and the household flag (app.domain.ai.switches).
    switches.require_enabled(db, client.household_id)
    service.check_rate(request, client)
    policy = ClientPolicy.of(client)
    return AiContext(
        db=db,
        principal=principal,
        client=client,
        policy=policy,
        scrubber=service.scrubber_for(db, client.household_id, policy),
    )


Ctx = Annotated[AiContext, Depends(ai_context)]


def _jsonable(value: Any) -> Any:
    if isinstance(value, date | Decimal | uuid.UUID):
        return str(value)
    return value


def respond(
    ctx: AiContext, tool: str, params: dict[str, Any], build: Callable[[], s.AiOut]
) -> Response:
    """Build, audit (M4) and return one view (module doc)."""
    clean = {k: _jsonable(v) for k, v in sorted(params.items()) if v is not None}
    key = None
    cached = None
    if tool in CACHED_TOOLS:
        key = json.dumps(
            [
                str(ctx.client.id),
                ctx.client.updated_at.isoformat(),
                # The member terms and denylist the body was scrubbed with: a
                # member who joins or is renamed invalidates it (P6B-SR-R1-05).
                ctx.scrubber.fingerprint,
                tool,
                clean,
                str(ctx.today),
            ],
            sort_keys=True,
        )
        cached = CACHE.get(key)
    if cached is not None:
        status, body = cached
    else:
        try:
            body = build().model_dump_json().encode()
            status = 200
        except ProblemError as exc:
            status = exc.status
            body = json.dumps(
                problem_body(exc.status, exc.slug, exc.title, exc.problem_detail, exc.extra),
                default=str,
            ).encode()
        if key is not None and status == 200:
            CACHE.put(key, status, body)
    service.record_call(
        ctx.db,
        ctx.client,
        tool=tool,
        params=clean,
        status=status,
        body=body,
        response=json.loads(body),
    )
    media = "application/json" if status < 400 else PROBLEM_MEDIA_TYPE
    return Response(
        content=body, status_code=status, media_type=media, headers={"Cache-Control": "no-store"}
    )


def _limits_text(request: Request) -> list[str]:
    limits = service.get_ai_limits(request)
    return [
        f"{lim.max_events} calls per {int(lim.window.total_seconds())} s"
        for lim in (limits.per_minute, limits.per_ten_minutes)
    ]


Period = Annotated[
    str | None,
    Query(
        max_length=20,
        description="this_month, last_month, last_30_days, last_90_days, last_12_months, "
        "this_year, last_year, ytd, YYYY or YYYY-MM",
    ),
]
DateFrom = Annotated[date | None, Query()]
DateTo = Annotated[date | None, Query()]
Name = Annotated[str | None, Query(min_length=1, max_length=200)]
Amount = Annotated[Decimal | None, Query(ge=0, le=Decimal("1e12"), max_digits=20, decimal_places=6)]
RangeQ = Annotated[views.Range, Query(alias="range")]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/policy",
    response_model=s.AiDataPolicyOut,
    responses=PROBLEMS,
    summary="explain_data_policy: this client's mode, options and the identity denylist",
)
def explain_data_policy(ctx: Ctx, request: Request) -> Response:
    limits = _limits_text(request)
    return respond(
        ctx, "explain_data_policy", {}, lambda: views.data_policy(ctx, rate_limits=limits)
    )


@router.get("/overview", response_model=s.AiOverviewOut, responses=PROBLEMS, summary="get_overview")
def get_overview(
    ctx: Ctx, period: Period = None, date_from: DateFrom = None, date_to: DateTo = None
) -> Response:
    params = {"period": period, "date_from": date_from, "date_to": date_to}
    return respond(
        ctx,
        "get_overview",
        params,
        lambda: views.overview(ctx, period=period, date_from=date_from, date_to=date_to),
    )


def _flow_route(flow: Flow) -> Callable[..., Response]:
    tool = f"get_{flow.value}"

    def handler(
        ctx: Ctx,
        period: Period = None,
        date_from: DateFrom = None,
        date_to: DateTo = None,
        group_by: views.AiGroupBy = "category",
        depth: Annotated[int, Query(ge=1, le=MAX_DEPTH)] = 1,
    ) -> Response:
        params = {
            "period": period,
            "date_from": date_from,
            "date_to": date_to,
            "group_by": group_by,
            "depth": depth,
        }
        return respond(
            ctx,
            tool,
            params,
            lambda: views.flow(
                ctx,
                flow=flow,
                period=period,
                date_from=date_from,
                date_to=date_to,
                group_by=group_by,
                depth=depth,
            ),
        )

    handler.__name__ = tool
    return handler


router.add_api_route(
    "/spending",
    _flow_route(Flow.SPENDING),
    methods=["GET"],
    response_model=s.AiFlowOut,
    responses=PROBLEMS,
    summary="get_spending",
)
router.add_api_route(
    "/income",
    _flow_route(Flow.INCOME),
    methods=["GET"],
    response_model=s.AiFlowOut,
    responses=PROBLEMS,
    summary="get_income",
)


@router.get("/trends", response_model=s.AiTrendsOut, responses=PROBLEMS, summary="get_trends")
def get_trends(
    ctx: Ctx,
    metric: Literal["spending", "income"] = "spending",
    month: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
    months: Annotated[int, Query(ge=1, le=MAX_TREND_MONTHS)] = 12,
) -> Response:
    params = {"metric": metric, "month": month, "months": months}
    return respond(
        ctx,
        "get_trends",
        params,
        lambda: views.trends(ctx, flow=Flow(metric), month=month, months=months),
    )


@router.get(
    "/budgets/status",
    response_model=s.AiBudgetStatusOut,
    responses=PROBLEMS,
    summary="get_budget_status",
)
def get_budget_status(ctx: Ctx, on: DateFrom = None) -> Response:
    return respond(ctx, "get_budget_status", {"on": on}, lambda: views.budget_status(ctx, on=on))


@router.get("/goals", response_model=s.AiGoalsOut, responses=PROBLEMS, summary="get_savings_goals")
def get_savings_goals(ctx: Ctx) -> Response:
    return respond(ctx, "get_savings_goals", {}, lambda: views.goals(ctx))


@router.get(
    "/recurring", response_model=s.AiRecurringOut, responses=PROBLEMS, summary="get_recurring"
)
def get_recurring(ctx: Ctx) -> Response:
    return respond(ctx, "get_recurring", {}, lambda: views.recurring(ctx))


@router.get(
    "/net-worth/history",
    response_model=s.AiNetWorthHistoryOut,
    responses=PROBLEMS,
    summary="get_net_worth_history",
)
def get_net_worth_history(
    ctx: Ctx, range_: RangeQ = "1y", granularity: views.GranularityName | None = None
) -> Response:
    params = {"range": range_, "granularity": granularity}
    return respond(
        ctx,
        "get_net_worth_history",
        params,
        lambda: views.net_worth_history(ctx, range_=range_, granularity=granularity),
    )


@router.get(
    "/allocation", response_model=s.AiAllocationOut, responses=PROBLEMS, summary="get_allocation"
)
def get_allocation(ctx: Ctx) -> Response:
    return respond(ctx, "get_allocation", {}, lambda: views.allocation(ctx))


@router.get(
    "/accounts", response_model=s.AiAccountsOut, responses=PROBLEMS, summary="list_accounts"
)
def list_accounts(ctx: Ctx) -> Response:
    return respond(ctx, "list_accounts", {}, lambda: views.accounts(ctx))


@router.get(
    "/transactions",
    response_model=s.AiTransactionsOut,
    responses=PROBLEMS,
    summary="list_transactions",
)
def list_transactions(
    ctx: Ctx,
    period: Period = None,
    date_from: DateFrom = None,
    date_to: DateTo = None,
    account_id: uuid.UUID | None = None,
    category: Name = None,
    payee: Name = None,
    min_amount: Amount = None,
    max_amount: Amount = None,
    limit: Annotated[int, Query(ge=1, le=views.MAX_TRANSACTIONS)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> Response:
    params = {
        "period": period,
        "date_from": date_from,
        "date_to": date_to,
        "account_id": account_id,
        "category": category,
        "payee": payee,
        "min_amount": min_amount,
        "max_amount": max_amount,
        "limit": limit,
        "cursor": cursor,
    }
    return respond(
        ctx,
        "list_transactions",
        params,
        lambda: views.transactions(
            ctx,
            period=period,
            date_from=date_from,
            date_to=date_to,
            account_id=account_id,
            category=category,
            payee=payee,
            min_amount=min_amount,
            max_amount=max_amount,
            limit=limit,
            cursor=cursor,
        ),
    )


@router.get(
    "/anomalies", response_model=s.AiAnomaliesOut, responses=PROBLEMS, summary="find_anomalies"
)
def find_anomalies(
    ctx: Ctx, period: Period = None, date_from: DateFrom = None, date_to: DateTo = None
) -> Response:
    params = {"period": period, "date_from": date_from, "date_to": date_to}
    return respond(
        ctx,
        "find_anomalies",
        params,
        lambda: views.anomalies(ctx, period=period, date_from=date_from, date_to=date_to),
    )


@router.get(
    "/holdings", response_model=s.AiHoldingsOut, responses=PROBLEMS, summary="list_holdings"
)
def list_holdings(ctx: Ctx, account_id: uuid.UUID | None = None) -> Response:
    return respond(
        ctx,
        "list_holdings",
        {"account_id": account_id},
        lambda: views.holdings(ctx, account_id=account_id),
    )


@router.get(
    "/performance", response_model=s.AiPerformanceOut, responses=PROBLEMS, summary="get_performance"
)
def get_performance(
    ctx: Ctx, range_: RangeQ = "1y", account_id: uuid.UUID | None = None
) -> Response:
    params = {"range": range_, "account_id": account_id}
    return respond(
        ctx,
        "get_performance",
        params,
        lambda: views.performance(ctx, range_=range_, account_id=account_id),
    )


@router.get("/loans", response_model=s.AiLoansOut, responses=PROBLEMS, summary="list_loans")
def list_loans(ctx: Ctx) -> Response:
    return respond(ctx, "list_loans", {}, lambda: views.loans(ctx))


LoanId = Annotated[uuid.UUID, Path()]


@router.get(
    "/loans/{loan_id}/schedule",
    response_model=s.AiLoanScheduleOut,
    responses=PROBLEMS,
    summary="get_loan_schedule",
)
def get_loan_schedule(
    ctx: Ctx,
    loan_id: LoanId,
    basis: Literal["original", "projected"] = "projected",
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=views.MAX_SCHEDULE_ROWS)] = 120,
) -> Response:
    params = {"loan_id": loan_id, "basis": basis, "offset": offset, "limit": limit}
    return respond(
        ctx,
        "get_loan_schedule",
        params,
        lambda: views.loan_schedule(ctx, loan_id, basis=basis, offset=offset, limit=limit),
    )


@router.get(
    "/loans/{loan_id}/simulate",
    response_model=s.AiSimulationOut,
    responses=PROBLEMS,
    summary="simulate_loan (computes only, stores nothing)",
)
def simulate_loan(
    ctx: Ctx,
    loan_id: LoanId,
    extra_monthly: Amount = None,
    extra_once: Amount = None,
    extra_once_on: DateFrom = None,
) -> Response:
    params = {
        "loan_id": loan_id,
        "extra_monthly": extra_monthly,
        "extra_once": extra_once,
        "extra_once_on": extra_once_on,
    }
    return respond(
        ctx,
        "simulate_loan",
        params,
        lambda: views.simulate_loan(
            ctx,
            loan_id,
            extra_monthly=extra_monthly or None,
            extra_once=extra_once or None,
            extra_once_on=extra_once_on,
        ),
    )


@router.get(
    "/tax/{year}/summary",
    response_model=s.AiTaxYearSummaryOut,
    responses=PROBLEMS,
    summary="get_tax_year_summary",
)
def get_tax_year_summary(ctx: Ctx, year: Annotated[int, Path(ge=1900, le=2200)]) -> Response:
    return respond(
        ctx, "get_tax_year_summary", {"year": year}, lambda: views.tax_year_summary(ctx, year=year)
    )

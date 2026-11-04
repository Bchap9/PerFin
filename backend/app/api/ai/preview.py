# SPDX-License-Identifier: AGPL-3.0-or-later
"""The exposure preview and the scrubber preview of the AI settings page (E3, M2, M5).

**Exposure preview** (:func:`exposure_preview`): runs **every** AI tool with
its default parameters against the household's real data through the very
views and serialisers of ``/ai/v1`` (``app.api.ai.views``) with the chosen
options, as an unsaved :class:`~app.domain.ai.models.AiClient`. Nothing is
stored: no client, no token, no ``ai_call`` row, no cache entry. Each tool's
body is returned exactly as the view would serialise it (a refusal such as
``403 not-in-aggregates-mode`` as its problem body), with every string that
carries a scrubber placeholder marked by its JSON path, so the page can
highlight what was removed.

**Scrubber preview** (:func:`scrubber_preview`): the owner's own free text
(transaction payees, memos and notes, split memos, payee and account names)
next to its scrubbed form, with the member terms of the household plus the
aliases and denylist being tuned, to see what a new term would catch. This
shows original text and is therefore an owner-only settings view; an AI
token can never reach it (``app.auth.deps`` confines it to ``/ai/v1``).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.api.ai import schemas as s
from app.api.ai import views
from app.api.ai.views import AiContext
from app.auth.principal import Principal
from app.core import clock
from app.core.errors import ProblemError, problem_body
from app.core.scrubber import PLACEHOLDERS, Scrubber
from app.domain.accounts.models import Account
from app.domain.accounts.visibility import visible_account_filter
from app.domain.ai import service
from app.domain.ai.models import AiClient
from app.domain.ai.policy import ClientPolicy
from app.domain.ai.schemas import (
    ExposurePreviewOut,
    PlaceholderMark,
    PreviewToolOut,
    ScrubbedSample,
    ScrubberPreviewOut,
)
from app.domain.loans import service as loans_service
from app.domain.reports.schemas import Flow
from app.domain.tax.models import TaxYear
from app.domain.transactions.models import Payee, Transaction, TransactionSplit

#: Rows of a transaction-level tool in the preview.
PREVIEW_ROWS: Final = 20
_MARK: Final = re.compile("|".join(re.escape(p) for p in PLACEHOLDERS))


def transient_client(
    household_id: uuid.UUID, options: service.ClientOptions, *, client_id: uuid.UUID | None = None
) -> AiClient:
    """An unsaved client carrying ``options`` (never added to the session)."""
    now = clock.utcnow()
    return AiClient(
        id=client_id or uuid.uuid4(),
        household_id=household_id,
        name=options.name,
        mode=options.mode,
        transport=options.transport,
        rounding=options.rounding,
        hide_nicknames=options.hide_nicknames,
        hide_merchant_location=options.hide_merchant_location,
        date_range_cap=options.date_range_cap,
        custom_denylist=list(options.custom_denylist),
        member_aliases=list(options.member_aliases),
        created_at=now,
        updated_at=now,
        expires_at=now,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, date | Decimal | uuid.UUID):
        return str(value)
    return value


def placeholder_marks(body: Any) -> list[PlaceholderMark]:
    """Every string in ``body`` holding a placeholder, by JSON path."""
    marks: list[PlaceholderMark] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str):
            found = _MARK.findall(node)
            if found:
                marks.append(PlaceholderMark(path=path, placeholders=found))

    walk(body, "")
    return marks


@dataclass(frozen=True)
class _Tool:
    name: str
    params: dict[str, Any]
    build: Callable[[AiContext], s.AiOut]


def _first_loan(db: Session, principal: Principal) -> uuid.UUID | None:
    page = loans_service.list_loans(db, principal, cursor=None, limit=1)
    return page.items[0].id if page.items else None


def _tax_year(db: Session, household_id: uuid.UUID, today: date) -> int:
    year = db.execute(
        select(TaxYear.year)
        .where(TaxYear.household_id == household_id, TaxYear.year <= today.year)
        .order_by(TaxYear.year.desc())
        .limit(1)
    ).scalar_one_or_none()
    return int(year) if year is not None else today.year - 1


def _flow_view(flow: Flow) -> Callable[[AiContext], s.AiOut]:
    def build(ctx: AiContext) -> s.AiOut:
        return views.flow(
            ctx,
            flow=flow,
            period=None,
            date_from=None,
            date_to=None,
            group_by="category",
            depth=1,
        )

    return build


def _tools(ctx: AiContext, rate_limits: list[str]) -> Iterator[_Tool]:
    """Every AI tool with the defaults of its route (``app.api.ai.router``)."""
    yield _Tool("explain_data_policy", {}, lambda c: views.data_policy(c, rate_limits=rate_limits))
    yield _Tool(
        "get_overview",
        {},
        lambda c: views.overview(c, period=None, date_from=None, date_to=None),
    )
    for flow in (Flow.SPENDING, Flow.INCOME):
        yield _Tool(f"get_{flow.value}", {"group_by": "category", "depth": 1}, _flow_view(flow))
    yield _Tool(
        "get_trends",
        {"metric": "spending", "months": 12},
        lambda c: views.trends(c, flow=Flow.SPENDING, month=None, months=12),
    )
    yield _Tool("get_budget_status", {}, lambda c: views.budget_status(c, on=None))
    yield _Tool("get_savings_goals", {}, views.goals)
    yield _Tool("get_recurring", {}, views.recurring)
    yield _Tool(
        "get_net_worth_history",
        {"range": "1y"},
        lambda c: views.net_worth_history(c, range_="1y", granularity=None),
    )
    yield _Tool("get_allocation", {}, views.allocation)
    yield _Tool("list_accounts", {}, views.accounts)
    yield _Tool(
        "list_transactions",
        {"limit": PREVIEW_ROWS},
        lambda c: views.transactions(
            c,
            period=None,
            date_from=None,
            date_to=None,
            account_id=None,
            category=None,
            payee=None,
            min_amount=None,
            max_amount=None,
            limit=PREVIEW_ROWS,
            cursor=None,
        ),
    )
    yield _Tool(
        "find_anomalies",
        {},
        lambda c: views.anomalies(c, period=None, date_from=None, date_to=None),
    )
    yield _Tool("list_holdings", {}, lambda c: views.holdings(c, account_id=None))
    yield _Tool(
        "get_performance",
        {"range": "1y"},
        lambda c: views.performance(c, range_="1y", account_id=None),
    )
    yield _Tool("list_loans", {}, views.loans)
    loan_id = _first_loan(ctx.db, ctx.principal)
    if loan_id is not None:
        yield _Tool(
            "get_loan_schedule",
            {"loan_id": loan_id, "basis": "projected", "offset": 0, "limit": PREVIEW_ROWS},
            lambda c: views.loan_schedule(
                c, loan_id, basis="projected", offset=0, limit=PREVIEW_ROWS
            ),
        )
        yield _Tool(
            "simulate_loan",
            {"loan_id": loan_id, "extra_monthly": "100"},
            lambda c: views.simulate_loan(
                c, loan_id, extra_monthly=Decimal(100), extra_once=None, extra_once_on=None
            ),
        )
    year = _tax_year(ctx.db, ctx.household_id, ctx.today)
    yield _Tool(
        "get_tax_year_summary",
        {"year": year},
        lambda c: views.tax_year_summary(c, year=year),
    )


def _run(ctx: AiContext, tool: _Tool) -> PreviewToolOut:
    try:
        raw = tool.build(ctx).model_dump_json().encode()
        status = 200
    except ProblemError as exc:
        status = exc.status
        raw = json.dumps(
            problem_body(exc.status, exc.slug, exc.title, exc.problem_detail, exc.extra),
            default=str,
        ).encode()
    body = json.loads(raw)
    return PreviewToolOut(
        tool=tool.name,
        params={k: _jsonable(v) for k, v in sorted(tool.params.items())},
        status=status,
        bytes=len(raw),
        body=body,
        placeholders=placeholder_marks(body),
    )


def exposure_preview(
    db: Session,
    conn: HTTPConnection,
    principal: Principal,
    client: AiClient,
) -> ExposurePreviewOut:
    """Every tool's body for ``client``'s options (module doc). Stores nothing."""
    policy = ClientPolicy.of(client)
    ctx = AiContext(
        db=db,
        principal=principal,
        client=client,
        policy=policy,
        scrubber=service.scrubber_for(db, client.household_id, policy),
    )
    limits = service.get_ai_limits(conn)
    rate_limits = [
        f"{lim.max_events} calls per {int(lim.window.total_seconds())} s"
        for lim in (limits.per_minute, limits.per_ten_minutes)
    ]
    tools = [_run(ctx, tool) for tool in _tools(ctx, rate_limits)]
    counts = dict.fromkeys(PLACEHOLDERS, 0)
    for tool in tools:
        for mark in tool.placeholders:
            for p in mark.placeholders:
                counts[p] += 1
    return ExposurePreviewOut(mode=client.mode, tools=tools, placeholder_counts=counts)


# ---------------------------------------------------------------------------
# Scrubber preview
# ---------------------------------------------------------------------------


def _own_free_text(
    db: Session, principal: Principal, household_id: uuid.UUID, limit: int
) -> list[tuple[str, str]]:
    """Recent free text of the household the owner can see: ``(source, text)``."""
    out: list[tuple[str, str]] = []
    rows = db.execute(
        select(Transaction.raw_payee, Transaction.memo, Transaction.notes)
        .where(
            Transaction.household_id == household_id,
            visible_account_filter(principal, Transaction.account_id),
            or_(
                Transaction.raw_payee.is_not(None),
                Transaction.memo.is_not(None),
                Transaction.notes.is_not(None),
            ),
        )
        .order_by(Transaction.posted_date.desc(), Transaction.id.desc())
        .limit(limit)
    ).all()
    for raw_payee, memo, notes in rows:
        for source, text in (
            ("transaction.raw_payee", raw_payee),
            ("transaction.memo", memo),
            ("transaction.notes", notes),
        ):
            if text:
                out.append((source, text))
    splits = db.execute(
        select(TransactionSplit.memo)
        .join(Transaction, Transaction.id == TransactionSplit.transaction_id)
        .where(
            Transaction.household_id == household_id,
            visible_account_filter(principal, Transaction.account_id),
            TransactionSplit.memo.is_not(None),
        )
        .order_by(Transaction.posted_date.desc(), TransactionSplit.id.desc())
        .limit(limit)
    ).scalars()
    out.extend(("transaction_split.memo", memo) for memo in splits if memo)
    payees = db.execute(
        select(Payee.name)
        .where(Payee.household_id == household_id)
        .order_by(Payee.name)
        .limit(limit)
    ).scalars()
    out.extend(("payee.name", name) for name in payees)
    accounts = db.execute(
        select(Account.name, Account.nickname)
        .where(
            Account.household_id == household_id,
            visible_account_filter(principal, Account.id),
        )
        .order_by(Account.created_at, Account.id)
    ).all()
    for name, nickname in accounts:
        out.append(("account.name", name))
        if nickname:
            out.append(("account.nickname", nickname))
    # Same text from several rows is shown once.
    seen: set[tuple[str, str]] = set()
    unique = []
    for item in out:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def sample(scrubber: Scrubber, source: str, text: str) -> ScrubbedSample:
    scrubbed = scrubber.scrub_text(text)
    found = sorted(set(_MARK.findall(scrubbed)))
    return ScrubbedSample(
        source=source,
        original=text,
        scrubbed=scrubbed,
        changed=scrubbed != text,
        placeholders=found,
    )


def scrubber_preview(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    *,
    custom_denylist: list[str],
    member_aliases: list[str],
    text: str | None,
    limit: int,
) -> ScrubberPreviewOut:
    """The owner's free text, original next to scrubbed (module doc)."""
    policy = ClientPolicy.of(
        transient_client(
            household_id,
            service.ClientOptions(
                name="Preview", custom_denylist=custom_denylist, member_aliases=member_aliases
            ),
        )
    )
    scrubber = service.scrubber_for(db, household_id, policy)
    items: list[ScrubbedSample] = []
    if text:
        items.append(sample(scrubber, "text", text))
    items.extend(
        sample(scrubber, source, value)
        for source, value in _own_free_text(db, principal, household_id, limit)
    )
    return ScrubberPreviewOut(samples=items, changed=sum(1 for i in items if i.changed))

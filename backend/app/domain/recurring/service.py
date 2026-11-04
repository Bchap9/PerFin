# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recurring transactions and the cash-flow calendar (API.md ``/recurring``, ``/cashflow``).

Schedules are ``scheduled_transaction`` rows: ``origin = detected`` (written
by detection) or ``manual`` (entered by a person).

Who may do what (routers check role and token scope first; every function
here checks again):

* **Read** (any role, ``transactions:read``): schedules of accounts the caller
  can see (``app.domain.accounts.visibility``); any other is ``404
  schedule-not-found``. ``GET /recurring/candidates`` runs detection over the
  visible accounts without writing anything.
* **Write** (member or above, ``transactions:write``): only on accounts the
  caller may **edit**; a schedule on an account they only view is ``403``.
  ``POST /recurring/detect`` writes detected schedules for the editable
  accounts (or the listed ones, each editable, else ``404
  account-not-found``). The daily ``recurring.detect`` job does the same for
  every account of a household.

Detection (``app.domain.recurring.detection``) feeds schedules as follows. A
candidate matches an existing schedule on the same account with the same
cadence and interval and the same direction (money in or out), preferring
the same payee (or, without a payee, the same normalised name) with an
overlapping amount range, then the same payee or name, then a payee-less
detected schedule with an overlapping range (one a person renamed); each
schedule matches at most one candidate per run. A **manual** match wins:
nothing is written (``skipped_manual``). A **detected** match gets the
new amount range, ``next_due`` and ``last_seen_on`` (and a category when it
had none); its name and ``active`` flag are left alone, so a schedule a
person renamed or switched off stays that way. Otherwise a detected schedule
is created (active, named after the payee or the latest payee text).

Calendar (``GET /cashflow/calendar?days=&account=``): every occurrence of the
active schedules on visible accounts from today for ``days`` days
(``app.domain.budgets.reports.occurrences``), at the midpoint of
``amount_min..amount_max``; positive is income, negative a bill. Amounts are
converted into the base currency at today's rate (latest on or before
today); a schedule whose currency cannot be converted is listed in
``unconverted`` and left out of the totals. ``opening_balance`` is today's
balance of the visible liquid accounts (checking, savings, cash) included in
net worth, and each day's ``balance`` adds the expected flows of schedules
on those accounts up to that day.

Writes are audited (``schedule.create|update|delete|detect``; names are
free text and stay out of the audit trail) and emit ``schedule.created|
updated|deleted`` with ids and the ``account_id`` only.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.money import Money, MoneyIn, to_storage
from app.domain.accounts.balances import balances_as_of
from app.domain.accounts.models import Account, AccountType
from app.domain.accounts.visibility import (
    can_edit_account,
    get_visible_account,
    require_visible_accounts,
    visible_account_filter,
    visible_account_ids,
)
from app.domain.budgets.reports import midpoint, occurrences
from app.domain.fx.service import FxConverter, base_currency
from app.domain.recurring import detection
from app.domain.recurring.detection import Candidate, Observed
from app.domain.recurring.schemas import (
    MAX_SCHEDULES,
    CalendarDay,
    CalendarEvent,
    CandidateList,
    CandidateOut,
    CashflowCalendar,
    DetectIn,
    DetectResult,
    ScheduleCreate,
    ScheduleList,
    ScheduleOut,
    ScheduleUpdate,
)
from app.domain.taxonomy import repo as taxonomy_repo
from app.domain.transactions.models import (
    NAME_MAX,
    Payee,
    ScheduledTransaction,
    ScheduleOrigin,
    Transaction,
)

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

LIQUID_TYPES: Final = frozenset({AccountType.CHECKING, AccountType.SAVINGS, AccountType.CASH})
DEFAULT_NAME: Final = "Recurring"
ZERO: Final = Decimal(0)


def today() -> date:
    return clock.utcnow().date()


def schedule_not_found() -> ProblemError:
    return ProblemError(404, "schedule-not-found", "Recurring transaction not found")


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return principal.household_id


def _visible(principal: Principal) -> Select[ScheduledTransaction]:
    household_id = _household(principal, HouseholdRole.VIEWER)
    return select(ScheduledTransaction).where(
        ScheduledTransaction.household_id == household_id,
        visible_account_filter(principal, ScheduledTransaction.account_id),
    )


def get_row(
    db: Session, principal: Principal, schedule_id: uuid.UUID, *, lock: bool = False
) -> ScheduledTransaction:
    """A schedule the caller may read, or ``404 schedule-not-found``."""
    stmt = _visible(principal).where(ScheduledTransaction.id == schedule_id)
    if lock:
        stmt = stmt.with_for_update()
    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        raise schedule_not_found()
    return row


def require_editable(
    db: Session, principal: Principal, schedule_id: uuid.UUID
) -> ScheduledTransaction:
    """A schedule the caller may change: hidden ``404``, view-only ``403`` (locked)."""
    _household(principal, HouseholdRole.MEMBER)
    row = get_row(db, principal, schedule_id)
    if not can_edit_account(db, principal, row.account_id):
        raise forbidden("You can view this recurring transaction but not change it.")
    return get_row(db, principal, schedule_id, lock=True)


def schedule_out(row: ScheduledTransaction) -> ScheduleOut:
    return ScheduleOut(
        id=row.id,
        account_id=row.account_id,
        name=row.name,
        cadence=row.cadence,
        interval_count=row.interval_count,
        next_due=row.next_due,
        amount_min=Money.of(row.amount_min, row.currency),
        amount_max=Money.of(row.amount_max, row.currency),
        payee_id=row.payee_id,
        category_id=row.category_id,
        origin=row.origin,
        active=row.active,
        last_seen_on=row.last_seen_on,
    )


def _facts(row: ScheduledTransaction) -> dict[str, Any]:
    """What the audit trail keeps about a schedule (no free text)."""
    return {
        "account_id": str(row.account_id),
        "cadence": row.cadence.value,
        "interval_count": row.interval_count,
        "next_due": row.next_due.isoformat(),
        "amount_min": str(row.amount_min),
        "amount_max": str(row.amount_max),
        "currency": row.currency,
        "payee_id": str(row.payee_id) if row.payee_id else None,
        "category_id": str(row.category_id) if row.category_id else None,
        "origin": row.origin.value,
        "active": row.active,
    }


def _event(db: Session, row: ScheduledTransaction, kind: str) -> None:
    emit(
        db,
        row.household_id,
        f"schedule.{kind}",
        {"schedule_id": str(row.id), "account_id": str(row.account_id)},
    )


def _record(
    db: Session,
    principal: Principal,
    row: ScheduledTransaction,
    action: str,
    *,
    before: Any = None,
    after: Any = None,
    request: HTTPConnection | None,
) -> None:
    audit(
        db,
        action=f"schedule.{action}",
        target_type="scheduled_transaction",
        target_id=row.id,
        before=before,
        after=after,
        household_id=row.household_id,
        user_id=principal.user_id,
        request=request,
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def list_schedules(
    db: Session,
    principal: Principal,
    *,
    origin: ScheduleOrigin | None = None,
    active: bool | None = None,
    account_ids: Collection[uuid.UUID] | None = None,
) -> ScheduleList:
    stmt = _visible(principal)
    if account_ids:
        require_visible_accounts(db, principal, account_ids)
        stmt = stmt.where(ScheduledTransaction.account_id.in_(list(account_ids)))
    if origin is not None:
        stmt = stmt.where(ScheduledTransaction.origin == origin)
    if active is not None:
        stmt = stmt.where(ScheduledTransaction.active.is_(active))
    rows = db.execute(
        stmt.order_by(ScheduledTransaction.next_due, ScheduledTransaction.id).limit(MAX_SCHEDULES)
    ).scalars()
    return ScheduleList(items=[schedule_out(r) for r in rows])


def get_schedule(db: Session, principal: Principal, schedule_id: uuid.UUID) -> ScheduleOut:
    return schedule_out(get_row(db, principal, schedule_id))


def _currency_mismatch(currency: str) -> ProblemError:
    return ProblemError(
        422,
        "currency-mismatch",
        "Currency mismatch",
        f"Amounts must be in the account's currency ({currency}).",
    )


def _amount(value: MoneyIn, currency: str) -> Decimal:
    if value.currency != currency:
        raise _currency_mismatch(currency)
    return value.amount


def _check_range(low: Decimal, high: Decimal) -> None:
    if low > high:
        raise ProblemError(
            422, "invalid-amount-range", "Invalid amount range", "amount_min exceeds amount_max."
        )


def _check_refs(
    db: Session,
    household_id: uuid.UUID,
    payee_id: uuid.UUID | None,
    category_id: uuid.UUID | None,
) -> None:
    if payee_id is not None and taxonomy_repo.get_payee(db, household_id, payee_id) is None:
        raise ProblemError(404, "payee-not-found", "Payee not found")
    if (
        category_id is not None
        and taxonomy_repo.get_category(db, household_id, category_id) is None
    ):
        raise ProblemError(404, "category-not-found", "Category not found")


def create_schedule(
    db: Session, principal: Principal, data: ScheduleCreate, *, request: HTTPConnection | None
) -> ScheduleOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    account = get_visible_account(db, principal, data.account_id, need="edit")
    low = _amount(data.amount_min, account.currency)
    high = _amount(data.amount_max, account.currency)
    _check_range(low, high)
    _check_refs(db, household_id, data.payee_id, data.category_id)
    row = ScheduledTransaction(
        household_id=household_id,
        account_id=account.id,
        name=data.name,
        cadence=data.cadence,
        interval_count=data.interval_count,
        next_due=data.next_due,
        amount_min=low,
        amount_max=high,
        currency=account.currency,
        payee_id=data.payee_id,
        category_id=data.category_id,
        origin=ScheduleOrigin.MANUAL,
        active=data.active,
    )
    db.add(row)
    db.flush()
    _record(db, principal, row, "create", after=_facts(row), request=request)
    _event(db, row, "created")
    return schedule_out(row)


def update_schedule(
    db: Session,
    principal: Principal,
    schedule_id: uuid.UUID,
    data: ScheduleUpdate,
    *,
    request: HTTPConnection | None,
) -> ScheduleOut:
    row = require_editable(db, principal, schedule_id)
    before = _facts(row)
    given = data.model_fields_set
    if data.name is not None:
        row.name = data.name
    if data.cadence is not None:
        row.cadence = data.cadence
    if data.interval_count is not None:
        row.interval_count = data.interval_count
    if data.next_due is not None:
        row.next_due = data.next_due
    low = _amount(data.amount_min, row.currency) if data.amount_min is not None else row.amount_min
    high = _amount(data.amount_max, row.currency) if data.amount_max is not None else row.amount_max
    _check_range(low, high)
    row.amount_min, row.amount_max = low, high
    if "payee_id" in given:
        _check_refs(db, row.household_id, data.payee_id, None)
        row.payee_id = data.payee_id
    if "category_id" in given:
        _check_refs(db, row.household_id, None, data.category_id)
        row.category_id = data.category_id
    if data.active is not None:
        row.active = data.active
    db.flush()
    _record(db, principal, row, "update", before=before, after=_facts(row), request=request)
    _event(db, row, "updated")
    return schedule_out(row)


def delete_schedule(
    db: Session, principal: Principal, schedule_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    row = require_editable(db, principal, schedule_id)
    _record(db, principal, row, "delete", before=_facts(row), request=request)
    _event(db, row, "deleted")
    db.delete(row)
    db.flush()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def observed(
    db: Session, household_id: uuid.UUID, account_ids: Collection[uuid.UUID], on: date
) -> list[Observed]:
    """The ledger rows detection reads: the look-back window, transfers excluded."""
    if not account_ids:
        return []
    rows = db.execute(
        select(
            Transaction.id,
            Transaction.account_id,
            Transaction.posted_date,
            Transaction.amount,
            Transaction.payee_id,
            Transaction.raw_payee,
            Transaction.category_id,
        ).where(
            Transaction.household_id == household_id,
            Transaction.account_id.in_(list(account_ids)),
            Transaction.posted_date > on - timedelta(days=detection.LOOKBACK_DAYS),
            Transaction.posted_date <= on,
            Transaction.transfer_group_id.is_(None),
        )
    )
    return [Observed(*row) for row in rows]


def _payee_names(
    db: Session, household_id: uuid.UUID, candidates: Iterable[Candidate]
) -> dict[uuid.UUID, str]:
    ids = {c.payee_id for c in candidates if c.payee_id is not None}
    if not ids:
        return {}
    rows = db.execute(
        select(Payee.id, Payee.name).where(Payee.household_id == household_id, Payee.id.in_(ids))
    )
    return {pid: name for pid, name in rows}  # noqa: C416 - Row tuples


def _name(candidate: Candidate, payees: dict[uuid.UUID, str]) -> str:
    if candidate.payee_id is not None and candidate.payee_id in payees:
        name = payees[candidate.payee_id]
    else:
        name = " ".join((candidate.label or "").split())
    return (name or DEFAULT_NAME)[:NAME_MAX]


def _match_rank(row: ScheduledTransaction, candidate: Candidate) -> int | None:
    """How well ``row`` matches ``candidate`` (lower is better), ``None`` for no match.

    Same account, cadence, interval and direction are required. Then: 0 the
    same payee (or, without a payee, the same normalised name) and
    overlapping amount ranges; 1 the same payee or name; 2 a payee-less
    **detected** schedule with an overlapping range (one a person renamed).
    """
    if row.account_id != candidate.account_id:
        return None
    if row.cadence is not candidate.cadence or row.interval_count != candidate.interval_count:
        return None
    if (row.amount_min >= 0) != candidate.inflow:
        return None
    overlaps = max(row.amount_min, candidate.amount_min) <= min(
        row.amount_max, candidate.amount_max
    )
    if candidate.payee_id is not None:
        same = row.payee_id == candidate.payee_id
    elif row.payee_id is not None:
        return None
    else:
        same = f"text:{detection.normalize_payee(row.name)}" == candidate.payee_key
    if same:
        return 0 if overlaps else 1
    if candidate.payee_id is None and row.origin is ScheduleOrigin.DETECTED and overlaps:
        return 2
    return None


def _existing(
    db: Session, household_id: uuid.UUID, account_ids: Collection[uuid.UUID]
) -> list[ScheduledTransaction]:
    if not account_ids:
        return []
    return list(
        db.execute(
            select(ScheduledTransaction)
            .where(
                ScheduledTransaction.household_id == household_id,
                ScheduledTransaction.account_id.in_(list(account_ids)),
            )
            .order_by(ScheduledTransaction.id)
        ).scalars()
    )


def _match(
    schedules: Sequence[ScheduledTransaction],
    candidate: Candidate,
    claimed: set[uuid.UUID],
) -> ScheduledTransaction | None:
    """The best match not yet claimed by another candidate of this run (manual first)."""
    ranked = [
        (rank, s)
        for s in schedules
        if s.id not in claimed and (rank := _match_rank(s, candidate)) is not None
    ]
    if not ranked:
        return None
    manual = [s for _, s in ranked if s.origin is ScheduleOrigin.MANUAL]
    if manual:
        return manual[0]
    return min(ranked, key=lambda pair: (pair[0], pair[1].id.bytes))[1]


def _candidate_out(
    candidate: Candidate, name: str, currency: str, schedule_id: uuid.UUID | None
) -> CandidateOut:
    return CandidateOut(
        account_id=candidate.account_id,
        payee_id=candidate.payee_id,
        name=name,
        pattern=candidate.pattern,
        cadence=candidate.cadence,
        interval_count=candidate.interval_count,
        amount_min=Money.of(candidate.amount_min, currency),
        amount_max=Money.of(candidate.amount_max, currency),
        category_id=candidate.category_id,
        occurrences=candidate.occurrences,
        first_seen=candidate.first_seen,
        last_seen=candidate.last_seen,
        next_due=candidate.next_due,
        regularity=f"{candidate.regularity:f}",
        confidence=f"{candidate.confidence:f}",
        schedule_id=schedule_id,
    )


def _currencies(
    db: Session, household_id: uuid.UUID, account_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not account_ids:
        return {}
    rows = db.execute(
        select(Account.id, Account.currency).where(
            Account.household_id == household_id, Account.id.in_(list(account_ids))
        )
    )
    return {aid: cur for aid, cur in rows}  # noqa: C416 - Row tuples


def candidates(
    db: Session, principal: Principal, *, account_ids: Collection[uuid.UUID] | None = None
) -> CandidateList:
    """Detection over the visible accounts; writes nothing."""
    household_id = _household(principal, HouseholdRole.VIEWER)
    if account_ids:
        require_visible_accounts(db, principal, account_ids)
        scope = sorted(set(account_ids), key=lambda u: u.bytes)
    else:
        scope = visible_account_ids(db, principal)
    on = today()
    found = detection.detect(observed(db, household_id, scope, on), on)
    payees = _payee_names(db, household_id, found)
    currencies = _currencies(db, household_id, scope)
    schedules = _existing(db, household_id, scope)
    items = []
    claimed: set[uuid.UUID] = set()
    for candidate in found:
        match = _match(schedules, candidate, claimed)
        if match is not None:
            claimed.add(match.id)
        items.append(
            _candidate_out(
                candidate,
                _name(candidate, payees),
                currencies[candidate.account_id],
                match.id if match is not None else None,
            )
        )
    return CandidateList(as_of=on, items=items)


@dataclass
class DetectCounts:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_manual: int = 0


def persist(
    db: Session, household_id: uuid.UUID, account_ids: Collection[uuid.UUID], on: date
) -> tuple[DetectCounts, list[CandidateOut]]:
    """Detect on ``account_ids`` and write detected schedules (module doc)."""
    found = detection.detect(observed(db, household_id, account_ids, on), on)
    payees = _payee_names(db, household_id, found)
    currencies = _currencies(db, household_id, account_ids)
    schedules = _existing(db, household_id, account_ids)
    counts = DetectCounts()
    items: list[CandidateOut] = []
    claimed: set[uuid.UUID] = set()
    for candidate in found:
        name = _name(candidate, payees)
        currency = currencies[candidate.account_id]
        match = _match(schedules, candidate, claimed)
        if match is not None:
            claimed.add(match.id)
        if match is not None and match.origin is ScheduleOrigin.MANUAL:
            counts.skipped_manual += 1
            items.append(_candidate_out(candidate, name, currency, match.id))
            continue
        if match is None:
            row = ScheduledTransaction(
                household_id=household_id,
                account_id=candidate.account_id,
                name=name,
                cadence=candidate.cadence,
                interval_count=candidate.interval_count,
                next_due=candidate.next_due,
                amount_min=to_storage(candidate.amount_min),
                amount_max=to_storage(candidate.amount_max),
                currency=currency,
                payee_id=candidate.payee_id,
                category_id=candidate.category_id,
                origin=ScheduleOrigin.DETECTED,
                active=True,
                last_seen_on=candidate.last_seen,
            )
            db.add(row)
            db.flush()
            schedules.append(row)
            claimed.add(row.id)
            counts.created += 1
            _event(db, row, "created")
            items.append(_candidate_out(candidate, name, currency, row.id))
            continue
        wanted = {
            "amount_min": to_storage(candidate.amount_min),
            "amount_max": to_storage(candidate.amount_max),
            "next_due": candidate.next_due,
            "last_seen_on": candidate.last_seen,
        }
        if match.category_id is None and candidate.category_id is not None:
            wanted["category_id"] = candidate.category_id
        changed = {k: v for k, v in wanted.items() if getattr(match, k) != v}
        if changed:
            for key, value in changed.items():
                setattr(match, key, value)
            counts.updated += 1
            _event(db, match, "updated")
        else:
            counts.unchanged += 1
        items.append(_candidate_out(candidate, name, currency, match.id))
    db.flush()
    return counts, items


def detect(
    db: Session, principal: Principal, data: DetectIn, *, request: HTTPConnection | None
) -> DetectResult:
    household_id = _household(principal, HouseholdRole.MEMBER)
    if data.account_ids:
        require_visible_accounts(db, principal, data.account_ids, need="edit")
        scope = sorted(set(data.account_ids), key=lambda u: u.bytes)
    else:
        scope = visible_account_ids(db, principal, need="edit")
    on = today()
    counts, items = persist(db, household_id, scope, on)
    audit(
        db,
        action="schedule.detect",
        target_type="household",
        target_id=household_id,
        after={"accounts": len(scope), **counts.__dict__},
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    return DetectResult(as_of=on, items=items, **counts.__dict__)


def detect_household(db: Session, household_id: uuid.UUID, on: date) -> DetectCounts:
    """The daily job: detection over every account of the household."""
    account_ids = list(
        db.execute(select(Account.id).where(Account.household_id == household_id)).scalars()
    )
    counts, _ = persist(db, household_id, account_ids, on)
    return counts


# ---------------------------------------------------------------------------
# Cash-flow calendar
# ---------------------------------------------------------------------------


def calendar(
    db: Session,
    principal: Principal,
    *,
    days: int,
    account_ids: Collection[uuid.UUID] | None = None,
) -> CashflowCalendar:
    household_id = _household(principal, HouseholdRole.VIEWER)
    start = today()
    end = start + timedelta(days=days - 1)
    base = base_currency(db, household_id)
    stmt = _visible(principal).where(ScheduledTransaction.active.is_(True))
    account_stmt = select(Account).where(
        Account.household_id == household_id, visible_account_filter(principal, Account.id)
    )
    if account_ids:
        require_visible_accounts(db, principal, account_ids)
        stmt = stmt.where(ScheduledTransaction.account_id.in_(list(account_ids)))
        account_stmt = account_stmt.where(Account.id.in_(list(account_ids)))
    schedules = list(db.execute(stmt.order_by(ScheduledTransaction.id)).scalars())
    accounts = {a.id: a for a in db.execute(account_stmt).scalars()}
    fx = FxConverter(db, household_id, base)

    liquid = [a for a in accounts.values() if a.type in LIQUID_TYPES and a.include_in_net_worth]
    opening = ZERO
    for account_id, balance in balances_as_of(db, liquid, start).items():
        converted = fx.try_convert(balance, accounts[account_id].currency, start)
        if converted is not None:
            opening += converted

    unconverted: list[uuid.UUID] = []
    by_day: dict[date, list[tuple[CalendarEvent, Decimal | None, bool]]] = defaultdict(list)
    for row in schedules:
        expected = midpoint(row.amount_min, row.amount_max)
        base_amount = fx.try_convert(expected, row.currency, start)
        if base_amount is None:
            unconverted.append(row.id)
        account = accounts.get(row.account_id)
        is_liquid = account is not None and account.type in LIQUID_TYPES
        for day in occurrences(row.next_due, row.cadence, row.interval_count, start, end):
            event = CalendarEvent(
                schedule_id=row.id,
                name=row.name,
                account_id=row.account_id,
                kind="income" if expected > 0 else "bill",
                amount=Money.of(expected, row.currency),
                amount_min=Money.of(row.amount_min, row.currency),
                amount_max=Money.of(row.amount_max, row.currency),
                amount_base=Money.of(base_amount, base) if base_amount is not None else None,
            )
            by_day[day].append((event, base_amount, is_liquid))

    out: list[CalendarDay] = []
    income_total = bills_total = ZERO
    balance = opening
    for day in sorted(by_day):
        entries = sorted(by_day[day], key=lambda e: (e[0].name, e[0].schedule_id.bytes))
        income = sum((b for _, b, _ in entries if b is not None and b > 0), ZERO)
        bills = sum((-b for _, b, _ in entries if b is not None and b < 0), ZERO)
        balance += sum((b for _, b, liquid_ in entries if b is not None and liquid_), ZERO)
        income_total += income
        bills_total += bills
        out.append(
            CalendarDay(
                on=day,
                events=[e for e, _, _ in entries],
                income=Money.of(income, base),
                bills=Money.of(bills, base),
                net=Money.of(income - bills, base),
                balance=Money.of(balance, base),
            )
        )
    return CashflowCalendar(
        currency=base,
        date_from=start,
        date_to=end,
        opening_balance=Money.of(opening, base),
        days=out,
        income=Money.of(income_total, base),
        bills=Money.of(bills_total, base),
        net=Money.of(income_total - bills_total, base),
        unconverted=unconverted,
    )

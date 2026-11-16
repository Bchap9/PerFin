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

Detection (``app.domain.recurring.detection``) feeds schedules as follows.
The rows it reads are those of the look-back window that are not
transfer-linked, each with its merchant key (the stored one, else read from
the descriptor on the fly), its descriptor kind and its category. The clock
is the data: per account, ``as_of`` is the day of its latest row (never
later than today), so a series is not "stopped" because nobody uploaded the
next statement yet.

A candidate is matched to an existing series of the same account and
direction (money in or out), best first: (0) the same merchant key and
overlapping amounts; (1) the same merchant key; (2) a series without a key
(from before merchant keys, or entered by hand) with the same payee or the
same normalised name and overlapping amounts; (3) the series most of the
candidate's already linked charges belong to (a series without a key that
a person renamed, or one whose key was recomputed). Cadence is not part of the
match: a plan that moves from monthly to annual is the same series. The best
pairs are taken first and a series matches at most one candidate per run.

* A **dismissed** match: the candidate is dropped. A dismissed series never
  returns and no second one is created for it. It matches on (0) and (2),
  and on (1) only when the candidate is that series at a new price: its
  charges ran on from the dismissed amount without a break (the tombstone
  then takes the new amount range). A plan the merchant starts after the
  dismissed one stopped is not that series: it is created, as ``suggested``
  whatever the evidence, and stays a suggestion until a person decides, like
  every other series detection finds while a dismissed one of the same
  merchant (or name) stands on the account. So is one found under a key a
  dismissed or ended series' rows went to when a re-key split its merchant
  (``split_keys``, written by ``categorisation.service``), and that one is
  not announced as new either.
* A **manual** match wins over a detected one and is never rewritten
  (``skipped_manual``); its charges are linked to it.
* Any other match has its facts refreshed (the cadence, the amount range,
  ``next_due``, when it was first and last seen, how many charges, the
  confidence, a category when it had none, and the merchant key when the
  candidate has one). Its name is left alone. The latest amount is written
  by ``subscriptions.settle``, which reads a price change from it. Its ``kind`` is
  written only while the detector decided it; its ``status`` only while the
  detector decided it, and then only upwards: ``suggested`` may become
  ``active``, never the reverse. What a person paused, ended or confirmed
  stays so.
* No match: a detected series is created with the candidate's kind and
  status (``active`` or ``suggested``), both decided by the detector.

What detection found once and finds no more is settled too. Its charges, and
the rows of the same group (``detection.group_key``: the merchant, or for a
series without a merchant key the person paid or the text) that belong to no
series, tell whether it still charges.

The charges of a candidate are linked to its series
(``transaction.scheduled_transaction_id``) through the pinned write, which
leaves ``updated_at`` alone: a linked row still reads as untouched since its
import. A charge that is no longer part of the series is not unlinked.

What changed about a series (a new one, a price, a missed charge, the end, a
charge after a person ended it, a trial that became a paid plan) is read off
in the same run by ``app.domain.recurring.subscriptions.settle``, for every
detected series whether detection found it again or not; that module says
what is written and who is told. The one status the detector takes from a
person is there: a confirmed series whose charges stopped is ended.

A person's word: ``PATCH`` with ``kind`` sets the kind for good; ``active:
false`` pauses the series and ``active: true`` confirms it. ``DELETE`` on a
detected series dismisses it (the row stays as the tombstone, its charges
are unlinked, and the list no longer shows it); on a manual series it
deletes the row. ``active`` is always ``status in (active, confirmed)``.

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

Writes are audited (``schedule.create|update|delete|detect``; names and
merchant keys stay out of the audit trail) and emit ``schedule.created|
updated|deleted`` with ids and the ``account_id`` only.
"""

from __future__ import annotations

import itertools
import uuid
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, cast

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session, aliased

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
from app.domain.categorisation import engine as categorisation
from app.domain.categorisation.pinned import pinned_update
from app.domain.fx.service import FxConverter, base_currency
from app.domain.recurring import detection, subscriptions
from app.domain.recurring.detection import Candidate, Observed
from app.domain.recurring.schemas import (
    MAX_SCHEDULES,
    CalendarDay,
    CalendarEvent,
    CandidateList,
    CandidateOut,
    CashflowCalendar,
    Decider,
    DetectIn,
    DetectResult,
    ScheduleCreate,
    ScheduleList,
    ScheduleOut,
    ScheduleUpdate,
    SeriesKind,
    SeriesStatus,
)
from app.domain.taxonomy import repo as taxonomy_repo
from app.domain.transactions.models import (
    ACTIVE_STATUSES,
    NAME_MAX,
    Category,
    Payee,
    ScheduledTransaction,
    ScheduleOrigin,
    Transaction,
)

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

LIQUID_TYPES: Final = frozenset({AccountType.CHECKING, AccountType.SAVINGS, AccountType.CASH})
DEFAULT_NAME: Final = "Recurring"
DISMISSED: Final = "dismissed"
ENDED: Final = "ended"
#: An account, detection's group key and the direction (money in).
type _GroupKey = tuple[uuid.UUID, str, bool]
DETECTOR: Final = "detector"
PERSON: Final = "person"
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
        kind=cast("SeriesKind", row.kind),
        status=cast("SeriesStatus", row.status),
        status_source=cast("Decider", row.status_source),
        merchant=categorisation.display_name(row.merchant_key),
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
        "kind": row.kind,
        "status": row.status,
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
    """Schedules of visible accounts, soonest first; a dismissed one is not listed."""
    stmt = _visible(principal).where(ScheduledTransaction.status != DISMISSED)
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
    if data.kind is not None:
        row.kind, row.kind_source = data.kind, PERSON
    if data.active is not None and not (row.status_source == PERSON and row.active == data.active):
        # A person's word: switched off is a pause, switched on a confirmation.
        # What a person already decided the same way (ended, say) stays as it is.
        _set_status(row, "confirmed" if data.active else "paused", PERSON)
    db.flush()
    _record(db, principal, row, "update", before=before, after=_facts(row), request=request)
    _event(db, row, "updated")
    return schedule_out(row)


def delete_schedule(
    db: Session, principal: Principal, schedule_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    """Delete a manual schedule; dismiss a detected one (module doc)."""
    row = require_editable(db, principal, schedule_id)
    before = _facts(row)
    if row.origin is ScheduleOrigin.DETECTED:
        _set_status(row, DISMISSED, PERSON)
        unlink_charges(db, row)
        db.flush()
        _record(db, principal, row, "delete", before=before, after=_facts(row), request=request)
        _event(db, row, "deleted")
        return
    _record(db, principal, row, "delete", before=before, request=request)
    _event(db, row, "deleted")
    db.delete(row)
    db.flush()


def _set_status(row: ScheduledTransaction, status: str, decider: str) -> None:
    """``status`` with who set it; ``active`` follows."""
    row.status = status
    row.status_source = decider
    row.active = status in ACTIVE_STATUSES


def unlink_charges(db: Session, row: ScheduledTransaction) -> int:
    """Take every charge off ``row`` (pinned: the rows stay untouched)."""
    ids = db.execute(
        select(Transaction.id).where(
            Transaction.household_id == row.household_id,
            Transaction.scheduled_transaction_id == row.id,
        )
    ).scalars()
    return pinned_update(db, row.household_id, {i: {"scheduled_transaction_id": None} for i in ids})


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ledger:
    """What one detection run reads (module doc)."""

    rows: list[Observed]
    #: Per account, the day of its latest row, never later than the run's day.
    as_of: dict[uuid.UUID, date]
    #: The series each read row is linked to, if any.
    links: dict[uuid.UUID, uuid.UUID | None]


def _read(
    db: Session, household_id: uuid.UUID, account_ids: Collection[uuid.UUID], on: date
) -> _Ledger:
    if not account_ids:
        return _Ledger([], {}, {})
    accounts = list(account_ids)
    latest = db.execute(
        select(Transaction.account_id, func.max(Transaction.posted_date))
        .where(Transaction.household_id == household_id, Transaction.account_id.in_(accounts))
        .group_by(Transaction.account_id)
    )
    as_of = {account_id: min(on, day) for account_id, day in latest}
    suggested = aliased(Category)
    found = db.execute(
        select(
            Transaction.id,
            Transaction.account_id,
            Transaction.posted_date,
            Transaction.amount,
            Transaction.payee_id,
            Transaction.raw_payee,
            Transaction.category_id,
            Transaction.merchant_key,
            Transaction.scheduled_transaction_id,
            Category.system_key,
            Category.kind,
            suggested.system_key,
        )
        .outerjoin(
            Category,
            and_(
                Category.household_id == Transaction.household_id,
                Category.id == Transaction.category_id,
            ),
        )
        .outerjoin(
            suggested,
            and_(
                suggested.household_id == Transaction.household_id,
                suggested.id == Transaction.suggested_category_id,
            ),
        )
        .where(
            Transaction.household_id == household_id,
            Transaction.account_id.in_(accounts),
            Transaction.posted_date > on - timedelta(days=detection.LOOKBACK_DAYS),
            Transaction.posted_date <= on,
            Transaction.transfer_group_id.is_(None),
        )
    )
    identities: dict[str | None, categorisation.Identity] = {}
    rows: list[Observed] = []
    links: dict[uuid.UUID, uuid.UUID | None] = {}
    for (
        row_id,
        account_id,
        posted_date,
        amount,
        payee_id,
        raw_payee,
        category_id,
        merchant_key,
        schedule_id,
        category_key,
        category_kind,
        suggested_key,
    ) in found:
        identity = identities.get(raw_payee)
        if identity is None:
            identity = identities[raw_payee] = categorisation.identify(raw_payee)
        merchant = identity.hit.merchant if identity.hit is not None else None
        links[row_id] = schedule_id
        rows.append(
            Observed(
                id=row_id,
                account_id=account_id,
                posted_date=posted_date,
                amount=amount,
                # A row the engine has not keyed yet is identified on the fly.
                merchant_key=merchant_key or identity.key,
                text_key=detection.normalize_payee(raw_payee) or None,
                kind=identity.kind,
                category_key=category_key,
                category_kind=category_kind.value if category_kind is not None else None,
                series_hint=merchant.series if merchant is not None else None,
                payee_id=payee_id,
                category_id=category_id,
                label=raw_payee,
                cadence_hints=frozenset(merchant.cadences) if merchant is not None else frozenset(),
                habit_hint=merchant is not None and merchant.mixed and merchant.series is None,
                suggested_key=suggested_key,
            )
        )
    return _Ledger(rows, as_of, links)


def observed(
    db: Session, household_id: uuid.UUID, account_ids: Collection[uuid.UUID], on: date
) -> tuple[list[Observed], dict[uuid.UUID, date]]:
    """The ledger rows detection reads, and each account's data clock (module doc)."""
    ledger = _read(db, household_id, account_ids, on)
    return ledger.rows, ledger.as_of


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
    """The payee's name, else the merchant's, else the latest statement text."""
    if candidate.payee_id is not None and candidate.payee_id in payees:
        name = payees[candidate.payee_id]
    else:
        name = categorisation.display_name(candidate.merchant_key) or " ".join(
            (candidate.label or "").split()
        )
    return (name or DEFAULT_NAME)[:NAME_MAX]


def _overlaps(row: ScheduledTransaction, candidate: Candidate) -> bool:
    """Whether the amount ranges of ``row`` and ``candidate`` overlap."""
    return max(row.amount_min, candidate.amount_min) <= min(row.amount_max, candidate.amount_max)


def _linked(
    candidate: Candidate, links: Mapping[uuid.UUID, uuid.UUID | None]
) -> Counter[uuid.UUID]:
    """How many of the candidate's charges each series already holds."""
    return Counter(
        schedule_id
        for row_id in candidate.transaction_ids
        if (schedule_id := links.get(row_id)) is not None
    )


def _match_rank(
    row: ScheduledTransaction, candidate: Candidate, *, holds_most: bool = False
) -> int | None:
    """How well ``row`` matches ``candidate`` (lower is better), ``None`` for no match.

    The same account and direction are required; the cadence is not looked
    at. Then: 0 the same merchant key and overlapping amount ranges; 1 the
    same merchant key; 2 a series without a key that has the same payee or
    the same normalised name, and overlapping ranges; 3 ``row`` holds most of
    the candidate's charges that are linked to a series at all
    (``holds_most``).
    """
    if row.account_id != candidate.account_id:
        return None
    if (row.amount_min >= 0) != candidate.inflow:
        return None
    overlaps = _overlaps(row, candidate)
    if row.merchant_key is not None and row.merchant_key == candidate.merchant_key:
        return 0 if overlaps else 1
    if row.merchant_key is None and overlaps:
        same_payee = candidate.payee_id is not None and row.payee_id == candidate.payee_id
        same_name = (
            candidate.text_key is not None
            and detection.normalize_payee(row.name) == candidate.text_key
        )
        if same_payee or same_name:
            return 2
    return 3 if holds_most else None


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


def _continues(
    row: ScheduledTransaction, candidate: Candidate, rows: Mapping[uuid.UUID, Observed]
) -> bool:
    """Whether ``candidate`` is the series ``row`` at a new price.

    True when the candidate's charges leave the amount range of ``row`` from
    one charge to the next without a break (at most one and a half cycles
    plus the grace apart): a price that was raised. A plan that starts after
    the old one stopped is another series.
    """
    cycle = (candidate.cadence, candidate.interval_count)
    longest = subscriptions.cycle_days(*cycle) * 3 // 2 + subscriptions.grace_days(*cycle)
    charges = sorted(
        (rows[row_id] for row_id in candidate.transaction_ids if row_id in rows),
        key=lambda charge: (charge.posted_date, charge.id.bytes),
    )
    left: bool | None = None
    for before, after in itertools.pairwise(charges):
        inside = row.amount_min <= before.amount <= row.amount_max
        if inside and not row.amount_min <= after.amount <= row.amount_max:
            left = (after.posted_date - before.posted_date).days <= longest
    return bool(left)


def _match(
    schedules: Sequence[ScheduledTransaction],
    found: Sequence[Candidate],
    links: Mapping[uuid.UUID, uuid.UUID | None],
    rows: Mapping[uuid.UUID, Observed],
) -> dict[int, ScheduledTransaction]:
    """The series of each candidate that has one, by the candidate's index.

    Every possible pair is ranked (:func:`_match_rank`) and the best are taken
    first: a manual series before a detected one, then the rank, then the
    series holding more of the candidate's charges. A series matches at most
    one candidate, and a candidate at most one series. A dismissed series
    matches by its merchant key alone (rank 1) only a candidate that
    continues it (:func:`_continues`).
    """
    pairs: list[tuple[bool, int, int, bytes, int, ScheduledTransaction]] = []
    for index, candidate in enumerate(found):
        linked = _linked(candidate, links)
        total = linked.total()
        for schedule in schedules:
            count = linked.get(schedule.id, 0)
            rank = _match_rank(schedule, candidate, holds_most=2 * count > total)
            if rank == 1 and schedule.status == DISMISSED:
                rank = 1 if _continues(schedule, candidate, rows) else None
            if rank is not None:
                detected = schedule.origin is not ScheduleOrigin.MANUAL
                pairs.append((detected, rank, -count, schedule.id.bytes, index, schedule))
    pairs.sort(key=lambda pair: pair[:5])
    out: dict[int, ScheduledTransaction] = {}
    claimed: set[uuid.UUID] = set()
    for *_, index, schedule in pairs:
        if index in out or schedule.id in claimed:
            continue
        out[index] = schedule
        claimed.add(schedule.id)
    return out


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
    """Detection over the visible accounts; writes nothing.

    A candidate that matches a dismissed series is left out, as in a real run.
    """
    household_id = _household(principal, HouseholdRole.VIEWER)
    if account_ids:
        require_visible_accounts(db, principal, account_ids)
        scope = sorted(set(account_ids), key=lambda u: u.bytes)
    else:
        scope = visible_account_ids(db, principal)
    on = today()
    ledger = _read(db, household_id, scope, on)
    found = detection.detect(ledger.rows, ledger.as_of)
    payees = _payee_names(db, household_id, found)
    currencies = _currencies(db, household_id, scope)
    matches = _match(
        _existing(db, household_id, scope), found, ledger.links, {r.id: r for r in ledger.rows}
    )
    items = []
    for index, candidate in enumerate(found):
        match = matches.get(index)
        if match is not None and match.status == DISMISSED:
            continue
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
    #: Candidates dropped because a person dismissed their series.
    skipped_dismissed: int = 0


def _shadowed(schedules: Iterable[ScheduledTransaction], candidate: Candidate) -> bool:
    """Whether a person dismissed a series of the candidate's merchant (or name) on the account."""
    return any(
        row.status == DISMISSED and _match_rank(row, candidate) is not None for row in schedules
    )


def _split_from(schedules: Iterable[ScheduledTransaction], candidate: Candidate) -> bool:
    """Whether the candidate's merchant is one a dismissed or ended series' rows went to.

    A re-key that split a merchant (``ScheduledTransaction.split_keys``)
    leaves the series under one key; what is found under another of them on
    the same account and direction is not new.
    """
    return candidate.merchant_key is not None and any(
        row.status in (DISMISSED, ENDED)
        and row.account_id == candidate.account_id
        and (row.amount_min >= 0) == candidate.inflow
        and candidate.merchant_key in row.split_keys
        for row in schedules
    )


def _create(
    db: Session,
    household_id: uuid.UUID,
    candidate: Candidate,
    name: str,
    currency: str,
    *,
    status: str,
) -> ScheduledTransaction:
    """A detected series as the detector found it (``status``: the candidate's, or less)."""
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
        active=status in ACTIVE_STATUSES,
        last_seen_on=candidate.last_seen,
        kind=candidate.kind,
        kind_source=DETECTOR,
        status=status,
        status_source=DETECTOR,
        merchant_key=candidate.merchant_key,
        first_seen_on=candidate.first_seen,
        occurrences=candidate.occurrences,
        confidence=candidate.confidence,
        last_amount=to_storage(candidate.last_amount),
    )
    db.add(row)
    db.flush()
    return row


def _refresh(row: ScheduledTransaction, candidate: Candidate, *, rise: bool = True) -> bool:
    """Write what detection found onto a matched series; whether anything changed.

    Facts are always refreshed (the latest amount by ``subscriptions.settle``,
    which runs first). The kind and the status are the detector's to write
    only while no person decided them, and the status only rises; not even
    that without ``rise`` (a dismissed series of the same merchant stands).
    """
    first_seen = min(row.first_seen_on or candidate.first_seen, candidate.first_seen)
    wanted: dict[str, Any] = {
        "cadence": candidate.cadence,
        "interval_count": candidate.interval_count,
        "next_due": candidate.next_due,
        "amount_min": to_storage(candidate.amount_min),
        "amount_max": to_storage(candidate.amount_max),
        "first_seen_on": first_seen,
        "last_seen_on": candidate.last_seen,
        "occurrences": candidate.occurrences,
        "confidence": candidate.confidence,
    }
    if candidate.merchant_key is not None:
        wanted["merchant_key"] = candidate.merchant_key  # a series from before keys takes it
    if row.category_id is None and candidate.category_id is not None:
        wanted["category_id"] = candidate.category_id
    if row.kind_source == DETECTOR:
        wanted["kind"] = candidate.kind
    changed = {k: v for k, v in wanted.items() if getattr(row, k) != v}
    for key, value in changed.items():
        setattr(row, key, value)
    rises = (
        rise
        and row.status_source == DETECTOR
        and row.status == "suggested"
        and candidate.status == "active"
    )
    if rises:
        _set_status(row, "active", DETECTOR)
    return bool(changed) or rises


def _settled(row: ScheduledTransaction) -> tuple[Any, ...]:
    """The columns ``subscriptions.settle`` may write."""
    return (
        row.status,
        row.status_source,
        row.ended_on,
        row.last_amount,
        row.previous_amount,
        row.price_changed_on,
    )


def _first_run(db: Session, household_id: uuid.UUID) -> bool:
    """Whether the household has no series with a merchant key yet (a quiet run)."""
    keyed = db.execute(
        select(ScheduledTransaction.id)
        .where(
            ScheduledTransaction.household_id == household_id,
            ScheduledTransaction.merchant_key.is_not(None),
        )
        .limit(1)
    ).first()
    return keyed is None


def _charge(row: Observed) -> subscriptions.Charge:
    return subscriptions.Charge(row.id, row.posted_date, row.amount)


def persist(
    db: Session, household_id: uuid.UUID, account_ids: Collection[uuid.UUID], on: date
) -> tuple[DetectCounts, list[CandidateOut]]:
    """Detect on ``account_ids``, write the series, link their charges and
    settle what changed (module doc)."""
    quiet = _first_run(db, household_id)
    ledger = _read(db, household_id, account_ids, on)
    found = detection.detect(ledger.rows, ledger.as_of)
    payees = _payee_names(db, household_id, found)
    currencies = _currencies(db, household_id, account_ids)
    existing = _existing(db, household_id, account_ids)
    by_id = {row.id: row for row in ledger.rows}
    matches = _match(existing, found, ledger.links, by_id)
    counts = DetectCounts()
    items: list[CandidateOut] = []
    charges: dict[uuid.UUID, dict[str, Any]] = {}
    #: The series each read row belongs to once this run's links are written.
    owner: dict[uuid.UUID, uuid.UUID] = {
        row_id: schedule_id for row_id, schedule_id in ledger.links.items() if schedule_id
    }
    resolved: list[tuple[Candidate, ScheduledTransaction, bool]] = []
    #: Series that stay a suggestion: a dismissed one of the same merchant stands.
    held: set[uuid.UUID] = set()
    #: ...of which these are not announced as new: they are a person's
    #: dismissed or ended series under another of its merchant's keys.
    unannounced: set[uuid.UUID] = set()
    for index, candidate in enumerate(found):
        match = matches.get(index)
        if match is not None and match.status == DISMISSED:
            counts.skipped_dismissed += 1
            if not _overlaps(match, candidate):  # the tombstone follows its price
                match.amount_min = to_storage(candidate.amount_min)
                match.amount_max = to_storage(candidate.amount_max)
            continue
        name = _name(candidate, payees)
        currency = currencies[candidate.account_id]
        split_from = _split_from(existing, candidate)
        shadowed = split_from or _shadowed(existing, candidate)
        if match is None:
            status = "suggested" if shadowed else candidate.status
            row = _create(db, household_id, candidate, name, currency, status=status)
            counts.created += 1
            _event(db, row, "created")
        else:
            row = match
        if shadowed:
            held.add(row.id)
        if split_from:
            unannounced.add(row.id)
        for row_id in candidate.transaction_ids:
            if ledger.links.get(row_id) != row.id:
                charges[row_id] = {"scheduled_transaction_id": row.id}
            owner[row_id] = row.id
        resolved.append((candidate, row, match is None))
        items.append(_candidate_out(candidate, name, currency, row.id))

    own: dict[uuid.UUID, list[subscriptions.Charge]] = defaultdict(list)
    #: The groups (account, detection's group key, money in) of each series' charges...
    groups: dict[uuid.UUID, set[_GroupKey]] = defaultdict(set)
    #: ...and per group the rows that belong to no series.
    loose: dict[_GroupKey, list[subscriptions.Charge]] = defaultdict(list)
    for observed_row in ledger.rows:
        schedule_id = owner.get(observed_row.id)
        group = detection.group_key(observed_row)
        key = (observed_row.account_id, group or "", observed_row.amount > 0)
        if schedule_id is not None:
            own[schedule_id].append(_charge(observed_row))
            if group is not None:
                groups[schedule_id].add(key)
        elif group is not None:
            loose[key].append(_charge(observed_row))

    def settle(row: ScheduledTransaction, candidate: Candidate | None, created: bool) -> bool:
        """Settle ``row``; whether one of its columns changed."""
        as_of = ledger.as_of.get(row.account_id)
        if as_of is None:
            return False
        before = _settled(row)
        # A series without a merchant key (a person paid, or a text) has its group
        # from its charges.
        keys = set(groups.get(row.id, ()))
        if row.merchant_key is not None:
            keys.add((row.account_id, f"merchant:{row.merchant_key}", row.amount_min >= 0))
        events = subscriptions.settle(
            db,
            household_id,
            row,
            candidate,
            own.get(row.id, ()),
            as_of,
            quiet=quiet,
            created=created,
            announce_new=row.id not in unannounced,
            loose=[charge for key in sorted(keys) for charge in loose.get(key, ())],
        )
        for event in events:
            # One stray charge after a person's "ended" is filed with its series.
            stray = event.transaction_id if event.kind == "charged_after_end" else None
            if stray is not None and owner.get(stray) != row.id:
                charges[stray] = {"scheduled_transaction_id": row.id}
        return _settled(row) != before

    found_again: set[uuid.UUID] = set()
    for candidate, row, created in resolved:
        found_again.add(row.id)
        if row.origin is ScheduleOrigin.MANUAL:
            counts.skipped_manual += 1
            continue
        settled = settle(row, candidate, created)
        if created:
            continue
        refreshed = _refresh(row, candidate, rise=row.id not in held)
        if settled or refreshed:
            counts.updated += 1
            _event(db, row, "updated")
        else:
            counts.unchanged += 1
    for row in existing:
        # A series detection did not find this time: missed, or ended.
        if row.id not in found_again and settle(row, None, False):
            counts.updated += 1
            _event(db, row, "updated")
    db.flush()
    pinned_update(db, household_id, charges)
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
    return DetectResult(
        as_of=on,
        items=items,
        created=counts.created,
        updated=counts.updated,
        unchanged=counts.unchanged,
        skipped_manual=counts.skipped_manual,
    )


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

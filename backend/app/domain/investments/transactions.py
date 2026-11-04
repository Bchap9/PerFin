# SPDX-License-Identifier: AGPL-3.0-or-later
"""Investment transactions and account cost methods (API.md ``/investments/transactions``).

Who may do what (routers check role and token scope first; every function
here checks again):

* **Read** (any role, ``investments:read``): transactions of accounts the
  caller can see (``app.domain.accounts.visibility``); any other is ``404
  investment-transaction-not-found``. An account or security filter the
  caller cannot see is ``404 account-not-found`` / ``security-not-found``.
* **Write** (member or above, ``investments:write``): only on accounts the
  caller may **edit** (owner/admin, or a member's ``edit`` grant); an
  account they cannot edit is ``404 account-not-found`` (create) and a
  transaction on it ``404 investment-transaction-not-found`` (change,
  delete). The account must be a brokerage or retirement account (``422
  not-an-investment-account``) and the security one the caller can see.

Rules per type (``422 invalid-investment-transaction`` otherwise; signs are
the cash effect on the account, DATA_MODEL.md "Investments"):

=================  ========  ========  ==========================  =========
type               security  quantity  amount                      other
=================  ========  ========  ==========================  =========
buy                required  > 0       <= 0 (cost incl. fees)
sell               required  > 0       >= 0 (net proceeds)          ``lots``
reinvest           required  > 0       <= 0, or 0 with ``price``
transfer_in        required  > 0       <= 0 (fees)                  ``price`` = cost per unit
transfer_out       required  > 0       <= 0 (fees)                  ``lots``
dividend/interest  optional  none      any
fee                optional  none      any (a fee is negative)
split              required  none      0                            ``ratio``
corporate_action   optional  none      any (cash)                   ``ratio`` (needs a security)
=================  ========  ========  ==========================  =========

``amount`` and ``fees`` (>= 0) are in the account's currency (``422
currency-mismatch``), ``price`` in the security's. ``trade_date`` is not in
the future (``422 trade-date-in-future``) and ``settle_date`` not before
it. ``lots`` (specific lots, sell/transfer_out only, ``fifo`` accounts only)
names open lots of the same account and security whose quantities add up to
the transaction's. Every write replays the touched (account, security)
pairs through the lot engine (``service.replay_position``); what the lots
cannot satisfy is ``422`` with the engine's type (``insufficient-quantity``,
``lot-not-available``, ``lot-links-mismatch``, ``lot-selection-not-allowed``);
for a delete it is ``409`` with the same types.

Every write is audited (``investment_txn.create|update|delete``,
``investment_account.cost_method``; the memo's text never goes to the audit
trail) and emits ``investment.txn_created|txn_updated|txn_deleted`` /
``investment.cost_method_changed`` with ids only and the ``account_id``
(so only callers who see the account hear about it).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select, tuple_
from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.money import Money, MoneyIn
from app.domain.accounts.models import Account
from app.domain.accounts.visibility import (
    get_visible_account,
    visible_account_filter,
)
from app.domain.investments import lots as engine
from app.domain.investments import service
from app.domain.investments.lots import format_units
from app.domain.investments.models import (
    QUANTITY_TXN_TYPES,
    SECURITY_TXN_TYPES,
    CostMethod,
    InvestmentAccount,
    InvestmentTxn,
    InvestmentTxnType,
    Lot,
)
from app.domain.investments.schemas import (
    CostMethodOut,
    CostMethodSet,
    InvestmentTxnCreate,
    InvestmentTxnFields,
    InvestmentTxnOut,
    InvestmentTxnPage,
    InvestmentTxnUpdate,
    LotPickOut,
)
from app.domain.investments.service import LotLink, dump_lot_links, parse_lot_links

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

T = InvestmentTxnType
_AMOUNT_NOT_POSITIVE = frozenset({T.BUY, T.REINVEST, T.TRANSFER_IN, T.TRANSFER_OUT})
_NO_QUANTITY = frozenset({T.DIVIDEND, T.INTEREST, T.FEE, T.SPLIT, T.CORPORATE_ACTION})


def txn_not_found() -> ProblemError:
    return ProblemError(404, "investment-transaction-not-found", "Investment transaction not found")


def _invalid(detail: str) -> ProblemError:
    return ProblemError(
        422, "invalid-investment-transaction", "Invalid investment transaction", detail
    )


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return principal.household_id


def _investment_account(
    db: Session, principal: Principal, account_id: uuid.UUID, *, lock: bool = False
) -> Account:
    account = get_visible_account(db, principal, account_id, need="edit", lock=lock)
    if account.type not in service.INVESTMENT_ACCOUNT_TYPES:
        raise ProblemError(
            422,
            "not-an-investment-account",
            "Not an investment account",
            "Investment transactions belong to brokerage and retirement accounts.",
        )
    return account


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def txn_out(row: InvestmentTxn) -> InvestmentTxnOut:
    ratio = service.ratio_of(row)
    is_ratio = ratio is not None
    return InvestmentTxnOut(
        id=row.id,
        account_id=row.account_id,
        security_id=row.security_id,
        type=row.type,
        trade_date=row.trade_date,
        settle_date=row.settle_date,
        quantity=format_units(row.quantity) if row.quantity is not None and not is_ratio else None,
        price=format_units(row.price) if row.price is not None and not is_ratio else None,
        ratio={"new": format_units(ratio[0]), "old": format_units(ratio[1])} if ratio else None,
        amount=Money.of(row.amount, row.currency),
        fees=Money.of(row.fees, row.currency),
        lots=[
            LotPickOut(lot_id=link.lot_id, quantity=format_units(link.quantity))
            for link in parse_lot_links(row.lot_links)
        ],
        lots_specified=row.lots_specified,
        memo=row.memo,
        source_document_id=row.source_document_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _facts(row: InvestmentTxn) -> dict[str, Any]:
    """Audit facts: ids, type, dates and numbers; never the memo's text."""
    return {
        "account_id": str(row.account_id),
        "security_id": str(row.security_id) if row.security_id else None,
        "type": row.type.value,
        "trade_date": row.trade_date.isoformat(),
        "settle_date": row.settle_date.isoformat() if row.settle_date else None,
        "quantity": format_units(row.quantity) if row.quantity is not None else None,
        "price": format_units(row.price) if row.price is not None else None,
        "amount": f"{row.amount:f}",
        "fees": f"{row.fees:f}",
        "currency": row.currency,
        "lots_specified": row.lots_specified,
        "memo_set": row.memo is not None,
    }


def _record(
    db: Session,
    principal: Principal,
    row: InvestmentTxn,
    action: str,
    *,
    before: Any = None,
    after: Any = None,
    request: HTTPConnection | None,
) -> None:
    audit(
        db,
        action=f"investment_txn.{action}",
        target_type="investment_txn",
        target_id=row.id,
        before=before,
        after=after,
        household_id=row.household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(
        db,
        row.household_id,
        f"investment.txn_{action}d",
        {"id": str(row.id), "account_id": str(row.account_id)},
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass
class _Draft:
    security_id: uuid.UUID | None
    type: InvestmentTxnType
    trade_date: date
    settle_date: date | None
    quantity: Decimal | None
    price: Decimal | None
    amount: Decimal
    fees: Decimal
    ratio: tuple[Decimal, Decimal] | None
    lots: list[LotLink] | None
    memo: str | None


def _money(value: MoneyIn, currency: str) -> Decimal:
    if value.currency != currency:
        raise ProblemError(
            422,
            "currency-mismatch",
            "Currency mismatch",
            f"Amounts of this account are in {currency}.",
            extra={"currency": currency},
        )
    return value.amount


def _merge(data: InvestmentTxnFields, account: Account, row: InvestmentTxn | None) -> _Draft:
    """The transaction as it would be: ``data`` over ``row`` (``None`` on create)."""
    given = data.model_fields_set

    def take(name: str, current: Any) -> Any:
        return getattr(data, name) if row is None or name in given else current

    ratio_now = service.ratio_of(row) if row is not None else None
    if row is None or "ratio" in given:
        ratio = (data.ratio.new, data.ratio.old) if data.ratio is not None else None
    else:
        ratio = ratio_now
    amount_in: MoneyIn | None = take("amount", None)
    fees_in: MoneyIn | None = take("fees", None)
    if row is None or "lots" in given:
        lots = [LotLink(lot_id=p.lot_id, quantity=p.quantity) for p in data.lots or []] or None
    else:
        lots = parse_lot_links(row.lot_links) if row.lots_specified else None
    kind: InvestmentTxnType | None = take("type", row.type if row is not None else None)
    trade_date: date | None = take("trade_date", row.trade_date if row is not None else None)
    if kind is None or trade_date is None:
        raise _invalid("A type and a trade date are needed.")
    return _Draft(
        security_id=take("security_id", row.security_id if row is not None else None),
        type=kind,
        trade_date=trade_date,
        settle_date=take("settle_date", row.settle_date if row is not None else None),
        quantity=take("quantity", row.quantity if row is not None and not ratio_now else None),
        price=take("price", row.price if row is not None and not ratio_now else None),
        amount=(
            _money(amount_in, account.currency)
            if amount_in is not None
            else (row.amount if row is not None else Decimal(0))
        ),
        fees=(
            _money(fees_in, account.currency)
            if fees_in is not None
            else (row.fees if row is not None else Decimal(0))
        ),
        ratio=ratio,
        lots=lots,
        memo=take("memo", row.memo if row is not None else None),
    )


def _validate(draft: _Draft, method: CostMethod) -> None:
    kind = draft.type
    if draft.trade_date > service.today():
        raise ProblemError(
            422, "trade-date-in-future", "Trade date in the future", "Enter trades once made."
        )
    if draft.settle_date is not None and draft.settle_date < draft.trade_date:
        raise _invalid("The settlement date cannot be before the trade date.")
    if draft.fees < 0:
        raise _invalid("Fees cannot be negative.")
    needs_security = kind in SECURITY_TXN_TYPES or (
        kind is T.CORPORATE_ACTION and draft.ratio is not None
    )
    if needs_security and draft.security_id is None:
        raise _invalid(f"A {kind.value} names a security.")
    if kind in QUANTITY_TXN_TYPES and draft.quantity is None:
        raise _invalid(f"A {kind.value} needs a quantity above zero.")
    if kind in _NO_QUANTITY and draft.quantity is not None:
        raise _invalid(f"A {kind.value} has no quantity (a split gives its ratio).")
    if kind is T.SPLIT and draft.ratio is None:
        raise _invalid("A split needs its ratio.")
    if draft.ratio is not None and kind not in engine.RATIO_TYPES:
        raise _invalid("Only a split or a corporate action has a ratio.")
    if draft.ratio is not None and draft.price is not None:
        raise _invalid("A split has a ratio, not a price.")
    if kind in _AMOUNT_NOT_POSITIVE and draft.amount > 0:
        raise _invalid(f"The cash effect of a {kind.value} cannot be positive.")
    if kind is T.SELL and draft.amount < 0:
        raise _invalid("The net proceeds of a sale cannot be negative.")
    if kind is T.SPLIT and draft.amount != 0:
        raise _invalid("A split moves no cash.")
    if kind is T.TRANSFER_IN and draft.price is None:
        raise _invalid("A transfer in gives the cost per unit as its price.")
    if kind is T.REINVEST and draft.amount == 0 and draft.price is None:
        raise _invalid("A reinvestment that moved no cash needs its price.")
    if draft.lots is not None:
        if kind not in engine.CLOSING:
            raise _invalid("Only a sale or a transfer out names lots.")
        if method is CostMethod.AVERAGE:
            raise ProblemError(
                422,
                "lot-selection-not-allowed",
                "Lot selection not allowed",
                "Specific lots cannot be chosen in an average-cost account.",
            )
        if sum((link.quantity for link in draft.lots), Decimal(0)) != draft.quantity:
            raise ProblemError(
                422,
                "lot-links-mismatch",
                "Lot quantities do not match",
                "The units taken from the named lots must add up to the quantity.",
            )


def _apply(row: InvestmentTxn, draft: _Draft) -> None:
    row.security_id = draft.security_id
    row.type = draft.type
    row.trade_date = draft.trade_date
    row.settle_date = draft.settle_date
    if draft.ratio is not None:
        row.quantity, row.price = draft.ratio
    else:
        row.quantity = draft.quantity
        row.price = draft.price
    row.amount = draft.amount
    row.fees = draft.fees
    row.lots_specified = draft.lots is not None
    row.lot_links = dump_lot_links(draft.lots) if draft.lots is not None else []
    row.memo = draft.memo


def _replay(
    db: Session,
    account: Account,
    pairs: set[uuid.UUID | None],
    *,
    conflict_status: int = 422,
) -> None:
    for security_id in sorted((p for p in pairs if p is not None), key=lambda u: u.bytes):
        service.replay_position(db, account, security_id, conflict_status=conflict_status)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _get(
    db: Session,
    principal: Principal,
    txn_id: uuid.UUID,
    *,
    need: str = "view",
    lock: bool = False,
) -> InvestmentTxn:
    household_id = _household(
        principal, HouseholdRole.MEMBER if need == "edit" else HouseholdRole.VIEWER
    )
    stmt = select(InvestmentTxn).where(
        InvestmentTxn.household_id == household_id,
        InvestmentTxn.id == txn_id,
        visible_account_filter(
            principal, InvestmentTxn.account_id, need="edit" if need == "edit" else "view"
        ),
    )
    if lock:
        stmt = stmt.with_for_update()
    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        raise txn_not_found()
    return row


def get_txn(db: Session, principal: Principal, txn_id: uuid.UUID) -> InvestmentTxnOut:
    return txn_out(_get(db, principal, txn_id))


def list_txns(
    db: Session,
    principal: Principal,
    *,
    account_id: uuid.UUID | None,
    security_id: uuid.UUID | None,
    type_: InvestmentTxnType | None,
    date_from: date | None,
    date_to: date | None,
    cursor: uuid.UUID | None,
    limit: int,
) -> InvestmentTxnPage:
    """Newest trade date first (then newest entry); ``cursor`` is the last id of a page."""
    household_id = _household(principal, HouseholdRole.VIEWER)
    stmt = service.visible_investment_txns(principal)
    if account_id is not None:
        get_visible_account(db, principal, account_id)
        stmt = stmt.where(InvestmentTxn.account_id == account_id)
    if security_id is not None:
        service.get_visible_security(db, principal, security_id)
        stmt = stmt.where(InvestmentTxn.security_id == security_id)
    if type_ is not None:
        stmt = stmt.where(InvestmentTxn.type == type_)
    if date_from is not None:
        stmt = stmt.where(InvestmentTxn.trade_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(InvestmentTxn.trade_date <= date_to)
    if cursor is not None:
        after = db.execute(
            select(InvestmentTxn.trade_date).where(
                InvestmentTxn.household_id == household_id, InvestmentTxn.id == cursor
            )
        ).scalar_one_or_none()
        if after is None:
            raise ProblemError(422, "invalid-cursor", "Invalid cursor")
        stmt = stmt.where(
            tuple_(InvestmentTxn.trade_date, InvestmentTxn.id) < tuple_(after, cursor)
        )
    rows = list(
        db.execute(
            stmt.order_by(InvestmentTxn.trade_date.desc(), InvestmentTxn.id.desc()).limit(limit + 1)
        ).scalars()
    )
    page = rows[:limit]
    return InvestmentTxnPage(
        items=[txn_out(r) for r in page],
        next_cursor=page[-1].id if len(rows) > limit and page else None,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_txn(
    db: Session,
    principal: Principal,
    data: InvestmentTxnCreate,
    *,
    request: HTTPConnection | None,
) -> InvestmentTxnOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    account = _investment_account(db, principal, data.account_id, lock=True)
    draft = _merge(data, account, None)
    if draft.security_id is not None:
        service.get_visible_security(db, principal, draft.security_id)
    _validate(draft, service.cost_method(db, household_id, account.id))
    row = InvestmentTxn(household_id=household_id, account_id=account.id, currency=account.currency)
    _apply(row, draft)
    db.add(row)
    db.flush()
    _replay(db, account, {row.security_id})
    db.refresh(row)
    _record(db, principal, row, "create", after=_facts(row), request=request)
    return txn_out(row)


def require_editable_account(db: Session, principal: Principal, account_id: uuid.UUID) -> Account:
    """An account the caller may edit, or ``404 account-not-found`` (routers call it first)."""
    _household(principal, HouseholdRole.MEMBER)
    return get_visible_account(db, principal, account_id, need="edit")


def require_editable(db: Session, principal: Principal, txn_id: uuid.UUID) -> InvestmentTxn:
    """A transaction the caller may change, or ``404`` (routers call it before the body)."""
    return _get(db, principal, txn_id, need="edit")


def update_txn(
    db: Session,
    principal: Principal,
    txn_id: uuid.UUID,
    data: InvestmentTxnUpdate,
    *,
    request: HTTPConnection | None,
) -> InvestmentTxnOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    found = _get(db, principal, txn_id, need="edit")
    account = _investment_account(db, principal, found.account_id, lock=True)
    row = _get(db, principal, txn_id, need="edit", lock=True)
    before = _facts(row)
    draft = _merge(data, account, row)
    if draft.security_id is not None and draft.security_id != row.security_id:
        service.get_visible_security(db, principal, draft.security_id)
    _validate(draft, service.cost_method(db, household_id, account.id))
    old_security = row.security_id
    _apply(row, draft)
    db.flush()
    _replay(db, account, {old_security, row.security_id})
    db.refresh(row)
    _record(db, principal, row, "update", before=before, after=_facts(row), request=request)
    return txn_out(row)


def delete_txn(
    db: Session, principal: Principal, txn_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    _household(principal, HouseholdRole.MEMBER)
    found = _get(db, principal, txn_id, need="edit")
    account = _investment_account(db, principal, found.account_id, lock=True)
    row = _get(db, principal, txn_id, need="edit", lock=True)
    before = _facts(row)
    security_id = row.security_id
    _record(db, principal, row, "delete", before=before, request=request)
    # The lots it opened go first: the database would otherwise only clear
    # their ``opening_txn_id`` and leave them looking like statement seeds.
    db.execute(
        delete(Lot).where(Lot.household_id == row.household_id, Lot.opening_txn_id == row.id)
    )
    db.delete(row)
    db.flush()
    _replay(db, account, {security_id}, conflict_status=409)


# ---------------------------------------------------------------------------
# Cost method
# ---------------------------------------------------------------------------


def set_cost_method(
    db: Session,
    principal: Principal,
    account_id: uuid.UUID,
    data: CostMethodSet,
    *,
    request: HTTPConnection | None,
) -> CostMethodOut:
    """Change the account's cost method and replay all its lots (``422`` if they do not fit)."""
    household_id = _household(principal, HouseholdRole.MEMBER)
    account = _investment_account(db, principal, account_id, lock=True)
    row = db.execute(
        select(InvestmentAccount).where(
            InvestmentAccount.household_id == household_id,
            InvestmentAccount.account_id == account.id,
        )
    ).scalar_one_or_none()
    before = row.cost_method if row is not None else CostMethod.FIFO
    if row is None:
        row = InvestmentAccount(household_id=household_id, account_id=account.id)
        db.add(row)
    row.cost_method = data.method
    db.flush()
    if before is not data.method:
        service.replay_account(db, account)
        audit(
            db,
            action="investment_account.cost_method",
            target_type="account",
            target_id=account.id,
            before={"cost_method": before.value},
            after={"cost_method": data.method.value},
            household_id=household_id,
            user_id=principal.user_id,
            request=request,
        )
        emit(
            db,
            household_id,
            "investment.cost_method_changed",
            {"account_id": str(account.id), "cost_method": data.method.value},
        )
    return CostMethodOut(account_id=account.id, method=data.method)


def get_cost_method(db: Session, principal: Principal, account_id: uuid.UUID) -> CostMethodOut:
    household_id = _household(principal, HouseholdRole.VIEWER)
    account = get_visible_account(db, principal, account_id)
    return CostMethodOut(
        account_id=account.id, method=service.cost_method(db, household_id, account.id)
    )

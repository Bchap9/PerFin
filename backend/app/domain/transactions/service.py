# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transactions: list, read, create, change, split, delete and bulk actions.

Authorisation (AUTH.md "Authorisation model", SECURITY.md F3/I3):

* Routers require a household role (viewer to read, member to write) and the
  ``transactions:read|write`` token scope; everything here then applies
  **account visibility** (``app.domain.accounts.visibility``): owners and
  admins see every account, members and viewers the accounts granted to
  them; writing needs ``edit`` (never for viewers).
* A transaction on a hidden account, of another household or missing is
  ``404 transaction-not-found``; an account named in a request body or a
  filter that the caller cannot see is ``404 account-not-found``; payees,
  categories and tags of another household are ``404
  payee-|category-|tag-not-found``.
* Bulk actions authorise every id on its own: ids the caller cannot edit
  are reported as ``not_found`` and left untouched; the others are applied.

Invariants kept here (DATA_MODEL.md "Ledger"):

* ``amount.currency`` must be the account's currency (``422 currency-mismatch``).
* A split transaction has ``category_id`` NULL and splits that add up
  exactly to its amount (``422 split-sum-mismatch``); while it has splits,
  its amount and category cannot be changed directly (``409
  transaction-split``).
* A leg of a transfer keeps its amount (``409 transfer-conflict``: unlink
  first). Deleting a leg dissolves the transfer.

Every change is audited (``transaction.create|update|split|delete|bulk_*``,
with ids, amounts, dates and the names of changed free-text fields; the free
text itself (payee text, memo, notes) is never copied into the audit trail)
and emits a ``transaction.*`` event. A deleted transaction's audit row stays.

A new manual transaction goes through the household's rules
(``app.domain.rules.service.apply_rules_to``) in the same database
transaction; the category, payee, notes and tags given by hand are protected.

Reports exclude transactions whose ``review_state`` is ``pending`` unless the
household setting ``reports.include_pending_review`` is ``true``
(:func:`include_pending_in_reports`, :func:`reportable_clause`).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import ColumnElement, select, true
from sqlalchemy.orm import Session

from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.money import Money, format_amount, sum_amounts
from app.db.models import Setting, SettingScope
from app.domain.accounts.visibility import (
    Need,
    get_visible_account,
    require_visible_accounts,
    visible_account_filter,
)
from app.domain.transactions import repo, transfers
from app.domain.transactions.models import (
    Category,
    Payee,
    ReviewState,
    Tag,
    Transaction,
    TransactionSplit,
)
from app.domain.transactions.schemas import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    BulkAction,
    BulkItemResult,
    BulkResult,
    SplitOut,
    SplitSet,
    TransactionCreate,
    TransactionOut,
    TransactionPage,
    TransactionUpdate,
    TransferOut,
    decode_cursor,
    encode_cursor,
    transaction_not_found,
)

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

#: Household setting: count ``review_state = pending`` transactions in reports.
INCLUDE_PENDING_SETTING: Final = "reports.include_pending_review"
#: Free-text columns: only their names reach the audit trail.
FREE_TEXT_FIELDS: Final = frozenset({"raw_payee", "memo", "notes"})


# ---------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------


def _not_found(kind: str) -> ProblemError:
    return ProblemError(404, f"{kind}-not-found", f"{kind.capitalize()} not found")


def _currency_mismatch(expected: str) -> ProblemError:
    return ProblemError(
        422,
        "currency-mismatch",
        "Currency mismatch",
        f"Amounts on this account must be in {expected}.",
    )


def _split_conflict() -> ProblemError:
    return ProblemError(
        409,
        "transaction-split",
        "Transaction is split",
        "Change or remove the splits first; the amount and category of a split "
        "transaction come from its splits.",
    )


def _household(principal: Principal) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise transaction_not_found()
    return principal.household_id


def _visible(principal: Principal, need: Need = "view") -> ColumnElement[bool]:
    return visible_account_filter(principal, Transaction.account_id, need=need)


# ---------------------------------------------------------------------------
# Reports: pending review
# ---------------------------------------------------------------------------


def include_pending_in_reports(db: Session, household_id: uuid.UUID) -> bool:
    """The household's ``reports.include_pending_review`` setting (default ``False``)."""
    value = db.execute(
        select(Setting.value).where(
            Setting.scope == SettingScope.HOUSEHOLD,
            Setting.scope_id == household_id,
            Setting.key == INCLUDE_PENDING_SETTING,
        )
    ).scalar_one_or_none()
    return value is True


def reportable_clause(include_pending: bool = False, *, column: Any = None) -> ColumnElement[bool]:
    """SQL predicate for transactions that count in reports.

    Reports (budgets spent, cash flow, net worth contributions, dashboards)
    leave out transactions still waiting in the review queue
    (``review_state = pending``) unless ``include_pending``; pass the value of
    :func:`include_pending_in_reports` (or an explicit request parameter).
    """
    if include_pending:
        return true()
    target = Transaction.review_state if column is None else column
    result: ColumnElement[bool] = target != ReviewState.PENDING
    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _split_out(split: TransactionSplit, currency: str) -> SplitOut:
    return SplitOut(
        id=split.id,
        category_id=split.category_id,
        amount=Money.of(split.amount, currency),
        memo=split.memo,
        tax_flag=split.tax_flag,
    )


def to_out(db: Session, principal: Principal, rows: Sequence[Transaction]) -> list[TransactionOut]:
    """API models for ``rows`` (tags, splits and transfer legs loaded in batches)."""
    if not rows:
        return []
    household_id = rows[0].household_id
    ids = [row.id for row in rows]
    tags = repo.tag_ids_for(db, household_id, ids)
    splits = repo.splits_for(db, household_id, ids)
    groups = repo.group_members(
        db, household_id, {r.transfer_group_id for r in rows if r.transfer_group_id is not None}
    )
    leg_accounts = {account for legs in groups.values() for _, account in legs}
    visible_accounts = (
        set(
            db.execute(
                select(Transaction.account_id)
                .where(
                    Transaction.household_id == household_id,
                    Transaction.account_id.in_(leg_accounts),
                    _visible(principal),
                )
                .distinct()
            ).scalars()
        )
        if leg_accounts
        else set()
    )
    out: list[TransactionOut] = []
    for row in rows:
        transfer = None
        if row.transfer_group_id is not None:
            others = [
                tid
                for tid, account in groups.get(row.transfer_group_id, [])
                if tid != row.id and account in visible_accounts
            ]
            transfer = TransferOut(
                group_id=row.transfer_group_id, counterpart_id=others[0] if others else None
            )
        out.append(
            TransactionOut(
                id=row.id,
                account_id=row.account_id,
                posted_date=row.posted_date,
                effective_date=row.effective_date,
                amount=Money.of(row.amount, row.currency),
                payee_id=row.payee_id,
                category_id=row.category_id,
                raw_payee=row.raw_payee,
                memo=row.memo,
                notes=row.notes,
                status=row.status,
                review_state=row.review_state,
                confidence=None if row.confidence is None else str(row.confidence),
                transfer=transfer,
                source_document_id=row.source_document_id,
                tag_ids=tags.get(row.id, []),
                splits=[_split_out(s, row.currency) for s in splits.get(row.id, [])],
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    return out


def one_out(db: Session, principal: Principal, row: Transaction) -> TransactionOut:
    return to_out(db, principal, [row])[0]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_visible_transaction(
    db: Session,
    principal: Principal,
    transaction_id: uuid.UUID,
    *,
    need: Need = "view",
    lock: bool = False,
) -> Transaction:
    """The transaction, or ``404 transaction-not-found`` (hidden, foreign or missing)."""
    household_id = _household(principal)
    row = repo.get(db, household_id, transaction_id, _visible(principal, need), lock=lock)
    if row is None:
        raise transaction_not_found()
    return row


def list_transactions(
    db: Session,
    principal: Principal,
    filters: repo.TransactionFilters,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> TransactionPage:
    """One page, newest first (``posted_date`` then id, descending).

    The keyset cursor makes paging stable: rows inserted or deleted while a
    client pages never shift the remaining pages (no duplicates, no gaps
    among rows that existed throughout).
    """
    household_id = _household(principal)
    if filters.account_ids:
        require_visible_accounts(db, principal, filters.account_ids)
    limit = max(1, min(limit, MAX_LIMIT))
    after = decode_cursor(cursor) if cursor else None
    rows = repo.list_page(db, household_id, _visible(principal), filters, after=after, limit=limit)
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(last.posted_date, last.id)
    return TransactionPage(items=to_out(db, principal, rows), next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# Reference checks
# ---------------------------------------------------------------------------


def _check_refs(
    db: Session,
    household_id: uuid.UUID,
    *,
    payee_id: uuid.UUID | None = None,
    category_ids: Iterable[uuid.UUID | None] = (),
    tag_ids: Collection[uuid.UUID] = (),
) -> None:
    """``404 <kind>-not-found`` unless every reference is a row of the household.

    (The composite foreign keys refuse another household's id too, but only
    at commit and as a database error; this answers with a problem.)
    """
    if payee_id is not None and not repo.existing_ids(db, Payee, household_id, [payee_id]):
        raise _not_found("payee")
    categories = {c for c in category_ids if c is not None}
    if categories and repo.existing_ids(db, Category, household_id, categories) != categories:
        raise _not_found("category")
    tags = set(tag_ids)
    if tags and repo.existing_ids(db, Tag, household_id, tags) != tags:
        raise _not_found("tag")


def _amount_in(currency: str, amount: Decimal, expected: str) -> Decimal:
    if currency != expected:
        raise _currency_mismatch(expected)
    return amount


# ---------------------------------------------------------------------------
# Audit and events
# ---------------------------------------------------------------------------


def _facts(row: Transaction) -> dict[str, Any]:
    """What the audit trail keeps about a transaction (no free text)."""
    return {
        "account_id": row.account_id,
        "posted_date": row.posted_date,
        "amount": format_amount(row.amount, row.currency),
        "currency": row.currency,
        "payee_id": row.payee_id,
        "category_id": row.category_id,
        "status": row.status,
        "review_state": row.review_state,
        "transfer_group_id": row.transfer_group_id,
        "source_document_id": row.source_document_id,
        "imported": row.import_hash is not None,
    }


def _record(
    db: Session,
    principal: Principal,
    row: Transaction,
    *,
    action: str,
    event: str,
    before: Any = None,
    after: Any = None,
    request: HTTPConnection | None,
) -> None:
    audit(
        db,
        action=action,
        target_type="transaction",
        target_id=row.id,
        before=before,
        after=after,
        household_id=row.household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(db, row.household_id, event, {"id": str(row.id), "account_id": str(row.account_id)})


# ---------------------------------------------------------------------------
# Create / update / delete
# ---------------------------------------------------------------------------


def create_transaction(
    db: Session, principal: Principal, data: TransactionCreate, *, request: HTTPConnection | None
) -> Transaction:
    """A manual transaction on an account the caller may edit."""
    household_id = _household(principal)
    account = get_visible_account(db, principal, data.account_id, need="edit")
    amount = _amount_in(data.amount.currency, data.amount.amount, account.currency)
    _check_refs(
        db,
        household_id,
        payee_id=data.payee_id,
        category_ids=[data.category_id],
        tag_ids=data.tag_ids,
    )
    row = Transaction(
        household_id=household_id,
        account_id=account.id,
        posted_date=data.posted_date,
        effective_date=data.effective_date,
        amount=amount,
        currency=account.currency,
        payee_id=data.payee_id,
        category_id=data.category_id,
        raw_payee=data.raw_payee,
        memo=data.memo,
        notes=data.notes,
        status=data.status,
        review_state=ReviewState.NONE,
    )
    db.add(row)
    db.flush()
    if data.tag_ids:
        repo.add_tags(db, household_id, [row.id], data.tag_ids)
    after = _facts(row)
    after["tag_ids"] = sorted(data.tag_ids)
    after["free_fields_set"] = sorted(f for f in FREE_TEXT_FIELDS if getattr(row, f) is not None)
    _record(
        db,
        principal,
        row,
        action="transaction.create",
        event="transaction.created",
        after=after,
        request=request,
    )
    _apply_rules_to_new(db, principal, row, data, request=request)
    return row


def _apply_rules_to_new(
    db: Session,
    principal: Principal,
    row: Transaction,
    data: TransactionCreate,
    *,
    request: HTTPConnection | None,
) -> None:
    """Run the household's rules over a new manual transaction (rules hook).

    Fields the person entered by hand are protected: rules never overwrite a
    category, payee, notes or tags given in the request.
    """
    # Imported here: ``app.domain.rules.service`` imports this module.
    from app.domain.rules.service import apply_rules_to

    protect = [
        name
        for name, given in (
            ("categorization", data.category_id is not None),
            ("payee", data.payee_id is not None),
            ("notes", data.notes is not None),
            ("tags", bool(data.tag_ids)),
        )
        if given
    ]
    apply_rules_to(
        db, row.household_id, [row], protect=protect, user_id=principal.user_id, request=request
    )


_SIMPLE_FIELDS: Final = (
    "posted_date",
    "effective_date",
    "payee_id",
    "category_id",
    "raw_payee",
    "memo",
    "notes",
    "status",
    "review_state",
)


def update_transaction(
    db: Session,
    principal: Principal,
    row: Transaction,
    data: TransactionUpdate,
    *,
    request: HTTPConnection | None,
) -> Transaction:
    """Apply ``data`` to ``row`` (loaded for edit and locked)."""
    household_id = row.household_id
    given = data.model_fields_set
    split = repo.has_splits(db, household_id, row.id)
    if "category_id" in given and data.category_id is not None and split:
        raise _split_conflict()
    _check_refs(
        db,
        household_id,
        payee_id=data.payee_id if "payee_id" in given else None,
        category_ids=[data.category_id] if "category_id" in given else [],
        tag_ids=data.tag_ids or () if "tag_ids" in given else (),
    )
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    if "amount" in given and data.amount is not None:
        amount = _amount_in(data.amount.currency, data.amount.amount, row.currency)
        if amount != row.amount:
            if split:
                raise _split_conflict()
            if row.transfer_group_id is not None:
                raise transfers.transfer_conflict(
                    "This transaction is one leg of a transfer; unlink it before changing "
                    "the amount."
                )
            before["amount"] = format_amount(row.amount, row.currency)
            after["amount"] = format_amount(amount, row.currency)
            row.amount = amount
    for name in _SIMPLE_FIELDS:
        if name not in given:
            continue
        new = getattr(data, name)
        old = getattr(row, name)
        if new == old:
            continue
        if name in FREE_TEXT_FIELDS:
            before[name] = after[name] = "[changed]"
        else:
            before[name], after[name] = old, new
        setattr(row, name, new)
    if "tag_ids" in given and data.tag_ids is not None:
        old_tags = sorted(repo.tag_ids_for(db, household_id, [row.id]).get(row.id, []))
        if repo.set_tags(db, row, data.tag_ids):
            before["tag_ids"], after["tag_ids"] = old_tags, sorted(data.tag_ids)
    if after:
        row.updated_at = clock.utcnow()
        db.flush()
        _record(
            db,
            principal,
            row,
            action="transaction.update",
            event="transaction.updated",
            before=before,
            after=after,
            request=request,
        )
    return row


def delete_transaction(
    db: Session, principal: Principal, row: Transaction, *, request: HTTPConnection | None
) -> None:
    """Delete ``row`` (loaded for edit and locked); the audit row keeps its facts.

    A transfer leg dissolves its transfer first (the other leg stays, unlinked).
    """
    if row.transfer_group_id is not None:
        transfers.unlink(db, principal, row, request=request)
    before = _facts(row)
    before["splits"] = len(repo.splits_for(db, row.household_id, [row.id]).get(row.id, []))
    before["tag_ids"] = sorted(repo.tag_ids_for(db, row.household_id, [row.id]).get(row.id, []))
    _record(
        db,
        principal,
        row,
        action="transaction.delete",
        event="transaction.deleted",
        before=before,
        request=request,
    )
    repo.delete_transaction(db, row)
    db.expunge(row)


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------


def split_sum_mismatch(expected: Decimal, actual: Decimal, currency: str) -> ProblemError:
    return ProblemError(
        422,
        "split-sum-mismatch",
        "Splits do not add up",
        f"The splits add up to {format_amount(actual, currency)} {currency}; the "
        f"transaction amount is {format_amount(expected, currency)} {currency}.",
    )


def set_splits(
    db: Session,
    principal: Principal,
    row: Transaction,
    data: SplitSet,
    *,
    request: HTTPConnection | None,
) -> Transaction:
    """Replace the splits of ``row`` (loaded for edit and locked); see :class:`SplitSet`."""
    household_id = row.household_id
    for line in data.splits:
        _amount_in(line.amount.currency, line.amount.amount, row.currency)
    _check_refs(
        db,
        household_id,
        category_ids=[line.category_id for line in data.splits] + [data.category_id],
    )
    if data.splits:
        total = sum_amounts(line.amount.amount for line in data.splits)
        if total != row.amount:
            raise split_sum_mismatch(row.amount, total, row.currency)
    old = repo.splits_for(db, household_id, [row.id]).get(row.id, [])
    before = {
        "category_id": row.category_id,
        "splits": [
            {"category_id": s.category_id, "amount": format_amount(s.amount, row.currency)}
            for s in old
        ],
    }
    repo.replace_splits(
        db,
        row,
        [
            {
                "amount": line.amount.amount,
                "category_id": line.category_id,
                "memo": line.memo,
                "tax_flag": line.tax_flag,
            }
            for line in data.splits
        ],
    )
    row.category_id = None if data.splits else data.category_id
    row.updated_at = clock.utcnow()
    db.flush()
    _record(
        db,
        principal,
        row,
        action="transaction.split",
        event="transaction.updated",
        before=before,
        after={
            "category_id": row.category_id,
            "splits": [
                {
                    "category_id": line.category_id,
                    "amount": format_amount(line.amount.amount, row.currency),
                    "tax_flag": line.tax_flag,
                }
                for line in data.splits
            ],
        },
        request=request,
    )
    return row


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------

#: Account ids listed in one bulk event at most (the payload stays small).
_EVENT_ACCOUNTS_MAX: Final = 50


def bulk(
    db: Session, principal: Principal, data: BulkAction, *, request: HTTPConnection | None
) -> BulkResult:
    """Apply ``data.action`` to every id the caller may edit (each authorised alone)."""
    household_id = _household(principal)
    if data.action == "categorise":
        _check_refs(db, household_id, category_ids=[data.category_id])
    if data.tag_ids:
        _check_refs(db, household_id, tag_ids=data.tag_ids)
    rows = repo.get_many(db, household_id, data.ids, _visible(principal, "edit"), lock=True)
    results: dict[uuid.UUID, BulkItemResult] = {
        i: BulkItemResult(id=i, result="not_found") for i in data.ids if i not in rows
    }
    changed: list[Transaction] = []
    now = clock.utcnow()
    if data.action == "categorise":
        split_ids = repo.split_transaction_ids(db, household_id, list(rows))
        for tid, row in rows.items():
            if tid in split_ids:
                results[tid] = BulkItemResult(id=tid, result="skipped", reason="split")
            elif row.category_id == data.category_id:
                results[tid] = BulkItemResult(id=tid, result="unchanged")
            else:
                previous = row.category_id
                row.category_id = data.category_id
                row.updated_at = now
                changed.append(row)
                results[tid] = BulkItemResult(id=tid, result="updated")
                _bulk_audit(db, principal, row, data, request, {"category_id": previous})
    elif data.action == "confirm":
        for tid, row in rows.items():
            if row.review_state is ReviewState.CONFIRMED:
                results[tid] = BulkItemResult(id=tid, result="unchanged")
                continue
            before_state = row.review_state
            row.review_state = ReviewState.CONFIRMED
            row.updated_at = now
            changed.append(row)
            results[tid] = BulkItemResult(id=tid, result="updated")
            _bulk_audit(db, principal, row, data, request, {"review_state": before_state})
    elif data.action in ("tag", "untag"):
        tag_ids = data.tag_ids or []
        if data.action == "tag":
            touched = repo.add_tags(db, household_id, list(rows), tag_ids)
        else:
            touched = repo.remove_tags(db, household_id, list(rows), tag_ids)
        for tid, row in rows.items():
            if tid in touched:
                row.updated_at = now
                changed.append(row)
                results[tid] = BulkItemResult(id=tid, result="updated")
                _bulk_audit(db, principal, row, data, request, None)
            else:
                results[tid] = BulkItemResult(id=tid, result="unchanged")
    else:  # delete
        for tid, row in rows.items():
            if row.transfer_group_id is not None:
                transfers.unlink(db, principal, row, request=request)
            _bulk_audit(db, principal, row, data, request, _facts(row))
            repo.delete_transaction(db, row)
            db.expunge(row)
            changed.append(row)
            results[tid] = BulkItemResult(id=tid, result="deleted")
    db.flush()
    if changed:
        accounts = sorted({str(r.account_id) for r in changed})
        emit(
            db,
            household_id,
            "transaction.bulk_updated",
            {
                "action": data.action,
                "count": len(changed),
                "account_ids": accounts[:_EVENT_ACCOUNTS_MAX],
            },
        )
    return BulkResult(
        action=data.action, results=[results[i] for i in data.ids], changed=len(changed)
    )


def _bulk_audit(
    db: Session,
    principal: Principal,
    row: Transaction,
    data: BulkAction,
    request: HTTPConnection | None,
    before: Mapping[str, Any] | None,
) -> None:
    after: dict[str, Any] = {}
    if data.action == "categorise":
        after = {"category_id": data.category_id}
    elif data.action == "confirm":
        after = {"review_state": ReviewState.CONFIRMED}
    elif data.action in ("tag", "untag"):
        after = {"tag_ids": sorted(data.tag_ids or [])}
    audit(
        db,
        action=f"transaction.bulk_{data.action}",
        target_type="transaction",
        target_id=row.id,
        before=before,
        after=after or None,
        household_id=row.household_id,
        user_id=principal.user_id,
        request=request,
    )


# ---------------------------------------------------------------------------
# Transfers (API entry points; rules in app.domain.transactions.transfers)
# ---------------------------------------------------------------------------


def transfer_candidates(
    db: Session, principal: Principal, row: Transaction, *, window_days: int
) -> list[TransactionOut]:
    return to_out(db, principal, transfers.candidates(db, principal, row, window_days=window_days))

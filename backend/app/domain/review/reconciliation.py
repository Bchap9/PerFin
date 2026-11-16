# SPDX-License-Identifier: AGPL-3.0-or-later
"""A statement checked against the ledger rows its document produced.

``GET /documents/{id}/reconciliation`` and its four actions (confirm,
accept, restore, remove-earlier; ``app.api.routers.reconciliation``). The
shapes are in ``app.domain.review.schemas`` (:class:`DocumentReconciliation`,
:class:`ReconcileOutcome`).

**The read** (:func:`get_reconciliation`). One entry per stored bank
statement of the document (``document.extraction``) whose account is known
and visible to the caller; a statement on an account the caller cannot see
is left out, never an error. Nothing is parsed again and nothing is
written.

* *The account* of a statement is the one its ``statement_record`` names
  (the record of this document with the statement's period start; when one
  document holds several accounts with the same period, the record whose
  account has the statement's digits, closing balance or rows), else the
  document's account when the document holds a single statement. A
  statement that fits several accounts equally is left out: no sum is
  better than a sum on the wrong account.
* *The rows* (:func:`statement_rows`) are ledger rows of that account: the
  rows whose ``import_hash`` (or alias, for a row that came first from
  another file) is a hash of one of the stored statement's rows, plus the
  rows whose source is this document. When the document holds several
  statements of one account, a row with a statement's hash counts for that
  statement, and a row without one (added by a person, or kept from an
  earlier reading) for the first statement whose period holds its date.
* *The sum* is the ledger's: ``difference = opening + total - closing`` in
  ledger terms, so an edited row changes it and a deleted row leaves it.
  ``status`` is ``unchecked`` without both balances, ``balanced`` within
  :data:`~app.ingestion.reconcile.TOLERANCE`, else ``off``.
* *Balances as printed.* ``opening``, ``closing`` and ``previous_closing``
  are shown the way the statement prints them: for an account on the
  liability side a balance owed is positive (:func:`as_printed`). A balance
  a person types is read with :func:`read_printed` and turned back with
  :func:`to_ledger`.
* *The previous closing* is the closing balance of the account's latest
  recorded statement of another document that starts before this one and
  ends no later than this one starts (two statements may share their edge
  day).

A response carries what the caller may already read of the account (its
rows) and boxes on pages of the document; causes carry ids, amounts and
boxes only.

**The actions** are declared only: each settles who may ask (a hidden
document is ``404``, a read-only one ``403``) and then answers ``409
statement-unavailable``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Literal, cast

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.principal import Principal
from app.core.errors import ProblemError
from app.core.money import AMOUNT_INTEGER_DIGITS, AMOUNT_PLACES, to_storage
from app.domain.accounts import repo as accounts_repo
from app.domain.accounts.models import LIABILITY_TYPES, Account
from app.domain.accounts.visibility import can_edit_account, can_view_account
from app.domain.documents.manage import can_edit_document, get_editable_document
from app.domain.documents.models import Document
from app.domain.documents.schemas import Verification
from app.domain.documents.service import get_visible_document
from app.domain.imports import tombstones
from app.domain.imports.models import StatementRecord
from app.domain.review import repo
from app.domain.review.causes import Cause, LedgerRow, PageAmount, fee_lines, rank_causes
from app.domain.review.layout import row_region, stored_result
from app.domain.review.schemas import (
    CauseKind,
    DocumentReconciliation,
    ReconcileAccept,
    ReconcileCause,
    ReconcileConfirm,
    ReconcileOutcome,
    ReconcileRemoveEarlier,
    ReconcileRestore,
    ReconcileRow,
    RemovedRow,
    StatementReconciliation,
)
from app.domain.transactions.import_hash import HashInput, assign_import_hashes
from app.domain.transactions.models import Transaction
from app.ingestion.parsers.base import DocumentView
from app.ingestion.reconcile import TOLERANCE
from app.ingestion.sandbox.protocol import ExtractedTransaction, ExtractResult, Statement

#: Rows listed for one statement (the sum always counts every row).
ROWS_MAX: Final = 2000
#: Row ids one cause names.
CAUSE_ROWS_MAX: Final = 200

Period = tuple[date, date]

#: A balance as a person types it from paper: digits with an optional
#: fraction, thousands separators only between groups of three, at most one
#: currency symbol (before or after), negative by one leading ``-`` or by
#: surrounding brackets. ASCII digits only; no exponent.
_PRINTED: Final = re.compile(
    r"(?P<open>\()?[ ]*(?P<minus>-)?[ ]*(?P<before>[$€£])?[ ]*(?P<minus2>-)?[ ]*"
    r"(?P<whole>[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.(?P<fraction>[0-9]+))?"
    r"[ ]*(?P<after>[$€£])?[ ]*(?P<close>\))?"
)


def statement_unavailable() -> ProblemError:
    return ProblemError(
        409,
        "statement-unavailable",
        "No statement to check",
        "The document has no statement that could be checked against the ledger.",
    )


def invalid_amount() -> ProblemError:
    return ProblemError(
        422,
        "invalid-amount",
        "Not an amount",
        "Type the balance as the statement prints it, for example 1,204.18.",
    )


# ---------------------------------------------------------------------------
# Balances as printed
# ---------------------------------------------------------------------------


def read_printed(text: str) -> Decimal:
    """A balance typed as printed (``1,204.18``, ``$1,204.18``, ``(12.00)``, ``-12``).

    ``422 invalid-amount`` for anything else: a separator in the wrong
    place, two symbols, two signs, an exponent, more digits than the ledger
    holds. The value is never guessed.
    """
    found = _PRINTED.fullmatch(text.strip(" "))
    if found is None:
        raise invalid_amount()
    brackets = found["open"] is not None
    signs = [found["minus"], found["minus2"]].count("-") + (1 if brackets else 0)
    whole = found["whole"].replace(",", "")
    fraction = found["fraction"] or ""
    if (
        brackets != (found["close"] is not None)
        or signs > 1
        or (found["before"] and found["after"])
        or len(whole) > AMOUNT_INTEGER_DIGITS
        or len(fraction) > AMOUNT_PLACES
    ):
        raise invalid_amount()
    value = Decimal(f"{whole}.{fraction}" if fraction else whole)
    return -value if signs and value else value


def printed_sign(account: Account) -> Literal["ledger", "owed"]:
    """``owed``: the account's statements print a balance owed as a positive number."""
    return "owed" if account.type in LIABILITY_TYPES else "ledger"


def to_ledger(value: Decimal, account: Account) -> Decimal:
    """A balance as printed, in ledger terms (a balance owed is negative)."""
    return -value if value and printed_sign(account) == "owed" else value


def _text(value: Decimal) -> str:
    """An amount as text: cents when it has no finer part, never an exponent."""
    cents = value.quantize(Decimal("0.01"))
    return f"{cents:f}" if cents == value else f"{value:f}"


def as_printed(value: Decimal | None, account: Account) -> str | None:
    """A ledger balance the way the account's statements print it."""
    if value is None:
        return None
    # Negating is its own inverse: the same rule turns a ledger value to paper.
    return _text(to_ledger(value, account))


# ---------------------------------------------------------------------------
# The rows of a statement
# ---------------------------------------------------------------------------


def _hashed(statement: Statement, account: Account) -> list[tuple[str, ExtractedTransaction]]:
    """The statement's rows with the hash the importer gives each on ``account``.

    As ``imports.service`` commits them: a row in another currency than the
    account's is left out before the rows are numbered.
    """
    rows = [
        t for t in statement.transactions if t.currency is None or t.currency == account.currency
    ]
    hashes = assign_import_hashes(
        account.id, [HashInput(t.posted, t.amount, t.payee) for t in rows]
    )
    return list(zip(hashes, rows, strict=True))


def _period(statement: Statement) -> Period | None:
    start, end = statement.period_start, statement.period_end
    return (start, end) if start is not None and end is not None and start <= end else None


@dataclass(frozen=True)
class _Rows:
    """The ledger rows of one stored statement, oldest first."""

    rows: list[Transaction]
    #: The statement's row hashes, and the stored row each stands for.
    read: dict[str, ExtractedTransaction]
    #: Every hash (its own and its aliases) of each ledger row.
    hashes: dict[uuid.UUID, tuple[str, ...]]


def _split(
    db: Session, document: Document, account: Account, statements: Sequence[Statement]
) -> list[_Rows]:
    """The ledger rows of each of ``statements`` (all of ``account``, in document order)."""
    read: list[dict[str, ExtractedTransaction]] = []
    for statement in statements:
        mapping: dict[str, ExtractedTransaction] = {}
        for value, printed in _hashed(statement, account):
            mapping.setdefault(value, printed)
        read.append(mapping)
    wanted = sorted({value for mapping in read for value in mapping})
    aliases: dict[uuid.UUID, list[str]] = {}
    for transaction_id, value in repo.alias_transactions(
        db, account.household_id, account.id, wanted
    ):
        aliases.setdefault(transaction_id, []).append(value)
    conditions = [Transaction.source_document_id == document.id]
    if wanted:
        conditions.append(Transaction.import_hash.in_(wanted))
    if aliases:
        conditions.append(Transaction.id.in_(sorted(aliases)))
    ledger = list(
        db.execute(
            select(Transaction)
            .where(
                Transaction.household_id == account.household_id,
                Transaction.account_id == account.id,
                or_(*conditions),
            )
            .order_by(Transaction.posted_date, Transaction.id)
        ).scalars()
    )
    periods = [_period(statement) for statement in statements]
    out = [_Rows([], mapping, {}) for mapping in read]
    for row in ledger:
        own = (row.import_hash,) if row.import_hash else ()
        mine = (*own, *sorted(aliases.get(row.id, ())))
        targets = [i for i, mapping in enumerate(read) if any(v in mapping for v in mine)]
        if not targets:
            if row.source_document_id != document.id:  # pragma: no cover - the query's own rule
                continue
            targets = [
                next(
                    (
                        i
                        for i, period in enumerate(periods)
                        if period is not None and period[0] <= row.posted_date <= period[1]
                    ),
                    0,
                )
            ]
        for index in targets:
            out[index].rows.append(row)
            out[index].hashes[row.id] = mine
    return out


def statement_rows(
    db: Session,
    document: Document,
    account: Account,
    statement: Statement,
    *,
    others: Sequence[Statement] = (),
) -> list[Transaction]:
    """The ledger rows of ``account`` that count towards ``statement`` (module doc).

    ``others`` are the document's other stored statements of the same
    account. A row without a statement's hash counts for ``statement`` when
    its period holds the row's date, or when no period of any of them does.
    """
    return _split(db, document, account, [statement, *others])[0].rows


# ---------------------------------------------------------------------------
# Which account a stored statement is on
# ---------------------------------------------------------------------------


def _records(db: Session, document: Document) -> list[StatementRecord]:
    return list(
        db.execute(
            select(StatementRecord)
            .where(
                StatementRecord.household_id == document.household_id,
                StatementRecord.document_id == document.id,
                StatementRecord.kind == "bank",
            )
            .order_by(StatementRecord.period_start, StatementRecord.id)
        ).scalars()
    )


def _has_rows(db: Session, account: Account, statement: Statement) -> bool:
    """A row of ``statement`` is, or was, in the ledger of ``account``."""
    hashes = [value for value, _ in _hashed(statement, account)]
    if not hashes:
        return False
    found = db.execute(
        select(Transaction.id)
        .where(
            Transaction.household_id == account.household_id,
            Transaction.account_id == account.id,
            Transaction.import_hash.in_(hashes),
        )
        .limit(1)
    ).first()
    return (
        found is not None
        or bool(repo.alias_transactions(db, account.household_id, account.id, hashes))
        or bool(tombstones.tombstoned_hashes(db, account.household_id, account.id, hashes))
    )


def _record_of(
    db: Session,
    statement: Statement,
    records: Sequence[StatementRecord],
    accounts: dict[uuid.UUID, Account],
) -> StatementRecord | None:
    """The document's record of ``statement``, or None when none or several fit."""
    if statement.period_start is None:
        return None
    found = [
        r for r in records if r.period_start == statement.period_start and r.account_id in accounts
    ]
    closing = statement.ledger_balance

    def digits(record: StatementRecord) -> bool:
        last4 = statement.account.last4
        return last4 is not None and accounts[record.account_id].number_last4 == last4

    def balance(record: StatementRecord) -> bool:
        return closing is not None and record.closing_balance == to_storage(closing.amount)

    for fits in (
        lambda r: r.period_end == statement.period_end,
        digits,
        balance,
        lambda r: _has_rows(db, accounts[r.account_id], statement),
    ):
        if len(found) <= 1:
            break
        narrowed = [r for r in found if fits(r)]
        if narrowed:
            found = narrowed
    return found[0] if len(found) == 1 else None


@dataclass(frozen=True)
class Placed:
    """A stored bank statement of a document and the account it is on."""

    index: int
    statement: Statement
    account: Account
    record: StatementRecord | None


def placed_statements(db: Session, document: Document, result: ExtractResult) -> list[Placed]:
    """The stored bank statements of ``document`` whose account is known (module doc)."""
    records = _records(db, document)
    wanted = {r.account_id for r in records}
    if document.account_id is not None:
        wanted.add(document.account_id)
    accounts = {
        account.id: account
        for account in (
            accounts_repo.get_account(db, document.household_id, account_id)
            for account_id in sorted(wanted)
        )
        if account is not None
    }
    out: list[Placed] = []
    for index, statement in enumerate(result.statements):
        record = _record_of(db, statement, records, accounts)
        account: Account | None = None
        if record is not None:
            account = accounts[record.account_id]
        elif len(result.statements) == 1 and document.account_id is not None:
            account = accounts.get(document.account_id)
        if account is not None:
            out.append(Placed(index, statement, account, record))
    return out


# ---------------------------------------------------------------------------
# GET /documents/{id}/reconciliation
# ---------------------------------------------------------------------------


def _previous_closing(
    db: Session, document: Document, account: Account, start: date | None
) -> Decimal | None:
    if start is None:
        return None
    found = db.execute(
        select(StatementRecord.closing_balance)
        .where(
            StatementRecord.household_id == account.household_id,
            StatementRecord.account_id == account.id,
            StatementRecord.kind == "bank",
            StatementRecord.document_id != document.id,
            StatementRecord.period_start < start,
            StatementRecord.period_end <= start,
        )
        .order_by(
            StatementRecord.period_end.desc(),
            StatementRecord.period_start.desc(),
            StatementRecord.id.desc(),
        )
        .limit(1)
    ).first()
    return found[0] if found is not None else None


def _place(
    result: ExtractResult, group: _Rows, row: Transaction
) -> tuple[int | None, list[float] | None]:
    """Where the document prints ``row``: by its hash, else by its date and amount."""
    for value in group.hashes.get(row.id, ()):
        read = group.read.get(value)
        if read is not None:
            return read.page, list(read.bbox) if read.bbox is not None else None
    found = row_region(result, page=None, posted=row.posted_date, amount=row.amount)
    return (found.page, list(found.bbox)) if found is not None else (None, None)


def _cause_out(cause: Cause, account: Account) -> ReconcileCause:
    amount: str | None = None
    if cause.amount is not None:
        # The previous closing is a balance (shown as printed); the others are row amounts.
        amount = (
            as_printed(cause.amount, account)
            if cause.kind == "opening_differs_from_previous"
            else _text(cause.amount)
        )
    return ReconcileCause(
        kind=cast(CauseKind, cause.kind),
        transaction_ids=list(cause.rows[:CAUSE_ROWS_MAX]),
        amount=amount,
        page=cause.page,
        bbox=list(cause.bbox) if cause.bbox is not None else None,
    )


def _fee_lines(result: ExtractResult, statement: Statement) -> list[PageAmount]:
    pages: Collection[int] | None = None
    if statement.first_page is not None and statement.last_page is not None:
        pages = range(statement.first_page, statement.last_page + 1)
    return fee_lines(DocumentView.from_result(result), pages)


def _statement_out(
    db: Session,
    document: Document,
    result: ExtractResult,
    placed: Placed,
    group: _Rows,
    *,
    can_edit: bool,
) -> StatementReconciliation:
    statement, account = placed.statement, placed.account
    period = _period(statement)
    opening = statement.opening_balance.amount if statement.opening_balance else None
    closing = statement.ledger_balance.amount if statement.ledger_balance else None
    previous = _previous_closing(db, document, account, statement.period_start)
    total = sum((row.amount for row in group.rows), Decimal(0))
    difference: Decimal | None = None
    status: Literal["balanced", "off", "unchecked"] = "unchecked"
    if opening is not None and closing is not None:
        difference = opening + total - closing
        status = "balanced" if abs(difference) <= TOLERANCE else "off"
    causes = rank_causes(
        difference,
        [LedgerRow(row.id, row.posted_date, row.amount) for row in group.rows],
        period=period,
        opening=opening,
        previous_closing=previous,
        fee_lines=_fee_lines(result, statement) if status == "off" else (),
    )
    rows: list[ReconcileRow] = []
    for row in group.rows[:ROWS_MAX]:
        page, bbox = _place(result, group, row)
        origin: Literal["read", "added", "matched"] = "read"
        if row.source_document_id != document.id:
            origin = "matched"
        elif row.import_hash is None:
            origin = "added"
        rows.append(
            ReconcileRow(
                transaction_id=row.id,
                posted_date=row.posted_date,
                payee=row.raw_payee,
                amount=_text(row.amount),
                review_state=row.review_state,
                origin=origin,
                edited=row.updated_at != row.created_at,
                in_period=period is None or period[0] <= row.posted_date <= period[1],
                page=page,
                bbox=bbox,
            )
        )
    removed = [
        RemovedRow(
            tombstone_id=t.id,
            posted_date=t.posted_date,
            amount=_text(t.amount) if t.amount is not None else None,
            deleted_at=t.deleted_at,
        )
        for t in tombstones.list_tombstones(db, account.household_id, account.id, list(group.read))
    ]
    return StatementReconciliation(
        statement=placed.index,
        account_id=account.id,
        account_name=account.name,
        currency=account.currency,
        printed_sign=printed_sign(account),
        period_start=statement.period_start,
        period_end=statement.period_end,
        opening=as_printed(opening, account),
        closing=as_printed(closing, account),
        previous_closing=as_printed(previous, account),
        rows=rows,
        rows_truncated=len(group.rows) > ROWS_MAX,
        removed=removed,
        total=_text(total),
        difference=_text(difference) if difference is not None else None,
        status=status,
        verification=cast(Verification, placed.record.verification) if placed.record else None,
        causes=[_cause_out(cause, account) for cause in causes],
        lines_skipped=result.parse.skipped_lines if result.parse is not None else 0,
        can_edit=can_edit,
    )


def get_reconciliation(
    db: Session, principal: Principal, document_id: uuid.UUID
) -> DocumentReconciliation:
    """Each statement of the document the caller can see, against the ledger (module doc)."""
    document = get_visible_document(db, principal, document_id)
    result = stored_result(document.extraction)
    if result is None or not result.statements:
        raise statement_unavailable()
    may_change = can_edit_document(db, principal, document)
    by_account: dict[uuid.UUID, list[Placed]] = {}
    for placed in placed_statements(db, document, result):
        by_account.setdefault(placed.account.id, []).append(placed)
    out: list[StatementReconciliation] = []
    for account_id, group in by_account.items():
        if not can_view_account(db, principal, account_id):
            continue
        account = group[0].account
        can_edit = may_change and can_edit_account(db, principal, account_id)
        split = _split(db, document, account, [placed.statement for placed in group])
        out.extend(
            _statement_out(db, document, result, placed, rows, can_edit=can_edit)
            for placed, rows in zip(group, split, strict=True)
        )
    out.sort(key=lambda statement: statement.statement)
    return DocumentReconciliation(document_id=document.id, statements=out)


def confirm(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: ReconcileConfirm,
    *,
    request: HTTPConnection | None,
) -> ReconcileOutcome:
    get_editable_document(db, principal, document_id)
    raise statement_unavailable()


def accept(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: ReconcileAccept,
    *,
    request: HTTPConnection | None,
) -> ReconcileOutcome:
    get_editable_document(db, principal, document_id)
    raise statement_unavailable()


def restore(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: ReconcileRestore,
    *,
    request: HTTPConnection | None,
) -> ReconcileOutcome:
    get_editable_document(db, principal, document_id)
    raise statement_unavailable()


def remove_earlier(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: ReconcileRemoveEarlier,
    *,
    request: HTTPConnection | None,
) -> ReconcileOutcome:
    get_editable_document(db, principal, document_id)
    raise statement_unavailable()

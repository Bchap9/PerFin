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

**The actions** each settle who may ask before anything is written: a hidden
document is ``404``, a read-only one ``403``, and the statement's account
must be one the caller may edit (``404`` otherwise, as for any account).

* :func:`confirm`: the statement balances against the ledger (balances may
  be typed as printed and are stored first). Its pending rows are confirmed,
  its closing balance is recorded as a statement snapshot, its record
  becomes ``reconciled`` and its findings are closed. ``409
  statement-does-not-reconcile`` otherwise, and nothing is kept.
  Neither this nor accepting is offered for a statement the document
  imported nothing of (no record, no row of its own: a second copy that
  waits as an overlap): ``409 statement-unavailable``.
* :func:`accept`: the statement is taken as read. Its pending rows are
  confirmed and its findings closed; the record becomes ``accepted`` and
  **no balance is recorded**. ``409 statement-balanced`` for a statement
  that balances (it is confirmed instead).
* :func:`restore`: a removed row is put back from the stored statement,
  pending, with its original hash; ``404 tombstone-not-found`` for a
  tombstone that is not of this document's statements.
* :func:`remove_earlier`: removes the statement's rows an earlier reading
  of the file left behind (:func:`earlier_rows`). The importer already
  removed those nobody touched; these are the ones it kept because a person
  had changed them. Each leaves a tombstone, like any row a person deletes,
  and the ``earlier_rows_kept`` finding is closed when none is left.

**Rows of an earlier reading** (:func:`earlier_rows`). A row of the account
whose source is this document and whose ``import_hash`` (or alias) is not a
hash of any statement of the document's current reading: the file was read
again and the new reading has no such row. A row without an ``import_hash``
(added by hand for the document) is never one. A document that also holds
brokerage statements has none here: their cash rows are not bank statement
rows, and nothing says which reading they are of.

"Finish later" is no request: the findings stay open.

The findings of a statement are closed by
:func:`app.domain.review.service.close_statement_items`; a question about
another statement of the document (its account, a duplicate) stays open.
Audit rows (``statement.reconcile``, ``statement.accept``,
``tombstone.restore``) carry ids, counts and balances only.
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
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.money import AMOUNT_INTEGER_DIGITS, AMOUNT_PLACES, to_storage
from app.domain.accounts import repo as accounts_repo
from app.domain.accounts.models import LIABILITY_TYPES, Account, BalanceSource
from app.domain.accounts.visibility import (
    can_edit_account,
    can_view_account,
    get_visible_account,
)
from app.domain.documents.manage import can_edit_document, get_editable_document
from app.domain.documents.models import Document, ReviewItem, ReviewItemState, ReviewKind
from app.domain.documents.schemas import Verification
from app.domain.documents.service import get_visible_document
from app.domain.imports import tombstones
from app.domain.imports.models import StatementRecord
from app.domain.review import repo, service
from app.domain.review.actions import correct_balances
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
from app.domain.transactions import service as transactions_service
from app.domain.transactions.import_hash import HashInput, assign_import_hashes
from app.domain.transactions.models import (
    MEMO_MAX,
    PAYEE_MAX,
    ReviewState,
    Transaction,
    TransactionStatus,
)
from app.ingestion.parsers.base import DocumentView
from app.ingestion.parsers.structured import ParsedStatement
from app.ingestion.reconcile import TOLERANCE
from app.ingestion.sandbox.protocol import ExtractedTransaction, ExtractResult, Statement

#: Rows listed for one statement (the sum always counts every row).
ROWS_MAX: Final = 2000
#: Row ids one cause names.
CAUSE_ROWS_MAX: Final = 200

#: Reason of the finding that lists the rows of an earlier reading the
#: importer kept (``imports.service``; kind ``low_confidence_field``).
EARLIER_KEPT: Final = "earlier_rows_kept"

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


def reading_hashes(account: Account, statements: Sequence[ParsedStatement | Statement]) -> set[str]:
    """Every hash the importer gives the rows of ``statements`` on ``account``.

    Rows are numbered per statement, after those in another currency than
    the account's are left out, as ``imports.service`` commits them.
    """
    out: set[str] = set()
    for statement in statements:
        if isinstance(statement, Statement):
            out.update(value for value, _ in _hashed(statement, account))
            continue
        rows = [
            t
            for t in statement.transactions
            if t.currency is None or t.currency == account.currency
        ]
        out.update(
            assign_import_hashes(
                account.id, [HashInput(t.posted_date, t.amount, t.raw_payee) for t in rows]
            )
        )
    return out


def earlier_rows_of(
    db: Session,
    document: Document,
    account_id: uuid.UUID,
    hashes: Collection[str],
    *,
    lock: bool = False,
) -> list[Transaction]:
    """The rows of ``document`` on the account that a reading with ``hashes`` does not hold.

    Rows whose source is the document, that carry an ``import_hash``, and
    of which neither that hash nor an alias is in ``hashes``; oldest first.
    ``lock`` locks them for the caller's change.
    """
    statement = (
        select(Transaction)
        .where(
            Transaction.household_id == document.household_id,
            Transaction.account_id == account_id,
            Transaction.source_document_id == document.id,
            Transaction.import_hash.is_not(None),
        )
        .order_by(Transaction.posted_date, Transaction.id)
    )
    if lock:
        statement = statement.with_for_update()
    wanted = set(hashes)
    aliased = {
        transaction_id
        for transaction_id, _ in repo.alias_transactions(
            db, document.household_id, account_id, sorted(wanted)
        )
    }
    return [
        row
        for row in db.execute(statement).scalars()
        if row.import_hash not in wanted and row.id not in aliased
    ]


def earlier_rows(
    db: Session,
    document: Document,
    account: Account,
    statements: Sequence[ParsedStatement | Statement],
) -> list[Transaction]:
    """Rows of ``account`` an earlier reading of ``document`` left behind (module doc).

    ``statements`` are the statements of the document's current reading for
    the account; naming more of them only spares rows.
    """
    return earlier_rows_of(db, document, account.id, reading_hashes(account, statements))


def _earlier_ids(
    db: Session, document: Document, result: ExtractResult, account: Account
) -> set[uuid.UUID]:
    """The ids of the account's rows of an earlier reading, as the view names them.

    Every stored bank statement of the document counts as the current
    reading, whatever account it is on: a statement whose account is not
    settled yet must not turn its rows into an earlier reading.
    """
    if result.investments:
        return set()
    return {row.id for row in earlier_rows(db, document, account, result.statements)}


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
    taken_out = tombstones.list_tombstones(db, account.household_id, account.id, list(group.read))
    earlier = _earlier_ids(db, document, result, account)
    causes = rank_causes(
        difference,
        [
            LedgerRow(row.id, row.posted_date, row.amount, earlier=row.id in earlier)
            for row in group.rows
        ],
        period=period,
        opening=opening,
        previous_closing=previous,
        fee_lines=_fee_lines(result, statement) if status == "off" else (),
        removed=[t.amount for t in taken_out if t.amount is not None],
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
        for t in taken_out
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


# ---------------------------------------------------------------------------
# The actions
# ---------------------------------------------------------------------------


def statement_not_found() -> ProblemError:
    return ProblemError(
        422, "statement-not-found", "No such statement", "The document has no such statement."
    )


def tombstone_not_found() -> ProblemError:
    return ProblemError(
        404,
        "tombstone-not-found",
        "No such removed row",
        "This document has no removed row with that id; it may have been put back already.",
    )


@dataclass(frozen=True)
class _Open:
    """One statement of a document the caller may change, with its ledger rows."""

    document: Document
    result: ExtractResult
    placed: Placed
    account: Account
    group: _Rows
    #: The document holds another statement of the same account.
    shared: bool
    #: Every statement of the document is a bank statement on a known account.
    whole: bool


def _group(db: Session, document: Document, all_placed: Sequence[Placed], placed: Placed) -> _Rows:
    same = [p for p in all_placed if p.account.id == placed.account.id]
    split = _split(db, document, placed.account, [p.statement for p in same])
    return split[same.index(placed)]


def _load(
    db: Session, document: Document, index: int
) -> tuple[ExtractResult, list[Placed], Placed]:
    result = stored_result(document.extraction)
    if result is None or not result.statements:
        raise statement_unavailable()
    if index >= len(result.statements):
        raise statement_not_found()
    all_placed = placed_statements(db, document, result)
    placed = next((p for p in all_placed if p.index == index), None)
    if placed is None:
        # Its account is not known yet: that question comes first.
        raise statement_unavailable()
    return result, all_placed, placed


def _open(db: Session, principal: Principal, document_id: uuid.UUID, index: int) -> _Open:
    """Statement ``index`` of a document, for a change.

    Who may ask is settled before anything is written: ``404`` for a hidden
    document, ``403`` for a read-only one, then whatever
    ``get_visible_account(need="edit")`` answers for the statement's account.
    The document row is locked: two answers to one statement run in turn.
    """
    document = get_editable_document(db, principal, document_id, lock=True)
    result, all_placed, placed = _load(db, document, index)
    account = get_visible_account(db, principal, placed.account.id, need="edit", lock=True)
    return _Open(
        document=document,
        result=result,
        placed=placed,
        account=account,
        group=_group(db, document, all_placed, placed),
        shared=sum(1 for p in all_placed if p.account.id == account.id) > 1,
        whole=len(all_placed) == len(result.statements) and not result.investments,
    )


def _again(db: Session, opened: _Open) -> _Open:
    """``opened`` read again from the database, after a change."""
    db.flush()
    result, all_placed, placed = _load(db, opened.document, opened.placed.index)
    return _Open(
        document=opened.document,
        result=result,
        placed=placed,
        account=opened.account,
        group=_group(db, opened.document, all_placed, placed),
        shared=opened.shared,
        whole=opened.whole,
    )


def _view(db: Session, opened: _Open) -> StatementReconciliation:
    return _statement_out(
        db, opened.document, opened.result, opened.placed, opened.group, can_edit=True
    )


def _require_imported(opened: _Open) -> None:
    """``409 statement-unavailable`` for a statement this document imported nothing of.

    A second copy of a statement that waits as an overlap has no record
    and no row of its own; the rows it would be checked against are the
    first file's. Confirming it would post nothing and make the copy the
    source of the account's balance, which then goes with the copy when it
    is deleted. Its own question (the same statement, or import anyway) is
    answered first.
    """
    if opened.placed.record is not None:
        return
    if any(row.source_document_id == opened.document.id for row in opened.group.rows):
        return
    raise ProblemError(
        409,
        "statement-unavailable",
        "Nothing imported from this document",
        "Nothing was imported from this document for the statement yet; "
        "answer the document's open question first.",
    )


def _settle(
    db: Session,
    principal: Principal,
    opened: _Open,
    resolution: dict[str, object],
    *,
    request: HTTPConnection | None,
) -> tuple[int, int]:
    """Post the statement's pending rows and close its findings; the two counts."""
    confirmed = service.confirm_rows(
        db,
        principal,
        [row.id for row in opened.group.rows if row.review_state is ReviewState.PENDING],
        request=request,
    )
    closed = service.close_statement_items(
        db,
        principal,
        opened.document,
        opened.account.id,
        {
            **resolution,
            "account_id": str(opened.account.id),
            "statement": opened.placed.index,
            "confirmed": confirmed,
        },
        statement=opened.placed.index,
        transaction_ids=None if not opened.shared else {row.id for row in opened.group.rows},
        whole_document=opened.whole,
        request=request,
    )
    return confirmed, closed


def _outcome(
    db: Session, opened: _Open, *, confirmed: int = 0, snapshot: bool = False, closed: int = 0
) -> ReconcileOutcome:
    return ReconcileOutcome(
        statement=_view(db, _again(db, opened)),
        confirmed=confirmed,
        snapshot=snapshot,
        items_closed=closed,
    )


def confirm(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: ReconcileConfirm,
    *,
    request: HTTPConnection | None,
) -> ReconcileOutcome:
    """The statement balances against the ledger: post it and record its balance.

    Balances typed with the request (as printed) replace the stored ones
    first. ``409 statement-does-not-reconcile`` while the rows and the
    balances differ, or a balance is missing; nothing is kept then.
    """
    opened = _open(db, principal, document_id, body.statement)
    _require_imported(opened)
    typed = {
        name: read_printed(text) if text is not None else None
        for name, text in (("opening", body.opening_balance), ("closing", body.closing_balance))
    }
    if typed["opening"] is not None or typed["closing"] is not None:
        correct_balances(
            db,
            opened.document,
            None,
            opening=typed["opening"],
            closing=typed["closing"],
            statement=body.statement,
            as_printed=True,
            account=opened.account,
        )
        opened = _again(db, opened)
    shown = _view(db, opened)
    if shown.status != "balanced":
        raise ProblemError(
            409,
            "statement-does-not-reconcile",
            "Still does not add up",
            "Both balances are needed to check the statement; type the missing one."
            if shown.status == "unchecked"
            else f"With these balances the rows differ from the closing balance by "
            f"{shown.difference}; correct the rows or the balances.",
        )
    statement, account, record = opened.placed.statement, opened.account, opened.placed.record
    closing = statement.ledger_balance
    if closing is None:  # pragma: no cover - a balanced statement has both balances
        raise statement_unavailable()
    confirmed, closed = _settle(db, principal, opened, {"reconciled": True}, request=request)
    as_of = closing.as_of or statement.period_end
    snapshot = False
    if as_of is not None:
        accounts_repo.upsert_snapshot(
            db,
            account,
            as_of,
            to_storage(closing.amount),
            BalanceSource.STATEMENT,
            source_document_id=opened.document.id,
        )
        emit(db, account.household_id, "balance.updated", {"account_id": str(account.id)})
        snapshot = True
    if record is not None:
        record.verification = "reconciled"
        record.closing_balance = to_storage(closing.amount)
    db.flush()
    audit(
        db,
        action="statement.reconcile",
        target_type="document",
        target_id=opened.document.id,
        after={
            "account_id": account.id,
            "statement": opened.placed.index,
            "opening": shown.opening,
            "closing": shown.closing,
            "balances_typed": sorted(name for name, value in typed.items() if value is not None),
            "confirmed": confirmed,
            "items_closed": closed,
            "snapshot": snapshot,
        },
        household_id=account.household_id,
        user_id=principal.user_id,
        request=request,
    )
    return _outcome(db, opened, confirmed=confirmed, snapshot=snapshot, closed=closed)


def accept(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: ReconcileAccept,
    *,
    request: HTTPConnection | None,
) -> ReconcileOutcome:
    """Accept a statement as read without balancing it: rows posted, no balance recorded.

    ``409 statement-balanced`` for a statement that balances: that one is
    confirmed, which records its balance.
    """
    opened = _open(db, principal, document_id, body.statement)
    _require_imported(opened)
    shown = _view(db, opened)
    if shown.status == "balanced":
        raise ProblemError(
            409,
            "statement-balanced",
            "The statement balances",
            "The rows add up to the closing balance. Confirm it instead.",
        )
    confirmed, closed = _settle(db, principal, opened, {"accepted": True}, request=request)
    if opened.placed.record is not None:
        opened.placed.record.verification = "accepted"
    db.flush()
    audit(
        db,
        action="statement.accept",
        target_type="document",
        target_id=opened.document.id,
        after={
            "account_id": opened.account.id,
            "statement": opened.placed.index,
            "difference": shown.difference,
            "confirmed": confirmed,
            "items_closed": closed,
            "snapshot": False,
        },
        household_id=opened.account.household_id,
        user_id=principal.user_id,
        request=request,
    )
    return _outcome(db, opened, confirmed=confirmed, closed=closed)


def _removed(
    db: Session, document: Document, all_placed: Sequence[Placed], tombstone_id: uuid.UUID
) -> tuple[Placed, str] | None:
    """The statement of the document one of whose rows ``tombstone_id`` removed, and the hash."""
    for placed in all_placed:
        hashes = [value for value, _ in _hashed(placed.statement, placed.account)]
        for tombstone in tombstones.list_tombstones(
            db, placed.account.household_id, placed.account.id, hashes
        ):
            if tombstone.id == tombstone_id:
                return placed, str(tombstone.import_hash)
    return None


def restore(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: ReconcileRestore,
    *,
    request: HTTPConnection | None,
) -> ReconcileOutcome:
    """Put a removed row of the document back, as the statement prints it, pending.

    ``404 tombstone-not-found`` unless the tombstone stands for a row of one
    of this document's statements. A statement that was confirmed as
    balanced and no longer is goes back to ``unverified``.
    """
    document = get_editable_document(db, principal, document_id, lock=True)
    result = stored_result(document.extraction)
    if result is None or not result.statements:
        raise statement_unavailable()
    found = _removed(db, document, placed_statements(db, document, result), body.tombstone_id)
    if found is None:
        raise tombstone_not_found()
    opened = _open(db, principal, document_id, found[0].index)
    value, account = found[1], opened.account
    printed = opened.group.read.get(value)
    if printed is None:  # pragma: no cover - the hash was found among this statement's
        raise tombstone_not_found()
    tombstones.remove_tombstone(db, account.household_id, body.tombstone_id)
    held = any(value in mine for mine in opened.group.hashes.values())
    row: Transaction | None = None
    if not held:
        # As the importer writes a row it is unsure of; rules do not run again.
        row = Transaction(
            household_id=account.household_id,
            account_id=account.id,
            posted_date=printed.posted,
            amount=to_storage(printed.amount),
            currency=account.currency,
            raw_payee=printed.payee[:PAYEE_MAX] if printed.payee else None,
            memo=printed.memo[:MEMO_MAX] if printed.memo else None,
            status=TransactionStatus.CLEARED,
            source_document_id=document.id,
            import_hash=value,
            review_state=ReviewState.PENDING,
        )
        db.add(row)
        db.flush()
        emit(
            db,
            account.household_id,
            "transaction.created",
            {"id": str(row.id), "account_id": str(account.id)},
        )
    audit(
        db,
        action="tombstone.restore",
        target_type="document",
        target_id=document.id,
        after={
            "account_id": account.id,
            "statement": opened.placed.index,
            "tombstone_id": body.tombstone_id,
            "transaction_id": row.id if row is not None else None,
        },
        household_id=account.household_id,
        user_id=principal.user_id,
        request=request,
    )
    after = _again(db, opened)
    record = after.placed.record
    if (
        record is not None
        and record.verification == "reconciled"
        and _view(db, after).status != "balanced"
    ):
        record.verification = "unverified"
        db.flush()
    return _outcome(db, opened)


def _close_earlier_items(
    db: Session,
    principal: Principal,
    document: Document,
    account_id: uuid.UUID,
    resolution: dict[str, object],
    *,
    request: HTTPConnection | None,
) -> int:
    """Resolve the document's open ``earlier_rows_kept`` findings of the account."""
    found = [
        item
        for item in db.execute(
            select(ReviewItem)
            .where(
                ReviewItem.household_id == document.household_id,
                ReviewItem.document_id == document.id,
                ReviewItem.state == ReviewItemState.OPEN,
                ReviewItem.kind == ReviewKind.LOW_CONFIDENCE_FIELD,
            )
            .order_by(ReviewItem.created_at, ReviewItem.id)
            .with_for_update()
        ).scalars()
        if (item.payload or {}).get("reason") == EARLIER_KEPT
        and (item.payload or {}).get("account_id") == str(account_id)
    ]
    for item in found:
        service.close_item(db, principal, item, dict(resolution), request=request)
    return len(found)


def remove_earlier(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: ReconcileRemoveEarlier,
    *,
    request: HTTPConnection | None,
) -> ReconcileOutcome:
    """Remove the statement's rows an earlier reading of the file left behind.

    Each goes the way a row a person deletes does (audited, with a
    tombstone, so no later reading brings it back). When the account has no
    such row left, its ``earlier_rows_kept`` finding is closed. A statement
    that was confirmed as balanced and no longer is goes back to
    ``unverified``. With nothing to remove the statement is returned as it is.
    """
    opened = _open(db, principal, document_id, body.statement)
    account, document = opened.account, opened.document
    earlier = _earlier_ids(db, document, opened.result, account)
    mine = [row.id for row in opened.group.rows if row.id in earlier]
    for transaction_id in mine:
        row = transactions_service.get_visible_transaction(
            db, principal, transaction_id, need="edit", lock=True
        )
        transactions_service.delete_transaction(db, principal, row, request=request, tombstone=True)
    db.flush()
    closed = 0
    if mine and not _earlier_ids(db, document, opened.result, account):
        closed = _close_earlier_items(
            db,
            principal,
            document,
            account.id,
            {"removed": len(mine), "account_id": str(account.id), "statement": body.statement},
            request=request,
        )
    after = _again(db, opened)
    record = after.placed.record
    if (
        mine
        and record is not None
        and record.verification == "reconciled"
        and _view(db, after).status != "balanced"
    ):
        record.verification = "unverified"
        db.flush()
    return _outcome(db, opened, closed=closed)

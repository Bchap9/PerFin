# SPDX-License-Identifier: AGPL-3.0-or-later
"""The accounts a document names, which of them PerFin knows, and proposed new ones.

``GET /documents/{id}/accounts`` lists one :class:`DetectedAccount` per
account the document's stored extraction names (an OFX/QIF/PDF statement, a
brokerage statement or activity export's account, a group of a
multi-account CSV, or the one statement of a single-account table), read
again from the stored extraction (never stored themselves):

* ``matched``: an account of the caller's matches it (by last four digits,
  institution breaking ties) and nothing about it is waiting for review;
* ``new``: an open ``unknown_account`` item says no account matched; a
  ``proposal`` (name, type, currency, institution, last four digits, an
  opening balance when the statement prints one) is ready to create;
* ``ambiguous``: several accounts match (``candidates``), plus a proposal;
* ``conflict``: an account matched but cannot take it (another currency, not
  an investment account for a brokerage statement), plus a proposal;
* ``done``: nothing matches and nothing waits (already imported by a choice
  made earlier).

``POST /documents/{id}/accounts/apply`` takes one choice per account (use an
existing account, or create one from the edited proposal), creates the new
accounts (and institutions by name) in the caller's transaction, records the
last four digits on accounts that have none, and queues **one** re-import of
the stored extraction with every choice as an assignment
(``imports.apply_mapping``), which resolves the ``unknown_account`` items.

Visibility (SECURITY.md F3, H8): the caller must be able to change the
document (``can_edit_document``); only accounts they may import into are
matched or offered; the last four digits are returned only here, to such a
caller, as the review queue's own account suggestions are.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.principal import Principal
from app.core.errors import ProblemError
from app.domain.accounts import service as accounts_service
from app.domain.accounts.models import AccountType, Institution
from app.domain.accounts.schemas import AccountCreate
from app.domain.accounts.subtypes import guess_subtype, valid_subtype
from app.domain.accounts.visibility import get_visible_account
from app.domain.documents.manage import get_editable_document
from app.domain.documents.models import Document, ReviewItem, ReviewItemState, ReviewKind
from app.domain.households import repo as households_repo
from app.domain.imports import repo
from app.domain.imports.schemas import (
    AccountChoice,
    AccountProposal,
    ApplyMapping,
    Assignment,
    AssignmentTarget,
    DetectedAccount,
    DetectedStatus,
    DocumentAccounts,
    DocumentAccountsApplied,
    DocumentAccountsApply,
    MappingSpec,
)
from app.domain.imports.service import (
    _item_spec,
    _spec,
    _stored_result,
    choose_mapping,
    request_apply,
    stored_investments,
)
from app.ingestion.match.accounts import importable_account_ids, match_account
from app.ingestion.parsers.structured import from_table_grouped
from app.ingestion.sandbox.protocol import ExtractResult, InvestmentStatement, Statement
from app.ml.client import MLAccount, MLHints

#: A statement's own account-type word -> the PerFin account type it proposes.
TYPE_WORDS: Final[dict[str, AccountType]] = {
    "checking": AccountType.CHECKING,
    "savings": AccountType.SAVINGS,
    "moneymrkt": AccountType.SAVINGS,
    "money_market": AccountType.SAVINGS,
    "creditcard": AccountType.CREDIT_CARD,
    "credit_card": AccountType.CREDIT_CARD,
    "credit": AccountType.CREDIT_CARD,
    "creditline": AccountType.LINE_OF_CREDIT,
    "line_of_credit": AccountType.LINE_OF_CREDIT,
    "brokerage": AccountType.BROKERAGE,
    "investment": AccountType.BROKERAGE,
    "retirement": AccountType.RETIREMENT,
    "loan": AccountType.LOAN,
    "mortgage": AccountType.MORTGAGE,
}
TYPE_LABELS: Final[dict[AccountType, str]] = {
    AccountType.CHECKING: "Checking",
    AccountType.SAVINGS: "Savings",
    AccountType.CREDIT_CARD: "Credit card",
    AccountType.LINE_OF_CREDIT: "Line of credit",
    AccountType.BROKERAGE: "Brokerage",
    AccountType.RETIREMENT: "Retirement",
    AccountType.LOAN: "Loan",
    AccountType.MORTGAGE: "Mortgage",
}
INVESTMENT_TYPES: Final = frozenset({AccountType.BROKERAGE, AccountType.RETIREMENT})
NAME_MAX: Final = 120


@dataclass
class _Found:
    """One account the extraction names (before matching)."""

    target: AssignmentTarget
    index: int
    last4: str | None
    name: str | None
    institution: str | None
    account_type: str | None
    currency: str | None
    period_start: date | None
    period_end: date | None
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    transactions: int
    #: Filled in from the local model's hints (ADR-0010).
    from_model: bool = False
    #: The subtype the document prints (an account index's group, a PDF parser).
    subtype: str | None = None


def _statements(result: ExtractResult) -> list[_Found]:
    found: list[_Found] = []
    for index, statement in enumerate(result.statements):
        found.append(_bank(index, statement))
    return found


def _bank(index: int, statement: Statement) -> _Found:
    account = statement.account
    return _Found(
        target="statement",
        index=index,
        last4=account.last4,
        name=account.name,
        institution=account.institution,
        account_type=account.account_type,
        currency=account.currency,
        period_start=statement.period_start,
        period_end=statement.period_end,
        opening_balance=(
            statement.opening_balance.amount if statement.opening_balance is not None else None
        ),
        closing_balance=(
            statement.ledger_balance.amount if statement.ledger_balance is not None else None
        ),
        transactions=len(statement.transactions),
        subtype=account.subtype,
    )


def _investment(index: int, statement: InvestmentStatement) -> _Found:
    account = statement.account
    closing = statement.total_value or statement.cash_closing
    return _Found(
        target="investment",
        index=index,
        last4=account.last4,
        name=account.name,
        institution=account.institution,
        account_type=account.account_type or "brokerage",
        currency=account.currency,
        period_start=statement.period_start,
        period_end=statement.period_end,
        opening_balance=None,
        closing_balance=closing.amount if closing is not None else None,
        transactions=len(statement.activity),
        subtype=account.subtype,
    )


def _table_spec(
    db: Session, document: Document, result: ExtractResult, items: Sequence[ReviewItem]
) -> MappingSpec | None:
    """The cash mapping the import used: the one an item names, else the saved one."""
    for item in items:
        spec = _item_spec(db, document, item.payload or {})
        if spec is not None:
            return spec
        raw = (item.payload or {}).get("mapping_id")
        if isinstance(raw, str):
            try:
                mapping = repo.get_mapping(db, document.household_id, uuid.UUID(raw))
            except ValueError:
                mapping = None
            if mapping is not None:
                return _spec(mapping)
    choice = choose_mapping(
        db, document.household_id, result.tables[0], account_id=document.account_id, allowed=None
    )
    return choice.spec


def _tables(
    db: Session, document: Document, result: ExtractResult, items: Sequence[ReviewItem]
) -> list[_Found]:
    spec = _table_spec(db, document, result, items)
    if spec is None or spec.mode != "cash":
        return []
    statements = from_table_grouped(result.tables[0], spec)
    if spec.account_column is None:
        parsed = statements[0] if statements else None
        if parsed is None:
            return []
        return [_parsed("table", 0, parsed)]
    readable = [s for s in statements if s.transactions]
    return [_parsed("group", index, parsed) for index, parsed in enumerate(readable)]


def _parsed(target: AssignmentTarget, index: int, parsed: Any) -> _Found:
    closing = parsed.closing_balance
    return _Found(
        target=target,
        index=index,
        last4=parsed.last4,
        name=None,
        institution=parsed.institution,
        account_type=parsed.account_type,
        currency=parsed.currency,
        period_start=parsed.period_start,
        period_end=parsed.period_end,
        opening_balance=parsed.opening_balance,
        closing_balance=closing.amount if closing is not None else None,
        transactions=len(parsed.transactions),
    )


def _found(
    db: Session, document: Document, result: ExtractResult, items: Sequence[ReviewItem]
) -> list[_Found]:
    if result.statements:
        found = _statements(result)
    elif investments := stored_investments(document, db):
        found = [_investment(i, s) for i, s in enumerate(investments)]
    elif result.tables:
        found = _tables(db, document, result, items)
    else:
        found = []
    hints = MLHints.from_stored((document.confidence or {}).get("ml"))
    if hints is not None:
        _with_hints(found, hints)
    return found


def _with_hints(found: list[_Found], hints: MLHints) -> None:
    """Fill what the file's own fields left empty from the model's checked hints.

    The institution for every account; an account's last four digits, name
    and type when the file names one account and the model saw one, else by
    matching last four digits. The hints' digits were printed in the
    document (``app.ml.client.validated``).
    """
    for entry in found:
        if entry.institution is None and hints.institution is not None:
            entry.institution, entry.from_model = hints.institution, True
    pairs: list[tuple[_Found, MLAccount]] = []
    if len(found) == 1 and len(hints.accounts) == 1:
        pairs = [(found[0], hints.accounts[0])]
    else:
        by_last4 = {a.last4: a for a in hints.accounts if a.last4 is not None}
        pairs = [(e, by_last4[e.last4]) for e in found if e.last4 in by_last4]
    for entry, hint in pairs:
        if entry.last4 is None and hint.last4 is not None:
            entry.last4, entry.from_model = hint.last4, True
        if entry.name is None and hint.name is not None:
            entry.name, entry.from_model = hint.name, True
        if entry.account_type is None and hint.type is not None:
            entry.account_type, entry.from_model = hint.type, True
        if entry.currency is None and hint.currency is not None:
            entry.currency, entry.from_model = hint.currency, True


def _item_for(found: _Found, items: Sequence[ReviewItem]) -> ReviewItem | None:
    """The open ``unknown_account`` item about this account of the document."""
    for item in items:
        payload = item.payload or {}
        investment = payload.get("investment") is True
        if (
            found.target == "statement"
            and not investment
            and payload.get("statement") == found.index
        ):
            return item
        if found.target == "investment" and investment and payload.get("statement") == found.index:
            return item
        if found.target == "group" and payload.get("group") == found.index:
            return item
        if found.target == "table" and payload.get("reason") == "account_required":
            return item
    return None


def _type(found: _Found) -> AccountType:
    word = (found.account_type or "").strip().lower()
    kind = TYPE_WORDS.get(word)
    if kind is not None:
        return kind
    return AccountType.BROKERAGE if found.target == "investment" else AccountType.CHECKING


def _proposal(found: _Found, institution: str | None, base_currency: str | None) -> AccountProposal:
    kind = _type(found)
    # The account's own name says what kind it is (``ROTH IRA``, ``529``,
    # ``Health Savings Account``); an investment account may move between
    # brokerage and retirement on it.
    guessed, subtype = guess_subtype(found.name, None if kind in INVESTMENT_TYPES else kind)
    if kind in INVESTMENT_TYPES and guessed is not None:
        kind = guessed
    if found.subtype is not None and valid_subtype(kind, found.subtype):
        # The document said so (an index's ``EDUCATION (529) ACCOUNTS`` group).
        subtype = found.subtype
    label = TYPE_LABELS.get(kind, "Account")
    if found.name and institution and institution.lower() not in found.name.lower():
        name = f"{institution.split()[0]} {found.name}"
    elif found.name:
        name = found.name
    else:
        name = f"{institution.split()[0]} {label}" if institution else label
    if found.last4 is not None and found.last4 not in name:
        name = f"{name} {found.last4}"
    opening_date = found.period_start if found.opening_balance is not None else None
    return AccountProposal(
        name=name[:NAME_MAX],
        type=kind,
        subtype=subtype,
        currency=found.currency or base_currency,
        institution_name=institution,
        number_last4=found.last4,
        opening_balance=(
            f"{found.opening_balance:f}"
            if found.opening_balance is not None and kind not in INVESTMENT_TYPES
            else None
        ),
        opening_balance_date=opening_date if kind not in INVESTMENT_TYPES else None,
        suggested_by_model=found.from_model,
    )


def _open_items(db: Session, document: Document) -> list[ReviewItem]:
    return list(
        db.execute(
            select(ReviewItem).where(
                ReviewItem.household_id == document.household_id,
                ReviewItem.document_id == document.id,
                ReviewItem.kind == ReviewKind.UNKNOWN_ACCOUNT,
                ReviewItem.state == ReviewItemState.OPEN,
            )
        ).scalars()
    )


def _institution_name(db: Session, document: Document) -> str | None:
    if document.institution_id is None:
        return None
    return db.execute(
        select(Institution.name).where(
            Institution.household_id == document.household_id,
            Institution.id == document.institution_id,
        )
    ).scalar_one_or_none()


def _base_currency(db: Session, household_id: uuid.UUID) -> str | None:
    household = households_repo.get_household(db, household_id)
    return household.base_currency if household is not None else None


def _detect(
    db: Session, principal: Principal, document: Document
) -> list[tuple[_Found, DetectedAccount]]:
    result = _stored_result(document)
    if result is None:
        return []
    items = _open_items(db, document)
    allowed = importable_account_ids(db, document.household_id, principal.user_id)
    fallback_institution = _institution_name(db, document)
    currency = _base_currency(db, document.household_id)
    out: list[tuple[_Found, DetectedAccount]] = []
    for found in _found(db, document, result, items):
        institution = found.institution or fallback_institution
        item = _item_for(found, items)
        candidates: list[uuid.UUID] = []
        account_id: uuid.UUID | None = None
        status: DetectedStatus
        if item is not None:
            payload = item.payload or {}
            reason = payload.get("reason")
            raw = payload.get("candidates")
            for value in raw if isinstance(raw, list) else []:
                try:
                    candidates.append(uuid.UUID(str(value)))
                except ValueError:
                    continue
            if reason in ("currency_mismatch", "account_type_mismatch"):
                status = "conflict"
                try:
                    account_id = uuid.UUID(str(payload.get("account_id")))
                except ValueError:
                    account_id = None
            else:
                status = "ambiguous" if candidates else "new"
        else:
            match = match_account(
                db,
                document.household_id,
                last4=found.last4,
                institution=institution,
                allowed=allowed,
            )
            account_id = match.account_id
            status = "matched" if account_id is not None else "done"
            if account_id is None and match.candidates:
                status, candidates = "ambiguous", list(match.candidates)
        out.append(
            (
                found,
                DetectedAccount(
                    target=found.target,
                    index=found.index,
                    status=status,
                    account_id=account_id,
                    candidates=candidates,
                    review_item_id=item.id if item is not None else None,
                    last4=found.last4,
                    name=found.name,
                    institution_name=institution,
                    account_type=found.account_type,
                    currency=found.currency,
                    period_start=found.period_start,
                    period_end=found.period_end,
                    opening_balance=(
                        f"{found.opening_balance:f}" if found.opening_balance is not None else None
                    ),
                    closing_balance=(
                        f"{found.closing_balance:f}" if found.closing_balance is not None else None
                    ),
                    transactions=found.transactions,
                    proposal=(
                        _proposal(found, institution, currency)
                        if status in ("new", "ambiguous", "conflict")
                        else None
                    ),
                ),
            )
        )
    return out


def detect_accounts(db: Session, principal: Principal, document_id: uuid.UUID) -> DocumentAccounts:
    """``GET /documents/{id}/accounts`` (module doc)."""
    document = get_editable_document(db, principal, document_id)
    return DocumentAccounts(
        document_id=document.id,
        accounts=[detected for _, detected in _detect(db, principal, document)],
    )


def _invalid(detail: str) -> ProblemError:
    return ProblemError(422, "invalid-account-choice", "Invalid account choice", detail)


def apply_accounts(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    data: DocumentAccountsApply,
    *,
    request: HTTPConnection | None,
) -> DocumentAccountsApplied:
    """``POST /documents/{id}/accounts/apply`` (module doc)."""
    document = get_editable_document(db, principal, document_id)
    detected = {(f.target, f.index): f for f, _ in _detect(db, principal, document)}
    created: list[uuid.UUID] = []
    assignments: list[Assignment] = []
    seen: set[tuple[str, int]] = set()
    for choice in data.items:
        key = (choice.target, choice.index)
        found = detected.get(key)
        if found is None:
            raise _invalid(f"The document has no {choice.target} {choice.index}.")
        if key in seen:
            raise _invalid(f"{choice.target} {choice.index} is chosen twice.")
        seen.add(key)
        account_id = _chosen_account(db, principal, choice, found, request=request)
        if choice.create is not None:
            created.append(account_id)
        assignments.append(
            Assignment(target=choice.target, index=choice.index, account_id=account_id)
        )
    accepted = request_apply(
        db, principal, document.id, ApplyMapping(assignments=assignments), request=request
    )
    return DocumentAccountsApplied(document_id=document.id, created=created, job_id=accepted.job_id)


def _chosen_account(
    db: Session,
    principal: Principal,
    choice: AccountChoice,
    found: _Found,
    *,
    request: HTTPConnection | None,
) -> uuid.UUID:
    if choice.create is None:
        if choice.account_id is None:  # pragma: no cover - the schema requires one
            raise _invalid("Choose an account or create one.")
        account = get_visible_account(db, principal, choice.account_id, need="edit")
        if found.last4 is not None and account.number_last4 is None:
            # Remembered, so the next file with these digits matches on its own.
            account.number_last4 = found.last4
        return account.id
    data: AccountCreate = choice.create
    if choice.institution_name and data.institution_id is None:
        institution_id = accounts_service.find_or_create_institution(
            db, principal, choice.institution_name, request=request
        )
        data = data.model_copy(update={"institution_id": institution_id})
    if data.number_last4 is None and found.last4 is not None:
        data = data.model_copy(update={"number_last4": found.last4})
    if found.target == "investment" and data.type not in INVESTMENT_TYPES:
        raise _invalid("A brokerage statement needs a brokerage or retirement account.")
    return accounts_service.create_account(db, principal, data, request=request).id

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Receipt -> transaction matching (INGESTION.md "Match and deduplicate").

A parsed receipt (:class:`app.ingestion.sandbox.protocol.Receipt`, from the
receipt parser on a photo, scan, e-receipt PDF or receipt mail) is linked to
the ledger transaction that paid it, as a ``document_link`` (``target_type =
transaction``).

**Merchant.** :func:`resolve_merchant` first looks the receipt's merchant
candidates (header lines, the mail's sender) up among the household's
**payees** (name and aliases, compared like payees elsewhere:
:func:`app.domain.transactions.import_hash.normalize_payee`); a known payee
becomes the receipt's merchant (``payee_id``) with confidence 1.0 (0.9 for
a near spelling).

**Candidates** (:func:`find_candidates`): transactions of the household,
not transfers, on accounts the uploader may **edit**
(:func:`app.ingestion.match.accounts.importable_account_ids`: owner/admin
and intake without an uploader: every account; a member: the accounts
granted ``edit``; a viewer: none) and, when the receipt was filed under an
account, only that account (SECURITY.md F3: a suggestion never names a
transaction the people who can see the receipt cannot see). Posted within
:data:`DATE_WINDOW` days of the receipt date (a receipt without a readable
date: the :data:`UNDATED_WINDOW` days before it arrived, exact amounts
only), in the receipt's currency when it printed one, for the total exactly
(a purchase: ``-total``; a refund: ``+total``) or up to
:data:`TIP_ALLOWANCE` above it (a tip added to a card slip).

**Score** per candidate, 0..1: ``0.5 amount + 0.2 date + 0.3 merchant``.
Amount: exact purchase 1.0, exact refund 0.6, above the total by a tip
0.6 down to 0. Date: same day 1.0, then 0.9, 0.75, 0.6 for one to three
days (undated: 0.3). Merchant: the best similarity between the receipt's
merchant candidates and the transaction's raw payee, payee name and payee
aliases (same payee as resolved: 1.0; equal keys 1.0; all words of the
shorter contained in the longer ("SQ *CORNER MARKET 0412" and "Corner
Market"): 0.9; otherwise the character similarity).

**Decision** (:func:`apply_receipt`): the best candidate is linked when the
match is *confident and unique*: exact amount, the receipt's total read at
or above the threshold and a printed date, a score of at least
:data:`AUTO_LINK`, at least :data:`MARGIN` ahead of the next candidate, and
the transaction not already carrying another receipt (one that came from
the same mail does not count). Otherwise a ``receipt_match`` review item
("attach this receipt to ...") lists up to :data:`MAX_SUGGESTIONS`
candidates, best first, as ids and short facts (score, exact/refund/tip,
days apart), never the receipt's text or card digits. Resolving it
(:func:`resolve_receipt_item`) links the transaction the person chose (one
they may edit) or records that none applies.

**Later transactions** (:func:`rematch_receipts`): a receipt photographed
before the statement arrived waits in review; when a document's import
adds transactions, the worker runs the matcher again for the household's
open ``receipt_match`` items and links (and resolves) those that now have a
confident unique match.

The worker links without a person (audit ``document.link`` with
``auto: true`` and no user). Card digits from a receipt are never logged,
audited or put in a review payload.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any, Final, Literal

from sqlalchemy import String, and_, cast, or_, select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.events import emit
from app.core.logging import get_logger
from app.domain.documents import repo as documents_repo
from app.domain.documents.models import (
    BlobArea,
    Document,
    DocumentKind,
    DocumentLink,
    DocumentStatus,
    LinkTarget,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.review.repo import open_items_of
from app.domain.transactions.import_hash import normalize_payee
from app.domain.transactions.models import Payee, Transaction
from app.ingestion.match.accounts import importable_account_ids
from app.ingestion.sandbox.protocol import ExtractResult, Receipt

log = get_logger(__name__)

DATE_WINDOW: Final = 3
UNDATED_WINDOW: Final = 60
#: A card total may exceed the printed total by a tip of up to this share.
TIP_ALLOWANCE: Final = Decimal("0.30")
AUTO_LINK: Final = 0.85
MARGIN: Final = 0.1
#: Candidates below this score are not suggested.
SUGGEST_MIN: Final = 0.35
MAX_SUGGESTIONS: Final = 5
MAX_CANDIDATES: Final = 200
MAX_PAYEES: Final = 5000
#: Open receipt items re-matched per new document.
MAX_REMATCH: Final = 200
MATCHER_VERSION: Final = "receipt-match-1"
_DATE_SCORES: Final = {0: 1.0, 1: 0.9, 2: 0.75, 3: 0.6}

AmountMatch = Literal["exact", "refund", "tip"]


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def merchant_similarity(a: str | None, b: str | None) -> float:
    """How alike two merchant names are (0..1, module doc "Score")."""
    ka, kb = normalize_payee(a), normalize_payee(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    ta, tb = set(ka.split()), set(kb.split())
    short, long = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if short and short <= long and sum(len(t) for t in short) >= 4:
        return 0.9
    return round(SequenceMatcher(None, ka, kb).ratio(), 4)


def _best_similarity(names: list[str], others: list[str]) -> float:
    return max((merchant_similarity(a, b) for a in names for b in others), default=0.0)


# ---------------------------------------------------------------------------
# Merchant -> payee
# ---------------------------------------------------------------------------


def _names(receipt: Receipt) -> list[str]:
    names = [receipt.merchant] if receipt.merchant else []
    names.extend(c for c in receipt.merchant_candidates if c not in names)
    return names[:8]


def resolve_merchant(db: Session, household_id: uuid.UUID, receipt: Receipt) -> Receipt:
    """``receipt`` with its merchant resolved to a household payee, if one fits."""
    names = _names(receipt)
    if not names:
        return receipt
    payees = db.execute(
        select(Payee.id, Payee.name, Payee.aliases)
        .where(Payee.household_id == household_id)
        .order_by(Payee.id)
        .limit(MAX_PAYEES)
    ).all()
    best: tuple[float, uuid.UUID, str] | None = None
    for payee_id, name, aliases in payees:
        score = _best_similarity(names, [name, *(aliases or [])])
        if score >= 0.85 and (best is None or score > best[0]):
            best = (score, payee_id, name)
    if best is None:
        return receipt
    score, payee_id, name = best
    confidence = dict(receipt.confidence)
    confidence["merchant"] = max(confidence.get("merchant", 0.0), 1.0 if score >= 0.9 else 0.9)
    return receipt.model_copy(
        update={
            "merchant": name[:200],
            "payee_id": payee_id,
            "confidence": confidence,
            "low_confidence": [f for f in receipt.low_confidence if f != "merchant"],
        }
    )


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    transaction_id: uuid.UUID
    account_id: uuid.UUID
    score: float
    amount: AmountMatch
    days: int | None
    merchant: float
    already_linked: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "transaction_id": str(self.transaction_id),
            "account_id": str(self.account_id),
            "score": self.score,
            "amount": self.amount,
            "days": self.days,
            "already_linked": self.already_linked,
        }


def _amount_score(amount: Decimal, total: Decimal) -> tuple[AmountMatch, float] | None:
    if abs(amount + total) < Decimal("0.005"):
        return "exact", 1.0
    if abs(amount - total) < Decimal("0.005"):
        return "refund", 0.6
    if amount < 0 and total > 0:
        over = (-amount - total) / total
        if 0 < over <= TIP_ALLOWANCE:
            return "tip", round(float(Decimal("0.6") * (1 - over / TIP_ALLOWANCE)), 4)
    return None


def _related_documents(db: Session, document: Document) -> set[uuid.UUID]:
    """The mail or archive the document came from and the other documents in it."""
    meta = document.source_meta or {}
    parent = meta.get("parent_document_id") if isinstance(meta, dict) else None
    roots = {str(document.id)}
    if isinstance(parent, str):
        roots.add(parent)
    related = {document.id}
    for value in roots:
        try:
            related.add(uuid.UUID(value))
        except ValueError:
            continue
    rows = db.execute(
        select(Document.id).where(
            Document.household_id == document.household_id,
            cast(Document.source_meta["parent_document_id"].astext, String).in_(sorted(roots)),
        )
    ).scalars()
    related.update(rows)
    return related


def find_candidates(
    db: Session,
    document: Document,
    receipt: Receipt,
    *,
    only_source_document: uuid.UUID | None = None,
) -> list[Candidate]:
    """Transactions that may have paid ``receipt``, best first (module doc)."""
    total = receipt.total
    if total is None or total <= 0:
        return []
    allowed = importable_account_ids(db, document.household_id, document.uploaded_by)
    if allowed is not None and not allowed:
        return []
    if receipt.purchase_date is not None:
        start = receipt.purchase_date - timedelta(days=DATE_WINDOW)
        end = receipt.purchase_date + timedelta(days=DATE_WINDOW)
        undated = False
    else:
        arrived = document.created_at.date() if document.created_at else clock.utcnow().date()
        start, end, undated = arrived - timedelta(days=UNDATED_WINDOW), arrived, True
    low, high = total, total * (1 + TIP_ALLOWANCE)
    query = (
        select(Transaction, Payee.name, Payee.aliases)
        .outerjoin(
            Payee,
            (Payee.household_id == Transaction.household_id) & (Payee.id == Transaction.payee_id),
        )
        .where(
            Transaction.household_id == document.household_id,
            Transaction.posted_date >= start,
            Transaction.posted_date <= end,
            Transaction.transfer_group_id.is_(None),
            or_(
                and_(Transaction.amount <= -low, Transaction.amount >= -high),
                Transaction.amount == total,
            ),
        )
        .order_by(Transaction.posted_date, Transaction.id)
        .limit(MAX_CANDIDATES)
    )
    if allowed is not None:
        query = query.where(Transaction.account_id.in_(sorted(allowed)))
    if document.account_id is not None:
        query = query.where(Transaction.account_id == document.account_id)
    if only_source_document is not None:
        query = query.where(Transaction.source_document_id == only_source_document)
    if receipt.currency is not None:
        query = query.where(Transaction.currency == receipt.currency)
    rows = db.execute(query).all()
    if not rows:
        return []
    linked = _linked_elsewhere(db, document, [t.id for t, _, _ in rows])
    names = _names(receipt)
    out: list[Candidate] = []
    for transaction, payee_name, aliases in rows:
        amount = _amount_score(transaction.amount, total)
        if amount is None or (undated and amount[0] != "exact"):
            continue
        kind, amount_score = amount
        days = None if undated else abs((transaction.posted_date - receipt.purchase_date).days)  # type: ignore[operator]
        date_score = 0.3 if days is None else _DATE_SCORES.get(days, 0.0)
        if receipt.payee_id is not None and transaction.payee_id == receipt.payee_id:
            merchant = 1.0
        else:
            others = [transaction.raw_payee or "", payee_name or "", *(aliases or [])]
            merchant = _best_similarity(names, [o for o in others if o])
        score = round(0.5 * amount_score + 0.2 * date_score + 0.3 * merchant, 4)
        out.append(
            Candidate(
                transaction_id=transaction.id,
                account_id=transaction.account_id,
                score=score,
                amount=kind,
                days=days,
                merchant=merchant,
                already_linked=transaction.id in linked,
            )
        )
    out.sort(key=lambda c: (-c.score, c.days if c.days is not None else 99, str(c.transaction_id)))
    return out


def _linked_elsewhere(
    db: Session, document: Document, transaction_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """Transactions already carrying a receipt from an unrelated document."""
    if not transaction_ids:
        return set()
    related = _related_documents(db, document)
    rows = db.execute(
        select(DocumentLink.target_id)
        .join(
            Document,
            (Document.household_id == DocumentLink.household_id)
            & (Document.id == DocumentLink.document_id),
        )
        .where(
            DocumentLink.household_id == document.household_id,
            DocumentLink.target_type == LinkTarget.TRANSACTION,
            DocumentLink.target_id.in_(transaction_ids),
            DocumentLink.document_id.not_in(sorted(related)),
            Document.kind == DocumentKind.RECEIPT,
        )
    ).scalars()
    return set(rows)


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchDecision:
    """``linked`` (with the transaction) or ``review`` (with the reason and suggestions)."""

    status: Literal["linked", "review", "already_linked"]
    transaction_id: uuid.UUID | None = None
    reason: str | None = None
    suggestions: tuple[Candidate, ...] = ()
    score: float | None = None


def decide(receipt: Receipt, candidates: list[Candidate], *, threshold: float) -> MatchDecision:
    """Link the best candidate when the match is confident and unique (module doc)."""
    suggestions = tuple(c for c in candidates if c.score >= SUGGEST_MIN)[:MAX_SUGGESTIONS]
    if receipt.total is None:
        return MatchDecision("review", reason="no_total")
    if not candidates:
        return MatchDecision("review", reason="no_candidates")
    best = candidates[0]
    second = candidates[1].score if len(candidates) > 1 else 0.0
    if receipt.confidence.get("total", 0.0) < threshold or receipt.purchase_date is None:
        return MatchDecision("review", reason="receipt_uncertain", suggestions=suggestions)
    if best.amount != "exact" or best.score < AUTO_LINK:
        return MatchDecision("review", reason="not_confident", suggestions=suggestions)
    if best.score - second < MARGIN:
        return MatchDecision("review", reason="ambiguous", suggestions=suggestions)
    if best.already_linked:
        return MatchDecision("review", reason="already_has_receipt", suggestions=suggestions)
    return MatchDecision("linked", transaction_id=best.transaction_id, score=best.score)


def _link(db: Session, document: Document, candidate: Candidate) -> DocumentLink:
    existing = db.execute(
        select(DocumentLink).where(
            DocumentLink.household_id == document.household_id,
            DocumentLink.document_id == document.id,
            DocumentLink.target_type == LinkTarget.TRANSACTION,
            DocumentLink.target_id == candidate.transaction_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = DocumentLink(
        household_id=document.household_id,
        document_id=document.id,
        target_type=LinkTarget.TRANSACTION,
        target_id=candidate.transaction_id,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        action="document.link",
        target_type="document",
        target_id=document.id,
        after={
            "target_type": str(LinkTarget.TRANSACTION),
            "target_id": str(candidate.transaction_id),
            "auto": True,
            "matcher": MATCHER_VERSION,
            "score": candidate.score,
        },
        household_id=document.household_id,
    )
    emit(
        db,
        document.household_id,
        "document.linked",
        {"id": str(document.id), "link_id": str(row.id), "account_id": str(candidate.account_id)},
    )
    return row


def _transaction_links(db: Session, document: Document) -> list[uuid.UUID]:
    return list(
        db.execute(
            select(DocumentLink.target_id).where(
                DocumentLink.household_id == document.household_id,
                DocumentLink.document_id == document.id,
                DocumentLink.target_type == LinkTarget.TRANSACTION,
            )
        ).scalars()
    )


def _open_receipt_items(db: Session, document: Document) -> list[ReviewItem]:
    return list(
        db.execute(
            select(ReviewItem).where(
                ReviewItem.household_id == document.household_id,
                ReviewItem.document_id == document.id,
                ReviewItem.kind == ReviewKind.RECEIPT_MATCH,
                ReviewItem.state == ReviewItemState.OPEN,
            )
        ).scalars()
    )


@dataclass(frozen=True)
class ReceiptOutcome:
    status: DocumentStatus
    summary: dict[str, Any]
    receipt: Receipt


def apply_receipt(
    db: Session, document: Document, receipt: Receipt, *, threshold: float
) -> ReceiptOutcome:
    """Record a parsed receipt on its document and link or queue it (module doc).

    The caller stores the returned ``receipt`` (merchant resolved) in the
    document's extraction.
    """
    receipt = resolve_merchant(db, document.household_id, receipt)
    document.kind = DocumentKind.RECEIPT
    if receipt.purchase_date is not None and receipt.confidence.get("date", 0.0) >= threshold:
        document.period_start = document.period_end = receipt.purchase_date
    summary: dict[str, Any] = {"receipt": True, "low_confidence": len(receipt.low_confidence)}
    if _transaction_links(db, document):
        # Re-processing a receipt that is already attached changes nothing.
        decision = MatchDecision("already_linked")
    else:
        candidates = find_candidates(db, document, receipt)
        decision = decide(receipt, candidates, threshold=threshold)
        summary["candidates"] = len(candidates)
    summary["match"] = decision.status
    if decision.status == "linked":
        best = next(c for c in candidates if c.transaction_id == decision.transaction_id)
        _link(db, document, best)
        summary["score"] = decision.score
    elif decision.status == "review" and not _open_receipt_items(db, document):
        payload: dict[str, Any] = {
            "reason": decision.reason,
            "suggestions": [c.payload() for c in decision.suggestions],
        }
        if document.account_id is not None:
            payload["account_id"] = str(document.account_id)
        documents_repo.add_review_item(
            db, document.household_id, ReviewKind.RECEIPT_MATCH, payload, document_id=document.id
        )
        summary["reason"] = decision.reason
    document.confidence = {
        **(document.confidence or {}),
        "receipt": {
            "fields": dict(receipt.confidence),
            "low_confidence": list(receipt.low_confidence),
            "adds_up": receipt.adds_up,
            "match": decision.status,
            "score": decision.score,
            "matcher": MATCHER_VERSION,
        },
    }
    open_items = open_items_of(db, document.household_id, document.id)
    status = DocumentStatus.NEEDS_REVIEW if open_items else DocumentStatus.PROCESSED
    return ReceiptOutcome(status, summary, receipt)


# ---------------------------------------------------------------------------
# Later transactions
# ---------------------------------------------------------------------------


def stored_receipt(document: Document) -> Receipt | None:
    """The parsed receipt kept in the document's extraction, if any."""
    if not isinstance(document.extraction, dict):
        return None
    try:
        result = ExtractResult.model_validate(document.extraction)
    except ValueError:
        return None
    return result.parse.receipt if result.parse is not None else None


def _close_item(db: Session, item: ReviewItem, resolution: dict[str, Any]) -> None:
    item.state = ReviewItemState.RESOLVED
    item.resolution = resolution
    item.resolved_at = clock.utcnow()
    db.flush()
    audit(
        db,
        action="review.resolve",
        target_type="review_item",
        target_id=item.id,
        after={"kind": str(item.kind), "document_id": item.document_id, "resolution": resolution},
        household_id=item.household_id,
    )
    event: dict[str, Any] = {"id": str(item.id), "kind": str(item.kind)}
    if item.document_id is not None:
        event["document_id"] = str(item.document_id)
    emit(db, item.household_id, "review.resolved", event)


def _settle(db: Session, document: Document) -> None:
    if document.status is not DocumentStatus.NEEDS_REVIEW or open_items_of(
        db, document.household_id, document.id
    ):
        return
    blob = documents_repo.get_blob(db, document.household_id, document.blob_id)
    if blob is None or blob.area is not BlobArea.PERMANENT:
        return
    document.status = DocumentStatus.PROCESSED
    db.flush()
    emit(
        db,
        document.household_id,
        "document.updated",
        {"id": str(document.id), "status": str(document.status)},
    )


def rematch_receipts(
    db: Session, household_id: uuid.UUID, source_document_id: uuid.UUID, *, threshold: float
) -> int:
    """Link waiting receipts to the transactions ``source_document_id`` brought (module doc).

    Returns the number of receipts linked.
    """
    has_new = db.execute(
        select(Transaction.id)
        .where(
            Transaction.household_id == household_id,
            Transaction.source_document_id == source_document_id,
        )
        .limit(1)
    ).first()
    if has_new is None:
        return 0
    items = list(
        db.execute(
            select(ReviewItem)
            .where(
                ReviewItem.household_id == household_id,
                ReviewItem.kind == ReviewKind.RECEIPT_MATCH,
                ReviewItem.state == ReviewItemState.OPEN,
                ReviewItem.document_id.is_not(None),
            )
            .order_by(ReviewItem.created_at, ReviewItem.id)
            .limit(MAX_REMATCH)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    linked = 0
    for item in items:
        if item.document_id is None:  # pragma: no cover - the query asks for one
            continue
        document = documents_repo.get_document(db, household_id, item.document_id, lock=True)
        if document is None or document.id == source_document_id:
            continue
        receipt = stored_receipt(document)
        if receipt is None or _transaction_links(db, document):
            continue
        if not find_candidates(db, document, receipt, only_source_document=source_document_id):
            continue
        candidates = find_candidates(db, document, receipt)
        decision = decide(receipt, candidates, threshold=threshold)
        if decision.status != "linked":
            continue
        best = next(c for c in candidates if c.transaction_id == decision.transaction_id)
        _link(db, document, best)
        _close_item(
            db,
            item,
            {
                "action": "auto_link",
                "transaction_id": str(best.transaction_id),
                "source_document_id": str(source_document_id),
            },
        )
        receipt_facts = dict((document.confidence or {}).get("receipt") or {})
        receipt_facts.update({"match": "linked", "score": best.score})
        document.confidence = {**(document.confidence or {}), "receipt": receipt_facts}
        _settle(db, document)
        linked += 1
    return linked


# ---------------------------------------------------------------------------
# Review resolution
# ---------------------------------------------------------------------------


def resolve_receipt_item(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    *,
    transaction_id: uuid.UUID | None,
    request: HTTPConnection | None,
) -> dict[str, Any]:
    """Attach the receipt to the chosen transaction (or to none); the resolution facts.

    Linking goes through :func:`app.domain.documents.manage.link`: the caller
    must be able to change the document and edit the transaction (a hidden
    one is ``404 transaction-not-found``).
    """
    from app.domain.documents.manage import link
    from app.domain.documents.schemas import DocumentLinkIn

    if transaction_id is None or item.document_id is None:
        return {"action": "no_transaction"}
    out, created = link(
        db,
        principal,
        item.document_id,
        DocumentLinkIn(target_type=LinkTarget.TRANSACTION, target_id=transaction_id),
        request=request,
    )
    suggested = {
        s.get("transaction_id")
        for s in (item.payload or {}).get("suggestions") or []
        if isinstance(s, dict)
    }
    return {
        "action": "link",
        "transaction_id": str(transaction_id),
        "link_id": str(out.id),
        "created": created,
        "suggested": str(transaction_id) in suggested,
    }

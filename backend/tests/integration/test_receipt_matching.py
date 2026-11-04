# SPDX-License-Identifier: AGPL-3.0-or-later
"""Receipt -> transaction matching (INGESTION.md "Match and deduplicate"; F3, I1).

The matcher (``app.ingestion.match.receipts``) runs on parsed receipts
against real ledger rows: a confident unique match is linked, anything else
becomes a ``receipt_match`` review item with ranked suggestions limited to
what the uploader may edit, and resolving the item links the chosen
transaction through the same checks as ``POST /documents/{id}/links``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.db.models import AuditLog
from app.db.rls import set_context
from app.domain.accounts.models import Account
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Document,
    DocumentKind,
    DocumentLink,
    DocumentStatus,
    LinkTarget,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.transactions.models import Transaction
from app.ingestion.match import receipts as matcher
from app.ingestion.sandbox.protocol import ExtractResult, ParseInfo, Receipt, ReceiptPayment
from tests.fixtures.documents import make_document
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, problem_type
from tests.fixtures.ledger import LedgerFactories, make_account

pytestmark = pytest.mark.integration

DAY = date(2026, 3, 14)
THRESHOLD = 0.85


def receipt(
    total: str = "11.43",
    *,
    when: date | None = DAY,
    merchant: str | None = "CORNER MARKET",
    currency: str | None = "USD",
    total_conf: float = 1.0,
    candidates: tuple[str, ...] = (),
) -> Receipt:
    return Receipt(
        merchant=merchant,
        merchant_candidates=[c for c in (merchant, *candidates) if c],
        purchase_date=when,
        currency=currency,
        total=Decimal(total),
        payment=ReceiptPayment(method="visa", last4="4321"),
        confidence={"merchant": 0.9, "date": 1.0, "total": total_conf},
        low_confidence=[] if total_conf >= THRESHOLD else ["total"],
    )


@pytest.fixture
def doc_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={"documents_dir": tmp_path / "documents", "quarantine_dir": tmp_path / "quarantine"}
    )


@pytest.fixture
def store(doc_settings: Settings) -> LocalFSBlobStore:
    return LocalFSBlobStore.from_settings(doc_settings)


@pytest.fixture
def actors(doc_settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(doc_settings, factories) as opened:
        yield opened


class World:
    def __init__(
        self, factories: Factories, ledger: LedgerFactories, store: LocalFSBlobStore
    ) -> None:
        self.factories = factories
        self.ledger = ledger
        self.store = store

    def document(
        self, household_id: uuid.UUID, *, rcpt: Receipt | None = None, **kwargs: Any
    ) -> Document:
        extraction = None
        if rcpt is not None:
            extraction = ExtractResult(
                kind="png", ok=True, parse=ParseInfo(threshold=THRESHOLD, receipt=rcpt)
            ).model_dump(mode="json")
        kwargs.setdefault("status", DocumentStatus.PARSING)
        with self.factories.transaction() as db:
            return make_document(
                db,
                self.store,
                self.factories.keyring,
                household_id,
                data=b"\x89PNG\r\n\x1a\nnot really",
                mime="image/png",
                filename="receipt.png",
                kind=DocumentKind.UNKNOWN,
                extraction=extraction,
                **kwargs,
            )

    def apply(self, document: Document, rcpt: Receipt) -> matcher.ReceiptOutcome:
        with self.factories.transaction() as db:
            set_context(db, document.household_id)
            row = db.get(Document, document.id)
            assert row is not None
            outcome = matcher.apply_receipt(db, row, rcpt, threshold=THRESHOLD)
            row.status = outcome.status
            return outcome

    def rows(self, household_id: uuid.UUID, statement: Any) -> list[Any]:
        with self.factories.transaction() as db:
            set_context(db, household_id)
            found = list(db.execute(statement).scalars())
            db.expunge_all()
            return found

    def links(self, document: Document) -> list[uuid.UUID]:
        return [
            link.target_id
            for link in self.rows(
                document.household_id,
                select(DocumentLink).where(DocumentLink.document_id == document.id),
            )
        ]

    def items(self, document: Document) -> list[ReviewItem]:
        return self.rows(
            document.household_id,
            select(ReviewItem).where(
                ReviewItem.document_id == document.id, ReviewItem.kind == ReviewKind.RECEIPT_MATCH
            ),
        )

    def txn(self, account: Account, amount: str, posted: date = DAY, **kwargs: Any) -> Transaction:
        return self.ledger.transaction(account, amount=amount, posted_date=posted, **kwargs)


@pytest.fixture
def world(factories: Factories, ledger: LedgerFactories, store: LocalFSBlobStore) -> World:
    return World(factories, ledger, store)


@pytest.fixture
def owner_hh(factories: Factories) -> tuple[uuid.UUID, uuid.UUID]:
    owner = factories.user(role="owner")
    assert owner.household_id is not None
    return owner.household_id, owner.user.id


def test_merchant_similarity() -> None:
    assert matcher.merchant_similarity("Corner Market", "CORNER MARKET") == 1.0
    assert matcher.merchant_similarity("SQ *CORNER MARKET 0412", "Corner Market") == 0.9
    assert 0.7 < matcher.merchant_similarity("CORNER MKT", "Corner Market") < 0.9
    assert matcher.merchant_similarity("Lantern Books", "Riverside Fuel") < 0.4
    assert matcher.merchant_similarity(None, "x") == 0.0


def test_confident_unique_match_is_linked(
    world: World, owner_hh: tuple[uuid.UUID, uuid.UUID]
) -> None:
    hh, user_id = owner_hh
    account = world.ledger.account(hh)
    paid = world.txn(account, "-11.43", date(2026, 3, 15), raw_payee="SQ *CORNER MARKET 0412")
    world.txn(account, "-11.43", date(2026, 3, 30), raw_payee="CORNER MARKET")  # out of window
    world.txn(account, "-99.00", DAY, raw_payee="CORNER MARKET")  # other amount
    doc = world.document(hh, uploaded_by=user_id)
    outcome = world.apply(doc, receipt())
    assert outcome.status is DocumentStatus.PROCESSED
    assert outcome.summary["match"] == "linked"
    assert world.links(doc) == [paid.id]
    assert world.items(doc) == []
    audit = world.rows(
        hh,
        select(AuditLog).where(
            AuditLog.action == "document.link", AuditLog.target_id == str(doc.id)
        ),
    )
    assert len(audit) == 1
    assert audit[0].after["auto"] is True
    assert audit[0].user_id is None
    # Card digits never reach the audit log.
    assert "4321" not in str(audit[0].after)
    stored = world.rows(hh, select(Document).where(Document.id == doc.id))[0]
    assert stored.kind is DocumentKind.RECEIPT
    assert (stored.period_start, stored.period_end) == (DAY, DAY)
    assert stored.confidence["receipt"]["match"] == "linked"


def test_payee_alias_resolves_the_merchant(
    world: World, owner_hh: tuple[uuid.UUID, uuid.UUID]
) -> None:
    hh, user_id = owner_hh
    account = world.ledger.account(hh)
    payee = world.ledger.payee(hh, name="Corner Market", aliases=["CNR MKT SPRINGFIELD"])
    paid = world.txn(account, "-11.43", DAY, raw_payee="POS 0412 CNRMKT", payee=payee)
    doc = world.document(hh, uploaded_by=user_id)
    rcpt = receipt(merchant="CNR MKT SPRINGFIELD")
    outcome = world.apply(doc, rcpt)
    assert outcome.receipt.payee_id == payee.id
    assert outcome.receipt.merchant == "Corner Market"
    assert outcome.receipt.confidence["merchant"] == 1.0
    assert world.links(doc) == [paid.id]


def test_two_equally_good_transactions_go_to_review_ranked(
    world: World, owner_hh: tuple[uuid.UUID, uuid.UUID]
) -> None:
    hh, user_id = owner_hh
    account = world.ledger.account(hh)
    first = world.txn(account, "-11.43", DAY, raw_payee="CORNER MARKET")
    second = world.txn(account, "-11.43", DAY, raw_payee="CORNER MARKET #2")
    tipped = world.txn(account, "-13.43", DAY, raw_payee="CORNER MARKET")
    doc = world.document(hh, uploaded_by=user_id)
    outcome = world.apply(doc, receipt())
    assert outcome.status is DocumentStatus.NEEDS_REVIEW
    assert world.links(doc) == []
    (item,) = world.items(doc)
    assert item.payload["reason"] == "ambiguous"
    ranked = [s["transaction_id"] for s in item.payload["suggestions"]]
    assert set(ranked[:2]) == {str(first.id), str(second.id)}
    assert ranked[2] == str(tipped.id)
    assert item.payload["suggestions"][2]["amount"] == "tip"
    scores = [s["score"] for s in item.payload["suggestions"]]
    assert scores == sorted(scores, reverse=True)
    # Ids and short facts only: no receipt text, no card digits.
    assert "4321" not in str(item.payload)
    assert "CORNER" not in str(item.payload)
    # Re-running the matcher does not add a second item.
    world.apply(doc, receipt())
    assert len(world.items(doc)) == 1


@pytest.mark.parametrize(
    ("rcpt", "reason"),
    [
        (receipt(total_conf=0.6), "receipt_uncertain"),
        (receipt(when=None), "receipt_uncertain"),
        (receipt(merchant=None), "not_confident"),
    ],
)
def test_doubtful_receipts_are_suggested_not_linked(
    world: World, owner_hh: tuple[uuid.UUID, uuid.UUID], rcpt: Receipt, reason: str
) -> None:
    hh, user_id = owner_hh
    account = world.ledger.account(hh)
    # Posted two days later (cards settle late): suggested, but not certain
    # enough without a readable merchant.
    paid = world.txn(account, "-11.43", date(2026, 3, 16), raw_payee="CORNER MARKET")
    doc = world.document(hh, uploaded_by=user_id)
    if rcpt.purchase_date is None:
        # An undated receipt looks at what was posted before it arrived.
        with world.factories.transaction() as db:
            set_context(db, hh)
            row = db.get(Document, doc.id)
            assert row is not None
            row.created_at = row.created_at.replace(year=2026, month=3, day=20)
        doc = world.rows(hh, select(Document).where(Document.id == doc.id))[0]
    world.apply(doc, rcpt)
    assert world.links(doc) == []
    (item,) = world.items(doc)
    assert item.payload["reason"] == reason
    assert item.payload["suggestions"][0]["transaction_id"] == str(paid.id)


def test_no_candidate_is_a_review_item_without_suggestions(
    world: World, owner_hh: tuple[uuid.UUID, uuid.UUID]
) -> None:
    hh, user_id = owner_hh
    account = world.ledger.account(hh, currency="EUR")
    world.txn(account, "-11.43", DAY, raw_payee="CORNER MARKET")  # another currency
    doc = world.document(hh, uploaded_by=user_id)
    outcome = world.apply(doc, receipt())
    assert outcome.status is DocumentStatus.NEEDS_REVIEW
    (item,) = world.items(doc)
    assert item.payload == {"reason": "no_candidates", "suggestions": []}


def test_a_transaction_with_another_receipt_is_not_taken(
    world: World, owner_hh: tuple[uuid.UUID, uuid.UUID]
) -> None:
    hh, user_id = owner_hh
    account = world.ledger.account(hh)
    paid = world.txn(account, "-11.43", DAY, raw_payee="CORNER MARKET")
    first = world.document(hh, uploaded_by=user_id)
    world.apply(first, receipt())
    assert world.links(first) == [paid.id]
    with world.factories.transaction() as db:
        set_context(db, hh)
        row = db.get(Document, first.id)
        assert row is not None
        row.kind = DocumentKind.RECEIPT
    second = world.document(hh, uploaded_by=user_id)
    world.apply(second, receipt())
    assert world.links(second) == []
    (item,) = world.items(second)
    assert item.payload["reason"] == "already_has_receipt"
    assert item.payload["suggestions"][0]["already_linked"] is True
    # The PDF attached to the same mail may share the transaction.
    child = world.document(hh, uploaded_by=user_id)
    with world.factories.transaction() as db:
        set_context(db, hh)
        row = db.get(Document, child.id)
        assert row is not None
        row.source_meta = {"parent_document_id": str(first.id), "depth": 1}
    child = world.rows(hh, select(Document).where(Document.id == child.id))[0]
    world.apply(child, receipt())
    assert world.links(child) == [paid.id]


def test_member_candidates_are_limited_to_accounts_they_edit(
    world: World, factories: Factories
) -> None:
    owner = factories.user(role="owner")
    hh = owner.household_id
    assert hh is not None
    member = factories.user(role="member", household=owner.household)
    editable = world.ledger.account(hh)
    viewable = world.ledger.account(hh)
    hidden = world.ledger.account(hh)
    world.ledger.grant(hh, member.user.id, editable, GrantLevel.EDIT)
    world.ledger.grant(hh, member.user.id, viewable, GrantLevel.VIEW)
    mine = world.txn(editable, "-11.43", DAY, raw_payee="POS 99")
    world.txn(viewable, "-11.43", DAY, raw_payee="CORNER MARKET")
    world.txn(hidden, "-11.43", DAY, raw_payee="CORNER MARKET")
    doc = world.document(hh, uploaded_by=member.user.id)
    world.apply(doc, receipt())
    # Only the editable account's row is a candidate (and, without a
    # merchant match, only a suggestion).
    (item,) = world.items(doc)
    assert [s["transaction_id"] for s in item.payload["suggestions"]] == [str(mine.id)]
    # A viewer's receipt has no candidates at all.
    viewer = factories.user(role="viewer", household=owner.household)
    doc2 = world.document(hh, uploaded_by=viewer.user.id)
    world.apply(doc2, receipt())
    assert world.items(doc2)[0].payload["reason"] == "no_candidates"


def test_a_receipt_filed_under_an_account_stays_in_it(
    world: World, owner_hh: tuple[uuid.UUID, uuid.UUID]
) -> None:
    hh, user_id = owner_hh
    card = world.ledger.account(hh)
    other = world.ledger.account(hh)
    world.txn(other, "-11.43", DAY, raw_payee="CORNER MARKET")
    doc = world.document(hh, uploaded_by=user_id, account_id=card.id)
    world.apply(doc, receipt())
    (item,) = world.items(doc)
    assert item.payload["reason"] == "no_candidates"
    assert item.payload["account_id"] == str(card.id)


def test_later_transactions_link_waiting_receipts(
    world: World, owner_hh: tuple[uuid.UUID, uuid.UUID]
) -> None:
    hh, user_id = owner_hh
    account = world.ledger.account(hh)
    rcpt = receipt()
    doc = world.document(hh, uploaded_by=user_id, rcpt=rcpt, status=DocumentStatus.PARSING)
    world.apply(doc, rcpt)
    with world.factories.transaction() as db:
        set_context(db, hh)
        row = db.get(Document, doc.id)
        assert row is not None
        row.status = DocumentStatus.NEEDS_REVIEW
    statement = world.document(hh, uploaded_by=user_id)
    paid = world.txn(
        account, "-11.43", DAY, raw_payee="CORNER MARKET", source_document_id=statement.id
    )
    with world.factories.transaction() as db:
        set_context(db, hh)
        assert matcher.rematch_receipts(db, hh, statement.id, threshold=THRESHOLD) == 1
        # Nothing new for a document without transactions.
        assert matcher.rematch_receipts(db, hh, doc.id, threshold=THRESHOLD) == 0
    assert world.links(doc) == [paid.id]
    (item,) = world.items(doc)
    assert item.state is ReviewItemState.RESOLVED
    assert item.resolution == {
        "action": "auto_link",
        "transaction_id": str(paid.id),
        "source_document_id": str(statement.id),
    }
    stored = world.rows(hh, select(Document).where(Document.id == doc.id))[0]
    assert stored.status is DocumentStatus.PROCESSED


# -- the review API ---------------------------------------------------------------


def _resolve(client: AuthedClient, item_id: uuid.UUID, transaction_id: uuid.UUID | None) -> Any:
    return client.post(
        f"{API}/review/{item_id}/resolve",
        json={
            "kind": "receipt_match",
            "transaction_id": str(transaction_id) if transaction_id else None,
        },
    )


def test_resolving_attaches_the_chosen_transaction(world: World, actors: Actors) -> None:
    owner = actors.new(role="owner")
    hh = owner.record.household_id
    assert hh is not None
    member = actors.new(role="member", household=owner.record.household)
    with world.factories.transaction() as db:
        mine = make_account(db, hh)
        hidden = make_account(db, hh)
    world.factories.grant(hh, member.user.id, mine.id, GrantLevel.EDIT)
    a = world.txn(mine, "-11.43", DAY, raw_payee="CORNER MARKET")
    world.txn(mine, "-11.43", DAY, raw_payee="CORNER MARKET")
    secret = world.txn(hidden, "-11.43", DAY, raw_payee="CORNER MARKET")
    doc = world.document(hh, uploaded_by=member.user.id, status=DocumentStatus.PARSING)
    world.apply(doc, receipt())
    with world.factories.transaction() as db:
        set_context(db, hh)
        row = db.get(Document, doc.id)
        assert row is not None
        row.status = DocumentStatus.NEEDS_REVIEW
    (item,) = world.items(doc)
    listed = member.get(f"{API}/review/{item.id}").json()
    assert listed["kind"] == "receipt_match"
    assert listed["next_step"] == "attach_receipt"
    assert listed["can_resolve"] is True
    assert str(secret.id) not in str(listed["payload"])
    # A transaction the member cannot see is "not found", like a foreign one.
    hidden_try = _resolve(member, item.id, secret.id)
    assert hidden_try.status_code == 404, hidden_try.text
    assert problem_type(hidden_try) == "transaction-not-found"
    wrong_kind = member.post(
        f"{API}/review/{item.id}/resolve", json={"kind": "unparsed", "action": "reprocess"}
    )
    assert wrong_kind.status_code == 422
    done = _resolve(member, item.id, a.id)
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["state"] == "resolved"
    assert body["resolution"]["action"] == "link"
    assert body["resolution"]["suggested"] is True
    assert world.links(doc) == [a.id]
    stored = world.rows(hh, select(Document).where(Document.id == doc.id))[0]
    assert stored.status is DocumentStatus.PROCESSED
    links = owner.get(f"{API}/documents/{doc.id}").json()["links"]
    assert [(link["target_type"], link["target_id"]) for link in links] == [
        (LinkTarget.TRANSACTION.value, str(a.id))
    ]


def test_resolving_with_no_transaction(world: World, actors: Actors) -> None:
    owner = actors.new(role="owner")
    hh = owner.record.household_id
    assert hh is not None
    viewer = actors.new(role="viewer", household=owner.record.household)
    doc = world.document(hh, uploaded_by=owner.user.id, status=DocumentStatus.PARSING)
    world.apply(doc, receipt())
    (item,) = world.items(doc)
    assert viewer.get(f"{API}/review/{item.id}").status_code == 404  # not their document
    done = _resolve(owner, item.id, None)
    assert done.status_code == 200, done.text
    assert done.json()["resolution"] == {"action": "no_transaction"}
    assert world.links(doc) == []

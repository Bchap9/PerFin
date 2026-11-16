# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /inbox/summary``: one count of what waits on a person.

Design note (stage 3 UI redesign), sections 4.4 and 11: the rail badge,
Home and the Inbox read one aggregate instead of four requests. It counts
documents with an open review item, open items of no document, subscription
series that are suggested or have an open change (each once), and the
categorisation summary's suggestions and uncategorised rows, all over what
the caller may see. ``cards`` is what the Inbox shows as cards: documents,
loose items, series and one card for suggestions; a "make it a rule" offer
is not a question and is not counted.

Documents, items, series and changes are written by hand, as the importer
and detection would leave them. Every name and amount is invented.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.auth.models import GrantLevel
from app.core import clock
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType
from app.domain.categorisation import memory
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Document,
    DocumentStatus,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.recurring import service as recurring_service
from app.domain.recurring.models import SubscriptionEvent
from app.domain.transactions.models import Cadence, ScheduledTransaction, ScheduleOrigin
from tests.fixtures.documents import make_document, make_review_item
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors
from tests.fixtures.ledger import LedgerFactories, household_context

pytestmark = pytest.mark.integration

SUMMARY = f"{API}/inbox/summary"
TODAY = date(2026, 6, 30)
T0 = datetime(2026, 4, 6, 9, 15, tzinfo=UTC)
ZERO = {
    "documents": 0,
    "loose": 0,
    "subscriptions": 0,
    "suggestions": 0,
    "uncategorised": 0,
    "cards": 0,
}


@pytest.fixture
def doc_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={"documents_dir": tmp_path / "documents", "quarantine_dir": tmp_path / "quarantine"}
    )


@pytest.fixture
def actors(doc_settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(doc_settings, factories) as opened:
        yield opened


@pytest.fixture
def store(doc_settings: Settings) -> LocalFSBlobStore:
    return LocalFSBlobStore.from_settings(doc_settings)


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recurring_service, "today", lambda: TODAY)


@pytest.fixture
def owner(actors: Actors) -> AuthedClient:
    return actors.new(role="owner")


def hh_of(user: AuthedClient) -> uuid.UUID:
    household_id = user.record.household_id
    assert household_id is not None
    return household_id


def summary(user: AuthedClient) -> dict[str, Any]:
    response = user.get(SUMMARY)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def account(ledger: LedgerFactories, household_id: uuid.UUID, name: str) -> Account:
    return ledger.account(household_id, type=AccountType.CHECKING, name=name)


def document(
    factories: Factories, store: LocalFSBlobStore, household_id: uuid.UUID, **kwargs: Any
) -> Document:
    kwargs.setdefault("status", DocumentStatus.NEEDS_REVIEW)
    with factories.transaction() as db:
        return make_document(db, store, factories.keyring, household_id, **kwargs)


def item(factories: Factories, doc: Document, *, closed: bool = False) -> ReviewItem:
    with factories.transaction() as db:
        row = make_review_item(db, doc, ReviewKind.UNPARSED, {"reason": "import_pending"})
    if closed:
        with factories.transaction() as db:
            set_context(db, doc.household_id)
            found = db.get_one(ReviewItem, row.id)
            found.state = ReviewItemState.RESOLVED
            found.resolved_at = clock.utcnow()
    return row


def loose_item(factories: Factories, household_id: uuid.UUID) -> None:
    with factories.transaction() as db:
        set_context(db, household_id)
        db.add(
            ReviewItem(
                household_id=household_id,
                kind=ReviewKind.REJECTED_EMAIL,
                payload={"reason": "daily_digest"},
            )
        )


def series(factories: Factories, on: Account, name: str, **columns: Any) -> uuid.UUID:
    """A series written as detection would leave it."""
    columns.setdefault("kind", "subscription")
    columns.setdefault("status", "active")
    with factories.transaction() as db:
        set_context(db, on.household_id)
        row = ScheduledTransaction(
            household_id=on.household_id,
            account_id=on.id,
            name=name,
            cadence=Cadence.MONTHLY,
            next_due=date(2026, 7, 5),
            amount_min=Decimal("-10.00"),
            amount_max=Decimal("-10.00"),
            currency=on.currency,
            origin=ScheduleOrigin.DETECTED,
            **columns,
        )
        db.add(row)
        db.flush()
        return row.id


def event(
    factories: Factories, on: Account, schedule_id: uuid.UUID, kind: str, **columns: Any
) -> None:
    with factories.transaction() as db:
        set_context(db, on.household_id)
        db.add(
            SubscriptionEvent(
                household_id=on.household_id,
                schedule_id=schedule_id,
                account_id=on.id,
                kind=kind,
                occurred_on=date(2026, 6, 20),
                **columns,
            )
        )


def _record(
    db: Session, household_id: uuid.UUID, key: str, category_id: uuid.UUID, times: int
) -> None:
    with household_context(db, household_id):
        for step in range(times):
            memory.record(
                db, household_id, key, "out", category_id, at=T0 + timedelta(minutes=step)
            )


def test_empty_household_is_all_zero(owner: AuthedClient) -> None:
    assert summary(owner) == ZERO


def test_counts_documents_and_loose_items(
    owner: AuthedClient, factories: Factories, store: LocalFSBlobStore, ledger: LedgerFactories
) -> None:
    hh = hh_of(owner)
    checking = account(ledger, hh, "Everyday Checking")
    two = document(factories, store, hh, account_id=checking.id)
    one = document(factories, store, hh, account_id=checking.id)
    settled = document(factories, store, hh, account_id=checking.id)
    item(factories, two)
    item(factories, two)
    item(factories, one)
    # Only a resolved item: nothing waits on this document.
    item(factories, settled, closed=True)
    loose_item(factories, hh)

    body = summary(owner)
    assert (body["documents"], body["loose"]) == (2, 1)
    assert body["cards"] == 3


def test_counts_a_series_once(
    owner: AuthedClient, factories: Factories, ledger: LedgerFactories
) -> None:
    hh = hh_of(owner)
    checking = account(ledger, hh, "Everyday Checking")
    maybe = series(factories, checking, "Pinecone Radio", status="suggested")
    event(factories, checking, maybe, "new")
    event(factories, checking, maybe, "price_changed")
    assert summary(owner)["subscriptions"] == 1

    # A change already seen asks nothing; a confirmed series with an open
    # change asks once more.
    quiet = series(factories, checking, "Harbour Paper")
    event(factories, checking, quiet, "new", state="seen")
    changed = series(factories, checking, "Otterflix")
    event(factories, checking, changed, "price_changed")
    body = summary(owner)
    assert body["subscriptions"] == 2
    assert body["cards"] == 2


def test_suggestions_and_uncategorised_match_the_categorisation_summary(
    owner: AuthedClient, ledger: LedgerFactories
) -> None:
    hh = hh_of(owner)
    checking = account(ledger, hh, "Everyday Checking")
    chosen = ledger.category(hh)
    ledger.transaction(
        checking, category=chosen, category_source="person", category_confidence=Decimal(1)
    )
    for _ in range(2):
        ledger.transaction(
            checking,
            suggested_category_id=chosen.id,
            category_source="dictionary",
            category_confidence=Decimal("0.60"),
        )
    ledger.transaction(checking)

    expected = owner.get(f"{API}/categorisation/summary").json()
    body = summary(owner)
    assert (body["suggestions"], body["uncategorised"]) == (
        expected["suggestions"],
        expected["uncategorised"],
    )
    assert (body["suggestions"], body["uncategorised"]) == (2, 3)


def test_cards_adds_one_for_suggestions_and_nothing_for_rule_offers(
    owner: AuthedClient, ledger: LedgerFactories
) -> None:
    hh = hh_of(owner)
    checking = account(ledger, hh, "Everyday Checking")
    food = ledger.category(hh)
    # Three unanimous decisions: the learned list offers a rule.
    ledger.run(_record, hh, "mossgrove grocer", food.id, 3)
    ledger.transaction(checking, merchant_key="mossgrove grocer", raw_payee="MOSSGROVE GROCER 11")
    learned = owner.get(f"{API}/categorisation/learned").json()
    assert [entry["offer_rule"] for entry in learned["items"]] == [True]
    assert summary(owner)["cards"] == 0

    for _ in range(3):
        ledger.transaction(
            checking,
            suggested_category_id=food.id,
            category_source="dictionary",
            category_confidence=Decimal("0.60"),
        )
    body = summary(owner)
    assert body["suggestions"] == 3
    assert body["cards"] == 1


def test_a_viewer_sees_only_granted_accounts(
    actors: Actors,
    owner: AuthedClient,
    factories: Factories,
    store: LocalFSBlobStore,
    ledger: LedgerFactories,
) -> None:
    hh = hh_of(owner)
    viewer = actors.new(role="viewer", household=owner.record.household)
    granted = account(ledger, hh, "Shared Checking")
    hidden = account(ledger, hh, "Private Checking")
    ledger.grant(hh, viewer.record.id, granted, GrantLevel.VIEW)
    category = ledger.category(hh)

    for on in (granted, hidden):
        item(factories, document(factories, store, hh, account_id=on.id))
        ledger.transaction(
            on,
            suggested_category_id=category.id,
            category_source="dictionary",
            category_confidence=Decimal("0.60"),
        )
    series(factories, granted, "Pinecone Radio", status="suggested")
    series(factories, hidden, "Velvet Gym", status="suggested")
    changed = series(factories, hidden, "Otterflix")
    event(factories, hidden, changed, "price_changed")
    loose_item(factories, hh)

    assert summary(owner) == {
        "documents": 2,
        "loose": 1,
        "subscriptions": 3,
        "suggestions": 2,
        "uncategorised": 2,
        "cards": 7,
    }
    # Items of no document are for owners and admins.
    assert summary(viewer) == {
        "documents": 1,
        "loose": 0,
        "subscriptions": 1,
        "suggestions": 1,
        "uncategorised": 1,
        "cards": 3,
    }
    outsider = actors.new(role="owner")
    assert summary(outsider) == ZERO


def test_unauthenticated_is_401(actors: Actors) -> None:
    assert actors.anonymous().get(SUMMARY).status_code == 401

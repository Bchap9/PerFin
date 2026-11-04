# SPDX-License-Identifier: AGPL-3.0-or-later
"""Events about hidden accounts never reach members (SECURITY.md I2/F3, P2-SR-03).

The services' real ``emit`` calls (transfers, rules, goals, document
deletion) are run as an owner on a hidden account and on a granted one; the
stream's :func:`app.domain.events.service.deliverable` filter for a member
must keep only the latter. The filter also fails closed on account-derived
event types whose payload names no account.
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.models import GrantLevel
from app.auth.principal import Principal, apply_rls_context
from app.core.events import emit
from app.db.models import Event
from app.db.rls import set_context
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.service import delete_document
from app.domain.events.service import StreamView, deliverable
from app.domain.goals import service as goals
from app.domain.goals.schemas import GoalCreate
from app.domain.rules import service as rules
from app.domain.transactions import transfers
from app.domain.transactions.models import Transaction
from tests.fixtures.documents import make_document
from tests.fixtures.factories import Factories, UserRecord, grant_account, make_user
from tests.fixtures.ledger import make_account, make_rule, make_transaction

pytestmark = pytest.mark.integration

D = date(2026, 4, 2)


def _principal(db: Session, record: UserRecord) -> Principal:
    principal = Principal(
        user=record.user, household_id=record.household_id, role=record.role, auth_method="session"
    )
    apply_rls_context(db, principal)
    return principal


def _events_since(db: Session, household_id: uuid.UUID, marker: uuid.UUID) -> list[Event]:
    return list(
        db.execute(
            select(Event)
            .where(Event.household_id == household_id, Event.id > marker)
            .order_by(Event.id)
        ).scalars()
    )


def _marker(db: Session, household_id: uuid.UUID) -> uuid.UUID:
    set_context(db, household_id)
    return emit(db, household_id, "test.marker", {})


def test_service_events_about_hidden_accounts_are_not_delivered_to_members(
    db: Session, factories: Factories, tmp_path: Path
) -> None:
    owner = make_user(db, role="owner")
    hh = owner.household_id
    assert hh is not None
    member = make_user(db, role="member", household=owner.household)
    mine, mine2, hidden, hidden2 = (make_account(db, hh) for _ in range(4))
    for acct in (mine, mine2):
        grant_account(db, hh, member.id, acct.id, GrantLevel.EDIT)
    store = LocalFSBlobStore(tmp_path / "documents", tmp_path / "quarantine")
    hidden_doc = make_document(db, store, factories.keyring, hh, account_id=hidden.id)
    loose_doc = make_document(db, store, factories.keyring, hh, uploaded_by=owner.id)
    own_doc = make_document(db, store, factories.keyring, hh, uploaded_by=member.id)
    visible_doc = make_document(db, store, factories.keyring, hh, account_id=mine.id)
    h_out = make_transaction(db, hidden, amount="-10.00", posted_date=D)
    h_in = make_transaction(db, hidden2, amount="10.00", posted_date=D)
    v_out = make_transaction(db, mine, amount="-20.00", posted_date=D)
    v_in = make_transaction(db, mine2, amount="20.00", posted_date=D)
    make_transaction(db, hidden, amount="-30.00", posted_date=D)
    make_transaction(db, hidden2, amount="30.00", posted_date=D)
    make_rule(db, hh)
    rule_target = make_transaction(db, hidden, amount="-5.00", posted_date=D)
    visible_rule_target = make_transaction(db, mine, amount="-6.00", posted_date=D)

    as_owner = _principal(db, owner)
    hidden_marker = _marker(db, hh)
    apply_rls_context(db, as_owner)
    transfers.link(db, as_owner, h_out, h_in.id, request=None)
    transfers.unlink(db, as_owner, h_out, request=None)
    transfers.auto_pair(db, hh, principal=as_owner, account_ids=[hidden.id])
    rules.apply_rules_to(db, hh, [rule_target])
    goals.create_goal(
        db,
        as_owner,
        GoalCreate.model_validate(
            {
                "name": "Hidden and mine",
                "target": {"amount": "100", "currency": "USD"},
                "account_ids": [str(hidden.id), str(mine.id)],
            }
        ),
        request=None,
    )
    for doc in (hidden_doc, loose_doc):
        delete_document(db, store, household_id=hh, document_id=doc.id)
    hidden_events = _events_since(db, hh, hidden_marker)
    assert {e.type for e in hidden_events} == {
        "transaction.transfer_linked",
        "transaction.transfer_unlinked",
        "transaction.transfers_paired",
        "transaction.rules_applied",
        "goal.created",
        "document.deleted",
    }

    visible_marker = _marker(db, hh)
    apply_rls_context(db, as_owner)
    transfers.link(db, as_owner, v_out, v_in.id, request=None)
    rules.apply_rules_to(db, hh, [visible_rule_target])
    goals.create_goal(
        db,
        as_owner,
        GoalCreate.model_validate(
            {
                "name": "Mine only",
                "target": {"amount": "100", "currency": "USD"},
                "account_ids": [str(mine.id)],
            }
        ),
        request=None,
    )
    for doc in (own_doc, visible_doc):
        delete_document(db, store, household_id=hh, document_id=doc.id)
    visible_events = _events_since(db, hh, visible_marker)
    assert [e.type for e in visible_events] == [
        "transaction.transfer_linked",
        "transaction.rules_applied",
        "goal.created",
        "document.deleted",
        "document.deleted",
    ]

    as_member = _principal(db, member)
    view = StreamView(as_member, frozenset({mine.id, mine2.id}))
    assert deliverable(db, view, hidden_events) == []
    assert deliverable(db, view, visible_events) == visible_events
    everyone = StreamView(as_owner, None)
    assert deliverable(db, everyone, hidden_events) == hidden_events


def test_account_derived_events_without_accounts_fail_closed(db: Session) -> None:
    owner = make_user(db, role="owner")
    hh = owner.household_id
    assert hh is not None
    member = make_user(db, role="member", household=owner.household)
    mine = make_account(db, hh)
    marker = _marker(db, hh)
    for type_, payload in (
        ("transaction.transfer_linked", {"ids": [str(uuid.uuid4())]}),
        ("transaction.transfers_paired", {"paired": 1}),
        ("goal.updated", {"goal_id": str(uuid.uuid4())}),
        ("balance.updated", {}),
        ("document.deleted", {"id": str(uuid.uuid4())}),
        ("category.changed", {"id": str(uuid.uuid4())}),
        ("goal.created", {"goal_id": str(uuid.uuid4()), "all_account_ids": []}),
    ):
        emit(db, hh, type_, payload)
    events = _events_since(db, hh, marker)
    view = StreamView(_principal(db, member), frozenset({mine.id}))
    assert [e.type for e in deliverable(db, view, events)] == ["category.changed", "goal.created"]


def test_transaction_rows_are_untouched(db: Session) -> None:
    # Sanity: the payload additions do not change the transfer itself.
    owner = make_user(db, role="owner")
    hh = owner.household_id
    assert hh is not None
    a, b = make_account(db, hh), make_account(db, hh)
    out = make_transaction(db, a, amount="-1.00", posted_date=D)
    inn = make_transaction(db, b, amount="1.00", posted_date=D)
    principal = _principal(db, owner)
    group = transfers.link(db, principal, out, inn.id, request=None)
    rows = db.execute(select(Transaction).where(Transaction.transfer_group_id == group)).scalars()
    assert {r.id for r in rows} == {out.id, inn.id}

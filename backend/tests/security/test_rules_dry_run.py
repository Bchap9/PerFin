# SPDX-License-Identifier: AGPL-3.0-or-later
"""``POST /rules/test`` never writes (property tests against a real database).

Hypothesis generates rule documents (conditions, actions, saved / inline /
whole rule set), scopes and page sizes, runs the dry run as owners, members
with partial grants and viewers, and checks that

* nothing in the household changed (transactions, splits, tags, rules and
  their hit counts, audit rows, events, jobs);
* the database transaction is ``READ ONLY`` afterwards, so a write that a
  future bug might add to the dry-run path is refused by PostgreSQL
  (demonstrated by injecting one);
* only transactions on accounts the caller can see are ever reported.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from psycopg.errors import ReadOnlySqlTransaction
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import InternalError

from app.auth.models import GrantLevel
from app.auth.principal import Principal
from app.db.models import AuditLog, Event, Job
from app.db.rls import set_context
from app.domain.rules import service
from app.domain.rules.schemas import RuleTest
from app.domain.transactions.models import Rule, Transaction, TransactionSplit, TransactionTag
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.ledger import LedgerFactories, make_rule, synthetic_transactions

pytestmark = [pytest.mark.integration, pytest.mark.security]


@dataclass
class World:
    household_id: uuid.UUID
    owner: UserRecord
    member: UserRecord
    viewer: UserRecord
    granted: list[uuid.UUID]
    hidden: uuid.UUID
    categories: list[uuid.UUID]
    tags: list[uuid.UUID]
    payees: list[uuid.UUID]
    rules: list[uuid.UUID]


@pytest.fixture
def world(factories: Factories, ledger: LedgerFactories) -> World:
    owner = factories.user(role="owner", with_totp=True)
    hh = owner.household_id
    assert hh is not None
    member = factories.user(role="member", household=owner.household)
    viewer = factories.user(role="viewer", household=owner.household)
    accounts = [ledger.account(hh, name=f"Account {i}") for i in range(3)]
    for account in accounts[:2]:
        ledger.grant(hh, member.id, account, GrantLevel.EDIT)
        ledger.grant(hh, viewer.id, account, GrantLevel.VIEW)
    for i, account in enumerate(accounts):
        ledger.run(synthetic_transactions, account, count=25, seed=i)
    categories = [ledger.category(hh, name=f"Category {i}").id for i in range(4)]
    tags = [ledger.tag(hh, name=f"Tag {i}").id for i in range(3)]
    payees = [ledger.payee(hh, name=f"Synthetic Payee {i}").id for i in range(1, 4)]
    rules = [
        ledger.rule(
            hh,
            sort_order=i,
            conditions={"type": "payee", "op": "contains", "value": f"payee {i}"},
            actions=[{"type": "set_category", "category_id": str(categories[i])}],
        ).id
        for i in range(3)
    ]
    return World(
        household_id=hh,
        owner=owner,
        member=member,
        viewer=viewer,
        granted=[a.id for a in accounts[:2]],
        hidden=accounts[2].id,
        categories=categories,
        tags=tags,
        payees=payees,
        rules=rules,
    )


def _principal(record: UserRecord) -> Principal:
    return Principal(
        user=record.user,
        household_id=record.household_id,
        role=record.role,
        auth_method="session",
    )


def _fingerprint(factories: Factories, hh: uuid.UUID) -> tuple[Any, ...]:
    with factories.transaction() as db:
        set_context(db, hh)
        txns = db.execute(
            select(
                Transaction.id,
                Transaction.category_id,
                Transaction.payee_id,
                Transaction.notes,
                Transaction.review_state,
                Transaction.updated_at,
            ).order_by(Transaction.id)
        ).all()
        splits = db.execute(
            select(TransactionSplit.id, TransactionSplit.amount).order_by(TransactionSplit.id)
        ).all()
        tags = db.execute(
            select(TransactionTag.transaction_id, TransactionTag.tag_id).order_by(
                TransactionTag.transaction_id, TransactionTag.tag_id
            )
        ).all()
        rules = db.execute(
            select(Rule.id, Rule.hit_count, Rule.last_hit_at, Rule.updated_at).order_by(Rule.id)
        ).all()
        counts = tuple(
            db.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (AuditLog, Event, Job)
        )
        return (tuple(txns), tuple(splits), tuple(tags), tuple(rules), counts)


@st.composite
def _requests(draw: st.DrawFn, w: World) -> dict[str, Any]:
    text_cond = st.fixed_dictionaries(
        {
            "type": st.sampled_from(["payee", "memo"]),
            "op": st.sampled_from(["contains", "equals", "starts_with", "ends_with", "glob"]),
            "value": st.sampled_from(["payee 1", "synthetic*", "?ynthetic payee 1?", "x", "*"]),
        }
    )
    leaf = st.one_of(
        text_cond,
        st.fixed_dictionaries(
            {"type": st.just("sign"), "sign": st.sampled_from(["outflow", "inflow"])}
        ),
        st.fixed_dictionaries(
            {"type": st.just("amount"), "max": st.sampled_from(["-10", "0", "50.5"])}
        ),
        st.just({"type": "category_unset"}),
        st.fixed_dictionaries(
            {"type": st.just("category_is"), "category_ids": st.just([str(w.categories[0])])}
        ),
        st.fixed_dictionaries(
            {
                "type": st.just("account"),
                "account_ids": st.sampled_from([[str(w.granted[0])], [str(w.hidden)]]),
            }
        ),
        st.fixed_dictionaries({"type": st.just("currency"), "currencies": st.just(["USD"])}),
    )
    conditions = st.recursive(
        leaf,
        lambda children: st.one_of(
            st.fixed_dictionaries(
                {"type": st.just("all"), "conditions": st.lists(children, max_size=3)}
            ),
            st.fixed_dictionaries(
                {"type": st.just("any"), "conditions": st.lists(children, min_size=1, max_size=3)}
            ),
            st.fixed_dictionaries({"type": st.just("not"), "condition": children}),
        ),
        max_leaves=5,
    )
    action = st.one_of(
        st.just({"type": "set_category", "category_id": str(w.categories[1])}),
        st.just({"type": "add_tags", "tag_ids": [str(w.tags[0])]}),
        st.just({"type": "set_payee", "payee_id": str(w.payees[0])}),
        st.just({"type": "flag_for_review"}),
        st.just({"type": "set_note", "note": "dry"}),
        st.just(
            {
                "type": "split_percent",
                "lines": [
                    {"category_id": str(w.categories[2]), "percent": "60"},
                    {"category_id": str(w.categories[3]), "percent": "40"},
                ],
            }
        ),
    )
    mode = draw(st.sampled_from(["inline", "saved", "all"]))
    body: dict[str, Any] = {
        "limit": draw(st.integers(min_value=1, max_value=40)),
        "only_changes": draw(st.booleans()),
    }
    if mode == "inline":
        body["rule"] = {
            "conditions": draw(conditions),
            "actions": draw(
                st.lists(action, min_size=1, max_size=3, unique_by=lambda a: a["type"])
            ),
            "stop_processing": draw(st.booleans()),
        }
    elif mode == "saved":
        body["rule_id"] = str(draw(st.sampled_from(w.rules)))
    if draw(st.booleans()):
        body["account_ids"] = [str(w.granted[draw(st.integers(min_value=0, max_value=1))])]
    return body


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(data=st.data())
def test_dry_run_never_writes(world: World, factories: Factories, data: st.DataObject) -> None:
    hh = world.household_id
    body = data.draw(_requests(world))
    try:
        request = RuleTest.model_validate(body)
    except ValidationError:
        assume(False)
        raise
    who = data.draw(st.sampled_from([world.owner, world.member, world.viewer]))
    before = _fingerprint(factories, hh)
    with factories.transaction() as db:
        set_context(db, hh)
        result = service.test_rules(db, _principal(who), request)
        assert db.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
    assert _fingerprint(factories, hh) == before
    assert len(result.items) <= request.limit
    if who is not world.owner:
        assert all(item.transaction.account_id in world.granted for item in result.items)
    for item in result.items:
        assert item.matched_rule_ids


def test_a_write_in_the_dry_run_path_is_refused(
    world: World, factories: Factories, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a bug that writes during a dry run: PostgreSQL refuses it."""
    hh = world.household_id
    real = service.transaction_facts

    def writing_facts(db: Any, household_id: uuid.UUID, rows: Any) -> Any:
        make_rule(db, household_id)  # an INSERT on the dry run's transaction
        return real(db, household_id, rows)

    monkeypatch.setattr(service, "transaction_facts", writing_facts)
    before = _fingerprint(factories, hh)
    with factories.session_factory() as db:
        set_context(db, hh)
        with pytest.raises(InternalError) as caught:
            service.test_rules(db, _principal(world.owner), RuleTest())
        db.rollback()
    assert isinstance(caught.value.orig, ReadOnlySqlTransaction)
    assert _fingerprint(factories, hh) == before


def test_dry_run_over_http_writes_nothing(world: World, factories: Factories, client: Any) -> None:
    from tests.fixtures.factories import AuthedClient

    login = factories.login(client, world.owner)
    owner = AuthedClient(client, world.owner, login)
    before = _fingerprint(factories, world.household_id)
    response = owner.post("/api/v1/rules/test", json={"limit": 500})
    assert response.status_code == 200, response.text
    assert response.json()["matched"] > 0
    assert _fingerprint(factories, world.household_id) == before

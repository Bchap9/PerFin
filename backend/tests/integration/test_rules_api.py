# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rules over HTTP and the rules service against a real database.

CRUD and roles (any role reads, owner/admin change), reference checks,
reorder, account redaction for members, cross-household ``404``, the dry
run (visible history only, paginated, writes nothing), apply as a job plus
``GET /jobs/{id}``, the new-transaction hook (``apply_rules_to``) with
protected fields and dangling references, hit counts, audit and events, and
export/import between households.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth.models import GrantLevel
from app.core.errors import ProblemError
from app.core.settings import Settings
from app.db.models import Job
from app.db.rls import set_context
from app.domain.rules import service
from app.domain.rules.engine import RuleField
from app.domain.rules.schemas import (
    HIDDEN_ACCOUNT,
    RULES_MAX,
    RUN_RULES_JOB_TYPE,
    RuleCreate,
    RuleTest,
    RunRulesJob,
)
from app.domain.transactions.models import (
    Category,
    ReviewState,
    Rule,
    Transaction,
    TransactionSplit,
    TransactionTag,
)
from app.workers.handlers.rules import run_rules as run_rules_handler
from app.workers.registry import JobContext
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import (
    API,
    Actors,
    household_audit,
    household_events,
    problem_type,
)
from tests.fixtures.ledger import LedgerFactories, make_rule, make_transaction

pytestmark = pytest.mark.integration

RULES = f"{API}/rules"
D = date(2026, 3, 14)


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


def _hid(client: AuthedClient) -> uuid.UUID:
    household = client.record.household
    assert household is not None
    return household.id


def _created(response: Any) -> dict[str, Any]:
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _coffee_rule(category_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    return {
        "name": "Coffee",
        "conditions": {"type": "payee", "op": "contains", "value": "coffee"},
        "actions": [{"type": "set_category", "category_id": str(category_id)}],
        **extra,
    }


def _row(factories: Factories, household_id: uuid.UUID, model: Any, row_id: uuid.UUID) -> Any:
    with factories.transaction() as db:
        set_context(db, household_id)
        found = db.get(model, row_id)
        if found is not None:
            db.expunge(found)
        return found


def _tags(factories: Factories, household_id: uuid.UUID, txn_id: uuid.UUID) -> set[uuid.UUID]:
    with factories.transaction() as db:
        set_context(db, household_id)
        return set(
            db.execute(
                select(TransactionTag.tag_id).where(TransactionTag.transaction_id == txn_id)
            ).scalars()
        )


def _splits(
    factories: Factories, household_id: uuid.UUID, txn_id: uuid.UUID
) -> list[tuple[uuid.UUID | None, Decimal]]:
    with factories.transaction() as db:
        set_context(db, household_id)
        return [
            (s.category_id, s.amount)
            for s in db.execute(
                select(TransactionSplit)
                .where(TransactionSplit.transaction_id == txn_id)
                .order_by(TransactionSplit.amount)
            ).scalars()
        ]


def _ctx(db: Session, household_id: uuid.UUID, job_id: uuid.UUID) -> JobContext:
    return JobContext(
        job_id=job_id,
        job_type=RUN_RULES_JOB_TYPE,
        attempt=1,
        max_attempts=3,
        household_id=household_id,
        session=db,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


# ---- CRUD, roles, references ---------------------------------------------------------


def test_crud_roles_and_audit(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    member = actors.new(role="member", household=owner.record.household)
    viewer = actors.new(role="viewer", household=owner.record.household)
    admin = actors.new(role="admin", household=owner.record.household)
    food = ledger.category(hh, name="Coffee shops")
    tag = ledger.tag(hh, name="caffeine")

    assert member.post(RULES, json=_coffee_rule(food.id)).status_code == 403
    created = _created(owner.post(RULES, json=_coffee_rule(food.id, stop_processing=True)))
    assert created["sort_order"] == 0
    assert created["stop_processing"] is True
    assert created["hit_count"] == 0
    assert created["conditions"] == {"type": "payee", "op": "contains", "value": "coffee"}
    second = _created(
        admin.post(
            RULES,
            json={
                "name": "Tag big spends",
                "conditions": {"type": "amount", "max": "-100.00"},
                "actions": [{"type": "add_tags", "tag_ids": [str(tag.id)]}],
                "enabled": False,
            },
        )
    )
    assert second["sort_order"] == 1
    assert second["conditions"]["max"] == "-100.000000"

    for reader in (viewer, member):
        listed = reader.get(RULES)
        assert listed.status_code == 200
        assert [r["id"] for r in listed.json()["items"]] == [created["id"], second["id"]]
        assert reader.get(f"{RULES}/{created['id']}").json()["name"] == "Coffee"

    patched = admin.patch(f"{RULES}/{created['id']}", json={"name": "Café", "enabled": False})
    assert patched.status_code == 200, patched.text
    assert (patched.json()["name"], patched.json()["enabled"]) == ("Café", False)
    assert member.patch(f"{RULES}/{created['id']}", json={"name": "x"}).status_code == 403
    null = admin.patch(f"{RULES}/{created['id']}", json={"conditions": None})
    assert null.status_code == 422
    assert member.delete(f"{RULES}/{created['id']}").status_code == 403
    assert owner.delete(f"{RULES}/{created['id']}").status_code == 204
    gone = owner.get(f"{RULES}/{created['id']}")
    assert (gone.status_code, problem_type(gone)) == (404, "rule-not-found")

    created_rows = household_audit(factories, hh, "rule.create")
    assert len(created_rows) == 2
    after = created_rows[0].after
    assert after is not None
    assert after["conditions"] == ["payee"]
    assert after["references"] == {"category": [str(food.id)]}
    assert "coffee" not in str(after)  # condition text stays out of the audit trail
    assert household_audit(factories, hh, "rule.update")
    assert household_audit(factories, hh, "rule.delete")
    assert household_events(factories, hh, "rule.created")
    assert household_events(factories, hh, "rule.deleted")


def test_references_must_be_objects_of_the_household(
    actors: Actors, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    other = actors.new(role="owner")
    foreign_category = ledger.category(_hid(other))
    foreign_account = ledger.account(_hid(other))
    foreign_tag = ledger.tag(_hid(other))
    mine = ledger.category(_hid(owner))
    cases = [
        (_coffee_rule(foreign_category.id), "category-not-found"),
        (_coffee_rule(uuid.uuid4()), "category-not-found"),
        (
            {
                **_coffee_rule(mine.id),
                "conditions": {"type": "account", "account_ids": [str(foreign_account.id)]},
            },
            "account-not-found",
        ),
        (
            {
                **_coffee_rule(mine.id),
                "actions": [{"type": "add_tags", "tag_ids": [str(foreign_tag.id)]}],
            },
            "tag-not-found",
        ),
        (
            {
                **_coffee_rule(mine.id),
                "actions": [{"type": "set_payee", "payee_id": str(uuid.uuid4())}],
            },
            "payee-not-found",
        ),
    ]
    for body, problem in cases:
        response = owner.post(RULES, json=body)
        assert (response.status_code, problem_type(response)) == (404, problem), body
    assert owner.get(RULES).json()["items"] == []
    smuggled = owner.post(RULES, json={**_coffee_rule(mine.id), "hit_count": 99})
    assert smuggled.status_code == 422
    regex = owner.post(
        RULES,
        json={
            **_coffee_rule(mine.id),
            "conditions": {"type": "payee", "op": "regex", "value": "(a+)+"},
        },
    )
    assert regex.status_code == 422


def test_other_household_gets_404(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    other = actors.new(role="owner")
    rule = _created(owner.post(RULES, json=_coffee_rule(ledger.category(_hid(owner)).id)))
    url = f"{RULES}/{rule['id']}"
    for response in (
        other.get(url),
        other.patch(url, json={"name": "mine now"}),
        other.delete(url),
        other.post(f"{RULES}/test", json={"rule_id": rule["id"]}),
        other.post(f"{RULES}/apply", json={"rule_ids": [rule["id"]]}),
    ):
        assert (response.status_code, problem_type(response)) == (404, "rule-not-found")
    assert other.get(RULES).json()["items"] == []
    assert owner.get(url).json()["name"] == "Coffee"


def test_reorder(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    member = actors.new(role="member", household=owner.record.household)
    category = ledger.category(_hid(owner))
    ids = [
        _created(owner.post(RULES, json={**_coffee_rule(category.id), "name": f"R{i}"}))["id"]
        for i in range(3)
    ]
    wanted = [ids[2], ids[0], ids[1]]
    assert member.post(f"{RULES}/reorder", json={"rule_ids": wanted}).status_code == 403
    response = owner.post(f"{RULES}/reorder", json={"rule_ids": wanted})
    assert response.status_code == 200, response.text
    assert [r["id"] for r in owner.get(RULES).json()["items"]] == wanted
    assert [r["sort_order"] for r in owner.get(RULES).json()["items"]] == [0, 1, 2]
    for bad in ([ids[0], ids[1]], [*wanted, str(uuid.uuid4())], [ids[0], ids[0], ids[1]]):
        refused = owner.post(f"{RULES}/reorder", json={"rule_ids": bad})
        assert refused.status_code == 422
    assert problem_type(owner.post(f"{RULES}/reorder", json={"rule_ids": ids[:2]})) == (
        "rule-order-mismatch"
    )


def test_members_see_hidden_accounts_redacted(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    member = actors.new(role="member", household=owner.record.household)
    shared = ledger.account(hh, name="Shared")
    hidden = ledger.account(hh, name="Private")
    ledger.grant(hh, member.user.id, shared, GrantLevel.VIEW)
    category = ledger.category(hh)
    body = {
        **_coffee_rule(category.id),
        "conditions": {
            "type": "any",
            "conditions": [
                {"type": "account", "account_ids": [str(shared.id), str(hidden.id)]},
                {"type": "account", "account_ids": [str(hidden.id)]},
            ],
        },
    }
    created = _created(owner.post(RULES, json=body))
    assert created["redacted"] is False
    seen = member.get(f"{RULES}/{created['id']}").json()
    assert seen["redacted"] is True
    lists = [c["account_ids"] for c in seen["conditions"]["conditions"]]
    assert lists == [[str(shared.id), str(HIDDEN_ACCOUNT)], [str(HIDDEN_ACCOUNT)]]
    exported = member.get(f"{RULES}/export").json()
    assert str(hidden.id) not in str(exported)
    assert [a["name"] for a in exported["references"]["accounts"]] == ["Shared"]
    assert str(hidden.id) in str(owner.get(f"{RULES}/export").json())


# ---- dry run -------------------------------------------------------------------------


def _txn_state(factories: Factories, hh: uuid.UUID) -> list[tuple[Any, ...]]:
    with factories.transaction() as db:
        set_context(db, hh)
        rows = db.execute(select(Transaction).order_by(Transaction.id)).scalars()
        return [
            (r.id, r.category_id, r.payee_id, r.notes, r.review_state, r.updated_at) for r in rows
        ]


def test_dry_run_reports_matches_and_writes_nothing(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    member = actors.new(role="member", household=owner.record.household)
    mine = ledger.account(hh, name="Joint")
    hidden = ledger.account(hh, name="Private")
    ledger.grant(hh, member.user.id, mine, GrantLevel.EDIT)
    coffee = ledger.category(hh, name="Coffee")
    tag = ledger.tag(hh)
    visible_txns = [
        ledger.transaction(
            mine, amount="-4.50", raw_payee=f"Corner Coffee {i}", posted_date=D - timedelta(days=i)
        )
        for i in range(5)
    ]
    ledger.transaction(
        mine, amount="-80.00", raw_payee="Grocer", review_state=ReviewState.CONFIRMED
    )
    hidden_txn = ledger.transaction(hidden, amount="-3.00", raw_payee="Coffee Hidden")
    saved = _created(
        owner.post(
            RULES,
            json={
                **_coffee_rule(coffee.id),
                "actions": [
                    {"type": "set_category", "category_id": str(coffee.id)},
                    {"type": "add_tags", "tag_ids": [str(tag.id)]},
                ],
            },
        )
    )
    before = _txn_state(factories, hh)

    owner_run = owner.post(f"{RULES}/test", json={})
    assert owner_run.status_code == 200, owner_run.text
    result = owner_run.json()
    matched = {item["transaction"]["id"] for item in result["items"]}
    assert matched == {str(t.id) for t in visible_txns} | {str(hidden_txn.id)}
    assert result["next_cursor"] is None
    assert result["scanned"] == 7
    first = result["items"][0]
    assert first["matched_rule_ids"] == [saved["id"]]
    assert first["changes"]["category_id"] == {"before": None, "after": str(coffee.id)}
    assert first["changes"]["added_tag_ids"] == [str(tag.id)]

    # A member sees only the accounts granted to them.
    member_run = member.post(f"{RULES}/test", json={"rule_id": saved["id"]}).json()
    assert {i["transaction"]["id"] for i in member_run["items"]} == {
        str(t.id) for t in visible_txns
    }
    refused = member.post(f"{RULES}/test", json={"account_ids": [str(hidden.id)]})
    assert (refused.status_code, problem_type(refused)) == (404, "account-not-found")

    # Pages of one, newest first, until history is exhausted.
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page = member.post(
            f"{RULES}/test", json={"rule_id": saved["id"], "limit": 1, "cursor": cursor}
        ).json()
        seen += [i["transaction"]["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [str(t.id) for t in visible_txns]

    # An unsaved rule, and only_changes.
    draft = {
        "conditions": {"type": "payee", "op": "glob", "value": "grocer*"},
        "actions": [{"type": "set_note", "note": "weekly shop"}],
    }
    drafted = owner.post(f"{RULES}/test", json={"rule": draft}).json()
    assert len(drafted["items"]) == 1
    assert drafted["items"][0]["matched_rule_ids"] == [str(uuid.UUID(int=0))]
    assert drafted["items"][0]["changes"]["notes"] == {"before": None, "after": "weekly shop"}
    flag = {**draft, "actions": [{"type": "flag_for_review"}]}  # already confirmed
    assert len(owner.post(f"{RULES}/test", json={"rule": flag}).json()["items"]) == 1
    unchanged = owner.post(f"{RULES}/test", json={"rule": flag, "only_changes": True}).json()
    assert unchanged["items"] == []
    dated = owner.post(
        f"{RULES}/test", json={"rule": draft, "date_from": (D + timedelta(days=1)).isoformat()}
    ).json()
    assert dated["items"] == []

    both = owner.post(f"{RULES}/test", json={"rule": draft, "rule_id": saved["id"]})
    assert both.status_code == 422
    assert _txn_state(factories, hh) == before
    assert _row(factories, hh, Rule, uuid.UUID(saved["id"])).hit_count == 0


def test_dry_run_transaction_is_read_only(
    db: Session, factories: Factories, ledger: LedgerFactories
) -> None:
    """PostgreSQL refuses any write after the dry run switched the transaction."""
    from psycopg.errors import ReadOnlySqlTransaction
    from sqlalchemy.exc import InternalError

    from app.auth.principal import Principal

    record = factories.user(role="owner")
    hh = record.household_id
    assert hh is not None
    account = ledger.account(hh)
    ledger.transaction(account, raw_payee="Coffee")
    principal = Principal(
        user=record.user, household_id=hh, role=record.role, auth_method="session"
    )
    set_context(db, hh)
    data = RuleTest.model_validate({})
    result = service.test_rules(db, principal, data)
    assert result.scanned == 1
    with pytest.raises(InternalError) as caught:
        make_rule(db, hh)
    assert isinstance(caught.value.orig, ReadOnlySqlTransaction)


# ---- apply (job) ---------------------------------------------------------------------


def test_apply_queues_a_job_that_applies_the_rules(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    member = actors.new(role="member", household=owner.record.household)
    viewer = actors.new(role="viewer", household=owner.record.household)
    account = ledger.account(hh)
    coffee = ledger.category(hh, name="Coffee")
    shared = ledger.category(hh, name="Shared")
    rent = ledger.category(hh, name="Rent")
    tag = ledger.tag(hh)
    payee = ledger.payee(hh, name="Corner Coffee")
    cafe = ledger.transaction(account, amount="-4.50", raw_payee="CORNER COFFEE 12")
    rent_txn = ledger.transaction(account, amount="-1000.00", raw_payee="Landlord")
    other = ledger.transaction(account, amount="25.00", raw_payee="Refund")
    rules = [
        {
            "name": "Rename",
            "conditions": {"type": "payee", "op": "contains", "value": "corner coffee"},
            "actions": [{"type": "set_payee", "payee_id": str(payee.id)}],
        },
        {
            "name": "Categorise by payee",
            "conditions": {"type": "payee_is", "payee_ids": [str(payee.id)]},
            "actions": [
                {"type": "set_category", "category_id": str(coffee.id)},
                {"type": "add_tags", "tag_ids": [str(tag.id)]},
            ],
            "stop_processing": True,
        },
        {
            "name": "Rent split",
            "conditions": {"type": "amount", "max": "-500"},
            "actions": [
                {
                    "type": "split_percent",
                    "lines": [
                        {"category_id": str(rent.id), "percent": "75"},
                        {"category_id": str(shared.id), "percent": "25"},
                    ],
                },
                {"type": "set_note", "note": "split 75/25"},
                {"type": "flag_for_review"},
            ],
        },
        {
            "name": "Fallback",
            "conditions": {"type": "category_unset"},
            "actions": [{"type": "set_note", "note": "never reached for coffee"}],
        },
    ]
    ids = [_created(owner.post(RULES, json=r))["id"] for r in rules]

    assert member.post(f"{RULES}/apply", json={}).status_code == 403
    accepted = owner.post(f"{RULES}/apply", json={}, headers={"Idempotency-Key": "apply-1"})
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]
    replay = owner.post(f"{RULES}/apply", json={}, headers={"Idempotency-Key": "apply-1"})
    assert (replay.status_code, replay.json()["job_id"]) == (202, job_id)

    for reader in (owner, viewer):
        status = reader.get(f"{API}/jobs/{job_id}")
        assert status.status_code == 200, status.text
        assert status.json()["type"] == "run_rules"
        assert (status.json()["state"], status.json()["finished"]) == ("queued", False)
        assert "payload" not in status.json()
    assert household_audit(factories, hh, "rule.apply_requested")
    assert household_events(factories, hh, "job.queued")

    with factories.transaction() as db:
        set_context(db, hh)
        job = db.get(Job, uuid.UUID(job_id))
        assert job is not None
        payload = RunRulesJob.model_validate(job.payload)
        assert payload.requested_by == owner.user.id
        run_rules_handler(_ctx(db, hh, job.id), payload)

    cafe_row = _row(factories, hh, Transaction, cafe.id)
    assert (cafe_row.payee_id, cafe_row.category_id, cafe_row.notes) == (payee.id, coffee.id, None)
    assert _tags(factories, hh, cafe.id) == {tag.id}
    rent_row = _row(factories, hh, Transaction, rent_txn.id)
    assert rent_row.category_id is None
    assert rent_row.notes == "split 75/25"
    assert rent_row.review_state is ReviewState.PENDING
    assert _splits(factories, hh, rent_txn.id) == [
        (rent.id, Decimal("-750.000000")),
        (shared.id, Decimal("-250.000000")),
    ]
    other_row = _row(factories, hh, Transaction, other.id)
    assert other_row.notes == "never reached for coffee"

    hits = [_row(factories, hh, Rule, uuid.UUID(i)) for i in ids]
    assert [r.hit_count for r in hits] == [1, 1, 1, 1]
    assert all(r.last_hit_at is not None for r in hits)
    applied = household_audit(factories, hh, "rule.apply")
    assert len(applied) == 1
    assert applied[0].after is not None
    assert applied[0].after["changed"] == 3
    events = household_events(factories, hh, "transaction.rules_applied")
    assert events[-1].payload["job_id"] == job_id

    # Running again changes nothing (except hit counts) and writes no event.
    with factories.transaction() as db:
        set_context(db, hh)
        again = service.run_rules(db, hh, RunRulesJob())
    assert (again.matched, again.changed) == (3, 0)
    assert len(household_events(factories, hh, "transaction.rules_applied")) == 1


def test_apply_scope_and_unknown_rules(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    other = actors.new(role="owner")
    refused = owner.post(f"{RULES}/apply", json={"rule_ids": [str(uuid.uuid4())]})
    assert (refused.status_code, problem_type(refused)) == (404, "rule-not-found")
    foreign = ledger.account(_hid(other))
    hidden = owner.post(f"{RULES}/apply", json={"account_ids": [str(foreign.id)]})
    assert (hidden.status_code, problem_type(hidden)) == (404, "account-not-found")
    bad_dates = owner.post(
        f"{RULES}/apply", json={"date_from": "2026-02-01", "date_to": "2026-01-01"}
    )
    assert bad_dates.status_code == 422
    ok = owner.post(f"{RULES}/apply", json={"account_ids": [str(ledger.account(hh).id)]})
    assert ok.status_code == 202
    # Another household cannot read the job.
    job_url = f"{API}/jobs/{ok.json()['job_id']}"
    assert (other.get(job_url).status_code, problem_type(other.get(job_url))) == (
        404,
        "job-not-found",
    )
    assert problem_type(owner.get(f"{API}/jobs/{uuid.uuid4()}")) == "job-not-found"


def test_job_status_needs_the_family_scope_for_tokens(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    job_id = owner.post(f"{RULES}/apply", json={}).json()["job_id"]
    url = f"{API}/jobs/{job_id}"
    client = actors.anonymous()
    for scopes, expected in ((["transactions:read"], 200), (["documents:read"], 403)):
        raw = factories.token(owner.record, scopes=scopes)
        response = client.get(url, headers={"Authorization": f"Bearer {raw}"})
        assert response.status_code == expected, (scopes, response.text)
    # A job family tokens cannot read at all.
    with factories.transaction() as db:
        set_context(db, hh)
        noop = Job(type="noop", payload={}, household_id=hh, max_attempts=1)
        db.add(noop)
        db.flush()
        noop_id = noop.id
    raw = factories.token(owner.record, scopes=["transactions:read", "documents:read"])
    response = client.get(f"{API}/jobs/{noop_id}", headers={"Authorization": f"Bearer {raw}"})
    assert (response.status_code, problem_type(response)) == (403, "insufficient-scope")
    assert owner.get(f"{API}/jobs/{noop_id}").json()["state"] == "queued"


# ---- the new-transaction hook --------------------------------------------------------


def test_apply_rules_to_new_transactions(
    db: Session, factories: Factories, ledger: LedgerFactories
) -> None:
    record = factories.user(role="owner")
    hh = record.household_id
    assert hh is not None
    account = ledger.account(hh)
    coffee = ledger.category(hh, name="Coffee")
    manual = ledger.category(hh, name="Picked by hand")
    tag = ledger.tag(hh)
    actions = [
        {"type": "set_category", "category_id": str(coffee.id)},
        {"type": "add_tags", "tag_ids": [str(tag.id)]},
    ]
    rule = ledger.rule(
        hh, conditions={"type": "payee", "op": "contains", "value": "coffee"}, actions=actions
    )
    ledger.rule(hh, sort_order=5, enabled=False, actions=[{"type": "set_note", "note": "off"}])
    imported = make_transaction(db, account, raw_payee="Coffee Cart")
    by_hand = make_transaction(db, account, raw_payee="Coffee Cart", category=manual)
    unrelated = make_transaction(db, account, raw_payee="Bookshop")
    set_context(db, hh)

    summary = service.apply_rules_to(db, hh, [imported, unrelated], user_id=record.id)
    assert (summary.scanned, summary.matched, summary.changed_ids) == (2, 1, [imported.id])
    assert imported.category_id == coffee.id
    assert unrelated.category_id is None
    assert unrelated.notes is None

    protected = service.apply_rules_to(db, hh, [by_hand], protect={RuleField.CATEGORIZATION})
    assert by_hand.category_id == manual.id  # what the person chose stays
    assert protected.changed_ids == [by_hand.id]  # the tag was still added
    tags = set(
        db.execute(
            select(TransactionTag.tag_id).where(TransactionTag.transaction_id == by_hand.id)
        ).scalars()
    )
    assert tags == {tag.id}
    fresh = db.get(Rule, rule.id)
    assert fresh is not None
    db.refresh(fresh)
    assert fresh.hit_count == 2
    with pytest.raises(ValueError, match="household"):
        service.apply_rules_to(db, uuid.uuid4(), [imported])


def test_dangling_references_are_skipped_not_fatal(
    db: Session, factories: Factories, ledger: LedgerFactories
) -> None:
    record = factories.user(role="owner")
    hh = record.household_id
    assert hh is not None
    account = ledger.account(hh)
    doomed = ledger.category(hh)
    tag = ledger.tag(hh)
    ledger.rule(
        hh,
        actions=[
            {"type": "set_category", "category_id": str(doomed.id)},
            {"type": "add_tags", "tag_ids": [str(tag.id)]},
        ],
    )
    ledger.rule(hh, sort_order=3, conditions={"type": "not-a-condition"})  # never runs
    txn = make_transaction(db, account, raw_payee="Anything")
    set_context(db, hh)
    db.execute(delete(Category).where(Category.id == doomed.id))
    summary = service.apply_rules_to(db, hh, [txn])
    assert summary.changed_ids == [txn.id]
    assert txn.category_id is None
    db.flush()
    db.execute(select(1))  # the deferred household foreign keys hold
    db.connection().exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")


# ---- export / import -----------------------------------------------------------------


def test_export_import_between_households(actors: Actors, ledger: LedgerFactories) -> None:
    source = actors.new(role="owner")
    target = actors.new(role="owner")
    target_member = actors.new(role="member", household=target.record.household)
    src, dst = _hid(source), _hid(target)
    food = ledger.category(src, name="Food")
    coffee = ledger.category(src, name="Coffee", parent=food)
    tag = ledger.tag(src, name="Treats")
    payee = ledger.payee(src, name="Corner Coffee")
    checking = ledger.account(src, name="Checking")
    _created(
        source.post(
            RULES,
            json={
                "name": "Coffee",
                "conditions": {
                    "type": "all",
                    "conditions": [
                        {"type": "payee", "op": "glob", "value": "*coffee*"},
                        {"type": "account", "account_ids": [str(checking.id)]},
                    ],
                },
                "actions": [
                    {"type": "set_category", "category_id": str(coffee.id)},
                    {"type": "add_tags", "tag_ids": [str(tag.id)]},
                    {"type": "set_payee", "payee_id": str(payee.id)},
                ],
            },
        )
    )
    exported = source.get(f"{RULES}/export")
    assert exported.status_code == 200
    document = exported.json()
    assert document["format"] == "perfin-rules"
    assert {r["name"] for r in document["references"]["categories"]} == {"Food / Coffee"}
    assert "hit_count" not in str(document)

    # The target has none of these objects yet: nothing is created.
    missing = target.post(f"{RULES}/import", json=document)
    assert (missing.status_code, problem_type(missing)) == (422, "rule-reference-unresolved")
    assert "Food / Coffee" in missing.json()["detail"]
    assert target.get(RULES).json()["items"] == []

    t_food = ledger.category(dst, name="food")
    t_coffee = ledger.category(dst, name="COFFEE", parent=t_food)
    t_tag = ledger.tag(dst, name="treats")
    t_payee = ledger.payee(dst, name="corner coffee")
    t_account = ledger.account(dst, name="checking")
    assert target_member.post(f"{RULES}/import", json=document).status_code == 403
    imported = target.post(f"{RULES}/import", json=document)
    assert imported.status_code == 201, imported.text
    [rule] = imported.json()["items"]
    text = str(rule)
    for new_id in (t_coffee.id, t_tag.id, t_payee.id, t_account.id):
        assert str(new_id) in text
    for old_id in (coffee.id, tag.id, payee.id, checking.id):
        assert str(old_id) not in text

    # Re-importing into the source keeps its own ids; replace drops the old rules.
    again = source.post(f"{RULES}/import", json={**document, "mode": "append"})
    assert len(again.json()["items"]) == 2
    replaced = source.post(f"{RULES}/import", json={**document, "mode": "replace"})
    assert len(replaced.json()["items"]) == 1
    assert replaced.json()["items"][0]["sort_order"] == 0
    junk = source.post(f"{RULES}/import", json={**document, "format": "other"})
    assert junk.status_code == 422


def test_invalid_stored_rules_never_run(
    db: Session, factories: Factories, ledger: LedgerFactories
) -> None:
    record = factories.user(role="owner")
    hh = record.household_id
    assert hh is not None
    ledger.rule(hh, conditions={"all": []}, actions=[])
    good = ledger.rule(hh, sort_order=1)
    set_context(db, hh)
    assert [r.id for r in service.compiled_rules(db, hh)] == [good.id]


def test_rule_limit(db: Session, factories: Factories) -> None:
    from app.auth.principal import Principal

    record = factories.user(role="owner")
    hh = record.household_id
    assert hh is not None
    principal = Principal(
        user=record.user, household_id=hh, role=record.role, auth_method="session"
    )
    set_context(db, hh)
    for i in range(RULES_MAX):
        db.add(
            Rule(
                household_id=hh,
                name=f"R{i}",
                sort_order=i,
                conditions={"type": "all", "conditions": []},
                actions=[{"type": "flag_for_review"}],
            )
        )
    db.flush()
    body = RuleCreate.model_validate(
        {
            "name": "one more",
            "conditions": {"type": "all"},
            "actions": [{"type": "flag_for_review"}],
        }
    )
    with pytest.raises(ProblemError) as caught:
        service.create_rule(db, principal, body, request=None)
    assert caught.value.slug == "rule-limit"

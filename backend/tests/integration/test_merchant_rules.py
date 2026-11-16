# SPDX-License-Identifier: AGPL-3.0-or-later
""" "Always categorise this merchant as...": the preview, the rule, and its undo.

Over HTTP against a real database: the counts a preview gives are the rows
the apply job then changes, a member sees and changes only accounts they
may edit, a category a person chose is never touched, an earlier rule that
already decides the row refuses the save, and a revert returns exactly the
rows the rule categorised to undecided without moving their ``updated_at``.

Merchants and descriptors are invented (``corner grocer``, ``harbour
bakery``); ``merchant_key`` and the provenance columns are set by hand, as
the engine's lane writes them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.db.models import Job
from app.db.rls import set_context
from app.domain.categorisation import engine as categorisation
from app.domain.rules import service
from app.domain.rules.schemas import RULES_MAX, RUN_RULES_JOB_TYPE, RunRulesJob
from app.domain.transactions.models import Rule, Transaction, TransactionSplit
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
PREVIEW = f"{RULES}/merchant/preview"
MERCHANT = f"{RULES}/merchant"
KEY = "corner grocer"
RAW = "CORNER GROCER 0417"
REFRESH_JOB_TYPE = "categorise.refresh"


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


@pytest.fixture(autouse=True)
def decided(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """Stands in for the engine's ``person_decided`` (another lane's), doing the
    part these tests read: the row is marked as a person's. The ids it was
    called for are returned."""
    calls: list[uuid.UUID] = []

    def person_decided(
        db: Session,
        row: Transaction,
        *,
        before_category_id: uuid.UUID | None,
        before_source: str | None,
        at: datetime,
    ) -> None:
        del db, before_category_id, before_source, at
        row.category_source = "person"
        row.category_confidence = Decimal(1)
        row.category_rule_id = None
        row.suggested_category_id = None
        calls.append(row.id)

    monkeypatch.setattr(categorisation, "person_decided", person_decided)
    return calls


def _hid(client: AuthedClient) -> uuid.UUID:
    household = client.record.household
    assert household is not None
    return household.id


def _body(row: Transaction, category: Any, **extra: Any) -> dict[str, Any]:
    return {"transaction_id": str(row.id), "category_id": str(category.id), **extra}


def _ok(response: Any, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    return body


def _row(factories: Factories, household_id: uuid.UUID, row_id: uuid.UUID) -> Transaction:
    with factories.transaction() as db:
        set_context(db, household_id)
        found = db.get(Transaction, row_id)
        assert found is not None
        db.expunge(found)
        return found


def _provenance(row: Transaction) -> tuple[Any, ...]:
    return (row.category_id, row.category_source, row.category_rule_id, row.category_confidence)


def _state(factories: Factories, household_id: uuid.UUID, row_id: uuid.UUID) -> tuple[Any, ...]:
    row = _row(factories, household_id, row_id)
    return (*_provenance(row), row.suggested_category_id, row.updated_at)


def _rules(factories: Factories, household_id: uuid.UUID) -> list[Rule]:
    with factories.transaction() as db:
        set_context(db, household_id)
        rows = list(
            db.execute(
                select(Rule).where(Rule.household_id == household_id).order_by(Rule.sort_order)
            ).scalars()
        )
        for row in rows:
            db.expunge(row)
        return rows


def _jobs(factories: Factories, household_id: uuid.UUID, job_type: str) -> list[Job]:
    with factories.transaction() as db:
        set_context(db, household_id)
        rows = list(
            db.execute(
                select(Job).where(Job.household_id == household_id, Job.type == job_type)
            ).scalars()
        )
        for row in rows:
            db.expunge(row)
        return rows


def _run_job(factories: Factories, household_id: uuid.UUID, job_id: str) -> RunRulesJob:
    """Run a queued ``run_rules`` job as the worker would; its payload is returned."""
    with factories.transaction() as db:
        set_context(db, household_id)
        job = db.get(Job, uuid.UUID(job_id))
        assert job is not None
        assert job.type == RUN_RULES_JOB_TYPE
        payload = RunRulesJob.model_validate(job.payload)
        ctx = JobContext(
            job_id=job.id,
            job_type=RUN_RULES_JOB_TYPE,
            attempt=1,
            max_attempts=3,
            household_id=household_id,
            session=db,
            deadline=datetime.now(UTC) + timedelta(minutes=5),
        )
        run_rules_handler(ctx, payload)
        return payload


def _grocer(ledger: LedgerFactories, account: Any, **fields: Any) -> Transaction:
    fields.setdefault("raw_payee", RAW)
    fields.setdefault("merchant_key", KEY)
    return ledger.transaction(account, **fields)


# ---- preview -------------------------------------------------------------------------


def test_preview_counts_every_state_truthfully(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    account = ledger.account(hh)
    target = ledger.category(hh, name="Groceries")
    other = ledger.category(hh, name="Eating out")

    undecided = [_grocer(ledger, account) for _ in range(14)]
    suggested = [
        _grocer(ledger, account, suggested_category_id=other.id),
        _grocer(ledger, account, suggested_category_id=target.id),
        # A suggestion that says where it came from is still only a suggestion.
        _grocer(
            ledger,
            account,
            suggested_category_id=other.id,
            category_source="dictionary",
            category_confidence=Decimal("0.60"),
        ),
    ]
    guessed = [
        _grocer(ledger, account, category=other, category_source=source)
        for source in ("dictionary", "dictionary", "dictionary", "learned", "keyword")
    ]
    by_hand = [_grocer(ledger, account, category=other, category_source="person") for _ in range(3)]
    by_hand.append(_grocer(ledger, account, category_source="person"))  # cleared by a person
    legacy = [_grocer(ledger, account, category=other, category_source="legacy") for _ in range(2)]
    already = [
        _grocer(ledger, account, category=target, category_source="person"),
        _grocer(ledger, account, category=target, category_source="dictionary"),
    ]
    assert len(undecided + suggested + guessed + by_hand + legacy + already) == 30
    # Negative: a longer key, and the same words with no key, are other merchants.
    ledger.transaction(account, raw_payee="CORNER GROCER DELI", merchant_key="corner grocer deli")
    ledger.transaction(account, raw_payee=RAW)

    before = [_state(factories, hh, row.id) for row in undecided + suggested + guessed]
    seen = _ok(owner.post(PREVIEW, json=_body(undecided[0], target)))
    assert (seen["merchant_key"], seen["merchant"]) == (KEY, "Corner Grocer")
    assert (seen["teachable"], seen["reason"]) == (True, None)
    assert (seen["uncategorised"], seen["guessed"], seen["decided"], seen["same_category"]) == (
        17,
        5,
        6,
        2,
    )
    assert (seen["existing_rule_id"], seen["shadowed_by"]) == (None, None)
    affected = {str(row.id) for row in undecided + suggested + guessed}
    assert len(seen["sample"]) == 20
    assert set(seen["sample"]) <= affected
    # A preview writes nothing.
    assert [_state(factories, hh, row.id) for row in undecided + suggested + guessed] == before
    assert _rules(factories, hh) == []

    # The counts are counts, not a page: 700 rows of another merchant are 700.
    def many(db: Session) -> Transaction:
        rows = [
            make_transaction(db, account, raw_payee="HARBOUR BAKERY", merchant_key="harbour bakery")
            for _ in range(700)
        ]
        return rows[0]

    first = ledger.run(many)
    big = _ok(owner.post(PREVIEW, json=_body(first, target)))
    assert (big["uncategorised"], big["guessed"], big["decided"], big["same_category"]) == (
        700,
        0,
        0,
        0,
    )
    assert len(big["sample"]) == 20


def test_preview_leaves_out_accounts_the_member_cannot_edit(
    actors: Actors, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    member = actors.new(role="member", household=owner.record.household)
    mine = ledger.account(hh)
    readable = ledger.account(hh)
    hidden = ledger.account(hh)
    ledger.grant(hh, member.user.id, mine, GrantLevel.EDIT)
    ledger.grant(hh, member.user.id, readable, GrantLevel.VIEW)
    target = ledger.category(hh, name="Groceries")
    editable = [_grocer(ledger, mine) for _ in range(2)]
    seen_only = [_grocer(ledger, readable) for _ in range(3)]
    unseen = [_grocer(ledger, hidden) for _ in range(4)]

    # From a row the member may only read: the rows they may edit are counted.
    seen = _ok(member.post(PREVIEW, json=_body(seen_only[0], target)))
    assert (seen["teachable"], seen["uncategorised"]) == (True, 2)
    assert set(seen["sample"]) == {str(row.id) for row in editable}
    # The owner sees them all.
    assert _ok(owner.post(PREVIEW, json=_body(seen_only[0], target)))["uncategorised"] == 9
    # A row on an account the member cannot see does not exist for them.
    refused = member.post(PREVIEW, json=_body(unseen[0], target))
    assert refused.status_code == 404
    assert problem_type(refused) == "transaction-not-found"


def test_preview_says_why_a_transfer_or_a_p2p_row_cannot_teach(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    account = ledger.account(hh)
    target = ledger.category(hh, name="Groceries")
    half = ledger.category(hh, name="Household")
    transfer = ledger.transaction(
        account, raw_payee="ONLINE TRANSFER TO SAV ...1234", merchant_key="sav"
    )
    person = ledger.transaction(
        account, raw_payee="ZELLE PAYMENT TO RILEY EXAMPLE", merchant_key="zelle"
    )
    no_key = ledger.transaction(account, raw_payee=RAW)
    split = _grocer(ledger, account, amount="-20.00")
    ledger.split(split, amount="-12.00", category=target)
    ledger.split(split, amount="-8.00", category=half)
    _grocer(ledger, account)  # a row the split one's merchant could have counted

    for row, reason in (
        (transfer, "not_a_purchase"),
        (person, "not_a_purchase"),
        (no_key, "no_merchant"),
        (split, "split"),
    ):
        seen = _ok(owner.post(PREVIEW, json=_body(row, target)))
        assert (seen["teachable"], seen["reason"]) == (False, reason), reason
        counts = (seen["uncategorised"], seen["guessed"], seen["decided"], seen["same_category"])
        assert counts == (0, 0, 0, 0)
        assert seen["sample"] == []
        refused = owner.post(MERCHANT, json=_body(row, target))
        assert refused.status_code == 422, refused.text
        assert problem_type(refused) == "merchant-not-teachable"
    assert _rules(factories, hh) == []
    # Positive control: the same merchant from a row that is a purchase can teach.
    assert _ok(owner.post(PREVIEW, json=_body(_grocer(ledger, account), target)))["teachable"]


# ---- create --------------------------------------------------------------------------


def test_create_appends_a_merchant_rule_a_member_may_make(
    actors: Actors, factories: Factories, ledger: LedgerFactories, decided: list[uuid.UUID]
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    member = actors.new(role="member", household=owner.record.household)
    account = ledger.account(hh)
    ledger.grant(hh, member.user.id, account, GrantLevel.EDIT)
    target = ledger.category(hh, name="Groceries")
    general = ledger.rule(hh, sort_order=7)
    row = _grocer(ledger, account)

    made = _ok(member.post(MERCHANT, json=_body(row, target)), 201)
    rule = made["rule"]
    assert rule["name"] == "Corner Grocer: Groceries"
    assert rule["conditions"] == {"type": "merchant", "key": KEY}
    assert rule["actions"] == [{"type": "set_category", "category_id": str(target.id)}]
    assert (rule["enabled"], rule["stop_processing"], rule["sort_order"]) == (True, False, 8)
    assert (made["replaced"], made["previous_category_id"], made["job_id"]) == (False, None, None)
    assert [r.id for r in _rules(factories, hh)] == [general.id, uuid.UUID(rule["id"])]
    # The preview now names it.
    seen = _ok(member.post(PREVIEW, json=_body(row, target)))
    assert seen["existing_rule_id"] == rule["id"]

    # The row it was made from is the person's own choice, not the rule's.
    after = _row(factories, hh, row.id)
    assert _provenance(after) == (target.id, "person", None, Decimal("1.0000"))
    assert after.updated_at > row.updated_at
    assert decided == [row.id]

    [created] = household_audit(factories, hh, "rule.create")
    assert created.target_id == rule["id"]
    assert created.after is not None
    assert created.after["origin"] == "merchant"
    assert created.after["references"] == {"category": [str(target.id)]}
    # Ids and types only: neither the merchant nor the rule's name (which holds it).
    assert "name" not in created.after
    assert "grocer" not in str(created.after).lower()
    assert household_events(factories, hh, "rule.created")[-1].payload == {"id": rule["id"]}


def test_create_without_apply_changes_no_other_row(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    account = ledger.account(hh)
    target = ledger.category(hh, name="Groceries")
    other = ledger.category(hh, name="Eating out")
    row = _grocer(ledger, account)
    others = [
        _grocer(ledger, account),
        _grocer(ledger, account, suggested_category_id=other.id),
        _grocer(ledger, account, category=other, category_source="dictionary"),
    ]
    before = [_state(factories, hh, r.id) for r in others]

    made = _ok(owner.post(MERCHANT, json=_body(row, target)), 201)
    assert made["job_id"] is None
    assert [_state(factories, hh, r.id) for r in others] == before
    assert _jobs(factories, hh, RUN_RULES_JOB_TYPE) == []
    assert household_audit(factories, hh, "rule.apply_requested") == []


def test_create_with_apply_changes_undecided_and_guessed_rows_and_never_a_person_row(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    member = actors.new(role="member", household=owner.record.household)
    mine = ledger.account(hh)
    readable = ledger.account(hh)
    ledger.grant(hh, member.user.id, mine, GrantLevel.EDIT)
    ledger.grant(hh, member.user.id, readable, GrantLevel.VIEW)
    target = ledger.category(hh, name="Groceries")
    other = ledger.category(hh, name="Eating out")
    # Another rule that decided a row long ago and matches nothing here.
    old_rule = ledger.rule(
        hh,
        conditions={"type": "payee", "op": "contains", "value": "no such descriptor"},
        actions=[{"type": "set_category", "category_id": str(other.id)}],
    )
    source = _grocer(ledger, mine)
    changes = [
        _grocer(ledger, mine),
        _grocer(ledger, mine, suggested_category_id=other.id),
        _grocer(ledger, mine, category=other, category_source="dictionary"),
        _grocer(ledger, mine, category=other, category_source="learned"),
        _grocer(ledger, mine, category=other, category_source="keyword"),
    ]
    stays = [
        _grocer(ledger, mine, category=other, category_source="person"),
        _grocer(ledger, mine, category_source="person"),  # cleared by a person
        _grocer(ledger, mine, category=other, category_source="legacy"),
        _grocer(ledger, mine, category=other, category_source="file"),
        _grocer(ledger, mine, category=other, category_source="payee"),
        _grocer(ledger, mine, category=other, category_source="rule", category_rule_id=old_rule.id),
        _grocer(ledger, readable),  # an account the member may not edit
        ledger.transaction(mine, raw_payee="CORNER GROCER DELI", merchant_key="corner grocer deli"),
    ]
    before = [_state(factories, hh, r.id) for r in stays]

    seen = _ok(member.post(PREVIEW, json=_body(source, target)))
    assert (seen["uncategorised"], seen["guessed"], seen["decided"]) == (3, 3, 6)

    made = _ok(member.post(MERCHANT, json=_body(source, target, apply_to_existing=True)), 201)
    rule_id = uuid.UUID(made["rule"]["id"])
    assert made["job_id"] is not None
    [requested] = household_audit(factories, hh, "rule.apply_requested")
    assert requested.target_id == made["job_id"]
    payload = _run_job(factories, hh, made["job_id"])
    assert payload.rule_ids == [rule_id]
    assert payload.account_ids == [mine.id]
    assert payload.requested_by == member.user.id

    for row in changes:
        after = _row(factories, hh, row.id)
        assert _provenance(after) == (target.id, "rule", rule_id, Decimal("1.0000"))
        assert after.suggested_category_id is None
    # What the preview called decided, and every row out of reach, is as it was.
    assert [_state(factories, hh, r.id) for r in stays] == before
    assert _provenance(_row(factories, hh, source.id))[:2] == (target.id, "person")
    # The preview's promise, kept: the source row became the person's own in between.
    [applied] = household_audit(factories, hh, "rule.apply")
    assert applied.after is not None
    assert applied.after["changed"] == seen["uncategorised"] + seen["guessed"] - 1 == 5

    # Negative: asked for by an admin with every rule, the job is the general
    # one again and decides what a file, a payee or an earlier release decided
    # (and still never what a person did).
    with factories.transaction() as db:
        set_context(db, hh)
        again = service.run_rules(db, hh, RunRulesJob())
    assert set(again.changed_ids) == {
        stays[2].id,
        stays[3].id,
        stays[4].id,
        stays[5].id,
        stays[6].id,
    }
    assert [_state(factories, hh, r.id) for r in stays[:2]] == before[:2]


def test_a_second_create_for_the_key_replaces_the_category(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    account = ledger.account(hh)
    first = ledger.category(hh, name="Groceries")
    second = ledger.category(hh, name="Eating out")
    row = _grocer(ledger, account)
    again = _grocer(ledger, account)

    made = _ok(owner.post(MERCHANT, json=_body(row, first)), 201)
    replaced = _ok(owner.post(MERCHANT, json=_body(again, second)), 201)
    assert replaced["rule"]["id"] == made["rule"]["id"]
    assert (replaced["replaced"], replaced["previous_category_id"]) == (True, str(first.id))
    assert replaced["rule"]["actions"] == [{"type": "set_category", "category_id": str(second.id)}]
    assert replaced["rule"]["name"] == "Corner Grocer: Eating out"
    assert replaced["rule"]["sort_order"] == made["rule"]["sort_order"]
    assert len(_rules(factories, hh)) == 1
    assert _provenance(_row(factories, hh, again.id))[:2] == (second.id, "person")
    # Without "apply to existing" the row the first rule was made from is not followed.
    assert _provenance(_row(factories, hh, row.id))[:2] == (first.id, "person")
    [updated] = household_audit(factories, hh, "rule.update")
    assert updated.before is not None
    assert updated.after is not None
    assert updated.before["references"] == {"category": [str(first.id)]}
    assert updated.after["references"] == {"category": [str(second.id)]}
    assert "grocer" not in (str(updated.before) + str(updated.after)).lower()
    assert len(household_audit(factories, hh, "rule.create")) == 1

    # Saving what the rule already says changes nothing.
    same = _ok(owner.post(MERCHANT, json=_body(again, second)), 201)
    assert (same["replaced"], same["previous_category_id"]) == (False, None)
    assert len(household_audit(factories, hh, "rule.update")) == 1


def test_an_earlier_rule_that_already_decides_the_row_refuses_with_its_id(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    account = ledger.account(hh)
    target = ledger.category(hh, name="Groceries")
    other = ledger.category(hh, name="Eating out")
    tag = ledger.tag(hh)
    row = _grocer(ledger, account, category=target, category_source="person")
    # Negatives: a rule that matches and only tags, and one that sets a category
    # for other rows, do not decide this one.
    ledger.rule(hh, sort_order=0, actions=[{"type": "add_tags", "tag_ids": [str(tag.id)]}])
    ledger.rule(
        hh,
        sort_order=1,
        conditions={"type": "payee", "op": "contains", "value": "harbour bakery"},
        actions=[{"type": "set_category", "category_id": str(other.id)}],
    )
    assert _ok(owner.post(PREVIEW, json=_body(row, target)))["shadowed_by"] is None

    earlier = ledger.rule(
        hh,
        sort_order=2,
        conditions={"type": "payee", "op": "contains", "value": "corner grocer"},
        actions=[{"type": "set_category", "category_id": str(other.id)}],
    )
    ledger.rule(
        hh,
        sort_order=3,
        conditions={"type": "payee", "op": "contains", "value": "grocer"},
        actions=[{"type": "set_category", "category_id": str(target.id)}],
    )
    seen = _ok(owner.post(PREVIEW, json=_body(row, target)))
    assert seen["shadowed_by"] == str(earlier.id)  # the first of the two
    assert seen["teachable"] is True

    before = _state(factories, hh, row.id)
    refused = owner.post(MERCHANT, json=_body(row, target, apply_to_existing=True))
    assert refused.status_code == 409, refused.text
    assert problem_type(refused) == "rule-shadowed"
    assert str(earlier.id) in refused.json()["detail"]
    assert refused.json()["rule_id"] == str(earlier.id)
    assert len(_rules(factories, hh)) == 4
    assert _state(factories, hh, row.id) == before
    assert _jobs(factories, hh, RUN_RULES_JOB_TYPE) == []


def test_a_rule_that_stops_the_run_before_the_merchant_rule_also_refuses(
    actors: Actors, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    account = ledger.account(hh)
    target = ledger.category(hh, name="Groceries")
    tag = ledger.tag(hh)
    row = _grocer(ledger, account)
    stopper = ledger.rule(
        hh,
        conditions={"type": "payee", "op": "contains", "value": "corner grocer"},
        actions=[{"type": "add_tags", "tag_ids": [str(tag.id)]}],
        stop_processing=True,
    )
    assert _ok(owner.post(PREVIEW, json=_body(row, target)))["shadowed_by"] == str(stopper.id)
    refused = owner.post(MERCHANT, json=_body(row, target))
    assert refused.status_code == 409
    assert problem_type(refused) == "rule-shadowed"


# ---- revert --------------------------------------------------------------------------


def test_revert_returns_exactly_the_rows_the_rule_set_to_undecided(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    member = actors.new(role="member", household=owner.record.household)
    mine = ledger.account(hh)
    ledger.grant(hh, member.user.id, mine, GrantLevel.EDIT)
    target = ledger.category(hh, name="Groceries")
    other = ledger.category(hh, name="Eating out")
    source = _grocer(ledger, mine)
    set_by_rule = [_grocer(ledger, mine) for _ in range(3)]
    legacy = _grocer(ledger, mine, category=other, category_source="legacy")

    made = _ok(member.post(MERCHANT, json=_body(source, target, apply_to_existing=True)), 201)
    rule_id = made["rule"]["id"]
    _run_job(factories, hh, made["job_id"])
    # Afterwards a person re-categorises one of the rows the rule had set.
    with factories.transaction() as db:
        set_context(db, hh)
        mind_changed = db.get(Transaction, set_by_rule[2].id)
        assert mind_changed is not None
        mind_changed.category_id = other.id
        mind_changed.category_source = "person"
        mind_changed.category_rule_id = None
    keeps = [source, set_by_rule[2], legacy]
    before = [_state(factories, hh, r.id) for r in keeps]
    stamps = [_row(factories, hh, r.id).updated_at for r in set_by_rule[:2]]

    reverted = _ok(member.post(f"{RULES}/{rule_id}/revert"))
    assert (reverted["rule_id"], reverted["reverted"]) == (rule_id, 2)
    assert owner.get(f"{RULES}/{rule_id}").status_code == 404
    for row, stamp in zip(set_by_rule[:2], stamps, strict=True):
        after = _row(factories, hh, row.id)
        assert _provenance(after) == (None, None, None, None)
        assert after.suggested_category_id is None
        assert after.updated_at == stamp  # the system undid a system write
    assert [_state(factories, hh, r.id) for r in keeps] == before

    # The engine is asked to guess again for the household.
    [job] = _jobs(factories, hh, REFRESH_JOB_TYPE)
    assert reverted["job_id"] == str(job.id)
    assert job.payload["force"] is True
    [audited] = household_audit(factories, hh, "rule.revert")
    assert audited.target_id == rule_id
    assert audited.after == {"reverted": 2}
    assert "grocer" not in str(audited.before).lower()
    assert household_events(factories, hh, "rule.deleted")[-1].payload == {"id": rule_id}
    changed = household_events(factories, hh, "transaction.rules_applied")[-1].payload
    assert (changed["changed"], changed["account_ids"]) == (2, [str(mine.id)])


def test_revert_of_a_rule_that_set_nothing_queues_no_refresh(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    account = ledger.account(hh)
    target = ledger.category(hh, name="Groceries")
    row = _grocer(ledger, account)
    made = _ok(owner.post(MERCHANT, json=_body(row, target)), 201)

    reverted = _ok(owner.post(f"{RULES}/{made['rule']['id']}/revert"))
    assert (reverted["reverted"], reverted["job_id"]) == (0, None)
    assert _jobs(factories, hh, REFRESH_JOB_TYPE) == []
    # The row the rule was made from was the person's and stays so.
    assert _provenance(_row(factories, hh, row.id))[:2] == (target.id, "person")


def test_replacing_back_with_the_previous_category_restores_the_rule(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    account = ledger.account(hh)
    first = ledger.category(hh, name="Groceries")
    second = ledger.category(hh, name="Eating out")
    row = _grocer(ledger, account)

    made = _ok(owner.post(MERCHANT, json=_body(row, first)), 201)
    changed = _ok(owner.post(MERCHANT, json=_body(row, second)), 201)
    undo = {"transaction_id": str(row.id), "category_id": changed["previous_category_id"]}
    restored = _ok(owner.post(MERCHANT, json=undo), 201)
    assert restored["rule"]["id"] == made["rule"]["id"]
    assert (restored["replaced"], restored["previous_category_id"]) == (True, str(second.id))
    assert restored["rule"]["actions"] == made["rule"]["actions"]
    assert restored["rule"]["name"] == made["rule"]["name"] == "Corner Grocer: Groceries"
    assert len(_rules(factories, hh)) == 1
    assert _provenance(_row(factories, hh, row.id))[:2] == (first.id, "person")


def test_a_member_cannot_revert_a_general_rule(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    member = actors.new(role="member", household=owner.record.household)
    account = ledger.account(hh)
    ledger.grant(hh, member.user.id, account, GrantLevel.EDIT)
    target = ledger.category(hh, name="Groceries")
    tag = ledger.tag(hh)
    general = ledger.rule(
        hh,
        conditions={"type": "payee", "op": "contains", "value": "corner grocer"},
        actions=[{"type": "set_category", "category_id": str(target.id)}],
    )
    # A rule on a merchant that also does something else is a general rule too.
    wider = ledger.rule(
        hh,
        sort_order=1,
        conditions={"type": "merchant", "key": KEY},
        actions=[
            {"type": "set_category", "category_id": str(target.id)},
            {"type": "add_tags", "tag_ids": [str(tag.id)]},
        ],
    )
    row = _grocer(
        ledger, account, category=target, category_source="rule", category_rule_id=general.id
    )

    for rule in (general, wider):
        refused = member.post(f"{RULES}/{rule.id}/revert")
        assert refused.status_code == 403, refused.text
        assert problem_type(refused) == "rule-not-revertible"
    assert len(_rules(factories, hh)) == 2
    assert _provenance(_row(factories, hh, row.id))[:3] == (target.id, "rule", general.id)

    # An owner may, and the rows that rule set are undecided again.
    reverted = _ok(owner.post(f"{RULES}/{general.id}/revert"))
    assert reverted["reverted"] == 1
    assert _provenance(_row(factories, hh, row.id)) == (None, None, None, None)
    assert [r.id for r in _rules(factories, hh)] == [wider.id]


def test_revert_removes_the_split_a_rule_made(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    account = ledger.account(hh)
    target = ledger.category(hh, name="Groceries")
    half = ledger.category(hh, name="Household")
    splitter = ledger.rule(hh)
    row = _grocer(
        ledger, account, amount="-20.00", category_source="rule", category_rule_id=splitter.id
    )
    ledger.split(row, amount="-12.00", category=target)
    ledger.split(row, amount="-8.00", category=half)

    assert _ok(owner.post(f"{RULES}/{splitter.id}/revert"))["reverted"] == 1
    after = _row(factories, hh, row.id)
    assert _provenance(after) == (None, None, None, None)
    assert after.updated_at == row.updated_at
    with factories.transaction() as db:
        set_context(db, hh)
        left = db.execute(
            select(TransactionSplit.id).where(TransactionSplit.transaction_id == row.id)
        ).all()
    assert left == []


# ---- who may -------------------------------------------------------------------------


def test_viewer_is_refused_and_a_foreign_rule_is_404(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    viewer = actors.new(role="viewer", household=owner.record.household)
    account = ledger.account(hh)
    ledger.grant(hh, viewer.user.id, account, GrantLevel.VIEW)
    target = ledger.category(hh, name="Groceries")
    row = _grocer(ledger, account)
    rule_id = _ok(owner.post(MERCHANT, json=_body(row, target)), 201)["rule"]["id"]

    for refused in (
        viewer.post(PREVIEW, json=_body(row, target)),
        viewer.post(MERCHANT, json=_body(row, target)),
        viewer.post(f"{RULES}/{rule_id}/revert"),
    ):
        assert refused.status_code == 403, refused.text

    stranger = actors.new(role="owner")
    theirs = _hid(stranger)
    their_account = ledger.account(theirs)
    their_category = ledger.category(theirs, name="Groceries")
    their_row = ledger.transaction(their_account, raw_payee=RAW, merchant_key=KEY)
    gone = stranger.post(f"{RULES}/{rule_id}/revert")
    assert gone.status_code == 404
    assert problem_type(gone) == "rule-not-found"
    for url in (PREVIEW, MERCHANT):
        hidden = stranger.post(url, json=_body(row, their_category))
        assert hidden.status_code == 404
        assert problem_type(hidden) == "transaction-not-found"
        foreign = stranger.post(url, json=_body(their_row, target))
        assert foreign.status_code == 404
        assert problem_type(foreign) == "category-not-found"
    # Their own household's merchant of the same name is none of this one's business.
    assert _ok(stranger.post(PREVIEW, json=_body(their_row, their_category)))["uncategorised"] == 1
    assert [str(r.id) for r in _rules(factories, hh)] == [rule_id]
    assert _rules(factories, theirs) == []


def test_a_member_who_may_only_read_the_row_makes_the_rule_and_leaves_the_row(
    actors: Actors, factories: Factories, ledger: LedgerFactories, decided: list[uuid.UUID]
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    member = actors.new(role="member", household=owner.record.household)
    readable = ledger.account(hh)
    ledger.grant(hh, member.user.id, readable, GrantLevel.VIEW)
    target = ledger.category(hh, name="Groceries")
    row = _grocer(ledger, readable)
    before = _state(factories, hh, row.id)

    made = _ok(member.post(MERCHANT, json=_body(row, target, apply_to_existing=True)), 201)
    # Nothing they may edit has this merchant: no job, and the row is not theirs to change.
    assert made["job_id"] is None
    assert _state(factories, hh, row.id) == before
    assert decided == []
    assert len(_rules(factories, hh)) == 1


def test_the_rule_cap_applies(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    account = ledger.account(hh)
    target = ledger.category(hh, name="Groceries")
    other = ledger.category(hh, name="Eating out")
    row = _grocer(ledger, account)
    bakery = ledger.transaction(account, raw_payee="HARBOUR BAKERY", merchant_key="harbour bakery")

    def fill(db: Session) -> None:
        # Disabled: they only count.
        for index in range(RULES_MAX - 1):
            make_rule(db, hh, sort_order=index, enabled=False)

    ledger.run(fill)
    _ok(owner.post(MERCHANT, json=_body(row, target)), 201)  # the 500th
    full = owner.post(MERCHANT, json=_body(bakery, target))
    assert full.status_code == 409, full.text
    assert problem_type(full) == "rule-limit"
    assert _provenance(_row(factories, hh, bakery.id)) == (None, None, None, None)
    # Changing the rule a merchant already has adds none, so the cap does not refuse it.
    assert _ok(owner.post(MERCHANT, json=_body(row, other)), 201)["replaced"] is True
    assert len(_rules(factories, hh)) == RULES_MAX

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Categories, payees and tags over HTTP (API.md; ``app.domain.taxonomy.service``).

Behaviour (tree rules, merges with reassignment, payee aliases), role rules
(viewer reads, member creates and changes, owner/admin delete and merge),
cross-household ``404`` and mass assignment.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.budgets.models import BudgetLine
from app.domain.transactions.models import (
    Category,
    Payee,
    ScheduledTransaction,
    Transaction,
    TransactionSplit,
    TransactionTag,
)
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import (
    API,
    Actors,
    household_audit,
    household_events,
    problem_type,
)
from tests.fixtures.ledger import (
    LedgerFactories,
    make_scheduled_transaction,
    make_split,
    tag_transaction,
)

pytestmark = pytest.mark.integration

CATEGORIES = f"{API}/categories"
PAYEES = f"{API}/payees"
TAGS = f"{API}/tags"


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


def _hid(client: AuthedClient) -> uuid.UUID:
    household = client.record.household
    assert household is not None
    return household.id


def _post(client: AuthedClient, url: str, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(url, json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def _scalar(factories: Factories, household_id: uuid.UUID, stmt: Any) -> Any:
    with factories.transaction() as db:
        set_context(db, household_id)
        return db.execute(stmt).scalar_one_or_none()


# ---- categories --------------------------------------------------------------------


def test_category_tree_rules(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    household_id = _hid(owner)
    income = _post(owner, CATEGORIES, {"name": "Side income", "kind": "income"})
    child = _post(owner, CATEGORIES, {"name": "Tutoring", "parent_id": income["id"]})
    assert child["kind"] == "income"  # inherited
    mismatch = owner.post(
        CATEGORIES, json={"name": "X", "parent_id": income["id"], "kind": "expense"}
    )
    assert (mismatch.status_code, problem_type(mismatch)) == (422, "category-kind-mismatch")
    grandchild = _post(owner, CATEGORIES, {"name": "Maths", "parent_id": child["id"]})
    too_deep = owner.post(CATEGORIES, json={"name": "Algebra", "parent_id": grandchild["id"]})
    assert (too_deep.status_code, problem_type(too_deep)) == (422, "category-too-deep")
    duplicate = owner.post(CATEGORIES, json={"name": "tutoring", "parent_id": income["id"]})
    assert (duplicate.status_code, problem_type(duplicate)) == (409, "category-name-taken")
    assert _post(owner, CATEGORIES, {"name": "Tutoring"})["parent_id"] is None  # other level

    # Moves: no cycles, depth respected, kinds follow the new parent.
    cycle = owner.patch(f"{CATEGORIES}/{income['id']}", json={"parent_id": grandchild["id"]})
    assert (cycle.status_code, problem_type(cycle)) == (422, "category-cycle")
    self_parent = owner.patch(f"{CATEGORIES}/{income['id']}", json={"parent_id": income["id"]})
    assert problem_type(self_parent) == "category-cycle"
    expense = _post(owner, CATEGORIES, {"name": "Lessons"})
    # Income has three levels: under another category it would have four.
    deep_move = owner.patch(f"{CATEGORIES}/{income['id']}", json={"parent_id": expense["id"]})
    assert problem_type(deep_move) == "category-too-deep"
    moved = owner.patch(f"{CATEGORIES}/{grandchild['id']}", json={"parent_id": expense["id"]})
    assert moved.status_code == 200, moved.text
    assert (moved.json()["parent_id"], moved.json()["kind"]) == (expense["id"], "expense")
    top = owner.patch(f"{CATEGORIES}/{grandchild['id']}", json={"parent_id": None})
    assert top.json()["parent_id"] is None

    # A top-level kind change cascades to descendants.
    owner.patch(f"{CATEGORIES}/{income['id']}", json={"kind": "transfer"})
    assert owner.get(f"{CATEGORIES}/{child['id']}").json()["kind"] == "transfer"
    child_kind = owner.patch(f"{CATEGORIES}/{child['id']}", json={"kind": "income"})
    assert problem_type(child_kind) == "category-kind-mismatch"

    renamed = owner.patch(
        f"{CATEGORIES}/{child['id']}", json={"name": "Teaching", "tax_treatment": "taxable_income"}
    )
    assert renamed.json()["name"] == "Teaching"
    assert renamed.json()["tax_treatment"] == "taxable_income"
    assert len(household_audit(factories, household_id, "category.create")) == 5
    assert household_events(factories, household_id, "category.changed")


def test_category_delete_rules(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _hid(owner)
    parent = ledger.category(household_id)
    child = ledger.category(household_id, parent=parent)
    account = ledger.account(household_id)
    txn = ledger.transaction(account, category=child)
    blocked = owner.delete(f"{CATEGORIES}/{parent.id}")
    assert (blocked.status_code, problem_type(blocked)) == (409, "category-has-children")
    assert owner.delete(f"{CATEGORIES}/{child.id}").status_code == 204
    assert (
        _scalar(
            factories, household_id, select(Transaction.category_id).where(Transaction.id == txn.id)
        )
        is None
    )
    assert owner.delete(f"{CATEGORIES}/{parent.id}").status_code == 204
    assert owner.get(f"{CATEGORIES}/{parent.id}").status_code == 404
    assert len(household_audit(factories, household_id, "category.delete")) == 2


def test_category_merge_reassigns_everything(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _hid(owner)
    source = ledger.category(household_id, name="Eating out")
    target = ledger.category(household_id, name="Restaurants")
    source_child = ledger.category(household_id, name="Lunch", parent=source)
    account = ledger.account(household_id)
    txn = ledger.transaction(account, category=source)
    split_txn = ledger.transaction(account, amount="-30.00")
    split = ledger.run(make_split, split_txn, amount="-30.00", category=source)
    payee = ledger.payee(household_id, default_category=source)
    schedule = ledger.run(make_scheduled_transaction, account, category=source)
    both = ledger.budget(household_id, lines={source.id: "50", target.id: "100"})
    only_source = ledger.budget(household_id, lines={source.id: "25"})

    merged = owner.post(f"{CATEGORIES}/{source.id}/merge", json={"into": str(target.id)})
    assert merged.status_code == 200, merged.text
    assert merged.json()["id"] == str(target.id)
    assert owner.get(f"{CATEGORIES}/{source.id}").status_code == 404

    def value(stmt: Any) -> Any:
        return _scalar(factories, household_id, stmt)

    assert value(select(Transaction.category_id).where(Transaction.id == txn.id)) == target.id
    assert (
        value(select(TransactionSplit.category_id).where(TransactionSplit.id == split.id))
        == target.id
    )
    assert value(select(Payee.default_category_id).where(Payee.id == payee.id)) == target.id
    assert (
        value(
            select(ScheduledTransaction.category_id).where(ScheduledTransaction.id == schedule.id)
        )
        == target.id
    )
    assert value(select(Category.parent_id).where(Category.id == source_child.id)) == target.id
    with factories.transaction() as db:
        set_context(db, household_id)
        lines = {
            (line.budget_id, line.category_id): line.amount
            for line in db.execute(select(BudgetLine)).scalars()
        }
    assert lines == {
        (both.id, target.id): Decimal("150.000000"),
        (only_source.id, target.id): Decimal("25.000000"),
    }
    audit = household_audit(factories, household_id, "category.merge")
    assert len(audit) == 1
    assert audit[0].after is not None
    assert audit[0].after["moved"]["transactions"] == 1


def test_category_merge_refusals(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    household_id = _hid(owner)
    parent = ledger.category(household_id)
    child = ledger.category(household_id, parent=parent)
    income = ledger.category(household_id, kind="income")
    into_child = owner.post(f"{CATEGORIES}/{parent.id}/merge", json={"into": str(child.id)})
    assert (into_child.status_code, problem_type(into_child)) == (422, "category-merge-invalid")
    into_self = owner.post(f"{CATEGORIES}/{parent.id}/merge", json={"into": str(parent.id)})
    assert problem_type(into_self) == "category-merge-invalid"
    kinds = owner.post(f"{CATEGORIES}/{child.id}/merge", json={"into": str(income.id)})
    assert problem_type(kinds) == "category-kind-mismatch"
    missing = owner.post(f"{CATEGORIES}/{child.id}/merge", json={"into": str(uuid.uuid4())})
    assert (missing.status_code, problem_type(missing)) == (404, "category-not-found")
    # Children named like the target's children would clash.
    other = ledger.category(household_id)
    ledger.category(household_id, name=child.name, parent=other)
    clash = owner.post(f"{CATEGORIES}/{parent.id}/merge", json={"into": str(other.id)})
    assert (clash.status_code, problem_type(clash)) == (409, "category-name-taken")


def test_default_categories_are_listed_and_paged(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    household_id = _hid(owner)
    seeded = ledger.seed_categories(household_id)
    page = owner.get(CATEGORIES).json()
    assert len(page["items"]) == seeded
    assert page["next_cursor"] is None
    assert {c["system_key"] for c in page["items"]} >= {"food", "food.groceries"}
    first = owner.get(CATEGORIES, params={"limit": 10}).json()
    second = owner.get(CATEGORIES, params={"limit": 10, "cursor": first["next_cursor"]}).json()
    assert len(first["items"]) == 10
    assert not {c["id"] for c in first["items"]} & {c["id"] for c in second["items"]}
    incomes = owner.get(CATEGORIES, params={"kind": "income"}).json()["items"]
    assert incomes
    assert {c["kind"] for c in incomes} == {"income"}


# ---- payees ------------------------------------------------------------------------


def test_payee_names_and_aliases(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    household_id = _hid(owner)
    category = ledger.category(household_id)
    created = _post(
        owner,
        PAYEES,
        {
            "name": "Corner Grocer",
            "aliases": ["CORNER GROCER #12", "corner-grocer #12", "Corner Grocer"],
            "default_category_id": str(category.id),
        },
    )
    assert created["normalized_name"] == "corner grocer"
    assert created["aliases"] == ["CORNER GROCER #12"]  # deduplicated, own name dropped
    assert created["default_category_id"] == str(category.id)

    same = owner.post(PAYEES, json={"name": "corner  GROCER"})
    assert (same.status_code, problem_type(same)) == (409, "payee-exists")
    alias_clash = owner.post(PAYEES, json={"name": "Other", "aliases": ["Corner grocer 12"]})
    assert (alias_clash.status_code, problem_type(alias_clash)) == (409, "payee-alias-taken")
    name_is_alias = owner.post(PAYEES, json={"name": "corner grocer 12"})
    assert problem_type(name_is_alias) == "payee-alias-taken"
    blank = owner.post(PAYEES, json={"name": "!!!"})
    assert (blank.status_code, problem_type(blank)) == (422, "invalid-payee-name")
    too_many = owner.post(PAYEES, json={"name": "Many", "aliases": [f"a{i}" for i in range(101)]})
    assert too_many.status_code == 422
    foreign_category = owner.post(
        PAYEES, json={"name": "Bakery", "default_category_id": str(uuid.uuid4())}
    )
    assert (foreign_category.status_code, problem_type(foreign_category)) == (
        404,
        "category-not-found",
    )

    updated = owner.patch(
        f"{PAYEES}/{created['id']}", json={"aliases": ["GROCER 12"], "default_category_id": None}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["aliases"] == ["GROCER 12"]
    assert updated.json()["default_category_id"] is None
    _post(owner, PAYEES, {"name": "Fuel Stop"})
    found = owner.get(PAYEES, params={"q": "GROCER"}).json()["items"]
    assert [p["id"] for p in found] == [created["id"]]
    assert owner.get(PAYEES, params={"q": "x" * 101}).status_code == 422


def test_payee_merge_and_delete(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _hid(owner)
    category = ledger.category(household_id)
    source = ledger.payee(household_id, name="Coffee Cart", aliases=["COFFEE CART 7"])
    target = ledger.payee(household_id, name="Coffee Shop")
    source_cat = ledger.payee(household_id, name="Cafe", default_category=category)
    account = ledger.account(household_id)
    txn = ledger.transaction(account, payee=source)
    schedule = ledger.run(make_scheduled_transaction, account, payee=source)

    merged = owner.post(f"{PAYEES}/{source.id}/merge", json={"into": str(target.id)})
    assert merged.status_code == 200, merged.text
    assert merged.json()["aliases"] == ["Coffee Cart", "COFFEE CART 7"]
    assert owner.get(f"{PAYEES}/{source.id}").status_code == 404
    assert (
        _scalar(
            factories, household_id, select(Transaction.payee_id).where(Transaction.id == txn.id)
        )
        == target.id
    )
    assert (
        _scalar(
            factories,
            household_id,
            select(ScheduledTransaction.payee_id).where(ScheduledTransaction.id == schedule.id),
        )
        == target.id
    )
    # The target takes the source's default category when it has none.
    again = owner.post(f"{PAYEES}/{source_cat.id}/merge", json={"into": str(target.id)})
    assert again.json()["default_category_id"] == str(category.id)
    itself = owner.post(f"{PAYEES}/{target.id}/merge", json={"into": str(target.id)})
    assert (itself.status_code, problem_type(itself)) == (422, "payee-merge-invalid")

    assert owner.delete(f"{PAYEES}/{target.id}").status_code == 204
    assert (
        _scalar(
            factories, household_id, select(Transaction.payee_id).where(Transaction.id == txn.id)
        )
        is None
    )
    assert len(household_audit(factories, household_id, "payee.merge")) == 2


# ---- tags --------------------------------------------------------------------------


def test_tags(actors: Actors, factories: Factories, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    household_id = _hid(owner)
    created = _post(owner, TAGS, {"name": "Holiday 2026"})
    dup = owner.post(TAGS, json={"name": "holiday 2026"})
    assert (dup.status_code, problem_type(dup)) == (409, "tag-exists")
    other = _post(owner, TAGS, {"name": "Work"})
    clash = owner.patch(f"{TAGS}/{other['id']}", json={"name": "HOLIDAY 2026"})
    assert problem_type(clash) == "tag-exists"
    renamed = owner.patch(f"{TAGS}/{created['id']}", json={"name": "Trip"})
    assert renamed.json()["name"] == "Trip"
    page = owner.get(TAGS, params={"limit": 1}).json()
    assert [t["id"] for t in page["items"]] == [created["id"]]
    rest = owner.get(TAGS, params={"limit": 1, "cursor": page["next_cursor"]}).json()
    assert [t["id"] for t in rest["items"]] == [other["id"]]
    assert rest["next_cursor"] is None

    account = ledger.account(household_id)
    txn = ledger.transaction(account)
    tag_row = ledger.tag(household_id)
    ledger.run(tag_transaction, txn, tag_row)
    assert owner.delete(f"{TAGS}/{tag_row.id}").status_code == 204
    assert (
        _scalar(
            factories,
            household_id,
            select(TransactionTag.id).where(TransactionTag.transaction_id == txn.id),
        )
        is None
    )
    assert len(household_audit(factories, household_id, "tag.delete")) == 1


# ---- roles, households, mass assignment ------------------------------------------


def test_roles(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    member = actors.new(role="member", household=household)
    viewer = actors.new(role="viewer", household=household)
    category = ledger.category(household.id)
    target = ledger.category(household.id)
    payee = ledger.payee(household.id)
    tag = ledger.tag(household.id)

    for url in (CATEGORIES, PAYEES, TAGS):
        assert viewer.get(url).status_code == 200
        assert viewer.post(url, json={"name": "Viewer"}).status_code == 403
        assert member.post(url, json={"name": "Member"}).status_code == 201
    assert viewer.get(f"{CATEGORIES}/{category.id}").status_code == 200
    assert viewer.patch(f"{TAGS}/{tag.id}", json={"name": "x"}).status_code == 403
    assert member.patch(f"{TAGS}/{tag.id}", json={"name": "renamed"}).status_code == 200
    for path in (f"{CATEGORIES}/{category.id}", f"{PAYEES}/{payee.id}", f"{TAGS}/{tag.id}"):
        assert member.delete(path).status_code == 403
    merge = member.post(f"{CATEGORIES}/{category.id}/merge", json={"into": str(target.id)})
    assert merge.status_code == 403
    admin = actors.new(role="admin", household=household)
    assert admin.delete(f"{TAGS}/{tag.id}").status_code == 204


def test_other_household_objects_are_404(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    victim = actors.new(role="owner")
    victim_household = _hid(victim)
    category = ledger.category(victim_household, name="Private")
    payee = ledger.payee(victim_household, name="Private payee")
    tag = ledger.tag(victim_household, name="private")
    intruder = actors.new(role="owner")
    own_category = ledger.category(_hid(intruder))
    for path, kind in (
        (f"{CATEGORIES}/{category.id}", "category-not-found"),
        (f"{PAYEES}/{payee.id}", "payee-not-found"),
        (f"{TAGS}/{tag.id}", "tag-not-found"),
    ):
        for method, body in (("GET", None), ("PATCH", {"name": "Mine"}), ("DELETE", None)):
            response = intruder.request(method, path, json=body)
            assert (response.status_code, problem_type(response)) == (404, kind), (method, path)
    # References to another household's objects in a body are 404 too.
    child = intruder.post(CATEGORIES, json={"name": "Sub", "parent_id": str(category.id)})
    assert (child.status_code, problem_type(child)) == (404, "category-not-found")
    stolen = intruder.post(f"{CATEGORIES}/{own_category.id}/merge", json={"into": str(category.id)})
    assert problem_type(stolen) == "category-not-found"
    for url in (CATEGORIES, PAYEES, TAGS):
        names = [item["name"] for item in intruder.get(url).json()["items"]]
        assert not {"Private", "Private payee", "private"} & set(names)
    assert victim.get(f"{CATEGORIES}/{category.id}").json()["name"] == "Private"
    assert victim.get(f"{TAGS}/{tag.id}").json()["name"] == "private"


@pytest.mark.parametrize(
    ("url", "body"),
    [
        (CATEGORIES, {"name": "X", "system_key": "food"}),
        (CATEGORIES, {"name": "X", "household_id": str(uuid.uuid4())}),
        (PAYEES, {"name": "X", "normalized_name": "y"}),
        (PAYEES, {"name": "X", "id": str(uuid.uuid4())}),
        (TAGS, {"name": "X", "created_at": "2026-01-01T00:00:00Z"}),
    ],
)
def test_mass_assignment_is_refused(actors: Actors, url: str, body: dict[str, Any]) -> None:
    owner = actors.new(role="owner")
    response = owner.post(url, json=body)
    assert response.status_code == 422, response.text
    assert owner.get(url).json()["items"] == []


def test_patch_shapes(actors: Actors, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    household_id = _hid(owner)
    category = ledger.category(household_id)
    payee = ledger.payee(household_id)
    for url, body in (
        (f"{CATEGORIES}/{category.id}", {}),
        (f"{CATEGORIES}/{category.id}", {"name": None}),
        (f"{CATEGORIES}/{category.id}", {"kind": None}),
        (f"{CATEGORIES}/{category.id}", {"system_key": "x"}),
        (f"{PAYEES}/{payee.id}", {"aliases": None}),
        (f"{PAYEES}/{payee.id}", {"normalized_name": "x"}),
    ):
        assert owner.patch(url, json=body).status_code == 422, (url, body)


def test_idempotent_creates(actors: Actors) -> None:
    owner = actors.new(role="owner")
    for url in (CATEGORIES, PAYEES, TAGS):
        key = {"Idempotency-Key": f"create-{url}"}
        first = owner.post(url, json={"name": "Once"}, headers=key)
        second = owner.post(url, json={"name": "Once"}, headers=key)
        assert first.status_code == second.status_code == 201
        assert first.json() == second.json()
        assert second.headers["Idempotent-Replayed"] == "true"
        assert len(owner.get(url).json()["items"]) == 1

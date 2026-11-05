# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure parts of the ledger API: idempotency hashing, category tree, input schemas."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.errors import ProblemError
from app.core.idempotency import check_key, payload_of, request_hash
from app.domain.accounts.schemas import AccountCreate, AccountUpdate, ManualBalanceIn
from app.domain.taxonomy import service as taxonomy
from app.domain.taxonomy.repo import CategoryNode
from app.domain.taxonomy.schemas import CategoryUpdate, PayeeCreate, PayeeUpdate
from app.domain.transactions.models import CategoryKind

E = CategoryKind.EXPENSE


def test_request_hash_is_canonical_and_scoped() -> None:
    a = request_hash("POST /x", {"b": 1, "a": [1, 2]})
    assert a == request_hash("POST /x", {"a": [1, 2], "b": 1})
    assert a != request_hash("POST /y", {"a": [1, 2], "b": 1})
    assert a != request_hash("POST /x", {"a": [2, 1], "b": 1})
    account = uuid.uuid4()
    assert payload_of(None, account_id=account) == {
        "path": {"account_id": str(account)},
        "body": None,
    }


@pytest.mark.parametrize("key", ["a", "x" * 255, "0f1e-uuid-like", "!~"])
def test_good_keys(key: str) -> None:
    assert check_key(key) == key


@pytest.mark.parametrize("key", ["", "x" * 256, "with space", "tab\t", "café", "\x7f"])
def test_bad_keys(key: str) -> None:
    with pytest.raises(ProblemError) as caught:
        check_key(key)
    assert caught.value.slug == "invalid-idempotency-key"


def _tree(*edges: tuple[int, int | None]) -> dict[uuid.UUID, CategoryNode]:
    ids = {n: uuid.UUID(int=n + 1) for n, _ in edges}
    return {
        ids[n]: CategoryNode(ids[n], None if parent is None else ids[parent], E)
        for n, parent in edges
    }


def test_category_tree_helpers() -> None:
    tree = _tree((0, None), (1, 0), (2, 1), (3, 0), (4, None))
    node = {n: uuid.UUID(int=n + 1) for n in range(5)}
    assert [taxonomy._depth(tree, node[n]) for n in range(5)] == [1, 2, 3, 2, 1]
    assert set(taxonomy._descendants(tree, node[0])) == {node[1], node[2], node[3]}
    assert taxonomy._descendants(tree, node[4]) == []
    assert taxonomy._height(tree, node[0]) == 3
    assert taxonomy._height(tree, node[1]) == 2
    assert taxonomy._height(tree, node[4]) == 1


def test_tree_helpers_survive_a_corrupt_cycle() -> None:
    a, b = uuid.UUID(int=1), uuid.UUID(int=2)
    tree = {a: CategoryNode(a, b, E), b: CategoryNode(b, a, E)}
    assert taxonomy._depth(tree, a) == 2
    assert set(taxonomy._descendants(tree, a)) == {a, b}


@pytest.mark.parametrize(
    "body",
    [
        {"type": "manual_asset", "name": "House"},
        {"type": "checking", "name": "A", "asset_kind": "other"},
        {"type": "checking", "name": "A", "number_last4": "123"},
        {"type": "checking", "name": "A", "number_last4": "12345"},
        {"type": "checking", "name": "A", "opening_balance": {"amount": "1e3", "currency": "USD"}},
        {"type": "checking", "name": "A", "opening_balance": {"amount": "1", "currency": "usdx"}},
        {"type": "checking", "name": "\u0000"},
        {"type": "checking", "name": "A", "extra": 1},
    ],
)
def test_account_create_refuses(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AccountCreate.model_validate(body)


def test_update_models_require_a_change_and_refuse_nulls() -> None:
    for model, body in (
        (AccountUpdate, {}),
        (AccountUpdate, {"is_asset": None}),
        (CategoryUpdate, {}),
        (CategoryUpdate, {"name": None}),
        (PayeeUpdate, {}),
        (PayeeUpdate, {"name": None}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(body)
    assert AccountUpdate.model_validate({"opened_on": None}).model_fields_set == {"opened_on"}
    assert CategoryUpdate.model_validate({"parent_id": None}).parent_id is None
    assert PayeeCreate.model_validate({"name": " Shop "}).name == "Shop"
    with pytest.raises(ValidationError):
        ManualBalanceIn.model_validate({"as_of_date": "2026-01-01", "balance": {"amount": 1.5}})

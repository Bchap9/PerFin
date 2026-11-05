# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure parts of the transactions API: cursor, search escaping, input models, matcher."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.errors import ProblemError
from app.domain.transactions.repo import LIKE_ESCAPE, like_pattern
from app.domain.transactions.schemas import (
    AutoPairRequest,
    BulkAction,
    SplitSet,
    TransactionCreate,
    TransactionUpdate,
    decode_cursor,
    encode_cursor,
)
from app.domain.transactions.transfers import _Edge, _match

ID = uuid.UUID("01900000-0000-7000-8000-000000000001")


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


def test_cursor_round_trip() -> None:
    token = encode_cursor(date(2026, 1, 31), ID)
    assert token.isascii()
    assert "=" not in token
    assert decode_cursor(token) == (date(2026, 1, 31), ID)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "!!!",
        "a" * 129,
        "AAAA",  # decodes to bytes that are no cursor
        encode_cursor(date(2026, 1, 1), ID)[:-3],  # truncated
        "MjAyNi0xMy0wMXwx",  # "2026-13-01|1"
        "w4k",  # non-ASCII payload
    ],
)
def test_bad_cursors_are_422(token: str) -> None:
    with pytest.raises(ProblemError) as exc:
        decode_cursor(token)
    assert (exc.value.status, exc.value.slug) == (422, "invalid-cursor")


# ---------------------------------------------------------------------------
# q escaping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        ("coffee", "%coffee%"),
        ("100%", "%100\\%%"),
        ("a_b", "%a\\_b%"),
        ("back\\slash", "%back\\\\slash%"),
        ("%_\\", "%\\%\\_\\\\%"),
    ],
)
def test_like_pattern_escapes_wildcards(text: str, pattern: str) -> None:
    assert LIKE_ESCAPE == "\\"
    assert like_pattern(text) == pattern


# ---------------------------------------------------------------------------
# Input models (F4)
# ---------------------------------------------------------------------------


def _create(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "account_id": str(ID),
        "posted_date": "2026-01-02",
        "amount": {"amount": "-1.00", "currency": "usd"},
    }
    body.update(extra)
    return body


def test_create_normalises_and_bounds() -> None:
    model = TransactionCreate.model_validate(_create(raw_payee="  Shop  ", memo="  "))
    assert model.amount.amount == Decimal("-1.000000")
    assert model.amount.currency == "USD"
    assert model.raw_payee == "Shop"
    assert model.memo is None


@pytest.mark.parametrize(
    "extra",
    [
        {"household_id": str(ID)},
        {"review_state": "confirmed"},
        {"currency": "USD"},
        {"amount": {"amount": 1.5, "currency": "USD"}},
        {"amount": {"amount": "1e3", "currency": "USD"}},
        {"amount": {"amount": "NaN", "currency": "USD"}},
        {"amount": {"amount": "1", "currency": "XXX"}},
        {"raw_payee": "a\u202eb"},
        {"raw_payee": "line\nbreak"},
        {"memo": "nul\x00"},
        {"notes": "x" * 4001},
        {"tag_ids": [str(ID), str(ID)]},
        {"tag_ids": [str(uuid.uuid4()) for _ in range(51)]},
    ],
)
def test_create_refuses(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TransactionCreate.model_validate(_create(**extra))


def test_update_distinguishes_absent_from_null() -> None:
    model = TransactionUpdate.model_validate({"memo": None})
    assert model.model_fields_set == {"memo"}
    assert TransactionUpdate.model_validate({}).model_fields_set == set()
    assert TransactionUpdate.model_validate({"memo": "multi\nline"}).memo == "multi\nline"
    for name in ("posted_date", "amount", "status", "review_state", "tag_ids"):
        with pytest.raises(ValidationError):
            TransactionUpdate.model_validate({name: None})
    with pytest.raises(ValidationError):
        TransactionUpdate.model_validate({"account_id": str(ID)})


def _line(amount: str) -> dict[str, Any]:
    return {"amount": {"amount": amount, "currency": "USD"}}


def test_split_set_shape() -> None:
    assert SplitSet.model_validate({"splits": []}).splits == []
    assert len(SplitSet.model_validate({"splits": [_line("-1"), _line("-2")]}).splits) == 2
    with pytest.raises(ValidationError):
        SplitSet.model_validate({"splits": [_line("-1")]})
    with pytest.raises(ValidationError):
        SplitSet.model_validate({"splits": [_line("-1"), _line("-1")], "category_id": str(ID)})
    with pytest.raises(ValidationError):
        SplitSet.model_validate({"splits": [_line("-1")] * 101})


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        ({"action": "categorise", "ids": [str(ID)], "category_id": None}, True),
        ({"action": "categorise", "ids": [str(ID)]}, False),
        ({"action": "tag", "ids": [str(ID)], "tag_ids": [str(ID)]}, True),
        ({"action": "untag", "ids": [str(ID)]}, False),
        ({"action": "confirm", "ids": [str(ID)]}, True),
        ({"action": "confirm", "ids": [str(ID)], "tag_ids": [str(ID)]}, False),
        ({"action": "delete", "ids": [str(ID)], "category_id": str(ID)}, False),
        ({"action": "delete", "ids": []}, False),
        ({"action": "delete", "ids": [str(ID), str(ID)]}, False),
    ],
)
def test_bulk_action_fields(body: dict[str, Any], ok: bool) -> None:
    if ok:
        BulkAction.model_validate(body)
    else:
        with pytest.raises(ValidationError):
            BulkAction.model_validate(body)


def test_auto_pair_request_bounds() -> None:
    assert AutoPairRequest.model_validate({}).window_days == 3
    for bad in ({"window_days": 15}, {"date_from": "2026-01-02", "date_to": "2026-01-01"}):
        with pytest.raises(ValidationError):
            AutoPairRequest.model_validate(bad)


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

ACCT = {name: uuid.uuid4() for name in "ABCD"}


def _edge(distance: int, out: str, out_acct: str, inn: str, in_acct: str) -> _Edge:
    return _Edge(
        distance,
        uuid.uuid5(uuid.NAMESPACE_OID, out),
        ACCT[out_acct],
        date(2026, 1, 1),
        uuid.uuid5(uuid.NAMESPACE_OID, inn),
        ACCT[in_acct],
    )


def _names(pairs: list[tuple[uuid.UUID, uuid.UUID]]) -> set[tuple[str, str]]:
    lookup = {uuid.uuid5(uuid.NAMESPACE_OID, n): n for n in ("o1", "o2", "i1", "i2", "i3")}
    return {(lookup[o], lookup[i]) for o, i in pairs}


def test_match_closest_first() -> None:
    edges = sorted(
        [_edge(2, "o1", "A", "i1", "B"), _edge(0, "o1", "A", "i2", "C")],
        key=lambda e: e.distance,
    )
    pairs, ambiguous = _match(edges)
    assert _names(pairs) == {("o1", "i2")}
    assert ambiguous == 0


def test_match_refuses_equal_rivals_on_other_accounts() -> None:
    pairs, ambiguous = _match([_edge(1, "o1", "A", "i1", "B"), _edge(1, "o1", "A", "i2", "C")])
    assert pairs == []
    assert ambiguous == 1


def test_match_refuses_equal_rival_outflows() -> None:
    pairs, ambiguous = _match([_edge(0, "o1", "A", "i1", "C"), _edge(0, "o2", "B", "i1", "C")])
    assert pairs == []
    assert ambiguous == 1


def test_match_interchangeable_duplicates() -> None:
    edges = [
        _edge(0, "o1", "A", "i1", "B"),
        _edge(0, "o1", "A", "i2", "B"),
        _edge(0, "o2", "A", "i1", "B"),
        _edge(0, "o2", "A", "i2", "B"),
    ]
    pairs, ambiguous = _match(edges)
    assert _names(pairs) == {("o1", "i1"), ("o2", "i2")}
    assert ambiguous == 0


def test_match_uses_each_leg_once() -> None:
    edges = [_edge(0, "o1", "A", "i1", "B"), _edge(1, "o1", "A", "i3", "D")]
    pairs, _ = _match(edges)
    assert _names(pairs) == {("o1", "i1")}

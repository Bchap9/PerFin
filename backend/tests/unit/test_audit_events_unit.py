# SPDX-License-Identifier: AGPL-3.0-or-later
"""audit() sanitising and events validation without a database."""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy.orm import Session

from app.core.audit import (
    MAX_STATE_BYTES,
    MAX_STRING_CHARS,
    REDACTED,
    TRUNCATED_SUFFIX,
    _bounded_state,
    audit,
    is_secret_key,
    sanitize,
)
from app.core.events import emit, parse_notice

NO_DB = cast(Session, object())


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "new_password",
        "Password-Hash",
        "client_secret",
        "api_token",
        "refresh_token",
        "totp_secret",
        "recovery_codes",
        "wrapped_dek",
        "session_id",
        "csrf_token",
        "Authorization",
        "cookie",
        "private_key",
        "imap_credentials",
        "extracted_text",
        "account_number",
    ],
)
def test_secret_keys_detected(key: str) -> None:
    assert is_secret_key(key)


@pytest.mark.parametrize(
    "key", ["display_name", "amount", "role", "key", "currency", "email_at_link"]
)
def test_ordinary_keys_kept(key: str) -> None:
    assert not is_secret_key(key)


class Color(enum.Enum):
    RED = "red"


class Model(BaseModel):
    name: str
    token: str


@dataclass
class Thing:
    label: str
    secret: str


def test_sanitize_redacts_converts_and_scrubs() -> None:
    value: dict[str, Any] = {
        "password": "hunter2-synthetic",
        "nested": [{"api_key": "k-synthetic"}, {"memo": "card 4111111111111111"}],
        "id": uuid.UUID("01890000-0000-7000-8000-000000000001"),
        "when": dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
        "day": dt.date(2026, 1, 2),
        "amount": Decimal("12345678.90"),
        "color": Color.RED,
        "raw": b"\x00\x01",
        "model": Model(name="Synthetic", token="t-synthetic"),
        "thing": Thing(label="x", secret="s-synthetic"),
        "wrapped": SecretStr("never"),
        "nan": float("nan"),
        "tags": ("a", "b"),
    }
    clean = sanitize(value)
    assert clean["password"] == REDACTED
    assert clean["nested"][0] == {"api_key": REDACTED}
    assert "4111111111111111" not in clean["nested"][1]["memo"]
    assert clean["id"] == "01890000-0000-7000-8000-000000000001"
    assert clean["when"] == "2026-01-02T03:04:05+00:00"
    assert clean["day"] == "2026-01-02"
    assert clean["amount"] == "12345678.90"  # amounts are not run through the scrubber
    assert clean["color"] == "red"
    assert clean["raw"] == "<2 bytes>"
    assert clean["model"] == {"name": "Synthetic", "token": REDACTED}
    assert clean["thing"] == {"label": "x", "secret": REDACTED}
    assert clean["wrapped"] == REDACTED
    assert clean["nan"] is None
    assert clean["tags"] == ["a", "b"]
    assert "hunter2" not in repr(clean)


def test_sanitize_depth_is_bounded() -> None:
    deep: dict[str, Any] = {}
    cursor = deep
    for _ in range(20):
        cursor["n"] = {}
        cursor = cursor["n"]
    rendered = repr(sanitize(deep))
    assert REDACTED in rendered


def test_long_strings_are_cut_before_scrubbing() -> None:
    clean = sanitize({"memo": "x" * 100_000})
    assert clean["memo"].endswith(TRUNCATED_SUFFIX)
    assert len(clean["memo"]) <= MAX_STRING_CHARS + len(TRUNCATED_SUFFIX)


def test_oversized_state_is_truncated() -> None:
    big = {f"k{i}": "y" * MAX_STRING_CHARS for i in range(MAX_STATE_BYTES // MAX_STRING_CHARS)}
    result = _bounded_state(big)
    assert result["_truncated"] is True
    assert result["bytes"] > MAX_STATE_BYTES
    assert _bounded_state(None) is None
    assert _bounded_state({"a": 1}) == {"a": 1}


@pytest.mark.parametrize("action", ["", "Login", "user login", "x" * 101, "drop;table"])
def test_invalid_action_rejected(action: str) -> None:
    with pytest.raises(ValueError, match="invalid audit action"):
        audit(NO_DB, action=action)


def test_invalid_target_and_ids_rejected() -> None:
    with pytest.raises(ValueError, match="target_type"):
        audit(NO_DB, action="x.y", target_type="Bad Type")
    with pytest.raises(TypeError):
        audit(NO_DB, action="x.y", household_id="not-a-uuid")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        audit(NO_DB, action="x.y", user_id=1)  # type: ignore[arg-type]


def test_emit_validates_before_touching_the_database() -> None:
    household = uuid.uuid4()
    with pytest.raises(TypeError):
        emit(NO_DB, "nope", "x.y")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid event type"):
        emit(NO_DB, household, "Bad Type")
    with pytest.raises(ValueError, match="JSON"):
        emit(NO_DB, household, "x.y", {"when": dt.datetime.now(dt.UTC)})
    with pytest.raises(ValueError, match="JSON"):
        emit(NO_DB, household, "x.y", {"n": float("nan")})
    with pytest.raises(ValueError, match="exceeds"):
        emit(NO_DB, household, "x.y", {"blob": "x" * 20_000})
    with pytest.raises(TypeError, match="mapping"):
        emit(NO_DB, household, "x.y", ["not", "a", "mapping"])  # type: ignore[arg-type]


def test_parse_notice() -> None:
    event_id, household = uuid.uuid4(), uuid.uuid4()
    payload = f'{{"id":"{event_id}","household_id":"{household}","type":"a.b"}}'
    notice = parse_notice(payload)
    assert notice is not None
    assert (notice.id, notice.household_id, notice.type) == (event_id, household, "a.b")
    for bad in (
        "not json",
        "[]",
        '{"id":"x","household_id":"y","type":"a"}',
        f'{{"id":"{event_id}","household_id":"{household}"}}',
        f'{{"id":"{event_id}","household_id":"{household}","type":"Bad Type"}}',
        f'{{"id":"{event_id}","household_id":"{household}","type":7}}',
    ):
        assert parse_notice(bad) is None

# SPDX-License-Identifier: AGPL-3.0-or-later
"""deploy/ml/perfin_ml.py: the optional model adapter (ADR-0010), with a fake runtime."""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ADAPTER = Path(__file__).resolve().parents[3] / "deploy" / "ml" / "perfin_ml.py"
TOKEN = "s" * 40


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("perfin_ml_adapter", ADAPTER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ml = _load()


class FakeRuntime(ml.Runtime):  # type: ignore[misc, name-defined]
    def __init__(self, config: Any, answer: Any) -> None:
        super().__init__(config)
        self.answer = answer
        self.bodies: list[dict[str, Any]] = []

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        return {"message": {"content": json.dumps(self.answer)}}


@pytest.fixture
def served() -> Iterator[tuple[str, FakeRuntime]]:
    config = ml.Config({"PERFIN_ML_TOKEN": TOKEN, "PERFIN_ML_LISTEN": "127.0.0.1:0"})
    runtime = FakeRuntime(
        config,
        {
            "kind": "statement",
            "institution": "  Synthetic   Bank ",
            "accounts": [
                {"last4": "12345", "name": "Main", "type": "spaceship", "currency": "USD"}
            ],
            "period_start": None,
            "period_end": None,
            "confidence": 3,
            "extra": "dropped",
        },
    )
    server = ml.ThreadingHTTPServer(("127.0.0.1", 0), ml.make_handler(config, runtime))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", runtime
    finally:
        server.shutdown()


def _post(
    url: str, body: Any, token: str | None = TOKEN, path: str = "/v1/document"
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(  # noqa: S310 - the test server
        f"{url}{path}", data=data, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_a_document_is_answered_in_the_schema_only(served: tuple[str, FakeRuntime]) -> None:
    url, runtime = served
    status, answer = _post(url, {"text": "Account ending 1234"})
    assert status == 200
    assert answer == {
        "kind": "statement",
        "institution": "Synthetic Bank",
        "accounts": [{"last4": None, "name": "Main", "type": None, "currency": "USD"}],
        "period_start": None,
        "period_end": None,
        "confidence": 0.0,
        "model": "qwen2.5:7b-instruct",
    }
    (body,) = runtime.bodies
    assert body["format"] == ml.SCHEMA
    assert body["options"]["temperature"] == 0
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["content"] == "Account ending 1234"


def test_requests_need_the_token_and_text(served: tuple[str, FakeRuntime]) -> None:
    url, runtime = served
    assert _post(url, {"text": "x"}, token=None)[0] == 401
    assert _post(url, {"text": "x"}, token="wrong")[0] == 401
    assert _post(url, {"text": "  "})[0] == 422
    assert _post(url, ["no", "object"])[0] == 422
    assert runtime.bodies == []


def test_the_text_sent_to_the_model_is_capped(served: tuple[str, FakeRuntime]) -> None:
    url, runtime = served
    assert _post(url, {"text": "a" * (ml.MAX_TEXT + 500)})[0] == 200
    assert len(runtime.bodies[0]["messages"][1]["content"]) == ml.MAX_TEXT


def test_the_adapter_refuses_to_start_without_a_strong_token() -> None:
    with pytest.raises(SystemExit):
        ml.Config({"PERFIN_ML_TOKEN": "short"})
    with pytest.raises(SystemExit):
        ml.Config({"PERFIN_ML_TOKEN": TOKEN, "PERFIN_ML_RUNTIME": "file:///etc/passwd"})


# ---------------------------------------------------------------------------
# /v1/vocabulary: what kind of activity a phrase or a heading is
# ---------------------------------------------------------------------------

VOCABULARY = "/v1/vocabulary"


def test_phrases_are_numbered_for_the_model_and_answered_from_the_lists_only(
    served: tuple[str, FakeRuntime],
) -> None:
    url, runtime = served
    runtime.answer = {
        "actions": [
            {"index": 1, "type": "buy"},
            {"index": 2, "type": "unknown"},  # not sure: left out
            {"index": 3, "type": "teleport"},  # not a listed kind
            {"index": 7, "type": "sell"},  # no such phrase
            {"index": 1, "type": "sell"},  # one answer per phrase
        ],
        "headings": [{"index": 1, "kind": "cash"}, {"index": 2, "kind": "skip"}],
        "note": "dropped",
    }
    status, answer = _post(
        url,
        {
            "actions": ["periodic investment plan", "bank sweep credit ref 0", "   x   y  "],
            "headings": ["sweep program activity", "estimated cash flow"],
        },
        path=VOCABULARY,
    )
    assert status == 200
    assert answer == {
        "actions": [{"index": 1, "type": "buy"}],
        "headings": [{"index": 1, "kind": "cash"}],
        "model": "qwen2.5:7b-instruct",
    }
    (body,) = runtime.bodies
    assert body["format"] == ml.VOCABULARY_SCHEMA
    assert body["options"]["temperature"] == 0
    assert body["messages"][0] == {"role": "system", "content": ml.VOCABULARY_PROMPT}
    assert body["messages"][1]["content"] == (
        "ACTIONS:\n1. periodic investment plan\n2. bank sweep credit ref 0\n3. x y\n\n"
        "HEADINGS:\n1. sweep program activity\n2. estimated cash flow"
    )


def test_the_model_cannot_be_asked_to_hide_a_table() -> None:
    # The lists the adapter offers the model type rows; none says "not activity".
    assert "skip" not in ml.SECTION_KINDS
    assert "split" not in ml.ACTIVITY_TYPES
    assert ml.SECTION_KINDS[-1] == ml.ACTIVITY_TYPES[-1] == "unknown"


def test_vocabulary_requests_need_the_token_and_phrases_and_are_capped(
    served: tuple[str, FakeRuntime],
) -> None:
    url, runtime = served
    runtime.answer = {"actions": [], "headings": []}
    assert _post(url, {"actions": ["x"]}, token=None, path=VOCABULARY)[0] == 401
    assert _post(url, {"actions": [], "headings": ["  "]}, path=VOCABULARY)[0] == 422
    assert _post(url, {"text": "a whole document"}, path=VOCABULARY)[0] == 422
    assert runtime.bodies == []
    many = [f"phrase {number} " + "x" * 300 for number in range(ml.MAX_PHRASES + 30)]
    status, answer = _post(url, {"actions": many, "headings": []}, path=VOCABULARY)
    assert (status, answer["actions"]) == (200, [])
    sent = runtime.bodies[0]["messages"][1]["content"].splitlines()
    numbered = [line for line in sent if line[:1].isdigit()]
    assert len(numbered) == ml.MAX_PHRASES
    assert max(len(line) for line in numbered) <= ml.PHRASE_MAX + len("40. ")
    assert sent[-1] == "(none)"

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


def _post(url: str, body: Any, token: str | None = TOKEN) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(  # noqa: S310 - the test server
        f"{url}/v1/document", data=data, headers=headers
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

# SPDX-License-Identifier: AGPL-3.0-or-later
"""``perfin ml eval``: the local model measured against PerFin's own vocabulary.

No model runs here: the adapter is an ``httpx.MockTransport`` that answers
from the same vocabulary, perfectly or with chosen mistakes.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from app import cli
from app.core.settings import Settings
from app.ml import client
from app.ml.evaluate import BATCH, evaluate, ground_truth


@pytest.fixture(autouse=True)
def _fresh_breaker() -> None:
    client.reset_breaker()


def _settings() -> Settings:
    return Settings(ml_url="http://ml:3003", ml_token=SecretStr("t" * 40))


Change = Callable[[str, str], str | None]


def _adapter(
    change: Change | None = None,
) -> tuple[httpx.MockTransport, list[dict[str, list[str]]]]:
    """An adapter that knows PerFin's vocabulary; ``change`` alters an answer (None: no answer)."""
    actions, headings = ground_truth()
    seen: list[dict[str, list[str]]] = []

    def answer(phrases: list[str], known: dict[str, str], field: str) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for index, phrase in enumerate(phrases, start=1):
            kind: str | None = known[phrase]
            if change is not None:
                kind = change(phrase, known[phrase])
            if kind is not None:
                out.append({"index": index, field: kind})
        return out

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "actions": answer(body["actions"], actions, "type"),
                "headings": answer(body["headings"], headings, "kind"),
                "model": "example-model",
            },
        )

    return httpx.MockTransport(handler), seen


def test_the_ground_truth_is_what_a_suggestion_may_name() -> None:
    actions, headings = ground_truth()
    assert actions["dividend received"] == "dividend"
    assert actions["you bought"] == "buy"
    # A split needs a ratio no phrase gives; a skipped section is never suggested.
    assert "split" not in actions.values()
    assert not {"skip", "plain", "margin"} & set(headings.values())
    assert headings


def test_a_model_that_knows_the_vocabulary_agrees_with_all_of_it() -> None:
    transport, seen = _adapter()
    report = evaluate(_settings(), transport=transport)
    actions, headings = ground_truth()
    assert report.model == "example-model"
    assert report.reachable
    assert report.actions.asked == len(actions)
    assert report.headings.asked == len(headings)
    assert report.total.agreed == len(actions) + len(headings)
    assert report.total.agreement == 1.0
    assert report.differences == []
    # Only PerFin's own word lists are sent, in bounded batches, with no text.
    assert all(len(body["actions"]) + len(body["headings"]) <= BATCH for body in seen)
    assert all(body["text"] == "" for body in seen)
    assert {p for body in seen for p in body["actions"]} == set(actions)


def test_wrong_and_missing_answers_are_counted_and_listed() -> None:
    def change(phrase: str, kind: str) -> str | None:
        if phrase == "dividend received":
            return "interest"
        return None if phrase == "you bought" else kind

    transport, _ = _adapter(change)
    report = evaluate(_settings(), transport=transport)
    assert report.actions.differed == 1
    assert report.actions.unanswered == 1
    assert report.actions.agreed == report.actions.asked - 2
    assert report.differences == [
        {
            "list": "actions",
            "phrase": "dividend received",
            "expected": "dividend",
            "answered": "interest",
        }
    ]
    share = report.total.agreement
    assert share is not None
    assert 0.9 < share < 1.0
    assert report.as_dict()["total"]["differed"] == 1


def test_an_adapter_that_does_not_answer_is_said() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    report = evaluate(_settings(), transport=transport)
    assert not report.reachable
    assert report.total.agreed == 0
    assert report.total.unanswered == report.total.asked


def test_the_command_needs_a_configured_model(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("app.core.settings.get_settings", lambda: Settings())
    assert cli.main(["ml", "eval"]) == cli.EXIT_FAIL
    assert "no local model is configured" in capsys.readouterr().err


def test_the_command_prints_the_score(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport, _ = _adapter(lambda phrase, kind: "interest" if phrase == "sold" else kind)
    monkeypatch.setattr("app.core.settings.get_settings", _settings)
    monkeypatch.setattr(
        "app.ml.evaluate.activity_hints",
        lambda settings, actions, headings, **_: client.activity_hints(
            settings, actions, headings, transport=transport
        ),
    )
    assert cli.main(["ml", "eval", "--json"]) == cli.EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed["model"] == "example-model"
    assert printed["total"]["differed"] == 1
    assert printed["differences"][0]["phrase"] == "sold"
    # A floor the model does not reach fails the command.
    client.reset_breaker()
    assert cli.main(["ml", "eval", "--min-agreement", "1.0"]) == cli.EXIT_FAIL
    assert "the model said interest" in capsys.readouterr().out

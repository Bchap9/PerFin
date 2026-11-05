# SPDX-License-Identifier: AGPL-3.0-or-later
"""The optional local-model hints (ADR-0010): local-only address, checked answers.

Every answer here is invented; no model runs. The adapter is replaced by an
``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.core.local_url import is_local_url
from app.core.settings import Settings
from app.domain.documents.models import Document, DocumentKind
from app.domain.imports import proposals
from app.ingestion import pipeline
from app.ingestion.sandbox.protocol import ExtractResult
from app.ml import client
from app.ml.client import MLAccount, MLHints, document_hints, validated

TEXT = "Synthetic Savings Bank\nAccount ending in 4321\nStatement period 10/01/2026 - 10/31/2026"
ANSWER: dict[str, Any] = {
    "kind": "statement",
    "institution": "Synthetic Savings Bank",
    "accounts": [
        {"last4": "4321", "name": "Everyday", "type": "checking", "currency": "USD"},
        {"last4": "9999", "name": "Made up", "type": "savings", "currency": "usd"},
    ],
    "period_start": "2026-10-01",
    "period_end": "2026-10-31",
    "confidence": 0.9,
    "model": "qwen2.5:7b-instruct",
}


@pytest.fixture(autouse=True)
def _fresh_breaker() -> None:
    client.reset_breaker()


def _settings(**values: Any) -> Settings:
    return Settings(ml_url="http://ml:3003", ml_token=SecretStr("t" * 40), **values)


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://ml:3003", True),
        ("http://localhost:3003", True),
        ("http://127.0.0.1:3003", True),
        ("http://192.168.1.20:3003", True),
        ("http://[fd00::1]:3003", True),
        ("https://gpu-box.lan", True),
        ("http://gpu.home.arpa:3003", True),
        ("https://api.example.com", False),
        ("http://8.8.8.8", False),
        ("ftp://ml:21", False),
        ("http://user:pw@ml:3003", False),
        ("ml:3003", False),
    ],
)
def test_only_local_addresses_are_accepted(url: str, local: bool) -> None:
    assert is_local_url(url) is local


def test_settings_refuse_a_remote_model_and_are_off_by_default() -> None:
    assert Settings().ml_enabled is False
    assert _settings().ml_enabled is True
    with pytest.raises(ValidationError, match="must be a local"):
        Settings(ml_url="https://api.example.com/v1", ml_token=SecretStr("t" * 40))


def test_answers_are_checked_against_the_document() -> None:
    hints = validated({**ANSWER, "kind": "spaceship"}, TEXT)
    assert hints is not None
    assert hints.kind is None  # not a kind PerFin knows
    first, second = hints.accounts
    assert (first.last4, first.type, first.currency) == ("4321", "checking", "USD")
    # 9999 is not printed in the document: a guess, dropped. "usd" is no ISO code.
    assert (second.last4, second.currency) == (None, None)
    assert (hints.period_start, hints.period_end) == (date(2026, 10, 1), date(2026, 10, 31))
    assert validated("not an object", TEXT) is None
    assert validated({**ANSWER, "period_start": "1066-10-14"}, TEXT).period_start is None  # type: ignore[union-attr]


def test_the_adapter_is_asked_with_text_only_and_a_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ANSWER)

    hints = document_hints(
        _settings(ml_max_chars=1000), TEXT * 100, transport=httpx.MockTransport(handler)
    )
    assert hints is not None
    assert hints.kind == "statement"
    (request,) = seen
    assert str(request.url) == "http://ml:3003/v1/document"
    assert request.headers["Authorization"] == "Bearer " + "t" * 40
    assert set(json.loads(request.content)) == {"text"}
    assert len(json.loads(request.content)["text"]) == 1000


def test_failures_never_fail_ingestion_and_trip_the_breaker() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, json={"error": "runtime_failed"})

    transport = httpx.MockTransport(handler)
    for _ in range(client.BREAKER_FAILURES + 2):
        assert document_hints(_settings(), TEXT, transport=transport) is None
    assert calls == client.BREAKER_FAILURES  # then not asked for a while


def test_off_means_no_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError("called")

    assert document_hints(Settings(), TEXT, transport=httpx.MockTransport(handler)) is None


def test_hints_round_trip_through_storage() -> None:
    hints = validated(ANSWER, TEXT)
    assert hints is not None
    assert MLHints.from_stored(hints.stored()) == hints


def test_the_pipeline_keeps_hints_and_sets_an_unknown_kind() -> None:
    class Db:
        def flush(self) -> None:
            return None

    document = Document(kind=DocumentKind.UNKNOWN, confidence={"kind": 0.3})
    result = ExtractResult(ok=True, kind="pdf", extractor_version="pdf/1", text=TEXT)
    asked: list[str] = []

    def ask(text: str) -> MLHints | None:
        asked.append(text)
        return validated(ANSWER, text)

    pipeline._ml_hints(Db(), document, result, ask)  # type: ignore[arg-type]
    assert asked == [TEXT]
    assert document.kind is DocumentKind.STATEMENT
    facts = document.confidence
    assert facts is not None
    assert facts["kind_source"] == "ml"
    assert facts["kind"] == 0.3
    assert facts["ml"]["accounts"][0]["last4"] == "4321"


def test_proposals_fill_gaps_from_the_hints_and_say_so() -> None:
    found = proposals._Found(
        target="statement",
        index=0,
        last4=None,
        name=None,
        institution=None,
        account_type=None,
        currency=None,
        period_start=None,
        period_end=None,
        opening_balance=None,
        closing_balance=None,
        transactions=3,
    )
    hints = MLHints(
        kind="statement",
        institution="Synthetic Savings Bank",
        accounts=(MLAccount("4321", "Everyday", "checking", "USD"),),
        period_start=None,
        period_end=None,
        confidence=0.9,
    )
    proposals._with_hints([found], hints)
    proposal = proposals._proposal(found, found.institution, "USD")
    assert proposal.suggested_by_model is True
    assert (proposal.name, proposal.number_last4, proposal.institution_name) == (
        "Synthetic Everyday 4321",
        "4321",
        "Synthetic Savings Bank",
    )

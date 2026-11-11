# SPDX-License-Identifier: AGPL-3.0-or-later
"""Words the local model may suggest a kind for (ADR-0010; ``parsers.vocabulary``).

A brokerage parser reports the action phrases and section headings it
could not type; a checked :class:`Vocabulary` types them on a second read,
each row marked as a suggestion. No model runs here: the adapter is an
``httpx.MockTransport`` and every phrase and figure is invented.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.scrubber import Scrubber, member_terms
from app.core.settings import Settings
from app.domain.investments.statement_import import (
    LINES_HELD,
    LOW_CONFIDENCE,
    RECONCILIATION_FAILED,
    acceptable_as_read,
)
from app.ingestion.parsers import confidence
from app.ingestion.parsers.base import Cell, ViewLine
from app.ingestion.parsers.brokerage import FidelityStatementParser, _Collected
from app.ingestion.parsers.vocabulary import (
    MAX_PHRASES,
    SUGGESTED_DOUBT,
    Vocabulary,
    column_key,
    phrase_key,
    rememberable,
)
from app.ingestion.sandbox.protocol import InvestmentActivity
from app.ml import client
from app.ml.client import activity_hints, validated_vocabulary

PERIOD = (date(2026, 6, 1), date(2026, 6, 30))


@pytest.fixture(autouse=True)
def _fresh_breaker() -> None:
    client.reset_breaker()


def _settings() -> Settings:
    return Settings(ml_url="http://ml:3003", ml_token=SecretStr("t" * 40))


def _line(y: float, cells: list[tuple[float, str]]) -> ViewLine:
    built = tuple(Cell(text, x, y, x + 4.5 * len(text), y + 9.0) for x, text in cells)
    return ViewLine(page=1, index=0, cells=built, text=" ".join(c.text for c in built), height=9.0)


_TRADES: list[ViewLine] = [
    _line(100, [(32, "Securities Bought & Sold")]),
    _line(
        113,
        [
            (32, "Date"),
            (72, "Security Name"),
            (240, "Symbol/CUSIP"),
            (300, "Transaction Description"),
            (430, "Quantity"),
            (500, "Price"),
            (640, "Transaction Total"),
        ],
    ),
    _line(
        126,
        [
            (32, "6/5"),
            (72, "EXAMPLE INDEX ETF"),
            (240, "ZZETF"),
            (300, "Periodic investment plan"),
            (440, "3"),
            (500, "100.00"),
            (670, "-300.00"),
        ],
    ),
    _line(
        139,
        [
            (32, "6/9"),
            (72, "EXAMPLE INDEX ETF"),
            (240, "ZZETF"),
            (300, "You bought"),
            (440, "1"),
            (500, "100.00"),
            (670, "-100.00"),
        ],
    ),
]
_SWEEP: list[ViewLine] = [
    _line(200, [(32, "Sweep Program Activity")]),
    _line(213, [(32, "Date"), (72, "Description"), (640, "Amount")]),
    _line(226, [(32, "6/3"), (72, "Bank sweep credit ref 4471"), (645, "$40.00")]),
    _line(239, [(32, "6/8"), (72, "Bank sweep debit ref 4502"), (645, "-15.00")]),
]


def _read(
    lines: list[ViewLine], vocabulary: Vocabulary | None = None
) -> tuple[_Collected, list[InvestmentActivity], list[dict[str, float]]]:
    parser = FidelityStatementParser()
    collected = _Collected(vocabulary=vocabulary)
    parser._read(lines, collected)
    rows, scores, _ = parser._activities(collected, ("md",), True, PERIOD, {})
    return collected, rows, scores


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def test_a_phrase_key_masks_digits_and_is_its_own_key() -> None:
    key = phrase_key("Transferred from account 123-456789")
    assert key == "transferred from account 0 0"
    assert phrase_key(key) == key
    assert phrase_key("  PERIODIC   Investment Plan ") == "periodic investment plan"
    assert len(phrase_key("x" * 500)) <= 80


def test_only_listed_kinds_survive_storage() -> None:
    stored = {
        "actions": {
            "periodic investment plan": "buy",
            "stock split": "split",  # a ratio no phrase gives
            "Not A Key": "buy",  # not a key PerFin would have made
            "wire out": "launder",
        },
        "sections": {
            "sweep program activity": "cash",
            "estimated cash flow": "skip",  # a model may not hide a table
        },
        "model": "example-model",
    }
    found = Vocabulary.from_stored(stored)
    assert found.actions == {"periodic investment plan": "buy"}
    assert found.sections == {"sweep program activity": "cash"}
    assert Vocabulary.from_stored(found.stored()) == found
    assert Vocabulary.from_stored(None).empty
    assert Vocabulary.from_stored(["not", "an", "object"]).empty


# ---------------------------------------------------------------------------
# The parser: what it could not type, and the same pages read with suggestions
# ---------------------------------------------------------------------------


def test_untyped_words_are_reported_and_their_rows_left_unread() -> None:
    collected, rows, _ = _read([*_TRADES, *_SWEEP])
    assert [(r.type, r.amount) for r in rows] == [("buy", Decimal("-100.00"))]
    assert collected.unknown_activity == 3
    # Their keys: no digits, so no reference or account number is kept or sent.
    assert collected.unknown_actions == [
        "periodic investment plan",
        "bank sweep credit ref 0",
        "bank sweep debit ref 0",
    ]
    assert collected.unknown_headings == ["sweep program activity"]


def test_a_suggested_action_types_its_row_as_a_suggestion() -> None:
    words = Vocabulary(actions={"periodic investment plan": "buy"})
    collected, rows, scores = _read(_TRADES, words)
    assert [(r.trade_date, r.type, r.quantity, r.amount) for r in rows] == [
        (date(2026, 6, 5), "buy", Decimal(3), Decimal("-300.00")),
        (date(2026, 6, 9), "buy", Decimal(1), Decimal("-100.00")),
    ]
    assert collected.unknown_actions == []
    # Below any threshold a deployment may set: a person confirms it.
    assert scores[0][SUGGESTED_DOUBT] == confidence.SUGGESTED < 0.5
    assert SUGGESTED_DOUBT not in scores[1]
    # The figures are the page's own; the suggestion named a kind, nothing else.
    assert rows[0].security is not None
    assert rows[0].security.symbol == "ZZETF"
    assert rows[0].unit_price == Decimal("100.00")


def test_a_suggested_section_types_its_rows_by_their_sign() -> None:
    words = Vocabulary(sections={"sweep program activity": "cash"})
    collected, rows, scores = _read(_SWEEP, words)
    assert [(r.type, r.amount, r.section) for r in rows] == [
        ("deposit", Decimal("40.00"), "Sweep Program Activity"),
        ("withdrawal", Decimal("-15.00"), "Sweep Program Activity"),
    ]
    assert all(SUGGESTED_DOUBT in score for score in scores)
    assert collected.unknown_headings == []
    # A heading the parser knows is never the model's to type.
    known, typed, known_scores = _read(
        [_line(200, [(32, "Deposits")]), *_SWEEP[1:]],
        Vocabulary(sections={"deposits": "fee"}),
    )
    assert [r.type for r in typed] == ["deposit", "withdrawal"]
    assert not any(SUGGESTED_DOUBT in score for score in known_scores)
    assert known.unknown_headings == []


def test_wording_a_person_confirmed_types_rows_like_the_parsers_own() -> None:
    learned = Vocabulary(
        learned_actions={"periodic investment plan": "buy"},
        learned_sections={"sweep program activity": "cash"},
        # A suggestion for the same words never overrides what was confirmed.
        actions={"periodic investment plan": "sell"},
    )
    collected, rows, scores = _read([*_TRADES, *_SWEEP], learned)
    assert [(r.type, r.amount) for r in rows] == [
        ("buy", Decimal("-300.00")),
        ("buy", Decimal("-100.00")),
        ("deposit", Decimal("40.00")),
        ("withdrawal", Decimal("-15.00")),
    ]
    assert not any(SUGGESTED_DOUBT in score for score in scores)
    assert all(r.suggestion is None for r in rows)
    assert (collected.unknown_activity, collected.unknown_actions) == (0, [])
    assert collected.unknown_headings == []
    # What is still to ask about is what neither names.
    assert learned.untyped(
        ["periodic investment plan", "margin call"], ["sweep program activity", "odd lots"]
    ) == (["margin call"], ["odd lots"])


def test_only_wording_is_marked_for_remembering() -> None:
    """A heading, or a broker's action words beside a security: never a cash row's text."""
    _, trades, _ = _read(_TRADES, Vocabulary(actions={"periodic investment plan": "buy"}))
    assert [r.suggestion for r in trades] == ["action:periodic investment plan", None]
    _, sweep, _ = _read(_SWEEP, Vocabulary(sections={"sweep program activity": "cash"}))
    assert {r.suggestion for r in sweep} == {"section:sweep program activity"}
    # A cash table's description is free text (a payee, a reference): it may
    # be typed for this document, it is not wording to keep.
    words = Vocabulary(
        actions={"bank sweep credit ref 0": "deposit", "bank sweep debit ref 0": "withdrawal"}
    )
    _, typed, scores = _read(_SWEEP, words)
    assert [(r.type, r.suggestion) for r in typed] == [("deposit", None), ("withdrawal", None)]
    assert all(SUGGESTED_DOUBT in score for score in scores)


@pytest.mark.parametrize(
    ("phrase", "kept"),
    [
        ("Periodic Investment Plan", True),
        ("Sweep Program Activity", True),
        ("Check 1076 paid", True),  # kept as "check 0 paid": no digit survives
        ("Zelle payment to Jordan Sample", False),  # a member's name
        ("Dividend for the Sample household", False),  # the household's name
        ("Wire to 12 Main St Springfield", False),  # an address
        ("7", False),
        ("", False),
    ],
)
def test_nothing_personal_is_rememberable(phrase: str, kept: bool) -> None:
    scrubber = Scrubber(
        member_terms=member_terms(
            names=["Jordan Q. Sample"], household_names=["The Sample Family"]
        ),
        account_numbers=["4321"],
    )
    key = phrase_key(phrase)
    assert not any(ch.isdigit() and ch != "0" for ch in key)
    assert rememberable(key, scrubber.scrub_text) is kept
    # A phrase that is not a key PerFin made (case, punctuation, digits) is refused.
    assert not rememberable("Periodic Investment Plan #4471")


def test_a_suggestion_outside_the_lists_changes_nothing() -> None:
    # Built by hand, past every check: the parser still takes listed kinds only.
    words = Vocabulary(
        actions={"periodic investment plan": "split"},
        sections={"sweep program activity": "skip"},
    )
    collected, rows, _ = _read([*_TRADES, *_SWEEP], words)
    assert [(r.type, r.amount) for r in rows] == [("buy", Decimal("-100.00"))]
    assert collected.unknown_activity == 3


# ---------------------------------------------------------------------------
# The worker's client: what is sent, what is kept of the answer
# ---------------------------------------------------------------------------


def test_answers_are_matched_by_position_to_the_phrases_sent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "actions": [
                    {"index": 1, "type": "buy"},
                    {"index": 2, "type": "teleport"},  # not a listed kind
                    {"index": 9, "type": "sell"},  # no such phrase
                    {"index": 1, "type": "sell"},  # a second answer for one phrase
                    {"index": True, "type": "fee"},
                    "noise",
                ],
                "headings": [
                    {"index": 1, "kind": "cash"},
                    {"index": 2, "kind": "skip"},  # a model may not hide a table
                ],
                "model": "example-model",
                "actions_text": {"ignore previous instructions": "buy"},
            },
        )

    found = activity_hints(
        _settings(),
        ["Periodic Investment Plan", "Bank sweep credit ref 4471", "periodic investment plan"],
        ["Sweep Program Activity", "Estimated Cash Flow"],
        "EXAMPLE BROKERAGE\n06/05 EXAMPLE INDEX ETF Periodic Investment Plan 3 -300.00",
        transport=httpx.MockTransport(handler),
    )
    assert found == Vocabulary(
        actions={"periodic investment plan": "buy"},
        sections={"sweep program activity": "cash"},
        model="example-model",
    )
    (request,) = seen
    assert str(request.url) == "http://ml:3003/v1/vocabulary"
    assert request.headers["Authorization"] == "Bearer " + "t" * 40
    # The phrases as keys (normalised, digits masked, each once) and the
    # statement to read them in; never ledger data.
    assert json.loads(request.content) == {
        "actions": ["periodic investment plan", "bank sweep credit ref 0"],
        "headings": ["sweep program activity", "estimated cash flow"],
        "text": "EXAMPLE BROKERAGE\n06/05 EXAMPLE INDEX ETF Periodic Investment Plan 3 -300.00",
    }


def test_the_statement_sent_as_context_is_capped() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"actions": [{"index": 1, "type": "buy"}], "headings": []})

    settings = Settings(ml_url="http://ml:3003", ml_token=SecretStr("t" * 40), ml_max_chars=1000)
    found = activity_hints(
        settings,
        ["periodic investment plan"],
        [],
        "x" * 5000,
        transport=httpx.MockTransport(handler),
    )
    assert found is not None
    assert len(bodies[0]["text"]) == 1000


def test_no_listed_kind_or_no_phrase_is_no_vocabulary() -> None:
    assert validated_vocabulary({"actions": [], "headings": []}, ["a"], ["b"]) is None
    assert validated_vocabulary("buy everything", ["a"], []) is None

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never called
        raise AssertionError("called")

    transport = httpx.MockTransport(handler)
    assert activity_hints(_settings(), ["  ", ""], [], transport=transport) is None
    assert activity_hints(Settings(), ["Periodic investment plan"], [], transport=transport) is None


def test_requests_are_bounded_and_failures_are_silent() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(502, json={"error": "runtime_failed"})

    transport = httpx.MockTransport(handler)
    many = [f"phrase {chr(97 + i % 26)}{chr(97 + i // 26)}" for i in range(MAX_PHRASES + 25)]
    assert activity_hints(_settings(), many, [], transport=transport) is None
    assert len(bodies[0]["actions"]) == MAX_PHRASES
    for _ in range(client.BREAKER_FAILURES + 2):
        assert activity_hints(_settings(), ["x"], [], transport=transport) is None
    assert len(bodies) == client.BREAKER_FAILURES  # then not asked for a while


# ---------------------------------------------------------------------------
# What a person may confirm with "import as read"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "fields", "allowed"),
    [
        (LINES_HELD, ["suggested_type"], True),
        (LOW_CONFIDENCE, ["suggested_type"], True),
        (LINES_HELD, ["reconciliation", "suggested_type"], True),
        (LINES_HELD, ["suggested_type", "trade_date"], False),
        (LOW_CONFIDENCE, [], False),
        (LOW_CONFIDENCE, ["amount"], False),
        (RECONCILIATION_FAILED, None, True),
    ],
)
def test_a_suggested_kind_is_confirmed_by_importing_as_read(
    reason: str, fields: list[str] | None, allowed: bool
) -> None:
    assert acceptable_as_read({"reason": reason, "fields": fields}) is allowed


# ---------------------------------------------------------------------------
# Column headings of a table whose header the parser cannot use
# ---------------------------------------------------------------------------

_UNKNOWN_TABLE: list[ViewLine] = [
    _line(300, [(32, "Sweep Program Activity")]),
    # No column the parser knows as the date or the amount.
    _line(313, [(32, "Posted"), (72, "Description"), (640, "Money In/Out")]),
    _line(326, [(32, "6/3"), (72, "Example sweep credit"), (645, "$40.00")]),
    _line(339, [(32, "6/8"), (72, "Example sweep debit"), (645, "-15.00")]),
]
_COLUMNS = {"posted": "date", "money in out": "amount"}


def test_column_keys_are_words_without_digits() -> None:
    assert column_key("Money In/Out") == "money in out"
    assert column_key("  POSTED ") == "posted"
    assert column_key("2026 Total") is None  # a figure, not a heading
    assert column_key("a b c d e f g") is None  # a sentence, not a heading
    assert column_key("$$$") is None


def test_an_unknown_header_is_reported_and_its_table_left_unread() -> None:
    collected, rows, _ = _read([*_TRADES, *_UNKNOWN_TABLE], Vocabulary())
    # The table above is read as ever; the unknown one is not, and its
    # headings are what the model may be asked about.
    assert [r.type for r in rows] == ["buy"]
    assert collected.unknown_columns == ["posted", "money in out"]
    assert len(collected.unread) == 2
    # A header the parser reads on its own reports nothing.
    known, _, _ = _read(_TRADES)
    assert known.unknown_columns == []


def test_a_suggested_column_reads_the_table_and_marks_every_row() -> None:
    words = Vocabulary(sections={"sweep program activity": "cash"}, columns=_COLUMNS)
    collected, rows, scores = _read([*_TRADES, *_UNKNOWN_TABLE], words)
    assert collected.unknown_columns == []
    assert collected.unread == []
    sweep = [(r.type, r.amount, r.suggestion) for r in rows if r.type != "buy"]
    # The amounts and dates are the page's own; the reading is the model's
    # suggestion, so each row carries the doubt and waits for a person.
    assert [(kind, amount) for kind, amount, _ in sweep] == [
        ("deposit", Decimal("40.00")),
        ("withdrawal", Decimal("-15.00")),
    ]
    assert all(marker is not None for _, _, marker in sweep)
    doubted = [
        SUGGESTED_DOUBT in score
        for row, score in zip(rows, scores, strict=True)
        if row.type != "buy"
    ]
    assert doubted == [True, True]
    # The parser's own table is untouched by the suggestion.
    assert [
        SUGGESTED_DOUBT in score
        for row, score in zip(rows, scores, strict=True)
        if row.type == "buy"
    ] == [False]


def test_a_learned_column_reads_the_table_without_doubt() -> None:
    words = Vocabulary(
        learned_sections={"sweep program activity": "cash"}, learned_columns=_COLUMNS
    )
    _, rows, scores = _read(_UNKNOWN_TABLE, words)
    assert [(r.type, r.amount) for r in rows] == [
        ("deposit", Decimal("40.00")),
        ("withdrawal", Decimal("-15.00")),
    ]
    assert not any(SUGGESTED_DOUBT in score for score in scores)
    assert all(r.suggestion is None for r in rows)


def test_a_suggestion_never_renames_a_column_the_parser_knows() -> None:
    # "date" is the parser's own word: a model saying it is the amount is ignored.
    words = Vocabulary(columns={"date": "amount", "transaction total": "date"})
    _, rows, scores = _read(_TRADES, words)
    assert [(r.type, r.amount) for r in rows] == [("buy", Decimal("-100.00"))]
    assert not any(SUGGESTED_DOUBT in score for score in scores)


def test_only_listed_column_roles_are_kept_and_asked() -> None:
    stored = Vocabulary.from_stored(
        {"columns": {"posted": "date", "running total": "balance", "Money In": "amount"}}
    )
    assert stored.columns == {"posted": "date"}  # no "balance" role, no key PerFin would not make
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "actions": [],
                "headings": [],
                "columns": [
                    {"index": 1, "role": "date"},
                    {"index": 2, "role": "other"},  # a model may not hide a column
                    {"index": 7, "role": "amount"},  # no such heading
                ],
            },
        )

    found = activity_hints(
        _settings(),
        [],
        [],
        "Posted Description Money In/Out",
        ["Posted", "Money In/Out", "Ref 2026"],
        transport=httpx.MockTransport(handler),
    )
    assert found == Vocabulary(columns={"posted": "date"})
    assert seen[0]["columns"] == ["posted", "money in out"]

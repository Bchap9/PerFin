# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scrubber leak corpus: zero leaks, and ordinary merchants survive (M2, E1).

``tests/security/ai/corpus/*.yaml`` holds known-bad strings per identity
class (account numbers, government ids, e-mail, phone, addresses, member
names, the custom denylist) with the secrets that must not survive. Every
case is checked as written **and** under each obfuscation in
:data:`VARIANTS` (full-width and other-script digits, zero-width joiners and
spaces, soft hyphens, non-breaking spaces, combining marks, mixed case), so
the corpus effectively has several hundred known-bad strings. The leak count
must be zero. A case marked ``any_placeholder`` may be caught by another
class first (a member's e-mail is an ``[EMAIL]``, a bare ten-digit number an
``[ACCT]``); it must still not leak. ``false_positives.yaml`` lists merchant strings that must come
out unchanged (the store-number policy is documented there and in
``app/core/scrubber.py``).
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.scrubber import MAX_INPUT, PLACEHOLDERS, Scrubber, member_terms, normalise
from app.mcp.scrub import ResponseScrubber

pytestmark = pytest.mark.security

CORPUS = Path(__file__).parent / "corpus"
CLASS_FILES = ("account", "govid", "email", "phone", "address", "member", "custom")


def _load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((CORPUS / f"{name}.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def corpus_scrubber() -> Scrubber:
    config = _load("config")
    members = config["members"]
    return Scrubber(
        member_terms(
            names=[m["display_name"] for m in members],
            usernames=[m["username"] for m in members],
            emails=[m["email"] for m in members],
            aliases=config["aliases"],
        ),
        config["denylist"],
        account_numbers=config["account_numbers"],
    )


SCRUBBER = corpus_scrubber()
#: The MCP process's second stage: fixed patterns only, no household terms.
MCP_STAGE = ResponseScrubber()
#: Classes the fixed patterns cover (members and the denylist need terms).
PATTERN_CLASS_FILES = ("account", "govid", "email", "phone", "address")

# --- obfuscations ----------------------------------------------------------

_ZW = ("‍", "​", "­", "⁠")


def _fullwidth(text: str) -> str:
    return "".join(
        chr(ord(ch) + 0xFEE0) if "!" <= ch <= "~" and ch not in "[]" else ch for ch in text
    )


def _script_digits(base: int) -> Callable[[str], str]:
    def convert(text: str) -> str:
        return "".join(chr(base + int(ch)) if ch.isdigit() else ch for ch in text)

    return convert


def _interleave(mark: str) -> Callable[[str], str]:
    def convert(text: str) -> str:
        return mark.join(text)

    return convert


def _mixed_case(text: str) -> str:
    return "".join(ch.upper() if i % 2 else ch.lower() for i, ch in enumerate(text))


def _combining(text: str) -> str:
    return "".join(ch + "̲" if ch.isalnum() else ch for ch in text)


VARIANTS: dict[str, Callable[[str], str]] = {
    "plain": lambda t: t,
    "fullwidth": _fullwidth,
    "arabic_indic_digits": _script_digits(0x0660),
    "devanagari_digits": _script_digits(0x0966),
    "mathematical_digits": _script_digits(0x1D7CE),
    "zwj": _interleave(_ZW[0]),
    "zwsp": _interleave(_ZW[1]),
    "soft_hyphen": _interleave(_ZW[2]),
    "word_joiner": _interleave(_ZW[3]),
    "nbsp": lambda t: t.replace(" ", "\u00a0"),
    "upper": str.upper,
    "lower": str.lower,
    "mixed_case": _mixed_case,
    "combining_underline": _combining,
    "padded": lambda t: f"  ..{t}..  ",
}


def _digits(text: str) -> str:
    return re.sub(r"[^0-9]", "", text)


def _squash(text: str) -> str:
    folded = normalise(text).casefold()
    for placeholder in PLACEHOLDERS:
        folded = folded.replace(placeholder.casefold(), " ")
    return re.sub(r"[\W_]+", "", folded)


def leaked(kind: str, output: str, secrets: list[str]) -> list[str]:
    """Secrets visible in ``output`` (numbers: digits anywhere; text: separators ignored)."""
    found = []
    for secret in secrets:
        if kind == "digits":
            if _digits(secret) in _digits(output):
                found.append(secret)
        elif _squash(secret) in _squash(output):
            found.append(secret)
    return found


def _cases() -> list[Any]:
    params = []
    for name in CLASS_FILES:
        data = _load(name)
        for index, case in enumerate(data["cases"]):
            params.append(
                pytest.param(name, data["kind"], data["placeholder"], case, id=f"{name}-{index}")
            )
    return params


CASES = _cases()


def test_corpus_is_substantial() -> None:
    assert len(CASES) >= 150
    assert len(CASES) * len(VARIANTS) >= 2000


@pytest.mark.parametrize(("name", "kind", "placeholder", "case"), CASES)
def test_known_bad_strings_do_not_leak(
    name: str, kind: str, placeholder: str, case: dict[str, Any]
) -> None:
    failures = []
    for variant, convert in VARIANTS.items():
        text = convert(case["text"])
        out = SCRUBBER.scrub_text(text)
        leaks = leaked(kind, out, case["secrets"])
        if leaks:
            failures.append(f"{variant}: {text!r} -> {out!r} leaks {leaks}")
        elif not any(p in out for p in PLACEHOLDERS):
            failures.append(f"{variant}: {text!r} -> {out!r} has no placeholder")
    plain = SCRUBBER.scrub_text(case["text"])
    if not case.get("any_placeholder") and placeholder not in plain:
        failures.append(f"plain: {case['text']!r} -> {plain!r} lacks {placeholder}")
    assert not failures, "\n".join(failures)


def test_zero_leaks_overall() -> None:
    """One number for the build log: total leaks across corpus and variants."""
    total = 0
    checked = 0
    for name in CLASS_FILES:
        data = _load(name)
        for case in data["cases"]:
            for convert in VARIANTS.values():
                checked += 1
                out = SCRUBBER.scrub_text(convert(case["text"]))
                total += len(leaked(data["kind"], out, case["secrets"]))
    assert checked >= 2000
    assert total == 0


def _pattern_cases() -> list[Any]:
    params = []
    for name in PATTERN_CLASS_FILES:
        data = _load(name)
        for index, case in enumerate(data["cases"]):
            if case.get("needs_terms"):
                continue
            params.append(pytest.param(data["kind"], case, id=f"{name}-{index}"))
    return params


@pytest.mark.parametrize(("kind", "case"), _pattern_cases())
def test_mcp_stage_alone_does_not_leak(kind: str, case: dict[str, Any]) -> None:
    """The MCP second stage catches every pattern-class case on its own (M2).

    It runs the fixed patterns without the household's terms, so a case that
    needs them (a bare stored last four: ``needs_terms``) is the API stage's
    alone; every other case must be caught here too, in a key and a value.
    """
    failures = []
    for variant, convert in VARIANTS.items():
        text = convert(case["text"])
        out = MCP_STAGE.json({"memo": text, text: 1})
        assert isinstance(out, dict)
        rendered = " ".join([*map(str, out.values()), *out.keys()])
        leaks = leaked(kind, rendered, case["secrets"])
        if leaks:
            failures.append(f"{variant}: {text!r} -> {out!r} leaks {leaks}")
    assert not failures, "\n".join(failures)


FALSE_POSITIVES = _load("false_positives")["cases"]


@pytest.mark.parametrize("text", FALSE_POSITIVES)
def test_ordinary_merchants_survive(text: str) -> None:
    assert SCRUBBER.scrub_text(text) == unicodedata.normalize("NFC", text)


@pytest.mark.parametrize(("name", "kind", "placeholder", "case"), CASES)
def test_idempotent_on_corpus(name: str, kind: str, placeholder: str, case: dict[str, Any]) -> None:
    for convert in VARIANTS.values():
        once = SCRUBBER.scrub_text(convert(case["text"]))
        assert SCRUBBER.scrub_text(once) == once


def test_linear_time_on_hostile_input() -> None:
    """No pattern backtracks badly: worst-case shapes scrub fast (ReDoS)."""
    shapes = [
        "1 " * (MAX_INPUT // 2),
        "a" * MAX_INPUT,
        "a." * (MAX_INPUT // 2),
        "1-" * (MAX_INPUT // 2),
        "x" * (MAX_INPUT - 5) + "1234",
        ("12 Main " * 600)[:MAX_INPUT],
        ("acct " * 900)[:MAX_INPUT],
        ("a@" * (MAX_INPUT // 2)),
        ("jane " * 900)[:MAX_INPUT],
        ("." * (MAX_INPUT - 4)) + "1234",
        ("(1 " * 1400)[:MAX_INPUT],
        ("PO BOX " * 600)[:MAX_INPUT],
        ("A1 " * 1400)[:MAX_INPUT],
        ("ending in " * 450)[:MAX_INPUT],
        # P6B-SR-R2 shapes: grouped numbers after keywords, bracket and glued
        # keywords, spaced e-mails, spelled-out and digit-folded names.
        ("acct 1 " * 700)[:MAX_INPUT],
        ("card (" * 800)[:MAX_INPUT],
        ("visa" * 1100)[:MAX_INPUT],
        ("a . b @ " * 600)[:MAX_INPUT],
        ("a @ b . " * 600)[:MAX_INPUT],
        ("j a n e " * 600)[:MAX_INPUT],
        ("j.a.n." * 800)[:MAX_INPUT],
        ("D0E1 " * 900)[:MAX_INPUT],
    ]
    for text in shapes:
        start = time.perf_counter()
        SCRUBBER.scrub_text(text)
        assert time.perf_counter() - start < 1.0, text[:40]


def test_overlong_input_is_cut_and_marked() -> None:
    text = "ok " * MAX_INPUT + "4111 1111 1111 1111"
    out = SCRUBBER.scrub_text(text)
    assert out.endswith("[REDACTED]")
    assert "4111" not in out

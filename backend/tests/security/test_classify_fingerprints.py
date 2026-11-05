# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fingerprints are untrusted data: schema rejection and the ReDoS guard.

Shipped YAML and user-remembered layouts both go through the same strict
schema; patterns accept only a linear-time subset and are compiled with
possessive quantifiers. Hostile inputs must stay fast, both for fingerprint
patterns and for the classifier's own date/account/tax-year patterns.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import pytest

from app.ingestion.classify import accounts, dates, period
from app.ingestion.classify.fingerprints import (
    MAX_YAML_BYTES,
    FingerprintError,
    compile_fingerprint,
    match_fingerprint,
    parse_fingerprint_yaml,
    parse_match_rules,
)
from app.ingestion.classify.safe_regex import (
    MAX_PATTERN_CHARS,
    MAX_SUBJECT_CHARS,
    UnsafePatternError,
    compile_safe,
    rewrite_possessive,
    search_line,
)
from app.ingestion.classify.text import DocumentView

pytestmark = pytest.mark.security

VALID = """\
schema: 1
key: test-bank-statement
institution: Test Fictional Bank
kind: statement
parser: statement_generic
version: 1
match:
  all:
    - header: Test Fictional Bank
  any:
    - regex: 'statement period \\d{2}'
    - account_mask: 'XXXX-####'
    - columns: [Date, Description, Amount]
  min_any: 1
"""


def test_valid_fingerprint_parses_and_matches() -> None:
    spec = parse_fingerprint_yaml(VALID)
    fp = compile_fingerprint(spec)
    view = DocumentView.from_text("Test Fictional Bank\nStatement period 01 to 31\n")
    found = match_fingerprint(fp, view)
    assert found is not None
    assert found.confidence > 0.85


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda t: t.replace("schema: 1", "schema: 2"), "schema"),
        (lambda t: t + "unknown_key: 1\n", "unknown_key"),
        (lambda t: t.replace("kind: statement", "kind: unknown"), "kind"),
        (lambda t: t.replace("kind: statement", "kind: password"), "kind"),
        (lambda t: t.replace("key: test-bank-statement", "key: ../../etc"), "key"),
        (lambda t: t.replace("parser: statement_generic", "parser: os.system"), "parser"),
        (lambda t: t.replace("version: 1", "version: 0"), "version"),
        (lambda t: t.replace("version: 1", "version: '1'"), "version"),
        (lambda t: t.replace("    - header: Test Fictional Bank\n", ""), "all"),
        (lambda t: t.replace("min_any: 1", "min_any: 9"), "min_any"),
        (
            lambda t: t.replace(
                "- header: Test Fictional Bank", "- {header: Test Fictional Bank, text: x y}"
            ),
            "exactly one",
        ),
        (lambda t: t.replace("'statement period \\d{2}'", "'(a+)+$'"), "unsafe regex"),
        (lambda t: t.replace("'XXXX-####'", "'XXXX-##'"), "mask"),
        (lambda t: t.replace("'XXXX-####'", "'ABCD-####'"), "mask"),
        (lambda t: t.replace("[Date, Description, Amount]", "[Date]"), "columns"),
        (
            lambda t: t.replace("institution: Test Fictional Bank", "institution: ' x'"),
            "institution",
        ),
    ],
)
def test_schema_rejections(change: Callable[[str], str], message: str) -> None:
    with pytest.raises(FingerprintError, match=message):
        parse_fingerprint_yaml(change(VALID))


@pytest.mark.parametrize(
    "text",
    [
        "- just a list\n",
        "plain string",
        "",
        "schema: 1\nkey: !!python/object/apply:os.system ['true']\n",
        "a: &x [1]\nb: *x\n",
        # An alias bomb: refused before anything is expanded.
        'a: &a ["x","x"]\nb: &b [*a,*a]\nc: &c [*b,*b]\nd: [*c,*c]\n',
        "a: [unclosed\n",
    ],
)
def test_hostile_yaml_is_refused(text: str) -> None:
    with pytest.raises(FingerprintError):
        parse_fingerprint_yaml(text)


def test_oversized_and_non_utf8_yaml_are_refused() -> None:
    with pytest.raises(FingerprintError, match="too large"):
        parse_fingerprint_yaml(VALID + "#" * MAX_YAML_BYTES)
    with pytest.raises(FingerprintError, match="UTF-8"):
        parse_fingerprint_yaml(b"\xff\xfe" + VALID.encode())


def test_stored_match_rules_use_the_same_schema() -> None:
    assert parse_match_rules({"all": [{"header": "Some Bank"}]}).all[0].header == "Some Bank"
    bad: object
    for bad in (
        {},
        {"all": []},
        {"all": [{"regex": "(?=x)"}]},
        {"all": [{"header": "x" * 500}]},
        {"all": [{"header": "Bank"}], "extra": True},
        {"all": [{"header": "Bank"}] * 40},
        ["not", "a", "mapping"],
    ):
        with pytest.raises(FingerprintError):
            parse_match_rules(bad)


# ---------------------------------------------------------------------------
# ReDoS guard
# ---------------------------------------------------------------------------

UNSAFE = [
    "(a+)+$",
    "(a*)*b",
    "(a|aa)*c",
    "(a|a)+",
    "(.*a){20}",
    "(\\w+\\s?)+$",
    "(x+x+)+y",
    "(a)\\1",
    "(?=a)a",
    "(?!a)b",
    "(?<=a)b",
    "(?<!a)b",
    "(?i)abc",
    "(?>a+)b",
    "a+?",
    "a*+",
    "a{1,1000}",
    "a{5,2}",
    "[[a]]",
    "a{",
    "(abc",
    "abc)",
    "\\",
    "x" * 300,
    "\\N{BULLET}",
    "\\x41",
    "(?P<1bad>a)",
    "a" + "b?" * 13,
    "",
    "*a",
]


@pytest.mark.parametrize("pattern", UNSAFE)
def test_unsafe_patterns_are_rejected(pattern: str) -> None:
    with pytest.raises(UnsafePatternError):
        compile_safe(pattern)


@pytest.mark.parametrize(
    ("pattern", "rewritten"),
    [
        ("statement period \\d{2}", "statement period \\d{2}+"),
        ("card ending in \\d{4}", "card ending in \\d{4}+"),
        ("acct[ -]?no\\.? ?\\d+", "acct[ -]?+no\\.?+ ?+\\d++"),
        ("(?:member|share) statement", "(?>(?:member|share)) statement"),
        ("(?P<m>jan|feb) \\d+", "(?>(?P<m>jan|feb)) \\d++"),
        ("(form|statement) (?:a(?:b|c)d)", "(?>(form|statement)) (?:a(?>(?:b|c))d)"),
        ("w-2|1099", "(?>w-2|1099)"),
        ("(?P<year>\\d{4}) form", "(?P<year>\\d{4}+) form"),
        ("^[A-Z]{2,10}$", "^[A-Z]{2,10}+$"),
    ],
)
def test_accepted_patterns_become_possessive(pattern: str, rewritten: str) -> None:
    assert rewrite_possessive(pattern) == rewritten
    compile_safe(pattern)


HOSTILE = [
    "a" * 5000,
    "a" * 5000 + "!",
    " " * 5000,
    "1" * 5000,
    "x-" * 2500,
    ("aaaa" * 50 + "\t") * 20,
    "*" * 5000 + "1234",
    "x" * 5000 + "1",
    "Jan " * 1200,
    "1/" * 2500,
    "statement period " * 300,
]

ACCEPTED_WORST = [
    "\\w+\\s*\\w+\\s*\\w+\\s*!",
    "a*a*a*a*a*a*b",
    "[a-z]+[a-z]+[a-z]+\\d",
    "(?:ab|a)c",
    "(?:a|b|c)(?:a|b|c)(?:a|b|c)z",
    ".*.*.*x",
    "\\d+\\d+\\d+x",
]


def test_alternation_cannot_backtrack_exponentially() -> None:
    """P34-SR-01: ``(?:a|a)`` x n + ``b`` was 2^n steps with plain groups.

    No quantifier at all, 253 characters: before alternation was compiled
    atomic this search took about 26 s at n=22 and would never end at n=36.
    """
    for n in (16, 22, 36):
        pattern = "(?:a|a)" * n + "b"
        assert len(pattern) <= MAX_PATTERN_CHARS
        compiled = compile_safe(pattern)
        started = time.perf_counter()
        assert search_line(compiled, "a" * MAX_SUBJECT_CHARS) is None
        assert time.perf_counter() - started < 0.5, n
    # The same shape with capturing, named and nested groups and a top-level "|".
    for pattern in (
        "(a|a)" * 36 + "b",
        "(?P<g>a|a)" + "(?:(?:a|a)(?:a|a)|a)" * 11 + "b",
        "(?:a|a)" * 30 + "b|c",
        "(?:(?:a|a)(?:a|a)|(?:a|a)a)" * 9 + "b",
    ):
        assert len(pattern) <= MAX_PATTERN_CHARS, pattern
        compiled = compile_safe(pattern)
        started = time.perf_counter()
        search_line(compiled, "a" * MAX_SUBJECT_CHARS)
        assert time.perf_counter() - started < 0.5, pattern


def _random_pattern(rng: random.Random, depth: int = 0) -> str:
    """A random pattern built from the accepted subset's pieces."""
    atoms = ["a", "b", ".", "\\w", "\\d", "\\s", "[ab]", "[^b]"]
    quants = ["", "", "", "*", "+", "?", "{2,5}", "{3}"]
    parts: list[str] = []
    for _ in range(rng.randint(1, 4)):
        if depth < 3 and rng.random() < 0.4:
            alternatives = [_random_pattern(rng, depth + 1) for _ in range(rng.randint(1, 3))]
            parts.append("(?:" + "|".join(alternatives) + ")")
        elif rng.random() < 0.1:
            parts.append(rng.choice(["^", "$", "\\b"]))
        else:
            parts.append(rng.choice(atoms) + rng.choice(quants))
    return "".join(parts)


def test_random_accepted_patterns_stay_fast() -> None:
    """Any accepted pattern searches a hostile line in bounded time (seeded)."""
    rng = random.Random(0x5EED)
    subjects = ["a" * MAX_SUBJECT_CHARS, "ab" * 150, "a " * 150, "1a" * 150]
    accepted = 0
    started = time.perf_counter()
    for _ in range(3000):
        pattern = "".join(_random_pattern(rng) for _ in range(rng.randint(1, 3))) + "!"
        try:
            compiled = compile_safe(pattern)
        except UnsafePatternError:
            continue
        accepted += 1
        one = time.perf_counter()
        for subject in subjects:
            search_line(compiled, subject)
        assert time.perf_counter() - one < 0.2, pattern
    assert accepted > 500
    assert time.perf_counter() - started < 30


def test_atomic_alternation_keeps_matching() -> None:
    assert search_line(compile_safe("(?:member|share) statement"), "Share Statement 2026")
    match = search_line(compile_safe("(?P<m>jan|feb) (?P<d>\\d+)"), "due feb 12")
    assert match is not None
    assert (match.group("m"), match.group("d")) == ("feb", "12")
    assert search_line(compile_safe("w-2|1099"), "Form 1099-INT")
    # The first alternative that matches wins: longer alternatives go first.
    assert search_line(compile_safe("(?:ab|a)c"), "xabc")
    assert search_line(compile_safe("(?:a|ab)c"), "xabc") is None


@pytest.mark.parametrize("pattern", ACCEPTED_WORST)
def test_accepted_patterns_stay_fast_on_hostile_lines(pattern: str) -> None:
    compiled = compile_safe(pattern)
    started = time.perf_counter()
    for subject in HOSTILE:
        search_line(compiled, subject)
    assert time.perf_counter() - started < 0.5


def test_search_sees_only_a_capped_line() -> None:
    compiled = compile_safe("needle")
    assert search_line(compiled, "x" * MAX_SUBJECT_CHARS + "needle") is None


def test_classifier_patterns_stay_fast_on_hostile_text() -> None:
    lines = "\n".join(HOSTILE)
    view = DocumentView.from_text(lines)
    started = time.perf_counter()
    for subject in HOSTILE:
        dates.find_dates(subject)
    period.detect_period(view)
    period.detect_printed_date(view)
    period.detect_tax_year(view)
    accounts.detect_last4(view, (accounts.mask_regex("XXXX-XXXX-XXXX-####"),))
    assert time.perf_counter() - started < 2.0


def test_fingerprint_matching_is_bounded_on_a_huge_document() -> None:
    fp = compile_fingerprint(parse_fingerprint_yaml(VALID))
    view = DocumentView.from_text("\n".join(["a" * 10_000] * 10_000))
    assert len(view.lines) <= 4000
    assert all(len(line) <= 300 for line in view.lines)
    started = time.perf_counter()
    assert match_fingerprint(fp, view) is None
    assert time.perf_counter() - started < 2.0

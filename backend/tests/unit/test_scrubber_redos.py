# SPDX-License-Identifier: AGPL-3.0-or-later
"""The log/audit scrubber runs in linear time on hostile input (SECURITY.md H8).

Log messages, audit values and exception text are attacker-influenced. A
pattern that backtracks quadratically turns one long header or form value
into seconds of CPU per log line (an earlier version took 8-31 s on 20 000
characters). Each case here is 50 000 characters; linear patterns finish in
milliseconds, a quadratic one takes minutes, so the 2 s budget is not a
flaky threshold.
"""

from __future__ import annotations

import time

import pytest

from app.core.logging import scrub_text

SIZE = 50_000
BUDGET_SECONDS = 2.0

SEEDS = (
    "x",
    "X",
    ".",
    "*",
    "#",
    "\u2022",
    "\u2026",
    "1-",
    "a.",
    "a@",
    "%40",
    "a%",
    "_",
    "-",
    " ",
    "\t",
    "password",
    "'password'",
    "password=",
    "a_password",
    "secret-",
    "account ",
    "account",
    "ending in ",
    "dob ",
    "born:",
    "a://b:",
    "http://",
    "eyJa.",
    "xx1",
    "..1",
    "1234 ",
    "AB12 ",
    "+1 ",
    "(555) ",
    "'cookie', ",
    "authorization: ",
    "bearer ",
)
PREFIXES = ("", "account", "dob", "password", "'", "http://", "user")


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("seed", SEEDS)
def test_scrub_text_is_linear_on_hostile_input(prefix: str, seed: str) -> None:
    text = prefix + seed * (SIZE // len(seed))
    started = time.perf_counter()
    scrub_text(text)
    elapsed = time.perf_counter() - started
    assert elapsed < BUDGET_SECONDS, f"{elapsed:.2f}s for {prefix!r}+{seed!r}*N"


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        # Run starts that the old patterns also caught.
        ("card xxxx1234 on file", "1234"),
        ("boxx4321", "4321"),
        ("acct ...9876 closed", "9876"),
        ("api_key=s3cr3tvalue", "s3cr3tvalue"),
        ("--password=hunter2hunter2", "hunter2hunter2"),
        ("cfg.db_password: 'pa55word!'", "pa55word!"),
        ("headers=[('x-csrf-token', 'tok3nvalue')]", "tok3nvalue"),
        ("mail .first.last@example.com now", "first.last@example.com"),
        ("user%40example.com", "user%40example.com"),
        ("dsn=x_postgresql://perfin:pw0rdpw0rd@db/perfin", "pw0rdpw0rd"),
        ("account   #   55667788", "55667788"),
        ("dob   :   1990-01-02", "1990-01-02"),
    ],
)
def test_linear_patterns_still_redact(raw: str, secret: str) -> None:
    assert secret not in scrub_text(raw)

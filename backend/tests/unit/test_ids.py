# SPDX-License-Identifier: AGPL-3.0-or-later
"""UUIDv7 generator: layout, ordering and monotonicity."""

from __future__ import annotations

import threading
import time
import uuid
from itertools import pairwise

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core import ids
from app.core.ids import build_uuid7, uuid7, uuid7_datetime, uuid7_unix_ms


def test_version_and_variant() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122
    assert (value.int >> 76) & 0xF == 7
    assert (value.int >> 62) & 0b11 == 0b10


def test_timestamp_is_current_milliseconds() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000
    # Monotonic carry may push the embedded time slightly ahead, never behind.
    assert before <= uuid7_unix_ms(value) <= after + 1
    assert uuid7_datetime(value).tzinfo is not None


def test_rfc9562_test_vector_layout() -> None:
    # RFC 9562 appendix A.6: 017F22E2-79B0-7CC3-98C4-DC0C0C07398F
    unix_ms = 0x017F22E279B0
    rand_a = 0xCC3
    rand_b = 0x18C4DC0C0C07398F
    tail = (rand_a << 62) | rand_b
    assert str(build_uuid7(unix_ms, tail)) == "017f22e2-79b0-7cc3-98c4-dc0c0c07398f"


def test_strictly_increasing_in_a_tight_loop() -> None:
    values = [uuid7() for _ in range(20_000)]
    assert all(a < b for a, b in pairwise(values))
    assert len(set(values)) == len(values)


def test_monotonic_when_clock_goes_backwards(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [time.time_ns()]
    monkeypatch.setattr(ids, "_clock_ns", lambda: now[0])
    first = uuid7()
    now[0] -= 5_000_000_000  # step the wall clock back five seconds
    second = uuid7()
    assert second > first
    assert uuid7_unix_ms(second) >= uuid7_unix_ms(first)


def test_tail_overflow_carries_into_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen = time.time_ns()
    monkeypatch.setattr(ids, "_clock_ns", lambda: frozen)
    first = uuid7()
    monkeypatch.setattr(ids, "_last_tail", (1 << 74) - 1)
    second = uuid7()
    assert second > first
    assert uuid7_unix_ms(second) == uuid7_unix_ms(first) + 1


def test_thread_safety_no_duplicates() -> None:
    results: list[uuid.UUID] = []
    lock = threading.Lock()

    def worker() -> None:
        local = [uuid7() for _ in range(2_000)]
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == len(results) == 16_000


@given(
    unix_ms=st.integers(min_value=0, max_value=(1 << 48) - 1),
    tail=st.integers(min_value=0, max_value=(1 << 74) - 1),
)
def test_build_roundtrip(unix_ms: int, tail: int) -> None:
    value = build_uuid7(unix_ms, tail)
    assert value.version == 7
    assert value.variant == uuid.RFC_4122
    assert uuid7_unix_ms(value) == unix_ms


@pytest.mark.parametrize(("unix_ms", "tail"), [(-1, 0), (1 << 48, 0), (0, -1), (0, 1 << 74)])
def test_build_rejects_out_of_range(unix_ms: int, tail: int) -> None:
    with pytest.raises(ValueError, match="out of range"):
        build_uuid7(unix_ms, tail)


def test_unix_ms_rejects_other_versions() -> None:
    with pytest.raises(ValueError, match="not a UUIDv7"):
        uuid7_unix_ms(uuid.uuid4())

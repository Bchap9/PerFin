# SPDX-License-Identifier: AGPL-3.0-or-later
"""UUIDv7 generation (RFC 9562, section 5.7).

Layout: 48-bit Unix epoch milliseconds, 4-bit version (7), 12 bits ``rand_a``,
2-bit variant (``10``), 62 bits ``rand_b``. The 74 ``rand_a``/``rand_b`` bits
are treated as one monotonic random field (RFC 9562 section 6.2, method 2):
a fresh random value each new millisecond, otherwise the previous value plus a
random increment. Values are therefore strictly increasing within a process,
even when the wall clock stalls or steps backwards.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

_TAIL_BITS = 74
_TAIL_LIMIT = 1 << _TAIL_BITS
_RAND_B_BITS = 62
_RAND_B_MASK = (1 << _RAND_B_BITS) - 1
_MS_LIMIT = 1 << 48

# Nanosecond wall clock; a module attribute so tests can simulate clock steps.
_clock_ns: Callable[[], int] = time.time_ns

_lock = threading.Lock()
_last_ms = -1
_last_tail = 0


def _fresh_tail() -> int:
    # Top bit cleared so increments within one millisecond rarely overflow.
    return secrets.randbits(_TAIL_BITS - 1)


def _next_fields(now_ms: int) -> tuple[int, int]:
    global _last_ms, _last_tail
    with _lock:
        if now_ms > _last_ms:
            ms, tail = now_ms, _fresh_tail()
        else:
            ms, tail = _last_ms, _last_tail + 1 + secrets.randbits(32)
            if tail >= _TAIL_LIMIT:
                ms, tail = _last_ms + 1, _fresh_tail()
        _last_ms, _last_tail = ms, tail
        return ms, tail


def build_uuid7(unix_ms: int, tail: int) -> uuid.UUID:
    """Assemble a UUIDv7 from a millisecond timestamp and 74 bits of entropy."""
    if not 0 <= unix_ms < _MS_LIMIT:
        raise ValueError("timestamp out of range for UUIDv7")
    if not 0 <= tail < _TAIL_LIMIT:
        raise ValueError("tail out of range for UUIDv7")
    rand_a = tail >> _RAND_B_BITS
    rand_b = tail & _RAND_B_MASK
    value = (unix_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)


def uuid7() -> uuid.UUID:
    """Return a new UUIDv7, strictly greater than any previous one from this process."""
    ms, tail = _next_fields(_clock_ns() // 1_000_000)
    return build_uuid7(ms, tail)


def uuid7_unix_ms(value: uuid.UUID) -> int:
    """Extract the embedded Unix timestamp in milliseconds."""
    if value.version != 7:
        raise ValueError("not a UUIDv7")
    return value.int >> 80


def uuid7_datetime(value: uuid.UUID) -> datetime:
    """Extract the embedded timestamp as an aware UTC datetime."""
    return datetime.fromtimestamp(uuid7_unix_ms(value) / 1000, tz=UTC)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure parts of the SSE stream: resume points, stream slots, wire format."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.core.ids import uuid7, uuid7_unix_ms
from app.domain.events.service import event_frame, frame, named_accounts
from app.domain.events.stream import StreamSlots, _Cursor, start_point, uuid7_floor

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
RETENTION = timedelta(days=7)


def test_uuid7_floor_is_below_every_id_of_that_millisecond() -> None:
    floor = uuid7_floor(NOW)
    assert uuid7_unix_ms(floor) == int(NOW.timestamp() * 1000)
    assert floor.version == 7
    assert uuid7_floor(datetime.now(UTC) - timedelta(seconds=1)) < uuid7()


def test_start_point() -> None:
    fresh = start_point(None, retention=RETENTION, now=NOW)
    assert fresh.cursor == uuid7_floor(NOW)
    assert not fresh.resync
    recent = uuid7_floor(NOW - timedelta(hours=1))
    resumed = start_point(str(recent), retention=RETENTION, now=NOW)
    assert resumed.cursor == recent
    assert not resumed.resync
    for bad in (
        "garbage",
        str(uuid.uuid4()),  # not a UUIDv7
        str(uuid7_floor(NOW - timedelta(days=8))),  # older than retention
        str(uuid7_floor(NOW + timedelta(hours=1))),  # from the future
    ):
        point = start_point(bad, retention=RETENTION, now=NOW)
        assert point.resync, bad
        assert point.cursor == uuid7_floor(NOW)


def test_cursor_never_goes_before_its_start_and_rereads_the_grace_window() -> None:
    start = uuid7_floor(NOW - timedelta(seconds=10))
    cursor = _Cursor(start=start, position=start)
    assert cursor.lower_bound(NOW, timedelta(seconds=60)) == start
    later = uuid7_floor(NOW + timedelta(minutes=5))
    cursor.position = later
    assert cursor.lower_bound(NOW + timedelta(minutes=5), timedelta(seconds=60)) == uuid7_floor(
        NOW + timedelta(minutes=4)
    )


def test_stream_slots_cap_release_and_expire() -> None:
    slots = StreamSlots()
    user = uuid.uuid4()
    first = slots.acquire(user, limit=2, lease_seconds=60)
    second = slots.acquire(user, limit=2, lease_seconds=60)
    assert first is not None
    assert second is not None
    assert slots.acquire(user, limit=2, lease_seconds=60) is None
    assert slots.acquire(uuid.uuid4(), limit=2, lease_seconds=60) is not None
    slots.release(user, first)
    assert slots.count(user) == 1
    assert slots.acquire(user, limit=2, lease_seconds=60) is not None
    # A lease that ran out frees its slot even without a release.
    lapsed = uuid.uuid4()
    assert slots.acquire(lapsed, limit=1, lease_seconds=-1) is not None
    assert slots.acquire(lapsed, limit=1, lease_seconds=60) is not None


def test_frames_carry_id_and_type_only() -> None:
    event_id = uuid7()

    row = SimpleNamespace(
        id=event_id, type="document.updated", payload={"id": "x", "memo": "Synthetic secret"}
    )
    raw = event_frame(row).decode()  # type: ignore[arg-type]
    assert raw == (
        f"id: {event_id}\ndata: "
        + json.dumps({"id": str(event_id), "type": "document.updated"}, separators=(",", ":"))
        + "\n\n"
    )
    assert "secret" not in raw
    assert frame(None, {"type": "stream.resync"}) == b'data: {"type":"stream.resync"}\n\n'


def test_named_accounts() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    assert named_accounts({"account_id": str(a), "account_ids": [str(b), "junk", 3]}) == {a, b}
    assert named_accounts({"id": str(a)}) == set()

# SPDX-License-Identifier: AGPL-3.0-or-later
"""The deduplicator and import tombstones: a row a person deleted stays deleted.

Pure :func:`app.ingestion.match.dedupe.plan` cases (no database). Every row
is invented.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from app.domain.transactions.import_hash import HashInput, assign_import_hashes
from app.ingestion.match.dedupe import Existing, Removed, plan

ACCOUNT = uuid.UUID("01900000-0000-7000-8000-0000000000d4")
D1, D2 = date(2026, 3, 4), date(2026, 3, 5)


def _rows(*items: tuple[date, str, str | None]) -> list[HashInput]:
    return [HashInput(d, Decimal(a), p) for d, a, p in items]


def _existing(row: HashInput, digest: str | None) -> Existing:
    return Existing(uuid.uuid4(), row.posted_date, row.amount, digest)


def test_a_tombstoned_hash_is_not_inserted() -> None:
    rows = _rows((D1, "-7.25", "Harbor Kiosk"), (D2, "-31.00", "Tin Roof Books"))
    hashes = assign_import_hashes(ACCOUNT, rows)
    result = plan(hashes, rows, [], tombstoned={hashes[0]})
    assert result.removed == [0]
    assert (result.exact, result.near, result.new) == ([], {}, [1])
    assert result.to_insert == [1]


def test_one_of_two_identical_rows_stays_deleted() -> None:
    rows = _rows((D1, "-7.25", "Harbor Kiosk"), (D1, "-7.25", "Harbor Kiosk"))
    hashes = assign_import_hashes(ACCOUNT, rows)
    assert hashes[0] != hashes[1]  # running index 0 and 1
    kept = _existing(rows[0], hashes[0])
    result = plan(hashes, rows, [kept], tombstoned=[hashes[1]])
    assert result.exact == [0]
    assert result.removed == [1]
    assert result.near == {}
    assert result.new == []
    assert result.to_insert == []


def test_the_deleted_twin_is_no_near_match_of_the_other() -> None:
    rows = _rows((D1, "-7.25", "Harbor Kiosk"), (D1, "-7.25", "Harbor Kiosk"))
    hashes = assign_import_hashes(ACCOUNT, rows)
    result = plan(hashes, rows, [], tombstoned={hashes[0]})
    assert result.removed == [0]
    assert result.new == [1]
    assert (result.exact, result.near) == ([], {})


def test_a_tombstoned_row_claims_no_existing_row() -> None:
    """A row typed by hand stays free to back the near match of another incoming row."""
    typed = _existing(HashInput(D1, Decimal("-7.25"), "kiosk"), None)
    rows = _rows((D1, "-7.25", "Harbor Kiosk"), (D1, "-7.25", "HARBOR KIOSK 0042"))
    hashes = assign_import_hashes(ACCOUNT, rows)
    result = plan(hashes, rows, [typed], tombstoned={hashes[0]})
    assert result.removed == [0]
    assert result.near == {1: [typed.id]}
    assert (result.exact, result.new) == ([], [])


def test_a_tombstoned_hash_that_is_also_in_the_ledger_is_removed_first() -> None:
    """The tombstone wins, and the ledger row it names is not claimed by it."""
    rows = _rows((D1, "-7.25", "Harbor Kiosk"), (D1, "-7.25", "HARBOR KIOSK 0042"))
    hashes = assign_import_hashes(ACCOUNT, rows)
    kept = _existing(rows[0], hashes[0])
    result = plan(hashes, rows, [kept], tombstoned={hashes[0]})
    assert result.removed == [0]
    assert result.exact == []
    assert result.near == {1: [kept.id]}
    assert result.new == []


def test_a_hash_that_is_not_tombstoned_is_untouched() -> None:
    """The negative: a tombstone for another row removes nothing."""
    rows = _rows((D1, "-7.25", "Harbor Kiosk"), (D2, "-31.00", "Tin Roof Books"))
    hashes = assign_import_hashes(ACCOUNT, rows)
    other = assign_import_hashes(ACCOUNT, _rows((D2, "-7.25", "Harbor Kiosk")))
    result = plan(hashes, rows, [], tombstoned=set(other))
    assert result.removed == []
    assert result.new == [0, 1]


# ---------------------------------------------------------------------------
# The deleted row arriving from another file, spelled another way
# ---------------------------------------------------------------------------

KIOSK = (D1, "-7.25", "Harbor Kiosk")
RESPELLED = (D1, "-7.25", "HARBOR KIOSK 0042 CARD PURCHASE")


def _stone(row: HashInput, digest: str) -> Removed:
    return Removed(digest, row.posted_date, row.amount)


def test_another_spelling_of_a_deleted_row_is_held_not_new() -> None:
    first = _rows(KIOSK)
    stone = _stone(first[0], assign_import_hashes(ACCOUNT, first)[0])
    rows = _rows(RESPELLED)
    hashes = assign_import_hashes(ACCOUNT, rows)
    assert hashes[0] != stone.import_hash  # the payee is part of the hash
    result = plan(hashes, rows, [], deleted=[stone])
    assert result.removed == []
    assert result.new == []
    # Held as a possible duplicate that names no live row, and listed apart.
    assert result.near == {0: []}
    assert result.resembles_removed == [0]
    assert result.to_insert == [0]


def test_the_respelled_deleted_twin_is_held_beside_the_kept_one() -> None:
    first = _rows(KIOSK, KIOSK)
    digests = assign_import_hashes(ACCOUNT, first)
    kept = _existing(first[1], digests[1])
    rows = _rows(RESPELLED, RESPELLED)
    result = plan(
        assign_import_hashes(ACCOUNT, rows), rows, [kept], deleted=[_stone(first[0], digests[0])]
    )
    assert result.near == {0: [kept.id], 1: []}
    assert result.resembles_removed == [1]
    assert (result.exact, result.new, result.removed) == ([], [], [])


def test_one_tombstone_holds_one_row() -> None:
    """A second purchase of that amount that day, with nothing deleted for it, is new."""
    first = _rows(KIOSK)
    stone = _stone(first[0], assign_import_hashes(ACCOUNT, first)[0])
    rows = _rows(RESPELLED, RESPELLED)
    result = plan(assign_import_hashes(ACCOUNT, rows), rows, [], deleted=[stone])
    assert result.near == {0: []}
    assert result.resembles_removed == [0]
    assert result.new == [1]


def test_another_amount_or_another_day_is_still_new() -> None:
    """The negative: a tombstone holds only a row of its own date and amount."""
    first = _rows(KIOSK)
    stone = _stone(first[0], assign_import_hashes(ACCOUNT, first)[0])
    rows = _rows((D1, "-7.52", RESPELLED[2]), (D2, "-7.25", RESPELLED[2]), (D1, "7.25", "Refund"))
    result = plan(assign_import_hashes(ACCOUNT, rows), rows, [], deleted=[stone])
    assert result.new == [0, 1, 2]
    assert (result.near, result.resembles_removed, result.removed) == ({}, [], [])


def test_a_tombstone_its_own_row_used_holds_no_other() -> None:
    """The same file again: the deleted twin is removed by its hash, and its
    tombstone does not also hold the twin that was never deleted."""
    rows = _rows(KIOSK, KIOSK)
    hashes = assign_import_hashes(ACCOUNT, rows)
    result = plan(hashes, rows, [], tombstoned={hashes[0]}, deleted=[_stone(rows[0], hashes[0])])
    assert result.removed == [0]
    assert result.new == [1]
    assert (result.near, result.resembles_removed) == ({}, [])
    # Named only in ``deleted``, the hash is tombstoned all the same.
    again = plan(hashes, rows, [], deleted=[_stone(rows[0], hashes[0])])
    assert (again.removed, again.new, again.near) == ([0], [1], {})


def test_a_live_row_is_named_before_a_removed_one() -> None:
    """A row typed again by hand backs the near match; the tombstone is not needed."""
    first = _rows(KIOSK)
    stone = _stone(first[0], assign_import_hashes(ACCOUNT, first)[0])
    typed = _existing(first[0], None)
    rows = _rows(RESPELLED)
    result = plan(assign_import_hashes(ACCOUNT, rows), rows, [typed], deleted=[stone])
    assert result.near == {0: [typed.id]}
    assert result.resembles_removed == []


def test_a_tombstone_without_date_or_amount_holds_nothing() -> None:
    first = _rows(KIOSK)
    digest = assign_import_hashes(ACCOUNT, first)[0]
    rows = _rows(RESPELLED)
    hashes = assign_import_hashes(ACCOUNT, rows)
    for stone in (Removed(digest, None, Decimal("-7.25")), Removed(digest, D1, None)):
        result = plan(hashes, rows, [], deleted=[stone])
        assert result.new == [0]
        assert result.resembles_removed == []


def test_without_tombstones_the_plan_is_unchanged() -> None:
    """The alias case of ``tests/unit/test_structured_import.py``, with and without the argument."""
    first = _rows((D1, "1480.10", "Cobalt Mills Payroll"))
    kept = _existing(first[0], assign_import_hashes(ACCOUNT, first)[0])
    respelled = _rows((D1, "1480.10", "COBALT MILLS PAYROLL DIRECT DEP"))
    alias = Existing(kept.id, D1, kept.amount, assign_import_hashes(ACCOUNT, respelled)[0])
    rows = _rows((D1, "1480.10", "COBALT MILLS PAYROLL DIRECT DEP"), (D1, "1480.10", "Bonus"))
    hashes = assign_import_hashes(ACCOUNT, rows)
    for result in (
        plan(hashes, rows, [kept, alias]),
        plan(hashes, rows, [kept, alias], tombstoned=()),
        plan(hashes, rows, [kept, alias], ()),
    ):
        assert (result.exact, result.new, result.near) == ([0], [1], {})
        assert result.removed == []
        assert result.resembles_removed == []
        assert result.to_insert == [1]
    without = plan(hashes, rows, [kept])
    assert without.near == {0: [kept.id]}
    assert without.removed == []

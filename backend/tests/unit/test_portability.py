# SPDX-License-Identifier: AGPL-3.0-or-later
"""Export/import building blocks without a database (docs/PORTABILITY.md; SECURITY.md C2, C3).

* every household-scoped table is either exported or deliberately left out;
* :mod:`app.domain.portability.zipsafe` refuses unsafe names, links,
  encrypted members, bombs and oversized archives before decompressing;
* :mod:`app.domain.portability.codec` decodes strictly;
* document file names inside the archive are sanitised.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Column, Numeric, String, Table

from app.db.base import Base, HouseholdScoped
from app.db.registry import import_all_models
from app.domain.documents.models import Document
from app.domain.portability import codec
from app.domain.portability.codec import ArchiveInvalidError
from app.domain.portability.entities import BY_NAME, ENTITIES, NOT_EXPORTED
from app.domain.portability.export import member_filename
from app.domain.portability.zipsafe import SafeZip, ZipLimits, check_member_name


def test_every_household_table_is_classified() -> None:
    import_all_models()
    scoped = {
        table.name
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, HouseholdScoped)
        and isinstance(table := mapper.local_table, Table)
    }
    exported = {spec.table.name for spec in ENTITIES}
    assert not exported & set(NOT_EXPORTED)
    missing = scoped - exported - set(NOT_EXPORTED)
    assert not missing, f"classify these tables in app/domain/portability/entities.py: {missing}"
    assert not set(NOT_EXPORTED) - scoped, "NOT_EXPORTED names a table that is gone"


def test_entities_are_in_dependency_order() -> None:
    seen: set[str] = set()
    for spec in ENTITIES:
        for column, target in spec.refs.items():
            assert target in seen or target == spec.name, f"{spec.name}.{column} -> {target}"
            assert column in spec.table.c
        seen.add(spec.name)


def zip_of(entries: list[tuple[zipfile.ZipInfo | str, bytes]], **kwargs: Any) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", **kwargs) as archive:
        for info, data in entries:
            archive.writestr(info, data)
    return buffer.getvalue()


def refused(data: bytes, limits: ZipLimits | None = None) -> str:
    with pytest.raises(ArchiveInvalidError) as caught:
        SafeZip(data, limits)
    return caught.value.slug


@pytest.mark.parametrize(
    "name",
    [
        "../x",
        "a/../../x",
        "/abs",
        "a\\b",
        "C:/x",
        "c:x",
        "a//b",
        "./a",
        "a/.",
        "dir/",
        "a\x00b",
        "a\nb",
        "x" * 513,
        "",
    ],
)
def test_unsafe_names(name: str) -> None:
    with pytest.raises(ArchiveInvalidError):
        check_member_name(name)


@pytest.mark.parametrize("name", ["manifest.json", "data/accounts.json", "documents/1/a b.csv"])
def test_safe_names(name: str) -> None:
    assert check_member_name(name) == name


def test_name_checks_apply_to_real_archives() -> None:
    assert refused(zip_of([("../../etc/cron.d/x", b"x")])) == "archive-unsafe"


def test_links_directories_and_duplicates_are_refused() -> None:
    link = zipfile.ZipInfo("data/link")
    link.external_attr = (0o120777) << 16
    assert refused(zip_of([(link, b"/etc/passwd")])) == "archive-unsafe"
    directory = zipfile.ZipInfo("data/")
    directory.external_attr = (0o40755 << 16) | 0x10
    assert refused(zip_of([(directory, b"")])) == "archive-unsafe"
    with pytest.warns(UserWarning, match="Duplicate name"):
        duplicate = zip_of([("a.json", b"1"), ("a.json", b"2")])
    assert refused(duplicate) == "archive-unsafe"


def test_encrypted_and_exotic_members_are_refused() -> None:
    data = bytearray(zip_of([("a.json", b"{}")]))
    # Set the "encrypted" flag in the local header and the central directory.
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        at = data.index(signature) + offset
        data[at] |= 0x01
    assert refused(bytes(data)) == "archive-unsafe"
    assert (
        refused(zip_of([("a.json", b"{}" * 10)], compression=zipfile.ZIP_BZIP2)) == "archive-unsafe"
    )


def test_bombs_and_size_caps() -> None:
    bomb = zip_of([("documents/x/zeros", bytes(8 * 1024 * 1024))], compression=zipfile.ZIP_DEFLATED)
    assert len(bomb) < 64 * 1024
    assert refused(bomb) == "archive-unsafe"
    small = ZipLimits(max_members=2, max_member_bytes=100, max_total_bytes=150)
    assert refused(zip_of([(f"m{i}", b"x") for i in range(3)]), small) == "archive-unsafe"
    assert refused(zip_of([("big", b"x" * 101)]), small) == "archive-unsafe"
    assert refused(zip_of([("a", b"x" * 80), ("b", b"x" * 80)]), small) == "archive-unsafe"
    ok = SafeZip(zip_of([("a", b"x" * 80)]), small)
    assert ok.read("a", limit=100) == b"x" * 80
    with pytest.raises(ArchiveInvalidError):
        ok.read("a", limit=10)


def test_declared_sizes_cannot_lie() -> None:
    data = bytearray(zip_of([("a.json", b"0123456789")]))
    # Claim 5 bytes uncompressed in the central directory: reading stops short of the data.
    at = data.index(b"PK\x01\x02") + 24
    data[at : at + 4] = (5).to_bytes(4, "little")
    archive = SafeZip(bytes(data))
    with pytest.raises(ArchiveInvalidError):
        archive.read("a.json", limit=100)


def test_overlapping_members_are_refused() -> None:
    data = bytearray(zip_of([("a", b"x" * 1000), ("b", b"y" * 1000)]))
    # Point the second central entry at the first local header (overlap).
    first = data.index(b"PK\x01\x02")
    second = data.index(b"PK\x01\x02", first + 4)
    data[second + 42 : second + 46] = data[first + 42 : first + 46]
    assert refused(bytes(data)) == "archive-unsafe"


def test_non_ascii_member_names_are_not_mistaken_for_overlaps() -> None:
    archive = SafeZip(zip_of([("documents/x/relev\u00e9.csv", b"a" * 50), ("b", b"b")]))
    assert archive.read("b", limit=10) == b"b"


def test_not_a_zip() -> None:
    assert refused(b"PK\x03\x04 broken") == "archive-not-zip"
    assert refused(b"plain text") == "archive-not-zip"


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------

AMOUNT = Column("amount", Numeric(20, 6), nullable=False)
NAME = Column("name", String(5), nullable=True)


@pytest.mark.parametrize(
    ("column", "raw"),
    [
        (AMOUNT, 12.5),
        (AMOUNT, "1.0000001"),
        (AMOUNT, "123456789012345"),
        (AMOUNT, "NaN"),
        (AMOUNT, "Infinity"),
        (AMOUNT, "12,5"),
        (AMOUNT, None),
        (NAME, "toolong"),
        (NAME, 5),
        (NAME, "a\x00"),
    ],
)
def test_strict_decoding(column: Column[Any], raw: Any) -> None:
    with pytest.raises(ArchiveInvalidError):
        codec.decode("t", column, raw)


def test_decoding_by_column_type() -> None:
    txn = {c.name: c for c in BY_NAME["transactions"].columns()}
    assert codec.decode("t", txn["amount"], "-12.500000") == Decimal("-12.5")
    assert codec.decode("t", txn["posted_date"], "2026-03-01") == date(2026, 3, 1)
    assert codec.decode("t", txn["id"], str(uuid.UUID(int=1))) == uuid.UUID(int=1)
    stamp = codec.decode("t", txn["created_at"], "2026-03-01T10:00:00+00:00")
    assert isinstance(stamp, datetime)
    assert stamp.tzinfo is not None
    for column, raw in (
        (txn["created_at"], "2026-03-01T10:00:00"),  # no offset
        (txn["status"], "exploded"),  # not an enum member
        (txn["posted_date"], "2026-02-30"),
        (txn["id"], "not-a-uuid"),
        (txn["confidence"], True),
    ):
        with pytest.raises(ArchiveInvalidError):
            codec.decode("t", column, raw)
    account = {c.name: c for c in BY_NAME["accounts"].columns()}
    with pytest.raises(ArchiveInvalidError):
        codec.decode("a", account["is_asset"], 1)
    rule = {c.name: c for c in BY_NAME["rules"].columns()}
    with pytest.raises(ArchiveInvalidError):
        codec.decode("r", rule["sort_order"], True)
    with pytest.raises(ArchiveInvalidError):
        codec.decode("r", rule["sort_order"], 2**40)
    payee = {c.name: c for c in BY_NAME["payees"].columns()}
    assert codec.decode("p", payee["aliases"], ["A", "B"]) == ["A", "B"]
    with pytest.raises(ArchiveInvalidError):
        codec.decode("p", payee["aliases"], ["A", 1])


def test_encoding_round_trips() -> None:
    txn = {c.name: c for c in BY_NAME["transactions"].columns()}
    for name, value in (
        ("amount", Decimal("-0.000001")),
        ("posted_date", date(2026, 1, 2)),
        ("id", uuid.UUID(int=7)),
    ):
        assert codec.decode("t", txn[name], codec.encode(txn[name], value)) == value


# ---------------------------------------------------------------------------
# Document names inside the archive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("statement.csv", "statement.csv"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\evil.exe", "evil.exe"),
        ("March statement?.csv", "March statement_.csv"),
        ("Relevé café.pdf", "Releve cafe.pdf"),
        ("...", "document"),
        (".hidden", "hidden"),
        (None, "document"),
        ("a" * 300 + ".csv", "a" * 120),
    ],
)
def test_member_filenames_are_sanitised(original: str | None, expected: str) -> None:
    document = Document(original_filename=original)
    assert member_filename(document) == expected


def test_not_exported_only_holds_justified_tables() -> None:
    """Secrets, per-instance identity, caches and job history only; each with a reason."""
    allowed = {
        "api_token",
        "account_grant",
        "household_member",
        "mail_intake",
        "dropfolder_route",
        "blob",
        "idempotency_key",
        "event",
        "holding",
        "import_hash_alias",
        # Import history of this instance (like ingest_job): the statement guard.
        "statement_record",
        "portability_archive",
        "tax_export",
        "review_item",
        "ingest_job",
        "dropfolder_entry",
        "mail_intake_message",
        "ai_call",
        "notification_channel",
    }
    assert set(NOT_EXPORTED) <= allowed
    assert all(len(reason) >= 15 for reason in NOT_EXPORTED.values())
    # Secret columns of exported tables are excluded.
    assert {"token_id", "created_by"} <= BY_NAME["ai_clients"].exclude


def test_version_1_entities_are_the_original_ones() -> None:
    from app.domain.portability.entities import entities_for

    v1 = [spec.name for spec in entities_for(1)]
    assert "securities" not in v1
    assert "transactions" in v1
    assert entities_for(2) == ENTITIES


def test_investment_hashes_are_recomputed_for_the_new_ids() -> None:
    from app.domain.investments.models import InvestmentTxnType
    from app.domain.investments.statement_import import HASH_VERSION, activity_hash
    from app.domain.portability.import_ import recover_investment_hash
    from app.ingestion.sandbox.protocol import InvestmentActivity

    old_acct, new_acct, old_sec, new_sec = (uuid.uuid4() for _ in range(4))
    day, units, amount = date(2026, 2, 3), Decimal("3.5000000000"), Decimal("-350.000000")
    row: dict[str, Any] = {
        "type": InvestmentTxnType.BUY,
        "trade_date": day,
        "quantity": units,
        "amount": amount,
    }
    activity = InvestmentActivity.model_construct(
        type="buy", trade_date=day, quantity=units, amount=amount
    )
    stored = activity_hash(old_acct, activity, old_sec, 2)
    assert recover_investment_hash(stored, old_acct, new_acct, old_sec, new_sec, row) == (
        activity_hash(new_acct, activity, new_sec, 2)
    )
    import hashlib

    def opening(account: uuid.UUID, security: uuid.UUID) -> str:
        parts = (HASH_VERSION, str(account), "opening", str(security), "2026-02-03")
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

    transfer = {**row, "type": InvestmentTxnType.TRANSFER_IN, "amount": Decimal(0)}
    assert recover_investment_hash(
        opening(old_acct, old_sec), old_acct, new_acct, old_sec, new_sec, transfer
    ) == opening(new_acct, new_sec)
    assert recover_investment_hash("0" * 64, old_acct, new_acct, old_sec, new_sec, row) is None

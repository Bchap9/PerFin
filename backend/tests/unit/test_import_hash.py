# SPDX-License-Identifier: AGPL-3.0-or-later
"""``transaction.import_hash`` and payee normalisation (DATA_MODEL.md "Ledger")."""

from __future__ import annotations

import hashlib
import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.domain.transactions.import_hash import (
    HASH_VERSION,
    PAYEE_KEY_MAX,
    HashInput,
    assign_import_hashes,
    canonical_amount,
    duplicate_hashes,
    import_hash,
    normalize_payee,
)

ACCOUNT = uuid.UUID("01890000-0000-7000-8000-000000000001")
OTHER = uuid.UUID("01890000-0000-7000-8000-000000000002")
DAY = date(2026, 3, 14)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SYNTHETIC GROCER #42", "synthetic grocer 42"),
        ("  Synthetic   Grocer\t#42  ", "synthetic grocer 42"),
        ("synthetic*grocer--42", "synthetic grocer 42"),
        ("CAFÉ Exemple", "cafe exemple"),
        ("Café Exemple", "cafe exemple"),
        ("Café Exemple", "cafe exemple"),  # combining accent
        ("\uff26\uff35\uff2c\uff2c\uff37\uff29\uff24\uff34\uff28", "fullwidth"),  # NFKC
        ("Straße", "strasse"),  # case folding
        ("a\x00b\x1fc", "a b c"),  # control characters are separators
        ("***", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_payee(raw: str | None, expected: str) -> None:
    assert normalize_payee(raw) == expected


def test_normalize_payee_is_bounded() -> None:
    assert len(normalize_payee("x " * 1000)) <= PAYEE_KEY_MAX
    assert not normalize_payee("y" * 199 + " zzz").endswith(" ")


def test_hash_matches_the_documented_format() -> None:
    """Recomputed from the specification in the module doc, field by field."""
    expected = hashlib.sha256(
        "\x1f".join(
            [
                HASH_VERSION,
                str(ACCOUNT),
                "2026-03-14",
                "-12.500000",
                "synthetic grocer 42",
                "0",
            ]
        ).encode()
    ).hexdigest()
    assert import_hash(ACCOUNT, DAY, Decimal("-12.5"), "SYNTHETIC GROCER #42", 0) == expected


def test_hash_is_pinned() -> None:
    """Changing the algorithm changes every stored hash: bump HASH_VERSION instead."""
    assert import_hash(ACCOUNT, DAY, Decimal("-12.5"), "SYNTHETIC GROCER #42", 0) == (
        hashlib.sha256(
            b"perfin-import-v1\x1f01890000-0000-7000-8000-000000000001\x1f2026-03-14"
            b"\x1f-12.500000\x1fsynthetic grocer 42\x1f0"
        ).hexdigest()
    )


def test_equivalent_inputs_hash_alike() -> None:
    a = import_hash(ACCOUNT, DAY, Decimal("-12.5"), "Synthetic Grocer #42", 0)
    assert a == import_hash(ACCOUNT, DAY, Decimal("-12.500"), "SYNTHETIC  GROCER 42", 0)
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)
    assert canonical_amount(Decimal("-0.0000001")) == "0.000000"
    assert import_hash(ACCOUNT, DAY, Decimal(0), None, 0) == import_hash(
        ACCOUNT, DAY, Decimal("-0"), "", 0
    )


@pytest.mark.parametrize(
    "change",
    [
        {"account": OTHER},
        {"day": date(2026, 3, 15)},
        {"amount": Decimal("12.5")},
        {"amount": Decimal("-12.51")},
        {"payee": "Synthetic Grocer 43"},
        {"index": 1},
    ],
)
def test_every_field_matters(change: dict[str, object]) -> None:
    base: dict[str, object] = {
        "account": ACCOUNT,
        "day": DAY,
        "amount": Decimal("-12.5"),
        "payee": "Synthetic Grocer 42",
        "index": 0,
    }

    def h(fields: dict[str, object]) -> str:
        return import_hash(
            fields["account"],  # type: ignore[arg-type]
            fields["day"],  # type: ignore[arg-type]
            fields["amount"],  # type: ignore[arg-type]
            fields["payee"],  # type: ignore[arg-type]
            fields["index"],  # type: ignore[arg-type]
        )

    assert h(base) != h({**base, **change})


def test_separator_cannot_be_injected() -> None:
    """A payee containing the field separator normalises it away."""
    smuggled = import_hash(ACCOUNT, DAY, Decimal("1"), "a\x1f0", 0)
    assert smuggled == import_hash(ACCOUNT, DAY, Decimal("1"), "a 0", 0)
    assert smuggled != import_hash(ACCOUNT, DAY, Decimal("1"), "a", 0)


def test_negative_index_refused() -> None:
    with pytest.raises(ValueError, match="index"):
        import_hash(ACCOUNT, DAY, Decimal("1"), "x", -1)


def test_running_index_counts_identical_rows_only() -> None:
    rows = [
        HashInput(DAY, Decimal("-4.50"), "Synthetic Coffee"),
        HashInput(DAY, Decimal("-30.00"), "Synthetic Grocer"),
        HashInput(DAY, Decimal("-4.5"), "SYNTHETIC COFFEE"),  # second identical coffee
        HashInput(date(2026, 3, 15), Decimal("-4.50"), "Synthetic Coffee"),
    ]
    hashes = assign_import_hashes(ACCOUNT, rows)
    assert hashes == [
        import_hash(ACCOUNT, DAY, Decimal("-4.50"), "Synthetic Coffee", 0),
        import_hash(ACCOUNT, DAY, Decimal("-30.00"), "Synthetic Grocer", 0),
        import_hash(ACCOUNT, DAY, Decimal("-4.50"), "Synthetic Coffee", 1),
        import_hash(ACCOUNT, date(2026, 3, 15), Decimal("-4.50"), "Synthetic Coffee", 0),
    ]
    assert len(set(hashes)) == 4


def test_overlapping_sources_deduplicate() -> None:
    """A second export covering the same day with an extra row still matches."""
    first = assign_import_hashes(
        ACCOUNT,
        [
            HashInput(DAY, Decimal("-4.50"), "Synthetic Coffee"),
            HashInput(DAY, Decimal("-4.50"), "Synthetic Coffee"),
        ],
    )
    second = assign_import_hashes(
        ACCOUNT,
        [
            HashInput(DAY, Decimal("-99.00"), "Synthetic Hardware"),  # new
            HashInput(DAY, Decimal("-4.5"), "SYNTHETIC COFFEE"),
            HashInput(DAY, Decimal("-4.5"), "synthetic coffee"),
            HashInput(DAY, Decimal("-4.5"), "synthetic coffee"),  # a third coffee: new
        ],
    )
    assert duplicate_hashes([*first, None], second) == set(first)
    assert second[0] not in first
    assert second[3] not in first

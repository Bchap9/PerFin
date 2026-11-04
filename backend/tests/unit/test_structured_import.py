# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured import units: mapping specs, cell parsing, fingerprints, dedupe plans.

The synthetic statements of ``tests/fixtures/synth_statements.py`` are run
through the real extractors in-process (allowed in tests; production reaches
them only through the sandbox) and read back with the parser and each
layout's mapping.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.imports.schemas import (
    DATE_FORMATS,
    ApplyMapping,
    MappingCreate,
    MappingSpec,
    MappingUpdate,
    Sign,
)
from app.domain.transactions.import_hash import HashInput, assign_import_hashes
from app.ingestion.extract.registry import builtin_extractors
from app.ingestion.match.dedupe import Existing, plan
from app.ingestion.parsers.structured import (
    candidate_fingerprints,
    from_statement,
    from_table,
    guess_date_format,
    header_fingerprint,
    parse_cell_amount,
    parse_cell_date,
    spec_fingerprint,
    suggest_spec,
)
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, Table
from tests.fixtures.synth_statements import (
    INSTITUTIONS,
    LAYOUTS,
    csv_bytes,
    layout_statement,
    main,
    ofx_sgml_bytes,
    ofx_xml_bytes,
    qif_bytes,
    sample_statement,
    statement,
    synth_ledger,
)

EXTRACTORS = builtin_extractors()


def _extract(kind: str, data: bytes) -> ExtractResult:
    return EXTRACTORS[kind](data, ExtractOptions(), kind)


def _table(rows: list[list[str]]) -> Table:
    return Table(
        encoding="utf-8",
        delimiter=",",
        has_header=True,
        rows=rows,
        column_count=max(len(r) for r in rows),
    )


# ---------------------------------------------------------------------------
# Mapping specs
# ---------------------------------------------------------------------------


def test_spec_needs_exactly_one_way_to_read_the_amount() -> None:
    MappingSpec(date_column=0, date_format="YYYY-MM-DD", amount_column=1)
    MappingSpec(date_column=0, date_format="YYYY-MM-DD", debit_column=1)
    MappingSpec(date_column=0, date_format="YYYY-MM-DD", debit_column=1, credit_column=2)
    with pytest.raises(ValidationError):
        MappingSpec(date_column=0, date_format="YYYY-MM-DD")
    with pytest.raises(ValidationError):
        MappingSpec(date_column=0, date_format="YYYY-MM-DD", amount_column=1, debit_column=2)


@pytest.mark.parametrize(
    "fields",
    [
        {"amount_column": 0},  # same column as the date
        {"amount_column": 1, "payee_column": 1},
        {"amount_column": 1, "indicator_column": 2},  # no outflow values
        {"amount_column": 1, "outflow_values": ["DR"]},  # no indicator column
        {"debit_column": 1, "indicator_column": 2, "outflow_values": ["DR"]},
        {"amount_column": 1, "currency": "USD", "currency_column": 2},
        {"amount_column": 1, "currency": "XYZ"},
        {"amount_column": 1, "date_format": "%Y-%m-%d"},
        {"amount_column": 1000},
        {"amount_column": 1, "skip_rows": 51},
        {"amount_column": 1, "unknown": True},
        {"amount_column": 1, "outflow_values": [" "], "indicator_column": 2},
    ],
)
def test_invalid_specs_are_refused(fields: dict[str, object]) -> None:
    data: dict[str, object] = {"date_column": 0, "date_format": "YYYY-MM-DD", **fields}
    with pytest.raises(ValidationError):
        MappingSpec.model_validate(data)


def test_mapping_inputs_forbid_extra_fields_and_need_a_fingerprint_source() -> None:
    spec = {"date_column": 0, "date_format": "YYYY-MM-DD", "amount_column": 1}
    with pytest.raises(ValidationError):
        MappingCreate.model_validate({"name": "x", "spec": spec})
    with pytest.raises(ValidationError):
        MappingCreate.model_validate(
            {
                "name": "x",
                "spec": spec,
                "document_id": str(uuid.uuid4()),
                "header_fingerprint": "a" * 64,
            }
        )
    with pytest.raises(ValidationError):
        MappingCreate.model_validate({"name": "x", "spec": spec, "header_fingerprint": "A" * 64})
    with pytest.raises(ValidationError):
        MappingCreate.model_validate(
            {"name": "x", "spec": spec, "header_fingerprint": "a" * 64, "household_id": "x"}
        )
    with pytest.raises(ValidationError):
        MappingUpdate.model_validate({})
    with pytest.raises(ValidationError):
        MappingUpdate.model_validate({"name": None})
    MappingUpdate.model_validate({"account_id": None})
    with pytest.raises(ValidationError):
        ApplyMapping.model_validate({"mapping_id": str(uuid.uuid4()), "extra": 1})


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "separator", "expected"),
    [
        ("1,234.56", ".", "1234.56"),
        ("-12.50", ".", "-12.50"),
        ("+5", ".", "5"),
        ("(12.34)", ".", "-12.34"),
        ("12.34-", ".", "-12.34"),
        ("$ -5.00", ".", "-5.00"),
        ("-$5.00", ".", "-5.00"),
        ("1.234,56", ",", "1234.56"),
        ("-3,50 €", ",", "-3.50"),
        ("1'234.50", ".", "1234.50"),
        ("USD 12.00", ".", "12.00"),
        ("'-12.34", ".", "-12.34"),  # the import-side formula guard
        ("0.000001", ".", "0.000001"),
    ],
)
def test_amount_cells(text: str, separator: str, expected: str) -> None:
    assert parse_cell_amount(text, separator) == Decimal(expected)


@pytest.mark.parametrize(
    "text",
    ["", "abc", "1.2.3", "12.1234567", "1e5", "NaN", "--5", "100000000000000", "=1+2"],
)
def test_non_amount_cells(text: str) -> None:
    assert parse_cell_amount(text, ".") is None


@pytest.mark.parametrize(
    ("text", "fmt", "expected"),
    [
        ("2026-01-31", "YYYY-MM-DD", date(2026, 1, 31)),
        ("2026-1-5", "YYYY-MM-DD", date(2026, 1, 5)),
        ("2026-01-31 10:22:05", "YYYY-MM-DD", date(2026, 1, 31)),
        ("2026-01-31T10:22:05Z", "YYYY-MM-DD", date(2026, 1, 31)),
        ("20260131", "YYYYMMDD", date(2026, 1, 31)),
        ("01/31/2026", "MM/DD/YYYY", date(2026, 1, 31)),
        ("1/5/26", "MM/DD/YY", date(2026, 1, 5)),
        ("31/01/2026", "DD/MM/YYYY", date(2026, 1, 31)),
        ("31.01.2026", "DD.MM.YYYY", date(2026, 1, 31)),
        ("31 Jan 2026", "DD MMM YYYY", date(2026, 1, 31)),
        ("05-Sept-2026", "DD-MMM-YYYY", date(2026, 9, 5)),
        ("Jan 31, 2026", "MMM DD, YYYY", date(2026, 1, 31)),
    ],
)
def test_date_cells(text: str, fmt: str, expected: date) -> None:
    assert parse_cell_date(text, fmt) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "fmt"),
    [
        ("2026-02-30", "YYYY-MM-DD"),
        ("31/01/2026", "MM/DD/YYYY"),
        ("31 Foo 2026", "DD MMM YYYY"),
        ("2026-01-31x", "YYYY-MM-DD"),
        ("", "YYYY-MM-DD"),
    ],
)
def test_invalid_date_cells(text: str, fmt: str) -> None:
    assert parse_cell_date(text, fmt) is None  # type: ignore[arg-type]


def test_every_documented_date_format_reads_its_example() -> None:
    for fmt, example in DATE_FORMATS.items():
        assert parse_cell_date(example, fmt) == date(2026, 1, 31), fmt  # type: ignore[arg-type]


def test_date_format_guess_prefers_month_first_only_when_it_fits() -> None:
    assert guess_date_format(["01/02/2026", "12/31/2026"]) == "MM/DD/YYYY"
    assert guess_date_format(["31/01/2026", "01/02/2026"]) == "DD/MM/YYYY"
    assert guess_date_format(["not a date"]) is None


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def test_header_fingerprint_ignores_case_spacing_and_formula_guard() -> None:
    a = header_fingerprint(["Date", "Amount", "Payee"])
    assert a == header_fingerprint(["  date ", "AMOUNT", "payee"])
    assert a == header_fingerprint(["Date", "Amount", "'Payee"])
    assert a != header_fingerprint(["Date", "Payee", "Amount"])
    assert a != header_fingerprint(["Date", "Amount", "Payee", ""])
    # Headerless tables fingerprint the shape of the first row.
    assert header_fingerprint(["2026-01-02", "-1.00", "X"], has_header=False) == header_fingerprint(
        ["2026-03-04", "77.10", "Other"], has_header=False
    )
    assert len(a) == 64


def test_debit_credit_indicator_and_sign_conventions() -> None:
    table = _table(
        [
            ["Date", "Payee", "Out", "In", "Flag", "Signed"],
            ["2026-01-02", "A", "10.00", "", "DR", "10.00"],
            ["2026-01-03", "B", "", "2.50", "cr", "-2.50"],
            ["2026-01-04", "C", "-1.00", "", "dr", "1.00"],
        ]
    )
    split = from_table(
        table,
        MappingSpec(date_column=0, date_format="YYYY-MM-DD", debit_column=2, credit_column=3),
    )
    assert [t.amount for t in split.transactions] == [
        Decimal("-10.00"),
        Decimal("2.50"),
        Decimal("-1.00"),
    ]
    flagged = from_table(
        table,
        MappingSpec(
            date_column=0,
            date_format="YYYY-MM-DD",
            amount_column=5,
            indicator_column=4,
            outflow_values=["DR"],
        ),
    )
    assert [t.amount for t in flagged.transactions] == [
        Decimal("-10.00"),
        Decimal("2.50"),
        Decimal("-1.00"),
    ]
    card = from_table(
        table,
        MappingSpec(
            date_column=0, date_format="YYYY-MM-DD", amount_column=5, sign=Sign.OUTFLOW_POSITIVE
        ),
    )
    assert [t.amount for t in card.transactions] == [
        Decimal("-10.00"),
        Decimal("2.50"),
        Decimal("-1.00"),
    ]


def test_unreadable_rows_are_skipped_and_reported() -> None:
    table = _table(
        [
            ["Date", "Amount", "Payee"],
            ["2026-01-02", "-1.00", "A"],
            ["Total", "-1.00", ""],
            ["2026-01-03", "n/a", "B"],
            ["2026-01-04", "3.00"],
        ]
    )
    parsed = from_table(
        table, MappingSpec(date_column=0, date_format="YYYY-MM-DD", amount_column=1, payee_column=2)
    )
    assert [t.row for t in parsed.transactions] == [1, 4]
    assert parsed.skipped_rows == [2, 3]
    assert parsed.transactions[1].raw_payee is None
    assert (parsed.period_start, parsed.period_end) == (date(2026, 1, 2), date(2026, 1, 4))


def test_running_balance_gives_closing_balance_and_reconciles() -> None:
    rows = [
        ["Date", "Amount", "Balance"],
        ["2026-01-03", "-5.00", "90.00"],
        ["2026-01-02", "-5.00", "95.00"],
        ["2026-01-01", "100.00", "100.00"],
    ]
    spec = MappingSpec(date_column=0, date_format="YYYY-MM-DD", amount_column=1, balance_column=2)
    parsed = from_table(_table(rows), spec)
    assert parsed.closing_balance is not None
    assert (parsed.closing_balance.amount, parsed.closing_balance.as_of) == (
        Decimal("90.00"),
        date(2026, 1, 3),
    )
    assert parsed.balance_mismatches == []
    rows[1][2] = "89.00"
    assert from_table(_table(rows), spec).balance_mismatches == [1]


def test_foreign_currency_cells_and_bad_currency_cells() -> None:
    table = _table(
        [["Date", "Amount", "Ccy"], ["2026-01-02", "1.00", "EUR"], ["2026-01-03", "2.00", "??"]]
    )
    parsed = from_table(
        table,
        MappingSpec(date_column=0, date_format="YYYY-MM-DD", amount_column=1, currency_column=2),
    )
    assert [t.currency for t in parsed.transactions] == ["EUR"]
    assert parsed.skipped_rows == [2]
    assert parsed.currency == "EUR"


def test_candidate_fingerprints_cover_preamble_rows() -> None:
    layout = LAYOUTS["preamble"]
    stmt = sample_statement(5)
    table = _extract("csv", csv_bytes(stmt, layout)).tables[0]
    spec = MappingSpec.model_validate(layout.spec)
    fingerprint = spec_fingerprint(table, spec)
    assert fingerprint is not None
    assert candidate_fingerprints(table)[(2, True)] == fingerprint


# ---------------------------------------------------------------------------
# The synthetic generator, through the real extractors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_every_csv_layout_reads_back_with_its_mapping(layout: str) -> None:
    stmt = layout_statement(sample_statement(7), layout)
    spec = MappingSpec.model_validate(LAYOUTS[layout].spec)
    table = _extract(LAYOUTS[layout].kind, csv_bytes(stmt, layout)).tables[0]
    parsed = from_table(table, spec)
    assert parsed.skipped_rows == []
    assert parsed.balance_mismatches == []
    got = sorted((t.posted_date, t.amount, t.raw_payee) for t in parsed.transactions)
    want = sorted((t.posted, t.amount, t.payee) for t in stmt.transactions)
    assert got == want
    if spec.balance_column is not None:
        assert parsed.closing_balance is not None
        assert parsed.closing_balance.amount == stmt.closing_balance
    if spec.currency_column is not None:
        assert parsed.currency == stmt.currency


def test_suggestions_find_the_simple_layouts() -> None:
    stmt = sample_statement(8)
    for name in ("us_signed", "tsv"):
        table = _extract(LAYOUTS[name].kind, csv_bytes(stmt, name)).tables[0]
        assert suggest_spec(table) == MappingSpec.model_validate(LAYOUTS[name].spec)
    table = _extract("csv", csv_bytes(stmt, "debit_credit")).tables[0]
    suggested = suggest_spec(table)
    assert suggested is not None
    assert (suggested.debit_column, suggested.credit_column) == (2, 3)
    assert suggest_spec(_table([["a", "b"], ["c", "d"]])) is None


@pytest.mark.parametrize("credit_card", [False, True])
@pytest.mark.parametrize("fmt", ["ofx", "ofx-xml", "qfx"])
def test_ofx_family_reads_back(fmt: str, credit_card: bool) -> None:
    stmt = sample_statement(9, credit_card=credit_card)
    data = ofx_xml_bytes(stmt) if fmt == "ofx-xml" else ofx_sgml_bytes(stmt, qfx=fmt == "qfx")
    result = _extract("qfx" if fmt == "qfx" else "ofx", data)
    (parsed,) = [from_statement(s) for s in result.statements]
    assert parsed.last4 == stmt.last4
    assert parsed.institution == stmt.institution.name
    assert parsed.currency == stmt.currency
    assert parsed.closing_balance is not None
    assert parsed.closing_balance.amount == stmt.closing_balance
    assert parsed.closing_balance.as_of == stmt.end
    assert [(t.posted_date, t.amount) for t in parsed.transactions] == [
        (t.posted, t.amount) for t in stmt.transactions
    ]
    assert stmt.account_number not in result.model_dump_json()


def test_qif_reads_back_with_categories_and_a_split() -> None:
    stmt = sample_statement(10)
    (parsed,) = [from_statement(s) for s in _extract("qif", qif_bytes(stmt)).statements]
    assert len(parsed.transactions) == len(stmt.transactions)
    split = [t for t in parsed.transactions if t.splits]
    assert len(split) == 1
    assert sum(s.amount for s in split[0].splits) == split[0].amount
    assert {t.category for t in parsed.transactions} >= {"Housing:Rent", "Food:Coffee"}
    assert not parsed.date_order_assumed


def test_overlapping_windows_share_transactions() -> None:
    ledger = synth_ledger(11, days=60)
    first = statement(ledger, date(2026, 1, 1), date(2026, 1, 31), seed=11)
    second = statement(ledger, date(2026, 1, 20), date(2026, 2, 28), seed=11)
    assert first.closing_balance == second.opening_balance + sum(
        (t.amount for t in second.transactions if t.posted <= date(2026, 1, 31)), Decimal(0)
    )
    shared = set(first.transactions) & set(second.transactions)
    assert shared
    assert all(date(2026, 1, 20) <= t.posted <= date(2026, 1, 31) for t in shared)


def test_generator_is_deterministic_and_synthetic(tmp_path: Path) -> None:
    assert sample_statement(12) == sample_statement(12)
    assert sample_statement(12) != sample_statement(13)
    assert main(["--out", str(tmp_path), "--seed", "3", "--days", "20"]) == 0
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    files = {entry["file"] for entry in manifest}
    assert {"checking.ofx", "checking-v2.ofx", "card.qfx", "checking.qif"} <= files
    assert {f"{name}.{layout.kind}" for name, layout in LAYOUTS.items()} <= files
    for entry in manifest:
        assert (tmp_path / entry["file"]).stat().st_size > 0
        assert entry["institution"] in {i.name for i in INSTITUTIONS}


# ---------------------------------------------------------------------------
# Dedupe plans
# ---------------------------------------------------------------------------

ACCOUNT = uuid.UUID("01900000-0000-7000-8000-000000000001")
D1, D2 = date(2026, 1, 10), date(2026, 1, 11)


def _rows(*items: tuple[date, str, str | None]) -> list[HashInput]:
    return [HashInput(d, Decimal(a), p) for d, a, p in items]


def _existing(rows: list[HashInput], *, hashed: bool = True) -> list[Existing]:
    hashes = assign_import_hashes(ACCOUNT, rows)
    return [
        Existing(uuid.uuid4(), r.posted_date, r.amount, h if hashed else None)
        for r, h in zip(rows, hashes, strict=True)
    ]


def test_exact_hash_matches_are_skipped() -> None:
    rows = _rows((D1, "-4.50", "Cafe"), (D1, "-4.50", "Cafe"), (D2, "-9.00", "Shop"))
    existing = _existing(rows[:2])
    result = plan(assign_import_hashes(ACCOUNT, rows), rows, existing)
    assert result.exact == [0, 1]
    assert result.new == [2]
    assert result.near == {}
    assert result.to_insert == [2]


def test_a_third_identical_purchase_is_new() -> None:
    rows = _rows((D1, "-4.50", "Cafe"), (D1, "-4.50", "Cafe"), (D1, "-4.50", "Cafe"))
    result = plan(assign_import_hashes(ACCOUNT, rows), rows, _existing(rows[:2]))
    assert (result.exact, result.new, result.near) == ([0, 1], [2], {})


def test_near_matches_need_same_date_and_amount_and_are_one_to_one() -> None:
    manual = _existing(_rows((D1, "-4.5", "Coffee")), hashed=False)
    rows = _rows(
        (D1, "-4.50", "SQ *LANTERN CAFE"), (D1, "-4.50", "SQ *LANTERN CAFE"), (D2, "-4.50", "X")
    )
    result = plan(assign_import_hashes(ACCOUNT, rows), rows, manual)
    assert result.near == {0: [manual[0].id]}
    assert result.new == [1, 2]
    assert result.to_insert == [0, 1, 2]


def test_rows_claimed_by_an_exact_match_are_no_near_candidates() -> None:
    old = _rows((D1, "-4.50", "Cafe"))
    existing = _existing(old)
    rows = _rows((D1, "-4.50", "Cafe"), (D1, "-4.50", "Other spelling"))
    result = plan(assign_import_hashes(ACCOUNT, rows), rows, existing)
    assert (result.exact, result.new, result.near) == ([0], [1], {})


def test_an_alias_hash_matches_its_kept_transaction_exactly() -> None:
    """A confirmed duplicate's hash (``import_hash_alias``) is an extra entry for the kept row."""
    kept = _existing(_rows((D1, "3210.55", "Initech Payroll")))[0]
    respelled = _rows((D1, "3210.55", "INITECH PAYROLL DIRECT DEPOSIT"))
    alias = Existing(kept.id, D1, kept.amount, assign_import_hashes(ACCOUNT, respelled)[0])
    rows = _rows((D1, "3210.55", "INITECH PAYROLL DIRECT DEPOSIT"), (D1, "3210.55", "Bonus"))
    result = plan(assign_import_hashes(ACCOUNT, rows), rows, [kept, alias])
    # The respelled row is exact; the kept row is claimed by it, so a second
    # payment that day is new rather than another near match of the same row.
    assert (result.exact, result.new, result.near) == ([0], [1], {})
    # Without the alias the respelled row would be a near match, listed once.
    without = plan(assign_import_hashes(ACCOUNT, rows), rows, [kept])
    assert without.near == {0: [kept.id]}
    listed_twice = plan(assign_import_hashes(ACCOUNT, rows[1:]), rows[1:], [kept, alias])
    assert listed_twice.near == {0: [kept.id]}


def test_plan_needs_one_hash_per_row() -> None:
    with pytest.raises(ValueError, match="one hash per row"):
        plan([], _rows((D1, "1", None)), [])

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Classify stage: fingerprints, heuristic classifier, period, tax year, last four digits.

Everything runs on synthetic documents from ``tests/fixtures/synth_classify.py``.
The held-out accuracy test uses a seed the shipped artefact was not trained
on and prints the accuracy (``pytest -s`` shows it).
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from importlib import resources

import pytest

from app.ingestion.classify import model as kind_model
from app.ingestion.classify.dates import DateOrder, find_dates
from app.ingestion.classify.fingerprints import builtin_fingerprints
from app.ingestion.classify.period import detect_period, detect_printed_date, detect_tax_year
from app.ingestion.classify.service import KIND_THRESHOLD, classify_extraction, classify_view
from app.ingestion.classify.text import DocumentView
from app.ingestion.sandbox.protocol import (
    ExtractedTransaction,
    ExtractResult,
    Statement,
    StatementAccount,
)
from tests.fixtures import synth_classify as synth


def _view(doc: synth.SynthDoc) -> DocumentView:
    return DocumentView.from_text(doc.text, table_headers=doc.table_headers)


# ---------------------------------------------------------------------------
# Shipped fingerprints
# ---------------------------------------------------------------------------


def test_shipped_catalogue_covers_the_fictional_institutions() -> None:
    fps = builtin_fingerprints()
    assert sorted(fp.key for fp in fps) == sorted(i.key for i in synth.FAKE_INSTITUTIONS)
    institutions = {fp.spec.institution for fp in fps}
    assert len(institutions) == 6
    assert all(fp.origin == "builtin" for fp in fps)


@pytest.mark.parametrize("key", [i.key for i in synth.FAKE_INSTITUTIONS])
def test_each_shipped_layout_is_recognised_with_its_facts(key: str) -> None:
    for seed in range(15):
        doc = synth.institution_document(key, seed)
        result = classify_view(_view(doc))
        assert result.method == "fingerprint", (key, seed)
        assert result.fingerprint is not None
        assert result.fingerprint.key == key
        assert result.kind == doc.kind
        assert result.institution == doc.institution
        assert result.confidence["kind"] >= 0.85
        if doc.kind in ("statement", "tax_form"):
            assert result.last4.value == doc.last4, (key, seed, doc.lines)
        if doc.period_start is not None:
            assert (result.period.start, result.period.end) == (doc.period_start, doc.period_end)
            assert not result.period.inferred
        if doc.tax_year is not None:
            assert result.tax_year.value == doc.tax_year


def test_fingerprints_do_not_fire_on_other_documents() -> None:
    for doc in synth.corpus(synth.HELD_OUT_SEED, 30, noisy_share=0.0):
        if doc.fingerprint is None:
            result = classify_view(_view(doc))
            assert result.method != "fingerprint", doc.lines


def test_noisy_layout_falls_back_to_the_classifier() -> None:
    doc = synth.institution_document("demo-card-services-card", 3)
    damaged = DocumentView.from_text(doc.text.replace("Demo Card Services", "Dem0 Card Servlces"))
    result = classify_view(damaged)
    assert result.method == "classifier"
    assert result.kind == "statement"
    assert result.institution is None


# ---------------------------------------------------------------------------
# Heuristic classifier
# ---------------------------------------------------------------------------


def test_shipped_artefact_is_reproducible_from_the_generator() -> None:
    shipped = (
        resources.files("app.ingestion.classify")
        .joinpath("artefacts", kind_model.ARTEFACT)
        .read_text(encoding="utf-8")
    )
    assert synth.build_artefact() == shipped
    loaded = kind_model.shipped_model()
    assert loaded.classes == list(synth.KINDS)
    assert loaded.model_version == synth.MODEL_VERSION


def test_held_out_accuracy() -> None:
    """Classifier alone (no fingerprints) on an unseen seed, 40% with OCR noise."""
    model = kind_model.shipped_model()
    total: Counter[str] = Counter()
    right: Counter[str] = Counter()
    confident_wrong = 0
    for doc in synth.corpus(synth.HELD_OUT_SEED, 60, noisy_share=0.4):
        result = classify_view(_view(doc), fingerprints=(), model=model)
        total[doc.kind] += 1
        right[doc.kind] += result.kind == doc.kind
        confident_wrong += result.kind is not None and result.kind != doc.kind
    accuracy = sum(right.values()) / sum(total.values())
    per_kind = {k: round(right[k] / total[k], 3) for k in synth.KINDS}
    print(f"held-out kind accuracy {accuracy:.4f} over {sum(total.values())} docs: {per_kind}")
    assert accuracy >= 0.97
    assert min(per_kind.values()) >= 0.9
    assert confident_wrong == 0


def test_held_out_accuracy_with_the_full_cascade() -> None:
    total = right = 0
    for doc in synth.corpus(synth.HELD_OUT_SEED + 1, 40, noisy_share=0.4):
        result = classify_view(_view(doc))
        total += 1
        right += result.kind == doc.kind
    print(f"held-out cascade accuracy {right / total:.4f} over {total} docs")
    assert right / total >= 0.97


@pytest.mark.parametrize("text", ["", "qwerty zxcvb plmok", "Hello world", "Statement"])
def test_little_or_unknown_text_is_not_classified(text: str) -> None:
    result = classify_view(DocumentView.from_text(text), fingerprints=())
    assert result.kind is None
    assert "low_confidence_kind" in result.review_reasons
    assert result.confidence["kind"] < KIND_THRESHOLD


def test_prediction_is_deterministic() -> None:
    doc = synth.generic_document("invoice", 5)
    model = kind_model.shipped_model()
    first = kind_model.predict(model, doc.text)
    assert all(kind_model.predict(model, doc.text) == first for _ in range(3))
    assert first.kind == "invoice"
    assert 0 < first.confidence <= 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format="other"),
        lambda d: d["log_prior"].pop(),
        lambda d: d["features"].update({"x": [0.0]}),
        lambda d: d.update(extra=1),
        lambda d: d.update(model_version="latest"),
    ],
)
def test_tampered_artefacts_are_refused(mutate: object) -> None:
    data = json.loads(kind_model.dumps(kind_model.shipped_model()))
    assert callable(mutate)
    mutate(data)
    with pytest.raises(ValueError, match=r"."):
        kind_model.loads(json.dumps(data).encode())


def test_non_finite_and_oversized_artefacts_are_refused() -> None:
    raw = kind_model.dumps(kind_model.shipped_model())
    data = json.loads(raw)
    first = next(iter(data["features"]))
    data["features"][first][0] = float("nan")
    with pytest.raises(ValueError, match=r"."):
        kind_model.loads(json.dumps(data).encode())
    with pytest.raises(ValueError, match="too large"):
        kind_model.loads(b" " * (kind_model.MAX_ARTEFACT_BYTES + 1))


# ---------------------------------------------------------------------------
# Period, printed date and tax year
# ---------------------------------------------------------------------------

START, END = date(2026, 1, 1), date(2026, 1, 31)


@pytest.mark.parametrize("style", sorted({*synth.US_STYLES, *synth.DMY_STYLES}))
@pytest.mark.parametrize(
    "template",
    [
        "Statement period {a} to {b}",
        "STATEMENT PERIOD: {a} - {b}",
        "For the period {a} through {b}",
        "Billing Period {a} \u2013 {b}",
        "Period covered: {a} to {b}",
    ],
)
def test_explicit_period_in_many_formats(style: str, template: str) -> None:
    line = template.format(a=synth.fmt_date(START, style), b=synth.fmt_date(END, style))
    order: DateOrder | None = None
    if style.startswith("dmy") and style not in ("dmy_name", "dmy_dash"):
        order = "dmy"
    period = detect_period(DocumentView.from_text(f"Some Bank\n{line}\nBalance 1.00"), order=order)
    assert (period.start, period.end) == (START, END), line
    assert not period.inferred
    assert period.source == "label"


def test_period_with_a_shared_year_and_on_the_next_line() -> None:
    view = DocumentView.from_text("Statement period\nJan 1 - Jan 31, 2026\n")
    assert (detect_period(view).start, detect_period(view).end) == (START, END)
    across = DocumentView.from_text("Statement period: Dec 15 - Jan 14, 2026")
    assert detect_period(across).start == date(2025, 12, 15)


def test_ambiguous_day_month_lowers_confidence_and_follows_the_hint() -> None:
    view = DocumentView.from_text("Statement period 03/04/2026 to 02/05/2026")
    us = detect_period(view)
    assert us.start is None  # month-first would end before it starts
    eu = detect_period(view, order="dmy")
    assert (eu.start, eu.end) == (date(2026, 4, 3), date(2026, 5, 2))
    assert eu.confidence == 0.95
    plain = detect_period(DocumentView.from_text("Statement period 03/04/2026 to 04/04/2026"))
    assert plain.confidence < 0.95


def test_opening_and_closing_dates() -> None:
    view = DocumentView.from_text("Opening date 2026-01-01\nClosing date: 2026-01-31")
    period = detect_period(view)
    assert (period.start, period.end, period.source) == (START, END, "open_close")


def test_inferred_period_from_transaction_lines_and_structured_dates() -> None:
    text = "Riverbend Test Bank\n01/03/2026 COFFEE 3.00\n01/29/2026 RENT 900.00\n2026-01-15 FEE 1"
    period = detect_period(DocumentView.from_text(text))
    assert (period.start, period.end) == (date(2026, 1, 3), date(2026, 1, 29))
    assert period.inferred
    assert period.confidence < 0.7
    result = ExtractResult(
        kind="ofx",
        ok=True,
        statements=[
            Statement(
                source_format="ofx",
                account=StatementAccount(last4="1234"),
                transactions=[
                    ExtractedTransaction(posted=date(2026, 2, d), amount="1.00")  # type: ignore[arg-type]
                    for d in (3, 9, 27)
                ],
            )
        ],
    )
    from_txns = detect_period(DocumentView.from_extraction(result))
    assert (from_txns.start, from_txns.end, from_txns.inferred) == (
        date(2026, 2, 3),
        date(2026, 2, 27),
        True,
    )
    structured = classify_extraction(result)
    assert (structured.kind, structured.method, structured.last4.value) == (
        "statement",
        "structured",
        "1234",
    )


def test_implausible_periods_are_refused() -> None:
    assert (
        detect_period(DocumentView.from_text("Statement period 2026-01-31 to 2026-01-01")).start
        is None
    )
    assert (
        detect_period(DocumentView.from_text("Statement period 2020-01-01 to 2026-01-01")).start
        is None
    )
    assert detect_period(DocumentView.from_text("no dates here")).start is None


def test_receipt_printed_date_and_tax_year() -> None:
    receipt = synth.generic_document("receipt", 4)
    printed = detect_printed_date(_view(receipt))
    assert printed.value == receipt.printed_date
    assert printed.source == "label"
    for seed in range(12):
        form = synth.generic_document("tax_form", seed)
        assert detect_tax_year(_view(form)).value == form.tax_year, form.lines
    assert (
        detect_tax_year(DocumentView.from_text("Form W-2 Wage and Tax Statement 2025")).value
        == 2025
    )
    assert detect_tax_year(DocumentView.from_text("For calendar year 2024")).value == 2024
    assert detect_tax_year(DocumentView.from_text("no year")).value is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-01-31", date(2026, 1, 31)),
        ("31.01.2026", date(2026, 1, 31)),
        ("1/31/26", date(2026, 1, 31)),
        ("January 31, 2026", date(2026, 1, 31)),
        ("Sept. 3rd, 2026", date(2026, 9, 3)),
        ("31-Jan-2026", date(2026, 1, 31)),
        ("31 January 2026", date(2026, 1, 31)),
    ],
)
def test_date_formats(text: str, expected: date) -> None:
    (hit,) = find_dates(f"on {text} we", order=None)
    assert hit.value == expected


def test_invalid_dates_are_ignored() -> None:
    assert find_dates("13/13/2026 2026-02-30 Feb 30, 2026 99/99/99") == []


# ---------------------------------------------------------------------------
# Last four digits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "last4"),
    [
        ("Account ending in 9876", "9876"),
        ("Card number ****9876", "9876"),
        ("Account xxxx-xxxx-9876", "9876"),
        ("Account number: 0012 3456 9876", "9876"),
    ],
)
def test_generic_last4(line: str, last4: str) -> None:
    from app.ingestion.classify.accounts import detect_last4

    found = detect_last4(DocumentView.from_text(f"Some Bank\n{line}"))
    assert found.value == last4


def test_two_different_accounts_give_no_last4() -> None:
    from app.ingestion.classify.accounts import detect_last4

    view = DocumentView.from_text("Account ****1111\nTransfer to account ****2222")
    assert detect_last4(view).value is None


def test_only_last_four_digits_leave_the_classifier() -> None:
    doc = synth.institution_document("sample-national-bank-checking", 1)
    text = doc.text.replace(f"XXXX-XXXX-{doc.last4}", f"0123-4567-{doc.last4}")
    result = classify_view(DocumentView.from_text(text, table_headers=doc.table_headers))
    assert result.last4.value == doc.last4
    assert "0123" not in json.dumps(result.facts())

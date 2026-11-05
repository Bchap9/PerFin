# SPDX-License-Identifier: AGPL-3.0-or-later
"""The tax form parser on synthetic forms (``tests/fixtures/synth_tax_forms.py``).

PDFs are extracted in process and parsed by the registry as the worker does.
Every US form type is read exactly (type, year, issuer name, amount boxes,
W-2 box 12/14, 1098 box 10, 1095 coverage months and monthly amounts); a
consolidated statement gives one form per heading and identical copies are
kept once. The parser output and the redacted extraction hold none of the
fake identity values printed on the forms.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.tax.forms import form_def, sanitize_boxes
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers import confidence
from app.ingestion.parsers.registry import parse_extraction, reparse
from app.ingestion.parsers.tax_forms import PARSER_NAME, REDACTED, redact_identity, redact_text
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, TaxFormRead
from tests.fixtures import synth_tax_forms as synth

OPTIONS = ExtractOptions(previews=False, ocr=False)


def _extract(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return result


def _parse(data: bytes) -> ExtractResult:
    return parse_extraction(_extract(data), threshold=confidence.DEFAULT_THRESHOLD)


def _forms(result: ExtractResult) -> list[TaxFormRead]:
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == PARSER_NAME
    assert result.parse.parser.kind == "tax_form"
    assert result.statements == []
    return list(result.parse.tax_forms)


@pytest.mark.parametrize("seed", [0, 1, 2])
@pytest.mark.parametrize("form_type", synth.FORM_TYPES)
def test_every_form_is_read_exactly(form_type: str, seed: int) -> None:
    form = synth.synth_form(form_type, seed=seed, tax_year=2024 + seed)
    (read,) = _forms(_parse(synth.form_pdf(form)))
    assert read.jurisdiction == "US"
    assert read.form_type == form_type
    assert read.tax_year == 2024 + seed
    assert read.issuer_name == form.issuer
    assert read.boxes == synth.expected_boxes(form)
    assert read.low_confidence == []
    # The output is exactly what the tax vault stores (amount boxes only).
    sanitized = sanitize_boxes(form_def("US", form_type), dict(read.boxes))
    assert sanitized.boxes == read.boxes
    assert sanitized.discarded == ()


def test_w2_key_boxes() -> None:
    form = synth.synth_form("W-2", seed=7)
    (read,) = _forms(_parse(synth.form_pdf(form)))
    for box in ("1", "2", "3", "4", "5", "6"):
        assert read.boxes[box] == form.amounts[box]
    assert read.boxes["12_D"] == form.box12[0][1]
    assert read.boxes["12_DD"] == form.box12[1][1]
    assert read.boxes["14t"] == sum((a for _, a in form.other), Decimal(0))


def test_1095_coverage_months() -> None:
    b = synth.synth_form("1095-B", seed=3)
    (read,) = _forms(_parse(synth.form_pdf(b)))
    assert {k for k in read.boxes if k.startswith("cov_")} == {f"cov_{m}" for m in b.coverage}
    c = synth.synth_form("1095-C", seed=3)
    (read,) = _forms(_parse(synth.form_pdf(c)))
    assert read.boxes["cov_ALL"] == 1
    assert read.boxes["15_ALL"] == c.line15["ALL"]


def test_consolidated_statement_gives_one_form_per_heading() -> None:
    forms = [
        synth.synth_form("1099-INT", seed=1),
        synth.synth_form("1099-DIV", seed=1),
        synth.synth_form("1099-B", seed=1),
        synth.synth_form("1099-DIV", seed=2, issuer="Placeholder Brokerage"),
    ]
    reads = _forms(_parse(synth.forms_pdf(forms)))
    assert [(r.form_type, r.form_index) for r in reads] == [
        ("1099-INT", 0),
        ("1099-DIV", 0),
        ("1099-B", 0),
        ("1099-DIV", 1),
    ]
    for read, form in zip(reads, forms, strict=True):
        assert read.boxes == synth.expected_boxes(form)


def test_identical_copies_are_kept_once() -> None:
    form = synth.synth_form("W-2", seed=4)
    reads = _forms(_parse(synth.forms_pdf([form, form, form])))
    assert len(reads) == 1


def test_reparse_keeps_the_choice() -> None:
    first = _parse(synth.form_pdf(synth.synth_form("1098", seed=5)))
    again = reparse(first)
    assert _forms(again) == _forms(first)


def test_a_mention_of_a_form_is_not_a_form() -> None:
    from app.ingestion.parsers.base import DocumentView
    from app.ingestion.parsers.tax_forms import TaxFormParser

    result = _extract(synth.form_pdf(synth.synth_form("W-2")))
    view = DocumentView.from_result(result)
    assert TaxFormParser().detect(view) > 0.9
    # Only the heading line: below the registry's minimum score.
    heading_only = DocumentView(
        lines=view.lines[:1], source_format="pdf", truncated=False, page_count=1
    )
    assert TaxFormParser().detect(heading_only) < 0.35


def _all_text(result: ExtractResult) -> str:
    parts = [result.text, result.model_dump_json()]
    return "\n".join(parts).lower()


@pytest.mark.parametrize("form_type", synth.FORM_TYPES)
def test_identity_values_never_leave_the_parser(form_type: str) -> None:
    form = synth.synth_form(form_type, seed=11)
    raw = _extract(synth.form_pdf(form))
    raw_text = _all_text(raw)
    # The fixture really prints them...
    assert synth.FAKE_SSN in raw_text or synth.FAKE_EIN in raw_text
    assert "testperson" in raw_text
    parsed = parse_extraction(raw, threshold=confidence.DEFAULT_THRESHOLD)
    (read,) = _forms(parsed)
    # ...the parser output holds none of them,
    output = read.model_dump_json().lower()
    for token in synth.IDENTITY_TOKENS:
        assert token not in output, token
    # ...and neither does the redacted extraction (pages, lines, words,
    # layout tables, text), while the amounts and the labels are still there.
    redacted = redact_identity(parsed, [read])
    text = _all_text(redacted)
    for token in synth.IDENTITY_TOKENS:
        assert token not in text, token
    assert REDACTED in redacted.text
    some_amount = next(iter(synth.expected_boxes(form).values()))
    if some_amount != 1:
        assert f"{some_amount:,.2f}" in redacted.text
    assert form.issuer in redacted.text  # appended once, from the parsed form
    # The parser still reads the redacted pages the same way.
    again = parse_extraction(redacted, threshold=confidence.DEFAULT_THRESHOLD)
    assert _forms(again)[0].boxes == read.boxes


def test_redact_text_keeps_vocabulary_and_amounts() -> None:
    text = "1 Wages, tips, other compensation 52,345.67 Pat Example 000-00-0000 2025 12a D"
    out = redact_text(text)
    assert out.startswith("1 Wages, tips, other compensation 52,345.67 ")
    assert "Pat" not in out
    assert "Example" not in out
    assert "000-00-0000" not in out
    assert out.endswith("2025 12a D")


def test_the_result_model_holds_amounts_only() -> None:
    with pytest.raises(ValidationError):
        TaxFormRead.model_validate(
            {"jurisdiction": "US", "form_type": "W-2", "boxes": {"a": "000-00-0000"}}
        )
    with pytest.raises(ValidationError):
        TaxFormRead.model_validate(
            {"jurisdiction": "US", "form_type": "W-2", "boxes": {"recipient name": "1.00"}}
        )

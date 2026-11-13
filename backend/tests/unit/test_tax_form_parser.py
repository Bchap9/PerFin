# SPDX-License-Identifier: AGPL-3.0-or-later
"""The tax form parser on synthetic forms (``tests/fixtures/synth_tax_forms.py``).

PDFs are extracted in process and parsed by the registry as the worker does.
Every US form type is read exactly (type, year, issuer name, amount boxes,
W-2 box 12/14, 1098 box 10, 1095 coverage months and monthly amounts); a
consolidated statement gives one form per heading and identical copies are
kept once. A broker's composite statement (forms side by side under
year-first headings, dot leaders, a contents page) gives its forms and
nothing from its cover page. The parser output and the redacted extraction hold none of the
fake identity values printed on the forms.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

import pytest
from pydantic import ValidationError

from app.domain.tax.forms import form_def, sanitize_boxes
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers import confidence
from app.ingestion.parsers.base import Cell, DocumentView, ViewLine
from app.ingestion.parsers.registry import parse_extraction, reparse
from app.ingestion.parsers.tax_forms import (
    PARSER_NAME,
    REDACTED,
    TaxFormParser,
    read_tax_forms,
    redact_identity,
    redact_text,
    split_leaders,
)
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


def test_ssa_1099_is_not_read_as_a_1099() -> None:
    form = synth.synth_form("SSA-1099", seed=3)
    (read,) = _forms(_parse(synth.form_pdf(form)))
    assert read.form_type == "SSA-1099"
    assert set(read.boxes) == {"3", "5", "6"}
    # The form has no payer box: the only payer is the administration itself.
    assert read.issuer_name is None


def _line(index: int, y: float, *cells: tuple[float, str]) -> ViewLine:
    built = tuple(Cell(text, x, y, x + 6.0 * len(text), y + 10.0) for x, text in cells)
    return ViewLine(
        page=1, index=index, cells=built, text=" ".join(c.text for c in built), height=10.0
    )


def _view(*lines: ViewLine) -> DocumentView:
    return DocumentView(lines=lines, source_format="pdf", truncated=False, page_count=1)


def test_box_prefix_before_an_id_is_a_label() -> None:
    view = _view(
        _line(0, 40.0, (36.0, "Form SSA-1099"), (300.0, "2025")),
        _line(1, 80.0, (36.0, "Box 3. Benefits Paid in 2025"), (300.0, "Box 4. Benefits Repaid")),
        _line(2, 96.0, (36.0, "18,240.00"), (300.0, "NONE")),
        _line(3, 130.0, (36.0, "Box 5. Net Benefits for 2025")),
        _line(4, 146.0, (36.0, "18,240.00")),
    )
    (read,) = read_tax_forms(view, threshold=confidence.DEFAULT_THRESHOLD)
    assert read.form_type == "SSA-1099"
    assert read.tax_year == 2025
    assert read.boxes == {"3": Decimal("18240.00"), "5": Decimal("18240.00")}


def test_none_under_a_label_ends_the_search_for_its_amount() -> None:
    # Boxes 3, 4 and 5 in a row as the administration prints them; the lines
    # that describe box 3 put their amounts under box 4, which says NONE.
    view = _view(
        _line(
            0, 40.0, (36.0, "FORM SSA-1099 - SOCIAL SECURITY BENEFIT STATEMENT"), (420.0, "2025")
        ),
        _line(
            1,
            80.0,
            (36.0, "Box 3. Benefits Paid in 2025"),
            (215.0, "Box 4. Benefits Repaid to SSA in 2025"),
            (440.0, "Box 5. Net Benefits for 2025 (Box 3 minus Box 4)"),
        ),
        _line(2, 96.0, (36.0, "$17,310.00"), (215.0, "NONE"), (440.0, "$17,310.00")),
        _line(
            3,
            116.0,
            (36.0, "DESCRIPTION OF AMOUNT IN BOX 3"),
            (440.0, "Box 6. Voluntary Federal Income Tax Withheld"),
        ),
        _line(
            4,
            130.0,
            (36.0, "Paid by check or direct deposit"),
            (250.0, "$15,200.00"),
            (440.0, "None"),
        ),
        _line(5, 144.0, (36.0, "Medicare Part B premiums deducted"), (250.0, "$2,110.00")),
        _line(6, 158.0, (36.0, "Benefits for 2025"), (250.0, "$17,310.00"), (440.0, "$44.00")),
    )
    (read,) = read_tax_forms(view, threshold=confidence.DEFAULT_THRESHOLD)
    assert read.boxes == {"3": Decimal("17310.00"), "5": Decimal("17310.00")}


def test_none_beside_a_label_is_no_amount() -> None:
    view = _view(
        _line(0, 40.0, (36.0, "Form 1099-INT"), (300.0, "2025")),
        _line(1, 80.0, (36.0, "1 Interest income"), (220.0, "62.00")),
        _line(2, 96.0, (36.0, "4 Federal income tax withheld"), (220.0, "N/A")),
        _line(3, 112.0, (36.0, "Interest and withholding"), (220.0, "62.00")),
    )
    (read,) = read_tax_forms(view, threshold=confidence.DEFAULT_THRESHOLD)
    assert read.boxes == {"1": Decimal("62.00")}


@pytest.mark.parametrize(
    "text",
    [
        "Description of Amount in Box 3 Benefits Paid",  # a sentence naming the box
        "Boxes 3 Benefits Paid",
        "Box Box 3 Benefits Paid",
        "Box office 3 Benefits Paid",
    ],
)
def test_box_inside_a_sentence_is_no_label(text: str) -> None:
    view = _view(
        _line(0, 40.0, (36.0, "Form SSA-1099"), (300.0, "2025")),
        _line(1, 80.0, (36.0, text)),
        _line(2, 96.0, (36.0, "512.00")),
    )
    assert read_tax_forms(view, threshold=confidence.DEFAULT_THRESHOLD) == []


def _by_type(forms: list[TaxFormRead]) -> dict[str, TaxFormRead]:
    assert len({f.form_type for f in forms}) == len(forms)
    return {f.form_type: f for f in forms}


def test_composite_statement_reads_each_form() -> None:
    forms = _by_type(_forms(_parse(synth.composite_1099_pdf())))
    # The 1099-B summary prints no box ids; page 1 only mentions forms.
    assert set(forms) == {"1099-DIV", "1099-INT"}
    assert forms["1099-DIV"].boxes == synth.COMPOSITE_BOXES["1099-DIV"]
    assert forms["1099-DIV"].tax_year == 2025
    assert forms["1099-INT"].boxes == synth.COMPOSITE_BOXES["1099-INT"]
    assert forms["1099-INT"].tax_year == 2025
    assert all(f.low_confidence == [] for f in forms.values())


def test_composite_heading_gives_the_year() -> None:
    forms = _by_type(_forms(_parse(synth.composite_1099_pdf(2023))))
    assert {f.tax_year for f in forms.values()} == {2023}


def test_box_4_of_one_form_is_not_read_into_its_neighbour() -> None:
    forms = _by_type(_forms(_parse(synth.composite_1099_pdf())))
    assert forms["1099-DIV"].boxes["4"] == Decimal("184.02")
    assert forms["1099-INT"].boxes["4"] == Decimal("31.10")


def test_redacted_composite_statement_reads_the_same() -> None:
    parsed = _parse(synth.composite_1099_pdf())
    forms = _forms(parsed)
    redacted = redact_identity(parsed, forms)
    # An amount printed in one word with its label and leaders is still there.
    assert "1,840.25" in redacted.text
    again = _forms(parse_extraction(redacted, threshold=confidence.DEFAULT_THRESHOLD))
    assert _by_type(again).keys() == _by_type(forms).keys()
    for form in forms:
        assert _by_type(again)[form.form_type].boxes == form.boxes
        assert _by_type(again)[form.form_type].tax_year == form.tax_year


def test_contents_and_sentences_start_no_form() -> None:
    result = _extract(synth.composite_1099_pdf())
    cover = result.model_copy(update={"pages": result.pages[:1]})
    view = DocumentView.from_result(cover)
    assert any("Form 1099-DIV" in line.text for line in view.lines)
    assert read_tax_forms(view, threshold=confidence.DEFAULT_THRESHOLD) == []
    # No section at all: not a heading whose boxes happened to be missing.
    assert TaxFormParser().detect(view) == 0.0


@pytest.mark.parametrize(
    "mention",
    [
        ((36.0, "Form 1099-DIV Dividends and Distributions ........ 2"),),  # a contents line
        ((36.0, "Form 1099-DIV Dividends and Distributions"), (400.0, "2")),
        ((36.0, "This statement includes Form 1099-DIV, Form 1099-INT and Form 1099-MISC."),),
    ],
)
def test_rows_after_a_mention_are_not_read(mention: tuple[tuple[float, str], ...]) -> None:
    view = _view(
        _line(0, 40.0, *mention),
        _line(1, 56.0, (36.0, "1a Total ordinary dividends"), (300.0, "1b Qualified dividends")),
        _line(2, 70.0, (36.0, "15.00"), (300.0, "11.00")),
        _line(3, 90.0, (36.0, "1 Interest income"), (300.0, "2 Royalties")),
        _line(4, 104.0, (36.0, "6.00"), (300.0, "3.00")),
    )
    assert read_tax_forms(view, threshold=confidence.DEFAULT_THRESHOLD) == []
    assert TaxFormParser().detect(view) == 0.0


def test_a_contents_like_line_with_an_amount_is_a_heading() -> None:
    # It ends in a number, but the line holds an amount with decimals.
    view = _view(
        _line(
            0,
            40.0,
            (36.0, "Form 1099-INT"),
            (200.0, "1 Interest income"),
            (400.0, "6.25"),
            (480.0, "2"),
        ),
    )
    (read,) = read_tax_forms(view, threshold=confidence.DEFAULT_THRESHOLD)
    assert read.boxes == {"1": Decimal("6.25")}


def _staggered(*lower: ViewLine) -> DocumentView:
    """1099-DIV beside 1099-INT, cut at x=320, then ``lower``."""
    return _view(
        _line(
            0,
            40.0,
            (36.0, "2025 1099-DIV Dividends and Distributions"),
            (320.0, "2025 1099-INT Interest Income"),
        ),
        _line(
            1,
            56.0,
            (36.0, "1a Total ordinary dividends......1,522.10"),
            (320.0, "1 Interest income......208.40"),
        ),
        *lower,
    )


def _read_by_type(view: DocumentView) -> dict[str, dict[str, Decimal]]:
    forms = _by_type(read_tax_forms(view, threshold=confidence.DEFAULT_THRESHOLD))
    assert all(f.tax_year == 2025 for f in forms.values())
    return {form_type: dict(f.boxes) for form_type, f in forms.items()}


def test_a_heading_in_the_right_column_leaves_the_left_form_open() -> None:
    view = _staggered(
        _line(
            2,
            70.0,
            (36.0, "1b Qualified dividends......1,004.75"),
            (320.0, "3 Interest on U.S. Savings Bonds......2.50"),
        ),
        _line(3, 84.0, (36.0, "2a Total capital gain distributions......71.30")),
        _line(4, 98.0, (320.0, "2025 1099-MISC Miscellaneous Information")),
        _line(
            5,
            112.0,
            (36.0, "3 Nondividend distributions......14.00"),
            (320.0, "2 Royalties......48.00"),
        ),
        _line(
            6,
            126.0,
            (36.0, "4 Federal income tax withheld......152.21"),
            (320.0, "8 Substitute payments in lieu......6.00"),
        ),
        _line(7, 140.0, (36.0, "7 Foreign tax paid......8.80")),
    )
    assert _read_by_type(view) == {
        "1099-DIV": {
            "1a": Decimal("1522.10"),
            "1b": Decimal("1004.75"),
            "2a": Decimal("71.30"),
            "3": Decimal("14.00"),
            "4": Decimal("152.21"),
            "7": Decimal("8.80"),
        },
        "1099-INT": {"1": Decimal("208.40"), "3": Decimal("2.50")},
        "1099-MISC": {"2": Decimal("48.00"), "8": Decimal("6.00")},
    }


def test_a_heading_beside_a_row_of_the_left_form() -> None:
    view = _staggered(
        _line(
            2,
            70.0,
            (36.0, "1b Qualified dividends......1,004.75"),
            (320.0, "2025 1099-MISC Miscellaneous Information"),
        ),
        _line(
            3,
            84.0,
            (36.0, "3 Nondividend distributions......14.00"),
            (320.0, "2 Royalties......48.00"),
        ),
        _line(
            4,
            98.0,
            (36.0, "4 Federal income tax withheld......152.21"),
            (320.0, "8 Substitute payments in lieu......6.00"),
        ),
    )
    assert _read_by_type(view) == {
        "1099-DIV": {
            "1a": Decimal("1522.10"),
            "1b": Decimal("1004.75"),
            "3": Decimal("14.00"),
            "4": Decimal("152.21"),
        },
        "1099-INT": {"1": Decimal("208.40")},
        "1099-MISC": {"2": Decimal("48.00"), "8": Decimal("6.00")},
    }


def test_a_heading_in_the_left_column_closes_the_columns_to_its_right() -> None:
    # Under a left heading the right side may be the old form's rows or the new
    # form's second column: nobody reads it until a heading opens that column.
    view = _staggered(
        _line(2, 70.0, (36.0, "2025 1099-MISC Miscellaneous Information")),
        _line(
            3,
            84.0,
            (36.0, "2 Royalties......48.00"),
            (320.0, "4 Federal income tax withheld......9.00"),
        ),
        _line(4, 98.0, (320.0, "2025 1099-NEC Nonemployee Compensation")),
        _line(
            5,
            112.0,
            (36.0, "8 Substitute payments in lieu......6.00"),
            (320.0, "1 Nonemployee compensation......70.00"),
        ),
    )
    assert _read_by_type(view) == {
        "1099-DIV": {"1a": Decimal("1522.10")},
        "1099-INT": {"1": Decimal("208.40")},
        "1099-MISC": {"2": Decimal("48.00"), "8": Decimal("6.00")},
        "1099-NEC": {"1": Decimal("70.00")},
    }


def test_a_heading_wider_than_its_column_takes_the_page() -> None:
    view = _staggered(
        _line(
            2, 70.0, (36.0, "2025 1099-MISC Miscellaneous Information for recipients and nominees")
        ),
        _line(
            3,
            84.0,
            (36.0, "2 Royalties......48.00"),
            (330.0, "4 Federal income tax withheld......9.00"),
        ),
    )
    assert _read_by_type(view) == {
        "1099-DIV": {"1a": Decimal("1522.10")},
        "1099-INT": {"1": Decimal("208.40")},
        "1099-MISC": {"2": Decimal("48.00"), "4": Decimal("9.00")},
    }


def test_a_box_with_leaders_and_no_amount_is_not_read_from_the_next_line() -> None:
    view = _view(
        _line(0, 40.0, (36.0, "2025 1099-DIV Dividends and Distributions")),
        _line(1, 56.0, (36.0, "1a Total ordinary dividends......1,522.10")),
        _line(2, 70.0, (36.0, "5 Section 199A dividends......")),
        _line(3, 84.0, (36.0, "Total dividends and distributions......1,900.00")),
        _line(4, 98.0, (36.0, "7 Foreign tax paid ......"), (300.0, "8.80")),
        _line(
            5,
            112.0,
            (36.0, "3 Nondividend distributions......"),
            (300.0, "6 Investment expenses......4.00"),
        ),
        _line(6, 126.0, (36.0, "Subtotal......77.00")),
    )
    assert _read_by_type(view) == {
        "1099-DIV": {"1a": Decimal("1522.10"), "7": Decimal("8.80"), "6": Decimal("4.00")}
    }


def test_detect_looks_at_every_section() -> None:
    # The first section (the 1099-B summary) holds no box at all.
    view = DocumentView.from_result(_extract(synth.composite_1099_pdf()))
    assert TaxFormParser().detect(view) > 0.9


def test_identity_labels_alone_do_not_make_a_tax_form() -> None:
    view = _view(
        _line(0, 40.0, (36.0, "Form 1099-DIV"), (300.0, "2025")),
        _line(1, 80.0, (36.0, "Recipient's name"), (300.0, "Account number")),
        _line(2, 96.0, (36.0, "Pat Example"), (300.0, "12345")),
    )
    assert TaxFormParser().detect(view) < 0.35


@pytest.mark.parametrize(
    ("text", "pieces"),
    [
        ("Total ordinary dividends......1,840.25", ["Total ordinary dividends", "1,840.25"]),
        ("Interest income . . . . . 311.08", ["Interest income", "311.08"]),
        ("Rents ...... 40.00 Royalties ...... 2.00", ["Rents", "40.00 Royalties", "2.00"]),
        ("U.S. Treasury obligations", ["U.S. Treasury obligations"]),
        ("No. 0000-0000", ["No. 0000-0000"]),
        ("see above..", ["see above.."]),
    ],
)
def test_split_leaders(text: str, pieces: list[str]) -> None:
    cell = Cell(text, 100.0, 10.0, 100.0 + 5.0 * len(text), 20.0)
    cut = split_leaders(cell)
    assert [c.text for c in cut] == pieces
    if len(pieces) == 1:
        assert cut == (cell,)
        return
    # Pieces keep their place on the line, left to right, inside the cell.
    assert cut[0].x0 == cell.x0
    assert cut[-1].x1 == cell.x1
    for before, after in pairwise(cut):
        assert before.x1 < after.x0


def test_split_leaders_places_pieces_by_word_boxes() -> None:
    words = (("Net", 10.0, 30.0), ("gain....9.50", 34.0, 154.0))
    cell = Cell("Net gain....9.50", 10.0, 0.0, 154.0, 10.0, None, words)
    label, amount = split_leaders(cell)
    assert (label.text, label.x0, label.x1) == ("Net gain", 10.0, 74.0)
    assert (amount.text, amount.x0, amount.x1) == ("9.50", 114.0, 154.0)
    assert label.words == (("Net", 10.0, 30.0), ("gain", 34.0, 74.0))


def test_a_lone_number_before_other_words_is_no_box() -> None:
    view = _view(
        _line(0, 40.0, (36.0, "Form 1099-DIV"), (300.0, "2025")),
        _line(1, 80.0, (36.0, "4"), (60.0, "Copies enclosed"), (300.0, "12.00")),
        _line(2, 96.0, (36.0, "3"), (60.0, "88.00")),
    )
    assert read_tax_forms(view, threshold=confidence.DEFAULT_THRESHOLD) == []


def test_a_year_and_type_inside_a_sentence_start_no_form() -> None:
    view = _view(
        _line(0, 40.0, (36.0, "Form 1099-INT"), (300.0, "2024")),
        _line(1, 60.0, (36.0, "Compare with your 2025 1099-DIV and the details (Form 1099-DIV)")),
        _line(2, 80.0, (36.0, "1 Interest income"), (300.0, "4 Federal income tax withheld")),
        _line(3, 96.0, (36.0, "40.00"), (300.0, "4.00")),
    )
    (read,) = read_tax_forms(view, threshold=confidence.DEFAULT_THRESHOLD)
    assert read.form_type == "1099-INT"
    assert read.tax_year == 2024
    assert read.boxes == {"1": Decimal("40.00"), "4": Decimal("4.00")}


@pytest.mark.parametrize(
    "text",
    [
        "2024 1099-INT corrections were issued separately",
        "2024 1099-INT and other forms",
        "2024 1099-INT Summary of interest income",
    ],
)
def test_a_year_and_type_before_other_words_start_no_form(text: str) -> None:
    view = _view(
        _line(0, 40.0, (36.0, "2025 1099-INT Interest Income")),
        _line(1, 56.0, (36.0, "1 Interest income......208.40")),
        _line(2, 70.0, (36.0, text)),
        _line(3, 84.0, (36.0, "4 Federal income tax withheld......20.84")),
    )
    (read,) = read_tax_forms(view, threshold=confidence.DEFAULT_THRESHOLD)
    assert read.tax_year == 2025
    assert read.boxes == {"1": Decimal("208.40"), "4": Decimal("20.84")}


def test_an_earnings_summary_line_does_not_split_a_w2() -> None:
    view = _view(
        _line(0, 40.0, (36.0, "Form W-2 Wage and Tax Statement"), (400.0, "2025")),
        _line(
            1,
            56.0,
            (36.0, "1 Wages, tips, other compensation"),
            (300.0, "2 Federal income tax withheld"),
        ),
        _line(2, 70.0, (36.0, "41,000.00"), (300.0, "4,100.00")),
        _line(3, 90.0, (36.0, "2025 W-2 and EARNINGS SUMMARY")),
        _line(
            4, 104.0, (36.0, "3 Social security wages"), (300.0, "4 Social security tax withheld")
        ),
        _line(5, 118.0, (36.0, "43,000.00"), (300.0, "2,666.00")),
    )
    (read,) = read_tax_forms(view, threshold=confidence.DEFAULT_THRESHOLD)
    assert read.form_type == "W-2"
    assert set(read.boxes) == {"1", "2", "3", "4"}


@pytest.mark.parametrize("text", ["2025 1099-INT", "2025 1099-INT Interest Income (continued)"])
def test_a_year_and_type_alone_or_with_the_title_is_a_heading(text: str) -> None:
    view = _view(
        _line(0, 40.0, (36.0, text)),
        _line(1, 56.0, (36.0, "1 Interest income......208.40")),
    )
    (read,) = read_tax_forms(view, threshold=confidence.DEFAULT_THRESHOLD)
    assert (read.form_type, read.tax_year) == ("1099-INT", 2025)


def test_identical_copies_are_kept_once() -> None:
    form = synth.synth_form("W-2", seed=4)
    reads = _forms(_parse(synth.forms_pdf([form, form, form])))
    assert len(reads) == 1


def test_reparse_keeps_the_choice() -> None:
    first = _parse(synth.form_pdf(synth.synth_form("1098", seed=5)))
    again = reparse(first)
    assert _forms(again) == _forms(first)


def test_a_mention_of_a_form_is_not_a_form() -> None:
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
    if form.issuer is not None:
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
    # Label, leaders and amount in one word: kept when every piece is.
    assert redact_text("dividends......1,840.25") == "dividends......1,840.25"
    assert redact_text("Example......1,840.25") == REDACTED
    assert redact_text("dividends......000-00-0000") == REDACTED


def test_the_result_model_holds_amounts_only() -> None:
    with pytest.raises(ValidationError):
        TaxFormRead.model_validate(
            {"jurisdiction": "US", "form_type": "W-2", "boxes": {"a": "000-00-0000"}}
        )
    with pytest.raises(ValidationError):
        TaxFormRead.model_validate(
            {"jurisdiction": "US", "form_type": "W-2", "boxes": {"recipient name": "1.00"}}
        )


def test_identity_scan_ignores_tokens_inside_random_strings() -> None:
    # The CI collision: an argon2 salt that happened to spell "quill".
    hash_row = "$argon2id$v=19$m=1024,t=1,p=1$nsuwz9h0zyquillmwsalg1tgy"
    assert synth.identity_tokens_in(hash_row) == []
    # A name or address token printed as a word is still found...
    assert synth.identity_tokens_in('{"recipient": "Jordan Quill Testperson"}') == [
        "testperson",
        "quill",
    ]
    assert synth.identity_tokens_in("'quuxwood':4 'lane':5") == ["quuxwood"]
    # ...and a number token is found even glued to a prefix.
    assert synth.identity_tokens_in("acct0000111122") == ["0000111122"]

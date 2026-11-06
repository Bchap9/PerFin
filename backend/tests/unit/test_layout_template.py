# SPDX-License-Identifier: AGPL-3.0-or-later
"""Learned layouts (``app.ingestion.parsers.template``; INGESTION.md "Teaching PerFin a layout").

Pages are built here word by word (invented labels and figures): anchors are
label text and a direction, never a value; tables are found by their column
headers on any page; the template fills a parser's statement; skipped lines
keep their boxes for the review highlight.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.ingestion.parsers.base import DocumentView
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.template import (
    FieldAnchor,
    LayoutTemplate,
    TableColumn,
    TableTemplate,
    derive_anchor,
    label_text,
    parse_template,
    read_template,
    read_value,
    table_from_selection,
)
from app.ingestion.sandbox.protocol import ExtractResult

Line = tuple[float, list[tuple[float, str]]]


def _page(number: int, lines: list[Line]) -> dict[str, Any]:
    words: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    for index, (y, cells) in enumerate(lines):
        first = len(words)
        for x, text in cells:
            words.append(
                {"text": text, "bbox": [x, y, x + 6.0 * len(text), y + 10.0], "line": index}
            )
        out.append(
            {
                "text": " ".join(t for _, t in cells),
                "bbox": [cells[0][0], y, cells[-1][0] + 6.0 * len(cells[-1][1]), y + 10.0],
                "first_word": first,
                "word_count": len(cells),
            }
        )
    return {
        "number": number,
        "width": 612.0,
        "height": 792.0,
        "unit": "pt",
        "text_source": "text_layer",
        "text": "\n".join(line["text"] for line in out),
        "words": words,
        "lines": out,
    }


def _row(y: float, day: str, payee: str, amount: str, kept: str) -> Line:
    words = [(60.0, day)]
    x = 160.0
    for part in payee.split():
        words.append((x, part))
        x += 6.0 * len(part) + 6.0
    if amount:
        words.append((400.0, amount))
    words.append((490.0, kept))
    return (y, words)


HEADER: Line = (200.0, [(60.0, "Posted"), (160.0, "Detail"), (400.0, "Change"), (490.0, "Kept")])


def _result(
    rows: list[Line], *, account: str = "QZ-5521-9034", opening: str = "100.00"
) -> ExtractResult:
    first = _page(
        1,
        [
            (40.0, [(60.0, "Invented"), (120.0, "Ledger"), (180.0, "Society")]),
            (80.0, [(60.0, "Holder"), (102.0, "ref:"), (140.0, account)]),
            (110.0, [(60.0, "Span")]),
            (125.0, [(60.0, "2026-03-01"), (130.0, "to"), (150.0, "2026-03-31")]),
            (150.0, [(60.0, "Brought"), (110.0, "in"), (140.0, opening)]),
            HEADER,
            *rows[:3],
        ],
    )
    second = _page(
        2, [HEADER, *rows[3:], (700.0, [(60.0, "Close"), (100.0, "of"), (120.0, "ledger")])]
    )
    return ExtractResult.model_validate({"kind": "pdf", "ok": True, "pages": [first, second]})


ROWS = [
    _row(220.0, "2026-03-02", "Corner Shop", "-10.00", "90.00"),
    _row(235.0, "2026-03-05", "Wage Payment", "50.00", "140.00"),
    _row(250.0, "2026-03-09", "Book Stall", "-5.50", "134.50"),
    _row(220.0, "2026-03-20", "Rail Pass", "-4.50", "130.00"),
    _row(235.0, "2026-03-22", "Parking", "", "130.00"),
]


def _template() -> LayoutTemplate:
    return LayoutTemplate(
        fields=[
            FieldAnchor(field="account_number", direction="right", label="Holder ref"),
            FieldAnchor(field="period_start", direction="below", label="Span"),
            FieldAnchor(field="period_end", direction="below", label="Span"),
            FieldAnchor(field="opening_balance", direction="right", label="Brought in"),
        ],
        tables=[
            TableTemplate(
                kind="transactions",
                columns=[
                    TableColumn(header="Posted", role="date"),
                    TableColumn(header="Detail", role="payee"),
                    TableColumn(header="Change", role="amount"),
                    TableColumn(header="Kept", role="balance"),
                ],
                end="Close of ledger",
            )
        ],
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Holder ref:", "Holder ref"),
        ("  Brought   in ", "Brought in"),
        ("Account 1234", None),
        ("Page 2", None),
        ("$100.00", None),
        ("::", None),
    ],
)
def test_a_label_never_holds_a_digit(text: str, expected: str | None) -> None:
    assert label_text(text) == expected


def test_templates_refuse_values_and_unusable_tables() -> None:
    with pytest.raises(ValidationError):
        FieldAnchor(field="account_number", direction="right", label="Account 5521")
    with pytest.raises(ValidationError):
        FieldAnchor(field="opening_balance", direction="band")
    with pytest.raises(ValidationError):
        TableColumn(header="Value 2026", role="balance")
    with pytest.raises(ValidationError, match="date and an amount"):
        TableTemplate(
            kind="transactions",
            columns=[
                TableColumn(header="Detail", role="payee"),
                TableColumn(header="Kept", role="balance"),
            ],
        )
    with pytest.raises(ValidationError, match="no such column role"):
        TableTemplate(
            kind="holdings",
            columns=[
                TableColumn(header="Units", role="quantity"),
                TableColumn(header="Name", role="security"),
                TableColumn(header="Posted", role="date"),
            ],
        )
    assert (
        parse_template({"fields": [{"field": "period_start", "direction": "right", "label": "1"}]})
        is None
    )
    assert parse_template(None) is None
    assert parse_template(_template().model_dump(mode="json")) == _template()


def test_values_are_read_by_field() -> None:
    assert read_value("account_number", "QZ-5521-9034") == "9034"
    assert read_value("account_number", "no digits") is None
    assert read_value("period_start", "1 Mar - 31 Mar 2026") == "2026-03-01"
    assert read_value("period_end", "1 Mar - 31 Mar 2026") == "2026-03-31"
    assert read_value("closing_balance", "Total: $1,234.50") == "1234.50"
    assert read_value("opening_balance", "(12.00)") == "-12.00"


def test_anchors_are_derived_from_the_label_next_to_or_above_a_value() -> None:
    result = _result(ROWS)
    view = DocumentView.from_result(result)
    page = result.pages[0]
    account = next(w for w in page.words if w.text.startswith("QZ"))
    anchor = derive_anchor(view, 1, list(account.bbox), "account_number")
    assert anchor == FieldAnchor(field="account_number", direction="right", label="Holder ref")
    period = [w for w in page.words if w.line == 3]
    box = [period[0].bbox[0], period[0].bbox[1], period[-1].bbox[2], period[-1].bbox[3]]
    assert derive_anchor(view, 1, box, "period_end") == FieldAnchor(
        field="period_end", direction="below", label="Span"
    )
    # A selection that holds its own label.
    line = [w for w in page.words if w.line == 4]
    whole = [line[0].bbox[0], line[0].bbox[1], line[-1].bbox[2], line[-1].bbox[3]]
    assert derive_anchor(view, 1, whole, "opening_balance") == FieldAnchor(
        field="opening_balance", direction="right", label="Brought in"
    )
    # Nothing labels the institution's name: no anchor.
    name = [w for w in page.words if w.line == 0]
    assert derive_anchor(view, 1, list(name[0].bbox), "account_name") is None


def test_a_table_is_read_on_every_page_its_header_starts() -> None:
    result = _result(ROWS)
    view = DocumentView.from_result(result)
    taught = read_template(view, _template(), threshold=0.85)
    statement = taught.statements[0]
    assert statement.account.last4 == "9034"
    assert (statement.period_start, statement.period_end) == (date(2026, 3, 1), date(2026, 3, 31))
    assert [(t.posted, t.amount, t.payee) for t in statement.transactions] == [
        (date(2026, 3, 2), Decimal("-10.00"), "Corner Shop"),
        (date(2026, 3, 5), Decimal("50.00"), "Wage Payment"),
        (date(2026, 3, 9), Decimal("-5.50"), "Book Stall"),
        (date(2026, 3, 20), Decimal("-4.50"), "Rail Pass"),
    ]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert statement.parser is not None
    assert statement.parser.name == "layout_template"
    # The dated line without an amount is skipped, with its box.
    assert taught.skipped_lines == 1
    assert [page for page, _ in taught.skipped] == [2]


def test_a_table_template_comes_from_a_selected_box_and_its_columns() -> None:
    result = _result(ROWS)
    view = DocumentView.from_result(result)
    table = table_from_selection(
        view,
        1,
        [50.0, 198.0, 600.0, 262.0],
        "transactions",
        [
            (50.0, 150.0, "date"),
            (150.0, 390.0, "payee"),
            (390.0, 480.0, "amount"),
            (480.0, 600.0, "ignore"),
        ],
    )
    assert [(c.header, c.role) for c in table.columns] == [
        ("Posted", "date"),
        ("Detail", "payee"),
        ("Change", "amount"),
        ("Kept", "ignore"),
    ]
    with pytest.raises(ValueError, match="date and an amount"):
        table_from_selection(
            view,
            1,
            [50.0, 198.0, 600.0, 262.0],
            "transactions",
            [(50.0, 150.0, "date"), (150.0, 600.0, "payee")],
        )


def test_the_registry_applies_a_learned_layout_and_a_parser_statement_is_filled() -> None:
    result = _result(ROWS)
    parsed = parse_extraction(result, template=_template())
    assert parsed.parse is not None
    assert parsed.parse.parser is not None
    assert parsed.parse.parser.name == "layout_template"
    assert "no_parser" not in parsed.parse.warnings
    assert len(parsed.statements[0].transactions) == 4
    assert parsed.parse.skipped_lines == 1
    assert parsed.parse.skipped_regions[0].page == 2
    # Without the template nothing reads the layout.
    plain = parse_extraction(result)
    assert plain.statements == []
    assert plain.parse is not None
    assert "no_parser" in plain.parse.warnings

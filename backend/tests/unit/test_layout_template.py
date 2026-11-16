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

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers import template as template_module
from app.ingestion.parsers.base import DocumentView
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.template import (
    FieldAnchor,
    LayoutTemplate,
    TableColumn,
    TableTemplate,
    derive_anchor,
    label_text,
    merge_templates,
    note_text,
    parse_template,
    read_anchor,
    read_template,
    read_value,
    rebalance,
    table_from_selection,
)
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult
from tests.fixtures import synth_taught_layout as synth

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


def test_an_anchor_must_read_the_selected_value_back() -> None:
    """A label printed twice would read the first figure on the next statement."""
    lines: list[Line] = [
        (100.0, [(60.0, "Balance"), (400.0, "$100.00")]),
        (120.0, [(60.0, "Payments"), (400.0, "$20.00")]),
        (140.0, [(60.0, "Balance"), (400.0, "$150.00")]),
        (160.0, [(60.0, "New"), (100.0, "balance"), (400.0, "$150.00")]),
    ]
    view = DocumentView.from_result(
        ExtractResult.model_validate({"kind": "pdf", "ok": True, "pages": [_page(1, lines)]})
    )
    # The second "Balance": no label finds it again, so it is not remembered.
    assert derive_anchor(view, 1, (390.0, 138.0, 460.0, 152.0), "closing_balance") is None
    # The first one reads back.
    first = derive_anchor(view, 1, (390.0, 98.0, 460.0, 112.0), "opening_balance")
    assert first == FieldAnchor(field="opening_balance", direction="right", label="Balance")
    # A distinct label reads its own value.
    last = derive_anchor(view, 1, (390.0, 158.0, 460.0, 172.0), "closing_balance")
    assert last == FieldAnchor(field="closing_balance", direction="right", label="New balance")
    found = read_anchor(view, last)
    assert found is not None
    assert found.value == "150.00"


def test_a_label_in_every_page_header_is_anchored_from_any_page() -> None:
    """Selected on page 2, a header value is read back (identical) on page 1."""
    lines: list[Line] = [
        (40.0, [(60.0, "Holder"), (110.0, "ref:"), (170.0, "QZ-7731")]),
        (100.0, [(60.0, "Brought"), (110.0, "in"), (400.0, "$100.00")]),
    ]
    view = DocumentView.from_result(
        ExtractResult.model_validate(
            {"kind": "pdf", "ok": True, "pages": [_page(1, lines), _page(2, lines)]}
        )
    )
    expected = FieldAnchor(field="account_number", direction="right", label="Holder ref")
    box = (165.0, 38.0, 230.0, 52.0)
    assert derive_anchor(view, 1, box, "account_number") == expected
    assert derive_anchor(view, 2, box, "account_number") == expected


def test_notes_keep_no_figures() -> None:
    assert note_text("my other account 98765432 sort 12-34-56") == "my other account sort"
    assert note_text("  page\n two  ") == "page two"
    assert note_text("12345678") is None
    assert note_text(None) is None
    template = LayoutTemplate(notes=["ref 4821 is the joint one", "1234"])
    assert template.notes == ["ref is the joint one"]


def test_teaching_a_layout_again_adds_to_what_was_taught() -> None:
    earlier = _template()
    later = LayoutTemplate(
        fields=[FieldAnchor(field="account_number", direction="right", label="Card ref")],
        ignore=["Carried forward"],
        notes=["second lesson"],
    )
    merged, kept = merge_templates(earlier, later)
    anchors = {a.field: a for a in merged.fields}
    assert anchors["account_number"].label == "Card ref"
    assert {a.field for a in merged.fields} == {a.field for a in earlier.fields} | {
        "account_number"
    }
    assert [t.kind for t in merged.tables] == [t.kind for t in earlier.tables]
    assert "account_number" not in kept
    assert all(f"table:{t.kind}" in kept for t in earlier.tables)
    assert merged.ignore[0] == "Carried forward"
    assert set(earlier.ignore) <= set(merged.ignore)
    assert merged.notes[0] == "second lesson"
    # Nothing new: the earlier template as it was.
    same, kept_all = merge_templates(earlier, None)
    assert same == earlier
    assert len(kept_all) == len(earlier.fields) + len(earlier.tables)


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


# ---------------------------------------------------------------------------
# Import 1d: a direction per table, several tables, continued pages, month names
# ---------------------------------------------------------------------------


def _pdf_view(data: bytes) -> DocumentView:
    result = extract_pdf(data, ExtractOptions(previews=False, ocr=False), "pdf")
    assert result.ok, result.error
    return DocumentView.from_result(result)


def _hand_view(*pages: list[Line]) -> DocumentView:
    built = [_page(number, lines) for number, lines in enumerate(pages, start=1)]
    return DocumentView.from_result(
        ExtractResult.model_validate({"kind": "pdf", "ok": True, "pages": built})
    )


def _anchors(opening: str = "Carried in", closing: str = "Carried out") -> list[FieldAnchor]:
    return [
        FieldAnchor(field="account_number", direction="right", label="Member key"),
        FieldAnchor(field="period_start", direction="below", label="Window"),
        FieldAnchor(field="period_end", direction="below", label="Window"),
        FieldAnchor(field="opening_balance", direction="right", label=opening),
        FieldAnchor(field="closing_balance", direction="right", label=closing),
    ]


def _ledger(**more: Any) -> TableTemplate:
    return TableTemplate(
        kind="transactions",
        columns=[
            TableColumn(header="Booked", role="date"),
            TableColumn(header="Narrative", role="payee"),
            TableColumn(header="Movement", role="amount"),
            TableColumn(header="Kept", role="balance"),
        ],
        **more,
    )


def _movements(end: str, direction: str) -> TableTemplate:
    return TableTemplate.model_validate(
        {
            "kind": "transactions",
            "columns": [
                {"header": "Booked", "role": "date"},
                {"header": "Narrative", "role": "payee"},
                {"header": "Movement", "role": "amount"},
            ],
            "end": end,
            "direction": direction,
        }
    )


def _read(view: DocumentView, template: LayoutTemplate) -> list[tuple[date, Decimal, str | None]]:
    taught = read_template(view, template, threshold=0.85)
    assert len(taught.statements) == 1, taught.warnings
    return [(t.posted, t.amount, t.payee) for t in taught.statements[0].transactions]


#: A template as ``parser_fingerprint.template`` holds it since the first release.
STORED_V1: dict[str, Any] = {
    "version": 1,
    "fields": [
        {"field": "account_number", "direction": "right", "label": "Holder ref"},
        {"field": "period_start", "direction": "below", "label": "Span"},
        {"field": "period_end", "direction": "below", "label": "Span"},
        {"field": "opening_balance", "direction": "right", "label": "Brought in"},
    ],
    "tables": [
        {
            "kind": "transactions",
            "columns": [
                {"header": "Posted", "role": "date"},
                {"header": "Detail", "role": "payee"},
                {"header": "Change", "role": "amount"},
                {"header": "Kept", "role": "balance"},
            ],
            "end": "Close of ledger",
        }
    ],
    "ignore": [],
    "notes": [],
}


def test_stored_templates_without_the_new_fields_still_parse() -> None:
    stored = parse_template(STORED_V1)
    assert stored is not None
    assert stored.version == 1
    assert stored.tables[0].direction == "as_printed"
    assert stored.tables[0].continues is False
    # It reads what it read before: signed amounts as printed, a table per page header.
    assert _read(DocumentView.from_result(_result(ROWS)), stored) == [
        (date(2026, 3, 2), Decimal("-10.00"), "Corner Shop"),
        (date(2026, 3, 5), Decimal("50.00"), "Wage Payment"),
        (date(2026, 3, 9), Decimal("-5.50"), "Book Stall"),
        (date(2026, 3, 20), Decimal("-4.50"), "Rail Pass"),
    ]


def test_two_transaction_tables_with_directions_reconcile() -> None:
    spec = synth.split_statement()
    view = _pdf_view(synth.two_tables_statement())
    template = LayoutTemplate(
        fields=_anchors(),
        tables=[
            _movements("Received in all", "money_in"),
            _movements("Sent in all", "money_out"),
        ],
    )
    taught = read_template(view, template, threshold=0.85)
    assert len(taught.statements) == 1
    statement = taught.statements[0]
    # One statement, the rows of both tables in page order; the second table is money out.
    assert [(t.posted, t.amount, t.payee) for t in statement.transactions] == [
        (r.day, r.amount, r.narrative) for r in spec.rows
    ]
    assert all(t.amount < 0 for t in statement.transactions[len(spec.received) :])
    assert taught.row_tables == [0] * len(spec.received) + [1] * len(spec.sent)
    assert [len(read.rows) for read in taught.tables] == [len(spec.received), len(spec.sent)]
    assert taught.skipped_lines == 0
    assert statement.account.last4 == spec.last4
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    checked = rebalance(statement).reconciliation
    assert checked is not None
    assert checked.status == "ok"

    # Both tables as printed: every row is money in, and the statement does not add up.
    printed = LayoutTemplate(
        fields=_anchors(),
        tables=[
            _movements("Received in all", "as_printed"),
            _movements("Sent in all", "as_printed"),
        ],
    )
    wrong = read_template(view, printed, threshold=0.85).statements[0]
    assert all(t.amount > 0 for t in wrong.transactions)
    assert wrong.reconciliation is not None
    assert wrong.reconciliation.status == "mismatch"


def _three(y: float, day: str, payee: str, *amount: str) -> Line:
    return (
        y,
        [(60.0, day), (160.0, payee), *((400.0 + 50.0 * i, a) for i, a in enumerate(amount))],
    )


THREE: Line = (200.0, [(60.0, "Posted"), (160.0, "Detail"), (400.0, "Change")])


def _change(direction: str) -> LayoutTemplate:
    return LayoutTemplate(
        tables=[
            TableTemplate.model_validate(
                {
                    "kind": "transactions",
                    "columns": [
                        {"header": "Posted", "role": "date"},
                        {"header": "Detail", "role": "payee"},
                        {"header": "Change", "role": "amount"},
                    ],
                    "direction": direction,
                }
            )
        ]
    )


def test_a_printed_minus_in_a_money_out_table_is_money_in() -> None:
    view = _hand_view(
        [
            THREE,
            _three(220.0, "2026-03-02", "Shop", "12.00"),
            _three(235.0, "2026-03-05", "Refund", "-3.00"),
            _three(250.0, "2026-03-09", "Reversal", "(4.00)"),
            _three(265.0, "2026-03-11", "Marked", "6.00", "CR"),
            _three(280.0, "2026-03-12", "Owing", "7.00", "DR"),
        ]
    )
    amounts = {
        direction: [amount for _, amount, _ in _read(view, _change(direction))]
        for direction in ("as_printed", "money_in", "money_out", "card")
    }
    printed = [Decimal(v) for v in ("12.00", "-3.00", "-4.00", "6.00", "-7.00")]
    assert amounts["as_printed"] == printed
    # Money in is what an unsigned figure already means; a printed minus takes it back.
    assert amounts["money_in"] == printed
    # Money out: an unsigned figure is spending, a printed minus or bracket is
    # money in (a refund); a printed CR or DR says which way on its own.
    out = [Decimal(v) for v in ("-12.00", "3.00", "4.00", "6.00", "-7.00")]
    assert amounts["money_out"] == out
    assert amounts["card"] == out


def test_a_direction_leaves_debit_and_credit_columns_alone() -> None:
    header: Line = (200.0, [(60.0, "Posted"), (160.0, "Detail"), (400.0, "Paid"), (490.0, "Got")])
    view = _hand_view(
        [
            header,
            (220.0, [(60.0, "2026-03-02"), (160.0, "Shop"), (400.0, "5.00")]),
            (235.0, [(60.0, "2026-03-05"), (160.0, "Wage"), (490.0, "7.00")]),
        ]
    )
    columns = [
        {"header": "Posted", "role": "date"},
        {"header": "Detail", "role": "payee"},
        {"header": "Paid", "role": "debit"},
        {"header": "Got", "role": "credit"},
    ]
    for direction in ("as_printed", "money_in", "money_out", "card"):
        table = TableTemplate.model_validate(
            {"kind": "transactions", "columns": columns, "direction": direction}
        )
        read = _read(view, LayoutTemplate(tables=[table]))
        assert [amount for _, amount, _ in read] == [Decimal("-5.00"), Decimal("7.00")], direction


def test_card_direction_negates() -> None:
    spec = synth.card_statement()
    view = _pdf_view(synth.card_positive_charges_statement())

    def taught(direction: str) -> LayoutTemplate:
        return LayoutTemplate(
            fields=_anchors("Owed before", "Owed now"),
            tables=[
                TableTemplate.model_validate(
                    {
                        "kind": "transactions",
                        "columns": [
                            {"header": "Booked", "role": "date"},
                            {"header": "Narrative", "role": "payee"},
                            {"header": "Charged", "role": "amount"},
                        ],
                        "end": "End of charges",
                        "direction": direction,
                    }
                )
            ],
        )

    statement = read_template(view, taught("card"), threshold=0.85).statements[0]
    # Charges printed positive are spending; the payment and the refund are money in.
    assert [(t.posted, t.amount, t.payee) for t in statement.transactions] == [
        (r.day, r.amount, r.narrative) for r in spec.rows
    ]
    # The balances of a card are amounts owed: negative in the ledger, like the rows.
    assert statement.opening_balance is not None
    assert statement.ledger_balance is not None
    assert statement.opening_balance.amount == -spec.owed_before
    assert statement.ledger_balance.amount == -spec.owed_now
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    # Any other direction keeps the balances as they are printed.
    plain = read_template(view, taught("as_printed"), threshold=0.85).statements[0]
    assert plain.opening_balance is not None
    assert plain.opening_balance.amount == spec.owed_before


def test_table_continues_on_a_page_without_its_header() -> None:
    spec = synth.continued_statement()
    view = _pdf_view(synth.continued_table_statement())
    assert len(spec.rows) == 30

    continued = LayoutTemplate(
        fields=_anchors(), tables=[_ledger(end="End of ledger", continues=True)]
    )
    taught = read_template(view, continued, threshold=0.85)
    statement = taught.statements[0]
    assert [(t.posted, t.amount, t.payee) for t in statement.transactions] == synth.expected(spec)
    assert [t.page for t in statement.transactions].count(2) == 30 - synth.CONTINUED_FIRST_SHEET
    assert taught.skipped_lines == 0
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"

    # Not told that it continues: the rows of the sheet with the header, as before.
    single = LayoutTemplate(fields=_anchors(), tables=[_ledger(end="End of ledger")])
    assert _read(view, single) == synth.expected(spec)[: synth.CONTINUED_FIRST_SHEET]


def test_a_continued_table_stops_at_its_end_text() -> None:
    spec = synth.continued_statement()
    view = _pdf_view(synth.continued_table_statement())
    ended = LayoutTemplate(fields=_anchors(), tables=[_ledger(end="End of ledger", continues=True)])
    payees = [payee for _, _, payee in _read(view, ended)]
    assert len(payees) == len(spec.rows)
    assert synth.CONTINUED_NOTICE not in payees
    # The end text is what stops it: without one, the dated notice under the
    # ledger is read as a row (and the statement no longer adds up).
    endless = LayoutTemplate(fields=_anchors(), tables=[_ledger(continues=True)])
    statement = read_template(view, endless, threshold=0.85).statements[0]
    assert statement.transactions[-1].payee == synth.CONTINUED_NOTICE
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "mismatch"


def test_a_continued_table_stops_at_the_header_of_another_table() -> None:
    other: Line = (300.0, [(60.0, "Dated"), (160.0, "Reason"), (400.0, "Fee")])
    view = _hand_view(
        [THREE, _three(220.0, "2026-03-02", "Shop", "-12.00")],
        [
            (40.0, [(60.0, "Invented"), (120.0, "Ledger"), (180.0, "Society")]),
            # No date in the date column: not yet the table.
            (60.0, [(60.0, "Sheet"), (100.0, "two"), (400.0, "9.99")]),
            _three(220.0, "2026-03-05", "Wage", "50.00"),
            other,
            _three(320.0, "2026-03-09", "Upkeep", "2.50"),
        ],
    )
    first = _change("as_printed").tables[0].model_copy(update={"continues": True})
    second = TableTemplate.model_validate(
        {
            "kind": "transactions",
            "columns": [
                {"header": "Dated", "role": "date"},
                {"header": "Reason", "role": "payee"},
                {"header": "Fee", "role": "amount"},
            ],
            "direction": "money_out",
        }
    )
    taught = read_template(view, LayoutTemplate(tables=[first, second]), threshold=0.85)
    assert [(t.amount, t.payee) for t in taught.statements[0].transactions] == [
        (Decimal("-12.00"), "Shop"),
        (Decimal("50.00"), "Wage"),
        (Decimal("-2.50"), "Upkeep"),
    ]
    assert taught.row_tables == [0, 0, 1]
    assert taught.skipped_lines == 0


def test_a_repeated_header_on_the_next_page_continues_the_open_table() -> None:
    """Two tables under the same headers: the header on a new page is the open table's."""
    view = _hand_view(
        [
            THREE,
            _three(220.0, "2026-03-02", "Wage", "50.00"),
            (235.0, [(60.0, "Sum"), (90.0, "in")]),
            (260.0, [(60.0, "Posted"), (160.0, "Detail"), (400.0, "Change")]),
            _three(280.0, "2026-03-05", "Shop", "12.00"),
        ],
        [THREE, _three(220.0, "2026-03-09", "Stall", "4.00")],
    )
    tables = [
        _change("money_in").tables[0].model_copy(update={"end": "Sum in"}),
        _change("money_out").tables[0].model_copy(update={"end": "Sum out"}),
    ]
    taught = read_template(view, LayoutTemplate(tables=tables), threshold=0.85)
    assert [t.amount for t in taught.statements[0].transactions] == [
        Decimal("50.00"),
        Decimal("-12.00"),
        Decimal("-4.00"),
    ]
    assert taught.row_tables == [0, 1, 1]


def test_month_name_dates_take_the_period_year() -> None:
    spec = synth.month_name_statement()
    assert {r.day.year for r in spec.rows} == {2025, 2026}
    template = LayoutTemplate(fields=_anchors(), tables=[_ledger(end="End of ledger")])
    taught = read_template(_pdf_view(synth.month_name_rows_statement()), template, threshold=0.85)
    statement = taught.statements[0]
    assert [(t.posted, t.amount, t.payee) for t in statement.transactions] == synth.expected(spec)
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert "dates_without_year" not in taught.warnings


def test_month_name_dates_without_a_period_read_no_rows() -> None:
    spec = synth.month_name_statement()
    template = LayoutTemplate(fields=_anchors(), tables=[_ledger(end="End of ledger")])
    taught = read_template(
        _pdf_view(synth.month_name_rows_statement(period=False)), template, threshold=0.85
    )
    assert taught.statements == []
    assert not taught.read
    assert taught.skipped_lines == len(spec.rows)
    assert taught.warnings.count("dates_without_year") == 1


def test_five_transaction_tables_are_refused() -> None:
    def table(header: str) -> TableTemplate:
        return TableTemplate(
            kind="transactions",
            columns=[
                TableColumn(header="Posted", role="date"),
                TableColumn(header=header, role="amount"),
            ],
        )

    names = ("Alpha", "Bravo", "Charlie", "Delta", "Echo")
    assert template_module.MAX_TRANSACTION_TABLES == 4
    assert template_module.MAX_TABLES == 5
    assert len(LayoutTemplate(tables=[table(n) for n in names[:4]]).tables) == 4
    with pytest.raises(ValidationError, match="transaction tables"):
        LayoutTemplate(tables=[table(n) for n in names])
    holdings = TableTemplate(
        kind="holdings",
        columns=[
            TableColumn(header="Units", role="quantity"),
            TableColumn(header="Name", role="security"),
        ],
    )
    assert len(LayoutTemplate(tables=[*(table(n) for n in names[:4]), holdings]).tables) == 5
    with pytest.raises(ValidationError, match="one holdings table"):
        LayoutTemplate(tables=[holdings, holdings])
    # Taught again, the new transaction tables replace the earlier ones together.
    earlier = LayoutTemplate(tables=[table(n) for n in names[:4]])
    merged, _ = merge_templates(earlier, LayoutTemplate(tables=[table("Echo"), holdings]))
    assert [t.columns[1].header for t in merged.tables] == ["Echo", "Name"]


def test_parser_version_is_5() -> None:
    # The plan wrote "2 to 3"; the reader stood at 4 when import 1d began.
    assert template_module.PARSER_VERSION == "6"
    assert template_module.TEMPLATE_REF.version == "6"


# ---------------------------------------------------------------------------
# Review of import 1d: tables under the same headers, end texts, card sections
# ---------------------------------------------------------------------------

_SAME: Line = (200.0, [(60.0, "Posted"), (160.0, "Detail"), (400.0, "Change")])


def _same(y: float) -> Line:
    return (y, list(_SAME[1]))


def _in_and_out(first_end: str | None, second_end: str | None) -> LayoutTemplate:
    """Money in, then money out, under the same three headers."""
    return LayoutTemplate(
        tables=[
            _change("money_in").tables[0].model_copy(update={"end": first_end}),
            _change("money_out").tables[0].model_copy(update={"end": second_end}),
        ]
    )


def _signed(taught: Any) -> list[tuple[Decimal, str | None, list[str]]]:
    return [(t.amount, t.payee, t.low_confidence) for t in taught.statements[0].transactions]


def _split_selections(view: DocumentView) -> list[tuple[float, float]]:
    """Top and bottom of each table of the two-table sheet (header to above its total)."""
    words = [w for line in view.lines for w in template_module.line_words(line)]
    tops = [w.y0 - 2 for w in words if w.text == "Booked"]
    bottoms = [next(w for w in words if w.text == first).y0 - 3 for first in ("Received", "Sent")]
    return list(zip(tops, bottoms, strict=True))


_SPLIT_SPANS = [(40.0, 140.0, "date"), (140.0, 390.0, "payee"), (390.0, 470.0, "amount")]


def test_the_end_text_is_read_from_a_total_line_with_a_figure() -> None:
    view = _pdf_view(synth.two_tables_statement())
    ends = [
        table_from_selection(view, 1, (40.0, top, 580.0, bottom), "transactions", _SPLIT_SPANS).end
        for top, bottom in _split_selections(view)
    ]
    # "Received in all 1,962.75": the words before the figure end the table.
    assert ends == ["Received in all", "Sent in all"]


def test_the_end_text_is_not_taken_from_a_row_or_from_inside_the_table() -> None:
    view = _pdf_view(synth.two_tables_statement())
    (top, bottom), _ = _split_selections(view)
    # A box that stops one row short: under it stands a row, which starts
    # with its date. A row is never an end text.
    short = table_from_selection(
        view, 1, (40.0, top, 580.0, bottom - 16.0), "transactions", _SPLIT_SPANS
    )
    assert short.end is None

    # The line under the box starts like a line inside it: ending there
    # would cut the table on this very page.
    hand = _hand_view(
        [
            THREE,
            _three(220.0, "2026-03-02", "Shop", "12.00"),
            (235.0, [(60.0, "Sum"), (90.0, "so"), (110.0, "far"), (400.0, "12.00")]),
            _three(250.0, "2026-03-05", "Stall", "4.00"),
            (265.0, [(60.0, "Sum"), (90.0, "so"), (110.0, "far"), (400.0, "16.00")]),
        ]
    )
    spans = [(50.0, 150.0, "date"), (150.0, 390.0, "payee"), (390.0, 480.0, "amount")]
    inside = table_from_selection(hand, 1, (50.0, 198.0, 600.0, 262.0), "transactions", spans)
    assert inside.end is None

    # A table that continues over pages: under its first page stands the
    # page's foot, with its sheet number. It is not the table's end.
    paged = _hand_view(
        [
            THREE,
            _three(220.0, "2026-03-02", "Shop", "12.00"),
            (700.0, [(60.0, "Leaf"), (90.0, "1"), (100.0, "of"), (118.0, "2")]),
        ],
        [_three(60.0, "2026-03-05", "Stall", "4.00")],
    )
    box = (50.0, 198.0, 600.0, 232.0)
    assert table_from_selection(paged, 1, box, "transactions", spans).end == "Leaf"
    continued = table_from_selection(paged, 1, box, "transactions", spans, continues=True)
    assert continued.end is None
    assert continued.continues is True
    assert [t.payee for t in _read_rows(paged, continued)] == ["Shop", "Stall"]


def _read_rows(view: DocumentView, table: TableTemplate) -> list[Any]:
    taught = read_template(view, LayoutTemplate(tables=[table]), threshold=0.85)
    return list(taught.statements[0].transactions)


def test_an_end_text_ends_the_table_only_where_a_line_starts_with_it() -> None:
    view = _hand_view(
        [
            THREE,
            # The payee holds the end text: it is a row, not the end.
            _three(220.0, "2026-03-02", "Sum Street Bakery", "-12.00"),
            _three(235.0, "2026-03-05", "Wage", "50.00"),
            (250.0, [(60.0, "Sum"), (400.0, "38.00")]),
            # Under the end: no row.
            _three(265.0, "2026-03-09", "Notice", "-1.00"),
        ]
    )
    table = _change("as_printed").tables[0].model_copy(update={"end": "Sum"})
    assert [(t.payee, t.amount) for t in _read_rows(view, table)] == [
        ("Sum Street Bakery", Decimal("-12.00")),
        ("Wage", Decimal("50.00")),
    ]


def test_a_second_table_on_the_next_page_is_not_read_as_the_first() -> None:
    """Money in on page 1 with its total, money out from the top of page 2."""
    view = _hand_view(
        [
            THREE,
            _three(220.0, "2026-03-02", "Wage", "50.00"),
            (235.0, [(60.0, "Sum"), (90.0, "in"), (400.0, "50.00")]),
        ],
        [_same(200.0), _three(220.0, "2026-03-05", "Shop", "12.00")],
    )
    # The first table's end text was found: the header of page 2 is the next table's.
    taught = read_template(view, _in_and_out("Sum in", None), threshold=0.85)
    assert _signed(taught) == [
        (Decimal("50.00"), "Wage", ["reconciliation"]),
        (Decimal("-12.00"), "Shop", ["reconciliation"]),
    ]
    assert taught.row_tables == [0, 1]
    assert "table_order_guessed" not in taught.warnings

    # Without an end text nothing says whether page 2 goes on with the first
    # table or starts the second: the rows are read, and their amounts wait.
    guessed = read_template(view, _in_and_out(None, None), threshold=0.85)
    assert [low for _, _, low in _signed(guessed)] == [["amount", "reconciliation"]] * 2
    assert guessed.warnings.count("table_order_guessed") == 1
    assert [read.guessed for read in guessed.tables] == [True, True]


def test_tables_printed_on_every_page_keep_their_directions() -> None:
    pages = [
        [
            THREE,
            _three(220.0, f"2026-0{month}-02", f"Wage{month}", "50.00"),
            (235.0, [(60.0, "Sum"), (90.0, "in"), (400.0, "50.00")]),
            _same(260.0),
            _three(280.0, f"2026-0{month}-05", f"Shop{month}", "12.00"),
            (295.0, [(60.0, "Sum"), (90.0, "out"), (400.0, "12.00")]),
        ]
        for month in (3, 4)
    ]
    view = _hand_view(*pages)
    taught = read_template(view, _in_and_out("Sum in", "Sum out"), threshold=0.85)
    assert _signed(taught) == [
        (Decimal("50.00"), "Wage3", ["reconciliation"]),
        (Decimal("-12.00"), "Shop3", ["reconciliation"]),
        (Decimal("50.00"), "Wage4", ["reconciliation"]),
        (Decimal("-12.00"), "Shop4", ["reconciliation"]),
    ]
    assert taught.warnings == []

    # The second table has no end text: it is still open at the top of page
    # 2, whose first header may be either table's. Every amount waits.
    open_ended = read_template(view, _in_and_out("Sum in", None), threshold=0.85)
    assert all("amount" in low for _, _, low in _signed(open_ended))
    assert "table_order_guessed" in open_ended.warnings


def test_a_section_the_statement_left_out_makes_the_order_a_guess() -> None:
    """One header, two tables it could start: which section is printed is not said."""
    anchors = [
        FieldAnchor(field="opening_balance", direction="right", label="Brought in"),
        FieldAnchor(field="closing_balance", direction="right", label="Taken out"),
    ]

    def page(opening: str, closing: str) -> DocumentView:
        return _hand_view(
            [
                (100.0, [(60.0, "Brought"), (110.0, "in"), (140.0, opening)]),
                (120.0, [(60.0, "Taken"), (100.0, "out"), (140.0, closing)]),
                THREE,
                _three(220.0, "2026-03-05", "Shop", "12.00"),
                (235.0, [(60.0, "Sum"), (90.0, "out"), (400.0, "12.00")]),
            ]
        )

    tables = _in_and_out("Sum in", "Sum out").tables
    unchecked = read_template(
        page("100.00", "88.00"), LayoutTemplate(tables=tables), threshold=0.85
    )
    assert _signed(unchecked) == [(Decimal("12.00"), "Shop", ["amount", "reconciliation"])]
    assert "table_order_guessed" in unchecked.warnings

    # Balances that the guess meets bear it out: nothing waits.
    template = LayoutTemplate(fields=anchors, tables=tables)
    met = read_template(page("100.00", "112.00"), template, threshold=0.85)
    assert met.statements[0].reconciliation is not None
    assert met.statements[0].reconciliation.status == "ok"
    assert _signed(met) == [(Decimal("12.00"), "Shop", [])]
    assert "table_order_guessed" not in met.warnings
    # Balances it does not meet: held as a mismatch, the amounts in doubt.
    unmet = read_template(page("100.00", "88.00"), template, threshold=0.85)
    assert unmet.statements[0].reconciliation is not None
    assert unmet.statements[0].reconciliation.status == "mismatch"
    assert all("amount" in low for _, _, low in _signed(unmet))


def test_tables_that_read_a_row_alike_are_never_a_guess() -> None:
    """The same headers and the same direction: whichever table it is, the row is the same."""
    view = _hand_view(
        [THREE, _three(220.0, "2026-03-02", "Wage", "50.00")],
        [_same(200.0), _three(220.0, "2026-03-05", "Gift", "12.00")],
    )
    alike = LayoutTemplate(tables=[_change("money_in").tables[0]] * 2)
    taught = read_template(view, alike, threshold=0.85)
    assert _signed(taught) == [
        (Decimal("50.00"), "Wage", ["reconciliation"]),
        (Decimal("12.00"), "Gift", ["reconciliation"]),
    ]
    assert taught.warnings == []


def test_a_card_with_a_table_of_payments_reconciles() -> None:
    """Payments and purchases in two tables, the balances amounts owed."""
    view = _hand_view(
        [
            (100.0, [(60.0, "Owed"), (90.0, "before"), (140.0, "100.00")]),
            (120.0, [(60.0, "Owed"), (90.0, "now"), (140.0, "120.00")]),
            THREE,
            _three(220.0, "2026-03-02", "Payment", "30.00"),
            (235.0, [(60.0, "Sum"), (90.0, "paid"), (400.0, "30.00")]),
            _same(260.0),
            _three(280.0, "2026-03-05", "Shop", "50.00"),
            (295.0, [(60.0, "Sum"), (90.0, "charged"), (400.0, "50.00")]),
        ]
    )
    anchors = [
        FieldAnchor(field="opening_balance", direction="right", label="Owed before"),
        FieldAnchor(field="closing_balance", direction="right", label="Owed now"),
    ]

    def read(payments: str, purchases: str) -> Any:
        tables = [
            _change(payments).tables[0].model_copy(update={"end": "Sum paid"}),
            _change(purchases).tables[0].model_copy(update={"end": "Sum charged"}),
        ]
        template = LayoutTemplate(fields=anchors, tables=tables)
        return read_template(view, template, threshold=0.85).statements[0]

    statement = read("money_in", "card")
    assert [(t.amount, t.payee) for t in statement.transactions] == [
        (Decimal("30.00"), "Payment"),
        (Decimal("-50.00"), "Shop"),
    ]
    # One card table makes the balances amounts owed: -100 + 30 - 50 = -120.
    assert statement.opening_balance is not None
    assert statement.ledger_balance is not None
    assert statement.opening_balance.amount == Decimal("-100.00")
    assert statement.ledger_balance.amount == Decimal("-120.00")
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"

    # Taught wrongly (the payments as charges): it does not add up and waits.
    wrong = read("card", "card")
    assert wrong.reconciliation is not None
    assert wrong.reconciliation.status == "mismatch"
    # No card table: the balances stay as printed, as before.
    plain = read("money_in", "money_out")
    assert plain.opening_balance is not None
    assert plain.opening_balance.amount == Decimal("100.00")


def test_a_row_a_few_days_before_the_period_keeps_its_date_score() -> None:
    """Ten days of grace (``confidence.PERIOD_GRACE_DAYS``); further out the date is doubted."""
    rows = [
        _row(220.0, "2026-02-10", "Coal Merchant", "-2.00", "98.00"),
        _row(235.0, "2026-02-24", "Corner Shop", "-8.00", "90.00"),
        _row(250.0, "2026-03-05", "Wage Payment", "50.00", "140.00"),
        _row(220.0, "2026-04-10", "Rail Pass", "-4.50", "135.50"),
        _row(235.0, "2026-04-11", "Book Stall", "-5.50", "130.00"),
    ]
    taught = read_template(DocumentView.from_result(_result(rows)), _template(), threshold=0.85)
    statement = taught.statements[0]
    assert (statement.period_start, statement.period_end) == (date(2026, 3, 1), date(2026, 3, 31))
    assert [(t.posted, "posted_date" in t.low_confidence) for t in statement.transactions] == [
        (date(2026, 2, 10), True),
        (date(2026, 2, 24), False),
        (date(2026, 3, 5), False),
        (date(2026, 4, 10), False),
        (date(2026, 4, 11), True),
    ]

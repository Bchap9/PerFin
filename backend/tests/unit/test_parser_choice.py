# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which parser reads a page that looks like both a statement and a receipt.

A page with a transaction table, or one the classifier calls a statement,
is never read by the receipt parser; a document classified as a receipt
still is, table or not (``registry._without_receipt``).

And what a page with one holding on it is worth: a table that prints no
period and no figure about the account as a whole is read, and is not a
checked statement, whichever parser was asked first (``brokerage``, "a
check that vouches for the account").
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.base import DocumentView, ParseOutcome
from app.ingestion.parsers.registry import PARSERS, _without_receipt, parse_extraction, reparse
from app.ingestion.parsers.table_statement import TableStatementParser
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult
from tests.fixtures import synth_parser_choice as pc
from tests.fixtures import synth_receipts as sr

OPTIONS = ExtractOptions(previews=False, ocr=False)


def _extract(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return result


def test_a_page_with_a_transaction_table_is_not_a_receipt() -> None:
    parse = parse_extraction(_extract(pc.activity_page_with_totals())).parse
    assert parse is not None
    assert parse.parser is not None
    assert parse.parser.name == "statement_generic"
    assert parse.receipt is None
    scores = {s.name: s.score for s in parse.scores}
    assert scores["receipt_basic"] == 0.0


def test_the_activity_rows_are_read_and_the_total_lines_are_not() -> None:
    result = parse_extraction(_extract(pc.activity_page_with_totals()))
    (statement,) = result.statements
    assert len(statement.transactions) == len(pc.ACTIVITY_ROWS) == 6
    read = " ".join(f"{t.payee} {t.memo}".lower() for t in statement.transactions)
    for word in ("subtotal", "tax", "total", "thank you"):
        assert word not in read


def test_a_statement_classification_vetoes_the_receipt_parser() -> None:
    result = _extract(sr.receipt_pdf(sr.receipt_spec(4242)))
    parse = parse_extraction(result, kind="statement").parse
    assert parse is not None
    assert parse.parser is None
    assert "no_parser" in parse.warnings
    assert parse.receipt is None


def test_a_classified_receipt_with_an_item_table_is_still_a_receipt() -> None:
    parse = parse_extraction(_extract(pc.receipt_with_item_table()), kind="receipt").parse
    assert parse is not None
    assert parse.parser is not None
    assert parse.parser.name == "receipt_basic"
    assert parse.receipt is not None
    assert parse.receipt.total == pc.RECEIPT_TOTAL


def test_an_unclassified_receipt_with_a_dated_table_is_read_as_a_statement() -> None:
    """A decision, not an accident: with no classifier kind the table wins.

    The receipt parser scores this page higher than the statement parser
    does, and a page of activity that ends in "Total" lines scores nearly
    the same (``activity_page_with_totals``), so the scores cannot tell the
    two apart. Read as a statement, the items are rows that wait for a
    person (none is confident, the tender line is no row); read as a
    receipt, a real statement's rows would vanish behind one total. The
    classifier calls this page a receipt, which keeps the receipt parser
    (the test above).
    """
    result = parse_extraction(_extract(pc.receipt_with_item_table()))
    parse = result.parse
    assert parse is not None
    assert parse.parser is not None
    assert parse.parser.name == "statement_generic"
    assert parse.receipt is None
    (statement,) = result.statements
    assert [t.payee for t in statement.transactions] == [text for _, text, _ in pc.RECEIPT_ITEMS]
    assert all(t.low_confidence for t in statement.transactions)
    assert pc.RECEIPT_TOTAL not in {abs(t.amount) for t in statement.transactions}


def test_a_stored_receipt_is_still_a_receipt_when_read_again() -> None:
    first = parse_extraction(_extract(pc.receipt_with_item_table()), kind="receipt")
    assert first.parse is not None
    assert first.parse.receipt is not None
    again = reparse(first).parse
    assert again is not None
    assert again.parser is not None
    assert again.parser.name == "receipt_basic"
    assert again.receipt is not None
    assert again.receipt.total == pc.RECEIPT_TOTAL
    assert reparse(first).statements == []


def test_reading_a_statement_again_does_not_make_it_a_receipt_hint() -> None:
    first = parse_extraction(_extract(pc.activity_page_with_totals()))
    again = reparse(first).parse
    assert again is not None
    assert again.parser is not None
    assert again.parser.name == "statement_generic"
    assert again.receipt is None
    assert {s.name: s.score for s in again.scores}["receipt_basic"] == 0.0


def test_reparse_hands_on_the_period_and_the_day_of_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = parse_extraction(_extract(pc.activity_page_with_totals()))
    seen: list[DocumentView] = []

    def capture(self: TableStatementParser, view: DocumentView, **_: Any) -> ParseOutcome:
        seen.append(view)
        return ParseOutcome()

    monkeypatch.setattr(TableStatementParser, "parse_with", capture)
    start, end, received = date(2026, 4, 1), date(2026, 4, 30), date(2026, 5, 4)
    reparse(first, period=(start, end, 0.9), not_after=received)
    assert seen
    assert seen[0].hints.period == (start, end)
    assert seen[0].hints.period_confidence == 0.9
    assert seen[0].hints.not_after == received


def test_receipts_without_a_table_are_unchanged() -> None:
    parse = parse_extraction(_extract(sr.receipt_pdf(sr.receipt_spec(4242)))).parse
    assert parse is not None
    assert parse.parser is not None
    assert parse.parser.name == "receipt_basic"
    assert parse.receipt is not None


def test_only_the_receipt_parsers_lose_their_score() -> None:
    scores = {p.name: 0.5 for p in PARSERS}
    receipts = {p.name for p in PARSERS if p.kind == "receipt"}
    assert receipts
    for kind in ("statement", None, "unknown"):
        vetoed = _without_receipt(scores, kind)
        assert {name for name, score in vetoed.items() if score == 0.0} == receipts
    assert _without_receipt(scores, "receipt") == scores
    no_table = {**scores, "statement_generic": 0.0}
    assert _without_receipt(no_table, None) == no_table
    assert _without_receipt(no_table, "payslip") == no_table


@pytest.mark.parametrize("kind", ["tax_form", None])
@pytest.mark.parametrize("total", [False, True])
def test_a_holding_on_a_page_with_no_period_and_no_account_figure_is_not_checked(
    kind: str | None, total: bool
) -> None:
    """A tax form explainer that shows one fund: read, and held for a person.

    The classifier calls it a tax form, the tax form parser reads no box and
    the brokerage parser is the fallback; with no classifier kind the
    brokerage parser is chosen outright. Either way the row multiplying out
    (and a ``Total`` line repeating it) says nothing about an account.
    """
    result = parse_extraction(
        _extract(pc.tax_form_explainer_with_a_holding(total=total)), kind=kind
    )
    parse = result.parse
    assert parse is not None
    assert parse.parser is not None
    assert parse.parser.name == "brokerage_statement_generic"
    assert ("parser_fallback:tax_forms" in parse.warnings) is (kind == "tax_form")
    assert parse.tax_forms == []
    (statement,) = result.investments
    assert (statement.period_start, statement.period_end) == (None, None)
    (position,) = statement.positions
    assert position.market_value == pc.HOLDING_VALUE
    check = statement.reconciliation
    assert check is not None
    assert check.failed == []
    assert check.status == "unavailable"
    assert "reconciliation" in position.low_confidence
    assert statement.confidence["reconciliation"] == conf.UNCHECKED


@pytest.mark.parametrize(
    ("totals", "checks"),
    [
        (True, ["positions", "holdings_total", "cash", "total_value"]),
        # The period says whose statement the table is; read as before.
        (False, ["positions"]),
    ],
)
def test_a_one_holding_statement_with_a_period_is_checked_as_before(
    totals: bool, checks: list[str]
) -> None:
    result = parse_extraction(_extract(pc.one_holding_statement(totals=totals)))
    (statement,) = result.investments
    assert statement.account.last4 == pc.HOLDING_LAST4
    assert (statement.period_start, statement.period_end) == (date(2026, 3, 1), date(2026, 3, 31))
    (position,) = statement.positions
    assert position.low_confidence == []
    check = statement.reconciliation
    assert check is not None
    assert (check.status, check.checks) == ("ok", checks)
    assert statement.confidence["reconciliation"] == 1.0

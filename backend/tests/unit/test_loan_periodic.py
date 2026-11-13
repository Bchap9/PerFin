# SPDX-License-Identifier: AGPL-3.0-or-later
"""Periodic mortgage statements (``tests/fixtures/synth_loans.py``, :class:`PeriodicStatement`).

A periodic statement prints the split of the next payment, of last month's
payment and of the year so far. Only last month's is the payment made: the
tests pin that no year-to-date figure, no part of the next amount due and no
due date is ever read as it, that the two older layouts still take the line
reader, and that a statement whose columns cannot be told is left unread.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers import confidence
from app.ingestion.parsers.base import Cell, DocumentView, ViewLine
from app.ingestion.parsers.loan_periodic import is_periodic, read_periodic
from app.ingestion.parsers.loan_statement import LoanStatementParser, reconcile_loan
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.text import FULL_STYLES
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, LoanStatement
from tests.fixtures import synth_loans as synth

OPTIONS = ExtractOptions(previews=False, ocr=False)
VARIANTS: tuple[synth.PeriodicVariant, ...] = (
    "blocks",
    "labels_over_values",
    "payment_date_is_due_date",
    "no_payment",
)
#: Figures the statement prints that are not the payment made.
YEAR_TO_DATE = {Decimal("2444.91"), Decimal("4588.59"), Decimal("1809.00"), Decimal("8867.50")}
NEXT_PAYMENT = {Decimal("412.08"), Decimal("760.17")}


def _view(data: bytes) -> DocumentView:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return DocumentView.from_result(result)


def _parse(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return parse_extraction(result, threshold=confidence.DEFAULT_THRESHOLD)


def _loan(result: ExtractResult) -> LoanStatement:
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == "loan_statement_generic"
    assert result.parse.loan is not None
    assert result.statements == []
    return result.parse.loan


def _payment(loan: LoanStatement) -> set[Decimal | None]:
    return {loan.principal, loan.interest, loan.escrow, loan.fees, loan.total}


def _lines(rows: Sequence[Sequence[tuple[float, str]]]) -> DocumentView:
    """A one-page text view: each row a list of ``(x0, text)`` cells."""
    lines = []
    for at, row in enumerate(rows):
        y = 40.0 + 16 * at
        cells = tuple(Cell(text, x, y, x + 5.5 * len(text), y + 10) for x, text in row)
        lines.append(ViewLine(1, at, cells, " ".join(c.text for c in cells), 10.0))
    return DocumentView(lines=tuple(lines), source_format="pdf", truncated=False, page_count=1)


def test_existing_layouts_take_the_line_reader() -> None:
    stmt = synth.loan_series(1)[0]
    for layout in ("classic", "compact"):
        assert not is_periodic(_view(synth.loan_statement_pdf(stmt, layout=layout))), layout
    for variant in VARIANTS:
        assert is_periodic(_view(synth.periodic_statement_pdf(variant=variant))), variant


def test_last_payment_split_is_read_never_the_year_to_date() -> None:
    loan = _loan(_parse(synth.periodic_statement_pdf()))
    assert loan.principal == Decimal("410.37")
    assert loan.interest == Decimal("761.88")
    assert loan.escrow == Decimal("301.50")
    assert loan.fees == Decimal("0.00")
    assert loan.total == Decimal("1473.75")
    assert not _payment(loan) & (YEAR_TO_DATE | NEXT_PAYMENT)
    assert loan.previous_balance is None


def test_balance_paid_date_and_due() -> None:
    loan = _loan(_parse(synth.periodic_statement_pdf()))
    assert loan.lender == synth.LENDER
    assert loan.balance == Decimal("182440.17")
    assert loan.paid_on == date(2026, 3, 2)
    assert loan.statement_date == date(2026, 3, 20)
    assert loan.next_due_date == date(2026, 4, 1)
    assert loan.next_amount_due == Decimal("1498.75")
    assert loan.rate == Decimal("0.05")
    assert loan.last4 == "7755"
    assert loan.currency == "USD"


def test_blocks_reconcile() -> None:
    loan = _loan(_parse(synth.periodic_statement_pdf()))
    assert loan.reconciliation is not None
    assert loan.reconciliation.status == "ok"
    assert loan.reconciliation.checks == ["parts", "amount_due"]
    assert loan.low_confidence == []
    assert loan.confidence["principal"] == 1.0
    assert loan.confidence["balance"] == confidence.GENERIC


@pytest.mark.parametrize(("tamper", "check"), [("paid", "parts"), ("due", "amount_due")])
def test_tampered_blocks_do_not_reconcile(tamper: synth.PeriodicTamper, check: str) -> None:
    loan = _loan(_parse(synth.periodic_statement_pdf(tamper=tamper)))
    assert loan.reconciliation is not None
    assert loan.reconciliation.status == "mismatch"
    assert loan.reconciliation.failed == [check]
    assert {"principal", "interest", "balance"} <= set(loan.low_confidence)


def test_labels_over_values_variant() -> None:
    loan = _loan(_parse(synth.periodic_statement_pdf(variant="labels_over_values")))
    assert loan.lender == synth.LENDER
    assert (loan.principal, loan.interest, loan.escrow, loan.fees, loan.total) == (
        Decimal("410.37"),
        Decimal("761.88"),
        Decimal("301.50"),
        Decimal("0.00"),
        Decimal("1473.75"),
    )
    assert not _payment(loan) & (YEAR_TO_DATE | NEXT_PAYMENT)
    assert loan.balance == Decimal("182440.17")
    assert loan.paid_on == date(2026, 3, 2)
    assert loan.statement_date == date(2026, 3, 20)
    assert loan.next_due_date == date(2026, 4, 1)
    assert loan.next_amount_due == Decimal("1498.75")
    assert loan.rate == Decimal("0.05")
    assert loan.last4 == "7755"
    assert loan.reconciliation is not None
    assert (loan.reconciliation.status, loan.reconciliation.checks) == (
        "ok",
        ["parts", "amount_due"],
    )
    assert loan.low_confidence == []


def test_payment_date_label_is_the_due_date() -> None:
    loan = _loan(_parse(synth.periodic_statement_pdf(variant="payment_date_is_due_date")))
    assert loan.paid_on == date(2026, 3, 2)
    assert loan.next_due_date == date(2026, 4, 1)
    assert loan.low_confidence == []


def test_no_payment_in_the_period() -> None:
    result = _parse(synth.periodic_statement_pdf(variant="no_payment"))
    loan = _loan(result)
    assert result.parse is not None
    assert "loan_no_payment_in_period" in result.parse.warnings
    assert loan.paid_on is None
    assert (loan.principal, loan.interest) == (Decimal("0.00"), Decimal("0.00"))
    assert not _payment(loan) & (YEAR_TO_DATE | NEXT_PAYMENT)
    assert "paid_on" not in loan.low_confidence
    assert "paid_on" not in loan.confidence
    assert loan.reconciliation is not None
    assert loan.reconciliation.status == "ok"
    assert "amount_due" in loan.reconciliation.checks
    assert loan.balance == Decimal("182440.17")


def test_coupon_figures_are_not_read() -> None:
    stmt = replace(synth.PeriodicStatement(), coupon_amount=Decimal("9876.54"))
    loan = _loan(_parse(synth.periodic_statement_pdf(stmt)))
    assert loan.next_amount_due == Decimal("1498.75")


def test_figures_after_a_stub_marker_are_ignored() -> None:
    rows = [
        [(72.0, "Explanation of Amount Due")],
        [(72.0, "Principal"), (220.0, "$412.08")],
        [(72.0, "Interest"), (220.0, "$760.17")],
        [(72.0, "Please detach and return this portion")],
        [(72.0, "Total Amount Due"), (220.0, "$9,876.54")],
        [(72.0, "Outstanding Principal"), (220.0, "$1.00")],
    ]
    read = read_periodic(_lines(rows), FULL_STYLES)
    assert read.due == {"principal": Decimal("412.08"), "interest": Decimal("760.17")}
    assert read.next_due_amount is None
    assert read.money == {}
    # The same lines with no marker are read: the cut is what keeps them out.
    kept = read_periodic(_lines([*rows[:3], *rows[4:]]), FULL_STYLES)
    assert kept.next_due_amount == Decimal("9876.54")


def test_payment_row_that_is_not_the_total_gives_no_date() -> None:
    row = synth.ActivityRow(date(2026, 3, 2), "Payment received, thank you", Decimal("1000.00"))
    stmt = replace(synth.PeriodicStatement(), activity=(row,))
    result = _parse(synth.periodic_statement_pdf(stmt))
    loan = _loan(result)
    assert result.parse is not None
    assert "loan_payment_row_mismatch" in result.parse.warnings
    assert loan.paid_on is None
    assert "paid_on" in loan.low_confidence


def test_payment_made_in_two_rows_is_dated_by_the_later() -> None:
    rows = (
        synth.ActivityRow(date(2026, 2, 27), "Payment received, thank you", Decimal("700.00")),
        synth.ActivityRow(date(2026, 3, 5), "Payment received, thank you", Decimal("773.75")),
        synth.ActivityRow(date(2026, 3, 9), "Payment reversal", Decimal("773.75")),
        synth.ActivityRow(date(2026, 3, 17), "Late charge", Decimal("25.00"), "charge"),
        synth.ActivityRow(date(2026, 3, 18), "Late payment fee", Decimal("1473.75"), "charge"),
    )
    stmt = replace(synth.PeriodicStatement(), activity=rows)
    loan = _loan(_parse(synth.periodic_statement_pdf(stmt)))
    assert loan.paid_on == date(2026, 3, 5)


def test_a_charge_is_never_the_payment_row() -> None:
    # The amount stands in the Charges column: no payment was received.
    row = synth.ActivityRow(date(2026, 3, 2), "Payment adjustment", Decimal("1473.75"), "charge")
    stmt = replace(synth.PeriodicStatement(), activity=(row,))
    loan = _loan(_parse(synth.periodic_statement_pdf(stmt)))
    assert loan.paid_on is None
    assert "paid_on" in loan.low_confidence


def test_columns_that_cannot_be_told_are_not_read() -> None:
    rows = [
        [(72.0, "Past Payments Breakdown")],
        [(200.0, "Billed"), (300.0, "Received"), (400.0, "Paid Year to Date")],
        [(72.0, "Principal"), (200.0, "$410.37"), (300.0, "$410.37"), (400.0, "$2,444.91")],
        [(72.0, "Interest"), (200.0, "$761.88"), (300.0, "$761.88"), (400.0, "$4,588.59")],
    ]
    view = _lines(rows)
    assert is_periodic(view)
    read = read_periodic(view, FULL_STYLES)
    assert read.money == {}
    assert read.warnings == ["loan_columns_unknown"]
    outcome = LoanStatementParser().parse(view, threshold=confidence.DEFAULT_THRESHOLD)
    assert outcome.loan is None
    assert outcome.warnings == ["loan_columns_unknown", "no_loan_figures"]


def test_the_one_column_beside_the_year_to_date_is_the_payment() -> None:
    rows = [
        [(200.0, "Paid 03/2026"), (400.0, "Paid Year to Date")],
        [(72.0, "Principal"), (200.0, "$410.37"), (400.0, "$2,444.91")],
        [(72.0, "Interest"), (200.0, "$761.88"), (400.0, "$4,588.59")],
        [(72.0, "Escrow"), (400.0, "$1,809.00")],
    ]
    read = read_periodic(_lines(rows), FULL_STYLES)
    # Escrow prints a year-to-date figure only: it is no payment.
    assert read.money == {"principal": Decimal("410.37"), "interest": Decimal("761.88")}


def test_amounts_under_a_heading_with_no_columns_named_are_not_read() -> None:
    # Nothing says whether the one column is last month's or the year's.
    rows = [
        [(72.0, "Past Payments Breakdown")],
        [(72.0, "Principal"), (200.0, "$410.37")],
        [(72.0, "Interest"), (200.0, "$761.88")],
    ]
    read = read_periodic(_lines(rows), FULL_STYLES)
    assert read.money == {}
    assert read.warnings == ["loan_columns_unknown"]


def test_one_period_column_under_the_paid_heading() -> None:
    rows = [
        [(72.0, "Past Payments Breakdown")],
        [(200.0, "Paid Last Month")],
        [(72.0, "Principal"), (200.0, "$410.37")],
        [(72.0, "Interest"), (200.0, "$761.88")],
    ]
    read = read_periodic(_lines(rows), FULL_STYLES)
    assert read.money == {"principal": Decimal("410.37"), "interest": Decimal("761.88")}
    assert read.warnings == []
    # The same header with no paid heading over it names nothing.
    bare = read_periodic(_lines(rows[1:]), FULL_STYLES)
    assert bare.money == {}


def test_balance_printed_over_its_label_and_blocks_side_by_side() -> None:
    rows = [
        [(72.0, "$182,440.17"), (320.0, "Explanation of Amount Due")],
        [(72.0, "Current Principal Balance"), (320.0, "Principal"), (480.0, "$412.08")],
        [(72.0, "Interest Rate"), (200.0, "5.25%"), (320.0, "Interest"), (480.0, "$760.17")],
        [
            (72.0, "Statement Ending"),
            (200.0, "03/20/2026"),
            (320.0, "Amount Due"),
            (480.0, "$1,172.25"),
        ],
        [(72.0, "Principal"), (200.0, "$77.00")],
    ]
    read = read_periodic(_lines(rows), FULL_STYLES)
    assert read.money == {"balance": Decimal("182440.17")}
    assert read.due == {
        "principal": Decimal("412.08"),
        "interest": Decimal("760.17"),
        "total": Decimal("1172.25"),
    }
    assert read.rate == Decimal("0.0525")
    assert read.dates == {"statement_date": date(2026, 3, 20)}
    assert read.next_due_amount == Decimal("1172.25")


def test_a_last_payment_date_is_dropped_when_nothing_was_paid() -> None:
    rows = [
        [(72.0, "Date Last Payment Received"), (260.0, "01/04/2026")],
        [(200.0, "Paid Last Month"), (400.0, "Paid Year to Date")],
        [(72.0, "Principal"), (200.0, "$0.00"), (400.0, "$2,444.91")],
        [(72.0, "Interest"), (200.0, "$0.00"), (400.0, "$4,588.59")],
        [(72.0, "Total"), (200.0, "$0.00"), (400.0, "$7,033.50")],
    ]
    read = read_periodic(_lines(rows), FULL_STYLES)
    assert "paid_on" not in read.dates
    assert read.warnings == ["loan_no_payment_in_period"]


def test_a_full_loan_number_gives_only_its_last_four() -> None:
    loan = _loan(_parse(synth.periodic_statement_pdf()))
    assert loan.last4 == synth.PERIODIC_LAST4
    assert synth.PERIODIC_LOAN_NUMBER not in loan.model_dump_json()


def test_amount_due_check() -> None:
    paid = {"principal": Decimal("10.00"), "interest": Decimal("5.00"), "total": Decimal("15.00")}
    due = {
        "principal": Decimal("11.00"),
        "interest": Decimal("4.00"),
        "escrow": Decimal("3.00"),
        "regular": Decimal("18.00"),
        "fees": Decimal("2.00"),
        "total": Decimal("20.00"),
    }
    checked = reconcile_loan(paid, due)
    assert (checked.status, checked.checks, checked.failed) == ("ok", ["parts", "amount_due"], [])
    wrong = reconcile_loan(paid, {**due, "regular": Decimal("19.00")})
    assert (wrong.status, wrong.failed) == ("mismatch", ["amount_due"])
    # A total alone is the parts and the fees; without a regular payment or a total, no check.
    only_total = reconcile_loan(paid, {k: v for k, v in due.items() if k != "regular"})
    assert (only_total.status, only_total.checks) == ("ok", ["parts", "amount_due"])
    assert reconcile_loan(paid, {"principal": Decimal("1"), "interest": Decimal("1")}).checks == [
        "parts"
    ]
    assert reconcile_loan({}, due).checks == ["amount_due"]


def test_a_scanned_statement_scales_each_field_by_its_own_cells() -> None:
    """Garbled year-to-date figures and a garbled footer lower nothing that was read."""
    view = _view(synth.periodic_statement_pdf())
    year = {"$2,444.91", "$4,588.59", "$1,809.00", "$8,867.50"}

    def conf_of(cell: Cell, line: ViewLine, balance: float) -> float:
        if cell.text in year or line.norm.startswith("synthetic test document"):
            return 0.03
        return balance if cell.text == "$182,440.17" else 0.96

    def scan(balance: float) -> DocumentView:
        lines = tuple(
            replace(
                line, cells=tuple(replace(c, conf=conf_of(c, line, balance)) for c in line.cells)
            )
            for line in view.lines
        )
        return replace(view, lines=lines, source_format="image")

    loan = LoanStatementParser().parse(scan(0.96), threshold=confidence.DEFAULT_THRESHOLD).loan
    assert loan is not None
    for name in ("principal", "interest", "escrow", "fees", "total"):
        assert loan.confidence[name] == 1.0, name
    for name in ("balance", "paid_on", "statement_date", "rate", "lender", "last4"):
        assert loan.confidence[name] == pytest.approx(confidence.GENERIC * 0.96), name
    assert loan.low_confidence == []
    doubtful = LoanStatementParser().parse(scan(0.40), threshold=confidence.DEFAULT_THRESHOLD).loan
    assert doubtful is not None
    assert doubtful.low_confidence == ["balance"]

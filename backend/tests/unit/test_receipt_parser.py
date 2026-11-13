# SPDX-License-Identifier: AGPL-3.0-or-later
"""The receipt parser (INGESTION.md "Parse"): amounts, dates, totals, payment, merchant.

Text receipts are built as parser lines directly (``view_lines_from_text``,
the same path a mail body takes), so the rules are tested without OCR; the
PDF and mail cases run the real extractors in process on synthetic
documents (``tests/fixtures/synth_receipts.py``).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from email.message import EmailMessage

import pytest

from app.ingestion.extract.eml import extract_eml
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers import receipt as rp
from app.ingestion.parsers.base import DocumentView, ViewLine
from app.ingestion.parsers.registry import MIN_SCORE, PARSERS, parse_extraction
from app.ingestion.sandbox.protocol import ExtractOptions, Receipt
from tests.fixtures import synth_receipts as sr

THRESHOLD = 0.85


def _read(text: str, **kwargs: object) -> Receipt:
    receipt = rp.read_receipt(rp.view_lines_from_text(text), threshold=THRESHOLD, **kwargs)  # type: ignore[arg-type]
    assert receipt is not None
    return receipt


BASIC = """CORNER MARKET
12 Example St
Springfield
Tel 555-0100
03/14/2026 12:41
MILK 2L        3.49
BREAD          2.99
APPLES         4.10
SUBTOTAL      10.58
TAX 8%         0.85
TOTAL         11.43
VISA **** 1234   11.43
THANK YOU
"""


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("11.43", (Decimal("11.43"), None)),
        ("$11.43", (Decimal("11.43"), "USD")),
        ("£1,234.56", (Decimal("1234.56"), "GBP")),
        ("1.234,56€", (Decimal("1234.56"), "EUR")),
        ("11,43", (Decimal("11.43"), None)),
        ("EUR74,05", (Decimal("74.05"), "EUR")),
        ("-2.00", (Decimal("-2.00"), None)),
        ("(2.00)", (Decimal("-2.00"), None)),
        ("2.00-", (Decimal("-2.00"), None)),
        ("3.49A", (Decimal("3.49"), None)),
        ("“47.80", (Decimal("47.80"), None)),
        ("165.75.", (Decimal("165.75"), None)),
    ],
)
def test_money_tokens(token: str, expected: tuple[Decimal, str | None]) -> None:
    assert rp.read_money(token) == expected


@pytest.mark.parametrize("token", ["1234", "8.25%", "12:41", "14.03.2026", "1.234", "(2.00", "abc"])
def test_not_money(token: str) -> None:
    assert rp.read_money(token) is None


def test_ocr_folding_reads_confusable_digits_as_letters() -> None:
    assert rp.fold("T0TAL") == "total"
    assert rp.fold("$UB T0TAL") == "sub total"
    assert rp.fold("TOTAI DUE") == "total due"
    assert rp.fold("AMOUNTDUE") == "amount due"
    assert rp.fold("SALESTAX8%", ocr=False) == "sales tax 8"


def test_basic_receipt_fields_and_consistency() -> None:
    receipt = _read(BASIC)
    assert receipt.merchant == "CORNER MARKET"
    assert receipt.purchase_date == date(2026, 3, 14)
    assert receipt.time == "12:41"
    assert (receipt.subtotal, receipt.tax, receipt.total) == (
        Decimal("10.58"),
        Decimal("0.85"),
        Decimal("11.43"),
    )
    assert receipt.adds_up is True
    assert receipt.payment is not None
    assert (receipt.payment.method, receipt.payment.last4) == ("visa", "1234")
    assert [(i.label, i.amount) for i in receipt.lines] == [
        ("MILK 2L", Decimal("3.49")),
        ("BREAD", Decimal("2.99")),
        ("APPLES", Decimal("4.10")),
    ]
    assert receipt.confidence["total"] == 1.0
    assert receipt.confidence["lines"] == 1.0
    assert receipt.low_confidence == []


def test_the_consistent_total_wins_over_other_total_lines() -> None:
    text = """GREEN GROCER
2026-05-02
SUBTOTAL 20.00
TAX 1.60
TOTAL SAVINGS 3.00
TOTAL ITEMS 2.00
TOTAL 21.60
CASH 30.00
CHANGE 8.40
"""
    receipt = _read(text)
    assert receipt.total == Decimal("21.60")
    assert receipt.adds_up is True
    assert receipt.payment is not None
    assert receipt.payment.method == "cash"
    assert receipt.payment.last4 is None


def test_tip_and_discount_take_part_in_the_check() -> None:
    receipt = _read(
        "HARBOR CAFE\n2026-06-01\nSUBTOTAL 40.00\nDISCOUNT -5.00\nTAX 2.80\nTIP 6.00\nTOTAL 43.80\n"
    )
    assert (receipt.tip, receipt.discount, receipt.total) == (
        Decimal("6.00"),
        Decimal("5.00"),
        Decimal("43.80"),
    )
    assert receipt.adds_up is True


def test_a_savings_line_the_sum_did_not_use_is_not_added_back_as_tax() -> None:
    """Subtotal = total holds without the "you saved" line, so the tax is nil."""
    receipt = _read("PINE PANTRY\n2026-06-01\nSUBTOTAL 61.40\nYOU SAVED 20.98\nTOTAL 61.40\n")
    assert receipt.adds_up is True
    assert (receipt.subtotal, receipt.total) == (Decimal("61.40"), Decimal("61.40"))
    assert receipt.tax == Decimal("0.00")


def test_a_savings_line_the_sum_did_not_use_is_not_added_back_as_subtotal() -> None:
    """Items + tax = total holds without the savings line: the subtotal is the items."""
    receipt = _read(
        "PINE PANTRY\n2026-06-01\nOAT FLOUR 12.00\nLENTILS 8.00\n"
        "TAX 1.60\nYOU SAVED 3.00\nTOTAL 21.60\n"
    )
    assert receipt.adds_up is True
    assert receipt.subtotal == Decimal("20.00")


def test_a_discount_and_tip_the_sum_used_stay_in_a_derived_tax() -> None:
    """The reverse: subtotal + tip - discount = total used both, so the tax is nil too."""
    receipt = _read(
        "HARBOR CAFE\n2026-06-01\nSUBTOTAL 40.00\nDISCOUNT -5.00\nTIP 6.00\nTOTAL 41.00\n"
    )
    assert receipt.adds_up is True
    assert receipt.tax == Decimal("0.00")


def test_a_tax_derived_through_an_unchecked_savings_line_is_in_doubt() -> None:
    """Nothing adds up, so whether the saving is in the total is a guess: flagged."""
    receipt = _read("PINE PANTRY\n2026-06-01\nSUBTOTAL 50.00\nYOU SAVED 5.00\nTOTAL 55.00\n")
    assert receipt.adds_up is not True
    assert receipt.tax == Decimal("10.00")
    assert "tax" in receipt.low_confidence
    # Without a tip or a discount the rest of the total is the tax, as before.
    plain = _read("PINE PANTRY\n2026-06-01\nSUBTOTAL 50.00\nTOTAL 55.00\n")
    assert plain.tax == Decimal("5.00")
    assert "tax" not in plain.low_confidence


def test_a_subtotal_derived_through_an_unchecked_tip_is_in_doubt() -> None:
    receipt = _read("HARBOR CAFE\n2026-06-01\nTAX 3.20\nTIP 6.00\nTOTAL 49.20\n")
    assert receipt.subtotal == Decimal("40.00")
    assert "subtotal" in receipt.low_confidence
    plain = _read("HARBOR CAFE\n2026-06-01\nTAX 3.20\nTOTAL 43.20\n")
    assert plain.subtotal == Decimal("40.00")
    assert "subtotal" not in plain.low_confidence


def test_an_inconsistent_receipt_is_kept_with_low_confidence() -> None:
    receipt = _read("SHOP\n2026-06-01\nSUBTOTAL 10.00\nTAX 1.00\nTOTAL 12.00\n")
    assert receipt.total == Decimal("12.00")
    assert receipt.adds_up is False
    assert {"total", "subtotal", "tax"} <= set(receipt.low_confidence)


def test_a_currency_symbol_read_as_a_digit_is_recovered_by_the_check() -> None:
    receipt = _read("SHOP\n2026-04-23\nSUBTOTAL 109.19\nTax 7% 7.64\nTOTAL 8116.83\n")
    assert receipt.total == Decimal("116.83")
    assert receipt.adds_up is True


def test_a_lost_decimal_point_is_used_only_when_the_figures_agree() -> None:
    good = _read("SHOP\n07/26/26\nSUB-TOTAL 64.55\nTAX 3.23\nTOTAL 6778\n")
    assert good.total == Decimal("67.78")
    bad = rp.read_receipt(
        rp.view_lines_from_text("SHOP\n07/26/26\nSUB-TOTAL 64.55\nTAX 3.23\nTOTAL 6779\n"),
        threshold=THRESHOLD,
    )
    # Digits that do not add up are not believed as a total: no receipt is read.
    assert bad is None


def test_label_and_amount_on_separate_lines() -> None:
    receipt = _read(
        "SHOP\n2026-02-09\nNOTEBOOK 23.01\nSubtotal\n23.01\nTAX 1.15\nTOTAL DUE 24.16\n"
    )
    assert receipt.subtotal == Decimal("23.01")
    assert receipt.adds_up is True


def test_amounts_printed_above_their_labels() -> None:
    receipt = rp.read_receipt(sr.amounts_above_labels_lines(), threshold=THRESHOLD)
    assert receipt is not None
    assert receipt.subtotal == Decimal("41.20")
    assert receipt.tax == Decimal("3.30")
    assert receipt.total == Decimal("44.50")
    assert receipt.adds_up is True
    assert receipt.confidence["total"] == 1.0
    assert [line.amount for line in receipt.lines] == [
        Decimal("18.40"),
        Decimal("4.80"),
        Decimal("18.00"),
    ]
    assert receipt.purchase_date == date(2026, 3, 14)


def _paired(lines: list[ViewLine], pairing: rp.Pairing) -> dict[str, list[Decimal]]:
    """The amounts each lettered line holds after ``_typed`` paired the lines."""
    return {
        entry.line.text: [money.value for money in entry.money]
        for entry in rp._typed(lines, pairing=pairing)
        if any(ch.isalpha() for ch in entry.line.text)
    }


def test_the_reading_that_adds_up_wins() -> None:
    # Every amount exactly midway between two labels: the nearest line is a
    # tie, which goes to the line below, and that reading finds no total.
    lines = sr.amounts_above_labels_lines(lift=1.0)
    nearest = _paired(lines, "nearest")
    assert nearest["SUBTOTAL"] == [Decimal("3.30")]
    assert nearest["TAX"] == [Decimal("44.50")]
    assert nearest["TOTAL"] == []
    receipt = rp.read_receipt(lines, threshold=THRESHOLD)
    assert receipt is not None
    assert (receipt.subtotal, receipt.tax, receipt.total) == (
        Decimal("41.20"),
        Decimal("3.30"),
        Decimal("44.50"),
    )
    assert receipt.adds_up is True


def test_no_pairing_adds_up_keeps_the_reading_below() -> None:
    # 5.00 above "Subtotal", 23.01 below it, and a total neither agrees with.
    text = "SHOP\n2026-02-09\nNOTEBOOK 23.01\n5.00\nSubtotal\n23.01\nTAX 1.15\nTOTAL DUE 99.99\n"
    lines = rp.view_lines_from_text(text)
    assert _paired(lines, "above")["Subtotal"] == [Decimal("5.00")]
    receipt = _read(text)
    assert receipt.subtotal == Decimal("23.01")
    assert receipt.total == Decimal("99.99")
    assert receipt.adds_up is False
    assert "total" in receipt.low_confidence


def test_nearest_takes_no_amount_farther_than_a_line_and_a_half() -> None:
    lines = [
        sr.placed_line(0, 0.0, "9.00"),
        sr.placed_line(1, 4 * sr.LINE_HEIGHT, "SUBTOTAL"),
        sr.placed_line(2, 5 * sr.LINE_HEIGHT, "TAX", "0.70"),
    ]
    assert _paired(lines, "nearest")["SUBTOTAL"] == []
    close = [sr.placed_line(0, 2.5 * sr.LINE_HEIGHT, "9.00"), *lines[1:]]
    assert _paired(close, "nearest")["SUBTOTAL"] == [Decimal("9.00")]


def test_an_amount_line_is_given_to_one_label_only() -> None:
    lines = [
        sr.placed_line(0, 0.0, "SUBTOTAL"),
        sr.placed_line(1, sr.LINE_HEIGHT, "9.00"),
        sr.placed_line(2, 2 * sr.LINE_HEIGHT, "TAX"),
    ]
    for pairing in ("below", "nearest"):
        assert _paired(lines, pairing) == {"SUBTOTAL": [Decimal("9.00")], "TAX": []}
    assert _paired(lines, "above") == {"SUBTOTAL": [], "TAX": [Decimal("9.00")]}


def test_a_line_with_words_or_on_another_page_is_no_amount_line() -> None:
    worded = [
        sr.placed_line(0, 0.0, "GIFT BAG", "9.00"),
        sr.placed_line(1, sr.LINE_HEIGHT, "SUBTOTAL"),
        sr.placed_line(2, 2 * sr.LINE_HEIGHT, "CARD FEE", "0.70"),
    ]
    other_page = [
        sr.placed_line(0, 0.0, "9.00", page=1),
        sr.placed_line(1, sr.LINE_HEIGHT, "SUBTOTAL", page=2),
    ]
    for pairing in ("nearest", "above"):
        assert _paired(worded, pairing)["SUBTOTAL"] == []
        assert _paired(other_page, pairing)["SUBTOTAL"] == []


def test_a_receipt_that_adds_up_reads_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pairing below is tried first and kept when its figures add up, so the
    receipts of both accuracy sets read as they do with that pairing alone."""
    specs = [*sr.tuning_specs(), *sr.held_out_specs()]
    views = [rp.view_lines_from_text(sr.receipt_text(spec)) for spec in specs]
    now = [rp.read_receipt(lines, threshold=THRESHOLD) for lines in views]
    monkeypatch.setattr(rp, "_PAIRINGS", ("below",))
    before = [rp.read_receipt(lines, threshold=THRESHOLD) for lines in views]
    assert now == before
    for spec, receipt in zip(sr.tuning_specs(), now, strict=False):
        assert receipt is not None
        assert receipt.adds_up is True, spec.seed
        assert (receipt.subtotal, receipt.tax, receipt.total) == (
            spec.subtotal,
            spec.tax,
            spec.total,
        ), spec.seed


def _below_only(text: str, monkeypatch: pytest.MonkeyPatch) -> Receipt:
    """``text`` read with the amounts-below pairing alone (how it read before)."""
    with monkeypatch.context() as patch:
        patch.setattr(rp, "_PAIRINGS", ("below",))
        return _read(text)


#: Every amount drifted to the line below its label, with nothing to check the
#: total by: the amount-only line above the tax label is an item's price.
DRIFTED_TAX_EXEMPT = (
    "FERNLEY STORES\n2026-03-14 10:15\nMILK  4.00\nBREAD  3.00\nAPPLES\n2.50\nTAX\n0.00\n"
)
DRIFTED_VAT = (
    "FERNLEY STORES\n14/03/2026 10:15\nMILK  £4.00\nBREAD  £3.00\nAPPLES\n£2.50\nVAT\n£1.58\n"
)


@pytest.mark.parametrize(
    ("text", "tax"),
    [
        (DRIFTED_TAX_EXEMPT + "TOTAL  9.50\n", "0.00"),
        (DRIFTED_VAT + "TOTAL  £9.50\n", "1.58"),
        (DRIFTED_TAX_EXEMPT + "TOTAL\n9.50\nVISA\n9.50\n", "0.00"),
        # The item's name lost: the amount is still not the tax printed below its label.
        (DRIFTED_TAX_EXEMPT.replace("APPLES\n", "") + "TOTAL  9.50\n", "0.00"),
    ],
)
def test_an_item_price_on_its_own_line_is_not_the_tax(
    text: str, tax: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _read(text)
    assert receipt.tax == Decimal(tax)
    assert receipt.total == Decimal("9.50")
    assert receipt.subtotal != Decimal("7.00")
    assert receipt == _below_only(text, monkeypatch)


def test_above_and_nearest_leave_an_item_its_price() -> None:
    lines = [
        sr.placed_line(0, 0.0, "APPLES"),
        sr.placed_line(1, sr.LINE_HEIGHT, "2.50"),
        sr.placed_line(2, 2 * sr.LINE_HEIGHT, "TAX"),
        sr.placed_line(3, 3 * sr.LINE_HEIGHT, "TOTAL", "9.50"),
    ]
    for pairing in ("below", "nearest", "above"):
        assert _paired(lines, pairing)["TAX"] == [], pairing
    # Under a keyword or a priced line the amount is free to take.
    for above in (("SUBTOTAL", "7.00"), ("APPLES", "2.50"), ("TIP",)):
        free = [sr.placed_line(0, 0.0, *above), *lines[1:]]
        assert _paired(free, "above")["TAX"] == [Decimal("2.50")], above


def test_a_label_with_its_amount_below_and_no_contradiction_is_kept() -> None:
    # Adding up through another pairing is no evidence against a reading
    # that paired its labels and that nothing contradicts.
    text = "SHOP\n2026-03-14\nMILK 4.00\nBREAD 3.00\n2.50\nTAX\n0.00\nTOTAL 9.50\n"
    assert _paired(rp.view_lines_from_text(text), "above")["TAX"] == [Decimal("2.50")]
    receipt = _read(text)
    assert (receipt.tax, receipt.total, receipt.adds_up) == (Decimal("0.00"), Decimal("9.50"), None)
    # No label paired below, no item the amount could belong to: above is tried.
    lost = "SHOP\n2026-03-14\nMILK 4.00\nBREAD 3.00\n2.50\nTAX\nTOTAL 9.50\n"
    receipt = _read(lost)
    assert (receipt.tax, receipt.total, receipt.adds_up) == (Decimal("2.50"), Decimal("9.50"), True)


def test_an_unchecked_receipt_reads_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """Amounts below their labels and no subtotal to check them by: the other
    pairings must not find a set that "adds up" out of an item's price."""
    specs = [*sr.tuning_specs(), *sr.held_out_specs()]
    texts = [sr.unchecked_receipt_text(spec) for spec in specs]
    now = [_read(text) for text in texts]
    before = [_below_only(text, monkeypatch) for text in texts]
    changed = [spec.seed for spec, a, b in zip(specs, now, before, strict=True) if a != b]
    assert changed == []
    assert any(spec.tax == 0 for spec in specs)
    for spec, receipt in zip(specs, now, strict=True):
        assert receipt.total == spec.total, spec.seed
        assert receipt.tax == spec.tax, spec.seed


@pytest.mark.parametrize(
    ("printed", "currency", "expected", "order"),
    [
        ("2026-03-04", None, date(2026, 3, 4), "ymd"),
        ("2026/03/04", None, date(2026, 3, 4), "ymd"),
        ("03/24/2026", None, date(2026, 3, 24), "mdy"),
        ("24/03/2026", None, date(2026, 3, 24), "dmy"),
        ("24.03.2026", None, date(2026, 3, 24), "dmy"),
        ("03/04/2026", "USD", date(2026, 3, 4), "mdy"),
        ("03/04/2026", "GBP", date(2026, 4, 3), "dmy"),
        ("03/04/26", "USD", date(2026, 3, 4), "mdy"),
        ("Mar 4, 2026", None, date(2026, 3, 4), "named"),
        ("4 Mar 2026", None, date(2026, 3, 4), "named"),
        ("March 4 2026", None, date(2026, 3, 4), "named"),
        ("04-MAR-26", None, date(2026, 3, 4), "named"),
        ("Sat 4th March 2026", None, date(2026, 3, 4), "named"),
    ],
)
def test_date_formats(printed: str, currency: str | None, expected: date, order: str) -> None:
    symbol = {"USD": "$", "GBP": "£", None: ""}[currency]
    receipt = _read(f"SHOP\nDate: {printed}\nTOTAL {symbol}5.00\n")
    assert receipt.purchase_date == expected
    assert receipt.date_order == order
    assert receipt.confidence["date"] >= THRESHOLD


def test_an_ambiguous_date_without_a_currency_is_a_guess() -> None:
    receipt = _read("SHOP\n03/04/2026\nTOTAL 5.00\n")
    assert receipt.date_order == "guessed"
    assert "date" in receipt.low_confidence


def test_labelled_date_wins_and_expiry_dates_are_ignored() -> None:
    text = "SHOP\nRef 2026-01-01\nTOTAL 5.00\nCard exp 2029-12-31\nDate: 2026-02-03 10:15\n"
    receipt = _read(text)
    assert receipt.purchase_date == date(2026, 2, 3)
    assert receipt.time == "10:15"


def test_purchase_date_survives_an_expiry_on_its_line() -> None:
    # A coupon or points expiry printed on the date line (R2).
    receipt = _read("SHOP\n03/14/2026 11:42 AM   EXPIRES 06/12/26\nTOTAL 5.00\n")
    assert receipt.purchase_date == date(2026, 3, 14)
    assert receipt.time == "11:42"
    assert receipt.confidence["date"] == 1.0
    assert "date" not in receipt.low_confidence


@pytest.mark.parametrize(
    "line",
    [
        "EXPIRES 06/12/26",
        "Return by 04/13/2026",
        # The word after its date: nothing says which side it belongs to.
        "06/12/26 EXPIRY",
        "04/13/2026 06/12/26 last day to return",
    ],
)
def test_a_lone_expiry_or_return_date_is_still_ignored(line: str) -> None:
    receipt = _read(f"SHOP\n{line}\nTOTAL 5.00\n")
    assert receipt.purchase_date is None
    assert rp._purchase_dates(line, "mdy") == []


def test_expiry_and_return_lines_beside_no_other_date() -> None:
    receipt = _read("SHOP\nEXPIRES 06/12/26\nReturn by 04/13/2026\nTOTAL 5.00\n")
    assert receipt.purchase_date is None


def test_expiry_first_then_purchase_date() -> None:
    receipt = _read("SHOP\nEXP 06/12/26  DATE 03/14/2026\nTOTAL 5.00\n")
    assert receipt.purchase_date == date(2026, 3, 14)


def test_purchase_dates_drops_only_the_date_its_lead_in_disowns() -> None:
    line = "03/14/2026 11:42 AM   EXPIRES 06/12/26"
    assert [d for d, _, _ in rp._dates_in(line, "mdy")] == [date(2026, 3, 14), date(2026, 6, 12)]
    assert [d for d, _, _ in rp._purchase_dates(line, "mdy")] == [date(2026, 3, 14)]
    plain = "Date: 2026-02-03 10:15"
    assert rp._purchase_dates(plain, None) == rp._dates_in(plain, None)


@pytest.mark.parametrize(
    "line",
    [
        # The second date of a validity or return range is no purchase date.
        "Coupon valid 03/15/2026 - 03/29/2026",
        "Offer valid from 03/15/2026 to 03/29/2026",
        "Returns accepted 03/14/2026 through 04/13/2026",
        "Return by 04/13/2026 or exchange until 05/13/2026",
        "Warranty valid 03/14/2026 - 03/14/2027",
        "Rewards expire 06/30/2026 Next statement 04/01/2026",
        "Valid 03/15/2026 09:00 - 03/29/2026 21:00",
        # Nor is a date in front of the word that nothing says is the purchase.
        "Member since 01/02/2019 EXP 01/02/2027",
        "Member since 01/02/2019 Expiry date 01/02/2027",
    ],
)
def test_a_range_or_an_unmarked_date_beside_an_expiry_is_no_purchase_date(line: str) -> None:
    receipt = _read(f"SHOP\nWIDGET 5.00\nTOTAL 5.00\n{line}\n")
    assert receipt.purchase_date is None
    assert "date" in receipt.low_confidence
    assert rp._purchase_dates(line, "mdy") == []


def test_a_validity_range_does_not_displace_the_purchase_date() -> None:
    text = "SHOP\nCoupon valid 03/15/2026 - 03/29/2026\nWIDGET 5.00\nTOTAL 5.00\n03/14/2026\n"
    assert _read(text).purchase_date == date(2026, 3, 14)


@pytest.mark.parametrize(
    "line",
    [
        "03/14/2026 11:42 AM   EXPIRES 06/12/26",
        "11:42 AM 03/14/2026   EXPIRES 06/12/26",
        "Date 03/14/2026   Valid until 06/12/26",
        "EXP 06/12/26  DATE 03/14/2026",
        "Return by 04/13/2026   Purchase date: 03/14/2026",
    ],
)
def test_a_date_beside_an_expiry_is_kept_on_a_time_or_a_date_label(line: str) -> None:
    assert [d for d, _, _ in rp._purchase_dates(line, "mdy")] == [date(2026, 3, 14)]


def test_mail_date_header_when_the_body_has_none() -> None:
    receipt = _read("SHOP\nTOTAL 5.00\n", source="email_body", sent=date(2026, 5, 6))
    assert receipt.purchase_date == date(2026, 5, 6)
    assert receipt.date_order is None


@pytest.mark.parametrize(
    "line",
    [
        "VISA **** 4321 9.99",
        "MASTERCARD XXXX4321 9.99",
        "Paid with Visa ending in 4321",
        "AMEX ...4321",
        "DEBIT CARD #### 4321",
        "VISA 4111111111114321",
    ],
)
def test_card_last_four_only(line: str) -> None:
    receipt = _read(f"SHOP\n2026-01-02\nTOTAL 9.99\n{line}\n")
    assert receipt.payment is not None
    assert receipt.payment.last4 == "4321"
    dumped = receipt.model_dump_json()
    # A printed full number never survives beyond its last four digits.
    assert "411111111111" not in dumped


def test_merchant_skips_address_phone_and_boilerplate() -> None:
    text = (
        "*** RECEIPT ***\nWelcome!\n55 Sample Ave\nTel (555) 010-2233\n"
        "PINE STREET DELI\nTOTAL 3.00\n"
    )
    receipt = _read(text)
    assert receipt.merchant == "PINE STREET DELI"
    assert receipt.merchant_candidates[0] == "PINE STREET DELI"


def test_mail_sender_name_is_the_first_merchant_candidate() -> None:
    receipt = _read("Order summary\nTOTAL 3.00\n", source="email_body", sender="Lantern Books")
    assert receipt.merchant == "Lantern Books"
    assert receipt.confidence["merchant"] == rp.SENDER_MERCHANT


def test_quantity_lines() -> None:
    text = "SHOP\n2026-01-02\n2 @ 1.50\nCOFFEE 3.00\nTEA 2 @ 2.00 4.00\nSUBTOTAL 7.00\nTOTAL 7.00\n"
    receipt = _read(text)
    assert [(i.label, i.quantity, i.unit_price, i.amount) for i in receipt.lines] == [
        ("COFFEE", 2, Decimal("1.50"), Decimal("3.00")),
        ("TEA", 2, Decimal("2.00"), Decimal("4.00")),
    ]


def test_no_total_no_receipt() -> None:
    assert rp.read_receipt(rp.view_lines_from_text("SHOP\nMILK 3.49\n"), threshold=0.85) is None


def _score(text: str) -> float:
    return rp._scores(rp.view_lines_from_text(text))


def test_detection() -> None:
    assert _score(BASIC) >= 0.8
    statement = "Statement period 01/01/2026 - 01/31/2026\nOpening balance 10.00\nTOTAL 5.00\n"
    assert _score(statement) == 0.0
    assert _score("ACME LTD\nINVOICE 42\nTOTAL 5.00\n") < 0.35
    assert _score("Hello\nnothing here\n") == 0.0


def test_lines_are_bounded() -> None:
    text = "SHOP\n" + "ITEM 1.00\n" * (rp.MAX_RECEIPT_LINES + 50) + "TOTAL 9.00\n"
    assert _score(text) == 0.0


def test_registry_lists_the_receipt_parser() -> None:
    assert any(p.name == rp.PARSER_NAME and p.kind == "receipt" for p in PARSERS)


def test_pdf_e_receipt_through_the_extractor_and_registry() -> None:
    spec = sr.receipt_spec(4242)
    result = extract_pdf(sr.receipt_pdf(spec), ExtractOptions(previews=False), "pdf")
    parsed = parse_extraction(result)
    assert parsed.parse is not None
    assert parsed.parse.parser is not None
    assert parsed.parse.parser.name == rp.PARSER_NAME
    assert parsed.statements == []
    receipt = parsed.parse.receipt
    assert receipt is not None
    assert receipt.total == spec.total
    assert receipt.purchase_date == spec.when
    assert receipt.currency == spec.currency
    assert receipt.low_confidence == []


def test_receipt_mail_body() -> None:
    spec = sr.receipt_spec(4243)
    result = extract_eml(sr.receipt_html_email(spec), ExtractOptions(), "eml")
    receipt = rp.parse_email_receipt(result, threshold=THRESHOLD)
    assert receipt is not None
    assert receipt.source == "email_body"
    assert receipt.merchant == spec.payee
    assert receipt.total == spec.total
    assert receipt.purchase_date == spec.when


def test_a_mail_that_is_not_a_receipt() -> None:
    msg = EmailMessage()
    msg["From"] = "Friend <friend@example.invalid>"
    msg["Subject"] = "Lunch?"
    msg.set_content("Are we still on for lunch on Friday?")
    result = extract_eml(msg.as_bytes(), ExtractOptions(), "eml")
    assert rp.parse_email_receipt(result, threshold=THRESHOLD) is None


def test_parser_detect_and_parse_on_a_document_view() -> None:
    spec = sr.receipt_spec(4244)
    result = extract_pdf(sr.receipt_pdf(spec), ExtractOptions(previews=False), "pdf")
    view = DocumentView.from_result(result)
    parser = rp.ReceiptParser()
    assert parser.detect(view) >= 0.8
    outcome = parser.parse(view, threshold=THRESHOLD)
    assert outcome.receipt is not None
    assert not outcome.empty


def test_a_line_with_an_ssn_or_ein_is_never_the_merchant() -> None:
    """P56-SR-R1-02: identity-shaped tokens are not merchant candidates."""
    text = "Jordan Quill Testperson SSN 000-00-0000\n" + BASIC
    receipt = _read(text)
    assert receipt.merchant == "CORNER MARKET"
    assert not any("000-00-0000" in c for c in receipt.merchant_candidates)


# -- what is not a receipt (bills, long documents) -------------------------------------


@pytest.mark.parametrize("kind", ["auto_loan", "student_loan", "activity_statement"])
def test_a_bill_is_not_a_receipt(kind: sr.BillKind) -> None:
    assert _score(sr.bill_text(kind)) == 0.0


def test_more_than_two_pages_is_not_a_receipt() -> None:
    spec = sr.receipt_spec(4245)
    result = extract_pdf(sr.receipt_pdf(spec), ExtractOptions(previews=False), "pdf")
    view = DocumentView.from_result(result)
    parser = rp.ReceiptParser()
    assert view.page_count == 1
    assert parser.detect(view) >= 0.8
    assert parser.detect(replace(view, page_count=rp.MAX_RECEIPT_PAGES)) > 0.0
    assert parser.detect(replace(view, page_count=4)) == 0.0


TWO_PAGE_ORDER = """HILLVIEW SUPPLY CO
ORDER 7731
BRACKET        3.49
SUBTOTAL       3.49
TAX            0.30
TOTAL          3.79
"""


def test_two_pages_without_receipt_words_is_below_the_minimum() -> None:
    lines = rp.view_lines_from_text(TWO_PAGE_ORDER)
    one_page = rp._scores(lines)
    assert one_page >= 0.7
    assert rp._scores(lines, pages=1) == one_page
    assert rp._scores(lines, pages=2) <= 0.3 < MIN_SCORE
    # A receipt word on the page: two pages keep the score of one.
    thanked = rp.view_lines_from_text(TWO_PAGE_ORDER + "THANK YOU\n")
    assert rp._scores(thanked, pages=2) == rp._scores(thanked) > one_page


def test_a_mail_bill_is_not_a_receipt_mail() -> None:
    msg = EmailMessage()
    msg["From"] = "Northgate Auto Credit <billing@northgate.invalid>"
    msg["Subject"] = "Your bill is ready"
    msg["Date"] = "Wed, 01 Apr 2026 12:00:00 +0000"
    msg.set_content(sr.bill_text("auto_loan"))
    result = extract_eml(msg.as_bytes(), ExtractOptions(), "eml")
    assert result.email is not None
    assert "Total amount due" in result.email.body_text
    assert rp.parse_email_receipt(result, threshold=THRESHOLD) is None


def test_no_synthetic_receipt_prints_a_bill_word() -> None:
    """The bill words open one new zero path in ``_scores``: no receipt of the
    accuracy sets may walk into it (the page rules do not apply to one page)."""
    specs = [*sr.tuning_specs(), *sr.held_out_specs()]
    assert specs
    for spec in specs:
        lines = rp.view_lines_from_text(sr.receipt_text(spec))
        text = " " + " ".join(rp.fold(line.text) for line in lines) + " "
        printed = [word for word in rp._BILL_WORDS if f" {word} " in text]
        assert printed == [], (spec.seed, printed)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""``app.core.money``: parsing, rounding, formatting and the API money shapes."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.money import (
    ISO_4217_CURRENCIES,
    MAX_ABS_AMOUNT,
    Money,
    MoneyError,
    MoneyIn,
    clean_currency,
    format_amount,
    minor_units,
    parse_amount,
    quantize,
    round_to_minor,
    sum_amounts,
    to_storage,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12.34", Decimal("12.340000")),
        ("-0.5", Decimal("-0.500000")),
        ("+3.000001", Decimal("3.000001")),
        (".25", Decimal("0.250000")),
        ("  7 ", Decimal("7.000000")),
        ("-0", Decimal("0.000000")),
        ("1.2300000", Decimal("1.230000")),  # trailing zeros beyond 6 decimals are harmless
        ("99999999999999.999999", MAX_ABS_AMOUNT),
        (Decimal("4.5"), Decimal("4.500000")),
        (42, Decimal("42.000000")),
    ],
)
def test_parse_amount_accepts(raw: object, expected: Decimal) -> None:
    value = parse_amount(raw)
    assert value == expected
    assert value.as_tuple().exponent == -6
    assert not value.is_signed() or value != 0


@pytest.mark.parametrize(
    "raw",
    [
        1.5,  # float: already lossy
        True,
        "1e3",
        "1E-2",
        "NaN",
        "Infinity",
        "-inf",
        "1,000.00",
        "12.",
        "",
        " ",
        "--1",
        "0x10",
        "1.0000001",  # 7 significant decimals
        "100000000000000",  # 15 integer digits
        "1" * 40,
        "١٢",  # non-ASCII digits
        None,
        [],
    ],
)
def test_parse_amount_refuses(raw: object) -> None:
    with pytest.raises(MoneyError):
        parse_amount(raw)


def test_quantize_is_half_even() -> None:
    assert quantize(Decimal("0.0000005")) == Decimal("0.000000")
    assert quantize(Decimal("0.0000015")) == Decimal("0.000002")
    assert quantize(Decimal("0.0000025")) == Decimal("0.000002")
    assert quantize(Decimal("-0.0000025")) == Decimal("-0.000002")
    assert round_to_minor(Decimal("2.345"), "USD") == Decimal("2.34")
    assert round_to_minor(Decimal("2.355"), "USD") == Decimal("2.36")
    assert round_to_minor(Decimal("2.5"), "JPY") == Decimal("2")
    assert round_to_minor(Decimal("3.5"), "JPY") == Decimal("4")
    assert round_to_minor(Decimal("1.2345"), "BHD") == Decimal("1.234")


def test_negative_zero_is_normalised() -> None:
    assert str(quantize(Decimal("-0.0000001"))) == "0.000000"
    assert format_amount(Decimal("-0"), "USD") == "0.00"
    assert to_storage(Decimal("-0.0000004")).is_signed() is False


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        (Decimal("12.340000"), "USD", "12.34"),
        (Decimal("12"), "USD", "12.00"),
        (Decimal("0.123400"), "USD", "0.1234"),
        (Decimal("-1234.5"), "EUR", "-1234.50"),
        (Decimal("1500.000000"), "JPY", "1500"),
        (Decimal("1500.5"), "JPY", "1500.5"),
        (Decimal("1.2"), "BHD", "1.200"),
        (Decimal("1E+3"), "USD", "1000.00"),
        (Decimal("0.0000004"), "USD", "0.00"),
        (Decimal("99999999999999.999999"), "USD", "99999999999999.999999"),
    ],
)
def test_format_amount(amount: Decimal, currency: str, expected: str) -> None:
    assert format_amount(amount, currency) == expected
    assert "e" not in expected.lower()


def test_minor_units() -> None:
    assert (minor_units("USD"), minor_units("JPY"), minor_units("KWD")) == (2, 0, 3)


def test_currencies() -> None:
    assert clean_currency(" eur ") == "EUR"
    for bad in ("XXX", "XAU", "US", "USDX", "usd1", "", "€"):
        with pytest.raises(MoneyError):
            clean_currency(bad)
    assert {"USD", "EUR", "JPY", "GBP"} <= ISO_4217_CURRENCIES
    assert not {"XTS", "XXX", "XAU", "XDR"} & ISO_4217_CURRENCIES


def test_sum_amounts_is_exact() -> None:
    values = [Decimal("0.1")] * 10 + [Decimal("99999999999999.999999"), Decimal("-0.000001")]
    assert sum_amounts(values) == Decimal("100000000000000.999998")
    assert sum_amounts([]) == 0


def test_money_in_validates_and_forbids_extras() -> None:
    money = MoneyIn.model_validate({"amount": "-12.5", "currency": "usd"})
    assert (money.amount, money.currency) == (Decimal("-12.500000"), "USD")
    for body in (
        {"amount": 12.5, "currency": "USD"},  # JSON number
        {"amount": "12.5", "currency": "XXX"},
        {"amount": "1e2", "currency": "USD"},
        {"amount": "12.5", "currency": "USD", "account_id": "x"},
        {"amount": "12.5"},
    ):
        with pytest.raises(ValidationError):
            MoneyIn.model_validate(body)


def test_money_out() -> None:
    money = Money.of(Decimal("-12.500000"), "USD")
    assert money.model_dump() == {"amount": "-12.50", "currency": "USD"}
    assert money.as_decimal() == Decimal("-12.5")

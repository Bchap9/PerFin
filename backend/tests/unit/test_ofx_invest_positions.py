# SPDX-License-Identifier: AGPL-3.0-or-later
"""OFX prices that are not the price of one unit (``app.ingestion.extract.ofx_invest``).

A bond's ``UNITPRICE`` is a percentage of par and an option's is the price of
one share of the contract, while ``UNITS`` are face value and contracts. The
extractor settles which reading a file uses by the value printed beside the
price (``MKTVAL`` or ``TOTAL``).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.ingestion.extract.ofx import extract_ofx
from app.ingestion.extract.ofx_invest import _nearer
from app.ingestion.parsers.brokerage import reconcile_investment
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, InvestmentStatement
from tests.fixtures import synth_brokerage as synth

OPTIONS = ExtractOptions(previews=False, ocr=False)


def _extract(data: bytes) -> ExtractResult:
    result = extract_ofx(data, OPTIONS, "ofx")
    assert result.ok
    assert result.warnings == []
    return result


def _only(data: bytes) -> InvestmentStatement:
    result = _extract(data)
    assert len(result.investments) == 1
    return result.investments[0]


def test_debt_position_is_priced_per_100() -> None:
    statement = _only(synth.ofx_debt_position())
    (position,) = statement.positions
    assert position.security.asset_class == "bond"
    assert position.quantity == 25000
    assert position.price_per_100 is True
    assert position.price_per_unit == Decimal("0.98750")
    assert position.market_value == Decimal("24687.50")
    assert reconcile_investment(statement).status == "ok"


def test_debt_position_quoted_per_unit_is_left_alone() -> None:
    statement = _only(synth.ofx_debt_position_per_unit())
    (position,) = statement.positions
    assert position.price_per_100 is False
    assert position.price_per_unit == Decimal("0.98750")
    assert reconcile_investment(statement).status == "ok"


@pytest.mark.parametrize("shperctrct", [100, None], ids=["printed", "default"])
def test_option_position_price_is_per_contract(shperctrct: int | None) -> None:
    statement = _only(synth.ofx_option_position(shperctrct))
    (position,) = statement.positions
    assert position.security.asset_class == "option"
    assert position.quantity == 2
    assert position.unit_price == Decimal("340.00")
    assert position.price_per_100 is False
    assert reconcile_investment(statement).status == "ok"


def test_option_contract_size_is_the_printed_one() -> None:
    data = synth.ofx_option_position(10).replace(b"<MKTVAL>680.00", b"<MKTVAL>68.00")
    statement = _only(data)
    (position,) = statement.positions
    assert position.unit_price == Decimal("34.00")
    assert reconcile_investment(statement).status == "ok"


@pytest.mark.parametrize("size", [b"0", b"-100", b"10001", b"many"])
def test_an_implausible_contract_size_is_the_usual_hundred(size: bytes) -> None:
    data = synth.ofx_option_position(100).replace(
        b"<SHPERCTRCT>100</SHPERCTRCT>", b"<SHPERCTRCT>" + size + b"</SHPERCTRCT>"
    )
    (position,) = _only(data).positions
    assert position.unit_price == Decimal("340.00")


def test_option_position_already_priced_per_contract_is_left_alone() -> None:
    data = synth.ofx_option_position().replace(
        b"<UNITPRICE>3.40</UNITPRICE>", b"<UNITPRICE>340.00</UNITPRICE>"
    )
    statement = _only(data)
    (position,) = statement.positions
    assert position.unit_price == Decimal("340.00")
    assert reconcile_investment(statement).status == "ok"


def test_stock_positions_are_never_rescaled() -> None:
    # A stock whose printed value is a hundredth of quantity x price is a
    # mismatch to hold, not a bond: only POSDEBT is read per 100.
    data = synth.ofx_truncated_values().replace(b"<MKTVAL>600.00", b"<MKTVAL>6.00")
    statement = _only(data)
    first, second = statement.positions
    assert (first.unit_price, first.price_per_100) == (Decimal("19.8700000"), False)
    assert (second.unit_price, second.price_per_100) == (Decimal("50.00"), False)
    check = reconcile_investment(statement)
    assert check.status == "mismatch"
    assert 1 in check.position_mismatches


def test_option_trade_price_is_per_contract_and_classed() -> None:
    statement = _only(synth.ofx_option_trades())
    (trade,) = statement.activity
    assert trade.type == "buy"
    assert trade.quantity == 2
    assert trade.unit_price == Decimal("185.00")
    assert trade.security is not None
    assert trade.security.asset_class == "option"
    assert trade.amount == Decimal("-370.65")
    assert trade.fees == Decimal("0.65")


def test_option_trade_without_a_catalogue_uses_its_own_contract_size() -> None:
    # A mini contract of ten shares, said by the trade itself; the file has no SECLIST.
    data = (
        synth.ofx_option_trades()
        .replace(b"<OPTBUYTYPE>", b"<SHPERCTRCT>10</SHPERCTRCT><OPTBUYTYPE>")
        .replace(b"<TOTAL>-370.65", b"<TOTAL>-37.65")
    )
    (trade,) = _only(data).activity
    assert trade.unit_price == Decimal("18.50")
    assert trade.amount == Decimal("-37.65")
    # A size that is not believed is the usual hundred.
    data = synth.ofx_option_trades().replace(
        b"<OPTBUYTYPE>", b"<SHPERCTRCT>0</SHPERCTRCT><OPTBUYTYPE>"
    )
    (trade,) = _only(data).activity
    assert trade.unit_price == Decimal("185.00")


def test_option_sale_without_a_catalogue_uses_its_own_contract_size() -> None:
    # The same for a SELLOPT: ten shares a contract, said by the trade itself.
    data = (
        synth.ofx_option_trades()
        .replace(b"BUYOPT>", b"SELLOPT>")
        .replace(b"INVBUY>", b"INVSELL>")
        .replace(
            b"<OPTBUYTYPE>BUYTOOPEN</OPTBUYTYPE>",
            b"<OPTSELLTYPE>SELLTOCLOSE</OPTSELLTYPE><SHPERCTRCT>10</SHPERCTRCT>",
        )
        .replace(b"<UNITS>2<", b"<UNITS>-2<")
        .replace(b"<TOTAL>-370.65", b"<TOTAL>36.35")
    )
    (trade,) = _only(data).activity
    assert trade.type == "sell"
    assert trade.unit_price == Decimal("18.50")
    assert trade.amount == Decimal("36.35")


def _debt_trade(verb: str, price: bytes, total: bytes) -> bytes:
    inner = "INVBUY" if verb == "BUY" else "INVSELL"
    return (
        synth.ofx_option_trades()
        .replace(b"BUYOPT>", f"{verb}DEBT>".encode())
        .replace(b"INVBUY>", f"{inner}>".encode())
        .replace(b"<OPTBUYTYPE>BUYTOOPEN</OPTBUYTYPE>", b"")
        .replace(b"<UNITS>2</UNITS>", b"<UNITS>5000</UNITS>")
        .replace(b"<UNITPRICE>1.85</UNITPRICE>", b"<UNITPRICE>" + price + b"</UNITPRICE>")
        .replace(b"<COMMISSION>0.65</COMMISSION>", b"<COMMISSION>1.00</COMMISSION>")
        .replace(b"<TOTAL>-370.65</TOTAL>", b"<TOTAL>" + total + b"</TOTAL>")
    )


@pytest.mark.parametrize(
    ("verb", "price", "total", "per_100", "amount"),
    [
        ("BUY", b"99.200", b"-4961.00", True, "-4961.00"),
        ("SELL", b"99.200", b"4959.00", True, "4959.00"),
        ("BUY", b"0.99200", b"-4961.00", False, "-4961.00"),
    ],
    ids=["buy-per-100", "sell-per-100", "buy-per-unit"],
)
def test_debt_trade_price_is_per_100_when_the_total_says_so(
    verb: str, price: bytes, total: bytes, per_100: bool, amount: str
) -> None:
    statement = _only(_debt_trade(verb, price, total))
    (trade,) = statement.activity
    assert trade.security is not None
    assert trade.security.asset_class == "bond"
    assert trade.quantity == 5000
    assert trade.price_per_100 is per_100
    assert trade.price_per_unit == Decimal("0.99200")
    assert trade.amount == Decimal(amount)


def test_stock_trades_keep_their_price_and_class() -> None:
    data = (
        synth.ofx_option_trades()
        .replace(b"BUYOPT>", b"BUYSTOCK>")
        .replace(b"<OPTBUYTYPE>BUYTOOPEN</OPTBUYTYPE>", b"<BUYTYPE>BUY</BUYTYPE>")
    )
    (trade,) = _only(data).activity
    assert trade.unit_price == Decimal("1.85")
    assert trade.price_per_100 is False
    assert trade.security is not None
    assert trade.security.asset_class == "other"


def test_child_may_say_price_per_100() -> None:
    result = _extract(synth.ofx_debt_position())
    assert result.investments[0].positions[0].price_per_100 is True
    assert result.worker_only_fields() is False
    # Only the last four digits of the account leave the child.
    assert synth.SMALL_ACCOUNT not in result.model_dump_json()
    assert result.investments[0].account.last4 == synth.SMALL_ACCOUNT[-4:]


@pytest.mark.parametrize(
    ("printed", "plain", "scaled", "expected"),
    [
        (Decimal("24687.50"), Decimal("2468750"), Decimal("24687.50"), True),
        (Decimal("24687.50"), Decimal("24687.50"), Decimal("246.875"), False),
        # Exactly between the two readings: not strictly nearer, so left alone.
        (Decimal("50"), Decimal("0"), Decimal("100"), False),
        # Nothing printed: the OFX definition applies.
        (None, Decimal("6.80"), Decimal("680.00"), True),
    ],
)
def test_nearer(printed: Decimal | None, plain: Decimal, scaled: Decimal, expected: bool) -> None:
    assert _nearer(printed, plain, scaled) is expected

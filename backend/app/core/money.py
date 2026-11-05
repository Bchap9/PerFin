# SPDX-License-Identifier: AGPL-3.0-or-later
"""Money: ``Decimal`` amounts, ISO 4217 currencies and the API money shape.

Rules (DATA_MODEL.md, API.md, DEVELOPMENT.md "Database rules"):

* Amounts are :class:`decimal.Decimal`, never ``float``. The database column
  is ``NUMERIC(20, 6)`` (:data:`AMOUNT_PLACES` decimals, at most
  :data:`AMOUNT_INTEGER_DIGITS` integer digits) next to a ``CHAR(3)``
  currency column.
* Rounding is banker's rounding (``ROUND_HALF_EVEN``) everywhere, so sums of
  rounded values do not drift in one direction. Calculations run in a local
  decimal context with :data:`DECIMAL_PRECISION` significant digits and are
  rounded once, at the end, to :data:`AMOUNT_PLACES` decimals
  (:func:`quantize`) or to the currency's minor unit (:func:`round_to_minor`).
* The API carries money as ``{"amount": "12.34", "currency": "USD"}``
  (:class:`Money`). :func:`format_amount` renders an amount with at least the
  currency's minor-unit decimals and no trailing zeros beyond them (stored
  precision is never lost: ``0.123400 USD`` is ``"0.1234"``), never in
  exponent notation. Inputs take the amount as a **string**
  (:class:`MoneyIn`): a JSON number would already have been through a
  binary float in many clients.
* Currencies are active ISO 4217 codes (:data:`ISO_4217_CURRENCIES`): fund
  codes, precious metals, SDR, the testing / "no currency" codes and
  withdrawn codes are refused.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field

#: Decimals stored for every amount (``NUMERIC(20, 6)``).
AMOUNT_PLACES: Final = 6
#: Integer digits that fit ``NUMERIC(20, 6)``.
AMOUNT_INTEGER_DIGITS: Final = 14
AMOUNT_QUANTUM: Final = Decimal(1).scaleb(-AMOUNT_PLACES)
#: Largest absolute amount that fits the column.
MAX_ABS_AMOUNT: Final = Decimal(10) ** AMOUNT_INTEGER_DIGITS - AMOUNT_QUANTUM
#: Significant digits for intermediate calculations (IEEE 754 decimal128).
DECIMAL_PRECISION: Final = 34
#: Longest amount string accepted on input (sign, 14 + 6 digits, point, slack).
AMOUNT_INPUT_MAX: Final = 32

# Active ISO 4217 currency codes. Fund codes, precious metals, SDR and the
# testing/"no currency" codes are left out on purpose (BOV, CHE, CHW, CLF,
# COU, MXV, USN, UYI, UYW, XAG, XAU, XBA, XBB, XBC, XBD, XDR, XPD, XPT, XSU,
# XTS, XUA, XXX), as are withdrawn codes.
ISO_4217_CURRENCIES: Final = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB
    BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUC CUP CVE CZK DJF DKK
    DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG
    HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT
    LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR
    MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB
    RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT
    TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VED VES VND VUV WST XAF XCD XCG
    XOF XPF YER ZAR ZMW ZWG
    """.split()  # noqa: SIM905 - a compact table reads better than 160 lines
)

#: Minor-unit exponents that differ from the usual two decimals (ISO 4217).
_MINOR_UNITS: Final[dict[str, int]] = {
    **dict.fromkeys(
        "BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX VND VUV XAF XOF XPF".split(),  # noqa: SIM905
        0,
    ),
    **dict.fromkeys("BHD IQD JOD KWD LYD OMR TND".split(), 3),  # noqa: SIM905
}

# Plain decimal: optional sign, ASCII digits, optional fraction. No exponent, no
# grouping, no whitespace inside, no bare point.
_AMOUNT_RE: Final = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)$")
_CURRENCY_RE: Final = re.compile(r"^[A-Z]{3}$")


class MoneyError(ValueError):
    """An amount or currency that is not acceptable."""


def calc_context() -> Context:
    """The decimal context for money calculations (34 digits, half-even)."""
    return Context(prec=DECIMAL_PRECISION, rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------------------
# Currencies
# ---------------------------------------------------------------------------


def is_currency(value: object) -> bool:
    return isinstance(value, str) and value in ISO_4217_CURRENCIES


def clean_currency(value: str) -> str:
    """Upper-cased active ISO 4217 code, or :class:`MoneyError`."""
    if not isinstance(value, str):
        raise MoneyError("must be an active ISO 4217 currency code such as USD")
    code = value.strip().upper()
    if not _CURRENCY_RE.fullmatch(code) or code not in ISO_4217_CURRENCIES:
        raise MoneyError("must be an active ISO 4217 currency code such as USD")
    return code


def minor_units(currency: str) -> int:
    """Decimals of the currency's minor unit (2 for USD, 0 for JPY, 3 for BHD)."""
    return _MINOR_UNITS.get(currency, 2)


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------


def _check_range(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise MoneyError("amount must be a finite number")
    if abs(value) > MAX_ABS_AMOUNT:
        raise MoneyError(f"amount must be at most {AMOUNT_INTEGER_DIGITS} integer digits")
    return value


def parse_amount(value: object) -> Decimal:
    """A stored-precision amount from an API string (or a ``Decimal``/``int``).

    Refuses floats and booleans, exponents, ``NaN``/``Infinity``, more than
    :data:`AMOUNT_PLACES` decimals and values that do not fit the column.
    ``-0`` becomes ``0``. The result is quantized to :data:`AMOUNT_PLACES`.
    """
    if isinstance(value, bool | float):
        raise MoneyError('amount must be a decimal string such as "12.34"')
    if isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, Decimal):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if len(text) > AMOUNT_INPUT_MAX or not _AMOUNT_RE.fullmatch(text):
            raise MoneyError('amount must be a decimal string such as "12.34"')
        number = Decimal(text)
    else:
        raise MoneyError('amount must be a decimal string such as "12.34"')
    _check_range(number)
    if number.quantize(AMOUNT_QUANTUM, context=calc_context()) != number:
        raise MoneyError(f"amount must have at most {AMOUNT_PLACES} decimals")
    return quantize(number)


def quantize(value: Decimal, places: int = AMOUNT_PLACES) -> Decimal:
    """``value`` rounded half-even to ``places`` decimals; ``-0`` becomes ``0``."""
    _check_range(value)
    quantum = Decimal(1).scaleb(-places)
    try:
        result = value.quantize(quantum, rounding=ROUND_HALF_EVEN, context=calc_context())
    except InvalidOperation as exc:  # pragma: no cover - guarded by _check_range
        raise MoneyError("amount out of range") from exc
    return result + 0 if result.is_zero() else result  # ``+ 0`` drops the sign of -0


def round_to_minor(value: Decimal, currency: str) -> Decimal:
    """``value`` rounded half-even to the currency's minor unit (cents, yen, fils)."""
    return quantize(value, minor_units(currency))


def to_storage(value: Decimal) -> Decimal:
    """Round a calculated amount for a ``NUMERIC(20, 6)`` column."""
    return quantize(value, AMOUNT_PLACES)


def format_amount(value: Decimal, currency: str) -> str:
    """API string for an amount: at least the minor-unit decimals, no exponent.

    Trailing zeros beyond the currency's minor unit are dropped; significant
    stored decimals are kept (``Decimal("0.123400")`` USD -> ``"0.1234"``,
    ``Decimal("1500.000000")`` JPY -> ``"1500"``, ``-0`` -> ``"0.00"``).
    """
    places = minor_units(currency)
    stored = quantize(value, AMOUNT_PLACES)
    normalized = stored.normalize(context=calc_context())
    exponent = normalized.as_tuple().exponent
    decimals = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    shown = quantize(stored, max(places, decimals))
    return f"{shown:f}"


def sum_amounts(values: Iterable[Decimal]) -> Decimal:
    """Exact sum (34 significant digits) of ``values``; ``0`` when empty."""
    total = Decimal(0)
    with localcontext(calc_context()):
        for value in values:
            total += value
    return total


# ---------------------------------------------------------------------------
# API shapes
# ---------------------------------------------------------------------------


def _amount_input(value: object) -> Decimal:
    if not isinstance(value, str):
        raise MoneyError('amount must be a decimal string such as "12.34"')
    return parse_amount(value)


#: An amount on input: a decimal string, at most 6 decimals, fits NUMERIC(20, 6).
AmountIn = Annotated[Decimal, BeforeValidator(_amount_input)]
#: A currency code on input (normalised to upper case, active ISO 4217 only).
CurrencyCode = Annotated[str, Field(min_length=3, max_length=3), AfterValidator(clean_currency)]


class MoneyIn(BaseModel):
    """Money in a request body: ``{"amount": "12.34", "currency": "USD"}``."""

    model_config = ConfigDict(extra="forbid")

    amount: AmountIn
    currency: CurrencyCode


class Money(BaseModel):
    """Money in a response body; build with :meth:`of`."""

    model_config = ConfigDict(frozen=True)

    amount: str
    currency: str

    @classmethod
    def of(cls, amount: Decimal, currency: str) -> Money:
        return cls(amount=format_amount(amount, currency), currency=currency)

    def as_decimal(self) -> Decimal:
        return Decimal(self.amount)

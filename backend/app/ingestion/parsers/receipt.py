# SPDX-License-Identifier: AGPL-3.0-or-later
"""The receipt parser: merchant, date, totals, items, payment (INGESTION.md "Parse").

Reads the pages of a receipt (a PDF e-receipt's text layer or the OCR of a
photo or scan of a thermal print) and, through :func:`parse_email_receipt`,
the text body of a receipt mail. Deterministic rules only (ADR-0007); the
input is the schema-checked extraction, never file bytes (ADR-0006), and
every pattern runs on single lines cut to :data:`MAX_LINE` characters.

* **Labels** are compared after OCR folding (:func:`fold`): lower case,
  punctuation as spaces, and inside words the digits Tesseract confuses
  with letters read as letters (``T0TAL`` -> ``total``, ``$UB`` -> ``sub``),
  plus a near-miss match for the few keywords that matter (``TOTAI``).
* **Amounts** (:func:`read_money`): two decimals after a point or a comma
  (``11.43``, ``11,43``, ``1,234.56``, ``1.234,56``), an optional currency
  symbol or code, a minus sign, parentheses or a trailing minus (discounts),
  a trailing tax-code letter (``3.49 A``). Integers, percentages, times and
  dates are never money.
* **Totals**: lines are typed by keyword (subtotal, tax/VAT/GST, tip,
  discount, total, tender), and the parser looks for the **consistent set**:
  a total for which subtotal + tax (+ tip - discount) = total, or the item
  lines add up, or the card payment equals it. A consistent total scores
  1.0; a labelled one that cannot be checked :data:`conf.UNCHECKED`; one
  that contradicts the other figures :data:`conf.GUESSED`. A missing
  subtotal or tax is derived from the others (:data:`conf.DERIVED`).
  A label whose amount OCR put on a line of its own takes the amount-only
  line below it; when that reading finds no labelled total, is
  contradicted, or paired no label at all, the nearest such line (above or
  below) and then the line above are tried, and the first set that adds up
  is kept (:func:`_best_reading`). The amount under an unpriced item line
  is that item's price and is never taken from above.
* **Date**: every date format of :mod:`app.ingestion.classify.dates` plus
  ``14-MAR-26``; a labelled date ("Date", "Order date") or one printed next
  to a time wins over others; a line that says expiry, valid, return by or
  the like gives a date only where a date label or a time marks it as the
  purchase date (:func:`_purchase_dates`).
  Numeric dates whose day/month order is not visible follow the receipt's
  currency (GBP/EUR day first, USD/CAD month first) at the threshold, or
  are a guess (:data:`conf.GUESSED`) without one.
* **Merchant**: the first header line that is not an address, phone,
  date, web address or receipt boilerplate (and, for mail, the sender's
  display name first). The worker's matcher
  (:mod:`app.ingestion.match.receipts`) then looks the candidates up among
  the household's payee names and aliases and raises the confidence of a
  known one to 1.0.
* **Payment**: card brand (or cash) and the **last four digits** of a
  masked card number ("**** 1234", "ending in 1234"); a printed full number
  is cut to its last four here and never kept. The digits are identity
  data: stored in the extraction, never logged, never put in a review
  item or an audit row.
* **Line items** (best effort): priced lines above the totals, with
  ``2 @ 1.50`` quantities.

OCR lowers each field's score by the Tesseract confidence of the cell its
value was read from (:func:`conf.scaled`).
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import ClassVar, Final, Literal

from app.core.money import sum_amounts
from app.ingestion.classify import dates as date_rules
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.base import (
    Cell,
    DocumentParser,
    DocumentView,
    ParseOutcome,
    ViewLine,
)
from app.ingestion.parsers.text import MAX_LINE
from app.ingestion.reconcile import TOLERANCE
from app.ingestion.sandbox.protocol import (
    ExtractResult,
    Receipt,
    ReceiptLine,
    ReceiptPayment,
)

PARSER_NAME: Final = "receipt_basic"
PARSER_VERSION: Final = "1"
#: Receipts are short; a longer document is not read as one.
MAX_RECEIPT_LINES: Final = 400
#: A receipt is one page, sometimes two; a longer document is a statement, a
#: bill or a contract however many "Total" lines it prints.
MAX_RECEIPT_PAGES: Final = 2
#: The most a two-page document with none of ``_RECEIPT_WORDS`` scores: below
#: ``registry.MIN_SCORE``, so it is a receipt only when the classifier says so.
UNWORDED_TWO_PAGE_SCORE: Final = 0.3
#: Header lines the merchant is looked for in.
HEADER_LINES: Final = 8
#: How a keyword line without an amount is paired with an amount-only line,
#: in the order :func:`read_receipt` tries them.
Pairing = Literal["below", "nearest", "above"]
_PAIRINGS: tuple[Pairing, ...] = ("below", "nearest", "above")
#: The farthest amount-only line the ``nearest`` pairing takes, in line heights.
PAIRING_REACH: Final = 1.5
MAX_CANDIDATES: Final = 5
MAX_ITEMS: Final = 200
#: Score of an email body's detection needed to read it as a receipt.
EMAIL_MIN_SCORE: Final = 0.5
#: Confidence of a numeric date whose order follows the currency.
ORDER_FROM_CURRENCY: Final = 0.85
#: Confidence of a merchant read from the header lines (scaled by OCR).
HEADER_MERCHANT: Final = 0.9
SENDER_MERCHANT: Final = 0.95

#: OCR noise around a figure ("\u201c47.80", "165.75:").
_NOISE: Final = "\"'`\u201c\u201d\u2018\u2019\u00ab\u00bb~\u00b0*|:;_"
#: Keywords OCR runs together ("AMOUNTDUE", "SALESTAX8%").
_GLUED: Final = re.compile(
    r"(total|amount|balance|sales|grand|sub)[a-z]?(due|tax|total|amount|paid)"
)
_COLON_MONEY: Final = re.compile(r"(\d{1,5}):(\d{2})")
#: Letters and digits run together by OCR ("taxx5" -> "taxx 5").
_ALNUM_EDGE: Final = re.compile(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])")
_CONFUSABLE: Final = str.maketrans({"0": "o", "1": "l", "5": "s", "$": "s", "|": "l"})
_KEYWORDS: Final = ("total", "subtotal", "tax", "change", "cash", "amount", "balance")

_SUBTOTAL: Final = re.compile(r"\bsub ?tota?l|\bnet (?:total|amount)\b|\bmerchandise total\b")
_TAX: Final = re.compile(
    r"\b(?:sales ?)?tax(?![a-z])|\bvat(?![a-z])|\bgst\b|\bhst\b|\bpst\b|\bqst\b"
)
_NOT_TAX: Final = re.compile(
    r"\btax (?:id|no|number|reg\w*|exempt)\b|\bvat (?:no|number|reg\w*|id)\b|\btax invoice\b"
)
_TIP: Final = re.compile(r"\btip\b|\bgratuity\b|\bservice charge\b")
_DISCOUNT: Final = re.compile(r"\bdiscount\b|\bsavings?\b|\bcoupon\b|\bpromo\b|\byou saved\b")
_TOTAL: Final = re.compile(
    r"\btotal|\bamount due\b|\bbalance due\b|\bamount payable\b|\bto pay\b|\bpurchase amount\b"
)
_STRONG_TOTAL: Final = re.compile(
    r"\bgrand total\b|\bamount due\b|\bbalance due\b|\btotal ?due\b|\btotal amount\b"
    r"|\btotal to pay\b|\bamount payable\b"
)
_NOT_TOTAL: Final = re.compile(
    r"\btotal ?(?:tax|vat|savings?|discounts?|items?|qty|quantity|points|units)\b"
    r"|\bitems? total\b|\btax total\b|\bvat total\b|\bno of items\b"
)
_CHANGE: Final = re.compile(r"\bchange(?: due)?\b")
_TENDER: Final = re.compile(
    r"\bcash\b|\btendered\b|\bvisa\b|\bmaster ?card\b|\bamex\b|\bamerican express\b"
    r"|\bdiscover\b|\bmaestro\b|\bdebit\b|\bcredit\b|\bcard\b|\bpaid\b|\bpayment\b"
    r"|\bapple pay\b|\bgoogle pay\b|\bcontactless\b"
)
_BRANDS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"\bvisa\b"), "visa"),
    (re.compile(r"\bmaster ?card\b|\bmc\b"), "mastercard"),
    (re.compile(r"\bamex\b|\bamerican express\b"), "amex"),
    (re.compile(r"\bdiscover\b"), "discover"),
    (re.compile(r"\bmaestro\b"), "maestro"),
    (re.compile(r"\bapple pay\b|\bgoogle pay\b"), "mobile"),
    (re.compile(r"\bdebit\b"), "debit"),
    (re.compile(r"\bcredit\b"), "credit"),
)
_CARDISH: Final = re.compile(r"\bcard\b|\bacct\b|\baccount\b|\bending\b|\bpan\b")
_MASKED_LAST4: Final = re.compile(r"(?:[*xX#\u2022.\u00ab\u00bb+]\s?){2,}\s?(\d{4})(?!\d)")
_ENDING_LAST4: Final = re.compile(
    r"\b(?:ending(?:\s+in)?|last\s+(?:4|four)(?:\s+digits)?)\s*[:#]?\s*(\d{4})(?!\d)",
    re.IGNORECASE,
)
_FULL_NUMBER: Final = re.compile(r"(?<!\d)\d{12,19}(?!\d)")
_LONE_FOUR: Final = re.compile(r"(?<![\d.,])(\d{4})(?![\d.,])")

_MONEY: Final = re.compile(
    r"(?P<open>\()?(?P<sign>[-\u2212])?(?P<code>USD|EUR|GBP|CAD|AUD|CHF)?"
    r"(?P<sym>[$\u00a3\u20ac])?(?P<sign2>[-\u2212])?"
    r"(?P<int>\d{1,3}(?:[.,]\d{3})+|\d+)[.,](?P<dec>\d{2})"
    r"(?P<sym2>[$\u00a3\u20ac])?(?P<close>\))?(?P<trail>-)?(?P<flag>[A-Za-z])?"
)
_CODES: Final = ("USD", "EUR", "GBP", "CAD", "AUD", "NZD", "CHF", "SEK", "NOK", "DKK")
_CODE: Final = re.compile(r"\b(" + "|".join(_CODES) + r")\b")
_SYMBOLS: Final = {"$": "USD", "\u00a3": "GBP", "\u20ac": "EUR"}
_DAY_FIRST: Final = frozenset({"GBP", "EUR", "AUD", "NZD", "CHF", "SEK", "NOK", "DKK"})
_MONTH_FIRST: Final = frozenset({"USD", "CAD"})
_SHORT_NAMED: Final = re.compile(
    r"(?<!\d)(\d{1,2})[-\s]([A-Za-z]{3})[-\s](\d{2})(?![\d:])", re.IGNORECASE
)
_MONTHS: Final = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_TIME: Final = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?\s?([AaPp][Mm])?(?![\d])")
_DATE_LABEL: Final = re.compile(
    r"\b(?:date|order date|purchase date|transaction date|receipt date|sale date|dated)\b"
)
_NOT_PURCHASE_DATE: Final = re.compile(
    r"\bexp\w*\b|\bvalid\b|\breturn\w*\b|\bbest before\b|\buse by\b|\bdue date\b|\bbirth\b"
)
_NOT_MERCHANT: Final = re.compile(
    r"\breceipt\b|\binvoice\b|\bwelcome\b|\bthank\b|\bstore\s*(?:no|#|\d)|\btel\b|\bphone\b"
    r"|\bfax\b|\bwww\b|\bhttp|\b\w+ (?:com|net|org|invalid)\b|\border\b|\bcashier\b"
    r"|\bvat\b|\btax\b|\breg\b|\bcustomer copy\b|\bmerchant copy\b|\bduplicate\b"
)
#: SSN/ITIN (``123-45-6789``) and EIN (``12-3456789``) shapes (P56-SR-R1-02).
_TIN_LIKE: Final = re.compile(r"(?<!\d)(?:\d{3}-\d{2}-\d{4}|\d{2}-\d{7})(?!\d)")
#: Address shapes (only checked on lines with a digit: "PINE STREET DELI" is a name).
_ADDRESS: Final = re.compile(
    r"^\d+\s+\w+|\b(?:st|street|ave|avenue|road|rd|lane|ln|blvd|way|drive|dr|suite|unit)\b"
    r"|\b\d{5}(?:-\d{4})?\b|\b[a-z]{1,2}\d{1,2}\s?\d[a-z]{2}\b"
)
_STATEMENT_WORDS: Final = (
    "statement period",
    "opening balance",
    "closing balance",
    "beginning balance",
    "ending balance",
    "account summary",
    "statement date",
    "previous balance",
    "new balance",
    "minimum payment",
    "gross pay",
    "net pay",
    "pay period",
    "payslip",
    "pay stub",
)
_RECEIPT_WORDS: Final = (
    "receipt",
    "thank you",
    "thanks for",
    "cashier",
    "change",
    "tendered",
    "your order",
    "order date",
    "auth",
)
#: What a loan bill, a mortgage statement or an investment statement prints
#: beside its "Total" or "Amount due" and no till or order confirmation does
#: (whole words of the folded text). "year to date", "ytd", "due date" and
#: "account number" are left out on purpose: loyalty totals, invoices and
#: store accounts print them on real receipts.
_BILL_WORDS: Final = (
    "principal balance",
    "outstanding principal",
    "principal paid",
    "interest paid",
    "interest rate",
    "escrow",
    "payoff",
    "loan number",
    "loan seq",
    "payment due date",
    "past due",
    "amount past due",
    "payment coupon",
    "payment allocation",
    "account statement",
    "activity statement",
    "starting cash",
    "ending cash",
    "net asset value",
    "vested balance",
)
_QTY_ONLY: Final = re.compile(r"^(\d{1,4})\s?[@xX]\s?(\S{0,20})$")
_QTY_IN: Final = re.compile(r"\b(\d{1,4})\s?[@x]\s?$")
_HEADER_QTY: Final = re.compile(r"\bqty\b|\bquantity\b")

Kind = Literal["subtotal", "tax", "tip", "discount", "total", "change", "tender", "item", "other"]


def fold(text: str, *, ocr: bool = True) -> str:
    """A label for keyword comparison: lower case, single spaced, near misses
    of the keywords corrected; with ``ocr`` the confusable digits inside
    words are read as letters."""
    tokens: list[str] = []
    for token in text[:MAX_LINE].split():
        if ocr and any(ch.isalpha() for ch in token):
            token = token.translate(_CONFUSABLE)
        cleaned = "".join(ch if ch.isalnum() else " " for ch in token.lower())
        cleaned = _ALNUM_EDGE.sub(" ", cleaned)
        tokens.extend(_GLUED.sub(r"\1 \2", cleaned).split())
    out: list[str] = []
    for token in tokens:
        if token.isalpha() and token not in _KEYWORDS:
            close = [
                k
                for k in difflib.get_close_matches(token, _KEYWORDS, n=1, cutoff=0.8)
                if abs(len(k) - len(token)) <= 1
            ]
            token = close[0] if close else token
        out.append(token)
    return " ".join(out)


@dataclass(frozen=True)
class Money:
    value: Decimal
    currency: str | None
    #: Tesseract confidence of the cell (0..1); None for a text layer.
    conf: float | None
    #: Read from digits whose decimal point OCR lost ("6778" on a TOTAL
    #: line); only ever used when it makes the figures consistent.
    implied: bool = False


def read_money(token: str) -> tuple[Decimal, str | None] | None:
    """``(value, currency)`` of one whitespace-free token, or None."""
    token = token.strip(_NOISE)[:64]
    match = _MONEY.fullmatch(token)
    if match is None and token[-1:] in (".", ","):
        match = _MONEY.fullmatch(token[:-1])
    if match is None:
        colon = _COLON_MONEY.fullmatch(token)
        if colon is not None and (int(colon[1]) > 23 or int(colon[2]) > 59):
            # "94:29" is no time of day: a decimal point OCR read as a colon.
            match = _MONEY.fullmatch(f"{colon[1]}.{colon[2]}")
    if match is None or bool(match["open"]) != bool(match["close"]):
        return None
    digits = re.sub(r"[.,]", "", match["int"])
    value = Decimal(f"{digits}.{match['dec']}")
    sign = match["sign"] or match["sign2"]
    if match["open"] or match["trail"] or sign:
        value = -value
    symbol = match["sym"] or match["sym2"]
    if match["code"]:
        return value, match["code"]
    return value, _SYMBOLS.get(symbol) if symbol else None


def _money_in(cells: Sequence[Cell]) -> list[Money]:
    out: list[Money] = []
    for cell in cells:
        tokens = cell.text[:MAX_LINE].split()
        for index, token in enumerate(tokens):
            read = read_money(token)
            if read is None:
                continue
            value, currency = read
            for near in (tokens[index - 1] if index else "", tokens[index + 1 : index + 2]):
                text = near if isinstance(near, str) else (near[0] if near else "")
                if text.upper() in _CODES:
                    currency = text.upper()
            out.append(Money(value, currency, cell.conf))
    return out


def _label(text: str) -> tuple[str, str]:
    """The line without its amounts, folded with and without OCR corrections."""
    kept = " ".join(t for t in text[:MAX_LINE].split() if read_money(t) is None)
    return fold(kept), fold(kept, ocr=False)


@dataclass
class _Line:
    line: ViewLine
    label: str
    money: list[Money]
    kind: Kind = "other"
    strong: bool = False
    #: The label without OCR corrections ("TAX8" stays "tax8", not "taxb").
    plain: str = ""
    #: The amounts come from an adjacent amount-only line (:func:`_typed`).
    paired: bool = False

    @property
    def amount(self) -> Money | None:
        return self.money[-1] if self.money else None


def _kind(label: str, has_money: bool, plain: str = "") -> tuple[Kind, bool]:
    if not has_money:
        return "other", False
    if plain and plain != label:
        first = _kind(label, has_money)
        if first[0] not in ("item", "other"):
            return first
        return _kind(plain, has_money)
    if _SUBTOTAL.search(label):
        return "subtotal", False
    if _TAX.search(label) and not _NOT_TAX.search(label) and not _STRONG_TOTAL.search(label):
        return "tax", False
    if _TIP.search(label):
        return "tip", False
    if _DISCOUNT.search(label):
        return "discount", False
    if _TOTAL.search(label) and not _NOT_TOTAL.search(label):
        return "total", bool(_STRONG_TOTAL.search(label))
    if _CHANGE.search(label):
        return "change", False
    if _TENDER.search(label):
        return "tender", False
    return ("item" if sum(ch.isalpha() for ch in label) >= 2 else "other"), False


def _amount_only(entry: _Line) -> bool:
    return bool(entry.money) and sum(ch.isalpha() for ch in entry.label) < 2


def _apart(a: ViewLine, b: ViewLine) -> float | None:
    """The vertical distance between the centres of two lines (None: no cells)."""
    if not a.cells or not b.cells:
        return None
    (_, a0, _, a1), (_, b0, _, b1) = a.bbox, b.bbox
    return abs((a0 + a1) / 2 - (b0 + b1) / 2)


def _unpriced_item(entry: _Line, below: _Line) -> bool:
    """``entry`` is an item's name without a price and ``below`` the line
    under it on the same page: an amount-only line there is that item's
    price ("APPLES" / "2.50"), not the amount of the label that follows."""
    if entry.money or entry.paired or entry.line.page != below.line.page:
        return False
    return _kind(entry.label, True, entry.plain)[0] == "item"


def _amount_line(out: Sequence[_Line], index: int, pairing: Pairing) -> _Line | None:
    """The amount-only line the amountless keyword line ``out[index]`` takes."""
    entry = out[index]
    after = out[index + 1] if index + 1 < len(out) else None
    if pairing == "below":
        return after if after is not None and _amount_only(after) else None
    before = out[index - 1] if index else None
    if before is not None and index >= 2 and _unpriced_item(out[index - 2], before):
        before = None
    # The line below first: it keeps a tie.
    near = [
        other
        for other in (after, before)
        if other is not None and _amount_only(other) and other.line.page == entry.line.page
    ]
    if pairing == "above":
        return before if before in near else None
    reach = PAIRING_REACH * entry.line.height
    best: tuple[float, _Line] | None = None
    for other in near:
        gap = _apart(entry.line, other.line)
        if gap is not None and gap <= reach and (best is None or gap < best[0]):
            best = (gap, other)
    return best[1] if best is not None else None


def _typed(lines: Iterable[ViewLine], *, pairing: Pairing = "below") -> list[_Line]:
    """The lines with their amounts and kinds.

    A keyword whose amount OCR put on a line of its own ("Subtotal" /
    "107.65") takes the amounts of an adjacent amount-only line: the line
    ``below`` it, the line ``above`` it on the same page, or the ``nearest``
    of the two (by the distance between line centres, the line below on a
    tie, none farther than :data:`PAIRING_REACH` line heights). An amount
    line is given to one label only, and the line above is never taken when
    it sits under an item's name that has no price (:func:`_unpriced_item`).
    """
    out: list[_Line] = []
    for line in lines:
        money = _money_in(line.cells)
        label, plain = _label(line.text)
        kind, strong = _kind(label, bool(money), plain)
        if not money:
            implied = _implied(line, label, plain)
            if implied:
                money = implied
                kind, strong = _kind(label, True, plain)
        out.append(_Line(line, label, money, kind, strong, plain))
    for index, entry in enumerate(out):
        if entry.money:
            continue
        kind, strong = _kind(entry.label, True, entry.plain)
        if kind in ("item", "other"):
            continue
        source = _amount_line(out, index, pairing)
        if source is None:
            continue
        entry.money, entry.kind, entry.strong = source.money, kind, strong
        entry.paired = True
        source.money, source.kind = [], "other"
    return out


_IMPLIED: Final = re.compile(r"^(?:USD|EUR|GBP|CAD|AUD|CHF)?[$\u00a3\u20ac-]?(\d{3,7})$")


def _implied(line: ViewLine, label: str, plain: str) -> list[Money]:
    """Amounts of a totals line whose decimal points OCR dropped (``Money.implied``)."""
    if _kind(label, True, plain)[0] not in ("subtotal", "tax", "total", "tender", "tip"):
        return []
    out: list[Money] = []
    for cell in line.cells:
        for token in cell.text.split():
            match = _IMPLIED.fullmatch(token.strip(_NOISE).rstrip(".,"))
            if match is not None:
                out.append(Money(Decimal(match[1]) / 100, None, cell.conf, implied=True))
    return out[-1:]


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= TOLERANCE


# -- totals ---------------------------------------------------------------------


@dataclass
class _Totals:
    total: Money | None = None
    subtotal: Money | None = None
    tax: Decimal | None = None
    tax_conf: float | None = None
    tip: Decimal | None = None
    discount: Decimal | None = None
    adds_up: bool | None = None
    confirmed_by_tender: bool = False
    derived: set[str] = field(default_factory=set)
    total_labelled: bool = False


def _first(lines: Sequence[_Line], kind: Kind) -> list[_Line]:
    return [t for t in lines if t.kind == kind and t.amount is not None]


def _sum(values: Iterable[Decimal]) -> Decimal:
    return sum_amounts(values)


def _variants(value: Decimal) -> list[Decimal]:
    """``value`` and, for a figure whose currency symbol OCR read as a digit
    (``\u00a3116.83`` -> ``8116.83``), the figure without its first digit."""
    out = [value]
    digits = str(int(abs(value)))
    if len(digits) >= 2:
        out.append(abs(value) - Decimal(int(digits[0]) * 10 ** (len(digits) - 1)))
    return out


def _solve(typed: Sequence[_Line], items: Sequence[ReceiptLine]) -> _Totals:
    totals = _first(typed, "total")
    subs = _first(typed, "subtotal")
    tax_lines = _first(typed, "tax")
    tips = _first(typed, "tip")
    discounts = _first(typed, "discount")
    cards = [t for t in _first(typed, "tender") if _brand(t.label) not in (None, "cash")]
    tax = _sum(abs(t.amount.value) for t in tax_lines if t.amount) if tax_lines else None
    tip = _sum(abs(t.amount.value) for t in tips if t.amount) if tips else None
    discount = _sum(abs(t.amount.value) for t in discounts if t.amount) if discounts else None
    item_sum = _sum(i.amount for i in items) if items else None
    out = _Totals(tax=tax, tip=tip, discount=discount)
    if tax_lines:
        confs = [t.amount.conf for t in tax_lines if t.amount and t.amount.conf is not None]
        out.tax_conf = min(confs) if confs else None
    sub_values = [
        (variant, sub.amount)
        for sub in subs
        if sub.amount
        for variant in _variants(sub.amount.value)
    ]

    def consistent(value: Decimal) -> tuple[bool, Money | None]:
        extras = {Decimal(0), tip or Decimal(0)}
        cuts = {Decimal(0), discount or Decimal(0)}
        for sub_value, money in sub_values:
            for extra in extras:
                for cut in cuts:
                    if _close(sub_value + (tax or 0) + extra - cut, value):
                        return True, Money(sub_value, money.currency, money.conf)
                    if tax is not None and _close(sub_value + extra - cut, value):
                        # VAT-inclusive totals: the tax is part of the subtotal.
                        return True, Money(sub_value, money.currency, money.conf)
        if item_sum is not None:
            for extra in extras:
                for cut in cuts:
                    if _close(item_sum + (tax or Decimal(0)) + extra - cut, value) or (
                        tax is not None and _close(item_sum + extra - cut, value)
                    ):
                        return True, None
        return False, None

    # Labelled totals (strong labels first, then the later line), then card payments.
    ranked = [t for _, t in sorted(enumerate(totals), key=lambda it: (not it[1].strong, -it[0]))]
    for line in [*ranked, *cards]:
        if line.amount is None:  # pragma: no cover - _first keeps lines with an amount
            continue
        for value in _variants(line.amount.value):
            ok, sub = consistent(value)
            if ok:
                out.total = Money(value, line.amount.currency, line.amount.conf)
                out.subtotal = sub
                out.adds_up = True
                out.total_labelled = line.kind == "total"
                break
        if out.total is not None:
            break
    # Figures read from digits without their decimal point are never believed alone.
    printed = [t for t in ranked if t.amount is not None and not t.amount.implied]
    printed_cards = [t for t in cards if t.amount is not None and not t.amount.implied]
    if out.total is None and printed and (amount := printed[0].amount) is not None:
        out.total = Money(abs(amount.value), amount.currency, amount.conf)
        out.total_labelled = True
        if subs and (tax is not None or item_sum is not None):
            out.adds_up = False
    if out.total is None and printed_cards and (amount := printed_cards[0].amount) is not None:
        out.total = Money(abs(amount.value), amount.currency, amount.conf)
    if out.total is None:
        return out
    total = out.total
    out.confirmed_by_tender = out.total_labelled and any(
        t.amount is not None and _close(abs(t.amount.value), total.value) for t in cards
    )
    if out.subtotal is None and subs:
        nearest = min(
            (s.amount for s in subs if s.amount), key=lambda m: abs(abs(m.value) - total.value)
        )
        out.subtotal = Money(abs(nearest.value), nearest.currency, nearest.conf)
    if out.subtotal is None and tax is not None:
        derived = total.value - tax - (tip or 0) + (discount or 0)
        if derived >= 0:
            out.subtotal = Money(derived, None, total.conf)
            out.derived.add("subtotal")
    if out.tax is None and out.subtotal is not None:
        rest = total.value - abs(out.subtotal.value) - (tip or 0) + (discount or 0)
        if rest >= 0:
            out.tax = rest
            out.tax_conf = total.conf
            out.derived.add("tax")
    return out


# -- dates, payment, currency, merchant -----------------------------------------------


DateKind = Literal["ymd", "named", "visible", "hinted", "guessed"]


@dataclass(frozen=True)
class _DateFound:
    value: date
    kind: DateKind
    order: Literal["ymd", "mdy", "dmy", "named", "guessed"]
    score: int
    conf: float | None
    #: Labelled ("Date:") or printed next to a time.
    anchored: bool


def _order_hint(currency: str | None) -> date_rules.DateOrder | None:
    if currency in _DAY_FIRST:
        return "dmy"
    if currency in _MONTH_FIRST:
        return "mdy"
    return None


_DateRead = tuple[date, DateKind, Literal["ymd", "mdy", "dmy", "named", "guessed"]]


def _dated_spans(text: str, order: date_rules.DateOrder | None) -> list[tuple[int, int, _DateRead]]:
    """Every date of a line with where it starts and ends, in reading order."""
    found: list[tuple[int, int, _DateRead]] = []
    for hit in date_rules.find_dates(text, order=order):
        value = hit.value
        if value is None:
            continue
        piece = text[hit.start : hit.end]
        if re.match(r"\d{4}", piece):
            found.append((hit.start, hit.end, (value, "ymd", "ymd")))
        elif any(ch.isalpha() for ch in piece):
            found.append((hit.start, hit.end, (value, "named", "named")))
        else:
            first = int(re.match(r"\d+", piece)[0])  # type: ignore[index]
            read: Literal["mdy", "dmy"] = "mdy" if first == hit.month else "dmy"
            if hit.ambiguous or (hit.day <= 12 and hit.day != hit.month and order is None):
                found.append((hit.start, hit.end, (value, "guessed", "guessed")))
            elif hit.day <= 12 and hit.day != hit.month:
                found.append((hit.start, hit.end, (value, "hinted", read)))
            else:
                found.append((hit.start, hit.end, (value, "visible", read)))
    if not found:
        for m in _SHORT_NAMED.finditer(text[:MAX_LINE]):
            month = _MONTHS.get(m[2].lower())
            if month is None:
                continue
            try:
                value = date(2000 + int(m[3]), month, int(m[1]))
            except ValueError:
                continue
            found.append((m.start(), m.end(), (value, "named", "named")))
    return found


def _dates_in(text: str, order: date_rules.DateOrder | None) -> list[_DateRead]:
    return [read for _, _, read in _dated_spans(text, order)]


def _beside_time(text: str, start: int, end: int, floor: int) -> bool:
    """A time of day is printed directly after the date at ``text[start:end]``,
    or directly before it and after ``floor`` (the end of the previous date)."""
    after = _TIME.search(text, end)
    if after is not None and text[end : after.start()].strip(" ,@-").lower() in ("", "at"):
        return True
    return any(
        not text[match.end() : start].strip(" ,-") for match in _TIME.finditer(text, floor, start)
    )


def _purchase_dates(text: str, order: date_rules.DateOrder | None) -> list[_DateRead]:
    """The dates of a line that may be the day of the purchase.

    A line without an expiry, validity or return word
    (:data:`_NOT_PURCHASE_DATE`) gives all its dates. On a line with one, a
    date is kept only on positive evidence that it is the purchase date:

    * its own lead-in (the words between the previous date, or the line
      start, and the date) holds a date label and no such word:
      ``EXP 06/12/26  DATE 03/14/2026`` keeps its second date;
    * or it stands in front of the first disowned date with a time of day
      beside it: ``03/14/2026 11:42 AM  EXPIRES 06/12/26`` keeps its first.

    So the second date of a range (``valid 03/15/2026 - 03/29/2026``,
    ``Return by ... or exchange until ...``) and an unmarked date in front of
    the word (``Member since 01/02/2019 EXP 01/02/2027``) are left out. When
    the word is in front of none of the dates (``06/12/26 EXPIRY``) nothing
    says which date it speaks of, and none is kept.
    """
    spans = _dated_spans(text, order)
    kept: list[_DateRead] = []
    disowned = False
    previous_end = 0
    for start, end, read in spans:
        lead = fold(text[previous_end:start])
        if _NOT_PURCHASE_DATE.search(lead):
            disowned = True
        elif _DATE_LABEL.search(lead) or (
            not disowned and _beside_time(text, start, end, previous_end)
        ):
            kept.append(read)
        previous_end = max(previous_end, end)
    if disowned:
        return kept
    if _NOT_PURCHASE_DATE.search(fold(text)):
        return []
    return [read for _, _, read in spans]


def _find_date(
    typed: Sequence[_Line], currency: str | None
) -> tuple[_DateFound | None, str | None]:
    order = _order_hint(currency)
    best: _DateFound | None = None
    time: str | None = None
    count = max(1, len(typed))
    for index, entry in enumerate(typed):
        text = entry.line.text
        label = fold(text)
        time_match = _TIME.search(text)
        labelled = bool(_DATE_LABEL.search(label))
        for value, kind, read in _purchase_dates(text, order):
            score = (3 if labelled else 0) + (2 if time_match else 0)
            score += 1 if index < count * 0.5 else 0
            score -= 1 if kind == "guessed" else 0
            if best is None or score > best.score:
                best = _DateFound(
                    value,
                    kind,
                    read,
                    score,
                    _cell_conf(entry.line, lambda t: bool(_dates_in(t, order))),
                    labelled or bool(time_match),
                )
                time = _hhmm(time_match) if time_match else None
    return best, time


def _cell_conf(line: ViewLine, holds: Callable[[str], bool]) -> float | None:
    """OCR confidence of the cells whose text ``holds`` the value (else the line's)."""
    values = [c.conf for c in line.cells if c.conf is not None and holds(c.text)]
    return min(values) if values else line.conf


def _containing(needle: str) -> Callable[[str], bool]:
    return lambda text: needle in text


def _hhmm(match: re.Match[str]) -> str | None:
    hour, minute, half = int(match[1]), int(match[2]), (match[3] or "").lower()
    if half == "pm" and hour < 12:
        hour += 12
    if half == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}" if hour < 24 else None


def _brand(label: str) -> str | None:
    for pattern, name in _BRANDS:
        if pattern.search(label):
            return name
    if re.search(r"\bcash\b", label):
        return "cash"
    if re.search(r"\bcard\b|\bcontactless\b", label):
        return "card"
    return None


def _last4(text: str) -> str | None:
    text = text[:MAX_LINE]
    full = _FULL_NUMBER.search(text.replace(" ", ""))
    if full is not None:
        return full[0][-4:]
    for pattern in (_MASKED_LAST4, _ENDING_LAST4):
        match = pattern.search(text)
        if match is not None:
            return match[1]
    return None


def _find_payment(typed: Sequence[_Line]) -> tuple[ReceiptPayment | None, float | None]:
    method: str | None = None
    last4: str | None = None
    last4_conf: float | None = None
    for entry in typed:
        label = entry.label
        brand = _brand(label)
        cardish = brand not in (None, "cash") or bool(_CARDISH.search(label))
        if cardish and last4 is None:
            found = _last4(entry.line.text)
            if found is None and brand not in (None, "cash", "card"):
                # "VISA 1234" with the mask lost to OCR: a lone four-digit group.
                stripped = " ".join(t for t in entry.line.text.split() if read_money(t) is None)
                lone = _LONE_FOUR.findall(stripped)
                found = (
                    lone[-1] if len(lone) == 1 and not lone[-1].startswith(("19", "20")) else None
                )
            if found is not None:
                last4 = found
                last4_conf = _cell_conf(entry.line, _containing(found))
        upgrade = method is None or (method in ("card", "cash") and brand != "cash")
        if brand is not None and upgrade:
            if brand == "cash" and entry.kind not in ("tender", "change"):
                continue
            method = brand
    if method is None and last4 is None:
        return None, None
    return ReceiptPayment(method=method, last4=last4), last4_conf


def _find_currency(typed: Sequence[_Line], money: Sequence[Money]) -> tuple[str | None, float]:
    for entry in typed:
        if "currency" in entry.label:
            match = _CODE.search(entry.line.text.upper())
            if match is not None:
                return match[1], 1.0
    codes = [m.currency for m in money if m.currency]
    if codes:
        best = max(set(codes), key=codes.count)
        return best, (conf.GENERIC if best != "USD" else conf.DERIVED)
    for entry in typed:
        match = _CODE.search(entry.line.text)
        if match is not None:
            return match[1], conf.DERIVED
    return None, conf.MISSING


def _merchant_lines(typed: Sequence[_Line]) -> list[tuple[str, float | None]]:
    out: list[tuple[str, float | None]] = []
    for entry in typed[:HEADER_LINES]:
        text = " ".join(entry.line.text.split())[:200]
        label = fold(text)
        letters = sum(ch.isalpha() for ch in text)
        if letters < 3 or letters < len(text.replace(" ", "")) * 0.5:
            continue
        if entry.money or _dates_in(text, None) or _TIME.search(text):
            continue
        if _NOT_MERCHANT.search(label) or (
            any(ch.isdigit() for ch in text) and _ADDRESS.search(label)
        ):
            continue
        if re.search(r"\d{3}[\s.-]?\d{4}", text) or _TIN_LIKE.search(text):
            # Phone numbers; SSN/EIN-shaped tokens are identity, never a merchant.
            continue
        cleaned = re.sub(r"^\W+|\W+$", "", text)
        if cleaned:
            out.append((cleaned, entry.line.conf))
        if len(out) >= MAX_CANDIDATES:
            break
    return out


# -- items -------------------------------------------------------------------------


def _items(typed: Sequence[_Line], start: int) -> list[ReceiptLine]:
    out: list[ReceiptLine] = []
    qty_header = False
    pending_qty: tuple[int, Decimal | None] | None = None
    for entry in typed[start:]:
        if entry.kind in ("subtotal", "total", "tax", "tender", "change"):
            break
        if _HEADER_QTY.search(entry.label) and not entry.money:
            qty_header = True
            continue
        label_words = [t for t in entry.line.text.split() if read_money(t) is None]
        only = _QTY_ONLY.match(" ".join(entry.line.text.split()))
        if only is not None:
            # "2 @ 48.11" above the item line it multiplies.
            each = read_money(only[2]) if only[2] else None
            pending_qty = (int(only[1]), abs(each[0]) if each else None)
            continue
        if entry.kind != "item" or entry.amount is None or len(out) >= MAX_ITEMS:
            pending_qty = None
            continue
        words = label_words
        quantity: int | None = None
        unit: Decimal | None = None
        if pending_qty is not None:
            quantity, unit = pending_qty
        joined = " ".join(words)
        match = _QTY_IN.search(joined)
        if match is not None and len(entry.money) >= 2:
            quantity, unit = int(match[1]), abs(entry.money[0].value)
            joined = joined[: match.start()].strip()
        elif qty_header and words and words[-1].isdigit() and len(words[-1]) <= 3:
            quantity = int(words[-1]) or None
            joined = " ".join(words[:-1])
        pending_qty = None
        label = joined.strip(" .:-")[:200]
        if not label:
            continue
        out.append(
            ReceiptLine(
                label=label,
                amount=abs(entry.amount.value),
                quantity=quantity if quantity and quantity > 0 else None,
                unit_price=unit,
            )
        )
    return out


# -- the parser ----------------------------------------------------------------------


def _scores(view_lines: Sequence[ViewLine], *, pages: int = 1) -> float:
    """How much the lines read as a receipt (0 to 0.95); ``pages`` is the
    page count of the document they come from (a mail body is one page)."""
    if not view_lines or len(view_lines) > MAX_RECEIPT_LINES:
        return 0.0
    text = " " + " ".join(fold(line.text) for line in view_lines) + " "
    if any(f" {word} " in text for word in _STATEMENT_WORDS):
        return 0.0
    if any(f" {word} " in text for word in _BILL_WORDS):
        return 0.0
    typed = _typed(view_lines)
    # Keywords count even where OCR lost the amount next to them.
    kinds = {t.kind for t in typed} | {_kind(t.label, True, t.plain)[0] for t in typed}
    cards = any(
        t.kind == "tender" and t.money and _brand(t.label) not in (None, "cash") for t in typed
    )
    if "total" not in kinds and not (cards and "subtotal" in kinds):
        return 0.0
    score = 0.45
    score += 0.15 if "subtotal" in kinds else 0.0
    score += 0.1 if "tax" in kinds else 0.0
    score += 0.1 if ("tender" in kinds or "change" in kinds) else 0.0
    worded = any(f" {word} " in text for word in _RECEIPT_WORDS)
    score += 0.1 if worded else 0.0
    score += 0.05 if "item" in kinds else 0.0
    if " invoice " in text and " receipt " not in text:
        score = min(score, 0.3)
    if pages == MAX_RECEIPT_PAGES and not worded:
        score = min(score, UNWORDED_TWO_PAGE_SCORE)
    return round(min(score, 0.95), 4)


def _scale(value: float, ocr: float | None) -> float:
    return conf.scaled(value, ocr)


@dataclass
class _Reading:
    """What one pairing of labels and amount lines reads (:func:`_typed`)."""

    typed: list[_Line]
    currency: str | None
    currency_conf: float
    header: list[tuple[str, float | None]]
    found_date: _DateFound | None
    time: str | None
    items: list[ReceiptLine]
    totals: _Totals


def _reading(view_lines: Sequence[ViewLine], pairing: Pairing) -> _Reading:
    typed = _typed(view_lines, pairing=pairing)
    money = [m for t in typed for m in t.money]
    currency, currency_conf = _find_currency(typed, money)
    header = _merchant_lines(typed)
    found_date, time = _find_date(typed, currency)
    date_index = next(
        (i for i, t in enumerate(typed) if found_date and _dates_in(t.line.text, None)), 0
    )
    start = max(date_index + 1, 1 if header else 0)
    items = _items(typed, start)
    totals = _solve(typed, items)
    return _Reading(typed, currency, currency_conf, header, found_date, time, items, totals)


def _settled(reading: _Reading) -> bool:
    """A labelled total that nothing contradicts, in a reading that paired at
    least one label with the amount below it: another pairing "adding up" is
    then no evidence, since it can do so by moving an amount to the wrong
    label (an unlabelled price read as the tax)."""
    totals = reading.totals
    return (
        totals.total_labelled
        and totals.adds_up is None
        and any(entry.paired for entry in reading.typed)
    )


def _best_reading(view_lines: Sequence[ViewLine]) -> _Reading:
    """The first pairing whose figures add up; the first pairing when none does.

    Amounts below their labels are tried first, so a receipt that adds up
    that way reads as it always did; a pairing that does not add up is never
    preferred to it. The other pairings are tried only when the first found
    no labelled total, is contradicted by its own figures, or paired no
    label at all (:func:`_settled`).
    """
    first: _Reading | None = None
    for pairing in _PAIRINGS:
        reading = _reading(view_lines, pairing)
        if reading.totals.adds_up:
            return reading
        if first is None:
            first = reading
            if _settled(reading):
                break
    if first is None:  # pragma: no cover - _PAIRINGS is never empty
        return _reading(view_lines, "below")
    return first


def read_receipt(
    view_lines: Sequence[ViewLine],
    *,
    threshold: float,
    source: Literal["pages", "email_body"] = "pages",
    sender: str | None = None,
    sent: date | None = None,
) -> Receipt | None:
    """The receipt on ``view_lines`` (None: no total found)."""
    reading = _best_reading(view_lines[:MAX_RECEIPT_LINES])
    typed, items, totals = reading.typed, reading.items, reading.totals
    currency, currency_conf = reading.currency, reading.currency_conf
    header, found_date, time = reading.header, reading.found_date, reading.time
    if totals.total is None:
        return None
    payment, last4_conf = _find_payment(typed)
    candidates: list[str] = []
    merchant_conf: float | None = None
    merchant: str | None = None
    if sender:
        candidates.append(sender[:200])
        merchant, merchant_conf = sender[:200], SENDER_MERCHANT
    for text, ocr in header:
        if text not in candidates:
            candidates.append(text)
        if merchant is None:
            merchant, merchant_conf = text, _scale(HEADER_MERCHANT, ocr)
    # Scores (module doc).
    scores: dict[str, float] = {}
    scores["merchant"] = merchant_conf if merchant_conf is not None else conf.MISSING
    if found_date is not None:
        if found_date.kind == "guessed":
            base = conf.GUESSED
        elif found_date.kind == "hinted":
            base = ORDER_FROM_CURRENCY
        else:
            base = 1.0 if found_date.anchored else conf.GENERIC
        scores["date"] = _scale(base, found_date.conf)
        purchase_date: date | None = found_date.value
        order = found_date.order
    elif sent is not None:
        purchase_date, order = sent, None
        scores["date"] = conf.DERIVED if source == "email_body" else conf.INFERRED
    else:
        purchase_date, order = None, None
        scores["date"] = conf.MISSING
    total = totals.total
    if totals.adds_up or totals.confirmed_by_tender:
        total_base = 1.0
    elif totals.adds_up is False:
        total_base = conf.GUESSED
    elif totals.total_labelled:
        total_base = conf.UNCHECKED
    else:
        total_base = conf.INFERRED
    # A consistent set confirms the figures however unsure OCR was of each
    # (as a running balance confirms a statement row).
    checked = bool(totals.adds_up or totals.confirmed_by_tender)
    scores["total"] = 1.0 if checked else _scale(total_base, total.conf)
    if totals.subtotal is not None:
        base = (
            1.0
            if totals.adds_up
            else conf.UNRECONCILED
            if totals.adds_up is False
            else conf.UNCHECKED
        )
        if "subtotal" in totals.derived:
            base = conf.DERIVED
        scores["subtotal"] = (
            1.0 if totals.adds_up and base == 1.0 else _scale(base, totals.subtotal.conf)
        )
    if totals.tax is not None:
        base = (
            1.0
            if totals.adds_up
            else conf.UNRECONCILED
            if totals.adds_up is False
            else conf.UNCHECKED
        )
        if "tax" in totals.derived:
            base = conf.DERIVED
        scores["tax"] = 1.0 if totals.adds_up and base == 1.0 else _scale(base, totals.tax_conf)
    if currency is not None:
        # No currency printed: the account's currency applies (not a doubt).
        scores["currency"] = currency_conf
    if payment is not None and payment.last4 is not None:
        scores["payment_last4"] = _scale(conf.GENERIC, last4_conf)
    if items:
        reference = totals.subtotal.value if totals.subtotal is not None else total.value
        scores["lines"] = (
            1.0 if _close(_sum(i.amount for i in items), abs(reference)) else conf.GUESSED
        )
    receipt = Receipt(
        source=source,
        merchant=merchant,
        merchant_candidates=candidates[: MAX_CANDIDATES + 1],
        purchase_date=purchase_date,
        time=time,
        date_order=order,
        currency=currency,
        subtotal=abs(totals.subtotal.value) if totals.subtotal is not None else None,
        tax=totals.tax,
        tip=totals.tip,
        discount=totals.discount,
        total=total.value,
        lines=items[:MAX_ITEMS],
        payment=payment,
        adds_up=totals.adds_up,
        confidence={k: round(v, 4) for k, v in scores.items()},
        low_confidence=conf.low_fields(scores, threshold),
    )
    return receipt


class ReceiptParser(DocumentParser):
    name: ClassVar[str] = PARSER_NAME
    version: ClassVar[str] = PARSER_VERSION
    kind: ClassVar[Literal["statement", "payslip", "receipt"]] = "receipt"

    def detect(self, view: DocumentView) -> float:
        if view.page_count > MAX_RECEIPT_PAGES:
            return 0.0
        lines = [line for line in view.lines if line.page <= MAX_RECEIPT_PAGES]
        return _scores(lines, pages=view.page_count)

    def parse(
        self, view: DocumentView, *, threshold: float, date_order: str | None = None
    ) -> ParseOutcome:
        outcome = ParseOutcome()
        lines = [line for line in view.lines if line.page <= MAX_RECEIPT_PAGES]
        receipt = read_receipt(lines, threshold=threshold)
        if receipt is None:
            outcome.warnings.append("no_receipt_total")
        outcome.receipt = receipt
        return outcome


# -- mail bodies ----------------------------------------------------------------------


def view_lines_from_text(text: str, *, limit: int = MAX_RECEIPT_LINES) -> list[ViewLine]:
    """Plain text (a mail body) as parser lines: cells split on tabs or wide gaps."""
    out: list[ViewLine] = []
    for raw in text.splitlines():
        if len(out) >= limit:
            break
        line = " ".join(raw[:MAX_LINE].split("\u00a0"))
        if not line.strip():
            continue
        parts = [p.strip() for p in re.split(r"\t| {2,}", line) if p.strip()]
        cells = tuple(
            Cell(text=part, x0=float(i), y0=float(len(out)), x1=float(i) + 0.5, y1=len(out) + 1.0)
            for i, part in enumerate(parts)
        )
        out.append(
            ViewLine(
                page=1,
                index=len(out),
                cells=cells,
                text=" ".join(parts)[:MAX_LINE],
                height=1.0,
            )
        )
    return out


def _sender_name(value: str | None) -> str | None:
    if not value:
        return None
    match = re.match(r'^\s*"?([^"<]{2,200}?)"?\s*<', value)
    if match is None:
        return None
    name = " ".join(match[1].split())
    return name if sum(ch.isalpha() for ch in name) >= 3 else None


def parse_email_receipt(result: ExtractResult, *, threshold: float) -> Receipt | None:
    """The receipt in a mail's text body (``result.email``), if it reads as one."""
    email = result.email
    if email is None or not email.body_text.strip():
        return None
    lines = view_lines_from_text(email.body_text)
    if _scores(lines) < EMAIL_MIN_SCORE:
        return None
    sent: date | None = None
    if email.headers.date:
        try:
            sent = parsedate_to_datetime(email.headers.date).date()
        except (TypeError, ValueError, IndexError):
            sent = None
    return read_receipt(
        lines,
        threshold=threshold,
        source="email_body",
        sender=_sender_name(email.headers.from_addr),
        sent=sent,
    )

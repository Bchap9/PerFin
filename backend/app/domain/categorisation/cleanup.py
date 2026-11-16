# SPDX-License-Identifier: AGPL-3.0-or-later
"""Payee clean-up: from the text a bank printed to who was paid.

``clean(raw)`` gives the **core** (what is left of a descriptor once the
bank's and the processor's additions are gone, in merchant-key form), the
descriptor's **kind** (a purchase, a transfer, a card payment...), the
processor it came through (``via``) and any signals read on the way. Pure
functions: no database, no I/O; the raw descriptor is never changed.

The steps, in order (design note, section 3):

0. :func:`canonical`: NFKC, upper case, no control or format characters,
   single spaces, at most ``PAYEE_MAX`` characters.
1. A labelled ACH line (``ORIG CO NAME:``, ``DES:``) is taken apart first:
   the company is kept, the company's entry description becomes the signal
   ``descr:<text>``, and the individual's name and every id are dropped.
2. The bank's own wording gives the **kind**: :data:`KIND_LINES` (lines a
   bank writes about itself: a fee, interest, a refund, a card or loan
   payment) and then :data:`KIND_PREFIXES` (what a bank puts in front of a
   merchant). The matched wording leaves the text, except for a
   person-to-person service, which is the only name such a line may keep.
3. Bank trailers go: card suffixes, references, dates and times, a final
   country, and the ids a bank prints after an ACH entry.

What is left is folded to lower-case ``a-z0-9`` tokens. It still holds
processor prefixes (``SQ *``), places and store numbers, and for a payment
between people the other person's name: the second half of clean-up removes
those.

Every pattern is a module constant, anchored or guarded so that it cannot
backtrack over the line, and none is built from data.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

from app.domain.transactions.models import PAYEE_MAX

#: Bumped when clean-up changes what it returns; a household whose rows were
#: keyed with an older version is refreshed.
CLEANER_VERSION: Final = 1

DescriptorKind = Literal[
    "purchase",
    "recurring_purchase",
    "ach_debit",
    "ach_credit",
    "payroll",
    "transfer",
    "card_payment",
    "loan_payment",
    "cash",
    "check",
    "fee",
    "interest",
    "refund",
    "p2p",
    "deposit",
    "unknown",
]

#: Kinds a correction is remembered for and a merchant rule may be made from.
TEACHABLE: Final[frozenset[DescriptorKind]] = frozenset(
    {"purchase", "recurring_purchase", "ach_debit", "ach_credit", "payroll", "refund", "unknown"}
)

_KEPT: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
#: Unicode categories dropped by :func:`canonical`: control and format characters.
_UNPRINTED: Final = frozenset({"Cc", "Cf"})

#: Nothing that could continue a word follows.
_END: Final = r"(?![A-Z0-9])"
#: Nothing that could begin a word precedes.
_START: Final = r"(?<![A-Z0-9])"

KindPattern = tuple[re.Pattern[str], DescriptorKind, str | None]


def _p(pattern: str, kind: DescriptorKind, signal: str | None = None) -> KindPattern:
    return (re.compile(pattern), kind, signal)


#: Words a bank builds the name of a fee from. A fee line is made of these
#: only, so a shop's own "membership fee" is not read as a bank fee.
_FEE_WORDS: Final = (
    "MONTHLY|ANNUAL|MAINTENANCE|SERVICE|OVERDRAFT|ITEM|NON-[A-Z]+|ATM|FOREIGN|INTERNATIONAL"
    "|DOMESTIC|TRANSACTION|WIRE|TRANSFER|LATE|PAYMENT|MEMBERSHIP|RETURNED|NSF|PAPER|STATEMENT"
    "|CASH|ADVANCE|ACCOUNT|INSUFFICIENT|FUNDS|STOP|BALANCE|MINIMUM|OUTGOING|INCOMING|PROTECTION"
)

#: Lines a bank writes about itself, read before the prefixes because they
#: begin with the same words ("POS REFUND", "TRANSFER TO LOAN", "ONLINE
#: PAYMENT, THANK YOU"). Matched from the start of the line; first match wins.
KIND_LINES: Final[tuple[KindPattern, ...]] = (
    _p(
        r"(?:ONLINE )?(?:(?:AUTO|CAR|HOME EQUITY|STUDENT|PERSONAL|MORTGAGE) )?"
        rf"LOAN (?:PAYMENT|PMT){_END}",
        "loan_payment",
    ),
    _p(rf"MORTGAGE (?:PAYMENT|PMT){_END}", "loan_payment"),
    _p(rf"(?:TRANSFER|PAYMENT) TO LOAN{_END}", "loan_payment"),
    _p(
        r"(?:(?:ONLINE|MOBILE|AUTOPAY|AUTOMATIC) )?PAYMENT(?: RECEIVED)?(?: ?[-,] ?| )"
        rf"THANK YOU{_END}(?: ?- ?(?:WEB|MOBILE|ONLINE){_END})?",
        "card_payment",
    ),
    _p(r"AUTOPAY(?: PAYMENT)?$", "card_payment"),
    _p(
        r"(?:(?:ONLINE|MOBILE) )?(?:BANKING )?(?:PAYMENT|PMT) TO (?:[A-Z]+ ){0,2}"
        rf"(?:CARD|CRD){_END}",
        "card_payment",
    ),
    _p(
        r"(?:DISCOVER|AMEX|AMERICAN EXPRESS|CAPITAL ONE|CHASE|CITI|BARCLAYCARD|SYNCHRONY)"
        r"(?: CARD| CREDIT CARD| CREDIT CRD)? "
        rf"(?:E-?PAYMENT|EPAY|CRCARDPMT|CARD PAYMENT|CARD PMT|AUTOPAY|ACH PMT){_END}",
        "card_payment",
    ),
    _p(rf"(?:(?:{_FEE_WORDS}) ){{1,5}}FEES?(?:$|(?=[ -]))", "fee"),
    _p(rf"(?:MONTHLY )?SERVICE CHARGE{_END}", "fee"),
    _p(
        r"(?:(?:MONTHLY|MINIMUM|PURCHASE|PURCHASES|CASH ADVANCE) )?"
        rf"INTEREST (?:PAYMENT|PAID|EARNED|CREDIT|CHARGED?){_END}",
        "interest",
    ),
    _p(r"INTEREST$", "interest"),
    _p(
        r"(?:(?:POS|DEBIT CARD|MERCHANT|PURCHASE|MERCHANDISE) )?(?:REFUND|RETURN)"
        rf"(?: CREDIT| AUTHORIZED ON)?{_END}",
        "refund",
    ),
    _p(rf"CREDIT VOUCHER{_END}", "refund"),
)

#: What a bank puts in front of whoever was paid, in the order of the market
#: notes' step 1. Matched from the start of the line; first match wins.
KIND_PREFIXES: Final[tuple[KindPattern, ...]] = (
    # Card purchases.
    _p(
        rf"(?:POS|POINT OF SALE) (?:DEBIT|PURCHASE|PUR|WITHDRAWAL|SIGNATURE){_END}"
        r"(?: - VISA CHECK CARD \d{4} -| TERMINAL \d{1,12}| [A-Z]\d{3} DATE \d\d-\d\d)?",
        "purchase",
    ),
    _p(
        r"(?:DEBIT CARD|DBT CRD|DEBIT CRD|CHECK ?CARD|CHKCARD|VISA(?: DDA)?|MC|CARD) "
        rf"(?:PURCHASE|DEBIT|PUR){_END}",
        "purchase",
    ),
    _p(r"(?:CHECK ?CARD|PURCHASE) \d{4} ", "purchase"),
    _p(rf"PURCHASE AUTHORIZED ON \d\d/\d\d{_END}", "purchase"),
    # The bank says the merchant bills on a schedule.
    _p(rf"RECURRING PAYMENT AUTHORIZED ON \d\d/\d\d{_END}", "recurring_purchase", "recurring"),
    _p(
        r"(?:RECURRING|RECUR) (?:DEBIT CARD|CHECK ?CARD|CARD|PURCHASE|PMT|PAYMENT)"
        rf"(?: PURCHASE)?{_END}",
        "recurring_purchase",
        "recurring",
    ),
    # ACH and pre-authorised entries.
    _p(
        r"(?:ACH|ELECTRONIC|EXTERNAL|PREAUTHORIZED|PRE-AUTHORIZED|PRE-AUTH|DIRECT) "
        rf"(?:DEBIT|WITHDRAWAL|PMT|PAYMENT){_END}",
        "ach_debit",
    ),
    _p(
        r"(?:ACH|ELECTRONIC|EXTERNAL|PREAUTHORIZED|PRE-AUTHORIZED|PRE-AUTH) "
        rf"(?:CREDIT|DEPOSIT){_END}",
        "ach_credit",
    ),
    # Online and mobile payment (bill pay).
    _p(
        r"(?:ONLINE|MOBILE|INTERNET|WEB) (?:BANKING )?(?:BILL )?(?:PAYMENT|PMT|PAY)"
        rf"(?: TO)?{_END}",
        "ach_debit",
    ),
    _p(rf"BILL ?PAY(?:MENT)?(?: TO)?{_END}", "ach_debit"),
    # Between a person's own accounts.
    _p(
        r"(?:(?:ONLINE|MOBILE|INTERNET|FUNDS|WITHDRAWAL|DEPOSIT|RECURRING|EXTERNAL|AUTOMATIC"
        r"|SCHEDULED|ACCOUNT) )?(?:BANKING )?TRANSFER(?: REF ?# ?[A-Z0-9]{1,24})? "
        rf"(?:TO|FROM){_END}",
        "transfer",
    ),
    # Between people: the service is the only name the line may keep.
    _p(rf"ZELLE{_END}", "p2p"),
    _p(rf"VENMO{_END}", "p2p"),
    _p(rf"CASH APP{_END}", "p2p"),
    # Cash.
    _p(rf"(?:NON-[A-Z]+ )?ATM (?:CASH )?(?:WITHDRAWAL|WITHDRAW|WITHDRWL|W/D){_END}", "cash"),
    _p(rf"(?:[A-Z]+ )?ATM \d\d/\d\d #?\d{{1,12}} (?:WITHDRAWAL|WITHDRWL){_END}", "cash"),
    _p(rf"CASH E?WITHDRAWAL{_END}", "cash"),
    # Deposits and cheques.
    _p(rf"(?:MOBILE|REMOTE ONLINE|ATM|BRANCH)(?: CHECK)? DEPOSIT{_END}", "deposit"),
    _p(rf"E-?DEPOSIT{_END}", "deposit"),
    _p(rf"COUNTER CREDIT{_END}", "deposit"),
    _p(r"DEPOSIT(?:$| ID NUMBER(?![A-Z])| ?#)", "deposit"),
    _p(r"(?:CHECK|CHK|DRAFT)(?: PAID| NUMBER| NO)? ?#? ?\d{1,12}(?: PAID)?$", "check"),
    # Payroll. "DIRECT DEPOSIT" alone says only that money arrived (a tax
    # refund comes the same way); the word PAYROLL makes it pay (_PAYROLL_WORD).
    _p(rf"PAYROLL(?: DEPOSIT)?{_END}", "payroll"),
    _p(rf"(?:DIRECT|DIR) DEP(?:OSIT)?{_END}", "ach_credit"),
)

#: Kinds whose matched wording stays in the text: it names the service.
_KEEPS_WORDING: Final[frozenset[DescriptorKind]] = frozenset({"p2p"})

_PAYROLL_WORD: Final = re.compile(rf"{_START}PAYROLL{_END}")
#: Kinds the word PAYROLL may still turn into payroll.
_PAYROLL_FROM: Final[frozenset[DescriptorKind]] = frozenset({"unknown", "ach_credit"})
_RECURRING_TAIL: Final = re.compile(r" RECURRING$")

# --- labelled ACH ----------------------------------------------------------

#: The field labels of the two labelled layouts, longest first.
_ACH_LABEL: Final = re.compile(
    rf"{_START}(ORIG CO NAME|CO ENTRY DESCR|DESC DATE|ORIG ID|IND NAME|IND ID|TRACE ?#"
    r"|CO ID|INDN|DES|SEC|EED|TRN|ID) ?: ?"
)
_DESCR_MAX: Final = 16
_DESCR_PAYROLL: Final = re.compile(
    r"(?:^| )(?:PAYROLL|PAYRL|SALARY|PAYCHECK)(?: |$)|^DIR(?:ECT)? DEP"
)
_DESCR_LOAN: Final = re.compile(r"(?:^| )(?:LOAN|LN|MORTGAGE|MTG)(?: |$)")
_DESCR_CARD: Final = "CRCARDPMT"
_DESCR_PAYMENT: Final = frozenset(
    {
        "AUTOPAY",
        "AUTO PAY",
        "AUTO PMT",
        "PAYMENT",
        "PMT",
        "ACH PMT",
        "EPAY",
        "EPAYMENT",
        "E-PAYMENT",
        "ONLINE PMT",
        "CARD PMT",
        _DESCR_CARD,
    }
)
#: A company name that says it is a card account ("CHASE CREDIT CRD").
_CARD_ACCOUNT: Final = re.compile(r"(?:[A-Z]+ ){0,3}(?:CARD|CRD)(?: ONLINE)?$")
#: Companies whose ACH "payment" entry is a card payment whatever the name adds.
_CARD_ISSUERS: Final = frozenset(
    {"AMERICAN EXPRESS", "AMEX", "DISCOVER", "CAPITAL ONE", "BARCLAYCARD", "SYNCHRONY BANK"}
)
#: Kinds a labelled line's description may still refine.
_ACH_OPEN: Final[frozenset[DescriptorKind]] = frozenset({"unknown", "ach_debit", "ach_credit"})

# --- trailers ----------------------------------------------------------------

#: What a bank adds after the merchant; each match becomes a space.
_TRAILERS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(rf"{_START}CARD (?:ENDING IN )?\d{{4}}{_END}"),
    re.compile(rf"{_START}X{{4,}}\d{{4}}{_END}"),
    # A reference of one letter and 15 digits.
    re.compile(rf"{_START}[A-Z]\d{{15}}{_END}"),
    re.compile(r"(?<![A-Z0-9/])\d\d/\d\d(?:/\d\d(?:\d\d)?)?(?![A-Z0-9/])"),
    re.compile(r"(?<![A-Z0-9:])\d\d:\d\d(?::\d\d)?(?![A-Z0-9:])"),
    # The ids a bank prints after an ACH entry that has no labelled layout.
    re.compile(rf"{_START}(?:PPD|WEB|CCD|TEL) ID ?: ?[A-Z0-9]+"),
    re.compile(
        rf"{_START}(?:TRACE|REF|CONF|CONFIRMATION|AUTH)"
        r"(?: ?# ?:? ?[A-Z0-9]+| (?=[A-Z0-9]*\d)[A-Z0-9]+)"
    ),
)
_COUNTRIES: Final = frozenset({"US", "USA"})


@dataclass(frozen=True)
class Cleaned:
    """What clean-up read from one descriptor (module doc)."""

    #: Lower-case ``a-z0-9`` tokens joined by single spaces; may be empty.
    core: str
    kind: DescriptorKind = "unknown"
    #: The processor or marketplace the charge came through, when one was read.
    via: str | None = None
    signals: frozenset[str] = frozenset()


def canonical(raw: str | None) -> str:
    """``raw`` as clean-up reads it: NFKC, upper case, printable, single spaces, bounded."""
    if not raw:
        return ""
    text = unicodedata.normalize("NFKC", raw[:PAYEE_MAX]).upper()
    printed = "".join(
        " " if ch.isspace() else ch
        for ch in text
        if ch.isspace() or unicodedata.category(ch) not in _UNPRINTED
    )
    return " ".join(printed.split())[:PAYEE_MAX].strip()


def _fold(text: str) -> str:
    """Lower-case ``a-z0-9`` tokens joined by single spaces."""
    folded = "".join(ch if ch in _KEPT else " " for ch in text.lower())
    return " ".join(folded.split())


def _labelled(text: str) -> tuple[str, str] | None:
    """The company and the entry description of a labelled ACH line, as printed.

    Two layouts: ``ORIG CO NAME:<name> ... CO ENTRY DESCR:<descr> ... IND
    NAME:<person>`` and ``<name> DES:<descr> ID:<id> INDN:<person> CO ID:<id>``.
    The company comes back with any bank wording printed before the first
    label; the individual's name and every id are dropped.
    """
    marks = list(_ACH_LABEL.finditer(text))
    fields: dict[str, str] = {}
    starts: dict[str, int] = {}
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        if mark.group(1) not in fields:
            fields[mark.group(1)] = text[mark.end() : end].strip()
            starts[mark.group(1)] = mark.start()
    if "ORIG CO NAME" in fields:
        name = f"{text[: marks[0].start()]} {fields['ORIG CO NAME']}"
        return " ".join(name.split()), fields.get("CO ENTRY DESCR", "")
    if "DES" in fields:
        return text[: starts["DES"]].strip(), fields["DES"]
    return None


def _ach(text: str) -> tuple[str, frozenset[str]] | None:
    """The company of a labelled ACH line and its ``descr:`` signal, else ``None``."""
    labelled = _labelled(text)
    if labelled is None:
        return None
    name, descr = labelled
    described = _fold(descr)[:_DESCR_MAX].strip()
    return name, frozenset({f"descr:{described}"}) if described else frozenset()


def _kind(text: str) -> tuple[DescriptorKind, str | None, str]:
    """The kind the bank's wording gives, its signal, and the text without that wording."""
    for pattern, kind, signal in (*KIND_LINES, *KIND_PREFIXES):
        found = pattern.match(text)
        if found is not None:
            rest = text if kind in _KEEPS_WORDING else text[found.end() :]
            return kind, signal, rest.strip()
    return "unknown", None, text


def _ach_kind(kind: DescriptorKind, company: str, descr: str) -> DescriptorKind:
    """What a labelled line is, from its description and who the company is."""
    if _DESCR_PAYROLL.search(descr):
        return "payroll"
    if kind not in _ACH_OPEN:
        return kind
    if descr == _DESCR_CARD:
        return "card_payment"
    if descr in _DESCR_PAYMENT and (company in _CARD_ISSUERS or _CARD_ACCOUNT.match(company)):
        return "card_payment"
    if _DESCR_LOAN.search(descr):
        return "loan_payment"
    return "ach_debit" if kind == "unknown" else kind


def _strip_trailers(text: str) -> str:
    """``text`` without what the bank added after the merchant."""
    for pattern in _TRAILERS:
        text = pattern.sub(" ", text)
    tokens = text.split()
    # A final country, unless it is part of a name ("... R US") or all there is.
    if len(tokens) >= 2 and tokens[-1] in _COUNTRIES and len(tokens[-2]) > 1:
        tokens.pop()
    return " ".join(tokens)


def clean(raw: str | None) -> Cleaned:
    """The descriptor's core, kind, processor and signals (module doc)."""
    text = canonical(raw)
    if not text:
        return Cleaned(core="")
    signals: set[str] = set()
    labelled = _labelled(text)
    descr = ""
    if labelled is not None:
        _, described = _ach(text) or ("", frozenset())
        text, descr = labelled
        signals |= described
    kind, signal, rest = _kind(text)
    if labelled is not None:
        kind = _ach_kind(kind, rest, descr)
    if kind in _PAYROLL_FROM and _PAYROLL_WORD.search(rest):
        kind = "payroll"
        rest = _PAYROLL_WORD.sub(" ", rest)
    if kind == "purchase" and _RECURRING_TAIL.search(rest):
        kind, signal = "recurring_purchase", "recurring"
        rest = _RECURRING_TAIL.sub("", rest)
    if signal is not None:
        signals.add(signal)
    return Cleaned(core=_fold(_strip_trailers(rest)), kind=kind, signals=frozenset(signals))

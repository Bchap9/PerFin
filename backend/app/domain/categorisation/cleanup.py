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
   country, and the ids a bank prints after an ACH entry. A number between
   the bank's wording and the merchant goes when it cannot begin a name
   (:data:`_LEADING_NUMBER`). What is left is read once more for a
   person-to-person service (:data:`_P2P`): behind ``POS DEBIT`` or ``ACH
   DEBIT`` the line is as much a payment between people as with nothing in
   front of it. A line that speaks of a student loan (:data:`_STUDENT_LOAN`)
   is a loan payment to whoever it names.
4. A processor in front of the merchant (:data:`PROCESSORS`: ``SQ *``,
   ``TST*``, ``PAYPAL *``...) leaves the text and is recorded as ``via``. A
   marketplace or platform (``APPLE.COM/BILL``, ``GOOGLE *``, ``AMZN``,
   ``UBER *``) is not a processor: it is who was paid.
5. Telephone numbers, a ZIP, and a final state with the city in front of it.
6. Store, terminal and order numbers, also one printed onto the end of a
   word ("PETRO4333"). A "+" at the end of a name is the word it is read as
   ("PARAMOUNT+" is "paramount plus").
7. Web endings, possessives and company suffixes.

What is left is the core: lower-case ``a-z0-9`` tokens, at most
:data:`CORE_TOKENS_MAX` of them and :data:`CORE_MAX` characters. A payment
between people keeps the name of the service and nothing else, so that the
other person never reaches a merchant key; a line that names no merchant at
all (a transfer, a card payment, cash, a cheque, a fee, interest, a deposit)
has no core.

A place is recognised by its position, not from a list of towns: a state
code ends the line, and the city is what stands between a store number and
that state. Without a number one word is taken for the city, and a second
when the pair is shaped like a town's name ("NEW ...", "... FALLS"), as long
as two words of the name are left. A wrong guess costs a word of the name on
every line of that shop alike, which a merchant key survives.

Where that rule keeps a word that may as well be the city ("JUNEBUG ASHGROVE
OH": a shop of one word in Ashgrove, or the shop "Junebug Ashgrove"?), the
core keeps it and ``shorter`` is the core without it. A card line prints a
city in front of the state more often than nothing, but the line cannot say
which it is, and shops that share a first word must not become one merchant:
the key is made of the core, and the shorter reading is only a second name
under which the engine looks up what the household taught
(``engine.learned_for``), so that what was filed for "SQ *JUNEBUG" answers
for this line too.

Every pattern is a module constant, anchored or guarded so that it cannot
backtrack over the line, and none is built from data.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

from app.domain.transactions.models import PAYEE_MAX

#: Bumped when clean-up changes what it returns, or the merchant key the
#: engine makes of it (3: no key of fewer than three letters and digits, nor
#: of bare cash wording; the CHEQUE and CHQ spellings are a cheque; 4: a
#: credit union's WITHDRAWAL in front of ACH, a card, bill pay, a transfer or
#: a cheque, and no key of a teller's cash wording, ``engine.CASH_FILLER_WORDS``);
#: a household whose rows were keyed with an older version is refreshed.
CLEANER_VERSION: Final = 4

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

#: The most tokens and characters a core holds (a merchant key is no longer).
CORE_TOKENS_MAX: Final = 6
CORE_MAX: Final = 64

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
    "|BANK|CHECK|OVERLIMIT|OVER-LIMIT|OVER|LIMIT"
)

#: The reference some banks print between "PAYMENT" and "TO".
_REFERENCE_TO: Final = r"(?: #?\d{4,17}(?= TO ))?"

#: What a bank puts in front of TRANSFER; by itself none makes a line a transfer.
_TRANSFER_LEAD: Final = (
    "ONLINE|MOBILE|INTERNET|HOME(?= BANKING)|FUNDS|WITHDRAWAL|DEPOSIT|RECURRING|EXTERNAL"
    "|AUTOMATIC|SCHEDULED|ACCOUNT|ACH|WIRE|INSTANT|KEEP THE CHANGE|SAVE AS YOU GO"
    "|OVERDRAFT PROTECTION"
)
_TRANSFER_WORD: Final = r"(?:BANKING )?(?:TRANSFER|XFER)(?: (?:DEBIT|CREDIT|OUTGOING|INCOMING))?"
#: A transfer the bank calls a wire: WIRE among the words in front of TRANSFER.
#: Such a line may pay a person or a company (signal :data:`WIRE_SIGNAL`).
_WIRE: Final = re.compile(r"(?:[A-Z]+ ){0,3}WIRE (?:[A-Z]+ ){0,2}(?:TRANSFER|XFER)\b")
#: The signal of a transfer that is a wire: it need not stay in the household.
WIRE_SIGNAL: Final = "wire"
#: An account or reference number, masked or not ("4410", "#4410", "XXXX7731").
_ACCOUNT_NUMBER: Final = r"[#X*.]{0,24}\d[A-Z0-9-]{0,23}"

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
        r"(?:(?:ONLINE|MOBILE|INTERNET|ELECTRONIC|WEB|AUTOPAY|AUTOMATIC) )?"
        r"(?:PAYMENT|PYMT|PMT)(?: RECEIVED)?(?: ?[-,] ?| )"
        rf"THANK YOU{_END}(?: ?- ?(?:WEB|MOBILE|ONLINE){_END})?",
        "card_payment",
    ),
    _p(r"AUTOPAY(?: PAYMENT)?$", "card_payment"),
    _p(
        r"(?:(?:ONLINE|MOBILE|INTERNET|ELECTRONIC|WEB) )?(?:BANKING )?(?:PAYMENT|PYMT|PMT)"
        rf"{_REFERENCE_TO} TO (?:[A-Z]+ ){{0,2}}(?:CARD|CRD){_END}",
        "card_payment",
    ),
    _p(
        r"(?:DISCOVER|AMEX|AMERICAN EXPRESS|CAPITAL ONE|CHASE|CITI|BARCLAYCARD|SYNCHRONY)"
        r"(?: CARD| CREDIT CARD| CREDIT CRD)? "
        r"(?:E-?PAYMENT|EPAY|CRCARDPMT|CARD PAYMENT|CARD PMT|AUTOPAY|ACH PMT"
        rf"|(?:MOBILE|ONLINE) (?:PAYMENT|PYMT|PMT)){_END}",
        "card_payment",
    ),
    _p(rf"(?:(?:{_FEE_WORDS}) ){{1,5}}FEES?(?:$|(?=[ -]))", "fee"),
    _p(rf"(?:MONTHLY )?SERVICE CHARGE{_END}", "fee"),
    _p(
        r"(?:(?:MONTHLY|MINIMUM|PURCHASE|PURCHASES|CASH ADVANCE|SAVINGS|CHECKING) )?"
        rf"INTEREST (?:PAYMENT|PAID|EARNED|CREDIT|CHARGED?|FEE){_END}",
        "interest",
    ),
    _p(r"INTEREST$", "interest"),
    _p(
        r"(?:(?:POS|DEBIT CARD|MERCHANT|PURCHASE|MERCHANDISE) )?REFUND"
        rf"(?: CREDIT| AUTHORIZED ON)?{_END}",
        "refund",
    ),
    _p(
        r"(?:POS|DEBIT CARD|MERCHANT|PURCHASE|MERCHANDISE) RETURN"
        rf"(?: CREDIT| AUTHORIZED ON)?{_END}",
        "refund",
    ),
    # RETURN alone begins a shop's name as easily ("RETURN TO NATURE FOODS"):
    # a refund only with the bank's next word, a number, or nothing behind it.
    _p(rf"RETURN(?: CREDIT{_END}| AUTHORIZED ON{_END}|$|(?= #?\d))", "refund"),
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
        rf"{_REFERENCE_TO}(?: TO)?{_END}",
        "ach_debit",
    ),
    _p(rf"BILL ?PAY(?:MENT)?(?: TO)?{_END}", "ach_debit"),
    # Between a person's own accounts (and a wire, which names a person or a
    # company and never a merchant): TRANSFER or XFER, then TO or FROM.
    _p(
        rf"(?:(?:{_TRANSFER_LEAD}) ){{0,3}}{_TRANSFER_WORD}"
        rf"(?: REF ?# ?[A-Z0-9]{{1,24}}| {_ACCOUNT_NUMBER})? (?:TO|FROM){_END}",
        "transfer",
    ),
    # With no TO or FROM: a bank's word in front and nothing but numbers behind.
    _p(
        rf"(?:(?:{_TRANSFER_LEAD}) ){{1,3}}{_TRANSFER_WORD}(?: {_ACCOUNT_NUMBER}){{0,3}}$",
        "transfer",
    ),
    # The bank's word for a debit it was told to make; who was paid follows.
    # After the transfers: "AUTOMATIC WITHDRAWAL TRANSFER TO SAVINGS" is one.
    _p(rf"(?:AUTOMATIC|AUTO) WITHDRAWAL{_END}", "ach_debit"),
    # A credit union writes WITHDRAWAL first and the way the money went next;
    # a number behind it goes only with its "#" ("WITHDRAWAL POS 365 MARKET").
    _p(rf"WITHDRAWAL (?:ACH|BILL ?PAY(?:MENT)?)(?: #\d{{1,12}})?{_END}", "ach_debit"),
    _p(
        rf"WITHDRAWAL (?:DEBIT CARD|POS)(?: PURCHASE| PUR| DEBIT)?(?: #\d{{1,12}})?{_END}",
        "purchase",
    ),
    # Between people: the service is the only name the line may keep (:data:`_P2P`).
    _p(rf"ZELLE{_END}", "p2p"),
    _p(rf"VENMO{_END}", "p2p"),
    _p(rf"CASH APP{_END}", "p2p"),
    _p(r"SQC ?\*", "p2p"),
    # Cash.
    _p(rf"(?:NON-[A-Z]+ )?ATM (?:CASH )?(?:WITHDRAWAL|WITHDRAW|WITHDRWL|W/D){_END}", "cash"),
    _p(rf"(?:[A-Z]+ )?ATM \d\d/\d\d #?\d{{1,12}} (?:WITHDRAWAL|WITHDRWL){_END}", "cash"),
    _p(rf"CASH E?WITHDRAWAL{_END}", "cash"),
    # Deposits and cheques.
    _p(rf"(?:MOBILE|REMOTE ONLINE|ATM|BRANCH)(?: CHECK)? DEPOSIT{_END}", "deposit"),
    _p(rf"E-?DEPOSIT{_END}", "deposit"),
    _p(rf"COUNTER CREDIT{_END}", "deposit"),
    _p(r"DEPOSIT(?:$| ID NUMBER(?![A-Z])| ?#)", "deposit"),
    # The CHEQUE and CHQ spellings too, a teller's cheque, and the word alone.
    _p(
        r"(?:(?:TELLER |COUNTER |OFFICIAL )?(?:CHECK|CHEQUE)|CHK|CHQ|DRAFT)"
        r"(?: PAID| NUMBER| NO)? ?#? ?\d{1,12}(?: PAID)?$",
        "check",
    ),
    _p(rf"(?:CHECK|CHK|CHEQUE|CHQ) (?:WITHDRAWAL|WITHDRWL|W/D){_END}", "check"),
    _p(r"(?:CHECK|CHK|CHEQUE|CHQ)$", "check"),
    # A credit union's cheque is a share draft.
    _p(
        r"(?:WITHDRAWAL BY (?:CHECK|CHK|CHEQUE|CHQ)|SHARE DRAFT(?: WITHDRAWAL)?)"
        r"(?: ?#? ?\d{1,12})?$",
        "check",
    ),
    # Payroll. "DIRECT DEPOSIT" alone says only that money arrived (a tax
    # refund comes the same way); the word PAYROLL makes it pay (_PAYROLL_WORD).
    _p(rf"PAYROLL(?: DEPOSIT)?{_END}", "payroll"),
    _p(rf"(?:DIRECT|DIR) DEP(?:OSIT)?{_END}", "ach_credit"),
)

#: Kinds whose matched wording stays in the text: it names the service.
_KEEPS_WORDING: Final[frozenset[DescriptorKind]] = frozenset({"p2p"})

#: A person-to-person service where the merchant would stand: at the start,
#: or behind the bank's wording, a number and a few more of the bank's words.
#: ``SQC*`` is how Square prints Cash App. The group is the service.
_P2P: Final = re.compile(
    r"(?:\d{3,17} )?"
    r"(?:(?:ONLINE|MOBILE|INSTANT|EXTERNAL|WITHDRAWAL|DEPOSIT|PAYMENT|DEBIT|CREDIT|TRANSFER) )"
    rf"{{0,3}}(?:(ZELLE|VENMO|CASH APP){_END}|SQC ?\*)"
)
_SQUARE_CASH: Final = "CASH APP"

_PAYROLL_WORD: Final = re.compile(rf"{_START}PAYROLL{_END}")
#: Kinds the word PAYROLL may still turn into payroll.
_PAYROLL_FROM: Final[frozenset[DescriptorKind]] = frozenset({"unknown", "ach_credit"})
#: With no wording from the bank the word counts only where a company's
#: entry ends ("MAPLE STUDIO PAYROLL"), not inside a product's name
#: ("QUICKBOOKS PAYROLL 800-555-0142").
_PAYROLL_LAST: Final = re.compile(rf"{_START}PAYROLL$")
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
_DESCR_LOAN: Final = re.compile(
    r"(?:^| )(?:(?:STUDENT|STUDNT|STDNT) ?)?(?:LOAN|LN|MORTGAGE|MTG)(?: |$)"
)
#: "STUDENT LOAN" as a servicer's ACH entry prints it ("STDNT LOAN", "STUDNTLOAN"),
#: on a line without labels.
_STUDENT_LOAN: Final = re.compile(rf"{_START}(?:STUDENT|STUDNT|STDNT) ?(?:LOAN|LN)S?{_END}")
#: The entry's own "payment" on such a line, which is nobody's name.
_LOAN_PAYMENT_WORD: Final = re.compile(rf"{_START}(?:PMT|PYMT|PAYMT|PAYMENT){_END}")
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
    re.compile(rf"{_START}X{{4}}(?: X{{4}}){{0,2}} \d{{4}}{_END}"),
    re.compile(rf"{_START}CKCD \d{{4}}{_END}"),
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
#: The terminal or sequence number some banks print between their own wording
#: and the merchant ("POS DEBIT 0042 ...", "VISA DDA PUR 448120 ..."). Only a
#: number that cannot begin a name: it starts with a zero or has six digits
#: or more ("365 MARKET", "1800 FLOWERS" and "5280 BURGER BAR" are names).
_LEADING_NUMBER: Final = re.compile(r"(?:0\d{2,}|\d{6,}) (?=\S)")

# --- processors ----------------------------------------------------------------

#: A payment processor's literal prefix and the ``via`` it gives. The first
#: prefix the text starts with wins, so a longer prefix stands before any
#: shorter one it begins with. Each is told from a name by its ``*`` (or its
#: dot), except the bare ``SP `` (:data:`_BARE`).
PROCESSORS: Final[tuple[tuple[str, str], ...]] = (
    ("PADDLE.NET*", "paddle"),
    ("DOORDASH*", "doordash"),
    ("GRUBHUB*", "grubhub"),
    ("GOSQ.COM", "square"),
    ("SQ *SQ *", "square"),
    ("PAYPAL *", "paypal"),
    ("TST *", "toast"),
    ("TST*", "toast"),
    ("DRI*", "digitalriver"),
    ("WPY*", "wepay"),
    ("ACT*", "active"),
    ("ABC*", "abc"),
    ("ACI*", "aci"),
    ("SQ *", "square"),
    ("DD *", "doordash"),
    ("GH *", "grubhub"),
    ("FS *", "fastspring"),
    ("IN *", "intuit"),
    ("EB *", "eventbrite"),
    ("SP *", "shopify"),
    ("IC*", "instacart"),
    ("PP*", "paypal"),
    ("BT*", "braintree"),
    ("SP ", "shopify"),
)
#: Prefixes with nothing but a space to tell them from a name: read as a
#: processor when two words follow, or one word of at least
#: :data:`_BARE_WORD_MIN` letters ("SP COPPERLINE"; not "SP 500" or "SP AG").
_BARE: Final = frozenset({"SP "})
_BARE_WORD_MIN: Final = 4
#: First words that make such a prefix part of a company's own name ("SP PLUS",
#: "SP GLOBAL"). Not a complete list: any other single word of four letters
#: or more behind a bare "SP " is read as a Shopify shop.
_NAME_AFTER: Final = frozenset({"PLUS", "GLOBAL", "GROUP", "PARKING"})

# --- places and numbers --------------------------------------------------------

#: Stands where a telephone, store or order number stood, until the place is
#: read: a number in front of "<city> <state>" says where the city begins.
#: :func:`canonical` removes control characters, so no descriptor holds it
#: (and ``str.split`` does not take a NUL for white space).
_MARK: Final = "\x00"
_MARKED: Final = f" {_MARK} "

_PHONE: Final = re.compile(
    rf"{_START}(?:1[-. ])?(?:\(\d{{3}}\) ?|\d{{3}}[-.])\d{{3}}[-.]\d{{4}}{_END}"
    rf"|{_START}1?8(?:00|33|44|55|66|77|88)\d{{7}}{_END}"
    rf"|{_START}\d{{3}}-\d{{4}}{_END}"
)
#: "#123", "# 123", "#R7731": never at the very start ("#1 NOODLE HOUSE").
_HASH_NUMBER: Final = re.compile(r"(?<=.)# ?[A-Z]?\d[A-Z0-9-]*")
_STORE_NUMBER: Final = re.compile(rf"(?<=[A-Z0-9] )(?:STORE|STR) (?:- )?#?\d{{1,12}}{_END}")
#: What follows a "*": an order number when it holds a digit or, as one in
#: some twenty does, no digit and no vowel either (:func:`_star_code`).
_STAR_CODE: Final = re.compile(r"\* ?([A-Z0-9]{6,})")
_VOWELS: Final = frozenset("AEIOU")
#: A ZIP or a longer reference at the end of the line.
_LONG_NUMBER: Final = re.compile(r"\d{5,}|\d{5}-\d{4}")
#: A store or terminal code: "F12345", "T-1234".
_STORE_CODE: Final = re.compile(r"[A-Z]\d{5,}|[A-Z]-\d{3,}")
#: A store or ticket number printed onto the end of a word ("PETRO4333",
#: "H&M09086", "AIR001282775083"): four digits or more behind two letters.
#: "STUDIO54" and "FOREVER21" are names.
_GLUED_NUMBER: Final = re.compile(rf"(?<=[A-Z&][A-Z])\d{{4,}}{_END}")
#: "PARAMOUNT+", "DISNEY+": the sign is part of the name and is read "plus".
_PLUS_SIGN: Final = re.compile(rf"(?<=[A-Z])\+{_END}")
_NUMBER_DIGITS_MIN: Final = 3
#: An order number printed without a "*": this long, with this many digits
#: and changes between letters and digits.
_CODE_MIN: Final = 6
_CODE_DIGITS_MIN: Final = 2
_CODE_TURNS_MIN: Final = 2

# fmt: off
_STATES: Final = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT",
    "VA", "WA", "WV", "WI", "WY", "DC", "PR",
})
# fmt: on
#: "CO" at the end is a company far more often than Colorado: it is read as
#: the state only behind a number or a web address.
_STATE_NEEDS_NUMBER: Final = frozenset({"CO"})
#: Words "IN" completes into a name ("DRIVE IN"), so that it is not Indiana.
_BEFORE_IN: Final = frozenset({"DRIVE", "DINE", "CHECK", "WALK", "STOP", "POP", "DROP", "TUNE"})
#: The most words of a city between a store number and the state.
_CITY_TOKENS_MAX: Final = 3
#: A name keeps at least this many tokens when a city is only guessed at.
_NAME_TOKENS_MIN: Final = 2
# fmt: off
#: First words of two-word towns.
_CITY_FIRST: Final = frozenset({
    "NORTH", "SOUTH", "EAST", "WEST", "N", "S", "E", "W", "N.", "S.", "E.", "W.", "NEW", "OLD",
    "SAN", "SANTA", "SAINT", "ST", "ST.", "FORT", "FT", "FT.", "MOUNT", "MT", "MT.", "LAKE",
    "PORT", "POINT", "EL", "LA", "LAS", "LOS", "DES", "DEL", "RIO", "GRAND", "UPPER", "LOWER",
    "LITTLE", "TWIN", "PALM", "CAPE", "GLEN",
})
#: Last words of two-word towns.
_CITY_LAST: Final = frozenset({
    "CITY", "FALLS", "SPRINGS", "HEIGHTS", "BEACH", "PARK", "VALLEY", "HILLS", "HILL", "GROVE",
    "RAPIDS", "CREEK", "HARBOR", "ISLAND", "JUNCTION", "RIDGE", "HOLLOW", "BLUFF", "BLUFFS",
    "POINT", "GLEN", "LAKE", "LAKES", "FOREST", "HAVEN", "BAY", "VISTA", "MESA", "ROCK", "FORK",
    "FORKS", "PLAINS", "PRAIRIE", "COVE", "SHORES", "TOWNSHIP", "TWP",
})
# fmt: on
_WORD_MARKS: Final = frozenset(".'-&")

# --- the name ------------------------------------------------------------------

#: Endings a token "NAME.COM" loses; "NAME.COM/BILL" is left as it is.
_WEB_ENDINGS: Final = (".COM", ".NET", ".ORG", ".IO", ".CO", ".TV", ".US")
_WEB_PREFIX: Final = "WWW."
_POSSESSIVE: Final = re.compile(rf"(?<=[A-Z])['\u2019`]S{_END}")
#: What is left of a possessive printed apart ("TRADER JOE S").
_APART: Final = "s"
_COMPANY_SUFFIXES: Final = frozenset({"inc", "llc", "corp", "co", "ltd", "llp"})
#: Kinds that name no merchant: the core is empty.
_NO_CORE: Final[frozenset[DescriptorKind]] = frozenset(
    {"transfer", "card_payment", "cash", "check", "fee", "interest", "deposit"}
)


@dataclass(frozen=True)
class Cleaned:
    """What clean-up read from one descriptor (module doc)."""

    #: Lower-case ``a-z0-9`` tokens joined by single spaces; may be empty.
    core: str
    kind: DescriptorKind = "unknown"
    #: The processor or marketplace the charge came through, when one was read.
    via: str | None = None
    signals: frozenset[str] = frozenset()
    #: The core without its last word, when that word may be the city in
    #: front of a state (module doc); ``None`` when the line leaves no doubt.
    shorter: str | None = None


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
    """Lower-case ``a-z0-9`` tokens joined by single spaces; an accent leaves its letter."""
    plain = (
        ch for ch in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(ch)
    )
    folded = "".join(ch if ch in _KEPT else " " for ch in plain)
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


def _kind(text: str) -> tuple[DescriptorKind, str | None, str, str]:
    """The kind the bank's wording gives, its signal, the text without it, and the wording."""
    for pattern, kind, signal in (*KIND_LINES, *KIND_PREFIXES):
        found = pattern.match(text)
        if found is not None:
            rest = text if kind in _KEEPS_WORDING else text[found.end() :]
            return kind, signal, rest.strip(), found.group(0)
    return "unknown", None, text, ""


def _says_payroll(kind: DescriptorKind, rest: str, *, labelled: bool) -> bool:
    """Whether the word PAYROLL in ``rest`` makes the line pay (:data:`_PAYROLL_LAST`)."""
    if kind != "unknown" or labelled:
        return _PAYROLL_WORD.search(rest) is not None
    return _PAYROLL_LAST.search(_strip_trailers(rest)) is not None


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


def _processor(text: str) -> tuple[str | None, str]:
    """The processor ``text`` begins with and what stands behind it, else ``(None, text)``."""
    for prefix, via in PROCESSORS:
        if not text.startswith(prefix):
            continue
        rest = text[len(prefix) :].lstrip(" *")
        words = rest.split()
        named = sum(1 for token in words if any(ch.isalnum() for ch in token))
        if prefix in _BARE:
            if words and words[0] in _NAME_AFTER:
                return None, text
            one_word = named == 1 and words[0].isalpha() and len(words[0]) >= _BARE_WORD_MIN
            if named < _NAME_TOKENS_MIN and not one_word:
                return None, text
        if named >= 1:
            return via, rest
        # Nobody stands behind it: the line names the processor itself.
        return None, text
    return None, text


def _star_code(found: re.Match[str]) -> str:
    """A mark for an order number behind a ``*``; a word ("*MONTHLY") is left alone."""
    code = found.group(1)
    coded = any(ch.isdigit() for ch in code) or not _VOWELS.intersection(code)
    return _MARKED if coded else found.group(0)


def _is_word(token: str) -> bool:
    """Letters only (and the marks a town's name may hold): what a city is written in."""
    return any(ch.isalpha() for ch in token) and all(
        ch.isalpha() or ch in _WORD_MARKS for ch in token
    )


def _is_number(token: str) -> bool:
    """A mark, a store code, or digits (and dashes) with at least three digits."""
    if token == _MARK or _STORE_CODE.fullmatch(token):
        return True
    return (
        token[0].isdigit()
        and all(ch.isdigit() or ch == "-" for ch in token)
        and sum(ch.isdigit() for ch in token) >= _NUMBER_DIGITS_MIN
    )


def _is_code(token: str) -> bool:
    """Letters and digits mixed the way an order number is ("GSZ9B79Z"), not a name ("STUDIO54")."""
    plain = token.replace("-", "")
    if len(plain) < _CODE_MIN or not plain.isascii() or not plain.isalnum():
        return False
    digits = sum(ch.isdigit() for ch in plain)
    turns = sum(1 for a, b in itertools.pairwise(plain) if a.isdigit() != b.isdigit())
    return digits >= _CODE_DIGITS_MIN and turns >= _CODE_TURNS_MIN


def _is_web(token: str) -> bool:
    """A web address, as some merchants print where the city goes ("AMZN.COM/BILL")."""
    return any(ending in token for ending in _WEB_ENDINGS)


def _town_shaped(first: str, last: str) -> bool:
    return first in _CITY_FIRST or last in _CITY_LAST


def _strip_place(tokens: list[str]) -> bool:
    """Remove a final ZIP, state and city from ``tokens`` (module doc).

    Returns whether the last token left may be the city as well: the rule
    that two words of a name stay is all that kept it.
    """
    while len(tokens) > 1 and (tokens[-1] == _MARK or _LONG_NUMBER.fullmatch(tokens[-1])):
        tokens.pop()
    if len(tokens) < _NAME_TOKENS_MIN or tokens[-1] not in _STATES:
        return False
    state = tokens[-1]
    before = len(tokens) - 1
    # A web address where the city goes.
    if before >= _NAME_TOKENS_MIN and _is_web(tokens[-2]):
        del tokens[-2:]
        return False
    # A number says where the city begins: everything from there on is the place.
    words = 0
    while words < _CITY_TOKENS_MAX and words < before and _is_word(tokens[before - 1 - words]):
        words += 1
    if before - words >= _NAME_TOKENS_MIN and _is_number(tokens[before - 1 - words]):
        del tokens[before - words :]
        return False
    if state in _STATE_NEEDS_NUMBER or (state == "IN" and tokens[-2] in _BEFORE_IN):
        return False
    # Two letters behind a single word are as likely its name ("UNCLE AL").
    if before < _NAME_TOKENS_MIN and not _is_web(tokens[0]):
        return False
    tokens.pop()
    # No number: one word of city, two when the pair is shaped like a town's name.
    if not _is_word(tokens[-1]):
        return False
    if len(tokens) <= _NAME_TOKENS_MIN:
        # "<word> <word> <state>": the second word is the city or the name's.
        return len(tokens) == _NAME_TOKENS_MIN and _is_word(tokens[0])
    last = tokens.pop()
    if not (_is_word(tokens[-1]) and _town_shaped(tokens[-1], last)):
        return False
    if len(tokens) > _NAME_TOKENS_MIN:
        tokens.pop()
        return False
    # "<word> <town> <town> <state>": the first word of the town was kept.
    return _is_word(tokens[0])


def _strip_numbers(tokens: list[str]) -> list[str]:
    """``tokens`` without store, terminal and order numbers; the first token is the name's."""
    kept: list[str] = []
    for token in tokens:
        if token == _MARK:
            continue
        if kept and (
            _STORE_CODE.fullmatch(token) or _LONG_NUMBER.fullmatch(token) or _is_code(token)
        ):
            continue
        kept.append(token)
    while len(kept) > 1 and _is_number(kept[-1]):
        kept.pop()
    # A number alone names nobody ("MORTGAGE PAYMENT 5512098871").
    return [] if len(kept) == 1 and _is_number(kept[0]) else kept


def _web_name(token: str) -> str:
    """``NAME`` for ``NAME.COM`` and ``WWW.NAME.COM``; any other token as it is."""
    name = token.rstrip(".,;:")
    prefixed = name.startswith(_WEB_PREFIX) and len(name) > len(_WEB_PREFIX)
    if prefixed:
        name = name[len(_WEB_PREFIX) :]
    for ending in _WEB_ENDINGS:
        if name.endswith(ending) and len(name) > len(ending):
            return name[: -len(ending)]
    return name if prefixed else token


def _bounded(tokens: list[str]) -> str:
    """At most :data:`CORE_TOKENS_MAX` tokens and :data:`CORE_MAX` characters."""
    tokens = tokens[:CORE_TOKENS_MAX]
    while len(tokens) > 1 and len(" ".join(tokens)) > CORE_MAX:
        tokens.pop()
    return " ".join(tokens)[:CORE_MAX].strip()


def _name(tokens: list[str]) -> str:
    """The core made of ``tokens``: no number, no web ending, no company suffix."""
    named = " ".join(_web_name(token) for token in _strip_numbers(tokens))
    folded = _fold(_POSSESSIVE.sub("", named)).split()
    # A possessive printed apart ("TRADER JOE S"), then what kind of company it is.
    folded = [word for index, word in enumerate(folded) if index == 0 or word != _APART]
    while len(folded) > 1 and folded[-1] in _COMPANY_SUFFIXES:
        folded.pop()
    return _bounded(folded)


def _core(text: str) -> tuple[str, str | None]:
    """The merchant in ``text`` without place and numbers, and its shorter reading if any."""
    text = _PHONE.sub(_MARKED, text)
    text = _PLUS_SIGN.sub(" PLUS", text)
    text = _GLUED_NUMBER.sub(_MARKED, text)
    text = _HASH_NUMBER.sub(_MARKED, text)
    text = _STORE_NUMBER.sub(_MARKED, text)
    text = _STAR_CODE.sub(_star_code, text)
    tokens = [
        token for token in text.split() if token == _MARK or any(ch.isalnum() for ch in token)
    ]
    open_city = _strip_place(tokens)
    core = _name(tokens)
    shorter = _name(tokens[:-1]) if open_city else ""
    return core, shorter if shorter and shorter != core else None


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
    kind, signal, rest, wording = _kind(text)
    if labelled is not None:
        kind = _ach_kind(kind, rest, descr)
    if kind in _PAYROLL_FROM and _says_payroll(kind, rest, labelled=labelled is not None):
        kind = "payroll"
        rest = _PAYROLL_WORD.sub(" ", rest)
    if kind == "purchase" and _RECURRING_TAIL.search(rest):
        kind, signal = "recurring_purchase", "recurring"
        rest = _RECURRING_TAIL.sub("", rest)
    if signal is not None:
        signals.add(signal)
    if kind == "transfer" and _WIRE.match(text):
        signals.add(WIRE_SIGNAL)
    if kind in _NO_CORE:
        return Cleaned(core="", kind=kind, signals=frozenset(signals))
    if kind in _ACH_OPEN and _STUDENT_LOAN.search(rest):
        kind = "loan_payment"
        rest = _LOAN_PAYMENT_WORD.sub(" ", _STUDENT_LOAN.sub(" ", rest))
    rest = _strip_trailers(rest)
    numbered = _LEADING_NUMBER.match(rest) if wording else None
    if numbered is not None:
        rest = rest[numbered.end() :]
    service = _P2P.match(rest)
    if service is not None:
        # Between people the service is the only name: never the other person,
        # whatever the bank printed in front of the service.
        core = _fold(service.group(1) or _SQUARE_CASH)
        return Cleaned(core=core, kind="p2p", signals=frozenset(signals))
    via, rest = _processor(rest)
    core, shorter = _core(rest)
    return Cleaned(core=core, kind=kind, via=via, signals=frozenset(signals), shorter=shorter)

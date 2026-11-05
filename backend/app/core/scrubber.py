# SPDX-License-Identifier: AGPL-3.0-or-later
"""Free-text identity scrubber for everything that leaves through ``ai:read``.

AI_ACCESS.md "Free-text scrubbing", SECURITY.md E1, AI_ACCESS.md M2. Payees,
memos and notes come from statements and people; they can carry stray
identifiers ("ZELLE TO JANE DOE", "CARD 1234", "ACCT ...5678", a phone number
in a utility memo). :meth:`Scrubber.scrub` replaces them with typed
placeholders:

==============  ==============================================================
``[ACCT]``      account, card, routing, IBAN, loan and policy numbers,
                including partial masks (``...5678``, ``XXXX1234``,
                ``****1234``, ``x1234``, ``x 1234``, ``*1234``) and numbers
                named by a keyword, a card brand or network, with up to two
                words in between (``ACCT 1234``, ``ending in 1234``,
                ``CARD 1234``, ``AMEX 4455``, ``DEBIT CARD PURCHASE 6655``,
                ``LOAN #12-345``; a date or an amount after the keyword is
                kept: ``card payment 12.50``), with every further
                space-separated group (``Account 9876 5432``), in brackets
                (``Checking (4455)``, ``Card [4455]``) or glued to the
                keyword (``visa4455``, ``card_4455``, ``AMEX·1357``); the
                household's stored account-number fragments
                (``account.number_last4``) wherever they stand alone
                (``Sapphire 6611``); every standalone digit run of
                7 or more digits, groups joined by spaces, ``.``, ``-``,
                ``/``, ``_`` or ``,`` (cards are recognised by Luhn, routing
                numbers by the ABA checksum, but an unrecognised long number
                is an account number too)
``[GOVID]``     SSN / ITIN (``123-45-6789``, ``123,456,789`` shapes), EIN
                (``12-3456789``), and numbers named by ``SSN``/``ITIN``/
                ``EIN``/``TIN``; a bare 9-digit number that is not a valid
                routing number
``[EMAIL]``     e-mail addresses, also ``name [at] host [dot] com`` forms
                and spaced ones (``bob . smith @ gmail . com``)
``[PHONE]``     US numbers (``(555) 123-4567``, ``555.123.4567``,
                ``+1 555 123 4567``) and international ``+CC ...`` numbers
                (8-15 digits)
``[ADDRESS]``   US-style street addresses (number, optional direction,
                up to four name words, a street suffix, optional unit and
                ``, City, ST 12345`` tail), PO boxes, and a unit on its own
                after an address
``[MEMBER]``    household members' display names (whole and each part of
                three or more letters), usernames, e-mail local parts and
                configured aliases; also spelled out (``j a n e``,
                ``J.A.N.E``), with Greek or Cyrillic look-alike letters or
                digits for letters (``JANE`` with a Greek alpha, ``D0E``) and, for a word of five
                letters or more, with a trailing ``s`` (``WOLCOTTS``)
``[REDACTED]``  the client's custom denylist
==============  ==============================================================

**What survives** (the false-positive corpus in
``tests/security/ai/corpus/false_positives.yaml`` pins it): merchant names,
city and state (merchant location is a financial fact, AI_ACCESS.md), dates,
amounts (``1234.56``, ``12,345.67``; a thousands-separated whole number
only up to six digits, so ``1,234,567`` is redacted) and **store numbers of up to six digits**
(``STARBUCKS #1234 SEATTLE WA``, ``COSTCO WHSE #0123``). A four-digit number
is kept unless a mask or a keyword marks it as an account (``#1234`` is a
store; ``x1234``, ``...1234``, ``CARD 1234`` are accounts). Longer store or
terminal numbers (7+ digits) are redacted as possible account numbers: losing
them costs little, leaking an account number is what this module prevents.

**Normalisation first.** Before matching, text is NFKC-normalised (full-width
digits and letters become ASCII, ``…`` becomes ``...``), every decimal digit
of any script becomes its ASCII digit, accents are stripped (NFKD, combining
marks dropped: ``José`` matches ``JOSE``), format characters (zero-width
joiners and spaces, soft hyphens, bidi marks: Unicode category ``Cf``) are
removed and control characters become spaces. The scrubbed output is the
normalised text: an obfuscation never survives into what an assistant sees.

**Linear time.** Every pattern is a fixed regular expression without nested
unbounded quantifiers; repetition that could backtrack uses possessive
quantifiers or small fixed bounds, and the input is capped at
:data:`MAX_INPUT` characters (longer text is cut, and the cut is marked with
``[REDACTED]``, never passed through). Name and denylist patterns are
built once per :class:`Scrubber` from ``re.escape``-d terms (no user text is
ever a pattern).

**Idempotent.** ``scrub(scrub(x)) == scrub(x)``: placeholders are never
matched again (each stage works on the text between existing placeholders),
normalisation is idempotent, and the stages repeat until a pass changes
nothing (a placeholder can open a word boundary for another pattern):
property-tested.

The scrubber runs in the API (``app.domain.ai``) and again in the MCP process
(``app.mcp``), which is why it lives in ``app.core`` with no database access.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final

#: Longest text scrubbed; anything after it is dropped (and marked).
MAX_INPUT: Final = 4096
#: Most scrub passes over one text (Scrubber.scrub_text runs to a fixed point).
MAX_PASSES: Final = 8

ACCT: Final = "[ACCT]"
GOVID: Final = "[GOVID]"
EMAIL: Final = "[EMAIL]"
PHONE: Final = "[PHONE]"
ADDRESS: Final = "[ADDRESS]"
MEMBER: Final = "[MEMBER]"
REDACTED: Final = "[REDACTED]"
PLACEHOLDERS: Final = (ACCT, GOVID, EMAIL, PHONE, ADDRESS, MEMBER, REDACTED)

_PLACEHOLDER_SPLIT: Final = re.compile("(" + "|".join(re.escape(p) for p in PLACEHOLDERS) + ")")

#: Shortest name part matched on its own (``Al`` would hit too much text).
MIN_NAME_PART: Final = 3
#: Shortest custom denylist entry or alias.
MIN_TERM: Final = 2


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _fold_char(ch: str) -> str:
    cat = unicodedata.category(ch)
    if cat == "Cf":  # zero-width joiners/spaces, soft hyphen, bidi marks, BOM
        return ""
    if cat == "Mn":  # combining marks (after NFKD): accents, underlines
        return ""
    if cat == "Nd":
        return str(unicodedata.decimal(ch))
    if cat in ("Cc", "Zl", "Zp", "Zs"):
        return " "
    return ch


def normalise(text: str) -> str:
    """The canonical form every pattern is matched against (module doc)."""
    folded = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text))
    out = "".join(_fold_char(ch) for ch in folded)
    # NFKD splits ligatures and compatibility forms; recompose what is left.
    return unicodedata.normalize("NFC", out)


# ---------------------------------------------------------------------------
# Fixed patterns
# ---------------------------------------------------------------------------

_I = re.IGNORECASE

_EMAIL_AT = r"(?:@|\s?[\[(]\s?at\s?[\])]\s?)"
_EMAIL_DOT = r"(?:\.|\s?[\[(]\s?dot\s?[\])]\s?)"
EMAIL_RE: Final = re.compile(
    r"(?<![\w+\-])[a-z0-9][\w.%+\-]{0,127}+"
    + _EMAIL_AT
    + r"[a-z0-9](?:[a-z0-9\-]{0,62}+)(?:"
    + _EMAIL_DOT
    + r"[a-z0-9](?:[a-z0-9\-]{0,62}+)){1,6}+",
    _I,
)

# "bob . smith @ gmail . com": spaces around "@" and the dots (P6B-SR-R2-02).
# Only with a common top-level domain at the end, so ordinary text with an
# "@" ("Lunch @ Cafe. Then home") is not taken for an address, and only when
# a dot has a space next to it (Scrubber._spaced_email): "Coffee @
# Starbucks.com" names a merchant.
_SPACED_TLD = (
    r"(?:com|net|org|edu|gov|mil|int|io|co|us|uk|ca|au|de|fr|nl|es|it|ch|se|no|dk|"
    r"fi|ie|nz|jp|in|br|mx|me|info|biz|email|mail|online|xyz|app|dev)"
)
SPACED_EMAIL_RE: Final = re.compile(
    r"(?<![\w+\-])[a-z0-9][\w%+\-]{0,63}+(?:\s{0,2}+\.\s{0,2}+[\w%+\-]{1,63}+){0,4}+"
    r"\s{0,2}+@\s{0,2}+[a-z0-9][a-z0-9\-]{0,62}+"
    r"(?:\s{0,2}+\.\s{0,2}+[a-z0-9][a-z0-9\-]{0,62}+){0,4}"  # backtracks at most 4 steps
    r"\s{0,2}+\.\s{0,2}+" + _SPACED_TLD + r"(?![\w\-])",
    _I,
)

_SPACED_DOT: Final = re.compile(r"\s\.|\.\s")

SPELLED_EMAIL_RE: Final = re.compile(
    r"\b[a-z0-9][a-z0-9._%+\-]{0,63}+\s+at\s+[a-z0-9][a-z0-9\-]{0,62}+"
    r"(?:\s+dot\s+[a-z0-9][a-z0-9\-]{0,62}+){1,4}+\b",
    _I,
)

# A mask of 2+ mask characters followed by 2-8 digits: ...5678, XXXX1234,
# ****1234, ••••1234, xx-1234, XX**1234. Symbol masks match anywhere
# ("VISA...5678"); an x mask must not continue a word ("TJ MAXX 1234" is a store).
MASKED_SYM_RE: Final = re.compile(r"[.*•·]{2,12}+[\s\-]{0,2}+\d{2,8}+(?!\d)")
MASKED_X_RE: Final = re.compile(r"(?<![A-WYZa-wyz0-9])[xX.*•·#]{2,12}+[\s\-]{0,2}+\d{2,8}+(?!\d)")
# x1234 / X-1234 / x 1234 (a single x before 4 digits: "CARD x1234").
X_DIGITS_RE: Final = re.compile(r"(?<![\w])x[\s\-]?\d{4}(?!\d)", _I)
# *1234 / * 1234 (a single star before 4 digits: "VISA *3344"). A star before
# letters ("UBER *TRIP", "SQ *CAFE") is a payment-processor prefix and kept.
STAR_DIGITS_RE: Final = re.compile(r"(?<![\d*])\*[\s\-]?\d{4}(?!\d)")

_MASK = r"[xX.*•·#]{2,12}+"
_SEP = r"[\s:#.\-=/]{0,4}+"
_NUMBER_WORD = r"(?:(?:no|nbr|num|number|ref)\b\.?" + _SEP + r")?"
_ENDING = r"(?:(?:ending|ends|end)(?:\s+(?:in|with))?" + _SEP + r")?"
# The number: optional mask and up to four letters, then digits and letters
# with single "-", "/", "_", "," or "." separators between them (so
# "1234/5678/9012" and "1234,5678,9012" are one value, not "[ACCT]/5678/9012").
_ACCT_VALUE = (
    r"(?:" + _MASK + r"[\s\-]{0,2}+)?[a-z]{0,4}+(?P<value>\d(?:[\-/_,.]?+[\dA-Z]){1,33}+)"
    # Further space-separated digit groups belong to the same number
    # ("Account 9876 5432", "acct 98 76 54 32", "routing 0210 0002 1",
    # P6B-SR-R2-01); a group that starts a date or an amount ("card 4455
    # 12.50", "VISA 4455 2026-03-04") ends the number.
    r"(?:\s{1,2}+\d{1,6}+(?![.,/:\-]?\d)){0,5}+"
)
# Up to two plain words between the keyword and the number ("DEBIT CARD
# PURCHASE 6655", "VISA SIGNATURE 1234"). A value that is a date or an amount
# ("card payment 12.50") is kept (Scrubber._acct_keyword).
_FILLER = r"(?:[a-z]{2,12}+\b" + _SEP + r"){0,2}"
#: Keywords naming an account, card or loan number, card brands and networks.
_ACCT_KEYWORDS = (
    r"acct|account|acc|a/c|card|cc|debit|credit\s+card|chk|checking|sav|savings|"
    r"loan|mortgage|policy|pol|member|mbr|iban|routing|aba|rtn|bsb|sort\s+code|"
    r"last\s*4|last\s+four|visa|amex|american\s+express|mastercard|master\s+card|mc|"
    r"discover|diners|jcb|maestro|unionpay|pos|purchase"
)
# Between a keyword and its number: any non-word characters, brackets
# included ("Checking (4455)", "Card [4455]", "card_4455", "AMEX·1357"), or
# none at all ("visa4455", "MC4455"): P6B-SR-R2-01. Mask characters are left
# to the mask of the value ("CARD ****9911"), except a single one directly
# before a digit ("AMEX·1357", "VISA*4455").
_KEYWORD_SEP = r"(?:[^\w*•·]|_){0,4}+(?:[*•·](?=\d))?"
ACCT_KEYWORD_RE: Final = re.compile(
    r"\b(?:"
    + _ACCT_KEYWORDS
    + r")(?![a-z])\.?"
    + _KEYWORD_SEP
    + _FILLER
    + _NUMBER_WORD
    + _ENDING
    + _ACCT_VALUE,
    _I,
)
ENDING_RE: Final = re.compile(
    r"\b(?:ending|ends|end)(?:\s+(?:in|with))?" + _SEP + r"(?:" + _MASK + r"[\s\-]{0,2}+)?"
    r"\d{2,8}+(?!\d)",
    _I,
)
GOVID_KEYWORD_RE: Final = re.compile(
    r"\b(?:ssn|ss#|soc\s+sec|social\s+security|itin|ein|fein|tin|tax\s+id)\b\.?"
    + _SEP
    + _NUMBER_WORD
    + r"(?:last\s*(?:4|four)\b"
    + _SEP
    + r")?(?P<value>(?:"
    + _MASK
    + r"[\s\-]{0,2}+)?[\dX*][\dX*\-\s]{0,14}\d)",
    _I,
)
#: Most digits in an SSN, ITIN or EIN (a masked position counts as one).
GOVID_MAX_DIGITS: Final = 9
_GOVID_MASK_CHARS: Final = frozenset("xX*•·.#")
_LAST_FOUR: Final = re.compile(r"last\s*(?:4|four)\b", _I)
# An IBAN: country code, check digits, 11-30 alphanumerics (spaces allowed),
# at least 8 digits in all (checked in code).
IBAN_RE: Final = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]){11,30}+\b", _I)

_SUFFIX = (
    r"(?:street|st|avenue|ave|av|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|"
    r"place|pl|way|terrace|ter|circle|cir|parkway|pkwy|highway|hwy|trail|trl|"
    r"square|sq|loop|alley|aly|crossing|xing|pike|plaza|plz|point|pt|ridge|"
    r"route|rte|run|walk|row|close|crescent|cres|expressway|expy|freeway|fwy|"
    r"turnpike|tpke|cove|cv|commons|path)"
)
_DIRECTION = (
    r"(?:n|s|e|w|ne|nw|se|sw|north|south|east|west|northeast|northwest|southeast|southwest)"
)
_UNIT = r"(?:apt|apartment|unit|ste|suite|fl|floor|rm|room|bldg|building|#)"
STREET_RE: Final = re.compile(
    # Not the tail of a dashed number ("123-45-6789 742 Elm St": the house
    # number is 742, and the SSN stays whole for the digit-run rules).
    r"(?<!\d-)\b\d{1,6}[a-z]?(?:-\d{1,4})?\s+"  # house number (12, 12B, 12-14)
    r"(?:" + _DIRECTION + r"\.?\s+)?"
    r"(?:[a-z0-9][a-z0-9'.\-]{0,30}+\s+){0,4}"  # street name words (bounded)
     + _SUFFIX + r"\b\.?"
    r"(?:\s+" + _DIRECTION + r"\b\.?)?"
    r"(?:[\s,]{1,3}+" + _UNIT + r"\.?\s?#?\s?[a-z0-9\-]{1,8}+\b)?"
    r"(?:,\s?[a-z][a-z .'\-]{1,40}?,\s?[a-z]{2}\b(?:\s+\d{5}(?:-\d{4})?)?)?",
    _I,
)
PO_BOX_RE: Final = re.compile(
    r"\b(?:p\.?\s?o\.?\s?box|post\s+office\s+box|pob)\s*#?\s*\d{1,10}+\b", _I
)
UNIT_AFTER_ADDRESS_RE: Final = re.compile(
    re.escape(ADDRESS) + r"[\s,]{0,3}+" + _UNIT + r"\.?\s?#?\s?[a-z0-9\-]{1,8}+\b", _I
)
ZIP_AFTER_ADDRESS_RE: Final = re.compile(
    re.escape(ADDRESS) + r"(?:,?\s?[a-z][a-z .'\-]{1,40}?,?\s[a-z]{2})?\s+\d{5}(?:-\d{4})?\b",
    _I,
)

# A run of digits with single separators between them, optionally starting
# with "+" or "(": the unit numbers are classified as (date, amount, phone,
# government id, card, account, short number).
# "/", "_" and "," join groups too ("1234/5678/9012", "123,456,789"); a
# thousands-separated amount ("12,345.67") and a date ("01/15/2026") are
# recognised as such by classify_digit_run.
DIGIT_RUN_RE: Final = re.compile(r"(?<!\d)[+(]?\d(?:[ .\-()/_,]{0,3}+\d)*+(?!\d)")


@dataclass(frozen=True)
class _RunShape:
    digits: str
    groups: tuple[str, ...]
    plus: bool


_GROUPS_RE: Final = re.compile(r"\d+")


def _shape(raw: str) -> _RunShape:
    groups = tuple(_GROUPS_RE.findall(raw))
    return _RunShape(digits="".join(groups), groups=groups, plus=raw.startswith("+"))


def luhn_ok(digits: str) -> bool:
    total = 0
    for index, ch in enumerate(reversed(digits)):
        value = int(ch)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def aba_ok(digits: str) -> bool:
    if len(digits) != 9:
        return False
    weights = (3, 7, 1) * 3
    return sum(int(d) * w for d, w in zip(digits, weights, strict=True)) % 10 == 0


_DATE_SHAPES: Final = (
    re.compile(r"\d{4}([-./])\d{1,2}\1\d{1,2}"),
    re.compile(r"\d{1,2}([-./])\d{1,2}\1\d{2,4}"),
)
# 1234.56, 12,345.67, 1,234,567.89; a thousands-separated whole number only
# up to six digits (12,345): "123,456,789" is an SSN or account shape, not an
# amount.
_AMOUNT_RE: Final = re.compile(r"\d{1,12}\.\d{1,2}|\d{1,3}(?:,\d{3})++\.\d{1,2}|\d{1,3},\d{3}")


def _plausible_date(raw: str) -> bool:
    if not any(p.fullmatch(raw) for p in _DATE_SHAPES):
        return False
    parts = [int(p) for p in _GROUPS_RE.findall(raw)]
    if len(str(parts[0])) == 4 or raw[:4].isdigit():
        _, month, day = parts
    else:
        month, day, _ = parts
        if month > 12:  # day.month.year
            month, day = day, month
    return 1 <= month <= 12 and 1 <= day <= 31


_MONTH_DAY_RE: Final = re.compile(r"(\d{1,2})/(\d{1,2})")


def _month_day(raw: str) -> bool:
    """A statement's ``MM/DD`` posting date ("VISA PURCHASE 03/14")."""
    found = _MONTH_DAY_RE.fullmatch(raw)
    return found is not None and 1 <= int(found.group(1)) <= 12 and 1 <= int(found.group(2)) <= 31


def _date_or_amount(raw: str) -> bool:
    stripped = raw.strip("()")
    return _plausible_date(stripped) or _AMOUNT_RE.fullmatch(stripped) is not None


def classify_digit_run(raw: str) -> str | None:
    """The placeholder for a digit run, ``None`` to keep it (module doc).

    A run that joins a date or an amount to other numbers across spaces
    ("2026-01-15 12.34", "12.34 5551234567") is judged by the other numbers:
    kept if they are harmless, the whole run replaced if they are not.
    """
    if _date_or_amount(raw):
        return None
    chunks = [c.strip(",") for c in raw.split()]
    chunks = [c for c in chunks if c]
    if len(chunks) > 1 and any(_date_or_amount(c) for c in chunks):
        rest = " ".join(c for c in chunks if not _date_or_amount(c))
        return classify_digit_run(rest) if rest else None
    shape = _shape(raw)
    n = len(shape.digits)
    lengths = tuple(len(g) for g in shape.groups)
    if lengths == (3, 2, 4) or (lengths == (2, 7) and "-" in raw):
        return GOVID
    if shape.plus and 8 <= n <= 15:
        return PHONE
    if n == 10 and lengths in ((3, 3, 4), (3, 7), (6, 4)):
        return PHONE
    if n == 11 and shape.digits[0] == "1" and lengths in ((1, 3, 3, 4), (4, 3, 4)):
        return PHONE
    if n >= 13 and luhn_ok(shape.digits):
        return ACCT
    if n == 9 and lengths in ((9,), (3, 3, 3)):
        return ACCT if aba_ok(shape.digits) else GOVID
    if n >= 7:
        return ACCT
    return None


# ---------------------------------------------------------------------------
# The scrubber
# ---------------------------------------------------------------------------


def _segments(text: str, fn: Callable[[str], str]) -> str:
    """Apply ``fn`` to the text between placeholders (never to a placeholder)."""
    parts = _PLACEHOLDER_SPLIT.split(text)
    return "".join(part if i % 2 else fn(part) for i, part in enumerate(parts))


def _sub(pattern: re.Pattern[str], repl: str | Callable[[re.Match[str]], str], text: str) -> str:
    return _segments(text, lambda part: pattern.sub(repl, part))


_TERM_SEP: Final = r"[\s._\-,'*/+&]{0,3}+"
_WORD_SPLIT: Final = re.compile(r"[^\w]+|_")


#: Between the letters of a spelled-out term ("j a n e", "J.A.N.E").
_SPELL_SEP: Final = r"[\s._\-*/+]{1,3}+"
#: Shortest word that may also appear with a plural or possessive "s" and no
#: apostrophe ("WOLCOTTS TRUST"); a shorter one ("Doe") would hit "does".
MIN_PLURAL_WORD: Final = 5


def _term_pattern(term: str, *, member: bool = False) -> str | None:
    """A word-bounded, case-insensitive, separator-tolerant pattern for ``term``.

    For a member term (``member=True``) the pattern also matches the term
    spelled out letter by letter ("j a n e  d o e", "J.A.N.E") and, when its
    last word has :data:`MIN_PLURAL_WORD` letters or more, with a trailing
    ``s`` ("WOLCOTTS"): P6B-SR-R2-02.
    """
    words = [w for w in _WORD_SPLIT.split(normalise(term).casefold()) if w]
    if not words or sum(len(w) for w in words) < MIN_TERM:
        return None
    body = _TERM_SEP.join(re.escape(w) for w in words)
    if member:
        if sum(len(w) for w in words) >= MIN_NAME_PART:
            spelled = _SPELL_SEP.join(_SPELL_SEP.join(re.escape(ch) for ch in w) for w in words)
            body = f"(?:{body}|{spelled})"
        if len(words[-1]) >= MIN_PLURAL_WORD:
            body += "s?"
    return r"(?<![^\W_])" + body + r"(?![^\W_])"


def _compile_terms(terms: Iterable[str], *, member: bool = False) -> re.Pattern[str] | None:
    patterns = sorted(
        {p for p in (_term_pattern(t, member=member) for t in terms) if p is not None},
        key=len,
        reverse=True,
    )
    if not patterns:
        return None
    return re.compile("|".join(patterns), re.IGNORECASE)


# Unicode confusables folded before names are matched (a small TR39-style
# skeleton, P6B-SR-R2-02): Greek and Cyrillic letters that look like Latin
# ones ("JANE" written with a Greek alpha) and, inside a word that has letters, the
# digits that stand in for letters ("D0E", "SM1THERS"). The fold maps one
# character to one character, so a match in the folded text is replaced at
# the same place in the real text; the output keeps the real text.
_CONFUSABLES: Final = str.maketrans(
    "ΑΒΕΖΗΙΚΜΝΟΡΤΥΧαβεικνορτυχϲϳАВЕКМНОРСТХУЅІЈаеорсухѕіјԁһӏɑ",
    "ABEZHIKMNOPTYXabeikvoptuxcjABEKMHOPCTXYSIJaeopcyxsijdhla",
)
_LEET_I: Final = str.maketrans("013457", "oieast")
_LEET_L: Final = str.maketrans("013457", "oleast")
_ALNUM_WORD: Final = re.compile(r"[^\W_]+")


def _skeletons(text: str) -> list[str]:
    """The text and its folded forms, each as long as ``text`` (see above)."""
    base = text.translate(_CONFUSABLES)
    out = [text] if base == text else [text, base]
    for table in (_LEET_I, _LEET_L):

        def fold(match: re.Match[str], table: dict[int, int] = table) -> str:
            word = match.group(0)
            if word.isdigit() or not any(ch.isdigit() for ch in word):
                return word
            return word.translate(table)

        folded = _ALNUM_WORD.sub(fold, base)
        if folded not in out:
            out.append(folded)
    return out


def _sub_folded(pattern: re.Pattern[str], repl: str, text: str) -> str:
    """Replace every match of ``pattern`` in ``text`` or in a folded form of it."""
    spans: list[tuple[int, int]] = []
    for form in _skeletons(text):
        if len(form) != len(text):  # pragma: no cover - the fold is one-to-one
            raise AssertionError("confusable fold changed the length")
        spans.extend(m.span() for m in pattern.finditer(form) if m.end() > m.start())
    if not spans:
        return text
    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out: list[str] = []
    last = 0
    for start, end in merged:
        out.append(text[last:start])
        out.append(repl)
        last = end
    out.append(text[last:])
    return "".join(out)


#: A stored account-number fragment (``account.number_last4``).
_ACCOUNT_NUMBER: Final = re.compile(r"\d{2,8}")


def _compile_numbers(numbers: Iterable[str]) -> re.Pattern[str] | None:
    """Known account-number fragments as stand-alone digit runs (P6B-SR-R2-01).

    ``Sapphire 6611`` where 6611 is that account's stored last four. A
    fragment inside a longer number is left to the digit-run rules, and one
    that is the whole part of an amount ("6611.00") is an amount.
    """
    wanted = sorted({n for n in numbers if _ACCOUNT_NUMBER.fullmatch(n)}, key=len, reverse=True)
    if not wanted:
        return None
    return re.compile(r"(?<!\d)(?<!\d[.,])(?:" + "|".join(wanted) + r")(?![.,]?\d)")


#: Words of a household's name that are not a family name ("The Doe Family").
HOUSEHOLD_STOPWORDS: Final = frozenset(
    {
        "and",
        "the",
        "our",
        "family",
        "families",
        "household",
        "home",
        "house",
        "residence",
        "finances",
        "finance",
        "money",
    }
)


def member_terms(
    names: Iterable[str | None],
    usernames: Iterable[str | None] = (),
    emails: Iterable[str | None] = (),
    aliases: Iterable[str | None] = (),
    household_names: Iterable[str | None] = (),
) -> list[str]:
    """Every string that identifies a household member.

    A display name counts whole and by each part of :data:`MIN_NAME_PART`
    letters or more ("Jane Q. Doe" -> "Jane Q. Doe", "Jane", "Doe"). A
    shorter part ("Li", "Bo") is too common to match alone; it counts next to
    any longer part of any member's name, in either order ("Jane Li",
    "LI JANE", "Sam Li"). An e-mail address counts by itself and its local
    part. A household's name ("The Wolcott Family") counts by each word of
    :data:`MIN_NAME_PART` letters or more that is not a
    :data:`HOUSEHOLD_STOPWORDS` word: it is usually a family name.
    """
    terms: list[str] = []
    long_parts: list[str] = []
    short_parts: list[str] = []
    for name in names:
        if not name:
            continue
        terms.append(name)
        for word in _WORD_SPLIT.split(normalise(name)):
            if len(word) >= MIN_NAME_PART:
                long_parts.append(word)
            elif len(word) == MIN_NAME_PART - 1 and word.isalpha():
                short_parts.append(word)
    terms.extend(long_parts)
    for short in dict.fromkeys(short_parts):
        for long in dict.fromkeys(long_parts):
            terms.extend((f"{long} {short}", f"{short} {long}"))
    for household in household_names:
        if household:
            terms.extend(
                w
                for w in _WORD_SPLIT.split(normalise(household))
                if len(w) >= MIN_NAME_PART and w.casefold() not in HOUSEHOLD_STOPWORDS
            )
    for value in (*usernames, *aliases):
        if value:
            terms.append(value)
    for email in emails:
        if email:
            terms.append(email)
            local = email.split("@", 1)[0]
            if len(local) >= MIN_NAME_PART:
                terms.append(local)
    return terms


class Scrubber:
    """Replace identifiers in free text with typed placeholders (module doc).

    ``member_terms``: names, usernames, e-mail addresses and aliases of the
    household's members (:func:`member_terms` builds the list); ``denylist``:
    the AI client's custom terms. Both are matched case-insensitively on word
    boundaries after normalisation, tolerating up to three separator
    characters between words ("JANE.DOE", "Jane  Doe", "JANE*DOE").
    """

    def __init__(
        self,
        member_terms: Iterable[str] = (),
        denylist: Iterable[str] = (),
        account_numbers: Iterable[str | None] = (),
    ) -> None:
        members = sorted({normalise(t).casefold() for t in member_terms})
        denied = sorted({normalise(t).casefold() for t in denylist})
        numbers = sorted({normalise(n).strip() for n in account_numbers if n})
        self._members = _compile_terms(members, member=True)
        self._denylist = _compile_terms(denied)
        self._numbers = _compile_numbers(numbers)
        digest = hashlib.sha256(json.dumps([members, denied, numbers]).encode()).hexdigest()
        #: A digest of the terms (never the terms): what a cached scrubbed
        #: answer depends on besides the fixed patterns (router cache key).
        self.fingerprint: Final[str] = digest

    def scrub(self, text: str | None) -> str | None:
        if text is None:
            return None
        return self.scrub_text(text)

    def scrub_text(self, text: str) -> str:
        cut = len(text) > MAX_INPUT
        out = normalise(text[:MAX_INPUT])
        # A replacement can create a word boundary a pattern needs ("6611visa00"
        # with 6611 a known number: "[ACCT]visa00", whose "visa00" is then a
        # keyworded number). Passes repeat until nothing changes, so the result
        # is a fixed point (idempotent). Every pass only turns text into
        # placeholders, so this ends; MAX_PASSES bounds it regardless.
        for _ in range(MAX_PASSES):
            again = self._pass(out)
            if again == out:
                break
            out = again
        if cut:
            out = out + " " + REDACTED
        return out

    def _pass(self, out: str) -> str:
        # Order matters: e-mails and keyworded numbers before bare digit runs,
        # addresses before masks and digit runs (a house number is short and
        # "..12 Main St" must stay an address), names after e-mails (an
        # address's local part may be a member's name), known account numbers
        # after digit runs (a stored last four inside a longer number must not
        # split it: the whole number is replaced).
        out = _sub(SPACED_EMAIL_RE, self._spaced_email, out)
        out = _sub(EMAIL_RE, EMAIL, out)
        out = _sub(SPELLED_EMAIL_RE, EMAIL, out)
        out = _sub(GOVID_KEYWORD_RE, self._govid_keyword, out)
        out = _sub(PO_BOX_RE, ADDRESS, out)
        out = _sub(STREET_RE, ADDRESS, out)
        out = UNIT_AFTER_ADDRESS_RE.sub(ADDRESS, out)
        out = ZIP_AFTER_ADDRESS_RE.sub(ADDRESS, out)
        out = _sub(IBAN_RE, self._iban, out)
        out = _sub(ACCT_KEYWORD_RE, self._acct_keyword, out)
        out = _sub(ENDING_RE, ACCT, out)
        out = _sub(MASKED_SYM_RE, ACCT, out)
        out = _sub(MASKED_X_RE, ACCT, out)
        out = _sub(X_DIGITS_RE, ACCT, out)
        out = _sub(STAR_DIGITS_RE, ACCT, out)
        members, denylist = self._members, self._denylist
        if members is not None:
            out = _segments(out, lambda part: _sub_folded(members, MEMBER, part))
        if denylist is not None:
            out = _segments(out, lambda part: _sub_folded(denylist, REDACTED, part))
        out = _sub(DIGIT_RUN_RE, self._digit_run, out)
        if self._numbers is not None:
            out = _sub(self._numbers, ACCT, out)
        return out

    @staticmethod
    def _govid_keyword(match: re.Match[str]) -> str:
        # A government id has at most GOVID_MAX_DIGITS digits. The value part
        # may run on over spaces ("SSN 123 45 6789"), so a number after the
        # id ("SSN 123-45-6789 742 Evergreen Terrace") is handed back to the
        # later rules instead of being eaten: otherwise the house number
        # vanishes into [GOVID] and the street name is left in the clear
        # (v1.0.0 verification run, docs/RELEASE_CHECKLIST.md).
        value = match.group("value")
        limit = 4 if _LAST_FOUR.search(match.group(0)) else GOVID_MAX_DIGITS
        seen = 0
        for i, ch in enumerate(value):
            if ch.isdigit() or ch in _GOVID_MASK_CHARS:
                seen += 1
                if seen == limit:
                    return GOVID + value[i + 1 :]
        return GOVID

    @staticmethod
    def _acct_keyword(match: re.Match[str]) -> str:
        # "card payment 12.50", "Mortgage paid 2026-03-04": a date or an amount
        # after the keyword is a financial fact, not a number naming an account.
        value = match.group("value")
        if _date_or_amount(value) or _month_day(value):
            return match.group(0)
        return ACCT

    @staticmethod
    def _spaced_email(match: re.Match[str]) -> str:
        value = match.group(0)
        return EMAIL if _SPACED_DOT.search(value) else value

    @staticmethod
    def _iban(match: re.Match[str]) -> str:
        value = match.group(0)
        if sum(ch.isdigit() for ch in value) >= 8:
            return ACCT
        return value

    @staticmethod
    def _digit_run(match: re.Match[str]) -> str:
        raw = match.group(0)
        placeholder = classify_digit_run(raw)
        if placeholder is None:
            return raw
        # Keep a leading "(" that belonged to the surrounding text balanced.
        return placeholder


#: A scrubber with no member names or denylist (patterns only).
PATTERNS_ONLY: Final = Scrubber()


def scrub(text: str | None) -> str | None:
    """Scrub with the fixed patterns only (no member names, no denylist)."""
    return PATTERNS_ONLY.scrub(text)

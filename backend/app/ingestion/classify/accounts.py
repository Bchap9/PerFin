# SPDX-License-Identifier: AGPL-3.0-or-later
"""The last four digits of the account a document is about.

Only the last four digits are ever kept (SECURITY.md E1, DATA_MODEL
``account.number_last4``): a printed full number is read through and cut
to its last four digits here, so nothing longer reaches ``document``.

Sources, strongest first: a structured statement's ``last4``; an account
mask of the matching fingerprint (``XXXX-XXXX-XXXX-####``: ``X`` a masked or
any digit, ``#`` a kept digit, other characters literal); generic masks
(``****1234``, ``xxxx-xxxx-1234``, ``x1234``, ``...1234``, "ending in 1234",
"ends with 1234", "last 4 digits 1234"); a labelled number
("Account number: 0012 3456 7890").
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from app.ingestion.classify.text import DocumentView

#: Characters an account mask may hold.
MASK_ALPHABET: Final = frozenset("X#*-. /")
MAX_MASK_CHARS: Final = 40

_MASK_CHAR: Final = r"[x*\u2022\u00b7#\d]"
_ENDING: Final = re.compile(
    r"\b(?:ending|ends)(?: (?:in|with))?:? ?(\d{4})\b"
    r"|\blast (?:4|four)(?: digits)?(?: of (?:the )?account(?: number)?)?:? ?(\d{4})\b"
)
#: ``x1234``, ``x-1234``, ``...1234``, ``\u20261234`` (a short mask before the digits).
_SHORT_MASK: Final = re.compile(r"(?<![\w.\u2026])(?:x|\.{2,4}|\u2026)-?(\d{4})(?![\d])")
#: Possessive quantifiers: a run of mask characters is never split two ways.
_MASKED: Final = re.compile(
    r"(?<![\w*\u2022])[x*\u2022]{2,8}+(?:[ -]?+[x*\u2022]{2,8}+){0,4}+[ -]?(\d{4})\b"
)
_LABELLED: Final = re.compile(
    r"\b(?:account|acct|card|policy|member)(?: number| no\.?| #|#)?:? ?((?:\d[ -]?+){4,20}+)"
)


class MaskError(ValueError):
    """An account mask outside the accepted alphabet."""


def mask_regex(mask: str) -> re.Pattern[str]:
    """Compile an account mask; the last four ``#`` are captured as one group."""
    if not mask or len(mask) > MAX_MASK_CHARS:
        raise MaskError(f"a mask has 1..{MAX_MASK_CHARS} characters")
    if not set(mask) <= MASK_ALPHABET:
        raise MaskError("a mask uses only X, #, *, -, ., / and spaces")
    if mask.count("#") < 4 or not mask.rstrip().endswith("####"):
        raise MaskError("a mask ends with the four kept digits ####")
    head = mask.rstrip()[:-4]
    parts: list[str] = []
    for c in head:
        if c in "X*":
            parts.append(_MASK_CHAR)
        elif c == "#":
            parts.append(r"\d")
        elif c == " ":
            parts.append(" ?")
        else:
            parts.append(re.escape(c))
    # Fixed length, no repetition: linear by construction.
    return re.compile(r"(?<![\w*\u2022])" + "".join(parts) + r"(\d{4})(?!\d)", re.IGNORECASE)


@dataclass(frozen=True)
class Last4:
    value: str | None = None
    source: str = "none"
    confidence: float = 0.0


def _unique(values: Sequence[str]) -> str | None:
    distinct = list(dict.fromkeys(values))
    return distinct[0] if len(distinct) == 1 else None


def detect_last4(view: DocumentView, masks: Sequence[re.Pattern[str]] = ()) -> Last4:
    """The account's last four digits when the document names exactly one account."""
    if view.statement_last4:
        value = _unique(view.statement_last4)
        return Last4(value, "statement", 0.98) if value else Last4(None, "several", 0.0)
    for compiled in masks:
        found = [m.group(1) for line in view.normalised for m in compiled.finditer(line[:300])]
        value = _unique(found)
        if value:
            return Last4(value, "fingerprint_mask", 0.95)
    found = [m.group(1) or m.group(2) for line in view.normalised for m in _ENDING.finditer(line)]
    found += [m.group(1) for line in view.normalised for m in _MASKED.finditer(line)]
    found += [m.group(1) for line in view.normalised for m in _SHORT_MASK.finditer(line)]
    value = _unique(found)
    if value:
        return Last4(value, "mask", 0.85)
    if found:
        return Last4(None, "several", 0.0)
    labelled: list[str] = []
    for line in view.normalised:
        for m in _LABELLED.finditer(line):
            digits = re.sub(r"\D", "", m.group(1))
            if len(digits) >= 4:
                labelled.append(digits[-4:])
    value = _unique(labelled)
    if value:
        return Last4(value, "label", 0.75)
    return Last4(None, "several" if labelled else "none", 0.0)

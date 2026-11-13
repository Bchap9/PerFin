# SPDX-License-Identifier: AGPL-3.0-or-later
"""The currency a document or a table cell names: an ISO code PerFin knows.

Two readings, shared by every document parser:

* :func:`stated_currency`: the code printed after the word "currency" on a
  line of text (``Currency: USD``). The word itself may be in any case; the
  code must be printed as three capital letters and be an active ISO 4217
  code (:func:`app.core.money.is_currency`). A heading such as "Currency
  Forward Contracts" or a sentence ("currency for the period") therefore
  names no currency, and neither does a code PerFin does not know.
* :func:`known_currency`: a table's currency cell, in any case, when it is
  such a code. A filled cell that is not one is for the caller to refuse (the
  table readers skip the row and count it).

Neither guesses: an unread currency stays ``None`` and the parser falls back
on the amounts' symbol or its own default, as it did before.
"""

from __future__ import annotations

import re
from typing import Final

from app.core.money import is_currency

__all__ = ["known_currency", "stated_currency"]

# "currency", an optional colon, then exactly three capital letters as printed
# (only the label is case-insensitive).
_STATED: Final = re.compile(r"\b(?i:currency)\s*:?\s*([A-Z]{3})\b")


def stated_currency(text: str) -> str | None:
    """The known ISO code printed after the word "currency" in ``text``, or None."""
    for match in _STATED.finditer(text):
        if is_currency(match[1]):
            return match[1]
    return None


def known_currency(cell: str) -> str | None:
    """``cell`` upper-cased when it is a known ISO code, else None."""
    code = cell.strip().upper()
    return code if is_currency(code) else None

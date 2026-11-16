# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lines of a statement that are no transaction: balances and column names.

Shared by the QIF extractor (inside the sandbox child) and the table reader
(in the worker), so it imports nothing but the standard library.

* :func:`balance_line`: the whole text is a balance phrase (``Opening
  Balance``, ``Balance brought forward``...), case and spacing aside, and
  says which end of the statement it is. ``Opening balance fee`` is not one.
* :func:`column_name`: the whole text is the name of a statement column
  (``Description``, ``Reference``, ``Debit``...). Words that are also
  everyday payees (``Check``, ``Transfer``, ``Deposit``) are not in the list.
"""

from __future__ import annotations

import re
from typing import Final, Literal

BalanceEnd = Literal["opening", "closing"]

#: Phrases that print the balance a statement starts from.
OPENING_PHRASES: Final = frozenset(
    {
        "opening balance",
        "previous balance",
        "beginning balance",
        "balance forward",
        "balance brought forward",
    }
)
#: Phrases that print the balance a statement ends on.
CLOSING_PHRASES: Final = frozenset(
    {
        "closing balance",
        "ending balance",
        "balance carried forward",
    }
)
#: Column names of statement exports that are never a payee or a category.
COLUMN_NAMES: Final = frozenset(
    {
        "account",
        "amount",
        "balance",
        "category",
        "class",
        "comment",
        "comments",
        "credit",
        "date",
        "debit",
        "description",
        "details",
        "memo",
        "narrative",
        "note",
        "notes",
        "number",
        "particulars",
        "payee",
        "reference",
        "remarks",
        "status",
        "type",
    }
)

_SPACE: Final = re.compile(r"\s+")


def plain(text: str | None) -> str:
    """``text`` case-folded, its spacing collapsed and a trailing ``:`` or ``.`` dropped."""
    if text is None:
        return ""
    return _SPACE.sub(" ", text).strip().rstrip(":.").strip().casefold()


def balance_line(text: str | None) -> BalanceEnd | None:
    """Which balance the whole of ``text`` names, or None when it is no balance phrase."""
    phrase = plain(text)
    if phrase in OPENING_PHRASES:
        return "opening"
    if phrase in CLOSING_PHRASES:
        return "closing"
    return None


def column_name(text: str | None) -> bool:
    """Whether the whole of ``text`` is the name of a statement column."""
    return plain(text) in COLUMN_NAMES

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Second-stage scrub of every MCP tool answer (AI_ACCESS.md M2, SECURITY.md E1).

The API has already scrubbed free text with the household's member names and
the client's denylist. The MCP process does not know those (they are
identity), so this stage re-runs the fixed patterns of
:mod:`app.core.scrubber` (account, card, routing and loan numbers including
partial masks, SSN/ITIN/EIN, e-mail, phone, street addresses, PO boxes) over
**every** string of the answer, keys included, before anything reaches the
assistant. If an API serialiser or the API-side scrubber ever lets an
identifier through, this pass still replaces it.

Only four structural shapes are left untouched, because the patterns would
otherwise corrupt them and they cannot carry free text:

* a whole value that is a canonical lowercase UUID (``loan_id``,
  ``account_id`` references the assistant passes back);
* the ``amount`` of a money object (``{"amount", "currency"}``) when it is a
  plain decimal number;
* the value of a computed or numeric-column field the AI serialisers send as
  a decimal string (:data:`NUMERIC_FIELDS`: weights, rates, returns,
  percentages, prices, quantities) when it is a plain decimal number with at
  most 15 integer digits. Without this, a weight of ``1.0000000000`` read as
  a 10-digit account number and reached the assistant as ``[ACCT]``;
* ``next_cursor`` when it is an opaque base64url token.

Numbers, booleans and ``null`` pass as they are (no serialiser exposes an
identifier as a number; the column-classification test pins that).
"""

from __future__ import annotations

import re
from typing import Any, Final

from app.core.scrubber import PATTERNS_ONLY, Scrubber

_UUID: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DECIMAL: Final = re.compile(r"^-?[0-9]{1,15}(?:\.[0-9]{1,8})?$")
_CURRENCY: Final = re.compile(r"^[A-Z]{3}$")
_CURSOR: Final = re.compile(r"^[A-Za-z0-9_-]{1,200}={0,2}$")
#: A decimal number as the serialisers render ``Decimal`` (no exponent).
_NUMBER: Final = re.compile(r"^-?[0-9]{1,15}(?:\.[0-9]{1,18})?$")
#: Fields of ``app.api.ai.schemas`` that are decimal strings computed by the
#: API or read from a numeric column (``x-ai-source`` ``computed``,
#: ``holding.quantity``, ``price.close``, ``loan.rate``); never free text.
NUMERIC_FIELDS: Final = frozenset(
    {
        "mom_percent",
        "mwr",
        "mwr_annualized",
        "percent",
        "price",
        "quantity",
        "rate",
        "savings_rate",
        "twr",
        "twr_annualized",
        "weight",
        "yoy_percent",
    }
)
MAX_DEPTH: Final = 32


def _is_money(value: dict[str, Any]) -> bool:
    return (
        set(value) == {"amount", "currency"}
        and isinstance(value["amount"], str)
        and _DECIMAL.match(value["amount"]) is not None
        and isinstance(value["currency"], str)
        and _CURRENCY.match(value["currency"]) is not None
    )


class ResponseScrubber:
    """Applies a :class:`Scrubber` to every string of a JSON value."""

    def __init__(self, scrubber: Scrubber = PATTERNS_ONLY) -> None:
        self._scrubber = scrubber

    def text(self, value: str) -> str:
        return self._scrubber.scrub_text(value)

    def _string(self, value: str) -> str:
        if _UUID.match(value):
            return value
        return self._scrubber.scrub_text(value)

    def json(self, value: Any, depth: int = 0) -> Any:
        if depth > MAX_DEPTH:
            raise ValueError("response nested too deeply")
        if isinstance(value, str):
            return self._string(value)
        if isinstance(value, bool | int | float) or value is None:
            return value
        if isinstance(value, list):
            return [self.json(item, depth + 1) for item in value]
        if isinstance(value, dict):
            if _is_money(value):
                return {"amount": value["amount"], "currency": value["currency"]}
            out: dict[str, Any] = {}
            for key, item in value.items():
                clean_key = self._string(str(key))
                if (
                    key == "next_cursor"
                    and isinstance(item, str)
                    and _CURSOR.match(item) is not None
                ) or (
                    key in NUMERIC_FIELDS
                    and isinstance(item, str)
                    and _NUMBER.match(item) is not None
                ):
                    out[clean_key] = item
                else:
                    out[clean_key] = self.json(item, depth + 1)
            return out
        # Anything else is not JSON the API sends; never pass it through.
        raise TypeError(f"unexpected JSON value {type(value).__name__}")

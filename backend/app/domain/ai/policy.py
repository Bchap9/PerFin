# SPDX-License-Identifier: AGPL-3.0-or-later
"""A client's exposure policy applied to values (AI_ACCESS.md "Modes", ADR-0007).

:class:`ClientPolicy` is a snapshot of an ``ai_client`` row: the mode
(``detail`` or ``aggregates``) and the options

* ``rounding``: money rounded to the nearest 1, 10 or 100 (half up) before it
  leaves (:meth:`ClientPolicy.money`); quantities and rates are not money and
  stay exact;
* ``hide_nicknames``: account labels become "<Type> A", "<Type> B", ... in a
  stable order (:func:`account_labels`); in Aggregates mode labels are always
  hidden;
* ``hide_merchant_location``: a trailing ``CITY ST`` (and ZIP) is cut from
  payee strings (:func:`split_merchant_location`);
* ``date_range_cap``: the furthest back a client may look, in days before
  today (:meth:`ClientPolicy.clamp_start`);
* ``custom_denylist`` / ``member_aliases``: terms the scrubber removes.

Everything here is pure; ``app.domain.ai.service`` builds the policy and the
scrubber for a request.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from app.core.money import Money
from app.core.scrubber import Scrubber
from app.domain.ai.models import AiClient, AiMode

US_STATES: Final = frozenset(
    [
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
        "PR",
        "VI",
        "GU",
        "AS",
        "MP",
    ]
)
#: First words of two-word city names ("SAN FRANCISCO", "NEW YORK").
CITY_PREFIXES: Final = frozenset(
    [
        "SAN",
        "SANTA",
        "LOS",
        "LAS",
        "NEW",
        "SAINT",
        "ST",
        "FORT",
        "FT",
        "PORT",
        "NORTH",
        "SOUTH",
        "EAST",
        "WEST",
        "SALT",
        "EL",
        "LA",
        "PALM",
        "BATON",
        "CORPUS",
        "COLORADO",
        "GRAND",
        "LONG",
        "OKLAHOMA",
        "KANSAS",
        "JERSEY",
        "CEDAR",
        "SIOUX",
        "VIRGINIA",
        "BOWLING",
        "CAPE",
        "CHULA",
        "DALY",
        "DES",
        "DEL",
        "FALLS",
        "GREEN",
        "HIGH",
        "IDAHO",
        "IOWA",
        "JEFFERSON",
        "LAKE",
        "MOUNT",
        "MT",
        "OVERLAND",
        "PALO",
        "PEMBROKE",
        "POMPANO",
        "REDONDO",
        "ROCK",
        "ROUND",
        "SIMI",
        "SILVER",
        "SPRING",
        "STERLING",
        "THOUSAND",
        "UNION",
        "WALNUT",
        "WEST",
        "WHITE",
    ]
)
_TRAILING_LOCATION: Final = re.compile(
    r"(?:\s+(?P<city2>[A-Z][A-Z.'\-]{1,24}))?\s+(?P<city>[A-Z][A-Z.'\-]{1,24})"
    r"\s+(?P<state>[A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s*$"
)

#: Label for the n-th account of a type when nicknames are hidden: A..Z, AA..
_LETTERS: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True)
class MerchantLocation:
    text: str
    city: str | None
    state: str | None


def split_merchant_location(text: str) -> MerchantLocation:
    """``"STARBUCKS #1234 SEATTLE WA"`` -> text ``"STARBUCKS #1234"``, SEATTLE, WA.

    Only an upper-case trailing ``CITY ST`` with a real US state code is
    recognised; a two-word city is taken when its first word is a common
    city prefix (``SAN``, ``NEW``, ...). Anything else is left alone.
    """
    match = _TRAILING_LOCATION.search(text)
    if match is None or match.group("state") not in US_STATES:
        return MerchantLocation(text, None, None)
    city = match.group("city")
    start = match.start("city")
    city2 = match.group("city2")
    if city2 is not None and city2 in CITY_PREFIXES:
        city = f"{city2} {city}"
        start = match.start("city2")
    rest = text[:start].rstrip(" ,-")
    if not rest:
        return MerchantLocation(text, None, None)
    return MerchantLocation(rest, city, match.group("state"))


def _letters(index: int) -> str:
    out = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        out = _LETTERS[rem] + out
    return out


def type_label(account_type: str) -> str:
    """``credit_card`` -> ``Credit card``."""
    return account_type.replace("_", " ").capitalize()


def account_labels(accounts: Iterable[tuple[object, str]]) -> dict[object, str]:
    """Hidden-nickname labels: ``{id: "Checking A"}`` by type, in the given order."""
    counts: dict[str, int] = {}
    labels: dict[object, str] = {}
    for key, account_type in accounts:
        n = counts.get(account_type, 0)
        counts[account_type] = n + 1
        labels[key] = f"{type_label(account_type)} {_letters(n)}"
    return labels


@dataclass(frozen=True)
class ClientPolicy:
    """The exposure policy of one AI client (module doc)."""

    mode: AiMode = AiMode.DETAIL
    rounding: int | None = None
    hide_nicknames: bool = False
    hide_merchant_location: bool = False
    date_range_cap: int | None = None
    custom_denylist: Sequence[str] = field(default_factory=tuple)
    member_aliases: Sequence[str] = field(default_factory=tuple)

    @classmethod
    def of(cls, client: AiClient) -> ClientPolicy:
        return cls(
            mode=client.mode,
            rounding=client.rounding,
            hide_nicknames=client.hide_nicknames,
            hide_merchant_location=client.hide_merchant_location,
            date_range_cap=client.date_range_cap,
            custom_denylist=tuple(client.custom_denylist or ()),
            member_aliases=tuple(client.member_aliases or ()),
        )

    @property
    def aggregates(self) -> bool:
        return self.mode is AiMode.AGGREGATES

    @property
    def labels_hidden(self) -> bool:
        return self.hide_nicknames or self.aggregates

    def round(self, amount: Decimal) -> Decimal:
        if self.rounding is None:
            return amount
        step = Decimal(self.rounding)
        return (amount / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step

    def money(self, amount: Decimal | str, currency: str) -> Money:
        value = Decimal(amount)
        if self.rounding is None:
            return Money.of(value, currency)
        rounded = self.round(value).quantize(Decimal(1))
        if rounded == 0:
            rounded = Decimal(0)  # never "-0"
        return Money(amount=str(rounded), currency=currency)

    def earliest(self, today: date) -> date | None:
        """The first day this client may see, ``None`` without a cap."""
        if self.date_range_cap is None:
            return None
        return today - timedelta(days=self.date_range_cap)

    def clamp_start(self, start: date, today: date) -> date:
        first = self.earliest(today)
        return max(start, first) if first is not None else start

    def payee_text(self, scrubber: Scrubber, text: str | None) -> str | None:
        """A payee or merchant string: scrubbed, location cut when hidden."""
        cleaned = scrubber.scrub(text)
        if cleaned is None or not self.hide_merchant_location:
            return cleaned
        return split_merchant_location(cleaned).text

# SPDX-License-Identifier: AGPL-3.0-or-later
"""A 24-month household ledger with planted recurring series and decoys.

The yardstick for subscription detection, written before the detector it
measures and not by its authors. No lane edits this file; a label believed
wrong is reported to the coordinator.

Three accounts (``checking``, ``card``, ``joint``) from 1 November 2024 to
:data:`TODAY`. Merchant names are real public brands spelled as banks print
them; every number, town, employer, landlord, local business and person is
invented, and no row is copied from a statement.

* **40 planted series** (:class:`Planted`): 22 monthly with a fixed amount
  (rent and a car loan among them, and two plans billed under one opaque
  descriptor), 2 monthly phone bills that drift a few per cent, 3 utilities
  whose amount swings with the season (two monthly, one every two months), 4
  annual (one seen once), 3 weekly, 2 fortnightly, 2 quarterly and 2
  half-yearly. Five change price once, four stop for good, one starts with a
  trial charge. ``expect`` is the least a detector that follows the design
  note should say: ``active``, or ``suggested`` where the evidence is thin
  (one annual charge) or the note's own rule says so (a weekly meal kit is
  groceries, a habit category).
* **25 decoys** (:class:`Decoy`): shopping habits (groceries, coffee, fuel,
  fast food, parking, fares), money moved between the household's own
  accounts, card payments, cash, a bank fee, card interest, and money sent to
  people. None is a subscription or a bill. Rent sent to a housemate through
  a person-to-person service may be suggested, never shown as active.
* Salary (``members["salary"]``) is income: neither planted nor a decoy.
  The remaining rows are one-off purchases.

The ``joint`` account was last uploaded in July 2026 (:attr:`Ledger.coverage`)
although its bills run on: nothing on it is "missed" or "ended". The four
stopped series are on the card, which has rows to the end.

``Row.category`` is the default ``system_key`` the row would carry once
categorised (a known merchant's, or what a person chose for a local
business), or ``None`` where nothing says. :attr:`Ledger.members` maps each
series name to the indexes of its rows in :attr:`Ledger.rows`.
"""

from __future__ import annotations

import calendar
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, Literal

TODAY: Final = date(2026, 10, 31)
START: Final = date(2024, 11, 1)

Cadence = Literal[
    "weekly", "fortnightly", "monthly", "two_monthly", "quarterly", "half_yearly", "annual"
]

_SEED: Final = 20261031
_ALNUM: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"
#: The last day each account has rows for (the statements uploaded so far).
_UPLOADED_TO: Final = {
    "checking": date(2026, 10, 30),
    "card": date(2026, 10, 30),
    "joint": date(2026, 7, 24),
}
_MONTHS: Final[dict[str, int]] = {
    "monthly": 1,
    "two_monthly": 2,
    "quarterly": 3,
    "half_yearly": 6,
    "annual": 12,
}
_DAYS: Final[dict[str, int]] = {"weekly": 7, "fortnightly": 14, "four_weekly": 28}
#: The cadence a series is labelled with (every four weeks reads as monthly).
_LABEL: Final[dict[str, Cadence]] = {
    "weekly": "weekly",
    "fortnightly": "fortnightly",
    "four_weekly": "monthly",
    "monthly": "monthly",
    "two_monthly": "two_monthly",
    "quarterly": "quarterly",
    "half_yearly": "half_yearly",
    "annual": "annual",
}
#: How much of a heating or cooling bill each month carries (January first).
_WINTER: Final = (100, 92, 70, 45, 28, 20, 18, 18, 24, 42, 68, 94)
_SUMMER: Final = (48, 44, 40, 42, 58, 84, 100, 97, 76, 50, 42, 46)
_FLAT: Final = (60, 58, 62, 70, 82, 96, 100, 98, 84, 68, 60, 58)


@dataclass(frozen=True)
class Row:
    account: str
    posted: date
    amount: Decimal
    text: str
    category: str | None


@dataclass(frozen=True)
class Planted:
    name: str
    account: str
    kind: Literal["subscription", "bill"]
    cadence: Cadence
    expect: Literal["active", "suggested"]
    #: The day of the first charge at the new price.
    price_change_on: date | None = None
    #: The day of the last charge of a series that stopped for good.
    stopped_after: date | None = None


@dataclass(frozen=True)
class Decoy:
    name: str
    account: str


@dataclass(frozen=True)
class Ledger:
    rows: tuple[Row, ...]
    planted: tuple[Planted, ...]
    decoys: tuple[Decoy, ...]
    #: Account to the date of its latest row.
    coverage: dict[str, date]
    #: Series name (planted, decoy or ``salary``) to the indexes of its rows.
    members: dict[str, tuple[int, ...]]


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    last = calendar.monthrange(year, month + 1)[1]
    return date(year, month + 1, min(day.day, last))


def _cents(value: str | Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


class _Builder:
    def __init__(self) -> None:
        self._random = random.Random(_SEED)
        self.entries: list[tuple[Row, str | None]] = []
        self.planted: list[Planted] = []
        self.decoys: list[Decoy] = []

    # Only ``random()`` is used: its sequence for a seed is stable across versions.
    def below(self, limit: int) -> int:
        return min(int(self._random.random() * limit), limit - 1)

    def between(self, low: int, high: int) -> int:
        return low + self.below(high - low + 1)

    def money(self, low: str, high: str) -> Decimal:
        a, b = int(Decimal(low) * 100), int(Decimal(high) * 100)
        return Decimal(self.between(a, b)) / 100

    def fill(self, text: str, day: date) -> str:
        out = text.replace("{d}", f"{day.month:02d}/{day.day:02d}")
        while "%" in out:
            out = out.replace("%", "".join(_ALNUM[self.below(len(_ALNUM))] for _ in range(8)), 1)
        return out

    def row(
        self,
        owner: str | None,
        account: str,
        day: date,
        amount: Decimal,
        text: str,
        category: str | None,
    ) -> bool:
        """Add a row unless the account has no statement for that day."""
        if not START <= day <= _UPLOADED_TO[account]:
            return False
        self.entries.append(
            (Row(account, day, _cents(amount), self.fill(text, day), category), owner)
        )
        return True

    def _posted(self, account: str, nominal: date) -> date:
        """A bank debit waits for a working day; a card charge posts a day or two late."""
        if account == "card":
            return nominal + timedelta(days=self.below(3))
        while nominal.weekday() >= 5:
            nominal += timedelta(days=1)
        return nominal

    def series(
        self,
        name: str,
        account: str,
        text: str,
        amount: str,
        cadence: str,
        start: date,
        *,
        kind: Literal["subscription", "bill"],
        category: str | None,
        expect: Literal["active", "suggested"] = "active",
        change: tuple[date, str] | None = None,
        stop: date | None = None,
        drift: str | None = None,
        season: tuple[tuple[int, ...], str] | None = None,
        trial: tuple[int, str] | None = None,
    ) -> None:
        """A planted series: ``amount`` every ``cadence`` from ``start``.

        ``change`` is (from this day, the new amount); ``stop`` the last day a
        charge may fall on; ``drift`` a fraction the amount wanders by;
        ``season`` (monthly weights, the lowest amount) a bill between that and
        ``amount``; ``trial`` (days before the first charge, the trial amount).
        """
        posted: list[tuple[date, Decimal]] = []
        if trial is not None:
            posted.append((start - timedelta(days=trial[0]), Decimal(trial[1])))
        step = 0
        while True:
            if cadence in _DAYS:
                nominal = start + timedelta(days=_DAYS[cadence] * step)
            else:
                nominal = _add_months(start, _MONTHS[cadence] * step)
            step += 1
            if nominal > (stop or TODAY):
                break
            value = Decimal(amount)
            if change is not None and nominal >= change[0]:
                value = Decimal(change[1])
            if drift is not None:
                span = int(value * Decimal(drift) * 100)
                value += Decimal(self.between(-span, span)) / 100
            if season is not None:
                weights, lowest = season
                low = Decimal(lowest)
                share = Decimal(weights[nominal.month - 1] + self.between(-8, 8)) / 100
                value = low + (value - low) * max(share, Decimal(0))
            day = nominal if cadence in _DAYS else self._posted(account, nominal)
            posted.append((day, value))
        kept = [
            (day, value)
            for day, value in posted
            if self.row(name, account, day, -value, text, category)
        ]
        changed = None
        if change is not None:
            changed = min(day for day, value in kept if value == Decimal(change[1]))
        self.planted.append(
            Planted(
                name=name,
                account=account,
                kind=kind,
                cadence=_LABEL[cadence],
                expect=expect,
                price_change_on=changed,
                stopped_after=max(day for day, _ in kept) if stop is not None else None,
            )
        )

    def habit(
        self,
        name: str,
        account: str,
        texts: tuple[str, ...],
        category: str | None,
        *,
        gap: tuple[int, int],
        amount: tuple[str, str],
        sign: int = -1,
    ) -> None:
        """A decoy: purchases a few days apart, never the same amount twice running."""
        day = START + timedelta(days=self.below(gap[1] + 1))
        while day <= TODAY:
            text = texts[self.below(len(texts))]
            self.row(name, account, day, sign * self.money(*amount), text, category)
            day += timedelta(days=self.between(*gap))
        self.decoys.append(Decoy(name, account))

    def monthly_decoy(
        self,
        name: str,
        account: str,
        text: str,
        category: str | None,
        *,
        day: int,
        amount: tuple[str, str],
    ) -> None:
        """A decoy that is regular but is not spending on a service."""
        nominal = START.replace(day=day)
        while nominal <= TODAY:
            value = self.money(*amount)
            self.row(name, account, self._posted(account, nominal), -value, text, category)
            nominal = _add_months(nominal, 1)
        self.decoys.append(Decoy(name, account))


def _plant(b: _Builder) -> None:
    sub: Final = "subscription"
    bill: Final = "bill"
    streaming = "leisure.subscriptions"

    # --- 22 monthly, fixed ---------------------------------------------------
    b.series(
        "netflix",
        "card",
        "NETFLIX.COM",
        "15.49",
        "monthly",
        date(2024, 11, 12),
        kind=sub,
        category=streaming,
        change=(date(2026, 2, 1), "17.99"),
    )
    b.series(
        "spotify",
        "card",
        "SPOTIFY USA",
        "10.99",
        "monthly",
        date(2024, 11, 3),
        kind=sub,
        category=streaming,
        change=(date(2025, 7, 1), "12.99"),
    )
    b.series(
        "hulu",
        "card",
        "HLU*HULU %-U HULU.COM/BILL CA",
        "7.99",
        "monthly",
        date(2024, 11, 21),
        kind=sub,
        category=streaming,
        stop=date(2026, 3, 31),
    )
    b.series(
        "disney-plus",
        "card",
        "DISNEY PLUS 888-555-0190 CA",
        "13.99",
        "monthly",
        date(2024, 11, 8),
        kind=sub,
        category=streaming,
    )
    b.series(
        "youtube-premium",
        "card",
        "GOOGLE *YouTubePremium g.co/helppay# CA",
        "13.99",
        "monthly",
        date(2024, 11, 27),
        kind=sub,
        category=streaming,
    )
    # Two plans under one descriptor that names no service.
    b.series(
        "apple-storage",
        "card",
        "APPLE.COM/BILL 866-555-0177 CA",
        "2.99",
        "monthly",
        date(2024, 11, 4),
        kind=sub,
        category=None,
    )
    b.series(
        "apple-music",
        "card",
        "APPLE.COM/BILL 866-555-0177 CA",
        "10.99",
        "monthly",
        date(2024, 11, 19),
        kind=sub,
        category=None,
    )
    b.series(
        "new-york-times",
        "card",
        "NYTIMES*NYTIMES DISC",
        "17.00",
        "four_weekly",
        date(2024, 11, 6),
        kind=sub,
        category="leisure.news",
    )
    b.series(
        "adobe",
        "card",
        "ADOBE *CREATIVE CLOUD",
        "22.99",
        "monthly",
        date(2024, 11, 15),
        kind=sub,
        category="leisure.software",
    )
    b.series(
        "openai",
        "card",
        "OPENAI *CHATGPT SUBSCR",
        "20.00",
        "monthly",
        date(2025, 5, 23),
        kind=sub,
        category="leisure.software",
    )
    b.series(
        "amazon-prime",
        "card",
        "Amazon Prime*% Amzn.com/bill WA",
        "14.99",
        "monthly",
        date(2024, 11, 9),
        kind=sub,
        category="leisure.memberships",
    )
    b.series(
        "peloton",
        "card",
        "PELOTON* MEMBERSHIP",
        "44.00",
        "monthly",
        date(2024, 11, 17),
        kind=sub,
        category="health.fitness",
        stop=date(2025, 12, 31),
    )
    # A local business nobody's dictionary knows; a person filed it under fitness.
    b.series(
        "climbing-gym",
        "card",
        "SQ *IRONWORKS CLIMBING",
        "65.00",
        "monthly",
        date(2024, 11, 1),
        kind=sub,
        category="health.fitness",
    )
    b.series(
        "xbox-game-pass",
        "card",
        "MICROSOFT*XBOX GAME PASS",
        "16.99",
        "monthly",
        date(2024, 11, 24),
        kind=sub,
        category="leisure.games",
        change=(date(2025, 10, 1), "19.99"),
    )
    b.series(
        "paramount-plus",
        "card",
        "PARAMOUNT+ 888-555-0125 CA",
        "11.99",
        "monthly",
        date(2026, 4, 10),
        kind=sub,
        category=streaming,
        trial=(7, "1.00"),
    )
    b.series(
        "linkedin-premium",
        "card",
        "LINKEDIN PRE*%",
        "39.99",
        "monthly",
        date(2025, 3, 20),
        kind=sub,
        category=None,
        stop=date(2026, 6, 30),
    )
    b.series(
        "planet-fitness",
        "checking",
        "PLANET FIT CLUB FEES PPD ID: 7019342250",
        "24.99",
        "monthly",
        date(2024, 11, 17),
        kind=sub,
        category="health.fitness",
    )
    b.series(
        "geico",
        "checking",
        "GEICO PREM COLL PPD ID: 4408815207",
        "128.40",
        "monthly",
        date(2024, 11, 14),
        kind=bill,
        category="transport.car_insurance",
        change=(date(2026, 5, 1), "141.15"),
    )
    b.series(
        "life-insurance",
        "joint",
        "NORTHWESTERN MU ISA PYMNT PPD ID: 6120097731",
        "46.20",
        "monthly",
        date(2024, 11, 20),
        kind=bill,
        category="financial.insurance",
    )
    b.series(
        "comcast",
        "joint",
        "COMCAST CABLE WEB ID: 3305571846",
        "79.99",
        "monthly",
        date(2024, 11, 7),
        kind=bill,
        category="utilities.internet",
        change=(date(2025, 12, 1), "89.99"),
    )
    b.series(
        "rent",
        "checking",
        "HOLLOWBROOK APTS DES:RENT ID:U4471902 CO ID:8230116954 WEB",
        "1850.00",
        "monthly",
        date(2024, 11, 1),
        kind=bill,
        category="housing.rent",
    )
    b.series(
        "car-loan",
        "checking",
        "TOYOTA FINANCIAL DES:RETAIL PMT ID:0%0 CO ID:2917740365 PPD",
        "412.37",
        "monthly",
        date(2024, 11, 5),
        kind=bill,
        category="transfers.loan_payment",
    )

    # --- monthly, drifting a few per cent --------------------------------------
    b.series(
        "att",
        "checking",
        "ATT DES:Payment ID:%001 CO ID:5104482996 PPD",
        "73.20",
        "monthly",
        date(2024, 11, 22),
        kind=bill,
        category="utilities.phone",
        drift="0.03",
    )
    b.series(
        "xfinity-mobile",
        "joint",
        "XFINITY MOBILE 888-555-0193 PA",
        "32.50",
        "monthly",
        date(2024, 11, 26),
        kind=bill,
        category="utilities.phone",
        drift="0.04",
    )

    # --- utilities whose amount follows the season -----------------------------
    b.series(
        "electricity",
        "checking",
        "DUKEENERGY DES:BILL PAY ID:%91 CO ID:3391207745 WEB",
        "212.00",
        "monthly",
        date(2024, 11, 18),
        kind=bill,
        category="utilities.electricity",
        season=(_SUMMER, "38.00"),
    )
    b.series(
        "heating-gas",
        "joint",
        "PIEDMONT NG DES:DRAFT ID:%4 CO ID:7702295513 PPD",
        "148.00",
        "monthly",
        date(2024, 11, 11),
        kind=bill,
        category="utilities.heating",
        season=(_WINTER, "9.00"),
    )
    b.series(
        "water",
        "joint",
        "ASHGROVE WATER DIST BILLPAY",
        "124.00",
        "two_monthly",
        date(2024, 11, 13),
        kind=bill,
        category="utilities.water",
        season=(_FLAT, "22.00"),
    )

    # --- 4 annual, one of them seen once ----------------------------------------
    # The gym's yearly fee: its usual descriptor, another amount, another day.
    b.series(
        "planet-fitness-annual-fee",
        "checking",
        "PLANET FIT CLUB FEES PPD ID: 7019342250",
        "49.00",
        "annual",
        date(2025, 2, 3),
        kind=sub,
        category="health.fitness",
    )
    b.series(
        "duolingo",
        "card",
        "DUOLINGO 412-555-0131 PA",
        "83.99",
        "annual",
        date(2025, 6, 14),
        kind=sub,
        category="family.education",
    )
    b.series(
        "costco-membership",
        "card",
        "COSTCO *ANNUAL RENEWAL",
        "65.00",
        "annual",
        date(2025, 3, 22),
        kind=sub,
        category="leisure.memberships",
    )
    b.series(
        "dropbox",
        "card",
        "DROPBOX*%",
        "119.88",
        "annual",
        date(2026, 1, 14),
        kind=sub,
        category="leisure.software",
        expect="suggested",
    )

    # --- 3 weekly ------------------------------------------------------------------
    b.series(
        "kindercare",
        "checking",
        "KINDERCARE LEARNING DES:TUITION ID:%1 CO ID:9054417720 PPD",
        "310.00",
        "weekly",
        date(2024, 11, 4),
        kind=bill,
        category="family.childcare",
    )
    # A meal kit is groceries, a habit category: the design note allows "suggested".
    b.series(
        "hellofresh",
        "card",
        "HELLOFRESH 646-555-0116 NY",
        "71.93",
        "weekly",
        date(2026, 2, 7),
        kind=sub,
        category="food.groceries",
        expect="suggested",
    )
    b.series(
        "swim-school",
        "card",
        "SQ *RIVERBEND SWIM SCHOOL",
        "32.00",
        "weekly",
        date(2025, 9, 3),
        kind=sub,
        category="leisure.hobbies",
    )

    # --- 2 fortnightly ---------------------------------------------------------------
    b.series(
        "anytime-fitness",
        "card",
        "ABC*ANYTIME FITNESS",
        "21.99",
        "fortnightly",
        date(2024, 11, 8),
        kind=sub,
        category="health.fitness",
        stop=date(2025, 9, 30),
    )
    b.series(
        "after-school",
        "checking",
        "LITTLE OAKS ACADEMY DES:TUITION ID:%7 CO ID:6648120093 PPD",
        "185.00",
        "fortnightly",
        date(2025, 8, 22),
        kind=bill,
        category="family.childcare",
    )

    # --- 2 quarterly ------------------------------------------------------------------
    b.series(
        "waste-management",
        "joint",
        "WASTE MGMT WM EZPAY",
        "96.45",
        "quarterly",
        date(2024, 11, 15),
        kind=bill,
        category="utilities.other",
    )
    b.series(
        "sewer",
        "joint",
        "MAPLE RIDGE SEWER DIST PPD ID: 1187305526",
        "148.20",
        "quarterly",
        date(2024, 12, 9),
        kind=bill,
        category="utilities.water",
    )

    # --- 2 half-yearly ----------------------------------------------------------------
    b.series(
        "progressive",
        "checking",
        "PROG DIRECT INS DES:INS PREM ID:%22 CO ID:3075591184 PPD",
        "318.00",
        "half_yearly",
        date(2025, 1, 9),
        kind=bill,
        category="transport.car_insurance",
    )
    b.series(
        "siriusxm",
        "card",
        "SXM*SIRIUSXM.COM/ACCT",
        "68.94",
        "half_yearly",
        date(2024, 12, 2),
        kind=sub,
        category=streaming,
    )


def _decoys(b: _Builder) -> None:
    # --- shopping habits ----------------------------------------------------------
    b.habit(
        "groceries-weekly",
        "card",
        ("KROGER #0412 ASHGROVE OH",),
        "food.groceries",
        gap=(5, 9),
        amount=("62.00", "184.00"),
    )
    b.habit(
        "coffee",
        "card",
        ("STARBUCKS STORE 08213 ASHGROVE OH", "STARBUCKS STORE 11740 PINE HOLLOW OH"),
        "food.coffee",
        gap=(1, 4),
        amount=("4.25", "9.80"),
    )
    b.habit(
        "fuel-weekly",
        "card",
        ("SHELL OIL 57442190041 ASHGROVE OH",),
        "transport.fuel",
        gap=(5, 10),
        amount=("34.00", "61.00"),
    )
    b.habit(
        "restaurant-monthly",
        "card",
        ("TST* MARIGOLD KITCHEN ASHGROVE OH",),
        "food.restaurants",
        gap=(24, 38),
        amount=("48.00", "96.00"),
    )
    b.habit(
        "groceries-second-store",
        "card",
        ("TRADER JOE'S #552 PINE HOLLOW OH",),
        "food.groceries",
        gap=(10, 18),
        amount=("38.00", "112.00"),
    )
    b.habit(
        "costco-runs",
        "card",
        ("COSTCO WHSE #0388 PINE HOLLOW OH",),
        None,
        gap=(22, 40),
        amount=("118.00", "312.00"),
    )
    b.habit(
        "amazon-orders",
        "card",
        ("AMZN Mktp US*%", "AMAZON.COM*% AMZN.COM/BILL WA"),
        None,
        gap=(3, 16),
        amount=("8.00", "140.00"),
    )
    b.habit(
        "pharmacy",
        "card",
        ("CVS/PHARMACY #08123 ASHGROVE OH",),
        "health.pharmacy",
        gap=(18, 45),
        amount=("6.00", "48.00"),
    )
    b.habit(
        "fast-food",
        "card",
        ("MCDONALD'S F12841 ASHGROVE OH",),
        "food.fast_food",
        gap=(4, 11),
        amount=("6.80", "14.20"),
    )
    b.habit(
        "delivery",
        "card",
        ("DD *DOORDASH PANDAEXPR", "DD *DOORDASH THAIBASIL", "DD *DOORDASH CHIPOTLE"),
        "food.delivery",
        gap=(4, 12),
        amount=("18.00", "46.00"),
    )
    b.habit(
        "rideshare",
        "card",
        ("UBER *TRIP", "UBER *TRIP HELP.UBER.COM"),
        "transport.taxi",
        gap=(3, 20),
        amount=("9.00", "38.00"),
    )
    b.habit(
        "parking", "card", ("PARKMOBILE",), "transport.parking", gap=(1, 5), amount=("2.75", "8.25")
    )
    # One fare, always the same amount, several times a week.
    b.habit(
        "transit-fares",
        "card",
        ("MTA*NYCT PAYGO NEW YORK NY",),
        "transport.public_transit",
        gap=(0, 3),
        amount=("2.90", "2.90"),
    )
    b.habit(
        "liquor-store",
        "card",
        ("TOTAL WINE AND MORE 0214 PINE HOLLOW OH",),
        "food.alcohol",
        gap=(10, 20),
        amount=("19.00", "64.00"),
    )
    b.habit(
        "lunch",
        "card",
        ("CHIPOTLE 2291 ASHGROVE OH",),
        "food.fast_food",
        gap=(5, 9),
        amount=("11.85", "14.60"),
    )
    b.habit(
        "target-runs",
        "card",
        ("TARGET 00012841 ASHGROVE OH",),
        None,
        gap=(9, 19),
        amount=("24.00", "140.00"),
    )

    # --- money that is not spending -----------------------------------------------
    b.monthly_decoy(
        "savings-transfer",
        "checking",
        "ONLINE TRANSFER TO SAV ...7731",
        "transfers.savings",
        day=2,
        amount=("500.00", "500.00"),
    )
    b.monthly_decoy(
        "card-payment",
        "checking",
        "CHASE CREDIT CRD DES:AUTOPAY ID:%0 CO ID:5512094473 PPD",
        "transfers.credit_card_payment",
        day=25,
        amount=("900.00", "2400.00"),
    )
    b.monthly_decoy(
        "card-payment-joint",
        "joint",
        "CAPITAL ONE DES:CRCARDPMT ID:%3 CO ID:8840127365 WEB",
        "transfers.credit_card_payment",
        day=18,
        amount=("150.00", "700.00"),
    )
    b.monthly_decoy(
        "brokerage-transfer",
        "checking",
        "FID BKG SVC LLC DES:MONEYLINE ID:%5 CO ID:2260418837 PPD",
        "transfers.investments",
        day=16,
        amount=("250.00", "250.00"),
    )
    b.monthly_decoy(
        "bank-fee",
        "checking",
        "MONTHLY MAINTENANCE FEE",
        "financial.bank_fees",
        day=28,
        amount=("12.00", "12.00"),
    )
    b.monthly_decoy(
        "card-interest",
        "card",
        "INTEREST CHARGE ON PURCHASES",
        "financial.interest_paid",
        day=27,
        amount=("8.00", "41.00"),
    )
    b.habit(
        "cash",
        "checking",
        ("ATM WITHDRAWAL 004417 {d} 400 ELM ST ASHGROVE OH",),
        "transfers.cash",
        gap=(12, 16),
        amount=("100.00", "100.00"),
    )

    # --- money between people -------------------------------------------------------
    # A housemate's share of the rent: regular and fixed, and still not a subscription.
    b.monthly_decoy(
        "rent-share-zelle",
        "checking",
        "ZELLE PAYMENT TO DESMOND KILLIGREW %001",
        None,
        day=1,
        amount=("600.00", "600.00"),
    )
    b.habit(
        "venmo-friends",
        "checking",
        ("VENMO PAYMENT 10%4 WEB ID: 7730915528",),
        None,
        gap=(20, 45),
        amount=("15.00", "120.00"),
    )


_NOISE_FIRST: Final = (
    "HOLLOW CREEK", "GILDED FERN", "NORTHWIND", "KESTREL", "TWO RIVERS", "FOXGLOVE",
    "IRON GATE", "PAPER LANTERN", "BRASS COMPASS", "WILDFLOWER", "STARLING", "COPPERLINE",
    "OSPREY POINT", "TIN ROOSTER", "BRAMBLEWOOD", "SILVER BIRCH", "CURIOUS CROW",
    "PINECONE", "RIVERSTONE", "INDIGO DOOR",
)  # fmt: skip
_NOISE_SECOND: Final = (
    "TRADING", "SUPPLY CO", "WORKS", "MERCANTILE", "STUDIO", "COLLECTIVE", "OUTFITTERS",
    "GOODS", "PROVISIONS", "EMPORIUM",
)  # fmt: skip
_NOISE_TOWNS: Final = ("ASHGROVE OH", "PINE HOLLOW OH", "MAPLE RIDGE WA", "SANTA LUMA CA")


def _others(b: _Builder) -> None:
    # Salary: income, neither planted nor a decoy.
    day = date(2024, 11, 8)
    while day <= TODAY:
        b.row(
            "salary",
            "checking",
            day,
            Decimal("2415.37"),
            "LARKSPUR DESIGN GRP DES:PAYROLL ID:%2 CO ID:9471203358 PPD",
            "income.salary",
        )
        day += timedelta(days=14)
    # One-off purchases: every shop is seen once.
    names = [f"{first} {second}" for first in _NOISE_FIRST for second in _NOISE_SECOND]
    for index, name in enumerate(names):
        account = ("card", "card", "checking", "joint")[index % 4]
        span = (_UPLOADED_TO[account] - START).days
        when = START + timedelta(days=b.below(span + 1))
        prefix = ("", "SQ *", "", "POS DEBIT ")[b.below(4)]
        town = _NOISE_TOWNS[b.below(len(_NOISE_TOWNS))]
        b.row(None, account, when, -b.money("5.00", "250.00"), f"{prefix}{name} {town}", None)


def build() -> Ledger:
    """The ledger: the same rows, in the same order, every time."""
    b = _Builder()
    _plant(b)
    _decoys(b)
    _others(b)
    # The joint account's last statement ends on a day that has a row.
    b.row(
        None,
        "joint",
        _UPLOADED_TO["joint"],
        Decimal("-23.80"),
        "POS DEBIT LAKESIDE GARDEN CENTER ASHGROVE OH",
        None,
    )
    order = sorted(
        range(len(b.entries)),
        key=lambda i: (b.entries[i][0].posted, b.entries[i][0].account, b.entries[i][0].text, i),
    )
    rows = tuple(b.entries[i][0] for i in order)
    members: dict[str, list[int]] = {}
    for position, i in enumerate(order):
        owner = b.entries[i][1]
        if owner is not None:
            members.setdefault(owner, []).append(position)
    coverage = {
        account: max(row.posted for row in rows if row.account == account)
        for account in _UPLOADED_TO
    }
    return Ledger(
        rows=rows,
        planted=tuple(b.planted),
        decoys=tuple(b.decoys),
        coverage=coverage,
        members={name: tuple(indexes) for name, indexes in members.items()},
    )

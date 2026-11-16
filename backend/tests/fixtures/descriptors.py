# SPDX-License-Identifier: AGPL-3.0-or-later
"""1,200 labelled bank descriptors: the yardstick for clean-up and categorisation.

Written before the merchant dictionary, the clean-up and the engine, and not
by their authors, so that it measures them instead of mirroring them. No lane
edits this file; a label believed wrong is reported to the coordinator.

**What is real and what is not.** A known merchant's public brand name is
real (a dictionary of real merchants cannot be measured otherwise), spelled
the way card networks and banks print it. Everything else is invented: store
and terminal numbers, reference and trace numbers, company ids, telephone
numbers (all in the fictional ``555-01xx`` range), towns, streets, employers,
local shops and every person. No line is copied from a statement.

**The three groups** (:attr:`Labelled.group`):

* ``known`` (800 lines, :data:`KNOWN_MERCHANTS` merchants): each merchant in
  at least two of the six :data:`SHAPES`: ``bare`` (the network descriptor
  alone), ``pos`` (a debit-card prefix), ``authorized`` (``PURCHASE
  AUTHORIZED ON mm/dd ... CARD nnnn``), ``checkcard`` (``CHECKCARD mmdd ...``
  and a reference), ``ach`` (``DES:`` / ``ORIG CO NAME:`` layouts, with the
  account holder's name in them) and ``processor`` (the merchant behind
  ``PAYPAL *`` or ``GOOGLE *``). :data:`SHAPE_OF` says which line is which.
* ``invented`` (250 lines): 125 local shops that exist nowhere, two lines
  each. About half carry a telling word (``PIZZA``, ``PHARMACY``) and the
  category that word suggests; the others say nothing about what they sell.
* ``non_purchase`` (150 lines): transfers, card payments, loan payments,
  cash, fees, interest, payroll, refunds that name no merchant, cheques,
  deposits and money between people.

**How to read a label.**

* ``merchant`` groups lines: every line with one label must get one merchant
  key, and two labels must not share a key. The key's spelling is not
  prescribed. ``None`` means the line names nobody.
* ``category`` is a default ``system_key``, or ``None`` for "must not be
  stated as a fact". ``None`` on a ``known`` line marks a store that sells
  everything (Amazon, Walmart, a convenience store with pumps, an insurer
  with several lines of business): a suggestion there is not judged. ``None``
  anywhere else means any guess is wrong.
* A category on an ``invented`` line is what its telling word suggests; it is
  expected as a suggestion at most, and a fact there counts as right only if
  it is that category.
* ``kind`` is a ``DescriptorKind``. It is measured on ``non_purchase`` lines
  only; on the others it records the shape (a purchase, a recurring
  purchase, an ACH debit).
* ``private`` holds the invented personal names printed in the line. None of
  their words may reach a merchant key.

Counter-service chains (no table service) are ``food.fast_food``, chains
with table service ``food.restaurants``, coffee and doughnut chains
``food.coffee``. Department and dollar stores are ``shopping.general``;
off-price clothing chains are ``shopping.clothing``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

Group = Literal["known", "invented", "non_purchase"]


@dataclass(frozen=True)
class Labelled:
    text: str
    group: Group
    merchant: str | None
    category: str | None
    kind: str
    sign: Literal[-1, 1] = -1
    private: tuple[str, ...] = ()


#: The leaves of the default category tree once its second edition is in
#: (design note, section 1): the 55 of the first edition and the 13 added.
CATEGORY_KEYS: Final[frozenset[str]] = frozenset(
    f"{group}.{leaf}"
    for group, leaves in {
        "income": "salary bonus interest dividends refunds gifts other",
        "housing": "rent mortgage_interest property_tax insurance maintenance fees improvement",
        "utilities": "electricity heating water internet phone other",
        "food": "groceries restaurants coffee fast_food alcohol delivery",
        "transport": "fuel public_transit parking car_maintenance car_insurance taxi",
        "health": "medical pharmacy dental insurance fitness",
        "shopping": "clothing electronics household general home",
        "leisure": "subscriptions hobbies events travel games news software memberships",
        "family": "personal_care education childcare pets gifts donations",
        "financial": "bank_fees interest_paid taxes insurance services",
        "transfers": "between_accounts credit_card_payment savings investments loan_payment cash",
        "excluded": "reimbursable",
    }.items()
    for leaf in leaves.split()
)

SHAPES: Final = ("bare", "pos", "authorized", "checkcard", "ach", "processor")

KNOWN_LINES: Final = 800
INVENTED_LINES: Final = 250
NON_PURCHASE_LINES: Final = 150

_ALNUM: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"

#: Invented towns, with real state codes (a bank prints both).
_TOWNS: Final = (
    ("ASHGROVE", "OH"),
    ("PINE HOLLOW", "OH"),
    ("MAPLE RIDGE", "WA"),
    ("ELKTON FALLS", "MT"),
    ("COPPER BLUFF", "AZ"),
    ("SALTMARSH", "SC"),
    ("NORTH WREN", "MN"),
    ("LAKE ORRIN", "MI"),
    ("FOXBURROW", "PA"),
    ("CEDAR STRAND", "OR"),
    ("BELLHAVEN", "NC"),
    ("OLD TARRY", "NY"),
    ("SANTA LUMA", "CA"),
    ("RIO VERDANT", "TX"),
    ("WINDROW", "KS"),
    ("HARLOWTON", "GA"),
    ("MOSSY POINT", "FL"),
    ("EAST DUNMORE", "MA"),
    ("STONEFIELD", "CO"),
    ("BRIAR GLEN", "TN"),
    ("PORT ELWYN", "ME"),
    ("TWIN SPRUCE", "ID"),
    ("LOWER MERRIT", "NJ"),
    ("CALDER CITY", "IL"),
)

_STREETS: Final = (
    "400 ELM ST",
    "1217 ORCHARD AVE",
    "88 FERRY RD",
    "2301 N MILLRACE BLVD",
    "9 COURT SQ",
    "615 W LANTERN WAY",
)

#: Invented people. Their words never appear in a merchant label (a test checks).
_PEOPLE: Final = (
    "ODALYS BRENNICK",
    "TOBIAS VANTREESE",
    "MIRELA OSTRANDER",
    "CASPIAN WELLBROOK",
    "ANNELIESE DRUMGOOLE",
    "LEOPOLD ASHWORTHY",
    "SUNNIVA HALVORSEN",
    "THADDEUS OKPARA",
    "ROSALIND QUENNELL",
    "EVANDER MCCLATCHY",
    "PERPETUA LINDQVIST",
    "BARNABY TRELAWNEY",
    "IMOGEN FAIRWEATHER",
    "LUCIAN ABERNATHY",
    "OTTILIE VANDERHOEK",
    "CORMAC DELACROIX",
    "ZENOBIA HARTSOUGH",
    "AUGUSTIN PELLEGRINO",
    "WILHELMINA STROUD",
    "DESMOND KILLIGREW",
    "HENRIETTA BLOXHAM",
    "ALARIC THISTLEDOWN",
    "SERAPHINA NAKAGAWA",
    "GIDEON MOLYNEUX",
    "BEATRIX OYELARAN",
    "FERDINAND ZABRISKIE",
    "CORDELIA ROSSITER",
    "LAZARUS WETHERELL",
    "PHILIPPA ANDRZEJEWSKI",
    "MAXIMILIAN GROSVENOR",
)

#: Invented employers (payroll lines).
_EMPLOYERS: Final = (
    "LARKSPUR DESIGN GRP",
    "NORTHWIND LOGISTICS",
    "CALDERA SYSTEMS INC",
    "BRIGHTWATER SCHOOLS",
    "HOLLIS & DRAKE LLP",
    "PINNACLE STAFFING",
    "OAKHAVEN MEDICAL CTR",
)


class _Dice:
    """Repeatable choices from a seed string (a hash stream: no ``random``)."""

    def __init__(self, seed: str) -> None:
        self._seed = seed
        self._count = 0

    def below(self, limit: int) -> int:
        digest = hashlib.sha256(f"{self._seed}:{self._count}".encode()).digest()
        self._count += 1
        return int.from_bytes(digest[:8], "big") % limit

    def pick[T](self, options: Sequence[T]) -> T:
        return options[self.below(len(options))]

    def digits(self, count: int) -> str:
        return "".join(str(self.below(10)) for _ in range(count))

    def alnum(self, count: int) -> str:
        return "".join(self.pick(_ALNUM) for _ in range(count))

    def mmdd(self, sep: str = "/") -> str:
        return f"{1 + self.below(12):02d}{sep}{1 + self.below(28):02d}"

    def yymmdd(self) -> str:
        return f"2{5 + self.below(2)}{1 + self.below(12):02d}{1 + self.below(28):02d}"

    def phone(self) -> str:
        area = self.pick(("800", "833", "844", "855", "866", "877", "888"))
        return f"{area}-555-01{11 + self.below(89):02d}"

    def town(self) -> tuple[str, str]:
        return self.pick(_TOWNS)

    def person(self) -> str:
        return self.pick(_PEOPLE)

    def fill(self, pattern: str) -> str:
        """``@`` becomes a four-digit store number, ``%`` an eight-character reference."""
        out = []
        for ch in pattern:
            if ch == "@":
                out.append(self.digits(4))
            elif ch == "%":
                out.append(self.alnum(8))
            else:
                out.append(ch)
        return "".join(out)


# ---------------------------------------------------------------------------
# Known merchants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Merchant:
    label: str
    category: str | None
    #: ``s``: a shop (store number, town and state follow); ``o``: online or
    #: billed remotely (sometimes a telephone number and a state); ``-``: neither.
    where: str
    #: The name as the card network prints it (``@`` and ``%``: :meth:`_Dice.fill`).
    prints: tuple[str, ...]
    #: ACH company name and entry description, for merchants that debit an account.
    ach: tuple[str, str] | None = None
    #: The name PayPal prints behind ``PAYPAL *``.
    paypal: str | None = None
    #: Whole processor-fronted names (``GOOGLE *YouTubePremium``).
    fronted: tuple[str, ...] = ()
    recurring: bool = False


def _parse(table: str) -> tuple[_Merchant, ...]:
    """One merchant per line: ``label | category | where | prints | extras``.

    ``prints`` are separated by ``;``. ``extras`` are ``ach=NAME/DESCR``,
    ``pp=NAME``, ``fr=NAME;NAME`` and ``rec``, separated by `` ~ ``. ``-`` is
    "none". A line that starts with ``>`` continues the one before it.
    """
    joined: list[str] = []
    for raw in table.strip().splitlines():
        line = raw.strip()
        if line.startswith(">"):
            more = line[1:].strip()
            joined[-1] += f" {more}" if more.startswith("~") else more
        elif line and not line.startswith("#"):
            joined.append(line)
    out: list[_Merchant] = []
    for line in joined:
        label, category, where, prints, *rest = (part.strip() for part in line.split(" | "))
        ach: tuple[str, str] | None = None
        paypal: str | None = None
        fronted: tuple[str, ...] = ()
        recurring = False
        for extra in rest[0].split(" ~ ") if rest else ():
            name, _, value = extra.strip().partition("=")
            if name == "ach":
                company, _, descr = value.partition("/")
                ach = (company, descr)
            elif name == "pp":
                paypal = value
            elif name == "fr":
                fronted = tuple(v.strip() for v in value.split(";"))
            elif name == "rec":
                recurring = True
            else:  # pragma: no cover - a typo in the table
                raise ValueError(f"unknown extra {extra!r} for {label}")
        out.append(
            _Merchant(
                label=label,
                category=None if category == "-" else category,
                where=where,
                prints=() if prints == "-" else tuple(p.strip() for p in prints.split(";")),
                ach=ach,
                paypal=paypal,
                fronted=fronted,
                recurring=recurring,
            )
        )
    return tuple(out)


_KNOWN: Final = _parse(
    """
# --- stores that sell everything: never a fact -----------------------------
Walmart | - | s | WAL-MART #@;WM SUPERCENTER #@;WALMART.COM;WAL-MART SUPERCENTER @ | pp=WALMART.COM
Target | - | s | TARGET 0001@;TARGET T-@;TARGET.COM * | pp=TARGETCORPO
Costco | - | s | COSTCO WHSE #0@;COSTCO WHSE #1@
Amazon | - | o | AMZN Mktp US*%;AMAZON.COM*%;Amazon.com*% Amzn.com/bill WA;
> AMZN MKTP US*% AMZN.COM/BILL WA
Sam's Club | - | s | SAMSCLUB #@;SAMS CLUB #@;SAM'S CLUB #@
BJ's Wholesale | - | s | BJS WHOLESALE #0@;BJ'S WHOLESALE CLUB @
Apple | - | o | APPLE.COM/BILL;APPLE.COM/BILL 866-555-0177 CA;APPLE COM BILL | rec
Google Play | - | - | - | fr=GOOGLE *Google Play;GOOGLE *GOOGLE PLAY APP;
> GOOGLE*GOOGLE PLAY g.co/helppay# CA
7-Eleven | - | s | 7-ELEVEN @@;7-ELEVEN 3@
Circle K | - | s | CIRCLE K #0@;CIRCLE K # @
Wawa | - | s | WAWA @;WAWA STORE @
Sheetz | - | s | SHEETZ @;SHEETZ 0@
QuikTrip | - | s | QT @;QUIKTRIP #@;QT @ INSIDE
Casey's | - | s | CASEYS #@;CASEY'S GEN STORE @
State Farm | - | o | STATE FARM INSURANCE | ach=STATE FARM RO 27/SFPP ~ rec
Allstate | - | o | ALLSTATE *PAYMENT;ALLSTATE INS CO | ach=ALLSTATE INS CO/INS PREM ~ rec
# --- sub-brands that must beat their parent --------------------------------
Amazon Prime | leisure.memberships | o | Amazon Prime*%;AMAZON PRIME*% Amzn.com/bill WA;
> AMZN PRIME*% | rec
Prime Video | leisure.subscriptions | o | PRIME VIDEO *%;Prime Video Channels*%;
> PRIME VIDEO *% 888-555-0126 WA | rec
Costco Gas | transport.fuel | s | COSTCO GAS #0@;COSTCO GAS #1@
Costco Membership | leisure.memberships | o | COSTCO *ANNUAL RENEWAL;COSTCO *MEMBERSHIP RENEW
Apple Store | shopping.electronics | s | APPLE STORE #R@;APPLE STORE  #R@
Xfinity Mobile | utilities.phone | o | XFINITY MOBILE;
> XFINITY MOBILE 888-555-0193 PA | ach=XFINITY MOBILE/PAYMENT ~ rec
Uber Eats | food.delivery | o | UBER *EATS;UBER EATS;UBER *EATS PENDING;
> UBER * EATS HELP.UBER.COM | pp=UBER EATS
# --- groceries -------------------------------------------------------------
Kroger | food.groceries | s | KROGER #@;KROGER @
Safeway | food.groceries | s | SAFEWAY #@;SAFEWAY STORE 0000@
Trader Joe's | food.groceries | s | TRADER JOE'S #@;TRADER JOE S #@;TRADER JOES #@
Whole Foods | food.groceries | s | WHOLEFDS MKT 1@;WHOLE FOODS MARKET;WHOLEFDS #1@
Aldi | food.groceries | s | ALDI @;ALDI 6@
Publix | food.groceries | s | PUBLIX #@;PUBLIX SUPER MARKETS #@
H-E-B | food.groceries | s | H-E-B #@;HEB #@;H-E-B GROCERY #@
Albertsons | food.groceries | s | ALBERTSONS #@;ALBERTSONS STORE @
Wegmans | food.groceries | s | WEGMANS #@;WEGMANS FOOD MARKETS #@
Food Lion | food.groceries | s | FOOD LION #@
Giant Eagle | food.groceries | s | GIANT EAGLE #@;GIANT-EAGLE #@
Stop & Shop | food.groceries | s | STOP & SHOP @;STOP&SHOP #@
Meijer | food.groceries | s | MEIJER # @;MEIJER STORE #@
Hy-Vee | food.groceries | s | HY-VEE @;HY VEE #@
Sprouts | food.groceries | s | SPROUTS FARMERS MKT#@;SPROUTS FARMERS MAR
Harris Teeter | food.groceries | s | HARRIS TEETER #@
WinCo | food.groceries | s | WINCO FOODS #@
Ralphs | food.groceries | s | RALPHS #@
Vons | food.groceries | s | VONS #@;VONS STORE 0000@
Fred Meyer | food.groceries | s | FRED-MEYER #@;FRED MEYER #@
ShopRite | food.groceries | s | SHOPRITE @;SHOPRITE #@
Lidl | food.groceries | s | LIDL #@
Instacart | food.groceries | o | INSTACART;IC* INSTACART;INSTACART 888-555-0142 CA | pp=INSTACART
# --- fast food (counter service) --------------------------------------------
McDonald's | food.fast_food | s | MCDONALD'S F@;MCDONALDS @;MCDONALD'S M@
Burger King | food.fast_food | s | BURGER KING #@;BURGER KING @
Wendy's | food.fast_food | s | WENDY'S #@;WENDYS @;WENDY'S @
Taco Bell | food.fast_food | s | TACO BELL #0@;TACO BELL @
Chick-fil-A | food.fast_food | s | CHICK-FIL-A #0@;CHICK FIL A @
Subway | food.fast_food | s | SUBWAY @;SUBWAY 000@
KFC | food.fast_food | s | KFC #@;KFC J@
Popeyes | food.fast_food | s | POPEYES @;POPEYES #1@
Arby's | food.fast_food | s | ARBY'S @;ARBYS #@
Sonic Drive-In | food.fast_food | s | SONIC DRIVE IN #@
Jack in the Box | food.fast_food | s | JACK IN THE BOX @
Whataburger | food.fast_food | s | WHATABURGER @
In-N-Out | food.fast_food | s | IN-N-OUT BURGER #@;IN N OUT BURGER @
Five Guys | food.fast_food | s | FIVE GUYS @;FIVE GUYS BURGERS @
Chipotle | food.fast_food | s | CHIPOTLE @;CHIPOTLE ONLINE;CHIPOTLE MEX GR ONLINE
Panda Express | food.fast_food | s | PANDA EXPRESS #@
Domino's | food.fast_food | s | DOMINO'S @;DOMINOS @
Pizza Hut | food.fast_food | s | PIZZA HUT #0@;PIZZA HUT @
Little Caesars | food.fast_food | s | LITTLE CAESARS @
Papa John's | food.fast_food | s | PAPA JOHN'S #@;PAPA JOHNS @
Dairy Queen | food.fast_food | s | DAIRY QUEEN #@
Jimmy John's | food.fast_food | s | JIMMY JOHNS # @;JIMMY JOHN'S @
Jersey Mike's | food.fast_food | s | JERSEY MIKES @;JERSEY MIKE'S SUBS @
Raising Cane's | food.fast_food | s | RAISING CANES #@;RAISING CANE'S @
Wingstop | food.fast_food | s | WINGSTOP @
Zaxby's | food.fast_food | s | ZAXBY'S #@
Shake Shack | food.fast_food | s | SHAKE SHACK @
Panera | food.fast_food | s | PANERA BREAD #@;PANERA BREAD #20@ P
# --- coffee and snacks -------------------------------------------------------
Starbucks | food.coffee | s | STARBUCKS STORE 0@;STARBUCKS @;STARBUCKS STORE 1@
Dunkin | food.coffee | s | DUNKIN #3@;DUNKIN DONUTS #3@
Dutch Bros | food.coffee | s | DUTCH BROS COFFEE;DUTCH BROS @
Tim Hortons | food.coffee | s | TIM HORTONS #@
Peet's | food.coffee | s | PEET'S COFFEE #@;PEETS @
Krispy Kreme | food.coffee | s | KRISPY KREME #@
Caribou Coffee | food.coffee | s | CARIBOU COFFEE #@
Baskin-Robbins | food.coffee | s | BASKIN ROBBINS #3@;BASKIN #3@
Auntie Anne's | food.coffee | s | AUNTIE ANNE'S #@
# --- restaurants (table service) ---------------------------------------------
Olive Garden | food.restaurants | s | OLIVE GARDEN 00@;OLIVE GARDEN #@
Applebee's | food.restaurants | s | APPLEBEES @;APPLEBEE'S NEIGHBORHOOD @
Chili's | food.restaurants | s | CHILI'S #@;CHILIS @
Outback | food.restaurants | s | OUTBACK @;OUTBACK STEAKHOUSE #@
Texas Roadhouse | food.restaurants | s | TEXAS ROADHOUSE #@
Red Lobster | food.restaurants | s | RED LOBSTER 0@
Cheesecake Factory | food.restaurants | s | THE CHEESECAKE FACTORY;CHEESECAKE FACTORY 0@
IHOP | food.restaurants | s | IHOP #@;IHOP @
Denny's | food.restaurants | s | DENNY'S #@;DENNYS @
Waffle House | food.restaurants | s | WAFFLE HOUSE @
Cracker Barrel | food.restaurants | s | CRACKER BARREL #@
Buffalo Wild Wings | food.restaurants | s | BUFFALO WILD WINGS @;BUFFALO WILD WINGS 0@
Red Robin | food.restaurants | s | RED ROBIN NO @
LongHorn | food.restaurants | s | LONGHORN STEAK 0@;LONGHORN STEAKHOUSE @
# --- delivery ------------------------------------------------------------------
DoorDash | food.delivery | o | DOORDASH*CHIPOTLE;DD *DOORDASH PANDAEXPR;DD DOORDASH MCDONALDS;
> DOORDASH*THAI BASIL;DD *DOORDASH WENDYS | pp=DOORDASH
Grubhub | food.delivery | o | GRUBHUB*TACOBELL;GRUBHUB HOLDINGS INC;GRUBHUB*NOODLEHOUSE
# --- fuel ----------------------------------------------------------------------
Shell | transport.fuel | s | SHELL OIL 57@@;SHELL SERVICE STATION;SHELL OIL 12@@
Chevron | transport.fuel | s | CHEVRON 0@@;CHEVRON 00@
ExxonMobil | transport.fuel | s | EXXONMOBIL @@;EXXON @;EXXON EXPRESS @
BP | transport.fuel | s | BP#@@;BP #@
Speedway | transport.fuel | s | SPEEDWAY 0@;SPEEDWAY @
Marathon | transport.fuel | s | MARATHON PETRO@;MARATHON @
Sunoco | transport.fuel | s | SUNOCO 0@@;SUNOCO @
Valero | transport.fuel | s | VALERO @;VALERO #@
Phillips 66 | transport.fuel | s | PHILLIPS 66 - @;PHILLIPS 66 #@
Conoco | transport.fuel | s | CONOCO - @;CONOCO #@
Citgo | transport.fuel | s | CITGO @;CITGO #@
Arco | transport.fuel | s | ARCO #@;ARCO AMPM #@
Murphy USA | transport.fuel | s | MURPHY USA @;MURPHY EXPRESS @
Love's | transport.fuel | s | LOVE'S #@;LOVES TRAVEL S0@
Pilot | transport.fuel | s | PILOT @;PILOT TRAVEL CTR @
RaceTrac | transport.fuel | s | RACETRAC @;RACETRAC#@
Maverik | transport.fuel | s | MAVERIK #@
# --- getting around ------------------------------------------------------------
Uber | transport.taxi | o | UBER *TRIP;UBER TRIP HELP.UBER.COM;UBER *TRIP %;UBER TRIP | pp=UBER
Lyft | transport.taxi | o | LYFT *RIDE SUN 3PM;LYFT *1 RIDE 05-14;LYFT *RIDE FRI 11PM;
> LYFT RIDE | pp=LYFT
ParkMobile | transport.parking | o | PARKMOBILE;PARKMOBILE %
SpotHero | transport.parking | o | SPOTHERO;SPOTHERO 844-555-0163 IL
LAZ Parking | transport.parking | s | LAZ PARKING @;LAZ PARKING 6@
E-ZPass | transport.parking | o | E-Z PASS REBILL;E-ZPASS PREPAID TOLL;EZPASS REPLENISHMENT
SunPass | transport.parking | o | SUNPASS*ACC%;SUNPASS *ACC%
FasTrak | transport.parking | o | FASTRAK CSC;FASTRAK REPLENISH
MTA | transport.public_transit | o | MTA*NYCT PAYGO;MTA*METROCARD MACHINE;
> MTA*NYCT PAYGO NEW YORK NY
Clipper | transport.public_transit | o | CLIPPER SERVICE;CLIPPER SYSTEMS
Ventra | transport.public_transit | o | VENTRA WEBSITE;VENTRA VENDING @
Jiffy Lube | transport.car_maintenance | s | JIFFY LUBE #@;JIFFY LUBE @
Valvoline | transport.car_maintenance | s | VALVOLINE INSTANT OIL;VALVOLINE INSTANT OI 0@
AutoZone | transport.car_maintenance | s | AUTOZONE #@;AUTOZONE @
O'Reilly Auto Parts | transport.car_maintenance | s | O'REILLY AUTO PARTS @;OREILLY AUTO #0@;
> O REILLY AUTO PARTS @
Advance Auto Parts | transport.car_maintenance | s | ADVANCE AUTO PARTS #@
NAPA | transport.car_maintenance | s | NAPA AUTO PARTS;NAPA STORE @
Discount Tire | transport.car_maintenance | s | DISCOUNT TIRE CO @;DISCOUNT TIRE @
Firestone | transport.car_maintenance | s | FIRESTONE @;FIRESTONE COMPLETE AUT
Pep Boys | transport.car_maintenance | s | PEP BOYS STORE @
GEICO | transport.car_insurance | o | GEICO *AUTO;
> GEICO *AUTO 800-555-0158 DC | ach=GEICO/PREM COLL ~ rec
Progressive | transport.car_insurance | o | PROGRESSIVE *INSURANCE;
> PROGRESSIVE INS | ach=PROG DIRECT INS/INS PREM ~ rec
Root Insurance | transport.car_insurance | o | ROOT INSURANCE;ROOT INSURANCE 866-555-0119 OH | rec
# --- phone, internet, power ----------------------------------------------------
Verizon Wireless | utilities.phone | o | VZWRLSS*APOCC VISB;VERIZON WRLS @-01;
> VZWRLSS*MY VZ VB P | ach=VERIZON WIRELESS/PAYMENTS ~ rec
AT&T | utilities.phone | o | ATT*BILL PAYMENT;AT&T *PAYMENT;
> ATT* BILL PAYMENT 800-555-0121 TX | ach=ATT/Payment ~ rec
T-Mobile | utilities.phone | o | TMOBILE*AUTO PAY;T-MOBILE*POSTPAID TEL;
> TMOBILE*POSTPAID WEB | ach=T-MOBILE/PCS SVC ~ rec
Mint Mobile | utilities.phone | o | MINT MOBILE;MINT MOBILE 800-555-0171 CA | rec
Cricket Wireless | utilities.phone | o | CRICKET WIRELESS;CRICKET WRLS @ | rec
Google Fi | utilities.phone | - | - | fr=GOOGLE *Fi %;GOOGLE *FI;GOOGLE*FI g.co/helppay# CA ~ rec
Comcast | utilities.internet | o | COMCAST CABLE COMM;COMCAST XFINITY;
> COMCAST 800-555-0134 PA | ach=COMCAST/CABLE ~ rec
Spectrum | utilities.internet | o | SPECTRUM;SPECTRUM 855-555-0155 MO;
> SPECTRUM MOBILE & INTERNET | ach=SPECTRUM/SPECTRUM ~ rec
Cox | utilities.internet | o | COX COMMUNICATIONS;COX*COMM SERVICES | ach=COX COMM/BANKDRAFT ~ rec
CenturyLink | utilities.internet | o | CENTURYLINK;CTL*CENTURYLINK | ach=CENTURYLINK/AUTO PAY ~ rec
Optimum | utilities.internet | o | OPTIMUM @;OPTIMUM CABLE | rec
Starlink | utilities.internet | o | STARLINK INTERNET;STARLINK INTERNET 833-555-0114 CA | rec
Duke Energy | utilities.electricity | o | DUKE ENERGY *SPEEDPAY;
> DUKE-ENERGY BILL PAY | ach=DUKEENERGY/BILL PAY ~ rec
PG&E | utilities.electricity | o | PG&E/EZ-PAY;PGANDE WEB ONLINE | ach=PGANDE/WEB ONLINE ~ rec
Con Edison | utilities.electricity | o | CONED BILL PAYMENT;
> CON ED OF NY | ach=CON ED OF NY/INTELL CK ~ rec
Southern California Edison | utilities.electricity | - | - | ach=SO CAL EDISON CO/BILL PAYMT ~ rec
FPL | utilities.electricity | o | FPL DIRECT DEBIT;
> FPL *BILL PAY | ach=FPL DIRECT DEBIT/ELEC PYMT ~ rec
Dominion Energy | utilities.electricity | o | DOMINION ENERGY;
> DOMINION ENERGY *BILL | ach=DOMINION ENERGY/BILLPAY ~ rec
Xcel Energy | utilities.electricity | - | - | ach=XCEL ENERGY/XCELENERGY ~ rec
Georgia Power | utilities.electricity | - | - | ach=GEORGIA POWER/GPC EFT ~ rec
ComEd | utilities.electricity | o | COMED *BILL PAYMENT;COMED | ach=COMED/UTIL_BIL ~ rec
SoCalGas | utilities.heating | - | - | ach=SO CAL GAS/PAID SCGC ~ rec
Nicor Gas | utilities.heating | - | - | ach=NICOR GAS/GAS PAYMNT ~ rec
Piedmont Natural Gas | utilities.heating | o | PIEDMONT NATURAL GAS;
> PIEDMONT NAT GAS *PAY | ach=PIEDMONT NG/DRAFT ~ rec
Atmos Energy | utilities.heating | o | ATMOS ENERGY;
> ATMOS ENERGY *BILLPAY | ach=ATMOS ENERGY/UTIL PYMT ~ rec
CenterPoint Energy | utilities.heating | o | CENTERPOINT ENERGY;
> CENTERPOINT ENERGY 800-555-0147 TX | ach=CENTERPOINT ENER/CNP PAYMNT ~ rec
Waste Management | utilities.other | o | WASTE MGMT WM EZPAY;WM.COM;
> WASTE MANAGEMENT INTERNET | ach=WASTE MANAGEMENT/INTERNET ~ rec
Republic Services | utilities.other | o | REPUBLIC SERVICES TRASH;
> REPUBLIC SERVICES #@ | ach=REPUBLICSERVICES/RSIBILLPAY ~ rec
# --- streaming -------------------------------------------------------------------
Netflix | leisure.subscriptions | o | NETFLIX.COM;NETFLIX;Netflix.com;
> NETFLIX.COM 866-555-0162 CA | pp=NETFLIX.COM ~ rec
Hulu | leisure.subscriptions | o | HULU;HLU*HULU %-U;HULU 877-555-0118 CA | pp=HULU ~ rec
Disney+ | leisure.subscriptions | o | DISNEY PLUS;DISNEYPLUS;
> DISNEY PLUS 888-555-0190 CA | pp=DISNEYPLUS ~ rec
Max | leisure.subscriptions | o | HBO MAX;HBOMAX HELP.HBOMAX.COM;HBO MAX 855-555-0139 NY | rec
Paramount+ | leisure.subscriptions | o | PARAMOUNT+;PARAMNTPLUS;PARAMOUNT+ 888-555-0125 CA | rec
Peacock | leisure.subscriptions | o | PEACOCK TV LLC;PEACOCK % PREMIUM;PEACOCKTV.COM | rec
Spotify | leisure.subscriptions | o | SPOTIFY USA;Spotify P%;
> SPOTIFY 877-555-0181 NY | pp=SPOTIFYUSAI ~ rec
YouTube | leisure.subscriptions | - | - | fr=GOOGLE *YouTubePremium;GOOGLE *YouTube TV;
> GOOGLE*YOUTUBEPREMIUM g.co/helppay# CA;GOOGLE *YOUTUBE TV ~ rec
SiriusXM | leisure.subscriptions | o | SXM*SIRIUSXM.COM/ACCT;SIRIUS XM RADIO;
> SIRIUSXM.COM/ACCT 888-555-0166 NY | rec
Pandora | leisure.subscriptions | o | PANDORA*INTERNET RADIO;PANDORA MEDIA | rec
Audible | leisure.subscriptions | o | AUDIBLE*%;Audible*% Amzn.com/bill NJ;AUDIBLE US*% | rec
Crunchyroll | leisure.subscriptions | o | CRUNCHYROLL;
> CRUNCHYROLL *MEMBERSHIP | pp=CRUNCHYROLL ~ rec
ESPN+ | leisure.subscriptions | o | ESPN PLUS;ESPN+ 800-555-0152 NY | rec
Tidal | leisure.subscriptions | o | TIDAL;TIDALMUSIC | rec
Sling TV | leisure.subscriptions | o | SLING.COM;SLING TV LLC | rec
fuboTV | leisure.subscriptions | o | FUBOTV INC;FUBOTV | rec
# --- news ----------------------------------------------------------------------
New York Times | leisure.news | o | NYTIMES*NYTIMES;NEW YORK TIMES DIGITAL;
> NYT*NYTIMES DISC | pp=NYTIMES ~ rec
Wall Street Journal | leisure.news | o | D J*WALL-ST-JOURNAL;WSJ DIGITAL | rec
Washington Post | leisure.news | o | WASHINGTON POST;WASHPOST %;WASHINGTON POST DIGITAL | rec
The Atlantic | leisure.news | o | THE ATLANTIC;THE ATLANTIC MONTHLY | rec
The New Yorker | leisure.news | o | CNE*THE NEW YORKER;THE NEW YORKER | rec
The Economist | leisure.news | o | THE ECONOMIST NEWSPAPER;ECONOMIST.COM | rec
# --- software --------------------------------------------------------------------
Adobe | leisure.software | o | ADOBE *CREATIVE CLOUD;ADOBE INC;ADOBE *ACROPRO SUBS;
> ADOBE 408-555-0143 CA | pp=ADOBESYSTEM ~ rec
Microsoft 365 | leisure.software | o | MICROSOFT*MICROSOFT 365;MICROSOFT*MICROSOFT 365 P;
> MSFT *MICROSOFT 365 | rec
Dropbox | leisure.software | o | DROPBOX*%;DROPBOX | pp=DROPBOX ~ rec
Google One | leisure.software | - | - | fr=GOOGLE *Google One;GOOGLE *GOOGLE ONE;
> GOOGLE*GOOGLE ONE g.co/helppay# CA ~ rec
OpenAI | leisure.software | o | OPENAI *CHATGPT SUBSCR;CHATGPT SUBSCRIPTION;OPENAI | rec
Anthropic | leisure.software | o | CLAUDE.AI SUBSCRIPTION;ANTHROPIC | rec
GitHub | leisure.software | o | GITHUB;GITHUB, INC. | rec
Notion | leisure.software | o | NOTION LABS, INC.;NOTION LABS INC | rec
Zoom | leisure.software | o | ZOOM.US 888-555-0199;ZOOM.US | rec
1Password | leisure.software | o | 1PASSWORD;1PASSWORD 888-555-0122 ON | rec
Canva | leisure.software | o | CANVA* %;CANVA | pp=CANVA ~ rec
Grammarly | leisure.software | o | GRAMMARLY CO%;GRAMMARLY | rec
Evernote | leisure.software | o | EVERNOTE;EVERNOTE CORPORATION | rec
# --- games -----------------------------------------------------------------------
Steam | leisure.games | o | STEAMGAMES.COM;STEAM PURCHASE;
> STEAMGAMES.COM 425-555-0188 WA | pp=STEAM GAMES
PlayStation | leisure.games | o | PLAYSTATION NETWORK;PLAYSTATION*NETWORK;
> PLAYSTATIONNETWORK | pp=PLAYSTATION
Nintendo | leisure.games | o | NINTENDO *AMERICAUS;Nintendo CA%;NINTENDO OF AMERICA | pp=NINTENDO
Xbox | leisure.games | o | MICROSOFT*XBOX GAME PASS;MICROSOFT*XBOX;MICROSOFT*XBOX LIVE GOLD | rec
Epic Games | leisure.games | o | EPIC GAMES;EPC*EPIC GAMES | pp=EPIC GAMES
Blizzard | leisure.games | o | BLIZZARD ENTERTAINMENT;BLIZZARD *BATTLENET | pp=BLIZZARD
Roblox | leisure.games | o | ROBLOX;RBLX* ROBLOX;ROBLOX 888-555-0137 CA
GameStop | leisure.games | s | GAMESTOP #@;GAMESTOP @
# --- memberships and fitness -------------------------------------------------------
AARP | leisure.memberships | o | AARP MEMBERSHIP;AARP *MEMBERSHIP | rec
Planet Fitness | health.fitness | s | PLANET FITNESS;
> PLANET FITNESS @ | ach=PLANET FIT/CLUB FEES ~ rec
LA Fitness | health.fitness | s | LA FITNESS;LA FITNESS @ | ach=LA FITNESS/LAFITNESS ~ rec
Anytime Fitness | health.fitness | o | ABC*ANYTIME FITNESS;ANYTIME FITNESS | rec
24 Hour Fitness | health.fitness | s | 24 HOUR FITNESS USA;24 HOUR FITNESS #@ | rec
Equinox | health.fitness | s | EQUINOX #@;EQUINOX MEMBERSHIP | rec
Orangetheory | health.fitness | s | ORANGETHEORY FITNESS;ORANGETHEORY FITNESS @ | rec
Peloton | health.fitness | o | PELOTON* MEMBERSHIP;PELOTON INTERACTIVE;PELOTON *MEMBERSHIP | rec
Crunch Fitness | health.fitness | s | CRUNCH FITNESS;
> CRUNCH FITNESS @ | ach=CRUNCH FITNESS/CLUB DUES ~ rec
YMCA | health.fitness | s | YMCA @;YMCA MEMBERSHIP | rec
ClassPass | health.fitness | o | CLASSPASS* MONTHLY;CLASSPASS | rec
Life Time | health.fitness | s | LIFE TIME FITNESS;LIFETIME FITNESS @ | rec
Gold's Gym | health.fitness | s | GOLD'S GYM;GOLDS GYM @ | rec
# --- health ------------------------------------------------------------------------
CVS | health.pharmacy | s | CVS/PHARMACY #0@;CVS PHARMACY #@;CVS/PHARMACY #1@
Walgreens | health.pharmacy | s | WALGREENS #@;WALGREENS STORE @;WALGREENS #1@
Rite Aid | health.pharmacy | s | RITE AID @;RITE AID STORE - @
Duane Reade | health.pharmacy | s | DUANE READE #1@
Quest Diagnostics | health.medical | o | QUEST DIAGNOSTICS;QUEST DIAGNOSTICS 866-555-0128 NJ
Labcorp | health.medical | o | LABCORP;LABCORP *PATIENT BILL
One Medical | health.medical | o | ONE MEDICAL;ONE MEDICAL MEMBERSHIP
Teladoc | health.medical | o | TELADOC;TELADOC HEALTH
Aspen Dental | health.dental | s | ASPEN DENTAL;ASPEN DENTAL @
UnitedHealthcare | health.insurance | - | - | ach=UNITEDHEALTHCARE/PREMIUM ~ rec
Anthem | health.insurance | o | ANTHEM BLUE CROSS;
> ANTHEM BCBS *PREMIUM | ach=ANTHEM BLUE/PREMIUM ~ rec
Aetna | health.insurance | - | - | ach=AETNA/INS PREM ~ rec
Cigna | health.insurance | - | - | ach=CIGNA/PREMIUM ~ rec
Humana | health.insurance | - | - | ach=HUMANA INC/INS PYMT ~ rec
# --- clothing ----------------------------------------------------------------------
Old Navy | shopping.clothing | s | OLD NAVY US @;OLDNAVY.COM;OLD NAVY #@
Gap | shopping.clothing | s | GAP US @;GAP ONLINE;GAP OUTLET US @
H&M | shopping.clothing | s | H&M0@;H&M @;HM.COM
Zara | shopping.clothing | s | ZARA USA @;ZARA.COM
Uniqlo | shopping.clothing | s | UNIQLO;UNIQLO USA @
Nike | shopping.clothing | s | NIKE.COM;NIKE FACTORY STORE;NIKEPOS_US | pp=NIKE
Adidas | shopping.clothing | s | ADIDAS;ADIDAS #@;ADIDAS.COM
Lululemon | shopping.clothing | s | LULULEMON ATHLETICA;LULULEMON @
TJ Maxx | shopping.clothing | s | TJ MAXX #@;TJMAXX #0@
Marshalls | shopping.clothing | s | MARSHALLS #@;MARSHALLS #0@
Ross | shopping.clothing | s | ROSS STORES #@;ROSS STORE #@
Burlington | shopping.clothing | s | BURLINGTON STORES @;BURLINGTON #@
Foot Locker | shopping.clothing | s | FOOT LOCKER @;FOOTLOCKER.COM
American Eagle | shopping.clothing | s | AMERICAN EAGLE @;AMERICAN EAGLE OUTFITTERS
Victoria's Secret | shopping.clothing | s | VICTORIA'S SECRET @;VICTORIASSECRET.COM
Forever 21 | shopping.clothing | s | FOREVER 21 #@;FOREVER 21
Shein | shopping.clothing | o | SHEIN.COM;SHEIN | pp=SHEIN
DSW | shopping.clothing | s | DSW @;DSW INC
Zappos | shopping.clothing | o | ZAPPOS.COM;ZAPPOS.COM 800-555-0115 NV
Carter's | shopping.clothing | s | CARTER'S #@;CARTERS @
J.Crew | shopping.clothing | s | J CREW #@;JCREW.COM;J.CREW FACTORY @
Banana Republic | shopping.clothing | s | BANANA REPUBLIC US @;BANANA REPUBLIC
# --- electronics, home, improvement ------------------------------------------------
Best Buy | shopping.electronics | s | BEST BUY 00@;BESTBUYCOM%;BEST BUY @
Micro Center | shopping.electronics | s | MICRO CENTER;MICRO CENTER #@
B&H Photo | shopping.electronics | o | B&H PHOTO 800-555-0174;B&H PHOTO-VIDEO
Newegg | shopping.electronics | o | NEWEGG INC;NEWEGG.COM | pp=NEWEGG
Home Depot | housing.improvement | s | THE HOME DEPOT #@;HOMEDEPOT.COM;THE HOME DEPOT @
Lowe's | housing.improvement | s | LOWES #0@*;LOWE'S #@;LOWES #@
Menards | housing.improvement | s | MENARDS @;MENARDS
Ace Hardware | housing.improvement | s | ACE HARDWARE;ACE HDWE @
Harbor Freight | housing.improvement | s | HARBOR FREIGHT TOOLS @
Sherwin-Williams | housing.improvement | s | SHERWIN WILLIAMS 70@;SHERWIN-WILLIAMS @
IKEA | shopping.home | s | IKEA @;IKEA.COM;IKEA US
Wayfair | shopping.home | o | WAYFAIR*WAYFAIR;WAYFAIR %;WAYFAIR*% | pp=WAYFAIR
Bed Bath & Beyond | shopping.home | s | BED BATH & BEYOND #@;BEDBATH&BEYOND#@
HomeGoods | shopping.home | s | HOMEGOODS #@;HOMEGOODS #0@
Pottery Barn | shopping.home | s | POTTERY BARN;POTTERY BARN @
Crate & Barrel | shopping.home | s | CRATE & BARREL #@;CRATE&BARREL
Williams-Sonoma | shopping.home | s | WILLIAMS-SONOMA;WILLIAMS SONOMA @
West Elm | shopping.home | s | WEST ELM;WEST ELM @
Ashley | shopping.home | s | ASHLEY HOMESTORE;ASHLEY FURNITURE @
Kirkland's | shopping.home | s | KIRKLAND'S #@
The Container Store | shopping.home | s | THE CONTAINER STORE;CONTAINERSTORE #@
# --- general shopping --------------------------------------------------------------
Kohl's | shopping.general | s | KOHL'S #0@;KOHLS #@
Macy's | shopping.general | s | MACY'S #@;MACYS .COM;MACY'S EAST #0@
JCPenney | shopping.general | s | JCPENNEY @;JCP.COM
Dillard's | shopping.general | s | DILLARDS @;DILLARD'S @
Nordstrom | shopping.general | s | NORDSTROM #0@;NORDSTROM DIRECT #@
Dollar General | shopping.general | s | DOLLAR GENERAL #@;DOLLAR-GENERAL #@
Dollar Tree | shopping.general | s | DOLLAR TREE;DOLLAR TREE #@
Five Below | shopping.general | s | FIVE BELOW @;FIVE BELOW #7@
Big Lots | shopping.general | s | BIG LOTS #@;BIG LOTS STORES - #@
eBay | shopping.general | o | eBay O*%;EBAY O*%;EBAY INC. | pp=EBAY
Etsy | shopping.general | o | ETSY.COM*%;Etsy.com - Multiple Shops;ETSY.COM | pp=ETSY INC
Temu | shopping.general | o | TEMU.COM;TEMU | pp=TEMU
AliExpress | shopping.general | o | ALIEXPRESS;ALIEXPRESS.COM | pp=ALIEXPRESS
QVC | shopping.general | o | QVC*%;QVC
# --- personal care, pets, hobbies -------------------------------------------------
Ulta | family.personal_care | s | ULTA #@;ULTA.COM
Sephora | family.personal_care | s | SEPHORA @;SEPHORA.COM
Bath & Body Works | family.personal_care | s | BATH & BODY WORKS @;BATH AND BODY WORKS
Great Clips | family.personal_care | s | GREAT CLIPS;GREAT CLIPS AT @
Supercuts | family.personal_care | s | SUPERCUTS @;SUPERCUTS
Sally Beauty | family.personal_care | s | SALLY BEAUTY #@
Massage Envy | family.personal_care | s | MASSAGE ENVY;MASSAGE ENVY @ | rec
PetSmart | family.pets | s | PETSMART #@;PETSMART INC @
Petco | family.pets | s | PETCO @;PETCO #@
Chewy | family.pets | o | CHEWY.COM;CHEWY INC;CHEWY.COM 800-555-0169 FL | pp=CHEWY INC
Banfield | family.pets | s | BANFIELD PET HOSPITAL;BANFIELD-PET @
Rover | family.pets | o | ROVER.COM;ROVER.COM 888-555-0112 WA
BarkBox | family.pets | o | BARKBOX;BARKBOX 855-555-0185 NY | rec
Pet Supplies Plus | family.pets | s | PET SUPPLIES PLUS #@
Michaels | leisure.hobbies | s | MICHAELS STORES @;MICHAELS #@
Hobby Lobby | leisure.hobbies | s | HOBBY LOBBY #@;HOBBY-LOBBY #0@
Joann | leisure.hobbies | s | JOANN STORES #@;JO-ANN STORE #@
Barnes & Noble | leisure.hobbies | s | BARNES & NOBLE #@;BARNES&NOBLE.COM;BARNESNOBLE
Dick's Sporting Goods | leisure.hobbies | s | DICK'S SPORTING GOODS;DICK'S CLOTHING&SPORTING
REI | leisure.hobbies | s | REI #@;REI.COM
Academy Sports | leisure.hobbies | s | ACADEMY SPORTS #@
Bass Pro Shops | leisure.hobbies | s | BASS PRO STORE;BASS PRO SHOPS @
Guitar Center | leisure.hobbies | s | GUITAR CENTER #@
Kindle | leisure.hobbies | o | KINDLE SVCS*%;Kindle Svcs*% 888-555-0133 WA
# --- events and travel ---------------------------------------------------------------
Ticketmaster | leisure.events | o | TM *TICKETMASTER;TICKETMASTER.COM;TICKETMASTER
StubHub | leisure.events | o | STUBHUB, INC.;STUBHUB | pp=STUBHUB
AMC Theatres | leisure.events | s | AMC 0@ ONLINE;AMC THEATRES @;AMC @
Regal | leisure.events | s | REGAL CINEMAS @;REGAL CINEMAS
Cinemark | leisure.events | s | CINEMARK THEATRES @;CINEMARK @
Fandango | leisure.events | o | FANDANGO.COM;FANDANGO | pp=FANDANGO
Eventbrite | leisure.events | o | EB *SUMMER LANTERN FEST;EVENTBRITE;EB *OPEN STUDIO NIGHT
SeatGeek | leisure.events | o | SEATGEEK;SEATGEEK TICKETS
Delta | leisure.travel | o | DELTA AIR LINES;DELTA AIR 0062@@@
United Airlines | leisure.travel | o | UNITED AIRLINES;UNITED 0162@@@
American Airlines | leisure.travel | o | AMERICAN AIRLINES;AMERICAN AIR0012@@
Southwest | leisure.travel | o | SOUTHWEST AIRLINES;SOUTHWES 5262@@@
JetBlue | leisure.travel | o | JETBLUE 2792@@@;JETBLUE AIRWAYS
Alaska Airlines | leisure.travel | o | ALASKA AIR 0272@@;ALASKA AIRLINES
Spirit Airlines | leisure.travel | o | SPIRIT AIRL 4872@@;SPIRIT AIRLINES
Marriott | leisure.travel | s | MARRIOTT @;COURTYARD BY MARRIOTT
Hilton | leisure.travel | s | HILTON HOTELS;HILTON GARDEN INN
Hampton Inn | leisure.travel | s | HAMPTON INN;HAMPTON INN & SUITES
Holiday Inn | leisure.travel | s | HOLIDAY INN EXPRESS;HOLIDAY INN
Hyatt | leisure.travel | s | HYATT PLACE;HYATT REGENCY
Airbnb | leisure.travel | o | AIRBNB * HM%;AIRBNB;AIRBNB * HM% 855-555-0136 CA | pp=AIRBNB
Expedia | leisure.travel | o | EXPEDIA 7@@@;EXPEDIA.COM
Booking.com | leisure.travel | o | BOOKING.COM;HOTEL AT BOOKING.COM
Hotels.com | leisure.travel | o | HOTELS.COM%;HOTELS.COM
Enterprise | leisure.travel | s | ENTERPRISE RENT-A-CAR
Hertz | leisure.travel | s | HERTZ RENT-A-CAR;THE HERTZ CORPORATION
Avis | leisure.travel | s | AVIS RENT-A-CAR;AVIS.COM PREPAY
# --- family --------------------------------------------------------------------------
Coursera | family.education | o | COURSERA;COURSERA* %
Udemy | family.education | o | UDEMY: ONLINE COURSES;UDEMY | pp=UDEMY
Chegg | family.education | o | CHEGG ORDER;CHEGG | rec
MasterClass | family.education | o | MASTERCLASS;MASTERCLASS 855-555-0151 CA | rec
KinderCare | family.childcare | s | KINDERCARE;
> KINDERCARE LEARNING CTR | ach=KINDERCARE LEARNING/TUITION ~ rec
Bright Horizons | family.childcare | o | BRIGHT HORIZONS;
> BRIGHT HORIZONS FAMILY | ach=BRIGHT HORIZONS/TUITION ~ rec
American Red Cross | family.donations | o | AMERICAN RED CROSS;AMERICAN RED CROSS 800-555-0124 DC
UNICEF | family.donations | o | UNICEF USA;UNICEF USA 800-555-0178 NY
St. Jude | family.donations | o | ALSAC ST JUDE;ST JUDE CHILDRENS HOSP | rec
Wikimedia | family.donations | o | WIKIMEDIA FOUNDATION;WIKIMEDIA 877-555-0165 CA | pp=WIKIMEDIA
1-800-Flowers | family.gifts | o | 1800FLOWERS.COM;1-800-FLOWERS.COM,INC.
FTD | family.gifts | o | FTD*FTD.COM;FTD
Hallmark | family.gifts | s | HALLMARK @;HALLMARK GOLD CROWN
Edible Arrangements | family.gifts | s | EDIBLE ARRANGEMENTS;EDIBLE ARRANGEMENTS @
Shutterfly | family.gifts | o | SHUTTERFLY;SHUTTERFLY, INC.
# --- alcohol -------------------------------------------------------------------------
Total Wine | food.alcohol | s | TOTAL WINE AND MORE @;TOTAL WINE & MORE
BevMo | food.alcohol | s | BEVMO! @;BEVMO
ABC Fine Wine & Spirits | food.alcohol | s | ABC FINE WINE & SPIRITS;ABC FINE WINE SPIRITS @
Spec's | food.alcohol | s | SPEC'S #@;SPECS #@
Binny's | food.alcohol | s | BINNY'S BEVERAGE DEPOT;BINNYS BEVERAGE DEPOT @
Drizly | food.alcohol | o | DRIZLY;DRIZLY 855-555-0141 MA
# --- money matters -----------------------------------------------------------------
IRS | financial.taxes | - | - | ach=IRS/USATAXPYMT
TurboTax | financial.services | o | INTUIT *TURBOTAX;INTUIT *TURBOTAX 800-555-0157 CA
H&R Block | financial.services | s | H&R BLOCK;H&R BLOCK @
Experian | financial.services | o | EXPERIAN* CREDIT REPORT;EXPERIAN *CREDITWORKS | rec
LegalZoom | financial.services | o | LEGALZOOM* %;LEGALZOOM.COM
Northwestern Mutual | financial.insurance | - | - | ach=NORTHWESTERN MU/ISA PYMNT ~ rec
New York Life | financial.insurance | - | - | ach=NEW YORK LIFE/INS. PREM ~ rec
Prudential | financial.insurance | - | - | ach=PRUDENTIAL/INS PREM ~ rec
Lemonade | housing.insurance | o | LEMONADE I* LEMONADE;LEMONADE INSURANCE | rec
Navient | transfers.loan_payment | - | - | ach=NAVIENT/STDNT LOAN ~ rec
Nelnet | transfers.loan_payment | - | - | ach=NELNET/STUDNTLOAN ~ rec
Sallie Mae | transfers.loan_payment | - | - | ach=SALLIE MAE/PAYMENT ~ rec
Toyota Financial | transfers.loan_payment | - | - | ach=TOYOTA FINANCIAL/RETAIL PMT ~ rec
American Honda Finance | transfers.loan_payment | - | - | ach=AMERICAN HONDA FIN/PAYMENT ~ rec
Vanguard | transfers.investments | - | - | ach=VANGUARD BUY/INVESTMENT
Fidelity | transfers.investments | - | - | ach=FID BKG SVC LLC/MONEYLINE
Charles Schwab | transfers.investments | - | - | ach=SCHWAB BROKERAGE/MONEYLINK
Robinhood | transfers.investments | - | - | ach=ROBINHOOD/DEBITS
Acorns | transfers.investments | - | - | ach=ACORNS INVEST/TRANSFER ~ rec
Betterment | transfers.investments | - | - | ach=BETTERMENT/TRANSFER
Wealthfront | transfers.investments | - | - | ach=WEALTHFRONT/EDI PYMNTS
"""
)

KNOWN_MERCHANTS: Final = len(_KNOWN)

_Wrap = Callable[[str, _Dice], str]

_POS: Final[tuple[_Wrap, ...]] = (
    lambda d, r: f"POS DEBIT {d}",
    lambda d, r: f"POS PURCHASE {d}",
    lambda d, r: f"DEBIT CARD PURCHASE {d}",
    lambda d, r: f"POS DEBIT - VISA CHECK CARD {r.digits(4)} - {d}",
    lambda d, r: f"DEBIT CARD PURCHASE XXXXX{r.digits(4)} {d}",
    lambda d, r: f"Point of Sale Debit L340 DATE {r.mmdd('-')} {d}",
    lambda d, r: f"POS PURCHASE TERMINAL {r.digits(8)} {d}",
    lambda d, r: f"VISA DDA PUR {r.digits(6)} {d}",
)
_AUTHORIZED: Final[tuple[_Wrap, ...]] = (
    lambda d, r: f"PURCHASE AUTHORIZED ON {r.mmdd()} {d} S{r.digits(15)} CARD {r.digits(4)}",
    lambda d, r: f"PURCHASE AUTHORIZED ON {r.mmdd()} {d} P{r.digits(15)} CARD {r.digits(4)}",
)
_AUTHORIZED_RECURRING: Final[tuple[_Wrap, ...]] = (
    lambda d, r: (
        f"RECURRING PAYMENT AUTHORIZED ON {r.mmdd()} {d} S{r.digits(15)} CARD {r.digits(4)}"
    ),
)
_CHECKCARD: Final[tuple[_Wrap, ...]] = (
    lambda d, r: f"CHECKCARD {r.mmdd('')} {d} {r.digits(23)}",
    lambda d, r: (
        f"CHECKCARD {r.mmdd('')} {d} {r.digits(23)} CKCD {r.digits(4)} "
        f"XXXXXXXXXXXX{r.digits(4)} XXXX XXXX XXXX {r.digits(4)}"
    ),
    lambda d, r: f"PURCHASE {r.mmdd('')} {d} {r.digits(23)}",
)
_CHECKCARD_RECURRING: Final[tuple[_Wrap, ...]] = (
    lambda d, r: f"CHECKCARD {r.mmdd('')} {d} {r.digits(23)} RECURRING",
)


def _reversed_name(person: str) -> str:
    first, last = person.split()
    return f"{last},{first}"


def _ach(company: str, descr: str, person: str, dice: _Dice) -> str:
    """A labelled ACH debit as two kinds of bank print it."""
    sec = dice.pick(("WEB", "PPD", "TEL"))
    if dice.below(2):
        name = person if dice.below(3) else _reversed_name(person)
        return (
            f"{company} DES:{descr} ID:{dice.alnum(10)} INDN:{name} CO ID:{dice.digits(10)} {sec}"
        )
    return (
        f"ORIG CO NAME:{company} ORIG ID:{dice.digits(10)} DESC DATE:{dice.yymmdd()} "
        f"CO ENTRY DESCR:{descr} SEC:{sec} TRACE#:{dice.digits(15)} EED:{dice.yymmdd()} "
        f"IND ID:{dice.digits(9)} IND NAME:{person} TRN: {dice.digits(10)}TC"
    )


def _located(merchant: _Merchant, name: str, dice: _Dice) -> str:
    """The network descriptor: the name and what the network adds after it."""
    remote = "." in name or "ONLINE" in name
    if merchant.where == "s" and not remote:
        town, state = dice.town()
        return f"{name} {town} {state}"
    if (
        merchant.where == "o"
        and not remote
        and dice.below(3) == 0
        and not any(ch.isdigit() for ch in name[-6:])
    ):
        return f"{name} {dice.phone()} {dice.pick(('CA', 'NY', 'WA', 'TX', 'DE'))}"
    return name


def _fronts(merchant: _Merchant, dice: _Dice) -> list[str]:
    out = [dice.fill(name) for name in merchant.fronted]
    if merchant.paypal is not None:
        out.append(f"PAYPAL *{merchant.paypal}")
        out.append(f"PAYPAL *{merchant.paypal} {dice.phone()} CA")
        out.append(f"PP*{merchant.paypal}")
    return out


def _shapes_for(merchant: _Merchant) -> list[str]:
    shapes: list[str] = []
    if merchant.prints:
        shapes += ["bare", "pos", "authorized", "checkcard"]
    elif merchant.fronted:
        shapes += ["pos", "authorized", "checkcard"]
    if merchant.ach is not None:
        shapes.append("ach")
        if not merchant.prints:
            shapes.append("bare")
    if merchant.fronted or merchant.paypal is not None:
        shapes.append("processor")
    return shapes


def _known_line(merchant: _Merchant, shape: str, turn: int) -> tuple[Labelled, str]:
    dice = _Dice(f"known:{merchant.label}:{shape}:{turn}")
    private: tuple[str, ...] = ()
    kind = "purchase"
    if shape == "ach":
        assert merchant.ach is not None
        person = dice.person()
        text = _ach(*merchant.ach, person, dice)
        private = (person,)
        kind = "ach_debit"
    elif shape == "bare" and not merchant.prints:
        # A bank that prints an ACH debit without labels.
        assert merchant.ach is not None
        company, descr = merchant.ach
        text = dice.pick(
            (
                f"{company} {descr} PPD ID: {dice.digits(10)}",
                f"{company} {descr} WEB ID: {dice.digits(10)}",
                f"ACH DEBIT {company} {descr} {dice.digits(12)}",
                f"{company} {descr}",
            )
        )
        kind = "ach_debit"
    elif shape == "processor":
        fronts = _fronts(merchant, dice)
        text = fronts[(turn + dice.below(len(fronts))) % len(fronts)]
    else:
        if merchant.prints:
            name = dice.fill(merchant.prints[turn % len(merchant.prints)])
            descriptor = _located(merchant, name, dice)
        else:
            fronts = _fronts(merchant, dice)
            descriptor = fronts[turn % len(fronts)]
        recurring = merchant.recurring and dice.below(2) == 0
        if shape == "bare":
            text = descriptor
        elif shape == "pos":
            text = dice.pick(_POS)(descriptor, dice)
        elif shape == "authorized":
            wraps = _AUTHORIZED_RECURRING if recurring else _AUTHORIZED
            text = dice.pick(wraps)(descriptor, dice)
            kind = "recurring_purchase" if recurring else "purchase"
        else:
            wraps = _CHECKCARD_RECURRING if recurring else _CHECKCARD
            text = dice.pick(wraps)(descriptor, dice)
            kind = "recurring_purchase" if recurring else "purchase"
    line = Labelled(
        text=text,
        group="known",
        merchant=merchant.label,
        category=merchant.category,
        kind=kind,
        private=private,
    )
    return line, shape


def _known() -> tuple[list[Labelled], dict[str, str]]:
    """Two shapes for every merchant, then a further shape each until 800 lines."""
    plans: list[list[str]] = []
    for index, merchant in enumerate(_KNOWN):
        shapes = _shapes_for(merchant)
        # The rarer shapes first (few merchants debit an account or sit behind
        # a processor); the card shapes in turn.
        rare = [s for s in shapes if s in ("ach", "processor")]
        card = [s for s in shapes if s not in rare]
        start = index % len(card) if card else 0
        plans.append(rare + card[start:] + card[:start])
    taken = [2] * len(_KNOWN)
    total = 2 * len(_KNOWN)
    if total > KNOWN_LINES:  # pragma: no cover - the table grew too long
        raise ValueError("too many known merchants for 800 lines")
    # The lines left over go to merchants spread through the table, not to its start.
    spread = sorted(range(len(_KNOWN)), key=lambda i: (i * 61) % len(_KNOWN))
    while total < KNOWN_LINES:
        for index in spread:
            if total == KNOWN_LINES:
                break
            if _KNOWN[index].category is not None and taken[index] < len(plans[index]):
                taken[index] += 1
                total += 1
    lines: list[Labelled] = []
    shape_of: dict[str, str] = {}
    for index, merchant in enumerate(_KNOWN):
        plan = plans[index]
        for turn in range(taken[index]):
            line, shape = _known_line(merchant, plan[turn % len(plan)], turn)
            if line.text in shape_of:  # pragma: no cover - a merchant with one plain name
                raise ValueError(f"repeated line: {line.text}")
            shape_of[line.text] = shape
            lines.append(line)
    return lines, shape_of


# ---------------------------------------------------------------------------
# Invented local shops
# ---------------------------------------------------------------------------

#: Shops with a telling word, and the category that word suggests.
_TELLING: Final[tuple[tuple[str, str], ...]] = (
    ("NONNA VESPA PIZZA", "food.restaurants"),
    ("COPPER KETTLE DINER", "food.restaurants"),
    ("TAQUERIA EL COLIBRI", "food.restaurants"),
    ("SAKURA GARDEN SUSHI", "food.restaurants"),
    ("SMOKY HOLLOW BBQ", "food.restaurants"),
    ("BLUE HERON BISTRO", "food.restaurants"),
    ("GOLDEN LANTERN RESTAURANT", "food.restaurants"),
    ("HARBORSIDE GRILL", "food.restaurants"),
    ("OLD MILL STEAKHOUSE", "food.restaurants"),
    ("RED CLOVER PIZZERIA", "food.restaurants"),
    ("DOS HERMANAS TAQUERIA", "food.restaurants"),
    ("WILLOW STREET DELI", "food.restaurants"),
    ("LUCKY DRAGON CHINESE RESTAURANT", "food.restaurants"),
    ("EL RANCHITO MEXICAN GRILL", "food.restaurants"),
    ("DRIFTWOOD COFFEE CO", "food.coffee"),
    ("FOGLINE COFFEE ROASTERS", "food.coffee"),
    ("SWEET ALDER BAKERY", "food.coffee"),
    ("HONEYCOMB DONUTS", "food.coffee"),
    ("NORTHSIDE ESPRESSO BAR", "food.coffee"),
    ("CEDAR POINT PHARMACY", "health.pharmacy"),
    ("BROOKVALE PHARMACY", "health.pharmacy"),
    ("WESTGATE FAMILY PHARMACY", "health.pharmacy"),
    ("LAKEVIEW FAMILY DENTAL", "health.dental"),
    ("BRIGHT SMILE DENTISTRY", "health.dental"),
    ("ELM GROVE ORTHODONTICS", "health.dental"),
    ("FOUR PAWS VETERINARY", "family.pets"),
    ("RIVERBEND ANIMAL HOSPITAL", "family.pets"),
    ("WAGGING TAIL PET GROOMING", "family.pets"),
    ("HILLCREST AUTO REPAIR", "transport.car_maintenance"),
    ("BIG VALLEY TIRE CENTER", "transport.car_maintenance"),
    ("SPARKLE BAY CAR WASH", "transport.car_maintenance"),
    ("IRONHORSE AUTO SERVICE", "transport.car_maintenance"),
    ("CORK & BARREL LIQUOR", "food.alcohol"),
    ("STONEBRIDGE WINE & SPIRITS", "food.alcohol"),
    ("VILLAGE LIQUORS", "food.alcohol"),
    ("FARM FRESH GROCERY", "food.groceries"),
    ("PINE STREET GROCERY", "food.groceries"),
    ("LA ESPERANZA SUPERMARKET", "food.groceries"),
    ("HILLTOP SUPERMARKET", "food.groceries"),
    ("MILLBROOK HARDWARE", "housing.improvement"),
    ("TOWN & COUNTRY LUMBER", "housing.improvement"),
    ("VELVET ROSE SALON", "family.personal_care"),
    ("KINGSLEY BARBER SHOP", "family.personal_care"),
    ("LOTUS NAIL SPA", "family.personal_care"),
    ("SUMMIT YOGA STUDIO", "health.fitness"),
    ("IRONWORKS FITNESS", "health.fitness"),
    ("RIVER CITY CROSSFIT", "health.fitness"),
    ("HIGH POINT CLIMBING GYM", "health.fitness"),
    ("CASCADE CHIROPRACTIC", "health.medical"),
    ("OAKMONT URGENT CARE", "health.medical"),
    ("CLEARVIEW OPTOMETRY", "health.medical"),
    ("MAPLE GROVE PEDIATRICS", "health.medical"),
    ("NORTHFIELD PHYSICAL THERAPY", "health.medical"),
    ("PAGE TURNER BOOKS", "leisure.hobbies"),
    ("TALL TALES BOOKSTORE", "leisure.hobbies"),
    ("LITTLE LANTERN DAYCARE", "family.childcare"),
    ("BUSY BEES PRESCHOOL", "family.childcare"),
    ("RIDGELINE FUEL STOP", "transport.fuel"),
    ("COUNTRY CORNER GAS", "transport.fuel"),
    ("CITY CENTER PARKING GARAGE", "transport.parking"),
    ("HARBOR LOT PARKING", "transport.parking"),
    ("BRIGHT MINDS TUTORING", "family.education"),
    ("LAKESHORE DRIVING SCHOOL", "family.education"),
)

#: Shops whose name says nothing about what they sell: never a fact.
_OPAQUE: Final[tuple[str, ...]] = (
    "HOLLOW CREEK TRADING",
    "THE GILDED FERN",
    "NORTHWIND SUPPLY CO",
    "ALDER & ASH",
    "KESTREL WORKS",
    "TWO RIVERS MERCANTILE",
    "SALT & CEDAR",
    "FOXGLOVE STUDIO",
    "IRON GATE COLLECTIVE",
    "THE WANDERING GOAT",
    "PAPER LANTERN",
    "BRASS COMPASS",
    "JUNEBUG",
    "MOSS & STONE",
    "THE VELVET ACORN",
    "WILDFLOWER LANE",
    "HARTWELL & SONS",
    "STARLING ROW",
    "COPPERLINE",
    "THISTLE & THREAD",
    "OSPREY POINT",
    "LITTLE OWL",
    "THE TIN ROOSTER",
    "HAVEN ON MAIN",
    "BRAMBLEWOOD",
    "CEDAR & VINE",
    "SILVER BIRCH CO",
    "OLD TOWN EMPORIUM",
    "THE CURIOUS CROW",
    "MERIDIAN GOODS",
    "PINECONE PROVISIONS",
    "RIVERSTONE",
    "GREYFRIARS",
    "THE INDIGO DOOR",
    "MARLOWE & FINCH",
    "NINE ELMS",
    "QUARRY HILL",
    "WINDMILL COMMONS",
    "SPARROW & PINE",
    "THE BLUE DOOR",
    "HEARTH & HOLLOW",
    "TIDEWATER TRADING POST",
    "BIRCHBARK",
    "EMBER & OAK",
    "SADDLEBROOK",
    "FERNDALE COLLECTIVE",
    "THE LUCKY PENNY",
    "CROOKED PINE",
    "WREN & WILLOW",
    "STONECROP",
    "NORTH FORK DEPOT",
    "HIGH DESERT TRADERS",
    "CALLOWAY BROTHERS",
    "FIDDLEHEAD",
    "GOLDENROD",
    "PEMBERTON HOUSE",
    "ASTER & IVY",
    "TALLGRASS",
    "MOONLIT HARBOR",
    "BUTTONWOOD",
    "WHISTLESTOP",
    "SUGARPINE",
)

_FOOD: Final = frozenset({"food.restaurants", "food.coffee"})


def _invented_pair(name: str, category: str | None, index: int) -> list[Labelled]:
    """Two lines for one shop: as a processor or the network prints it, then as a bank does."""
    dice = _Dice(f"invented:{name}")
    town, state = dice.town()
    plain = f"{name} {town} {state}"
    first_options = [
        f"SQ *{name}",
        f"SQ *{name} {town} {state}",
        plain,
        f"{name} #{dice.digits(2)} {town} {state}",
    ]
    if category in _FOOD:
        first_options += [f"TST* {name}", f"TST* {name} {town} {state}", f"TST*{name}"]
    else:
        first_options += [f"SP {name}", f"IN *{name}", f"PAYPAL *{name}"]
    first = first_options[index % len(first_options)]
    second_options: tuple[_Wrap, ...] = (*_POS, *_AUTHORIZED, *_CHECKCARD)
    # The second line is the same shop through a bank: sometimes behind a processor too.
    inner = (
        plain,
        f"SQ *{name} {town} {state}",
        f"{name} {town} {state}",
        f"TST* {name} {town} {state}" if category in _FOOD else f"{name} {dice.phone()} {state}",
    )[(index // 2) % 4]
    second = second_options[(index * 5 + 3) % len(second_options)](inner, dice)
    label = name.title()
    return [
        Labelled(text=text, group="invented", merchant=label, category=category, kind="purchase")
        for text in (first, second)
    ]


def _invented() -> list[Labelled]:
    lines: list[Labelled] = []
    for index, (name, category) in enumerate(_TELLING):
        lines += _invented_pair(name, category, index)
    for index, name in enumerate(_OPAQUE):
        lines += _invented_pair(name, None, index)
    return lines


# ---------------------------------------------------------------------------
# Lines that are not purchases
# ---------------------------------------------------------------------------

# Slots: {a4} digits, {x9} letters and digits, {d} mm/dd, {dd} mm-dd, {ymd}
# yymmdd, {n} a person, {rn} the same person as LAST,FIRST, {street}, {town},
# {st}, {emp} an employer.

_TRANSFER: Final[tuple[tuple[str, int], ...]] = (
    ("ONLINE TRANSFER TO SAV ...{a4}", -1),
    ("Online Banking transfer to SAV {a4} Confirmation# {a10}", -1),
    ("Online Banking transfer from CHK {a4} Confirmation# {a10}", 1),
    ("ONLINE TRANSFER REF #IB0{x7} TO WAY2SAVE SAVINGS XXXXXX{a4} ON {d}", -1),
    ("TRANSFER TO SAVINGS XXXXXX{a4}", -1),
    ("INTERNET TRANSFER FROM ACCOUNT ENDING IN {a4}", 1),
    ("FUNDS TRANSFER TO CHECKING {a4}", -1),
    ("Withdrawal Transfer To ****{a4}", -1),
    ("Deposit Transfer From ****{a4}", 1),
    ("MOBILE TRANSFER TO CHK ...{a4}", -1),
    ("RECURRING TRANSFER TO SAVINGS {a4} REF #OP0{x7}", -1),
    ("TRANSFER TO BROKERAGE ACCT {a4}", -1),
    ("Transfer from x{a4} to x{a4}", -1),
    ("EXTERNAL TRANSFER TO ACCT ENDING {a4}", -1),
)

_CARD_PAYMENT_PLAIN: Final[tuple[str, ...]] = (
    "PAYMENT THANK YOU-MOBILE",
    "PAYMENT - THANK YOU",
    "ONLINE PAYMENT, THANK YOU",
    "AUTOPAY PAYMENT - THANK YOU",
    "AUTOMATIC PAYMENT - THANK YOU",
    "PAYMENT RECEIVED - THANK YOU",
)
_CARD_PAYMENT: Final[tuple[str, ...]] = (
    "CHASE CREDIT CRD DES:AUTOPAY ID:{a10} INDN:{n} CO ID:{a10} PPD",
    "CAPITAL ONE DES:CRCARDPMT ID:{x12} INDN:{rn} CO ID:{a10} WEB",
    "DISCOVER E-PAYMENT {a4} WEB ID: {a10}",
    "AMEX EPAYMENT ACH PMT W{a4} WEB ID: {a10}",
    "CITI CARD ONLINE DES:PAYMENT ID:{a15} INDN:{n} CO ID:CITICTP WEB",
    "ONLINE BANKING PAYMENT TO CRD {a4} CONFIRMATION# {a10}",
    "ORIG CO NAME:AMERICAN EXPRESS ORIG ID:{a10} DESC DATE:{ymd} CO ENTRY DESCR:ACH PMT "
    "SEC:WEB TRACE#:{a15} EED:{ymd} IND ID:M{a4} IND NAME:{n} TRN: {a10}TC",
)

_LOAN_PAYMENT: Final[tuple[str, ...]] = (
    "TRANSFER TO LOAN {a4}",
    "AUTO LOAN PAYMENT XXXXXX{a4}",
    "MORTGAGE PAYMENT {a10}",
    "LOAN PMT - PRINCIPAL AND INTEREST",
    "ONLINE LOAN PAYMENT REF #{x9}",
    "DEPT EDUCATION DES:STUDENT LN ID:{a10} INDN:{n} CO ID:{a10} PPD",
    "HOME EQUITY LOAN PAYMENT {a4}",
    "Payment to loan ending in {a4}",
)

_CASH: Final[tuple[str, ...]] = (
    "ATM WITHDRAWAL {a6} {d} {street} {town} {st}",
    "NON-CHASE ATM WITHDRAW {a6} {d} {street} {town} {st}",
    "ATM CASH WITHDRAWAL {d} {town} {st} CARD {a4}",
    "CASH WITHDRAWAL IN BRANCH {d}",
    "BKOFAMERICA ATM {d} #{a9} WITHDRWL {street} {town} {st}",
    "ATM W/D {a4} {d} {town} {st}",
    "CASH EWITHDRAWAL IN BRANCH/STORE {d} {street} {town} {st} {a4}",
)

_FEE: Final[tuple[str, ...]] = (
    "MONTHLY MAINTENANCE FEE",
    "MONTHLY SERVICE FEE",
    "OVERDRAFT FEE FOR A TRANSACTION POSTED ON {d}",
    "OVERDRAFT ITEM FEE",
    "NON-CHASE ATM FEE-WITH",
    "FOREIGN TRANSACTION FEE",
    "INTERNATIONAL TRANSACTION FEE {d}",
    "WIRE TRANSFER FEE",
    "DOMESTIC WIRE FEE",
    "LATE FEE",
    "LATE PAYMENT FEE",
    "ANNUAL MEMBERSHIP FEE",
    "RETURNED ITEM FEE",
    "NSF RETURNED ITEM FEE FOR ACTIVITY OF {d}",
    "PAPER STATEMENT FEE",
    "CASH ADVANCE FEE",
)

_INTEREST: Final[tuple[tuple[str, int], ...]] = (
    ("INTEREST PAYMENT", 1),
    ("INTEREST PAID", 1),
    ("Interest Earned", 1),
    ("INTEREST CREDIT", 1),
    ("MONTHLY INTEREST PAID", 1),
    ("INTEREST CHARGE ON PURCHASES", -1),
    ("PURCHASE INTEREST CHARGE", -1),
    ("INTEREST CHARGE ON CASH ADVANCES", -1),
    ("INTEREST CHARGE:PURCHASES", -1),
    ("MINIMUM INTEREST CHARGE", -1),
)

_PAYROLL: Final[tuple[str, ...]] = (
    "{emp} DES:PAYROLL ID:{a10} INDN:{n} CO ID:{a10} PPD",
    "{emp} DES:DIRECT DEP ID:{a9} INDN:{rn} CO ID:{a10} PPD",
    "ORIG CO NAME:{emp} ORIG ID:{a10} DESC DATE:{ymd} CO ENTRY DESCR:PAYROLL SEC:PPD "
    "TRACE#:{a15} EED:{ymd} IND ID:{a6} IND NAME:{n} TRN: {a10}TC",
    "DIRECT DEPOSIT {emp} PAYROLL",
    "DIRECT DEP {emp} PAYROLL {a15}",
    "PAYROLL DEPOSIT {emp}",
    "{emp} PAYROLL PPD ID: {a10}",
)

_REFUND: Final[tuple[str, ...]] = (
    "REFUND",
    "MERCHANT REFUND",
    "PURCHASE RETURN",
    "POS REFUND {d} CARD {a4}",
    "PURCHASE RETURN AUTHORIZED ON {d} CARD {a4}",
    "MERCHANDISE RETURN",
    "REFUND CREDIT {a8}",
    "DEBIT CARD REFUND {d}",
    "POS RETURN {a6}",
    "CREDIT VOUCHER",
)

_CHECK: Final[tuple[str, ...]] = (
    "CHECK 1042",
    "CHECK # 2281",
    "CHECK PAID 000001187",
    "Check 3317",
    "CHECK #{a4} PAID",
    "DRAFT {a4}",
    "CHECK NUMBER {a4}",
    "CHK {a4}",
)

_DEPOSIT: Final[tuple[str, ...]] = (
    "DEPOSIT",
    "MOBILE DEPOSIT",
    "REMOTE ONLINE DEPOSIT # 1",
    "ATM CHECK DEPOSIT {d} {street} {town} {st}",
    "COUNTER CREDIT",
    "eDeposit in Branch/Store {d} {street} {town} {st} {a4}",
    "MOBILE CHECK DEPOSIT REF {a10}",
    "DEPOSIT ID NUMBER {a6}",
)

_P2P: Final[tuple[tuple[str, str, int], ...]] = (
    ("Zelle", "ZELLE PAYMENT TO {n} {a11}", -1),
    ("Zelle", "Zelle payment to {n} Conf# {x9}", -1),
    ("Zelle", "Zelle payment from {n} Conf# {x9}", 1),
    ("Zelle", "ZELLE FROM {n} ON {d} REF # {x12}", 1),
    ("Zelle", "ZELLE TO {n} ON {d} REF # {x12} RENT", -1),
    ("Zelle", "Zelle Transfer Conf# {x9}; {rn}", -1),
    ("Zelle", "ZELLE PAYMENT FROM {n} {a11}", 1),
    ("Zelle", 'Zelle payment to {n} for "utilities"; Conf# {x9}', -1),
    ("Venmo", "VENMO DES:PAYMENT ID:{a13} INDN:{n} CO ID:{a10} WEB", -1),
    ("Venmo", "VENMO PAYMENT {a13} WEB ID: {a10} {n}", -1),
    ("Venmo", "VENMO* {n} Visa Direct NY", -1),
    (
        "Venmo",
        "ORIG CO NAME:VENMO ORIG ID:{a10} DESC DATE:{ymd} CO ENTRY DESCR:PAYMENT "
        "SEC:WEB TRACE#:{a15} EED:{ymd} IND ID:{a13} IND NAME:{n} TRN: {a10}TC",
        -1,
    ),
    ("Cash App", "CASH APP*{n} {phone} CA", -1),
    ("Cash App", "CASH APP*{n}", -1),
)


def _slots(pattern: str, dice: _Dice) -> tuple[str, tuple[str, ...], str | None]:
    """Fill a pattern; also the people and the employer it printed."""
    person = dice.person()
    employer = dice.pick(_EMPLOYERS)
    town, state = dice.town()
    out: list[str] = []
    people: list[str] = []
    used_employer: str | None = None
    rest = pattern
    while rest:
        head, brace, tail = rest.partition("{")
        out.append(head)
        if not brace:
            break
        slot, _, rest = tail.partition("}")
        if slot == "n":
            out.append(person)
            people.append(person)
        elif slot == "rn":
            out.append(_reversed_name(person))
            people.append(person)
        elif slot == "emp":
            out.append(employer)
            used_employer = employer
        elif slot == "d":
            out.append(dice.mmdd())
        elif slot == "ymd":
            out.append(dice.yymmdd())
        elif slot == "street":
            out.append(dice.pick(_STREETS))
        elif slot == "town":
            out.append(town)
        elif slot == "st":
            out.append(state)
        elif slot == "phone":
            out.append(dice.phone())
        elif slot[0] == "a":
            out.append(dice.digits(int(slot[1:])))
        elif slot[0] == "x":
            out.append(dice.alnum(int(slot[1:])))
        else:  # pragma: no cover - a typo in a pattern
            raise ValueError(f"unknown slot {slot!r}")
    return "".join(out), tuple(dict.fromkeys(people)), used_employer


def _non_purchase() -> list[Labelled]:
    lines: list[Labelled] = []

    def add(
        pattern: str,
        kind: str,
        category: str | None,
        sign: int,
        turn: int,
        merchant: str | None = None,
    ) -> None:
        text, people, employer = _slots(pattern, _Dice(f"other:{kind}:{pattern}:{turn}"))
        lines.append(
            Labelled(
                text=text,
                group="non_purchase",
                merchant=merchant or (employer.title() if employer else None),
                category=category,
                kind=kind,
                sign=1 if sign > 0 else -1,
                private=people,
            )
        )

    for turn in range(2):
        for pattern, sign in _TRANSFER:
            add(pattern, "transfer", "transfers.between_accounts", sign, turn)
        for pattern in _CARD_PAYMENT:
            add(pattern, "card_payment", "transfers.credit_card_payment", -1, turn)
        for pattern in _CASH:
            add(pattern, "cash", "transfers.cash", -1, turn)
        for pattern in _PAYROLL:
            add(pattern, "payroll", "income.salary", 1, turn)
    for pattern in _CARD_PAYMENT_PLAIN:
        # Seen from the card: the payment arrives.
        add(pattern, "card_payment", "transfers.credit_card_payment", 1, 0)
    for pattern in _LOAN_PAYMENT:
        add(pattern, "loan_payment", "transfers.loan_payment", -1, 0)
    for pattern in _FEE:
        add(pattern, "fee", "financial.bank_fees", -1, 0)
    for pattern, sign in _INTEREST:
        add(
            pattern,
            "interest",
            "income.interest" if sign > 0 else "financial.interest_paid",
            sign,
            0,
        )
    for pattern in _REFUND:
        add(pattern, "refund", "income.refunds", 1, 0)
    for pattern in _CHECK:
        add(pattern, "check", None, -1, 0)
    for pattern in _DEPOSIT:
        add(pattern, "deposit", None, 1, 0)
    for service, pattern, sign in _P2P:
        add(pattern, "p2p", None, sign, 0, merchant=service)
    return lines


def _build() -> tuple[tuple[Labelled, ...], Mapping[str, str]]:
    known, shape_of = _known()
    invented = _invented()
    other = _non_purchase()
    for lines, wanted in (
        (known, KNOWN_LINES),
        (invented, INVENTED_LINES),
        (other, NON_PURCHASE_LINES),
    ):
        if len(lines) != wanted:  # pragma: no cover - the tables and the counts disagree
            raise ValueError(f"{len(lines)} lines where {wanted} are wanted")
    return (*known, *invented, *other), MappingProxyType(shape_of)


_BUILT: Final = _build()
LABELLED: Final[tuple[Labelled, ...]] = _BUILT[0]
#: The shape of each ``known`` line, by its text (one of :data:`SHAPES`).
SHAPE_OF: Final[Mapping[str, str]] = _BUILT[1]

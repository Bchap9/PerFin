# SPDX-License-Identifier: AGPL-3.0-or-later
"""Payee clean-up: from the text a bank printed to who was paid.

Every descriptor here is invented in the shapes banks print; a merchant in
the labelled set carries its public brand name and nothing else that is real
(telephone numbers are in the fictional ``555-01xx`` range). First half: the
canonical text, the kind, labelled ACH lines and the bank's trailers. Second
half: processors (``via``), places, telephone numbers, store and order
numbers, web endings and company suffixes, down to the core.
"""

from __future__ import annotations

import re
import time

import pytest

from app.domain.categorisation import cleanup
from app.domain.categorisation.cleanup import Cleaned, canonical, clean
from app.domain.transactions.models import PAYEE_MAX
from tests.fixtures.descriptors import LABELLED

KEY_FORM = re.compile(r"^[a-z0-9]+( [a-z0-9]+)*$")
DIGIT_RUN = re.compile(r"\d{2,}")


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        # The twelve the plan names.
        ("POS DEBIT 0042 QUIET BEAN COFFEE", "purchase"),
        ("RECURRING PAYMENT AUTHORIZED ON 04/12 STREAMBOX.COM", "recurring_purchase"),
        ("ONLINE TRANSFER TO SAV ...1234", "transfer"),
        ("ZELLE PAYMENT TO JORDAN SAMPLE", "p2p"),
        ("ATM WITHDRAWAL 004512 MAIN ST", "cash"),
        ("MONTHLY MAINTENANCE FEE", "fee"),
        ("INTEREST PAYMENT", "interest"),
        ("DIRECT DEP MAPLE STUDIO PAYROLL", "payroll"),
        ("PAYMENT THANK YOU - WEB", "card_payment"),
        ("CHECK # 1042", "check"),
        ("MOBILE CHECK DEPOSIT", "deposit"),
        ("QUIET BEAN COFFEE", "unknown"),
        # Card purchases as other banks print them.
        ("Point of Sale Debit L340 DATE 04-12 LANTERN BOOKS", "purchase"),
        ("DEBIT CARD PURCHASE XXXXX7731 LANTERN BOOKS", "purchase"),
        ("POS DEBIT - VISA CHECK CARD 7731 - LANTERN BOOKS", "purchase"),
        ("CHECKCARD 0412 LANTERN BOOKS #123 SPRINGFIELD IL", "purchase"),
        ("PURCHASE AUTHORIZED ON 04/12 HARBOR FUEL 0091234", "purchase"),
        ("Card Purchase 01/14 Corner Grocer #204", "purchase"),
        ("VISA DDA PUR 448120 CORNER GROCER", "purchase"),
        ("RECURRING DEBIT CARD STREAMBOX USA", "recurring_purchase"),
        ("CHECKCARD 0412 STREAMBOX.COM 24055230412000187654321 RECURRING", "recurring_purchase"),
        # Automated clearing house and bill pay.
        ("ACH DEBIT OAKFIELD RENTALS", "ach_debit"),
        ("PREAUTHORIZED DEBIT RIVERBEND WATER", "ach_debit"),
        ("ACH CREDIT MAPLE STUDIO", "ach_credit"),
        ("ONLINE PAYMENT TO RIVERBEND WATER", "ach_debit"),
        ("DIRECT DEPOSIT RIVERBEND WATER REBATE", "ach_credit"),
        # Money between a person's own accounts.
        ("Online Banking transfer from CHK 4410 Confirmation# 5512098871", "transfer"),
        ("RECURRING TRANSFER TO SAVINGS 4410 REF #OP0X7K2M9Q", "transfer"),
        ("Deposit Transfer From ****4410", "transfer"),
        # Card and loan payments.
        ("ONLINE PAYMENT, THANK YOU", "card_payment"),
        ("AUTOPAY PAYMENT - THANK YOU", "card_payment"),
        ("AUTOPAY", "card_payment"),
        ("ONLINE BANKING PAYMENT TO CRD 4410 CONFIRMATION# 5512098871", "card_payment"),
        ("DISCOVER E-PAYMENT 4410 WEB ID: 5512098871", "card_payment"),
        ("TRANSFER TO LOAN 4410", "loan_payment"),
        ("LOAN PMT - PRINCIPAL AND INTEREST", "loan_payment"),
        ("MORTGAGE PAYMENT 5512098871", "loan_payment"),
        # Cash, cheques and deposits.
        ("NON-CHASE ATM WITHDRAW 004512 04/12 MAIN ST SPRINGFIELD IL", "cash"),
        ("CASH WITHDRAWAL IN BRANCH 04/12", "cash"),
        ("ATM CHECK DEPOSIT 04/12 MAIN ST SPRINGFIELD IL", "deposit"),
        ("COUNTER CREDIT", "deposit"),
        ("CHECK 1042", "check"),
        ("CHECK #1042 PAID", "check"),
        # Fees and interest.
        ("NON-CHASE ATM FEE-WITH", "fee"),
        ("WIRE TRANSFER FEE", "fee"),
        ("OVERDRAFT FEE FOR A TRANSACTION POSTED ON 04/12", "fee"),
        ("INTEREST CHARGE ON PURCHASES", "interest"),
        ("PURCHASE INTEREST CHARGE", "interest"),
        ("Interest Earned", "interest"),
        # Refunds, with and without a merchant.
        ("POS REFUND 04/12 CARD 7731", "refund"),
        ("PURCHASE RETURN AUTHORIZED ON 04/12 CARD 7731", "refund"),
        ("REFUND LANTERN BOOKS", "refund"),
        # Person to person.
        ("Zelle payment from JORDAN SAMPLE Conf# a1b2c3d4e", "p2p"),
        ("VENMO PAYMENT 1029384756123 WEB ID: 5512098871", "p2p"),
        ("CASH APP*JORDAN SAMPLE", "p2p"),
        # Payroll.
        ("PAYROLL DEPOSIT MAPLE STUDIO", "payroll"),
        ("MAPLE STUDIO PAYROLL PPD ID: 5512098871", "payroll"),
    ],
)
def test_kind(raw: str, kind: str) -> None:
    assert clean(raw).kind == kind


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        # A word that also begins a bank's own line does not make the line one.
        ("POS DEBIT 0042 COFFEE FEED STORE", "purchase"),
        ("LANTERN CLUB ANNUAL MEMBERSHIP FEE", "unknown"),
        ("INTEREST BOOKS AND MAPS", "unknown"),
        ("CHECKERED FLAG DINER", "unknown"),
        ("CHECK INTO HARBOR 12 MOTEL", "unknown"),
        ("TRANSFER STATION CAFE", "unknown"),
        ("DEPOSIT BOTTLE SHOP", "unknown"),
        ("REFUNDABLE GOODS OUTLET", "unknown"),
        ("RETURNED ITEM FEE", "fee"),
        ("ZELLERS MARKET", "unknown"),
        ("VENMORE GARDEN SUPPLY", "unknown"),
        ("AUTOPAY RIVERBEND WATER", "unknown"),
        ("PAYMENT THANKFUL HARVEST FARM", "unknown"),
        ("ATMOSPHERE YOGA STUDIO", "unknown"),
        ("RECURRING THEME RECORDS", "unknown"),
    ],
)
def test_kind_negatives(raw: str, kind: str) -> None:
    assert clean(raw).kind == kind


def test_recurring_signal() -> None:
    cleaned = clean("RECURRING PAYMENT AUTHORIZED ON 04/12 STREAMBOX.COM")
    assert cleaned.signals == frozenset({"recurring"})
    assert cleaned.core == "streambox"
    tail = clean("CHECKCARD 0412 STREAMBOX.COM 24055230412000187654321 RECURRING")
    assert "recurring" in tail.signals
    assert "recurring" not in tail.core.split()
    assert clean("POS DEBIT 0042 QUIET BEAN COFFEE").signals == frozenset()


def test_the_bank_prefix_leaves_the_core() -> None:
    assert clean("DEBIT CARD PURCHASE XXXXX7731 LANTERN BOOKS").core == "lantern books"
    assert clean("Point of Sale Debit L340 DATE 04-12 LANTERN BOOKS").core == "lantern books"
    assert clean("ACH DEBIT OAKFIELD RENTALS").core == "oakfield rentals"
    assert clean("REFUND LANTERN BOOKS") == Cleaned(core="lantern books", kind="refund")
    assert clean("QUIET BEAN COFFEE") == Cleaned(core="quiet bean coffee")


def test_labelled_ach_keeps_the_company_and_drops_the_person() -> None:
    cleaned = clean("OAKFIELD RENTALS DES:RENT ID:88123 INDN:JORDAN SAMPLE CO ID:XXXXX12345 PPD")
    assert cleaned.kind == "ach_debit"
    assert cleaned.signals == frozenset({"descr:rent"})
    assert cleaned.core == "oakfield rentals"
    tokens = cleaned.core.split()
    assert "jordan" not in tokens
    assert "sample" not in tokens
    assert not DIGIT_RUN.search(cleaned.core)


def test_labelled_ach_in_the_long_layout() -> None:
    cleaned = clean(
        "ORIG CO NAME:RIVERBEND WATER ORIG ID:5512098871 DESC DATE:260412 "
        "CO ENTRY DESCR:WATER BILL SEC:WEB TRACE#:021000021234567 EED:260412 "
        "IND ID:004512887 IND NAME:SAMPLE,JORDAN TRN: 1020304050TC"
    )
    assert cleaned == Cleaned(
        core="riverbend water", kind="ach_debit", signals=frozenset({"descr:water bill"})
    )


@pytest.mark.parametrize(
    ("raw", "kind", "core"),
    [
        (
            "MAPLE STUDIO DES:PAYROLL ID:5512098871 INDN:JORDAN SAMPLE CO ID:1020304050 PPD",
            "payroll",
            "maple studio",
        ),
        (
            "MAPLE STUDIO DES:DIRECT DEP ID:551209887 INDN:SAMPLE,JORDAN CO ID:1020304050 PPD",
            "payroll",
            "maple studio",
        ),
        (
            "CHASE CREDIT CRD DES:AUTOPAY ID:5512098871 INDN:JORDAN SAMPLE CO ID:1020304050 PPD",
            "card_payment",
            "",
        ),
        (
            "CAPITAL ONE DES:CRCARDPMT ID:A1B2C3D4E5F6 INDN:SAMPLE,JORDAN CO ID:1020304050 WEB",
            "card_payment",
            "",
        ),
        (
            "DEPT EDUCATION DES:STUDENT LN ID:5512098871 INDN:JORDAN SAMPLE CO ID:1020304050 PPD",
            "loan_payment",
            "dept education",
        ),
        (
            "VENMO DES:PAYMENT ID:1029384756123 INDN:JORDAN SAMPLE CO ID:1020304050 WEB",
            "p2p",
            "venmo",
        ),
        # A company that is not a card issuer is not paid "by card payment".
        (
            "RIVERBEND WATER DES:AUTOPAY ID:5512098871 INDN:JORDAN SAMPLE CO ID:1020304050 PPD",
            "ach_debit",
            "riverbend water",
        ),
        # A bank prefix in front of a labelled line still decides debit or credit.
        (
            "ACH CREDIT OAKFIELD RENTALS DES:REFUND ID:88123 INDN:JORDAN SAMPLE CO ID:1020304050",
            "ach_credit",
            "oakfield rentals",
        ),
    ],
)
def test_labelled_ach_kinds(raw: str, kind: str, core: str) -> None:
    cleaned = clean(raw)
    assert (cleaned.kind, cleaned.core) == (kind, core)


def test_generic_ach_ids_are_removed() -> None:
    cleaned = clean("RIVERBEND WATER BILLPAY PPD ID: 5512098871 TRACE# 021000021234567")
    assert cleaned.core == "riverbend water billpay"
    for raw in (
        "OAKFIELD RENTALS REF# 88123A",
        "OAKFIELD RENTALS REF # 88123A",
        "OAKFIELD RENTALS CONF# A1B2C3D4E",
        "OAKFIELD RENTALS AUTH# 004512",
        "OAKFIELD RENTALS WEB ID: 5512098871",
    ):
        assert clean(raw).core == "oakfield rentals", raw
    # "REF" and "AUTH" inside or before an ordinary word are left alone.
    assert clean("AUTH ENTIC TACOS").core == "auth entic tacos"
    assert clean("REF RESH JUICE BAR").core == "ref resh juice bar"


def test_trailers_are_removed() -> None:
    cleaned = clean(
        "PURCHASE AUTHORIZED ON 04/12 HARBOR FUEL 0091234 SPRINGFIELD IL S385102345678901 CARD 1234"
    )
    assert cleaned.kind == "purchase"
    tokens = cleaned.core.split()
    assert tokens[:2] == ["harbor", "fuel"]
    assert "04" not in tokens
    assert "12" not in tokens
    assert "s385102345678901" not in tokens
    assert "card" not in tokens
    assert "1234" not in tokens


@pytest.mark.parametrize(
    ("raw", "core"),
    [
        ("LANTERN BOOKS CARD 7731", "lantern books"),
        ("LANTERN BOOKS CARD ENDING IN 7731", "lantern books"),
        ("LANTERN BOOKS XXXX7731", "lantern books"),
        ("LANTERN BOOKS XXXXXXXXXXXX7731", "lantern books"),
        ("LANTERN BOOKS P385102345678901", "lantern books"),
        ("LANTERN BOOKS 04/12", "lantern books"),
        ("LANTERN BOOKS 04/12/26", "lantern books"),
        ("LANTERN BOOKS 04/12/2026 13:05", "lantern books"),
        ("LANTERN BOOKS 13:05:59", "lantern books"),
        ("LANTERN BOOKS US", "lantern books"),
        ("LANTERN BOOKS USA", "lantern books"),
        ("LANTERN BOOKS 04/12 USA CARD 7731", "lantern books"),
        # Not trailers: a word that only looks like one.
        ("CARD SHARK GAMES", "card shark games"),
        ("POSTCARD 1234 PRESS", "postcard 1234 press"),
        ("US CELLAR WINES", "us cellar wines"),
        ("TOYS R US", "toys r us"),
        ("LANTERN BOOKS A1234", "lantern books a1234"),
        ("LANTERN BOOKS 4/5", "lantern books 4 5"),
    ],
)
def test_trailer_forms(raw: str, core: str) -> None:
    assert clean(raw).core == core


def test_canonical() -> None:
    assert canonical("  Quiet\tBean \n Coffee ") == "QUIET BEAN COFFEE"
    # NFKC: full-width letters, a ligature, a no-break space.
    assert canonical("\uff31\uff55\uff49\uff45\uff54\u00a0\ufb01sh") == "QUIET FISH"
    # Control and format characters go: NUL, a zero-width space, a right-to-left override.
    assert canonical("QUI\x00ET\u200b BE\u202eAN") == "QUIET BEAN"
    assert len(canonical("A" * (PAYEE_MAX + 100))) == PAYEE_MAX
    # A character that grows when folded cannot push the text past the limit.
    assert len(canonical("\ufdfa" * PAYEE_MAX)) <= PAYEE_MAX


def test_empty_and_none() -> None:
    for raw in (None, "", "   ", "\x00\u200b", "***", "-- / --"):
        assert clean(raw) == Cleaned(core=""), repr(raw)
    assert canonical(None) == ""
    assert canonical("") == ""


def test_the_core_keeps_the_key_form() -> None:
    for raw in (
        "POS DEBIT 0042 QUIET BEAN COFFEE",
        "SQ *CORNER GROCER 0412 SPRINGFIELD IL",
        "Café Lumière #12",
        "OAKFIELD RENTALS DES:RENT ID:88123 INDN:JORDAN SAMPLE CO ID:XXXXX12345 PPD",
    ):
        assert KEY_FORM.match(clean(raw).core), raw


def test_the_patterns_are_constants() -> None:
    for pattern, _kind, _signal in (*cleanup.KIND_LINES, *cleanup.KIND_PREFIXES):
        assert isinstance(pattern, re.Pattern)
    assert cleanup.CLEANER_VERSION == 1


@pytest.mark.parametrize(
    "unit",
    [
        "A *",
        "* ",
        "(([[--//",
        "REF # ",
        "CARD 1234 ",
        "DES: ID: INDN: ",
        "ORIG CO NAME:",
        "MONTHLY ",
        "04/12 ",
        "X",
        "9",
        "POS DEBIT ",
        "SQ *",
        "#1 ",
        "*A1B2C3 ",
        "800-555-0142 ",
        "STORE 123 ",
        "A.COM ",
        "WWW.",
        "'S ",
        "ALDER IL ",
        "INC ",
    ],
)
def test_long_and_hostile_input_is_linear(unit: str) -> None:
    raw = (unit * PAYEE_MAX)[:PAYEE_MAX]
    clean(raw)  # the first call pays for anything lazy
    started = time.perf_counter()
    cleaned = clean(raw)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.05, f"{elapsed:.3f}s for {unit!r}"
    assert cleaned.core == "" or KEY_FORM.match(cleaned.core)
    # Longer than a payee can be: only the first 500 characters are read.
    started = time.perf_counter()
    clean(unit * 20_000)
    assert time.perf_counter() - started < 0.05


# --- second half: processors, tails, numbers, the core ------------------------


@pytest.mark.parametrize(
    ("raw", "core", "via"),
    [
        # The twelve the plan names (telephone numbers replaced by invented ones).
        ("POS DEBIT 0042 QUIET BEAN COFFEE", "quiet bean coffee", None),
        ("Card Purchase 01/14 Corner Grocer #204 Springfield IL Card 7731", "corner grocer", None),
        ("SQ *CORNER GROCER 0412 SPRINGFIELD IL", "corner grocer", "square"),
        # No state follows, so nothing says SPRINGFIELD is a place: it stays.
        ("TST* HARBOR PIZZA - SPRINGFIELD", "harbor pizza springfield", "toast"),
        ("PAYPAL *STREAMBOX 402-555-0133 CA", "streambox", "paypal"),
        ("STREAMBOX.COM", "streambox", None),
        ("STREAMBOX *MONTHLY", "streambox monthly", None),
        ("CHECKCARD 0412 LANTERN BOOKS #123 SPRINGFIELD IL", "lantern books", None),
        ("APPLE.COM/BILL 866-555-0153 CA", "apple com bill", None),
        ("AMZN MKTP US*7T4TU7Z13 AMZN.COM/BILL WA", "amzn mktp us", None),
        ("MAPLE BAKERY LLC", "maple bakery", None),
        ("TRADER JOE S #123", "trader joe", None),
        # The same shop through other wrappings gives the same core.
        ("TRADER JOE'S #6089 WINDROW KS", "trader joe", None),
        ("VISA DDA PUR 448120 CORNER GROCER #204 SPRINGFIELD IL", "corner grocer", None),
        (
            "CHECKCARD 0412 CORNER GROCER SPRINGFIELD IL 24055230412000187654321 "
            "CKCD 5411 XXXXXXXXXXXX7731 XXXX XXXX XXXX 7731",
            "corner grocer",
            None,
        ),
        ("POS DEBIT SQ *CORNER GROCER SPRINGFIELD IL", "corner grocer", "square"),
        ("PURCHASE AUTHORIZED ON 04/12 TST* HARBOR PIZZA SPRINGFIELD IL", "harbor pizza", "toast"),
    ],
)
def test_core(raw: str, core: str, via: str | None) -> None:
    cleaned = clean(raw)
    assert (cleaned.core, cleaned.via) == (core, via)


@pytest.mark.parametrize(
    ("raw", "core", "via"),
    [
        ("SQ *SQ *HARBOR PIZZA", "harbor pizza", "square"),
        ("SQ *HARBOR PIZZA", "harbor pizza", "square"),
        ("GOSQ.COM HARBOR PIZZA", "harbor pizza", "square"),
        ("TST*HARBOR PIZZA", "harbor pizza", "toast"),
        ("TST *HARBOR PIZZA", "harbor pizza", "toast"),
        ("PP*LANTERN BOOKS", "lantern books", "paypal"),
        ("DD *HARBOR PIZZA", "harbor pizza", "doordash"),
        ("DOORDASH*HARBOR PIZZA", "harbor pizza", "doordash"),
        ("IC* CORNER GROCER", "corner grocer", "instacart"),
        ("GH *HARBOR PIZZA", "harbor pizza", "grubhub"),
        ("GRUBHUB*HARBOR PIZZA", "harbor pizza", "grubhub"),
        ("PADDLE.NET* STREAMBOX", "streambox", "paddle"),
        ("FS *STREAMBOX", "streambox", "fastspring"),
        ("DRI*STREAMBOX", "streambox", "digitalriver"),
        ("BT*LANTERN BOOKS", "lantern books", "braintree"),
        ("WPY*MAPLE STUDIO", "maple studio", "wepay"),
        ("IN *MAPLE STUDIO", "maple studio", "intuit"),
        ("EB *LANTERN FESTIVAL", "lantern festival", "eventbrite"),
        ("ACT*RIVERBEND SWIM CLUB", "riverbend swim club", "active"),
        ("ABC*HARBOR FITNESS", "harbor fitness", "abc"),
        ("ACI*RIVERBEND WATER", "riverbend water", "aci"),
        ("SP * LANTERN BOOKS", "lantern books", "shopify"),
        ("SP LANTERN BOOKS", "lantern books", "shopify"),
    ],
)
def test_processors(raw: str, core: str, via: str | None) -> None:
    cleaned = clean(raw)
    assert (cleaned.core, cleaned.via) == (core, via)


@pytest.mark.parametrize(
    ("raw", "core"),
    [
        # Marketplaces and platforms are the merchant, not a way to one.
        ("APPLE.COM/BILL", "apple com bill"),
        ("GOOGLE *STREAMBOX", "google streambox"),
        ("AMZN MKTP", "amzn mktp"),
        ("AMAZON PRIME", "amazon prime"),
        ("UBER *TRIP", "uber trip"),
        ("LYFT *RIDE SUN 7PM", "lyft ride sun 7pm"),
        ("MICROSOFT*STORE", "microsoft store"),
        # A name that only begins like a processor.
        ("SQUARE ONE DINER", "square one diner"),
        ("SQ FT FLOORING", "sq ft flooring"),
        ("TSTREET TACOS", "tstreet tacos"),
        ("PAYPAL", "paypal"),
        ("PPG PAINT SHOP", "ppg paint shop"),
        ("DDS DISCOUNTS", "dds discounts"),
        ("DOORDASH", "doordash"),
        ("GRUBHUB HARBOR PIZZA", "grubhub harbor pizza"),
        ("INN AT THE HARBOR", "inn at the harbor"),
        ("IN AND OUT CAR WASH", "in and out car wash"),
        ("ACT TWO THEATRE", "act two theatre"),
        ("ABC FINE WINE", "abc fine wine"),
        ("BT CYCLES", "bt cycles"),
        ("FS GARAGE", "fs garage"),
        ("SPORTS HUB", "sports hub"),
        # Shopify's bare prefix needs two words behind it.
        ("SP LANTERN", "sp lantern"),
        # A processor with nobody behind it is all the line names.
        ("PAYPAL *", "paypal"),
        ("SQ *", "sq"),
    ],
)
def test_not_a_processor(raw: str, core: str) -> None:
    cleaned = clean(raw)
    assert (cleaned.core, cleaned.via) == (core, None)


@pytest.mark.parametrize(
    ("raw", "core"),
    [
        # Telephone numbers, delimited and toll-free without delimiters.
        ("STREAMBOX 800-555-0142", "streambox"),
        ("STREAMBOX 800.555.0142 CA", "streambox"),
        ("STREAMBOX (800) 555-0142 CA", "streambox"),
        ("STREAMBOX 1-800-555-0142", "streambox"),
        ("STREAMBOX 8005550142 CA", "streambox"),
        ("STREAMBOX 18885550142", "streambox"),
        ("LANTERN BOOKS 555-0142 IL", "lantern books"),
        # A ZIP, with and without the state.
        ("LANTERN BOOKS SPRINGFIELD IL 62799", "lantern books"),
        ("LANTERN BOOKS SPRINGFIELD IL 62799-0142", "lantern books"),
        ("LANTERN BOOKS 62799", "lantern books"),
        # The state and the city in front of it.
        ("LANTERN BOOKS SPRINGFIELD IL", "lantern books"),
        ("LANTERN BOOKS WASHINGTON DC", "lantern books"),
        ("LANTERN BOOKS PONCE PR", "lantern books"),
        ("HARBOR FREIGHT DEPOT NEW ALDERTON CT", "harbor freight depot"),
        ("HARBOR FREIGHT DEPOT ALDER FALLS MT", "harbor freight depot"),
        ("HARBOR FREIGHT DEPOT ALDERTON MT", "harbor freight depot"),
        ("LANTERN BOOKS #12 SALT ALDER CITY UT", "lantern books"),
        ("LANTERN BOOKS 0412 EAST ALDERTON MA", "lantern books"),
        ("MOTEL 6 ALDERTON TX", "motel 6"),
        ("LANTERN BOOKS ALDERTON IN", "lantern books"),
        ("LANTERN BOOKS #12 ALDERTON CO", "lantern books"),
        ("STREAMBOX 800-555-0142 CO", "streambox"),
        # Store, terminal and order numbers.
        ("LANTERN BOOKS #123", "lantern books"),
        ("LANTERN BOOKS # 123", "lantern books"),
        ("LANTERN BOOKS #R7731", "lantern books"),
        ("LANTERN BOOKS STORE 123", "lantern books"),
        ("LANTERN BOOKS STORE - 0123 ALDERTON MT", "lantern books"),
        ("LANTERN BOOKS STR 123", "lantern books"),
        ("LANTERN BOOKS T-1234 ALDERTON OH", "lantern books"),
        ("LANTERN BOOKS F12345", "lantern books"),
        ("STREAMBOX *Y90D93VU", "streambox"),
        ("STREAMBOX*79WC0KSD", "streambox"),
        ("LANTERN BOOKS 123", "lantern books"),
        ("LANTERN BOOKS 4729-01", "lantern books"),
        ("LANTERN BOOKS 0412 0091234", "lantern books"),
        ("LANTERN 06581 ONLINE", "lantern online"),
        ("STREAMBOX GSZ9B79Z", "streambox"),
        # Web endings.
        ("WWW.STREAMBOX.COM", "streambox"),
        ("STREAMBOX.NET", "streambox"),
        ("STREAMBOX.ORG", "streambox"),
        ("STREAMBOX.IO", "streambox"),
        ("STREAMBOX.CO", "streambox"),
        ("STREAMBOX.TV", "streambox"),
        ("STREAMBOX.US 888-555-0199", "streambox"),
        ("LANTERN&BOOKS.COM", "lantern books"),
        ("STREAMBOX STREAMBOX.COM CA", "streambox"),
        # Company suffixes and possessives.
        ("MAPLE BAKERY INC", "maple bakery"),
        ("MAPLE BAKERY, INC.", "maple bakery"),
        ("MAPLE BAKERY CORP", "maple bakery"),
        ("MAPLE BAKERY CO", "maple bakery"),
        ("MAPLE BAKERY LTD", "maple bakery"),
        ("MAPLE & DRAKE LLP", "maple drake"),
        ("MAPLE BAKERY CO INC", "maple bakery"),
        ("HARLOW'S BAKERY", "harlow bakery"),
        ("HARLOW S BAKERY", "harlow bakery"),
        ("HARLOW\u2019S BAKERY", "harlow bakery"),
    ],
)
def test_tails_and_numbers(raw: str, core: str) -> None:
    assert clean(raw).core == core


@pytest.mark.parametrize(
    ("raw", "core"),
    [
        # A number that is part of the name.
        ("PHILLIPS 66", "phillips 66"),
        ("FOREVER 21", "forever 21"),
        ("STUDIO 54 DANCE", "studio 54 dance"),
        ("24 HOUR FITNESS", "24 hour fitness"),
        ("POS DEBIT 24 HOUR FITNESS", "24 hour fitness"),
        ("WAY2SAVE GROCERY", "way2save grocery"),
        ("LANTERN STUDIO54", "lantern studio54"),
        ("1234 LANTERN", "1234 lantern"),
        ("#1 NOODLE HOUSE", "1 noodle house"),
        ("76 #1234 ALDERTON CA", "76"),
        ("STORE 24 MARKET", "store 24 market"),
        # A word is not an order number, whatever precedes it.
        ("STREAMBOX *MONTHLY", "streambox monthly"),
        ("STREAMBOX *MEMBERSHIP", "streambox membership"),
        # Two letters at the end that are a word here, not a state.
        ("DRIFTWOOD COFFEE CO", "driftwood coffee"),
        # ... so a town in Colorado stays when no number says where it begins.
        ("LANTERN BOOKS ALDERTON CO", "lantern books alderton"),
        ("STARLITE DRIVE IN", "starlite drive in"),
        ("TOYS R US", "toys r us"),
        # The name is never emptied for a place: two words stay.
        ("MASSAGE ENVY CA", "massage envy"),
        ("QUIET BEAN OR", "quiet bean"),
        ("OR", "or"),
        # An ending that is not a web ending, and one with a path behind it.
        ("LANTERN.COMICS", "lantern comics"),
        ("STREAMBOX.COM/BILL", "streambox com bill"),
        ("COM", "com"),
        (".COM", "com"),
        # A suffix word that is all there is, or not at the end.
        ("CO", "co"),
        ("INC MAGAZINE", "inc magazine"),
        ("S", "s"),
    ],
)
def test_tails_negatives(raw: str, core: str) -> None:
    assert clean(raw).core == core


def test_the_five_examples_the_old_dialog_pinned() -> None:
    examples = {
        "Card Purchase 01/14 Corner Grocer #204 Springfield IL Card 7731": "corner grocer",
        "OAKFIELD RENTALS DES:RENT ID:88123 INDN:JORDAN SAMPLE": "oakfield rentals",
        "POS DEBIT 0042 QUIET BEAN COFFEE": "quiet bean coffee",
        "Gusto Pay 123456 PPD ID: 9138864007": "gusto pay",
        "STREAMBOX *MONTHLY": "streambox monthly",
    }
    for raw, core in examples.items():
        cleaned = clean(raw)
        assert cleaned.core == core, raw
        folded = set(re.findall(r"[a-z0-9]+", raw.lower()))
        assert set(cleaned.core.split()) <= folded, raw


@pytest.mark.parametrize(
    ("raw", "core"),
    [
        ("ZELLE PAYMENT TO JORDAN SAMPLE", "zelle"),
        ("Zelle payment from JORDAN SAMPLE Conf# a1b2c3d4e", "zelle"),
        ('Zelle payment to JORDAN SAMPLE for "rent"; Conf# a1b2c3d4e', "zelle"),
        ("VENMO PAYMENT 1029384756123 WEB ID: 5512098871 JORDAN SAMPLE", "venmo"),
        ("CASH APP*JORDAN SAMPLE", "cash app"),
        ("VENMO DES:PAYMENT ID:1029384756123 INDN:JORDAN SAMPLE CO ID:1020304050 WEB", "venmo"),
    ],
)
def test_p2p_core_is_the_service(raw: str, core: str) -> None:
    cleaned = clean(raw)
    assert (cleaned.core, cleaned.kind, cleaned.via) == (core, "p2p", None)


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("ONLINE TRANSFER TO SAV ...1234", "transfer"),
        ("TRANSFER TO BROKERAGE ACCT 4410", "transfer"),
        ("PAYMENT THANK YOU - WEB", "card_payment"),
        (
            "CHASE CREDIT CRD DES:AUTOPAY ID:5512098871 INDN:JORDAN SAMPLE CO ID:1020304050",
            "card_payment",
        ),
        ("ATM WITHDRAWAL 004512 MAIN ST SPRINGFIELD IL", "cash"),
        ("CHECK # 1042", "check"),
        ("NSF RETURNED ITEM FEE FOR ACTIVITY OF 04/12", "fee"),
        ("INTEREST CHARGE ON PURCHASES", "interest"),
        ("ATM CHECK DEPOSIT 04/12 MAIN ST SPRINGFIELD IL", "deposit"),
    ],
)
def test_non_purchases_have_no_core(raw: str, kind: str) -> None:
    cleaned = clean(raw)
    assert cleaned.kind == kind
    assert (cleaned.core, cleaned.via) == ("", None)


def test_a_number_alone_is_not_a_core() -> None:
    assert clean("MORTGAGE PAYMENT 5512098871") == Cleaned(core="", kind="loan_payment")
    assert clean("POS RETURN 390269") == Cleaned(core="", kind="refund")
    assert clean("5512098871").core == ""
    assert clean("#1234").core == "1234"


def test_the_core_is_bounded() -> None:
    long_name = "ALDER BIRCH CEDAR DOGWOOD ELM FIR GINKGO HAZEL"
    assert clean(long_name).core == "alder birch cedar dogwood elm fir"
    assert cleanup.CORE_TOKENS_MAX == 6
    wide = clean(" ".join(["ALDERBIRCHCEDARDOGWOOD"] * 4))
    assert wide.core == "alderbirchcedardogwood alderbirchcedardogwood"
    assert len(clean("A" * 200).core) == 64


def test_core_form() -> None:
    for line in LABELLED:
        core = clean(line.text).core
        assert core == "" or KEY_FORM.match(core), line.text
        assert len(core) <= 64, line.text
        assert len(core.split()) <= cleanup.CORE_TOKENS_MAX, line.text


def test_kinds_on_the_labelled_set() -> None:
    other = [line for line in LABELLED if line.group == "non_purchase"]
    assert len(other) == 150
    right = sum(1 for line in other if clean(line.text).kind == line.kind)
    assert right / len(other) >= 0.97
    assert len(LABELLED) == 1200
    for line in LABELLED:
        named = {word for name in line.private for word in re.findall(r"[a-z0-9]+", name.lower())}
        assert not named & set(clean(line.text).core.split()), line.text


def test_processors_are_literal_and_longest_first() -> None:
    prefixes = [prefix for prefix, _via in cleanup.PROCESSORS]
    assert len(set(prefixes)) == len(prefixes)
    for index, prefix in enumerate(prefixes):
        assert prefix == prefix.upper()
        # No later prefix is hidden behind an earlier, shorter one.
        assert not any(prefix.startswith(earlier) for earlier in prefixes[:index]), prefix

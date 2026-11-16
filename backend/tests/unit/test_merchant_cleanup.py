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
    assert cleaned == Cleaned(core="harbor fuel", kind="purchase")


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


def test_accented_letters_are_folded_not_dropped() -> None:
    assert clean("Café Lumière #12").core == "cafe lumiere"
    assert clean("CAFE\u0301 LUMIE\u0300RE #12").core == "cafe lumiere"
    assert clean("CAFE LUMIERE #12").core == "cafe lumiere"
    assert clean("PEÑA'S TAQUERÍA").core == "pena taqueria"
    # A letter with no plain form is still a break, as before.
    assert clean("SMØRREBRØD HOUSE").core == "sm rrebr d house"


def test_the_patterns_are_constants() -> None:
    for pattern, _kind, _signal in (*cleanup.KIND_LINES, *cleanup.KIND_PREFIXES):
        assert isinstance(pattern, re.Pattern)
    assert cleanup.CLEANER_VERSION == 2


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
        "ONLINE ",
        "KEEP THE CHANGE ",
        "FUNDS TRANSFER 4410 ",
        "TRANSFER #X*.",
        "WITHDRAWAL PAYMENT ",
        "PAYMENT 5512098871 ",
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
        # Most shops behind Shopify have a name of one word.
        ("SP LANTERN", "lantern", "shopify"),
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
        # Shopify's bare prefix needs two words behind it, or one of four letters.
        ("SP AG", "sp ag"),
        ("SP", "sp"),
        # ... and "SP" in front of a word companies carry in their own name is the name.
        ("SP GLOBAL", "sp global"),
        ("SP PARKING", "sp parking"),
        ("SP GROUP", "sp group"),
        ("SP PLUS PARKING", "sp plus parking"),
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


# --- review of lane C ---------------------------------------------------------

#: What a bank prints in front of whoever was paid, one of each kind of prefix.
BANK_PREFIXES = (
    "POS DEBIT",
    "POS DEBIT 0042",
    "POS DEBIT 4412",
    "DEBIT CARD PURCHASE XXXXX7731",
    "PURCHASE AUTHORIZED ON 04/12",
    "CHECKCARD 0412",
    "VISA DDA PUR 448120",
    "RECURRING PAYMENT AUTHORIZED ON 04/12",
    "RECURRING DEBIT CARD",
    "ACH DEBIT",
    "ACH CREDIT",
    "ELECTRONIC WITHDRAWAL",
    "ONLINE PAYMENT TO",
    "BILL PAY",
    "POS REFUND",
    "DIRECT DEPOSIT",
)
#: A payment between people as each service prints it, and the service.
P2P_LINES = (
    ("ZELLE JORDAN SAMPLE", "zelle"),
    ("ZELLE PAYMENT TO JORDAN SAMPLE", "zelle"),
    ("ZELLE TO JORDAN SAMPLE ON 04/12 REF # A1B2C3D4E5F6 RENT", "zelle"),
    ("VENMO* JORDAN SAMPLE", "venmo"),
    ("VENMO *JORDAN SAMPLE VISA DIRECT NY", "venmo"),
    ("VENMO PAYMENT 1029384756123 JORDAN SAMPLE", "venmo"),
    ("CASH APP*JORDAN SAMPLE", "cash app"),
    ("CASH APP*JORDAN SAMPLE 8005550142 CA", "cash app"),
    ("CASH APP*JORDAN SAMPLE CA S385102345678901 CARD 1234", "cash app"),
    ("SQC*JORDAN SAMPLE", "cash app"),
)


@pytest.mark.parametrize("prefix", BANK_PREFIXES)
@pytest.mark.parametrize(("line", "service"), P2P_LINES)
def test_p2p_behind_a_bank_prefix_is_the_service(prefix: str, line: str, service: str) -> None:
    cleaned = clean(f"{prefix} {line}")
    assert (cleaned.core, cleaned.kind, cleaned.via) == (service, "p2p", None)


@pytest.mark.parametrize(
    ("raw", "core"),
    [
        (
            "ACH DEBIT VENMO DES:PAYMENT ID:1029384756123 INDN:JORDAN SAMPLE CO ID:1020304050 WEB",
            "venmo",
        ),
        (
            "ORIG CO NAME:VENMO ORIG ID:5512098871 DESC DATE:260412 CO ENTRY DESCR:PAYMENT "
            "SEC:WEB IND ID:004512887 IND NAME:JORDAN SAMPLE",
            "venmo",
        ),
        # A bank word between the bank's wording, or the start, and the service.
        ("WITHDRAWAL ZELLE TO JORDAN SAMPLE", "zelle"),
        ("ELECTRONIC WITHDRAWAL ONLINE PAYMENT ZELLE JORDAN SAMPLE", "zelle"),
        ("INSTANT PAYMENT CREDIT VENMO JORDAN SAMPLE", "venmo"),
        # Square's own print of Cash App.
        ("SQC*JORDAN SAMPLE", "cash app"),
        ("SQC *JORDAN SAMPLE", "cash app"),
    ],
)
def test_p2p_in_other_wrappings(raw: str, core: str) -> None:
    cleaned = clean(raw)
    assert (cleaned.core, cleaned.kind, cleaned.via) == (core, "p2p", None)


def test_p2p_behind_a_recurring_prefix_keeps_the_signal() -> None:
    cleaned = clean("RECURRING PAYMENT AUTHORIZED ON 04/12 CASH APP*JORDAN SAMPLE")
    assert cleaned == Cleaned(core="cash app", kind="p2p", signals=frozenset({"recurring"}))


@pytest.mark.parametrize(
    ("raw", "kind", "core"),
    [
        # A shop whose name only begins like a service, behind the same prefixes.
        ("POS DEBIT ZELLERS MARKET", "purchase", "zellers market"),
        (
            "DEBIT CARD PURCHASE XXXXX7731 VENMORE GARDEN SUPPLY",
            "purchase",
            "venmore garden supply",
        ),
        ("PURCHASE AUTHORIZED ON 04/12 CASH APPLIANCE OUTLET", "purchase", "cash appliance outlet"),
        ("ACH DEBIT CASH APPAREL CO", "ach_debit", "cash apparel"),
        ("POS DEBIT SQCUP COFFEE", "purchase", "sqcup coffee"),
        ("SQCUP COFFEE", "unknown", "sqcup coffee"),
        # The service's name further along is part of a shop's name.
        ("POS DEBIT LANTERN CASH APP KIOSK", "purchase", "lantern cash app kiosk"),
        ("WITHDRAWAL SYMPTOMS RECORDS", "unknown", "withdrawal symptoms records"),
    ],
)
def test_not_p2p_behind_a_bank_prefix(raw: str, kind: str, core: str) -> None:
    cleaned = clean(raw)
    assert (cleaned.kind, cleaned.core) == (kind, core)


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        # A card payment in the wording of other issuers.
        ("INTERNET PAYMENT - THANK YOU", "card_payment"),
        ("ELECTRONIC PAYMENT THANK YOU", "card_payment"),
        ("WEB PAYMENT, THANK YOU", "card_payment"),
        ("ONLINE PYMT THANK YOU", "card_payment"),
        ("PMT - THANK YOU", "card_payment"),
        ("ONLINE PAYMENT 5512098871 TO CHASE CARD ENDING IN 4410", "card_payment"),
        ("CAPITAL ONE MOBILE PMT 5512098871 WEB ID: 1020304050", "card_payment"),
        ("CITI CARD ONLINE PAYMENT 5512098871 WEB ID: 1020304050", "card_payment"),
        ("DISCOVER ONLINE PMT 4410", "card_payment"),
        # Money between a person's own accounts, in other wording.
        ("ONLINE SCHEDULED TRANSFER TO CHK 4410 CONFIRMATION# 5512098871", "transfer"),
        ("KEEP THE CHANGE TRANSFER TO ACCT 4410 FOR 04/12", "transfer"),
        ("SAVE AS YOU GO TRANSFER DEBIT TO XXXXXXXXXXX4410", "transfer"),
        ("XFER TO SAV 4410", "transfer"),
        ("ONLINE XFER TO SAVINGS 4410", "transfer"),
        ("ACH TRANSFER TO SAVINGS 4410", "transfer"),
        ("TRANSFER 4410 TO SAVINGS", "transfer"),
        ("FUNDS TRANSFER 4410", "transfer"),
        ("FUNDS TRANSFER", "transfer"),
        ("ONLINE BANKING TRANSFER #4410 XXXX7731", "transfer"),
        ("WIRE TRANSFER OUTGOING 5512098871", "transfer"),
        # A wire names a person or a company: never a merchant key.
        ("WIRE TRANSFER TO JORDAN SAMPLE", "transfer"),
    ],
)
def test_card_payments_and_transfers_in_other_wording(raw: str, kind: str) -> None:
    cleaned = clean(raw)
    assert (cleaned.core, cleaned.kind, cleaned.via, cleaned.signals) == (
        "",
        kind,
        None,
        frozenset(),
    )


@pytest.mark.parametrize(
    ("raw", "kind", "core"),
    [
        ("ELECTRONIC PAYMENT THANKFUL HARVEST FARM", "ach_debit", "thankful harvest farm"),
        ("INTERNET PAYMENT RIVERBEND WATER", "ach_debit", "riverbend water"),
        # The reference a bank prints before "TO" leaves with the wording.
        ("ONLINE PAYMENT 5512098871 TO RIVERBEND WATER", "ach_debit", "riverbend water"),
        ("ONLINE PAYMENT TO CARDINAL HEATING", "ach_debit", "cardinal heating"),
        # An issuer's name in front of something that is not its payment line.
        ("CITI BIKE ONLINE PAYMENT", "unknown", "citi bike online payment"),
        ("CAPITAL ONE CAFE MOBILE ORDER", "unknown", "capital one cafe mobile order"),
        ("DISCOVERY ONLINE PAYMENT", "unknown", "discovery online payment"),
        # A bank word in front of TRANSFER does not make a shop a transfer.
        ("TRANSFER STATION CAFE", "unknown", "transfer station cafe"),
        ("FUNDS TRANSFER STATION CAFE", "unknown", "funds transfer station cafe"),
        ("ONLINE TRANSFER STATION 4410", "unknown", "online transfer station"),
        ("TRANSFER 4410", "unknown", "transfer"),
        ("KEEP THE CHANGE LAUNDROMAT", "unknown", "keep the change laundromat"),
        ("WIRE TRANSFERENCE RECORDS", "unknown", "wire transference records"),
        ("XFERA MOBILE TO GO", "unknown", "xfera mobile to go"),
        ("WIRE TRANSFER FEE", "fee", ""),
    ],
)
def test_not_a_card_payment_or_transfer(raw: str, kind: str, core: str) -> None:
    cleaned = clean(raw)
    assert (cleaned.kind, cleaned.core) == (kind, core)


@pytest.mark.parametrize(
    ("raw", "core"),
    [
        # A name that begins with a number keeps it behind every bank prefix...
        ("POS DEBIT 365 MARKET", "365 market"),
        ("CHECKCARD 0412 360 FITNESS", "360 fitness"),
        ("PURCHASE AUTHORIZED ON 04/12 1800 FLOWERS", "1800 flowers"),
        ("DEBIT CARD PURCHASE 5280 BURGER BAR", "5280 burger bar"),
        ("DEBIT CARD PURCHASE 800 DEGREES PIZZA", "800 degrees pizza"),
        ("POS DEBIT 365 MARKET #12 SPRINGFIELD IL", "365 market"),
        # ... as it does with none.
        ("365 MARKET", "365 market"),
        ("1800 FLOWERS", "1800 flowers"),
        ("5280 BURGER BAR", "5280 burger bar"),
        # A terminal or sequence number is what cannot be a name: it begins
        # with a zero or has six digits or more.
        ("POS DEBIT 0042 365 MARKET", "365 market"),
        ("POS DEBIT 0042 QUIET BEAN COFFEE", "quiet bean coffee"),
        ("POS DEBIT 007 QUIET BEAN COFFEE", "quiet bean coffee"),
        ("VISA DDA PUR 448120 CORNER GROCER", "corner grocer"),
        ("VISA DDA PUR 448120 365 MARKET", "365 market"),
        ("POS DEBIT 0042 MARKET", "market"),
    ],
)
def test_a_number_behind_a_bank_prefix(raw: str, core: str) -> None:
    assert clean(raw).core == core


@pytest.mark.parametrize(
    ("raw", "kind", "core", "via"),
    [
        # RETURN alone begins a shop's name; with a bank word or a number it is a refund.
        ("RETURN TO NATURE FOODS", "unknown", "return to nature foods", None),
        ("RETURN OF THE MAC REPAIRS", "unknown", "return of the mac repairs", None),
        ("POS RETURN LANTERN BOOKS", "refund", "lantern books", None),
        ("MERCHANDISE RETURN LANTERN BOOKS", "refund", "lantern books", None),
        ("RETURN CREDIT LANTERN BOOKS", "refund", "lantern books", None),
        ("RETURN 04/12 LANTERN BOOKS", "refund", "lantern books", None),
        ("RETURN", "refund", "", None),
        ("REFUND LANTERN BOOKS", "refund", "lantern books", None),
        # PAYROLL in the middle of an unlabelled line is a product, not pay.
        ("QUICKBOOKS PAYROLL 800-555-0142 CA", "unknown", "quickbooks payroll", None),
        ("ALDER PAYROLL SERVICES FEE", "unknown", "alder payroll services fee", None),
        ("MAPLE STUDIO PAYROLL", "payroll", "maple studio", None),
        ("MAPLE STUDIO PAYROLL PPD ID: 5512098871", "payroll", "maple studio", None),
        ("ACH CREDIT MAPLE STUDIO PAYROLL 5512098871", "payroll", "maple studio", None),
        ("DIRECT DEP MAPLE STUDIO PAYROLL", "payroll", "maple studio", None),
        # SP PLUS is a company; Shopify's bare prefix does not take it apart.
        ("SP PLUS PARKING", "unknown", "sp plus parking", None),
        ("SP PLUS CORP ALDERTON IL", "unknown", "sp plus", None),
        ("SP LANTERN BOOKS", "unknown", "lantern books", "shopify"),
        ("SP * PLUS SIZE OUTLET", "unknown", "plus size outlet", "shopify"),
    ],
)
def test_kind_patterns_that_matched_too_much(
    raw: str, kind: str, core: str, via: str | None
) -> None:
    cleaned = clean(raw)
    assert (cleaned.kind, cleaned.core, cleaned.via) == (kind, core, via)


@pytest.mark.parametrize(
    ("raw", "core"),
    [
        # An order number behind "*" that happens to hold no digit has no vowel either.
        ("AMZN Mktp US*VCVNJCPZQ", "amzn mktp us"),
        ("AMZN Mktp US*AB12CD34E", "amzn mktp us"),
        ("STREAMBOX *BCDFGHJK", "streambox"),
        # A word behind "*" stays, abbreviated or not.
        ("STREAMBOX *MONTHLY", "streambox monthly"),
        ("STREAMBOX *MEMBERSHIP", "streambox membership"),
        ("STREAMBOX *SUBSCR", "streambox subscr"),
        # The price: an abbreviation with no vowel goes too, on every line alike.
        ("STREAMBOX *PRMPLN", "streambox"),
    ],
)
def test_an_order_number_without_a_digit(raw: str, core: str) -> None:
    assert clean(raw).core == core


@pytest.mark.parametrize(
    ("raw", "core"),
    [
        # Two letters behind a single word are as likely the name as a state.
        ("UNCLE AL", "uncle al"),
        ("PHOTO ID", "photo id"),
        # With a number, a telephone or a web address in front, it is a state.
        ("STREAMBOX 800-555-0142 AL", "streambox"),
        ("STREAMBOX #12 ALDERTON AL", "streambox"),
        ("STREAMBOX STREAMBOX.COM AL", "streambox"),
        ("STREAMBOX.COM AL", "streambox"),
        # Behind two words it is read as a state, as before.
        ("MASSAGE ENVY CA", "massage envy"),
    ],
)
def test_a_state_behind_one_word(raw: str, core: str) -> None:
    assert clean(raw).core == core


# --- found by the quality targets (tests/unit/test_categorisation_quality.py) ---


@pytest.mark.parametrize(
    ("raw", "core", "shorter"),
    [
        # Two words in front of a state: the second is the city, or the name's.
        ("JUNEBUG ASHGROVE OH", "junebug ashgrove", "junebug"),
        ("SQ *BUTTONWOOD HARLOWTON GA", "buttonwood harlowton", "buttonwood"),
        ("MASSAGE ENVY CA", "massage envy", "massage"),
        # One word and a town of two: the town's first word was kept.
        ("BRAMBLEWOOD MAPLE RIDGE WA", "bramblewood maple", "bramblewood"),
        (
            "DEBIT CARD PURCHASE XXXXX9891 WHISTLESTOP RIO VERDANT TX",
            "whistlestop rio",
            "whistlestop",
        ),
        # A number says where the city begins: nothing is left open.
        ("JUNEBUG #12 ASHGROVE OH", "junebug", None),
        ("JUNEBUG 800-555-0142 OH", "junebug", None),
        # Two words of the name are left: nothing is left open.
        ("LANTERN BOOKS ALDERTON OH", "lantern books", None),
        ("LANTERN BOOKS MAPLE RIDGE WA", "lantern books", None),
        # Two words that are not shaped like a town: one is taken, as before.
        ("JUNEBUG CEDAR STRAND OR", "junebug cedar", None),
        # No state, or two letters that are not read as one.
        ("JUNEBUG ASHGROVE", "junebug ashgrove", None),
        ("JUNEBUG ASHGROVE CO", "junebug ashgrove", None),
        ("UNCLE AL", "uncle al", None),
        ("STARLITE DRIVE IN", "starlite drive in", None),
        ("TRANSFER TO SAVINGS", "", None),
    ],
)
def test_a_word_that_may_be_the_city_is_kept_and_offered(
    raw: str, core: str, shorter: str | None
) -> None:
    cleaned = clean(raw)
    assert (cleaned.core, cleaned.shorter) == (core, shorter)


@pytest.mark.parametrize(
    ("raw", "core"),
    [
        ("MARATHON PETRO4333 LOWER MERRIT NJ", "marathon petro"),
        ("PURCHASE 1204 H&M09086 SANTA LUMA CA 31880737734464603054910", "h m"),
        ("POS PURCHASE AMERICAN AIR001282775083", "american air"),
        ("LANTERN BOOKS0412 ALDERTON OH", "lantern books"),
        # Two digits, or a number in front, are part of a name.
        ("LANTERN STUDIO54", "lantern studio54"),
        ("FOREVER21 ALDERTON OH", "forever21 alderton"),
        ("1800LANTERNS", "1800lanterns"),
        # One letter and four digits is not a word with a number on it.
        ("LANTERN BOOKS A1234", "lantern books a1234"),
    ],
)
def test_a_number_printed_onto_a_word_goes(raw: str, core: str) -> None:
    assert clean(raw).core == core


@pytest.mark.parametrize(
    ("raw", "core"),
    [
        ("PARAMOUNT+ 866-555-0127 CA", "paramount plus"),
        ("PURCHASE 0516 PARAMOUNT+ 866-555-0127 CA 28781754327697587437783", "paramount plus"),
        ("STREAMBOX+", "streambox plus"),
        ("STREAMBOX+ ANNUAL", "streambox plus annual"),
        # A sign between two names, or in front of one, is punctuation.
        ("LANTERN + MAPLE", "lantern maple"),
        ("LANTERN+MAPLE", "lantern maple"),
        ("+LANTERN", "lantern"),
    ],
)
def test_a_plus_that_ends_a_name_is_read(raw: str, core: str) -> None:
    assert clean(raw).core == core


@pytest.mark.parametrize(
    ("raw", "kind", "core"),
    [
        ("NAVIENT STDNT LOAN WEB ID: 3829316688", "loan_payment", "navient"),
        ("ACH DEBIT NELNET STUDNTLOAN 719702316680", "loan_payment", "nelnet"),
        ("NELNET DES:STUDNTLOAN ID:M1KHGRE5ZV CO ID:2954119753 PPD", "loan_payment", "nelnet"),
        ("LANTERN SERVICING STUDENT LN PPD ID: 4410772913", "loan_payment", "lantern servicing"),
        # "payment" behind the wording is the entry's, not the servicer's name.
        ("ACH DEBIT MOHELA STUDENT LOAN PMT", "loan_payment", "mohela"),
        ("ACH DEBIT MOHELA STDNT LOAN PAYMENT 7741", "loan_payment", "mohela"),
        # A shop is not a lender for a word that begins like one.
        ("STUDENT LOANERS BIKE HIRE", "unknown", "student loaners bike hire"),
        # A card purchase is a purchase, whatever the shop calls itself.
        ("POS DEBIT STUDENT LOAN CAFE", "purchase", "student loan cafe"),
    ],
)
def test_a_student_loan_is_a_loan_payment(raw: str, kind: str, core: str) -> None:
    cleaned = clean(raw)
    assert (cleaned.kind, cleaned.core) == (kind, core)

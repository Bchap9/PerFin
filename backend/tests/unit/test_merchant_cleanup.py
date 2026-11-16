# SPDX-License-Identifier: AGPL-3.0-or-later
"""Payee clean-up, first half: canonical text, the kind, labelled ACH, trailers.

Every descriptor here is invented in the shapes banks print. The core still
holds processor prefixes, places and store numbers at this point; what is
pinned is the kind, the signals and what must already be gone (the bank's
own wrapping, the individual's name in an ACH line, references and dates).
"""

from __future__ import annotations

import re
import time

import pytest

from app.domain.categorisation import cleanup
from app.domain.categorisation.cleanup import Cleaned, canonical, clean
from app.domain.transactions.models import PAYEE_MAX

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
    assert cleaned.core == "streambox com"
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
            "chase credit crd",
        ),
        (
            "CAPITAL ONE DES:CRCARDPMT ID:A1B2C3D4E5F6 INDN:SAMPLE,JORDAN CO ID:1020304050 WEB",
            "card_payment",
            "capital one",
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

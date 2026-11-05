# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic structured statements (CSV, TSV, OFX SGML/XML, QFX, QIF) for tests.

Everything is generated from a seeded ``random.Random``: payees like
``Payee-3f9a``, account numbers of random digits, amounts and dates chosen
by the generator. No real institution, person or number appears.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal


@dataclass(frozen=True)
class SynthTxn:
    posted: date
    amount: Decimal
    payee: str
    memo: str
    fitid: str


def synth_transactions(
    count: int = 5, *, seed: int = 7, start: date | None = None
) -> list[SynthTxn]:
    rng = random.Random(seed)
    day = start or date(2026, 1, 3)
    out = []
    for index in range(count):
        cents = rng.randint(-50_000, 30_000) or 1
        out.append(
            SynthTxn(
                posted=day + timedelta(days=index * 2),
                amount=Decimal(cents) / 100,
                payee=f"Payee-{rng.getrandbits(16):04x}",
                memo=f"Memo {index}",
                fitid=f"FIT{seed}{index:05d}",
            )
        )
    return out


def account_number(seed: int = 7) -> str:
    rng = random.Random(seed * 31 + 1)
    return "".join(str(rng.randint(0, 9)) for _ in range(10))


def csv_bytes(
    txns: list[SynthTxn], *, delimiter: str = ",", encoding: str = "utf-8", bom: bool = False
) -> bytes:
    lines = [delimiter.join(("Date", "Amount", "Description", "Memo"))]
    for t in txns:
        lines.append(delimiter.join((t.posted.isoformat(), str(t.amount), t.payee, t.memo)))
    text = "\r\n".join(lines) + "\r\n"
    data = text.encode(encoding)
    return (b"\xef\xbb\xbf" + data) if bom else data


def _ofx_date(day: date) -> str:
    return day.strftime("%Y%m%d") + "120000[-5:EST]"


def _stmttrn_sgml(t: SynthTxn) -> str:
    kind = "CREDIT" if t.amount > 0 else "DEBIT"
    return (
        "<STMTTRN>\n"
        f"<TRNTYPE>{kind}\n<DTPOSTED>{_ofx_date(t.posted)}\n<TRNAMT>{t.amount}\n"
        f"<FITID>{t.fitid}\n<NAME>{t.payee}\n<MEMO>{t.memo}\n"
        "</STMTTRN>\n"
    )


def ofx_sgml(
    txns: list[SynthTxn], *, acct: str | None = None, qfx: bool = False, currency: str = "USD"
) -> bytes:
    acct = acct or account_number()
    start, end = txns[0].posted, txns[-1].posted
    intu = "<INTU.BID>00000\n" if qfx else ""
    body = (
        "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\n"
        "CHARSET:1252\nCOMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n"
        "<OFX>\n<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>"
        f"<DTSERVER>20260201<LANGUAGE>ENG{intu}</SONRS></SIGNONMSGSRSV1>\n"
        "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STMTRS>\n"
        f"<CURDEF>{currency}\n<BANKACCTFROM><BANKID>000000000<ACCTID>{acct}"
        "<ACCTTYPE>CHECKING</BANKACCTFROM>\n"
        f"<BANKTRANLIST><DTSTART>{_ofx_date(start)}<DTEND>{_ofx_date(end)}\n"
        + "".join(_stmttrn_sgml(t) for t in txns)
        + "</BANKTRANLIST>\n"
        f"<LEDGERBAL><BALAMT>1234.56<DTASOF>{_ofx_date(end)}</LEDGERBAL>\n"
        "</STMTRS></STMTTRNRS></BANKMSGSRSV1>\n</OFX>\n"
    )
    return body.encode("ascii")


def ofx_xml(txns: list[SynthTxn], *, acct: str | None = None, prolog: str = "") -> bytes:
    acct = acct or account_number()
    start, end = txns[0].posted, txns[-1].posted
    trns = "".join(
        "<STMTTRN>"
        f"<TRNTYPE>{'CREDIT' if t.amount > 0 else 'DEBIT'}</TRNTYPE>"
        f"<DTPOSTED>{_ofx_date(t.posted)}</DTPOSTED><TRNAMT>{t.amount}</TRNAMT>"
        f"<FITID>{t.fitid}</FITID><NAME>{t.payee}</NAME><MEMO>{t.memo}</MEMO>"
        "</STMTTRN>"
        for t in txns
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" '
        'NEWFILEUID="NONE"?>\n'
        f"{prolog}"
        "<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><TRNUID>1</TRNUID><CCSTMTRS>"
        f"<CURDEF>EUR</CURDEF><CCACCTFROM><ACCTID>{acct}</ACCTID></CCACCTFROM>"
        f"<BANKTRANLIST><DTSTART>{_ofx_date(start)}</DTSTART><DTEND>{_ofx_date(end)}</DTEND>"
        f"{trns}</BANKTRANLIST>"
        f"<LEDGERBAL><BALAMT>-200.10</BALAMT><DTASOF>{_ofx_date(end)}</DTASOF></LEDGERBAL>"
        "</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>\n"
    )
    return body.encode("utf-8")


def qif_bytes(txns: list[SynthTxn], *, date_style: str = "mdy", kind: str = "Bank") -> bytes:
    lines = ["!Account", "NAccount-synthetic", "TBank", "^", f"!Type:{kind}"]
    for t in txns:
        d = t.posted
        if date_style == "mdy":
            ds = f"{d.month}/{d.day}'{d.year % 100:02d}"
        elif date_style == "dmy":
            ds = f"{d.day:02d}/{d.month:02d}/{d.year}"
        else:
            ds = d.isoformat()
        lines += [f"D{ds}", f"T{t.amount:,}", f"P{t.payee}", f"M{t.memo}", "C*", "LGroceries", "^"]
    return ("\n".join(lines) + "\n").encode("utf-8")


XXE_PROLOG = '<!DOCTYPE OFX [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
BILLION_LAUGHS_PROLOG = (
    "<!DOCTYPE lolz [\n"
    '<!ENTITY lol "lol">\n'
    '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
    '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">\n'
    '<!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">\n'
    "]>\n"
)

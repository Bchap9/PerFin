# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic bank statements for import tests and the dev seed.

Everything here is invented and generated from a seed: the institutions
(``Acme Synthetic Bank``...), the merchants (``Blue Owl Grocers``...), the
account numbers (random digits) and every amount and date. No real
institution, person, account or transaction appears, and nothing is read
from outside this module (standard library only, so ``scripts/synth/
statements.py`` can run it without the backend environment).

* :func:`synth_ledger` makes a long, deterministic history for one account
  (salary, rent, card spend, a transfer to savings, two identical coffees on
  one day so the import hash's running index is exercised).
* :func:`statement` cuts a window out of it with its opening and closing
  balances, so two overlapping windows share transactions exactly as two
  overlapping bank exports do.
* Writers: :func:`csv_bytes` for the layouts in :data:`LAYOUTS` (signed
  amount, debit/credit columns, semicolon and decimal comma, purchases
  positive, DR/CR indicator, a preamble before the header, TSV), each with the
  column mapping (:attr:`Layout.spec`, ``MappingSpec`` JSON) that reads it;
  :func:`ofx_sgml_bytes` (OFX 1.x, optionally QFX), :func:`ofx_xml_bytes`
  (OFX 2.x), :func:`qif_bytes` (with categories and a split).
* :func:`main` is the command line (``python -m tests.fixtures.synth_statements
  --out DIR`` or ``scripts/synth/statements.py``): one file per format and
  layout, plus a ``manifest.json`` with each file's account number, layout and
  mapping.
"""

from __future__ import annotations

import argparse
import json
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

# ---------------------------------------------------------------------------
# Invented institutions and merchants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeInstitution:
    key: str
    name: str
    fid: str


INSTITUTIONS: Final = (
    FakeInstitution("acme", "Acme Synthetic Bank", "9001"),
    FakeInstitution("ledgerly", "Ledgerly Savings Cooperative", "9002"),
    FakeInstitution("paperclip", "Paperclip Card Services", "9003"),
)

_ADJECTIVES: Final = ("Blue Owl", "Corner", "Maple", "Quartz", "Lantern", "Harbor", "Juniper")
_NOUNS: Final = ("Grocers", "Fuel", "Bakery", "Books", "Pharmacy", "Hardware", "Cafe")
CENT: Final = Decimal("0.01")


@dataclass(frozen=True)
class SynthTxn:
    posted: date
    amount: Decimal
    payee: str
    memo: str
    fitid: str
    category: str | None = None


@dataclass(frozen=True)
class SynthStatement:
    institution: FakeInstitution
    account_number: str
    account_type: str  # "checking", "savings" or "creditcard"
    currency: str
    start: date
    end: date
    opening_balance: Decimal
    transactions: tuple[SynthTxn, ...]

    @property
    def closing_balance(self) -> Decimal:
        return self.opening_balance + sum((t.amount for t in self.transactions), Decimal(0))

    @property
    def last4(self) -> str:
        return self.account_number[-4:]

    def running(self) -> list[tuple[SynthTxn, Decimal]]:
        """Each transaction with the balance after it, oldest first."""
        balance = self.opening_balance
        out: list[tuple[SynthTxn, Decimal]] = []
        for t in self.transactions:
            balance += t.amount
            out.append((t, balance))
        return out


def account_number(seed: int) -> str:
    """Ten random digits (never a real account)."""
    rng = random.Random(f"acct-{seed}")  # nosec B311 - test data, not security
    return "".join(str(rng.randint(0, 9)) for _ in range(10))


def _merchant(rng: random.Random) -> str:
    return f"{rng.choice(_ADJECTIVES)} {rng.choice(_NOUNS)}"


def _money(rng: random.Random, low: int, high: int) -> Decimal:
    return Decimal(rng.randint(low, high)) * CENT


def synth_ledger(
    seed: int = 1,
    *,
    start: date = date(2026, 1, 1),
    days: int = 90,
    credit_card: bool = False,
) -> list[SynthTxn]:
    """A deterministic history, oldest first (module doc)."""
    rng = random.Random(f"ledger-{seed}")  # nosec B311 - test data, not security
    out: list[SynthTxn] = []
    counter = 0

    def add(day: date, amount: Decimal, payee: str, memo: str, category: str | None) -> None:
        nonlocal counter
        counter += 1
        out.append(SynthTxn(day, amount, payee, memo, f"S{seed:03d}{counter:06d}", category))

    for offset in range(days):
        day = start + timedelta(days=offset)
        if credit_card:
            if rng.random() < 0.6:
                add(day, -_money(rng, 300, 9000), _merchant(rng), "Card purchase", "Shopping")
            if day.day == 20:
                add(day, _money(rng, 20000, 60000), "Payment Thank You", "Autopay", None)
            continue
        if day.day == 1:
            add(day, -Decimal("1450.00"), "Oakfield Rentals", "Rent", "Housing:Rent")
        if day.day == 15:
            add(day, Decimal("3210.55"), "Initech Payroll", "Salary", "Income:Salary")
        if day.day == 16:
            add(day, -Decimal("250.00"), "Transfer to savings", "Internal transfer", None)
        if day.day == 10:
            # Two identical purchases on one day (the import hash's running index).
            add(day, -Decimal("4.50"), "Quiet Bean Coffee", "Coffee", "Food:Coffee")
            add(day, -Decimal("4.50"), "Quiet Bean Coffee", "Coffee", "Food:Coffee")
        if rng.random() < 0.5:
            add(day, -_money(rng, 199, 12000), _merchant(rng), "Card purchase", "Food:Groceries")
    return out


def statement(
    ledger: Sequence[SynthTxn],
    start: date,
    end: date,
    *,
    seed: int = 1,
    institution: FakeInstitution = INSTITUTIONS[0],
    account_type: str = "checking",
    currency: str = "USD",
    base_balance: Decimal = Decimal("5000.00"),
) -> SynthStatement:
    """The window ``start..end`` of ``ledger`` with its opening balance."""
    before = sum((t.amount for t in ledger if t.posted < start), Decimal(0))
    return SynthStatement(
        institution=institution,
        account_number=account_number(seed),
        account_type=account_type,
        currency=currency,
        start=start,
        end=end,
        opening_balance=base_balance + before,
        transactions=tuple(t for t in ledger if start <= t.posted <= end),
    )


def sample_statement(seed: int = 1, *, days: int = 30, credit_card: bool = False) -> SynthStatement:
    """A one-month statement of a fresh synthetic account."""
    start = date(2026, 1, 1)
    ledger = synth_ledger(seed, start=start, days=days, credit_card=credit_card)
    return statement(
        ledger,
        start,
        start + timedelta(days=days - 1),
        seed=seed,
        institution=INSTITUTIONS[2] if credit_card else INSTITUTIONS[0],
        account_type="creditcard" if credit_card else "checking",
        base_balance=Decimal(0) if credit_card else Decimal("5000.00"),
    )


# ---------------------------------------------------------------------------
# CSV / TSV layouts
# ---------------------------------------------------------------------------


def _quote(cell: str, delimiter: str) -> str:
    if any(ch in cell for ch in (delimiter, '"', "\n", "\r")):
        return '"' + cell.replace('"', '""') + '"'
    return cell


def _comma(amount: Decimal) -> str:
    return f"{amount:.2f}".replace(".", ",")


@dataclass(frozen=True)
class Layout:
    """A bank-like delimited export and the mapping that reads it."""

    name: str
    delimiter: str
    header: tuple[str, ...]
    row: Callable[[SynthTxn, Decimal, SynthStatement], tuple[str, ...]]
    spec: dict[str, Any]
    newest_first: bool = False
    preamble: tuple[tuple[str, ...], ...] = ()
    currency: str = "USD"
    kind: str = "csv"
    extra: dict[str, Any] = field(default_factory=dict)


LAYOUTS: Final[dict[str, Layout]] = {
    layout.name: layout
    for layout in (
        Layout(
            name="us_signed",
            delimiter=",",
            header=("Date", "Description", "Amount", "Balance"),
            row=lambda t, bal, _s: (
                t.posted.strftime("%m/%d/%Y"),
                t.payee,
                f"{t.amount:.2f}",
                f"{bal:.2f}",
            ),
            spec={
                "date_column": 0,
                "date_format": "MM/DD/YYYY",
                "payee_column": 1,
                "amount_column": 2,
                "balance_column": 3,
            },
        ),
        Layout(
            name="debit_credit",
            delimiter=",",
            header=("Posting Date", "Details", "Withdrawals", "Deposits", "Balance", "Reference"),
            row=lambda t, bal, _s: (
                t.posted.isoformat(),
                t.payee,
                f"{-t.amount:,.2f}" if t.amount < 0 else "",
                f"{t.amount:,.2f}" if t.amount > 0 else "",
                f"{bal:,.2f}",
                t.memo,
            ),
            spec={
                "date_column": 0,
                "date_format": "YYYY-MM-DD",
                "payee_column": 1,
                "debit_column": 2,
                "credit_column": 3,
                "balance_column": 4,
                "memo_column": 5,
            },
            newest_first=True,
        ),
        Layout(
            name="eu_semicolon",
            delimiter=";",
            header=("Booking date", "Counterparty", "Purpose", "Amount", "Currency"),
            row=lambda t, _bal, s: (
                t.posted.strftime("%d.%m.%Y"),
                t.payee,
                t.memo,
                _comma(t.amount),
                s.currency,
            ),
            spec={
                "date_column": 0,
                "date_format": "DD.MM.YYYY",
                "payee_column": 1,
                "memo_column": 2,
                "amount_column": 3,
                "decimal_separator": ",",
                "currency_column": 4,
            },
            currency="EUR",
        ),
        Layout(
            name="card_positive_purchases",
            delimiter=",",
            header=("Trans Date", "Post Date", "Description", "Category", "Amount"),
            row=lambda t, _bal, _s: (
                (t.posted - timedelta(days=1)).strftime("%m/%d/%Y"),
                t.posted.strftime("%m/%d/%Y"),
                t.payee,
                t.category or "",
                f"{-t.amount:.2f}",
            ),
            spec={
                "date_column": 1,
                "date_format": "MM/DD/YYYY",
                "payee_column": 2,
                "memo_column": 3,
                "amount_column": 4,
                "sign": "outflow_positive",
            },
        ),
        Layout(
            name="indicator",
            delimiter=",",
            header=("Date", "Narrative", "Value", "DR/CR", "Balance"),
            row=lambda t, bal, _s: (
                t.posted.strftime("%d/%m/%Y"),
                t.payee,
                f"{abs(t.amount):.2f}",
                "DR" if t.amount < 0 else "CR",
                f"{bal:.2f}",
            ),
            spec={
                "date_column": 0,
                "date_format": "DD/MM/YYYY",
                "payee_column": 1,
                "amount_column": 2,
                "indicator_column": 3,
                "outflow_values": ["DR"],
                "balance_column": 4,
            },
            currency="GBP",
        ),
        Layout(
            name="preamble",
            delimiter=",",
            header=("Date", "Payee", "Memo", "Amount"),
            row=lambda t, _bal, _s: (t.posted.isoformat(), t.payee, t.memo, f"{t.amount:.2f}"),
            spec={
                "skip_rows": 2,
                "date_column": 0,
                "date_format": "YYYY-MM-DD",
                "payee_column": 1,
                "memo_column": 2,
                "amount_column": 3,
            },
            preamble=(("Synthetic account export", "2026"), ("Account", "masked")),
        ),
        Layout(
            name="tsv",
            delimiter="\t",
            header=("Date", "Description", "Amount"),
            row=lambda t, _bal, _s: (t.posted.isoformat(), t.payee, f"{t.amount:.2f}"),
            spec={
                "date_column": 0,
                "date_format": "YYYY-MM-DD",
                "payee_column": 1,
                "amount_column": 2,
            },
            kind="tsv",
        ),
    )
}


def csv_bytes(stmt: SynthStatement, layout: str | Layout = "us_signed") -> bytes:
    """``stmt`` as the delimited export ``layout`` (CRLF line ends, UTF-8)."""
    spec = LAYOUTS[layout] if isinstance(layout, str) else layout
    rows = [spec.row(t, balance, stmt) for t, balance in stmt.running()]
    if spec.newest_first:
        rows.reverse()
    lines = [
        spec.delimiter.join(_quote(c, spec.delimiter) for c in row)
        for row in (*spec.preamble, spec.header, *rows)
    ]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def layout_statement(stmt: SynthStatement, layout: str) -> SynthStatement:
    """``stmt`` in the currency the layout's bank uses."""
    return replace(stmt, currency=LAYOUTS[layout].currency)


# ---------------------------------------------------------------------------
# OFX / QFX
# ---------------------------------------------------------------------------


def _ofx_date(day: date) -> str:
    return day.strftime("%Y%m%d") + "120000[-5:EST]"


def _trntype(t: SynthTxn) -> str:
    return "CREDIT" if t.amount > 0 else "DEBIT"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def ofx_sgml_bytes(stmt: SynthStatement, *, qfx: bool = False) -> bytes:
    """OFX 1.x (SGML, optional end tags); ``qfx`` adds the Intuit ``INTU.BID``."""
    card = stmt.account_type == "creditcard"
    intu = f"<INTU.BID>{stmt.institution.fid}\n" if qfx else ""
    trns = "".join(
        "<STMTTRN>\n"
        f"<TRNTYPE>{_trntype(t)}\n<DTPOSTED>{_ofx_date(t.posted)}\n<TRNAMT>{t.amount:.2f}\n"
        f"<FITID>{t.fitid}\n<NAME>{_escape(t.payee)}\n<MEMO>{_escape(t.memo)}\n"
        "</STMTTRN>\n"
        for t in stmt.transactions
    )
    if card:
        acct = f"<CCACCTFROM><ACCTID>{stmt.account_number}</CCACCTFROM>\n"
        open_tags, close_tags = (
            "<CREDITCARDMSGSRSV1><CCSTMTTRNRS><TRNUID>1<CCSTMTRS>\n",
            "</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1>\n",
        )
    else:
        acct = (
            f"<BANKACCTFROM><BANKID>000000000<ACCTID>{stmt.account_number}"
            f"<ACCTTYPE>{stmt.account_type.upper()}</BANKACCTFROM>\n"
        )
        open_tags, close_tags = (
            "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STMTRS>\n",
            "</STMTRS></STMTTRNRS></BANKMSGSRSV1>\n",
        )
    body = (
        "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\n"
        "CHARSET:1252\nCOMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n"
        "<OFX>\n<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>\n"
        f"<DTSERVER>{_ofx_date(stmt.end)}<LANGUAGE>ENG\n"
        f"<FI><ORG>{_escape(stmt.institution.name)}<FID>{stmt.institution.fid}</FI>\n"
        f"{intu}</SONRS></SIGNONMSGSRSV1>\n"
        f"{open_tags}<CURDEF>{stmt.currency}\n{acct}"
        f"<BANKTRANLIST><DTSTART>{_ofx_date(stmt.start)}<DTEND>{_ofx_date(stmt.end)}\n"
        f"{trns}</BANKTRANLIST>\n"
        f"<LEDGERBAL><BALAMT>{stmt.closing_balance:.2f}<DTASOF>{_ofx_date(stmt.end)}</LEDGERBAL>\n"
        f"{close_tags}</OFX>\n"
    )
    return body.encode("ascii")


def ofx_xml_bytes(stmt: SynthStatement) -> bytes:
    """OFX 2.x (XML, every element closed)."""
    card = stmt.account_type == "creditcard"
    trns = "".join(
        "<STMTTRN>"
        f"<TRNTYPE>{_trntype(t)}</TRNTYPE><DTPOSTED>{_ofx_date(t.posted)}</DTPOSTED>"
        f"<TRNAMT>{t.amount:.2f}</TRNAMT><FITID>{t.fitid}</FITID>"
        f"<NAME>{_escape(t.payee)}</NAME><MEMO>{_escape(t.memo)}</MEMO>"
        "</STMTTRN>"
        for t in stmt.transactions
    )
    if card:
        wrap = ("CREDITCARDMSGSRSV1", "CCSTMTTRNRS", "CCSTMTRS")
        acct = f"<CCACCTFROM><ACCTID>{stmt.account_number}</ACCTID></CCACCTFROM>"
    else:
        wrap = ("BANKMSGSRSV1", "STMTTRNRS", "STMTRS")
        acct = (
            f"<BANKACCTFROM><BANKID>000000000</BANKID><ACCTID>{stmt.account_number}</ACCTID>"
            f"<ACCTTYPE>{stmt.account_type.upper()}</ACCTTYPE></BANKACCTFROM>"
        )
    body = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" '
        'NEWFILEUID="NONE"?>\n'
        "<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>"
        f"<DTSERVER>{_ofx_date(stmt.end)}</DTSERVER><LANGUAGE>ENG</LANGUAGE>"
        f"<FI><ORG>{_escape(stmt.institution.name)}</ORG><FID>{stmt.institution.fid}</FID></FI>"
        "</SONRS></SIGNONMSGSRSV1>"
        f"<{wrap[0]}><{wrap[1]}><TRNUID>1</TRNUID><{wrap[2]}>"
        f"<CURDEF>{stmt.currency}</CURDEF>{acct}"
        f"<BANKTRANLIST><DTSTART>{_ofx_date(stmt.start)}</DTSTART>"
        f"<DTEND>{_ofx_date(stmt.end)}</DTEND>{trns}</BANKTRANLIST>"
        f"<LEDGERBAL><BALAMT>{stmt.closing_balance:.2f}</BALAMT>"
        f"<DTASOF>{_ofx_date(stmt.end)}</DTASOF></LEDGERBAL>"
        f"</{wrap[2]}></{wrap[1]}></{wrap[0]}></OFX>\n"
    )
    return body.encode("utf-8")


# ---------------------------------------------------------------------------
# QIF
# ---------------------------------------------------------------------------


def qif_bytes(stmt: SynthStatement, *, split_first_rent: bool = True) -> bytes:
    """QIF (``!Type:Bank`` or ``CCard``), US dates with ``'`` years, categories.

    The first rent payment is written as a split (rent plus a fee) when
    ``split_first_rent``; every other row carries its single category.
    """
    kind = "CCard" if stmt.account_type == "creditcard" else "Bank"
    lines = ["!Account", f"N{stmt.institution.name} {stmt.account_type}", f"T{kind}", "^"]
    lines.append(f"!Type:{kind}")
    split_done = not split_first_rent
    for t in stmt.transactions:
        d = t.posted
        lines += [f"D{d.month}/{d.day}'{d.year % 100:02d}", f"T{t.amount:,.2f}", f"P{t.payee}"]
        lines.append(f"M{t.memo}")
        if not split_done and t.category == "Housing:Rent":
            fee = Decimal("-50.00")
            lines += [
                "LHousing:Rent",
                "SHousing:Rent",
                f"${t.amount - fee:.2f}",
                "SBank Fees",
                "EService fee",
                f"${fee:.2f}",
            ]
            split_done = True
        elif t.category:
            lines.append(f"L{t.category}")
        lines += ["C*", "^"]
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

FORMATS: Final = ("csv", "ofx", "ofx-xml", "qfx", "qif")


def write_samples(out: Path, *, seed: int = 1, days: int = 30) -> list[dict[str, Any]]:
    """One file per format and CSV layout under ``out``; returns the manifest."""
    out.mkdir(parents=True, exist_ok=True)
    checking = sample_statement(seed, days=days)
    card = sample_statement(seed + 1, days=days, credit_card=True)
    manifest: list[dict[str, Any]] = []

    def put(name: str, data: bytes, stmt: SynthStatement, **facts: Any) -> None:
        (out / name).write_bytes(data)
        manifest.append(
            {
                "file": name,
                "institution": stmt.institution.name,
                "account_number": stmt.account_number,
                "account_type": stmt.account_type,
                "currency": stmt.currency,
                "transactions": len(stmt.transactions),
                "closing_balance": f"{stmt.closing_balance:.2f}",
                **facts,
            }
        )

    for name, layout in LAYOUTS.items():
        stmt = layout_statement(checking, name)
        put(f"{name}.{layout.kind}", csv_bytes(stmt, layout), stmt, layout=name, spec=layout.spec)
    put("checking.ofx", ofx_sgml_bytes(checking), checking, format="ofx-sgml")
    put("checking-v2.ofx", ofx_xml_bytes(checking), checking, format="ofx-xml")
    put("card.qfx", ofx_sgml_bytes(card, qfx=True), card, format="qfx")
    put("card-v2.ofx", ofx_xml_bytes(card), card, format="ofx-xml")
    put("checking.qif", qif_bytes(checking), checking, format="qif")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write synthetic (invented) bank statements in every supported format."
    )
    parser.add_argument("--out", type=Path, required=True, help="directory to write into")
    parser.add_argument("--seed", type=int, default=1, help="random seed (default 1)")
    parser.add_argument("--days", type=int, default=30, help="days per statement (default 30)")
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 3660:
        parser.error("--days must be between 1 and 3660")
    manifest = write_samples(args.out, seed=args.seed, days=args.days)
    for entry in manifest:
        print(f"{entry['file']}: {entry['transactions']} transactions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

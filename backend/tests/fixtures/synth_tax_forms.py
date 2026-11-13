# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic US tax forms drawn as PDFs (reportlab) for the tax form parser.

Every form of the ``US`` form set (``app.domain.tax.forms.us``) can be drawn
in an IRS-like layout: a heading (``Form W-2``, the title, the year), an
identity block (payer/employer, TINs, recipient name and address, account,
control and claim numbers, state ids) and a grid of numbered boxes, each with its
label on top and its amount below. Special parts:

* W-2: box 12a-d (code and amount) and box 14 "Other" (labelled lines,
  summed into ``14t``);
* 1098: box 10 "Other" with a property tax line (``10t``);
* 1095-A: the Part III monthly table (lines 21-33, columns A/B/C);
* 1095-B / 1095-C: the covered-individuals table (``X`` per covered month:
  ``cov_<MON>`` or ``cov_ALL``); 1095-C line 15 (``15_ALL`` or per month).

**Identity data is fake and obvious** (:data:`FAKE_SSN` ``000-00-0000``,
:data:`FAKE_EIN` ``00-0000000``, an invented name and address, account,
control, policy, claim and state id numbers): :data:`IDENTITY_TOKENS` lists the
strings tests look for to prove none of it is stored or served. The issuer
(payer/employer/lender) **name** is not identity data: it is kept as
``tax_form_data.issuer_name``. An SSA-1099 has no issuer box (its numbered
boxes 1, 2, 7 and 8 are the beneficiary's name, number, address and claim
number), so its issuer is ``None``.

:func:`synth_form` builds a form with seeded amounts; :func:`expected_boxes`
is what a parser must return for it (stored box keys, ``Decimal``);
:func:`form_pdf` / :func:`forms_pdf` draw one or several forms (one per
page, as in a consolidated 1099).
"""

from __future__ import annotations

import io
import random
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Final

from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

from app.domain.tax.forms import BoxDef, BoxKind, FormDef, form_def

FONT: Final = "Helvetica"
BOLD: Final = "Helvetica-Bold"

FAKE_SSN: Final = "000-00-0000"
FAKE_EIN: Final = "00-0000000"
RECIPIENT: Final = "Jordan Quill Testperson"
RECIPIENT_STREET: Final = "742 Quuxwood Lane"
RECIPIENT_CITY: Final = "Nowhereville ZZ 00000"
ISSUER_STREET: Final = "1 Makebelieve Plaza"
ISSUER_CITY: Final = "Faketown ZZ 00000"
PHONE: Final = "(000) 555-0100"
ACCOUNT: Final = "ACCT0000111122"
CONTROL: Final = "CTRL0000099"
STATE_ID: Final = "ZZ STATEID0000"
POLICY: Final = "POLICY0000777"
CLAIM: Final = "CLAIM0000444"
BIRTH: Final = "01/01/1900"
RTN: Final = "000000000"

#: Strings that must never be found in the database or an API response
#: (lower case; the search index lower-cases too).
IDENTITY_TOKENS: Final = (
    FAKE_SSN,
    FAKE_EIN,
    "testperson",
    "quill",  # no other fixture may print it: the scan reads the shared test database
    "quuxwood",  # unique: a token shared with another fixture made the scan order-dependent
    "nowhereville",
    "makebelieve",
    "faketown",
    "555-0100",
    "0000111122",
    "ctrl0000099",
    "stateid0000",
    "policy0000777",
    "claim0000444",
    "01/01/1900",
)


def identity_tokens_in(text: str) -> list[str]:
    """The :data:`IDENTITY_TOKENS` found in ``text`` (compared in lower case).

    The name and address tokens are letters only, and the fixture prints them
    as words, so they are matched as whole words: a database scan also reads
    password hashes, salts and other random base32/base64 strings, and a short
    word such as "quill" turns up inside one of those now and then. The number
    tokens are matched anywhere, because the forms print them glued to a
    prefix ("ACCT0000111122").
    """
    low = text.lower()
    found = []
    for token in IDENTITY_TOKENS:
        if token.isalpha():
            hit = re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", low)
        else:
            hit = re.search(re.escape(token), low)
        if hit:
            found.append(token)
    return found


#: A fictional issuer per form type (kept as the issuer name). ``None``: the
#: form has no issuer box (an SSA-1099 has one payer, the administration).
ISSUERS: Final[dict[str, str | None]] = {
    "W-2": "Fictional Widgets Inc",
    "1099-INT": "Sample National Bank",
    "1099-DIV": "Placeholder Brokerage",
    "1099-B": "Placeholder Brokerage",
    "1099-MISC": "Demo Rentals LLC",
    "1099-NEC": "Example Studio LLC",
    "1099-R": "Mock Retirement Trust",
    "1099-G": "Department of Example Revenue",
    "1099-SA": "Pretend HSA Bank",
    "SSA-1099": None,
    "1098": "Placeholder Home Lending",
    "1098-E": "Sample Student Loans",
    "1098-T": "University of Nowhere Real",
    "1095-A": "Example Health Marketplace",
    "1095-B": "Demo Health Insurance Co",
    "1095-C": "Fictional Widgets Inc",
}

MONTHS: Final = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_MONTH_NAMES: Final = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

#: Boxes given a seeded amount per form (the others are drawn empty).
KEY_BOXES: Final[dict[str, tuple[str, ...]]] = {
    "W-2": ("1", "2", "3", "4", "5", "6", "16", "17"),
    "1099-INT": ("1", "3", "4", "8"),
    "1099-DIV": ("1a", "1b", "2a", "4", "7"),
    "1099-B": ("1d", "1e", "1g", "4"),
    "1099-MISC": ("1", "2", "3", "4"),
    "1099-NEC": ("1", "4"),
    "1099-R": ("1", "2a", "4"),
    "1099-G": ("1", "2", "4"),
    "1099-SA": ("1",),
    "SSA-1099": ("3", "5", "6"),
    "1098": ("1", "2", "5", "6"),
    "1098-E": ("1",),
    "1098-T": ("1", "5"),
    "1095-A": (),
    "1095-B": (),
    "1095-C": (),
}
FORM_TYPES: Final = tuple(KEY_BOXES)


@dataclass(frozen=True)
class SynthTaxForm:
    form_type: str
    tax_year: int
    #: None when the form has no issuer box (:data:`ISSUERS`).
    issuer: str | None
    #: Plain amount boxes (box id -> amount).
    amounts: dict[str, Decimal] = field(default_factory=dict)
    #: W-2 box 12 (code, amount), at most four.
    box12: tuple[tuple[str, Decimal], ...] = ()
    #: W-2 box 14 / 1098 box 10 "Other" lines (label, amount).
    other: tuple[tuple[str, Decimal], ...] = ()
    #: 1095-A Part III: month index (1-12) -> (A, B, C).
    monthly: dict[int, tuple[Decimal, Decimal, Decimal]] = field(default_factory=dict)
    #: 1095-B/C: covered months (``MONTHS`` codes) or ``("ALL",)``.
    coverage: tuple[str, ...] = ()
    #: 1095-C line 15: ``"ALL"`` or a month code -> amount.
    line15: dict[str, Decimal] = field(default_factory=dict)


def _money(rng: random.Random, low: int, high: int) -> Decimal:
    return Decimal(rng.randint(low * 100, high * 100)).scaleb(-2)


def synth_form(
    form_type: str, *, seed: int = 0, tax_year: int = 2025, issuer: str | None = None
) -> SynthTaxForm:
    """A form of ``form_type`` with seeded amounts in its key boxes."""
    rng = random.Random(f"{form_type}:{seed}")
    amounts = {box: _money(rng, 10, 90_000) for box in KEY_BOXES[form_type]}
    form = SynthTaxForm(
        form_type=form_type,
        tax_year=tax_year,
        issuer=issuer or ISSUERS[form_type],
        amounts=amounts,
    )
    if form_type == "W-2":
        form = replace(
            form,
            box12=(("D", _money(rng, 500, 9_000)), ("DD", _money(rng, 1_000, 12_000))),
            other=(("SDI", _money(rng, 50, 900)), ("UNION", _money(rng, 100, 600))),
        )
    elif form_type == "1098":
        form = replace(form, other=(("Property tax", _money(rng, 1_000, 9_000)),))
    elif form_type == "1095-A":
        months = rng.randint(6, 12)
        form = replace(
            form,
            monthly={
                m: (_money(rng, 300, 900), _money(rng, 300, 900), _money(rng, 50, 300))
                for m in range(1, months + 1)
            },
        )
    elif form_type == "1095-B":
        start = rng.randint(0, 5)
        form = replace(form, coverage=MONTHS[start:])
    elif form_type == "1095-C":
        form = replace(form, coverage=("ALL",), line15={"ALL": _money(rng, 50, 300)})
    return form


def expected_boxes(form: SynthTaxForm) -> dict[str, Decimal]:
    """The stored boxes a parser must return for ``form`` (``tax_form_data.boxes``)."""
    out = dict(form.amounts)
    for code, amount in form.box12:
        out[f"12_{code}"] = amount
    if form.other:
        key = "14t" if form.form_type == "W-2" else "10t"
        out[key] = sum((amount for _, amount in form.other), Decimal(0))
    if form.monthly:
        totals = [Decimal(0), Decimal(0), Decimal(0)]
        for month, values in form.monthly.items():
            for column, value in zip("ABC", values, strict=True):
                out[f"{20 + month}{column}"] = value
            totals = [t + v for t, v in zip(totals, values, strict=True)]
        for column, total in zip("ABC", totals, strict=True):
            out[f"33{column}"] = total
    if form.coverage == ("ALL",):
        out["cov_ALL"] = Decimal(1)
    else:
        for code in form.coverage:
            out[f"cov_{code}"] = Decimal(1)
    for key, amount in form.line15.items():
        out[f"15_{key}"] = amount
    return out


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _fmt(value: Decimal) -> str:
    return f"{value:,.2f}"


def _identity_values(form: SynthTaxForm, box: BoxDef) -> list[str]:
    """Fake values for an identity box (module doc)."""
    label = box.label.lower()
    issuer = form.issuer or ""
    if box.id == form_def("US", form.form_type).issuer_box_id:
        return [issuer, ISSUER_STREET, ISSUER_CITY, PHONE]
    if box.id == "rtn":
        return [RTN]
    if box.id == "covered":
        return [RECIPIENT, FAKE_SSN, BIRTH]
    if box.id in ("responsible", "employee"):
        return [RECIPIENT, FAKE_SSN, RECIPIENT_STREET, RECIPIENT_CITY]
    if box.id in ("employer", "issuer"):
        return [issuer, FAKE_EIN, ISSUER_STREET, ISSUER_CITY]
    if "state" in label and ("id" in label or "no." in label or "number" in label):
        return [STATE_ID]
    if "tin" in label or "social security" in label or "ssn" in label or "identification" in label:
        if "borrower" in label or "student" in label:
            return [FAKE_SSN]
        if any(k in label for k in ("payer's tin", "filer", "employer", "lender")):
            return [FAKE_EIN]
        return [FAKE_SSN]
    if "policy" in label:
        return [POLICY]
    if "birth" in label:
        return [BIRTH]
    if "control" in label:
        return [CONTROL]
    if "claim" in label:
        return [CLAIM]
    if "account" in label or "acct" in label:
        return [ACCOUNT]
    if "zip" in label and "address" not in label:
        return ["00000"]
    if "address" in label or "city" in label or "province" in label:
        return [RECIPIENT_STREET, RECIPIENT_CITY]
    return [RECIPIENT]


def _label(box: BoxDef) -> str:
    prefix = box.id if len(box.id) <= 3 else ""
    return f"{prefix} {box.label}".strip()


class _Page:
    """A canvas with a cursor and page breaks."""

    left: Final = 36.0
    width: Final = 540.0
    bottom: Final = 60.0

    def __init__(self, c: canvas.Canvas) -> None:
        self.c = c
        self.y = 750.0

    def need(self, height: float) -> None:
        if self.y - height < self.bottom:
            self.c.showPage()
            self.y = 750.0

    def text(self, x: float, y: float, value: str, *, size: float = 9, bold: bool = False) -> None:
        self.c.setFont(BOLD if bold else FONT, size)
        self.c.drawString(x, y, value)


def _cell(
    page: _Page, x: float, top: float, width: float, label: str, lines: Sequence[str]
) -> float:
    """Draw a labelled box; returns its height."""
    label_lines = simpleSplit(label, FONT, 6.5, width - 30)[:3]
    height = 8 + 8 * len(label_lines) + 12 * max(1, len(lines)) + 4
    page.c.rect(x, top - height, width, height, stroke=1, fill=0)
    y = top - 8
    for line in label_lines:
        page.text(x + 4, y, line, size=6.5)
        y -= 8
    y -= 4
    for line in lines:
        page.text(x + 4, y, line, size=9)
        y -= 12
    return height


def _grid(page: _Page, cells: Sequence[tuple[str, list[str]]], columns: int) -> None:
    width = page.width / columns
    for start in range(0, len(cells), columns):
        row = cells[start : start + columns]
        heights = [
            8 + 8 * len(simpleSplit(label, FONT, 6.5, width - 30)[:3]) + 12 * max(1, len(lines)) + 4
            for label, lines in row
        ]
        height = max(heights)
        page.need(height)
        for index, (label, lines) in enumerate(row):
            _cell(page, page.left + index * width, page.y, width, label, lines)
        page.y -= height


def _draw_other(page: _Page, x: float, y: float, form: SynthTaxForm) -> None:
    for label, amount in form.other:
        page.text(x, y, label)
        page.text(x + 70, y, _fmt(amount))
        y -= 12


def _amount_cells(form: SynthTaxForm, definition: FormDef) -> list[tuple[str, list[str]]]:
    cells: list[tuple[str, list[str]]] = []
    for box in definition.boxes:
        if box.identity or box.id in ("14t", "10t", "cov") or box.kind is BoxKind.CODED_AMOUNT:
            continue
        if form.form_type == "1095-A" and box.kind is BoxKind.AMOUNT:
            continue  # Part III table
        if form.form_type == "W-2" and box.id == "12":
            continue
        if form.other and box.id == ("14" if form.form_type == "W-2" else "10"):
            continue  # drawn with its lines by _other_box
        value = form.amounts.get(box.id)
        cells.append((_label(box), [_fmt(value)] if value is not None else []))
    return cells


def _w2_box12(page: _Page, form: SynthTaxForm) -> None:
    width = page.width / 4
    height = 34.0
    page.need(height)
    for index, letter_ in enumerate("abcd"):
        x = page.left + index * width
        page.c.rect(x, page.y - height, width, height, stroke=1, fill=0)
        page.text(x + 4, page.y - 8, f"12{letter_} See instructions for box 12", size=6.5)
        if index < len(form.box12):
            code, amount = form.box12[index]
            page.text(x + 4, page.y - 26, code)
            page.text(x + 40, page.y - 26, _fmt(amount))
    page.y -= height


def _other_box(page: _Page, form: SynthTaxForm, box_id: str) -> None:
    height = 20.0 + 12 * len(form.other)
    page.need(height)
    page.c.rect(page.left, page.y - height, page.width / 2, height, stroke=1, fill=0)
    page.text(page.left + 4, page.y - 8, f"{box_id} Other", size=6.5)
    _draw_other(page, page.left + 4, page.y - 20, form)
    page.y -= height


def _monthly_table(page: _Page, form: SynthTaxForm) -> None:
    page.need(40)
    page.text(page.left, page.y - 10, "Part III Coverage Information", bold=True)
    page.y -= 24
    headers = ("A. Monthly enrollment premiums", "B. Monthly SLCSP premium", "C. Monthly APTC")
    for index, header in enumerate(headers):
        page.text(page.left + 160 + index * 130, page.y, header, size=6.5)
    page.y -= 14
    totals = [Decimal(0), Decimal(0), Decimal(0)]
    for month in range(1, 13):
        values = form.monthly.get(month)
        page.need(14)
        page.text(page.left, page.y, f"{20 + month} {_MONTH_NAMES[month - 1]}")
        if values is not None:
            for index, value in enumerate(values):
                page.text(page.left + 160 + index * 130, page.y, _fmt(value))
                totals[index] += value
        page.y -= 13
    page.text(page.left, page.y, "33 Annual Totals")
    for index, total in enumerate(totals):
        page.text(page.left + 160 + index * 130, page.y, _fmt(total))
    page.y -= 20


def _month_header(page: _Page, x0: float, title: str) -> list[float]:
    """Column header ``All 12 Months Jan ... Dec``; returns the column x positions."""
    page.need(30)
    page.text(page.left, page.y, title, bold=True)
    page.y -= 14
    xs = [x0, *(x0 + 56 + 30 * i for i in range(12))]
    page.text(xs[0], page.y, "All 12 Months", size=6.5)
    for index, name in enumerate(_MONTH_NAMES, start=1):
        page.text(xs[index], page.y, name[:3], size=6.5)
    page.y -= 12
    return xs


def _coverage_table(page: _Page, form: SynthTaxForm) -> None:
    page.text(page.left, page.y - 4, "Covered Individuals", bold=True)
    page.y -= 18
    page.text(page.left, page.y, "Name of covered individual", size=6.5)
    page.text(page.left + 120, page.y, "SSN or other TIN", size=6.5)
    page.text(page.left + 190, page.y, "DOB", size=6.5)
    page.y -= 2
    xs = [page.left + 250 + 22 * i for i in range(13)]
    page.text(xs[0], page.y + 8, "All 12 Months", size=5)
    for index, name in enumerate(_MONTH_NAMES, start=1):
        page.text(xs[index] + 8, page.y - 6, name[:3], size=6.5)
    page.y -= 18
    page.text(page.left, page.y, "Jordan Quill")
    page.text(page.left + 60, page.y, "Testperson")
    page.text(page.left + 120, page.y, FAKE_SSN)
    page.text(page.left + 190, page.y, BIRTH)
    if form.coverage == ("ALL",):
        page.text(xs[0] + 10, page.y, "X")
    else:
        for month in form.coverage:
            page.text(xs[MONTHS.index(month) + 1] + 10, page.y, "X")
    page.y -= 20


def _line15_table(page: _Page, form: SynthTaxForm) -> None:
    xs = _month_header(page, page.left + 60, "Part II Employee Offer and Coverage")
    rows = (("14", "1E"), ("15", None), ("16", "2C"))
    for line, code in rows:
        page.text(page.left, page.y, line)
        if code is not None:
            page.text(xs[0] + 4, page.y, code)
        else:
            for key, amount in form.line15.items():
                column = 0 if key == "ALL" else MONTHS.index(key) + 1
                page.text(xs[column], page.y, _fmt(amount), size=7)
        page.y -= 14
    page.y -= 8


def _draw_form(page: _Page, form: SynthTaxForm) -> None:
    definition = form_def("US", form.form_type)
    page.text(page.left, page.y, f"Form {form.form_type}", size=12, bold=True)
    page.text(page.left + 110, page.y, definition.title[:60], size=10, bold=True)
    page.text(page.left + 500, page.y, str(form.tax_year), size=12, bold=True)
    page.y -= 14
    page.text(page.left, page.y, "Copy B For Recipient", size=7)
    page.text(
        page.left + 200, page.y, "Department of the Treasury Internal Revenue Service", size=7
    )
    page.y -= 10
    identity = [
        (_label(box), _identity_values(form, box)) for box in definition.boxes if box.identity
    ]
    _grid(page, identity, 2)
    if form.form_type == "1095-A":
        _monthly_table(page, form)
    if form.form_type == "1095-C":
        _line15_table(page, form)
    _grid(page, _amount_cells(form, definition), 3)
    if form.form_type == "W-2":
        _w2_box12(page, form)
    if form.other:
        _other_box(page, form, "14" if form.form_type == "W-2" else "10")
    if form.coverage:
        page.need(60)
        _coverage_table(page, form)
    page.text(
        page.left, 40, "Synthetic test document. Every name and number on it is invented.", size=7
    )


def forms_pdf(forms: Iterable[SynthTaxForm]) -> bytes:
    """Draw ``forms``, each starting on a new page (a consolidated statement)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    first = True
    for form in forms:
        if not first:
            c.showPage()
        first = False
        _draw_form(_Page(c), form)
    c.showPage()
    c.save()
    return buf.getvalue()


def form_pdf(form: SynthTaxForm) -> bytes:
    return forms_pdf([form])


def all_forms(seed: int = 0, tax_year: int = 2025) -> list[SynthTaxForm]:
    return [synth_form(t, seed=seed, tax_year=tax_year) for t in FORM_TYPES]


def unregistered_pdf(kind: str, tax_year: int = 2024) -> bytes:
    """A tax document outside the registered form set, with the fake identity values.

    ``kind`` ``1040``: a prior-year individual return drawn like a short
    receipt (a name line on top, a date, amounts and a ``Total``), the shape
    the classifier has called a receipt (security review P56-SR-R1-02).
    ``benefits``: an unrecognised statement with no tax-form heading, only an
    SSN, name and address next to amounts.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 740.0

    def line(text: str, font: str = FONT, size: float = 11) -> None:
        nonlocal y
        c.setFont(font, size)
        c.drawString(72, y, text)
        y -= 18

    if kind == "1040":
        line(RECIPIENT, BOLD, 14)
        line(RECIPIENT_STREET)
        line(RECIPIENT_CITY)
        line(f"Form 1040 U.S. Individual Income Tax Return {tax_year}", BOLD)
        line(f"Your social security number {FAKE_SSN}")
        line(f"Date 04/15/{tax_year + 1}")
        line("Wages 1 52,340.00")
        line("Taxable interest 2b 112.40")
        line("Total income 52,452.40")
        line("Total tax 4,210.00")
        line("Total 4,210.00", BOLD)
    elif kind == "benefits":
        line("Annual Benefit Statement", BOLD, 14)
        line(RECIPIENT)
        line(RECIPIENT_STREET)
        line(RECIPIENT_CITY)
        line(f"SSN {FAKE_SSN}")
        line(f"Benefits paid in {tax_year} 6,000.00")
        line("Amount withheld 600.00")
    else:  # pragma: no cover - test helper misuse
        raise ValueError(kind)
    c.showPage()
    c.save()
    return buf.getvalue()

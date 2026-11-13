# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tax form parser: form type, tax year, issuer name and amount boxes (INGESTION.md "Parse").

Reads the information returns of a jurisdiction's form set
(``app.domain.tax.forms``, plug-in point "Tax form sets"; the US set ships
W-2, the 1099 family, SSA-1099, 1098, 1098-E, 1098-T and 1095-A/B/C) from
the stored pages, in the worker:

* **Form type**: a heading ``Form <type>`` (``Form W-2``, ``Form 1099-INT``;
  the longest type wins, so ``1098-E`` is not read as ``1098``), or, as a
  broker's composite statement prints it, a cell that starts with the year
  and the type (``2025 1099-DIV Dividends and Distributions``). Every
  heading starts a section, so a consolidated statement (1099-INT, -DIV and
  -B on consecutive pages) gives one form each; identical copies of a form
  (Copy B, C, 2 on one sheet) are kept once. Two headings of different
  forms on one line open two sections side by side, cut at the second
  heading's left edge: each reads only the cells on its side. A later
  heading on that page that fits inside one of those columns starts its
  form in that column only: the forms on its left stay open, the columns
  on its right are read by nobody until a heading opens them again (their
  rows could be the older form's or the new form's second column). A
  heading wider than its column, several headings on a line or a heading
  on another page start a new layout. A mention is
  no heading: a contents line (it ends in a page number and holds no
  amount), a line naming three or more forms, a ``Form <type>`` inside
  parentheses, a year and a type followed by words other than the form's
  title (``2024 1099-INT corrections were issued separately``).
* **Tax year**: the year a heading starts with, else ``Tax year 2025``,
  ``For calendar year 2025`` or the year printed on the section's side of
  the heading line.
* **Dot leaders**: a cell is cut at every run of three or more dots
  (:func:`split_leaders`), so ``Total ordinary dividends......1,840.25`` is
  a label and its amount.
* **Amount boxes** by label: a box is a cell starting with its id and the
  first words of its label (``1 Wages, tips, other compensation``); the
  word ``Box`` before the id is part of the label (``Box 3. Benefits Paid
  in 2025``, as the SSA-1099 prints it), a box named inside a sentence
  (``Description of Amount in Box 3``) is not one; a box id printed
  apart from its label (``1a`` | ``Total ordinary dividends``) is read with
  it when the two together are the label; its
  amount is the first amount below it in its column (up to the next label)
  or right after it on the same line. A cell that says the box is empty
  (``NONE``, ``N/A``, a dash) ends that search, and a label printed with
  dot leaders is read from its own line only: the next amount down the
  page (a description line, a total) is not the box. W-2 box 12 (``12a``..``12d``: code and
  amount) is stored per code (``12_D``); a text box with an amount sibling
  ``<id>t`` (W-2 box 14, 1098 box 10 "Other") stores the sum of its lines.
  1095-A Part III (lines 21-33, columns A/B/C) is a table; 1095-B/C covered
  months are the ``X`` marks under a ``Jan .. Dec`` header (``cov_<MON>``,
  or ``cov_ALL`` under "All 12 Months"), 1095-C line 15 its amounts under
  the same kind of header (``15_ALL``, ``15_<MON>``).
* **Issuer name**: the first line under the form's issuer box label (W-2
  box c, the 1099 payer box, the 1098 lender box...): the payer's or
  employer's display name, the only text kept. A form with no issuer box
  (SSA-1099: its numbered boxes 1, 2, 7 and 8 identify the beneficiary) has
  no issuer name.

**Identity boxes are never read.** Nothing from an SSN/TIN/EIN, name,
address, account, control or policy number box gets into the output: the
result model (:class:`~app.ingestion.sandbox.protocol.TaxFormRead`) has
room for amounts only, and every key is checked against the form's stored
boxes. Because the stored extraction would still hold the page text, the
worker then calls :func:`redact_identity`, which rewrites the pages, lines,
layout tables and text of a tax form so that only form vocabulary (the
words of the registered forms' titles and labels), amounts, years, box ids
and codes remain (a word of label, dot leaders and amount is kept when each
of its pieces is); everything else (names, addresses, TINs, account
numbers) becomes ``***`` before the extraction is stored or indexed.
:func:`carries_tax_identity` extends this to documents outside the
registered set: any document no statement, payslip or loan parser read
whose text holds an SSN/ITIN, a labelled EIN or a tax-return heading is
redacted the same way, and any receipt read from it is dropped.

Confidence per form: ``form_type`` and ``boxes`` :data:`conf.GENERIC`,
``tax_year`` :data:`conf.GENERIC` when labelled, :data:`conf.INFERRED` when
taken from the heading line, ``issuer`` :data:`conf.INFERRED`; OCR lowers
all of them (lowest word confidence of the cells used).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import cache
from typing import ClassVar, Final

from app.core.money import sum_amounts
from app.domain.tax.forms import (
    BoxDef,
    BoxKind,
    FormDef,
    FormSet,
    get_form_set,
    jurisdictions,
)
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.base import (
    Cell,
    DocumentParser,
    DocumentView,
    ParseOutcome,
    ViewLine,
    join_cells,
)
from app.ingestion.parsers.text import normalise, read_amount
from app.ingestion.sandbox.protocol import ExtractResult, LayoutTable, Page, TaxFormRead

PARSER_NAME: Final = "tax_forms"
PARSER_VERSION: Final = "1"
REDACTED: Final = "***"

#: Lines below a label searched for its amount.
_MAX_BELOW: Final = 6
#: Points below a label searched for its amount.
_MAX_DROP: Final = 80.0
#: Column window when no label follows on the right.
_DEFAULT_COLUMN: Final = 250.0
_YEAR_MIN: Final = 1990
_YEAR_MAX: Final = 2100
_MONTHS: Final = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)
_MONTH_WORDS: Final = {m.lower(): m for m in _MONTHS}
_MARKS: Final = frozenset({"x", "X", "✓", "✔", "✗", "✘"})
_YEAR_RE: Final = re.compile(r"^(?:19|20)\d{2}$")
#: A cell that may be a year-first heading (``2025 1099-DIV ...``).
_YEAR_FIRST: Final = re.compile(r"^(?:19|20)\d{2}\s")
#: Dot leaders between a label and its amount: three or more dots, also
#: printed with single spaces between them.
_LEADER: Final = re.compile(r"\s*\.(?: ?\.){2,}\s*")
_PARENTHESISED: Final = re.compile(r"\([^()]*\)")
#: The page number a contents line ends in.
_PAGE_NUMBER: Final = re.compile(r"^\d{1,3}$")
_DECIMALS: Final = re.compile(r"\d[.,]\d{2}(?!\d)")
#: A line naming this many different forms lists them; it heads none.
_LISTED_FORMS: Final = 3
_LABELLED_YEAR: Final = (
    re.compile(r"\btax year (\d{4})\b"),
    re.compile(r"\b(\d{4}) tax year\b"),
    re.compile(r"\bfor (?:the )?calendar year (\d{4})\b"),
)
_BOX12_RE: Final = re.compile(r"^12\s?[a-d]\b")
#: Printed before a box id on some forms (normalised).
_BOX_PREFIX: Final = "box "
_CODE_RE: Final = re.compile(r"^[A-Z]{1,2}$")
#: Printed in place of an amount: the box is empty.
_NO_AMOUNT: Final = frozenset({"none", "n/a", "na", "nil", "-", "--", "---", "\u2013", "\u2014"})
_PART3_RE: Final = re.compile(r"^(2[1-9]|3[0-3])\b")
#: Tokens kept by :func:`redact_identity` besides the vocabulary.
_BOX_ID_TOKEN: Final = re.compile(r"^\d{1,2}[A-Za-z]?$")
_SHORT_CODE: Final = re.compile(r"^[A-Z]{1,2}\d?$|^\d[A-Z]$")
_PUNCT: Final = ".,:;()[]'\"/*-"
#: Words printed on information returns besides their labels.
_COMMON_TEXT: Final = """form copy b c d 1 2 for to recipient employee employer payer filer
borrower lender student department of the treasury internal revenue service irs omb no rev this
is important tax information and being furnished you if are required file a return negligence
penalty or other sanction may be imposed on income taxable not has been reported corrected void
statement year calendar part i ii iii iv v covered individuals all months jan feb mar apr may jun
jul aug sep oct nov dec january february march april june july august september october november
december annual totals total monthly premium premiums slcsp aptc see instructions box boxes code
codes amount amounts offer coverage page summary consolidated information returns federal state
local name dob ssn tin ein other every number on it in with from by at as an each per line lines"""
_COMMON_WORDS: Final = frozenset(_COMMON_TEXT.split())

_SSN_LIKE: Final = re.compile(r"\d{3}-\d{2}-\d{4}|\d{2}-\d{7}")
#: A taxpayer identification number as printed (SSN/ITIN ``123-45-6789``).
_TIN_TOKEN: Final = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")
#: An EIN only next to its label (``EIN 12-3456789``): bare, the shape is too common.
_EIN_LABELLED: Final = re.compile(
    r"\b(?:ein|employer identification (?:number|no))\b[^\d\n]{0,40}\d{2}-\d{7}(?!\d)",
    re.IGNORECASE,
)
#: Headings of tax documents outside the registered set (returns and schedules).
_TAX_RETURN_HEADING: Final = re.compile(
    r"\bform\s{0,3}10(?:40|41|65|99|98|95|20)(?:-[a-z]{1,3})?\b"
    r"|\bincome\s{1,3}tax\s{1,3}return\b|\bsocial\s{1,3}security\s{1,3}number\b"
    r"|\btaxpayer\s{1,3}identification\s{1,3}number\b",
    re.IGNORECASE,
)
#: Characters of a document scanned for identity (the start holds the headings).
IDENTITY_SCAN_CHARS: Final = 1_000_000


# ---------------------------------------------------------------------------
# Form set knowledge
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Label:
    box: BoxDef
    #: Normalised prefix a label cell starts with.
    key: str


def _label_key(box: BoxDef) -> str:
    words = normalise(box.label).split()
    if len(box.id) <= 3:
        return " ".join([box.id.lower(), *words[:2]])
    # Named identity boxes: long enough that a wrapped label line such as
    # "payer(s)/borrower(s)" is not taken for "Payer's/Borrower's name".
    return " ".join(words[:5])


@dataclass(frozen=True)
class _FormInfo:
    form: FormDef
    jurisdiction: str
    heading: re.Pattern[str]
    labels: tuple[_Label, ...]
    stored: frozenset[str]
    #: The normalised form type (``1099 div``).
    type_norm: str = ""
    #: A cell starting with a year and the type (``2025 1099 div ...``).
    year_heading: re.Pattern[str] = re.compile(r"(?!)")
    #: Box ids that can stand alone in a cell (lower case, at most three characters).
    short_ids: frozenset[str] = frozenset()
    #: The first word of the form's title (normalised).
    title_word: str = ""

    def heading_year(self, cell: Cell) -> int | None:
        """The year ``cell`` starts with when it is a year-first heading of this form.

        After the year and the type the cell ends or goes on with the form's
        title: ``2024 1099-INT corrections were issued separately`` is a
        sentence, ``2025 W-2 and EARNINGS SUMMARY`` another document's line.
        """
        text = normalise(_PARENTHESISED.sub(" ", cell.text))
        match = self.year_heading.match(text)
        if match is None or not _YEAR_MIN <= int(match.group(1)) <= _YEAR_MAX:
            return None
        rest = text[match.end() :].split()
        if rest and rest[0] != self.title_word:
            return None
        return int(match.group(1))

    def label_of(self, cell: Cell) -> _Label | None:
        # "Box 3. Benefits Paid in 2025": the word before the id is no label word.
        text = normalise(cell.text).removeprefix(_BOX_PREFIX)
        for label in self.labels:
            if text == label.key or text.startswith(label.key + " "):
                return label
        return None

    def is_label(self, cell: Cell) -> bool:
        return self.label_of(cell) is not None or bool(_BOX12_RE.match(normalise(cell.text)))


def _forms_of(form_set: FormSet) -> list[_FormInfo]:
    infos: list[_FormInfo] = []
    for form in form_set.forms.values():
        type_norm = normalise(form.form_type)
        heading = re.compile(rf"\bform {re.escape(type_norm)}(?: |$)")
        year_heading = re.compile(rf"^(\d{{4}}) {re.escape(type_norm)}(?: |$)")
        labels = tuple(
            _Label(box, _label_key(box))
            for box in form.boxes
            if box.kind is not BoxKind.CHECKBOX or len(box.id) <= 3
        )
        # Longer keys first: "1 wages tips" must not hide "12a see instructions".
        labels = tuple(sorted(labels, key=lambda lab: -len(lab.key)))
        short_ids = frozenset(lab.box.id.lower() for lab in labels if len(lab.box.id) <= 3)
        infos.append(
            _FormInfo(
                form,
                form_set.jurisdiction,
                heading,
                labels,
                form.stored_keys(),
                type_norm,
                year_heading,
                short_ids,
                (normalise(form.title).split() or [""])[0],
            )
        )
    # Longest form type first ("1098 e" before "1098").
    return sorted(infos, key=lambda info: -len(info.type_norm))


def _all_forms() -> list[_FormInfo]:
    out: list[_FormInfo] = []
    for code in jurisdictions():
        out.extend(_forms_of(get_form_set(code)))
    return sorted(out, key=lambda info: -len(info.type_norm))


@cache
def _vocabulary_for(codes: tuple[str, ...]) -> frozenset[str]:
    words: set[str] = set(_COMMON_WORDS)
    for code in codes:
        form_set = get_form_set(code)
        for form in form_set.forms.values():
            words.update(normalise(form.title).split())
            words.update(normalise(form.form_type).split())
            for box in form.boxes:
                words.update(normalise(box.label).split())
    return frozenset(words)


def vocabulary() -> frozenset[str]:
    """Words :func:`redact_identity` keeps (titles and labels of every registered form)."""
    return _vocabulary_for(tuple(jurisdictions()))


# ---------------------------------------------------------------------------
# Dot leaders
# ---------------------------------------------------------------------------


def _word_spans(cell: Cell) -> list[tuple[int, int, float, float]]:
    """``(start, end, x0, x1)`` per word of the cell's text; one span when not known."""
    spans: list[tuple[int, int, float, float]] = []
    if cell.words and " ".join(word for word, _, _ in cell.words) == cell.text:
        offset = 0
        for word, x0, x1 in cell.words:
            spans.append((offset, offset + len(word), x0, x1))
            offset += len(word) + 1
    return spans or [(0, len(cell.text), cell.x0, cell.x1)]


def _piece(cell: Cell, spans: Sequence[tuple[int, int, float, float]], a: int, b: int) -> Cell:
    """Characters ``a..b`` of ``cell``: a cut inside a word is placed by character offset."""
    words: list[tuple[str, float, float]] = []
    for start, end, x0, x1 in spans:
        lo, hi = max(a, start), min(b, end)
        if lo >= hi:
            continue
        width = (x1 - x0) / max(end - start, 1)
        words.append((cell.text[lo:hi], x0 + (lo - start) * width, x0 + (hi - start) * width))
    return Cell(
        cell.text[a:b], words[0][1], cell.y0, words[-1][2], cell.y1, cell.conf, tuple(words)
    )


def split_leaders(cell: Cell) -> tuple[Cell, ...]:
    """``cell`` cut at every run of dot leaders (module doc); alone when it has none."""
    text = cell.text
    if ". ." not in text and "..." not in text:
        return (cell,)
    cuts: list[tuple[int, int]] = []
    start = 0
    for match in _LEADER.finditer(text):
        if match.start() > start:
            cuts.append((start, match.start()))
        start = match.end()
    if start == 0:
        return (cell,)
    if start < len(text):
        cuts.append((start, len(text)))
    if not cuts:
        return (cell,)  # nothing but dots
    spans = _word_spans(cell)
    return tuple(_piece(cell, spans, a, b) for a, b in cuts)


def _split_line(line: ViewLine) -> ViewLine:
    if "." not in line.text:
        return line
    cells = tuple(piece for cell in line.cells for piece in split_leaders(cell))
    return line if len(cells) == len(line.cells) else replace(line, cells=cells)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


@dataclass
class _Section:
    info: _FormInfo
    lines: list[ViewLine]
    #: The heading line (its cells on the section's side).
    heading: ViewLine
    #: The cell that names the form.
    cell: Cell
    #: The section's side of the page: it reads cells whose left edge is in ``x0 <= x < x1``.
    x0: float = float("-inf")
    x1: float = float("inf")
    #: ``(page, index)`` of the lines that print dot leaders on the section's side.
    led: set[tuple[int, int]] = field(default_factory=set)

    def add(self, line: ViewLine, raw: ViewLine) -> None:
        """Take ``line`` (``raw`` cut at its dot leaders): the cells on the section's side."""
        clipped = _clip(line, self.x0, self.x1)
        if clipped is None:
            return
        self.lines.append(clipped)
        if any(self.x0 <= c.x0 < self.x1 and _LEADER.search(c.text) for c in raw.cells):
            self.led.add((line.page, line.index))


def _clip(line: ViewLine, x0: float, x1: float) -> ViewLine | None:
    """``line`` with only the cells whose left edge is in ``x0 <= x < x1``."""
    cells = tuple(c for c in line.cells if x0 <= c.x0 < x1)
    if len(cells) == len(line.cells):
        return line
    if not cells:
        return None
    return replace(line, cells=cells, text=" ".join(c.text for c in cells))


def _is_contents(line: ViewLine) -> bool:
    """A contents line: it ends in a page number and holds no amount with decimals."""
    if len(line.cells) < 2 or not _PAGE_NUMBER.match(line.cells[-1].text.strip()):
        return False
    return not any(_DECIMALS.search(cell.text) for cell in line.cells)


def _forms_named(norm: str, forms: Sequence[_FormInfo]) -> int:
    """How many different form types ``norm`` names (``forms``: longest type first)."""
    rest = f" {norm} "
    count = 0
    for info in forms:
        key = f" {info.type_norm} "
        if key in rest:
            count += 1
            rest = rest.replace(key, " | ")
    return count


def _headings(line: ViewLine, forms: Sequence[_FormInfo]) -> list[tuple[_FormInfo, Cell]]:
    """The forms ``line`` heads, each with its heading cell (module doc), left to right."""
    norm = line.norm
    has_form = "form" in norm
    if not has_form and not any(_YEAR_FIRST.match(cell.text) for cell in line.cells):
        return []
    if _is_contents(line) or _forms_named(norm, forms) >= _LISTED_FORMS:
        return []
    found: dict[str, tuple[_FormInfo, Cell]] = {}
    for cell in line.cells:
        text = normalise(_PARENTHESISED.sub(" ", cell.text))
        info = next((i for i in forms if i.heading_year(cell) is not None), None)
        if info is None and "form" in text:
            info = next((i for i in forms if i.heading.search(text)), None)
        if info is not None:
            found.setdefault(info.form.form_type, (info, cell))
    if not found and has_form:
        # "Form" and the type in neighbouring cells.
        text = normalise(_PARENTHESISED.sub(" ", line.text))
        info = next((i for i in forms if i.heading.search(text)), None)
        if info is not None:
            ends = [c for c in line.cells if normalise(c.text).endswith("form")]
            return [(info, ends[0] if ends else line.cells[0])]
    return sorted(found.values(), key=lambda pair: pair[1].x0)


def _column_of(
    columns: Sequence[_Section | None],
    bounds: Sequence[tuple[float, float]],
    page: int | None,
    line: ViewLine,
    heads: Sequence[tuple[_FormInfo, Cell]],
) -> int | None:
    """The column a lone heading opens in on a page of side-by-side forms.

    None when the line starts a new layout: no columns are open, it is on
    another page, it holds several headings, or its heading is wider than
    the column it starts in.
    """
    if len(bounds) < 2 or len(heads) != 1 or line.page != page:
        return None
    cell = heads[0][1]
    for position, (x0, x1) in enumerate(bounds):
        if x0 <= cell.x0 < x1:
            return position if cell.x1 <= x1 else None
    return None


def _sections(view: DocumentView, forms: Sequence[_FormInfo]) -> list[_Section]:
    sections: list[_Section] = []
    #: The open section of each column, left to right; None: nobody reads that column.
    columns: list[_Section | None] = []
    bounds: list[tuple[float, float]] = []
    page: int | None = None
    before: list[tuple[ViewLine, ViewLine]] = []
    for raw in view.lines:
        line = _split_line(raw)
        heads = _headings(line, forms)
        if not heads:
            if not bounds:
                before.append((line, raw))
            for section in columns:
                if section is not None:
                    section.add(line, raw)
            continue
        position = _column_of(columns, bounds, page, line, heads)
        if position is not None:
            # A heading inside one column. The forms on its left go on; what
            # is printed on its right may be an older form's rows or this
            # form's second column, so nobody reads it until a heading opens
            # that column again.
            info, cell = heads[0]
            x0, x1 = bounds[position]
            section = _Section(info, [], _clip(line, x0, x1) or line, cell, x0, x1)
            columns[position:] = [section, *([None] * (len(bounds) - position - 1))]
            sections.append(section)
            for each in columns[: position + 1]:
                if each is not None:
                    each.add(line, raw)
            continue
        lead = before if not sections else []
        before = []
        columns, bounds, page = [], [], line.page
        for position, (info, cell) in enumerate(heads):
            x0 = cell.x0 if position else float("-inf")
            x1 = heads[position + 1][1].x0 if position + 1 < len(heads) else float("inf")
            section = _Section(info, [], _clip(line, x0, x1) or line, cell, x0, x1)
            for each_line, each_raw in (*lead, (line, raw)):
                section.add(each_line, each_raw)
            columns.append(section)
            bounds.append((x0, x1))
            sections.append(section)
    return sections


# ---------------------------------------------------------------------------
# Reading one section
# ---------------------------------------------------------------------------


@dataclass
class _Read:
    boxes: dict[str, Decimal] = field(default_factory=dict)
    cells: list[Cell] = field(default_factory=list)

    def put(self, key: str, value: Decimal, *cells: Cell) -> None:
        self.boxes[key] = value
        self.cells.extend(cells)


def _window(line: ViewLine, label_cell: Cell, info: _FormInfo) -> tuple[float, float]:
    """The column of ``label_cell``: up to the next label on its line."""
    left = label_cell.x0 - 15
    right = label_cell.x0 + _DEFAULT_COLUMN
    for cell in line.cells:
        if cell.x0 > label_cell.x0 + 1 and info.is_label(cell):
            right = min(right, cell.x0 - 5)
    return left, right


def _below(
    lines: Sequence[ViewLine], index: int, label_cell: Cell, info: _FormInfo, *, limit: int
) -> Iterable[tuple[ViewLine, list[Cell]]]:
    """Lines under a label with their cells in its column, up to the next label."""
    left, right = _window(lines[index], label_cell, info)
    start = lines[index]
    for line in lines[index + 1 : index + 1 + limit]:
        if line.page != start.page:
            return
        if line.bbox[1] - label_cell.y1 > _MAX_DROP:
            return
        cells = [c for c in line.cells if left <= c.x0 < right]
        if any(info.is_label(c) for c in cells):
            return
        if cells:
            yield line, cells


def _amount_of(cell: Cell) -> Decimal | None:
    amount = read_amount(cell.text.rstrip(".,;:"))
    return amount.value if amount is not None else None


def _says_no_amount(cell: Cell) -> bool:
    return cell.text.strip().lower() in _NO_AMOUNT


def _labelled_amount(
    lines: Sequence[ViewLine], index: int, cell: Cell, info: _FormInfo, *, same_line: bool = False
) -> tuple[Decimal, Cell] | None:
    """The amount of the box labelled by ``cell``: right after it, else below it.

    A cell that says the box is empty (``NONE``) ends the search: amounts
    further down belong to something else. ``same_line``: the label is
    printed with dot leaders, which lead to its amount on the same line or
    to nothing.
    """
    line = lines[index]
    after = [c for c in line.cells if c.x0 > cell.x1]
    if after and not info.is_label(after[0]):
        value = _amount_of(after[0])
        if value is not None:
            return value, after[0]
        if _says_no_amount(after[0]):
            return None
    if same_line:
        return None
    for _, cells in _below(lines, index, cell, info, limit=_MAX_BELOW):
        for candidate in cells:
            value = _amount_of(candidate)
            if value is not None:
                return value, candidate
            if _says_no_amount(candidate):
                return None
    return None


def _summed_lines(
    lines: Sequence[ViewLine], index: int, cell: Cell, info: _FormInfo
) -> tuple[Decimal, list[Cell]] | None:
    found: list[tuple[Decimal, Cell]] = []
    for _, cells in _below(lines, index, cell, info, limit=12):
        for candidate in cells:
            value = _amount_of(candidate)
            if value is not None:
                found.append((value, candidate))
    if not found:
        return None
    return sum_amounts(v for v, _ in found), [c for _, c in found]


def _box12(lines: Sequence[ViewLine], index: int, cell: Cell, info: _FormInfo, read: _Read) -> None:
    box = info.form.box("12")
    if box is None:
        return
    for _, cells in _below(lines, index, cell, info, limit=3):
        codes = [c for c in cells if _CODE_RE.match(c.text.strip())]
        amounts = [(c, _amount_of(c)) for c in cells]
        for code_cell in codes:
            code = code_cell.text.strip()
            value = next(
                (v for c, v in amounts if v is not None and c.x0 > code_cell.x1),
                None,
            )
            if value is not None and code in box.codes:
                read.put(f"12_{code}", value, code_cell)
            return


def _part3(lines: Sequence[ViewLine], read: _Read) -> None:
    """1095-A Part III: lines 21-33, amounts in columns A, B, C."""
    header: list[float] = []
    for line in lines:
        starts = {normalise(c.text)[:2]: c for c in line.cells}
        if all(k in starts for k in ("a ", "b ", "c ")):
            header = [starts[k].x0 for k in ("a ", "b ", "c ")]
            continue
        first = normalise(line.cells[0].text)
        match = _PART3_RE.match(first)
        if match is None:
            continue
        amounts = [(c, _amount_of(c)) for c in line.cells[1:]]
        values = [(c, v) for c, v in amounts if v is not None]
        if not values or len(values) > 3:
            continue
        for c, v in values:
            if header:
                column = min(range(3), key=lambda i: abs(header[i] - c.x0))
            else:
                column = values.index((c, v))
            read.put(f"{match.group(1)}{'ABC'[column]}", v, c)


def _month_columns(
    lines: Sequence[ViewLine], index: int
) -> tuple[dict[str, float], float | None] | None:
    line = lines[index]
    months: dict[str, float] = {}
    all_x: float | None = None
    for cell in line.cells:
        words = normalise(cell.text).split()
        if words and words[-1] in _MONTH_WORDS and len(words) == 1:
            months[_MONTH_WORDS[words[-1]]] = cell.centre
        if "all 12 months" in normalise(cell.text):
            all_x = cell.x0 + 20 if len(words) > 3 else cell.centre
    if len(months) < 10:
        return None
    for previous in lines[max(0, index - 2) : index]:
        if previous.page != line.page:
            continue
        for cell in previous.cells:
            if normalise(cell.text) == "all 12 months":
                all_x = cell.centre
    return months, all_x


def _nearest(x: float, months: dict[str, float], all_x: float | None) -> str | None:
    columns = dict(months)
    if all_x is not None:
        columns["ALL"] = all_x
    key, centre = min(columns.items(), key=lambda kv: abs(kv[1] - x))
    return key if abs(centre - x) <= 25 else None


def _monthly_tables(lines: Sequence[ViewLine], info: _FormInfo, read: _Read) -> None:
    """1095-B/C covered months and 1095-C line 15 (module doc)."""
    has_cov = any(k.startswith("cov_") for k in info.stored)
    has_15 = "15_ALL" in info.stored
    for index, line in enumerate(lines):
        found = _month_columns(lines, index)
        if found is None:
            continue
        months, all_x = found
        for row in lines[index + 1 : index + 12]:
            if row.page != line.page or _month_columns(lines, lines.index(row)) is not None:
                break
            first = normalise(row.cells[0].text)
            if has_15 and first == "15":
                for cell in row.cells[1:]:
                    value = _amount_of(cell)
                    column = _nearest(cell.centre, months, all_x) if value is not None else None
                    if value is not None and column is not None:
                        read.put(f"15_{column}", value, cell)
                continue
            if not has_cov:
                continue
            for cell in row.cells:
                if cell.text.strip() in _MARKS:
                    column = _nearest(cell.centre, months, all_x)
                    if column is not None:
                        read.put(f"cov_{column}", Decimal(1), cell)


def _issuer(lines: Sequence[ViewLine], info: _FormInfo) -> tuple[str | None, list[Cell]]:
    box_id = info.form.issuer_box_id
    box = info.form.box(box_id) if box_id else None
    if box is None:
        return None, []
    key = _label_key(box)
    for index, line in enumerate(lines):
        for cell in line.cells:
            if not normalise(cell.text).startswith(key):
                continue
            for _, cells in _below(lines, index, cell, info, limit=3):
                text = cells[0].text.strip()
                if not text or _SSN_LIKE.search(text) or _amount_of(cells[0]) is not None:
                    continue
                if sum(ch.isdigit() for ch in text) > len(text) // 3:
                    continue
                return text[:200], [cells[0]]
            return None, []
    return None, []


def _tax_year(section: _Section) -> tuple[int | None, float]:
    year = section.info.heading_year(section.cell)
    if year is not None:
        return year, conf.GENERIC
    for line in section.lines[:40]:
        for pattern in _LABELLED_YEAR:
            match = pattern.search(line.norm)
            if match is not None and _YEAR_MIN <= int(match.group(1)) <= _YEAR_MAX:
                return int(match.group(1)), conf.GENERIC
    for cell in section.heading.cells:
        for token in cell.text.split():
            if _YEAR_RE.match(token) and _YEAR_MIN <= int(token) <= _YEAR_MAX:
                return int(token), conf.GENERIC
    return None, 0.0


def _with_lone_ids(line: ViewLine, info: _FormInfo) -> ViewLine:
    """``line`` with a box id printed apart from its label joined to it.

    Only where the id is a box of the form, the next cell starts with a
    letter and the two together are that box's label.
    """
    cells = line.cells
    if len(cells) < 2 or not any(
        c.text.strip().rstrip(".:").lower() in info.short_ids for c in cells
    ):
        return line
    out: list[Cell] = []
    index = 0
    while index < len(cells):
        cell = cells[index]
        following = cells[index + 1] if index + 1 < len(cells) else None
        if (
            following is not None
            and cell.text.strip().rstrip(".:").lower() in info.short_ids
            and following.text.lstrip()[:1].isalpha()
        ):
            joined = join_cells([cell, following])
            if info.label_of(joined) is not None:
                out.append(joined)
                index += 2
                continue
        out.append(cell)
        index += 1
    return line if len(out) == len(cells) else replace(line, cells=tuple(out))


def _label_lines(section: _Section) -> list[ViewLine]:
    return [_with_lone_ids(line, section.info) for line in section.lines]


def _holds_form_boxes(section: _Section) -> bool:
    """Two label cells of the form's own boxes, or one of its tables with a value.

    Identity labels (a recipient's name, an account number) do not count:
    any statement prints those. The other numbered boxes do, amounts or
    not: a 1098-E has one amount box and one checkbox.
    """
    info = section.info
    lines = _label_lines(section)
    hits = 0
    for line in lines:
        for cell in line.cells:
            label = info.label_of(cell)
            box12 = info.form.form_type == "W-2" and _BOX12_RE.match(normalise(cell.text))
            if box12 or (label is not None and label.box.kind is not BoxKind.IDENTITY):
                hits += 1
            if hits >= 2:
                return True
    # 1095-A/B/C print their amounts and covered months in tables without box labels.
    read = _Read()
    if "21A" in info.stored:
        _part3(lines, read)
    _monthly_tables(lines, info, read)
    return any(key in info.stored for key in read.boxes)


def _read_boxes(section: _Section) -> _Read:
    info = section.info
    lines = _label_lines(section)
    read = _Read()
    stored = info.stored
    for index, line in enumerate(lines):
        for cell in line.cells:
            if _BOX12_RE.match(normalise(cell.text)) and info.form.form_type == "W-2":
                _box12(lines, index, cell, info, read)
                continue
            label = info.label_of(cell)
            if label is None:
                continue
            box = label.box
            if box.kind is BoxKind.AMOUNT and box.id in stored and box.id not in read.boxes:
                led = (line.page, line.index) in section.led
                found = _labelled_amount(lines, index, cell, info, same_line=led)
                if found is not None:
                    read.put(box.id, found[0], found[1])
            elif box.kind is BoxKind.TEXT and f"{box.id}t" in stored:
                summed = _summed_lines(lines, index, cell, info)
                if summed is not None:
                    read.put(f"{box.id}t", summed[0], *summed[1])
    if "21A" in stored:
        _part3(lines, read)
    _monthly_tables(lines, info, read)
    read.boxes = {k: v for k, v in read.boxes.items() if k in stored}
    return read


def _ocr_factor(cells: Iterable[Cell]) -> float:
    values = [c.conf for c in cells if c.conf is not None]
    return min(values) if values else 1.0


def _form_read(section: _Section, index: int, threshold: float) -> TaxFormRead | None:
    read = _read_boxes(section)
    if not read.boxes:
        return None
    year, year_conf = _tax_year(section)
    issuer, issuer_cells = _issuer(section.lines, section.info)
    factor = _ocr_factor([*section.heading.cells, *read.cells, *issuer_cells])
    confidence = {
        "form_type": round(conf.GENERIC * _ocr_factor(section.heading.cells), 4),
        "tax_year": round(year_conf * factor, 4),
        "boxes": round(conf.GENERIC * _ocr_factor(read.cells), 4),
    }
    if issuer is not None:
        confidence["issuer"] = round(conf.INFERRED * factor, 4)
    low = sorted(k for k, v in confidence.items() if v < threshold and k != "issuer")
    return TaxFormRead(
        jurisdiction=section.info.jurisdiction,
        form_type=section.info.form.form_type,
        form_index=index,
        tax_year=year,
        issuer_name=issuer,
        boxes=read.boxes,
        confidence=confidence,
        low_confidence=low,
    )


def read_tax_forms(view: DocumentView, *, threshold: float) -> list[TaxFormRead]:
    """Every distinct form in ``view`` (module doc)."""
    forms: list[TaxFormRead] = []
    seen: set[tuple[str, int | None, tuple[tuple[str, Decimal], ...]]] = set()
    counts: dict[str, int] = {}
    for section in _sections(view, _all_forms()):
        form_type = section.info.form.form_type
        read = _form_read(section, counts.get(form_type, 0), threshold)
        if read is None:
            continue
        fingerprint = (form_type, read.tax_year, tuple(sorted(read.boxes.items())))
        if fingerprint in seen:
            continue  # another copy of the same form
        seen.add(fingerprint)
        counts[form_type] = counts.get(form_type, 0) + 1
        forms.append(read)
        if len(forms) >= 50:
            break
    return forms


class TaxFormParser(DocumentParser):
    name: ClassVar[str] = PARSER_NAME
    version: ClassVar[str] = PARSER_VERSION
    kind = "tax_form"

    def detect(self, view: DocumentView) -> float:
        forms = _all_forms()
        sections = _sections(view, forms)
        if not sections:
            return 0.0
        return 0.97 if any(_holds_form_boxes(section) for section in sections) else 0.3

    def parse(
        self, view: DocumentView, *, threshold: float, date_order: str | None = None
    ) -> ParseOutcome:
        forms = read_tax_forms(view, threshold=threshold)
        outcome = ParseOutcome(tax_forms=forms)
        if not forms:
            outcome.warnings.append("no_tax_form_boxes")
        if view.truncated:
            outcome.warnings.append("truncated")
        return outcome


# ---------------------------------------------------------------------------
# Redaction of the stored pages
# ---------------------------------------------------------------------------


def _keep(token: str, words: frozenset[str]) -> bool:
    core = token.strip(_PUNCT)
    if not core:
        return True
    if read_amount(token.rstrip(".,;:")) is not None:
        return True
    if "..." in core:
        # Label, dot leaders and amount printed as one word: kept when every piece is.
        return all(_keep(piece, words) for piece in _LEADER.split(core) if piece)
    if _YEAR_RE.match(core) or _BOX_ID_TOKEN.match(core) or _SHORT_CODE.match(core):
        return True
    parts = normalise(core).split()
    return bool(parts) and all(part in words for part in parts) and not _SSN_LIKE.search(core)


def redact_text(text: str, words: frozenset[str] | None = None) -> str:
    """``text`` with every token that is not form vocabulary, an amount or a box id masked."""
    vocab = vocabulary() if words is None else words
    out: list[str] = []
    for line in text.split("\n"):
        out.append(" ".join(t if _keep(t, vocab) else REDACTED for t in line.split()))
    return "\n".join(out)


def _redact_table(table: LayoutTable, words: frozenset[str]) -> LayoutTable:
    rows = [[redact_text(cell, words) for cell in row] for row in table.rows]
    return table.model_copy(update={"rows": rows})


def _redact_page(page: Page, words: frozenset[str]) -> Page:
    new_words = [
        w if _keep(w.text, words) else w.model_copy(update={"text": REDACTED}) for w in page.words
    ]
    new_lines = [
        line.model_copy(
            update={
                "text": " ".join(
                    w.text for w in new_words[line.first_word : line.first_word + line.word_count]
                )
            }
        )
        for line in page.lines
    ]
    text = "\n".join(line.text for line in new_lines)
    if not new_lines and page.text:
        text = redact_text(page.text, words)
    return page.model_copy(
        update={
            "words": new_words,
            "lines": new_lines,
            "text": text,
            "tables": [_redact_table(t, words) for t in page.tables],
        }
    )


def carries_tax_identity(result: ExtractResult) -> bool:
    """Whether the text holds a taxpayer identity or a tax-document heading.

    True for an SSN/ITIN-shaped token, a labelled EIN, or a heading such as
    ``Form 1040`` / ``Income Tax Return`` / ``Social Security Number``: a
    tax document outside the registered set (a prior-year return, an
    unrecognised form) that the classifier may call something else
    (security review P56-SR-R1-02). The worker then redacts it like a tax
    form. The patterns are linear (no nested quantifiers) and the scan is
    capped at :data:`IDENTITY_SCAN_CHARS` per text.
    """
    texts = [result.text or "", *(page.text or "" for page in result.pages)]
    for table in result.tables:
        texts.extend(" ".join(str(c) for c in row) for row in table.rows)
    for raw in texts:
        text = raw[:IDENTITY_SCAN_CHARS]
        if _TIN_TOKEN.search(text) or _EIN_LABELLED.search(text):
            return True
        if _TAX_RETURN_HEADING.search(text):
            return True
    return False


def redact_identity(result: ExtractResult, forms: Sequence[TaxFormRead] = ()) -> ExtractResult:
    """The extraction of a tax form with identity text masked (module doc).

    The forms read (type, year, issuer name) are appended to the text so the
    document stays searchable by them.
    """
    words = vocabulary()
    pages = [_redact_page(page, words) for page in result.pages]
    summary = [
        " ".join(
            part
            for part in (f"Form {f.form_type}", str(f.tax_year or ""), f.issuer_name or "")
            if part
        )
        for f in forms
    ]
    text = "\f".join(page.text for page in pages)
    if not pages and result.text:
        text = redact_text(result.text, words)
    if summary:
        text = "\n".join([text, *summary]) if text else "\n".join(summary)
    meta = result.meta
    if meta is not None:
        meta = meta.model_copy(update={"title": None, "author": None})
    return result.model_copy(
        update={
            "pages": pages,
            "text": text,
            "tables": [
                t.model_copy(
                    update={"rows": [[redact_text(str(c), words) for c in r] for r in t.rows]}
                )
                for t in result.tables
            ],
            "meta": meta,
        }
    )

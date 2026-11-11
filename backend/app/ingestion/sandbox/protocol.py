# SPDX-License-Identifier: AGPL-3.0-or-later
"""The JSON contract between the worker and the extraction child (ADR-0006).

The parent passes a :class:`SandboxJob` as the child's only argument and the
file's plaintext on stdin; the child answers with one :class:`ExtractResult`
on stdout. The parent validates the answer with these models (``extra``
fields forbidden, every string and list bounded), so a compromised or
confused child can only ever hand back data of this shape. Amounts travel as
decimal strings and are re-validated here (finite, bounded).

Nothing in this module parses file content; the extractors live in
``app.ingestion.extract`` and only the child imports them.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.core.csv_safety import neutralise_formula

FORMAT_VERSION: Final = 1

#: Extractor kinds the child knows: structured formats (Phase 2) and the
#: document formats of Phases 3-4 (PDF, images with OCR, EML, ZIP).
ExtractKind = Literal[
    "csv", "tsv", "ofx", "qfx", "qif", "pdf", "png", "jpeg", "tiff", "heic", "eml", "zip", "xlsx"
]
EXTRACT_KINDS: Final[tuple[str, ...]] = (
    "csv",
    "tsv",
    "ofx",
    "qfx",
    "qif",
    "pdf",
    "png",
    "jpeg",
    "tiff",
    "heic",
    "eml",
    "zip",
    "xlsx",
)
#: Image kinds (extracted by OCR).
IMAGE_KINDS: Final[tuple[str, ...]] = ("png", "jpeg", "tiff", "heic")
#: Container kinds whose extraction yields child documents (D4).
CONTAINER_KINDS: Final[tuple[str, ...]] = ("eml", "zip")
#: Error codes that mean "a person must look at this" rather than "broken":
#: the pipeline sends such a document to ``needs_review``, not ``failed``.
REVIEW_ERROR_CODES: Final = frozenset({"encrypted", "depth_limit"})

#: ``Statement.source_format`` values only the worker's document parsers write.
DOCUMENT_STATEMENT_FORMATS: Final = frozenset({"pdf", "image"})
#: Every ``source_format`` only the worker writes: the document parsers' and
#: ``csv`` (brokerage activity tables read by
#: ``app.ingestion.parsers.brokerage_table`` from a stored table). A child
#: answering with any of them is refused (``bad_output``).
WORKER_STATEMENT_FORMATS: Final = DOCUMENT_STATEMENT_FORMATS | frozenset({"csv"})

#: Hard ceilings the models enforce whatever the job's options say.
MAX_ROWS_CEILING: Final = 1_000_000
MAX_COLUMNS_CEILING: Final = 1000
MAX_CELL_CEILING: Final = 65_536
TEXT_MAX: Final = 4096
WARNINGS_MAX: Final = 100
MAX_ABS_AMOUNT: Final = Decimal(10) ** 14
#: Document ceilings (pages, words, rendered pixels, child documents).
MAX_PAGES_CEILING: Final = 2000
MAX_WORDS_PER_PAGE: Final = 20_000
PAGE_TEXT_MAX: Final = 400_000
MAX_PIXELS_CEILING: Final = 200_000_000
MAX_PREVIEW_PNG_BYTES: Final = 8 * 1024 * 1024
MAX_CHILDREN_CEILING: Final = 1000
#: Most bank/card and investment statements one result carries (extractors stop there).
MAX_STATEMENTS: Final = 100
MAX_CHILD_BYTES_CEILING: Final = 1024 * 1024 * 1024
#: Largest coordinate accepted (points or pixels); page sizes beyond are refused.
MAX_COORD: Final = 1_000_000.0


def _bounded_amount(value: Decimal) -> Decimal:
    if not value.is_finite() or abs(value) >= MAX_ABS_AMOUNT:
        raise ValueError("amount out of range")
    return value


Amount = Annotated[Decimal, AfterValidator(_bounded_amount)]
ShortText = Annotated[str, Field(max_length=TEXT_MAX)]
Code = Annotated[str, Field(max_length=64, pattern=r"^[A-Za-z0-9_.:-]*$")]
PageNumber = Annotated[int, Field(ge=1, le=MAX_PAGES_CEILING)]


class _Strict(BaseModel):
    # Bytes (previews, child documents) travel base64-encoded in JSON.
    model_config = ConfigDict(
        extra="forbid", frozen=True, ser_json_bytes="base64", val_json_bytes="base64"
    )


class ExtractOptions(_Strict):
    """Caps applied by the extractors (SECURITY.md C2)."""

    max_rows: int = Field(default=100_000, ge=1, le=MAX_ROWS_CEILING)
    max_columns: int = Field(default=200, ge=1, le=MAX_COLUMNS_CEILING)
    max_cell_chars: int = Field(default=4096, ge=16, le=MAX_CELL_CEILING)
    max_transactions: int = Field(default=100_000, ge=1, le=MAX_ROWS_CEILING)
    # -- documents (PDF, images, OCR; SECURITY.md C2) --------------------------
    #: Pages read from a PDF or multi-frame TIFF; later pages are left out
    #: (``truncated``).
    max_pages: int = Field(default=200, ge=1, le=MAX_PAGES_CEILING)
    #: Objects in a PDF's cross-reference table (a parsing-cost cap).
    max_pdf_objects: int = Field(default=500_000, ge=100, le=10_000_000)
    #: Pixels of a decoded input image (``Image.MAX_IMAGE_PIXELS`` and an
    #: explicit check on the header's size before decoding).
    max_image_pixels: int = Field(default=50_000_000, ge=10_000, le=MAX_PIXELS_CEILING)
    #: Pixels of any page the child renders (previews, OCR); the DPI is
    #: lowered until a page fits.
    max_render_pixels: int = Field(default=12_000_000, ge=10_000, le=MAX_PIXELS_CEILING)
    #: OCR on (images, PDF pages without a usable text layer).
    ocr: bool = True
    #: Tesseract profile: ``auto`` picks ``receipt`` for narrow, tall images.
    ocr_profile: Literal["auto", "document", "receipt"] = "auto"
    ocr_dpi: int = Field(default=300, ge=72, le=600)
    #: Pages OCR'd at most per document (later pages: ``truncated``).
    max_ocr_pages: int = Field(default=50, ge=0, le=MAX_PAGES_CEILING)
    #: Wall-clock budget for one page's recognition, and for all of them.
    ocr_page_seconds: int = Field(default=30, ge=1, le=600)
    ocr_total_seconds: int = Field(default=90, ge=1, le=3600)
    #: Page previews (PNG) for the review UI; at most ``max_preview_pages``.
    previews: bool = True
    preview_dpi: int = Field(default=100, ge=36, le=200)
    max_preview_pages: int = Field(default=50, ge=0, le=MAX_PAGES_CEILING)
    # -- containers (EML, ZIP; C2, C3, D4) --------------------------------------
    #: Nesting depth of the document being extracted: 0 for an upload, 1
    #: for a document that came out of an EML or ZIP. Containers at depth
    #: >= ``max_depth`` are not expanded (``depth_limit``).
    depth: int = Field(default=0, ge=0, le=8)
    max_depth: int = Field(default=1, ge=0, le=1)
    max_children: int = Field(default=100, ge=1, le=MAX_CHILDREN_CEILING)
    max_child_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=MAX_CHILD_BYTES_CEILING)
    #: All children together (also bounded by the runner's output cap: the
    #: bytes travel base64-encoded on stdout).
    max_children_total_bytes: int = Field(
        default=40 * 1024 * 1024, ge=1, le=MAX_CHILD_BYTES_CEILING
    )
    #: Uncompressed/compressed ratio refused for a ZIP member above 1 MiB.
    max_zip_ratio: int = Field(default=100, ge=2, le=10_000)
    #: Characters of an HTML mail body handed to the HTML-to-text converter.
    max_html_chars: int = Field(default=2_000_000, ge=1024, le=50_000_000)


class SandboxLimits(_Strict):
    """Resource limits the child applies to itself before reading its input."""

    memory_bytes: int = Field(default=1024 * 1024 * 1024, ge=32 * 1024 * 1024)
    cpu_seconds: int = Field(default=60, ge=1, le=3600)
    max_open_files: int = Field(default=64, ge=16, le=1024)
    max_file_bytes: int = Field(default=0, ge=0)
    max_input_bytes: int = Field(default=1024 * 1024 * 1024, ge=1)
    #: Refuse to run when the kernel has no Landlock (PERFIN_EXTRACT_REQUIRE_LANDLOCK).
    require_landlock: bool = True


class SandboxJob(_Strict):
    """The child's single command-line argument (JSON)."""

    kind: Annotated[str, Field(max_length=32, pattern=r"^[a-z][a-z0-9_]*$")]
    limits: SandboxLimits = SandboxLimits()
    options: ExtractOptions = ExtractOptions()


class Table(_Strict):
    """A delimited text table, cells as text (formula characters neutralised, C7)."""

    encoding: Code
    delimiter: Annotated[str, Field(min_length=1, max_length=1)]
    has_header: bool
    rows: list[list[Annotated[str, Field(max_length=MAX_CELL_CEILING)]]] = Field(
        max_length=MAX_ROWS_CEILING
    )
    column_count: int = Field(ge=0, le=MAX_COLUMNS_CEILING)
    truncated: bool = False


Coord = Annotated[float, Field(ge=-MAX_COORD, le=MAX_COORD, allow_inf_nan=False)]
Size = Annotated[float, Field(gt=0, le=MAX_COORD, allow_inf_nan=False)]
Confidence = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
WordText = Annotated[str, Field(min_length=1, max_length=512)]
BBox = tuple[Coord, Coord, Coord, Coord]


class Balance(_Strict):
    amount: Amount
    as_of: date | None = None


Confidence01 = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class ParserRef(_Strict):
    """Which document parser (and version) produced a statement or payslip."""

    name: Code
    version: Code
    #: The fake-or-real institution key the parser is written for (None: generic).
    institution: Code | None = None
    kind: Literal["statement", "payslip", "receipt", "tax_form"]


class Reconciliation(_Strict):
    """Opening balance + transactions = closing balance (SECURITY.md C12).

    ``unavailable``: the statement states neither balance and has no running
    balance to check against. Amounts are in the ledger sign convention.
    """

    status: Literal["ok", "mismatch", "unavailable"]
    opening: Amount | None = None
    closing: Amount | None = None
    total: Amount
    difference: Amount | None = None
    tolerance: Amount
    #: Indices of transactions whose running balance disagrees with the
    #: previous balance plus the row's amount.
    running_mismatches: list[int] = Field(default_factory=list, max_length=MAX_ROWS_CEILING)


class SplitLine(_Strict):
    amount: Amount
    category: ShortText | None = None
    memo: ShortText | None = None


class ExtractedTransaction(_Strict):
    posted: date
    amount: Amount
    payee: ShortText | None = None
    memo: ShortText | None = None
    category: ShortText | None = None
    fitid: ShortText | None = None
    check_number: ShortText | None = None
    type: Code | None = None
    cleared: Code | None = None
    user_date: date | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    splits: list[SplitLine] = Field(default_factory=list, max_length=1000)
    # -- set by the worker's document parsers (``app.ingestion.parsers``) -------
    #: Lowest per-field confidence of the row (0..1); None for structured formats.
    confidence: Confidence01 | None = None
    #: Fields whose confidence is below the threshold (the row is imported
    #: with ``review_state = pending`` and a ``low_confidence_field`` item).
    low_confidence: list[Code] = Field(default_factory=list, max_length=20)
    #: Where the row was read: page number and box (page units), for the
    #: review UI's region highlight.
    page: int | None = Field(default=None, ge=1, le=MAX_PAGES_CEILING)
    bbox: BBox | None = None


class StatementAccount(_Strict):
    """Identifying facts; full account numbers never leave the child (only last 4)."""

    account_type: Code | None = None
    last4: Annotated[str, Field(pattern=r"^[0-9]{4}$")] | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    name: ShortText | None = None
    #: The financial institution named by the file (OFX ``FI/ORG``), free text.
    institution: Annotated[str, Field(max_length=200)] | None = None
    #: The account's subtype the document prints (``traditional_ira``,
    #: ``education_529``; ``app.domain.accounts.subtypes``), read by the
    #: worker's document parsers from an account's name or index group.
    subtype: Code | None = None


class Statement(_Strict):
    #: ``ofx``/``qfx``/``qif`` come from the sandbox child; ``pdf``/``image``
    #: statements are written by the worker's document parsers from the
    #: extracted pages (a child answering with one is ``bad_output``); ``csv``
    #: is the cash rows of a brokerage activity table, written by the worker.
    source_format: Literal["ofx", "qfx", "qif", "pdf", "image", "csv"]
    account: StatementAccount = StatementAccount()
    period_start: date | None = None
    period_end: date | None = None
    ledger_balance: Balance | None = None
    available_balance: Balance | None = None
    transactions: list[ExtractedTransaction] = Field(
        default_factory=list, max_length=MAX_ROWS_CEILING
    )
    date_order: Literal["ymd", "mdy", "dmy", "mdy_assumed"] | None = None
    truncated: bool = False
    # -- set by the worker's document parsers (``app.ingestion.parsers``) -------
    #: The opening balance the statement prints (ledger sign convention).
    opening_balance: Balance | None = None
    #: The pages the statement was read from (one account of a document of several).
    first_page: PageNumber | None = None
    last_page: PageNumber | None = None
    parser: ParserRef | None = None
    #: Per-field confidence of the statement's own facts (0..1).
    confidence: dict[Code, Confidence01] = Field(default_factory=dict, max_length=50)
    reconciliation: Reconciliation | None = None


# ---------------------------------------------------------------------------
# Investment (brokerage) statements: OFX ``INVSTMTRS`` from the child, PDF
# holdings and activity sections from the worker's brokerage parser
# (``app.ingestion.parsers.brokerage``; INGESTION.md "Brokerage statements").
# ---------------------------------------------------------------------------

#: Largest quantity or unit price accepted (``NUMERIC(28, 10)``: 18 integer digits).
MAX_ABS_QUANTITY: Final = Decimal(10) ** 18
MAX_POSITIONS: Final = 10_000
#: Longest section heading or summary label carried.
SECTION_LABEL_MAX: Final = 120
MAX_SECTION_CHECKS: Final = 200
MAX_FIGURES: Final = 400


def _bounded_quantity(value: Decimal) -> Decimal:
    if not value.is_finite() or abs(value) >= MAX_ABS_QUANTITY:
        raise ValueError("quantity out of range")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -10:
        value = value.quantize(Decimal("1e-10"))
    return value


Quantity = Annotated[Decimal, AfterValidator(_bounded_quantity)]
InvestmentActivityType = Literal[
    "buy",
    "sell",
    "dividend",
    "interest",
    "fee",
    "split",
    "transfer_in",
    "transfer_out",
    "reinvest",
    "corporate_action",
    "deposit",
    "withdrawal",
]
AssetClassCode = Literal[
    "equity",
    "etf",
    "mutual_fund",
    "bond",
    "cash_equivalent",
    "option",
    "crypto",
    "real_estate",
    "commodity",
    "other",
]


#: Longest security name carried (``security.name`` holds as much).
SECURITY_NAME_MAX: Final = 200


def _security_name(value: str) -> str:
    """Whitespace collapsed and a formula neutralised (C7), as the extractors do."""
    return neutralise_formula(" ".join(value.split()))[:SECURITY_NAME_MAX]


def has_word(text: str | None) -> bool:
    """``text`` holds a letter or a digit (a name that can stand for a security)."""
    return text is not None and any(ch.isalnum() for ch in text)


class SecurityRef(_Strict):
    """How a statement names an instrument (no holder identity, ever).

    A symbol, CUSIP or ISIN identifies it; without any of them the name
    alone does (a holding printed only by its name: an unlisted
    partnership, a 529 portfolio), when it holds a letter or a digit. The
    importer matches such a security by its name
    (``app.domain.investments.statement_import``). Names are bounded,
    whitespace collapsed and formula-neutralised (SECURITY.md C7).
    """

    symbol: Annotated[str, Field(pattern=r"^[A-Za-z0-9.^/:_-]{1,32}$")] | None = None
    cusip: Annotated[str, Field(pattern=r"^[0-9A-Z*@#]{9}$")] | None = None
    isin: Annotated[str, Field(pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")] | None = None
    name: (
        Annotated[str, Field(max_length=SECURITY_NAME_MAX), AfterValidator(_security_name)] | None
    ) = None
    asset_class: AssetClassCode | None = None

    @property
    def identified(self) -> bool:
        """A symbol, CUSIP or ISIN names it (not its name alone)."""
        return bool(self.symbol or self.cusip or self.isin)

    @model_validator(mode="after")
    def _named(self) -> SecurityRef:
        if not (self.identified or has_word(self.name)):
            raise ValueError("a security needs a symbol, CUSIP, ISIN or name")
        return self


class InvestmentPosition(_Strict):
    """One holding as the statement reports it on ``InvestmentStatement.as_of``."""

    security: SecurityRef
    #: Units held (negative for a short position).
    quantity: Quantity
    unit_price: Quantity | None = None
    #: ``unit_price`` is quoted per 100 of face value (a bond: ``98.625`` is
    #: 98.625% of par), so the market value is quantity x price / 100.
    price_per_100: bool = False
    market_value: Amount | None = None
    cost_basis: Amount | None = None
    price_as_of: date | None = None
    # -- set by the worker's document parsers -----------------------------------
    confidence: Confidence01 | None = None
    low_confidence: list[Code] = Field(default_factory=list, max_length=20)
    page: int | None = Field(default=None, ge=1, le=MAX_PAGES_CEILING)
    bbox: BBox | None = None

    @property
    def price_per_unit(self) -> Decimal | None:
        """The price of one unit (``unit_price``, divided by 100 when quoted per 100)."""
        if self.unit_price is None:
            return None
        return self.unit_price / 100 if self.price_per_100 else self.unit_price


class InvestmentActivity(_Strict):
    """One activity row. ``amount`` is the signed cash effect on the account.

    A buy, fee, withdrawal or reinvestment is negative, a sale, dividend,
    interest or deposit positive (``fees`` >= 0 is part of it). ``quantity``
    is in units (positive); a ``split`` carries ``split_ratio`` instead.
    """

    type: InvestmentActivityType
    trade_date: date
    settle_date: date | None = None
    security: SecurityRef | None = None
    quantity: Quantity | None = None
    unit_price: Quantity | None = None
    #: ``unit_price`` is quoted per 100 of face value (a bond trade: ``99.00``
    #: is 99% of par), as :attr:`InvestmentPosition.price_per_100`.
    price_per_100: bool = False
    #: A split's ``(new, old)`` units (``(2, 1)`` for a 2-for-1 split).
    split_ratio: tuple[Quantity, Quantity] | None = None
    amount: Amount
    fees: Amount = Decimal(0)
    fitid: Annotated[str, Field(max_length=255)] | None = None
    memo: ShortText | None = None
    # -- set by the worker's document parsers -----------------------------------
    confidence: Confidence01 | None = None
    low_confidence: list[Code] = Field(default_factory=list, max_length=20)
    page: int | None = Field(default=None, ge=1, le=MAX_PAGES_CEILING)
    bbox: BBox | None = None
    #: The heading of the statement section the row was printed under
    #: (``Deposits``, ``Securities Bought & Sold``), as printed.
    section: Annotated[str, Field(max_length=SECTION_LABEL_MAX)] | None = None
    #: The wording whose suggested kind typed the row, when that wording may
    #: be remembered once a person confirms it (``action:<key>`` /
    #: ``section:<key>``, ``app.ingestion.parsers.vocabulary``): a heading or
    #: a broker's action words, never a payee or a memo.
    suggestion: Annotated[str, Field(max_length=SECTION_LABEL_MAX + 8)] | None = None

    @property
    def price_per_unit(self) -> Decimal | None:
        """The price of one unit (``unit_price``, divided by 100 when quoted per 100)."""
        if self.unit_price is None:
            return None
        return self.unit_price / 100 if self.price_per_100 else self.unit_price


FigureGroup = Literal["account_value", "cash_flow", "income", "realized"]


class StatementFigure(_Strict):
    """One labelled line of a statement's own summary tables (not a row of activity).

    A brokerage statement prints, beside its holdings and activity, tables
    that only restate them: the change in the account's value (``Beginning
    Account Value``, ``Additions``, ``Subtractions``, ``Ending Account
    Value``), the cash flow of its core position, its income by tax
    treatment and its realized gains. They are never positions or activity;
    read as what they are they give the opening and closing figures and a
    second account of every section the activity is checked against
    (``app.ingestion.parsers.brokerage``).
    """

    group: FigureGroup
    label: Annotated[str, Field(max_length=SECTION_LABEL_MAX)]
    #: ``opening``/``closing``: the period's first and last figure; ``total``:
    #: a subtotal of the lines above it; ``item``: one line of the bridge.
    role: Literal["opening", "closing", "total", "item"]
    #: Indented under the line above it (a breakdown of that line, not added again).
    detail: bool = False
    #: The period's figure (None: printed as a dash).
    amount: Amount | None = None
    year_to_date: Amount | None = None
    page: int | None = Field(default=None, ge=1, le=MAX_PAGES_CEILING)
    bbox: BBox | None = None


class SectionCheck(_Strict):
    """The activity rows of one section against the total the statement prints for them.

    ``rows`` index :attr:`InvestmentStatement.activity` and ``computed`` is
    what they add up to (a transfer of units by the value printed).
    ``printed`` is the section's own ``Total ...`` line (``source``
    ``summary``: the cash-flow summary's line for it, when the section
    prints none). ``ok``: they agree within the tolerance; ``mismatch``:
    they do not; ``unchecked``: the statement prints no total for them.
    """

    label: Annotated[str, Field(max_length=SECTION_LABEL_MAX)]
    rows: list[int] = Field(default_factory=list, max_length=MAX_ROWS_CEILING)
    status: Literal["ok", "mismatch", "unchecked"]
    printed: Amount | None = None
    computed: Amount
    difference: Amount | None = None
    #: Where ``printed`` came from: the section's total line or the summary table.
    source: Literal["total", "summary"] | None = None
    page: int | None = Field(default=None, ge=1, le=MAX_PAGES_CEILING)


class InvestmentReconciliation(_Strict):
    """The brokerage checks (SECURITY.md C12; ``app.ingestion.parsers.brokerage``).

    ``positions``: quantity x price = market value for each position;
    ``holdings_total``: the positions add up to the reported holdings value;
    ``cash``: opening cash + activity = closing cash; ``total_value``:
    holdings + closing cash = the reported account value. ``checks`` lists
    those that could run, ``failed`` those that did not hold.
    """

    status: Literal["ok", "mismatch", "unavailable"]
    checks: list[Code] = Field(default_factory=list, max_length=10)
    failed: list[Code] = Field(default_factory=list, max_length=10)
    #: Indices of positions whose quantity x price is not their market value.
    position_mismatches: list[int] = Field(default_factory=list, max_length=MAX_POSITIONS)
    positions_total: Amount | None = None
    reported_positions_total: Amount | None = None
    cash_opening: Amount | None = None
    cash_closing: Amount | None = None
    cash_activity: Amount | None = None
    cash_difference: Amount | None = None
    total_value: Amount | None = None
    reported_total_value: Amount | None = None
    tolerance: Amount
    # -- where a failed check comes from (document parsers) ----------------------
    #: Each section's rows against its printed total (:class:`SectionCheck`).
    sections: list[SectionCheck] = Field(default_factory=list, max_length=MAX_SECTION_CHECKS)
    #: Indices of the activity rows and positions no figure of the statement
    #: vouches for once a check failed: the only rows that carry the
    #: ``reconciliation`` doubt (None: not worked out, every row carries it).
    doubtful_activity: list[int] | None = Field(default=None, max_length=MAX_ROWS_CEILING)
    doubtful_positions: list[int] | None = Field(default=None, max_length=MAX_POSITIONS)
    #: The statement's own cash-flow summary: opening + its lines - closing
    #: (None: no such table, or a figure of it unread).
    summary_cash_difference: Amount | None = None
    #: Cash-flow summary lines no activity row was read for (their labels).
    unread_summary_lines: list[Annotated[str, Field(max_length=SECTION_LABEL_MAX)]] = Field(
        default_factory=list, max_length=MAX_SECTION_CHECKS
    )


class InvestmentStatement(_Strict):
    """A brokerage statement: positions on ``as_of`` and the period's activity.

    ``ofx``/``qfx`` come from the sandbox child; ``pdf``/``image`` from the
    worker's document parsers and ``csv`` from its brokerage activity table
    parser (:data:`WORKER_STATEMENT_FORMATS`, refused in a child's answer).
    """

    source_format: Literal["ofx", "qfx", "pdf", "image", "csv"]
    account: StatementAccount = StatementAccount()
    period_start: date | None = None
    period_end: date | None = None
    #: The day the positions are valued (OFX ``INVPOSLIST``/``DTASOF``).
    as_of: date | None = None
    positions: list[InvestmentPosition] = Field(default_factory=list, max_length=MAX_POSITIONS)
    activity: list[InvestmentActivity] = Field(default_factory=list, max_length=MAX_ROWS_CEILING)
    cash_opening: Balance | None = None
    cash_closing: Balance | None = None
    #: Totals the statement prints (holdings, and holdings plus cash).
    positions_value: Balance | None = None
    total_value: Balance | None = None
    truncated: bool = False
    # -- set by the worker's document parsers -----------------------------------
    #: The statement's own summary tables, line by line (:class:`StatementFigure`).
    figures: list[StatementFigure] = Field(default_factory=list, max_length=MAX_FIGURES)
    #: The pages the statement was read from (one account of a document of several).
    first_page: PageNumber | None = None
    last_page: PageNumber | None = None
    parser: ParserRef | None = None
    confidence: dict[Code, Confidence01] = Field(default_factory=dict, max_length=50)
    reconciliation: InvestmentReconciliation | None = None


class Word(_Strict):
    """One word and its box ``(x0, y0, x1, y1)`` in page units (origin top left)."""

    text: WordText
    bbox: BBox
    line: int = Field(ge=0)
    #: OCR confidence 0..100 (None for a text layer).
    conf: Confidence | None = None


class Line(_Strict):
    text: Annotated[str, Field(max_length=PAGE_TEXT_MAX)]
    bbox: BBox
    #: Index of the line's first word in :attr:`Page.words` and the word count.
    first_word: int = Field(ge=0)
    word_count: int = Field(ge=0)


class LayoutTable(_Strict):
    """A table found on a page: from ruling lines or from aligned columns."""

    source: Literal["rulings", "columns"]
    bbox: BBox
    rows: list[list[Annotated[str, Field(max_length=MAX_CELL_CEILING)]]] = Field(max_length=10_000)
    column_count: int = Field(ge=0, le=MAX_COLUMNS_CEILING)
    #: For ``columns`` tables: the column boundaries (x of each gutter).
    column_bounds: list[Coord] = Field(default_factory=list, max_length=MAX_COLUMNS_CEILING)


class Page(_Strict):
    """One page (PDF) or frame (image) in page units: PDF points or image pixels."""

    number: int = Field(ge=1, le=MAX_PAGES_CEILING)
    width: Size
    height: Size
    unit: Literal["pt", "px"]
    #: Where the words came from: the PDF text layer, OCR, or nothing usable.
    text_source: Literal["text_layer", "ocr", "none"]
    text: Annotated[str, Field(max_length=PAGE_TEXT_MAX)] = ""
    words: list[Word] = Field(default_factory=list, max_length=MAX_WORDS_PER_PAGE)
    lines: list[Line] = Field(default_factory=list, max_length=MAX_WORDS_PER_PAGE)
    tables: list[LayoutTable] = Field(default_factory=list, max_length=100)
    #: Mean OCR word confidence (0..100) for OCR pages.
    ocr_confidence: Confidence | None = None
    ocr_profile: Literal["document", "receipt"] | None = None
    #: Why the text layer was not used (``no_text``, ``low_density``, ``garbage``).
    ocr_reason: Code | None = None
    #: Degrees the page image was turned to straighten it before OCR.
    deskew_degrees: Annotated[float, Field(ge=-45, le=45, allow_inf_nan=False)] | None = None
    words_truncated: bool = False


PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
#: The only PNG chunks a preview may carry (no text, EXIF or other metadata).
PREVIEW_PNG_CHUNKS: Final = frozenset({b"IHDR", b"PLTE", b"IDAT", b"IEND"})


def _plain_png(value: bytes) -> bytes:
    """Refuse anything but a PNG made only of :data:`PREVIEW_PNG_CHUNKS`."""
    if not value.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG")
    offset = len(PNG_SIGNATURE)
    seen_end = False
    while offset < len(value):
        if offset + 12 > len(value) or seen_end:
            raise ValueError("malformed PNG")
        length = int.from_bytes(value[offset : offset + 4], "big")
        chunk = value[offset + 4 : offset + 8]
        if chunk not in PREVIEW_PNG_CHUNKS:
            raise ValueError("PNG chunk not allowed")
        seen_end = chunk == b"IEND"
        offset += 12 + length
    if not seen_end or offset != len(value):
        raise ValueError("malformed PNG")
    return value


PreviewPng = Annotated[
    bytes, Field(min_length=8, max_length=MAX_PREVIEW_PNG_BYTES), AfterValidator(_plain_png)
]


class Preview(_Strict):
    """A rendered page (PNG, no metadata chunks) for the review UI's region highlight.

    ``scale`` maps page units to preview pixels (``px = unit * scale``).
    """

    page: int = Field(ge=1, le=MAX_PAGES_CEILING)
    width: int = Field(ge=1, le=20_000)
    height: int = Field(ge=1, le=20_000)
    dpi: int = Field(ge=1, le=600)
    scale: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)]
    png: PreviewPng


class DocumentMeta(_Strict):
    """Facts about a PDF or image; content flags say what was found and ignored (C8)."""

    page_count: int = Field(default=0, ge=0)
    title: Annotated[str, Field(max_length=500)] | None = None
    author: Annotated[str, Field(max_length=500)] | None = None
    producer: Annotated[str, Field(max_length=500)] | None = None
    creator: Annotated[str, Field(max_length=500)] | None = None
    created: Annotated[str, Field(max_length=64)] | None = None
    encrypted: bool = False
    #: Active content that was present and stripped before anything was read.
    javascript: bool = False
    launch_actions: bool = False
    embedded_files: int = Field(default=0, ge=0)
    links: int = Field(default=0, ge=0)
    forms: bool = False
    image_format: Code | None = None
    exif_stripped: bool = False


Address = Annotated[str, Field(max_length=1000)]


class EmailHeaders(_Strict):
    """Selected headers of an EML (provenance and the intake policy, D1)."""

    from_addr: Address | None = None
    to: list[Address] = Field(default_factory=list, max_length=200)
    cc: list[Address] = Field(default_factory=list, max_length=200)
    delivered_to: list[Address] = Field(default_factory=list, max_length=50)
    reply_to: Address | None = None
    subject: Annotated[str, Field(max_length=2000)] | None = None
    date: Annotated[str, Field(max_length=200)] | None = None
    message_id: Annotated[str, Field(max_length=1000)] | None = None
    authentication_results: list[Annotated[str, Field(max_length=2000)]] = Field(
        default_factory=list, max_length=20
    )


class EmailInfo(_Strict):
    headers: EmailHeaders = EmailHeaders()
    #: The text/plain body, or the HTML body converted to text (D3).
    body_text: Annotated[str, Field(max_length=PAGE_TEXT_MAX)] = ""
    body_source: Literal["plain", "html", "none"] = "none"
    #: Remote resources the HTML referenced; none was fetched (D3).
    remote_resources_blocked: int = Field(default=0, ge=0)
    attachments: int = Field(default=0, ge=0)
    inline_images: int = Field(default=0, ge=0)


class ChildDocument(_Strict):
    """A document found inside an EML or ZIP, handed back for intake (D4).

    ``name`` and ``declared_type`` are metadata only: the intake sniffs the
    bytes and never builds a path from the name (C3).
    """

    origin: Literal["attachment", "inline", "zip_member"]
    name: Annotated[str, Field(max_length=255)] | None = None
    declared_type: Annotated[str, Field(max_length=128)] | None = None
    content_id: Annotated[str, Field(max_length=500)] | None = None
    size: int = Field(ge=0, le=MAX_CHILD_BYTES_CEILING)
    #: Nesting depth the child will be extracted at (parent depth + 1).
    depth: int = Field(ge=1, le=8)
    data: Annotated[bytes, Field(max_length=MAX_CHILD_BYTES_CEILING)]

    @model_validator(mode="after")
    def _size_matches(self) -> ChildDocument:
        if self.size != len(self.data):
            raise ValueError("size does not match the data")
        return self


class StoredPreview(_Strict):
    """Where the worker stored a :class:`Preview` (an encrypted ``blob``; worker-written)."""

    page: int = Field(ge=1, le=MAX_PAGES_CEILING)
    blob_id: uuid.UUID
    width: int = Field(ge=1, le=20_000)
    height: int = Field(ge=1, le=20_000)
    dpi: int = Field(ge=1, le=600)
    scale: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)]


class StoredChild(_Strict):
    """What the worker did with a :class:`ChildDocument` (worker-written)."""

    origin: Literal["attachment", "inline", "zip_member"]
    name: Annotated[str, Field(max_length=255)] | None = None
    size: int = Field(ge=0, le=MAX_CHILD_BYTES_CEILING)
    document_id: uuid.UUID | None = None
    #: Why the intake refused it (sniffed type not allowed, too large, ...).
    rejected: Code | None = None


class PayslipLine(_Strict):
    label: Annotated[str, Field(max_length=200)]
    amount: Amount


class Payslip(_Strict):
    """A payslip read by the worker's payslip parser (worker-written)."""

    employer: Annotated[str, Field(max_length=200)] | None = None
    period_start: date | None = None
    period_end: date | None = None
    pay_date: date | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    gross: Amount | None = None
    net: Amount | None = None
    earnings: list[PayslipLine] = Field(default_factory=list, max_length=100)
    deductions: list[PayslipLine] = Field(default_factory=list, max_length=100)
    total_deductions: Amount | None = None
    #: gross - deductions = net (None: not enough figures to check).
    adds_up: bool | None = None
    confidence: dict[Code, Confidence01] = Field(default_factory=dict, max_length=50)
    low_confidence: list[Code] = Field(default_factory=list, max_length=50)


class LoanReconciliation(_Strict):
    """The loan statement checks (SECURITY.md C12; ``app.ingestion.parsers.loan_statement``).

    ``parts``: principal + interest + escrow + fees = the total paid;
    ``balance``: previous principal balance - principal = new balance.
    ``checks`` lists those that could run, ``failed`` those that did not hold.
    """

    status: Literal["ok", "mismatch", "unavailable"]
    checks: list[Code] = Field(default_factory=list, max_length=10)
    failed: list[Code] = Field(default_factory=list, max_length=10)


class LoanStatement(_Strict):
    """A loan or mortgage statement read by the worker's loan parser (worker-written).

    One payment's split and the principal balance before and after it. No
    loan number (identity data): only its last four digits, for matching.
    """

    lender: Annotated[str, Field(max_length=200)] | None = None
    last4: Annotated[str, Field(pattern=r"^[0-9]{4}$")] | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    statement_date: date | None = None
    paid_on: date | None = None
    principal: Amount | None = None
    interest: Amount | None = None
    escrow: Amount | None = None
    fees: Amount | None = None
    total: Amount | None = None
    previous_balance: Amount | None = None
    balance: Amount | None = None
    #: Annual rate as a fraction (``0.06125``).
    rate: Annotated[Decimal, Field(ge=0, lt=1, max_digits=12, decimal_places=10)] | None = None
    next_due_date: date | None = None
    next_amount_due: Amount | None = None
    reconciliation: LoanReconciliation | None = None
    confidence: dict[Code, Confidence01] = Field(default_factory=dict, max_length=50)
    low_confidence: list[Code] = Field(default_factory=list, max_length=50)


class ReceiptLine(_Strict):
    """One item line of a receipt (best effort)."""

    label: Annotated[str, Field(max_length=200)]
    amount: Amount
    quantity: int | None = Field(default=None, ge=1, le=100_000)
    unit_price: Amount | None = None


class ReceiptPayment(_Strict):
    """How a receipt was paid. ``last4`` is identity data: stored, never logged."""

    method: Code | None = None
    last4: Annotated[str, Field(pattern=r"^[0-9]{4}$")] | None = None


class Receipt(_Strict):
    """A receipt read by the worker's receipt parser (worker-written).

    ``merchant_candidates`` are the short header lines the merchant was
    chosen from (and the mail's sender name), kept so the matcher can look
    them up among the household's payee names and aliases.
    """

    source: Literal["pages", "email_body"] = "pages"
    merchant: Annotated[str, Field(max_length=200)] | None = None
    merchant_candidates: list[Annotated[str, Field(max_length=200)]] = Field(
        default_factory=list, max_length=8
    )
    #: The household payee the merchant was matched to (worker matcher).
    payee_id: uuid.UUID | None = None
    purchase_date: date | None = None
    time: Annotated[str, Field(pattern=r"^[0-2][0-9]:[0-5][0-9]$")] | None = None
    #: The day/month order the date was read in (``guessed``: not visible).
    date_order: Literal["ymd", "mdy", "dmy", "named", "guessed"] | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    subtotal: Amount | None = None
    tax: Amount | None = None
    tip: Amount | None = None
    discount: Amount | None = None
    total: Amount | None = None
    lines: list[ReceiptLine] = Field(default_factory=list, max_length=200)
    payment: ReceiptPayment | None = None
    #: subtotal + tax (+ tip - discount) = total (None: not enough figures).
    adds_up: bool | None = None
    confidence: dict[Code, Confidence01] = Field(default_factory=dict, max_length=50)
    low_confidence: list[Code] = Field(default_factory=list, max_length=50)


class TaxFormRead(_Strict):
    """One tax form read by the worker's tax form parser (worker-written).

    **Amounts only** (``app.ingestion.parsers.tax_forms``): ``boxes`` maps
    stored box keys of the jurisdiction's form set (``"1"``, ``"12_D"``,
    ``"cov_JAN"``) to amounts. Identity boxes (SSN/TIN/EIN, names,
    addresses, account and control numbers) are never read into it; the
    issuer's display name is the only text.
    """

    jurisdiction: Annotated[str, Field(pattern=r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")]
    form_type: Annotated[str, Field(pattern=r"^[A-Z0-9][A-Za-z0-9-]{0,31}$")]
    form_index: int = Field(default=0, ge=0, le=1000)
    tax_year: int | None = Field(default=None, ge=1900, le=2200)
    issuer_name: Annotated[str, Field(max_length=200)] | None = None
    boxes: dict[Annotated[str, Field(pattern=r"^[A-Za-z0-9_]{1,32}$")], Amount] = Field(
        default_factory=dict, max_length=128
    )
    confidence: dict[Code, Confidence01] = Field(default_factory=dict, max_length=50)
    low_confidence: list[Code] = Field(default_factory=list, max_length=50)


class ParserScore(_Strict):
    name: Code
    score: Confidence01


class LineRegion(_Strict):
    """A line on a page (page units), e.g. one a parser skipped."""

    page: PageNumber
    bbox: BBox


#: Skipped lines whose boxes are kept with the parse (review highlight).
MAX_SKIPPED_REGIONS: Final = 50


class ParseInfo(_Strict):
    """What the worker's document parsers did with the stored pages (worker-written).

    The statements they produced are in :attr:`ExtractResult.statements`
    (``source_format`` ``pdf``/``image``); a payslip is here.
    """

    parser: ParserRef | None = None
    #: Every parser's detection score, best first.
    scores: list[ParserScore] = Field(default_factory=list, max_length=100)
    threshold: Confidence01
    #: Statement lines left out: pending (not yet posted) items, and dated
    #: lines whose amount could not be read.
    pending_items: int = Field(default=0, ge=0)
    skipped_lines: int = Field(default=0, ge=0)
    #: Where skipped lines are, when the parser knows (the first ones).
    skipped_regions: list[LineRegion] = Field(default_factory=list, max_length=MAX_SKIPPED_REGIONS)
    payslip: Payslip | None = None
    receipt: Receipt | None = None
    #: A loan or mortgage statement (``app.ingestion.parsers.loan_statement``).
    loan: LoanStatement | None = None
    #: Tax forms (``app.ingestion.parsers.tax_forms``; amounts only).
    tax_forms: list[TaxFormRead] = Field(default_factory=list, max_length=50)
    warnings: list[Code] = Field(default_factory=list, max_length=WARNINGS_MAX)
    #: Action phrases and section headings of activity rows a brokerage
    #: parser could not type (``app.ingestion.parsers.vocabulary.phrase_key``:
    #: normalised, digits masked): what the optional local model may be asked.
    unknown_actions: list[Annotated[str, Field(max_length=SECTION_LABEL_MAX)]] = Field(
        default_factory=list, max_length=MAX_SECTION_CHECKS
    )
    unknown_headings: list[Annotated[str, Field(max_length=SECTION_LABEL_MAX)]] = Field(
        default_factory=list, max_length=MAX_SECTION_CHECKS
    )


class ExtractError(_Strict):
    code: Code
    message: Annotated[str, Field(max_length=500)] = ""


class ExtractResult(_Strict):
    """The child's answer. ``ok=False`` carries an :class:`ExtractError`."""

    format_version: Literal[1] = FORMAT_VERSION
    kind: Annotated[str, Field(max_length=32)]
    ok: bool
    extractor_version: Annotated[str, Field(max_length=32)] = ""
    tables: list[Table] = Field(default_factory=list, max_length=100)
    statements: list[Statement] = Field(default_factory=list, max_length=MAX_STATEMENTS)
    #: Brokerage statements (OFX ``INVSTMTRS``; PDF ones are worker-written).
    investments: list[InvestmentStatement] = Field(default_factory=list, max_length=MAX_STATEMENTS)
    #: Documents (PDF pages, image frames), previews and container contents.
    pages: list[Page] = Field(default_factory=list, max_length=MAX_PAGES_CEILING)
    previews: list[Preview] = Field(default_factory=list, max_length=MAX_PAGES_CEILING)
    meta: DocumentMeta | None = None
    email: EmailInfo | None = None
    children: list[ChildDocument] = Field(default_factory=list, max_length=MAX_CHILDREN_CEILING)
    #: The document's text (pages joined by form feeds, or the mail body).
    text: Annotated[str, Field(max_length=MAX_PAGES_CEILING * 50_000)] = ""
    truncated: bool = False
    warnings: list[Annotated[str, Field(max_length=200)]] = Field(
        default_factory=list, max_length=WARNINGS_MAX
    )
    error: ExtractError | None = None
    #: Set by the worker when it keeps the result (``document.extraction``):
    #: the previews' blobs and the children's documents. A child that
    #: answers with either is refused (``bad_output``).
    stored_previews: list[StoredPreview] = Field(default_factory=list, max_length=MAX_PAGES_CEILING)
    stored_children: list[StoredChild] = Field(
        default_factory=list, max_length=MAX_CHILDREN_CEILING
    )
    #: Set by the worker's document parsers (PDF and image statements,
    #: payslips); like the two above, refused in a child's answer.
    parse: ParseInfo | None = None

    def worker_only_fields(self) -> bool:
        """Whether this result carries anything only the worker may write."""
        return bool(
            self.stored_previews
            or self.stored_children
            or self.parse is not None
            or any(
                s.source_format in WORKER_STATEMENT_FORMATS
                or s.parser is not None
                or s.reconciliation is not None
                or s.first_page is not None
                for s in self.statements
            )
            or any(
                i.source_format in WORKER_STATEMENT_FORMATS
                or i.parser is not None
                or i.reconciliation is not None
                or i.first_page is not None
                or bool(i.confidence)
                or bool(i.figures)
                or any(a.section is not None or a.suggestion is not None for a in i.activity)
                or any(p.confidence is not None or p.low_confidence for p in i.positions)
                or any(a.confidence is not None or a.low_confidence for a in i.activity)
                for i in self.investments
            )
        )

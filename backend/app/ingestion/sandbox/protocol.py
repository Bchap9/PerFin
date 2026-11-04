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

from datetime import date
from decimal import Decimal
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

FORMAT_VERSION: Final = 1

#: Extractor kinds the child knows (Phase 2: structured formats only).
ExtractKind = Literal["csv", "tsv", "ofx", "qfx", "qif"]
EXTRACT_KINDS: Final[tuple[str, ...]] = ("csv", "tsv", "ofx", "qfx", "qif")

#: Hard ceilings the models enforce whatever the job's options say.
MAX_ROWS_CEILING: Final = 1_000_000
MAX_COLUMNS_CEILING: Final = 1000
MAX_CELL_CEILING: Final = 65_536
TEXT_MAX: Final = 4096
WARNINGS_MAX: Final = 100
MAX_ABS_AMOUNT: Final = Decimal(10) ** 14


def _bounded_amount(value: Decimal) -> Decimal:
    if not value.is_finite() or abs(value) >= MAX_ABS_AMOUNT:
        raise ValueError("amount out of range")
    return value


Amount = Annotated[Decimal, AfterValidator(_bounded_amount)]
ShortText = Annotated[str, Field(max_length=TEXT_MAX)]
Code = Annotated[str, Field(max_length=64, pattern=r"^[A-Za-z0-9_.:-]*$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExtractOptions(_Strict):
    """Caps applied by the extractors (SECURITY.md C2)."""

    max_rows: int = Field(default=100_000, ge=1, le=MAX_ROWS_CEILING)
    max_columns: int = Field(default=200, ge=1, le=MAX_COLUMNS_CEILING)
    max_cell_chars: int = Field(default=4096, ge=16, le=MAX_CELL_CEILING)
    max_transactions: int = Field(default=100_000, ge=1, le=MAX_ROWS_CEILING)


class SandboxLimits(_Strict):
    """Resource limits the child applies to itself before reading its input."""

    memory_bytes: int = Field(default=1024 * 1024 * 1024, ge=32 * 1024 * 1024)
    cpu_seconds: int = Field(default=60, ge=1, le=3600)
    max_open_files: int = Field(default=64, ge=16, le=1024)
    max_file_bytes: int = Field(default=0, ge=0)
    max_input_bytes: int = Field(default=1024 * 1024 * 1024, ge=1)


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


class Balance(_Strict):
    amount: Amount
    as_of: date | None = None


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


class StatementAccount(_Strict):
    """Identifying facts; full account numbers never leave the child (only last 4)."""

    account_type: Code | None = None
    last4: Annotated[str, Field(pattern=r"^[0-9]{4}$")] | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    name: ShortText | None = None
    #: The financial institution named by the file (OFX ``FI/ORG``), free text.
    institution: Annotated[str, Field(max_length=200)] | None = None


class Statement(_Strict):
    source_format: Literal["ofx", "qfx", "qif"]
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
    statements: list[Statement] = Field(default_factory=list, max_length=100)
    warnings: list[Annotated[str, Field(max_length=200)]] = Field(
        default_factory=list, max_length=WARNINGS_MAX
    )
    error: ExtractError | None = None

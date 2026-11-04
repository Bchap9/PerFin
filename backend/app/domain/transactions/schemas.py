# SPDX-License-Identifier: AGPL-3.0-or-later
"""Input and output models for the transactions API (API.md "Transactions").

Inputs forbid unknown fields (SECURITY.md F4) and carry money as
``{"amount": "12.34", "currency": "USD"}`` with the amount as a **string**
(:class:`app.core.money.MoneyIn`). Outputs carry money as
:class:`app.core.money.Money`. Free text (``raw_payee``, ``memo``, ``notes``)
is bounded here to the column sizes and control characters are refused, so
nothing longer than the column ever reaches the database.

The list cursor (:func:`encode_cursor` / :func:`decode_cursor`) is an opaque
URL-safe token for the keyset ``(posted_date, id)`` of the last row of a page.
It is not signed: it only says *where* to continue, and every filter
(household, visible accounts, the query parameters) is applied again on the
next page, so a crafted cursor can do no more than skip rows the caller could
list anyway.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import date, datetime
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.core.errors import ProblemError
from app.core.money import Money, MoneyIn
from app.domain.transactions.models import (
    MEMO_MAX,
    NOTES_MAX,
    PAYEE_MAX,
    ReviewState,
    TransactionStatus,
)

#: Page size bounds (API.md: cursor pagination, max 500).
DEFAULT_LIMIT: Final = 100
MAX_LIMIT: Final = 500
#: Most ids one bulk request may name.
BULK_MAX: Final = 500
#: Most values per repeatable filter parameter (``?account=...&account=...``).
FILTER_VALUES_MAX: Final = 50
#: Longest ``q`` search string.
Q_MAX: Final = 100
#: Most tags on one transaction in a request.
TAGS_MAX: Final = 50
#: Most splits on one transaction.
SPLITS_MAX: Final = 100
#: Auto-pairing window bounds, in days.
TRANSFER_WINDOW_DEFAULT: Final = 3
TRANSFER_WINDOW_MAX: Final = 14

# C0/C1 control characters other than tab/newline, plus bidi overrides.
_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    if _CONTROL_RE.search(value):
        raise ValueError("must not contain control characters")
    stripped = value.strip()
    return stripped or None


def _single_line(value: str | None) -> str | None:
    if value is not None and ("\n" in value or "\r" in value):
        raise ValueError("must be a single line")
    return value


PayeeText = Annotated[
    str | None,
    Field(max_length=PAYEE_MAX),
    AfterValidator(_clean_text),
    AfterValidator(_single_line),
]
MemoText = Annotated[str | None, Field(max_length=MEMO_MAX), AfterValidator(_clean_text)]
NotesText = Annotated[str | None, Field(max_length=NOTES_MAX), AfterValidator(_clean_text)]


def _unique_ids(values: list[uuid.UUID]) -> list[uuid.UUID]:
    if len(set(values)) != len(values):
        raise ValueError("ids must be unique")
    return values


def _unique_optional_ids(values: list[uuid.UUID] | None) -> list[uuid.UUID] | None:
    return None if values is None else _unique_ids(values)


TagIds = Annotated[list[uuid.UUID], Field(max_length=TAGS_MAX), AfterValidator(_unique_ids)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class TransactionCreate(_In):
    """A manual transaction. ``amount.currency`` must be the account's currency."""

    account_id: uuid.UUID
    posted_date: date
    effective_date: date | None = None
    amount: MoneyIn
    payee_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    raw_payee: PayeeText = None
    memo: MemoText = None
    notes: NotesText = None
    status: TransactionStatus = TransactionStatus.CLEARED
    tag_ids: TagIds = Field(default_factory=list)


class TransactionUpdate(_In):
    """Fields to change; an absent field is left alone, ``null`` clears it.

    The account and currency of a transaction never change (delete and
    re-create instead). ``tag_ids`` replaces the whole tag set.
    """

    posted_date: date | None = None
    effective_date: date | None = None
    amount: MoneyIn | None = None
    payee_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    raw_payee: PayeeText = None
    memo: MemoText = None
    notes: NotesText = None
    status: TransactionStatus | None = None
    review_state: ReviewState | None = None
    tag_ids: TagIds | None = None

    @model_validator(mode="after")
    def _not_null(self) -> Self:
        for name in ("posted_date", "amount", "status", "review_state", "tag_ids"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class SplitIn(_In):
    amount: MoneyIn
    category_id: uuid.UUID | None = None
    memo: MemoText = None
    tax_flag: bool = False


class SplitSet(_In):
    """Replace a transaction's splits.

    ``splits`` is empty (remove the splits; the transaction is uncategorised
    unless ``category_id`` is given) or holds at least two lines whose amounts,
    in the transaction's currency, add up exactly to the transaction amount.
    """

    splits: list[SplitIn] = Field(max_length=SPLITS_MAX)
    category_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if len(self.splits) == 1:
            raise ValueError("a split needs at least two lines (or none to remove the splits)")
        if self.splits and self.category_id is not None:
            raise ValueError("category_id is only allowed when removing the splits")
        return self


BulkActionName = Literal["categorise", "tag", "untag", "delete", "confirm"]


class BulkAction(_In):
    """One action over many transactions; each id is authorised on its own.

    * ``categorise``: set ``category_id`` (``null`` clears it); split
      transactions are skipped (their categories live on the splits);
    * ``tag`` / ``untag``: add / remove ``tag_ids``;
    * ``delete``: delete (each deletion is audited);
    * ``confirm``: set ``review_state`` to ``confirmed``.
    """

    action: BulkActionName
    ids: Annotated[
        list[uuid.UUID], Field(min_length=1, max_length=BULK_MAX), AfterValidator(_unique_ids)
    ]
    category_id: uuid.UUID | None = None
    tag_ids: TagIds | None = None

    @model_validator(mode="after")
    def _fields_for_action(self) -> Self:
        if self.action == "categorise":
            if "category_id" not in self.model_fields_set:
                raise ValueError("categorise needs category_id (null clears it)")
        elif self.category_id is not None:
            raise ValueError("category_id is only allowed with categorise")
        if self.action in ("tag", "untag"):
            if not self.tag_ids:
                raise ValueError(f"{self.action} needs tag_ids")
        elif self.tag_ids is not None:
            raise ValueError("tag_ids is only allowed with tag or untag")
        return self


class TransferLink(_In):
    """Pair the path transaction with ``counterpart_id`` as one transfer."""

    counterpart_id: uuid.UUID


class AutoPairRequest(_In):
    """Pair unlinked transfers among the accounts the caller may edit.

    ``account_ids`` narrows the accounts (all editable ones by default);
    ``date_from``/``date_to`` bound the posted dates; ``window_days`` is how
    far apart the two legs' dates may be.
    """

    account_ids: Annotated[
        list[uuid.UUID] | None,
        Field(max_length=FILTER_VALUES_MAX),
        AfterValidator(_unique_optional_ids),
    ] = None
    date_from: date | None = None
    date_to: date | None = None
    window_days: int = Field(default=TRANSFER_WINDOW_DEFAULT, ge=0, le=TRANSFER_WINDOW_MAX)

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")
        return self


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


class SplitOut(BaseModel):
    id: uuid.UUID
    category_id: uuid.UUID | None
    amount: Money
    memo: str | None
    tax_flag: bool


class TransferOut(BaseModel):
    """The transfer a transaction belongs to.

    ``counterpart_id`` is the other leg when the caller may see its account,
    else ``null`` (the transfer exists, the hidden account is not revealed).
    """

    group_id: uuid.UUID
    counterpart_id: uuid.UUID | None


class TransactionOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    posted_date: date
    effective_date: date | None
    amount: Money
    payee_id: uuid.UUID | None
    category_id: uuid.UUID | None
    raw_payee: str | None
    memo: str | None
    notes: str | None
    status: TransactionStatus
    review_state: ReviewState
    confidence: str | None
    transfer: TransferOut | None
    source_document_id: uuid.UUID | None
    tag_ids: list[uuid.UUID]
    splits: list[SplitOut]
    created_at: datetime
    updated_at: datetime


class TransactionPage(BaseModel):
    items: list[TransactionOut]
    next_cursor: str | None


BulkResultName = Literal["updated", "deleted", "unchanged", "skipped", "not_found"]


class BulkItemResult(BaseModel):
    id: uuid.UUID
    result: BulkResultName
    reason: str | None = None


class BulkResult(BaseModel):
    action: BulkActionName
    results: list[BulkItemResult]
    changed: int


class TransferPair(BaseModel):
    group_id: uuid.UUID
    transaction_ids: list[uuid.UUID]


class AutoPairResult(BaseModel):
    paired: list[TransferPair]
    ambiguous: int


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------

_CURSOR_MAX: Final = 128
_CURSOR_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def transaction_not_found() -> ProblemError:
    return ProblemError(404, "transaction-not-found", "Transaction not found")


def invalid_cursor() -> ProblemError:
    return ProblemError(422, "invalid-cursor", "Invalid cursor", "Start again without a cursor.")


def encode_cursor(posted_date: date, row_id: uuid.UUID) -> str:
    raw = f"{posted_date.isoformat()}|{row_id}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token: str) -> tuple[date, uuid.UUID]:
    """``(posted_date, id)`` from a cursor, or ``422 invalid-cursor``."""
    if len(token) > _CURSOR_MAX or not _CURSOR_RE.fullmatch(token):
        raise invalid_cursor()
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("ascii")
        day, _, ident = raw.partition("|")
        return date.fromisoformat(day), uuid.UUID(ident)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise invalid_cursor() from None


# ---------------------------------------------------------------------------
# Worker job
# ---------------------------------------------------------------------------

#: Job type of the after-import transfer matcher (``app.workers.handlers.transfers``).
AUTO_PAIR_JOB_TYPE: Final = "transactions.auto_pair"


class AutoPairJob(_In):
    """Payload of :data:`AUTO_PAIR_JOB_TYPE`: the seeds of one matcher run (household-wide)."""

    source_document_id: uuid.UUID | None = None
    account_ids: Annotated[
        list[uuid.UUID] | None,
        Field(max_length=FILTER_VALUES_MAX),
        AfterValidator(_unique_optional_ids),
    ] = None
    date_from: date | None = None
    date_to: date | None = None
    window_days: int = Field(default=TRANSFER_WINDOW_DEFAULT, ge=0, le=TRANSFER_WINDOW_MAX)

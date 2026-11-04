# SPDX-License-Identifier: AGPL-3.0-or-later
"""Review queue API models (API.md ``/review``).

A resolution is a typed body whose ``kind`` must equal the item's kind
(``422 review-kind-mismatch`` otherwise):

* ``possible_duplicate``: ``{"kind", "decision": "confirm" | "reject",
  "duplicate_of"?}``. For a file whose content is already in the household
  (payload ``reason = same_content``) *confirm* marks the new document
  ``rejected`` (it stays in quarantine and can be deleted) and *reject*
  ("not a duplicate") sends it through the pipeline. For an imported row
  that resembles an existing one (``reason = near_match``; payload
  ``transaction_id`` and ``candidates``) *confirm* deletes the imported row
  and records its hash as an alias of the kept transaction (``duplicate_of``,
  required when there are several candidates); *reject* keeps it and moves
  it from ``pending`` to ``confirmed``.
* ``unknown_account``: ``{"kind", "account_id", "remember"}``: the document
  is imported into that account (an account the caller edits). With
  ``remember``, the statement's last four digits are saved on the account
  (OFX/QIF) or the account on the CSV mapping, so the next file of that
  account is placed without asking.
* ``unparsed``: ``{"kind", "action": "reprocess"}`` runs the whole pipeline
  again; ``{"kind", "action": "apply_mapping", "mapping_id"?, "account_id"?}``
  re-imports the stored extraction with a saved column mapping (the mapping
  editor works on ``GET /imports/{document_id}/preview``; ``next_step`` is
  ``column_mapping`` for such items).

* ``unknown_owner`` (a drop-folder file nobody could be found for, held in
  ``needs_review``): ``{"kind", "user_id", "account_id"?, "remember"}``
  assigns the document to that member (a member, admin or owner; the
  account, if given, one the caller edits and the member may import into)
  and sends it through the pipeline. ``remember`` routes the item's
  subfolder (payload ``folder``) to that member and account.

* ``receipt_match`` ("attach this receipt to ...": a receipt the matcher
  could not link with confidence; payload ``reason`` and ranked
  ``suggestions`` of ``{transaction_id, account_id, score, amount, days,
  already_linked}``): ``{"kind", "transaction_id"}`` attaches the receipt to
  that transaction (one the caller edits; any transaction, not only a
  suggested one) as a ``document_link``; ``{"kind", "transaction_id":
  null}`` records that no transaction applies (a cash purchase, a gift).

* ``low_confidence_field`` (rows or figures a parser was unsure of, a
  statement that does not reconcile): ``{"kind", "transactions": [{"transaction_id",
  "posted_date"?, "amount"?, "raw_payee"?, "memo"?}], "confirm"}`` corrects the
  named rows (only rows imported from the item's document, ones the caller
  edits) and, with ``confirm`` (default), moves the item's pending rows to
  ``confirmed`` (for ``reconciliation_*`` items: every pending row of the
  document on the item's account).

Other kinds can only be dismissed for now (``409
review-resolution-unsupported``).

Review tools next to the resolutions (``app.domain.review.actions``):
``POST /review/{id}/correct`` (:class:`Correction`: document institution,
account, period, tax year, kind and row values), ``POST
/review/{id}/remember-layout`` (:class:`RememberLayout`) and ``GET
/review/{id}/rule-draft`` (:class:`RuleDraftOut`). ``GET /review/{id}`` adds
``regions`` and ``alternatives`` (:class:`Region`, :class:`Alternative`);
the list leaves them ``null``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.money import MoneyIn
from app.domain.documents.models import DocumentKind, ReviewItemState, ReviewKind
from app.domain.rules.schemas import RuleCreate
from app.domain.transactions.schemas import MemoText, PayeeText
from app.ingestion.classify.fingerprints import MatchSpec

#: ``[x0, y0, x1, y1]`` in page units. A list of exactly four numbers rather
#: than a tuple: openapi-fetch's response typing does not keep tuple types,
#: so a tuple made the generated client types disagree with themselves.
Bbox = Annotated[list[float], Field(min_length=4, max_length=4)]

MAX_PAGE: Final = 500
DEFAULT_PAGE: Final = 100
#: Most rows one correction or resolution may change.
FIXES_MAX: Final = 200
#: Most regions and alternatives on one item.
REGIONS_MAX: Final = 50
ALTERNATIVES_MAX: Final = 50

NextStep = Literal[
    "confirm_duplicate",
    "choose_account",
    "column_mapping",
    "reprocess",
    "choose_owner",
    "attach_receipt",
    "correct_fields",
]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolveDuplicate(_Input):
    kind: Literal["possible_duplicate"]
    decision: Literal["confirm", "reject"]
    #: The existing transaction the imported row duplicates (one of the
    #: item's ``candidates``; needed only when there are several).
    duplicate_of: uuid.UUID | None = None


class ResolveUnknownAccount(_Input):
    kind: Literal["unknown_account"]
    account_id: uuid.UUID
    remember: bool = False


class ResolveUnparsed(_Input):
    kind: Literal["unparsed"]
    action: Literal["reprocess", "apply_mapping"]
    mapping_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None


class ResolveUnknownOwner(_Input):
    """A held drop-folder file: who it belongs to (and, optionally, its account)."""

    kind: Literal["unknown_owner"]
    user_id: uuid.UUID
    account_id: uuid.UUID | None = None
    #: Route the file's subfolder to this member/account from now on.
    remember: bool = False


class ResolveReceiptMatch(_Input):
    """Attach the receipt to a transaction (``null``: none applies)."""

    kind: Literal["receipt_match"]
    transaction_id: uuid.UUID | None


class TransactionFix(_Input):
    """New values for one imported row; absent fields are left alone."""

    transaction_id: uuid.UUID
    posted_date: date | None = None
    amount: MoneyIn | None = None
    raw_payee: PayeeText = None
    memo: MemoText = None

    @model_validator(mode="after")
    def _not_null(self) -> Self:
        for name in ("posted_date", "amount"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class ResolveLowConfidence(_Input):
    """Correct the doubtful rows and (``confirm``) accept the item's pending rows."""

    kind: Literal["low_confidence_field"]
    transactions: list[TransactionFix] = Field(default_factory=list, max_length=FIXES_MAX)
    confirm: bool = True


ReviewResolution = Annotated[
    ResolveDuplicate
    | ResolveUnknownAccount
    | ResolveUnparsed
    | ResolveUnknownOwner
    | ResolveReceiptMatch
    | ResolveLowConfidence,
    Field(discriminator="kind"),
]


class DocumentFix(_Input):
    """Corrected document facts; ``null`` clears institution, period and tax year."""

    institution_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    period_start: date | None = None
    period_end: date | None = None
    tax_year: int | None = Field(default=None, ge=1900, le=2200)
    kind: DocumentKind | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        for name in ("account_id", "kind"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        if (
            self.period_start is not None
            and self.period_end is not None
            and self.period_start > self.period_end
        ):
            raise ValueError("period_start is after period_end")
        return self


class Correction(_Input):
    """``POST /review/{id}/correct``: fix the item's document and rows (``resolve`` closes it)."""

    document: DocumentFix | None = None
    transactions: list[TransactionFix] = Field(default_factory=list, max_length=FIXES_MAX)
    resolve: bool = False


ParserName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class RememberLayout(_Input):
    """``POST /review/{id}/remember-layout``: a household fingerprint from this document.

    ``match`` (the fingerprint rules, the same schema as a shipped
    fingerprint's ``match``) is derived from the document's header and
    table layout when absent; ``kind`` defaults to the document's and
    ``parser`` to the one that read it (else the generic parser of the kind).
    """

    institution_id: uuid.UUID
    kind: DocumentKind | None = None
    parser: ParserName | None = None
    match: MatchSpec | None = None
    reprocess_similar: bool = True


class Region(BaseModel):
    """A box on a page (page units, origin top left) the review UI highlights."""

    page: int
    bbox: Bbox
    label: Literal["transaction", "account_number", "page"]
    transaction_id: uuid.UUID | None = None
    fields: list[str] = Field(default_factory=list)


class Alternative(BaseModel):
    """A value the caller may pick for ``field`` (``value`` as a string or id)."""

    field: str
    value: str
    label: str | None = None
    score: float | None = None
    transaction_id: uuid.UUID | None = None
    source: Literal["document", "ledger", "household"]


class ReviewItemOut(BaseModel):
    """One review item. ``payload`` carries ids and short facts, never file content.

    ``next_step`` names what the UI should offer; ``can_resolve`` whether the
    caller may resolve or dismiss it.
    """

    id: uuid.UUID
    kind: ReviewKind
    state: ReviewItemState
    document_id: uuid.UUID | None
    payload: dict[str, Any]
    resolution: dict[str, Any] | None
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    created_at: datetime
    next_step: NextStep | None
    can_resolve: bool
    #: ``GET /review/{id}`` only (``null`` in lists).
    regions: list[Region] | None = None
    alternatives: list[Alternative] | None = None


class ReviewPage(BaseModel):
    """Oldest first (a queue); ``next_cursor`` is ``null`` on the last page."""

    items: list[ReviewItemOut]
    next_cursor: uuid.UUID | None


class RememberLayoutOut(BaseModel):
    """The stored fingerprint and what was sent through the pipeline again."""

    fingerprint_id: uuid.UUID
    version: int
    institution_id: uuid.UUID
    kind: DocumentKind
    parser: str
    match: dict[str, Any]
    #: Another fingerprint of a different institution matches the document
    #: as strongly: classification stays undecided until the rules differ.
    ambiguous: bool
    reprocessed: list[uuid.UUID]
    item: ReviewItemOut


class RuleDraftOut(BaseModel):
    """A pre-filled rule (not saved) for ``POST /rules``, from one of the item's rows.

    The schema is the request body's (``RuleCreate``): the client sends
    ``rule`` back to ``POST /rules`` as it is, so OpenAPI keeps a single
    ``RuleCreate`` instead of separate input and output variants.
    """

    model_config = ConfigDict(json_schema_mode_override="validation")

    item_id: uuid.UUID
    transaction_id: uuid.UUID
    rule: RuleCreate


class RegionWord(BaseModel):
    bbox: Bbox
    line: int
    #: The word itself only for callers who may see the document's content (I1).
    text: str | None
    conf: float | None


class PreviewInfo(BaseModel):
    width: int
    height: int
    dpi: int
    #: Page units to preview pixels (``px = unit * scale``).
    scale: float


class DocumentRegions(BaseModel):
    """``GET /documents/{id}/regions?page=``: word boxes of one page."""

    document_id: uuid.UUID
    page: int
    page_count: int
    width: float
    height: float
    unit: Literal["pt", "px"]
    text_source: Literal["text_layer", "ocr", "none"]
    text_included: bool
    words_truncated: bool
    preview: PreviewInfo | None
    words: list[RegionWord]

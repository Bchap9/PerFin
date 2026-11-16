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
  document on the item's account). ``{"kind", "import_as_read": true}``
  answers a brokerage statement, or the lines of one, held back because
  its figures do not add up (``investment_reconciliation_failed``, or
  ``investment_low_confidence`` / ``investment_lines_held`` with
  ``reconciliation`` its only doubt; ``422`` for any other item, or
  with ``transactions``): the statement is imported again as printed into
  the item's account (an account the caller edits), without a balance
  snapshot.

Other kinds can only be dismissed for now (``409
review-resolution-unsupported``).

Review tools next to the resolutions (``app.domain.review.actions``):
``POST /review/{id}/correct`` (:class:`Correction`: document institution,
account, period, tax year, kind and row values), ``POST
/review/{id}/remember-layout`` (:class:`RememberLayout`) and ``GET
/review/{id}/rule-draft`` (:class:`RuleDraftOut`). ``GET /review/{id}`` adds
``regions`` and ``alternatives`` (:class:`Region`, :class:`Alternative`);
the list leaves them ``null``. "Teach PerFin" (``POST /documents/{id}/teach``,
``app.domain.review.teach``) takes a :class:`TeachRequest` and answers a
:class:`TeachOut`.

The reconcile view (``GET /documents/{id}/reconciliation`` and its four
actions; ``app.domain.review.reconciliation``) reads a
:class:`DocumentReconciliation` and answers a :class:`ReconcileOutcome`;
``GET /review/cards`` groups the open items by document
(:class:`ReviewCardPage`).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.money import AmountIn, MoneyIn
from app.domain.documents.models import (
    DocumentKind,
    DocumentSource,
    DocumentStatus,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.schemas import Verification
from app.domain.rules.schemas import RuleCreate
from app.domain.transactions.models import ReviewState
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


#: A balance as a person reads it off the page (``1,204.18``, ``$12.00``):
#: read by ``reconciliation.read_printed``, never stored as typed.
PrintedAmount = Annotated[str, Field(min_length=1, max_length=32)]
#: Most near-duplicate rows one resolution may decide.
DECISIONS_MAX: Final = 50

#: The one thing a document's card asks for next (``GET /review/cards``).
CardAction = Literal[
    "choose_account",
    "map_columns",
    "answer_sign",
    "reconcile",
    "check_rows",
    "confirm_duplicate",
    "attach_receipt",
    "choose_owner",
    "teach",
    "reprocess",
    "acknowledge",
]
#: A likely cause of a statement's difference (``app.domain.review.causes``).
CauseKind = Literal[
    "extra_row",
    "sign_flipped",
    "transposed_digits",
    "fee_or_interest",
    "opening_differs_from_previous",
    "duplicated_row",
    "out_of_period",
    "earlier_reading",
    "missing_row",
]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DuplicateDecision(_Input):
    """What to do with one imported row of a ``near_matches`` item."""

    transaction_id: uuid.UUID
    decision: Literal["confirm", "reject"]
    #: The existing transaction it duplicates (needed with several candidates).
    duplicate_of: uuid.UUID | None = None


class ResolveDuplicate(_Input):
    kind: Literal["possible_duplicate"]
    decision: Literal["confirm", "reject"]
    #: The existing transaction the imported row duplicates (one of the
    #: item's ``candidates``; needed only when there are several).
    duplicate_of: uuid.UUID | None = None
    #: One decision per listed row of an item that names several
    #: (``reason = near_matches``); absent: ``decision`` applies to all.
    rows: list[DuplicateDecision] | None = Field(default=None, max_length=DECISIONS_MAX)


class ResolveUnknownAccount(_Input):
    kind: Literal["unknown_account"]
    account_id: uuid.UUID
    remember: bool = False


class ResolveUnparsed(_Input):
    kind: Literal["unparsed"]
    action: Literal["reprocess", "apply_mapping", "answer_sign"]
    mapping_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    #: With ``answer_sign`` (an item held because the sign of its amounts
    #: was taken on trust): what a positive amount of the file means.
    positive_is: Literal["money_in", "money_out"] | None = None


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


class LineFix(_Input):
    """Corrected values for one held activity line of a brokerage statement.

    ``line`` is the line's ``index`` in the item's payload; absent fields
    are left as read.
    """

    line: int = Field(ge=0)
    trade_date: date | None = None
    amount: MoneyIn | None = None
    quantity: MoneyIn | None = None
    type: Annotated[str, Field(pattern=r"^[a-z_]{1,32}$")] | None = None


class ResolveLowConfidence(_Input):
    """Correct the doubtful rows and (``confirm``) accept the item's pending rows."""

    kind: Literal["low_confidence_field"]
    transactions: list[TransactionFix] = Field(default_factory=list, max_length=FIXES_MAX)
    confirm: bool = True
    #: Import the item's held brokerage statement as printed (module doc).
    import_as_read: bool = False
    #: With ``import_as_read`` on an item whose lines may be answered one by
    #: one (payload ``selectable``): the lines to import, by their ``index``
    #: in the payload's ``lines``. The other lines it lists are left out for
    #: good. Omitted: every line.
    lines: list[Annotated[int, Field(ge=0)]] | None = Field(default=None, max_length=2000)
    #: Corrections to held brokerage lines, applied before they are imported.
    line_fixes: list[LineFix] = Field(default_factory=list, max_length=FIXES_MAX)

    @model_validator(mode="after")
    def _as_read_alone(self) -> Self:
        if self.import_as_read and self.transactions:
            raise ValueError("import_as_read takes no transactions")
        if self.lines is not None and not self.import_as_read:
            raise ValueError("lines goes with import_as_read")
        return self


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
    """Corrected document facts; ``null`` clears institution, period and tax year.

    ``opening_balance``/``closing_balance`` correct the balances of one
    statement of the document's stored extraction (``statement``: its index;
    default the item's statement, else the document's only one, else the one
    of the item's account), which is then reconciled again: a statement held
    because it did not add up (``reconciliation_failed``/``_unavailable``)
    is accepted once it does.
    """

    institution_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    period_start: date | None = None
    period_end: date | None = None
    tax_year: int | None = Field(default=None, ge=1900, le=2200)
    kind: DocumentKind | None = None
    opening_balance: AmountIn | None = None
    closing_balance: AmountIn | None = None
    statement: int | None = Field(default=None, ge=0, le=10_000)

    @model_validator(mode="after")
    def _check(self) -> Self:
        for name in ("account_id", "kind", "opening_balance", "closing_balance"):
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
    #: Drop what was taught for the remembered layout of this institution
    #: (kept when the same layout is remembered again; ``409
    #: layout-conflict`` when it is another layout).
    replace: bool = False


class Region(BaseModel):
    """A box on a page (page units, origin top left) the review UI highlights."""

    page: int
    bbox: Bbox
    label: Literal["transaction", "account_number", "page", "skipped_line"]
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


class ReviewDocument(BaseModel):
    """The document an item is about, so the queue can name and show it.

    ``previewable``: the caller may see its pages (``GET
    /documents/{id}/preview``: a PDF or image out of quarantine, member or
    above, or a viewer where the household allows downloads).
    """

    id: uuid.UUID
    original_filename: str | None
    kind: DocumentKind
    status: DocumentStatus
    mime_detected: str
    source: DocumentSource
    created_at: datetime
    period_start: date | None
    period_end: date | None
    previewable: bool


class ReviewItemOut(BaseModel):
    """One review item. ``payload`` carries ids and short facts, never file content.

    ``next_step`` names what the UI should offer; ``can_resolve`` whether the
    caller may resolve or dismiss it. ``reason`` is ``payload.reason`` (what
    raised it); ``document`` names the item's document.
    """

    id: uuid.UUID
    kind: ReviewKind
    state: ReviewItemState
    document_id: uuid.UUID | None
    document: ReviewDocument | None = None
    reason: str | None = None
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


class ReviewCard(BaseModel):
    """A document's open items as one card, with the one thing to do next.

    ``action_item_id`` is the item that asks for ``action``; ``open`` counts
    the items that wait on a person, ``notices`` the rest.
    """

    document: ReviewDocument
    items: list[ReviewItemOut]
    action: CardAction
    action_item_id: uuid.UUID
    open: int
    notices: int


class ReviewCardPage(BaseModel):
    """``GET /review/cards``: cards by document; ``loose`` are open items of no document.

    ``next_cursor`` (a document id) is ``null`` on the last page; ``loose``
    is filled on the first page only.
    """

    cards: list[ReviewCard]
    loose: list[ReviewItemOut]
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
    #: The earlier version whose taught template was kept, and what of it.
    merged_from: int | None = None
    kept: list[str] = Field(default_factory=list)


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


# ---------------------------------------------------------------------------
# Teach PerFin (``POST /documents/{id}/teach``; ``app.domain.review.teach``)
# ---------------------------------------------------------------------------

#: What a selection on the page is. ``other`` carries a free-text ``note``
#: (kept for the household, never interpreted); ``ignore`` marks a line
#: never to read.
TeachField = Literal[
    "account_number",
    "account_name",
    "period_start",
    "period_end",
    "opening_balance",
    "closing_balance",
    "institution",
    "ignore",
    "other",
]
#: A column's role in a labelled table (``other``: described in ``note``, not read).
TeachRole = Literal[
    "ignore",
    "other",
    "date",
    "payee",
    "memo",
    "amount",
    "debit",
    "credit",
    "balance",
    "symbol",
    "security",
    "quantity",
    "price",
    "market_value",
    "cost_basis",
]
TeachNote = Annotated[str, Field(min_length=1, max_length=500)]
TEACH_LABELS_MAX: Final = 40
#: Up to four transactions tables and one holdings table.
TEACH_TABLES_MAX: Final = 5
#: Most rows a teach preview lists (``row_count`` has the full number).
TEACH_PREVIEW_ROWS_MAX: Final = 50


TeachDirection = Literal["as_printed", "money_in", "money_out", "card"]


# ``direction``, ``continues`` and ``preview`` default through a factory (to
# ``as_printed``, ``False`` and ``False``): a default written into the schema
# makes the generated client type require the field, and a request written
# before these fields existed must still type-check and mean what it meant.
def _as_printed() -> TeachDirection:
    return "as_printed"


def _off() -> bool:
    return False


class TeachLabel(_Input):
    """One selection (a word clicked or a box dragged on a page) and what it is."""

    field: TeachField
    page: int = Field(ge=1, le=10_000)
    bbox: Bbox
    #: The value as the person reads it, when the page's text is wrong or
    #: unreadable (a date ``YYYY-MM-DD``, an amount, the account's digits).
    value: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    note: TeachNote | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.field == "other" and not (self.note or "").strip():
            raise ValueError("describe what an 'other' selection is in note")
        return self


class TeachColumn(_Input):
    """A column of a labelled table: its horizontal span (page units) and role."""

    x0: float = Field(ge=-1.0, le=100_000.0)
    x1: float = Field(ge=-1.0, le=100_000.0)
    role: TeachRole
    note: TeachNote | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.x1 <= self.x0:
            raise ValueError("a column ends after it starts")
        if self.role == "other" and not (self.note or "").strip():
            raise ValueError("describe what an 'other' column is in note")
        return self


class TeachTable(_Input):
    """A table selected on a page: the box starts at its header line.

    ``direction`` says what a single ``amount`` column means: as printed
    (signed on the page), all money in, all money out, or a card's charges
    printed positive. ``continues``: the table runs on over later pages
    that do not repeat its header.
    """

    kind: Literal["transactions", "holdings"]
    page: int = Field(ge=1, le=10_000)
    bbox: Bbox
    columns: list[TeachColumn] = Field(min_length=2, max_length=20)
    direction: TeachDirection = Field(default_factory=_as_printed)
    continues: bool = Field(default_factory=_off)


class TeachRequest(_Input):
    """``POST /documents/{id}/teach``: what the person labelled on the document.

    The labels are applied to this document at once (period, institution,
    account, balances, the labelled table re-imported); with ``remember``
    the layout is also kept as a household fingerprint with the labels'
    anchors (label text and direction, never the values), and this document
    and similar ones are read again with it. ``item_id`` names the review
    item the teaching answers (closed with ``resolve``). With ``preview``
    nothing is applied or kept: the answer says what would be read
    (:class:`TeachPreview`).
    """

    labels: list[TeachLabel] = Field(default_factory=list, max_length=TEACH_LABELS_MAX)
    tables: list[TeachTable] = Field(default_factory=list, max_length=TEACH_TABLES_MAX)
    institution_id: uuid.UUID | None = None
    institution_name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    account_id: uuid.UUID | None = None
    remember: bool = False
    reprocess_similar: bool = True
    #: With ``remember``: start the remembered template afresh instead of
    #: adding to what was taught for this layout before (``409
    #: layout-conflict`` asks for it when another layout of the institution
    #: was taught).
    replace: bool = False
    item_id: uuid.UUID | None = None
    resolve: bool = True
    preview: bool = Field(default_factory=_off)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.labels and not self.tables and not self.remember:
            raise ValueError("label something on the page, or remember the layout")
        if self.institution_id is not None and self.institution_name is not None:
            raise ValueError("give institution_id or institution_name, not both")
        return self


class TaughtField(BaseModel):
    """One label as PerFin understood it."""

    field: str
    #: What the selection reads as (ISO date, amount, last four digits,
    #: text); ``null`` when it could not be read.
    value: str | None
    page: int
    #: The label text that finds the value again on the next statement
    #: (``null``: none next to it, so it applies to this document only).
    anchor: str | None = None
    direction: Literal["right", "below", "band"] | None = None
    note: str | None = None


class TaughtColumn(BaseModel):
    header: str
    role: str


class TaughtTable(BaseModel):
    kind: Literal["transactions", "holdings"]
    #: Rows read from the whole document with this table's columns.
    rows: int
    columns: list[TaughtColumn]


class TaughtRow(BaseModel):
    """One row a taught table reads (``payee`` only for callers who may see content)."""

    posted_date: date
    amount: str
    payee: str | None
    #: Index of the taught table in the request's ``tables``.
    table: int
    page: int


class TeachPreview(BaseModel):
    """What the labels would read, before anything is saved."""

    rows: list[TaughtRow] = Field(max_length=TEACH_PREVIEW_ROWS_MAX)
    row_count: int
    reconciliation: Literal["ok", "mismatch", "unavailable"] | None
    difference: str | None
    #: Other documents of this layout that wait and would be read again.
    similar_waiting: int
    account_id: uuid.UUID | None


class TeachOut(BaseModel):
    """What was applied to the document and what was remembered."""

    document_id: uuid.UUID
    fields: list[TaughtField]
    tables: list[TaughtTable]
    ignored: int
    notes: list[str]
    #: Document facts changed (``period_start``, ``institution_id``...).
    applied: list[str]
    #: ``import``: the labelled rows are being imported; ``pipeline``: the
    #: document is read again with the remembered layout.
    rerun: Literal["import", "pipeline"] | None = None
    job_id: uuid.UUID | None = None
    fingerprint_id: uuid.UUID | None = None
    reprocessed: list[uuid.UUID] = Field(default_factory=list)
    #: The earlier version of the remembered layout whose teaching was kept
    #: (the same layout taught again), and what of it is still remembered
    #: (field names, ``table:<kind>``).
    merged_from: int | None = None
    kept: list[str] = Field(default_factory=list)
    #: Short codes: ``no_label:<field>`` (no label next to the value, it is
    #: not remembered), ``unreadable:<field>``, ``rows_already_imported``,
    #: ``does_not_reconcile``, ``ignore_not_rememberable``,
    #: ``item_left_open`` (the review item is not settled by what was taught)...
    warnings: list[str] = Field(default_factory=list)
    item: ReviewItemOut | None = None
    #: Set, and nothing applied, when the request asked for a preview.
    preview: TeachPreview | None = None


# ---------------------------------------------------------------------------
# The reconcile view (``app.domain.review.reconciliation``)
# ---------------------------------------------------------------------------

STATEMENT_INDEX_MAX: Final = 10_000


class ReconcileRow(BaseModel):
    """A ledger row that counts towards a statement's sum.

    ``origin``: ``read`` from this document, ``added`` by a person for it,
    ``matched`` when the row first came from another file. ``edited``: it
    was changed after its import. ``page``/``bbox``: where the document
    prints it, when known.
    """

    transaction_id: uuid.UUID
    posted_date: date
    payee: str | None
    amount: str
    review_state: ReviewState
    origin: Literal["read", "added", "matched"]
    edited: bool
    in_period: bool
    page: int | None
    bbox: Bbox | None


class RemovedRow(BaseModel):
    """An imported row a person deleted (its tombstone); it can be put back."""

    tombstone_id: uuid.UUID
    posted_date: date | None
    amount: str | None
    deleted_at: datetime


class ReconcileCause(BaseModel):
    """A likely reason for the difference, most likely first."""

    kind: CauseKind
    transaction_ids: list[uuid.UUID]
    amount: str | None
    page: int | None
    bbox: Bbox | None


class StatementReconciliation(BaseModel):
    """One statement of a document against the ledger rows it produced.

    ``opening``, ``closing`` and ``previous_closing`` are as printed
    (``printed_sign = owed``: a balance owed is positive on paper); ``total``
    and ``difference`` (``opening + total - closing``) are in ledger terms.
    ``status`` is ``unchecked`` without both balances.
    """

    statement: int
    account_id: uuid.UUID
    account_name: str
    currency: str
    printed_sign: Literal["ledger", "owed"]
    period_start: date | None
    period_end: date | None
    opening: str | None
    closing: str | None
    previous_closing: str | None
    rows: list[ReconcileRow]
    rows_truncated: bool
    removed: list[RemovedRow]
    total: str
    difference: str | None
    status: Literal["balanced", "off", "unchecked"]
    verification: Verification | None
    causes: list[ReconcileCause]
    lines_skipped: int
    can_edit: bool


class DocumentReconciliation(BaseModel):
    """``GET /documents/{id}/reconciliation``: the statements the caller can see."""

    document_id: uuid.UUID
    statements: list[StatementReconciliation]


class ReconcileConfirm(_Input):
    """Confirm a statement that balances; balances are typed as printed."""

    statement: int = Field(ge=0, le=STATEMENT_INDEX_MAX)
    opening_balance: PrintedAmount | None = None
    closing_balance: PrintedAmount | None = None


class ReconcileAccept(_Input):
    """Accept a statement as read, without balancing it (no balance is recorded)."""

    statement: int = Field(ge=0, le=STATEMENT_INDEX_MAX)


class ReconcileRestore(_Input):
    """Put a removed row of the document back."""

    tombstone_id: uuid.UUID


class ReconcileRemoveEarlier(_Input):
    """Remove the rows an earlier reading of the statement left behind."""

    statement: int = Field(ge=0, le=STATEMENT_INDEX_MAX)


class ReconcileOutcome(BaseModel):
    """The statement after the action, and what the action did."""

    statement: StatementReconciliation
    confirmed: int
    snapshot: bool
    items_closed: int

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

Other kinds (Phase 3 and later) can only be dismissed for now
(``409 review-resolution-unsupported``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.documents.models import ReviewItemState, ReviewKind

MAX_PAGE: Final = 500
DEFAULT_PAGE: Final = 100

NextStep = Literal[
    "confirm_duplicate",
    "choose_account",
    "column_mapping",
    "reprocess",
    "choose_owner",
    "attach_receipt",
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


ReviewResolution = Annotated[
    ResolveDuplicate
    | ResolveUnknownAccount
    | ResolveUnparsed
    | ResolveUnknownOwner
    | ResolveReceiptMatch,
    Field(discriminator="kind"),
]


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


class ReviewPage(BaseModel):
    """Oldest first (a queue); ``next_cursor`` is ``null`` on the last page."""

    items: list[ReviewItemOut]
    next_cursor: uuid.UUID | None

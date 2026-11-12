# SPDX-License-Identifier: AGPL-3.0-or-later
"""A person's answers about one held statement: import anyway, import as read.

Two review answers let a statement the importer held back be imported:

* **Import anyway** on a ``possible_duplicate`` / ``statement_overlap``
  item (``app.domain.imports.statement_records``): the statement is
  imported although its period overlaps one already imported for the
  account from another document (and a "same statement" answer is taken
  back);
* **Import as read** on an ``investment_reconciliation_failed`` /
  ``investment_low_confidence`` item
  (``app.domain.investments.statement_import``, step 3): the brokerage
  statement is imported as printed although its figures do not add up, or
  could not be checked.

**An answer belongs to one statement as read**, never to an account or to
a place in the document. The item that holds a statement names its
:func:`statement_key`: a digest of the statement exactly as extracted (its
account digits, period, every row and figure, the parser and its version).
The resolution records that key with the item's account
(``app.domain.review.service``). Every import of the document
(``app.domain.imports.service.import_extraction``: any ``apply-mapping``
re-run, whoever or whatever starts it, and the pipeline when the document
is processed again) loads the document's answers (:func:`document_answers`)
and, for a statement whose key an answer names:

* puts the statement on the answer's account wherever it now sits in the
  document, unless the run itself assigns it (the Accounts dialog);
* imports it despite the overlap, or as read, **only on that account**.

**Where a statement went** is remembered the same way: when a person puts a
statement on an account (the Accounts dialog, "use this account" on an
``unknown_account`` item) and the re-run imports it there, the item's
resolution records the statement's key with that account (:data:`PLACED`).
Later imports of the document put that statement on that account again, so
a statement that names its account by no digits (an export that masks its
account numbers) is asked about once, not on every re-run. A placement
alone imports nothing that would otherwise be held.

Any other statement, including another statement of the same account and a
statement read differently after a reprocess, a lesson (Teach PerFin) or a
parser change, is checked as though nothing had been answered, and is held
again with an item of its own if it has to be. An answer is used only for
an account the person running the import may import into (the importer's
``allowed`` set, SECURITY.md F3). Items raised before statements carried
keys cannot be answered this way (``409 review-item-stale``): processing
the document again raises current ones.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.documents.models import Document, ReviewItem, ReviewItemState

#: Resolution keys of the two answers.
IMPORT_ANYWAY: Final = "import_anyway"
IMPORT_AS_READ: Final = "import_as_read"
#: Resolution key of an account choice the re-run carried out (module doc).
PLACED: Final = "placed"
#: Payload and resolution key naming the statement an item is about.
KEY_FIELD: Final = "statement_key"
#: Resolution key: the lines of the item an "import as read" answer imports
#: (indices in the statement; the others it listed are left out).
LINES_FIELD: Final = "lines"
LINES_MAX: Final = 2000
#: A bank statement (``statements``) or a brokerage one (``investments``).
Target = Literal["statement", "investment"]
_KEY_LENGTH: Final = 64
#: The key's alphabet: one letter per hex digit of the digest. No digits, so
#: the audit log's scrubber never mistakes a run of them for an account
#: number (``app.core.scrubber``) and the key reads back unchanged.
_KEY_ALPHABET: Final = "abcdefghijklmnop"
_FROM_HEX: Final = str.maketrans("0123456789abcdef", _KEY_ALPHABET)


def statement_key(statement: BaseModel) -> str:
    """The digest of one extracted statement as read (module doc).

    SHA-256 of its canonical JSON, written in :data:`_KEY_ALPHABET`.
    """
    canonical = json.dumps(statement.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest().translate(_FROM_HEX)


def payload_key(payload: Mapping[str, Any]) -> str | None:
    """The statement key a review item's payload (or resolution) names."""
    value = payload.get(KEY_FIELD)
    if (
        isinstance(value, str)
        and len(value) == _KEY_LENGTH
        and all(c in _KEY_ALPHABET for c in value)
    ):
        return value
    return None


def _lines(value: Any) -> frozenset[int] | None:
    """The line indices a resolution names (None: it names none, so all)."""
    if not isinstance(value, list):
        return None
    return frozenset(
        v for v in value[:LINES_MAX] if isinstance(v, int) and not isinstance(v, bool) and v >= 0
    )


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


@dataclass
class StatementAnswers:
    """The answers of one document's resolved items, by statement (module doc)."""

    #: The account the latest answer about each statement put it on.
    placed: dict[tuple[Target, str], uuid.UUID] = field(default_factory=dict)
    #: ``(target, key, account, answer)`` of every answer given.
    given: set[tuple[Target, str, uuid.UUID, str]] = field(default_factory=set)
    #: The lines an "import as read" answer named (their indices in the
    #: statement); absent: every line it was asked about.
    chosen: dict[tuple[Target, str, uuid.UUID], frozenset[int]] = field(default_factory=dict)

    def add(
        self,
        target: Target,
        key: str,
        account_id: uuid.UUID,
        answer: str,
        lines: frozenset[int] | None = None,
    ) -> None:
        self.placed[(target, key)] = account_id
        self.given.add((target, key, account_id, answer))
        if answer == IMPORT_AS_READ:
            if lines is None:
                # A later answer for every line takes the choice back.
                self.chosen.pop((target, key, account_id), None)
            else:
                self.chosen[(target, key, account_id)] = lines

    def place(self, target: Target, key: str, account_id: uuid.UUID) -> None:
        """A person put the statement on the account (nothing more is said by it)."""
        self.placed[(target, key)] = account_id

    def lines(self, target: Target, key: str, account_id: uuid.UUID) -> frozenset[int] | None:
        """The lines the "import as read" answer named (None: all of them)."""
        return self.chosen.get((target, key, account_id))

    def account(self, target: Target, key: str) -> uuid.UUID | None:
        """The account the statement was answered on (None: no answer)."""
        return self.placed.get((target, key))

    def overlap(self, target: Target, key: str, account_id: uuid.UUID) -> bool:
        """The person said to import this statement into this account anyway."""
        return (target, key, account_id, IMPORT_ANYWAY) in self.given

    def as_read(self, target: Target, key: str, account_id: uuid.UUID) -> bool:
        """The person said to import this statement into this account as read."""
        return (target, key, account_id, IMPORT_AS_READ) in self.given


def document_answers(db: Session, document: Document) -> StatementAnswers:
    """What the document's resolved items said to import anyway / as read.

    Oldest answer first, so a later answer about the same statement on
    another account decides where it goes.
    """
    found = StatementAnswers()
    rows = db.execute(
        select(ReviewItem.resolution)
        .where(
            ReviewItem.household_id == document.household_id,
            ReviewItem.document_id == document.id,
            ReviewItem.state == ReviewItemState.RESOLVED,
        )
        .order_by(ReviewItem.resolved_at.asc().nulls_first(), ReviewItem.id)
    ).scalars()
    for resolution in rows:
        if not isinstance(resolution, dict):
            continue
        answer: str | None
        if resolution.get(IMPORT_ANYWAY) is True:
            answer = IMPORT_ANYWAY
        elif resolution.get(IMPORT_AS_READ) is True:
            answer = IMPORT_AS_READ
        elif resolution.get(PLACED) is True:
            answer = None
        else:
            continue
        account_id = _uuid(resolution.get("account_id"))
        key = payload_key(resolution)
        target = resolution.get("target")
        if account_id is None or key is None or target not in ("statement", "investment"):
            continue
        kind: Target = "investment" if target == "investment" else "statement"
        if answer is None:
            found.place(kind, key, account_id)
        else:
            found.add(kind, key, account_id, answer, _lines(resolution.get(LINES_FIELD)))
    return found


def placement(target: Target, key: str, account_id: uuid.UUID) -> dict[str, Any]:
    """The resolution keys recording that a statement was put on an account."""
    return {PLACED: True, "target": target, KEY_FIELD: key, "account_id": str(account_id)}

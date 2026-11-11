# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wording the household confirmed: what PerFin remembers of a statement's layout.

The optional local model may suggest what kind of activity an unfamiliar
wording on a brokerage statement is (ADR-0010,
:mod:`app.ingestion.parsers.vocabulary`). When a person confirms such
suggestions ("Import as read" on the item listing them,
``app.domain.review.service``), the wording itself is kept for the
household, so the next statement of that layout is read without asking the
model or the person again.

**Only layout and wording is kept, never a detail of the statement.** An
entry is a phrase and what it means, nothing else:

* the *phrase* is a section's heading, or the action words of a table that
  names its securities in a column of their own (a broker's words); the
  parser marks which rows carry such wording
  (``InvestmentActivity.suggestion``). The free text of a cash table (a
  payee, a merchant, a memo) is never marked, so it is never kept;
* it is a :func:`~app.ingestion.parsers.vocabulary.phrase_key`: lower case,
  no punctuation, **every digit masked**, 80 characters at most;
* it is refused when the household's scrubber would mask any of it (a
  member's or the household's name, an address, a stored account number:
  :func:`~app.ingestion.parsers.vocabulary.rememberable`). PerFin cannot
  know a stranger's name; that is what the first rule is for, and why
  everything kept is shown and can be removed;
* the *meaning* is one of the closed lists a suggestion may name
  (``SUGGESTED_TYPES`` / ``SUGGESTED_SECTIONS``), and the *layout* is the
  parser's institution key (else its name): wording learned from one
  broker's statements is not applied to another's.

No amount, date, security, account or name of the statement it was learned
from is stored: an entry says ``periodic investment plan`` means ``buy`` on
this layout, and where it was learned (the document's id, for the "Learned"
page's link).

Stored as one household-scoped ``setting`` row (:data:`SETTING_KEY`; no
table of its own), bounded (:data:`MAX_ENTRIES`), validated again on every
read. ``GET /imports/wording`` lists it for any role; ``DELETE
/imports/wording/{id}`` (member or above) forgets an entry, audited
(``learned_wording.delete``). Remembering is audited too
(``learned_wording.add``: ids and counts, never the phrases).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.authz import role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.scrubber import Scrubber, member_terms
from app.db.models import Household, Setting, SettingScope
from app.domain.accounts.models import Account
from app.domain.documents.models import Document
from app.domain.households.repo import list_members
from app.domain.imports.schemas import LearnedWordingList, LearnedWordingOut
from app.ingestion.parsers.vocabulary import (
    ACTION_PREFIX,
    COLUMN_PREFIX,
    SECTION_PREFIX,
    SUGGESTED_COLUMNS,
    SUGGESTED_DOUBT,
    SUGGESTED_SECTIONS,
    SUGGESTED_TYPES,
    Vocabulary,
    rememberable,
)
from app.ingestion.sandbox.protocol import InvestmentStatement, ParserRef

#: The household setting that holds the learned wording.
SETTING_KEY: Final = "ingestion.learned_wording"
#: Where the model's suggestions are kept on a document (``app.ingestion.pipeline``).
SUGGESTIONS_KEY: Final = "ml_vocabulary"
MAX_ENTRIES: Final = 500
_LAYOUT_MAX: Final = 64

WordingKind = Literal["action", "section", "column"]
_ALLOWED: Final[dict[str, frozenset[str]]] = {
    "action": SUGGESTED_TYPES,
    "section": SUGGESTED_SECTIONS,
    # The heading of a table's column and what the column holds.
    "column": SUGGESTED_COLUMNS,
}


def wording_not_found() -> ProblemError:
    return ProblemError(404, "wording-not-found", "Not found", "No such learned wording.")


@dataclass(frozen=True)
class Entry:
    """One learned phrase (module doc)."""

    layout: str
    kind: WordingKind
    phrase: str
    meaning: str
    learned_at: datetime
    document_id: uuid.UUID | None = None

    @property
    def id(self) -> str:
        """Stable for ``(layout, kind, phrase)``: what a forget request names."""
        return hashlib.sha256(
            "\x1f".join((self.layout, self.kind, self.phrase)).encode()
        ).hexdigest()[:32]

    def stored(self) -> dict[str, Any]:
        return {
            "layout": self.layout,
            "kind": self.kind,
            "phrase": self.phrase,
            "meaning": self.meaning,
            "learned_at": self.learned_at.isoformat(),
            "document_id": str(self.document_id) if self.document_id else None,
        }


def layout_key(parser: ParserRef | None) -> str | None:
    """The layout a parser read: its institution's key, else the parser's name."""
    if parser is None:
        return None
    return (parser.institution or parser.name)[:_LAYOUT_MAX] or None


def _entry(data: Any) -> Entry | None:
    """A stored entry, checked again (None: not one this version would have written)."""
    if not isinstance(data, dict):
        return None
    layout, kind, phrase, meaning = (data.get(k) for k in ("layout", "kind", "phrase", "meaning"))
    if not (isinstance(layout, str) and 0 < len(layout) <= _LAYOUT_MAX and layout.isprintable()):
        return None
    if kind not in _ALLOWED or meaning not in _ALLOWED[kind]:
        return None
    if not isinstance(phrase, str) or not rememberable(phrase):
        return None
    try:
        learned_at = datetime.fromisoformat(str(data.get("learned_at")))
        raw = data.get("document_id")
        document_id = uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None
    return Entry(
        layout,
        cast(WordingKind, kind),
        phrase,
        meaning,
        learned_at,
        document_id,
    )


def _row(db: Session, household_id: uuid.UUID) -> Setting | None:
    return db.execute(
        select(Setting).where(
            Setting.scope == SettingScope.HOUSEHOLD,
            Setting.scope_id == household_id,
            Setting.key == SETTING_KEY,
        )
    ).scalar_one_or_none()


def _entries(row: Setting | None) -> list[Entry]:
    value = row.value if row is not None else None
    raw = value.get("entries") if isinstance(value, dict) else None
    found = [_entry(item) for item in (raw if isinstance(raw, list) else [])[:MAX_ENTRIES]]
    return [entry for entry in found if entry is not None]


def _save(db: Session, household_id: uuid.UUID, row: Setting | None, entries: list[Entry]) -> None:
    value = {"version": 1, "entries": [entry.stored() for entry in entries[:MAX_ENTRIES]]}
    now = clock.utcnow()
    if row is None:
        db.add(
            Setting(
                scope=SettingScope.HOUSEHOLD,
                scope_id=household_id,
                key=SETTING_KEY,
                value=value,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        row.value = value
        row.updated_at = now
    db.flush()


def household_vocabulary(db: Session, household_id: uuid.UUID, layout: str | None) -> Vocabulary:
    """What the household learned about ``layout``'s wording (empty: nothing)."""
    if layout is None:
        return Vocabulary()
    entries = [e for e in _entries(_row(db, household_id)) if e.layout == layout]
    return Vocabulary(
        learned_actions={e.phrase: e.meaning for e in entries if e.kind == "action"},
        learned_sections={e.phrase: e.meaning for e in entries if e.kind == "section"},
        learned_columns={e.phrase: e.meaning for e in entries if e.kind == "column"},
    )


def _scrubber(db: Session, household_id: uuid.UUID) -> Scrubber:
    """What identifies this household: its members, its name, its account numbers."""
    users = [user for _, user in list_members(db, household_id)]
    household = db.execute(
        select(Household.name).where(Household.id == household_id)
    ).scalar_one_or_none()
    numbers = db.execute(
        select(Account.number_last4).where(
            Account.household_id == household_id, Account.number_last4.is_not(None)
        )
    ).scalars()
    return Scrubber(
        member_terms=member_terms(
            names=[u.display_name for u in users],
            usernames=[u.username for u in users],
            emails=[u.email for u in users],
            household_names=[household],
        ),
        account_numbers=list(numbers),
    )


def confirmed(
    statement: InvestmentStatement, suggestions: Vocabulary
) -> list[tuple[WordingKind, str, str]]:
    """``(kind, phrase, meaning)`` of the suggestions that typed ``statement``'s rows
    and may be remembered (the parser marked their wording), each once."""
    found: dict[tuple[WordingKind, str], str] = {}
    for row in statement.activity:
        marker = row.suggestion
        if marker is None or SUGGESTED_DOUBT not in row.low_confidence:
            continue
        if marker.startswith(ACTION_PREFIX):
            phrase = marker[len(ACTION_PREFIX) :]
            meaning = suggestions.actions.get(phrase)
            if meaning is not None:
                found.setdefault(("action", phrase), meaning)
        elif marker.startswith(SECTION_PREFIX):
            phrase = marker[len(SECTION_PREFIX) :]
            meaning = suggestions.sections.get(phrase)
            if meaning is not None:
                found.setdefault(("section", phrase), meaning)
        elif marker.startswith(COLUMN_PREFIX):
            # A table read through suggested column headings: all of them
            # are confirmed with its rows.
            for phrase, meaning in suggestions.columns.items():
                found.setdefault(("column", phrase), meaning)
    return [(kind, phrase, meaning) for (kind, phrase), meaning in found.items()]


def remember_confirmed(
    db: Session,
    principal: Principal,
    document: Document,
    statement: InvestmentStatement,
    *,
    request: HTTPConnection | None = None,
) -> int:
    """Keep the wording a person just confirmed on ``statement`` (module doc).

    Returns how many phrases were added. Wording already learned for the
    layout stays as it is (the first confirmation decides; forget it to
    change it), and a phrase the household's scrubber would mask is left
    out: it is asked about again next time.
    """
    layout = layout_key(statement.parser)
    suggestions = Vocabulary.from_stored((document.confidence or {}).get(SUGGESTIONS_KEY))
    wording = confirmed(statement, suggestions)
    if layout is None or not wording:
        return 0
    clean = _scrubber(db, document.household_id).scrub_text
    row = _row(db, document.household_id)
    entries = _entries(row)
    known = {(e.layout, e.kind, e.phrase) for e in entries}
    now = clock.utcnow()
    added: list[Entry] = []
    for kind, phrase, meaning in wording:
        if (layout, kind, phrase) in known or not rememberable(phrase, clean):
            continue
        if len(entries) + len(added) >= MAX_ENTRIES:
            break
        added.append(Entry(layout, kind, phrase, meaning, now, document.id))
    if not added:
        return 0
    _save(db, document.household_id, row, [*entries, *added])
    audit(
        db,
        action="learned_wording.add",
        target_type="document",
        target_id=document.id,
        after={"layout": layout, "count": len(added), "ids": [e.id for e in added]},
        household_id=document.household_id,
        user_id=principal.user_id,
        request=request,
    )
    return len(added)


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or not role_at_least(principal.role, minimum):
        raise wording_not_found()
    return principal.household_id


def _out(entries: Iterable[Entry]) -> list[LearnedWordingOut]:
    return [
        LearnedWordingOut(
            id=e.id,
            layout=e.layout,
            kind=e.kind,
            phrase=e.phrase,
            meaning=e.meaning,
            learned_at=e.learned_at,
            learned_from_document_id=e.document_id,
        )
        for e in entries
    ]


def list_wording(db: Session, principal: Principal) -> LearnedWordingList:
    """The household's learned wording, newest first."""
    household_id = _household(principal, HouseholdRole.VIEWER)
    entries = sorted(_entries(_row(db, household_id)), key=lambda e: e.learned_at, reverse=True)
    return LearnedWordingList(items=_out(entries))


def forget_wording(
    db: Session, principal: Principal, entry_id: str, *, request: HTTPConnection | None
) -> None:
    """Forget one learned phrase; the next statement wording it is asked about again."""
    household_id = _household(principal, HouseholdRole.MEMBER)
    row = _row(db, household_id)
    entries = _entries(row)
    gone = next((e for e in entries if e.id == entry_id), None)
    if gone is None:
        raise wording_not_found()
    _save(db, household_id, row, [e for e in entries if e.id != entry_id])
    audit(
        db,
        action="learned_wording.delete",
        target_type="setting",
        target_id=None,
        before={"id": gone.id, "layout": gone.layout, "kind": gone.kind, "meaning": gone.meaning},
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )

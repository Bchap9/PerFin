# SPDX-License-Identifier: AGPL-3.0-or-later
"""The institution a never-seen statement names in its header (deterministic).

A fingerprint names its institution; a structured statement states it (OFX
``FI/ORG``). A statement in a layout no fingerprint knows still prints who
sent it, almost always in the first lines of its first page: ``Northwind
Bank, N.A.``, ``Prairie Lakes Credit Union``, ``Thistle & Wren Building
Society``, ``Issued by Meridian Card Bank``. :func:`detect_institution`
reads that name so the first statement of a new bank can propose its
institution and the next one is recognised by it
(:func:`app.ingestion.classify.service.resolve_institution`,
:mod:`app.ingestion.match.institutions`).

A name is a run of capitalised words that ends in a word saying what kind
of company it is (:data:`STRONG_KINDS`, :data:`WEAK_KINDS`), with the
corporate suffixes after it (``N.A.``, ``FSB``, ``Inc.``) and, for a bank,
``of <Place>`` (``Bank of Springfield``). It is refused when

* nothing distinctive is left once the generic words are dropped
  (``Federal Credit Union``, ``Your Bank``:
  :func:`app.ingestion.match.institutions.institution_key`);
* a weak kind word is followed by a word that makes it a heading
  (``Financial Summary``, ``Mortgage Statement``, ``Insurance Account``;
  ``Acme Mortgage LLC Statement`` names a company);
* it holds a digit (an address, a reference).

Only the header lines of the classifier's view are read (at most
:data:`app.ingestion.classify.text.HEADER_LINES` lines of
:data:`~app.ingestion.classify.text.MAX_LINE_CHARS` characters), so the work
is bounded and a payee in a transaction row is never taken for the sender.
The name is a **suggestion**: it resolves to one of the household's
institutions by name, is offered when an account is set up from the file,
and breaks a tie between accounts sharing their last four digits. It never
creates an institution by itself and is never the only reason a statement
lands on an account.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from app.ingestion.match.institutions import institution_key

#: Kind words that name a deposit-taking institution by themselves.
STRONG_KINDS: Final[tuple[tuple[str, ...], ...]] = (
    ("federal", "credit", "union"),
    ("credit", "union"),
    ("building", "society"),
    ("savings", "and", "loan"),
    ("savings", "&", "loan"),
    ("savings", "bank"),
    ("bank",),
    ("banque",),
    ("banco",),
    ("bancorp",),
    ("fcu",),
    ("sparkasse",),
)
#: Kind words that also head sections of a statement (``Financial Summary``).
WEAK_KINDS: Final[tuple[tuple[str, ...], ...]] = (
    ("card", "services"),
    ("wealth", "management"),
    ("asset", "management"),
    ("trust", "company"),
    ("financial",),
    ("brokerage",),
    ("securities",),
    ("investments",),
    ("insurance",),
    ("mutual",),
    ("mortgage",),
    ("lending",),
)
#: Corporate forms printed after the kind word.
SUFFIXES: Final = frozenset(
    {"n.a", "na", "fsb", "ssb", "inc", "llc", "plc", "ltd", "corp", "co", "company", "usa", "group"}
)
#: Words joining the words of a name (never its first or last word).
CONNECTORS: Final = frozenset({"&", "and", "of", "the", "de", "du", "la"})
#: Words before a name that are not part of it (``Issued by``, ``Welcome to``).
LEADING_STOP: Final = frozenset(
    {
        *("your", "my", "our", "the", "a", "an", "to", "from", "by", "at", "with", "for"),
        *("welcome", "issued", "member", "dear", "thank", "you", "statement", "account"),
        *("online", "mobile", "personal", "business", "visit", "call", "contact", "page"),
    }
)
#: After a weak kind word: the phrase is a heading, not a name.
HEADING_WORDS: Final = frozenset(
    {
        *("summary", "statement", "statements", "account", "accounts", "activity", "detail"),
        *("details", "information", "overview", "report", "balance", "transactions", "plan"),
        *("center", "centre", "charge", "charges", "fee", "fees", "payment", "interest"),
        *("advisor", "adviser", "review", "planning", "goals", "history", "terms"),
    }
)
#: Words of a name before its kind word, and of a place after ``of``.
MAX_NAME_WORDS: Final = 5
MAX_PLACE_WORDS: Final = 3
MAX_NAME_CHARS: Final = 80
STRONG_CONFIDENCE: Final = 0.7
WEAK_CONFIDENCE: Final = 0.6
_TRIM: Final = ",.;:()[]\"'"


@dataclass(frozen=True)
class InstitutionName:
    """A name read from the header and how far it is trusted."""

    name: str
    confidence: float


def _norm(token: str) -> str:
    return token.strip(_TRIM).casefold()


def _capitalised(token: str) -> bool:
    """A word a name is written with: starts with a capital, holds no digit."""
    word = token.strip(_TRIM)
    return bool(word) and word[0].isupper() and not any(c.isdigit() for c in word)


def _kind_at(
    norms: Sequence[str], start: int, kinds: Sequence[tuple[str, ...]]
) -> tuple[str, ...] | None:
    for kind in kinds:
        if tuple(norms[start : start + len(kind)]) == kind:
            return kind
    return None


def _name_start(tokens: Sequence[str], norms: Sequence[str], kind_start: int) -> int:
    """Where the name ending at ``kind_start`` begins (``kind_start`` when it has no words)."""
    start = kind_start
    while start > 0 and kind_start - start < MAX_NAME_WORDS:
        token = tokens[start - 1]
        if token.endswith((",", ";", ":")):
            break  # the previous phrase ended there
        if not (_capitalised(token) or norms[start - 1] in CONNECTORS):
            break
        start -= 1
    while start < kind_start and (norms[start] in LEADING_STOP or norms[start] in CONNECTORS):
        start += 1
    return start


def _name_end(tokens: Sequence[str], norms: Sequence[str], end: int, *, strong: bool) -> int:
    """Where the name whose kind word ends at ``end`` stops: suffixes, more kinds, ``of X``."""
    while end < len(tokens):
        more = _kind_at(norms, end, (*STRONG_KINDS, *WEAK_KINDS))
        if more is not None:
            end += len(more)
        elif norms[end] in SUFFIXES and (tokens[end][:1].isupper() or norms[end] == "n.a"):
            end += 1
        else:
            break
    if strong and end < len(tokens) and norms[end] == "of":
        place = end + 1
        if place < len(tokens) and norms[place] == "the":
            place += 1
        last = place
        while (
            last < len(tokens)
            and last - place < MAX_PLACE_WORDS
            and _capitalised(tokens[last])
            and norms[last] not in HEADING_WORDS
        ):
            last += 1
        if last > place:
            end = last
    return end


def _read_line(line: str) -> InstitutionName | None:
    tokens = line.split()
    norms = [_norm(token) for token in tokens]
    for position in range(len(tokens)):
        strong = _kind_at(norms, position, STRONG_KINDS)
        kind = strong or _kind_at(norms, position, WEAK_KINDS)
        if kind is None or not _capitalised(tokens[position]):
            continue
        start = _name_start(tokens, norms, position)
        end = _name_end(tokens, norms, position + len(kind), strong=strong is not None)
        bare = end == position + len(kind)  # no corporate form after the kind word
        if strong is None and bare and end < len(tokens) and norms[end] in HEADING_WORDS:
            continue
        name = " ".join(tokens[start:end]).strip(" ,;:")
        if name.endswith(".") and _norm(tokens[end - 1]) not in SUFFIXES:
            name = name.rstrip(".")
        if (
            not name
            or len(name) > MAX_NAME_CHARS
            or any(c.isdigit() for c in name)
            or not institution_key(name)
        ):
            continue
        return InstitutionName(name, STRONG_CONFIDENCE if strong else WEAK_CONFIDENCE)
    return None


def detect_institution(header: Sequence[str]) -> InstitutionName | None:
    """The institution the header names, top line first (module doc)."""
    best: InstitutionName | None = None
    for line in header:
        found = _read_line(line)
        if found is None:
            continue
        if found.confidence >= STRONG_CONFIDENCE:
            return found
        best = best or found
    return best

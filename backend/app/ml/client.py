# SPDX-License-Identifier: AGPL-3.0-or-later
"""The worker's client of the optional ml adapter, and the checks on its answers (ADR-0010).

PerFin's own readers come first. Only when they are unsure (a document whose
kind is unknown, or an account no last four digits matched) does the worker
send the document's extracted text (``PERFIN_ML_MAX_CHARS`` characters,
never the file, never ledger data) to the adapter at ``PERFIN_ML_URL``, which
asks a model running on the same host. The answer is an **untrusted
suggestion** and is checked here (:func:`validated`) before anything uses it:

* the kind must be one PerFin knows;
* an account's last four digits are kept only when exactly those four digits
  appear in the document's text (a model cannot invent an account number);
* names, institutions and currencies are trimmed and shape-checked; dates must
  parse and lie between 1970 and next year.

What survives is stored on the document (``document.confidence["ml"]``) and
offered to a person (account proposals, account suggestions in the review
queue); it is never written to the ledger on its own.

A second question (:func:`activity_hints`): when a brokerage parser could not
type some activity rows because their action words or their section's
heading are in no vocabulary of its own, the worker sends those phrases
(normalised, digits masked, at most
:data:`~app.ingestion.parsers.vocabulary.MAX_PHRASES` of each) with the
statement's text to read them in (``PERFIN_ML_MAX_CHARS`` characters, as for
the first question) and asks what kind of activity each is, from a closed
list. The answer is matched back by position to the phrases PerFin sent
(never by text the model returns) and only listed kinds are kept
(:func:`validated_vocabulary`): whatever the model read in the statement,
all that comes back is a kind per numbered phrase. A row typed this way is
marked as a suggestion and waits for a person
(:mod:`app.ingestion.parsers.vocabulary`).

Nothing of a document is remembered by the model: it is not trained or
tuned on what it is sent, every call stands alone (no conversation is
carried from one to the next) and the adapter stores nothing and logs no
content. What PerFin keeps of an answer is the checked kinds above, on the
document they are about.

Failures never fail ingestion: a timeout, an error, a refused address or a
malformed answer is logged and the document goes on without hints. After
:data:`BREAKER_FAILURES` failures in a row the adapter is not asked again for
:data:`BREAKER_SECONDS`.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

import httpx

from app.core.local_url import resolves_locally
from app.core.settings import Settings
from app.ingestion.parsers.vocabulary import (
    MAX_PHRASES,
    SUGGESTED_COLUMNS,
    SUGGESTED_SECTIONS,
    SUGGESTED_TYPES,
    Vocabulary,
    column_key,
    phrase_key,
)

log = logging.getLogger(__name__)

KINDS: Final = frozenset(
    {"statement", "receipt", "invoice", "tax_form", "payslip", "insurance", "other"}
)
ACCOUNT_TYPES: Final = frozenset(
    {
        "checking",
        "savings",
        "credit_card",
        "brokerage",
        "retirement",
        "loan",
        "mortgage",
        "line_of_credit",
    }
)
MAX_ACCOUNTS: Final = 10
BREAKER_FAILURES: Final = 3
BREAKER_SECONDS: Final = 300.0
_CURRENCY: Final = re.compile(r"^[A-Z]{3}$")
_LAST4: Final = re.compile(r"^[0-9]{4}$")


@dataclass(frozen=True)
class MLAccount:
    last4: str | None
    name: str | None
    type: str | None
    currency: str | None


@dataclass(frozen=True)
class MLHints:
    """A checked answer (module doc); what failed a check is simply absent."""

    kind: str | None
    institution: str | None
    accounts: tuple[MLAccount, ...]
    period_start: date | None
    period_end: date | None
    confidence: float
    model: str | None = None

    def stored(self) -> dict[str, Any]:
        """The JSON kept in ``document.confidence["ml"]``."""
        return {
            "kind": self.kind,
            "institution": self.institution,
            "accounts": [
                {"last4": a.last4, "name": a.name, "type": a.type, "currency": a.currency}
                for a in self.accounts
            ],
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "confidence": self.confidence,
            "model": self.model,
        }

    @classmethod
    def from_stored(cls, data: Any) -> MLHints | None:
        """The hints stored on a document (None: none, or not readable)."""
        if not isinstance(data, dict):
            return None
        return validated(data, None)


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    clean = " ".join(value.split())[:limit]
    return clean if clean and clean.isprintable() else None


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value[:10])
    except ValueError:
        return None
    return parsed if date(1970, 1, 1) <= parsed <= date(date.today().year + 1, 12, 31) else None


def _printed(last4: str, text: str) -> bool:
    return re.search(rf"(?<![0-9]){last4}(?![0-9])", text) is not None


def validated(answer: Any, text: str | None) -> MLHints | None:
    """The answer reduced to what passes the module's checks.

    ``text`` is the document text the model read; ``None`` (hints read back
    from storage, already checked) skips the printed-digits check.
    """
    if not isinstance(answer, dict):
        return None
    kind = answer.get("kind")
    accounts: list[MLAccount] = []
    raw = answer.get("accounts")
    for entry in raw[:MAX_ACCOUNTS] if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        last4 = entry.get("last4")
        if not (isinstance(last4, str) and _LAST4.fullmatch(last4)):
            last4 = None
        elif text is not None and not _printed(last4, text):
            last4 = None  # not printed in the document: a guess, dropped
        currency = entry.get("currency")
        kind_of = entry.get("type")
        accounts.append(
            MLAccount(
                last4=last4,
                name=_text(entry.get("name"), 80),
                type=kind_of if kind_of in ACCOUNT_TYPES else None,
                currency=currency
                if isinstance(currency, str) and _CURRENCY.fullmatch(currency)
                else None,
            )
        )
    confidence = answer.get("confidence")
    return MLHints(
        kind=kind if kind in KINDS else None,
        institution=_text(answer.get("institution"), 120),
        accounts=tuple(accounts),
        period_start=_date(answer.get("period_start")),
        period_end=_date(answer.get("period_end")),
        confidence=float(confidence)
        if isinstance(confidence, int | float) and 0 <= confidence <= 1
        else 0.0,
        model=_text(answer.get("model"), 80),
    )


@dataclass
class _Breaker:
    failures: int = 0
    until: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def open(self) -> bool:
        with self.lock:
            return time.monotonic() < self.until

    def record(self, ok: bool) -> None:
        with self.lock:
            if ok:
                self.failures = 0
                return
            self.failures += 1
            if self.failures >= BREAKER_FAILURES:
                self.until = time.monotonic() + BREAKER_SECONDS
                self.failures = 0


_BREAKER = _Breaker()


def _ask(
    settings: Settings,
    path: str,
    body: dict[str, Any],
    transport: httpx.BaseTransport | None,
) -> Any:
    """One call to the adapter (None: off, breaker open, not local, or failed)."""
    if not settings.ml_enabled or _BREAKER.open():
        return None
    token = settings.ml_token.get_secret_value() if settings.ml_token is not None else ""
    if transport is None and not resolves_locally(settings.ml_url):
        log.warning("ml adapter address does not resolve to a local address; not asked")
        _BREAKER.record(False)
        return None
    try:
        with httpx.Client(
            timeout=settings.ml_timeout_seconds,
            trust_env=False,  # never through HTTP(S)_PROXY: the adapter is local
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = client.post(
                f"{settings.ml_url}{path}",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()
        answer = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("ml adapter call failed", extra={"error": type(exc).__name__})
        _BREAKER.record(False)
        return None
    _BREAKER.record(True)
    return answer


def document_hints(
    settings: Settings, text: str, *, transport: httpx.BaseTransport | None = None
) -> MLHints | None:
    """Ask the adapter about ``text`` and check the answer (None: off, failed or nothing usable)."""
    if not text.strip():
        return None
    sent = text[: settings.ml_max_chars]
    answer = _ask(settings, "/v1/document", {"text": sent}, transport)
    return validated(answer, sent) if answer is not None else None


def _keys(phrases: Sequence[str]) -> list[str]:
    """The distinct :func:`phrase_key` of ``phrases``, in order, bounded."""
    out: list[str] = []
    for phrase in phrases:
        key = phrase_key(phrase)
        if key and key not in out and len(out) < MAX_PHRASES:
            out.append(key)
    return out


def _choices(answer: Any, key: str, sent: Sequence[str], allowed: frozenset[str]) -> dict[str, str]:
    """``{phrase sent: kind}`` from the answer's ``{"index", key}`` entries.

    ``index`` counts from 1 over ``sent``; a number out of range or given
    twice, and a kind that is not listed, are dropped.
    """
    out: dict[str, str] = {}
    for entry in answer if isinstance(answer, list) else []:
        if not isinstance(entry, dict):
            continue
        index, value = entry.get("index"), entry.get(key)
        if (
            isinstance(index, int)
            and not isinstance(index, bool)
            and 1 <= index <= len(sent)
            and sent[index - 1] not in out
            and value in allowed
        ):
            out[sent[index - 1]] = value
    return out


def validated_vocabulary(
    answer: Any,
    actions: Sequence[str],
    headings: Sequence[str],
    columns: Sequence[str] = (),
) -> Vocabulary | None:
    """The answer reduced to listed kinds for the phrases that were sent (module doc)."""
    if not isinstance(answer, dict):
        return None
    found = Vocabulary(
        actions=_choices(answer.get("actions"), "type", actions, SUGGESTED_TYPES),
        sections=_choices(answer.get("headings"), "kind", headings, SUGGESTED_SECTIONS),
        columns=_choices(answer.get("columns"), "role", columns, SUGGESTED_COLUMNS),
        model=_text(answer.get("model"), 80),
    )
    return None if found.empty else found


def activity_hints(
    settings: Settings,
    actions: Sequence[str],
    headings: Sequence[str],
    text: str = "",
    columns: Sequence[str] = (),
    *,
    transport: httpx.BaseTransport | None = None,
) -> Vocabulary | None:
    """Ask what kind of activity ``actions`` and ``headings`` are (module doc).

    ``text``: the statement they are from, for the model to read them in
    (cut to ``PERFIN_ML_MAX_CHARS``). ``columns``: the column headings of an
    activity table whose header no parser could use; the model says what
    each names. None: off, failed, or no phrase got a listed kind.
    """
    sent_actions, sent_headings = _keys(actions), _keys(headings)
    sent_columns = [key for c in columns if (key := column_key(c)) is not None]
    sent_columns = list(dict.fromkeys(sent_columns))[:MAX_PHRASES]
    if not sent_actions and not sent_headings and not sent_columns:
        return None
    answer = _ask(
        settings,
        "/v1/vocabulary",
        {
            "actions": sent_actions,
            "headings": sent_headings,
            **({"columns": sent_columns} if sent_columns else {}),
            "text": text[: settings.ml_max_chars],
        },
        transport,
    )
    if answer is None:
        return None
    return validated_vocabulary(answer, sent_actions, sent_headings, sent_columns)


def reset_breaker() -> None:
    """Tests: forget earlier failures."""
    global _BREAKER
    _BREAKER = _Breaker()

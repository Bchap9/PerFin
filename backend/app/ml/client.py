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
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

import httpx

from app.core.local_url import resolves_locally
from app.core.settings import Settings

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


def document_hints(
    settings: Settings, text: str, *, transport: httpx.BaseTransport | None = None
) -> MLHints | None:
    """Ask the adapter about ``text`` and check the answer (None: off, failed or nothing usable)."""
    if not settings.ml_enabled or not text.strip() or _BREAKER.open():
        return None
    token = settings.ml_token.get_secret_value() if settings.ml_token is not None else ""
    if transport is None and not resolves_locally(settings.ml_url):
        log.warning("ml adapter address does not resolve to a local address; not asked")
        _BREAKER.record(False)
        return None
    sent = text[: settings.ml_max_chars]
    try:
        with httpx.Client(
            timeout=settings.ml_timeout_seconds,
            trust_env=False,  # never through HTTP(S)_PROXY: the adapter is local
            follow_redirects=False,
            transport=transport,
        ) as client:
            response = client.post(
                f"{settings.ml_url}/v1/document",
                json={"text": sent},
                headers={"Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()
        answer = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("ml adapter call failed", extra={"error": type(exc).__name__})
        _BREAKER.record(False)
        return None
    _BREAKER.record(True)
    return validated(answer, sent)


def reset_breaker() -> None:
    """Tests: forget earlier failures."""
    global _BREAKER
    _BREAKER = _Breaker()

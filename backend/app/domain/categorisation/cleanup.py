# SPDX-License-Identifier: AGPL-3.0-or-later
"""Payee clean-up: from the text a bank printed to who was paid.

``clean(raw)`` gives the **core** (what is left of a descriptor once the
bank's and the processor's additions are gone, in merchant-key form), the
descriptor's **kind** (a purchase, a transfer, a card payment...), the
processor it came through (``via``) and any signals read on the way. Pure
functions: no database, no I/O; the raw descriptor is never changed.

This is the declaration the other modules build against. For now ``clean``
only folds the text to lower-case ``a-z0-9`` tokens: the kind is always
``unknown`` and nothing is stripped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from app.domain.transactions.models import PAYEE_MAX

#: Bumped when clean-up changes what it returns; a household whose rows were
#: keyed with an older version is refreshed.
CLEANER_VERSION: Final = 1

DescriptorKind = Literal[
    "purchase",
    "recurring_purchase",
    "ach_debit",
    "ach_credit",
    "payroll",
    "transfer",
    "card_payment",
    "loan_payment",
    "cash",
    "check",
    "fee",
    "interest",
    "refund",
    "p2p",
    "deposit",
    "unknown",
]

#: Kinds a correction is remembered for and a merchant rule may be made from.
TEACHABLE: Final[frozenset[DescriptorKind]] = frozenset(
    {"purchase", "recurring_purchase", "ach_debit", "ach_credit", "payroll", "refund", "unknown"}
)

_KEPT: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


@dataclass(frozen=True)
class Cleaned:
    """What clean-up read from one descriptor (module doc)."""

    #: Lower-case ``a-z0-9`` tokens joined by single spaces; may be empty.
    core: str
    kind: DescriptorKind = "unknown"
    #: The processor or marketplace the charge came through, when one was read.
    via: str | None = None
    signals: frozenset[str] = frozenset()


def clean(raw: str | None) -> Cleaned:
    """The descriptor's core, kind, processor and signals (module doc)."""
    if not raw:
        return Cleaned(core="")
    folded = "".join(ch if ch in _KEPT else " " for ch in raw[:PAYEE_MAX].lower())
    return Cleaned(core=" ".join(folded.split()))

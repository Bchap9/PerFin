# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which of the household's payees an imported row is about (deterministic).

A bank prints more than the payee: ``Card Purchase 01/14 Corner Grocer #204
Springfield IL Card 7731``, ``OAKFIELD RENTALS DES:RENT ID:88123``. Comparing
the whole description with the payee's name (or one of its aliases) only
recognises exports that print the bare name, so most statement rows arrived
without a payee and therefore without the payee's default category.

:class:`PayeeMatcher` keeps the exact comparison and adds one step: a row
whose description **contains** the normalised name or alias of a payee as
whole words (``normalize_payee`` on both sides) is that payee's. When
one payee's name is part of another's, the longer one wins (``Corner Grocer
Deli`` over ``Corner Grocer``); a description naming two different payees
side by side (``Transfer from A to B``) is no match.
Names shorter than :data:`MIN_CONTAINED_CHARS` characters, or made of digits
only, are compared exactly and never searched for inside a description
(``BP``, ``7``).

A charge that came through a processor names two things (``PAYPAL
*STREAMBOX``), and a store number hides a short name (``BP#5521044``). So
when the whole description names nobody, or two payees, its **core** is
looked at: what payee clean-up left of it, the merchant
(``app.domain.categorisation.cleanup.clean``). The core is cut short and is
not always a merchant, so it answers in two cases only:

* clean-up stripped a processor (``via``): the same comparison is made with
  the core, and when the whole description named two payees, every one of
  them other than the core's must be that processor. A second payee further
  along the line, past the end of the core, still makes it no match;
* nothing was stripped and the whole description named nobody: the core must
  **be** a payee's name or alias (``BP``), not merely contain one.

A short name still matches only a core it equals. A line between people, a
transfer and a card payment have no merchant: the core of ``VENMO PAYMENT
1027 <a person>`` is the app, which is not who was paid, so
:meth:`PayeeMatcher.match_descriptor` never asks the core for those kinds.

No model, no network, nothing learned behind the person's back: what is
recognised is exactly the names and aliases they keep under "Categories,
payees and tags".
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Final

from app.domain.categorisation import cleanup
from app.domain.categorisation.cleanup import DescriptorKind
from app.domain.transactions.import_hash import normalize_payee

#: A name or alias shorter than this is never looked for inside a description.
MIN_CONTAINED_CHARS: Final = 4
#: Kinds of line whose core is not a merchant (module doc).
NO_MERCHANT_KINDS: Final[frozenset[DescriptorKind]] = frozenset({"p2p", "transfer", "card_payment"})


class PayeeMatcher:
    """Normalised names and aliases -> payee, exact or contained (module doc)."""

    def __init__(self, lookup: Mapping[str, uuid.UUID]) -> None:
        self._exact = dict(lookup)
        self._by_first_word: dict[str, list[tuple[str, uuid.UUID]]] = {}
        for key, payee_id in lookup.items():
            if len(key) < MIN_CONTAINED_CHARS or key.replace(" ", "").isdigit():
                continue
            first = key.split(" ", 1)[0]
            self._by_first_word.setdefault(first, []).append((key, payee_id))

    def match_descriptor(self, raw_payee: str | None) -> uuid.UUID | None:
        """The payee the bank's line ``raw_payee`` names, read with payee clean-up."""
        cleaned = cleanup.clean(raw_payee)
        if cleaned.kind in NO_MERCHANT_KINDS:
            return self.match(raw_payee)
        return self.match(raw_payee, core=cleaned.core, via=cleaned.via)

    def match(
        self, raw_payee: str | None, *, core: str | None = None, via: str | None = None
    ) -> uuid.UUID | None:
        """The payee ``raw_payee`` names, or None (unknown, or two fit equally).

        ``core`` and ``via``: the merchant and the processor clean-up read
        from ``raw_payee``; the core is tried when the whole text gives no
        answer, within the limits of the module doc.
        """
        named = self._fits(normalize_payee(raw_payee))
        chosen = _only(named)
        text = normalize_payee(core) if core else ""
        if chosen is not None or not text:
            return chosen
        if via is None:
            # Nothing was stripped: only a core that is a payee's name answers.
            return None if named else self._exact.get(text)
        behind = _only(self._fits(text))
        if behind is None:
            return None
        processor = via.replace(" ", "")
        for key, payee_id in named.items():
            if payee_id != behind and key.replace(" ", "") != processor:
                # Someone else is named past the end of the core.
                return None
        return behind

    def _fits(self, text: str) -> dict[str, uuid.UUID]:
        """The names the normalised ``text`` is, or contains as whole words."""
        if not text:
            return {}
        exact = self._exact.get(text)
        if exact is not None:
            return {text: exact}
        padded = f" {text} "
        fits: dict[str, uuid.UUID] = {}
        for word in dict.fromkeys(text.split()):
            for key, payee_id in self._by_first_word.get(word, ()):
                if f" {key} " in padded:
                    fits[key] = payee_id
        return fits


def _only(fits: Mapping[str, uuid.UUID]) -> uuid.UUID | None:
    """The one payee ``fits`` names (the longest name wins), or None."""
    if not fits:
        return None
    longest = max(fits, key=len)
    chosen = fits[longest]
    inside = f" {longest} "
    # Another payee's name elsewhere in the description: it could be either.
    if any(other != chosen and f" {key} " not in inside for key, other in fits.items()):
        return None
    return chosen

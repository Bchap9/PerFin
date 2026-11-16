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
when the whole description names nobody, or two payees, the same comparison
is made with its **core**: what payee clean-up left of it, the merchant
(``app.domain.categorisation.cleanup.clean``; the caller passes it). A short
name still matches only a core it equals.

No model, no network, nothing learned behind the person's back: what is
recognised is exactly the names and aliases they keep under "Categories,
payees and tags".
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Final

from app.domain.transactions.import_hash import normalize_payee

#: A name or alias shorter than this is never looked for inside a description.
MIN_CONTAINED_CHARS: Final = 4


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

    def match(self, raw_payee: str | None, *, core: str | None = None) -> uuid.UUID | None:
        """The payee ``raw_payee`` names, or None (unknown, or two fit equally).

        ``core``: the merchant clean-up read from ``raw_payee``; tried when
        the whole text gives no answer (module doc).
        """
        found = self._named(normalize_payee(raw_payee))
        if found is None and core:
            found = self._named(normalize_payee(core))
        return found

    def _named(self, text: str) -> uuid.UUID | None:
        """The payee the normalised ``text`` is, or contains as whole words."""
        if not text:
            return None
        exact = self._exact.get(text)
        if exact is not None:
            return exact
        padded = f" {text} "
        fits: dict[str, uuid.UUID] = {}
        for word in dict.fromkeys(text.split()):
            for key, payee_id in self._by_first_word.get(word, ()):
                if f" {key} " in padded:
                    fits[key] = payee_id
        if not fits:
            return None
        longest = max(fits, key=len)
        chosen = fits[longest]
        inside = f" {longest} "
        # Another payee's name elsewhere in the description: it could be either.
        if any(other != chosen and f" {key} " not in inside for key, other in fits.items()):
            return None
        return chosen

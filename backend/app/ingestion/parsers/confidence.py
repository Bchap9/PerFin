# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-field confidence for parsed documents (INGESTION.md "Commit").

Every field a document parser produces gets a score in 0..1. A field below
the threshold (``PERFIN_INGEST_CONFIDENCE_THRESHOLD``, default
:data:`DEFAULT_THRESHOLD`) is listed in the record's ``low_confidence``; the
importer writes such a transaction with ``review_state = pending`` (left out
of reports until a person confirms it) and raises a ``low_confidence_field``
review item naming the fields.

The scores are deterministic and explainable, built from what the parser
knows about how it read the value:

* a value read in the format the institution's parser declares: 1.0, a
  generic parser's reading of a recognised layout: :data:`GENERIC`;
* a numeric date whose day/month order could not be told: :data:`GUESSED`;
* a yearless date placed in the statement period: unchanged; outside it:
  :data:`GUESSED`;
* an amount confirmed by the running balance: 1.0 whatever the other
  factors; contradicted by it: :data:`CONTRADICTED`;
* OCR: multiplied by the lowest Tesseract word confidence of the cell
  (0..100 scaled to 0..1);
* a statement that does not reconcile: every row capped at
  :data:`UNRECONCILED`; one that cannot be checked: :data:`UNCHECKED`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

DEFAULT_THRESHOLD: Final = 0.85
GENERIC: Final = 0.95
DERIVED: Final = 0.9
INFERRED: Final = 0.7
GUESSED: Final = 0.6
MISSING: Final = 0.5
UNRECONCILED: Final = 0.5
UNCHECKED: Final = 0.8
CONTRADICTED: Final = 0.4


def scaled(base: float, ocr: float | None) -> float:
    """``base`` lowered by an OCR confidence (0..1), rounded to 4 places."""
    value = base if ocr is None else base * max(0.0, min(1.0, ocr))
    return round(max(0.0, min(1.0, value)), 4)


def low_fields(fields: Mapping[str, float], threshold: float) -> list[str]:
    """Names of the fields below ``threshold``, sorted."""
    return sorted(name for name, value in fields.items() if value < threshold)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Extractors by kind (ADR-0006).

An extractor is ``fn(data: bytes, options: ExtractOptions, kind: str) ->
ExtractResult``; it raises :class:`app.ingestion.extract.common.ExtractFailure`
for input it cannot handle. Modules register at import with :func:`register`;
:func:`builtin_extractors` imports the shipped ones. Only the sandbox child
(``app.ingestion.sandbox.child``) calls this: the API and the domain never
import ``app.ingestion.extract`` (``tests/unit/test_import_boundaries.py``),
and the worker reaches extractors only through the subprocess.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Final

from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult

Extractor = Callable[[bytes, ExtractOptions, str], ExtractResult]

BUILTIN_MODULES: Final = (
    "app.ingestion.extract.delimited",
    "app.ingestion.extract.ofx",
    "app.ingestion.extract.qif",
    "app.ingestion.extract.pdf",
    "app.ingestion.extract.image",
    "app.ingestion.extract.eml",
    "app.ingestion.extract.archive",
    "app.ingestion.extract.xlsx",
)

_EXTRACTORS: dict[str, Extractor] = {}


def register(kind: str, extractor: Extractor) -> None:
    existing = _EXTRACTORS.get(kind)
    if existing is not None and existing is not extractor:
        raise ValueError(f"an extractor is already registered for {kind!r}")
    _EXTRACTORS[kind] = extractor


def builtin_extractors() -> dict[str, Extractor]:
    """Import the shipped extractor modules and return the registry (a copy)."""
    for name in BUILTIN_MODULES:
        importlib.import_module(name)
    return dict(_EXTRACTORS)

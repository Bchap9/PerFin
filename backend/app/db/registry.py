# SPDX-License-Identifier: AGPL-3.0-or-later
"""Import every models module so ``Base.metadata`` is complete.

Alembic's ``env.py`` and the test fixtures call ``import_all_models()``. New
bounded contexts are picked up automatically from ``app.domain.<context>.models``.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from types import ModuleType

_CORE_MODULES = ("app.db.models", "app.auth.models")


def _import_if_present(name: str) -> ModuleType | None:
    try:
        spec = importlib.util.find_spec(name)
    except ModuleNotFoundError:
        return None
    if spec is None:
        return None
    return importlib.import_module(name)


def import_all_models() -> list[str]:
    """Import all model modules; return their dotted names in import order."""
    imported: list[str] = []
    for name in _CORE_MODULES:
        if _import_if_present(name) is not None:
            imported.append(name)
    domain = importlib.import_module("app.domain")
    for info in sorted(pkgutil.iter_modules(domain.__path__), key=lambda i: i.name):
        if not info.ispkg:
            continue
        name = f"app.domain.{info.name}.models"
        if _import_if_present(name) is not None:
            imported.append(name)
    return imported

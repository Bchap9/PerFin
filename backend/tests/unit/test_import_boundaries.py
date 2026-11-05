# SPDX-License-Identifier: AGPL-3.0-or-later
"""Layer boundaries (docs/DEVELOPMENT.md, "API conventions"; ADR-0006).

Nothing in ``app/api`` or ``app/domain`` may import ``app.ingestion.extract``:
parsing untrusted files happens only in the worker's sandboxed extraction
stage, never in the API process. The check walks the AST of every module, so
it sees imports inside functions and ``TYPE_CHECKING`` blocks too, and it
resolves relative imports.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[2] / "app"
FORBIDDEN = "app.ingestion.extract"
GUARDED_PACKAGES = ("api", "domain")


def _module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative(module: str | None, level: int, current: str, is_package: bool) -> str:
    package = current if is_package else current.rpartition(".")[0]
    for _ in range(level - 1):
        package = package.rpartition(".")[0]
    return f"{package}.{module}" if module else package


def imported_modules(source: str, module_name: str, *, is_package: bool = False) -> set[str]:
    """Every dotted name ``source`` imports, including ``from x import y`` as ``x.y``."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _resolve_relative(node.module, node.level, module_name, is_package)
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute | ast.Name)
            and (getattr(node.func, "attr", None) or getattr(node.func, "id", None))
            in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
    return found


def violates(names: set[str]) -> set[str]:
    return {n for n in names if n == FORBIDDEN or n.startswith(FORBIDDEN + ".")}


def _guarded_files() -> list[Path]:
    files: list[Path] = []
    for package in GUARDED_PACKAGES:
        files.extend(sorted((APP_DIR / package).rglob("*.py")))
    return files


def test_guarded_packages_exist() -> None:
    files = _guarded_files()
    assert (APP_DIR / "api" / "__init__.py") in files
    assert (APP_DIR / "api" / "routers" / "health.py") in files
    assert (APP_DIR / "domain" / "__init__.py") in files


@pytest.mark.parametrize("path", _guarded_files(), ids=lambda p: str(p.relative_to(APP_DIR)))
def test_api_and_domain_never_import_extraction(path: Path) -> None:
    name = _module_name(path, APP_DIR)
    names = imported_modules(
        path.read_text(encoding="utf-8"), name, is_package=path.name == "__init__.py"
    )
    assert violates(names) == set(), f"{name} imports the extraction stage"


@pytest.mark.parametrize(
    ("source", "module", "is_package"),
    [
        ("import app.ingestion.extract", "app.api.routers.x", False),
        ("import app.ingestion.extract.pdf as pdf", "app.api.routers.x", False),
        ("from app.ingestion.extract import pdf", "app.domain.ledger.service", False),
        ("from app.ingestion import extract", "app.domain.ledger.service", False),
        ("from ...ingestion import extract", "app.api.routers.x", False),
        ("from ...ingestion.extract.pdf import parse", "app.domain.ledger", True),
        ("def f():\n    from app.ingestion.extract import pdf\n", "app.api.x", False),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"
            "    from app.ingestion.extract import pdf\n",
            "app.api.x",
            False,
        ),
        ("import importlib\nimportlib.import_module('app.ingestion.extract')", "app.api.x", False),
    ],
)
def test_checker_catches_every_import_form(source: str, module: str, is_package: bool) -> None:
    assert violates(imported_modules(source, module, is_package=is_package))


@pytest.mark.parametrize(
    "source",
    [
        "from app.ingestion import intake",
        "import app.ingestion.classify",
        "from app.ingestion.extractors_registry import x",
        "from app.db import models",
    ],
)
def test_checker_allows_other_imports(source: str) -> None:
    assert violates(imported_modules(source, "app.api.routers.x")) == set()

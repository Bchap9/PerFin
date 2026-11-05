# SPDX-License-Identifier: AGPL-3.0-or-later
"""Serve the built single-page app from the API process (SECURITY.md F1).

The image copies ``frontend/dist`` to ``/app/static`` and sets
``PERFIN_STATIC_DIR``. When that directory holds an ``index.html``:

* ``/static/*`` (Vite's content-hashed bundles, ``build.assetsDir`` in
  ``frontend/vite.config.ts``) is served by ``StaticFiles`` without following
  symlinks (not Vite's default ``/assets/``: ``/assets`` and
  ``/assets/<id>`` are the SPA's Assets pages); the headers middleware gives successful
  responses a one-year ``immutable`` cache and everything else ``no-store``.
* Top-level files of the build (``favicon.ico``, ``robots.txt``, ...) are
  served by exact name only, from a list made at startup.
* Any other ``GET``/``HEAD`` that matched no route and is not under
  ``/api/`` returns ``index.html`` so client-side routes (``/accounts``)
  load the app. The fallback is a 404 handler, not a catch-all route, so
  real routes keep their ``405 Method Not Allowed`` and API paths keep
  their problem-details 404.

Every response still passes through the security-headers middleware, so the
strict CSP (no inline script or style, Trusted Types) applies to the app.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import ProblemError

ASSETS_PREFIX = "/static"
NON_SPA_PREFIXES: tuple[str, ...] = ("/api/", "/static/")
NON_SPA_PATHS: frozenset[str] = frozenset({"/api", "/static", "/healthz", "/readyz"})

ExceptionHandler = Callable[[Request, Any], Response | Awaitable[Response]]


def _is_spa_path(path: str) -> bool:
    if path in NON_SPA_PATHS:
        return False
    return not any(path.startswith(prefix) for prefix in NON_SPA_PREFIXES)


def _root_files(static_dir: Path) -> dict[str, Path]:
    """Regular, non-hidden files directly in the build directory (no symlinks)."""
    files: dict[str, Path] = {}
    for entry in static_dir.iterdir():
        if entry.name.startswith(".") or entry.name == "index.html":
            continue
        if entry.is_symlink() or not entry.is_file():
            continue
        files[f"/{entry.name}"] = entry
    return files


def install_spa(app: FastAPI, static_dir: Path | None) -> bool:
    """Serve the SPA from ``static_dir``; return False (and do nothing) if absent.

    Call after ``install_error_handlers`` and after the routers are included.
    """
    if static_dir is None:
        return False
    index = static_dir / "index.html"
    if not index.is_file():
        return False
    index_html = index.read_bytes()
    assets = static_dir / ASSETS_PREFIX.lstrip("/")
    if assets.is_dir():
        app.mount(
            ASSETS_PREFIX,
            StaticFiles(directory=assets, follow_symlink=False),
            name="spa-assets",
        )
    root_files = _root_files(static_dir)
    problem_handler: ExceptionHandler = app.exception_handlers[ProblemError]
    http_handler: ExceptionHandler = app.exception_handlers[StarletteHTTPException]

    async def not_found(request: Request, exc: Exception) -> Response:
        path = request.url.path
        if (
            not isinstance(exc, ProblemError)
            and request.method in ("GET", "HEAD")
            and _is_spa_path(path)
        ):
            root_file = root_files.get(path)
            if root_file is not None:
                return FileResponse(root_file)
            return Response(index_html, media_type="text/html; charset=utf-8")
        handler = problem_handler if isinstance(exc, ProblemError) else http_handler
        result = handler(request, exc)
        return await result if inspect.isawaitable(result) else result

    app.add_exception_handler(404, not_found)
    return True

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Security response headers and Content Security Policy (SECURITY.md F1, F6,
F7, F9, C4).

:class:`SecurityHeadersMiddleware` adds to every HTTP response:

* ``Content-Security-Policy`` (:func:`build_csp`): nothing inline, nothing
  third-party, no framing, no plugins, ``base-uri 'none'``, and
  ``require-trusted-types-for 'script'``.
* ``X-Frame-Options: DENY``, ``X-Content-Type-Options: nosniff``,
  ``Referrer-Policy: no-referrer``, a deny-all ``Permissions-Policy``,
  ``Cross-Origin-Opener-Policy: same-origin``,
  ``Cross-Origin-Resource-Policy: same-origin``.
* ``Strict-Transport-Security`` unless ``PERFIN_DEV_MODE`` is on.
* ``Cache-Control: no-store`` on every response except hashed static assets
  under ``/static/``, which get a one-year ``immutable`` cache.

A handler may set its own ``Content-Security-Policy`` (downloads use
:func:`attachment_headers`, which adds ``sandbox``); the middleware never
replaces one that is already present. Every other header is enforced.

Trusted Types decision
----------------------
``require-trusted-types-for 'script'`` is on by default
(``PERFIN_CSP_TRUSTED_TYPES=true``). React 18 only reaches a Trusted Types
sink (``innerHTML``) through ``dangerouslySetInnerHTML``, which the frontend
ESLint config bans, and when rendering a literal ``<script>`` element, which
the app never does; the production bundle therefore runs unchanged under
enforcement. The switch exists so an operator can turn enforcement off if a
future dependency trips it, without patching code. Turning it off weakens
F1 and is meant as a temporary measure while the offending sink is fixed.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from urllib.parse import quote

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

STATIC_PREFIXES: tuple[str, ...] = ("/static/",)
STATIC_CACHE_CONTROL = "public, max-age=31536000, immutable"
NO_STORE = "no-store"
HSTS_VALUE = "max-age=63072000; includeSubDomains"

_CSP_BASE: tuple[tuple[str, str], ...] = (
    ("default-src", "'none'"),
    ("script-src", "'self'"),
    ("style-src", "'self'"),
    ("img-src", "'self' data: blob:"),
    ("font-src", "'self'"),
    ("connect-src", "'self'"),
    ("manifest-src", "'self'"),
    ("worker-src", "'self'"),
    ("frame-ancestors", "'none'"),
    ("base-uri", "'none'"),
    ("form-action", "'self'"),
    ("object-src", "'none'"),
)

# Features PerFin never uses. WebAuthn (publickey-credentials-*) keeps its
# default 'self' allowlist and is deliberately not listed.
PERMISSIONS_POLICY = ", ".join(
    f"{feature}=()"
    for feature in (
        "accelerometer",
        "autoplay",
        "bluetooth",
        "browsing-topics",
        "camera",
        "display-capture",
        "encrypted-media",
        "geolocation",
        "gyroscope",
        "hid",
        "magnetometer",
        "microphone",
        "midi",
        "payment",
        "picture-in-picture",
        "serial",
        "usb",
        "xr-spatial-tracking",
    )
)

# Download responses: the document is rendered (if at all) in an opaque
# origin with no scripts, forms, plugins or same-origin access.
DOWNLOAD_CSP = "sandbox; default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


def build_csp(*, trusted_types: bool = True) -> str:
    """The application CSP. ``trusted_types`` adds ``require-trusted-types-for``."""
    directives = [f"{name} {value}" for name, value in _CSP_BASE]
    if trusted_types:
        directives.append("require-trusted-types-for 'script'")
    return "; ".join(directives)


def security_headers(*, csp: str, hsts: bool) -> list[tuple[str, str]]:
    """Headers enforced on every response (CSP is only a default)."""
    headers = [
        ("X-Frame-Options", "DENY"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Permissions-Policy", PERMISSIONS_POLICY),
        ("Cross-Origin-Opener-Policy", "same-origin"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
        ("X-Permitted-Cross-Domain-Policies", "none"),
    ]
    if hsts:
        headers.append(("Strict-Transport-Security", HSTS_VALUE))
    return headers


def cache_control_for(path: str, static_prefixes: Sequence[str] = STATIC_PREFIXES) -> str:
    """``no-store`` for everything except fingerprinted static assets."""
    if any(path.startswith(prefix) for prefix in static_prefixes):
        return STATIC_CACHE_CONTROL
    return NO_STORE


class SecurityHeadersMiddleware:
    """Add the security headers above to every HTTP response."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        hsts: bool,
        trusted_types: bool = True,
        static_prefixes: Sequence[str] = STATIC_PREFIXES,
    ) -> None:
        self.app = app
        self.csp = build_csp(trusted_types=trusted_types)
        self.enforced = security_headers(csp=self.csp, hsts=hsts)
        self.static_prefixes = tuple(static_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        cache_control = cache_control_for(path, self.static_prefixes)
        is_static = cache_control != NO_STORE

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.enforced:
                    headers[name] = value
                if "content-security-policy" not in headers:
                    headers["Content-Security-Policy"] = self.csp
                status = int(message.get("status", 200))
                # Static assets: long cache only for successful responses; an
                # error page or redirect under /static/ must not be cached.
                if is_static and 200 <= status < 300:
                    headers["Cache-Control"] = cache_control
                else:
                    headers["Cache-Control"] = NO_STORE
                    if "pragma" in headers:
                        del headers["pragma"]
            await send(message)

        await self.app(scope, receive, send_wrapper)


_UNSAFE_FILENAME_CHARS = re.compile(r'[\x00-\x1f\x7f"\\/;:*?<>|]')
_SAFE_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")


def _ascii_fallback(filename: str) -> str:
    normalized = unicodedata.normalize("NFKD", filename)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", ascii_only).strip(" .")
    return cleaned[:150] or "download"


def attachment_headers(filename: str, media_type: str) -> dict[str, str]:
    """Headers for serving a stored document back to a browser (SECURITY.md C4).

    ``media_type`` must be the type sniffed from magic bytes at intake, never
    the client-supplied one. The response is always an attachment, cannot be
    MIME-sniffed into HTML, and carries a ``sandbox`` CSP so that even if a
    browser renders it, it runs in an opaque origin without scripts. The
    original filename is sanitised (no control characters, quotes, path
    separators) and also sent RFC 6266/5987-encoded for non-ASCII names.
    """
    media = media_type.strip().lower()
    if not _SAFE_MEDIA_TYPE.fullmatch(media):
        media = "application/octet-stream"
    display = _UNSAFE_FILENAME_CHARS.sub("_", unicodedata.normalize("NFC", filename)).strip(" .")
    display = display[:200] or "download"
    disposition = (
        f'attachment; filename="{_ascii_fallback(display)}"; '
        f"filename*=UTF-8''{quote(display, safe='')}"
    )
    return {
        "Content-Type": media,
        "Content-Disposition": disposition,
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": DOWNLOAD_CSP,
        "Cache-Control": NO_STORE,
        "Cross-Origin-Resource-Policy": "same-origin",
    }

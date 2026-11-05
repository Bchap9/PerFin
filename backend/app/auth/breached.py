# SPDX-License-Identifier: AGPL-3.0-or-later
"""Breached-password checks (AUTH.md "Passwords", SECURITY.md A1).

* **Local list** (always on): ``data/breached-passwords.txt.gz``, common
  passwords of 12..128 characters from public breach compilations
  (provenance and licence in ``data/README.md``). Loaded once, lazily, into a
  ``frozenset``; the check is case-insensitive.
* **Have I Been Pwned** (opt-in, ``PERFIN_HIBP_CHECK_ENABLED``): the
  k-anonymity range API. Only the first five hex characters of the
  password's SHA-1 leave the host; the response (padded with
  ``Add-Padding: true`` so its size does not hint at the prefix) is searched
  locally. The endpoint is a fixed public host, never taken from input; the
  client follows no redirects, caps the response size and times out. A
  failed lookup does not block the user (the local list still applies) and
  is logged without any password material.
"""

from __future__ import annotations

import gzip
import hashlib
import threading
from collections.abc import Callable
from importlib import resources
from typing import Final

import httpx

from app.core.logging import get_logger

DATA_PACKAGE: Final = "app.auth"
DATA_FILE: Final = "data/breached-passwords.txt.gz"
HIBP_RANGE_URL: Final = "https://api.pwnedpasswords.com/range/"
HIBP_TIMEOUT_SECONDS: Final = 5.0
#: A padded range response is roughly 800-1000 lines of 40 bytes.
HIBP_MAX_RESPONSE_BYTES: Final = 1024 * 1024
USER_AGENT: Final = "PerFin-password-check"

log = get_logger(__name__)

_lock = threading.Lock()
_entries: frozenset[str] | None = None


def _load() -> frozenset[str]:
    ref = resources.files(DATA_PACKAGE).joinpath(*DATA_FILE.split("/"))
    with ref.open("rb") as raw, gzip.open(raw, "rt", encoding="utf-8") as handle:
        return frozenset(line.rstrip("\n") for line in handle if line.strip())


def local_list() -> frozenset[str]:
    """The bundled list (loaded on first use)."""
    global _entries
    with _lock:
        if _entries is None:
            _entries = _load()
        return _entries


def in_local_list(password: str) -> bool:
    """True when ``password`` (case-insensitively) is on the bundled list."""
    return password.lower() in local_list()


def _sha1_hex(password: str) -> str:
    # SHA-1 is what the HIBP range API is keyed on; it is not used for security here.
    # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
    digest = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False)
    return digest.hexdigest().upper()


def _fetch_range(prefix: str, client: httpx.Client) -> str | None:
    with client.stream(
        "GET",
        HIBP_RANGE_URL + prefix,
        headers={"Add-Padding": "true", "User-Agent": USER_AGENT},
    ) as response:
        if response.status_code != 200:
            return None
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > HIBP_MAX_RESPONSE_BYTES:
                return None
    return body.decode("ascii", errors="replace")


ClientFactory = Callable[[], httpx.Client]


def _default_client() -> httpx.Client:
    return httpx.Client(
        timeout=HIBP_TIMEOUT_SECONDS,
        follow_redirects=False,
        # HTTPS_PROXY (the optional egress proxy) is honoured.
        trust_env=True,
    )


def in_hibp(password: str, *, client_factory: ClientFactory = _default_client) -> bool | None:
    """``True``/``False`` from the HIBP range API, ``None`` when unavailable."""
    digest = _sha1_hex(password)
    prefix, suffix = digest[:5], digest[5:]
    try:
        with client_factory() as client:
            text = _fetch_range(prefix, client)
    except httpx.HTTPError as exc:
        log.warning("HIBP range lookup failed", extra={"error": type(exc).__name__})
        return None
    if text is None:
        log.warning("HIBP range lookup returned an unusable response")
        return None
    for line in text.splitlines():
        candidate, _, count = line.strip().partition(":")
        if candidate.upper() == suffix:
            # Padding entries carry a count of 0.
            return count.strip() not in ("", "0")
    return False


def is_breached(
    password: str,
    *,
    hibp: bool = False,
    client_factory: ClientFactory = _default_client,
) -> bool:
    """True when ``password`` is known from a breach (local list, then HIBP if enabled)."""
    if in_local_list(password):
        return True
    if hibp:
        return in_hibp(password, client_factory=client_factory) is True
    return False

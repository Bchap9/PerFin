#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PerFin machine-learning adapter (ADR-0010, docs/ML.md). Optional.

A small HTTP service the worker asks "what is this document?" when its own
deterministic readers are unsure. It holds the prompt and the answer's JSON
schema and forwards one request at a time to a local model runtime (Ollama,
``PERFIN_ML_RUNTIME``), which runs the model on the host's GPU or CPU. It is
the only thing the worker talks to; the runtime is never exposed to PerFin.

Policy, deliberately narrow:

* ``POST /v1/document`` only, with ``Authorization: Bearer <PERFIN_ML_TOKEN>``
  (compared in constant time). Bodies are capped (:data:`MAX_BODY`); the
  text sent to the model is cut to :data:`MAX_TEXT` characters.
* The model must answer with JSON that matches :data:`SCHEMA` (the runtime's
  structured-output mode). The answer is normalised here and checked again
  by the worker, which treats it as an untrusted suggestion: nothing it
  says is written to the ledger without a person's confirmation.
* Nothing is stored and no document content is logged: one JSON line per
  request with its status, size and duration.
* ``GET /health`` says whether the runtime answers and which model is set.

Standard library only. Environment:
    PERFIN_ML_LISTEN        address:port (default 0.0.0.0:3003)
    PERFIN_ML_TOKEN         shared bearer token (required, at least 32 characters)
    PERFIN_ML_RUNTIME       Ollama base URL (default http://ollama:11434)
    PERFIN_ML_MODEL         text model (default qwen2.5:7b-instruct)
    PERFIN_ML_VISION_MODEL  optional vision model for page images (default: none)
    PERFIN_ML_TIMEOUT       seconds per model call (default 90)
    PERFIN_ML_CONTEXT       context window in tokens (default 8192)
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final

MAX_BODY: Final = 6 * 1024 * 1024
MAX_TEXT: Final = 24_000
MAX_ACCOUNTS: Final = 10
KINDS: Final = (
    "statement",
    "receipt",
    "invoice",
    "tax_form",
    "payslip",
    "insurance",
    "other",
    "unknown",
)
ACCOUNT_TYPES: Final = (
    "checking",
    "savings",
    "credit_card",
    "brokerage",
    "retirement",
    "loan",
    "mortgage",
    "line_of_credit",
    "other",
)
#: The answer every model call must produce (Ollama ``format``).
SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "institution": {"type": ["string", "null"], "maxLength": 120},
        "accounts": {
            "type": "array",
            "maxItems": MAX_ACCOUNTS,
            "items": {
                "type": "object",
                "properties": {
                    "last4": {"type": ["string", "null"], "pattern": "^[0-9]{4}$"},
                    "name": {"type": ["string", "null"], "maxLength": 80},
                    "type": {"type": ["string", "null"], "enum": [*ACCOUNT_TYPES, None]},
                    "currency": {"type": ["string", "null"], "pattern": "^[A-Z]{3}$"},
                },
                "required": ["last4", "name", "type", "currency"],
            },
        },
        "period_start": {"type": ["string", "null"], "format": "date"},
        "period_end": {"type": ["string", "null"], "format": "date"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["kind", "institution", "accounts", "period_start", "period_end", "confidence"],
}
PROMPT: Final = """You read financial documents for a personal finance app.
Answer only with JSON matching the schema. Use only facts printed in the document:
- kind: what the document is (statement = bank, card, loan or brokerage statement or
  an account activity export; tax_form = W-2, 1099 and similar; unknown if unsure).
- institution: the bank, card issuer, broker or lender that issued it, as printed.
- accounts: every account the document is about. last4 = the last four digits of its
  account number exactly as printed (null if no digits are printed; never guess).
  name = the account's own name or nickname (e.g. "Individual", "ROTH IRA").
  type = the kind of account. currency = ISO code if printed.
- period_start/period_end: the statement period as YYYY-MM-DD, null if not printed.
- confidence: how sure you are overall, 0 to 1.
Never invent account numbers, names or dates."""


class Config:
    def __init__(self, env: dict[str, str]) -> None:
        self.listen = env.get("PERFIN_ML_LISTEN", "0.0.0.0:3003")
        self.token = env.get("PERFIN_ML_TOKEN", "")
        self.runtime = env.get("PERFIN_ML_RUNTIME", "http://ollama:11434").rstrip("/")
        self.model = env.get("PERFIN_ML_MODEL", "qwen2.5:7b-instruct")
        self.vision_model = env.get("PERFIN_ML_VISION_MODEL", "") or None
        self.timeout = float(env.get("PERFIN_ML_TIMEOUT", "90"))
        self.context = int(env.get("PERFIN_ML_CONTEXT", "8192"))
        if len(self.token) < 32:
            raise SystemExit("perfin-ml: PERFIN_ML_TOKEN must be set (at least 32 characters)")
        if not self.runtime.startswith(("http://", "https://")):
            raise SystemExit("perfin-ml: PERFIN_ML_RUNTIME must be an http(s) URL")


def log(**fields: Any) -> None:
    print(json.dumps({"ts": round(time.time(), 3), **fields}), flush=True)


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    clean = " ".join(value.split())[:limit]
    return clean or None


def normalise(answer: Any) -> dict[str, Any]:
    """The model's answer reduced to :data:`SCHEMA`'s fields and types (anything else dropped)."""
    data = answer if isinstance(answer, dict) else {}
    kind = data.get("kind")
    accounts: list[dict[str, Any]] = []
    raw = data.get("accounts")
    for entry in raw[:MAX_ACCOUNTS] if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        last4 = entry.get("last4")
        currency = entry.get("currency")
        kind_of = entry.get("type")
        accounts.append(
            {
                "last4": last4
                if isinstance(last4, str) and len(last4) == 4 and last4.isdigit()
                else None,
                "name": _text(entry.get("name"), 80),
                "type": kind_of if kind_of in ACCOUNT_TYPES else None,
                "currency": currency
                if isinstance(currency, str) and len(currency) == 3 and currency.isupper()
                else None,
            }
        )
    confidence = data.get("confidence")
    return {
        "kind": kind if kind in KINDS else "unknown",
        "institution": _text(data.get("institution"), 120),
        "accounts": accounts,
        "period_start": _text(data.get("period_start"), 10),
        "period_end": _text(data.get("period_end"), 10),
        "confidence": float(confidence)
        if isinstance(confidence, int | float) and 0 <= confidence <= 1
        else 0.0,
    }


class Runtime:
    """The Ollama runtime: one request at a time (a GPU runs one model call well)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.lock = threading.Lock()

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - the configured runtime URL
            f"{self.config.runtime}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # The runtime URL is checked to be http(s) at start-up (Config).
        with urllib.request.urlopen(  # noqa: S310  # nosec B310
            request, timeout=self.config.timeout
        ) as response:
            parsed = json.loads(response.read(MAX_BODY))
        if not isinstance(parsed, dict):
            raise ValueError("runtime answered with no object")
        return parsed

    def document(self, text: str, page_png: str | None) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "user", "content": text[:MAX_TEXT]}
        model = self.config.model
        if page_png and self.config.vision_model:
            message["images"] = [page_png]
            model = self.config.vision_model
        body = {
            "model": model,
            "stream": False,
            "format": SCHEMA,
            "options": {"temperature": 0, "num_ctx": self.config.context},
            "messages": [{"role": "system", "content": PROMPT}, message],
        }
        with self.lock:
            reply = self._post("/api/chat", body)
        content = reply.get("message", {}).get("content", "")
        answer = normalise(json.loads(content) if isinstance(content, str) else {})
        return {**answer, "model": model}

    def health(self) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(  # noqa: S310 - the configured runtime URL
                f"{self.config.runtime}/api/tags", timeout=5
            ) as response:  # nosec B310
                tags = json.loads(response.read(MAX_BODY))
        except (OSError, ValueError):
            return {"status": "runtime_unreachable", "model": self.config.model}
        names = {m.get("name") for m in tags.get("models", []) if isinstance(m, dict)}
        ready = self.config.model in names or f"{self.config.model}:latest" in names
        return {
            "status": "ok" if ready else "model_missing",
            "model": self.config.model,
            "vision_model": self.config.vision_model,
        }


def make_handler(config: Config, runtime: Runtime) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "perfin-ml"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            return  # one JSON line per request instead (no paths with content)

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path != "/health":
                self._send(404, {"error": "not_found"})
                return
            self._send(200, runtime.health())

        def do_POST(self) -> None:
            started = time.monotonic()
            status, size = self._document()
            log(
                path=self.path,
                status=status,
                bytes=size,
                ms=round((time.monotonic() - started) * 1000),
            )

        def _document(self) -> tuple[int, int]:
            if self.path != "/v1/document":
                self._send(404, {"error": "not_found"})
                return 404, 0
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), f"Bearer {config.token}".encode()):
                self._send(401, {"error": "unauthorized"})
                return 401, 0
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                self._send(413, {"error": "body_size"})
                return 413, max(length, 0)
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                self._send(400, {"error": "bad_json"})
                return 400, length
            text = body.get("text") if isinstance(body, dict) else None
            page = body.get("page_png") if isinstance(body, dict) else None
            if not isinstance(text, str) or not text.strip():
                self._send(422, {"error": "text_required"})
                return 422, length
            try:
                answer = runtime.document(text, page if isinstance(page, str) else None)
            except (OSError, ValueError, urllib.error.URLError):
                self._send(502, {"error": "runtime_failed"})
                return 502, length
            self._send(200, answer)
            return 200, length

    return Handler


def healthcheck(config: Config) -> int:
    port = config.listen.rpartition(":")[2]
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=3):  # nosec B310
            return 0
    except OSError:
        return 1


def main(argv: list[str]) -> int:
    config = Config(dict(os.environ))
    if argv[1:2] == ["healthcheck"]:
        return healthcheck(config)
    host, _, port = config.listen.rpartition(":")
    server = ThreadingHTTPServer((host, int(port)), make_handler(config, Runtime(config)))
    log(event="listening", listen=config.listen, model=config.model)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

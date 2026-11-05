# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every request body of every route forbids unknown fields (SECURITY.md F4).

Mass assignment needs an input model that silently accepts fields it does not
declare. This walks the OpenAPI document of the real app, so a router added
in any later phase is covered without touching this file: each request body
must be a named Pydantic model (reached directly, through a discriminated
union or as an optional body) whose JSON schema says
``additionalProperties: false``, and so must every model nested in it.
Free-form JSON objects are allowed only where listed below with the reason.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.settings import Settings
from app.main import create_app

pytestmark = pytest.mark.security

#: Request bodies that are not JSON models, with the reason.
NON_MODEL_BODIES: dict[tuple[str, str], str] = {
    # OpenID Connect Back-Channel Logout 1.0 mandates a form post with a single
    # `logout_token` field, which the handler reads by name.
    ("post", "/api/v1/auth/oidc/backchannel-logout"): "application/x-www-form-urlencoded",
    # Document upload: one binary part named `file`, streamed by the handler
    # (app.ingestion.intake.multipart) into the encrypted store; the declared
    # schema is closed (additionalProperties false) and nothing else is read.
    ("post", "/api/v1/documents"): "multipart/form-data",
    # Full import: one binary part named `file` (a PerFin export archive),
    # streamed by the handler into the encrypted quarantine area; nothing
    # else is read (the passphrase of an encrypted archive is a header).
    ("post", "/api/v1/import"): "multipart/form-data",
}

#: ``(model, field)`` pairs that are deliberately free-form JSON objects.
FREE_FORM_FIELDS: dict[tuple[str, str], str] = {
    # The browser's PublicKeyCredential JSON, parsed and verified field by
    # field by py_webauthn; nothing from it is copied onto a model.
    ("WebAuthnProofIn", "credential"): "WebAuthn assertion",
    ("WebAuthnRegisterIn", "credential"): "WebAuthn attestation",
}


def _openapi() -> dict[str, Any]:
    schema: dict[str, Any] = create_app(Settings(dev_mode=True)).openapi()
    return schema


def _component(document: dict[str, Any], ref: str) -> tuple[str, dict[str, Any]]:
    name = ref.rsplit("/", 1)[-1]
    found: dict[str, Any] = document["components"]["schemas"][name]
    return name, found


def _violations(document: dict[str, Any], node: Any, *, owner: str, seen: set[str]) -> list[str]:
    """Models reachable from ``node`` that accept unknown fields."""
    if not isinstance(node, dict):
        return []
    found: list[str] = []
    if "$ref" in node:
        name, model = _component(document, node["$ref"])
        if name in seen:
            return []
        seen.add(name)
        if model.get("type") == "object" or "properties" in model:
            if model.get("additionalProperties") is not False:
                found.append(f"{name} (in {owner}) accepts unknown fields")
            for field, sub in model.get("properties", {}).items():
                if (name, field) in FREE_FORM_FIELDS:
                    continue
                found += _violations(document, sub, owner=f"{name}.{field}", seen=seen)
        return found
    if node.get("type") == "object" and node.get("additionalProperties") not in (None, False):
        found.append(f"{owner} is a free-form object")
    if node.get("type") == "object" and "properties" in node:
        if node.get("additionalProperties") is not False:
            found.append(f"{owner} is an inline object that accepts unknown fields")
        for field, sub in node["properties"].items():
            found += _violations(document, sub, owner=f"{owner}.{field}", seen=seen)
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in node.get(key, []):
            found += _violations(document, sub, owner=owner, seen=seen)
    if "items" in node:
        found += _violations(document, node["items"], owner=f"{owner}[]", seen=seen)
    return found


def _bodies(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            body = operation.get("requestBody")
            if body is not None:
                out[(method, path)] = body
    return out


def test_every_request_body_is_a_model_that_forbids_unknown_fields() -> None:
    document = _openapi()
    bodies = _bodies(document)
    assert len(bodies) >= 20, "the walk found suspiciously few request bodies"
    problems: list[str] = []
    for (method, path), body in sorted(bodies.items()):
        content: dict[str, Any] = body["content"]
        if (method, path) in NON_MODEL_BODIES:
            assert list(content) == [NON_MODEL_BODIES[(method, path)]], (method, path)
            continue
        assert list(content) == ["application/json"], f"{method.upper()} {path}: {list(content)}"
        schema = content["application/json"]["schema"]
        refs = (
            [schema] if "$ref" in schema else [*schema.get("anyOf", []), *schema.get("oneOf", [])]
        )
        if not any("$ref" in ref for ref in refs):
            problems.append(f"{method.upper()} {path}: body is not a named model")
        problems += [
            f"{method.upper()} {path}: {v}"
            for v in _violations(document, schema, owner="body", seen=set())
        ]
    assert problems == []


def test_exemptions_are_still_needed() -> None:
    """A stale exemption would quietly cover a future route or field."""
    document = _openapi()
    bodies = _bodies(document)
    for key in NON_MODEL_BODIES:
        assert key in bodies, f"exempt body {key} no longer exists"
    schemas = document["components"]["schemas"]
    for model, field in FREE_FORM_FIELDS:
        assert field in schemas[model]["properties"], f"{model}.{field} no longer exists"


def test_the_walk_catches_a_permissive_model() -> None:
    """Guard against a walk that silently passes everything."""
    document = {
        "components": {
            "schemas": {
                "Outer": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"inner": {"$ref": "#/components/schemas/Inner"}},
                },
                "Inner": {"type": "object", "properties": {"x": {"type": "string"}}},
            }
        }
    }
    found = _violations(document, {"$ref": "#/components/schemas/Outer"}, owner="body", seen=set())
    assert found == ["Inner (in Outer.inner) accepts unknown fields"]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""RFC 9457 problem details (SECURITY.md F5)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, SecretStr

from app.core.errors import (
    PROBLEM_MEDIA_TYPE,
    ProblemError,
    problem_body,
    problem_type,
    validation_errors,
)
from app.core.settings import Settings
from app.main import API_PREFIX, create_app

SECRET_INPUT = "hunter2-synthetic-secret-value"


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: int
    note: str


def _build_app() -> FastAPI:
    app = create_app(
        Settings(
            dev_mode=True,
            secret_key=SecretStr("s" * 48),
            public_url="http://testserver",
        )
    )
    router = APIRouter()

    @router.get("/problem")
    def raise_problem() -> None:
        raise ProblemError(
            409, "account-conflict", "Account conflict", "Already exists.", {"field": "name"}
        )

    @router.get("/boom")
    def boom() -> None:
        raise RuntimeError("database password is p4ss and table secret_stuff")

    @router.get("/boom-async")
    async def boom_async() -> None:
        raise KeyError("internal-identifier-1234")

    @router.get("/unauthorized")
    def unauthorized() -> None:
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})

    @router.get("/framework-500")
    def framework_500() -> None:
        raise HTTPException(503, "upstream db-host-7 is down")

    @router.post("/validate")
    def validate(payload: Payload) -> dict[str, int]:
        return {"amount": payload.amount}

    @router.get("/query")
    def query(limit: int) -> dict[str, int]:
        return {"limit": limit}

    app.include_router(router, prefix=API_PREFIX)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    # Bearer: these tests exercise error rendering, not CSRF.
    with TestClient(_build_app(), headers={"Authorization": "Bearer synthetic"}) as test_client:
        yield test_client


def _assert_problem(response: Any, status: int, slug: str) -> dict[str, Any]:
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["type"] == f"urn:perfin:problem:{slug}"
    assert body["status"] == status
    assert isinstance(body["title"], str)
    assert isinstance(body, dict)
    return body


def test_problem_error_renders_rfc9457(client: TestClient) -> None:
    body = _assert_problem(client.get(f"{API_PREFIX}/problem"), 409, "account-conflict")
    assert body == {
        "type": "urn:perfin:problem:account-conflict",
        "title": "Account conflict",
        "status": 409,
        "detail": "Already exists.",
        "field": "name",
    }


def test_problem_error_validates_arguments() -> None:
    with pytest.raises(ValueError, match="kebab-case"):
        ProblemError(400, "Not A Slug", "x")
    with pytest.raises(ValueError, match="4xx or 5xx"):
        ProblemError(302, "redirect", "x")
    with pytest.raises(ValueError, match="override"):
        ProblemError(400, "bad", "x", extra={"type": "https://evil.example"})


def test_problem_body_omits_empty_detail() -> None:
    assert problem_body(404, "not-found", "Not Found") == {
        "type": problem_type("not-found"),
        "title": "Not Found",
        "status": 404,
    }


def test_unknown_route_is_a_problem(client: TestClient) -> None:
    _assert_problem(client.get(f"{API_PREFIX}/no-such-thing"), 404, "not-found")


def test_method_not_allowed_keeps_allow_header(client: TestClient) -> None:
    response = client.delete(f"{API_PREFIX}/problem")
    _assert_problem(response, 405, "method-not-allowed")
    assert response.headers["allow"] == "GET"


def test_http_exception_keeps_headers_and_detail(client: TestClient) -> None:
    response = client.get(f"{API_PREFIX}/unauthorized")
    body = _assert_problem(response, 401, "unauthorized")
    assert response.headers["www-authenticate"] == "Bearer"
    assert body["detail"] == "Not authenticated"


def test_framework_5xx_detail_is_not_returned(client: TestClient) -> None:
    body = _assert_problem(client.get(f"{API_PREFIX}/framework-500"), 503, "service-unavailable")
    assert "detail" not in body
    assert "db-host-7" not in str(body)


def test_validation_error_lists_paths_without_input_values(client: TestClient) -> None:
    response = client.post(
        f"{API_PREFIX}/validate",
        json={"amount": SECRET_INPUT, "note": 5, "extra_field": SECRET_INPUT},
    )
    body = _assert_problem(response, 422, "validation-failed")
    assert SECRET_INPUT not in response.text
    errors = body["errors"]
    assert isinstance(errors, list)
    locations = sorted(tuple(error["loc"]) for error in errors)
    assert locations == [("body", "amount"), ("body", "extra_field"), ("body", "note")]
    for error in errors:
        assert set(error) == {"loc", "msg", "type"}


def test_invalid_json_body_is_422_without_echo(client: TestClient) -> None:
    response = client.post(
        f"{API_PREFIX}/validate",
        content=b'{"amount": "' + SECRET_INPUT.encode() + b'"',
        headers={"Content-Type": "application/json"},
    )
    _assert_problem(response, 422, "validation-failed")
    assert SECRET_INPUT not in response.text


def test_query_validation_error(client: TestClient) -> None:
    response = client.get(f"{API_PREFIX}/query", params={"limit": SECRET_INPUT})
    body = _assert_problem(response, 422, "validation-failed")
    assert SECRET_INPUT not in response.text
    assert [(e["loc"], e["type"]) for e in body["errors"]] == [(["query", "limit"], "int_parsing")]


@pytest.mark.parametrize("path", ["/boom", "/boom-async"])
def test_unhandled_exception_is_generic_500_and_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    with caplog.at_level(logging.ERROR, logger="perfin.errors"):
        response = client.get(f"{API_PREFIX}{path}")
    body = _assert_problem(response, 500, "internal-error")
    assert body == {
        "type": "urn:perfin:problem:internal-error",
        "title": "Internal Server Error",
        "status": 500,
    }
    for leaked in ("p4ss", "secret_stuff", "RuntimeError", "Traceback", "internal-identifier"):
        assert leaked not in response.text
    # Security headers and the request id still apply to the 500.
    assert response.headers["x-content-type-options"] == "nosniff"
    request_id = response.headers["x-request-id"]
    records = [r for r in caplog.records if r.name == "perfin.errors"]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert getattr(records[0], "request_id", None) == request_id
    assert getattr(records[0], "route", None) == f"{API_PREFIX}{path}"


def test_validation_errors_drop_input_and_ctx() -> None:
    cleaned = validation_errors(
        [
            {
                "loc": ("body", "iban", 0),
                "msg": "String should match pattern",
                "type": "string_pattern_mismatch",
                "input": "SYNTHETIC-IBAN-0000",
                "ctx": {"pattern": "^x$"},
                "url": "https://errors.pydantic.dev",
            },
            "not-a-mapping",
        ]
    )
    assert cleaned == [
        {
            "loc": ["body", "iban", 0],
            "msg": "String should match pattern",
            "type": "string_pattern_mismatch",
        }
    ]


def test_dev_mode_does_not_enable_debug_tracebacks() -> None:
    app = _build_app()
    assert app.debug is False
    with TestClient(app, headers={"Authorization": "Bearer synthetic"}) as test_client:
        response = test_client.get(f"{API_PREFIX}/boom")
    assert "Traceback" not in response.text

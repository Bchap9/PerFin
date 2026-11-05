# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every column is classified for AI access, and no AI serialiser reads a denied one (M1, E1).

* Enumerates every column of every table in ``Base.metadata`` (all models
  imported) and fails unless ``app.domain.ai.classification`` classifies it
  as exposed, free text or denied; denied columns need a reason; a column
  whose model marks it ``classification: identity`` or ``secret`` must be
  denied. Stale entries (a classified column that no longer exists) fail too.
* Walks every model of ``app.api.ai.schemas`` reachable from a route's
  response model and fails for a field without ``x-ai-source``, for a source
  that is not a real column or a known pseudo source, and for a source
  column classified denied.
* Pins the identity columns the design names (AI_ACCESS.md) as denied, so
  loosening one of them is a visible, failing change.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.api.ai import schemas
from app.db.base import Base
from app.db.registry import import_all_models
from app.domain.ai.classification import CLASSIFICATION, Exposure, classify, is_exposable

pytestmark = pytest.mark.security

import_all_models()


def all_columns() -> Iterator[tuple[str, str, Any]]:
    for table in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
        for column in table.columns:
            yield table.name, column.name, column


def test_every_column_is_classified() -> None:
    missing = [f"{t}.{c}" for t, c, _ in all_columns() if classify(t, c) is None]
    assert not missing, (
        "Unclassified columns (add them to app/domain/ai/classification.py as exposed, "
        f"free_text or denied with a reason): {missing}"
    )


def test_no_stale_classification() -> None:
    real = {(t, c) for t, c, _ in all_columns()}
    stale = [f"{t}.{c}" for t, cols in CLASSIFICATION.items() for c in cols if (t, c) not in real]
    assert not stale, f"Classified columns that do not exist: {stale}"


def test_denied_columns_have_a_reason() -> None:
    bare = [
        f"{t}.{c}"
        for t, cols in CLASSIFICATION.items()
        for c, cls in cols.items()
        if cls.exposure is Exposure.DENIED and not (cls.reason or "").strip()
    ]
    assert not bare, f"Denied without a reason: {bare}"


def test_model_identity_and_secret_columns_are_denied() -> None:
    wrong = []
    for table, name, column in all_columns():
        marked = column.info.get("classification")
        found = classify(table, name)
        if marked in ("identity", "secret") and (
            found is None or found.exposure is not Exposure.DENIED
        ):
            wrong.append(f"{table}.{name} ({marked})")
        if marked == "free_text" and found is not None and found.exposure is Exposure.EXPOSED:
            wrong.append(f"{table}.{name} (free_text classified exposed, would skip the scrubber)")
    assert not wrong, wrong


#: The identity data AI_ACCESS.md names, pinned: these must stay denied.
PINNED_DENIED = (
    "account.number_last4",
    "manual_asset.description",
    "app_user.username",
    "app_user.display_name",
    "app_user.email",
    "identity.email_at_link",
    "identity.subject",
    "household.name",
    "document.extracted_text",
    "document.extraction",
    "document.original_filename",
    "blob.wrapped_dek",
    "credential.password_hash",
    "api_token.token_hash",
    "mfa_method.secret_encrypted",
    "mail_intake.member_addresses",
    "mail_intake.password_encrypted",
    "user_session.ip",
    "audit_log.ip",
    "review_item.payload",
    "ai_client.custom_denylist",
    "ai_client.member_aliases",
    "ai_client.token_id",
    "ai_call.response",
    "transaction.source_document_id",
    "tax_form_data.document_id",
)


#: Text members type or statements supply, pinned free text so the views must
#: scrub it (P6B-SR-R1-01: a security's name and sector were "exposed").
PINNED_FREE_TEXT = (
    "security.symbol",
    "security.name",
    "security.sector",
    "security.exchange",
    "transaction.raw_payee",
    "transaction.memo",
    "transaction.notes",
    "payee.name",
    "category.name",
)


@pytest.mark.parametrize("column", PINNED_FREE_TEXT)
def test_member_typed_text_stays_free_text(column: str) -> None:
    table, _, name = column.partition(".")
    found = classify(table, name)
    assert found is not None, column
    assert found.exposure is Exposure.FREE_TEXT, column


@pytest.mark.parametrize("column", PINNED_DENIED)
def test_identity_columns_stay_denied(column: str) -> None:
    table, _, name = column.partition(".")
    found = classify(table, name)
    assert found is not None, column
    assert found.exposure is Exposure.DENIED, column


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------


def _models_in(annotation: Any) -> Iterator[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    origin = typing.get_origin(annotation)
    if origin is None and not isinstance(annotation, types.UnionType):
        return
    for arg in typing.get_args(annotation):
        yield from _models_in(arg)


def reachable_models(roots: tuple[type[BaseModel], ...]) -> list[type[BaseModel]]:
    seen: dict[type[BaseModel], None] = {}
    stack = list(roots)
    while stack:
        model = stack.pop()
        if model in seen:
            continue
        seen[model] = None
        for info in model.model_fields.values():
            stack.extend(_models_in(info.annotation))
    return list(seen)


def field_sources(model: type[BaseModel]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, info in model.model_fields.items():
        extra = info.json_schema_extra
        sources = extra.get(schemas.SOURCE_KEY) if isinstance(extra, dict) else None
        out[name] = [str(x) for x in sources] if isinstance(sources, list) else []
    return out


def serialiser_problems(models: list[type[BaseModel]]) -> list[str]:
    problems: list[str] = []
    for model in models:
        if not issubclass(model, schemas.AiOut):
            problems.append(f"{model.__name__}: not an AiOut allowlist model")
            continue
        for name, sources in field_sources(model).items():
            where = f"{model.__name__}.{name}"
            if not sources:
                problems.append(f"{where}: no x-ai-source")
            for source in sources:
                if source in schemas.PSEUDO_SOURCES:
                    continue
                table, dot, column = source.partition(".")
                if not dot or classify(table, column) is None:
                    problems.append(f"{where}: unknown source {source!r}")
                elif not is_exposable(source):
                    problems.append(f"{where}: reads denied column {source!r}")
    return problems


def ai_routes() -> list[APIRoute]:
    from app.api.ai.router import router

    return [r for r in router.routes if isinstance(r, APIRoute)]


def test_every_ai_route_uses_an_allowlist_model() -> None:
    routes = ai_routes()
    assert len(routes) >= 19
    for route in routes:
        assert route.path.startswith("/ai/v1/"), route.path
        assert route.methods == {"GET"}, f"{route.path}: AI views are read-only"
        assert route.response_model in schemas.RESPONSE_MODELS, route.path
    used = {r.response_model for r in routes}
    assert used == set(schemas.RESPONSE_MODELS), "RESPONSE_MODELS lists a model no route uses"


def test_serialisers_read_only_allowlisted_columns() -> None:
    models = reachable_models(schemas.RESPONSE_MODELS)
    assert schemas.AiMoney in models
    assert schemas.TransactionAi in models
    assert serialiser_problems(models) == []


def test_a_serialiser_reading_a_denied_column_is_caught() -> None:
    """The check is not vacuous: a field on account.number_last4 fails it."""

    class Leaky(schemas.AiOut):
        last4: str = schemas.src("account.number_last4")
        address: str = schemas.src("manual_asset.description")
        nothing: str = "x"
        unknown: str = schemas.src("account.no_such_column")

    problems = serialiser_problems([Leaky])
    assert any("number_last4" in p for p in problems)
    assert any("manual_asset.description" in p for p in problems)
    assert any("Leaky.nothing: no x-ai-source" in p for p in problems)
    assert any("unknown source" in p for p in problems)


def test_no_serialiser_for_denylist_classes() -> None:
    """Identity classes have no serialiser: no AI model field reads them at all."""
    models = reachable_models(schemas.RESPONSE_MODELS)
    sources = {s for m in models for srcs in field_sources(m).values() for s in srcs}
    for table in ("app_user", "identity", "document", "blob", "credential", "household_member"):
        assert not any(s.startswith(f"{table}.") for s in sources), table

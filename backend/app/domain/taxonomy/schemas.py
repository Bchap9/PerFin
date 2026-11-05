# SPDX-License-Identifier: AGPL-3.0-or-later
"""Input and output schemas for categories, payees and tags (API.md).

Inputs forbid unknown fields (SECURITY.md F4): ``system_key``,
``normalized_name``, ``household_id`` or ``id`` in a request body is a
``422``. ``PATCH`` bodies change only the fields they name; ``null`` clears
the nullable ones (``parent_id``, ``tax_treatment``, ``default_category_id``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Final, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.domain.accounts.schemas import clean_text
from app.domain.transactions.models import ALIASES_MAX, NAME_MAX, CategoryKind, TaxTreatment

MAX_PAGE: Final = 500
DEFAULT_PAGE: Final = 100
#: Categories are few and shown as a tree: one page holds the usual set.
DEFAULT_CATEGORY_PAGE: Final = 500
SEARCH_MAX: Final = 100

Name = Annotated[str, Field(min_length=1, max_length=NAME_MAX), AfterValidator(clean_text)]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True)


def _no_nulls(model: BaseModel, nullable: frozenset[str]) -> None:
    given = model.model_fields_set
    if not given:
        raise ValueError("give at least one field to change")
    for name in given - nullable:
        if getattr(model, name) is None:
            raise ValueError(f"{name} cannot be null")


class MergeInto(_Input):
    """``POST /categories/{id}/merge``, ``POST /payees/{id}/merge``: the survivor."""

    into: uuid.UUID


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class CategoryCreate(_Input):
    """``POST /categories``.

    A child takes its parent's ``kind`` (giving another one is a ``422``); a
    top-level category defaults to ``expense``.
    """

    name: Name
    parent_id: uuid.UUID | None = None
    kind: CategoryKind | None = None
    tax_treatment: TaxTreatment | None = None


class CategoryUpdate(_Input):
    """``PATCH /categories/{id}``: rename, move (``parent_id``), kind, tax treatment.

    Moving under another parent makes the category and its descendants take
    that parent's kind; changing a top-level category's kind changes its
    descendants too.
    """

    name: Name | None = None
    parent_id: uuid.UUID | None = None
    kind: CategoryKind | None = None
    tax_treatment: TaxTreatment | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        _no_nulls(self, frozenset({"parent_id", "tax_treatment"}))
        return self


class CategoryOut(_Output):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    kind: CategoryKind
    tax_treatment: TaxTreatment | None
    #: Key of a seeded default (``app.domain.transactions.categories``), else null.
    system_key: str | None
    created_at: datetime
    updated_at: datetime


class CategoryPage(_Output):
    items: list[CategoryOut]
    next_cursor: uuid.UUID | None


# ---------------------------------------------------------------------------
# Payees
# ---------------------------------------------------------------------------

Alias = Annotated[str, Field(min_length=1, max_length=NAME_MAX), AfterValidator(clean_text)]
Aliases = Annotated[list[Alias], Field(max_length=ALIASES_MAX)]


class PayeeCreate(_Input):
    """``POST /payees``: a canonical name, the statement spellings that mean it, a default."""

    name: Name
    aliases: Aliases = Field(default_factory=list)
    default_category_id: uuid.UUID | None = None


class PayeeUpdate(_Input):
    """``PATCH /payees/{id}``; ``aliases`` replaces the whole list."""

    name: Name | None = None
    aliases: Aliases | None = None
    default_category_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        _no_nulls(self, frozenset({"default_category_id"}))
        return self


class PayeeOut(_Output):
    id: uuid.UUID
    name: str
    normalized_name: str
    aliases: list[str]
    default_category_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class PayeePage(_Output):
    items: list[PayeeOut]
    next_cursor: uuid.UUID | None


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class TagCreate(_Input):
    name: Name


class TagUpdate(_Input):
    name: Name


class TagOut(_Output):
    id: uuid.UUID
    name: str
    created_at: datetime
    updated_at: datetime


class TagPage(_Output):
    items: list[TagOut]
    next_cursor: uuid.UUID | None

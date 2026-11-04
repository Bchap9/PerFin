# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pluggable registry of jurisdiction form sets (DATA_MODEL.md "Tax vault").

A jurisdiction is added by writing a module in this package that builds a
:class:`~app.domain.tax.forms.base.FormSet` and listing it in
:data:`_BUILTIN` (or calling :func:`register` from a plug-in at start-up).
``tax_year.jurisdiction``, ``tax_checklist_item.expected_form`` and
``tax_form_data.form_type`` are validated against the registered sets by the
tax service; the database only checks their format.
"""

from __future__ import annotations

from threading import Lock

from app.domain.tax.forms.base import (
    JURISDICTION_RE,
    BoxDef,
    BoxKind,
    FormDataError,
    FormDef,
    FormSet,
    SanitizedBoxes,
    StaticFormSet,
    sanitize_boxes,
)
from app.domain.tax.forms.us import US_FORMS

__all__ = [
    "BoxDef",
    "BoxKind",
    "FormDataError",
    "FormDef",
    "FormSet",
    "SanitizedBoxes",
    "StaticFormSet",
    "UnknownFormError",
    "UnknownJurisdictionError",
    "form_def",
    "get_form_set",
    "jurisdictions",
    "register",
    "sanitize_boxes",
    "unregister",
]

_BUILTIN: tuple[FormSet, ...] = (US_FORMS,)
_REGISTRY: dict[str, FormSet] = {fs.jurisdiction: fs for fs in _BUILTIN}
_LOCK = Lock()


class UnknownJurisdictionError(LookupError):
    pass


class UnknownFormError(LookupError):
    pass


def register(form_set: FormSet, *, replace: bool = False) -> None:
    """Add a jurisdiction; refuses to shadow an existing one unless ``replace``."""
    if not isinstance(form_set, FormSet):
        raise TypeError("not a FormSet")
    code = form_set.jurisdiction
    if not JURISDICTION_RE.fullmatch(code):
        raise ValueError(f"invalid jurisdiction {code!r}")
    with _LOCK:
        if code in _REGISTRY and not replace:
            raise ValueError(f"jurisdiction {code} is already registered")
        _REGISTRY[code] = form_set


def unregister(code: str) -> None:
    """Remove a jurisdiction (tests and plug-in reloads); built-ins stay removable."""
    with _LOCK:
        _REGISTRY.pop(code, None)


def jurisdictions() -> list[str]:
    with _LOCK:
        return sorted(_REGISTRY)


def get_form_set(code: str) -> FormSet:
    with _LOCK:
        found = _REGISTRY.get(code)
    if found is None:
        raise UnknownJurisdictionError(code)
    return found


def form_def(jurisdiction: str, form_type: str) -> FormDef:
    form = get_form_set(jurisdiction).forms.get(form_type)
    if form is None:
        raise UnknownFormError(f"{jurisdiction} {form_type}")
    return form

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tax form sets: registry, the US definitions and box sanitising (SECURITY.md H8).

Identity boxes (SSN/EIN/TIN, names, addresses, account numbers) must never
survive :func:`sanitize_boxes`; only amounts are kept.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.domain.tax import forms
from app.domain.tax.forms import (
    BoxDef,
    BoxKind,
    FormDataError,
    FormDef,
    StaticFormSet,
    UnknownFormError,
    UnknownJurisdictionError,
    form_def,
    get_form_set,
    sanitize_boxes,
)
from app.domain.tax.forms.base import STORED_KEY_RE
from app.domain.tax.models import BOXES_MAX
from tests.fixtures.tax import synthetic_w2_boxes

US_REQUIRED = {
    "W-2",
    "1099-INT",
    "1099-DIV",
    "1099-B",
    "1099-MISC",
    "1099-NEC",
    "1099-R",
    "1099-G",
    "1099-SA",
    "SSA-1099",
    "1098",
    "1098-E",
    "1098-T",
    "1095-A",
    "1095-B",
    "1095-C",
}

IDENTITY_WORDS = re.compile(
    r"\b(SSN|TIN|EIN|name|address|account|ZIP|identification|control)\b", re.I
)


def us_forms() -> Iterator[FormDef]:
    yield from get_form_set("US").forms.values()


def test_us_is_registered_with_every_required_form() -> None:
    assert "US" in forms.jurisdictions()
    us = get_form_set("US")
    assert set(us.forms) == US_REQUIRED
    assert "married_filing_jointly" in us.filing_statuses


def test_unknown_jurisdiction_and_form() -> None:
    with pytest.raises(UnknownJurisdictionError):
        get_form_set("ZZ")
    with pytest.raises(UnknownFormError):
        form_def("US", "W-9")


@pytest.mark.parametrize("form_type", sorted(US_REQUIRED))
def test_identity_labels_are_identity_boxes(form_type: str) -> None:
    """Every box whose label names a person, number or address is IDENTITY, never stored."""
    form = form_def("US", form_type)
    for box in form.boxes:
        if box.stored:
            assert not re.search(r"\b(SSN|TIN|EIN|address|ZIP)\b", box.label, re.I), box
        label_is_identity = IDENTITY_WORDS.search(box.label) and not re.search(
            r"name of locality|locality name|state name|bond CUSIP", box.label, re.I
        )
        if label_is_identity and box.kind is not BoxKind.IDENTITY:
            assert box.kind in (BoxKind.TEXT, BoxKind.CHECKBOX), box
            assert not box.stored


@pytest.mark.parametrize("form_type", sorted(US_REQUIRED - {"1095-B"}))
def test_every_form_keeps_some_amounts(form_type: str) -> None:
    form = form_def("US", form_type)
    assert form.stored_boxes
    assert len(form.stored_keys()) <= BOXES_MAX
    assert all(STORED_KEY_RE.fullmatch(key) for key in form.stored_keys())


@pytest.mark.parametrize("form_type", ["W-2", "1099-INT", "1098", "1098-T", "1095-A", "1095-C"])
def test_payer_and_recipient_tins_are_identity(form_type: str) -> None:
    identity_labels = " ".join(b.label for b in form_def("US", form_type).identity_boxes)
    assert re.search(r"SSN|TIN|EIN|identification", identity_labels)
    assert re.search(r"name", identity_labels, re.I)


def test_w2_sanitize_keeps_amounts_and_drops_identity() -> None:
    raw = synthetic_w2_boxes(seed=7)
    result = sanitize_boxes(form_def("US", "W-2"), raw)
    assert set(result.boxes) == {"1", "2", "3", "4", "5", "6", "12_D", "16", "17"}
    assert result.boxes["12_D"] == Decimal("5000.000000")
    assert set(result.discarded) == {"a", "b", "c", "d", "e", "f", "13", "14", "15"}
    stored = repr(result.as_json())
    for key in ("a", "b", "c", "d", "e", "f", "15"):
        assert str(raw[key]) not in stored
    assert all(isinstance(v, str) for v in result.as_json().values())


def test_sanitize_refuses_unknown_boxes_codes_and_non_amounts() -> None:
    w2 = form_def("US", "W-2")
    with pytest.raises(FormDataError, match="no box"):
        sanitize_boxes(w2, {"99": "1.00"})
    with pytest.raises(FormDataError, match="known code"):
        sanitize_boxes(w2, {"12_ZZZ": "1.00"})
    with pytest.raises(FormDataError, match="known code"):
        sanitize_boxes(w2, {"12": "1.00"})
    with pytest.raises(FormDataError, match="takes no code"):
        sanitize_boxes(w2, {"1_D": "1.00"})
    with pytest.raises(FormDataError, match="not an amount") as caught:
        sanitize_boxes(w2, {"1": "123-45-6789"})
    assert "123-45-6789" not in str(caught.value)
    with pytest.raises(FormDataError):
        sanitize_boxes(w2, {"1": 1.5})


def test_sanitize_skips_empty_boxes_and_identity_with_any_value() -> None:
    result = sanitize_boxes(
        form_def("US", "1099-INT"),
        {"1": None, "8": "12.5", "payer_tin": "12-3456789", "recipient": {"x": 1}},
    )
    assert result.boxes == {"8": Decimal("12.500000")}
    assert result.discarded == ("payer_tin", "recipient")


def test_1095c_monthly_contribution_is_coded() -> None:
    result = sanitize_boxes(form_def("US", "1095-C"), {"15_ALL": "104.00", "15_JAN": "10"})
    assert set(result.boxes) == {"15_ALL", "15_JAN"}
    assert "1095-A" in get_form_set("US").forms
    assert len(form_def("US", "1095-A").stored_keys()) == 39


def test_box_definitions_are_validated() -> None:
    with pytest.raises(ValueError, match="amount box id"):
        BoxDef("1_a", "x", BoxKind.AMOUNT)
    with pytest.raises(ValueError, match="codes"):
        BoxDef("12", "x", BoxKind.AMOUNT, frozenset({"A"}))
    with pytest.raises(ValueError, match="duplicate"):
        FormDef("X-1", "x", "payer", (BoxDef("1", "a", BoxKind.AMOUNT),) * 2)
    with pytest.raises(ValueError, match="jurisdiction"):
        StaticFormSet.of("usa", "x", (), ())


@dataclass(frozen=True)
class _Plugin:
    jurisdiction: str
    name: str
    filing_statuses: frozenset[str]
    forms: Mapping[str, FormDef]


def test_registry_is_pluggable() -> None:
    plugin = _Plugin(
        "ZZ",
        "Testland",
        frozenset({"single"}),
        {"T-1": FormDef("T-1", "Test form", "payer", (BoxDef("1", "Income", BoxKind.AMOUNT),))},
    )
    forms.register(plugin)
    try:
        assert form_def("ZZ", "T-1").box("1") is not None
        with pytest.raises(ValueError, match="already registered"):
            forms.register(plugin)
        with pytest.raises(TypeError):
            forms.register(object())  # type: ignore[arg-type]
    finally:
        forms.unregister("ZZ")
    with pytest.raises(UnknownJurisdictionError):
        get_form_set("ZZ")

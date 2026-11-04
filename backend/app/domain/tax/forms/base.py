# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tax form definitions: the :class:`FormSet` protocol and box sanitising.

A jurisdiction (``"US"``) ships a :class:`FormSet`: its code, the filing
statuses it knows and a :class:`FormDef` per form type (``"W-2"``,
``"1099-INT"``...). Each form lists its boxes with an id (``"1"``, ``"2a"``,
``"12"``), a label and a :class:`BoxKind`.

Only **amounts** are ever stored (``tax_form_data.boxes``, SECURITY.md H8,
AI_ACCESS.md "Tax form identity boxes"): :func:`sanitize_boxes` keeps
``amount`` and ``coded_amount`` boxes and discards every other kind,
``identity`` boxes (SSN, EIN/TIN, names, addresses, account and control
numbers) included, so a parser that read them cannot persist them. It
returns the ids it discarded, never their values. A ``coded_amount`` box
(W-2 box 12) is stored once per code as ``<box>_<code>`` (``"12_D"``).
Box ids of stored boxes match :data:`STORED_KEY_RE`, which the
``tax_form_data`` check constraint enforces as well.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from app.core.money import MoneyError, parse_amount

#: Keys of ``tax_form_data.boxes`` (box id, optionally ``_<code>``).
STORED_KEY_RE: Final = re.compile(r"[A-Za-z0-9_]{1,32}")
_BOX_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]{0,23}")
#: Ids of stored (amount) boxes: no ``_``, so ``<box>_<code>`` is unambiguous.
_STORED_BOX_ID_RE: Final = re.compile(r"[A-Za-z0-9]{1,8}")
_CODE_RE: Final = re.compile(r"[A-Z0-9]{1,4}")
#: Jurisdiction codes: ISO 3166-1 alpha-2, optionally ``-`` and a subdivision.
JURISDICTION_RE: Final = re.compile(r"[A-Z]{2}(-[A-Z0-9]{1,3})?")
FORM_TYPE_RE: Final = re.compile(r"[A-Z0-9][A-Za-z0-9-]{0,31}")


class BoxKind(enum.StrEnum):
    #: A money amount: stored.
    AMOUNT = "amount"
    #: An amount qualified by a code (W-2 box 12): stored as ``<box>_<code>``.
    CODED_AMOUNT = "coded_amount"
    #: Codes, dates, state abbreviations, percentages, free text: discarded.
    TEXT = "text"
    #: A check box: discarded.
    CHECKBOX = "checkbox"
    #: SSN/TIN/EIN, names, addresses, account/control/policy numbers:
    #: discarded, never stored, never logged.
    IDENTITY = "identity"


STORED_KINDS: Final = frozenset({BoxKind.AMOUNT, BoxKind.CODED_AMOUNT})


class FormDataError(ValueError):
    """Box data that does not fit the form definition (message names ids, never values)."""


@dataclass(frozen=True)
class BoxDef:
    id: str
    label: str
    kind: BoxKind
    #: Allowed codes of a ``coded_amount`` box.
    codes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not _BOX_ID_RE.fullmatch(self.id):
            raise ValueError(f"invalid box id {self.id!r}")
        if self.kind in STORED_KINDS and not _STORED_BOX_ID_RE.fullmatch(self.id):
            raise ValueError(f"invalid amount box id {self.id!r}")
        if (self.kind is BoxKind.CODED_AMOUNT) != bool(self.codes):
            raise ValueError(f"box {self.id}: codes go with coded_amount boxes only")
        for code in self.codes:
            if not _CODE_RE.fullmatch(code):
                raise ValueError(f"box {self.id}: invalid code {code!r}")

    @property
    def stored(self) -> bool:
        return self.kind in STORED_KINDS

    @property
    def identity(self) -> bool:
        return self.kind is BoxKind.IDENTITY


@dataclass(frozen=True)
class FormDef:
    form_type: str
    title: str
    #: Who issues it (``employer``, ``payer``, ``lender``...): ``issuer_name`` role.
    issuer_role: str
    boxes: tuple[BoxDef, ...]
    #: The identity box printing the issuer's name and address (parsers read
    #: the name from it); defaults to the box whose id is ``issuer_role``.
    issuer_box: str | None = None
    _by_id: Mapping[str, BoxDef] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not FORM_TYPE_RE.fullmatch(self.form_type):
            raise ValueError(f"invalid form type {self.form_type!r}")
        by_id = {box.id: box for box in self.boxes}
        if len(by_id) != len(self.boxes):
            raise ValueError(f"{self.form_type}: duplicate box ids")
        object.__setattr__(self, "_by_id", MappingProxyType(by_id))
        if self.issuer_box is not None and self.issuer_box not in by_id:
            raise ValueError(f"{self.form_type}: no issuer box {self.issuer_box!r}")

    @property
    def issuer_box_id(self) -> str | None:
        """Id of the box with the issuer's name (``issuer_box`` or ``issuer_role``)."""
        if self.issuer_box is not None:
            return self.issuer_box
        return self.issuer_role if self.issuer_role in self._by_id else None

    def box(self, box_id: str) -> BoxDef | None:
        return self._by_id.get(box_id)

    @property
    def stored_boxes(self) -> tuple[BoxDef, ...]:
        return tuple(b for b in self.boxes if b.stored)

    @property
    def identity_boxes(self) -> tuple[BoxDef, ...]:
        return tuple(b for b in self.boxes if b.identity)

    def stored_keys(self) -> frozenset[str]:
        """Every key ``tax_form_data.boxes`` may hold for this form."""
        keys: set[str] = set()
        for box in self.stored_boxes:
            if box.kind is BoxKind.CODED_AMOUNT:
                keys.update(f"{box.id}_{code}" for code in box.codes)
            else:
                keys.add(box.id)
        return frozenset(keys)


@runtime_checkable
class FormSet(Protocol):
    """A jurisdiction's tax forms (module doc)."""

    @property
    def jurisdiction(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def filing_statuses(self) -> frozenset[str]: ...

    @property
    def forms(self) -> Mapping[str, FormDef]: ...


@dataclass(frozen=True)
class StaticFormSet:
    """A :class:`FormSet` from literal definitions."""

    jurisdiction: str
    name: str
    filing_statuses: frozenset[str]
    forms: Mapping[str, FormDef]

    @classmethod
    def of(
        cls, jurisdiction: str, name: str, filing_statuses: Iterable[str], forms: Iterable[FormDef]
    ) -> StaticFormSet:
        if not JURISDICTION_RE.fullmatch(jurisdiction):
            raise ValueError(f"invalid jurisdiction {jurisdiction!r}")
        by_type: dict[str, FormDef] = {}
        for form in forms:
            if form.form_type in by_type:
                raise ValueError(f"{jurisdiction}: duplicate form {form.form_type}")
            by_type[form.form_type] = form
        return cls(jurisdiction, name, frozenset(filing_statuses), MappingProxyType(by_type))


@dataclass(frozen=True)
class SanitizedBoxes:
    #: ``box key -> amount`` (stored precision), only amount boxes.
    boxes: dict[str, Decimal]
    #: Ids of the boxes that were given and dropped (identity, text, checkbox).
    discarded: tuple[str, ...]

    def as_json(self) -> dict[str, str]:
        """The ``tax_form_data.boxes`` document (amounts as decimal strings)."""
        return {key: f"{value:f}" for key, value in sorted(self.boxes.items())}


def _split_key(key: str) -> tuple[str, str | None]:
    box_id, sep, code = key.partition("_")
    return box_id, (code if sep else None)


def sanitize_boxes(form: FormDef, raw: Mapping[str, object]) -> SanitizedBoxes:
    """Keep only the amount boxes of ``raw`` (module doc).

    ``raw`` maps box ids (``"1"``, ``"12_D"``, ``"a"``) to values. Values of
    amount boxes must be decimal strings, ``Decimal`` or ``int`` (never
    floats); ``None`` means "box empty" and is skipped. Non-amount boxes are
    dropped whatever their value. An id the form does not define, or a code
    a coded box does not allow, is an error (the parser is out of date), and
    the error names the id only.
    """
    kept: dict[str, Decimal] = {}
    discarded: list[str] = []
    for key, value in raw.items():
        box = form.box(key)
        box_id, code = key, None
        if box is None:
            box_id, code = _split_key(key)
            box = form.box(box_id)
        if box is None:
            raise FormDataError(f"{form.form_type} has no box {box_id!r}")
        if not box.stored:
            discarded.append(box_id)
            continue
        if box.kind is BoxKind.CODED_AMOUNT:
            if code is None or code not in box.codes:
                raise FormDataError(f"{form.form_type} box {box_id} needs a known code")
        elif code is not None:
            raise FormDataError(f"{form.form_type} box {box_id} takes no code")
        if value is None:
            continue
        try:
            amount = parse_amount(value)
        except MoneyError:
            raise FormDataError(f"{form.form_type} box {key} is not an amount") from None
        kept[key] = amount
    return SanitizedBoxes(kept, tuple(sorted(set(discarded))))

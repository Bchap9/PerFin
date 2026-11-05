# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure membership rules and input schemas (no database).

The rules are checked over every combination of actor role, current role,
new role, self/other and owner count against an independent statement of
AUTH.md's "Authorisation model". The schemas are checked for ``extra=forbid``
(F4) and for their field validation.
"""

from __future__ import annotations

import itertools
import uuid
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.auth.models import HouseholdRole
from app.core.errors import ProblemError
from app.domain.households.schemas import (
    ISO_4217_CURRENCIES,
    AccountGrantSet,
    AdminUserCreate,
    AdminUserUpdate,
    HouseholdCreate,
    HouseholdSwitch,
    HouseholdUpdate,
    MemberAdd,
    MemberUpdate,
    NewLocalUser,
    UserPreferencesUpdate,
    clean_timezone,
)
from app.domain.households.service import check_add, check_change, check_remove

ROLES = list(HouseholdRole)
ACTORS: list[HouseholdRole | None] = [*ROLES, None]
OWN, ADM = HouseholdRole.OWNER, HouseholdRole.ADMIN


def _outcome(fn: Any, *args: Any, **kwargs: Any) -> str | None:
    try:
        fn(*args, **kwargs)
    except ProblemError as exc:
        return exc.slug
    return None


def _expected_add(actor: HouseholdRole | None, role: HouseholdRole) -> str | None:
    if actor not in (OWN, ADM):
        return "forbidden"
    if role is OWN and actor is not OWN:
        return "owner-protected"
    return None


def _expected_change(
    actor: HouseholdRole | None,
    current: HouseholdRole,
    new: HouseholdRole,
    is_self: bool,
    owners: int,
) -> str | None:
    if actor not in (OWN, ADM):
        return "forbidden"
    if is_self:
        return "own-membership"
    if (current is OWN or new is OWN) and actor is not OWN:
        return "owner-protected"
    if current is OWN and new is not OWN and owners < 2:
        return "last-owner"
    return None


def _expected_remove(
    actor: HouseholdRole | None, current: HouseholdRole, is_self: bool, owners: int
) -> str | None:
    if actor not in (OWN, ADM):
        return "forbidden"
    if is_self:
        return "own-membership"
    if current is OWN and actor is not OWN:
        return "owner-protected"
    if current is OWN and owners < 2:
        return "last-owner"
    return None


@pytest.mark.parametrize(("actor", "role"), list(itertools.product(ACTORS, ROLES)))
def test_add_rules(actor: HouseholdRole | None, role: HouseholdRole) -> None:
    assert _outcome(check_add, actor, role) == _expected_add(actor, role)


@pytest.mark.parametrize(
    ("actor", "current", "new", "is_self", "owners"),
    list(itertools.product(ACTORS, ROLES, ROLES, [False, True], [1, 2])),
)
def test_change_rules(
    actor: HouseholdRole | None,
    current: HouseholdRole,
    new: HouseholdRole,
    is_self: bool,
    owners: int,
) -> None:
    got = _outcome(check_change, actor, current, new, is_self=is_self, owners=owners)
    assert got == _expected_change(actor, current, new, is_self, owners)


@pytest.mark.parametrize(
    ("actor", "current", "is_self", "owners"),
    list(itertools.product(ACTORS, ROLES, [False, True], [1, 2])),
)
def test_remove_rules(
    actor: HouseholdRole | None, current: HouseholdRole, is_self: bool, owners: int
) -> None:
    got = _outcome(check_remove, actor, current, is_self=is_self, owners=owners)
    assert got == _expected_remove(actor, current, is_self, owners)


def test_the_last_owner_is_kept() -> None:
    with pytest.raises(ProblemError) as caught:
        check_remove(OWN, OWN, is_self=False, owners=1)
    assert caught.value.status == 409
    with pytest.raises(ProblemError):
        check_change(OWN, OWN, ADM, is_self=False, owners=1)


# ---- schemas (F4) ------------------------------------------------------------------

USER = {"username": "synthetic", "display_name": "Synthetic", "password": "x" * 16}
VALID: list[tuple[type[BaseModel], dict[str, Any]]] = [
    (HouseholdCreate, {"name": "Home"}),
    (HouseholdUpdate, {"name": "Home"}),
    (MemberAdd, {"user_id": str(uuid.uuid4()), "role": "member"}),
    (MemberUpdate, {"role": "viewer"}),
    (NewLocalUser, USER),
    (AdminUserCreate, USER),
    (AdminUserUpdate, {"is_active": False}),
    (HouseholdSwitch, {"household_id": str(uuid.uuid4())}),
    (UserPreferencesUpdate, {"theme": "dark"}),
    (
        AccountGrantSet,
        {"user_id": str(uuid.uuid4()), "account_id": str(uuid.uuid4()), "level": "view"},
    ),
]
SMUGGLED = ("id", "household_id", "is_instance_admin", "is_breakglass", "created_at", "owner")


@pytest.mark.parametrize(("model", "data"), VALID, ids=[m.__name__ for m, _ in VALID])
def test_every_input_forbids_unknown_fields(model: type[BaseModel], data: dict[str, Any]) -> None:
    model.model_validate(data)
    assert model.model_config.get("extra") == "forbid"
    for field in SMUGGLED:
        if field in model.model_fields:
            continue
        with pytest.raises(ValidationError):
            model.model_validate({**data, field: "x"})


def test_member_add_needs_exactly_one_target() -> None:
    with pytest.raises(ValidationError):
        MemberAdd.model_validate({"role": "member"})
    with pytest.raises(ValidationError):
        MemberAdd.model_validate({"role": "member", "user_id": str(uuid.uuid4()), "username": "a"})
    with pytest.raises(ValidationError):
        MemberAdd.model_validate({"role": "boss", "username": "a"})
    assert MemberAdd.model_validate({"role": "admin", "username": " Alice "}).username == "alice"


@pytest.mark.parametrize("code", ["usd", "eUr", "JPY", "CHF", "XOF"])
def test_currency_accepted(code: str) -> None:
    assert HouseholdCreate.model_validate({"name": "H", "base_currency": code}).base_currency == (
        code.strip().upper()
    )


@pytest.mark.parametrize("code", ["XXX", "XAU", "XTS", "ABC", "US", "EURO", "HRK", "12$"])
def test_currency_rejected(code: str) -> None:
    with pytest.raises(ValidationError):
        HouseholdUpdate.model_validate({"base_currency": code})


def test_currency_list_is_iso_shaped() -> None:
    assert len(ISO_4217_CURRENCIES) > 150
    assert all(len(c) == 3 and c.isupper() and c.isalpha() for c in ISO_4217_CURRENCIES)
    assert {"USD", "EUR", "GBP", "JPY"} <= ISO_4217_CURRENCIES


def test_names_are_cleaned_and_must_be_printable() -> None:
    assert HouseholdCreate.model_validate({"name": "  Home  "}).name == "Home"
    for bad in ("   ", "a\nb", "tab\there", "x" * 201):
        with pytest.raises(ValidationError):
            HouseholdCreate.model_validate({"name": bad})


@pytest.mark.parametrize("username", ["Bad Name", "-dash", "", "x" * 65, "a@b"])
def test_new_user_username_rules(username: str) -> None:
    with pytest.raises(ValidationError):
        NewLocalUser.model_validate({**USER, "username": username})


def test_new_user_email() -> None:
    assert NewLocalUser.model_validate({**USER, "email": " a@b.test "}).email == "a@b.test"
    assert NewLocalUser.model_validate({**USER, "email": ""}).email is None
    with pytest.raises(ValidationError):
        NewLocalUser.model_validate({**USER, "email": "not-an-email"})


@pytest.mark.parametrize(
    ("field", "value", "ok"),
    [
        ("locale", "en-US", True),
        ("locale", "de", True),
        ("locale", "zh-Hant-TW", True),
        ("locale", "es-419", True),
        ("locale", "english", False),
        ("locale", "en_US", False),
        ("theme", "dark", True),
        ("theme", "neon", False),
        ("timezone", "UTC", True),
        ("timezone", "../../etc/passwd", False),
        ("timezone", "Not/AZone", False),
    ],
)
def test_preference_validation(field: str, value: str, ok: bool) -> None:
    if ok:
        assert getattr(UserPreferencesUpdate.model_validate({field: value}), field) == value
    else:
        with pytest.raises(ValidationError):
            UserPreferencesUpdate.model_validate({field: value})


def test_known_iana_zone_when_tzdata_is_present() -> None:
    import zoneinfo

    if "Europe/Berlin" not in zoneinfo.available_timezones():
        pytest.fail("the test environment needs the system tz database")
    assert clean_timezone("Europe/Berlin") == "Europe/Berlin"


def test_updates_need_a_field() -> None:
    for model in (HouseholdUpdate, UserPreferencesUpdate, AdminUserUpdate):
        with pytest.raises(ValidationError):
            model.model_validate({})

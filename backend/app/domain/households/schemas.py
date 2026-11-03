# SPDX-License-Identifier: AGPL-3.0-or-later
"""Input and output schemas for households, members, grants, preferences and users.

Inputs and outputs are separate models (SECURITY.md F4, mass assignment):
every input model has ``extra="forbid"``, so a request that adds a field such
as ``role``, ``household_id``, ``is_instance_admin`` or ``id`` where it is not
accepted is a ``422``; services copy the validated fields one by one, never
``**body`` into an ORM object. Outputs never carry secrets or hashes.
"""

from __future__ import annotations

import re
import uuid
import zoneinfo
from datetime import datetime
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.auth.local import PASSWORD_INPUT_MAX
from app.auth.models import USERNAME_MAX, GrantLevel, HouseholdRole, normalize_username

NAME_MAX: Final = 200
EMAIL_MAX: Final = 320
LOCALE_MAX: Final = 35
TIMEZONE_MAX: Final = 64

# Same rule as first-run setup (``app.api.routers.setup``): lower-case letters,
# digits, '.', '_' and '-', starting with a letter or digit.
USERNAME_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# A deliberately plain address check (no deliverability lookups): one '@',
# no spaces, a dot in the domain. Email is informational in PerFin.
EMAIL_PATTERN: Final = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")
# BCP 47 subset: language, optional script, optional region (``en``,
# ``en-US``, ``zh-Hant-TW``, ``es-419``).
LOCALE_PATTERN: Final = re.compile(r"^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|[0-9]{3}))?$")
TIMEZONE_PATTERN: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}$")

# Active ISO 4217 currency codes usable as a household's base currency. Fund
# codes, precious metals, SDR and the testing/"no currency" codes are left
# out on purpose (BOV, CHE, CHW, CLF, COU, MXV, USN, UYI, UYW, XAG, XAU, XBA,
# XBB, XBC, XBD, XDR, XPD, XPT, XSU, XTS, XUA, XXX), as are withdrawn codes.
ISO_4217_CURRENCIES: Final = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB
    BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUC CUP CVE CZK DJF DKK
    DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG
    HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT
    LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR
    MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB
    RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT
    TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VED VES VND VUV WST XAF XCD XCG
    XOF XPF YER ZAR ZMW ZWG
    """.split()  # noqa: SIM905 - a compact table reads better than 160 lines
)

Theme = Literal["system", "light", "dark"]
DEFAULT_LOCALE: Final = "en-US"
DEFAULT_THEME: Final = "system"
DEFAULT_TIMEZONE: Final = "UTC"


# ---------------------------------------------------------------------------
# Field validators (shared)
# ---------------------------------------------------------------------------


def clean_text(value: str) -> str:
    """Stripped, non-empty, printable text (names shown in the UI)."""
    cleaned = value.strip()
    if not cleaned or not cleaned.isprintable():
        raise ValueError("must be non-empty printable text")
    return cleaned


def clean_currency(value: str) -> str:
    code = value.strip().upper()
    if code not in ISO_4217_CURRENCIES:
        raise ValueError("must be an active ISO 4217 currency code such as USD")
    return code


def clean_username(value: str) -> str:
    name = normalize_username(value)
    if not USERNAME_PATTERN.fullmatch(name):
        raise ValueError(
            "use 1-64 lower-case letters, digits, '.', '_' or '-', starting with a letter or digit"
        )
    return name


def clean_email(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not EMAIL_PATTERN.fullmatch(cleaned):
        raise ValueError("must be an email address")
    return cleaned


def clean_locale(value: str) -> str:
    cleaned = value.strip()
    if not LOCALE_PATTERN.fullmatch(cleaned):
        raise ValueError("must be a language tag such as en-US")
    return cleaned


def clean_timezone(value: str) -> str:
    """An IANA time zone name known to this system's tz database."""
    cleaned = value.strip()
    if cleaned == "UTC":
        return cleaned
    if not TIMEZONE_PATTERN.fullmatch(cleaned):
        raise ValueError("must be an IANA time zone such as Europe/Berlin")
    try:
        zoneinfo.ZoneInfo(cleaned)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("must be an IANA time zone such as Europe/Berlin") from exc
    return cleaned


Name = Annotated[str, Field(min_length=1, max_length=NAME_MAX)]
Currency = Annotated[str, Field(min_length=3, max_length=3)]
Username = Annotated[str, Field(min_length=1, max_length=USERNAME_MAX)]
Password = Annotated[str, Field(min_length=1, max_length=PASSWORD_INPUT_MAX)]
Email = Annotated[str, Field(max_length=EMAIL_MAX)]


class _Input(BaseModel):
    """Base for every input: unknown fields are an error (F4)."""

    model_config = ConfigDict(extra="forbid")


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Households
# ---------------------------------------------------------------------------


class HouseholdCreate(_Input):
    """``POST /households`` (instance admin)."""

    name: Name
    base_currency: Currency = "USD"
    #: The first owner; the calling instance admin when omitted.
    owner_user_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return clean_text(value)

    @field_validator("base_currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return clean_currency(value)


class HouseholdUpdate(_Input):
    """``PATCH /households/{id}``: only these two fields can change."""

    name: Name | None = None
    base_currency: Currency | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else clean_text(value)

    @field_validator("base_currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        return None if value is None else clean_currency(value)

    @model_validator(mode="after")
    def _something(self) -> Self:
        if self.name is None and self.base_currency is None:
            raise ValueError("give at least one of name, base_currency")
        return self


class HouseholdOut(_Output):
    id: uuid.UUID
    name: str
    base_currency: str
    created_at: datetime
    #: The caller's role here; ``null`` for an instance admin who is not a member.
    role: HouseholdRole | None


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


class NewLocalUser(_Input):
    """A local account created for an invited member (or by an instance admin)."""

    username: Username
    display_name: Name
    password: Password
    email: Email | None = None

    @field_validator("username")
    @classmethod
    def _username(cls, value: str) -> str:
        return clean_username(value)

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str) -> str:
        return clean_text(value)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return clean_email(value)


class MemberAdd(_Input):
    """``POST /households/{id}/members``: exactly one of ``user_id``, ``username``, ``new_user``."""

    role: HouseholdRole
    user_id: uuid.UUID | None = None
    username: Username | None = None
    new_user: NewLocalUser | None = None

    @field_validator("username")
    @classmethod
    def _username(cls, value: str | None) -> str | None:
        return None if value is None else normalize_username(value)

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        given = sum(x is not None for x in (self.user_id, self.username, self.new_user))
        if given != 1:
            raise ValueError("give exactly one of user_id, username, new_user")
        return self


class MemberUpdate(_Input):
    """``PATCH /households/{id}/members/{user_id}``: the role only."""

    role: HouseholdRole


class MemberOut(_Output):
    user_id: uuid.UUID
    username: str
    display_name: str
    role: HouseholdRole
    is_active: bool
    joined_at: datetime


# ---------------------------------------------------------------------------
# Household switching
# ---------------------------------------------------------------------------


class HouseholdSwitch(_Input):
    household_id: uuid.UUID


class ActiveHouseholdOut(_Output):
    id: uuid.UUID
    name: str
    role: HouseholdRole


class HouseholdSwitchOut(_Output):
    household: ActiveHouseholdOut
    #: CSRF token of the rotated session (the old one is dead).
    csrf_token: str


# ---------------------------------------------------------------------------
# Account grants (service only until accounts arrive in Phase 2)
# ---------------------------------------------------------------------------


class AccountGrantSet(_Input):
    user_id: uuid.UUID
    account_id: uuid.UUID
    level: GrantLevel


class AccountGrantOut(_Output):
    id: uuid.UUID
    user_id: uuid.UUID
    account_id: uuid.UUID
    level: GrantLevel
    updated_at: datetime


# ---------------------------------------------------------------------------
# User preferences (``/settings/user``)
# ---------------------------------------------------------------------------


class UserPreferencesOut(_Output):
    display_name: str
    locale: str
    theme: Theme
    timezone: str


class UserPreferencesUpdate(_Input):
    display_name: Name | None = None
    locale: Annotated[str, Field(min_length=2, max_length=LOCALE_MAX)] | None = None
    theme: Theme | None = None
    timezone: Annotated[str, Field(min_length=1, max_length=TIMEZONE_MAX)] | None = None

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str | None) -> str | None:
        return None if value is None else clean_text(value)

    @field_validator("locale")
    @classmethod
    def _locale(cls, value: str | None) -> str | None:
        return None if value is None else clean_locale(value)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str | None) -> str | None:
        return None if value is None else clean_timezone(value)

    @model_validator(mode="after")
    def _something(self) -> Self:
        if all(v is None for v in (self.display_name, self.locale, self.theme, self.timezone)):
            raise ValueError("give at least one field to change")
        return self


# ---------------------------------------------------------------------------
# Instance administration (``/admin/users``)
# ---------------------------------------------------------------------------


class AdminUserCreate(NewLocalUser):
    """``POST /admin/users``: a local account with no household and no admin flag."""


class AdminUserUpdate(_Input):
    """``PATCH /admin/users/{id}``: activation, break-glass and email-verified flags.

    ``is_instance_admin`` is deliberately not accepted (``extra="forbid"``).
    """

    is_active: bool | None = None
    is_breakglass: bool | None = None
    email_verified: bool | None = None

    @model_validator(mode="after")
    def _something(self) -> Self:
        if self.is_active is None and self.is_breakglass is None and self.email_verified is None:
            raise ValueError("give at least one of is_active, is_breakglass, email_verified")
        return self


class AdminUserOut(_Output):
    id: uuid.UUID
    username: str
    display_name: str
    email: str | None
    email_verified: bool
    is_active: bool
    is_instance_admin: bool
    is_breakglass: bool
    has_password: bool
    mfa_methods: list[Literal["totp", "webauthn"]]
    locked_until: datetime | None
    last_login_at: datetime | None
    created_at: datetime


class AdminUserPage(_Output):
    items: list[AdminUserOut]
    #: Pass as ``cursor`` for the next page; ``null`` on the last page.
    next_cursor: uuid.UUID | None

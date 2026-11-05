# SPDX-License-Identifier: AGPL-3.0-or-later
"""Account subtypes: the catalogue, its tax treatment and reading them from account names."""

from __future__ import annotations

import pytest

from app.domain.accounts.models import AccountType
from app.domain.accounts.subtypes import (
    SUBTYPES,
    guess_subtype,
    tax_treatment,
    valid_subtype,
)

R, B = AccountType.RETIREMENT, AccountType.BROKERAGE


@pytest.mark.parametrize(
    ("name", "kind", "code"),
    [
        ("ROTH IRA", R, "roth_ira"),
        ("Traditional IRA", R, "traditional_ira"),
        ("IRA", R, "traditional_ira"),
        ("Rollover IRA", R, "rollover_ira"),
        ("SEP-IRA", R, "sep_ira"),
        ("SIMPLE IRA", R, "simple_ira"),
        ("Inherited IRA", R, "inherited_ira"),
        ("Inherited Roth IRA", R, "inherited_roth_ira"),
        ("401(k) PLAN", R, "401k"),
        ("ROTH 401(K)", R, "roth_401k"),
        ("Individual 401k", R, "solo_401k"),
        ("403(b)", R, "403b"),
        ("457(b) Deferred Comp", R, "457b"),
        ("Thrift Savings Plan", R, "tsp"),
        ("Health Savings Account", R, "hsa"),
        ("Individual", B, "individual"),
        ("Joint WROS", B, "joint"),
        ("529 College Savings", B, "education_529"),
        ("UTMA Custodial", B, "custodial"),
        ("Coverdell ESA", B, "coverdell_esa"),
        ("Cash Management (Individual)", B, "cash_management"),
    ],
)
def test_names_say_what_kind_of_investment_account(name: str, kind: AccountType, code: str) -> None:
    assert guess_subtype(name) == (kind, code)


def test_a_name_that_says_nothing_guesses_nothing() -> None:
    assert guess_subtype("Brokerage Link") == (None, None)
    assert guess_subtype(None) == (None, None)
    assert guess_subtype("Everyday", AccountType.CHECKING) == (AccountType.CHECKING, None)
    assert guess_subtype("Joint checking", AccountType.CHECKING) == (AccountType.CHECKING, "joint")


def test_subtypes_belong_to_their_type() -> None:
    assert valid_subtype(R, "roth_ira")
    assert valid_subtype(R, None)
    assert not valid_subtype(B, "roth_ira")
    assert not valid_subtype(R, "ROTH IRA")
    for kind, entries in SUBTYPES.items():
        codes = [s.code for s in entries]
        assert len(codes) == len(set(codes)), kind
        assert all(valid_subtype(kind, code) for code in codes)


def test_tax_treatment() -> None:
    assert tax_treatment(R, "roth_ira") == "tax_free"
    assert tax_treatment(R, "401k") == "tax_deferred"
    assert tax_treatment(B, "education_529") == "tax_free"
    assert tax_treatment(B, None) == "taxable"
    assert tax_treatment(AccountType.CHECKING, "joint") is None

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Account subtypes: what kind of checking, retirement or investment account it is.

An account's ``type`` decides how PerFin treats it (asset or liability,
transactions or holdings); its optional ``subtype`` says which kind it is
within that type (a Roth IRA or a 401(k) within ``retirement``, a 529 or a
custodial account within ``brokerage``), for display, grouping and its
**tax treatment**:

* ``taxable``: gains and income taxed as they happen (an individual
  brokerage account);
* ``tax_deferred``: taxed on withdrawal (a traditional IRA, a 401(k));
* ``tax_free``: qualified withdrawals untaxed (a Roth IRA, an HSA for
  medical costs, a 529 for education).

Subtypes are short codes in :data:`SUBTYPES`; a code is valid only under its
own type (``valid_subtype``). The list is data, not schema: a new subtype is
a line here, no migration. :func:`guess_subtype` reads the kind from an
account's printed name or nickname (an activity export's ``ROTH IRA``,
``Health Savings Account``), deterministically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from app.domain.accounts.models import AccountType

TaxTreatment = Literal["taxable", "tax_deferred", "tax_free"]


@dataclass(frozen=True)
class Subtype:
    code: str
    label: str
    tax: TaxTreatment | None = None
    #: Lower-case regular expression that names it in an account's name.
    pattern: str | None = None


T = AccountType

#: Every subtype per type, in display order (module doc).
SUBTYPES: Final[dict[AccountType, tuple[Subtype, ...]]] = {
    T.CHECKING: (
        Subtype("personal", "Personal"),
        Subtype("joint", "Joint", pattern=r"\bjoint\b"),
        Subtype("business", "Business", pattern=r"\bbusiness\b"),
        Subtype("student", "Student", pattern=r"\bstudent\b"),
        Subtype("cash_management", "Cash management", pattern=r"\bcash management\b"),
    ),
    T.SAVINGS: (
        Subtype("regular", "Savings"),
        Subtype("high_yield", "High-yield savings", pattern=r"\bhigh[ -]?yield\b"),
        Subtype("money_market", "Money market", pattern=r"\bmoney market\b"),
        Subtype("cd", "Certificate of deposit (CD)", pattern=r"\b(cd|certificate of deposit)\b"),
        Subtype("emergency", "Emergency fund", pattern=r"\bemergency\b"),
    ),
    T.CASH: (
        Subtype("wallet", "Wallet"),
        Subtype("petty_cash", "Petty cash", pattern=r"\bpetty\b"),
    ),
    T.CREDIT_CARD: (
        Subtype("personal", "Personal card"),
        Subtype("business", "Business card", pattern=r"\bbusiness\b"),
        Subtype("charge", "Charge card", pattern=r"\bcharge card\b"),
        Subtype("store", "Store card", pattern=r"\bstore card\b"),
        Subtype("secured", "Secured card", pattern=r"\bsecured\b"),
    ),
    T.BROKERAGE: (
        Subtype("individual", "Individual", "taxable", r"\bindividual\b"),
        Subtype("joint", "Joint", "taxable", r"\bjoint\b|\bwros\b|\btenants\b"),
        Subtype("trust", "Trust", "taxable", r"\btrust\b"),
        Subtype("custodial", "Custodial (UTMA/UGMA)", "taxable", r"\b(utma|ugma|custodial)\b"),
        Subtype("education_529", "529 education plan", "tax_free", r"\b529\b"),
        Subtype("coverdell_esa", "Coverdell ESA", "tax_free", r"\b(coverdell|esa)\b"),
        Subtype("able", "ABLE account", "tax_free", r"\bable\b"),
        Subtype("cash_management", "Cash management", "taxable", r"\bcash management\b"),
        Subtype("crypto", "Crypto", "taxable", r"\bcrypto\b"),
        Subtype("business", "Business", "taxable", r"\bbusiness\b|\bcorporate\b"),
    ),
    T.RETIREMENT: (
        Subtype("traditional_ira", "Traditional IRA", "tax_deferred", r"\b(traditional )?ira\b"),
        Subtype("roth_ira", "Roth IRA", "tax_free", r"\broth ira\b|\broth\b"),
        Subtype("rollover_ira", "Rollover IRA", "tax_deferred", r"\brollover\b"),
        Subtype("sep_ira", "SEP IRA", "tax_deferred", r"\bsep\b"),
        Subtype("simple_ira", "SIMPLE IRA", "tax_deferred", r"\bsimple\b"),
        Subtype("inherited_ira", "Inherited IRA", "tax_deferred", r"\b(inherited|beneficiary)\b"),
        Subtype(
            "inherited_roth_ira",
            "Inherited Roth IRA",
            "tax_free",
            r"\b(inherited|beneficiary) roth\b|\broth (inherited|beneficiary)\b",
        ),
        Subtype("spousal_ira", "Spousal IRA", "tax_deferred", r"\bspousal\b"),
        Subtype("401k", "401(k)", "tax_deferred", r"\b401 ?\(?k\)?"),
        Subtype("roth_401k", "Roth 401(k)", "tax_free", r"\broth 401 ?\(?k\)?"),
        Subtype("solo_401k", "Solo 401(k)", "tax_deferred", r"\b(solo|individual) 401 ?\(?k\)?"),
        Subtype("403b", "403(b)", "tax_deferred", r"\b403 ?\(?b\)?"),
        Subtype("roth_403b", "Roth 403(b)", "tax_free", r"\broth 403 ?\(?b\)?"),
        Subtype("457b", "457(b)", "tax_deferred", r"\b457"),
        Subtype("tsp", "Thrift Savings Plan (TSP)", "tax_deferred", r"\b(tsp|thrift savings)\b"),
        Subtype("hsa", "Health savings account (HSA)", "tax_free", r"\b(hsa|health savings)\b"),
        Subtype("pension", "Pension", "tax_deferred", r"\bpension\b"),
        Subtype("annuity", "Annuity", "tax_deferred", r"\bannuity\b"),
        Subtype("keogh", "Keogh", "tax_deferred", r"\bkeogh\b"),
        Subtype("profit_sharing", "Profit sharing", "tax_deferred", r"\bprofit sharing\b"),
        Subtype("other", "Other retirement", "tax_deferred"),
    ),
    T.LOAN: (
        Subtype("auto", "Auto loan", pattern=r"\b(auto|car|vehicle)\b"),
        Subtype("student", "Student loan", pattern=r"\bstudent\b"),
        Subtype("personal", "Personal loan", pattern=r"\bpersonal\b"),
        Subtype("home_equity", "Home equity loan", pattern=r"\bhome equity\b"),
        Subtype("business", "Business loan", pattern=r"\bbusiness\b"),
        Subtype("medical", "Medical", pattern=r"\bmedical\b"),
        Subtype("family", "Family loan", pattern=r"\bfamily\b"),
    ),
    T.MORTGAGE: (
        Subtype("primary_residence", "Primary residence"),
        Subtype("second_home", "Second home", pattern=r"\bsecond home\b|\bvacation\b"),
        Subtype("investment_property", "Investment property", pattern=r"\brental\b|\binvestment"),
    ),
    T.LINE_OF_CREDIT: (
        Subtype("heloc", "Home equity line (HELOC)", pattern=r"\bheloc\b|\bhome equity\b"),
        Subtype("personal", "Personal line of credit", pattern=r"\bpersonal\b"),
        Subtype("business", "Business line of credit", pattern=r"\bbusiness\b"),
        Subtype("margin", "Margin", pattern=r"\bmargin\b"),
        Subtype("pledged_asset", "Pledged-asset line", pattern=r"\bpledged\b|\bsecurities.based\b"),
    ),
    T.MANUAL_ASSET: (),
    T.MANUAL_LIABILITY: (),
}

#: The account types an account's name may move a guess to (investment accounts).
_RETIREMENT_FIRST: Final = (T.RETIREMENT, T.BROKERAGE)
_CODE: Final = re.compile(r"^[a-z0-9_]{1,40}$")


def subtypes_of(account_type: AccountType) -> tuple[Subtype, ...]:
    return SUBTYPES.get(account_type, ())


def valid_subtype(account_type: AccountType, code: str | None) -> bool:
    """``code`` is ``None`` or one of ``account_type``'s subtypes."""
    if code is None:
        return True
    return _CODE.fullmatch(code) is not None and any(
        s.code == code for s in subtypes_of(account_type)
    )


def subtype(account_type: AccountType, code: str | None) -> Subtype | None:
    if code is None:
        return None
    return next((s for s in subtypes_of(account_type) if s.code == code), None)


def tax_treatment(account_type: AccountType, code: str | None) -> TaxTreatment | None:
    """The tax treatment of a subtype; a brokerage account without one is taxable."""
    found = subtype(account_type, code)
    if found is not None and found.tax is not None:
        return found.tax
    return "taxable" if account_type is T.BROKERAGE else None


def _match(account_type: AccountType, text: str) -> Subtype | None:
    """The most specific subtype ``text`` names (the longest matching pattern text wins)."""
    best: tuple[int, Subtype] | None = None
    for candidate in subtypes_of(account_type):
        if candidate.pattern is None:
            continue
        found = re.search(candidate.pattern, text)
        # Ties go to the later, more specific entry (``SEP IRA``: sep_ira, not traditional_ira).
        if found is not None and (best is None or len(found.group(0)) >= best[0]):
            best = (len(found.group(0)), candidate)
    return best[1] if best is not None else None


def guess_subtype(
    name: str | None, account_type: AccountType | None = None
) -> tuple[AccountType | None, str | None]:
    """The type and subtype an account's name says (``ROTH IRA`` -> retirement, roth_ira).

    With ``account_type``, only that type's subtypes are looked at; else an
    investment account's name is read as retirement first, then brokerage
    (``Individual`` -> brokerage, individual). ``(None, None)`` when the name
    says nothing.
    """
    text = " ".join((name or "").lower().replace("-", " ").split())
    if not text:
        return account_type, None
    types = (account_type,) if account_type is not None else _RETIREMENT_FIRST
    for kind in types:
        found = _match(kind, text)
        if found is not None:
            return kind, found.code
    return account_type, None

# SPDX-License-Identifier: AGPL-3.0-or-later
"""The authorisation policy of every route, as data (SECURITY.md F3, I2, I3, A5, A6).

``tests/security/test_authz_matrix.py`` walks ``app.routes`` and fails for any
route that has no entry in :data:`POLICIES`, then checks every entry against
every principal in :data:`PRINCIPALS`. A new endpoint therefore cannot ship
unclassified: add one line here (see docs/DEVELOPMENT.md, "Tests").

Keys are ``"<METHOD> <path template>"`` exactly as the app routes them
(``"GET /api/v1/households/{household_id}"``); a ``Mount`` is
``"MOUNT <path>"``. Each value is a :class:`Policy`:

``access``
    ``public`` (no authentication; the handler decides), ``pending_login``
    (only a password-accepted session waiting for its second factor),
    ``mfa_ceremony`` (any live browser session, pending or full; never a
    token), ``authenticated`` (a principal; see the other fields) or
    ``instance_admin`` (the local flag on a browser session).
``tokens``
    For ``authenticated`` routes: ``"refused"`` (browser session only) or
    ``"scope"`` (a personal access token holding :attr:`Policy.scope`).
``role``
    Minimum household role in the *active* household; ``None`` means no
    household is needed.
``household_object``
    The request names the matrix household (in the path or the body):
    anyone who is not a member of it gets ``404 household-not-found``, never
    ``403`` (F3). Set automatically when the path has ``{household_id}``.
``step_up``
    A fresh second-factor verification (10 minutes, A6). For MFA enrolment
    routes it applies only to users who already have a factor.
``enrol``
    An MFA-restricted session (``PERFIN_REQUIRE_MFA`` and no factor yet)
    may call it (enrolment, ``GET /auth/mfa``).
``params``
    How to build each path parameter from a real object (:data:`PARAM_KINDS`):
    ``household`` (the matrix household), ``member`` (a fresh member of
    it), ``user`` (a fresh user), and objects owned by the caller
    (``own_session``, ``own_token``, ``own_webauthn``). A cross-household
    caller is handed the victim's object and must get ``404``.
``body``
    JSON body for unsafe methods (``"{household_id}"`` is replaced). Only
    needed when a check runs inside the handler, after body validation;
    otherwise an empty body is sent and an allowed caller ends in ``422``.
``overrides``
    Documented exceptions for one principal (public routes that still
    answer ``401`` or ``400``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Literal

from app.auth.models import HouseholdRole
from app.auth.scopes import CATALOG

Access = Literal["public", "pending_login", "mfa_ceremony", "authenticated", "instance_admin"]
TokenRule = Literal["refused", "scope"]

#: Every principal the matrix tries, in the order it tries them. Order
#: matters only for destructive endpoints: the most privileged callers go
#: last, so an allowed ``DELETE /households/{id}`` by the owner cannot change
#: what an earlier caller sees.
PRINCIPALS: Final[tuple[str, ...]] = (
    "anonymous",
    "pat_expired",  # the owner's token, expired
    "cookie_bogus_bearer",  # the owner's valid session cookie plus a junk bearer token
    "pat_none",  # the owner's token without scopes
    "pat_scope",  # the owner's token with the route's scope (or every grantable one)
    "mfa_pending",  # member, password accepted, second factor pending
    "restricted",  # owner without any factor while PERFIN_REQUIRE_MFA is on
    "other_owner",  # owner of another household
    "instance_admin",  # instance admin, no household
    "viewer",
    "member",
    "admin",
    "stale",  # owner and instance admin whose MFA verification is 11 minutes old
    "owner",
)

TOKEN_PRINCIPALS: Final = frozenset({"pat_none", "pat_scope"})
INVALID_BEARER: Final = frozenset({"pat_expired", "cookie_bogus_bearer"})
#: Household role of each session principal in the matrix household.
HOUSEHOLD_ROLE: Final[Mapping[str, HouseholdRole]] = MappingProxyType(
    {
        "viewer": HouseholdRole.VIEWER,
        "member": HouseholdRole.MEMBER,
        "admin": HouseholdRole.ADMIN,
        "owner": HouseholdRole.OWNER,
        "stale": HouseholdRole.OWNER,
        "restricted": HouseholdRole.OWNER,
        "mfa_pending": HouseholdRole.MEMBER,
        # Tokens belong to the owner.
        "pat_none": HouseholdRole.OWNER,
        "pat_scope": HouseholdRole.OWNER,
    }
)
INSTANCE_ADMINS: Final = frozenset({"instance_admin", "stale"})


@dataclass(frozen=True)
class ParamKind:
    """How a path parameter is filled; ``not_found`` is the 404 for a foreign object."""

    name: str
    not_found: str | None = None


PARAM_KINDS: Final[Mapping[str, ParamKind]] = MappingProxyType(
    {
        "household": ParamKind("household", "household-not-found"),
        "member": ParamKind("member", "member-not-found"),
        "user": ParamKind("user", "user-not-found"),
        "own_session": ParamKind("own_session", "session-not-found"),
        "own_token": ParamKind("own_token", "token-not-found"),
        "own_webauthn": ParamKind("own_webauthn", "mfa-method-not-found"),
        # Ledger objects of the matrix household (Phase 2). The generator
        # grants the matrix member `edit` and the viewer `view` on the
        # object's account, so the role columns of the policy decide.
        "transaction": ParamKind("transaction", "transaction-not-found"),
        "account": ParamKind("account", "account-not-found"),
        "category": ParamKind("category", "category-not-found"),
        "payee": ParamKind("payee", "payee-not-found"),
        "tag": ParamKind("tag", "tag-not-found"),
        # Budgets and goals (Phase 2). A goal is on an account granted to the
        # matrix member (edit) and viewer (view); a contribution belongs to
        # the goal built for the same path.
        "budget": ParamKind("budget", "budget-not-found"),
        "goal": ParamKind("goal", "goal-not-found"),
        "goal_contribution": ParamKind("goal_contribution", "contribution-not-found"),
        "rule": ParamKind("rule", "rule-not-found"),
        # A run_rules job queued in the matrix household (GET /jobs/{id}).
        "job": ParamKind("job", "job-not-found"),
        # A document (with an extracted table) on an account granted to the
        # matrix member (edit) and viewer (view).
        "document": ParamKind("document", "document-not-found"),
        # A CSV column mapping tied to an account granted to the matrix
        # member (edit) and viewer (view).
        "import_mapping": ParamKind("import_mapping", "import-mapping-not-found"),
        # A processed document whose encrypted file really is in the matrix
        # blob store (downloadable), on an account granted to the matrix
        # member (edit) and viewer (view); a review item of such a document.
        "stored_document": ParamKind("stored_document", "document-not-found"),
        "review_item": ParamKind("review_item", "review-item-not-found"),
        # Full export/import (docs/PORTABILITY.md): an archive belongs to the
        # user who requested it, so the generator makes it the caller's (the
        # victim's for outsiders); a ready export's bytes are really in the
        # matrix blob store. The path names the archive's job id.
        "export_archive": ParamKind("export_archive", "export-not-found"),
        "import_archive": ParamKind("import_archive", "import-not-found"),
    }
)
OWN_KINDS: Final = frozenset(k for k in PARAM_KINDS if k.startswith("own_"))
#: Objects that belong to the matrix household (not to one user): the owner
#: of another household is handed one and must get its ``not_found``.
HOUSEHOLD_OBJECT_KINDS: Final = frozenset(
    {"transaction", "account", "category", "payee", "tag", "rule", "job"}
    | {"budget", "goal", "goal_contribution"}
    | {"document", "import_mapping"}
    | {"stored_document", "review_item"}
    | {"export_archive", "import_archive"}
)


@dataclass(frozen=True)
class Outcome:
    """Expected result for one principal: allowed, or refused with ``status``/``types``."""

    allowed: bool
    status: int | None = None
    types: frozenset[str] = frozenset()

    def __str__(self) -> str:
        if self.allowed:
            return "allowed"
        return f"{self.status} {'|'.join(sorted(self.types))}"


ALLOWED: Final = Outcome(allowed=True)


def deny(status: int, *types: str) -> Outcome:
    return Outcome(allowed=False, status=status, types=frozenset(types))


UNAUTHENTICATED: Final = deny(401, "unauthenticated")
INVALID_TOKEN: Final = deny(401, "invalid-token")
FORBIDDEN: Final = deny(403, "forbidden")
STEP_UP: Final = deny(403, "step-up-required")
MFA_ENROLMENT: Final = deny(403, "mfa-enrollment-required")
NOT_IN_HOUSEHOLD: Final = deny(404, "household-not-found")
#: A token on a browser-session-only route: refused with one of these
#: (which guard answers first differs per route; all are authorisation
#: refusals).
TOKEN_REFUSED: Final = deny(403, "session-required", "step-up-required", "forbidden")
#: Endpoints that only browser sessions may call and that answer a bearer
#: request with ``400`` instead of resolving a principal at all.
BEARER_NOT_A_SESSION: Final = deny(400, "session-required")

#: Problem types that mean "refused by authorisation" (an allowed caller must
#: never see one of these, whatever else goes wrong).
DENIAL_TYPES: Final[Mapping[int, frozenset[str]]] = MappingProxyType(
    {
        400: frozenset({"session-required"}),
        403: frozenset(
            {
                "forbidden",
                "insufficient-scope",
                "session-required",
                "step-up-required",
                "mfa-enrollment-required",
                "csrf-failed",
            }
        ),
        404: frozenset(k.not_found for k in PARAM_KINDS.values() if k.not_found),
    }
)


@dataclass(frozen=True)
class Policy:
    access: Access
    tokens: TokenRule = "refused"
    scope: str | None = None
    role: HouseholdRole | None = None
    household_object: bool = False
    step_up: bool = False
    enrol: bool = False
    params: Mapping[str, str] = field(default_factory=dict)
    body: Mapping[str, Any] | None = None
    overrides: Mapping[str, Outcome] = field(default_factory=dict)


def public(**overrides: Outcome) -> Policy:
    return Policy("public", overrides=overrides)


def session(**kwargs: Any) -> Policy:
    """A browser session (no token), optionally in a household."""
    return Policy("authenticated", tokens="refused", **kwargs)


def scoped(scope: str, **kwargs: Any) -> Policy:
    """A browser session, or a token with ``scope``."""
    return Policy("authenticated", tokens="scope", scope=scope, **kwargs)


def instance_admin(**kwargs: Any) -> Policy:
    return Policy("instance_admin", **kwargs)


V = HouseholdRole.VIEWER
A = HouseholdRole.ADMIN
O = HouseholdRole.OWNER  # noqa: E741 - mirrors the role names
API = "/api/v1"
HH = {"household_id": "household"}
TXN = {"transaction_id": "transaction"}
M = HouseholdRole.MEMBER
ACCT = {"account_id": "account"}
CAT = {"category_id": "category"}
PAYEE = {"payee_id": "payee"}
TAG = {"tag_id": "tag"}
BUDGET = {"budget_id": "budget"}
GOAL = {"goal_id": "goal"}
RULE = {"rule_id": "rule"}
DOC = {"document_id": "document"}
IMAP = {"mapping_id": "import_mapping"}
SDOC = {"document_id": "stored_document"}
ITEM = {"item_id": "review_item"}
#: A member's grants on one account (the path names a fresh member).
GRANT = {**HH, "user_id": "member", "account_id": "account"}

#: GET /auth/session is public (the SPA asks it who is logged in) but answers
#: 401 to anything that is not a live principal.
_SESSION_PROBE = public(
    anonymous=UNAUTHENTICATED,
    mfa_pending=deny(401, "mfa-required"),
    pat_expired=INVALID_TOKEN,
    cookie_bogus_bearer=INVALID_TOKEN,
)
#: Logout refuses any Authorization header (tokens are revoked under
#: /auth/tokens) instead of falling back to the cookie.
_LOGOUT = public(
    pat_expired=BEARER_NOT_A_SESSION,
    cookie_bogus_bearer=BEARER_NOT_A_SESSION,
    pat_none=BEARER_NOT_A_SESSION,
    pat_scope=BEARER_NOT_A_SESSION,
)

POLICIES: Final[Mapping[str, Policy]] = MappingProxyType(
    {
        # --- outside /api/v1 ------------------------------------------------
        "GET /healthz": public(),
        "GET /readyz": public(),
        "MOUNT /assets": public(),
        # Dev mode only (PERFIN_DEV_MODE); absent in production.
        f"GET {API}/openapi.json": public(),
        f"GET {API}/docs": public(),
        # --- first-run setup ------------------------------------------------
        f"GET {API}/setup/status": public(),
        f"POST {API}/setup": public(),
        # --- local login ----------------------------------------------------
        f"GET {API}/auth/login/options": public(),
        f"POST {API}/auth/login": public(),
        f"POST {API}/auth/logout": _LOGOUT,
        f"POST {API}/auth/password": session(step_up=True),
        f"POST {API}/auth/password-reset/request": public(),
        f"POST {API}/auth/password-reset/confirm": public(),
        # --- session --------------------------------------------------------
        f"GET {API}/auth/session": _SESSION_PROBE,
        f"GET {API}/auth/sessions": session(),
        f"DELETE {API}/auth/sessions": session(),
        f"DELETE {API}/auth/sessions/{{session_id}}": session(params={"session_id": "own_session"}),
        f"POST {API}/auth/session/household": session(
            household_object=True, body={"household_id": "{household_id}"}
        ),
        # --- MFA ------------------------------------------------------------
        f"POST {API}/auth/mfa/verify": Policy(
            "pending_login", body={"method": "totp", "code": "000000"}
        ),
        f"POST {API}/auth/mfa/webauthn/authentication-options": Policy("mfa_ceremony"),
        f"POST {API}/auth/step-up": session(),
        f"GET {API}/auth/mfa": session(enrol=True),
        f"POST {API}/auth/mfa/totp": session(enrol=True, step_up=True),
        f"POST {API}/auth/mfa/totp/confirm": session(
            enrol=True, step_up=True, body={"code": "000000"}
        ),
        f"DELETE {API}/auth/mfa/totp": session(step_up=True),
        f"GET {API}/auth/mfa/webauthn": session(enrol=True),
        f"POST {API}/auth/mfa/webauthn/registration-options": session(enrol=True, step_up=True),
        f"POST {API}/auth/mfa/webauthn": session(enrol=True, step_up=True, body={"credential": {}}),
        f"DELETE {API}/auth/mfa/webauthn/{{method_id}}": session(
            step_up=True, params={"method_id": "own_webauthn"}
        ),
        f"POST {API}/auth/mfa/recovery-codes": session(step_up=True),
        # --- OIDC (disabled in the matrix app: the handlers answer 404
        # oidc-disabled, the guards in front of them still run) -------------
        f"GET {API}/auth/oidc/status": public(),
        f"GET {API}/auth/oidc/start": public(),
        f"GET {API}/auth/oidc/callback": public(),
        f"GET {API}/auth/oidc/link": public(),
        f"POST {API}/auth/oidc/link": session(),
        f"DELETE {API}/auth/oidc/link": public(),
        f"POST {API}/auth/oidc/logout": public(),
        f"POST {API}/auth/oidc/backchannel-logout": public(),
        # --- personal access tokens ----------------------------------------
        f"GET {API}/auth/tokens/scopes": session(role=V),
        f"GET {API}/auth/tokens": session(role=V),
        f"POST {API}/auth/tokens": session(role=V, step_up=True),
        f"DELETE {API}/auth/tokens/{{token_id}}": session(role=V, params={"token_id": "own_token"}),
        # --- households -----------------------------------------------------
        f"GET {API}/households": scoped("households:read"),
        f"POST {API}/households": instance_admin(),
        f"GET {API}/households/{{household_id}}": scoped("households:read", role=V, params=HH),
        # The role check runs in the service, after body validation: send a body.
        f"PATCH {API}/households/{{household_id}}": scoped(
            "households:write", role=A, params=HH, body={"name": "Matrix household"}
        ),
        f"DELETE {API}/households/{{household_id}}": session(role=O, step_up=True, params=HH),
        f"GET {API}/households/{{household_id}}/members": scoped(
            "households:read", role=V, params=HH
        ),
        f"POST {API}/households/{{household_id}}/members": session(role=A, step_up=True, params=HH),
        f"PATCH {API}/households/{{household_id}}/members/{{user_id}}": session(
            role=A, step_up=True, params={**HH, "user_id": "member"}
        ),
        f"DELETE {API}/households/{{household_id}}/members/{{user_id}}": session(
            role=A, step_up=True, params={**HH, "user_id": "member"}
        ),
        # --- transactions (Phase 2; account grants applied by the service) --
        f"GET {API}/transactions": scoped("transactions:read", role=V),
        f"POST {API}/transactions": scoped("transactions:write", role=HouseholdRole.MEMBER),
        f"POST {API}/transactions/bulk": scoped("transactions:write", role=HouseholdRole.MEMBER),
        f"POST {API}/transactions/transfers/auto-pair": scoped(
            "transactions:write", role=HouseholdRole.MEMBER
        ),
        f"GET {API}/transactions/{{transaction_id}}": scoped(
            "transactions:read", role=V, params=TXN
        ),
        f"PATCH {API}/transactions/{{transaction_id}}": scoped(
            "transactions:write", role=HouseholdRole.MEMBER, params=TXN
        ),
        f"DELETE {API}/transactions/{{transaction_id}}": scoped(
            "transactions:write", role=HouseholdRole.MEMBER, params=TXN
        ),
        f"POST {API}/transactions/{{transaction_id}}/split": scoped(
            "transactions:write", role=HouseholdRole.MEMBER, params=TXN
        ),
        f"GET {API}/transactions/{{transaction_id}}/transfer-candidates": scoped(
            "transactions:read", role=HouseholdRole.MEMBER, params=TXN
        ),
        f"POST {API}/transactions/{{transaction_id}}/transfer": scoped(
            "transactions:write", role=HouseholdRole.MEMBER, params=TXN
        ),
        f"DELETE {API}/transactions/{{transaction_id}}/transfer": scoped(
            "transactions:write", role=HouseholdRole.MEMBER, params=TXN
        ),
        # --- accounts and balances (Phase 2; visibility applied by the service:
        # the generator grants the member edit and the viewer view) ---------
        f"GET {API}/accounts": scoped("accounts:read", role=V),
        f"POST {API}/accounts": scoped("accounts:write", role=M),
        f"GET {API}/accounts/{{account_id}}": scoped("accounts:read", role=V, params=ACCT),
        f"PATCH {API}/accounts/{{account_id}}": scoped("accounts:write", role=M, params=ACCT),
        f"DELETE {API}/accounts/{{account_id}}": scoped("accounts:write", role=A, params=ACCT),
        f"POST {API}/accounts/{{account_id}}/close": scoped("accounts:write", role=M, params=ACCT),
        f"POST {API}/accounts/{{account_id}}/reopen": scoped("accounts:write", role=M, params=ACCT),
        f"GET {API}/accounts/{{account_id}}/balances": scoped("accounts:read", role=V, params=ACCT),
        f"POST {API}/accounts/{{account_id}}/balances": scoped(
            "accounts:write", role=M, params=ACCT
        ),
        # --- account grants -------------------------------------------------
        f"GET {API}/accounts/{{account_id}}/grants": scoped("accounts:read", role=A, params=ACCT),
        # Members and viewers read only their own grants; the path names
        # another member, so they get 403 here.
        f"GET {API}/households/{{household_id}}/members/{{user_id}}/grants": scoped(
            "households:read", role=A, params={**HH, "user_id": "member"}
        ),
        f"PUT {API}/households/{{household_id}}/members/{{user_id}}/grants/{{account_id}}": session(
            role=A, step_up=True, params=GRANT
        ),
        f"DELETE {API}/households/{{household_id}}/members/{{user_id}}/grants/{{account_id}}": (
            session(role=A, step_up=True, params=GRANT)
        ),
        # --- categories, payees, tags (household-wide) ----------------------
        f"GET {API}/categories": scoped("transactions:read", role=V),
        f"POST {API}/categories": scoped("transactions:write", role=M),
        f"GET {API}/categories/{{category_id}}": scoped("transactions:read", role=V, params=CAT),
        f"PATCH {API}/categories/{{category_id}}": scoped("transactions:write", role=M, params=CAT),
        f"DELETE {API}/categories/{{category_id}}": scoped(
            "transactions:write", role=A, params=CAT
        ),
        f"POST {API}/categories/{{category_id}}/merge": scoped(
            "transactions:write", role=A, params=CAT
        ),
        f"GET {API}/payees": scoped("transactions:read", role=V),
        f"POST {API}/payees": scoped("transactions:write", role=M),
        f"GET {API}/payees/{{payee_id}}": scoped("transactions:read", role=V, params=PAYEE),
        f"PATCH {API}/payees/{{payee_id}}": scoped("transactions:write", role=M, params=PAYEE),
        f"DELETE {API}/payees/{{payee_id}}": scoped("transactions:write", role=A, params=PAYEE),
        f"POST {API}/payees/{{payee_id}}/merge": scoped("transactions:write", role=A, params=PAYEE),
        f"GET {API}/tags": scoped("transactions:read", role=V),
        f"POST {API}/tags": scoped("transactions:write", role=M),
        f"GET {API}/tags/{{tag_id}}": scoped("transactions:read", role=V, params=TAG),
        f"PATCH {API}/tags/{{tag_id}}": scoped("transactions:write", role=M, params=TAG),
        f"DELETE {API}/tags/{{tag_id}}": scoped("transactions:write", role=A, params=TAG),
        # --- rules (household-wide; they rewrite transactions on every account,
        # so changing or applying them needs owner/admin; the dry run only
        # reads the caller's visible accounts) ------------------------------
        f"GET {API}/rules": scoped("transactions:read", role=V),
        f"POST {API}/rules": scoped("transactions:write", role=A),
        f"GET {API}/rules/export": scoped("transactions:read", role=V),
        f"POST {API}/rules/import": scoped("transactions:write", role=A),
        f"POST {API}/rules/reorder": scoped("transactions:write", role=A),
        f"POST {API}/rules/test": scoped("transactions:read", role=V),
        f"POST {API}/rules/apply": scoped("transactions:write", role=A),
        f"GET {API}/rules/{{rule_id}}": scoped("transactions:read", role=V, params=RULE),
        f"PATCH {API}/rules/{{rule_id}}": scoped("transactions:write", role=A, params=RULE),
        f"DELETE {API}/rules/{{rule_id}}": scoped("transactions:write", role=A, params=RULE),
        # --- full export and import (owner/admin, browser session; creating
        # and downloading need step-up; outcomes only for the requester) -----
        f"POST {API}/export": session(role=A, step_up=True),
        f"GET {API}/export/{{job_id}}": session(role=A, params={"job_id": "export_archive"}),
        f"GET {API}/export/{{job_id}}/download": session(
            role=A, step_up=True, params={"job_id": "export_archive"}
        ),
        f"POST {API}/import": session(role=A, step_up=True),
        f"GET {API}/import/{{job_id}}": session(role=A, params={"job_id": "import_archive"}),
        # --- background jobs (state only; the token scope follows the job's
        # family, transactions:read for the run_rules job the matrix builds) --
        f"GET {API}/jobs/{{job_id}}": scoped("transactions:read", role=V, params={"job_id": "job"}),
        # --- budgets and goals (Phase 2): household-wide plans; the report's
        # actuals and the goals follow account visibility ------------------
        f"GET {API}/budgets": scoped("budgets:read", role=V),
        f"POST {API}/budgets": scoped("budgets:write", role=M),
        f"GET {API}/budgets/{{budget_id}}": scoped("budgets:read", role=V, params=BUDGET),
        f"PATCH {API}/budgets/{{budget_id}}": scoped("budgets:write", role=M, params=BUDGET),
        f"DELETE {API}/budgets/{{budget_id}}": scoped("budgets:write", role=M, params=BUDGET),
        f"GET {API}/budgets/{{budget_id}}/report": scoped("budgets:read", role=V, params=BUDGET),
        f"GET {API}/goals": scoped("budgets:read", role=V),
        f"POST {API}/goals": scoped("budgets:write", role=M),
        f"GET {API}/goals/{{goal_id}}": scoped("budgets:read", role=V, params=GOAL),
        f"PATCH {API}/goals/{{goal_id}}": scoped("budgets:write", role=M, params=GOAL),
        f"DELETE {API}/goals/{{goal_id}}": scoped("budgets:write", role=M, params=GOAL),
        f"GET {API}/goals/{{goal_id}}/contributions": scoped("budgets:read", role=V, params=GOAL),
        f"POST {API}/goals/{{goal_id}}/contributions": scoped("budgets:write", role=M, params=GOAL),
        f"DELETE {API}/goals/{{goal_id}}/contributions/{{contribution_id}}": scoped(
            "budgets:write",
            role=M,
            params={**GOAL, "contribution_id": "goal_contribution"},
        ),
        # --- reports (any role; amounts from visible accounts only) ---------
        f"GET {API}/reports/spending": scoped("transactions:read", role=V),
        f"GET {API}/reports/income": scoped("transactions:read", role=V),
        f"GET {API}/reports/trends": scoped("transactions:read", role=V),
        f"GET {API}/reports/cashflow": scoped("transactions:read", role=V),
        # --- documents (Phase 2): visibility follows the document's account;
        # downloading needs member or above (I1: viewers only where the
        # household allows it, 403 by default) -----------------------------
        f"POST {API}/documents": scoped("documents:write", role=M),
        f"GET {API}/documents": scoped("documents:read", role=V),
        f"GET {API}/documents/{{document_id}}": scoped("documents:read", role=V, params=SDOC),
        f"GET {API}/documents/{{document_id}}/download": scoped(
            "documents:read", role=M, params=SDOC
        ),
        f"POST {API}/documents/{{document_id}}/reprocess": scoped(
            "documents:write", role=M, params=SDOC
        ),
        f"POST {API}/documents/{{document_id}}/link": scoped(
            "documents:write", role=M, params=SDOC
        ),
        f"DELETE {API}/documents/{{document_id}}": scoped("documents:write", role=M, params=SDOC),
        # --- review queue (Phase 2): items of visible documents -------------
        f"GET {API}/review": scoped("review:read", role=V),
        f"GET {API}/review/{{item_id}}": scoped("review:read", role=V, params=ITEM),
        f"POST {API}/review/{{item_id}}/resolve": scoped("review:write", role=M, params=ITEM),
        f"POST {API}/review/{{item_id}}/dismiss": scoped("review:write", role=M, params=ITEM),
        # --- live updates (SSE): browser sessions only, any role ------------
        f"GET {API}/events": session(role=V),
        # --- structured imports (Phase 2): mappings follow account visibility;
        # the preview and re-run need the document --------------------------
        f"GET {API}/imports/mappings": scoped("documents:read", role=V),
        f"POST {API}/imports/mappings": scoped("documents:write", role=M),
        f"GET {API}/imports/mappings/{{mapping_id}}": scoped("documents:read", role=V, params=IMAP),
        f"PATCH {API}/imports/mappings/{{mapping_id}}": scoped(
            "documents:write", role=M, params=IMAP
        ),
        f"DELETE {API}/imports/mappings/{{mapping_id}}": scoped(
            "documents:write", role=M, params=IMAP
        ),
        f"GET {API}/imports/{{document_id}}/preview": scoped("documents:read", role=V, params=DOC),
        f"POST {API}/imports/{{document_id}}/apply-mapping": scoped(
            "documents:write", role=M, params=DOC
        ),
        # --- user settings --------------------------------------------------
        f"GET {API}/settings/user": scoped("settings:read"),
        f"PATCH {API}/settings/user": session(),
        # --- instance administration ---------------------------------------
        f"GET {API}/admin/users": instance_admin(),
        f"GET {API}/admin/users/{{user_id}}": instance_admin(params={"user_id": "user"}),
        f"POST {API}/admin/users": instance_admin(step_up=True),
        f"PATCH {API}/admin/users/{{user_id}}": instance_admin(
            step_up=True, params={"user_id": "user"}
        ),
        f"POST {API}/admin/users/{{user_id}}/mfa-reset": instance_admin(
            step_up=True, params={"user_id": "user"}
        ),
    }
)


def names_household(policy: Policy) -> bool:
    return policy.household_object or "household" in policy.params.values()


def expected(policy: Policy, who: str) -> Outcome:
    """What ``who`` must get from a route with ``policy`` (see the module doc)."""
    if who in policy.overrides:
        return policy.overrides[who]
    if policy.access == "public":
        return ALLOWED
    if policy.access == "pending_login":
        return ALLOWED if who == "mfa_pending" else UNAUTHENTICATED
    if policy.access == "mfa_ceremony":
        browser = who not in {"anonymous"} | TOKEN_PRINCIPALS | INVALID_BEARER
        return ALLOWED if browser else UNAUTHENTICATED
    # authenticated / instance_admin
    if who in ("anonymous", "mfa_pending"):
        return UNAUTHENTICATED
    if who in INVALID_BEARER:
        return INVALID_TOKEN
    if who == "restricted":
        return ALLOWED if policy.enrol and policy.access == "authenticated" else MFA_ENROLMENT
    if who in TOKEN_PRINCIPALS:
        if policy.access == "instance_admin" or policy.tokens == "refused" or policy.step_up:
            return TOKEN_REFUSED
        if who == "pat_none":
            return deny(403, "insufficient-scope")
    if policy.access == "instance_admin":
        if who not in INSTANCE_ADMINS:
            return FORBIDDEN
        return STEP_UP if policy.step_up and who == "stale" else ALLOWED
    member_here = who in HOUSEHOLD_ROLE
    if names_household(policy) and not member_here:
        return NOT_IN_HOUSEHOLD
    if policy.role is not None:
        if who == "instance_admin":  # no household at all
            return FORBIDDEN
        role = HOUSEHOLD_ROLE.get(who, HouseholdRole.OWNER)  # other_owner: owner at home
        if _rank(role) < _rank(policy.role):
            return FORBIDDEN
    if policy.step_up and who == "stale":
        return STEP_UP
    if who == "other_owner":
        # Objects of the matrix household (or its owner), handed to an outsider.
        for kind in policy.params.values():
            if kind in OWN_KINDS or kind in HOUSEHOLD_OBJECT_KINDS:
                return deny(404, str(PARAM_KINDS[kind].not_found))
    return ALLOWED


_RANK: Final = {HouseholdRole.VIEWER: 1, HouseholdRole.MEMBER: 2, A: 3, O: 4}


def _rank(role: HouseholdRole) -> int:
    return _RANK[role]


def policy_problems() -> list[str]:
    """Internal consistency of :data:`POLICIES` (run by the matrix test)."""
    problems: list[str] = []
    for key, policy in POLICIES.items():
        if policy.scope is not None and policy.scope not in CATALOG:
            problems.append(f"{key}: scope {policy.scope!r} is not in app.auth.scopes.CATALOG")
        if (policy.tokens == "scope") != (policy.scope is not None):
            problems.append(f"{key}: tokens='scope' needs a scope (and only then)")
        if policy.access != "authenticated" and (
            policy.role or policy.enrol or policy.tokens != "refused"
        ):
            problems.append(f"{key}: role/enrol/tokens only apply to access='authenticated'")
        for name, kind in policy.params.items():
            if kind not in PARAM_KINDS:
                problems.append(f"{key}: unknown parameter kind {kind!r} for {name}")
        for who in policy.overrides:
            if who not in PRINCIPALS:
                problems.append(f"{key}: override for unknown principal {who!r}")
    return problems

# SPDX-License-Identifier: AGPL-3.0-or-later
"""The single classification of every database column for AI access (AI_ACCESS.md M1).

Each column of each table in ``Base.metadata`` is exactly one of

* ``exposed``: a financial fact or a harmless internal value an AI view may
  return as is (amounts, dates, types, ids);
* ``free_text``: text people or statements wrote (payees, memos, notes,
  names people give things); an AI view may return it **only** after the
  scrubber (``app.core.scrubber``) has replaced identifiers;
* ``denied``: never returned by any AI view, with the reason. Identity
  (government ids, account and card numbers including the last four, member
  names, usernames and contacts, addresses, a manual asset's description),
  documents and extracted text, credentials and secrets, tax identity data,
  and internals no view needs.

``tests/security/ai/test_column_classification.py`` enumerates every column
of every mapped table and fails for a column missing here (or listed here but
gone), for a ``denied`` column without a reason, for a column whose model
info says ``classification: identity`` or ``secret`` but is not denied, and
for an AI serialiser field (``app.api.ai.schemas``) whose source column is
denied. A new column is therefore unclassified (and the build red) until the
change that adds it classifies it here.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


class Exposure(enum.StrEnum):
    EXPOSED = "exposed"
    FREE_TEXT = "free_text"
    DENIED = "denied"


@dataclass(frozen=True)
class ColumnClass:
    exposure: Exposure
    reason: str | None = None


EXPOSED: Final = ColumnClass(Exposure.EXPOSED)
FREE_TEXT: Final = ColumnClass(Exposure.FREE_TEXT)

# Reasons for denied columns.
R_ACCOUNT_NUMBER: Final = "identity: account number digits (last 4); AI_ACCESS.md denylist"
R_ADDRESS: Final = "identity: may hold a property address (classification identity)"
R_PERSON: Final = "identity: a person's name, username, e-mail or login data"
R_CREDENTIAL: Final = "credentials and secrets (SECURITY.md credentials class)"
R_DOCUMENT: Final = "documents: originals, extracted text, OCR and their references"
R_INGEST: Final = "ingestion internals (raw statement data, parser state)"
R_AUDIT: Final = "audit and platform internals; not financial facts"
R_ACCESS: Final = "who may see what on this instance (members, grants, sessions)"
R_SETTINGS: Final = "settings and automation; AI clients do not read settings"
R_HOUSEHOLD_NAME: Final = "identity: a household's name is usually a family name"
R_TERMS: Final = "identity: the terms the scrubber removes (names, a school, an employer)"
R_EXPORT: Final = "export archives and their passphrases"
R_AI_AUDIT: Final = "the AI call audit itself (responses as sent)"
R_RAW_ALIASES: Final = "raw statement strings matched to the payee; the canonical name is exposed"
R_BREAKDOWN: Final = "per-account detail with account names; AI views recompute totals"

_COMMON: Final = ("id", "created_at", "updated_at", "household_id")

_registry: dict[str, dict[str, ColumnClass]] = {}


def _table(
    name: str,
    *,
    exposed: str = "",
    free_text: str = "",
    denied: Mapping[str, str] | None = None,
    common: tuple[str, ...] = _COMMON,
) -> None:
    columns: dict[str, ColumnClass] = {}
    for column in (*common, *exposed.split()):
        columns[column] = EXPOSED
    for column in free_text.split():
        columns[column] = FREE_TEXT
    for column, reason in (denied or {}).items():
        columns[column] = ColumnClass(Exposure.DENIED, reason)
    if name in _registry:  # pragma: no cover - a typo in this file
        raise ValueError(f"table {name} classified twice")
    _registry[name] = columns


def _deny_table(name: str, reason: str, columns: str) -> None:
    """Every column (listed explicitly, so a new one is still unclassified) denied."""
    _registry[name] = {c: ColumnClass(Exposure.DENIED, reason) for c in columns.split()}


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

_table(
    "account",
    exposed="institution_id type asset_kind currency is_asset manual_balance "
    "include_in_net_worth opened_on closed_on",
    free_text="name nickname",
    denied={"number_last4": R_ACCOUNT_NUMBER},
)
_table(
    "institution",
    exposed="country",
    free_text="name",
    denied={
        "fingerprint_rules": R_INGEST,
        "default_parser": R_INGEST,
        "builtin_key": R_INGEST,
    },
)
_table(
    "balance_snapshot",
    exposed="account_id as_of_date balance currency source",
    denied={"source_document_id": R_DOCUMENT},
)
_table(
    "transaction",
    exposed="account_id posted_date effective_date amount currency payee_id category_id "
    "status transfer_group_id review_state confidence",
    free_text="raw_payee memo notes",
    denied={"source_document_id": R_DOCUMENT, "import_hash": R_INGEST},
)
_table("transaction_split", exposed="transaction_id category_id amount tax_flag", free_text="memo")
_table("transaction_tag", exposed="transaction_id tag_id")
_table("category", exposed="parent_id kind tax_treatment system_key", free_text="name")
_table(
    "payee",
    exposed="default_category_id",
    free_text="name",
    denied={"normalized_name": R_RAW_ALIASES, "aliases": R_RAW_ALIASES},
)
_table("tag", free_text="name")
_table(
    "scheduled_transaction",
    exposed="account_id cadence interval_count next_due amount_min amount_max currency "
    "payee_id category_id origin active last_seen_on",
    free_text="name",
)
_table("fx_rate", exposed="base quote rate_date rate source")
_table("budget", exposed="period start_date end_date currency rollover", free_text="name")
_table("budget_line", exposed="budget_id category_id amount")
_table("goal", exposed="target_amount currency target_date achieved_at archived", free_text="name")
_table("goal_account", exposed="goal_id account_id")
_table(
    "goal_contribution",
    exposed="goal_id contributed_on amount currency transaction_id",
    free_text="memo",
)

# ---------------------------------------------------------------------------
# Investments, loans, assets, net worth
# ---------------------------------------------------------------------------

# A security's symbol, name, sector and exchange are typed by members or come
# from a statement (OFX SECNAME): free text, scrubbed like a payee
# (P6B-SR-R1-01). isin/cusip are fixed-format public identifiers.
_table(
    "security",
    exposed="isin cusip asset_class currency",
    free_text="symbol name sector exchange",
)
_table("price", exposed="security_id price_date close currency source")
_table("investment_account", exposed="account_id cost_method")
_table(
    "investment_txn",
    exposed="account_id security_id type trade_date settle_date quantity price amount fees "
    "currency lots_specified",
    free_text="memo",
    denied={
        "lot_links": R_INGEST,
        "import_hash": R_INGEST,
        "source_document_id": R_DOCUMENT,
    },
)
_table(
    "lot",
    exposed="account_id security_id open_date quantity remaining_quantity cost_per_unit "
    "currency closed_date opening_txn_id",
)
_table("holding", exposed="account_id security_id quantity cost_basis currency as_of")
_table(
    "loan",
    exposed="account_id principal_original currency rate rate_type term_months "
    "origination_date payment_amount payment_day escrow_amount",
    free_text="lender",
)
_table(
    "loan_payment",
    exposed="loan_id paid_on principal interest escrow fees currency transaction_id",
    denied={"source_document_id": R_DOCUMENT},
)
_table("loan_rate_change", exposed="loan_id effective_date new_rate")
_table("manual_asset", exposed="account_id valuation_method", denied={"description": R_ADDRESS})
_table("asset_valuation", exposed="account_id valued_on value currency source")
_table(
    "net_worth_snapshot",
    exposed="as_of_date currency assets liabilities net",
    denied={"breakdown": R_BREAKDOWN},
)

# ---------------------------------------------------------------------------
# Tax vault
# ---------------------------------------------------------------------------

_table("tax_year", exposed="year jurisdiction filing_status state", free_text="notes")
_table(
    "tax_checklist_item",
    exposed="tax_year_id expected_form status origin",
    free_text="issuer",
    denied={"document_id": R_DOCUMENT},
)
_table("tax_category_map", exposed="category_id jurisdiction schedule line")
_table(
    "tax_form_data",
    # ``boxes`` holds amounts only: identity boxes are never stored
    # (``app.domain.tax.service.record_form_data``, SECURITY.md E1).
    exposed="tax_year tax_year_id jurisdiction form_type form_index boxes source confidence",
    free_text="issuer_name",
    denied={"document_id": R_DOCUMENT},
)
_deny_table(
    "tax_export",
    R_EXPORT,
    "tax_year_id year state job_id requested_by blob_id size sha256 expires_at finished_at "
    "downloaded_at error summary id created_at updated_at household_id",
)

# ---------------------------------------------------------------------------
# Households, identity, access, credentials
# ---------------------------------------------------------------------------

_table(
    "household",
    exposed="base_currency",
    denied={"name": R_HOUSEHOLD_NAME},
    common=("id", "created_at", "updated_at"),
)
_deny_table("household_member", R_ACCESS, "user_id role id created_at updated_at household_id")
_deny_table(
    "app_user",
    R_PERSON,
    "username display_name email email_verified is_instance_admin is_active is_breakglass "
    "locked_until failed_login_count last_failed_login_at last_login_at id created_at updated_at",
)
_deny_table(
    "credential",
    R_CREDENTIAL,
    "user_id password_hash hash_params rotated_at id created_at updated_at",
)
_deny_table(
    "identity",
    R_PERSON,
    "user_id provider issuer subject email_at_link linked_at id created_at updated_at",
)
_deny_table(
    "user_session",
    R_ACCESS,
    "token_hash user_id household_id stage login_method user_agent ip last_seen_at "
    "idle_expires_at absolute_expires_at mfa_verified_at remember_device revoked_at "
    "revoked_reason rotated_from_id id created_at updated_at",
)
_deny_table(
    "api_token",
    R_CREDENTIAL,
    "prefix token_hash user_id name scopes expires_at last_used_at revoked_at id created_at "
    "updated_at household_id",
)
_deny_table(
    "mfa_method",
    R_CREDENTIAL,
    "user_id type name secret_encrypted public_key credential_id sign_count transports "
    "code_hash totp_last_counter last_used_at consumed_at id created_at updated_at",
)
_deny_table(
    "mfa_challenge",
    R_CREDENTIAL,
    "session_id user_id purpose challenge secret_encrypted expires_at id created_at updated_at",
)
_deny_table(
    "oidc_session",
    R_CREDENTIAL,
    "session_id user_id issuer subject sid id_token_encrypted refresh_token_encrypted "
    "claims_refreshed_at id created_at updated_at",
)
_deny_table("oidc_used_state", R_CREDENTIAL, "state_hash expires_at created_at")
_deny_table(
    "password_reset_token",
    R_CREDENTIAL,
    "token_hash user_id expires_at used_at requested_ip id created_at updated_at",
)
_deny_table("rate_limit_event", R_AUDIT, "id scope key_hash created_at")
_deny_table(
    "account_grant", R_ACCESS, "user_id account_id level id created_at updated_at household_id"
)

# ---------------------------------------------------------------------------
# Documents and ingestion
# ---------------------------------------------------------------------------

_deny_table(
    "document",
    R_DOCUMENT,
    "blob_id kind status source source_meta sha256 size mime_detected original_filename "
    "uploaded_by account_id institution_id period_start period_end tax_year extracted_text "
    "extraction confidence parser_version error id created_at updated_at household_id",
)
_deny_table(
    "document_link",
    R_DOCUMENT,
    "document_id target_type target_id id created_at updated_at household_id",
)
_deny_table(
    "blob",
    R_DOCUMENT,
    "store key area size stored_size enc_alg key_version wrap_nonce wrapped_dek nonce "
    "shredded_at id created_at updated_at household_id",
)
_deny_table(
    "review_item",
    R_DOCUMENT,
    "document_id kind payload state resolved_by resolved_at resolution id created_at "
    "updated_at household_id",
)
_deny_table(
    "ingest_job",
    R_INGEST,
    "document_id stage attempt started_at finished_at error log id created_at updated_at "
    "household_id",
)
_deny_table(
    "import_mapping",
    R_INGEST,
    "name institution_id account_id header_fingerprint spec created_by use_count sign_unverified "
    "last_used_at id created_at updated_at household_id",
)
_deny_table(
    "import_hash_alias",
    R_INGEST,
    "account_id transaction_id import_hash id created_at updated_at household_id",
)
_deny_table(
    "parser_fingerprint",
    R_INGEST,
    "institution_id kind match_rules parser_name version learned_from_document_id id "
    "created_at updated_at household_id",
)
_deny_table(
    "dropfolder_entry",
    R_INGEST,
    "path_key rel_path size mtime_ns inode sha256 document_id outcome reason after_state "
    "after_result id created_at updated_at household_id",
)
_deny_table(
    "dropfolder_route",
    R_INGEST,
    "folder user_id account_id id created_at updated_at household_id",
)
_deny_table(
    "mail_intake",
    R_CREDENTIAL,
    "enabled host port username_encrypted password_encrypted inbox_folder processed_folder "
    "rejected_folder alias_encrypted alias_rotated_at allowed_senders member_addresses "
    "require_auth_results authserv_id last_poll_at last_poll_outcome last_poll_counts id "
    "created_at updated_at household_id",
)
_deny_table(
    "notification_channel",
    R_CREDENTIAL,
    "name type config_encrypted events enabled last_sent_at last_outcome id created_at "
    "updated_at household_id",
)
_deny_table(
    "mail_intake_message",
    R_INGEST,
    "message_key outcome reason document_id id created_at updated_at household_id",
)

# ---------------------------------------------------------------------------
# Platform
# ---------------------------------------------------------------------------

_deny_table(
    "audit_log",
    R_AUDIT,
    "id seq household_id user_id action target_type target_id before after ip user_agent "
    "request_id created_at prev_hash hash",
)
_deny_table("event", R_AUDIT, "type payload id created_at updated_at household_id")
_deny_table(
    "job",
    R_AUDIT,
    "queue type payload state priority run_after attempts max_attempts locked_by locked_at "
    "lease_expires_at last_error household_id dedupe_key id created_at updated_at",
)
_deny_table(
    "idempotency_key",
    R_AUDIT,
    "user_id key request_hash status_code response_body created_at expires_at id household_id",
)
_deny_table("setting", R_SETTINGS, "scope scope_id key value id created_at updated_at")
_deny_table(
    "rule",
    R_SETTINGS,
    "name sort_order enabled stop_processing conditions actions hit_count last_hit_at id "
    "created_at updated_at household_id",
)
_deny_table(
    "portability_archive",
    R_EXPORT,
    "kind state job_id requested_by blob_id passphrase_protected passphrase_key size sha256 "
    "expires_at finished_at downloaded_at error summary id created_at updated_at household_id",
)

# ---------------------------------------------------------------------------
# AI access itself
# ---------------------------------------------------------------------------

_table(
    "ai_client",
    exposed="mode transport rounding hide_nicknames hide_merchant_location date_range_cap "
    "expires_at revoked_at last_used_at",
    free_text="name",
    denied={
        "custom_denylist": R_TERMS,
        "member_aliases": R_TERMS,
        "token_id": R_CREDENTIAL,
        "created_by": R_PERSON,
    },
)
_deny_table(
    "ai_call",
    R_AI_AUDIT,
    "ai_client_id tool params status bytes response id created_at updated_at household_id",
)


#: ``{table: {column: ColumnClass}}`` for every table (module doc).
CLASSIFICATION: Final[Mapping[str, Mapping[str, ColumnClass]]] = MappingProxyType(
    {table: MappingProxyType(cols) for table, cols in _registry.items()}
)


def classify(table: str, column: str) -> ColumnClass | None:
    """The class of ``table.column``, ``None`` when it is not classified."""
    return CLASSIFICATION.get(table, {}).get(column)


def is_exposable(source: str) -> bool:
    """Whether ``"<table>.<column>"`` may feed an AI serialiser field."""
    table, _, column = source.partition(".")
    found = classify(table, column)
    return found is not None and found.exposure is not Exposure.DENIED

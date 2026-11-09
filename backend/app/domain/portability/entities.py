# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a full export contains and how its references are re-mapped on import.

Each :class:`EntitySpec` is one ``data/<name>.json`` member: every column of
the table except ``household_id`` and the :attr:`EntitySpec.exclude`\\d ones
(user references, which do not travel between instances, and storage
internals). :attr:`EntitySpec.refs` names, per id column, the entity whose
ids it holds; :data:`ENTITIES` is in dependency order, so an importer can
insert front to back with every referenced id already mapped.

Household-scoped tables that are deliberately **not** exported are listed
in :data:`NOT_EXPORTED` with the reason (``tests/unit/test_portability.py``
fails for a table in neither list).

:attr:`EntitySpec.since` is the archive format version that introduced the
entity: an older archive has no such member and imports none of its rows
(format version 1 had no Phase 3-6 tables).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import Column

from app.db.base import Base
from app.domain.accounts.models import Account, BalanceSnapshot, Institution
from app.domain.ai.models import AiClient
from app.domain.assets.models import AssetValuation, ManualAsset
from app.domain.budgets.models import Budget, BudgetLine
from app.domain.documents.models import Document, DocumentLink, ParserFingerprint
from app.domain.fx.models import FxRate
from app.domain.goals.models import Goal, GoalAccount, GoalContribution
from app.domain.imports.models import ImportMapping
from app.domain.investments.models import InvestmentAccount, InvestmentTxn, Lot, Price, Security
from app.domain.loans.models import Loan, LoanPayment, LoanRateChange
from app.domain.networth.models import NetWorthSnapshot
from app.domain.tax.models import TaxCategoryMap, TaxChecklistItem, TaxFormData, TaxYear
from app.domain.transactions.models import (
    Category,
    Payee,
    Rule,
    ScheduledTransaction,
    Tag,
    Transaction,
    TransactionSplit,
    TransactionTag,
)

#: ``data/<name>.json``.
DATA_PREFIX: Final = "data/"
CSV_PREFIX: Final = "csv/"
DOCUMENTS_PREFIX: Final = "documents/"
MANIFEST: Final = "manifest.json"


@dataclass(frozen=True)
class EntitySpec:
    """One exported table (module doc)."""

    name: str
    model: type[Base]
    #: id column -> entity name whose ids it holds.
    refs: Mapping[str, str] = field(default_factory=dict)
    #: Columns not exported (their value on import is NULL or computed).
    exclude: frozenset[str] = frozenset()
    #: The archive format version that introduced the entity (module doc).
    since: int = 1

    @property
    def member(self) -> str:
        return f"{DATA_PREFIX}{self.name}.json"

    @property
    def table(self) -> Any:
        return self.model.__table__

    def columns(self) -> list[Column[Any]]:
        """The exported columns, in table order."""
        return [
            c for c in self.table.columns if c.name != "household_id" and c.name not in self.exclude
        ]


ENTITIES: Final[tuple[EntitySpec, ...]] = (
    EntitySpec("institutions", Institution),
    EntitySpec("categories", Category, refs={"parent_id": "categories"}),
    EntitySpec("payees", Payee, refs={"default_category_id": "categories"}),
    EntitySpec("tags", Tag),
    EntitySpec("accounts", Account, refs={"institution_id": "institutions"}),
    EntitySpec(
        "documents",
        Document,
        refs={"account_id": "accounts", "institution_id": "institutions"},
        # blob: re-encrypted into a new blob on import; extracted_text: the
        # Phase 3 full-text index, rebuilt; uploaded_by: a user of this instance.
        exclude=frozenset({"blob_id", "extracted_text", "uploaded_by"}),
    ),
    EntitySpec(
        "transactions",
        Transaction,
        refs={
            "account_id": "accounts",
            "payee_id": "payees",
            "category_id": "categories",
            "source_document_id": "documents",
        },
    ),
    EntitySpec(
        "transaction_splits",
        TransactionSplit,
        refs={"transaction_id": "transactions", "category_id": "categories"},
    ),
    EntitySpec(
        "transaction_tags",
        TransactionTag,
        refs={"transaction_id": "transactions", "tag_id": "tags"},
    ),
    EntitySpec(
        "balance_snapshots",
        BalanceSnapshot,
        refs={"account_id": "accounts", "source_document_id": "documents"},
    ),
    EntitySpec(
        "scheduled_transactions",
        ScheduledTransaction,
        refs={"account_id": "accounts", "payee_id": "payees", "category_id": "categories"},
    ),
    EntitySpec("rules", Rule),
    EntitySpec("budgets", Budget),
    EntitySpec(
        "budget_lines", BudgetLine, refs={"budget_id": "budgets", "category_id": "categories"}
    ),
    EntitySpec("goals", Goal),
    EntitySpec("goal_accounts", GoalAccount, refs={"goal_id": "goals", "account_id": "accounts"}),
    EntitySpec(
        "goal_contributions",
        GoalContribution,
        refs={"goal_id": "goals", "transaction_id": "transactions"},
    ),
    EntitySpec("fx_rates", FxRate),
    EntitySpec(
        "import_mappings",
        ImportMapping,
        refs={"institution_id": "institutions", "account_id": "accounts"},
        exclude=frozenset({"created_by"}),
    ),
    # ---- format version 2: the Phase 3-6 tables --------------------------
    EntitySpec(
        "parser_fingerprints",
        ParserFingerprint,
        refs={"institution_id": "institutions", "learned_from_document_id": "documents"},
        since=2,
    ),
    EntitySpec("securities", Security, since=2),
    EntitySpec("prices", Price, refs={"security_id": "securities"}, since=2),
    EntitySpec("investment_accounts", InvestmentAccount, refs={"account_id": "accounts"}, since=2),
    EntitySpec(
        "investment_txns",
        InvestmentTxn,
        # ``lot_links`` holds lot ids too; they are re-mapped by the importer.
        refs={
            "account_id": "accounts",
            "security_id": "securities",
            "source_document_id": "documents",
        },
        since=2,
    ),
    EntitySpec(
        "lots",
        Lot,
        refs={
            "account_id": "accounts",
            "security_id": "securities",
            "opening_txn_id": "investment_txns",
        },
        since=2,
    ),
    EntitySpec("loans", Loan, refs={"account_id": "accounts"}, since=2),
    EntitySpec(
        "loan_payments",
        LoanPayment,
        refs={
            "loan_id": "loans",
            "transaction_id": "transactions",
            "source_document_id": "documents",
        },
        since=2,
    ),
    EntitySpec("loan_rate_changes", LoanRateChange, refs={"loan_id": "loans"}, since=2),
    EntitySpec("manual_assets", ManualAsset, refs={"account_id": "accounts"}, since=2),
    EntitySpec("asset_valuations", AssetValuation, refs={"account_id": "accounts"}, since=2),
    # ``breakdown`` names accounts and securities; re-mapped by the importer.
    EntitySpec("net_worth_snapshots", NetWorthSnapshot, since=2),
    EntitySpec("tax_years", TaxYear, since=2),
    EntitySpec(
        "tax_checklist_items",
        TaxChecklistItem,
        refs={"tax_year_id": "tax_years", "document_id": "documents"},
        since=2,
    ),
    EntitySpec("tax_category_maps", TaxCategoryMap, refs={"category_id": "categories"}, since=2),
    EntitySpec(
        "tax_form_data",
        TaxFormData,
        refs={"document_id": "documents", "tax_year_id": "tax_years"},
        since=2,
    ),
    EntitySpec(
        "ai_clients",
        AiClient,
        # token_id: the client's bearer token of this instance (a secret that
        # never leaves); created_by: a user of this instance. An imported
        # client has no token and arrives revoked (docs/PORTABILITY.md).
        exclude=frozenset({"token_id", "created_by"}),
        since=2,
    ),
    # Last: a link may name a row of any of the entities above.
    EntitySpec("document_links", DocumentLink, refs={"document_id": "documents"}),
)

BY_NAME: Final[Mapping[str, EntitySpec]] = MappingProxyType({e.name: e for e in ENTITIES})

#: ``document_link.target_type`` -> the entity its ``target_id`` names.
LINK_TARGETS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "transaction": "transactions",
        "account": "accounts",
        "investment_txn": "investment_txns",
        "loan_payment": "loan_payments",
        "tax_year": "tax_years",
    }
)


def entities_for(version: int) -> tuple[EntitySpec, ...]:
    """The entities an archive of format ``version`` holds."""
    return tuple(spec for spec in ENTITIES if spec.since <= version)


#: Household-scoped tables left out of the export, and why.
# A variable, not a literal: bandit's B105 reads a "token" key with a string
# value as a hard-coded password (a ``nosec`` on the dict covers every line).
_PAT_REASON: Final = "personal access tokens of this instance"

NOT_EXPORTED: Final[Mapping[str, str]] = MappingProxyType(
    {
        # Secrets and per-instance identity: never leave the instance.
        "api_token": _PAT_REASON,
        "account_grant": "per-user access of this instance (user ids do not travel)",
        "household_member": "users of this instance",
        "mail_intake": (
            "the mailbox connection of this instance: credentials and the intake alias are "
            "secrets, allowed senders map to this instance's user ids; set it up again"
        ),
        "dropfolder_route": "drop-folder subfolders of this instance's mount, by user id",
        "notification_channel": (
            "notification destinations and their credentials are secrets of this instance; "
            "set the channels up again"
        ),
        # Storage internals, sessions and caches.
        "blob": "storage internals; document originals are exported decrypted",
        "idempotency_key": "request replay cache",
        "event": "live-update stream",
        "holding": "derived cache, rebuilt from the imported lots",
        "import_hash_alias": (
            "the hash of a deleted duplicate cannot be recomputed for new account ids"
        ),
        "statement_record": (
            "the statement duplicate guard's history of this instance's imports; documents "
            "imported again record their statements anew"
        ),
        # Jobs and workflow history of this instance's pipelines.
        "portability_archive": "the export and import jobs themselves",
        "tax_export": "a transient, one-time accountant export (like portability_archive)",
        "review_item": "workflow state of this instance's ingestion",
        "ingest_job": "pipeline history of this instance",
        "dropfolder_entry": "which inbox files this instance has taken",
        "mail_intake_message": "which mailbox messages this instance has taken",
        # Audit of this instance (like audit_log, which is instance-wide).
        "ai_call": (
            "audit of what this instance's AI clients read; its response bodies only "
            "repeat exported data"
        ),
    }
)

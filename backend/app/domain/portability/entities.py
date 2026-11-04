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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import Column

from app.db.base import Base
from app.domain.accounts.models import Account, BalanceSnapshot, Institution
from app.domain.budgets.models import Budget, BudgetLine
from app.domain.documents.models import Document, DocumentLink
from app.domain.fx.models import FxRate
from app.domain.goals.models import Goal, GoalAccount, GoalContribution
from app.domain.imports.models import ImportMapping
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
    EntitySpec("document_links", DocumentLink, refs={"document_id": "documents"}),
)

BY_NAME: Final[Mapping[str, EntitySpec]] = MappingProxyType({e.name: e for e in ENTITIES})

#: ``document_link.target_type`` -> the entity its ``target_id`` names; links
#: to other targets (investment transactions, loan payments, tax years:
#: later phases) are not exported yet.
LINK_TARGETS: Final[Mapping[str, str]] = MappingProxyType(
    {"transaction": "transactions", "account": "accounts"}
)

#: Household-scoped tables left out of the export, and why.
# A variable, not a literal: bandit's B105 reads a "token" key with a string
# value as a hard-coded password (a ``nosec`` on the dict covers every line).
_PAT_REASON: Final = "personal access tokens of this instance"

NOT_EXPORTED: Final[Mapping[str, str]] = MappingProxyType(
    {
        "blob": "storage internals; document originals are exported decrypted",
        "review_item": "workflow state of this instance's ingestion",
        "ingest_job": "pipeline history of this instance",
        "parser_fingerprint": "learned parser state, rebuilt from new imports",
        "import_hash_alias": (
            "the hash of a deleted duplicate cannot be recomputed for new account ids"
        ),
        "account_grant": "per-user access of this instance",
        "household_member": "users of this instance",
        "api_token": _PAT_REASON,
        "idempotency_key": "request replay cache",
        "event": "live-update stream",
        "portability_archive": "the export and import jobs themselves",
        "dropfolder_entry": "which inbox files this instance has taken",
        "dropfolder_route": "drop-folder subfolders of this instance's mount, by user id",
        "mail_intake": "mailbox credentials of this instance (secrets are not exported)",
        "mail_intake_message": "which mailbox messages this instance has taken",
        # Phase 5/6 tables: not in the archive format yet (docs/PORTABILITY.md).
        "security": "Phase 5/6 export not implemented yet",
        "price": "Phase 5/6 export not implemented yet",
        "investment_txn": "Phase 5/6 export not implemented yet",
        "lot": "Phase 5/6 export not implemented yet",
        "holding": "derived cache, rebuilt from lots",
        "investment_account": "Phase 5/6 export not implemented yet",
        "loan": "Phase 5/6 export not implemented yet",
        "loan_payment": "Phase 5/6 export not implemented yet",
        "loan_rate_change": "Phase 5/6 export not implemented yet",
        "manual_asset": "Phase 5/6 export not implemented yet",
        "asset_valuation": "Phase 5/6 export not implemented yet",
        "net_worth_snapshot": "derived, recomputed from the ledger",
        "tax_year": "Phase 5/6 export not implemented yet",
        "tax_checklist_item": "Phase 5/6 export not implemented yet",
        "tax_category_map": "Phase 5/6 export not implemented yet",
        "tax_form_data": "Phase 5/6 export not implemented yet",
        "tax_export": "a transient, one-time accountant export (like portability_archive)",
    }
)

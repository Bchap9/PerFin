# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a brokerage statement says about itself (``GET /documents/{id}/summary``).

A statement prints, beside its holdings and activity, tables that only
restate them: the change in the account's value over the period, the cash
flow of its core position, its income by tax treatment, its realized gains,
and a total under each section of activity. The parsers read them as what
they are (``StatementFigure``, ``SectionCheck``: never as positions or
activity) and use them to check the rows. This module shows them, per
account of the document, read from the stored extraction: nothing is
computed here and nothing is written.

The caller needs to be able to edit the document (the same people who may
assign its accounts, ``app.domain.imports.proposals``): the figures are an
account's balances.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Final

from sqlalchemy.orm import Session

from app.auth.principal import Principal
from app.domain.documents.manage import get_editable_document
from app.domain.imports.schemas import (
    DocumentSummary,
    StatementSummary,
    SummaryGroup,
    SummaryLine,
    SummarySection,
)
from app.domain.imports.service import stored_investments
from app.ingestion.sandbox.protocol import Balance, InvestmentStatement

#: The summary tables, in the order they are shown.
GROUPS: Final = ("account_value", "cash_flow", "income", "realized")
#: Statements, lines of a table and sections shown at most.
STATEMENTS_MAX: Final = 50
LINES_MAX: Final = 100
SECTIONS_MAX: Final = 100


def _text(value: Decimal | None) -> str | None:
    return f"{value:f}" if value is not None else None


def _balance(balance: Balance | None) -> str | None:
    return _text(balance.amount) if balance is not None else None


def _groups(statement: InvestmentStatement) -> list[SummaryGroup]:
    out: list[SummaryGroup] = []
    for group in GROUPS:
        lines = [
            SummaryLine(
                label=figure.label,
                role=figure.role,
                detail=figure.detail,
                amount=_text(figure.amount),
                year_to_date=_text(figure.year_to_date),
                page=figure.page,
            )
            for figure in statement.figures
            if figure.group == group
        ][:LINES_MAX]
        if lines:
            out.append(SummaryGroup(group=group, lines=lines))
    return out


def _sections(statement: InvestmentStatement) -> list[SummarySection]:
    check = statement.reconciliation
    if check is None:
        return []
    return [
        SummarySection(
            label=section.label,
            rows=len(section.rows),
            status=section.status,
            printed=_text(section.printed),
            computed=f"{section.computed:f}",
            difference=_text(section.difference),
            source=section.source,
            page=section.page,
        )
        for section in check.sections[:SECTIONS_MAX]
    ]


def statement_summary(index: int, statement: InvestmentStatement) -> StatementSummary:
    check = statement.reconciliation
    return StatementSummary(
        index=index,
        last4=statement.account.last4,
        name=statement.account.name,
        currency=statement.account.currency,
        period_start=statement.period_start,
        period_end=statement.period_end,
        as_of=statement.as_of,
        positions=len(statement.positions),
        activity=len(statement.activity),
        positions_value=_balance(statement.positions_value),
        cash_opening=_balance(statement.cash_opening),
        cash_closing=_balance(statement.cash_closing),
        total_value=_balance(statement.total_value),
        reconciliation=check.status if check is not None else None,
        checks=list(check.checks) if check is not None else [],
        failed=list(check.failed) if check is not None else [],
        groups=_groups(statement),
        sections=_sections(statement),
    )


def document_summary(db: Session, principal: Principal, document_id: uuid.UUID) -> DocumentSummary:
    """``GET /documents/{id}/summary`` (module doc); no statements: an empty list."""
    document = get_editable_document(db, principal, document_id)
    statements = stored_investments(document, db)[:STATEMENTS_MAX]
    return DocumentSummary(
        document_id=document.id,
        statements=[statement_summary(i, s) for i, s in enumerate(statements)],
    )

# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the household has taught PerFin about its documents, to look at and to forget.

"Remember this layout" and "Teach PerFin" store a ``parser_fingerprint`` row
per layout (:mod:`app.ingestion.classify.fingerprints`,
:mod:`app.domain.review.teach`): the header phrases that recognise a
document, the parser that reads it and, when taught, a template (labels and
column headers, never values). Until now a person could add to that memory
but not see it or take anything back, so a layout learned from a wrong
answer kept misreading every later statement of that institution.

* :func:`list_layouts` (``GET /imports/layouts``, any role): the layouts in
  use, one per institution, kind and parser (the newest version, as the
  classifier uses them: :func:`household_fingerprints`), with what each
  recognises and reads. Everything shown was written by the household and
  holds no digit (the rule every stored label and header line obeys), so
  there is nothing here a viewer could not already read on a statement's
  first page.
* :func:`delete_layout` (``DELETE /imports/layouts/{id}``, member or above,
  like column mappings): removes **every** version of that layout, so an
  older one is not used in its place. Documents already imported keep their
  rows; the next statement of that layout is read by the generic parsers
  again (and can be taught afresh). Audited (``parser_fingerprint.delete``).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.authz import role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.domain.accounts.models import Institution
from app.domain.documents.models import ParserFingerprint
from app.domain.imports.schemas import LearnedLayoutList, LearnedLayoutOut, LearnedTable
from app.ingestion.classify.fingerprints import (
    FingerprintError,
    household_fingerprints,
    lock_layouts,
    parse_match_rules,
)
from app.ingestion.parsers.template import parse_template


def layout_not_found() -> ProblemError:
    return ProblemError(404, "layout-not-found", "Not found", "No such learned layout.")


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or not role_at_least(principal.role, minimum):
        raise layout_not_found()
    return principal.household_id


def _recognised_by(match_rules: dict[str, Any]) -> list[str]:
    """The phrases a document must print for the layout to be recognised."""
    try:
        match = parse_match_rules(match_rules)
    except FingerprintError:
        return []
    out: list[str] = []
    for rule in (*match.all, *match.one_of):
        phrase = rule.header or rule.text or rule.metadata
        if phrase:
            out.append(phrase)
    return out


def _out(row: ParserFingerprint, institution: str) -> LearnedLayoutOut:
    template = parse_template(row.template)
    return LearnedLayoutOut(
        id=row.id,
        institution_id=row.institution_id,
        institution_name=institution,
        kind=row.kind,
        parser=row.parser_name,
        version=row.version,
        learned_at=row.created_at,
        learned_from_document_id=row.learned_from_document_id,
        recognised_by=_recognised_by(row.match_rules),
        taught=template is not None,
        fields=sorted({anchor.field for anchor in template.fields}) if template else [],
        tables=(
            [
                LearnedTable(
                    kind=table.kind,
                    columns=[c.role for c in table.columns if c.role != "ignore"],
                )
                for table in template.tables
            ]
            if template
            else []
        ),
        ignored_lines=len(template.ignore) if template else 0,
        notes=list(template.notes) if template else [],
    )


def list_layouts(db: Session, principal: Principal) -> LearnedLayoutList:
    """The layouts the classifier uses for this household, newest first."""
    household_id = _household(principal, HouseholdRole.VIEWER)
    used = [f.fingerprint_id for f in household_fingerprints(db, household_id) if f.fingerprint_id]
    if not used:
        return LearnedLayoutList(items=[])
    rows = db.execute(
        select(ParserFingerprint, Institution.name)
        .join(
            Institution,
            (Institution.household_id == ParserFingerprint.household_id)
            & (Institution.id == ParserFingerprint.institution_id),
        )
        .where(ParserFingerprint.household_id == household_id, ParserFingerprint.id.in_(used))
        .order_by(ParserFingerprint.created_at.desc(), ParserFingerprint.id.desc())
    ).all()
    return LearnedLayoutList(items=[_out(row, name) for row, name in rows])


def delete_layout(
    db: Session, principal: Principal, layout_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    """Forget a layout: every version of its institution, kind and parser."""
    household_id = _household(principal, HouseholdRole.MEMBER)
    lock_layouts(db, household_id)
    row = db.execute(
        select(ParserFingerprint).where(
            ParserFingerprint.household_id == household_id, ParserFingerprint.id == layout_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise layout_not_found()
    before = {
        "institution_id": str(row.institution_id),
        "kind": row.kind.value,
        "parser": row.parser_name,
        "version": row.version,
        "taught": row.template is not None,
    }
    db.execute(
        delete(ParserFingerprint).where(
            ParserFingerprint.household_id == household_id,
            ParserFingerprint.institution_id == row.institution_id,
            ParserFingerprint.kind == row.kind,
            ParserFingerprint.parser_name == row.parser_name,
        )
    )
    audit(
        db,
        action="parser_fingerprint.delete",
        target_type="parser_fingerprint",
        target_id=layout_id,
        before=before,
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(db, household_id, "document.layout_deleted", {"id": str(layout_id)})

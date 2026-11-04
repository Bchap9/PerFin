# SPDX-License-Identifier: AGPL-3.0-or-later
"""Documents never reach an ``ai:read`` client (AI_ACCESS.md denylist, SECURITY.md E1).

Security review P56-SR-R1-02 left one limit on document redaction: the page
text of a document a statement, brokerage, loan or payslip parser read is kept
as extracted (its review tools need it), so identity printed on such a page
stays in ``document.extraction`` and the search index. This module shows that
the limit does not reach the AI boundary, whatever is in a document:

* **structurally**: every document table (``document``, ``document_link``,
  ``blob``, ``review_item``, ``ingest_job``) is denied column by column, every
  reference to a document from another table is denied, and no AI route
  serves a document, a preview, a region, a review item or a search;
* **behaviourally**: a household holding an *unredacted* prior-year Form 1040,
  a payslip and a statement whose kept page text carries an SSN, a name, an
  address and an employee number (plus a W-2 form record whose issuer name is
  a person) is read through every AI view; no document value appears in any
  answer or in any ``ai_call`` row, and the ``ai:read`` token is refused on
  every document route.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.settings import Settings
from app.db.base import Base
from app.domain.accounts.models import Account
from app.domain.ai.classification import Exposure, classify
from app.domain.documents.models import Document, DocumentKind, DocumentLink, LinkTarget
from app.ingestion.sandbox.protocol import ExtractResult, Page
from app.main import create_app
from tests.fixtures.ai import ai_client, ai_headers
from tests.fixtures.factories import Factories
from tests.fixtures.imports import make_extracted_document
from tests.fixtures.ledger import LedgerFactories, household_context, make_transaction
from tests.fixtures.tax import make_form_data
from tests.security.ai.test_ai_views import (
    API,
    RECENT,
    TODAY,
    World,
    build_world,
    calls_of,
    every_call,
    leaks,
)
from tests.security.test_authz_matrix import route_keys

pytestmark = [pytest.mark.security, pytest.mark.integration]

#: Values that exist only inside the planted documents (page text, file
#: names, a parsed form's issuer). None is a member name, so the scrubber's
#: member terms do not help: only the allowlist keeps them out.
DOCUMENT_ONLY = (
    "Zebulon",
    "Quixote",
    "987-65-4320",
    "987654320",
    "1600 Synthetic",
    "Lantern Row",
    "EMP4455667",
    "f1040_quixote",
    "payslip_quixote",
    "statement_quixote",
    "Quixote Holdings",
)

#: Tables that are documents or document internals (DATA_MODEL.md "Documents").
DOCUMENT_TABLES = frozenset({"document", "document_link", "blob", "review_item", "ingest_job"})
#: Column names that reference or hold document content anywhere else.
DOCUMENT_COLUMNS = frozenset(
    {"document_id", "source_document_id", "extracted_text", "extraction", "original_filename"}
)


def document_leaks(body: str) -> list[str]:
    folded = body.casefold()
    return [value for value in DOCUMENT_ONLY if value.casefold() in folded]


def _pages(text: str) -> list[Page]:
    return [Page(number=1, width=612, height=792, unit="pt", text_source="text_layer", text=text)]


def _document(db: Session, world: World, kind: DocumentKind, filename: str, text: str) -> Document:
    result = ExtractResult(kind="pdf", ok=True, extractor_version="pdf/1", pages=_pages(text))
    result = result.model_copy(update={"text": text})
    document = make_extracted_document(db, world.household_id, result=result)
    with household_context(db, world.household_id):
        document.kind = kind
        document.original_filename = filename
        db.flush()
    return document


def plant_documents(ledger: LedgerFactories, world: World) -> list[uuid.UUID]:
    """Documents whose kept text carries identity, linked to the ledger and tax year."""
    year = TODAY.year - 1

    def plant(db: Session) -> list[uuid.UUID]:
        # The P56-SR-R1-02 shapes: an unregistered tax return and the
        # documents whose page text is kept as extracted.
        f1040 = _document(
            db,
            world,
            DocumentKind.TAX_FORM,
            "f1040_quixote.pdf",
            f"Form 1040 U.S. Individual Income Tax Return {year - 1}\n"
            "Your first name Zebulon  Last name Quixote  SSN 987-65-4320\n"
            "Home address 1600 Synthetic Lantern Row, Springfield",
        )
        payslip = _document(
            db,
            world,
            DocumentKind.PAYSLIP,
            "payslip_quixote.pdf",
            "Earnings statement  Employee Zebulon Quixote  Employee ID EMP4455667\n"
            "SSN 987654320  1600 Synthetic Lantern Row  Net pay 3000.00",
        )
        statement = _document(
            db,
            world,
            DocumentKind.STATEMENT,
            "statement_quixote.pdf",
            "Zebulon Quixote  1600 Synthetic Lantern Row  Account 000123456789\n"
            "Opening balance 5000.00",
        )
        make_form_data(
            db,
            f1040,
            form_type="W-2",
            tax_year=year,
            issuer_name="Quixote Holdings for Zebulon Quixote",
        )
        account_id = world.checking_id
        with household_context(db, world.household_id):
            checking = db.get(Account, account_id)
            assert checking is not None
        txn = make_transaction(
            db,
            checking,
            amount="3000.00",
            posted_date=RECENT,
            raw_payee="PAYROLL DEPOSIT",
            source_document_id=payslip.id,
        )
        with household_context(db, world.household_id):
            for document, target_type, target_id in (
                (payslip, LinkTarget.TRANSACTION, txn.id),
                (statement, LinkTarget.ACCOUNT, account_id),
            ):
                db.add(
                    DocumentLink(
                        household_id=world.household_id,
                        document_id=document.id,
                        target_type=target_type,
                        target_id=target_id,
                    )
                )
            db.flush()
        return [f1040.id, payslip.id, statement.id]

    return ledger.run(plant)


@pytest.fixture
def world(factories: Factories, ledger: LedgerFactories) -> World:
    return build_world(factories, ledger)


@pytest.fixture
def app_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


# ---------------------------------------------------------------------------
# Structural: nothing about documents is classified as exposable
# ---------------------------------------------------------------------------


def test_every_document_column_and_reference_is_denied() -> None:
    checked = 0
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if table.name in DOCUMENT_TABLES or column.name in DOCUMENT_COLUMNS:
                cls = classify(table.name, column.name)
                assert cls is not None, (table.name, column.name)
                assert cls.exposure is Exposure.DENIED, (table.name, column.name)
                assert cls.reason, (table.name, column.name)
                checked += 1
    # The document tables and the references to them really were found.
    assert checked > 50


def test_no_ai_route_serves_a_document(settings: Settings) -> None:
    keys = route_keys(create_app(settings))
    ai_routes = sorted(k for k in keys if k.split(" ", 1)[1].startswith(API + "/"))
    assert len(ai_routes) == 19
    assert all(k.startswith("GET ") for k in ai_routes)
    banned = re.compile(r"document|preview|region|review|search|download|blob|upload|export")
    assert [k for k in ai_routes if banned.search(k)] == []


# ---------------------------------------------------------------------------
# Behavioural: planted document text reaches no AI answer and no audit row
# ---------------------------------------------------------------------------


def test_document_text_never_reaches_an_ai_client(
    app_client: TestClient, factories: Factories, ledger: LedgerFactories, world: World
) -> None:
    document_ids = plant_documents(ledger, world)
    _, raw = ai_client(factories, world.owner, custom_denylist=["Oakwood Clinic"])
    headers = ai_headers(raw)
    bodies: list[tuple[str, str]] = []
    for path, params in every_call(world):
        response = app_client.get(API + path, params=params, headers=headers)
        assert response.status_code == 200, (path, response.text)
        bodies.append((path, response.text))
    found = {path: document_leaks(body) for path, body in bodies if document_leaks(body)}
    assert found == {}
    # Neither the documents' ids nor their identity (beyond the usual planted set).
    joined = "\n".join(body for _, body in bodies)
    for document_id in document_ids:
        assert str(document_id) not in joined
    assert leaks(joined, world.usernames) == []
    # The planted W-2 really is in the tax summary: its amounts, not its issuer.
    summary = json.loads(dict(bodies)[f"/tax/{TODAY.year - 1}/summary"])
    assert [f["form_type"] for f in summary["forms"]] == ["W-2"]
    assert summary["forms"][0]["boxes"]

    # The audit holds exactly what was sent, so it holds no document text either.
    rows = calls_of(factories, world)
    assert len(rows) == len(bodies)
    for row in rows:
        stored = json.dumps(row.response) + json.dumps(row.params)
        assert document_leaks(stored) == [], row.tool
        assert all(str(d) not in stored for d in document_ids)


def test_ai_token_is_refused_on_every_document_route(
    app_client: TestClient, factories: Factories, ledger: LedgerFactories, world: World
) -> None:
    document_id = plant_documents(ledger, world)[0]
    _, raw = ai_client(factories, world.owner)
    headers = ai_headers(raw)
    assert isinstance(app_client.app, FastAPI)
    probed: list[str] = []
    for key in sorted(route_keys(app_client.app)):
        method, template = key.split(" ", 1)
        if not re.search(r"/(documents|review|imports)(/|$)", template):
            continue
        path = re.sub(r"\{[a-z_]+\}", str(document_id), template)
        response = app_client.request(method, path, headers=headers)
        body: Any = response.json()
        assert response.status_code == 403, (method, path, response.text)
        assert str(body["type"]).endswith("ai-token-confined"), (method, path)
        assert document_leaks(response.text) == []
        probed.append(key)
    assert len(probed) >= 10

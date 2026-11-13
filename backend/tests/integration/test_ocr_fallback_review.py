# SPDX-License-Identifier: AGPL-3.0-or-later
"""A document read without OCR (after two engine crashes) waits for a person.

The pipeline raises ``unparsed/ocr_failed`` for it. Nothing but the person
(or reading the document again through the pipeline) answers that item: not
a later import re-run, and not an outcome that reads the text layer cleanly.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator
from dataclasses import replace

import pytest

from app.auth.principal import apply_rls_context
from app.domain.documents.models import DocumentKind, DocumentStatus, ReviewItemState
from app.domain.imports.schemas import APPLY_MAPPING_JOB_TYPE, ApplyMapping
from app.domain.imports.service import request_apply
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult
from app.ingestion.sandbox.runner import SandboxConfig, SandboxError, run_extraction
from app.workers.handlers import ingest
from tests.fixtures import synth_loans
from tests.fixtures import synth_pdf_statements as synth
from tests.fixtures.imports import run_jobs
from tests.integration.test_loan_statement_pipeline import Env as LoanEnv
from tests.integration.test_loan_statement_pipeline import env as loan_env  # noqa: F401
from tests.integration.test_pdf_statement_pipeline import Env, env  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
def ocr_crashes() -> Iterator[None]:
    """Every extraction with OCR on dies; the no-OCR fallback reads the text layer.

    Asked for after the environment fixture, which sets the pipeline's
    settings first and clears the real function's cache last.
    """
    deps = ingest.default_deps()

    def extract(
        kind: str,
        data: bytes | Iterable[bytes],
        *,
        config: SandboxConfig | None = None,
        options: ExtractOptions | None = None,
    ) -> ExtractResult:
        options = options or ExtractOptions()
        if options.ocr:
            raise SandboxError("crashed", "child killed by signal 11")
        return run_extraction(kind, data, config=config, options=options)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ingest, "default_deps", lambda: replace(deps, extract=extract))
        yield


def _items(env: Env, document_id: uuid.UUID) -> list[tuple[str, object, ReviewItemState]]:  # noqa: F811
    return [(i.kind.value, i.payload.get("reason"), i.state) for i in env.reviews(document_id)]


def test_choosing_the_account_does_not_answer_ocr_failed(
    env: Env,  # noqa: F811
    ocr_crashes: None,
) -> None:
    stmt = synth.mock_savings_statement(seed=143)
    document_id = env.ingest(synth.mock_savings_pdf(stmt))
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert sorted(_items(env, document_id)) == [
        ("unknown_account", "account_not_found", ReviewItemState.OPEN),
        ("unparsed", "ocr_failed", ReviewItemState.OPEN),
    ]

    account = env.ledger.account(env.household_id, currency="GBP", name="Savings")
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        request_apply(
            db, env.principal(), document_id, ApplyMapping(account_id=account.id), request=None
        )
    assert run_jobs(env.factories, env.household_id, APPLY_MAPPING_JOB_TYPE, done=env.done)

    # The rows of the text pages are imported and the account item answered;
    # the person never answered the OCR item, so the document still waits.
    assert env.transactions(account)
    assert sorted(_items(env, document_id)) == [
        ("unknown_account", "account_not_found", ReviewItemState.RESOLVED),
        ("unparsed", "ocr_failed", ReviewItemState.OPEN),
    ]
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW


def test_payslip_read_without_ocr_waits_in_review(
    env: Env,  # noqa: F811
    ocr_crashes: None,
) -> None:
    document_id = env.ingest(synth.payslip_pdf())
    document = env.document(document_id)
    assert document.kind is DocumentKind.PAYSLIP
    assert _items(env, document_id) == [("unparsed", "ocr_failed", ReviewItemState.OPEN)]
    assert document.status is DocumentStatus.NEEDS_REVIEW


def test_loan_statement_read_without_ocr_waits_in_review(
    loan_env: LoanEnv,  # noqa: F811
    ocr_crashes: None,
) -> None:
    """The loan outcome knows only its own doubts: the pipeline holds the document."""
    _, loan = loan_env.loan()
    (stmt,) = synth_loans.loan_series(1)
    document = loan_env.ingest(synth_loans.loan_statement_pdf(stmt), "mortgage.pdf")
    # The text layer read cleanly (the payment is recorded, nothing else is asked)...
    assert len(loan_env.payments(loan)) == 1
    assert loan_env.reasons(document) == {"ocr_failed"}
    # ...and the document still waits: its scanned pages, if any, were never read.
    assert document.status is DocumentStatus.NEEDS_REVIEW

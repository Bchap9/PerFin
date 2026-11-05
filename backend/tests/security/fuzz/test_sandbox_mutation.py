# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mutation fuzzing of the extraction entry points (hypothesis; SECURITY.md C1, C2).

Seeds are synthetic files of every extractor kind; hypothesis mutates them
(byte flips, inserted runs, deletions, truncation, duplicated slices, splices
with another seed, magic-preserving garbage).

* **In process**, the structured extractors and XLSX (``csv``, ``tsv``,
  ``ofx``, ``qif``, ``xlsx``) must either refuse with a deliberate
  :class:`ExtractFailure` code or return a result that survives the
  parent's validation (``ExtractResult`` JSON round trip, no worker-only
  fields): no other exception.
* **Through the sandbox** (``run_extraction``: the real child entry point,
  hardened), every kind including PDF, images, mail and ZIP must answer with
  a validated result for its own kind or stop on a resource limit; never
  ``bad_output``, never an exception other than :class:`SandboxError`.

Each test has a wall-clock budget (``PERFIN_FUZZ_SECONDS``, default 20 s;
examples after it pass trivially) and an example cap
(``PERFIN_FUZZ_EXAMPLES``). The hypothesis database is off so nothing is
written outside ``tmp_path``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from functools import cache
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.ingestion.extract.common import ExtractFailure
from app.ingestion.extract.registry import builtin_extractors
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult
from app.ingestion.sandbox.runner import SandboxConfig, SandboxError, run_extraction
from tests.fixtures import synth_documents as synth
from tests.fixtures.statements import csv_bytes, ofx_sgml, ofx_xml, qif_bytes, synth_transactions
from tests.fixtures.synth_xlsx import workbook
from tests.security.fuzz.corpus import LIMIT_CODES

pytestmark = pytest.mark.security

BUDGET: Final = float(os.environ.get("PERFIN_FUZZ_SECONDS", "20"))
EXAMPLES: Final = int(os.environ.get("PERFIN_FUZZ_EXAMPLES", "300"))
SANDBOX_EXAMPLES: Final = int(os.environ.get("PERFIN_FUZZ_SANDBOX_EXAMPLES", "25"))
#: Small limits so a mutated bomb is cut short quickly.
OPTIONS: Final = ExtractOptions(
    max_rows=2000,
    max_pages=3,
    max_ocr_pages=1,
    ocr_page_seconds=3,
    ocr_total_seconds=5,
    previews=False,
    max_children=20,
)
SANDBOX: Final = SandboxConfig(timeout_seconds=30, cpu_seconds=20, memory_bytes=768 * 1024 * 1024)


@cache
def seeds() -> dict[str, bytes]:
    txns = synth_transactions(6, seed=11)
    return {
        "csv": csv_bytes(txns),
        "tsv": csv_bytes(txns, delimiter="\t"),
        "ofx": ofx_sgml(txns),
        "qfx": ofx_xml(txns),
        "qif": qif_bytes(txns),
        "xlsx": workbook([["Date", "Payee", "Amount"], ["2026-01-02", "Shop", -1.5], [1, 2, 3]]),
        "pdf": synth.statement_pdf(),
        "png": synth.image_bytes(synth.text_image(["TOTAL 12.00"]), "PNG"),
        "jpeg": synth.image_bytes(synth.text_image(["TOTAL 12.00"]), "JPEG"),
        "eml": synth.receipt_email(attachments=[("a.csv", "text/csv", b"Date,Amount\n1,2\n")]),
        "zip": synth.zip_bytes([("a.csv", b"Date,Amount\n2026-01-01,1.00\n"), ("b.txt", b"x")]),
    }


Mutation = Callable[[bytes], bytes]


@st.composite
def mutation(draw: st.DrawFn) -> Mutation:
    op = draw(st.sampled_from(["flip", "insert", "delete", "truncate", "duplicate", "splice"]))
    pos = draw(st.floats(min_value=0, max_value=1))
    span = draw(st.integers(min_value=1, max_value=4096))
    blob = draw(st.binary(min_size=1, max_size=64))
    other = draw(st.sampled_from(sorted(seeds())))

    def apply(data: bytes) -> bytes:
        at = int(pos * len(data))
        if op == "flip":
            return data[:at] + bytes([data[at % len(data)] ^ 0xFF]) + data[at + 1 :]
        if op == "insert":
            return data[:at] + blob * (span // len(blob) + 1) + data[at:]
        if op == "delete":
            return data[:at] + data[at + span :]
        if op == "truncate":
            return data[:at]
        if op == "duplicate":
            return data[:at] + data[at : at + span] * 8 + data[at:]
        donor = seeds()[other]
        return data[:at] + donor[:span] + data[at + span :]

    return apply


@st.composite
def mutated(draw: st.DrawFn, kinds: tuple[str, ...]) -> tuple[str, bytes]:
    kind = draw(st.sampled_from(kinds))
    data = seeds()[kind]
    for change in draw(st.lists(mutation(), min_size=1, max_size=4)):
        data = change(data) or data[:1]
    return kind, data


class Budget:
    def __init__(self) -> None:
        self.deadline = time.monotonic() + BUDGET
        self.ran = 0

    def spent(self) -> bool:
        return time.monotonic() > self.deadline


def _validated(result: ExtractResult, kind: str) -> None:
    again = ExtractResult.model_validate_json(result.model_dump_json())
    assert again.kind == kind
    assert not again.worker_only_fields()
    if not again.ok:
        assert again.error is not None


IN_PROCESS: Final = ("csv", "tsv", "ofx", "qfx", "qif", "xlsx")
_EXTRACTOR_KIND: Final = {"qfx": "qfx"}


def test_mutated_inputs_in_process() -> None:
    seeds()  # built outside hypothesis (the generators use seeded ``random``)
    extractors = builtin_extractors()
    budget = Budget()

    @settings(
        max_examples=EXAMPLES,
        deadline=None,
        database=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(mutated(IN_PROCESS))
    def run(case: tuple[str, bytes]) -> None:
        if budget.spent():
            return
        budget.ran += 1
        kind, data = case
        try:
            result = extractors[kind](data, OPTIONS, kind)
        except ExtractFailure as failure:
            failure_code: str | None = failure.code
        else:
            failure_code = None
        if failure_code is not None:
            assert failure_code, "an extractor failure always carries a code"
            return
        _validated(result, kind)

    run()
    assert budget.ran > 0


SANDBOX_KINDS: Final = ("csv", "ofx", "qif", "xlsx", "pdf", "png", "jpeg", "eml", "zip")


def test_mutated_inputs_through_the_sandbox() -> None:
    seeds()
    budget = Budget()

    @settings(
        max_examples=SANDBOX_EXAMPLES,
        deadline=None,
        database=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(mutated(SANDBOX_KINDS))
    def run(case: tuple[str, bytes]) -> None:
        if budget.spent():
            return
        budget.ran += 1
        kind, data = case
        try:
            result = run_extraction(kind, data, config=SANDBOX, options=OPTIONS)
        except SandboxError as exc:
            contained: str | None = exc.code
        else:
            contained = None
        if contained is not None:
            # Contained: a resource limit, or a native crash of a parser
            # inside the child. Never output the parent had to refuse.
            assert contained in LIMIT_CODES | {"crashed"}, contained
            return
        _validated(result, kind)

    run()
    assert budget.ran > 0

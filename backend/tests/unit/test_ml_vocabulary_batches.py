# SPDX-License-Identifier: AGPL-3.0-or-later
"""A long statement is put to the local model in several calls (ADR-0010).

One call carries ``PERFIN_ML_MAX_CHARS`` of text; each phrase the parser
could not type is asked about together with the page that prints it.
"""

from __future__ import annotations

from app.ingestion.parsers.vocabulary import phrase_key
from app.ingestion.pipeline import ML_VOCABULARY_CALLS, _vocabulary_batches
from app.ingestion.sandbox.protocol import ExtractResult, Page


def _result(*texts: str) -> ExtractResult:
    return ExtractResult(
        kind="pdf",
        ok=True,
        pages=[
            Page(number=i + 1, width=612, height=792, unit="pt", text_source="text_layer", text=t)
            for i, t in enumerate(texts)
        ],
    )


FILLER = ("Nothing to see on this line. " * 20).strip()  # ~580 characters
PLAN = phrase_key("Periodic Investment Plan")
SWEEP = phrase_key("Core Sweep Journal")
HEADING = phrase_key("Other Movements 2026")


def test_a_statement_that_fits_is_sent_whole_in_one_call() -> None:
    pages = _result(
        FILLER, f"10/05 Periodic Investment Plan 25.00\n{FILLER}", "Other Movements 2026"
    )
    ((actions, headings, text, _),) = _vocabulary_batches(pages, [PLAN], [HEADING], 16_000)
    assert (actions, headings) == ([PLAN], [HEADING])
    # The pages that print a phrase first, then the rest: all of it.
    assert text.index("Periodic Investment Plan") < text.index("Nothing to see")
    assert text.count("Nothing to see") == 40
    assert "Other Movements 2026" in text


def test_each_phrase_is_asked_with_the_page_that_prints_it() -> None:
    first = f"10/05 Periodic Investment Plan 25.00\n{FILLER}"
    second = f"{FILLER}\n10/09 Core Sweep Journal 4.00"
    third = f"Other Movements 2026\n{FILLER}"
    pages = _result(first, FILLER, second, third)
    batches = _vocabulary_batches(pages, [PLAN, SWEEP], [HEADING], 700)
    assert [(a, h) for a, h, _, _ in batches] == [([PLAN], []), ([SWEEP], []), ([], [HEADING])]
    assert [text for _, _, text, _ in batches] == [first, second, third]
    # Pages that print none of the phrases are not sent when it takes several calls.
    assert all(text != FILLER for _, _, text, _ in batches)
    # Two pages that fit together share a call.
    together = _vocabulary_batches(pages, [PLAN, SWEEP], [HEADING], 1300)
    assert [(a, h) for a, h, _, _ in together] == [([PLAN, SWEEP], []), ([], [HEADING])]
    assert together[0][2] == f"{first}\n\n{second}"


def test_a_phrase_no_page_prints_is_asked_in_the_first_call() -> None:
    pages = _result(f"10/05 Periodic Investment Plan 25.00\n{FILLER}", FILLER)
    ((actions, headings, _, _),) = _vocabulary_batches(pages, [PLAN, SWEEP], [HEADING], 16_000)
    assert (actions, headings) == ([PLAN, SWEEP], [HEADING])
    # Nothing printed at all: still one call, with the document's text.
    ((actions, headings, text, _),) = _vocabulary_batches(_result(), [PLAN], [], 16_000)
    assert (actions, headings, text) == ([PLAN], [], "")


def test_the_calls_are_bounded() -> None:
    keys = [phrase_key(f"Unusual Movement Kind {chr(65 + i)}") for i in range(10)]
    pages = _result(*(f"Unusual Movement Kind {chr(65 + i)}\n{FILLER}" for i in range(10)))
    batches = _vocabulary_batches(pages, keys, [], 700)
    assert len(batches) == ML_VOCABULARY_CALLS
    # Every phrase is still asked about, the last ones together.
    assert [key for actions, _, _, _ in batches for key in actions] == keys
    assert len(batches[-1][0]) == 10 - (ML_VOCABULARY_CALLS - 1)


def test_a_column_heading_is_asked_with_the_page_of_its_table() -> None:
    first = f"10/05 Periodic Investment Plan 25.00\n{FILLER}"
    second = f"Posted Details Money In Out\n10/09 Example Deposit 4.00\n{FILLER}"
    batches = _vocabulary_batches(
        _result(first, second), [PLAN], [], 700, ["money in out", "posted"]
    )
    assert [(a, c) for a, _, _, c in batches] == [([PLAN], []), ([], ["money in out", "posted"])]
    assert batches[1][2] == second

# SPDX-License-Identifier: AGPL-3.0-or-later
"""``perfin ingest --dry-run FILE`` without a database (``app.ingestion.dryrun``)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from app.cli import EXIT_UNAVAILABLE, main
from app.ingestion.dryrun import DryRun
from app.integrations.antivirus.clamav import ClamdAddress, ClamdScanner
from tests.fixtures import synth_documents as synth
from tests.fixtures.fake_clamd import MARKER, SIGNATURE, fake_clamd
from tests.fixtures.statements import csv_bytes, ofx_sgml, synth_transactions
from tests.fixtures.synth_xlsx import workbook


def _stages(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _run(path: Path, **kwargs: Any) -> tuple[int, list[dict[str, Any]]]:
    out = io.StringIO()
    code = DryRun(path=path, out=out, **kwargs).run()
    return code, _stages(out.getvalue())


def test_pdf_runs_every_stage_through_the_sandbox(tmp_path: Path) -> None:
    code, stages = _run(_write(tmp_path, "s.pdf", synth.statement_pdf(pages=2)))
    assert code == 0
    assert [s["stage"] for s in stages] == [
        "receive",
        "scan",
        "extract",
        "classify",
        "parse",
        "match",
    ]
    receive, scan, extract, classify, parse, match = stages
    assert receive["mime"] == "application/pdf"
    assert len(receive["sha256"]) == 64
    assert scan == {"stage": "scan", "skipped": "antivirus off"}
    assert extract["ok"] is True
    assert [p["number"] for p in extract["pages"]] == [1, 2]
    # Previews are summarised, never printed as image bytes.
    assert all(set(p) == {"page", "width", "height", "png_bytes"} for p in extract["previews"])
    assert classify["kind"] == "statement"
    assert classify["period"]["start"] == "2026-01-01"
    assert classify["last4"]["value"] == "4321"
    assert "parse" in parse
    assert match["skipped"]


def test_csv_shows_the_suggested_mapping_and_rows(tmp_path: Path) -> None:
    txns = synth_transactions(4, seed=3)
    code, stages = _run(_write(tmp_path, "s.csv", csv_bytes(txns)))
    assert code == 0
    parse = next(s for s in stages if s["stage"] == "parse")
    (table,) = parse["tables"]
    assert table["suggested_mapping"]["date_column"] == 0
    assert table["transactions"] == 4
    assert [t["amount"] for t in table["first_transactions"]] == [str(t.amount) for t in txns]
    classify = next(s for s in stages if s["stage"] == "classify")
    assert classify["pipeline_kind"] == "statement"


def test_xlsx_and_ofx(tmp_path: Path) -> None:
    code, stages = _run(_write(tmp_path, "w.xlsx", workbook([["Date", "Amount"], ["x", 1]])))
    assert code == 0
    assert next(s for s in stages if s["stage"] == "extract")["kind"] == "xlsx"
    txns = synth_transactions(3, seed=5)
    code, stages = _run(_write(tmp_path, "s.ofx", ofx_sgml(txns, acct="0000009876")))
    assert code == 0
    parse = next(s for s in stages if s["stage"] == "parse")
    assert parse["statements"][0]["last4"] == "9876"
    assert parse["statements"][0]["transactions"] == 3


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        pytest.param(b"<html><script>x</script></html>", "markup", id="html"),
        pytest.param(b"<svg onload=alert(1)></svg>", "markup", id="svg"),
        pytest.param(
            b"Date,Amount\n" + b"2026-01-01,1.00\n" * 1000 + b"<script>x</script>\n",
            "polyglot",
            id="csv-polyglot-past-window",
        ),
        pytest.param(b"\x00\x01\x02binary", "binary_unknown", id="binary"),
    ],
)
def test_refused_like_an_upload(tmp_path: Path, data: bytes, reason: str) -> None:
    code, stages = _run(_write(tmp_path, "x", data))
    assert code == 1
    assert stages == [
        {
            "stage": "receive",
            "ok": False,
            "reason": reason,
            "size": len(data),
            "sha256": stages[0]["sha256"],
        }
    ]


def test_too_large_is_not_read_further(tmp_path: Path) -> None:
    from app.ingestion import dryrun

    path = _write(tmp_path, "big.csv", b"Date,Amount\n" + b"2026-01-01,1\n" * 10)
    out = io.StringIO()
    run = DryRun(path=path, out=out)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dryrun, "DEFAULT_MAX_BYTES", 20)
        assert run.run() == 1
    assert _stages(out.getvalue())[0]["reason"] == "too_large"


def test_extraction_failure_is_reported(tmp_path: Path) -> None:
    code, stages = _run(_write(tmp_path, "e.pdf", synth.encrypted_pdf()))
    assert code == 1
    assert stages[-1]["stage"] == "extract"
    assert stages[-1]["ok"] is False
    assert stages[-1]["error"]["code"] == "encrypted"


def test_scan_stage_with_a_fake_clamd(tmp_path: Path) -> None:
    infected = _write(tmp_path, "i.csv", b"Date,Payee,Amount\n2026-01-01," + MARKER + b",1\n")
    with fake_clamd("signature") as clamd:
        scanner = ClamdScanner(address=ClamdAddress.parse(clamd.address), timeout_seconds=5)
        code, stages = _run(infected, scanner=scanner)
    assert code == 1
    assert stages[-1] == {
        "stage": "scan",
        "antivirus": "clamav",
        "status": "infected",
        "signature": SIGNATURE,
        "error": None,
    }


def test_cli_entry_point(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write(tmp_path, "s.csv", csv_bytes(synth_transactions(2)))
    assert main(["ingest", "--dry-run", str(path)]) == 0
    stages = _stages(capsys.readouterr().out)
    assert [s["stage"] for s in stages][:3] == ["receive", "scan", "extract"]
    assert main(["ingest", "--dry-run", "--pretty", str(path)]) == 0
    assert capsys.readouterr().out.startswith("{\n")
    # Only dry runs; bad arguments are refused.
    assert main(["ingest", str(path)]) == EXIT_UNAVAILABLE
    assert main(["ingest", "--dry-run"]) == EXIT_UNAVAILABLE
    assert main(["ingest", "--dry-run", str(tmp_path / "missing")]) == 1
    assert main(["ingest", "--dry-run", "--household", "nope", str(path)]) == 1
    # Nothing was written next to the file.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s.csv"]

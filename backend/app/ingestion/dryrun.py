# SPDX-License-Identifier: AGPL-3.0-or-later
"""``perfin ingest --dry-run FILE``: every pipeline stage on a local file, nothing written.

For parser contributors and debugging (INGESTION.md "Parser contribution
model"). The stages mirror :mod:`app.ingestion.pipeline`, each printing one
JSON document:

1. ``receive``: size, SHA-256 and the magic-byte sniff (with the whole-file
   markup scan for text types, C4) - the file is refused exactly as an upload
   would be; above ``PERFIN_UPLOAD_MAX_MB`` it is not read further.
2. ``scan``: the ClamAV sidecar when ``PERFIN_ANTIVIRUS=clamav`` (sending the
   file to clamd is not a write), else skipped.
3. ``extract``: the real sandbox (``run_extraction``), so a dry run is as
   isolated as the worker. Preview PNGs and child documents are summarised
   (page, size; name, size, sniffed type), not printed.
4. ``classify``: kind, institution fingerprint, last four digits, period,
   per-field confidence (shipped fingerprints; the household's own too with
   ``--household``).
5. ``parse``: document parsers on PDF/image pages (bank statements as
   read, and the investment statements per account: last four digits,
   period, positions and activity counts by type, reconciliation with the
   failed checks, each section's rows against its printed total, the rows
   a failed check leaves in doubt and the statement's own summary
   figures); for tables the suggested
   column mapping and the rows it reads, and for a brokerage activity export
   (``app.ingestion.parsers.brokerage_table``) the investment statements the
   importer would commit instead (per account: last four digits, rows by
   type), and for a brokerage positions export
   (``app.ingestion.parsers.positions_table``) the positions snapshot per
   account (the day it is valued, with today standing in for the day of
   receipt when the file prints none); structured statements as extracted.
6. ``match``: only with ``--household`` and a reachable database: the
   account the last four digits would match, read in a ``READ ONLY``
   transaction that is rolled back.

Nothing touches the blob store, and no database row is written: the only
database access is the optional read-only transaction. Without a usable
configuration (no master key, no database) the extract, classify and parse
stages still run with the defaults.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TextIO

from app.ingestion.intake.sniff import (
    SNIFF_BYTES,
    TEXT_TYPES,
    FileType,
    MarkupScanner,
    Rejected,
    RejectReason,
    sniff,
)
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, InvestmentStatement
from app.ingestion.sandbox.runner import SandboxConfig, SandboxError, run_extraction

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.core.settings import Settings
    from app.ingestion.classify.service import Classification
    from app.integrations.antivirus import Scanner

DEFAULT_MAX_BYTES: Final = 50 * 1024 * 1024
_CHUNK: Final = 64 * 1024
#: Table rows shown in the ``parse`` stage (the rest are counted).
SHOWN_ROWS: Final = 20


def _json_default(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal | uuid.UUID):
        return str(value)
    if isinstance(value, frozenset | set | tuple):
        return list(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


@dataclass
class DryRun:
    """One dry run; :attr:`stages` collects what was printed."""

    path: Path
    out: TextIO
    settings: Settings | None = None
    household_id: uuid.UUID | None = None
    pretty: bool = False
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    options: ExtractOptions = field(default_factory=ExtractOptions)
    scanner: Scanner | None = None
    session_factory: Callable[[], Any] | None = None
    stages: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, stage: str, **facts: Any) -> dict[str, Any]:
        record = {"stage": stage, **facts}
        self.stages.append(record)
        text = json.dumps(
            record, default=_json_default, indent=2 if self.pretty else None, ensure_ascii=False
        )
        self.out.write(text + "\n")
        self.out.flush()
        return record

    @property
    def max_bytes(self) -> int:
        return self.settings.upload_max_bytes if self.settings is not None else DEFAULT_MAX_BYTES

    # -- stages -----------------------------------------------------------------

    def receive(self) -> tuple[bytes, FileType] | None:
        digest = hashlib.sha256()
        data = bytearray()
        with self.path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                data += chunk
                digest.update(chunk)
                if len(data) > self.max_bytes:
                    self.emit("receive", ok=False, reason="too_large", max_bytes=self.max_bytes)
                    return None
        verdict = sniff(bytes(data[:SNIFF_BYTES]))
        facts: dict[str, Any] = {"size": len(data), "sha256": digest.hexdigest()}
        if isinstance(verdict, Rejected):
            self.emit("receive", ok=False, reason=str(verdict.reason), **facts)
            return None
        if verdict in TEXT_TYPES and MarkupScanner().feed(bytes(data)):
            self.emit("receive", ok=False, reason=str(RejectReason.POLYGLOT), **facts)
            return None
        self.emit("receive", ok=True, mime=str(verdict), extract_kind=verdict.extract_kind, **facts)
        return bytes(data), verdict

    def scan(self, data: bytes) -> bool:
        if self.scanner is None:
            self.emit("scan", skipped="antivirus off")
            return True
        result = self.scanner.scan((data,), size=len(data))
        self.emit(
            "scan",
            antivirus=self.scanner.name,
            status=result.status,
            signature=result.signature,
            error=result.error,
        )
        return not result.infected

    def extract(self, kind: str) -> ExtractResult | None:
        try:
            result = run_extraction(
                kind, self.path.read_bytes(), config=self.sandbox, options=self.options
            )
        except SandboxError as exc:
            self.emit("extract", ok=False, sandbox_error=exc.code, detail=exc.detail)
            return None
        shown = result.model_dump(mode="json", exclude={"previews", "children"})
        shown["previews"] = [
            {"page": p.page, "width": p.width, "height": p.height, "png_bytes": len(p.png)}
            for p in result.previews
        ]
        shown["children"] = [
            {
                "origin": c.origin,
                "name": c.name,
                "size": c.size,
                "depth": c.depth,
                "sniffed": _sniffed(c.data),
            }
            for c in result.children
        ]
        self.emit("extract", **shown)
        return result if result.ok else None

    def classify(self, result: ExtractResult, db: Session | None) -> Classification:
        from app.ingestion.classify.service import all_fingerprints, classify_extraction

        fingerprints = (
            all_fingerprints(db, self.household_id)
            if db is not None and self.household_id is not None
            else None
        )
        found = classify_extraction(result, fingerprints=fingerprints)
        facts = found.facts()
        facts["period"] = {
            **facts.get("period", {}),
            "start": found.period.start,
            "end": found.period.end,
        }
        facts["last4"] = {
            "value": found.last4.value,
            "source": found.last4.source,
            "confidence": found.last4.confidence,
        }
        facts["tax_year"] = found.tax_year.value
        # The pipeline files structured records (tables, OFX/QIF statements)
        # as statements without consulting the classifier.
        facts["pipeline_kind"] = "statement" if result.tables or result.statements else found.kind
        facts["household_fingerprints"] = fingerprints is not None
        self.emit("classify", **facts)
        return found

    def parse(self, kind: str, result: ExtractResult, found: Classification) -> ExtractResult:
        from app.core import clock
        from app.ingestion.parsers.brokerage_table import TABLE_KINDS, read_activity_table
        from app.ingestion.parsers.positions_table import read_positions_table
        from app.ingestion.parsers.registry import DOCUMENT_KINDS, parse_extraction
        from app.ingestion.parsers.structured import from_table, suggest_mapping

        if kind in DOCUMENT_KINDS:
            parsed = parse_extraction(
                result,
                parser_name=found.parser,
                institution=found.fingerprint.key if found.fingerprint else None,
                kind=found.kind,
                date_order=found.fingerprint.spec.date_order if found.fingerprint else None,
            )
            self.emit(
                "parse",
                parse=parsed.parse.model_dump(mode="json") if parsed.parse else None,
                statements=[s.model_dump(mode="json") for s in parsed.statements],
                investments=[_investment_summary(i) for i in parsed.investments],
            )
            return parsed
        if result.tables:
            tables = []
            for table in result.tables:
                suggestion = suggest_mapping(table)
                spec = suggestion.spec if suggestion is not None else None
                entry: dict[str, Any] = {
                    "rows": len(table.rows),
                    "suggested_mapping": spec.model_dump(mode="json") if spec else None,
                    "suggestion": suggestion.summary() if suggestion is not None else None,
                }
                if spec is not None:
                    statement = from_table(table, spec)
                    entry["transactions"] = len(statement.transactions)
                    entry["skipped_rows"] = statement.skipped_rows[:SHOWN_ROWS]
                    entry["first_transactions"] = [
                        {"posted": t.posted_date, "amount": t.amount, "payee": t.raw_payee}
                        for t in statement.transactions[:SHOWN_ROWS]
                    ]
                tables.append(entry)
            activity = read_activity_table(result.tables[0]) if kind in TABLE_KINDS else None
            if activity is not None:
                tables[0]["brokerage_activity"] = {
                    **activity.summary(),
                    "accounts": [
                        {
                            "last4": s.account.last4,
                            "currency": s.account.currency,
                            "period": [s.period_start, s.period_end],
                            "types": dict(Counter(a.type for a in s.activity)),
                        }
                        for s in activity.statements
                    ],
                }
            positions = (
                read_positions_table(result.tables[0], received_on=clock.utcnow().date())
                if kind in TABLE_KINDS
                else None
            )
            if positions is not None:
                tables[0]["brokerage_positions"] = {
                    **positions.summary(),
                    "accounts": [_investment_summary(s) for s in positions.statements],
                }
            self.emit("parse", tables=tables)
            return result
        self.emit(
            "parse",
            statements=[
                {
                    "source_format": s.source_format,
                    "last4": s.account.last4,
                    "name": s.account.name,
                    "account_type": s.account.account_type,
                    "subtype": s.account.subtype,
                    "pages": [s.first_page, s.last_page],
                    "period": [s.period_start, s.period_end],
                    "transactions": len(s.transactions),
                }
                for s in result.statements
            ],
        )
        return result

    def match(self, result: ExtractResult, found: Classification, db: Session | None) -> None:
        if db is None or self.household_id is None:
            self.emit("match", skipped="no --household or no database")
            return
        from app.ingestion.match.accounts import match_account

        last4s = {s.account.last4 for s in result.statements if s.account.last4}
        if found.last4.value:
            last4s.add(found.last4.value)
        if len(last4s) != 1:
            self.emit("match", account_id=None, reason="no single last-four", candidates=[])
            return
        institution = found.institution or next(
            (s.account.institution for s in result.statements if s.account.institution), None
        )
        account = match_account(
            db, self.household_id, last4=next(iter(last4s)), institution=institution
        )
        self.emit("match", account_id=account.account_id, candidates=list(account.candidates))

    # -- driver -----------------------------------------------------------------

    @contextmanager
    def _read_only(self) -> Iterator[Session | None]:
        if self.session_factory is None or self.household_id is None:
            yield None
            return
        from sqlalchemy import text

        from app.db.rls import set_context

        with self.session_factory() as db:
            db.begin()
            try:
                db.execute(text("SET TRANSACTION READ ONLY"))
                set_context(db, self.household_id)
                yield db
            finally:
                db.rollback()

    def run(self) -> int:
        received = self.receive()
        if received is None:
            return 1
        data, file_type = received
        if not self.scan(data):
            return 1
        kind = file_type.extract_kind
        if kind is None:
            self.emit("extract", ok=False, reason="format_not_supported")
            return 1
        del data
        result = self.extract(kind)
        if result is None:
            return 1
        with self._read_only() as db:
            found = self.classify(result, db)
            result = self.parse(kind, result, found)
            self.match(result, found, db)
        return 0


def _investment_summary(statement: InvestmentStatement) -> dict[str, Any]:
    """One brokerage statement as the parse stage shows it (counts, no rows)."""
    check = statement.reconciliation
    return {
        "source_format": statement.source_format,
        "last4": statement.account.last4,
        "name": statement.account.name,
        "account_type": statement.account.account_type,
        "subtype": statement.account.subtype,
        "pages": [statement.first_page, statement.last_page],
        "institution": statement.account.institution,
        "currency": statement.account.currency,
        "period": [statement.period_start, statement.period_end],
        "as_of": statement.as_of,
        "positions": len(statement.positions),
        "activity": len(statement.activity),
        "activity_types": dict(Counter(a.type for a in statement.activity)),
        "cash_opening": statement.cash_opening.amount if statement.cash_opening else None,
        "cash_closing": statement.cash_closing.amount if statement.cash_closing else None,
        "positions_value": statement.positions_value.amount if statement.positions_value else None,
        "total_value": statement.total_value.amount if statement.total_value else None,
        "reconciliation": (
            {"status": check.status, "checks": list(check.checks), "failed": list(check.failed)}
            if check is not None
            else None
        ),
        # Each section's rows against its printed total (or the cash-flow
        # summary's line for it), and the rows a failed check leaves in doubt.
        "sections": [
            {
                "label": section.label,
                "status": section.status,
                "source": section.source,
                "rows": len(section.rows),
                "printed": section.printed,
                "computed": section.computed,
                "difference": section.difference,
                "page": section.page,
            }
            for section in (check.sections if check is not None else [])
        ],
        "doubtful": (
            {
                "activity": [
                    {
                        "index": index,
                        "page": statement.activity[index].page,
                        "section": statement.activity[index].section,
                        "type": statement.activity[index].type,
                        "date": statement.activity[index].trade_date,
                        "amount": statement.activity[index].amount,
                    }
                    for index in check.doubtful_activity or []
                ],
                "positions": list(check.doubtful_positions or []),
                "summary_cash_difference": check.summary_cash_difference,
                "unread_summary_lines": list(check.unread_summary_lines),
            }
            if check is not None
            else None
        ),
        # The statement's own summary tables (account value, cash flow, income,
        # realized gains), line by line.
        "figures": [
            {
                "group": figure.group,
                "label": figure.label,
                "role": figure.role,
                "detail": figure.detail,
                "amount": figure.amount,
                "year_to_date": figure.year_to_date,
                "page": figure.page,
            }
            for figure in statement.figures
        ],
        "low_confidence_rows": sum(1 for a in statement.activity if a.low_confidence)
        + sum(1 for p in statement.positions if p.low_confidence),
        "confidence": statement.confidence,
    }


def _sniffed(data: bytes) -> str:
    verdict = sniff(data[:SNIFF_BYTES])
    return f"rejected:{verdict.reason}" if isinstance(verdict, Rejected) else str(verdict)


def from_settings(
    path: Path,
    out: TextIO,
    settings: Settings | None,
    *,
    household_id: uuid.UUID | None = None,
    pretty: bool = False,
) -> DryRun:
    """A :class:`DryRun` configured like the worker (or with defaults)."""
    run = DryRun(path=path, out=out, settings=settings, household_id=household_id, pretty=pretty)
    if settings is None:
        return run
    from app.integrations.antivirus import scanner_from_settings

    run.sandbox = SandboxConfig.from_settings(settings)
    run.scanner = scanner_from_settings(settings)
    if household_id is not None:
        from sqlalchemy import pool

        from app.db.session import make_app_engine, make_sessionmaker

        engine = make_app_engine(settings, poolclass=pool.NullPool)
        run.session_factory = make_sessionmaker(engine)
    return run

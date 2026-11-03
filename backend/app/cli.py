# SPDX-License-Identifier: AGPL-3.0-or-later
"""``perfin`` command-line entry point.

One function per subcommand so each roadmap track edits only its own.
Commands whose phase has not landed print a message and exit with status 2.

The serving roles (``api``, ``worker``, ``mcp``) pass through a startup gate
first: JSON logging is configured and ``app.core.config_check.enforce_config``
refuses to boot on any insecure-configuration error (SECURITY.md H6). The
gate runs in ``main()``, so a handler added later is covered automatically.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
    from alembic.config import Config

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_UNAVAILABLE = 2

BACKEND_DIR = Path(__file__).resolve().parent.parent

Handler = Callable[[argparse.Namespace], int]

#: Commands that serve traffic or data and must refuse insecure configuration.
SERVING_COMMANDS = frozenset({"api", "worker", "mcp"})


def _unavailable(command: str, phase: str) -> Handler:
    def handler(_args: argparse.Namespace) -> int:
        print(f"perfin {command}: not available until {phase}", file=sys.stderr)
        return EXIT_UNAVAILABLE

    handler.perfin_unavailable = True  # type: ignore[attr-defined]
    return handler


def _startup_gate() -> int:
    """Configure logging and refuse to start on configuration errors."""
    from app.core.config_check import InsecureConfigError, enforce_config
    from app.core.logging import configure_logging
    from app.core.settings import get_settings

    try:
        settings = get_settings()
    except ValidationError as exc:
        _print_validation_error(exc)
        return EXIT_FAIL
    configure_logging(settings.log_level)
    try:
        enforce_config(settings)
    except InsecureConfigError as exc:
        print(f"perfin: {exc}", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


def _print_validation_error(exc: ValidationError) -> None:
    for error in exc.errors(include_input=False, include_url=False):
        field = ".".join(str(part) for part in error["loc"])
        print(f"ERROR {field}: {error['msg']}", file=sys.stderr)


def cmd_api(_args: argparse.Namespace) -> int:
    import uvicorn

    from app.core.metrics import start_metrics_server
    from app.core.settings import get_settings

    settings = get_settings()
    # Internal-only Prometheus endpoint on PERFIN_METRICS_PORT (H7).
    start_metrics_server(settings)
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.bind_host,
        port=settings.bind_port,
        # Forwarded headers are interpreted only by
        # app.core.request_context.RequestContextMiddleware, and only from
        # PERFIN_TRUSTED_PROXIES; uvicorn must not rewrite the client first.
        proxy_headers=False,
        server_header=False,
        date_header=False,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=30,
        log_level=settings.log_level.lower(),
        # Logging is already configured (JSON + scrubbing) by the startup
        # gate; log_config=None keeps uvicorn from replacing it.
        log_config=None,
        access_log=True,
    )
    return EXIT_OK


def cmd_worker(args: argparse.Namespace) -> int:
    """Run the job worker (ADR-0002) until SIGTERM/SIGINT.

    ``main()`` has already run the startup gate (configuration check, H6).
    Handlers are discovered from ``app.workers.handlers``.
    """
    from app.core.metrics import start_metrics_server
    from app.core.settings import get_settings
    from app.db.session import make_app_engine
    from app.workers.registry import discover_handlers
    from app.workers.runner import WorkerConfig, run_worker

    try:
        config = WorkerConfig(
            queues=tuple(getattr(args, "queues", None) or ("default",)),
            concurrency=getattr(args, "concurrency", 1),
        )
    except ValueError as exc:
        print(f"perfin worker: {exc}", file=sys.stderr)
        return EXIT_FAIL
    settings = get_settings()
    discover_handlers()
    # Internal-only Prometheus endpoint on PERFIN_METRICS_PORT (H7).
    start_metrics_server(settings)
    # Application role with statement_timeout and lock_timeout (F8).
    engine = make_app_engine(settings, pool_size=config.concurrency + 2)
    try:
        return run_worker(engine, config=config, install_signal_handlers=True)
    finally:
        engine.dispose()


def alembic_config() -> Config:
    """Alembic configuration for the migrations shipped with this build.

    No URL is set here: ``migrations/env.py`` connects with
    ``PERFIN_MIGRATION_DATABASE_URL`` (the owner role), and the revisions
    grant DML to ``PERFIN_DB_APP_ROLE``.
    """
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    # JSON logging (with scrubbing) is configured by the command instead of
    # alembic.ini's plain-text handlers.
    cfg.attributes["configure_logger"] = False
    return cfg


def cmd_migrate(_args: argparse.Namespace) -> int:
    """Apply every pending revision (``alembic upgrade head``) as the owner role."""
    from alembic import command
    from sqlalchemy.exc import SQLAlchemyError

    from app.core.logging import configure_logging, get_logger
    from app.core.settings import get_settings

    try:
        settings = get_settings()
    except ValidationError as exc:
        _print_validation_error(exc)
        return EXIT_FAIL
    configure_logging(settings.log_level)
    log = get_logger("perfin.migrate")
    if settings.migration_database_url is None:
        log.warning(
            "PERFIN_MIGRATION_DATABASE_URL is unset; migrating with PERFIN_DATABASE_URL. "
            "Production runs migrations as the owner role."
        )
    try:
        command.upgrade(alembic_config(), "head")
    except SQLAlchemyError as exc:
        # The driver message can include the host but never the password;
        # still, log only the error class to keep connection details out.
        log.error("migration failed (%s)", type(exc).__name__)
        return EXIT_FAIL
    log.info("database is at the migration head")
    return EXIT_OK


def cmd_audit_verify(_args: argparse.Namespace) -> int:
    """Recompute the audit-log hash chain (SECURITY.md I4) and print the result.

    Connects with ``PERFIN_MIGRATION_DATABASE_URL`` (the owner role), which
    sees every row; the application role would only see its RLS context.
    Prints one JSON object; record ``head_hash`` outside the database (for
    example next to each backup) to detect truncation of the tail.
    Exit 0 when the chain is intact, 1 otherwise.
    """
    from sqlalchemy import pool
    from sqlalchemy.exc import SQLAlchemyError

    from app.core.audit import verify_chain
    from app.core.settings import get_settings
    from app.db.session import make_engine

    try:
        settings = get_settings()
    except ValidationError as exc:
        _print_validation_error(exc)
        return EXIT_FAIL
    engine = make_engine(settings.effective_migration_database_url, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            result = verify_chain(conn)
    except SQLAlchemyError as exc:
        print(f"perfin audit verify: database error ({type(exc).__name__})", file=sys.stderr)
        return EXIT_FAIL
    finally:
        engine.dispose()
    print(
        json.dumps(
            {
                "ok": result.ok,
                "rows_checked": result.rows_checked,
                "head_seq": result.head_seq,
                "head_hash": result.head_hash,
                "first_bad_seq": result.first_bad_seq,
                "reason": result.reason,
            },
            sort_keys=True,
        )
    )
    return EXIT_OK if result.ok else EXIT_FAIL


def cmd_config_check(_args: argparse.Namespace) -> int:
    from app.core.config_check import check_config, has_errors
    from app.core.settings import Settings

    try:
        settings = Settings()
    except ValidationError as exc:
        _print_validation_error(exc)
        return EXIT_FAIL
    findings = check_config(settings)
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    errors = sum(1 for f in findings if f.level == "error")
    warnings = len(findings) - errors
    if has_errors(findings):
        print(
            f"perfin config check: {errors} error(s), {warnings} warning(s); "
            "refusing insecure configuration",
            file=sys.stderr,
        )
        return EXIT_FAIL
    print(f"perfin config check: OK ({warnings} warning(s))")
    return EXIT_OK


def cmd_healthcheck(_args: argparse.Namespace) -> int:
    import httpx

    from app.core.settings import get_settings

    settings = get_settings()
    url = f"http://127.0.0.1:{settings.bind_port}/healthz"
    try:
        response = httpx.get(url, timeout=3.0)
    except httpx.HTTPError:
        return EXIT_FAIL
    return EXIT_OK if response.status_code == 200 else EXIT_FAIL


def cmd_openapi(args: argparse.Namespace) -> int:
    from app.core.settings import Settings
    from app.main import create_app

    schema = create_app(Settings(dev_mode=True)).openapi()
    rendered = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="perfin", description="PerFin personal finance")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("api", help="run the HTTP API").set_defaults(func=cmd_api)
    worker = sub.add_parser("worker", help="run the background worker")
    worker.add_argument(
        "--queue",
        action="append",
        dest="queues",
        metavar="NAME",
        help="queue to consume (repeatable; default: default)",
    )
    worker.add_argument(
        "--concurrency", type=int, default=1, help="jobs run in parallel (default 1)"
    )
    worker.set_defaults(func=cmd_worker)
    sub.add_parser("mcp", help="run the read-only MCP server").set_defaults(
        func=_unavailable("mcp", "Phase 6b")
    )
    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)

    config = sub.add_parser("config", help="configuration tools")
    config_sub = config.add_subparsers(dest="config_command", required=True, metavar="ACTION")
    config_sub.add_parser("check", help="refuse insecure configuration").set_defaults(
        func=cmd_config_check
    )

    audit = sub.add_parser("audit", help="audit log tools")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True, metavar="ACTION")
    audit_sub.add_parser(
        "verify", help="verify the audit-log hash chain (uses the migration URL)"
    ).set_defaults(func=cmd_audit_verify)

    sub.add_parser("healthcheck", help="probe the local API (container health)").set_defaults(
        func=cmd_healthcheck
    )
    openapi = sub.add_parser("openapi", help="print the OpenAPI schema as JSON")
    openapi.add_argument("-o", "--output", help="write to this file instead of stdout")
    openapi.set_defaults(func=cmd_openapi)

    ingest = sub.add_parser("ingest", help="run the ingestion pipeline on a file")
    ingest.add_argument("path", nargs="?")
    ingest.add_argument("--dry-run", action="store_true")
    ingest.set_defaults(func=_unavailable("ingest", "Phase 3"))
    sub.add_parser("bootstrap-url", help="print the one-time setup URL").set_defaults(
        func=_unavailable("bootstrap-url", "Phase 1")
    )
    sub.add_parser("rotate-key", help="rewrap data keys under a new master key").set_defaults(
        func=_unavailable("rotate-key", "Phase 7")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Handler = args.func
    if args.command in SERVING_COMMANDS and not getattr(handler, "perfin_unavailable", False):
        gate = _startup_gate()
        if gate != EXIT_OK:
            return gate
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())

# SPDX-License-Identifier: AGPL-3.0-or-later
"""``perfin`` command-line entry point.

One function per subcommand so each roadmap track edits only its own.
Commands whose phase has not landed print a message and exit with status 2.

The serving roles (``api``, ``worker``, ``mcp``) pass through a startup gate
first: JSON logging is configured and ``app.core.config_check.enforce_config``
refuses to boot on any insecure-configuration error (SECURITY.md H6). The
gate runs in ``main()``, so a handler added later is covered automatically.
The ``mcp`` role has its own gate (``app.mcp.config``): it holds no database
URL, master key or secret key and refuses to start if it is given one (M7);
``perfin config check`` with ``PERFIN_ROLE=mcp`` runs that check.
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
    from sqlalchemy import Engine
    from sqlalchemy.orm import Session

    from app.core.settings import Settings

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


def _mcp_gate() -> int:
    """Startup gate of the ``mcp`` role (AI_ACCESS.md M7).

    The role never loads ``Settings`` (database URL, master key): it is
    checked by ``app.mcp.config``, which refuses an environment that carries
    a database URL, the master key, the secret key or another credential, and
    requires ``PERFIN_MCP_API_URL`` and ``PERFIN_MCP_TOKEN(_FILE)``.
    """
    from app.core.logging import configure_logging, get_logger
    from app.mcp.config import check

    config, findings = check()
    configure_logging(config.log_level if config is not None else "INFO")
    log = get_logger("perfin.config")
    for finding in findings:
        log.log(
            40 if finding.level == "error" else 30,
            finding.message,
            extra={"code": finding.code, "check": "config"},
        )
    if config is None:
        for finding in findings:
            if finding.level == "error":
                print(f"perfin mcp: {finding.render()}", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


def _startup_gate(command: str = "api") -> int:
    """Configure logging and refuse to start on configuration errors."""
    if command == "mcp":
        return _mcp_gate()
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
    if not _database_role_ok(settings, "api"):
        return EXIT_FAIL
    # First boot with no user: publish the one-time setup URL (A10).
    _announce_bootstrap(settings)
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


def _database_role_ok(settings: Settings, command: str, *, engine: Engine | None = None) -> bool:
    """Refuse to serve as a superuser, BYPASSRLS or table-owning role (H3).

    ``PERFIN_DATABASE_URL`` must be the unprivileged app role, or row-level
    security stops being a control (``app.db.role_check``). Fails closed when
    the database cannot be asked; dev mode only warns.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from app.db.role_check import privileged_role_problems
    from app.db.session import make_app_engine

    own = engine is None
    probe = make_app_engine(settings, pool_size=1) if engine is None else engine
    try:
        problems = privileged_role_problems(probe)
    except SQLAlchemyError as exc:
        print(
            f"perfin {command}: could not check the database role of PERFIN_DATABASE_URL "
            f"({type(exc).__name__}); refusing to start",
            file=sys.stderr,
        )
        return False
    finally:
        if own:
            probe.dispose()
    if not problems:
        return True
    message = (
        f"the PERFIN_DATABASE_URL role {', '.join(problems)}; it must be the "
        "unprivileged app role (perfin_app), see deploy/db-init (SECURITY.md H3)"
    )
    if settings.dev_mode:
        print(f"perfin {command}: WARNING {message}", file=sys.stderr)
        return True
    print(f"perfin {command}: ERROR {message}; refusing to start", file=sys.stderr)
    return False


def _announce_bootstrap(settings: Settings) -> None:
    """Print and write the setup URL while no user exists; never blocks startup.

    The URL goes out as a WARNING log line (JSON on stderr). When
    ``PERFIN_LOG_LEVEL`` suppresses warnings it is printed to stderr as plain
    text instead, so the first boot always shows it exactly once.
    """
    import logging

    from sqlalchemy.exc import SQLAlchemyError

    from app.auth.bootstrap import announce_on_startup
    from app.core.logging import get_logger

    logger = get_logger("perfin.bootstrap")
    try:
        url = announce_on_startup(settings)
    except SQLAlchemyError as exc:
        logger.error("could not check whether first-run setup is pending (%s)", type(exc).__name__)
        return
    if url is not None and not logger.isEnabledFor(logging.WARNING):
        print(
            f"perfin: no user exists yet; open this one-time setup URL: {url}",
            file=sys.stderr,
        )


def cmd_bootstrap_url(_args: argparse.Namespace) -> int:
    """Print a fresh one-time setup URL while no user exists (AUTH.md, A10).

    Replaces any earlier setup token and rewrites ``PERFIN_BOOTSTRAP_URL_FILE``.
    Exits 1 once setup has been completed (there is nothing to bootstrap).
    """
    from sqlalchemy import pool
    from sqlalchemy.exc import SQLAlchemyError

    from app.auth.bootstrap import issue_setup_token, remove_url_file, setup_url, write_url_file
    from app.core.settings import get_settings
    from app.db.session import make_app_engine, make_sessionmaker

    try:
        settings = get_settings()
    except ValidationError as exc:
        _print_validation_error(exc)
        return EXIT_FAIL
    engine = make_app_engine(settings, poolclass=pool.NullPool)
    try:
        with make_sessionmaker(engine)() as db, db.begin():
            token = issue_setup_token(db)
    except SQLAlchemyError as exc:
        print(f"perfin bootstrap-url: database error ({type(exc).__name__})", file=sys.stderr)
        return EXIT_FAIL
    finally:
        engine.dispose()
    if token is None:
        remove_url_file(settings.bootstrap_url_file)
        print(
            "perfin bootstrap-url: setup is already complete; log in with an existing account",
            file=sys.stderr,
        )
        return EXIT_FAIL
    url = setup_url(settings, token)
    write_url_file(settings.bootstrap_url_file, url)
    print(url)
    return EXIT_OK


def cmd_notify(_args: argparse.Namespace) -> int:
    """Route ``backup.failed`` to every household's channels (notifications.md)."""
    from app.domain.notifications.backup import run_cli

    return run_cli()


def cmd_worker(args: argparse.Namespace) -> int:
    """Run the job worker (ADR-0002) until SIGTERM/SIGINT.

    ``main()`` has already run the startup gate (configuration check, H6).
    Handlers are discovered from ``app.workers.handlers``.
    """
    from app.core.metrics import start_metrics_server
    from app.core.settings import get_settings
    from app.db.session import make_app_engine
    from app.ingestion.sandbox.runner import SandboxError, protect_parent
    from app.workers.registry import discover_handlers
    from app.workers.runner import WorkerConfig, run_worker

    # Before anything else: a same-UID extraction child must not be able to
    # read this process's environment or memory (SECURITY.md C1).
    try:
        protect_parent()
    except SandboxError as exc:
        print(f"perfin worker: {exc}", file=sys.stderr)
        return EXIT_FAIL
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
        if not _database_role_ok(settings, "worker", engine=engine):
            return EXIT_FAIL
        return run_worker(engine, config=config, install_signal_handlers=True)
    finally:
        engine.dispose()


def cmd_mcp(_args: argparse.Namespace) -> int:
    """Run the read-only MCP server (AI_ACCESS.md; ``app.mcp``).

    ``main()`` has already run the mcp gate (``app.mcp.config``): no database
    URL, master key or secret key in the environment, an API URL and an
    ``ai:read`` token present. stdio by default; ``PERFIN_MCP_TRANSPORT=http``
    serves streamable HTTP for the reverse proxy.
    """
    from app.mcp.config import McpConfigError, load
    from app.mcp.server import run

    try:
        config = load()
    except McpConfigError as exc:
        print(f"perfin mcp: {exc}", file=sys.stderr)
        return EXIT_FAIL
    run(config)
    return EXIT_OK


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


def cmd_breakglass(args: argparse.Namespace) -> int:
    """Check, set or clear the break-glass flag of a local account (B7).

    ``status`` checks the account named by ``--username`` or
    ``PERFIN_BREAKGLASS_ADMIN_USERNAME`` and exits 1 unless it is usable as
    the break-glass admin (exists, active, flagged, instance admin, local
    password, second factor); ``perfin config check`` cannot see the database,
    so run this before switching to OIDC-only mode. ``enable``/``disable``
    change the flag as the local operator: the change is audited
    (``user.breakglass_set``, ``via: cli``) and ends every session of the
    account. This is the supported way to flag the bootstrap instance admin,
    who cannot change their own account through ``PATCH /admin/users``.
    """
    from sqlalchemy import pool
    from sqlalchemy.exc import SQLAlchemyError

    from app.core.errors import ProblemError
    from app.core.settings import get_settings
    from app.db.session import make_app_engine, make_sessionmaker

    try:
        settings = get_settings()
    except ValidationError as exc:
        _print_validation_error(exc)
        return EXIT_FAIL
    action: str = args.breakglass_command
    username = (args.username or settings.breakglass_admin_username or "").strip().lower()
    prog = f"perfin breakglass {action}"
    if not username:
        print(
            f"{prog}: no username (pass one or set PERFIN_BREAKGLASS_ADMIN_USERNAME)",
            file=sys.stderr,
        )
        return EXIT_FAIL
    engine = make_app_engine(settings, poolclass=pool.NullPool)
    try:
        with make_sessionmaker(engine)() as db, db.begin():
            if action == "status":
                report = _breakglass_report(db, username, settings)
                print(json.dumps(report, sort_keys=True))
                return EXIT_OK if not report["problems"] else EXIT_FAIL
            return _breakglass_set(db, username, enabled=action == "enable", prog=prog)
    except ProblemError as exc:
        print(f"{prog}: {exc.problem_detail or exc.title}", file=sys.stderr)
        return EXIT_FAIL
    except SQLAlchemyError as exc:
        print(f"{prog}: database error ({type(exc).__name__})", file=sys.stderr)
        return EXIT_FAIL
    finally:
        engine.dispose()


def _breakglass_report(db: Session, username: str, settings: Settings) -> dict[str, object]:
    from app.domain.households import repo

    report: dict[str, object] = {
        "username": username,
        "configured": username == (settings.breakglass_admin_username or "").strip().lower(),
        "local_login_enabled": settings.local_login_enabled,
        "breakglass_cidrs": [str(net) for net in settings.breakglass_cidrs],
    }
    problems: list[str] = []
    user = repo.get_user_by_username(db, username)
    if user is None:
        report["exists"] = False
        report["problems"] = ["account-not-found"]
        return report
    factors = repo.mfa_types_by_user(db, [user.id]).get(user.id, [])
    has_password = bool(repo.users_with_password(db, [user.id]))
    report.update(
        exists=True,
        is_active=user.is_active,
        is_breakglass=user.is_breakglass,
        is_instance_admin=user.is_instance_admin,
        has_password=has_password,
        mfa_methods=list(factors),
    )
    for ok, problem in (
        (user.is_active, "account-inactive"),
        (user.is_breakglass, "not-flagged"),
        (user.is_instance_admin, "not-instance-admin"),
        (has_password, "no-local-password"),
        (bool(factors), "no-second-factor"),
        (bool(report["configured"]), "not-the-configured-account"),
    ):
        if not ok:
            problems.append(problem)
    report["problems"] = problems
    return report


def _breakglass_set(db: Session, username: str, *, enabled: bool, prog: str) -> int:
    from app.domain.households import repo, users

    found = repo.get_user_by_username(db, username)
    user = repo.get_user(db, found.id, lock=True) if found is not None else None
    if user is None:
        print(f"{prog}: no account named {username!r}", file=sys.stderr)
        return EXIT_FAIL
    state = "set" if enabled else "cleared"
    if user.is_breakglass == enabled:
        print(f"{prog}: the break-glass flag of {username!r} is already {state}")
        return EXIT_OK
    revoked = users.set_breakglass(db, user, enabled, actor_id=None, request=None, via="cli")
    print(f"{prog}: break-glass flag {state} for {username!r}; {revoked} session(s) ended")
    if enabled:
        if not repo.mfa_types_by_user(db, [user.id]).get(user.id):
            print(f"{prog}: warning: the account has no second factor", file=sys.stderr)
        if not user.is_instance_admin:
            print(f"{prog}: warning: the account is not an instance admin", file=sys.stderr)
    return EXIT_OK


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


def _config_check_mcp() -> int:
    """``perfin config check`` with ``PERFIN_ROLE=mcp`` (role-aware, M7)."""
    from app.mcp.config import check

    config, findings = check()
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    errors = sum(1 for f in findings if f.level == "error")
    warnings = len(findings) - errors
    if config is None:
        print(
            f"perfin config check (mcp): {errors} error(s), {warnings} warning(s); "
            "refusing this configuration",
            file=sys.stderr,
        )
        return EXIT_FAIL
    print(f"perfin config check (mcp): OK ({warnings} warning(s))")
    return EXIT_OK


def cmd_config_check(_args: argparse.Namespace) -> int:
    import os

    from app.core.config_check import check_config, has_errors
    from app.core.settings import Settings

    if os.environ.get("PERFIN_ROLE", "").strip().lower() == "mcp":
        return _config_check_mcp()

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


def cmd_ingest(args: argparse.Namespace) -> int:
    """``perfin ingest --dry-run FILE``: every stage's JSON, nothing written.

    See :mod:`app.ingestion.dryrun`. Real ingestion goes through the API, the
    drop folder or email; this command only ever dry-runs.
    """
    import uuid

    from app.core.settings import get_settings
    from app.ingestion.dryrun import from_settings

    if not args.dry_run or not args.path:
        print("perfin ingest: usage: perfin ingest --dry-run FILE", file=sys.stderr)
        print("(documents are ingested through the API, drop folder or email)", file=sys.stderr)
        return EXIT_UNAVAILABLE
    path = Path(args.path)
    if not path.is_file():
        print(f"perfin ingest: not a regular file: {path}", file=sys.stderr)
        return EXIT_FAIL
    household_id = None
    if args.household:
        try:
            household_id = uuid.UUID(args.household)
        except ValueError:
            print("perfin ingest: --household must be a UUID", file=sys.stderr)
            return EXIT_FAIL
    try:
        settings: Settings | None = get_settings()
    except ValidationError as exc:
        if household_id is not None:
            _print_validation_error(exc)
            return EXIT_FAIL
        settings = None  # extract/classify/parse need no configuration
    from sqlalchemy.exc import SQLAlchemyError

    run = from_settings(path, sys.stdout, settings, household_id=household_id, pretty=args.pretty)
    try:
        return run.run()
    except SQLAlchemyError as exc:
        print(f"perfin ingest: database error ({type(exc).__name__})", file=sys.stderr)
        return EXIT_FAIL


def cmd_review_report(args: argparse.Namespace) -> int:
    """``perfin review-report --household ID``: review items by kind and reason (read-only)."""
    import json
    import uuid

    from app.core.settings import get_settings
    from app.db.session import make_app_engine, make_sessionmaker
    from app.domain.review.report import review_report

    try:
        household_id = uuid.UUID(args.household)
    except ValueError:
        print("perfin review-report: --household must be a UUID", file=sys.stderr)
        return EXIT_FAIL
    try:
        settings = get_settings()
    except ValidationError as exc:
        _print_validation_error(exc)
        return EXIT_FAIL
    engine = make_app_engine(settings)
    try:
        with make_sessionmaker(engine)() as db, db.begin():
            report = review_report(db, household_id)
    finally:
        engine.dispose()
    print(json.dumps(report, indent=2))
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
    sub.add_parser("mcp", help="run the read-only MCP server").set_defaults(func=cmd_mcp)
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
    notify = sub.add_parser("notify", help="send a household notification from a script")
    notify_sub = notify.add_subparsers(dest="notify_command", required=True, metavar="EVENT")
    notify_sub.add_parser(
        "backup-failed", help="tell every household's channels that a backup failed"
    ).set_defaults(func=cmd_notify)

    ingest = sub.add_parser("ingest", help="run the ingestion pipeline on a file")
    ingest.add_argument("path", nargs="?")
    ingest.add_argument("--dry-run", action="store_true")
    ingest.add_argument(
        "--household", help="household id: also read its fingerprints and match an account"
    )
    ingest.add_argument("--pretty", action="store_true", help="indent each stage's JSON")
    ingest.set_defaults(func=cmd_ingest)
    report = sub.add_parser(
        "review-report", help="count a household's review items by kind and reason (read-only)"
    )
    report.add_argument("--household", required=True, help="household id")
    report.set_defaults(func=cmd_review_report)
    sub.add_parser(
        "bootstrap-url", help="print a new one-time setup URL (only while no user exists)"
    ).set_defaults(func=cmd_bootstrap_url)
    breakglass = sub.add_parser(
        "breakglass", help="check or change the break-glass admin account (B7)"
    )
    breakglass_sub = breakglass.add_subparsers(
        dest="breakglass_command", required=True, metavar="ACTION"
    )
    status = breakglass_sub.add_parser(
        "status", help="exit 1 unless the account is a usable break-glass admin"
    )
    status.add_argument(
        "--username", help="account to check (default: PERFIN_BREAKGLASS_ADMIN_USERNAME)"
    )
    for name, text in (("enable", "flag"), ("disable", "unflag")):
        cmd = breakglass_sub.add_parser(
            name, help=f"{text} a local account as break-glass (audited, ends its sessions)"
        )
        cmd.add_argument("username", help="the local account")
    breakglass.set_defaults(func=cmd_breakglass)
    # rotate-key, backup-encrypt, backup-decrypt, backup-info, restore-check
    # (docs/BACKUP_RESTORE.md).
    from app.ops.commands import register as register_ops_commands

    register_ops_commands(sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Handler = args.func
    if args.command in SERVING_COMMANDS and not getattr(handler, "perfin_unavailable", False):
        gate = _startup_gate(args.command)
        if gate != EXIT_OK:
            return gate
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())

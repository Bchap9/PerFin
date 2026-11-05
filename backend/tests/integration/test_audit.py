# SPDX-License-Identifier: AGPL-3.0-or-later
"""audit_log: append-only for every role, RLS visibility, hash chain (SECURITY.md I4).

The audit table is shared by the whole test session and rows can never be
removed, so assertions look only at rows these tests wrote. Tampering is
done as the owner inside a transaction that is always rolled back.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from app.core.audit import REDACTED, RequestMeta, audit, verify_chain
from app.core.logging import bind_request_id
from app.db.models import AuditLog, Household
from app.db.rls import AUDIT_GENESIS_HASH, set_context
from app.db.session import make_sessionmaker

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Households:
    a: uuid.UUID
    b: uuid.UUID


@pytest.fixture
def factory(app_engine: Engine) -> sessionmaker[Session]:
    return make_sessionmaker(app_engine)


@pytest.fixture
def households(factory: sessionmaker[Session], admin_engine: Engine) -> Iterator[Households]:
    with factory() as session, session.begin():
        # ``household`` is under RLS: an instance admin creates households.
        set_context(session, None, instance_admin=True)
        a = Household(name="Synthetic Audit A")
        b = Household(name="Synthetic Audit B")
        session.add_all([a, b])
    try:
        yield Households(a.id, b.id)
    finally:
        with admin_engine.begin() as conn:
            conn.execute(text("DELETE FROM household WHERE id IN (:a, :b)"), {"a": a.id, "b": b.id})


def _row(owner_engine: Engine, row_id: uuid.UUID) -> Any:
    with owner_engine.connect() as conn:
        return conn.execute(
            select(AuditLog.__table__).where(AuditLog.__table__.c.id == row_id)
        ).one()


def _write(
    factory: sessionmaker[Session],
    household_id: uuid.UUID | None,
    action: str = "test.synthetic",
    **kwargs: Any,
) -> uuid.UUID:
    with factory() as session, session.begin():
        # Instance-level rows are written by the system actor (auth layer).
        set_context(session, household_id, system_actor=household_id is None)
        return audit(session, action=action, household_id=household_id, **kwargs)


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


def test_app_role_cannot_update_or_delete(
    factory: sessionmaker[Session], households: Households
) -> None:
    row_id = _write(factory, households.a)
    for statement in (
        "UPDATE audit_log SET action = 'test.tampered' WHERE id = :id",
        "DELETE FROM audit_log WHERE id = :id",
    ):
        with factory() as session, session.begin():
            set_context(session, households.a)
            with pytest.raises(ProgrammingError, match="permission denied for table audit_log"):
                session.execute(text(statement), {"id": row_id})
    with (
        factory() as session,
        session.begin(),
        pytest.raises(ProgrammingError, match="permission denied for table audit_log"),
    ):
        session.execute(text("TRUNCATE audit_log"))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_log SET action = 'test.tampered' WHERE id = :id",
        "DELETE FROM audit_log WHERE id = :id",
        "TRUNCATE audit_log",
    ],
)
def test_trigger_refuses_owner_and_superuser(
    factory: sessionmaker[Session],
    owner_engine: Engine,
    admin_engine: Engine,
    households: Households,
    statement: str,
) -> None:
    row_id = _write(factory, households.a)
    for engine in (owner_engine, admin_engine):
        with (
            engine.connect() as conn,
            conn.begin(),
            pytest.raises(DBAPIError, match="audit_log is append-only"),
        ):
            conn.execute(text(statement), {"id": row_id})
    assert _row(owner_engine, row_id).action == "test.synthetic"


# ---------------------------------------------------------------------------
# RLS visibility
# ---------------------------------------------------------------------------


def _visible(factory: sessionmaker[Session], row_id: uuid.UUID, **ctx: Any) -> bool:
    with factory() as session, session.begin():
        set_context(session, ctx.get("household"), None, instance_admin=ctx.get("admin", False))
        found = session.execute(
            select(AuditLog.__table__.c.id).where(AuditLog.__table__.c.id == row_id)
        ).first()
        return found is not None


def test_household_rows_visible_only_in_that_household(
    factory: sessionmaker[Session], households: Households
) -> None:
    row_id = _write(factory, households.a)
    assert _visible(factory, row_id, household=households.a)
    assert not _visible(factory, row_id, household=households.b)
    assert not _visible(factory, row_id)
    # Instance admins see instance-level rows, not other households' rows.
    assert not _visible(factory, row_id, household=households.b, admin=True)


def test_instance_rows_visible_only_to_instance_admin(
    factory: sessionmaker[Session], households: Households
) -> None:
    row_id = _write(factory, None, action="auth.login_failed")
    assert not _visible(factory, row_id)
    assert not _visible(factory, row_id, household=households.a)
    assert _visible(factory, row_id, admin=True)
    assert _visible(factory, row_id, household=households.a, admin=True)


def test_cannot_append_for_another_household(
    factory: sessionmaker[Session], households: Households
) -> None:
    with factory() as session, session.begin():
        set_context(session, households.a)
        with pytest.raises(DBAPIError, match="row-level security"):
            audit(session, action="test.forged", household_id=households.b)
    with (
        factory() as session,
        session.begin(),
        pytest.raises(DBAPIError, match="row-level security"),
    ):
        audit(session, action="test.forged", household_id=households.a)


@pytest.mark.parametrize(
    "ctx",
    [
        pytest.param({}, id="no-context"),
        pytest.param({"household": "a"}, id="household-context"),
        pytest.param({"household": "a", "user": True}, id="household-and-user-context"),
    ],
)
def test_instance_row_cannot_be_forged_without_system_or_admin_context(
    factory: sessionmaker[Session], households: Households, ctx: dict[str, Any]
) -> None:
    """Review finding SR-5: household code must not forge instance-level events."""
    household = households.a if ctx.get("household") else None
    user = uuid.uuid4() if ctx.get("user") else None
    with factory() as session, session.begin():
        set_context(session, household, user)
        with pytest.raises(DBAPIError, match="row-level security"):
            audit(session, action="admin.role_granted", user_id=uuid.uuid4())


@pytest.mark.parametrize(
    ("household", "instance_admin", "system_actor"),
    [
        pytest.param(None, False, True, id="system-actor"),
        pytest.param("a", False, True, id="system-actor-in-household"),
        pytest.param(None, True, False, id="instance-admin"),
    ],
)
def test_instance_row_appended_by_system_actor_or_instance_admin(
    factory: sessionmaker[Session],
    households: Households,
    owner_engine: Engine,
    household: str | None,
    instance_admin: bool,
    system_actor: bool,
) -> None:
    with factory() as session, session.begin():
        set_context(
            session,
            households.a if household else None,
            instance_admin=instance_admin,
            system_actor=system_actor,
        )
        row_id = audit(session, action="user.password_changed", user_id=uuid.uuid4())
    assert _row(owner_engine, row_id).household_id is None


def test_rolled_back_change_leaves_no_audit_row(
    factory: sessionmaker[Session], households: Households, owner_engine: Engine
) -> None:
    with factory() as session:
        session.begin()
        set_context(session, households.a)
        row_id = audit(session, action="test.rolled_back", household_id=households.a)
        session.rollback()
    with owner_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE id = :id"), {"id": row_id}
        ).scalar_one()
    assert count == 0


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


def test_secrets_are_never_stored(
    factory: sessionmaker[Session], households: Households, owner_engine: Engine
) -> None:
    row_id = _write(
        factory,
        households.a,
        action="user.update",
        target_type="user",
        target_id=uuid.uuid4(),
        before={"password_hash": "$argon2id$v=19$synthetic", "display_name": "Synthetic"},
        after={
            "password": "correct horse battery staple",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "nested": {"api_token": "pf_synthetic_token", "note": "acct 123456789012"},
            "contact": "someone@example.invalid",
            "amount": "12.34",
        },
        meta=RequestMeta(
            ip="203.0.113.7",
            user_agent="SyntheticAgent/1.0 (+mail@example.invalid)\x00",
            request_id="req-0123456789",
        ),
    )
    row = _row(owner_engine, row_id)
    assert row.before == {"password_hash": REDACTED, "display_name": "Synthetic"}
    assert row.after["password"] == REDACTED
    assert row.after["totp_secret"] == REDACTED
    assert row.after["nested"]["api_token"] == REDACTED
    assert "123456789012" not in row.after["nested"]["note"]
    assert "someone@example.invalid" not in row.after["contact"]
    assert row.after["amount"] == "12.34"
    stored = str(row._mapping)
    for secret in ("correct horse", "JBSWY3DPEHPK3PXP", "pf_synthetic_token", "argon2id"):
        assert secret not in stored
    assert str(row.ip) == "203.0.113.7"
    assert "\x00" not in row.user_agent
    assert "mail@example.invalid" not in row.user_agent
    assert row.request_id == "req-0123456789"
    assert row.target_type == "user"


def test_request_id_falls_back_to_logging_context(
    factory: sessionmaker[Session], households: Households, owner_engine: Engine
) -> None:
    with bind_request_id("worker-job-000001") as bound:
        row_id = _write(factory, households.a, meta=RequestMeta(ip="not-an-ip"))
    row = _row(owner_engine, row_id)
    assert row.request_id == bound
    assert row.ip is None
    assert row.before is None  # SQL NULL, not JSON null
    with owner_engine.connect() as conn:
        is_sql_null = conn.execute(
            text("SELECT before IS NULL FROM audit_log WHERE id = :id"), {"id": row_id}
        ).scalar_one()
    assert is_sql_null


def test_server_assigns_chain_columns_and_time(
    factory: sessionmaker[Session], households: Households, owner_engine: Engine
) -> None:
    first = _write(factory, households.a)
    second = _write(factory, households.b)
    a, b = _row(owner_engine, first), _row(owner_engine, second)
    assert b.seq > a.seq
    assert len(a.hash) == 64
    assert a.hash != b.hash
    assert a.created_at.tzinfo is not None
    with owner_engine.connect() as conn:
        db_now = conn.execute(text("SELECT now()")).scalar_one()
    assert abs((db_now - b.created_at).total_seconds()) < 60


def test_orm_insert_also_chained(
    factory: sessionmaker[Session], households: Households, owner_engine: Engine
) -> None:
    with factory() as session, session.begin():
        set_context(session, households.a)
        entry = AuditLog(action="test.orm", household_id=households.a)
        session.add(entry)
        session.flush()
        row_id = entry.id
    assert len(_row(owner_engine, row_id).hash) == 64


# ---------------------------------------------------------------------------
# Hash chain
# ---------------------------------------------------------------------------


def test_chain_verifies(
    factory: sessionmaker[Session], households: Households, owner_engine: Engine
) -> None:
    for household in (households.a, None, households.b):
        _write(factory, household, after={"n": 1})
    with owner_engine.connect() as conn:
        result = verify_chain(conn, batch_size=2)
        first = conn.execute(text("SELECT prev_hash FROM audit_log WHERE seq = 1")).scalar_one()
        total = conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one()
    assert result.ok, result
    assert result.rows_checked == total >= 3
    assert result.head_seq == total
    assert first == AUDIT_GENESIS_HASH


def test_chain_survives_concurrent_writers(
    factory: sessionmaker[Session], households: Households, owner_engine: Engine
) -> None:
    errors: list[Exception] = []

    def writer(household: uuid.UUID) -> None:
        try:
            for i in range(10):
                _write(factory, household, after={"i": i})
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(h,)) for h in (households.a, households.b)]
    threads += [threading.Thread(target=writer, args=(households.a,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors
    with owner_engine.connect() as conn:
        result = verify_chain(conn)
    assert result.ok, result


def _tamper(owner_engine: Engine, statement: str, params: dict[str, Any]) -> Any:
    """Bypass the append-only trigger as the owner, verify, then roll back."""
    with owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only"))
            conn.execute(text(statement), params)
            return verify_chain(conn)
        finally:
            transaction.rollback()


def test_chain_detects_edited_row(
    factory: sessionmaker[Session], households: Households, owner_engine: Engine
) -> None:
    row_id = _write(factory, households.a, after={"amount": "10.00"})
    _write(factory, households.a)
    seq = _row(owner_engine, row_id).seq
    result = _tamper(
        owner_engine,
        """UPDATE audit_log SET after = '{"amount": "99.00"}'::jsonb WHERE id = :id""",
        {"id": row_id},
    )
    assert not result.ok
    assert result.first_bad_seq == seq
    assert result.reason == "hash mismatch"


def test_chain_detects_rehashed_row(
    factory: sessionmaker[Session], households: Households, owner_engine: Engine
) -> None:
    """Recomputing the edited row's own hash still breaks the next link."""
    row_id = _write(factory, households.a, after={"amount": "10.00"})
    _write(factory, households.a)
    seq = _row(owner_engine, row_id).seq
    result = _tamper(
        owner_engine,
        """UPDATE audit_log SET action = 'test.rewritten',
               hash = perfin_audit_hash(prev_hash, seq, id, household_id, user_id,
                   'test.rewritten', target_type, target_id, before, after, ip,
                   user_agent, request_id, created_at)
           WHERE id = :id""",
        {"id": row_id},
    )
    assert not result.ok
    assert result.first_bad_seq == seq + 1
    assert result.reason == "broken link"


def test_chain_detects_deleted_row(
    factory: sessionmaker[Session], households: Households, owner_engine: Engine
) -> None:
    row_id = _write(factory, households.a)
    _write(factory, households.a)
    seq = _row(owner_engine, row_id).seq
    result = _tamper(owner_engine, "DELETE FROM audit_log WHERE id = :id", {"id": row_id})
    assert not result.ok
    assert result.first_bad_seq == seq + 1
    assert result.reason == "sequence gap"
    # Rolled back: the real chain is intact.
    with owner_engine.connect() as conn:
        assert verify_chain(conn).ok

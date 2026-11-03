# SPDX-License-Identifier: AGPL-3.0-or-later
"""RLS DDL generation and the HouseholdScoped registry."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table

from app.db.base import HOUSEHOLD_SCOPED_TABLES, Base, HouseholdScoped, UUIDPk
from app.db.rls import all_rls_ddl, grant_ddl, quote_ident, rls_ddl


def test_rls_ddl_enables_and_forces_with_household_policy() -> None:
    statements = rls_ddl("thing")
    assert 'ALTER TABLE "thing" ENABLE ROW LEVEL SECURITY' in statements
    assert 'ALTER TABLE "thing" FORCE ROW LEVEL SECURITY' in statements
    create = [s for s in statements if s.startswith("CREATE POLICY")]
    assert len(create) == 1
    predicate = "household_id = nullif(current_setting('perfin.household_id', true), '')::uuid"
    assert f"USING ({predicate})" in create[0]
    assert f"WITH CHECK ({predicate})" in create[0]


@pytest.mark.parametrize("name", ['x"; DROP TABLE y; --', "Upper", "1abc", "", "a" * 64, "a b"])
def test_unsafe_identifiers_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        quote_ident(name)
    with pytest.raises(ValueError, match="unsafe SQL identifier"):
        rls_ddl(name)


def test_household_scoped_mixin_registers_concrete_tables_only() -> None:
    class LocalBase(Base):
        __abstract__ = True
        metadata = MetaData()

    class AbstractScoped(HouseholdScoped, UUIDPk, LocalBase):
        __abstract__ = True

    class ScopedThing(AbstractScoped):
        __tablename__ = "zz_test_scoped_thing"

    try:
        assert "zz_test_scoped_thing" in HOUSEHOLD_SCOPED_TABLES
        assert "household_id" in ScopedThing.__table__.c
        fk = next(iter(ScopedThing.__table__.c.household_id.foreign_keys))
        assert fk.target_fullname == "household.id"
        assert not ScopedThing.__table__.c.household_id.nullable
        assert any("zz_test_scoped_thing" in s for s in all_rls_ddl())
    finally:
        HOUSEHOLD_SCOPED_TABLES.discard("zz_test_scoped_thing")


def test_grants_are_least_privilege() -> None:
    metadata = MetaData()
    Table("ledger_row", metadata, Column("id", Integer, primary_key=True))
    Table("audit_log", metadata, Column("id", Integer, primary_key=True))
    statements = grant_ddl(metadata, "perfin_app")
    assert 'GRANT SELECT, INSERT, UPDATE, DELETE ON "ledger_row" TO "perfin_app"' in statements
    assert 'GRANT SELECT, INSERT ON "audit_log" TO "perfin_app"' in statements
    assert not any("ALL PRIVILEGES" in s or "TRUNCATE" in s for s in statements)

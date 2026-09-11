"""Migration 015 — LedgerEntries rebuilt with kind='constraint', why_ref, why_visibility."""
import sqlite3
from pathlib import Path

import pytest

from orchestrator import db

MIGRATIONS = Path(db.__file__).parent / "migrations"


def test_015_ledger_accepts_constraint_kind_and_why_columns(conn):
    conn.execute("INSERT INTO LedgerEntries (project, kind, subjects, statement, why_ref, why_visibility) "
                 "VALUES ('p','constraint','[\"nk_abc\"]','exclude region X','https://wiki/decisions/42','restricted')")
    row = conn.execute("SELECT kind, why_ref, why_visibility, hit_count FROM LedgerEntries").fetchone()
    assert tuple(row) == ("constraint", "https://wiki/decisions/42", "restricted", 0)
    assert conn.execute("SELECT why_visibility FROM LedgerEntries WHERE 0").fetchone() is None
    conn.execute("INSERT INTO LedgerEntries (project, kind, statement) VALUES ('p','decision','s')")
    assert conn.execute("SELECT why_visibility FROM LedgerEntries WHERE kind='decision'").fetchone()[0] == "shared"


def test_015_preserves_existing_rows_and_indexes(tmp_path, monkeypatch):
    """Apply everything up to 014 (through run_migrations itself), insert a
    legacy row under the old CHECK, then let 015 rebuild the table."""
    import shutil
    upto_014 = tmp_path / "migrations"
    upto_014.mkdir()
    for sql_file in sorted(MIGRATIONS.glob("*.sql")):
        if sql_file.name < "015":
            shutil.copy(sql_file, upto_014 / sql_file.name)
    c = db.open_db(tmp_path / "old.db")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", upto_014)
    assert db.run_migrations(c) == 14
    c.execute("INSERT INTO LedgerEntries (project, kind, subjects, keywords, statement, rationale, plan_id, hit_count) "
              "VALUES ('p','decision','[\"split\"]','[\"rolling\"]','legacy','because','plan-1',3)")
    c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO LedgerEntries (project, kind, statement) VALUES ('p','constraint','not yet')")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", MIGRATIONS)
    assert db.run_migrations(c) == 1                         # exactly 015
    row = c.execute("SELECT id, statement, rationale, plan_id, hit_count, why_visibility FROM LedgerEntries").fetchone()
    assert tuple(row) == (1, "legacy", "because", "plan-1", 3, "shared")
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='LedgerEntries'")}
    assert {"idx_ledger_project_status", "idx_ledger_project_kind"} <= names
    c.execute("INSERT INTO LedgerEntries (project, kind, statement) VALUES ('p','constraint','now allowed')")
    assert db.run_migrations(c) == 0
    c.close()


def test_015_rejects_bad_visibility_and_kind(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO LedgerEntries (project, kind, statement, why_visibility) VALUES ('p','decision','s','secret')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO LedgerEntries (project, kind, statement) VALUES ('p','rule','s')")

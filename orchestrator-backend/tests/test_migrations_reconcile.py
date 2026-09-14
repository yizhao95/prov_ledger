"""FL-021: run_migrations on a DB migrated before filename bookkeeping existed
must reconcile (record the files those version rows applied) and then apply
only the NEW files — never re-run 001 and die on 'duplicate column'."""
import sqlite3

import pytest

from orchestrator import db


def _legacy_db(tmp_path, upto: int):
    """A DB whose first `upto` migrations were applied WITHOUT filename bookkeeping
    (the pre-migration_file era): executescript each file, insert version rows only."""
    c = db.open_db(tmp_path / "legacy.db")
    files = sorted(db.MIGRATIONS_DIR.glob("*.sql"))[:upto]
    for f in files:
        c.executescript(f.read_text())
        # what the pre-bookkeeping loop did: MAX(version)+1 per file — on top of
        # 001's own marker row, so a legacy DB holds files+1 unlabelled rows
        c.execute("INSERT OR IGNORE INTO schema_version (version) "
                  "VALUES ((SELECT COALESCE(MAX(version), 0) + 1 FROM schema_version))")
    c.commit()
    assert c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == upto + 1
    assert "migration_file" not in {r[1] for r in c.execute("PRAGMA table_info(schema_version)")}
    return c


def test_fl021_rerun_on_legacy_db_applies_only_new_files(tmp_path):
    c = _legacy_db(tmp_path, upto=13)
    n = db.run_migrations(c)                       # must NOT raise 'duplicate column'
    total = len(list(db.MIGRATIONS_DIR.glob("*.sql")))
    assert n == total - 13
    names = {r[0] for r in c.execute("SELECT migration_file FROM schema_version")}
    assert all(f.name in names for f in db.MIGRATIONS_DIR.glob("*.sql"))
    # only 001's own marker row (version 1, written by the SQL itself) stays unlabelled — on every DB
    assert [r[0] for r in c.execute("SELECT version FROM schema_version WHERE migration_file IS NULL")] == [1]
    assert "project" in {r[1] for r in c.execute("PRAGMA table_info(Plans)")}
    assert db.run_migrations(c) == 0
    c.close()


def test_fl021_rerun_on_current_db_is_noop(conn):
    assert db.run_migrations(conn) == 0


def test_fl021_fresh_db_applies_everything(tmp_path):
    c = db.open_db(tmp_path / "fresh.db")
    assert db.run_migrations(c) == len(list(db.MIGRATIONS_DIR.glob("*.sql"))) >= 16
    c.close()


# ── 对账自愈 (phase 4 Task 0): a legacy DB that recorded one row too few ────────

def test_reconcile_self_heals_when_a_version_row_is_missing(tmp_path, capsys):
    """Every file was applied but the bookkeeping is one row short: reconcile can
    only label the rows it has, so the unrecorded file re-runs, hits 'duplicate
    column name' and must be rolled back + marked applied — never raise. The
    row dropped is 016's (an ALTER TABLE, not idempotent) rather than the newest
    file's: since 017 the newest migrations are IF NOT EXISTS-idempotent and
    re-run silently, which is fine but exercises nothing."""
    c = db.open_db(tmp_path / "labelled.db")
    db.run_migrations(c)                                       # every file applied AND labelled
    c.execute("DELETE FROM schema_version WHERE migration_file = '016_plans_project.sql'")
    c.commit()
    n = db.run_migrations(c)                                   # must NOT raise
    assert n == 1
    names = {r[0] for r in c.execute("SELECT migration_file FROM schema_version")}
    assert all(f.name in names for f in db.MIGRATIONS_DIR.glob("*.sql"))
    assert "project" in {r[1] for r in c.execute("PRAGMA table_info(Plans)")}
    assert "reconciled 016_plans_project.sql: already applied" in capsys.readouterr().err
    assert db.run_migrations(c) == 0
    c.close()


def test_reconcile_rolls_back_the_savepoint_and_reraises_other_errors(tmp_path, monkeypatch):
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "001_init.sql").write_text(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY);\n"
        "INSERT INTO schema_version (version) VALUES (1);\n"
        "CREATE TABLE t (a INTEGER);\n")
    # partial file: its first statement succeeds, its second is a duplicate column
    (mig / "002_partial.sql").write_text(
        "CREATE TABLE t_side (x INTEGER);\nALTER TABLE t ADD COLUMN a INTEGER;\n")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", mig)
    c = db.open_db(tmp_path / "x.db")
    assert db.run_migrations(c) == 2
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "t_side" not in tables, "the failed file must be rolled back to its savepoint"
    assert {r[0] for r in c.execute("SELECT migration_file FROM schema_version WHERE migration_file IS NOT NULL")} \
        == {"001_init.sql", "002_partial.sql"}
    # anything else still raises (and nothing of it sticks)
    (mig / "003_bad.sql").write_text("CREATE TABLE t_bad (y INTEGER);\nSELECT * FROM nope;\n")
    with pytest.raises(sqlite3.OperationalError):
        db.run_migrations(c)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "t_bad" not in tables
    c.close()

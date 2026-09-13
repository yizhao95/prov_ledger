"""FL-021: run_migrations on a DB migrated before filename bookkeeping existed
must reconcile (record the files those version rows applied) and then apply
only the NEW files — never re-run 001 and die on 'duplicate column'."""
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

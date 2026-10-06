"""A constraint added to a brand-new ledger is recorded once.

Since DP phase 1 a ledger constraint is mirrored into change_reason the moment it
is added (ledger_store -> constraints.record_constraint). The one-time reclass
(provenance_migrate, run by db.open_db until migration_state says dp_reclass)
copies LedgerEntries constraints into change_reason as well. On a freshly
created ledger nothing marked that reclass done, so a constraint added before
the next open was written twice: four rows for one constraint with two subjects.
anchored_constraints folds twins by (statement, …, recorded_at), which hid the
copy whenever both writes landed in the same second; under load they did not,
and the close recorded constraint_bypassed twice (the release check's flaky
constraint_bypassed scenario).
"""
import json
from pathlib import Path

from orchestrator import constraints, db, provenance_migrate

MIGRATIONS = Path(db.__file__).resolve().parent / "migrations"
STATEMENT = "clean() must keep dropping zero-quantity rows"
SUBJECTS = ["pkg.pipeline.clean", "nk_290532fc7c77"]


def _add_like_ledger_store(conn) -> None:
    """What ledger_store.add_entry does for kind='constraint': the LedgerEntries row, then the mirror."""
    conn.execute("INSERT INTO LedgerEntries (project, kind, subjects, keywords, statement, rationale, source) "
                 "VALUES ('p', 'constraint', ?, '[]', ?, '', 'extensions')", (json.dumps(SUBJECTS), STATEMENT))
    conn.commit()
    constraints.record_constraint(conn, project="p", subjects=SUBJECTS, statement=STATEMENT, rationale="")


def _constraint_anchors(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT node_key FROM change_reason WHERE role = 'constraint' AND rule_id IS NULL ORDER BY id")]


def test_a_constraint_added_to_a_fresh_ledger_is_recorded_once(tmp_path):
    conn = db.open_db(tmp_path / "o.db")
    db.run_migrations(conn)
    _add_like_ledger_store(conn)
    conn.close()
    conn = db.open_db(tmp_path / "o.db")       # the open where the reclass used to copy it again
    assert _constraint_anchors(conn) == SUBJECTS
    conn.close()


def test_a_ledger_from_before_018_still_gets_its_constraints_reclassed_once(tmp_path, monkeypatch):
    staged = tmp_path / "migrations_017"
    staged.mkdir()
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name < "018":
            (staged / f.name).symlink_to(f)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged)
    conn = db.open_db(tmp_path / "old.db")
    db.run_migrations(conn)
    conn.execute("INSERT INTO LedgerEntries (project, kind, subjects, statement) VALUES ('p', 'constraint', ?, ?)",
                 (json.dumps(SUBJECTS), STATEMENT))
    conn.commit()
    conn.close()
    monkeypatch.undo()
    conn = db.open_db(tmp_path / "old.db")
    db.run_migrations(conn)                    # 018 and later arrive on a ledger that has legacy rows
    conn.close()
    conn = db.open_db(tmp_path / "old.db")     # the reclass runs here
    assert _constraint_anchors(conn) == SUBJECTS
    conn.close()
    conn = db.open_db(tmp_path / "old.db")
    assert _constraint_anchors(conn) == SUBJECTS, "the reclass runs once"
    conn.close()


def test_the_reclass_skips_a_constraint_already_mirrored(tmp_path):
    """A ledger created before this fix may hold mirrored constraints and no reclass marker."""
    conn = db.open_db(tmp_path / "o.db")
    db.run_migrations(conn)
    _add_like_ledger_store(conn)
    conn.execute("DELETE FROM migration_state WHERE key = 'dp_reclass'")
    conn.commit()
    provenance_migrate.run(conn)
    assert _constraint_anchors(conn) == SUBJECTS
    conn.close()

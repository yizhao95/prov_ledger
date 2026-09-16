"""Migration 019 — shown (read_hit), adopted (influence), headline + responses,
session_run, the overhead columns, reason_stats_v; trigger_log accepts 'ambiguous'."""
import sqlite3

import pytest

from orchestrator import db, provenance as pv

TABLES = {"read_hit", "influence", "headline", "headline_response", "session_run"}


def _names(conn, kind):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def _reason(conn, **kw):
    base = dict(project="p", plan_id="P1", node_key="nk_a", kind="technical", interpretation="weekly grain")
    base.update(kw)
    return pv.insert_reason(conn, **base)


def test_019_tables_views_triggers_and_columns(conn):
    assert TABLES <= _names(conn, "table") and "reason_stats_v" in _names(conn, "view")
    trig = _names(conn, "trigger")
    for t in ("read_hit", "influence", "headline", "headline_response", "trigger_log"):
        assert f"trg_{t}_no_delete" in trig and f"trg_{t}_no_update" in trig, t
    assert "command_head" in {r[1] for r in conn.execute("PRAGMA table_info(tool_call_log)")}
    assert "headline_json" in {r[1] for r in conn.execute("PRAGMA table_info(Plans)")}
    assert "injected_chars" in {r[1] for r in conn.execute("PRAGMA table_info(read_hit)")}
    assert db.run_migrations(conn) == 0
    assert "019_read_hit_influence.sql" in {r[0] for r in conn.execute("SELECT migration_file FROM schema_version")}


def test_trigger_log_accepts_ambiguous_and_stays_append_only(conn):
    conn.execute("INSERT INTO trigger_log (project, plan_id, node_key, path, rule_id, verdict, basis) VALUES ('p', 'P1', 'nk_a', 'code', 'R0', 'ambiguous', 'R0: sentence names 4 nodes')")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("INSERT INTO trigger_log (project, plan_id, node_key, path, verdict) VALUES ('p', 'P1', 'nk_a', 'code', 'maybe')")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM trigger_log")


def test_read_hit_and_influence_shapes_and_append_only(conn):
    r = _reason(conn)
    conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, moment, injected_chars) VALUES (?, 'p', 'P1', 'edit', 120)", (r,))
    conn.execute("INSERT INTO influence (reason_id, project, plan_id, node_key, via, by) VALUES (?, 'p', 'P1', 'nk_a', 'reason_because', 'agent')", (r,))
    for table, bad in (("read_hit", "INSERT INTO read_hit (reason_id, project, moment) VALUES (%d, 'p', 'dream')" % r),
                       ("influence", "INSERT INTO influence (reason_id, project, via, by) VALUES (%d, 'p', 'telepathy', 'agent')" % r),
                       ("influence", "INSERT INTO influence (reason_id, project, via, by) VALUES (%d, 'p', 'reason_because', 'model')" % r)):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            conn.execute(bad)
    for table in ("read_hit", "influence"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"DELETE FROM {table}")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"UPDATE {table} SET project='q'")
    with pytest.raises(sqlite3.IntegrityError):                       # a hit needs an existing reason
        conn.execute("INSERT INTO read_hit (reason_id, project, moment) VALUES (999, 'p', 'plan')")


def test_headline_response_is_unique_per_finding_and_a_recompute_is_a_new_row(conn):
    conn.execute("INSERT INTO headline (project, plan_id, findings_json) VALUES ('p', 'P1', '[]')")
    h1 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO headline_response (headline_id, finding_id, action, rationale, by) VALUES (?, 'active_constraint:nk_a:1', 'proceed', 'x', 'agent')", (h1,))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute("INSERT INTO headline_response (headline_id, finding_id, action, by) VALUES (?, 'active_constraint:nk_a:1', 'revise', 'agent')", (h1,))
    conn.execute("INSERT INTO headline (project, plan_id, findings_json) VALUES ('p', 'P1', '[]')")     # changing the answer = a new headline
    h2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO headline_response (headline_id, finding_id, action, by) VALUES (?, 'active_constraint:nk_a:1', 'revise', 'human')", (h2,))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("INSERT INTO headline (project, findings_json) VALUES ('p', '[]')")               # neither plan nor session
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM headline")


def test_reason_stats_v_counts_per_moment_never_summed(conn):
    r = _reason(conn)
    for m in ("plan", "edit", "edit", "why"):
        conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, moment) VALUES (?, 'p', 'P1', ?)", (r, m))
    conn.execute("INSERT INTO influence (reason_id, project, plan_id, via, by) VALUES (?, 'p', 'P1', 'headline_response', 'agent')", (r,))
    row = dict(conn.execute("SELECT * FROM reason_stats_v WHERE reason_id=?", (r,)).fetchone())
    assert (row["shown_plan"], row["shown_edit"], row["shown_close"], row["shown_why"], row["adopted"]) == (1, 2, 0, 1, 1)
    assert "shown_total" not in row
    assert dict(conn.execute("SELECT * FROM reason_stats_v WHERE reason_id=?", (_reason(conn),)).fetchone())["adopted"] == 0


def test_session_run_is_the_only_mutable_table(conn):
    conn.execute("INSERT INTO session_run (session_id, project, cwd, started_at) VALUES ('s1', 'p', '/x', '2026-09-15 10:00:00')")
    conn.execute("UPDATE session_run SET refresh_state='queued', ended_at='2026-09-15 11:00:00' WHERE session_id='s1'")
    assert conn.execute("SELECT refresh_state FROM session_run").fetchone()[0] == "queued"
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("UPDATE session_run SET refresh_state='maybe'")


def test_reconcile_on_a_db_that_applied_019_without_bookkeeping(tmp_path):
    c = db.open_db(tmp_path / "legacy.db")
    for f in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        c.executescript(f.read_text())
        c.execute("INSERT OR IGNORE INTO schema_version (version) VALUES ((SELECT COALESCE(MAX(version), 0) + 1 FROM schema_version))")
    c.commit()
    assert db.run_migrations(c) == 0 and TABLES <= _names(c, "table")
    c.close()

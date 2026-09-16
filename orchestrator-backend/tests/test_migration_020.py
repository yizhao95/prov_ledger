"""Migration 020 — plans carry their session, significance_log, node badges (DP phase 2b, Task 1)."""
import sqlite3

import pytest

from orchestrator import constraints, provenance as pv


def _names(conn, kind):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type=?", (kind,))}


def test_column_table_view_and_index_exist(conn):
    assert "session_id" in [r[1] for r in conn.execute("PRAGMA table_info(Plans)")]
    assert "significance_log" in _names(conn, "table") and "node_badge_v" in _names(conn, "view") and "change_reason_v" in _names(conn, "view")
    assert "idx_plans_session" in _names(conn, "index") and "idx_significance_log_reason" in _names(conn, "index")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(significance_log)")]
    assert cols == ["id", "reason_id", "project", "hint", "hint_basis", "verdict", "verdict_basis", "judged_by", "runner", "at"]
    assert "significance_eff" in [r[1] for r in conn.execute("PRAGMA table_info(change_reason_v)")]


def _reason(conn, key="nk_a", **kw):
    return pv.insert_reason(conn, project="proj", plan_id="P1", node_key=key, kind="technical", interpretation="why", recorded_by="agent", **kw)


def test_significance_log_is_append_only_and_checks_its_shape(conn):
    rid = _reason(conn)
    conn.execute("INSERT INTO significance_log (reason_id, project, hint, hint_basis, judged_by) VALUES (?, 'proj', 'major', 'struct_sig changed', 'hint')", (rid,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE significance_log SET hint='minor'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM significance_log")
    with pytest.raises(sqlite3.IntegrityError):                                    # an llm / human row must carry a verdict
        conn.execute("INSERT INTO significance_log (reason_id, project, hint, hint_basis, judged_by) VALUES (?, 'proj', 'major', 'x', 'llm')", (rid,))
    with pytest.raises(sqlite3.IntegrityError):                                    # closed sets
        conn.execute("INSERT INTO significance_log (reason_id, project, hint, hint_basis, judged_by) VALUES (?, 'proj', 'huge', 'x', 'hint')", (rid,))


def test_significance_eff_is_the_latest_verdict_then_the_latest_hint_then_null(conn):
    rid = _reason(conn)
    eff = lambda: conn.execute("SELECT significance_eff FROM change_reason_v WHERE id=?", (rid,)).fetchone()[0]
    assert eff() is None
    conn.execute("INSERT INTO significance_log (reason_id, project, hint, hint_basis, judged_by) VALUES (?, 'proj', 'minor', 'nothing hit', 'hint')", (rid,))
    assert eff() == "minor"
    conn.execute("INSERT INTO significance_log (reason_id, project, hint, hint_basis, verdict, verdict_basis, judged_by, runner) VALUES (?, 'proj', 'minor', 'nothing hit', 'major', 'renames a public api', 'llm', 'stub')", (rid,))
    assert eff() == "major"
    conn.execute("INSERT INTO significance_log (reason_id, project, hint, hint_basis, judged_by) VALUES (?, 'proj', 'major', 'later hint', 'hint')", (rid,))
    assert eff() == "major"                                                        # a verdict outranks a later hint
    conn.execute("INSERT INTO significance_log (reason_id, project, hint, hint_basis, verdict, verdict_basis, judged_by) VALUES (?, 'proj', 'major', 'later hint', 'minor', 'a person said so', 'human')", (rid,))
    assert eff() == "minor"                                                        # the latest verdict wins
    # the stored column of phase 1 is not consulted
    assert conn.execute("SELECT significance FROM change_reason WHERE id=?", (rid,)).fetchone()[0] is None


def test_node_badge_v_counts_what_has_a_story(conn):
    r1 = _reason(conn); r2 = _reason(conn); r3 = _reason(conn)
    pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical", recorded_by="system")            # unstated: no badge
    pv.insert_reason(conn, project="proj", plan_id="P1", node_key="nk_a", kind="technical", role="rejected_path", interpretation="tried x", rule_id="R6", recorded_by="system")
    c1 = constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="keep paid")
    c2 = constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="old rule")
    pv.supersede(conn, c2, c1)
    conn.execute("INSERT INTO significance_log (reason_id, project, hint, hint_basis, judged_by) VALUES (?, 'proj', 'minor', 'nothing hit', 'hint')", (r2,))
    row = conn.execute("SELECT reasons, rejected_paths, constraints, badge, minor FROM node_badge_v WHERE node_key='nk_a'").fetchone()
    assert tuple(row) == (2, 1, 1, 4, 1)              # r1 (no judgement → counts), r3 (counts), r2 minor (folded); 1 rejected; 1 active constraint
    assert conn.execute("SELECT COUNT(*) FROM node_badge_v WHERE node_key='nk_zzz'").fetchone()[0] == 0


def test_plans_session_id_round_trips_through_db_helper(conn):
    from orchestrator import db
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status) VALUES ('P1', 'g', 'IN_PROGRESS')")
    db.set_plan_session(conn, "P1", "sess-1")
    assert conn.execute("SELECT session_id FROM Plans WHERE plan_id='P1'").fetchone()[0] == "sess-1"
    db.set_plan_session(conn, "P1", None)
    assert conn.execute("SELECT session_id FROM Plans WHERE plan_id='P1'").fetchone()[0] is None

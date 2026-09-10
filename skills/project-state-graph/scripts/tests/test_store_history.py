"""Node-history tables in analyzer.store (spec §2.1): append-only snapshots and
events, one-shot node_key assignment, run attribution, reset_graph isolation."""
import sqlite3

import pytest

from analyzer import store


@pytest.fixture
def db(tmp_path):
    conn = store.init_db(str(tmp_path / "demo-state-graph.db"))
    yield conn
    conn.close()


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _snap(conn, run_id, qn="pkg.m.f", **kw):
    args = dict(node_type="function", qualified_name=qn, file_path="pkg/m.py", line_start=1, line_end=2,
                struct_sig="s1", dataflow_sig="d1", dataflow_trivial=False, attrs={"node_id": 7})
    args.update(kw)
    return store.add_node_snapshot(conn, run_id, **args)


def test_history_tables_and_columns_exist(db):
    assert {"node_snapshot", "node_event"} <= _tables(db)
    assert "node_key" in _cols(db, "node")
    assert {"plan_id", "step_id", "trigger"} <= _cols(db, "analysis_run")


def test_init_db_upgrades_an_older_db(tmp_path):
    """A pre-history DB (no node_key, no history tables) is upgraded in place."""
    path = tmp_path / "old-state-graph.db"
    c = sqlite3.connect(str(path))
    c.executescript(store._SCHEMA)
    c.close()
    conn = store.init_db(str(path))
    assert {"node_snapshot", "node_event"} <= _tables(conn)
    assert "node_key" in _cols(conn, "node") and "trigger" in _cols(conn, "analysis_run")
    conn.close()


def test_start_run_records_attribution(db):
    rid = store.start_run(db, project_name="demo", commit_sha="abc", plan_id="P", step_id="S", trigger="review")
    assert db.execute("SELECT plan_id, step_id, trigger FROM analysis_run WHERE id=?", (rid,)).fetchone() == ("P", "S", "review")
    rid2 = store.start_run(db, project_name="demo", commit_sha="abc")
    assert db.execute("SELECT plan_id, step_id, trigger FROM analysis_run WHERE id=?", (rid2,)).fetchone() == (None, None, "manual")


def test_snapshot_placeholder_key_assigned_exactly_once(db):
    rid = store.start_run(db, project_name="demo")
    sid = _snap(db, rid)
    assert db.execute("SELECT node_key, attrs_json FROM node_snapshot WHERE id=?", (sid,)).fetchone()[0] == ""
    store.set_snapshot_key(db, sid, "nk_abc")
    assert db.execute("SELECT node_key FROM node_snapshot WHERE id=?", (sid,)).fetchone()[0] == "nk_abc"
    with pytest.raises(ValueError):
        store.set_snapshot_key(db, sid, "nk_other")
    assert db.execute("SELECT node_key FROM node_snapshot WHERE id=?", (sid,)).fetchone()[0] == "nk_abc"


def test_snapshot_key_unique_per_run(db):
    rid = store.start_run(db, project_name="demo")
    a, b = _snap(db, rid, qn="pkg.m.a"), _snap(db, rid, qn="pkg.m.b")
    store.set_snapshot_key(db, a, "nk_1")
    with pytest.raises(sqlite3.IntegrityError):
        store.set_snapshot_key(db, b, "nk_1")


def test_events_seq_increments_per_run_and_tier_is_checked(db):
    r1 = store.start_run(db, project_name="demo")
    r2 = store.start_run(db, project_name="demo")
    e1 = store.add_node_event(db, r1, "node_added", "nk_1", {"qualified_name": "pkg.m.f"})
    e2 = store.add_node_event(db, r1, "node_matched", "nk_2", {})
    e3 = store.add_node_event(db, r2, "node_removed", "nk_1", {}, tier="asserted")
    seqs = {r[0]: (r[1], r[2]) for r in db.execute("SELECT id, run_id, seq FROM node_event")}
    assert seqs[e1] == (r1, 1) and seqs[e2] == (r1, 2) and seqs[e3] == (r2, 1)
    assert db.execute("SELECT tier, created_at FROM node_event WHERE id=?", (e1,)).fetchone()[0] == "observed"
    with pytest.raises(ValueError):
        store.add_node_event(db, r1, "node_added", "nk_3", {}, tier="guessed")


def test_events_are_append_only(db):
    rid = store.start_run(db, project_name="demo")
    eid = store.add_node_event(db, rid, "node_added", "nk_1", {})
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE node_event SET event_type='x' WHERE id=?", (eid,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM node_event WHERE id=?", (eid,))


def test_snapshots_cannot_be_deleted(db):
    rid = store.start_run(db, project_name="demo")
    sid = _snap(db, rid)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM node_snapshot WHERE id=?", (sid,))


def test_reset_graph_keeps_history(db):
    rid = store.start_run(db, project_name="demo")
    t = store.get_or_create_node_type(db, "function")
    store.add_node(db, t, name="f", qualified_name="pkg.m.f")
    _snap(db, rid)
    store.add_node_event(db, rid, "node_added", "nk_1", {})
    store.reset_graph(db)
    assert db.execute("SELECT COUNT(*) FROM node").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM node_snapshot").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM node_event").fetchone()[0] == 1


def test_latest_and_previous_run_id(db):
    r1 = store.start_run(db, project_name="demo")
    _snap(db, r1)
    r_other = store.start_run(db, project_name="other")
    _snap(db, r_other)
    r2 = store.start_run(db, project_name="demo")          # no snapshots: skipped as a predecessor
    r3 = store.start_run(db, project_name="demo")
    _snap(db, r3)
    assert store.latest_run_id(db) == r3
    assert store.previous_run_id(db, r3) == r1
    assert store.previous_run_id(db, r1) is None
    assert store.previous_run_id(db, r2) == r1

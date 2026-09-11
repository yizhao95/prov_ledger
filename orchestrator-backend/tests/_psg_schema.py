"""Schema contract for orchestrator.psg_bridge tests.

HISTORY_SCHEMA is a VERBATIM copy of
    skills/project-state-graph/scripts/analyzer/store.py::_HISTORY_SCHEMA
(the backend suite must not import the analyzer). test_psg_bridge.py asserts
the copy still appears verbatim in store.py so the two cannot drift. The
analysis_run / node DDL below is the minimal subset the bridge reads.
"""
import sqlite3

HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS node_snapshot (
    id               INTEGER PRIMARY KEY,
    run_id           INTEGER NOT NULL REFERENCES analysis_run(id),
    node_key         TEXT NOT NULL,
    node_type        TEXT NOT NULL,
    qualified_name   TEXT NOT NULL,
    file_path        TEXT,
    line_start       INTEGER,
    line_end         INTEGER,
    struct_sig       TEXT,
    dataflow_sig     TEXT,
    dataflow_trivial INTEGER NOT NULL DEFAULT 1,
    attrs_json       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_event (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES analysis_run(id),
    seq          INTEGER NOT NULL,
    event_type   TEXT NOT NULL,
    node_key     TEXT,
    tier         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_node_snapshot_run_key ON node_snapshot(run_id, node_key)
    WHERE node_key <> '';
CREATE INDEX IF NOT EXISTS idx_node_snapshot_run_qn ON node_snapshot(run_id, node_type, qualified_name);
CREATE INDEX IF NOT EXISTS idx_node_event_key ON node_event(node_key, run_id);
CREATE TRIGGER IF NOT EXISTS trg_node_event_no_update BEFORE UPDATE ON node_event
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_node_event_no_delete BEFORE DELETE ON node_event
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_node_snapshot_no_delete BEFORE DELETE ON node_snapshot
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_run (
    id           INTEGER PRIMARY KEY,
    project_name TEXT NOT NULL,
    commit_sha   TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    tool_version TEXT,
    plan_id      TEXT,
    step_id      TEXT,
    trigger      TEXT
);
CREATE TABLE IF NOT EXISTS node_type (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT);
CREATE TABLE IF NOT EXISTS node (
    id INTEGER PRIMARY KEY, node_type_id INTEGER NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
    file_path TEXT, line_start INTEGER, line_end INTEGER, metadata_json TEXT, run_id INTEGER, node_key TEXT
);
"""


def build(path) -> sqlite3.Connection:
    """A fresh PSG-shaped DB (history tables + triggers) at `path`."""
    c = sqlite3.connect(str(path))
    c.executescript(BASE_SCHEMA + HISTORY_SCHEMA)
    c.commit()
    return c


def add_run(c, run_id, plan_id=None, step_id=None, sha="c0ffee", trigger="review"):
    c.execute("INSERT INTO analysis_run (id, project_name, commit_sha, started_at, plan_id, step_id, trigger) "
              "VALUES (?, 'demo', ?, '2026-09-11T00:00:00+00:00', ?, ?, ?)", (run_id, sha, plan_id, step_id, trigger))


def add_snapshot(c, run_id, key, qn, ntype="function", struct_sig="s1"):
    c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, struct_sig, "
              "dataflow_sig, dataflow_trivial, attrs_json) VALUES (?, ?, ?, ?, 'pkg/m.py', ?, NULL, 1, '{}')",
              (run_id, key, ntype, qn, struct_sig))


def add_event(c, run_id, seq, event_type, key, payload="{}", tier="observed"):
    c.execute("INSERT INTO node_event (run_id, seq, event_type, node_key, tier, payload_json, created_at) "
              "VALUES (?, ?, ?, ?, ?, ?, '2026-09-11T00:00:01+00:00')", (run_id, seq, event_type, key, tier, payload))

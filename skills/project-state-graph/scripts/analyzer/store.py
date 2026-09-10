"""SQLite store for the project state-graph. Sole owner of DB access.

Schema (generic graph):
    node_type    (id, name UNIQUE, description)
    node         (id, node_type_id, name, qualified_name, file_path,
                  line_start, line_end, metadata_json)
    edge_type    (id, name UNIQUE, description)
    edge         (id, edge_type_id, src_node_id, dst_node_id, metadata_json)
    analysis_run (id, project_name, commit_sha, started_at, finished_at, tool_version,
                  plan_id, step_id, trigger)

Node history (spec §2.1, append-only — never deleted, never reset):
    node_snapshot (id, run_id, node_key, node_type, qualified_name, file_path,
                   line_start, line_end, struct_sig, dataflow_sig, dataflow_trivial, attrs_json)
    node_event    (id, run_id, seq, event_type, node_key, tier, payload_json, created_at)
    node.node_key is the stable identity backfilled per run by analyzer.history.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from typing import Optional

TOOL_VERSION = "0.1.0"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS node_type (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT
);
CREATE TABLE IF NOT EXISTS node (
    id             INTEGER PRIMARY KEY,
    node_type_id   INTEGER NOT NULL REFERENCES node_type(id),
    name           TEXT NOT NULL,
    qualified_name TEXT,
    file_path      TEXT,
    line_start     INTEGER,
    line_end       INTEGER,
    metadata_json  TEXT,
    run_id         INTEGER REFERENCES analysis_run(id),
    dtype          TEXT,
    dtype_provenance TEXT,
    data_class     TEXT,
    nullable       INTEGER
);
CREATE TABLE IF NOT EXISTS edge_type (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT
);
CREATE TABLE IF NOT EXISTS edge (
    id            INTEGER PRIMARY KEY,
    edge_type_id  INTEGER NOT NULL REFERENCES edge_type(id),
    src_node_id   INTEGER NOT NULL REFERENCES node(id),
    dst_node_id   INTEGER NOT NULL REFERENCES node(id),
    metadata_json TEXT,
    run_id        INTEGER REFERENCES analysis_run(id),
    confidence    TEXT
);
CREATE TABLE IF NOT EXISTS analysis_run (
    id           INTEGER PRIMARY KEY,
    project_name TEXT NOT NULL,
    commit_sha   TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    tool_version TEXT
);
"""

# Indexes are created AFTER the defensive column-ALTER (init_db), so upgrading an
# older DB that predates a column doesn't fail building that column's index.
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_node_node_type_id ON node(node_type_id);
CREATE INDEX IF NOT EXISTS idx_node_file_path     ON node(file_path);
CREATE INDEX IF NOT EXISTS idx_node_qualified_name ON node(qualified_name);
CREATE INDEX IF NOT EXISTS idx_node_name          ON node(name);
CREATE INDEX IF NOT EXISTS idx_node_run_id        ON node(run_id);
CREATE INDEX IF NOT EXISTS idx_edge_src_node_id   ON edge(src_node_id);
CREATE INDEX IF NOT EXISTS idx_edge_dst_node_id   ON edge(dst_node_id);
CREATE INDEX IF NOT EXISTS idx_edge_edge_type_id  ON edge(edge_type_id);
CREATE INDEX IF NOT EXISTS idx_edge_run_id        ON edge(run_id);
CREATE INDEX IF NOT EXISTS idx_node_dtype          ON node(dtype);
CREATE INDEX IF NOT EXISTS idx_edge_confidence     ON edge(confidence);
"""

# History layer (spec §2.1). Snapshots are one compact projection per run and
# events are the append-only ledger; both survive reset_graph and are protected
# by triggers (events: no UPDATE/DELETE; snapshots: no DELETE — node_key is
# assigned once from the '' placeholder, which is a first write, not a rewrite).
_HISTORY_SCHEMA = """
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

EVENT_TIERS = ("observed", "asserted")


def init_db(path: str) -> sqlite3.Connection:
    """Create (if needed) and return a connection to the state-graph DB."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)  # tables only
    # Defensive: an older graph DB may predate columns added after its creation
    # (PSG-D2 run_id, PSG-D1 dtype/confidence). CREATE TABLE IF NOT EXISTS won't
    # add them, so ALTER if missing — BEFORE building indexes on those columns.
    _ensure_columns(conn, "node", {
        "run_id": "INTEGER", "dtype": "TEXT", "dtype_provenance": "TEXT",
        "data_class": "TEXT", "nullable": "INTEGER"})
    _ensure_columns(conn, "edge", {"run_id": "INTEGER", "confidence": "TEXT"})
    # History layer (spec §2.1): node_key identity + run attribution, then the
    # append-only tables and their triggers.
    _ensure_columns(conn, "node", {"node_key": "TEXT"})
    _ensure_columns(conn, "analysis_run", {"plan_id": "TEXT", "step_id": "TEXT", "trigger": "TEXT"})
    conn.executescript(_INDEXES)
    conn.executescript(_HISTORY_SCHEMA)
    conn.commit()
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, cols: dict) -> None:
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for col, decl in cols.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def reset_graph(conn: sqlite3.Connection) -> None:
    """Clear graph + card rows before a rebuild (PSG-C1).

    Re-running the analyzer over an existing DB previously DOUBLED the whole graph
    (add_node/add_edge are unconditional INSERTs). Wiping the graph and card tables
    first makes a rebuild idempotent. analysis_run history is intentionally kept,
    and so are node_snapshot / node_event (the history layer is append-only and
    never part of a rebuild). Order respects the node(id) foreign keys:
    cards + edge before node.
    """
    for table in ("consistency_card", "symbol_card", "edge", "node"):
        try:
            conn.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass  # card tables may not exist yet on a fresh DB
    conn.commit()


def stamp_run(conn: sqlite3.Connection, run_id: int) -> None:
    """Tag every graph row produced by this rebuild with its run_id (PSG-D2)."""
    conn.execute("UPDATE node SET run_id = ? WHERE run_id IS NULL", (run_id,))
    conn.execute("UPDATE edge SET run_id = ? WHERE run_id IS NULL", (run_id,))
    conn.commit()


def _get_or_create(conn: sqlite3.Connection, table: str, name: str) -> int:
    row = conn.execute(
        f"SELECT id FROM {table} WHERE name = ?", (name,)
    ).fetchone()
    if row is not None:
        return int(row[0])
    cur = conn.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
    conn.commit()
    return int(cur.lastrowid)


def get_or_create_node_type(conn: sqlite3.Connection, name: str) -> int:
    return _get_or_create(conn, "node_type", name)


def get_or_create_edge_type(conn: sqlite3.Connection, name: str) -> int:
    return _get_or_create(conn, "edge_type", name)


def add_node(
    conn: sqlite3.Connection,
    node_type_id: int,
    *,
    name: str,
    qualified_name: Optional[str] = None,
    file_path: Optional[str] = None,
    line_start: Optional[int] = None,
    line_end: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> int:
    md = metadata or {}
    # PSG-D1: mirror typed fields from metadata into first-class indexed columns
    # (keep metadata_json too for back-compat). Callers don't change.
    nullable = md.get("nullable")
    cur = conn.execute(
        """INSERT INTO node
           (node_type_id, name, qualified_name, file_path,
            line_start, line_end, metadata_json,
            dtype, dtype_provenance, data_class, nullable)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            node_type_id, name, qualified_name, file_path,
            line_start, line_end,
            json.dumps(metadata) if metadata is not None else None,
            md.get("dtype"), md.get("dtype_provenance"), md.get("data_class"),
            int(nullable) if isinstance(nullable, bool) else nullable,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def add_edge(
    conn: sqlite3.Connection,
    edge_type_id: int,
    src_node_id: int,
    dst_node_id: int,
    *,
    metadata: Optional[dict] = None,
) -> int:
    md = metadata or {}
    cur = conn.execute(
        """INSERT INTO edge
           (edge_type_id, src_node_id, dst_node_id, metadata_json, confidence)
           VALUES (?, ?, ?, ?, ?)""",
        (
            edge_type_id, src_node_id, dst_node_id,
            json.dumps(metadata) if metadata is not None else None,
            md.get("confidence"),  # PSG-D1: mirror into an indexed column
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def start_run(
    conn: sqlite3.Connection,
    *,
    project_name: str,
    commit_sha: Optional[str] = None,
    plan_id: Optional[str] = None,
    step_id: Optional[str] = None,
    trigger: str = "manual",
) -> int:
    """Open an analysis run. plan_id/step_id/trigger attribute the run to the
    orchestrator step that caused it (spec §2.5); trigger defaults to manual."""
    cur = conn.execute(
        """INSERT INTO analysis_run
           (project_name, commit_sha, started_at, tool_version, plan_id, step_id, trigger)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (project_name, commit_sha, _now(), TOOL_VERSION, plan_id, step_id, trigger),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute(
        "UPDATE analysis_run SET finished_at=? WHERE id=?",
        (_now(), run_id),
    )
    conn.commit()


# ── history layer (spec §2.1) ────────────────────────────────────────────────

def add_node_snapshot(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    node_type: str,
    qualified_name: str,
    file_path: Optional[str],
    line_start: Optional[int],
    line_end: Optional[int],
    struct_sig: Optional[str],
    dataflow_sig: Optional[str],
    dataflow_trivial: bool,
    attrs: dict,
) -> int:
    """Insert this run's projection of one node with the '' node_key placeholder;
    the key is assigned exactly once later by set_snapshot_key."""
    cur = conn.execute(
        """INSERT INTO node_snapshot
           (run_id, node_key, node_type, qualified_name, file_path, line_start, line_end,
            struct_sig, dataflow_sig, dataflow_trivial, attrs_json)
           VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, node_type, qualified_name, file_path, line_start, line_end,
         struct_sig, dataflow_sig, 1 if dataflow_trivial else 0, json.dumps(attrs, sort_keys=True)),
    )
    return int(cur.lastrowid)


def set_snapshot_key(conn: sqlite3.Connection, snapshot_id: int, node_key: str) -> None:
    """First (and only) assignment of a snapshot's node_key: only the ''
    placeholder may be written. Re-assignment raises — history is not rewritten."""
    if not node_key:
        raise ValueError("node_key must be non-empty")
    cur = conn.execute(
        "UPDATE node_snapshot SET node_key=? WHERE id=? AND node_key=''",
        (node_key, snapshot_id),
    )
    if cur.rowcount != 1:
        raise ValueError(f"snapshot {snapshot_id}: node_key already assigned or row missing")


def add_node_event(
    conn: sqlite3.Connection,
    run_id: int,
    event_type: str,
    node_key: Optional[str],
    payload: dict,
    tier: str = "observed",
) -> int:
    """Append one event; seq is per-run and monotonic. tier ∈ EVENT_TIERS."""
    if tier not in EVENT_TIERS:
        raise ValueError(f"tier must be one of {EVENT_TIERS}, got {tier!r}")
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM node_event WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    cur = conn.execute(
        """INSERT INTO node_event (run_id, seq, event_type, node_key, tier, payload_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (run_id, seq, event_type, node_key, tier, json.dumps(payload, sort_keys=True), _now()),
    )
    return int(cur.lastrowid)


def latest_run_id(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute("SELECT MAX(id) FROM analysis_run").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def previous_run_id(conn: sqlite3.Connection, run_id: int) -> Optional[int]:
    """The most recent earlier run of the same project that has snapshot rows
    (runs that never snapshotted — interrupted, or pre-history — are skipped)."""
    row = conn.execute(
        """SELECT MAX(a.id) FROM analysis_run a
           WHERE a.id < ? AND a.project_name = (SELECT project_name FROM analysis_run WHERE id=?)
             AND EXISTS (SELECT 1 FROM node_snapshot s WHERE s.run_id = a.id)""",
        (run_id, run_id),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None

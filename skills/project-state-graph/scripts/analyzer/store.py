"""SQLite store for the project state-graph. Sole owner of DB access.

Schema (generic graph):
    node_type    (id, name UNIQUE, description)
    node         (id, node_type_id, name, qualified_name, file_path,
                  line_start, line_end, metadata_json)
    edge_type    (id, name UNIQUE, description)
    edge         (id, edge_type_id, src_node_id, dst_node_id, metadata_json)
    analysis_run (id, project_name, commit_sha, started_at, finished_at, tool_version)
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
    metadata_json  TEXT
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
    metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS analysis_run (
    id           INTEGER PRIMARY KEY,
    project_name TEXT NOT NULL,
    commit_sha   TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    tool_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_node_node_type_id ON node(node_type_id);
CREATE INDEX IF NOT EXISTS idx_node_file_path     ON node(file_path);
CREATE INDEX IF NOT EXISTS idx_edge_src_node_id   ON edge(src_node_id);
CREATE INDEX IF NOT EXISTS idx_edge_dst_node_id   ON edge(dst_node_id);
CREATE INDEX IF NOT EXISTS idx_edge_edge_type_id  ON edge(edge_type_id);
"""


def init_db(path: str) -> sqlite3.Connection:
    """Create (if needed) and return a connection to the state-graph DB."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


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
    cur = conn.execute(
        """INSERT INTO node
           (node_type_id, name, qualified_name, file_path,
            line_start, line_end, metadata_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            node_type_id, name, qualified_name, file_path,
            line_start, line_end,
            json.dumps(metadata) if metadata is not None else None,
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
    cur = conn.execute(
        """INSERT INTO edge
           (edge_type_id, src_node_id, dst_node_id, metadata_json)
           VALUES (?, ?, ?, ?)""",
        (
            edge_type_id, src_node_id, dst_node_id,
            json.dumps(metadata) if metadata is not None else None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def start_run(
    conn: sqlite3.Connection,
    *,
    project_name: str,
    commit_sha: Optional[str] = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO analysis_run
           (project_name, commit_sha, started_at, tool_version)
           VALUES (?, ?, ?, ?)""",
        (project_name, commit_sha, _dt.datetime.now(_dt.timezone.utc).isoformat(), TOOL_VERSION),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute(
        "UPDATE analysis_run SET finished_at=? WHERE id=?",
        (_dt.datetime.now(_dt.timezone.utc).isoformat(), run_id),
    )
    conn.commit()

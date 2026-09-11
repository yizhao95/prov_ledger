"""psg_bridge — the orchestrator's READ-ONLY window into a project's state graph.

The analyzer (skills/project-state-graph) owns the graph and its history
tables (node_snapshot / node_event, see analyzer/store.py::_HISTORY_SCHEMA);
the backend only ever reads them, through file:…?mode=ro connections, and
never imports the analyzer. Every function degrades to None / [] when the
graph is missing, unregistered or built before the history layer existed —
"state graph unavailable" is a visible outcome, never an exception.
"""
from __future__ import annotations

import json
import os
import sqlite3

DEFAULT_REGISTRY_PATH = os.path.expanduser("~/skill-workspace/project-graphs/projects.json")

# Events that make a node a reason slot at close time (spec §2.8). node_changed
# counts only when the body changed (payload.changed contains 'struct_sig'):
# a dataflow-only change means a callee was renamed — nothing the author DID
# to this node (#37 review).
CHANGED_EVENTS = ("node_changed", "node_added", "node_removed")


def db_path_for(project: str, registry_path: str | None = None) -> str | None:
    """projects.json → db_path of `project`; None when the registry or the
    project is missing (the caller decides how loudly to say so)."""
    path = registry_path or os.environ.get("PSG_REGISTRY_PATH", DEFAULT_REGISTRY_PATH)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            reg = json.load(f)
    except (OSError, ValueError):
        return None
    for p in (reg.get("projects", []) if isinstance(reg, dict) else []):
        if p.get("name") == project:
            return p.get("db_path") or None
    return None


def open_ro(psg_db_path: str) -> sqlite3.Connection:
    """Read-only connection (mode=ro): the backend cannot write the graph."""
    conn = sqlite3.connect(f"file:{psg_db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _query(psg_db_path: str | None, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Run one read; [] when the file is absent or the schema predates history."""
    if not psg_db_path or not os.path.exists(psg_db_path):
        return []
    try:
        conn = open_ro(psg_db_path)
    except sqlite3.Error:
        return []
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:   # no such table / column: pre-phase-2 graph
        return []
    finally:
        conn.close()


def changed_node_keys(psg_db_path: str | None, plan_id: str) -> list[dict]:
    """Nodes this plan changed, added or removed, one dict per node_key:
    {node_key, qualified_name, node_type, event_types: [...], run_id}. The
    qualified_name/node_type are the node's latest snapshot values."""
    rows = _query(psg_db_path, """
        SELECT e.node_key, e.event_type, e.run_id, e.payload_json,
               (SELECT qualified_name FROM node_snapshot s WHERE s.node_key = e.node_key
                 ORDER BY s.run_id DESC, s.id DESC LIMIT 1) AS qn,
               (SELECT node_type FROM node_snapshot s WHERE s.node_key = e.node_key
                 ORDER BY s.run_id DESC, s.id DESC LIMIT 1) AS nt
        FROM node_event e JOIN analysis_run a ON a.id = e.run_id
        WHERE a.plan_id = ? AND e.event_type IN (?, ?, ?) AND e.node_key IS NOT NULL
        ORDER BY e.node_key, e.run_id, e.seq""", (plan_id, *CHANGED_EVENTS))
    out: dict[str, dict] = {}
    for r in rows:
        if r["event_type"] == "node_changed":
            try:
                changed = json.loads(r["payload_json"] or "{}").get("changed", [])
            except ValueError:
                changed = []
            if "struct_sig" not in changed:
                continue
        d = out.setdefault(r["node_key"], {"node_key": r["node_key"], "qualified_name": r["qn"],
                                           "node_type": r["nt"], "event_types": [], "run_id": r["run_id"]})
        if r["event_type"] not in d["event_types"]:
            d["event_types"].append(r["event_type"])
        d["run_id"] = max(d["run_id"], r["run_id"])
    return list(out.values())


def node_key_of(psg_db_path: str | None, qualified_name: str) -> str | None:
    """The node_key behind a qualified name (latest snapshot carrying it), so a
    reason recorded under an old name still resolves after a rename."""
    rows = _query(psg_db_path,
                  "SELECT node_key FROM node_snapshot WHERE qualified_name = ? AND node_key <> '' "
                  "ORDER BY run_id DESC, id DESC LIMIT 1", (qualified_name,))
    return rows[0]["node_key"] if rows else None


def events_of(psg_db_path: str | None, node_key: str) -> list[dict]:
    """All events of one node with run attribution — same shape as
    analyzer.history.events_of, read-only."""
    rows = _query(psg_db_path, """
        SELECT e.id, e.run_id, e.seq, e.event_type, e.tier, e.payload_json, e.created_at,
               a.commit_sha, a.plan_id, a.step_id, a.trigger
        FROM node_event e JOIN analysis_run a ON a.id = e.run_id
        WHERE e.node_key = ? ORDER BY e.run_id, e.seq""", (node_key,))
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except ValueError:
            payload = {}
        out.append({"event_id": r["id"], "run_id": r["run_id"], "seq": r["seq"], "event_type": r["event_type"],
                    "tier": r["tier"], "payload": payload, "created_at": r["created_at"],
                    "commit_sha": r["commit_sha"], "plan_id": r["plan_id"], "step_id": r["step_id"],
                    "trigger": r["trigger"]})
    return out


def latest_run_id(psg_db_path: str | None, plan_id: str | None = None) -> int | None:
    if plan_id is None:
        rows = _query(psg_db_path, "SELECT MAX(id) AS m FROM analysis_run")
    else:
        rows = _query(psg_db_path, "SELECT MAX(id) AS m FROM analysis_run WHERE plan_id = ?", (plan_id,))
    return int(rows[0]["m"]) if rows and rows[0]["m"] is not None else None


def output_consumers(psg_db_path: str | None, qualified_name: str) -> list[str]:
    """The consistency card's output_consumers of a symbol (who eats its return
    value) — the input survival needs to tell untouched_consumed from untouched."""
    rows = _query(psg_db_path, """
        SELECT cc.card_json FROM consistency_card cc JOIN node n ON n.id = cc.symbol_id
        WHERE n.qualified_name = ? ORDER BY n.id DESC LIMIT 1""", (qualified_name,))
    if not rows or not rows[0]["card_json"]:
        return []
    try:
        return list(json.loads(rows[0]["card_json"]).get("output_consumers", []))
    except ValueError:
        return []

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


def registered_sha_for(project: str, registry_path: str | None = None) -> str | None:
    """projects.json -> commit_sha the project's graph was built at."""
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
            return p.get("commit_sha") or None
    return None


def repo_for(project: str, registry_path: str | None = None) -> str | None:
    """projects.json -> repo path of `project` (the extensions file lives there)."""
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
            return p.get("repo") or None
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


def card_of(psg_db_path: str | None, qualified_name: str) -> dict:
    """The consistency card of a symbol (callers / callees / output_consumers /
    reads / writes / dtype_map …) — the SPACE dimension of a node ledger
    (phase 8, FL-009). {} when the graph, the symbol or the card is missing."""
    rows = _query(psg_db_path, """
        SELECT cc.card_json FROM consistency_card cc JOIN node n ON n.id = cc.symbol_id
        WHERE n.qualified_name = ? ORDER BY n.id DESC LIMIT 1""", (qualified_name,))
    if not rows or not rows[0]["card_json"]:
        return {}
    try:
        card = json.loads(rows[0]["card_json"])
    except ValueError:
        return {}
    return card if isinstance(card, dict) else {}


def latest_qualified_name(psg_db_path: str | None, node_key: str) -> str | None:
    """The most recent qualified name carried by a node_key."""
    rows = _query(psg_db_path,
                  "SELECT qualified_name FROM node_snapshot WHERE node_key = ? ORDER BY run_id DESC, id DESC LIMIT 1", (node_key,))
    return rows[0]["qualified_name"] if rows else None


def project_for_cwd(cwd: str | None, registry_path: str | None = None) -> str | None:
    """The registered project whose repo contains `cwd` (longest repo prefix
    wins); None when cwd is empty or outside every registered repo."""
    if not cwd:
        return None
    path = registry_path or os.environ.get("PSG_REGISTRY_PATH", DEFAULT_REGISTRY_PATH)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            reg = json.load(f)
    except (OSError, ValueError):
        return None
    cwd = os.path.abspath(cwd).rstrip("/") or "/"
    best: tuple[int, str] | None = None
    for p in (reg.get("projects", []) if isinstance(reg, dict) else []):
        repo = (p.get("repo") or "").rstrip("/")
        name = p.get("name")
        if not repo or not name:
            continue
        if cwd == repo or cwd.startswith(repo + "/"):
            if best is None or len(repo) > best[0]:
                best = (len(repo), name)
    return best[1] if best else None


def nodes_at(psg_db_path: str | None, file_path: str, line_lo: int, line_hi: int) -> list[dict]:
    """DP phase 2: the nodes whose latest snapshot covers [line_lo, line_hi]
    of file_path (repo-relative or absolute — matched by path suffix),
    innermost first. Used by `provledger why file:line` and the PreToolUse
    hook; read-only, one query."""
    if not psg_db_path or not file_path:
        return []
    norm = file_path.replace("\\", "/")
    rows = _query(psg_db_path,
                  "SELECT s.node_key, s.qualified_name, s.node_type, s.file_path, s.line_start, s.line_end "
                  "FROM node_snapshot s WHERE s.node_key <> '' AND s.file_path IS NOT NULL "
                  "AND s.run_id = (SELECT MAX(run_id) FROM node_snapshot x WHERE x.node_key = s.node_key) "
                  "AND s.line_start IS NOT NULL AND s.line_end IS NOT NULL "
                  "AND s.line_start <= ? AND s.line_end >= ?", (line_hi, line_lo))
    out = []
    for r in rows:
        fp = (r["file_path"] or "").replace("\\", "/")
        if fp and (fp == norm or norm.endswith("/" + fp) or fp.endswith("/" + norm)):
            out.append({"node_key": r["node_key"], "qualified_name": r["qualified_name"], "node_type": r["node_type"],
                        "file_path": r["file_path"], "line_start": r["line_start"], "line_end": r["line_end"]})
    out.sort(key=lambda n: (n["line_end"] - n["line_start"], n["line_start"]))
    return out


def changed_in_file(psg_db_path: str | None, plan_id: str, file_path: str) -> list[dict]:
    """DP phase 2b (R0, FL-066): the nodes this plan changed / added / removed
    whose latest snapshot lives in `file_path` (a repo-relative path or just a
    basename — matched by suffix). The file name in the user's words anchors
    to exactly these, however many."""
    if not file_path:
        return []
    norm = file_path.replace("\\", "/")
    out = []
    for c in changed_node_keys(psg_db_path, plan_id):
        row = _query(psg_db_path, "SELECT file_path FROM node_snapshot WHERE node_key = ? ORDER BY run_id DESC, id DESC LIMIT 1", (c["node_key"],))
        fp = (row[0]["file_path"] or "").replace("\\", "/") if row else ""
        if fp and (fp == norm or fp.endswith("/" + norm)):
            out.append({"node_key": c["node_key"], "qualified_name": c["qualified_name"], "file_path": fp})
    out.sort(key=lambda n: n["node_key"])
    return out


# ── DP phase 2b (Task 3): the graph as it is, or as it was at a run ──────────

APP_FUNC_TYPES = ("function", "method", "route")
FLOW_EDGES = ("calls", "downstream_data_feed")


def runs_of(psg_db_path: str | None) -> list[dict]:
    """Every analysis run, newest first: {run_id, commit_sha, plan_id, started_at, trigger}."""
    return [{"run_id": r["id"], "commit_sha": r["commit_sha"], "plan_id": r["plan_id"], "started_at": r["started_at"], "trigger": r["trigger"]}
            for r in _query(psg_db_path, "SELECT id, commit_sha, plan_id, started_at, trigger FROM analysis_run ORDER BY id DESC")]


def graph_at(psg_db_path: str | None, run_id: int | None = None, level: str = "functions") -> dict:
    """{nodes: [{node_key, qualified_name, node_type, file_path}], edges: [{src_key, dst_key, edge_type}],
    run_id, edges_from, level}. Nodes come from node_snapshot of `run_id` (the
    newest run when None). Edges have no run dimension in the graph — they are
    the latest graph's, mapped through node_key, and the result says so
    (`edges_from: 'latest'`), never silently. level=functions keeps
    function / method / route and calls / downstream_data_feed; full keeps all."""
    if not psg_db_path:
        return {"nodes": [], "edges": [], "run_id": None, "edges_from": "none", "level": level}
    latest = _query(psg_db_path, "SELECT MAX(run_id) AS r FROM node_snapshot")
    latest_run = latest[0]["r"] if latest and latest[0]["r"] is not None else None
    run = int(run_id) if run_id is not None else latest_run
    if run is None:
        return {"nodes": [], "edges": [], "run_id": None, "edges_from": "none", "level": level}
    type_filter = f"AND node_type IN ({','.join('?' * len(APP_FUNC_TYPES))})" if level == "functions" else ""
    params: tuple = (run, *APP_FUNC_TYPES) if level == "functions" else (run,)
    rows = _query(psg_db_path, f"SELECT node_key, qualified_name, node_type, file_path FROM node_snapshot WHERE run_id = ? AND node_key <> '' {type_filter} ORDER BY qualified_name", params)
    nodes = [{"node_key": r["node_key"], "qualified_name": r["qualified_name"], "node_type": r["node_type"], "file_path": r["file_path"]} for r in rows]
    keep = {n["node_key"] for n in nodes}
    edge_filter = f"AND t.name IN ({','.join('?' * len(FLOW_EDGES))})" if level == "functions" else ""
    erows = _query(psg_db_path,
                   f"SELECT s.node_key AS src_key, d.node_key AS dst_key, t.name AS edge_type FROM edge e "
                   f"JOIN edge_type t ON t.id = e.edge_type_id JOIN node s ON s.id = e.src_node_id JOIN node d ON d.id = e.dst_node_id "
                   f"WHERE s.node_key IS NOT NULL AND d.node_key IS NOT NULL {edge_filter}", FLOW_EDGES if level == "functions" else ())
    edges = [{"src_key": r["src_key"], "dst_key": r["dst_key"], "edge_type": r["edge_type"]} for r in erows if r["src_key"] in keep and r["dst_key"] in keep]
    return {"nodes": nodes, "edges": edges, "run_id": run, "edges_from": "latest" if run != latest_run else "run",
            "latest_run_id": latest_run, "level": level}


def latest_tier_of(psg_db_path: str | None, run_id: int | None = None) -> dict[str, str]:
    """node_key → tier of its latest event (at or before run_id) — the colour rule of the views."""
    if not psg_db_path:
        return {}
    sql = "SELECT node_key, tier FROM node_event WHERE node_key IS NOT NULL"
    params: tuple = ()
    if run_id is not None:
        sql += " AND run_id <= ?"; params = (int(run_id),)
    sql += " ORDER BY run_id, seq"
    out: dict[str, str] = {}
    for r in _query(psg_db_path, sql, params):
        out[r["node_key"]] = r["tier"]
    return out

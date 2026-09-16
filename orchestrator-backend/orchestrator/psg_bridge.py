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


# DP phase 2d (Task 0, FL-076): a whole project is too much graph for one page.
# User feedback on the 2b screenshots (2026-09-16): 记全是对的，画全是错的 — recording
# every node is right, DRAWING every node is wrong. So the reader has four modes
# and the biggest one is never what a page asks for by default:
#
#   focus — the focus node's ±hops neighbourhood
#   story — the nodes that have a story (the caller passes their keys, since the
#           badge lives in the orchestrator DB, not here) plus the data nodes
#           they directly produce or consume
#   data  — every data node plus the functions that directly produce or consume it
#   full  — everything at `level` (the 2b behaviour, and this reader's default:
#           the UX default belongs to the view, not to the bridge)
GRAPH_MODES = ("focus", "story", "data", "full")
DATA_TYPES = ("dataset", "column", "sql_table", "api_source", "bq_dataset", "data_var")
NEIGHBOURHOOD_HOPS = 2
NEIGHBOURHOOD_MAX_NODES = 400
# Past this many selected nodes the view CLUSTERS by module instead of dropping
# nodes. The previous flat cap of 200 silently discarded three quarters of a
# 1601-node selection, which is not a reduced view but a wrong one: the count in
# the corner said 1601 and the picture showed 200.
CLUSTER_ABOVE = 600
# level 0 of the data-flow layout: where data comes from
SOURCE_TYPES = ("sql_table", "bq_dataset", "api_source", "dataset", "file")
# story / data pick their nodes from the whole project rather than from one
# neighbourhood, so they need their own, tighter cap: on prov_ledger 1163 nodes
# carry a story and an uncapped story page is 2 MB. The cut is reported as
# `mode_total` (what the mode selected) beside `total_nodes` (the whole graph).
MODE_MAX_NODES = 200


def subsystem_of(file_path: str | None) -> str:
    """Top two path segments → module key. No path → '(external)'.

    A deliberate copy of skills/update-project-state-graph/scripts/graph_viz.py
    ::subsystem_of — the backend never imports the analyzer (module docstring),
    and this is four lines of convention, not logic worth a dependency."""
    if not file_path:
        return "(external)"
    parts = file_path.replace("\\", "/").lstrip("./").split("/")
    if len(parts) >= 3:
        return parts[0] + "/" + parts[1]
    if len(parts) == 2:
        return parts[0]
    return "(root)"


def _adjacency(edges: list[dict]) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {}
    for e in edges:
        adj.setdefault(e["src_key"], set()).add(e["dst_key"])
        adj.setdefault(e["dst_key"], set()).add(e["src_key"])
    return adj


def _neighbourhood(nodes: list[dict], edges: list[dict], focus: str,
                   hops: int, max_nodes: int) -> tuple[set[str] | None, str | None, bool]:
    """(keys to keep, the focus node_key, truncated) — (None, None, False) when
    `focus` names nothing in this graph, so the caller can show the whole thing
    and SAY the focus was not found rather than render an empty canvas."""
    by_key = {n["node_key"]: n for n in nodes}
    key = focus if focus in by_key else next((n["node_key"] for n in nodes if n["qualified_name"] == focus), None)
    if key is None:
        return None, None, False
    adj = _adjacency(edges)
    seen, frontier, truncated = {key}, {key}, False
    for _ in range(max(0, int(hops))):
        nxt = {k for f in frontier for k in adj.get(f, ()) if k not in seen and k in by_key}
        if not nxt:
            break
        room = max_nodes - len(seen)
        if len(nxt) > room:
            nxt = set(sorted(nxt, key=lambda k: by_key[k]["qualified_name"])[:room])
            truncated = True
        seen |= nxt
        frontier = nxt
        if truncated:
            break
    return seen, key, truncated or len(seen) < len(nodes)


def graph_at(psg_db_path: str | None, run_id: int | None = None, level: str = "functions",
             mode: str = "full", focus: str | None = None, hops: int = NEIGHBOURHOOD_HOPS,
             story_keys=None, max_nodes: int | None = None,
             cluster_above: int = CLUSTER_ABOVE) -> dict:
    """{nodes: [{node_key, qualified_name, node_type, file_path}], edges: [{src_key, dst_key, edge_type}],
    run_id, edges_from, level, mode, total_nodes, mode_total, truncated, focus_key, focus_found, hops}.

    Nodes come from node_snapshot of `run_id` (the newest run when None). Edges
    have no run dimension in the graph — they are the latest graph's, mapped
    through node_key, and the result says so (`edges_from: 'latest'`), never
    silently. `level` filters node TYPES (functions keeps function / method /
    route and calls / downstream_data_feed; full keeps all); `mode` decides how
    much of that graph is drawn (see GRAPH_MODES). `total_nodes` is always the
    full count at `level` and `mode_total` what the mode picked before the cap,
    so a cropped view can always say exactly what it cropped."""
    if not psg_db_path:
        return {"nodes": [], "edges": [], "run_id": None, "edges_from": "none", "level": level}
    latest = _query(psg_db_path, "SELECT MAX(run_id) AS r FROM node_snapshot")
    latest_run = latest[0]["r"] if latest and latest[0]["r"] is not None else None
    run = int(run_id) if run_id is not None else latest_run
    if run is None:
        return {"nodes": [], "edges": [], "run_id": None, "edges_from": "none", "level": level}
    mode = mode if mode in GRAPH_MODES else "full"
    if max_nodes is None:
        max_nodes = NEIGHBOURHOOD_MAX_NODES
    rows = _query(psg_db_path, "SELECT node_key, qualified_name, node_type, file_path FROM node_snapshot "
                               "WHERE run_id = ? AND node_key <> '' ORDER BY qualified_name", (run,))
    every = [{"node_key": r["node_key"], "qualified_name": r["qualified_name"], "node_type": r["node_type"],
              "file_path": r["file_path"]} for r in rows]
    at_level = [n for n in every if level != "functions" or n["node_type"] in APP_FUNC_TYPES]
    total_nodes = len(at_level)
    erows = _query(psg_db_path,
                   "SELECT s.node_key AS src_key, d.node_key AS dst_key, t.name AS edge_type FROM edge e "
                   "JOIN edge_type t ON t.id = e.edge_type_id JOIN node s ON s.id = e.src_node_id JOIN node d ON d.id = e.dst_node_id "
                   "WHERE s.node_key IS NOT NULL AND d.node_key IS NOT NULL")
    all_edges = [{"src_key": r["src_key"], "dst_key": r["dst_key"], "edge_type": r["edge_type"]} for r in erows]

    focus_key, focus_found, truncated = None, False, False
    if mode in ("story", "data"):
        # story / data reason over every node type and every edge type: a data node
        # is invisible at level=functions, and `reads_sql` is not a flow edge.
        nodes, pool_edges = every, all_edges
        present = {n["node_key"] for n in nodes}
        edges = [e for e in pool_edges if e["src_key"] in present and e["dst_key"] in present]
        adj = _adjacency(edges)
        data_keys = {n["node_key"] for n in nodes if n["node_type"] in DATA_TYPES}
        if mode == "story":
            # story_keys may be an ORDERED sequence (queries sorts it by badge,
            # most-storied first) so the cap keeps the nodes with the most to say
            ranked = list(dict.fromkeys(story_keys or ()))
            rank = {k: i for i, k in enumerate(ranked)}
            wanted = set(ranked)
            seeds = [n["node_key"] for n in nodes if n["node_key"] in wanted or n["qualified_name"] in wanted]
            seeds.sort(key=lambda k: rank.get(k, len(rank)))
            order = {k: i for i, k in enumerate(seeds)}
            neighbours = [k for s in seeds for k in sorted(adj.get(s, ())) if k in data_keys]
            for k in dict.fromkeys(neighbours):
                order.setdefault(k, len(order))
            keep = set(seeds) | set(neighbours)
        else:
            # data nodes first, the functions that touch them after: a cap eats the tail
            ordered_data = sorted(data_keys)
            order = {k: i for i, k in enumerate(ordered_data)}
            touching = [k for d in ordered_data for k in sorted(adj.get(d, ())) if k not in data_keys]
            for k in dict.fromkeys(touching):
                order.setdefault(k, len(order))
            keep = data_keys | set(touching)
        nodes = [n for n in nodes if n["node_key"] in keep]
        mode_total = len(nodes)
        edges = [e for e in edges if e["src_key"] in keep and e["dst_key"] in keep]
        truncated = len(nodes) < total_nodes
    else:
        nodes = at_level
        mode_total = len(nodes)
        keep = {n["node_key"] for n in nodes}
        edge_types = FLOW_EDGES if level == "functions" else None
        edges = [e for e in all_edges if e["src_key"] in keep and e["dst_key"] in keep
                 and (edge_types is None or e["edge_type"] in edge_types)]
        if mode == "focus" and focus:
            neigh, focus_key, truncated = _neighbourhood(nodes, edges, focus, hops, max_nodes)
            focus_found = neigh is not None
            if neigh is not None:
                nodes = [n for n in nodes if n["node_key"] in neigh]
                edges = [e for e in edges if e["src_key"] in neigh and e["dst_key"] in neigh]
                mode_total = len(nodes)
    # Past the threshold the picture is clustered by module, never cropped: the
    # clusters' node counts add up to exactly what the mode selected.
    layout = "physics" if mode == "full" else "hierarchical"
    _assign_levels(nodes, edges, focus_key if mode == "focus" else None)
    clusters = _cluster_by_module(nodes, edges) if len(nodes) > cluster_above else []
    return {"nodes": nodes, "edges": edges, "run_id": run, "edges_from": "latest" if run != latest_run else "run",
            "latest_run_id": latest_run, "level": level, "mode": mode, "focus": focus or None,
            "focus_key": focus_key, "focus_found": focus_found, "hops": hops,
            "total_nodes": total_nodes, "mode_total": mode_total, "truncated": truncated,
            "clusters": clusters, "layout": layout}


def _cluster_by_module(nodes: list[dict], edges: list[dict] | None = None) -> list[dict]:
    """Group the drawn nodes by module (the top two path segments) so a large
    selection renders as a handful of expandable bubbles. Every node belongs to
    exactly one cluster, so the totals still reconcile."""
    buckets: dict[str, list[dict]] = {}
    for n in nodes:
        buckets.setdefault(subsystem_of(n.get("file_path")), []).append(n)
    out = [{"key": k, "label": k, "nodes": len(v), "members": [x["node_key"] for x in v],
            "badged": sum(1 for x in v if x.get("badge")), "level": 0}
           for k, v in sorted(buckets.items())]
    # A cluster's layer is its depth in the MODULE-to-module flow. Taking the
    # minimum member level put every module on row 0, because nearly every module
    # contains something that reads a source — one row is not a layered picture.
    of = {m: c["key"] for c in out for m in c["members"]}
    proj = [{"src_key": of[e["src_key"]], "dst_key": of[e["dst_key"]]}
            for e in (edges or []) if e["src_key"] in of and e["dst_key"] in of
            and of[e["src_key"]] != of[e["dst_key"]]]
    stand = [{"node_key": c["key"], "node_type": "module"} for c in out]
    _assign_levels(stand, proj)
    lv = {n["node_key"]: n["level"] for n in stand}
    for c in out:
        c["level"] = lv.get(c["key"], 0)
    return out


def _assign_levels(nodes: list[dict], edges: list[dict], focus_key: str | None = None) -> None:
    """Give every node a LAYER, so the picture reads top to bottom.

    The first version bucketed by node_type, which put every function on level 1
    and drew 56 nodes on one horizontal line — a hierarchical layout with one
    level is just a list. A layer has to be a depth:

      with a focus  signed BFS distance from it. Callers are negative (above),
                    the focus is 0, callees positive (below), so the thing you
                    asked about sits in the middle and the flow runs through it.
      otherwise     topological depth: a node with no incoming edge is 0, every
                    other node is max(level of its predecessors) + 1. Cycles are
                    collapsed to their entry depth rather than looping forever —
                    a call cycle is real and must not hang the page.
    """
    by_key = {n["node_key"]: n for n in nodes}
    out: dict[str, set[str]] = {}
    inc: dict[str, set[str]] = {}
    for e in edges:
        a, b = e["src_key"], e["dst_key"]
        if a not in by_key or b not in by_key or a == b:
            continue
        # A read edge points AT the table (`f3 --reads_sql--> orders`) but the
        # data flows the other way. For layering, reverse it: a source a function
        # reads is upstream of that function, which is the whole point of the
        # data-flow picture.
        if by_key[b].get("node_type") in SOURCE_TYPES and by_key[a].get("node_type") not in SOURCE_TYPES:
            a, b = b, a
        out.setdefault(a, set()).add(b)
        inc.setdefault(b, set()).add(a)

    if focus_key and focus_key in by_key:
        level = {focus_key: 0}
        for adj, sign in ((out, 1), (inc, -1)):
            frontier, depth = {focus_key}, 0
            while frontier:
                depth += sign
                nxt = {k for f in frontier for k in adj.get(f, ()) if k not in level}
                for k in nxt:
                    level[k] = depth
                frontier = nxt
        for k, n in by_key.items():
            n["level"] = level.get(k, 0)
        return

    # Kahn's algorithm, with the remainder (the cycles) placed at the depth they
    # were reached from instead of being dropped
    remaining = {k: set(inc.get(k, set())) for k in by_key}
    level = {k: 0 for k in by_key if not remaining[k]}
    queue = list(level)
    while queue:
        k = queue.pop(0)
        for nxt in out.get(k, ()):
            remaining[nxt].discard(k)
            level[nxt] = max(level.get(nxt, 0), level[k] + 1)
            if not remaining[nxt] and nxt not in queue and nxt not in level.keys() - set(queue):
                queue.append(nxt)
            elif not remaining[nxt] and nxt not in queue:
                queue.append(nxt)
    for k in by_key:
        if k not in level:                      # inside a cycle: one below its shallowest entry
            preds = [level[p] for p in inc.get(k, ()) if p in level]
            level[k] = (min(preds) + 1) if preds else 0
    for k, n in by_key.items():
        n["level"] = int(level.get(k, 0))


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

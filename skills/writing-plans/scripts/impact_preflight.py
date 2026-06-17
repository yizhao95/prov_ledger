#!/usr/bin/env python3
"""impact_preflight.py — provLedger Phase D plan-time forward impact analysis.

Computes an ``impact_context`` from a project's state-graph BEFORE a plan is
published, so the LLM authors the plan *with the blast radius in hand* and the
orchestrator stores the analysis on the Plan row. "Publish a plan" and "do
impact analysis" become one atomic act (enforced in publish_plan.py).

The flow (union + per-symbol graph verification):
  1. collect_targets   — union of declared_targets (LLM) + keyword reverse-lookup
                         of the raw user_query against node.name/qualified_name.
  2. verify_symbol     — existing -> callers/output_consumers/dtype_map/
                         lineage_downstream; missing -> status 'new'.
  3. upstream_assumptions — surface sql_table/api_source assumed_schema (A-4) as
                         unverified upstream + a fail-fast recommendation.
  4. ledger_matches    — Phase E: deterministic fuzzy-match of recorded
                         decisions/anti-patterns surfaced as reminders.
  5. compute_impact_context — assemble + a capability_boundary note.

Stdlib only (sqlite3 + json + re); imports nothing from the orchestrator package.

Capability boundary: strongest for MODIFYING EXISTING code. For a brand-new
module it degrades to "here is what the existing functions you intend to call
look like" — the graph only describes code that exists.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

CALLABLE_TYPES = ("function", "method", "route")
SQL_SOURCE_TYPES = ("sql_table", "api_source")

# Small stopword set — keep deterministic + dependency-free. Tokens shorter than
# 3 chars are dropped regardless.
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "but", "not",
    "you", "are", "was", "all", "any", "can", "has", "have", "will", "please",
    "function", "method", "step", "code", "change", "update", "add", "fix",
    "refactor", "touch",
}
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _meta(raw) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}


# ── 1 · collect targets (union of two routes) ────────────────────────────────────

def _all_symbol_names(conn) -> Dict[str, str]:
    """Map every name AND qualified_name -> qualified_name (for reverse lookup)."""
    out: Dict[str, str] = {}
    for r in conn.execute(
        """SELECT n.name, n.qualified_name FROM node n
           JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('function','method','route','class','data_var')"""
    ).fetchall():
        q = r["qualified_name"] or r["name"]
        if r["name"]:
            out.setdefault(r["name"], q)
        if r["qualified_name"]:
            out.setdefault(r["qualified_name"], q)
    return out


def collect_targets(user_query: str, declared_targets: Optional[List[str]],
                    db_path: str) -> Dict:
    """Union the LLM-declared targets with a deterministic keyword reverse-lookup.

    Returns ``{targets: [{name, route:[...]}]}`` deduped by name, each recording
    which route(s) found it ('declared', 'keyword').
    """
    declared = list(declared_targets or [])
    routes: Dict[str, set] = {}
    for name in declared:
        if name:
            routes.setdefault(name, set()).add("declared")

    conn = _connect(db_path)
    try:
        known = _all_symbol_names(conn)
    finally:
        conn.close()

    seen_tokens = set()
    for tok in _TOKEN_RE.findall(user_query or ""):
        low = tok.lower()
        if low in _STOPWORDS or low in seen_tokens:
            continue
        seen_tokens.add(low)
        # exact name / qualified_name match against the graph
        if tok in known:
            routes.setdefault(tok, set()).add("keyword")

    targets = [{"name": n, "route": sorted(routes[n])} for n in sorted(routes)]
    return {"targets": targets}


# ── 2 · per-symbol graph verification ────────────────────────────────────────────

def _resolve_node(conn, name: str):
    return conn.execute(
        """SELECT n.id, n.name, n.qualified_name, n.file_path
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('function','method','route','class')
             AND (n.qualified_name = ? OR n.name = ?)
           LIMIT 1""", (name, name)).fetchone()


def verify_symbol(conn, name: str) -> Dict:
    """Verify one candidate against the graph.

    Existing -> {status:'existing', callers, output_consumers, dtype_map,
    lineage_downstream}. Missing -> {status:'new', empty lists} (a declared
    symbol the graph can't find is itself a useful signal: new, or misremembered).
    """
    node = _resolve_node(conn, name)
    if node is None:
        return {"name": name, "status": "new", "callers": [],
                "output_consumers": [], "dtype_map": {}, "lineage_downstream": []}

    nid = node["id"]
    # callers: incoming calls edges
    callers = sorted({r["q"] for r in conn.execute(
        """SELECT COALESCE(s.qualified_name, s.name) AS q
           FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
           JOIN node s ON s.id=e.src_node_id
           WHERE t.name='calls' AND e.dst_node_id=?""", (nid,)).fetchall()})

    # produced data_vars
    produced = conn.execute(
        """SELECT e.dst_node_id AS dv, e.metadata_json AS m
           FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
           WHERE t.name='produces' AND e.src_node_id=?""", (nid,)).fetchall()
    produced_ids = [r["dv"] for r in produced]

    dtype_map: Dict[str, str] = {}
    for r in produced:
        info = _meta(r["m"])
        dt = info.get("dtype") or info.get("type")
        if dt:
            nm = conn.execute("SELECT name FROM node WHERE id=?", (r["dv"],)).fetchone()
            dtype_map[nm[0] if nm else str(r["dv"])] = dt

    # output_consumers: who consumes the produced data_vars
    output_consumers = set()
    for dv in produced_ids:
        for r in conn.execute(
            """SELECT COALESCE(d.qualified_name, d.name) AS q
               FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
               JOIN node d ON d.id=e.dst_node_id
               WHERE t.name='consumes' AND e.src_node_id=?""", (dv,)).fetchall():
            output_consumers.add(r["q"])

    # lineage_downstream: BFS over feeds / downstream_data_feed, depth <= 2
    lineage = set()
    frontier = set(produced_ids) | {nid}
    for _ in range(2):
        nxt = set()
        ph = ",".join("?" for _ in frontier)
        if not frontier:
            break
        rows = conn.execute(
            f"""SELECT e.dst_node_id AS d, n.name AS nm
                FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
                JOIN node n ON n.id=e.dst_node_id
                WHERE t.name IN ('feeds','downstream_data_feed')
                  AND e.src_node_id IN ({ph})""", tuple(frontier)).fetchall()
        for r in rows:
            if r["d"] not in lineage:
                lineage.add(r["d"])
                lineage_name = r["nm"]
                nxt.add(r["d"])
        frontier = nxt
    lineage_names = sorted({
        conn.execute("SELECT name FROM node WHERE id=?", (i,)).fetchone()[0]
        for i in lineage})

    return {"name": node["qualified_name"] or node["name"], "status": "existing",
            "callers": callers, "output_consumers": sorted(output_consumers),
            "dtype_map": dtype_map, "lineage_downstream": lineage_names}


# ── 3 · upstream-data assumptions (the un-gatable external boundary) ──────────────

def upstream_assumptions(conn, target_names: List[str]) -> List[Dict]:
    """Surface assumed-schema for sql_table / api_source nodes the targets read.

    A pipeline reading an external table/api cannot have its upstream schema
    gated — we can only state the assumption. For each related source carrying an
    ``assumed_schema`` (recorded by A-4), emit the table, the assumed columns and
    a recommendation to add a runtime fail-fast assertion at the load seam.

    Resolution: prefer ``reads_sql`` edges from the target functions; if the
    graph has no such edges, fall back to every sql/api source in the graph
    (best-effort, same shape as the reviewer's sql_contract).
    """
    targets = set(target_names or [])

    # source nodes (sql_table / api_source) with assumed_schema
    src_rows = conn.execute(
        """SELECT n.id, n.name, n.metadata_json
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('sql_table','api_source')"""
    ).fetchall()
    if not src_rows:
        return []

    # which sources are read by the targets (via reads_sql), else all
    read_map: Dict[int, set] = {}
    has_reads = False
    for r in conn.execute(
        """SELECT e.dst_node_id AS tbl, COALESCE(s.qualified_name, s.name) AS reader
           FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
           JOIN node s ON s.id=e.src_node_id
           WHERE t.name='reads_sql'""").fetchall():
        has_reads = True
        read_map.setdefault(r["tbl"], set()).add(r["reader"])

    out: List[Dict] = []
    for r in src_rows:
        info = _meta(r["metadata_json"])
        schema = info.get("assumed_schema")
        if not schema:
            continue
        if has_reads and targets:
            readers = read_map.get(r["id"], set())
            if not (readers & targets):
                continue  # this source isn't read by any target
        cols = sorted(schema.keys())
        out.append({
            "table": r["name"],
            "columns": cols,
            "assumed_schema": schema,
            "recommendation": (
                f"add a runtime fail-fast assertion at the load seam verifying "
                f"columns {cols} exist in '{r['name']}' — upstream schema is "
                f"assumed/unverified."),
        })
    return out





# ── 4 · ledger fuzzy-match (Phase E: DE Decision-Memory) ─────────────────────────

def _tokens(*texts):
    """Reuse the same tokenizer + stopword rule as keyword target collection."""
    out = set()
    for t in texts:
        for tok in _TOKEN_RE.findall(t or ""):
            low = tok.lower()
            if low not in _STOPWORDS:
                out.add(low)
    return out


def _reminder_text(entry):
    stmt = (entry.get("statement") or "").strip()
    why = (entry.get("rationale") or "").strip()
    if entry.get("kind") == "anti_pattern":
        base = "warning: " + stmt + " was tried and failed"
        if why:
            base += " — " + why
        return base + "; reconsider before proceeding."
    base = "reminder: " + stmt
    if why:
        base += " — because " + why
    return base + "; confirm before changing."


def ledger_matches(db_or_conn, project, user_query, declared_targets, top_n=5):
    """Surface relevant past decisions/failures from the provenance ledger.

    Phase E (DE Decision-Memory): a DETERMINISTIC lexical fuzzy-match — tokens
    from declared_targets + user_query are overlap-scored against each ACTIVE
    ledger entry's subjects + keywords. Entries with score>0 are returned sorted
    by score desc then recency, capped at top_n, each carrying a `reminder`
    string. These are advisory REMINDERS, never blocks.

    Honest boundary: matching is lexical (no embeddings/LLM) so recall is bounded
    by the keywords a human recorded on the entry. Degrades to [] when the
    project has no ledger table/entries.
    """
    owns = isinstance(db_or_conn, str)
    conn = sqlite3.connect(db_or_conn) if owns else db_or_conn
    if owns:
        conn.row_factory = sqlite3.Row
    try:
        has = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='LedgerEntries'"
        ).fetchone()
        if not has:
            return []
        rows = conn.execute(
            "SELECT * FROM LedgerEntries WHERE project = ? AND status = 'active' "
            "ORDER BY created_at DESC, id DESC", (project,)).fetchall()
    finally:
        if owns:
            conn.close()

    query_tokens = _tokens(user_query, " ".join(declared_targets or []))
    if not query_tokens:
        return []

    scored = []
    for r in rows:
        d = dict(r)
        subjects = json.loads(d["subjects"]) if d.get("subjects") else []
        keywords = json.loads(d["keywords"]) if d.get("keywords") else []
        entry_tokens = _tokens(" ".join(subjects), " ".join(keywords))
        score = len(query_tokens & entry_tokens)
        if score <= 0:
            continue
        scored.append({
            "kind": d["kind"],
            "statement": d["statement"],
            "rationale": d.get("rationale") or "",
            "subjects": subjects,
            "score": score,
            "reminder": _reminder_text(d),
        })

    scored.sort(key=lambda m: m["score"], reverse=True)
    return scored[:top_n]


# ── 5 · assemble impact_context ──────────────────────────────────────────────────

_BOUNDARY_MODIFY = (
    "Pre-flight is strongest for MODIFYING EXISTING code: callers, consumers, "
    "dtypes and downstream lineage above are read from the verified graph.")
_BOUNDARY_NEW = (
    " Some targets are NEW (not in the graph): their blast radius cannot be "
    "predicted — the graph only describes code that already exists. For new "
    "modules this degrades to describing the existing functions the new code "
    "will call. Upstream external sources (SQL/API) remain assumed, not gated.")


def compute_impact_context(db_path: str, user_query: str,
                           declared_targets: Optional[List[str]],
                           project: str = "") -> Dict:
    """Assemble the full impact_context for a plan (steps 1-5 + boundary note).

    The graph and the decision-memory ledger (Phase E) share the same project DB
    file, so ledger_reminders are pulled from the same db_path. When the DB has
    no LedgerEntries table or no project is given, ledger_reminders degrades to []
    without raising.
    """
    collected = collect_targets(user_query, declared_targets, db_path)
    target_names = [t["name"] for t in collected["targets"]]

    conn = _connect(db_path)
    try:
        symbols = [verify_symbol(conn, t) for t in target_names]
        upstream = upstream_assumptions(conn, target_names)
    finally:
        conn.close()

    any_new = any(s["status"] == "new" for s in symbols)
    boundary = _BOUNDARY_MODIFY + (_BOUNDARY_NEW if any_new else "")

    reminders = ledger_matches(db_path, project, user_query, declared_targets) \
        if project else []

    return {
        "targets": collected["targets"],
        "symbols": symbols,
        "upstream_assumptions": upstream,
        "ledger_reminders": reminders,
        "capability_boundary": boundary,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

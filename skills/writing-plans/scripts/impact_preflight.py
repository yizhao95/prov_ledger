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

Stdlib only (sqlite3 + json + re); imports nothing from the orchestrator package
(the optional `orch_conn` is a plain sqlite3 connection to the orchestrator DB,
used only to read node_reason and the constraint ledger via ledger_store).

Phase 3 (spec §2.8/§2.9, E2-1): one lookup per target now also carries its
stable node_key, its recent history (node_event + run attribution), the
reasons recorded for it (node_reason) and the constraints anchored to it
(LedgerEntries.kind='constraint', matched by node_key — never lexically;
restricted rationale never leaves the ledger). Each surfaced constraint is
counted as a hit (record_hit) — reading is the only evidence the anchoring
works — and the plan row keeps `constraint_ids` for the close-time bypass check.

Capability boundary: strongest for MODIFYING EXISTING code. For a brand-new
module it degrades to "here is what the existing functions you intend to call
look like" — the graph only describes code that exists.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

import ledger_store  # sibling, stdlib-only: constraints_for / record_hit

CALLABLE_TYPES = ("function", "method", "route")
SQL_SOURCE_TYPES = ("sql_table", "api_source")
HISTORY_LIMIT = 10

# Small stopword set — keep deterministic + dependency-free. Tokens shorter than
# 3 chars are dropped regardless.
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "but", "not",
    "you", "are", "was", "all", "any", "can", "has", "have", "will", "please",
    "function", "method", "step", "code", "change", "update", "add", "fix",
    "refactor", "touch",
}
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


# FL-020: the graph is rebuilt by init_project.sh during a review; publishing a
# plan at that moment must never crash. Open read-only, wait a bounded time for
# the writer, then degrade (see compute_impact_context) instead of raising.
BUSY_TIMEOUT_MS = int(os.environ.get("PROVLEDGER_GRAPH_BUSY_TIMEOUT_MS", "5000"))


def _connect(db_path: str) -> sqlite3.Connection:
    """Read-only connection to a project graph with a busy timeout."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
    return conn


def _is_busy(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


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

def _has_column(conn, table: str, column: str) -> bool:
    try:
        return column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return False


def _resolve_node(conn, name: str):
    """A callable/class node by qualified or bare name; else (phase 2) a
    column/dataframe by its snapshot qualified_name ("owner.column",
    "fn:df.column"). Returns a dict {id, name, qualified_name, file_path,
    kind, node_key} or None."""
    row = conn.execute(
        """SELECT n.id, n.name, n.qualified_name, n.file_path, t.name AS kind
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('function','method','route','class')
             AND (n.qualified_name = ? OR n.name = ?)
           LIMIT 1""", (name, name)).fetchone()
    if row is not None:
        d = dict(row)
        d["node_key"] = None
        if _has_column(conn, "node", "node_key"):
            k = conn.execute("SELECT node_key FROM node WHERE id=?", (d["id"],)).fetchone()
            d["node_key"] = k[0] if k and k[0] else None
        if d["node_key"] is None:
            d["node_key"] = _snapshot_key(conn, d["qualified_name"] or d["name"])
        return d
    snap = _snapshot_row(conn, name)
    if snap is None:
        return None
    return {"id": None, "name": name, "qualified_name": snap["qualified_name"], "file_path": snap["file_path"],
            "kind": snap["node_type"], "node_key": snap["node_key"]}


def _snapshot_row(conn, qualified_name: str):
    try:
        return conn.execute(
            "SELECT node_key, node_type, qualified_name, file_path FROM node_snapshot "
            "WHERE qualified_name = ? AND node_key <> '' ORDER BY run_id DESC, id DESC LIMIT 1",
            (qualified_name,)).fetchone()
    except sqlite3.OperationalError:      # graph built before the history layer
        return None


def _snapshot_key(conn, qualified_name: str) -> Optional[str]:
    row = _snapshot_row(conn, qualified_name) if qualified_name else None
    return row["node_key"] if row else None


def _history(conn, node_key: Optional[str], limit: int) -> List[Dict]:
    """The node's most recent `limit` events with run attribution (oldest first)."""
    if not node_key:
        return []
    try:
        rows = conn.execute(
            """SELECT e.event_type, e.run_id, e.seq, e.payload_json, e.created_at,
                      a.plan_id, a.step_id, a.commit_sha
               FROM node_event e JOIN analysis_run a ON a.id = e.run_id
               WHERE e.node_key = ? ORDER BY e.run_id DESC, e.seq DESC LIMIT ?""", (node_key, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in reversed(rows):
        out.append({"event_type": r["event_type"], "run_id": r["run_id"], "plan_id": r["plan_id"],
                    "step_id": r["step_id"], "commit_sha": r["commit_sha"], "created_at": r["created_at"],
                    "payload": _meta(r["payload_json"])})
    return out


def _reasons(orch_conn, node_key: Optional[str]) -> List[Dict]:
    if orch_conn is None or not node_key:
        return []
    try:
        rows = orch_conn.execute(
            "SELECT node_key, plan_id, step_id, kind, text, source, tier, created_at FROM node_reason "
            "WHERE node_key = ? ORDER BY id", (node_key,)).fetchall()
    except sqlite3.OperationalError:      # orchestrator DB predates migration 014
        return []
    cols = ("node_key", "plan_id", "step_id", "kind", "text", "source", "tier", "created_at")
    return [dict(zip(cols, r)) for r in rows]


def _constraints(orch_conn, project: str, node_key: Optional[str]) -> List[Dict]:
    if orch_conn is None or not node_key or not project:
        return []
    try:
        return ledger_store.constraints_for(orch_conn, project, [node_key])
    except sqlite3.OperationalError:      # orchestrator DB predates migration 015
        return []


def verify_symbol(conn, name: str, *, orch_conn=None, project: str = "",
                  history_limit: int = HISTORY_LIMIT) -> Dict:
    """Verify one candidate against the graph — one lookup, everything a
    change author needs about this data point.

    Existing -> {status:'existing', kind, node_key, callers, output_consumers,
    dtype_map, lineage_downstream, history, reasons, constraints}. Missing ->
    {status:'new', empty lists} (a declared symbol the graph can't find is
    itself a useful signal: new, or misremembered). history/reasons/constraints
    are [] without a node_key or without `orch_conn`.
    """
    node = _resolve_node(conn, name)
    if node is None:
        return {"name": name, "status": "new", "kind": None, "node_key": None, "callers": [],
                "output_consumers": [], "dtype_map": {}, "lineage_downstream": [],
                "history": [], "reasons": [], "constraints": []}

    key = node["node_key"]
    extras = {"kind": node["kind"], "node_key": key,
              "history": _history(conn, key, history_limit),
              "reasons": _reasons(orch_conn, key),
              "constraints": _constraints(orch_conn, project, key)}
    nid = node["id"]
    if nid is None:                       # column / dataframe: identity + history only
        return {"name": node["qualified_name"], "status": "existing", "callers": [],
                "output_consumers": [], "dtype_map": {}, "lineage_downstream": [], **extras}
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
            "dtype_map": dtype_map, "lineage_downstream": lineage_names, **extras}


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
            "id": d["id"],
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
                           project: str = "", orch_conn=None) -> Dict:
    """Assemble the full impact_context for a plan (steps 1-5 + boundary note).

    `orch_conn` (the orchestrator DB, where node_reason and the ledger live)
    adds per-target reasons + constraints and the lexical ledger_reminders;
    every constraint surfaced — by node_key or lexically — is recorded as a
    hit. Without it (legacy callers) those parts degrade to [] and the ledger
    is looked up on db_path as before. The result carries `constraint_ids`
    (for the close-time bypass check) and `approx_tokens` (len(json)//4).
    """
    try:
        collected = collect_targets(user_query, declared_targets, db_path)
        target_names = [t["name"] for t in collected["targets"]]
        conn = _connect(db_path)
        try:
            symbols = [verify_symbol(conn, t, orch_conn=orch_conn, project=project) for t in target_names]
            upstream = upstream_assumptions(conn, target_names)
        finally:
            conn.close()
        reminders = ledger_matches(orch_conn if orch_conn is not None else db_path,
                                   project, user_query, declared_targets) if project else []
    except sqlite3.OperationalError as exc:
        if not _is_busy(exc):
            raise
        # FL-020: the graph is being rebuilt (review refresh). Publish anyway,
        # say so loudly, keep the declared targets verbatim so the plan row still
        # records what the author intended to touch.
        return {
            "degraded": "graph busy",
            "targets": [{"name": n, "route": ["declared"]} for n in (declared_targets or []) if n],
            "symbols": [],
            "upstream_assumptions": [],
            "ledger_reminders": [],
            "capability_boundary": (
                f"graph busy: {db_path} was locked for {BUSY_TIMEOUT_MS} ms (a refresh in "
                "progress?) — no impact analysis was performed; re-check the blast radius "
                "before touching the declared targets."),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    any_new = any(s["status"] == "new" for s in symbols)
    boundary = _BOUNDARY_MODIFY + (_BOUNDARY_NEW if any_new else "")

    # E4-1: every constraint that reached the author counts as read.
    constraint_ids: List[int] = []
    for sym in symbols:
        for c in sym.get("constraints", []):
            if c["id"] not in constraint_ids:
                constraint_ids.append(c["id"])
    for m in reminders:
        if m.get("id") is not None and m.get("kind") == "constraint" and m["id"] not in constraint_ids:
            constraint_ids.append(m["id"])
    if orch_conn is not None:
        for cid in constraint_ids:
            ledger_store.record_hit(orch_conn, cid)
        for m in reminders:
            if m.get("id") is not None and m.get("kind") != "constraint":
                ledger_store.record_hit(orch_conn, m["id"])

    ctx = {
        "targets": collected["targets"],
        "symbols": symbols,
        "upstream_assumptions": upstream,
        "ledger_reminders": reminders,
        "constraint_ids": constraint_ids,
        "capability_boundary": boundary,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    ctx["approx_tokens"] = len(json.dumps(ctx, default=str)) // 4   # E2-3: the cost is reported, always
    return ctx

"""Precomputed LLM-digestible cards derived PURELY from graph edges.

consistency_card: for every function/method, the ready-made impact sets so an
LLM does ONE lookup instead of a graph traversal:
    callers              - functions that call me  (calls: caller -> me)
    callees              - functions I call        (calls: me -> callee)
    output_consumers     - who eats my return val  (produces me->dv, consumes dv->X)
    reads                - sql/bq I read           (reads_sql: me -> table)
    writes               - sql/bq I write          (writes_sql: me -> table)
    pipeline_membership  - pipelines I'm a step in (contains_step: pipe -> me)

symbol_card: one self-contained record per function/class (metadata + the
consistency sets above), for direct retrieval by an LLM.

Cards are ALWAYS rebuilt from edges in one pass; never hand-edited. This keeps
a single source of truth (the graph) and prevents drift.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Dict, List

_SYMBOL_TYPES = ("function", "method")

_CONSISTENCY_SCHEMA = """
CREATE TABLE IF NOT EXISTS consistency_card (
    symbol_id INTEGER PRIMARY KEY REFERENCES node(id),
    card_json TEXT NOT NULL
);
"""

_SYMBOL_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbol_card (
    symbol_id      INTEGER PRIMARY KEY REFERENCES node(id),
    qualified_name TEXT,
    card_json      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbol_card_qn ON symbol_card(qualified_name);
"""


def _name(conn, node_id: int) -> str:
    row = conn.execute("SELECT name FROM node WHERE id=?", (node_id,)).fetchone()
    return row[0] if row else str(node_id)


def _dv_param_name(conn, node_id: int) -> str | None:
    """Return the parameter name carried by a param-role data_var node."""
    row = conn.execute(
        "SELECT metadata_json FROM node WHERE id=?", (node_id,)
    ).fetchone()
    if row and row[0]:
        info = json.loads(row[0])
        if info.get("role") == "param":
            return info.get("param")
    return None


def _symbol_nodes(conn) -> List[tuple]:
    return conn.execute(
        f"""SELECT n.id, n.name, n.qualified_name, n.file_path,
                   n.line_start, n.line_end, t.name
            FROM node n JOIN node_type t ON n.node_type_id=t.id
            WHERE t.name IN ({','.join('?' * len(_SYMBOL_TYPES))})""",
        _SYMBOL_TYPES,
    ).fetchall()


def _edge_pairs(conn, edge_type: str) -> List[tuple]:
    return conn.execute(
        """SELECT e.src_node_id, e.dst_node_id, e.metadata_json
           FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
           WHERE t.name=?""",
        (edge_type,),
    ).fetchall()


def _sorted_unique(names) -> List[str]:
    return sorted(set(names))


def build_consistency_cards(conn: sqlite3.Connection) -> Dict[int, dict]:
    conn.executescript(_CONSISTENCY_SCHEMA)

    symbols = _symbol_nodes(conn)
    sym_ids = {s[0] for s in symbols}

    # calls: caller -> callee
    callers: Dict[int, list] = {}
    callees: Dict[int, list] = {}
    for src, dst, _ in _edge_pairs(conn, "calls"):
        if dst in sym_ids:
            callers.setdefault(dst, []).append(_name(conn, src))
        if src in sym_ids:
            callees.setdefault(src, []).append(_name(conn, dst))

    # output consumers: function --produces--> data_var --consumes--> consumer
    produces = _edge_pairs(conn, "produces")   # fn -> data_var
    consumes = _edge_pairs(conn, "consumes")   # data_var -> consumer
    dv_to_consumers: Dict[int, list] = {}
    for dv, consumer, _ in consumes:
        dv_to_consumers.setdefault(dv, []).append(consumer)
    output_consumers: Dict[int, list] = {}
    for fn, dv, _ in produces:
        for consumer in dv_to_consumers.get(dv, []):
            output_consumers.setdefault(fn, []).append(_name(conn, consumer))

    # sql reads/writes: fn -> table
    reads: Dict[int, list] = {}
    for src, dst, _ in _edge_pairs(conn, "reads_sql"):
        if src in sym_ids:
            reads.setdefault(src, []).append(_name(conn, dst))
    writes: Dict[int, list] = {}
    for src, dst, _ in _edge_pairs(conn, "writes_sql"):
        if src in sym_ids:
            writes.setdefault(src, []).append(_name(conn, dst))

    # pipeline membership: pipeline --contains_step--> fn
    membership: Dict[int, list] = {}
    for pipe, step, _ in _edge_pairs(conn, "contains_step"):
        if step in sym_ids:
            membership.setdefault(step, []).append(_name(conn, pipe))

    # ---- data-aware sets ----
    # profile tags: fn --tagged_profile--> profile
    profile_of: Dict[int, list] = {}
    for sid, pid, _ in _edge_pairs(conn, "tagged_profile"):
        if sid in sym_ids:
            profile_of.setdefault(sid, []).append(_name(conn, pid))

    # dtype_map: from produces (output) and feeds-param (inputs) data_vars
    dtype_map: Dict[int, dict] = {}
    for fn, dv, meta in produces:
        if fn in sym_ids:
            info = json.loads(meta) if meta else {}
            dtype_map.setdefault(fn, {})["return"] = info.get("type", "unknown")
    # feeds: data_var(param) -> fn
    for dv, fn, meta in _edge_pairs(conn, "feeds"):
        if fn in sym_ids:
            info = json.loads(meta) if meta else {}
            pname = _dv_param_name(conn, dv)
            if pname:
                dtype_map.setdefault(fn, {})[pname] = info.get("dtype", "unknown")

    # columns_in / columns_out via has_column on dataframes the fn touches:
    # approximate by collecting columns whose file matches — kept simple:
    # columns_out = columns the fn's dataframes expose (best-effort empty list).
    columns_in: Dict[int, list] = {}
    columns_out: Dict[int, list] = {}

    # lineage upstream/downstream: source --lineage(op=fn)--> target
    lineage_up: Dict[int, list] = {}
    lineage_down: Dict[int, list] = {}
    name_to_id = {nm: sid for sid in sym_ids for nm in [_name(conn, sid)]}
    for src, dst, meta in _edge_pairs(conn, "lineage"):
        info = json.loads(meta) if meta else {}
        op = info.get("op")
        fid = name_to_id.get(op)
        if fid is None:
            continue
        lineage_up.setdefault(fid, []).append(_name(conn, src))
        lineage_down.setdefault(fid, []).append(_name(conn, dst))

    cards: Dict[int, dict] = {}
    for sid, *_rest in symbols:
        card = {
            "callers": _sorted_unique(callers.get(sid, [])),
            "callees": _sorted_unique(callees.get(sid, [])),
            "output_consumers": _sorted_unique(output_consumers.get(sid, [])),
            "reads": _sorted_unique(reads.get(sid, [])),
            "writes": _sorted_unique(writes.get(sid, [])),
            "pipeline_membership": _sorted_unique(membership.get(sid, [])),
            "dtype_map": dtype_map.get(sid, {}),
            "columns_in": _sorted_unique(columns_in.get(sid, [])),
            "columns_out": _sorted_unique(columns_out.get(sid, [])),
            "lineage_upstream": _sorted_unique(lineage_up.get(sid, [])),
            "lineage_downstream": _sorted_unique(lineage_down.get(sid, [])),
            "profile": _sorted_unique(profile_of.get(sid, [])),
        }
        cards[sid] = card
        conn.execute(
            "INSERT OR REPLACE INTO consistency_card (symbol_id, card_json) VALUES (?, ?)",
            (sid, json.dumps(card)),
        )
    conn.commit()
    return cards


def _output_type(conn, symbol_id: int) -> str:
    row = conn.execute(
        """SELECT e.metadata_json
           FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
           WHERE t.name='produces' AND e.src_node_id=?""",
        (symbol_id,),
    ).fetchone()
    if row and row[0]:
        return json.loads(row[0]).get("type", "unknown")
    return "unknown"


def build_symbol_cards(conn: sqlite3.Connection) -> Dict[int, dict]:
    """Join symbol metadata + consistency card into a single retrieval record."""
    conn.executescript(_SYMBOL_SCHEMA)
    # ensure consistency cards exist / are current
    consistency = build_consistency_cards(conn)

    symbols = _symbol_nodes(conn)
    out: Dict[int, dict] = {}
    for sid, name, qname, fpath, lstart, lend, ntype in symbols:
        record = {
            "name": name,
            "qualified_name": qname,
            "kind": ntype,
            "file_path": fpath,
            "line_start": lstart,
            "line_end": lend,
            "output_type": _output_type(conn, sid),
            "consistency": consistency.get(sid, {}),
        }
        out[sid] = record
        conn.execute(
            """INSERT OR REPLACE INTO symbol_card (symbol_id, qualified_name, card_json)
               VALUES (?, ?, ?)""",
            (sid, qname, json.dumps(record)),
        )
    conn.commit()
    return out

"""Sub-flow profiling: tag every application function/method with the
data-science workflow(s) it participates in, so the graph can be subdivided
and the visualization can offer a per-sub-flow lens.

Profiles (a symbol may carry several):
  function-calling   - participates in `calls` edges (and nothing richer)
  pipeline           - is a step inside a pipeline (`contains_step` target)
  data-flow          - participates in a REAL data flow: it consumes a data_var,
                       OR its produced return is actually consumed downstream.
                       (Merely returning a value nobody consumes is NOT enough.)
  ml-training        - calls train_test_split / .fit / .train
  data-engineering   - has BOTH `reads_sql` and `writes_sql`
  endpoint           - is a request handler: target of a `handles` edge from a
                       route. Its return goes to the framework, not to data.

Everything except the ML call scan is derived purely from edges already in the
graph (same single-source-of-truth discipline as cards.py). ML patterns are
detected with a conservative AST re-walk, mirroring pipeline.py.
"""
from __future__ import annotations

import ast
import os
import sqlite3
from typing import Dict, Set

from . import store

PROFILE_NAMES = (
    "function-calling",
    "pipeline",
    "data-flow",
    "ml-training",
    "data-engineering",
    "endpoint",
)

_ML_CALL_NAMES = {"train_test_split", "fit", "train"}


def _symbol_index(conn: sqlite3.Connection) -> Dict[str, int]:
    """Map function/method name -> node id."""
    rows = conn.execute(
        """SELECT n.name, n.id
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('function', 'method')"""
    ).fetchall()
    return {name: nid for name, nid in rows}


def _ids_with_edge(conn, edge_type: str, *, side: str) -> Set[int]:
    col = "src_node_id" if side == "src" else "dst_node_id"
    rows = conn.execute(
        f"""SELECT e.{col}
            FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
            WHERE t.name=?""",
        (edge_type,),
    ).fetchall()
    return {int(r[0]) for r in rows}


def _call_name(func) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _ml_symbols(repo_root: str, file_map: Dict[str, int], symbols: Dict[str, int]) -> Set[int]:
    """Function ids whose body calls train_test_split / .fit / .train."""
    found: Set[int] = set()
    repo_root = os.path.abspath(repo_root)
    for rel_path in file_map:
        if not rel_path.endswith(".py"):
            continue
        abs_path = os.path.join(repo_root, rel_path)
        try:
            with open(abs_path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name not in symbols:
                continue
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Call) and _call_name(sub.func) in _ML_CALL_NAMES:
                    found.add(symbols[fn.name])
                    break
    return found


def analyze(
    conn: sqlite3.Connection,
    repo_root: str,
    file_map: Dict[str, int],
) -> None:
    profile_t = store.get_or_create_node_type(conn, "profile")
    tagged_e = store.get_or_create_edge_type(conn, "tagged_profile")

    # one profile node per name (deduped)
    profile_id: Dict[str, int] = {}
    for name in PROFILE_NAMES:
        row = conn.execute(
            """SELECT n.id FROM node n JOIN node_type t ON n.node_type_id=t.id
               WHERE t.name='profile' AND n.name=?""",
            (name,),
        ).fetchone()
        profile_id[name] = (
            int(row[0])
            if row
            else store.add_node(conn, profile_t, name=name, qualified_name=name)
        )

    symbols = _symbol_index(conn)
    sym_ids = set(symbols.values())

    # ---- gather membership sets (all derived from existing edges) ----
    callers = _ids_with_edge(conn, "calls", side="src") & sym_ids
    callees = _ids_with_edge(conn, "calls", side="dst") & sym_ids
    in_calls = callers | callees

    pipeline_steps = _ids_with_edge(conn, "contains_step", side="dst") & sym_ids

    # data-flow: REAL flow only.
    #  - a consumer (dst of a consumes edge) always counts.
    #  - a producer counts ONLY if its produced return is actually consumed,
    #    i.e. the data_var it produces is the src of a consumes edge.
    consumers = _ids_with_edge(conn, "consumes", side="dst") & sym_ids
    consumed_dv = _ids_with_edge(conn, "consumes", side="src")  # data_var ids
    real_producers = {
        int(src)
        for src, dst in conn.execute(
            """SELECT e.src_node_id, e.dst_node_id
               FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
               WHERE t.name='produces'"""
        ).fetchall()
        if int(dst) in consumed_dv
    } & sym_ids
    in_dataflow = real_producers | consumers

    # endpoint: function/method that handles a route (dst of a handles edge).
    endpoints = _ids_with_edge(conn, "handles", side="dst") & sym_ids
    # an endpoint's return-to-framework is not data flow.
    in_dataflow -= endpoints

    reads = _ids_with_edge(conn, "reads_sql", side="src") & sym_ids
    writes = _ids_with_edge(conn, "writes_sql", side="src") & sym_ids
    data_eng = reads & writes

    ml = _ml_symbols(repo_root, file_map, symbols)

    membership: Dict[str, Set[int]] = {
        "pipeline": pipeline_steps,
        "data-flow": in_dataflow,
        "ml-training": ml,
        "data-engineering": data_eng,
        "function-calling": in_calls,
        "endpoint": endpoints,
    }

    # ---- emit tagged_profile edges (a symbol may match several) ----
    seen: Set[tuple] = set()
    for profile_name, ids in membership.items():
        pid = profile_id[profile_name]
        for sid in ids:
            key = (sid, pid)
            if key in seen:
                continue
            seen.add(key)
            store.add_edge(conn, tagged_e, sid, pid,
                           metadata={"profile": profile_name})

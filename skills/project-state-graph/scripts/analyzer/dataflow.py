"""Dataflow analysis: downstream_data_feed edges (a function's output var
becomes another function's input -> data + type dependency).

Conservative, intra-function tracing:
  x = producer()      # x is tainted by producer
  a, b = multi()      # a, b tainted by multi
  consumer(x)         # edge producer -> consumer (var=x)
"""
from __future__ import annotations

import ast
import os
import sqlite3
from typing import Dict

from . import store


def _callable_index(conn: sqlite3.Connection) -> Dict[str, int]:
    """Map simple callable name -> node id for functions and methods."""
    rows = conn.execute(
        """SELECT n.name, n.id
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('function', 'method')"""
    ).fetchall()
    index: Dict[str, int] = {}
    for name, nid in rows:
        index[name] = nid
    return index


def _name_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Map simple callable name -> number of definitions (>1 => ambiguous)."""
    rows = conn.execute(
        """SELECT n.name, COUNT(*)
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('function', 'method')
           GROUP BY n.name"""
    ).fetchall()
    return {name: int(c) for name, c in rows}


def _call_name(func) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def analyze(
    conn: sqlite3.Connection,
    repo_root: str,
    file_map: Dict[str, int],
) -> None:
    feed_e = store.get_or_create_edge_type(conn, "downstream_data_feed")
    callables = _callable_index(conn)
    name_counts = _name_counts(conn)
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
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _trace_function(conn, fn, callables, name_counts, feed_e)


def _trace_function(conn, fn, callables, name_counts, feed_e) -> None:
    # var name -> producer node id
    tainted: Dict[str, int] = {}
    seen: set[tuple[int, int, str]] = set()

    for stmt in ast.walk(fn):
        # Assignment of a call result: x = producer(); a, b = multi()
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            producer_name = _call_name(stmt.value.func)
            if producer_name in callables:
                producer_id = callables[producer_name]
                for target in stmt.targets:
                    for var in _target_names(target):
                        tainted[var] = producer_id

    # Now scan calls and link tainted args to the called function.
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call):
            consumer_name = _call_name(sub.func)
            if consumer_name not in callables:
                continue
            consumer_id = callables[consumer_name]
            for arg in sub.args:
                if isinstance(arg, ast.Name) and arg.id in tainted:
                    producer_id = tainted[arg.id]
                    if producer_id == consumer_id:
                        continue
                    key = (producer_id, consumer_id, arg.id)
                    if key in seen:
                        continue
                    seen.add(key)
                    confidence = "high"
                    if (name_counts.get(consumer_name, 0) > 1
                            or name_counts.get(_producer_name_for(producer_id, callables), 0) > 1):
                        confidence = "inferred"
                    store.add_edge(
                        conn, feed_e, producer_id, consumer_id,
                        metadata={"var": arg.id, "confidence": confidence},
                    )


def _producer_name_for(producer_id, callables) -> str:
    for name, nid in callables.items():
        if nid == producer_id:
            return name
    return ""


def _target_names(target) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for elt in target.elts:
            names.extend(_target_names(elt))
        return names
    return []

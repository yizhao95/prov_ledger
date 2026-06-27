"""Dataflow analysis: downstream_data_feed edges (a function's output var
becomes another function's input -> data + type dependency).

Conservative, intra-function tracing:
  x = producer()      # x is tainted by producer
  a, b = multi()      # a, b tainted by multi
  consumer(x)         # edge producer -> consumer (var=x)

Resolution uses the shared qualified-name resolver (PSG-C2): a producer/consumer
name is resolved with the call site's MODULE context, so same-named functions in
different modules no longer collapse to one arbitrary node. Ambiguous names emit
one edge per candidate tagged `inferred` (producer taint that can't be resolved
to a single node is skipped — never bound arbitrarily).
"""
from __future__ import annotations

import ast
import os
import sqlite3
from typing import Dict

from . import _resolve, store
from .py_ast import _module_name


def analyze(
    conn: sqlite3.Connection,
    repo_root: str,
    file_map: Dict[str, int],
) -> None:
    feed_e = store.get_or_create_edge_type(conn, "downstream_data_feed")
    idx = _resolve.build_index(conn)
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
        module = _module_name(rel_path)
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _trace_function(conn, fn, idx, module, feed_e)


def _trace_function(conn, fn, idx, module, feed_e) -> None:
    # var name -> producer node id
    tainted: Dict[str, int] = {}
    seen: set[tuple[int, int, str]] = set()

    # Assignment of a call result: x = producer(); a, b = multi()
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            producer_name = _resolve.call_name(stmt.value.func)
            if not producer_name:
                continue
            # Only taint when the producer resolves to a SINGLE node — never bind
            # to an arbitrary same-named candidate.
            producer_id = idx.resolve_one(producer_name, module=module)
            if producer_id is None:
                continue
            for target in stmt.targets:
                for var in _target_names(target):
                    tainted[var] = producer_id

    # Scan calls and link tainted args to the called function(s).
    for sub in ast.walk(fn):
        if not isinstance(sub, ast.Call):
            continue
        consumer_name = _resolve.call_name(sub.func)
        if not consumer_name:
            continue
        consumers = idx.resolve(consumer_name, module=module)  # [(id, confidence)]
        if not consumers:
            continue
        for arg in sub.args:
            if not (isinstance(arg, ast.Name) and arg.id in tainted):
                continue
            producer_id = tainted[arg.id]
            for consumer_id, confidence in consumers:
                if producer_id == consumer_id:
                    continue
                key = (producer_id, consumer_id, arg.id)
                if key in seen:
                    continue
                seen.add(key)
                store.add_edge(
                    conn, feed_e, producer_id, consumer_id,
                    metadata={"var": arg.id, "confidence": confidence},
                )


def _target_names(target) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for elt in target.elts:
            names.extend(_target_names(elt))
        return names
    return []

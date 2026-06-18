"""Pipeline detection: chained function calls (e.g. __main__ orchestration or
an orchestrator function) become a `pipeline` node with ordered
`pipeline_step` edges between consecutive steps.
"""
from __future__ import annotations

import ast
import os
import sqlite3
from typing import Dict, List

from . import store

# An orchestrator must chain at least this many recognised calls.
MIN_STEPS = 2


def _callable_index(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        """SELECT n.name, n.id
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('function', 'method')"""
    ).fetchall()
    return {name: nid for name, nid in rows}


def _call_name(func) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _ordered_calls(body, callables) -> List[str]:
    """Top-level (statement-order) calls within a body that resolve to known callables."""
    out: List[str] = []
    for stmt in body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Call):
                name = _call_name(sub.func)
                if name in callables:
                    out.append(name)
    return out


def analyze(
    conn: sqlite3.Connection,
    repo_root: str,
    file_map: Dict[str, int],
) -> None:
    pipeline_t = store.get_or_create_node_type(conn, "pipeline")
    step_e = store.get_or_create_edge_type(conn, "pipeline_step")
    contains_e = store.get_or_create_edge_type(conn, "contains_step")
    callables = _callable_index(conn)
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
        module = os.path.splitext(rel_path)[0].replace(os.sep, ".")

        # 1) __main__ blocks
        for stmt in tree.body:
            if _is_main_block(stmt):
                steps = _ordered_calls(stmt.body, callables)
                if len(steps) >= MIN_STEPS:
                    _emit(conn, pipeline_t, step_e, contains_e, callables,
                          f"{module}.__main__", rel_path, stmt.lineno, steps)

        # 2) orchestrator functions
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                steps = _ordered_calls(stmt.body, callables)
                if len(steps) >= MIN_STEPS:
                    _emit(conn, pipeline_t, step_e, contains_e, callables,
                          f"{module}.{stmt.name}", rel_path, stmt.lineno, steps)


def _is_main_block(stmt) -> bool:
    if not isinstance(stmt, ast.If):
        return False
    test = stmt.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
    )


def _emit(conn, pipeline_t, step_e, contains_e, callables,
          qualified_name, rel_path, lineno, steps) -> None:
    pid = store.add_node(
        conn, pipeline_t, name=qualified_name, qualified_name=qualified_name,
        file_path=rel_path, line_start=lineno,
        metadata={"steps": steps},
    )
    # contains_step: pipeline -> each step (with index)
    for idx, name in enumerate(steps):
        store.add_edge(conn, contains_e, pid, callables[name],
                       metadata={"index": idx})
    # pipeline: consecutive step -> step (ordered)
    for idx in range(len(steps) - 1):
        store.add_edge(
            conn, step_e, callables[steps[idx]], callables[steps[idx + 1]],
            metadata={"index": idx, "pipeline": qualified_name},
        )

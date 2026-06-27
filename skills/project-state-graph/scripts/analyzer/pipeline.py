"""Pipeline detection: chained function calls (e.g. __main__ orchestration or
an orchestrator function) become a `pipeline` node with ordered
`pipeline_step` edges between consecutive steps.
"""
from __future__ import annotations

import ast
import os
import sqlite3
from typing import Dict, List, Tuple

from . import _resolve, store
from .py_ast import _module_name

# An orchestrator must chain at least this many recognised calls.
MIN_STEPS = 2


def _call_name(func) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _ordered_calls(body, idx, module) -> List[Tuple[str, int]]:
    """Statement-order calls resolving UNIQUELY (PSG-C2) to a callable node.

    Returns (name, node_id) pairs. Ambiguous/unknown call names are skipped rather
    than bound to an arbitrary same-named node.
    """
    out: List[Tuple[str, int]] = []
    for stmt in body:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Call):
                name = _call_name(sub.func)
                if name is None:
                    continue
                nid = idx.resolve_one(name, module=module)
                if nid is not None:
                    out.append((name, nid))
    return out


def analyze(
    conn: sqlite3.Connection,
    repo_root: str,
    file_map: Dict[str, int],
) -> None:
    pipeline_t = store.get_or_create_node_type(conn, "pipeline")
    step_e = store.get_or_create_edge_type(conn, "pipeline_step")
    contains_e = store.get_or_create_edge_type(conn, "contains_step")
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

        # 1) __main__ blocks
        for stmt in tree.body:
            if _is_main_block(stmt):
                steps = _ordered_calls(stmt.body, idx, module)
                if len(steps) >= MIN_STEPS:
                    _emit(conn, pipeline_t, step_e, contains_e,
                          f"{module}.__main__", rel_path, stmt.lineno, steps)

        # 2) orchestrator functions
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                steps = _ordered_calls(stmt.body, idx, module)
                if len(steps) >= MIN_STEPS:
                    _emit(conn, pipeline_t, step_e, contains_e,
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


def _emit(conn, pipeline_t, step_e, contains_e,
          qualified_name, rel_path, lineno, steps) -> None:
    # steps: list of (name, node_id), already uniquely resolved.
    pid = store.add_node(
        conn, pipeline_t, name=qualified_name, qualified_name=qualified_name,
        file_path=rel_path, line_start=lineno,
        metadata={"steps": [name for name, _ in steps]},
    )
    # contains_step: pipeline -> each step (with index)
    for i, (_name, nid) in enumerate(steps):
        store.add_edge(conn, contains_e, pid, nid, metadata={"index": i})
    # pipeline: consecutive step -> step (ordered)
    for i in range(len(steps) - 1):
        store.add_edge(
            conn, step_e, steps[i][1], steps[i + 1][1],
            metadata={"index": i, "pipeline": qualified_name},
        )

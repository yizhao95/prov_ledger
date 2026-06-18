"""Conservative bare-name undefined-call detection.

Records an `unresolved_call` DB node for every bare-name function call (`foo()`,
never `obj.foo()`) whose name cannot be resolved to ANY of:

  - a project-defined callable (function/method node, any module),
  - an imported symbol of the same file (see py_ast.imported_symbols),
  - a Python builtin,
  - a name bound anywhere in the module (params, assignments, for/with/except
    targets, comprehension targets, lambda args, nested def/class names).

The binding set is intentionally OVER-approximated (module-wide rather than
strictly lexical). Treating a name as bound when it might not be in this exact
scope only ever SUPPRESSES a finding — which is the correct bias for a hard
build-gate: we never want a false "undefined" failure. Attribute calls are never
bare names and are therefore never flagged.
"""
from __future__ import annotations

import ast
import builtins
import os
import sqlite3
from typing import Dict, Set

from . import py_ast, store

_BUILTINS: Set[str] = set(dir(builtins))


def _project_callables(conn: sqlite3.Connection) -> Set[str]:
    rows = conn.execute(
        """SELECT n.name FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('function', 'method')"""
    ).fetchall()
    return {r[0] for r in rows}


def _bound_names(tree: ast.AST) -> Set[str]:
    """Every name bound anywhere in the module (over-approximation)."""
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.alias):
            # import bindings (defensive; also covered by imported_symbols)
            names.add(node.asname or node.name.split(".")[0])
    return names


def _bare_call_name(call: ast.Call):
    """Return the bare callee name for a Name-call, else None for attribute calls."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def analyze(
    conn: sqlite3.Connection,
    repo_root: str,
    file_map: Dict[str, int],
) -> None:
    unresolved_t = store.get_or_create_node_type(conn, "unresolved_call")
    project_callables = _project_callables(conn)
    repo_root = os.path.abspath(repo_root)

    for rel_path in file_map:
        if not rel_path.endswith(".py"):
            continue
        abs_path = os.path.join(repo_root, rel_path)
        try:
            with open(abs_path, encoding="utf-8") as fh:
                source = fh.read()
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue

        resolvable = (
            project_callables
            | py_ast.imported_symbols(source)
            | _BUILTINS
            | _bound_names(tree)
        )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _bare_call_name(node)
            if name is None or name in resolvable:
                continue
            store.add_node(
                conn, unresolved_t, name=name, qualified_name=name,
                file_path=rel_path, line_start=getattr(node, "lineno", None),
                metadata={"name": name},
            )

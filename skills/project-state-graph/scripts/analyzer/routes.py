"""FastAPI route extraction: @app.get("/path") / @router.post(...) -> route nodes.

A `route` node is created per decorated endpoint, with a `defines` edge from
the file and a `handles` edge route -> handler function.
"""
from __future__ import annotations

import ast
import os
import sqlite3
from typing import Dict

from . import store

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}


def analyze(
    conn: sqlite3.Connection,
    repo_root: str,
    file_map: Dict[str, int],
) -> None:
    route_t = store.get_or_create_node_type(conn, "route")
    defines_e = store.get_or_create_edge_type(conn, "defines")
    handles_e = store.get_or_create_edge_type(conn, "handles")
    callables = _callable_index(conn)
    repo_root = os.path.abspath(repo_root)

    for rel_path, file_node in file_map.items():
        if not rel_path.endswith(".py"):
            continue
        abs_path = os.path.join(repo_root, rel_path)
        try:
            with open(abs_path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                info = _route_info(deco)
                if info is None:
                    continue
                method, path = info
                rid = store.add_node(
                    conn, route_t, name=path,
                    qualified_name=f"{method.upper()} {path}",
                    file_path=rel_path, line_start=node.lineno,
                    metadata={"method": method.upper(), "handler": node.name},
                )
                store.add_edge(conn, defines_e, file_node, rid)
                if node.name in callables:
                    store.add_edge(conn, handles_e, rid, callables[node.name])


def _route_info(deco):
    """Return (method, path) for an @app.get("/x") style decorator, else None."""
    if not isinstance(deco, ast.Call):
        return None
    func = deco.func
    if not isinstance(func, ast.Attribute):
        return None
    method = func.attr.lower()
    if method not in HTTP_METHODS:
        return None
    if not deco.args:
        return None
    first = deco.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return method, first.value
    return None


def _callable_index(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        """SELECT n.name, n.id
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('function', 'method')"""
    ).fetchall()
    return {name: nid for name, nid in rows}

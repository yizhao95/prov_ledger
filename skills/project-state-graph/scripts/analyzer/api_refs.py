"""HTTP API reference analysis.

Scans Python for requests / httpx / aiohttp calls and models each endpoint URL
as an `api_source` node, linked from the enclosing function with:
  reads_api  : GET / HEAD
  writes_api : POST / PUT / PATCH / DELETE

Conservative: only string-literal (or f-string literal-part) URLs are captured.
"""
from __future__ import annotations

import ast
import os
import sqlite3
from typing import Dict, Optional

from . import _resolve, store
from .py_ast import _module_name

_READ_METHODS = {"get", "head", "options"}
_WRITE_METHODS = {"post", "put", "patch", "delete"}
_HTTP_LIBS = {"requests", "httpx", "aiohttp", "session", "client"}


def analyze(
    conn: sqlite3.Connection,
    repo_root: str,
    file_map: Dict[str, int],
) -> None:
    api_t = store.get_or_create_node_type(conn, "api_source")
    reads_e = store.get_or_create_edge_type(conn, "reads_api")
    writes_e = store.get_or_create_edge_type(conn, "writes_api")
    idx = _resolve.build_index(conn)
    repo_root = os.path.abspath(repo_root)

    cache: Dict[str, int] = {}

    def api_node(url: str) -> int:
        if url not in cache:
            cache[url] = store.add_node(conn, api_t, name=url, qualified_name=url)
        return cache[url]

    for rel_path in file_map:
        if not rel_path.endswith(".py"):
            continue
        abs_path = os.path.join(repo_root, rel_path)
        try:
            with open(abs_path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        # PSG-C2: enclosing function resolved by unique qualified name.
        for fn, qual in _resolve.iter_funcs(tree, _module_name(rel_path)):
            fn_id = idx.by_qual.get(qual)
            if fn_id is None:
                continue
            # A-4: collect the keys the function subscripts off any dict/json
            # (best-effort proxy for the columns it expects the API to return).
            expected_keys = _subscript_keys(fn)
            for sub in ast.walk(fn):
                hit = _http_call(sub)
                if hit is None:
                    continue
                method, url = hit
                edge = writes_e if method in _WRITE_METHODS else reads_e
                node_id = api_node(url)
                store.add_edge(conn, edge, fn_id, node_id,
                               metadata={"method": method.upper()})
                if method not in _WRITE_METHODS and expected_keys:
                    _merge_assumed_schema(conn, node_id, expected_keys)


def _subscript_keys(fn) -> dict:
    """Return {key: 'unknown'} for every string-literal subscript in `fn`
    (e.g. data["name"]). Best-effort proxy for the fields the code expects an
    API response to carry — used to populate api_source.assumed_schema (A-4).
    """
    keys: dict = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Subscript):
            key = _const_str(node.slice)
            if key is not None:
                keys[key] = "unknown"
    return keys


def _const_str(node) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _merge_assumed_schema(conn: sqlite3.Connection, node_id: int,
                          cols: dict) -> None:
    """Merge {key: dtype} into a node's metadata_json['assumed_schema']."""
    import json
    if not cols:
        return
    row = conn.execute("SELECT metadata_json FROM node WHERE id=?",
                       (node_id,)).fetchone()
    meta = json.loads(row[0]) if row and row[0] else {}
    existing = meta.get("assumed_schema", {})
    existing.update(cols)
    meta["assumed_schema"] = existing
    conn.execute("UPDATE node SET metadata_json=? WHERE id=?",
                 (json.dumps(meta), node_id))
    conn.commit()


def _http_call(node) -> Optional[tuple]:
    """Return (method, url) for an http library call with a literal URL."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    method = func.attr.lower()
    if method not in _READ_METHODS and method not in _WRITE_METHODS:
        return None
    root = _attr_root(func.value)
    if root is not None and root.lower() not in _HTTP_LIBS:
        return None
    if not node.args:
        return None
    url = _string_value(node.args[0])
    if not url or not url.startswith(("http://", "https://")):
        return None
    return method, url


def _attr_root(value) -> Optional[str]:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return _attr_root(value.value)
    if isinstance(value, ast.Call):
        return _attr_root(value.func)
    return None


def _string_value(node) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [
            v.value
            for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        return "".join(parts) if parts else None
    return None


def _callable_index(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        """SELECT n.name, n.id
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('function', 'method')"""
    ).fetchall()
    return {name: nid for name, nid in rows}

"""SQL reference analysis.

- Parses `.sql` files: FROM/JOIN -> sql_table nodes.
- Scans Python string literals for BigQuery refs (`project.dataset.table`)
  -> bq_dataset nodes, and links the enclosing function with reads_sql /
  writes_sql edges based on the surrounding SQL verb.
"""
from __future__ import annotations

import ast
import os
import re
import sqlite3
from typing import Dict

from . import store

# FROM/JOIN <identifier>   (plain SQL tables)
_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+`?([A-Za-z_][\w.\-]*)`?", re.IGNORECASE
)
# BigQuery fully-qualified: project.dataset.table (optionally back-ticked)
_BQ_RE = re.compile(r"`?([A-Za-z][\w\-]*\.[\w\-]+\.[\w\-]+)`?")
# write targets
_WRITE_RE = re.compile(
    r"\b(?:INSERT\s+INTO|CREATE\s+TABLE|MERGE\s+INTO|UPDATE)\s+`?([A-Za-z_][\w.\-]*)`?",
    re.IGNORECASE,
)


def _looks_like_sql(text: str) -> bool:
    upper = text.upper()
    return any(kw in upper for kw in ("SELECT ", "INSERT ", "CREATE TABLE", "MERGE ", "UPDATE "))


# SELECT <projection> FROM ...  — for the A-4 stored assumed-schema.
_SELECT_RE = re.compile(r"\bselect\b(.*?)\bfrom\b", re.IGNORECASE | re.DOTALL)
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")


def _split_top_level(clause: str) -> list[str]:
    parts, depth, buf = [], 0, []
    for ch in clause:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _projection(sql: str) -> Dict[str, str]:
    """Best-effort {column: dtype} the code expects this query to return.

    dtype is 'unknown' (a bare SELECT doesn't declare types). A `SELECT *`
    yields {'*': 'unknown'}. Used to populate a source node's assumed_schema.
    """
    m = _SELECT_RE.search(sql)
    if not m:
        return {}
    clause = m.group(1).strip()
    if clause == "*":
        return {"*": "unknown"}
    cols: Dict[str, str] = {}
    for item in _split_top_level(clause):
        if item == "*":
            cols["*"] = "unknown"
            continue
        as_m = re.search(r"\bas\b\s+([A-Za-z_]\w*)\s*$", item, re.IGNORECASE)
        if as_m:
            cols[as_m.group(1)] = "unknown"
            continue
        idents = _IDENT_RE.findall(item)
        if idents:
            cols[idents[-1]] = "unknown"
    return cols


def _merge_assumed_schema(conn: sqlite3.Connection, node_id: int,
                          cols: Dict[str, str]) -> None:
    """Merge {column: dtype} into a node's metadata_json['assumed_schema']."""
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


def analyze(
    conn: sqlite3.Connection,
    repo_root: str,
    file_map: Dict[str, int],
) -> None:
    sql_table_t = store.get_or_create_node_type(conn, "sql_table")
    bq_t = store.get_or_create_node_type(conn, "bq_dataset")
    reads_e = store.get_or_create_edge_type(conn, "reads_sql")
    writes_e = store.get_or_create_edge_type(conn, "writes_sql")

    callables = _callable_index(conn)
    repo_root = os.path.abspath(repo_root)

    # cache: table/bq name -> node id (dedupe)
    cache: Dict[str, int] = {}

    def table_node(name: str) -> int:
        if name not in cache:
            ntype = bq_t if name.count(".") >= 2 else sql_table_t
            cache[name] = store.add_node(conn, ntype, name=name, qualified_name=name)
        return cache[name]

    for rel_path, file_node in file_map.items():
        abs_path = os.path.join(repo_root, rel_path)
        if rel_path.endswith(".sql"):
            try:
                text = open(abs_path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            read_tables = [m.group(1) for m in _TABLE_RE.finditer(text)]
            for name in read_tables:
                table_node(name)
            for m in _WRITE_RE.finditer(text):
                table_node(m.group(1))
            # A-4: record the query's projected columns as the assumed schema
            # of the table(s) it reads from.
            proj = _projection(text)
            for name in read_tables:
                _merge_assumed_schema(conn, table_node(name), proj)
        elif rel_path.endswith(".py"):
            _scan_python(conn, abs_path, callables, table_node,
                         reads_e, writes_e)


def _scan_python(conn, abs_path, callables, table_node, reads_e, writes_e) -> None:
    try:
        with open(abs_path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (SyntaxError, UnicodeDecodeError):
        return

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name not in callables:
            continue
        fn_id = callables[fn.name]
        for sub in ast.walk(fn):
            text = _string_value(sub)
            if not text or not _looks_like_sql(text):
                continue
            writes = {m.group(1) for m in _WRITE_RE.finditer(text)}
            proj = _projection(text)
            for name in _BQ_RE.findall(text):
                tid = table_node(name)
                edge = writes_e if name in writes else reads_e
                store.add_edge(conn, edge, fn_id, tid, metadata={"sql": name})
                if name not in writes:
                    _merge_assumed_schema(conn, tid, proj)
            # plain (non-BQ) tables referenced from python SQL too
            for m in _TABLE_RE.finditer(text):
                name = m.group(1)
                if name.count(".") >= 2:
                    continue  # already handled as BQ
                tid = table_node(name)
                store.add_edge(conn, reads_e, fn_id, tid)
                _merge_assumed_schema(conn, tid, proj)


def _string_value(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # f-string: concatenate the literal parts
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

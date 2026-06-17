"""Data-model analysis: DataFrames, columns and declared dataset schemas as
first-class nodes, so data (not just code) is traceable through the graph.

Node types:
  dataframe : a pandas/pyspark frame bound to a variable
  column    : a named field (df["col"] access or schema field) with dtype +
              dtype_provenance
  dataset   : a declared schema source (StructType / pandera / pydantic)

Edges:
  has_column : dataframe|dataset -> column
  derives    : dataframe -> dataframe  (op-labeled, df2 = df.dropna())
  transforms : column -> column        (df["c"] = df["a"] + df["b"])

Detection is conservative static AST. Where a column dtype cannot be determined
statically it is left "unknown" with provenance "unknown" — the agent profiling
phase may later probe it at runtime (provenance "runtime-probe").
"""
from __future__ import annotations

import ast
import os
import sqlite3
from typing import Dict, Optional, Tuple

from . import store

_DF_CONSTRUCTORS = {"DataFrame", "createDataFrame"}
_DF_READERS_PREFIX = "read_"  # read_csv, read_parquet, ...


def analyze(
    conn: sqlite3.Connection,
    repo_root: str,
    file_map: Dict[str, int],
) -> None:
    df_t = store.get_or_create_node_type(conn, "dataframe")
    col_t = store.get_or_create_node_type(conn, "column")
    ds_t = store.get_or_create_node_type(conn, "dataset")
    has_col_e = store.get_or_create_edge_type(conn, "has_column")
    derives_e = store.get_or_create_edge_type(conn, "derives")
    transforms_e = store.get_or_create_edge_type(conn, "transforms")
    repo_root = os.path.abspath(repo_root)

    ctx = _Ctx(conn, df_t, col_t, ds_t, has_col_e, derives_e, transforms_e)

    for rel_path in file_map:
        if not rel_path.endswith(".py"):
            continue
        abs_path = os.path.join(repo_root, rel_path)
        try:
            with open(abs_path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        # module-level declared schemas (StructType, etc.)
        for stmt in tree.body:
            _scan_schema_assign(ctx, stmt, rel_path)
        # function-level dataframes / columns / transforms
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _scan_function(ctx, fn, rel_path)


class _Ctx:
    def __init__(self, conn, df_t, col_t, ds_t, has_col_e, derives_e, transforms_e):
        self.conn = conn
        self.df_t = df_t
        self.col_t = col_t
        self.ds_t = ds_t
        self.has_col_e = has_col_e
        self.derives_e = derives_e
        self.transforms_e = transforms_e
        # (owner_node_id, col_name) -> column node id
        self.columns: Dict[Tuple[int, str], int] = {}

    def column(self, owner_id: int, name: str, rel_path: str) -> int:
        key = (owner_id, name)
        if key not in self.columns:
            cid = store.add_node(
                self.conn, self.col_t, name=name, qualified_name=name,
                file_path=rel_path,
                metadata={"dtype": "unknown", "dtype_provenance": "unknown"},
            )
            store.add_edge(self.conn, self.has_col_e, owner_id, cid)
            self.columns[key] = cid
        return self.columns[key]

    def set_col_dtype(self, col_id: int, dtype: str, provenance: str) -> None:
        self.conn.execute(
            "UPDATE node SET metadata_json=? WHERE id=?",
            (
                _json({"dtype": dtype, "dtype_provenance": provenance}),
                col_id,
            ),
        )
        self.conn.commit()


def _json(d: dict) -> str:
    import json
    return json.dumps(d)


# ── module-level declared schemas ────────────────────────────────────────────

def _scan_schema_assign(ctx: _Ctx, stmt, rel_path: str) -> None:
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return
    if not isinstance(stmt.targets[0], ast.Name):
        return
    if not _is_structtype_call(stmt.value):
        return
    ds_name = stmt.targets[0].id
    ds_id = store.add_node(
        ctx.conn, ctx.ds_t, name=ds_name, qualified_name=ds_name,
        file_path=rel_path, line_start=stmt.lineno,
        metadata={"source_kind": "schema"},
    )
    for field_name, dtype in _structfields(stmt.value):
        cid = ctx.column(ds_id, field_name, rel_path)
        ctx.set_col_dtype(cid, dtype, "declared-schema")


def _is_structtype_call(node) -> bool:
    return (
        isinstance(node, ast.Call)
        and _func_attr(node.func) == "StructType"
    )


def _structfields(call) -> list:
    """Yield (name, dtype) from StructType([StructField('n', XType()), ...])."""
    out = []
    if not call.args:
        return out
    first = call.args[0]
    if not isinstance(first, (ast.List, ast.Tuple)):
        return out
    for elt in first.elts:
        if not (isinstance(elt, ast.Call) and _func_attr(elt.func) == "StructField"):
            continue
        if not elt.args:
            continue
        name = _str_const(elt.args[0])
        if name is None:
            continue
        dtype = "unknown"
        if len(elt.args) >= 2:
            dtype = _func_attr(elt.args[1].func) if isinstance(elt.args[1], ast.Call) else "unknown"
        out.append((name, dtype or "unknown"))
    return out


# ── function-level dataframes / columns / transforms ─────────────────────────

def _scan_function(ctx: _Ctx, fn, rel_path: str) -> None:
    df_vars: Dict[str, int] = {}

    # Pass 1 (statement order): identify dataframe variables + derives edges.
    for stmt in ast.walk(fn):
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name):
            continue
        val = stmt.value
        if _is_df_constructor(val):
            df_vars[target.id] = store.add_node(
                ctx.conn, ctx.df_t, name=target.id, qualified_name=target.id,
                file_path=rel_path, line_start=stmt.lineno,
                metadata={"engine": _engine_of(val)},
            )
        elif _is_df_method(val, df_vars):
            base = _attr_base_name(val.func)
            new_id = store.add_node(
                ctx.conn, ctx.df_t, name=target.id, qualified_name=target.id,
                file_path=rel_path, line_start=stmt.lineno,
                metadata={"engine": "derived"},
            )
            df_vars[target.id] = new_id
            store.add_edge(ctx.conn, ctx.derives_e, df_vars[base], new_id,
                           metadata={"op": val.func.attr})

    # Pass 2: column accesses df["col"] -> column nodes + has_column.
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Subscript):
            base, col = _subscript_df_col(sub, df_vars)
            if base is not None:
                ctx.column(df_vars[base], col, rel_path)

    # Pass 3: astype dtype + transforms.
    for stmt in ast.walk(fn):
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        tbase, tcol = _subscript_df_col(stmt.targets[0], df_vars)
        if tbase is None:
            continue
        tcol_id = ctx.column(df_vars[tbase], tcol, rel_path)
        # astype on the RHS sets the dtype
        dtype = _astype_arg(stmt.value)
        if dtype is not None:
            ctx.set_col_dtype(tcol_id, dtype, "static-inference")
        # source columns on the RHS -> transforms edges
        for src in ast.walk(stmt.value):
            if isinstance(src, ast.Subscript):
                sbase, scol = _subscript_df_col(src, df_vars)
                if sbase is not None and scol != tcol:
                    src_id = ctx.column(df_vars[sbase], scol, rel_path)
                    store.add_edge(ctx.conn, ctx.transforms_e, src_id, tcol_id,
                                   metadata={"to": tcol})


# ── small AST helpers ────────────────────────────────────────────────────────

def _is_df_constructor(node) -> bool:
    if not isinstance(node, ast.Call):
        return False
    attr = _func_attr(node.func)
    if attr in _DF_CONSTRUCTORS:
        return True
    if attr and attr.startswith(_DF_READERS_PREFIX):
        return True
    return False


def _engine_of(node) -> str:
    attr = _func_attr(node.func)
    if attr == "createDataFrame":
        return "pyspark"
    return "pandas"


def _is_df_method(node, df_vars) -> bool:
    """A `<known_df>.<method>(...)` call -> derives a new dataframe."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    base = _attr_base_name(node.func)
    return base is not None and base in df_vars


def _attr_base_name(func) -> Optional[str]:
    """For df.method, return 'df' when the receiver is a bare Name."""
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return None


def _subscript_df_col(node, df_vars) -> Tuple[Optional[str], Optional[str]]:
    """For df["col"] return (df_var, col) when df_var is a known dataframe."""
    if not isinstance(node, ast.Subscript):
        return None, None
    if not isinstance(node.value, ast.Name) or node.value.id not in df_vars:
        return None, None
    col = _str_const(_subscript_key(node.slice))
    if col is None:
        return None, None
    return node.value.id, col


def _subscript_key(slc):
    # py>=3.9 slice is the expr directly; older wraps in ast.Index
    if isinstance(slc, ast.Index):  # pragma: no cover - legacy
        return slc.value
    return slc


def _astype_arg(node) -> Optional[str]:
    """Return the dtype literal of a `.astype('int64')` call, else None."""
    if isinstance(node, ast.Call) and _func_attr(node.func) == "astype" and node.args:
        return _str_const(node.args[0])
    return None


def _func_attr(func) -> Optional[str]:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _str_const(node) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None

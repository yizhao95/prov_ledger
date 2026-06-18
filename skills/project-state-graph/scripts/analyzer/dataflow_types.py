"""Data-variable analysis: model each function's OUTPUT as a first-class
data_var node, with a captured type, plus produces/consumes edges.

This is the unlock for "I changed a function's output type -> who consumes it?".

Type capture ladder (most to least certain):
  1. return annotation  ->  e.g. "list[int]"
  2. inferred shape from return statements:
        return a, b      -> "tuple[2]"
        return {..}      -> "dict"
        return [..]      -> "list"
  3. "unknown"

Edges:
  produces : function -> data_var      (the function returns this value)
  consumes : data_var -> function      (a consumer takes the value as input)
Both carry metadata {"type": <type>, "confidence": "high"|"inferred"}.
"""
from __future__ import annotations

import ast
import os
import sqlite3
from typing import Dict, Optional, Tuple

from . import store


def _callable_index(conn: sqlite3.Connection) -> Dict[str, list]:
    """Map simple callable name -> list of node ids (function/method)."""
    rows = conn.execute(
        """SELECT n.name, n.id
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name IN ('function', 'method')"""
    ).fetchall()
    index: Dict[str, list] = {}
    for name, nid in rows:
        index.setdefault(name, []).append(nid)
    return index


def _ann_to_str(node) -> Optional[str]:
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _infer_return_type(fn) -> Tuple[str, str]:
    """Return (type_str, confidence) for a function definition."""
    if fn.returns is not None:
        ann = _ann_to_str(fn.returns)
        if ann:
            return ann, "high"
    # Walk return statements for a shape hint.
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Return) and sub.value is not None:
            val = sub.value
            if isinstance(val, ast.Tuple):
                return f"tuple[{len(val.elts)}]", "inferred"
            if isinstance(val, ast.Dict):
                return "dict", "inferred"
            if isinstance(val, (ast.List, ast.ListComp)):
                return "list", "inferred"
            if isinstance(val, ast.SetComp):
                return "set", "inferred"
            if isinstance(val, ast.DictComp):
                return "dict", "inferred"
    return "unknown", "inferred"


def _has_return_value(fn) -> bool:
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Return) and sub.value is not None:
            return True
    return False


def _call_name(func) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _target_names(target) -> list:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names = []
        for elt in target.elts:
            names.extend(_target_names(elt))
        return names
    return []


def _emit_params(conn, fn, fn_id, dv_type, feeds_e, rel_path) -> dict:
    """Type every input parameter as a data_var and `feeds` it into the fn.

    Annotated params -> dtype from annotation (provenance 'annotation').
    Unannotated params -> dtype 'unknown' (provenance 'unknown').
    self/cls are skipped.

    Returns {param_name: (data_var_id, dtype)} for downstream taint tracing.
    """
    args = fn.args
    all_args = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    if args.vararg:
        all_args.append(args.vararg)
    if args.kwarg:
        all_args.append(args.kwarg)
    param_map: dict = {}
    for a in all_args:
        if a.arg in ("self", "cls"):
            continue
        if a.annotation is not None:
            dtype = _ann_to_str(a.annotation) or "unknown"
            prov = "annotation" if dtype != "unknown" else "unknown"
        else:
            dtype, prov = "unknown", "unknown"
        dv_id = store.add_node(
            conn, dv_type, name=f"{fn.name}:param:{a.arg}",
            qualified_name=f"{fn.name}:param:{a.arg}", file_path=rel_path,
            line_start=fn.lineno,
            metadata={"type": dtype, "dtype": dtype,
                      "dtype_provenance": prov, "confidence": "high",
                      "role": "param", "param": a.arg},
        )
        store.add_edge(conn, feeds_e, dv_id, fn_id,
                       metadata={"dtype": dtype, "role": "param"})
        param_map[a.arg] = (dv_id, dtype)
    return param_map


def analyze(
    conn: sqlite3.Connection,
    repo_root: str,
    file_map: Dict[str, int],
) -> None:
    dv_type = store.get_or_create_node_type(conn, "data_var")
    produces_e = store.get_or_create_edge_type(conn, "produces")
    consumes_e = store.get_or_create_edge_type(conn, "consumes")
    feeds_e = store.get_or_create_edge_type(conn, "feeds")

    callables = _callable_index(conn)
    repo_root = os.path.abspath(repo_root)

    # function/method node id -> (data_var id, type, confidence)
    var_of: Dict[int, Tuple[int, str, str]] = {}
    # function/method node id -> {param_name: (data_var_id, dtype)}
    params_of: Dict[int, dict] = {}

    def _resolve_unique(name: str) -> Tuple[Optional[int], str]:
        ids = callables.get(name)
        if not ids:
            return None, "inferred"
        if len(ids) == 1:
            return ids[0], "high"
        return ids[0], "inferred"

    # Pass 0: type every function INPUT parameter (total dtype coverage).
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
            fn_id, _conf = _resolve_unique(fn.name)
            if fn_id is None:
                continue
            pmap = _emit_params(conn, fn, fn_id, dv_type, feeds_e, rel_path)
            if pmap:
                params_of[fn_id] = pmap

    # Pass 1: create one data_var per function that returns a value.
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
            if not _has_return_value(fn):
                continue
            fn_id, _conf = _resolve_unique(fn.name)
            if fn_id is None or fn_id in var_of:
                continue
            type_str, conf = _infer_return_type(fn)
            provenance = "annotation" if conf == "high" else (
                "unknown" if type_str == "unknown" else "static-inference"
            )
            dv_id = store.add_node(
                conn, dv_type, name=f"{fn.name}:return",
                qualified_name=f"{fn.name}:return", file_path=rel_path,
                line_start=fn.lineno,
                metadata={"type": type_str, "dtype": type_str,
                          "dtype_provenance": provenance, "confidence": conf,
                          "role": "return"},
            )
            store.add_edge(
                conn, produces_e, fn_id, dv_id,
                metadata={"type": type_str, "confidence": "high"},
            )
            var_of[fn_id] = (dv_id, type_str, conf)

    # Pass 2: consumes edges via intra-function taint tracing.
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
                fn_id, _c = _resolve_unique(fn.name)
                _trace_consumes(conn, fn, callables, var_of, consumes_e,
                                _resolve_unique,
                                params_of.get(fn_id) if fn_id is not None else None)


def _tainted_names_in(node, tainted):
    """Tainted Names referenced by an argument expression.

    Catches a value used directly (`func(data)`) or wrapped (`func([data])`,
    `func(data.x)`, `func(data["k"])`, `func(f"{data}")`). Does NOT descend into
    a nested call (`outer(inner(data))`): `data` there is consumed by `inner`,
    which the outer AST walk visits as its own consumer.

    Returns a list of (name, is_direct) where is_direct marks a bare `Name` arg.
    """
    if isinstance(node, ast.Call):
        return []  # nested call -> handled separately as its own consumer
    if isinstance(node, ast.Name):
        return [(node.id, True)] if node.id in tainted else []
    found = []
    for child in ast.iter_child_nodes(node):
        found.extend((n, False) for n, _ in _tainted_names_in(child, tainted))
    return found


def _trace_consumes(conn, fn, callables, var_of, consumes_e, resolve_unique,
                    param_map=None):
    # var name -> (data_var_id, dtype, producer_fn_id_or_None)
    # producer_fn_id is the function that produced the value (for self-consume
    # guarding); None when the value is one of THIS function's own parameters.
    tainted: Dict[str, tuple] = {}

    def _bind(target, info):
        for var in _target_names(target):
            tainted[var] = info

    # Seed: this function's own parameters are tainted data (their data_var is
    # the param node itself), so a param flowing into a call reads as data flow.
    if param_map:
        for pname, (dv_id, dtype) in param_map.items():
            tainted[pname] = (dv_id, dtype, None)

    # Pass A: assignments whose RHS is a call to a known producer.
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            pname = _call_name(stmt.value.func)
            if pname is None:
                continue
            pid, _ = resolve_unique(pname)
            if pid is None or pid not in var_of:
                continue
            dv_id, type_str, _ = var_of[pid]
            for target in stmt.targets:
                _bind(target, (dv_id, type_str, pid))

    # Pass B: propagate taint to comprehension targets that iterate a tainted
    # var, so `[transform(it) for it in data]` traces data -> transform.
    for comp in ast.walk(fn):
        if isinstance(comp, (ast.ListComp, ast.SetComp, ast.DictComp,
                             ast.GeneratorExp)):
            for gen in comp.generators:
                if isinstance(gen.iter, ast.Name) and gen.iter.id in tainted:
                    _bind(gen.target, tainted[gen.iter.id])

    seen = set()
    for sub in ast.walk(fn):
        if not isinstance(sub, ast.Call):
            continue
        cname = _call_name(sub.func)
        if cname is None:
            continue
        cid, cconf = resolve_unique(cname)
        if cid is None:
            continue
        # Candidate tainted names from positional AND keyword arguments.
        candidates = []
        for arg in sub.args:
            candidates.extend(_tainted_names_in(arg, tainted))
        for kw in sub.keywords:
            if kw.value is not None:
                candidates.extend(_tainted_names_in(kw.value, tainted))
        for var_name, is_direct in candidates:
            dv_id, type_str, producer_fn = tainted[var_name]
            # skip a value flowing into its own producer (recursion noise)
            if producer_fn is not None and cid == producer_fn:
                continue
            key = (dv_id, cid, var_name)
            if key in seen:
                continue
            seen.add(key)
            conf = cconf if is_direct else "inferred"
            store.add_edge(
                conn, consumes_e, dv_id, cid,
                metadata={"type": type_str, "confidence": conf,
                          "var": var_name},
            )

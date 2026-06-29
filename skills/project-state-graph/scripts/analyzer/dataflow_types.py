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

from . import _resolve, store
from .py_ast import _module_name


def _iter_funcs(tree, module):
    """Yield (fn_node, qualified_name) for top-level functions and class methods.

    Matches py_ast's node set + qualified_name scheme (module.fn, module.Class.method)
    so each function resolves to its OWN unique node id — fixing the C3 bug where a
    second same-named function collapsed onto the first via simple-name resolution.
    """
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node, f"{module}.{node.name}"
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield item, f"{module}.{node.name}.{item.name}"


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

    idx = _resolve.build_index(conn)
    callables = _callable_index(conn)  # retained for _trace_consumes signature
    repo_root = os.path.abspath(repo_root)

    # function/method node id -> (data_var id, type, confidence)
    var_of: Dict[int, Tuple[int, str, str]] = {}
    # function/method node id -> {param_name: (data_var_id, dtype)}
    params_of: Dict[int, dict] = {}

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
        module = _module_name(rel_path)
        for fn, qual in _iter_funcs(tree, module):
            fn_id = idx.by_qual.get(qual)   # PSG-C3: self id by UNIQUE qualified name
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
        module = _module_name(rel_path)
        for fn, qual in _iter_funcs(tree, module):
            if not _has_return_value(fn):
                continue
            fn_id = idx.by_qual.get(qual)   # PSG-C3: each same-named fn keeps its own
            if fn_id is None or fn_id in var_of:
                continue
            type_str, conf = _infer_return_type(fn)
            provenance = "annotation" if conf == "high" else (
                "unknown" if type_str == "unknown" else "static-inference"
            )
            dv_id = store.add_node(
                conn, dv_type, name=f"{fn.name}:return",
                qualified_name=f"{qual}:return", file_path=rel_path,
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
        module = _module_name(rel_path)
        for fn, qual in _iter_funcs(tree, module):
            fn_id = idx.by_qual.get(qual)
            _trace_consumes(conn, fn, idx, module, var_of, consumes_e,
                            params_of.get(fn_id) if fn_id is not None else None,
                            all_params=params_of)


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


def _expected_param_type(cparams, slot):
    """The consumer's DECLARED type for the param a value fills (PSG-C4).

    cparams is {param_name: (data_var_id, dtype)} in param order (self/cls already
    excluded). slot is ("pos", index) or ("kw", name). Returns None when it can't
    be resolved, so an ambiguous call never fabricates an expected type.
    """
    if not cparams:
        return None
    kind, k = slot
    names = list(cparams)
    if kind == "pos":
        return cparams[names[k]][1] if 0 <= k < len(names) else None
    if kind == "kw":
        return cparams[k][1] if k in cparams else None
    return None


def _trace_consumes(conn, fn, idx, module, var_of, consumes_e,
                    param_map=None, all_params=None):
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
            pid = idx.resolve_one(pname, module=module)  # taint binds ONE producer
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
        # PSG-C2: resolve the consumer with module context. Ambiguous -> emit ONE
        # edge per candidate (each `inferred`), never an arbitrary single pick.
        candidates = idx.resolve(cname, module=module)
        if not candidates:
            continue
        # Tainted args tagged with the param slot they fill (for the expected type).
        slots = []  # (var_name, is_direct, slot)
        for i, arg in enumerate(sub.args):
            for vn, direct in _tainted_names_in(arg, tainted):
                slots.append((vn, direct, ("pos", i)))
        for kw in sub.keywords:
            if kw.value is not None and kw.arg is not None:
                for vn, direct in _tainted_names_in(kw.value, tainted):
                    slots.append((vn, direct, ("kw", kw.arg)))
        for cid, cconf in candidates:
            cparams = (all_params or {}).get(cid)
            for var_name, is_direct, slot in slots:
                dv_id, type_str, producer_fn = tainted[var_name]
                # skip a value flowing into its own producer (recursion noise)
                if producer_fn is not None and cid == producer_fn:
                    continue
                key = (dv_id, cid, var_name)
                if key in seen:
                    continue
                seen.add(key)
                conf = cconf if is_direct else "inferred"
                meta = {"type": type_str, "confidence": conf, "var": var_name}
                # PSG-C4: the CONSUMER's declared param type so the e2e dtype gate
                # compares producer-output vs consumer-expected (not a tautology).
                expected = _expected_param_type(cparams, slot)
                if expected is not None:
                    meta["expected_type"] = expected
                store.add_edge(conn, consumes_e, dv_id, cid, metadata=meta)

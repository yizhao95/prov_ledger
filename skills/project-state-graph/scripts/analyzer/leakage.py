"""Data-leakage detection (Phase 4.1) — the north-star silent-failure gate.

Catches the acceptance example: the SAME dataset (same lineage root) used to both
fit AND evaluate a model, i.e. a broken/absent train-test split (evaluating on
training data, or fit+score on the raw frame).

Per-function static AST tracing:
  - `a, b, ... = train_test_split(SRC, ...)` records each target as a split output
    with lineage_root = SRC and a role (train / test / validation) inferred from
    the target name, else positionally.
  - `model.fit(D)` / `.train(D)`         -> model fits on D
    `model.predict/score/evaluate(D)`    -> model evaluates on D
  - LEAK when a model fits on F and evaluates on E whose lineage roots are the
    SAME and the pair is NOT a proper split (fit=train, eval=test/validation).

Emitted to the graph as `leakage` nodes so selfcheck can gate on them (ERROR).
Conservative: only flags a shared root; distinct sources never trip it.
"""
from __future__ import annotations

import ast
import os
import sqlite3
from typing import Dict, List, Optional

from . import store

_SPLIT_FUNCS = {"train_test_split"}
_FIT_METHODS = {"fit", "train"}
_EVAL_METHODS = {"predict", "score", "evaluate"}
_EVAL_ROLES = {"test", "validation"}


def _first_arg_name(call: ast.Call) -> Optional[str]:
    if call.args and isinstance(call.args[0], ast.Name):
        return call.args[0].id
    return None


def _attr_root(value) -> Optional[str]:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return _attr_root(value.value)
    if isinstance(value, ast.Call):
        return _attr_root(value.func)
    return None


def _is_split_call(node) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    name = f.id if isinstance(f, ast.Name) else (
        f.attr if isinstance(f, ast.Attribute) else None)
    return name in _SPLIT_FUNCS


def _target_names(targets) -> List[str]:
    out: List[str] = []
    for t in targets:
        if isinstance(t, (ast.Tuple, ast.List)):
            out.extend(n.id for n in t.elts if isinstance(n, ast.Name))
        elif isinstance(t, ast.Name):
            out.append(t.id)
    return out


def _role(name: str, i: int, n: int) -> str:
    low = name.lower()
    if "val" in low:
        return "validation"
    if "test" in low:
        return "test"
    if "train" in low:
        return "train"
    if n >= 3:
        return ["train", "validation", "test"][i] if i < 3 else "test"
    return ["train", "test"][i] if i < 2 else "test"


def detect(source: str) -> List[dict]:
    """Return leakage findings for one source file (pure AST; never raises)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    out: List[dict] = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.extend(_scan_function(fn))
    return out


def _scan_function(fn) -> List[dict]:
    # var -> (lineage_root, role)
    split_out: Dict[str, tuple] = {}
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Assign) and _is_split_call(stmt.value):
            root = _first_arg_name(stmt.value) or f"<split@{stmt.lineno}>"
            names = _target_names(stmt.targets)
            for i, nm in enumerate(names):
                split_out[nm] = (root, _role(nm, i, len(names)))

    # model receiver -> {"fit": set(vars), "eval": set(vars)}
    models: Dict[str, Dict[str, set]] = {}
    for sub in ast.walk(fn):
        if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)):
            continue
        recv = _attr_root(sub.func.value)
        dvar = _first_arg_name(sub)
        if recv is None or dvar is None:
            continue
        meth = sub.func.attr
        if meth in _FIT_METHODS:
            models.setdefault(recv, {"fit": set(), "eval": set()})["fit"].add(dvar)
        elif meth in _EVAL_METHODS:
            models.setdefault(recv, {"fit": set(), "eval": set()})["eval"].add(dvar)

    leaks: List[dict] = []
    for recv, use in models.items():
        for f in use["fit"]:
            for e in use["eval"]:
                froot, frole = split_out.get(f, (f, "raw"))
                eroot, erole = split_out.get(e, (e, "raw"))
                if froot != eroot:
                    continue  # different sources -> no leakage
                proper = frole == "train" and erole in _EVAL_ROLES
                if proper:
                    continue
                leaks.append({
                    "function": fn.name, "model": recv,
                    "fit_var": f, "fit_role": frole,
                    "eval_var": e, "eval_role": erole, "root": froot,
                    "detail": (f"model '{recv}' fits on '{f}' ({frole}) and evaluates "
                               f"on '{e}' ({erole}) from the same source '{froot}' "
                               f"— data leakage / dual-use (no proper train/test split)"),
                })
    return leaks


_MODEL_DATA_METHODS = _FIT_METHODS | _EVAL_METHODS
_VALIDATOR_NAMES = {"validate", "check", "check_schema", "expect", "assert_schema"}


def _function_has_guard(fn) -> bool:
    """A function 'guards' its data if it asserts or calls a validator (pandera/GE
    style). Conservative: any guard in the function suppresses the warning."""
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Assert):
            return True
        if isinstance(sub, ast.Call):
            f = sub.func
            name = f.id if isinstance(f, ast.Name) else (
                f.attr if isinstance(f, ast.Attribute) else None)
            if name and ("validat" in name.lower() or name in _VALIDATOR_NAMES):
                return True
    return False


def detect_unguarded_inputs(source: str) -> List[dict]:
    """Model inputs (.fit/.predict/... args) in a function with NO validation
    guard (Phase 4.2, example B static half). Value-level failures like an all-null
    batch -> predict all-0 can only be caught at runtime; the static gate just
    ensures a guard EXISTS."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    out: List[dict] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        inputs = []
        for sub in ast.walk(fn):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in _MODEL_DATA_METHODS):
                dv = _first_arg_name(sub)
                recv = _attr_root(sub.func.value)
                if dv and recv:
                    inputs.append((recv, dv, sub.func.attr))
        if not inputs or _function_has_guard(fn):
            continue
        seen = set()
        for recv, dv, meth in inputs:
            if (recv, dv) in seen:
                continue
            seen.add((recv, dv))
            out.append({
                "function": fn.name, "model": recv, "input": dv, "method": meth,
                "detail": (f"model input '{dv}' fed to '{recv}.{meth}' with no "
                           f"validation guard in '{fn.name}' — a null/degenerate "
                           f"batch would fail silently (e.g. predict all-0)"),
            })
    return out


def scan(repo_root: str, file_map: Dict[str, int]) -> List[dict]:
    """Leakage findings across all .py files in the repo."""
    repo_root = os.path.abspath(repo_root)
    out: List[dict] = []
    for rel_path in file_map:
        if not rel_path.endswith(".py"):
            continue
        try:
            with open(os.path.join(repo_root, rel_path), encoding="utf-8") as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        for leak in detect(src):
            leak["file"] = rel_path
            out.append(leak)
    return out


def analyze(conn: sqlite3.Connection, repo_root: str,
            file_map: Dict[str, int]) -> None:
    """Analyzer entry point: emit one `leakage` node per finding so selfcheck can
    gate on it (ERROR)."""
    leak_t = store.get_or_create_node_type(conn, "leakage")
    for leak in scan(repo_root, file_map):
        store.add_node(
            conn, leak_t, name=f"{leak['function']}:{leak['model']}",
            qualified_name=f"{leak.get('file', '')}:{leak['function']}:{leak['model']}",
            file_path=leak.get("file"), metadata={**leak, "kind": "data_leakage"},
        )

    # Phase 4.2: unguarded model inputs (emit `unguarded_input` nodes).
    repo_abs = os.path.abspath(repo_root)
    unguarded_t = store.get_or_create_node_type(conn, "unguarded_input")
    for rel_path in file_map:
        if not rel_path.endswith(".py"):
            continue
        try:
            with open(os.path.join(repo_abs, rel_path), encoding="utf-8") as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        for f in detect_unguarded_inputs(src):
            store.add_node(
                conn, unguarded_t, name=f"{f['function']}:{f['input']}",
                qualified_name=f"{rel_path}:{f['function']}:{f['input']}",
                file_path=rel_path, metadata={**f, "file": rel_path,
                                              "kind": "unguarded_model_input"},
            )
